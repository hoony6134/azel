"""Spark voice server: STT -> router (Qwen or Claude via gateway) -> Qwen3-TTS per sentence.

Contract: ../PROTOCOL.md sections 1-2.
  WS   /v1/call    live call: server-side VAD, barge-in, optional call opening
  POST /v1/turn    one WAV turn -> NDJSON (benchmark)
  POST /v1/audio/transcriptions   OpenAI-compatible STT
  GET  /health     (no auth)
"""
import asyncio
import base64
import hashlib
import hmac
import io
import ipaddress
import json
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
NAME = os.environ.get("ASSISTANT_NAME", "박비서")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "12"))
END_SILENCE_MS = int(os.environ.get("END_SILENCE_MS", "500"))
KST = timezone(timedelta(hours=9))
MAX_UPLOAD = 16000 * 2 * 60  # one minute of 16 kHz PCM16
ROUTE_TAG = "[CLAUDE]"

SYSTEM = (
    f"너는 사용자의 개인 비서 '{NAME}'야. 지금은 음성 통화 중이라 답이 그대로 소리로 읽혀. "
    "한국어 존댓말로, 짧고 자연스럽게 말해. 첫 문장은 열 글자 안팎으로 짧게 시작해. "
    "마크다운, 목록, 이모지, 괄호 설명, URL은 쓰지 마. 숫자와 단위는 읽기 쉽게 말해. "
    "길게 설명할 일은 요점만 두세 문장으로 줄여. "
    f"중요: 답하려면 실제 데이터나 도구가 필요하면(일정, 할 일, 과제, 수업, 메일, 서버 상태, 예전에 한 말이나 기억, "
    f"파일, MacBook이나 서버 작업, 코드, 검색, 날씨 같은 최신 정보, 여러 단계 작업) 다른 말 없이 정확히 {ROUTE_TAG} 만 출력해. "
    "잡담, 인사, 간단한 상식, 계산, 방금 대화 내용 정리는 네가 바로 답해."
)
FILLER = "잠시만요, 확인해 볼게요."
FILLER_LONG = "조금만 더 기다려 주세요."
FILLER_AFTER_S = float(os.environ.get("FILLER_AFTER_MS", "1200")) / 1000  # say FILLER only if Claude is this slow
NUDGE_AFTER_S = 9.0  # then FILLER_LONG once, counted from the start of the Claude turn
# Whisper's classic outputs on silence or noise; never treat them as the user speaking.
HALLUCINATIONS = {"시청해 주셔서 감사합니다", "시청해주셔서 감사합니다", "구독과 좋아요 부탁드립니다", "감사합니다", "MBC 뉴스 이덕영입니다"}
# A model name counts only as a request to use it ("오퍼스로", "소넷한테"), not as a topic ("하이쿠가 뭐야").
_USE = r"\s*(으?로|한테|에게|모델)"
MODEL_WORDS = {"opus": rf"(오퍼스|opus){_USE}", "sonnet": rf"(소넷|sonnet){_USE}", "haiku": rf"(하이쿠|haiku){_USE}"}
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

    def pick(self, personal: bool) -> Link:
        """The link for one ask; RuntimeError carries a speakable reason when none may take it."""
        ranked = sorted(self.links, key=lambda lk: (lk.fails >= 3, lk.priority))
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
        t = msg.get("type")
        if t == "hello":
            lk.device = str(msg.get("device", "?"))
            lk.mode = "limited" if lk.locked else msg.get("mode", "full")  # a limited token cannot claim full
            lk.priority = int(msg.get("priority", 50))
            for old in [o for o in self.links if o is not lk and o.device == lk.device]:
                self.remove(old)  # that gateway reconnected; its old socket is dead
        elif t == "settings":
            self.settings = msg.get("value") or {}
        elif t == "call":
            if len(self.openings) > 100:
                self.openings.pop(next(iter(self.openings)))
            self.openings[msg["call_id"]] = msg.get("opening", "")
        elif msg.get("id") in self.queues:
            self.queues[msg["id"]][0].put_nowait(msg)


links = GatewayLinks()

_SENT_END = re.compile(r"(?<=[.!?。…~])\s+|\n+")


