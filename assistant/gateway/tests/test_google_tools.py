import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import google_tools as GT  # noqa: E402


def private(path, data):
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)


def setup(tmp, expired=False):
    private(tmp / "google_client.json", {"installed": {"client_id": "cid", "client_secret": "SECRET-CS",
                                                       "token_uri": "https://oauth2.example/token",
                                                       "auth_uri": "https://accounts.example/auth"}})
    private(tmp / "google_oauth.json", {"refresh_token": "SECRET-RT", "scopes": [], "expired": expired})
    GT._access.update(token=None, exp=0.0)


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class FakeGoogle:
    def __init__(self, grant_ok=True):
        self.grant_ok, self.seen = grant_ok, []

    def __call__(self, request):
        url = str(request.url)
        self.seen.append(url)
        if url.startswith("https://oauth2.example/token"):
            if not self.grant_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer AT"
        if "/calendarList" in url:
            return httpx.Response(200, json={"items": [{"id": "primary", "primary": True, "selected": True},
                                                       {"id": "hidden", "selected": False}]})
        if "/calendars/primary/events" in url:
            return httpx.Response(200, json={"items": [
                {"summary": "지온나래 정기회의", "location": "구관 토론실", "start": {"dateTime": "2026-11-23T20:00:00+09:00"},
                 "end": {"dateTime": "2026-11-23T22:00:00+09:00"}, "description": "SECRET-DESC"},
                {"summary": "취소됨", "status": "cancelled", "start": {"date": "2026-11-24"}, "end": {"date": "2026-11-25"}},
                {"summary": "생일", "start": {"date": "2026-11-24"}, "end": {"date": "2026-11-25"}}]})
        if "/calendars/hidden" in url:  # hidden in the UI, read anyway: the user wants every calendar
            return httpx.Response(200, json={"items": [{"summary": "생일", "start": {"date": "2026-11-24"},
                                                        "end": {"date": "2026-11-25"}}]})  # also on primary: kept once
        if url.endswith("/messages?q=is%3Aunread&maxResults=2") or "/messages?q=" in url:
            return httpx.Response(200, json={"messages": [{"id": "abc123def456"}]})
        if "/messages/abc123def456" in url and "format=metadata" in url:
            return httpx.Response(200, json={"snippet": "개인정보 유출 안내", "payload": {"headers": [
                {"name": "From", "value": "로폼 <no-reply@lawform>"}, {"name": "Subject", "value": "개인정보 유출 통지"},
                {"name": "Date", "value": "Tue, 11 Nov 2026 03:00:00 +0900"}]}})
        if "/messages/abc123def456" in url:
            return httpx.Response(200, json={"payload": {"headers": [{"name": "From", "value": "로폼"},
                                                                     {"name": "Subject", "value": "유출 통지"}],
                                                         "parts": [{"mimeType": "text/plain", "body": {"data": b64("본문 " * 2000)}},
                                                                   {"mimeType": "text/html", "body": {"data": b64("<b>html</b>")}},
                                                                   {"filename": "secret.pdf", "mimeType": "application/pdf",
                                                                    "body": {"attachmentId": "x"}}]}})
        raise AssertionError(url)


def run(tmp, name, args, fake):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as c:
            return await GT.call(tmp, name, args, client=c)
    return asyncio.run(go())


def test_status_and_bridge(tmp_path):
    assert GT.status(tmp_path) == "none" and GT.bridge(tmp_path) == ([], None)
    setup(tmp_path)
    assert GT.status(tmp_path) == "connected" and [d["name"] for d in GT.bridge(tmp_path)[0]] == \
        ["calendar_events", "gmail_search", "gmail_read"]
    os.chmod(tmp_path / "google_oauth.json", 0o644)
    assert GT.status(tmp_path) == "none"  # readable by others: treated as missing
    setup(tmp_path, expired=True)
    assert GT.status(tmp_path) == "expired" and GT.bridge(tmp_path) == ([], None)


