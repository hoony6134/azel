"""Azel gateway (the assistant's spoken name is the `assistant_name` setting): Claude access, settings, usage, devices, approvals, calls, push, scheduler.

One gateway runs on every device. Shared state lives behind the memory service's state API on ruchba
(PROTOCOL.md section 5), never in a local database, so any gateway can answer for the others.
  mode "full"    (jh-only devices): Claude via `claude -p` with the user's login and claude.ai connectors.
  mode "limited" (team devices):    Messages API with a separate API key, tools = this device's daemon only,
                                    personal-data tasks refused. No scheduler, no calls.

Contract: ../PROTOCOL.md section 3. Run: uv run gateway.py  (binds the Tailscale IP only)
           uv run gateway.py --migrate  (copy the old local gateway.db into ruchba; also runs once on first start)
"""
import asyncio
import copy
import hmac
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

HOME = Path(os.environ.get("BISEO_HOME", Path.home() / ".config/biseo"))
TOKEN = (HOME / "token").read_text().strip()
SETTINGS_PATH = HOME / "settings.json"  # offline copy of the shared settings
APNS_PATH = HOME / "apns.json"  # {"key_path","key_id","team_id","bundle_id"}
API_KEY_PATH = HOME / "anthropic_api_key"
HOST = os.environ.get("BISEO_HOST", "100.64.0.8")
PORT = int(os.environ.get("BISEO_PORT", "8892"))
MODE = os.environ.get("BISEO_MODE", "full")
DEVICE = os.environ.get("BISEO_DEVICE", "macbook")  # ASCII slug, unique per gateway
PRIORITY = int(os.environ.get("BISEO_PRIORITY", "10"))  # lower = preferred by the voice server
INSTANCE = f"{DEVICE}-{uuid.uuid4().hex[:6]}"  # lease holder: a restarted process is a new holder
STATE_URL = os.environ.get("BISEO_STATE_URL", "http://100.64.0.10:8894")
VOICE_URL = os.environ.get("BISEO_VOICE_URL", "http://100.64.0.11:8891")
TTS_URL = os.environ.get("BISEO_TTS_URL", "http://100.64.0.11:8890")
CLAUDE = os.environ.get("BISEO_CLAUDE", "claude")
WORKDIR = HOME / "claude-work"  # empty cwd so no project CLAUDE.md leaks into answers
KST = timezone(timedelta(hours=9))
DEVICE_FRESH_S = 150
MAX_TOOL_TURNS = 8
assert MODE in ("full", "limited"), "BISEO_MODE must be full or limited"

MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
PRICES = {"haiku": (0.10, 0.50), "sonnet": (2.0, 10.0), "opus": (4.0, 20.0)}  # USD per MTok in/out
DEFAULTS = {
    "default_model": "auto", "claude_path": "auto",
    "task_defaults": {"chat": {"model": "qwen"}, "schedule": {"model": "sonnet"},
                      "code": {"model": "opus", "path": "claude_code"}, "mac": {"model": "sonnet"}},
    "monthly_budget_usd": 500, "budget_warn_pct": 80,
    "proactive": {"morning_briefing": "08:00", "deadline_check": "20:00", "quiet_hours": ["23:30", "07:30"],
                  "triggers": {"deadline": True, "server_down": True}},
    "default_device": "macbook", "device_fallback": ["albireo", "spark"],  # device ids (display names also match)
    "assistant_name": "아젤",
    "history_days": 365,  # call history kept this long; 0 = forever
}
NAME_RE = re.compile(r"[가-힣A-Za-z ]{1,12}")


def name_prompt(settings):
    return f"너의 이름은 '{settings['assistant_name']}'이야. "
# Personal data only full gateways may touch. Same list as the voice server's PERSONAL.
PERSONAL = re.compile(r"일정(?!한)|캘린더|스케줄|회의|약속|메일|편지함|기억|메모리|연락처|전화번호|과제|수업|시험|성적|마감"
                      r"|할 ?일|브리핑|지스트라|GISTRA|플래닛|Planit|지난번|저번에|예전에 (내가|우리)", re.I)
PERSONAL_REFUSAL = "개인 비서 기능은 지금 쓸 수 없어요."
VOICE_BASE = (
    "지금은 음성 통화 중이고 답은 그대로 소리로 읽혀. 한국어 존댓말로 두세 문장 안에 요점만 말해. "
    "마크다운, 목록, 표, 이모지, URL, 코드 블록을 쓰지 마. '확인하지 않은 것' 같은 꼬리말도 붙이지 마. "
    "커밋이나 파일에 AI 작성자 표시를 절대 남기지 마. "
)
VOICE_STYLE = VOICE_BASE + (
    "사용자 데이터는 Cygnus MCP(메모리, GISTRA, Planit, 인프라), 캘린더, 메일 도구로 확인해. "
    "MacBook이나 서버에서 할 일은 등록된 기기 MCP 도구로 해. 위험한 명령이 승인 대기면 앱에서 승인해 달라고 말해."
)
API_STYLE = VOICE_BASE + (
    "너는 일정, 메일, 메모리, GISTRA, Planit 같은 개인 데이터를 볼 수 없어. 그런 요청이면 "
    f"정확히 '{PERSONAL_REFUSAL}'라고만 말해. 기기 작업은 주어진 기기 도구로만 해. "
    "위험한 명령이 승인 대기면 앱에서 승인해 달라고 말해."
)


