"""Claude through the Messages API with an API key: the opt-in alternative to `claude -p` (setting claude_path "api").

Same tools the local Qwen gets: the read-only Cygnus bridge (assistant/brain/cygnus.py, a PAT in
~/.config/biseo/cygnus_pat) as client-side tools, so the allowlist and argument checks stay in our code and the PAT
never leaves our hosts, plus the device daemons' MCP tools the gateway already exposes. Gmail and Calendar are
claude.ai-hosted connectors and are not reachable here; questions about them stay on `claude -p`.

Prompt caching: the tool definitions and the system prompt are a stable prefix with one 5-minute breakpoint at its
end (anything volatile, like the clock, goes into the user message). Haiku by default; the caller passes Sonnet for
multi-step work.
"""
import asyncio
import json

import httpx

API_URL = "https://api.anthropic.com/v1/messages"
MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
# USD per MTok: input, output, cache read. Cache writes (5-minute TTL) cost 1.25x input.
PRICES = {"haiku": (0.10, 0.50, 0.01), "sonnet": (2.0, 10.0, 0.10), "opus": (4.0, 20.0, 0.20)}
MAX_ROUNDS = 6
TOOL_RESULT_CHARS = 8000


def cost_usd(model, usage):
    pin, pout, pread = PRICES[model]
    return (usage.get("input_tokens", 0) * pin + usage.get("cache_creation_input_tokens", 0) * pin * 1.25
            + usage.get("cache_read_input_tokens", 0) * pread + usage.get("output_tokens", 0) * pout) / 1e6


def request_body(model, system, tools, messages, max_tokens=1024):
    """The cached prefix is tools then system: the breakpoint on the system block caches both."""
    body = {"model": MODELS[model], "max_tokens": max_tokens, "messages": messages,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "output_config": {"effort": "low"}}  # voice answers: short, few tool rounds
    if tools:
        body["tools"] = tools
    return body


async def ask(api_key, model, system, text, tools, run_tool, client=None):
    """Yield ("text", str), ("tool", name) and finally ("done", {usage…, "cost_usd"}). `run_tool(name, input)` returns
    the result text; a tool that raises is reported to Claude as an error result."""
    c = client or httpx.AsyncClient(timeout=120)
    messages = [{"role": "user", "content": text}]
    total = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    try:
        for _ in range(MAX_ROUNDS):
            r = await c.post(API_URL, json=request_body(model, system, tools, messages),
                             headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"})
            r.raise_for_status()
            m = r.json()
            for k in total:
                total[k] += m.get("usage", {}).get(k) or 0
            for b in m["content"]:
                if b["type"] == "text" and b["text"].strip():
                    yield "text", b["text"] + "\n"
            if m["stop_reason"] == "refusal":
                yield "text", "그 요청은 도와드릴 수 없어요."
            if m["stop_reason"] != "tool_use":
                break
            calls = [b for b in m["content"] if b["type"] == "tool_use"]
            for b in calls:
                yield "tool", b["name"]
            results = await asyncio.gather(*[_result(b, run_tool) for b in calls])
            messages += [{"role": "assistant", "content": m["content"]},  # unchanged, thinking blocks included
                         {"role": "user", "content": list(results)}]  # every result in one message
    finally:
        if client is None:
            await c.aclose()
    yield "done", {**total, "model": model, "cost_usd": cost_usd(model, total)}


async def _result(block, run_tool):
    try:
        out = str(await run_tool(block["name"], block.get("input") or {}))[:TOOL_RESULT_CHARS]
        return {"type": "tool_result", "tool_use_id": block["id"], "content": out or "(empty)"}
    except Exception as e:
        return {"type": "tool_result", "tool_use_id": block["id"], "content": f"tool failed: {type(e).__name__}",
                "is_error": True}


def cygnus_bridge(home):
    """(tool definitions, run_tool) for the read-only Cygnus tools, or ([], None) without a PAT.
    The bridge itself (allowlist, validation, trimming) is cygnus.py's tools() / call(), in this directory.

    shortcut: gateway/cygnus.py is a copy of brain/cygnus.py plus the people tool and the memory-scoped writes;
    make it one shared module when the brain branch lands."""
    import os
    if not (home / "cygnus_pat").exists():
        return [], None
    os.environ.setdefault("BISEO_HOME", str(home))
    import cygnus  # the gateway's own copy (batch 5); the brain keeps its own
    return (cygnus.tools(), cygnus.call) if cygnus.available() else ([], None)
