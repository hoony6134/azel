"""Scheduled-call openings on the real Qwen: 10 reminder phrasings -> purpose -> opening, judged natural by Haiku,
and no "-하라고" / "-해줘" endings in what is spoken. Read-only, well under a cent.

  env -u CONDA_PREFIX uv run --frozen python tests/eval_openings.py
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openings as O  # noqa: E402

QWEN = os.environ.get("BISEO_LLM_URL", "http://100.64.0.11:8888/v1/chat/completions")
QWEN_MODEL = os.environ.get("BISEO_LLM_MODEL", "Qwen3.8-Flash-Next")
KEY = Path.home() / ".config/biseo/anthropic_api_key"
PERSONA = "이름은 아젤. 다정하고 차분한 존댓말 비서."
CASES = [  # (request, hour, minute)
    ("오후 1시에 역사 특강 과제하라고 전화해줘", 13, 0),
    ("7시에 약 먹으라고 전화해줘", 19, 0),
    ("30분 뒤에 빨래 꺼내라고 전화 좀 해줘", 15, 30),
    ("저녁 6시 반에 엄마한테 전화하라고 해줘", 18, 30),
    ("매일 밤 11시에 운동하라고 연락해 줘", 23, 0),
    ("오후 4시에 택배 받으러 가라고 전화해 주세요", 16, 0),
    ("10시에 분리수거 하는 거 알려 달라고", 22, 0),
    ("2시에 과외 준비해 달라고 전화해줘", 14, 0),
    ("내일 오후 3시에 회의 들어가야 한다고 전화해줘", 15, 0),
    ("9시에 알고리즘 퀴즈 공부하라고 전화해줘", 21, 0),
]
ENDINGS = re.compile(r"(하라고|라고|해\s*줘|해\s*달라|줘)[.!?~]*(\s|$)")


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

        async def judge(raw, text):
            r = await c.post("https://api.anthropic.com/v1/messages", headers={
                "x-api-key": KEY.read_text().strip(), "anthropic-version": "2023-06-01"}, json={
                "model": "claude-haiku-5-5", "max_tokens": 600, "messages": [{"role": "user", "content": (
                    f"사용자가 '{raw}'라고 예약했고, 그 시각에 비서가 전화를 걸어 첫마디로 '{text}'라고 했어. "
                    "자연스러운 한국어 비서의 첫마디인가(부탁한 말을 그대로 읽거나 명령·인용 어미가 남으면 false)? "
                    'JSON 하나만: {"ok": true|false, "why": "한 줄"}')}]})
            out = "".join(b.get("text", "") for b in r.json().get("content", []))
            try:
                return json.loads(out[out.index("{"):out.rindex("}") + 1])
            except ValueError:
                return {"ok": False, "why": "judge failed: " + out[:80].replace("\n", " ")}

        natural = clean = from_qwen = 0
        for raw, h, m in CASES:
            p = await O.purpose(raw, llm)
            text = await O.compose("reminder", "정훈님", h, m, p, raw=raw, persona=PERSONA, llm=llm)
            qwen_used = text != O.template("reminder", "정훈님", h, m, p)
            no_end = not ENDINGS.search(text)
            j = await judge(raw, text)
            natural += bool(j.get("ok"))
            clean += no_end
            from_qwen += qwen_used
            print(f"{'OK  ' if j.get('ok') and no_end else 'FAIL'} {'qwen' if qwen_used else 'tmpl'} {p!r:18} {text} | {j.get('why', '')[:50]}")
    print(f"natural {natural}/{len(CASES)}, no -하라고/-해줘 endings {clean}/{len(CASES)}, Qwen's opening used {from_qwen}/{len(CASES)}")

if __name__ == "__main__":
    asyncio.run(main())
