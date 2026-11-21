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
        return {"at": body.get("at")}, "내일 오전 10시에 전화드릴게요."

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
                         schedule_list_text=self.schedule_text, schedule_items=self.schedule_items,
                         schedule_cancel=self.schedule_cancel, task_get=self.task_get, task_recent=self.task_recent,
                         cygnus=lambda: ([{"name": "infra_health", "description": "x", "input_schema": {}}],
                                         self.cygnus_run))

    async def schedule_items(self):
        return [{"id": f"s{i}", "spoken": f"내일 {7 + i}시", "note": b.get("note", ""), **b}
                for i, b in enumerate(self.schedule) if not b.get("cancelled")]

    async def schedule_text(self):
        return f"예약된 전화가 {len(await self.schedule_items())}개 있어요."

    async def schedule_cancel(self, sid, at):
        for i in await self.schedule_items():
            if sid in (None, i["id"]):
                self.schedule[int(i["id"][1:])]["cancelled"] = True
                return i, f"{i['spoken']} 예약을 취소했어요."
        return None

    async def task_get(self, tid):
        return {"id": tid, "status": "done", "goal": "정리", "result": "끝"} if tid == "a00000000001" else None

    async def task_recent(self, n):
        return [{"id": "a00000000001", "status": "running", "goal": "정리"}][:n]

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
    r = run(w.bridge().call("call_user", {"when": "2026-11-22T10:00:00+09:00", "reason": "기상"}, caller="app"))
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
    assert [t["name"] for t in listed] == [t["name"] for t in TB.ALL_OWN] and "inputSchema" in listed[0]
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


def test_schedule_task_and_approval_tools():
    w = World()
    b = w.bridge()
    for note, when in (("약 먹기", "2026-11-22T08:00:00+09:00"), ("회의 준비", "2026-11-22T09:00:00+09:00")):
        run(b.call("call_user", {"when": when, "reason": note}, caller="app"))
    assert [t["name"] for t in run(b.tools("api"))[0]][3:11] == ["call_user", "schedule_list", "schedule_cancel",
                                                                  "schedule_update", "create_task", "task_status",
                                                                  "task_cancel", "approval_status"]
    assert run(b.call("schedule_list", {}))["content"] == "예약된 전화가 2개 있어요."
    assert run(b.call("schedule_cancel", {"description": "준비"}))["content"] == "내일 8시 예약을 취소했어요."
    assert "골라 주세요" in run(b.call("call_user", {"when": "2026-11-22T10:00:00+09:00", "reason": "약 먹기 두번째"},
                                     caller="app")) \
        ["content"] + run(b.call("schedule_cancel", {"description": "약"}))["content"]  # two match: none cancelled
    assert run(b.call("schedule_cancel", {"description": "없는것"}))["content"] == "그런 예약이 없어요."
    assert run(b.call("call_user", {"when": "2026-11-23T07:00:00+09:00", "reason": "x", "kind": "wake",
                                    "repeat": "weekdays"}, caller="app"))["is_error"] is False
    assert w.schedule[-1]["kind"] == "wake" and w.schedule[-1]["repeat"] == "weekdays" and w.schedule[-1]["note"] == ""
    assert "running" in run(b.call("task_status", {}))["content"]
    assert run(b.call("task_status", {"id": "a00000000001"}))["content"].endswith("→ 끝")
    w.approvals["a9"] = {"id": "a9", "status": "pending", "device": "macbook", "command": "open x"}
    w.kv["/v1/state/approvals"] = None
    assert "그런 승인 요청이 없어요." == run(b.call("approval_status", {"id": "zz"}))["content"]


