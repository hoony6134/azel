"""Voice call cards (cards.py): built from each tool's own data, fields without data left out, under 32 KB."""
import asyncio
import json
import sys
from datetime import date as Date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_gateway import gw  # noqa: F401  the fixture (and the gateway directory on sys.path)
import cards  # noqa: E402
import tools_bridge  # noqa: E402

NOW = datetime(2026, 11, 23, 14, 0, tzinfo=cards.KST).timestamp()


def ok(spoken, data=None):
    return tools_bridge.result(True, spoken, data=data)


def test_agenda_card_from_merged_rows():
    rows = [{"day": Date(2026, 11, 24), "start": "10:00", "end": "11:30", "title": "치과", "where": "광주", "cal": "기본",
             "ref": "ab12cd", "src": "calendar"},
            {"day": Date(2026, 11, 24), "start": "23:00", "end": "01:00", "title": "야간 작업", "where": "", "cal": "",
             "src": "calendar"},
            {"day": Date(2026, 11, 25), "start": "", "end": "", "text": "🔁 과제 마감", "src": "gistra"}]
    data = {"text": "일정", "from": "2026-11-24", "to": "2026-11-25", "items": cards.agenda_items(rows)}
    c = cards.build("agenda", ok("일정 3건", data))
    assert c["kind"] == "agenda" and c["range_label"] == "11월 24일 (화) ~ 11월 25일 (수)"
    assert c["items"][0] == {"title": "치과", "source": "google", "calendar": "기본", "location": "광주", "all_day": False,
                             "start": "2026-11-24T10:00:00+09:00", "end": "2026-11-24T11:30:00+09:00"}
    assert c["items"][1]["end"] == "2026-11-25T01:00:00+09:00"  # past midnight: the next day
    assert c["items"][2] == {"title": "과제 마감", "source": "gistra", "all_day": True, "start": "2026-11-25"}
    json.dumps(c)
    assert cards.build("agenda", ok("x", {**data, "text": "일정을 지금 확인할 수 없어요."})) is None


def test_stock_card_takes_the_tv_chart_and_skips_rates():
    q = {"name": "삼성전자", "price": 180900.0, "change": -16000.0, "pct": -8.13, "currency": "KRW", "symbol": "005930"}
    c = cards.build("stock_quote", ok("x", q))
    assert c == {"kind": "stock", "title": "삼성전자", "symbol": "005930", "name": "삼성전자", "price": 180900.0,
                 "change": -16000.0, "change_pct": -8.13, "currency": "KRW", "prev_close": 196900.0}
    detail = {"price": 181000.0, "prev": 196900.0, "change": -15900.0, "pct": -8.08, "state": "closed", "high": 190000.0,
              "low": 180000.0, "volume": 123, "index": False, "points": [[1, 2.0], [2, 3.0]]}
    c = cards.build("stock_quote", ok("x", q), detail)
    assert c["points"] == [[1, 2.0], [2, 3.0]] and c["range"] == "1d" and c["market_state"] == "closed"
    assert c["is_index"] is False and c["name"] == "삼성전자"
    assert c["price"] == 180900.0 and c["prev_close"] == 196900.0  # what was said, not the chart's delayed price
    assert cards.build("stock_quote", ok("x", {"price": 1, "rate": 1337.8, "per": 1})) is None


def test_portfolio_card():
    rows = [{"name": "애플", "ticker": "AAPL", "listed": True, "price": 342.5, "pct": -1.9, "spark": [1, 2]},
            {"name": "토스", "listed": False}]
    c = cards.build("portfolio", ok("x", {"holdings": rows}))
    assert c["items"] == [{"symbol": "AAPL", "name": "애플", "price": 342.5, "change_pct": -1.9, "spark": [1, 2]},
                          {"name": "토스"}]


def test_weather_card_kma_and_open_meteo():
    vil = [{"fcstDate": "20261123", "fcstTime": t, "category": k, "fcstValue": v}
           for t in ("1500", "1600") for k, v in (("TMP", "8"), ("POP", "30"), ("SKY", "3"), ("PTY", "0"))]
    vil += [{"fcstDate": "20261124", "fcstTime": "1200", "category": k, "fcstValue": v}
            for k, v in (("TMN", "1"), ("TMX", "9"), ("TMP", "7"), ("POP", "60"), ("SKY", "4"), ("PTY", "1"))]
    ncst = [{"category": k, "obsrValue": v} for k, v in (("T1H", "7.5"), ("REH", "55"), ("WSD", "2.1"), ("PTY", "0"))]
    r = {"spoken": "x", "place": {"name": "광주"}, "source": "기상청", "data": {"ncst": ncst, "fcst": [], "vilage": vil}}
    c = cards.build("weather", ok("x", r), now=NOW)
    assert c["title"] == "광주 날씨" and c["now"]["temp"] == 7.5 and c["now"]["humidity"] == 55 and c["now"]["wind"] == 2.1
    assert c["hours"][0] == {"t": "2026-11-23T15:00:00+09:00", "temp": 8.0, "icon": "cloudy", "pop": 30}
    assert c["days"][1] == {"d": "2026-11-24", "min": 1.0, "max": 9.0, "pop": 60, "icon": "rain"}
    om = {"spoken": "x", "place": {"name": "Tokyo"}, "source": "Open-Meteo",
          "data": {"current": {"temperature_2m": 12.0, "weather_code": 0, "relative_humidity_2m": 40, "wind_speed_10m": 3},
                   "daily": {"time": ["2026-11-23"], "temperature_2m_min": [5], "temperature_2m_max": [14],
                             "precipitation_probability_max": [10], "weather_code": [2]}}}
    c = cards.build("weather", ok("x", om))
    assert c["now"] == {"temp": 12.0, "humidity": 40, "wind": 3, "icon": "clear", "sky": "맑음"}
    assert c["days"] == [{"d": "2026-11-23", "min": 5, "max": 14, "pop": 10, "icon": "cloudy"}] and "hours" not in c


