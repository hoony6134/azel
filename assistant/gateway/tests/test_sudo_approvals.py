"""sudo: run only after the user taps approve, every time (user decision 2026-11-23). Never auto, never 항상 허용."""
from test_approvals import agw  # noqa: F401  (fixture)

SUDO = {"device": "macbook", "tool": "run_command", "command": "sudo -n softwareupdate -l", "cwd": "/Users/jh/dev/proj/app",
        "in_roots": True, "requester": "ask:azel:call-1"}


def test_sudo_is_never_approved_without_a_tap(agw, monkeypatch):  # noqa: F811
    g, c, pushes = agw

    async def safe(*a, **k):
        return g.risk.SAFE, "fake"  # even a classifier that calls it safe
    monkeypatch.setattr(g.risk, "lenient_classify", safe)
    monkeypatch.setattr(g.risk, "classify", safe)
    c.put("/v1/settings", json={"approval_mode": "auto"})
    assert c.portal.call(g.grant_create, {"session": "azel:call-1"}, None, 120)  # "알아서 해" for this call
    rules = [{"id": "r1", "device": "macbook", "command": SUDO["command"], "cwd": SUDO["cwd"], "created": 0}]
    c.portal.call(g.state.call, "PUT", "/v1/state/kv/approval-rules", rules)  # even a stored rule for it
    r = c.post("/v1/approvals", json=SUDO).json()
    assert r["status"] == "pending" and not r.get("auto"), r
    plain = c.post("/v1/approvals", json={**SUDO, "command": "pip3 install rich"}).json()
    assert plain["status"] == "approved"  # the same settings still approve an ordinary command


def test_sudo_push_list_and_decision_are_emphasised_and_once_only(agw):  # noqa: F811
    g, c, pushes = agw
    aid = c.post("/v1/approvals", json=SUDO).json()["approval_id"]
    title, body, extra = pushes[-1]
    assert title == "관리자 권한(sudo) 승인 요청" and "관리자 권한(sudo)" in body
    assert extra["category"] == "APPROVAL_RISKY" and extra["sudo"] is True and extra["tier"] == "sudo" and extra["forced_once"]
    item = next(a for a in c.get("/v1/approvals").json() if a["id"] == aid)
    assert item["tier"] == "sudo" and item["sudo"] is True and item["forced_once"] and item["forced_reason"] == g.SUDO_ONCE
    out = c.post(f"/v1/approvals/{aid}", json={"decision": "always"}).json()
    assert out["decision"] == "once" and out["forced_reason"] == g.SUDO_ONCE and "rule_id" not in out
    assert not c.get("/v1/approvals/rules").json()  # no standing rule for sudo


def test_plain_commands_are_unchanged(agw):  # noqa: F811
    g, c, pushes = agw
    c.post("/v1/approvals", json={**SUDO, "command": "rm -rf build"})
    title, _, extra = pushes[-1]
    assert title == "승인 요청" and extra["category"] == "APPROVAL_ONCE" and "sudo" not in extra
    assert g.tools_bridge.ask_text("sudo -n ls").startswith("관리자 권한(sudo) 명령이에요")
    assert g.tools_bridge.ask_text("ls -la") == g.tools_bridge.ASK_APPROVAL and not g.is_sudo("echo sudo")
