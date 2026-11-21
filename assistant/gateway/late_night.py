"""Spoken times, resolved deterministically: day words late at night, and bare hours ("6시").

Day words late at night. Between 00:00 and 05:00 KST the user hasn't slept yet, so "내일" is the coming daytime:
the same calendar day ("모레" and "글피" shift by one too). After 05:00 the words mean what they say.

One function for every path: call_user/schedule, the voice fast path, calendar questions and tasks (prompt_rule).
The voice image ships single files, so assistant/spark/late_night.py is a byte-identical copy;
gateway/tests/test_late_night.py fails when the two differ.
"""
import re
from datetime import datetime, time, timedelta

LATE_END = 5  # the night counts as the day before until 05:00 KST
DAY_WORDS = (("글피", 3), ("모레", 2), ("내일", 1), ("오늘", 0))


def is_late(now):
    return now.hour < LATE_END


def day_word(text):
    """(word, days ahead as said) for the first day word in the text, else (None, None)."""
    return next(((w, add) for w, add in DAY_WORDS if w in text), (None, None))


def day_for(add, now):
    """The calendar date a day word means right now: at night "내일" (add=1) is today."""
    return now.date() + timedelta(days=add - (1 if is_late(now) and add > 0 else 0))


def clock_ko(hour, minute):
    part = "새벽" if hour < 6 else "아침" if hour < 10 else "오전" if hour < 12 else "낮" if hour == 12 else \
        "오후" if hour < 18 else "저녁" if hour < 21 else "밤"
    return f"{part} {hour % 12 or 12}시" + (f" {minute}분" if minute else "")


def date_ko(d, now):
    return f"오늘({d.month}월 {d.day}일)" if d == now.date() else f"{d.month}월 {d.day}일"


def resolve(word, add, hour, minute, now):
    """A day word plus a clock time: {"at", "ask", "say", "note"}.

    at:   the KST datetime to use, or None when the user must choose first;
    ask:  the one question to ask instead of creating anything (late night, a morning time: after waking, or the
          day after?). A morning time also covers "within the hour", since at night the shifted date is today;
    say:  what to tell the user when the night rule moved the date; note: the interpretation to store on the item.
    """
    literal = datetime.combine(now.date() + timedelta(days=add), time(hour, minute), now.tzinfo)
    if not is_late(now) or add == 0:
        return {"at": literal, "ask": None, "say": None, "note": None}
    shifted = literal - timedelta(days=1)
    clock = clock_ko(hour, minute)
    yo, ro = ("이요", "으로") if minute else ("요", "로")  # "30분이요", "30분으로" after the final consonant
    if hour < 12:
        first = "오늘" if shifted.date() == now.date() else f"{shifted.day}일"
        return {"at": None, "ask": f"{first} {clock}{yo}, 아니면 {literal.day}일 {clock}{yo}?", "say": None, "note": None}
    return {"at": shifted, "ask": None,
            "say": f"{date_ko(shifted.date(), now)} {clock}{ro} 잡았어요. 주무시고 일어난 뒤로 이해했어요. 다르면 말씀해 주세요.",
            "note": f"새벽 '{word}' → {'오늘' if shifted.date() == now.date() else f'{shifted.day}일'}로 해석"}


def prompt_rule(now):
    """One system-prompt paragraph for the model paths (voice, Claude, API/NIM tasks); empty after 05:00."""
    if not is_late(now):
        return ""
    today, tomorrow = now.date(), now.date() + timedelta(days=1)
    return (f"\n지금은 새벽 {now.hour}시({today.month}월 {today.day}일)예요. 사용자는 아직 안 잤다고 보고, '내일'은 "
            f"오늘({today.month}월 {today.day}일), '모레'는 {tomorrow.month}월 {tomorrow.day}일로 이해해요(새벽 5시까지). "
            "일정·메일·할 일 질문의 '내일'도 오늘 날짜로 찾고, 답할 때 날짜를 말해요. "
            f"'내일' 오후 시각의 전화·예약은 오늘로 잡고 \"오늘({today.month}월 {today.day}일) 오후 1시로 잡았어요. "
            "주무시고 일어난 뒤로 이해했어요. 다르면 말씀해 주세요.\"처럼 말해요. "
            f"'내일' 오전(12시 전) 시각이면 아무것도 만들지 말고 \"오늘 아침 7시요, 아니면 {tomorrow.day}일 아침 7시요?\"처럼 "
            "한 번만 물어요.\n")


# ---------- a whole spoken time ("내일 오후 1시", "6시로", "30분 뒤", "금요일 3시 반") ----------
KO_NUM = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
          "열한": 11, "열두": 12, "삼십": 30, "십": 10, "이십": 20, "사십": 40, "오십": 50}
_N = r"(\d{1,2}|열한|열두|열|한|두|세|네|다섯|여섯|일곱|여덟|아홉|삼십|이십|사십|오십|십)"
REL_RE = re.compile(_N + r"\s*(분|시간)\s*(뒤|후|있다가|지나서)")
CLOCK_RE = re.compile(r"(오전|오후|아침|저녁|밤|새벽|낮|점심)?\s*" + _N + r"\s*시(?!간)\s*(반|" + _N + r"\s*분)?")
WEEKDAY_RE = re.compile(r"(다음\s*주|담주|이번\s*주)?\s*([월화수목금토일])요일")
DAYS = "월화수목금토일"


