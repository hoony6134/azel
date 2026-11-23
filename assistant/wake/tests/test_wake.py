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
                       "in_call_path": str(tmp_path / "in-call"), "enrolling_path": str(tmp_path / "enrolling"),
                       "utterance_dir": str(tmp_path / "azel"), "mic_live_path": str(tmp_path / "mic-live"),
                       "listener_mic_path": str(tmp_path / "listener-mic")} | cfg
    extra = {k: v for k, v in {"wall": wall, "open_url": open_url}.items() if v} | {"spawn": lambda f, *a: f(*a)}
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
    assert not bw.use_app(cfg | {"handoff": "headless"})  # only an explicit headless answers on the Mac
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
    lis, clock, played, _, _ = make(tmp_path, handler=h, handoff="app",
                                    open_url=lambda u: opened.append(u) or True)
    lis.run(frames(clock, [(WAKE, 1), *utterance(), (SIL, 40)]))
    assert len(opened) == 1 and opened[0].startswith("azel://wake?persona=sohee_x&utterance=")
    assert played == [16000]  # the ack only (no /v1/turn, no reply audio)
    assert [r.url.path for r in reqs] == ["/v1/personas"]
    assert log_lines(tmp_path)[0]["handoff"] == "app"


def test_persona_falls_back_to_azel_and_a_failed_open_never_goes_headless(tmp_path):
    lis, clock, _, _, requests = make(tmp_path, handoff="app", open_url=lambda u: False)
    # the default handler answers every URL with the voice-turn stream, so /v1/personas has no "primary" -> azel
    assert bw.primary_persona(lis.cfg, lis.http) == "azel"
    requests.clear()
    lis.run(frames(clock, [(WAKE, 1), *utterance(), (SIL, 40)]))
    assert not any(r.url.path == "/v1/turn" for r in requests)  # no headless fallback in app mode
    assert log_lines(tmp_path)[0]["error"] == "open azel://wake failed"


def test_deaf_for_a_few_seconds_after_handoff(tmp_path):
    app = tmp_path / "Azel.app"
    app.mkdir()
    opened = []
    lis, clock, _, _, _ = make(tmp_path, handoff="app", open_url=lambda u: opened.append(u) or True)
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


# ---------- listen only while the app runs ----------
class FakeMic:
    def __init__(self, gen):
        self.gen, self.closed, self.flushes = gen, False, 0

    def __iter__(self):
        try:
            yield from self.gen
        finally:
            self.closed = True  # what leaving Mic.__iter__'s `with stream` does: the input stream is released

    def flush(self):
        self.flushes += 1


def test_gated_stops_when_the_app_quits_and_does_not_pull_more_frames(tmp_path):
    lis, clock, *_ = make(tmp_path)
    up, pulled = [True], []

    def src():
        for f in frames(clock, [(SIL, 100)]):
            pulled.append(1)
            yield f
    out = []
    for f in bw.gated(src(), lis.cfg, lambda c: up[0], clock):
        out.append(f)
        if len(out) == 20:
            up[0] = False  # the app quits; noticed at the next 2 s check
    assert 20 <= len(out) <= 34 and len(pulled) <= len(out) + 1


def test_supervise_opens_the_mic_only_while_the_app_runs_and_resumes(tmp_path, capsys):
    lis, clock, played, _, _ = make(tmp_path, handoff="app", open_url=lambda u: True)
    states = iter([False, False, True, False, False, True, True])  # not running, running, quit, running again
    opened, mics, slept = [], [], []

    def open_mic():
        opened.append(1)
        m = FakeMic(frames(clock, [(SIL, 3), (WAKE, 1), (SIL, 40)]))
        mics.append(m)
        return m
    seq = []

    def running(cfg):
        seq.append(next(states, False))
        return seq[-1]
    done = []

    def stop():
        done.append(1)
        return len(done) > 9
    bw.supervise(lis.cfg, lis, open_mic, running, sleep=slept.append, stop=stop)
    assert opened and all(m.closed for m in mics)  # every stream was released
    assert len(opened) == 2  # one per run of the app, none while it was down
    assert slept and set(slept) == {lis.cfg["app_check_s"]}
    out = capsys.readouterr().out.splitlines()
    assert out.count("app not running: mic closed") >= 1 and out.count("app running: listening") >= 1
    # state changes are logged once each, not on every check
    events = [l["event"] for l in log_lines(tmp_path) if "event" in l]
    assert all(a != b for a, b in zip(events, events[1:]))
    assert any(l.get("handoff") == "app" for l in log_lines(tmp_path))  # a wake while running still hands off


