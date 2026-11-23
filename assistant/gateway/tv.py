"""GET /v1/tv: one compact, read-only JSON for the Roku dashboard (assistant/roku/).

Built from pieces the gateway already has, each fetched on its own so one failing source leaves only its own part
`null`. Nothing here shows a command, a token or a message body: tasks by goal and status only, approvals as a
count. The answer is cached for TTL_S seconds, so a TV polling every 30 s never costs more than one build per window.
"""
import asyncio
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
TTL_S = 30
SECTION_TIMEOUT_S = 8
LINES, WIDTH = 10, 90


def plain(text):
    """Display text without pictographs (🔁, ⬜, ✅…): the TV's Hangul-subset font has no glyph for them and draws a blank
    gap instead, which pushed the first word of a card out of line. Letters, digits, punctuation and ·, ~, ° stay."""
    out = "".join(c for c in str(text or "") if c in "°℃" or not (unicodedata.category(c) in ("So", "Cs", "Co")
                                                   or c in "\uFE0F\u200D" or ord(c) > 0xFFFF))
    return " ".join(out.split())


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
MARKS = "✓✗⚠!"
NODE = re.compile(r"^(?P<mark>[✓✗⚠!]|정상|주의|문제|꺼짐)\s+(?P<name>\S+)\s+\[(?P<group>[^\]]*)\]\s*(?P<rest>.*)$")
STAMP = re.compile(r"As of (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})")
SNAP = re.compile(r"스냅샷:.*?(\d{1,2}:\d{2})")
DUE = re.compile(r"(\d{1,2})월 (\d{1,2})일 \([월화수목금토일]\) (\d{1,2}):(\d{2})")
DATE_LEAD = re.compile(r"^(\d{1,2})월 (\d{1,2})일(?: \([월화수목금토일]\))?(?: (\d{1,2}):(\d{2}))?")
CAL = re.compile(r"^- (?:(\d{2}/\d{2})\([A-Za-z]{3}\) (\d{2}:\d{2})~(\d{2}:\d{2})|\d{4}-\d{2}-\d{2} 종일) (.+)$")
CLASS_TIME = re.compile(r"(\d{1,2}:\d{2})\s*[~–-]\s*(\d{1,2}:\d{2})")
TIME_LEAD = re.compile(r"^(\d{1,2}:\d{2})\s*[~–-]\s*(\d{1,2}:\d{2})\s*")
CRIT = re.compile(r"crit|심각|\bP1\b|치명", re.I)
SERVICE = re.compile(r"^(?P<name>.+?)(?: \((?P<type>[A-Za-z]{2,6})\))?\s+(?P<state>멈춤|DOWN|꺼짐|느려짐|degraded|slow)(?:\s*·\s*(?P<note>.+))?$", re.I)
STATUS_WORDS = ("새로 배움", "익히는 중", "복습 중", "익힘")


def _n(m, i):
    return int(m.group(i)) if m else None


def _status(mark, rest):
    """✓/정상 ok, ⚠/주의/! warn; ✗/문제 a problem; 꺼짐 or a line with no figures and DOWN is off (not responding)."""
    if mark in ("✓", "정상"):
        return "ok"
    if mark in ("⚠", "!", "주의"):
        return "warn"
    if mark == "꺼짐" or re.search(r"DOWN|멈춤|꺼짐|unreachable", rest, re.I) or not re.search(r"cpu \d", rest):
        return "off"
    return "problem"