def read_api_key():
    """The limited-mode key lives in a 0600 file; refuse anything readable by others."""
    if API_KEY_PATH.exists():
        assert API_KEY_PATH.stat().st_mode & 0o077 == 0, f"{API_KEY_PATH} must be chmod 600"
        return API_KEY_PATH.read_text().strip()
    return os.environ.get("ANTHROPIC_API_KEY")


API_KEY = read_api_key()
assert MODE == "full" or API_KEY, f"limited mode needs {API_KEY_PATH}"


def secret(name, fallback=None):
    """A 0600 token file under HOME; `fallback` when it does not exist."""
    path = HOME / name
    if not path.exists():
        return fallback
    assert path.stat().st_mode & 0o077 == 0, f"{path} must be chmod 600"
    return path.read_text().strip()


# Tokens: `token` guards this gateway's API and its daemons' MCP endpoints (the shared token on jh devices,
# a device-local one on team devices). `link_token` dials the voice server (full devices: the link token;
# team devices: the link_limited one). The state API takes the shared token, or on team devices the
# scoped `state_token_limited`.
LINK_TOKEN = secret("link_token", TOKEN)  # shortcut: falls back to the shared token until link tokens are rolled out
STATE_TOKEN = TOKEN if MODE == "full" else secret("state_token_limited")
assert STATE_TOKEN, "limited mode needs state_token_limited"


# ---------- shared state (memory service on ruchba) ----------
class StateDown(Exception):
    """ruchba unreachable: callers fall back to the cache (read-only) or refuse with 503."""


class State:
    def __init__(self, url):
        self.client = httpx.AsyncClient(base_url=url, timeout=5, headers={"Authorization": f"Bearer {STATE_TOKEN}"})
        self.cache: dict = {}

    async def call(self, method, path, body=None, **params):
        """JSON result, None on 404, HTTPException on other 4xx, StateDown when unreachable."""
        try:
            r = await self.client.request(method, path, json=body, params=params or None)
        except httpx.HTTPError as e:
            raise StateDown(type(e).__name__) from e
        if r.status_code >= 500:
            raise StateDown(f"HTTP {r.status_code}")
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.json().get("detail", "state error"))
        return r.json()

    async def get(self, path, default=None, persist=None, **params):
        """GET with a last-good cache, so reads keep working while ruchba is down. `persist`: also keep the
        last good answer in that file under HOME, so it survives a restart during an outage."""
        key = (path, tuple(sorted(params.items())))
        try:
            self.cache[key] = await self.call("GET", path, **params)
            if persist:
                write_private(HOME / persist, json.dumps(self.cache[key], ensure_ascii=False))
        except StateDown as e:
            print("state down, using cache:", path, e, flush=True)
            if key not in self.cache and persist and (HOME / persist).exists():
                self.cache[key] = json.loads((HOME / persist).read_text())
        return self.cache.get(key, default)


def write_private(path, text):
    """Atomic 0600 write (concurrent writers each use their own temp file)."""
    tmp = path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text)
    tmp.chmod(0o600)
    tmp.replace(path)


state = State(STATE_URL)


