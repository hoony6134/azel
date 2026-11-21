"""Day words late at night. Between 00:00 and 05:00 KST the user hasn't slept yet, so "내일" is the coming daytime:
the same calendar day ("모레" and "글피" shift by one too). After 05:00 the words mean what they say.

One function for every path: call_user/schedule, the voice fast path, calendar questions and tasks (prompt_rule).
The voice image ships single files, so assistant/spark/late_night.py is a byte-identical copy;
gateway/tests/test_late_night.py fails when the two differ.
"""
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
