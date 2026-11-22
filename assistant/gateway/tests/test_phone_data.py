"""iPhone data (PHONE_DATA.md): upload checks, ranges, the spoken results, live vs synced, and the gateway routes."""
import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import phone_data as P  # noqa: E402
from test_tasks import tgw  # noqa: E402,F401  (fixture)
from test_tools_routes import tailnet  # noqa: E402

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 22, 9, 30, tzinfo=KST)  # a Sunday
TODAY = NOW.date()


def day(d, **kw):
    return {"date": d, **kw}


class Store:
    def __init__(self, value=None):
        self.value = value

    async def __call__(self, method, path, body=None, **kw):
        assert path == P.KV
        if method == "PUT":
            self.value = body
        return self.value


def offline():
    async def live(kind, args, timeout):
        return {"ok": False, "error": "offline"}
    return live


def phone(data=None, error=None, seen=None):
    async def live(kind, args, timeout):
        if seen is not None:
            seen.append((kind, args))
        return {"ok": False, "error": error} if error else {"ok": True, "data": data or {}}
    return live


def run(name, args, store=None, live=None):
    return asyncio.run(P.run(name, args, store or Store(), live or offline(), NOW))


# ---------- upload checks ----------
def test_clean_day_keeps_known_fields_and_refuses_the_rest():
    d = P.clean_day(day("2026-11-21", steps=8532, sleep={"start": "2026-11-21T01:10:00+09:00", "asleep_min": 400},
                        workouts=[{"type": "running", "start": "2026-11-21T07:00:00+09:00", "minutes": 30}]), TODAY)
    assert d["steps"] == 8532 and d["sleep"]["asleep_min"] == 400 and d["workouts"][0]["type"] == "running"
    for bad in (day("2026-11-21", steps=-1), day("2026-11-21", steps=True), day("2026-11-21", notes="x"),
                day("2026-11-21", sleep={"dream": 1}), day("2026-11-21", workouts=[{"type": "Run!"}]),
                day("2026-11-21", sleep={"start": "2026-11-21T01:10:00"}), day("2025-01-01", steps=1),
                day("2026-12-01", steps=1), {"steps": 1}, day("2026-11-21", cycle="lots")):
        with pytest.raises(ValueError):
            P.clean_day(bad, TODAY)


def test_merge_replaces_by_date_and_keeps_30_days():
    old = {"days": {"2026-10-01": day("2026-10-01", steps=1), "2026-11-20": day("2026-11-20", steps=2)}}
    m = P.merge(old, [day("2026-11-20", steps=5), day("2026-11-21", steps=6)], TODAY, 100)
    assert list(m["days"]) == ["2026-11-20", "2026-11-21"] and m["days"]["2026-11-20"]["steps"] == 5
    assert m["updated"] == 100


def test_spans():
    assert P.span("this_week", TODAY) == (date(2026, 11, 16), TODAY)
    assert P.span("last_week", TODAY) == (date(2026, 11, 9), date(2026, 11, 15))
    assert P.span("this_week", TODAY, cal=True) == (TODAY, TODAY)  # Sunday: the rest of the week is today
    assert P.span("next_week", TODAY, cal=True) == (date(2026, 11, 23), date(2026, 11, 29))
    assert P.span("2026-11-03", TODAY) == (date(2026, 11, 3),) * 2
    with pytest.raises(ValueError):
        P.span("tomorrow", TODAY)  # health has no future


# ---------- spoken results ----------
def test_steps_today_live_and_week_from_sync():
    seen = []
    text, data = run("phone_health", {"metric": "steps"}, live=phone({"days": [day("2026-11-22", steps=8532)]}, seen=seen))
    assert text == "오늘 걸음 수는 8,532걸음이에요." and data["source"] == "live"
    assert seen == [("health", {"from": "2026-11-22", "to": "2026-11-22", "metrics": ["steps"]})]
    store = Store({"days": {"2026-11-20": day("2026-11-20", steps=10000), "2026-11-21": day("2026-11-21", steps=6000),
                            "2026-11-10": day("2026-11-10", steps=99999)}, "updated": NOW.timestamp() - 600})
    text, data = run("phone_health", {"metric": "steps", "range": "this_week"}, store)
    assert text.startswith("이번 주(11월 16일–22일) 걸음 수 합계는 16,000걸음, 하루 평균 8,000걸음이에요.")
    assert "오전 9시 20분에 받은 기록 기준" in text and data["source"] == "sync"


