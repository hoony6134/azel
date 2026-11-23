"""GET /v1/tv: the Roku dashboard's read-only JSON and the separate token that opens only it."""
import asyncio
import sys

import pytest
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
    nodes, asof = tv.parse_hosts(HEALTH, NOW)
    assert asof == "13:27"
    assert nodes[0] == {"name": "albireo", "group": "cygnus", "status": "ok", "up": True, "seen": "2026-11-22T13:27:00+09:00",
                        "cpu": 0, "mem": 69, "disk": 15, "temp": None, "load": "0.05/2", "uptime": "142d"}
    assert nodes[1]["temp"] == 74 and nodes[2]["up"] is False and nodes[2]["status"] == "off"
    assert nodes[2]["cpu"] is None and nodes[2]["seen"] is None
    assert tv.parse_down(STATUS) == [{"service": "Vuja De claw (NemoClaw)", "node": "vd-spark", "group": "Vuja De GPU",
                                      "type": "http", "state": "down", "note": "24h 0.00%", "since": None}]
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
    assert out["infra"]["hosts"][1]["name"] == "fawaris" and out["infra"]["asof"] == "13:27" and out["infra"]["down"][0]["node"] == "vd-spark"
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


def test_tv_route_versions(gw, monkeypatch):
    g, c, tmp = gw

    async def offline():  # never the network in tests: weather and quotes have their own recorded-response tests
        raise RuntimeError("offline")

    monkeypatch.setattr(g, "tv_weather", offline)
    monkeypatch.setattr(g, "tv_holdings", offline)
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


# ---- the text the gateway's tools really return (same shapes as the live output, invented names) ----
LIVE_HEALTH = """As of 2026-11-22 17:04 KST (cpu = 5-min avg, disk = /)
정상 albireo [cygnus] cpu 1% · mem 69% · disk 15% · load 0.08/2 · uptime 142d
주의 fawaris [cygnus] cpu 3% · mem 49% · disk 91% · 67°C · load 0.07/4 · uptime 24d
문제 sadr [cygnus] DOWN
정상 vd-spark [vujade] cpu 2% · mem 78% · disk 19% · 42°C · load 0.30/20 · uptime 23h
(maintenance window until 18:00)"""
LIVE_STATUS = """# Cygnus · Ops (internal)
## vd-spark · Vuja De GPU
  문제 Vuja De claw (NemoClaw) (http) 멈춤 · 24h 0.00%
  주의 searx (http) 느려짐 · 24h 97.10%"""
LIVE_TODAY = """스냅샷: 11월 22일 (일) 15:41

## 2026-11-22 기준

### 수업
없음

### 7일 안 마감
- 11월 23일 (월) 23:59 · 하우스 문서 — 문서 부착 공지
- 11월 23일 (월) 23:59 · 할 일 — 컴파일러 프로젝트

### 복습할 단원
- 현대 예술의 이해 · 팝아트 (새로 배움) — 예정 10월 28일 (수)
- 프로그래밍 언어 및 컴파일러 · Lexical Analysis (Token, Regex) (익히는 중) — 예정 11월 1일 (일)

### LMS 알림 (최근 7일)
- 11월 22일 (일) 현대 예술의 이해[01] · 강의 개요 — 새 토론글이 등록되었습니다.
알 수 없는 줄 하나"""
LIVE_ASSIGN = """스냅샷: 11월 22일 (일) 15:41

## 남은 항목 (3)
- ⬜ 11월 23일 (월) 23:59 · 하우스 문서 — 문서 부착 공지 [할 일]
- ⬜ 11월 23일 (월) 23:59 · 컴파일러 프로젝트 [할 일]
- ⬜ 마감 없음 · 대외부 — 대외부장 인수인계 기록지 작성 [할 일]"""
LATER = 1_795_300_000  # 2026-11-22 ~15:00 KST


def test_hosts_in_the_gateways_spoken_words_and_in_groups():
    hosts, asof = tv.parse_hosts(LIVE_HEALTH, LATER)
    assert asof == "17:04" and [h["name"] for h in hosts] == ["albireo", "fawaris", "sadr", "vd-spark"]
    assert [h["status"] for h in hosts] == ["ok", "warn", "off", "ok"] and [h["group"] for h in hosts][-1] == "vujade"
    assert hosts[1]["disk"] == 91 and hosts[1]["temp"] == 67 and hosts[3]["uptime"] == "23h" and hosts[3]["load"] == "0.30/20"
    assert tv.parse_notes(LIVE_HEALTH) == ["(maintenance window until 18:00)"]  # an unknown line is kept, as text


