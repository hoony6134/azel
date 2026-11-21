"""The user's title (호칭) over a call: 아젤 with 대표님, six turns after an opening, on Qwen and Claude (Haiku).
Replies go through the same Address step as the live server. Pass: never twice in one reply, and 1–2 uses across
the six replies besides the opening (which carries it itself).

  uv run --no-project --with fastapi,uvicorn,httpx,numpy,python-multipart,websockets,soundfile,pysilero-vad \
      python eval_title.py
"""
import asyncio
import sys
from pathlib import Path

import httpx

sys.argv = [sys.argv[0], "--check"]  # import without the speech models
import voice_server as V  # noqa: E402

TITLE = "대표님"
TURNS = ["응, 나야. 무슨 일이야?", "아 맞다, 오늘 좀 피곤하네", "점심 뭐 먹을까?", "고마워. 저녁엔 운동 갈까 말까 고민돼",
         "그럼 30분만 할게", "알겠어, 이따 또 얘기하자"]
OPENING = f"{TITLE}, 3시예요. 역사특강 과제 하실 시간이라 전화드렸어요."
KEY = (Path.home() / ".config/biseo/anthropic_api_key").read_text().strip()


async def qwen(messages):
    return "".join([d async for d in V.unthink(V.llm_stream(messages), V.Unthink())])


async def haiku(messages):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.anthropic.com/v1/messages", headers={
            "x-api-key": KEY, "anthropic-version": "2023-06-01"}, json={
            "model": "claude-haiku-5-5", "max_tokens": 2000, "system": messages[0]["content"],
            "messages": [{"role": "user", "content": "(예약 전화가 연결됐어요)"}, *messages[1:]]})  # user turn first
    return "".join(b.get("text", "") for b in r.json().get("content", []))


async def call(model):
    system = V.system_prompt("아젤", "normal", "", "차분하고 다정하게 챙겨 주는 비서", TITLE) + V.now_kst()
    hist, uses, per_reply, said = [{"role": "assistant", "content": OPENING}], 0, [], []
    V.title_gap.clear()
    V.title_gap["eval"] = 0  # the opening said it
    for i, turn in enumerate(TURNS):
        hist.append({"role": "user", "content": turn})
        raw = await model([{"role": "system", "content": system}, *hist])
        a = V.Address(TITLE, "eval", i == 0)
        sentences = [a(s) for s in V.split_ready(raw, final=True)[0]]
        a.done()
        reply = " ".join(sentences)
        hist.append({"role": "assistant", "content": reply})
        per_reply.append(reply.count(TITLE))
        uses += reply.count(TITLE)
        said.append(reply)
    return per_reply, uses, said


async def main():
    for label, model in (("qwen", qwen), ("haiku", haiku)):
        per_reply, uses, said = await call(model)
        ok = max(per_reply) <= 1 and 1 <= uses <= 2
        print(f"{label:6} {'OK  ' if ok else 'FAIL'} opening+{uses} per reply {per_reply}")
        for s in said:
            print("   ", s[:90])

if __name__ == "__main__":
    asyncio.run(main())
