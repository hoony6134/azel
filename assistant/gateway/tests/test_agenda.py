"""agenda: Google Calendar and Gistra for one range, merged (no real network: Google is a MockTransport, Gistra a fake)."""
import asyncio
from datetime import date as Date, datetime

import httpx
import pytest

import agenda
import google_tools as GT
from test_gateway import start
from test_google_tools import FakeGoogle, setup

GISTRA = """스냅샷: 11월 22일 (일) 14:08

## 일정 2026-11-23 ~ 2026-11-24 (3개)

### 11월 23일 (월)
- 09:00~10:15 수업 컴파일러 (CS401) — 오룡관 101 [cl-1a2b]
- 20:00~22:00 고정 지온나래 정기회의 (정기 회의) — 지온나래 · 구관 토론실 [re-81fc2580bb0388ec]
- 23:59 마감 할 일 컴파일러 프로젝트 [t-fed016d602be]
"""


def calendar_via(tmp, fake):
    async def calendar(f, t):
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as c:
            return await GT.events(tmp, f, t, client=c)
    return calendar


def gistra_text(text):
    async def gistra(f, t):
        return text
    return gistra


def test_both_sources_merged_by_day_and_time_with_duplicates_dropped(tmp_path):
    setup(tmp_path)
    out = asyncio.run(agenda.merged("2026-11-23", "2026-11-24", calendar_via(tmp_path, FakeGoogle()), gistra_text(GISTRA)))
    lines = out.splitlines()
    assert lines[1:] == ["### 11/23(월)",
                         "- 09:00~10:15 수업 컴파일러 (CS401) — 오룡관 101",
                         "- 20:00~22:00 고정 지온나래 정기회의 (정기 회의) — 지온나래 · 구관 토론실",
                         "- 23:59 마감 할 일 컴파일러 프로젝트",
                         "### 11/24(화)",
                         "- 종일 생일 (기본 캘린더)"], out
    assert "구관 토론실 (기본 캘린더)" not in out  # the calendar's copy of the Gistra meeting is dropped
    assert "SECRET" not in out and "취소됨" not in out and "[t-" not in out


def test_a_failing_source_is_named_and_the_other_still_answers(tmp_path):
    setup(tmp_path, expired=True)  # Google expired: Gistra alone, and it says so
    out = asyncio.run(agenda.merged("2026-11-23", "2026-11-23", calendar_via(tmp_path, FakeGoogle()), gistra_text(GISTRA)))
    assert "수업 컴파일러" in out and "구글 캘린더는 지금 못 읽었어요: Google 연결이 없거나 만료됐어요" in out
    setup(tmp_path)
    out = asyncio.run(agenda.merged("2026-11-23", "2026-11-24", calendar_via(tmp_path, FakeGoogle()),
                                    gistra_text("지금은 Cygnus를 볼 수 없어요. (ConnectError)")))
    assert "20:00~22:00 지온나래 정기회의 @ 구관 토론실 (기본 캘린더)" in out and "Gistra는 지금 못 읽었어요" in out

    async def down(f, t):
        raise httpx.ConnectError("no route")
    out = asyncio.run(agenda.merged("2026-11-23", "2026-11-23", down, down))
    assert "- " not in out and "읽지 못했어요" in out  # nothing to say: no event is made up


def test_ranges_and_gistra_days_across_new_year():
    with pytest.raises(ValueError):
        agenda.span("2026-11-01", "2026-12-15")
    with pytest.raises(ValueError):
        agenda.span("2026-11-02", "2026-11-01")
    rows = agenda.parse_gistra("### 12월 31일 (목)\n- 종일 학사 종강\n### 1월 1일 (금)\n- 10:00 할 일 새해 계획",
                               Date(2026, 12, 30), Date(2027, 1, 2))
    assert [(r["day"], r["start"], r["text"]) for r in rows] == [
        (Date(2026, 12, 31), "", "종일 학사 종강"), (Date(2027, 1, 1), "10:00", "할 일 새해 계획")]


def test_the_agenda_tool_reads_both_sources_at_once(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch)
    import cygnus
    seen, both = [], asyncio.Event()

    async def events(home, f, t):
        seen.append(("calendar", f, t))
        await asyncio.wait_for(both.wait(), 1)  # finishes only once Gistra was asked too: in parallel
        return [{"day": Date.fromisoformat(f), "start": "09:00", "end": "10:00", "title": "치과", "where": ""}]

    async def call(name, args):
        seen.append((name, args["from"], args["to"]))
        both.set()
        return "### " + f"{int(args['from'][5:7])}월 {int(args['from'][8:])}일\n- 23:59 마감 과제 리포트 [a-1]"
    monkeypatch.setattr(g.google_tools, "events", events)
    monkeypatch.setattr(cygnus, "available", lambda: True)
    monkeypatch.setattr(cygnus, "call", call)
    today = g.tools_bridge.L.day_for(0, datetime.now(g.KST)).isoformat()
    r = asyncio.run(g.bridge.run("agenda", {}, caller="qwen"))
    assert r["ok"] and r["spoken"] == "일정 2건을 확인했어요.", r
    assert sorted(seen) == [("calendar", today, today), ("gistra_schedule", today, today)]
    assert "치과 (기본 캘린더)" in r["data"]["text"] and "마감 과제 리포트" in r["data"]["text"]
    bad = asyncio.run(g.bridge.run("agenda", {"from": "2026-11-01", "to": "2027-01-01"}, caller="qwen"))
    assert not bad["ok"] and bad["error"]["code"] == "invalid"
    assert "agenda" in g.tools_bridge.TAINT_TOOLS  # event titles are outside text: actions after it need a read-back


def test_here_is_the_phones_city_in_a_call_and_nothing_otherwise(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch)
    asked = []

    async def live(kind, args, timeout):
        asked.append((kind, timeout))
        return reply
    monkeypatch.setattr(g, "phone_live", live)
    reply = {"ok": True, "data": {"place": "서울특별시 중구"}}
    assert asyncio.run(g.here_location()) is None and asked == []  # no live call: the phone is never asked (no 12 s)
    g.call_warm_keys["call-1"] = "azel:call-1"
    reply = {"ok": True, "data": {"name": "GIST", "place": "광주광역시 북구 오룡동", "activity": "walking"}}
    assert asyncio.run(g.here_location()) == {"name": "광주광역시"} and asked == [("location", g.phone_data.LOCATION_S)]
    for reply in ({"ok": False, "error": "offline"}, {"ok": True, "data": {}}):
        assert asyncio.run(g.here_location()) is None  # no call, or no fix: the weather tool falls back to weather_home


def test_a_long_range_keeps_whole_days_and_names_the_cut():
    rows = [{"day": Date(2026, 12, d), "start": f"{h:02d}:00", "end": "", "text": f"수업 아주 긴 과목 이름 {d}-{h} — 오룡관 강의실",
             "src": "gistra"} for d in range(1, 31) for h in range(9, 15)]
    out = agenda.render(rows, Date(2026, 12, 1), Date(2026, 12, 30), ["(구글 캘린더는 지금 못 읽었어요: x.)"])
    assert len(out) < 6000 and "(이후 생략: 12/" in out and out.endswith("못 읽었어요: x.)")
    last = out.split("(이후 생략: ")[1].split("까지만")[0]
    body = out.split("\n(이후 생략")[0]
    assert body.count(f"### {last}") == 1 and body.split(f"### {last}")[1].count("\n- ") == 6  # that day is whole
