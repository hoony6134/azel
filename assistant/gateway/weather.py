"""Weather: 기상청 (KMA, data.go.kr) for places in Korea when the gateway has the key, Open-Meteo abroad, without a key
or when KMA fails. Both are free, so every path may use it, local mode included.

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


# ---------- 기상청 (KMA) ----------
KMA_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/"
KMA_SKY = {"1": "맑아요", "3": "구름이 많아요", "4": "흐려요"}
KMA_PTY = {"1": "비가 와요", "2": "비나 눈이 와요", "3": "눈이 와요", "4": "소나기가 와요", "5": "빗방울이 떨어져요",
           "6": "빗방울이나 눈날림이 있어요", "7": "눈날림이 있어요"}
VILAGE_BASES = (2, 5, 8, 11, 14, 17, 20, 23)  # 단기예보 issue hours; each is served about 10 minutes later


def grid(lat, lon):
    """KMA's 5 km Lambert conformal conic grid (the formula from the 동네예보 guide): GIST -> (57, 74)."""
    import math
    re_, g, s1, s2, olon, olat, xo, yo = 6371.00877, 5.0, 30.0, 60.0, 126.0, 38.0, 43, 136
    d = math.pi / 180.0
    r = re_ / g
    sn = math.log(math.cos(s1 * d) / math.cos(s2 * d)) / math.log(math.tan(math.pi * 0.25 + s2 * d * 0.5) /
                                                                 math.tan(math.pi * 0.25 + s1 * d * 0.5))
    sf = math.tan(math.pi * 0.25 + s1 * d * 0.5) ** sn * math.cos(s1 * d) / sn
    ro = r * sf / math.tan(math.pi * 0.25 + olat * d * 0.5) ** sn
    ra = r * sf / math.tan(math.pi * 0.25 + lat * d * 0.5) ** sn
    theta = lon * d - olon * d
    theta = theta - 2 * math.pi if theta > math.pi else theta + 2 * math.pi if theta < -math.pi else theta
    theta *= sn
    return int(ra * math.sin(theta) + xo + 0.5), int(ro - ra * math.cos(theta) + yo + 0.5)


def in_korea(lat, lon):
    return 33.0 <= lat <= 38.7 and 124.5 <= lon <= 132.0


def bases(now):
    """(초단기실황, 초단기예보, 단기예보) base (date, time) for `now` (KST): 실황 HH00 from HH:40, 예보 HH30 from HH:45, and the
    단기예보 of 02:00 today (from 02:10; before that 23:00 yesterday), which still holds today's 최저·최고 (TMN at 06, TMX at 15)."""
    from datetime import timedelta
    n = now - timedelta(minutes=40)
    f = now - timedelta(minutes=45)
    v = now.replace(hour=2, minute=0) if (now.hour, now.minute) >= (2, 10) else (now - timedelta(days=1)).replace(hour=23, minute=0)
    return ((n.strftime("%Y%m%d"), n.strftime("%H00")), (f.strftime("%Y%m%d"), f.strftime("%H30")),
            (v.strftime("%Y%m%d"), v.strftime("%H00")))


KMA_TRIES = (0.0, 0.3, 0.6, 1.0)
KMA_CALL_S = 2.5  # one request; data.go.kr sometimes just hangs
KMA_BUDGET_S = 5.0  # all of KMA for one answer, retries included; then Open-Meteo answers instead  # data.go.kr answers 500 APPLICATION_ERROR to about every other call (2026-11-22): retry


async def kma_items(c, op, key, base, nx, ny):
    for i, wait in enumerate(KMA_TRIES):
        await asyncio.sleep(wait)
        try:
            r = await c.get(KMA_URL + op, timeout=KMA_CALL_S, params={
                "serviceKey": key, "pageNo": 1, "numOfRows": 1000, "dataType": "JSON", "base_date": base[0],
                "base_time": base[1], "nx": nx, "ny": ny})
        except httpx.TimeoutException:
            if i < len(KMA_TRIES) - 1:
                continue
            raise
        if r.status_code >= 500 and i < len(KMA_TRIES) - 1:
            continue
        r.raise_for_status()
        d = r.json()["response"]
        if d["header"]["resultCode"] != "00":
            raise RuntimeError(f"KMA {d['header']['resultCode']}")
        return d["body"]["items"]["item"]


def day_summary(vilage, date):
    """(최저, 최고, 강수확률 최대, 하늘 말) for one date (YYYYMMDD) from 단기예보 items; None where missing."""
    rows = [i for i in vilage if i["fcstDate"] == date]
    val = lambda cat: [float(i["fcstValue"]) for i in rows if i["category"] == cat]  # noqa: E731
    tmn, tmx, tmp, pop = val("TMN"), val("TMX"), val("TMP"), val("POP")
    lo = tmn[0] if tmn else (min(tmp) if tmp else None)
    hi = tmx[0] if tmx else (max(tmp) if tmp else None)
    wet = [i["fcstValue"] for i in rows if i["category"] == "PTY" and i["fcstValue"] != "0" and "06" <= i["fcstTime"][:2] <= "21"]
    noon = next((i["fcstValue"] for i in rows if i["category"] == "SKY" and i["fcstTime"] == "1200"), None) or \
        next((i["fcstValue"] for i in rows if i["category"] == "SKY"), None)
    sky = KMA_PTY.get(max(set(wet), key=wet.count)) if wet else KMA_SKY.get(noon or "")
    return lo, hi, (int(max(pop)) if pop else None), sky


