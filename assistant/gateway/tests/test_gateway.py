import asyncio
import importlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import fake_state  # noqa: E402


def start(tmp_path, monkeypatch, **env):
    """Load a fresh gateway module wired to an in-memory fake of ruchba's state API."""
    (tmp_path / "token").write_text("t0k\n")
    monkeypatch.setenv("BISEO_HOME", str(tmp_path))
    monkeypatch.setenv("BISEO_CLAUDE", str(HERE / "fake_claude.py"))
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.path.insert(0, str(HERE.parent))
    import gateway
    g = importlib.reload(gateway)
    g.fake = fake_state.make()
    g.state.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=g.fake), base_url="http://state")

    async def no_link():  # tests drive the link by hand
        return None
    g.voice_link = no_link
    return g


@pytest.fixture
def gw(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch, BISEO_DEVICE="macbook")
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


def test_assistant_name(gw, monkeypatch):
    g, c, tmp = gw
    assert c.get("/v1/settings").json()["assistant_name"] == "아젤"
    pushes = []

    async def fake_push(payload):
        pushes.append(payload)
        return {"sent": 1}
    monkeypatch.setattr(g, "send_voip_push", fake_push)
    c.post("/v1/calls", json={"reason": "manual", "opening": "테스트예요."})
    c.post("/v1/ask", json={"text": "a"})
    args = json.loads((tmp / "args.json").read_text())
    assert pushes[-1]["caller"] == "아젤" and "'아젤'" in args[args.index("--append-system-prompt") + 1]
    assert c.put("/v1/settings", json={"assistant_name": " 루나  Luna "}).json()["assistant_name"] == "루나 Luna"
    assert g.fake.db["settings"]["assistant_name"] == "루나 Luna"  # shared: every gateway and the voice server
    c.post("/v1/calls", json={"reason": "manual", "opening": "테스트예요."})
    c.post("/v1/ask", json={"text": "b"})
    args = json.loads((tmp / "args.json").read_text())
    prompt = args[args.index("--append-system-prompt") + 1]
    assert pushes[-1]["caller"] == "루나 Luna" and "'루나 Luna'" in prompt and "아젤" not in prompt
    for bad in ["", "   ", "가" * 13, "아젤2", "R2-D2", "<b>x</b>", "아젤😀", 7, ["x"]]:
        assert c.put("/v1/settings", json={"assistant_name": bad}).status_code == 400, bad
    assert c.get("/v1/settings").json()["assistant_name"] == "루나 Luna"
    for name in ["가" * 12, "Azel", "아젤"]:
        assert c.put("/v1/settings", json={"assistant_name": name}).status_code == 200


def test_tool_activity_without_arguments(gw):
    g, c, _ = gw
    ev = events(c.post("/v1/ask", json={"text": "도구 써서 오늘 수업 알려줘"}))
    acts = [e for e in ev if e["type"] == "activity"]
    tool = {"tool": "mcp__claude_ai_Cygnus_MCP__gistra_today", "label": "학사 일정 확인 중", "kind": "schedule",
            "route": "claude"}
    assert acts == [{"type": "activity", "phase": "tool_start", **tool}, {"type": "activity", "phase": "tool_end", **tool}]
    assert "SECRET" not in json.dumps(ev)  # neither the arguments nor the result
    assert "".join(e["delta"] for e in ev if e["type"] == "text").strip() == "네, 확인했어요."


