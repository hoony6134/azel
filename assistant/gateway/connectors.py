"""Which MCP servers a gateway's Claude runs load: the user picks them (shared setting `connectors`).

Every `claude -p` used to wait for every MCP server the login has (all claude.ai connectors, every plugin's
servers, user-scope stdio servers): 89 servers, 915 tools, and a 30 s stall whenever one of them hangs. Each run now
gets a run-scoped `--settings` (never the user's global settings):
  - every installed plugin off (`enabledPlugins`), so no plugin MCP servers, hooks or skills;
  - `allowedMcpServers` by URL: the enabled claude.ai connectors, the enabled custom servers and the device daemons;
plus `ENABLE_TOOL_SEARCH=false`, so the few tools left load up front and no ToolSearch round trip happens.

Setting (PROTOCOL.md section 3):
  {"claude_ai": {"<connector name>": bool}, "custom": [{"name", "url", "transport": "http"|"sse", "enabled",
   "auth_ref"?, "oauth"?, "allowed_tools"?: [name or glob]}]}
Connector names are claude.ai's display names without the "claude.ai " prefix ("Gmail"). A name missing from
`claude_ai` takes its default: DEFAULT_ON on, everything else off. Custom servers' secrets never go into settings:
`auth_ref` names a 0600 file under ~/.config/biseo/mcp/ on each full gateway (a raw token → `Authorization: Bearer`,
or a JSON object of headers), written through the write-only `PUT /v1/connectors/secret/{name}`.
`allowed_tools`: names or globs of the server's tools a run may call; empty or missing means all. Only those
`mcp__<server>__<pattern>` entries reach `--allowedTools`, so in `claude -p` (no one to answer a prompt) the rest are
refused. Any other path that calls a custom server must ask `tool_allowed`.
"""
import asyncio
import fnmatch
import ipaddress
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import oauth

NO_DELEGATE = {"sadr"}  # devices never handed a delegate (team devices may, since batch 8)

DEFAULT_ON = ("Cygnus MCP", "Google Calendar", "Gmail")
SLUG = re.compile(r"[A-Za-z0-9_-]{1,40}")
LIST_RE = re.compile(r"^claude\.ai (.+?): (\S+) - (.*)$")
MAX_SECRET = 8192
TOOL_PAT = re.compile(r"[A-Za-z0-9_.*?-]{1,80}")  # a tool name or a glob over names


class Discovery:
    """claude.ai connectors and installed plugins as this gateway's Claude login sees them. Filled at startup in the
    background (`claude mcp list` takes ~15 s: it health-checks every server) and on demand."""

    def __init__(self, claude="claude"):
        self.claude, self.connectors, self.plugins, self.at = claude, [], [], 0.0
        self.lock = asyncio.Lock()

    async def refresh(self):
        async with self.lock:
            listed = await _run(self.claude, "mcp", "list")
            plugins = await _run(self.claude, "plugin", "list", "--json")
            if listed is not None:
                self.connectors = parse_mcp_list(listed)
            try:
                self.plugins = [p["id"] for p in json.loads(plugins or "[]") if p.get("enabled", True)]
            except (ValueError, TypeError, KeyError):
                pass
            self.at = time.time()


async def _run(*cmd, timeout=90):
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return out.decode()
    except (OSError, asyncio.TimeoutError):
        return None


def parse_mcp_list(text):
    """`claude mcp list` lines for claude.ai connectors → [{"name","url","status"}]."""
    out = []
    for line in text.splitlines():
        if m := LIST_RE.match(line.strip()):
            st = m.group(3).lower()
            status = "connected" if "connected" in st and "fail" not in st else "needs_auth" if "auth" in st else "failed"
            out.append({"name": m.group(1), "url": m.group(2), "status": status})
    return out


