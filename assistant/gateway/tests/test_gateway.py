import asyncio
import importlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import fake_state  # noqa: E402


def start(tmp_path, monkeypatch, **env):
    """Load a fresh gateway module wired to an in-memory fake of ruchba's state API."""
    (tmp_path / "token").write_text("t0k\n")
    monkeypatch.setenv("BISEO_HOME", str(tmp_path))
    monkeypatch.setenv("BISEO_CLAUDE", str(HERE / "fake_claude.py"))
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(tmp_path / "args.json"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("BISEO_LLM_URL", "http://127.0.0.1:9/v1/chat/completions")  # never the real Qwen in tests
    monkeypatch.setenv("BISEO_SEARXNG_URL", "http://127.0.0.1:9/search")  # nor the real SearXNG
    monkeypatch.setenv("BISEO_WARM_CLAUDE", "0")  # the one-shot fake; test_warm_claude covers the warm path
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.path.insert(0, str(HERE.parent))
    import gateway
    g = importlib.reload(gateway)
    # most tests were written for "every command asks": keep that here; test_approval_policy covers the auto default
    g.DEFAULTS.update(approval_mode="ask", task_auto_approve_default="ask")
    g.fake = fake_state.make()
    g.state.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=g.fake), base_url="http://state")

    async def no_link():  # tests drive the link by hand
        return None
    g.voice_link = no_link

    async def no_scheduler():  # tests drive scheduler_tick / fire_scheduled by hand, at fixed times; a background
        return None             # loop on the real clock would mark those items late ("missed") behind their back
    g.scheduler = no_scheduler

    async def discovered():  # what `claude mcp list` / `claude plugin list` would report
        g.discovery.connectors = [{"name": "Cygnus MCP", "url": "https://mcp.example/cygnus", "status": "connected"},
                                  {"name": "Gmail", "url": "https://gmail.example/mcp", "status": "connected"},
                                  {"name": "Google Calendar", "url": "https://cal.example/mcp", "status": "connected"},
                                  {"name": "Notion", "url": "https://notion.example/mcp", "status": "connected"},
                                  {"name": "Slack", "url": "https://slack.example/mcp", "status": "needs_auth"}]
        g.discovery.plugins, g.discovery.at = ["figma@official", "legal@x"], 1.0
    g.discovery.refresh = discovered
    return g


@pytest.fixture
def gw(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch, BISEO_DEVICE="macbook")
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        yield g, c, tmp_path


def events(resp):
    return [json.loads(line) for line in resp.text.splitlines()]