def parse_hosts(text, now):
    """infra_health: `정상 albireo [cygnus] cpu 0% · mem 68% · disk 15% · load 0.00/2 · uptime 142d` (or `✓` for 정상).
    Returns (hosts, "17:04" from the `As of …` line). Hosts keep their bracket group; a line that does not fit is
    skipped here and kept in `infra.notes` by the caller."""
    stamp = STAMP.search(text or "")
    asof = stamp.group(2) if stamp else None
    seen = f"{stamp.group(1)}T{stamp.group(2)}:00+09:00" if stamp else None
    out = []
    for line in (text or "").splitlines():
        m = NODE.match(line.strip())
        if not m:
            continue
        rest, status = m.group("rest"), _status(m.group("mark"), m.group("rest"))
        load = re.search(r"load ([\d.]+/\d+|[\d.]+)", rest)
        up = re.search(r"uptime (\S+)", rest)
        out.append({"name": m.group("name"), "group": m.group("group"), "status": status, "up": status != "off",
                    "seen": seen if status != "off" else None,
                    "cpu": _n(re.search(r"cpu (\d+)%", rest), 1), "mem": _n(re.search(r"mem (\d+)%", rest), 1),
                    "disk": _n(re.search(r"disk (\d+)%", rest), 1), "temp": _n(re.search(r"(\d+)°C", rest), 1),
                    "load": load.group(1) if load else None, "uptime": up.group(1) if up else None})
    return out[:LINES * 2], asof


def parse_nodes(text, now):
    return parse_hosts(text, now)[0]


def parse_notes(text):
    """Lines of a Cygnus text that no parser claimed (a header, a new format): shown as plain text, never dropped."""
    out = []
    for line in lines(text, 30):
        if NODE.match(line) or STAMP.search(line) or line.startswith("#"):
            continue
        out.append(line[:WIDTH])
    return out[:3]


def parse_down(text):
    """infra_status (problems only): `# Cygnus · Ops` and `## vd-spark · Vuja De GPU` are group labels, then
    `  문제 Vuja De claw (NemoClaw) (http) 멈춤 · 24h 0.00%` (or `✗ … DOWN`). Returns the services with a state."""
    out, node, group = [], None, None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            node, _, group = (x.strip() for x in line[3:].partition(" · "))
        elif line.startswith("#"):
            continue
        elif line and (line[0] in "✗⚠!" or line.split(" ", 1)[0] in ("문제", "주의", "꺼짐")):
            body = line[1:].strip() if line[0] in "✗⚠!" else line.split(" ", 1)[1].strip()
            m = SERVICE.match(body)
            if m:
                state = "degraded" if m.group("state").lower() in ("느려짐", "degraded", "slow") else "down"
                out.append({"service": m.group("name")[:WIDTH], "node": node, "group": group, "type": m.group("type"),
                            "state": state, "note": (m.group("note") or "")[:WIDTH] or None, "since": None})
            else:  # a problem line in a format we do not know: still shown, as plain text
                out.append({"service": body[:WIDTH], "node": node, "group": group, "type": None, "state": "down",
                            "note": None, "since": None})
    return out[:LINES]


HEADS = ((re.compile(r"disk|디스크", re.I), "디스크 부족"), (re.compile(r"mem|메모리", re.I), "메모리 부족"),
         (re.compile(r"cpu|load|부하", re.I), "CPU 높음"), (re.compile(r"temp|온도", re.I), "온도 높음"),
         (re.compile(r"down|멈춤|꺼짐|unreachable", re.I), "응답 없음"))


def alert_head(text):
    """A short run-in head for the TV card (SPEC 2.2), from the alert's own words; `경고` when nothing matches."""
    return next((h for pat, h in HEADS if pat.search(text)), "경고")


def parse_alerts(text):
    """infra_alerts: one alert per line. `#` headers are group labels, `No firing alerts.` is none."""
    out, group = [], None
    for line in lines(text, 20):
        if line.startswith("#"):
            group = line.lstrip("# ").split(" · ")[0].strip() or None
            continue
        if re.match(r"(no firing alerts|지금 울리는 경보 없음)", line, re.I):
            continue
        out.append({"text": plain(line.lstrip("-*• "))[:WIDTH], "head": alert_head(line),
                    "level": "crit" if CRIT.search(line) else "warn", "at": None, "group": group})
    return out[:LINES]


def _due_iso(m, now):
    return _iso_at(*(int(x) for x in m.groups()), now)


def _iso_at(mo, d, h, mi, now):
    today = datetime.fromtimestamp(now, KST)
    y = today.year + (1 if (mo, d) < (today.month, today.day) and today.month - mo > 2 else 0)
    try:
        return datetime(y, mo, d, h, mi, tzinfo=KST).isoformat(timespec="seconds")
    except ValueError:
        return None


