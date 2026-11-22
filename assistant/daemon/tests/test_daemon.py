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
        if method == "POST" and path.endswith("/use"):  # state's atomic spend: approved, same device/command/cwd, once
            a = approvals.get(path.split("/")[-2], {})
            if a.get("status") != "approved" or any(a.get(k) != body[k] for k in ("device", "command", "cwd")):
                raise httpx.HTTPStatusError("409", request=httpx.Request("POST", path), response=httpx.Response(409))
            a["status"] = "used"
            return {"status": "used"}
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
                                                "command": "rm a.txt", "cwd": str(victim.parent), "in_roots": True})]


def test_approval_flow(cfg, gw, tmp_path):
    calls, approvals = gw
    proj = str(tmp_path / "root" / "proj")
    (tmp_path / "root" / "proj" / "b.txt").write_text("x")
    approvals["pend"] = {"device": "testbox", "status": "pending", "command": "rm b.txt", "cwd": proj}
    approvals["ok"] = {"device": "testbox", "status": "approved", "command": "rm b.txt", "cwd": proj}
    approvals["other"] = {"device": "testbox", "status": "approved", "command": "rm a.txt", "cwd": proj}

    assert d.run_command(command="rm b.txt", cwd="proj", approval_id="pend")["status"] == "refused"
    assert d.run_command(command="rm b.txt", cwd="proj", approval_id="other")["status"] == "refused"  # mismatch
    assert d.run_command(command="rm b.txt", cwd=".", approval_id="ok")["status"] == "refused"  # cwd mismatch
    assert (tmp_path / "root" / "proj" / "a.txt").exists()
    assert d.run_command(command="rm b.txt", cwd="proj", approval_id="ok")["status"] == "ok"
    assert not (tmp_path / "root" / "proj" / "b.txt").exists()
    reuse = d.run_command(command="rm b.txt", cwd="proj", approval_id="ok")
    assert reuse["status"] == "refused"  # spent centrally: "used" now


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
                            "--allowedTools", *d.DEFAULTS["delegate_allowed_tools"],
                            "--disallowedTools", "Bash(git push:*)", "Bash(rm:*)", "Bash(sudo:*)",
                            "--output-format", "json", "--append-system-prompt", d.NO_ATTRIBUTION,
                            "--model", "sonnet"]
    assert d.list_jobs()["jobs"][0]["job_id"] == job_id
    assert d.delegate(task="x", cwd="escape")["status"] == "refused"


