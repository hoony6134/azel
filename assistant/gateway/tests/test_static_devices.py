"""Static team devices: probed by full gateways, limited, their own token, never `delegate`."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from test_gateway import start

SPARK = {"id": "spark", "name": "vd-spark", "url": "http://100.64.0.11:8893/mcp", "mode": "limited"}


def token_file(tmp_path, did, value):
    d = tmp_path / "device_tokens"
    d.mkdir(exist_ok=True)
    (d / did).write_text(value + "\n")
    (d / did).chmod(0o600)


@pytest.fixture
def sgw(tmp_path, monkeypatch):
    token_file(tmp_path, "spark", "spark-daemon-tok")
    g = start(tmp_path, monkeypatch, BISEO_STATIC_DEVICES=json.dumps([SPARK, {**SPARK, "id": "core", "url": "http://x/mcp"}]))
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}) as c:
        yield g, c


def test_static_device_listed_limited_and_probed(sgw, monkeypatch):
    g, c = sgw
    assert set(g.static_devices) == {"spark"}  # core has no token file: skipped, never the shared token
    seen = []

    async def probe(url, token=False):
        seen.append((url, token))
        return {"ok": True, "ms": 1}
    monkeypatch.setattr(g, "probe", probe)

    async def one_round():
        try:
            await asyncio.wait_for(g.static_prober(), 0.2)
        except asyncio.TimeoutError:
            pass
    asyncio.run(one_round())
    assert seen == [(SPARK["url"], True)]
    rows = c.get("/v1/devices").json()
    spark = next(d for d in rows if d["id"] == "spark")
    assert spark["writer"] == "full" and spark["limited"] is True and "tok" not in json.dumps(rows)
    assert [d["id"] for d in asyncio.run(g.fresh_devices())] == ["spark"]


def test_daemon_calls_use_the_device_token(sgw, monkeypatch):
    g, _ = sgw
    seen, orig = [], httpx.AsyncClient

    def handler(req):
        seen.append(req.headers["authorization"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
    monkeypatch.setattr(g.httpx, "AsyncClient", lambda **kw: orig(transport=httpx.MockTransport(handler), **kw))
    asyncio.run(g.mcp(SPARK["url"], "tools/list", {}))
    asyncio.run(g.mcp("http://100.64.0.8:8893/mcp", "tools/list", {}))
    assert seen == ["Bearer spark-daemon-tok", "Bearer t0k"]


def test_team_devices_may_delegate_and_sadr_never(sgw, monkeypatch):
    g, _ = sgw
    g.static_devices["spark"]["last_seen"] = 9e12
    calls = []

    async def mcp(url, method, params):
        calls.append((url, method, params))
        if method == "tools/list":
            return {"tools": [{"name": n, "inputSchema": {"type": "object"}} for n in ("run_command", "delegate")]}
        return {"content": [{"type": "text", "text": "ok"}]}
    monkeypatch.setattr(g, "mcp", mcp)
    devs = asyncio.run(g.fresh_devices())
    tools, where = asyncio.run(g.device_tools(devs))
    assert [t["name"] for t in tools] == ["spark__run_command", "spark__delegate"]  # a team device may delegate now
    where["sadr__delegate"] = ("http://100.64.0.99:8765/mcp", "delegate")  # even if a model names it anyway
    res = asyncio.run(g.run_tool({"id": "1", "name": "sadr__delegate", "input": {"task": "x"}}, where))
    assert res["is_error"] and not any(m == "tools/call" and u.startswith("http://100.64.0.99") for u, m, _ in calls)
    servers, allowed, run_settings, _ = g.run_args(asyncio.run(g.load_settings()), devs)
    assert servers["spark"]["headers"]["Authorization"] == "Bearer spark-daemon-tok"
    assert "mcp__spark__delegate" not in json.loads(run_settings)["permissions"]["deny"]
    sadr = [{"id": "sadr", "url": "http://100.64.0.99:8765/mcp"}]
    _, _, rs, _ = g.run_args(asyncio.run(g.load_settings()), sadr)
    assert "mcp__sadr__delegate" in json.loads(rs)["permissions"]["deny"]


def test_static_entry_must_be_limited(tmp_path, monkeypatch):
    token_file(tmp_path, "spark", "x")
    with pytest.raises(ValueError, match="mode limited"):
        start(tmp_path, monkeypatch, BISEO_STATIC_DEVICES=json.dumps([{**SPARK, "mode": "full"}]))