def test_app_running_matches_the_bundle_path(monkeypatch):
    seen = []
    monkeypatch.setattr(bw.subprocess, "run", lambda argv, **kw: seen.append(argv) or type("R", (), {"returncode": 0})())
    assert bw.app_running({"app_path": "/Applications/Azel.app"})
    assert seen[0][:2] == ["pgrep", "-f"] and seen[0][2] == "^/Applications/Azel\\.app/Contents/MacOS/"


def uid_of(url):
    return url.split("utterance=")[1]


def test_one_breath_command_is_captured_after_the_wake_word_and_handed_over(tmp_path):
    opened = []
    lis, clock, played, flushed, requests = make(tmp_path, handoff="app", open_url=lambda u: opened.append(u) or True)
    lis.run(frames(clock, [(SIL, 3), (WAKE, 1), (SIL, 1), (SPEECH, 5), (SIL, 5), (SIL, 40)]))
    f = tmp_path / "azel" / f"wake-utterance-{uid_of(opened[0])}.pcm"
    pcm = np.frombuffer(f.read_bytes(), dtype="<i2")
    assert f.stat().st_mode & 0o777 == 0o600
    assert (pcm == SPEECH).sum() == 5 * FRAME           # the command, not the wake word
    assert (pcm == WAKE).sum() == 0
    assert played == [16000] and not any(r.url.path == '/v1/turn' for r in requests)  # the ack, no headless turn
    assert flushed == []  # the mic is not flushed: the command being said is kept
    assert log_lines(tmp_path)[0]["utterance"] == uid_of(opened[0])


def test_preroll_keeps_the_frame_before_speech_starts(tmp_path):
    opened = []
    lis, clock, *_ = make(tmp_path, handoff="app", open_url=lambda u: opened.append(u) or True)
    lis.run(frames(clock, [(WAKE, 1), (SPEECH, 4), (SIL, 40)]))
    pcm = np.frombuffer((tmp_path / "azel" / f"wake-utterance-{uid_of(opened[0])}.pcm").read_bytes(), "<i2")
    assert len(pcm) == 6 * FRAME and (pcm[:FRAME] == WAKE).all()  # wake frame + 4 speech + 1 quiet frame (tail trimmed)


def test_a_short_pause_mid_command_does_not_end_the_recording(tmp_path):
    """"오늘 아침 | 컴파일러 프로젝트 관련 일정 삭제해" (2026-11-23): an 0.8 s pause mid-command was the end; now
    only a pause of utterance_silence_ms is, and its silence is trimmed so it can't end the server's turn early."""
    opened = []
    lis, clock, *_ = make(tmp_path, handoff="app", open_url=lambda u: opened.append(u) or True)
    lis.run(frames(clock, [(WAKE, 1), (SPEECH, 4), (SIL, 5), (SPEECH, 6), (SIL, 40)]))
    pcm = np.frombuffer((tmp_path / "azel" / f"wake-utterance-{uid_of(opened[0])}.pcm").read_bytes(), "<i2")
    assert (pcm == SPEECH).sum() == 10 * FRAME, "both halves"
    assert (pcm[-2 * FRAME:] == SPEECH).sum() >= FRAME, "at most one quiet frame after the last word"


def test_utterance_file_is_empty_when_nobody_speaks(tmp_path):
    opened = []
    lis, clock, *_ = make(tmp_path, handoff="app", open_url=lambda u: opened.append(u) or True)
    lis.run(frames(clock, [(WAKE, 1), (SIL, 40)]))
    assert (tmp_path / "azel" / f"wake-utterance-{uid_of(opened[0])}.pcm").read_bytes() == b""


def test_capture_ends_when_the_app_mic_is_live(tmp_path):
    opened = []
    lis, clock, *_ = make(tmp_path, handoff="app", open_url=lambda u: opened.append(u) or True)

    def stream():
        yield from frames(clock, [(WAKE, 1), (SPEECH, 3)])
        (tmp_path / "mic-live").write_text("")  # the app's mic is live: it hears the rest itself
        yield from frames(clock, [(SPEECH, 5), (SIL, 40)])

    lis.run(stream())
    pcm = np.frombuffer((tmp_path / "azel" / f"wake-utterance-{uid_of(opened[0])}.pcm").read_bytes(), "<i2")
    assert len(pcm) == 5 * FRAME  # wake frame as pre-roll + 3 speech + the frame that showed mic-live


def test_stale_utterance_files_are_swept(tmp_path):
    lis, *_ = make(tmp_path, handoff="app")
    d = tmp_path / "azel"
    d.mkdir()
    old = d / "wake-utterance-old.pcm"
    old.write_bytes(b"x")
    import os
    os.utime(old, (1, 1))
    lis.write_utterance("new", np.zeros(4, np.int16))
    assert not old.exists() and (d / "wake-utterance-new.pcm").exists()


