from pathlib import Path
import base64
import json

import httpx
import numpy as np
import pytest

import biseo_wake as bw
from biseo_wake import FRAME, FRAME_S, Listener

WAKE, SPEECH, SIL = 1000, 500, 0


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


class FakeOww:
    resets = 0

    def predict(self, frame):
        return {"m": 0.9 if frame[0] == WAKE else 0.01}

    def reset(self):
        self.resets += 1


class FakeVad:
    def __call__(self, chunk: bytes):
        return 1.0 if any(chunk) else 0.0

    def reset(self):
        pass


def frames(clock, spec):
    """spec: [(value, count), ...] -> FRAME-sized int16 frames, advancing the fake clock in real time."""
    for value, count in spec:
        for _ in range(count):
            clock.t += FRAME_S
            yield np.full(FRAME, value, dtype=np.int16)


def make(tmp_path, handler=None, wall=None, open_url=None, **cfg):
    clock, played, flushed, requests = Clock(), [], [], []

    def default_handler(req):
        requests.append(req)
        n = len(requests)
        body = [{"type": "transcript", "text": f"질문{n}"},
                {"type": "audio", "text": f"답{n}", "sr": 24000,
                 "pcm16": base64.b64encode(np.zeros(2400, np.int16).tobytes()).decode()},
                {"type": "done", "timings": {}}]
        return httpx.Response(200, text="\n".join(json.dumps(e, ensure_ascii=False) for e in body))

    def play(pcm, sr):
        played.append(sr)
        clock.t += len(pcm) / sr

    http = httpx.Client(transport=httpx.MockTransport(handler or default_handler),
                        headers={"Authorization": "Bearer t"})
    c = bw.DEFAULTS | {"log_path": str(tmp_path / "wake.jsonl"), "handoff": "headless",
                       "in_call_path": str(tmp_path / "in-call")} | cfg
    extra = {k: v for k, v in {"wall": wall, "open_url": open_url}.items() if v}
    lis = Listener(c, FakeOww(), FakeVad(), http, play, ack=(np.zeros(8000, np.int16), 16000),
                   flush=lambda: flushed.append(1), clock=clock, **extra)
    return lis, clock, played, flushed, requests


def log_lines(tmp_path):
    return [json.loads(l) for l in (tmp_path / "wake.jsonl").read_text().splitlines()]


def utterance():
    return [(SIL, 3), (SPEECH, 5), (SIL, 5)]


def test_full_cycle_wake_ack_record_request_followup_back_to_wake(tmp_path):
    lis, clock, played, _, requests = make(tmp_path)
    spec = [(SIL, 3), (WAKE, 1), *utterance(),  # wake -> ack -> first request
            *utterance(),                         # follow-up without the wake word
            (SIL, 40),                            # 6.4 s of nothing: window closes
            (SPEECH, 5), (SIL, 5),                # speech without the wake word is ignored now
            (WAKE, 1), (SIL, 40)]                 # a second wake with no request
    lis.run(frames(clock, spec))

    assert len(requests) == 2
    sessions = {r.url.params["session"] for r in requests}
    assert len(sessions) == 1  # stable for the conversation
    assert all(r.headers["Authorization"] == "Bearer t" for r in requests)
    assert played == [16000, 24000, 24000, 16000]  # ack, reply, reply, ack
    lines = log_lines(tmp_path)
    assert [(l["request"], l["transcript"]) for l in lines] == [(True, "질문1"), (False, None)]
    assert lines[0]["score"] == 0.9 and lines[0]["model"] == "hey_jarvis"


def test_recorded_request_is_wav_with_the_speech(tmp_path):
    lis, clock, _, _, requests = make(tmp_path)
    lis.run(frames(clock, [(WAKE, 1), *utterance(), (SIL, 40)]))
    import io, soundfile as sf
    pcm, sr = sf.read(io.BytesIO(requests[0].content), dtype="int16")
    assert sr == 16000 and (pcm == SPEECH).sum() == 5 * FRAME
    assert len(pcm) <= (2 + 5 + 4) * FRAME  # pre-roll + speech + 0.6 s silence tail


def test_no_request_after_wake_returns_to_wake(tmp_path):
    lis, clock, _, _, requests = make(tmp_path)
    lis.run(frames(clock, [(WAKE, 1), (SIL, 40), (WAKE, 1), (SIL, 40)]))
    assert requests == []
    assert [l["request"] for l in log_lines(tmp_path)] == [False, False]


