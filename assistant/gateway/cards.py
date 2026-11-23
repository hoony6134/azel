"""Visual cards for the voice call (side-session-notes cards-contract.md): built from a tool's own data, never from model
text. `build(name, r)` returns {"kind", "title", ...} or None; a field without data is left out, never guessed."""
import json
import re
import time
from datetime import date as Date, datetime, timedelta, timezone

import tv

KST = timezone(timedelta(hours=9))
MAX_BYTES = 32_000  # the voice server drops a card over 32 KB
WEEKDAY = "월화수목금토일"


def clean(d):
    return {k: v for k, v in d.items() if v is not None and v != "" and v != []}


def kst_iso(day, hhmm):
    return f"{day}T{hhmm}:00+09:00"


def agenda_items(rows):
    """agenda.merge rows → card items (JSON-ready strings). An all-day row has neither start nor end time."""
    out = []
    for r in rows:
        day, s, e = r["day"], r.get("start") or "", r.get("end") or ""
        cal = r.get("src") == "calendar"
        item = {"title": tv.plain(r["title"] if cal else r["text"]), "source": "google" if cal else "gistra",
                "calendar": r.get("cal") if cal else None, "location": r.get("where") if cal else None}
        if not s and not e:
            item.update(all_day=True, start=day.isoformat())
        else:
            s = s or "00:00"  # an overnight event that began before the range: from the day's start
            item.update(all_day=False, start=kst_iso(day, s),
                        end=kst_iso(day + timedelta(days=1) if e and e < s else day, e) if e else None)
        out.append(clean(item))
    return out


def day_label(d):
    return f"{d.month}월 {d.day}일 ({WEEKDAY[d.weekday()]})"


def range_label(f, t):
    f, t = Date.fromisoformat(f), Date.fromisoformat(t)
    return day_label(f) if f == t else f"{day_label(f)} ~ {day_label(t)}"


def agenda_card(data):
    if "확인할 수 없어요" in data.get("text", ""):  # both sources failed: an empty card would say "no plans"
        return None
    return {"kind": "agenda", "title": "일정", "range_label": range_label(data["from"], data["to"]), "items": data["items"]}


def stock_card(q, detail=None):
    """quote.quote data, plus the TV's 1d stock detail when it came (points, day high/low, volume, market state)."""
    if q.get("rate") is not None or not q.get("symbol"):  # an exchange rate, or nothing to chart
        return None
    card = {"kind": "stock", "title": q.get("name") or q["symbol"], "symbol": q["symbol"], "name": q.get("name"),
            "price": q.get("price"), "change": q.get("change"), "change_pct": q.get("pct"),
            "currency": q.get("currency"),
            "prev_close": q["price"] - q["change"] if q.get("price") is not None and q.get("change") is not None else None}
    if detail:
        extra = clean({"price": detail.get("price"), "change": detail.get("change"),
                       "change_pct": detail.get("pct"), "prev_close": detail.get("prev"),
                       "market_state": detail.get("state"), "high": detail.get("high"), "low": detail.get("low"),
                       "volume": detail.get("volume"), "is_index": detail.get("index"),
                       "points": detail.get("points"), "range": "1d" if detail.get("points") else None})
        card.update({k: v for k, v in extra.items() if card.get(k) is None})  # the spoken price wins (Naver is live)
        if detail.get("index"):
            card["currency"] = None  # points, not a currency
    return clean(card)


def portfolio_card(data):
    items = [clean({"symbol": h.get("ticker"), "name": h.get("name"), "price": h.get("price"),
                    "change_pct": h.get("pct"), "spark": h.get("spark")}) for h in data.get("holdings") or []]
    return {"kind": "portfolio", "title": "내 종목", "items": items} if items else None


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def weather_card(r, now=None):
    """weather.forecast's result: KMA (ncst now, fcst next hours, vilage 3 days) or Open-Meteo (current, daily)."""
    import weather as wx
    now = now or time.time()
    data, place = r.get("data") or {}, (r.get("place") or {}).get("name")
    card = {"kind": "weather", "title": f"{place} 날씨" if place else "날씨", "place": place}
    if r.get("source") == "기상청":
        vil = data.get("vilage") or []
        hours = {}
        start = datetime.fromtimestamp(now, KST).strftime("%Y%m%d%H00")
        for i in vil:
            k = i["fcstDate"] + i["fcstTime"]
            if k >= start:
                hours.setdefault(k, {})[i["category"]] = i["fcstValue"]
        card["hours"] = [clean({"t": f"{k[:4]}-{k[4:6]}-{k[6:8]}T{k[8:10]}:{k[10:12]}:00+09:00", "temp": num(v["TMP"]),
                                "icon": tv.kma_icon(v.get("PTY"), v.get("SKY")),
                                "pop": int(num(v["POP"])) if num(v.get("POP")) is not None else None})
                         for k, v in sorted(hours.items())[:12] if "TMP" in v]
        days = []
        for d in sorted({i["fcstDate"] for i in vil})[:5]:
            lo, hi, pop, _ = wx.day_summary(vil, d)
            noon = {i["category"]: i["fcstValue"] for i in vil if i["fcstDate"] == d and i["fcstTime"] == "1200"}
            days.append(clean({"d": f"{d[:4]}-{d[4:6]}-{d[6:]}", "min": lo, "max": hi, "pop": pop,
                               "icon": tv.kma_icon(noon.get("PTY"), noon.get("SKY"))}))
        card["days"] = days
        obs = {i["category"]: i["obsrValue"] for i in data.get("ncst") or []}
        if obs:
            pty = obs.get("PTY", "0")
            sky = next((i["fcstValue"] for i in sorted(data.get("fcst") or [], key=lambda i: (i["fcstDate"], i["fcstTime"]))
                        if i["category"] == "SKY"), None)
            card["now"] = clean({"temp": num(obs.get("T1H")), "humidity": num(obs.get("REH")), "wind": num(obs.get("WSD")),
                                 "icon": tv.kma_icon(pty, sky),
                                 "sky": tv.sky_word(wx.KMA_PTY.get(pty) if pty != "0" else wx.KMA_SKY.get(sky or "")),
                                 "pop": card["hours"][0].get("pop") if card["hours"] else None})
    else:
        cur, daily = data.get("current") or {}, data.get("daily") or {}
        if cur:
            code = cur.get("weather_code")
            card["now"] = clean({"temp": cur.get("temperature_2m"), "humidity": cur.get("relative_humidity_2m"),
                                 "wind": cur.get("wind_speed_10m"), "icon": tv.wmo_icon(code),
                                 "sky": tv.sky_word(wx.SKY.get(code))})

        def col(k, i):
            v = daily.get(k) or []
            return v[i] if i < len(v) else None
        card["days"] = [clean({"d": d, "min": col("temperature_2m_min", i), "max": col("temperature_2m_max", i),
                               "pop": col("precipitation_probability_max", i),
                               "icon": tv.wmo_icon(col("weather_code", i))})
                        for i, d in enumerate((daily.get("time") or [])[:5])]
    card = clean(card)
    return card if any(k in card for k in ("now", "hours", "days")) else None