def test_services_and_alerts_headers_are_labels_not_alerts():
    down = tv.parse_down(LIVE_STATUS)
    assert [(d["service"], d["state"], d["node"], d["group"], d["note"]) for d in down] == [
        ("Vuja De claw (NemoClaw)", "down", "vd-spark", "Vuja De GPU", "24h 0.00%"),
        ("searx", "degraded", "vd-spark", "Vuja De GPU", "24h 97.10%")]
    assert tv.parse_alerts("# Cygnus · Ops (internal)\n## vd-spark · Vuja De GPU\n지금 울리는 경보 없음.") == []
    a = tv.parse_alerts("## vd-spark · Vuja De GPU\nspark 디스크 91% (warning)")
    assert len(a) == 1 and a[0]["group"] == "vd-spark" and a[0]["head"] == "디스크 부족"


def test_gistra_sections_items_and_empty_headers():
    snap, secs = tv.parse_sections(LIVE_TODAY, LATER)
    assert snap == "15:41"
    assert [(s["title"], len(s["items"])) for s in secs] == [("수업", 0), ("7일 안 마감", 2), ("복습할 단원", 2),
                                                          ("LMS 알림 (최근 7일)", 1 + 1)]  # 수업 is empty: no card
    d = secs[1]["items"][0]
    assert (d["when"], d["kind"], d["title"], d["due"][:16]) == ("11/23 23:59", "하우스 문서", "문서 부착 공지", "2026-11-23T23:59")
    r = secs[2]["items"][1]
    assert (r["kind"], r["title"], r["note"]) == ("프로그래밍 언어 및 컴파일러", "Lexical Analysis (Token, Regex)", "익히는 중 · 예정 11월 1일 (일)")
    lms = secs[3]["items"][0]
    assert (lms["when"], lms["kind"], lms["title"], lms["note"]) == ("11/22", "현대 예술의 이해[01]", "강의 개요", "새 토론글이 등록되었습니다.")
    assert secs[3]["items"][1]["title"] == "알 수 없는 줄 하나"  # never dropped
    snap2, left = tv.parse_sections(LIVE_ASSIGN, LATER)
    assert snap2 == "15:41" and [s["title"] for s in left] == ["남은 항목"] and len(left[0]["items"]) == 3
    assert left[0]["items"][2]["when"] == "마감 없음" and left[0]["items"][2]["due"] is None


def test_next_deadline_from_gistra_or_the_calendar():
    gistra = {"assignments": [], "todos": [{"text": "x", "title": "컴파일러 프로젝트", "kind": "", "due": "2026-11-23T23:59:00+09:00"}],
              "sections": []}
    nd = tv.next_deadline(gistra, [], LATER, "2026-11-22")
    assert nd["title"] == "컴파일러 프로젝트" and nd["source"] == "gistra"
    cal = [{"start": "19:30", "end": "19:45", "what": "토론지 LMS 업로드 확인 (마감 23:00)", "cal": "미리알림", "where": ""}]
    assert tv.next_deadline(gistra, cal, LATER, "2026-11-22")["source"] == "calendar"  # tonight is earlier than tomorrow
    assert tv.next_deadline(None, None, LATER, "2026-11-22") is None


def test_build2_gives_hosts_sections_and_next_deadline():
    async def raw(n, a=None):
        return {"infra_health": LIVE_HEALTH, "infra_status": LIVE_STATUS, "infra_alerts": "지금 울리는 경보 없음.",
                "gistra_assignments": LIVE_ASSIGN, "gistra_today": LIVE_TODAY}[n]

    out = asyncio.run(tv.build2(deps2(infra_raw=raw), LATER))
    assert out["infra"]["asof"] == "17:04" and len(out["infra"]["hosts"]) == 4 and out["infra"]["alerts"] == []
    g = out["gistra"]
    assert g["asof"] == "15:41" and [s["title"] for s in g["sections"]] == ["7일 안 마감", "복습할 단원", "LMS 알림 (최근 7일)", "남은 항목"]
    assert out["next_deadline"]["title"] and out["next_deadline"]["due"].startswith("2026-11-2")


# ---------- real captured text (tests/fixtures/tv_*), 2026-11-23 ----------
FIX = Path(__file__).resolve().parent / "fixtures"


