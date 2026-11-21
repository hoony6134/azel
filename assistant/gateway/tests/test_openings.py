"""Call openings: the purpose rule, the templates, and Qwen's output never trusted past the checks."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openings as O  # noqa: E402

RAW = "오후 1시에 역사 특강 과제하라고 전화해줘"


@pytest.mark.parametrize("raw,want", [
    (RAW, "역사 특강 과제"),
    ("7시에 약 먹으라고 깨워줘", "약 먹기"),
    ("내일 아침 9시에 회의 있다고 알려줘", "회의"),
    ("30분 뒤에 빨래 꺼내라고 전화 좀 해줘", "빨래 꺼내기"),
    ("저녁 6시 반에 엄마한테 전화하라고 해줘", "엄마한테 전화"),
    ("3시에 전화해줘", ""),
    ("매일 밤 11시에 운동하라고 연락해 줘", "운동"),
    ("2시에 과외 준비해 달라고 전화해줘", "과외 준비"),
    ("10시에 분리수거 하는 거 알려 달라고", "분리수거"),
    ("회의 알림", "회의"),
    ("약 드실 시간이에요.", "약 드실 시간"),  # a note written as a sentence (items before batch 5)  # call_user reasons the model already wrote
    ("", ""),
])
def test_purpose_rule(raw, want):
    assert O.purpose_rule(raw) == want


def test_templates():
    assert O.template("reminder", "정훈님", 13, 0, "역사 특강 과제") == "정훈님, 1시예요. 역사 특강 과제 하실 시간이라 전화드렸어요."
    assert O.template("reminder", "", 18, 30, "빨래 꺼내기") == "6시 30분이에요. 빨래 꺼낼 시간이라 전화드렸어요."
    assert O.template("reminder", "정훈님", 15, 0) == "정훈님, 3시예요. 말씀하신 시간이라 전화드렸어요."
    assert O.template("wake", "정훈님", 7, 0).startswith("정훈님, 좋은 아침이에요. 7시예요.")
    assert O.template("task", "정훈님", 0, 0, goal="다운로드 정리") == "정훈님, 맡기신 다운로드 정리 끝났어요."


def test_clean_opening_rejects_leaks():
    ok = "정훈님, 1시예요. 역사 특강 과제 하실 시간이라 전화드렸어요."
    assert O.clean_opening(ok, RAW, "역사 특강 과제", "정훈님", "1시예요")
    for bad in ("정훈님, 역사 특강 과제하라고 하셔서 전화드렸어요.", "정훈님, 역사 특강 과제 해줘라고 하셨죠.",
                "정훈님, 1시예요. 시간 됐어요.",  # purpose missing
                "1시예요. 역사 특강 과제 하실 시간이에요.",  # title missing
                "정훈님, " + "아주 " * 40 + "역사 특강 과제",
                "정훈님, 1시였어요. 역사 특강 과제 하실 시간이라 전화드렸어요."):  # the time not as said
        assert not O.clean_opening(bad, RAW, "역사 특강 과제", "정훈님", "1시예요"), bad


def run(coro):
    return asyncio.run(coro)


def test_compose_falls_back_to_the_template():
    async def leaky(system, user):
        return {"opening": "정훈님, 역사 특강 과제하라고 전화해달라고 하셨죠?"}

    async def broken(system, user):
        raise OSError("qwen down")

    async def good(system, user):
        return {"opening": "정훈님, 1시예요! 역사 특강 과제 하실 시간이에요."}
    want = O.template("reminder", "정훈님", 13, 0, "역사 특강 과제")
    for llm in (leaky, broken, None):
        assert run(O.compose("reminder", "정훈님", 13, 0, "역사 특강 과제", raw=RAW, llm=llm)) == want
    assert run(O.compose("reminder", "정훈님", 13, 0, "역사 특강 과제", raw=RAW, llm=good)).endswith("하실 시간이에요.")


def test_purpose_uses_qwen_only_when_clean():
    async def says(p):
        async def llm(system, user):
            return {"purpose": p}
        return llm
    assert run(O.purpose(RAW, run(says("역사특강 과제")))) == "역사특강 과제"
    assert run(O.purpose(RAW, run(says("역사 특강 과제하라고")))) == "역사 특강 과제"  # leak: the rule
    assert run(O.purpose("3시에 전화해줘", run(says("")))) == ""
