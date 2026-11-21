"""Run: PYTHONPATH=. ../client/.venv/bin/python test_negatives.py"""
from gen_clips import ADVERSARIAL, leaked

assert not leaked("아젤"), leaked("아젤")
assert leaked("아저") == ["아저", "아저씨"]  # the check does fire
for near in ["아저씨", "엔젤", "아제", "아재", "젤리", "아델", "하젤", "앗 젤", "아일", "에젤", "박비서", "김비서"]:
    assert near in ADVERSARIAL, near
print("ok")
