"""Recurring requests -> cron on the real Qwen, checked by cron.py. A case passes when the rule fires at the same
times as the expected one over the next 3 weeks (not string equality), with the right notify, kind and confirmation.
Free (local Qwen), read-only.

  env -u CONDA_PREFIX uv run --frozen python tests/eval_cron.py
"""
import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cron as C  # noqa: E402

KST = timezone(timedelta(hours=9))
QWEN = os.environ.get("BISEO_LLM_URL", "http://100.64.0.11:8888/v1/chat/completions")
QWEN_MODEL = os.environ.get("BISEO_LLM_MODEL", "Qwen3.8-Flash-Next")
NOW = datetime(2026, 11, 22, 20, 0, tzinfo=KST)
# (request, expected cron or None = refused, start, end, notify, kind, confirm)
CASES = [
    ("매일 아침 7시에 알려줘", "0 7 * * *", None, None, "call", "reminder", False),
    ("매일 저녁 9시에 운동하라고 알려줘", "0 21 * * *", None, None, "call", "reminder", False),
    ("화목 2시에 전화해줘", "0 14 * * 2,4", None, None, "call", "reminder", False),
    ("평일 10시부터 6시까지 2시간마다 전화해줘", "0 10-18/2 * * 1-5", None, None, "call", "reminder", True),
    ("매월 1일에 월세 내라고 알려줘", "0 9 1 * *", None, None, "call", "reminder", True),
    ("주말 아침 8시 반에 깨워줘", "30 8 * * 0,6", None, None, "call", "wake", False),
    ("매주 월요일 오전 9시에 주간 회의 준비하라고 알림으로 보내줘", "0 9 * * 1", None, None, "push", "reminder", False),
    ("12월 한 달 동안 매일 밤 11시에 일기 쓰라고 전화해줘", "0 23 * * *", date(2026, 12, 1), date(2026, 12, 31),
     "call", "reminder", True),
    ("5분마다 전화해줘", None, None, None, None, None, None),
    ("평일 점심 12시 반에 약 먹으라고 전화해줘", "30 12 * * 1-5", None, None, "call", "reminder", False),
]


def same(a, b, sa, ea, sb, eb):
    until = NOW + timedelta(days=60)
    return list(C.fires(a, NOW, until, sa, ea)) == list(C.fires(b, NOW, until, sb, eb))


async def main():
    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        async def llm(system, user):  # the gateway's llm_json
            r = await c.post(QWEN, json={"model": QWEN_MODEL, "max_tokens": 300, "temperature": 0.2,
                                         "chat_template_kwargs": {"enable_thinking": False},
                                         "messages": [{"role": "system", "content": system},
                                                      {"role": "user", "content": user}]})
            r.raise_for_status()
            out = r.json()["choices"][0]["message"]["content"]
            try:
                return json.loads(out[out.index("{"):out.rindex("}") + 1])
            except ValueError:
                return None
        good = 0
        for text, want, start, end, notify, kind, confirm in CASES:
            try:
                r = await C.from_words(text, llm, NOW)
            except C.CronError as e:
                ok = want is None
                print(f"{'OK  ' if ok else 'FAIL'} {text:40} refused: {e}")
                good += ok
                continue
            ok = want is not None and same(r["cron"], want, r["start_date"], r["end_date"], start, end) and \
                r["notify"] == notify and r["kind"] == kind and r["confirm"] == confirm
            good += ok
            print(f"{'OK  ' if ok else 'FAIL'} {text:40} {r['cron']:18} {r['start_date']}~{r['end_date']} "
                  f"{r['notify']}/{r['kind']} purpose={r['purpose']!r} | {r['say']}")
    print(f"{good}/{len(CASES)}")

if __name__ == "__main__":
    asyncio.run(main())
