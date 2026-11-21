"""Biseo agent daemon: an MCP server (streamable HTTP at /mcp) exposing guarded tools.

See ../PROTOCOL.md section 4. Run: `uv run python biseo_daemon.py [--config PATH]`.
"""

import argparse
import asyncio
import concurrent.futures
import functools
import hmac
import ipaddress
import json
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import tomllib
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

log = logging.getLogger("biseo-daemon")

DEFAULTS = {
    "id": "macbook",
    "name": "macbook",
    "host": "100.64.0.8",
    "port": 8893,
    "token_file": "~/.config/biseo/token",
    "gateway_url": "http://100.64.0.8:8892",
    "allowed_roots": ["~/dev"],
    "allowed_commands": ["git status", "git log", "git diff", "ls", "rg"],
    "approval_required": ["rm", "git push", "git reset --hard", "mv", "chmod", "chown"],
    # true on jh-only devices: a command not in allowed_commands becomes an approval request instead of a refusal.
    # Team devices (vd-spark, vd-core) and sadr keep false.
    "approve_unlisted": False,
    "approval_wait_s": 120,  # run_command(…, approval_id) on a still-pending approval waits this long for the tap
    "log_path": "~/.local/state/biseo/daemon.jsonl",
    "read_max_bytes": 256_000,
    "output_max_bytes": 64_000,
    "command_timeout": 120,
    "delegate_timeout": 3600,
    "claude_bin": "claude",
    "browse_enabled": True,
    "browse_timeout_s": 20,
    "browse_max_chars": 20_000,
    "browse_allow_private": [],
}
NO_ATTRIBUTION = "Never add Co-Authored-By or any AI attribution to commits, PRs or files."

CFG: dict = {}
JOBS: dict[str, dict] = {}
_lock = threading.Lock()


class Refused(Exception):
    pass


def load_config(path: str | os.PathLike) -> dict:
    cfg = dict(DEFAULTS)
    p = Path(path).expanduser()
    if p.exists():
        cfg.update(tomllib.loads(p.read_text()))
    cfg["roots"] = [Path(r).expanduser().resolve() for r in cfg["allowed_roots"]]
    cfg["log_path"] = Path(cfg["log_path"]).expanduser()
    cfg["token"] = Path(cfg["token_file"]).expanduser().read_text().strip()
    if not cfg["token"]:
        raise SystemExit("token file is empty")
    return cfg


# ---------- guards ----------

