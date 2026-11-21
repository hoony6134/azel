"""The late-night day rule over a table of times × phrases; the spark copy must stay identical."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

GW = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GW))
import late_night as L  # noqa: E402

KST = timezone(timedelta(hours=9))


def at(day, h, m=0):
    return datetime(2026, 11, day, h, m, tzinfo=KST)


# now, phrase (day word + clock already parsed), expected at (None = ask), expected question or spoken text
TABLE = [
    (at(23, 0, 30), "내일", 13, 0, at(23, 13), "오늘(11월 23일) 오후 1시로 잡았어요. 주무시고 일어난 뒤로 이해했어요. 다르면 말씀해 주세요."),
    (at(23, 1), "내일", 7, 0, None, "오늘 아침 7시요, 아니면 24일 아침 7시요?"),
    (at(23, 1), "내일", 1, 30, None, "오늘 새벽 1시 30분이요, 아니면 24일 새벽 1시 30분이요?"),  # within the hour
    (at(23, 4, 59), "내일", 11, 0, None, "오늘 오전 11시요, 아니면 24일 오전 11시요?"),
    (at(23, 2), "내일", 12, 0, at(23, 12), "오늘(11월 23일) 낮 12시로 잡았어요."),
    (at(23, 3), "내일", 19, 30, at(23, 19, 30), "오늘(11월 23일) 저녁 7시 30분으로 잡았어요."),
    (at(23, 1), "모레", 15, 0, at(24, 15), "11월 24일 오후 3시로 잡았어요."),
    (at(23, 1), "모레", 8, 0, None, "24일 아침 8시요, 아니면 25일 아침 8시요?"),
    (at(23, 1), "글피", 14, 0, at(25, 14), "11월 25일 오후 2시로 잡았어요."),
    (at(23, 1), "오늘", 23, 0, at(23, 23), None),  # "오늘 밤" at 01:00 is tonight
    (at(23, 5), "내일", 13, 0, at(24, 13), None),  # 05:00 on: words mean what they say
    (at(23, 5), "내일", 7, 0, at(24, 7), None),
    (at(22, 23, 59), "내일", 7, 0, at(23, 7), None),
    (at(23, 12), "모레", 9, 0, at(25, 9), None),
]


@pytest.mark.parametrize("now,word,hour,minute,want,text", TABLE)
def test_resolve_table(now, word, hour, minute, want, text):
    _, add = L.day_word(f"{word} 전화해 줘")
    r = L.resolve(word, add, hour, minute, now)
    assert r["at"] == want
    if want is None:
        assert r["ask"] == text and r["say"] is None and r["note"] is None
    else:
        assert r["ask"] is None
        if text:
            assert r["say"].startswith(text) and r["note"].startswith(f"새벽 '{word}' → ")
        else:
            assert r["say"] is None and r["note"] is None


def test_clear_case_note():
    assert L.resolve("내일", 1, 13, 0, at(23, 0, 30))["note"] == "새벽 '내일' → 오늘로 해석"


def test_day_for_calendar_questions():
    assert L.day_for(1, at(23, 1)) == at(23, 0).date()  # "내일 일정" at 01:00: today
    assert L.day_for(0, at(23, 1)) == at(23, 0).date()
    assert L.day_for(2, at(23, 1)) == at(24, 0).date()
    assert L.day_for(1, at(23, 5)) == at(24, 0).date()


def test_prompt_rule_only_at_night():
    assert L.prompt_rule(at(23, 5)) == "" and L.prompt_rule(at(22, 23, 59)) == ""
    rule = L.prompt_rule(at(23, 1))
    assert "새벽 1시(11월 23일)" in rule and "'모레'는 11월 24일" in rule and "24일 아침 7시요?" in rule


def test_day_word():
    assert L.day_word("내일모레 3시") == ("모레", 2) and L.day_word("3시") == (None, None)


def test_spark_copy_is_identical():
    assert (GW.parent / "spark" / "late_night.py").read_bytes() == (GW / "late_night.py").read_bytes()
