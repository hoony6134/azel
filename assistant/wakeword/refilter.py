"""Re-check <split>_rejected positives with the current filter rule (one Whisper request at a time,
live voice server) and move the ones that now pass back. Logs every transcript to <split>_rejected.jsonl.

usage: ../client/.venv/bin/python refilter.py clips/azel/positive_train 아젤
"""
import json
import sys
from pathlib import Path

import httpx

from filter_clips import close_to

STT = "http://100.64.0.11:8891/v1/audio/transcriptions"
split, wake = Path(sys.argv[1]), sys.argv[2]
rej = split.parent / f"{split.name}_rejected"
token = (Path.home() / ".config/biseo/token").read_text().strip()
kept = 0
with httpx.Client(timeout=60) as c, open(rej.parent / f"{rej.name}.jsonl", "a") as log:
    for f in sorted(rej.glob("*.wav")):
        heard = c.post(STT, files={"file": ("c.wav", f.read_bytes(), "audio/wav")},
                       headers={"Authorization": f"Bearer {token}"}).json()["text"]
        ok = close_to(wake, heard, tolerance=1)
        log.write(json.dumps({"file": f.name, "heard": heard[:60], "ok": ok}, ensure_ascii=False) + "\n")
        if ok:
            f.rename(split / f.name)
            kept += 1
print("moved back:", kept)
