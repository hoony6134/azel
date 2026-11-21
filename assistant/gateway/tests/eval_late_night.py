"""Late-night day words on the model paths: does the model follow late_night.prompt_rule? Read-only: the tools are
schemas only, nothing is called. Qwen (local) and the Claude API (haiku, sonnet); a few cents in all.

  env -u CONDA_PREFIX uv run --frozen python tests/eval_late_night.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import late_night as L  # noqa: E402

KST = timezone(timedelta(hours=9))
QWEN = os.environ.get("BISEO_LLM_URL", "http://100.64.0.11:8888/v1/chat/completions")
QWEN_MODEL = os.environ.get("BISEO_LLM_MODEL", "Qwen3.8-Flash-Next")
KEY = (Path.home() / ".config/biseo/anthropic_api_key")
TOOLS = [
    {"name": "call_user", "description": "사용자에게 전화를 건다. when이 있으면 그 시각(KST, ISO 8601)에 예약한다.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"},
                                                       "when": {"type": ["string", "null"]}}, "required": ["reason"]}},
    {"name": "calendar_events", "description": "구글 캘린더 일정을 날짜 범위로 읽는다.",
     "input_schema": {"type": "object", "properties": {"start": {"type": "string", "description": "YYYY-MM-DD"},
                                                       "end": {"type": "string", "description": "YYYY-MM-DD"}},
                      "required": ["start"]}},
]


def at(day, h, m=0):
    return datetime(2026, 11, day, h, m, tzinfo=KST)


# (now, utterance, check(tool, args, text) -> bool, what is expected)
CASES = [
    (at(23, 0, 30), "내일 오후 1시에 전화해줘",
     lambda t, a, x: t == "call_user" and str(a.get("when", "")).startswith("2026-11-23T13:00"), "call 11-23 13:00"),
    (at(23, 1), "내일 아침 7시에 전화로 깨워줘",
     lambda t, a, x: t is None and "24일" in x, "no tool, asks with 24일"),
    (at(23, 1), "내일 일정 뭐 있어?",
     lambda t, a, x: t == "calendar_events" and str(a.get("start", "")).startswith("2026-11-23"), "calendar 11-23"),
    (at(23, 6), "내일 오후 1시에 전화해줘",
     lambda t, a, x: t == "call_user" and str(a.get("when", "")).startswith("2026-11-24T13:00"), "call 11-24 13:00"),
]


def system(now):
    return (f"너는 음성 비서야. 짧게 존댓말로 답해. 지금은 {now:%Y-%m-%d %H:%M} (KST, {'월화수목금토일'[now.weekday()]}요일)이야. "
            "필요하면 도구를 써." + L.prompt_rule(now))


async def qwen(c, now, text):
    tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                               "parameters": t["input_schema"]}} for t in TOOLS]
    r = await c.post(QWEN, json={"model": QWEN_MODEL, "messages": [{"role": "system", "content": system(now)},
                                                                     {"role": "user", "content": text}],
                                 "tools": tools, "max_tokens": 400, "temperature": 0.2,
                                 "chat_template_kwargs": {"enable_thinking": False}})
    m = r.json()["choices"][0]["message"]
    call = (m.get("tool_calls") or [None])[0]
    if call:
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except ValueError:
            args = {}
        return call["function"]["name"], args, m.get("content") or ""
    return None, {}, m.get("content") or ""


async def claude(c, model, now, text):
    r = await c.post("https://api.anthropic.com/v1/messages", headers={
        "x-api-key": KEY.read_text().strip(), "anthropic-version": "2023-06-01"}, json={
        "model": model, "max_tokens": 400, "system": system(now), "tools": TOOLS,
        "messages": [{"role": "user", "content": text}]})
    blocks = r.json().get("content") or []
    use = next((b for b in blocks if b["type"] == "tool_use"), None)
    say = "".join(b.get("text", "") for b in blocks if b["type"] == "text")
    return (use["name"], use["input"], say) if use else (None, {}, say)


async def main():
    paths = {"qwen": lambda c, n, t: qwen(c, n, t),
             "haiku": lambda c, n, t: claude(c, "claude-haiku-5-5", n, t),
             "sonnet": lambda c, n, t: claude(c, "claude-sonnet-5-5", n, t)}
    score = {p: 0 for p in paths}
    async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
        for now, text, ok, want in CASES:
            for p, f in paths.items():
                try:
                    tool, args, say = await f(c, now, text)
                except Exception as e:  # noqa: BLE001
                    tool, args, say = None, {}, f"ERROR {type(e).__name__}"
                good = ok(tool, args, say)
                score[p] += good
                print(f"{now:%H:%M} {text:22} {p:6} {'OK  ' if good else 'FAIL'} want {want:22} | {tool} {args} {say[:70]!r}")
    print(" | ".join(f"{p} {s}/{len(CASES)}" for p, s in score.items()))

if __name__ == "__main__":
    asyncio.run(main())
