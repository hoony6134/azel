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
import contextvars
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
from fastapi.responses import JSONResponse, Response, StreamingResponse

import connectors
import nim
import wake_enroll
import claude_api
from claude_warm import Pool, base_cmd

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
# One warm `claude` per voice session (claude_warm.py); one-shot jobs (briefings, checkpoints, brain) stay cold.
WARM = os.environ.get("BISEO_WARM_CLAUDE", "1") not in ("0", "false", "no")
COLD_SESSIONS = ("briefing-", "deadline-", "checkpoint-", "brain-")
warm_pool = Pool()
call_warm_keys: dict[str, str] = {}  # call_log id -> its warm process key, closed when the call ends
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
    "user_title": "",  # how the assistant addresses the user ("" = naturally, no fixed title); a persona may override
    "history_days": 365,  # call history kept this long; 0 = forever
    "personality": "",  # free text up to 600 chars; "" = PERSONALITY_DEFAULT (the primary persona's)
    "personas": [],  # managed through /v1/personas; empty = the assistant migrates to persona azel
    "reply_length": "normal",  # brief | normal | detailed: drives both Qwen's and Claude's answer length
    "noise_mode": "normal",  # normal | noisy: the voice server's voice-activity thresholds and barge-in
    "connectors": {"claude_ai": {}, "custom": []},  # which MCP servers Claude runs load (connectors.py)
    "providers": {"nim": nim.DEFAULT},  # other model providers (nim.py); keys never live in settings
}
CHOOSABLE = ("auto", "qwen", "nim", *MODELS)  # default_model, task_defaults.*.model
NAME_RE = re.compile(r"[가-힣A-Za-z ]{1,12}")
TITLE_RE = re.compile(r"[가-힣A-Za-z ]{0,12}")


def check_title(v, field):
    """user_title (shared or a persona's): 0-12 Hangul/Latin letters or spaces, whitespace collapsed; null → ""."""
    if v is None:
        return ""
    if not (isinstance(v, str) and TITLE_RE.fullmatch(v)):
        raise HTTPException(400, f"{field}: 0-12 Hangul/Latin letters or spaces")
    return " ".join(v.split())


# reply_length: the same wording the voice server gives Qwen, so both routes answer at the same length.
LENGTH_STYLE = {
    "brief": "짧게 한두 문장으로 요점만 말해. ",
    "normal": "질문에 맞는 길이로 말해. 잡담과 간단한 답은 한두 문장, 설명·요약·일정·상태 보고는 네다섯 문장 정도로 자연스럽게. ",
    "detailed": "질문에 맞는 길이로 말해. 잡담은 짧게, 설명·요약·일정·상태 보고는 여섯에서 여덟 문장까지 자세히, 말하듯 자연스럽게. ",
}


# Azel's character (shared setting `personality`; "" / missing = this default). The single source: the gateway
# sends the default to the voice server in the settings (`personality_default`), so Qwen and Claude get the same text.
PERSONALITY_DEFAULT = (
    "유능하고 믿음직한 개인 비서. 핵심부터 정확하게 말하고, 일정·마감·장애·돈·건강처럼 중요한 일은 진지하게 "
    "다룬다. 분위기가 편할 때만 짧고 재치 있는 한마디를 덧붙이되 답 하나에 한 번을 넘기지 않는다. 급하거나 나쁜 소식, 사용자가 지쳐 보일 때,"
    " 위험한 작업이나 승인이 필요한 일에는 장난을 치지 않는다. 존댓말을 쓰되 딱딱하지 않게, 친한 동료처럼 말한다. 과장된 칭찬이나 이모티콘 같은 말투는 쓰지 않는다."
)


def personality(settings):
    p = settings.get("personality")
    return p.strip() if isinstance(p, str) and p.strip() else PERSONALITY_DEFAULT


def character_line(name, text, title=""):
    """The persona's character as the assistant's own (same wording in gateway.py and voice_server.py). Without the
    framing a character like "귀엽고 따뜻한 동생" was read as a description of the user ("귀여우시네요"). `title`: how to
    address the user (the user_title setting); without one, never a made-up 오빠/형/언니/누나."""
    text = text.strip().rstrip(".。 ")
    address = (f"사용자를 '{title}'(이)라고 부른다. 그 호칭은 자연스러운 곳에서만 쓰고 매 문장마다 부르지는 마. " if title else
               "사용자를 오빠, 형, 언니, 누나처럼 새로 부르지 말고 지금까지 부르던 대로 불러. ")
    return (f"너({name})의 성격과 말투: {text}. 이것은 너 자신의 성격이야. 사용자를 이 말로 묘사하거나 칭찬하지 말고, "
            "말투와 태도로만 드러내. 성격에 나오는 관계 말(동생, 친구, 선배, 비서 같은 것)은 너의 말투와 친밀도만 정해. "
            + address + "장난스러운 한마디는 답 하나에 한 번까지만 하고, 진지한 주제에는 하지 마. ")


def user_title(settings, persona):
    """The title the user set for this persona, else the shared one ("" = none). Up to 12 letters, no quotes."""
    for t in ((persona or {}).get("user_title"), settings.get("user_title")):
        t = re.sub(r"[\"'\\\x00-\x1f]", "", t).strip()[:12] if isinstance(t, str) else ""
        if t:
            return t
    return ""


def persona_block(settings):
    """Who is talking: name and character of the persona this ask belongs to (the primary by default). Used for
    Claude's voice style and every prompt that speaks to the user."""
    p = active_persona(settings)
    text = p["personality"].strip() if isinstance(p.get("personality"), str) and p["personality"].strip() \
        else PERSONALITY_DEFAULT
    return f"너의 이름은 '{p['name']}'이야. " + character_line(p["name"], text, user_title(settings, p))


# ---------- personas: several assistants, one primary (stored in the shared settings as `personas`) ----------
PERSONA_ID = re.compile(r"[a-z0-9_-]{1,32}")
COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
MAX_PERSONAS = 8
DEFAULT_TTS_VOICE = os.environ.get("BISEO_TTS_VOICE", "sohee")
current_persona: contextvars.ContextVar = contextvars.ContextVar("persona", default=None)  # set per ask/call


def migrate_personas(s):
    """Settings from before personas: the assistant becomes persona `azel` (primary), keeping its name and
    character; `assistant_name`/`personality` stay readable as the primary's for older clients."""
    ps = [p for p in s.get("personas") or [] if isinstance(p, dict) and p.get("id")]
    if not any(p.get("primary") and not p.get("deleted_at") for p in ps):
        ps = [p for p in ps if p.get("id") != "azel"]
        ps.insert(0, {"id": "azel", "name": s.get("assistant_name") or DEFAULTS["assistant_name"],
                      "voice": DEFAULT_TTS_VOICE, "voice_style": "", "personality": s.get("personality") or "",
                      "color": "#7C5CFF", "default_model": None, "share_memory": False, "created_at": 0,
                      "primary": True})
    for p in ps:
        p.setdefault("user_title", "")
    s["personas"] = ps
    prim = primary_persona(s)
    s["assistant_name"], s["personality"] = prim.get("name") or DEFAULTS["assistant_name"], prim.get("personality") or ""
    return s


def live_personas(s):
    return [p for p in s["personas"] if not p.get("deleted_at")]


def primary_persona(s):
    return next(p for p in s["personas"] if p.get("primary") and not p.get("deleted_at"))


def persona_by_id(s, pid, include_deleted=False):
    return next((p for p in s["personas"] if p["id"] == pid and (include_deleted or not p.get("deleted_at"))), None)


def active_persona(s):
    pid = current_persona.get()
    return (persona_by_id(s, pid) if pid else None) or primary_persona(s)


def name_prompt(settings):
    """Persona and reply length, from the shared settings."""
    return persona_block(settings) + LENGTH_STYLE.get(settings.get("reply_length"), LENGTH_STYLE["normal"])
# Personal data only full gateways may touch. Same list as the voice server's PERSONAL.
PERSONAL = re.compile(r"일정(?!한)|캘린더|스케줄|회의|약속|메일|편지함|기억|메모리|연락처|전화번호|과제|수업|시험|성적|마감"
                      r"|할 ?일|브리핑|지스트라|GISTRA|플래닛|Planit|지난번|저번에|예전에 (내가|우리)"
                      # the voice server's PLANS_ASK: "내일 내가 해야 될 일", "토요일에 뭐 있어"
                      r"|해야 ?(할|될|하는|되는) ?(일|거|것)|할 ?(거|것|게) ?(뭐|있|정리|알려)"
                      r"|(오늘|내일|모레|글피|이번 ?주|다음 ?주|주말|[월화수목금토일]요일).{0,10}(뭐 ?(해|하|있)|정리|계획|바빠|바쁘|일과|해야)",
                      re.I)