class Outbox:
    """Writes to ruchba that must not be lost: queued in a 0600 file while ruchba is down and sent in order
    on the next put. A body ruchba refuses (4xx) goes to the `rejected` file instead of blocking the queue."""

    def __init__(self, name, rejected, route):
        self.file, self.rejected, self.route = HOME / name, HOME / rejected, route
        self.items: list[dict] = json.loads(self.file.read_text()) if self.file.exists() else []

    async def put(self, body):
        self.items.append(body)
        try:
            while self.items:
                try:
                    await state.call("POST", self.route(self.items[0]), self.items[0])
                except HTTPException as e:  # refused, not unreachable: retrying would block the queue forever
                    fd = os.open(self.rejected, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    with os.fdopen(fd, "a") as f:
                        f.write(json.dumps({"status": e.status_code, "row": self.items[0]}, ensure_ascii=False) + "\n")
                self.items.pop(0)
        except StateDown:
            pass  # kept in the file until ruchba is back
        if self.items or self.file.exists():
            write_private(self.file, json.dumps(self.items, ensure_ascii=False))


usage_outbox = Outbox("pending_usage.json", "usage_rejected.jsonl", lambda b: "/v1/state/usage")
pending_usage = usage_outbox.items  # usage rows not yet in ruchba (they still count against the budget)
HISTORY_PATHS = {"start": "start", "turn": "turns", "end": "end"}
history_outbox = Outbox("pending_history.json", "history_rejected.jsonl",
                        lambda b: f"/v1/state/history/{quote(b['log_id'], safe='')}/{HISTORY_PATHS[b['event']]}")
local_devices: dict[str, dict] = {}  # daemons that registered here, so this device works offline


async def load_settings():
    s = copy.deepcopy(DEFAULTS)
    try:
        doc = await state.call("GET", "/v1/state/settings") or {}
        local = json.loads(SETTINGS_PATH.read_text()) if SETTINGS_PATH.exists() else {}
        if not doc and local and MODE == "full":  # first start against an empty ruchba: seed it from old settings
            doc = await state.call("PUT", "/v1/state/settings", local)
        if local != doc:
            write_settings_copy(doc)
    except StateDown:
        doc = json.loads(SETTINGS_PATH.read_text()) if SETTINGS_PATH.exists() else {}
    s.update(doc)
    return s


def write_settings_copy(doc):
    write_private(SETTINGS_PATH, json.dumps(doc, ensure_ascii=False, indent=2))


async def save_settings(s):
    write_settings_copy(await state.call("PUT", "/v1/state/settings", s))  # StateDown -> 503 (read-only)


def month_start():
    return datetime.now(KST).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


async def month_rows():
    since = month_start()
    return await state.get("/v1/state/usage", [], since=since) + [u for u in pending_usage if u["ts"] >= since]


async def month_cost():
    return sum(r["cost_usd"] for r in await month_rows())


async def record_usage(row):
    await usage_outbox.put({"ts": time.time(), "device": DEVICE, **row})


async def record_call_log(m):
    """A call_log event from the voice server (start / turn / end) into the shared call history."""
    if MODE != "full" or m.get("event") not in HISTORY_PATHS or not isinstance(m.get("log_id"), str) or not m["log_id"]:
        return  # history is personal: limited gateways never store it
    if m["event"] == "turn" and not str(m.get("user", "")).strip():
        return  # nothing was said
    await history_outbox.put({k: v for k, v in m.items() if k != "type"})


async def push_tokens():
    return await state.get("/v1/state/push_tokens", [], persist="push_tokens.json")


async def take_lease(name, ttl_s):
    return (await state.call("POST", f"/v1/state/leases/{quote(name, safe='')}",
                             {"holder": INSTANCE, "ttl_s": ttl_s}))["held"]


def auth(request: Request):
    got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(got.encode(), TOKEN.encode()):
        raise HTTPException(401, "bad token")


# ---------- Claude ----------
def trusted(d):
    """May this gateway send its token to the device's url and feed its output to Claude? Full gateways trust
    only rows written by full gateways (a limited writer could point a row at any url); a limited gateway
    only its own daemon."""
    return d.get("writer") == "full" if MODE == "full" else d.get("gateway") == DEVICE


async def fresh_devices():
    rows = await state.get("/v1/state/devices")
    if rows is None:  # ruchba down and nothing cached yet
        rows = list(local_devices.values())
    return [d for d in rows if d["last_seen"] > time.time() - DEVICE_FRESH_S and trusted(d)]


def resolve(settings, model, path, task):
    td = settings["task_defaults"].get(task or "", {})
    model = model or td.get("model") or settings["default_model"]
    if model in ("auto", "qwen"):  # the voice server only calls us when Claude is wanted
        model = "sonnet"
    path = path or td.get("path") or settings["claude_path"]
    if path == "auto":
        path = "api" if API_KEY else "claude_code"
    if MODE == "limited":
        path = "api"  # never `claude -p` (no OAuth login, no claude.ai connectors) on a team machine
    if model not in MODELS:
        raise HTTPException(400, f"model must be one of {sorted(MODELS)} or auto")
    return model, path


def device_prompt(settings, devices):
    """Tell Claude which device to use by default and in what order to fall back."""
    if not devices:
        return " 지금 켜져 있는 작업 기기가 없어. 기기 작업은 할 수 없다고 말해."
    # Devices are spoken of by their English id; settings may name them by id or (older) display name.
    alias = {d["name"]: d["id"] for d in devices} | {d["id"]: d["id"] for d in devices}
    order = list(dict.fromkeys(alias[n] for n in [settings["default_device"], *settings["device_fallback"]]
                               if n in alias))
    order += [d["id"] for d in devices if d["id"] not in order]
    names = ", ".join(f"{i}(mcp__{i})" for i in order)
    first = settings["default_device"]
    note = "" if order[0] == alias.get(first) else f" 기본 기기 {first}는 꺼져 있어. {order[0]}에서 했다고 꼭 말해."
    return (f" 켜져 있는 기기, 우선순위 순: {names}. 사용자가 기기를 말하지 않으면 맨 앞 기기를 써.{note}")


# (prefix of the tool's own name, label, kind); the first match wins. `kind` picks the app's icon.
TOOL_LABELS = [("gistra_", "학사 일정 확인 중", "schedule"), ("planit_", "할 일 확인 중", "schedule"),
               ("memory_", "기억 찾는 중", "memory"), ("infra_", "서버 상태 보는 중", "server")]
SERVER_LABELS = [("google_calendar", "캘린더 확인 중", "calendar"), ("gmail", "메일 확인 중", "mail"),
                 ("notion", "노션 보는 중", "notes")]
DEVICE_LABELS = {"run_command": ("{}에서 명령 실행 중", "terminal"), "list_dir": ("{} 파일 보는 중", "files"),
                 "read_file": ("{} 파일 보는 중", "files"), "search": ("{} 파일 보는 중", "files"),
                 "browse": ("웹페이지 읽는 중", "web"), "delegate": ("{}에 작업 맡기는 중", "terminal")}
GENERIC = ("도구 준비 중", "generic")


def tool_info(name):
    """(Korean label, kind) for a tool, from its name alone (never its arguments or result)."""
    server, _, tool = name.removeprefix("mcp__").partition("__") if name.startswith("mcp__") else ("", "", name)
    for prefix, label, kind in TOOL_LABELS:
        if tool.startswith(prefix):
            return label, kind
    for key, label, kind in SERVER_LABELS:
        if key in server.lower() or tool.lower().startswith(key):
            return label, kind
    if server and not server.startswith("claude_ai_") and tool in DEVICE_LABELS:
        label, kind = DEVICE_LABELS[tool]
        return label.format(server), kind  # a device daemon: the server name is the device id
    return GENERIC


def tool_label(name):
    return tool_info(name)[0]


def activity(phase, tool=""):
    """Progress event for the app. Only the tool's name, label and kind: arguments and results stay private."""
    label, kind = ("생각 중", "generic") if phase == "thinking" else tool_info(tool)
    return {"type": "activity", "phase": phase, "tool": tool, "label": label, "kind": kind, "route": "claude"}


async def ask_claude_code(text, model, session):
    """Yield text deltas, then a final dict with usage, from `claude -p` stream-json."""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    devices = await fresh_devices()
    settings = await load_settings()
    style = name_prompt(settings) + VOICE_STYLE + device_prompt(settings, devices)
    cmd = [CLAUDE, "-p", text, "--model", MODELS[model], "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--append-system-prompt", style,
           "--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit",
           "--allowedTools", "mcp__claude_ai_Cygnus_MCP", "mcp__claude_ai_Google_Calendar",
           "mcp__claude_ai_Gmail", *[f"mcp__{d['id']}" for d in devices]]
    prev = await state.get(f"/v1/state/sessions/{quote(session, safe='')}")
    if prev and prev["device"] == DEVICE:  # `claude -p` sessions live on the host that made them
        cmd += ["--resume", prev["claude_session"]]
    cfg = None
    if devices:
        fd, cfg = tempfile.mkstemp(suffix=".json")  # holds the token: 0600, deleted after the call
        with os.fdopen(fd, "w") as f:
            json.dump({"mcpServers": {d["id"]: {"type": "http", "url": d["url"],
                                                "headers": {"Authorization": f"Bearer {TOKEN}"}}
                                      for d in devices}}, f)
        cmd += ["--mcp-config", cfg]
    env = os.environ | {"MCP_CONNECTION_NONBLOCKING": "false"}  # wait for claude.ai connectors
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=WORKDIR, env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL, limit=2**24)
    final = {"model": model, "path": "claude_code"}
    open_tools: dict[str, str] = {}  # tool_use id -> name, until its result comes back
    try:
        async for raw in proc.stdout:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if e.get("type") == "stream_event":
                ev = e["event"]
                if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                    yield ev["delta"]["text"]
                elif ev.get("type") == "message_stop":
                    yield "\n"  # turns between tool calls are separate utterances
                elif ev.get("type") == "content_block_start" and ev["content_block"].get("type") == "tool_use":
                    block = ev["content_block"]
                    open_tools[block.get("id", "")] = block.get("name", "")
                    yield activity("tool_start", block.get("name", ""))
            elif e.get("type") == "user":  # tool results come back as a user message
                content = e.get("message", {}).get("content")
                for block in content if isinstance(content, list) else []:
                    if block.get("type") == "tool_result" and block.get("tool_use_id") in open_tools:
                        yield activity("tool_end", open_tools.pop(block["tool_use_id"]))
            elif e.get("type") == "result":
                u = e.get("usage", {})
                final.update(cost_usd=e.get("total_cost_usd") or 0, input_tokens=u.get("input_tokens", 0),
                             output_tokens=u.get("output_tokens", 0), error=e.get("is_error", False))
                if e.get("session_id"):
                    try:
                        await state.call("PUT", f"/v1/state/sessions/{quote(session, safe='')}",
                                         {"claude_session": e["session_id"], "device": DEVICE})
                    except StateDown:
                        pass  # the next turn just starts a fresh Claude session
        await proc.wait()
    finally:
        if proc.returncode is None:
            proc.kill()
        if cfg:
            os.unlink(cfg)
    yield final


