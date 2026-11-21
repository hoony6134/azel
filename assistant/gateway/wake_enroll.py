"""Wake-word voice retraining: the user's own 아젤 clips retrain the wake model on the Mac (CPU only).

Mount in gateway.py (full mode only; the global bearer dependency applies):
    import wake_enroll
    app.include_router(wake_enroll.router)

  POST   /v1/wake/enroll   multipart clips[] (WAV, 16 kHz mono 16-bit, 0.4-2.5 s, up to 60 per request and in total)
                           -> {"accepted": n, "rejected": [{"index", "reason"}], "training": bool}
                           training starts once at least MIN_CLIPS (20) clips are stored
  GET    /v1/wake/enroll   -> {"clips": n, "min_clips": 20, "trained_at": ISO|null,
                               "status": "none"|"training"|"ready"|"failed",
                               "improved": bool|null, "metrics": {...}|null, "error": str|null}
  DELETE /v1/wake/enroll   -> 204 (clips and status gone, the generic model restored)

Clips never leave the Mac: ~/.config/biseo/wake-enroll/ (0700, files 0600). Training runs
assistant/wakeword/retrain.py (the wakeword venv, nice'd) in a background thread. It replaces
~/.config/biseo/azel.onnx only when held-out recall goes up without more false accepts, keeps azel.prev.onnx
and azel.base.onnx, and the wake listener reloads the model when the file changes. status "ready" with
"improved": false means the old model stayed. Needs python-multipart in the gateway venv; one-time setup in
assistant/wakeword/RETRAIN.md.
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

from fastapi import HTTPException, Response, UploadFile
from fastapi.routing import APIRoute, APIRouter

HOME = Path(os.environ.get("BISEO_HOME", Path.home() / ".config/biseo"))
ROOT = Path(__file__).resolve().parents[1]  # .../assistant
DIR = HOME / "wake-enroll"
MODEL = HOME / "azel.onnx"
MIN_CLIPS, MAX_CLIPS = 20, 60
MIN_S, MAX_S = 0.4, 2.5
MAX_BYTES = int(MAX_S * 32000) + 1024  # a valid clip is never bigger

MAX_REQUEST = MAX_CLIPS * (MAX_BYTES + 1024)  # per-part multipart overhead


class LimitedRoute(APIRoute):
    """Refuse an oversized upload from its Content-Length, before FastAPI parses (and spools) the body."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def limited(request):
            if request.method == "POST":
                n = request.headers.get("content-length")
                if n is None:
                    raise HTTPException(411, "Content-Length required")
                if not n.isdigit() or int(n) > MAX_REQUEST:
                    raise HTTPException(413, "upload too large")
            return await handler(request)
        return limited


router = APIRouter(prefix="/v1/wake", route_class=LimitedRoute)
lock = threading.RLock()
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
            pcm = array.array("h", w.readframes(w.getnframes()))  # little-endian host (macOS)
    except (wave.Error, EOFError):
        return "not a WAV file"
    if not MIN_S <= len(pcm) / 16000 <= MAX_S:  # the header's frame count can lie
        return f"length must be {MIN_S}-{MAX_S} s"
    if max(map(abs, pcm), default=0) < 500:
        return "too quiet"
    return None


def read_status():
    """status.json, or a failed status if it is unreadable (a crash mid-write must not turn GET into a 500)."""
    f = DIR / "status.json"
    try:
        s = json.loads(f.read_text()) if f.exists() else {}
        return s if isinstance(s, dict) else {"status": "failed", "error": "status file is not an object"}
    except (OSError, ValueError):
        return {"status": "failed", "error": "status file unreadable"}