def test_tool_labels(gw):
    g, _, _ = gw
    for name, label, kind in [
        ("mcp__claude_ai_Cygnus_MCP__gistra_today", "학사 일정 확인 중", "schedule"),
        ("mcp__claude_ai_Cygnus_MCP__planit_get_day", "할 일 확인 중", "schedule"),
        ("mcp__claude_ai_Cygnus_MCP__memory_recall", "기억 찾는 중", "memory"),
        ("mcp__claude_ai_Cygnus_MCP__infra_status", "서버 상태 보는 중", "server"),
        ("mcp__claude_ai_Google_Calendar__list_events", "캘린더 확인 중", "calendar"),
        ("mcp__claude_ai_Gmail__search_threads", "메일 확인 중", "mail"),
        ("mcp__claude_ai_Notion__notion-search", "노션 보는 중", "notes"),
        ("mcp__macbook__run_command", "macbook에서 명령 실행 중", "terminal"),
        ("mcp__albireo__read_file", "albireo 파일 보는 중", "files"),
        ("mcp__albireo__list_dir", "albireo 파일 보는 중", "files"),
        ("mcp__gienah__search", "gienah 파일 보는 중", "files"), ("mcp__macbook__browse", "웹페이지 읽는 중", "web"),
        ("mcp__deneb__delegate", "deneb에 작업 맡기는 중", "terminal"),
        ("mcp__macbook__job_status", "도구 준비 중", "generic"), ("ToolSearch", "도구 준비 중", "generic"),
        ("mcp__claude_ai_Cygnus_MCP__search", "도구 준비 중", "generic"), ("", "도구 준비 중", "generic"),
    ]:
        assert g.tool_info(name) == (label, kind), name
    assert g.activity("thinking") == {"type": "activity", "phase": "thinking", "tool": "", "label": "생각 중",
                                      "kind": "generic", "route": "claude"}


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
    s = asyncio.run(g.load_settings())
    devs = [{"name": "알비레오", "id": "albireo"}, {"name": "spark", "id": "spark"}]  # old Korean names still match
    p = g.device_prompt(s, devs)
    assert p.index("albireo(") < p.index("spark(") and "기본 기기 macbook는 꺼져" in p and "알비레오" not in p
    p = g.device_prompt(s, [{"name": "맥북", "id": "macbook"}] + devs)
    assert p.index("macbook(") < p.index("albireo(") and "꺼져" not in p
    s["default_device"], s["device_fallback"] = "맥북", ["스파크"]  # settings saved before the rename
    p = g.device_prompt(s, [{"name": "맥북", "id": "macbook"}, {"name": "스파크", "id": "spark"}])
    assert p.index("macbook(") < p.index("spark(") and "꺼져" not in p
    assert "없어" in g.device_prompt(s, [])


def test_deadline_call_only_when_due(gw, monkeypatch):
    g, _, _ = gw
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


# ---------- shared state ----------
def test_state_is_shared_not_local(gw):
    g, c, tmp = gw
    c.put("/v1/settings", json={"default_model": "opus"})
    assert g.fake.db["settings"]["default_model"] == "opus"
    g.fake.db["settings"]["monthly_budget_usd"] = 7  # another gateway changed it
    assert c.get("/v1/settings").json()["monthly_budget_usd"] == 7
    c.post("/v1/ask", json={"text": "a"})
    assert g.fake.db["usage"][0]["device"] == "macbook" and g.fake.db["usage"][0]["cost_usd"] == 0.5
    assert c.get("/v1/usage").json()["by_device"] == {"macbook": 0.5}
    c.post("/v1/devices/register", json={"id": "albireo", "name": "알비레오", "url": "http://a/mcp"})
    assert g.fake.db["devices"]["albireo"]["gateway"] == "macbook"
    c.post("/v1/push/register", json={"voip_token": "tok", "bundle_id": "xyz.scian.biseo"})
    assert g.fake.db["push"]["tok"]["env"] == "sandbox"
    assert not (tmp / "gateway.db").exists()


def test_first_start_seeds_ruchba_from_old_settings(tmp_path, monkeypatch):
    (tmp_path / "settings.json").write_text(json.dumps({"default_model": "opus", "monthly_budget_usd": 300}))
    g = start(tmp_path, monkeypatch)
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        assert c.get("/v1/settings").json()["monthly_budget_usd"] == 300
    assert g.fake.db["settings"] == {"default_model": "opus", "monthly_budget_usd": 300}


def test_limited_writer_cannot_steer_full_gateways(gw, monkeypatch):
    g, c, tmp = gw
    c.post("/v1/devices/register", json={"id": "macbook", "name": "macbook", "url": "http://100.64.0.8:8893/mcp"})
    lim = TestClient(g.fake, headers={"Authorization": "Bearer l1m"})  # a team machine's gateway
    takeover = lim.put("/v1/state/devices/macbook", json={"name": "macbook", "url": "http://evil/mcp", "gateway": "x"})
    assert takeover.status_code == 403 and g.fake.db["devices"]["macbook"]["url"] == "http://100.64.0.8:8893/mcp"
    assert lim.put("/v1/state/devices/evil", json={"name": "evil", "url": "http://evil/mcp"}).status_code == 200
    c.post("/v1/ask", json={"text": "a"})
    args = json.loads((tmp / "args.json").read_text())
    assert "mcp__macbook" in args and "mcp__evil" not in args  # never handed to claude -p, never gets the token
    probed = []

    async def fake_probe(url, token=False):
        probed.append((url, token))
        return {"ok": True, "ms": 1}
    monkeypatch.setattr(g, "probe", fake_probe)
    c.get("/v1/status")
    assert ("http://evil/mcp", False) in probed and ("http://100.64.0.8:8893/mcp", True) in probed
    assert all(not tok for url, tok in probed if url.endswith("/health"))  # health checks carry no token