def kma_spoken(where, when, now, ncst, fcst, vilage):
    """Short and natural: '기상청 기준 지금 광주는 9도이고 맑아요. 오늘 최고 14도, 최저 2도, 비 올 확률은 30%예요.'"""
    from datetime import timedelta
    day = DAYS.get(when, 0)
    lo, hi, pop, sky = day_summary(vilage, (now + timedelta(days=day)).strftime("%Y%m%d"))
    temps = ", ".join(x for x in (f"최고 {deg(hi)}" if hi is not None else "", f"최저 {deg(lo)}" if lo is not None else "",
                                  f"비 올 확률은 {pop}%" if pop is not None else "") if x)
    if when == "now" and ncst:
        obs = {i["category"]: i["obsrValue"] for i in ncst}
        t = float(obs["T1H"])  # the 02:00 forecast's high or low already passed: never "지금 10도, 최고 9도"
        hi, lo = (max(hi, t) if hi is not None else None), (min(lo, t) if lo is not None else None)
        temps = ", ".join(x for x in (f"최고 {deg(hi)}" if hi is not None else "", f"최저 {deg(lo)}" if lo is not None
                                      else "", f"비 올 확률은 {pop}%" if pop is not None else "") if x)
        pty = obs.get("PTY", "0")
        next_sky = next((i["fcstValue"] for i in sorted(fcst or [], key=lambda i: (i["fcstDate"], i["fcstTime"]))
                         if i["category"] == "SKY"), None)
        now_sky = KMA_PTY.get(pty) if pty != "0" else KMA_SKY.get(next_sky or "")
        out = f"기상청 기준 지금 {topic(where)} {deg(float(obs['T1H']))}" + (f"이고 {now_sky}" if now_sky else "예요") + "."
        soon = [i for i in fcst or [] if i["category"] == "PTY" and i["fcstValue"] != "0"]
        if pty == "0" and soon:
            out += " 몇 시간 안에 " + KMA_PTY.get(soon[0]["fcstValue"], "비가 와요").replace("와요", "올 수 있어요") \
                .replace("있어요", "있을 수 있어요").replace("수 있을 수 있어요", "수 있어요") + "."
        return out + (f" 오늘 {temps}예요." if temps else "")
    head = f"기상청 기준 {DAY_WORD[day]} {topic(where)} " + (f"{sky}." if sky else "")
    return (head + (f" {temps}예요." if temps else "")).strip()


async def kma_now_items(p, key, c, now):
    """(초단기실황, 초단기예보, 단기예보) items for `now` at place p. The observation is required (raises); the next hours and
    the day's forecast are [] when they did not come. Shared by the voice answer and the TV's weather card."""
    nx, ny = grid(p["lat"], p["lon"])
    b_ncst, b_fcst, b_vil = bases(now)
    ncst, fcst, vilage = await asyncio.gather(kma_items(c, "getUltraSrtNcst", key, b_ncst, nx, ny),
                                              kma_items(c, "getUltraSrtFcst", key, b_fcst, nx, ny),
                                              kma_items(c, "getVilageFcst", key, b_vil, nx, ny), return_exceptions=True)
    if isinstance(ncst, BaseException):
        raise ncst
    fcst, vilage = [x if isinstance(x, list) else [] for x in (fcst, vilage)]
    return ncst, fcst, vilage


async def kma_forecast(p, when, key, c, now=None):
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone(timedelta(hours=9)))
    nx, ny = grid(p["lat"], p["lon"])
    b_ncst, b_fcst, b_vil = bases(now)
    if when == "now":  # the observation is needed; the next hours and the day's high/low are said when they came
        ncst, fcst, vilage = await kma_now_items(p, key, c, now)
    else:
        ncst, fcst, vilage = None, None, await kma_items(c, "getVilageFcst", key, b_vil, nx, ny)
    where = re.sub(r"(광역시|특별시|특별자치시)$", "", p["name"]) or p["name"]
    return {"spoken": kma_spoken(where, when, now, ncst, fcst, vilage), "place": p, "source": "기상청",
            "data": {"grid": [nx, ny], "ncst": ncst}}


HERE = re.compile(r"^(지금\s*)?(여기|이\s*근처|근처|내\s*위치|이\s*동네)$")


async def forecast(place_name, when, home, client=None, kma_key=None, here=None, now=None):
    """{"spoken", "place", "data"} for place_name (or home: {"name", "lat", "lon"}; "여기" asks `here()`, the phone's
    location, when there is one). KMA for Korea with a key, Open-Meteo otherwise or when KMA fails. Raises
    LookupError for an unknown place, httpx errors when both are down."""
    when = when if when in DAYS else "now"
    c = client or httpx.AsyncClient(timeout=TIMEOUT_S)
    try:
        if place_name and HERE.match(place_name.strip()):
            loc = await here() if here else None
            place_name = None
            p = loc if loc and "lat" in loc else (await place(loc["name"], c) if loc and loc.get("name") else None) or home
        else:
            p = await place(place_name, c) if place_name else home
        if not p:
            raise LookupError(f"'{place_name}'이라는 곳을 찾지 못했어요.")
        if kma_key and in_korea(p["lat"], p["lon"]):
            try:
                return await asyncio.wait_for(kma_forecast(p, when, kma_key, c, now), KMA_BUDGET_S)
            except (httpx.HTTPError, RuntimeError, KeyError, ValueError, TypeError, asyncio.TimeoutError) as e:
                print("kma failed, Open-Meteo instead:", type(e).__name__, flush=True)
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
    return {"spoken": spoken(where, when, d), "place": p, "source": "Open-Meteo",
            "data": {"current": d.get("current"), "daily": d.get("daily")}}
