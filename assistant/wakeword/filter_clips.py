"""Drop generated clips whose speech doesn't match their label, using Whisper.

Positives must be heard as the wake word (allowing one wrong syllable for 3+ syllables, e.g.
박비사); negatives must NOT contain it. Rejected files move to <split>_rejected/
so nothing is lost.

usage: python filter_clips.py /work/clips/azel 아젤
"""
import sys
from pathlib import Path



# How Whisper writes the wake word when TTS liaises or spells it out: 아젤아 -> 아제라, 아젤 -> 아제엘.
# Bare 아제/아재 stay rejected: they are indistinguishable from the real near-misses.
ALIASES = {"아젤": ("azel", "아제라", "아제엘", "아제을")}


def close_to(wake, heard, tolerance=1):
    """True if `heard` contains `wake` (or a known alias), or a same-length span that starts with the same
    syllable and differs in <=tolerance later syllables (Whisper often hears 서 as 사; 이비서 must not pass).
    Runaway transcripts (a stuck TTS: 아제에에에…) never count."""
    heard = "".join(ch for ch in heard.lower() if "가" <= ch <= "힣" or "a" <= ch <= "z")
    if len(heard) > len(wake) + 8:
        return False
    if wake in heard or any(al in heard for al in ALIASES.get(wake, ())):
        return True
    n = len(wake)
    tolerance = min(tolerance, (n - 1) // 2)  # a 2-syllable word allows no wrong syllable
    if tolerance == 0:
        return False
    spans = (heard[i:i + n] for i in range(len(heard) - n + 1))
    return any(s[0] == wake[0] and sum(a != b for a, b in zip(wake, s)) <= tolerance for s in spans)


def main(root, wake):
    import torch
    from transformers import pipeline
    asr = pipeline("automatic-speech-recognition", model="openai/whisper-large-v3-turbo",
                   torch_dtype=torch.float16, device="cuda:0")
    for split in ["positive_test", "positive_train", "negative_test", "negative_train"]:
        files = sorted((Path(root) / split).glob("*.wav"))
        rejected = Path(root) / f"{split}_rejected"
        rejected.mkdir(exist_ok=True)
        dropped = 0
        for i in range(0, len(files), 64):
            batch = files[i:i + 64]
            outs = asr([str(f) for f in batch], batch_size=64,
                       generate_kwargs={"language": "korean", "task": "transcribe"})
            for f, out in zip(batch, outs):
                positive = split.startswith("positive")
                # Positives tolerate one misheard syllable; negatives are dropped only on an exact hit,
                # so near-misses like 이비서 stay as the hard negatives we want.
                hit = close_to(wake, out["text"], tolerance=1 if positive else 0)
                if hit != positive:
                    f.rename(rejected / f.name)
                    dropped += 1
        print(f"{split}: kept {len(files) - dropped}/{len(files)}", flush=True)


if __name__ == "__main__":
    assert close_to("박비서", "박비서야") and close_to("박비서", "박 비사!") and not close_to("박비서", "김밥 주세요")
    assert not close_to("박비서", "이비서") and not close_to("박비서", "박비사", tolerance=0)
    assert close_to("아젤", "아젤아!") and close_to("아젤", "야, 아 젤.") and close_to("아젤", "Azel!")
    assert close_to("아젤", "아제라.") and close_to("아젤", "아제엘")
    for bad in ["아제", "아재", "엔젤", "아저씨", "젤리", "", "아제" + "에" * 40]:
        assert not close_to("아젤", bad), bad
    main(sys.argv[1], sys.argv[2])
