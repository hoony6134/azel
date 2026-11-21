"""Recurring schedules as 5-field cron in KST: parse, next fire, Korean read-back, and the limits.

No croniter in the dependencies, so a minimal parser: `*`, lists, ranges and steps (`*/n`, `a-b/n`) for minute, hour,
day of month, month and day of week (0 or 7 = Sunday). No names, L, W or #. When both day of month and day of week are
restricted, either may match (standard cron).
"""
from datetime import date, datetime, time, timedelta

FIELDS = (("분", 0, 59), ("시", 0, 23), ("일", 1, 31), ("월", 1, 12), ("요일", 0, 7))
MIN_GAP_S = 30 * 60
DAY_CAP = 20  # user-requested calls a day, as tools_bridge.CALL_CAP
HORIZON_DAYS = 14
DOW = "일월화수목금토"


class CronError(ValueError):
    """The Korean reason a rule can't be used; safe to speak."""


def _field(text, lo, hi, name):
    out = set()
    for part in text.split(","):
        rng, _, step = part.partition("/")
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = (int(x) for x in rng.split("-", 1))
        else:
            a = b = int(rng)
        n = int(step) if step else 1
        if not (lo <= a <= b <= hi) or n < 1 or (step and rng != "*" and "-" not in rng):
            raise CronError(f"{name} 값이 맞지 않아요.")
        out.update(range(a, b + 1, n))
    return out


def parse(expr):
    """{"min", "hour", "dom", "month", "dow", "dom_any", "dow_any"}; CronError when malformed."""
    parts = str(expr or "").split()
    if len(parts) != 5:
        raise CronError("반복 규칙을 이해하지 못했어요.")
    try:
        sets = [_field(p, lo, hi, name) for p, (name, lo, hi) in zip(parts, FIELDS)]
    except ValueError as e:
        raise e if isinstance(e, CronError) else CronError("반복 규칙을 이해하지 못했어요.")
    dow = {d % 7 for d in sets[4]}
    return {"min": sorted(sets[0]), "hour": sorted(sets[1]), "dom": sets[2], "month": sets[3], "dow": dow,
            "dom_any": parts[2] == "*", "dow_any": parts[4] == "*"}


def _day_ok(c, d):
    if d.month not in c["month"]:
        return False
    dom, dow = d.day in c["dom"], (d.weekday() + 1) % 7 in c["dow"]
    if not c["dom_any"] and not c["dow_any"]:
        return dom or dow
    return dom and dow


def fires(expr, after, until, start=None, end=None, skips=()):
    """Every fire time in (after, until], KST-aware like `after`."""
    c, skip = parse(expr), {int(s) for s in skips}
    d = after.date()
    while d <= until.date():
        if (start is None or d >= start) and (end is None or d <= end) and _day_ok(c, d):
            for h in c["hour"]:
                for m in c["min"]:
                    t = datetime.combine(d, time(h, m), after.tzinfo)
                    if after < t <= until and int(t.timestamp()) not in skip:
                        yield t
        d += timedelta(days=1)


def next_fire(expr, after, start=None, end=None, skips=()):
    """The first fire after `after`, or None (past the end date, or no such day within ~4 years)."""
    return next(fires(expr, after, after + timedelta(days=366 * 4 + 1), start, end, skips), None)


def legacy(repeat, at):
    """The old repeat values as cron, at the time of day of `at` (a KST datetime)."""
    if not repeat:
        return None
    base = f"{at.minute} {at.hour} * *"
    if repeat == "daily":
        return base + " *"
    if repeat == "weekdays":
        return base + " 1-5"
    return base + f" {(int(repeat.split(':')[1]) + 1) % 7}"  # weekly:0 is Monday, cron 1 is Monday


def trivial(expr):
    """One time of day, every day of month and month: said back without asking."""
    p = expr.split()
    c = parse(expr)
    return len(c["min"]) == 1 and len(c["hour"]) == 1 and p[2] == "*" and p[3] == "*"


def clock(h, m):
    return f"{'오전' if h < 12 else '오후'} {h % 12 or 12}시" + (f" {m}분" if m else "")


def _days(c, parts):
    out = []
    if parts[3] != "*":
        out.append("·".join(f"{x}월" for x in sorted(c["month"])))
    if not c["dom_any"]:
        out.append(("" if parts[3] != "*" else "매월 ") + "·".join(f"{x}일" for x in sorted(c["dom"])))
    if not c["dow_any"]:
        dow = c["dow"]
        names = "평일" if dow == {1, 2, 3, 4, 5} else "주말" if dow == {0, 6} else \
            "매주 " + "·".join(f"{DOW[x]}요일" for x in sorted(dow, key=lambda x: (x + 6) % 7))
        out.append(names if c["dom_any"] else f"또는 {names}")
    return " ".join(out) or "매일"


def _times(c):
    hs, ms = c["hour"], c["min"]
    if len(hs) == 24 and len(ms) > 1 and len({b - a for a, b in zip(ms, ms[1:])} | {60 - ms[-1] + ms[0]}) == 1:
        return f"{ms[1] - ms[0]}분마다"
    if len(ms) == 1 and len(hs) > 2 and len({b - a for a, b in zip(hs, hs[1:])}) == 1:
        step = hs[1] - hs[0]
        return f"{clock(hs[0], ms[0])}부터 {clock(hs[-1], ms[0])}까지 {step}시간마다" if step > 1 or len(hs) < 24 \
            else "매시간"
    times = [clock(h, m) for h in hs for m in ms]
    return "·".join(times) if len(times) <= 4 else f"하루 {len(times)}번"


