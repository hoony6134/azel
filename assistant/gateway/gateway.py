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
import hashlib
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
# A gateway without a Claude login or API key (e.g. an always-on deneb that only fires scheduled calls).
CLAUDE_OK = os.environ.get("BISEO_NO_CLAUDE", "") not in ("1", "true", "yes")
# Scheduled calls fire on one gateway: the scheduler lease goes to the lowest value among live gateways.
SCHED_PRIORITY = int(os.environ.get("BISEO_SCHEDULER_PRIORITY", os.environ.get("BISEO_PRIORITY", "10")))
STATE_URL = os.environ.get("BISEO_STATE_URL", "http://100.64.0.10:8894")
# The local Qwen on vd-spark decides autonomous calls (free, no Claude).
LLM_URL = os.environ.get("BISEO_LLM_URL", "http://100.64.0.11:8888/v1/chat/completions")
LLM_MODEL = os.environ.get("BISEO_LLM_MODEL", "Qwen3.8-Flash-Next")
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
    # "먼저 전화하기": proactive calls. User-created scheduled calls are not proactive and always fire.
    "proactive": {"enabled": True, "morning_briefing": {"enabled": True, "time": "08:00"},
                  "deadline_check": {"enabled": True, "time": "20:00"}, "server_alerts": True,
                  "autonomous": {"enabled": False, "max_per_day": 3, "min_gap_minutes": 120,
                                 "window": ["08:00", "23:00"], "checkpoints": ["08:30", "13:00", "18:00"]}},
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
    "macbook이나 서버에서 할 일은 등록된 기기 MCP 도구로 해. 위험한 명령이 승인 대기면 앱에서 승인해 달라고 말해."
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


HM_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
# Settings saved before devices were named in English; read as their ids.
OLD_DEVICE_NAMES = {"맥북": "macbook", "알비레오": "albireo", "기에나": "gienah", "데네브": "deneb", "스파크": "spark"}


def migrate_proactive(p):
    """Any stored `proactive` (incl. the old flat shape with quiet_hours/triggers) in today's shape, defaults
    filled in. Old: {"morning_briefing":"08:00","deadline_check":"20:00","quiet_hours":[…],
    "triggers":{"deadline":bool,"server_down":bool}}."""
    p, out = dict(p) if isinstance(p, dict) else {}, copy.deepcopy(DEFAULTS["proactive"])
    trig = p.pop("triggers", None) or {}
    p.pop("quiet_hours", None)  # removed: scheduled calls replace quiet hours
    for key, flag in (("morning_briefing", None), ("deadline_check", "deadline")):
        if isinstance(p.get(key), str):
            p[key] = {"time": p[key], "enabled": bool(trig.get(flag, True)) if flag else True}
    if "server_down" in trig and "server_alerts" not in p:
        p["server_alerts"] = bool(trig["server_down"])
    for k, v in p.items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k].update({kk: vv for kk, vv in v.items() if kk in out[k]})
        elif k in out and not isinstance(out[k], dict):
            out[k] = v
    return out


def check_proactive(p):
    """400 unless `p` (already migrated) has the right types and ranges."""
    a = p["autonomous"]
    ok = all(isinstance(x, bool) for x in (p["enabled"], p["server_alerts"], p["morning_briefing"]["enabled"],
                                            p["deadline_check"]["enabled"], a["enabled"]))
    times = [p["morning_briefing"]["time"], p["deadline_check"]["time"], *a["window"], *a["checkpoints"]] \
        if isinstance(a["window"], list) and isinstance(a["checkpoints"], list) else [None]
    ok = ok and all(isinstance(t, str) and HM_RE.fullmatch(t) for t in times) and len(a["window"]) == 2 \
        and len(a["checkpoints"]) <= 6 and a["window"][0] < a["window"][1]
    ok = ok and all(isinstance(a[k], int) and not isinstance(a[k], bool) for k in ("max_per_day", "min_gap_minutes")) \
        and 0 <= a["max_per_day"] <= 10 and 0 <= a["min_gap_minutes"] <= 1440
    if not ok:
        raise HTTPException(400, "proactive: see PROTOCOL.md section 3 for the shape")


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
    s["proactive"] = migrate_proactive(s.get("proactive"))
    s["default_device"] = OLD_DEVICE_NAMES.get(s["default_device"], s["default_device"])
    s["device_fallback"] = [OLD_DEVICE_NAMES.get(d, d) for d in s["device_fallback"]]
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