def check(conn):
    """The `connectors` setting, validated (a trust boundary: the app writes it). ValueError says what is wrong."""
    if not isinstance(conn, dict) or set(conn) - {"claude_ai", "custom"}:
        raise ValueError("connectors: {\"claude_ai\": {name: bool}, \"custom\": [...]}")
    ai = conn.get("claude_ai", {})
    if not isinstance(ai, dict) or len(ai) > 100 or \
            not all(isinstance(k, str) and 0 < len(k) <= 80 and isinstance(v, bool) for k, v in ai.items()):
        raise ValueError("connectors.claude_ai: {connector name: true|false}")
    custom = conn.get("custom", [])
    if not isinstance(custom, list) or len(custom) > 20:
        raise ValueError("connectors.custom: a list of at most 20 servers")
    names = set()
    for c in custom:
        if not isinstance(c, dict) or set(c) - {"name", "url", "transport", "enabled", "auth_ref", "oauth", "allowed_tools"}:
            raise ValueError("connectors.custom[]: name, url, transport, enabled, auth_ref, oauth, allowed_tools")
        if not isinstance(c.get("oauth", False), bool):
            raise ValueError("connectors.custom[].oauth: true for a server that signs in with OAuth")
        if not (isinstance(c.get("name"), str) and SLUG.fullmatch(c["name"])) or c["name"] in names:
            raise ValueError("connectors.custom[].name: a unique ASCII slug")
        if c["name"].lower() in oauth.PROVIDERS:  # its grant is the built-in provider's: never a custom server's
            raise ValueError(f"connectors.custom[].name: {c['name']} is reserved")
        names.add(c["name"])
        if not (isinstance(c.get("url"), str) and re.fullmatch(r"https?://[^\s]{1,500}", c["url"]) and secure_url(c["url"])):
            raise ValueError("connectors.custom[].url: https, or http only to a tailnet address (100.64.0.0/10)")
        if c.get("transport", "http") not in ("http", "sse") or not isinstance(c.get("enabled", True), bool):
            raise ValueError("connectors.custom[]: transport http|sse, enabled bool")
        if c.get("auth_ref") is not None and c["auth_ref"] != c["name"]:
            raise ValueError("connectors.custom[].auth_ref: the server's own name (secrets are never shared)")
        t = c.get("allowed_tools", [])
        if not (isinstance(t, list) and len(t) <= 50 and all(isinstance(x, str) and TOOL_PAT.fullmatch(x) for x in t)):
            raise ValueError("connectors.custom[].allowed_tools: at most 50 tool names or globs ([A-Za-z0-9_.*?-])")
    return {"claude_ai": dict(ai), "custom": [{"transport": "http", "enabled": True, **c} for c in custom]}


def secure_url(url):
    """https anywhere; plain http only to a tailnet IP (100.64.0.0/10), where WireGuard already encrypts."""
    u = urlsplit(url)
    if u.scheme == "https":
        return True
    try:
        return u.scheme == "http" and ipaddress.ip_address(u.hostname or "") in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def enabled(conn, name):
    return conn.get("claude_ai", {}).get(name, name in DEFAULT_ON)


def tool_allowed(conn, server, tool):
    """May a run call `tool` of custom server `server`? An empty allowlist means every tool."""
    for c in conn.get("custom", []):
        if c["name"] == server:
            pats = c.get("allowed_tools") or []
            return not pats or any(fnmatch.fnmatchcase(tool, p) for p in pats)
    return False


def view(conn, disc, home):
    """GET /v1/connectors: what this gateway can load and what is switched on. Never any secret."""
    known = {c["name"] for c in disc.connectors}
    ai = [{**c, "enabled": enabled(conn, c["name"])} for c in disc.connectors]
    ai += [{"name": n, "url": None, "status": "unknown", "enabled": v}  # set, but not visible to this login
           for n, v in conn.get("claude_ai", {}).items() if n not in known]
    custom = [{**c, "has_secret": bool(c.get("auth_ref")) and (home / "mcp" / c["auth_ref"]).is_file()}
              for c in conn.get("custom", [])]
    return {"claude_ai": ai, "custom": custom, "discovered_at": disc.at or None}


