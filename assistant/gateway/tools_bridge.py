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

import cron as C
import late_night as L

KST = timezone(timedelta(hours=9))
APPROVAL_WAIT_S = 300
APPROVAL_POLL_S = 2
CALL_CAP = 20  # user-requested calls a day, apart from the autonomous-call limits
ASK_APPROVAL = "앱에서 허용해 주세요."
PENDING_RE = re.compile(r'"?status"?\s*[:=]\s*"?pending_approval"?.{0,80}?"?approval_id"?\s*[:=]\s*"?([\w-]{2,64})', re.S)
READ_TOOLS = {"list_dir", "read_file", "search", "browse", "job_status", "list_jobs"}

CALL_USER = {
    "name": "call_user",
    "description": ("사용자에게 전화를 건다. 시각이 없으면 지금 걸고(통화 중이면 거절), 있으면 그 시각에 예약 전화를 건다. "
                    "시각은 사용자가 말한 그대로 when_text에 넣어(예: '7시', '내일 오후 1시'). '7시'처럼 애매해 보여도 먼저 묻지 "
                    "말고 넘겨: 도구가 정하거나, 물어볼 말을 돌려줘. reason은 전화의 목적."),
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["reason"], "properties": {
        "when": {"type": ["string", "null"], "description": "정확한 ISO 시각을 이미 알 때만(앱 등). 지금이면 null"},
        "when_text": {"type": "string", "maxLength": 60, "description": (
            "사용자가 말한 시각 표현을 한 글자도 바꾸지 말고 그대로(예: '6시', '11시 반', '내일 오후 1시', '30분 뒤'). 오전·오후를 "
            "덧붙이거나 계산하지 마: 게이트웨이가 정하고, 애매하면 물어볼 말을 돌려줘")},
        "reason": {"type": "string", "maxLength": 200},
        "kind": {"type": "string", "enum": ["reminder", "wake"], "description": "wake = 깨우는 전화"},
        "repeat": {"type": ["string", "null"], "description": "옛 방식: daily, weekdays, weekly:0..6(0=월). repeat_cron을 쓸 것"},
        "repeat_cron": {"type": ["string", "null"], "description": (
            "반복이면 5칸 cron(KST, 분 시 일 월 요일, 요일 0=일요일). 예: 매일 7시 '0 7 * * *', 화목 2시 '0 14 * * 2,4', "
            "평일 10시~6시 2시간마다 '0 10-18/2 * * 1-5', 매월 1일 '0 9 1 * *'. 전화 사이 30분 이상, 하루 20번까지")},
        "repeat_text": {"type": "string", "maxLength": 200, "description": (
            "반복 요청을 사용자가 말한 그대로(예: '평일 10시부터 6시까지 2시간마다'). repeat_cron 대신 써도 돼요: "
            "게이트웨이가 규칙으로 바꾸고 읽어 줄 문장을 돌려줘요")},
        "start_date": {"type": ["string", "null"], "description": "반복 시작일 YYYY-MM-DD"},
        "end_date": {"type": ["string", "null"], "description": "반복 마지막 날 YYYY-MM-DD"},
        "notify": {"type": "string", "enum": ["call", "push"], "description": "'알림으로'면 push, 기본 call"},
        "confirmed": {"type": "boolean", "description": "단순한 매일·매주 규칙이 아니면, 사용자에게 읽어 주고 확인받은 뒤 true"},
        "persona_id": {"type": "string", "maxLength": 32}}},
}
PICK = {  # how schedule_cancel and schedule_update choose their item
    "id": {"type": "string", "maxLength": 40}, "at": {"type": "string", "description": "ISO 8601 KST (반복이면 그 회차의 시각)"},
    "description": {"type": "string", "maxLength": 100, "description": "메모나 '내일 7시' 같은 말"}}
SCHEDULE_LIST = {"name": "schedule_list", "description": "예약된 전화 목록(id, 반복 규칙, 다음 시각 포함).",
                 "input_schema": {"type": "object", "additionalProperties": False, "properties": {
                     "include": {"type": "string", "enum": ["all"], "description": "all이면 끝난 것과 취소된 것도"},
                     "persona_id": {"type": "string", "maxLength": 32}}}}
SCHEDULE_CANCEL = {
    "name": "schedule_cancel",
    "description": "예약된 전화 하나를 취소한다. id, 시각(at), 또는 설명(description: 메모나 '내일 7시' 같은 말)으로 고른다. "
                   "여러 개가 맞으면 아무것도 취소하지 않고 어느 것인지 돌려준다. 아무것도 안 주면 예약이 하나일 때만 그것. "
                   "반복 예약은 scope가 필요하다: occurrence(at의 그 회차, 없으면 다음 회차 하나만) 또는 rule(반복 전체). "
                   "없으면 사용자에게 물어본다.",
    "input_schema": {"type": "object", "additionalProperties": False, "properties": {
        **PICK, "scope": {"type": "string", "enum": ["occurrence", "rule"]}}}}
SCHEDULE_UPDATE = {
    "name": "schedule_update",
    "description": ("예약된 전화 하나를 바꾼다(시각, 목적, 알림 방식, 반복 규칙, 기간). 고르는 법은 schedule_cancel과 같고, 여러 개가 "
                    "맞으면 아무것도 바꾸지 않고 어느 것인지 돌려준다. 반복 예약이면 scope가 필요해: 사용자가 '오늘', '내일'처럼 "
                    "특정한 날을 말했으면 occurrence(그 회차만, at에 그 회차의 원래 시각), 아니면 rule(반복 전체). 애매하면 먼저 "
                    "물어봐. '~만 빼줘', '이번만 건너뛰어'처럼 한 회차를 빼는 건 이 도구가 아니라 schedule_cancel(scope=occurrence, "
                    "at=그 회차 시각)이야. 한 회차를 빼려고 반복 규칙(repeat_cron)을 고치지 마."),
    "input_schema": {"type": "object", "additionalProperties": False, "properties": {
        **PICK, "scope": {"type": "string", "enum": ["occurrence", "rule"]},
        "when_text": {"type": "string", "maxLength": 60, "description": (
            "사용자가 말한 시각 표현을 한 글자도 바꾸지 말고 그대로(예: '6시', '11시 반', '내일 오후 1시', '30분 뒤'). 오전·오후를 "
            "덧붙이거나 계산하지 마: 게이트웨이가 정하고, 애매하면 물어볼 말을 돌려줘")},
        "when": {"type": "string", "description": (
            "정확한 ISO 시각을 이미 알 때만(앱 등). 반복 전체면 이 시각의 시·분이 새 규칙의 시각. cron은 repeat_cron에")},
        "purpose": {"type": "string", "maxLength": 60},
        "notify": {"type": "string", "enum": ["call", "push"], "description": (
            "사용자가 받는 방식을 바꾸라고 할 때만('전화 말고 알림으로' → push). '10시 알림'의 '알림'은 예약을 가리키는 말일 뿐, "
            "그때는 notify를 넣지 마")},
        "repeat_cron": {"type": "string", "description": "새 반복 규칙(5칸 cron)"},
        "start_date": {"type": ["string", "null"]}, "end_date": {"type": ["string", "null"]},
        "confirmed": {"type": "boolean", "description": ("단순한 매일·매주가 아닌 새 규칙을, 도구가 돌려준 읽어 주기 문장으로 "
                                                         "사용자에게 물어 '네'라는 답을 들은 뒤에만 true. 스스로 true로 하지 마")}}}}
