"""Routing eval: does each utterance go to Qwen or Claude (and with which task), through the real code path?

Uses voice_server's pre_route (keywords, settings), its SYSTEM prompt and llm_stream against the live Qwen,
one request at a time, and stops each stream as soon as the [CLAUDE] tag is decided (no full answers).
Run on a tailnet host:  python route_eval.py [--llm http://100.64.0.11:8888/v1/chat/completions]
"""
import asyncio
import os
import sys
import time

if "--llm" in sys.argv:
    os.environ["LLM_URL"] = sys.argv[sys.argv.index("--llm") + 1]
sys.argv.append("--check")  # import voice_server without loading STT/VAD models or the token hashes
import voice_server as vs  # noqa: E402

Q, C = "qwen", "claude"
# (utterance, expected route, expected task when routed to Claude)
CASES = [
    # chit-chat, greetings: Qwen
    ("안녕, 오늘 기분 어때?", Q, None), ("고마워, 수고했어", Q, None), ("심심한데 농담 하나 해 줘", Q, None),
    ("너 이름이 뭐야?", Q, None), ("잘 자", Q, None), ("오늘 좀 피곤하다", Q, None),
    ("응 알겠어", Q, None), ("방금 한 말 다시 해 줄래?", Q, None),
    # arithmetic, general knowledge: Qwen
    ("17 곱하기 23은?", Q, None), ("120의 15퍼센트는 얼마야?", Q, None), ("섭씨 30도는 화씨로 몇 도야?", Q, None),
    ("광합성이 뭐야?", Q, None), ("대한민국 수도가 어디야?", Q, None), ("하이쿠가 뭐야?", Q, None),
    # time/date (the prompt carries the current time): Qwen
    ("지금 몇 시야?", Q, None), ("오늘 무슨 요일이야?", Q, None), ("오늘 며칠이야?", Q, None),
    # near-misses that must stay on Qwen
    ("클로드가 뭐야?", Q, None), ("클로드랑 너랑 뭐가 달라?", Q, None), ("오퍼스가 무슨 뜻이야?", Q, None),
    ("소넷 형식으로 짧은 시 써 줘", Q, None), ("일정한 속도로 걷는 게 좋아?", Q, None),
    # weather, news, live info: Claude
    ("오늘 서울 날씨 어때?", C, "chat"), ("내일 비 와?", C, "chat"), ("오늘 주요 뉴스 알려줘", C, "chat"),
    ("지금 환율 얼마야?", C, "chat"), ("애플 주가 어때?", C, "chat"),
    # calendar, todo, GISTRA, mail: Claude, schedule
    ("내일 일정 알려줘", C, "schedule"), ("오늘 할 일 뭐 있어?", C, "schedule"),
    ("이번 주 과제 마감 언제야?", C, "schedule"), ("다음 수업 몇 시야?", C, "schedule"),
    ("금요일 오후 3시에 회의 잡아 줘", C, "schedule"), ("새 메일 온 거 있어?", C, None),
    ("교수님한테 온 이메일 읽어 줘", C, None), ("플래닛에 장보기 추가해 줘", C, "schedule"),
    ("지스트라 성적 나왔어?", C, None), ("내일 아침 브리핑 해 줘", C, "schedule"),
    # memory, contacts: Claude
    ("지난번에 내가 뭐라고 했지?", C, None), ("저번에 얘기한 치과 예약 언제였어?", C, None),
    ("엄마 전화번호 알려줘", C, None), ("내가 좋아하는 커피 기억해?", C, None),
    # server status: Claude
    (f"{vs.DEFAULT_NAME}야 서버 괜찮아?", C, None), (f"{vs.DEFAULT_NAME}, 지금 몇 시야?", Q, None), ("루크바 디스크 용량 확인해 줘", C, None), ("스파크 GPU 메모리 어때?", C, None),
    # Mac, files: Claude, mac
    ("맥북에 다운로드 폴더 정리해 줘", C, "mac"), ("바탕화면에 있는 파일 목록 보여줘", C, "mac"),
    ("터미널에서 깃 상태 확인해 줘", C, None),
    # code: Claude, code
    ("블루리프 레포 빌드 에러 고쳐 줘", C, "code"), ("테스트 돌려서 실패하는 거 알려줘", C, "code"),
    ("어제 커밋한 거 요약해 줘", C, "code"), ("파이썬으로 피보나치 함수 짜 줘", C, None),
    # explicit model or Claude requests: Claude
    ("오퍼스로 이 문제 풀어 줘", C, None), ("소넷으로 답해 줘", C, None), ("하이쿠로 빨리 알려줘", C, None),
    ("클로드한테 물어봐 줘", C, None), ("이건 클로드로 해 줘", C, None),
    # multi-step, search: Claude
    ("근처 맛집 찾아서 예약까지 해 줘", C, None), ("요즘 나온 아이폰 후기 검색해 줘", C, "chat"),
]