GISTRA_TODO_SECTION = "7일 안 마감"


def gistra_todos(text, only=None, now=None):
    """`- ⬜ 11월 23일 (월) 23:59 · 과목 — 제목 [과제]` lines (all sections, or the one named `only`) → todo items."""
    now = now or time.time()
    if not re.search(r"^#", str(text or ""), re.M):
        return None  # not a Gistra list (a Cygnus error line): no card rather than an empty one
    out, sec = [], None
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            sec = re.sub(r"\s*\(\d+\)\s*$", "", line.lstrip("# ").strip())
        elif line.startswith("- ") and (only is None or sec == only):
            it = tv.parse_item(line, now)
            course = it["kind"] if it["kind"] not in (it["tag"], "할 일", "할일") else None
            out.append(clean({"title": it["title"], "due": it["due"] or it.get("date"), "course": course,
                              "source": "gistra", "done": "✅" in line or "☑" in line}))
    return out


def planit_todos(text):
    try:
        events = json.loads(text)["events"]
    except (ValueError, KeyError, TypeError):
        return None  # the markdown answer: not parsed, no card
    return [clean({"title": e.get("title"), "due": e.get("start"), "source": "planit", "done": bool(e.get("done"))})
            for e in events if isinstance(e, dict) and e.get("title")]


def infra_card(name, text, now=None):
    now = now or time.time()
    if name == "infra_health":
        hosts = [clean({"name": h["name"], "up": h["up"], "cpu_pct": h["cpu"], "mem_pct": h["mem"], "disk_pct": h["disk"],
                        "temp_c": h["temp"]}) for h in tv.parse_hosts(text, now)[0]]
        return {"kind": "infra", "title": "서버 상태", "hosts": hosts} if hosts else None
    if name == "infra_status":
        if not re.search(r"^\s*(?:[✓✗⚠!#]|정상|문제|주의|꺼짐)", text or "", re.M):
            return None  # not the monitor list (an error line)
        return {"kind": "infra", "title": "서비스 상태", "down": [d["service"] for d in tv.parse_down(text)]}
    alerts = tv.parse_alerts(text)
    if not alerts and not re.search(r"no firing alerts|경보 없음", text or "", re.I):
        return None
    hosts = {}
    for a in alerts:  # grouped by the header they came under; the alert text names the host itself
        hosts.setdefault(a["group"] or "경보", []).append(a["text"])
    return {"kind": "infra", "title": "경보", "hosts": [{"name": k, "alerts": v} for k, v in hosts.items()]}


TODO_TITLES = {"gistra_assignments": "과제·할 일", "gistra_today": "오늘 마감", "planit_get_day": "Planit"}


def build(name, r, stock_detail=None, now=None):
    """The card for one finished tool run (bridge.run's result), or None."""
    if not r.get("ok"):
        return None
    data = r.get("data")
    try:
        if name == "agenda" and isinstance(data, dict) and "items" in data:
            card = agenda_card(data)
        elif name == "stock_quote" and isinstance(data, dict):
            card = stock_card(data, stock_detail)
        elif name == "portfolio" and isinstance(data, dict):
            card = portfolio_card(data)
        elif name == "weather" and isinstance(data, dict):
            card = weather_card(data, now)
        elif name in TODO_TITLES:
            items = planit_todos(r["spoken"]) if name == "planit_get_day" else \
                gistra_todos(r["spoken"], GISTRA_TODO_SECTION if name == "gistra_today" else None, now)
            card = {"kind": "todo", "title": TODO_TITLES[name], "items": items} if items is not None else None
        elif name in ("infra_health", "infra_status", "infra_alerts"):
            card = infra_card(name, r["spoken"], now)
        else:
            return None
    except Exception as e:  # noqa: BLE001  a card is extra: a format it can't read means no card, never a failed tool
        print("card failed:", name, type(e).__name__, flush=True)
        return None
    return fit(card) if card else None


def fit(card):
    """Under MAX_BYTES of JSON: the longest list loses its tail until it fits."""
    while len(json.dumps(card, ensure_ascii=False).encode()) > MAX_BYTES:
        lists = [k for k, v in card.items() if isinstance(v, list) and v]
        if not lists:
            return None
        k = max(lists, key=lambda k: len(json.dumps(card[k], ensure_ascii=False)))
        card[k] = card[k][:len(card[k]) // 2]
    return card