TASK_STATUS = {"name": "task_status", "description": "백그라운드 작업 상태. id가 없으면 최근 것들(limit, 기본 3).",
               "input_schema": {"type": "object", "additionalProperties": False, "properties": {
                   "id": {"type": "string", "pattern": "^[0-9a-f]{12}$"},
                   "limit": {"type": "integer", "minimum": 1, "maximum": 100}}}}
TASK_CANCEL = {"name": "task_cancel", "description": "실행 중인 백그라운드 작업을 멈춘다.",
               "input_schema": {"type": "object", "additionalProperties": False, "required": ["id"], "properties": {
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
        "notify": {"type": "string", "enum": ["call", "push", "none"]},
        "backend": {"type": "string", "enum": ["claude_api", "claude_code", "nim", "qwen"],
                    "description": "보통은 비워 둬(설정의 기본값)"},
        "persona": {"type": "string", "maxLength": 32},
        "budget_usd": {"type": "number", "minimum": 0, "description": "보통은 비워 둬"},
        "max_minutes": {"type": "number", "minimum": 0, "description": "보통은 비워 둬"}}},
}


class Bridge:
    """`deps` (all async unless noted): fresh_devices(), device_tools(devices) -> (defs, where),
    run_tool(block, where) -> tool_result dict, place_call(reason, opening, persona_id) -> dict,
    schedule_create(body, created_by) -> (item, say), task_create(body, source) -> task,
    state(method, path, body=None, **params), in_call() (sync), cygnus() (sync) -> (defs, run|None)."""

    def __init__(self, **deps):
        self.d = deps
        self.recent = {}  # (at, cron) -> (monotonic, say): a model repeating the same call_user creates nothing

    # ---------- listing ----------
    async def tools(self, caller="api"):
        """Messages-API definitions of everything this caller may use, and where device tools run."""
        dev_defs, where = await self.d["device_tools"](await self.d["fresh_devices"]())
        if caller in ("qwen", "voice"):  # a voice turn of any model never delegates; tasks do
            dev_defs = [t for t in dev_defs if where[t["name"]][1] != "delegate"]
        cyg_defs, _ = self.d["cygnus"]()
        cyg_defs = [t for t in cyg_defs if t["name"] not in CYGNUS_WRAPPED]  # one name, one tool (the API needs it)
        g_defs, _ = self.d.get("google", lambda: ([], None))()
        return [*dev_defs, *OWN_TOOLS, *cyg_defs, *g_defs], where

    # ---------- one call ----------
    async def call(self, name, args, persona_id=None, caller="api", on_progress=None, where=None):
        """The model-facing shape of run(): {"content", "is_error", "waited_s"[, "approval"]}."""
        r = await self.run(name, args, persona_id, caller, on_progress=on_progress, where=where)
        out = {"content": for_model(r), "is_error": not r["ok"], "waited_s": r.get("_waited", 0)}
        return out | ({"approval": r["_approval"]} if r.get("_approval") else {})

    async def run(self, name, args, persona_id=None, caller="api", surface="tool", on_progress=None, where=None):
        """Every action, every surface: {"ok", "spoken", "data", "needs", "error"} (ACTIONS.md §2), audited once."""
        args = args if isinstance(args, dict) else {}
        try:
            if caller == "app" and name not in APP_ACTIONS:
                raise Fail("not_found", f"없는 기능이에요: {str(name)[:40]}")
            if name in OWN_SCHEMAS:
                check_args(OWN_SCHEMAS[name], args)
            out = await self.dispatch(name, args, persona_id, caller, on_progress, where)
            if isinstance(out, dict):  # a device tool's own result
                r = result(not out["is_error"], out["content"],
                           error={"code": "failed", "message": out["content"][:300]} if out["is_error"] else None)
                r.update({k: out[k] for k in ("waited_s", "approval") if k in out and out[k]})
                r["_waited"], r["_approval"] = out.get("waited_s", 0), out.get("approval")
            elif isinstance(out, tuple):
                r = result(True, out[0], data=out[1])
                if len(out) > 2:
                    r["_model"] = out[2]
            else:
                r = result(True, out)
        except Need as e:
            r = result(False, e.question, needs={"kind": e.kind, "question": e.question,
                                                 **({"options": e.options} if e.options else {}),
                                                 **({"args": e.resend} if e.resend else {})})
            r["_model"] = e.hint
        except Fail as e:
            r = result(False, e.message, error={"code": e.code, "message": e.message})
        except C.CronError as e:
            r = result(False, str(e), error={"code": "invalid", "message": str(e)})
        except Exception as e:  # a failed tool is an answer for the model, never a crash
            code = HTTP_CODES.get(getattr(e, "status_code", None))
            msg = e.detail if code and isinstance(getattr(e, "detail", None), str) else f"도구가 실패했어요: {type(e).__name__}"
            r = result(False, msg, error={"code": code or "failed", "message": msg})
        await self.audit(name, args, caller, surface, persona_id, r)
        return r

    async def audit(self, name, args, caller, surface, persona_id, r):
        """One row per action in kv actions-<KST date>: never the inputs, only what and whom (ACTIONS.md §6)."""
        try:
            day = self.now().strftime("%Y-%m-%d")
            rows = await self.d["state"]("GET", f"/v1/state/kv/actions-{day}") or []
            target = next((str(args[k])[:40] for k in ("id",) if args.get(k)), None)
            rows.append({"at": time.time(), "action": str(name)[:60], "surface": surface, "caller": caller,
                         "persona": persona_id, "ok": r["ok"], "needs": (r["needs"] or {}).get("kind"),
                         "error": (r["error"] or {}).get("code"), "target": target})
            await self.d["state"]("PUT", f"/v1/state/kv/actions-{day}", rows[-500:])
        except Exception:  # noqa: BLE001  the audit never fails an action
            pass

    async def dispatch(self, name, args, persona_id, caller, on_progress, where):
        if name in ("call_user", "schedule_create", "schedule_update") and args.get("when") \
                and not args.get("when_text") and caller != "app":
            raise Fail("invalid", "시각은 직접 계산하지 말고, 사용자가 말한 표현 그대로 when_text로 주세요(예: '6시', '내일 오후 1시').")
        if name == "call_user":
            return await self.call_user(args, persona_id, caller)
        if name == "schedule_create":
            if not (args.get("when") or args.get("when_text") or args.get("repeat_cron")):
                raise Fail("invalid", "언제 걸지 알려 주세요.")
            return await self.call_user(args, persona_id, caller)
        if name == "create_task":
            return await self.create_task(args, persona_id, caller)
        if name == "schedule_list":
            items = await self.d["schedule_items"](**{k: args[k] for k in ("include", "persona_id") if args.get(k)})
            text = await self.d["schedule_list_text"]()
            return text.split("\n")[0], items, text
        if name == "schedule_cancel":
            return await self.schedule_cancel(args)
        if name == "schedule_update":
            return await self.schedule_update(args)
        if name == "task_status":
            return await self.task_status(args)
        if name == "task_cancel":
            if not args.get("id"):
                raise Fail("invalid", "어느 작업인지 id를 알려 주세요.")
            view, say = await self.d["task_cancel"](args["id"])
            return say, view
        if name == "approval_status":
            return await self.approval_status(args)
        if name in ("memory_recall", "people_lookup", "remember", "forget", "memory_list"):
            return await getattr(self, name)(args, persona_id)
        if name == "web_search":
            q = str(args["query"]).strip()
            if not q:
                raise Fail("invalid", "무엇을 찾을지 알려 주세요.")
            r = await self.d["web_search"](q, int(args.get("max") or 5))
            src = "; ".join(f"{x['title'] or x['url']} <{x['url']}>" for x in r["sources"][:5])
            site = site_name(r["sources"][0]) if r["sources"] else ""
            first = re.split(r"(?<=[.!?。])\s+|\n", r["text"].strip())[0] if r["text"] else "찾은 결과가 없어요."
            lead = f"[{site} 기준] " if site else ""  # the citation in front, where a model copies it from
            return (lead + first)[:220], r, lead + (r["text"] or "찾은 결과가 없어요.") + (f"\n출처: {src}" if src else "") + \
                f"\n(답할 때 '{site or '출처'} 기준으로'처럼 사이트 이름만 짧게 말하고 URL은 읽지 마.)"
        if name == "web_fetch":
            try:
                r = await self.d["web_fetch"](str(args["url"]))
            except ValueError as e:
                raise Fail("invalid", str(e))
            return r["text"][:200], r, f"{r['text']}\n(출처: {r['url']}; 말할 때는 사이트 이름만.)"
        for source in ("cygnus", "google"):  # read-only sources with their own allowlist and checks
            defs, run = self.d.get(source, lambda: ([], None))()
            if run and name in {t["name"] for t in defs}:
                return await run(name, args)
        if where is None:
            _, where = await self.tools(caller)
        if name not in where:
            raise Fail("not_found", f"없는 도구예요: {str(name)[:40]}")
        return await self.device(name, args, where, caller, on_progress)

    async def device(self, name, args, where, caller, on_progress):
        url, tool = where[name]
        if caller in ("qwen", "voice") and tool == "delegate":
            raise Fail("invalid", "여러 단계 작업은 create_task로 맡겨 주세요.")
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
    async def call_user(self, args, persona_id, caller="api"):
        reason = str(args.get("reason") or "").strip()[:200]
        if not reason:
            raise Fail("invalid", "무엇 때문에 전화할지(reason)를 알려 주세요.")
        pid = args.get("persona_id") if isinstance(args.get("persona_id"), str) else persona_id
        when = args.get("when")
        kind = args.get("kind") if args.get("kind") in ("reminder", "wake") else "reminder"
        heard = None
        if args.get("when_text"):  # the user's words win over any time a model worked out itself
            heard = self.heard(args["when_text"], None, kind, "만들지")
            when = heard["at"].isoformat()
        by = "app" if caller == "app" else "voice"
        if args.get("repeat_text") and not args.get("repeat_cron"):  # the user's words -> a checked rule (cron.py)
            if "llm" not in self.d:
                raise Fail("unavailable", "지금은 반복 예약을 받을 수 없어요.")
            w = await C.from_words(args["repeat_text"], self.d["llm"], self.now())
            args = {**{k: v for k, v in args.items() if k != "repeat_text"}, "repeat_cron": w["cron"],
                    "start_date": w["start_date"] and w["start_date"].isoformat(),
                    "end_date": w["end_date"] and w["end_date"].isoformat(),
                    "notify": args.get("notify") or w["notify"], "kind": args.get("kind") or w["kind"]}
            kind = args["kind"] if args["kind"] in ("reminder", "wake") else "reminder"
        if args.get("repeat_cron"):
            return await self.call_user_rule(args, reason, kind, pid, by)
        key = (str(when or ""), pid)  # a model's retry often rewords the reason; a person's form never repeats by itself
        if when and caller != "app" and key in self.recent and time.monotonic() - self.recent[key][0] < 60:
            return self.recent[key][1] + " (이미 예약했어요)", self.recent[key][2]
        if when:
            item, say = await self.d["schedule_create"]({"at": when, "kind": kind, "note": "" if kind == "wake" else reason,
                                                         "repeat": args.get("repeat") or None,
                                                         **({"interpretation": heard["note"]}
                                                            if heard and heard["note"] else {}),
                                                         **({"notify": args["notify"]} if args.get("notify") else {}),
                                                         **({"persona_id": pid} if pid else {})}, by)
            await self.log_call(reason, item.get("at"), pid)
            say = heard["say"] if heard and heard["say"] else say
            self.recent[key] = (time.monotonic(), say, item)
            return say, item
        if self.d["in_call"]():
            raise Fail("conflict", "이미 통화 중이에요. 지금은 전화를 걸 수 없어요.")
        today = datetime.now(KST).strftime("%Y-%m-%d")
        made = await self.d["state"]("GET", f"/v1/state/kv/user-calls-{today}") or []
        if len(made) >= CALL_CAP:
            raise Fail("limit", f"오늘은 요청 전화를 {CALL_CAP}번 다 걸었어요.")
        await self.d["place_call"]("user_request", f"말씀하신 대로 전화드렸어요. {reason}", pid)
        await self.log_call(reason, None, pid)
        return "지금 전화를 걸게요."

    def now(self):
        return self.d.get("now", lambda: datetime.now(KST))()

    def heard(self, words, inherit, kind, verb):
        """late_night.when_from_words, or Need(clarify) with the one question to ask the user."""
        r = L.when_from_words(words, self.now(), inherit, kind)
        if r["at"] is None:
            raise Need("clarify", r["ask"],
                       f"아직 {verb} 않았어요. 사용자에게 \"{r['ask']}\"라고 물어보고, 답한 말을 when_text로 다시 불러 주세요.")
        return r

    @staticmethod
    def rule_check(args, expr):
        """(cron, start, end) parsed, or Need(confirm) with the read-back when an unconfirmed rule needs the user's yes."""
        expr = " ".join(str(expr).split())
        C.parse(expr)
        start, end = C.as_date(args.get("start_date")), C.as_date(args.get("end_date"))
        if (not C.trivial(expr) or start or end) and args.get("confirmed") is not True:
            verb = "알림 드릴게요" if args.get("notify") == "push" else "전화드릴게요"
            rule = C.rule_text(expr, start, end)
            q = f"{rule}{'' if rule.endswith('마다') else '에'} {verb}. 맞죠?"
            keep = ("reason", "kind", "notify", "persona_id", "id", "scope", "purpose")
            resend = {**{k: args[k] for k in keep if args.get(k)}, "repeat_cron": expr,
                      "start_date": start and start.isoformat(), "end_date": end and end.isoformat(), "confirmed": True}
            raise Need("confirm", q, f"아직 저장하지 않았어요. 사용자에게 '{q}'라고 읽어 주고, 맞다고 하면 confirmed=true로 "
                                     "다시 불러 주세요.", resend=resend)
        return expr, start, end

    async def call_user_rule(self, args, reason, kind, pid, by="voice"):
        expr, start, end = self.rule_check(args, args["repeat_cron"])
        body = {"cron": expr, "start_date": start and start.isoformat(), "end_date": end and end.isoformat(),
                "kind": kind, "notify": args.get("notify") or "call", "note": "" if kind == "wake" else reason,
                "request": reason, **({"at": args["when"]} if args.get("when") else {}),
                **({"persona_id": pid} if pid else {})}
        item, say = await self.d["schedule_create"](body, by)
        await self.log_call(reason, item.get("at"), pid)
        return say, item

    async def pick(self, args, soonest):
        """(item or None, message or None) the way cancel and update choose: id, a time (an item's next time or one of
        its rule's fires), a description, else the soonest (cancel) or the only one (update)."""
        items = await self.d["schedule_items"]()
        if args.get("id"):
            hits = [i for i in items if i["id"] == args["id"]]
        elif args.get("at"):
            t = iso(args["at"])
            hits = [i for i in items if datetime.fromisoformat(i["at"]) == t] or \
                [i for i in items if (e := rule_of(i)) and C.next_fire(e, t - timedelta(minutes=1)) == t]
        elif desc := str(args.get("description") or "").strip():
            words = [w for w in re.split(r"\s+", desc) if len(w) > 1] or [desc]  # "약" is a whole description
            hits = [i for i in items if all(w in " ".join((i.get("note", ""), i.get("purpose") or "", i["spoken"],
                                                          i.get("rule_text") or "")) for w in words)]
        else:
            hits = items[:1] if soonest else items
        if len(hits) == 1:
            return hits[0], None
        if not hits:
            raise Fail("not_found", "그런 예약이 없어요." if args else "예약이 없어요.")
        raise Need("choose", "어느 것인지 골라 주세요.",
                   "어느 것인지 골라 주세요: " + "; ".join(f"{i['spoken']}(id {i['id']})" for i in hits[:5]),
                   [{"id": i["id"], "spoken": i["spoken"]} for i in hits[:5]])

    async def schedule_update(self, args):
        item, _ = await self.pick(args, soonest=False)
        changes = {k: args[k] for k in ("when", "purpose", "notify", "repeat_cron") if args.get(k)}
        if args.get("when_text"):  # a bare hour keeps this item's (or this fire's) 오전/오후 and day
            base = iso(args["at"] if args.get("at") and args.get("scope") == "occurrence" else item["at"])
            changes["when"] = self.heard(args["when_text"], base, item.get("kind", "reminder"), "바꾸지")["at"].isoformat()
        changes.update({k: args[k] for k in ("start_date", "end_date") if k in args})
        if not changes:
            raise Fail("invalid", "무엇을 바꿀지 말씀해 주세요.")
        if changes.get("when"):
            try:
                iso(changes["when"])
            except Fail:
                raise Fail("invalid", "when은 2026-11-23T18:00:00+09:00 같은 ISO 시각이에요. 반복 규칙은 repeat_cron에 주세요.")
        scope = args.get("scope")
        if rule_of(item) and scope not in ("occurrence", "rule"):
            raise Need("clarify", "이번 한 번만 바꿀까요, 매번 바꿀까요?", ("아직 바꾸지 않았어요. 반복 예약이라 scope가 필요해요. 사용자가 '오늘', '내일', '24일'처럼 특정한 날을 "
                    "말했으면 scope=occurrence(at=그 회차 시각), 말하지 않았으면 scope=rule로 바로 다시 불러요. 둘 다 "
                    "말이 안 될 때만 '이번 한 번만 바꿀까요, 매번 바꿀까요?'라고 물어요."))
        if scope == "occurrence" and {"repeat_cron", "start_date", "end_date"} & set(changes):
            raise Fail("invalid", "한 번만 바꿀 때는 시각, 목적, 알림 방식만 바꿀 수 있어요.")
        if changes.get("repeat_cron"):
            expr, start, end = self.rule_check({**args, "notify": args.get("notify") or item.get("notify")},
                                               changes["repeat_cron"])
            changes["repeat_cron"] = expr
        occurrence = None
        if rule_of(item) and scope == "occurrence":
            if not args.get("at"):  # "오늘 2시 건": guessing the next fire (10시) would move the wrong one
                raise Need("clarify", "어느 회차를 바꿀까요?",
                           f"아직 바꾸지 않았어요. 바꿀 회차의 원래 시각을 at으로 주세요(예: 다음 회차 {item['at']}). "
                           "id나 설명과 함께 줘도 돼요.")
            occurrence = iso(args["at"]).isoformat()
        new, say = await self.d["schedule_update"](item["id"], changes, occurrence)
        return say, new

    # ---------- memory: Azel's own (ruchba records) and Cygnus (read PAT; writes with the memory-scoped PAT) ----------
    def mine(self, rec, persona_id):
        """A record this persona may see: shared ones, and its own."""
        p = (rec.get("meta") or {}).get("persona")
        return p in (None, "", "shared") or p == persona_id

    async def cygnus_ok(self, persona_id):
        """The user's own Cygnus memory: the primary persona and personas that share memory (PROTOCOL.md)."""
        return await self.d.get("persona_shares", lambda pid: none_true())(persona_id)

    async def cygnus_read(self, name, args):
        _, run = self.d["cygnus"]()
        if not run:
            return ""
        out = await run(name, args)
        return "" if out.startswith(("지금은 Cygnus", "이 기기에는", "모르는 도구", "Cygnus가 거절")) or out == "(결과 없음)" \
            else out

    async def memory_recall(self, args, persona_id):
        q = str(args["query"]).strip()
        if not q:
            raise Fail("invalid", "무엇을 찾을지 알려 주세요.")
        recs = [r for r in await self.d["memory_search"](q, 8) if self.mine(r, persona_id) and r.get("score", 1) >= 0.25]
        cyg = await self.cygnus_read("memory_recall", {"query": q}) if await self.cygnus_ok(persona_id) else ""
        lines = [f"[아젤 기억 {str(r.get('created', ''))[:10]}] {r['text'][:240]}" for r in recs[:4]]
        if cyg:
            lines.append(f"[Cygnus 기억] {cyg[:900]}")
        if not lines:
            return "기억에서 찾지 못했어요.", {"azel": [], "cygnus": ""}, "기억에서 찾지 못했어요. 지어내지 말고 모른다고 해."
        spoken = (recs[0]["text"][:120] + " (아젤 기억)") if recs else "Cygnus 기억에서 찾았어요."
        return spoken, {"azel": recs[:4], "cygnus": cyg}, "\n".join(lines) + "\n(출처를 밝혀 답하고, 없는 사실은 지어내지 마.)"

    async def people_lookup(self, args, persona_id):
        q = " ".join(str(args.get(k) or "") for k in ("name", "relation", "query")).strip()
        if not q:
            raise Fail("invalid", "누구를 찾을지(name, relation, query) 알려 주세요.")
        people = await self.cygnus_read("gistra_people", {"query": args["name"]} if args.get("name") else {})
        known = await self.cygnus_read("memory_recall", {"query": f"{q} 사람 person", "kind": "person"}) \
            if await self.cygnus_ok(persona_id) else ""
        recs = [r for r in await self.d["memory_search"](q, 6) if self.mine(r, persona_id) and r.get("score", 1) >= 0.25]
        lines = ([f"[Cygnus 사람 목록] {people[:900]}"] if people else []) + \
            ([f"[Cygnus 기억] {known[:600]}"] if known else []) + [f"[아젤 기억] {r['text'][:200]}" for r in recs[:3]]
        if not lines:
            return "그 사람에 대한 정보를 찾지 못했어요.", {}, "찾지 못했어요. 지어내지 말고 모른다고 해."
        return "찾았어요.", {"people": people, "cygnus": known, "azel": recs[:3]}, \
            "\n".join(lines) + "\n(출처를 밝혀 답하고, 결과에 없는 사람이나 사실은 지어내지 마.)"

    async def remember(self, args, persona_id):
        import cygnus  # the secret rule is the same one the Cygnus write path enforces
        fact = re.sub(r"\s+", " ", str(args["fact"])).strip(" .")
        about = str(args.get("about") or "").strip()[:40]
        if not fact:
            raise Fail("invalid", "무엇을 기억할지 알려 주세요.")
        if cygnus.SECRET.search(f"{fact} {about}"):
            raise Fail("invalid", "비밀번호나 카드번호 같은 비밀은 기억하지 않을게요. 그런 건 안전한 곳에 따로 두세요.")
        shared = args.get("persona_scope", "shared") == "shared"
        cyg_id, note = None, ""
        if shared and self.d.get("cygnus_can_write", lambda: False)() and await self.cygnus_ok(persona_id):
            # only an explicit "기억해" goes to Cygnus
            kind = "person" if about else "preference" if re.search(r"좋아|싫어|선호|즐겨|별로", fact) else "fact"
            try:
                out = await self.d["cygnus_write"]("memory_save", {"text": f"{fact}" + (f" ({about})" if about else ""),
                                                                   "kind": kind})
                cyg_id, note = cygnus_saved(out)
            except Exception:  # noqa: BLE001  Azel still remembers; say Cygnus didn't take it
                note = " (Cygnus에는 지금 못 남겼어요.)"
        rec = await self.d["memory_add"](fact, {"memory": True, "source": "explicit", "about": about or None,
                                                "persona": None if shared else persona_id, "cygnus_id": cyg_id})
        return f"기억해 둘게요: {fact}.{note}", {"id": str(rec.get("id")), "cygnus_id": cyg_id}

    async def memory_list(self, args, persona_id):
        rows = [r for r in await self.d["memory_list"](int(args.get("limit") or 50)) if self.mine(r, persona_id)]
        items = [{"id": str(r["id"]), "text": r["text"], "source": (r.get("meta") or {}).get("source", "explicit"),
                  "about": (r.get("meta") or {}).get("about"), "persona": (r.get("meta") or {}).get("persona"),
                  "in_cygnus": bool((r.get("meta") or {}).get("cygnus_id")), "created": str(r.get("created", ""))}
                 for r in rows]
        return (f"기억하는 게 {len(items)}개 있어요." if items else "아직 기억하는 게 없어요."), items, \
            "\n".join(f"- id {i['id']}: {i['text'][:80]}" for i in items[:20]) or "아직 기억하는 게 없어요."

    async def forget(self, args, persona_id):
        rows = [r for r in await self.d["memory_list"](200) if self.mine(r, persona_id)]
        if args.get("id"):
            hits = [r for r in rows if str(r["id"]) == str(args["id"])]
        elif args.get("query"):
            words = [w for w in str(args["query"]).split() if len(w) > 1] or [str(args["query"])]
            hits = [r for r in rows if all(w in r["text"] for w in words)]
        else:
            raise Fail("invalid", "무엇을 잊을지 알려 주세요.")
        if not hits:
            raise Fail("not_found", "그런 기억이 없어요.")
        if len(hits) > 1:
            raise Need("choose", "어느 기억을 지울까요?",
                       "어느 기억인지 골라 주세요: " + "; ".join(f"{r['text'][:40]}(id {r['id']})" for r in hits[:5]),
                       [{"id": str(r["id"]), "spoken": r["text"][:60]} for r in hits[:5]])
        r = hits[0]
        if args.get("confirmed") is not True:
            q = f"'{r['text'][:80]}' 이 기억을 지울까요?"
            raise Need("confirm", q, f"아직 지우지 않았어요. 사용자에게 \"{q}\"라고 묻고, 맞다고 하면 id={r['id']}, "
                                     "confirmed=true로 다시 불러 주세요.", resend={"id": str(r["id"]), "confirmed": True})
        await self.d["memory_delete"](r["id"])
        cid = (r.get("meta") or {}).get("cygnus_id")
        if cid and self.d.get("cygnus_can_write", lambda: False)():
            try:
                await self.d["cygnus_write"]("memory_edit", {"id": cid, "op": "archive"})
            except Exception:  # noqa: BLE001
                return f"잊었어요: {r['text'][:80]}. (Cygnus 쪽은 지금 지우지 못했어요.)", {"id": str(r["id"])}
        return f"잊었어요: {r['text'][:80]}.", {"id": str(r["id"])}

    async def log_call(self, reason, at, pid):
        """Every user-requested call: who, when (None = now), why. The same list counts the daily cap."""
        today = datetime.now(KST).strftime("%Y-%m-%d")
        rows = await self.d["state"]("GET", f"/v1/state/kv/user-calls-{today}") or []
        rows.append({"at": time.time(), "scheduled_for": at, "reason": reason, "persona": pid})
        await self.d["state"]("PUT", f"/v1/state/kv/user-calls-{today}", rows[-100:])

    async def schedule_cancel(self, args):
        """One item, always through pick(): by id, time or description. Nothing given: the only item, else ask which.
        A recurring item needs a scope: occurrence skips one fire (the one at `at`, else the next) and keeps the
        rule; rule cancels it all. Without one, ask: "내일 7시 알람 취소" must not end a daily 7 am rule."""
        if args.get("at"):
            iso(args["at"])
        if args.get("id") or args.get("at") or str(args.get("description") or "").strip():
            item, _ = await self.pick(args, soonest=False)
        else:
            items = await self.d["schedule_items"]()
            if not items:
                raise Fail("not_found", "취소할 예약이 없어요.")
            if len(items) > 1:
                raise Need("choose", "어느 예약을 취소할까요?",
                           "아직 취소하지 않았어요. 어느 것인지 골라 주세요: " +
                           "; ".join(f"{i['spoken']}(id {i['id']})" for i in items[:5]),
                           [{"id": i["id"], "spoken": i["spoken"]} for i in items[:5]])
            item = items[0]
        if rule_of(item):
            if args.get("scope") == "occurrence":
                done = await self.d["schedule_cancel"](item["id"], None, occurrence=args.get("at") or item["at"])
                return cancelled(done)
            if args.get("scope") != "rule":
                raise Need("clarify", "이번 한 번만 취소할까요, 매번 취소할까요?",
                           "아직 취소하지 않았어요. 반복 예약이라 scope가 필요해요: 이번 한 번이면 occurrence(그 날짜의 at과 함께), "
                           "매번이면 rule로 다시 보내.", resend={**args, "id": item["id"]})
        done = await self.d["schedule_cancel"](item["id"], None)
        return cancelled(done)

    async def task_status(self, args):
        ts = [t for t in [await self.d["task_get"](args["id"])] if t] if args.get("id") else \
            await self.d["task_recent"](max(1, min(int(args.get("limit") or 3), 100)))
        if not ts and args.get("id"):
            raise Fail("not_found", "그런 작업이 없어요.")
        if not ts:
            return "맡긴 작업이 없어요.", []
        view = self.d.get("task_view", lambda t, full=False: t)
        one = bool(args.get("id"))
        return "\n".join(f"- {t['id']} {t['status']}: {t['goal'][:60]}" + (f" → {str(t.get('result') or t.get('error'))[:200]}"
                                                                       if t.get("result") or t.get("error") else "")
                         for t in ts), (view(ts[0], True) if one else [view(t) for t in ts])

    async def approval_status(self, args):
        if args.get("id") and "approval_get" in self.d:  # the gateway's own view (expiry, requester)
            a = await self.d["approval_get"](args["id"])
            return f"{a.get('device', '')}의 '{str(a.get('command', ''))[:60]}': {a.get('status')}", a
        if "approval_list" in self.d:
            rows = await self.d["approval_list"]()
            return ("\n".join(f"- {a['id']} {a.get('device', '')}: {str(a.get('command', ''))[:60]}" for a in rows[:5])
                    or "기다리는 승인 요청이 없어요."), rows
        if args.get("id"):
            a = await self.d["state"]("GET", f"/v1/state/approvals/{args['id']}")
            if not a:
                raise Fail("not_found", "그런 승인 요청이 없어요.")
            return f"{a.get('device', '')}의 '{str(a.get('command', ''))[:60]}': {a.get('status')}", a
        rows = await self.d["state"]("GET", "/v1/state/approvals") or []
        pending = [a for a in rows if a.get("status") == "pending"]
        return ("\n".join(f"- {a['id']} {a.get('device', '')}: {str(a.get('command', ''))[:60]}" for a in pending[:5])
                or "기다리는 승인 요청이 없어요."), rows

    async def create_task(self, args, persona_id, caller):
        goal = str(args.get("goal") or "").strip()
        notify = args.get("notify") or ("call" if caller in ("voice", "qwen", "claude_code") else "push")
        if not goal:
            raise Fail("invalid", "맡길 일(goal)을 알려 주세요.")
        body = {"goal": goal, "notify": notify, **({"device": args["device"]} if args.get("device") else {}),
                **({"backend": args["backend"]} if args.get("backend") else {}),
                **{k: args[k] for k in ("budget_usd", "max_minutes") if k in args},
                **({"persona": args.get("persona") or persona_id} if args.get("persona") or persona_id else {})}
        t = await self.d["task_create"](body, "app" if caller == "app" else "tool")
        how = {"call": "끝나면 전화드릴게요.", "push": "끝나면 알림으로 알려 드릴게요.", "none": ""}[notify]
        return f"작업으로 맡겼어요. {how}".strip() + f" (작업 {t['id']})", \
            self.d.get("task_view", lambda t, full=False: t)(t, True)


