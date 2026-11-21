import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_loop  # noqa: E402
import tools_bridge as TB  # noqa: E402

TB.APPROVAL_POLL_S = 0.01


class World:
    """The gateway's side of the bridge, in memory: one device (macbook), approvals, kv, calls, schedule, tasks."""

    def __init__(self):
        self.kv, self.approvals, self.calls, self.schedule, self.tasks, self.run = {}, {}, [], [], [], []
        self.in_call = False

    async def fresh_devices(self):
        return [{"id": "macbook"}]

    async def device_tools(self, devices):
        names = ["list_dir", "run_command", "delegate"]
        return ([{"name": f"macbook__{n}", "description": n, "input_schema": {"type": "object"}} for n in names],
                {f"macbook__{n}": ("http://mac", n) for n in names})

    async def run_tool(self, block, where):
        self.run.append((block["name"], dict(block["input"])))
        inp = block["input"]
        if block["name"] == "macbook__run_command" and inp.get("command", "").startswith("open") \
                and not inp.get("approval_id"):
            aid = f"a{len(self.approvals) + 1}"
            self.approvals[aid] = {"status": "pending"}
            return {"content": json.dumps({"status": "pending_approval", "approval_id": aid}), "is_error": False}
        return {"content": f"ran {block['name']} {json.dumps(inp, ensure_ascii=False)}", "is_error": False}

    async def place_call(self, reason, opening, persona_id):
        self.calls.append((reason, opening, persona_id))
        return {"call_id": "c1"}

    async def schedule_create(self, body, created_by):
        self.schedule.append(body)
        return {"at": body["at"]}, "내일 오전 10시에 전화드릴게요."

    async def task_create(self, body, source):
        self.tasks.append(body)
        return {"id": "t000000000001"}

    async def state(self, method, path, body=None, **params):
        if path == "/v1/state/approvals" and method == "POST":
            aid = f"q{len(self.approvals) + 1}"
            self.approvals[aid] = {"status": "pending", **body}
            return {"approval_id": aid}
        if path.startswith("/v1/state/approvals/"):
            return self.approvals.get(path.rsplit("/", 1)[1])
        if path.startswith("/v1/state/kv/"):
            if method == "PUT":
                self.kv[path] = body
                return body
            return self.kv.get(path)
        raise AssertionError(path)

    def bridge(self):
        return TB.Bridge(fresh_devices=self.fresh_devices, device_tools=self.device_tools, run_tool=self.run_tool,
                         place_call=self.place_call, schedule_create=self.schedule_create,
                         task_create=self.task_create, state=self.state, in_call=lambda: self.in_call,
                         cygnus=lambda: ([{"name": "infra_health", "description": "x", "input_schema": {}}],
                                         self.cygnus_run))

    async def cygnus_run(self, name, args):
        return "albireo disk 15%"


def run(coro):
    return asyncio.run(coro)


def test_tool_list_per_caller():
    w = World()
    api = [t["name"] for t in run(w.bridge().tools("api"))[0]]
    qwen = [t["name"] for t in run(w.bridge().tools("qwen"))[0]]
    assert {"macbook__delegate", "call_user", "create_task", "infra_health"} <= set(api)
    assert "macbook__delegate" not in qwen and "macbook__run_command" in qwen  # qwen: no delegate


def test_device_approval_flow_waits_then_retries_with_the_id():
    w, said = World(), []

    async def progress(text):
        said.append(text)
        w.approvals["a1"]["status"] = "approved"  # the user taps 허용 in the app
    r = run(w.bridge().call("macbook__run_command", {"command": "open -a 'Google Chrome'"}, on_progress=progress))
    assert said == [TB.ASK_APPROVAL] and r["approval"] == "approved" and not r["is_error"]
    assert w.run[-1] == ("macbook__run_command", {"command": "open -a 'Google Chrome'", "approval_id": "a1"})

    async def deny(text):
        w.approvals["a2"]["status"] = "rejected"
    r = run(w.bridge().call("macbook__run_command", {"command": "open x"}, on_progress=deny))
    assert r["approval"] == "denied" and r["is_error"] and w.run[-1] == ("macbook__run_command", {"command": "open x"})  # never ran after a no


