"""The user's schedule for a date range from both places it lives, merged: Google Calendar (google_tools.events)
and Gistra (Cygnus gistra_schedule: classes, exams, assignments, todos, meetings). One answer for every model path.

Rows are merged by day and time; a calendar event that Gistra also lists (same day, same start, same title) is
dropped, since Gistra's line says more (its kind: 수업, 과제, 회의 ...). Calendar lines are marked "(구글 캘린더)".
A source that fails is named in one short line; the other still answers. Nothing is ever made up.
"""
import asyncio
import re
from datetime import date as Date

MAX_DAYS = 31  # Gistra's own cap
SOURCE_TIMEOUT_S = 12
MAX_CHARS = 5400  # under the voice server's agenda cap (6000), notes included
WEEKDAY = "월화수목금토일"
GISTRA_DAY = re.compile(r"^#{2,4}\s*(\d{1,2})월\s*(\d{1,2})일")
GISTRA_ROW = re.compile(r"^-\s+(?:(\d{1,2}:\d{2})(?:~(\d{1,2}:\d{2}))?\s+)?(.+?)(?:\s*\[[\w.-]+\])?\s*$")
# what cygnus.call returns instead of a result
GISTRA_FAIL = ("이 기기에는 Cygnus", "지금은 Cygnus", "Cygnus가 거절", "도구 인자가", "모르는 도구")


def span(f, t):
    """(from, to) as dates, checked: to on or after from, at most MAX_DAYS apart. ValueError otherwise."""
    f, t = Date.fromisoformat(str(f)), Date.fromisoformat(str(t))
    if not 0 <= (t - f).days <= MAX_DAYS:
        raise ValueError(f"기간은 {MAX_DAYS}일 안쪽")
    return f, t


def parse_gistra(text, f, t):
    """gistra_schedule's text -> rows {day, start, end, text, src}. Day headers carry no year: the one that puts the
    day inside [f, t] wins (a range over New Year)."""
    rows, day = [], None
    for line in str(text or "").splitlines():
        line = line.strip()
        if m := GISTRA_DAY.match(line):
            mo, d = int(m[1]), int(m[2])
            day = next((x for y in (f.year, t.year) if (x := _date(y, mo, d)) and f <= x <= t), None)
        elif day and (m := GISTRA_ROW.match(line)):
            rows.append({"day": day, "start": _hhmm(m[1]), "end": _hhmm(m[2]), "text": m[3].strip(), "src": "gistra"})
    return rows


def _date(y, m, d):
    try:
        return Date(y, m, d)
    except ValueError:
        return None


def _hhmm(s):
    return f"{int(s.split(':')[0]):02d}:{s.split(':')[1]}" if s else ""


def _norm(s):
    return re.sub(r"\s+", "", str(s)).lower()


def merge(cal, gis):
    """Both lists into one, by day then time (all-day first); a calendar row Gistra also has is dropped."""
    def dup(c):
        return any(g["day"] == c["day"] and g["start"] == c["start"] and _norm(c["title"]) and
                   _norm(c["title"]) in _norm(g["text"]) for g in gis)
    rows = gis + [{**c, "text": c["title"] + (f" @ {c['where']}" if c.get("where") else "") + " (구글 캘린더)",
                   "src": "calendar"} for c in cal if not dup(c)]
    return sorted(rows, key=lambda r: (r["day"], r["start"] or "", r["src"] != "gistra"))


def render(rows, f, t, notes=()):
    head = f"일정 {f}~{t} (구글 캘린더와 Gistra를 합친 것, KST)" if f != t else f"일정 {f} (구글 캘린더와 Gistra를 합친 것, KST)"
    out, day = [head], None
    for r in rows:
        if r["day"] != day:
            day = r["day"]
            out.append(f"### {day.month}/{day.day}({WEEKDAY[day.weekday()]})")
        when = f"{r['start']}~{r['end']}" if r["start"] and r["end"] else r["start"] or "종일"
        out.append(f"- {when} {r['text']}")
    if not rows:
        out.append("이 기간에 일정 없음" if len(notes) < 2 else "")
    if sum(len(x) + 1 for x in out) > MAX_CHARS:  # a long month: whole days up to the cap, and the cut is named
        n = size = 0
        while n < len(out) and size + len(out[n]) + 1 <= MAX_CHARS:
            size += len(out[n]) + 1
            n += 1
        while n > 1 and not out[n].startswith("### "):  # never half a day: back to the last whole one
            n -= 1
        days = [x[4:] for x in out[:n] if x.startswith("### ")]
        out = out[:n] + [f"(이후 생략: {days[-1]}까지만 보여요. 그 뒤 일정은 기간을 줄여 다시 물어봐.)" if days else "(이후 생략)"]
    out += list(notes)
    return "\n".join(x for x in out if x).strip()


async def merged(f, t, calendar, gistra):
    """calendar(f, t) -> rows {day, start, end, title, where} and gistra(f, t) -> gistra_schedule text, both run at
    once; either may raise (expired grant, no token, network). Returns the text a model reads."""
    f, t = span(f, t)

    async def one(fn):
        return await asyncio.wait_for(fn(f.isoformat(), t.isoformat()), SOURCE_TIMEOUT_S)
    c, g = await asyncio.gather(one(calendar), one(gistra), return_exceptions=True)
    if isinstance(g, str) and g.startswith(GISTRA_FAIL):
        g = RuntimeError(g)
    notes = []
    if isinstance(c, BaseException):
        notes.append(f"(구글 캘린더는 지금 못 읽었어요: {_why(c)}. 위는 Gistra 일정뿐이에요.)")
        c = []
    if isinstance(g, BaseException):
        notes.append(f"(Gistra는 지금 못 읽었어요: {_why(g)}. 위는 구글 캘린더 일정뿐이에요.)")
        g = ""
    if len(notes) == 2:
        return render([], f, t, ["일정을 지금 확인할 수 없어요. 구글 캘린더도 Gistra도 읽지 못했어요. 일정을 짐작해서 말하지 마."])
    return render(merge(c, parse_gistra(g, f, t)), f, t, notes)


def _why(e):
    msg = str(e).strip()
    if isinstance(e, asyncio.TimeoutError):
        return "응답이 늦음"
    return msg[:80] if msg and re.search(r"[가-힣]", msg) else type(e).__name__
