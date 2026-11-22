"""NVIDIA NIM provider against a fake OpenAI-compatible server (streaming, a tool call, refusals, keys)."""
import json
import stat

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from test_gateway import events, start

KEY = "nvapi-test-0123456789"


def fake_openai(refuse_tools=False, tool="macbook__list_dir", args='{"pa', more='th": "~"}'):
    app = FastAPI()
    app.seen = []

    @app.get("/v1/models")
    def models(request: Request):
        app.seen.append(("models", request.headers.get("authorization")))
        return {"data": [{"id": "nvidia/nemotron-3-ultra-550b-a55b"}, {"id": "nvidia/nemotron-x"}]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        app.seen.append(("chat", request.headers.get("authorization"), body))
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return JSONResponse({"error": "unauthorized"}, 401)
        if refuse_tools and body.get("tools"):
            return JSONResponse({"error": "tools not supported"}, 400)

        def sse(*chunks):
            return StreamingResponse(iter([f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]),
                                     media_type="text/event-stream")
        if body.get("tools") and not any(m["role"] == "tool" for m in body["messages"]):
            return sse({"choices": [{"delta": {"content": "볼게요. "}}]},
                       {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function":
                                                               {"name": tool, "arguments": args}}]}}]},
                       {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": more}}]},
                                     "finish_reason": "tool_calls"}]},
                       {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10}})
        return sse({"choices": [{"delta": {"content": "파일이 "}}]}, {"choices": [{"delta": {"content": "세 개예요."}}]},
                   {"choices": [], "usage": {"prompt_tokens": 200, "completion_tokens": 20}})
    return app


@pytest.fixture
def nimgw(tmp_path, monkeypatch):
    g = start(tmp_path, monkeypatch, BISEO_DEVICE="macbook")
    fake = fake_openai()
    g.nim.transport = httpx.ASGITransport(app=fake)
    g.nim.no_tools.clear()
    g.nim._models.update(base=None, at=0.0, ids=[])
    calls = []

    async def devices():
        return [{"id": "macbook", "name": "macbook", "url": "http://daemon"}]

    async def mcp(url, method, params):
        calls.append((method, params))
        if method == "tools/list":
            return {"tools": [{"name": "list_dir", "description": "list", "inputSchema": {"type": "object"}}]}
        return {"content": [{"type": "text", "text": "a b c"}]}
    g.fresh_devices, g.mcp = devices, mcp
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        yield g, c, tmp_path, fake, calls


def enable(c, **extra):
    r = c.put("/v1/settings", json={"providers": {"nim": {"enabled": True, "base_url": "https://integrate.api.nvidia.com/v1", **extra}}})
    assert r.status_code == 200, r.text
    return r.json()


def test_key_is_write_only(nimgw):
    g, c, tmp, fake, _ = nimgw
    assert c.get("/v1/settings").json()["providers"]["nim"]["has_key"] is False
    assert c.put("/v1/providers/nim/key", json={"value": KEY}).status_code == 204
    p = tmp / "providers" / "nim_key"
    assert p.read_text().strip() == KEY
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    s = c.get("/v1/settings")
    assert s.json()["providers"]["nim"]["has_key"] is True and KEY not in s.text
    assert KEY not in json.dumps(g.fake.db["settings"])  # never in the shared settings either
    assert c.put("/v1/providers/nim/key", json={"value": "has space"}).status_code == 400
    assert c.put("/v1/providers/nim/key", json={"value": ""}).status_code == 204 and not p.exists()


def test_settings_validation(nimgw):
    g, c, *_ = nimgw
    assert c.put("/v1/settings", json={"providers": {"nim": {"base_url": "http://integrate.api.nvidia.com/v1"}}}).status_code == 400
    assert c.put("/v1/settings", json={"providers": {"nim": {"base_url": "https://u:p@integrate.api.nvidia.com"}}}).status_code == 400
    assert c.put("/v1/settings", json={"providers": {"nim": {"evil": 1}}}).status_code == 400
    assert c.put("/v1/settings", json={"providers": {"openai": {}}}).status_code == 400
    s = enable(c, model="nvidia/nemotron-x", prices={"nvidia/nemotron-x": [1.0, 2.0]})
    assert s["providers"]["nim"]["model"] == "nvidia/nemotron-x" and s["providers"]["nim"]["enabled"] is True
    s = c.put("/v1/settings", json={"providers": {"nim": {"model": "nvidia/nemotron-3-ultra-550b-a55b", "has_key": True}}}).json()
    assert s["providers"]["nim"]["enabled"] is True  # a partial update keeps the other fields
    assert c.put("/v1/settings", json={"default_model": "nim"}).status_code == 200
    assert c.put("/v1/settings", json={"task_defaults": {"chat": {"model": "nim"}}}).status_code == 200
    assert c.put("/v1/settings", json={"default_model": "gpt"}).status_code == 400