def test_detections_are_ignored_while_the_app_enrolls_but_not_when_stale(tmp_path):
    opened = []
    wall = lambda: 1_000_000.0
    lis, clock, *_ = make(tmp_path, handoff="app", wall=wall, open_url=lambda u: opened.append(u) or True)
    flag = tmp_path / "enrolling"
    flag.write_text("")
    import os
    os.utime(flag, (wall() - 60, wall() - 60))
    lis.run(frames(clock, [(WAKE, 1), (SPEECH, 3), (SIL, 10)]))
    assert opened == []
    os.utime(flag, (wall() - 1801, wall() - 1801))
    lis.run(frames(clock, [(WAKE, 1), (SPEECH, 3), (SIL, 10)]))
    assert len(opened) == 1


def test_utterance_off_keeps_the_ack_and_the_plain_url(tmp_path):
    opened = []
    lis, clock, played, *_ = make(tmp_path, handoff="app", utterance=False, open_url=lambda u: opened.append(u) or True)
    lis.run(frames(clock, [(WAKE, 1), (SPEECH, 3), (SIL, 10)]))
    assert played == [16000] and opened == ["azel://wake?persona=azel"] and not (tmp_path / "azel").exists()


def test_part_file_exists_while_capturing_and_is_renamed_when_done(tmp_path):
    seen = []
    lis, clock, *_ = make(tmp_path, handoff="app", open_url=lambda u: True)

    def stream():
        yield from frames(clock, [(WAKE, 1), (SPEECH, 2)])
        seen.append(sorted(p.name[-9:] for p in (tmp_path / "azel").iterdir()))
        yield from frames(clock, [(SIL, 40)])

    lis.run(stream())
    assert seen == [[".pcm.part"]]
    assert [p.suffix for p in (tmp_path / "azel").iterdir()] == [".pcm"]


def test_unwritable_utterance_dir_hands_over_without_the_file(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("")  # a file where the folder should be: mkdir fails
    opened = []
    lis, clock, *_ = make(tmp_path, handoff="app", utterance_dir=str(blocker / "azel"),
                          open_url=lambda u: opened.append(u) or True)
    lis.run(frames(clock, [(WAKE, 1), (SPEECH, 3), (SIL, 10)]))
    assert opened == ["azel://wake?persona=azel"]  # no utterance= the app would wait for
    assert "utterance file failed" in capsys.readouterr().out and "utterance" not in log_lines(tmp_path)[0]


def test_ack_plays_at_once_in_one_breath_mode_and_the_command_is_still_captured(tmp_path):
    order, opened = [], []
    lis, clock, played, flushed, _ = make(tmp_path, handoff="app", open_url=lambda u: order.append("open") or opened.append(u) or True)
    real = lis._play
    lis._play = lambda pcm, sr: order.append("ack") or real(pcm, sr)
    lis.run(frames(clock, [(WAKE, 1), (SPEECH, 4), (SIL, 40)]))
    assert order == ["ack", "open"]  # heard before the app is asked to open
    pcm = np.frombuffer((tmp_path / "azel" / f"wake-utterance-{opened[0].split('utterance=')[1]}.pcm").read_bytes(), "<i2")
    assert (pcm == SPEECH).sum() == 4 * FRAME and flushed == []


def test_without_a_fetched_ack_the_system_beep_plays_in_one_breath_mode(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(bw.subprocess, "run", lambda argv, **k: ran.append(argv))
    lis, clock, played, *_ = make(tmp_path, handoff="app", open_url=lambda u: True)
    lis.ack = None
    lis.run(frames(clock, [(WAKE, 1), (SIL, 40)]))
    assert played == [] and ran == [["afplay", "/System/Library/Sounds/Tink.aiff"]]


def test_supervise_releases_the_mic_for_a_call_and_marks_it_only_while_open(tmp_path):
    lis, clock, *_ = make(tmp_path, handoff="app", open_url=lambda u: True)
    marker, call = tmp_path / "listener-mic", tmp_path / "in-call"
    seen, mics, slept = [], [], []

    def open_mic():
        seen.append(marker.exists())  # marked before the stream opens

        def gen():
            yield from frames(clock, [(SIL, 3)])
            call.write_text("")  # the app's call starts
            yield from frames(clock, [(SIL, 40)])  # must not be pulled: the stream closes on the next frame
        m = FakeMic(gen())
        mics.append(m)
        return m

    def sleep(s):
        slept.append((s, marker.exists()))
        if len(slept) == 3:
            call.unlink()  # the call ends: the listener opens the mic again

    n = []
    bw.supervise(lis.cfg, lis, open_mic, running=lambda c: True, sleep=sleep, stop=lambda: n.append(1) or len(n) > 6)
    assert seen == [True, True] and all(m.closed for m in mics)
    assert slept[:3] == [(lis.cfg["busy_check_s"], False)] * 3  # released (marker gone) for the whole call
    assert not marker.exists()
