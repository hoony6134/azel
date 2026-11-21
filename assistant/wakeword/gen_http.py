"""Generate + filter wake-word clips through the ALREADY RUNNING Spark services.

Loading another TTS model on the Spark (next to the 125B Qwen) ran the GPU out of
memory and rebooted it twice on 2026-11-21, so this uses only the live TTS server
(9 speakers + style instructions) and the voice server's Whisper, one request at a time.

usage: uv run --project ../client gen_http.py --out clips/azel --n-train 2500 --n-test 400
Resumable: each split tops up to its target. Writes 16 kHz mono PCM16 WAVs.
"""
import argparse
import io
import random
import time
import uuid
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf

from gen_clips import leaked, negative_text, positive_text
from filter_clips import close_to

TTS = "http://100.64.0.11:8890/v1/audio/speech"
STT = "http://100.64.0.11:8891/v1/audio/transcriptions"
SPEAKERS = ["sohee", "vivian", "serena", "ono_anna", "ryan", "aiden", "eric", "dylan", "uncle_fu"]
STYLES = ["", "", "빠르게 말해", "천천히 말해", "작게 속삭이듯 말해", "멀리 있는 사람을 부르듯 크게 말해",
          "피곤한 목소리로 말해", "무뚝뚝하게 말해", "다정하게 말해", "졸린 목소리로 말해",
          "궁금한 듯 물어보듯 말해", "급하게 말해", "짜증 난 듯 말해", "웅얼거리듯 말해"]


def to16k(wav, sr):
    n = int(len(wav) * 16000 / sr)
    wav = np.interp(np.linspace(0, len(wav) - 1, n), np.arange(len(wav)), wav)
    nz = np.flatnonzero(np.abs(wav) > 0.02)  # trim leading/trailing silence
    wav = wav[max(nz[0] - 1600, 0): nz[-1] + 1600] if nz.size else wav
    return (wav / max(np.abs(wav).max(), 1e-6) * 0.9 * 32767).astype(np.int16)


def fill(c, token, out: Path, target, make_text, positive, wake, rng):
    out.mkdir(parents=True, exist_ok=True)
    rejected = out.parent / f"{out.name}_rejected"
    rejected.mkdir(exist_ok=True)
    have = len(list(out.glob("*.wav")))
    while have < target:
        try:
            have += one_clip(c, token, out, rejected, make_text, positive, wake, rng)
        except (httpx.HTTPError, KeyError, ValueError) as e:  # a server restarting: wait and go on
            print(f"retry after {type(e).__name__}", flush=True)
            time.sleep(15)


def one_clip(c, token, out, rejected, make_text, positive, wake, rng, voices=SPEAKERS):
    """Generate one clip; return 1 if it was kept, 0 if rejected or unusable."""
    if True:
        text = make_text(rng)
        body = {"input": text, "voice": rng.choice(voices), "response_format": "wav", "language": "Korean"}
        if style := rng.choice(STYLES):
            body["instructions"] = style
        r = c.post(TTS, json=body)
        r.raise_for_status()
        wav, sr = sf.read(io.BytesIO(r.content), dtype="float32")
        pcm = to16k(wav, sr)
        if not 0.25 * 16000 <= len(pcm) <= 3.0 * 16000:
            return 0
        buf = io.BytesIO()
        sf.write(buf, pcm, 16000, format="WAV", subtype="PCM_16")
        heard = c.post(STT, files={"file": ("c.wav", buf.getvalue(), "audio/wav")},
                       headers={"Authorization": f"Bearer {token}"}).json()["text"]
        ok = close_to(wake, heard, tolerance=1 if positive else 0) == positive
        name = f"{uuid.uuid4().hex}.wav"
        (out / name if ok else rejected / name).write_bytes(buf.getvalue())
        if ok and len(list(out.glob("*.wav"))) % 50 == 0:
            print(f"{out.name}: {len(list(out.glob('*.wav')))}", flush=True)
        return int(ok)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--wake", default="아젤")
    ap.add_argument("--n-train", type=int, default=2500)
    ap.add_argument("--n-test", type=int, default=400)
    args = ap.parse_args()
    assert not leaked(args.wake), leaked(args.wake)
    token = (Path.home() / ".config/biseo/token").read_text().strip()
    rng, root = random.Random(1), Path(args.out)
    pos = lambda r: positive_text(args.wake, r)  # noqa: E731
    with httpx.Client(timeout=60) as c:
        for split, n, make, positive in [("positive_test", args.n_test, pos, True),
                                         ("negative_test", args.n_test, negative_text, False),
                                         ("positive_train", args.n_train, pos, True),
                                         ("negative_train", args.n_train, negative_text, False)]:
            fill(c, token, root / split, n, make, positive, args.wake, rng)


if __name__ == "__main__":
    main()
