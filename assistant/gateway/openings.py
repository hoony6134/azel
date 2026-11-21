"""What a placed call says first. Never the user's request read back ("…과제하라고 전화해줘"): at creation the request
becomes a short purpose ("역사 특강 과제"); at fire time the opening is composed from the user's title, the time, the
kind and that purpose, by Qwen under a length cap, else by a template.

`llm(system, user)` is the gateway's llm_json: the first {...} Qwen answers, or None.
"""
import re

MAX_PURPOSE = 30
MAX_OPENING = 80
TIME_RE = re.compile(r"(매일|평일|주중|매주\s*[월화수목금토일]요일|오늘|내일|모레|글피|[월화수목금토일]요일)\s*|"
                     r"(오전|오후|아침|저녁|밤|낮|새벽)?\s*[0-9한두세네다섯여일곱덟아홉열]+\s*시\s*(반|[0-9]+\s*분)?\s*(쯤|에|까지)?\s*|"
                     r"[0-9]+\s*(분|시간)\s*(뒤|후)에?\s*")
CALL_RE = re.compile(r"\s*(나한테|저한테|좀|\s)*(전화(로)?|연락)?(\s|좀)*(해|걸어|해서|걸어서)?\s*(깨워|알려)?\s*"
                     r"(줘|줄래|주세요|줘요|주라|달라|줄\s*수\s*있어\??|주실래요\??)?[.!?~ ]*$")
# quotative / imperative tails: "하라고", "해 달라고", "하도록", "먹으라고", "알려 달라고", "-라고"
QUOTE_RE = re.compile(r"\s*((이\s*)?있다고|하라고|하래|해\s*달라고|하도록|할\s*수\s*있게|하는\s*거\s*알려\s*달라고|알려\s*달라고|"
                      r"(으)?라고|(으)?래)$")
LEAK_RE = re.compile(r"(하라고|라고\b|라고[ .,]|해\s*줘|해\s*달라|전화해|깨워\s*줘|알려\s*줘|줘\.|줘$)")


def purpose_rule(raw):
    """'오후 1시에 역사 특강 과제하라고 전화해줘' -> '역사 특강 과제'; '약 먹으라고' -> '약 먹기'; '' when nothing is left."""
    text = CALL_RE.sub("", TIME_RE.sub(" ", str(raw or ""))).strip()  # the call verb only once: "엄마한테 전화" stays
    for _ in range(3):
        m = QUOTE_RE.search(text)
        if not m:
            break
        verb = m.group(0).strip() not in ("하라고", "하래", "해 달라고", "해달라고", "하도록") and \
            m.group(0).strip().endswith(("라고", "래")) and "알려" not in m.group(0) and "있다고" not in m.group(0)
        text = text[:m.start()].strip()
        if verb and text and not text.endswith("하"):  # "약 먹으라고" -> "약 먹기"
            text += "기"
        text = re.sub(r"하$", "", text).strip()  # "과제하" -> "과제"
    text = re.sub(r"\s*알림$", "", re.sub(r"^(에|에는)\s+", "", text)).strip(" ,.")
    return text[:MAX_PURPOSE] if valid_purpose(text) else ""


def valid_purpose(p):
    return isinstance(p, str) and 0 < len(p.strip()) <= MAX_PURPOSE and not LEAK_RE.search(p) and not QUOTE_RE.search(p)


async def purpose(raw, llm=None):
    """The normalized purpose of a request: Qwen's, when it passes the checks, else the rule's."""
    if llm and raw:
        try:
            d = await llm("사용자가 비서에게 예약 전화를 부탁한 말에서 '전화할 목적'만 짧은 명사구로 뽑아. 시각, '전화해줘', "
                          "'-하라고', '-해 달라고', '-라고' 같은 명령·인용 어미는 빼. 예: '오후 1시에 역사 특강 과제하라고 전화해줘' -> "
                          "'역사 특강 과제', '7시에 약 먹으라고 깨워줘' -> '약 먹기'. 목적이 없으면 빈 문자열. "
                          'JSON 하나만: {"purpose": "..."}', str(raw)[:300])
            p = str((d or {}).get("purpose", "")).strip(" .'\"")
            if d is not None and (p == "" or valid_purpose(p)):
                return p
        except Exception:  # noqa: BLE001  Qwen down: the rule below
            pass
    return purpose_rule(raw)