def safe_path(path: str) -> Path:
    """Resolve symlinks and '..'; refuse anything outside allowed_roots. Relative paths start at the first root."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = CFG["roots"][0] / p
    p = p.resolve()
    if not any(p.is_relative_to(r) for r in CFG["roots"]):
        raise Refused(f"path outside allowed_roots: {path}")
    return p


def _has_seq(tokens: list[str], pattern: str, anywhere: bool) -> bool:
    pat = shlex.split(pattern)
    starts = range(len(tokens) - len(pat) + 1) if anywhere else [0]
    return any(tokens[i:i + len(pat)] == pat for i in starts)


GIT_CODE_FLAGS = ("--config", "--exec-path", "--upload-pack", "--receive-pack")


def check_args(tokens: list[str], wd: Path) -> None:
    """Refuse flags that run arbitrary code and path-like arguments that leave allowed_roots."""
    if tokens[0] == "rg" and any(t.startswith("--pre") for t in tokens):
        raise Refused("rg --pre runs arbitrary programs")
    if tokens[0] == "git" and any(t in ("-c", "-u") or t.startswith(GIT_CODE_FLAGS) for t in tokens):
        raise Refused("git flag can run arbitrary code")
    for t in tokens[1:]:
        v = t.split("=", 1)[1] if t.startswith("-") and "=" in t else (None if t.startswith("-") else t)
        # shortcut: a non-path argument such as an rg pattern "/api/" is also treated as a path and may be refused
        if v and (v.startswith(("/", "~")) or "/" in v or os.sep in v or ".." in v or (wd / v).exists()):
            p = (wd / Path(v).expanduser()).resolve()
            if not any(p.is_relative_to(r) for r in CFG["roots"]):
                raise Refused(f"argument outside allowed_roots: {v}")


# Never runnable, approval or not: privilege escalation, secrets, wiping, and inline code (argv only, no shell:
# `sh -c`, `python -c`, `osascript` would smuggle a whole script past the exact-command approval). Names are
# compared case-insensitively (APFS: SUDO is sudo), and every argv position counts, so wrappers (env, xargs,
# find -exec, nice, nohup, time, timeout, caffeinate, command) cannot hide a command.
ESCALATE = {"sudo", "su", "doas", "pkexec", "runas"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"}
INLINE = {"perl": "eE", "ruby": "e", "node": "ep", "nodejs": "ep", "php": "r", "lua": "e"}  # + python* : c
INLINE_LONG = {"--eval", "--print", "--command"}
TERMINALS = {"terminal", "terminal.app", "iterm", "iterm.app", "iterm2", "iterm2.app", "com.apple.terminal",
             "com.googlecode.iterm2"}
SCRIPT_EXT = (".command", ".tool", ".sh", ".scpt", ".scptd", ".applescript", ".workflow")
SECRET_DIRS = ("~/.config/biseo", "~/.ssh", "~/.claude", "~/Library/Keychains")
SECRET_WORDS = (".config/biseo", ".ssh/", "/.ssh", ".claude", "Keychains", ".keychain")  # also inside flag values


def _expand(tokens: list[str]) -> list[str]:
    """argv with `env -S "…"` / `--split-string=…` values split into their own tokens (env runs them)."""
    out, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-S", "--split-string") and i + 1 < len(tokens):
            out += [t] + _split(tokens[i + 1])
            i += 2
            continue
        if t.startswith("--split-string="):
            out += _split(t.split("=", 1)[1])
        elif t.startswith("-S") and len(t) > 2 and not t.startswith("--"):
            out += _split(t[2:])
        else:
            out.append(t)
        i += 1
    return out


def _split(v: str) -> list[str]:
    try:
        return _expand(shlex.split(v))
    except ValueError:
        raise Refused("unparseable env -S string")


def _short_flags(t: str) -> str:
    return t[1:] if t.startswith("-") and not t.startswith("--") else ""


def _path_values(t: str) -> list[str]:
    """The path-like values a token may carry: itself, `--opt=/p`, and attached short options (`-C/p`, `-T~/p`)."""
    vals = [t]
    if t.startswith("-") and "=" in t:
        vals.append(t.split("=", 1)[1])
    if t.startswith("-") and not t.startswith("--") and len(t) > 2 and t[2] in "/~.":
        vals.append(t[2:])
    return vals


def hard_deny(tokens: list[str], wd: Path) -> None:
    """Refuse (before any approval exists, and for approved runs) what no approval may unlock."""
    tokens = _expand(tokens)
    names = [os.path.basename(t).lower() for t in tokens]
    home = Path.home().resolve()
    secrets = [Path(d).expanduser().resolve() for d in SECRET_DIRS]
    if ESCALATE & set(names):
        raise Refused("privilege escalation is never allowed")
    for i, n in enumerate(names):
        rest, low = tokens[i + 1:], [r.lower() for r in tokens[i + 1:]]
        if n in SHELLS and any(r == "-c" or "c" in _short_flags(r) or r == "--command" for r in rest):
            raise Refused("shell -c is never allowed (run the command itself)")
        if n == "osascript" or n == "security":
            raise Refused(f"{n} is never allowed")
        letters = "c" if re.fullmatch(r"python[\d.]*", n) else INLINE.get(n)
        if letters and any(set(_short_flags(r)) & set(letters) or r.split("=")[0] in INLINE_LONG for r in rest):
            raise Refused(f"inline code ({n} {'/'.join('-' + c for c in letters)}) is never allowed")
        if n == "open":
            app = [low[k + 1] for k, r in enumerate(low[:-1]) if r in ("-a", "-b")]
            if any(os.path.basename(a.rstrip("/")) in TERMINALS for a in app) or any(r.endswith(SCRIPT_EXT) for r in low):
                raise Refused("opening a terminal or a script is never allowed")
        if n == "rm" and any(r == "--recursive" or set(_short_flags(r)) & {"r", "R"} for r in rest):
            for r in rest:
                if not r.startswith("-"):
                    p = (wd / Path(r.replace("$HOME", "~")).expanduser()).resolve()
                    if p == Path("/") or home.is_relative_to(p):
                        raise Refused("rm -r on / or the home directory is never allowed")
        if n == "find" and "-delete" in rest:
            for r in rest:
                if r.startswith(("-", "(", "!")):
                    break
                p = (wd / Path(r.replace("$HOME", "~")).expanduser()).resolve()
                if p == Path("/") or home.is_relative_to(p):
                    raise Refused("find -delete on / or the home directory is never allowed")
        if n == "diskutil" and rest:
            sub = low[0]
            if sub.startswith(("erase", "zerodisk", "secureerase", "reformat", "partitiondisk")) or (
                    sub == "apfs" and len(low) > 1 and low[1].startswith(("delete", "erase"))):
                raise Refused("erasing disks is never allowed")
        if n.startswith(("mkfs", "newfs")):
            raise Refused("formatting is never allowed")
        if n == "dd" and any(r.startswith("of=/dev/") for r in low):
            raise Refused("dd to a device is never allowed")
    if any(wd == s or wd.is_relative_to(s) for s in secrets):
        raise Refused("the assistant's, SSH, Claude and keychain files are never touched")
    for t in tokens:
        for v in _path_values(t):
            if any(w.lower() in v.lower() for w in SECRET_WORDS) or v.rstrip("/").lower().endswith(".ssh"):
                raise Refused("the assistant's, SSH, Claude and keychain files are never touched")
            if not v or (v.startswith("-") and v == t):
                continue
            p = (wd / Path(v.replace("$HOME", "~")).expanduser()).resolve()
            # the secret dirs themselves, anything inside them, and their ancestors (~, ~/.config, /Users, /)
            if any(p == s or p.is_relative_to(s) or s.is_relative_to(p) for s in secrets):
                raise Refused(f"path covers the assistant's, SSH, Claude or keychain files: {v}")


def run_dir(cwd: str | None, approved: bool) -> Path:
    """Working directory: inside allowed_roots, or (approved runs and unlisted commands awaiting approval) any
    existing directory under $HOME. Relative paths start at the first root."""
    try:
        return safe_path(cwd or str(CFG["roots"][0]))
    except Refused:
        if not approved:
            raise
    p = Path(cwd).expanduser()
    p = (p if p.is_absolute() else CFG["roots"][0] / p).resolve()
    if not p.is_dir() or not p.is_relative_to(Path.home().resolve()):
        raise Refused(f"cwd must be an existing directory under $HOME: {cwd}")
    return p


def _cap(b: bytes) -> str:
    n = CFG["output_max_bytes"]
    s = b[:n].decode(errors="replace")
    return s + (f"\n…[truncated {len(b) - n} bytes]" if len(b) > n else "")


def gateway(method: str, path: str, body: dict | None = None) -> dict:
    r = httpx.request(method, CFG["gateway_url"].rstrip("/") + path, json=body, timeout=10,
                      headers={"Authorization": f"Bearer {CFG['token']}"})
    r.raise_for_status()
    return r.json()


def _used_file() -> Path:
    return CFG["log_path"].with_name("used_approvals.txt")


def _claim_approval(approval_id: str) -> None:
    """Mark an approval id used (persisted, so a restart can't replay it)."""
    f = _used_file()
    with _lock:
        if f.exists() and approval_id in f.read_text().split():
            raise Refused("approval already used")
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a") as fh:
            fh.write(approval_id + "\n")


# ---------- logging wrapper ----------

def _short(v):
    if isinstance(v, str) and len(v) > 200:
        return v[:200] + f"…(+{len(v) - 200})"
    return v


def logged(fn):
    @functools.wraps(fn)
    def wrapper(**kwargs):
        t0 = time.monotonic()
        try:
            res = fn(**kwargs)
        except Refused as e:
            res = {"status": "refused", "reason": str(e)}
        except Exception as e:  # report to the caller instead of a bare protocol error
            res = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        entry = {"ts": time.time(), "tool": fn.__name__, "args": {k: _short(v) for k, v in kwargs.items()},
                 "status": res.get("status", "ok"), "duration_ms": round((time.monotonic() - t0) * 1000)}
        try:
            CFG["log_path"].parent.mkdir(parents=True, exist_ok=True)
            with CFG["log_path"].open("a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("tool log write failed: %s", e)
        return res
    return wrapper


# ---------- tools ----------

@logged
def list_dir(path: str) -> dict:
    """List a directory inside the allowed roots."""
    p = safe_path(path)
    entries = [{"name": c.name, "type": "dir" if c.is_dir() else "file",
                "size": c.stat().st_size if c.is_file() else None} for c in sorted(p.iterdir())]
    return {"status": "ok", "path": str(p), "entries": entries}


@logged
def read_file(path: str, max_bytes: int | None = None) -> dict:
    """Read a text file inside the allowed roots (capped)."""
    p = safe_path(path)
    cap = min(max_bytes or CFG["read_max_bytes"], CFG["read_max_bytes"])
    with p.open("rb") as f:
        data = f.read(cap + 1)
    return {"status": "ok", "path": str(p), "content": data[:cap].decode(errors="replace"),
            "truncated": len(data) > cap}


@logged
def search(query: str, path: str | None = None) -> dict:
    """Search file contents for a literal string (ripgrep if installed)."""
    p = safe_path(path or str(CFG["roots"][0]))
    rg = shutil.which("rg")
    if rg:
        out = subprocess.run([rg, "-n", "-F", "--max-count", "20", "--", query, str(p)],
                             capture_output=True, timeout=CFG["command_timeout"])
        lines = out.stdout.decode(errors="replace").splitlines()
    else:
        lines = []
        files = [p] if p.is_file() else (Path(d) / n for d, _, ns in os.walk(p) for n in ns)
        for fp in files:
            try:
                if fp.stat().st_size > 2_000_000:
                    continue
                for i, line in enumerate(fp.read_text(errors="ignore").splitlines(), 1):
                    if query in line:
                        lines.append(f"{fp}:{i}:{line}")
            except OSError:
                continue
            if len(lines) >= 500:
                break
    return {"status": "ok", "matches": lines[:500], "truncated": len(lines) > 500}


@logged
def run_command(command: str, cwd: str | None = None, approval_id: str | None = None) -> dict:
    """Run an allowlisted command (no shell). Risky commands need the user's approval via the gateway:
    the first call returns pending_approval; call again with the approval_id once approved."""
    tokens = shlex.split(command)
    if not tokens:
        raise Refused("empty command")
    listed = any(_has_seq(tokens, pat, anywhere=False) for pat in CFG["allowed_commands"])
    risky = any(_has_seq(tokens, pat, anywhere=True) for pat in CFG["approval_required"])
    unlisted = not listed and not risky
    if unlisted and not CFG["approve_unlisted"]:  # an approval id cannot unlock it here either
        raise Refused("command not in allowed_commands")
    wd = run_dir(cwd, approved=bool(approval_id) or (unlisted and CFG["approve_unlisted"]))
    hard_deny(tokens, wd)  # before any approval is created, and for approved runs too
    if not approval_id and (risky or unlisted):
        r = gateway("POST", "/v1/approvals",
                    {"device": CFG["id"], "tool": "run_command", "command": command, "cwd": str(wd)})
        if r.get("status") != "approved":  # an "always" rule approves it on the spot
            return {"status": "pending_approval", "approval_id": r["approval_id"]}
        approval_id = r["approval_id"]
    if approval_id:
        a = gateway("GET", f"/v1/approvals/{approval_id}")
        waited = time.monotonic()
        while a.get("status") == "pending" and time.monotonic() - waited < CFG["approval_wait_s"]:
            time.sleep(2)  # the user is deciding in the app
            a = gateway("GET", f"/v1/approvals/{approval_id}")
        if a.get("status") == "pending" and CFG["approval_wait_s"] > 0:
            return {"status": "pending_approval", "approval_id": approval_id}
        if a.get("status") != "approved":
            raise Refused(f"approval not granted (status={a.get('status')})")
        if a.get("device") != CFG["id"] or a.get("command") != command or a.get("cwd") != str(wd):
            raise Refused("approval does not match this device/command/cwd")
        try:  # spend it centrally, atomically and once (also stale after 10 min), then locally
            gateway("POST", f"/v1/approvals/{approval_id}/use", {"device": CFG["id"], "command": command, "cwd": str(wd)})
        except httpx.HTTPStatusError:
            raise Refused("approval already used, stale or not for this device")
        _claim_approval(approval_id)
    else:
        check_args(tokens, wd)
    try:
        out = subprocess.run(tokens, cwd=wd, capture_output=True, timeout=CFG["command_timeout"])
    except subprocess.TimeoutExpired:
        res = {"status": "timeout", "timeout_s": CFG["command_timeout"]}
    else:
        res = {"status": "ok", "exit_code": out.returncode, "stdout": _cap(out.stdout), "stderr": _cap(out.stderr)}
    if approval_id:  # audit: what ran under an approval (never its output)
        try:
            gateway("POST", f"/v1/approvals/{approval_id}/ran", {"exit_code": res.get("exit_code"), "status": res["status"]})
        except Exception as e:
            log.warning("approval audit not recorded: %s", type(e).__name__)
    return res


def _run_job(job: dict, argv: list[str], cwd: Path) -> None:
    try:
        out = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=CFG["delegate_timeout"])
        job["exit_code"] = out.returncode
        try:
            data = json.loads(out.stdout)
            job["result"] = data.get("result", "")
            job["cost_usd"] = data.get("total_cost_usd")
            job["status"] = "failed" if data.get("is_error") or out.returncode else "done"
        except ValueError:
            job["result"] = _cap(out.stdout)
            job["status"] = "failed"
        job["stderr"] = _cap(out.stderr[-4000:])
    except Exception as e:
        job.update(status="failed", result=f"{type(e).__name__}: {e}")
    job["summary"] = (job.get("result") or "")[:500]
    job["finished"] = time.time()


@logged
def delegate(task: str, cwd: str, harness: str = "claude_code", model: str | None = None) -> dict:
    """Run a coding task with Claude Code (`claude -p`) in the background; returns a job_id."""
    if harness != "claude_code":
        raise Refused(f"unsupported harness: {harness}")
    wd = safe_path(cwd)
    argv = [CFG["claude_bin"], "-p", task, "--permission-mode", "acceptEdits",
            "--disallowedTools", "Bash(git push:*)", "Bash(rm:*)", "Bash(sudo:*)",
            "--output-format", "json", "--append-system-prompt", NO_ATTRIBUTION]
    if model:
        argv += ["--model", model]
    # shortcut: jobs live in memory and are lost on restart; persist them if long jobs matter
    job = {"job_id": uuid.uuid4().hex[:12], "status": "running", "task": task, "cwd": str(wd),
           "model": model, "started": time.time()}
    JOBS[job["job_id"]] = job
    threading.Thread(target=_run_job, args=(job, argv, wd), daemon=True).start()
    return {"status": "ok", "job_id": job["job_id"]}


@logged
def job_status(job_id: str) -> dict:
    """Status, result and summary of a delegated job."""
    if job_id not in JOBS:
        raise Refused("unknown job_id")
    return {**JOBS[job_id], "job_status": JOBS[job_id]["status"], "status": "ok"}


@logged
def list_jobs() -> dict:
    """All delegated jobs (without full results)."""
    return {"status": "ok", "jobs": [{k: j.get(k) for k in ("job_id", "status", "task", "cwd", "started", "finished",
                                                            "summary")} for j in JOBS.values()]}


PRIVATE_NETS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "100.64.0.0/10", "::/128", "::1/128", "fe80::/10", "fc00::/7")]