def parse_item(line, now):
    """One `- …` line of a Gistra list → {when, due, date, kind, title, note, tag}. Tolerant: a line it cannot split
    is a plain item with the whole text as `title`. Shapes seen:
      `⬜ 11월 23일 (월) 23:59 · 하우스 문서 — 문서 부착 공지 [할 일]`   date · kind — title [tag]
      `⬜ 마감 없음 · 대외부 — 인수인계 기록지 작성 [할 일]`             no date
      `11월 22일 (일) 현대 예술의 이해[01] · 강의 개요 — 새 토론글이 …`  date only, course · topic — note
      `프로그래밍 언어 · Lexical Analysis (익히는 중) — 예정 11월 1일 (일)`  no date, course · topic (status) — note
      `🔁 20:00~22:00 지온나래 · 지온나래 정기회의 (정기 회의) · 구관 토론실`   time range club · title · place"""
    body = plain(re.sub(r"^[-*]\s*", "", line.strip()))
    when = due = date = None
    m, t = DATE_LEAD.match(body), TIME_LEAD.match(body)
    if t:  # an activity: `20:00~22:00 club · title · place`, the club up to the first ` · `
        when, body = f"{t.group(1)}~{t.group(2)}", body[t.end():]
        club, _, rest = body.partition(" · ")
        title, _, place = rest.rpartition(" · ") if rest.count(" · ") else (rest, "", "")
        return {"when": when, "due": None, "date": None, "kind": club.strip()[:40], "title": (title or rest).strip()[:WIDTH],
                "note": place.strip()[:WIDTH], "tag": None}
    if m:
        mo, d, h, mi = m.groups()
        when = f"{int(mo)}/{int(d)}" + (f" {int(h):02d}:{mi}" if h else "")
        due = _iso_at(int(mo), int(d), int(h), int(mi), now) if h else None
        date = _iso_at(int(mo), int(d), 0, 0, now)[:10] if _iso_at(int(mo), int(d), 0, 0, now) else None
        body = body[m.end():]
        body = re.sub(r"^\s*\([월화수목금토일]\)", "", body)
    elif body.startswith("마감 없음"):
        when, body = "마감 없음", body[len("마감 없음"):]
    body = re.sub(r"^\s*(?:·|-)\s*", "", body).strip()
    tag = re.search(r"\[([^\]]+)\]\s*$", body)
    if tag:
        body = body[:tag.start()].strip()
    left, _, right = body.partition(" — ")
    kind = title = note = ""
    if " · " in left:
        kind, _, title = left.partition(" · ")
        note = right
    elif right:
        kind, title = left, right
    else:
        title = left
    status = re.search(r"\s*\(([^)]+)\)\s*$", title)
    if status and status.group(1) in STATUS_WORDS:
        note = f"{status.group(1)} · {note}" if note else status.group(1)
        title = title[:status.start()]
    if not kind and tag:
        kind = tag.group(1)
    return {"when": when, "due": due, "date": date, "kind": kind.strip()[:40], "title": title.strip()[:WIDTH] or body[:WIDTH],
            "note": note.strip()[:WIDTH], "tag": tag.group(1) if tag else None}


def parse_sections(text, now):
    """A Gistra text → (snapshot "15:41" or None, [{title, items}]). `스냅샷:` is the timestamp, `#`/`##`/`###` headers
    open sections (the trailing count `(6)` is dropped, a `YYYY-MM-DD 기준` header is not a section), `- ` lines are
    items, `없음` under a header means an empty section, any other line is kept as a plain item."""
    snap, sections, cur = None, [], None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("스냅샷"):
            m = SNAP.search(line)
            snap = m.group(1) if m else None
            continue
        if line.startswith("#"):
            title = re.sub(r"\s*\(\d+\)\s*$", "", line.lstrip("# ").strip())
            if re.match(r"\d{4}-\d{2}-\d{2}", title) and title.endswith("기준"):
                continue
            cur = {"title": title[:WIDTH], "items": []}
            sections.append(cur)
        elif line == "없음":
            continue
        else:
            if cur is None:
                cur = {"title": "", "items": []}
                sections.append(cur)
            item = parse_item(line, now) if line.startswith(("-", "*")) else \
                {"when": None, "due": None, "kind": "", "title": plain(line)[:WIDTH], "note": "", "tag": None}
            cur["items"].append(item)
    for sec in sections:
        sec["items"] = sec["items"][:LINES]
    return snap, [s for s in sections if s["title"] or s["items"]]


