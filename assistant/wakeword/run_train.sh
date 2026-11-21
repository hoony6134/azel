#!/usr/bin/env bash
# Augment + train on the Spark, CPU only and memory-capped: the GPU is shared with the
# 125B Qwen, and an extra GPU job ran it out of memory (two reboots on 2026-11-21).
set -euo pipefail
docker rm -f kws-train >/dev/null 2>&1 || true
docker run --name kws-train --memory 16g --cpus 8 -e CUDA_VISIBLE_DEVICES= \
  -v ~/kws:/work -w /work --entrypoint nice kws-train:local -n 15 \
  sh -c "python -u /opt/oww/openwakeword/train.py --training_config /work/train_config.yaml --augment_clips --overwrite \
      && python -u /opt/oww/openwakeword/train.py --training_config /work/train_config.yaml --train_model"
