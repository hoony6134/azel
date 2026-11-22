"""Local mode's web search: Qwen with the user's SearXNG only (no Claude, no paid search). Weather, a headline,
a price and a fact. Free. Pass: the asked-for content, a named source, no URL.

  env -u CONDA_PREFIX uv run --frozen python tests/eval_web_local.py
"""
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import agent_loop  # noqa: E402
import web  # noqa: E402
from test_tools_bridge import World  # noqa: E402

NOW = datetime.now(timezone(timedelta(hours=9)))
QWEN = {"url": os.environ.get("BISEO_LLM_URL", "http://100.64.0.11:8888/v1/chat/completions"),
        "model": os.environ.get("BISEO_LLM_MODEL", "Qwen3.8-Flash-Next"), "prices": [0, 0]}
SYSTEM = (f"너는 음성 비서야. 두세 문장으로 존댓말로 답해. 지금은 {NOW:%Y-%m-%d %H:%M} (KST). 지금 정보는 web_search로 찾아. "
          "출처는 '○○ 기준으로'처럼 사이트 이름만 말하고 주소(URL)는 절대 쓰지 마.")
CASES = [
    ("오늘 광주 날씨 어때?", r"\d+ ?(도|℃|°)"),
    ("오늘 주요 뉴스 헤드라인 하나만 알려줘", r"^(?!.*(못했|모르|오류|어렵)).{20,}"),
    ("지금 원달러 환율 얼마야?", r"\d[\d,.]* ?원"),
    ("올해 노벨 문학상 수상자 누구야?", r"^(?!.*(못했|모르|오류)).*[가-힣A-Za-z]{2,}.*(수상|받)"),
]


async def one(text):
    b = World().bridge()
    called = []

    async def sx(q, n):
        called.append("web_search")
        r = await web.searxng(q, n)
        if not r:
            raise RuntimeError("searxng found nothing")
        return r

    async def local():
        return "local"

    async def up():
        return True
    b.d.update(web_search=sx, web_fetch=web.fetch, cygnus=lambda: ([], None), mode=local, web_ok=up)

    async def never():
        return False
    out = ""
    async for ev in agent_loop.run("qwen", SYSTEM, text, b, QWEN, 1.0, 2, never):  # Qwen costs nothing
        if ev["type"] in ("done", "failed"):
            out = ev.get("result") or ev.get("message", "")
    return out, called


async def main():
    good = 0
    only = [c for c in CASES if os.environ.get("EVAL_ONLY", "") in c[0]]
    for text, want in only:
        out, called = await one(text)
        ok = "web_search" in called and bool(re.search(want, out, re.S)) and "http" not in out and "www." not in out \
            and bool(re.search(r"기준|따르면|출처|[(（][^)）]{2,20}[)）]", out))
        good += ok
        print(f"{'OK  ' if ok else 'FAIL'} {text:24} {called} | {out[:130]!r}")
    print(f"local qwen + searxng {good}/{len(only)}, cost $0")

if __name__ == "__main__":
    asyncio.run(main())
