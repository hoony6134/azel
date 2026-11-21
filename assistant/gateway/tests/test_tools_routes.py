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
    assert [t["name"] for t in tools] == ["call_user", "schedule_list", "schedule_cancel", "create_task", "task_status", "approval_status"]
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
    assert sent[1] == {"id": "t1", "type": "tool_result", "result": "지금 전화를 걸게요.", "is_error": False}


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
    assert notes == [("call", "task", "맡기신 작업 끝났어요. 정리했어요.", "azel")]  # "call" on finish rings
    assert c.post("/v1/tasks", json={"goal": "x", "backend": "gpt"}).status_code == 400


def test_task_backend_setting(tgw, monkeypatch):  # noqa: F811
    g, c, notes = tgw
    assert c.get("/v1/settings").json()["task_backend"] == "claude_code"
    assert c.put("/v1/settings", json={"task_backend": "nope"}).status_code == 400
    assert c.put("/v1/settings", json={"task_backend": "claude_api"}).status_code == 200
    r = c.post("/v1/tasks", json={"goal": "x", "notify": "none"})
    t = wait_for(c, r.json()["id"], ("done", "failed"))
    assert t["backend"] == "claude_api" and t["status"] == "failed" and "키" in t["error"]  # no API key in tests


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
