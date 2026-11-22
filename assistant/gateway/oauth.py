"""OAuth 2.1 with PKCE (S256) for connectors: built-in API clients (Spotify) and custom MCP servers.

Flow (the user consents once, in their own browser; the gateway never sees a password):
  1. POST /v1/connectors/{name}/authorize  -> {"url", "state", "redirect_uri", "expires_in"}: the app opens `url`.
  2. The provider redirects to `redirect_uri` with ?code&state:
       - azel://oauth/callback (default): the app forwards both to POST /v1/connectors/{name}/authorize/finish;
       - http://127.0.0.1:<port>/... : an app-side loopback listener does the same;
       - the gateway's own GET /v1/oauth/callback: its tailnet address, or its public HTTPS address
         (BISEO_PUBLIC_URL, through the proxy) for providers that want HTTPS, like Spotify.
  3. finish() trades the code (with the PKCE verifier kept here, 10 minutes) for tokens.

Client registration, in order: a pre-registered client id (the request's, else the stored one), else RFC 7591
Dynamic Client Registration when the authorization server offers it. Endpoints: a built-in provider's own, or, for
an MCP server, discovered from its 401 (RFC 9728 protected-resource metadata, then RFC 8414 / OIDC metadata).

Tokens: ~/.config/biseo/oauth/<name>.json (0600, atomic write; the folder 0700). Refresh runs under a per-name
lock (refresh tokens may rotate and be single-use). status(): needs_consent | connected | expired (a refresh the
provider refused: consent again). Tokens are never printed, logged, returned over the API or sent anywhere but
the provider's token endpoint and the API they are for.
"""
import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

PENDING_S = 600
SLUG = re.compile(r"[A-Za-z0-9_-]{1,40}")
APP_REDIRECT = "azel://oauth/callback"
PROVIDERS = {
    "spotify": {"authorization_endpoint": "https://accounts.spotify.com/authorize",
                "token_endpoint": "https://accounts.spotify.com/api/token",
                "scopes": ["user-read-playback-state", "user-modify-playback-state", "user-read-currently-playing"]},
}
_pending: dict[str, dict] = {}  # state -> {name, verifier, redirect_uri, cfg, client_id, at}
_locks: dict[str, asyncio.Lock] = {}


class Expired(Exception):
    """No usable grant: never connected, or the provider refused the refresh (consent again)."""


# ---------- the token file ----------
def path(home: Path, name: str) -> Path:
    if not SLUG.fullmatch(name or ""):
        raise ValueError("name: an ASCII slug")
    return home / "oauth" / f"{name}.json"


def load(home, name):
    p = path(home, name)
    if not p.is_file() or p.stat().st_mode & 0o077:
        return None  # missing, or readable by others: treated as not connected
    try:
        d = json.loads(p.read_text())
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def save(home, name, data):
    p = path(home, name)
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    p.parent.chmod(0o700)
    tmp = p.with_name(f".{name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, p)


def forget(home, name):
    path(home, name).unlink(missing_ok=True)


def status(home, name):
    d = load(home, name)
    if not d or not (d.get("refresh_token") or d.get("access_token")):
        return "needs_consent"
    if d.get("expired") or (not d.get("refresh_token") and time.time() >= d.get("expires_at", 0)):
        return "expired"
    return "connected"


def bearer(home, name):
    """{"Authorization": "Bearer …"} while the access token is valid, else None (sync, for a run's --mcp-config;
    refresh_due keeps it fresh)."""
    d = load(home, name)
    if d and d.get("access_token") and not d.get("expired") and time.time() < d.get("expires_at", 0) - 30:
        return {"Authorization": f"Bearer {d['access_token']}"}
    return None


# ---------- endpoint safety ----------
def safe_endpoint(url):
    """https to a public host, or http to a tailnet address: no loopback, private or link-local targets (SSRF)."""
    u = urlsplit(url or "")
    host = u.hostname or ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    tailnet = ip is not None and ip in ipaddress.ip_network("100.64.0.0/10")
    if u.scheme == "http":
        return tailnet
    if u.scheme != "https" or not host or host == "localhost" or host.endswith(".localhost"):
        return False
    return tailnet or ip is None or not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)


