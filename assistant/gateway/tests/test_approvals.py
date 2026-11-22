"""Approvals: 5-minute expiry, once / always / deny, rules the app can revoke, audit, push, requester."""
import asyncio
import json
import time

import httpx

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
    r = c.post("/v1/approvals", json={**CMD, "requester": "ask:azel:call-1"}).json()  # the daemon passes it on
    assert r["status"] == "pending" and r["expires_at"] - time.time() == pytest.approx(300, abs=5)
    assert pushes == [("승인 요청", "macbook에서 명령 실행 승인이 필요해요",
                       {"category": "APPROVAL", "type": "approval", "approval_id": r["approval_id"]})]  # 허용/거부/항상 허용
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


SAFE = {**CMD, "command": "ls -la reports/2026"}


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
    assert c.post("/v1/approvals", json={**CMD, "command": "ls -la reports"}).json()["status"] == "pending"  # exact only
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


@pytest.mark.parametrize("cmd", ["python3 tool.py", "env FOO=1 ls", "bash run.sh", "/usr/bin/python3.12 x.py",
                                 "mkdir -p reports", "cp a b", "rm x", "git push", "git -C x status", "git commit -m x",
                                 "find . -delete", "find . -exec ls ;", "pmset -a sleep 0", "open run.command",
                                 "open ./x.sh", "open ~/Downloads/a.pkg", "xargs ls", "'unbalanced",
                                 "git diff --ext-diff", "git diff --textconv", "git log --output=/tmp/x", "git show --output x",
                                 "git branch -D main", "git branch -d old", "git branch newname", "git branch -m a b",
                                 "open notes.txt", "open ./run", "open -n -a Safari", "open -a Safari --args --x",
                                 "open --env X=1 -a Safari", "open -b com.apple.Safari", "open -a /Applications/X.app",
                                 "open", "open file:///etc/passwd"])
def test_always_forced_to_once_unless_read_only(agw, cmd):
    g, c, _ = agw
    aid = c.post("/v1/approvals", json={**CMD, "command": cmd}).json()["approval_id"]
    r = c.post(f"/v1/approvals/{aid}", json={"decision": "always"}).json()
    assert r["status"] == "approved" and r["decision"] == "once" and r["forced_once"] is True, (cmd, r)
    assert "한 번만" in r["forced_reason"]
    assert c.get("/v1/approvals/rules").json() == []


@pytest.mark.parametrize("cmd", ['open -a "Google Chrome"', "open https://example.com", "ls -la", "cat a.txt", "head -n5 x",
                                 "tail x", "wc -l x", "du -sh .", "df -h", "pwd", "git status", "git log -5", "git diff",
                                 "git show HEAD", "git branch", "git branch -a -v", "rg foo .", "grep -r x .",
                                 "find . -name x", "sw_vers", "open mailto:a@b.c", "open -a Safari https://example.com",
                                 "uptime", "pmset -g batt", "system_profiler SPHardwareDataType"])
def test_always_allowed_for_read_only(agw, cmd):
    g, c, _ = agw
    aid = c.post("/v1/approvals", json={**CMD, "command": cmd}).json()["approval_id"]
    r = c.post(f"/v1/approvals/{aid}", json={"decision": "always"}).json()
    assert r["decision"] == "always" and "forced_once" not in r, (cmd, r)


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
    r = c.post("/v1/approvals", json={**SAFE, "requester": "task:abcdefabcdef"}).json()
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


def test_requester_per_ask_and_task(agw):
    """Two tasks and a chat at once: each approval belongs to the run that asked, never to whoever ran last."""
    g, c, _ = agw
    a = c.post("/v1/approvals", json={**SAFE, "requester": "task:aaaaaaaaaaaa"}).json()["approval_id"]
    b = c.post("/v1/approvals", json={**SAFE, "requester": "task:bbbbbbbbbbbb"}).json()["approval_id"]
    chat = c.post("/v1/approvals", json={**SAFE, "requester": "ask:luna:call-9"}).json()["approval_id"]
    none = c.post("/v1/approvals", json={**SAFE, "requester": "evil; rm"}).json()["approval_id"]  # malformed: dropped
    assert g.task_approvals["aaaaaaaaaaaa"] == {a} and g.task_approvals["bbbbbbbbbbbb"] == {b}
    assert c.get(f"/v1/approvals/{chat}").json()["requester"] == {"session": "luna:call-9", "persona": "luna"}
    assert c.get(f"/v1/approvals/{none}").json()["requester"] is None


def test_runs_tell_daemons_who_they_are(agw, monkeypatch):
    g, c, _ = agw
    devs = [{"id": "macbook", "name": "macbook", "url": "http://d/mcp"}]
    servers, *_ = g.run_args(asyncio.run(g.load_settings()), devs, g.requester_id("task", "abcabcabcabc"))
    assert servers["macbook"]["headers"]["X-Azel-Requester"] == "task:abcabcabcabc"
    assert g.requester_id("ask", "azel:call 1/x") == "ask:azel:call_1_x"  # only the daemon's allowed characters
    seen, orig = [], httpx.AsyncClient

    def handler(req):
        seen.append(req.headers.get("x-azel-requester"))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
    monkeypatch.setattr(g.httpx, "AsyncClient", lambda **kw: orig(transport=httpx.MockTransport(handler), **kw))

    async def api_path():  # what ask_stream sets for the API/NIM paths
        g.requester_ctx.set(g.requester_id("ask", "azel:call-2"))
        await g.mcp("http://d/mcp", "tools/list", {})
    asyncio.run(api_path())
    assert seen == ["ask:azel:call-2"]


def test_requester_does_not_outlive_its_ask(agw):
    g, c, _ = agw

    async def two():
        async for _ in g.ask_stream("서버 괜찮아?", "azel:call-7"):
            pass
        return g.requester_ctx.get()  # same task, after the ask: nothing left over
    assert asyncio.run(two()) == ""


@pytest.mark.parametrize("key,ok", [("approval-" + "0123456789ab", True), ("approval-rules", True), ("tasks-index", True),
                                    ("task-0123456789ab", True), ("alerts_recent", True), ("approval rules", False),
                                    ("Approval-Rules", False), ("a" * 65, False)])
def test_kv_keys_fit_the_memory_service(agw, key, ok):
    """fake_state uses memory_service.py's own KV_KEY, so the keys every flow here writes are known to be accepted."""
    g, c, _ = agw
    try:
        asyncio.run(g.state.call("PUT", f"/v1/state/kv/{key}", []))
        accepted = True
    except g.HTTPException as e:
        accepted, code = False, e.status_code
    assert accepted is ok, key
    if not ok:
        assert code == 422, (key, code)



def test_a_write_approval_push_says_always_is_not_offered(agw):
    g, c, pushes = agw
    c.post("/v1/approvals", json={**CMD, "command": "rm -rf build"})
    assert pushes[-1][2]["category"] == "APPROVAL_ONCE" and pushes[-1][2]["forced_once"] is True  # no 항상 허용 button
