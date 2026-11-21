"""Retrain the wake model on the user's own voice, on this Mac (CPU only; the clips never leave it).

The user's enrollment clips (80% train / 20% held out) are augmented (gain, noise, room reverb, speed +-5%) into
extra positives, then the current azel.onnx is fine-tuned on them mixed with the generic TTS positives,
near-miss negatives and background features from `setup_train_data.sh`. The new model replaces azel.onnx
(old one kept as azel.prev.onnx) only if it is measurably better on the held-out user clips without
more false accepts; the metrics come back as one `RESULT {json}` line.

  uv run python retrain.py --clips ~/.config/biseo/wake-enroll/clips
"""
import argparse
import json
import os
import random
import shutil
import time
from pathlib import Path

import numpy as np
import soundfile as sf

SR, TOTAL, STEP = 16000, 32000, 1280
HOME = Path(os.environ.get("BISEO_HOME", Path.home() / ".config/biseo"))
LAYERS = ["layer1", "layernorm1", "blocks.0.fcn_layer", "blocks.0.layer_norm", "last_layer"]


# ---------- the model: the same tiny network openWakeWord's train.py builds ----------
def make_net():
    import torch.nn as nn

    class Net(nn.Module):
        def __init__(s):
            super().__init__()
            s.layer1, s.layernorm1 = nn.Linear(16 * 96, 32), nn.LayerNorm(32)
            s.fcn, s.ln2, s.last = nn.Linear(32, 32), nn.LayerNorm(32), nn.Linear(32, 1)

        def forward(s, x):
            x = nn.functional.relu(s.layernorm1(s.layer1(x.flatten(1))))
            return s.last(nn.functional.relu(s.ln2(s.fcn(x)))).sigmoid()  # (N, 1)
    return Net()


TORCH_NAMES = {"layer1": "layer1", "layernorm1": "layernorm1", "blocks.0.fcn_layer": "fcn",
               "blocks.0.layer_norm": "ln2", "last_layer": "last"}


def load_weights(onnx_path):
    import onnx
    import torch
    from onnx import numpy_helper
    init = {i.name: torch.from_numpy(numpy_helper.to_array(i).copy()) for i in onnx.load(onnx_path).graph.initializer}
    net = make_net()
    net.load_state_dict({f"{TORCH_NAMES[k.rsplit('.', 1)[0]]}.{k.rsplit('.', 1)[1]}": v for k, v in init.items()
                         if k.rsplit(".", 1)[0] in TORCH_NAMES})
    return net


def save_onnx(net, template, out):
    """Template model with the new weights written into its initializers (single file, same graph)."""
    import onnx
    from onnx import numpy_helper
    m = onnx.load(template)
    sd = net.state_dict()
    for i in m.graph.initializer:
        base, kind = i.name.rsplit(".", 1) if "." in i.name else (i.name, "")
        if base in TORCH_NAMES:
            i.CopyFrom(numpy_helper.from_array(sd[f"{TORCH_NAMES[base]}.{kind}"].numpy().astype(np.float32), i.name))
    onnx.save_model(m, out, save_as_external_data=False)


# ---------- audio ----------
def trim(x):
    nz = np.flatnonzero(np.abs(x) > 0.02 * max(np.abs(x).max(), 1e-6))
    return x[max(nz[0] - 1600, 0): nz[-1] + 1600] if nz.size else x


def to_window(x, rng):
    """Left-pad with silence to 2 s, 0-200 ms of tail after the clip: how train.py's augment_clips lays out positives."""
    x = x[-(TOTAL - 3200):]  # keep the end of a too-long clip, leave room for the tail
    tail = rng.randint(0, 3200)
    w = np.zeros(TOTAL, np.float32)
    end = TOTAL - tail
    w[end - len(x): end] = x
    return w


def augment(x, rng, noises, rirs):
    n = max(int(len(x) * rng.uniform(0.95, 1.05)), 1)  # speed +-5%
    x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
    if rirs and rng.random() < 0.5:
        r = rirs[rng.randrange(len(rirs))]
        y = np.convolve(x, r / max(np.abs(r).max(), 1e-6))[: len(x) + 4000]
        x = (y * (np.abs(x).max() / max(np.abs(y).max(), 1e-6))).astype(np.float32)
    w = to_window(x, rng)
    if noises and rng.random() < 0.75:
        nz = noises[rng.randrange(len(noises))]
        s = rng.randrange(max(len(nz) - TOTAL, 1))
        seg = nz[s: s + TOTAL]
        seg = np.pad(seg, (0, TOTAL - len(seg)))
        snr = rng.uniform(0, 25)
        w = w + seg * (np.sqrt((w ** 2).mean() + 1e-9) / np.sqrt((seg ** 2).mean() + 1e-9)) / 10 ** (snr / 20)
    w = w * 10 ** (-rng.uniform(0, 12) / 20)  # gain
    return np.clip(w, -1, 1)