class Need(Exception):
    """Nothing happened: the user must answer first. kind: confirm | clarify | choose. `question` is for the user,
    `hint` tells a model how to resend."""

    def __init__(self, kind, question, hint=None, options=None, resend=None):
        super().__init__(question)
        self.kind, self.question, self.hint, self.options, self.resend = kind, question, hint or question, options, resend


class Fail(Exception):
    """Nothing happened, and asking won't help: code invalid | not_found | conflict | limit | unavailable | failed."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def check_args(schema, args, path=""):
    """The subset of JSON Schema our own tools use: type, required, enum, pattern, maxLength, minimum, maximum,
    additionalProperties false. Invalid input is the same error on every surface (a trust boundary)."""
    def bad(where, why):
        raise Fail("invalid", f"입력이 맞지 않아요: {where or '인자'} {why}")
    props = schema.get("properties", {})
    for k in schema.get("required", []):
        if k not in args:
            bad(k, "가 필요해요")
    for k, v in args.items():
        p = props.get(k)
        if p is None:
            if schema.get("additionalProperties") is False:
                bad(k, "는 없는 인자예요")
            continue
        types = p.get("type")
        types = types if isinstance(types, list) else [types] if types else []
        kinds = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "null": type(None),
                 "object": dict, "array": list}
        if types and not any(isinstance(v, kinds[t]) and not (t == "integer" and isinstance(v, bool)) for t in types):
            bad(k, f"의 형식이 {'/'.join(types)}가 아니에요")
        if v is None:
            continue
        if "enum" in p and v not in p["enum"]:
            bad(k, f"는 {', '.join(map(str, p['enum']))} 중 하나여야 해요")
        if isinstance(v, str) and ("maxLength" in p and len(v) > p["maxLength"]
                                   or "pattern" in p and not re.search(p["pattern"], v)):
            bad(k, "의 값이 맞지 않아요")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and ("minimum" in p and v < p["minimum"] or "maximum" in p and v > p["maximum"]):
            bad(k, "가 범위를 벗어났어요")


HTTP_CODES = {400: "invalid", 404: "not_found", 409: "conflict", 429: "limit", 503: "unavailable"}
HTTP_OF = {"invalid": 400, "not_found": 404, "conflict": 409, "limit": 429, "unavailable": 503, "failed": 502}


def result(ok, spoken, data=None, needs=None, error=None):
    return {"ok": ok, "spoken": str(spoken), "data": data, "needs": needs, "error": error}


def public(r):
    """The result as surfaces outside the gateway see it (no internal keys)."""
    return {k: r[k] for k in ("ok", "spoken", "data", "needs", "error")}


def for_model(r):
    """What a model reads back: the hint when the user must answer, else the spoken text (or a fuller model text)."""
    return r.get("_model") or r["spoken"]


def cancelled(done):
    if not done:
        raise Fail("not_found", "그런 예약이 없어요.")
    return done[1], done[0]


def site_name(src):
    """'기상청' from a result's title, else the bare domain ('weather.go.kr')."""
    title = re.split(r"\s*[|·:\-–—]\s*", str(src.get("title") or ""))[-1].strip()
    if title and len(title) <= 20:
        return title
    host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", str(src.get("url", ""))).split("/")[0])
    return host


