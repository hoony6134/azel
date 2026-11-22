"""iPhone data (PHONE_DATA.md): the phone_* read-only actions and the synced daily summaries.

Live: the gateway asks the app over the voice link (`live(kind, args, timeout)` -> {"ok", "data"|"error"}), during a
call only. Sync: the app PUTs one compact summary per day (`clean_days`) into kv `phone-days`, the newest KEEP_DAYS
kept. Every Korean sentence is made here, for live and synced data alike; the app only sends numbers.
"""
import re
from datetime import date, datetime, timedelta

KV = "/v1/state/kv/phone-days"
KEEP_DAYS = 30
LIVE_S = 8
LOCATION_S = 12
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RANGES = ["today", "yesterday", "this_week", "last_week", "last_7_days", "this_month"]
CAL_RANGES = ["today", "tomorrow", "this_week", "next_week"]
METRICS = ["summary", "steps", "distance", "active_energy", "exercise", "heart_rate", "resting_heart_rate", "hrv",
           "mindful", "weight", "cycle"]
RANGE_PROP = {"type": "string", "maxLength": 12, "description": (
    "today, yesterday, this_week(월요일부터), last_week, last_7_days, this_month, 또는 YYYY-MM-DD 하루")}

TOOLS = (
    {"name": "phone_health",
     "description": ("사용자 아이폰의 건강 데이터(걸음 수, 거리, 활동 칼로리, 운동 시간, 심박수, 안정 시 심박수, HRV, 마음챙김, "
                     "몸무게, 생리 주기). '오늘 걸음 수', '심박수 어땠어?'는 이걸로. summary는 하루 요약."),
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["metric"], "properties": {
         "metric": {"type": "string", "enum": METRICS}, "range": RANGE_PROP}}},
    {"name": "phone_sleep",
     "description": ("아이폰·애플워치 수면 기록. '어제 몇 시간 잤어?', '어젯밤 잠'은 date를 비워(가장 최근 밤). "
                     "특정한 밤이면 date에 일어난 날(YYYY-MM-DD)."),
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {
         "date": {"type": "string", "pattern": DATE_RE.pattern, "description": "일어난 날"}}}},
    {"name": "phone_workouts",
     "description": "아이폰·애플워치 운동 기록(종류, 시간, 칼로리). '이번 주 운동 얼마나 했어?'는 이걸로.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {"range": RANGE_PROP}}},
    {"name": "phone_calendar",
     "description": "아이폰 캘린더 일정(아이폰에 있는 모든 계정). 구글 캘린더는 calendar_events. 통화 중에만 볼 수 있어.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {
         "range": {"type": "string", "maxLength": 12, "description": "today, tomorrow, this_week, next_week, 또는 YYYY-MM-DD"}}}},
    {"name": "phone_reminders",
     "description": "아이폰 미리 알림에서 아직 안 끝난 항목. list에 목록 이름을 주면 그 목록만. 통화 중에만.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {
         "list": {"type": "string", "maxLength": 60}}}},
    {"name": "phone_contacts",
     "description": "아이폰 연락처에서 이름으로 찾기(전화번호, 이메일, 생일, 회사). 통화 중에만.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["name"], "properties": {
         "name": {"type": "string", "minLength": 1, "maxLength": 40}}}},
    {"name": "phone_location",
     "description": "사용자가 '지금 어디야?'처럼 지금 위치를 물을 때만. 아이폰의 현재 위치(장소 이름)와 움직임. 통화 중에만.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {}}},
    {"name": "phone_now_playing",
     "description": "아이폰에서 지금 듣는 음악과 최근에 들은 곡. 통화 중에만.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {}}},
)
NAMES = {t["name"] for t in TOOLS}
KIND = {"phone_health": "health", "phone_sleep": "sleep", "phone_workouts": "workouts", "phone_calendar": "calendar",
        "phone_reminders": "reminders", "phone_contacts": "contacts", "phone_location": "location",
        "phone_now_playing": "now_playing"}
SYNCED = {"health", "sleep", "workouts"}
CATEGORY = {"health": "건강", "sleep": "수면", "workouts": "운동", "calendar": "캘린더", "reminders": "미리 알림",
            "contacts": "연락처", "location": "위치", "now_playing": "음악", "cycle": "생리 주기"}
OFFLINE = "지금은 아이폰에 연결돼 있지 않아서 못 봐요. 앱으로 통화 중일 때 물어봐 주세요."


class Unavailable(Exception):
    pass


# ---------- the synced summaries: what the app may upload ----------
NUM = {"steps": (0, 200000), "distance_m": (0, 500000), "active_kcal": (0, 20000), "exercise_min": (0, 1440),
       "mindful_min": (0, 1440), "hr_avg": (20, 250), "hr_min": (20, 250), "hr_max": (20, 250),
       "resting_hr": (20, 250), "hrv_ms": (1, 500), "weight_kg": (20, 400)}
SLEEP_NUM = {k: (0, 1440) for k in ("asleep_min", "in_bed_min", "core_min", "deep_min", "rem_min", "awake_min")}
WORKOUT_NUM = {"minutes": (0, 1440), "kcal": (0, 20000), "distance_m": (0, 500000)}
CYCLE = {"none", "light", "medium", "heavy", "unspecified"}


def _num(v, lo, hi, where):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ValueError(f"{where}: a number from {lo} to {hi}")
    return v


def _time(v, where):
    try:
        t = datetime.fromisoformat(str(v))
    except ValueError:
        raise ValueError(f"{where}: ISO 8601 time") from None
    if t.tzinfo is None:
        raise ValueError(f"{where}: ISO 8601 time with an offset")
    return t.isoformat()


def clean_day(d, today):
    """One uploaded day, checked field by field (unknown keys refused), or ValueError."""
    if not isinstance(d, dict) or not DATE_RE.fullmatch(str(d.get("date", ""))):
        raise ValueError("days[]: an object with date YYYY-MM-DD")
    day = date.fromisoformat(d["date"])
    if not today - timedelta(days=KEEP_DAYS) <= day <= today + timedelta(days=1):
        raise ValueError(f"days[].date: within the last {KEEP_DAYS} days")
    out = {"date": d["date"]}
    for k, v in d.items():
        w = f"days[{d['date']}].{k}"
        if k == "date":
            continue
        if k in NUM:
            out[k] = _num(v, *NUM[k], w)
        elif k == "cycle":
            if v not in CYCLE:
                raise ValueError(f"{w}: one of {sorted(CYCLE)}")
            out[k] = v
        elif k == "sleep":
            if not isinstance(v, dict) or set(v) - {"start", "end", *SLEEP_NUM}:
                raise ValueError(f"{w}: start, end and minutes only")
            out[k] = {sk: (_time(sv, f"{w}.{sk}") if sk in ("start", "end") else _num(sv, *SLEEP_NUM[sk], f"{w}.{sk}"))
                      for sk, sv in v.items()}
        elif k == "workouts":
            if not isinstance(v, list) or len(v) > 20:
                raise ValueError(f"{w}: a list of up to 20")
            out[k] = []
            for x in v:
                if not isinstance(x, dict) or set(x) - {"type", "start", *WORKOUT_NUM} \
                        or not re.fullmatch(r"[a-z_]{1,30}", str(x.get("type", ""))):
                    raise ValueError(f"{w}[]: type (a-z_), start, minutes, kcal, distance_m")
                out[k].append({wk: (wv if wk == "type" else _time(wv, f"{w}.start") if wk == "start"
                                    else _num(wv, *WORKOUT_NUM[wk], f"{w}.{wk}")) for wk, wv in x.items()})
        else:
            raise ValueError(f"{w}: unknown field")
    return out


def merge(stored, days, today, now_ts):
    """kv value {"days": {date: summary}, "updated": epoch} with `days` replacing their dates; the newest KEEP_DAYS."""
    keep = {**(stored or {}).get("days", {}), **{d["date"]: d for d in days}}
    cut = (today - timedelta(days=KEEP_DAYS)).isoformat()
    return {"days": {k: keep[k] for k in sorted(keep) if k >= cut}, "updated": now_ts}


FIELDS = {"health": set(NUM), "sleep": {"sleep"}, "workouts": {"workouts"}, "cycle": {"cycle"}}


def strip(stored, kind):
    """The kv value without one category's fields (that toggle was turned off in the app)."""
    drop = FIELDS[kind]
    days = {k: {f: v for f, v in d.items() if f not in drop} for k, d in (stored or {}).get("days", {}).items()}
    return {"days": {k: d for k, d in days.items() if len(d) > 1}, "updated": (stored or {}).get("updated")}


# ---------- dates ----------
def span(rng, today, cal=False):
    """(first, last) dates of a range word or one YYYY-MM-DD; ValueError otherwise."""
    rng = (rng or "today").strip()
    mon = today - timedelta(days=today.weekday())
    table = {"today": (today, today), "yesterday": (today - timedelta(days=1),) * 2,
             "this_week": (today if cal else mon, mon + timedelta(days=6) if cal else today),
             "last_week": (mon - timedelta(days=7), mon - timedelta(days=1)),
             "last_7_days": (today - timedelta(days=6), today), "this_month": (today.replace(day=1), today),
             "tomorrow": (today + timedelta(days=1),) * 2,
             "next_week": (mon + timedelta(days=7), mon + timedelta(days=13))}
    if rng in (CAL_RANGES if cal else RANGES):
        return table[rng]
    if DATE_RE.fullmatch(rng):
        d = date.fromisoformat(rng)
        return d, d
    raise ValueError("range: " + ", ".join(CAL_RANGES if cal else RANGES) + " 또는 YYYY-MM-DD")


def day_word(d, today):
    return {0: "오늘", 1: "어제", -1: "내일"}.get((today - d).days, f"{d.month}월 {d.day}일")


def span_word(a, b, rng, today):
    words = {"this_week": "이번 주", "last_week": "지난주", "last_7_days": "최근 7일", "this_month": "이번 달",
             "next_week": "다음 주"}
    if a == b:
        return day_word(a, today)
    end = f"{b.day}일" if a.month == b.month else f"{b.month}월 {b.day}일"
    return f"{words.get(rng, '그 기간')}({a.month}월 {a.day}일–{end})"


def ye(s):
    """s + 이에요/예요 by the last syllable's final consonant."""
    c = s[-1]
    return s + ("이에요" if "가" <= c <= "힣" and (ord(c) - 0xAC00) % 28 else "예요")


def dur(minutes):
    h, m = divmod(round(minutes), 60)
    return f"{h}시간 {m}분" if h and m else f"{h}시간" if h else f"{m}분"


def clock(iso):
    t = datetime.fromisoformat(iso)
    h = t.hour % 12 or 12
    return f"{'오전' if t.hour < 12 else '오후'} {h}시" + (f" {t.minute}분" if t.minute else "")


# ---------- formatting ----------
def health_text(metric, days, a, b, rng, today):
    when = span_word(a, b, rng, today)
    n = len(days)

    def total(key):
        vals = [d[key] for d in days if key in d]
        return (sum(vals), len(vals)) if vals else (None, 0)

    def avg(key):
        vals = [d[key] for d in days if key in d]
        return sum(vals) / len(vals) if vals else None

    def summed(key, unit_fmt, noun):
        s, c = total(key)
        if s is None:
            return None
        if a == b:
            return f"{when} {noun}{'은' if noun[-1] in '음율량간' else '는'} {ye(unit_fmt(s))}."
        return f"{when} {noun} 합계는 {unit_fmt(s)}, 하루 평균 {ye(unit_fmt(s / c))}."

    steps = lambda v: f"{round(v):,}걸음"  # noqa: E731
    km = lambda v: f"{v / 1000:.1f}킬로미터"  # noqa: E731
    kcal = lambda v: f"{round(v):,}킬로칼로리"  # noqa: E731
    if metric == "steps":
        return summed("steps", steps, "걸음 수")
    if metric == "distance":
        return summed("distance_m", km, "걸은 거리")
    if metric == "active_energy":
        return summed("active_kcal", kcal, "활동 칼로리")
    if metric == "exercise":
        return summed("exercise_min", dur, "운동 시간")
    if metric == "mindful":
        return summed("mindful_min", dur, "마음챙김 시간")
    if metric == "heart_rate":
        if avg("hr_avg") is None:
            return None
        lo = min(d["hr_min"] for d in days if "hr_min" in d) if any("hr_min" in d for d in days) else None
        hi = max(d["hr_max"] for d in days if "hr_max" in d) if any("hr_max" in d for d in days) else None
        extra = (f", 가장 낮을 때 {round(lo)}" if lo else "") + (f", 가장 높을 때 {round(hi)}" if hi else "")
        return f"{when} 심박수는 평균 {round(avg('hr_avg'))}{extra}이에요(분당 횟수)."
    if metric == "resting_heart_rate":
        v = avg("resting_hr")
        return None if v is None else f"{when} 안정 시 심박수는 {'평균 ' if n > 1 else ''}분당 {round(v)}회예요."
    if metric == "hrv":
        v = avg("hrv_ms")
        return None if v is None else f"{when} 심박 변이도는 {'평균 ' if n > 1 else ''}{round(v)}밀리초예요."
    if metric == "weight":
        last = next((d for d in reversed(days) if "weight_kg" in d), None)
        return None if not last else \
            f"가장 최근 몸무게는 {last['weight_kg']:.1f}킬로그램이에요({day_word(date.fromisoformat(last['date']), today)})."
    if metric == "cycle":
        flow = [d for d in days if d.get("cycle") not in (None, "none")]
        if not any("cycle" in d for d in days):
            return None
        if not flow:
            return f"{when}에는 생리 기록이 없어요."
        return f"{when} 생리 기록이 있는 날은 " + ", ".join(day_word(date.fromisoformat(d["date"]), today)
                                                     for d in flow) + "이에요."
    parts = [p for p in (summed("steps", steps, "걸음 수"), summed("active_kcal", kcal, "활동 칼로리"),
                         summed("exercise_min", dur, "운동 시간")) if p]
    return " ".join(parts[:2]) if parts else None


def sleep_text(day, today):
    s = day.get("sleep") or {}
    if not s.get("asleep_min") and not s.get("in_bed_min"):
        return None
    d = date.fromisoformat(day["date"])
    night = "어젯밤" if d == today else "그저께 밤" if (today - d).days == 1 else f"{d.month}월 {d.day}일 아침까지"
    main = f"{night} {dur(s.get('asleep_min') or s['in_bed_min'])} 주무셨어요."
    if s.get("start") and s.get("end"):
        main += f" {clock(s['start'])}에 잠들어 {clock(s['end'])}에 일어나셨어요."
    stages = [f"{n} {dur(s[k])}" for k, n in (("deep_min", "깊은 잠"), ("rem_min", "렘수면")) if s.get(k)]
    return main + (f" {', '.join(stages)}이에요." if stages else "")


WORKOUT = {"running": "달리기", "walking": "걷기", "cycling": "자전거", "swimming": "수영", "hiking": "등산",
           "strength": "근력 운동", "hiit": "고강도 인터벌", "yoga": "요가", "dance": "댄스", "elliptical": "일립티컬",
           "rowing": "로잉", "stairs": "계단 오르기", "core": "코어 운동", "pilates": "필라테스", "soccer": "축구",
           "basketball": "농구", "tennis": "테니스", "badminton": "배드민턴", "other": "운동"}


def workouts_text(days, a, b, rng, today):
    ws = [w for d in days for w in d.get("workouts", [])]
    when = span_word(a, b, rng, today)
    if not ws:
        return f"{when}에는 운동 기록이 없어요."
    total = sum(w.get("minutes", 0) for w in ws)
    kcal = sum(w.get("kcal", 0) for w in ws)
    items = ", ".join(f"{WORKOUT.get(w['type'], '운동')} {dur(w.get('minutes', 0))}" for w in ws[:4])
    more = f" 외 {len(ws) - 4}번" if len(ws) > 4 else ""
    return ye(f"{when} 운동은 {len(ws)}번, 모두 {dur(total)}" + (f", {round(kcal):,}킬로칼로리" if kcal else "")) + \
        f". {items}{more}."


def calendar_text(events, a, b, rng, today):
    when = span_word(a, b, rng, today)
    if not events:
        return f"아이폰 캘린더에 {when} 일정은 없어요."

    def one(e):
        t = "하루 종일" if e.get("all_day") else clock(e["start"])
        day = "" if a == b else day_word(datetime.fromisoformat(e["start"]).date(), today) + " "
        return f"{day}{t} {e.get('title') or '제목 없음'}"
    more = f" 외 {len(events) - 5}개" if len(events) > 5 else ""
    return f"아이폰 캘린더에 {when} 일정이 {len(events)}개 있어요. " + ", ".join(one(e) for e in events[:5]) + more + "."


def reminders_text(items, lst, today):
    where = f"'{lst}' 목록에" if lst else "미리 알림에"
    if not items:
        return f"{where} 남은 항목이 없어요."

    def one(r):
        due = r.get("due")
        return r.get("title") or "제목 없음" if not due else \
            f"{r.get('title') or '제목 없음'}({day_word(datetime.fromisoformat(due).date(), today)}까지)"
    more = f" 외 {len(items) - 5}개" if len(items) > 5 else ""
    return f"{where} {len(items)}개가 있어요. " + ", ".join(one(r) for r in items[:5]) + more + "."


def contacts_text(matches, name):
    if not matches:
        return f"연락처에서 '{name}'을(를) 찾지 못했어요."
    if len(matches) > 1:
        return f"'{name}'(으)로 {len(matches)}명이 있어요: " + ", ".join(m.get("name", "") for m in matches[:5]) + \
            ". 누구인지 알려 주세요."
    m = matches[0]
    bits = [f"{p.get('label') or '전화'} {p['number']}" for p in m.get("phones", [])[:2]]
    bits += [f"이메일 {e}" for e in m.get("emails", [])[:1]]
    if m.get("organization"):
        bits.append(m["organization"])
    if m.get("birthday"):
        mo, dd = m["birthday"][-5:].split("-")
        bits.append(f"생일 {int(mo)}월 {int(dd)}일")
    return f"{m.get('name', name)}: " + (", ".join(bits) if bits else "저장된 정보가 이름뿐이에요") + "."


MOTION = {"walking": "걷는 중", "running": "뛰는 중", "automotive": "차로 이동 중", "cycling": "자전거 타는 중",
          "stationary": "멈춰 계신 상태"}


def location_text(d):
    place = d.get("name") or d.get("place")
    if not place:
        return None
    s = f"지금 {place} 근처예요." if not d.get("name") or not d.get("place") else f"지금 {d['name']}, {d['place']} 근처예요."
    if d.get("activity") in MOTION:
        s += f" {MOTION[d['activity']]}이에요."
    return s


def music_text(d):
    recent = d.get("recent") or []
    if d.get("title") and d.get("playing"):
        return f"지금 {d.get('artist') + '의 ' if d.get('artist') else ''}'{d['title']}'을(를) 듣고 계세요."
    if d.get("title"):
        return f"지금은 멈춰 있고, 마지막 곡은 {d.get('artist') + '의 ' if d.get('artist') else ''}'{d['title']}'이에요."
    if recent:
        return "지금 재생 중인 음악은 없어요. 최근에 들은 곡은 " + \
            ", ".join(f"'{r['title']}'" for r in recent[:3] if r.get("title")) + "이에요."
    return "지금 재생 중인 음악이 없어요."


# ---------- the actions ----------
async def run(name, args, state, live, now):
    """(spoken, data) or raises Unavailable(spoken) / ValueError(invalid). `now`: an aware KST datetime."""
    kind, today = KIND[name], now.date()
    rng = args.get("range") or ("this_week" if kind == "workouts" else "today")
    if kind in ("health", "workouts"):
        a, b = span(rng, today)
    elif kind == "sleep":
        a = b = date.fromisoformat(args["date"]) if args.get("date") else today
        if not args.get("date"):
            a = today - timedelta(days=1)  # the latest night: last night, else the one before
    elif kind == "calendar":
        a, b = span(rng, today, cal=True)
    q = {"from": a.isoformat(), "to": b.isoformat()} if kind in ("health", "sleep", "workouts", "calendar") else {}
    if kind == "health":
        q["metrics"] = [args["metric"]]
    if kind == "reminders" and args.get("list"):
        q["list"] = str(args["list"])[:60]
    if kind == "contacts":
        q["name"] = str(args["name"]).strip()[:40]
        if not q["name"]:
            raise ValueError("name: 찾을 이름을 알려 주세요.")
    r = await live(kind, q, LOCATION_S if kind == "location" else LIVE_S) if live else {"ok": False, "error": "offline"}
    source = "live"
    if r.get("ok"):
        data = r.get("data") or {}
    elif r.get("error") in ("off", "denied"):
        what = CATEGORY["cycle" if args.get("metric") == "cycle" else kind]
        raise Unavailable(f"아이폰 데이터에서 {what}을(를) 켜 주시면 알 수 있어요." if r["error"] == "off"
                          else f"{what} 접근이 허용되지 않았어요. 설정 앱에서 Azel의 {what} 권한을 허용해 주세요.")
    elif kind in SYNCED:
        stored = await state("GET", KV) or {}
        data, source = {"days": [d for k, d in sorted(stored.get("days", {}).items())
                                 if a.isoformat() <= k <= b.isoformat()]}, "sync"
        if not stored.get("days"):
            raise Unavailable(f"아직 아이폰 {CATEGORY[kind]} 데이터가 없어요. 앱 설정의 아이폰 데이터에서 켜 주세요.")
    else:
        raise Unavailable(OFFLINE)
    days = data.get("days") or []
    if kind == "health":
        text = health_text(args["metric"], days, a, b, rng, today)
        if not text:
            text = f"{span_word(a, b, rng, today)}에는 그 기록이 없어요."
    elif kind == "sleep":
        nights = [d for d in days if (d.get("sleep") or {}).get("asleep_min") or (d.get("sleep") or {}).get("in_bed_min")]
        text = sleep_text(nights[-1], today) if nights else \
            f"{'최근' if not args.get('date') else day_word(a, today)} 수면 기록이 없어요."
    elif kind == "workouts":
        text = workouts_text(days, a, b, rng, today)
    elif kind == "calendar":
        text = calendar_text(data.get("events") or [], a, b, rng, today)
    elif kind == "reminders":
        text = reminders_text(data.get("items") or [], args.get("list"), today)
    elif kind == "contacts":
        text = contacts_text(data.get("matches") or [], q["name"])
    elif kind == "location":
        text = location_text(data) or "지금 위치를 알 수 없어요."
    else:
        text = music_text(data)
    if source == "sync" and b >= today and (up := stored.get("updated")):
        t = datetime.fromtimestamp(up, tz=now.tzinfo)
        text += f" (아이폰에서 {clock(t.isoformat())}에 받은 기록 기준)"
    return text, {"source": source, **data}
