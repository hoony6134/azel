"""The tool executor through the gateway: /v1/tools, /v1/tools/call, /v1/mcp, the link, and API-backed tasks."""
import asyncio
import json

from test_gateway import TestClient, private, start
from test_tasks import tgw, wait_for  # noqa: F401  (fixture)


def tailnet(g):
    return TestClient(g.app, headers={"Authorization": "Bearer t0k"}, client=("100.64.0.8", 5))


def test_tools_routes_are_tailnet_only(tgw):  # noqa: F811
    g, c, notes = tgw
    assert c.get("/v1/tools").status_code == 403  # "testclient" is no tailnet address
    t = tailnet(g)
    assert t.get("/v1/tools").status_code == 200
    for path, kw in (("/v1/tools", {}), ("/v1/mcp", {"json": {"jsonrpc": "2.0", "id": 1, "method": "ping"}}),
                     ("/v1/tools/call", {"json": {"name": "schedule_list"}})):
        method = t.get if path == "/v1/tools" else t.post
        assert method(path, headers={"X-Forwarded-For": "1.2.3.4"}, **kw).status_code == 403, path  # the public proxy


def test_tools_routes_and_call_user_refusal_in_a_call(tgw):  # noqa: F811
    g, _, notes = tgw
    c = tailnet(g)
    names = [t["name"] for t in c.get("/v1/tools").json()]
    assert "call_user" in names and "create_task" in names
    r = c.post("/v1/tools/call", json={"name": "call_user", "args": {"reason": "회의 알림"}, "persona_id": "azel"}).json()
    assert r == {"content": "지금 전화를 걸게요.", "is_error": False, "waited_s": 0}
    assert notes[-1] == ("call", "user_request", "말씀하신 대로 전화드렸어요. 회의 알림", "azel")
    g.call_warm_keys["log1"] = "azel:s"  # a call is up
    r = c.post("/v1/tools/call", json={"name": "call_user", "args": {"reason": "x"}}).json()
    assert r["content"].startswith("이미 통화 중이에요") and len(notes) == 1
    g.call_warm_keys.clear()
    log = next(v for k, v in g.fake.db["kv"].items() if k.startswith("user-calls-"))
    assert [x["reason"] for x in log] == ["회의 알림"]
    assert c.post("/v1/tools/call", json={"args": {}}).status_code == 400