def check_redirect(uri, gateway_callback):
    """The places a code may be sent: the app's scheme, a loopback port, or this gateway's own callback (one URL,
    or several: its tailnet address and its public HTTPS one)."""
    ours = (gateway_callback,) if isinstance(gateway_callback, str) else tuple(gateway_callback)
    if uri in (APP_REDIRECT, *ours):
        return uri
    u = urlsplit(uri or "")
    if u.scheme == "http" and u.hostname == "127.0.0.1" and u.port and not u.query and not u.fragment:
        return uri
    raise ValueError(f"redirect_uri: {APP_REDIRECT}, http://127.0.0.1:<port>/…, or {' / '.join(ours)}")


# ---------- discovery and registration (MCP servers) ----------
def _resource_metadata_url(resp, server_url):
    m = re.search(r'resource_metadata="([^"]+)"', resp.headers.get("www-authenticate", ""))
    if m:
        return m.group(1)
    u = urlsplit(server_url)
    return f"{u.scheme}://{u.netloc}/.well-known/oauth-protected-resource{u.path.rstrip('/')}"


async def discover(server_url, http):
    """Authorization endpoints for an MCP server: its 401 -> protected-resource metadata -> authorization server
    metadata (RFC 8414, then OIDC discovery)."""
    resp = await http.post(server_url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "azel", "version": "1"}}})
    prm_url = _resource_metadata_url(resp, server_url)
    if not safe_endpoint(prm_url):
        raise ValueError("protected-resource metadata URL is not allowed")
    r = await http.get(prm_url)
    r.raise_for_status()
    prm = r.json()
    issuer = (prm.get("authorization_servers") or [None])[0]
    if not isinstance(issuer, str) or not safe_endpoint(issuer):
        raise ValueError("authorization server is missing or not allowed")
    u = urlsplit(issuer)
    base, tail = f"{u.scheme}://{u.netloc}", u.path.rstrip("/")
    meta = None
    for well_known in (f"{base}/.well-known/oauth-authorization-server{tail}",
                       f"{base}/.well-known/openid-configuration{tail}", f"{issuer.rstrip('/')}/.well-known/openid-configuration"):
        r = await http.get(well_known)
        if r.status_code == 200:
            meta = r.json()
            break
    if not meta:
        raise ValueError("authorization server metadata not found")
    cfg = {k: meta.get(k) for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint")}
    for k in ("authorization_endpoint", "token_endpoint"):
        if not safe_endpoint(cfg[k]):
            raise ValueError(f"{k} is missing or not allowed")
    if cfg["registration_endpoint"] and not safe_endpoint(cfg["registration_endpoint"]):
        cfg["registration_endpoint"] = None
    if "S256" not in meta.get("code_challenge_methods_supported", ["S256"]):
        raise ValueError("the server does not support PKCE S256")
    cfg["resource"] = prm.get("resource") or server_url
    cfg["scopes"] = list(prm.get("scopes_supported") or [])
    return cfg


async def register(cfg, redirect_uri, http):
    """RFC 7591 dynamic registration as a public client (no secret); the client id is stored with the tokens."""
    r = await http.post(cfg["registration_endpoint"], json={
        "client_name": "Azel", "redirect_uris": [redirect_uri], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"})
    r.raise_for_status()
    cid = r.json().get("client_id")
    if not isinstance(cid, str) or not cid:
        raise ValueError("registration returned no client_id")
    return cid


# ---------- consent ----------
async def authorize(home, name, http, gateway_callback, server_url=None, client_id=None, redirect_uri=None):
    """The authorization URL for the app to open. `server_url`: a custom MCP connector's URL (discovery);
    otherwise `name` must be a built-in provider."""
    redirect = check_redirect(redirect_uri or APP_REDIRECT, gateway_callback)
    stored = load(home, name) or {}
    if name in PROVIDERS:
        cfg = dict(PROVIDERS[name])
    elif server_url:
        cfg = await discover(server_url, http)
    else:
        raise LookupError(f"no OAuth provider or connector named {name}")
    if client_id is not None and not (isinstance(client_id, str) and re.fullmatch(r"[\w.:\-]{1,200}", client_id)):
        raise ValueError("client_id")
    # a pre-registered id (built-in provider) is reused for any allowed redirect; a DCR id only for its own
    reuse = name in PROVIDERS or stored.get("redirect_uri") in (None, redirect)
    cid = client_id or (stored.get("client_id") if reuse else None)
    if not cid:
        if not cfg.get("registration_endpoint"):
            raise ValueError("client_id: register an app with the provider and send its client id")
        cid = await register(cfg, redirect, http)
    now = time.time()
    for s in [s for s, p in _pending.items() if now - p["at"] > PENDING_S]:
        _pending.pop(s, None)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    _pending[state] = {"name": name, "verifier": verifier, "redirect_uri": redirect, "cfg": cfg, "client_id": cid, "at": now}
    q = {"response_type": "code", "client_id": cid, "redirect_uri": redirect, "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256"}
    if cfg.get("scopes"):
        q["scope"] = " ".join(cfg["scopes"])
    if cfg.get("resource"):
        q["resource"] = cfg["resource"]
    return {"url": f"{cfg['authorization_endpoint']}?{urlencode(q)}", "state": state, "redirect_uri": redirect,
            "expires_in": PENDING_S}


def pending_name(state):
    p = _pending.get(state or "")
    return p["name"] if p and time.time() - p["at"] <= PENDING_S else None


async def finish(home, name, code, state, http):
    """Trade the code for tokens; the state is single-use. Returns the new status."""
    p = _pending.pop(state or "", None)
    if not p or p["name"] != name or time.time() - p["at"] > PENDING_S:
        raise ValueError("state: unknown or expired, start again")
    if not isinstance(code, str) or not 0 < len(code) <= 2048:
        raise ValueError("code")
    cfg = p["cfg"]
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": p["redirect_uri"],
            "client_id": p["client_id"], "code_verifier": p["verifier"]}
    if cfg.get("resource"):
        data["resource"] = cfg["resource"]
    r = await http.post(cfg["token_endpoint"], data=data)
    if r.status_code != 200:
        raise ValueError(f"the provider refused the code (HTTP {r.status_code})")
    t = r.json()
    if not t.get("access_token"):
        raise ValueError("the provider returned no access token")
    save(home, name, {"client_id": p["client_id"], "redirect_uri": p["redirect_uri"],
                      "token_endpoint": cfg["token_endpoint"], "resource": cfg.get("resource"),
                      "scopes": t.get("scope", " ".join(cfg.get("scopes") or [])).split(),
                      "access_token": t["access_token"], "refresh_token": t.get("refresh_token"),
                      "expires_at": time.time() + float(t.get("expires_in", 3600)), "created": time.time()})
    return status(home, name)


# ---------- access tokens ----------
async def access_token(home, name, http, min_left=60):
    """A valid access token, refreshed under the name's lock when it has less than `min_left` seconds left."""
    d = load(home, name)
    if not d or d.get("expired"):
        raise Expired()
    if time.time() < d.get("expires_at", 0) - min_left:
        return d["access_token"]
    async with _locks.setdefault(name, asyncio.Lock()):
        d = load(home, name)  # another caller may have refreshed meanwhile
        if not d or d.get("expired"):
            raise Expired()
        if time.time() < d.get("expires_at", 0) - min_left:
            return d["access_token"]
        if not d.get("refresh_token"):
            raise Expired()
        data = {"grant_type": "refresh_token", "refresh_token": d["refresh_token"], "client_id": d["client_id"]}
        if d.get("resource"):
            data["resource"] = d["resource"]
        r = await http.post(d["token_endpoint"], data=data)
        if r.status_code in (400, 401):
            save(home, name, {**d, "expired": True, "expired_at": time.time()})
            raise Expired()
        r.raise_for_status()
        t = r.json()
        save(home, name, {**d, "access_token": t["access_token"],
                          "refresh_token": t.get("refresh_token") or d["refresh_token"],  # rotated or kept
                          "expires_at": time.time() + float(t.get("expires_in", 3600))})
        return t["access_token"]


async def refresh_due(home, http, within_s=300):
    """Refresh every grant that expires soon (the gateway runs this each minute, so runs find fresh tokens)."""
    d = home / "oauth"
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        g = load(home, p.stem)
        if g and g.get("refresh_token") and not g.get("expired") and time.time() > g.get("expires_at", 0) - within_s:
            try:
                await access_token(home, p.stem, http, min_left=within_s)
            except (Expired, httpx.HTTPError, KeyError, ValueError) as e:
                print(f"oauth refresh failed for {p.stem}: {type(e).__name__}", flush=True)