def split_ready(buf: str, final: bool, first: bool = False):
    """Return (complete sentences, remainder) from the streamed text buffer.

    first=True: nothing has been spoken yet, so a leading clause ending in a comma
    ("네,") goes out alone. TTS time grows with length, so this starts the voice soonest.
    """
    if first and not final:
        m = re.match(r"\s*([^,.!?\n]{1,12},)\s", buf)
        if m:
            return [m.group(1)], buf[m.end():]
    parts = _SENT_END.split(buf)
    done, rest = parts[:-1], parts[-1]
    if final:
        done, rest = parts, ""
    elif len(rest) > 80 and "," in rest:  # flush an over-long clause at its last comma
        cut = rest.rfind(",") + 1
        done, rest = done + [rest[:cut]], rest[cut:]
    return [s.strip() for s in done if s.strip()], rest


def pick(table, text):
    return next((k for k, pat in table.items() if re.search(pat, text, re.I)), None)


def pre_route(text, settings):
    """(straight to Claude?, model) decided by keywords and settings, before any LLM call."""
    forced_model = pick(MODEL_WORDS, text)
    default = settings.get("default_model", "auto")
    model = forced_model or (default if default in MODEL_WORDS else None)
    keywords = USE_CLAUDE.search(text) or PERSONAL.search(text) or NEEDS_CLAUDE.search(text) or pick(TASK_WORDS, text)
    return bool(forced_model) or default in MODEL_WORDS or bool(keywords), model


def tag_state(head):
    """Qwen's reply so far (left-stripped): 'tag' = route to Claude, 'maybe' = too short to tell, 'no' = speak it."""
    if head.startswith(ROUTE_TAG):
        return "tag"
    return "maybe" if ROUTE_TAG.startswith(head) else "no"


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


async def llm_stream(messages):
    body = {"model": LLM_MODEL, "messages": messages, "stream": True, "max_tokens": 400, "temperature": 0.7}
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


async def claude_stream(text, session, model, task, personal=False, context="", timeout=180):
    """Text deltas from a gateway over its link; RuntimeError carries a speakable message.

    `context` (recent turns of the call) goes only to full-mode links: earlier turns may be personal."""
    lk = links.pick(personal)
    text = (context if lk.mode == "full" else "") + text
    rid, q, finished = uuid.uuid4().hex, asyncio.Queue(), False
    links.queues[rid] = (q, lk)
    try:
        await lk.send({"type": "ask", "id": rid, "text": text, "session": session, "model": model, "task": task})
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout)
            except asyncio.TimeoutError:
                lk.fails += 1
                raise RuntimeError("Claude 답이 너무 늦어서 끊었어요.")
            if ev["type"] == "text":
                yield ev["delta"]
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


_tts_cache: dict[str, tuple[bytes, int]] = {}


async def tts(sentence: str) -> tuple[bytes, int]:
    # Fillers and short acknowledgements ("네,") repeat constantly: synthesize them once.
    cacheable = sentence in (FILLER, FILLER_LONG) or len(sentence) <= 6
    if cacheable and sentence in _tts_cache:
        return _tts_cache[sentence]
    r = await client.post(TTS_URL, json={"input": sentence, "voice": TTS_VOICE, "response_format": "wav"})
    r.raise_for_status()
    wav, sr = sf.read(io.BytesIO(r.content), dtype="int16")
    if cacheable and len(_tts_cache) < 500:
        _tts_cache[sentence] = (wav.tobytes(), sr)
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