def test_token_required(gw):
    g, c, _ = gw
    assert c.get("/v1/settings", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/v1/settings", headers={"Authorization": ""}).status_code == 401


def test_ask_streams_and_records_usage(gw):
    g, c, tmp = gw
    ev = events(c.post("/v1/ask", json={"text": "서버 괜찮아?", "session": "s1", "model": "opus"}))
    assert "".join(e["delta"] for e in ev if e["type"] == "text") == "네, 확인했어요."
    assert ev[-1]["type"] == "done" and ev[-1]["path"] == "claude_code" and ev[-1]["model"] == "opus"
    args = json.loads((tmp / "args.json").read_text())
    assert args[args.index("--model") + 1] == "claude-opus-5-5"
    assert "--resume" not in args
    c.post("/v1/ask", json={"text": "또", "session": "s1"})  # same voice session resumes
    args = json.loads((tmp / "args.json").read_text())
    assert args[args.index("--resume") + 1] == "sess-1"
    assert c.get("/v1/usage").json()["cost_usd"] == 0  # claude_code is the subscription: not counted
    assert c.get("/v1/usage").json()["subscription"]["claude_code"]["equivalent_usd"] == 1.0


def test_budget_blocks(gw):
    g, c, _ = gw
    c.put("/v1/settings", json={"monthly_budget_usd": 0.4, "budget_paths": ["claude_code"]})
    c.post("/v1/ask", json={"text": "a"})
    ev = events(c.post("/v1/ask", json={"text": "b"}))
    assert ev == [{"type": "error", "code": "budget", "message": "이번 달 유료 사용 한도를 다 썼습니다."}]


def test_budget_ignores_the_subscription_by_default(gw):
    g, c, _ = gw
    assert c.get("/v1/settings").json()["budget_paths"] == ["api", "nim"]
    c.put("/v1/settings", json={"monthly_budget_usd": 0.4})
    for _ in range(3):  # 1.5 equivalent USD of claude_code: over the budget, yet never blocked
        ev = events(c.post("/v1/ask", json={"text": "a"}))
        assert ev[-1]["type"] == "done" and not ev[-1]["budget_warning"]
    asyncio.run(g.record_usage({"model": "haiku", "path": "api", "cost_usd": 0.5}))  # paid: counts
    assert events(c.post("/v1/ask", json={"text": "b"}))[-1]["type"] == "done"  # the subscription path still answers
    g.API_KEY = "sk-test"
    assert events(c.post("/v1/ask", json={"text": "b", "path": "api"}))[0]["code"] == "budget"  # a paid path is held


def test_budget_paths_validated_and_usage_shape(gw):
    g, c, _ = gw
    for bad in ("api", ["gpt"], [1], None):
        assert c.put("/v1/settings", json={"budget_paths": bad}).status_code == 400, bad
    assert c.put("/v1/settings", json={"budget_paths": ["nim", "api", "api"]}).json()["budget_paths"] == ["api", "nim"]
    c.post("/v1/ask", json={"text": "a"})
    asyncio.run(g.record_usage({"model": "haiku", "path": "api", "cost_usd": 0.25, "input_tokens": 3}))
    u = c.get("/v1/usage").json()
    assert u["calls"] == 2 and u["by_path"] == {"claude_code": 0.5, "api": 0.25}
    assert u["cost_usd"] == 0.25 and u["paid"] == {"cost_usd": 0.25, "by_path": {"api": 0.25}}
    cc = u["subscription"]["claude_code"]
    assert (cc["turns"], cc["input_tokens"], cc["output_tokens"], cc["equivalent_usd"]) == (1, 10, 5, 0.5)
    assert cc["quota"]["window"] == "5h" and cc["quota"]["utilization_pct"] == 41.0
    assert cc["quota"]["windows"]["weekly"]["utilization_pct"] == 30.0
    g.claude_quota["5h"]["resets_at"] = 1  # a window past its reset is dropped
    assert c.get("/v1/usage").json()["subscription"]["claude_code"]["quota"]["window"] == "weekly"


def test_assistant_name(gw, monkeypatch):
    g, c, tmp = gw
    assert c.get("/v1/settings").json()["assistant_name"] == "아젤"
    pushes = []

    async def fake_push(payload):
        pushes.append(payload)
        return {"sent": 1}
    monkeypatch.setattr(g, "send_voip_push", fake_push)
    c.post("/v1/calls", json={"reason": "manual", "opening": "테스트예요."})
    c.post("/v1/ask", json={"text": "a"})
    args = json.loads((tmp / "args.json").read_text())
    assert pushes[-1]["caller"] == "아젤" and "'아젤'" in args[args.index("--append-system-prompt") + 1]
    assert c.put("/v1/settings", json={"assistant_name": " 루나  Luna "}).json()["assistant_name"] == "루나 Luna"
    assert g.fake.db["settings"]["assistant_name"] == "루나 Luna"  # shared: every gateway and the voice server
    c.post("/v1/calls", json={"reason": "manual", "opening": "테스트예요."})
    c.post("/v1/ask", json={"text": "b"})
    args = json.loads((tmp / "args.json").read_text())
    prompt = args[args.index("--append-system-prompt") + 1]
    assert pushes[-1]["caller"] == "루나 Luna" and "'루나 Luna'" in prompt and "아젤" not in prompt
    for bad in ["", "   ", "가" * 13, "아젤2", "R2-D2", "<b>x</b>", "아젤😀", 7, ["x"]]:
        assert c.put("/v1/settings", json={"assistant_name": bad}).status_code == 400, bad
    assert c.get("/v1/settings").json()["assistant_name"] == "루나 Luna"
    for name in ["가" * 12, "Azel", "아젤"]:
        assert c.put("/v1/settings", json={"assistant_name": name}).status_code == 200


def test_tool_activity_without_arguments(gw):
    g, c, _ = gw
    ev = events(c.post("/v1/ask", json={"text": "도구 써서 오늘 수업 알려줘"}))
    acts = [e for e in ev if e["type"] == "activity"]
    tool = {"tool": "mcp__claude_ai_Cygnus_MCP__gistra_today", "label": "학사 일정 확인 중", "kind": "schedule",
            "route": "claude"}
    assert acts == [{"type": "activity", "phase": "tool_start", **tool}, {"type": "activity", "phase": "tool_end", **tool}]
    assert "SECRET" not in json.dumps(ev)  # neither the arguments nor the result
    assert "".join(e["delta"] for e in ev if e["type"] == "text").strip() == "네, 확인했어요."


def test_tool_labels(gw):
    g, _, _ = gw
    for name, label, kind in [
        ("mcp__claude_ai_Cygnus_MCP__gistra_today", "학사 일정 확인 중", "schedule"),
        ("mcp__claude_ai_Cygnus_MCP__planit_get_day", "할 일 확인 중", "schedule"),
        ("mcp__claude_ai_Cygnus_MCP__memory_recall", "기억 찾는 중", "memory"),
        ("mcp__claude_ai_Cygnus_MCP__infra_status", "서버 상태 보는 중", "server"),
        ("mcp__claude_ai_Google_Calendar__list_events", "캘린더 확인 중", "calendar"),
        ("mcp__claude_ai_Gmail__search_threads", "메일 확인 중", "mail"),
        ("mcp__claude_ai_Notion__notion-search", "노션 보는 중", "notes"),
        ("mcp__macbook__run_command", "macbook에서 명령 실행 중", "terminal"),
        ("mcp__albireo__read_file", "albireo 파일 읽는 중", "files"),
        ("mcp__albireo__list_dir", "albireo 파일 보는 중", "files"),
        ("mcp__gienah__search", "gienah 파일 찾는 중", "files"), ("mcp__macbook__browse", "웹페이지 읽는 중", "web"),
        ("mcp__deneb__delegate", "deneb에서 작업 위임", "terminal"),
        ("mcp__macbook__job_status", "macbook 작업 확인 중", "terminal"), ("ToolSearch", "도구 준비 중", "generic"),
        ("mcp__claude_ai_Cygnus_MCP__search", "도구 준비 중", "generic"), ("", "도구 준비 중", "generic"),
    ]:
        assert g.tool_info(name) == (label, kind), name
    assert g.activity("thinking") == {"type": "activity", "phase": "thinking", "tool": "", "label": "생각 중",
                                      "kind": "generic", "route": "claude"}


def test_old_proactive_settings_migrate_on_read(gw):
    g, c, _ = gw
    g.fake.db["settings"] = {"proactive": {"morning_briefing": "07:30", "deadline_check": "21:00",
                                           "quiet_hours": ["23:30", "07:30"],
                                           "triggers": {"deadline": False, "server_down": False}}}
    p = c.get("/v1/settings").json()["proactive"]
    assert p == {"enabled": True, "morning_briefing": {"enabled": True, "time": "07:30"},
                 "deadline_check": {"enabled": False, "time": "21:00"}, "server_alerts": False,
                 "autonomous": {"enabled": False, "max_per_day": 3, "min_gap_minutes": 120,
                                "window": ["08:00", "23:00"], "checkpoints": ["08:30", "13:00", "18:00"]}}
    assert "quiet_hours" not in json.dumps(p) and not hasattr(g, "in_quiet_hours")


def test_proactive_settings_merge_and_validate(gw):
    g, c, _ = gw
    p = c.put("/v1/settings", json={"proactive": {"autonomous": {"enabled": True}}}).json()["proactive"]
    assert p["autonomous"]["enabled"] and p["autonomous"]["max_per_day"] == 3 and p["morning_briefing"]["time"] == "08:00"
    p = c.put("/v1/settings", json={"proactive": {"morning_briefing": {"time": "07:00"}}}).json()["proactive"]
    assert p["morning_briefing"] == {"enabled": True, "time": "07:00"} and p["autonomous"]["enabled"]  # kept
    for bad in [{"enabled": "yes"}, {"morning_briefing": {"time": "25:00"}}, {"autonomous": {"max_per_day": 50}},
                {"autonomous": {"window": ["23:00", "08:00"]}}, {"autonomous": {"window": ["08:00"]}},
                {"autonomous": {"min_gap_minutes": -1}}, {"autonomous": {"checkpoints": ["8:30"]}},
                {"server_alerts": 1}]:
        assert c.put("/v1/settings", json={"proactive": bad}).status_code == 400, bad


def test_master_switch_stops_proactive_calls_but_not_scheduled_ones(gw, monkeypatch):
    g, c, _ = gw
    c.put("/v1/settings", json={"proactive": {"enabled": False}})
    placed = []

    async def fake_place(reason, opening=None, call_id=None, persona_id=None, **kw):
        placed.append(reason)
        return {"call_id": f"c{len(placed)}"}
    monkeypatch.setattr(g, "place_call", fake_place)
    alert = {"alerts": [{"status": "firing", "labels": {"severity": "critical", "alertname": "NodeDown"}}]}
    assert c.post("/v1/alerts", json=alert).json() == {"called": False}
    asyncio.run(g.scheduler_tick(datetime(2026, 11, 21, 8, 0, tzinfo=g.KST), {}))  # briefing time
    assert placed == []
    at = (datetime.now(g.KST) + timedelta(minutes=5)).replace(second=0, microsecond=0)
    c.post("/v1/schedule", json={"at": at.isoformat(), "kind": "wake"})
    asyncio.run(g.fire_scheduled(at.timestamp() + 1))
    assert placed == ["wake"]  # the user asked for this one


def test_reply_length_setting(gw):
    g, c, tmp = gw
    assert c.get("/v1/settings").json()["reply_length"] == "normal"
    c.post("/v1/ask", json={"text": "a"})
    prompt = lambda: (a := json.loads((tmp / "args.json").read_text()))[a.index("--append-system-prompt") + 1]  # noqa: E731
    assert g.LENGTH_STYLE["normal"] in prompt() and "두세 문장 안에" not in prompt()
    assert c.put("/v1/settings", json={"reply_length": "detailed"}).status_code == 200
    c.post("/v1/ask", json={"text": "b"})
    assert g.LENGTH_STYLE["detailed"] in prompt()
    assert c.put("/v1/settings", json={"reply_length": "long"}).status_code == 400
    assert c.get("/v1/settings").json()["noise_mode"] == "normal"
    assert c.put("/v1/settings", json={"noise_mode": "noisy"}).json()["noise_mode"] == "noisy"
    assert c.put("/v1/settings", json={"noise_mode": "loud"}).status_code == 400


def test_personality(gw):
    g, c, tmp = gw
    s = c.get("/v1/settings").json()
    assert s["personality"] == "" and s["personality_default"] == g.PERSONALITY_DEFAULT
    c.post("/v1/ask", json={"text": "a"})
    prompt = lambda: (a := json.loads((tmp / "args.json").read_text()))[a.index("--append-system-prompt") + 1]  # noqa: E731
    assert f"성격과 말투: {g.PERSONALITY_DEFAULT.rstrip('.')}. 이것은 너 자신의 성격이야." in prompt()  # empty = the default, for Claude too
    s = c.put("/v1/settings", json={**s, "personality": "느긋하고 다정함"}).json()  # whole document back: fine
    assert s["personality"] == "느긋하고 다정함" and "personality_default" not in g.fake.db["settings"]
    c.post("/v1/ask", json={"text": "b"})
    assert "성격과 말투: 느긋하고 다정함. 이것은" in prompt() and g.PERSONALITY_DEFAULT not in prompt()
    for reset in ["", "   ", None]:  # "기본값으로 되돌리기"
        s = c.put("/v1/settings", json={"personality": reset}).json()
        assert s["personality"] == ("" if reset is None else reset) and g.personality(s) == g.PERSONALITY_DEFAULT
    g.fake.db["settings"]["user_title"] = "정훈 님"  # the setting itself is added with the persona model
    c.post("/v1/ask", json={"text": "c"})
    assert "사용자를 '정훈 님'(이)라고 부른다." in prompt() and "새로 부르지 말고" not in prompt()
    g.fake.db["settings"].pop("user_title")
    assert c.put("/v1/settings", json={"personality": "가" * 601}).status_code == 400
    assert c.put("/v1/settings", json={"personality": 3}).status_code == 400


# ---------- personas ----------
def persona_gw(g, monkeypatch):
    async def voices():
        return ["sohee", "ryan", "aiden", "vivian"]
    monkeypatch.setattr(g, "tts_voices", voices)


def test_personas_migrate_from_one_assistant(gw):
    g, c, _ = gw
    g.fake.db["settings"] = {"assistant_name": "루나", "personality": "다정함"}  # stored before personas
    [p] = c.get("/v1/personas").json()
    assert p["id"] == "azel" and p["primary"] and p["name"] == "루나" and p["personality"] == "다정함"
    s = c.get("/v1/settings").json()
    assert s["assistant_name"] == "루나" and s["personality"] == "다정함"  # older clients still read these
    c.put("/v1/settings", json={"assistant_name": "아젤"})  # and write the primary through them
    assert c.get("/v1/personas/azel").json()["name"] == "아젤"


def test_personas_null_optional_fields(gw, monkeypatch):
    g, c, _ = gw
    persona_gw(g, monkeypatch)
    nulls = {"voice_style": None, "personality": None, "color": None, "default_model": None}
    r = c.post("/v1/personas", json={"id": "nul", "name": "널이", "voice": "sohee", **nulls})
    assert r.status_code == 201, r.text
    assert (r.json()["voice_style"], r.json()["personality"], r.json()["color"]) == ("", "", "celadon")
    assert r.json()["default_model"] is None
    c.put("/v1/personas/nul", json={"voice_style": "차분함", "personality": "꼼꼼함", "color": "#112233"})
    r = c.put("/v1/personas/nul", json=nulls)
    assert r.status_code == 200, r.text
    assert (r.json()["voice_style"], r.json()["personality"], r.json()["color"]) == ("", "", "#112233")


def test_personas_tint_colors_like_the_app_sends(gw, monkeypatch):
    g, c, _ = gw
    persona_gw(g, monkeypatch)
    r = c.post("/v1/personas", json={"name": "미아", "voice": "sohee", "voice_style": "밝고 빠르게", "personality": "다정함",
                                     "color": "Celadon", "default_model": None, "share_memory": True})
    assert r.status_code == 201, r.text
    assert r.json()["color"] == "celadon"
    pid = r.json()["id"]
    assert c.put(f"/v1/personas/{pid}", json={"color": "AZALEA", "voice": None}).json()["color"] == "azalea"
    assert c.get(f"/v1/personas/{pid}").json()["voice"] == "sohee"  # null voice on edit keeps it
    assert c.put(f"/v1/personas/{pid}", json={"color": "#112233"}).status_code == 200
    assert c.put(f"/v1/personas/{pid}", json={"color": "red"}).status_code == 400


def test_personas_crud_and_limits(gw, monkeypatch):
    g, c, _ = gw
    persona_gw(g, monkeypatch)
    r = c.post("/v1/personas", json={"id": "luna", "name": "루나", "voice": "Aiden", "personality": "느긋함",
                                     "color": "#22aa88", "share_memory": True})
    assert r.status_code == 201 and r.json()["voice"] == "aiden" and not r.json()["primary"]
    for bad in [{"name": "루나", "voice": "sohee"},  # name taken
                {"name": "새비서", "voice": "nova"},  # not a TTS speaker
                {"name": "a" * 13, "voice": "sohee"}, {"name": "새비서"}, {"name": "새비서", "voice": "sohee", "color": "red"},
                {"name": "새비서", "voice": "sohee", "default_model": "gpt"}, {"name": "새비서", "voice": "sohee", "id": "Bad!"},
                {"name": "새비서", "voice": "sohee", "id": "luna"}, {"name": "새비서", "voice": "sohee", "evil": 1},
                {"name": "새비서", "voice": "sohee", "personality": "가" * 601}]:
        assert c.post("/v1/personas", json=bad).status_code == 400, bad
    assert c.put("/v1/personas/luna", json={"personality": "꼼꼼함", "voice": "ryan"}).json()["voice"] == "ryan"
    assert c.put("/v1/personas/luna", json={"primary": True}).status_code == 400  # only via /primary
    for i in range(6):
        assert c.post("/v1/personas", json={"name": f"비서{'가나다라마바'[i]}", "voice": "sohee"}).status_code == 201
    assert c.post("/v1/personas", json={"name": "아홉째", "voice": "sohee"}).status_code == 400  # max 8
    assert c.delete("/v1/personas/azel").status_code == 400  # the primary stays
    assert c.post("/v1/personas/luna/primary").json()["primary"]
    assert [p["id"] for p in c.get("/v1/personas").json() if p["primary"]] == ["luna"]
    assert c.get("/v1/settings").json()["assistant_name"] == "루나"
    gone = c.delete("/v1/personas/azel").json()  # no longer primary: marked deleted, not removed
    assert gone["deleted_at"] and "azel" not in [p["id"] for p in c.get("/v1/personas").json()]
    assert c.get("/v1/personas/azel").json()["name"] == "아젤"  # still resolvable for its old calls
    assert "azel" in [p["id"] for p in c.get("/v1/personas?include=deleted").json()]
    assert c.post("/v1/personas", json={"name": "아젤", "voice": "sohee"}).status_code == 201  # name free again
    assert c.get("/v1/personas/nope").status_code == 404


def test_persona_calls_schedule_and_asks(gw, monkeypatch):
    g, c, tmp = gw
    persona_gw(g, monkeypatch)
    c.post("/v1/personas", json={"id": "luna", "name": "루나", "voice": "aiden", "personality": "느긋하고 다정함"})
    sent, pushes = [], []

    async def capture(obj):
        sent.append(obj)
        return True

    async def fake_push(payload):
        pushes.append(payload)
        return {"sent": 1}
    g.link_send, g.send_voip_push = capture, fake_push
    asyncio.run(g.place_call("manual", "안녕하세요.", persona_id="luna"))
    assert pushes[-1]["caller"] == "루나" and sent[-1]["persona"] == "luna"
    asyncio.run(g.place_call("deadline", "마감이에요."))  # proactive calls: the primary
    assert pushes[-1]["caller"] == "아젤" and sent[-1]["persona"] == "azel"
    at = (datetime.now(g.KST) + timedelta(hours=2)).replace(second=0, microsecond=0).isoformat()
    item = c.post("/v1/schedule", json={"at": at, "persona_id": "luna"}).json()
    assert item["persona_id"] == "luna"
    assert c.post("/v1/schedule", json={"at": at, "persona_id": "ghost"}).status_code == 400
    assert c.get("/v1/schedule?persona=luna").status_code == 200
    asyncio.run(g.serve_ask({"type": "ask", "id": "a1", "text": "안녕", "session": "luna:s", "persona": "luna"}))
    prompt = (a := json.loads((tmp / "args.json").read_text()))[a.index("--append-system-prompt") + 1]
    assert "'루나'" in prompt and "느긋하고 다정함" in prompt and "'아젤'" not in prompt
    c.post("/v1/ask", json={"text": "b"})
    prompt = (a := json.loads((tmp / "args.json").read_text()))[a.index("--append-system-prompt") + 1]
    assert "'아젤'" in prompt  # no persona: the primary


def test_persona_delete_voices_and_samples(gw, monkeypatch):
    g, c, _ = gw
    persona_gw(g, monkeypatch)
    c.post("/v1/personas", json={"id": "luna", "name": "루나", "voice": "aiden"})
    c.post("/v1/personas", json={"id": "mira", "name": "미라", "voice": "ryan"})
    assert c.delete("/v1/personas/luna").json()["deleted_at"] and g.fake.persona_data == ("luna", False)
    c.delete("/v1/personas/mira?delete_history=true")
    assert g.fake.persona_data == ("mira", True)
    c.post("/v1/personas", json={"id": "nova", "name": "노바", "voice": "sohee"})
    g.fake.state.down = True  # data cannot be deleted: the persona is not deleted either
    assert c.delete("/v1/personas/nova").status_code == 503
    g.fake.state.down = False
    assert not c.get("/v1/personas/nova").json()["deleted_at"]
    v = c.get("/v1/voices").json()
    assert {"id": "sohee", "label": "소희", "desc": "따뜻하고 감정이 풍부한 여성", "lang": "ko"} in v and len(v) == 4
    assert {"id": "vivian", "label": "비비안", "desc": "밝고 또렷한, 살짝 톡 쏘는 젊은 여성", "lang": "zh"} in v
    assert not any(w in x["desc"] for x in v for w in ("한국어", "중국어", "영어", "일본어", "억양"))  # character, not origin
    r = c.post("/v1/personas", json={"id": "vivi", "name": "비비", "voice": "vivian"})
    assert r.status_code == 201 and c.get("/v1/personas/vivi").json()["voice"] == "vivian"  # every speaker, no migration
    calls = []

    async def fake_sample(text, voice, style):
        calls.append((text, voice, style))
        return b"RIFFfakewav"
    monkeypatch.setattr(g, "tts_sample", fake_sample)
    g._sample_at[0] = 0.0
    r = c.post("/v1/personas/voice-sample", json={"voice": "Sohee", "voice_style": "밝게", "text": "안녕하세요, 루나예요."})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content == b"RIFFfakewav"
    assert calls == [("안녕하세요, 루나예요.", "sohee", "밝게")]
    assert c.post("/v1/personas/voice-sample", json={"voice": "sohee", "text": "또"}).status_code == 429  # 1 per 2 s
    g._sample_at[0] = 0.0
    for bad in [{"voice": "sohee", "text": "가" * 61}, {"voice": "nova", "text": "안녕"}, {"voice": "sohee", "text": " "}]:
        assert c.post("/v1/personas/voice-sample", json=bad).status_code == 400, bad


def test_connectors_app_shape(gw, monkeypatch):
    g, c, tmp = gw
    g.discovery.connectors = [{"name": "Gmail", "url": "https://gmail.mcp.claude.com/mcp", "status": "connected"},
                              {"name": "Notion", "url": "https://mcp.notion.com/mcp", "status": "failed"}]
    g.discovery.at = time.time()
    got = c.get("/v1/connectors").json()
    assert got == {"claude_ai": [{"name": "Gmail", "enabled": True, "status": "connected"},
                                 {"name": "Notion", "enabled": False, "status": "needs_auth"}], "custom": [],
                   "oauth": {"spotify": "needs_consent"}}
    put = {"claude_ai": [{"name": "Gmail", "enabled": False}, {"name": "Notion", "enabled": True}],
           "custom": [{"name": "linear", "url": "https://mcp.linear.app/sse", "transport": "sse", "enabled": True}]}
    got = c.put("/v1/connectors", json=put).json()
    assert [x["enabled"] for x in got["claude_ai"]] == [False, True]
    assert got["custom"] == [{"name": "linear", "url": "https://mcp.linear.app/sse", "transport": "sse", "enabled": True,
                              "has_secret": False, "allowed_tools": []}]
    r = c.put("/v1/connectors/secret/linear", json={"value": "lin_secret"})
    assert r.status_code == 204 and r.content == b""
    assert c.get("/v1/connectors").json()["custom"][0]["has_secret"] is True
    assert "lin_secret" not in json.dumps(c.get("/v1/connectors").json())
    for bad in [{"claude_ai": {"Gmail": True}}, {"claude_ai": [{"name": "Gmail", "enabled": "yes"}]},
                {"custom": [{"name": "Bad Name", "url": "https://x"}]}, {"extra": 1}]:
        assert c.put("/v1/connectors", json=bad).status_code == 400, bad


def test_wake_enrollment_mounted_on_full_gateways(gw):
    g, c, _ = gw
    assert c.get("/v1/wake/enroll").json()["status"] == "none"
    assert c.get("/v1/wake/enroll", headers={"Authorization": "Bearer nope"}).status_code == 401  # bearer applies


def test_claude_error_is_spoken_not_silent(gw):
    g, c, _ = gw
    ev = events(c.post("/v1/ask", json={"text": "한도 걸린 질문"}))
    assert ev == [{"type": "error", "code": "limit", "message": "지금은 Claude 사용 한도에 걸려서 답할 수 없어요. 오후 10시쯤 풀려요."}]
    assert g.claude_failure("something broke")["code"] == "claude_error"
    assert g.claude_failure("rate limited (429)")["message"].endswith("다시 물어봐 주세요.")
    assert events(c.post("/v1/ask", json={"text": "평범한 질문"}))[-1]["type"] == "done"  # unchanged otherwise


def test_settings_reject_unknown(gw):
    g, c, _ = gw
    assert c.put("/v1/settings", json={"nope": 1}).status_code == 400
    assert c.put("/v1/settings", json={"default_model": "opus"}).json()["default_model"] == "opus"


def test_devices_and_mcp_config(gw):
    g, c, tmp = gw
    assert c.post("/v1/devices/register", json={"id": "한글", "name": "x", "url": "u"}).status_code == 400
    c.post("/v1/devices/register", json={"id": "macbook", "name": "macbook", "url": "http://x/mcp"})
    c.post("/v1/ask", json={"text": "a"})
    args = json.loads((tmp / "args.json").read_text())
    assert "mcp__macbook" in args and "--mcp-config" in args
    assert not Path(args[args.index("--mcp-config") + 1]).exists()  # token-bearing file removed


def test_approval_flow(gw):
    g, c, _ = gw
    aid = c.post("/v1/approvals", json={"device": "macbook", "tool": "run_command",
                                        "command": "git push", "cwd": "~/dev/x"}).json()["approval_id"]
    assert [a["id"] for a in c.get("/v1/approvals").json()] == [aid]
    assert c.post(f"/v1/approvals/{aid}", json={"approve": True}).json()["status"] == "approved"
    assert c.post(f"/v1/approvals/{aid}", json={"approve": False}).status_code == 409
    got = c.get(f"/v1/approvals/{aid}").json()
    assert got["status"] == "approved" and got["command"] == "git push"


def test_alert_places_call_without_apns(gw):
    g, c, _ = gw
    r = c.post("/v1/alerts", json={"alerts": [{"status": "firing", "labels": {"severity": "critical", "alertname": "NodeDown"}}]}).json()
    assert r["called"] and r["sent"] == 0
    assert "NodeDown" in c.get(f"/v1/calls/{r['call_id']}").json()["opening"]
    r = c.post("/v1/alerts", json={"alerts": [{"status": "firing", "labels": {"severity": "warning"}}]}).json()
    assert r == {"called": False}


def test_device_prompt_fallback(gw):
    g, c, _ = gw
    s = asyncio.run(g.load_settings())
    devs = [{"name": "albireo", "id": "albireo"}, {"name": "spark", "id": "spark"}]
    p = g.device_prompt(s, devs)
    assert p.index("albireo(") < p.index("spark(") and "기본 기기 macbook는 꺼져" in p
    p = g.device_prompt(s, [{"name": "macbook", "id": "macbook"}] + devs)
    assert p.index("macbook(") < p.index("albireo(") and "꺼져" not in p
    assert "없어" in g.device_prompt(s, [])
    g.fake.db["settings"] = {"default_device": "맥북", "device_fallback": ["알비레오", "스파크"]}  # saved before
    s = c.get("/v1/settings").json()
    assert s["default_device"] == "macbook" and s["device_fallback"] == ["albireo", "spark"]

def test_deadline_call_only_when_due(gw, monkeypatch):
    g, _, _ = gw
    placed = []

    async def fake_place(reason, opening=None, call_id=None, persona_id=None, **kw):
        placed.append((reason, opening))
        return {"call_id": "x"}
    monkeypatch.setattr(g, "place_call", fake_place)
    for answer, expect in [("NONE", 0), ("마감이 다가오는 게 있어요. 토론지가 내일 23시까지예요.", 1)]:
        async def fake_ask(*a, **k):
            return answer
        monkeypatch.setattr(g, "ask_text", fake_ask)
        placed.clear()
        asyncio.run(g.deadline_call())
        assert len(placed) == expect


def test_link_serves_ask_and_cancel(gw):
    g, _, _ = gw
    sent = []

    class FakeWS:
        def __init__(self, msgs):
            self.msgs = msgs

        async def send(self, raw):
            sent.append(json.loads(raw))

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for m in self.msgs:
                yield json.dumps(m)
                await asyncio.sleep(0.3)
            await asyncio.sleep(3)  # stay connected until the fake claude finishes

    async def run():
        ws = FakeWS([{"type": "ask", "id": "a1", "text": "서버 괜찮아?", "session": "s"},
                     {"type": "ask", "id": "a2", "text": "느린 질문"}, {"type": "cancel", "id": "a2"}])
        g.link["ws"] = ws
        await g.handle_link(ws)
        await asyncio.sleep(0.5)
    asyncio.run(run())
    a1 = [m for m in sent if m.get("id") == "a1"]
    assert "".join(m["delta"] for m in a1 if m["type"] == "text") == "네, 확인했어요." and a1[-1]["type"] == "done"
    assert not any(m.get("id") == "a2" and m["type"] == "done" for m in sent)  # cancelled before finishing


# ---------- shared state ----------
def test_state_is_shared_not_local(gw):
    g, c, tmp = gw
    c.put("/v1/settings", json={"default_model": "opus"})
    assert g.fake.db["settings"]["default_model"] == "opus"
    g.fake.db["settings"]["monthly_budget_usd"] = 7  # another gateway changed it
    assert c.get("/v1/settings").json()["monthly_budget_usd"] == 7
    c.post("/v1/ask", json={"text": "a"})
    assert g.fake.db["usage"][0]["device"] == "macbook" and g.fake.db["usage"][0]["cost_usd"] == 0.5
    assert c.get("/v1/usage").json()["by_device"] == {"macbook": 0.5}
    c.post("/v1/devices/register", json={"id": "albireo", "name": "albireo", "url": "http://a/mcp"})
    assert g.fake.db["devices"]["albireo"]["gateway"] == "macbook"
    c.post("/v1/push/register", json={"voip_token": "tok", "bundle_id": "xyz.scian.biseo"})
    assert g.fake.db["push"]["tok"]["env"] == "sandbox"
    assert not (tmp / "gateway.db").exists()


def test_first_start_seeds_ruchba_from_old_settings(tmp_path, monkeypatch):
    (tmp_path / "settings.json").write_text(json.dumps({"default_model": "opus", "monthly_budget_usd": 300}))
    g = start(tmp_path, monkeypatch)
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        assert c.get("/v1/settings").json()["monthly_budget_usd"] == 300
    assert g.fake.db["settings"] == {"default_model": "opus", "monthly_budget_usd": 300}


def test_limited_writer_cannot_steer_full_gateways(gw, monkeypatch):
    g, c, tmp = gw
    c.post("/v1/devices/register", json={"id": "macbook", "name": "macbook", "url": "http://100.64.0.8:8893/mcp"})
    lim = TestClient(g.fake, headers={"Authorization": "Bearer l1m"})  # a team machine's gateway
    takeover = lim.put("/v1/state/devices/macbook", json={"name": "macbook", "url": "http://evil/mcp", "gateway": "x"})
    assert takeover.status_code == 403 and g.fake.db["devices"]["macbook"]["url"] == "http://100.64.0.8:8893/mcp"
    assert lim.put("/v1/state/devices/evil", json={"name": "evil", "url": "http://evil/mcp"}).status_code == 200
    c.post("/v1/ask", json={"text": "a"})
    args = json.loads((tmp / "args.json").read_text())
    assert "mcp__macbook" in args and "mcp__evil" not in args  # never handed to claude -p, never gets the token
    probed = []

    async def fake_probe(url, token=False):
        probed.append((url, token))
        return {"ok": True, "ms": 1}
    monkeypatch.setattr(g, "probe", fake_probe)
    assert c.get("/v1/status").json()["calls"] == 0
    g.call_warm_keys["log9"], g.call_started["log9"] = "azel:call-9", time.time()  # a call in progress
    assert c.get("/v1/status").json()["calls"] == 1
    assert ("http://evil/mcp", False) in probed and ("http://100.64.0.8:8893/mcp", True) in probed
    assert all(not tok for url, tok in probed if url.endswith("/health"))  # health checks carry no token


def test_ruchba_down_read_only(gw):
    g, c, tmp = gw
    c.put("/v1/settings", json={"default_model": "opus"})
    c.post("/v1/devices/register", json={"id": "macbook", "name": "macbook", "url": "http://x/mcp"})
    g.state.cache.clear()  # nothing cached in memory: the settings file copy and local devices must do
    g.fake.state.down = True
    assert c.get("/v1/settings").json()["default_model"] == "opus"
    assert c.put("/v1/settings", json={"default_model": "haiku"}).status_code == 503
    assert c.post("/v1/approvals", json={"device": "macbook", "command": "rm x"}).status_code == 503
    ev = events(c.post("/v1/ask", json={"text": "a", "session": "s1"}))
    assert ev[-1]["type"] == "done"  # Claude still answers
    args = json.loads((tmp / "args.json").read_text())
    assert "mcp__macbook" in args  # this device's own daemon still works
    assert len(g.pending_usage) == 1 and g.fake.db["usage"] == []
    g.fake.state.down = False
    c.post("/v1/ask", json={"text": "b"})
    assert len(g.fake.db["usage"]) == 2 and g.pending_usage == []  # the queued row was flushed


def test_outage_survives_a_restart(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch)
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        c.post("/v1/push/register", json={"voip_token": "tok1", "bundle_id": "xyz.scian.biseo"})
        g.fake.state.down = True
        c.post("/v1/ask", json={"text": "a"})
    assert len(g.pending_usage) == 1 and os.stat(tmp_path / "pending_usage.json").st_mode & 0o077 == 0
    g2 = start(tmp_path, monkeypatch)  # restarted while ruchba is still down
    g2.fake.state.down = True
    assert [t["token"] for t in asyncio.run(g2.push_tokens())] == ["tok1"]  # calls still reach the phone
    assert len(g2.pending_usage) == 1
    g2.fake.state.down = False
    with TestClient(g2.app, headers={"Authorization": "Bearer t0k"}) as c:
        c.post("/v1/ask", json={"text": "b"})
    assert len(g2.fake.db["usage"]) == 2 and g2.pending_usage == []
    assert json.loads((tmp_path / "pending_usage.json").read_text()) == []


def test_rejected_usage_row_does_not_block_the_queue(gw):
    g, c, tmp = gw
    asyncio.run(g.record_usage({"model": "bad", "path": "api", "cost_usd": 1}))
    asyncio.run(g.record_usage({"model": "haiku", "path": "api", "cost_usd": 0.1}))
    assert [u["model"] for u in g.fake.db["usage"]] == ["haiku"] and g.pending_usage == []
    assert os.stat(tmp / "usage_rejected.jsonl").st_mode & 0o077 == 0  # holds usage rows: owner only
    dead = [json.loads(line) for line in (tmp / "usage_rejected.jsonl").read_text().splitlines()]
    assert dead[0]["status"] == 422 and dead[0]["row"]["model"] == "bad"


def old_gateway_db(path):
    import sqlite3
    con = sqlite3.connect(path)
    con.executescript("""
        create table usage(ts real, model text, path text, task text, input_tokens int, output_tokens int, cost_usd real);
        create table push_tokens(token text primary key, bundle_id text, env text, ts real);
        create table approvals(id text primary key, device text, tool text, command text, cwd text, status text,
            created real, decided real);""")
    now = datetime.now().timestamp()
    con.executemany("insert into usage values(?,?,?,?,?,?,?)", [(now - 60, "opus", "claude_code", "", 1, 1, 2.5),
                                                               (now - 86400 * 70, "opus", "claude_code", "", 1, 1, 9)])
    con.execute("insert into push_tokens values('tokA','xyz.scian.azel','production',0)")
    con.executemany("insert into approvals values(?,?,?,?,?,?,?,?)", [
        ("aaaaaaaaaaaa", "macbook", "run_command", "git push", "~/dev/x", "pending", now, None),
        ("bbbbbbbbbbbb", "macbook", "run_command", "rm x", "~", "approved", now, now)])
    con.commit()
    con.close()


def test_old_gateway_db_migrates_once_and_idempotently(tmp_path, monkeypatch):
    old_gateway_db(tmp_path / "gateway.db")
    g = start(tmp_path, monkeypatch)
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        for _ in range(50):  # runs in the background on start
            if (tmp_path / "migrated_from_sqlite").exists():
                break
            time.sleep(0.05)
        assert (tmp_path / "migrated_from_sqlite").exists()
        assert c.get("/v1/usage").json()["subscription"]["claude_code"]["equivalent_usd"] == 2.5  # this month only
    assert list(g.fake.db["approvals"]) == ["aaaaaaaaaaaa"]  # pending only, same id
    assert [t["token"] for t in g.fake.db["push"].values()] == ["tokA"]
    asyncio.run(g.migrate_sqlite())  # by hand again: nothing doubles
    assert len(g.fake.db["usage"]) == 1 and len(g.fake.db["approvals"]) == 1


def test_migration_retries_when_ruchba_is_down(tmp_path, monkeypatch):
    old_gateway_db(tmp_path / "gateway.db")
    g = start(tmp_path, monkeypatch)
    g.fake.state.down = True
    asyncio.run(g.migrate_once())
    assert not (tmp_path / "migrated_from_sqlite").exists()  # tried again on the next start


CALL_LOG = [
    {"type": "call_log", "event": "start", "log_id": "L1", "session": "s1", "call_id": "c1", "direction": "incoming",
     "reason": "morning_briefing", "client": "iphone", "opening": "좋은 아침이에요.", "started_at": 1.0},
    {"type": "call_log", "event": "turn", "log_id": "L1", "user": "오늘 일정 알려줘", "assistant": ["10시 회의예요."],
     "route": "claude", "activity": ["캘린더 확인 중"], "timings": {"total_ms": 4000}, "interrupted": False},
    {"type": "call_log", "event": "turn", "log_id": "L1", "user": "  ", "assistant": []},  # silence: skipped
    {"type": "call_log", "event": "end", "log_id": "L1", "ended_at": 61.0, "duration_s": 60.0},
]


class LinkWS:
    def __init__(self, msgs):
        self.msgs = msgs

    async def send(self, raw):
        pass

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self.msgs:
            yield json.dumps(m)


def test_call_history_round_trip(gw):
    g, c, _ = gw
    asyncio.run(g.handle_link(LinkWS(CALL_LOG)))
    h = g.fake.db["history"]["L1"]
    assert [t["user"] for t in h["turns"]] == ["오늘 일정 알려줘"] and h["ended"] and h["direction"] == "incoming"
    assert "type" not in h and h["turns"][0]["activity"] == ["캘린더 확인 중"]
    assert c.get("/v1/history").json() == [{"id": "L1", "title": "오늘 일정 알려줘", "turns": 1, "started_at": 1.0,
                                            "direction": "incoming", "reason": "morning_briefing"}]
    assert c.get("/v1/history?q=일정").json()[0]["id"] == "L1" and c.get("/v1/history?q=없음").json() == []
    assert c.get("/v1/history/L1").json()["opening"] == "좋은 아침이에요."
    assert c.delete("/v1/history/L1").json() == {"deleted": 1}
    assert c.get("/v1/history/L1").status_code == 404 and c.delete("/v1/history/L1").status_code == 404
    assert c.delete("/v1/history?older_than_days=30").json() == {"deleted": 0} and g.fake.pruned == 30
    assert c.delete("/v1/history?older_than_days=0").status_code == 400


def test_call_history_survives_an_outage(gw):
    g, c, tmp = gw
    g.fake.state.down = True
    asyncio.run(g.handle_link(LinkWS(CALL_LOG[:2])))
    assert len(g.history_outbox.items) == 2 and (tmp / "pending_history.json").exists()
    g.fake.state.down = False
    asyncio.run(g.handle_link(LinkWS(CALL_LOG[3:])))
    assert g.fake.db["history"]["L1"]["ended"] and len(g.fake.db["history"]["L1"]["turns"]) == 1
    assert g.history_outbox.items == []


def test_history_days_setting_and_retention(gw, monkeypatch):
    g, c, _ = gw
    assert c.get("/v1/settings").json()["history_days"] == 365
    for bad in [-1, 1.5, "30", True, 99999]:
        assert c.put("/v1/settings", json={"history_days": bad}).status_code == 400, bad
    assert c.put("/v1/settings", json={"history_days": 30}).status_code == 200
    at4 = datetime(2026, 11, 22, 4, 0, tzinfo=g.KST)
    asyncio.run(g.scheduler_tick(at4, {}))
    assert g.fake.pruned == 30
    g.fake.pruned = None
    monkeypatch.setattr(g, "INSTANCE", "albireo-x")  # another gateway, same minute: lease taken, no second prune
    asyncio.run(g.scheduler_tick(at4, {}))
    assert g.fake.pruned is None
    c.put("/v1/settings", json={"history_days": 0})  # keep forever
    monkeypatch.setattr(g, "INSTANCE", "macbook-y")
    asyncio.run(g.scheduler_tick(datetime(2026, 11, 23, 4, 0, tzinfo=g.KST), {}))
    assert g.fake.pruned is None


# ---------- scheduled calls ----------
def kst(*a):
    return datetime(*a, tzinfo=timezone(timedelta(hours=9)))


def test_schedule_api_validates_and_confirms(gw):
    g, c, _ = gw
    now = time.time()
    tomorrow10 = (datetime.now(g.KST) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    r = c.post("/v1/schedule", json={"at": tomorrow10.isoformat(), "kind": "wake"})
    assert r.status_code == 201
    item = r.json()
    assert item == {"id": item["id"], "at": tomorrow10.isoformat(), "repeat": None, "kind": "wake", "note": "",
                    "status": "pending", "attempts": 0, "created_by": "app", "persona_id": "azel",
                    "next_at": tomorrow10.isoformat(), "cron": None, "rule_text": None, "start_date": None,
                    "end_date": None, "notify": "call", "purpose": ""}
    iso = lambda t: datetime.fromtimestamp(t, g.KST).isoformat()  # noqa: E731
    for bad in [{"at": iso(now - 60)}, {"at": iso(now + 31 * 86400)}, {"at": "2026-12-01T10:00:00"},  # no offset
                {"at": now + 3600}, {"at": iso(now + 3600), "repeat": "hourly"},
                {"at": iso(now + 3600), "repeat": "weekly:mon"}, {"at": iso(now + 3600), "repeat": "weekly:7"},
                {"at": iso(now + 3600), "kind": "nap"}, {"at": iso(now + 3600), "note": "x" * 201}, {"at": "내일"},
                {"at": kst(2026, 11, 25, 9, 0).isoformat(), "repeat": "weekly:0"}]:  # weekly:0 on a Wednesday
        assert c.post("/v1/schedule", json=bad).status_code == 400, bad
    dow = datetime.fromtimestamp(now + 7200, g.KST).weekday()
    weekly = c.post("/v1/schedule", json={"at": iso(now + 7200), "repeat": f"weekly:{dow}", "kind": "reminder",
                                          "note": "회의"}).json()
    assert weekly["repeat"] == f"weekly:{dow}" and weekly["created_by"] == "app"
    assert [i["id"] for i in c.get("/v1/schedule").json()] == [weekly["id"], item["id"]]  # soonest first
    gone = c.delete(f"/v1/schedule/{item['id']}").json()  # the whole result: the app never reads it, errors show text
    assert gone["ok"] and gone["data"] == {**item, "status": "cancelled"}
    assert c.delete(f"/v1/schedule/{item['id']}").status_code == 404
    assert [i["id"] for i in c.get("/v1/schedule").json()] == [weekly["id"]]  # default: pending and fired
    assert {i["id"] for i in c.get("/v1/schedule?include=all").json()} == {weekly["id"], item["id"]}


def test_spoken_times(gw):
    g, _, _ = gw
    now = kst(2026, 11, 21, 22, 0).timestamp()
    for at, repeat, want in [
        (kst(2026, 11, 22, 10, 0), None, "내일 오전 10시"), (kst(2026, 11, 22, 7, 30), None, "내일 오전 7시 30분"),
        (kst(2026, 11, 21, 22, 30), None, "오늘 오후 10시 30분"), (kst(2026, 11, 23, 15, 0), None, "모레 오후 3시"),
        (kst(2026, 11, 30, 9, 0), None, "11월 30일 오전 9시"), (kst(2026, 11, 22, 7, 0), "daily", "매일 오전 7시"),
        (kst(2026, 11, 23, 6, 40), "weekdays", "평일 오전 6시 40분"), (kst(2026, 11, 23, 9, 0), "weekly:0", "매주 월요일 오전 9시"),
        (kst(2026, 11, 29, 9, 0), "weekly:6", "매주 일요일 오전 9시"),
        (kst(2026, 11, 22, 0, 5), None, "내일 밤 12시 5분"), (kst(2026, 11, 21, 12, 30), None, "오늘 낮 12시 30분")]:
        assert g.spoken_when(at.timestamp(), repeat, now) == want, want
    fri7 = kst(2026, 11, 27, 7, 0).timestamp()  # a Friday
    assert g.next_fire_of({"at": fri7, "repeat": "weekdays"}, fri7) == kst(2026, 11, 30, 7, 0).timestamp()  # skips the weekend
    assert g.next_fire_of({"at": fri7, "repeat": "daily"}, fri7) == kst(2026, 11, 28, 7, 0).timestamp()
    assert g.next_fire_of({"at": fri7, "repeat": "weekly:4"}, fri7) == kst(2026, 12, 4, 7, 0).timestamp()
    assert g.next_fire_of({"at": fri7, "repeat": "daily"}, fri7 + 3 * 86400) == kst(2026, 12, 1, 7, 0).timestamp()  # skips missed days


def test_schedule_over_the_link(gw):
    g, c, _ = gw
    sent = []

    async def capture(obj):
        sent.append(obj)
        return True
    g.link_send = capture
    at = (datetime.now(g.KST) + timedelta(days=1)).replace(hour=7, minute=30, second=0, microsecond=0)
    for m in [{"type": "schedule", "id": "r1", "action": "create", "at": at.isoformat(), "kind": "wake",
               "repeat": None, "note": ""},
              {"type": "schedule", "id": "r2", "action": "list"},
              {"type": "schedule", "id": "r3", "action": "cancel"},
              {"type": "schedule", "id": "r4", "action": "cancel"},
              {"type": "schedule", "id": "r5", "action": "create",
               "at": datetime.fromtimestamp(time.time() - 10, g.KST).isoformat()}]:
        asyncio.run(g.serve_schedule(m))
    by = {m["id"]: m for m in sent}
    assert by["r1"]["say"] == "내일 오전 7시 30분에 깨워 드릴게요." and by["r1"]["ok"]
    assert by["r1"]["item"]["at"] == at.isoformat() and by["r1"]["item"]["created_by"] == "voice"
    assert set(by["r1"]["item"]) == {"id", "at", "repeat", "kind", "note", "status", "attempts", "created_by",
                                     "persona_id", "next_at", "cron", "rule_text", "start_date", "end_date", "notify",
                                     "purpose"}
    assert by["r2"]["say"] == "예약된 전화는 내일 오전 7시 30분 기상이에요."
    assert by["r3"]["say"] == "내일 오전 7시 30분 예약을 취소했어요."
    assert by["r4"]["say"] == "취소할 예약이 없어요."
    assert not by["r5"]["ok"] and by["r5"]["say"] == "그 시간으로는 예약할 수 없어요."
    assert all(m["type"] == "schedule_result" for m in sent)


def fire_setup(g, monkeypatch, **item):
    placed, alerts = [], []

    async def fake_place(reason, opening=None, call_id=None, persona_id=None, **kw):
        placed.append((reason, opening))
        assert g.fake.db["schedule"]["s1"]["call_id"] == call_id  # claimed with the attempt, before ringing
        return {"call_id": call_id}

    async def fake_alert(title, body):
        alerts.append(body)
        return {"sent": 1}
    monkeypatch.setattr(g, "place_call", fake_place)
    monkeypatch.setattr(g, "send_alert_push", fake_alert)
    at = kst(2026, 11, 22, 7, 0).timestamp()
    g.fake.db["schedule"]["s1"] = {"id": "s1", "at": at, "repeat": None, "kind": "wake", "note": "", "created_by": "",
                                   "status": "pending", "attempts": 0, "call_id": None, "last_attempt": None, **item}
    return at, placed, alerts


def test_wake_call_retries_every_two_minutes_for_thirty(gw, monkeypatch):
    g, _, _ = gw
    at, placed, alerts = fire_setup(g, monkeypatch)  # kind wake
    item = g.fake.db["schedule"]["s1"]
    for k in range(15):
        asyncio.run(g.fire_scheduled(at + k * 120))
        assert len(placed) == k + 1 and item["status"] == "fired", k
    asyncio.run(g.fire_scheduled(at + 28 * 60 + 30))
    assert len(placed) == 15 and item["status"] == "fired"  # waiting for the last ring to be answered
    asyncio.run(g.fire_scheduled(at + 29 * 60))
    assert item["status"] == "missed" and len(placed) == 15


def test_scheduled_call_fires_retries_and_is_missed(gw, monkeypatch):
    g, _, _ = gw
    at, placed, alerts = fire_setup(g, monkeypatch, kind="reminder")
    item = g.fake.db["schedule"]["s1"]
    asyncio.run(g.fire_scheduled(at - 60))
    assert placed == []  # not yet
    for dt, n in [(0, 1), (60, 1), (120, 2), (180, 2), (240, 3), (290, 3)]:
        asyncio.run(g.fire_scheduled(at + dt))
        assert len(placed) == n and item["attempts"] == n, (dt, n)
    t = datetime.fromtimestamp(at, g.KST)  # the opening: composed from the time, never the request (openings.py)
    assert placed[0][0] == "reminder" and item["status"] == "fired" \
        and len(item["call_id"]) == 32
    asyncio.run(g.fire_scheduled(at + 300))  # 60 s after the third attempt
    assert item["status"] == "missed" and len(placed) == 3
    assert len(alerts) == 1 and "받지 않으셨어요" in alerts[0]


def test_answered_call_stops_retries_and_repeat_reschedules(gw, monkeypatch):
    g, _, _ = gw
    at, placed, _ = fire_setup(g, monkeypatch, repeat="daily", kind="reminder", note="약 드실 시간이에요.")
    item = g.fake.db["schedule"]["s1"]
    asyncio.run(g.fire_scheduled(at))
    t = datetime.fromtimestamp(at, g.KST)
    purpose = g.openings.purpose_rule("약 드실 시간이에요.")
    assert placed == [("reminder", g.openings.template("reminder", "", t.hour, t.minute, purpose))]
    assert "약 드실 시간이에요." not in placed[0][1]  # the note is not read back verbatim
    asyncio.run(g.handle_link(LinkWS([{"type": "call_answered", "call_id": item["call_id"]}])))
    assert item["status"] == "pending" and item["attempts"] == 0 and item["at"] > at  # tomorrow, same time
    assert datetime.fromtimestamp(item["at"], g.KST).strftime("%H:%M") == "07:00"
    asyncio.run(g.fire_scheduled(at + 600))
    assert len(placed) == 1  # answered: no retry


def test_late_scheduled_call_is_missed_not_rung(gw, monkeypatch):
    g, _, _ = gw
    at, placed, alerts = fire_setup(g, monkeypatch)
    asyncio.run(g.fire_scheduled(at + 6 * 3600))  # 07:00 alarm, gateway back at 13:00
    assert placed == [] and g.fake.db["schedule"]["s1"]["status"] == "missed"
    assert alerts == ["오늘 오전 7시 기상 전화를 제때 걸지 못했어요."]
    at, placed, alerts = fire_setup(g, monkeypatch, repeat="daily")
    asyncio.run(g.fire_scheduled(at + 11 * 60))  # just past the 10-minute grace
    item = g.fake.db["schedule"]["s1"]
    assert placed == [] and item["status"] == "pending" and item["at"] == at + 86400  # repeats move on
    at, placed, alerts = fire_setup(g, monkeypatch, status="fired", attempts=1, last_attempt=at, call_id="c0")
    asyncio.run(g.fire_scheduled(at + 3600))  # a retry the gateway slept through: no late ring either
    assert placed == [] and g.fake.db["schedule"]["s1"]["status"] == "missed"


def test_cancelled_call_never_fires(gw, monkeypatch):
    g, c, _ = gw
    at, placed, _ = fire_setup(g, monkeypatch)
    c.delete("/v1/schedule/s1")
    asyncio.run(g.fire_scheduled(at + 10))
    assert placed == []


def test_only_the_preferred_gateway_fires(gw, monkeypatch):
    g, _, _ = gw
    at, placed, _ = fire_setup(g, monkeypatch)
    monkeypatch.setattr(g, "SCHED_PRIORITY", 1)
    monkeypatch.setattr(g, "INSTANCE", "deneb-a")
    asyncio.run(g.scheduler_tick(datetime.fromtimestamp(at - 120, g.KST), {}))  # deneb holds the lease
    monkeypatch.setattr(g, "SCHED_PRIORITY", 10)
    monkeypatch.setattr(g, "INSTANCE", "macbook-b")
    asyncio.run(g.scheduler_tick(datetime.fromtimestamp(at + 5, g.KST), {}))
    assert placed == []  # the Mac does not fire while deneb is alive
    g.fake.db["leases"]["scheduler"]["expires"] = 0  # deneb went away
    asyncio.run(g.scheduler_tick(datetime.fromtimestamp(at + 65, g.KST), {}))
    assert len(placed) == 1


def test_gateway_without_claude(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch, BISEO_NO_CLAUDE="1")
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        ev = events(c.post("/v1/ask", json={"text": "안녕"}))
        assert ev == [{"type": "error", "code": "no_claude", "message": "이 기기에서는 Claude를 쓸 수 없어요."}]
    ran = []

    async def fake_place(reason, opening=None, call_id=None, persona_id=None, **kw):
        ran.append(reason)
        return {"call_id": "x"}
    monkeypatch.setattr(g, "place_call", fake_place)
    asyncio.run(g.scheduler_tick(datetime(2026, 11, 21, 8, 0, tzinfo=g.KST), {}))
    assert ran == []  # no briefing without Claude


# ---------- autonomous calls ----------
A = {"enabled": True, "max_per_day": 3, "min_gap_minutes": 120, "window": ["08:00", "23:00"],
     "checkpoints": ["08:30", "13:00", "18:00"]}


def test_guardrails(gw):
    g, _, _ = gw
    at = lambda h, m=0: kst(2026, 11, 22, h, m).timestamp()  # noqa: E731
    yes = {"call": True, "urgency": 5, "reason": "서버 다운", "opening": "서버가 다운됐어요."}
    called = lambda t, reason="다른 일": {"at": t, "reason_hash": g.reason_hash(reason)}  # noqa: E731
    assert g.guard(yes, at(10), [], A) is None
    assert g.guard({**yes, "call": False}, at(10), [], A) == "no_call"
    assert g.guard({**yes, "urgency": 3}, at(10), [], A) == "urgency"
    for h, m in [(2, 0), (7, 59), (23, 0), (23, 30)]:  # Qwen can never ring outside the window
        assert g.guard(yes, at(h, m), [], A) == "window", (h, m)
    # no count or gap cap: the judge decides from recent_calls
    assert g.guard(yes, at(20), [called(at(h)) for h in (8, 11, 14, 19)], A) is None
    assert g.guard(yes, at(11, 30), [called(at(11))], A) is None
    assert g.guard(yes, at(10), [called(at(9), "서버 다운!")], A) == "duplicate"  # same reason, other punctuation
    assert g.guard(yes, at(17), [called(at(14), "서버 다운")], A) is None  # 3 h later and still true: ring again


def auto_setup(g, monkeypatch, verdict):
    placed, asked = [], []

    async def fake_place(reason, opening=None, call_id=None, persona_id=None, **kw):
        placed.append((reason, opening))
        return {"call_id": f"call{len(placed)}"}

    async def fake_llm(system, user, max_tokens=300):
        asked.append(user)
        return verdict
    monkeypatch.setattr(g, "place_call", fake_place)
    monkeypatch.setattr(g, "llm_json", fake_llm)
    g.fake.db["kv"]["alerts_recent"] = [{"names": "NodeDown", "at": time.time() - 300}]
    return placed, asked


def test_autonomous_tick_calls_logs_and_dedupes(gw, monkeypatch):
    g, c, _ = gw
    verdict = {"call": True, "urgency": 5, "reason": "ruchba 디스크 경보", "opening": "ruchba 디스크가 꽉 찼어요. 볼까요?"}
    placed, asked = auto_setup(g, monkeypatch, verdict)
    now = datetime.now(g.KST).replace(hour=10, minute=0)
    g.fake.now = lambda: now.timestamp()
    entry = asyncio.run(g.autonomous_tick(now, A))
    assert placed == [("autonomous", "ruchba 디스크가 꽉 찼어요. 볼까요?")] and entry["called"] and len(entry["call_id"]) == 32
    assert g.fake.db["plog"][-1]["call_id"] == entry["call_id"]  # the row names the call that rang
    assert "NodeDown" in asked[0]  # the signal reached the decision
    later = now + timedelta(hours=1)  # same reason within 2 h
    g.fake.now = lambda: later.timestamp()
    entry = asyncio.run(g.autonomous_tick(later, A))
    assert entry["blocked_by"] == "duplicate" and len(placed) == 1
    log = c.get("/v1/proactive/log").json()
    assert [r["called"] for r in log] == [False, True] and log[0]["blocked_by"] == "duplicate"
    assert log[1]["signals"]["alerts"][0].startswith("NodeDown")


def test_a_long_opening_is_said_in_full(gw, monkeypatch):
    g, c, _ = gw
    opening = ("대표님, 역사학특강 토론지 제출이 오늘 밤 11시 59분까지예요. 아직 제출 기록이 없어서 전화드렸어요. "
               "주제는 동아시아 근대화 비교였고, 분량은 A4 두 장 이상이에요. ") * 8  # well over the old 200 cut
    placed, _ = auto_setup(g, monkeypatch, {"call": True, "urgency": 5, "reason": "토론지 마감", "opening": opening})
    now = datetime.now(g.KST).replace(hour=10, minute=0)
    g.fake.now = lambda: now.timestamp()
    entry = asyncio.run(g.autonomous_tick(now, A))
    assert entry["called"] and placed == [("autonomous", opening)]  # nothing cut from what is said
    assert len(g.fake.db["plog"][-1]["opening"]) <= 500  # the log keeps a short copy (the deployed limit)
    assert "길어도 돼" in g.DECIDE_PROMPT and "짧은 한 구절" not in g.DECIDE_PROMPT


def test_autonomous_never_calls_outside_the_window(gw, monkeypatch):
    g, _, _ = gw
    placed, _ = auto_setup(g, monkeypatch, {"call": True, "urgency": 5, "reason": "급함", "opening": "급해요."})
    night = datetime.now(g.KST).replace(hour=2, minute=0)
    entry = asyncio.run(g.autonomous_tick(night, A))
    assert placed == [] and entry["blocked_by"] == "window" and g.fake.db["plog"][-1]["blocked_by"] == "window"


def test_autonomous_tick_only_when_enabled(gw, monkeypatch):
    g, c, _ = gw
    placed, asked = auto_setup(g, monkeypatch, {"call": True, "urgency": 5, "reason": "x", "opening": "x"})
    noon = datetime(2026, 11, 22, 12, 0, tzinfo=g.KST)
    asyncio.run(g.scheduler_tick(noon, {}))
    assert asked == [] and g.fake.db["plog"] == []  # autonomous is off by default
    c.put("/v1/settings", json={"proactive": {"autonomous": {"enabled": True}}})
    asyncio.run(g.scheduler_tick(noon.replace(minute=7), {}))
    assert asked == []  # only at :00 and :30
    asyncio.run(g.scheduler_tick(noon, {}))
    assert len(asked) == 1
    c.put("/v1/settings", json={"proactive": {"enabled": False}})  # master switch off
    asyncio.run(g.scheduler_tick(noon.replace(minute=30), {}))
    assert len(asked) == 1


def test_autonomous_fails_closed(gw, monkeypatch):
    g, _, _ = gw
    placed, _ = auto_setup(g, monkeypatch, {"call": True, "urgency": 5, "reason": "급함", "opening": "급해요."})
    ten = datetime.now(g.KST).replace(hour=10, minute=0)
    real_call = g.state.call

    async def no_log(method, path, body=None, **params):  # the log route answers 404
        if path == "/v1/state/proactive_log" and method == "GET":
            return None
        return await real_call(method, path, body, **params)
    g.state.call = no_log
    with pytest.raises(g.StateDown):
        asyncio.run(g.autonomous_tick(ten, A))
    assert placed == []

    async def write_fails(method, path, body=None, **params):  # cannot record the call: do not ring
        if path == "/v1/state/proactive_log" and method == "POST":
            raise g.StateDown("down")
        return await real_call(method, path, body, **params)
    g.state.call = write_fails
    with pytest.raises(g.StateDown):
        asyncio.run(g.autonomous_tick(ten, A))
    assert placed == []


def test_bad_qwen_output_never_calls(gw, monkeypatch):
    g, _, _ = gw
    for verdict in [None, "call", {"call": "true", "urgency": 5, "reason": "x"}, {"call": True, "urgency": "많이"},
                    {"call": True, "urgency": 4.9, "reason": "y"}, {"call": True, "urgency": "5", "reason": "z"},
                    {"call": True, "urgency": 9, "reason": "w"}, {"call": True, "urgency": True, "reason": "v"}]:
        placed, _ = auto_setup(g, monkeypatch, verdict)
        entry = asyncio.run(g.autonomous_tick(datetime.now(g.KST).replace(hour=10, minute=0), A))
        assert placed == [] and entry["blocked_by"] in ("no_call", "urgency"), verdict


def test_signals_and_checkpoint(gw, monkeypatch):
    g, c, _ = gw
    now = time.time()
    g.fake.db["approvals"]["a1"] = {"id": "a1", "device": "macbook", "command": "git push", "status": "pending",
                                    "created": now - 3600}
    g.fake.db["approvals"]["a2"] = {"id": "a2", "device": "macbook", "command": "ls", "status": "pending",
                                    "created": now - 60}
    g.fake.db["schedule"]["m1"] = {"id": "m1", "at": now - 900, "repeat": None, "kind": "wake", "note": "",
                                   "status": "missed", "attempts": 3, "call_id": "c", "last_attempt": now - 600}
    g.fake.db["context"]["t1"] = {"task_id": "t1", "summary": "치과 예약", "open_questions": ["몇 시?"]}
    g.fake.db["context"]["t2"] = {"task_id": "t2", "summary": "끝난 일", "open_questions": []}
    c.post("/v1/devices/register", json={"id": "macbook", "name": "macbook", "url": "http://x/mcp"})

    async def fake_mcp(url, method, params):
        return {"content": [{"type": "text", "text": json.dumps({"jobs": [
            {"job_id": "j1", "status": "failed", "task": "빌드", "finished": now - 120},
            {"job_id": "j2", "status": "running", "task": "테스트", "finished": None}]})}]}
    monkeypatch.setattr(g, "mcp", fake_mcp)

    async def fake_ask(*a, **k):
        return "[5] 토론지 — 내일 23시 — 아직 안 냄"
    monkeypatch.setattr(g, "ask_text", fake_ask)
    asyncio.run(g.run_checkpoint())
    sig = asyncio.run(g.gather_signals(now))
    assert sig["approvals"] == []  # an hour past its 5-minute expiry: expired, not "waiting"
    assert g.fake.db["approvals"]["a1"]["status"] == "expired"
    assert len(sig["missed_calls"]) == 1 and sig["missed_calls"][0].endswith("기상 전화 못 받음")
    assert sig["tasks"] == ["t1: 치과 예약 / 열린 질문: 몇 시?"]
    assert sig["device_jobs"] == ["macbook: 빌드 → failed"]
    assert sig["situation"] == ["[5] 토론지 — 내일 23시 — 아직 안 냄"]  # from azel-checkpoint (no brain note)
    assert "azel-situation" not in g.fake.db["context"] and "azel-checkpoint" in g.fake.db["context"]
    g.fake.db["context"]["azel-situation"] = {"task_id": "azel-situation", "summary": "브레인 메모",
                                              "open_questions": [], "updated": datetime.now(g.KST).isoformat()}
    assert asyncio.run(g.gather_signals(now))["situation"] == ["브레인 메모"]  # the brain's note wins when fresh
    g.fake.db["context"]["azel-situation"]["updated"] = (datetime.now(g.KST) - timedelta(hours=7)).isoformat()
    assert asyncio.run(g.gather_signals(now))["situation"] == ["[5] 토론지 — 내일 23시 — 아직 안 냄"]  # stale
    alert = {"alerts": [{"status": "firing", "labels": {"severity": "critical", "alertname": "DiskFull"}}]}
    c.put("/v1/settings", json={"proactive": {"server_alerts": False}})
    c.post("/v1/alerts", json=alert)  # no call, but remembered as a signal
    assert asyncio.run(g.gather_signals(time.time()))["alerts"][0].startswith("DiskFull")


def test_checkpoint_runs_at_its_times_with_claude_only(gw, monkeypatch):
    g, c, _ = gw
    ran = []

    async def fake_cp():
        ran.append(1)
    monkeypatch.setattr(g, "run_checkpoint", fake_cp)
    c.put("/v1/settings", json={"proactive": {"autonomous": {"enabled": True}}})
    asyncio.run(g.scheduler_tick(datetime(2026, 11, 22, 13, 0, tzinfo=g.KST), {}))
    asyncio.run(g.scheduler_tick(datetime(2026, 11, 22, 13, 1, tzinfo=g.KST), {}))
    assert ran == [1]
    monkeypatch.setattr(g, "CLAUDE_OK", False)
    asyncio.run(g.scheduler_tick(datetime(2026, 11, 22, 18, 0, tzinfo=g.KST), {}))
    assert ran == [1]


def test_briefing_context(gw):
    g, c, _ = gw
    sent = []

    async def capture(obj):
        sent.append(obj)
        return True
    g.link_send = capture
    asyncio.run(g.serve_briefing_context({"id": "b0"}))
    assert sent[-1] == {"id": "b0", "type": "briefing_context", "situation": "", "facts": ["최근 24시간 서버 경보 없음"]}
    g.fake.db["context"]["azel-situation"] = {"task_id": "azel-situation", "summary": "[4] 토론지 오늘 23시",
                                              "open_questions": [], "updated": datetime.now(g.KST).isoformat()}
    g.fake.db["approvals"]["a1"] = {"id": "a1", "device": "macbook", "command": "git push", "status": "pending",
                                    "created": time.time()}
    at = (datetime.now(g.KST) + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    c.post("/v1/schedule", json={"at": at.isoformat(), "kind": "wake"})
    asyncio.run(g.serve_briefing_context({"id": "b1"}))
    assert sent[-1]["situation"] == "[4] 토론지 오늘 23시"
    assert sent[-1]["facts"][:2] == ["승인 대기 1건", "다음 예약 전화: 내일 오전 7시 기상"]


def test_outbox_survives_concurrent_puts(gw):
    g, _, _ = gw

    async def two_at_once():  # two asks finishing together used to raise "pop from empty list"
        await asyncio.gather(*[g.record_usage({"model": "haiku", "path": "api", "cost_usd": 0.01}) for _ in range(5)])
    asyncio.run(two_at_once())
    assert len(g.fake.db["usage"]) == 5 and g.pending_usage == []


def test_resume_only_on_the_device_that_made_the_session(gw):
    g, c, tmp = gw
    key = g.session_key("s9", asyncio.run(g.load_settings()))  # session + persona/title signature
    g.fake.db["sessions"][key] = {"claude_session": "other", "device": "albireo"}
    c.post("/v1/ask", json={"text": "a", "session": "s9"})
    assert "--resume" not in json.loads((tmp / "args.json").read_text())
    assert g.fake.db["sessions"][key] == {"claude_session": "sess-1", "device": "macbook"}
    c.post("/v1/ask", json={"text": "b", "session": "s9"})
    args = json.loads((tmp / "args.json").read_text())
    assert args[args.index("--resume") + 1] == "sess-1"


def test_scheduled_job_fires_once_across_gateways(gw, monkeypatch):
    g, _, _ = gw
    ran = []

    async def fake_place(reason, opening=None, call_id=None, persona_id=None, **kw):
        ran.append(g.INSTANCE)
        return {"call_id": "x"}
    monkeypatch.setattr(g, "place_call", fake_place)
    g.fake.db["settings"] = {"proactive": {"deadline_check": {"enabled": False}}}
    now = datetime(2026, 11, 21, 8, 0, tzinfo=g.KST)
    for inst in ["macbook-aaa", "albireo-bbb", "gienah-ccc"]:  # three gateways tick in the same minute
        monkeypatch.setattr(g, "INSTANCE", inst)
        asyncio.run(g.scheduler_tick(now, {}))
    assert ran == ["macbook-aaa"]
    monkeypatch.setattr(g, "INSTANCE", "macbook-new")  # the winner restarts: still no second call
    asyncio.run(g.scheduler_tick(now, {}))
    assert ran == ["macbook-aaa"]
    g.fake.state.down = True  # no lease service: skip rather than risk several calls
    asyncio.run(g.scheduler_tick(datetime(2026, 11, 22, 8, 0, tzinfo=g.KST), {}))
    assert ran == ["macbook-aaa"]


def test_alert_limits(gw, monkeypatch):
    g, c, _ = gw
    placed = []

    async def fake_place(reason, opening=None, call_id=None, persona_id=None, **kw):
        placed.append(opening)
        return {"call_id": "x"}
    monkeypatch.setattr(g, "place_call", fake_place)
    fire = lambda *names: c.post("/v1/alerts", json={"alerts": [  # noqa: E731
        {"status": "firing", "labels": {"severity": "critical", "alertname": n}} for n in names]}).json()
    assert fire("NodeDown")["called"]
    for _ in range(5):  # flapping: re-fires every few minutes
        assert not fire("NodeDown")["called"]
    assert fire("NodeDown", "DiskFull")["called"] and "DiskFull" in placed[-1] and "NodeDown" not in placed[-1]
    assert fire("TLSExpiry")["called"]  # third call today
    assert not fire("BackupFailed")["called"]  # cap: 3 per rolling 24 h
    assert len(placed) == 3
    g.fake.db["kv"]["alert_calls"] = [time.time() - 90000] * 3  # a day later
    g.fake.db["leases"].clear()
    assert fire("BackupFailed")["called"]
    g.fake.state.down = True  # ruchba down: the same limits from this gateway's memory
    assert fire("RuchbaDown")["called"] and not fire("RuchbaDown")["called"]


def test_alert_calls_once_across_gateways(gw, monkeypatch):
    g, c, _ = gw
    body = {"alerts": [{"status": "firing", "labels": {"severity": "critical", "alertname": "NodeDown"}}]}
    assert c.post("/v1/alerts", json=body).json()["called"]
    monkeypatch.setattr(g, "INSTANCE", "albireo-x")
    assert not c.post("/v1/alerts", json=body).json()["called"]


# ---------- limited mode (team devices) ----------
def private(path, text):
    path.write_text(text)
    os.chmod(path, 0o600)


@pytest.fixture
def limited(tmp_path, monkeypatch):
    private(tmp_path / "anthropic_api_key", "sk-test\n")
    private(tmp_path / "state_token_limited", "l1m\n")
    g = start(tmp_path, monkeypatch, BISEO_MODE="limited", BISEO_DEVICE="spark")
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        yield g, c, tmp_path


def test_limited_key_file_must_be_private(tmp_path, monkeypatch):
    (tmp_path / "anthropic_api_key").write_text("sk-test\n")
    os.chmod(tmp_path / "anthropic_api_key", 0o644)
    with pytest.raises(AssertionError, match="chmod 600"):
        start(tmp_path, monkeypatch, BISEO_MODE="limited")
    os.unlink(tmp_path / "anthropic_api_key")
    with pytest.raises(AssertionError, match="limited mode needs"):
        start(tmp_path, monkeypatch, BISEO_MODE="limited")
    private(tmp_path / "anthropic_api_key", "sk-test\n")
    with pytest.raises(AssertionError, match="state_token_limited"):
        start(tmp_path, monkeypatch, BISEO_MODE="limited")


def test_tokens_per_mode(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch)
    assert g.LINK_TOKEN == "t0k" and g.STATE_TOKEN == "t0k"  # before link tokens exist: the shared token
    private(tmp_path / "link_token", "l1nk\n")
    private(tmp_path / "anthropic_api_key", "sk-test\n")
    private(tmp_path / "state_token_limited", "l1m\n")
    g = start(tmp_path, monkeypatch, BISEO_MODE="limited")
    assert g.LINK_TOKEN == "l1nk" and g.STATE_TOKEN == "l1m"
    assert g.State("http://x").client.headers["authorization"] == "Bearer l1m"
    os.chmod(tmp_path / "link_token", 0o644)
    with pytest.raises(AssertionError, match="chmod 600"):
        start(tmp_path, monkeypatch)


def test_limited_has_no_call_history(limited):
    g, c, _ = limited
    for method in ("get", "post", "delete"):  # wake-word enrollment is not mounted on a limited gateway
        assert getattr(c, method)("/v1/wake/enroll").status_code in (404, 405), method
    for method, path in [("get", "/v1/history"), ("get", "/v1/history/L1"), ("delete", "/v1/history/L1"),
                         ("delete", "/v1/history?older_than_days=30"), ("get", "/v1/schedule"),
                         ("get", "/v1/proactive/log"), ("get", "/v1/personas"), ("delete", "/v1/personas/azel"),
                         ("delete", "/v1/schedule/s1")]:
        assert getattr(c, method)(path).status_code == 403, path
    asyncio.run(g.handle_link(LinkWS(CALL_LOG)))
    assert g.fake.db["history"] == {} and g.history_outbox.items == []  # never stored, never queued


def test_limited_never_seeds_settings(tmp_path, monkeypatch):
    private(tmp_path / "anthropic_api_key", "sk-test\n")
    private(tmp_path / "state_token_limited", "l1m\n")
    (tmp_path / "settings.json").write_text(json.dumps({"monthly_budget_usd": 1}))
    g = start(tmp_path, monkeypatch, BISEO_MODE="limited", BISEO_DEVICE="spark")
    puts = []
    real = g.state.call

    async def spy(method, path, body=None, **params):
        puts.append((method, path))
        return await real(method, path, body, **params)
    g.state.call = spy
    s = asyncio.run(g.load_settings())
    assert ("PUT", "/v1/state/settings") not in puts and g.fake.db["settings"] == {}
    assert s["monthly_budget_usd"] == 500  # the shared (empty) settings, not this host's old copy


def test_limited_refuses_personal_data(limited, monkeypatch):
    g, c, tmp = limited

    async def fake_anthropic(body):
        return {"content": [{"type": "text", "text": "1400원이에요."}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1}}
    monkeypatch.setattr(g, "anthropic", fake_anthropic)
    for text, task in [("내일 일정 알려줘", None), ("메일 왔어?", None), ("뭐든", "schedule"), ("GISTRA 과제", None)]:
        ev = events(c.post("/v1/ask", json={"text": text, "task": task}))
        assert ev == [{"type": "error", "code": "personal", "message": "개인 비서 기능은 지금 쓸 수 없어요."}]
    ok = events(c.post("/v1/ask", json={"text": "최근 통화 내용:\n사용자: 내일 일정 알려줘\n\n고마워, 근데 환율은?"}))
    assert ok[0]["type"] == "activity" and ok[0]["phase"] == "thinking"
    assert ok[1] == {"type": "text", "delta": "1400원이에요.\n"}  # personal words in history alone do not refuse
    assert c.post("/v1/calls", json={"reason": "manual"}).status_code == 403
    assert not (tmp / "args.json").exists()  # `claude -p` never ran


def test_limited_uses_api_with_own_daemon_only(limited, monkeypatch):
    g, c, tmp = limited
    c.post("/v1/devices/register", json={"id": "spark", "name": "spark", "url": "http://spark/mcp"})
    g.fake.db["devices"]["macbook"] = {"id": "macbook", "name": "macbook", "url": "http://mac/mcp", "harness": [],
                                       "gateway": "macbook", "last_seen": 9e12}
    rpc, bodies = [], []

    async def fake_mcp(url, method, params):
        rpc.append((url, method, params))
        if method == "tools/list":
            return {"tools": [{"name": "list_dir", "description": "ls", "inputSchema": {"type": "object"}}]}
        return {"content": [{"type": "text", "text": "a.txt b.txt"}], "isError": False}

    async def fake_anthropic(body):
        bodies.append(json.loads(json.dumps(body)))
        if len(bodies) == 1:
            return {"content": [{"type": "text", "text": "볼게요."}, {"type": "tool_use", "id": "t1",
                    "name": "spark__list_dir", "input": {"path": "~"}}], "stop_reason": "tool_use",
                    "usage": {"input_tokens": 1000, "output_tokens": 100}}
        return {"content": [{"type": "text", "text": "파일이 두 개 있어요."}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 1000, "output_tokens": 100}}
    monkeypatch.setattr(g, "mcp", fake_mcp)
    monkeypatch.setattr(g, "anthropic", fake_anthropic)
    ev = events(c.post("/v1/ask", json={"text": "spark 홈 폴더에 뭐 있어?", "model": "sonnet", "path": "claude_code"}))
    assert "".join(e["delta"] for e in ev if e["type"] == "text") == "볼게요.\n파일이 두 개 있어요.\n"
    assert ev[-1]["path"] == "api" and ev[-1]["cost_usd"] == pytest.approx((2000 * 2 + 200 * 10) / 1e6)
    assert {u for u, _, _ in rpc} == {"http://spark/mcp"}  # never the Mac's daemon
    assert ("http://spark/mcp", "tools/call", {"name": "list_dir", "arguments": {"path": "~"}}) in rpc
    assert [t["name"] for t in bodies[0]["tools"]] == ["spark__list_dir"]
    assert bodies[0]["model"] == "claude-sonnet-5-5" and "개인 데이터" in bodies[0]["system"]
    assert bodies[1]["messages"][-1]["content"][0] == {"type": "tool_result", "tool_use_id": "t1",
                                                       "content": "a.txt b.txt", "is_error": False}
    assert not (tmp / "args.json").exists()


def test_mcp_client_parses_sse_and_json(gw, monkeypatch):
    g, _, _ = gw
    answers = iter([
        httpx.Response(200, headers={"content-type": "text/event-stream"},
                       text='event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\n'),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"message": "nope"}})])
    real = httpx.AsyncClient
    monkeypatch.setattr(g.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda r: next(answers))))
    assert asyncio.run(g.mcp("http://d/mcp", "tools/list", {})) == {"tools": []}
    with pytest.raises(RuntimeError, match="nope"):
        asyncio.run(g.mcp("http://d/mcp", "tools/list", {}))


