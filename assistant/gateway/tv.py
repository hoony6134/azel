"""GET /v1/tv: one compact, read-only JSON for the Roku dashboard (assistant/roku/).

Built from pieces the gateway already has, each fetched on its own so one failing source leaves only its own part
`null`. Nothing here shows a command, a token or a message body: tasks by goal and status only, approvals as a
count. The answer is cached for TTL_S seconds, so a TV polling every 30 s never costs more than one build per window.
"""
import asyncio
import re
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
TTL_S = 30
SECTION_TIMEOUT_S = 8
LINES, WIDTH = 10, 90


def lines(text, limit=LINES):
    """Free text from a Cygnus/Google read → a few short display lines."""
    out = [" ".join(l.split())[:WIDTH] for l in str(text or "").splitlines()]
    return [l for l in out if l][:limit]


async def section(fn):
    """One source: its value, or None when it is missing, slow or failing (never an exception)."""
    try:
        return await asyncio.wait_for(fn(), SECTION_TIMEOUT_S)
    except Exception:  # noqa: BLE001 a dashboard shows what it can
        return None


async def build(d, now=None):
    """`d`: async callables name → data (see gateway.tv_deps). `now`: epoch seconds."""
    now = now or time.time()
    today = datetime.fromtimestamp(now, KST).date().isoformat()

    async def infra():
        if not d["infra_ok"]():
            return None
        h, s, a = await asyncio.gather(*(d["infra"](n) for n in ("infra_health", "infra_status", "infra_alerts")))
        return {"health": lines(h), "problems": lines(s), "alerts": lines(a)}

    async def tasks():
        rows = await d["tasks"](20)
        run = [t for t in rows if t.get("status") == "running"]
        return {"running": [{"goal": str(t.get("goal", ""))[:WIDTH], "status": t["status"]} for t in run[:5]],
                "queued": sum(t.get("status") in ("queued", "waiting_approval") for t in rows),
                "recent": [{"goal": str(t.get("goal", ""))[:WIDTH], "status": t.get("status")}
                           for t in rows if t.get("status") in ("done", "failed")][:3],
                "approvals": len(await d["approvals"]())}

    async def todo():
        if not d["infra_ok"]():
            return None
        t, a = await asyncio.gather(d["infra"]("gistra_today"), d["infra"]("gistra_assignments"))
        return {"today": lines(t), "assignments": lines(a)}

    async def calls():
        out = []
        for item, at in await d["fires"](now):
            if datetime.fromtimestamp(at, KST).date().isoformat() == today:
                out.append((at, str(item.get("purpose") or ("기상" if item.get("kind") == "wake" else "알림"))[:WIDTH]))
        return [{"at": datetime.fromtimestamp(at, KST).strftime("%H:%M"), "what": w} for at, w in sorted(out)[:8]]

    async def calendar():
        if not d["google_ok"]():
            return None
        return lines(await d["calendar"](today))

    async def assistant():
        return {"name": await d["persona"](), "state": "call" if d["in_call"]() else "idle"}

    names = ("infra", "tasks", "todo", "calls", "calendar", "assistant")
    vals = await asyncio.gather(*(section(f) for f in (infra, tasks, todo, calls, calendar, assistant)))
    return {"at": datetime.fromtimestamp(now, KST).isoformat(timespec="seconds"), **dict(zip(names, vals))}


# ---------- v2: the same sources, parsed into the fields the TV design needs (SPEC.md section 2.2) ----------
NODE = re.compile(r"^([✓✗⚠!])\s+(\S+)\s+\[[^\]]*\]\s*(.*)$")
STAMP = re.compile(r"As of (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})")
DUE = re.compile(r"(\d{1,2})월 (\d{1,2})일 \([월화수목금토일]\) (\d{1,2}):(\d{2})")
CAL = re.compile(r"^- (?:(\d{2}/\d{2})\([A-Za-z]{3}\) (\d{2}:\d{2})~(\d{2}:\d{2})|\d{4}-\d{2}-\d{2} 종일) (.+)$")
CLASS_TIME = re.compile(r"(\d{1,2}:\d{2})\s*[~–-]\s*(\d{1,2}:\d{2})")
CRIT = re.compile(r"crit|심각|\bP1\b|치명", re.I)


def _n(m, i):
    return int(m.group(i)) if m else None