def parse_assignments(text, now):
    """gistra_assignments `남은 항목` → (assignments, todos): each with kind (the course or category), title, due and the
    one-line `text` the first TV version used. `[할 일]` items are todos."""
    assignments, todos = [], []
    for sec in parse_sections(text, now)[1]:
        for it in sec["items"]:
            kind = it["kind"] if it["kind"] != it["tag"] else ""
            if it["tag"] in ("할 일", "할일"):
                todos.append({"text": (f"{kind} — {it['title']}" if kind else it["title"])[:WIDTH], "due": it["due"],
                              "kind": kind, "title": it["title"]})
            else:
                assignments.append({"title": it["title"], "course": kind[:40], "due": it["due"]})
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
            what = plain(re.sub(r"\s+@ .*$", "", m.group(4)))[:WIDTH]
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
        out.append({"start": r["start"], "end": r.get("end") or None, "what": plain(r["title"])[:WIDTH],
                    "cal": cal or "캘린더", "where": plain(r.get("where"))[:WIDTH]})
    return out[:LINES]


DEADLINE_CALS = ("과제/평가", "GiSTRA")


def next_deadline(gistra, calendar, now, today):
    """The earliest upcoming deadline from Gistra (assignments, todos, any dated list item) or the calendar (an event in a
    deadline calendar, or one whose title says 마감) → {title, due, course, source}, or None."""
    found = []
    if gistra:
        for a in gistra.get("assignments", []):
            found.append((a.get("due"), a["title"], a.get("course"), "gistra"))
        for t in gistra.get("todos", []):
            found.append((t.get("due"), t.get("title") or t["text"], t.get("kind"), "gistra"))
        for sec in gistra.get("sections", []):
            for it in sec["items"]:
                if it.get("due"):
                    found.append((it["due"], it["title"], it["kind"], "gistra"))
    for e in calendar or []:
        if (e.get("cal") in DEADLINE_CALS or "마감" in e["what"]) and e.get("start"):
            found.append((f"{today}T{e['start']}:00+09:00", e["what"], e.get("cal"), "calendar"))
    future = []
    for due, title, course, source in found:
        try:
            t = datetime.fromisoformat(due).timestamp() if due else 0
        except ValueError:
            t = 0
        if t > now:
            future.append((t, due, title, course, source))
    if not future:
        return None
    _, due, title, course, source = min(future)
    return {"title": title, "due": due, "course": course or "", "source": source}


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
        hosts, asof = parse_hosts(h, now)
        return {"asof": asof, "hosts": hosts, "alerts": parse_alerts(a), "down": parse_down(s), "notes": parse_notes(h)}

    async def tasks():
        rows, apps, night = await asyncio.gather(d["tasks"](20), d["approvals"](), d["overnight"]())
        queued = [t for t in rows if t.get("status") in ("queued", "waiting_approval")]
        created = [a.get("created") for a in apps if a.get("created")]
        return {"running": [{"goal": plain(t.get("goal"))[:WIDTH], "since": iso(t.get("started"))}
                            for t in rows if t.get("status") == "running"][:5],
                "queued": {"count": len(queued), "next": plain(queued[0].get("goal"))[:WIDTH] if queued else None},
                "approvals": {"count": len(apps), "oldest": iso(min(created)) if created else None},
                "overnight": night,
                "recent": [{"goal": plain(t.get("goal"))[:WIDTH], "status": t["status"], "at": iso(t.get("finished"))}
                           for t in rows if t.get("status") in ("done", "failed")][:3]}

    async def gistra():
        if not d["infra_ok"]():
            return None
        a, t = await asyncio.gather(d["infra_raw"]("gistra_assignments"), d["infra_raw"]("gistra_today"))
        assignments, todos = parse_assignments(a, now)
        snap, today_sections = parse_sections(t, now)
        sections = [x for x in today_sections if x["title"] != "수업"] + parse_sections(a, now)[1]
        return {"asof": snap, "classes": parse_classes(t), "assignments": assignments, "todos": todos,
                "sections": sections}

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

    async def weather():
        return weather_card(await d["weather"](), now) if "weather" in d else None

    async def holdings():
        return await d["holdings"]() if "holdings" in d else None

    names = ("infra", "tasks", "gistra", "calls", "calendar", "assistant", "weather", "holdings")
    vals = await asyncio.gather(*(section(f) for f in (infra, tasks, gistra, calls, calendar, assistant, weather, holdings)))
    out = dict(zip(names, vals))
    return {"at": iso(now), **out, "next_deadline": next_deadline(out["gistra"], out["calendar"], now, today)}