def ko_int(tok):
    return int(tok) if tok.isdigit() else KO_NUM[tok]


def _hours(part, h, inherit, kind):
    """The 24-hour readings of a clock hour: one when it is clear, two when both are plausible."""
    if part in ("오후", "저녁", "낮", "점심"):
        return [h + 12 if h < 12 and not (part in ("낮", "점심") and h >= 11) else h % 24]
    if part == "밤":  # 밤 1시 is the coming night's 01:00; 밤 12시 is midnight
        return [0 if h == 12 else h if h <= 4 else h + 12 if h < 12 else h % 24]
    if part in ("오전", "아침", "새벽"):
        return [h % 12]
    if h == 0 or h >= 13:
        return [h % 24]
    if h == 12:
        return [12]  # bare 12시 is noon; midnight is said 밤 12시 or 자정
    if inherit is not None:
        return [h if inherit.hour < 12 else h + 12]  # "오후 10시 알림 6시로": the item's own 오후
    if kind == "wake":
        return [h] if h >= 4 else [h + 12]
    return [h + 12] if h <= 5 else [h, h + 12]  # 1–5시 is afternoon; 6–11시 could be either


def _ask_two(day_label, hours, minute):
    a, b = (clock_ko(x, minute) for x in hours)
    lead = f"{day_label} " if day_label else ""
    return f"{lead}{a}{'이요' if minute else '요'}, {b}{'이요' if minute else '요'}?"


def when_from_words(text, now, inherit=None, kind="reminder"):
    """A spoken time -> {"at", "ask", "say", "note"} like resolve(): at is None when the user must choose first.

    inherit: the item being changed (schedule_update), whose 오전/오후 and date a bare hour keeps.
    Without it a bare hour takes the nearest future reading (call_user); with a day word or weekday and two plausible
    readings ("내일 7시") it asks. A time that has already passed asks too. The late-night day rule applies throughout.
    """
    text = str(text or "")
    done = {"ask": None, "say": None, "note": None}
    if m := REL_RE.search(text):
        n = ko_int(m.group(1))
        return {"at": (now + (timedelta(minutes=n) if m.group(2) == "분" else timedelta(hours=n))).replace(
            second=0, microsecond=0), **done}
    m = CLOCK_RE.search(text)
    if not m:
        return {**done, "at": None, "ask": "몇 시로 할까요?"}
    part, h = m.group(1), ko_int(m.group(2))
    if part is None:  # "내일 7시 … 저녁" or an answer appended later ("내일 7시에 전화해줘 저녁"): the part said apart
        rest = text[:m.start()] + " " + text[m.end():]
        if p := re.search(r"(오전|오후|아침|저녁|밤|새벽|낮|점심)", rest):
            part = p.group(1)
    minute = 30 if m.group(3) == "반" else ko_int(m.group(4)) if m.group(4) else 0
    if h > 24 or minute > 59:
        return {**done, "at": None, "ask": "몇 시인지 다시 말씀해 주세요."}
    hours = _hours(part, h, inherit, kind)
    word, add = day_word(text)
    wd = WEEKDAY_RE.search(text)

    def on(d, hh):
        return datetime.combine(d, time(hh, minute), now.tzinfo)
    if len(hours) == 2 and (word or wd):  # a reading already past on the named day drops out ("오늘 7시" at 9:00)
        d = day_for(add, now) if word else None
        if wd and not word:
            want = DAYS.index(wd.group(2))
            if not wd.group(1) and want == now.weekday():
                d = now.date()
        if d is not None:
            left = [hh for hh in hours if on(d, hh) > now]
            hours = left if len(left) == 1 else hours
    if word:
        if len(hours) == 2:
            return {**done, "at": None, "ask": _ask_two(date_ko(day_for(add, now), now), hours, minute)}
        r = resolve(word, add, hours[0], minute, now)
        if r["at"] is not None and r["at"] <= now:
            return {**done, "at": None, "ask": f"{clock_ko(hours[0], minute)}는 이미 지났어요. 언제로 할까요?"}
        return r
    if wd:
        want = DAYS.index(wd.group(2))
        nxt = wd.group(1) and ("다음" in wd.group(1) or wd.group(1) == "담주")
        d = now.date() + timedelta(days=(7 - now.weekday() + want) if nxt else (want - now.weekday()) % 7)
        if len(hours) == 2:
            return {**done, "at": None, "ask": _ask_two(f"{d.month}월 {d.day}일", hours, minute)}
        at = on(d, hours[0])
        if at <= now and nxt:
            return {**done, "at": None, "ask": f"{clock_ko(hours[0], minute)}는 이미 지났어요. 언제로 할까요?"}
        return {**done, "at": at if at > now else at + timedelta(days=7)}  # "월요일 9시" said at 10:00 Monday
    if inherit is not None:  # keep the item's day
        at = on(inherit.date(), hours[0])
        if at <= now:
            return {**done, "at": None,
                    "ask": f"{date_ko(at.date(), now)} {clock_ko(hours[0], minute)}는 이미 지났어요. 언제로 할까요?"}
        return {**done, "at": at}
    future = sorted(t for k in (0, 1) for hh in hours if (t := on(now.date() + timedelta(days=k), hh)) > now)
    return {**done, "at": future[0]}
