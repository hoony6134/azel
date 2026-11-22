"""Today's figures in local mode, through the voice server's own Qwen path (respond(), the [TOOL] loop): the live
bugs of 2026-11-22. "광주 날씨 알려줄래?" got "결과가 없었어요", "엔트로픽 주가 지금 알려줘" got only "찾아볼게요",
and "1달러 환율 얼마야?" got a made-up rate without any lookup.

Real Qwen, real Open-Meteo, the user's SearXNG and page fetches behind the gateway's own tool bridge; TTS stubbed;
Claude must never be called. Free.

  cd assistant && env -u CONDA_PREFIX uv run --project gateway --with numpy --with soundfile --with python-multipart \
      python gateway/tests/eval_live_local.py
"""
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent), str(HERE.parents[1] / "spark")]
sys.argv = [sys.argv[0], "--check"]  # import the voice server without its speech models
import voice_server as V  # noqa: E402
import quote  # noqa: E402
import weather  # noqa: E402
import web  # noqa: E402
from test_tools_bridge import World  # noqa: E402

HOME = {"name": "광주", "lat": 35.2285, "lon": 126.8434}
NUM = r"(\d[\d,.]*|[일이삼사오육칠팔구십백천만억]{2,})"  # Qwen says figures in digits or in words
CASES = [  # (said, the tool it must use, what the answer must hold)
    ("광주 날씨 알려줄래?", "weather", rf"{NUM} ?도"),
    ("내일 서울 날씨 어때?", "weather", rf"{NUM} ?도|비|맑|흐"),
    # a dated ANT price from the quote source (Anthropic listed on Nasdaq; "비상장" from memory was stale)
    ("그러면 클로드 그 엔트로픽 있잖아 엔트로픽의 주가 지금 알려줘", "stock_quote",
     rf"(?=.*{NUM} ?달러)(?=.*(\d+월 ?\d+일|기준))(?!.*비상장)"),
    ("삼성전자 주가 지금 얼마야?", "stock_quote", rf"{NUM} ?원"),
    ("엔비디아 주식 얼마야?", "stock_quote", rf"{NUM} ?달러"),
    ("지금 1달러 환율 얼마야?", "stock_quote", rf"{NUM} ?원"),
]
ANNOUNCE_ONLY = re.compile(r"^[^.?!]*(찾아|알아|확인해)\s*볼게요\.?$")


async def main():
    b = World().bridge()
    used, claude = [], []

    async def local():
        return "local"

    async def up():
        return True

    kma = Path.home() / ".config/biseo/kma_key"  # 기상청 when the Mac has the key (never printed), else Open-Meteo
    key = kma.read_text().strip() if kma.exists() else None

    async def wx(place, when):
        return await weather.forecast(place, when, HOME, kma_key=key)

    async def sx(q, n):
        r = await web.searxng(q, n, os.environ.get("BISEO_SEARXNG_URL", web.SEARXNG_URL))
        if not r:
            raise RuntimeError("searxng found nothing")
        return r
    async def sq(q):
        return await quote.quote(q, search=lambda s: web.searxng(s, 6, os.environ.get("BISEO_SEARXNG_URL", web.SEARXNG_URL),
                                                               fetch_pages=False))
    b.d.update(weather=wx, stock_quote=sq, web_search=sx, web_fetch=web.fetch, cygnus=lambda: ([], None), mode=local, web_ok=up)

    async def gateway_tool(name, args, on_say=None):  # the link to the gateway, as the bridge answers it
        used.append(name)
        return (await b.call(name, args, caller="qwen"))["content"][:2000]

    async def no_tts(sentence, *a, **k):
        return b"", 24000

    async def no_claude(*a, **k):
        claude.append(a[0] if a else "")
        yield "클로드가 답해요."
    defs = [t for t in (await b.tools("qwen"))[0] if "__" not in t["name"]]
    V.gw_tools.update(at=time.time() + 3600, defs=defs)
    V.gateway_tool, V.tts, V.claude_stream = gateway_tool, no_tts, no_claude
    V.links.settings = {**(V.links.settings or {}), "brain_mode": "local"}
    V.CALL_MODE.set("local")

    rows = []
    for i, (said, tool, want) in enumerate(CASES):
        used.clear()
        timings = {}
        t0 = time.perf_counter()
        out = [s async for s, *_ in V.respond(said, f"live-{i}", t0, timings)]
        reply = " ".join(out)
        checks = {"used " + tool: tool in used, "answered": bool(re.search(want, reply)),
                  "not only a promise": not ANNOUNCE_ONLY.match(reply.strip()), "no URL": "http" not in reply}
        rows.append((said, checks, reply, used[:], timings, time.perf_counter() - t0))
    for said, checks, reply, tools, timings, s in rows:
        ok = all(checks.values())
        print(f"{'PASS' if ok else 'FAIL'} {said[:30]:30} {s:4.1f}s tools={tools} "
              + ", ".join(k + ("" if v else " ✗") for k, v in checks.items()))
        print(f"     said: {reply[:200]!r}" + (f"  nudged×{timings['announce_only']}" if timings.get("announce_only") else ""))
    print(f"passed {sum(all(c.values()) for _, c, *_ in rows)}/{len(rows)}; Claude called {len(claude)} times; cost $0")
    print(json.dumps({"passed": sum(all(c.values()) for _, c, *_ in rows), "of": len(rows)}))

if __name__ == "__main__":
    asyncio.run(main())