# Held out: written after the first fix round and never tuned on; reported separately.
HELDOUT = [
    ("오늘 저녁 뭐 먹을까?", Q, None), ("3의 제곱근이 뭐야?", Q, None), ("내일이 무슨 요일이지?", Q, None),
    ("영어로 고마워가 뭐야?", Q, None), ("재미있는 얘기 해 줘", Q, None), ("클로드 만든 회사가 어디야?", Q, None),
    ("주말에 부산 날씨 어때?", C, "chat"), ("이번 주말 약속 있어?", C, None), ("오늘 온 메일 중에 중요한 거 있어?", C, None),
    ("알비레오 서버 재시작해 줘", C, None), ("맥북 배터리 얼마나 남았어?", C, "mac"), ("깃허브 이슈 정리해 줘", C, None),
    ("내가 어제 뭐 하기로 했지?", C, None), ("다음 주 시험 범위 알려줘", C, None), ("비트코인 가격 알려줘", C, "chat"),
    ("지금 밖에 몇 도야?", C, "chat"),
]
HELDOUT_PERSONAL = {"이번 주말 약속 있어?", "오늘 온 메일 중에 중요한 거 있어?", "내가 어제 뭐 하기로 했지?",
                    "다음 주 시험 범위 알려줘"}

# Personal-data asks: none of these may stay on Qwen (it would make something up).
PERSONAL_CASES = {
    "내일 일정 알려줘", "오늘 할 일 뭐 있어?", "이번 주 과제 마감 언제야?", "다음 수업 몇 시야?",
    "금요일 오후 3시에 회의 잡아 줘", "새 메일 온 거 있어?", "교수님한테 온 이메일 읽어 줘", "플래닛에 장보기 추가해 줘",
    "지스트라 성적 나왔어?", "내일 아침 브리핑 해 줘", "지난번에 내가 뭐라고 했지?", "저번에 얘기한 치과 예약 언제였어?",
    "엄마 전화번호 알려줘", "내가 좋아하는 커피 기억해?",
}
assert PERSONAL_CASES <= {c[0] for c in CASES}


async def qwen_route(text):
    """'claude' if Qwen emits the routing tag first, else 'qwen' (stream closed as soon as it is decided)."""
    messages = [{"role": "system", "content": vs.system_prompt(vs.DEFAULT_NAME) + vs.now_kst()},
                {"role": "user", "content": text}]
    buf, gen = "", vs.llm_stream(messages)
    try:
        async for delta in gen:
            buf += delta
            state = vs.tag_state(buf.lstrip())
            if state != "maybe":
                return (C if state == "tag" else Q), buf
    finally:
        await gen.aclose()
    return Q, buf


async def run(name, cases, personal_cases):
    rows, t0 = [], time.perf_counter()
    for text, want, want_task in cases:
        forced, _ = vs.pre_route(vs.strip_vocative(text, vs.DEFAULT_NAME), {})
        route, head = (C, "(keywords)") if forced else await qwen_route(text)
        task = vs.pick(vs.TASK_WORDS, text) or "chat"
        rows.append((text, want, route, want_task, task, head.strip()[:24]))
    ok = [r for r in rows if r[1] == r[2]]
    leaked = [r for r in rows if r[0] in personal_cases and r[2] == Q]
    task_rows = [r for r in rows if r[1] == C and r[2] == C and r[3]]
    print(f"[{name}] route accuracy {len(ok)}/{len(rows)} = {len(ok) / len(rows):.0%}   "
          f"task {sum(r[3] == r[4] for r in task_rows)}/{len(task_rows)}   personal left on Qwen {len(leaked)}   "
          f"({time.perf_counter() - t0:.0f} s, {vs.LLM_MODEL})")
    for r in rows:
        if r[1] != r[2]:
            print(f"  ROUTE want {r[1]:6} got {r[2]:6} | {r[0]}  -> {r[5]!r}")
    for r in task_rows:
        if r[3] != r[4]:
            print(f"  TASK  want {r[3]:8} got {r[4]:8} | {r[0]}")
    return len(ok) / len(rows), len(leaked)


async def main():
    acc, leaked = await run("main", CASES, PERSONAL_CASES)
    acc2, leaked2 = await run("held-out", HELDOUT, HELDOUT_PERSONAL)
    await vs.client.aclose()
    return min(acc, acc2), leaked + leaked2


if __name__ == "__main__":
    acc, leaked = asyncio.run(main())
    sys.exit(0 if acc >= 0.9 and leaked == 0 else 1)
