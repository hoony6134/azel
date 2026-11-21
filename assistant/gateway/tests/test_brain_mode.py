"""brain_mode: auto | local | claude_api | claude_code, from the setting or a call's own link frames."""
import asyncio
import json

from test_gateway import events, gw  # noqa: F401  (fixture)


def test_setting_is_validated(gw):  # noqa: F811
    g, c, _ = gw
    assert c.get("/v1/settings").json()["brain_mode"] == "auto"
    assert c.put("/v1/settings", json={"brain_mode": "qwen"}).status_code == 400
    assert c.put("/v1/settings", json={"brain_mode": "local"}).json()["brain_mode"] == "local"


def test_local_never_reaches_claude(gw):  # noqa: F811
    g, c, tmp = gw
    c.put("/v1/settings", json={"brain_mode": "local"})
    ev = events(c.post("/v1/ask", json={"text": "서버 괜찮아?", "session": "s1"}))
    assert ev[0] == {"type": "text", "delta": g.LOCAL_LINE} and ev[-1]["path"] == "local"
    assert not (tmp / "args.json").exists()  # `claude -p` never ran
    t = c.post("/v1/tasks", json={"goal": "정리", "notify": "none", "backend": "claude_code"}).json()
    assert t["backend"] == "qwen" and t["fallback_reason"] is None  # local: Qwen, even over an explicit backend
    names = [x["name"] for x in asyncio.run(g.bridge.tools("qwen"))[0]]
    assert "web_search" not in names and "web_fetch" in names  # web_search is Haiku-backed
    r = asyncio.run(g.bridge.run("web_search", {"query": "날씨"}, caller="qwen"))
    assert r["error"]["code"] == "unavailable"


def test_claude_modes_force_the_path_and_task_backend(gw, monkeypatch):  # noqa: F811
    g, c, tmp = gw
    c.put("/v1/settings", json={"brain_mode": "claude_code", "claude_path": "api"})
    ev = events(c.post("/v1/ask", json={"text": "서버 괜찮아?", "session": "s1"}))
    assert ev[-1]["path"] == "claude_code"  # over the claude_path setting
    t = c.post("/v1/tasks", json={"goal": "정리", "notify": "none", "backend": "qwen"}).json()
    assert t["backend"] == "claude_code"
    c.put("/v1/settings", json={"brain_mode": "claude_api"})
    g.API_KEY = "sk-test"
    t = c.post("/v1/tasks", json={"goal": "정리", "notify": "none"}).json()
    assert t["backend"] == "claude_api"


def test_a_calls_own_mode_wins_over_the_setting(gw):  # noqa: F811
    g, c, _ = gw
    c.put("/v1/settings", json={"brain_mode": "auto"})
    sent = []

    async def link_send(obj):
        sent.append(obj)
        return True
    g.link_send = link_send

    async def go():
        m = {"type": "ask", "id": "a1", "text": "서버 괜찮아?", "session": "azel:s9", "mode": "local"}
        await g.serve_ask(m)
        assert await g.brain_mode() == "auto"  # the call's mode never leaks into later work
    asyncio.run(go())
    assert sent[0] == {"id": "a1", "type": "text", "delta": g.LOCAL_LINE}
    m = {"type": "task", "id": "t1", "goal": "정리", "notify": "none", "mode": "local"}

    async def task():
        return await g.served(g.serve_task(m), m)
    asyncio.run(task())
    tid = next(x["task_id"] for x in sent if x.get("type") == "task_result")
    assert c.get(f"/v1/tasks/{tid}").json()["backend"] == "qwen"
