"""Spark voice server: STT -> router (Qwen or Claude via gateway) -> Qwen3-TTS per sentence.

Contract: ../PROTOCOL.md sections 1-2.
  WS   /v1/call    live call: server-side VAD, barge-in, optional call opening
  POST /v1/turn    one WAV turn -> NDJSON (benchmark)
  POST /v1/audio/transcriptions   OpenAI-compatible STT
  GET  /health     (no auth)
"""
import asyncio
import base64
import contextvars
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import soundfile as sf
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

LLM_URL = os.environ.get("LLM_URL", "http://100.64.0.11:8888/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "Qwen3.8-Flash-Next")
TTS_URL = os.environ.get("TTS_URL", "http://100.64.0.11:8890/v1/audio/speech")
TTS_VOICE = os.environ.get("TTS_VOICE", "sohee")
STT_MODEL = os.environ.get("STT_MODEL", "openai/whisper-large-v3-turbo")
# Only SHA-256 digests of accepted tokens live on this shared team machine, never a token itself.
# One "<label> <hex digest>" per line: client (apps, /v1/call and /v1/turn), link (full gateways),
# link_limited (team-device gateways: always limited, whatever their hello says).
HASH_FILE = os.environ.get("TOKEN_HASH_FILE", "/run/secrets/biseo_token_hashes")
LABELS = ("client", "link", "link_limited")
DEFAULT_NAME = os.environ.get("ASSISTANT_NAME", "아젤")  # until the gateways push settings with assistant_name
MAX_TURNS = int(os.environ.get("MAX_TURNS", "12"))
END_SILENCE_MS = int(os.environ.get("END_SILENCE_MS", "500"))
KST = timezone(timedelta(hours=9))
MAX_UPLOAD = 16000 * 2 * 60  # one minute of 16 kHz PCM16
ROUTE_TAG = "[CLAUDE]"

# reply_length (shared setting, the app exposes it): how long answers run. The short first clause stays for latency.
LENGTH_STYLE = {
    "brief": "짧게 한두 문장으로 요점만 말해.",
    "normal": "질문에 맞는 길이로 말해. 잡담과 간단한 답은 한두 문장, 설명·요약·일정·상태 보고는 네다섯 문장 정도로 자연스럽게.",
    "detailed": "질문에 맞는 길이로 말해. 잡담은 짧게, 설명·요약·일정·상태 보고는 여섯에서 여덟 문장까지 자세히, 말하듯 자연스럽게.",
}
LENGTH_TOKENS = {"brief": 250, "normal": 600, "detailed": 1000}  # Qwen max_tokens per reply_length
SYSTEM_BODY = (
    "지금은 음성 통화 중이라 답이 그대로 소리로 읽혀. "
    "한국어 존댓말로 자연스럽게 말해. 첫 마디는 열 글자 안팎으로 짧게 하되 내용으로 시작해(예: '오늘 일정은 두 개예요,'). "
    "'네'나 '알겠습니다'만 하고 끝내지 마. "
    "마크다운, 목록, 이모지, 괄호 설명, URL은 쓰지 마. 숫자와 단위는 읽기 쉽게 말해. "
    f"중요: 답하려면 실제 데이터나 도구가 필요하면(일정, 할 일, 과제, 수업, 메일, 서버 상태, 예전에 한 말이나 기억, "
    f"파일, macbook이나 서버 작업, 코드, 검색, 날씨 같은 최신 정보, 여러 단계 작업) 다른 말 없이 정확히 {ROUTE_TAG} 만 출력해. "
    "잡담, 인사, 간단한 상식, 계산, 방금 대화 내용 정리는 네가 바로 답해."
)


def assistant_name(settings):
    return settings.get("assistant_name") or DEFAULT_NAME


def reply_length(settings):
    return settings.get("reply_length") if settings.get("reply_length") in LENGTH_STYLE else "normal"


# ---------- personas (PROTOCOL.md section 3): the call's persona sets name, character and voice ----------
CALL_PERSONA: contextvars.ContextVar = contextvars.ContextVar("call_persona", default=None)  # set per /v1/call


def personas_of(settings):
    """Live personas from the shared settings; a gateway from before personas gives the one assistant."""
    ps = [p for p in settings.get("personas") or [] if isinstance(p, dict) and p.get("id") and not p.get("deleted_at")]
    return ps or [{"id": "azel", "name": assistant_name(settings), "voice": TTS_VOICE, "voice_style": "",
                   "personality": settings.get("personality") or "", "primary": True}]


def pick_persona(settings, pid=None):
    ps = personas_of(settings)
    return next((p for p in ps if p["id"] == pid), None) or next((p for p in ps if p.get("primary")), ps[0])


def persona_text(p, settings):
    """A persona's character ("" = the default the gateway sends as personality_default)."""
    t = p.get("personality")
    return t.strip() if isinstance(t, str) and t.strip() else str(settings.get("personality_default") or "")


def current_persona(settings):
    return CALL_PERSONA.get() or pick_persona(settings)


def personality(settings):
    """Azel's character from the shared settings ("" = the default the gateway sends as personality_default)."""
    p = settings.get("personality")
    return p.strip() if isinstance(p, str) and p.strip() else str(settings.get("personality_default") or "")


def system_prompt(name, length="normal", context="", persona=""):
    """Qwen's system prompt for this turn, built from the shared settings (name, reply_length), so changes apply
    at once. `context`: the brain's block, appended last; with it, Qwen answers personal questions from the context
    or its tools (BRAIN_BODY) instead of always handing them to Claude. Stable parts come first, so the LLM
    endpoint's prefix cache covers them; callers append the clock and per-turn memory hits after this."""
    body = SYSTEM_BODY if not context else SYSTEM_BODY.split("중요:")[0] + BRAIN_BODY
    return f"너는 사용자의 개인 비서 '{name}'야. " + (f"성격: {persona} " if persona else "") + body + " " \
        + LENGTH_STYLE.get(length, LENGTH_STYLE["normal"]) \
        + (f"\n\n참고할 상황:\n{context}" if context else "")


def strip_vocative(text, name):
    """'아젤아, 서버 괜찮아?' -> '서버 괜찮아?': calling the assistant by name says nothing about the route."""
    return re.sub(rf"^\s*{re.escape(name)}\s*(아|야|씨|님)?[\s,!.~]*", "", text) or text
FILLER = "잠시만요, 확인해 볼게요."
FILLER_LONG = "조금만 더 기다려 주세요."
FILLER_AFTER_S = float(os.environ.get("FILLER_AFTER_MS", "1200")) / 1000  # say FILLER only if Claude is this slow
NUDGE_AFTER_S = 9.0  # then FILLER_LONG once, counted from the start of the Claude turn
# Whisper's classic outputs on silence or noise; never treat them as the user speaking.
HALLUCINATIONS = {"시청해 주셔서 감사합니다", "시청해주셔서 감사합니다", "구독과 좋아요 부탁드립니다", "감사합니다", "MBC 뉴스 이덕영입니다"}
# A model name counts only as a request to use it ("오퍼스로", "소넷한테"), not as a topic ("하이쿠가 뭐야").
_USE = r"\s*(으?로|한테|에게|모델)"
MODEL_WORDS = {"opus": rf"(오퍼스|opus){_USE}", "sonnet": rf"(소넷|sonnet){_USE}", "haiku": rf"(하이쿠|haiku){_USE}",
               "nim": rf"(엔비디아|nvidia|\bnim){_USE}"}  # NVIDIA NIM on a full gateway (gateway/nim.py)
USE_CLAUDE = re.compile(rf"(클로드|claude){_USE}|(클로드|claude)(가|이)\s*(해|답|풀|찾|봐|알아)", re.I)
# Needs live data or a device: straight to Claude without asking Qwen (Qwen would guess).
NEEDS_CLAUDE = re.compile(r"날씨|비 ?와|눈 ?와|뉴스|환율|주가|시세|검색|찾아(서|줘|봐)|맛집|서버|디스크|GPU|메모리 사용"
                          r"|spark|ruchba|albireo|gienah|deneb|macbook"
                          r"|스파크|루크바|알비레오|기에나|데네브", re.I)  # Korean: how Whisper spells the spoken names
# Personal data only full-mode gateways may touch (same list as the gateway's PERSONAL).
PERSONAL = re.compile(r"일정(?!한)|캘린더|스케줄|회의|약속|메일|편지함|기억|메모리|연락처|전화번호|과제|수업|시험|성적|마감"
                      r"|할 ?일|브리핑|지스트라|GISTRA|플래닛|Planit|지난번|저번에|예전에 (내가|우리)", re.I)
NO_PERSONAL = "개인 비서 기능은 지금 쓸 수 없어요."
NO_GATEWAY = "지금 Claude 게이트웨이에 연결이 안 돼요."
TASK_WORDS = {"code": r"코드|레포|빌드|버그|커밋|테스트 ?돌|에러|함수 ?짜|스크립트", "mac": r"macbook|맥북|파일|폴더|터미널|바탕화면|다운로드",
              "schedule": r"일정(?!한)|할 ?일|과제|캘린더|회의|수업|마감|알람|브리핑|플래닛|planit"}

def load_hashes(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {k: [] for k in LABELS}
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        label, digest = line.split()
        assert label in LABELS and re.fullmatch(r"[0-9a-f]{64}", digest), f"bad token hash line for {label!r}"
        out[label].append(digest)
    assert out["client"] and out["link"], "token hash file needs client and link entries"
    return out


class RedactQuery(logging.Filter):
    """Strip query strings (which may carry ?token=) from uvicorn's log lines, whatever the log level."""
    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = redact_query(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_query(a) if isinstance(a, str) else a for a in record.args)
        return True


def redact_query(text):
    return re.sub(r"\?[^\s\"']*", "?<redacted>", text)


for _name in ("uvicorn.error", "uvicorn.access"):
    logging.getLogger(_name).addFilter(RedactQuery())

CHECK_ONLY = "--check" in sys.argv
if not CHECK_ONLY:
    import torch
    from pysilero_vad import SileroVoiceActivityDetector
    from transformers import pipeline
    asr = pipeline("automatic-speech-recognition", model=STT_MODEL, torch_dtype=torch.float16, device="cuda:0")
    HASHES = load_hashes(open(HASH_FILE).read())
asr_lock = asyncio.Lock()
history: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_TURNS * 2))
client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5))


class Link:
    """One gateway connection. Gateways dial in here (Spark may not open connections to jh hosts, and
    personal Claude credentials stay off this shared team machine)."""

    def __init__(self, ws, limited=False):
        self.ws, self.lock, self.locked = ws, asyncio.Lock(), limited  # locked: a link_limited token
        self.device, self.priority = "?", 50  # until hello (older gateways never send one)
        self.mode = "limited" if limited else "full"
        self.fails = 0  # consecutive failed asks; 3 or more = unhealthy, tried last
        self.claude = True  # False: a gateway without Claude (scheduled calls, history, settings only)
        self.brain = False  # the resident brain: sends contexts, serves Qwen's tools, never takes asks or logs
        self.tools: tuple = ()  # the tools a brain link serves (its hello), e.g. read-only Cygnus tools

    async def send(self, obj):
        async with self.lock:
            await self.ws.send_text(json.dumps(obj, ensure_ascii=False))


class GatewayLinks:
    """Every connected gateway at once. Asks go to the healthiest full-mode link by priority (lower first);
    with no full link, limited links take only non-personal asks. Settings and call openings may come
    from any link (the gateways share state)."""

    def __init__(self):
        self.links: list[Link] = []
        self.queues: dict[str, tuple[asyncio.Queue, Link]] = {}
        self.settings: dict = {}
        self.openings: dict[str, str] = {}
        self.persona_ctx: dict[str, tuple[float, str]] = {}  # (received at, block) per persona ("" = the primary)
        self.session_ctx: dict[str, tuple[float, str]] = {}  # per call session (e.g. why the assistant called)

    def add(self, ws, limited=False):
        lk = Link(ws, limited)
        self.links.append(lk)
        return lk

    def remove(self, lk):
        if lk in self.links:
            self.links.remove(lk)
        for q, owner in self.queues.values():
            if owner is lk:
                q.put_nowait({"type": "error", "message": "게이트웨이 연결이 끊겼어요."})

    async def log(self, event):
        """A call_log event for the call history. Only to a full-mode link (history is personal); dropped when
        none is up. This shared machine never keeps history itself."""
        await self.notify({"type": "call_log", **event})

    async def notify(self, msg):
        """A one-way message for a full-mode link (any, Claude or not); dropped when none is up."""
        try:
            await self.pick(personal=True, need_claude=False).send(msg)
        except Exception as e:
            print(msg.get("type"), "dropped:", msg.get("event", ""), type(e).__name__, flush=True)

    async def request(self, msg, timeout=10):
        """Ask a full-mode link and wait for its one reply (e.g. a schedule request)."""
        lk, rid, q = self.pick(personal=True, need_claude=False), uuid.uuid4().hex, asyncio.Queue()
        self.queues[rid] = (q, lk)
        try:
            await lk.send({**msg, "id": rid})
            return await asyncio.wait_for(q.get(), timeout)
        finally:
            self.queues.pop(rid, None)

    def pick(self, personal: bool, need_claude: bool = True) -> Link:
        """The link for one ask; RuntimeError carries a speakable reason when none may take it."""
        ranked = sorted((lk for lk in self.links if (lk.claude or not need_claude) and not lk.brain),
                        key=lambda lk: (lk.fails >= 3, lk.priority))
        full = [lk for lk in ranked if lk.mode == "full"]
        if full:
            return full[0]
        if personal:
            raise RuntimeError(NO_PERSONAL)
        limited = [lk for lk in ranked if lk.mode == "limited"]
        if limited:
            return limited[0]
        raise RuntimeError(NO_GATEWAY)

    def receive(self, lk, msg):
        """One frame from a gateway. Malformed frames are ignored, never allowed to drop the link."""
        if not isinstance(msg, dict):
            return
        t = msg.get("type")
        if t == "hello":
            device = str(msg.get("device", "?"))
            mode = "limited" if lk.locked or msg.get("mode") == "limited" else "full"  # limited tokens stay limited
            same = [o for o in self.links if o is not lk and o.device == device]
            if any(o.mode != mode for o in same):
                return  # never let a link of the other mode evict (or pose as) that device's link
            lk.device, lk.mode = device, mode
            lk.claude = msg.get("claude") is not False
            lk.brain = msg.get("role") == "brain" and mode == "full"
            tools = msg.get("tools") if lk.brain and isinstance(msg.get("tools"), list) else []
            lk.tools = tuple(t for t in tools[:40] if isinstance(t, str) and TOOL_NAME.fullmatch(t))
            try:
                lk.priority = int(msg.get("priority", 50))
            except (TypeError, ValueError):
                lk.priority = 50
            for old in same:
                self.remove(old)  # that gateway reconnected; its old socket is dead
        elif t == "context":
            if lk.brain and isinstance(msg.get("value"), str):
                self.take_context(msg.get("session"), msg["value"], msg.get("persona"))
        elif t in ("settings", "call") and lk.mode != "full":
            return  # shared settings and call openings come from full gateways only
        elif t == "settings":
            self.settings = msg.get("value") if isinstance(msg.get("value"), dict) else {}
        elif t == "call":
            if not isinstance(msg.get("call_id"), str) or not msg["call_id"] \
                    or not isinstance(msg.get("opening", ""), str):
                return  # malformed: ignore it rather than drop the link
            if len(self.openings) > 100:
                old = self.openings.pop(next(iter(self.openings)))
                if old["audio"]:
                    old["audio"].cancel()
            self.openings[msg["call_id"]] = prepare_opening(msg.get("opening", ""), str(msg.get("reason", "")),
                                                            str(msg.get("persona") or ""))
        elif isinstance(msg.get("id"), str) and isinstance(t, str) and msg["id"] in self.queues:
            self.queues[msg["id"]][0].put_nowait(msg)


links = GatewayLinks()


# ---------- brain: context and local tools for Qwen (PROTOCOL.md section 6) ----------
BRAIN_FRESH_S = 900  # an older context block is ignored (the brain pushes one at least every 5 min while it changes)
RECALL_WAIT_S = 0.4  # per-turn memory retrieval may delay the answer this much at most
TOOL_WAIT_S = 3.0  # a slow brain never stalls a turn
TOOL_TAG = "[TOOL]"
MEMORY_CUE = re.compile(r"아까|지난번|저번에|예전에|전에 (내가|우리|네가)|했었|했던|라고 했|물어봤")
BRAIN_TOOLS = ("search_memory", "add_todo", "done_todo", "device_status", "situation")  # a brain without a tool list
TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
# With the brain's read-only server tools, a status question stays local; doing something on a server does not.
WEB_LIVE = re.compile(r"날씨|비 ?와|눈 ?와|뉴스|환율|주가|시세|검색|찾아(서|줘|봐)|맛집")
STATUS_Q = re.compile(r"괜찮|상태|살아 ?있|켜져|꺼졌|다운|죽었|문제|경보|알림|이상|느려|디스크|용량|온도|메모리")
DEVICE_Q = re.compile(r"(맥북|macbook|기기|노트북|컴퓨터).{0,6}(켜져|꺼져|꺼졌|연결|살아|온라인|오프라인)", re.I)
SERVER_ACTION = re.compile(r"재시작|재부팅|리부트|고쳐|켜 ?줘|꺼 ?줘|지워|삭제|정리해|로그|배포|실행|돌려|설치")


