"""Speaker-labelled "아젤" clips for evaluating the personal verifier (one TTS voice per folder).
Same live-server, one-request-at-a-time rules as gen_http.py.

usage: ../client/.venv/bin/python gen_speakers.py --out clips/azel_speakers --per-speaker 40
"""
import argparse
import io
import random
import time
from pathlib import Path

import httpx
import soundfile as sf

import gen_http
from gen_clips import positive_text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-speaker", type=int, default=40)
    ap.add_argument("--wake", default="아젤")
    ap.add_argument("--neutral-tts-only", action="store_true",
                    help="plain wake word, no style, no Whisper filter: a person enrolling repeats one natural style")
    a = ap.parse_args()
    token = (Path.home() / ".config/biseo/token").read_text().strip()
    rng = random.Random(7)
    with httpx.Client(timeout=60) as c:
        for sp in gen_http.SPEAKERS:
            out = Path(a.out) / sp
            out.mkdir(parents=True, exist_ok=True)
            rejected = Path(a.out) / f"{sp}_rejected"
            rejected.mkdir(exist_ok=True)
            have = len(list(out.glob("*.wav")))
            while a.neutral_tts_only and have < a.per_speaker:
                try:
                    r = c.post(gen_http.TTS, json={"input": a.wake, "voice": sp, "response_format": "wav", "language": "Korean"})
                    r.raise_for_status()
                    wav, sr = sf.read(io.BytesIO(r.content), dtype="float32")
                    sf.write(out / f"{have}.wav", gen_http.to16k(wav, sr), 16000, subtype="PCM_16")
                    have += 1
                except (httpx.HTTPError, ValueError) as e:
                    print(f"retry after {type(e).__name__}", flush=True)
                    time.sleep(15)
            while have < a.per_speaker:
                try:
                    have += gen_http.one_clip(c, token, out, rejected, lambda r: positive_text(a.wake, r),
                                              True, a.wake, rng, voices=[sp])
                except (httpx.HTTPError, KeyError, ValueError) as e:
                    print(f"retry after {type(e).__name__}", flush=True)
                    time.sleep(15)
            print(sp, have, flush=True)


if __name__ == "__main__":
    main()
