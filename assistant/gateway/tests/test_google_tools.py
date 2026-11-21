import asyncio
import base64
import json
import os
import sys
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
        if "/calendars/hidden" in url:
            raise AssertionError("a calendar the user hides was read")
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