def test_models_list_cached(nimgw):
    g, c, tmp, fake, _ = nimgw
    assert c.get("/v1/providers/nim/models").status_code == 409  # no key yet
    c.put("/v1/providers/nim/key", json={"value": KEY})
    enable(c)
    assert c.get("/v1/providers/nim/models").json() == {"models": ["nvidia/nemotron-3-ultra-550b-a55b", "nvidia/nemotron-x"]}
    c.get("/v1/providers/nim/models")
    assert [x[0] for x in fake.seen] == ["models"] and fake.seen[0][1] == f"Bearer {KEY}"


def test_ask_streams_with_a_tool_call_and_records_usage(nimgw):
    g, c, tmp, fake, calls = nimgw
    c.put("/v1/providers/nim/key", json={"value": KEY})
    enable(c, prices={"nvidia/nemotron-3-ultra-550b-a55b": [1.0, 10.0]})
    ev = events(c.post("/v1/ask", json={"text": "맥북 홈에 파일 몇 개야?", "session": "n1", "model": "nim"}))
    assert "".join(e["delta"] for e in ev if e["type"] == "text") == "볼게요. 파일이 세 개예요."
    acts = [(e["phase"], e["label"]) for e in ev if e["type"] == "activity"]
    assert acts == [("thinking", "생각 중"), ("tool_start", "macbook 파일 보는 중"), ("tool_end", "macbook 파일 보는 중")]
    assert ("tools/call", {"name": "list_dir", "arguments": {"path": "~"}}) in calls
    done = ev[-1]
    assert done["type"] == "done" and done["path"] == "nim" and done["model"] == "nvidia/nemotron-3-ultra-550b-a55b"
    assert done["input_tokens"] == 300 and done["output_tokens"] == 30
    row = g.fake.db["usage"][-1]
    assert row["path"] == "nim" and row["model"] == "nvidia/nemotron-3-ultra-550b-a55b"
    assert abs(row["cost_usd"] - (300 * 1.0 + 30 * 10.0) / 1e6) < 1e-12
    assert "존댓말" in fake.seen[0][2]["messages"][0]["content"] and "요일" in fake.seen[0][2]["messages"][0]["content"]
    second = fake.seen[-1][2]["messages"]
    assert second[-1] == {"role": "tool", "tool_call_id": "c1", "content": "a b c"}
    assert all(KEY not in json.dumps(m) for m in second)  # the key only ever rides in the header


def test_voice_phrase_and_default_route_to_nim(nimgw):
    g, c, *_ = nimgw
    c.put("/v1/providers/nim/key", json={"value": KEY})
    enable(c)
    c.put("/v1/settings", json={"default_model": "nim"})
    ev = events(c.post("/v1/ask", json={"text": "안녕", "session": "n2"}))
    assert ev[-1]["path"] == "nim"  # default_model nim: no model in the ask


def test_tools_refused_falls_back_without_tools(nimgw):
    g, c, tmp, _, calls = nimgw
    fake = fake_openai(refuse_tools=True)
    g.nim.transport = httpx.ASGITransport(app=fake)
    c.put("/v1/providers/nim/key", json={"value": KEY})
    enable(c)
    ev = events(c.post("/v1/ask", json={"text": "안녕", "session": "n3", "model": "nim"}))
    assert "".join(e["delta"] for e in ev if e["type"] == "text") == "파일이 세 개예요."
    assert "nvidia/nemotron-3-ultra-550b-a55b" in g.nim.no_tools
    events(c.post("/v1/ask", json={"text": "또", "session": "n3", "model": "nim"}))
    assert "tools" not in fake.seen[-1][2]  # remembered: no second refused attempt


