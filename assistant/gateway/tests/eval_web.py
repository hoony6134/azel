"""Live web questions on Qwen (the registry's web_search: Haiku with server-side search) and the Claude API (its
native web tools): weather, a headline, a price, a fact. Read-only; a few cents of searches.
Pass: an answer with the asked-for kind of content, a named source, and no URL (it is spoken).

  env -u CONDA_PREFIX uv run --frozen python tests/eval_web.py
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

KEY = (Path.home() / ".config/biseo/anthropic_api_key").read_text().strip()
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
spent = []


async def web_search(q, n):
    r = await web.search(KEY, q, n)
    spent.append(r["cost_usd"])
    return r


async def one(backend, text):
    w = World()
    b = w.bridge()
    b.d.update(web_search=web_search, web_fetch=web.fetch, cygnus=lambda: ([], None))
    called = []
    real = b.dispatch

    async def spy(name, *a, **k):
        called.append(name)
        return await real(name, *a, **k)
    b.dispatch = spy

    async def never():
        return False
    cfg = QWEN if backend == "qwen" else {"api_key": KEY, "model": "haiku"}
    out, cost = "", 0.0
    async for ev in agent_loop.run(backend, SYSTEM, text, b, cfg, 0.2, 2, never):
        if ev["type"] in ("done", "failed"):
            out, cost = ev.get("result") or ev.get("message", ""), ev["cost_usd"]
    return out, called, cost


async def main():
    score = {}
    for backend in ("qwen", "claude_api"):
        score[backend] = 0
        for text, want in CASES:
            out, called, cost = await one(backend, text)
            used = backend == "claude_api" or "web_search" in called  # claude_api: its server-side tool
            ok = used and bool(re.search(want, out, re.S)) and "http" not in out and "www." not in out and \
                bool(re.search(r"기준|따르면|출처|에서 (보면|발표)|[(（][^)）]{2,20}[)）]", out))
            score[backend] += ok
            print(f"{backend:10} {'OK  ' if ok else 'FAIL'} {text:24} {called} ${cost:.3f} | {out[:110]!r}")
    print(" | ".join(f"{b} {s}/{len(CASES)}" for b, s in score.items()), f"| Qwen's searches ${sum(spent):.3f}")

if __name__ == "__main__":
    asyncio.run(main())
