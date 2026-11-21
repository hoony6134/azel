"""Always-on wake-word listener for 아젤 (Mac).

wake word (openWakeWord) -> "네?" -> record with Silero VAD -> POST /v1/turn ->
play the streamed reply -> 6 s follow-up window without the wake word -> back to wake.

usage:
  uv run python biseo_wake.py                    # listen on the default mic
  uv run python biseo_wake.py --score clip.wav   # print the best wake score of a 16 kHz WAV
"""
import argparse
import base64
import io
import itertools
import json
import queue
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf

SR = 16000
VAD_CHUNK = 512  # Silero's chunk at 16 kHz
FRAME = 2560  # 160 ms: a multiple of both Silero's 512 and openWakeWord's 1280 samples
FRAME_S = FRAME / SR
CONFIG_PATH = Path.home() / ".config/biseo/wake.toml"
DEFAULTS = {
    "model": "hey_jarvis",  # built-in openWakeWord name, or a path to an .onnx (azel.onnx)
    "threshold": 0.5,
    "verifier": "",  # optional personal voice verifier (.pkl); off: it rejected the user's own voice too often
    "verifier_gate": 0.3,  # base score that makes the verifier run; wake needs both gate and threshold
    "server": "http://100.64.0.11:8891",
    "tts_url": "http://100.64.0.11:8890/v1/audio/speech",
    "ack_text": "네?",
    "voice": "sohee",
    "silence_ms": 600,
    "max_record_s": 15,
    "followup_s": 6,
    "mute_after_ms": 300,
    "vad_threshold": 0.5,
    "save_audio": False,
    "log_path": "~/.local/state/biseo/wake.jsonl",
    "token_path": "~/.config/biseo/token",
}


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        user = tomllib.loads(path.read_text())
        unknown = set(user) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
        cfg |= user
    return cfg


def to_wav(pcm: np.ndarray) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, pcm, SR, format="WAV", subtype="PCM_16")
    return buf.getvalue()


class Listener:
    """The state machine. Audio in/out, the detector and the clock are injected so tests can fake them."""

    def __init__(self, cfg, oww, vad, http: httpx.Client, play, ack=None, flush=lambda: None,
                 clock=time.monotonic):
        self.cfg, self.oww, self.vad, self.http = cfg, oww, vad, http
        self._play, self.ack, self.flush, self.clock = play, ack, flush, clock
        self.muted_until = 0.0
        self.ring: list[np.ndarray] = []  # last ~2 s, saved with a detection when save_audio is on
        self.log_path = Path(cfg["log_path"]).expanduser()

    # --- self-listening guard -------------------------------------------------------------
    def play(self, pcm, sr):
        """Play blocking; everything the mic heard meanwhile, plus mute_after_ms, is dropped."""
        self._play(pcm, sr)
        self.mute()

    def mute(self):
        self.flush()
        self.muted_until = self.clock() + self.cfg["mute_after_ms"] / 1000
        self.oww.reset()

    def live(self, frames):
        """Frames that arrive outside the mute window."""
        for f in frames:
            if self.clock() >= self.muted_until:
                yield f

    # --- main loop ------------------------------------------------------------------------
    def run(self, frames):
        frames = iter(frames)
        seen = watch_key(self.cfg)
        for n, frame in enumerate(self.live(frames)):
            if n % 30 == 0:  # ~every 5 s
                seen = self.reload_if_changed(seen)
            self.ring = (self.ring + [frame])[-int(2 / FRAME_S):]
            score = max(self.oww.predict(frame).values())
            if score >= self.cfg["threshold"]:
                self.on_wake(score, frames)

    def reload_if_changed(self, seen):
        """Load a changed model; a bad file keeps the working one (logged once, not retried until it changes again)."""
        now = watch_key(self.cfg)
        if now != seen:
            try:
                self.oww = load_oww(self.cfg)
            except Exception as e:
                print(f"model reload failed ({type(e).__name__}: {e}); keeping the old one", flush=True)
        return now

    def on_wake(self, score, frames):
        entry = {"time": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "model": self.cfg["model"], "score": round(float(score), 3),
                 "request": False, "transcript": None}
        if self.cfg["save_audio"]:
            entry["audio"] = str(self.save_clip(np.concatenate(self.ring)))
        self.ring = []
        session = f"wake-{int(time.time())}"
        if self.ack:
            self.play(*self.ack)
        else:
            self.beep()
        try:
            while (wav := self.record(frames)) is not None:  # first request, then follow-ups
                transcript = self.turn(wav, session)
                if not entry["request"]:
                    entry["request"], entry["transcript"] = True, transcript
        except httpx.HTTPError as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            print(f"turn failed: {entry['error']}", flush=True)
            self.beep()
        finally:
            self.log(entry)
            self.oww.reset()

    def record(self, frames):
        """One utterance via Silero VAD, or None if nobody starts talking within followup_s."""
        wait = int(self.cfg["followup_s"] / FRAME_S)
        end = int(self.cfg["silence_ms"] / 1000 / FRAME_S + 0.999)
        cap = int(self.cfg["max_record_s"] / FRAME_S)
        speech, silent, n = [], 0, 0
        self.vad.reset()
        for frame in self.live(frames):
            n += 1
            voiced = any(self.vad(frame[i:i + VAD_CHUNK].tobytes()) >= self.cfg["vad_threshold"]
                         for i in range(0, FRAME, VAD_CHUNK))
            if not speech:
                if voiced:
                    speech = self.ring[-2:] + [frame]  # ~300 ms pre-roll
                elif n >= wait:
                    return None
                self.ring = (self.ring + [frame])[-2:]
                continue
            speech.append(frame)
            silent = 0 if voiced else silent + 1
            if silent >= end or len(speech) >= cap:
                wav = np.concatenate(speech)
                if len(wav) < 0.4 * SR:  # a cough or click, not speech
                    speech, silent, n = [], 0, 0
                    continue
                return wav
        return None

    def turn(self, wav, session):
        """POST /v1/turn (PROTOCOL.md section 2) and play the reply as it streams; returns the transcript."""
        transcript = None
        with self.http.stream("POST", f"{self.cfg['server']}/v1/turn", params={"session": session},
                              content=to_wav(wav), headers={"Content-Type": "audio/wav"}, timeout=120) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                ev = json.loads(line)
                if ev["type"] == "transcript":
                    transcript = ev["text"]
                    print(f"  나: {transcript}", flush=True)
                elif ev["type"] == "audio":
                    print(f"  비서: {ev.get('text', '')}", flush=True)
                    self.play(np.frombuffer(base64.b64decode(ev["pcm16"]), dtype=np.int16), ev["sr"])
        return transcript

    # --- side effects ---------------------------------------------------------------------
    def log(self, entry):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def save_clip(self, pcm) -> Path:
        d = self.log_path.parent / "wake-audio"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{datetime.now():%Y%m%d-%H%M%S}.wav"
        p.write_bytes(to_wav(pcm))
        return p

    def beep(self):
        subprocess.run(["afplay", "/System/Library/Sounds/Tink.aiff"], check=False)
        self.mute()


