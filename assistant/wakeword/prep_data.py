"""Prepare augmentation audio for openWakeWord training (16 kHz mono 16-bit WAVs).

- AudioSet parquet shards  -> /work/data/background/*.wav  (noise, music, speech)
- MIT room impulse responses -> /work/data/rirs/*.wav
"""
import io
from pathlib import Path

import librosa
import numpy as np
import pyarrow.parquet as pq
import soundfile as sf
from huggingface_hub import snapshot_download

DATA = Path("/work/data")


def write16k(path, wav, sr):
    wav = np.asarray(wav, dtype=np.float32)
    if wav.ndim > 1:
        wav = wav.mean(axis=1)
    if sr != 16000:
        wav = librosa.resample(wav, orig_sr=sr, target_sr=16000)
    sf.write(path, np.clip(wav, -1, 1), 16000, subtype="PCM_16")


def background():
    out = DATA / "background"
    out.mkdir(exist_ok=True)
    n = 0
    for shard in sorted((DATA / "audioset_parquet").glob("*.parquet")):
        table = pq.read_table(shard, columns=["audio"])
        for i, cell in enumerate(table.column("audio").to_pylist()):
            dst = out / f"{shard.stem}_{i}.wav"
            if dst.exists():
                n += 1
                continue
            try:
                wav, sr = sf.read(io.BytesIO(cell["bytes"]))
            except Exception:  # a few AudioSet clips are corrupt; skip them
                continue
            write16k(dst, wav, sr)
            n += 1
    print("background clips:", n)


def rirs():
    out = DATA / "rirs"
    out.mkdir(exist_ok=True)
    src = Path(snapshot_download("davidscripka/MIT_environmental_impulse_responses", repo_type="dataset",
                                 allow_patterns=["16khz/*.wav"]))
    files = list((src / "16khz").glob("*.wav"))
    for f in files:
        wav, sr = sf.read(f)
        write16k(out / f.name, wav, sr)
    print("rirs:", len(files))


if __name__ == "__main__":
    rirs()
    background()