async def respond(text, session, t0, timings):
    """Yield (sentence, route, pcm, sr) for one user utterance; records history as it goes."""
    hist = history[session]
    to_claude, model = pre_route(text, await gateway_settings())
    sentences: asyncio.Queue = asyncio.Queue()
    route = {"value": "claude" if to_claude else "qwen"}

    async def via_claude():
        route["value"] = "claude"
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
            async for delta in claude_stream(text, session, model, task, personal, recent_context(hist)):
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
            if to_claude:
                return await via_claude()
            messages = [{"role": "system", "content": SYSTEM + now_kst()}, *hist, {"role": "user", "content": text}]
            buf = ""
            async for delta in llm_stream(messages):
                timings.setdefault("llm_first_token_ms", ms(t0))
                buf += delta
                state = tag_state(buf.lstrip())
                if state == "maybe":
                    continue  # might be the routing tag; wait for more
                if state == "tag":
                    return await via_claude()
                done, buf = split_ready(buf, final=False, first="first_sentence_ms" not in timings)
                for s in done:
                    timings.setdefault("first_sentence_ms", ms(t0))
                    await sentences.put(s)
            for s in split_ready(buf, final=True)[0]:
                await sentences.put(s)
        finally:
            await sentences.put(None)

    producer = asyncio.create_task(produce())
    spoken = []
    hist.append({"role": "user", "content": text})
    try:
        while (s := await sentences.get()) is not None:
            pcm, sr = await tts(s)
            timings.setdefault("first_audio_ms", ms(t0))
            if s not in (FILLER, FILLER_LONG):
                spoken.append(s)
            yield s, route["value"], pcm, sr
        await producer  # surfaces LLM errors
    finally:
        producer.cancel()
        # Only what was actually produced goes into history (a barge-in cuts it short).
        hist.append({"role": "assistant", "content": " ".join(spoken) or "(말이 끊김)"})


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
        reply, seq = [], 0
        if text:
            async for s, route, pcm, sr in respond(text, session, t0, timings):
                reply.append(s)
                yield json.dumps({"type": "audio", "seq": seq, "text": s, "route": route, "sr": sr,
                                  "pcm16": base64.b64encode(pcm).decode()}, ensure_ascii=False) + "\n"
                seq += 1
        timings["total_ms"] = ms(t0)
        yield json.dumps({"type": "done", "reply": " ".join(reply), "timings": timings}, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


class Turn:
    """Server-side end-of-speech detection over a stream of 16 kHz PCM16 bytes."""
    FRAME = 512

    def __init__(self, vad, end_silence_ms=END_SILENCE_MS, threshold=0.5):
        self.vad, self.threshold = vad, threshold
        self.end_frames = max(1, int(end_silence_ms / 1000 * 16000 / self.FRAME))
        self.pre = int(0.3 * 16000 / self.FRAME)
        self.pending = b""
        self.ring, self.speech, self.silent, self.voiced = [], [], 0, 0

    def feed(self, data: bytes):
        """Yield ("start", None) when speech begins and ("end", float32 audio) when it ends."""
        self.pending += data
        step = self.FRAME * 2
        while len(self.pending) >= step:
            frame, self.pending = self.pending[:step], self.pending[step:]
            voiced = self.vad(frame) >= self.threshold
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
                if self.voiced * self.FRAME >= 0.25 * 16000:  # less voiced time is a click or cough
                    yield "end", audio


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
async def call(ws: WebSocket, token: str = "", session: str = "", call_id: str = ""):
    try:
        check_token(call_token(ws.headers, token))  # header preferred; ?token= only for old clients
    except HTTPException:
        await ws.close(code=4401)
        return
    await ws.accept()
    session = session or f"call-{int(time.time())}"
    vad_turn = Turn(SileroVoiceActivityDetector())
    send_lock = asyncio.Lock()
    # play_end: when the client will finish playing what we already sent. Audio goes out
    # faster than real time, so "still talking" is judged by this, not by the task.
    current: dict = {"task": None, "play_end": 0.0}

    async def send(obj=None, audio=None):
        async with send_lock:
            if audio is not None:
                now = time.monotonic()
                current["play_end"] = max(now, current["play_end"]) + len(audio) / 2 / 24000
                for i in range(0, len(audio), 9600):  # 200 ms chunks at 24 kHz
                    await ws.send_bytes(audio[i:i + 9600])
            else:
                await ws.send_text(json.dumps(obj, ensure_ascii=False))

    async def speak(sentences, route):
        await send({"type": "state", "value": "speaking"})
        for s in sentences:
            pcm, _ = await tts(s)
            await send({"type": "reply", "text": s, "route": route})
            await send(audio=pcm)

    async def answer(audio):
        t0, timings = time.perf_counter(), {}
        try:
            await send({"type": "state", "value": "thinking"})
            text = await transcribe(audio)
            timings["stt_ms"] = ms(t0)
            if not text:
                return
            await send({"type": "transcript", "text": text})
            started = False
            async for s, route, pcm, sr in respond(text, session, t0, timings):
                if not started:
                    await send({"type": "state", "value": "speaking"})
                    started = True
                await send({"type": "reply", "text": s, "route": route})
                await send(audio=pcm)
            timings["total_ms"] = ms(t0)
            await send({"type": "turn_done", "timings": timings})
        except asyncio.CancelledError:
            raise
        except Exception as e:  # keep the call alive on a failed turn
            await send({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            await send({"type": "state", "value": "listening"})

    try:
        if call_id:  # the assistant placed this call: say why first (sent ahead over the link)
            opening = links.openings.pop(call_id, "")
            if opening:
                parts = split_ready(opening, final=True)[0]
                history[session].append({"role": "assistant", "content": opening})
                current["task"] = asyncio.create_task(speak(parts, "opening"))
        await send({"type": "state", "value": "listening"})
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("text"):
                if json.loads(msg["text"]).get("type") == "hangup":
                    break
                continue
            for kind, audio in vad_turn.feed(msg.get("bytes") or b""):
                task = current["task"]
                busy = task and not task.done()
                if kind == "start" and (busy or time.monotonic() < current["play_end"]):
                    if busy:
                        task.cancel()
                    current["play_end"] = 0.0
                    await send({"type": "interrupt"})  # barge-in: the client drops its queue
                elif kind == "end":
                    current["task"] = asyncio.create_task(answer(audio))
    except WebSocketDisconnect:
        pass
    finally:
        if current["task"]:
            current["task"].cancel()


if __name__ == "__main__":
    # Self-checks without GPU or network (run: python voice_server.py --check).
    if CHECK_ONLY:
        assert split_ready("안녕하세요. 오늘", False) == (["안녕하세요."], "오늘")
        assert split_ready("네! 알겠어요? 그럼", False) == (["네!", "알겠어요?"], "그럼")
        assert split_ready("마지막 문장", True) == (["마지막 문장"], "")
        assert split_ready("네, 알겠습니다. 오늘", False, first=True) == (["네,"], "알겠습니다. 오늘")
        assert split_ready("네,", False, first=True) == ([], "네,")
        long = "가" * 70 + ", " + "나" * 20
        assert split_ready(long, False) == (["가" * 70 + ","], " " + "나" * 20)
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
        assert [k for k, _ in t.feed(short + quiet)] == ["start"]
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
        gl.receive(spark, {"type": "call", "call_id": "c1", "opening": "좋은 아침이에요."})  # openings too
        assert gl.settings["default_model"] == "opus" and gl.openings["c1"] == "좋은 아침이에요."
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
            async def fake_claude(text, session, model, task, personal=False, context="", timeout=180):
                await asyncio.sleep(first_delay)
                yield "네, 확인했어요. "
                await asyncio.sleep(gap)
                yield "내일은 일정이 없어요."

            async def fake_tts(sentence):
                return b"", 24000
            globals().update(claude_stream=fake_claude, tts=fake_tts, FILLER_AFTER_S=0.05, NUDGE_AFTER_S=0.25)
            return [s async for s, *_ in respond("내일 일정 알려줘", f"f{first_delay}{gap}", time.perf_counter(), {})]
        real = claude_stream, tts, FILLER_AFTER_S, NUDGE_AFTER_S
        said = "네, 확인했어요.", "내일은 일정이 없어요."
        assert asyncio.run(filler_check(0.0)) == [*said]  # quick answer: no filler
        assert asyncio.run(filler_check(0.1)) == [FILLER, *said]  # slow: one filler
        assert asyncio.run(filler_check(0.35)) == [FILLER, FILLER_LONG, *said]  # very slow: nudge once
        assert asyncio.run(filler_check(0.0, gap=0.4)) == [*said]  # pauses after the first text: no filler
        claude_stream, tts, FILLER_AFTER_S, NUDGE_AFTER_S = real
        assert pre_route("클로드가 뭐야?", {}) == (False, None) and pre_route("오퍼스로 풀어 줘", {}) == (True, "opus")
        assert pre_route("하이쿠가 뭐야?", {})[0] is False and pre_route("일정한 속도로 걸어", {})[0] is False
        assert pre_route("안녕", {"default_model": "sonnet"}) == (True, "sonnet")
        assert [tag_state(h) for h in ("", "[CLA", "[CLAUDE]", "네")] == ["maybe", "maybe", "tag", "no"]
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
        print("voice_server checks ok")