def test_approval_expires():
    w = World()
    TB.APPROVAL_WAIT_S, old = 0.05, TB.APPROVAL_WAIT_S
    try:
        r = run(w.bridge().call("macbook__run_command", {"command": "open y"}))
    finally:
        TB.APPROVAL_WAIT_S = old
    assert r["approval"] == "expired" and "5분" in r["content"] and len(w.run) == 1


def test_qwen_never_executes_without_an_approval_and_never_delegates():
    w = World()

    async def approve(text):
        aid = next(k for k in w.approvals if k.startswith("q"))
        w.approvals[aid]["status"] = "approved"
    r = run(w.bridge().call("macbook__run_command", {"command": "ls ~", "cwd": "/"}, caller="qwen", on_progress=approve))
    q = next(v for k, v in w.approvals.items() if k.startswith("q"))
    assert q["command"] == "ls ~" and q["tool"] == "run_command" and r["approval"] == "approved"
    assert w.run == [("macbook__run_command", {"command": "ls ~", "cwd": "/", "approval_id": "q1"})]  # only after OK
    assert run(w.bridge().call("macbook__list_dir", {"path": "~"}, caller="qwen"))["content"].startswith("ran")
    assert "create_task" in run(w.bridge().call("macbook__delegate", {"task": "x"}, caller="qwen"))["content"]


def test_call_user_now_later_in_call_and_cap():
    w = World()
    assert run(w.bridge().call("call_user", {"reason": "회의 알림"}, "luna"))["content"] == "지금 전화를 걸게요."
    assert w.calls == [("user_request", "말씀하신 대로 전화드렸어요. 회의 알림", "luna")]
    r = run(w.bridge().call("call_user", {"when": "2026-11-22T10:00:00+09:00", "reason": "기상"}))
    assert r["content"] == "내일 오전 10시에 전화드릴게요." and w.schedule[0]["kind"] == "reminder"
    w.in_call = True
    assert run(w.bridge().call("call_user", {"reason": "x"}))["content"].startswith("이미 통화 중이에요")
    assert len(w.calls) == 1  # refused while a call is up
    w.in_call = False
    log = next(v for k, v in w.kv.items() if "user-calls-" in k)
    assert len(log) == 2 and log[1]["scheduled_for"] == "2026-11-22T10:00:00+09:00"  # both logged
    log += [{"at": 0}] * (TB.CALL_CAP - 2)
    assert "다 걸었어요" in run(w.bridge().call("call_user", {"reason": "x"}))["content"] and len(w.calls) == 1
    assert run(w.bridge().call("call_user", {}))["is_error"]  # no reason


def test_create_task_notify_defaults():
    w = World()
    assert "끝나면 전화드릴게요" in run(w.bridge().call("create_task", {"goal": "정리"}, "max", caller="qwen"))["content"]
    run(w.bridge().call("create_task", {"goal": "정리"}, caller="api"))
    run(w.bridge().call("create_task", {"goal": "정리", "notify": "none"}, caller="qwen"))
    assert [t["notify"] for t in w.tasks] == ["call", "push", "none"] and w.tasks[0]["persona"] == "max"


def test_mcp_server_lists_and_calls_assistant_tools():
    w = World()
    b = w.bridge()
    init = run(TB.mcp_handle(b, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))
    assert init["result"]["capabilities"] == {"tools": {}}
    listed = run(TB.mcp_handle(b, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))["result"]["tools"]
    assert [t["name"] for t in listed] == ["call_user", "create_task"] and "inputSchema" in listed[0]
    out = run(TB.mcp_handle(b, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                "params": {"name": "create_task", "arguments": {"goal": "정리해서 전화해 줘"}}}))
    assert out["result"]["isError"] is False and w.tasks[0]["notify"] == "call"  # claude -p in a call: call back
    bad = run(TB.mcp_handle(b, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                "params": {"name": "macbook__run_command", "arguments": {}}}))
    assert bad["error"]["code"] == -32602  # only the assistant's own tools here
    assert run(TB.mcp_handle(b, {"jsonrpc": "2.0", "method": "notifications/initialized"})) is None


