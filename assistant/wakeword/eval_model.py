"""Score a trained wake model (and optional verifier) on held-out clips; print a threshold table.

usage (wake venv): ../wake/.venv/bin/python eval_model.py --model azel.onnx [--verifier v.pkl] \
    --pos clips/azel/positive_test --neg clips/azel/negative_test --near clips/azel_eval_neg/near \
    --everyday clips/azel_eval_neg/everyday [--user DIR] [--fp-features validation_set_features.npy]
"""
import argparse
from pathlib import Path

import numpy as np
import soundfile as sf

THRESHOLDS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]


def scores(model_path, verifier, files, gate):
    from openwakeword.model import Model
    kw = {"custom_verifier_models": {Path(model_path).stem: verifier}, "custom_verifier_threshold": gate} if verifier else {}
    m = Model(wakeword_models=[model_path], inference_framework="onnx", **kw)
    out = []
    for f in files:
        pcm, sr = sf.read(f, dtype="int16")
        assert sr == 16000
        m.reset()
        out.append(max(max(p.values()) for p in m.predict_clip(pcm)))
    return np.array(out)


def fp_per_hour(model_path, feats, hours=11.3, refractory=25):
    """Detections per hour on the 11.3 h background feature set (windows of 16 frames, 2 s refractory)."""
    import onnxruntime as ort
    s = ort.InferenceSession(model_path)
    name = s.get_inputs()[0].name
    x = np.load(feats, mmap_mode="r")
    x = np.ascontiguousarray(x, dtype=np.float32)  # the exported model takes one window per call
    sc = np.array([s.run(None, {name: x[i:i + 16][None]})[0].item() for i in range(len(x) - 15)])
    res = {}
    for t in THRESHOLDS:
        hits = np.flatnonzero(sc >= t)
        events = 0 if hits.size == 0 else 1 + int((np.diff(hits) > refractory).sum())
        res[t] = events / hours
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--verifier")
    ap.add_argument("--gate", type=float, default=0.3)
    ap.add_argument("--fp-features")
    for k in ["pos", "neg", "near", "everyday", "user"]:
        ap.add_argument(f"--{k}")
    a = ap.parse_args()
    sets = {k: scores(a.model, a.verifier, sorted(Path(getattr(a, k)).glob("*.wav")), a.gate)
            for k in ["pos", "neg", "near", "everyday", "user"] if getattr(a, k)}
    print("set (n)".ljust(18) + "".join(f"t={t:<7}" for t in THRESHOLDS))
    for k, s in sets.items():
        rate = lambda t: (s >= t).mean() if k != "neg" or True else 0  # noqa: E731
        print(f"{k + ' (' + str(len(s)) + ')':<18}" + "".join(f"{rate(t):<9.3f}" for t in THRESHOLDS),
              "<- recall" if k in ("pos", "user") else "<- false accepts")
    if a.fp_features:
        r = fp_per_hour(a.model, a.fp_features)
        print("fp/hour".ljust(18) + "".join(f"{r[t]:<9.2f}" for t in THRESHOLDS))


if __name__ == "__main__":
    main()
