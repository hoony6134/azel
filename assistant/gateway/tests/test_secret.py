"""Secret calls on the gateway: no stored Claude session, no storing tools or link fast paths, approvals only once."""
import asyncio
import contextvars
import json

from test_gateway import TestClient, events, start  # noqa: F401
import pytest

SECRET = "azel:secret-0123456789ab"


@pytest.fixture
def sgw(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch, BISEO_DEVICE="macbook")
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        yield g, c, tmp_path


def in_secret(g, coro_fn, key=f"ask:{SECRET}"):
    ctx = contextvars.copy_context()
    ctx.run(g.requester_ctx.set, key)
    return ctx.run(asyncio.run, coro_fn())


def test_secret_ask_keeps_no_claude_session(sgw):
    g, c, tmp = sgw
    for _ in range(2):
        ev = events(c.post("/v1/ask", json={"text": "안녕", "session": SECRET, "model": "opus"}))
        assert ev[-1]["type"] == "done"
        args = json.loads((tmp / "args.json").read_text())
        assert "--no-session-persistence" in args and "--resume" not in args
    assert not [k for k in g.fake.db.get("sessions", {}) if "secret" in k]  # never stored for that key
    usage = [r for r in g.fake.db.get("usage", [])] if isinstance(g.fake.db.get("usage"), list) else []
    assert all("안녕" not in json.dumps(r, ensure_ascii=False) for r in usage)  # cost rows carry no text
    ev = events(c.post("/v1/ask", json={"text": "안녕", "session": "azel:call-1", "model": "opus"}))
    assert "--no-session-persistence" not in json.loads((tmp / "args.json").read_text())  # normal calls unchanged


def test_storing_tools_are_refused_and_reads_still_work(sgw):
    g, c, _ = sgw
    for name, args in [("remember", {"fact": "좋아하는 커피는 라떼"}), ("schedule_create", {"reason": "약", "when_text": "3시"}),
                       ("create_task", {"goal": "정리해 줘", "notify": "none"}), ("call_user", {"reason": "알림", "when_text": "3시"}),
                       ("forget", {"id": "1", "confirmed": True}), ("schedule_cancel", {"id": "abc"})]:
        r = in_secret(g, lambda: g.bridge.run(name, args, caller="claude_code"))
        assert not r["ok"] and r["spoken"] == g.SECRET_LINE, (name, r)
    r = in_secret(g, lambda: g.bridge.run("memory_recall", {"query": "커피"}, caller="claude_code"))
    assert r["spoken"] != g.SECRET_LINE  # reading memories is allowed
    r = asyncio.run(g.bridge.run("remember", {"fact": "좋아하는 커피는 라떼"}, caller="claude_code"))
    assert r["spoken"] != g.SECRET_LINE  # outside a secret call nothing changes


def test_link_fast_paths_refuse_in_secret(sgw, monkeypatch):
    g, c, _ = sgw
    sent = []

    async def link_send(m):
        sent.append(m)
    monkeypatch.setattr(g, "link_send", link_send)

    async def go():
        await g.served(g.serve_task({"id": "t1", "type": "task", "goal": "정리해 줘", "origin_session": SECRET}),
                       {"origin_session": SECRET})
        await g.served(g.serve_schedule({"id": "s1", "type": "schedule", "action": "create", "when": "3시",
                                         "origin_session": SECRET}), {"origin_session": SECRET})
        await g.served(g.serve_schedule({"id": "s2", "type": "schedule", "action": "list", "origin_session": SECRET}),
                       {"origin_session": SECRET})
    asyncio.run(go())
    by = {m["id"]: m for m in sent}
    assert by["t1"]["ok"] is False and by["t1"]["say"] == g.SECRET_LINE
    assert by["s1"]["ok"] is False and by["s1"]["say"] == g.SECRET_LINE
    assert by["s2"]["ok"] is True  # listing reads only
    assert not g.fake.db["schedule"] and not [k for k in g.fake.db["kv"] if k.startswith("task-")]


def test_always_from_a_secret_call_becomes_once(sgw):
    g, c, _ = sgw
    cmd = {"device": "macbook", "tool": "run_command", "command": "ls ~/dev", "cwd": "~"}
    aid = c.post("/v1/approvals", json={**cmd, "requester": f"ask:{SECRET}"}).json()["approval_id"]
    r = c.post(f"/v1/approvals/{aid}", json={"decision": "always"}).json()
    assert r["decision"] == "once" and r["forced_once"] and r["forced_reason"] == g.SECRET_ALWAYS
    assert c.get("/v1/approvals/rules").json() == []
    aid = c.post("/v1/approvals", json={**cmd, "requester": "ask:azel:call-1"}).json()["approval_id"]
    assert c.post(f"/v1/approvals/{aid}", json={"decision": "always"}).json()["decision"] == "always"


def test_is_secret():
    import gateway as g
    assert g.is_secret(SECRET) and g.is_secret(f"ask:{SECRET}") and g.is_secret("secret-abc")
    assert not g.is_secret("azel:call-1") and not g.is_secret("ask:azel:my-secret-plan") and not g.is_secret(None)