def parse_nodes(text, now):
    """infra_health: `✓ name [group] cpu 3% · mem 50% · disk 29% · 74°C · load …`. A line that is not `✓` is down."""
    stamp = STAMP.search(text or "")
    seen = f"{stamp.group(1)}T{stamp.group(2)}:00+09:00" if stamp else None
    out = []
    for line in (text or "").splitlines():
        m = NODE.match(line.strip())
        if not m:
            continue
        rest, up = m.group(3), m.group(1) == "✓"
        out.append({"name": m.group(2), "up": up, "seen": seen if up else None,
                    "cpu": _n(re.search(r"cpu (\d+)%", rest), 1), "ram": _n(re.search(r"mem (\d+)%", rest), 1),
                    "disk": _n(re.search(r"disk (\d+)%", rest), 1), "temp": _n(re.search(r"(\d+)°C", rest), 1)})
    return out[:LINES]


def parse_down(text):
    """infra_status (problems only): `## node · what` headings, then `  ✗ service (type) DOWN · 24h 0.00%`."""
    out, node = [], None
    for line in (text or "").splitlines():
        if line.startswith("## "):
            node = line[3:].split(" · ")[0].strip()
        elif line.strip().startswith("✗"):
            name = re.sub(r"\s*\([^)]*\)\s*DOWN.*$", "", line.strip()[1:].strip())
            out.append({"service": name[:WIDTH], "node": node, "since": None})
    return out[:LINES]


HEADS = ((re.compile(r"disk|디스크", re.I), "디스크 부족"), (re.compile(r"mem|메모리", re.I), "메모리 부족"),
         (re.compile(r"cpu|load|부하", re.I), "CPU 높음"), (re.compile(r"temp|온도", re.I), "온도 높음"),
         (re.compile(r"down|멈춤|꺼짐|unreachable", re.I), "응답 없음"))


def alert_head(text):
    """A short run-in head for the TV card (SPEC 2.2), from the alert's own words; `경고` when nothing matches."""
    return next((h for pat, h in HEADS if pat.search(text)), "경고")


def parse_alerts(text):
    out = []
    for line in lines(text, 20):
        if re.match(r"(no firing alerts|지금 울리는 경보 없음)", line, re.I):
            continue
        out.append({"text": line.lstrip("-*• ")[:WIDTH], "head": alert_head(line), "level": "crit" if CRIT.search(line) else "warn", "at": None})
    return out[:LINES]


def _due_iso(m, now):
    mo, d, h, mi = (int(x) for x in m.groups())
    today = datetime.fromtimestamp(now, KST)
    y = today.year + (1 if (mo, d) < (today.month, today.day) and today.month - mo > 2 else 0)
    try:
        return datetime(y, mo, d, h, mi, tzinfo=KST).isoformat(timespec="seconds")
    except ValueError:
        return None


def parse_assignments(text, now):
    """gistra_assignments: `- ⬜ 11월 22일 (일) 23:00 · 과목 — 제목 [과제]` (due may be `마감 없음`; kind `할 일` = todo)."""
    assignments, todos = [], []
    for line in (text or "").splitlines():
        if not line.startswith("- "):
            continue
        body = re.sub(r"^- \S{0,2}\s*", "", line) if not line[2:3].isalnum() else line[2:]
        m = DUE.search(body)
        due = _due_iso(m, now) if m else None
        rest = body.split(" · ", 1)[1] if " · " in body else body
        kind = re.search(r"\[([^\]]+)\]\s*$", rest)
        rest = re.sub(r"\s*\[[^\]]+\]\s*$", "", rest).strip()
        course, _, title = rest.partition(" — ")
        if not title:
            course, title = None, rest
        if kind and kind.group(1) in ("할 일", "할일"):
            todos.append({"text": (f"{course} — {title}" if course else title)[:WIDTH], "due": due})
        else:
            assignments.append({"title": title[:WIDTH], "course": (course or "")[:40], "due": due})
    return assignments[:LINES], todos[:LINES]


def parse_classes(text):
    """gistra_today, section `### 수업`: `- 13:00~14:15 · 자료구조 · 대학 A동 212호` (or `없음`)."""
    out, on = [], False
    for line in (text or "").splitlines():
        if line.startswith("### "):
            on = line.strip() == "### 수업"
        elif on and line.startswith("- "):
            t = CLASS_TIME.search(line)
            parts = [p.strip() for p in CLASS_TIME.sub("", line[2:]).split(" · ") if p.strip(" ·-")]
            if t and parts:
                out.append({"start": t.group(1), "end": t.group(2), "name": parts[0][:WIDTH],
                            "room": (parts[1] if len(parts) > 1 else "")[:WIDTH]})
    return out[:LINES]