def test_warm_pool_race_cap_displace_and_call_end(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch, BISEO_WARM_CLAUDE="1", BISEO_CLAUDE=str(HERE / "fake_claude_warm.py"))
    import claude_warm
    monkeypatch.setattr(claude_warm, "MAX_PROCS", 2)

    def spawned():
        return len((tmp_path / "args.json").read_text().splitlines()) if (tmp_path / "args.json").exists() else 0

    async def run():
        await asyncio.gather(g.prewarm("azel:c1"), g.ask_text("안녕", "azel:c1"))  # prewarm races the first ask
        n_race = spawned()
        first = g.warm_pool.procs["azel:c1"]
        await g.ask_text("둘", "azel:c2")
        await g.ask_text("셋", "azel:c3")  # cap 2: the least recently used (c1) goes, closed
        capped = (sorted(g.warm_pool.procs), first.proc.returncode is not None)
        w = g.warm_pool.procs["azel:c3"]
        cfg = w.cfg
        settings = await g.load_settings()
        settings["assistant_name"] = "새이름"  # a different prompt: a new process, the old one closed with its config
        g.fake.db["settings"] = {**g.fake.db["settings"], "assistant_name": "새이름"}
        await g.ask_text("넷", "azel:c3")
        displaced = (w.proc.returncode is not None, cfg is None or not os.path.exists(cfg))
        await g.handle_link(LinkWS([{"type": "call_log", "event": "start", "log_id": "L9", "session": "s9",
                                     "persona_id": "azel"}]))
        await asyncio.sleep(0.3)  # the prewarm task runs
        warm9 = "azel:s9" in g.warm_pool.procs
        await g.handle_link(LinkWS([{"type": "call_log", "event": "end", "log_id": "L9", "ended_at": 1.0}]))
        await asyncio.sleep(0.1)
        closed9 = "azel:s9" not in g.warm_pool.procs
        await g.warm_pool.close_all()
        return n_race, capped, displaced, warm9, closed9
    n_race, capped, displaced, warm9, closed9 = asyncio.run(run())
    assert n_race == 1  # one process, not two
    assert capped == (["azel:c2", "azel:c3"], True)
    assert displaced == (True, True)
    assert warm9 and closed9  # prewarmed at the call's start under the asks' key, closed at its end