def test_activity_line_loses_its_emoji_and_keeps_the_time_as_when():
    # captured gistra_today `### 활동 일정`: the 🔁 has no glyph in the TV font and showed as an indent
    line = "- 🔁 20:00~22:00 지온나래 · 지온나래 정기회의 (정기 회의) · 구관 토론실"
    it = tv.parse_item(line, NOW)
    assert it["when"] == "20:00~22:00" and it["kind"] == "지온나래"
    assert it["title"] == "지온나래 정기회의 (정기 회의)" and it["note"] == "구관 토론실"
    snap, secs = tv.parse_sections("### 활동 일정\n" + line + "\n", NOW)
    assert secs[0]["items"][0]["kind"] == "지온나래"


def test_no_display_field_starts_with_a_symbol():
    lines_ = ["- ⬜ 11월 23일 (월) 23:59 · 하우스 문서 — 문서 부착 공지 [할 일]", "- ✅ 마감 없음 · 대외부 — 인수인계 기록지 작성 [할 일]",
              "- 🔁 20:00~22:00 지온나래 · 지온나래 정기회의 (정기 회의) · 구관 토론실"]
    for line in lines_:
        it = tv.parse_item(line, NOW)
        for k in ("when", "kind", "title", "note"):
            assert it[k] is None or (it[k] == it[k].strip() and not it[k].startswith(("⬜", "✅", "🔁", "\uFE0F"))), (k, it[k])
    assert tv.plain(" 🔁  정기 회의 ") == "정기 회의" and tv.plain("69°C · 20:00~22:00") == "69°C · 20:00~22:00"
    assert tv.calendar_events([{"start": "09:00", "title": "🎉 생일", "cal": "MyTT"}])[0]["what"] == "생일"


def test_monitors_parse_the_whole_status_page():
    groups = tv.parse_monitors((FIX / "tv_infra_status.txt").read_text())
    assert [g["node"] for g in groups][:3] == ["deneb", "sadr", "albireo"] and groups[-1]["node"] == "external"
    deneb = groups[0]
    assert deneb["role"] == "Public Edge · Identity" and len(deneb["services"]) == 9
    assert deneb["services"][0] == {"name": "Nginx Proxy Manager", "type": "http", "state": "up", "uptime": 100.0,
                                    "ms": 15, "last_down": None}
    restic = deneb["services"][1]
    assert restic["type"] == "push" and restic["ms"] is None and restic["last_down"] == "2026-10-31 05:00"
    assert deneb["services"][8]["name"] == "ntfy-relay · deneb (Kuma 알림 경로)"  # a name with its own ` · ` and parens
    claw = next(s for g in groups for s in g["services"] if s["name"].startswith("Vuja De claw"))
    assert claw == {"name": "Vuja De claw (NemoClaw)", "type": "http", "state": "down", "uptime": 2.85, "ms": None,
                    "last_down": None}
    assert all(s["state"] != "unknown" for g in groups for s in g["services"])  # every real line understood
    assert sum(len(g["services"]) for g in groups) == 51  # every ✓/✗ line of the capture


def test_infra_detail_joins_hosts_and_their_services():
    health, status = (FIX / "tv_infra_health.txt").read_text(), (FIX / "tv_infra_status.txt").read_text()
    asked = []

    async def raw(n, extra=None):
        asked.append((n, extra))
        return {"infra_health": health, "infra_status": status, "infra_alerts": "No firing alerts."}[n]

    out = asyncio.run(tv.build_infra_detail({"infra_ok": lambda: True, "infra_raw": raw}, NOW))
    assert ("infra_status", {"problems_only": False}) in asked  # the full page, not only problems
    spark = next(h for h in out["hosts"] if h["name"] == "vd-spark")
    assert spark["role"] == "Vuja De GPU" and spark["services"] == 4 and spark["services_down"] == 1
    assert spark["temp"] == 62 and spark["status"] == "ok" and out["asof"] == "03:15" and out["alerts"] == []
    assert len(out["hosts"]) == 8 and len(out["groups"]) == 9  # 8 nodes + external sites
    off = asyncio.run(tv.build_infra_detail({"infra_ok": lambda: False}, NOW))
    assert off["hosts"] is None and off["groups"] is None