async def take_lease(name, ttl_s, priority=100):
    return (await state.call("POST", f"/v1/state/leases/{quote(name, safe='')}",
                             {"holder": INSTANCE, "ttl_s": ttl_s, "priority": priority}))["held"]


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
    if not CLAUDE_OK:
        yield {"type": "error", "code": "no_claude", "message": "이 기기에서는 Claude를 쓸 수 없어요."}
        return
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
    return await apns_send(payload, "voip")


async def send_alert_push(title, body):
    """An ordinary notification (e.g. a missed scheduled call); needs the app's alert token."""
    return await apns_send({"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}, "alert")


async def apns_send(payload, kind):
    cfg = apns_config()
    tokens = [t for t in await push_tokens() if t.get("kind", "voip") == kind]
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
                "authorization": f"bearer {_apns_jwt['token']}",
                "apns-topic": t["bundle_id"] + (".voip" if kind == "voip" else ""),
                "apns-push-type": kind, "apns-priority": "10", "apns-expiration": "0"})
            sent += r.status_code == 200
    return {"sent": sent}


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
    p = s["proactive"]
    try:  # scheduled calls: only the scheduler lease holder fires (renewed every tick, lowest priority wins)
        if await take_lease("scheduler", 150, SCHED_PRIORITY):
            await fire_scheduled(now.timestamp())
            if now.minute in (0, 30) and p["enabled"] and p["autonomous"]["enabled"]:
                await autonomous_tick(now, p["autonomous"])
    except StateDown:
        pass
    a = p["autonomous"]
    if CLAUDE_OK and p["enabled"] and a["enabled"] and hm in a["checkpoints"] and fired.get(f"cp{hm}") != today:
        fired[f"cp{hm}"] = today
        try:
            if await take_lease(f"job:checkpoint:{today}:{hm}", 86400):
                await run_checkpoint()
        except StateDown:
            pass
    jobs = {"morning_briefing": lambda: place_call("morning_briefing"), "deadline_check": deadline_call}
    if not CLAUDE_OK or not p["enabled"]:
        jobs = {}  # briefings and deadline checks need Claude; the master switch stops every proactive call
    for key, job in jobs.items():
        if p[key]["enabled"] and hm == p[key]["time"] and fired.get(key) != today:
            fired[key] = today
            try:
                won = await take_lease(f"job:{key}:{today}", 86400)
            except StateDown:
                won = False  # a missed briefing beats several phones ringing at once
            if won:
                await job()


RETENTION_AT = "04:00"  # call history older than history_days is deleted daily


# ---------- autonomous calls: the server decides when to call, with guardrails ----------
SITUATION = "azel-situation"  # memory-service task context the Claude checkpoint writes
CHECKPOINT_PROMPT = (
    "자율 전화 판단용 상황 메모를 써. GISTRA(과제·시험·수업), Planit(할 일), 캘린더, 메일을 확인해서 "
    "앞으로 48시간 안에 마감이거나 사용자의 결정·답장이 필요한 것만, 한 줄에 하나씩 '[긴급도 1-5] 무엇 — 언제까지 — "
    "왜 사용자가 필요함' 형식으로 10줄 이내로 써. 없으면 '없음'이라고만 써. 인사나 설명은 쓰지 마."
)
DECIDE_PROMPT = (
    "너는 개인 비서가 사용자에게 먼저 전화할지 정하는 판단기야. 아래 신호와 상황 메모를 보고, 지금 전화하지 않으면 "
    "사용자가 손해를 보는 일일 때만 전화해. 단순 정보, 이미 지난 일, 사용자가 알아서 할 일은 전화하지 마. "
    'JSON 하나만 출력해: {"call": true|false, "urgency": 1-5, "reason": "한 줄 이유", '
    '"opening": "전화 첫마디, 이유를 한두 문장으로, 존댓말"}'
)


async def run_checkpoint():
    """One `claude -p` that reads GISTRA/Planit/Calendar/Gmail and leaves a compact situation note in memory."""
    text = await ask_text(CHECKPOINT_PROMPT, session=f"checkpoint-{uuid.uuid4().hex[:8]}", model="sonnet",
                          task="schedule")
    if text:
        await state.call("PUT", f"/v1/context/{SITUATION}", {"summary": text[:4000], "open_questions": []})


