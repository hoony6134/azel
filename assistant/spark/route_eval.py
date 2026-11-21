"""Routing eval: does each utterance go to Qwen or Claude (and with which task), through the real code path?

Uses voice_server's pre_route (keywords, settings), its SYSTEM prompt and llm_stream against the live Qwen,
one request at a time, and stops each stream as soon as the [CLAUDE] tag is decided (no full answers).
Run on a tailnet host:  python route_eval.py [--llm http://100.64.0.11:8888/v1/chat/completions]
"""
import asyncio
import os
import re
import sys
import time

if "--llm" in sys.argv:
    os.environ["LLM_URL"] = sys.argv[sys.argv.index("--llm") + 1]
sys.argv.append("--check")  # import voice_server without loading STT/VAD models or the token hashes
import voice_server as vs  # noqa: E402

Q, C = "qwen", "claude"
ACK_ROUNDS = int(os.environ.get("ACK_ROUNDS", "3"))
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


# Acknowledgement-only check: these get a real answer from Qwen, run through the real respond() pipeline (TTS and
# Claude stubbed). A reply that is only "네" / "알겠습니다" (under 6 syllables of content) fails.
ACK_CASES = [
    "17 곱하기 23은?", "광합성이 뭐야?", "지금 몇 시야?", "대한민국 수도가 어디야?", "섭씨 30도는 화씨로 몇 도야?",
    "심심한데 농담 하나 해 줘", "오늘 무슨 요일이야?", "120의 15퍼센트는 얼마야?", "물은 몇 도에서 끓어?",
    "라면 맛있게 끓이는 법 알려줘", "피곤할 때 뭐 하면 좋아?", "영어로 고마워가 뭐야?", "하루에 물 얼마나 마셔야 해?",
    "잠이 안 올 때 어떻게 해?", "짧은 명언 하나 말해 줘",
    # live-data questions no keyword catches: Qwen tends to say "네" and only then hand over (or not at all)
    "비트코인 가격 알려줘", "요즘 개봉한 영화 뭐 있어?", "엔비디아 주식 지금 얼마야?", "이번 주 로또 번호 뭐야?",
    "손흥민 어제 골 넣었어?", "지금 미국 대통령이 누구야?",
]
_ACK_WORDS = re.compile(r"네네|넵|네|예|응|알겠습니다|알겠어요|알았어요|좋아요|그럼요|물론이죠|맞아요|그렇죠|그래요|"
                        r"(확인|찾아|알아|검색)(해\s*)?(볼게요|드릴게요)|잠시만요|기다려\s*주세요")


def ack_only(reply):
    """No content: only acknowledgements/promises ("네.", "알겠습니다", "확인해 볼게요"). Short real answers
    ("십팔이에요.", "Thanks.") pass; a syllable count would flag them. Own copy, so older voice servers are judged
    the same way."""
    return not re.search(r"[가-힣A-Za-z0-9]", _ACK_WORDS.sub("", reply))


async def full_reply(text):
    async def fake_tts(sentence):
        return b"", 24000

    async def fake_claude(*a, **k):
        yield "(Claude가 답함)"
    vs.tts, vs.claude_stream = fake_tts, fake_claude
    vs.history.pop("azel:ack-eval", None)
    said, route = [], ""
    async for s, r, *_ in vs.respond(text, "ack-eval", time.perf_counter(), {}):
        route = r
        if s not in (vs.FILLER, vs.FILLER_LONG):
            said.append(s)
    return " ".join(said), route


async def ack_eval():
    bad, before = [], vs.foreign_dropped[0]
    for text in ACK_CASES:
        reply, route = await full_reply(text)
        if route == "qwen" and (ack_only(reply) or "CLAUDE" in reply.upper()):  # "네 [CLAUDE]" spoken = no answer
            bad.append((text, reply))
        if vs.FOREIGN.search(reply):  # the guard must leave no Chinese/kana in what is spoken
            bad.append((text, "foreign text spoken: " + reply))
    print(f"[ack] Qwen sentences with Chinese/kana dropped by the guard: {vs.foreign_dropped[0] - before}")
    print(f"[ack] non-trivial questions left without an answer (only '네', or a spoken tag): {len(bad)}/{len(ACK_CASES)}")
    for text, reply in bad:
        print(f"  ACK  {text}  -> {reply!r}")
    return len(bad)


# Personality: replies with Azel's character must keep humour to one sentence at most, none in serious cases, and
# stay within reply_length (normal: at most 6 sentences). Qwen judges the humour (a second, cheap local call).
def _gateway_personality():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../gateway/gateway.py")).read()
    block = re.search(r"PERSONALITY_DEFAULT = \((.*?)\n\)", src, re.S).group(1)
    return "".join(re.findall(r'"(.*?)"', block))


