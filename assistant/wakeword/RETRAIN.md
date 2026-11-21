# Wake-model retraining on the user's voice (Mac, CPU only)

The user's clips never leave the Mac. `gateway/wake_enroll.py` stores them in `~/.config/biseo/wake-enroll/clips`
and runs `retrain.py` (about 2 minutes on an M2 Pro) once at least 20 clips are in.

## One-time production setup
1. Python env: `cd assistant/wakeword && env -u CONDA_PREFIX uv sync`, then fetch openWakeWord's feature models:
   `env -u CONDA_PREFIX uv run python -c "import openwakeword.utils as u; u.download_models(['azel'])"`
2. Generic training material (no personal data; read-only on vd-spark, about 620 MB) into
   `~/.config/biseo/wake-train/` (0700): `./setup_train_data.sh [dir with azel_eval_neg]`. The eval clips
   (`gen_eval_negatives.py`, TTS near-misses and everyday phrases) exist only where they were generated.
3. `~/.config/biseo/azel.onnx` is the base model (trained on vd-spark with `run_train.sh`).
4. Gateway venv needs `python-multipart`; mount the router (see `wake_enroll.py`). Set `model` in `wake.toml` to
   `~/.config/biseo/azel.onnx`; the listener reloads when the file changes.

## What it does
80% of the clips train, 20% are held out. The training clips are augmented (gain, noise, room reverb, speed +-5%)
into extra positives. The current model is fine-tuned on them and anchored to its own output on generic data
(TTS positives and near-misses, ACAV and validation background features) so false accepts keep their calibration.
It replaces `azel.onnx` only if, at threshold 0.6, held-out recall goes up, near-miss false accepts are at most 5%,
everyday false accepts at most 1%, TTS recall stays at 90% of before, and false positives per hour stay within
1.5x (at least 0.5). Otherwise status is `ready` with `"improved": false` and nothing changes.
Backups: `azel.prev.onnx` (before the last replace) and `azel.base.onnx` (the generic model, restored by DELETE).

Manual run, never replacing: `uv run python retrain.py --dry-run`. Tests: `uv run --group dev pytest -q`.
`train_verifier.py` / `eval_verifier.py` (personal verifier) stay in the tree but are off: it rejected the user's own voice.