async def device_jobs(now):
    """Delegated jobs that finished or failed in the last 2 h, per trusted daemon (list_jobs over MCP)."""
    out = []
    for d in await fresh_devices():
        try:
            res = await mcp(d["url"], "tools/call", {"name": "list_jobs", "arguments": {}})
            jobs = json.loads("".join(c.get("text", "") for c in res.get("content", [])))["jobs"]
        except Exception:
            continue
        for j in jobs:
            fin = j.get("finished")
            if j.get("status") not in ("running", "pending", "queued") and isinstance(fin, (int, float)) \
                    and now - fin < 7200:
                out.append(f"{d['id']}: {str(j.get('task', ''))[:60]} → {j.get('status')}")
    return out


async def gather_signals(now):
    """Everything the decision may use, without Claude. Short strings only; each source fails on its own."""
    sig: dict[str, list] = {}

    async def source(name, fn):
        try:
            sig[name] = await fn()
        except Exception as e:
            print("signal failed:", name, type(e).__name__, flush=True)
            sig[name] = []

    async def alerts_recent():
        rows = await state.call("GET", "/v1/state/kv/alerts_recent") or []
        return [f"{r['names']} ({round((now - r['at']) / 60)}분 전)" for r in rows if now - r["at"] < 86400]

    async def approvals():
        rows = await state.call("GET", "/v1/state/approvals") or []
        return [f"{r['device']}: {r['command'][:60]} ({round((now - r['created']) / 60)}분째 승인 대기)"
                for r in rows if now - r["created"] > 1800]

    async def missed():
        rows = await state.call("GET", "/v1/state/schedule", status="missed") or []
        return [f"{spoken_when(r['at'], r['repeat'], now)} {'기상' if r['kind'] == 'wake' else '예약'} 전화 못 받음"
                for r in rows if now - (r["last_attempt"] or 0) < 86400]

    async def contexts():
        rows = await state.call("GET", "/v1/context", recent=20) or []
        return [f"{r['task_id']}: {r['summary'][:80]} / 열린 질문: {'; '.join(r['open_questions'])[:120]}"
                for r in rows if r["open_questions"] and r["task_id"] != SITUATION]

    for name, fn in (("alerts", alerts_recent), ("approvals", approvals), ("missed_calls", missed),
                     ("tasks", contexts), ("device_jobs", lambda: device_jobs(now))):
        await source(name, fn)
    note = None
    try:
        note = await state.call("GET", f"/v1/context/{SITUATION}")
    except Exception:
        pass
    sig["situation"] = [note["summary"][:1500]] if note and note.get("summary") else []
    return sig


async def llm_json(system, user):
    """One request to the local Qwen; the first {...} in its answer, or None."""
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(LLM_URL, json={"model": LLM_MODEL, "max_tokens": 300, "temperature": 0.2,
                                        "messages": [{"role": "system", "content": system},
                                                     {"role": "user", "content": user}]})
    r.raise_for_status()
    out = r.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(out[out.index("{"):out.rindex("}") + 1])
    except ValueError:
        return None


async def qwen_decide(signals, now):
    """Qwen's verdict, normalized; never trusted beyond its shape (the guardrails decide what happens)."""
    if not any(signals.values()):
        return {"call": False, "urgency": 0, "reason": "신호 없음", "opening": ""}
    when = datetime.fromtimestamp(now, KST).strftime("%Y-%m-%d %H:%M (%a)")
    try:
        d = await llm_json(DECIDE_PROMPT, f"지금: {when} KST\n" + json.dumps(signals, ensure_ascii=False))
    except Exception as e:
        print("autonomous decision failed:", type(e).__name__, flush=True)
        d = None
    if not isinstance(d, dict):
        return {"call": False, "urgency": 0, "reason": "판단 실패", "opening": ""}
    u = d.get("urgency")
    urgency = u if isinstance(u, int) and not isinstance(u, bool) and 0 <= u <= 5 else 0  # strict: no 4.9, no "5"
    reason = str(d.get("reason") or "")[:200]
    return {"call": d.get("call") is True, "urgency": urgency, "reason": reason,
            "opening": str(d.get("opening") or reason)[:200]}


def reason_hash(reason):
    return hashlib.sha256(re.sub(r"[\W_]+", "", reason.lower()).encode()).hexdigest()[:16]


