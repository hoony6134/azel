"""Google Calendar and Gmail, read-only, for the API paths (Claude API, NIM, Qwen) on full gateways on the Mac.

The claude.ai connectors are not reachable from the API, so the gateway holds its own OAuth grant:
  ~/.config/biseo/google_client.json  the user's "Desktop app" OAuth client (0600)
  ~/.config/biseo/google_oauth.json   the refresh token and the granted scopes (0600), written by `consent`
Neither is ever printed, logged or sent anywhere but Google's token endpoint. Limited gateways never load them.

One-time consent (the user does it in their own browser; nothing here sees the password):
  uv run python google_tools.py consent [--scopes read]
A scope set is named so adding write scopes later is the same command with another set (a re-consent with the same
client). While the consent screen is in Testing, Google expires refresh tokens after 7 days: a refresh that answers
invalid_grant marks the grant expired, calendar and mail go back to claude_code, and /v1/status says "expired".

Tools (results trimmed: no attachments, capped bodies, no HTML):
  calendar_events {from, to}   events in the user's selected calendars, YYYY-MM-DD, at most 62 days
  gmail_search {query, max}    message ids with date, sender, subject and snippet (max ≤ 10)
  gmail_read {id}              one message's headers and plain text (≤ 3000 chars)
"""
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

KST = timezone(timedelta(hours=9))
SCOPE_SETS = {
    "read": ["https://www.googleapis.com/auth/calendar.readonly", "https://www.googleapis.com/auth/gmail.readonly"],
    # later, each behind spoken confirmation: re-run consent with one of these
    "calendar_write": ["https://www.googleapis.com/auth/calendar.readonly",
                       "https://www.googleapis.com/auth/gmail.readonly",
                       "https://www.googleapis.com/auth/calendar.events"],
    "mail_write": ["https://www.googleapis.com/auth/calendar.readonly", "https://www.googleapis.com/auth/gmail.readonly",
                   "https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/gmail.compose",
                   "https://www.googleapis.com/auth/gmail.modify"],
}
CAL = "https://www.googleapis.com/calendar/v3"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
BODY_CHARS, MAX_RESULT = 3000, 4000
_access: dict = {"token": None, "exp": 0.0}


class Expired(Exception):
    """The refresh token was revoked or expired (invalid_grant): the user has to consent again."""


def _paths(home):
    return home / "google_client.json", home / "google_oauth.json"


def _private(path):
    return path.is_file() and path.stat().st_mode & 0o077 == 0


def _write_private(path, data):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _client(home):
    path = _paths(home)[0]
    if not _private(path):
        raise FileNotFoundError("google_client.json missing or not 0600")
    d = json.loads(path.read_text())
    return d.get("installed") or d.get("web")


def status(home):
    """"connected" | "expired" | "none" (for GET /v1/status and for routing)."""
    client, grant = _paths(home)
    if not (_private(client) and _private(grant)):
        return "none"
    try:
        g = json.loads(grant.read_text())
    except ValueError:
        return "none"
    return "expired" if g.get("expired") else "connected" if g.get("refresh_token") else "none"