PERSONAL_REFUSAL = "개인 비서 기능은 지금 쓸 수 없어요."
VOICE_BASE = (
    "지금은 음성 통화 중이고 답은 그대로 소리로 읽혀. 한국어 존댓말로 말하고, 첫 문장은 짧게 하되 '네'가 아니라 "
    "내용으로 시작해. "
    "마크다운, 목록, 표, 이모지, URL, 코드 블록을 쓰지 마. '확인하지 않은 것' 같은 꼬리말도 붙이지 마. "
    "커밋이나 파일에 AI 작성자 표시를 절대 남기지 마. "
    "도구 결과의 영어 상태어(firing, resolved, pending, DOWN, critical 같은 것), 키 이름, 기호는 그대로 읽지 말고 "
    "'경보가 울리고 있는', '해결된', '멈춘'처럼 자연스러운 한국어로 바꿔 말해. 서버 이름은 그대로 말해. "
    "기기 명령이 pending_approval을 돌려주면 먼저 '앱에서 허용해 주세요'라고 짧게 말한 다음, 같은 명령을 그 "
    "approval_id와 함께 다시 호출해서 승인을 기다려. 거절되거나(rejected) 만료되면(expired) 허용되지 않았다고 말해. "
)
VOICE_STYLE = VOICE_BASE + (
    "사용자 데이터는 Cygnus MCP(메모리, GISTRA, Planit, 인프라), 캘린더, 메일 도구로 확인해. "
    "macbook이나 서버에서 할 일은 등록된 기기 MCP 도구로 해. 위험한 명령이 승인 대기면 앱에서 승인해 달라고 말해."
)
# Full gateways on the Messages API path (claude_path "api", opt-in): Cygnus through our read-only bridge, no
# Gmail or Calendar (those are claude.ai connectors).
FULL_API_STYLE = VOICE_BASE + (
    "사용자의 일정, 과제, 할 일, 기억, 서버 상태는 주어진 Cygnus 도구로 확인하고, 확인하지 못한 것은 지어내지 마. "
    "메일과 캘린더는 이 경로에서 볼 수 없으니 그렇게 말해. 기기 작업은 주어진 기기 도구로만 해. "
    "위험한 명령이 승인 대기면 앱에서 승인해 달라고 말해."
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
        self._lock, self._loop = None, None

    def lock(self):
        """One sender at a time (two asks finishing together drained the same list: 'pop from empty list')."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._lock, self._loop = asyncio.Lock(), loop
        return self._lock

    async def put(self, body):
        self.items.append(body)
        async with self.lock():
            await self._flush()

    async def _flush(self):
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
# Team devices (vd-spark, vd-core) cannot dial a jh gateway (ACL), so their daemons never register: full gateways
# list them in HOME/static_devices.json (or BISEO_STATIC_DEVICES) and probe them directly. Rows live only in this
# process (the shared devices table has no `limited` column); each needs its daemon's own bearer token in
# HOME/device_tokens/<id> (0600): the shared token never goes to a team device.
STATIC_PATH = HOME / "static_devices.json"
static_devices: dict[str, dict] = {}
device_tokens: dict[str, str] = {}  # daemon url -> its bearer token, when not the shared one
limited_urls: set[str] = set()  # daemons that may never be handed a `delegate`
STATIC_PROBE_S = 60


def load_static_devices(raw):
    for e in json.loads(raw or "[]"):
        did, url = str(e.get("id", "")), e.get("url")
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", did) or not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError(f"static_devices: bad entry {did!r}")
        if e.get("mode") != "limited":
            raise ValueError(f"static_devices: {did} must be mode limited")
        tok = secret(f"device_tokens/{did}")
        if not tok:
            print(f"static device {did} skipped: no {HOME}/device_tokens/{did}", flush=True)
            continue
        static_devices[did] = {"id": did, "name": str(e.get("name") or did)[:40], "url": url, "harness": [],
                               "gateway": DEVICE, "writer": "full", "limited": True, "static": True, "last_seen": 0.0}
        device_tokens[url] = tok
        limited_urls.add(url)


if MODE == "full":
    load_static_devices(os.environ.get("BISEO_STATIC_DEVICES") or (STATIC_PATH.read_text() if STATIC_PATH.exists() else ""))


def with_static(rows):
    """Shared device rows plus this gateway's static ones (a static row wins over a row with the same id)."""
    return [d for d in rows if d["id"] not in static_devices] + list(static_devices.values())


async def static_prober():
    while True:
        for d in list(static_devices.values()):
            if (await probe(d["url"], token=True))["ok"]:
                d["last_seen"] = time.time()
        await asyncio.sleep(STATIC_PROBE_S)


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
    migrate_personas(s)
    s["personality_default"] = PERSONALITY_DEFAULT  # shown, never stored (the app's placeholder, the voice server's text)
    s["default_device"] = OLD_DEVICE_NAMES.get(s["default_device"], s["default_device"])
    s["device_fallback"] = [OLD_DEVICE_NAMES.get(d, d) for d in s["device_fallback"]]
    s["providers"] = {**DEFAULTS["providers"], **(s.get("providers") or {})}
    s["providers"]["nim"] = {**nim.DEFAULT, **s["providers"]["nim"]}
    return s


def settings_view(s):
    """Settings as the app sees them: plus `providers.nim.has_key` (only whether the key exists)."""
    return {**s, "providers": {**s["providers"], "nim": nim.view(s["providers"]["nim"], HOME)}}


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


async def take_lease(name, ttl_s, priority=100, holder=None):
    """`holder` defaults to this process (it renews its own lease); pass a fresh one for 'only if nobody has it'."""
    return (await state.call("POST", f"/v1/state/leases/{quote(name, safe='')}",
                             {"holder": holder or INSTANCE, "ttl_s": ttl_s, "priority": priority}))["held"]


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
    return [d for d in with_static(rows) if d["last_seen"] > time.time() - DEVICE_FRESH_S and trusted(d)]


# A short status or lookup question: Haiku answers it about 1 s sooner. Anything with several steps stays Sonnet.
LOOKUP = re.compile(r"괜찮|상태|뭐 ?있|몇 ?(개|시|명)|언제|어디|켜져|있어\?|됐어\?|알려 ?줘|확인해 ?줘")
MULTI_STEP = re.compile(r"그리고|하고 나서|정리|계획|짜 ?줘|비교|분석|고쳐|만들|보내|써 ?줘|요약|왜")


def resolve(settings, model, path, task, text=""):
    """(model, path). A model the user chose (in the ask, `default_model` or `task_defaults`) always wins; with
    none, short lookups get Haiku and everything else Sonnet."""
    td = settings["task_defaults"].get(task or "", {})
    model = model or td.get("model") or settings["default_model"]
    if model == "nim":
        if MODE == "limited":
            raise HTTPException(400, "nim: not on a limited gateway")
        return "nim", "nim"
    if model in ("auto", "qwen"):  # the voice server only calls us when Claude is wanted
        said = text.rsplit("\n\n", 1)[-1]  # the voice server prefixes recent turns
        model = "haiku" if len(said) <= 40 and LOOKUP.search(said) and not MULTI_STEP.search(said) else "sonnet"
    path = path or td.get("path") or settings["claude_path"]
    if path == "auto":  # a key on disk never switches a full gateway off claude.ai's connectors: "api" is opt-in
        path = "claude_code" if MODE == "full" else "api"
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
    servers, allowed, run_settings, run_env = run_args(settings, devices)
    resume = await resume_id(session)
    cfg, proc = None, None
    if WARM and not session.startswith(COLD_SESSIONS):
        source = (await warm_pool.get(session, MODELS[model], style, allowed, WORKDIR, servers, resume, run_settings,
                                      run_env)).turn(text, MODELS[model])
    else:
        if servers:
            fd, cfg = tempfile.mkstemp(suffix=".json")  # holds tokens: 0600, deleted after the call
            with os.fdopen(fd, "w") as f:
                json.dump({"mcpServers": servers}, f)
        env = os.environ | {"MCP_CONNECTION_NONBLOCKING": "false"} | run_env  # wait for the (few) servers
        cmd = base_cmd(MODELS[model], style, allowed, cfg, resume, run_settings)
        proc = await asyncio.create_subprocess_exec(cmd[0], "-p", text, *cmd[1:],
                                                    cwd=WORKDIR, env=env, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL, limit=2**24)
        source = proc.stdout
    final = {"model": model, "path": "claude_code"}
    open_tools: dict[str, str] = {}  # tool_use id -> name, until its result comes back
    try:
        async for raw in source:
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
                             output_tokens=u.get("output_tokens", 0), error=e.get("is_error", False),
                             result=str(e.get("result") or "")[:300])
                if e.get("session_id"):
                    try:
                        await state.call("PUT", f"/v1/state/sessions/{quote(session, safe='')}",
                                         {"claude_session": e["session_id"], "device": DEVICE})
                    except StateDown:
                        pass  # the next turn just starts a fresh Claude session
        if proc:
            await proc.wait()
    finally:
        if proc and proc.returncode is None:
            proc.kill()
        if cfg:
            os.unlink(cfg)
    yield final


discovery = connectors.Discovery(CLAUDE)


def run_args(settings, devices):
    """(--mcp-config servers, allowed tools, run-scoped --settings, env) for one Claude run: only the connectors and
    custom servers the user switched on, plus the device daemons."""
    return connectors.run_args(settings.get("connectors") or {}, discovery, devices, TOKEN, HOME, device_tokens)


async def resume_id(session):
    prev = await state.get(f"/v1/state/sessions/{quote(session, safe='')}")
    return prev["claude_session"] if prev and prev["device"] == DEVICE else None  # sessions live on their host


async def prewarm(session, persona_id=None):
    """A call started: start its warm Claude now, so MCP connects while the user is still talking. Same model,
    prompt (the call's persona) and devices as ask_claude_code would use for a chat turn, so that turn reuses it.
    `session` is the key the voice server's asks use: "<persona>:<call session>"."""
    if not (WARM and CLAUDE_OK and MODE == "full") or not isinstance(session, str) or not session \
            or session.startswith(COLD_SESSIONS):
        return
    current_persona.set(persona_id if isinstance(persona_id, str) else None)  # this task only
    try:
        WORKDIR.mkdir(parents=True, exist_ok=True)
        devices, settings = await fresh_devices(), await load_settings()
        model, path = resolve(settings, None, None, "chat")
        if path != "claude_code":
            return
        style = name_prompt(settings) + VOICE_STYLE + device_prompt(settings, devices)
        servers, allowed, run_settings, run_env = run_args(settings, devices)
        await warm_pool.get(session, MODELS[model], style, allowed, WORKDIR, servers, await resume_id(session),
                            run_settings, run_env)
    except Exception as e:
        print("prewarm failed:", type(e).__name__, flush=True)


