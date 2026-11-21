"""Train the personal 아젤 verifier on the Mac CPU (a variant of openWakeWord's custom verifier).

positives = frames of the user's enrollment clips where the base model fires; negatives = near-miss /
everyday clips and other voices saying 아젤 (a directory of 16 kHz WAVs, see make_negatives.py), keeping the
frames the verifier will actually see at run time (base score >= 0.1) plus each clip's best 3 frames.
openWakeWord's own trainer uses all frames and no class weights, which left the verifier near zero on the
user's own voice. Runs in the wake venv:

  ../wake/.venv/bin/python train_verifier.py --clips CLIPS --negatives NEG --model azel.onnx --out azel_verifier.pkl
"""
import argparse
import os
import pickle
import random
from pathlib import Path

import numpy as np
import scipy.io.wavfile
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler

STEP = 1280


def frame_features(oww, name, path, jitter):
    """[(base score, feature window)] for every 80 ms frame of a clip; jitter shifts the frame grid."""
    _, pcm = scipy.io.wavfile.read(path)
    pcm = pcm[random.randint(0, STEP - 1):] if jitter else pcm
    oww.reset()
    out = []
    for i in range(0, len(pcm) - STEP, STEP):
        score = oww.predict(pcm[i:i + STEP])[name]
        out.append((score, oww.preprocessor.get_features(oww.model_inputs[name])))
    return out


def train(clips, negatives, model, out, max_neg=400, seed=0, c=0.1, gate=0.3):
    import openwakeword
    from openwakeword.custom_verifier_model import flatten_features
    assert Path(model).is_file(), f"base wake model not found: {model} (train it first, see run_train.sh)"
    pos = sorted(Path(clips).glob("*.wav"))
    neg = sorted(Path(negatives).glob("*.wav"))
    assert pos, f"no enrollment clips in {clips}"
    assert neg, f"no negative clips in {negatives} (see make_negatives.py)"
    random.seed(seed)
    random.shuffle(neg)
    oww = openwakeword.Model(wakeword_models=[str(model)], inference_framework="onnx")
    name = Path(model).stem
    X, y = [], []
    for p in pos:
        for _ in range(5):
            X += [f[0] for s, f in frame_features(oww, name, p, True) if s >= gate]
    y = [1] * len(X)
    assert X, "the base model never fired on the enrollment clips: record them again, closer and clearer"
    for p in neg[:max_neg]:
        fr = frame_features(oww, name, p, False)
        keep = {i for i, (s, _) in enumerate(fr) if s >= 0.1} | set(np.argsort([s for s, _ in fr])[-3:].tolist())
        X += [fr[i][1][0] for i in keep]
        y += [0] * len(keep)
    clf = make_pipeline(FunctionTransformer(flatten_features), StandardScaler(),
                        LogisticRegression(C=c, class_weight="balanced", max_iter=2000, random_state=0))
    clf.fit(np.array(X), np.array(y))
    tmp = Path(f"{out}.tmp")
    tmp.write_bytes(pickle.dumps(clf))
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
