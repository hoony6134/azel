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
            "사용자가 말한 시각 표현 그대로(예: '6시', '내일 오후 1시', '30분 뒤', '금요일 3시 반'). 시각은 게이트웨이가 정하니 "
            "직접 계산하지 마. 애매하면 도구가 물어볼 말을 돌려줘")},
        "reason": {"type": "string", "maxLength": 200},
        "kind": {"type": "string", "enum": ["reminder", "wake"], "description": "wake = 깨우는 전화"},
        "repeat": {"type": ["string", "null"], "description": "옛 방식: daily, weekdays, weekly:0..6(0=월). repeat_cron을 쓸 것"},
        "repeat_cron": {"type": ["string", "null"], "description": (
            "반복이면 5칸 cron(KST, 분 시 일 월 요일, 요일 0=일요일). 예: 매일 7시 '0 7 * * *', 화목 2시 '0 14 * * 2,4', "
            "평일 10시~6시 2시간마다 '0 10-18/2 * * 1-5', 매월 1일 '0 9 1 * *'. 전화 사이 30분 이상, 하루 20번까지")},
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
                   "여러 개가 맞으면 아무것도 취소하지 않고 어느 것인지 돌려준다. 아무것도 안 주면 가장 가까운 예약. "
                   "반복 예약은 scope: occurrence(at의 그 회차, 없으면 다음 회차 하나만) 또는 rule(반복 전체, 기본).",
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
            "사용자가 말한 시각 표현 그대로(예: '6시', '내일 오후 1시', '30분 뒤', '금요일 3시 반'). 시각은 게이트웨이가 정하니 "
            "직접 계산하지 마. 애매하면 도구가 물어볼 말을 돌려줘")},
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
        if caller == "qwen":
            dev_defs = [t for t in dev_defs if where[t["name"]][1] != "delegate"]
        cyg_defs, _ = self.d["cygnus"]()
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
                                                 **({"options": e.options} if e.options else {})})
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
        if caller == "qwen" and tool == "delegate":
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
            raise Need("confirm", q, f"아직 저장하지 않았어요. 사용자에게 '{q}'라고 읽어 주고, 맞다고 하면 confirmed=true로 "
                                     "다시 불러 주세요.")
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

    async def log_call(self, reason, at, pid):
        """Every user-requested call: who, when (None = now), why. The same list counts the daily cap."""
        today = datetime.now(KST).strftime("%Y-%m-%d")
        rows = await self.d["state"]("GET", f"/v1/state/kv/user-calls-{today}") or []
        rows.append({"at": time.time(), "scheduled_for": at, "reason": reason, "persona": pid})
        await self.d["state"]("PUT", f"/v1/state/kv/user-calls-{today}", rows[-100:])

    async def schedule_cancel(self, args):
        """By id or time through the gateway's schedule_cancel; by description only when exactly one item matches.
        scope="occurrence" on a recurring item skips one fire (the one at `at`, else the next) and keeps the rule."""
        if args.get("scope") == "occurrence":
            if args.get("at"):
                iso(args["at"])
            item, _ = await self.pick(args, soonest=True)
            if not rule_of(item):
                args = {**args, "id": item["id"]}  # a one-off item: cancelling its only occurrence is cancelling it
            else:
                done = await self.d["schedule_cancel"](item["id"], None, occurrence=args.get("at") or item["at"])
                return cancelled(done)
        if args.get("id") or args.get("at"):
            at = datetime.fromisoformat(args["at"]).timestamp() if args.get("at") else None
            done = await self.d["schedule_cancel"](args.get("id"), at)
            return cancelled(done)
        if str(args.get("description") or "").strip():
            item, _ = await self.pick(args, soonest=True)
            done = await self.d["schedule_cancel"](item["id"], None)
            return cancelled(done)
        done = await self.d["schedule_cancel"](None, None)  # the soonest, as the voice path always did
        if not done:
            raise Fail("not_found", "취소할 예약이 없어요.")
        return done[1], done[0]

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

    def __init__(self, kind, question, hint=None, options=None):
        super().__init__(question)
        self.kind, self.question, self.hint, self.options = kind, question, hint or question, options


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
# schedule_create: call_user that must have a time (the app's "new schedule" form); same handler, same checks.
SCHEDULE_CREATE = {"name": "schedule_create",
                   "description": "예약 전화를 만든다(지금 거는 건 call_user). 인자와 확인 규칙은 call_user와 같고 시각이 꼭 있어야 한다.",
                   "input_schema": CALL_USER["input_schema"]}
OWN_TOOLS = (CALL_USER, SCHEDULE_LIST, SCHEDULE_CANCEL, SCHEDULE_UPDATE, CREATE_TASK, TASK_STATUS, TASK_CANCEL,
             APPROVAL_STATUS)
ALL_OWN = (*OWN_TOOLS, SCHEDULE_CREATE)  # schedule_create is not in model tool lists: call_user covers it there
OWN_SCHEMAS = {t["name"]: t["input_schema"] for t in ALL_OWN}
MCP_TOOLS = ALL_OWN  # claude -p already has the daemons, Cygnus and the claude.ai connectors as its own MCP servers
# The external token (the user's own Claude Code, later claude.ai): ring, schedule and hand off work, nothing personal.
SCOPES = {"full": {t["name"] for t in MCP_TOOLS},
          "external": {"call_user", "schedule_create", "schedule_list", "schedule_cancel", "schedule_update",
                       "create_task", "task_status", "task_cancel"}}
APP_ACTIONS = {t["name"] for t in ALL_OWN}  # the app: its own actions; device and personal reads stay with models
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
    "create_task": {"read_only": False, "scopes": ("internal", "qwen", "app", "external"),
                    "rest": ("POST", "/v1/tasks"), "examples": ({"goal": "정리", "notify": "none"}, {"goal": ""})},
    "task_status": {"read_only": True, "scopes": ("internal", "qwen", "app", "external"),
                    "rest": ("GET", "/v1/tasks/{id}"), "examples": ({"id": "{task}"}, {"id": "000000000000"})},
    "task_cancel": {"read_only": False, "scopes": ("internal", "qwen", "app", "external"),
                    "rest": ("POST", "/v1/tasks/{id}/cancel"), "examples": ({"id": "{task}"}, {"id": "000000000000"})},
    "approval_status": {"read_only": True, "scopes": ("internal", "qwen", "app"),
                        "rest": ("GET", "/v1/approvals/{id}"), "examples": ({}, {"id": "zz"})},
}
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
