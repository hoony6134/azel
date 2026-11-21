#!/usr/bin/env bash
# Augment + train on the Spark, CPU only and memory-capped: the GPU is shared with the
# 125B Qwen, and an extra GPU job ran it out of memory (two reboots on 2026-11-21).
# SKIP_AUGMENT=1 reuses the features of a previous augmentation step.
# --shm-size: DataLoader workers pass batches through /dev/shm, Docker's 64 MB default kills training.
set -euo pipefail
docker rm -f kws-train >/dev/null 2>&1 || true
T="python -u /opt/oww/openwakeword/train.py --training_config /work/train_config.yaml"
STEPS="$T --train_model"
[ "${SKIP_AUGMENT:-}" = 1 ] || STEPS="$T --augment_clips --overwrite && $STEPS"
docker run --name kws-train --memory 16g --shm-size 4g --cpus 8 -e CUDA_VISIBLE_DEVICES= \
  -v ~/kws:/work -w /work --entrypoint nice kws-train:local -n 15 sh -c "$STEPS"
