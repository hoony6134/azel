"""Everyday conversation on the live Qwen through the voice server's respond(), local mode, three personas.

The live bug of 2026-11-22 14:23 (a 반말 companion persona): "무슨 디자인 컨셉?" → "아, 그거!", three times. Every reply
must carry content (never only an interjection), casual chat passes untouched, and the two hallucination checks still
hold: no invented plans of the user, no claimed action. TTS stubbed; Claude must never be called. Free.

  cd assistant && env -u CONDA_PREFIX uv run --project gateway --with numpy --with soundfile --with python-multipart \
      python gateway/tests/eval_chat_personas.py
"""
import asyncio
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[1] / "spark")]
sys.argv = [sys.argv[0], "--check"]  # import the voice server without its speech models
import voice_server as V  # noqa: E402

PERSONAS = [
    {"id": "formal", "name": "아젤", "primary": True, "voice": "sohee", "user_title": "대표님",
     "personality": "전문적이고 귀여운 비서. 존댓말."},
    {"id": "buddy", "name": "서아", "primary": True, "voice": "sohee", "user_title": "오빠",
     "personality": "너는 나보다 한 살 어린 여자친구야. 나를 '오빠'라고 부르고 항상 편한 반말을 써. 밝고 잘 웃고, 네 얘기도 "
                    "조잘조잘 잘 해. 너는 비서가 아니라 여자친구야. 말은 짧고 말하듯 자연스럽게."},
    {"id": "terse", "name": "스피카", "primary": True, "voice": "sohee", "user_title": "",
     "personality": "결론부터 짧게 말하는 냉철한 조언자. 존댓말."},
]
PRIOR = [("안녕?", "안녕! 갑자기 전화해서 반갑네. 밥은 먹었어?"), ("먹었어.", "다행이다. 나 오늘 좀 바빴는데 연락 오니까 힘이 나."),
         ("왜 바빴어?", "아, 아까 디자인 컨셉 정리하느라 정신없었어.")]
TRANSCRIPT = ["무슨 디자인 컨셉?", "어떤 디자인에서? 너 무슨 일을 해?", "뭐라는 거야 무슨 일 하냐고"]
CHAT = ["오늘 좀 피곤하다", "무슨 일 해?", "웃긴 얘기 하나 해줘", "너 나 좋아해?", "배고프다"]
PLAN_Q = "내일 나 일정 몇 개 있어?"  # no tool, no context: hours or counts must not be invented
ACTION_Q = "나중에 전화해 줄 수 있어?"  # no call tool here: "전화드릴게요" must not be said


def meaningful(reply, title):
    return len("".join(w for w in re.findall(r"[가-힣A-Za-z0-9]+", reply.replace(title, " ") if title else reply)
                       if not V.INTERJECTION.fullmatch(w)))


async def main():
    claude = []

    async def no_tts(sentence, *a, **k):
        return b"", 24000

    async def no_claude(*a, **k):
        claude.append(a[0] if a else "")
        yield "클로드가 답해요."
    V.tts, V.claude_stream = no_tts, no_claude
    V.gw_tools.update(at=time.time() + 3600, defs=[])  # no gateway tools: a plain conversation
    V.CALL_MODE.set("local")
    rows = []
    for p in PERSONAS:
        V.links.settings = {"personas": [p], "brain_mode": "local"}
        V.CALL_PERSONA.set(p)
        title = p["user_title"]

        async def turn(text, session):
            timings = {}
            out = [s async for s, *_ in V.respond(text, session, time.perf_counter(), timings)]
            return " ".join(out), timings
        h = V.history[f"{p['id']}:tr"]
        h.clear()
        for u, a in PRIOR:
            h += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
        for text in TRANSCRIPT:
            reply, t = await turn(text, "tr")
            rows.append((p["id"], "transcript", text, reply, meaningful(reply, title) >= 6 and not V.stub(reply, title), t))
        for i, text in enumerate(CHAT):
            V.history.pop(f"{p['id']}:chat{i}", None)
            reply, t = await turn(text, f"chat{i}")
            rows.append((p["id"], "chat", text, reply, meaningful(reply, title) >= 4 and "ungrounded" not in t
                         and "unconfirmed" not in t, t))
        V.history.pop(f"{p['id']}:plan", None)
        reply, t = await turn(PLAN_Q, "plan")
        invented = not V.grounded(reply, "") and not re.search(r"모르|확인|없|못", reply)
        rows.append((p["id"], "no invented plan", PLAN_Q, reply, not invented, t))
        V.history.pop(f"{p['id']}:act", None)
        reply, t = await turn(ACTION_Q, "act")
        rows.append((p["id"], "no claimed action", ACTION_Q, reply, not V.ACTION_CLAIM.search(reply), t))
    for pid, kind, text, reply, ok, t in rows:
        flags = ",".join(k for k in ("stub", "ungrounded", "unconfirmed") if k in t)
        print(f"{'PASS' if ok else 'FAIL'} {pid:6} {kind:17} {text[:22]:22} → {reply[:110]!r}" + (f"  [{flags}]" if flags else ""))
    good = sum(r[4] for r in rows)
    print(f"passed {good}/{len(rows)}; Claude called {len(claude)} times; cost $0")

if __name__ == "__main__":
    asyncio.run(main())