# ---------- ?detail=infra|tasks: what the TV's detail screens show (a status page per host and service, the task queue) ----------
MONITOR = re.compile(r"^(?P<mark>[✓✗⚠!])\s+(?P<name>.+?) \((?P<type>[a-z-]{2,12})\) (?P<state>up|down|pending|maintenance|degraded)"
                     r"(?:\s*·\s*24h (?P<pct>[\d.]+)%)?(?:\s*·\s*(?P<ms>\d+)ms)?(?:\s*·\s*last down (?P<last>[\d-]+ [\d:]+))?", re.I)


def parse_monitors(text):
    """infra_status (all monitors): `## deneb · Public Edge · Identity` opens a node group, then
    `  ✓ Nginx Proxy Manager (http) up · 24h 100.00% · 15ms` or `✗ Vuja De claw (NemoClaw) (http) DOWN · 24h 2.85%`.
    → [{node, role, services: [{name, type, state, uptime, ms, last_down}]}]. A service line it can't read is kept with
    its text as the name and state `unknown`, never dropped."""
    groups, cur = [], None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            node, _, role = (x.strip() for x in line[3:].partition(" · "))
            cur = {"node": node, "role": role, "services": []}
            groups.append(cur)
        elif not line or line.startswith("#") or cur is None:
            continue
        else:
            m = MONITOR.match(line)
            if m:
                st = m.group("state").lower()
                cur["services"].append({
                    "name": plain(m.group("name"))[:60], "type": m.group("type").lower(),
                    "state": "up" if st == "up" else "degraded" if st in ("pending", "degraded") else
                    "maintenance" if st == "maintenance" else "down",
                    "uptime": float(m.group("pct")) if m.group("pct") else None,
                    "ms": int(m.group("ms")) if m.group("ms") else None, "last_down": m.group("last")})
            else:
                cur["services"].append({"name": plain(line.lstrip("✓✗⚠! "))[:60], "type": None, "state": "unknown",
                                        "uptime": None, "ms": None, "last_down": None})
    return groups


async def build_infra_detail(d, now=None):
    """GET /v1/tv?detail=infra: every host (health) and every monitored service by node (status), like the Cygnus
    status page. None parts when Cygnus is unreachable; `checked` is when the gateway read it."""
    now = now or time.time()
    if not d["infra_ok"]():
        return {"at": iso(now), "hosts": None, "groups": None, "alerts": None}
    h, s, a = await asyncio.gather(d["infra_raw"]("infra_health"), d["infra_raw"]("infra_status", {"problems_only": False}),
                                   d["infra_raw"]("infra_alerts"))
    hosts, asof = parse_hosts(h, now)
    groups = parse_monitors(s)
    hist = {"cpu": {}, "mem": {}}
    if "query" in d:
        cpu, mem = await asyncio.gather(*(section(lambda m=m: d["query"](history_query(m))) for m in ("cpu", "mem")))
        hist = {"cpu": parse_history(cpu), "mem": parse_history(mem)}
    for host in hosts:
        host.update(cpu_24h=hist["cpu"].get(host["name"]), mem_24h=hist["mem"].get(host["name"]))
    roles = {g["node"]: g["role"] for g in groups}
    for host in hosts:
        svc = next((g["services"] for g in groups if g["node"] == host["name"]), [])
        host.update(role=roles.get(host["name"], ""), services=len(svc),
                    services_down=sum(x["state"] in ("down", "unknown") for x in svc))
    return {"at": iso(now), "asof": asof, "hosts": hosts, "groups": groups, "alerts": parse_alerts(a)}


