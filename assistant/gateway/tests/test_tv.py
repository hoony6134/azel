"""GET /v1/tv: the Roku dashboard's read-only JSON and the separate token that opens only it."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tv  # noqa: E402
from test_gateway import gw  # noqa: E402,F401

NOW = 1_800_000_000  # a fixed instant; KST day computed by the module


def deps(**over):
    async def infra(n):
        return {"infra_health": "spark 켜짐\n\nmac 켜짐", "infra_status": "문제 없음", "infra_alerts": "경보 없음",
                "gistra_today": "10시 수업", "gistra_assignments": "과제 2개"}[n]

    async def tasks(n):
        return [{"id": "a", "goal": "보고서 정리", "status": "running", "result": "SECRET-BODY"},
                {"id": "b", "goal": "대기 중", "status": "queued"}, {"id": "c", "goal": "끝난 일", "status": "done"}]

    async def approvals():
        return [{"id": "x", "command": "rm -rf /secret", "status": "pending"}]

    async def fires(now):
        return [({"purpose": "약 먹기", "kind": "reminder"}, now), ({"kind": "wake"}, now - 10 * 86400)]

    async def persona():
        return "아젤"

    async def calendar(today):
        return "14:00 회의"

    d = {"infra": infra, "infra_ok": lambda: True, "tasks": tasks, "approvals": approvals, "fires": fires,
         "persona": persona, "in_call": lambda: False, "google_ok": lambda: True, "calendar": calendar}
    return {**d, **over}


def test_build_is_compact_and_read_only():
    out = asyncio.run(tv.build(deps(), NOW))
    assert out["infra"] == {"health": ["spark 켜짐", "mac 켜짐"], "problems": ["문제 없음"], "alerts": ["경보 없음"]}
    assert out["tasks"]["running"] == [{"goal": "보고서 정리", "status": "running"}] and out["tasks"]["queued"] == 1
    assert out["tasks"]["approvals"] == 1 and out["tasks"]["recent"] == [{"goal": "끝난 일", "status": "done"}]
    assert out["todo"] == {"today": ["10시 수업"], "assignments": ["과제 2개"]}
    assert [c["what"] for c in out["calls"]] == ["약 먹기"]  # only today's
    assert out["calendar"] == ["14:00 회의"] and out["assistant"] == {"name": "아젤", "state": "idle"}
    text = str(out)
    assert "rm -rf" not in text and "SECRET-BODY" not in text  # no command, no result body


def test_one_failing_source_only_blanks_its_own_part():
    async def boom(n):
        raise RuntimeError("down")

    out = asyncio.run(tv.build(deps(infra=boom, in_call=lambda: True, google_ok=lambda: False), NOW))
    assert out["infra"] is None and out["todo"] is None and out["calendar"] is None
    assert out["tasks"]["queued"] == 1 and out["assistant"]["state"] == "call"


def test_cache_holds_for_ttl():
    n = []

    async def make():
        n.append(1)
        return {"n": len(n)}

    c = tv.Cache()
    assert asyncio.run(c.get(make)) == asyncio.run(c.get(make)) == {"n": 1}
    assert asyncio.run(c.get(make, ttl=0)) == {"n": 2}


def test_tv_token_opens_only_the_tv_route(gw):
    g, c, tmp = gw
    path = tmp / "tv_token"
    path.write_text("t" * 40)
    path.chmod(0o600)
    tvh = {"Authorization": "Bearer " + "t" * 40}
    assert c.get("/v1/tv", headers=tvh).status_code == 200
    for method, route in (("get", "/v1/settings"), ("get", "/v1/tasks"), ("get", "/v1/approvals"), ("post", "/v1/tv")):
        assert getattr(c, method)(route, headers=tvh).status_code in (401, 405), route
    path.chmod(0o644)  # readable by others: ignored
    assert c.get("/v1/tv", headers=tvh).status_code == 401
    path.chmod(0o600)
    path.write_text("short")  # too short: ignored
    assert c.get("/v1/tv", headers={"Authorization": "Bearer short"}).status_code == 401
    assert c.get("/v1/tv", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/v1/tv").json().keys() >= {"at", "infra", "tasks", "calls", "assistant"}  # the full token works too


HEALTH = """As of 2026-11-22 13:27 KST (cpu = 5-min avg, disk = /)
✓ albireo [cygnus] cpu 0% · mem 69% · disk 15% · load 0.05/2 · uptime 142d
✓ fawaris [cygnus] cpu 2% · mem 50% · disk 29% · 74°C · load 0.14/4 · uptime 23d
✗ gienah [cygnus] DOWN"""
STATUS = """# Cygnus · Ops (internal)
## vd-spark · Vuja De GPU
  ✗ Vuja De claw (NemoClaw) (http) DOWN · 24h 0.00%"""
ASSIGN = """스냅샷: 11월 22일 (일) 11:08

