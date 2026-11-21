"""Speaker-labelled "아젤" clips for evaluating the personal verifier (one TTS voice per folder).
Same live-server, one-request-at-a-time rules as gen_http.py.

usage: ../client/.venv/bin/python gen_speakers.py --out clips/azel_speakers --per-speaker 40
"""
import argparse
import random
import time
from pathlib import Path

import httpx

import gen_http
from gen_clips import positive_text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-speaker", type=int, default=40)
    ap.add_argument("--wake", default="아젤")
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