def parse_calendar(text):
    """google_tools calendar_events lines → [{start, end, what}] (all-day: start `종일`, end null)."""
    out = []
    for line in (text or "").splitlines():
        m = CAL.match(line.strip())
        if m:
            what = re.sub(r"\s+@ .*$", "", m.group(4))[:WIDTH]
            out.append({"start": m.group(2) or "종일", "end": m.group(3), "what": what})
    return out[:LINES]


PRIORITY_CAL = "MyTT"  # the user's timetable calendar: its events come first among those that start together


def calendar_events(rows):
    """Structured Google Calendar rows (google_tools.events) → the TV's timed events, from every calendar the user has.
    Timed events only (all-day ones are deadlines and holidays: GISTRA and the strip carry those); a calendar that is
    just an imported feed (its name is a URL, such as the LMS export) is left out. The same title at the same minute
    is shown once, and MyTT wins a tie."""
    out, seen = [], set()
    for r in sorted(rows, key=lambda r: (r.get("start", ""), 0 if r.get("cal") == PRIORITY_CAL else 1)):
        cal = r.get("cal") or ""
        if not r.get("start") or cal.startswith("http") or (r["start"], r["title"]) in seen:
            continue
        seen.add((r["start"], r["title"]))
        out.append({"start": r["start"], "end": r.get("end") or None, "what": r["title"][:WIDTH],
                    "cal": cal or "캘린더", "where": (r.get("where") or "")[:WIDTH]})
    return out[:LINES]


def iso(ts):
    return datetime.fromtimestamp(ts, KST).isoformat(timespec="seconds") if ts else None


async def build2(d, now=None):
    """GET /v1/tv?v=2. Each part is None when its source is missing or fails; approvals stay a count (and the oldest time)."""
    now = now or time.time()
    today = datetime.fromtimestamp(now, KST).date().isoformat()

    async def infra():
        if not d["infra_ok"]():
            return None
        h, s, a = await asyncio.gather(*(d["infra_raw"](n) for n in ("infra_health", "infra_status", "infra_alerts")))
        return {"nodes": parse_nodes(h, now), "alerts": parse_alerts(a), "down": parse_down(s)}

    async def tasks():
        rows, apps, night = await asyncio.gather(d["tasks"](20), d["approvals"](), d["overnight"]())
        queued = [t for t in rows if t.get("status") in ("queued", "waiting_approval")]
        created = [a.get("created") for a in apps if a.get("created")]
        return {"running": [{"goal": str(t.get("goal", ""))[:WIDTH], "since": iso(t.get("started"))}
                            for t in rows if t.get("status") == "running"][:5],
                "queued": {"count": len(queued), "next": str(queued[0].get("goal", ""))[:WIDTH] if queued else None},
                "approvals": {"count": len(apps), "oldest": iso(min(created)) if created else None},
                "overnight": night,
                "recent": [{"goal": str(t.get("goal", ""))[:WIDTH], "status": t["status"], "at": iso(t.get("finished"))}
                           for t in rows if t.get("status") in ("done", "failed")][:3]}

    async def gistra():
        if not d["infra_ok"]():
            return None
        a, t = await asyncio.gather(d["infra_raw"]("gistra_assignments"), d["infra_raw"]("gistra_today"))
        assignments, todos = parse_assignments(a, now)
        return {"classes": parse_classes(t), "assignments": assignments, "todos": todos}

    async def calls():
        out = []
        for item, at in await d["fires"](now):
            if datetime.fromtimestamp(at, KST).date().isoformat() == today:
                out.append((at, str(item.get("purpose") or ("기상" if item.get("kind") == "wake" else "알림"))[:WIDTH]))
        return [{"at": datetime.fromtimestamp(at, KST).strftime("%H:%M"), "what": w} for at, w in sorted(out)[:8]]

    async def calendar():
        if not d["google_ok"]():
            return None
        if "calendar_rows" in d:
            return calendar_events(await d["calendar_rows"](today))
        return parse_calendar(await d["calendar"](today))

    async def assistant():
        return {"state": "call" if d["in_call"]() else "idle", "persona": await d["persona2"]()}

    names = ("infra", "tasks", "gistra", "calls", "calendar", "assistant")
    vals = await asyncio.gather(*(section(f) for f in (infra, tasks, gistra, calls, calendar, assistant)))
    return {"at": iso(now), **dict(zip(names, vals))}


class Cache:
    def __init__(self):
        self.at, self.value, self.lock = 0.0, None, asyncio.Lock()

    async def get(self, make, ttl=TTL_S):
        async with self.lock:
            if self.value is None or time.monotonic() - self.at >= ttl:
                self.value, self.at = await make(), time.monotonic()
            return self.value
