"""Weather from Open-Meteo: free, no key, so every path may use it, local mode included.

place(): the geocoder knows "광주광역시" but sends a bare "광주" to a village in 충남 and finds no "서울", so a Korean
name is also tried with 광역시/특별시/시 and the most populous match wins.
forecast(): now (current + today), today, tomorrow or the day after, as one or two spoken sentences.
"""
import asyncio
import re

import httpx

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_S = 6
DAYS = {"now": 0, "today": 0, "tomorrow": 1, "day_after": 2}
DAY_WORD = {0: "오늘", 1: "내일", 2: "모레"}
# WMO weather codes, as spoken
SKY = {0: "맑아요", 1: "대체로 맑아요", 2: "구름이 조금 있어요", 3: "흐려요", 45: "안개가 껴요", 48: "안개가 껴요",
       51: "이슬비가 와요", 53: "이슬비가 와요", 55: "이슬비가 와요", 56: "어는 비가 와요", 57: "어는 비가 와요",
       61: "비가 조금 와요", 63: "비가 와요", 65: "비가 많이 와요", 66: "어는 비가 와요", 67: "어는 비가 와요",
       71: "눈이 조금 와요", 73: "눈이 와요", 75: "눈이 많이 와요", 77: "싸락눈이 와요", 80: "소나기가 와요",
       81: "소나기가 와요", 82: "소나기가 세차게 와요", 85: "눈이 와요", 86: "눈이 많이 와요", 95: "뇌우가 있어요",
       96: "우박을 동반한 뇌우가 있어요", 99: "우박을 동반한 뇌우가 있어요"}


def candidates(name):
    name = re.sub(r"\s+", "", str(name or ""))[:40]
    out = [name]
    if re.fullmatch(r"[가-힣]{1,6}", name) and not re.search(r"(시|도|군|구|읍|면|동)$", name):
        out += [name + "광역시", name + "특별시", name + "시"]
    return out


async def place(name, client):
    """{"name", "lat", "lon"} for a place name, or None."""
    async def one(q):
        try:
            r = await client.get(GEO_URL, params={"name": q, "count": 10, "language": "ko"})
            return r.json().get("results") or []
        except (httpx.HTTPError, ValueError):
            return []
    hits = [h for rows in await asyncio.gather(*(one(q) for q in candidates(name))) for h in rows]
    if not hits:
        return None
    best = max(hits, key=lambda h: (h.get("population") or 0, h.get("country_code") == "KR"))
    return {"name": best["name"], "lat": best["latitude"], "lon": best["longitude"]}


def deg(x):
    return f"영하 {abs(round(x))}도" if round(x) < 0 else f"{round(x)}도"


def topic(word):
    """'광주는', '서울은': the topic particle by the last syllable's final consonant."""
    last = word[-1] if word else ""
    return word + ("은" if "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 else "는")


def spoken(where, when, d):
    """One or two sentences from an Open-Meteo forecast response."""
    day = DAYS.get(when, 0)
    daily = d["daily"]
    hi, lo = daily["temperature_2m_max"][day], daily["temperature_2m_min"][day]
    rain = (daily.get("precipitation_probability_max") or [None] * 3)[day]
    temps = f"최고 {deg(hi)}, 최저 {deg(lo)}" + (f", 비 올 확률은 {rain}%예요." if rain is not None else "예요.")
    if when == "now":
        cur = d["current"]
        feel = f"(체감 {deg(cur['apparent_temperature'])})" if abs(cur["apparent_temperature"] - cur["temperature_2m"]) >= 3 else ""
        sky = SKY.get(cur["weather_code"])
        return f"지금 {topic(where)} {deg(cur['temperature_2m'])}{feel}" + (f"이고 {sky}" if sky else "예요") + f". 오늘 {temps}"
    sky = SKY.get(daily["weather_code"][day])
    return f"{DAY_WORD[day]} {topic(where)} " + (f"{sky}. " if sky else "") + temps


async def forecast(place_name, when, home, client=None):
    """{"spoken", "place", "data"} for place_name (or home: {"name", "lat", "lon"}). Raises LookupError for an
    unknown place, httpx errors when Open-Meteo is down."""
    when = when if when in DAYS else "now"
    c = client or httpx.AsyncClient(timeout=TIMEOUT_S)
    try:
        p = await place(place_name, c) if place_name else home
        if not p:
            raise LookupError(f"'{place_name}'이라는 곳을 찾지 못했어요.")
        r = await c.get(FORECAST_URL, params={
            "latitude": p["lat"], "longitude": p["lon"], "timezone": "Asia/Seoul", "forecast_days": 3,
            "current": "temperature_2m,apparent_temperature,weather_code,precipitation",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"})
        r.raise_for_status()
        d = r.json()
    finally:
        if client is None:
            await c.aclose()
    where = re.sub(r"(광역시|특별시|특별자치시)$", "", p["name"]) or p["name"]
    return {"spoken": spoken(where, when, d), "place": p, "data": {"current": d.get("current"), "daily": d.get("daily")}}