def test_warm_turn_timeout_kills_a_hung_process(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch, BISEO_WARM_CLAUDE="1", BISEO_CLAUDE=str(HERE / "fake_claude_warm.py"))
    import claude_warm
    monkeypatch.setattr(claude_warm, "TURN_IDLE_S", 0.5)

    async def run():
        with pytest.raises(RuntimeError, match="nothing for too long"):  # serve_ask turns it into an error event
            [e async for e in g.ask_stream("멈춤", "azel:h1")]  # the fake never answers
        w = g.warm_pool.procs.get("azel:h1")
        dead = w is None or w.proc.returncode is not None
        await g.warm_pool.close_all()
        return dead
    assert asyncio.run(run())


def test_prewarm_uses_the_call_persona(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch, BISEO_WARM_CLAUDE="1", BISEO_CLAUDE=str(HERE / "fake_claude_warm.py"))
    g.fake.db["settings"] = {"personas": [{"id": "azel", "name": "아젤", "voice": "sohee", "primary": True},
                                          {"id": "luna", "name": "루나", "voice": "aiden", "personality": "느긋함"}]}

    async def run():
        await g.prewarm("luna:s1", "luna")
        style = g.warm_pool.procs["luna:s1"].style
        await g.warm_pool.close_all()
        return style
    style = asyncio.run(run())
    assert "'루나'" in style and "느긋함" in style