## 남은 항목 (7)
- ⬜ 11월 22일 (일) 23:00 · 역사학특강 — 12주차 토론지 제출 [과제]
- ⬜ 11월 23일 (월) 23:59 · 컴파일러 프로젝트 [할 일]
- ⬜ 11월 23일 (월) 23:59 · 과기사관 — 공결 확인서 전송 메일 [할 일]
- ⬜ 마감 없음 · 대외부 — 대외부장 인수인계 기록지 작성 [할 일]"""
TODAY = """## 2026-11-22 기준

### 수업
- 13:00~14:15 · 자료구조 · 대학 A동 212호

### 7일 안 마감
- 11월 22일 (일) 23:00 · 역사학특강 — 12주차 토론지 제출"""
CAL = """캘린더 2026-11-22~2026-11-22 (KST)
- 11/22(Sun) 19:00~20:00 연구실 회의 @ 대학 B동
- 2026-11-22 종일 듀오링고"""
SUNDAY = 1_795_000_000  # any instant: only the year logic uses it


def test_v2_parsers_on_real_cygnus_text():
    nodes = tv.parse_nodes(HEALTH, NOW)
    assert nodes[0] == {"name": "albireo", "up": True, "seen": "2026-11-22T13:27:00+09:00", "cpu": 0, "ram": 69, "disk": 15, "temp": None}
    assert nodes[1]["temp"] == 74 and nodes[2]["up"] is False and nodes[2]["cpu"] is None and nodes[2]["seen"] is None
    assert tv.parse_down(STATUS) == [{"service": "Vuja De claw (NemoClaw)", "node": "vd-spark", "since": None}]
    assert tv.parse_alerts("No firing alerts.") == []
    a = tv.parse_alerts("deneb disk 91% (warning)\nCRITICAL: gienah down")
    assert [x["level"] for x in a] == ["warn", "crit"] and [x["head"] for x in a] == ["디스크 부족", "응답 없음"]
    assign, todos = tv.parse_assignments(ASSIGN, NOW)
    assert assign == [{"title": "12주차 토론지 제출", "course": "역사학특강", "due": assign[0]["due"]}]
    assert assign[0]["due"].endswith("-11-22T23:00:00+09:00")
    assert [t["text"] for t in todos] == ["컴파일러 프로젝트", "과기사관 — 공결 확인서 전송 메일", "대외부 — 대외부장 인수인계 기록지 작성"]
    assert todos[0]["due"].endswith("-11-23T23:59:00+09:00") and todos[2]["due"] is None
    assert tv.parse_classes(TODAY) == [{"start": "13:00", "end": "14:15", "name": "자료구조", "room": "대학 A동 212호"}]
    assert tv.parse_classes("### 수업\n없음\n") == []
    assert tv.parse_calendar(CAL) == [{"start": "19:00", "end": "20:00", "what": "연구실 회의"},
                                      {"start": "종일", "end": None, "what": "듀오링고"}]


def deps2(**over):
    async def raw(n, a=None):
        return {"infra_health": HEALTH, "infra_status": STATUS, "infra_alerts": "No firing alerts.",
                "gistra_assignments": ASSIGN, "gistra_today": TODAY}[n]

    async def persona2():
        return {"name": "세레나", "tint": "plum"}

    async def overnight():
        return {"count": 2, "starts": "01:00"}

    async def tasks(n):
        return [{"goal": "보고서", "status": "running", "started": NOW - 600, "result": "SECRET-BODY"},
                {"goal": "다음 일", "status": "queued"}]

    async def approvals():
        return [{"id": "x", "command": "rm -rf /secret", "created": NOW - 900}]

    return {**deps(tasks=tasks, approvals=approvals), "infra_raw": raw, "persona2": persona2, "overnight": overnight, **over}


def test_build2_shape_and_no_secrets():
    out = asyncio.run(tv.build2(deps2(), NOW))
    assert out["assistant"] == {"state": "idle", "persona": {"name": "세레나", "tint": "plum"}}
    assert out["infra"]["nodes"][1]["name"] == "fawaris" and out["infra"]["down"][0]["node"] == "vd-spark"
    t = out["tasks"]
    assert t["running"][0]["since"] and t["queued"] == {"count": 1, "next": "다음 일"}
    assert t["approvals"] == {"count": 1, "oldest": t["approvals"]["oldest"]} and t["approvals"]["oldest"]
    assert t["overnight"] == {"count": 2, "starts": "01:00"}
    assert out["gistra"]["classes"][0]["name"] == "자료구조" and out["gistra"]["assignments"][0]["course"] == "역사학특강"
    assert out["calendar"] == []  # the fake calendar text is not in the real line format
    assert "rm -rf" not in str(out) and "SECRET-BODY" not in str(out)


def test_build2_a_failing_part_is_null():
    async def boom(n, a=None):
        raise RuntimeError("down")

    out = asyncio.run(tv.build2(deps2(infra_raw=boom, google_ok=lambda: False), NOW))
    assert out["infra"] is None and out["gistra"] is None and out["calendar"] is None and out["tasks"]["queued"]["count"] == 1


def test_tv_route_versions(gw):
    g, c, tmp = gw
    assert "tasks" in c.get("/v1/tv").json() and "gistra" not in c.get("/v1/tv").json()  # v1 is unchanged
    v2 = c.get("/v1/tv?v=2")
    assert v2.status_code == 200 and v2.json().keys() >= {"at", "infra", "tasks", "gistra", "calls", "calendar", "assistant"}
    assert v2.json()["assistant"]["persona"]["tint"]
    assert c.get("/v1/tv?v=3").status_code == 400
    path = tmp / "tv_token"
    path.write_text("t" * 40)
    path.chmod(0o600)
    assert c.get("/v1/tv?v=2", headers={"Authorization": "Bearer " + "t" * 40}).status_code == 200


def test_calendar_events_from_every_calendar_myTT_first():
    rows = [{"day": None, "start": "19:00", "end": "20:00", "title": "연구실 회의", "where": "B동", "cal": "일반"},
            {"day": None, "start": "13:00", "end": "14:15", "title": "자료구조", "where": "A동 212호", "cal": "MyTT"},
            {"day": None, "start": "13:00", "end": "14:15", "title": "자료구조", "where": "", "cal": "강의"},  # same one, twice
            {"day": None, "start": "", "end": "", "title": "12주차 토론지", "where": "", "cal": "GISTRA"},  # all-day
            {"day": None, "start": "09:00", "end": "10:00", "title": "lms feed", "where": "", "cal": "https://lms.gist.ac.kr/x"}]
    out = tv.calendar_events(rows)
    assert [(e["start"], e["what"], e["cal"], e["where"]) for e in out] == [
        ("13:00", "자료구조", "MyTT", "A동 212호"), ("19:00", "연구실 회의", "일반", "B동")]


def test_build2_uses_the_structured_calendar_when_there_is():
    async def rows(today):
        return [{"start": "21:00", "end": "22:00", "title": "스터디", "where": "", "cal": "공부"}]

    out = asyncio.run(tv.build2(deps2(calendar_rows=rows), NOW))
    assert out["calendar"] == [{"start": "21:00", "end": "22:00", "what": "스터디", "cal": "공부", "where": ""}]


def test_tv_deps_infra_calls_cygnus_with_args(monkeypatch):
    """tv.py asks d["infra"](name); cygnus.call takes (name, args). A one-arg fake once hid the TypeError (empty infra)."""
    import cygnus
    import gateway
    seen = []

    async def fake_call(name, args):
        seen.append((name, args))
        return "ok"

    monkeypatch.setattr(cygnus, "call", fake_call)
    assert asyncio.run(gateway.tv_deps()["infra"]("infra_health")) == "ok" and seen == [("infra_health", {})]