def cygnus_saved(out):
    """(id, note) from memory_save's reply. Only a "saved" item is ours: a "similar" reply names someone else's
    memory (forget must never archive it), and a held one is not a memory until the owner approves it."""
    try:
        item = (json.loads(out) or [{}])[0]
    except (ValueError, TypeError, IndexError, KeyError):
        return None, " (Cygnus 응답을 확인하지 못했어요.)"
    status = item.get("status")
    if status == "saved" and isinstance(item.get("id"), str):
        return item["id"], ""
    if status == "similar":
        return None, " (Cygnus에는 비슷한 기억이 이미 있어요.)"
    return None, " (Cygnus에서는 확인을 기다려요.)"


async def none_true():
    return True


def iso(text):
    try:
        t = datetime.fromisoformat(str(text))
    except ValueError:
        raise Fail("invalid", "시각은 2026-11-23T18:00:00+09:00 같은 ISO 형식으로 주세요.")
    if t.tzinfo is None:
        raise Fail("invalid", "시각에 +09:00 같은 시간대가 필요해요.")
    return t


def rule_of(item):
    """An item's cron: its own, or its legacy repeat at its time of day; None for a one-off."""
    if item.get("cron"):
        return item["cron"]
    return C.legacy(item.get("repeat"), datetime.fromisoformat(item["at"])) if item.get("repeat") else None


