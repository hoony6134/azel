"""calendar_write: Google Calendar create/update/delete only after a read-back the user says yes to in a later turn
(user 2026-11-23: "대신 약간 컨펌 받고 하게"). Google is a MockTransport: no real calendar is ever written."""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from test_tasks import tgw  # noqa: F401  (fixture)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import google_tools as GT  # noqa: E402

KST = GT.KST
TOMORROW = (datetime.now(KST) + timedelta(days=1)).date()


class FakeCal:
    def __init__(self):
        self.writes, self.events = [], {}

    def __call__(self, request):
        url, m = str(request.url), request.method
        if url.startswith("https://oauth2.example/token"):
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        if "/users/me/calendarList" in url:
            return httpx.Response(200, json={"items": [{"id": "primary", "primary": True},
                                                       {"id": "me#personal@group.calendar.google.com", "summary": "개인"}]})
        if m == "GET" and url.split("?")[0].endswith("/events"):  # one day's events, for date+match
            return httpx.Response(200, json={"items": list(self.events.values())})
        if m == "GET":
            eid = url.split("?")[0].rsplit("/", 1)[1]
            return httpx.Response(200, json=self.events[eid]) if eid in self.events else httpx.Response(404)
        self.writes.append((m, url, json.loads(request.content) if request.content else None))
        return httpx.Response(204) if m == "DELETE" else httpx.Response(200, json={"id": "new1", **(
            json.loads(request.content) if request.content else {})})


def event(eid, title, hour, **more):
    s = datetime.combine(TOMORROW, datetime.min.time(), KST).replace(hour=hour)
    return {"id": eid, "summary": title, "start": {"dateTime": s.isoformat()},
            "end": {"dateTime": (s + timedelta(hours=1)).isoformat()}, **more}


def wire(g, tmp, fake, scopes=("https://www.googleapis.com/auth/calendar.events",)):
    for name, data in (("google_client.json", {"installed": {"client_id": "c", "client_secret": "s",
                                                             "token_uri": "https://oauth2.example/token"}}),
                       ("google_oauth.json", {"refresh_token": "r", "scopes": list(scopes), "created": 1})):
        (tmp / name).write_text(json.dumps(data))
        os.chmod(tmp / name, 0o600)
    GT._access.update(token=None, exp=0.0, grant=None)
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    g.bridge.d["gcal"] = lambda: GT.Calendar(tmp, client=client)


def call(g, c, args, who="ask:azel:call-1", turn="t1"):
    async def go():
        g.requester_ctx.set(who)
        g.turn_ctx.set(turn)
        return await g.bridge.run("calendar_write", args, None, "voice")
    return c.portal.call(go)


def test_create_reads_back_then_runs_only_on_a_yes_in_the_next_turn_once(tgw, tmp_path):  # noqa: F811
    g, c, _ = tgw
    fake = FakeCal()
    wire(g, tmp_path, fake)
    r = call(g, c, {"op": "create", "title": "치과", "start": f"{TOMORROW}T15:00"})
    assert r["needs"]["kind"] == "confirm" and r["needs"]["question"] == "내일 오후 3시~4시 '치과' 일정을 기본 캘린더에 추가할까요?"
    pid = r["needs"]["args"]["confirm"]
    assert pid in r["_model"] and not fake.writes  # nothing written yet
    same = call(g, c, {"confirm": pid})  # the model answering itself in the same turn
    assert not same["ok"] and "대답을 듣기 전" in same["spoken"] and not fake.writes
    done = call(g, c, {"confirm": pid}, turn="t2")
    assert done["ok"] and done["spoken"] == "캘린더에 추가했어요." and done["data"]["event"]
    (m, url, body), = fake.writes
    assert m == "POST" and "/calendars/primary/events" in url and "sendUpdates=none" in url
    assert body["summary"] == "치과" and body["start"]["dateTime"].endswith("15:00:00+09:00")
    again = call(g, c, {"confirm": pid}, turn="t3")
    assert not again["ok"] and again["error"]["code"] == "not_found" and len(fake.writes) == 1  # single use
    day = g.bridge.now().strftime("%Y-%m-%d")
    rows = g.fake.db["kv"][f"actions-{day}"]
    assert rows[-2]["action"] == "calendar_write" and rows[-2]["detail"].startswith("create primary ev:")
    assert "치과" not in json.dumps(rows, ensure_ascii=False)  # the audit names what, never the title


