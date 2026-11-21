"""TTS-only negatives with known text for evaluation (no Whisper: we want every near-miss kept).
Writes <out>/near/<n>_<text>.wav (ADVERSARIAL phrases) and <out>/everyday/... ; one request at a time.

usage: ../client/.venv/bin/python gen_eval_negatives.py --out clips/azel_eval_neg --per-phrase 6
"""
import argparse
import io
import random
import time
from pathlib import Path

import httpx
import soundfile as sf

import gen_http
from gen_clips import ADVERSARIAL, EVERYDAY

ap = argparse.ArgumentParser()
ap.add_argument("--out", required=True)
ap.add_argument("--per-phrase", type=int, default=6)
a = ap.parse_args()
rng = random.Random(11)
with httpx.Client(timeout=60) as c:
    for kind, phrases in [("near", ADVERSARIAL), ("everyday", EVERYDAY)]:
        d = Path(a.out) / kind
        d.mkdir(parents=True, exist_ok=True)
        for p in phrases:
            for i in range(a.per_phrase):
                f = d / f"{p.replace(' ', '_')}__{i}.wav"
                while not f.exists():
                    body = {"input": p, "voice": rng.choice(gen_http.SPEAKERS), "response_format": "wav", "language": "Korean"}
                    if style := rng.choice(gen_http.STYLES):
                        body["instructions"] = style
                    try:
                        r = c.post(gen_http.TTS, json=body)
                        r.raise_for_status()
                        wav, sr = sf.read(io.BytesIO(r.content), dtype="float32")
                        sf.write(f, gen_http.to16k(wav, sr), 16000, subtype="PCM_16")
                    except (httpx.HTTPError, ValueError) as e:
                        print("retry", type(e).__name__, flush=True)
                        time.sleep(10)
        print(kind, len(list(d.glob("*.wav"))), flush=True)
