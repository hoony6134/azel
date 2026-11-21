"""Conformance (ACTIONS.md): every registry action has an MCP entry, a REST route (or a reason), a spoken result,
an eval case, and gives the same result through REST and as a tool, valid or not. A new action that skips a surface
fails here."""
import asyncio
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tasks import tgw, wait_for  # noqa: F401  (fixture)
from test_tools_routes import tailnet

V2 = {"X-Azel-Result": "2"}
IDS = re.compile(r"[0-9a-f]{12}")


def actions():
    import tools_bridge
    return tools_bridge.ACTIONS


def routes(g):
    return {(m, re.sub(r"\{\w+\}", "{id}", r.path)) for r in g.app.routes for m in getattr(r, "methods", ())}


def test_registry_covers_every_surface(tgw):  # noqa: F811
    g, _, _ = tgw
    TB = g.tools_bridge
    names = {t["name"] for t in TB.ALL_OWN}
    assert set(TB.ACTIONS) == names  # every own tool is a registered action and the other way round
    mcp = tailnet(g).post("/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    model_tools = {t["name"] for t in asyncio.run(g.bridge.tools("qwen"))[0]}
    import eval_actions
    for name, meta in TB.ACTIONS.items():
        assert name in {t["name"] for t in mcp}, name
        assert ("external" in meta["scopes"]) == (name in TB.SCOPES["external"]), name
        assert ("qwen" in meta["scopes"]) == (name in model_tools), name
        assert ("app" in meta["scopes"]) == (name in TB.APP_ACTIONS), name
        method, path = meta["rest"]
        assert (method, path) in routes(g), (name, method, path)
        assert name in eval_actions.CASES, f"{name}: no eval case"
        schema = TB.OWN_SCHEMAS[name]
        assert schema.get("type") == "object" and schema.get("properties") is not None, name  # never {} (Claude API)
    for family, why in TB.NOT_ON_REST.items():
        assert why.strip(), family
    assert ("POST", "/v1/tools/call") in routes(g)  # what every tool family is reachable through on the tailnet


def make_target(g, c, kind):
    if kind == "{id}":
        r = c.post("/v1/schedule", json={"reason": "약 먹기", "when": "2099-01-01T09:00:00+09:00"}, headers=V2)
        if r.status_code != 201:  # beyond 30 days: use a near time
            from datetime import datetime, timedelta
            when = (datetime.now(g.KST) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            r = c.post("/v1/schedule", json={"reason": "약 먹기", "when": when.isoformat()}, headers=V2)
        return r.json()["data"]["id"]
    t = c.post("/v1/tasks", json={"goal": "정리", "notify": "none", "backend": "claude_code"}, headers=V2).json()["data"]
    wait_for(c, t["id"], ("done", "failed"))
    return t["id"]


def fill(g, c, args):
    return {k: (make_target(g, c, v) if v in ("{id}", "{task}") else v) for k, v in args.items()}


def rest_call(c, meta, args):
    method, path = meta["rest"]
    path = path.replace("{id}", str(args.get("id", "")))
    body = {k: v for k, v in args.items() if k != "id" or "{id}" not in meta["rest"][1]}
    if method == "GET":
        return c.get(path, params={k: v for k, v in body.items()}, headers=V2)
    return c.request(method, path, json=body if method in ("POST", "PUT") else None, headers=V2)


def norm(r):
    """What must match across surfaces: outcome, needs, error, and the spoken text up to ids."""
    return {"ok": r["ok"], "spoken": IDS.sub("<id>", r["spoken"]), "needs": r["needs"], "error": r["error"]}


@pytest.mark.parametrize("name", sorted(actions()))
def test_rest_and_tool_give_the_same_result(tgw, name):  # noqa: F811
    g, c, _ = tgw
    meta = g.tools_bridge.ACTIONS[name]
    if name == "call_user":
        g.call_warm_keys["x"] = "azel:s"  # in a call: a refusal both ways, and nothing rings in the test
    valid, invalid = meta["examples"]
    for example in (valid, invalid):
        a1, a2 = fill(g, c, example), fill(g, c, example)
        via_rest = rest_call(c, meta, a1).json()
        via_tool = asyncio.run(g.bridge.run(name, a2, caller="app"))
        assert {"ok", "spoken", "data", "needs", "error"} <= set(via_rest), via_rest
        assert via_rest["spoken"].strip(), name  # always something to say
        assert norm(via_rest) == norm(via_tool), (name, example, via_rest, via_tool)
    g.call_warm_keys.clear()


def test_only_the_app_may_send_an_exact_time(tgw):  # noqa: F811
    g, c, _ = tgw
    from datetime import datetime, timedelta
    when = (datetime.now(g.KST) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0).isoformat()
    assert c.post("/v1/schedule", json={"reason": "x", "when": when}, headers=V2).json()["ok"]
    for caller in ("api", "qwen", "claude_code", "external"):
        r = asyncio.run(g.bridge.run("schedule_create", {"reason": "x", "when": when}, caller=caller))
        assert r["error"]["code"] == "invalid" and "when_text" in r["spoken"], caller