def fetch_ack(cfg, http: httpx.Client):
    """'네?' from the TTS server, once; None means fall back to the system beep."""
    try:
        r = http.post(cfg["tts_url"], json={"input": cfg["ack_text"], "voice": cfg["voice"],
                                            "response_format": "wav"}, timeout=30)
        r.raise_for_status()
        pcm, sr = sf.read(io.BytesIO(r.content), dtype="int16")
        return pcm, sr
    except Exception as e:  # TTS down at startup must not stop the listener
        print(f"ack fetch failed ({type(e).__name__}), using the system beep", flush=True)
        return None


def load_oww(cfg):
    import openwakeword.utils
    from openwakeword.model import Model
    m = cfg["model"]
    if m.endswith(".onnx"):
        m = str(Path(m).expanduser())
    # fetches the shared feature models, plus the built-in model when m names one; no-op once present
    openwakeword.utils.download_models([Path(m).stem])
    v = Path(cfg["verifier"]).expanduser() if cfg["verifier"] else None
    if v and v.exists():
        return Model(wakeword_models=[m], inference_framework="onnx", custom_verifier_threshold=cfg["verifier_gate"],
                     custom_verifier_models={Path(m).stem: str(v)})
    return Model(wakeword_models=[m], inference_framework="onnx")


def load_oww_or_fallback(cfg):
    """Startup load; if the model file is broken, fall back to the generic or previous model so launchd never crash-loops."""
    try:
        return load_oww(cfg)
    except Exception as e:
        m = Path(cfg["model"]).expanduser()
        for name in (f"{m.stem}.base.onnx", f"{m.stem}.prev.onnx"):
            if m.suffix == ".onnx" and (m.parent / name).exists():
                print(f"{m.name} failed to load ({type(e).__name__}: {e}); falling back to {name}", flush=True)
                try:
                    return load_oww(cfg | {"model": str(m.parent / name)})
                except Exception:
                    continue
        raise


def watch_key(cfg):
    """mtimes of the model and the verifier (0 when absent or off): a change means a retrain finished, enrollment
    was deleted, or the verifier was toggled."""
    def mtime(path):
        try:
            return Path(path).expanduser().stat().st_mtime if path else 0
        except OSError:
            return 0
    m = cfg["model"]
    return mtime(m if m.endswith(".onnx") else ""), mtime(cfg["verifier"])


class Mic:
    """Default input device, 16 kHz mono int16, FRAME-sized blocks through a queue."""

    def __init__(self):
        import sounddevice as sd
        self.q: queue.Queue = queue.Queue()
        self.stream = sd.InputStream(samplerate=SR, channels=1, dtype="int16", blocksize=FRAME,
                                     callback=lambda data, *_: self.q.put(data[:, 0].copy()))

    def __iter__(self):
        peak = 0
        with self.stream:
            for n in itertools.count(1):
                frame = self.q.get()
                peak = max(peak, int(np.abs(frame).max()))
                if n == 20 and peak == 0:  # macOS hands a process without mic permission pure zeros
                    print("WARNING: microphone is silent; check System Settings > Privacy & Security"
                          " > Microphone (see INSTALL.md)", flush=True)
                yield frame

    def flush(self):
        while not self.q.empty():
            self.q.get_nowait()


def play_blocking(pcm, sr):
    import sounddevice as sd
    sd.play(pcm, sr)
    sd.wait()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=CONFIG_PATH)
    ap.add_argument("--score", help="print the best wake score of a 16 kHz mono WAV and exit")
    args = ap.parse_args()
    cfg = load_config(args.config)
    oww = load_oww_or_fallback(cfg)
    if args.score:
        pcm, sr = sf.read(args.score, dtype="int16")
        assert sr == SR, "WAV must be 16 kHz mono"
        print(max(max(p.values()) for p in oww.predict_clip(pcm)))
        return
    from pysilero_vad import SileroVoiceActivityDetector
    token = Path(cfg["token_path"]).expanduser().read_text().strip()
    http = httpx.Client(headers={"Authorization": f"Bearer {token}"})
    mic = Mic()
    listener = Listener(cfg, oww, SileroVoiceActivityDetector(), http, play_blocking,
                        ack=fetch_ack(cfg, http), flush=mic.flush)
    print(f"listening for {cfg['model']} (threshold {cfg['threshold']})", flush=True)
    listener.run(mic)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
