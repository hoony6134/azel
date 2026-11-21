"""A generic tool-using agent loop for background tasks on the API paths, so task handoff no longer needs Claude Code.

Backends:
  claude_api  the Messages API (an API key), the bridge's tools plus Anthropic's server-side web search and fetch,
              prompt caching on the stable prefix (tools, then system)
  nim         hosted NVIDIA NIM (OpenAI-compatible), the bridge's tools (web: the daemon's browse tool)
  qwen        the local Qwen (OpenAI-compatible, native tool calls), the bridge's tools as caller "qwen": execution
              always behind an approval, no delegate

Caps like the Claude Code tasks: steps, wall time (approval waits do not count), and estimated dollars. Yields
activity events ({"type":"activity","phase","tool"}) and ends with one {"type":"done"|"failed",…} event.
Cancellation: `cancelled()` is awaited before every step.
"""
import json
import time

import httpx

import claude_api
from tools_bridge import args_of, openai_tools

MAX_STEPS = 30
WEB_TOOLS = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
             {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5}]
WEB_SEARCH_USD = 0.01  # per search request
BACKENDS = ("claude_api", "nim", "qwen")


class Limit(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind, self.message = kind, message


async def run(backend, system, goal, bridge, cfg, budget_usd, max_minutes, cancelled, persona_id=None):
    """cfg: {"api_key","model"} for claude_api; {"url","key","model","prices":[in,out]} for nim/qwen."""
    deadline, spent, steps = time.monotonic() + max_minutes * 60, 0.0, 0
    caller = "qwen" if backend == "qwen" else "api"
    defs, where = await bridge.tools(caller)
    try:
        if backend == "claude_api":
            loop = _claude(system, goal, defs, cfg)
        else:
            loop = _openai(system, goal, defs, cfg)
        reply = None
        while True:
            if await cancelled():
                yield {"type": "failed", "kind": "cancelled", "message": "취소됐어요.", "cost_usd": spent}
                return
            if steps >= MAX_STEPS:
                raise Limit("steps", f"{MAX_STEPS}단계를 넘어 멈췄어요.")
            if time.monotonic() > deadline:
                raise Limit("time", f"{max_minutes:g}분 시간 한도를 넘어 멈췄어요.")
            step = await loop.asend(reply)
            steps += 1
            spent += step["cost_usd"]
            if spent >= budget_usd:
                raise Limit("budget", f"예산 {budget_usd:g}달러를 넘어 멈췄어요.")
            if not step["calls"]:
                yield {"type": "done", "result": step["text"].strip()[:2000], "cost_usd": spent, "steps": steps}
                return
            reply = []
            for call in step["calls"]:
                yield {"type": "activity", "phase": "tool_start", "tool": call["name"]}
                r = await bridge.call(call["name"], call["input"], persona_id, caller=caller, where=where)
                deadline += r.get("waited_s", 0)  # the user's approval time is not the task's time
                if r.get("approval"):
                    yield {"type": "activity", "phase": "approval_" + r["approval"], "tool": call["name"]}
                yield {"type": "activity", "phase": "tool_end", "tool": call["name"]}
                reply.append({**call, "content": r["content"], "is_error": r["is_error"]})
    except Limit as e:
        yield {"type": "failed", "kind": e.kind, "message": e.message, "cost_usd": spent}
    except httpx.HTTPError as e:
        yield {"type": "failed", "kind": "error", "message": f"모델 호출이 실패했어요 ({type(e).__name__}).",
               "cost_usd": spent}


async def _claude(system, goal, defs, cfg):
    """A coroutine-generator: sends back {"text","calls":[{"id","name","input"}],"cost_usd"}, receives results."""
    model = cfg.get("model", "sonnet")
    messages, carried = [{"role": "user", "content": goal}], 0.0
    async with httpx.AsyncClient(timeout=180) as c:
        while True:
            body = claude_api.request_body(model, system, [*defs, *WEB_TOOLS], messages, max_tokens=4096)
            r = await c.post(claude_api.API_URL, json=body,
                             headers={"x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01"})
            r.raise_for_status()
            m = r.json()
            u = m.get("usage") or {}
            cost = claude_api.cost_usd(model, u) + \
                ((u.get("server_tool_use") or {}).get("web_search_requests", 0) * WEB_SEARCH_USD)
            text = "".join(b.get("text", "") for b in m["content"] if b["type"] == "text")
            calls = [{"id": b["id"], "name": b["name"], "input": b.get("input") or {}}
                     for b in m["content"] if b["type"] == "tool_use"]
            messages.append({"role": "assistant", "content": m["content"]})
            if m["stop_reason"] == "pause_turn":  # a long server-side web search: send it back as is to continue
                carried += cost
                continue
            results = yield {"text": text, "calls": calls if m["stop_reason"] == "tool_use" else [],
                             "cost_usd": cost + carried}
            carried = 0.0
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": x["id"], "content": x["content"][:20000] or "(empty)",
                 **({"is_error": True} if x["is_error"] else {})} for x in results]})


async def _openai(system, goal, defs, cfg):
    tools = openai_tools(defs)
    pin, pout = (cfg.get("prices") or [0, 0])[:2]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": goal}]
    headers = {"Authorization": f"Bearer {cfg['key']}"} if cfg.get("key") else {}
    async with httpx.AsyncClient(timeout=180, headers=headers) as c:
        while True:
            r = await c.post(cfg["url"], json={"model": cfg["model"], "messages": messages, "tools": tools,
                                                "max_tokens": 2048})
            r.raise_for_status()
            d = r.json()
            u = d.get("usage") or {}
            cost = (u.get("prompt_tokens", 0) * pin + u.get("completion_tokens", 0) * pout) / 1e6
            msg = d["choices"][0]["message"]
            calls = [{"id": x["id"], "name": x["function"]["name"], "input": args_of(x["function"].get("arguments"))}
                     for x in msg.get("tool_calls") or []]
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             **({"tool_calls": msg["tool_calls"]} if calls else {})})
            results = yield {"text": msg.get("content") or "", "calls": calls, "cost_usd": cost}
            messages += [{"role": "tool", "tool_call_id": x["id"], "content": x["content"][:20000] or "(empty)"}
                         for x in results]