def test_tasks_detail_from_real_rows():
    import json
    rows = json.loads((FIX / "tv_tasks.json").read_text())
    running = {"goal": "🔁 스파크 로그 정리", "status": "running", "device": "spark", "backend": "claude_api",
               "started": NOW - 60, "steps": [{"at": NOW - 5, "kind": "tool", "tool": "spark__run_command",
                                                "label": "spark에서 명령 실행 중"}], "brief": "SECRET-BRIEF"}

    async def tasks(n):
        return [running, *rows]

    async def approvals():
        return [{"id": "x", "command": "rm -rf /secret"}]

    out = asyncio.run(tv.build_tasks_detail({"tasks": tasks, "approvals": approvals}, NOW))
    a = out["active"][0]
    assert a["goal"] == "스파크 로그 정리" and a["engine"] == "Claude" and a["device"] == "spark"
    assert a["step"] == "spark에서 명령 실행 중" and a["started"].endswith("+09:00")
    assert len(out["recent"]) == 10 and out["approvals"] == 1
    first = out["recent"][0]
    assert first["status"] == "done" and first["result"] == "대표님, 스파크에서 네모클로 서비스 재시작을 마쳤고 정상으로 확인했습니다."
    cancelled = next(r for r in out["recent"] if r["status"] == "cancelled")
    assert cancelled["result"] == "취소됐어요."  # a failure shows its error, not the half-written result
    failed = next(r for r in out["recent"] if r["status"] == "failed")
    assert failed["result"].startswith("기기 작업이 권한 때문에 막혀서")
    text = str(out)
    assert "rm -rf" not in text and "SECRET-BRIEF" not in text and "`" not in text and "**" not in text


def test_detail_query_is_open_to_the_tv_token(gw, monkeypatch):
    g, c, tmp = gw

    async def fake(d, now=None):
        return {"ok": "tasks"}

    monkeypatch.setattr(g.tv, "build_tasks_detail", fake)
    g.tv_detail_cache["tasks"] = g.tv.Cache()
    path = tmp / "tv_token"
    path.write_text("t" * 40)
    path.chmod(0o600)
    tvh = {"Authorization": "Bearer " + "t" * 40}
    r = c.get("/v1/tv?detail=tasks", headers=tvh)
    assert r.status_code == 200 and r.json() == {"ok": "tasks"}
    assert c.get("/v1/tv?detail=secrets", headers=tvh).status_code == 400
    assert c.get("/v1/tasks", headers=tvh).status_code == 401  # still only the TV path


# ---------- weather, holdings, history: recorded responses (tests/fixtures/tv_*), never the network ----------
def test_weather_card_from_recorded_kma():
    import json
    from datetime import datetime
    rec = json.loads((FIX / "tv_kma.json").read_text())
    now = datetime.fromisoformat(rec["now"]).timestamp()
    w = tv.weather_card({"source": "기상청", "place": "광주", **rec}, now)
    t1h = float(next(i["obsrValue"] for i in rec["ncst"] if i["category"] == "T1H"))
    assert w["source"] == "기상청" and w["now"] == t1h and w["place"] == "광주"
    assert w["low"] <= t1h <= w["high"] and 0 <= w["pop"] <= 100 and w["sky"]
    assert len(w["hourly"]) == 12 and w["hourly"][0]["h"] == rec["now"][11:13]  # from this hour on
    assert all(isinstance(h["t"], float) and 0 <= h["pop"] <= 100 for h in w["hourly"])
    assert not w["sky"].endswith("요")  # a TV word, not a spoken sentence
    assert w["icon"] in tv.KMA_ICON_SKY.values() or w["icon"] in tv.KMA_ICON_PTY.values()
    assert all(h["icon"] in ("clear", "cloudy", "overcast", "rain", "snow", "sleet", "shower") for h in w["hourly"])
    assert tv.kma_icon("4", "1") == "shower" and tv.kma_icon("0", "3") == "cloudy" and tv.wmo_icon(73) == "snow"


def test_weather_card_falls_back_to_recorded_open_meteo():
    import json
    d = json.loads((FIX / "tv_openmeteo.json").read_text())
    w = tv.weather_card({"source": "Open-Meteo", "place": "광주", **d}, NOW)
    assert w["now"] == 1.7 and w["sky"] == "맑음" and w["icon"] == "clear" and w["hourly"] == []
    assert w["high"] == d["daily"]["temperature_2m_max"][0] and w["low"] == d["daily"]["temperature_2m_min"][0]
    assert tv.weather_card(None, NOW) is None


def test_history_from_recorded_infra_query():
    cpu = tv.parse_history((FIX / "tv_history_cpu.txt").read_text())
    mem = tv.parse_history((FIX / "tv_history_mem.txt").read_text())
    assert set(cpu) == set(mem) and len(cpu) == 8 and "vd-spark" in cpu
    assert all(len(v) == 8 and None not in v and all(0 <= x <= 100 for x in v) for v in [*cpu.values(), *mem.values()])
    assert mem["deneb"][0] == 71  # `{ago="21",node="deneb"} 71`: the oldest point comes first
    q = tv.history_query("mem")
    assert len(q) <= 2000 and q.count("offset") == 14 and '"ago", "0"' in q  # infra_query's limit; 7 offsets × 2 selectors