def ok(text):
    return {"content": str(text), "is_error": False, "waited_s": 0}


def err(text):
    return {"content": str(text), "is_error": True, "waited_s": 0}


# ---------- the same tools as an MCP server, for `claude -p` (POST /v1/mcp, stateless JSON-RPC) ----------
MEMORY_RECALL = {
    "name": "memory_recall",
    "description": ("사용자에 대한 기억 검색: 취향, 주변 사람, 지난 일, 계획, 결정. 아젤의 기억(지난 통화 포함)과 Cygnus 장기 기억을 "
                    "함께 찾아 출처와 함께 돌려준다. 사용자·사람·과거·취향·계획을 물으면 '모르겠어요' 하기 전에 먼저 불러. "
                    "결과에 없는 사실은 지어내지 마."),
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
        "query": {"type": "string", "maxLength": 120, "description": "찾을 말 두세 개(예: '커피 취향', '지도교수')"}}}}
PEOPLE_LOOKUP = {
    "name": "people_lookup",
    "description": ("주변 사람 찾기(읽기 전용): 교수·조교의 과목, 연락처, 사무실, 면담 시간과 기억에 있는 사람들. "
                    "name(이름), relation(지도교수, 친구 같은 관계), query 중 하나 이상. '주변 사람', '누구야'는 이걸로."),
    "input_schema": {"type": "object", "additionalProperties": False, "properties": {
        "name": {"type": "string", "maxLength": 40}, "relation": {"type": "string", "maxLength": 40},
        "query": {"type": "string", "maxLength": 80}}}}
