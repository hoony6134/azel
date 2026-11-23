"""Always-on wake-word listener for 아젤 (Mac).

wake word (openWakeWord) -> "네?" -> hand the call to the Azel app (azel://wake).
The mic is open only while the Azel app process runs: quit the app and the stream is released (no mic indicator),
start it again and listening resumes. While the app is in a call or enrolling (its in-call or enrolling file
exists) the stream is closed too, so its own voice can't wake it and its voice processing has the mic alone;
the listener-mic file exists exactly while the stream is open. handoff = "headless" keeps the old always-on flow on the Mac itself:
record with Silero VAD -> POST /v1/turn -> play the streamed reply -> 6 s follow-up window -> back to wake.

usage:
  uv run python biseo_wake.py                    # listen on the default mic
  uv run python biseo_wake.py --score clip.wav   # print the best wake score of a 16 kHz WAV
"""
import argparse
import base64
import io
import itertools
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

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
    "handoff": "app",  # "app": listen only while the app runs, open azel://wake; "headless": always on, /v1/turn flow
    "app_path": "/Applications/Azel.app",  # its process is told by this bundle path
    "in_call_path": "~/Library/Application Support/Azel/in-call",  # the app's lock file while it is in a call
    "gateway": "http://100.64.0.8:8892",  # for the primary persona id
    "handoff_ignore_s": 3,  # deaf this long after the handoff, until the app creates the in-call file
    "in_call_stale_s": 7200,  # an in-call file older than this is a leftover
    "app_check_s": 2,  # how often the app-running check runs
    "utterance": True,  # one-breath commands: capture what follows the wake word and hand it to the app
    "mic_live_path": "~/Library/Application Support/Azel/mic-live",  # the app is capturing: the listener's capture ends
    "utterance_dir": "~/Library/Application Support/Azel",  # wake-utterance-<id>.pcm goes here
    "utterance_wait_s": 2,  # give up this long after the wake word if nobody speaks
    "utterance_max_s": 10,
    "utterance_preroll_ms": 160,
    # The pause that ends a one-breath command. The turn's own silence_ms (600) cut "오늘 아침 | 컴파일러 프로젝트
    # 관련 일정 삭제해" after "오늘 아침" (2026-11-23), and what followed fell into the ~0.9 s before the app's mic
    # was up. Korean commands pause mid-sentence; the trailing silence is trimmed off the recording anyway.
    "utterance_silence_ms": 1100,  # audio kept from before the first voiced frame (the wake word's tail edge)
    "enrolling_path": "~/Library/Application Support/Azel/enrolling",  # the app's wake-clip recording screen
    "enrolling_stale_s": 1800,
    # Exists while this listener's input stream is open. The app waits for it to go before starting voice
    # processing: two processes on the mic made its engine start block for 3 s and rebuild (2026-11-23).
    "listener_mic_path": "~/Library/Application Support/Azel/listener-mic",
    "busy_check_s": 0.25,  # how often a released mic checks whether the app's call or enrollment ended
}


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        user = tomllib.loads(path.read_text())
        unknown = set(user) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
        cfg |= user
    if cfg["handoff"] not in ("app", "headless"):
        raise ValueError(f'{path}: handoff must be "app" or "headless"')
    return cfg


def to_wav(pcm: np.ndarray) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, pcm, SR, format="WAV", subtype="PCM_16")
    return buf.getvalue()


