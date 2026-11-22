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
    assert "spark" not in servers and "azel" in servers  # claude -p reaches a team device only through azel
    assert "mcp__azel__spark__delegate" not in json.loads(run_settings).get("permissions", {}).get("deny", [])
    sadr = [{"id": "sadr", "url": "http://100.64.0.99:8765/mcp"}]
    _, _, rs, _ = g.run_args(asyncio.run(g.load_settings()), sadr)
    assert "mcp__sadr__delegate" in json.loads(rs)["permissions"]["deny"]


def test_static_entry_must_be_limited(tmp_path, monkeypatch):
    token_file(tmp_path, "spark", "x")
    with pytest.raises(ValueError, match="mode limited"):
        start(tmp_path, monkeypatch, BISEO_STATIC_DEVICES=json.dumps([{**SPARK, "mode": "full"}]))


def test_claude_reaches_team_devices_through_azel_and_their_approvals_reach_the_app(sgw, monkeypatch):
    """A team daemon cannot file its own approval (needs_approval): claude -p used to call it directly, so the
    approval was never filed and the app never asked. Through azel's /v1/mcp the gateway files it and pushes."""
    g, c = sgw
    g.static_devices["spark"]["last_seen"] = 9e12
    na = {"status": "needs_approval", "command": "docker restart nemoclaw", "cwd": "/home/u/biseo-work",
          "device": "spark", "in_roots": True, "nonce": "n1"}

    async def mcp(url, method, params):
        if method == "tools/list":
            return {"tools": [{"name": "run_command", "inputSchema": {"type": "object"}}]}
        return {"content": [{"type": "text", "text": json.dumps(na)}]}
    monkeypatch.setattr(g, "mcp", mcp)
    pushed = []

    async def push(*a, **k):
        pushed.append(k.get("approval_id"))
    monkeypatch.setattr(g, "send_alert_push", push)
    monkeypatch.setattr(g.tools_bridge, "APPROVAL_WAIT_S", 0.3)
    monkeypatch.setattr(g.tools_bridge, "APPROVAL_POLL_S", 0.05)
    c = TestClient(g.app, headers={"Authorization": "Bearer t0k"}, client=("100.64.0.8", 5))  # tailnet only
    listed = c.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    assert "spark__run_command" in [t["name"] for t in listed]
    r = c.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "spark__run_command", "arguments": {"command": "docker restart nemoclaw"}}}).json()["result"]
    assert r["isError"] and "5분" in r["content"][0]["text"]  # nobody tapped within the (shortened) wait
    rows = list(g.fake.db["approvals"].values())
    assert [(a["device"], a["command"]) for a in rows] == [("spark", "docker restart nemoclaw")]
    assert pushed == [rows[0]["id"]]  # the app was asked
    assert g.tool_label("mcp__azel__spark__run_command") == "spark에서 명령 실행 중"
