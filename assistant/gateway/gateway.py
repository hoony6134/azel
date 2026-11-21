"""김비서 gateway: Claude access, settings, usage, devices, approvals, calls, push, scheduler.

Contract: ../PROTOCOL.md section 3. Run: uv run gateway.py  (binds the Tailscale IP only)
"""
import asyncio
import copy
import hmac
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

HOME = Path(os.environ.get("BISEO_HOME", Path.home() / ".config/biseo"))
TOKEN = (HOME / "token").read_text().strip()
SETTINGS_PATH = HOME / "settings.json"
APNS_PATH = HOME / "apns.json"  # {"key_path","key_id","team_id","bundle_id"}
HOST = os.environ.get("BISEO_HOST", "100.64.0.8")
PORT = int(os.environ.get("BISEO_PORT", "8892"))
VOICE_URL = os.environ.get("BISEO_VOICE_URL", "http://100.64.0.11:8891")
TTS_URL = os.environ.get("BISEO_TTS_URL", "http://100.64.0.11:8890")
CLAUDE = os.environ.get("BISEO_CLAUDE", "claude")
WORKDIR = HOME / "claude-work"  # empty cwd so no project CLAUDE.md leaks into answers
KST = timezone(timedelta(hours=9))
DEVICE_FRESH_S = 150

MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
DEFAULTS = {
    "default_model": "auto", "claude_path": "auto",
    "task_defaults": {"chat": {"model": "qwen"}, "schedule": {"model": "sonnet"},
                      "code": {"model": "opus", "path": "claude_code"}, "mac": {"model": "sonnet"}},
    "monthly_budget_usd": 500, "budget_warn_pct": 80,
    "proactive": {"morning_briefing": "08:00", "deadline_check": "20:00", "quiet_hours": ["23:30", "07:30"],
                  "triggers": {"deadline": True, "server_down": True}},
    "default_device": "맥북", "device_fallback": ["알비레오", "스파크"],
}
VOICE_STYLE = (
    "지금은 음성 통화 중이고 답은 그대로 소리로 읽혀. 한국어 존댓말로 두세 문장 안에 요점만 말해. "
    "마크다운, 목록, 표, 이모지, URL, 코드 블록을 쓰지 마. '확인하지 않은 것' 같은 꼬리말도 붙이지 마. "
    "사용자 데이터는 Cygnus MCP(메모리, GISTRA, Planit, 인프라), 캘린더, 메일 도구로 확인해. "
    "맥북이나 서버에서 할 일은 등록된 기기 MCP 도구로 해. 위험한 명령이 승인 대기면 앱에서 승인해 달라고 말해. "
    "커밋이나 파일에 AI 작성자 표시를 절대 남기지 마."
)


# ---------- storage ----------
class DB:
    def __init__(self, path):
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.con.executescript("""
            create table if not exists usage(ts real, model text, path text, task text,
                input_tokens int, output_tokens int, cost_usd real);
            create table if not exists devices(id text primary key, name text, url text,
                harness text, last_seen real);
            create table if not exists push_tokens(token text primary key, bundle_id text, env text, ts real);
            create table if not exists calls(id text primary key, reason text, opening text,
                status text, created real);
            create table if not exists approvals(id text primary key, device text, tool text,
                command text, cwd text, status text, created real, decided real);
            create table if not exists sessions(voice_session text primary key, claude_session text);
        """)

    def q(self, sql, args=()):
        with self.lock:
            rows = [dict(r) for r in self.con.execute(sql, args).fetchall()]  # read before commit (RETURNING)
            self.con.commit()
            return rows


db: DB


def load_settings():
    s = copy.deepcopy(DEFAULTS)
    if SETTINGS_PATH.exists():
        s.update(json.loads(SETTINGS_PATH.read_text()))
    return s


def save_settings(s):
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2))
    tmp.replace(SETTINGS_PATH)


def month_cost():
    start = datetime.now(KST).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    return db.q("select coalesce(sum(cost_usd),0) c from usage where ts>=?", (start,))[0]["c"]


def auth(request: Request):
    got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(got.encode(), TOKEN.encode()):
        raise HTTPException(401, "bad token")


# ---------- Claude ----------
def fresh_devices():
    return db.q("select * from devices where last_seen>?", (time.time() - DEVICE_FRESH_S,))


