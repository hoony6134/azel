"""What the autonomous-call judge reads: dated call summaries, only live approvals, a dated checkpoint."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

from test_gateway import gw  # noqa: F401  (fixture)

KST = timezone(timedelta(hours=9))


def ts(day, h, m=0):
    return datetime(2026, 11, day, h, m, tzinfo=KST).timestamp()


def test_call_summaries_say_the_real_day(gw):  # noqa: F811
    g, _, _ = gw
    saved = datetime(2026, 11, 22, 19, 50, tzinfo=KST).isoformat()
    assert g.relabel_day("오늘 19:48 통화: 토론지 얘기", saved, ts(23, 9)) == "어제 19:48 통화: 토론지 얘기"
    assert g.relabel_day("오늘 19:48 통화", saved, ts(22, 21)) == "오늘 19:48 통화"
    assert g.relabel_day("어제 08:00 통화", saved, ts(23, 9)) == "그저께 08:00 통화"
    assert g.relabel_day("오늘 19:48 통화", saved, ts(26, 9)) == "11월 22일 19:48 통화"
    assert g.relabel_day("오늘 19:48 통화", None, ts(23, 9)) == "오늘 19:48 통화"  # no time: unchanged
    g.fake.db["context"]["call-followup-c1"] = {"task_id": "call-followup-c1", "summary": "오늘 19:44 통화: 과제 마감",
                                                "open_questions": ["제출했는지"], "updated": saved}
    sig = asyncio.run(g.gather_signals(ts(23, 9)))
    assert any("어제 19:44 통화" in t for t in sig["tasks"]), sig["tasks"]


def test_expired_approvals_leave_the_signals_and_are_marked(gw):  # noqa: F811
    g, _, _ = gw
    now = time.time()
    g.fake.db["approvals"]["old"] = {"id": "old", "device": "macbook", "tool": "run_command", "command": "uptime",
                                     "cwd": "", "status": "pending", "created": now - 338 * 60, "decided": None}
    g.fake.db["approvals"]["new"] = {"id": "new", "device": "macbook", "tool": "run_command", "command": "ls",
                                     "cwd": "", "status": "pending", "created": now - 150, "decided": None}
    sig = asyncio.run(g.gather_signals(now))
    assert not any("uptime" in a for a in sig["approvals"]), sig["approvals"]  # 338 min old: not "waiting"
    assert g.fake.db["approvals"]["old"]["status"] == "expired"  # and no longer pending in state
    assert any("ls" in a and "만료" in a for a in sig["approvals"])  # a live one: how long it has left
    assert g.fake.db["approvals"]["new"]["status"] == "pending"


def test_checkpoint_prompt_is_dated_with_the_night_rule(gw):  # noqa: F811
    g, _, _ = gw
    p = g.checkpoint_prompt(datetime(2026, 11, 23, 8, 30, tzinfo=KST))
    assert "2026년 11월 23일 월요일 08:30" in p and "새벽" not in p
    night = g.checkpoint_prompt(datetime(2026, 11, 23, 1, 10, tzinfo=KST))
    assert "새벽 1시" in night and "'내일'은 오늘(11월 23일)" in night


def test_the_opening_says_why_now(gw):  # noqa: F811
    g, _, _ = gw
    assert "왜 지금 거는지" in g.DECIDE_PROMPT
