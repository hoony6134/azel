"""The weather action: Korean place names, the spoken sentence, the home default, and its place on every path."""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import weather  # noqa: E402
from test_tasks import tgw  # noqa: E402,F401

PLACES = {  # what Open-Meteo's geocoder answers (2026-11-22): a bare "광주" is a village in 충남, "서울" is nothing
    "광주": [{"name": "광주", "latitude": 36.91, "longitude": 127.13, "country_code": "KR"}],
    "광주광역시": [{"name": "광주광역시", "latitude": 35.15, "longitude": 126.92, "population": 1401235, "country_code": "KR"}],
    "서울특별시": [{"name": "서울특별시", "latitude": 37.57, "longitude": 126.98, "population": 10349312, "country_code": "KR"}],
    "도쿄": [{"name": "도쿄", "latitude": 35.69, "longitude": 139.69, "population": 9733276, "country_code": "JP"}],
}
FORECAST = {"current": {"temperature_2m": 8.7, "apparent_temperature": 5.4, "weather_code": 1, "precipitation": 0},
            "daily": {"temperature_2m_max": [8.7, 14.3, 11.3], "temperature_2m_min": [-1.7, 1.9, 3.6],
                      "precipitation_probability_max": [31, 86, 57], "weather_code": [1, 61, 3]}}


def client(asked):
    def handle(req):
        if "geocoding" in req.url.host:
            return httpx.Response(200, json={"results": PLACES.get(req.url.params["name"], [])})
        asked.append((float(req.url.params["latitude"]), float(req.url.params["longitude"])))
        return httpx.Response(200, json=FORECAST)
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


HOME = {"name": "광주", "lat": 35.2285, "lon": 126.8434}


@pytest.mark.parametrize("place,when,where,said", [
    ("광주", "now", (35.15, 126.92), "지금 광주는 9도(체감 5도)이고 대체로 맑아요. 오늘 최고 9도, 최저 영하 2도, 비 올 확률은 31%예요."),
    ("서울", "tomorrow", (37.57, 126.98), "내일 서울은 비가 조금 와요. 최고 14도, 최저 2도, 비 올 확률은 86%예요."),
    ("", "day_after", (35.2285, 126.8434), "모레 광주는 흐려요. 최고 11도, 최저 4도, 비 올 확률은 57%예요."),
    ("도쿄", "today", (35.69, 139.69), "오늘 도쿄는 대체로 맑아요. 최고 9도, 최저 영하 2도, 비 올 확률은 31%예요."),
])
def test_places_and_sentences(place, when, where, said):
    asked = []
    r = asyncio.run(weather.forecast(place, when, HOME, client(asked)))
    assert asked == [where] and r["spoken"] == said


def test_an_unknown_place_says_so():
    with pytest.raises(LookupError):
        asyncio.run(weather.forecast("없는곳", "now", HOME, client([])))