class Rules(World):
    """A schedule with a one-off and a recurring item, and the gateway's update and occurrence-cancel."""

    def __init__(self):
        super().__init__()
        self.items = [
            {"id": "one", "at": "2026-11-23T22:00:00+09:00", "spoken": "오늘 오후 10시", "note": "약 먹기", "purpose": "약 먹기"},
            {"id": "rule", "at": "2026-11-23T10:00:00+09:00", "spoken": "평일 오전 10시", "cron": "0 10-18/2 * * 1-5",
             "rule_text": "평일 오전 10시부터 오후 6시까지 2시간마다", "note": "", "purpose": "스트레칭"},
            {"id": "old", "at": "2026-11-24T07:00:00+09:00", "spoken": "매일 오전 7시", "repeat": "daily",
             "note": "", "purpose": ""},
        ]
        self.updates, self.cancels = [], []

    async def schedule_items(self):
        return self.items

    async def schedule_update(self, sid, changes, occurrence):
        self.updates.append((sid, changes, occurrence))
        return {}, "바꿨어요."

    async def schedule_cancel(self, sid, at, occurrence=None):
        self.cancels.append((sid, at, occurrence))
        return {}, "취소했어요."

    def bridge(self):
        b = super().bridge()
        b.d.update(schedule_items=self.schedule_items, schedule_update=self.schedule_update,
                   schedule_cancel=self.schedule_cancel)
        return b


def test_call_user_rules_need_a_yes_unless_simple():
    w = World()
    b = w.bridge()
    assert not run(b.call("call_user", {"reason": "운동", "repeat_cron": "0 21 * * *"}))["is_error"]
    assert w.schedule[-1]["cron"] == "0 21 * * *" and w.schedule[-1]["notify"] == "call"
    r = run(b.call("call_user", {"reason": "스트레칭", "repeat_cron": "0 10-18/2 * * 1-5", "notify": "push"}))
    assert r["is_error"] and "평일 오전 10시부터 오후 6시까지 2시간마다 알림 드릴게요. 맞죠?" in r["content"] and len(w.schedule) == 1
    run(b.call("call_user", {"reason": "스트레칭", "repeat_cron": "0 10-18/2 * * 1-5", "notify": "push", "confirmed": True}))
    assert w.schedule[-1]["notify"] == "push" and w.schedule[-1]["request"] == "스트레칭"
    r = run(b.call("call_user", {"reason": "x", "repeat_cron": "0 7 * * *", "end_date": "2026-12-31"}))
    assert r["is_error"] and "12월 31일까지" in r["content"]  # a date range is read back too
    assert run(b.call("call_user", {"reason": "x", "repeat_cron": "every day"}))["content"] == "반복 규칙을 이해하지 못했어요."

    class Refusal(Exception):
        status_code, detail = 400, "하루 전화가 20번을 넘게 돼요."

    async def refuse(body, by):
        raise Refusal()
    b.d["schedule_create"] = refuse
    r = run(b.call("call_user", {"reason": "x", "repeat_cron": "0 7 * * *"}))
    assert r == {"content": "하루 전화가 20번을 넘게 돼요.", "is_error": True, "waited_s": 0}


def test_schedule_update_picks_one_and_asks_otherwise():
    w = Rules()
    b = w.bridge()
    assert run(b.call("schedule_update", {"description": "약", "when": "2026-11-23T18:00:00+09:00"},
                      caller="app"))["content"] == "바꿨어요."
    assert w.updates[-1] == ("one", {"when": "2026-11-23T18:00:00+09:00"}, None)
    assert run(b.call("schedule_update", {"when": "2026-11-23T18:00:00+09:00"}, caller="app"))["content"].startswith(
        "어느 것인지")
    r = run(b.call("schedule_update", {"description": "약", "when": "2026-11-23T18:00:00+09:00"}))  # a model's own time
    assert r["is_error"] and "when_text" in r["content"]
    assert run(b.call("schedule_update", {"id": "one"}))["content"] == "무엇을 바꿀지 말씀해 주세요."
    assert run(b.call("schedule_update", {"description": "없는것", "purpose": "x"}))["content"] == "그런 예약이 없어요."
    r = run(b.call("schedule_update", {"id": "one", "when": "0 8 * * *"}, caller="app"))
    assert r["is_error"] and "repeat_cron" in r["content"]  # a cron in `when` is refused, not passed on
    assert len(w.updates) == 1  # nothing else changed


