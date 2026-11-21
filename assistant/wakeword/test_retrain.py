"""Run: env -u CONDA_PREFIX uv run pytest -q   (in assistant/wakeword)"""
import random
from pathlib import Path

import numpy as np
import pytest
import torch

import retrain

MODEL = Path.home() / ".config/biseo/azel.onnx"


def test_window_layout_and_augment_shape():
    rng = random.Random(0)
    x = np.ones(8000, np.float32) * 0.5
    w = retrain.to_window(x, rng)
    assert w.shape == (retrain.TOTAL,) and w[: retrain.TOTAL - 8000 - 3200].max() == 0  # left-padded with silence
    assert retrain.to_window(np.ones(40000, np.float32), rng).shape == (retrain.TOTAL,)  # too-long clips are cut
    a = retrain.augment(x, rng, [np.random.randn(20000).astype(np.float32) * 0.1], [np.random.randn(2000).astype(np.float32)])
    assert a.shape == (retrain.TOTAL,) and np.abs(a).max() <= 1


@pytest.mark.skipif(not MODEL.exists(), reason="needs the trained azel.onnx")
def test_weights_roundtrip_through_onnx(tmp_path):
    import onnxruntime as ort
    net = retrain.load_weights(str(MODEL)).eval()
    with torch.no_grad():
        net.last.bias += 0.5  # a changed model must survive save_onnx
    out = tmp_path / "m.onnx"
    retrain.save_onnx(net, str(MODEL), str(out))
    x = np.random.randn(1, 16, 96).astype(np.float32)
    got = ort.InferenceSession(str(out)).run(None, {"x": x})[0].item()
    assert abs(got - net(torch.from_numpy(x)).item()) < 1e-5