def _take_context(self, session, value, persona=None):
    value = value[:6000]
    if isinstance(session, str) and session:
        if len(self.session_ctx) > 200:
            self.session_ctx.pop(next(iter(self.session_ctx)))
        self.session_ctx[session] = (time.time(), value)
    else:  # a persona's shared block; frames without `persona` are the primary's (as before personas)
        self.persona_ctx[persona if isinstance(persona, str) and persona else ""] = (time.time(), value)


def _context_for(self, session, persona=None):
    """The freshest block for this call ('' when no brain is up): the session's own, else the persona's."""
    if not self.brain_link():
        return ""
    pid = persona or (CALL_PERSONA.get() or {}).get("id") or ""
    shared = self.persona_ctx.get(pid) or (self.persona_ctx.get("") if (CALL_PERSONA.get() or {}).get("primary", True)
                                           else None)
    got = [c for c in (self.session_ctx.get(session), shared) if c and time.time() - c[0] < BRAIN_FRESH_S]
    return max(got)[1] if got else ""


def _brain_tools(self):
    """What Qwen may call through the brain: the tools its hello listed (the brain re-checks each call)."""
    lk = self.brain_link()
    return set(lk.tools or BRAIN_TOOLS) if lk else set()


def _brain_link(self):
    return next((lk for lk in reversed(self.links) if lk.brain), None)


async def _brain_request(self, msg, timeout):
    """One request to the brain and its one reply; RuntimeError when no brain is up."""
    lk = self.brain_link()
    if lk is None:
        raise RuntimeError("no brain")
    rid, q = uuid.uuid4().hex, asyncio.Queue()
    self.queues[rid] = (q, lk)
    try:
        await lk.send({**msg, "id": rid})
        return await asyncio.wait_for(q.get(), timeout)
    finally:
        self.queues.pop(rid, None)


async def _brain_event(self, msg):
    if lk := self.brain_link():
        try:
            await lk.send(msg)
        except Exception:
            pass


GatewayLinks.take_context, GatewayLinks.context_for, GatewayLinks.brain_link = _take_context, _context_for, _brain_link
GatewayLinks.brain_tools = _brain_tools
GatewayLinks.brain_request, GatewayLinks.brain_event = _brain_request, _brain_event

BRAIN_BODY = (
    "아래 '참고할 상황'은 사용자 프로필, 지금 상황(일정, 마감, 할 일, 예약된 전화, 기기), 최근 통화 요약이야. "
    "여기에 답이 있으면 Claude 없이 네가 바로 답해. 없는 내용은 절대 지어내지 마. "
    f"더 찾아봐야 하면 다른 말 없이 한 줄로 {TOOL_TAG} {{\"name\": 도구, \"args\": {{…}}}} 만 출력해. 도구: "
    "search_memory(query) 예전 통화와 메모 검색, add_todo(text) 할 일 메모, done_todo(text) 메모한 할 일 지우기, "
    "device_status() 기기 연결 상태, situation() 최신 상황, scheduled_calls() 예약된 전화 목록. "
    "참고할 상황에 'Cygnus 도구' 목록이 있으면 일정, 과제, 할 일, 서버 상태는 그 도구로 확인해서 답해. "
    "참고할 상황과 도구로 안 되는 일(일정 정보도 일정 도구도 없을 때, 메일 내용, 일정 등록이나 수정, 웹 검색, 날씨와 뉴스 같은 "
    f"최신 정보, 서버 재시작 같은 작업, 파일이나 코드 작업, 여러 단계 작업)이면 다른 말 없이 정확히 {ROUTE_TAG} 만 출력해. "
    "잡담, 인사, 상식, 계산, 방금 대화 정리는 바로 답해."
)


def memory_hits(hits):
    return ("\n[이번 말과 관련된 기록]\n" + "\n".join(f"- {h}" for h in hits)) if hits else ""


async def run_tool(raw, session):
    """Run one '[TOOL] {...}' line from Qwen: brain tools over the link, scheduled calls via a gateway."""
    call = raw.strip().removeprefix(TOOL_TAG).strip()
    try:
        data = json.loads(call[call.index("{"):call.rindex("}") + 1])
        name, args = str(data.get("name", "")), data.get("args") if isinstance(data.get("args"), dict) else {}
    except ValueError:
        return "도구 호출 형식이 틀렸어요."
    if name == "scheduled_calls":
        return await schedule_reply("", "list", session)
    if name not in links.brain_tools():
        return f"없는 도구예요: {name[:40]}"
    try:
        reply = await links.brain_request({"type": "tool", "name": name, "args": args,
                                           "persona": (CALL_PERSONA.get() or {}).get("id")}, TOOL_WAIT_S)
        return str(reply.get("result", ""))[:2000]
    except (RuntimeError, asyncio.TimeoutError):
        return "지금은 그 도구를 쓸 수 없어요."


async def recall(text, timings):
    """Up to two memory hits for this utterance, or none if the brain cannot answer within RECALL_WAIT_S."""
    t = time.perf_counter()
    try:
        reply = await links.brain_request({"type": "recall", "text": text[:300],
                                           "persona": (CALL_PERSONA.get() or {}).get("id")}, RECALL_WAIT_S)
        hits = [str(h)[:300] for h in reply.get("hits", [])][:2] if isinstance(reply.get("hits"), list) else []
    except (RuntimeError, asyncio.TimeoutError):
        hits = []
    timings["recall_ms"] = ms(t)
    return hits


def prepare_opening(text, reason="", persona=""):
    """Split a placed call's opening and start synthesizing it now, so the phone hears it the moment it
    connects (the push arrives seconds before the callee answers)."""
    parts = split_ready(text, final=True)[0] if text else []
    op = {"text": text, "parts": parts, "audio": None, "reason": reason, "persona": persona}
    if parts:
        try:
            op["audio"] = asyncio.get_running_loop().create_task(_synth_all(parts, persona))
        except RuntimeError:
            pass  # no event loop (self-checks): synthesize on connect instead
    return op


async def _synth_all(parts, persona=""):
    CALL_PERSONA.set(pick_persona(links.settings, persona))  # this task only: the voice the call rings with
    return [(await tts(p))[0] for p in parts]


GREETING = re.compile(r"(여보세요|여보세용|여보|네+|예|응+|어+|안녕(하세요)?|네네)[\s.!?~,]*")
GREETING_MAX_S = 1.5


def is_greeting(text, seconds):
    return seconds < GREETING_MAX_S and bool(GREETING.fullmatch(text.strip()))


class OpeningGuard:
    """Keeps a placed call's opening from being cut off by the callee's "여보세요".

    While the opening plays, speech is held instead of interrupting: the first sentence always finishes;
    a short greeting (< GREETING_MAX_S) is let go; longer speech interrupts once the first sentence is
    over. Times are time.monotonic() seconds; the call handler acts on the returned verbs."""

    def __init__(self):
        self.first_end = self.play_end = 0.0
        self.speech_at = None

    def active(self, now):
        return now < self.play_end

    def start(self, now):
        """Speech began while the assistant is talking: 'interrupt' now, or 'hold'."""
        if not self.active(now):
            return "interrupt"
        self.speech_at = now
        return "hold"

    def tick(self, now):
        """Held speech still going: 'interrupt' once it is too long for a greeting and sentence one is done."""
        if (self.speech_at is not None and self.active(now) and now >= self.first_end
                and now - self.speech_at >= GREETING_MAX_S):
            self.speech_at = None
            return "interrupt"
        return None

    def protect(self, now):
        """A placed call's opening is starting: protected until speak_opening knows when sentence one ends."""
        self.first_end = self.play_end = now + 3600

    def wait(self, now):
        """Seconds until an interrupt may act: the rest of the opening's first sentence, else 0."""
        return max(0.0, self.first_end - now) if self.active(now) else 0.0

    def end(self, now, seconds):
        """An utterance ended: 'answer' (normal turn), 'check' (maybe a greeting) or 'interrupt' then answer."""
        held, self.speech_at = self.speech_at is not None, None
        if not held:
            return "answer"
        return "check" if seconds < GREETING_MAX_S else "interrupt"

_SENT_END = re.compile(r"(?<=[.!?。…~])\s+|\n+")
# Qwen sometimes slips Chinese characters (or kana, Cyrillic, Thai) into a Korean reply; the Korean voices read
# them as garble. They are dropped before TTS, captions and history.
FOREIGN = re.compile(r"[\u2e80-\u2fdf\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u0400-\u04ff\u0e00-\u0e7f]+")
foreign_dropped = [0]  # sentences cleaned since start (route_eval reports it)


def korean_only(s: str) -> str:
    t = FOREIGN.sub("", s)
    if t == s:
        return s
    foreign_dropped[0] += 1
    t = re.sub(r"\s{2,}", " ", re.sub(r"[(（]\s*[)）]", "", t)).strip()
    return t if re.search(r"[가-힣A-Za-z0-9]", t) else ""


MIN_SYL = 5  # Qwen3-TTS garbles very short inputs ("네," came out as "쵸-"): never synthesize less alone
_LETTER = re.compile(r"[가-힣A-Za-z0-9]")


def syllables(s: str) -> int:
    return len(_LETTER.findall(s))


def split_ready(buf: str, final: bool, first: bool = False):
    """Return (chunks ready for TTS, remainder) from the streamed text buffer.

    Chunks end at a sentence end or at ", " (never mid-word or inside "3,000") and have at least MIN_SYL
    syllables: a short sentence waits for the next one. first=True: nothing has been spoken yet, so a leading
    clause ending in a comma goes out alone if it is long enough. TTS time grows with length, so this starts
    the voice soonest. final=True: everything goes; a short tail joins the chunk before it.
    """
    if first and not final:
        m = re.match(r"\s*([^,.!?\n]{1,20},)\s", buf)
        if m and syllables(korean_only(m.group(1))) >= MIN_SYL:
            return [korean_only(m.group(1).strip())], buf[m.end():]
    parts = _SENT_END.split(buf)
    rest = "" if final else parts.pop()  # unstripped: "에너지를 " + "이용" must keep its space
    parts = [p.strip() for p in parts]
    out, cur = [], ""
    for p in parts:
        p = korean_only(p)
        if not p:
            continue
        cur = f"{cur} {p}".strip()
        if syllables(cur) >= MIN_SYL:
            out.append(cur)
            cur = ""
    if cur and final:
        if out and syllables(cur) < MIN_SYL:
            out[-1] += " " + cur
        else:
            out.append(cur)
    elif cur:
        rest = f"{cur} {rest}"
    if not final and len(rest) > 80:  # flush an over-long clause at its last ", "
        cuts = [m.end() for m in re.finditer(r",\s", rest)]
        if cuts and syllables(rest[:cuts[-1]]) >= MIN_SYL:
            out.append(korean_only(rest[:cuts[-1]].strip()))
            rest = rest[cuts[-1]:]
    return [c for c in out if c], rest


def pick(table, text):
    return next((k for k, pat in table.items() if re.search(pat, text, re.I)), None)


def pre_route(text, settings, local=False, tools=()):
    """(straight to Claude?, model) decided by keywords and settings, before any LLM call.

    local=True (the brain's context is in Qwen's prompt): personal questions go to Qwen first, which answers from
    the context or its tools, or hands over with the routing tag."""
    forced_model = pick(MODEL_WORDS, text)
    default = settings.get("default_model", "auto")
    model = forced_model or (default if default in MODEL_WORDS else None)
    if local:  # "아까 날씨 물어봤을 때 뭐라 했지?" is memory, not live data: Qwen decides
        # "서버 괜찮아?", "스파크는?" (a short follow-up), "맥북 켜져 있어?": a status question the brain's tools
        # answer (infra_health for servers, device_status for devices). Doing something there still goes to Claude.
        status = (STATUS_Q.search(text) or len(text.strip()) <= 10) and not SERVER_ACTION.search(text) \
            and not WEB_LIVE.search(text)
        server_status = status and "infra_health" in tools
        device_status = status and "device_status" in tools and DEVICE_Q.search(text)
        live = NEEDS_CLAUDE.search(text) and not MEMORY_CUE.search(text) and not server_status and not device_status
        task = pick({k: v for k, v in TASK_WORDS.items() if k != "schedule"}, text)
        keywords = USE_CLAUDE.search(text) or live or (task and not device_status)
    else:
        keywords = USE_CLAUDE.search(text) or PERSONAL.search(text) or NEEDS_CLAUDE.search(text) or pick(TASK_WORDS, text)
    return bool(forced_model) or default in MODEL_WORDS or bool(keywords), model


# A reply with no content: acknowledgements and promises only ("네.", "알겠습니다", "네, 확인해 볼게요").
ACK_WORDS = re.compile(r"네네|넵|네|예|응|알겠습니다|알겠어요|알았어요|좋아요|그럼요|물론이죠|맞아요|그렇죠|그래요|"
                       r"(확인|찾아|알아|검색)(해\s*)?(볼게요|드릴게요)|잠시만요|기다려\s*주세요")
TRIVIAL = re.compile(r"(안녕\S*|고마워\S*|고맙\S*|감사\S*|응|어|네|예|그래|그래요|알겠어|알았어|좋아|오케이|ok|"
                     r"잘\s*자|수고\S*|아니\S*|맞아\S*|됐어|괜찮아\S*|오키)[\s.!~?,]*", re.I)
NUDGE = ("방금 대답에 내용이 없었어. 질문에 실제 내용으로 바로 답해. "
         f"직접 답할 수 없으면 다른 말 없이 정확히 {ROUTE_TAG} 만 출력해.")


def content_free(reply):
    """True when nothing is left once acknowledgements, promises and punctuation are removed."""
    rest = ACK_WORDS.sub("", reply)
    return not re.search(r"[가-힣A-Za-z0-9]", rest)


def needs_answer(text):
    """A question or request (not a greeting, thanks or yes/no confirmation): a bare "네" is no answer."""
    return not TRIVIAL.fullmatch(text.strip())


def hold_tag_prefix(buf):
    """(speakable text, held tail): a tail that could still grow into the routing tag is never spoken early."""
    at = buf.rfind("[")
    tail = buf[at:].rstrip()
    if at != -1 and (ROUTE_TAG.startswith(tail) or TOOL_TAG.startswith(tail)) and len(buf) - at < len(ROUTE_TAG) + 2:
        return buf[:at], buf[at:]
    return buf, ""


def tag_state(head):
    """Qwen's reply so far (left-stripped): 'tag' = route to Claude, 'tool' = a tool call (collect it whole),
    'maybe' = too short to tell, 'no' = speak it."""
    if head.startswith(ROUTE_TAG):
        return "tag"
    if head.startswith(TOOL_TAG):
        return "tool"
    return "maybe" if ROUTE_TAG.startswith(head) or TOOL_TAG.startswith(head) else "no"


def token_label(got: str, labels=("client",)):
    """Which of `labels` this token belongs to (None if none), comparing digests in constant time."""
    digest, hit = hashlib.sha256(got.encode()).hexdigest(), None
    for label in labels:
        for h in HASHES[label]:
            if hmac.compare_digest(digest, h):
                hit = label
    return hit


def check_token(got: str, labels=("client",)):
    label = token_label(got, labels)
    if label is None:
        raise HTTPException(401, "bad token")
    return label


def bearer(headers) -> str:
    return headers.get("authorization", "").removeprefix("Bearer ").strip()


def auth(request: Request):
    check_token(bearer(request.headers))


def call_token(headers, query_token: str) -> str:
    """The client token for /v1/call: the Authorization header, else the legacy ?token= query."""
    return bearer(headers) or query_token


TAILNET = ipaddress.ip_network("100.64.0.0/10")
PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip")


def link_peer_ok(host: str, headers) -> bool:
    """/v1/gateway-link is tailnet-only and never proxied. Fail closed: a non-tailnet peer, a non-IP peer,
    or any proxy header (the public proxy on deneb is itself a tailnet peer) is refused."""
    if any(h in headers for h in PROXY_HEADERS):
        return False
    try:
        return ipaddress.ip_address(host) in TAILNET
    except ValueError:
        return False


def decode_wav(data: bytes) -> np.ndarray:
    if not data or len(data) > MAX_UPLOAD + 1024:
        raise HTTPException(400, "audio missing or longer than 60 s")
    try:
        wav, sr = sf.read(io.BytesIO(data), dtype="float32")
    except Exception:
        raise HTTPException(400, "body must be a WAV file")
    if wav.ndim > 1:
        wav = wav.mean(axis=1)
    if sr != 16000:
        raise HTTPException(400, "audio must be 16 kHz")
    return wav


async def transcribe(wav: np.ndarray) -> str:
    async with asr_lock:  # one GPU decode at a time
        out = await asyncio.to_thread(asr, {"raw": wav, "sampling_rate": 16000},
                                      generate_kwargs={"language": "korean", "task": "transcribe"})
    text = out["text"].strip()
    return "" if text.rstrip(".!") in HALLUCINATIONS else text


async def llm_stream(messages, max_tokens=600):
    body = {"model": LLM_MODEL, "messages": messages, "stream": True, "max_tokens": max_tokens, "temperature": 0.7}
    async with client.stream("POST", LLM_URL, json=body) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            delta = json.loads(line[6:])["choices"][0]["delta"].get("content")
            if delta:
                yield delta


async def gateway_settings():
    return links.settings  # pushed by the gateways on connect and on every change


ROUTING = {"type": "activity", "phase": "routing", "label": "Claude에게 물어보는 중", "tool": "", "kind": "generic",
           "route": "claude"}  # only when a turn goes to Claude; a Qwen turn sends no activity at all
