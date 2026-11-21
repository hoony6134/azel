import importlib
import array
import io
import random
import stat
import sys
import time
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))


def wav(seconds=1.0, sr=16000, ch=1, amp=8000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch), w.setsampwidth(2), w.setframerate(sr)
        w.writeframes(array.array("h", (int(random.uniform(-amp, amp)) for _ in range(int(seconds * sr) * ch))).tobytes())
    return buf.getvalue()


def files(*blobs):
    return [("clips", (f"c{i}.wav", b, "audio/wav")) for i, b in enumerate(blobs)]


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("BISEO_HOME", str(tmp_path))
    import wake_enroll
    m = importlib.reload(wake_enroll)
    runs = []

    def fake_train():
        runs.append(1)
        (m.VERIFIER).write_bytes(b"pkl")
        m.write_status(status="ready", trained_at="2026-11-21T00:00:00+00:00")
    m.train_once = fake_train
    app = FastAPI()
    app.include_router(m.router)
    return m, TestClient(app), tmp_path, runs


def test_enroll_validates_stores_and_trains(api):
    m, c, home, runs = api
    assert c.get("/v1/wake/enroll").json() == {"clips": 0, "trained_at": None, "status": "none"}
    bad = [b"junk", wav(0.2), wav(3.0), wav(sr=44100), wav(ch=2), wav(amp=1)]
    r = c.post("/v1/wake/enroll", files=files(*bad, wav(), wav())).json()
    assert r["accepted"] == 2 and r["training"] is False  # fewer than MIN_CLIPS: stored, not trained
    assert [x["index"] for x in r["rejected"]] == [0, 1, 2, 3, 4, 5]
    r = c.post("/v1/wake/enroll", files=files(wav(), wav(), wav())).json()
    assert r == {"accepted": 3, "rejected": [], "training": True}
    for _ in range(50):
        if not m.state["running"]:
            break
        time.sleep(0.05)
    s = c.get("/v1/wake/enroll").json()
    assert s == {"clips": 5, "trained_at": "2026-11-21T00:00:00+00:00", "status": "ready"} and runs
    assert stat.S_IMODE((home / "wake-enroll").stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in (home / "wake-enroll/clips").glob("*.wav"))
    assert c.delete("/v1/wake/enroll").status_code == 204
    assert c.get("/v1/wake/enroll").json()["status"] == "none" and not m.VERIFIER.exists()


def test_clip_cap(api):
    m, c, *_ = api
    r = c.post("/v1/wake/enroll", files=files(*[wav() for _ in range(m.MAX_CLIPS + 2)])).json()
    assert r["accepted"] == m.MAX_CLIPS and r["rejected"][0]["reason"] == "too many clips"