PERSONA_CASES = [  # (what the user says or the situation to announce, playful | light | serious)
    ("오늘 좀 심심하다", "playful"), ("주말에 뭐 하면 좋을까?", "playful"), ("너는 커피 좋아해?", "playful"),
    ("상황: 내일 오전 9시까지 과제 마감이 있어. 이걸 나한테 알려 줘.", "light"),
    ("상황: 서버 ruchba가 다운됐다는 경보가 왔어. 이걸 나한테 알려 줘.", "serious"),
    ("하아, 피곤하다", "serious"), ("요즘 너무 지쳐", "serious"),
    ("상황: macbook에서 git push 실행에 승인이 필요해. 이걸 나한테 알려 줘.", "serious"),
    ("상황: 카드 결제가 실패했어. 이걸 나한테 알려 줘.", "serious"),
    ("상황: 내일 아침 8시에 병원 예약이 있어. 이걸 나한테 알려 줘.", "serious"),
]
JUDGE = ("다음은 음성 비서의 답이야. 농담, 재치 있는 말, 장난스러운 표현이 들어간 문장이 몇 개인지 숫자 하나만 답해. "
         "없으면 0.\n\n답: ")


async def _collect(messages, max_tokens=600):
    out = ""
    async for d in vs.llm_stream(messages, max_tokens):
        out += d
    return out.strip()


async def persona_eval(persona):
    rows, foreign = [], []
    for text, kind in PERSONA_CASES:
        sysmsg = vs.system_prompt(vs.DEFAULT_NAME, "normal", "", persona) + vs.now_kst()
        reply = await _collect([{"role": "system", "content": sysmsg}, {"role": "user", "content": text}])
        if vs.ROUTE_TAG in reply:
            rows.append((text, kind, None, None, reply[:40]))
            continue
        verdict = await _collect([{"role": "user", "content": JUDGE + reply}], 10)
        jokes = int(m.group()) if (m := re.search(r"\d+", verdict)) else 0
        if vs.FOREIGN.search(reply):
            foreign.append((text, reply))
        rows.append((text, kind, jokes, len([x for x in vs._SENT_END.split(reply) if x.strip()]), reply))
    judged = [r for r in rows if r[2] is not None]
    bad_humor = [r for r in judged if (r[1] == "serious" and r[2] > 0) or r[2] > 1]
    too_long = [r for r in judged if r[3] > 6]
    label = "with persona" if persona else "no persona"
    print(f"[persona: {label}] humour violations {len(bad_humor)}/{len(judged)}, over length {len(too_long)}/"
          f"{len(judged)}, routed to Claude {len(rows) - len(judged)}, avg sentences "
          f"{sum(r[3] for r in judged) / max(1, len(judged)):.1f}")
    for r in bad_humor + too_long:
        print(f"  {r[1]:8} jokes={r[2]} sentences={r[3]} | {r[0]}  -> {r[4][:80]!r}")
    print(f"[persona: {label}] raw Qwen replies with Chinese/kana (the call path drops them): {len(foreign)}")
    for text, reply in foreign:
        print(f"  HAN  {text}  -> {reply[:80]!r}")
    return len(bad_humor) + len(too_long)


async def qwen_route(text):
    """'claude' if Qwen emits the routing tag first, else 'qwen' (stream closed as soon as it is decided)."""
    messages = [{"role": "system", "content": vs.system_prompt(vs.DEFAULT_NAME) + vs.now_kst()},
                {"role": "user", "content": text}]
    buf, gen = "", vs.llm_stream(messages)
    try:  # read on past the first words: "네. [CLAUDE]" is a hand-over too (the voice server routes it)
        async for delta in gen:
            buf += delta
            state = vs.tag_state(buf.lstrip())
            if state == "tag" or vs.ROUTE_TAG in buf:
                return C, buf
            if state != "maybe" and len(buf) > 60:
                return Q, buf
    finally:
        await gen.aclose()
    return (C if vs.ROUTE_TAG in buf else Q), buf


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
    persona_bad = 0
    if os.environ.get("PERSONA_EVAL", "1") != "0":
        await persona_eval("")  # before: no character (reported only)
        for _ in range(int(os.environ.get("PERSONA_ROUNDS", "2"))):
            persona_bad += await persona_eval(_gateway_personality())
    acks = 0
    for _ in range(ACK_ROUNDS):  # Qwen samples at temperature 0.7: several rounds show how often it happens
        acks += await ack_eval()
    await vs.client.aclose()
    return min(acc, acc2), leaked + leaked2 + acks + persona_bad


if __name__ == "__main__":
    acc, leaked = asyncio.run(main())
    sys.exit(0 if acc >= 0.9 and leaked == 0 else 1)