async def mcp(url, method, params):
    """One JSON-RPC call to a daemon's stateless streamable-HTTP MCP endpoint (JSON or SSE answer)."""
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                         headers={"Authorization": f"Bearer {device_tokens.get(url, TOKEN)}",
                                  "Accept": "application/json, text/event-stream"})
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
            if t["name"] == "delegate" and d.get("limited"):
                continue  # a team device only reads and diagnoses: never hand it work
            name = f"{d['id']}__{t['name']}"[:64]
            where[name] = (d["url"], t["name"])
            tools.append({"name": name, "description": f"[{d['name']}] {t.get('description') or ''}"[:1024],
                          "input_schema": t.get("inputSchema") or {"type": "object"}})
    return tools, where


async def run_tool(block, where):
    try:
        url, name = where[block["name"]]
        if name == "delegate" and url in limited_urls:
            raise RuntimeError("delegate is not allowed on a limited device")
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
    if MODE == "full":  # opt-in path: prompt caching and the Cygnus bridge (claude_api.py)
        async for item in ask_api_full(text, model, settings, devices, tools, where):
            yield item
        return
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


async def ask_api_full(text, model, settings, devices, dev_tools, where):
    """A full gateway's Messages API turn: Cygnus bridge tools plus the devices' tools, stable cached prefix."""
    cyg_tools, cyg_run = claude_api.cygnus_bridge(HOME)
    by_name = {t["name"] for t in cyg_tools}

    async def run(name, args):
        if name in by_name:
            return await cyg_run(name, args)
        r = await run_tool({"name": name, "input": args, "id": "x"}, where)
        if r.get("is_error"):
            raise RuntimeError("device tool failed")
        return r["content"]
    system = name_prompt(settings) + FULL_API_STYLE + device_prompt(settings, devices)
    yield activity("thinking")
    async for kind, v in claude_api.ask(API_KEY, model, system, text, cyg_tools + dev_tools, run):
        if kind == "text":
            yield v
        elif kind == "tool":
            yield activity("tool_start", v)
        else:
            yield {"model": model, "path": "api", "input_tokens": v["input_tokens"] + v["cache_read_input_tokens"]
                   + v["cache_creation_input_tokens"], "output_tokens": v["output_tokens"], "cost_usd": v["cost_usd"]}


async def ask_nim(text, model, session):
    """The hosted NVIDIA NIM path: streams like Claude, with the devices' tools when the model takes tools."""
    settings = await load_settings()
    cfg = settings["providers"]["nim"]
    if not cfg["enabled"]:
        yield {"type": "error", "code": "nim_off", "message": "엔비디아 모델이 꺼져 있어요. 앱의 설정에서 켜 주세요."}
        return
    devices = await fresh_devices()
    tools, where = await device_tools(devices)
    cyg_tools, cyg_run = claude_api.cygnus_bridge(HOME)  # the brain's read-only Cygnus tools, when this host has them
    cyg = {t["name"] for t in cyg_tools}
    system = name_prompt(settings) + API_STYLE + device_prompt(settings, devices)

    async def run(b):
        if b["name"] in cyg:
            return {"content": await cyg_run(b["name"], b["input"]), "is_error": False}
        return await run_tool(b, where)

    def act(phase, tool=""):
        return activity(phase, tool if tool in cyg or not tool else f"mcp__{tool}")
    try:
        async for item in nim.stream(cfg, nim.read_key(HOME), system, text, cyg_tools + tools, run, act):
            yield item
    except nim.NimError as e:
        yield {"type": "error", "code": e.code, "message": e.message}
    except httpx.HTTPError as e:
        print("nim failed:", type(e).__name__, flush=True)  # never the key: it is only in a header
        yield {"type": "error", "code": "nim_error", "message": "지금은 엔비디아 모델에 연결할 수 없어요."}


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
    if MODE == "limited" and model == "nim":
        model = None  # a limited gateway has no NIM: its usual Claude path
    model, path = resolve(settings, model, path, task, text)
    note_requester(session=session, persona=current_persona.get() or primary_persona(settings)["id"])
    gen = {"claude_code": ask_claude_code, "nim": ask_nim}.get(path, ask_api)
    said = False
    async for item in gen(text, model, session):
        if isinstance(item, str):
            said = said or bool(item.strip())
            yield {"type": "text", "delta": item}
        elif item.get("type") in ("activity", "error"):
            yield item
            if item["type"] == "error":
                return
        else:
            await record_usage({"model": item.get("model") or model, "path": path, "task": task or "",
                                "input_tokens": item.get("input_tokens", 0),
                                "output_tokens": item.get("output_tokens", 0), "cost_usd": item.get("cost_usd", 0)})
            if item.get("error") and not said:  # e.g. the subscription's usage limit: say so instead of silence
                yield claude_failure(item.get("result", ""))
                return
            spent = await month_cost()
            yield {"type": "done", **{k: v for k, v in item.items() if k != "result"},
                   "budget_warning": spent >= budget * settings["budget_warn_pct"] / 100}


LIMIT_WORDS = re.compile(r"limit|429|rate|quota|resets?|한도|사용량", re.I)


def claude_failure(result):
    """A Claude run that ended in an error with nothing said: a speakable error event (code `limit` for the usage
    limit, else `claude_error`). The result text names the reason; only its gist reaches the caller."""
    if LIMIT_WORDS.search(result):
        when = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", result, re.I)
        hint = ""
        if when:
            h = int(when.group(1)) % 12 + (12 if when.group(3).lower() == "pm" else 0)
            hint = f" {'오전' if h < 12 else '오후'} {h % 12 or 12}시{' ' + when.group(2) + '분' if when.group(2) and when.group(2) != '00' else ''}쯤 풀려요."
        return {"type": "error", "code": "limit",
                "message": "지금은 Claude 사용 한도에 걸려서 답할 수 없어요." + (hint or " 조금 뒤에 다시 물어봐 주세요.")}
    return {"type": "error", "code": "claude_error", "message": "지금은 Claude가 답하지 못했어요. 조금 뒤에 다시 물어봐 주세요."}


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


async def send_alert_push(title, body, **extra):
    """An ordinary notification (e.g. a missed scheduled call); needs the app's alert token. `extra` rides at the
    payload's top level next to `aps` (a task push: `type: "task"`, `task_id`, so the app opens azel://task/<id>)."""
    return await apns_send({"aps": {"alert": {"title": title, "body": body}, "sound": "default"}, **extra}, "alert")


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


async def place_call(reason, opening=None, call_id=None, persona_id=None):
    """Ring the phone as a persona (the primary by default: briefings, deadlines, alerts, autonomous calls).
    `call_id`: chosen by the caller when it must be recorded before the phone rings."""
    if MODE == "limited":
        raise HTTPException(403, "limited gateways do not place calls")
    if opening is None and reason == "morning_briefing":
        opening = await ask_text(
            "아침 브리핑 전화를 걸었어. 오늘 수업과 마감(GISTRA), 오늘 할 일(Planit), 캘린더 일정, "
            "밤사이 서버 상태(Cygnus 인프라)를 확인해서 '좋은 아침이에요'로 시작하는 30초 안쪽 브리핑으로 말해.",
            session=f"briefing-{uuid.uuid4().hex[:8]}", model="sonnet", task="schedule")
    call_id = call_id or uuid.uuid4().hex
    try:
        await state.call("POST", "/v1/state/calls", {"id": call_id, "reason": reason, "opening": opening or ""})
    except StateDown:
        pass  # the opening still reaches the voice server over the link, which is what the call needs
    # The opening must reach the voice server before the phone can answer and connect.
    settings = await load_settings()
    persona = (persona_by_id(settings, persona_id) if persona_id else None) or primary_persona(settings)
    await link_send({"type": "call", "call_id": call_id, "opening": opening or "", "reason": reason,
                     "persona": persona["id"]})
    push = await send_voip_push({"call_id": call_id, "reason": reason, "caller": persona["name"],
                                 "persona": persona["id"]})
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
SITUATION = "azel-situation"  # merged situation note, written by the brain (assistant/brain)
CHECKPOINT = "azel-checkpoint"  # the Claude checkpoint's raw note; fallback when the situation note is stale
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
        await state.call("PUT", f"/v1/context/{CHECKPOINT}", {"summary": text[:4000], "open_questions": []})


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
                for r in rows if r["open_questions"] and r["task_id"] not in (SITUATION, CHECKPOINT)]

    for name, fn in (("alerts", alerts_recent), ("approvals", approvals), ("missed_calls", missed),
                     ("tasks", contexts), ("device_jobs", lambda: device_jobs(now))):
        await source(name, fn)
    note = await situation_note(now)
    sig["situation"] = [note] if note else []
    return sig


async def situation_note(now):
    """The brain's merged note, or the raw checkpoint if that is missing or stale; "" if neither is fresh."""
    for key in (SITUATION, CHECKPOINT):
        try:
            note = await state.call("GET", f"/v1/context/{key}")
        except Exception:
            note = None
        if note and note.get("summary") and fresh_note(note, now):
            return note["summary"][:1500]
    return ""


async def serve_briefing_context(m):
    """What the voice server's local Qwen needs for a spoken briefing (`/v1/call?intent=briefing`): the
    situation note plus a few live facts. Empty situation = no note yet, the voice server asks Claude instead."""
    now, facts = time.time(), []
    situation = ""
    if MODE == "full":
        situation = await situation_note(now)
        try:
            pend = await state.call("GET", "/v1/state/approvals") or []
            if pend:
                facts.append(f"승인 대기 {len(pend)}건")
            items = await schedule_list()
            if items:
                i = items[0]
                facts.append(f"다음 예약 전화: {spoken_when(i['at'], i['repeat'], now)} "
                             f"{'기상' if i['kind'] == 'wake' else '알림'}")
            alerts = await state.call("GET", "/v1/state/kv/alerts_recent") or []
            recent = [a["names"] for a in alerts if now - a["at"] < 86400]
            facts.append(f"최근 24시간 서버 경보: {', '.join(recent)}" if recent else "최근 24시간 서버 경보 없음")
        except (StateDown, HTTPException):
            pass
    await link_send({"id": m["id"], "type": "briefing_context", "situation": situation, "facts": facts})