class Listener:
    """The state machine. Audio in/out, the detector and the clock are injected so tests can fake them."""

    def __init__(self, cfg, oww, vad, http: httpx.Client, play, ack=None, flush=lambda: None,
                 clock=time.monotonic, wall=time.time, open_url=None, spawn=None):
        self.cfg, self.oww, self.vad, self.http = cfg, oww, vad, http
        self._play, self.ack, self.flush, self.clock = play, ack, flush, clock
        self.wall, self.open_url = wall, open_url or open_app_url
        self.spawn = spawn or (lambda fn, *a: threading.Thread(target=fn, args=a, daemon=True).start())
        self.deaf_until, self.was_in_call = 0.0, False
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

    def fresh(self, path_key, stale_key):
        """A lock file of the app exists and is not a leftover (older than its stale limit)."""
        try:
            age = self.wall() - Path(self.cfg[path_key]).expanduser().stat().st_mtime
        except OSError:
            return False
        return age < self.cfg[stale_key]

    def in_call(self):
        return self.fresh("in_call_path", "in_call_stale_s")

    def enrolling(self):
        return self.fresh("enrolling_path", "enrolling_stale_s")

    def busy(self):
        """The app is in a call or recording wake clips: it uses the mic itself."""
        return self.in_call() or self.enrolling()

    def hold_mic(self, on):
        """The listener-mic marker: present exactly while the input stream is open."""
        path = Path(self.cfg["listener_mic_path"]).expanduser()
        try:
            if on:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{os.getpid()}\n")
            else:
                path.unlink(missing_ok=True)
        except OSError as e:
            print(f"listener-mic marker failed ({type(e).__name__}: {e})", flush=True)

    def live(self, frames):
        """Frames that arrive outside the mute window, the post-handoff deaf window, an app call and enrollment.
        App mode ends at a call or enrollment, so supervise() closes the stream and the app has the mic alone."""
        for f in frames:
            busy = self.busy()
            if busy and use_app(self.cfg):
                self.oww.reset()
                self.was_in_call = False
                return
            if self.was_in_call and not busy:
                self.oww.reset()  # forget what the call put into the detector's buffer
            self.was_in_call = busy
            if not busy and self.clock() >= max(self.muted_until, self.deaf_until):
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
        ring, self.ring = self.ring, []
        session = f"wake-{int(time.time())}"
        one_breath = use_app(self.cfg) and self.cfg["utterance"]
        if one_breath:
            self.ack_async()
        elif self.ack:
            self.play(*self.ack)
        else:
            self.beep()
        if use_app(self.cfg):
            persona = primary_persona(self.cfg, self.http)
            entry["handoff"], entry["persona"] = "app", persona
            uid = f"{int(time.time())}-{secrets.token_hex(3)}" if one_breath else None
            if uid and not self.begin_utterance(uid):  # the .part file tells the app a capture is under way
                uid = None  # no file to hand over: the app just listens itself
            url = f"azel://wake?persona={quote(persona)}" + (f"&utterance={uid}" if uid else "")
            opened = self.open_url(url)
            if uid:
                pcm = self.capture_utterance(frames, ring)  # always written, empty when nobody spoke: the app waits for it
                entry["utterance"], entry["utterance_s"] = uid, round(len(pcm) / SR, 2)
                self.write_utterance(uid, pcm)
            if opened:
                self.deaf_until = self.clock() + self.cfg["handoff_ignore_s"]
            else:  # no headless fallback in app mode: the app answers, or nobody does
                entry["error"] = "open azel://wake failed"
                print(entry["error"], flush=True)
            self.log(entry)
            self.oww.reset()
            return
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

    def capture_utterance(self, frames, ring) -> np.ndarray:
        """What is said right after the wake word, from the raw frames (the app's call-file ends it, so no live()).

        Ends on the app's in-call file (its mic is live), end of speech, the cap, or no speech within
        utterance_wait_s. Returns int16 samples; empty when nobody spoke."""
        c = self.cfg
        keep = max(1, -(-c["utterance_preroll_ms"] * SR // 1000 // FRAME))
        wait, cap = int(c["utterance_wait_s"] / FRAME_S), int(c["utterance_max_s"] / FRAME_S)
        end = int(c["utterance_silence_ms"] / 1000 / FRAME_S + 0.999)
        pre, speech, silent = list(ring[-keep:]), [], 0
        self.vad.reset()
        for n, frame in enumerate(frames, 1):
            live = Path(c["mic_live_path"]).expanduser().exists()  # the app hears the rest itself; keep this frame, then stop
            voiced = any(self.vad(frame[i:i + VAD_CHUNK].tobytes()) >= c["vad_threshold"]
                         for i in range(0, FRAME, VAD_CHUNK))
            if speech:
                speech.append(frame)
                silent = 0 if voiced else silent + 1
                if silent >= end or len(speech) >= cap:
                    break
            elif voiced:
                speech = pre + [frame]
            elif n >= wait:
                break
            else:
                pre = (pre + [frame])[-keep:]
            if live:
                break
        if silent > 1:  # keep one quiet frame: the pause that ended it would end the server's turn too (500 ms)
            speech = speech[:len(speech) - (silent - 1)]
        return np.concatenate(speech) if speech else np.zeros(0, dtype=np.int16)

    def utterance_paths(self, uid):
        d = Path(self.cfg["utterance_dir"]).expanduser()
        return d, d / f"wake-utterance-{uid}.pcm.part", d / f"wake-utterance-{uid}.pcm"

    def begin_utterance(self, uid) -> bool:
        """Create wake-utterance-<uid>.pcm.part; False (logged) if the folder or file can't be made."""
        d, part, _ = self.utterance_paths(uid)
        try:
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.close(os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
            return True
        except OSError as e:
            print(f"utterance file failed ({type(e).__name__}: {e}); handing over without it", flush=True)
            return False

    def write_utterance(self, uid, pcm) -> Path:
        """wake-utterance-<uid>.pcm (16 kHz mono int16 LE, 0600), renamed from .pcm.part so an existing .pcm is complete."""
        d, tmp, final = self.utterance_paths(uid)
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        for old in d.glob("wake-utterance-*"):  # the app deletes what it read; this is for a crashed app
            try:
                if self.wall() - old.stat().st_mtime > 3600:
                    old.unlink()
            except OSError:
                pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pcm.astype("<i2").tobytes())
        os.replace(tmp, final)
        return final

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

    def ack_async(self):
        """'네?' (or the system beep) the moment the wake word is heard, without waiting for it and without flushing
        the mic: the command is already being said, and it is captured right after. The ack may be heard in it."""
        if self.ack:
            self.spawn(self._play, *self.ack)
        else:
            self.spawn(lambda: subprocess.run(["afplay", "/System/Library/Sounds/Tink.aiff"], check=False))

    def beep(self):
        subprocess.run(["afplay", "/System/Library/Sounds/Tink.aiff"], check=False)
        self.mute()


def use_app(cfg) -> bool:
    """App mode hands every wake to the Azel app; only handoff = "headless" answers on the Mac itself."""
    return cfg["handoff"] == "app"


def app_running(cfg) -> bool:
    """Is the Azel app's process up? Matched on its bundle path so a lookalike name can't count."""
    pattern = "^" + re.escape(str(Path(cfg["app_path"]).expanduser())) + "/Contents/MacOS/"
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


def gated(frames, cfg, running=app_running, clock=time.monotonic):
    """Frames while the app runs, re-checked every app_check_s; ends when it quits so the caller can free the mic."""
    last = clock()
    for f in frames:
        if clock() - last >= cfg["app_check_s"]:
            last = clock()
            if not running(cfg):
                return
        yield f


def supervise(cfg, listener, open_mic, running=app_running, sleep=time.sleep, stop=lambda: False):
    """Open the mic only while the app runs, release it when the app quits, resume when it starts again.
    Each state change is printed and logged once."""
    state = None
    while not stop():
        up = running(cfg)
        if up != state:
            state = up
            msg = "app running: listening" if up else "app not running: mic closed"
            print(msg, flush=True)
            listener.log({"time": datetime.now().astimezone().isoformat(timespec="seconds"), "event": msg})
        if not up:
            sleep(cfg["app_check_s"])
            continue
        if listener.busy():  # the app has the mic (a call, enrollment): stay closed until it is done
            sleep(cfg["busy_check_s"])
            continue
        listener.hold_mic(True)
        try:
            mic = open_mic()
            frames = iter(mic)
            listener.flush = mic.flush
            try:
                listener.run(gated(frames, cfg, running, listener.clock))
            finally:
                frames.close()  # stops and closes the input stream
        finally:
            listener.hold_mic(False)  # only after the stream is closed: the app waits on this
            listener.flush = lambda: None
            listener.oww.reset()
            listener.ring = []


def primary_persona(cfg, http: httpx.Client) -> str:
    """The primary persona's id from the gateway, or "azel" when it cannot be had."""
    try:
        r = http.get(f"{cfg['gateway']}/v1/personas", timeout=2)
        r.raise_for_status()
        data = r.json()
        for p in (data["personas"] if isinstance(data, dict) else data):
            if p.get("primary") and not p.get("deleted_at"):
                return p["id"]
    except Exception:  # the gateway being down must not stop the wake
        pass
    return "azel"


def open_app_url(url: str) -> bool:
    return subprocess.run(["open", "-g", url], capture_output=True).returncode == 0


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
    listener = Listener(cfg, oww, SileroVoiceActivityDetector(), http, play_blocking, ack=fetch_ack(cfg, http))
    print(f"wake model {cfg['model']} (threshold {cfg['threshold']}), handoff {cfg['handoff']}", flush=True)
    if use_app(cfg):
        supervise(cfg, listener, Mic)
    else:
        mic = Mic()
        listener.flush = mic.flush
        listener.run(mic)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