def test_mcp_server_for_claude_code(tgw):  # noqa: F811
    g, _, notes = tgw
    c = tailnet(g)
    init = c.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).json()
    assert init["result"]["serverInfo"]["name"] == "azel"
    tools = c.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["call_user", "schedule_list", "schedule_cancel", "schedule_update", "create_task", "task_status", "task_cancel", "approval_status", "memory_recall", "people_lookup", "remember", "forget", "memory_list", "web_search", "web_fetch", "schedule_create"]
    assert c.post("/v1/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    assert c.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "call_user", "arguments": {"reason": "다 됐어요"}}}).json()["result"]["isError"] is False
    assert notes[-1][1] == "user_request"


def test_tools_over_the_link(tgw):  # noqa: F811
    g, c, notes = tgw
    sent = []

    async def link_send(obj):
        sent.append(obj)
        return True
    g.link_send = link_send
    asyncio.run(g.serve_tool({"type": "tools_list", "id": "l1"}))
    asyncio.run(g.serve_tool({"type": "tool_call", "id": "t1", "name": "call_user", "args": {"reason": "알림"},
                              "persona": "azel"}))
    assert sent[0]["type"] == "tools" and "call_user" in [t["name"] for t in sent[0]["tools"]]
    assert sent[1] == {"id": "t1", "type": "tool_result", "result": "지금 전화를 걸게요.", "is_error": False,
                       "spoken": "지금 전화를 걸게요.", "needs": None}


def test_api_backed_task_calls_when_done(tgw, monkeypatch):  # noqa: F811
    g, c, notes = tgw
    seen = {}

    async def fake_run(backend, system, goal, bridge, cfg, budget, minutes, cancelled, persona_id=None):
        seen.update(backend=backend, goal=goal, cfg=cfg, persona=persona_id)
        yield {"type": "activity", "phase": "tool_start", "tool": "macbook__list_dir"}
        yield {"type": "done", "result": "정리했어요.", "cost_usd": 0.02, "steps": 2}
    monkeypatch.setattr(g.agent_loop, "run", fake_run)
    r = c.post("/v1/tasks", json={"goal": "다운로드 정리", "notify": "call", "persona": "azel", "backend": "qwen"})
    t = wait_for(c, r.json()["id"], ("done", "failed"))
    assert t["status"] == "done" and t["backend"] == "qwen" and t["result"] == "정리했어요." and t["cost_usd"] == 0.02
    assert seen["backend"] == "qwen" and seen["cfg"]["url"].endswith("/v1/chat/completions") and seen["persona"] == "azel"
    assert notes == [("call", "task", "맡기신 다운로드 정리 끝났어요. 정리했어요.", "azel")]  # "call" on finish rings
    assert t["fallback_reason"] is None
    assert c.post("/v1/tasks", json={"goal": "x", "backend": "gpt"}).status_code == 400


def test_task_backend_setting(tgw, monkeypatch):  # noqa: F811
    g, c, notes = tgw
    assert c.get("/v1/settings").json()["task_backend"] == "claude_api"  # the default since batch 5
    assert c.put("/v1/settings", json={"task_backend": "nope"}).status_code == 400
    assert c.put("/v1/settings", json={"task_backend": "nim"}).status_code == 400  # NIM is off and has no key
    r = c.post("/v1/tasks", json={"goal": "x", "notify": "none"})
    t = wait_for(c, r.json()["id"], ("done", "failed"))
    assert t["backend"] == "claude_code" and t["fallback_reason"] == "API 키가 없어 Claude Code로 했어요."
    g.API_KEY = "sk-test"  # a key, but the budget is spent
    monkeypatch.setattr(g, "month_cost", lambda settings=None: asyncio.sleep(0, result=10**6))
    t = c.post("/v1/tasks", json={"goal": "x", "notify": "none"}).json()
    assert t["backend"] == "claude_code" and t["fallback_reason"] == "API 예산을 다 써서 Claude Code로 했어요."
    t = c.post("/v1/tasks", json={"goal": "이번 주 캘린더 일정 정리해 줘", "notify": "none"}).json()
    assert t["fallback_reason"] == "구글이 연결되지 않아 Claude Code로 했어요."


def test_api_task_falls_back_when_the_key_is_refused(tgw, monkeypatch):  # noqa: F811
    g, c, notes = tgw
    g.API_KEY = "sk-revoked"

    async def refused(backend, system, goal, bridge, cfg, budget, minutes, cancelled, persona_id=None):
        yield {"type": "failed", "kind": "auth", "message": "API 키가 맞지 않아요.", "cost_usd": 0.0}
    monkeypatch.setattr(g.agent_loop, "run", refused)
    t = wait_for(c, c.post("/v1/tasks", json={"goal": "정리", "notify": "none"}).json()["id"], ("done", "failed"))
    assert t["status"] == "done" and t["backend"] == "claude_code"  # the same goal, on claude -p
    assert t["fallback_reason"] == "API 키가 맞지 않아 Claude Code로 했어요."
    assert [s["kind"] for s in t["steps"]][0] == "fallback"


def test_google_tools_only_with_a_live_grant(tgw, tmp_path):  # noqa: F811
    g, c0, notes = tgw
    c = tailnet(g)
    assert g.google_status() == "none" and "gmail_search" not in [t["name"] for t in c.get("/v1/tools").json()]
    for name, data in (("google_client.json", {"installed": {"client_id": "x", "client_secret": "y",
                                                             "token_uri": "https://t", "auth_uri": "https://a"}}),
                       ("google_oauth.json", {"refresh_token": "r", "expired": False})):
        (g.HOME / name).write_text(json.dumps(data))
        (g.HOME / name).chmod(0o600)
    assert g.google_status() == "connected"
    assert {"calendar_events", "gmail_search", "gmail_read"} <= {t["name"] for t in c.get("/v1/tools").json()}
    (g.HOME / "google_oauth.json").write_text(json.dumps({"refresh_token": "r", "expired": True}))
    assert g.google_status() == "expired" and "gmail_read" not in [t["name"] for t in c.get("/v1/tools").json()]
    r = c.post("/v1/tasks", json={"goal": "이번 주 캘린더 일정 정리해 줘", "backend": "qwen", "notify": "none"})
    assert r.json()["backend"] == "claude_code"  # no live grant: calendar work stays on the claude.ai connectors


def test_limited_gateway_serves_no_tools(tmp_path, monkeypatch):
    private(tmp_path / "anthropic_api_key", "sk-test\n")
    private(tmp_path / "state_token_limited", "l1m\n")
    g = start(tmp_path, monkeypatch, BISEO_MODE="limited", BISEO_DEVICE="spark")
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}, client=("100.64.0.11", 5)) as c:
        assert c.get("/v1/tools").status_code == 403
        assert c.post("/v1/tools/call", json={"name": "call_user", "args": {"reason": "x"}}).status_code == 403
        assert c.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 403
    assert g.google_status() == "none" and g.bridge.d["google"]() == ([], None)


def test_external_token_sees_only_its_scope_on_mcp(tgw):  # noqa: F811
    g, _, notes = tgw
    ext = "e" * 40
    path = g.HOME / "mcp_external_token"
    path.write_text(ext + "\n")
    path.chmod(0o600)
    full = tailnet(g)
    outside = TestClient(g.app, headers={"Authorization": f"Bearer {ext}"}, client=("100.64.0.8", 5))

    def names(c):
        r = c.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return [t["name"] for t in r.json()["result"]["tools"]]
    assert "approval_status" in names(full) and "approval_status" not in names(outside)
    assert set(names(outside)) == g.tools_bridge.SCOPES["external"]
    r = outside.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                      "params": {"name": "approval_status", "arguments": {}}})
    assert r.json()["error"] == {"code": -32602, "message": "unknown tool"}  # same as a tool that doesn't exist
    for method, route in (("GET", "/v1/settings"), ("GET", "/v1/tools"), ("GET", "/v1/schedule"),
                          ("POST", "/v1/tools/call"), ("POST", "/v1/ask"), ("GET", "/v1/mcp")):
        assert outside.request(method, route, json={}).status_code in (401, 405), route  # only POST /v1/mcp
    ok = outside.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                       "params": {"name": "create_task", "arguments": {"goal": "x", "notify": "none"}}})
    assert ok.json()["result"]["isError"] is False
    path.chmod(0o644)  # readable by others: not a token any more
    assert outside.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "ping"}).status_code == 401
    path.chmod(0o600)
    path.unlink()  # revoked
    assert outside.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "ping"}).status_code == 401
    assert full.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 6, "method": "ping"}).status_code == 200