def rule_text(expr, start=None, end=None):
    """'평일 오전 10시부터 오후 6시까지 2시간마다', '화요일·목요일 오후 2시', '매월 1일 오전 9시'."""
    c, parts = parse(expr), expr.split()
    days, times = _days(c, parts), _times(c)
    text = times if days == "매일" and (times.endswith("분마다") or times == "매시간") else f"{days} {times}"
    span = (f"{start.month}월 {start.day}일부터 " if start else "") + (f"{end.month}월 {end.day}일까지 " if end else "")
    return span + text


def check(expr, now, start=None, end=None, others=()):
    """Refuse a rule that can't or shouldn't run: raises CronError with the reason to say.
    others: the active rules' upcoming fire datetimes (one-off items included) for the daily cap."""
    if start and end and end < start:
        raise CronError("끝나는 날이 시작하는 날보다 빨라요.")
    until = now + timedelta(days=HORIZON_DAYS)
    own = list(fires(expr, now, until, start, end))
    if not own and next_fire(expr, now, start, end) is None:
        raise CronError("그 규칙으로는 전화할 날이 없어요.")
    if any((b - a).total_seconds() < MIN_GAP_S for a, b in zip(own, own[1:])):
        raise CronError("전화 사이가 30분은 넘어야 해요.")
    per_day = {}
    for t in [*own, *(o for o in others if now < o <= until)]:
        per_day[t.date()] = per_day.get(t.date(), 0) + 1
    if per_day and max(per_day.values()) > DAY_CAP:
        raise CronError(f"하루 전화가 {DAY_CAP}번을 넘게 돼요.")


def as_date(value):
    """'2026-11-30' -> date; None stays None; anything else is a CronError."""
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise CronError("날짜는 2026-11-30처럼 써 주세요.")


WORDS_PROMPT = (
    "사용자가 반복 예약을 부탁했어. 5칸 cron(KST, 분 시 일 월 요일; 요일 0=일요일 1=월요일 … 6=토요일)으로 바꿔 JSON 하나만 출력해. "
    '형식: {"cron": "0 7 * * *", "start_date": null|"YYYY-MM-DD", "end_date": null|"YYYY-MM-DD", '
    '"kind": "wake"|"reminder", "purpose": "짧은 명사구 또는 빈 문자열"}. '
    "규칙: cron은 언제나 공백으로 나뉜 정확히 5칸. 쓸 수 있는 것은 숫자, *, 쉼표 목록, a-b 범위, */n, a-b/n뿐. "
    "예: '매일 아침 7시' = \"0 7 * * *\", '화목 2시' = \"0 14 * * 2,4\", '매월 1일' = \"0 9 1 * *\", "
    "'평일 10시부터 6시까지 2시간마다' = \"0 10-18/2 * * 1-5\", '주말 8시 반' = \"30 8 * * 0,6\". 오전·새벽 없이 1~6시라고만 하면 오후(13~18시). 시각이 없으면 오전 9시. "
    "'깨워'는 wake, 나머지는 reminder. purpose에는 '-하라고', '전화해줘', '알려줘' 같은 어미를 빼고 목적만. 다른 말은 쓰지 마.")


async def from_words(text, llm, now, others=()):
    """A spoken recurring request as a checked rule: {cron, start_date, end_date, notify, kind, purpose, rule_text,
    confirm, say}. Qwen proposes; parse() and check() decide. CronError carries the reason to say."""
    from late_night import prompt_rule  # the same night rule as one-off calls ("내일부터" at 01:00)
    system = WORDS_PROMPT + f"\n지금: {now:%Y-%m-%d %H:%M} ({DOW[(now.weekday() + 1) % 7]}요일)" + prompt_rule(now)
    d, expr, user = None, "", str(text)[:300]
    for _ in range(2):  # one retry, told what was wrong ("0 10-18/2 1-5": three fields)
        try:
            d = await llm(system, user)
        except Exception:  # noqa: BLE001
            d = None
        expr = " ".join(str((d or {}).get("cron") or "").split()) if isinstance(d, dict) else ""
        try:
            parse(expr)
            break
        except CronError:
            user = f"{text}\n(앞의 답 {expr!r}은 5칸 cron이 아니었어. 정확히 5칸으로 다시.)"[:400]
    else:
        raise CronError("반복 규칙을 이해하지 못했어요. 다시 말씀해 주세요.")
    start, end = as_date(d.get("start_date")), as_date(d.get("end_date"))
    check(expr, now, start, end, others)
    notify = "push" if any(w in str(text) for w in ("알림으로", "푸시", "알림만")) else "call"
    kind = "wake" if d.get("kind") == "wake" and notify == "call" else "reminder"
    purpose = str(d.get("purpose") or "").strip()[:60]
    purpose = "" if purpose in ("알림", "전화", "연락", "기상", "알람") else purpose  # the call itself, not a purpose
    rule = rule_text(expr, start, end)
    verb = "알림 드릴게요" if notify == "push" else "깨워 드릴게요" if kind == "wake" else "전화드릴게요"
    confirm = not trivial(expr) or bool(start or end)
    say = f"{rule}에 {verb}." if not confirm else f"{rule}{'' if rule.endswith('마다') else '에'} {verb}. 맞죠?"
    return {"cron": expr, "start_date": start, "end_date": end, "notify": notify, "kind": kind, "purpose": purpose,
            "rule_text": rule, "confirm": confirm, "say": say}
