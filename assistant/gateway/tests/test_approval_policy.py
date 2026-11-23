"""The default approval policy (user rule 2026-11-22): approve by default; ask only to delete, deploy and the like."""
import risk
from test_gateway import gw  # noqa: F401  (fixture)


def test_lenient_rules():
    for c in ("python3 make_pdf.py", "pip3 install weasyprint", "typst compile a.typ", "git commit -m x", "cp a b",
              "npm run build", "brew install ffmpeg", "echo hi > out.txt", "curl -s https://example.com"):
        assert risk.lenient(c) == risk.SAFE, c
    for c in ("rm -rf build", "cd /tmp && rm x", "git push origin main", "vercel deploy --prod", "cat ~/.ssh/id_rsa",
              "find . -name '*.pyc' -delete", "echo hi > /etc/hosts", "sudo reboot", "docker system prune -f",
              "scp a host:/tmp", "rsync -a out/ host:/srv/", "kubectl apply -f x.yaml", "npm publish"):
        assert risk.lenient(c) == risk.RISKY, c
    assert risk.lenient("python3 clean.py", script="import shutil\nshutil.rmtree('out')") == risk.RISKY
    assert risk.lenient("python3 chart.py", script="import matplotlib\nplt.savefig('a.png')") == risk.SAFE
    assert risk.lenient("python3 -c \"import os; os.remove('x')\"") == risk.RISKY


def test_auto_mode_approves_everything_but_the_dangerous(gw):  # noqa: F811
    g, c, _ = gw
    c.put("/v1/settings", json={"approval_mode": "auto"})
    ok = c.post("/v1/approvals", json={"device": "macbook", "tool": "run_command", "command": "pip3 install rich",
                                       "cwd": "/Users/jh/dev/x"}).json()
    assert ok["status"] == "approved" and ok["auto"]
    ask = c.post("/v1/approvals", json={"device": "macbook", "tool": "run_command", "command": "rm -rf out",
                                        "cwd": "/Users/jh/dev/x"}).json()
    assert ask["status"] == "pending"
    script = c.post("/v1/approvals", json={"device": "macbook", "tool": "claude:Bash", "command": "python3 wipe.py",
                                           "cwd": "/Users/jh/dev/x", "script": "import os\nos.remove('db.sqlite')"}).json()
    assert script["status"] == "pending"  # the script deletes: asked
    c.put("/v1/settings", json={"approval_mode": "ask"})
    assert c.post("/v1/approvals", json={"device": "macbook", "tool": "run_command", "command": "pip3 install rich",
                                         "cwd": "/Users/jh/dev/x"}).json()["status"] == "pending"
    assert c.put("/v1/settings", json={"approval_mode": "never"}).status_code == 400


def test_a_model_clears_what_the_regex_flags_except_the_few_that_always_ask(gw, monkeypatch):  # noqa: F811
    g, c, _ = gw
    c.put("/v1/settings", json={"approval_mode": "auto"})
    asked = []

    async def says_safe(system, user):
        asked.append(user)
        return {"tier": "safe", "confidence": 0.95}
    monkeypatch.setattr(g, "qwen_classify", says_safe)
    monkeypatch.setattr(g, "haiku_classify", says_safe)
    post = lambda cmd, **kw: c.post("/v1/approvals", json={"device": "macbook", "tool": "run_command",  # noqa: E731
                                                           "command": cmd, "cwd": "/Users/jh/dev/x", **kw}).json()
    assert post("rm -rf build")["status"] == "approved" and asked  # flagged by the regex, cleared by the models
    asked.clear()
    assert post("pip3 install rich")["status"] == "approved" and not asked  # the regex alone: no model call
    for cmd in ("sudo reboot", "rm -rf ~/", "git push --force origin main", "cat ~/.ssh/id_rsa"):
        assert post(cmd)["status"] == "pending", cmd  # always asks, whatever a model says

    async def says_risky(system, user):
        return {"tier": "risky", "confidence": 0.9}
    monkeypatch.setattr(g, "haiku_classify", says_risky)
    assert post("rm -rf src")["status"] == "pending"  # one judge says risky: asked