def test_max_record_length(tmp_path):
    lis, clock, _, _, requests = make(tmp_path, max_record_s=1)
    lis.run(frames(clock, [(WAKE, 1), (SPEECH, 30), (SIL, 40)]))
    assert len(requests) >= 1


def test_self_listening_pause(tmp_path):
    lis, clock, played, flushed, _ = make(tmp_path)
    resets = lis.oww.resets
    lis.play(np.zeros(16000, np.int16), 16000)  # 1 s of our own audio
    assert flushed and lis.oww.resets == resets + 1  # mic backlog dropped, detector cleared
    # a "wake word" 160 ms after playback ends is our own echo: ignored
    lis.run(frames(clock, [(WAKE, 1)]))
    assert not (tmp_path / "wake.jsonl").exists()
    # past the 300 ms window it counts
    lis.run(frames(clock, [(SIL, 2), (WAKE, 1), (SIL, 40)]))
    assert len(log_lines(tmp_path)) == 1


def test_server_error_is_logged_not_fatal(tmp_path):
    lis, clock, *_ = make(tmp_path, handler=lambda req: httpx.Response(503))
    lis.beep = lambda: lis.mute()
    lis.run(frames(clock, [(WAKE, 1), *utterance(), (WAKE, 1), (SIL, 40)]))
    lines = log_lines(tmp_path)
    assert len(lines) == 2 and "503" in lines[0]["error"] and lines[0]["request"] is False


def test_log_has_no_audio_by_default(tmp_path):
    lis, clock, *_ = make(tmp_path)
    lis.run(frames(clock, [(WAKE, 1), (SIL, 40)]))
    assert "audio" not in log_lines(tmp_path)[0]
    assert not (tmp_path / "wake-audio").exists()


def test_save_audio_true_writes_clip(tmp_path):
    lis, clock, *_ = make(tmp_path, save_audio=True)
    lis.run(frames(clock, [(SIL, 5), (WAKE, 1), (SIL, 40)]))
    p = log_lines(tmp_path)[0]["audio"]
    assert p.startswith(str(tmp_path / "wake-audio")) and p.endswith(".wav")


def test_config_defaults_and_override(tmp_path):
    assert bw.load_config(tmp_path / "missing.toml") == bw.DEFAULTS
    f = tmp_path / "wake.toml"
    f.write_text('model = "~/models/azel.onnx"\nthreshold = 0.7\n')
    cfg = bw.load_config(f)
    assert cfg["model"] == "~/models/azel.onnx" and cfg["threshold"] == 0.7
    assert cfg["save_audio"] is False and cfg["followup_s"] == 6


def test_config_rejects_typos(tmp_path):
    f = tmp_path / "wake.toml"
    f.write_text("treshold = 0.7\n")
    with pytest.raises(ValueError, match="treshold"):
        bw.load_config(f)


def test_example_config_parses():
    from pathlib import Path
    cfg = bw.load_config(Path(__file__).parent.parent / "wake.example.toml")
    assert cfg["model"] == "hey_jarvis"


def test_watch_key_tracks_model_and_verifier_files(tmp_path):
    m, v = tmp_path / "m.onnx", tmp_path / "v.pkl"
    cfg = {"model": str(m), "verifier": str(v)}
    assert bw.watch_key(cfg) == (0, 0)  # absent
    m.write_bytes(b"x")
    v.write_bytes(b"x")
    k = bw.watch_key(cfg)
    assert k[0] > 0 and k[1] > 0
    assert bw.watch_key({"model": "hey_jarvis", "verifier": ""}) == (0, 0)  # built-in model, verifier off


def test_bad_reload_keeps_old_model_and_startup_falls_back(tmp_path, monkeypatch, capsys):
    m = tmp_path / "azel.onnx"
    m.write_bytes(b"good")
    cfg = {"model": str(m), "verifier": ""}
    l = Listener.__new__(Listener)
    l.cfg, l.oww = cfg, "old"
    seen = bw.watch_key(cfg)
    assert l.reload_if_changed(seen) == seen and l.oww == "old"  # nothing changed

    def load(c):
        if Path(c["model"]).read_bytes() == b"bad":
            raise RuntimeError("corrupt")
        return "model:" + Path(c["model"]).name
    monkeypatch.setattr(bw, "load_oww", load)
    m.write_bytes(b"bad")
    import os
    os.utime(m, (1, 1))  # a changed mtime
    seen2 = l.reload_if_changed(seen)
    assert l.oww == "old" and seen2 != seen and "reload failed" in capsys.readouterr().out
    assert l.reload_if_changed(seen2) == seen2  # not retried until it changes again
    (tmp_path / "azel.prev.onnx").write_bytes(b"good")
    assert bw.load_oww_or_fallback(cfg) == "model:azel.prev.onnx"
    (tmp_path / "azel.base.onnx").write_bytes(b"good")
    assert bw.load_oww_or_fallback(cfg) == "model:azel.base.onnx"
    (tmp_path / "azel.base.onnx").unlink(), (tmp_path / "azel.prev.onnx").unlink()
    with pytest.raises(RuntimeError):
        bw.load_oww_or_fallback(cfg)