REMEMBER = {
    "name": "remember",
    "description": ("사용자가 '기억해', '잊지 마'라고 하거나 오래 갈 취향·사실·계획을 말하면 저장한다. 한 문장의 사실로. "
                    "비밀번호, 카드번호, 계좌, 토큰 같은 비밀은 저장하지 않는다. 결과의 문장을 그대로 읽어 줘."),
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["fact"], "properties": {
        "fact": {"type": "string", "maxLength": 300, "description": "예: '좋아하는 커피는 아이스 바닐라 라떼'"},
        "about": {"type": "string", "maxLength": 40, "description": "사람에 관한 거면 그 사람"},
        "persona_scope": {"type": "string", "enum": ["shared", "persona"],
                          "description": "shared(모든 비서가 공유, 기본) 또는 persona(이 비서만)"}}}}
FORGET = {
    "name": "forget",
    "description": "기억 하나를 지운다. 먼저 찾은 기억을 사용자에게 읽어 주고 확인받은 뒤 confirmed=true로 다시 불러.",
    "input_schema": {"type": "object", "additionalProperties": False, "properties": {
        "query": {"type": "string", "maxLength": 120}, "id": {"type": "string", "pattern": "^[0-9]{1,18}$"},
        "confirmed": {"type": "boolean"}}}}
