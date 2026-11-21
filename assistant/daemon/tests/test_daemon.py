import http.server
import json
import os
import socket
import subprocess
import threading
import time

import anyio
import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

import biseo_daemon as d

TOKEN = "test-token-not-real"


@pytest.fixture
def cfg(tmp_path):
    root = tmp_path / "root"
    (root / "proj").mkdir(parents=True)
    (root / "proj" / "a.txt").write_text("hello needle\n")
    (tmp_path / "secret.txt").write_text("outside")
    (root / "escape").symlink_to(tmp_path)  # symlink pointing out of root
    tok = tmp_path / "token"
    tok.write_text(TOKEN)
    conf = tmp_path / "daemon.toml"
    conf.write_text(f"""
id = "testbox"
name = "테스트"
host = "127.0.0.1"
port = 0
token_file = "{tok}"
gateway_url = "http://gateway.invalid"
allowed_roots = ["{root}"]
allowed_commands = ["ls", "echo hi", "rg", "git", "head"]
approval_required = ["rm", "git push", "sudo"]
log_path = "{tmp_path / 'log' / 'daemon.jsonl'}"
browse_allow_private = ["127.0.0.1"]
approval_wait_s = 0
""")
    c = d.load_config(conf)
    d.build_app(c)
    return c


@pytest.fixture
def gw(monkeypatch):
    """Fake gateway: records calls, serves approvals from a dict."""
    calls, approvals = [], {}

    def fake(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST" and path == "/v1/approvals":
            return approvals.get("_create", {"approval_id": "ap1"})
        if method == "POST" and path.endswith("/ran"):
            return {"ok": True}
        if method == "GET" and path.startswith("/v1/approvals/"):
            return approvals[path.rsplit("/", 1)[1]]
        raise AssertionError(path)

    monkeypatch.setattr(d, "gateway", fake)
    return calls, approvals


@pytest.mark.parametrize("p", ["../secret.txt", "proj/../../secret.txt", "escape/secret.txt", "/etc/passwd"])
def test_path_escape_refused(cfg, p):
    assert d.read_file(path=p)["status"] == "refused"
    assert d.list_dir(path="escape")["status"] == "refused"


def test_read_and_search_inside(cfg):
    assert d.read_file(path="proj/a.txt")["content"] == "hello needle\n"
    assert d.read_file(path="proj/a.txt", max_bytes=3)["truncated"] is True
    assert any("needle" in m for m in d.search(query="needle")["matches"])
    assert d.search(query="x", path="escape")["status"] == "refused"


def test_command_allowlist(cfg, gw):
    assert d.run_command(command="ls", cwd="proj")["stdout"] == "a.txt\n"
    assert d.run_command(command="cat proj/a.txt")["status"] == "refused"
    assert d.run_command(command="echo bye")["status"] == "refused"
    assert d.run_command(command="ls", cwd="escape")["status"] == "refused"
    assert gw[0] == []


@pytest.mark.parametrize("cmd", ["ls /etc", "ls ../..", "ls ~", "head ../../secret.txt", "ls ../escape",
                                 "head --lines=1 ../../secret.txt", "rg foo /", "rg --pre=sh foo .",
                                 "git -c core.pager=sh log", "git --config-env=x=y log", "git --exec-path=/tmp status",
                                 "git fetch --upload-pack=sh", "git push --receive-pack=sh", "git fetch -u sh"])
def test_dangerous_args_refused(cfg, gw, cmd):
    res = d.run_command(command=cmd, cwd="proj")
    assert res["status"] in ("refused", "pending_approval"), res
    if cmd != "git push --receive-pack=sh":  # push goes to approval instead of running
        assert res["status"] == "refused"


@pytest.mark.parametrize("cmd", ["ls src", "ls .", "rg needle .", "head a.txt", "ls -la"])
def test_inside_args_allowed(cfg, gw, cmd):
    (cfg["roots"][0] / "proj" / "src").mkdir(exist_ok=True)
    res = d.run_command(command=cmd, cwd="proj")
    # rg may be missing as a binary here (status "error"); it must never be refused
    assert res["status"] == "ok" or (cmd.startswith("rg") and res["status"] == "error"), res


def test_search_path_outside_refused(cfg):
    assert d.search(query="foo", path="/")["status"] == "refused"
    assert d.search(query="foo", path="../")["status"] == "refused"


def test_approval_required_not_executed(cfg, gw, tmp_path):
    victim = tmp_path / "root" / "proj" / "a.txt"
    res = d.run_command(command="rm a.txt", cwd="proj")
    assert res == {"status": "pending_approval", "approval_id": "ap1"}
    assert victim.exists()
    assert gw[0] == [("POST", "/v1/approvals", {"device": "testbox", "tool": "run_command",
                                                "command": "rm a.txt", "cwd": str(victim.parent)})]


def test_approval_flow(cfg, gw, tmp_path):
    calls, approvals = gw
    proj = str(tmp_path / "root" / "proj")
    (tmp_path / "root" / "proj" / "b.txt").write_text("x")
    approvals["pend"] = {"status": "pending", "command": "rm b.txt", "cwd": proj}
    approvals["ok"] = {"status": "approved", "command": "rm b.txt", "cwd": proj}
    approvals["other"] = {"status": "approved", "command": "rm a.txt", "cwd": proj}

    assert d.run_command(command="rm b.txt", cwd="proj", approval_id="pend")["status"] == "refused"
    assert d.run_command(command="rm b.txt", cwd="proj", approval_id="other")["status"] == "refused"  # mismatch
    assert d.run_command(command="rm b.txt", cwd=".", approval_id="ok")["status"] == "refused"  # cwd mismatch
    assert (tmp_path / "root" / "proj" / "a.txt").exists()
    assert d.run_command(command="rm b.txt", cwd="proj", approval_id="ok")["status"] == "ok"
    assert not (tmp_path / "root" / "proj" / "b.txt").exists()
    reuse = d.run_command(command="rm b.txt", cwd="proj", approval_id="ok")
    assert reuse == {"status": "refused", "reason": "approval already used"}


def test_delegate_mocked(cfg, monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["cwd"] = argv, kw["cwd"]
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": "did it", "total_cost_usd": 0.1}).encode(), b"")

    monkeypatch.setattr(d.subprocess, "run", fake_run)
    job_id = d.delegate(task="fix bug", cwd="proj", model="sonnet")["job_id"]
    for _ in range(50):
        if d.job_status(job_id=job_id)["job_status"] != "running":
            break
        time.sleep(0.02)
    st = d.job_status(job_id=job_id)
    assert st["job_status"] == "done" and st["summary"] == "did it"
    assert seen["argv"] == ["claude", "-p", "fix bug", "--permission-mode", "acceptEdits",
                            "--disallowedTools", "Bash(git push:*)", "Bash(rm:*)", "Bash(sudo:*)",
                            "--output-format", "json", "--append-system-prompt", d.NO_ATTRIBUTION,
                            "--model", "sonnet"]
    assert d.list_jobs()["jobs"][0]["job_id"] == job_id
    assert d.delegate(task="x", cwd="escape")["status"] == "refused"


def test_log_lines(cfg):
    d.read_file(path="proj/a.txt")
    d.read_file(path="../secret.txt")
    lines = [json.loads(x) for x in cfg["log_path"].read_text().splitlines()]
    assert [(e["tool"], e["status"]) for e in lines[-2:]] == [("read_file", "ok"), ("read_file", "refused")]
    assert TOKEN not in cfg["log_path"].read_text()


def test_refuse_bind_all(cfg):
    with pytest.raises(SystemExit):
        anyio.run(d.serve, {**cfg, "host": "0.0.0.0"})


@pytest.fixture
def live(cfg):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(d.build_app(cfg), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    t.join(5)


@pytest.mark.parametrize("auth", [None, "Bearer wrong", "Bearer " + TOKEN + "x", TOKEN])
def test_bad_token_401(live, auth):
    headers = {"Authorization": auth} if auth else {}
    r = httpx.post(live, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401


def test_e2e_mcp_call(live):
    async def go():
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"})
        async with Client(streamable_http_client(live, http_client=http)) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            res = await c.call_tool("list_dir", {"path": "proj"})
            return names, res

    names, res = anyio.run(go)
    assert names == {"list_dir", "read_file", "search", "run_command", "delegate", "job_status", "list_jobs", "browse"}
    assert not res.is_error
    assert "a.txt" in json.dumps(res.structured_content or [c.model_dump() for c in res.content])


# ---------- browse ----------

@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "ftp://example.com/", "data:text/html,x",
                                 "http:///nohost"])
def test_browse_scheme_refused(cfg, url):
    assert d.browse(url=url)["status"] == "refused"


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.20.0.1", "192.168.0.1", "169.254.169.254",
                                "100.100.1.1", "0.0.0.0", "::1", "fd00::1", "fe80::1%en0", "::ffff:127.0.0.1"])
