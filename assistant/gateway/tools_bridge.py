"""One tool executor for every model path: the Claude API, NIM, the local Qwen (over the gateway link) and `claude -p`
(as the gateway's own MCP server). Nothing should be possible only through Claude Code.

Tools:
  - device daemon tools (`<device>__<tool>`), proxied over MCP with the daemon's own permissions. A daemon answers
    pending_approval for commands that need the user's OK; the bridge then waits up to APPROVAL_WAIT_S for the app's
    decision (telling a caller in a call to say "앱에서 허용해 주세요") and retries with the approval id.
    Qwen ("qwen" caller) gets read tools freely, every run_command only after an approval the bridge asks for itself,
    and no delegate: multi-step work goes through create_task.
  - call_user: ring the user now (refused while a call is up) or at a time (a scheduled call). At most CALL_CAP a day.
  - create_task: background work (/v1/tasks), notifying by call, push or not at all when it ends or needs approval.
  - the read-only Cygnus tools (assistant/brain/cygnus.py through claude_api.cygnus_bridge).

The gateway wires it with its own functions (Bridge(...)), so this module holds the rules, not the plumbing.
"""
import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
APPROVAL_WAIT_S = 300
APPROVAL_POLL_S = 2
CALL_CAP = 20  # user-requested calls a day, apart from the autonomous-call limits
ASK_APPROVAL = "앱에서 허용해 주세요."
PENDING_RE = re.compile(r'"?status"?\s*[:=]\s*"?pending_approval"?.{0,80}?"?approval_id"?\s*[:=]\s*"?([\w-]{2,64})', re.S)
READ_TOOLS = {"list_dir", "read_file", "search", "browse", "job_status", "list_jobs"}

CALL_USER = {
    "name": "call_user",
    "description": ("사용자에게 전화를 건다. when이 없으면 지금 걸고(통화 중이면 거절), 있으면 그 시각(KST, ISO 8601)에 "
                    "예약 전화를 건다. reason은 전화를 받았을 때 첫마디로 말할 이유."),
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["reason"], "properties": {
        "when": {"type": ["string", "null"], "description": "예: 2026-11-22T10:00:00+09:00, 지금이면 null"},
        "reason": {"type": "string", "maxLength": 200},
        "kind": {"type": "string", "enum": ["reminder", "wake"], "description": "wake = 깨우는 전화"},
        "repeat": {"type": ["string", "null"], "description": "daily, weekdays, weekly:0..6(0=월), 한 번이면 null"},
        "persona_id": {"type": "string", "maxLength": 32}}},
}
SCHEDULE_LIST = {"name": "schedule_list", "description": "예약된 전화 목록(id 포함).",
                 "input_schema": {"type": "object", "additionalProperties": False, "properties": {}}}
SCHEDULE_CANCEL = {
    "name": "schedule_cancel",
    "description": "예약된 전화 하나를 취소한다. id, 시각(at), 또는 설명(description: 메모나 '내일 7시' 같은 말)으로 고른다. "
                   "여러 개가 맞으면 아무것도 취소하지 않고 어느 것인지 돌려준다. 아무것도 안 주면 가장 가까운 예약.",
    "input_schema": {"type": "object", "additionalProperties": False, "properties": {
        "id": {"type": "string", "maxLength": 40}, "at": {"type": "string", "description": "ISO 8601 KST"},
        "description": {"type": "string", "maxLength": 100}}}}
TASK_STATUS = {"name": "task_status", "description": "백그라운드 작업 상태. id가 없으면 최근 3개.",
               "input_schema": {"type": "object", "additionalProperties": False, "properties": {
                   "id": {"type": "string", "pattern": "^[0-9a-f]{12}$"}}}}
APPROVAL_STATUS = {"name": "approval_status", "description": "기기 명령 승인 상태. id가 없으면 기다리는 것들. 승인은 사용자가 앱에서만 한다.",
                   "input_schema": {"type": "object", "additionalProperties": False, "properties": {
                       "id": {"type": "string", "maxLength": 64}}}}
OWN_TOOLS = None  # set below: the assistant's own tools, in the order models see them
CREATE_TASK = {
    "name": "create_task",
    "description": ("여러 단계가 필요한 일을 백그라운드 작업으로 맡긴다(정리, 조사, 기기에서 하는 작업). 끝나거나 실패하거나 승인이 "
                    "필요하면 notify대로 알린다: call(전화), push(알림), none."),
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["goal"], "properties": {
        "goal": {"type": "string", "maxLength": 2000}, "device": {"type": "string", "maxLength": 32},
        "notify": {"type": "string", "enum": ["call", "push", "none"]}}},
}


