"""NVIDIA NIM (the hosted, OpenAI-compatible API) as a model provider. Full gateways only.

Settings `providers.nim`: {enabled, base_url, model, prices}. The API key lives only in
HOME/providers/nim_key (0600, directory 0700), written through a write-only endpoint, and is sent nowhere
but base_url, which must be https. Never logged, never returned (`has_key` says whether it exists).
"""
import json
import os
import re
import time
import uuid
from urllib.parse import urlparse

import httpx

DEFAULT = {"enabled": False, "base_url": "https://integrate.api.nvidia.com/v1",
           "model": "nvidia/nemotron-3-ultra-550b-a55b", "prices": {}}  # prices: {model: [usd_in, usd_out] per MTok}
MODELS_TTL_S = 600
transport = None  # tests swap in an ASGI fake of the OpenAI API
_models: dict = {"base": None, "at": 0.0, "ids": []}
no_tools: set[str] = set()  # models that refused a request with tools: asked without them from then on


class NimError(Exception):
    """A speakable failure: `code` is nim_key (missing or refused key), nim_off or nim_error."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def key_path(home):
    return home / "providers" / "nim_key"


def has_key(home) -> bool:
    p = key_path(home)
    return p.is_file() and p.stat().st_size > 0


def read_key(home) -> str:
    return key_path(home).read_text().strip() if has_key(home) else ""


def write_key(home, value: str):
    """Atomic 0600 write (temp file + rename) in a 0700 directory; "" deletes the key."""
    p = key_path(home)
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    p.parent.chmod(0o700)
    if not value:
        p.unlink(missing_ok=True)
        return
    tmp = p.with_name(f".nim_key.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
    tmp.replace(p)


def check_key(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{0,400}", value.strip()):
        raise ValueError("value: the API key (printable, no spaces), or \"\" to delete it")
    return value.strip()


def check(cfg, cur) -> dict:
    """Validated `providers.nim` merged over the current one (a partial update keeps the other fields)."""
    if not isinstance(cfg, dict):
        raise ValueError("providers.nim must be an object")
    cfg = {k: v for k, v in cfg.items() if k != "has_key"}  # read-only: an app may send the document back
    unknown = set(cfg) - set(DEFAULT)
    if unknown:
        raise ValueError(f"providers.nim: unknown keys {sorted(unknown)}")
    out = {**DEFAULT, **cur, **cfg}
    if not isinstance(out["enabled"], bool):
        raise ValueError("providers.nim.enabled: true or false")
    u = urlparse(out["base_url"]) if isinstance(out["base_url"], str) else None
    if not u or u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("providers.nim.base_url: an https URL without credentials or query")
    out["base_url"] = out["base_url"].rstrip("/")
    if not isinstance(out["model"], str) or not re.fullmatch(r"[\w.:/@+-]{1,200}", out["model"]):
        raise ValueError("providers.nim.model: a model id such as nvidia/nemotron-3-ultra-550b-a55b")
    prices = out["prices"]
    if not isinstance(prices, dict) or len(prices) > 50 or not all(
            isinstance(m, str) and isinstance(p, list) and len(p) == 2
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) and 0 <= x <= 1000 for x in p)
            for m, p in prices.items()):
        raise ValueError("providers.nim.prices: {model: [usd per MTok in, out]}")
    return out


def view(cfg, home) -> dict:
    return {**cfg, "has_key": has_key(home)}


def _client(timeout=120):
    return httpx.AsyncClient(timeout=timeout, transport=transport, trust_env=False)


async def list_models(cfg, key) -> list[str]:
    """Model ids from base_url/models, cached 10 min per base_url (a suggestion list; any id may be typed)."""
    if _models["base"] == cfg["base_url"] and time.time() - _models["at"] < MODELS_TTL_S:
        return _models["ids"]
    async with _client(20) as c:
        r = await c.get(f"{cfg['base_url']}/models", headers={"Authorization": f"Bearer {key}"})
    if r.status_code in (401, 403):
        raise NimError("nim_key", "엔비디아 키가 맞지 않아요.")
    r.raise_for_status()
    ids = sorted({m["id"] for m in r.json().get("data", []) if isinstance(m, dict) and isinstance(m.get("id"), str)})
    _models.update(base=cfg["base_url"], at=time.time(), ids=ids)
    return ids


def openai_tools(tools):
    """Messages-API tool definitions ({name, description, input_schema}) as OpenAI function tools."""
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("input_schema") or {"type": "object"}}}
            for t in tools]


async def _stream_once(cfg, key, messages, tools):
    """One streamed chat completion: yields text deltas, then {"tool_calls": [...], "usage": {...}}."""
    body = {"model": cfg["model"], "messages": messages, "stream": True, "max_tokens": 2048,
            "stream_options": {"include_usage": True}, **({"tools": tools} if tools else {})}
    calls, usage = {}, {}
    async with _client() as c:
        async with c.stream("POST", f"{cfg['base_url']}/chat/completions", json=body,
                            headers={"Authorization": f"Bearer {key}", "Accept": "text/event-stream"}) as r:
            if r.status_code in (401, 403):
                raise NimError("nim_key", "엔비디아 키가 맞지 않아요. 앱에서 키를 다시 넣어 주세요.")
            if r.status_code == 400 and tools:
                raise _ToolsRefused()
            if r.status_code >= 400:
                raise NimError("nim_error", "지금은 엔비디아 모델이 답하지 못했어요.")
            async for line in r.aiter_lines():
                if not line.startswith("data:") or line.strip() == "data: [DONE]":
                    continue
                chunk = json.loads(line[5:])
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        yield delta["content"]
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] += fn.get("name") or ""
                        slot["arguments"] += fn.get("arguments") or ""
    yield {"tool_calls": [calls[i] for i in sorted(calls)], "usage": usage}


class _ToolsRefused(Exception):
    pass


async def stream(cfg, key, system, text, tools, run_tool, activity, max_turns=8):
    """Yield text deltas and activity events, then a final usage dict ({"model","path":"nim",...}).

    `tools` are Messages-API definitions; `run_tool(block)` takes {"id","name","input"} and returns a
    tool_result dict. A model that refuses tools (HTTP 400 with tools) is asked again without them."""
    if not key:
        raise NimError("nim_key", "엔비디아 API 키가 아직 없어요. 앱의 설정에서 키를 넣어 주세요.")
    use = openai_tools(tools) if tools and cfg["model"] not in no_tools else []
    messages = [{"role": "system", "content": system}, {"role": "user", "content": text}]
    final = {"model": cfg["model"], "path": "nim", "input_tokens": 0, "output_tokens": 0}
    yield activity("thinking")
    for _ in range(max_turns):
        try:
            out, said = None, []
            async for item in _stream_once(cfg, key, messages, use):
                if isinstance(item, str):
                    said.append(item)
                    yield item
                else:
                    out = item
        except _ToolsRefused:
            no_tools.add(cfg["model"])
            use = []
            continue
        final["input_tokens"] += int(out["usage"].get("prompt_tokens") or 0)
        final["output_tokens"] += int(out["usage"].get("completion_tokens") or 0)
        if not out["tool_calls"]:
            break
        messages.append({"role": "assistant", "content": "".join(said) or None,
                         "tool_calls": [{"id": tc["id"], "type": "function",
                                         "function": {"name": tc["name"], "arguments": tc["arguments"] or "{}"}}
                                        for tc in out["tool_calls"]]})
        for tc in out["tool_calls"]:
            try:
                args = json.loads(tc["arguments"] or "{}")
            except json.JSONDecodeError:
                args = None
            yield activity("tool_start", tc["name"])
            res = (await run_tool({"id": tc["id"], "name": tc["name"], "input": args}) if isinstance(args, dict)
                   else {"content": "arguments were not valid JSON", "is_error": True})
            yield activity("tool_end", tc["name"])
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(res.get("content", ""))[:20000]})
    pin, pout = cfg["prices"].get(cfg["model"], (0, 0))
    final["cost_usd"] = (final["input_tokens"] * pin + final["output_tokens"] * pout) / 1e6
    yield final
