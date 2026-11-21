"""The minimal cron: parsing, next fires, the Korean read-back, legacy repeats and the limits."""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cron as C  # noqa: E402

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 22, 23, 0, tzinfo=KST)  # a Sunday


def at(month, day, h, m=0):
    return datetime(2026, month, day, h, m, tzinfo=KST)


@pytest.mark.parametrize("expr,text", [
    ("0 7 * * *", "매일 오전 7시"),
    ("0 21 * * *", "매일 오후 9시"),
    ("0 14 * * 2,4", "화요일·목요일 오후 2시"),
    ("0 10-18/2 * * 1-5", "평일 오전 10시부터 오후 6시까지 2시간마다"),
    ("0 9 1 * *", "매월 1일 오전 9시"),
    ("30 8 * * 0,6", "주말 오전 8시 30분"),
    ("0 9,13 * * 0", "일요일 오전 9시·오후 1시"),
    ("*/30 * * * *", "30분마다"),
    ("0 * * * 1-5", "평일 매시간"),
    ("0 12 25 12 *", "12월 25일 오후 12시"),
])
def test_rule_text(expr, text):
    assert C.rule_text(expr) == text


def test_rule_text_dates():
    assert C.rule_text("0 7 * * *", date(2026, 12, 1), date(2026, 12, 31)) == \
        "12월 1일부터 매일 오전 7시 (12월 31일까지)"


@pytest.mark.parametrize("expr,want", [
    ("0 7 * * *", at(11, 23, 7)),
    ("0 14 * * 2,4", at(11, 24, 14)),
    ("0 10-18/2 * * 1-5", at(11, 23, 10)),
    ("0 9 1 * *", at(12, 1, 9)),
    ("0 7 * * 7", at(11, 29, 7)),  # 7 is Sunday too
    ("0 9 1 * 1", at(11, 23, 9)),  # dom and dow both restricted: either matches
    ("0 0 31 2 *", None),
])
def test_next_fire(expr, want):
    assert C.next_fire(expr, NOW) == want


def test_next_fire_bounds_and_skips():
    assert C.next_fire("0 7 * * *", NOW, start=date(2026, 12, 1)) == at(12, 1, 7)
    assert C.next_fire("0 7 * * *", NOW, end=date(2026, 11, 22)) is None
    skip = int(at(11, 23, 7).timestamp())
    assert C.next_fire("0 7 * * *", NOW, skips=[skip]) == at(11, 24, 7)
    assert C.next_fire("0 10-18/2 * * 1-5", at(11, 23, 10)) == at(11, 23, 12)


@pytest.mark.parametrize("bad", ["", "0 7 * *", "60 7 * * *", "0 24 * * *", "0 7 0 * *", "0 7 * 13 *", "0 7 * * 8",
                                 "a 7 * * *", "0 7-5 * * *", "0 */0 * * *", "5/10 * * * *"])
def test_malformed(bad):
    with pytest.raises(C.CronError):
        C.parse(bad)


def test_legacy_repeats():
    t = at(11, 23, 6, 40)
    assert C.legacy("daily", t) == "40 6 * * *" and C.legacy("weekdays", t) == "40 6 * * 1-5"
    assert C.legacy("weekly:0", t) == "40 6 * * 1" and C.legacy("weekly:6", t) == "40 6 * * 0"
    assert C.legacy(None, t) is None
    assert C.next_fire(C.legacy("weekly:6", t), NOW) == at(11, 29, 6, 40)  # weekly:6 is Sunday


def test_trivial():
    assert C.trivial("0 7 * * *") and C.trivial("0 14 * * 2,4")
    assert not C.trivial("0 10-18/2 * * 1-5") and not C.trivial("0 9 1 * *")


def test_limits():
    C.check("0 10-18/2 * * 1-5", NOW)
    C.check("*/30 9-12 * * *", NOW)  # exactly 30 minutes apart is fine
    with pytest.raises(C.CronError, match="30분"):
        C.check("*/15 9 * * *", NOW)
    with pytest.raises(C.CronError, match="30분"):
        C.check("0,20 9 * * *", NOW)
    with pytest.raises(C.CronError, match="20번"):
        C.check("0,30 * * * *", NOW)  # 48 a day
    others = list(C.fires("0 0-17 * * *", NOW, NOW + timedelta(days=14)))  # 18 a day already
    C.check("0 20,22 * * *", NOW, others=others)  # 20: fine
    with pytest.raises(C.CronError, match="20번"):
        C.check("0 19,21,23 * * *", NOW, others=others)
    with pytest.raises(C.CronError, match="날이 없어요"):
        C.check("0 0 31 2 *", NOW)
    with pytest.raises(C.CronError, match="시작하는 날"):
        C.check("0 7 * * *", NOW, date(2026, 12, 2), date(2026, 12, 1))


def test_as_date():
    assert C.as_date("2026-11-30") == date(2026, 11, 30) and C.as_date(None) is None
    with pytest.raises(C.CronError):
        C.as_date("다음 주")