def test_ruchba_down_read_only(gw):
    g, c, tmp = gw
    c.put("/v1/settings", json={"default_model": "opus"})
    c.post("/v1/devices/register", json={"id": "macbook", "name": "맥북", "url": "http://x/mcp"})
    g.state.cache.clear()  # nothing cached in memory: the settings file copy and local devices must do
    g.fake.state.down = True
    assert c.get("/v1/settings").json()["default_model"] == "opus"
    assert c.put("/v1/settings", json={"default_model": "haiku"}).status_code == 503
    assert c.post("/v1/approvals", json={"device": "macbook", "command": "rm x"}).status_code == 503
    ev = events(c.post("/v1/ask", json={"text": "a", "session": "s1"}))
    assert ev[-1]["type"] == "done"  # Claude still answers
    args = json.loads((tmp / "args.json").read_text())
    assert "mcp__macbook" in args  # this device's own daemon still works
    assert len(g.pending_usage) == 1 and g.fake.db["usage"] == []
    g.fake.state.down = False
    c.post("/v1/ask", json={"text": "b"})
    assert len(g.fake.db["usage"]) == 2 and g.pending_usage == []  # the queued row was flushed


def test_outage_survives_a_restart(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch)
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        c.post("/v1/push/register", json={"voip_token": "tok1", "bundle_id": "xyz.scian.azel"})
        g.fake.state.down = True
        c.post("/v1/ask", json={"text": "a"})
    assert len(g.pending_usage) == 1 and os.stat(tmp_path / "pending_usage.json").st_mode & 0o077 == 0
    g2 = start(tmp_path, monkeypatch)  # restarted while ruchba is still down
    g2.fake.state.down = True
    assert [t["token"] for t in asyncio.run(g2.push_tokens())] == ["tok1"]  # calls still reach the phone
    assert len(g2.pending_usage) == 1
    g2.fake.state.down = False
    with TestClient(g2.app, headers={"Authorization": "Bearer t0k"}) as c:
        c.post("/v1/ask", json={"text": "b"})
    assert len(g2.fake.db["usage"]) == 2 and g2.pending_usage == []
    assert json.loads((tmp_path / "pending_usage.json").read_text()) == []


def test_rejected_usage_row_does_not_block_the_queue(gw):
    g, c, tmp = gw
    asyncio.run(g.record_usage({"model": "bad", "path": "api", "cost_usd": 1}))
    asyncio.run(g.record_usage({"model": "haiku", "path": "api", "cost_usd": 0.1}))
    assert [u["model"] for u in g.fake.db["usage"]] == ["haiku"] and g.pending_usage == []
    assert os.stat(tmp / "usage_rejected.jsonl").st_mode & 0o077 == 0  # holds usage rows: owner only
    dead = [json.loads(line) for line in (tmp / "usage_rejected.jsonl").read_text().splitlines()]
    assert dead[0]["status"] == 422 and dead[0]["row"]["model"] == "bad"


def old_gateway_db(path):
    import sqlite3
    con = sqlite3.connect(path)
    con.executescript("""
        create table usage(ts real, model text, path text, task text, input_tokens int, output_tokens int, cost_usd real);
        create table push_tokens(token text primary key, bundle_id text, env text, ts real);
        create table approvals(id text primary key, device text, tool text, command text, cwd text, status text,
            created real, decided real);""")
    now = datetime.now().timestamp()
    con.executemany("insert into usage values(?,?,?,?,?,?,?)", [(now - 60, "opus", "claude_code", "", 1, 1, 2.5),
                                                               (now - 86400 * 70, "opus", "claude_code", "", 1, 1, 9)])
    con.execute("insert into push_tokens values('tokA','xyz.scian.azel','production',0)")
    con.executemany("insert into approvals values(?,?,?,?,?,?,?,?)", [
        ("aaaaaaaaaaaa", "macbook", "run_command", "git push", "~/dev/x", "pending", now, None),
        ("bbbbbbbbbbbb", "macbook", "run_command", "rm x", "~", "approved", now, now)])
    con.commit()
    con.close()


def test_old_gateway_db_migrates_once_and_idempotently(tmp_path, monkeypatch):
    old_gateway_db(tmp_path / "gateway.db")
    g = start(tmp_path, monkeypatch)
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        for _ in range(50):  # runs in the background on start
            if (tmp_path / "migrated_from_sqlite").exists():
                break
            time.sleep(0.05)
        assert (tmp_path / "migrated_from_sqlite").exists()
        assert c.get("/v1/usage").json()["cost_usd"] == 2.5  # this month only
    assert list(g.fake.db["approvals"]) == ["aaaaaaaaaaaa"]  # pending only, same id
    assert [t["token"] for t in g.fake.db["push"].values()] == ["tokA"]
    asyncio.run(g.migrate_sqlite())  # by hand again: nothing doubles
    assert len(g.fake.db["usage"]) == 1 and len(g.fake.db["approvals"]) == 1


