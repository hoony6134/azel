"""The overnight queue: when=overnight waits for the window, runs one at a time, stays quiet at night (approvals
wait for the morning), and the morning briefing reports it."""
import time
from datetime import datetime, timedelta

import pytest

from test_tasks import tgw, wait_for  # noqa: F401  (fixture)

V2 = {"X-Azel-Result": "2"}


def kst(g, ts):
    return datetime.fromtimestamp(ts, g.KST)


def test_window_and_next_start(tgw):
    g, _, _ = tgw
    at = lambda h, m=0: datetime(2026, 11, 23, h, m, tzinfo=g.KST)  # noqa: E731
    w = ["01:00", "06:00"]
    assert [g.in_window(at(h), w) for h in (0, 1, 5, 6, 23)] == [False, True, True, False, False]
    assert [g.in_window(at(h), ["23:00", "05:00"]) for h in (22, 23, 2, 5)] == [False, True, True, False]
    s = {"overnight": {"window": w}}
    assert g.next_window_start(at(23).timestamp(), s) == at(1).timestamp() + 86400  # tonight's, after midnight
    assert g.next_window_start(at(0, 30).timestamp(), s) == at(1).timestamp()
    assert g.next_window_start(at(3).timestamp(), s) == at(3).timestamp()  # inside: now


def test_settings_validation(tgw):
    _, c, _ = tgw
    assert c.get("/v1/settings").json()["overnight"] == {"window": ["01:00", "06:00"], "report_push": True,
                                                         "report_call": False, "budget_usd": 5.0}
    assert c.put("/v1/settings", json={"overnight": {"budget_usd": -1}}).status_code == 400
    assert c.put("/v1/settings", json={"overnight": {"window": ["25:00", "06:00"]}}).status_code == 400
    assert c.put("/v1/settings", json={"overnight": {"window": ["02:00", "02:00"]}}).status_code == 400
    r = c.put("/v1/settings", json={"overnight": {"window": ["23:30", "05:00"], "report_call": True}})
    assert r.status_code == 200 and c.get("/v1/settings").json()["overnight"]["window"] == ["23:30", "05:00"]


def test_overnight_tasks_wait_then_run_one_at_a_time(tgw):
    g, c, notes = tgw
    ids = [c.post("/v1/tasks", json={"goal": f"자료 {n} 정리해 줘", "notify": "push", "when": "overnight"}).json()["id"]
           for n in (1, 2)]
    t1 = c.get(f"/v1/tasks/{ids[0]}").json()
    assert t1["status"] == "scheduled" and t1["when"] == "overnight" and t1["scheduled_for"]
    time.sleep(0.2)
    assert c.get(f"/v1/tasks/{ids[0]}").json()["status"] == "scheduled"  # nothing started it
    due = kst(g, t1["scheduled_for"])
    c.portal.call(g.overnight_tick, due.replace(hour=7) + timedelta(days=1))  # due, but past the window
    assert c.get(f"/v1/tasks/{ids[0]}").json()["status"] == "scheduled"
    c.portal.call(g.overnight_tick, due)
    assert c.get(f"/v1/tasks/{ids[0]}").json()["status"] in ("queued", "running", "done")
    assert c.get(f"/v1/tasks/{ids[1]}").json()["status"] == "scheduled"  # one at a time
    wait_for(c, ids[0], ("done",))
    c.portal.call(g.overnight_tick, due + timedelta(minutes=1))
    assert wait_for(c, ids[1], ("done",))["result"] == "파일 세 개를 정리했어요."
    assert notes == [] or not g.in_window(datetime.now(g.KST), ["01:00", "08:00"])  # quiet when run at night
    listed = c.get("/v1/tasks/overnight", headers=V2).json()
    assert listed["ok"] and {i["id"] for i in listed["data"]} == set(ids)


