"""Approvals: 5-minute expiry, once / always / deny, rules the app can revoke, audit, push, requester."""
import json
import time

import pytest

from test_gateway import TestClient, events, start
from test_tasks import wait_for

CMD = {"device": "macbook", "tool": "run_command", "command": 'open -a "Google Chrome"', "cwd": "/Users/jh"}


@pytest.fixture
def agw(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch, BISEO_DEVICE="macbook")
    pushes = []

    async def push(title, body, **extra):
        pushes.append((title, body, extra))
    g.send_alert_push = push
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        yield g, c, pushes


def test_pending_push_and_requester(agw):
    g, c, pushes = agw
    events(c.post("/v1/ask", json={"text": "맥북에서 크롬 열어", "session": "azel:call-1"}))
    r = c.post("/v1/approvals", json=CMD).json()
    assert r["status"] == "pending" and r["expires_at"] - time.time() == pytest.approx(300, abs=5)
    assert pushes == [("승인 요청", "macbook에서 명령 실행 승인이 필요해요", {"type": "approval", "approval_id": r["approval_id"]})]
    a = c.get(f"/v1/approvals/{r['approval_id']}").json()
    assert a["status"] == "pending" and a["requester"] == {"session": "azel:call-1", "persona": "azel"}
    assert [x["id"] for x in c.get("/v1/approvals").json()] == [r["approval_id"]]
    assert c.post("/v1/approvals", json={"device": "macbook"}).status_code == 400


def test_once_and_deny(agw):
    g, c, _ = agw
    a1 = c.post("/v1/approvals", json=CMD).json()["approval_id"]
    assert c.post(f"/v1/approvals/{a1}", json={"decision": "once"}).json()["status"] == "approved"
    assert c.post(f"/v1/approvals/{a1}", json={"decision": "deny"}).status_code == 409  # decided already
    a2 = c.post("/v1/approvals", json=CMD).json()["approval_id"]
    assert c.post(f"/v1/approvals/{a2}", json={"decision": "deny"}).json()["status"] == "rejected"
    a3 = c.post("/v1/approvals", json=CMD).json()["approval_id"]
    assert c.post(f"/v1/approvals/{a3}", json={"approve": True}).json()["decision"] == "once"  # older apps
    assert c.post(f"/v1/approvals/{a3}", json={"decision": "maybe"}).status_code == 400
    assert c.get("/v1/approvals/rules").json() == []  # once never makes a rule


def test_expiry(agw):
    g, c, _ = agw
    aid = c.post("/v1/approvals", json=CMD).json()["approval_id"]
    g.fake.db["kv"][f"approval-{aid}"]["expires_at"] = time.time() - 1
    assert c.get(f"/v1/approvals/{aid}").json()["status"] == "expired"
    assert c.get("/v1/approvals").json() == []
    assert c.post(f"/v1/approvals/{aid}", json={"decision": "once"}).status_code == 409


SAFE = {**CMD, "command": "mkdir -p reports/2026"}


def test_always_rule_auto_approves_and_can_be_revoked(agw):
    g, c, pushes = agw
    CMD = SAFE  # noqa: N806 — a command that runs no other code may become a rule
    aid = c.post("/v1/approvals", json=CMD).json()["approval_id"]
    r = c.post(f"/v1/approvals/{aid}", json={"decision": "always"}).json()
    rules = c.get("/v1/approvals/rules").json()
    assert r["status"] == "approved" and rules == [{**rules[0], "device": "macbook", "command": CMD["command"], "cwd": "/Users/jh"}]
    assert c.post("/v1/approvals", json={**CMD, "cwd": "/Users/jh/other"}).json()["status"] == "pending"  # cwd too
    n = len(pushes)
    again = c.post("/v1/approvals", json=CMD).json()
    assert again["status"] == "approved" and again["rule_id"] == rules[0]["id"] and len(pushes) == n  # no tap needed
    assert c.get(f"/v1/approvals/{again['approval_id']}").json()["decision"] == "rule"
    assert c.post("/v1/approvals", json={**CMD, "command": "mkdir -p reports"}).json()["status"] == "pending"  # exact only
    assert c.post("/v1/approvals", json={**CMD, "device": "albireo"}).json()["status"] == "pending"  # per device
    assert c.delete(f"/v1/approvals/rules/{rules[0]['id']}").json() == {"deleted": rules[0]["id"]}
    assert c.delete(f"/v1/approvals/rules/{rules[0]['id']}").status_code == 404
    assert c.post("/v1/approvals", json=CMD).json()["status"] == "pending"


def test_audit_of_approved_runs(agw):
    g, c, _ = agw
    aid = c.post("/v1/approvals", json=CMD).json()["approval_id"]
    assert c.post(f"/v1/approvals/{aid}/ran", json={"exit_code": 0}).status_code == 404  # not approved yet
    c.post(f"/v1/approvals/{aid}", json={"decision": "once"})
    assert c.post(f"/v1/approvals/{aid}/ran", json={"exit_code": 0, "status": "ok", "stdout": "SECRET"}).json() == {"ok": True}
    audit = c.get("/v1/approvals/audit").json()
    assert audit == [{**audit[0], "approval_id": aid, "device": "macbook", "command": CMD["command"], "exit_code": 0,
                      "status": "ok", "decision": "once"}]
    assert "SECRET" not in json.dumps(g.fake.db["kv"])  # output is never stored