def first_line(text, limit=100):
    """A task result's first real line, without markdown marks: the summary the detail screen shows."""
    for line in str(text or "").splitlines():
        line = plain(re.sub(r"[*`#>]+", "", line)).strip(" -")
        if line:
            return line[:limit]
    return ""


ENGINES = {"claude_api": "Claude", "claude_code": "Claude Code", "codex": "Codex", "qwen": "Qwen", "nim": "NIM"}


def task_row(t):
    steps = [s for s in t.get("steps") or [] if s.get("label")]
    return {"goal": plain(t.get("goal"))[:WIDTH], "status": t.get("status"), "device": t.get("device") or None,
            "engine": ENGINES.get(t.get("backend"), t.get("backend")) if t.get("backend") else None,
            "started": iso(t.get("started")), "finished": iso(t.get("finished")),
            "step": plain(steps[-1]["label"])[:60] if steps else None, "steps": len(t.get("steps") or []),
            "note": plain(t.get("note"))[:WIDTH] or None}


async def build_tasks_detail(d, now=None):
    """GET /v1/tv?detail=tasks: the running, queued and waiting tasks (goal, device, engine, start, current step) and the
    last 10 finished with a one-line result. Approvals stay a count; no command, brief or full result is included."""
    now = now or time.time()
    rows, apps = await asyncio.gather(d["tasks"](40), d["approvals"]())
    active = [task_row(t) for t in rows if t.get("status") in ("running", "waiting_approval", "queued", "scheduled")]
    done = []
    for t in rows:
        if t.get("status") in ("done", "failed", "cancelled"):
            r = task_row(t)
            r["result"] = first_line(t.get("error") if t.get("status") != "done" and t.get("error") else t.get("result"))
            done.append(r)
    order = {"running": 0, "waiting_approval": 1, "queued": 2, "scheduled": 3}
    return {"at": iso(now), "active": sorted(active, key=lambda r: order[r["status"]])[:LINES],
            "recent": done[:10], "approvals": len(apps)}


# ---------- weather, holdings, 24 h host history (charts drawn by the TV from these numbers) ----------
SKY_WORD = {"맑아요": "맑음", "대체로 맑아요": "대체로 맑음", "구름이 조금 있어요": "구름 조금", "구름이 많아요": "구름 많음",
            "흐려요": "흐림", "안개가 껴요": "안개", "비가 와요": "비", "비나 눈이 와요": "비나 눈", "눈이 와요": "눈",
            "소나기가 와요": "소나기", "빗방울이 떨어져요": "빗방울", "빗방울이나 눈날림이 있어요": "빗방울·눈날림",
            "눈날림이 있어요": "눈날림"}


# icon keys the TV draws (assistant/roku/images/wx_*.png): KMA 강수형태 first, else 하늘상태; Open-Meteo by WMO code
KMA_ICON_PTY = {"1": "rain", "2": "sleet", "3": "snow", "4": "shower", "5": "rain", "6": "sleet", "7": "snow"}
KMA_ICON_SKY = {"1": "clear", "3": "cloudy", "4": "overcast"}


def kma_icon(pty, sky):
    return KMA_ICON_PTY.get(str(pty or "0")) or KMA_ICON_SKY.get(str(sky or ""))


def wmo_icon(code):
    if code is None:
        return None
    if code in (0, 1):
        return "clear"
    if code == 2:
        return "cloudy"
    if code in (3, 45, 48):
        return "overcast"
    if code in (56, 57, 66, 67):
        return "sleet"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if 80 <= code <= 82 or code >= 95:
        return "shower"
    return "rain" if 51 <= code <= 65 else None


def sky_word(sentence):
    return SKY_WORD.get(sentence or "", (sentence or "").removesuffix("요").strip() or None)


