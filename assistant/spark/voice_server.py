"""Spark voice server: STT -> router (Qwen or Claude via gateway) -> Qwen3-TTS per sentence.

Contract: ../PROTOCOL.md sections 1-2.
  WS   /v1/call    live call: server-side VAD, barge-in, optional call opening
  POST /v1/turn    one WAV turn -> NDJSON (benchmark)
  POST /v1/audio/transcriptions   OpenAI-compatible STT
  GET  /health     (no auth)
"""
import asyncio
import base64
import hmac
import io
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
TOKEN_FILE = os.environ.get("TOKEN_FILE", "/run/secrets/biseo_token")
NAME = os.environ.get("ASSISTANT_NAME", "김비서")
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
    f"파일, 맥북이나 서버 작업, 코드, 검색, 날씨 같은 최신 정보, 여러 단계 작업) 다른 말 없이 정확히 {ROUTE_TAG} 만 출력해. "
    "잡담, 인사, 간단한 상식, 계산, 방금 대화 내용 정리는 네가 바로 답해."
)
FILLER = "잠시만요, 확인해 볼게요."
FILLER_LONG = "조금만 더 기다려 주세요."
# Whisper's classic outputs on silence or noise; never treat them as the user speaking.
HALLUCINATIONS = {"시청해 주셔서 감사합니다", "시청해주셔서 감사합니다", "구독과 좋아요 부탁드립니다", "감사합니다", "MBC 뉴스 이덕영입니다"}
MODEL_WORDS = {"opus": r"오퍼스|opus", "sonnet": r"소넷|sonnet", "haiku": r"하이쿠|haiku"}
TASK_WORDS = {"code": r"코드|레포|빌드|버그|커밋|테스트|에러", "mac": r"맥북|파일|폴더|터미널",
              "schedule": r"일정|할 일|과제|캘린더|회의|수업|마감|알람|브리핑"}

CHECK_ONLY = "--check" in sys.argv
if not CHECK_ONLY:
    import torch
    from pysilero_vad import SileroVoiceActivityDetector
    from transformers import pipeline
    asr = pipeline("automatic-speech-recognition", model=STT_MODEL, torch_dtype=torch.float16, device="cuda:0")
    TOKEN = open(TOKEN_FILE).read().strip()
asr_lock = asyncio.Lock()
history: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_TURNS * 2))
client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5))


class GatewayLink:
    """The gateway dials in here (Spark may not open connections to jh hosts, and personal
    Claude credentials stay off this shared team machine). Asks go out, answers come back."""

    def __init__(self):
        self.ws = None
        self.send_lock = asyncio.Lock()
        self.queues: dict[str, asyncio.Queue] = {}
        self.settings: dict = {}
        self.openings: dict[str, str] = {}

    async def send(self, obj):
        ws = self.ws
        if ws is None:
            raise RuntimeError("지금 Claude 게이트웨이에 연결이 안 돼요.")
        async with self.send_lock:
            await ws.send_text(json.dumps(obj, ensure_ascii=False))

    def receive(self, msg):
        t = msg.get("type")
        if t == "settings":
            self.settings = msg.get("value") or {}
        elif t == "call":
            if len(self.openings) > 100:
                self.openings.pop(next(iter(self.openings)))
            self.openings[msg["call_id"]] = msg.get("opening", "")
        elif msg.get("id") in self.queues:
            self.queues[msg["id"]].put_nowait(msg)

    def drop(self):
        self.ws = None
        for q in self.queues.values():
            q.put_nowait({"type": "error", "message": "게이트웨이 연결이 끊겼어요."})


link = GatewayLink()

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


def check_token(got: str):
    if not hmac.compare_digest(got.encode(), TOKEN.encode()):
        raise HTTPException(401, "bad token")


def auth(request: Request):
    check_token(request.headers.get("authorization", "").removeprefix("Bearer ").strip())


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
    return link.settings  # pushed by the gateway on connect and on every change