def test_heart_rate_and_weight():
    days = [day("2026-11-21", hr_avg=70, hr_min=52, hr_max=150, weight_kg=70.24),
            day("2026-11-22", hr_avg=74, hr_min=55, hr_max=120)]
    text, _ = run("phone_health", {"metric": "heart_rate", "range": "last_7_days"}, live=phone({"days": days}))
    assert text == "최근 7일(11월 16일–22일) 심박수는 평균 72, 가장 낮을 때 52, 가장 높을 때 150이에요(분당 횟수)."
    text, _ = run("phone_health", {"metric": "weight", "range": "last_7_days"}, live=phone({"days": days}))
    assert text == "가장 최근 몸무게는 70.2킬로그램이에요(어제)."


def test_sleep_latest_night():
    night = day("2026-11-22", sleep={"start": "2026-11-22T01:10:00+09:00", "end": "2026-11-22T08:05:00+09:00",
                                     "asleep_min": 400, "deep_min": 62, "rem_min": 90})
    seen = []
    text, _ = run("phone_sleep", {}, live=phone({"days": [day("2026-11-21"), night]}, seen=seen))
    assert seen[0][1] == {"from": "2026-11-21", "to": "2026-11-22"}
    assert text == ("어젯밤 6시간 40분 주무셨어요. 오전 1시 10분에 잠들어 오전 8시 5분에 일어나셨어요. "
                    "깊은 잠 1시간 2분, 렘수면 1시간 30분이에요.")
    text, _ = run("phone_sleep", {}, live=phone({"days": []}))
    assert text == "최근 수면 기록이 없어요."


def test_sleep_over_a_week_from_the_synced_days():  # live 2026-11-22: "일주일 동안 내가 얼마나 잤는지"
    mins = [400, 380, 420, 350, 460, 390, 410]
    store = Store({"days": {f"2026-11-{16 + i}": day(f"2026-11-{16 + i}", sleep={"asleep_min": m})
                            for i, m in enumerate(mins)}, "updated": NOW.timestamp() - 3600})
    text, data = run("phone_sleep", {"range": "last_7_days"}, store=store)
    assert data["source"] == "sync" and text.startswith(
        "최근 7일(11월 16일–22일) 기록된 7일 동안 하루 평균 6시간 41분 주무셨어요. 가장 길게는 11월 20일 아침까지 7시간 40분, "
        "가장 짧게는 11월 19일 아침까지 5시간 50분이었어요."), text
    seen = []
    run("phone_sleep", {"range": "this_week"}, live=phone({"days": []}, seen=seen))
    assert seen[0][1] == {"from": "2026-11-16", "to": "2026-11-22"}


def test_workouts_week():
    days = [day("2026-11-18", workouts=[{"type": "running", "minutes": 30, "kcal": 300}]),
            day("2026-11-20", workouts=[{"type": "strength", "minutes": 45}, {"type": "walking", "minutes": 55}])]
    text, _ = run("phone_workouts", {}, live=phone({"days": days}))
    assert text == "이번 주(11월 16일–22일) 운동은 3번, 모두 2시간 10분, 300킬로칼로리예요. 달리기 30분, 근력 운동 45분, 걷기 55분."


def test_live_only_actions():
    ev = [{"title": "랩미팅", "start": "2026-11-23T10:00:00+09:00", "end": "2026-11-23T11:00:00+09:00"}]
    assert run("phone_calendar", {"range": "tomorrow"}, live=phone({"events": ev}))[0] == \
        "아이폰 캘린더에 내일 일정이 1개 있어요. 오전 10시 랩미팅."
    items = [{"title": "우유 사기", "due": "2026-11-22T18:00:00+09:00"}, {"title": "책 반납"}]
    assert run("phone_reminders", {}, live=phone({"items": items}))[0] == "미리 알림에 2개가 있어요. 우유 사기(오늘까지), 책 반납."
    m = [{"name": "김철수", "phones": [{"label": "휴대전화", "number": "010-1234-5678"}], "birthday": "1999-03-07"}]
    assert run("phone_contacts", {"name": "철수"}, live=phone({"matches": m}))[0] == \
        "김철수: 휴대전화 010-1234-5678, 생일 3월 7일."
    loc = {"name": "GIST", "place": "광주광역시 북구 오룡동", "activity": "walking"}
    assert run("phone_location", {}, live=phone(loc))[0] == "지금 GIST, 광주광역시 북구 오룡동 근처예요. 걷는 중이에요."
    assert run("phone_now_playing", {}, live=phone({"title": "밤편지", "artist": "아이유", "playing": True}))[0] == \
        "지금 아이유의 '밤편지'을(를) 듣고 계세요."