def test_a_pending_task_approval_reaches_its_live_activity(gw, monkeypatch):  # noqa: F811
    """Live 2026-11-22: a delegated job's approvals kept the task "running", and the locked phone's Live Activity never
    showed buttons. The task now waits visibly, and its activity is updated by push."""
    g, c, _ = gw
    sent = []

    async def fake_apns(payload, kind, tokens=None):
        sent.append((kind, payload, tokens))
        return {"sent": 1}
    monkeypatch.setattr(g, "apns_send", fake_apns)
    monkeypatch.setattr(g, "apns_config", lambda: {"team_id": "t"})
    t = {"id": "abcabcabcabc", "status": "running", "steps": [{"label": "맥북 작업 진행 중"}], "goal": "보고서"}
    g.task_live[t["id"]] = t
    tok = "ab" * 32
    assert c.post("/v1/push/register", json={"bundle_id": g.APP_BUNDLE, "activity_token": tok,
                                             "task_id": t["id"]}).json() == {"ok": True}
    aid = c.post("/v1/approvals", json={"device": "macbook", "tool": "claude:Bash", "command": "rm -rf old",
                                        "cwd": "/Users/jh/dev/x", "requester": f"task:{t['id']}"}).json()["approval_id"]
    assert t["status"] == "waiting_approval"
    kind, payload, tokens = next(x for x in sent if x[0] == "liveactivity")
    cs = payload["aps"]["content-state"]
    assert tokens[0]["token"] == tok and cs["status"] == "waiting_approval" and cs["approval"]["id"] == aid
    assert set(cs["approval"]) == {"id", "device", "command", "forcedOnce", "risky"} and isinstance(cs["steps"], int)
    sent.clear()
    assert c.post(f"/v1/approvals/{aid}", json={"decision": "once"}).status_code == 200
    kind, payload, _ = next(x for x in sent if x[0] == "liveactivity")
    assert t["status"] == "running" and "approval" not in payload["aps"]["content-state"]


def test_recent_calls_answer_what_a_missed_call_was_about(gw):  # noqa: F811
    """Live 2026-11-22 21:50: "전화 못 받았는데 방금 무슨 용건이었어?" was searched in memory and not found."""
    import asyncio
    g, _, _ = gw
    asyncio.run(g.remember_placed("c1", "autonomous", "토론지 확인", "대표님, 토론지 업로드가 확인되지 않아 전화드렸어요."))
    asyncio.run(g.remember_placed("c2", "task", "작업 끝남", "맡기신 보고서가 끝났어요."))
    asyncio.run(g.call_answered("c2"))
    r = asyncio.run(g.bridge.run("recent_calls", {}, caller="qwen"))
    assert r["ok"] and "토론지 확인 (못 받음)" in r["spoken"] and "작업 끝남 (받음)" in r["spoken"]
    assert r["spoken"].index("작업 끝남") < r["spoken"].index("토론지")  # newest first


def test_a_tap_during_the_policy_check_stands(gw, monkeypatch):  # noqa: F811
    """The model takes seconds and the app shows the row at once: if the user decides first, the policy's approve
    is not an error (it was a 409 that failed the daemon's command), and the user's decision is what counts."""
    g, c, _ = gw
    c.put("/v1/settings", json={"approval_mode": "auto"})

    async def tap_then_safe(system, user):  # runs on the app's loop: the user's tap goes through the route function
        rows = await g.list_approvals()
        aid = next(a["id"] for a in rows if a["command"] == "rm build.log")
        await g.decide(aid, {"decision": "deny"})
        return {"tier": "safe", "confidence": 0.95}
    monkeypatch.setattr(g, "qwen_classify", tap_then_safe)
    monkeypatch.setattr(g, "haiku_classify", tap_then_safe)
    r = c.post("/v1/approvals", json={"device": "macbook", "tool": "run_command", "command": "rm build.log",
                                      "cwd": "/Users/jh/dev/x"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert c.get(f"/v1/approvals/{r.json()['approval_id']}").json()["status"] == "rejected"
