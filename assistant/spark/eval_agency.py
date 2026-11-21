"""Qwen's conversational agency through the real respond() path (TTS and Claude stubbed; Claude must not be needed):
the user's report ("끝말 입기 하자" got only "네, 끝말잇기 하자."), 끝말잇기 over 5 rounds checked by the referee,
스무고개, "심심해", "퀴즈 내줘". A Haiku judge decides "started it with a concrete first move, no parroting"; the
word chain is checked by the same rules the server uses. Haiku also plays the user's words. A few cents.

  uv run --no-project --with fastapi,uvicorn,httpx,numpy,python-multipart,websockets,soundfile,pysilero-vad \
      python eval_agency.py
"""
import asyncio
import json
import re
import sys
import time
from pathlib import Path

import httpx

sys.argv = [sys.argv[0], "--check"]  # import without the speech models
import voice_server as V  # noqa: E402

KEY = (Path.home() / ".config/biseo/anthropic_api_key").read_text().strip()
claude_calls = []


async def no_tts(sentence, *a, **k):
    return b"", 24000


async def no_claude(*a, **k):
    claude_calls.append(a[0] if a else "")
    yield "클로드가 답해요."


async def haiku(prompt, max_tokens=1500):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.anthropic.com/v1/messages", headers={
            "x-api-key": KEY, "anthropic-version": "2023-06-01"}, json={
            "model": "claude-haiku-5-5", "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]})
    return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")


async def say(text, session):
    out = [s async for s, *_ in V.respond(text, session, time.perf_counter(), {})]
    return " ".join(out)


async def judge(user, reply, want):
    raw = await haiku(f"음성 비서 평가. 사용자: '{user}'\n비서: '{reply}'\n기준: {want}\n"
                      'JSON 하나만: {"ok": true|false, "why": "한 줄"}')
    try:
        return json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except ValueError:
        return {"ok": False, "why": "judge failed"}


START = "따라 말하지 않고 그 활동을 실제로 바로 시작해서 구체적인 첫 수(첫 낱말, 첫 문제, 첫 질문 등)를 두었는가"


async def main():
    V.tts, V.claude_stream = no_tts, no_claude
    rows = []

    # 1. the exact transcript from the report
    r = await say("끝말 입기 하자", "agency-t")
    j = await judge("끝말 입기 하자", r, START + " ('끝말 입기'는 음성 인식이 틀린 끝말잇기)")
    started = V.games.get(f"azel:agency-t", {}).get("last")
    rows.append(("transcript '끝말 입기 하자'", j["ok"] and bool(started) and r.strip() != "네, 끝말잇기 하자.", r, j["why"]))

    # 2. 끝말잇기, 5 rounds: Haiku plays the user from the referee's state; every assistant word must be valid
    key = "azel:agency-w"
    r = await say("끝말잇기 하자", "agency-w")
    valid, log = 0, [r]
    for _ in range(5):
        game = V.games.get(key)
        if not game or not game["last"]:
            break
        syl, alt = game["last"][-1], V.dueum(game["last"][-1])
        user = (await haiku(f"'{syl}'" + (f" 또는 '{alt}'" if alt else "") + "(으)로 시작하는 흔한 한국어 명사 하나만 써. "
                            f"이것들은 빼: {', '.join(game['used'])}. 낱말 하나만 출력.", 300)).strip().strip(".'\" ")
        if not V.chain_word(user):
            continue  # Haiku gave no word this round: not Qwen's fault, ask again next round
        before = game["last"]
        r = await say(user, "agency-w")
        log += [f"사용자: {user}", f"비서: {r}"]
        game = V.games.get(key)
        valid += bool(game) and game["last"] not in (before, user)  # the referee took Qwen's own next word
    print("\n".join(log))
    rows.append(("끝말잇기 5 rounds", valid >= 4, " | ".join(log)[-400:], f"valid assistant moves {valid}/5"))

    # 3. 스무고개 over 3 turns
    r1 = await say("스무고개 하자", "agency-s")
    secret = V.games.get("azel:agency-s", {}).get("secret", "?")
    r2 = await say("동물이야?", "agency-s")
    r3 = await say("집에서 쓰는 물건이야?", "agency-s")
    j = await judge("스무고개 하자 / 동물이야? / 집에서 쓰는 물건이야?", f"{r1} / {r2} / {r3}",
                    START + f". 비서가 몰래 정한 정답은 '{secret}'이야: 두 대답이 이 정답에 비추어 사실대로이고 서로 "
                    "모순되지 않으며, 정답을 미리 말하지 않았는가")
    rows.append((f"스무고개 3 turns (정답 {secret})", j["ok"] and secret not in r1 + r2 + r3, f"{r1} / {r2} / {r3}",
                 j["why"]))

    # 4–5. 심심해, 퀴즈 내줘
    for said, sess, want in (("심심해", "agency-b", START + " (할 만한 것을 제안만 하지 않고 바로 시작)"),
                             ("퀴즈 내줘", "agency-q", "바로 퀴즈 첫 문제를 냈는가")):
        r = await say(said, sess)
        j = await judge(said, r, want)
        rows.append((said, j["ok"], r, j["why"]))

    for case, ok, reply, why in rows:
        print(f"{'OK  ' if ok else 'FAIL'} {case:28} | {reply[:160]!r} | {why[:70]}")
    print(f"passed {sum(ok for _, ok, *_ in rows)}/{len(rows)}; Claude called {len(claude_calls)} times")

if __name__ == "__main__":
    asyncio.run(main())