def guard(decision, now, called_recent, a):
    """Why this decision may not ring (None = ring). `called_recent`: proactive_log rows that called, last 24 h."""
    hm, today = datetime.fromtimestamp(now, KST).strftime("%H:%M"), datetime.fromtimestamp(now, KST).date()
    if not decision["call"]:
        return "no_call"
    if decision["urgency"] < 4:
        return "urgency"
    if not a["window"][0] <= hm < a["window"][1]:
        return "window"
    if sum(datetime.fromtimestamp(r["at"], KST).date() == today for r in called_recent) >= a["max_per_day"]:
        return "max_per_day"
    if any(now - r["at"] < a["min_gap_minutes"] * 60 for r in called_recent):
        return "min_gap"
    if any(r["reason_hash"] == reason_hash(decision["reason"]) for r in called_recent):
        return "duplicate"
    return None


async def autonomous_tick(now_dt, a):
    """Every 30 min on the scheduler lease holder: signals → local Qwen → guardrails → maybe a call. Every
    decision is logged, called or not, so the user can see why it called or didn't."""
    now = now_dt.timestamp()
    signals = await gather_signals(now)
    decision = await qwen_decide(signals, now)
    recent = await state.call("GET", "/v1/state/proactive_log", since=now - 86400, called="true") or []
    blocked = guard(decision, now, recent, a)
    entry = {**decision, "reason_hash": reason_hash(decision["reason"]), "called": False, "blocked_by": blocked,
             "signals": {k: v[:10] for k, v in signals.items()}}
    if blocked is None:
        call = await place_call("autonomous", decision["opening"] or decision["reason"])
        entry.update(called=True, call_id=call["call_id"])
    await state.call("POST", "/v1/state/proactive_log", entry)
    return entry


async def remember_alert(names, now):
    """Critical alerts become an autonomous-call signal (kept 24 h, at most 20)."""
    try:
        rows = await state.call("GET", "/v1/state/kv/alerts_recent") or []
        rows = [r for r in rows if now - r["at"] < 86400][-19:] + [{"names": names, "at": now}]
        await state.call("PUT", "/v1/state/kv/alerts_recent", rows)
    except (StateDown, HTTPException):
        pass


# ---------- scheduled calls ("내일 10시에 깨워 줘"); no Claude involved ----------
REPEAT_RE = re.compile(r"daily|weekdays|weekly:[0-6]")  # weekly:<dow>, 0 = Monday
MAX_DAYS, MAX_ATTEMPTS, RETRY_EVERY_S, ANSWER_WAIT_S = 30, 3, 120, 60


def check_schedule(body, created_by, now=None):
    """Validate a new scheduled call from the app or the voice server (a trust boundary)."""
    now = now or time.time()
    try:
        dt = datetime.fromisoformat(body.get("at"))
        if dt.tzinfo is None:
            raise ValueError("no offset")
        at = dt.timestamp()
    except (TypeError, ValueError):
        raise HTTPException(400, "at: an ISO 8601 string with an offset, e.g. 2026-11-22T10:00:00+09:00")
    if not now + 30 <= at <= now + MAX_DAYS * 86400 + 60:
        raise HTTPException(400, f"at must be ahead and within {MAX_DAYS} days")
    repeat = body.get("repeat") or None
    if repeat is not None and not (isinstance(repeat, str) and REPEAT_RE.fullmatch(repeat)):
        raise HTTPException(400, "repeat: null, daily, weekdays or weekly:<0-6> (0 = Monday)")
    kind = body.get("kind") or "wake"
    note = body.get("note") or ""
    if kind not in ("wake", "reminder") or not isinstance(note, str) or len(note) > 200:
        raise HTTPException(400, "kind: wake|reminder; note: up to 200 characters")
    return {"at": at, "repeat": repeat, "kind": kind, "note": note.strip(), "created_by": created_by}


def public_item(row):
    """The /v1/schedule item shape (app and voice alike): `at` as ISO 8601 in KST, no internal fields."""
    return {"id": row["id"], "at": datetime.fromtimestamp(row["at"], KST).isoformat(), "repeat": row["repeat"],
            "kind": row["kind"], "note": row["note"], "status": row["status"], "attempts": row["attempts"],
            "created_by": row["created_by"]}


def next_at(at, repeat, now=None):
    """The next occurrence of a repeating call after `now`."""
    now, dt = now or time.time(), datetime.fromtimestamp(at, KST)
    step = timedelta(days=7 if repeat.startswith("weekly") else 1)
    while True:
        dt += step
        if dt.timestamp() > now and (repeat != "weekdays" or dt.weekday() < 5):
            return dt.timestamp()