def test_schedule_update_on_rules_needs_a_scope():
    w = Rules()
    b = w.bridge()
    for sid in ("rule", "old"):  # a cron rule, and a legacy daily repeat
        assert "scope" in run(b.call("schedule_update", {"id": sid, "purpose": "물 마시기"}))["content"]
    assert w.updates == []
    run(b.call("schedule_update", {"id": "rule", "scope": "rule", "purpose": "물 마시기"}))
    assert w.updates[-1] == ("rule", {"purpose": "물 마시기"}, None)
    # "오늘 12시 건만 1시로": that fire, picked by its time
    run(b.call("schedule_update", {"at": "2026-11-23T12:00:00+09:00", "scope": "occurrence",
                                   "when": "2026-11-23T13:00:00+09:00"}, caller="app"))
    assert w.updates[-1] == ("rule", {"when": "2026-11-23T13:00:00+09:00"}, "2026-11-23T12:00:00+09:00")
    to = "2026-11-23T15:00:00+09:00"
    assert "at으로" in run(b.call("schedule_update", {"id": "rule", "scope": "occurrence", "when": to},
                                  caller="app"))["content"]
    run(b.call("schedule_update", {"id": "rule", "at": "2026-11-23T16:00:00+09:00", "scope": "occurrence", "when": to},
               caller="app"))
    assert w.updates[-1] == ("rule", {"when": to}, "2026-11-23T16:00:00+09:00")  # id picks, at names the fire
    assert "한 번만" in run(b.call("schedule_update", {"id": "rule", "scope": "occurrence",
                                                     "repeat_cron": "0 9 * * *"}))["content"]
    r = run(b.call("schedule_update", {"id": "rule", "scope": "rule", "repeat_cron": "0 9-17/2 * * 1-5"}))
    assert r["is_error"] and "맞죠?" in r["content"]  # a new non-trivial rule is read back first
    run(b.call("schedule_update", {"id": "rule", "scope": "rule", "repeat_cron": "0 9  * * *", "end_date": None}))
    assert w.updates[-1] == ("rule", {"repeat_cron": "0 9 * * *", "end_date": None}, None)


def test_schedule_cancel_one_occurrence():
    w = Rules()
    b = w.bridge()
    run(b.call("schedule_cancel", {"id": "rule", "scope": "occurrence"}))
    assert w.cancels[-1] == ("rule", None, "2026-11-23T10:00:00+09:00")  # the next fire
    run(b.call("schedule_cancel", {"at": "2026-11-23T14:00:00+09:00", "scope": "occurrence"}))
    assert w.cancels[-1] == ("rule", None, "2026-11-23T14:00:00+09:00")
    run(b.call("schedule_cancel", {"id": "one", "scope": "occurrence"}))
    assert w.cancels[-1] == ("one", None, None)  # a one-off: cancelled outright
    run(b.call("schedule_cancel", {"id": "rule"}))
    assert w.cancels[-1] == ("rule", None, None)  # default: the whole rule


def test_mcp_scopes():
    w = World()
    b = w.bridge()

    def names(scope):
        r = run(TB.mcp_handle(b, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, scope=scope))
        return [t["name"] for t in r["result"]["tools"]]
    assert names("full") == [t["name"] for t in TB.ALL_OWN]
    assert names("external") == ["call_user", "schedule_list", "schedule_cancel", "schedule_update", "create_task",
                                 "task_status", "task_cancel", "schedule_create"]
    for tool in ("approval_status", "macbook__list_dir", "infra_health", "nope"):
        r = run(TB.mcp_handle(b, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": tool, "arguments": {}}}, scope="external"))
        assert r["error"] == {"code": -32602, "message": "unknown tool"}, tool  # out of scope = nonexistent
    assert "result" in run(TB.mcp_handle(b, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                             "params": {"name": "approval_status", "arguments": {}}}, scope="full"))
    run(TB.mcp_handle(b, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                          "params": {"name": "create_task", "arguments": {"goal": "정리"}}}, scope="external"))
    assert w.tasks[-1]["notify"] == "push"  # the user is at a terminal, not in a call


