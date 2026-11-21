"""Build the verifier's negatives pool: everyday/near-miss clips plus other voices saying 아젤.

usage: python make_negatives.py --out ~/.config/biseo/wake-negatives [--n-neg 300] [--n-spk 300]
"""
import argparse
import random
import shutil
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--out", required=True)
ap.add_argument("--neg", default="clips/azel/negative_train")
ap.add_argument("--speakers", default="clips/azel_speakers")
ap.add_argument("--n-neg", type=int, default=300)
ap.add_argument("--n-spk", type=int, default=300)
a = ap.parse_args()
rng = random.Random(3)
out = Path(a.out).expanduser()
out.mkdir(parents=True, exist_ok=True)
out.chmod(0o700)
neg = sorted(Path(a.neg).glob("*.wav"))
spk = sorted(p for d in Path(a.speakers).iterdir() if d.is_dir() and not d.name.endswith("_rejected") for p in d.glob("*.wav"))
for tag, files, n in [("neg", neg, a.n_neg), ("spk", spk, a.n_spk)]:
    for f in rng.sample(files, min(n, len(files))):
        shutil.copy(f, out / f"{tag}_{f.name}")
        (out / f"{tag}_{f.name}").chmod(0o600)
print(len(list(out.glob("*.wav"))), "clips in", out)
