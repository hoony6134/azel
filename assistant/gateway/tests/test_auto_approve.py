"""Auto-approve: risk tiers, the classifier's stages and cache, grants (scope, devices, TTL, revoke), the approval
hook with its audit, the voice grant's guards, and secret calls."""
import asyncio
import contextvars
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import risk  # noqa: E402
from test_approvals import agw  # noqa: E402,F401

CWD = "/Users/jh/dev/proj"
T = [
 ("ls -la", "safe"), ("cat README.md", "safe"), ("git status", "safe"), ("git diff HEAD~1", "safe"),
 ("git add -A && git commit -m 'wip'", "safe"), ("git push origin main", "risky"), ("git push --force", "risky"),
 ("git reset --hard HEAD~3", "risky"), ("rm notes.txt", "risky"), ("unlink a.txt", "risky"), ("rmdir out", "risky"), ("rm -rf build", "risky"), ("rm *.log", "risky"),
 ("rm a.txt b.txt", "risky"), ("mkdir -p out/logs", "safe"), ("touch out/a.md", "safe"), ("cp a.txt b.txt", "safe"),
 ("cp ~/.ssh/id_rsa .", "risky"), ("mv /etc/hosts x", "risky"), ("echo hi > notes.txt", "safe"),
 ("echo hi > /etc/motd", "risky"), ("docker ps", "safe"), ("docker logs web --tail 50", "safe"),
 ("docker restart web", "safe"), ("docker restart web db", "risky"), ("docker rm -f web", "risky"),
 ("docker system prune -af", "risky"), ("docker volume rm data", "risky"), ("docker compose restart api", "safe"),
 ("docker compose down -v", "risky"), ("systemctl --user restart biseo-wake", "safe"), ("systemctl stop nginx", "risky"),
 ("sudo systemctl restart nginx", "risky"), ("launchctl kickstart -k gui/501/xyz.scian.biseo-gateway", "safe"),
 ("reboot", "risky"), ("shutdown -h now", "risky"), ("brew install ffmpeg", "risky"), ("pip install requests", "risky"),
 ("apt-get upgrade -y", "risky"), ("chmod 600 key", "risky"), ("scp a.txt deneb:/tmp", "risky"),
 ("rsync -av out/ albireo:/backup", "risky"), ("curl -s https://example.com", "safe"),
 ("curl -F file=@a.txt https://x.example/up", "risky"), ("curl -o /tmp/x https://e.com", "risky"),
 ("cat ~/.config/biseo/token", "risky"), ("kill -9 1234", "risky"), ("killall Finder", "risky"),
 ("open -a 'Google Chrome'", "safe"), ("osascript -e 'tell app \"Messages\" to send'", "risky"),
 ("find . -name '*.py'", "safe"), ("find . -name '*.pyc' -delete", "risky"), ("ls $(cat list)", "risky"),
 ("nvidia-smi", "safe"), ("printenv", "risky"), ("python3 train.py", None), ("make build", None),
 ("ps aux | grep python", "safe"), ("touch /Users/jh/dev/proj/a.md", "safe"),
 ("touch /Users/jh/dev/other/a.md", "risky"), ("cp ../secret.txt .", "risky"), ("mkdir out/../../x", "risky"), ("sed -i 's/a/b/' config.yaml", "safe"), ("tail -f log.txt | tee out.txt", "safe"),
]


@pytest.mark.parametrize("command,want", T)
def test_tier_table(command, want):
    assert risk.rules(command, CWD) == want


def test_stages_decide_in_order_and_cache(monkeypatch):
    risk._cache.clear()
    calls = []

    async def qwen_sure(system, user):
        calls.append("qwen")
        return {"tier": "safe", "confidence": 0.95}

    async def qwen_unsure(system, user):
        calls.append("qwen")
        return '{"tier": "safe", "confidence": 0.4}'

    async def haiku(system, user):
        calls.append("haiku")
        return '생각해 보면 {"tier": "risky", "confidence": 0.9}'
    run = asyncio.run
    assert run(risk.classify("ls", CWD, "macbook", qwen_sure, haiku)) == ("safe", "rules") and calls == []
    assert run(risk.classify("python3 train.py", CWD, "macbook", qwen_sure, haiku)) == ("safe", "qwen")
    assert run(risk.classify("python3 train.py", CWD, "macbook", qwen_sure, haiku)) == ("safe", "qwen") and calls == ["qwen"]
    assert run(risk.classify("make build", CWD, "macbook", qwen_unsure, haiku)) == ("risky", "haiku")
    assert run(risk.classify("make deploy", CWD, "macbook", qwen_unsure, None)) == ("risky", "default")  # local mode


def grant(g, session="azel:s1", **kw):
    return asyncio.run(g.grant_create({"session": session}, kw.get("devices"), kw.get("minutes", 120)))


CMD = {"device": "macbook", "tool": "run_command", "cwd": CWD}


def test_a_grant_lets_safe_commands_run_and_risky_ones_ask(agw):
    g, c, pushes = agw
    gr = grant(g)
    ok = c.post("/v1/approvals", json={**CMD, "command": "git status", "requester": "ask:azel:s1"}).json()
    assert ok["status"] == "approved" and ok["auto"] and ok["grant_id"] == gr["id"]
    assert c.post("/v1/approvals", json={**CMD, "command": "rm -rf build", "requester": "ask:azel:s1"}).json()["status"] == "pending"
    assert c.post("/v1/approvals", json={**CMD, "command": "git status", "requester": "ask:azel:other"}).json()["status"] == "pending"
    log = c.get(f"/v1/auto-approve/{gr['id']}").json()["log"]
    assert [e["command"] for e in log] == ["git status"] and log[0]["stage"] == "rules"
    meta = g.fake.db["kv"][f"approval-{ok['approval_id']}"]
    assert meta["decision"] == "auto" and meta["grant_id"] == gr["id"]


