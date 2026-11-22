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

    async def fake(place, when, home, client=None):
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