def test_migration_retries_when_ruchba_is_down(tmp_path, monkeypatch):
    old_gateway_db(tmp_path / "gateway.db")
    g = start(tmp_path, monkeypatch)
    g.fake.state.down = True
    asyncio.run(g.migrate_once())
    assert not (tmp_path / "migrated_from_sqlite").exists()  # tried again on the next start


CALL_LOG = [
    {"type": "call_log", "event": "start", "log_id": "L1", "session": "s1", "call_id": "c1", "direction": "outgoing",
     "reason": "morning_briefing", "client": "iphone", "opening": "좋은 아침이에요.", "started_at": 1.0},
    {"type": "call_log", "event": "turn", "log_id": "L1", "user": "오늘 일정 알려줘", "assistant": ["10시 회의예요."],
     "route": "claude", "activity": ["캘린더 확인 중"], "timings": {"total_ms": 4000}, "interrupted": False},
    {"type": "call_log", "event": "turn", "log_id": "L1", "user": "  ", "assistant": []},  # silence: skipped
    {"type": "call_log", "event": "end", "log_id": "L1", "ended_at": 61.0, "duration_s": 60.0},
]


class LinkWS:
    def __init__(self, msgs):
        self.msgs = msgs

    async def send(self, raw):
        pass

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self.msgs:
            yield json.dumps(m)


def test_call_history_round_trip(gw):
    g, c, _ = gw
    asyncio.run(g.handle_link(LinkWS(CALL_LOG)))
    h = g.fake.db["history"]["L1"]
    assert [t["user"] for t in h["turns"]] == ["오늘 일정 알려줘"] and h["ended"] and h["direction"] == "outgoing"
    assert "type" not in h and h["turns"][0]["activity"] == ["캘린더 확인 중"]
    assert c.get("/v1/history").json() == [{"id": "L1", "title": "오늘 일정 알려줘", "turns": 1}]
    assert c.get("/v1/history?q=일정").json()[0]["id"] == "L1" and c.get("/v1/history?q=없음").json() == []
    assert c.get("/v1/history/L1").json()["opening"] == "좋은 아침이에요."
    assert c.delete("/v1/history/L1").json() == {"deleted": 1}
    assert c.get("/v1/history/L1").status_code == 404 and c.delete("/v1/history/L1").status_code == 404
    assert c.delete("/v1/history?older_than_days=30").json() == {"deleted": 0} and g.fake.pruned == 30
    assert c.delete("/v1/history?older_than_days=0").status_code == 400


def test_call_history_survives_an_outage(gw):
    g, c, tmp = gw
    g.fake.state.down = True
    asyncio.run(g.handle_link(LinkWS(CALL_LOG[:2])))
    assert len(g.history_outbox.items) == 2 and (tmp / "pending_history.json").exists()
    g.fake.state.down = False
    asyncio.run(g.handle_link(LinkWS(CALL_LOG[3:])))
    assert g.fake.db["history"]["L1"]["ended"] and len(g.fake.db["history"]["L1"]["turns"]) == 1
    assert g.history_outbox.items == []


def test_history_days_setting_and_retention(gw, monkeypatch):
    g, c, _ = gw
    assert c.get("/v1/settings").json()["history_days"] == 365
    for bad in [-1, 1.5, "30", True, 99999]:
        assert c.put("/v1/settings", json={"history_days": bad}).status_code == 400, bad
    assert c.put("/v1/settings", json={"history_days": 30}).status_code == 200
    g.in_quiet_hours = lambda s, now=None: True  # 04:00 is in quiet hours; retention runs anyway
    at4 = datetime(2026, 11, 22, 4, 0, tzinfo=g.KST)
    asyncio.run(g.scheduler_tick(at4, {}))
    assert g.fake.pruned == 30
    g.fake.pruned = None
    monkeypatch.setattr(g, "INSTANCE", "albireo-x")  # another gateway, same minute: lease taken, no second prune
    asyncio.run(g.scheduler_tick(at4, {}))
    assert g.fake.pruned is None
    c.put("/v1/settings", json={"history_days": 0})  # keep forever
    monkeypatch.setattr(g, "INSTANCE", "macbook-y")
    asyncio.run(g.scheduler_tick(datetime(2026, 11, 23, 4, 0, tzinfo=g.KST), {}))
    assert g.fake.pruned is None


# ---------- scheduled calls ----------
def kst(*a):
    return datetime(*a, tzinfo=timezone(timedelta(hours=9)))