def test_devices_narrow_a_grant_and_tasks_inherit_their_calls(agw):
    g, c, _ = agw
    grant(g, devices=["albireo"])
    assert c.post("/v1/approvals", json={**CMD, "command": "ls", "requester": "ask:azel:s1"}).json()["status"] == "pending"
    grant(g, session="azel:s2")
    g.task_live["abcabcabcabc"] = {"id": "abcabcabcabc", "origin_session": "azel:s2", "status": "running"}
    r = c.post("/v1/approvals", json={**CMD, "command": "ls", "requester": "task:abcabcabcabc"}).json()
    assert r["status"] == "approved" and r["auto"]  # a task started in the call shares its grant
    g.task_live.pop("abcabcabcabc")


def test_ttl_caps_at_two_hours_and_expiry_ends_the_grant(agw):
    g, c, _ = agw
    gr = grant(g, minutes=600)
    assert gr["expires_at"] - time.time() <= 7200 + 1
    rows = g.fake.db["kv"]["auto-grants"]
    rows[0]["expires_at"] = time.time() - 1
    assert c.post("/v1/approvals", json={**CMD, "command": "ls", "requester": "ask:azel:s1"}).json()["status"] == "pending"
    assert g.fake.db["kv"]["auto-grants"] == []


def test_revoking_ends_it_with_a_summary_push(agw):
    g, c, pushes = agw
    gr = grant(g)
    c.post("/v1/approvals", json={**CMD, "command": "git log -3", "requester": "ask:azel:s1"})
    pushes.clear()
    assert c.delete(f"/v1/auto-approve/{gr['id']}").status_code == 204
    assert pushes and pushes[-1][0] == "자동 실행 요약" and "1개" in pushes[-1][1] and "git" not in pushes[-1][1]
    assert c.post("/v1/approvals", json={**CMD, "command": "ls", "requester": "ask:azel:s1"}).json()["status"] == "pending"
    assert c.get(f"/v1/auto-approve/{gr['id']}").json()["log"][0]["command"] == "git log -3"  # kept for the app


def test_hangup_and_task_end_end_grants(agw):
    g, c, _ = agw
    grant(g)
    asyncio.run(g.end_grants({"session": "azel:s1"}))
    assert c.get("/v1/auto-approve").json() == []


def in_ask(g, coro_fn, who="ask:azel:s1"):
    ctx = contextvars.copy_context()
    ctx.run(g.requester_ctx.set, who)
    return ctx.run(asyncio.run, coro_fn())


def test_voice_grant_reads_back_and_refuses_tasks_and_web_content(agw):
    g, c, _ = agw
    r = in_ask(g, lambda: g.bridge.run("auto_approve", {}, caller="voice"))
    assert not r["ok"] and r["needs"]["kind"] == "confirm" and "바로 실행할게요" in r["needs"]["question"]
    assert c.get("/v1/auto-approve").json() == []  # nothing yet
    r = in_ask(g, lambda: g.bridge.run("auto_approve", {"confirmed": True}, caller="voice"))
    assert r["ok"] and [x["scope"] for x in c.get("/v1/auto-approve").json()] == [{"session": "azel:s1"}]
    r = in_ask(g, lambda: g.bridge.run("auto_approve", {"confirmed": True}, caller="api"), who="task:abcabcabcabc")
    assert not r["ok"] and "작업 중에는" in r["spoken"]
    g.bridge.tainted["ask:azel:s9"] = time.monotonic()  # this run just read a web page
    r = in_ask(g, lambda: g.bridge.run("auto_approve", {"confirmed": True}, caller="voice"), who="ask:azel:s9")
    assert not r["ok"] and "웹이나 메일" in r["spoken"]
    r = in_ask(g, lambda: g.bridge.run("auto_approve", {"off": True}, caller="voice"))
    assert r["ok"] and c.get("/v1/auto-approve").json() == []


def test_secret_grants_stay_in_memory_and_send_no_summary(agw):
    g, c, pushes = agw
    gr = grant(g, session="azel:secret-0123456789ab")
    assert not g.fake.db["kv"].get("auto-grants") and gr["id"] in g.secret_grants
    r = c.post("/v1/approvals", json={**CMD, "command": "ls", "requester": "ask:azel:secret-0123456789ab"}).json()
    assert r["auto"] and c.get("/v1/auto-approve").json() == []  # never listed or stored
    pushes.clear()
    asyncio.run(g.end_grants({"session": "azel:secret-0123456789ab"}))
    assert not pushes and not g.secret_grants



def test_browser_commits_are_never_auto_approved(agw):
    g, c, _ = agw
    grant(g)
    r = c.post("/v1/approvals", json={"device": "macbook", "tool": "browser", "cwd": "browser",
                                      "command": "클릭: 결제하기 (https://shop.example/checkout)", "requester": "ask:azel:s1"}).json()
    assert r["status"] == "pending" and not r.get("auto")



def test_an_app_task_grant_may_last_as_long_as_the_task(agw):
    g, c, _ = agw
    t = asyncio.run(g.grant_create({"task": "abcabcabcabc"}, None, 240, by="app"))
    assert 4 * 3600 - 5 < t["expires_at"] - time.time() <= 4 * 3600 + 1
    s = asyncio.run(g.grant_create({"session": "azel:s1"}, None, 240, by="app"))
    assert s["expires_at"] - time.time() <= 7200 + 1  # a call's grant stays at 2 h