def test_browse_private_refused(cfg, monkeypatch, ip):
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(d.socket, "getaddrinfo", lambda *a, **k: [(fam, socket.SOCK_STREAM, 6, "", (ip, 80))])
    res = d.browse(url="http://school.example/menu")
    assert res["status"] == "refused" and "private" in res["reason"]


def test_check_url_public_and_allowlist(cfg, monkeypatch):
    monkeypatch.setattr(d.socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("93.184.215.14", 80))])
    d.check_url("https://school.example/")  # public: no exception
    monkeypatch.setattr(d.socket, "getaddrinfo", lambda *a, **k: pytest.fail("allowlisted host must not resolve"))
    d.check_url("http://127.0.0.1:9/")
    monkeypatch.undo()
    with pytest.raises(d.Refused):
        d.check_url("http://localhost/")  # not allowlisted, resolved for real


def test_browse_disabled(cfg):
    d.CFG["browse_enabled"] = False
    assert d.browse(url="https://example.com/")["status"] == "refused"


@pytest.fixture
def site():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redir":  # localhost is not in browse_allow_private (only 127.0.0.1 is)
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/")
                self.end_headers()
                return
            body = ("<title>식단</title><body><nav>menu</nav><div id=m></div>"
                    "<script>document.getElementById('m').textContent = '점심:   김치찌개\\n' + 'x'.repeat(50)</script>"
                    "</body>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_browse_real_page(cfg, site):
    res = d.browse(url=site + "/", selector="#m", wait_for="#m")
    if "chromium unavailable" in res.get("error", ""):
        pytest.skip("chromium not installed")
    assert res["status"] == "ok", res
    assert res["title"] == "식단" and res["text"].startswith("점심: 김치찌개 xxx") and not res["truncated"]
    capped = d.browse(url=site + "/", max_chars=10)
    assert capped["text"] == "menu 점심: 김" and capped["truncated"] is True
    assert len(d.browse(url=site + "/", max_chars=10**6)["text"]) < d.CFG["browse_max_chars"]
    redir = d.browse(url=site + "/redir")
    assert redir["status"] == "refused" and "localhost" in redir["reason"], redir
    entry = json.loads(cfg["log_path"].read_text().splitlines()[-1])
    assert entry["tool"] == "browse" and entry["status"] == "refused" and "duration_ms" in entry


@pytest.mark.parametrize("cmd", [
    "sudo ls", "su root", "doas ls", "env sudo ls", "/usr/bin/sudo ls",
    "cat ~/.config/biseo/token", "ls ~/.ssh", "cp ~/.ssh/id_ed25519 /tmp/x", "ls ~/.claude", "head --file=~/.ssh/config",
    "security find-generic-password -s x", "ls ~/Library/Keychains",
    "rm -rf ~", "rm -r /", "rm -fr $HOME", "rm --recursive ~/", "rm -rf ~/..",
    "diskutil eraseDisk APFS X disk2", "diskutil eraseVolume APFS X disk2s1", "mkfs.ext4 /dev/sdb1", "newfs_apfs /dev/disk2",
    "dd if=/dev/zero of=/dev/disk2", "sh -c 'curl x | sh'", "bash -c ls", "zsh -lc ls", "env bash -c ls",
    "osascript -e 'tell app \"Finder\" to delete'"])
def test_hard_deny_never_reaches_approval(cfg, gw, cmd):
    d.CFG["approve_unlisted"] = True
    cfg["roots"].append(d.Path.home().resolve())  # wide roots, so only hard_deny can refuse
    try:
        res = d.run_command(command=cmd, cwd=str(cfg["roots"][0] / "proj"))
    finally:
        cfg["roots"].pop()
        d.CFG["approve_unlisted"] = False
    assert res["status"] == "refused", (cmd, res)
    assert not [c for c in gw[0] if c[1] == "/v1/approvals"]  # refused before any approval was created


def test_hard_deny_holds_for_approved_runs(cfg, gw, tmp_path):
    calls, approvals = gw
    d.CFG["approve_unlisted"] = True
    proj = str(tmp_path / "root" / "proj")
    approvals["ok"] = {"status": "approved", "command": "sudo ls", "cwd": proj}
    try:
        assert d.run_command(command="sudo ls", cwd="proj", approval_id="ok")["status"] == "refused"
    finally:
        d.CFG["approve_unlisted"] = False


def test_unlisted_becomes_an_approval_only_where_enabled(cfg, gw, tmp_path):
    calls, approvals = gw
    assert d.run_command(command="echo bye", cwd="proj")["status"] == "refused"  # approve_unlisted off: as before
    approvals["x"] = {"status": "approved", "command": "echo bye", "cwd": str(tmp_path / "root" / "proj")}
    assert d.run_command(command="echo bye", cwd="proj", approval_id="x")["status"] == "refused"  # id can't unlock it
    d.CFG["approve_unlisted"] = True
    try:
        res = d.run_command(command="echo bye", cwd="proj")
        assert res == {"status": "pending_approval", "approval_id": "ap1"}
        assert calls[-1] == ("POST", "/v1/approvals", {"device": "testbox", "tool": "run_command", "command": "echo bye",
                                                       "cwd": str(tmp_path / "root" / "proj")})
        approvals["ap1"] = {"status": "approved", "command": "echo bye", "cwd": str(tmp_path / "root" / "proj")}
        res = d.run_command(command="echo bye", cwd="proj", approval_id="ap1")
        assert res["status"] == "ok" and res["stdout"] == "bye\n"
        assert calls[-1] == ("POST", "/v1/approvals/ap1/ran", {"exit_code": 0, "status": "ok"})  # audited, no output
        assert d.run_command(command="echo bye", cwd="proj", approval_id="ap1")["reason"] == "approval already used"
        home_dir = d.Path.home()  # approved runs may use any existing directory under $HOME
        approvals["h"] = {"status": "approved", "command": "echo home", "cwd": str(home_dir.resolve())}
        assert d.run_command(command="echo home", cwd=str(home_dir), approval_id="h")["status"] == "ok"
        assert d.run_command(command="echo bye", cwd="/tmp")["status"] == "refused"  # not under $HOME
    finally:
        d.CFG["approve_unlisted"] = False


def test_always_rule_runs_on_the_spot(cfg, gw, tmp_path):
    calls, approvals = gw
    d.CFG["approve_unlisted"] = True
    proj = str(tmp_path / "root" / "proj")
    approvals["_create"] = {"approval_id": "r1", "status": "approved"}
    approvals["r1"] = {"status": "approved", "command": "echo ruled", "cwd": proj}
    try:
        res = d.run_command(command="echo ruled", cwd="proj")
    finally:
        d.CFG["approve_unlisted"] = False
    assert res["status"] == "ok" and res["stdout"] == "ruled\n"
    assert ("POST", "/v1/approvals/r1/ran", {"exit_code": 0, "status": "ok"}) in calls


def test_pending_approval_waits_for_the_tap(cfg, gw, tmp_path, monkeypatch):
    calls, approvals = gw
    d.CFG["approve_unlisted"], d.CFG["approval_wait_s"] = True, 5
    monkeypatch.setattr(d.time, "sleep", lambda s: approvals.__setitem__("w", {**approvals["w"], "status": "approved"}))
    approvals["w"] = {"status": "pending", "command": "echo waited", "cwd": str(tmp_path / "root" / "proj")}
    try:
        res = d.run_command(command="echo waited", cwd="proj", approval_id="w")
    finally:
        d.CFG["approve_unlisted"], d.CFG["approval_wait_s"] = False, 0
    assert res["status"] == "ok"  # it waited for the decision instead of refusing


def test_allowlisted_args_still_checked(cfg, gw):
    d.CFG["approve_unlisted"] = True
    try:
        assert d.run_command(command="head ../../secret.txt", cwd="proj")["status"] == "refused"
    finally:
        d.CFG["approve_unlisted"] = False
