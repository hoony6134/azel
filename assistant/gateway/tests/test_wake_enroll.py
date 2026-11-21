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
        m.write_status(status="ready", trained_at="2026-11-21T00:00:00+00:00", improved=True, metrics={"recall_after": 1.0})
    m.train_once = fake_train
    app = FastAPI()
    app.include_router(m.router)
    return m, TestClient(app), tmp_path, runs


def test_enroll_validates_stores_and_trains(api):
    m, c, home, runs = api
    assert c.get("/v1/wake/enroll").json() == {"clips": 0, "min_clips": 20, "trained_at": None, "status": "none",
                                               "improved": None, "metrics": None, "error": None}
    (home / "azel.onnx").write_bytes(b"personal")
    (home / "azel.base.onnx").write_bytes(b"generic")
    bad = [b"junk", wav(0.2), wav(3.0), wav(sr=44100), wav(ch=2), wav(amp=1)]
    r = c.post("/v1/wake/enroll", files=files(*bad, wav(), wav())).json()
    assert r["accepted"] == 2 and r["training"] is False  # fewer than MIN_CLIPS: stored, not trained
    assert c.get("/v1/wake/enroll").json()["status"] == "none"
    assert [x["index"] for x in r["rejected"]] == [0, 1, 2, 3, 4, 5]
    r = c.post("/v1/wake/enroll", files=files(*[wav() for _ in range(18)])).json()
    assert r == {"accepted": 18, "rejected": [], "training": True}
    for _ in range(50):
        if not m.state["running"]:
            break
        time.sleep(0.05)
    s = c.get("/v1/wake/enroll").json()
    assert s == {"clips": 20, "min_clips": 20, "trained_at": "2026-11-21T00:00:00+00:00", "status": "ready",
                 "improved": True, "metrics": {"recall_after": 1.0}, "error": None} and runs
    assert stat.S_IMODE((home / "wake-enroll").stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in (home / "wake-enroll/clips").glob("*.wav"))
    assert c.delete("/v1/wake/enroll").status_code == 204
    assert c.get("/v1/wake/enroll").json()["status"] == "none"
    assert (home / "azel.onnx").read_bytes() == b"generic" and not (home / "azel.base.onnx").exists()


def test_clip_cap(api):
    m, c, *_ = api
    assert c.post("/v1/wake/enroll", files=files(*[wav() for _ in range(m.MAX_CLIPS)])).json()["accepted"] == m.MAX_CLIPS
    r = c.post("/v1/wake/enroll", files=files(wav(), wav())).json()
    assert r["accepted"] == 0 and r["rejected"][0]["reason"] == "too many clips"


def test_oversize_upload_and_request_caps(api):
    m, c, *_ = api
    r = c.post("/v1/wake/enroll", files=files(b"\0" * (m.MAX_BYTES + 5000), wav())).json()
    assert r["accepted"] == 1 and r["rejected"] == [{"index": 0, "reason": "too large"}]
    assert c.post("/v1/wake/enroll", files=files(*[wav(0.5)] * (m.MAX_CLIPS + 1))).status_code == 413


def test_training_never_sticks(tmp_path, monkeypatch):
    """Missing wake venv, a hung trainer, a crash and a corrupt status file all end in "failed", never "training"."""
    monkeypatch.setenv("BISEO_HOME", str(tmp_path))
    import wake_enroll
    m = importlib.reload(wake_enroll)
    m.private_dir(m.DIR)
    m.private_dir(m.DIR / "clips")
    (m.DIR / "clips" / "a.wav").write_bytes(wav())
    monkeypatch.setenv("BISEO_WAKE_PY", str(tmp_path / "no-such-python"))
    m.state["running"] = True
    m.train_loop()
    assert m.status()["status"] == "failed" and not m.state["running"]
    assert "not found" in m.read_status()["error"]
    py = tmp_path / "py"  # a "python" that hangs
    py.write_text("#!/bin/sh\nsleep 30\n")
    py.chmod(0o755)
    monkeypatch.setenv("BISEO_WAKE_PY", str(py))
    monkeypatch.setattr(m, "TRAIN_TIMEOUT_S", 1)
    m.state["running"] = True
    t = time.monotonic()
    m.train_loop()
    assert time.monotonic() - t < 10 and m.status()["status"] == "failed" and "longer than" in m.read_status()["error"]

    def boom():
        raise RuntimeError("disk full")
    monkeypatch.setattr(m, "train_once", boom)
    m.state["running"] = True
    m.train_loop()
    assert not m.state["running"] and m.status()["status"] == "failed"
    (m.DIR / "status.json").write_text("{not json")  # a crash mid-write
    assert m.status()["status"] == "failed"


def test_request_limits_and_truth_from_samples(api):
    m, c, home, runs = api
    big = c.post("/v1/wake/enroll", files=files(wav()), headers={"content-length": str(m.MAX_REQUEST + 1)})
    assert big.status_code == 413
    # the length comes from the samples read, not from a header that claims more frames than exist
    buf = io.BytesIO(wav(0.1))
    raw = bytearray(buf.getvalue())
    raw[40:44] = (16000 * 2 * 2).to_bytes(4, "little")  # data chunk says 2 s, only 0.1 s follow
    assert m.check_clip(bytes(raw)) == f"length must be {m.MIN_S}-{m.MAX_S} s"


def test_stale_training_status_reads_as_failed_and_delete_refuses_while_training(api):
    m, c, home, runs = api
    m.private_dir(m.DIR)
    (m.DIR / "clips").mkdir(exist_ok=True)
    (m.DIR / "clips" / "a.wav").write_bytes(wav())
    m.write_status(status="training")  # a leftover: the gateway died mid-training, nothing is running
    s = c.get("/v1/wake/enroll").json()
    assert s["status"] == "failed" and s["error"] == "학습이 중단됐어요"
    m.state["running"] = True
    assert c.delete("/v1/wake/enroll").status_code == 409  # checked under the lock, so it cannot slip past a starting POST
    m.state["running"] = False
    assert c.delete("/v1/wake/enroll").status_code == 204
