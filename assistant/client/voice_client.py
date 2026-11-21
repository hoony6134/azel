"""Terminal voice loop for the Spark voice server.

Listens on the mic, detects the end of speech with Silero VAD, sends the
utterance to /v1/turn and plays the streamed reply.

usage:
  uv run voice_client.py                 # talk (use headphones: no echo cancel yet)
  uv run voice_client.py --file q.wav    # one turn from a WAV file
"""
import argparse
import base64
import io
import json
import queue
import sys
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import sounddevice as sd
import soundfile as sf
from pysilero_vad import SileroVoiceActivityDetector

SERVER = "http://100.64.0.11:8891"
TOKEN = (Path.home() / ".config/biseo/token").read_text().strip()
SR = 16000
FRAME = 512  # Silero's chunk size at 16 kHz (32 ms)


class Player:
    """Plays PCM16 chunks in arrival order on one output stream."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        stream, sr = None, None
        while (item := self.q.get()) is not None:
            pcm, rate = item
            if stream is None or rate != sr:
                if stream:
                    stream.close()
                stream, sr = sd.OutputStream(samplerate=rate, channels=1, dtype="int16"), rate
                stream.start()
            stream.write(pcm)
        if stream:
            stream.close()

    def play(self, pcm, sr):
        self.q.put((pcm, sr))

    def finish(self):
        self.q.put(None)
        self.t.join()


def turn(wav: np.ndarray, session: str, play=True, quiet=False):
    """Send one utterance; returns the server's events plus client-side timings."""
    buf = io.BytesIO()
    sf.write(buf, wav, SR, format="WAV", subtype="PCM_16")
    player = Player() if play else None
    t0 = time.perf_counter()
    events, client = [], {}
    with httpx.stream("POST", f"{SERVER}/v1/turn", params={"session": session},
                      content=buf.getvalue(), headers={"Content-Type": "audio/wav", "Authorization": f"Bearer {TOKEN}"}, timeout=120) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            ev = json.loads(line)
            if ev["type"] == "transcript":
                client["transcript_ms"] = round((time.perf_counter() - t0) * 1000)
                if not quiet:
                    print(f"  나: {ev['text']}")
            elif ev["type"] == "audio":
                client.setdefault("first_audio_ms", round((time.perf_counter() - t0) * 1000))
                pcm = np.frombuffer(base64.b64decode(ev.pop("pcm16")), dtype=np.int16)
                if player:
                    player.play(pcm, ev["sr"])
                if not quiet:
                    print(f"  비서: {ev['text']}")
            events.append(ev)
    if player:
        player.finish()
    client["total_ms"] = round((time.perf_counter() - t0) * 1000)
    return events, client


def listen(vad, silence_ms, threshold=0.5):
    """Block until one utterance is spoken; return it with ~300 ms of pre-roll."""
    pre = int(0.3 * SR / FRAME)
    end_frames = int(silence_ms / 1000 * SR / FRAME)
    ring, speech, silent = [], [], 0
    with sd.InputStream(samplerate=SR, channels=1, dtype="int16", blocksize=FRAME) as mic:
        while True:
            frame, _ = mic.read(FRAME)
            frame = frame[:, 0]
            voiced = vad(frame.tobytes()) >= threshold
            if not speech:
                ring = (ring + [frame])[-pre:]
                if voiced:
                    speech = ring[:]
                continue
            speech.append(frame)
            silent = 0 if voiced else silent + 1
            if silent >= end_frames:
                t_end = time.perf_counter()
                return np.concatenate(speech).astype(np.float32) / 32768, t_end


def main():
    global SERVER
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--server", default=SERVER)
    ap.add_argument("--silence-ms", type=int, default=500, help="silence that ends an utterance")
    args = ap.parse_args()
    SERVER = args.server
    session = f"cli-{int(time.time())}"

    if args.file:
        wav, sr = sf.read(args.file, dtype="float32")
        assert sr == SR, "WAV must be 16 kHz mono"
        events, client = turn(wav, session)
        print(json.dumps(events[-1]["timings"] | {"client": client}, ensure_ascii=False))
        return

    vad = SileroVoiceActivityDetector()
    print("말하세요 (Ctrl+C로 종료). 이어폰을 쓰면 비서 목소리가 다시 마이크로 들어가지 않습니다.")
    while True:
        wav, _ = listen(vad, args.silence_ms)
        if len(wav) < 0.4 * SR:
            continue  # a cough or click, not speech
        events, client = turn(wav, session)
        t = events[-1]["timings"]
        print(f"  [STT {t.get('stt_ms')}ms · 첫 토큰 {t.get('llm_first_token_ms')}ms · "
              f"첫 소리 {client.get('first_audio_ms')}ms · 전체 {client['total_ms']}ms]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