def spoken_when(at, repeat=None, now=None):
    """'내일 오전 10시', '매일 오전 7시', '평일 오전 6시 40분', '매주 월요일 오전 9시'."""
    dt, today = datetime.fromtimestamp(at, KST), datetime.fromtimestamp(now or time.time(), KST).date()
    clock = f"{'오전' if dt.hour < 12 else '오후'} {dt.hour % 12 or 12}시" + (f" {dt.minute}분" if dt.minute else "")
    if repeat == "daily":
        day = "매일"
    elif repeat == "weekdays":
        day = "평일"
    elif repeat:
        day = f"매주 {'월화수목금토일'[int(repeat.split(':')[1])]}요일"
    else:
        diff = (dt.date() - today).days
        day = {0: "오늘", 1: "내일", 2: "모레"}.get(diff, f"{dt.month}월 {dt.day}일")
    return f"{day} {clock}"


def confirm_text(item, now=None):
    verb = "깨워 드릴게요" if item["kind"] == "wake" else "전화드릴게요"
    return f"{spoken_when(item['at'], item['repeat'], now)}에 {verb}."


async def schedule_create(body, created_by, now=None):
    """(public item, sentence to speak)."""
    row = await state.call("POST", "/v1/state/schedule", check_schedule(body, created_by, now))
    return public_item(row), confirm_text(row, now)


async def schedule_list():
    return await state.call("GET", "/v1/state/schedule") or []


def list_text(items, now=None):
    if not items:
        return "예약된 전화가 없어요."
    parts = [spoken_when(i["at"], i["repeat"], now) + (" 기상" if i["kind"] == "wake" else " 알림") for i in items[:3]]
    more = f" 외 {len(items) - 3}개" if len(items) > 3 else ""
    return f"예약된 전화는 {', '.join(parts)}{more}이에요."


async def schedule_cancel(sid=None, at=None, now=None):
    """Cancel by id, else the item at `at` (within a minute), else the soonest one."""
    items = await schedule_list()
    pick = next((i for i in items if i["id"] == sid), None) if sid else \
        next((i for i in items if abs(i["at"] - float(at)) < 60), None) if at else (items[0] if items else None)
    if not pick:
        return None
    row = await state.call("DELETE", f"/v1/state/schedule/{quote(pick['id'], safe='')}")
    return (public_item(row), f"{spoken_when(row['at'], row['repeat'], now)} 예약을 취소했어요.") if row else None


async def serve_schedule(m):
    """A schedule request from the voice server over the link; the reply carries the sentence to speak."""
    reply = {"id": m["id"], "type": "schedule_result", "ok": True}
    try:
        action = m.get("action")
        if MODE != "full":
            raise HTTPException(403, "limited")
        if action == "create":
            item, say = await schedule_create(m, "voice")
            reply.update(item=item, say=say)
        elif action == "list":
            items = await schedule_list()
            reply.update(items=[public_item(i) for i in items], say=list_text(items))
        elif action == "cancel":
            done = await schedule_cancel(at=m.get("at"))
            reply.update(item=done[0] if done else None, say=done[1] if done else "취소할 예약이 없어요.")
        else:
            raise HTTPException(400, "action")
    except HTTPException as e:
        reply.update(ok=False, say="그 시간으로는 예약할 수 없어요." if e.status_code == 400 else "지금은 예약을 받을 수 없어요.")
    except StateDown:
        reply.update(ok=False, say="지금은 예약을 받을 수 없어요.")
    await link_send(reply)


def opening_for(item):
    if item["kind"] == "wake":  # shortcut: no day summary yet (it would need cached calendar data without Claude)
        return "일어나실 시간이에요." + (f" {item['note']}" if item["note"] else "")
    return f"말씀하신 시간이에요. {item['note']}" if item["note"] else "말씀하신 시간이 돼서 전화드렸어요."


async def update_item(item, **fields):
    """Compare-and-set on the item's current status; None when another gateway got there first."""
    try:
        return await state.call("POST", f"/v1/state/schedule/{quote(item['id'], safe='')}",
                                {**fields, "expect_status": item["status"]})
    except HTTPException as e:
        if e.status_code == 409:
            return None
        raise