def test_missing_or_bad_key_is_speakable(nimgw):
    g, c, tmp, fake, _ = nimgw
    enable(c)
    ev = events(c.post("/v1/ask", json={"text": "안녕", "session": "n4", "model": "nim"}))
    assert ev == [{"type": "error", "code": "nim_key", "message": "엔비디아 API 키가 아직 없어요. 앱의 설정에서 키를 넣어 주세요."}]
    assert not fake.seen  # nothing was sent without a key
    c.put("/v1/providers/nim/key", json={"value": "nvapi-wrong"})
    ev = events(c.post("/v1/ask", json={"text": "안녕", "session": "n4", "model": "nim"}))
    assert ev[-1]["type"] == "error" and ev[-1]["code"] == "nim_key" and "nvapi" not in json.dumps(ev)
    c.put("/v1/settings", json={"providers": {"nim": {"enabled": False}}})
    ev = events(c.post("/v1/ask", json={"text": "안녕", "session": "n4", "model": "nim"}))
    assert ev[-1]["code"] == "nim_off"


def test_limited_gateway_has_no_nim(tmp_path, monkeypatch):
    for name, text in (("anthropic_api_key", "sk-test\n"), ("state_token_limited", "l1m\n")):
        (tmp_path / name).write_text(text)
        (tmp_path / name).chmod(0o600)
    g = start(tmp_path, monkeypatch, BISEO_MODE="limited", BISEO_DEVICE="spark")
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        assert c.put("/v1/providers/nim/key", json={"value": KEY}).status_code == 403
        assert c.get("/v1/providers/nim/models").status_code == 403
        assert c.put("/v1/settings", json={"providers": {"nim": {"enabled": True}}}).status_code == 403
        assert not (tmp_path / "providers").exists()


def test_cygnus_bridge_tools_on_nim(nimgw, monkeypatch):
    g, c, tmp, _, calls = nimgw
    fake = fake_openai(tool="gistra_today", args="{", more="}")
    g.nim.transport = httpx.ASGITransport(app=fake)
    got = []

    async def cyg_call(name, args):
        got.append((name, args))
        return "오늘 마감: 토론지"
    monkeypatch.setattr(g.claude_api, "cygnus_bridge", lambda home: (
        [{"name": "gistra_today", "description": "today", "input_schema": {"type": "object"}}], cyg_call))
    c.put("/v1/providers/nim/key", json={"value": KEY})
    enable(c)
    ev = events(c.post("/v1/ask", json={"text": "오늘 마감 뭐야?", "session": "n5", "model": "nim"}))
    sent_tools = [t["function"]["name"] for t in fake.seen[0][2]["tools"]]
    assert sent_tools == ["macbook__list_dir", "call_user", "schedule_list", "schedule_cancel", "schedule_update", "create_task", "task_status", "task_cancel", "approval_status", "memory_recall", "people_lookup", "remember", "forget", "memory_list", "web_search", "web_fetch", "music_play", "music_control", "home_scene", *(t["name"] for t in g.tools_bridge.P.TOOLS), "gistra_today"] and got == [("gistra_today", {})]
    assert ("tool_start", "학사 일정 확인 중") in [(e["phase"], e["label"]) for e in ev if e["type"] == "activity"]
    assert fake.seen[-1][2]["messages"][-1]["content"] == "오늘 마감: 토론지"
    assert not any(m == "tools/call" for m, _ in calls)


def test_limited_refuses_plan_questions(tmp_path, monkeypatch):
    for name, text in (("anthropic_api_key", "sk-test\n"), ("state_token_limited", "l1m\n")):
        (tmp_path / name).write_text(text)
        (tmp_path / name).chmod(0o600)
    g = start(tmp_path, monkeypatch, BISEO_MODE="limited", BISEO_DEVICE="spark")
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        for said in ("내일 내가 해야 될 일도 정리해줘", "토요일에 뭐 있어?", "이번 주 계획 정리해 줘"):
            ev = events(c.post("/v1/ask", json={"text": said, "session": "p"}))
            assert ev[-1]["code"] == "personal", said


def test_base_url_allowlist_and_key_dropped_on_change(nimgw):
    g, c, tmp, *_ = nimgw
    for bad in ("https://evil.example/v1", "https://api.nvidia.com.evil.io/v1", "https://integrate.api.nvidia.com:8443/v1",
                "https://nvidia.com/v1"):
        assert c.put("/v1/settings", json={"providers": {"nim": {"base_url": bad}}}).status_code == 400, bad
    c.put("/v1/providers/nim/key", json={"value": KEY})
    assert c.put("/v1/settings", json={"providers": {"nim": {"enabled": True}}}).json()["providers"]["nim"]["has_key"]
    s = c.put("/v1/settings", json={"providers": {"nim": {"base_url": "https://eu.api.nvidia.com/v1"}}}).json()
    assert s["providers"]["nim"]["has_key"] is False and not (tmp / "providers" / "nim_key").exists()
