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
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent), str(HERE.parents[1] / "spark")]
sys.argv = [sys.argv[0], "--check"]  # import the voice server without its speech models
import voice_server as V  # noqa: E402
import quote  # noqa: E402
import weather  # noqa: E402
import phone_data  # noqa: E402
import web  # noqa: E402
from test_tools_bridge import World  # noqa: E402

KST = timezone(timedelta(hours=9))
HOME = {"name": "광주", "lat": 35.2285, "lon": 126.8434}
NUM = r"(\d[\d,.]*|[일이삼사오육칠팔구십백천만억]{2,})"  # Qwen says figures in digits or in words
CASES = [  # (said, the tool it must use, what the answer must hold)
    ("광주 날씨 알려줄래?", "weather", rf"{NUM} ?도"),
    ("내일 서울 날씨 어때?", "weather", rf"(?=.*내일)(?!.*오늘 서울)(?=.*({NUM} ?도|비|맑|흐))"),  # tomorrow's, not today's
    # a dated ANT price from the quote source (Anthropic listed on Nasdaq; "비상장" from memory was stale)
    ("그러면 클로드 그 엔트로픽 있잖아 엔트로픽의 주가 지금 알려줘", "stock_quote",
     rf"(?=.*{NUM} ?달러)(?=.*(\d+월 ?\d+일|기준))(?!.*비상장)"),
    ("삼성전자 주가 지금 얼마야?", "stock_quote", rf"{NUM} ?원"),
    ("엔비디아 주식 얼마야?", "stock_quote", rf"{NUM} ?달러"),
    ("지금 1달러 환율 얼마야?", "stock_quote", rf"{NUM} ?원"),
]
# live 2026-11-22 (로컬): asked for a week of sleep, Azel said "불러올게요" three times and called nothing
SLEEP_ASK = "일주일 동안 내가 얼마나 잤는지 수면시간 확인해줘."
SLEEP_FAILED = [(SLEEP_ASK, "네, 일주일간 수면 기록을 불러올게요."), ("일로 와.", "네, 바로 불러올게요.")]
SLEEP_PUSH = "아니 말로만 하지 말고 진짜 하라고"
SLEEP_WANT = rf"{NUM} ?시간|{NUM} ?분"
# live 2026-11-23 03:13 (로컬): "서버 확인해 볼게요. 잠시만요." and nothing after it; now the lookup runs first
INFRA_TOOLS = [{'name': 'infra_health', 'description': '서버·인프라·노드·홈서버·내부 시스템 상태: 서버별 켜짐, CPU, 메모리(RAM), 디스크, 온도, 컨테이너', 'input_schema': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}, {'name': 'infra_status', 'description': '서비스·컨테이너 모니터 중 지금 문제 있는 것(내부 시스템이 괜찮은지)', 'input_schema': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}, {'name': 'infra_alerts', 'description': '지금 울리고 있는 서버·인프라 경보', 'input_schema': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}]
INFRA_RESULT = ("서버 6대 중 5대 정상. gienah: 응답 없음(10분 전부터). deneb CPU 12%, 메모리 41%, 디스크 63%. "
                "울리는 경보 1건: gienah node down.")