def test_delegate_token_only_for_the_delegate(cfg, monkeypatch, tmp_path):
    seen = []

    def fake_run(argv, **kw):
        seen.append((argv[0], kw.get("env", {}).get("CLAUDE_CODE_OAUTH_TOKEN")))
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": "ok"}).encode(), b"")

    def wait(job_id):
        for _ in range(100):
            if d.job_status(job_id=job_id)["job_status"] != "running":
                break
            time.sleep(0.02)

    monkeypatch.setattr(d.subprocess, "run", fake_run)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    d.CFG["claude_token_file"] = str(tmp_path / "missing")  # never read the real ~/.config token in tests
    wait(d.delegate(task="a", cwd="proj")["job_id"])  # no token file: nothing is added
    tok = tmp_path / "claude_oauth_token"
    tok.write_text("long-lived-token\n")
    d.CFG["claude_token_file"] = str(tok)
    tok.chmod(0o644)
    assert d.delegate(task="b", cwd="proj")["status"] == "refused"  # readable by others
    tok.chmod(0o600)
    wait(d.delegate(task="c", cwd="proj")["job_id"])
    assert [s[1] for s in seen] == [None, "long-lived-token"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ  # the daemon's own environment stays clean
    assert d.run_command(command="echo hi", cwd="proj")["status"] == "ok"  # run_command never sees it


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
    assert names == {"list_dir", "read_file", "search", "run_command", "delegate", "job_status", "list_jobs", "browse",
                     *(["music", "home"] if __import__("sys").platform == "darwin" else [])}
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
    approvals["ok"] = {"device": "testbox", "status": "approved", "command": "sudo ls", "cwd": proj}
    try:
        assert d.run_command(command="sudo ls", cwd="proj", approval_id="ok")["status"] == "refused"
    finally:
        d.CFG["approve_unlisted"] = False


def test_unlisted_becomes_an_approval_only_where_enabled(cfg, gw, tmp_path):
    calls, approvals = gw
    assert d.run_command(command="echo bye", cwd="proj")["status"] == "refused"  # approve_unlisted off: as before
    approvals["x"] = {"device": "testbox", "status": "approved", "command": "echo bye", "cwd": str(tmp_path / "root" / "proj")}
    assert d.run_command(command="echo bye", cwd="proj", approval_id="x")["status"] == "refused"  # id can't unlock it
    d.CFG["approve_unlisted"] = True
    try:
        res = d.run_command(command="echo bye", cwd="proj")
        assert res == {"status": "pending_approval", "approval_id": "ap1"}
        assert calls[-1] == ("POST", "/v1/approvals", {"device": "testbox", "tool": "run_command", "command": "echo bye",
                                                       "cwd": str(tmp_path / "root" / "proj"), "in_roots": True})
        approvals["ap1"] = {"device": "testbox", "status": "approved", "command": "echo bye", "cwd": str(tmp_path / "root" / "proj")}
        res = d.run_command(command="echo bye", cwd="proj", approval_id="ap1")
        assert res["status"] == "ok" and res["stdout"] == "bye\n"
        assert calls[-1] == ("POST", "/v1/approvals/ap1/ran", {"exit_code": 0, "status": "ok"})  # audited, no output
        assert d.run_command(command="echo bye", cwd="proj", approval_id="ap1")["status"] == "refused"  # used
        import tempfile
        with tempfile.TemporaryDirectory(dir=d.Path.home()) as sub:  # any existing directory under $HOME …
            approvals["h"] = {"device": "testbox", "status": "approved", "command": "echo home", "cwd": str(d.Path(sub).resolve())}
            assert d.run_command(command="echo home", cwd=sub, approval_id="h")["status"] == "ok"
        approvals["hh"] = {"device": "testbox", "status": "approved", "command": "echo home", "cwd": str(d.Path.home().resolve())}
        assert d.run_command(command="echo home", cwd=str(d.Path.home()), approval_id="hh")["status"] == "refused"  # … not ~
        assert d.run_command(command="echo bye", cwd="/tmp")["status"] == "refused"  # not under $HOME
    finally:
        d.CFG["approve_unlisted"] = False


def test_always_rule_runs_on_the_spot(cfg, gw, tmp_path):
    calls, approvals = gw
    d.CFG["approve_unlisted"] = True
    proj = str(tmp_path / "root" / "proj")
    approvals["_create"] = {"approval_id": "r1", "status": "approved"}
    approvals["r1"] = {"device": "testbox", "status": "approved", "command": "echo ruled", "cwd": proj}
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
    approvals["w"] = {"device": "testbox", "status": "pending", "command": "echo waited", "cwd": str(tmp_path / "root" / "proj")}
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


def test_approval_for_another_device_or_replayed(cfg, gw, tmp_path):
    calls, approvals = gw
    proj = str(tmp_path / "root" / "proj")
    approvals["x"] = {"device": "albireo", "status": "approved", "command": "rm b.txt", "cwd": proj}
    (tmp_path / "root" / "proj" / "b.txt").write_text("x")
    assert d.run_command(command="rm b.txt", cwd="proj", approval_id="x")["status"] == "refused"  # not ours
    approvals["y"] = {"device": "testbox", "status": "approved", "command": "rm b.txt", "cwd": proj}
    assert d.run_command(command="rm b.txt", cwd="proj", approval_id="y")["status"] == "ok"
    (tmp_path / "root" / "proj" / "b.txt").write_text("x")
    d._used_file().unlink()  # even with the local used-ids file gone, state refuses a second use
    assert d.run_command(command="rm b.txt", cwd="proj", approval_id="y")["status"] == "refused"
    assert (tmp_path / "root" / "proj" / "b.txt").exists()


@pytest.mark.parametrize("cmd", [
    "SUDO ls", "Sudo ls", "BASH -c ls", "Security dump-keychain", "env -S 'sudo ls'", "env --split-string='bash -c ls'",
    "env -Sbash\\ -c\\ ls", "xargs sudo ls", "nice -n 5 bash -c ls", "nohup zsh -c ls", "time sh -c ls",
    "timeout 5 bash -c ls", "caffeinate -i sh -c ls", "command bash -c ls", "find . -exec sh -c ls ;",
    "find ~ -delete", "find / -name x -delete", "env rm -rf ~", "xargs rm -rf /", "nice dd if=/dev/zero of=/dev/disk3",
    "env diskutil eraseDisk APFS X disk2", "diskutil apfs deleteContainer disk3", "diskutil APFS eraseVolume disk3s1",
    "time mkfs.ext4 /dev/sdb", "python -c print(1)", "python3 -c x", "/usr/bin/python3.12 -c x", "Python3 -c x",
    "perl -e 1", "perl -pe s/a/b/ f", "ruby -e 1", "node -e 1", "node --eval=1", "php -r 1", "lua -e 1",
    "osascript script.scpt", "osascript", "open -a Terminal", "open -a iTerm", "open -a /Applications/Utilities/Terminal.app",
    "open -b com.apple.Terminal", "open run.command", "open x.sh", "open a.scpt",
    "ls ~/.claude", "cat x/.claude/settings.json", "git -C ~/.ssh log", "tar -C/Users -xf a", "rg --path=~/.ssh x",
    "ls ~", "ls /Users", "ls /", "ls ~/.config", "rg x ~", "head -T~/.config/biseo/token"])
def test_hard_deny_bypasses_closed(cfg, gw, cmd):
    d.CFG["approve_unlisted"] = True
    cfg["roots"].append(d.Path.home().resolve())
    try:
        res = d.run_command(command=cmd, cwd=str(cfg["roots"][0] / "proj"))
    finally:
        cfg["roots"].pop()
        d.CFG["approve_unlisted"] = False
    assert res["status"] == "refused", (cmd, res)
    assert not [c for c in gw[0] if c[1] == "/v1/approvals"], cmd


def test_hard_deny_symlink_into_secrets(cfg, gw, tmp_path):
    link = cfg["roots"][0] / "proj" / "keys"
    link.symlink_to(d.Path("~/.ssh").expanduser())
    d.CFG["approve_unlisted"] = True
    try:
        assert d.run_command(command="ls keys", cwd="proj")["status"] == "refused"  # resolved before checking
    finally:
        d.CFG["approve_unlisted"] = False


@pytest.mark.parametrize("cmd", ['open -a "Google Chrome"', "open https://example.com", "python3 tool.py",
                                 "mkdir -p reports", "cp a.txt b.txt"])
def test_ordinary_commands_still_reach_approval(cfg, gw, cmd):
    d.CFG["approve_unlisted"] = True
    try:
        res = d.run_command(command=cmd, cwd="proj")
    finally:
        d.CFG["approve_unlisted"] = False
    assert res["status"] == "pending_approval", (cmd, res)


ROUND3 = [
    # 2: attached short-option values are always path-checked, and -C moves where later relative paths land
    "tar -czf /tmp/o.tgz -C ~ .config", "tar -czf /tmp/o.tgz --directory=/Users/x/.. jh",
    "rg -uuu BEGIN -g.ssh", "head -n1 -T" + "../" * 20,
    # 3: inline-code tools and command-running options
    "awk BEGIN{system(1)}", "gawk 1", "nawk 1", "expect -c x", "sqlite3 x .shell", "vim -c q", "vi x", "nvim x",
    "emacs --batch", "tmux new", "screen -dm x", "bun x.js", "deno run x.ts", "php x.php", "make", "make build",
    "xcrun simctl", "git -c core.pager=x log", "git -ccore.pager=x log", "git --config-env=x=y log",
    "git --exec-path=/tmp log", "git fetch --upload-pack=x", "git push --receive-pack=x", "git config core.sshCommand x",
    "env GIT_SSH_COMMAND=x git fetch", "env PAGER=x git log", "env LD_PRELOAD=x ls", "env DYLD_INSERT_LIBRARIES=x ls",
    "ssh -o ProxyCommand=x host", "ssh -oProxyCommand=x host", "ssh -F cfg host", "scp -S prog a b:c", "scp -o x a b:c",
    "rsync -e prog a b:", "rsync --rsh=prog a b:", "sftp -o x host", "zip -TT x a.zip a", "zip --unzip-command=x a.zip a",
    "tar -I prog -cf a.tar x", "tar -cIprog -f a.tar x", "tar --use-compress-program=x -cf a x", "tar --to-command=x -xf a",
    "tar --checkpoint-action=exec=x -cf a x", "less +!x file", "less !x",
    # 4: persistence
    "cp x ~/.zshrc", "tee ~/.zprofile", "cat ~/.zshenv", "cp x ~/.bashrc", "cp x ~/.bash_profile", "cp x ~/.profile",
    "cp x.plist ~/Library/LaunchAgents/", "cp x ~/.gitconfig", "cp x ~/.config/git/config", "launchctl load x",
    "crontab x", "defaults write com.apple.x k v", "at now", "periodic daily", "ls ~/Library",
    # 5: disks
    "diskutil unmountDisk disk2", "diskutil mount disk2", "diskutil apfs list", "dd if=x of=//dev/disk2",
    "dd if=x of=/dev/../dev/disk2",
    # 9: symlinks resolve from the link's directory
    "ln -s ../../.config/biseo proj/k", "ln -s ~/.ssh k", "ln -sf " + "../" * 20 + " k",
    "ln --symbolic ~/.config x",
]


@pytest.mark.parametrize("cmd", ROUND3)
def test_hard_deny_round3(cfg, gw, cmd):
    d.CFG["approve_unlisted"] = True
    cfg["roots"].append(d.Path.home().resolve())
    try:
        res = d.run_command(command=cmd, cwd=str(cfg["roots"][0] / "proj"))
    finally:
        cfg["roots"].pop()
        d.CFG["approve_unlisted"] = False
    assert res["status"] == "refused", (cmd, res)
    assert not [c for c in gw[0] if c[1] == "/v1/approvals"], cmd


@pytest.mark.parametrize("cwd", ["~", "~/.config", "/Users", "/"])
def test_cwd_above_secrets_refused(cfg, gw, cwd):
    d.CFG["approve_unlisted"] = True
    try:
        res = d.run_command(command="rg -uuu BEGIN", cwd=cwd)
    finally:
        d.CFG["approve_unlisted"] = False
    assert res["status"] == "refused", (cwd, res)


def test_ln_target_relative_to_the_link_dir(cfg, gw, tmp_path, monkeypatch):
    """`ln -s ../x deep/link`: ../x is resolved from deep/, so a target that only looks harmless from the cwd is caught."""
    deep = cfg["roots"][0] / "proj" / "a" / "b"
    deep.mkdir(parents=True)
    home = d.Path.home().resolve()
    rel = os.path.relpath(home / ".config", deep)  # harmless-looking from proj, ~/.config from deep/
    d.CFG["approve_unlisted"] = True
    try:
        res = d.run_command(command=f"ln -s {rel} a/b/link", cwd="proj")
    finally:
        d.CFG["approve_unlisted"] = False
    assert res["status"] == "refused", res


def test_requester_header_reaches_the_approval(live, gw):
    calls, approvals = gw
    d.CFG["approve_unlisted"] = True

    async def go():
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}", "X-Azel-Requester": "task:abc123abc123"})
        async with Client(streamable_http_client(live, http_client=http)) as c:
            return await c.call_tool("run_command", {"command": "printf requester", "cwd": "proj"})
    try:
        res = anyio.run(go)
    finally:
        d.CFG["approve_unlisted"] = False
    assert "pending_approval" in json.dumps(res.structured_content or [c.model_dump() for c in res.content])
    post = [c for c in calls if c[1] == "/v1/approvals"]
    assert post and post[-1][2]["requester"] == "task:abc123abc123", post


