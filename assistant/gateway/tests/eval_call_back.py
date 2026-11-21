"""Must-pass: "오픈 자비스에 대해 조사하고 끝나면 나한테 전화 줘" on every path ends in a call-back task.

The full chain against a test gateway served on 127.0.0.1 (in-memory state):
the request (claude_code: a real `claude -p` calling this gateway's /v1/mcp; claude_api; NIM; Qwen: the voice
server's own fast path) → create_task with notify=call and the call's origin_session → the task in the app's list →
the task finishes (its research is stubbed: this checks the routing, not the research) → one call is placed whose
opening carries the result and continues the call it came from.

Nothing real happens: place_call is stubbed, the research is stubbed, and the only push tool Claude can see is a
local stand-in notify_send that records (the live bug was Claude choosing Cygnus notify_send). Paid: one Sonnet
claude -p run and one Sonnet API turn, well under a dollar.

  env -u CONDA_PREFIX uv run --frozen --with uvicorn,numpy,soundfile,pysilero-vad,python-multipart \
      python tests/eval_call_back.py [claude_code api nim qwen]
"""
import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from test_gateway import start  # noqa: E402

UTTER = "오픈 자비스에 대해 조사하고 끝나면 나한테 전화 줘"
RESULT = "오픈 자비스는 음성으로 컴퓨터를 다루는 오픈소스 비서 프로젝트예요. 깃허브에서 활발하게 개발되고 있어요."
REAL = Path.home() / ".config/biseo"
V2 = {"Authorization": "Bearer t0k", "X-Azel-Result": "2"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app, port):
    """uvicorn in a thread; returns (server, loop) once it accepts connections."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(server.serve()), daemon=True).start()
    for _ in range(100):
        if server.started:
            return server, loop
        time.sleep(0.05)
    raise RuntimeError("server did not start")


def fake_notify_mcp(pushed):
    """A stand-in for Cygnus' notify_send: the only push tool in reach, and it only records."""
    app = FastAPI()

    @app.post("/mcp")
    async def mcp(msg: dict):
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            return {}
        if method == "initialize":
            res = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "cygnus-standin", "version": "1"}}
        elif method == "tools/list":
            res = {"tools": [{"name": "notify_send", "description": "사용자 폰에 푸시 알림을 보낸다.",
                              "inputSchema": {"type": "object", "properties": {
                                  "title": {"type": "string"}, "body": {"type": "string"}}}}]}
        elif method == "tools/call":
            pushed.append(msg.get("params"))
            res = {"content": [{"type": "text", "text": "보냈어요."}]}
        else:
            res = {}
        return {"jsonrpc": "2.0", "id": mid, "result": res}
    return app


