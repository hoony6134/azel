#!/usr/bin/env bash
# One-time: gather the generic (non-personal) material the Mac needs to retrain the wake model, into
# ~/.config/biseo/wake-train/ (0700). Read-only on vd-spark: features and noise made from TTS and public data.
# Needs the eval clips from gen_eval_negatives.py (default clips/azel_eval_neg next to this script).
set -euo pipefail
D=~/.config/biseo/wake-train
SP=admin@100.64.0.11
EVAL=${1:-$(dirname "$0")/clips/azel_eval_neg}
mkdir -p "$D"/{background,rirs,eval} && chmod 700 "$D"
rsync -a "$SP":kws/out/azel/{positive,negative}_features_{train,test}.npy "$D"/
# first 100k random-order ACAV windows (16x96 float16, raw, header stripped): ~300 MB of the 17 GB file
ssh "$SP" 'tail -c +129 ~/kws/data/openwakeword_features_ACAV100M_2000_hrs_16bit.npy | head -c $((100000*16*96*2))' > "$D"/acav_100k.f16
rsync -a "$SP":kws/data/validation_set_features.npy "$D"/
rsync -a "$SP":kws/data/rirs/ "$D"/rirs/
ssh "$SP" 'ls ~/kws/data/background | head -300' | rsync -a --files-from=- "$SP":kws/data/background/ "$D"/background/
rsync -a "$EVAL"/near "$EVAL"/everyday "$D"/eval/
chmod -R go-rwx "$D"
du -sh "$D"