def test_warm_claude_keeps_one_process_per_session(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch, BISEO_WARM_CLAUDE="1", BISEO_CLAUDE=str(HERE / "fake_claude_warm.py"))

    async def run():
        await g.prewarm("call-1")  # the call's start: spawned before the first question
        said = [await g.ask_text("안녕", "call-1"), await g.ask_text("또 안녕", "call-1"), await g.ask_text("다른 통화", "call-2")]
        pids = [json.loads(line)["pid"] for line in (tmp_path / "args.json").read_text().splitlines()]
        argv = json.loads((tmp_path / "args.json").read_text().splitlines()[0])["argv"]
        gen = g.ask_stream("느린 질문", "call-1")  # barge-in mid-turn: the process drains and stays usable
        async for ev in gen:
            break
        await gen.aclose()
        t = time.perf_counter()
        again = await g.ask_text("다시", "call-1")  # right after the barge-in: not stuck behind the slow answer
        again_s = time.perf_counter() - t
        lookup = await g.ask_text("서버 상태 괜찮아?", "call-1")  # Haiku by the lookup rule, same process
        n_procs = len((tmp_path / "args.json").read_text().splitlines())
        await g.warm_pool.close_all()
        return said, pids, argv, again, again_s, lookup, n_procs
    said, pids, argv, again, again_s, lookup, n_procs = asyncio.run(run())
    assert said == ["네, 1번째예요.", "네, 2번째예요.", "네, 1번째예요."]
    assert len(pids) == 2 and "--input-format" in argv and "stream-json" in argv
    assert again == "네, 4번째예요." and again_s < 0.5 and n_procs == 2, again_s  # interrupted, same process
    ctl = [json.loads(x) for x in (tmp_path / "args.json.ctl").read_text().splitlines()]
    assert {"subtype": "interrupt"} in ctl and {"subtype": "set_model", "model": "claude-haiku-5-5"} in ctl
    assert lookup == "네, 5번째예요."
    assert g.resolve(g.DEFAULTS, None, None, "chat", "서버 상태 괜찮아?")[0] == "haiku"
    assert g.resolve(g.DEFAULTS, None, None, "chat", "이번 주 계획 짜 줘")[0] == "sonnet"
    assert g.resolve(g.DEFAULTS, "opus", None, "chat", "서버 상태 괜찮아?")[0] == "opus"  # a forced model wins
    assert g.resolve(g.DEFAULTS, None, None, "schedule", "오늘 일정 뭐 있어?")[0] == "sonnet"  # task_defaults win
    assert g.fake.db["sessions"][g.session_key("call-1", asyncio.run(g.load_settings()))]["claude_session"] == "warm-1"