KINDS = {"calendar", "mail", "memory", "schedule", "server", "files", "web", "terminal", "notes", "generic"}


def client_activity(ev):
    """The app-facing activity event: phase, label, tool name and kind only (the gateway never sends arguments)."""
    kind = ev.get("kind") if ev.get("kind") in KINDS else "generic"
    return {"type": "activity", "phase": str(ev.get("phase", "")), "label": str(ev.get("label", "")),
            "tool": str(ev.get("tool", "")), "kind": kind, "route": "claude"}


async def claude_stream(text, session, model, task, personal=False, context="", timeout=180, on_event=None):
    """Text deltas from a gateway over its link; RuntimeError carries a speakable message.

    `context` (recent turns of the call) goes only to full-mode links: earlier turns may be personal."""
    lk = links.pick(personal)
    text = (context if lk.mode == "full" else "") + text
    rid, q, finished = uuid.uuid4().hex, asyncio.Queue(), False
    links.queues[rid] = (q, lk)
    try:
        await lk.send({"type": "ask", "id": rid, "text": text, "session": session, "model": model, "task": task,
                       "persona": (CALL_PERSONA.get() or {}).get("id")})
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout)
            except asyncio.TimeoutError:
                lk.fails += 1
                raise RuntimeError("Claude 답이 너무 늦어서 끊었어요.")
            if ev["type"] == "activity":
                if on_event:
                    await on_event(client_activity(ev))
            elif ev["type"] == "text":
                yield str(ev.get("delta", ""))
            elif ev["type"] == "error":
                finished = True
                if not ev.get("code"):  # budget/personal refusals are answers, not a sick gateway
                    lk.fails += 1
                raise RuntimeError(ev.get("message", "Claude를 쓸 수 없어요."))
            elif ev["type"] == "done":
                finished, lk.fails = True, 0
                return
    finally:
        links.queues.pop(rid, None)
        if not finished and lk in links.links:  # barge-in or timeout: stop the Claude run too
            try:
                await lk.send({"type": "cancel", "id": rid})
            except Exception:
                pass


# ---------- briefing on request: WS /v1/call?intent=briefing ----------
BRIEFING_QWEN = (
    "너는 개인 비서 '{name}'야. 사용자가 오늘 브리핑을 요청했어. 아래 메모와 사실만 근거로 30~40초 분량, "
    "여섯에서 여덟 문장의 음성 브리핑을 해. '오늘 브리핑이에요.'로 시작하고 오늘 날짜를 말한 뒤 수업·회의, 마감, 할 일, "
    "서버 상태, 승인 대기, 다음 예약 전화 순으로 말하고, 없는 항목은 건너뛰어. 메모에 없는 건 지어내지 마. "
    "한국어 존댓말, 마크다운·목록·이모지 금지."
)
BRIEFING_CLAUDE = (  # the morning-briefing call's prompt, for when no situation note exists yet
    "사용자가 오늘 브리핑을 요청했어. 오늘 수업과 마감(GISTRA), 오늘 할 일(Planit), 캘린더 일정, 서버 상태(Cygnus "
    "인프라), 승인 대기를 확인해서 '오늘 브리핑이에요'로 시작하는 30초 안쪽 브리핑으로 말해."
)


async def briefing_sentences(session, name, on_event, persona=""):
    """Sentences of a spoken briefing. Fast path: the local Qwen composes it from the situation note a full
    gateway hands over (first sentence within about 2 s). No note yet: a filler, then Claude via the link."""
    try:
        ctx = await links.request({"type": "briefing_context"}, timeout=2)
    except (RuntimeError, asyncio.TimeoutError):
        ctx = {}
    if ctx.get("situation"):
        facts = "\n".join(str(f) for f in ctx.get("facts") or [])
        messages = [{"role": "system", "content": BRIEFING_QWEN.format(name=name) + (f" 성격: {persona}" if persona else "")},
                    {"role": "user", "content": f"{now_kst().strip()}\n\n메모:\n{ctx['situation']}\n\n사실:\n{facts}"}]
        stream = llm_stream(messages, 700)
    elif time.time() - last_claude_briefing[0] < BRIEFING_COOLDOWN_S and last_claude_briefing[1]:
        for s in split_ready(last_claude_briefing[1], final=True)[0]:  # asked again soon: no second Claude run
            yield s
        return
    else:
        yield FILLER
        await on_event(dict(ROUTING))
        stream = claude_stream(BRIEFING_CLAUDE, session, None, "schedule", personal=True, on_event=on_event)
    buf, first, said, failed = "", True, [], False
    try:
        async for delta in stream:
            buf += delta
            done, buf = split_ready(buf, final=False, first=first)
            for s in done:
                first = False
                said.append(s)
                yield s
    except RuntimeError as e:  # no gateway / Claude failed: say why
        buf, failed = str(e), True
    rest = split_ready(buf, final=True)[0]
    if not ctx.get("situation") and not failed:  # remember a whole Claude briefing for the cooldown
        last_claude_briefing[:] = [time.time(), " ".join(said + rest)]
    for s in rest:
        yield s


BRIEFING_COOLDOWN_S = 600  # at most one Claude-fallback briefing per 10 min; within it the last text is reused
last_claude_briefing: list = [0.0, ""]


# ---------- scheduled calls: "내일 10시에 깨워 줘" (local; the gateway stores and fires them) ----------
KO_NUM = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
          "열한": 11, "열두": 12, "삼십": 30, "십": 10, "이십": 20, "사십": 40, "오십": 50}
_N = r"(\d{1,2}|열한|열두|열|한|두|세|네|다섯|여섯|일곱|여덟|아홉|삼십|이십|사십|오십|십)"
DAYS = "월화수목금토일"
REL_RE = re.compile(_N + r"\s*(분|시간)\s*(뒤|후|있다가|지나서)")
CLOCK_RE = re.compile(r"(오전|오후|아침|저녁|밤|새벽|낮)?\s*" + _N + r"\s*시(?!간)\s*(반|" + _N + r"\s*분)?")
WEEKDAY_RE = re.compile(r"(다음\s*주|담주|이번\s*주)?\s*([월화수목금토일])요일")
REPEATS = {"daily", "weekdays"} | {f"weekly:{d}" for d in range(7)}  # weekly:<dow>, 0 = Monday
SCHED_LIST = re.compile(r"(예약|알람|모닝 ?콜|깨우는 거).{0,8}(목록|뭐|있|확인|알려|된 거|돼 있)")
SCHED_CANCEL = re.compile(r"(예약|알람|모닝 ?콜|깨우는 거|전화).{0,8}취소|취소.{0,6}(예약|알람)|^\s*취소(해\s*줘|해요|요)?[.!]?\s*$")
SCHED_WAKE = re.compile(r"깨워|모닝 ?콜|알람.{0,10}(맞춰|설정|해\s*줘|부탁)")
SCHED_CALL = re.compile(r"(전화|콜)\s*(해|걸어|주)|(예약|알람).{0,10}(해\s*줘|잡아|걸어|맞춰)")
OTHER_TARGET = re.compile(r"(\S+)(한테|에게|께)")
pending_schedule: dict[str, tuple[float, dict]] = {}  # session -> (asked at, partial request) after "몇 시에…?"


def ko_int(tok):
    return int(tok) if tok.isdigit() else KO_NUM[tok]


def schedule_intent(text, session=""):
    """'create' / 'list' / 'cancel' when the utterance is about scheduled calls, else None (normal routing)."""
    if SCHED_LIST.search(text):
        return "list"
    if SCHED_CANCEL.search(text):
        return "cancel"
    if SCHED_WAKE.search(text):
        return "create"
    target = OTHER_TARGET.search(text)
    if SCHED_CALL.search(text) and (not target or target.group(1) in ("나", "저")) and parse_when(text)[0]:
        return "create"  # "내일 3시에 전화해 줘"; not "엄마한테 전화해 줘"
    asked = pending_schedule.get(session)
    if asked and time.time() - asked[0] < 90 and parse_when(text)[0]:
        return "create"  # the answer to "몇 시에 깨워 드릴까요?"
    return None


def parse_when(text, now=None):
    """(datetime in KST or None, repeat or None) from a Korean time phrase."""
    now = now or datetime.now(KST)
    if m := REL_RE.search(text):
        n = ko_int(m.group(1))
        return (now + (timedelta(minutes=n) if m.group(2) == "분" else timedelta(hours=n))).replace(
            second=0, microsecond=0), None
    repeat = "daily" if "매일" in text else "weekdays" if re.search(r"평일|주중", text) else None
    if m := re.search(r"매주\s*([월화수목금토일])요일", text):
        repeat = f"weekly:{DAYS.index(m.group(1))}"
    clock = CLOCK_RE.search(text)
    if not clock:
        return None, repeat
    part, hour = clock.group(1), ko_int(clock.group(2))
    minute = 30 if clock.group(3) == "반" else ko_int(clock.group(4)) if clock.group(4) else 0
    if hour > 24 or minute > 59:
        return None, repeat
    if part in ("오후", "저녁", "밤", "낮") and hour < 12 and not (part == "낮" and hour >= 11):
        hour += 12
    elif part in ("오전", "아침", "새벽") and hour == 12:
        hour = 0
    hour %= 24
    day = None
    for word, add in (("글피", 3), ("모레", 2), ("내일", 1), ("오늘", 0)):
        if word in text:
            day = now.date() + timedelta(days=add)
            break
    if (m := WEEKDAY_RE.search(text)) and not repeat:
        want = DAYS.index(m.group(2))
        if m.group(1) and "다음" in m.group(1) or m.group(1) == "담주":
            day = now.date() + timedelta(days=7 - now.weekday() + want)
        else:
            day = now.date() + timedelta(days=(want - now.weekday()) % 7)
    at = datetime.combine(day or now.date(), datetime.min.time(), KST).replace(hour=hour, minute=minute)
    if day is None or repeat:  # "10시": today if still ahead, else tomorrow; repeats start at the next match
        while at <= now or (repeat == "weekdays" and at.weekday() >= 5) or \
                (repeat and repeat.startswith("weekly") and at.weekday() != int(repeat.split(":")[1])):
            at += timedelta(days=1)
    return at, repeat


def to_kst(dt):
    """A naive time (as Qwen may write it) means KST, never the container's own zone."""
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)


def weekday_matches(at, repeat):
    return not (repeat and repeat.startswith("weekly:")) or at.weekday() == int(repeat.split(":")[1])


def valid_schedule(at, repeat, now=None):
    now = now or datetime.now(KST)
    return at is not None and now + timedelta(seconds=30) < at <= now + timedelta(days=30) and \
        (repeat is None or repeat in REPEATS) and weekday_matches(at, repeat)


async def qwen_schedule(text, now):
    """Fallback for phrasings the rules miss: ask the local Qwen for strict JSON, then validate it."""
    prompt = ("사용자의 말에서 예약 전화를 뽑아 JSON 하나만 출력해. 다른 말은 쓰지 마. 형식: "
              '{"action":"create|list|cancel","at":"YYYY-MM-DDTHH:MM:00+09:00","repeat":null|"daily"|"weekdays"|'
              '"weekly:0".."weekly:6" (0=월요일),"kind":"wake|reminder","note":"…"}. 시간을 모르면 "at":null. '
              f"지금은 {now.isoformat(timespec='minutes')} (KST)야.")
    out = ""
    try:
        async for delta in llm_stream([{"role": "system", "content": prompt}, {"role": "user", "content": text}]):
            out += delta
            if len(out) > 400:
                break
        data = json.loads(out[out.index("{"):out.rindex("}") + 1])
        at = to_kst(datetime.fromisoformat(data["at"])) if data.get("at") else None
        return at, data.get("repeat"), data.get("kind") if data.get("kind") in ("wake", "reminder") else None
    except Exception:  # unparseable or invalid: the caller asks back instead of guessing
        return None, None, None


async def schedule_reply(text, action, session, now=None):
    """What to say for a schedule utterance, after a full gateway stored, listed or cancelled it."""
    now = now or datetime.now(KST)
    if action == "list":
        msg = {"type": "schedule", "action": "list"}
    elif action == "cancel":
        at, _ = parse_when(text, now)
        msg = {"type": "schedule", "action": "cancel", **({"at": at.timestamp()} if at else {})}
    else:
        prior = pending_schedule.pop(session, (0, {}))[1]
        kind = "wake" if SCHED_WAKE.search(text) else prior.get("kind", "reminder")
        at, repeat = parse_when(text, now)
        repeat = repeat or prior.get("repeat")
        if at is None:
            at, q_repeat, q_kind = await qwen_schedule(text, now)
            repeat, kind = repeat or q_repeat, q_kind or kind
        if at is None:
            pending_schedule[session] = (time.time(), {"kind": kind, "repeat": repeat})
            return "몇 시에 깨워 드릴까요?" if kind == "wake" else "몇 시에 전화드릴까요?"
        if repeat in REPEATS and not weekday_matches(at, repeat):  # e.g. Qwen said weekly:0 for a Wednesday
            pending_schedule[session] = (time.time(), {"kind": kind, "repeat": None})
            return "무슨 요일 몇 시에 할까요?"
        if not valid_schedule(at, repeat, now):
            return "지금부터 30일 안쪽의 시간으로만 예약할 수 있어요."
        note = "" if kind == "wake" else text[:100]
        msg = {"type": "schedule", "action": "create", "at": at.isoformat(), "repeat": repeat, "kind": kind,
               "note": note, "persona_id": (CALL_PERSONA.get() or {}).get("id")}
    try:
        reply = await links.request(msg)
    except (RuntimeError, asyncio.TimeoutError):
        return "지금은 예약을 받을 수 없어요."
    return str(reply.get("say") or "지금은 예약을 받을 수 없어요.")


_tts_cache: dict[str, tuple[bytes, int]] = {}
# Measured 2026-11-21 against the shared TTS server (sohee): ~0.15 s of synthesis per syllable (about 0.7x real
# time), one request at a time behind a GPU lock: two concurrent requests took longer than the same two in a row.
PLAY_TAIL_S = float(os.environ.get("PLAY_TAIL_MS", "300")) / 1000  # after the phone's playback ends, then listen
# The server slows down when the shared GPU is busy, so the rate is tracked from real calls (the slowest of the
# default and a moving average), and merging leaves a 1.5x margin.
SYNTH_S_PER_SYL = float(os.environ.get("SYNTH_MS_PER_SYL", "150")) / 1000
synth_rate = [SYNTH_S_PER_SYL]


async def tts(sentence: str) -> tuple[bytes, int]:
    p = CALL_PERSONA.get() or {}  # the call's persona: its speaker and optional style instruction
    voice, style = p.get("voice") or TTS_VOICE, p.get("voice_style") or ""
    # Fillers and short openers ("네, 알겠습니다.") repeat constantly: synthesize them once (per voice). Under
    # MIN_SYL is never cached (such a chunk is only ever a whole reply, and garbles more often).
    cacheable = sentence in (FILLER, FILLER_LONG) or MIN_SYL <= syllables(sentence) <= 8
    key = (voice, style, sentence)
    if cacheable and key in _tts_cache:
        return _tts_cache[key]
    body = {"input": sentence, "voice": voice, "response_format": "wav", **({"instructions": style} if style else {})}
    t = time.perf_counter()
    r = await client.post(TTS_URL, json=body)
    r.raise_for_status()
    if (n := syllables(sentence)) >= MIN_SYL:
        synth_rate[0] = 0.7 * synth_rate[0] + 0.3 * (time.perf_counter() - t) / n
    wav, sr = sf.read(io.BytesIO(r.content), dtype="int16")
    if cacheable and len(_tts_cache) < 500:
        _tts_cache[key] = (wav.tobytes(), sr)
    return wav.tobytes(), sr


def now_kst():
    now = datetime.now(KST)
    ampm, h12 = ("오전" if now.hour < 12 else "오후"), (now.hour % 12 or 12)  # Qwen misreads 24-hour "17시"
    return f" 현재 시각: {now:%Y년 %-m월 %-d일} {'월화수목금토일'[now.weekday()]}요일 {ampm} {h12}시 {now.minute}분."


def ms(t0):
    return round((time.perf_counter() - t0) * 1000)


def recent_context(hist, n=6):
    lines = [f"{'사용자' if m['role'] == 'user' else '비서'}: {m['content']}" for m in list(hist)[-n:]]
    return ("최근 통화 내용:\n" + "\n".join(lines) + "\n\n") if lines else ""


