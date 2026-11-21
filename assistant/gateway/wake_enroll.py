"""Wake-word enrollment ("Siri-style"): the user's own 아젤 clips train a personal verifier on the Mac.

Mount in gateway.py (full mode only; the global bearer dependency applies):
    import wake_enroll
    app.include_router(wake_enroll.router)

  POST   /v1/wake/enroll   multipart clips[] (WAV, 16 kHz mono 16-bit, 0.4-2.5 s)
                           -> {"accepted": n, "rejected": [{"index", "reason"}], "training": bool}
  GET    /v1/wake/enroll   -> {"clips": n, "trained_at": ISO|null, "status": "none"|"training"|"ready"|"failed"}
  DELETE /v1/wake/enroll   -> 204 (clips, verifier and status gone)

Clips never leave the Mac: ~/.config/biseo/wake-enroll/ (0700, files 0600). Training runs the wake venv's
python on assistant/wakeword/train_verifier.py in a background thread and writes azel_verifier.pkl; the wake
listener picks the file up by mtime. Needs python-multipart in the gateway venv.
"""
import array
import io
import json
import os
import subprocess
import sys
import threading
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, UploadFile

HOME = Path(os.environ.get("BISEO_HOME", Path.home() / ".config/biseo"))
ROOT = Path(__file__).resolve().parents[1]  # .../assistant
DIR = HOME / "wake-enroll"
VERIFIER = HOME / "azel_verifier.pkl"
MIN_CLIPS, MAX_CLIPS = 5, 20
MIN_S, MAX_S = 0.4, 2.5
MAX_BYTES = int(MAX_S * 32000) + 1024  # a valid clip is never bigger

router = APIRouter(prefix="/v1/wake")
lock = threading.Lock()
state = {"running": False, "dirty": False}


def private_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)
    p.chmod(0o700)


def check_clip(data: bytes):
    """None if usable, else the reason it is not."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
                return "must be 16 kHz mono 16-bit WAV"
            n = w.getnframes()
            pcm = array.array("h", w.readframes(n))  # little-endian host (macOS)
    except (wave.Error, EOFError):
        return "not a WAV file"
    if not MIN_S <= n / 16000 <= MAX_S:
        return f"length must be {MIN_S}-{MAX_S} s"
    if max(map(abs, pcm), default=0) < 500:
        return "too quiet"
    return None


def status():
    f = DIR / "status.json"
    s = json.loads(f.read_text()) if f.exists() else {}
    n = len(list((DIR / "clips").glob("*.wav")))
    if state["running"]:
        s["status"] = "training"
    return {"clips": n, "trained_at": s.get("trained_at"), "status": s.get("status", "none") if n else "none"}


def write_status(**s):
    f = DIR / "status.json"
    f.write_text(json.dumps(s))
    f.chmod(0o600)


def train_once():
    py = os.environ.get("BISEO_WAKE_PY", str(ROOT / "wake/.venv/bin/python"))
    cmd = [py, str(ROOT / "wakeword/train_verifier.py"), "--clips", str(DIR / "clips"), "--out", str(VERIFIER),
           "--model", str(HOME / "azel.onnx"), "--negatives", str(HOME / "wake-negatives")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        write_status(status="ready", trained_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    else:
        write_status(status="failed", error=r.stderr[-500:])


def train_loop():
    """Retrain until no clip arrived while training."""
    while True:
        with lock:
            state["dirty"] = False
        train_once()
        with lock:
            if not state["dirty"]:
                state["running"] = False
                return


def start_training():
    with lock:
        if state["running"]:
            state["dirty"] = True
            return
        state["running"] = True
    threading.Thread(target=train_loop, daemon=True).start()


@router.post("/enroll")
async def enroll(clips: list[UploadFile]):
    private_dir(DIR)
    private_dir(DIR / "clips")
    have = len(list((DIR / "clips").glob("*.wav")))
    accepted, rejected = 0, []
    if len(clips) > MAX_CLIPS:
        raise HTTPException(413, f"at most {MAX_CLIPS} clips per request")
    for i, up in enumerate(clips):
        data = await up.read(MAX_BYTES + 1)  # never buffer more than one clip's worth
        reason = ("too large" if len(data) > MAX_BYTES else check_clip(data)) or ("too many clips" if have + accepted >= MAX_CLIPS else None)
        if reason:
            rejected.append({"index": i, "reason": reason})
            continue
        p = DIR / "clips" / f"{uuid.uuid4().hex}.wav"
        p.write_bytes(data)
        p.chmod(0o600)
        accepted += 1
    training = have + accepted >= MIN_CLIPS and accepted > 0
    if training:
        write_status(status="training")
        start_training()
    return {"accepted": accepted, "rejected": rejected, "training": training}


@router.get("/enroll")
def get_enroll():
    return status()


@router.delete("/enroll", status_code=204)
def delete_enroll():
    if state["running"]:
        raise HTTPException(409, "training in progress")
    for f in [*(DIR / "clips").glob("*.wav"), DIR / "status.json", VERIFIER]:
        f.unlink(missing_ok=True)
    return Response(status_code=204)