def test_unavailable_messages():
    for name, args in (("phone_location", {}), ("phone_calendar", {}), ("phone_now_playing", {})):
        with pytest.raises(P.Unavailable, match="통화 중일 때"):
            run(name, args)
    with pytest.raises(P.Unavailable, match="아직 아이폰 건강 데이터가 없어요"):
        run("phone_health", {"metric": "steps"})  # offline and nothing synced
    with pytest.raises(P.Unavailable, match="생리 주기을\\(를\\) 켜 주시면"):
        run("phone_health", {"metric": "cycle"}, live=phone(error="off"))
    with pytest.raises(P.Unavailable, match="설정 앱에서"):
        run("phone_sleep", {}, live=phone(error="denied"))
    with pytest.raises(ValueError):
        run("phone_contacts", {"name": "  "})


# ---------- the registry and the gateway ----------
def test_registry_entry(tgw):  # noqa: F811
    g, c, _ = tgw
    TB = g.tools_bridge
    for name in P.NAMES:
        meta = TB.ACTIONS[name]
        assert meta["read_only"] and "external" not in meta["scopes"] and "app" not in meta["scopes"]
    r = asyncio.run(g.bridge.run("phone_location", {}, caller="qwen"))
    assert r["error"]["code"] == "unavailable" and "통화 중일 때" in r["spoken"]  # no voice link in tests
    r = asyncio.run(g.bridge.run("phone_health", {"metric": "mood"}, caller="api"))
    assert r["error"]["code"] == "invalid"
    r = asyncio.run(g.bridge.run("phone_health", {"metric": "steps"}, caller="app"))
    assert r["error"]["code"] == "not_found"  # not an app action


def test_days_routes_and_tool(tgw):  # noqa: F811
    g, c, _ = tgw
    today = datetime.now(g.KST).date().isoformat()
    assert c.put("/v1/phone/days", json={"days": [day(today, steps=1234, secret="x")]}).status_code == 400
    assert c.put("/v1/phone/days", json={"days": []}).status_code == 400
    r = c.put("/v1/phone/days", json={"days": [day(today, steps=1234)]})
    assert r.status_code == 200 and r.json()["stored"] == 1
    assert c.get("/v1/phone/days").json()["days"] == [day(today, steps=1234)]
    r = asyncio.run(g.bridge.run("phone_health", {"metric": "steps"}, caller="qwen"))
    assert r["ok"] and r["spoken"].startswith("오늘 걸음 수는 1,234걸음이에요.") and r["data"]["source"] == "sync"
    c.put("/v1/phone/days", json={"days": [day(today, steps=1234, sleep={"asleep_min": 400})]})
    assert c.delete("/v1/phone/days", params={"kind": "mood"}).status_code == 400
    assert c.delete("/v1/phone/days", params={"kind": "health"}).json() == {"deleted": True, "kind": "health"}
    assert c.get("/v1/phone/days").json()["days"] == [day(today, sleep={"asleep_min": 400})]
    c.delete("/v1/phone/days", params={"kind": "sleep"})
    assert c.get("/v1/phone/days").json()["days"] == []  # a day with nothing left goes
    c.put("/v1/phone/days", json={"days": [day(today, steps=1)]})
    assert c.delete("/v1/phone/days").json() == {"deleted": True}
    assert c.get("/v1/phone/days").json()["days"] == []
    assert "phone_health" in {t["name"] for t in tailnet(g).post(
        "/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]}


def test_live_over_the_link(tgw):  # noqa: F811
    g, c, _ = tgw

    class Link:  # the voice server relays to the phone and back
        async def send(self, raw):
            m = json.loads(raw)
            assert m["type"] == "phone_query" and m["kind"] == "location"
            g.phone_pending[m["id"]].set_result({"id": m["id"], "type": "phone_result", "ok": True,
                                                 "data": {"place": "광주광역시 북구"}})
    g.link["ws"] = Link()
    try:
        r = asyncio.run(g.bridge.run("phone_location", {}, caller="qwen"))
    finally:
        g.link["ws"] = None
    assert r["ok"] and r["spoken"] == "지금 광주광역시 북구 근처예요." and not g.phone_pending
