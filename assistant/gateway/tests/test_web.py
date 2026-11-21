"""web_fetch's address rules, search result shaping, and where the web tools are (and aren't) offered."""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import web  # noqa: E402


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "http://localhost:8892/v1/settings",
                                 "http://127.0.0.1/", "http://10.0.0.5/", "http://192.168.1.1/", "http://100.64.0.10:8894/",
                                 "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://user:pw@example.com/"])
def test_private_and_odd_urls_are_refused(url):
    with pytest.raises(ValueError):
        asyncio.run(web.check_url(url))


def test_a_redirect_into_the_tailnet_is_refused(monkeypatch):
    monkeypatch.setattr(web, "_public", lambda host: host == "example.com")

    def handler(request):
        return httpx.Response(302, headers={"location": "http://100.64.0.10:8894/v1/state/settings"})
    c = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    with pytest.raises(ValueError):
        asyncio.run(web.fetch("https://example.com/", client=c))


def test_fetch_returns_text_and_caps_it(monkeypatch):
    monkeypatch.setattr(web, "_public", lambda host: True)
    page = "<html><head><title>예시</title><script>x()</script></head><body><p>안녕 " + "가" * 9000 + "</p></body></html>"
    c = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})))
    r = asyncio.run(web.fetch("https://example.com/", client=c))
    assert r["text"].startswith("예시\n안녕") and "x()" not in r["text"] and len(r["text"]) == web.MAX_CHARS


def test_search_keeps_text_and_sources(monkeypatch):
    def handler(request):
        body = json.loads(request.content)
        assert body["tools"][0]["type"].startswith("web_search") and body["model"].startswith("claude-haiku")
        return httpx.Response(200, json={"stop_reason": "end_turn", "usage": {
            "input_tokens": 100, "output_tokens": 50, "server_tool_use": {"web_search_requests": 1}}, "content": [
            {"type": "web_search_tool_result", "content": [
                {"type": "web_search_result", "title": "기상청", "url": "https://www.weather.go.kr/x"},
                {"type": "web_search_result", "title": "기상청", "url": "https://www.weather.go.kr/x"}]},
            {"type": "text", "text": "광주는 맑고 12도예요(기상청)."}]})
    c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    r = asyncio.run(web.search("sk-test", "광주 날씨", client=c))
    assert r["text"] == "광주는 맑고 12도예요(기상청)." and r["sources"] == [{"title": "기상청", "url": "https://www.weather.go.kr/x"}]
    assert r["cost_usd"] > 0.01


def test_web_tools_on_each_path(tmp_path, monkeypatch):
    from test_gateway import start
    from test_tools_bridge import World
    import tools_bridge as TB
    g = start(tmp_path, monkeypatch)
    allowed = g.run_args({"connectors": {}}, [])[1]
    assert "WebSearch" in allowed and "WebFetch" in allowed  # claude -p on a full gateway
    names = [t["name"] for t in asyncio.run(World().bridge().tools("qwen"))[0]]
    assert "web_search" in names and "web_fetch" in names  # Qwen and NIM get the registry's
    import agent_loop
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"stop_reason": "end_turn", "usage": {}, "content": [{"type": "text", "text": "ok"}]})
    real = httpx.AsyncClient
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    gen = agent_loop._claude("s", "g", [*TB.OWN_TOOLS], {"api_key": "k", "model": "haiku"})
    asyncio.run(gen.__anext__())
    tool_names = [t["name"] for t in sent["tools"]]
    assert tool_names.count("web_search") == 1 and tool_names.count("web_fetch") == 1  # the native ones only


def test_limited_gateway_has_no_web_on_claude_code(tmp_path, monkeypatch):
    from test_gateway import private, start
    private(tmp_path / "anthropic_api_key", "sk-test\n")
    private(tmp_path / "state_token_limited", "l1m\n")
    g = start(tmp_path, monkeypatch, BISEO_MODE="limited", BISEO_DEVICE="spark")
    assert "WebSearch" not in g.run_args({"connectors": {}}, [])[1]