def run_settings(tmp):
    args = json.loads((tmp / "args.json").read_text())
    return args, json.loads(args[args.index("--settings") + 1])


def test_runs_load_only_enabled_connectors(gw):
    g, c, tmp = gw
    c.post("/v1/ask", json={"text": "a", "session": "s1"})
    args, st = run_settings(tmp)
    urls = {a["serverUrl"] for a in st["allowedMcpServers"]}
    azel = "http://100.64.0.8:8892/v1/mcp"  # the gateway's own call_user / create_task server
    assert urls == {"https://mcp.example/cygnus", "https://gmail.example/mcp", "https://cal.example/mcp", azel}  # defaults
    assert "mcp__azel" in args
    assert st["enabledPlugins"] == {"figma@official": False, "legal@x": False}
    assert "mcp__claude_ai_Gmail" in args and "mcp__claude_ai_Notion" not in args
    r = c.put("/v1/connectors", json={"claude_ai": [{"name": "Gmail", "enabled": False}, {"name": "Notion", "enabled": True}],
                                      "custom": [{"name": "wiki", "url": "https://wiki.example/mcp"},
                                                 {"name": "off", "url": "https://off.example/mcp", "enabled": False}]})
    assert r.status_code == 200
    view = {x["name"]: x for x in r.json()["claude_ai"]}
    assert view["Notion"]["enabled"] and not view["Gmail"]["enabled"] and view["Slack"]["status"] == "needs_auth"
    assert r.json()["custom"][0]["has_secret"] is False
    c.post("/v1/ask", json={"text": "b", "session": "s2"})
    args, st = run_settings(tmp)
    urls = {a["serverUrl"] for a in st["allowedMcpServers"]}
    assert urls == {"https://mcp.example/cygnus", "https://cal.example/mcp", "https://notion.example/mcp", azel}
    assert "mcp__claude_ai_Notion" in args and "mcp__wiki" not in args  # no secret yet: the server is skipped


