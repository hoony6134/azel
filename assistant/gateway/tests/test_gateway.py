import importlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

HERE = Path(__file__).parent


@pytest.fixture
def gw(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("t0k\n")
    monkeypatch.setenv("BISEO_HOME", str(tmp_path))
    monkeypatch.setenv("BISEO_CLAUDE", str(HERE / "fake_claude.py"))
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sys.path.insert(0, str(HERE.parent))
    import gateway
    g = importlib.reload(gateway)

    async def no_link():  # tests drive the link by hand
        return None
    g.voice_link = no_link
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        yield g, c, tmp_path


def events(resp):
    return [json.loads(line) for line in resp.text.splitlines()]


def test_token_required(gw):
    g, c, _ = gw
    assert c.get("/v1/settings", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/v1/settings", headers={"Authorization": ""}).status_code == 401


def test_ask_streams_and_records_usage(gw):
    g, c, tmp = gw
    ev = events(c.post("/v1/ask", json={"text": "서버 괜찮아?", "session": "s1", "model": "opus"}))
    assert "".join(e["delta"] for e in ev if e["type"] == "text") == "네, 확인했어요."
    assert ev[-1]["type"] == "done" and ev[-1]["path"] == "claude_code" and ev[-1]["model"] == "opus"
    args = json.loads((tmp / "args.json").read_text())
    assert args[args.index("--model") + 1] == "claude-opus-5-5"
    assert "--resume" not in args
    c.post("/v1/ask", json={"text": "또", "session": "s1"})  # same voice session resumes
    args = json.loads((tmp / "args.json").read_text())
    assert args[args.index("--resume") + 1] == "sess-1"
    assert c.get("/v1/usage").json()["cost_usd"] == 1.0


def test_budget_blocks(gw):
    g, c, _ = gw
    c.put("/v1/settings", json={"monthly_budget_usd": 0.4})
    c.post("/v1/ask", json={"text": "a"})
    ev = events(c.post("/v1/ask", json={"text": "b"}))
    assert ev == [{"type": "error", "code": "budget", "message": "이번 달 Claude 한도를 다 썼습니다."}]


def test_settings_reject_unknown(gw):
    g, c, _ = gw
    assert c.put("/v1/settings", json={"nope": 1}).status_code == 400
    assert c.put("/v1/settings", json={"default_model": "opus"}).json()["default_model"] == "opus"


def test_devices_and_mcp_config(gw):
    g, c, tmp = gw
    assert c.post("/v1/devices/register", json={"id": "맥북", "name": "맥북", "url": "u"}).status_code == 400
    c.post("/v1/devices/register", json={"id": "macbook", "name": "맥북", "url": "http://x/mcp"})
    c.post("/v1/ask", json={"text": "a"})
    args = json.loads((tmp / "args.json").read_text())
    assert "mcp__macbook" in args and "--mcp-config" in args
    assert not Path(args[args.index("--mcp-config") + 1]).exists()  # token-bearing file removed


def test_approval_flow(gw):
    g, c, _ = gw
    aid = c.post("/v1/approvals", json={"device": "macbook", "tool": "run_command",
                                        "command": "git push", "cwd": "~/dev/x"}).json()["approval_id"]
    assert [a["id"] for a in c.get("/v1/approvals").json()] == [aid]
    assert c.post(f"/v1/approvals/{aid}", json={"approve": True}).json()["status"] == "approved"
    assert c.post(f"/v1/approvals/{aid}", json={"approve": False}).status_code == 409
    got = c.get(f"/v1/approvals/{aid}").json()
    assert got["status"] == "approved" and got["command"] == "git push"


def test_alert_places_call_without_apns(gw):
    g, c, _ = gw
    g.in_quiet_hours = lambda s, now=None: False
    r = c.post("/v1/alerts", json={"alerts": [{"status": "firing", "labels": {"severity": "critical", "alertname": "NodeDown"}}]}).json()
    assert r["called"] and r["sent"] == 0
    assert "NodeDown" in c.get(f"/v1/calls/{r['call_id']}").json()["opening"]
    r = c.post("/v1/alerts", json={"alerts": [{"status": "firing", "labels": {"severity": "warning"}}]}).json()
    assert r == {"called": False}


def test_quiet_hours(gw):
    g, _, _ = gw
    s = {"proactive": {"quiet_hours": ["23:30", "07:30"]}}
    at = lambda hm: datetime.strptime(f"2026-11-21 {hm}", "%Y-%m-%d %H:%M")  # noqa: E731
    assert g.in_quiet_hours(s, at("23:45")) and g.in_quiet_hours(s, at("03:00"))
    assert not g.in_quiet_hours(s, at("08:00")) and not g.in_quiet_hours(s, at("23:00"))


def test_device_prompt_fallback(gw):
    g, _, _ = gw
    s = g.load_settings()
    devs = [{"name": "알비레오", "id": "albireo"}, {"name": "스파크", "id": "spark"}]
    p = g.device_prompt(s, devs)
    assert p.index("알비레오") < p.index("스파크") and "기본 기기 맥북는 꺼져" in p
    p = g.device_prompt(s, [{"name": "맥북", "id": "macbook"}] + devs)
    assert p.index("맥북") < p.index("알비레오") and "꺼져" not in p
    assert "없어" in g.device_prompt(s, [])


def test_deadline_call_only_when_due(gw, monkeypatch):
    g, _, _ = gw
    import asyncio
    placed = []

    async def fake_place(reason, opening=None):
        placed.append((reason, opening))
        return {"call_id": "x"}
    monkeypatch.setattr(g, "place_call", fake_place)
    for answer, expect in [("NONE", 0), ("마감이 다가오는 게 있어요. 토론지가 내일 23시까지예요.", 1)]:
        async def fake_ask(*a, **k):
            return answer
        monkeypatch.setattr(g, "ask_text", fake_ask)
        placed.clear()
        asyncio.run(g.deadline_call())
        assert len(placed) == expect


def test_link_serves_ask_and_cancel(gw):
    g, _, _ = gw
    import asyncio
    sent = []

    class FakeWS:
        def __init__(self, msgs):
            self.msgs = msgs

        async def send(self, raw):
            sent.append(json.loads(raw))

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for m in self.msgs:
                yield json.dumps(m)
                await asyncio.sleep(0.3)
            await asyncio.sleep(3)  # stay connected until the fake claude finishes

    async def run():
        ws = FakeWS([{"type": "ask", "id": "a1", "text": "서버 괜찮아?", "session": "s"},
                     {"type": "ask", "id": "a2", "text": "느린 질문"}, {"type": "cancel", "id": "a2"}])
        g.link["ws"] = ws
        await g.handle_link(ws)
        await asyncio.sleep(0.5)
    asyncio.run(run())
    a1 = [m for m in sent if m.get("id") == "a1"]
    assert "".join(m["delta"] for m in a1 if m["type"] == "text") == "네, 확인했어요." and a1[-1]["type"] == "done"
    assert not any(m.get("id") == "a2" and m["type"] == "done" for m in sent)  # cancelled before finishing