def status():
    s = read_status()
    n = len(list((DIR / "clips").glob("*.wav")))
    if state["running"]:
        s["status"] = "training"
    elif s.get("status") == "training":  # the gateway restarted mid-training
        s = {**s, "status": "failed", "error": "학습이 중단됐어요"}
    return {"clips": n, "min_clips": MIN_CLIPS, "trained_at": s.get("trained_at"),
            "status": s.get("status", "none") if n else "none", "improved": s.get("improved"),
            "metrics": s.get("metrics"), "error": s.get("error")}


def write_status(**s):
    f = DIR / "status.json"
    f.write_text(json.dumps(s))
    f.chmod(0o600)


TRAIN_TIMEOUT_S = int(os.environ.get("BISEO_WAKE_TRAIN_TIMEOUT_S", "1800"))


def wake_python():
    return os.environ.get("BISEO_WAKE_PY", str(ROOT / "wakeword/.venv/bin/python"))


def train_once():
    py = wake_python()
    if not Path(py).is_file():  # no wakeword venv on this host: say so instead of failing silently
        write_status(status="failed", error=f"wakeword venv python not found: {py}")
        return
    cmd = ["nice", "-n", "10", py, str(ROOT / "wakeword/retrain.py"), "--clips", str(DIR / "clips"), "--model", str(MODEL),
           "--data", str(HOME / "wake-train")]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TRAIN_TIMEOUT_S)  # nice: the Mac stays usable
    except subprocess.TimeoutExpired:
        write_status(status="failed", error=f"training took longer than {TRAIN_TIMEOUT_S} s")
        return
    except OSError as e:
        write_status(status="failed", error=f"could not start training: {e}")
        return
    result = next((json.loads(line[7:]) for line in r.stdout.splitlines() if line.startswith("RESULT ")), None)
    if r.returncode == 0 and result:
        write_status(status="ready", trained_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     improved=result["improved"], metrics=result)
    else:
        write_status(status="failed", error=r.stderr[-500:])


def train_loop():
    """Retrain until no clip arrived while training. `running` is reset whatever happens, so the status can
    never stick at "training"."""
    try:
        while True:
            with lock:
                state["dirty"] = False
            try:
                train_once()
            except Exception as e:  # e.g. the status file could not be written
                write_status(status="failed", error=f"{type(e).__name__}: {e}"[:500])
            with lock:
                if not state["dirty"]:
                    return
    finally:
        with lock:
            state["running"] = False


def start_training():
    with lock:
        if state["running"]:
            state["dirty"] = True
            return
        state["running"] = True
    threading.Thread(target=train_loop, daemon=True).start()


@router.post("/enroll")
async def enroll(clips: list[UploadFile]):
    if len(clips) > MAX_CLIPS:
        raise HTTPException(413, f"at most {MAX_CLIPS} clips per request")
    blobs = [await up.read(MAX_BYTES + 1) for up in clips]  # never buffer more than one clip's worth each
    with lock:  # count, store and start training as one step: DELETE and other POSTs cannot interleave
        private_dir(HOME)
        private_dir(DIR)
        private_dir(DIR / "clips")
        if (HOME / "wake-train").is_dir():
            (HOME / "wake-train").chmod(0o700)
        have = len(list((DIR / "clips").glob("*.wav")))
        accepted, rejected = 0, []
        for i, data in enumerate(blobs):
            reason = ("too large" if len(data) > MAX_BYTES else check_clip(data)) or (
                "too many clips" if have + accepted >= MAX_CLIPS else None)
            if reason:
                rejected.append({"index": i, "reason": reason})
                continue
            p = DIR / "clips" / f"{uuid.uuid4().hex}.wav"
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
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
    with lock:
        if state["running"]:
            raise HTTPException(409, "training in progress")
        for f in [*(DIR / "clips").glob("*.wav"), DIR / "status.json", DIR / "heldout.json", MODEL.with_name("azel.prev.onnx")]:
            f.unlink(missing_ok=True)
        base = MODEL.with_name("azel.base.onnx")
        if base.exists():  # forget the user's voice: back to the generic model
            os.replace(base, MODEL)
    return Response(status_code=204)
