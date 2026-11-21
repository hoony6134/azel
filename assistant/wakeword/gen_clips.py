"""Generate Korean wake-word training clips with Qwen3-TTS VoiceDesign.

Writes 16 kHz mono 16-bit WAVs into the directory layout openWakeWord's
train.py expects: <out>/<model>/{positive,negative}_{train,test}/.
Re-running resumes: each split only tops up to its target count.

usage: python gen_clips.py --out /work/model --model azel \
           --wake 아젤 --n-train 4000 --n-test 500
"""
import argparse
import random
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

GENDERS = ["man", "woman"]
AGES = ["teenage", "20-year-old", "30-year-old", "40-year-old", "50-year-old", "60-year-old", "elderly"]
TONES = [
    "calm", "cheerful", "tired", "in a hurry", "whispering softly", "calling out loudly from across the room",
    "flat and blunt", "warm and friendly", "sleepy", "slightly annoyed", "curious, as if asking a question",
    "mumbling", "confident", "nervous",
]
SPEEDS = ["quickly", "slowly", "at a normal pace"]
PITCHES = ["low-pitched", "high-pitched", "medium-pitched"]
VOICE_QUALITY = ["clear", "husky", "nasal", "breathy", "deep", "bright", "raspy"]

# Words that sound close to the wake word, plus everyday speech. The wake word
# itself must never appear in these.
ADVERSARIAL = [
    "김비", "비서", "이 비서관님", "김밥", "기비서", "김기사", "김미소", "김비선", "김부서", "김치",
    "이비서", "최비서", "정비서", "비서실", "김서", "김씨", "김 대리", "김비석", "김비스",
    "김밥 주세요", "비서님", "김 부장님", "깁스", "김비어", "비상", "김 서방", "김빗자루", "김 실장",
    "김 선생님", "비싸요", "기억해", "김이 서려", "김포", "깁시다", "김수현", "김민서", "김지수",
    # near-misses of the current wake word 아젤; 박비서 and 김비서 are the old names
    "아저씨", "엔젤", "아제", "아재", "젤리", "아델", "하젤", "앗 젤", "아일", "에젤", "아잘", "아줄", "아쉽다",
    "아저", "아이고", "아 제발", "박비서", "김비서",
]
EVERYDAY = [
    "안녕하세요", "오늘 날씨 어때", "저기요", "잠깐만", "네 알겠습니다", "뭐 해", "밥 먹었어?", "회의 시작합니다",
    "이거 봐 봐", "그래서 말이야", "커피 한 잔 할래?", "지금 몇 시야", "음악 좀 꺼 줘", "고마워", "아니 그게 아니라",
    "내일 봐", "어디 가?", "괜찮아", "진짜?", "택배 왔어요", "불 좀 켜 줄래", "이따가 전화할게", "배고프다",
    "아 그렇구나", "문 좀 닫아 줘", "오늘 일정 뭐야", "숙제 다 했어?", "잘 자", "다녀오겠습니다", "엄마 나 왔어",
]


def leaked(wake):
    """Negative phrases that contain the wake word (must stay empty)."""
    return [t for t in ADVERSARIAL + EVERYDAY if wake.replace(" ", "") in t.replace(" ", "")]


def describe(rng):
    return (f"A {rng.choice(AGES)} Korean {rng.choice(GENDERS)} with a {rng.choice(VOICE_QUALITY)}, "
            f"{rng.choice(PITCHES)} voice, speaking Korean {rng.choice(SPEEDS)}, {rng.choice(TONES)}.")


def positive_text(wake, rng):
    return rng.choice([wake, wake, wake, f"{wake}!", f"{wake}?", f"{wake}.", f"{wake}~",
                       f"{wake}아", f"{wake}아!", f"야 {wake}", f"{wake} 저기"])


def negative_text(rng):
    return rng.choice(ADVERSARIAL) if rng.random() < 0.7 else rng.choice(EVERYDAY)


def to_16k_int16(wav, sr):
    import librosa
    wav = np.asarray(wav, dtype=np.float32)
    if sr != 16000:
        wav = librosa.resample(wav, orig_sr=sr, target_sr=16000)
    wav, _ = librosa.effects.trim(wav, top_db=35)
    peak = float(np.abs(wav).max()) if wav.size else 0.0
    if peak > 0:
        wav = wav / peak * 0.9
    return (wav * 32767).astype(np.int16)


def fill(model, out_dir: Path, target, make_text, rng, batch):
    out_dir.mkdir(parents=True, exist_ok=True)
    have = len(list(out_dir.glob("*.wav")))
    while have < target:
        n = min(batch, target - have)
        texts = [make_text(rng) for _ in range(n)]
        instructs = [describe(rng) for _ in range(n)]
        wavs, sr = model.generate_voice_design(text=texts, language=["Korean"] * n, instruct=instructs)
        for wav in wavs:
            pcm = to_16k_int16(wav, sr)
            # Drop degenerate outputs (silence, or runaway generations).
            if 0.25 * 16000 <= len(pcm) <= 3.0 * 16000:
                sf.write(out_dir / f"{uuid.uuid4().hex}.wav", pcm, 16000, subtype="PCM_16")
                have += 1
        print(f"{out_dir.name}: {have}/{target}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--wake", required=True)
    ap.add_argument("--n-train", type=int, default=4000)
    ap.add_argument("--n-test", type=int, default=500)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    assert not leaked(args.wake), f"negative phrases contain the wake word: {leaked(args.wake)}"

    import torch
    from qwen_tts import Qwen3TTSModel

    rng = random.Random(args.seed)
    model = Qwen3TTSModel.from_pretrained(
        "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", device_map="cuda:0", dtype=torch.bfloat16,
        attn_implementation="sdpa",
    )
    root = Path(args.out) / args.model
    pos = lambda r: positive_text(args.wake, r)  # noqa: E731
    fill(model, root / "positive_test", args.n_test, pos, rng, args.batch)
    fill(model, root / "negative_test", args.n_test, negative_text, rng, args.batch)
    fill(model, root / "positive_train", args.n_train, pos, rng, args.batch)
    fill(model, root / "negative_train", args.n_train, negative_text, rng, args.batch)


if __name__ == "__main__":
    main()