async def mcp(url, method, params):
    """One JSON-RPC call to a daemon's stateless streamable-HTTP MCP endpoint (JSON or SSE answer)."""
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                         headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"})
    r.raise_for_status()
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        msgs = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
        msg = next(m for m in msgs if m.get("id") == 1)
    else:
        msg = r.json()
    if "error" in msg:
        raise RuntimeError(msg["error"].get("message", "MCP error"))
    return msg["result"]


async def device_tools(devices):
    """Messages API tool definitions for the daemons' MCP tools, plus where each one runs."""
    tools, where = [], {}
    for d in devices:
        try:
            listed = (await mcp(d["url"], "tools/list", {}))["tools"]
        except Exception as e:
            print("tools/list failed:", d["id"], type(e).__name__, flush=True)
            continue
        for t in listed:
            name = f"{d['id']}__{t['name']}"[:64]
            where[name] = (d["url"], t["name"])
            tools.append({"name": name, "description": f"[{d['name']}] {t.get('description') or ''}"[:1024],
                          "input_schema": t.get("inputSchema") or {"type": "object"}})
    return tools, where


async def run_tool(block, where):
    try:
        url, name = where[block["name"]]
        res = await mcp(url, "tools/call", {"name": name, "arguments": block["input"]})
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        return {"type": "tool_result", "tool_use_id": block["id"], "content": text[:20000] or "(empty)",
                "is_error": bool(res.get("isError"))}
    except Exception as e:
        return {"type": "tool_result", "tool_use_id": block["id"], "content": f"tool failed: {e}", "is_error": True}