def main(paths):
    tmp = Path(tempfile.mkdtemp())
    for name in ("anthropic_api_key",):
        shutil.copyfile(REAL / name, tmp / name)
        (tmp / name).chmod(0o600)
    (tmp / "providers").mkdir()
    if (REAL / "providers/nim_key").exists():
        shutil.copyfile(REAL / "providers/nim_key", tmp / "providers/nim_key")
        (tmp / "providers/nim_key").chmod(0o600)
    port, notify_port = free_port(), free_port()
    pushed, placed = [], []
    serve(fake_notify_mcp(pushed), notify_port)
    mp = pytest.MonkeyPatch()
    g = start(tmp, mp, BISEO_DEVICE="macbook", BISEO_MODE="full", BISEO_HOST="127.0.0.1", BISEO_PORT=str(port),
              BISEO_CLAUDE=shutil.which("claude") or "claude",
              BISEO_LLM_URL=os.environ.get("BISEO_LLM_URL", "http://100.64.0.11:8888/v1/chat/completions"))

    async def fake_place(reason, opening=None, call_id=None, persona_id=None, origin_session=None, **kw):
        placed.append({"reason": reason, "opening": opening or "", "origin_session": origin_session})
        return {"call_id": call_id or "evalcall"}
    g.place_call = fake_place

    async def research(backend, system, goal, bridge, cfg, budget, minutes, cancelled, persona_id=None):
        yield {"type": "done", "result": RESULT, "cost_usd": 0.0, "steps": 1}  # stubbed: the chain is the point
    g.agent_loop.run = research
    real_settings = g.load_settings

    async def settings():
        s = await real_settings()
        s["connectors"] = {**(s.get("connectors") or {}), "custom": [
            {"name": "cygnus-standin", "url": f"http://127.0.0.1:{notify_port}/mcp", "enabled": True}]}
        s["providers"]["nim"]["enabled"] = (tmp / "providers/nim_key").exists()
        return s
    g.load_settings = settings
    _, loop = serve(g.app, port)
    c = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=V2, timeout=300)

    def tasks():
        return c.get("/v1/tasks", params={"limit": 20}).json()["data"]

    rows = []
    for path in paths:
        session = f"azel:eval-{path}"
        before, calls0, pushed0 = {t["id"] for t in tasks()}, len(placed), len(pushed)
        said = ""
        if path == "qwen":  # the voice fast path: its own regex decides, then a task frame with the call's origin
            sys.argv = [sys.argv[0], "--check"]
            sys.path.insert(0, str(HERE.parents[1] / "spark"))
            import voice_server as V
            notify = V.task_intent(UTTER)
            m = {"type": "task", "id": "e1", "goal": UTTER, "notify": notify, "persona": "azel",
                 "origin_session": session}
            sent = []

            async def link_send(obj):
                sent.append(obj)
                return True
            g.link_send = link_send
            async def go():  # served() makes an asyncio task: it must run in the gateway's own loop
                return await g.served(g.serve_task(m), m)
            asyncio.run_coroutine_threadsafe(go(), loop).result(30)
            said = (sent[-1] if sent else {}).get("say", "")
        else:
            body = {"text": UTTER, "session": session, "path": "api" if path == "api" else "claude_code",
                    **({"model": "nim"} if path == "nim" else {"model": "sonnet"})}
            with c.stream("POST", "/v1/ask", json=body) as r:
                for line in r.iter_lines():
                    if line:
                        ev = json.loads(line)
                        said += ev.get("delta", "") if ev.get("type") == "text" else ""
        new = [t for t in tasks() if t["id"] not in before]
        t = new[0] if new else None
        end = time.time() + 60
        while t and (len(placed) == calls0 or t["status"] not in ("done", "failed")) and time.time() < end:
            time.sleep(0.5)
            t = next((x for x in tasks() if x["id"] == t["id"]), t)
        stored = asyncio.run_coroutine_threadsafe(g.state.call("GET", g.task_key(t["id"])), loop).result(10) if t else {}
        calls = placed[calls0:]
        checks = {
            "one task": len(new) == 1,
            "notify=call": bool(t) and t.get("notify") == "call",
            "origin linked": (stored or {}).get("origin_session") == session,
            "in the app's list": bool(t),
            "one call, after it finished": len(calls) == 1 and calls[0]["reason"] == "task",
            "opening has the result": bool(calls) and "오픈소스 비서" in calls[0]["opening"],
            "call continues the origin": bool(calls) and calls[0]["origin_session"] == session,
            "no push instead": len(pushed) == pushed0,
        }
        rows.append((path, checks, said, calls[0]["opening"] if calls else ""))
    for path, checks, said, opening in rows:
        ok = all(checks.values())
        print(f"{path:11} {'PASS' if ok else 'FAIL'} " + ", ".join(f"{k}{'' if v else ' ✗'}" for k, v in checks.items()))
        print(f"            said: {said[:100]!r}")
        print(f"            call: {opening[:120]!r}")
    print(" | ".join(f"{p} {'PASS' if all(ch.values()) else 'FAIL'}" for p, ch, *_ in rows))
    mp.undo()

if __name__ == "__main__":
    main(sys.argv[1:] or ["qwen", "api", "nim", "claude_code"])