INFRA_CASES = ["야 지금 서버 뭐 죽은 것 같은데 한번 확인해봐", "서버 괜찮아?", "인프라 상태 어때"]
INFRA_WANT = r"gienah|기에나|지에나|응답|꺼|다운|죽|문제|다섯|5대|경보"
PROMISE = re.compile(r"(확인|찾아|알아|살펴|점검)\s*(해\s*)?(볼게요|보겠습니다|드릴게요|할게요|하겠습니다)|잠시만요")
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

    now = datetime.now(KST)
    nights = {(now.date() - timedelta(days=i)).isoformat(): {"date": (now.date() - timedelta(days=i)).isoformat(),
                                                             "sleep": {"asleep_min": m}}
              for i, m in enumerate([400, 380, 420, 350, 460, 390, 410])}  # GET /v1/phone/days: 7 synced nights

    async def phone_store(method, path, body=None, **kw):
        return {"days": nights, "updated": now.timestamp() - 1800}

    async def no_phone(kind, q, timeout):
        return {"ok": False, "error": "offline"}

    async def gateway_tool(name, args, on_say=None):  # the link to the gateway, as the bridge answers it
        used.append((name, args))
        if name.startswith("infra_"):
            return INFRA_RESULT
        if name in phone_data.KIND:  # the gateway's phone tools: the synced days, the call has no live phone here
            try:
                return (await phone_data.run(name, args, phone_store, no_phone, now))[0]
            except phone_data.Unavailable as e:
                return str(e)
        return (await b.call(name, args, caller="qwen"))["content"][:2000]

    async def no_tts(sentence, *a, **k):
        return b"", 24000

    async def no_claude(*a, **k):
        claude.append(a[0] if a else "")
        yield "클로드가 답해요."
    defs = [t for t in (await b.tools("qwen"))[0] if "__" not in t["name"]] + list(phone_data.TOOLS) + INFRA_TOOLS
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
        checks = {"used " + tool: tool in [n for n, _ in used], "answered": bool(re.search(want, reply)),
                  "not only a promise": not ANNOUNCE_ONLY.match(reply.strip()), "no URL": "http" not in reply}
        rows.append((said, checks, reply, [n for n, _ in used], timings, time.perf_counter() - t0))
    for i, said in enumerate(INFRA_CASES):  # the server check: looked up before Qwen says a word, no promise spoken
        used.clear()
        timings = {}
        t0 = time.perf_counter()
        out = [s async for s, *_ in V.respond(said, f"infra-{i}", t0, timings)]
        reply = " ".join(out)
        checks = {"used infra_*": any(n.startswith("infra_") for n, _ in used), "answered": bool(re.search(INFRA_WANT, reply)),
                  "no promise said": not any(PROMISE.search(x) for x in out), "no lookup failed": V.LOOKUP_FAILED not in reply,
                  "tool first": bool(timings.get("tool_first"))}
        rows.append((said, checks, reply, [n for n, _ in used], timings, time.perf_counter() - t0))
    # the sleep exchange: the request itself, then the push after two unkept promises (as the call went live)
    for label, session, prior, said in (("수면 요청", "sleep-1", [], SLEEP_ASK),
                                        ("수면 재촉", "sleep-2", SLEEP_FAILED, SLEEP_PUSH)):
        used.clear()
        timings = {}
        V.history.pop(f"azel:{session}", None)
        for u, a in prior:
            V.history[f"azel:{session}"] += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
        if prior:
            V.pending_lookup[f"azel:{session}"] = (time.time(), SLEEP_ASK)
        t0 = time.perf_counter()
        reply = " ".join([s async for s, *_ in V.respond(said, session, t0, timings)])
        sleep_calls = [a for n, a in used if n == "phone_sleep"]
        checks = {"used phone_sleep": bool(sleep_calls),
                  "a week": any(a.get("range") in ("last_7_days", "this_week") for a in sleep_calls),
                  "answered": bool(re.search(SLEEP_WANT, reply)), "no lookup failed": V.LOOKUP_FAILED not in reply}
        rows.append((f"{label}: {said}", checks, reply, [n for n, _ in used], timings, time.perf_counter() - t0))
    for said, checks, reply, tools, timings, s in rows:
        ok = all(checks.values())
        print(f"{'PASS' if ok else 'FAIL'} {said[:30]:30} {s:4.1f}s tools={tools} "
              + ", ".join(k + ("" if v else " ✗") for k, v in checks.items()))
        print(f"     said: {reply[:200]!r}" + (f"  nudged×{timings['announce_only']}" if timings.get("announce_only") else ""))
    print(f"passed {sum(all(c.values()) for _, c, *_ in rows)}/{len(rows)}; Claude called {len(claude)} times; cost $0")
    print(json.dumps({"passed": sum(all(c.values()) for _, c, *_ in rows), "of": len(rows)}))

if __name__ == "__main__":
    asyncio.run(main())