async def anthropic(body):
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post("https://api.anthropic.com/v1/messages", json=body,
                         headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01"})
    r.raise_for_status()
    return r.json()


async def ask_api(text, model, session):
    """Messages API with the devices' MCP tools, one non-streamed request per tool turn.

    shortcut: no conversation memory on this path (the voice server sends recent turns in `text`), and
    each turn's text arrives whole; stream it if limited-mode latency matters.
    """
    devices = await fresh_devices()
    tools, where = await device_tools(devices)
    settings = await load_settings()
    system = name_prompt(settings) + API_STYLE + device_prompt(settings, devices)
    messages = [{"role": "user", "content": text}]
    final = {"model": model, "path": "api", "input_tokens": 0, "output_tokens": 0}
    yield activity("thinking")
    for _ in range(MAX_TOOL_TURNS):
        body = {"model": MODELS[model], "max_tokens": 4096, "system": system, "messages": messages,
                "output_config": {"effort": "low"}, **({"tools": tools} if tools else {})}
        m = await anthropic(body)
        final["input_tokens"] += m["usage"]["input_tokens"]
        final["output_tokens"] += m["usage"]["output_tokens"]
        for b in m["content"]:
            if b["type"] == "text" and b["text"].strip():
                yield b["text"] + "\n"
        if m["stop_reason"] == "refusal":
            yield "그 요청은 도와드릴 수 없어요."
        if m["stop_reason"] != "tool_use":
            break
        messages.append({"role": "assistant", "content": m["content"]})  # unchanged, thinking blocks included
        calls = [b for b in m["content"] if b["type"] == "tool_use"]
        messages.append({"role": "user", "content": list(await asyncio.gather(*[run_tool(b, where) for b in calls]))})
    pin, pout = PRICES[model]
    final["cost_usd"] = (final["input_tokens"] * pin + final["output_tokens"] * pout) / 1e6
    yield final


async def ask_stream(text, session, model=None, path=None, task=None):
    # The voice server prefixes recent turns ("최근 통화 내용: ..."), so judge only the last paragraph.
    if MODE == "limited" and (task == "schedule" or PERSONAL.search(text.rsplit("\n\n", 1)[-1])):
        yield {"type": "error", "code": "personal", "message": PERSONAL_REFUSAL}
        return
    settings = await load_settings()
    budget = settings["monthly_budget_usd"]
    if await month_cost() >= budget:
        yield {"type": "error", "code": "budget", "message": "이번 달 Claude 한도를 다 썼습니다."}
        return
    model, path = resolve(settings, model, path, task)
    gen = ask_claude_code if path == "claude_code" else ask_api
    async for item in gen(text, model, session):
        if isinstance(item, str):
            yield {"type": "text", "delta": item}
        elif item.get("type") == "activity":
            yield item
        else:
            await record_usage({"model": model, "path": path, "task": task or "",
                                "input_tokens": item.get("input_tokens", 0),
                                "output_tokens": item.get("output_tokens", 0), "cost_usd": item.get("cost_usd", 0)})
            spent = await month_cost()
            yield {"type": "done", **item, "budget_warning": spent >= budget * settings["budget_warn_pct"] / 100}


async def ask_text(text, session, **kw):
    out = []
    async for ev in ask_stream(text, session, **kw):
        if ev["type"] == "text":
            out.append(ev["delta"])
    return "".join(out).strip()


# ---------- push / calls ----------
_apns_jwt = {"token": None, "ts": 0}


def apns_config():
    return json.loads(APNS_PATH.read_text()) if APNS_PATH.exists() else None


async def send_voip_push(payload):
    cfg = apns_config()
    tokens = await push_tokens()
    if not cfg or not tokens:
        return {"sent": 0, "reason": "APNs key or device token not configured"}
    if time.time() - _apns_jwt["ts"] > 50 * 60:  # APNs wants a fresh JWT at most hourly
        key = Path(cfg["key_path"]).expanduser().read_text()
        _apns_jwt.update(ts=time.time(), token=jwt.encode(
            {"iss": cfg["team_id"], "iat": int(time.time())}, key, algorithm="ES256",
            headers={"kid": cfg["key_id"]}))
    sent = 0
    async with httpx.AsyncClient(http2=True, timeout=10) as c:
        for t in tokens:
            host = "api.sandbox.push.apple.com" if t["env"] == "sandbox" else "api.push.apple.com"
            r = await c.post(f"https://{host}/3/device/{t['token']}", json=payload, headers={
                "authorization": f"bearer {_apns_jwt['token']}", "apns-topic": f"{t['bundle_id']}.voip",
                "apns-push-type": "voip", "apns-priority": "10", "apns-expiration": "0"})
            sent += r.status_code == 200
    return {"sent": sent}


def in_quiet_hours(settings, now=None):
    now = (now or datetime.now(KST)).strftime("%H:%M")
    start, end = settings["proactive"]["quiet_hours"]
    return (start <= now or now < end) if start > end else (start <= now < end)


async def place_call(reason, opening=None):
    if MODE == "limited":
        raise HTTPException(403, "limited gateways do not place calls")
    if opening is None and reason == "morning_briefing":
        opening = await ask_text(
            "아침 브리핑 전화를 걸었어. 오늘 수업과 마감(GISTRA), 오늘 할 일(Planit), 캘린더 일정, "
            "밤사이 서버 상태(Cygnus 인프라)를 확인해서 '좋은 아침이에요'로 시작하는 30초 안쪽 브리핑으로 말해.",
            session=f"briefing-{uuid.uuid4().hex[:8]}", model="sonnet", task="schedule")
    call_id = uuid.uuid4().hex
    try:
        await state.call("POST", "/v1/state/calls", {"id": call_id, "reason": reason, "opening": opening or ""})
    except StateDown:
        pass  # the opening still reaches the voice server over the link, which is what the call needs
    # The opening must reach the voice server before the phone can answer and connect.
    await link_send({"type": "call", "call_id": call_id, "opening": opening or "", "reason": reason})
    caller = (await load_settings())["assistant_name"]
    push = await send_voip_push({"call_id": call_id, "reason": reason, "caller": caller})
    return {"call_id": call_id, **push}


DEADLINE_PROMPT = (
    "GISTRA 과제·시험 마감과 Planit 할 일 중 지금부터 24시간 안에 끝나는데 아직 안 끝난 것을 확인해. "
    f"하나도 없으면 정확히 {'NONE'} 한 단어만 답해. 있으면 '마감이 다가오는 게 있어요'로 시작해서 "
    "무엇이 언제까지인지 두세 문장으로 말해."
)


async def deadline_call():
    text = await ask_text(DEADLINE_PROMPT, session=f"deadline-{uuid.uuid4().hex[:8]}", model="sonnet",
                          task="schedule")
    if text and text.strip().upper() != "NONE" and "NONE" not in text[:10]:
        return await place_call("deadline", text)
    return {"called": False}


async def scheduler_tick(now, fired):
    """Fire each due job once across all gateways: whoever takes the job's per-day lease runs it."""
    s = await load_settings()
    hm, today = now.strftime("%H:%M"), now.date()
    if hm == RETENTION_AT and fired.get("retention") != today and s.get("history_days", 0) > 0:
        fired["retention"] = today
        try:
            if await take_lease(f"job:retention:{today}", 86400):
                await state.call("DELETE", "/v1/state/history", older_than_days=s["history_days"])
        except StateDown:
            pass  # tomorrow, then
    jobs = {"morning_briefing": lambda: place_call("morning_briefing")}
    if s["proactive"]["triggers"].get("deadline"):
        jobs["deadline_check"] = deadline_call
    for key, job in jobs.items():
        if hm == s["proactive"].get(key) and fired.get(key) != today:
            fired[key] = today
            if in_quiet_hours(s, now):
                continue
            try:
                won = await take_lease(f"job:{key}:{today}", 86400)
            except StateDown:
                won = False  # a missed briefing beats several phones ringing at once
            if won:
                await job()


RETENTION_AT = "04:00"  # call history older than history_days is deleted daily (quiet hours do not matter)


async def scheduler():
    fired: dict = {}
    while True:
        try:
            await scheduler_tick(datetime.now(KST), fired)
        except Exception as e:  # never let the loop die
            print("scheduler error:", e, flush=True)
        await asyncio.sleep(60 - datetime.now().second)


# ---------- link to the voice server ----------
# The Spark may not open connections to jh hosts (Headscale ACL) and must not hold personal
# Claude credentials, so every gateway dials the voice server and serves its asks over one socket.
VOICE_LINK = VOICE_URL.replace("http", "ws", 1) + "/v1/gateway-link"
link: dict = {"ws": None, "lock": asyncio.Lock()}


async def link_send(obj):
    ws = link["ws"]
    if ws is None:
        return False
    async with link["lock"]:
        await ws.send(json.dumps(obj, ensure_ascii=False))
    return True


async def serve_ask(m):
    try:
        async for ev in ask_stream(m["text"], m.get("session") or "default", m.get("model"), None, m.get("task")):
            await link_send({"id": m["id"], **ev})
    except asyncio.CancelledError:
        raise  # barge-in: ask_claude_code's finally kills the claude process
    except Exception as e:
        print("ask failed:", type(e).__name__, e, flush=True)
        await link_send({"id": m["id"], "type": "error", "message": "Claude 요청이 실패했어요."})


async def handle_link(ws):
    tasks: dict[str, asyncio.Task] = {}
    async for raw in ws:
        m = json.loads(raw)
        if m.get("type") == "ask":
            tasks[m["id"]] = asyncio.create_task(serve_ask(m))
        elif m.get("type") == "cancel" and (t := tasks.pop(m.get("id"), None)):
            t.cancel()
        elif m.get("type") == "call_log":
            await record_call_log(m)  # in order, so a call's turns follow its start
        for k in [k for k, t in tasks.items() if t.done()]:
            tasks.pop(k)
    for t in tasks.values():
        t.cancel()


async def voice_link():
    import websockets
    backoff = 1
    while True:
        try:
            async with websockets.connect(VOICE_LINK, additional_headers={"Authorization": f"Bearer {LINK_TOKEN}"},
                                          max_size=None, ping_interval=20, open_timeout=10) as ws:
                link["ws"], backoff = ws, 1
                await link_send({"type": "hello", "device": DEVICE, "mode": MODE, "priority": PRIORITY})
                await link_send({"type": "settings", "value": await load_settings()})
                print("voice link up", flush=True)
                await handle_link(ws)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("voice link down:", type(e).__name__, e, flush=True)
        finally:
            link["ws"] = None
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


# ---------- HTTP ----------
@asynccontextmanager
async def lifespan(app):
    HOME.mkdir(parents=True, exist_ok=True)
    tasks = [asyncio.create_task(voice_link())]
    if MODE == "full":  # scheduled jobs need personal data
        tasks.append(asyncio.create_task(scheduler()))
        if OLD_DB.exists() and not MIGRATED.exists():
            tasks.append(asyncio.create_task(migrate_once()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="biseo-gateway", lifespan=lifespan, dependencies=[Depends(auth)])


@app.exception_handler(StateDown)
async def state_down(request, exc):
    return JSONResponse({"detail": "공유 상태 서버(ruchba)에 연결할 수 없어 지금은 읽기만 돼요."}, status_code=503)


@app.post("/v1/ask")
async def ask(body: dict):
    if not body.get("text"):
        raise HTTPException(400, "text required")

    async def gen():
        async for ev in ask_stream(body["text"], body.get("session", "default"), body.get("model"),
                                   body.get("path"), body.get("task")):
            yield json.dumps(ev, ensure_ascii=False) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/v1/settings")
async def get_settings():
    return await load_settings()


@app.put("/v1/settings")
async def put_settings(body: dict):
    unknown = set(body) - set(DEFAULTS)
    if unknown:
        raise HTTPException(400, f"unknown keys: {sorted(unknown)}")
    name = body.get("assistant_name")
    if name is not None and not (isinstance(name, str) and NAME_RE.fullmatch(name) and name.strip()):
        raise HTTPException(400, "assistant_name: 1-12 Hangul/Latin letters or spaces")
    if name is not None:
        body["assistant_name"] = " ".join(name.split())
    days = body.get("history_days")
    if days is not None and (isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 36500):
        raise HTTPException(400, "history_days: a whole number of days, 0 = keep forever")
    s = await load_settings() | body
    await save_settings(s)
    await link_send({"type": "settings", "value": s})
    return s


@app.get("/v1/usage")
async def usage():
    rows = await month_rows()
    by = lambda k: {v: round(sum(r["cost_usd"] for r in rows if r[k] == v), 4) for v in {r[k] for r in rows}}  # noqa: E731
    return {"month": datetime.now(KST).strftime("%Y-%m"), "calls": len(rows), "by_model": by("model"),
            "by_path": by("path"), "by_device": by("device"), "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
            "budget_usd": (await load_settings())["monthly_budget_usd"]}


async def probe(url, token=False):
    """Reachability only. The token goes along only when asked (a trusted daemon); health routes need none."""
    t = time.perf_counter()
    headers = {"Accept": "text/event-stream"} | ({"Authorization": f"Bearer {TOKEN}"} if token else {})
    try:
        # Headers only: an MCP endpoint may answer GET with a stream that never ends.
        async with httpx.AsyncClient(timeout=3) as c, c.stream("GET", url, headers=headers) as r:
            ok = r.status_code < 500
        return {"ok": ok, "ms": round((time.perf_counter() - t) * 1000)}
    except httpx.HTTPError:
        return {"ok": False, "ms": None}


@app.get("/v1/status")
async def status():
    devs = await state.get("/v1/state/devices", list(local_devices.values()))
    probes = await asyncio.gather(probe(f"{VOICE_URL}/health"), probe(f"{TTS_URL}/health"),
                                  probe(f"{STATE_URL}/health"), *[probe(d["url"], trusted(d)) for d in devs])
    return {"spark": probes[0], "tts": probes[1], "state": probes[2],
            "gateway": {"ok": True, "ms": 0, "device": DEVICE, "mode": MODE, "link": link["ws"] is not None},
            "devices": [{"id": d["id"], "name": d["name"], **p,
                         "fresh": time.time() - d["last_seen"] < DEVICE_FRESH_S} for d, p in zip(devs, probes[3:])]}


@app.post("/v1/devices/register")
async def register(body: dict):
    for k in ("id", "name", "url"):
        if not body.get(k):
            raise HTTPException(400, f"{k} required")
    if not body["id"].replace("_", "").replace("-", "").isalnum() or not body["id"].isascii():
        raise HTTPException(400, "id must be an ASCII slug")
    row = {"name": body["name"], "url": body["url"], "harness": body.get("harness", []), "gateway": DEVICE}
    local_devices[body["id"]] = {"id": body["id"], **row, "last_seen": time.time(), "writer": MODE}
    try:
        await state.call("PUT", f"/v1/state/devices/{body['id']}", row)
    except StateDown:
        pass  # still usable from this gateway via local_devices
    return {"ok": True}


@app.get("/v1/devices")
async def devices():
    return await state.get("/v1/state/devices", list(local_devices.values()))


def full_only():
    if MODE != "full":
        raise HTTPException(403, "call history is not available on a limited gateway")


@app.get("/v1/history")
async def history(limit: int = 20, before: float | None = None, q: str | None = None):
    full_only()
    params = {"limit": limit} | ({"before": before} if before is not None else {}) | ({"q": q} if q else {})
    return await state.call("GET", "/v1/state/history", **params)


@app.get("/v1/history/{hid}")
async def history_item(hid: str):
    full_only()
    row = await state.call("GET", f"/v1/state/history/{quote(hid, safe='')}")
    if row is None:
        raise HTTPException(404, "no such call")
    return row


@app.delete("/v1/history/{hid}")
async def history_delete(hid: str):
    full_only()
    row = await state.call("DELETE", f"/v1/state/history/{quote(hid, safe='')}")
    if row is None:
        raise HTTPException(404, "no such call")
    return row


@app.delete("/v1/history")
async def history_prune(older_than_days: float):
    full_only()
    if older_than_days <= 0:
        raise HTTPException(400, "older_than_days must be positive")
    return await state.call("DELETE", "/v1/state/history", older_than_days=older_than_days)


@app.post("/v1/push/register")
async def push_register(body: dict):
    if not body.get("voip_token") or not body.get("bundle_id"):
        raise HTTPException(400, "voip_token and bundle_id required")
    await state.call("PUT", f"/v1/state/push_tokens/{quote(body['voip_token'], safe='')}",
                     {"bundle_id": body["bundle_id"], "env": body.get("env", "sandbox")})
    await push_tokens()  # refresh the restart-proof copy
    return {"ok": True}


@app.post("/v1/calls")
async def calls(body: dict):
    return await place_call(body.get("reason", "manual"), body.get("opening"))


@app.get("/v1/calls/{call_id}")
async def get_call(call_id: str):
    row = await state.call("POST", f"/v1/state/calls/{quote(call_id, safe='')}/answer")
    if row is None:
        raise HTTPException(404, "no such call")
    return row


@app.post("/v1/alerts")
async def alerts(body: dict):
    s = await load_settings()
    firing = [a for a in body.get("alerts", []) if a.get("status") == "firing"
              and a.get("labels", {}).get("severity") == "critical"]
    if not firing or not s["proactive"]["triggers"].get("server_down") or in_quiet_hours(s):
        return {"called": False}
    names = ", ".join(sorted({a["labels"].get("alertname", "알 수 없는 경보") for a in firing}))
    try:  # Alertmanager may post the same alert to several gateways: one call per alert set per 10 min
        if not await take_lease(f"alert:{names}", 600):
            return {"called": False, "reason": "another gateway is calling"}
    except StateDown:
        pass  # ruchba itself may be what is down: call anyway
    return {"called": True, **await place_call("server_down", f"서버 경보가 왔어요. {names}. 로그를 볼까요, 재시작할까요?")}


@app.post("/v1/approvals")
async def create_approval(body: dict):
    return await state.call("POST", "/v1/state/approvals", {k: body.get(k, "") for k in ("device", "tool", "command", "cwd")})


@app.get("/v1/approvals")
async def list_approvals():
    return await state.call("GET", "/v1/state/approvals")


@app.get("/v1/approvals/{aid}")
async def get_approval(aid: str):
    row = await state.call("GET", f"/v1/state/approvals/{quote(aid, safe='')}")
    if row is None:
        raise HTTPException(404, "no such approval")
    return row


@app.post("/v1/approvals/{aid}")
async def decide(aid: str, body: dict):
    return await state.call("POST", f"/v1/state/approvals/{quote(aid, safe='')}", {"approve": bool(body.get("approve"))})


OLD_DB = HOME / "gateway.db"  # the per-host SQLite of the single-gateway era
MIGRATED = HOME / "migrated_from_sqlite"


async def migrate_sqlite():
    """Copy the old gateway.db into ruchba: settings (if ruchba has none), this month's usage, push tokens and
    pending approvals (same ids, so daemons waiting on them still match). Idempotent: usage rows carry an
    import key and approvals keep their id, so running it again adds nothing."""
    import sqlite3
    await load_settings()  # seeds ruchba from settings.json if ruchba has none
    con = sqlite3.connect(OLD_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("select rowid, * from usage where ts>=?", (month_start(),)).fetchall()
    for r in rows:
        row = {k: r[k] for k in r.keys() if k != "rowid"}
        await state.call("POST", "/v1/state/usage",
                         {**row, "device": DEVICE, "import_key": f"sqlite:{DEVICE}:{r['rowid']}"})
    toks = con.execute("select * from push_tokens").fetchall()
    for t in toks:
        await state.call("PUT", f"/v1/state/push_tokens/{quote(t['token'], safe='')}",
                         {"bundle_id": t["bundle_id"], "env": t["env"]})
    pend = con.execute("select * from approvals where status='pending'").fetchall()
    for a in pend:
        await state.call("POST", "/v1/state/approvals", {k: a[k] for k in ("id", "device", "tool", "command", "cwd")})
    con.close()
    write_private(MIGRATED, datetime.now(KST).isoformat())
    print(f"migrated {len(rows)} usage rows, {len(toks)} push tokens, {len(pend)} pending approvals", flush=True)


async def migrate_once():
    """On a full gateway's first start next to an old gateway.db; retried on later starts until it succeeds."""
    try:
        await migrate_sqlite()
    except Exception as e:  # ruchba down or refusing: the old file stays, the next start tries again
        print("migration from gateway.db failed, will retry on next start:", type(e).__name__, e, flush=True)


if __name__ == "__main__":
    import sys
    if "--migrate" in sys.argv:  # by hand; safe to repeat
        asyncio.run(migrate_sqlite())
        raise SystemExit
    import uvicorn
    assert HOST.startswith("100.") or HOST.startswith("127."), "bind a Tailscale or loopback address only"
    uvicorn.run(app, host=HOST, port=PORT)