def test_infra_detail_carries_24h_history():
    health, status = (FIX / "tv_infra_health.txt").read_text(), (FIX / "tv_infra_status.txt").read_text()
    cpu, mem = (FIX / "tv_history_cpu.txt").read_text(), (FIX / "tv_history_mem.txt").read_text()

    async def raw(n, extra=None):
        return {"infra_health": health, "infra_status": status, "infra_alerts": "No firing alerts."}[n]

    async def query(q):
        return mem if "MemAvailable" in q else cpu

    out = asyncio.run(tv.build_infra_detail({"infra_ok": lambda: True, "infra_raw": raw, "query": query}, NOW))
    deneb = next(h for h in out["hosts"] if h["name"] == "deneb")
    assert len(deneb["cpu_24h"]) == 8 and deneb["mem_24h"][0] == 71


def quote_transport():
    """Naver/Yahoo answered from tests/fixtures/tv_quotes.json; Naver's search is unreachable (as it was when recorded)
    and Yahoo's search answers 400 to a Korean query (recorded)."""
    import json
    import httpx
    rec = json.loads((FIX / "tv_quotes.json").read_text())

    def handle(req):
        url = str(req.url)
        if "ac.stock.naver.com" in url:
            raise httpx.ConnectTimeout("timed out", request=req)
        if "finance/search" in url:
            return httpx.Response(400, json={"finance": {"error": {"code": "Bad Request"}}})
        if "polling.finance.naver.com" in url:
            return httpx.Response(200, json=rec[f"naver:{url.rsplit('/', 1)[1]}"])
        sym, rng = url.split("/chart/")[1].split("?")[0], req.url.params["range"]
        key = f"chart:{sym}:{rng}"
        return httpx.Response(200, json=rec[key]) if key in rec else httpx.Response(404, json={"chart": {"result": None}})
    return httpx.MockTransport(handle)


def test_holdings_from_recorded_quotes():
    import httpx
    import quote

    async def run():
        async with httpx.AsyncClient(transport=quote_transport()) as c:
            return [await quote.holding(n, c) for n in ("삼성전자 (005930)", "애플 (AAPL)", "토스", "SK하이닉스 (000660)")]

    sam, apple, toss, hynix = asyncio.run(run())
    assert sam["name"] == "삼성전자" and sam["price"] == 180900 and sam["pct"] == -8.13 and sam["currency"] == "KRW"
    assert sam["spark"] == [196800.0, 190300.0, 194700.0, 196900.0, 183100.0] and sam["source"] == "네이버 증권"
    assert apple["name"] == "애플" and apple["ticker"] == "AAPL" and apple["currency"] == "USD" and len(apple["spark"]) == 5
    assert toss == {"name": "토스", "listed": False}  # unlisted: quietly 비상장, no guess
    assert hynix["price"] == 1100000 and hynix["spark"][-1] == 1113000.0


def test_build2_adds_weather_and_holdings():
    async def weather():
        return {"source": "Open-Meteo", "place": "광주", "current": {"temperature_2m": 3.0, "weather_code": 3},
                "daily": {"temperature_2m_max": [9.0], "temperature_2m_min": [-1.0], "precipitation_probability_max": [20]}}

    async def holdings():
        return [{"name": "토스", "listed": False}]

    out = asyncio.run(tv.build2(deps2(weather=weather, holdings=holdings), NOW))
    assert out["weather"]["now"] == 3.0 and out["weather"]["sky"] == "흐림" and out["holdings"] == [{"name": "토스", "listed": False}]
    assert asyncio.run(tv.build2(deps2(), NOW))["weather"] is None  # a gateway without them: null parts