def test_schedule_api_validates_and_confirms(gw):
    g, c, _ = gw
    now = time.time()
    tomorrow10 = (datetime.now(g.KST) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    r = c.post("/v1/schedule", json={"at": tomorrow10.isoformat(), "kind": "wake"}).json()
    assert r["say"] == "내일 오전 10시에 깨워 드릴게요." and r["status"] == "pending"
    for bad in [{"at": now - 60}, {"at": now + 31 * 86400}, {"at": "2026-12-01T10:00:00"},  # no time zone
                {"at": now + 3600, "repeat": "hourly"}, {"at": now + 3600, "kind": "nap"},
                {"at": now + 3600, "note": "x" * 201}, {"at": "내일"}]:
        assert c.post("/v1/schedule", json=bad).status_code == 400, bad
    [item] = c.get("/v1/schedule").json()
    assert item["at_iso"] == tomorrow10.isoformat()
    assert c.get("/v1/schedule?status=bogus").status_code == 400
    assert c.delete(f"/v1/schedule/{item['id']}").json()["say"] == "내일 오전 10시 예약을 취소했어요."
    assert c.delete(f"/v1/schedule/{item['id']}").status_code == 404 and c.get("/v1/schedule").json() == []


def test_spoken_times(gw):
    g, _, _ = gw
    now = kst(2026, 11, 21, 22, 0).timestamp()
    for at, repeat, want in [
        (kst(2026, 11, 22, 10, 0), None, "내일 오전 10시"), (kst(2026, 11, 22, 7, 30), None, "내일 오전 7시 30분"),
        (kst(2026, 11, 21, 22, 30), None, "오늘 오후 10시 30분"), (kst(2026, 11, 23, 15, 0), None, "모레 오후 3시"),
        (kst(2026, 11, 30, 9, 0), None, "11월 30일 오전 9시"), (kst(2026, 11, 22, 7, 0), "daily", "매일 오전 7시"),
        (kst(2026, 11, 23, 6, 40), "weekdays", "평일 오전 6시 40분"), (kst(2026, 11, 23, 9, 0), "weekly:mon", "매주 월요일 오전 9시"),
        (kst(2026, 11, 22, 0, 5), None, "내일 오전 12시 5분")]:
        assert g.spoken_when(at.timestamp(), repeat, now) == want, want
    fri7 = kst(2026, 11, 27, 7, 0).timestamp()  # a Friday
    assert g.next_at(fri7, "weekdays", fri7) == kst(2026, 11, 30, 7, 0).timestamp()  # skips the weekend
    assert g.next_at(fri7, "daily", fri7) == kst(2026, 11, 28, 7, 0).timestamp()
    assert g.next_at(fri7, "weekly:fri", fri7) == kst(2026, 12, 4, 7, 0).timestamp()
    assert g.next_at(fri7, "daily", fri7 + 3 * 86400) == kst(2026, 12, 1, 7, 0).timestamp()  # skips missed days


def test_schedule_over_the_link(gw):
    g, c, _ = gw
    sent = []

    async def capture(obj):
        sent.append(obj)
        return True
    g.link_send = capture
    at = (datetime.now(g.KST) + timedelta(days=1)).replace(hour=7, minute=30, second=0, microsecond=0)
    for m in [{"type": "schedule", "id": "r1", "action": "create", "at": at.isoformat(), "kind": "wake",
               "repeat": None, "note": ""},
              {"type": "schedule", "id": "r2", "action": "list"},
              {"type": "schedule", "id": "r3", "action": "cancel"},
              {"type": "schedule", "id": "r4", "action": "cancel"},
              {"type": "schedule", "id": "r5", "action": "create", "at": time.time() - 10}]:
        asyncio.run(g.serve_schedule(m))
    by = {m["id"]: m for m in sent}
    assert by["r1"]["say"] == "내일 오전 7시 30분에 깨워 드릴게요." and by["r1"]["ok"]
    assert by["r2"]["say"] == "예약된 전화는 내일 오전 7시 30분 기상이에요."
    assert by["r3"]["say"] == "내일 오전 7시 30분 예약을 취소했어요."
    assert by["r4"]["say"] == "취소할 예약이 없어요."
    assert not by["r5"]["ok"] and by["r5"]["say"] == "그 시간으로는 예약할 수 없어요."
    assert all(m["type"] == "schedule_result" for m in sent)


def fire_setup(g, monkeypatch, **item):
    placed, alerts = [], []

    async def fake_place(reason, opening=None):
        placed.append((reason, opening))
        return {"call_id": f"call{len(placed)}"}

    async def fake_alert(title, body):
        alerts.append(body)
        return {"sent": 1}
    monkeypatch.setattr(g, "place_call", fake_place)
    monkeypatch.setattr(g, "send_alert_push", fake_alert)
    at = kst(2026, 11, 22, 7, 0).timestamp()
    g.fake.db["schedule"]["s1"] = {"id": "s1", "at": at, "repeat": None, "kind": "wake", "note": "", "created_by": "",
                                   "status": "pending", "attempts": 0, "call_id": None, "last_attempt": None, **item}
    return at, placed, alerts


def test_scheduled_call_fires_retries_and_is_missed(gw, monkeypatch):
    g, _, _ = gw
    g.in_quiet_hours = lambda s, now=None: True  # a wake call the user asked for ignores quiet hours
    at, placed, alerts = fire_setup(g, monkeypatch)
    item = g.fake.db["schedule"]["s1"]
    asyncio.run(g.fire_scheduled(at - 60))
    assert placed == []  # not yet
    for dt, n in [(0, 1), (60, 1), (120, 2), (180, 2), (240, 3), (290, 3)]:
        asyncio.run(g.fire_scheduled(at + dt))
        assert len(placed) == n and item["attempts"] == n, (dt, n)
    assert placed[0] == ("wake", "일어나실 시간이에요.") and item["call_id"] == "call3" and item["status"] == "fired"
    asyncio.run(g.fire_scheduled(at + 300))  # 60 s after the third attempt
    assert item["status"] == "missed" and len(placed) == 3
    assert alerts == ["오늘 오전 7시 기상 전화를 받지 않으셨어요."]


def test_answered_call_stops_retries_and_repeat_reschedules(gw, monkeypatch):
    g, _, _ = gw
    at, placed, _ = fire_setup(g, monkeypatch, repeat="daily", kind="reminder", note="약 드실 시간이에요.")
    item = g.fake.db["schedule"]["s1"]
    asyncio.run(g.fire_scheduled(at))
    assert placed == [("reminder", "말씀하신 시간이에요. 약 드실 시간이에요.")]
    asyncio.run(g.handle_link(LinkWS([{"type": "call_answered", "call_id": "call1"}])))
    assert item["status"] == "pending" and item["attempts"] == 0 and item["at"] > at  # tomorrow, same time
    assert datetime.fromtimestamp(item["at"], g.KST).strftime("%H:%M") == "07:00"
    asyncio.run(g.fire_scheduled(at + 600))
    assert len(placed) == 1  # answered: no retry


def test_cancelled_call_never_fires(gw, monkeypatch):
    g, c, _ = gw
    at, placed, _ = fire_setup(g, monkeypatch)
    c.delete("/v1/schedule/s1")
    asyncio.run(g.fire_scheduled(at + 10))
    assert placed == []


def test_only_the_preferred_gateway_fires(gw, monkeypatch):
    g, _, _ = gw
    at, placed, _ = fire_setup(g, monkeypatch)
    monkeypatch.setattr(g, "SCHED_PRIORITY", 1)
    monkeypatch.setattr(g, "INSTANCE", "deneb-a")
    asyncio.run(g.scheduler_tick(datetime.fromtimestamp(at - 120, g.KST), {}))  # deneb holds the lease
    monkeypatch.setattr(g, "SCHED_PRIORITY", 10)
    monkeypatch.setattr(g, "INSTANCE", "macbook-b")
    asyncio.run(g.scheduler_tick(datetime.fromtimestamp(at + 5, g.KST), {}))
    assert placed == []  # the Mac does not fire while deneb is alive
    g.fake.db["leases"]["scheduler"]["expires"] = 0  # deneb went away
    asyncio.run(g.scheduler_tick(datetime.fromtimestamp(at + 65, g.KST), {}))
    assert len(placed) == 1


def test_gateway_without_claude(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch, BISEO_NO_CLAUDE="1")
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        ev = events(c.post("/v1/ask", json={"text": "안녕"}))
        assert ev == [{"type": "error", "code": "no_claude", "message": "이 기기에서는 Claude를 쓸 수 없어요."}]
    ran = []

    async def fake_place(reason, opening=None):
        ran.append(reason)
        return {"call_id": "x"}
    monkeypatch.setattr(g, "place_call", fake_place)
    g.in_quiet_hours = lambda s, now=None: False
    asyncio.run(g.scheduler_tick(datetime(2026, 11, 21, 8, 0, tzinfo=g.KST), {}))
    assert ran == []  # no briefing without Claude


def test_resume_only_on_the_device_that_made_the_session(gw):
    g, c, tmp = gw
    g.fake.db["sessions"]["s9"] = {"claude_session": "other", "device": "albireo"}
    c.post("/v1/ask", json={"text": "a", "session": "s9"})
    assert "--resume" not in json.loads((tmp / "args.json").read_text())
    assert g.fake.db["sessions"]["s9"] == {"claude_session": "sess-1", "device": "macbook"}
    c.post("/v1/ask", json={"text": "b", "session": "s9"})
    args = json.loads((tmp / "args.json").read_text())
    assert args[args.index("--resume") + 1] == "sess-1"


def test_scheduled_job_fires_once_across_gateways(gw, monkeypatch):
    g, _, _ = gw
    ran = []

    async def fake_place(reason, opening=None):
        ran.append(g.INSTANCE)
        return {"call_id": "x"}
    monkeypatch.setattr(g, "place_call", fake_place)
    g.in_quiet_hours = lambda s, now=None: False
    g.fake.db["settings"] = {"proactive": {**g.DEFAULTS["proactive"], "triggers": {"deadline": False}}}
    now = datetime(2026, 11, 21, 8, 0, tzinfo=g.KST)
    for inst in ["macbook-aaa", "albireo-bbb", "gienah-ccc"]:  # three gateways tick in the same minute
        monkeypatch.setattr(g, "INSTANCE", inst)
        asyncio.run(g.scheduler_tick(now, {}))
    assert ran == ["macbook-aaa"]
    monkeypatch.setattr(g, "INSTANCE", "macbook-new")  # the winner restarts: still no second call
    asyncio.run(g.scheduler_tick(now, {}))
    assert ran == ["macbook-aaa"]
    g.fake.state.down = True  # no lease service: skip rather than risk several calls
    asyncio.run(g.scheduler_tick(datetime(2026, 11, 22, 8, 0, tzinfo=g.KST), {}))
    assert ran == ["macbook-aaa"]


def test_alert_calls_once_across_gateways(gw, monkeypatch):
    g, c, _ = gw
    g.in_quiet_hours = lambda s, now=None: False
    body = {"alerts": [{"status": "firing", "labels": {"severity": "critical", "alertname": "NodeDown"}}]}
    assert c.post("/v1/alerts", json=body).json()["called"]
    monkeypatch.setattr(g, "INSTANCE", "albireo-x")
    assert not c.post("/v1/alerts", json=body).json()["called"]


# ---------- limited mode (team devices) ----------
def private(path, text):
    path.write_text(text)
    os.chmod(path, 0o600)


@pytest.fixture
def limited(tmp_path, monkeypatch):
    private(tmp_path / "anthropic_api_key", "sk-test\n")
    private(tmp_path / "state_token_limited", "l1m\n")
    g = start(tmp_path, monkeypatch, BISEO_MODE="limited", BISEO_DEVICE="spark")
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        yield g, c, tmp_path


def test_limited_key_file_must_be_private(tmp_path, monkeypatch):
    (tmp_path / "anthropic_api_key").write_text("sk-test\n")
    os.chmod(tmp_path / "anthropic_api_key", 0o644)
    with pytest.raises(AssertionError, match="chmod 600"):
        start(tmp_path, monkeypatch, BISEO_MODE="limited")
    os.unlink(tmp_path / "anthropic_api_key")
    with pytest.raises(AssertionError, match="limited mode needs"):
        start(tmp_path, monkeypatch, BISEO_MODE="limited")
    private(tmp_path / "anthropic_api_key", "sk-test\n")
    with pytest.raises(AssertionError, match="state_token_limited"):
        start(tmp_path, monkeypatch, BISEO_MODE="limited")


def test_tokens_per_mode(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch)
    assert g.LINK_TOKEN == "t0k" and g.STATE_TOKEN == "t0k"  # before link tokens exist: the shared token
    private(tmp_path / "link_token", "l1nk\n")
    private(tmp_path / "anthropic_api_key", "sk-test\n")
    private(tmp_path / "state_token_limited", "l1m\n")
    g = start(tmp_path, monkeypatch, BISEO_MODE="limited")
    assert g.LINK_TOKEN == "l1nk" and g.STATE_TOKEN == "l1m"
    assert g.State("http://x").client.headers["authorization"] == "Bearer l1m"
    os.chmod(tmp_path / "link_token", 0o644)
    with pytest.raises(AssertionError, match="chmod 600"):
        start(tmp_path, monkeypatch)


def test_limited_has_no_call_history(limited):
    g, c, _ = limited
    for method, path in [("get", "/v1/history"), ("get", "/v1/history/L1"), ("delete", "/v1/history/L1"),
                         ("delete", "/v1/history?older_than_days=30"), ("get", "/v1/schedule"),
                         ("delete", "/v1/schedule/s1")]:
        assert getattr(c, method)(path).status_code == 403, path
    asyncio.run(g.handle_link(LinkWS(CALL_LOG)))
    assert g.fake.db["history"] == {} and g.history_outbox.items == []  # never stored, never queued


def test_limited_never_seeds_settings(tmp_path, monkeypatch):
    private(tmp_path / "anthropic_api_key", "sk-test\n")
    private(tmp_path / "state_token_limited", "l1m\n")
    (tmp_path / "settings.json").write_text(json.dumps({"monthly_budget_usd": 1}))
    g = start(tmp_path, monkeypatch, BISEO_MODE="limited", BISEO_DEVICE="spark")
    puts = []
    real = g.state.call

    async def spy(method, path, body=None, **params):
        puts.append((method, path))
        return await real(method, path, body, **params)
    g.state.call = spy
    s = asyncio.run(g.load_settings())
    assert ("PUT", "/v1/state/settings") not in puts and g.fake.db["settings"] == {}
    assert s["monthly_budget_usd"] == 500  # the shared (empty) settings, not this host's old copy


def test_limited_refuses_personal_data(limited, monkeypatch):
    g, c, tmp = limited

    async def fake_anthropic(body):
        return {"content": [{"type": "text", "text": "1400원이에요."}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1}}
    monkeypatch.setattr(g, "anthropic", fake_anthropic)
    for text, task in [("내일 일정 알려줘", None), ("메일 왔어?", None), ("뭐든", "schedule"), ("GISTRA 과제", None)]:
        ev = events(c.post("/v1/ask", json={"text": text, "task": task}))
        assert ev == [{"type": "error", "code": "personal", "message": "개인 비서 기능은 지금 쓸 수 없어요."}]
    ok = events(c.post("/v1/ask", json={"text": "최근 통화 내용:\n사용자: 내일 일정 알려줘\n\n고마워, 근데 환율은?"}))
    assert ok[0]["type"] == "activity" and ok[0]["phase"] == "thinking"
    assert ok[1] == {"type": "text", "delta": "1400원이에요.\n"}  # personal words in history alone do not refuse
    assert c.post("/v1/calls", json={"reason": "manual"}).status_code == 403
    assert not (tmp / "args.json").exists()  # `claude -p` never ran


def test_limited_uses_api_with_own_daemon_only(limited, monkeypatch):
    g, c, tmp = limited
    c.post("/v1/devices/register", json={"id": "spark", "name": "스파크", "url": "http://spark/mcp"})
    g.fake.db["devices"]["macbook"] = {"id": "macbook", "name": "맥북", "url": "http://mac/mcp", "harness": [],
                                       "gateway": "macbook", "last_seen": 9e12}
    rpc, bodies = [], []

    async def fake_mcp(url, method, params):
        rpc.append((url, method, params))
        if method == "tools/list":
            return {"tools": [{"name": "list_dir", "description": "ls", "inputSchema": {"type": "object"}}]}
        return {"content": [{"type": "text", "text": "a.txt b.txt"}], "isError": False}

    async def fake_anthropic(body):
        bodies.append(json.loads(json.dumps(body)))
        if len(bodies) == 1:
            return {"content": [{"type": "text", "text": "볼게요."}, {"type": "tool_use", "id": "t1",
                    "name": "spark__list_dir", "input": {"path": "~"}}], "stop_reason": "tool_use",
                    "usage": {"input_tokens": 1000, "output_tokens": 100}}
        return {"content": [{"type": "text", "text": "파일이 두 개 있어요."}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 1000, "output_tokens": 100}}
    monkeypatch.setattr(g, "mcp", fake_mcp)
    monkeypatch.setattr(g, "anthropic", fake_anthropic)
    ev = events(c.post("/v1/ask", json={"text": "스파크 홈 폴더에 뭐 있어?", "model": "sonnet", "path": "claude_code"}))
    assert "".join(e["delta"] for e in ev if e["type"] == "text") == "볼게요.\n파일이 두 개 있어요.\n"
    assert ev[-1]["path"] == "api" and ev[-1]["cost_usd"] == pytest.approx((2000 * 2 + 200 * 10) / 1e6)
    assert {u for u, _, _ in rpc} == {"http://spark/mcp"}  # never the Mac's daemon
    assert ("http://spark/mcp", "tools/call", {"name": "list_dir", "arguments": {"path": "~"}}) in rpc
    assert [t["name"] for t in bodies[0]["tools"]] == ["spark__list_dir"]
    assert bodies[0]["model"] == "claude-sonnet-5-5" and "개인 데이터" in bodies[0]["system"]
    assert bodies[1]["messages"][-1]["content"][0] == {"type": "tool_result", "tool_use_id": "t1",
                                                       "content": "a.txt b.txt", "is_error": False}
    assert not (tmp / "args.json").exists()


def test_mcp_client_parses_sse_and_json(gw, monkeypatch):
    g, _, _ = gw
    answers = iter([
        httpx.Response(200, headers={"content-type": "text/event-stream"},
                       text='event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\n'),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"message": "nope"}})])
    real = httpx.AsyncClient
    monkeypatch.setattr(g.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda r: next(answers))))
    assert asyncio.run(g.mcp("http://d/mcp", "tools/list", {})) == {"tools": []}
    with pytest.raises(RuntimeError, match="nope"):
        asyncio.run(g.mcp("http://d/mcp", "tools/list", {}))