TODAY = """스냅샷: 11월 23일 (월) 12:16

## 2026-11-23 기준

### 활동 일정
- 🔁 20:00~22:00 지온나래 · 지온나래 정기회의 (정기 회의) · 구관 토론실

### 7일 안 마감
- 11월 23일 (월) 23:00 · 역사학특강 — 13주차 기록지 제출
- 11월 30일 (월) 23:59 · 할 일 — Agentic AI 기획서 작성
"""
ASSIGN = """스냅샷: 11월 23일 (월) 12:16

## 남은 항목 (7)
- ⬜ 11월 23일 (월) 23:00 · 역사학특강 — 13주차 기록지 제출 [과제]
- ✅ 11월 30일 (월) 23:59 · Agentic AI 기획서 작성 [할 일]
… 외 2개 생략"""


def test_todo_cards():
    c = cards.build("gistra_today", ok(TODAY), now=NOW)
    assert c["items"] == [{"title": "13주차 기록지 제출", "due": "2026-11-23T23:00:00+09:00", "course": "역사학특강",
                           "source": "gistra", "done": False},
                          {"title": "Agentic AI 기획서 작성", "due": "2026-11-30T23:59:00+09:00", "source": "gistra",
                           "done": False}]
    c = cards.build("gistra_assignments", ok(ASSIGN), now=NOW)
    assert [(i["title"], i.get("course"), i["done"]) for i in c["items"]] == [
        ("13주차 기록지 제출", "역사학특강", False), ("Agentic AI 기획서 작성", None, True)]
    assert cards.build("gistra_today", ok("지금은 Cygnus에 연결할 수 없어요.")) is None
    planit = json.dumps({"date": "2026-11-23", "events": [{"title": "듀오링고", "allDay": True, "start": "2026-11-23",
                                                           "done": True}]})
    assert cards.build("planit_get_day", ok(planit))["items"] == [
        {"title": "듀오링고", "due": "2026-11-23", "source": "planit", "done": True}]
    assert cards.build("planit_get_day", ok("## 11월 23일\n- 듀오링고")) is None


def test_infra_cards():
    health = """As of 2026-11-23 14:00 KST (cpu = 5-min avg, disk = /)
✓ fawaris [cygnus] cpu 5% · mem 50% · disk 29% · 69°C · load 0.26/4 · uptime 24d
✗ albireo [cygnus] DOWN"""
    c = cards.build("infra_health", ok(health), now=NOW)
    assert c["hosts"] == [{"name": "fawaris", "up": True, "cpu_pct": 5, "mem_pct": 50, "disk_pct": 29, "temp_c": 69},
                          {"name": "albireo", "up": False}]
    status = "# Cygnus · Ops (internal)\n## vd-spark · Vuja De GPU\n  ✗ Vuja De claw (NemoClaw) (http) DOWN · 24h 2.85%"
    assert cards.build("infra_status", ok(status))["down"] == ["Vuja De claw (NemoClaw)"]
    assert cards.build("infra_alerts", ok("No firing alerts."))["hosts"] == []
    assert cards.build("infra_health", ok("지금은 Cygnus에 연결할 수 없어요.")) is None
    assert cards.build("infra_health", tools_bridge.result(False, "x")) is None


def test_fit_trims_to_32kb():
    c = cards.fit({"kind": "todo", "title": "t", "items": [{"title": "가" * 200}] * 400})
    assert len(json.dumps(c, ensure_ascii=False).encode()) <= cards.MAX_BYTES and c["items"]


def test_serve_tool_attaches_the_card(gw, monkeypatch):  # noqa: F811
    g, _, _ = gw
    sent = []

    async def capture(obj):
        sent.append(obj)
        return True

    async def run(name, args, *a, **k):
        return ok("보유 종목 1개 시세예요.", {"holdings": [{"name": "애플", "ticker": "AAPL", "price": 1.0, "pct": 0.5}]})
    monkeypatch.setattr(g, "link_send", capture)
    monkeypatch.setattr(g.bridge, "run", run)
    asyncio.run(g.serve_tool({"id": "t1", "type": "tool_call", "name": "portfolio", "args": {}}))
    assert sent[-1]["card"] == {"kind": "portfolio", "title": "내 종목",
                                "items": [{"symbol": "AAPL", "name": "애플", "price": 1.0, "change_pct": 0.5}]}
    asyncio.run(g.serve_tool({"id": "t2", "type": "tool_call", "name": "web_search", "args": {}}))
    assert "card" not in sent[-1]