def fresh_note(note, now, max_age_s=6 * 3600):
    """True if the context row was updated within max_age_s (rows without a parseable time count as fresh)."""
    try:
        return now - datetime.fromisoformat(str(note["updated"])).timestamp() < max_age_s
    except (KeyError, ValueError):
        return True


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
        persona = persona_block(await load_settings())  # the opening is said in Azel's voice
        d = await llm_json(DECIDE_PROMPT + f" opening은 이 비서의 말투로 써: {persona}",
                           f"지금: {when} KST\n" + json.dumps(signals, ensure_ascii=False))
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
    recent = await state.call("GET", "/v1/state/proactive_log", since=now - 86400, called="true")
    if not isinstance(recent, list):  # unreadable history = unknown cap and gap: fail closed, no call
        raise StateDown("proactive_log unreadable")
    blocked = guard(decision, now, recent, a)
    entry = {**decision, "persona_id": primary_persona(await load_settings())["id"],
             "reason_hash": reason_hash(decision["reason"]), "called": blocked is None,
             "blocked_by": blocked, "signals": {k: v[:10] for k, v in signals.items()}}
    if blocked is None:  # write the row first: if it cannot be recorded, the cap cannot count it, so no call
        entry["call_id"] = uuid.uuid4().hex
        await state.call("POST", "/v1/state/proactive_log", entry)
        await place_call("autonomous", decision["opening"] or decision["reason"], call_id=entry["call_id"])
        return entry
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
LATE_S = 600  # a scheduled call more than 10 min late (gateway down) becomes missed instead of ringing


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
    if repeat and repeat.startswith("weekly:") and datetime.fromtimestamp(at, KST).weekday() != int(repeat[7:]):
        raise HTTPException(400, "repeat weekly:<dow> must match the weekday of at (0 = Monday)")
    kind = body.get("kind") or "wake"
    note = body.get("note") or ""
    if kind not in ("wake", "reminder") or not isinstance(note, str) or len(note) > 200:
        raise HTTPException(400, "kind: wake|reminder; note: up to 200 characters")
    return {"at": at, "repeat": repeat, "kind": kind, "note": note.strip(), "created_by": created_by}


def public_item(row):
    """The /v1/schedule item shape (app and voice alike): `at` as ISO 8601 in KST, no internal fields."""
    return {"id": row["id"], "at": datetime.fromtimestamp(row["at"], KST).isoformat(), "repeat": row["repeat"],
            "kind": row["kind"], "note": row["note"], "status": row["status"], "attempts": row["attempts"],
            "created_by": row["created_by"], "persona_id": row.get("persona_id", "azel")}


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
    settings = await load_settings()
    pid = body.get("persona_id") or primary_persona(settings)["id"]
    if not persona_by_id(settings, pid):
        raise HTTPException(400, "persona_id: no such persona")
    row = await state.call("POST", "/v1/state/schedule", {**check_schedule(body, created_by, now), "persona_id": pid})
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
        if item["status"] in ("pending", "fired") and now - item["at"] > LATE_S:
            await missed(item, now, late=True)  # gateway was down: no 07:00 alarm at 13:00
        elif item["status"] == "pending" and item["at"] <= now:
            await attempt(item, now)
        elif item["status"] == "fired" and item["attempts"] < MAX_ATTEMPTS and since >= RETRY_EVERY_S:
            await attempt(item, now)
        elif item["status"] == "fired" and item["attempts"] >= MAX_ATTEMPTS and since >= ANSWER_WAIT_S:
            await missed(item, now)


async def attempt(item, now):
    call_id = uuid.uuid4().hex  # in the claim itself, so call_answered always finds this attempt
    claimed = await update_item(item, status="fired", attempts=item["attempts"] + 1, last_attempt=now, call_id=call_id)
    if claimed:
        await place_call(item["kind"], opening_for(item), call_id=call_id, persona_id=item.get("persona_id"))


async def missed(item, now, late=False):
    if item["repeat"]:  # the next one still happens
        done = await update_item(item, status="pending", attempts=0, at=next_at(item["at"], item["repeat"], now))
    else:
        done = await update_item(item, status="missed")
    if done:
        what = "기상 전화" if item["kind"] == "wake" else "예약 전화"
        how = "를 제때 걸지 못했어요" if late else "를 받지 않으셨어요"
        await send_alert_push((await load_settings())["assistant_name"],
                              f"{spoken_when(item['at'], item['repeat'], now)} {what}{how}.")


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
    current_persona.set(m.get("persona") if isinstance(m.get("persona"), str) else None)  # this task only
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
            if m.get("event") == "start" and isinstance(m.get("session"), str):
                pid = m.get("persona_id") if isinstance(m.get("persona_id"), str) else "azel"
                warm_key = f"{pid}:{m['session']}"  # what the voice server's asks use as the session
                if isinstance(m.get("log_id"), str):
                    call_warm_keys[m["log_id"]] = warm_key
                asyncio.create_task(prewarm(warm_key, pid))
            elif m.get("event") == "end" and (warm_key := call_warm_keys.pop(m.get("log_id"), None)):
                asyncio.create_task(warm_pool.close(warm_key))  # the call is over: so is its warm Claude
            await record_call_log(m)  # in order, so a call's turns follow its start
        elif m.get("type") == "schedule" and isinstance(m.get("id"), str):
            tasks[m["id"]] = asyncio.create_task(serve_schedule(m))
        elif m.get("type") == "task" and isinstance(m.get("id"), str):
            tasks[m["id"]] = asyncio.create_task(serve_task(m))
        elif m.get("type") == "briefing_context" and isinstance(m.get("id"), str):
            tasks[m["id"]] = asyncio.create_task(serve_briefing_context(m))
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
    tasks = [asyncio.create_task(voice_link()), asyncio.create_task(reap_warm())]
    if MODE == "full":  # scheduled jobs need personal data
        tasks.append(asyncio.create_task(scheduler()))
        if static_devices:
            tasks.append(asyncio.create_task(static_prober()))
        tasks.append(asyncio.create_task(fail_orphan_tasks_safe()))
        if CLAUDE_OK:
            tasks.append(asyncio.create_task(discovery.refresh()))
        if OLD_DB.exists() and not MIGRATED.exists():
            tasks.append(asyncio.create_task(migrate_once()))
    yield
    for t in tasks:
        t.cancel()
    for run in list(task_runs.values()):  # a stopping gateway kills its task processes
        run.cancel()
    await asyncio.gather(*task_runs.values(), return_exceptions=True)
    await warm_pool.close_all()


async def fail_orphan_tasks_safe():
    try:
        await fail_orphan_tasks()
    except StateDown:
        pass


async def reap_warm():
    while True:
        await asyncio.sleep(60)
        try:
            await warm_pool.reap()
        except Exception as e:
            print("warm reap failed:", type(e).__name__, flush=True)


app = FastAPI(title="biseo-gateway", lifespan=lifespan, dependencies=[Depends(auth)])
if MODE == "full":  # wake-word enrollment (wake_enroll.py): the user's own clips stay on jh devices
    app.include_router(wake_enroll.router)


@app.exception_handler(StateDown)
async def state_down(request, exc):
    return JSONResponse({"detail": "공유 상태 서버(ruchba)에 연결할 수 없어 지금은 읽기만 돼요."}, status_code=503)


@app.post("/v1/ask")
async def ask(body: dict):
    if not body.get("text"):
        raise HTTPException(400, "text required")

    async def gen():
        current_persona.set(body.get("persona") if isinstance(body.get("persona"), str) else None)
        async for ev in ask_stream(body["text"], body.get("session", "default"), body.get("model"),
                                   body.get("path"), body.get("task")):
            yield json.dumps(ev, ensure_ascii=False) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/v1/settings")
async def get_settings():
    return settings_view(await load_settings())


@app.put("/v1/settings")
async def put_settings(body: dict):
    body.pop("personality_default", None)  # read-only: an app may send the whole document back
    body.pop("personas", None)  # managed through /v1/personas
    unknown = set(body) - set(DEFAULTS)
    if unknown:
        raise HTTPException(400, f"unknown keys: {sorted(unknown)}")
    name = body.get("assistant_name")
    if name is not None and not (isinstance(name, str) and NAME_RE.fullmatch(name) and name.strip()):
        raise HTTPException(400, "assistant_name: 1-12 Hangul/Latin letters or spaces")
    if name is not None:
        body["assistant_name"] = " ".join(name.split())
    if "user_title" in body:
        body["user_title"] = check_title(body["user_title"], "user_title")
    if "proactive" in body:  # a partial update merges into the current values, one level deep
        if not isinstance(body["proactive"], dict):
            raise HTTPException(400, "proactive must be an object")
        cur = (await load_settings())["proactive"]
        both = {**cur, **body["proactive"]}
        merged = migrate_proactive({k: {**cur[k], **v} if isinstance(cur.get(k), dict) and isinstance(v, dict) else v
                                    for k, v in both.items()})
        check_proactive(merged)
        body["proactive"] = merged
    if "personality" in body:
        if body["personality"] is None:
            body["personality"] = ""
        if not isinstance(body["personality"], str) or len(body["personality"]) > 600:
            raise HTTPException(400, "personality: text up to 600 characters (\"\" = the default)")
    if "noise_mode" in body and body["noise_mode"] not in ("normal", "noisy"):
        raise HTTPException(400, "noise_mode: normal or noisy")
    if "reply_length" in body and body["reply_length"] not in LENGTH_STYLE:
        raise HTTPException(400, f"reply_length: one of {sorted(LENGTH_STYLE)}")
    if "connectors" in body:
        full_only()
        try:
            body["connectors"] = connectors.check(body["connectors"])
        except ValueError as e:
            raise HTTPException(400, str(e))
    if "default_model" in body and body["default_model"] not in CHOOSABLE:
        raise HTTPException(400, f"default_model: one of {list(CHOOSABLE)}")
    if "task_defaults" in body:
        td = body["task_defaults"]
        if not isinstance(td, dict) or not all(isinstance(v, dict) and v.get("model", "auto") in CHOOSABLE
                                               for v in td.values()):
            raise HTTPException(400, f"task_defaults: {{task: {{model: one of {list(CHOOSABLE)}, path?}}}}")
    if "providers" in body:
        full_only()
        prov = body["providers"]
        if not isinstance(prov, dict) or set(prov) - {"nim"}:
            raise HTTPException(400, "providers: {nim: {...}}")
        try:
            cur = (await load_settings())["providers"]
            body["providers"] = {**cur, **({"nim": nim.check(prov["nim"], cur["nim"])} if "nim" in prov else {})}
        except ValueError as e:
            raise HTTPException(400, str(e))
    days = body.get("history_days")
    if days is not None and (isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 36500):
        raise HTTPException(400, "history_days: a whole number of days, 0 = keep forever")
    s = await load_settings() | body
    prim = primary_persona(s)  # older clients edit the primary persona through assistant_name / personality
    prim["name"], prim["personality"] = s["assistant_name"], s["personality"]
    await save_settings({k: v for k, v in s.items() if k != "personality_default"})
    await link_send({"type": "settings", "value": s})
    return settings_view(s)


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
    headers = {"Accept": "text/event-stream"} | ({"Authorization": f"Bearer {device_tokens.get(url, TOKEN)}"} if token else {})
    try:
        # Headers only: an MCP endpoint may answer GET with a stream that never ends.
        async with httpx.AsyncClient(timeout=3) as c, c.stream("GET", url, headers=headers) as r:
            ok = r.status_code < 500
        return {"ok": ok, "ms": round((time.perf_counter() - t) * 1000)}
    except httpx.HTTPError:
        return {"ok": False, "ms": None}