class Bridge:
    """`deps` (all async unless noted): fresh_devices(), device_tools(devices) -> (defs, where),
    run_tool(block, where) -> tool_result dict, place_call(reason, opening, persona_id) -> dict,
    schedule_create(body, created_by) -> (item, say), task_create(body, source) -> task,
    state(method, path, body=None, **params), in_call() (sync), cygnus() (sync) -> (defs, run|None)."""

    def __init__(self, **deps):
        self.d = deps

    # ---------- listing ----------
    async def tools(self, caller="api"):
        """Messages-API definitions of everything this caller may use, and where device tools run."""
        dev_defs, where = await self.d["device_tools"](await self.d["fresh_devices"]())
        if caller == "qwen":
            dev_defs = [t for t in dev_defs if where[t["name"]][1] != "delegate"]
        cyg_defs, _ = self.d["cygnus"]()
        g_defs, _ = self.d.get("google", lambda: ([], None))()
        return [*dev_defs, *OWN_TOOLS, *cyg_defs, *g_defs], where

    # ---------- one call ----------
    async def call(self, name, args, persona_id=None, caller="api", on_progress=None, where=None):
        """{"content": str, "is_error": bool, "waited_s": seconds spent waiting for an approval}."""
        args = args if isinstance(args, dict) else {}
        try:
            if name == "call_user":
                return ok(await self.call_user(args, persona_id))
            if name == "create_task":
                return ok(await self.create_task(args, persona_id, caller))
            if name == "schedule_list":
                return ok(await self.d["schedule_list_text"]())
            if name == "schedule_cancel":
                return ok(await self.schedule_cancel(args))
            if name == "task_status":
                return ok(await self.task_status(args))
            if name == "approval_status":
                return ok(await self.approval_status(args))
            for source in ("cygnus", "google"):  # read-only sources with their own allowlist and checks
                defs, run = self.d.get(source, lambda: ([], None))()
                if run and name in {t["name"] for t in defs}:
                    return ok(await run(name, args))
            if where is None:
                _, where = await self.tools(caller)
            if name not in where:
                return err(f"없는 도구예요: {str(name)[:40]}")
            return await self.device(name, args, where, caller, on_progress)
        except Exception as e:  # a failed tool is an answer for the model, never a crash
            return err(f"도구가 실패했어요: {type(e).__name__}")

    async def device(self, name, args, where, caller, on_progress):
        url, tool = where[name]
        if caller == "qwen" and tool == "delegate":
            return err("여러 단계 작업은 create_task로 맡겨 주세요.")
        if caller == "qwen" and tool not in READ_TOOLS and not args.get("approval_id"):
            # the local model never executes on its own: ask the user first, every time
            aid = (await self.d["state"]("POST", "/v1/state/approvals", {
                "device": name.split("__")[0], "tool": tool, "command": str(args.get("command", ""))[:500],
                "cwd": str(args.get("cwd", ""))[:300]}))["approval_id"]
            return await self.after_approval(name, args, aid, where, on_progress)
        res = await self.d["run_tool"]({"id": "bridge", "name": name, "input": args}, where)
        text = str(res.get("content", ""))
        if m := PENDING_RE.search(text.replace('\\"', '"')):
            return await self.after_approval(name, args, m.group(1), where, on_progress)
        return {"content": text, "is_error": bool(res.get("is_error")), "waited_s": 0}

    async def after_approval(self, name, args, aid, where, on_progress):
        """Wait for the app's decision on `aid`, then run the same call with the approval id."""
        if on_progress:
            await on_progress(ASK_APPROVAL)
        t0 = time.monotonic()
        while time.monotonic() - t0 < APPROVAL_WAIT_S:
            a = await self.d["state"]("GET", f"/v1/state/approvals/{aid}") or {}
            if a.get("status") == "approved":
                res = await self.d["run_tool"]({"id": "bridge", "name": name, "input": {**args, "approval_id": aid}},
                                               where)
                return {"content": str(res.get("content", "")), "is_error": bool(res.get("is_error")),
                        "waited_s": time.monotonic() - t0, "approval": "approved"}
            if a.get("status") == "rejected":
                return {"content": "사용자가 그 실행을 거절했어요.", "is_error": True,
                        "waited_s": time.monotonic() - t0, "approval": "denied"}
            await asyncio.sleep(APPROVAL_POLL_S)
        return {"content": "5분 안에 허용되지 않아서 실행하지 않았어요.", "is_error": True,
                "waited_s": time.monotonic() - t0, "approval": "expired"}

    # ---------- the assistant's own tools ----------
    async def call_user(self, args, persona_id):
        reason = str(args.get("reason") or "").strip()[:200]
        if not reason:
            raise ValueError("reason")
        pid = args.get("persona_id") if isinstance(args.get("persona_id"), str) else persona_id
        when = args.get("when")
        kind = args.get("kind") if args.get("kind") in ("reminder", "wake") else "reminder"
        if when:
            item, say = await self.d["schedule_create"]({"at": when, "kind": kind, "note": "" if kind == "wake" else reason,
                                                         "repeat": args.get("repeat") or None,
                                                         **({"persona_id": pid} if pid else {})}, "voice")
            await self.log_call(reason, item.get("at"), pid)
            return say
        if self.d["in_call"]():
            return "이미 통화 중이에요. 지금은 전화를 걸 수 없어요."
        today = datetime.now(KST).strftime("%Y-%m-%d")
        made = await self.d["state"]("GET", f"/v1/state/kv/user-calls-{today}") or []
        if len(made) >= CALL_CAP:
            return f"오늘은 요청 전화를 {CALL_CAP}번 다 걸었어요."
        await self.d["place_call"]("user_request", f"말씀하신 대로 전화드렸어요. {reason}", pid)
        await self.log_call(reason, None, pid)
        return "지금 전화를 걸게요."

    async def log_call(self, reason, at, pid):
        """Every user-requested call: who, when (None = now), why. The same list counts the daily cap."""
        today = datetime.now(KST).strftime("%Y-%m-%d")
        rows = await self.d["state"]("GET", f"/v1/state/kv/user-calls-{today}") or []
        rows.append({"at": time.time(), "scheduled_for": at, "reason": reason, "persona": pid})
        await self.d["state"]("PUT", f"/v1/state/kv/user-calls-{today}", rows[-100:])

    async def schedule_cancel(self, args):
        """By id or time through the gateway's schedule_cancel; by description only when exactly one item matches."""
        if args.get("id") or args.get("at"):
            at = datetime.fromisoformat(args["at"]).timestamp() if args.get("at") else None
            done = await self.d["schedule_cancel"](args.get("id"), at)
            return done[1] if done else "그런 예약이 없어요."
        items = await self.d["schedule_items"]()
        desc = str(args.get("description") or "").strip()
        if desc:
            words = [w for w in re.split(r"\s+", desc) if len(w) > 1]
            hits = [i for i in items if all(w in (i["note"] + " " + i["spoken"]) for w in words)]
            if len(hits) != 1:
                return ("그런 예약이 없어요." if not hits else
                        "어느 것인지 골라 주세요: " + "; ".join(f"{i['spoken']}(id {i['id']})" for i in hits[:5]))
            done = await self.d["schedule_cancel"](hits[0]["id"], None)
            return done[1] if done else "그런 예약이 없어요."
        done = await self.d["schedule_cancel"](None, None)  # the soonest, as the voice path always did
        return done[1] if done else "취소할 예약이 없어요."

    async def task_status(self, args):
        ts = [t for t in [await self.d["task_get"](args["id"])] if t] if args.get("id") else await self.d["task_recent"](3)
        if not ts:
            return "그런 작업이 없어요." if args.get("id") else "맡긴 작업이 없어요."
        return "\n".join(f"- {t['id']} {t['status']}: {t['goal'][:60]}" + (f" → {str(t.get('result') or t.get('error'))[:200]}"
                                                                       if t.get("result") or t.get("error") else "")
                         for t in ts)

    async def approval_status(self, args):
        if args.get("id"):
            a = await self.d["state"]("GET", f"/v1/state/approvals/{args['id']}")
            return f"{a.get('device', '')}의 '{str(a.get('command', ''))[:60]}': {a.get('status')}" if a else "그런 승인 요청이 없어요."
        rows = await self.d["state"]("GET", "/v1/state/approvals") or []
        pending = [a for a in rows if a.get("status") == "pending"]
        return "\n".join(f"- {a['id']} {a.get('device', '')}: {str(a.get('command', ''))[:60]}" for a in pending[:5]) \
            or "기다리는 승인 요청이 없어요."

    async def create_task(self, args, persona_id, caller):
        goal = str(args.get("goal") or "").strip()
        notify = args.get("notify") or ("call" if caller in ("voice", "qwen", "claude_code") else "push")
        body = {"goal": goal, "notify": notify, **({"device": args["device"]} if args.get("device") else {}),
                **({"persona": persona_id} if persona_id else {})}
        t = await self.d["task_create"](body, "tool")
        how = {"call": "끝나면 전화드릴게요.", "push": "끝나면 알림으로 알려 드릴게요.", "none": ""}[notify]
        return f"작업으로 맡겼어요. {how}".strip() + f" (작업 {t['id']})"