async def respond(text, session, t0, timings, on_event=None):
    """Yield (sentence, route, pcm, sr) for one user utterance; records history as it goes."""
    settings = await gateway_settings()
    persona = current_persona(settings)
    hkey = f"{persona['id']}:{session}"  # history and the Claude session map are per persona
    hist = history[hkey]
    name = persona["name"]
    bare = strip_vocative(text, name)
    sched = schedule_intent(bare, session)
    ctx = links.context_for(session)  # '' without a brain: the old prompt and routing
    to_claude, model = (False, None) if sched else pre_route(bare, settings, local=bool(ctx), tools=links.brain_tools() if ctx else ())
    sentences: asyncio.Queue = asyncio.Queue()
    route = {"value": "schedule" if sched else "claude" if to_claude else "qwen"}

    async def via_claude():
        route["value"] = "claude"
        if on_event:
            await on_event(dict(ROUTING))  # before any filler
        task = pick(TASK_WORDS, text) or "chat"
        buf, got_text = "", asyncio.Event()

        async def nudge():  # at most one FILLER, then one FILLER_LONG; none once Claude's text arrives
            for wait, line in ((FILLER_AFTER_S, FILLER), (NUDGE_AFTER_S - FILLER_AFTER_S, FILLER_LONG)):
                try:
                    await asyncio.wait_for(got_text.wait(), wait)
                    return
                except asyncio.TimeoutError:
                    await sentences.put(line)
        nudger = asyncio.create_task(nudge())
        try:
            personal = task == "schedule" or bool(PERSONAL.search(text))  # this utterance only, not history
            async for delta in claude_stream(text, hkey, model, task, personal, recent_context(hist),
                                             on_event=on_event):
                got_text.set()
                buf += delta
                done, buf = split_ready(buf, final=False)
                for s in done:
                    await sentences.put(s)
        except RuntimeError as e:
            buf = str(e)
        except httpx.HTTPError:
            buf = NO_GATEWAY
        finally:
            nudger.cancel()
        for s in split_ready(buf, final=True)[0]:
            await sentences.put(s)

    async def produce():
        try:
            if sched:  # handled here and by a full gateway; neither Qwen's answer nor Claude
                for s in split_ready(await schedule_reply(bare, sched, session), final=True)[0]:
                    await sentences.put(s)
                return
            if to_claude:
                return await via_claude()
            length = reply_length(settings)
            system = system_prompt(name, length, ctx, persona_text(persona, settings)) + now_kst() + (memory_hits(await recall(bare, timings)) if ctx else "")
            messages = [{"role": "system", "content": system}, *hist, {"role": "user", "content": text}]
            guard = needs_answer(bare)  # a question or request must not end with a bare "네"
            ack_held, flushed, nudged = [], False, False

            async def say(s):
                nonlocal flushed
                if guard and not flushed and content_free(" ".join(ack_held + [s])):
                    ack_held.append(s)  # "네," alone: keep it back until real content follows
                    return
                for x in ack_held + [s]:
                    await sentences.put(x)
                ack_held.clear()
                flushed = True

            for rounds in range(4):  # at most two tool calls and one nudge, then an answer
                buf, state, said_round = "", "maybe", []
                async for delta in llm_stream(messages, LENGTH_TOKENS[length]):
                    timings.setdefault("llm_first_token_ms", ms(t0))
                    buf += delta
                    if state not in ("tool", "no"):
                        state = tag_state(buf.lstrip())
                    if state in ("maybe", "tool"):
                        continue  # might be a tag; a tool call is collected whole
                    if state == "tag" or ROUTE_TAG in buf:  # also "수요일이요. [CLAUDE]": hand over, never say it
                        return await via_claude()
                    if TOOL_TAG in buf:  # "찾아볼게요. [TOOL] {…}": say the words, collect the call, never say it
                        before, _, after = buf.partition(TOOL_TAG)
                        for s in split_ready(before, final=True)[0]:
                            said_round.append(s)
                            await say(s)
                        buf, state = TOOL_TAG + after, "tool"
                        continue
                    speak, held = hold_tag_prefix(buf)  # a trailing "[CLA" might become the tag: keep it back
                    done, rest = split_ready(speak, final=False, first="first_sentence_ms" not in timings)
                    buf = rest + held
                    for s in done:
                        timings.setdefault("first_sentence_ms", ms(t0))
                        said_round.append(s)
                        await say(s)
                if state == "tool" and ctx and rounds < 2:
                    t_tool = time.perf_counter()
                    result = await run_tool(buf.strip().split("\n")[0], session)
                    timings.setdefault("tools", []).append(ms(t_tool))
                    messages += [{"role": "assistant", "content": buf.strip()},
                                 {"role": "user", "content": f"[도구 결과]\n{result}\n\n이 결과로 방금 질문에 답해."}]
                    continue
                if state == "tool" or ROUTE_TAG in buf:
                    return await via_claude()  # tools looping, or no brain, or a late tag: let Claude take it
                for s in split_ready(hold_tag_prefix(buf)[0], final=True)[0]:
                    said_round.append(s)
                    await say(s)
                if guard and not flushed:  # the whole reply was "네" / "확인해 볼게요": no answer at all
                    ack_held.clear()
                    if nudged:
                        return await via_claude()
                    nudged = True
                    messages += [{"role": "assistant", "content": " ".join(said_round) or "네"},
                                 {"role": "user", "content": NUDGE}]
                    continue
                break
        finally:
            await sentences.put(None)

    producer = asyncio.create_task(produce())
    spoken, back, play_end = [], [], 0.0
    hist.append({"role": "user", "content": text})

    async def next_chunk():
        """The next text to synthesize. Sentences that queued up while the last one was synthesized go out
        together (fewer, longer TTS calls), but only as many as the audio already sent covers: TTS is not
        streamed and runs at about 0.7x real time, so a chunk longer than that leaves the phone silent."""
        s = back.pop() if back else await sentences.get()
        if s is None or s in (FILLER, FILLER_LONG):
            return s
        room = max(0.0, play_end - time.monotonic()) / (1.5 * max(SYNTH_S_PER_SYL, synth_rate[0]))
        while not back and not sentences.empty():
            nxt = sentences.get_nowait()
            if nxt is None or nxt in (FILLER, FILLER_LONG) or syllables(s) + syllables(nxt) > room:
                back.append(nxt)
                break
            s = f"{s} {nxt}"
        return s

    try:
        while (s := await next_chunk()) is not None:
            pcm, sr = await tts(s)
            timings.setdefault("first_audio_ms", ms(t0))
            play_end = max(time.monotonic(), play_end) + len(pcm) / 2 / sr  # when the phone finishes this chunk
            if s not in (FILLER, FILLER_LONG):
                spoken.append(s)
            yield s, route["value"], pcm, sr
        await producer  # surfaces LLM errors
    finally:
        producer.cancel()
        # Only what was actually spoken goes into history (a barge-in cuts it short). Nothing spoken: no assistant
        # turn at all; a placeholder text would leak into captions and Qwen would parrot it.
        if spoken:
            hist.append({"role": "assistant", "content": " ".join(spoken)})


app = FastAPI(title="assistant-voice")


@app.get("/health")
def health():
    return {"ok": True, "stt": STT_MODEL, "llm": LLM_MODEL, "voice": TTS_VOICE,
            "gateways": [{"device": lk.device, "mode": lk.mode, "priority": lk.priority, "fails": lk.fails}
                         for lk in links.links]}


@app.post("/v1/audio/transcriptions", dependencies=[Depends(auth)])
async def transcriptions(file: UploadFile):
    return {"text": await transcribe(decode_wav(await file.read()))}