# ---------- one-time consent: loopback redirect, PKCE, the user's own browser ----------
def consent(home, scope_set="read", open_browser=True, timeout_s=300):
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse
    c = _client(home)
    scopes = SCOPE_SETS[scope_set]
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    got = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                got.update(code=q.get("code", [""])[0], error=q.get("error", [""])[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("Azel: Google 연결을 마쳤어요. 이 창은 닫아도 돼요.".encode())

        def log_message(self, *a):  # the redirect carries the code: never log it
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    url = c["auth_uri"] + "?" + urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": " ".join(scopes),
        "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print("브라우저에서 Google 계정으로 허용해 주세요. 창이 안 열리면 이 주소를 여세요:\n" + url, flush=True)
    if open_browser:
        webbrowser.open(url)
    server.timeout = 1
    end = time.time() + timeout_s
    while not got and time.time() < end:
        server.handle_request()
    server.server_close()
    if not got.get("code"):
        raise SystemExit(f"연결하지 못했어요: {got.get('error') or '시간 초과'}")
    r = httpx.post(c["token_uri"], data={"code": got["code"], "client_id": c["client_id"],
                                         "client_secret": c["client_secret"], "redirect_uri": redirect,
                                         "grant_type": "authorization_code", "code_verifier": verifier}, timeout=30)
    if r.status_code != 200:
        raise SystemExit(f"토큰 교환이 실패했어요 (HTTP {r.status_code}).")
    t = r.json()
    if not t.get("refresh_token"):
        raise SystemExit("Google이 refresh token을 주지 않았어요. myaccount.google.com/permissions에서 이 앱을 지우고 다시 해 주세요.")
    granted = t.get("scope", "").split()
    _write_private(_paths(home)[1], {"refresh_token": t["refresh_token"], "scopes": granted, "scope_set": scope_set,
                                     "created": time.time(), "expired": False})
    _access.update(token=None, exp=0.0)
    return granted


# ---------- access tokens ----------
async def access_token(home, client=None):
    if _access["token"] and time.time() < _access["exp"] - 60:
        return _access["token"]
    c, grant_path = _client(home), _paths(home)[1]
    g = json.loads(grant_path.read_text())
    if g.get("expired"):
        raise Expired()
    http = client or httpx.AsyncClient(timeout=20)
    try:
        r = await http.post(c["token_uri"], data={"client_id": c["client_id"], "client_secret": c["client_secret"],
                                                  "refresh_token": g["refresh_token"], "grant_type": "refresh_token"})
    finally:
        if client is None:
            await http.aclose()
    if r.status_code == 400 and r.json().get("error") == "invalid_grant":
        _write_private(grant_path, {**g, "expired": True, "expired_at": time.time()})
        raise Expired()
    r.raise_for_status()
    t = r.json()
    _access.update(token=t["access_token"], exp=time.time() + float(t.get("expires_in", 3600)))
    return _access["token"]


# ---------- tools ----------
DEFS = [
    {"name": "calendar_events", "description": "구글 캘린더 일정(사용자가 표시하는 캘린더 전부). from~to는 YYYY-MM-DD, 최대 62일.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["from", "to"], "properties": {
         "from": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
         "to": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"}}}},
    {"name": "gmail_search", "description": "지메일 검색(Gmail 검색어 그대로, 예: is:unread newer_than:2d). 날짜, 보낸 사람, 제목, "
                                            "미리보기와 id를 돌려준다. 본문은 gmail_read로.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
         "query": {"type": "string", "maxLength": 200}, "max": {"type": "integer", "minimum": 1, "maximum": 10}}}},
    {"name": "gmail_read", "description": "지메일 메시지 하나의 보낸 사람, 받는 사람, 제목, 날짜와 본문 텍스트(앞부분만, 첨부 제외).",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["id"], "properties": {
         "id": {"type": "string", "pattern": "^[A-Za-z0-9]{8,40}$"}}}},
]


def bridge(home):
    """(definitions, run) when this host holds a live grant, else ([], None)."""
    if status(home) != "connected":
        return [], None

    async def run(name, args):
        return await call(home, name, args)
    return DEFS, run


def _check(name, args):
    args = args if isinstance(args, dict) else {}
    if name == "calendar_events":
        f, t = (Date.fromisoformat(str(args.get(k, ""))) for k in ("from", "to"))
        if not 0 <= (t - f).days <= 62:
            raise ValueError("기간은 62일 안쪽")
        return {"from": f, "to": t}
    if name == "gmail_search":
        q = args.get("query")
        if not isinstance(q, str) or not q.strip() or len(q) > 200 or any(ord(c) < 32 for c in q):
            raise ValueError("검색어는 200자 안쪽 글")
        m = args.get("max", 5)
        return {"query": q.strip(), "max": m if isinstance(m, int) and 1 <= m <= 10 else 5}
    if name == "gmail_read":
        i = args.get("id")
        if not (isinstance(i, str) and re.fullmatch(r"[A-Za-z0-9]{8,40}", i)):
            raise ValueError("id")
        return {"id": i}
    raise KeyError(name)


async def call(home, name, args, client=None):
    """One tool; always a short Korean string, never an exception, never a body in a log."""
    try:
        a = _check(name, args)
    except KeyError:
        return f"모르는 도구예요: {str(name)[:40]}"
    except ValueError as e:
        return f"도구 인자가 틀렸어요: {e}"
    http = client or httpx.AsyncClient(timeout=20)
    try:
        token = await access_token(home, http)
        h = {"Authorization": f"Bearer {token}"}
        out = await {"calendar_events": _events, "gmail_search": _search, "gmail_read": _read}[name](http, h, a)
        return out[:MAX_RESULT]
    except Expired:
        return "Google 연결이 만료됐어요. 앱에서 'Google 다시 연결'을 눌러 주세요."
    except (httpx.HTTPError, KeyError, ValueError) as e:
        return f"지금은 Google을 볼 수 없어요. ({type(e).__name__})"
    finally:
        if client is None:
            await http.aclose()


