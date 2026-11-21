"""Run: PYTHONPATH=. ../client/.venv/bin/python test_negatives.py"""
from gen_clips import ADVERSARIAL, leaked

assert not leaked("박비서"), leaked("박비서")
assert "박 비서관님" not in ADVERSARIAL
assert leaked("박비") == ["박비"]  # the check does fire
for near in ["김비서", "박비", "박씨", "박사", "박수", "박서방", "바비"]:
    assert near in ADVERSARIAL, near
print("ok")