MEMORY_LIST = {"name": "memory_list", "description": "아젤이 기억하는 것 목록(최근 순, 직접 기억한 것과 통화에서 자동으로 남긴 것).",
               "input_schema": {"type": "object", "additionalProperties": False, "properties": {
                   "limit": {"type": "integer", "minimum": 1, "maximum": 200}}}}
WEB_SEARCH = {
    "name": "web_search",
    "description": ("웹 검색(날씨, 뉴스, 가격·환율, 사실 확인처럼 지금 정보가 필요할 때). 답할 때는 '○○ 기준으로'처럼 출처 "
                    "사이트 이름만 짧게 말하고 URL은 읽지 마."),
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
        "query": {"type": "string", "maxLength": 200}, "max": {"type": "integer", "minimum": 1, "maximum": 8}}}}
WEB_FETCH = {
    "name": "web_fetch",
    "description": "웹 페이지 하나를 읽어 글만 돌려준다(http/https 주소만, 내부 주소는 안 됨). 출처는 사이트 이름만 말해.",
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["url"], "properties": {
        "url": {"type": "string", "maxLength": 2000}}}}
WEB_NAMES = {"web_search", "web_fetch"}  # the Claude API path has native server tools by these names
# schedule_create: call_user that must have a time (the app's "new schedule" form); same handler, same checks.
SCHEDULE_CREATE = {"name": "schedule_create",
                   "description": "예약 전화를 만든다(지금 거는 건 call_user). 인자와 확인 규칙은 call_user와 같고 시각이 꼭 있어야 한다.",
                   "input_schema": CALL_USER["input_schema"]}