async def fire_scheduled(now):
    """Due items ring; unanswered ones retry every RETRY_EVERY_S up to MAX_ATTEMPTS, then count as missed.
    User-requested calls fire even with proactive.enabled off (the user asked for that time)."""
    for item in await schedule_list():
        since = now - (item["last_attempt"] or 0)
        if item["status"] == "pending" and item["at"] <= now:
            await attempt(item, now)
        elif item["status"] == "fired" and item["attempts"] < MAX_ATTEMPTS and since >= RETRY_EVERY_S:
            await attempt(item, now)
        elif item["status"] == "fired" and item["attempts"] >= MAX_ATTEMPTS and since >= ANSWER_WAIT_S:
            await missed(item, now)


async def attempt(item, now):
    claimed = await update_item(item, status="fired", attempts=item["attempts"] + 1, last_attempt=now)
    if claimed:
        call = await place_call(item["kind"], opening_for(item))
        await state.call("POST", f"/v1/state/schedule/{quote(item['id'], safe='')}", {"call_id": call["call_id"]})


async def missed(item, now):
    if item["repeat"]:  # the next one still happens
        done = await update_item(item, status="pending", attempts=0, at=next_at(item["at"], item["repeat"], now))
    else:
        done = await update_item(item, status="missed")
    if done:
        what = "기상 전화" if item["kind"] == "wake" else "예약 전화"
        await send_alert_push((await load_settings())["assistant_name"],
                              f"{spoken_when(item['at'], item['repeat'], now)} {what}를 받지 않으셨어요.")


async def call_answered(call_id):
    """The voice server saw /v1/call with this call_id: a fired scheduled call was answered."""
    if MODE != "full" or not isinstance(call_id, str):
        return
    for item in await schedule_list():
        if item["status"] == "fired" and item["call_id"] == call_id:
            if item["repeat"]:
                await update_item(item, status="pending", attempts=0, at=next_at(item["at"], item["repeat"]))
            else:
                await update_item(item, status="answered")


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
        elif m.get("type") == "schedule" and isinstance(m.get("id"), str):
            tasks[m["id"]] = asyncio.create_task(serve_schedule(m))
        elif m.get("type") == "call_answered":
            try:
                await call_answered(m.get("call_id"))
            except (StateDown, HTTPException) as e:
                print("call_answered not recorded:", type(e).__name__, flush=True)
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
                await link_send({"type": "hello", "device": DEVICE, "mode": MODE, "priority": PRIORITY,
                                 "claude": CLAUDE_OK})
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
    if "proactive" in body:  # a partial update merges into the current values, one level deep
        if not isinstance(body["proactive"], dict):
            raise HTTPException(400, "proactive must be an object")
        cur = (await load_settings())["proactive"]
        both = {**cur, **body["proactive"]}
        merged = migrate_proactive({k: {**cur[k], **v} if isinstance(cur.get(k), dict) and isinstance(v, dict) else v
                                    for k, v in both.items()})
        check_proactive(merged)
        body["proactive"] = merged
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
        raise HTTPException(403, "not available on a limited gateway")


@app.get("/v1/proactive/log")
async def proactive_log(limit: int = 50, since: float = 0):
    """Why the assistant called, or decided not to: every autonomous decision, newest first."""
    full_only()
    return await state.call("GET", "/v1/state/proactive_log", limit=max(1, min(limit, 500)), since=since)


@app.get("/v1/schedule")
async def schedule_get(include: str = ""):
    """Pending and fired items, soonest first; `include=all` adds answered, missed and cancelled ones."""
    full_only()
    status = "pending,fired,answered,missed,cancelled" if include == "all" else "pending,fired"
    return [public_item(i) for i in await state.call("GET", "/v1/state/schedule", status=status) or []]


@app.post("/v1/schedule", status_code=201)
async def schedule_post(body: dict):
    full_only()
    return (await schedule_create(body, "app"))[0]


@app.delete("/v1/schedule/{sid}")
async def schedule_delete(sid: str):
    full_only()
    done = await schedule_cancel(sid=sid)
    if done is None:
        raise HTTPException(404, "no such scheduled call")
    return done[0]


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
    if body.get("alert_token"):  # ordinary notifications (missed scheduled calls)
        await state.call("PUT", f"/v1/state/push_tokens/{quote(body['alert_token'], safe='')}",
                         {"bundle_id": body["bundle_id"], "env": body.get("env", "sandbox"), "kind": "alert"})
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
    if not firing:
        return {"called": False}
    names = ", ".join(sorted({a["labels"].get("alertname", "알 수 없는 경보") for a in firing}))
    await remember_alert(names, time.time())
    if not s["proactive"]["enabled"] or not s["proactive"]["server_alerts"]:
        return {"called": False}
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