def test_timed_and_cancelled_scheduled_tasks(tgw):
    g, c, _ = tgw
    at = (datetime.now(g.KST) + timedelta(hours=2)).replace(microsecond=0)
    tid = c.post("/v1/tasks", json={"goal": "백업 확인해 줘", "notify": "none", "when": at.isoformat()}).json()["id"]
    assert c.get(f"/v1/tasks/{tid}").json()["scheduled_for"] == at.timestamp()
    c.portal.call(g.overnight_tick, at - timedelta(minutes=1))
    assert c.get(f"/v1/tasks/{tid}").json()["status"] == "scheduled"
    c.portal.call(g.overnight_tick, at)  # a timed task ignores the window
    wait_for(c, tid, ("done",))
    other = c.post("/v1/tasks", json={"goal": "정리해 줘", "notify": "none", "when": "overnight"}).json()["id"]
    assert c.post(f"/v1/tasks/{other}/cancel").status_code == 200
    assert c.get(f"/v1/tasks/{other}").json()["status"] == "cancelled"
    assert c.post("/v1/tasks", json={"goal": "x", "when": "2020-01-01T00:00:00+09:00"}).status_code == 400
    assert c.post("/v1/tasks", json={"goal": "x", "when": "later"}).status_code == 400


def test_models_send_only_now_or_overnight(tgw):
    g, c, _ = tgw
    r = c.portal.call(g.bridge.run, "create_task", {"goal": "정리", "when": "2030-01-01T09:00:00+09:00"}, None, "api")
    assert r["error"]["code"] == "invalid"
    r = c.portal.call(g.bridge.run, "create_task", {"goal": "정리", "auto_approve": True, "when": "overnight"}, None, "api")
    assert r["error"]["code"] == "invalid"
    r = c.portal.call(g.bridge.run, "create_task", {"goal": "밤새 조사해 놔", "when": "overnight"}, None, "api")
    assert r["ok"] and r["spoken"].startswith("밤사이에 해 둘게요.") and r["data"]["status"] == "scheduled"
    r = c.portal.call(g.bridge.run, "overnight_list", {}, None, "api")
    assert r["ok"] and r["spoken"].startswith("밤사이 작업 1개")


def put_task(c, g, **t):
    t = {"id": t.pop("id"), "goal": "x", "notify": "push", "when": "overnight", "owner": g.DEVICE, "created": time.time(),
         "steps": [], "result": None, "error": None, **t}
    c.portal.call(g.task_save, t)
    c.portal.call(g.kv_list_add, "overnight-tasks", t["id"])
    return t


def test_morning_report_composition(tgw):
    g, c, notes = tgw
    put_task(c, g, id="a00000000001", goal="오픈 자비스 조사해 줘", status="done",
             result="**오픈 자비스**는 음성 비서 프로젝트예요. 깃허브에서 개발돼요.")
    put_task(c, g, id="a00000000002", goal="다운로드 폴더 정리해 줘", status="failed", error="권한이 없어요.")
    put_task(c, g, id="a00000000003", goal="임시 파일 지워 줘", status="waiting_approval")
    put_task(c, g, id="a00000000004", goal="논문 요약해 줘", status="scheduled")
    text, items = c.portal.call(g.overnight_report)
    assert text.startswith("밤사이 맡기신 일이에요.") and len(items) == 4
    assert "오픈 자비스는 음성 비서 프로젝트예요." in text and "깃허브" not in text and "**" not in text  # one sentence
    assert "끝내지 못했어요" in text and "앱에서 확인해 주세요" in text and "오늘 밤에 이어서" in text

    s = c.get("/v1/settings").json()
    c.portal.call(g.overnight_morning, s, True)  # the briefing rang: push only
    assert [n[:2] for n in notes] == [("push", "밤사이 한 일")] and notes[0][3]["type"] == "overnight"
    assert "4개 중 1개" in notes[0][2]
    text, items = c.portal.call(g.overnight_report)  # the finished ones are told once; the rest come again
    assert [i["id"] for i in items] == ["a00000000004"]
    told = c.get("/v1/tasks/a00000000001").json()
    assert told["reported"] and told["notified_at"]  # 6e5ee04's startup re-tell skips it

    notes.clear()
    put_task(c, g, id="a00000000005", goal="백업 확인해 줘", status="done", result="백업은 어제 밤 2시에 끝났어요.")
    s["overnight"]["report_call"] = True
    c.portal.call(g.overnight_morning, s, False)  # no briefing today: a call of its own
    assert [n[:2] for n in notes] == [("push", "밤사이 한 일"), ("call", "overnight_report")]
    assert "백업은 어제 밤 2시에 끝났어요." in notes[1][2]


def test_briefing_carries_the_report(tgw):
    g, c, notes = tgw
    put_task(c, g, id="a00000000006", goal="오픈 자비스 조사해 줘", status="done", result="음성 비서예요.")
    seen = {}

    async def place_call(reason, opening=None, extra="", **kw):
        seen.update(reason=reason, extra=extra)
    g.place_call = place_call
    c.portal.call(g.morning_briefing_call)
    assert seen["reason"] == "morning_briefing" and "음성 비서예요." in seen["extra"]