def read(p):
    x, sr = sf.read(p, dtype="float32")
    assert sr == SR, f"{p}: need 16 kHz"
    return x if x.ndim == 1 else x.mean(1)


def embed(F, wavs):
    """(N, samples) float [-1,1] -> (N, frames, 96)."""
    pcm = (np.clip(wavs, -1, 1) * 32767).astype(np.int16)
    dup = len(pcm) % 64 == 1  # embed_clips squeezes a batch of one into the wrong shape
    out = F.embed_clips(np.concatenate([pcm, pcm[-1:]]) if dup else pcm, batch_size=64)
    return out[:-1] if dup else out


def stream_scores(net, F, clips):
    """Best score over the 16-frame windows of each clip with a second of silence around it (like the listener)."""
    import torch
    out = []
    for x in clips:
        s = np.concatenate([np.zeros(SR, np.float32), x, np.zeros(SR // 2, np.float32)])
        s = np.pad(s, (0, max(-len(s) % STEP, 48000 - len(s))))  # whole 80 ms frames, and >= 16 of them
        feats = embed(F, s[None])[0]
        win = np.stack([feats[i:i + 16] for i in range(len(feats) - 15)])
        with torch.no_grad():
            out.append(net(torch.from_numpy(win)).max().item())
    return np.array(out)


def fp_per_hour(net, val, thr, hours=11.3, refractory=25, chunk=20000):
    import torch
    sc = []
    with torch.no_grad():
        for s in range(0, len(val) - 15, chunk):
            blk = torch.from_numpy(np.ascontiguousarray(val[s:s + chunk + 15], dtype=np.float32))
            sc.append(net(blk.unfold(0, 16, 1).permute(0, 2, 1)).ravel().numpy())
    hits = np.flatnonzero(np.concatenate(sc) >= thr)
    return (0 if hits.size == 0 else 1 + int((np.diff(hits) > refractory).sum())) / hours


# ---------- main ----------
def evaluate(net, F, data, held, near, everyday, thr):
    import torch
    with torch.no_grad():
        tts = (net(data["pos_test"]).ravel().numpy() >= thr).mean()
    return {"recall": float((stream_scores(net, F, held) >= thr).mean()) if held else None,
            "tts_recall": float(tts),
            "fa_nearmiss": float((stream_scores(net, F, near) >= thr).mean()),
            "fa_everyday": float((stream_scores(net, F, everyday) >= thr).mean()),
            "fp_per_hour": fp_per_hour(net, data["val_eval"], thr, hours=11.3 / 2)}


def main():
    import torch
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", type=Path, default=HOME / "wake-enroll/clips")
    ap.add_argument("--data", type=Path, default=HOME / "wake-train")
    ap.add_argument("--model", type=Path, default=HOME / "azel.onnx")
    ap.add_argument("--threshold", type=float, default=0.6)
    ap.add_argument("--steps", type=int, default=600)
    ap.add_argument("--aug", type=int, default=60, help="augmented copies per training clip")
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--freeze-first", action="store_true", help="train only the later layers")
    ap.add_argument("--anchor", type=float, default=200.0, help="weight of staying close to the current model on generic data")
    ap.add_argument("--user-per-batch", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true", help="measure, never replace the model")
    a = ap.parse_args()
    t0 = time.time()
    rng = random.Random(a.seed)
    torch.manual_seed(a.seed)
    from openwakeword.utils import AudioFeatures
    F = AudioFeatures()
    files = sorted(a.clips.glob("*.wav"))
    rng.shuffle(files)
    n_held = max(1, round(0.2 * len(files)))
    held_f, train_f = files[:n_held], files[n_held:]
    assert train_f, "need more enrollment clips"
    held = [trim(read(f)) for f in held_f]
    D = a.data
    noises = [read(f) for f in sorted((D / "background").glob("*.wav"))[:150]]
    rirs = [read(f) for f in sorted((D / "rirs").glob("*.wav"))]
    f32 = lambda n: torch.from_numpy(np.load(D / n).astype(np.float32))  # noqa: E731
    data = {"pos": f32("positive_features_train.npy"), "neg": f32("negative_features_train.npy"),
            "pos_test": f32("positive_features_test.npy"), "val": np.load(D / "validation_set_features.npy", mmap_mode="r")}
    half = len(data["val"]) // 2  # first half is extra hard negatives for training, second half is only for the fp/hour check
    data["val_train"], data["val_eval"] = data["val"][:half], data["val"][half:]
    acav = np.memmap(D / "acav_100k.f16", dtype=np.float16, mode="r").reshape(-1, 16, 96)
    near = [trim(read(f)) for f in sorted((D / "eval/near").glob("*.wav"))]
    everyday = [trim(read(f)) for f in sorted((D / "eval/everyday").glob("*.wav"))]

    before = load_weights(a.model).eval()
    m0 = evaluate(before, F, data, held, near, everyday, a.threshold)
    t1 = time.time()

    mine = np.stack([augment(trim(read(f)), rng, noises, rirs) for f in train_f for _ in range(a.aug)])
    user_pos = torch.from_numpy(embed(F, mine).astype(np.float32))
    net, old = load_weights(a.model), load_weights(a.model).eval()
    params = [p for n, p in net.named_parameters() if not (a.freeze_first and n.startswith(("layer1", "layernorm1")))]
    opt = torch.optim.Adam(params, lr=a.lr)
    vt = np.ascontiguousarray(data["val_train"], dtype=np.float32)
    for step in range(a.steps):
        ix = lambda n, k: torch.randint(0, n, (k,))  # noqa: E731
        generic = torch.cat([data["pos"][ix(len(data["pos"]), 30)], data["neg"][ix(len(data["neg"]), 50)],
                             torch.from_numpy(acav[np.sort(np.random.randint(0, len(acav), 256))].astype(np.float32)),
                             torch.from_numpy(np.stack([vt[i:i + 16] for i in np.random.randint(0, len(vt) - 16, 256)]))])
        with torch.no_grad():
            soft = old(generic)
        mine = user_pos[ix(len(user_pos), a.user_per_batch)]
        # teach the user's voice, and anchor everything else to the current model so the false-accept rate keeps its calibration
        loss = torch.nn.functional.binary_cross_entropy(net(mine), torch.ones(len(mine), 1)) + \
            a.anchor * torch.nn.functional.binary_cross_entropy(net(generic), soft)
        opt.zero_grad()
        loss.backward()
        opt.step()
    net.eval()
    m1 = evaluate(net, F, data, held, near, everyday, a.threshold)
    t2 = time.time()

    better = (m1["recall"] or 0) > (m0["recall"] or 0)
    safe = (m1["fa_nearmiss"] <= 0.05 and m1["fa_everyday"] <= 0.01 and m1["tts_recall"] >= 0.9 * m0["tts_recall"]
            and m1["fp_per_hour"] <= max(0.5, 1.5 * m0["fp_per_hour"]))
    improved = bool(better and safe)
    if improved and not a.dry_run:
        tmp = a.model.with_suffix(".new.onnx")
        save_onnx(net, str(a.model), str(tmp))
        tmp.chmod(0o600)
        base = a.model.with_name(a.model.stem + ".base.onnx")
        if not base.exists():  # the generic model, restored when the user deletes their voice data
            shutil.copy2(a.model, base)
        shutil.copy2(a.model, a.model.with_name(a.model.stem + ".prev.onnx"))
        os.replace(tmp, a.model)  # the listener reloads on the file's mtime
    res = {"improved": improved, "threshold": a.threshold, "clips_train": len(train_f), "clips_held_out": len(held_f),
           "recall_before": m0["recall"], "recall_after": m1["recall"], "tts_recall_before": m0["tts_recall"],
           "tts_recall_after": m1["tts_recall"], "fa_nearmiss": m1["fa_nearmiss"], "fa_everyday": m1["fa_everyday"],
           "fa_nearmiss_before": m0["fa_nearmiss"], "fa_everyday_before": m0["fa_everyday"],
           "fp_per_hour_before": m0["fp_per_hour"], "fp_per_hour_after": m1["fp_per_hour"],
           "seconds": {"eval_before": round(t1 - t0), "train_and_eval": round(t2 - t1), "total": round(t2 - t0)}}
    print("RESULT " + json.dumps(res))


if __name__ == "__main__":
    main()
