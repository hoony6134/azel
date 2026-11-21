"""Week-1 latency benchmark: 20 spoken questions through the Spark voice loop.

First run synthesizes the questions (Qwen3-TTS, voices other than the
assistant's) into bench/questions/; every run sends each one as a fresh
session and writes bench/results-<time>.csv plus a median/p90 summary.

usage: uv run benchmark.py [--rounds 1]
"""
import argparse
import csv
import io
import time
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf

from voice_client import turn

TTS = "http://100.64.0.11:8890/v1/audio/speech"
QUESTIONS = [
    "오늘 날씨 어때?", "지금 몇 시야?", "내일 아침 일정 알려줘.", "간단하게 자기소개 해 줄래?",
    "점심 메뉴 추천해 줘.", "파이썬에서 리스트 정렬하는 방법 알려줘.", "서버 상태 괜찮아?",
    "이번 주 과제 정리해 줘.", "오후 회의 삼십 분 미뤄 줘.", "오늘 할 일 뭐 남았어?", "좋은 아침이야.",
    "피곤한데 커피 마실까 말까?", "기숙사 정전되면 서버는 어떻게 돼?", "영어로 고마워가 뭐야?",
    "십오 곱하기 이십삼은?", "주말에 할 만한 거 추천해 줘.", "방금 한 말 다시 해 줄래?",
    "잠깐만 기다려 줘.", "알람 일곱 시로 맞춰 줘.", "고마워, 이제 끊을게.",
]
VOICES = ["ryan", "vivian", "serena", "aiden", "eric", "dylan"]
OUT = Path(__file__).parent / "bench"
STAGES = ["stt_ms", "llm_first_token_ms", "first_sentence_ms", "first_audio_ms", "total_ms",
          "client_first_audio_ms", "client_total_ms"]


def make_questions():
    qdir = OUT / "questions"
    qdir.mkdir(parents=True, exist_ok=True)
    for i, q in enumerate(QUESTIONS):
        path = qdir / f"{i:02d}.wav"
        if path.exists():
            continue
        r = httpx.post(TTS, json={"input": q, "voice": VOICES[i % len(VOICES)], "response_format": "wav",
                                  "language": "Korean"}, timeout=120)
        r.raise_for_status()
        wav, sr = sf.read(io.BytesIO(r.content), dtype="float32")
        # 24 kHz -> 16 kHz by linear interpolation; plenty for a speech test input.
        n = int(len(wav) * 16000 / sr)
        wav16 = np.interp(np.linspace(0, len(wav) - 1, n), np.arange(len(wav)), wav)
        sf.write(path, wav16, 16000, subtype="PCM_16")
    return sorted(qdir.glob("*.wav"))


def cer(ref, hyp):
    """Character error rate on Hangul/digits only (Levenshtein / len(ref))."""
    keep = lambda s: [c for c in s if c.isalnum()]  # noqa: E731
    r, h = keep(ref), keep(hyp)
    d = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hc in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rc != hc))
    return d[len(h)] / max(len(r), 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=1)
    args = ap.parse_args()
    files = make_questions()
    rows = []
    for rnd in range(args.rounds):
        for f in files:
            wav, _ = sf.read(f, dtype="float32")
            events, client = turn(wav, session=f"bench-{time.time()}", play=False, quiet=True)
            t = events[-1]["timings"]
            transcript = next(e["text"] for e in events if e["type"] == "transcript")
            q = QUESTIONS[int(f.stem)]
            row = {"round": rnd, "question": q, "transcript": transcript, "cer": round(cer(q, transcript), 3),
                   "reply": events[-1]["reply"], **{k: t.get(k) for k in STAGES[:5]},
                   "client_first_audio_ms": client.get("first_audio_ms"), "client_total_ms": client["total_ms"]}
            rows.append(row)
            print(f"{q:<24} STT {row['stt_ms']:>5}  첫토큰 {row['llm_first_token_ms']:>5}  "
                  f"첫소리 {row['client_first_audio_ms']:>5}  CER {row['cer']:.2f}")
    path = OUT / f"results-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\nsaved {path}\n\n{'stage':<24}{'median':>8}{'p90':>8}")
    for k in STAGES:
        vals = np.array([r[k] for r in rows if r[k] is not None], dtype=float)
        print(f"{k:<24}{np.median(vals):>8.0f}{np.percentile(vals, 90):>8.0f}")
    print(f"{'stt CER':<24}{np.median([r['cer'] for r in rows]):>8.3f}{np.percentile([r['cer'] for r in rows], 90):>8.3f}")


if __name__ == "__main__":
    assert cer("안녕하세요", "안녕하세요.") == 0 and cer("가나다", "가나") == 1 / 3
    main()