OWN_TOOLS = (CALL_USER, SCHEDULE_LIST, SCHEDULE_CANCEL, SCHEDULE_UPDATE, CREATE_TASK, TASK_STATUS, TASK_CANCEL,
             APPROVAL_STATUS, MEMORY_RECALL, PEOPLE_LOOKUP, REMEMBER, FORGET, MEMORY_LIST, WEB_SEARCH, WEB_FETCH)
CYGNUS_WRAPPED = {"memory_recall", "gistra_people"}  # reached through memory_recall / people_lookup, not on their own
ALL_OWN = (*OWN_TOOLS, SCHEDULE_CREATE)  # schedule_create is not in model tool lists: call_user covers it there
OWN_SCHEMAS = {t["name"]: t["input_schema"] for t in ALL_OWN}
MCP_TOOLS = ALL_OWN  # claude -p already has the daemons, Cygnus and the claude.ai connectors as its own MCP servers
# The external token (the user's own Claude Code, later claude.ai): ring, schedule and hand off work, nothing personal.
SCOPES = {"full": {t["name"] for t in MCP_TOOLS},
          # no personal reads, memory or tasks: a task runs with the whole bridge and its results hold personal data
          "external": {"call_user", "schedule_create", "schedule_list", "schedule_cancel", "schedule_update"}}
APP_ACTIONS = None  # set below from ACTIONS: the app's actions; device tools and the web stay with models
# ACTIONS.md §1 metadata. rest: the dedicated route (method, path, how the action's args map), or the reason there
# is none. examples: (valid, invalid) inputs for the conformance test.
ACTIONS = {
    "call_user": {"read_only": False, "scopes": ("internal", "qwen", "app", "external"),
                  "rest": ("POST", "/v1/calls"), "examples": ({"reason": "회의 알림"}, {"reason": ""})},
    "schedule_create": {"read_only": False, "scopes": ("internal", "app", "external"),
                        "rest": ("POST", "/v1/schedule"),
                        "examples": ({"reason": "약 먹기", "when_text": "3시"}, {"reason": "약 먹기"})},
    "schedule_list": {"read_only": True, "scopes": ("internal", "qwen", "app", "external"),
                      "rest": ("GET", "/v1/schedule"), "examples": ({}, {"include": "x"})},
    "schedule_update": {"read_only": False, "scopes": ("internal", "qwen", "app", "external"),
                        "rest": ("PUT", "/v1/schedule/{id}"),
                        "examples": ({"id": "{id}", "purpose": "물 마시기"}, {"id": "{id}"})},
    "schedule_cancel": {"read_only": False, "scopes": ("internal", "qwen", "app", "external"),
                        "rest": ("DELETE", "/v1/schedule/{id}"), "examples": ({"id": "{id}"}, {"id": "nope"})},
    "create_task": {"read_only": False, "scopes": ("internal", "qwen", "app"),
                    "rest": ("POST", "/v1/tasks"), "examples": ({"goal": "정리", "notify": "none"}, {"goal": ""})},
    "task_status": {"read_only": True, "scopes": ("internal", "qwen", "app"),
                    "rest": ("GET", "/v1/tasks/{id}"), "examples": ({"id": "{task}"}, {"id": "000000000000"})},
    "task_cancel": {"read_only": False, "scopes": ("internal", "qwen", "app"),
                    "rest": ("POST", "/v1/tasks/{id}/cancel"), "examples": ({"id": "{task}"}, {"id": "000000000000"})},
    "approval_status": {"read_only": True, "scopes": ("internal", "qwen", "app"),
                        "rest": ("GET", "/v1/approvals/{id}"), "examples": ({}, {"id": "zz"})},
    "memory_recall": {"read_only": True, "scopes": ("internal", "qwen", "app"),
                      "rest": ("POST", "/v1/memories/recall"), "examples": ({"query": "커피"}, {"query": ""})},
    "people_lookup": {"read_only": True, "scopes": ("internal", "qwen", "app"),
                      "rest": ("POST", "/v1/memories/people"), "examples": ({"name": "김교수"}, {})},
    "remember": {"read_only": False, "scopes": ("internal", "qwen", "app"),
                 "rest": ("POST", "/v1/memories"),
                 "examples": ({"fact": "좋아하는 커피는 아이스 라떼"}, {"fact": "내 카드번호는 1234 5678 9012 3456"})},
    "forget": {"read_only": False, "scopes": ("internal", "qwen", "app"),
               "rest": ("DELETE", "/v1/memories/{id}"),
               "examples": ({"id": "{mem}", "confirmed": True}, {"id": "999999999", "confirmed": True})},
    "memory_list": {"read_only": True, "scopes": ("internal", "qwen", "app"),
                    "rest": ("GET", "/v1/memories"), "examples": ({}, {"limit": 0})},
}
for _n, _m in {
    "web_search": {"read_only": True, "scopes": ("internal", "qwen"), "rest": None,
                   "no_rest": "앱에는 검색 화면이 없어요(모델 경로 전용; claude -p는 자체 WebSearch).",
                   "examples": ({"query": "광주 날씨"}, {"query": ""})},
    "web_fetch": {"read_only": True, "scopes": ("internal", "qwen"), "rest": None,
                  "no_rest": "앱에는 페이지 읽기 화면이 없어요(모델 경로 전용).",
                  "examples": ({"url": "https://example.com"}, {"url": "file:///etc/passwd"})},
}.items():
    ACTIONS[_n] = _m
APP_ACTIONS = {n for n, m in ACTIONS.items() if "app" in m["scopes"]}
NOT_ON_REST = {  # tool families with no dedicated app route (ACTIONS.md §7); tailnet POST /v1/tools/call reaches them
    "device": "기기 작업은 앱에서 create_task로 맡겨요(명령 실행은 승인과 함께 모델 경로에서만).",
    "cygnus": "앱에 아직 화면이 없는 개인 조회예요.",
    "google": "앱에 아직 화면이 없는 개인 조회예요.",
}


async def mcp_handle(bridge, msg, persona_id=None, scope="full"):
    """One JSON-RPC message; None for notifications. A tool outside the caller's scope is invisible in tools/list
    and refused in tools/call exactly like a tool that doesn't exist."""
    allowed = SCOPES[scope]
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "azel", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
                            for t in MCP_TOOLS if t["name"] in allowed]}
    elif method == "tools/call":
        p = msg.get("params") or {}
        if p.get("name") not in allowed:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
        r = await bridge.call(p["name"], p.get("arguments") or {}, persona_id,
                              caller="claude_code" if scope == "full" else "external")
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