def test_connector_allowed_tools_round_trip(gw):
    g, c, tmp = gw
    r = c.put("/v1/connectors", json={"custom": [{"name": "ha", "url": "https://ha.example/api/mcp", "allowed_tools": ["Get*"]}]})
    assert r.json()["custom"][0]["allowed_tools"] == ["Get*"]
    assert c.put("/v1/connectors", json={"custom": [{"name": "ha", "url": "https://ha.example/api/mcp",
                                                     "allowed_tools": ["a b"]}]}).status_code == 400
    c.put("/v1/connectors", json={"custom": [{"name": "ha", "url": "https://ha.example/api/mcp"}]})  # an older app
    assert c.get("/v1/connectors").json()["custom"][0]["allowed_tools"] == ["Get*"]  # kept, not wiped
    c.put("/v1/connectors", json={"custom": [{"name": "ha", "url": "https://ha.example/api/mcp", "allowed_tools": []}]})
    assert c.get("/v1/connectors").json()["custom"][0]["allowed_tools"] == []


def test_connector_secret_is_write_only(gw):
    g, c, tmp = gw
    c.put("/v1/connectors", json={"custom": [{"name": "wiki", "url": "https://wiki.example/mcp"}]})
    assert c.put("/v1/connectors/secret/wiki", json={"value": "SECRET-TOKEN"}).status_code == 204
    f = tmp / "mcp" / "wiki"
    assert f.read_text() == "SECRET-TOKEN" and f.stat().st_mode & 0o777 == 0o600
    for path in ("/v1/connectors", "/v1/settings"):
        assert "SECRET-TOKEN" not in c.get(path).text
    assert c.get("/v1/connectors").json()["custom"][0]["has_secret"] is True
    seen = {}

    async def spy(text, session, **kw):  # the generated --mcp-config carries the header to Claude only
        servers, allowed, *_ = g.run_args(await g.load_settings(), [])
        seen.update(servers=servers, allowed=allowed)
    asyncio.run(spy("x", "s"))
    assert seen["servers"]["wiki"]["headers"] == {"Authorization": "Bearer SECRET-TOKEN"} and "mcp__wiki" in seen["allowed"]
    for bad in ("../x", "a b", ""):
        assert c.put(f"/v1/connectors/secret/{bad}", json={"value": "v"}).status_code in (400, 404, 405)
    assert c.put("/v1/connectors/secret/wiki", json={"value": ""}).status_code == 400


def test_connectors_validation(gw):
    g, c, _ = gw
    ok = {"custom": [{"name": "tail", "url": "http://100.64.0.12:9000/mcp"}, {"name": "pub", "url": "https://x.example/mcp"}]}
    assert c.put("/v1/connectors", json=ok).status_code == 200  # http only to a tailnet address
    for url in ["http://x.example/mcp", "http://192.168.1.5/mcp", "http://100.128.0.1/mcp", "http://localhost/mcp"]:
        assert c.put("/v1/connectors", json={"custom": [{"name": "a", "url": url}]}).status_code == 400, url
    g.fake.db["settings"] = {**g.fake.db.get("settings", {})}
    # a server may only use its own secret (auth_ref = its name), even through the settings route
    assert c.put("/v1/settings", json={"connectors": {"custom": [{"name": "evil", "url": "https://e.example/mcp",
                                                                   "auth_ref": "pub"}]}}).status_code == 400
    for bad in [{"claude_ai": [{"name": "Gmail", "enabled": "yes"}]}, {"custom": [{"name": "a b", "url": "https://x"}]},
                {"custom": [{"name": "a", "url": "ftp://x"}]}, {"custom": [{"name": "a", "url": "https://x", "token": "t"}]},
                {"custom": [{"name": "a", "url": "https://x"}, {"name": "a", "url": "https://y"}]}, {"other": 1}]:
        assert c.put("/v1/connectors", json=bad).status_code == 400, bad


def test_limited_has_no_connectors(limited):
    g, c, tmp = limited
    assert c.get("/v1/connectors").status_code == 403
    assert c.put("/v1/connectors", json={"claude_ai": {}}).status_code == 403
    assert c.put("/v1/connectors/secret/x", json={"value": "v"}).status_code == 403
    assert c.put("/v1/settings", json={"connectors": {"claude_ai": {}}}).status_code == 403
    assert not (tmp / "mcp").exists()


def test_api_key_alone_never_moves_a_full_gateway_off_claude_code(tmp_path, monkeypatch):
    private(tmp_path / "anthropic_api_key", "sk-test\n")
    g = start(tmp_path, monkeypatch)
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        assert g.resolve(g.DEFAULTS, None, None, "chat", "안녕")[1] == "claude_code"  # "auto" = claude -p
        assert g.resolve(g.DEFAULTS, None, "api", "chat", "안녕")[1] == "api"  # explicit opt-in
        c.post("/v1/ask", json={"text": "a", "session": "s1"})
        assert (tmp_path / "args.json").exists()  # the claude -p fake ran


def test_full_api_path_uses_caching_bridge_and_device_tools(tmp_path, monkeypatch):
    private(tmp_path / "anthropic_api_key", "sk-test\n")
    g = start(tmp_path, monkeypatch)
    seen = {}

    async def fake_ask(key, model, system, text, tools, run):
        seen.update(key=key, model=model, system=system, tools=[t["name"] for t in tools])
        seen["result"] = await run("macbook__list_dir", {"path": "~"})
        yield "tool", "macbook__list_dir"
        yield "text", "홈 폴더에 파일이 세 개 있어요.\n"
        yield "done", {"input_tokens": 100, "cache_read_input_tokens": 1500, "cache_creation_input_tokens": 0,
                       "output_tokens": 20, "cost_usd": 0.0001}

    async def dev_tools(devices):
        return [{"name": "macbook__list_dir", "description": "x", "input_schema": {"type": "object"}}], \
            {"macbook__list_dir": ("http://d", "list_dir")}

    async def fake_run_tool(block, where):
        return {"type": "tool_result", "tool_use_id": block["id"], "content": "a b c", "is_error": False}
    monkeypatch.setattr(g.claude_api, "ask", fake_ask)
    monkeypatch.setattr(g, "device_tools", dev_tools)
    monkeypatch.setattr(g, "run_tool", fake_run_tool)
    monkeypatch.setattr(g.claude_api, "cygnus_bridge", lambda home: ([], None))  # no PAT in tests
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        evs = events(c.post("/v1/ask", json={"text": "홈 폴더 봐 줘", "session": "s", "path": "api", "model": "haiku"}))
    assert seen["key"] == "sk-test" and seen["model"] == "haiku" and seen["tools"] == ["macbook__list_dir", "call_user", "schedule_list", "schedule_cancel", "schedule_update", "create_task", "task_status", "task_cancel", "overnight_list", "recent_calls", "approval_status", "memory_recall", "people_lookup", "remember", "forget", "memory_list", "agenda", "calendar_write", "weather", "stock_quote", "portfolio", "holdings_update", "web_search", "web_fetch", "music_play", "music_control", "home_scene", "auto_approve", "web_task", *(t["name"] for t in g.tools_bridge.P.TOOLS), "end_call"]
    assert "Cygnus 도구" in seen["system"] and g.PERSONAL_REFUSAL not in seen["system"] and seen["result"] == "a b c"
    assert [e["phase"] for e in evs if e["type"] == "activity"] == ["thinking", "tool_start"]
    done = evs[-1]
    assert done["type"] == "done" and done["path"] == "api" and done["input_tokens"] == 1600


def test_user_title_shared_and_per_persona(gw, monkeypatch):
    g, c, _ = gw
    persona_gw(g, monkeypatch)
    assert c.get("/v1/settings").json()["user_title"] == ""
    assert "부른다" not in g.persona_block(asyncio.run(g.load_settings()))  # none set: no line
    assert c.put("/v1/settings", json={"user_title": "  선생 님 "}).json()["user_title"] == "선생 님"
    for bad in ["a" * 13, "주인님!", 3]:
        assert c.put("/v1/settings", json={"user_title": bad}).status_code == 400, bad
    assert c.put("/v1/settings", json={"user_title": "선생님"}).status_code == 200
    s = asyncio.run(g.load_settings())
    assert "사용자를 '선생님'(이)라고 부른다." in g.persona_block(s)
    r = c.post("/v1/personas", json={"id": "luna", "name": "루나", "voice": "sohee"})
    assert r.json()["user_title"] == ""  # "" = use the shared one
    assert c.put("/v1/personas/luna", json={"user_title": "주인님"}).json()["user_title"] == "주인님"
    assert c.put("/v1/personas/luna", json={"user_title": "!!"}).status_code == 400
    s = asyncio.run(g.load_settings())
    g.current_persona.set("luna")
    assert "사용자를 '주인님'(이)라고 부른다." in g.persona_block(s)
    assert c.put("/v1/personas/luna", json={"user_title": None}).json()["user_title"] == ""  # null: unset
    r = c.post("/v1/personas", json={"id": "nul", "name": "널", "voice": "sohee", "user_title": None, "color": None})
    assert r.status_code == 201 and r.json()["user_title"] == "" and r.json()["color"] == "celadon"  # create: defaults
    assert "사용자를 '선생님'(이)라고 부른다." in g.persona_block(asyncio.run(g.load_settings()))
    g.current_persona.set(None)
    stored = g.fake.db["settings"]
    assert stored["user_title"] == "선생님" and next(p for p in stored["personas"] if p["id"] == "luna")["user_title"] == ""


def test_persona_change_reaches_the_voice_server_and_restarts_the_conversation(gw, monkeypatch, tmp_path):
    g, c, tmp = gw
    persona_gw(g, monkeypatch)
    sent = []

    async def capture(obj):
        sent.append(obj)
        return True
    monkeypatch.setattr(g, "link_send", capture)
    c.post("/v1/personas", json={"id": "mia", "name": "미아", "voice": "sohee", "user_title": "정훈님"})
    assert c.put("/v1/personas/mia", json={"user_title": "오빠"}).status_code == 200
    pushed = [m for m in sent if m.get("type") == "settings"][-1]["value"]
    assert next(p for p in pushed["personas"] if p["id"] == "mia")["user_title"] == "오빠"  # at once, over the link
    # Same voice session all day: a new title starts a fresh Claude conversation, an unchanged one resumes.
    s = asyncio.run(g.load_settings())
    g.current_persona.set("mia")
    k1 = g.session_key("mia:iphone-2026-11-22", s)
    s["personas"][[p["id"] for p in s["personas"]].index("mia")]["user_title"] = "정훈님"
    assert g.session_key("mia:iphone-2026-11-22", s) != k1
    g.current_persona.set(None)

    def ask(text):
        events(c.post("/v1/ask", json={"text": text, "session": "mia:iphone-2026-11-22"}))
        return json.loads((tmp / "args.json").read_text())
    asyncio.run(g.state.call("PUT", "/v1/state/settings", {**asyncio.run(g.load_settings())}))
    ask("안녕")
    assert "--resume" in ask("또")  # nothing changed: resumed
    c.put("/v1/settings", json={"user_title": "대표님"})  # the shared title (azel has none of its own)
    assert "--resume" not in ask("다시")  # title changed: a fresh conversation


