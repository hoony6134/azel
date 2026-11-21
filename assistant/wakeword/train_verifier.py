"""Train the personal 아젤 verifier (openWakeWord custom verifier) on the Mac CPU.

positives = the user's enrollment clips; negatives = near-miss / everyday clips and other speakers
saying 아젤 (a directory of 16 kHz WAVs, see make_negatives.py). Runs in the wake venv:

  ../wake/.venv/bin/python train_verifier.py --clips CLIPS --negatives NEG --model azel.onnx --out azel_verifier.pkl
"""
import argparse
import os
import random
from pathlib import Path


def train(clips, negatives, model, out, max_neg=400, seed=0):
    from openwakeword.custom_verifier_model import train_custom_verifier
    pos = sorted(Path(clips).glob("*.wav"))
    neg = sorted(Path(negatives).glob("*.wav"))
    assert pos, f"no enrollment clips in {clips}"
    assert neg, f"no negative clips in {negatives}"
    random.Random(seed).shuffle(neg)
    tmp = Path(f"{out}.tmp")
    train_custom_verifier(positive_reference_clips=[str(p) for p in pos],
                          negative_reference_clips=[str(p) for p in neg[:max_neg]],
                          output_path=str(tmp), model_name=str(model), inference_framework="onnx")
    tmp.chmod(0o600)
    os.replace(tmp, out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", required=True)
    ap.add_argument("--negatives", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    train(a.clips, a.negatives, a.model, a.out)