def resolve(settings, model, path, task):
    td = settings["task_defaults"].get(task or "", {})
    model = model or td.get("model") or settings["default_model"]
    if model in ("auto", "qwen"):  # the voice server only calls us when Claude is wanted
        model = "sonnet"
    path = path or td.get("path") or settings["claude_path"]
    if path == "auto":
        path = "api" if os.environ.get("ANTHROPIC_API_KEY") else "claude_code"
    if model not in MODELS:
        raise HTTPException(400, f"model must be one of {sorted(MODELS)} or auto")
    return model, path


def device_prompt(settings, devices):
    """Tell Claude which device to use by default and in what order to fall back."""
    if not devices:
        return " 지금 켜져 있는 작업 기기가 없어. 기기 작업은 할 수 없다고 말해."
    on = {d["name"]: d["id"] for d in devices}
    order = [n for n in [settings["default_device"], *settings["device_fallback"]] if n in on]
    order += [n for n in on if n not in order]
    names = ", ".join(f"{n}(mcp__{on[n]})" for n in order)
    first = settings["default_device"]
    note = "" if order[0] == first else f" 기본 기기 {first}는 꺼져 있어. {order[0]}에서 했다고 꼭 말해."
    return (f" 켜져 있는 기기, 우선순위 순: {names}. 사용자가 기기를 말하지 않으면 맨 앞 기기를 써.{note}")


async def ask_claude_code(text, model, session):
    """Yield text deltas, then a final dict with usage, from `claude -p` stream-json."""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    devices = fresh_devices()
    style = VOICE_STYLE + device_prompt(load_settings(), devices)
    cmd = [CLAUDE, "-p", text, "--model", MODELS[model], "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--append-system-prompt", style,
           "--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit",
           "--allowedTools", "mcp__claude_ai_Cygnus_MCP", "mcp__claude_ai_Google_Calendar",
           "mcp__claude_ai_Gmail", *[f"mcp__{d['id']}" for d in devices]]
    prev = db.q("select claude_session from sessions where voice_session=?", (session,))
    if prev:
        cmd += ["--resume", prev[0]["claude_session"]]
    cfg = None
    if devices:
        fd, cfg = tempfile.mkstemp(suffix=".json")  # holds the token: 0600, deleted after the call
        with os.fdopen(fd, "w") as f:
            json.dump({"mcpServers": {d["id"]: {"type": "http", "url": d["url"],
                                                "headers": {"Authorization": f"Bearer {TOKEN}"}}
                                      for d in devices}}, f)
        cmd += ["--mcp-config", cfg]
    env = os.environ | {"MCP_CONNECTION_NONBLOCKING": "false"}  # wait for claude.ai connectors
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=WORKDIR, env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL, limit=2**24)
    final = {"model": model, "path": "claude_code"}
    try:
        async for raw in proc.stdout:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if e.get("type") == "stream_event":
                ev = e["event"]
                if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                    yield ev["delta"]["text"]
                elif ev.get("type") == "message_stop":
                    yield "\n"  # turns between tool calls are separate utterances
            elif e.get("type") == "result":
                u = e.get("usage", {})
                final.update(cost_usd=e.get("total_cost_usd") or 0, input_tokens=u.get("input_tokens", 0),
                             output_tokens=u.get("output_tokens", 0), error=e.get("is_error", False))
                if e.get("session_id"):
                    db.q("insert or replace into sessions values(?,?)", (session, e["session_id"]))
        await proc.wait()
    finally:
        if proc.returncode is None:
            proc.kill()
        if cfg:
            os.unlink(cfg)
    yield final