async def _events(http, h, a):
    tmin = datetime.combine(a["from"], datetime.min.time(), KST).isoformat()
    tmax = datetime.combine(a["to"] + timedelta(days=1), datetime.min.time(), KST).isoformat()
    cals = (await http.get(f"{CAL}/users/me/calendarList", headers=h, params={"minAccessRole": "reader"})).json()
    rows = []
    for cal in [c for c in cals.get("items", []) if c.get("selected", c.get("primary"))][:10]:
        r = await http.get(f"{CAL}/calendars/{cal['id']}/events", headers=h, params={
            "timeMin": tmin, "timeMax": tmax, "singleEvents": "true", "orderBy": "startTime", "maxResults": 50})
        if r.status_code != 200:
            continue
        for e in r.json().get("items", []):
            if e.get("status") == "cancelled":
                continue
            s, en = e.get("start", {}), e.get("end", {})
            if "dateTime" in s:
                st, et = datetime.fromisoformat(s["dateTime"]).astimezone(KST), datetime.fromisoformat(en["dateTime"]).astimezone(KST)
                when = f"{st:%m/%d(%a) %H:%M}~{et:%H:%M}"
            else:
                when = f"{s.get('date', '')} 종일"
            where = f" @ {e['location'][:40]}" if e.get("location") else ""
            rows.append((s.get("dateTime") or s.get("date") or "", f"- {when} {e.get('summary', '(제목 없음)')[:80]}{where}"))
    rows.sort()
    head = f"캘린더 {a['from']}~{a['to']} (KST)"
    return head + ("\n" + "\n".join(r for _, r in rows) if rows else "\n일정 없음")


async def _search(http, h, a):
    ids = (await http.get(f"{GMAIL}/messages", headers=h, params={"q": a["query"], "maxResults": a["max"]})).json()
    out = []
    for m in ids.get("messages", [])[:a["max"]]:
        d = (await http.get(f"{GMAIL}/messages/{m['id']}", headers=h, params={
            "format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})).json()
        hd = {x["name"].lower(): x["value"] for x in d.get("payload", {}).get("headers", [])}
        out.append(f"- id={m['id']} | {hd.get('date', '')[:31]} | {hd.get('from', '')[:60]} | {hd.get('subject', '')[:100]}"
                   f" | {d.get('snippet', '')[:120]}")
    return f"메일 검색 '{a['query']}'\n" + ("\n".join(out) if out else "결과 없음")


def _plain(part):
    """The text/plain parts of a payload, decoded; attachments only counted."""
    texts, files = [], 0
    stack = [part]
    while stack:
        p = stack.pop()
        if p.get("filename"):
            files += 1
            continue
        stack += p.get("parts") or []
        if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data"):
            texts.append(base64.urlsafe_b64decode(p["body"]["data"] + "==").decode("utf-8", "replace"))
    return "\n".join(texts), files


async def _read(http, h, a):
    d = (await http.get(f"{GMAIL}/messages/{a['id']}", headers=h, params={"format": "full"})).json()
    hd = {x["name"].lower(): x["value"] for x in d.get("payload", {}).get("headers", [])}
    text, files = _plain(d.get("payload", {}))
    text = re.sub(r"\n{3,}", "\n\n", text or d.get("snippet", "")).strip()[:BODY_CHARS]
    att = f"\n(첨부 {files}개, 내용은 열지 않음)" if files else ""
    return (f"보낸 사람: {hd.get('from', '')[:80]}\n받는 사람: {hd.get('to', '')[:80]}\n제목: {hd.get('subject', '')[:150]}\n"
            f"날짜: {hd.get('date', '')[:40]}\n\n{text}{att}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "consent":
        scope_set = sys.argv[sys.argv.index("--scopes") + 1] if "--scopes" in sys.argv else "read"
        if scope_set not in SCOPE_SETS:
            raise SystemExit(f"--scopes: one of {sorted(SCOPE_SETS)}")
        home = Path(os.environ.get("BISEO_HOME", Path.home() / ".config/biseo"))
        granted = consent(home, scope_set)
        print("Google 연결됨:", ", ".join(s.rsplit("/", 1)[-1] for s in granted))
    elif len(sys.argv) >= 2 and sys.argv[1] == "status":
        print(status(Path(os.environ.get("BISEO_HOME", Path.home() / ".config/biseo"))))
    else:
        raise SystemExit("usage: google_tools.py consent [--scopes read] | status")