def test_holdings_setting(gw, monkeypatch):
    g, c, _ = gw
    asked = []

    async def resolve(name):
        asked.append(name)
        return {"카카오": {"ticker": "035720", "name": "카카오"}, "토스": None,
                "삼성": {"candidates": ["삼성전자 (005930)", "삼성SDI (006400)"]}}[name]
    monkeypatch.setattr(g, "resolve_holding", resolve)
    assert c.get("/v1/settings").json()["holdings"][2] == "삼성전자 (005930)"
    r = c.put("/v1/settings", json={"holdings": ["  애플   (AAPL) ", "카카오", "토스"]})  # a new bare name is resolved
    assert r.status_code == 200 and c.get("/v1/settings").json()["holdings"] == ["애플 (AAPL)", "카카오 (035720)", "토스"]
    asked.clear()
    r = c.put("/v1/settings", json={"holdings": ["Apple (AAPL)", "카카오 (035720)", "토스"]})  # rename only: no lookup
    assert r.status_code == 200 and r.json()["holdings"] == ["Apple (AAPL)", "카카오 (035720)", "토스"] and asked == []
    r = c.put("/v1/settings", json={"holdings": ["삼성"]})
    assert r.status_code == 400 and "삼성SDI (006400)" in r.json()["detail"]  # several: the app asks which one
    assert c.get("/v1/settings").json()["holdings"][0] == "Apple (AAPL)"  # nothing saved
    for bad in ("애플", [""], ["x" * 41], ["a\nb"], ["a"] * 13):
        assert c.put("/v1/settings", json={"holdings": bad}).status_code == 400, bad


def test_holdings_update_by_voice(gw, monkeypatch):
    g, c, _ = gw
    answers = {"SK하이닉스": {"ticker": "000660", "name": "SK하이닉스"}, "삼성": {"candidates": ["SamsungElec (005930)",
               "SAMSUNG ELECTRONICS CO (SSNLF)"]}, "토스": None, "코스피": {"ticker": "^KS11", "name": "코스피"}}

    async def resolve(name):  # quote.resolve itself is tested on recorded responses above
        return answers[name]

    monkeypatch.setattr(g, "resolve_holding", resolve)
    r = asyncio.run(g.bridge.run("holdings_update", {"add": ["카카오 (035720)"], "remove": ["엔트로픽", "^GSPC"]}, caller="qwen"))
    assert r["ok"], r
    got = c.get("/v1/settings").json()["holdings"]
    assert "카카오 (035720)" in got and not any(x.startswith(("엔트로픽", "S&P500")) for x in got) and len(got) == 9
    assert r["spoken"] == "카카오는 035720으로 넣었어요. 엔트로픽, S&P500을 뺐어요. 지금 9개예요."  # 영으로, 오백을
    r = asyncio.run(g.bridge.run("holdings_update", {"add": ["SK하이닉스", "삼성", "토스", "코스피"]}, caller="qwen"))
    got = c.get("/v1/settings").json()["holdings"]
    assert "SK하이닉스 (000660)" in got and "토스" in got and "코스피 (^KS11)" in got and not any(x.startswith("삼성 ") for x in got)
    assert r["data"]["picked"] == [{"name": "SK하이닉스", "ticker": "000660"}, {"name": "코스피", "ticker": "^KS11"}]
    assert r["data"]["candidates"] == {"삼성": answers["삼성"]["candidates"]} and r["data"]["unlisted"] == ["토스"]
    assert r["spoken"].startswith("SK하이닉스는 000660으로, 코스피는 ^KS11로 넣었어요. 토스는 상장 종목을 못 찾아서 비상장으로")
    assert "삼성은 후보가 여러 개라 넣지 않았어요(SamsungElec (005930), SAMSUNG ELECTRONICS CO (SSNLF))" in r["spoken"]
    before = c.get("/v1/settings").json()["holdings"]
    r = asyncio.run(g.bridge.run("holdings_update", {"add": ["삼성"]}, caller="qwen"))  # only candidates: nothing saved
    assert c.get("/v1/settings").json()["holdings"] == before and "지금" not in r["spoken"]
    assert not asyncio.run(g.bridge.run("holdings_update", {}, caller="qwen"))["ok"]
    big = [f"종목{i} (00000{i % 10})" for i in range(12)]
    assert asyncio.run(g.bridge.run("holdings_update", {"add": big}, caller="qwen"))["error"]["code"] == "invalid"


def test_josa_as_read_aloud():
    import gateway as gw_
    assert gw_.josa("000660", ("으로", "로")) == "000660으로" and gw_.josa("AAPL", ("으로", "로")) == "AAPL로"
    assert gw_.josa("^KS11", ("으로", "로")) == "^KS11로" and gw_.josa("서울", ("으로", "로")) == "서울로"
    assert gw_.josa("SK하이닉스", ("은", "는")) == "SK하이닉스는" and gw_.josa("애플", ("을", "를")) == "애플을"


def test_seed_holdings_cover_the_users_list():
    import gateway
    import quote
    names = [quote.HOLDING.match(h)["name"] for h in gateway.DEFAULTS["holdings"]]
    assert names == ["애플", "엔비디아", "삼성전자", "SK하이닉스", "SOS랩", "엔트로픽", "SK스퀘어", "LS산전", "S&P500", "세트렉아이"]
    assert quote.HOLDING.match("S&P500 (^GSPC)")["tick"] == "^GSPC"