@pytest.mark.parametrize("cmd", ["tar -czf /tmp/o.tgz -Cx/../.. .config", "tar -czf /tmp/o.tgz -C .. .config",
                                 "tar -czf /tmp/o.tgz --directory=.. .ssh", "rg -uuu BEGIN -C2 .."])
def test_dash_c_moves_the_base(cfg, gw, cmd):
    """From a directory just under ~, `-C..` makes `.config` mean ~/.config."""
    import tempfile
    d.CFG["approve_unlisted"] = True
    try:
        with tempfile.TemporaryDirectory(dir=d.Path.home()) as sub:
            res = d.run_command(command=cmd, cwd=sub)
    finally:
        d.CFG["approve_unlisted"] = False
    assert res["status"] == "refused", (cmd, res)


def test_rg_hostname_bin_never_runs_without_a_tap(cfg, gw, tmp_path):
    """The exact review case: rg is allowlisted, so this ran ./x with no approval at all."""
    proj = cfg["roots"][0] / "proj"
    (proj / "x").write_text("#!/bin/sh\ntouch PWNED\n")
    (proj / "x").chmod(0o755)
    (proj / "f.txt").write_text("foo\n")
    for cmd in ["rg --hostname-bin=./x --hyperlink-format='file://{host}{path}' --color=always foo f.txt",
                "rg --hostname-bin ./x foo f.txt", "rg --pre=./x foo f.txt", "rg --pre-glob=* --pre ./x foo ."]:
        res = d.run_command(command=cmd, cwd="proj")
        assert res["status"] == "refused", (cmd, res)
    assert not (proj / "PWNED").exists() and not [c for c in gw[0] if c[1] == "/v1/approvals"]


