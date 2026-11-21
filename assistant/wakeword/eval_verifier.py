"""Simulate enrollment with TTS voices: one voice plays "the user" (8 enrollment clips), the others are strangers.
Reports false-reject (user's held-out clips), other-speaker false-accept and near-miss false-accept per threshold.

usage (wake venv): python eval_verifier.py --model azel.onnx --speakers clips/azel_speakers --neg clips/azel/negative_train \
    --near clips/azel_eval_neg/near --users sohee,ryan,serena
"""
import argparse
import random
import shutil
import tempfile
from pathlib import Path

import numpy as np

from eval_model import scores
from train_verifier import train

T = [0.3, 0.5, 0.7, 0.8, 0.9]
ap = argparse.ArgumentParser()
ap.add_argument("--model", required=True)
ap.add_argument("--speakers", required=True)
ap.add_argument("--neg", required=True)
ap.add_argument("--near", required=True)
ap.add_argument("--users", required=True)
ap.add_argument("--gate", type=float, default=0.3)
ap.add_argument("--n-enroll", type=int, default=8)
ap.add_argument("--c", type=float, default=0.1)
a = ap.parse_args()
rng = random.Random(5)
root = Path(a.speakers)
clips = {d.name: sorted(d.glob("*.wav")) for d in root.iterdir() if d.is_dir() and not d.name.endswith("_rejected")}
neg = sorted(Path(a.neg).glob("*.wav"))
near = sorted(Path(a.near).glob("*.wav"))
rows = []
for user in a.users.split(","):
    mine = clips[user][:]
    rng.shuffle(mine)
    enroll, held = mine[:a.n_enroll], mine[a.n_enroll:]
    others = [f for k, v in clips.items() if k != user for f in v]
    rng.shuffle(others)
    half = len(others) // 2
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "enroll").mkdir(), (td / "neg").mkdir()
        for f in enroll:
            shutil.copy(f, td / "enroll" / f.name)
        for i, f in enumerate(rng.sample(neg, 300) + others[:half][:300]):
            shutil.copy(f, td / "neg" / f"{i}.wav")
        train(td / "enroll", td / "neg", a.model, td / "v.pkl", c=a.c)
        r = {k: scores(a.model, str(td / "v.pkl"), fs, a.gate)
             for k, fs in [("held", held), ("others", others[half:]), ("near", near)]}
        r["held_base"] = scores(a.model, None, held, a.gate)
    rows.append(r)
    print(user, {k: len(v) for k, v in r.items()}, flush=True)
print("threshold".ljust(34) + "".join(f"{t:<8}" for t in T))
for key, label in [("held_base", "base model alone false-reject"), ("held", "user false-reject"), ("others", "other-speaker false-accept"), ("near", "near-miss false-accept")]:
    for u, r in zip(a.users.split(","), rows):
        s = r[key]
        v = [1 - (s >= t).mean() if key.startswith("held") else (s >= t).mean() for t in T]
        print(f"{label} [{u}]".ljust(34) + "".join(f"{x:<8.3f}" for x in v))