def resolve_transport():
    """Yahoo search and the KOSPI chart from tests/fixtures/tv_resolve.json (recorded); Naver's search unreachable, as
    it was from the gateway host when recorded."""
    import json
    import httpx
    rec = {**json.loads((FIX / "tv_resolve.json").read_text()), **json.loads((FIX / "tv_quotes.json").read_text())}

    def handle(req):
        url = str(req.url)
        if "ac.stock.naver.com" in url:
            raise httpx.ConnectTimeout("timed out", request=req)
        if "finance/search" in url:
            key = f"search:{req.url.params['q']}"
            return httpx.Response(200, json=rec[key]) if key in rec else httpx.Response(400, json={})
        sym = url.split("/chart/")[1].split("?")[0].replace("%5E", "^")
        key = f"chart:{sym}:{req.url.params['range']}"
        return httpx.Response(200, json=rec[key]) if key in rec else httpx.Response(404, json={"chart": {"result": None}})
    return httpx.MockTransport(handle)


def test_resolve_names_to_tickers_without_guessing():
    import httpx
    import quote

    async def run():
        async with httpx.AsyncClient(transport=resolve_transport()) as c:
            return [await quote.resolve(n, c) for n in ("SK하이닉스", "Apple", "Samsung", "Toss", "S&P 500", "에스앤피500",
                                                         "코스피", "나스닥", "다우존스", "알수없는회사")]

    hynix, apple, samsung, toss, sp, sp2, kospi, nasdaq, dow, nobody = asyncio.run(run())
    assert hynix == {"ticker": "000660", "name": "SK하이닉스"}  # the Korean listing, not the 2x ETF Yahoo lists first
    assert apple["ticker"] == "AAPL"  # "Apple" is "Apple Inc."
    assert "candidates" in samsung and samsung["candidates"][0] == "SamsungElec (005930)"  # several: ask, don't guess
    assert toss == {"candidates": ["TOSS THE COIN LIMITED (TTC.BO)"]}  # a different company: asked, never stored
    assert sp["ticker"] == sp2["ticker"] == "^GSPC" and kospi["ticker"] == "^KS11"
    assert nasdaq["ticker"] == "^IXIC" and dow["ticker"] == "^DJI" and nobody is None


def test_index_holding_shows_its_level():
    import httpx
    import quote

    async def run():
        async with httpx.AsyncClient(transport=resolve_transport()) as c:
            return await quote.holding("코스피", c), await quote.holding("KOSPI (^KS11)", c)

    by_name, by_tick = asyncio.run(run())
    assert by_name["ticker"] == by_tick["ticker"] == "^KS11" and by_name["currency"] == "index"
    assert by_name["price"] == 4869.96 and by_name["pct"] is not None and len(by_name["spark"]) == 5


def test_portfolio_tool_reads_the_holdings(gw, monkeypatch):
    import httpx
    import quote
    g, c, _ = gw

    async def rows():  # the TV row, from recorded Naver/Yahoo answers
        async with httpx.AsyncClient(transport=quote_transport()) as cl:
            return [await quote.holding(n, cl) for n in ("삼성전자 (005930)", "애플 (AAPL)", "토스")]

    monkeypatch.setattr(g, "tv_holdings", rows)
    g.tv_quote_cache = g.tv.Cache()
    r = asyncio.run(g.bridge.run("portfolio", {}, caller="qwen"))
    assert r["ok"] and r["spoken"] == "보유 종목 3개 시세예요." and len(r["data"]["holdings"]) == 3
    lines = quote.portfolio_lines(r["data"]["holdings"])
    assert lines[0].startswith("삼성전자 180,900원, 8.13% 내림 (") and lines[2] == "토스 비상장"
    assert lines[1].startswith("애플 342.56달러, ")
    assert quote.portfolio_lines([{"name": "S&P500", "listed": True, "price": 6910.39, "currency": "index", "pct": -3.7,
                                   "at": None}]) == ["S&P500 6,910.39, 3.70% 내림"]


# ---------- the stock page ----------
def yahoo(meta, closes, t0=1_700_000_000, step=300):
    return {"chart": {"result": [{"meta": meta, "timestamp": [t0 + i * step for i in range(len(closes))],
                                  "indicators": {"quote": [{"close": closes}]}}]}}