ROUND4 = [
    "git config --global core.fsmonitor ./x", "git config core.pager ./x", "git config diff.external ./x",
    "git config --add alias.st !sh", "git config --unset user.name", "git config -e",
    "tar -xPf evil.tar", "tar -x -P -f evil.tar", "tar --absolute-paths -xf evil.tar", "tar xPf evil.tar",
    "bsdtar -xPf evil.tar", "pax -r -f evil.pax", "pax -rw src dst",
    "open ./run", "open x.terminal", "open x.fileloc", "open /Applications/Calculator.app", "open a.workflow",
    "open a.action", "open a.inetloc",
    "defaults -currentHost write com.apple.x k v", "defaults -currentHost import com.x f.plist",
    "plutil -replace k -string v x.plist", "plutil -insert k -string v x.plist", "plutil -remove k x.plist",
    "cat ~/.zlogout", "ls ~/.config/fish", "cp x ~/.local/bin/ls", "cat ~/.aws/credentials", "cat ~/.netrc",
    "ls ~/.config/gh", "cat ~/.zsh_history", "ls ~/.local",
    "cp evil .git/hooks/pre-commit", "cp evil repo/.git/config", "tee .git/config", "cp -r hooks .git",
    "ls -C.git/hooks", "tar -xf a.tar -C .git",
]