def clock_said(hour, minute):
    return f"{hour % 12 or 12}시" + (f" {minute}분이에요" if minute else "예요")


def _attributive(stem):
    """'먹' -> '먹을', '가' -> '갈', '놀' -> '놀' (a verb stem before 시간)."""
    c = ord(stem[-1]) - 0xAC00
    if not 0 <= c < 11172:
        return stem + "을"
    if c % 28 == 0:
        return stem[:-1] + chr(ord(stem[-1]) + 8)  # add the final ㄹ
    return stem if c % 28 == 8 else stem + "을"


def what_time(p):
    """'역사 특강 과제' -> '역사 특강 과제 하실 시간'; '약 먹기' -> '약 먹을 시간'."""
    return f"{_attributive(p[:-1])} 시간" if p.endswith("기") and len(p) > 1 else f"{p} 하실 시간"


def template(kind, title, hour, minute, purpose_text="", goal=""):
    """The fallback opening, and the shape Qwen is asked to follow."""
    hi = f"{title}, " if title else ""
    if kind in ("task", "task_done"):
        return f"{hi}맡기신 {goal or '작업'} 끝났어요."
    if kind == "wake":
        rest = f" {purpose_text} 잊지 마세요." if purpose_text else ""
        return f"{hi}좋은 아침이에요. {clock_said(hour, minute)}. 일어나실 시간이에요.{rest}"
    why = f"{what_time(purpose_text)}이라" if purpose_text else "말씀하신 시간이라"
    return f"{hi}{clock_said(hour, minute)}. {why} 전화드렸어요."


def clean_opening(text, raw="", purpose_text="", title="", said_time=""):
    """Qwen's opening is used only when short, free of imperative/quotative endings, not the raw request, and it
    names the purpose, the title and the time as said ("3시 30분이에요", not "이었어요") it was given."""
    if not isinstance(text, str) or not 0 < len(text.strip()) <= MAX_OPENING or "\n" in text.strip():
        return False
    flat = text.replace(" ", "")
    body = TIME_RE.sub("", str(raw or "")).replace(" ", "")
    return not LEAK_RE.search(text) and not (len(body) >= 6 and body in flat) and \
        (not purpose_text.strip() or purpose_text.split()[0][:3] in flat) and (not title or title in text) and \
        (not said_time or said_time in text)


async def compose(kind, title, hour, minute, purpose_text="", goal="", raw="", persona="", llm=None):
    """The opening sentence(s) for a placed call: Qwen in the persona's voice, else the template."""
    base = template(kind, title, hour, minute, purpose_text, goal)
    if llm:
        try:
            d = await llm(f"너는 이 비서야: {persona}\n예약된 전화를 걸어 첫마디를 해. 한두 문장, {MAX_OPENING}자 이내, 해요체('-예요', '-드렸어요'). "
                          "사용자가 부탁한 말을 그대로 읽지 말고, '-하라고', '-해줘', '-라고' 같은 말투를 쓰지 마. "
                          f"호칭이 있으면 그 호칭으로 불러. 이 문장을 네 말투로 자연스럽게 바꿔: {base}\n"
                          'JSON 하나만: {"opening": "..."}',
                          f"종류: {kind}, 호칭: {title or '없음'}, 시각: {clock_said(hour, minute)}, "
                          f"목적: {purpose_text or goal or '없음'}")
            text = str((d or {}).get("opening", "")).strip()
            said = "" if kind in ("task", "task_done") else clock_said(hour, minute)
            if clean_opening(text, raw, purpose_text or goal, title, said):
                return text
        except Exception:  # noqa: BLE001  Qwen down: the template
            pass
    return base