def put_secret(home, name, value):
    """Write-only: the secret lands in a 0600 file and is never read back over the API."""
    if not SLUG.fullmatch(name or ""):
        raise ValueError("name: an ASCII slug")
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_SECRET:
        raise ValueError(f"value: a non-empty string up to {MAX_SECRET} characters")
    d = home / "mcp"
    d.mkdir(mode=0o700, exist_ok=True)
    d.chmod(0o700)
    fd = os.open(d / f".{name}.tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value.strip())
    os.replace(d / f".{name}.tmp", d / name)


def _headers(home, ref):
    if not ref:
        return {}
    path = home / "mcp" / ref
    if not path.is_file() or path.stat().st_mode & 0o077:
        return None  # missing or readable by others: the server is skipped rather than called without auth
    raw = path.read_text().strip()
    try:
        h = json.loads(raw)
        if isinstance(h, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in h.items()):
            return h
    except ValueError:
        pass
    return {"Authorization": f"Bearer {raw}"}


def run_args(conn, disc, devices, token, home, tokens=None, extra=None, own_only=False):
    """(mcp_servers for --mcp-config, allowed tool prefixes, run-scoped --settings JSON, extra env) for one run.
    `tokens`: daemon url -> its own bearer (team devices); others get `token`. A limited device's `delegate` is
    denied in the run's settings."""
    tokens = tokens or {}
    servers = {d["id"]: {"type": "http", "url": d["url"],
                         "headers": {"Authorization": f"Bearer {tokens.get(d['url'], token)}", **(extra or {})}}
               for d in devices}
    if own_only:  # a secret call: the device daemons only (the gateway adds its own azel server), nothing that stores
        conn, disc = {"claude_ai": {}, "custom": []}, type("NoConnectors", (), {"connectors": [], "plugins": disc.plugins})
    for c in conn.get("custom", []):
        h = _headers(home, c.get("auth_ref"))
        if c.get("oauth"):
            # its live access token (oauth.py refreshes it each minute) on top of any static headers from a secret
            # file (an API key); no token yet, or an unsafe secret file: skipped like a server whose secret is missing
            ref = c.get("auth_ref")
            static = h if ref and (home / "mcp" / ref).exists() else {}
            token = oauth.bearer(home, c["name"], c["url"])
            h = None if static is None or token is None else {**static, **token}
        if c.get("enabled", True) and h is not None and c["name"] not in servers:
            servers[c["name"]] = {"type": c.get("transport", "http"), "url": c["url"], **({"headers": h} if h else {})}
    ai = [c for c in disc.connectors if enabled(conn, c["name"])]
    pats = {c["name"]: c.get("allowed_tools") or [] for c in conn.get("custom", [])}
    allowed = [f"mcp__claude_ai_{re.sub(r'[^A-Za-z0-9_-]', '_', c['name'])}" for c in ai]
    for s in servers:  # a custom server with an allowlist grants only those tools (shortcut: globs rely on Claude Code's own matching)
        allowed += [f"mcp__{s}__{p}" for p in pats[s]] if pats.get(s) else [f"mcp__{s}"]
    settings = {"enabledPlugins": {p: False for p in disc.plugins}}
    if deny := [f"mcp__{d['id']}__delegate" for d in devices if d["id"] in NO_DELEGATE]:
        settings["permissions"] = {"deny": deny}
    if disc.connectors or own_only:  # shortcut: until discovery ran, every claude.ai connector loads (slow, but nothing missing)
        settings["allowedMcpServers"] = [{"serverUrl": c["url"]} for c in ai] + \
                                        [{"serverUrl": s["url"]} for s in servers.values()]
    return servers or None, allowed, json.dumps(settings, sort_keys=True), {"ENABLE_TOOL_SEARCH": "false"}
