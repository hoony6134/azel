import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import claude_api as A  # noqa: E402


def fake_api(replies):
    """The Messages API: returns `replies` in order and records every request body."""
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=replies[len(seen) - 1])
    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handler))


def msg(content, stop, usage):
    return {"content": content, "stop_reason": stop, "usage": usage}


def collect(gen):
    async def run():
        return [x async for x in gen]
    return asyncio.run(run())


def test_tool_loop_caches_the_prefix_and_counts_cost():
    seen, client = fake_api([
        msg([{"type": "tool_use", "id": "t1", "name": "gistra_today", "input": {}},
             {"type": "tool_use", "id": "t2", "name": "boom", "input": {}}], "tool_use",
            {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 1500}),
        msg([{"type": "text", "text": "내일은 토론지 마감이 있어요."}], "end_turn",
            {"input_tokens": 300, "output_tokens": 30, "cache_read_input_tokens": 1500})])
    calls = []

    async def run_tool(name, args):
        calls.append(name)
        if name == "boom":
            raise RuntimeError("down")
        return "- 11월 22일 23:00 토론지"
    tools = [{"name": "gistra_today", "description": "x", "input_schema": {"type": "object"}}]
    out = collect(A.ask("k", "haiku", "SYSTEM", "내일 뭐 있어?", tools, run_tool, client=client))
    assert ("tool", "gistra_today") in out and ("text", "내일은 토론지 마감이 있어요.\n") in out
    done = out[-1][1]
    assert done["input_tokens"] == 400 and done["cache_read_input_tokens"] == 1500 and done["output_tokens"] == 50
    want = (400 * 0.10 + 1500 * 0.10 * 1.25 + 1500 * 0.01 + 50 * 0.50) / 1e6
    assert abs(done["cost_usd"] - want) < 1e-12
    first, second = seen
    assert first["system"][-1]["cache_control"] == {"type": "ephemeral"} and first["tools"] == tools
    assert first["model"] == "claude-haiku-5-5" and "temperature" not in first
    results = second["messages"][-1]["content"]  # both results in one user message, the failed one marked
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"] and results[1]["is_error"] is True
    assert "down" not in json.dumps(results)  # only the error class goes to Claude
    assert second["system"] == first["system"] and second["tools"] == first["tools"]  # same prefix: cache hit


def test_refusal_and_no_tools():
    seen, client = fake_api([msg([], "refusal", {"input_tokens": 5, "output_tokens": 0})])
    out = collect(A.ask("k", "sonnet", "S", "x", [], None, client=client))
    assert out[0] == ("text", "그 요청은 도와드릴 수 없어요.") and "tools" not in seen[0]


def test_cygnus_bridge_needs_a_private_pat(tmp_path):
    assert A.cygnus_bridge(tmp_path) == ([], None)