# ---------- hand the call to the app, and stay deaf while it is in a call ----------
def personas_handler(requests, body):
    def h(req):
        requests.append(req)
        return httpx.Response(200, json=body) if req.url.path == "/v1/personas" else httpx.Response(500)
    return h


def test_handoff_decision(tmp_path):
    app = tmp_path / "Azel.app"
    app.mkdir()
    cfg = bw.DEFAULTS | {"app_path": str(app)}
    assert bw.use_app(cfg | {"handoff": "app"})
    assert not bw.use_app(cfg | {"handoff": "headless"})  # asked for headless
    assert not bw.use_app(cfg | {"handoff": "app", "app_path": str(tmp_path / "missing.app")})  # not installed
    assert bw.DEFAULTS["handoff"] == "app"
    bad = tmp_path / "wake.toml"
    bad.write_text('handoff = "carrier-pigeon"\n')
    with pytest.raises(ValueError):
        bw.load_config(bad)


def test_wake_opens_the_app_with_the_primary_persona_and_stays_headless_free(tmp_path):
    app = tmp_path / "Azel.app"
    app.mkdir()
    reqs, opened = [], []
    h = personas_handler(reqs, [{"id": "other", "primary": False},
                                {"id": "sohee_x", "primary": True, "deleted_at": None}])
    lis, clock, played, _, _ = make(tmp_path, handler=h, handoff="app", app_path=str(app),
                                    open_url=lambda u: opened.append(u) or True)
    lis.run(frames(clock, [(WAKE, 1), *utterance(), (SIL, 40)]))
    assert opened == ["azel://wake?persona=sohee_x"]
    assert played == [16000]  # the ack only; no /v1/turn, no reply audio
    assert [r.url.path for r in reqs] == ["/v1/personas"]
    assert log_lines(tmp_path)[0]["handoff"] == "app"


def test_persona_falls_back_to_azel_and_failed_open_falls_back_to_headless(tmp_path):
    app = tmp_path / "Azel.app"
    app.mkdir()
    lis, clock, _, _, requests = make(tmp_path, handoff="app", app_path=str(app), open_url=lambda u: False)
    # the default handler answers every URL with the voice-turn stream, so /v1/personas has no "primary" -> azel
    assert bw.primary_persona(lis.cfg, lis.http) == "azel"
    lis.run(frames(clock, [(WAKE, 1), *utterance(), (SIL, 40)]))
    assert len(requests) >= 1 and any(r.url.path == "/v1/turn" for r in requests)  # no handler: headless answered


def test_deaf_for_a_few_seconds_after_handoff(tmp_path):
    app = tmp_path / "Azel.app"
    app.mkdir()
    opened = []
    lis, clock, _, _, _ = make(tmp_path, handoff="app", app_path=str(app), open_url=lambda u: opened.append(u) or True)
    lis.run(frames(clock, [(WAKE, 1), (WAKE, 5)]))  # ~0.8 s later: still inside the 3 s
    assert len(opened) == 1
    lis.run(frames(clock, [(SIL, 30), (WAKE, 1)]))  # 4.8 s of quiet, then a real wake
    assert len(opened) == 2


def test_mic_ignored_while_the_app_is_in_a_call_but_not_when_stale(tmp_path):
    import os
    now = 1_000_000.0
    lock = tmp_path / "in-call"
    lock.write_text("")
    os.utime(lock, (now - 60, now - 60))
    lis, clock, played, _, _ = make(tmp_path, wall=lambda: now)
    assert lis.in_call()
    lis.run(frames(clock, [(WAKE, 3), (SIL, 3)]))
    assert played == [] and not (tmp_path / "wake.jsonl").exists()  # the assistant's own voice can't wake it
    os.utime(lock, (now - 3 * 3600, now - 3 * 3600))  # a leftover from a crashed call
    assert not lis.in_call()
    lis.run(frames(clock, [(WAKE, 1), (SIL, 40)]))
    assert played == [16000]
    lock.unlink()
    assert not lis.in_call()