def test_task_resumes_when_its_approval_expires(tmp_path, monkeypatch):
    from test_tasks import tgw_track
    g = start(tmp_path, monkeypatch, BISEO_DEVICE="macbook")
    g.TASK_APPROVAL_POLL_S = 0.05
    tgw_track(g)

    async def quiet(*a, **k):
        return {}
    g.place_call = g.send_alert_push = quiet
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        g.fake.db["approvals"]["ap1"] = {"id": "ap1", "device": "macbook", "status": "pending", "created": time.time() - 400}
        tid = c.post("/v1/tasks", json={"goal": "TASK-APPROVAL 지워 줘", "notify": "none"}).json()["id"]
        t = wait_for(c, tid, ("done", "failed"))
        assert t["result"] == "거절돼서 나머지만 했어요." and [s["kind"] for s in t["steps"]][-1] == "approval_expired"


@pytest.mark.parametrize("cmd", ['open -a "Google Chrome"', "python3 tool.py", "env FOO=1 make", "find . -name x",
                                 "bash run.sh", "/usr/bin/python3.12 x.py"])
def test_always_forced_to_once_for_code_runners(agw, cmd):
    g, c, _ = agw
    aid = c.post("/v1/approvals", json={**CMD, "command": cmd}).json()["approval_id"]
    r = c.post(f"/v1/approvals/{aid}", json={"decision": "always"}).json()
    assert r["status"] == "approved" and r["decision"] == "once" and r["forced_once"] is True
    assert c.get("/v1/approvals/rules").json() == []


def test_use_is_central_once_and_bound(agw):
    g, c, _ = agw
    aid = c.post("/v1/approvals", json=SAFE).json()["approval_id"]
    use = {k: SAFE[k] for k in ("device", "command", "cwd")}
    assert c.post(f"/v1/approvals/{aid}/use", json=use).status_code == 409  # not approved yet
    c.post(f"/v1/approvals/{aid}", json={"decision": "once"})
    assert c.post(f"/v1/approvals/{aid}/use", json={**use, "device": "albireo"}).status_code == 409  # another device
    assert c.post(f"/v1/approvals/{aid}/use", json={**use, "cwd": "/tmp"}).status_code == 409
    assert c.post(f"/v1/approvals/{aid}/use", json=use).json()["status"] == "used"
    assert c.post(f"/v1/approvals/{aid}/use", json=use).status_code == 409  # replay
    assert c.get(f"/v1/approvals/{aid}").json()["status"] == "used"
    assert c.post(f"/v1/approvals/{aid}/ran", json={"exit_code": 0}).json() == {"ok": True}
    late = c.post("/v1/approvals", json=SAFE).json()["approval_id"]
    c.post(f"/v1/approvals/{late}", json={"decision": "once"})
    g.fake.db["approvals"][late]["decided"] = time.time() - 601  # tapped 10 minutes ago, never run
    assert c.get(f"/v1/approvals/{late}").json()["status"] == "expired"
    assert c.post(f"/v1/approvals/{late}/use", json=use).status_code == 409


def test_task_commands_never_auto_approved(agw):
    g, c, pushes = agw
    aid = c.post("/v1/approvals", json=SAFE).json()["approval_id"]
    c.post(f"/v1/approvals/{aid}", json={"decision": "always"})
    g.note_requester(task_id="abcdefabcdef", persona="azel")
    r = c.post("/v1/approvals", json=SAFE).json()
    assert r["status"] == "pending" and r["approval_id"] in g.task_approvals["abcdefabcdef"]


def test_untracked_approval_id_does_not_stall_a_task(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch, BISEO_DEVICE="macbook")

    async def quiet(*a, **k):
        return {}
    g.place_call = g.send_alert_push = quiet
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        g.fake.db["approvals"]["ap1"] = {"id": "ap1", "device": "macbook", "status": "pending"}
        tid = c.post("/v1/tasks", json={"goal": "TASK-APPROVAL 지워 줘", "notify": "none"}).json()["id"]
        t = wait_for(c, tid, ("done", "failed", "waiting_approval"))
        assert t["status"] == "done" and "approval" not in [s["kind"] for s in t["steps"]]  # text in output ≠ a request


def test_limited_gateway_approvals_without_kv(tmp_path, monkeypatch):
    for name, text in (("anthropic_api_key", "sk-test\n"), ("state_token_limited", "l1m\n")):
        (tmp_path / name).write_text(text)
        (tmp_path / name).chmod(0o600)
    g = start(tmp_path, monkeypatch, BISEO_MODE="limited", BISEO_DEVICE="spark")

    def no_kv(*a, **k):
        raise AssertionError("limited gateways must not touch kv")
    g.fake.db["kv"] = type("NoKV", (dict,), {"__getitem__": no_kv, "__setitem__": no_kv, "__contains__": no_kv})()
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        r = c.post("/v1/approvals", json={**SAFE, "device": "spark"}).json()
        assert r["status"] == "pending"
        assert c.get(f"/v1/approvals/{r['approval_id']}").json()["status"] == "pending"
        assert c.post(f"/v1/approvals/{r['approval_id']}", json={"decision": "once"}).status_code == 403  # can't approve