def ok(text):
    return {"content": str(text), "is_error": False, "waited_s": 0}


def err(text):
    return {"content": str(text), "is_error": True, "waited_s": 0}


# ---------- the same tools as an MCP server, for `claude -p` (POST /v1/mcp, stateless JSON-RPC) ----------
OWN_TOOLS = (CALL_USER, SCHEDULE_LIST, SCHEDULE_CANCEL, CREATE_TASK, TASK_STATUS, APPROVAL_STATUS)
MCP_TOOLS = OWN_TOOLS  # claude -p already has the daemons, Cygnus and the claude.ai connectors as its own MCP servers


async def mcp_handle(bridge, msg, persona_id=None):
    """One JSON-RPC message; None for notifications."""
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "azel", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
                            for t in MCP_TOOLS]}
    elif method == "tools/call":
        p = msg.get("params") or {}
        if p.get("name") not in {t["name"] for t in MCP_TOOLS}:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
        r = await bridge.call(p["name"], p.get("arguments") or {}, persona_id, caller="claude_code")
        result = {"content": [{"type": "text", "text": r["content"]}], "isError": r["is_error"]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def openai_tools(defs):
    """Messages-API definitions as OpenAI function tools (NIM, Qwen)."""
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}} for t in defs]


def args_of(raw):
    """OpenAI tool-call arguments (a JSON string) as a dict; {} when malformed."""
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}