def check_url(url: str) -> None:
    """Refuse non-http(s) URLs and hosts resolving to loopback/link-local/private/Tailscale addresses."""
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise Refused(f"only http/https URLs: {url[:100]}")
    host = u.hostname.lower()
    if host in {h.lower() for h in CFG["browse_allow_private"]}:
        return
    try:
        infos = socket.getaddrinfo(host, u.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise Refused(f"cannot resolve host: {host}")
    for *_, sa in infos:
        ip = ipaddress.ip_address(sa[0].split("%")[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if any(ip in n for n in PRIVATE_NETS):
            raise Refused(f"private address refused: {host} -> {ip}")


# One thread owns the Playwright sync API (thread-bound), so one browser is reused and calls run one at a time.
_BROWSE_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="browse")
_PW: dict = {}


def _browser():
    from playwright.sync_api import Error, sync_playwright
    if _PW.get("browser") and _PW["browser"].is_connected():
        return _PW["browser"]
    try:
        _PW.setdefault("pw", sync_playwright().start())
        _PW["browser"] = _PW["pw"].chromium.launch(headless=True)
    except Error as e:
        raise RuntimeError("chromium unavailable (run `uv run playwright install chromium`): "
                           + str(e).strip().splitlines()[0])
    return _PW["browser"]


def _browse(url: str, selector: str, wait_for: str | None, max_chars: int, limit_s: float) -> dict:
    from playwright.sync_api import Error
    deadline = time.monotonic() + limit_s
    left = lambda: max(1, int((deadline - time.monotonic()) * 1000))  # noqa: E731  (0 would mean no timeout)
    ctx = _browser().new_context(accept_downloads=False, service_workers="block")
    page = ctx.new_page()
    blocked, redirect = [], []

    def guard(route):
        # Every request is re-checked here (Chromium's own URL parsing); redirects are not followed in-browser
        # because hops bypass routing: a main-frame redirect is re-navigated by goto() below, so it is checked too.
        req = route.request
        main = req.is_navigation_request() and req.frame.parent_frame is None
        try:
            check_url(req.url)
        except Refused as e:
            if main:
                blocked.append(str(e))
            return route.abort("blockedbyclient")
        if req.resource_type in ("image", "media", "font"):
            return route.abort()
        resp = route.fetch(max_redirects=0, timeout=left())
        if 300 <= resp.status < 400 and resp.headers.get("location"):
            if main:
                redirect.append(urljoin(req.url, resp.headers["location"]))
            return route.abort()
        route.fulfill(response=resp)

    try:
        page.route("**/*", guard)
        page.route_web_socket("**/*", lambda ws: ws.close())
        for _ in range(10):
            redirect.clear()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=left())
            except Error:
                if blocked:
                    raise Refused(blocked[0])
                if not redirect:
                    raise
            if not redirect:
                break
            url = redirect[0]
        else:
            raise Refused("too many redirects")
        check_url(page.url)
        if wait_for:
            page.wait_for_selector(wait_for, timeout=left())
        else:
            try:
                page.wait_for_load_state("networkidle", timeout=left())
            except Error:
                pass  # busy pages never go idle; read what has rendered
        texts = page.locator(selector).all_inner_texts()
        if not texts:
            raise ValueError(f"selector matched nothing: {selector}")
        text = " ".join(" ".join(texts).split())
        return {"status": "ok", "url": page.url, "title": page.title(), "text": text[:max_chars],
                "truncated": len(text) > max_chars}
    finally:
        ctx.close()


@logged
def browse(url: str, selector: str | None = None, wait_for: str | None = None, max_chars: int | None = None) -> dict:
    """Open a web page in headless Chromium (renders JavaScript) and return its visible text.
    Optional CSS `selector` (default body), `wait_for` selector to wait for, `max_chars` cap (default 8000)."""
    if not CFG["browse_enabled"]:
        raise Refused("browse is disabled")
    check_url(url)
    limit = min(CFG["browse_timeout_s"], 20)
    cap = min(max_chars or 8000, CFG["browse_max_chars"])
    fut = _BROWSE_POOL.submit(_browse, url, selector or "body", wait_for, cap, limit)
    return fut.result(timeout=limit + 30)


TOOLS = [list_dir, read_file, search, run_command, delegate, job_status, list_jobs, browse]


# ---------- HTTP app ----------

class BearerAuth:
    """ASGI middleware: 401 unless `Authorization: Bearer <token>` matches (constant-time)."""

    def __init__(self, app, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def build_app(cfg: dict):
    CFG.clear()
    CFG.update(cfg)
    server = MCPServer(name=f"biseo-daemon-{cfg['id']}")
    for t in TOOLS:
        server.tool()(t)
    # Host-header check (DNS rebinding) for our bind address; the bearer token is the real gate.
    sec = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                    allowed_hosts=[f"{cfg['host']}:*", cfg["host"]], allowed_origins=[])
    app = server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                                     transport_security=sec, host=cfg["host"])
    return BearerAuth(app, cfg["token"])


async def register_loop(cfg: dict) -> None:
    body = {"id": cfg["id"], "name": cfg["name"], "url": f"http://{cfg['host']}:{cfg['port']}/mcp",
            "harness": ["claude_code"]}
    async with httpx.AsyncClient(timeout=10, headers={"Authorization": f"Bearer {cfg['token']}"}) as c:
        while True:
            try:
                (await c.post(cfg["gateway_url"].rstrip("/") + "/v1/devices/register", json=body)).raise_for_status()
            except httpx.HTTPError as e:
                log.warning("gateway register failed (will retry): %s", type(e).__name__)
            await asyncio.sleep(60)


async def serve(cfg: dict) -> None:
    if cfg["host"] in ("0.0.0.0", "::", ""):
        raise SystemExit("refusing to bind all interfaces; set host to the Tailscale IP")
    app = build_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host=cfg["host"], port=cfg["port"], log_level="info"))
    reg = asyncio.create_task(register_loop(cfg))
    try:
        await server.serve()
    finally:
        reg.cancel()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="~/.config/biseo/daemon.toml")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(serve(load_config(ap.parse_args().config)))


if __name__ == "__main__":
    main()