def test_expired_changed_or_foreign_confirms_do_nothing(tgw, tmp_path):  # noqa: F811
    g, c, _ = tgw
    fake = FakeCal()
    wire(g, tmp_path, fake)
    args = {"op": "create", "title": "회의", "start": f"{TOMORROW}T10:00", "calendar": "개인"}
    r = call(g, c, args)
    assert "개인에 추가할까요" in r["needs"]["question"]
    pid = r["needs"]["args"]["confirm"]
    g.bridge.cal_pending[pid]["at"] -= 121  # two minutes on
    assert call(g, c, {"confirm": pid}, turn="t2")["error"]["code"] == "not_found"
    pid = call(g, c, args)["needs"]["args"]["confirm"]
    assert call(g, c, {"confirm": pid}, who="ask:azel:call-9", turn="t9")["error"]["code"] == "not_found"  # another run
    changed = call(g, c, {**args, "title": "다른 회의", "confirm": pid}, turn="t2")
    assert changed["error"]["code"] == "invalid" and "다시 확인" in changed["spoken"]
    assert call(g, c, {"confirm": pid}, turn="t3")["error"]["code"] == "not_found"  # spent by the bad try
    pid = call(g, c, args)["needs"]["args"]["confirm"]
    assert call(g, c, {**args, "confirm": pid}, turn="t2")["ok"]  # the same args resent with the id: fine
    assert fake.writes[0][1].split("?")[0].endswith("/calendars/me%23personal@group.calendar.google.com/events")


def test_update_and_delete_name_the_event_and_warn_about_repeats_and_guests(tgw, tmp_path):  # noqa: F811
    g, c, _ = tgw
    fake = FakeCal()
    fake.events = {"ev1": event("ev1", "스터디", 19, recurringEventId="series1",
                                attendees=[{"email": "me@x", "self": True}, {"email": "a@x"}, {"email": "b@x"}])}
    wire(g, tmp_path, fake)
    ref = GT.ref_of("primary", "ev1")
    r = call(g, c, {"op": "delete", "event": f"ev:{ref}"})
    q = r["needs"]["question"]
    assert q.startswith("내일 오후 7시~8시 '스터디' 일정을 지울까요?") and "이번 한 번만" in q and "참석자 2명" in q
    assert "알림은 보내지 않아요" in q
    assert call(g, c, {"confirm": r["needs"]["args"]["confirm"]}, turn="t2")["ok"]
    m, url, _ = fake.writes[-1]
    assert m == "DELETE" and "/events/ev1?" in url and "sendUpdates=none" in url
    r = call(g, c, {"op": "delete", "event": ref, "recurring": "all", "notify_attendees": True})
    assert "전체" in r["needs"]["question"] and "알림이 가요" in r["needs"]["question"]
    call(g, c, {"confirm": r["needs"]["args"]["confirm"]}, turn="t3")
    assert "/events/series1?" in fake.writes[-1][1] and "sendUpdates=all" in fake.writes[-1][1]
    r = call(g, c, {"op": "update", "event": ref, "start": f"{TOMORROW}T20:30"})
    assert r["needs"]["question"].startswith("내일 오후 7시~8시 '스터디' 일정의 시간을 내일 오후 8시 30분~9시 30분으로 바꿀까요?")
    call(g, c, {"confirm": r["needs"]["args"]["confirm"]}, turn="t4")
    m, url, body = fake.writes[-1]
    assert m == "PATCH" and body["end"]["dateTime"].endswith("21:30:00+09:00")  # moved, same length


def test_ambiguous_unknown_and_no_permission(tgw, tmp_path):  # noqa: F811
    g, c, _ = tgw
    fake = FakeCal()
    fake.events = {"a1": event("a1", "치과 예약", 10), "a2": event("a2", "치과 상담", 16)}
    wire(g, tmp_path, fake)
    r = call(g, c, {"op": "delete", "date": str(TOMORROW), "match": "치과"})
    assert r["needs"]["kind"] == "choose" and len(r["needs"]["options"]) == 2 and not g.bridge.cal_pending  # asks
    r = call(g, c, {"op": "delete", "date": str(TOMORROW), "match": "상담"})
    assert "치과 상담" in r["needs"]["question"]  # one match: straight to the read-back
    r = call(g, c, {"op": "delete", "event": "abcdef"})  # e.g. a Gistra line: no [ev:]
    assert r["error"]["code"] == "not_found" and "Gistra" in r["spoken"]
    wire(g, tmp_path, fake, scopes=("https://www.googleapis.com/auth/calendar.readonly",))
    r = call(g, c, {"op": "create", "title": "x", "start": str(TOMORROW)})
    assert r["error"]["code"] == "unavailable" and "쓰기 권한" in r["spoken"] and not fake.writes


def test_a_path_without_turn_ids_needs_time_apart_and_tasks_never_write(tgw, tmp_path):  # noqa: F811
    g, c, _ = tgw
    fake = FakeCal()
    wire(g, tmp_path, fake)
    r = call(g, c, {"op": "create", "title": "운동", "start": str(TOMORROW)}, turn="")
    assert r["needs"]["question"] == "내일 종일 '운동' 일정을 기본 캘린더에 추가할까요?"
    pid = r["needs"]["args"]["confirm"]
    assert not call(g, c, {"confirm": pid}, turn="")["ok"]  # right away: the model, not the user
    g.bridge.cal_pending[pid]["at"] -= 5
    assert call(g, c, {"confirm": pid}, turn="")["ok"]
    assert fake.writes[-1][2]["end"] == {"date": str(TOMORROW + timedelta(days=1))}  # Google's end is exclusive
    t = call(g, c, {"op": "create", "title": "운동", "start": str(TOMORROW)}, who="task:0123456789ab")
    assert t["error"]["code"] == "invalid" and len(fake.writes) == 1
