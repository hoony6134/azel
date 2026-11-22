"""Digits as Korean speech, just before TTS. Replies keep digits ("1,333원", "2도", "3시 30분") for captions and
history; only the TTS input is rewritten ("천삼백삼십삼 원", "이 도", "세 시 삼십 분").

Sino-Korean by default (원, 도, %, 분, 년, 월, 일, 층 …), native Korean before counters (개, 명, 살, 번, 시간, 마리 …) and
for clock hours up to 12. 만/억/조 grouping, decimals digit by digit, a leading 0 read digit by digit (codes, phones).
"""
import re

DIGIT = "영일이삼사오육칠팔구"
NATIVE_ONES = ["", "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉"]
NATIVE_TENS = ["", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔"]
NATIVE_COUNTERS = ("개", "명", "살", "번", "시간", "마리", "잔", "권", "달", "군데", "가지", "켤레", "통", "대", "곳", "병", "장")
UNIT_WORDS = {"%": "퍼센트", "％": "퍼센트", "℃": "도", "°C": "도", "°": "도", "km": "킬로미터", "kg": "킬로그램",
              "cm": "센티미터", "mm": "밀리미터", "ml": "밀리리터", "m": "미터", "g": "그램", "GB": "기가바이트",
              "MB": "메가바이트", "TB": "테라바이트", "kWh": "킬로와트시", "W": "와트",
              # Sino-Korean units, read with a space: 천삼백 원, 이 도, 삼십 분
              **{u: u for u in ("달러", "원", "엔", "유로", "위안", "도", "분", "초", "층", "호", "위", "등", "배", "회", "점", "번지",
                                "세기", "학년", "주년", "주", "포인트", "개월", "년간", "인분")}}
MONTH = {6: "유월", 10: "시월"}


def sino(n):
    """1234 -> 천이백삼십사, 10000 -> 만, 100000000 -> 일억."""
    if n == 0:
        return "영"
    out, big = "", ["", "만", "억", "조", "경"]
    i = 0
    while n:
        n, chunk = divmod(n, 10000)
        if chunk:
            part = ""
            for d, name in ((1000, "천"), (100, "백"), (10, "십"), (1, "")):
                q, chunk = divmod(chunk, d)
                if q:
                    part += ("" if q == 1 and name else DIGIT[q]) + name
            if i == 1 and part == "일":
                part = ""  # 10000 is 만, not 일만
            out = part + big[i] + (" " + out if out else "")  # 십팔만 삼천백: a pause after each group
        i += 1
    return out


def native(n):
    """1-99 before a counter: 한, 스무, 서른다섯. Larger numbers stay Sino-Korean."""
    if not 0 < n < 100:
        return sino(n)
    tens, ones = divmod(n, 10)
    if n == 20:
        return "스무"
    return NATIVE_TENS[tens] + NATIVE_ONES[ones]


def digits(s):
    return "".join("공" if c == "0" else DIGIT[int(c)] for c in s)


def number(s):
    """A number token ("1,333", "18.5", "-3", "005930") read Sino-Korean."""
    neg = s.startswith(("-", "−"))
    s = s.lstrip("-−").replace(",", "")
    whole, _, frac = s.partition(".")
    if len(whole) > 1 and whole.startswith("0"):
        spoken = digits(whole)
    else:
        spoken = sino(int(whole or 0))
    if frac:
        spoken += " 점 " + "".join(DIGIT[int(c)] for c in frac)
    return ("마이너스 " if neg else "") + spoken


NUM = r"[-−]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-−]?\d+(?:\.\d+)?"
DATE = re.compile(r"(?<!\d)(\d{4})[-./](\d{1,2})[-./](\d{1,2})(?!\d)")  # Hangul counts as \w, so no \b here
CLOCK = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")
YMD = re.compile(r"(\d+)\s*(년|월|일)(?![가-힣])|(\d+)\s*(년|월|일)(?=\s|[.,!?]|$|부터|까지|에|은|는|이|도|의|째)")
HOUR = re.compile(rf"(\d{{1,2}})\s*시(?!간)")
UNIT = re.compile(rf"({NUM})\s*({'|'.join(map(re.escape, sorted(UNIT_WORDS, key=len, reverse=True)))})(?![A-Za-z])")
# 세 달 (months) but 일 달러: a counter is not the start of a longer unit
COUNTER = re.compile(rf"({NUM})\s*({'|'.join(NATIVE_COUNTERS)})(?!러)")
DOLLAR = re.compile(rf"\$\s*({NUM})")
PLAIN = re.compile(NUM)


def hour(h):
    h = int(h)
    return f"{native(h)} 시" if 0 < h <= 12 else f"{sino(h)} 시"


def month_day(n, unit):
    n = int(n)
    if unit == "월":
        return f"{MONTH.get(n, sino(n))} 월" if n not in MONTH else MONTH[n]
    return f"{sino(n)} {unit}"


def speakable(text):
    """The TTS input for a reply: every digit read out in Korean; anything without digits is returned as is."""
    if not re.search(r"\d", text):
        return text
    t = DATE.sub(lambda m: f"{sino(int(m[1]))} 년 {month_day(m[2], '월')} {sino(int(m[3]))} 일", text)
    t = CLOCK.sub(lambda m: hour(m[1]) + ("" if m[2] == "00" else f" {sino(int(m[2]))} 분"), t)
    t = DOLLAR.sub(lambda m: f"{number(m[1])} 달러", t)
    t = YMD.sub(lambda m: month_day(m[1] or m[3], m[2] or m[4]), t)
    t = HOUR.sub(lambda m: hour(m[1]), t)
    t = UNIT.sub(lambda m: f"{number(m[1])} {UNIT_WORDS[m[2]]}", t)
    t = COUNTER.sub(lambda m: f"{native(int(m[1].replace(',', '')))} {m[2]}"
                    if "." not in m[1] and not m[1].startswith(("-", "−")) else f"{number(m[1])} {m[2]}", t)
    t = PLAIN.sub(lambda m: number(m[0]), t)
    return re.sub(r"\s{2,}", " ", t)


CASES = [  # (written, spoken): the self-check, run by voice_server --check and `python ko_read.py`
    ("1달러에 1,333원이에요.", "일 달러에 천삼백삼십삼 원이에요."),
    ("지금 2도이고 최저 영하 4도예요.", "지금 이 도이고 최저 영하 사 도예요."),
    ("삼성전자는 183,100원에 마감했어요.", "삼성전자는 십팔만 삼천백 원에 마감했어요."),
    ("비 올 확률은 86%예요.", "비 올 확률은 팔십육 퍼센트예요."),
    ("18.5도, 습도 60%", "십팔 점 오 도, 습도 육십 퍼센트"),
    ("오후 3시 30분에 만나요.", "오후 세 시 삼십 분에 만나요."),
    ("15:00까지 내세요.", "십오 시까지 내세요."),
    ("회의는 9:05예요.", "회의는 아홉 시 오 분예요."),
    ("2026-11-22 일요일", "이천이십육 년 십일 월 이십이 일 일요일"),
    ("2026년 6월 10일이에요.", "이천이십육 년 유월 십 일이에요."),
    ("10월에 가요.", "시월에 가요."),
    ("사과 3개랑 학생 20명, 2시간 걸려요.", "사과 세 개랑 학생 스무 명, 두 시간 걸려요."),
    ("25살이고 1번만 할게요.", "스물다섯 살이고 한 번만 할게요."),
    ("인구는 10,349,312명이에요.", "인구는 천삼십사만 구천삼백십이 명이에요."),
    ("1억 원이 넘어요.", "일억 원이 넘어요."),
    ("10000원, 100000000원", "만 원, 일억 원"),
    ("종목 코드는 005930이에요.", "종목 코드는 공공오구삼공이에요."),
    ("$188 정도예요.", "백팔십팔 달러 정도예요."),
    ("5km를 3.2kg 들고", "오 킬로미터를 삼 점 이 킬로그램 들고"),
    ("기온이 -3도까지", "기온이 마이너스 삼 도까지"),
    ("3개월 동안 12시에 0시까지", "삼 개월 동안 열두 시에 영 시까지"),
    ("세 달 말고 3달이요.", "세 달 말고 세 달이요."),
    ("1등, 2층, 3위", "일 등, 이 층, 삼 위"),
    ("숫자 없는 문장이에요.", "숫자 없는 문장이에요."),
]


def check():
    bad = [(w, speakable(w), s) for w, s in CASES if speakable(w) != s]
    assert not bad, bad
    assert sino(0) == "영" and sino(11) == "십일" and sino(101) == "백일" and sino(20000) == "이만" and sino(110000) == "십일만"
    assert native(1) == "한" and native(99) == "아흔아홉" and native(100) == "백"


if __name__ == "__main__":
    check()
    print("ko_read ok")
