"""Background tasks: run, steps, notify, approval wait, budget and time kills, cancel, voice request."""
import asyncio
import json
import time

import pytest

from test_gateway import start, TestClient


@pytest.fixture
def tgw(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch, BISEO_DEVICE="macbook")
    g.TASK_APPROVAL_POLL_S = 0.05
    notes = []

    async def place_call(reason, opening=None, call_id=None, persona_id=None):
        notes.append(("call", reason, opening, persona_id))
        return {"call_id": "c"}

    async def push(title, body):
        notes.append(("push", title, body))
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
    assert notes == [("call", "task", "맡기신 작업 끝났어요. 파일 세 개를 정리했어요.", "azel")]
    assert [x["id"] for x in c.get("/v1/tasks").json()] == [t["id"]]
    assert g.fake.db["usage"][-1]["path"] == "task"


def test_task_waits_for_approval_then_resumes(tgw):
    g, c, notes = tgw
    g.fake.db["approvals"]["ap1"] = {"id": "ap1", "device": "macbook", "status": "pending"}
    tid = c.post("/v1/tasks", json={"goal": "TASK-APPROVAL 임시 폴더 지워 줘", "notify": "push"}).json()["id"]
    t = wait_for(c, tid, ("waiting_approval",))
    assert t["steps"][-1] == {**t["steps"][-1], "kind": "approval", "approval_id": "ap1"}
    time.sleep(0.2)
    assert c.get(f"/v1/tasks/{tid}").json()["status"] == "waiting_approval"  # it waits, it does not go on
    assert c.post("/v1/approvals/ap1", json={"approve": True}).status_code == 200
    t = wait_for(c, tid, ("done", "failed"))
    assert t["status"] == "done" and t["result"] == "승인받고 정리했어요."
    assert [s["kind"] for s in t["steps"]] == ["tool", "approval", "approval_approved"]
    assert notes == [("push", "작업 완료", "맡기신 작업 끝났어요. 승인받고 정리했어요.")]


def test_budget_kill(tgw):
    g, c, notes = tgw
    tid = c.post("/v1/tasks", json={"goal": "TASK-SPEND 큰 작업", "budget_usd": 2}).json()["id"]
    t = wait_for(c, tid, ("done", "failed"))
    assert t["status"] == "failed" and "예산" in t["error"] and t["steps"][-1]["reason"] == "budget"
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