def weather_card(w, now):
    """The TV's weather card from the gateway's own weather reads (weather.py), KMA first as the voice answers do:
    {source, place, now, sky, high, low, pop, hourly: [{h: "04", t, pop}] for the next 12 hours (KMA only)}."""
    import weather as wx
    if w is None:
        return None
    out = {"source": w.get("source"), "place": w.get("place"), "now": None, "sky": None, "icon": None, "high": None,
           "low": None, "pop": None, "hourly": []}
    if w.get("source") == "기상청":
        today = datetime.fromtimestamp(now, KST).strftime("%Y%m%d")
        lo, hi, pop, sky = wx.day_summary(w.get("vilage") or [], today)
        obs = {i["category"]: i["obsrValue"] for i in w.get("ncst") or []}
        t = float(obs["T1H"]) if "T1H" in obs else None
        if t is not None:  # the morning's forecast high or low already passed: never "지금 10도, 최고 9도"
            hi, lo = (max(hi, t) if hi is not None else None), (min(lo, t) if lo is not None else None)
        pty = obs.get("PTY", "0")
        nxt = next((i["fcstValue"] for i in sorted(w.get("fcst") or [], key=lambda i: (i["fcstDate"], i["fcstTime"]))
                    if i["category"] == "SKY"), None)
        out.update(now=t, high=hi, low=lo, pop=pop, icon=kma_icon(pty, nxt),
                   sky=sky_word(wx.KMA_PTY.get(pty) if pty != "0" else wx.KMA_SKY.get(nxt or "")))
        start = datetime.fromtimestamp(now, KST).strftime("%Y%m%d%H00")
        hours = {}
        for i in w.get("vilage") or []:
            key = i["fcstDate"] + i["fcstTime"]
            if key >= start and i["category"] in ("TMP", "POP", "SKY", "PTY"):
                v = i["fcstValue"]
                hours.setdefault(key, {})[i["category"]] = float(v) if i["category"] in ("TMP", "POP") else v
        out["hourly"] = [{"h": k[8:10], "t": v.get("TMP"), "pop": int(v["POP"]) if "POP" in v else None,
                          "icon": kma_icon(v.get("PTY"), v.get("SKY"))}
                         for k, v in sorted(hours.items())[:12] if "TMP" in v]
        return out
    cur, day = w.get("current") or {}, w.get("daily") or {}
    first = lambda k: (day.get(k) or [None])[0]  # noqa: E731
    out.update(now=cur.get("temperature_2m"), high=first("temperature_2m_max"), low=first("temperature_2m_min"),
               pop=first("precipitation_probability_max"), sky=sky_word(wx.SKY.get(cur.get("weather_code"))),
               icon=wmo_icon(cur.get("weather_code")))
    return out


HISTORY_AGO = (21, 18, 15, 12, 9, 6, 3, 0)  # hours back: 8 points across the last day
HISTORY_EXPR = {
    "cpu": 'round(100 * (1 - avg by (node) (rate(node_cpu_seconds_total{{mode="idle"}}[30m]{off}))))',
    "mem": "round(100 * (1 - avg by (node) (node_memory_MemAvailable_bytes{off} / node_memory_MemTotal_bytes{off})))",
}
SERIES = re.compile(r'^\{ago="(\d+)",node="([^"]+)"\}\s+(-?[\d.]+)')


def history_query(metric):
    """One instant PromQL that returns every node's value 21, 18 … 3 hours ago and now, each tagged with `ago`."""
    parts = []
    for h in HISTORY_AGO:
        expr = HISTORY_EXPR[metric].format(off=f" offset {h}h" if h else "")
        parts.append(f'label_replace({expr}, "ago", "{h}", "", "")')
    return " or ".join(parts)


def parse_history(text):
    """infra_query lines `{ago="21",node="deneb"} 15` → {node: [8 values, oldest first, None where missing]}."""
    out = {}
    for line in (text or "").splitlines():
        m = SERIES.match(line.strip())
        if m and int(m.group(1)) in HISTORY_AGO:
            out.setdefault(m.group(2), [None] * len(HISTORY_AGO))[HISTORY_AGO.index(int(m.group(1)))] = round(float(m.group(3)))
    return out