def test_when_text_is_resolved_by_the_gateway_not_the_model():
    from datetime import datetime as dt
    w = Rules()
    b = w.bridge()
    b.d["now"] = lambda: dt(2026, 11, 23, 9, 0, tzinfo=TB.KST)
    # the model's own (wrong) `when` loses to the words it was told
    run(b.call("schedule_update", {"id": "one", "when_text": "6시로", "when": "2026-11-24T06:00:00+09:00"}))
    assert w.updates[-1] == ("one", {"when": "2026-11-23T18:00:00+09:00"}, None)  # 오후 10시 → 오후 6시
    r = run(b.call("schedule_update", {"id": "rule", "scope": "occurrence", "at": "2026-11-23T14:00:00+09:00",
                                       "when_text": "3시로"}))
    assert w.updates[-1] == ("rule", {"when": "2026-11-23T15:00:00+09:00"}, "2026-11-23T14:00:00+09:00")
    b.d["now"] = lambda: dt(2026, 11, 23, 21, 0, tzinfo=TB.KST)
    r = run(b.call("schedule_update", {"id": "one", "when_text": "7시로"}))
    assert r["is_error"] and "이미 지났어요" in r["content"] and len(w.updates) == 2
    b.d["now"] = lambda: dt(2026, 11, 23, 9, 0, tzinfo=TB.KST)
    r = run(b.call("call_user", {"reason": "회의", "when_text": "내일 7시"}))
    assert r["is_error"] and "아침 7시요, 저녁 7시요?" in r["content"] and not w.schedule
    run(b.call("call_user", {"reason": "회의", "when_text": "7시", "when": "2026-11-24T07:00:00+09:00"}))
    assert w.schedule[-1]["at"] == "2026-11-23T19:00:00+09:00"
    assert "이미 예약했어요" in run(b.call("call_user", {"reason": "회의", "when_text": "7시"}))["content"]
    assert len(w.schedule) == 1  # the same call twice in a row creates one item
    b.d["now"] = lambda: dt(2026, 11, 23, 0, 30, tzinfo=TB.KST)
    r = run(b.call("call_user", {"reason": "과제", "when_text": "내일 오후 1시"}))
    assert r["content"].startswith("오늘(11월 23일) 오후 1시로 잡았어요.")
    assert w.schedule[-1]["interpretation"] == "새벽 '내일' → 오늘로 해석"


def test_repeat_text_becomes_a_checked_rule_with_a_resend():
    w = World()
    b = w.bridge()

    async def qwen(system, user):
        return {"cron": "0 10-18/2 * * 1-5", "kind": "reminder", "purpose": "스트레칭"}
    b.d["llm"] = qwen
    r = run(b.run("call_user", {"reason": "스트레칭", "repeat_text": "평일 10시부터 6시까지 2시간마다"}, caller="qwen"))
    assert r["needs"]["kind"] == "confirm" and r["needs"]["args"]["repeat_cron"] == "0 10-18/2 * * 1-5" and not w.schedule
    r = run(b.run("call_user", r["needs"]["args"], caller="qwen"))  # the user said yes: resend as given
    assert r["ok"] and w.schedule[-1]["cron"] == "0 10-18/2 * * 1-5"

    async def broken(system, user):
        return {"cron": "every day"}
    b.d["llm"] = broken
    r = run(b.run("call_user", {"reason": "x", "repeat_text": "언젠가 매일"}, caller="qwen"))
    assert r["error"]["code"] == "invalid" and "이해하지 못했어요" in r["spoken"]