def test_calendar_and_mail_reads_are_trimmed(tmp_path):
    setup(tmp_path)
    fake = FakeGoogle()
    out = run(tmp_path, "calendar_events", {"from": "2026-11-22", "to": "2026-11-28"}, fake)
    assert "11/23(Mon) 20:00~22:00 지온나래 정기회의 @ 구관 토론실" in out and "2026-11-24 종일 생일" in out
    assert "취소됨" not in out and "SECRET-DESC" not in out  # cancelled events and descriptions stay out
    out = run(tmp_path, "gmail_search", {"query": "is:unread", "max": 2}, fake)
    assert "id=abc123def456" in out and "개인정보 유출 통지" in out
    out = run(tmp_path, "gmail_read", {"id": "abc123def456"}, fake)
    assert out.startswith("보낸 사람: 로폼") and "(첨부 1개, 내용은 열지 않음)" in out and "html" not in out
    assert len(out) < GT.BODY_CHARS + 200 and not any("attachments" in u for u in fake.seen)
    assert sum("oauth2.example/token" in u for u in fake.seen) == 1  # the access token is reused


def test_bad_arguments_never_reach_google(tmp_path):
    setup(tmp_path)
    fake = FakeGoogle()
    for name, args in [("calendar_events", {"from": "2026-11-01", "to": "2027-02-01"}),
                       ("calendar_events", {"from": "어제", "to": "오늘"}), ("gmail_search", {"query": "x\x00"}),
                       ("gmail_read", {"id": "../../etc"}), ("gmail_send", {"to": "x"})]:
        out = run(tmp_path, name, args, fake)
        assert out.startswith(("도구 인자가 틀렸어요", "모르는 도구예요")), (name, out)
    assert fake.seen == []


def test_invalid_grant_marks_expired(tmp_path):
    setup(tmp_path)
    out = run(tmp_path, "calendar_events", {"from": "2026-11-22", "to": "2026-11-23"}, FakeGoogle(grant_ok=False))
    assert "Google 다시 연결" in out and GT.status(tmp_path) == "expired"
    saved = json.loads((tmp_path / "google_oauth.json").read_text())
    assert saved["expired"] is True and (tmp_path / "google_oauth.json").stat().st_mode & 0o777 == 0o600
    assert "SECRET" not in out


def test_scope_sets_start_read_only():
    assert all(s.endswith(".readonly") for s in GT.SCOPE_SETS["read"])
    assert set(GT.SCOPE_SETS["read"]) <= set(GT.SCOPE_SETS["calendar_write"])  # a re-consent adds, never drops


class ManyCalendars:
    """15 calendars over two calendarList pages: primary, selected, unselected, hidden, a deleted one, one that fails,
    one too slow for the budget, one whose events come in two pages, and one repeating another's event."""
    def __init__(self):
        self.read = []

    async def __call__(self, request):
        url, q = str(request.url), dict(request.url.params)
        if url.startswith("https://oauth2.example/token"):
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        if "/calendarList" in url:
            assert q.get("showHidden") == "true"
            first = [{"id": "primary", "primary": True, "summary": "me@example.com"},
                     {"id": "classes", "summary": "수업", "selected": True},
                     {"id": "contest", "summary": "contest", "summaryOverride": "대회 일정", "selected": False},
                     {"id": "hw", "summary": "과제 일정", "hidden": True},
                     {"id": "gone", "summary": "지운 것", "deleted": True},
                     {"id": "ko.south_korea#holiday@group.v.calendar.google.com", "summary": "대한민국의 휴일"},
                     {"id": "broken", "summary": "고장"}]
            if q.get("pageToken") != "p2":
                return httpx.Response(200, json={"items": first, "nextPageToken": "p2"})
            return httpx.Response(200, json={"items": [{"id": "slow", "summary": "느린 것"}, {"id": "paged", "summary": "긴 것"},
                                                       {"id": "shared", "summary": "공유"}] +
                                             [{"id": f"extra{i}", "summary": f"기타{i}"} for i in range(5)]})
        cal = url.split("/calendars/")[1].split("/events")[0]
        self.read.append(cal)
        ev = lambda title, hh: {"summary": title, "start": {"dateTime": f"2026-11-23T{hh}:00:00+09:00"},  # noqa: E731
                                "end": {"dateTime": f"2026-11-23T{hh}:50:00+09:00"}}
        if cal == "broken":
            return httpx.Response(500)
        if cal == "slow":
            await asyncio.sleep(2)
            return httpx.Response(200, json={"items": [ev("늦은 일정", "08")]})
        if cal == "paged":
            if q.get("pageToken") == "e2":
                return httpx.Response(200, json={"items": [ev("둘째 쪽 일정", "17")]})
            return httpx.Response(200, json={"items": [ev("첫째 쪽 일정", "16")], "nextPageToken": "e2"})
        items = {"primary": [ev("랩미팅", "10")], "classes": [ev("컴파일러", "11")], "contest": [ev("ICPC 예선", "13")],
                 "hw": [ev("리포트 마감", "23")], "shared": [ev("랩미팅", "10")],
                 "ko.south_korea%23holiday@group.v.calendar.google.com": []}.get(cal, [])
        return httpx.Response(200, json={"items": items})


