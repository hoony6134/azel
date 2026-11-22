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


def test_voice_turns_never_delegate_tasks_still_can(tmp_path, monkeypatch):
    from test_gateway import start
    from test_tools_bridge import World
    b = World().bridge()
    for caller in ("voice", "qwen"):
        names = [t["name"] for t in asyncio.run(b.tools(caller))[0]]
        assert "macbook__delegate" not in names, caller
        r = asyncio.run(b.run("macbook__delegate", {"task": "x"}, caller=caller))
        assert r["error"]["code"] == "invalid" and "create_task" in r["spoken"], caller
    assert "macbook__delegate" in [t["name"] for t in asyncio.run(b.tools("api"))[0]]  # the task loop keeps it
    g = start(tmp_path, monkeypatch)
    devices = [{"id": "macbook", "url": "http://mac/mcp"}]
    ask = json.loads(g.run_args({"connectors": {}}, devices, "ask:s1")[2])
    task = json.loads(g.run_args({"connectors": {}}, devices, "task:t1")[2])
    assert "mcp__macbook__delegate" in ask["permissions"]["deny"]  # claude -p answering a call or the app
    assert "mcp__macbook__delegate" not in task.get("permissions", {}).get("deny", [])


SX = {"results": [
    {"title": "광주 날씨", "url": "https://weather.example/gwangju", "content": "오늘 광주는 맑고 낮 최고 9도예요. " * 6,
     "engine": "bing"},
    {"title": "광주지방기상청", "url": "https://www.kma.example/gj", "content": "예보와 특보를 확인하세요.", "engine": "yandex"},
    {"title": "nope", "url": "ftp://x/y", "content": "", "engine": "x"}],
    "answers": [], "infoboxes": [], "unresponsive_engines": [["brave", "Suspended: too many requests"],
                                                             ["duckduckgo", "CAPTCHA"]]}


def sx_client(payload, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(dict(request.url.params))
        return httpx.Response(200, json=payload)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_searxng_results_in_the_usual_shape():
    seen = []
    r = asyncio.run(web.searxng("광주 날씨", 5, client=sx_client(SX, seen)))
    assert seen[0] == {"q": "광주 날씨", "format": "json", "language": "ko"}
    assert [s["url"] for s in r["sources"]] == ["https://weather.example/gwangju", "https://www.kma.example/gj"]
    assert "(weather.example)" in r["text"] and "낮 최고 9도" in r["text"] and r["cost_usd"] == 0
    assert r["unresponsive"] == ["brave", "duckduckgo"]  # failing engines inside SearXNG are not a failure


def test_thin_snippets_read_the_top_pages(monkeypatch):
    thin = {**SX, "results": [{"title": "광주지방기상청", "url": "https://www.kma.example/gj", "content": "날씨 지도.",
                               "engine": "bing"}]}

    async def page(url, client=None):
        return {"url": url, "text": "현재 기온 7도, 오후에 맑음"}
    monkeypatch.setattr(web, "fetch", page)
    r = asyncio.run(web.searxng("광주 날씨", 5, client=sx_client(thin)))
    assert "[kma.example 본문] 현재 기온 7도" in r["text"]
    assert asyncio.run(web.searxng("x", 5, client=sx_client({"results": []}))) is None  # nothing found


def test_searxng_first_haiku_only_as_a_fallback_and_never_in_local(tmp_path, monkeypatch):
    from test_gateway import start
    g = start(tmp_path, monkeypatch)
    calls = []

    async def sx(query, n, url):
        calls.append("searxng")
        return None  # found nothing

    async def haiku(key, query, n):
        calls.append("haiku")
        return {"text": "맑음", "sources": [], "cost_usd": 0.01, "usage": {}}
    monkeypatch.setattr(g.web, "searxng", sx)
    monkeypatch.setattr(g.web, "search", haiku)
    g.API_KEY = "sk-test"
    assert asyncio.run(g.web_search("날씨", 5))["text"] == "맑음" and calls == ["searxng", "haiku"]
    calls.clear()

    async def local():
        g.mode_ctx.set("local")
        return await g.web_search("날씨", 5)
    try:
        asyncio.run(local())
        raise AssertionError("no error")
    except g.HTTPException as e:
        assert e.status_code == 503 and calls == ["searxng"]  # never the paid search in local mode


def test_local_mode_offers_web_search_only_while_searxng_answers():
    from test_tools_bridge import World
    b = World().bridge()
    up = {"ok": True}

    async def mode():
        return "local"

    async def web_ok():
        return up["ok"]

    async def found(q, n):
        return {"text": "맑음", "sources": [{"title": "기상청", "url": "https://kma.example"}], "cost_usd": 0.0}
    b.d.update(mode=mode, web_ok=web_ok, web_search=found)
    assert "web_search" in [t["name"] for t in asyncio.run(b.tools("qwen"))[0]]
    assert asyncio.run(b.run("web_search", {"query": "날씨"}, caller="qwen"))["ok"]
    up["ok"] = False
    assert "web_search" not in [t["name"] for t in asyncio.run(b.tools("qwen"))[0]]
    assert asyncio.run(b.run("web_search", {"query": "날씨"}, caller="qwen"))["error"]["code"] == "unavailable"


def test_a_numeric_question_reads_the_top_pages_even_with_long_snippets(monkeypatch):
    long = {"results": [{"title": "삼성전자 주가", "url": "https://finance.example/005930", "content": "증권 정보 " * 60}]}
    read = []

    async def page(url, client=None):
        read.append(url)
        return {"url": url, "text": "현재가 71,200원 (+1.2%)"}
    monkeypatch.setattr(web, "fetch", page)
    r = asyncio.run(web.searxng("삼성전자 주가", 5, client=sx_client(long)))
    assert read == ["https://finance.example/005930"] and "현재가 71,200원" in r["text"]
    read.clear()
    asyncio.run(web.searxng("광주 맛집 추천", 5, client=sx_client(long)))
    assert read == []  # long snippets and not a number question: the snippets do

    async def slow(url, client=None):
        await asyncio.sleep(5)
    monkeypatch.setattr(web, "fetch", slow)
    monkeypatch.setattr(web, "PAGES_S", 0.2)
    r = asyncio.run(web.searxng("원달러 환율", 5, client=sx_client(long)))
    assert r and "본문" not in r["text"]  # a slow page never holds the answer up