def test_web_tools_only_on_full_gateways(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch, BISEO_DEVICE="macbook")
    _, allowed, _, _ = g.run_args(asyncio.run(g.load_settings()), [])
    assert {"WebSearch", "WebFetch"} <= set(allowed)
    for name, text in (("anthropic_api_key", "sk-test\n"), ("state_token_limited", "l1m\n")):
        (tmp_path / name).write_text(text)
        (tmp_path / name).chmod(0o600)
    lim = start(tmp_path, monkeypatch, BISEO_MODE="limited", BISEO_DEVICE="spark")
    _, allowed, _, _ = lim.run_args(asyncio.run(lim.load_settings()), [])
    assert not {"WebSearch", "WebFetch"} & set(allowed)
    args = lim.base_cmd("m", "s", allowed)
    assert "WebSearch" not in args[args.index("--allowedTools"):]


def test_night_rule_reaches_every_prompt(gw, monkeypatch):
    """name_prompt builds every Claude/API/NIM/task prompt: at night they all read "내일" as today."""
    g, c, _ = gw
    s = asyncio.run(g.load_settings())
    monkeypatch.setattr(g, "night_rule", lambda: "\nNIGHT-RULE\n")
    assert "NIGHT-RULE" in g.name_prompt(s) and "NIGHT-RULE" in g.task_api_system(s, [])
    monkeypatch.undo()
    night = g.datetime(2026, 11, 22, 1, 30, tzinfo=g.KST)
    assert "'내일'은 오늘(11월 22일)" in g.late_night.prompt_rule(night)


def test_every_placed_call_opens_with_the_title(gw, monkeypatch):
    g, c, _ = gw
    persona_gw(g, monkeypatch)
    sent = []

    async def capture(obj):
        sent.append(obj)
        return True

    async def no_push(payload):
        return {"sent": 0}
    monkeypatch.setattr(g, "link_send", capture)
    monkeypatch.setattr(g, "send_voip_push", no_push)
    c.put("/v1/settings", json={"user_title": "정훈님"})
    c.post("/v1/personas", json={"id": "mia", "name": "미아", "voice": "sohee", "user_title": "오빠"})
    asyncio.run(g.place_call("deadline", "마감이 다가오는 게 있어요. 과제가 내일까지예요."))  # a model-written opening
    asyncio.run(g.place_call("user_request", "약 드실 시간이에요.", persona_id="mia"))
    asyncio.run(g.place_call("task", "정훈님, 맡기신 정리 끝났어요."))  # already has it: unchanged
    openings = [m["opening"] for m in sent if m.get("type") == "call"]
    assert openings == ["정훈님, 마감이 다가오는 게 있어요. 과제가 내일까지예요.", "오빠, 약 드실 시간이에요.",
                        "정훈님, 맡기신 정리 끝났어요."], openings


def test_speech_style_follows_the_persona(gw):
    """A persona whose personality asks for 반말 is not overruled by a fixed 존댓말 line (존댓말 stays the default)."""
    g, c, _ = gw
    for text in (g.VOICE_STYLE, g.FULL_API_STYLE, g.NIM_STYLE):
        assert "말투(존댓말/반말)는 비서 성격을 따르고" in text and "반드시 존댓말" not in text and "한국어 존댓말로" not in text


def test_main_block_is_last():
    """`python gateway.py` blocks in uvicorn.run: nothing may be defined after the __main__ block (batch 5 outage)."""
    import ast, pathlib
    tree = ast.parse(pathlib.Path(__file__).resolve().parents[1].joinpath("gateway.py").read_text())
    last = tree.body[-1]
    assert isinstance(last, ast.If) and "__main__" in ast.unparse(last.test)


def test_oauth_routes_consent_through_the_gateway_callback(gw, monkeypatch):
    """authorize -> the browser's redirect to GET /v1/oauth/callback (no token) -> connected; tokens never leave."""
    import base64 as b64
    import hashlib
    from urllib.parse import parse_qs, urlsplit
    g, c, tmp = gw
    seen = {}

    def provider(req):
        f = parse_qs(req.content.decode())
        want = b64.urlsafe_b64encode(hashlib.sha256(f["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
        if f["code"] != ["abc"] or want != seen["challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"access_token": "SECRET-AT", "refresh_token": "SECRET-RT", "expires_in": 3600})
    real = httpx.AsyncClient
    monkeypatch.setattr(g.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(provider)))
    assert c.post("/v1/connectors/nope/authorize", json={}).status_code == 404
    assert c.post("/v1/connectors/spotify/authorize", json={}).status_code == 400  # no client id yet
    a = c.post("/v1/connectors/spotify/authorize", json={"client_id": "cid", "redirect_uri": g.gateway_callback()[0]}).json()
    seen["challenge"] = parse_qs(urlsplit(a["url"]).query)["code_challenge"][0]
    r = c.get(f"/v1/oauth/callback?code=abc&state={a['state']}", headers={"Authorization": ""})
    assert r.status_code == 200 and "연결을 마쳤어요" in r.text and "abc" not in r.text
    assert c.get(f"/v1/oauth/callback?code=abc&state={a['state']}", headers={"Authorization": ""}).status_code == 400  # replay
    got = c.get("/v1/connectors").json()
    assert got["oauth"] == {"spotify": "connected"} and c.get("/v1/connectors/spotify/oauth").json()["status"] == "connected"
    assert not any(s in json.dumps(got) + json.dumps(a) for s in ("SECRET-AT", "SECRET-RT"))
    assert c.get("/v1/settings", headers={"Authorization": ""}).status_code == 401  # the exemption is the callback only
    b = c.post("/v1/connectors/spotify/authorize", json={}).json()  # the stored client id is reused
    assert parse_qs(urlsplit(b["url"]).query)["client_id"] == ["cid"]
    assert c.post("/v1/connectors/spotify/authorize/finish", json={"code": "x", "state": "bogus"}).status_code == 400
    assert c.delete("/v1/connectors/spotify/oauth").status_code == 204
    assert c.get("/v1/connectors/spotify/oauth").json()["status"] == "needs_consent"



def test_claude_runs_deny_daemon_music_and_home_and_secret_runs_have_no_connectors(gw):
    g, c, tmp = gw
    devs = [{"id": "macbook", "url": "http://100.64.0.8:8765/mcp"}]
    st = {"connectors": {"claude_ai": {"Gmail": True}, "custom": []}}
    for req in ("ask:azel:call-1", "task:abcabcabcabc", ""):
        _, _, rs, _ = g.run_args(st, devs, req)
        deny = json.loads(rs)["permissions"]["deny"]
        assert "mcp__macbook__music" in deny and "mcp__macbook__home" in deny, req
    servers, allowed, rs, _ = g.run_args(st, devs, "ask:azel:secret-0123456789ab")
    assert not any(a.startswith("mcp__claude_ai_") for a in allowed) and set(servers) <= {"macbook", "azel"}
    assert all("claude_ai" not in json.dumps(x) for x in json.loads(rs).get("allowedMcpServers", []))


def test_moving_an_oauth_connector_forgets_its_grant(gw):
    g, c, tmp = gw
    g.oauth.save(tmp, "notes", {"access_token": "SECRET-AT", "expires_at": time.time() + 600, "client_id": "c",
                                "token_endpoint": "https://auth.example.com/token", "server_origin": "https://mcp.example.com"})
    body = {"claude_ai": [], "custom": [{"name": "notes", "url": "https://mcp.example.com/mcp", "oauth": True}]}
    assert c.put("/v1/connectors", json=body).status_code == 200 and g.oauth.load(tmp, "notes")  # unchanged: kept
    body["custom"][0]["url"] = "https://other.example.com/mcp"
    assert c.put("/v1/connectors", json=body).status_code == 200 and g.oauth.load(tmp, "notes") is None
    assert c.put("/v1/connectors", json={"claude_ai": [], "custom": [{"name": "spotify", "url": "https://x.example/mcp"}]}).status_code == 400


def test_state_retries_an_idempotent_request_once_on_a_dropped_connection(gw):
    g, _, _ = gw
    import httpx
    seen = []

    def handler(req):
        seen.append(req.method)
        if len(seen) == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, json={"ok": True})
    st = g.State("http://state")
    st.client = httpx.AsyncClient(base_url="http://state", transport=httpx.MockTransport(handler))
    assert asyncio.run(st.call("GET", "/v1/x")) == {"ok": True} and seen == ["GET", "GET"]
    seen.clear()
    with pytest.raises(g.StateDown):  # a POST may already have run: it is not sent again
        asyncio.run(st.call("POST", "/v1/x", {}))
    assert seen == ["POST"]


def test_tools_frame_flags_read_only(gw, monkeypatch):
    """The voice link's tools frame marks each tool read_only (lookups) or not (actions); a device tool is an action."""
    g, _, _ = gw
    sent = []

    async def capture(obj):
        sent.append(obj)
        return True

    async def tools(caller="api"):
        return [{"name": n, "description": "", "input_schema": {"type": "object"}} for n in (
            "agenda", "calendar_write", "web_search", "remember", "gistra_today", "infra_health", "calendar_events",
            "spotify_play", "spotify_now_playing", "mac__run_command")], {}
    monkeypatch.setattr(g, "link_send", capture)
    monkeypatch.setattr(g.bridge, "tools", tools)
    asyncio.run(g.serve_tool({"id": "t", "type": "tools_list"}))
    flags = {t["name"]: t["read_only"] for t in sent[-1]["tools"]}
    assert flags == {"agenda": True, "calendar_write": False, "web_search": True, "remember": False, "gistra_today": True,
                     "infra_health": True, "calendar_events": True, "spotify_play": False, "spotify_now_playing": True,
                     "mac__run_command": False}


def test_slow_state_reads_fall_back_at_once(gw, monkeypatch, capsys):
    """A relayed ruchba (2 s RTT): a read with a fallback gives up after STATE_WAIT_S, warns once, and later reads
    use the fallback without waiting while a background refresh runs."""
    g, _, _ = gw
    monkeypatch.setattr(g, "STATE_WAIT_S", 0.05)
    hits = []

    async def handler(req):
        hits.append(req.url.path)
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=[{"id": "new"}])
    st = g.State("http://state")
    st.client = httpx.AsyncClient(base_url="http://state", transport=httpx.MockTransport(handler))
    st.cache[("/v1/state/devices", ())] = [{"id": "old"}]

    async def go():
        t0 = time.monotonic()
        first = await st.get("/v1/state/devices")
        second = await st.get("/v1/state/devices")
        took = time.monotonic() - t0
        await asyncio.sleep(0.3)  # the background refresh lands
        return first, second, took, await st.get("/v1/state/devices")
    first, second, took, later = asyncio.run(go())
    assert first == second == [{"id": "old"}] and took < 0.15 and st.slow()
    assert later == [{"id": "new"}]
    assert capsys.readouterr().out.count("warning: ruchba is slow") == 1


def test_tool_answer_does_not_wait_for_the_audit(gw):
    """The audit row (GET + PUT on ruchba) finishes in the background when ruchba is slow."""
    import tools_bridge as B
    written = []

    async def state(method, path, body=None):
        await asyncio.sleep(0.5)
        if method == "PUT":
            written.append(body)
    bridge = B.Bridge(state=state, who=lambda: "")

    async def go():
        t0 = time.monotonic()
        r = await bridge.run("nope_tool_x", {}, caller="qwen")
        took = time.monotonic() - t0
        await asyncio.sleep(1.2)
        return r, took
    r, took = asyncio.run(go())
    assert took < B.AUDIT_WAIT_S + 0.2 and written and written[0][-1]["action"] == "nope_tool_x"


def test_a_slow_daemon_does_not_hold_up_the_tool_list(gw, monkeypatch):
    """A daemon behind a slow relay: the list answers within DEVICE_LIST_WAIT_S without it, and has it once it came."""
    g, _, _ = gw
    monkeypatch.setattr(g, "DEVICE_LIST_WAIT_S", 0.05)

    async def mcp(url, method, params):
        await asyncio.sleep(0.3 if "slow" in url else 0)
        return {"tools": [{"name": "list_dir", "description": "", "inputSchema": {"type": "object"}}]}
    monkeypatch.setattr(g, "mcp", mcp)
    devs = [{"id": "fast", "name": "fast", "url": "http://fast/mcp"}, {"id": "slow", "name": "slow", "url": "http://slow/mcp"}]

    async def go():
        t0 = time.monotonic()
        first = [t["name"] for t in (await g.device_tools(devs))[0]]
        took = time.monotonic() - t0
        await asyncio.sleep(0.4)
        return first, took, [t["name"] for t in (await g.device_tools(devs))[0]]
    first, took, later = asyncio.run(go())
    assert first == ["fast__list_dir"] and took < 0.2 and later == ["fast__list_dir", "slow__list_dir"]


def test_api_only_gateway_never_runs_the_claude_cli(tmp_path, monkeypatch):
    """BISEO_CLAUDE_PATH=api (deneb): asks take the API whatever the shared claude_path says, tasks never become
    claude_code, and with no key or no budget they fall back to Qwen rather than the CLI."""
    private(tmp_path / "anthropic_api_key", "sk-test\n")
    g = start(tmp_path, monkeypatch, BISEO_CLAUDE_PATH="api")
    assert g.API_ONLY and not g.WARM
    assert g.resolve(g.DEFAULTS, None, None, "chat", "안녕")[1] == "api"
    assert g.resolve(g.DEFAULTS, None, "claude_code", "chat", "안녕")[1] == "api"
    settings = {**g.DEFAULTS, "task_backend": "claude_api"}

    async def cost(s):
        return 0.0
    monkeypatch.setattr(g, "month_cost", cost)

    async def mode(s=None):
        return "auto"
    monkeypatch.setattr(g, "brain_mode", mode)
    run = lambda spec: asyncio.run(g.task_backend_for(spec, settings))  # noqa: E731
    assert run({"backend": "claude_code", "goal": "레포 고쳐 줘"})[0] == "claude_api"
    assert run({"backend": None, "goal": "메일 정리해 줘"}) == ("claude_api", None)
    assert run({"backend": "qwen", "goal": "조사해 줘"}) == ("qwen", None)  # a local choice stays local
    monkeypatch.setattr(g, "API_KEY", "")
    assert run({"backend": None, "goal": "조사해 줘"})[0] == "qwen"