async def ask_api(text, model, session):
    """Anthropic Messages API, streamed. No tools on this path (shortcut: tools only via claude_code)."""
    key = os.environ["ANTHROPIC_API_KEY"]
    body = {"model": MODELS[model], "max_tokens": 600, "stream": True, "system": VOICE_STYLE,
            "messages": [{"role": "user", "content": text}]}
    final = {"model": model, "path": "api", "input_tokens": 0, "output_tokens": 0}
    async with httpx.AsyncClient(timeout=120) as c, c.stream(
            "POST", "https://api.anthropic.com/v1/messages", json=body,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"}) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if not line.startswith("data: "):
                continue
            e = json.loads(line[6:])
            if e["type"] == "content_block_delta" and e["delta"].get("type") == "text_delta":
                yield e["delta"]["text"]
            elif e["type"] == "message_start":
                final["input_tokens"] = e["message"]["usage"]["input_tokens"]
            elif e["type"] == "message_delta":
                final["output_tokens"] = e["usage"]["output_tokens"]
    final["cost_usd"] = 0.0  # shortcut: price table not wired; add per-model prices when an API key is used
    yield final


async def ask_stream(text, session, model=None, path=None, task=None):
    settings = load_settings()
    budget = settings["monthly_budget_usd"]
    if month_cost() >= budget:
        yield {"type": "error", "code": "budget", "message": "이번 달 Claude 한도를 다 썼습니다."}
        return
    model, path = resolve(settings, model, path, task)
    gen = ask_claude_code if path == "claude_code" else ask_api
    async for item in gen(text, model, session):
        if isinstance(item, str):
            yield {"type": "text", "delta": item}
        else:
            db.q("insert into usage values(?,?,?,?,?,?,?)", (time.time(), model, path, task or "",
                 item.get("input_tokens", 0), item.get("output_tokens", 0), item.get("cost_usd", 0)))
            spent = month_cost()
            yield {"type": "done", **item, "budget_warning": spent >= budget * settings["budget_warn_pct"] / 100}


async def ask_text(text, session, **kw):
    out = []
    async for ev in ask_stream(text, session, **kw):
        if ev["type"] == "text":
            out.append(ev["delta"])
    return "".join(out).strip()


# ---------- push / calls ----------
_apns_jwt = {"token": None, "ts": 0}


def apns_config():
    return json.loads(APNS_PATH.read_text()) if APNS_PATH.exists() else None


async def send_voip_push(payload):
    cfg = apns_config()
    tokens = db.q("select * from push_tokens")
    if not cfg or not tokens:
        return {"sent": 0, "reason": "APNs key or device token not configured"}
    if time.time() - _apns_jwt["ts"] > 50 * 60:  # APNs wants a fresh JWT at most hourly
        key = Path(cfg["key_path"]).expanduser().read_text()
        _apns_jwt.update(ts=time.time(), token=jwt.encode(
            {"iss": cfg["team_id"], "iat": int(time.time())}, key, algorithm="ES256",
            headers={"kid": cfg["key_id"]}))
    sent = 0
    async with httpx.AsyncClient(http2=True, timeout=10) as c:
        for t in tokens:
            host = "api.sandbox.push.apple.com" if t["env"] == "sandbox" else "api.push.apple.com"
            r = await c.post(f"https://{host}/3/device/{t['token']}", json=payload, headers={
                "authorization": f"bearer {_apns_jwt['token']}", "apns-topic": f"{t['bundle_id']}.voip",
                "apns-push-type": "voip", "apns-priority": "10", "apns-expiration": "0"})
            sent += r.status_code == 200
    return {"sent": sent}


def in_quiet_hours(settings, now=None):
    now = (now or datetime.now(KST)).strftime("%H:%M")
    start, end = settings["proactive"]["quiet_hours"]
    return (start <= now or now < end) if start > end else (start <= now < end)


async def place_call(reason, opening=None):
    if opening is None and reason == "morning_briefing":
        opening = await ask_text(
            "아침 브리핑 전화를 걸었어. 오늘 수업과 마감(GISTRA), 오늘 할 일(Planit), 캘린더 일정, "
            "밤사이 서버 상태(Cygnus 인프라)를 확인해서 '좋은 아침이에요'로 시작하는 30초 안쪽 브리핑으로 말해.",
            session=f"briefing-{uuid.uuid4().hex[:8]}", model="sonnet", task="schedule")
    call_id = uuid.uuid4().hex
    db.q("insert into calls values(?,?,?,?,?)", (call_id, reason, opening or "", "ringing", time.time()))
    # The opening must reach the voice server before the phone can answer and connect.
    await link_send({"type": "call", "call_id": call_id, "opening": opening or ""})
    push = await send_voip_push({"call_id": call_id, "reason": reason, "caller": "김비서"})
    return {"call_id": call_id, **push}


DEADLINE_PROMPT = (
    "GISTRA 과제·시험 마감과 Planit 할 일 중 지금부터 24시간 안에 끝나는데 아직 안 끝난 것을 확인해. "
    f"하나도 없으면 정확히 {'NONE'} 한 단어만 답해. 있으면 '마감이 다가오는 게 있어요'로 시작해서 "
    "무엇이 언제까지인지 두세 문장으로 말해."
)


async def deadline_call():
    text = await ask_text(DEADLINE_PROMPT, session=f"deadline-{uuid.uuid4().hex[:8]}", model="sonnet",
                          task="schedule")
    if text and text.strip().upper() != "NONE" and "NONE" not in text[:10]:
        return await place_call("deadline", text)
    return {"called": False}


async def scheduler():
    """Once a minute: fire the morning briefing and the daily deadline check at their times."""
    fired: dict = {}
    while True:
        try:
            s = load_settings()
            now = datetime.now(KST)
            hm, today = now.strftime("%H:%M"), now.date()
            jobs = {"morning_briefing": lambda: place_call("morning_briefing")}
            if s["proactive"]["triggers"].get("deadline"):
                jobs["deadline_check"] = deadline_call
            for key, job in jobs.items():
                if hm == s["proactive"].get(key) and fired.get(key) != today:
                    fired[key] = today
                    if not in_quiet_hours(s, now):
                        await job()
        except Exception as e:  # never let the loop die
            print("scheduler error:", e, flush=True)
        await asyncio.sleep(60 - datetime.now().second)


# ---------- link to the voice server ----------
# The Spark may not open connections to jh hosts (Headscale ACL) and must not hold personal
# Claude credentials, so the gateway dials the voice server and serves its asks over one socket.
VOICE_LINK = VOICE_URL.replace("http", "ws", 1) + "/v1/gateway-link"
link: dict = {"ws": None, "lock": asyncio.Lock()}


async def link_send(obj):
    ws = link["ws"]
    if ws is None:
        return False
    async with link["lock"]:
        await ws.send(json.dumps(obj, ensure_ascii=False))
    return True


async def serve_ask(m):
    try:
        async for ev in ask_stream(m["text"], m.get("session") or "default", m.get("model"), None, m.get("task")):
            await link_send({"id": m["id"], **ev})
    except asyncio.CancelledError:
        raise  # barge-in: ask_claude_code's finally kills the claude process
    except Exception as e:
        print("ask failed:", type(e).__name__, e, flush=True)
        await link_send({"id": m["id"], "type": "error", "message": "Claude 요청이 실패했어요."})


async def handle_link(ws):
    tasks: dict[str, asyncio.Task] = {}
    async for raw in ws:
        m = json.loads(raw)
        if m.get("type") == "ask":
            tasks[m["id"]] = asyncio.create_task(serve_ask(m))
        elif m.get("type") == "cancel" and (t := tasks.pop(m.get("id"), None)):
            t.cancel()
        for k in [k for k, t in tasks.items() if t.done()]:
            tasks.pop(k)
    for t in tasks.values():
        t.cancel()


async def voice_link():
    import websockets
    backoff = 1
    while True:
        try:
            async with websockets.connect(VOICE_LINK, additional_headers={"Authorization": f"Bearer {TOKEN}"},
                                          max_size=None, ping_interval=20, open_timeout=10) as ws:
                link["ws"], backoff = ws, 1
                await link_send({"type": "settings", "value": load_settings()})
                print("voice link up", flush=True)
                await handle_link(ws)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("voice link down:", type(e).__name__, e, flush=True)
        finally:
            link["ws"] = None
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


# ---------- HTTP ----------
@asynccontextmanager
async def lifespan(app):
    global db
    HOME.mkdir(parents=True, exist_ok=True)
    db = DB(HOME / "gateway.db")
    tasks = [asyncio.create_task(scheduler()), asyncio.create_task(voice_link())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="biseo-gateway", lifespan=lifespan, dependencies=[Depends(auth)])


@app.post("/v1/ask")
async def ask(body: dict):
    if not body.get("text"):
        raise HTTPException(400, "text required")

    async def gen():
        async for ev in ask_stream(body["text"], body.get("session", "default"), body.get("model"),
                                   body.get("path"), body.get("task")):
            yield json.dumps(ev, ensure_ascii=False) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/v1/settings")
def get_settings():
    return load_settings()


@app.put("/v1/settings")
async def put_settings(body: dict):
    unknown = set(body) - set(DEFAULTS)
    if unknown:
        raise HTTPException(400, f"unknown keys: {sorted(unknown)}")
    s = load_settings() | body
    save_settings(s)
    await link_send({"type": "settings", "value": s})
    return s


@app.get("/v1/usage")
def usage():
    start = datetime.now(KST).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = db.q("select * from usage where ts>=?", (start,))
    by = lambda k: {v: round(sum(r["cost_usd"] for r in rows if r[k] == v), 4) for v in {r[k] for r in rows}}  # noqa: E731
    return {"month": datetime.now(KST).strftime("%Y-%m"), "calls": len(rows), "by_model": by("model"),
            "by_path": by("path"), "cost_usd": round(month_cost(), 4),
            "budget_usd": load_settings()["monthly_budget_usd"]}


async def probe(url):
    t = time.perf_counter()
    try:
        # Headers only: an MCP endpoint may answer GET with a stream that never ends.
        async with httpx.AsyncClient(timeout=3) as c, c.stream(
                "GET", url, headers={"Authorization": f"Bearer {TOKEN}", "Accept": "text/event-stream"}) as r:
            ok = r.status_code < 500
        return {"ok": ok, "ms": round((time.perf_counter() - t) * 1000)}
    except httpx.HTTPError:
        return {"ok": False, "ms": None}


@app.get("/v1/status")
async def status():
    devs = db.q("select * from devices")
    probes = await asyncio.gather(probe(f"{VOICE_URL}/health"), probe(f"{TTS_URL}/health"),
                                  *[probe(d["url"]) for d in devs])
    return {"spark": probes[0], "tts": probes[1], "gateway": {"ok": True, "ms": 0},
            "devices": [{"id": d["id"], "name": d["name"], **p,
                         "fresh": time.time() - d["last_seen"] < DEVICE_FRESH_S} for d, p in zip(devs, probes[2:])]}


@app.post("/v1/devices/register")
def register(body: dict):
    for k in ("id", "name", "url"):
        if not body.get(k):
            raise HTTPException(400, f"{k} required")
    if not body["id"].replace("_", "").replace("-", "").isalnum() or not body["id"].isascii():
        raise HTTPException(400, "id must be an ASCII slug")
    db.q("insert or replace into devices values(?,?,?,?,?)",
         (body["id"], body["name"], body["url"], json.dumps(body.get("harness", [])), time.time()))
    return {"ok": True}


@app.get("/v1/devices")
def devices():
    return [{**d, "harness": json.loads(d["harness"])} for d in db.q("select * from devices")]


@app.post("/v1/push/register")
def push_register(body: dict):
    if not body.get("voip_token") or not body.get("bundle_id"):
        raise HTTPException(400, "voip_token and bundle_id required")
    db.q("insert or replace into push_tokens values(?,?,?,?)",
         (body["voip_token"], body["bundle_id"], body.get("env", "sandbox"), time.time()))
    return {"ok": True}


@app.post("/v1/calls")
async def calls(body: dict):
    return await place_call(body.get("reason", "manual"), body.get("opening"))


@app.get("/v1/calls/{call_id}")
def get_call(call_id: str):
    rows = db.q("select * from calls where id=?", (call_id,))
    if not rows:
        raise HTTPException(404, "no such call")
    db.q("update calls set status='answered' where id=?", (call_id,))
    return rows[0]


@app.post("/v1/alerts")
async def alerts(body: dict):
    s = load_settings()
    firing = [a for a in body.get("alerts", []) if a.get("status") == "firing"
              and a.get("labels", {}).get("severity") == "critical"]
    if not firing or not s["proactive"]["triggers"].get("server_down") or in_quiet_hours(s):
        return {"called": False}
    names = ", ".join(sorted({a["labels"].get("alertname", "알 수 없는 경보") for a in firing}))
    return {"called": True, **await place_call("server_down", f"서버 경보가 왔어요. {names}. 로그를 볼까요, 재시작할까요?")}


@app.post("/v1/approvals")
def create_approval(body: dict):
    aid = uuid.uuid4().hex[:12]
    db.q("insert into approvals values(?,?,?,?,?,?,?,?)", (aid, body.get("device", ""), body.get("tool", ""),
         body.get("command", ""), body.get("cwd", ""), "pending", time.time(), None))
    return {"approval_id": aid}


@app.get("/v1/approvals")
def list_approvals():
    return db.q("select * from approvals where status='pending' order by created desc")


@app.get("/v1/approvals/{aid}")
def get_approval(aid: str):
    rows = db.q("select * from approvals where id=?", (aid,))
    if not rows:
        raise HTTPException(404, "no such approval")
    return rows[0]


@app.post("/v1/approvals/{aid}")
def decide(aid: str, body: dict):
    status = "approved" if body.get("approve") else "rejected"
    n = db.q("update approvals set status=?, decided=? where id=? and status='pending' returning id",
             (status, time.time(), aid))
    if not n:
        raise HTTPException(409, "not pending")
    return {"id": aid, "status": status}


if __name__ == "__main__":
    import uvicorn
    assert HOST.startswith("100.") or HOST.startswith("127."), "bind a Tailscale or loopback address only"
    uvicorn.run(app, host=HOST, port=PORT)