def stock_deps(calls):
    meta = {"regularMarketPrice": 110.0, "chartPreviousClose": 100.0, "currency": "KRW", "longName": "Samsung",
            "regularMarketDayHigh": 112.0, "regularMarketDayLow": 99.0, "regularMarketVolume": 5000,
            "currentTradingPeriod": {"regular": {"start": 1000, "end": 2000}}}

    async def chart(sym, rng, interval):
        calls.append((sym, rng, interval))
        if sym.endswith(".KS") and sym.startswith("099320"):
            raise KeyError("none")  # a KOSDAQ code: .KS has nothing
        return yahoo(meta, [100.0, None, 105.0, 110.0] * 100 if rng != "1d" else [100.0, None, 105.0, 110.0])
    return {"chart": chart}


def test_stock_1d_points_baseline_and_state():
    calls = []
    r = asyncio.run(tv.build_stock(stock_deps(calls), "005930", "1d", now=1500))
    assert calls == [("005930.KS", "1d", "5m")]
    assert (r["price"], r["prev"], r["change"], r["pct"], r["base"]) == (110.0, 100.0, 10.0, 10.0, 100.0)
    assert (r["state"], r["high"], r["low"], r["volume"], r["index"]) == ("open", 112.0, 99.0, 5000, False)
    assert [p[1] for p in r["points"]] == [100.0, 105.0, 110.0]  # the gap (None) is dropped
    assert asyncio.run(tv.build_stock(stock_deps([]), "005930", "1d", now=5000))["state"] == "closed"


def test_stock_other_ranges_downsample_and_color_against_the_range_start():
    calls = []
    r = asyncio.run(tv.build_stock(stock_deps(calls), "AAPL", "1y"))
    assert [c[1:] for c in calls] == [("1d", "5m"), ("1y", "1d")]
    assert len(r["points"]) == tv.MAX_POINTS and r["points"][-1][1] == 110.0
    assert r["base"] == 100.0 and r["pct"] == 10.0  # the header stays the day's change


def test_stock_kosdaq_code_falls_back_to_kq_and_unknown_raises():
    calls = []
    asyncio.run(tv.build_stock(stock_deps(calls), "099320", "1d"))
    assert [c[0] for c in calls] == ["099320.KS", "099320.KQ"]

    async def none(*a):
        raise KeyError("x")
    with pytest.raises(LookupError):
        asyncio.run(tv.build_stock({"chart": none}, "ZZZZ", "1d"))


def test_stock_index_shows_the_level_only():
    r = asyncio.run(tv.build_stock(stock_deps([]), "^GSPC", "1d"))
    assert r["index"] and r["currency"] == "index" and (r["high"], r["low"], r["volume"]) == (None, None, None)


def test_stock_us_pre_and_post_market():
    meta = {"regularMarketPrice": 1.0, "chartPreviousClose": 1.0, "currentTradingPeriod": {
        "pre": {"start": 0, "end": 10}, "regular": {"start": 10, "end": 20}, "post": {"start": 20, "end": 30}}}
    assert [tv.market_state(meta, t) for t in (5, 15, 25, 40)] == ["pre", "open", "post", "closed"]


def test_key_cache_ttl_per_key():
    n = []

    async def make():
        n.append(1)
        return len(n)
    c = tv.KeyCache(cap=2)
    assert asyncio.run(c.get("a", make, 60)) == asyncio.run(c.get("a", make, 60)) == 1
    assert asyncio.run(c.get("a", make, 0)) == 2
    asyncio.run(c.get("b", make, 60)); asyncio.run(c.get("c", make, 60))
    assert len(c.items) == 2


def test_stock_route_validates_and_serves(gw, monkeypatch):
    g, c, _ = gw
    calls = []
    monkeypatch.setattr(g, "tv_deps", lambda: stock_deps(calls))
    g.tv_stock_cache.items.clear()
    for q in ("symbol=bad%20sym&range=1d", "symbol=AAPL&range=2d", "symbol=&range=1d"):
        assert c.get(f"/v1/tv?detail=stock&{q}").status_code == 400, q
    r = c.get("/v1/tv?detail=stock&symbol=aapl&range=1d")
    assert r.status_code == 200 and r.json()["symbol"] == "AAPL" and r.json()["pct"] == 10.0
    c.get("/v1/tv?detail=stock&symbol=AAPL&range=1d")
    assert len(calls) == 1  # cached

    async def none(*a):
        raise KeyError("x")
    monkeypatch.setattr(g, "tv_deps", lambda: {"chart": none})
    assert c.get("/v1/tv?detail=stock&symbol=ZZZZ&range=7d").status_code == 404