class Cache:
    def __init__(self):
        self.at, self.value, self.lock = 0.0, None, asyncio.Lock()

    async def get(self, make, ttl=TTL_S):
        async with self.lock:
            if self.value is None or time.monotonic() - self.at >= ttl:
                self.value, self.at = await make(), time.monotonic()
            return self.value


# ---------- GET /v1/tv?detail=stock&symbol=…&range=…: one holding's chart page ----------
STOCK_RANGES = {"1d": ("1d", "5m", 30), "7d": ("7d", "30m", 300), "1mo": ("1mo", "1h", 300),
                "3mo": ("3mo", "1d", 600), "1y": ("1y", "1d", 600)}  # Yahoo range, interval, cache seconds
STOCK_SYMBOL = re.compile(r"^\^?[A-Za-z0-9][A-Za-z0-9.=\-]{0,11}$")
MAX_POINTS = 200


def downsample(points, n=MAX_POINTS):
    """At most n points, evenly spread, always keeping the first and the last."""
    if len(points) <= n:
        return points
    return [points[round(i * (len(points) - 1) / (n - 1))] for i in range(n)]


def market_state(meta, now):
    """open | pre | post | closed from the chart meta's trading periods (pre and post exist for US stocks only)."""
    cur = meta.get("currentTradingPeriod") or {}
    for state in ("regular", "pre", "post"):
        p = cur.get(state) or {}
        if p.get("start") is not None and p["start"] <= now < p["end"]:
            return "open" if state == "regular" else state
    return "closed"


def chart_points(res):
    ts = res.get("timestamp") or []
    close = (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    return [[t, round(c, 4)] for t, c in zip(ts, close) if c is not None]


async def build_stock(d, symbol, rng, now=None):
    """One holding's page: the quote (price, change against the previous close, day high/low, volume, market state) and
    `points` [[unix, close] …, ≤ 200] for the range. The day figures always come from the 1d chart, so the header agrees
    across ranges; `base` is what the line is colored against (the previous close on 1d, else the range's first close).
    A KRX code is tried as .KS then .KQ. LookupError when the provider knows no such symbol."""
    now = now or time.time()
    yrange, interval, _ = STOCK_RANGES[rng]
    cands = [f"{symbol}.KS", f"{symbol}.KQ"] if re.fullmatch(r"\d{6}", symbol) else [symbol]
    for sym in cands:
        try:
            res = (await d["chart"](sym, "1d", "5m"))["chart"]["result"][0]
            break
        except (KeyError, IndexError, TypeError, ValueError, LookupError):
            continue
    else:
        raise LookupError(symbol)
    meta = res["meta"]
    series = res if rng == "1d" else (await d["chart"](sym, yrange, interval))["chart"]["result"][0]
    pts = chart_points(series)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") if meta.get("chartPreviousClose") is not None else meta.get("previousClose")
    index = symbol.startswith("^")
    base = prev if rng == "1d" else (pts[0][1] if pts else prev)
    return {"at": iso(now), "symbol": symbol, "name": meta.get("longName") or meta.get("shortName") or symbol, "range": rng,
            "currency": "index" if index else meta.get("currency"), "index": index, "state": market_state(meta, now),
            "price": price, "prev": prev, "change": price - prev if price is not None and prev else None,
            "pct": round((price - prev) / prev * 100, 2) if price is not None and prev else None,
            "high": None if index else meta.get("regularMarketDayHigh"), "low": None if index else meta.get("regularMarketDayLow"),
            "volume": None if index else meta.get("regularMarketVolume"), "base": base, "points": downsample(pts)}


class KeyCache:
    """Cache per key with its own ttl, at most `cap` keys (the oldest goes first)."""

    def __init__(self, cap=64):
        self.items, self.cap, self.lock = {}, cap, asyncio.Lock()

    async def get(self, key, make, ttl):
        async with self.lock:
            hit = self.items.get(key)
            if hit is None or time.monotonic() - hit[0] >= ttl:
                if len(self.items) >= self.cap:
                    self.items.pop(min(self.items, key=lambda k: self.items[k][0]))
                hit = self.items[key] = (time.monotonic(), await make())
            return hit[1]