@app.get("/v1/status")
async def status():
    devs = with_static(await state.get("/v1/state/devices", list(local_devices.values())))
    probes = await asyncio.gather(probe(f"{VOICE_URL}/health"), probe(f"{TTS_URL}/health"),
                                  probe(f"{STATE_URL}/health"), *[probe(d["url"], trusted(d)) for d in devs])
    return {"spark": probes[0], "tts": probes[1], "state": probes[2],
            "gateway": {"ok": True, "ms": 0, "device": DEVICE, "mode": MODE, "link": link["ws"] is not None},
            "devices": [{"id": d["id"], "name": d["name"], "limited": bool(d.get("limited")), **p,
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
    return with_static(await state.get("/v1/state/devices", list(local_devices.values())))


def full_only():
    if MODE != "full":
        raise HTTPException(403, "not available on a limited gateway")


@app.get("/v1/providers/nim/models")
async def nim_models():
    """Suggested model ids from NIM's /models (cached 10 min); the model setting stays free text."""
    full_only()
    key = nim.read_key(HOME)
    if not key:
        raise HTTPException(409, "no NIM key: PUT /v1/providers/nim/key first")
    cfg = (await load_settings())["providers"]["nim"]
    try:
        return {"models": await nim.list_models(cfg, key)}
    except nim.NimError as e:
        raise HTTPException(502, e.message)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"NIM unreachable ({type(e).__name__})")


@app.put("/v1/providers/nim/key", status_code=204)
async def nim_key(body: dict):
    """Write-only: the key goes to HOME/providers/nim_key (0600) and is never returned; "" deletes it."""
    full_only()
    try:
        nim.write_key(HOME, nim.check_key(body.get("value")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    nim._models.update(at=0.0)  # a new key may see other models
    return Response(status_code=204)


# Labels for the TTS server's speakers (Qwen3-TTS CustomVoice): the voice's character only, never a language or
# nationality. `lang` stays in /v1/voices for older apps (not shown). Unknown speakers show their id.
VOICE_INFO = {
    "sohee": ("소희", "따뜻하고 감정이 풍부한 여성", "ko"), "vivian": ("비비안", "밝고 또렷한, 살짝 톡 쏘는 젊은 여성", "zh"),
    "serena": ("세레나", "부드럽고 다정한 젊은 여성", "zh"), "uncle_fu": ("푸 아저씨", "낮고 묵직한, 연륜 있는 남성", "zh"),
    "dylan": ("딜런", "맑고 자연스러운 젊은 남성", "zh"), "eric": ("에릭", "활기차고 살짝 허스키한 남성", "zh"),
    "ryan": ("라이언", "리듬감 있고 힘찬 남성", "en"), "aiden": ("에이든", "밝고 시원한 청년", "en"),
    "ono_anna": ("오노 안나", "경쾌하고 발랄한 여성", "ja"),
}
_tts_voices: dict = {"at": 0.0, "voices": []}
_sample_at: list = [0.0]  # voice samples: one per 2 s on this gateway


@app.get("/v1/voices")
async def voices():
    """The TTS speakers for the persona editor, with Korean labels."""
    full_only()
    return [{"id": v, "label": VOICE_INFO.get(v, (v, "", ""))[0], "desc": VOICE_INFO.get(v, (v, "", ""))[1],
             "lang": VOICE_INFO.get(v, (v, "", ""))[2]} for v in await tts_voices()]


async def tts_sample(text, voice, style):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{TTS_URL}/v1/audio/speech", json={"input": text, "voice": voice, "response_format": "wav",
                                                             **({"instructions": style} if style else {})})
    r.raise_for_status()
    return r.content


@app.post("/v1/personas/voice-sample")
async def voice_sample(body: dict):
    """"들어보기" in the persona editor: a short line in a voice and style → audio/wav. One per 2 s."""
    full_only()
    text, voice, style = body.get("text"), body.get("voice"), body.get("voice_style") or ""
    if not (isinstance(text, str) and 0 < len(text.strip()) <= 60):
        raise HTTPException(400, "text: 1-60 characters")
    if not isinstance(voice, str) or voice.lower() not in await tts_voices():
        raise HTTPException(400, "voice: not a TTS speaker")
    if not isinstance(style, str) or len(style) > 200:
        raise HTTPException(400, "voice_style: text up to 200 characters")
    if time.time() - _sample_at[0] < 2:
        raise HTTPException(429, "one sample per 2 seconds")
    _sample_at[0] = time.time()
    try:
        return Response(await tts_sample(text.strip(), voice.lower(), style), media_type="audio/wav")
    except httpx.HTTPError:
        raise HTTPException(502, "TTS server unreachable")


async def tts_voices():
    """The TTS server's speakers (cached 10 min); BISEO_TTS_VOICES as the list when it cannot be reached."""
    if time.time() - _tts_voices["at"] < 600 and _tts_voices["voices"]:
        return _tts_voices["voices"]
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{TTS_URL}/v1/audio/voices")
        r.raise_for_status()
        _tts_voices.update(at=time.time(), voices=[str(v).lower() for v in r.json()["voices"]])
    except (httpx.HTTPError, ValueError, KeyError):
        fallback = [v.strip().lower() for v in os.environ.get("BISEO_TTS_VOICES", "").split(",") if v.strip()]
        if not fallback:
            raise HTTPException(503, "TTS server unreachable: cannot check the voice")
        return fallback
    return _tts_voices["voices"]


PERSONA_FIELDS = {"name", "voice", "voice_style", "personality", "color", "default_model", "share_memory", "user_title"}


async def check_persona(body, s, pid=None):
    """Validate persona fields (a trust boundary: the app writes them). `pid`: the persona being edited."""
    unknown = set(body) - PERSONA_FIELDS - ({"id"} if pid is None else set())
    if unknown:
        raise HTTPException(400, f"unknown persona fields: {sorted(unknown)}")
    if "name" in body:
        if not (isinstance(body["name"], str) and NAME_RE.fullmatch(body["name"]) and body["name"].strip()):
            raise HTTPException(400, "name: 1-12 Hangul/Latin letters or spaces")
        body["name"] = " ".join(body["name"].split())
        if any(p["name"] == body["name"] and p["id"] != pid for p in live_personas(s)):
            raise HTTPException(400, "name: another persona already has it")
    if "voice" in body and (not isinstance(body["voice"], str) or body["voice"].lower() not in await tts_voices()):
        raise HTTPException(400, "voice: not a TTS speaker (GET /v1/voices)")
    if not isinstance(body.get("voice_style", ""), str) or len(body.get("voice_style", "")) > 200:
        raise HTTPException(400, "voice_style: text up to 200 characters")
    if body.get("personality") is None and "personality" in body:
        body["personality"] = ""
    if "user_title" in body:
        body["user_title"] = check_title(body["user_title"], "user_title")
    if not isinstance(body.get("personality", ""), str) or len(body.get("personality", "")) > 600:
        raise HTTPException(400, "personality: text up to 600 characters (\"\" = the default)")
    if "color" in body and not (isinstance(body["color"], str) and COLOR_RE.fullmatch(body["color"])):
        raise HTTPException(400, "color: #rrggbb")
    if body.get("default_model") not in (None, "nim", *MODELS):
        raise HTTPException(400, f"default_model: null, nim or one of {sorted(MODELS)}")
    if not isinstance(body.get("share_memory", False), bool):
        raise HTTPException(400, "share_memory: true or false")


def persona_view(p):
    return {**{k: p.get(k) for k in ("id", "name", "voice", "voice_style", "personality", "color", "default_model",
                                     "share_memory", "created_at", "primary", "deleted_at")},
            "user_title": p.get("user_title") or ""}


async def save_personas(s):
    await save_settings({k: v for k, v in migrate_personas(s).items() if k != "personality_default"})
    await link_send({"type": "settings", "value": s})


@app.get("/v1/personas")
async def personas_list(include: str = ""):
    full_only()
    s = await load_settings()
    return [persona_view(p) for p in (s["personas"] if include == "deleted" else live_personas(s))]


@app.post("/v1/personas", status_code=201)
async def personas_create(body: dict):
    full_only()
    s = await load_settings()
    if len(live_personas(s)) >= MAX_PERSONAS:
        raise HTTPException(400, f"at most {MAX_PERSONAS} personas")
    for k in ("name", "voice"):
        if not body.get(k):
            raise HTTPException(400, f"{k} required")
    await check_persona(body, s)
    pid = body.pop("id", None) or f"p{uuid.uuid4().hex[:7]}"
    if not (isinstance(pid, str) and PERSONA_ID.fullmatch(pid)) or persona_by_id(s, pid, include_deleted=True):
        raise HTTPException(400, "id: a new ASCII slug (a-z, 0-9, _ -)")
    p = {"id": pid, "voice_style": "", "personality": "", "color": "#7C5CFF", "default_model": None,
         "share_memory": False, "user_title": "", **body, "voice": body["voice"].lower(), "created_at": time.time(), "primary": False}
    s["personas"].append(p)
    await save_personas(s)
    return persona_view(p)


@app.get("/v1/personas/{pid}")
async def personas_get(pid: str):
    full_only()
    p = persona_by_id(await load_settings(), pid, include_deleted=True)
    if not p:
        raise HTTPException(404, "no such persona")
    return persona_view(p)


@app.put("/v1/personas/{pid}")
async def personas_put(pid: str, body: dict):
    full_only()
    s = await load_settings()
    p = persona_by_id(s, pid)
    if not p:
        raise HTTPException(404, "no such persona")
    await check_persona(body, s, pid)
    p.update(body)
    if "voice" in body:
        p["voice"] = body["voice"].lower()
    await save_personas(s)
    return persona_view(p)


@app.delete("/v1/personas/{pid}")
async def personas_delete(pid: str, delete_history: bool = False):
    """Marks it deleted (the name stays resolvable; never the primary). Its own notes and call summaries always go;
    its call history stays, flagged "삭제된 비서", unless delete_history=true."""
    full_only()
    s = await load_settings()
    p = persona_by_id(s, pid)
    if not p:
        raise HTTPException(404, "no such persona")
    if p.get("primary"):
        raise HTTPException(400, "the primary persona cannot be deleted; make another one primary first")
    # data first: if ruchba cannot do it (StateDown → 503), the persona is not marked deleted either
    await state.call("DELETE", f"/v1/state/personas/{quote(pid, safe='')}/data", history=str(delete_history).lower())
    p["deleted_at"] = time.time()
    await save_personas(s)
    return persona_view(p)


@app.post("/v1/personas/{pid}/primary")
async def personas_primary(pid: str):
    full_only()
    s = await load_settings()
    p = persona_by_id(s, pid)
    if not p:
        raise HTTPException(404, "no such persona")
    for q in s["personas"]:
        q["primary"] = q is p
    await save_personas(s)
    return persona_view(p)


@app.get("/v1/proactive/log")
async def proactive_log(limit: int = 50, since: float = 0):
    """Why the assistant called, or decided not to: every autonomous decision, newest first."""
    full_only()
    return await state.call("GET", "/v1/state/proactive_log", limit=max(1, min(limit, 500)), since=since)


def connectors_out(conn):
    """The pinned app shape (PROTOCOL.md section 3): status is connected | needs_auth, no URLs of claude.ai
    connectors, never a secret."""
    v = connectors.view(conn, discovery, HOME)
    return {"claude_ai": [{"name": c["name"], "enabled": c["enabled"],
                           "status": "connected" if c["status"] == "connected" else "needs_auth"} for c in v["claude_ai"]],
            "custom": [{"name": c["name"], "url": c["url"], "transport": c.get("transport", "http"),
                        "enabled": c.get("enabled", True), "has_secret": c["has_secret"]} for c in v["custom"]]}


def connectors_in(body):
    """The app shape → the stored setting: claude_ai as {name: enabled}; a custom server's secret file is its name."""
    ai, custom = body.get("claude_ai", []), body.get("custom", [])
    if not isinstance(ai, list) or not all(isinstance(c, dict) and set(c) <= {"name", "enabled", "status"} for c in ai) \
            or not isinstance(custom, list) or set(body) - {"claude_ai", "custom"}:
        raise HTTPException(400, 'connectors: {"claude_ai":[{"name","enabled"}],"custom":[{"name","url","transport","enabled"}]}')
    try:
        return connectors.check({"claude_ai": {c.get("name"): c.get("enabled") for c in ai},
                                 "custom": [{**{k: v for k, v in c.items() if k != "has_secret"},
                                             "auth_ref": c.get("name")} if isinstance(c, dict) else c for c in custom]})
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/v1/connectors")
async def connectors_get(refresh: bool = False):
    """The claude.ai connectors this gateway's login has (status from `claude mcp list`), the custom servers and
    which are switched on. Never a secret."""
    full_only()
    if refresh or not discovery.at:
        await discovery.refresh()
    return connectors_out((await load_settings())["connectors"])


@app.put("/v1/connectors")
async def connectors_put(body: dict):
    """Replace the switches and the custom list (the GET shape without status / has_secret)."""
    full_only()
    await put_settings({"connectors": connectors_in(body)})
    return connectors_out((await load_settings())["connectors"])


@app.put("/v1/connectors/secret/{name}")
async def connectors_secret(name: str, body: dict):
    """Write-only: `{"value": "<token or JSON headers>"}` into ~/.config/biseo/mcp/<name> (0600) on this gateway."""
    full_only()
    try:
        connectors.put_secret(HOME, name, body.get("value"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    await warm_pool.close_all()  # running processes still hold the old secret
    return Response(status_code=204)


@app.get("/v1/schedule")
async def schedule_get(include: str = "", persona: str | None = None):
    """Pending and fired items, soonest first; `include=all` adds answered, missed and cancelled ones."""
    full_only()
    status = "pending,fired,answered,missed,cancelled" if include == "all" else "pending,fired"
    params = {"status": status} | ({"persona": persona} if persona else {})
    return [public_item(i) for i in await state.call("GET", "/v1/state/schedule", **params) or []]


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
async def history(limit: int = 20, before: float | None = None, q: str | None = None, persona: str | None = None):
    full_only()
    params = {"limit": limit} | ({"before": before} if before is not None else {}) | ({"q": q} if q else {}) \
        | ({"persona": persona} if persona else {})
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
    if not body.get("bundle_id") or not (body.get("voip_token") or body.get("alert_token")):
        raise HTTPException(400, "bundle_id and voip_token and/or alert_token required")  # macOS: alert only
    if body.get("voip_token"):
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
    all_names = sorted({a["labels"].get("alertname", "알 수 없는 경보") for a in firing})
    now = time.time()
    await remember_alert(", ".join(all_names), now)
    if not s["proactive"]["enabled"] or not s["proactive"]["server_alerts"]:
        return {"called": False}
    new = await alerts_to_ring(all_names, now)
    if not new:
        return {"called": False, "reason": "rang about these recently, or the daily cap is reached"}
    names = ", ".join(new)
    return {"called": True, **await place_call("server_down", f"서버 경보가 왔어요. {names}. 로그를 볼까요, 재시작할까요?")}


ALERT_NAME_S, ALERT_MAX_PER_DAY = 7200, 3  # one call per alert name per 2 h; at most 3 server-down calls per 24 h
local_alerts: dict = {"names": {}, "calls": []}  # the same limits on this gateway alone while ruchba is down


async def alerts_to_ring(names, now):
    """The alert names worth a call now, under the limits shared by every gateway (leases and a kv list), or
    this gateway's own memory when ruchba is down (ruchba itself may be what failed). A flapping alert
    re-fires every few minutes; its 2 h lease keeps it to one call."""
    try:
        calls = [t for t in (await state.call("GET", "/v1/state/kv/alert_calls") or []) if now - t < 86400]
        if len(calls) >= ALERT_MAX_PER_DAY:
            return []
        new = [n for n in names if await take_lease(f"alert:{n}", ALERT_NAME_S, holder=uuid.uuid4().hex)]
        if new:
            await state.call("PUT", "/v1/state/kv/alert_calls", calls + [now])
        return new
    except StateDown:
        local_alerts["calls"] = [t for t in local_alerts["calls"] if now - t < 86400]
        if len(local_alerts["calls"]) >= ALERT_MAX_PER_DAY:
            return []
        new = [n for n in names if now - local_alerts["names"].get(n, 0) >= ALERT_NAME_S]
        for n in new:
            local_alerts["names"][n] = now
        if new:
            local_alerts["calls"].append(now)
        return new


# ---------- approvals: risky or unlisted device commands wait for the user's tap ----------
# The approval row (device, tool, command, cwd, status) lives in ruchba's approvals table; what the table has no
# column for sits in kv: `approval-<id>` (expiry, requester, decision), `approval-rules` ("always" rules) and
# `approval-audit` (approved runs). A pending approval expires after APPROVAL_TTL_S.
APPROVAL_TTL_S = 300
DECISIONS = ("once", "always", "deny")
last_ask: dict = {}  # the most recent ask or task on this gateway: who an approval created meanwhile belongs to


def note_requester(**who):
    last_ask.clear()
    last_ask.update(who, at=time.time())


async def approval_meta(aid):
    return await state.call("GET", f"/v1/state/kv/approval-{aid}") or {}


def approval_view(row, meta, now=None):
    now = now or time.time()
    expires = meta.get("expires_at") or (row.get("created") or now) + APPROVAL_TTL_S
    status = "expired" if row["status"] == "pending" and now >= expires else row["status"]
    return {**row, "status": status, "expires_at": expires, "decision": meta.get("decision"),
            "requester": meta.get("requester"), "rule_id": meta.get("rule_id")}


@app.post("/v1/approvals")
async def create_approval(body: dict):
    row = {k: str(body.get(k) or "") for k in ("device", "tool", "command", "cwd")}
    if not row["device"] or not row["command"]:
        raise HTTPException(400, "device and command required")
    now = time.time()
    rules = await state.call("GET", "/v1/state/kv/approval-rules") or []
    rule = next((r for r in rules if r["device"] == row["device"] and r["command"] == row["command"]), None)
    created = await state.call("POST", "/v1/state/approvals", row)
    aid = created["approval_id"]
    requester = {k: v for k, v in last_ask.items() if k != "at"} if now - last_ask.get("at", 0) < 600 else None
    meta = {"expires_at": now + APPROVAL_TTL_S, "requester": requester, "decision": None}
    if rule:  # an "always" rule: approved on the spot, still audited when it runs
        await state.call("POST", f"/v1/state/approvals/{quote(aid, safe='')}", {"approve": True})
        meta.update(decision="rule", rule_id=rule["id"])
        await state.call("PUT", f"/v1/state/kv/approval-{aid}", meta)
        return {"approval_id": aid, "status": "approved", "rule_id": rule["id"]}
    await state.call("PUT", f"/v1/state/kv/approval-{aid}", meta)
    try:  # the app shows it; the command text only (never output), shortened
        await send_alert_push("승인 요청", f"{row['device']}: {row['command'][:80]}", type="approval", approval_id=aid)
    except Exception as e:
        print("approval push failed:", type(e).__name__, flush=True)
    return {"approval_id": aid, "status": "pending", "expires_at": meta["expires_at"]}


@app.get("/v1/approvals")
async def list_approvals():
    now, out = time.time(), []
    for row in await state.call("GET", "/v1/state/approvals") or []:
        v = approval_view(row, await approval_meta(row["id"]), now)
        if v["status"] == "pending":
            out.append(v)
    return out


@app.get("/v1/approvals/rules")
async def approval_rules():
    return await state.call("GET", "/v1/state/kv/approval-rules") or []


@app.delete("/v1/approvals/rules/{rid}")
async def approval_rule_delete(rid: str):
    rules = await state.call("GET", "/v1/state/kv/approval-rules") or []
    keep = [r for r in rules if r["id"] != rid]
    if len(keep) == len(rules):
        raise HTTPException(404, "no such rule")
    await state.call("PUT", "/v1/state/kv/approval-rules", keep)
    return {"deleted": rid}


@app.get("/v1/approvals/audit")
async def approval_audit(limit: int = 50):
    return (await state.call("GET", "/v1/state/kv/approval-audit") or [])[:max(1, min(limit, 500))]


@app.get("/v1/approvals/{aid}")
async def get_approval(aid: str):
    row = await state.call("GET", f"/v1/state/approvals/{quote(aid, safe='')}")
    if row is None:
        raise HTTPException(404, "no such approval")
    return approval_view(row, await approval_meta(aid))


@app.post("/v1/approvals/{aid}")
async def decide(aid: str, body: dict):
    """`{"decision": "once"|"always"|"deny"}` (older apps: `{"approve": bool}` = once / deny)."""
    decision = body.get("decision") or ("once" if body.get("approve") else "deny")
    if decision not in DECISIONS:
        raise HTTPException(400, f"decision: one of {list(DECISIONS)}")
    row = await state.call("GET", f"/v1/state/approvals/{quote(aid, safe='')}")
    if row is None:
        raise HTTPException(404, "no such approval")
    meta = await approval_meta(aid)
    view = approval_view(row, meta)
    if view["status"] != "pending":
        raise HTTPException(409, f"approval is {view['status']}")
    out = await state.call("POST", f"/v1/state/approvals/{quote(aid, safe='')}", {"approve": decision != "deny"})
    if decision == "always":
        rules = await state.call("GET", "/v1/state/kv/approval-rules") or []
        rule = {"id": uuid.uuid4().hex[:10], "device": row["device"], "command": row["command"], "created": time.time()}
        await state.call("PUT", "/v1/state/kv/approval-rules", [rule, *rules][:200])
        meta["rule_id"] = rule["id"]
    meta["decision"] = decision
    await state.call("PUT", f"/v1/state/kv/approval-{aid}", meta)
    return {**out, "decision": decision, **({"rule_id": meta["rule_id"]} if decision == "always" else {})}


@app.post("/v1/approvals/{aid}/ran")
async def approval_ran(aid: str, body: dict):
    """A daemon ran an approved command: the audit log gets device, command, exit code, time and decision."""
    row = await state.call("GET", f"/v1/state/approvals/{quote(aid, safe='')}")
    if row is None or row["status"] != "approved":
        raise HTTPException(404, "no such approved approval")
    code = body.get("exit_code")
    entry = {"approval_id": aid, "device": row["device"], "command": row["command"], "cwd": row["cwd"],
             "exit_code": code if isinstance(code, int) and not isinstance(code, bool) else None,
             "status": str(body.get("status") or "")[:20], "at": time.time(),
             "decision": (await approval_meta(aid)).get("decision")}
    audit = await state.call("GET", "/v1/state/kv/approval-audit") or []
    await state.call("PUT", "/v1/state/kv/approval-audit", [entry, *audit][:500])
    return {"ok": True}


# ---------- tasks: background Claude Code runs on the devices ("…해 줘, 끝나면 알려 줘") ----------
# Full gateways only. A task is not a voice turn: its own `claude -p` process (never the warm voice pool), at most
# TASK_SLOTS at once, hard limits on time and estimated cost, steps logged without arguments or results, approvals
# waited for, and a call or push at the end. Stored in ruchba's kv (`task-<id>` + the `tasks-index` list).
TASK_SLOTS = 2
TASK_DEFAULT_MIN, TASK_DEFAULT_USD = 30, 2.0
TASK_MAX_ROUNDS = 4  # one approval wait per round
TASK_STATUSES = ("queued", "running", "waiting_approval", "done", "failed", "cancelled")
TASK_STYLE = (" 지금은 통화가 아니라 백그라운드 작업이야. 목표를 끝까지 스스로 처리하고, 마지막에 결과를 두세 문장으로"
              " 요약해. 도구가 pending_approval을 돌려주면 그 명령을 다시 시도하지 말고 'APPROVAL_PENDING <approval_id>'"
              "라고만 쓰고 멈춰. 사용자의 승인 결과를 받으면 이어서 해. 한도(limited) 기기에는 읽기와 진단만 해.")
PENDING_RE = re.compile(r'"?status"?\s*[:=]\s*"?pending_approval"?.{0,80}?"?approval_id"?\s*[:=]\s*"?([\w-]{2,64})', re.S)
task_slots = asyncio.Semaphore(TASK_SLOTS)
task_runs: dict[str, asyncio.Task] = {}  # tasks this gateway is running


def task_key(tid):
    return f"/v1/state/kv/task-{tid}"


async def task_save(t):
    t["updated"] = time.time()
    await state.call("PUT", task_key(t["id"]), t)


async def task_index_add(t):
    # shortcut: read-modify-write of one list; two gateways creating tasks in the same instant can drop one entry
    # from the index (the task itself is kept). Move to a state table if tasks get busy.
    ids = [i for i in (await state.call("GET", "/v1/state/kv/tasks-index") or []) if i != t["id"]]
    await state.call("PUT", "/v1/state/kv/tasks-index", [t["id"], *ids][:200])


def task_step(t, kind, **fields):
    t["steps"].append({"at": time.time(), "kind": kind, **fields})
    del t["steps"][:-200]


def check_task(body):
    goal = body.get("goal")
    if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 2000:
        raise HTTPException(400, "goal: 1-2000 characters")
    notify = body.get("notify", "push")
    if notify not in ("call", "push", "none"):
        raise HTTPException(400, "notify: call, push or none")
    budget = body.get("budget_usd", TASK_DEFAULT_USD)
    minutes = body.get("max_minutes", TASK_DEFAULT_MIN)
    for name, v, top in (("budget_usd", budget, 50), ("max_minutes", minutes, 240)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= top:
            raise HTTPException(400, f"{name}: a number above 0, at most {top}")
    device = body.get("device")
    if device is not None and not (isinstance(device, str) and re.fullmatch(r"[a-z0-9_-]{1,32}", device)):
        raise HTTPException(400, "device: a device id")
    persona = body.get("persona")
    if persona is not None and not (isinstance(persona, str) and PERSONA_ID.fullmatch(persona)):
        raise HTTPException(400, "persona: a persona id")
    return {"goal": goal.strip(), "notify": notify, "budget_usd": float(budget), "max_minutes": float(minutes),
            "device": device, "persona": persona}


async def task_create(body, source="api"):
    full_only()
    if not CLAUDE_OK:
        raise HTTPException(503, "Claude is not available on this gateway")
    spec = check_task(body)
    settings = await load_settings()
    if spec["persona"] and not persona_by_id(settings, spec["persona"]):
        raise HTTPException(400, "persona: no such persona")
    t = {"id": uuid.uuid4().hex[:12], **spec, "status": "queued", "created": time.time(), "started": None,
         "finished": None, "owner": DEVICE, "source": source, "steps": [], "result": None, "error": None,
         "cost_usd": 0.0, "cancel_requested": False}
    await task_save(t)
    await task_index_add(t)
    task_runs[t["id"]] = asyncio.create_task(run_task(t))
    return t


def task_prompt(t, devices):
    where = f"작업 기기: {t['device']}." if t["device"] else "작업 기기는 기본 순서대로 골라."
    limited = [d["id"] for d in devices if d.get("limited")]
    note = f" 한도 기기(읽기·진단만): {', '.join(limited)}." if limited else ""
    return f"{where}{note}\n\n목표: {t['goal']}"


async def task_round(t, prompt, resume, deadline):
    """One `claude -p` run of a task: (session id, pending approval id or None). Kills on time or cost."""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    settings = await load_settings()
    devices = await fresh_devices()
    model = settings["task_defaults"].get("task", {}).get("model")
    model = model if model in MODELS else "sonnet"
    style = name_prompt(settings) + TASK_STYLE + device_prompt(settings, devices)
    servers, allowed, run_settings, run_env = run_args(settings, devices)
    cfg = None
    if servers:
        fd, cfg = tempfile.mkstemp(suffix=".json")  # holds tokens: 0600, deleted after the run
        with os.fdopen(fd, "w") as f:
            json.dump({"mcpServers": servers}, f)
    cmd = base_cmd(MODELS[model], style, allowed, cfg, resume, run_settings)
    text = prompt if resume else task_prompt(t, devices)
    proc = await asyncio.create_subprocess_exec(cmd[0], "-p", text, *cmd[1:], cwd=WORKDIR,
                                                env=os.environ | {"MCP_CONNECTION_NONBLOCKING": "false"} | run_env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                limit=2**24)
    pin, pout = PRICES[model]
    spent_before, session, pending, said = t["cost_usd"], resume, None, ""
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TaskLimit("time", f"{t['max_minutes']:g}분 시간 한도를 넘어 멈췄어요.")
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), min(left, 5))
            except asyncio.TimeoutError:
                if (await state.call("GET", task_key(t["id"])) or {}).get("cancel_requested"):
                    raise asyncio.CancelledError
                continue
            if not raw:
                break
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if e.get("type") == "assistant":
                msg = e.get("message") or {}
                for b in msg.get("content") or []:
                    if b.get("type") == "tool_use":
                        task_step(t, "tool", tool=b.get("name", ""), label=tool_label(b.get("name", "")))
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        said = b["text"].strip()
                u = msg.get("usage") or {}
                t["cost_usd"] += (u.get("input_tokens", 0) * pin + u.get("output_tokens", 0) * pout) / 1e6
                await task_save(t)
                if t["cost_usd"] >= t["budget_usd"]:
                    raise TaskLimit("budget", f"예산 {t['budget_usd']:g}달러를 넘어 멈췄어요.")
            elif e.get("type") == "user":
                for b in (e.get("message") or {}).get("content") or []:
                    if b.get("type") == "tool_result" and (m := PENDING_RE.search(json.dumps(b.get("content"), ensure_ascii=False).replace('\\"', '"'))):
                        pending = m.group(1)
            elif e.get("type") == "result":
                session = e.get("session_id") or session
                if e.get("total_cost_usd"):  # the CLI's own figure replaces the estimate for this round
                    t["cost_usd"] = spent_before + float(e["total_cost_usd"])
                said = str(e.get("result") or said)
                if e.get("is_error") and not pending:
                    raise TaskLimit("error", claude_failure(said)["message"])
        await proc.wait()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if cfg:
            os.unlink(cfg)
    t["result"] = re.sub(r"APPROVAL_PENDING\s+\S+", "", said).strip()[:2000] or None
    return session, pending


class TaskLimit(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind, self.message = kind, message


async def wait_approval(t, aid):
    """Poll the approval until decided (the task's clock stands still meanwhile): True if approved."""
    t["status"] = "waiting_approval"
    task_step(t, "approval", approval_id=aid)
    await task_save(t)
    await task_notify(t, approval=True)
    while True:
        row = await state.call("GET", f"/v1/state/approvals/{quote(aid, safe='')}")
        a = approval_view(row, await approval_meta(aid)) if row else {"status": "rejected"}
        if a["status"] in ("approved", "rejected", "expired"):
            t["status"] = "running"
            task_step(t, "approval_" + a["status"], approval_id=aid)
            await task_save(t)
            return a["status"] == "approved"
        if (await state.call("GET", task_key(t["id"])) or {}).get("cancel_requested"):
            raise asyncio.CancelledError
        await asyncio.sleep(TASK_APPROVAL_POLL_S)


TASK_APPROVAL_POLL_S = 3


async def run_task(t):
    try:
        async with task_slots:
            if t["cancel_requested"]:
                raise asyncio.CancelledError
            t.update(status="running", started=time.time())
            note_requester(task_id=t["id"], persona=t["persona"])
            await task_save(t)
            deadline = time.monotonic() + t["max_minutes"] * 60
            session, prompt = None, None
            for _ in range(TASK_MAX_ROUNDS):
                session, pending = await task_round(t, prompt, session, deadline)
                if not pending:
                    break
                waited = time.monotonic()
                ok = await wait_approval(t, pending)
                deadline += time.monotonic() - waited
                prompt = (f"승인됐어 (approval_id={pending}). 같은 명령을 approval_id와 함께 다시 실행하고 이어서 해." if ok else
                          "사용자가 그 명령을 거절했어. 그 명령 없이 할 수 있는 만큼 하고 결과를 요약해.")
            t.update(status="done", finished=time.time())
    except asyncio.CancelledError:
        t.update(status="cancelled", finished=time.time(), error="취소됐어요.")
    except TaskLimit as e:
        t.update(status="failed", finished=time.time(), error=e.message)
        task_step(t, "killed", reason=e.kind)
    except Exception as e:  # keep the gateway alive; the task records why
        print("task failed:", t["id"], type(e).__name__, e, flush=True)
        t.update(status="failed", finished=time.time(), error="작업이 실패했어요.")
    finally:
        task_runs.pop(t["id"], None)
        try:
            await task_save(t)
            await record_usage({"model": "task", "path": "task", "task": "task", "input_tokens": 0,
                                "output_tokens": 0, "cost_usd": round(t["cost_usd"], 6)})
        except StateDown:
            print("task not saved:", t["id"], flush=True)
        if t["status"] != "cancelled":
            await task_notify(t)


def task_summary(t):
    if t["status"] == "waiting_approval":
        return "맡기신 작업에 승인이 필요해요. 앱에서 확인해 주세요."
    if t["status"] == "done":
        return f"맡기신 작업 끝났어요. {t['result'] or '결과는 앱에서 확인해 주세요.'}"[:600]
    return f"맡기신 작업이 끝나지 못했어요. {t['error'] or ''}"[:600]


async def task_notify(t, approval=False):
    """When a task finishes, fails or needs an approval: a call as its persona, or an alert push the app opens
    as azel://task/<id>."""
    try:
        if t["notify"] == "call":
            await place_call("task_approval" if approval else "task", opening=task_summary(t), persona_id=t["persona"])
        elif t["notify"] == "push":
            title = "승인 필요" if approval else "작업 완료" if t["status"] == "done" else "작업 실패"
            await send_alert_push(title, task_summary(t)[:180], type="task", task_id=t["id"])
    except Exception as e:
        print("task notify failed:", t["id"], type(e).__name__, flush=True)


async def fail_orphan_tasks():
    """At start: this gateway's tasks that were running when it stopped can never finish."""
    for tid in await state.call("GET", "/v1/state/kv/tasks-index") or []:
        t = await state.call("GET", task_key(tid))
        if t and t.get("owner") == DEVICE and t["status"] in ("queued", "running", "waiting_approval"):
            t.update(status="failed", finished=time.time(), error="게이트웨이가 다시 시작돼서 멈췄어요.")
            await task_save(t)


def task_view(t, steps=True):
    keep = ("id", "goal", "status", "device", "persona", "notify", "budget_usd", "max_minutes", "created", "started",
            "finished", "result", "error", "cost_usd", "owner")
    return {**{k: t.get(k) for k in keep}, **({"steps": t["steps"]} if steps else {})}


@app.post("/v1/tasks", status_code=201)
async def tasks_post(body: dict):
    return task_view(await task_create(body))


@app.get("/v1/tasks")
async def tasks_list(limit: int = 20):
    full_only()
    out = []
    for tid in (await state.call("GET", "/v1/state/kv/tasks-index") or [])[:max(1, min(limit, 100))]:
        if t := await state.call("GET", task_key(tid)):
            out.append(task_view(t, steps=False))
    return out


@app.get("/v1/tasks/{tid}")
async def tasks_get(tid: str):
    full_only()
    t = await state.call("GET", task_key(quote(tid, safe=""))) if re.fullmatch(r"[0-9a-f]{12}", tid) else None
    if not t:
        raise HTTPException(404, "no such task")
    return task_view(t)


@app.post("/v1/tasks/{tid}/cancel")
async def tasks_cancel(tid: str):
    full_only()
    t = await state.call("GET", task_key(tid)) if re.fullmatch(r"[0-9a-f]{12}", tid) else None
    if not t:
        raise HTTPException(404, "no such task")
    if t["status"] in ("done", "failed", "cancelled"):
        raise HTTPException(409, f"task already {t['status']}")
    if run := task_runs.get(tid):  # ours: stop it now (the claude process is killed on the way out)
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
    else:  # another gateway's: it sees the flag within seconds
        t["cancel_requested"] = True
        await task_save(t)
    return task_view(await state.call("GET", task_key(tid)) or t, steps=False)


DEVICE_WORD = {"macbook": "맥북", "albireo": "알비레오", "gienah": "기에나", "deneb": "데네브", "spark": "스파크"}


async def serve_task(m):
    """A task request from the voice server ("…해 줘, 끝나면 알려 줘"): start it, answer what to say."""
    reply = {"id": m["id"], "type": "task_result", "ok": True}
    try:
        current_persona.set(m.get("persona") if isinstance(m.get("persona"), str) else None)
        t = await task_create({k: m[k] for k in ("goal", "device", "persona", "notify") if m.get(k) is not None},
                              source="voice")
        settings = await load_settings()
        dev = t["device"] or settings["default_device"]
        then = {"call": "끝나면 전화드릴게요.", "push": "끝나면 알림 드릴게요.", "none": "앱에서 진행 상황을 볼 수 있어요."}
        reply.update(task_id=t["id"], say=f"{DEVICE_WORD.get(dev, dev)}에서 시작할게요. {then[t['notify']]}")
    except (HTTPException, StateDown) as e:
        print("voice task refused:", getattr(e, "detail", e), flush=True)
        reply.update(ok=False, say="지금은 작업을 맡을 수 없어요.")
    await link_send(reply)


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