def fake_openai(script):
    """An OpenAI-compatible endpoint answering `script` in order: each a list of (tool name, args) or a text."""
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        step = script[len(seen) - 1]
        msg = {"role": "assistant", "content": step} if isinstance(step, str) else {
            "role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                for i, (n, a) in enumerate(step)]}
        return httpx.Response(200, json={"choices": [{"message": msg}],
                                         "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})
    return seen, httpx.MockTransport(handler)


def collect(gen):
    async def go():
        return [e async for e in gen]
    return asyncio.run(go())


def test_agent_loop_openai_backend_runs_tools_and_caps(monkeypatch):
    w = World()
    seen, transport = fake_openai([[("infra_health", {})], [("call_user", {"reason": "끝났어요"})], "albireo 디스크는 15%예요."])
    real = httpx.AsyncClient
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))

    async def never():
        return False
    cfg = {"url": "http://qwen/v1/chat/completions", "model": "q", "prices": [1.0, 2.0]}
    evs = collect(agent_loop.run("qwen", "S", "albireo 디스크 확인하고 전화해 줘", w.bridge(), cfg, 2.0, 30, never, "luna"))
    assert evs[-1]["type"] == "done" and evs[-1]["result"] == "albireo 디스크는 15%예요." and evs[-1]["steps"] == 3
    assert abs(evs[-1]["cost_usd"] - 3 * (1000 * 1.0 + 100 * 2.0) / 1e6) < 1e-12
    assert [e["tool"] for e in evs if e.get("phase") == "tool_start"] == ["infra_health", "call_user"]
    assert w.calls[0][2] == "luna" and seen[1]["messages"][-1]["role"] == "tool"
    names = {t["function"]["name"] for t in seen[0]["tools"]}
    assert "macbook__delegate" not in names and "call_user" in names  # qwen's tool set

    seen, transport = fake_openai([[("infra_health", {})]] * 50)
    evs = collect(agent_loop.run("nim", "S", "loop", w.bridge(), cfg | {"key": "k"}, 0.003, 30, never))
    assert evs[-1]["type"] == "failed" and evs[-1]["kind"] == "budget"

    async def yes():
        return True
    seen, transport = fake_openai(["x"])
    assert collect(agent_loop.run("nim", "S", "x", w.bridge(), cfg, 2.0, 30, yes))[-1]["kind"] == "cancelled"


def test_agent_loop_claude_backend_uses_web_tools_and_caching(monkeypatch):
    w, seen = World(), []
    replies = [{"content": [{"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {}}],
                "stop_reason": "pause_turn", "usage": {"input_tokens": 10, "output_tokens": 5,
                                                       "server_tool_use": {"web_search_requests": 1}}},
               {"content": [{"type": "tool_use", "id": "t1", "name": "infra_health", "input": {}}],
                "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}},
               {"content": [{"type": "text", "text": "끝났어요."}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5}}]

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=replies[len(seen) - 1])
    real = httpx.AsyncClient
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def never():
        return False
    evs = collect(agent_loop.run("claude_api", "SYS", "goal", w.bridge(), {"api_key": "k", "model": "haiku"}, 2, 30, never))
    assert evs[-1]["type"] == "done" and evs[-1]["result"] == "끝났어요." and evs[-1]["steps"] == 2
    assert {"web_search", "web_fetch"} <= {t["name"] for t in seen[0]["tools"]}
    assert seen[0]["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert seen[2]["messages"][-1]["content"][0]["tool_use_id"] == "t1"
    assert evs[-1]["cost_usd"] >= agent_loop.WEB_SEARCH_USD  # the search is counted