def test_every_calendar_is_read_in_parallel_within_the_budget(tmp_path, monkeypatch):
    setup(tmp_path)
    monkeypatch.setattr(GT, "CAL_BUDGET_S", 0.6)
    fake = ManyCalendars()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as c:
            return await GT.events(tmp_path, "2026-11-23", "2026-11-23", client=c), await GT.call(
                tmp_path, "calendar_events", {"from": "2026-11-23", "to": "2026-11-23"}, client=c)
    t0 = time.monotonic()
    (rows, missed), text = asyncio.run(go())
    assert time.monotonic() - t0 < 2.5  # two reads, each bounded by the budget, never the slow calendar's 2 s each
    assert "gone" not in fake.read and {"hw", "contest", "extra4", "paged"} <= set(fake.read)  # hidden/unselected too
    got = [(r["start"], r["title"], r["cal"]) for r in rows]
    assert got == [("10:00", "랩미팅", None), ("11:00", "컴파일러", "수업"), ("13:00", "ICPC 예선", "대회 일정"),
                   ("16:00", "첫째 쪽 일정", "긴 것"), ("17:00", "둘째 쪽 일정", "긴 것"), ("23:00", "리포트 마감", "과제 일정")], got
    assert sorted(missed) == ["고장", "느린 것"]  # the 랩미팅 on 공유 is the primary's again: kept once, unlabeled
    assert "- 11/23(Mon) 13:00~13:50 ICPC 예선 (대회 일정)" in text and "- 11/23(Mon) 10:00~10:50 랩미팅\n" in text
    assert text.endswith("못 읽은 캘린더: 고장, 느린 것")


def test_events_that_began_before_the_range_are_listed_only_on_the_days_they_cover(tmp_path):
    """Live 2026-11-23: from=11/23 to=11/24 showed a "### 11/22(일)" section of GIST LMS all-day items."""
    setup(tmp_path)

    def fake(request):
        url = str(request.url)
        if url.startswith("https://oauth2.example/token"):
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        if "/calendarList" in url:
            return httpx.Response(200, json={"items": [{"id": "primary", "primary": True}]})
        day = lambda title, a, b: {"summary": title, "start": {"date": a}, "end": {"date": b}}  # noqa: E731  b exclusive
        return httpx.Response(200, json={"items": [
            day("12주차 토론지 제출", "2026-11-22", "2026-11-24"),  # 22 and 23: only 23 is in range
            day("학회", "2026-11-21", "2026-11-28"),  # covers the whole range: 23 and 24
            day("어제 끝난 것", "2026-11-22", "2026-11-23"),  # 22 only: Google may still send it, never listed
            {"summary": "밤샘 실험", "start": {"dateTime": "2026-11-22T23:00:00+09:00"},
             "end": {"dateTime": "2026-11-23T01:00:00+09:00"}},
            {"summary": "랩미팅", "start": {"dateTime": "2026-11-24T10:00:00+09:00"},
             "end": {"dateTime": "2026-11-24T11:00:00+09:00"}}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as c:
            return await GT.events(tmp_path, "2026-11-23", "2026-11-24", client=c)
    rows, missed = asyncio.run(go())
    got = [(r["day"].isoformat(), r["start"], r["title"]) for r in rows]
    assert got == [("2026-11-23", "", "12주차 토론지 제출"), ("2026-11-23", "", "학회"), ("2026-11-23", "", "밤샘 실험"),
                   ("2026-11-24", "", "학회"), ("2026-11-24", "10:00", "랩미팅")], got
    assert all("2026-11-23" <= r["day"].isoformat() <= "2026-11-24" for r in rows) and missed == []
