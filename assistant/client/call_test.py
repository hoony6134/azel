"""Act like the phone on WS /v1/call: stream WAV questions in real time, log events.

usage: uv run call_test.py bench/questions/06.wav [more.wav ...] [--barge-in]
  --barge-in   start the next question 1.5 s after the assistant starts speaking
"""
import argparse
import asyncio
import json
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import websockets

SERVER = "ws://100.64.0.11:8891/v1/call"
TOKEN = (Path.home() / ".config/biseo/token").read_text().strip()
CHUNK = 640  # 20 ms of 16 kHz PCM16


async def main(files, barge_in, call_id=""):
    url = f"{SERVER}?session=calltest-{int(time.time())}" + (f"&call_id={call_id}" if call_id else "")
    async with websockets.connect(url, max_size=None, additional_headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
        t0 = time.perf_counter()
        spoke = asyncio.Event()
        audio_bytes = 0

        async def recv():
            nonlocal audio_bytes
            async for msg in ws:
                t = f"{time.perf_counter() - t0:6.2f}s"
                if isinstance(msg, bytes):
                    if not spoke.is_set():
                        print(f"{t}  <first audio>")
                    audio_bytes += len(msg)
                    spoke.set()
                    continue
                ev = json.loads(msg)
                if ev["type"] == "state":
                    print(f"{t}  [{ev['value']}]")
                elif ev["type"] in ("transcript", "reply"):
                    print(f"{t}  {ev['type']}: {ev['text']}" + (f"  ({ev['route']})" if "route" in ev else ""))
                else:
                    print(f"{t}  {ev}")

        receiver = asyncio.create_task(recv())
        silence = np.zeros(16000, np.int16).tobytes()
        for f in files:
            wav, sr = sf.read(f, dtype="int16")
            assert sr == 16000
            if barge_in and f != files[0]:
                await spoke.wait()
                await asyncio.sleep(1.5)
            spoke.clear()
            print(f"{time.perf_counter() - t0:6.2f}s  >> sending {Path(f).name}")
            for i in range(0, len(wav.tobytes()) + len(silence), CHUNK):  # speech + 1 s silence
                await ws.send((wav.tobytes() + silence)[i:i + CHUNK])
                await asyncio.sleep(0.02)
            if not barge_in:
                await asyncio.sleep(25)  # let the turn finish (Claude turns are slow)
        await asyncio.sleep(25 if barge_in else 0)
        await ws.send(json.dumps({"type": "hangup"}))
        receiver.cancel()
        print(f"received {audio_bytes / 48000:.1f}s of audio")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--barge-in", action="store_true")
    ap.add_argument("--call-id", default="")
    a = ap.parse_args()
    asyncio.run(main(a.files, a.barge_in, a.call_id))