@pytest.mark.parametrize("cmd", ROUND4)
def test_hard_deny_round4(cfg, gw, cmd):
    proj = cfg["roots"][0] / "proj"
    (proj / "run").write_text("#!/bin/sh\n")
    (proj / "run").chmod(0o755)
    (proj / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
    d.CFG["approve_unlisted"] = True
    cfg["roots"].append(d.Path.home().resolve())
    try:
        res = d.run_command(command=cmd, cwd=str(proj))
    finally:
        cfg["roots"].pop()
        d.CFG["approve_unlisted"] = False
    assert res["status"] == "refused", (cmd, res)
    assert not [c for c in gw[0] if c[1] == "/v1/approvals"], cmd


def test_cwd_inside_dot_git_refused(cfg, gw):
    hooks = cfg["roots"][0] / "proj" / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    d.CFG["approve_unlisted"] = True
    try:
        assert d.run_command(command="cp ../../x pre-commit", cwd=str(hooks))["status"] == "refused"
        assert d.run_command(command="ls", cwd=str(hooks.parent))["status"] == "refused"
    finally:
        d.CFG["approve_unlisted"] = False


@pytest.mark.parametrize("cmd", ["git -C . status", "git -C proj log -1", "git config --get user.name",
                                 "git config --list", "git config -l --show-origin", "git config --get-regexp alias"])
def test_git_reads_and_dash_capital_c_still_allowed(cfg, gw, cmd):
    res = d.run_command(command=cmd, cwd="proj" if "proj log" not in cmd else ".")
    assert res["status"] != "refused", (cmd, res)  # git is allowlisted here; it runs (exit code aside)


def test_open_apps_and_urls_still_reach_approval(cfg, gw):
    d.CFG["approve_unlisted"] = True
    try:
        for cmd in ['open -a "Google Chrome"', "open https://example.com", "open -a Safari https://example.com",
                    "open notes.txt"]:
            assert d.run_command(command=cmd, cwd="proj")["status"] == "pending_approval", cmd
    finally:
        d.CFG["approve_unlisted"] = False


# ---------- music and home: fixed templates, fake osascript / shortcuts ----------

mac_only = pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS only")


@pytest.fixture
def fake_bins(tmp_path, monkeypatch):
    """osascript and shortcuts stand-ins that log stdin and argv as JSON lines and print $FAKE_OUT."""
    log = tmp_path / "calls.jsonl"
    for name in ("osascript", "shortcuts"):
        b = tmp_path / name
        b.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$(python3 -c 'import json,sys;print(json.dumps({{\"bin\":\"{name}\",\"argv\":sys.argv[1:],"
                     f"\"stdin\":sys.stdin.read()}}))' \"$@\")\" >> {log}\necho \"${{FAKE_OUT:-ok}}\"\n[ -n \"$FAKE_FAIL\" ] && {{ echo \"$FAKE_FAIL\" >&2; exit 1; }}\nexit 0\n")
        b.chmod(0o755)
    monkeypatch.setattr(d, "OSASCRIPT", str(tmp_path / "osascript"))
    monkeypatch.setattr(d, "SHORTCUTS", str(tmp_path / "shortcuts"))
    return lambda: [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


@mac_only
def test_music_play_passes_the_query_as_argv_never_in_the_script(cfg, fake_bins):
    evil = 'x" & (do shell script "touch /tmp/pwned") & "'
    r = d.music(action="play", query=evil)
    (call,) = fake_bins()
    assert r["status"] == "ok" and call["bin"] == "osascript"
    assert call["argv"] == ["-", evil] and "pwned" not in call["stdin"]
    assert call["stdin"] == d.MUSIC_SCRIPTS["play"]


@mac_only
def test_music_actions_use_fixed_templates_and_volume_is_clamped(cfg, fake_bins):
    for a in ("pause", "resume", "next", "previous", "now_playing"):
        assert d.music(action=a)["status"] == "ok"
    d.music(action="volume", volume=250)
    d.music(action="volume", volume=-5)
    d.music(action="play")  # no query: continue
    calls = fake_bins()
    assert [c["stdin"] for c in calls[:5]] == [d.MUSIC_SCRIPTS[k] for k in ("pause", "resume", "next", "previous", "now_playing")]
    assert [c["argv"] for c in calls[5:7]] == [["-", "100"], ["-", "0"]]
    assert calls[7]["stdin"] == d.MUSIC_SCRIPTS["play_any"] and calls[7]["argv"] == ["-"]


@mac_only
def test_music_refuses_bad_input_and_reports_not_found_and_automation(cfg, fake_bins, monkeypatch):
    assert d.music(action="rm -rf")["status"] == "refused"
    assert d.music(action="volume")["status"] == "refused"
    assert fake_bins() == []
    monkeypatch.setenv("FAKE_OUT", "not found")
    assert d.music(action="play", query="없는 노래")["status"] == "not_found"
    monkeypatch.delenv("FAKE_OUT")
    monkeypatch.setenv("FAKE_FAIL", "execution error: Not authorized to send Apple events to Music. (-1743)")
    r = d.music(action="pause")
    assert r["status"] == "refused" and "Automation" in r["reason"]
    long = "가" * 500
    monkeypatch.delenv("FAKE_FAIL")
    d.music(action="play", query=long)
    assert len(fake_bins()[-1]["argv"][1]) == d.MUSIC_QUERY_MAX


@mac_only
def test_home_runs_only_allowlisted_scenes(cfg, fake_bins):
    d.CFG["home_shortcuts"] = ["azel-lights-off", "azel-movie"]
    r = d.home(scene="azel-movie")
    (call,) = fake_bins()
    assert r["status"] == "ok" and call == {"bin": "shortcuts", "argv": ["run", "azel-movie"], "stdin": ""}
    for bad in ("azel-lights-off; id", "Azel-Movie", "front door lock", "-h", ""):
        assert d.home(scene=bad)["status"] == "refused"
    assert len(fake_bins()) == 1
    d.CFG["home_shortcuts"] = []
    assert d.home(scene="azel-movie")["status"] == "refused"


def test_music_and_home_default_config_is_safe(cfg):
    assert d.CFG["home_shortcuts"] == [] and cfg["music_enabled"] is True
    assert "osascript" in d.DENY_PROGRAMS  # run_command still cannot run it


@pytest.fixture
def fake_open(tmp_path, monkeypatch):
    log = tmp_path / "open.log"
    b = tmp_path / "open"
    b.write_text(f"#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\" >> {log}; done\n")
    b.chmod(0o755)
    monkeypatch.setattr(d, "OPEN", str(b))
    return lambda: log.read_text().splitlines() if log.exists() else []


@mac_only
def test_spotify_play_opens_a_checked_uri_by_argv(cfg, fake_open, fake_bins):
    r = d.music(action="play", query="아이유 밤편지; rm -rf ~", app="spotify")
    (arg0, arg1, arg2) = fake_open()
    assert r["status"] == "ok" and (arg0, arg1) == ("-a", "Spotify")
    assert arg2.startswith("spotify:search:") and ";" not in arg2 and " " not in arg2 and "%EC%95%84" in arg2
    assert d.SPOTIFY_URI.fullmatch(arg2) and len(arg2) <= len("spotify:search:") + 200
    assert fake_bins() == []  # not through osascript


@mac_only
def test_spotify_uri_validation_and_length_cap(cfg, fake_open):
    ok = "spotify:track:4uLU6hMCjMI75M1A2tKUQC"
    assert d.music(action="play", uri=ok, app="spotify")["status"] == "ok" and fake_open()[-1] == ok
    for bad in ("https://evil.example", "spotify:track:short", "spotify:search:a b", "spotify:user:abc1234567890",
                "spotify:track:4uLU6hMCjMI75M1A2tKUQC --args", "file:///etc/passwd"):
        assert d.music(action="play", uri=bad, app="spotify")["status"] == "refused", bad
    d.music(action="play", query="가" * 150, app="spotify")
    assert len(fake_open()[-1]) <= len("spotify:search:") + 200 and d.SPOTIFY_URI.fullmatch(fake_open()[-1])
    assert d.music(action="play", query="x", app="tidal")["status"] == "refused"


@mac_only
def test_spotify_transport_and_system_volume(cfg, fake_bins):
    for a in ("pause", "resume", "next", "previous", "now_playing"):
        assert d.music(action=a, app="spotify")["status"] == "ok"
    d.music(action="volume", volume=999, app="spotify")
    d.music(action="play", app="spotify")  # nothing to search for: continue
    calls = fake_bins()
    assert [c["stdin"] for c in calls[:5]] == [d.SPOTIFY_SCRIPTS[k] for k in ("pause", "resume", "next", "previous", "now_playing")]
    assert calls[5]["stdin"] == d.SYSTEM_VOLUME and calls[5]["argv"] == ["-", "100"]
    assert calls[6]["stdin"] == d.SPOTIFY_SCRIPTS["resume"]


# ---------- grants: team devices the gateway reaches but that cannot reach the gateway ----------

@pytest.fixture
def gcfg(cfg, gw):
    d.CFG["approval_via"] = "gateway-grant"
    d.CFG["approve_unlisted"] = True
    d._issued.clear()
    return d.CFG, gw[0]


def make_grant(cfg, command, cwd, nonce, exp=None, key=None, device=None, aid="ap-1"):
    exp = int(time.time()) + 300 if exp is None else exp
    dev = device or cfg["id"]
    mac = d.grant_mac(key or cfg["token"], aid, dev, command, cwd, nonce, exp)
    return {"approval_id": aid, "device": dev, "command": command, "cwd": cwd, "nonce": nonce, "exp": exp, "mac": mac}


def ask(command="pwd"):
    r = d.run_command(command=command)
    assert r["status"] == "needs_approval", r
    return r


def test_grant_mode_returns_needs_approval_without_contacting_the_gateway(gcfg):
    cfg, calls = gcfg
    r = ask("pwd")
    assert set(r) == {"status", "command", "cwd", "device", "nonce", "in_roots"} and r["device"] == cfg["id"]
    assert r["command"] == "pwd" and r["nonce"] in d._issued
    assert calls == []
    assert ask("pwd")["nonce"] != r["nonce"]
    assert d.run_command(command="ls", cwd="proj")["status"] == "ok"  # a listed command needs nothing
    assert d.run_command(command="rm x")["status"] == "needs_approval"  # risky ones too


def test_grant_runs_the_command_once_and_the_used_file_is_private(gcfg, tmp_path):
    cfg, calls = gcfg
    r = ask("pwd")
    g = make_grant(cfg, r["command"], r["cwd"], r["nonce"])
    out = d.run_command(command="pwd", cwd=r["cwd"], grant=g)
    assert out["status"] == "ok" and out["stdout"].strip() == r["cwd"]
    assert calls == []  # nothing sent to the gateway: it audits on its side
    used = d._used_nonces_file()
    assert used.read_text().split() == [r["nonce"]] and used.stat().st_mode & 0o777 == 0o600
    again = d.run_command(command="pwd", cwd=r["cwd"], grant=g)  # replay
    assert again["status"] == "refused" and "nonce" in again["reason"]


def test_grant_with_a_wrong_signature_is_refused_and_does_not_spend_the_nonce(gcfg):
    cfg, _ = gcfg
    r = ask("pwd")
    bad = make_grant(cfg, "pwd", r["cwd"], r["nonce"], key="some-other-token")
    res = d.run_command(command="pwd", cwd=r["cwd"], grant=bad)
    assert res["status"] == "refused" and "signature" in res["reason"]
    assert d.run_command(command="pwd", cwd=r["cwd"], grant=make_grant(cfg, "pwd", r["cwd"], r["nonce"]))["status"] == "ok"
    tampered = make_grant(cfg, "pwd", r["cwd"], ask("pwd")["nonce"])
    tampered["exp"] += 1
    assert d.run_command(command="pwd", cwd=r["cwd"], grant=tampered)["status"] == "refused"


def test_grant_expired_or_too_long_is_refused(gcfg):
    cfg, _ = gcfg
    r = ask("pwd")
    old = make_grant(cfg, "pwd", r["cwd"], r["nonce"], exp=int(time.time()) - 1)
    assert "expired" in d.run_command(command="pwd", cwd=r["cwd"], grant=old)["reason"]
    long = make_grant(cfg, "pwd", r["cwd"], r["nonce"], exp=int(time.time()) + 3600)
    assert "too long" in d.run_command(command="pwd", cwd=r["cwd"], grant=long)["reason"]


def test_grant_for_another_cwd_command_or_device_is_refused(gcfg, tmp_path):
    cfg, _ = gcfg
    r = ask("pwd")
    other = str((cfg["roots"][0] / "proj").resolve())
    assert other != r["cwd"]
    for command, cwd, device in (("pwd", other, None), ("ls", r["cwd"], None), ("pwd", r["cwd"], "elsewhere")):
        g = make_grant(cfg, "pwd", r["cwd"], r["nonce"], device=device)
        res = d.run_command(command=command, cwd=cwd if cwd == r["cwd"] else other, grant=g)
        assert res["status"] == "refused", (command, cwd, device, res)
    assert r["nonce"] in d._issued  # none of those spent it


def test_grant_with_an_unissued_nonce_or_with_an_approval_id_is_refused(gcfg):
    cfg, _ = gcfg
    wd = str(cfg["roots"][0])
    forged = make_grant(cfg, "pwd", wd, "never-issued-by-this-daemon")
    assert "unknown" in d.run_command(command="pwd", grant=forged)["reason"]
    r = ask("pwd")
    g = make_grant(cfg, "pwd", r["cwd"], r["nonce"])
    assert d.run_command(command="pwd", cwd=r["cwd"], grant=g, approval_id="x")["status"] == "refused"
    assert d.run_command(command="pwd", approval_id="x")["status"] == "refused"  # no gateway round trip in this mode


def test_hard_deny_beats_a_valid_grant(gcfg):
    cfg, calls = gcfg
    r = ask("pwd")  # a real nonce, a real signature, for a command no approval may unlock
    for cmd in ("sudo id", "sh -c id", "osascript -e x"):
        g = make_grant(cfg, cmd, r["cwd"], r["nonce"])
        res = d.run_command(command=cmd, cwd=r["cwd"], grant=g)
        assert res["status"] == "refused", cmd
    assert r["nonce"] in d._issued and not d._used_nonces_file().exists()


def test_default_mode_is_unchanged_and_never_returns_needs_approval(cfg, gw):
    assert d.CFG["approval_via"] == "gateway"
    d.CFG["approve_unlisted"] = True
    assert d.run_command(command="pwd")["status"] == "pending_approval"


def test_grant_mac_matches_the_gateways_golden_value():
    assert d.grant_mac("tok", "ap-1", "vd-spark", "ls -la", "/home/u/x", "n0nce", 1700000000) == "78a6c4f27e365ff9965f2d61eb1bfeec26de56c0587bd4ab0a6013c5eea7f0f7"


SID = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
SID2 = "11111111-2222-3333-4444-555555555555"


def run_job(monkeypatch, sid=SID, **kw):
    seen = {}

    def fake_run(argv, **k):
        seen["argv"], seen["cwd"] = argv, k["cwd"]
        return subprocess.CompletedProcess(argv, 0, json.dumps(
            {"result": "did it", "session_id": sid, "total_cost_usd": 0.1}).encode(), b"")

    monkeypatch.setattr(d.subprocess, "run", fake_run)
    r = d.delegate(**kw)
    for _ in range(100):
        if r.get("job_id") and d.job_status(job_id=r["job_id"])["job_status"] != "running":
            break
        time.sleep(0.02)
    return r, seen


def test_delegate_reports_the_session_id_cwd_and_an_output_file(cfg, monkeypatch):
    r, seen = run_job(monkeypatch, task="fix bug", cwd="proj")
    st = d.job_status(job_id=r["job_id"])
    assert st["session_id"] == SID and st["cwd"] == seen["cwd"].as_posix() or st["cwd"] == str(seen["cwd"])
    out = __import__("pathlib").Path(st["output_file"])
    assert out.read_text() == "did it" and out.stat().st_mode & 0o777 == 0o600
    assert next(j for j in d.list_jobs()["jobs"] if j["job_id"] == r["job_id"])["session_id"] == SID
    sessions = d._sessions_file()
    assert json.loads(sessions.read_text())[SID]["cwd"] == str(seen["cwd"]) and sessions.stat().st_mode & 0o777 == 0o600


def test_resume_continues_the_same_session_in_the_same_cwd(cfg, monkeypatch):
    run_job(monkeypatch, task="first", cwd="proj")
    r, seen = run_job(monkeypatch, sid=SID2, task="and now the tests too", cwd="proj", resume=SID)
    assert r["status"] == "ok"
    argv = seen["argv"]
    assert argv[:5] == ["claude", "-p", "and now the tests too", "--resume", SID]
    assert "acceptEdits" in argv and "Bash(sudo:*)" in argv  # the same guards on a resumed run
    st = d.job_status(job_id=r["job_id"])
    assert st["resumes"] == SID and st["session_id"] == SID2
    assert SID2 in d._sessions()  # a resumed run's own session id can be resumed next


def test_resume_refuses_bad_unknown_or_foreign_sessions(cfg, monkeypatch):
    run_job(monkeypatch, task="first", cwd="proj")
    for bad in ("../../etc/passwd", "latest", "0A1B2C3D-4E5F-6789-ABCD-EF0123456789", SID + " --dangerously-skip", ""):
        if bad:
            assert d.delegate(task="x", cwd="proj", resume=bad)["status"] == "refused", bad
    assert "unknown session" in d.delegate(task="x", cwd="proj", resume=SID2)["reason"]  # valid shape, not ours
    other = d.CFG["roots"][0] / "other"
    other.mkdir()
    assert "lives in" in d.delegate(task="x", cwd="other", resume=SID)["reason"]  # another cwd


def test_resume_refuses_while_the_session_is_still_running(cfg, monkeypatch):
    run_job(monkeypatch, task="first", cwd="proj")
    d.JOBS["busy"] = {"job_id": "busy", "status": "running", "session_id": SID}
    assert "still running" in d.delegate(task="x", cwd="proj", resume=SID)["reason"]
    d.JOBS["busy"]["status"] = "done"


def test_there_is_no_team_device_block_on_delegate_in_the_daemon(cfg, monkeypatch):
    d.CFG["id"] = "vd-spark"
    r, _ = run_job(monkeypatch, task="x", cwd="proj")
    assert r["status"] == "ok"


def test_every_approval_the_daemon_files_says_whether_the_cwd_is_in_the_roots(cfg, gw):
    import tempfile
    calls, _ = gw
    d.CFG["approve_unlisted"] = True
    d.run_command(command="pwd", cwd="proj")  # inside the roots
    with tempfile.TemporaryDirectory(dir=d.Path.home()) as sub:  # an approved run may use any directory under $HOME
        d.run_command(command="pwd", cwd=sub)
    posts = [c[2] for c in calls if c[:2] == ("POST", "/v1/approvals")]
    assert [p["in_roots"] for p in posts] == [True, False] and posts[0]["cwd"].endswith("proj")
    assert not d.in_roots(d.Path.home().resolve()) and d.in_roots(d.CFG["roots"][0] / "proj")


def test_needs_approval_carries_in_roots_too(cfg, gw):
    import tempfile
    d.CFG["approval_via"], d.CFG["approve_unlisted"] = "gateway-grant", True
    r = d.run_command(command="pwd", cwd="proj")
    assert r["status"] == "needs_approval" and r["in_roots"] is True
    with tempfile.TemporaryDirectory(dir=d.Path.home()) as sub:
        assert d.run_command(command="pwd", cwd=sub)["in_roots"] is False


def run_delegate(monkeypatch, payload, rc=0):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, rc, json.dumps(payload).encode(), b"")
    monkeypatch.setattr(d.subprocess, "run", fake_run)
    job_id = d.delegate(task="실버텔러 시장 조사해서 PDF로", cwd="proj")["job_id"]
    for _ in range(100):
        if d.job_status(job_id=job_id)["job_status"] != "running":
            break
        time.sleep(0.02)
    return d.job_status(job_id=job_id), seen["argv"]


def test_delegate_pre_allows_research_and_build_tools(cfg, monkeypatch):
    st, argv = run_delegate(monkeypatch, {"result": "PDF를 ~/Desktop/silverteller.pdf에 저장했어요."})
    allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
    assert {"WebSearch", "WebFetch", "Bash(python3:*)", "Bash(uv:*)", "Bash(mkdir:*)"} <= set(allowed)
    assert "Bash(rm:*)" not in allowed and argv[argv.index("--disallowedTools") + 1:][:3] == \
        ["Bash(git push:*)", "Bash(rm:*)", "Bash(sudo:*)"] and d.NO_ATTRIBUTION in argv
    assert st["job_status"] == "done" and "blocked" not in st


def test_delegate_allowed_tools_come_from_the_config(cfg, monkeypatch, tmp_path):
    monkeypatch.setitem(d.CFG, "delegate_allowed_tools", ["WebSearch"])
    _, argv = run_delegate(monkeypatch, {"result": "ok"})
    assert argv[argv.index("--allowedTools"):argv.index("--disallowedTools")] == ["--allowedTools", "WebSearch"]
    monkeypatch.setitem(d.CFG, "delegate_allowed_tools", [])
    _, argv = run_delegate(monkeypatch, {"result": "ok"})
    assert "--allowedTools" not in argv  # an empty list: nothing pre-allowed
    conf = tmp_path / "bad.toml"
    conf.write_text('delegate_allowed_tools = "WebSearch"\n')
    with pytest.raises(SystemExit, match="delegate_allowed_tools"):
        d.load_config(conf)


def test_a_refused_run_is_not_done(cfg, monkeypatch):
    """Live 2026-11-23 (job 60ac064254d1): every WebSearch, WebFetch and python3 refused, yet the task read as done."""
    st, _ = run_delegate(monkeypatch, {"result": "조사를 못 했어요.", "is_error": False, "permission_denials": [
        {"tool_name": "WebSearch", "tool_use_id": "a"}, {"tool_name": "WebFetch", "tool_use_id": "b"},
        {"tool_name": "Bash", "tool_use_id": "c", "tool_input": {"command": "python3 make_pdf.py"}},
        {"tool_name": "WebSearch", "tool_use_id": "d"}]})
    assert st["job_status"] == "failed" and st["needs_input"] is True and st["blocked"] == ["Bash", "WebFetch", "WebSearch"]
    st, _ = run_delegate(monkeypatch, {"result": "I need you to allow WebSearch/WebFetch/python3 to finish this."})
    assert st["job_status"] == "failed" and st["needs_input"] is True and st["blocked"] == []  # said only in words
    st, _ = run_delegate(monkeypatch, {"result": "권한 설계 문서를 정리해서 docs/permissions.md에 저장했어요."})
    assert st["job_status"] == "done"  # talking about permissions is not being refused
    # live 2026-11-22: the report was made, then `rm` of old previews (refused on purpose) marked the run blocked
    st, _ = run_delegate(monkeypatch, {"result": "PDF를 만들었어요. 이전 미리보기는 지우는 게 허용되지 않아(wasn't allowed) 남겼어요.",
                                       "is_error": False, "permission_denials": [
        {"tool_name": "Bash", "tool_use_id": "e", "tool_input": {"command": "rm preview/p1.png"}}]})
    assert st["job_status"] == "done" and st["needs_input"] is False