def test_night_approvals_wait_for_the_morning(tgw, monkeypatch):
    g, c, notes = tgw

    async def quiet(now_ts=None):
        return True
    monkeypatch.setattr(g, "night_quiet", quiet)
    t = put_task(c, g, id="b00000000001", goal="정리해 줘", status="running")
    r = c.portal.call(g.create_approval, {"device": "macbook", "command": "rm -rf /tmp/x", "requester": f"task:{t['id']}"})
    assert r["status"] == "pending" and notes == []  # no push at night
    assert r["expires_at"] == c.portal.call(g.morning_deadline) and r["expires_at"] > time.time() + 3 * 3600
    assert c.get(f"/v1/approvals/{r['approval_id']}").json()["status"] == "pending"
    c.portal.call(g.task_notify, {**t, "status": "waiting_approval"}, True)
    assert notes == []  # the runner's "승인이 필요해요" call/push waits too

    auto = put_task(c, g, id="b00000000002", status="running", auto_approve=True)
    c.portal.call(g.grant_create, {"task": auto["id"]}, None, 240)  # what run_task does for auto_approve
    r = c.portal.call(g.create_approval, {"device": "macbook", "command": "ls -la", "requester": f"task:{auto['id']}",
                                          "cwd": "/Users/jh/dev/proj", "in_roots": True})  # as the daemon files it
    assert r["status"] == "approved" and r["auto"]  # the grant comes first, the classifier decides
    r = c.portal.call(g.create_approval, {"device": "macbook", "command": "rm -rf /", "requester": f"task:{auto['id']}"})
    assert r["status"] == "pending" and r["expires_at"] > time.time() + 3 * 3600  # risky: still waits for the morning
    assert [n for n in notes if n[0] == "push" and n[1] == "승인 요청"] == []

    day = put_task(c, g, id="b00000000003", status="running", when="now")  # not an overnight task: as before
    c.portal.call(g.create_approval, {"device": "macbook", "command": "rm b", "requester": f"task:{day['id']}"})
    assert [n[1] for n in notes if n[0] == "push"][-1:] == ["승인 요청"]


def test_task_prompt_reads_late_night_words_from_when_it_was_asked(tgw):
    g, c, _ = tgw
    at = datetime(2026, 11, 23, 1, 0, tzinfo=g.KST).timestamp()
    p = g.task_prompt({"goal": "내일 아침까지 정리해 놔", "created": at, "device": None}, [])
    assert "11월 23일 01:00에 맡겨졌어" in p and "'내일'은 오늘(11월 23일)" in p
    p = g.task_prompt({"goal": "정리", "created": datetime(2026, 11, 22, 23, 0, tzinfo=g.KST).timestamp(),
                       "device": None}, [])
    assert "맡겨졌어" not in p


def test_auto_approve_task_runs_under_a_task_grant(tgw):
    g, c, _ = tgw
    made, real = [], g.grant_create

    async def grant_create(scope, devices=None, minutes=120, by="app"):
        made.append((scope, minutes, by))
        return await real(scope, devices, minutes, by)
    g.grant_create = grant_create
    tid = c.post("/v1/tasks", json={"goal": "정리해 줘", "notify": "none", "auto_approve": True}).json()["id"]
    wait_for(c, tid, ("done",))
    assert [(s, by) for s, _, by in made] == [({"task": tid}, "app")]
    assert not [x for x in c.portal.call(g.grants_live) if x["scope"] == {"task": tid}]  # ended with the task



def test_a_spent_night_budget_starts_nothing_more(tgw, monkeypatch):
    g, c, notes = tgw
    started = []
    monkeypatch.setattr(g, "start_task", lambda t: started.append(t["id"]))

    async def spent(now):
        return 5.0
    monkeypatch.setattr(g, "overnight_spent", spent)
    t = put_task(c, g, id="b00000000009", status="scheduled", when="overnight", scheduled_for=0)
    g.fake.db["kv"]["tasks-scheduled"] = [t["id"]]
    night = g.datetime(2026, 11, 23, 2, 0, tzinfo=g.KST)
    c.portal.call(g.overnight_tick, night)
    assert started == [] and g.fake.db["kv"][f"task-{t['id']}"]["status"] == "scheduled"  # waits for the morning