@app.post("/v1/turn", dependencies=[Depends(auth)])
async def turn(request: Request, session: str = "default"):
    t0 = time.perf_counter()
    wav = decode_wav(await request.body())

    async def events():
        timings = {"audio_s": round(len(wav) / 16000, 2)}
        text = await transcribe(wav)
        timings["stt_ms"] = ms(t0)
        yield json.dumps({"type": "transcript", "text": text, "stt_ms": timings["stt_ms"]}, ensure_ascii=False) + "\n"
        reply, seq, acts = [], 0, []

        async def on_event(ev):
            acts.append(ev)
        if text:
            async for s, route, pcm, sr in respond(text, session, t0, timings, on_event):
                while acts:
                    yield json.dumps(acts.pop(0), ensure_ascii=False) + "\n"
                reply.append(s)
                yield json.dumps({"type": "audio", "seq": seq, "text": s, "route": route, "sr": sr,
                                  "pcm16": base64.b64encode(pcm).decode()}, ensure_ascii=False) + "\n"
                seq += 1
        for ev in acts:
            yield json.dumps(ev, ensure_ascii=False) + "\n"
        timings["total_ms"] = ms(t0)
        yield json.dumps({"type": "done", "reply": " ".join(reply), "timings": timings}, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


# Voice activity: Silero's probability AND a margin over the call's own noise floor (background noise, other
# people's voices far away). Tunable; `noise_mode: noisy` (shared setting) raises the bar and turns voice
# barge-in off during playback (tap-to-interrupt still works).
VAD_MODES = {
    "normal": {"threshold": float(os.environ.get("VAD_THRESHOLD", "0.5")),
               "margin_db": float(os.environ.get("NOISE_MARGIN_DB", "10")),
               "min_voiced_ms": int(os.environ.get("MIN_VOICED_MS", "250")),
               "barge_voiced_ms": int(os.environ.get("BARGE_MIN_VOICED_MS", "500")), "barge": True},
    "noisy": {"threshold": float(os.environ.get("NOISY_VAD_THRESHOLD", "0.7")),
              "margin_db": float(os.environ.get("NOISY_MARGIN_DB", "15")),
              "min_voiced_ms": int(os.environ.get("NOISY_MIN_VOICED_MS", "400")),
              "barge_voiced_ms": 10**9, "barge": False},
}


class Turn:
    """Server-side end-of-speech detection over a stream of 16 kHz PCM16 bytes, gated by an adaptive noise floor."""
    FRAME = 512
    FLOOR_MIN = 30.0  # int16 RMS; digital silence must not make every breath "10 dB above the floor"
    FLOOR_START_MAX = 300.0  # a quiet room; speech is roughly 2000-8000

    def __init__(self, vad, end_silence_ms=END_SILENCE_MS, threshold=None, mode="normal"):
        self.vad = vad
        self.set_mode(mode)
        if threshold is not None:
            self.threshold = threshold
        self.end_frames = max(1, int(end_silence_ms / 1000 * 16000 / self.FRAME))
        self.pre = int(0.3 * 16000 / self.FRAME)
        self.pending = b""
        self.ring, self.speech, self.silent, self.voiced = [], [], 0, 0
        self.floor, self.last_voiced_s = None, 0.0

    def set_mode(self, mode):
        m = VAD_MODES.get(mode, VAD_MODES["normal"])
        self.mode, self.threshold, self.margin = mode if mode in VAD_MODES else "normal", m["threshold"], \
            10 ** (m["margin_db"] / 20)
        self.min_voiced_s, self.barge_voiced_s, self.barge = m["min_voiced_ms"] / 1000, m["barge_voiced_ms"] / 1000, m["barge"]

    def loud_enough(self, frame):
        """Track the noise floor (down fast, up slowly, so steady babble becomes the floor within seconds while a
        sentence barely moves it) and say whether this frame stands out from it by the margin."""
        x = np.frombuffer(frame, np.int16).astype(np.float32)
        rms = max(float(np.sqrt(np.mean(x * x))), 1.0)
        if self.floor is None:  # capped: if the user talks the moment the call connects, the floor is not their voice
            self.floor = min(max(rms, self.FLOOR_MIN), self.FLOOR_START_MAX)
        loud = rms >= self.floor * self.margin
        # down fast (pauses between words), up 1 % per 32 ms frame: steady babble becomes the floor in ~4 s
        self.floor = max(self.FLOOR_MIN, 0.7 * self.floor + 0.3 * rms if rms < self.floor else self.floor * 1.01)
        return loud

    def feed(self, data: bytes):
        """Yield ("start", None) when speech begins, ("end", float32 audio) when it ends (self.last_voiced_s is its
        voiced time), and ("drop", None) when what started was too short to be a turn (a click, a cough)."""
        self.pending += data
        step = self.FRAME * 2
        while len(self.pending) >= step:
            frame, self.pending = self.pending[:step], self.pending[step:]
            voiced = self.loud_enough(frame) and self.vad(frame) >= self.threshold
            if not self.speech:
                self.ring = (self.ring + [frame])[-self.pre:]
                if voiced:
                    self.speech, self.silent, self.voiced = self.ring[:], 0, 1
                    yield "start", None
                continue
            self.speech.append(frame)
            self.silent = 0 if voiced else self.silent + 1
            self.voiced += voiced
            if self.silent >= self.end_frames:
                audio = np.frombuffer(b"".join(self.speech), dtype=np.int16).astype(np.float32) / 32768
                self.speech, self.ring = [], []
                self.last_voiced_s = self.voiced * self.FRAME / 16000
                yield ("end", audio) if self.last_voiced_s >= self.min_voiced_s else ("drop", None)


FILLER_SOUNDS = re.compile(r"(음+|어+|아+|으+|흠+|에+|그+|저기|그러니까)[\s.,!?~]*")
STOP_WORDS = re.compile(r"아니|그만|잠깐|취소|됐어|멈춰|스톱|stop|그거 말고", re.I)
QUESTION = re.compile(r"\?|뭐|언제|어디|누구|왜|어떻게|몇|얼마|무슨|어때|알려|해\s*줘|줄래|할까|인가|나요|까요|니\b")


def real_content(text):
    """A transcript that is someone talking to the assistant, not noise: two or more Hangul syllables outside
    filler sounds (음, 어…), and not one of Whisper's silence hallucinations."""
    text = (text or "").strip()
    if not text or text.rstrip(".!") in HALLUCINATIONS:
        return False
    words = [w for w in re.split(r"\s+", text) if not FILLER_SOUNDS.fullmatch(w)]
    return len(re.findall(r"[가-힣]", "".join(words))) >= 2 or bool(re.search(r"[A-Za-z]{3,}", text))


def barge_verdict(text, voiced_s, vad_turn):
    """While the assistant was speaking: 'interrupt' only for long enough speech with real content, else 'resume'."""
    return "interrupt" if vad_turn.barge and voiced_s >= vad_turn.barge_voiced_s and real_content(text) else "resume"


def followup_action(text):
    """While the assistant is thinking: 'ignore' noise, 'replace' the question being worked on (stop words or a
    new question), or 'queue' it to answer afterwards."""
    if not real_content(text):
        return "ignore"
    return "replace" if STOP_WORDS.search(text) or QUESTION.search(text) else "queue"


@app.websocket("/v1/gateway-link")
async def gateway_link(ws: WebSocket):
    if not link_peer_ok(ws.client.host if ws.client else "", ws.headers):
        await ws.close(code=4403)
        return
    try:
        label = check_token(bearer(ws.headers), ("link", "link_limited"))
    except HTTPException:
        await ws.close(code=4401)
        return
    await ws.accept()
    lk = links.add(ws, limited=label == "link_limited")  # every gateway stays connected; routing picks one per ask
    try:
        while True:
            links.receive(lk, json.loads(await ws.receive_text()))
    except (WebSocketDisconnect, ValueError):
        pass
    finally:
        links.remove(lk)


@app.websocket("/v1/call")
async def call(ws: WebSocket, token: str = "", session: str = "", call_id: str = "", client: str = "",
               intent: str = "", persona: str = ""):
    try:
        check_token(call_token(ws.headers, token))  # header preferred; ?token= only for old clients
        if token and not bearer(ws.headers):  # remove ?token= once every shipped app sends the header
            print("deprecated: /v1/call got its token in the query string; clients must send Authorization",
                  flush=True)
    except HTTPException:
        await ws.close(code=4401)
        return
    await ws.accept()
    session = session or f"call-{int(time.time())}"
    log_id, started = uuid.uuid4().hex, time.time()  # one call-history entry per connection
    start_log = {"event": "start", "log_id": log_id}
    vad_turn = Turn(SileroVoiceActivityDetector(), mode=links.settings.get("noise_mode", "normal"))
    send_lock = asyncio.Lock()
    # play_end: when the client will finish playing what we already sent. Audio goes out
    # faster than real time, so "still talking" is judged by this, not by the task.
    current: dict = {"task": None, "play_end": 0.0, "paused_at": None}

    async def send(obj=None, audio=None):
        async with send_lock:
            if audio is not None:
                now = time.monotonic()
                current["play_end"] = max(now, current["play_end"]) + len(audio) / 2 / 24000
                for i in range(0, len(audio), 9600):  # 200 ms chunks at 24 kHz
                    await ws.send_bytes(audio[i:i + 9600])
            else:
                await ws.send_text(json.dumps(obj, ensure_ascii=False))

    guard = OpeningGuard()

    async def speak_opening(op):
        done = False
        try:
            try:
                pcms = await op["audio"] if op["audio"] else None  # normally done long before the callee answers
            except Exception:
                pcms = None
            await send({"type": "state", "value": "speaking"})
            for i, s in enumerate(op["parts"]):
                pcm = pcms[i] if pcms else (await tts(s))[0]
                await send({"type": "reply", "text": s, "route": "opening"})
                await send(audio=pcm)
                if i == 0:
                    guard.first_end = current["play_end"]
                guard.play_end = current["play_end"]
            done = True
        finally:
            if not done:  # TTS failed or cancelled: drop the protection, or the call stays deaf for an hour
                guard.first_end = guard.play_end = current["play_end"]

    async def until_played():
        """Stay 'speaking' until the phone has played everything sent, plus a short tail: only then is it the
        user's turn. Speech meanwhile is a barge-in (pause/check), never a new turn. A pause stretches the wait."""
        while current["paused_at"] is not None or (left := current["play_end"] + PLAY_TAIL_S - time.monotonic()) > 0:
            await asyncio.sleep(0.05 if current["paused_at"] is not None else min(left, 0.2))

    async def pause():
        """First stage of a voice barge-in: the app pauses its playback queue; nothing is cancelled yet."""
        current["paused_at"] = time.monotonic()
        await send({"type": "pause"})

    async def resume():
        if current["paused_at"] is not None:
            current["play_end"] += time.monotonic() - current["paused_at"]  # playback stood still meanwhile
            current["paused_at"] = None
            await send({"type": "resume"})

    async def barge_check(audio, voiced_s):
        """Second stage: interrupt only for real speech, else let the reply play on. Whatever fails (STT down),
        the app never stays paused."""
        t = time.perf_counter()
        verdict, text = "resume", ""
        try:
            text = await transcribe(audio)
            verdict = barge_verdict(text, voiced_s, vad_turn)
        except Exception as e:  # STT down: treat it as noise and play on
            print("barge-in check failed:", type(e).__name__, flush=True)
        finally:
            if verdict != "interrupt":
                await resume()
        if verdict != "interrupt":
            return
        await interrupt()
        current["task"] = asyncio.current_task()
        await answer(audio, text, timings={"barge_stt_ms": ms(t)})

    async def followup(audio, prev):
        """Speech while the assistant is thinking: a replacement cancels the current answer, a follow-up waits."""
        text = await transcribe(audio)
        act = followup_action(text)
        if act == "ignore":
            return
        if act == "replace":
            if prev and not prev.done():
                prev.cancel()
            await asyncio.gather(prev, return_exceptions=True) if prev else None
            await send({"type": "interrupt"})
        else:
            await asyncio.gather(prev, return_exceptions=True) if prev else None
        await answer(audio, text)

    async def interrupt(protected=False):
        # Let the opening's first sentence finish playing; re-read, since its end is known only once sent.
        while protected and (wait := guard.wait(time.monotonic())) > 0:
            await asyncio.sleep(min(wait, 0.05))
        task = current["task"]
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        current["play_end"] = guard.play_end = 0.0
        await send({"type": "interrupt"})  # barge-in: the client drops its queue
        if current["paused_at"] is not None:  # it was paused: un-pause the (now empty) queue, or it stays stuck
            current["paused_at"] = None
            await send({"type": "resume"})

    async def speak_briefing():
        spoken, cut = [], False
        try:
            await send({"type": "state", "value": "speaking"})
            st = await gateway_settings()
            p = current_persona(st)
            async for s in briefing_sentences(f"{p['id']}:{session}", p["name"], send, persona_text(p, st)):
                pcm, _ = await tts(s)
                await send({"type": "reply", "text": s, "route": "briefing"})
                await send(audio=pcm)
                if s not in (FILLER, FILLER_LONG):
                    spoken.append(s)
        except asyncio.CancelledError:
            cut = True
            raise
        finally:  # what was actually said becomes the call's opening in history (the start event is an upsert)
            if spoken:
                history[f"{current_persona(await gateway_settings())['id']}:{session}"].append(
                    {"role": "assistant", "content": " ".join(spoken)})
                await links.log({**start_log, "opening": " ".join(spoken)})
            if not cut:
                await until_played()
            await send({"type": "state", "value": "listening"})

    async def tap_interrupt():
        guard.speech_at = None
        await interrupt(protected=True)  # cancels the reply task; its Claude run gets a link cancel on the way out
        await send({"type": "state", "value": "listening"})

    async def interrupt_then_answer(audio, text=None):
        await interrupt(protected=True)
        current["task"] = asyncio.current_task()
        await answer(audio, text)

    async def check_greeting(audio):
        text = await transcribe(audio)
        if text and not is_greeting(text, len(audio) / 16000):
            await interrupt_then_answer(audio, text)

    async def answer(audio, text=None, timings=None):
        t0, timings = time.perf_counter(), timings or {}
        turn = {"assistant": [], "activity": [], "route": "", "interrupted": False}

        async def on_event(ev):
            if ev.get("phase") in ("tool_start", "thinking"):
                turn["activity"].append({"label": ev.get("label", ""), "kind": ev.get("kind", "generic"),
                                         "at": time.time()})
            await send(ev)
        try:
            await send({"type": "state", "value": "thinking"})
            text = text if text is not None else await transcribe(audio)
            timings["stt_ms"] = ms(t0)
            if not text:
                return
            await send({"type": "transcript", "text": text})
            started = False
            async for s, route, pcm, sr in respond(text, session, t0, timings, on_event=on_event):
                if not started:
                    await send({"type": "state", "value": "speaking"})
                    started = True
                turn["route"] = route
                if s not in (FILLER, FILLER_LONG):
                    turn["assistant"].append(s)
                await send({"type": "reply", "text": s, "route": route})
                await send(audio=pcm)
            timings["total_ms"] = ms(t0)
            queued = max(0, round((current["play_end"] - time.monotonic()) * 1000))  # still to play when generation ended
            await until_played()
            await send({"type": "turn_done", "timings": timings, "audio_ends_in_ms": queued})
        except asyncio.CancelledError:
            turn["interrupted"] = True
            raise
        except Exception as e:  # keep the call alive on a failed turn
            await send({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            if text:  # turns with an empty transcript are not history
                await links.log({"event": "turn", "log_id": log_id, "user": text, **turn,
                                 "timings": timings, "at": time.time()})
            if not turn["interrupted"]:  # a failed turn's partial reply still plays out first
                await until_played()
            await send({"type": "state", "value": "listening"})

    try:
        op = links.openings.pop(call_id, None) if call_id else None
        # The persona: a placed call's own (it rang as that persona), else ?persona=, else the primary. Every task
        # this call starts inherits it (name, character, TTS voice, history key, Claude session, tags).
        CALL_PERSONA.set(pick_persona(links.settings, (op or {}).get("persona") or persona))
        pid = CALL_PERSONA.get()["id"]
        briefing = intent == "briefing" and not call_id
        if call_id:  # the callee picked up: stops a scheduled call's retries
            await links.notify({"type": "call_answered", "call_id": call_id})
        await links.brain_event({"type": "call_event", "event": "start", "session": session, "persona": pid,
                                 "reason": (op or {}).get("reason", ""), "opening": (op or {}).get("text", "")})
        start_log.update(session=session, call_id=call_id, client=client[:20], started_at=started, persona_id=pid,
                         # the user's point of view, like a phone's call log: the assistant calling = incoming
                         direction="incoming" if call_id else "outgoing",
                         reason="briefing" if briefing else (op or {}).get("reason", ""), opening=(op or {}).get("text", ""))
        await links.log(start_log)
        if briefing:  # the app's "오늘 브리핑": speak a briefing first, interruptible like any reply
            current["task"] = asyncio.create_task(speak_briefing())
        if call_id:  # the assistant placed this call: say why first (sent ahead over the link)
            if op and op["parts"]:
                history[f"{pid}:{session}"].append({"role": "assistant", "content": op["text"]})
                guard.protect(time.monotonic())  # before any frame is read, so an early tap cannot cut it
                current["task"] = asyncio.create_task(speak_opening(op))
        await send({"type": "state", "value": "listening"})
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("text"):
                kind = json.loads(msg["text"]).get("type")
                if kind == "hangup":
                    break
                if kind == "interrupt":  # tap-to-interrupt: same as a barge-in, after a protected first sentence
                    asyncio.create_task(tap_interrupt())
                continue
            vad_turn.set_mode(links.settings.get("noise_mode", "normal"))
            for kind, audio in vad_turn.feed(msg.get("bytes") or b""):
                now = time.monotonic()
                task = current["task"]
                busy = task and not task.done()
                playing = now < current["play_end"] or current["paused_at"] is not None
                if kind == "start" and guard.active(now):
                    guard.start(now)  # a placed call's opening: its own rules (first sentence, greetings)
                elif kind == "start" and playing and current["paused_at"] is None and vad_turn.barge:
                    await pause()
                elif kind == "drop":
                    guard.end(now, 0.0)
                    await resume()  # a click or cough: the reply plays on
                elif kind == "end":
                    if guard.speech_at is not None:
                        act = guard.end(now, len(audio) / 16000)
                        if act == "check":
                            asyncio.create_task(check_greeting(audio))
                        else:
                            asyncio.create_task(interrupt_then_answer(audio))
                    elif current["paused_at"] is not None:
                        asyncio.create_task(barge_check(audio, vad_turn.last_voiced_s))
                    elif playing and not vad_turn.barge:
                        pass  # noisy mode: speech over the reply is not a turn
                    elif busy:  # thinking: never cancel on raw voice, decide on the transcript
                        current["task"] = asyncio.create_task(followup(audio, task))
                    else:
                        current["task"] = asyncio.create_task(answer(audio))
            if guard.tick(time.monotonic()) == "interrupt":
                await interrupt()
    except WebSocketDisconnect:
        pass
    finally:
        if current["task"]:
            current["task"].cancel()
            await asyncio.gather(current["task"], return_exceptions=True)  # its turn is logged before the end
        ended = time.time()
        await links.log({"event": "end", "log_id": log_id, "ended_at": ended, "duration_s": round(ended - started, 1)})
        await links.brain_event({"type": "call_event", "event": "end", "session": session})
        links.session_ctx.pop(session, None)


if __name__ == "__main__":
    # Self-checks without GPU or network (run: python voice_server.py --check).
    if CHECK_ONLY:
        assert split_ready("안녕하세요. 오늘", False) == (["안녕하세요."], "오늘")
        assert korean_only("회의(會議)가 있어요.") == "회의가 있어요." and korean_only("好的。") == ""
        assert split_ready("好的. 내일 봬요.", True)[0] == ["내일 봬요."] and korean_only("API 서버") == "API 서버"
        assert split_ready("네! 알겠어요? 그럼", False) == (["네! 알겠어요?"], "그럼")  # "네!" alone is too short
        assert split_ready("네. 말씀하세요", False) == ([], "네. 말씀하세요") and split_ready("네. 그럼요.", True)[0] == ["네. 그럼요."]
        assert split_ready("오늘은 맑아요. 네.", True)[0] == ["오늘은 맑아요. 네."]
        assert split_ready("모두 " + "3,000원이고 " * 12, False)[0] == []  # never cut inside a number
        assert split_ready("마지막 문장", True) == (["마지막 문장"], "")
        buf, out = "", []  # streamed two characters at a time, words keep their spaces
        for i in range(0, len(text := "광합성은 식물이 빛 에너지를 이용해요. 그래서 산소가 나와요."), 2):
            done, buf = split_ready(buf + text[i:i + 2], False)
            out += done
        assert out + split_ready(buf, True)[0] == ["광합성은 식물이 빛 에너지를 이용해요.", "그래서 산소가 나와요."], out
        assert split_ready("네, 알겠습니다. 오늘", False, first=True) == (["네, 알겠습니다."], "오늘")
        assert split_ready("오늘 날씨는요, 맑아요", False, first=True) == (["오늘 날씨는요,"], "맑아요")
        assert split_ready("네,", False, first=True) == ([], "네,")
        long = "가" * 70 + ", " + "나" * 20
        assert split_ready(long, False) == (["가" * 70 + ","], "나" * 20)
        assert now_kst().startswith(" 현재 시각: 20") and "요일" in now_kst()
        assert pick(MODEL_WORDS, "이번엔 오퍼스로 해 줘") == "opus" and pick(MODEL_WORDS, "안녕") is None
        assert pick(TASK_WORDS, "블루리프 레포 빌드 에러 고쳐 줘") == "code"
        assert pick(TASK_WORDS, "내일 일정 알려줘") == "schedule"

        class FakeVad:  # loud frames = speech
            def __call__(self, frame):
                return float(np.abs(np.frombuffer(frame, np.int16)).mean() > 1000)
        t = Turn(FakeVad(), end_silence_ms=200)
        loud = (np.ones(16000, np.int16) * 3000).tobytes()  # 1 s speech
        quiet = np.zeros(8000, np.int16).tobytes()           # 0.5 s silence
        ev = list(t.feed(quiet + loud[:5000])) + list(t.feed(loud[5000:] + quiet))
        assert [k for k, _ in ev] == ["start", "end"] and 0.9 < len(ev[1][1]) / 16000 < 1.6, ev
        assert list(t.feed(quiet)) == []
        short = (np.ones(3000, np.int16) * 3000).tobytes()   # 0.19 s blip: no turn
        assert [k for k, _ in t.feed(short + quiet)] == ["start", "drop"]  # a blip: no turn, and playback resumes

        # noise: Silero says "speech" for everything here (its worst case); only the noise floor stands between
        def tone(seconds, amp, seed=0):
            rng = np.random.default_rng(seed)
            return (rng.standard_normal(int(16000 * seconds)) * amp).clip(-32000, 32000).astype(np.int16).tobytes()
        always = lambda frame: 0.95  # noqa: E731
        silence = np.zeros(16000, np.int16).tobytes()
        t = Turn(always)
        babble = tone(8, 1000, 1)  # continuous babble; speech below is 15 dB louder (1000 * 5.6)
        assert [k for k, _ in t.feed(babble)] in ([], ["start", "drop"])  # learns it as the floor, no turn
        assert [k for k, _ in t.feed(tone(4, 1000, 2))] == []  # and stays quiet on it
        ev = list(t.feed(tone(0.8, 5600, 3) + tone(1, 1000, 4)))  # "잠깐만" over the babble
        assert [k for k, _ in ev] == ["start", "end"] and t.last_voiced_s >= 0.5, (ev, t.last_voiced_s)
        t = Turn(always)
        t.feed(silence) and None
        list(t.feed(silence))
        cough = tone(0.2, 8000, 5)
        assert [k for k, _ in t.feed(cough + silence)] == ["start", "drop"]  # a 200 ms cough: never a turn
        bursts = b"".join(tone(0.1, 9000, 6 + i) + silence[:9600] for i in range(5))  # door slams, clatter
        kinds = [k for k, _ in t.feed(bursts + silence)]
        # bursts 300 ms apart merge into one utterance (end-of-speech needs 500 ms of quiet); it reaches STT, and
        # noise transcribes to nothing or a hallucination, which the transcript gate rejects
        assert kinds[0] == "start" and (kinds[-1] == "drop" or barge_verdict("", t.last_voiced_s, t) == "resume")
        noisy = Turn(always, mode="noisy")
        list(noisy.feed(silence))
        assert not noisy.barge and noisy.threshold > t.threshold and noisy.margin > t.margin
        for text, voiced, want in [("잠깐만", 0.8, "interrupt"), ("잠깐만", 0.3, "resume"), ("음", 0.9, "resume"),
                                   ("어 어", 0.9, "resume"), ("시청해 주셔서 감사합니다", 1.0, "resume"), ("", 1.0, "resume"),
                                   ("그거 말고 내일 거", 1.2, "interrupt")]:
            assert barge_verdict(text, voiced, t) == want, (text, voiced)
        assert barge_verdict("잠깐만", 2.0, noisy) == "resume"  # noisy: voice never interrupts playback
        for text, want in [("음", "ignore"), ("", "ignore"), ("아니 그거 말고", "replace"), ("잠깐", "replace"),
                           ("내일 날씨는 어때?", "replace"), ("그리고 메일도 확인해 줘", "replace"),
                           ("고마워요", "queue"), ("그리고 회의는 세 시예요", "queue")]:
            assert followup_action(text) == want, (text, followup_action(text))
        class FakeWS:
            def __init__(self):
                self.sent = []

            async def send_text(self, raw):
                self.sent.append(json.loads(raw))

        gl = GatewayLinks()
        mac, alb, spark = (gl.add(FakeWS()) for _ in range(3))
        gl.receive(mac, {"type": "hello", "device": "macbook", "mode": "full", "priority": 10})
        gl.receive(alb, {"type": "hello", "device": "albireo", "mode": "full", "priority": 20})
        gl.receive(spark, {"type": "hello", "device": "spark", "mode": "limited", "priority": 90})
        gl.receive(alb, {"type": "settings", "value": {"default_model": "opus"}})  # settings from any link
        gl.receive(alb, {"type": "call", "call_id": "c1", "opening": "좋은 아침이에요."})  # openings too
        gl.receive(spark, {"type": "call", "call_id": "c2", "opening": "x"})  # but never from a limited link
        assert "c2" not in gl.openings
        assert gl.settings["default_model"] == "opus" and gl.openings["c1"]["text"] == "좋은 아침이에요."
        assert gl.pick(True) is mac and gl.pick(False) is mac
        mac.fails = 3  # unhealthy: the next full link by priority takes over
        assert gl.pick(True) is alb
        mac.fails = 0
        q = asyncio.Queue()
        gl.queues["r1"] = (q, mac)
        gl.receive(mac, {"id": "r1", "type": "text", "delta": "네"})
        gl.receive(mac, {"id": "zz", "type": "text", "delta": "lost"})  # unknown ids are ignored
        assert q.get_nowait()["delta"] == "네" and q.empty()
        q2 = asyncio.Queue()
        gl.queues["r2"] = (q2, alb)
        gl.remove(mac)  # only the dropped link's asks fail
        assert q.get_nowait()["type"] == "error" and q2.empty() and mac not in gl.links
        gl.remove(alb)
        assert gl.pick(False) is spark  # no full link: limited takes non-personal asks only
        try:
            gl.pick(True)
            raise AssertionError("personal ask reached a limited gateway")
        except RuntimeError as e:
            assert str(e) == NO_PERSONAL
        again = gl.add(FakeWS())
        gl.receive(again, {"type": "hello", "device": "spark", "mode": "limited", "priority": 90})
        assert gl.links == [again]  # a reconnect replaces that device's old link
        gl.remove(again)
        try:
            gl.pick(False)
            raise AssertionError("picked a link with none connected")
        except RuntimeError as e:
            assert str(e) == NO_GATEWAY
        locked = gl.add(FakeWS(), limited=True)  # a link_limited token claiming full stays limited
        gl.receive(locked, {"type": "hello", "device": "x", "mode": "full", "priority": 1})
        assert locked.mode == "limited"
        try:
            gl.pick(True)
            raise AssertionError("personal ask reached a link_limited gateway")
        except RuntimeError as e:
            assert str(e) == NO_PERSONAL
        gl.remove(locked)
        full_mac = gl.add(FakeWS())
        gl.receive(full_mac, {"type": "hello", "device": "macbook", "mode": "full", "priority": 10})
        intruder = gl.add(FakeWS(), limited=True)  # a link_limited token posing as macbook
        gl.receive(intruder, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
        assert full_mac in gl.links and intruder.device == "?" and intruder.mode == "limited"
        gl.receive(intruder, {"type": "settings", "value": {"assistant_name": "해커"}})
        gl.receive(intruder, {"type": "call", "call_id": "evil", "opening": "가짜 전화예요."})
        assert gl.settings.get("assistant_name") != "해커" and "evil" not in gl.openings
        gl.receive(full_mac, {"type": "call", "opening": "call_id 없음"})  # ignored, no KeyError
        for junk in [[1, 2], "text", 7, None, {"type": "call", "call_id": "j1", "opening": ["x"]},
                     {"id": ["r"], "type": "text"}, {"id": {"a": 1}}, {"id": "r9", "type": 5}]:
            gl.receive(full_mac, junk)  # authenticated but malformed: ignored, the link stays
        assert "j1" not in gl.openings and full_mac in gl.links
        gl.receive(full_mac, {"type": "call", "call_id": 7})
        gl.receive(full_mac, {"type": "hello", "device": "macbook", "mode": "full", "priority": "x"})
        assert full_mac.priority == 50 and full_mac in gl.links
        gl.remove(full_mac)
        gl.remove(intruder)
        old = gl.add(FakeWS())  # a gateway that predates hello counts as full
        assert gl.pick(True) is old
        gl.remove(old)
        assert PERSONAL.search("내일 일정 알려줘") and PERSONAL.search("메일 왔어?") and not PERSONAL.search("환율 알려줘")

        async def stream_check():  # end to end over the module-level links, incl. barge-in cancel
            a, b = links.add(FakeWS()), links.add(FakeWS())
            links.receive(a, {"type": "hello", "device": "macbook", "mode": "full", "priority": 10})
            links.receive(b, {"type": "hello", "device": "spark", "mode": "limited", "priority": 90})
            gen = claude_stream("서버 괜찮아?", "s", None, "chat", context="최근 통화 내용:\n사용자: 일정\n\n")
            first = asyncio.ensure_future(gen.__anext__())
            await asyncio.sleep(0)
            ask = a.ws.sent[-1]
            assert ask["type"] == "ask" and not b.ws.sent and ask["text"].startswith("최근 통화 내용")
            links.receive(a, {"id": ask["id"], "type": "text", "delta": "네"})
            assert await first == "네"
            await gen.aclose()  # barge-in
            assert a.ws.sent[-1] == {"type": "cancel", "id": ask["id"]} and not links.queues
            links.remove(a)
            got = []
            gen = claude_stream("환율 알려줘", "s", None, "chat", context="최근 통화 내용:\n사용자: 일정\n\n")
            task = asyncio.ensure_future(gen.__anext__())
            await asyncio.sleep(0)
            ask = b.ws.sent[-1]
            assert ask["text"] == "환율 알려줘"  # limited links never see earlier turns
            links.receive(b, {"id": ask["id"], "type": "text", "delta": "1400원이에요."})
            got.append(await task)
            links.receive(b, {"id": ask["id"], "type": "done"})
            got += [d async for d in gen]
            assert got == ["1400원이에요."] and b.ws.sent[-1]["type"] == "ask"  # finished: no cancel
            try:
                async for _ in claude_stream("오늘 일정", "s", None, "schedule", personal=True):
                    pass
                raise AssertionError("personal ask went to the limited link")
            except RuntimeError as e:
                assert str(e) == NO_PERSONAL
            links.remove(b)
        asyncio.run(stream_check())
        async def filler_check(first_delay, gap=0.0):
            """Sentences spoken for one Claude turn whose first text arrives after `first_delay` s."""
            async def fake_claude(text, session, model, task, personal=False, context="", timeout=180, on_event=None):
                await asyncio.sleep(first_delay)
                yield "네, 확인했어요. "
                await asyncio.sleep(gap)
                yield "내일은 일정이 없어요."

            async def fake_tts(sentence):
                return b"", 24000
            globals().update(claude_stream=fake_claude, tts=fake_tts, FILLER_AFTER_S=0.05, NUDGE_AFTER_S=0.25)
            return [s async for s, *_ in respond("내일 일정 알려줘", f"f{first_delay}{gap}", time.perf_counter(), {})]
        real = claude_stream, tts, FILLER_AFTER_S, NUDGE_AFTER_S
        real_llm = llm_stream
        said = "네, 확인했어요.", "내일은 일정이 없어요."
        assert asyncio.run(filler_check(0.0)) == [*said]  # quick answer: no filler
        assert asyncio.run(filler_check(0.1)) == [FILLER, *said]  # slow: one filler
        assert asyncio.run(filler_check(0.35)) == [FILLER, FILLER_LONG, *said]  # very slow: nudge once
        assert asyncio.run(filler_check(0.0, gap=0.4)) == [*said]  # pauses after the first text: no filler

        async def cut_before_speaking():  # barge-in before any sentence was spoken
            async def slow_claude(text, session, model, task, personal=False, context="", timeout=180, on_event=None):
                await asyncio.sleep(5)
                yield "늦은 답"
            globals()["claude_stream"] = slow_claude
            gen = respond("내일 일정 알려줘", "cut", time.perf_counter(), {})
            first = asyncio.ensure_future(gen.__anext__())
            await asyncio.sleep(0.02)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            await gen.aclose()
        asyncio.run(cut_before_speaking())
        assert list(history["azel:cut"]) == [{"role": "user", "content": "내일 일정 알려줘"}]  # no placeholder turn
        assert not any("끊김" in m["content"] for h in history.values() for m in h)
        claude_stream, tts, FILLER_AFTER_S, NUDGE_AFTER_S = real

        async def activity_check():  # gateway activity -> app frames, routing first, nothing but phase/label/tool
            seen, a = [], links.add(FakeWS())
            links.receive(a, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})

            async def on_event(ev):
                seen.append(ev)

            async def fake_tts(sentence):
                return b"", 24000
            globals()["tts"] = fake_tts
            run = asyncio.ensure_future(collect(respond("내일 일정 알려줘", "act", time.perf_counter(), {}, on_event)))
            await asyncio.sleep(0.01)
            ask = a.ws.sent[-1]
            for m in [{"type": "activity", "phase": "tool_start", "tool": "mcp__claude_ai_Cygnus_MCP__gistra_today",
                       "label": "학사 일정 확인 중", "args": {"q": "SECRET"}},
                      {"type": "text", "delta": "내일은 수업이 없어요."}, {"type": "done"}]:
                links.receive(a, {"id": ask["id"], **m})
            said = await run
            links.remove(a)
            globals()["tts"] = real[1]
            return seen, said

        async def collect(gen):
            return [s async for s, *_ in gen]
        seen, said = asyncio.run(activity_check())
        assert seen == [ROUTING, {"type": "activity", "phase": "tool_start", "label": "학사 일정 확인 중",
                                  "tool": "mcp__claude_ai_Cygnus_MCP__gistra_today", "kind": "generic",
                                  "route": "claude"}], seen  # no kind from that gateway: generic
        assert client_activity({"phase": "tool_start", "kind": "mail"})["kind"] == "mail"
        assert client_activity({"phase": "tool_start", "kind": "<script>"})["kind"] == "generic"
        assert "SECRET" not in json.dumps(seen) and said == ["내일은 수업이 없어요."]
        assert pre_route("클로드가 뭐야?", {}) == (False, None) and pre_route("오퍼스로 풀어 줘", {}) == (True, "opus")
        assert pre_route("엔비디아로 답해 줘", {}) == (True, "nim") and pre_route("선생님으로 불러 줘", {})[1] is None
        assert pre_route("하이쿠가 뭐야?", {})[0] is False and pre_route("일정한 속도로 걸어", {})[0] is False
        assert pre_route("안녕", {"default_model": "sonnet"}) == (True, "sonnet")
        assert assistant_name({}) == "아젤" and "'아젤'" in system_prompt(assistant_name({}))
        assert "두세 문장으로 줄여" not in system_prompt("아젤")  # no longer forced short
        for length in ("brief", "normal", "detailed"):
            assert LENGTH_STYLE[length] in system_prompt("아젤", reply_length({"reply_length": length}))
        assert reply_length({"reply_length": "huge"}) == "normal" and reply_length({}) == "normal"
        assert system_prompt("아젤", "normal", "내일 9시 회의").endswith("참고할 상황:\n내일 9시 회의")
        assert personality({"personality": "  ", "personality_default": "기본 성격"}) == "기본 성격"
        assert personality({"personality": "까칠함", "personality_default": "기본 성격"}) == "까칠함"
        assert personality({}) == "" and "성격:" not in system_prompt("아젤")  # no settings yet: no persona line
        assert "성격: 까칠함" in system_prompt("아젤", "normal", "", "까칠함")
        gl.receive(gl.links[0] if gl.links else gl.add(FakeWS()), {"type": "settings", "value": {"assistant_name": "루나"}})
        assert "'루나'" in system_prompt(assistant_name(gl.settings)) and "아젤" not in system_prompt("루나")
        for said, name, want in [("아젤아, 서버 괜찮아?", "아젤", "서버 괜찮아?"), ("아젤 지금 몇 시야", "아젤", "지금 몇 시야"),
                                 ("루나야 일정 알려줘", "루나", "일정 알려줘"), ("아젤", "아젤", "아젤"),
                                 ("서버 괜찮아?", "아젤", "서버 괜찮아?"), ("스파크야 안녕", "스파크", "안녕")]:
            assert strip_vocative(said, name) == want, (said, name)
        assert pre_route(strip_vocative("스파크야 안녕", "스파크"), {})[0] is False  # a name that is also a keyword
        assert [tag_state(h) for h in ("", "[CLA", "[CLAUDE]", "네")] == ["maybe", "maybe", "tag", "no"]
        assert hold_tag_prefix("수요일이요. [CL") == ("수요일이요. ", "[CL") and hold_tag_prefix("네 [1]번") == ("네 [1]번", "")
        for reply, empty in [("네", True), ("네.", True), ("알겠습니다!", True), ("네, 확인해 볼게요.", True), ("좋아요~", True),
                             ("십팔이야.", False), ("Thanks.", False), ("네, 18이에요.", False), ("100도예요.", False), ("", True)]:
            assert content_free(reply) is empty, reply
        assert needs_answer("물은 몇 도에서 끓어?") and needs_answer("라면 끓이는 법 알려줘")
        assert not needs_answer("고마워!") and not needs_answer("응") and not needs_answer("안녕하세요") and not needs_answer("네")

        async def ack_check(replies, user="물은 몇 도에서 끓어?"):  # Qwen's successive replies → what gets spoken
            calls = []

            async def acking_llm(messages, max_tokens=600):
                calls.append(messages)
                yield replies[min(len(calls), len(replies)) - 1]

            async def answer_claude(text, session, model, task, personal=False, context="", timeout=180, on_event=None):
                yield "클로드가 답해요."

            async def fake_tts(sentence):
                return b"", 24000
            globals().update(llm_stream=acking_llm, claude_stream=answer_claude, tts=fake_tts, FILLER_AFTER_S=5)
            history.pop("azel:ack", None)
            said = [s async for s, *_ in respond(user, "ack", time.perf_counter(), {})]
            return said, calls
        said, calls = asyncio.run(ack_check(["네.", "물은 100도에서 끓어요."]))
        assert said == ["물은 100도에서 끓어요."] and calls[1][-1]["content"] == NUDGE, said  # nudged once, "네" unsaid
        said, calls = asyncio.run(ack_check(["네.", "네, 확인해 볼게요."]))
        assert said == ["클로드가 답해요."] and len(calls) == 2, said  # empty twice: Claude takes the turn
        said, _ = asyncio.run(ack_check(["네, 100도예요."]))
        assert said == ["네, 100도예요."], said  # an acknowledgement goes out with the content after it
        said, calls = asyncio.run(ack_check(["네."], user="고마워"))
        assert said == ["네."] and len(calls) == 1  # thanks: a bare "네" is the right answer
        globals().update(llm_stream=real_llm, claude_stream=real[0], tts=real[1], FILLER_AFTER_S=real[2])

        async def late_tag_check():  # Qwen starts answering, then emits the tag: Claude takes over, the tag is never spoken
            async def tagging_llm(messages, max_tokens=600):
                for d in ["수요", "일이요. [CL", "AUDE]"]:
                    yield d

            async def answer_claude(text, session, model, task, personal=False, context="", timeout=180, on_event=None):
                yield "정확히 확인해 보니 목요일이에요."

            async def fake_tts(sentence):
                return b"", 24000
            globals().update(llm_stream=tagging_llm, claude_stream=answer_claude, tts=fake_tts, FILLER_AFTER_S=5)
            return [s async for s, *_ in respond("회의가 무슨 요일이었더라", "late", time.perf_counter(), {})]
        said_late = asyncio.run(late_tag_check())

        async def merge_check():  # sentences that queue up during a slow synthesis go out together, within the audio sent
            async def burst_llm(messages, max_tokens=600):
                yield "첫 문장은 이렇게 말해요. "
                await asyncio.sleep(0.05)
                yield "두 번째 문장이에요. 세 번째 문장이에요. 네 번째 문장도 있어요. 마지막 문장입니다."

            async def slow_tts(sentence):
                await asyncio.sleep(0.2)
                return b"\0\0" * 24000 * (3 if sentence.startswith("첫") else 1), 24000  # 3 s of audio, then 1 s
            globals().update(llm_stream=burst_llm, tts=slow_tts)
            history.pop("azel:merge", None)
            return [s async for s, *_ in respond("문장 여러 개 말해 줘", "merge", time.perf_counter(), {})]
        merged = asyncio.run(merge_check())
        globals().update(llm_stream=real_llm, tts=real[1])
        assert merged[0] == "첫 문장은 이렇게 말해요." and len(merged) < 5, merged
        assert " ".join(merged) == "첫 문장은 이렇게 말해요. 두 번째 문장이에요. 세 번째 문장이에요. 네 번째 문장도 있어요. 마지막 문장입니다."
        globals().update(llm_stream=real_llm, claude_stream=real[0], tts=real[1], FILLER_AFTER_S=real[2])
        assert not any("CLA" in s or "[" in s for s in said_late) and said_late[-1] == "정확히 확인해 보니 목요일이에요.", said_late
        async def prefetch_check():  # the opening is synthesized when the call message arrives, not on connect
            spoken = []

            async def fake_tts(sentence):
                spoken.append(sentence)
                return sentence.encode(), 24000
            globals()["tts"] = fake_tts
            gl2 = GatewayLinks()
            gl2.receive(gl2.add(FakeWS()), {"type": "call", "call_id": "c9",
                                            "opening": "좋은 아침이에요. 오늘 수업은 두 개예요."})
            op = gl2.openings["c9"]
            assert await op["audio"] == ["좋은 아침이에요.".encode(), "오늘 수업은 두 개예요.".encode()]
            assert spoken == op["parts"] == ["좋은 아침이에요.", "오늘 수업은 두 개예요."]
        asyncio.run(prefetch_check())
        globals()["tts"] = real[1]
        assert prepare_opening("")["parts"] == [] and prepare_opening("네.")["audio"] is None  # no loop here
        for text, sec, want in [("여보세요?", 0.8, True), ("네", 0.3, True), ("응응", 0.4, True), ("안녕하세요!", 1.0, True),
                                ("여보세요", 1.6, False), ("잠깐만요", 0.6, False), ("네 그런데 그거 말고요", 1.2, False)]:
            assert is_greeting(text, sec) is want, (text, sec)
        g = OpeningGuard()
        assert g.start(0.0) == "interrupt" and g.end(0.5, 0.5) == "answer"  # no opening playing: normal turns
        g.first_end, g.play_end = 2.0, 8.0  # first sentence plays until t=2, the whole opening until t=8
        assert g.start(0.5) == "hold" and g.tick(1.9) is None  # during sentence one nothing interrupts
        assert g.end(1.2, 0.7) == "check"  # "여보세요": transcribe, and let it go if it is a greeting
        assert g.start(1.0) == "hold" and g.tick(1.9) is None and g.tick(2.6) == "interrupt"  # long talk, after s1
        assert g.end(3.0, 2.0) == "answer"  # that utterance is then answered normally
        assert g.start(0.2) == "hold" and g.end(1.9, 1.7) == "interrupt"  # long but over before s1 ends: deferred
        assert g.start(9.0) == "interrupt"  # opening finished: ordinary barge-in again
        assert g.wait(9.0) == 0.0 and g.wait(5.0) == 0.0 and g.wait(1.5) == 0.5  # waits only for sentence one
        g.protect(100.0)
        assert g.wait(100.0) > 3000 and g.start(100.1) == "hold"  # protected before its audio is even known

        def tap_check(tap_after, broken_tts=False):
            """Place a 2-sentence opening (0.3 s per sentence), tap interrupt after `tap_after` s, and return
            (seconds from connect to the server's interrupt, messages after it)."""
            async def slow_tts(sentence):
                if broken_tts:
                    raise httpx.ConnectError("tts down")
                return b"\0" * 14400, 24000  # 0.3 s at 24 kHz
            from fastapi.testclient import TestClient
            globals().update(tts=slow_tts, SileroVoiceActivityDetector=lambda: (lambda frame: 0.0))
            tc = TestClient(app, client=("100.64.0.8", 1))
            links.openings["cx"] = prepare_opening("첫 문장이에요. 두 번째 문장이에요.")
            gw_link = links.add(FakeWS())
            links.receive(gw_link, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            with tc.websocket_connect("/v1/call?call_id=cx", headers={"Authorization": "Bearer app-tok"}) as w:
                t0 = time.monotonic()
                time.sleep(tap_after)
                w.send_text(json.dumps({"type": "interrupt"}))
                while True:
                    m = w.receive()
                    if m.get("text") and json.loads(m["text"])["type"] == "interrupt":
                        at = time.monotonic() - t0
                        break
                after = json.loads(w.receive()["text"])
                w.send_text(json.dumps({"type": "hangup"}))
            time.sleep(0.05)
            assert {"type": "call_answered", "call_id": "cx"} in gw_link.ws.sent  # stops a scheduled call's retries
            links.remove(gw_link)
            return at, after
        h = {k: hashlib.sha256(k.encode()).hexdigest() for k in ("app-tok", "link-tok", "lim-tok")}
        HASHES = load_hashes(f"# generated on the Mac\nclient {h['app-tok']}\nlink {h['link-tok']}\n"
                             f"link_limited {h['lim-tok']}\n")
        assert check_token("app-tok") == "client" and token_label("link-tok") is None  # link token: not a client
        assert check_token("link-tok", ("link", "link_limited")) == "link"
        assert check_token("lim-tok", ("link", "link_limited")) == "link_limited"
        for bad in ["", "app-tok ", h["app-tok"]]:  # a stolen digest is not a token
            try:
                check_token(bad)
                raise AssertionError(f"accepted {bad!r}")
            except HTTPException:
                pass
        assert call_token({"authorization": "Bearer app-tok"}, "") == "app-tok"
        assert call_token({"authorization": "Bearer app-tok"}, "old") == "app-tok"  # header wins
        assert call_token({}, "app-tok") == "app-tok" and check_token(call_token({}, "app-tok")) == "client"
        assert link_peer_ok("100.64.0.8", {}) and link_peer_ok("100.127.255.1", {})
        for host, hdrs in [("172.19.0.1", {}), ("127.0.0.1", {}), ("10.0.0.2", {}), ("8.8.8.8", {}), ("", {}),
                           ("testclient", {}), ("100.64.0.9", {"x-forwarded-for": "1.2.3.4"}),
                           ("100.64.0.9", {"forwarded": "for=1.2.3.4"}), ("100.64.0.9", {"x-real-ip": "1.2.3.4"})]:
            assert not link_peer_ok(host, hdrs), (host, hdrs)
        from fastapi.testclient import TestClient

        def ws_close_code(tc, path, **kw):
            try:
                with tc.websocket_connect(path, **kw):
                    return None
            except WebSocketDisconnect as e:
                return e.code
        off_net = TestClient(app)  # peer "testclient": not a tailnet address
        assert ws_close_code(off_net, "/v1/gateway-link", headers={"Authorization": "Bearer link-tok"}) == 4403
        on_net = TestClient(app, client=("100.64.0.9", 1))
        assert ws_close_code(on_net, "/v1/gateway-link", headers={"Authorization": "Bearer link-tok",
                                                                   "X-Forwarded-For": "1.2.3.4"}) == 4403
        assert ws_close_code(on_net, "/v1/gateway-link", headers={"Authorization": "Bearer app-tok"}) == 4401
        assert ws_close_code(on_net, "/v1/gateway-link?token=link-tok") == 4401  # header-only
        with on_net.websocket_connect("/v1/gateway-link", headers={"Authorization": "Bearer link-tok"}) as w:
            w.send_text(json.dumps({"type": "hello", "device": "t", "mode": "full", "priority": 1}))
            w.send_text(json.dumps({"type": "settings", "value": {"k": 1}}))
            w.close()
        assert links.settings == {"k": 1}
        assert ws_close_code(off_net, "/v1/call", headers={"Authorization": "Bearer nope"}) == 4401
        assert ws_close_code(off_net, "/v1/call?token=link-tok") == 4401  # a link token is not a client token
        for broken in ["client abc", f"admin {h['app-tok']}", f"link {h['link-tok']}"]:
            try:
                load_hashes(broken)
                raise AssertionError(f"loaded {broken!r}")
            except AssertionError as e:
                assert "loaded" not in str(e)
        def history_check():
            """One incoming call with one spoken turn: the full link gets start, turn and end call_log events."""
            from fastapi.testclient import TestClient

            async def fake_transcribe(audio):
                return "안녕"

            async def fake_llm(messages, max_tokens=600):
                yield "네, 안녕하세요."

            async def fake_tts(sentence):
                return b"\0" * 480, 24000

            def loud_vad():
                return lambda frame: float(np.abs(np.frombuffer(frame, np.int16)).mean() > 1000)
            globals().update(transcribe=fake_transcribe, llm_stream=fake_llm, tts=fake_tts,
                             SileroVoiceActivityDetector=loud_vad)
            full, lim = links.add(FakeWS()), links.add(FakeWS(), limited=True)
            links.receive(full, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            links.receive(lim, {"type": "hello", "device": "spark", "mode": "limited", "priority": 9})
            tc = TestClient(app, client=("100.64.0.8", 1))
            kinds_seen = []
            with tc.websocket_connect("/v1/call?client=iphone&session=h", headers={"Authorization": "Bearer app-tok"}) as w:
                w.send_bytes((np.ones(16000, np.int16) * 3000).tobytes() + np.zeros(16000, np.int16).tobytes())
                while True:
                    m = w.receive()
                    if m.get("text"):
                        kinds_seen.append(json.loads(m["text"])["type"])
                        if kinds_seen[-1] in ("turn_done", "error"):  # never wait forever on a failed turn
                            break
                w.send_text(json.dumps({"type": "hangup"}))
            assert "activity" not in kinds_seen, kinds_seen  # a Qwen turn sends no activity events
            time.sleep(0.1)
            logs = [m for m in full.ws.sent if m.get("type") == "call_log"]
            links.remove(full)
            links.remove(lim)
            assert not [m for m in lim.ws.sent if m.get("type") == "call_log"]  # never to a limited gateway
            return logs
        def briefing_check(situation):
            """/v1/call?intent=briefing against a fake full gateway; returns (reply texts, frame types, call logs)."""
            from fastapi.testclient import TestClient

            class BriefingGW(FakeWS):
                async def send_text(self, raw):
                    await super().send_text(raw)
                    m = json.loads(raw)
                    if m.get("type") == "briefing_context":
                        links.receive(self.lk, {"id": m["id"], "type": "briefing_context", "situation": situation,
                                                "facts": ["승인 대기 1건"]})
                    elif m.get("type") == "ask":
                        for ev in [{"type": "activity", "phase": "tool_start", "label": "캘린더 확인 중",
                                    "tool": "mcp__claude_ai_Google_Calendar__list_events", "kind": "calendar"},
                                   {"type": "text", "delta": "오늘 브리핑이에요. 회의가 하나 있어요."}, {"type": "done"}]:
                            links.receive(self.lk, {"id": m["id"], **ev})

            async def qwen_briefing(messages, max_tokens=600):
                assert "토론지" in messages[1]["content"] and "승인 대기 1건" in messages[1]["content"]
                for d in ["오늘 브리핑이에요. ", "토론지가 오늘 밤 11시까지예요. ", "승인 대기가 한 건 있어요."]:
                    yield d

            async def fake_tts(sentence):
                return b"\0" * 480, 24000
            globals().update(llm_stream=qwen_briefing, tts=fake_tts,
                             SileroVoiceActivityDetector=lambda: (lambda frame: 0.0))
            gw_ws = BriefingGW()
            gw_ws.lk = links.add(gw_ws)
            links.receive(gw_ws.lk, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            replies, types = [], []
            tc = TestClient(app, client=("100.64.0.8", 1))
            with tc.websocket_connect("/v1/call?intent=briefing&session=b", headers={"Authorization": "Bearer app-tok"}) as w:
                while True:
                    m = w.receive()
                    if not m.get("text"):
                        continue
                    f = json.loads(m["text"])
                    types.append(f["type"])
                    if f["type"] == "reply":
                        replies.append(f["text"])
                    if f == {"type": "state", "value": "listening"} and replies:
                        break
                w.send_text(json.dumps({"type": "hangup"}))
            time.sleep(0.05)
            logs = [m for m in gw_ws.sent if m.get("type") == "call_log"]
            links.remove(gw_ws.lk)
            return replies, types, logs
        def barge_route_check():
            """Two-stage barge-in through /v1/call while a briefing plays: a cough pauses and resumes, '잠깐만'
            pauses and interrupts. Silero is stubbed to say 'speech' for everything (its worst case)."""
            from fastapi.testclient import TestClient

            class BriefGW(FakeWS):
                async def send_text(self, raw):
                    await super().send_text(raw)
                    m = json.loads(raw)
                    if m.get("type") == "briefing_context":
                        links.receive(self.lk, {"id": m["id"], "type": "briefing_context", "situation": "메모", "facts": []})

            async def long_briefing(messages, max_tokens=600):
                for i in range(6):
                    yield f"브리핑 {i}번째 문장이에요. "

            async def long_tts(sentence):
                return b"\0" * 96000, 24000  # 2 s each: the reply keeps playing while the user talks

            async def stt(audio):  # continuous speech → words; a cough or burst → nothing
                return "잠깐만" if float(np.mean(np.abs(audio) > 0.1)) > 0.25 and len(audio) >= 16000 else ""
            globals().update(llm_stream=long_briefing, tts=long_tts, transcribe=stt,
                             SileroVoiceActivityDetector=lambda: (lambda frame: 0.95))
            gw_ws = BriefGW()
            gw_ws.lk = links.add(gw_ws)
            links.receive(gw_ws.lk, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            got = []

            def until(w, kind):
                while True:
                    m = w.receive()
                    if m.get("text"):
                        got.append(json.loads(m["text"])["type"])
                        if got[-1] == kind:
                            return

            tc = TestClient(app, client=("100.64.0.8", 1))
            with tc.websocket_connect("/v1/call?intent=briefing&session=bg", headers={"Authorization": "Bearer app-tok"}) as w:
                until(w, "reply")  # the briefing is playing
                w.send_bytes(np.zeros(8000, np.int16).tobytes() + tone(0.2, 8000, 9) + np.zeros(16000, np.int16).tobytes())
                until(w, "resume")  # cough: paused, then resumed
                assert "interrupt" not in got and got.count("pause") == 1, got
                t_speech = time.monotonic()
                w.send_bytes(tone(0.8, 9000, 10) + np.zeros(16000, np.int16).tobytes())
                until(w, "interrupt")  # a clear "잠깐만": paused, then interrupted
                commit_s = time.monotonic() - t_speech
                assert got[-2:] == ["pause", "interrupt"] or "pause" in got[got.index("resume") + 1:], got
                w.send_text(json.dumps({"type": "hangup"}))
            links.remove(gw_ws.lk)
            return commit_s
        def pause_edge_check(stt_fails):
            """A tap during a pause, or STT failing during a pause: the app is never left paused."""
            from fastapi.testclient import TestClient

            class BriefGW(FakeWS):
                async def send_text(self, raw):
                    await super().send_text(raw)
                    m = json.loads(raw)
                    if m.get("type") == "briefing_context":
                        links.receive(self.lk, {"id": m["id"], "type": "briefing_context", "situation": "메모", "facts": []})

            async def long_briefing(messages, max_tokens=600):
                for i in range(6):
                    yield f"브리핑 {i}번째 문장이에요. "

            async def long_tts(sentence):
                return b"\0" * 96000, 24000

            async def stt(audio):
                if stt_fails:
                    raise httpx.ConnectError("stt down")
                return "음"
            globals().update(llm_stream=long_briefing, tts=long_tts, transcribe=stt,
                             SileroVoiceActivityDetector=lambda: (lambda frame: 0.95))
            gw_ws = BriefGW()
            gw_ws.lk = links.add(gw_ws)
            links.receive(gw_ws.lk, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            got = []

            def until(w, kind):
                while True:
                    m = w.receive()
                    if m.get("text"):
                        got.append(json.loads(m["text"])["type"])
                        if got[-1] == kind:
                            return
            tc = TestClient(app, client=("100.64.0.8", 1))
            with tc.websocket_connect("/v1/call?intent=briefing&session=pe", headers={"Authorization": "Bearer app-tok"}) as w:
                until(w, "reply")
                if stt_fails:  # speech long enough for STT, which then fails
                    w.send_bytes(np.zeros(8000, np.int16).tobytes() + tone(0.8, 9000, 11) + np.zeros(16000, np.int16).tobytes())
                    until(w, "resume")
                else:  # speech starts (pause), then the user taps
                    w.send_bytes(np.zeros(8000, np.int16).tobytes() + tone(0.3, 9000, 12))
                    until(w, "pause")
                    w.send_text(json.dumps({"type": "interrupt"}))
                    until(w, "resume")
                    assert "interrupt" in got[got.index("pause"):], got
                w.send_text(json.dumps({"type": "hangup"}))
            links.remove(gw_ws.lk)
            return got
        got = pause_edge_check(stt_fails=False)
        assert got.index("pause") < got.index("interrupt") < len(got) - 1 - got[::-1].index("resume") + 1
        got = pause_edge_check(stt_fails=True)
        assert got[-1] == "resume" and "interrupt" not in got[got.index("pause"):], got
        commit_s = barge_route_check()
        assert commit_s < 3.0, commit_s  # speech 0.8 s + 0.5 s trailing silence + STT (stubbed here)
        globals().update(llm_stream=real_llm, tts=real[1])
        replies, types, logs = briefing_check("[4] 토론지 오늘 23시")  # fast path: local Qwen from the note
        assert replies == ["오늘 브리핑이에요.", "토론지가 오늘 밤 11시까지예요.", "승인 대기가 한 건 있어요."], replies
        assert FILLER not in replies and "activity" not in types
        starts = [m for m in logs if m["event"] == "start"]
        assert starts[0]["reason"] == "briefing" and starts[0]["direction"] == "outgoing"
        assert starts[-1]["opening"] == " ".join(replies) and starts[-1]["started_at"] == starts[0]["started_at"]
        replies, types, logs = briefing_check("")  # no note yet: filler, then Claude over the link
        assert replies == [FILLER, "오늘 브리핑이에요.", "회의가 하나 있어요."], replies
        assert types.count("activity") == 2  # routing, then the tool step
        assert [m for m in logs if m["event"] == "start"][-1]["opening"] == "오늘 브리핑이에요. 회의가 하나 있어요."
        replies, types, logs = briefing_check("")  # again within 10 min: the same text, no second Claude run
        assert replies == ["오늘 브리핑이에요.", "회의가 하나 있어요."] and "activity" not in types, replies
        last_claude_briefing[:] = [0.0, ""]
        globals()["llm_stream"] = real_llm
        def persona_check():
            """/v1/call?persona=luna: Qwen speaks as 루나 in her voice, history and logs are hers; a call placed as
            luna (no ?persona) is hers too, opening voice included."""
            from fastapi.testclient import TestClient
            voices, prompts = [], []

            async def voice_tts(sentence):
                voices.append((CALL_PERSONA.get() or {}).get("voice"))
                return b"\0" * 24000, 24000  # 0.5 s of audio

            async def hi_llm(messages, max_tokens=600):
                prompts.append(messages[0]["content"])
                yield "안녕하세요, 루나예요."

            async def stt(audio):
                return "안녕"
            globals().update(tts=voice_tts, llm_stream=hi_llm, transcribe=stt,
                             SileroVoiceActivityDetector=lambda: (lambda f: float(np.abs(np.frombuffer(f, np.int16)).mean() > 1000)))
            full = links.add(FakeWS())
            links.receive(full, {"type": "hello", "device": "macbook", "mode": "full", "priority": 1})
            links.receive(full, {"type": "settings", "value": {"personality_default": "기본 성격", "personas": [
                {"id": "azel", "name": "아젤", "voice": "sohee", "personality": "", "primary": True},
                {"id": "luna", "name": "루나", "voice": "vivian", "voice_style": "부드럽게", "personality": "느긋함"},
                {"id": "old", "name": "옛비서", "voice": "ryan", "deleted_at": 1.0}]}})
            tc = TestClient(app, client=("100.64.0.8", 1))
            speech = np.zeros(8000, np.int16).tobytes() + (np.ones(16000, np.int16) * 3000).tobytes() + np.zeros(16000, np.int16).tobytes()

            def one_turn(path):
                with tc.websocket_connect(path, headers={"Authorization": "Bearer app-tok"}) as w:
                    w.send_bytes(speech)
                    first_audio, states = None, []
                    while True:
                        m = w.receive()
                        if m.get("bytes") and first_audio is None:
                            first_audio = time.monotonic()
                        ev = json.loads(m["text"]) if m.get("text") else {}
                        if ev.get("type") == "state":
                            states.append(ev["value"])
                        if ev.get("type") in ("turn_done", "error"):
                            break
                    # the reply is 0.5 s of audio: still "speaking" until it has played, plus the tail
                    assert time.monotonic() - first_audio >= 0.5 + PLAY_TAIL_S - 0.05 and states[-1] == "speaking", states
                    assert ev["type"] == "turn_done" and ev["audio_ends_in_ms"] > 300, ev
                    assert json.loads(w.receive_text()) == {"type": "state", "value": "listening"}
                    w.send_text(json.dumps({"type": "hangup"}))
                time.sleep(0.05)
            one_turn("/v1/call?session=ps&persona=luna")
            assert "'루나'" in prompts[-1] and "성격: 느긋함" in prompts[-1] and set(voices) == {"vivian"}, (prompts, voices)
            assert "luna:ps" in history and "azel:ps" not in history
            start = [m for m in full.ws.sent if m.get("type") == "call_log" and m["event"] == "start"][-1]
            assert start["persona_id"] == "luna"
            voices.clear()
            one_turn("/v1/call?session=ps2&persona=old")  # deleted persona: the primary
            assert "'아젤'" in prompts[-1] and "성격: 기본 성격" in prompts[-1] and set(voices) == {"sohee"}
            voices.clear()

            async def placed():  # the gateway rings as luna: the opening is synthesized in her voice up front
                links.receive(full, {"type": "call", "call_id": "cl", "opening": "루나예요. 일어날 시간이에요.",
                                     "persona": "luna"})
                await links.openings["cl"]["audio"]
            asyncio.run(placed())
            assert set(voices) == {"vivian"}
            links.remove(full)
            links.settings = {}
            globals().update(tts=real[1], llm_stream=real_llm)
        persona_check()
        logs = history_check()
        assert [m["event"] for m in logs] == ["start", "turn", "end"], logs
        start, turn, end = logs
        assert start["direction"] == "outgoing" and start["client"] == "iphone" and start["session"] == "h"  # user called
        assert turn["user"] == "안녕" and turn["assistant"] == ["네, 안녕하세요."] and turn["route"] == "qwen"
        assert turn["interrupted"] is False and "total_ms" in turn["timings"]
        assert len({m["log_id"] for m in logs}) == 1 and end["duration_s"] >= 0
        # scheduled calls: Korean time phrases (now = Saturday 2026-11-21 08:00 KST)
        sat8 = datetime(2026, 11, 21, 8, 0, tzinfo=KST)
        for said, now, want, rep in [
            ("내일 10시에 깨워 줘", sat8, (11, 22, 10, 0), None),
            ("내일 아침 7시 반에 깨워 줘", sat8, (11, 22, 7, 30), None),
            ("30분 뒤에 전화해 줘", sat8, (11, 21, 8, 30), None),
            ("한 시간 후에 깨워 줘", sat8, (11, 21, 9, 0), None),
            ("모레 오후 3시에 전화해 줘", sat8, (11, 23, 15, 0), None),
            ("매일 7시에 깨워 줘", sat8, (11, 22, 7, 0), "daily"),
            ("평일 6시 40분에 깨워 줘", sat8, (11, 23, 6, 40), "weekdays"),  # Saturday: next is Monday
            ("매주 수요일 9시에 깨워 줘", sat8, (11, 25, 9, 0), "weekly:2"),
            ("다음 주 월요일 9시에 전화해 줘", sat8, (11, 23, 9, 0), None),
            ("10시에 깨워 줘", sat8, (11, 21, 10, 0), None),  # still ahead today
            ("10시에 깨워 줘", sat8.replace(hour=11), (11, 22, 10, 0), None),  # passed: tomorrow
            ("저녁 7시에 전화해 줘", sat8, (11, 21, 19, 0), None),
            ("밤 11시에 깨워 줘", sat8, (11, 21, 23, 0), None),
            ("일곱 시에 깨워 줘", sat8, (11, 22, 7, 0), None),
        ]:
            at, repeat = parse_when(said, now)
            assert (at.month, at.day, at.hour, at.minute) == want and repeat == rep, (said, at, repeat)
        assert parse_when("깨워 줘", sat8) == (None, None) and parse_when("세 시간째 기다려", sat8)[0] is None
        for said, want in [("내일 10시에 깨워 줘", "create"), ("깨워 줘", "create"), ("알람 7시로 맞춰 줘", "create"),
                           ("내일 3시에 전화해 줘", "create"), ("나한테 30분 뒤에 전화해 줘", "create"),
                           ("엄마한테 전화해 줘", None), ("엄마한테 내일 3시에 전화해 줘", None),
                           ("예약 취소해 줘", "cancel"), ("취소", "cancel"), ("내일 10시 알람 취소해 줘", "cancel"),
                           ("예약된 거 뭐 있어?", "list"), ("알람 목록 알려줘", "list"), ("내일 일정 알려줘", None),
                           ("10시에 회의 있어", None), ("클로드가 뭐야?", None)]:
            assert schedule_intent(said) == want, (said, schedule_intent(said))
        assert valid_schedule(sat8 + timedelta(days=29), None, sat8) and not valid_schedule(sat8 - timedelta(minutes=1), None, sat8)
        assert to_kst(datetime(2026, 11, 22, 10, 0)).isoformat() == "2026-11-22T10:00:00+09:00"  # naive = KST
        assert to_kst(datetime(2026, 11, 22, 1, 0, tzinfo=timezone.utc)).hour == 10
        wed9 = datetime(2026, 11, 25, 9, 0, tzinfo=KST)
        assert weekday_matches(wed9, "weekly:2") and not weekday_matches(wed9, "weekly:0") and weekday_matches(wed9, None)
        assert not valid_schedule(sat8 + timedelta(days=31), None, sat8) and not valid_schedule(sat8 + timedelta(hours=1), "hourly", sat8)

        class GatewayWS(FakeWS):  # a full gateway that answers schedule requests
            async def send_text(self, raw):
                await super().send_text(raw)
                m = json.loads(raw)
                if m.get("type") == "schedule":
                    say = {"create": "내일 오전 10시에 깨워 드릴게요.", "list": "예약된 전화가 없어요.",
                           "cancel": "취소할 예약이 없어요."}[m["action"]]
                    links.receive(self.lk, {"id": m["id"], "type": "schedule_result", "ok": True, "say": say})

        async def schedule_check():
            gw_ws = GatewayWS()
            lk = links.add(gw_ws)
            gw_ws.lk = lk
            links.receive(lk, {"type": "hello", "device": "deneb", "mode": "full", "priority": 1, "claude": False})
            out = [await schedule_reply("내일 10시에 깨워 줘", "create", "s", sat8)]
            req = gw_ws.sent[-1]
            assert req["action"] == "create" and req["at"] == "2026-11-22T10:00:00+09:00" and req["kind"] == "wake"
            out.append(await schedule_reply("취소", "cancel", "s", sat8))
            assert "at" not in gw_ws.sent[-1]  # bare cancel: the gateway cancels the soonest one
            out.append(await schedule_reply("예약된 거 뭐 있어?", "list", "s", sat8))

            async def no_json(messages):
                yield "음, 잘 모르겠어요."
            globals()["llm_stream"] = no_json
            out.append(await schedule_reply("이따가 깨워 줘", "create", "s2", sat8))  # no time: ask back
            assert schedule_intent("7시", "s2") == "create"  # the answer completes it
            out.append(await schedule_reply("7시", "create", "s2", sat8))
            assert gw_ws.sent[-1]["kind"] == "wake" and gw_ws.sent[-1]["at"] == "2026-11-22T07:00:00+09:00"

            async def json_answer(messages):
                assert "2026-11-21T08:00+09:00" in messages[0]["content"]  # Qwen gets the current KST time
                yield '{"action":"create","at":"2026-11-21T13:00:00+09:00","repeat":null,"kind":"wake","note":""}'
            globals()["llm_stream"] = json_answer
            out.append(await schedule_reply("점심 먹고 나서 깨워 줘", "create", "s3", sat8))  # rules miss it: Qwen
            assert gw_ws.sent[-1]["at"] == "2026-11-21T13:00:00+09:00"

            async def naive_answer(messages):  # Qwen left out the offset: it means KST, not the container's zone
                yield '{"action":"create","at":"2026-11-21T14:00:00","repeat":null,"kind":"wake","note":""}'
            globals()["llm_stream"] = naive_answer
            assert await schedule_reply("점심 먹고 한숨 자고 나서 깨워 줘", "create", "s6", sat8) == "내일 오전 10시에 깨워 드릴게요."
            assert gw_ws.sent[-1]["at"] == "2026-11-21T14:00:00+09:00"

            async def wrong_weekday(messages):  # weekly:0 (Monday) but 2026-11-25 is a Wednesday
                yield '{"action":"create","at":"2026-11-25T09:00:00+09:00","repeat":"weekly:0","kind":"wake","note":""}'
            globals()["llm_stream"] = wrong_weekday
            n_sent = len(gw_ws.sent)
            assert await schedule_reply("수요일마다 그 시간에 깨워 줘", "create", "s7", sat8) == "무슨 요일 몇 시에 할까요?"
            assert len(gw_ws.sent) == n_sent  # nothing stored
            out.append(await schedule_reply("어제 7시에 깨워 줘", "create", "s4", sat8 + timedelta(days=1, hours=1)))
            assert not links.pick(False, need_claude=False) is None
            try:
                links.pick(False)  # a gateway without Claude never gets asks
                raise AssertionError("asked a gateway without Claude")
            except RuntimeError:
                pass
            links.remove(lk)
            out.append(await schedule_reply("내일 10시에 깨워 줘", "create", "s5", sat8))  # no full gateway
            return out
        replies = asyncio.run(schedule_check())
        globals()["llm_stream"] = real_llm
        assert replies == ["내일 오전 10시에 깨워 드릴게요.", "취소할 예약이 없어요.", "예약된 전화가 없어요.",
                           "몇 시에 깨워 드릴까요?", "내일 오전 10시에 깨워 드릴게요.", "내일 오전 10시에 깨워 드릴게요.",
                           "내일 오전 10시에 깨워 드릴게요.", "지금은 예약을 받을 수 없어요."], replies
        at, after = tap_check(0.0)  # tapped during the first sentence: deferred until it has played
        assert 0.25 <= at < 1.0 and after == {"type": "state", "value": "listening"}, (at, after)
        at, after = tap_check(0.4)  # after the first sentence: immediate
        assert at < 0.55 and after == {"type": "state", "value": "listening"}, (at, after)
        at, after = tap_check(0.05, broken_tts=True)  # TTS failed: the protection is dropped, not held an hour
        assert at < 0.5 and after == {"type": "state", "value": "listening"}, (at, after)
        # no token ever reaches a log: the container disables access logs and INFO, and query strings are redacted
        cmd = json.loads(re.search(r"^CMD\s+(\[.*\])\s*$", open(os.path.join(os.path.dirname(os.path.abspath(
            __file__)), "Dockerfile")).read(), re.M).group(1))
        assert "--no-access-log" in cmd and cmd[cmd.index("--log-level") + 1] == "warning", cmd
        rec = logging.LogRecord("uvicorn.error", logging.INFO, "", 0, '%s - "WebSocket %s" [accepted]',
                                (("100.64.0.8", 5), "/v1/call?token=s3cret&session=x"), None)
        RedactQuery().filter(rec)
        assert "s3cret" not in rec.getMessage() and "/v1/call?<redacted>" in rec.getMessage()
        assert any(isinstance(f, RedactQuery) for f in logging.getLogger("uvicorn.error").filters)
        def brain_check():
            """The brain link: never picked for asks or logs; its context reaches Qwen; Qwen's tool call runs on it."""
            class BrainWS(FakeWS):  # answers tool and recall requests like brain.py
                async def send_text(self, raw):
                    await super().send_text(raw)
                    m = json.loads(raw)
                    if m.get("type") == "tool":
                        links.receive(self.lk, {"id": m["id"], "type": "tool_result", "result": "- 11/19 랩미팅 발표는 다음 주로 미룸"})
                    elif m.get("type") == "recall" and "느린" not in m["text"]:
                        links.receive(self.lk, {"id": m["id"], "type": "recall_result", "hits": ["(어제) 서버 이전 끝남"]})
            gw, bws = links.add(FakeWS()), BrainWS()
            br = bws.lk = links.add(bws)
            links.receive(gw, {"type": "hello", "device": "macbook", "mode": "full", "priority": 10})
            links.receive(br, {"type": "hello", "device": "brain-deneb", "mode": "full", "priority": 1, "claude": False,
                               "role": "brain"})
            assert links.pick(True) is gw and links.pick(True, need_claude=False) is gw  # never the brain
            assert links.context_for("s") == ""
            links.receive(gw, {"type": "context", "value": "가짜"})  # only the brain may set context
            links.receive(br, {"type": "context", "value": "[사용자 프로필]\n- 광주에 삶"})
            links.receive(br, {"type": "context", "session": "s1", "value": "이번 전화: 마감 알림"})
            assert links.context_for("s") == "[사용자 프로필]\n- 광주에 삶" and links.context_for("s1") == "이번 전화: 마감 알림"
            lim = links.add(FakeWS(), limited=True)
            links.receive(lim, {"type": "hello", "device": "x", "mode": "full", "role": "brain"})
            assert not lim.brain  # a link_limited token cannot pose as the brain
            links.remove(lim)
            assert pre_route("오늘 일정 뭐야?", {})[0] and not pre_route("오늘 일정 뭐야?", {}, local=True)[0]
            infra = {"infra_health", "gistra_today"}
            assert not pre_route("서버 괜찮아?", {}, local=True, tools=infra)[0] and pre_route("서버 괜찮아?", {}, local=True)[0]
            assert not pre_route("스파크 디스크 용량 어때?", {}, local=True, tools=infra)[0]
            assert pre_route("스파크 재시작해 줘", {}, local=True, tools=infra)[0]  # an action: still Claude
            assert not pre_route("스파크는?", {}, local=True, tools=infra)[0]  # a short status follow-up
            assert not pre_route("맥북 켜져 있어?", {}, local=True, tools={"device_status"})[0]
            assert pre_route("맥북 파일 정리해 줘", {}, local=True, tools={"device_status", "infra_health"})[0]
            assert pre_route("맥북 켜져 있어?", {}, local=True, tools=())[0]  # no device tool: as before
            assert pre_route("서버 상태 검색해 줘", {}, local=True, tools=infra)[0]
            assert pre_route("날씨 어때?", {}, local=True)[0] and not pre_route("아까 날씨 물어봤을 때 뭐라 했어?", {}, local=True)[0] and pre_route("레포 빌드 에러 고쳐 줘", {}, local=True)[0]
            assert [tag_state(h) for h in ("[TO", "[TOOL] {", "[C")] == ["maybe", "tool", "maybe"]
            prompts = []

            async def fake_llm(messages, max_tokens=600):
                prompts.append(messages)
                if len(prompts) == 1:
                    yield '[TOOL] {"name": "search_memory", '
                    yield '"args": {"query": "랩미팅"}}'
                else:
                    yield "다음 주로 미루셨어요."

            async def fake_tts(sentence):
                return b"", 24000
            globals().update(llm_stream=fake_llm, tts=fake_tts)
            timings = {}

            async def run(text, session):
                return [(x, r) async for x, r, *_ in respond(text, session, time.perf_counter(), timings)]
            said = asyncio.run(run("아까 말한 랩미팅 그거 어떻게 됐지?", "s"))
            assert said == [("다음 주로 미루셨어요.", "qwen")], said
            sysmsg = prompts[0][0]["content"]
            assert "광주에 삶" in sysmsg and "서버 이전 끝남" in sysmsg and TOOL_TAG in sysmsg
            assert "랩미팅 발표는 다음 주로" in prompts[1][-1]["content"] and timings["recall_ms"] < 400
            assert not [m for m in gw.ws.sent if m.get("type") == "ask"]  # answered locally, no Claude
            late = []

            async def late_tool_llm(messages, max_tokens=600):  # words first, then a tool call
                late.append(messages)
                for d in (["찾아볼게요. [TO", 'OL] {"name": "search_memory", "args": {"query": "랩미팅"}}']
                          if len(late) == 1 else ["다음 주로 미루셨어요."]):
                    yield d
            globals()["llm_stream"] = late_tool_llm
            said = asyncio.run(run("랩미팅 어떻게 됐지?", "s"))
            assert all("TOOL" not in x and "{" not in x for x, _ in said) and said[-1] == ("다음 주로 미루셨어요.", "qwen"), said
            assert "랩미팅 발표는 다음 주로" in late[1][-1]["content"]
            globals()["llm_stream"] = fake_llm
            prompts.clear()
            t = time.perf_counter()
            asyncio.run(run("느린 기억 찾기", "s"))  # the brain never answers recall: give up after RECALL_WAIT_S
            assert RECALL_WAIT_S <= time.perf_counter() - t < RECALL_WAIT_S + 0.5 and "관련된 기록" not in prompts[0][0]["content"]
            assert links.brain_tools() == set(BRAIN_TOOLS)  # a brain that lists no tools: the old set
            links.receive(br, {"type": "hello", "device": "brain-deneb", "mode": "full", "priority": 1, "claude": False,
                               "role": "brain", "tools": ["search_memory", "gistra_today", "infra_health", "Bad Name!", 7]})
            assert links.brain_tools() == {"search_memory", "gistra_today", "infra_health"}
            got = asyncio.run(run_tool('[TOOL] {"name": "gistra_today", "args": {}}', "s"))
            assert got == "- 11/19 랩미팅 발표는 다음 주로 미룸" and bws.sent[-1]["name"] == "gistra_today"
            assert asyncio.run(run_tool('[TOOL] {"name": "planit_create_task", "args": {}}', "s")).startswith("없는 도구")
            assert asyncio.run(run_tool('[TOOL] {"name": "add_todo", "args": {}}', "s")).startswith("없는 도구")  # not listed
            links.remove(br)
            assert links.context_for("s") == ""  # brain gone: old prompt and routing
            links.remove(gw)
            globals().update(llm_stream=real_llm, tts=real[1])
        brain_check()
        print("voice_server checks ok")
