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
SCHEDULE_LIST = {"name": "schedule_list", "description": "예약된 전화 목록(id 포함).",
                 "input_schema": {"type": "object", "additionalProperties": False, "properties": {}}}
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
        """{"content": str, "is_error": bool, "waited_s": seconds spent waiting for an approval}."""
        args = args if isinstance(args, dict) else {}
        try:
            if name in ("call_user", "schedule_update") and args.get("when") and not args.get("when_text") \
                    and caller != "app":
                raise Refused("시각은 직접 계산하지 말고, 사용자가 말한 표현 그대로 when_text로 주세요(예: '6시', '내일 오후 1시').")
            if name == "call_user":
                return ok(await self.call_user(args, persona_id))
            if name == "create_task":
                return ok(await self.create_task(args, persona_id, caller))
            if name == "schedule_list":
                return ok(await self.d["schedule_list_text"]())
            if name == "schedule_cancel":
                return ok(await self.schedule_cancel(args))
            if name == "schedule_update":
                return ok(await self.schedule_update(args))
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
        except (Refused, C.CronError) as e:  # a reason to say as is
            return err(str(e))
        except Exception as e:  # a failed tool is an answer for the model, never a crash
            if getattr(e, "status_code", None) == 400 and isinstance(getattr(e, "detail", None), str):
                return err(e.detail)  # the gateway's own refusal (limits, dates), already in Korean
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
        heard = None
        if args.get("when_text"):  # the user's words win over any time a model worked out itself
            heard = self.heard(args["when_text"], None, kind, "만들지")
            when = heard["at"].isoformat()
        if args.get("repeat_cron"):
            return await self.call_user_rule(args, reason, kind, pid)
        key = (str(when or ""), str(args.get("repeat_cron") or ""))  # a retry often rewords the reason
        if when and key in self.recent and time.monotonic() - self.recent[key][0] < 60:
            return self.recent[key][1] + " (이미 예약했어요)"
        if when:
            item, say = await self.d["schedule_create"]({"at": when, "kind": kind, "note": "" if kind == "wake" else reason,
                                                         "repeat": args.get("repeat") or None,
                                                         **({"interpretation": heard["note"]}
                                                            if heard and heard["note"] else {}),
                                                         **({"notify": args["notify"]} if args.get("notify") else {}),
                                                         **({"persona_id": pid} if pid else {})}, "voice")
            await self.log_call(reason, item.get("at"), pid)
            say = heard["say"] if heard and heard["say"] else say
            self.recent[key] = (time.monotonic(), say)
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

    def now(self):
        return self.d.get("now", lambda: datetime.now(KST))()

    def heard(self, words, inherit, kind, verb):
        """late_night.when_from_words, or Refused with the one question to ask the user."""
        r = L.when_from_words(words, self.now(), inherit, kind)
        if r["at"] is None:
            raise Refused(f"아직 {verb} 않았어요. 사용자에게 \"{r['ask']}\"라고 물어보고, 답한 말을 when_text로 다시 불러 주세요.")
        return r

    @staticmethod
    def rule_check(args, expr):
        """(cron, start, end) parsed, or Refused with the read-back when an unconfirmed rule needs the user's yes."""
        expr = " ".join(str(expr).split())
        C.parse(expr)
        start, end = C.as_date(args.get("start_date")), C.as_date(args.get("end_date"))
        if (not C.trivial(expr) or start or end) and args.get("confirmed") is not True:
            verb = "알림 드릴게요" if args.get("notify") == "push" else "전화드릴게요"
            rule = C.rule_text(expr, start, end)
            raise Refused(f"아직 저장하지 않았어요. 사용자에게 '{rule}{'' if rule.endswith('마다') else '에'} {verb}. "
                          "맞죠?'라고 읽어 주고, 맞다고 하면 confirmed=true로 다시 불러 주세요.")
        return expr, start, end

    async def call_user_rule(self, args, reason, kind, pid):
        expr, start, end = self.rule_check(args, args["repeat_cron"])
        body = {"cron": expr, "start_date": start and start.isoformat(), "end_date": end and end.isoformat(),
                "kind": kind, "notify": args.get("notify") or "call", "note": "" if kind == "wake" else reason,
                "request": reason, **({"at": args["when"]} if args.get("when") else {}),
                **({"persona_id": pid} if pid else {})}
        item, say = await self.d["schedule_create"](body, "voice")
        await self.log_call(reason, item.get("at"), pid)
        return say

    async def pick(self, args, soonest):
        """(item or None, message or None) the way cancel and update choose: id, a time (an item's next time or one of
        its rule's fires), a description, else the soonest (cancel) or the only one (update)."""
        items = await self.d["schedule_items"]()
        if args.get("id"):
            hits = [i for i in items if i["id"] == args["id"]]
        elif args.get("at"):
            t = datetime.fromisoformat(args["at"])
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
            return None, "그런 예약이 없어요." if args else "예약이 없어요."
        return None, "어느 것인지 골라 주세요: " + "; ".join(f"{i['spoken']}(id {i['id']})" for i in hits[:5])

    async def schedule_update(self, args):
        item, msg = await self.pick(args, soonest=False)
        if msg:
            return msg
        changes = {k: args[k] for k in ("when", "purpose", "notify", "repeat_cron") if args.get(k)}
        if args.get("when_text"):  # a bare hour keeps this item's (or this fire's) 오전/오후 and day
            base = datetime.fromisoformat(args["at"] if args.get("at") and args.get("scope") == "occurrence"
                                          else item["at"])
            changes["when"] = self.heard(args["when_text"], base, item.get("kind", "reminder"), "바꾸지")["at"].isoformat()
        changes.update({k: args[k] for k in ("start_date", "end_date") if k in args})
        if not changes:
            return "무엇을 바꿀지 말씀해 주세요."
        if changes.get("when"):
            try:
                datetime.fromisoformat(changes["when"])
            except ValueError:
                raise Refused("when은 2026-11-23T18:00:00+09:00 같은 ISO 시각이에요. 반복 규칙은 repeat_cron에 주세요.")
        scope = args.get("scope")
        if rule_of(item) and scope not in ("occurrence", "rule"):
            return ("아직 바꾸지 않았어요. 반복 예약이라 scope가 필요해요. 사용자가 '오늘', '내일', '24일'처럼 특정한 날을 "
                    "말했으면 scope=occurrence(at=그 회차 시각), 말하지 않았으면 scope=rule로 바로 다시 불러요. 둘 다 "
                    "말이 안 될 때만 '이번 한 번만 바꿀까요, 매번 바꿀까요?'라고 물어요.")
        if scope == "occurrence" and {"repeat_cron", "start_date", "end_date"} & set(changes):
            return "한 번만 바꿀 때는 시각, 목적, 알림 방식만 바꿀 수 있어요."
        if changes.get("repeat_cron"):
            expr, start, end = self.rule_check({**args, "notify": args.get("notify") or item.get("notify")},
                                               changes["repeat_cron"])
            changes["repeat_cron"] = expr
        occurrence = None
        if rule_of(item) and scope == "occurrence":
            if not args.get("at"):  # "오늘 2시 건": guessing the next fire (10시) would move the wrong one
                return (f"아직 바꾸지 않았어요. 바꿀 회차의 원래 시각을 at으로 주세요(예: 다음 회차 {item['at']}). "
                        "id나 설명과 함께 줘도 돼요.")
            occurrence = args["at"]
        _, say = await self.d["schedule_update"](item["id"], changes, occurrence)
        return say

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
            item, msg = await self.pick(args, soonest=True)
            if msg:
                return msg
            if not rule_of(item):
                args = {**args, "id": item["id"]}  # a one-off item: cancelling its only occurrence is cancelling it
            else:
                done = await self.d["schedule_cancel"](item["id"], None, occurrence=args.get("at") or item["at"])
                return done[1] if done else "그런 예약이 없어요."
        if args.get("id") or args.get("at"):
            at = datetime.fromisoformat(args["at"]).timestamp() if args.get("at") else None
            done = await self.d["schedule_cancel"](args.get("id"), at)
            return done[1] if done else "그런 예약이 없어요."
        if str(args.get("description") or "").strip():
            item, msg = await self.pick(args, soonest=True)
            if msg:
                return msg
            done = await self.d["schedule_cancel"](item["id"], None)
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


class Refused(Exception):
    """A tool that did nothing on purpose; the message says why (and what the model should ask)."""


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
OWN_TOOLS = (CALL_USER, SCHEDULE_LIST, SCHEDULE_CANCEL, SCHEDULE_UPDATE, CREATE_TASK, TASK_STATUS, APPROVAL_STATUS)
MCP_TOOLS = OWN_TOOLS  # claude -p already has the daemons, Cygnus and the claude.ai connectors as its own MCP servers
# The external token (the user's own Claude Code, later claude.ai): ring, schedule and hand off work, nothing personal.
SCOPES = {"full": {t["name"] for t in MCP_TOOLS},
          "external": {"call_user", "schedule_list", "schedule_cancel", "schedule_update", "create_task", "task_status"}}


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