def test_weather_is_a_tool_on_every_path_local_included(tgw, monkeypatch):
    g, c, _ = tgw

    async def fake(place, when, home, client=None, **kw):
        return {"spoken": f"{place or home['name']} {when} 맑아요.", "place": home, "data": {}}
    monkeypatch.setattr(g.weather, "forecast", fake)
    for mode in ("auto", "local"):
        assert c.put("/v1/settings", json={"brain_mode": mode}).status_code == 200
        r = c.portal.call(g.bridge.run, "weather", {"when": "tomorrow"}, None, "qwen")
        assert r["ok"] and r["spoken"] == "광주 tomorrow 맑아요."
        assert "weather" in [t["name"] for t in c.portal.call(g.bridge.tools, "qwen")[0]]
    assert c.put("/v1/settings", json={"weather_home": {"name": "서울", "lat": 37.5, "lon": 127.0}}).status_code == 200
    assert c.portal.call(g.bridge.run, "weather", {}, None, "api")["spoken"] == "서울 now 맑아요."
    assert c.put("/v1/settings", json={"weather_home": {"name": "x", "lat": 99, "lon": 0}}).status_code == 400

    async def down(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(g.weather, "forecast", down)
    assert c.portal.call(g.bridge.run, "weather", {}, None, "qwen")["error"]["code"] == "unavailable"


# ---------- 기상청 (KMA): a recorded response (GIST, 2026-11-22 15:37 KST; categories used only), no live calls ----------
import json  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

KST = timezone(timedelta(hours=9))
FIX = json.loads((Path(__file__).resolve().parent / "fixtures/kma_gist.json").read_text())
AT = datetime.fromisoformat(FIX["now"])


def kma_client(asked, fail=None):
    def handle(req):
        if "data.go.kr" in req.url.host:
            op = req.url.path.rsplit("/", 1)[1]
            asked.append((op, req.url.params["base_date"], req.url.params["base_time"], req.url.params["nx"],
                          req.url.params["ny"], req.url.params["serviceKey"]))
            if fail == "http" or (fail == "once" and sum(a[0] == op for a in asked) == 1):
                return httpx.Response(500, text="APPLICATION_ERROR")
            code = "03" if fail == "nodata" else "00"
            return httpx.Response(200, json={"response": {"header": {"resultCode": code, "resultMsg": "x"},
                                                          "body": {"items": {"item": FIX[op]}}}})
        if "geocoding" in req.url.host:
            return httpx.Response(200, json={"results": PLACES.get(req.url.params["name"], [])})
        asked.append(("open-meteo",))
        return httpx.Response(200, json=FORECAST)
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def test_grid_matches_kma_points():
    assert weather.grid(37.5665, 126.9780) == (60, 127)  # 서울시청
    assert weather.grid(35.1796, 129.0756) == (98, 76)  # 부산시청
    assert weather.grid(33.4996, 126.5312) == (53, 38)  # 제주시
    assert weather.grid(35.1395, 126.7937) == (57, 74)  # 광주 광산구 (checked live: nx=57, ny=74)
    assert weather.grid(35.2285, 126.8434) == (58, 76)  # GIST, 북구 오룡동


def test_base_times():
    at = lambda h, m, d=22: datetime(2026, 11, d, h, m, tzinfo=KST)  # noqa: E731
    assert weather.bases(at(15, 37)) == (("20261122", "1400"), ("20261122", "1430"), ("20261122", "0200"))
    assert weather.bases(at(15, 41)) == (("20261122", "1500"), ("20261122", "1430"), ("20261122", "0200"))
    assert weather.bases(at(0, 20)) == (("20261121", "2300"), ("20261121", "2330"), ("20261121", "2300"))
    assert weather.bases(at(2, 5))[2] == ("20261121", "2300") and weather.bases(at(2, 10))[2] == ("20261122", "0200")


@pytest.mark.parametrize("when,said", [
    ("now", "기상청 기준 지금 광주는 10도이고 맑아요. 오늘 최고 10도, 최저 영하 3도, 비 올 확률은 0%예요."),
    ("today", "기상청 기준 오늘 광주는 맑아요. 최고 9도, 최저 영하 3도, 비 올 확률은 0%예요."),
    ("tomorrow", "기상청 기준 내일 광주는 비가 와요. 최고 14도, 최저 0도, 비 올 확률은 60%예요."),
])
def test_kma_for_korea(when, said):
    asked = []
    r = asyncio.run(weather.forecast("", when, HOME, kma_client(asked), kma_key="k3y", now=AT))
    assert r["spoken"] == said and r["source"] == "기상청"
    ops = {a[0] for a in asked}
    assert ops == ({"getUltraSrtNcst", "getUltraSrtFcst", "getVilageFcst"} if when == "now" else {"getVilageFcst"})
    assert all(a[3:] == ("58", "76", "k3y") for a in asked)


@pytest.mark.parametrize("fail", ["http", "nodata"])
def test_kma_failure_falls_back_to_open_meteo(fail, monkeypatch):
    monkeypatch.setattr(weather, "KMA_TRIES", (0, 0, 0))
    asked = []
    r = asyncio.run(weather.forecast("", "now", HOME, kma_client(asked, fail), kma_key="k3y", now=AT))
    assert r["source"] == "Open-Meteo" and r["spoken"].startswith("지금 광주는 9도") and ("open-meteo",) in asked


def test_a_transient_kma_error_is_retried(monkeypatch):
    monkeypatch.setattr(weather, "KMA_TRIES", (0, 0, 0))
    asked = []
    r = asyncio.run(weather.forecast("", "today", HOME, kma_client(asked, "once"), kma_key="k3y", now=AT))
    assert r["source"] == "기상청" and [a[0] for a in asked] == ["getVilageFcst", "getVilageFcst"]


def test_now_without_the_forecasts_still_says_the_observation(monkeypatch):
    monkeypatch.setattr(weather, "KMA_TRIES", (0,))
    fail = {"getUltraSrtFcst", "getVilageFcst"}

    def handle(req):
        op = req.url.path.rsplit("/", 1)[1]
        if op in fail:
            return httpx.Response(500, text="APPLICATION_ERROR")
        return httpx.Response(200, json={"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": FIX[op]}}}})
    r = asyncio.run(weather.forecast("", "now", HOME, httpx.AsyncClient(transport=httpx.MockTransport(handle)),
                                     kma_key="k3y", now=AT))
    assert r["spoken"] == "기상청 기준 지금 광주는 10도예요." and r["source"] == "기상청"


def test_abroad_and_without_a_key_open_meteo():
    asked = []
    r = asyncio.run(weather.forecast("도쿄", "today", HOME, kma_client(asked), kma_key="k3y", now=AT))
    assert r["source"] == "Open-Meteo" and not any(a[0].startswith("get") for a in asked)
    r = asyncio.run(weather.forecast("", "now", HOME, kma_client([]), kma_key=None, now=AT))
    assert r["source"] == "Open-Meteo"


def test_here_uses_the_phone_location_else_home():
    asked = []

    async def here():
        return {"name": "서울역", "lat": 37.5547, "lon": 126.9706}
    r = asyncio.run(weather.forecast("지금 여기", "today", HOME, kma_client(asked), kma_key="k3y", here=here, now=AT))
    assert r["place"]["name"] == "서울역" and asked[0][3:5] == ("60", "126")
    asked.clear()
    asyncio.run(weather.forecast("여기", "today", HOME, kma_client(asked), kma_key="k3y", here=None, now=AT))
    assert asked[0][3:5] == ("58", "76")  # no phone location: home


def test_the_kma_key_stays_on_the_full_gateway(tgw, tmp_path):  # noqa: F811
    g, _, _ = tgw
    assert g.kma_key() is None  # no file
    g.KMA_KEY_PATH.write_text("k3y\n")
    g.KMA_KEY_PATH.chmod(0o644)
    assert g.kma_key() is None  # readable by others: refused
    g.KMA_KEY_PATH.chmod(0o600)
    assert g.kma_key() == "k3y"
    g.MODE = "limited"
    assert g.kma_key() is None
    g.MODE = "full"
