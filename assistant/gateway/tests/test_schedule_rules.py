"""Batch 5 schedules end to end through the REST adapters: rules (cron), read-back, one fire moved or skipped,
push reminders, and the same result on the app's envelope and the legacy bodies."""
import asyncio
from datetime import datetime, timedelta
from urllib.parse import quote

from test_gateway import gw, kst  # noqa: F401  (fixture)

V2 = {"X-Azel-Result": "2"}


def test_rule_needs_a_yes_then_lists_with_its_text(gw):  # noqa: F811
    g, c, _ = gw
    body = {"reason": "스트레칭", "repeat_cron": "0 10-18/2 * * 1-5", "kind": "reminder", "notify": "push"}
    r = c.post("/v1/schedule", json=body, headers=V2)
    needs = r.json()["needs"]
    assert r.status_code == 200 and needs["kind"] == "confirm" \
        and needs["question"] == "평일 오전 10시부터 오후 6시까지 2시간마다 알림 드릴게요. 맞죠?"
    assert needs["args"] == {"reason": "스트레칭", "kind": "reminder", "notify": "push", "repeat_cron": "0 10-18/2 * * 1-5",
                             "start_date": None, "end_date": None, "confirmed": True}  # resend these after a yes
    assert c.post("/v1/schedule", json=body).status_code == 409  # legacy body: needs as 409 with the question
    assert not g.fake.db["schedule"]  # nothing made before the yes
    r = c.post("/v1/schedule", json={**body, "confirmed": True}, headers=V2)
    assert r.status_code == 201 and r.json()["ok"] and r.json()["spoken"].startswith("평일 오전 10시부터")
    item = r.json()["data"]
    assert item["cron"] == "0 10-18/2 * * 1-5" and item["notify"] == "push" and item["purpose"] == "스트레칭"
    assert item["rule_text"] == "평일 오전 10시부터 오후 6시까지 2시간마다"
    listed = c.get("/v1/schedule", headers=V2).json()
    assert listed["ok"] and [i["id"] for i in listed["data"]] == [item["id"]]
    assert c.get("/v1/schedule").json()[0]["rule_text"] == item["rule_text"]  # the legacy list shape, new fields


def test_refusals_are_the_same_code_on_rest_and_tools(gw):  # noqa: F811
    g, c, _ = gw
    r = c.post("/v1/schedule", json={"reason": "x", "repeat_cron": "*/10 * * * *", "confirmed": True}, headers=V2)
    assert r.status_code == 400 and r.json()["error"] == {"code": "invalid", "message": "전화 사이가 30분은 넘어야 해요."}
    tool = asyncio.run(g.bridge.run("schedule_create", {"reason": "x", "repeat_cron": "*/10 * * * *",
                                                         "confirmed": True}, caller="api"))
    assert tool["error"] == r.json()["error"]
    assert c.post("/v1/schedule", json={"reason": "x", "when": 5}, headers=V2).json()["error"]["code"] == "invalid"
    assert c.put("/v1/schedule/nope", json={"purpose": "x"}, headers=V2).status_code == 404


def test_move_and_skip_one_fire_keep_the_rule(gw):  # noqa: F811
    g, c, _ = gw
    now = datetime.now(g.KST)
    item = c.post("/v1/schedule", json={"reason": "물", "repeat_cron": "0 9 * * *", "kind": "reminder"},
                  headers=V2).json()["data"]
    first = datetime.fromisoformat(item["at"])
    r = c.put(f"/v1/schedule/{item['id']}?occurrence={quote(first.isoformat())}",
              json={"when": (first + timedelta(hours=1)).isoformat()}, headers=V2).json()
    assert r["ok"] and "→" in r["spoken"] and r["data"]["cron"] is None, r  # a one-off for that day
    rule = g.fake.db["schedule"][item["id"]]
    assert rule["skips"] == [first.timestamp()] and rule["at"] == (first + timedelta(days=1)).timestamp()
    second = first + timedelta(days=1)
    assert c.delete(f"/v1/schedule/{item['id']}?occurrence={second.isoformat()}", headers=V2).json()["error"][
        "code"] in ("invalid", "not_found")  # an unencoded "+" arrives as a space: refused, never a crash
    r = c.delete(f"/v1/schedule/{item['id']}?occurrence={quote(second.isoformat())}", headers=V2).json()
    assert r["ok"] and r["spoken"].endswith("한 번만 건너뛸게요.")
    assert rule["at"] == (first + timedelta(days=2)).timestamp() and rule["status"] == "pending"
    r = c.put(f"/v1/schedule/{item['id']}", json={"when": (now + timedelta(days=1)).replace(
        hour=8, minute=0, second=0, microsecond=0).isoformat()}, headers=V2).json()
    assert r["ok"] and rule["cron"] == "0 8 * * *" and "→" in r["spoken"]  # the rule's time of day moved
    assert c.delete(f"/v1/schedule/{item['id']}", headers=V2).json()["data"]["status"] == "cancelled"


def test_push_reminder_fires_as_a_notification_and_moves_on(gw, monkeypatch):  # noqa: F811
    g, _, _ = gw
    pushes, placed = [], []

    async def fake_alert(title, body, **extra):
        pushes.append((title, body, extra))
        return {"sent": 1}

    async def fake_place(*a, **k):
        placed.append(a)
    monkeypatch.setattr(g, "send_alert_push", fake_alert)
    monkeypatch.setattr(g, "place_call", fake_place)
    at = kst(2026, 11, 23, 9, 0).timestamp()  # a Monday
    g.fake.db["schedule"]["r1"] = {"id": "r1", "at": at, "repeat": None, "kind": "reminder", "note": "",
                                   "created_by": "app", "status": "pending", "attempts": 0, "call_id": None,
                                   "last_attempt": None, "cron": "0 9 * * 1-5", "notify": "push",
                                   "purpose": "스트레칭", "request": "평일 9시에 스트레칭하라고 알림으로"}
    asyncio.run(g.fire_scheduled(at))
    item = g.fake.db["schedule"]["r1"]
    assert placed == [] and len(pushes) == 1 and pushes[0][2] == {"type": "schedule", "schedule_id": "r1"}
    assert "스트레칭" in pushes[0][1] and "하라고" not in pushes[0][1]
    assert item["status"] == "pending" and item["at"] == kst(2026, 11, 24, 9, 0).timestamp()


def test_rule_ends_after_its_end_date(gw, monkeypatch):  # noqa: F811
    g, _, _ = gw

    async def fake_place(reason, opening=None, call_id=None, persona_id=None):
        return {"call_id": call_id}
    monkeypatch.setattr(g, "place_call", fake_place)
    at = kst(2026, 11, 23, 9, 0).timestamp()
    g.fake.db["schedule"]["r2"] = {"id": "r2", "at": at, "repeat": None, "kind": "reminder", "note": "",
                                   "created_by": "app", "status": "pending", "attempts": 0, "call_id": None,
                                   "last_attempt": None, "cron": "0 9 * * *", "end_date": "2026-11-23"}
    asyncio.run(g.fire_scheduled(at))
    asyncio.run(g.call_answered(g.fake.db["schedule"]["r2"]["call_id"]))
    assert g.fake.db["schedule"]["r2"]["status"] == "answered"  # the last day: nothing after it
