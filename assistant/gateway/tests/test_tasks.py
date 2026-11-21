"""Background tasks: run, steps, notify, approval wait, budget and time kills, cancel, voice request."""
import asyncio
import json
import time

import pytest

from test_gateway import start, TestClient


class _Tracked(dict):  # the fake claude reports approval "ap1": count it as created by whichever task asks
    def get(self, key, default=None):
        return {"ap1"}


def tgw_track(g):
    g.task_approvals = _Tracked()


@pytest.fixture
def tgw(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch, BISEO_DEVICE="macbook")
    g.TASK_APPROVAL_POLL_S = 0.05
    tgw_track(g)
    notes = []

    async def place_call(reason, opening=None, call_id=None, persona_id=None):
        notes.append(("call", reason, opening, persona_id))
        return {"call_id": "c"}

    async def push(title, body, **extra):
        notes.append(("push", title, body, extra))
    g.place_call, g.send_alert_push = place_call, push
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        yield g, c, notes


def wait_for(c, tid, statuses, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        t = c.get(f"/v1/tasks/{tid}").json()
        if t["status"] in statuses:
            return t
        time.sleep(0.05)
    raise AssertionError(f"task stuck in {t['status']}")


def test_task_runs_logs_steps_and_calls(tgw):
    g, c, notes = tgw
    r = c.post("/v1/tasks", json={"goal": "맥북 다운로드 폴더 정리해 줘", "notify": "call", "persona": "azel"})
    assert r.status_code == 201 and r.json()["status"] in ("queued", "running")
    t = wait_for(c, r.json()["id"], ("done", "failed"))
    assert t["status"] == "done" and t["result"] == "파일 세 개를 정리했어요." and t["cost_usd"] == pytest.approx(0.1)
    assert [(s["kind"], s.get("label")) for s in t["steps"]] == [("tool", "macbook 파일 보는 중")]
    assert "SECRET" not in json.dumps(t)  # tool arguments and results never reach the log
    end = time.time() + 5
    while not notes and time.time() < end:  # the status is saved a moment before the call goes out
        time.sleep(0.02)
    assert notes == [("call", "task", "맡기신 맥북 다운로드 폴더 정리 끝났어요. 파일 세 개를 정리했어요.", "azel")]
    assert t["backend"] == "claude_code" and t["fallback_reason"] == "API 키가 없어 Claude Code로 했어요."  # no key here
    assert [x["id"] for x in c.get("/v1/tasks").json()] == [t["id"]]
    row = g.fake.db["usage"][-1]  # a task runs on claude -p: counted as the subscription path, labelled as a task
    assert (row["path"], row["task"], row["model"]) == ("claude_code", "task", "sonnet")


def test_task_waits_for_approval_then_resumes(tgw):
    g, c, notes = tgw
    g.fake.db["approvals"]["ap1"] = {"id": "ap1", "device": "macbook", "status": "pending"}
    tid = c.post("/v1/tasks", json={"goal": "TASK-APPROVAL 임시 폴더 지워 줘", "notify": "push"}).json()["id"]
    t = wait_for(c, tid, ("waiting_approval",))
    end = time.time() + 5
    while not notes and time.time() < end:
        time.sleep(0.02)
    assert t["steps"][-1] == {**t["steps"][-1], "kind": "approval", "approval_id": "ap1"}
    time.sleep(0.2)
    assert c.get(f"/v1/tasks/{tid}").json()["status"] == "waiting_approval"  # it waits, it does not go on
    assert c.post("/v1/approvals/ap1", json={"approve": True}).status_code == 200
    t = wait_for(c, tid, ("done", "failed"))
    assert t["status"] == "done" and t["result"] == "승인받고 정리했어요."
    assert [s["kind"] for s in t["steps"]] == ["tool", "approval", "approval_approved"]
    assert notes == [("push", "승인 필요", "승인이 필요해요: TASK-APPROVAL 임시 폴더 지워 줘", {"type": "task", "task_id": tid}),
                     ("push", "작업 완료", "작업이 끝났어요: TASK-APPROVAL 임시 폴더 지워 줘", {"type": "task", "task_id": tid})]


def test_budget_kill(tgw):
    g, c, notes = tgw
    tid = c.post("/v1/tasks", json={"goal": "TASK-SPEND 큰 작업", "budget_usd": 2}).json()["id"]
    t = wait_for(c, tid, ("done", "failed"))
    assert t["status"] == "failed" and "예산" in t["error"] and t["steps"][-1]["reason"] == "budget"
    end = time.time() + 5
    while not notes and time.time() < end:
        time.sleep(0.02)
    assert notes and notes[0][1] == "작업 실패"


def test_time_kill(tgw):
    g, c, notes = tgw
    started = time.time()
    tid = c.post("/v1/tasks", json={"goal": "TASK-SLOW 오래 걸리는 작업", "max_minutes": 0.02}).json()["id"]
    t = wait_for(c, tid, ("done", "failed"))
    assert t["status"] == "failed" and "시간 한도" in t["error"] and time.time() - started < 6


def test_cancel(tgw):
    g, c, notes = tgw
    tid = c.post("/v1/tasks", json={"goal": "TASK-SLOW 취소할 작업", "notify": "call"}).json()["id"]
    wait_for(c, tid, ("running",))
    assert c.post(f"/v1/tasks/{tid}/cancel").json()["status"] == "cancelled"
    assert c.post(f"/v1/tasks/{tid}/cancel").status_code == 409
    assert notes == []  # a cancelled task does not call


def test_validation_and_two_slots(tgw):
    g, c, _ = tgw
    for bad in [{}, {"goal": ""}, {"goal": "x", "notify": "sms"}, {"goal": "x", "budget_usd": 0},
                {"goal": "x", "max_minutes": 999}, {"goal": "x", "device": "Bad Id"}, {"goal": "x", "persona": "nobody"}]:
        assert c.post("/v1/tasks", json=bad).status_code == 400, bad
    ids = [c.post("/v1/tasks", json={"goal": f"TASK-SLOW {i}", "notify": "none"}).json()["id"] for i in range(3)]
    time.sleep(0.5)
    states = [c.get(f"/v1/tasks/{i}").json()["status"] for i in ids]
    assert states.count("running") == 2 and states.count("queued") == 1  # at most two at once
    for i in ids:
        c.post(f"/v1/tasks/{i}/cancel")


def test_voice_task_request_over_the_link(tgw):
    g, c, notes = tgw
    sent = []

    async def link_send(obj):
        sent.append(obj)
        return True
    g.link_send = link_send

    async def go():
        await g.serve_task({"id": "q1", "type": "task", "goal": "맥북 다운로드 폴더 정리해 줘", "notify": "call"})
        await asyncio.gather(*g.task_runs.values())
    asyncio.run(go())
    assert sent[0]["ok"] and sent[0]["say"] == "맥북에서 시작할게요. 끝나면 전화드릴게요."
    assert notes and notes[0][0] == "call"


def test_limited_gateway_has_no_tasks(tmp_path, monkeypatch):
    for name, text in (("anthropic_api_key", "sk-test\n"), ("state_token_limited", "l1m\n")):
        (tmp_path / name).write_text(text)
        (tmp_path / name).chmod(0o600)
    g = start(tmp_path, monkeypatch, BISEO_MODE="limited", BISEO_DEVICE="spark")
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        assert c.post("/v1/tasks", json={"goal": "x"}).status_code == 403
        assert c.get("/v1/tasks").status_code == 403


def test_alert_only_registration_and_task_push_payload(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch, BISEO_DEVICE="macbook")
    sent = []

    async def apns_send(payload, kind):
        sent.append((kind, payload))
        return {"sent": 1}
    g.apns_send = apns_send
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        assert c.post("/v1/push/register", json={"bundle_id": "xyz.scian.biseo"}).status_code == 400
        assert c.post("/v1/push/register", json={"alert_token": "mac-alert", "bundle_id": "xyz.scian.other"}).status_code == 400
        assert c.post("/v1/push/register", json={"alert_token": "mac-alert", "bundle_id": "xyz.scian.biseo"}).status_code == 200
        row = g.fake.db["push"]["mac-alert"]
        assert (row["bundle_id"], row["env"], row["kind"]) == ("xyz.scian.biseo", "sandbox", "alert") and len(g.fake.db["push"]) == 1
        for i in range(12):  # at most 10 per kind: the oldest are evicted
            c.post("/v1/push/register", json={"voip_token": f"voip-{i:02d}", "alert_token": f"alert-{i:02d}",
                                              "bundle_id": "xyz.scian.biseo"})
            time.sleep(0.002)
        kinds = [r.get("kind", "voip") for r in g.fake.db["push"].values()]
        assert kinds.count("voip") == 10 and kinds.count("alert") == 10 and "voip-00" not in g.fake.db["push"]
    asyncio.run(g.task_notify({"id": "abc123abc123", "notify": "push", "status": "done", "result": "SECRET 모델 답",
                               "goal": "맥북 다운로드 폴더를 아주 아주 꼼꼼하게 정리하고 중복 파일도 지워서 용량 확보해 줘", "persona": None}))
    body = sent[-1][1]["aps"]["alert"]["body"]
    assert body.startswith("작업이 끝났어요: 맥북 다운로드") and body.endswith("…") and len(body) <= 50 and "SECRET" not in body
    assert sent[-1][1]["type"] == "task" and sent[-1][1]["task_id"] == "abc123abc123"


def test_call_mode_rings_for_an_approval(tgw):
    g, c, notes = tgw
    g.fake.db["approvals"]["ap1"] = {"id": "ap1", "device": "macbook", "status": "pending"}
    tid = c.post("/v1/tasks", json={"goal": "TASK-APPROVAL 지워 줘", "notify": "call", "persona": "azel"}).json()["id"]
    wait_for(c, tid, ("waiting_approval",))
    end = time.time() + 5
    while not notes and time.time() < end:  # the status is saved a moment before the call goes out
        time.sleep(0.02)
    assert notes[0] == ("call", "task_approval", "맡기신 작업에 승인이 필요해요. 앱에서 확인해 주세요.", "azel")
    c.post("/v1/approvals/ap1", json={"approve": False})
    t = wait_for(c, tid, ("done", "failed"))
    end = time.time() + 5
    while len(notes) < 2 and time.time() < end:  # the finish call also goes out just after the status is saved
        time.sleep(0.02)
    assert t["result"] == "거절돼서 나머지만 했어요." and notes[-1][1] == "task"


def test_call_mode_rings_on_failure(tgw):
    g, c, notes = tgw
    tid = c.post("/v1/tasks", json={"goal": "TASK-SPEND 큰 작업", "notify": "call", "persona": "azel"}).json()["id"]
    t = wait_for(c, tid, ("done", "failed"))
    end = time.time() + 5
    while not notes and time.time() < end:
        time.sleep(0.02)
    assert t["status"] == "failed" and notes == [("call", "task", "맡기신 TASK-SPEND 큰 작업이 끝나지 못했어요. " + t["error"],
                                                  "azel")]


def test_spoken_results_lose_markdown_and_emoji(tgw):
    g, c, notes = tgw
    assert g.spoken_text("## 결과 ✅\n- **세 개** 정리 🎉\n1. `a.txt`") == "결과 세 개 정리 a.txt"
    assert g.strip_marks("**오늘** 맑아요 ☀️") == "오늘 맑아요 "
    t = {"status": "done", "goal": "다운로드 정리해 줘", "result": "- **3개** 옮겼어요 👍"}
    assert g.task_summary(t, "정훈님") == "정훈님, 맡기신 다운로드 정리 끝났어요. 3개 옮겼어요"