async def claude_stream(text, session, model, task, timeout=180):
    """Text deltas from the gateway over the link; RuntimeError carries a speakable message."""
    rid, q, finished = uuid.uuid4().hex, asyncio.Queue(), False
    link.queues[rid] = q
    try:
        await link.send({"type": "ask", "id": rid, "text": text, "session": session, "model": model, "task": task})
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout)
            except asyncio.TimeoutError:
                raise RuntimeError("Claude 답이 너무 늦어서 끊었어요.")
            if ev["type"] == "text":
                yield ev["delta"]
            elif ev["type"] == "error":
                finished = True
                raise RuntimeError(ev.get("message", "Claude를 쓸 수 없어요."))
            elif ev["type"] == "done":
                finished = True
                return
    finally:
        link.queues.pop(rid, None)
        if not finished and link.ws is not None:  # barge-in or timeout: stop the Claude run too
            try:
                await link.send({"type": "cancel", "id": rid})
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
    settings = await gateway_settings()
    forced_model = pick(MODEL_WORDS, text)
    default = settings.get("default_model", "auto")
    to_claude = bool(forced_model) or default in MODEL_WORDS or bool(re.search(r"클로드", text))
    sentences: asyncio.Queue = asyncio.Queue()
    route = {"value": "claude" if to_claude else "qwen"}

    async def via_claude():
        route["value"] = "claude"
        await sentences.put(FILLER)
        task = pick(TASK_WORDS, text) or "chat"
        model = forced_model or (default if default in MODEL_WORDS else None)
        buf, got_text = "", asyncio.Event()

        async def nudge():  # Claude with tools can take 10-20 s; say something once
            try:
                await asyncio.wait_for(got_text.wait(), 9)
            except asyncio.TimeoutError:
                await sentences.put(FILLER_LONG)
        nudger = asyncio.create_task(nudge())
        try:
            async for delta in claude_stream(recent_context(hist) + text, session, model, task):
                got_text.set()
                buf += delta
                done, buf = split_ready(buf, final=False)
                for s in done:
                    await sentences.put(s)
        except RuntimeError as e:
            buf = str(e)
        except httpx.HTTPError:
            buf = "지금 Claude 게이트웨이에 연결이 안 돼요."
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
                head = buf.lstrip()
                if ROUTE_TAG.startswith(head[:len(ROUTE_TAG)]) and len(head) < len(ROUTE_TAG):
                    continue  # might be the routing tag; wait for more
                if head.startswith(ROUTE_TAG):
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
    return {"ok": True, "stt": STT_MODEL, "llm": LLM_MODEL, "voice": TTS_VOICE}


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
    try:
        check_token(ws.headers.get("authorization", "").removeprefix("Bearer ").strip())
    except HTTPException:
        await ws.close(code=4401)
        return
    await ws.accept()
    if link.ws is not None:
        link.drop()  # a newer gateway connection replaces the old one
    link.ws = ws
    try:
        while True:
            link.receive(json.loads(await ws.receive_text()))
    except (WebSocketDisconnect, ValueError):
        pass
    finally:
        if link.ws is ws:
            link.drop()


@app.websocket("/v1/call")
async def call(ws: WebSocket, token: str = "", session: str = "", call_id: str = ""):
    try:
        check_token(token)
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
            opening = link.openings.pop(call_id, "")
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
        lk = GatewayLink()
        q = asyncio.Queue()
        lk.queues["r1"] = q
        lk.receive({"type": "settings", "value": {"default_model": "opus"}})
        lk.receive({"type": "call", "call_id": "c1", "opening": "좋은 아침이에요."})
        lk.receive({"id": "r1", "type": "text", "delta": "네"})
        lk.receive({"id": "zz", "type": "text", "delta": "lost"})  # unknown ids are ignored
        assert lk.settings["default_model"] == "opus" and lk.openings["c1"] == "좋은 아침이에요."
        assert q.get_nowait()["delta"] == "네" and q.empty()
        lk.drop()
        assert q.get_nowait()["type"] == "error" and lk.ws is None
        print("voice_server checks ok")
