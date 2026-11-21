"""Azel memory: raw records (hybrid search), task context, contacts, and the gateways' shared state.
Long-term facts stay in Cygnus MCP.

Contract: ../PROTOCOL.md section 5. Runs on ruchba in Docker (compose.yaml), Tailscale IP only.
"""
import hmac
import ipaddress
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import numpy as np
import onnxruntime as ort
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from huggingface_hub import hf_hub_download
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field
from tokenizers import Tokenizer

HOME = Path(os.environ.get("BISEO_HOME", Path.home() / ".config/biseo"))
TOKEN = (HOME / "token").read_text().strip()
DB_URL = os.environ.get("BISEO_DB_URL") or (HOME / "db_url").read_text().strip()
HOST = os.environ.get("BISEO_HOST", "100.64.0.10")
PORT = int(os.environ.get("BISEO_PORT", "8894"))
# Measured on ruchba (2 vCPU x86, no GPU): ~66 ms per short text, Korean paraphrases rank first.
MODEL_REPO, MODEL_FILE = "Xenova/multilingual-e5-small", "onnx/model_quantized.onnx"
KINDS = ("transcript", "task_log", "daemon_result", "note")
W_VEC, W_LEX, W_FRESH, FRESH_DAYS = 0.6, 0.3, 0.1, 30
STATE = os.environ.get("BISEO_STATE_SCHEMA", "state")
assert STATE.replace("_", "").isalnum(), "BISEO_STATE_SCHEMA must be a plain identifier"
# Team machines (vd-spark, vd-core) run limited gateways. Two independent gates keep them to the routes
# marked `lim`: the scoped token in state_token_limited, and BISEO_LIMITED_IPS, which also limits the
# shared token from those tailnet IPs. With BISEO_LIMITED_IPS set the IP gate fails closed: a peer that
# is not a tailnet address (Docker's userland proxy shows 172.x bridge IPs) is treated as limited.
LIMITED_IPS = {ip.strip() for ip in os.environ.get("BISEO_LIMITED_IPS", "").split(",") if ip.strip()}
TAILNET = ipaddress.ip_network("100.64.0.0/10")


def load_limited_token(home: Path, token: str):
    path = home / "state_token_limited"
    if not path.exists():
        return None
    limited = path.read_text().strip()
    assert limited and limited != token, "state_token_limited must be set and differ from the shared token"
    return limited


LIMITED_TOKEN = load_limited_token(HOME, TOKEN)

SCHEMA = """
create extension if not exists pg_trgm;
create table if not exists records(
    id bigserial primary key, kind text not null, session text, device text, text text not null,
    meta jsonb not null default '{}', created timestamptz not null default now(), embedding real[] not null);
create index if not exists records_trgm on records using gin (text gin_trgm_ops);
create index if not exists records_created on records (created desc);
create table if not exists context(
    task_id text primary key, summary text not null default '',
    open_questions jsonb not null default '[]', updated timestamptz not null default now());
create table if not exists contacts(
    id bigserial primary key, name text not null, phone text, email text, notes text,
    updated timestamptz not null default now());
"""
# Gateways' shared state (PROTOCOL.md section 5, "State"). Timestamps go out as epoch seconds.
STATE_SCHEMA = f"""
create schema if not exists {STATE};
create table if not exists {STATE}.kv(key text primary key, value jsonb not null, updated timestamptz not null default now());
create table if not exists {STATE}.usage(
    id bigserial primary key, ts timestamptz not null default now(), device text, model text, path text, task text,
    input_tokens int not null default 0, output_tokens int not null default 0, cost_usd double precision not null default 0,
    import_key text unique);
create index if not exists usage_ts on {STATE}.usage (ts);
create table if not exists {STATE}.devices(
    id text primary key, name text not null, url text not null, harness jsonb not null default '[]',
    gateway text, last_seen timestamptz not null default now(), writer text not null default 'limited');
create table if not exists {STATE}.push_tokens(
    token text primary key, bundle_id text not null, env text not null, ts timestamptz not null default now());
create table if not exists {STATE}.calls(
    id text primary key, reason text, opening text, status text, created timestamptz not null default now());
create table if not exists {STATE}.approvals(
    id text primary key, device text, tool text, command text, cwd text, status text not null default 'pending',
    created timestamptz not null default now(), decided timestamptz);
create table if not exists {STATE}.sessions(
    voice_session text primary key, claude_session text not null, device text not null,
    updated timestamptz not null default now());
create table if not exists {STATE}.leases(name text primary key, holder text not null, expires timestamptz not null);
-- columns added after the first state schema (93aacc6): upgrade tables created by that version
alter table {STATE}.usage add column if not exists import_key text unique;
alter table {STATE}.devices add column if not exists writer text not null default 'limited';
alter table {STATE}.leases add column if not exists priority int not null default 100;
alter table {STATE}.push_tokens add column if not exists kind text not null default 'voip';
create table if not exists {STATE}.scheduled_calls(
    id text primary key, at timestamptz not null, repeat text, kind text not null default 'wake',
    note text not null default '', created_by text not null default '', status text not null default 'pending',
    attempts int not null default 0, call_id text, last_attempt timestamptz,
    created timestamptz not null default now());
create index if not exists scheduled_calls_at on {STATE}.scheduled_calls (status, at);
create table if not exists {STATE}.call_history(
    id text primary key, session text, call_id text, direction text, reason text, client text, opening text,
    started_at timestamptz not null default now(), ended_at timestamptz, duration_s double precision);
create index if not exists call_history_started on {STATE}.call_history (started_at desc);
create table if not exists {STATE}.call_turns(
    id bigserial primary key, call text not null references {STATE}.call_history(id) on delete cascade,
    at timestamptz not null default now(), user_text text not null, assistant jsonb not null default '[]',
    route text, activity jsonb not null default '[]', timings jsonb not null default '{{}}',
    interrupted boolean not null default false);
create index if not exists call_turns_call on {STATE}.call_turns (call, id);
"""


class Embedder:
    def __init__(self):
        self.tok = Tokenizer.from_file(hf_hub_download(MODEL_REPO, "tokenizer.json"))
        self.tok.enable_truncation(512)  # shortcut: long transcripts embed their first 512 tokens; chunk if recall suffers
        self.sess = ort.InferenceSession(hf_hub_download(MODEL_REPO, MODEL_FILE), providers=["CPUExecutionProvider"])

    def __call__(self, text, query=False):
        e = self.tok.encode(("query: " if query else "passage: ") + text)  # e5 prefixes
        ids, mask = np.array([e.ids]), np.array([e.attention_mask])
        h = self.sess.run(None, {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)})[0]
        v = (h * mask[..., None]).sum(1)[0] / mask.sum()
        return (v / np.linalg.norm(v)).astype(np.float32).tolist()


pool: ConnectionPool
embed: Embedder


def db(sql, args=None):
    with pool.connection() as con:
        cur = con.execute(sql, args)
        return cur.fetchall() if cur.description else []


def limited_peer(host):
    if not LIMITED_IPS:
        return False
    try:
        return host in LIMITED_IPS or ipaddress.ip_address(host) not in TAILNET
    except ValueError:  # not an IP at all: fail closed
        return True


def role(request: Request):
    """'full' or 'limited' for a request, or 401."""
    got = request.headers.get("authorization", "").removeprefix("Bearer ").strip().encode()
    if hmac.compare_digest(got, TOKEN.encode()):
        return "limited" if limited_peer(request.client.host if request.client else "") else "full"
    if LIMITED_TOKEN and hmac.compare_digest(got, LIMITED_TOKEN.encode()):
        return "limited"
    raise HTTPException(401, "bad token")


def token_only(request: Request):
    role(request)


def auth(request: Request):
    if role(request) != "full":
        raise HTTPException(403, "not allowed for a limited gateway")


@asynccontextmanager
async def lifespan(app):
    global pool, embed
    from psycopg.rows import dict_row
    pool = ConnectionPool(DB_URL, min_size=1, max_size=4, open=True, kwargs={"autocommit": True, "row_factory": dict_row})
    db(SCHEMA)
    db(STATE_SCHEMA)
    embed = Embedder()
    yield
    pool.close()


app = FastAPI(title="biseo-memory", lifespan=lifespan)
v1 = Depends(auth)
lim = Depends(token_only)  # also open to limited hosts


@app.get("/health")
def health():
    db("select 1")
    return {"ok": True}


class Record(BaseModel):
    kind: str
    text: str = Field(min_length=1)
    session: str | None = None
    device: str | None = None
    meta: dict = {}


@app.post("/v1/records", dependencies=[v1])
def add_record(r: Record):
    if r.kind not in KINDS:
        raise HTTPException(400, f"kind must be one of {KINDS}")
    return db("insert into records(kind, session, device, text, meta, embedding) values(%s,%s,%s,%s,%s,%s)"
             " returning id, created", (r.kind, r.session, r.device, r.text, Jsonb(r.meta), embed(r.text)))[0]


class Search(BaseModel):
    query: str = Field(min_length=1)
    kinds: list[str] | None = None
    limit: int = Field(10, ge=1, le=100)
    since: datetime | None = None


@app.post("/v1/search", dependencies=[v1])
def search(s: Search):
    # shortcut: pgvector isn't in ruchba's postgres image, so cosine runs over real[] for every candidate
    # row (embeddings are unit length, so dot = cosine). Fine for tens of thousands; switch to pgvector + HNSW beyond.
    rows = db(f"""
        select id, kind, session, device, text, meta, created, vec, lex, fresh,
               {W_VEC}*vec + {W_LEX}*lex + {W_FRESH}*fresh as score
        from (select r.*,
                (select sum(a*b) from unnest(r.embedding, %(e)s::real[]) x(a, b)) as vec,
                greatest(word_similarity(%(q)s, r.text),
                         least(1, ts_rank_cd(to_tsvector('simple', r.text), plainto_tsquery('simple', %(q)s)))) as lex,
                exp(-extract(epoch from now() - r.created) / 86400 / {FRESH_DAYS}) as fresh
              from records r
              where (%(kinds)s::text[] is null or r.kind = any(%(kinds)s))
                and (%(since)s::timestamptz is null or r.created >= %(since)s)) t
        order by score desc limit %(limit)s""",
             {"e": embed(s.query, query=True), "q": s.query, "kinds": s.kinds, "since": s.since, "limit": s.limit})
    return [{**r, **{k: round(float(r[k]), 4) for k in ("score", "vec", "lex", "fresh")}} for r in rows]


class Context(BaseModel):
    summary: str = ""
    open_questions: list[str] = []


@app.put("/v1/context/{task_id}", dependencies=[v1])
def put_context(task_id: str, c: Context):
    return db("insert into context values(%s,%s,%s,now()) on conflict(task_id) do update set"
             " summary=excluded.summary, open_questions=excluded.open_questions, updated=now() returning *",
             (task_id, c.summary, Jsonb(c.open_questions)))[0]


@app.get("/v1/context/{task_id}", dependencies=[v1])
def get_context(task_id: str):
    rows = db("select * from context where task_id=%s", (task_id,))
    if not rows:
        raise HTTPException(404, "no such task")
    return rows[0]


@app.get("/v1/context", dependencies=[v1])
def recent_context(recent: int = 10):
    return db("select * from context order by updated desc limit %s", (max(1, min(recent, 100)),))


class Contact(BaseModel):
    name: str = Field(min_length=1)
    phone: str | None = None
    email: str | None = None
    notes: str | None = None


@app.post("/v1/contacts", dependencies=[v1])
def add_contact(c: Contact):
    return db("insert into contacts(name, phone, email, notes) values(%s,%s,%s,%s) returning *",
             (c.name, c.phone, c.email, c.notes))[0]


@app.get("/v1/contacts", dependencies=[v1])
def list_contacts(q: str | None = None):
    if q:
        like = f"%{q}%"
        return db("select * from contacts where name ilike %s or phone like %s or email ilike %s or notes ilike %s"
                 " order by name", (like, like, like, like))
    return db("select * from contacts order by name")



def one_contact(rows):
    if not rows:
        raise HTTPException(404, "no such contact")
    return rows[0]


@app.get("/v1/contacts/{cid}", dependencies=[v1])
def get_contact(cid: int):
    return one_contact(db("select * from contacts where id=%s", (cid,)))


@app.put("/v1/contacts/{cid}", dependencies=[v1])
def put_contact(cid: int, c: Contact):
    return one_contact(db("update contacts set name=%s, phone=%s, email=%s, notes=%s, updated=now() where id=%s"
                         " returning *", (c.name, c.phone, c.email, c.notes, cid)))


@app.delete("/v1/contacts/{cid}", dependencies=[v1])
def delete_contact(cid: int):
    return {"deleted": one_contact(db("delete from contacts where id=%s returning id", (cid,)))["id"]}


# ---------- gateways' shared state ----------
EPOCH = "extract(epoch from {0})::float8 as {0}"


@app.get("/v1/state/settings", dependencies=[lim])
def get_state_settings():
    rows = db(f"select value from {STATE}.kv where key='settings'")
    return rows[0]["value"] if rows else {}


@app.put("/v1/state/settings", dependencies=[v1])
def put_state_settings(body: dict):
    return db(f"insert into {STATE}.kv values('settings', %s, now()) on conflict(key) do update"
              " set value=excluded.value, updated=now() returning value", (Jsonb(body),))[0]["value"]


class Usage(BaseModel):
    device: str | None = None
    model: str
    path: str
    task: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0
    ts: float | None = None  # epoch seconds; set only when replaying old rows (gateway --migrate)
    import_key: str | None = None  # replayed rows carry one, so running the migration twice adds nothing


@app.post("/v1/state/usage", dependencies=[lim])
def add_usage(u: Usage):
    db(f"insert into {STATE}.usage(ts, device, model, path, task, input_tokens, output_tokens, cost_usd, import_key)"
       " values(coalesce(to_timestamp(%s), now()),%s,%s,%s,%s,%s,%s,%s,%s) on conflict (import_key) do nothing",
       (u.ts, u.device, u.model, u.path, u.task, u.input_tokens, u.output_tokens, u.cost_usd, u.import_key))
    return {"ok": True}


@app.get("/v1/state/usage", dependencies=[lim])
def list_usage(since: float = 0):
    return db(f"select {EPOCH.format('ts')}, device, model, path, task, input_tokens, output_tokens, cost_usd"
              f" from {STATE}.usage where ts >= to_timestamp(%s) order by ts", (since,))


class Device(BaseModel):
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    harness: list[str] = []
    gateway: str | None = None


@app.put("/v1/state/devices/{did}", dependencies=[lim])
def put_device(did: str, d: Device, request: Request):
    """Upsert a daemon's row, stamped with the writer's role. A limited writer may not touch a row a full
    writer owns: full gateways send the shared token to these urls and feed their output to Claude."""
    writer = role(request)
    if not db(f"insert into {STATE}.devices values(%s,%s,%s,%s,%s,now(),%s) on conflict(id) do update set"
              " name=excluded.name, url=excluded.url, harness=excluded.harness, gateway=excluded.gateway,"
              f" last_seen=now(), writer=excluded.writer where {STATE}.devices.writer = 'limited'"
              " or excluded.writer = 'full' returning id", (did, d.name, d.url, Jsonb(d.harness), d.gateway, writer)):
        raise HTTPException(403, "device id belongs to a full gateway")
    return {"ok": True}


@app.get("/v1/state/devices", dependencies=[lim])
def list_devices():
    return db(f"select id, name, url, harness, gateway, writer, {EPOCH.format('last_seen')} from {STATE}.devices"
              " order by id")


class PushToken(BaseModel):
    bundle_id: str = Field(min_length=1)
    env: str = "sandbox"
    kind: str = Field("voip", pattern="^(voip|alert)$")  # alert: ordinary notifications (missed scheduled calls)


@app.put("/v1/state/push_tokens/{token}", dependencies=[v1])
def put_push_token(token: str, p: PushToken):
    db(f"insert into {STATE}.push_tokens(token, bundle_id, env, ts, kind) values(%s,%s,%s,now(),%s)"
       " on conflict(token) do update set bundle_id=excluded.bundle_id, env=excluded.env, ts=now(), kind=excluded.kind",
       (token, p.bundle_id, p.env, p.kind))
    return {"ok": True}


@app.get("/v1/state/push_tokens", dependencies=[v1])
def list_push_tokens():
    return db(f"select token, bundle_id, env, kind from {STATE}.push_tokens")


class Call(BaseModel):
    id: str = Field(min_length=1)
    reason: str = ""
    opening: str = ""


@app.post("/v1/state/calls", dependencies=[v1])
def add_call(c: Call):
    db(f"insert into {STATE}.calls values(%s,%s,%s,'ringing',now())", (c.id, c.reason, c.opening))
    return {"ok": True}


@app.post("/v1/state/calls/{cid}/answer", dependencies=[v1])
def answer_call(cid: str):
    rows = db(f"update {STATE}.calls set status='answered' where id=%s"
              f" returning id, reason, opening, status, {EPOCH.format('created')}", (cid,))
    if not rows:
        raise HTTPException(404, "no such call")
    return rows[0]


class Approval(BaseModel):
    id: str | None = Field(None, pattern=r"^[0-9a-f]{12}$")  # kept when migrating; full writers only
    device: str = ""
    tool: str = ""
    command: str = ""
    cwd: str = ""


APPROVAL_COLS = f"id, device, tool, command, cwd, status, {EPOCH.format('created')}, {EPOCH.format('decided')}"


@app.post("/v1/state/approvals", dependencies=[lim])
def add_approval(a: Approval, request: Request):
    if a.id and role(request) != "full":
        raise HTTPException(403, "only full gateways may choose an approval id")
    aid = a.id or uuid.uuid4().hex[:12]
    db(f"insert into {STATE}.approvals(id, device, tool, command, cwd) values(%s,%s,%s,%s,%s)"
       " on conflict (id) do nothing", (aid, a.device, a.tool, a.command, a.cwd))
    return {"approval_id": aid}


@app.get("/v1/state/approvals", dependencies=[v1])
def list_approvals(status: str = "pending"):
    return db(f"select {APPROVAL_COLS} from {STATE}.approvals where status=%s order by created desc", (status,))


@app.get("/v1/state/approvals/{aid}", dependencies=[lim])
def get_approval(aid: str):
    rows = db(f"select {APPROVAL_COLS} from {STATE}.approvals where id=%s", (aid,))
    if not rows:
        raise HTTPException(404, "no such approval")
    return rows[0]


class Decision(BaseModel):
    approve: bool


@app.post("/v1/state/approvals/{aid}", dependencies=[v1])
def decide_approval(aid: str, d: Decision):
    status = "approved" if d.approve else "rejected"
    if not db(f"update {STATE}.approvals set status=%s, decided=now() where id=%s and status='pending' returning id",
              (status, aid)):
        raise HTTPException(409, "not pending")
    return {"id": aid, "status": status}


class Session(BaseModel):
    claude_session: str = Field(min_length=1)
    device: str = Field(min_length=1)


@app.get("/v1/state/sessions/{voice_session}", dependencies=[v1])
def get_session(voice_session: str):
    rows = db(f"select claude_session, device from {STATE}.sessions where voice_session=%s", (voice_session,))
    if not rows:
        raise HTTPException(404, "no such session")
    return rows[0]


@app.put("/v1/state/sessions/{voice_session}", dependencies=[v1])
def put_session(voice_session: str, s: Session):
    db(f"insert into {STATE}.sessions values(%s,%s,%s,now()) on conflict(voice_session) do update"
       " set claude_session=excluded.claude_session, device=excluded.device, updated=now()",
       (voice_session, s.claude_session, s.device))
    return {"ok": True}


class Lease(BaseModel):
    holder: str = Field(min_length=1)
    ttl_s: float = Field(gt=0, le=7 * 86400)
    priority: int = 100  # lower wins: a lower-priority caller takes an unexpired lease from a higher one


@app.post("/v1/state/leases/{name}", dependencies=[v1])
def take_lease(name: str, lease: Lease):
    """Take or renew a lease. Atomic: one holder at a time; free, expired, own, or held at a worse priority."""
    L = f"{STATE}.leases"
    rows = db(f"insert into {L}(name, holder, expires, priority) values(%s,%s,now() + make_interval(secs => %s),%s)"
              " on conflict(name) do update set holder=excluded.holder, expires=excluded.expires,"
              f" priority=excluded.priority where {L}.expires < now() or {L}.holder = excluded.holder"
              f" or excluded.priority < {L}.priority"
              f" returning holder, priority, {EPOCH.format('expires')}", (name, lease.holder, lease.ttl_s, lease.priority))
    if rows:
        return {"held": True, **rows[0]}
    cur = db(f"select holder, priority, {EPOCH.format('expires')} from {STATE}.leases where name=%s", (name,))
    return {"held": False, **(cur[0] if cur else {})}


@app.delete("/v1/state/leases/{name}", dependencies=[v1])
def release_lease(name: str, holder: str):
    return {"released": bool(db(f"delete from {STATE}.leases where name=%s and holder=%s returning name",
                                (name, holder)))}


# ---------- scheduled calls (full gateways only) ----------
SCHED = f"{STATE}.scheduled_calls"
SCHED_COLS = (f"id, {EPOCH.format('at')}, repeat, kind, note, created_by, status, attempts, call_id,"
              f" {EPOCH.format('last_attempt')}, {EPOCH.format('created')}")
REPEAT_RE = r"^(daily|weekdays|weekly:(mon|tue|wed|thu|fri|sat|sun))$"


class Scheduled(BaseModel):
    at: float  # epoch seconds; the gateway checks it is ahead and within 30 days
    repeat: str | None = Field(None, pattern=REPEAT_RE)
    kind: str = Field("wake", pattern="^(wake|reminder)$")
    note: str = Field("", max_length=200)
    created_by: str = Field("", max_length=40)


@app.post("/v1/state/schedule", dependencies=[v1])
def add_scheduled(s: Scheduled):
    return db(f"insert into {SCHED}(id, at, repeat, kind, note, created_by) values(%s,to_timestamp(%s),%s,%s,%s,%s)"
              f" returning {SCHED_COLS}", (uuid.uuid4().hex[:12], s.at, s.repeat, s.kind, s.note, s.created_by))[0]


@app.get("/v1/state/schedule", dependencies=[v1])
def list_scheduled(status: str = "pending,fired"):
    """Items in the given statuses (comma list), soonest first."""
    return db(f"select {SCHED_COLS} from {SCHED} where status = any(%s) order by at", (status.split(","),))


class ScheduledUpdate(BaseModel):
    status: str | None = Field(None, pattern="^(pending|fired|answered|missed|cancelled)$")
    attempts: int | None = Field(None, ge=0, le=10)
    call_id: str | None = None
    at: float | None = None
    last_attempt: float | None = None
    expect_status: str | None = None  # compare-and-set: only update while the row is still in this status


@app.post("/v1/state/schedule/{sid}", dependencies=[v1])
def update_scheduled(sid: str, u: ScheduledUpdate):
    sets, args = [], []
    for col in ("status", "attempts", "call_id"):
        if getattr(u, col) is not None:
            sets.append(f"{col}=%s")
            args.append(getattr(u, col))
    for col in ("at", "last_attempt"):
        if getattr(u, col) is not None:
            sets.append(f"{col}=to_timestamp(%s)")
            args.append(getattr(u, col))
    if not sets:
        raise HTTPException(400, "nothing to update")
    rows = db(f"update {SCHED} set {', '.join(sets)} where id=%s and (%s::text is null or status=%s)"
              f" returning {SCHED_COLS}", (*args, sid, u.expect_status, u.expect_status))
    if not rows:
        raise HTTPException(409 if db(f"select 1 from {SCHED} where id=%s", (sid,)) else 404, "not updated")
    return rows[0]


@app.delete("/v1/state/schedule/{sid}", dependencies=[v1])
def cancel_scheduled(sid: str):
    rows = db(f"update {SCHED} set status='cancelled' where id=%s and status in ('pending','fired')"
              f" returning {SCHED_COLS}", (sid,))
    if not rows:
        raise HTTPException(404, "no such scheduled call")
    return rows[0]


# ---------- call history (full gateways only; personal) ----------
HIST = f"{STATE}.call_history"
TURNS = f"{STATE}.call_turns"


class CallStart(BaseModel):
    session: str = ""
    call_id: str = ""
    direction: str = Field("incoming", pattern="^(incoming|outgoing)$")
    reason: str = ""
    client: str = ""
    opening: str = ""
    started_at: float | None = None


@app.post("/v1/state/history/{hid}/start", dependencies=[v1])
def history_start(hid: str, c: CallStart):
    db(f"insert into {HIST}(id, session, call_id, direction, reason, client, opening, started_at)"
       " values(%s,%s,%s,%s,%s,%s,%s,coalesce(to_timestamp(%s), now())) on conflict(id) do update set"
       " session=excluded.session, call_id=excluded.call_id, direction=excluded.direction, reason=excluded.reason,"
       " client=excluded.client, opening=excluded.opening, started_at=excluded.started_at",
       (hid, c.session, c.call_id, c.direction, c.reason, c.client, c.opening, c.started_at))
    return {"ok": True}


class CallTurn(BaseModel):
    user: str = Field(min_length=1)
    assistant: list[str] = []
    route: str = ""
    activity: list[dict | str] = []  # {"label","kind","at": epoch}; plain labels from older voice servers
    timings: dict = {}
    interrupted: bool = False
    at: float | None = None


@app.post("/v1/state/history/{hid}/turns", dependencies=[v1])
def history_turn(hid: str, t: CallTurn):
    db(f"insert into {HIST}(id) values(%s) on conflict(id) do nothing", (hid,))  # a turn may beat its start
    db(f"insert into {TURNS}(call, at, user_text, assistant, route, activity, timings, interrupted)"
       " values(%s,coalesce(to_timestamp(%s), now()),%s,%s,%s,%s,%s,%s)",
       (hid, t.at, t.user, Jsonb(t.assistant), t.route, Jsonb(t.activity), Jsonb(t.timings), t.interrupted))
    return {"ok": True}


class CallEnd(BaseModel):
    ended_at: float | None = None
    duration_s: float | None = None


@app.post("/v1/state/history/{hid}/end", dependencies=[v1])
def history_end(hid: str, e: CallEnd):
    """Close the call and file its transcript as a `transcript` record, so memory search finds past calls."""
    rows = db(f"update {HIST} set ended_at=coalesce(to_timestamp(%s), now()), duration_s=coalesce(%s,"
              " extract(epoch from coalesce(to_timestamp(%s), now()) - started_at)) where id=%s"
              " returning session, client, direction", (e.ended_at, e.duration_s, e.ended_at, hid))
    if not rows:
        raise HTTPException(404, "no such call")
    turns = db(f"select user_text, assistant from {TURNS} where call=%s order by id", (hid,))
    if turns:
        text = "\n".join(f"사용자: {t['user_text']}\n비서: {' '.join(t['assistant'])}" for t in turns)
        db("delete from records where kind='transcript' and meta->>'call' = %s", (hid,))  # an end may repeat
        db("insert into records(kind, session, device, text, meta, embedding) values('transcript',%s,%s,%s,%s,%s)",
           (rows[0]["session"], rows[0]["client"], text,
            Jsonb({"call": hid, "direction": rows[0]["direction"]}), embed(text)))
    return {"ok": True, "turns": len(turns)}


LIST_COLS = f"""h.id, h.session, h.direction, h.reason, h.client,
    extract(epoch from h.started_at)::float8 as started_at, h.duration_s,
    (select user_text from {TURNS} t where t.call = h.id order by t.id limit 1) as title,
    (select left(array_to_string(array(select jsonb_array_elements_text(t.assistant)), ' '), 120) from {TURNS} t
        where t.call = h.id and jsonb_array_length(t.assistant) > 0 order by t.id desc limit 1) as last_reply,
    (select count(*) from {TURNS} t where t.call = h.id) as turns,
    (select coalesce(jsonb_object_agg(route, n), '{{}}') from (select route, count(*) n from {TURNS} t
        where t.call = h.id group by route) r) as routes,
    (select coalesce(jsonb_agg(distinct a->>'kind'), '[]') from {TURNS} t, jsonb_array_elements(t.activity) a
        where t.call = h.id and jsonb_typeof(a) = 'object' and a->>'kind' is not null) as kinds"""
# shortcut: e5 cosine for unrelated Korean text sits around 0.75-0.8, so a vector-only hit needs more than that;
# tune on real calls if q misses paraphrases or returns noise.
HISTORY_VEC_MIN, HISTORY_LEX_MIN = 0.86, 0.1


def snippet(text, q, width=80):
    """The transcript line that best matches q, trimmed around the match."""
    lines = [ln for ln in text.splitlines() if ln.strip()] or [text]
    words = set(q.lower().split())
    best = max(lines, key=lambda ln: (q.lower() in ln.lower(), len(words & set(ln.lower().split()))))
    at = max(best.lower().find(q.lower()), 0)
    start = max(0, min(at - width // 3, len(best) - width))
    return ("…" if start else "") + best[start:start + width] + ("…" if start + width < len(best) else "")


@app.get("/v1/state/history", dependencies=[v1])
def history_list(limit: int = 20, before: float | None = None, q: str | None = None):
    limit = max(1, min(limit, 100))
    where = "(%(before)s::float8 is null or h.started_at < to_timestamp(%(before)s))"
    if not q:
        return db(f"select {LIST_COLS} from {HIST} h where {where} order by h.started_at desc limit %(limit)s",
                  {"before": before, "limit": limit})
    # Full transcripts, ranked like memory search: ended calls have a transcript record (hybrid vector + text);
    # calls still running do not yet, so their turns are matched by substring.
    best: dict[str, tuple[float, str]] = {}
    for h in search(Search(query=q, kinds=["transcript"], limit=100)):
        cid = (h["meta"] or {}).get("call")
        if cid and cid not in best and (h["lex"] >= HISTORY_LEX_MIN or h["vec"] >= HISTORY_VEC_MIN):
            best[cid] = (h["score"], snippet(h["text"], q))
    for r in db(f"select t.call, t.user_text, t.assistant from {TURNS} t join {HIST} h on h.id = t.call"
                " where h.ended_at is null and (t.user_text ilike %(like)s or t.assistant::text ilike %(like)s)",
                {"like": f"%{q}%"}):
        best.setdefault(r["call"], (0.0, snippet(r["user_text"] + "\n" + " ".join(r["assistant"]), q)))
    if not best:
        return []
    rows = db(f"select {LIST_COLS} from {HIST} h where {where} and h.id = any(%(ids)s)",
              {"before": before, "ids": list(best)})
    rows.sort(key=lambda r: -best[r["id"]][0])
    return [{**r, "match": best[r["id"]][1]} for r in rows[:limit]]


def offsets(steps, started):
    """Activity steps as {label, kind, at}: `at` in seconds from the call start (older string steps become generic)."""
    out = []
    for st in steps or []:
        st = st if isinstance(st, dict) else {"label": str(st), "kind": "generic"}
        at = st.get("at")
        out.append({**st, "at": round(at - started, 1) if isinstance(at, (int, float)) and started else None})
    return out


@app.get("/v1/state/history/{hid}", dependencies=[v1])
def history_get(hid: str):
    rows = db(f"select id, session, call_id, direction, reason, client, opening, {EPOCH.format('started_at')},"
              f" {EPOCH.format('ended_at')}, duration_s from {HIST} where id=%s", (hid,))
    if not rows:
        raise HTTPException(404, "no such call")
    started = rows[0]["started_at"]
    turns = db(f"select {EPOCH.format('at')}, user_text as user, assistant, route, activity, timings, interrupted"
               f" from {TURNS} where call=%s order by id", (hid,))
    for t in turns:  # offsets from the call start, measured here rather than trusted from the client
        t["at"] = round(t["at"] - started, 1) if started else None
        t["activity"] = offsets(t["activity"], started)
    return {**rows[0], "turns": turns}


def delete_calls(ids):
    if ids:
        db("delete from records where kind='transcript' and meta->>'call' = any(%s)", (ids,))
        db(f"delete from {HIST} where id = any(%s)", (ids,))  # turns go with them (on delete cascade)
    return {"deleted": len(ids)}


@app.delete("/v1/state/history/{hid}", dependencies=[v1])
def history_delete(hid: str):
    if not db(f"select 1 from {HIST} where id=%s", (hid,)):
        raise HTTPException(404, "no such call")
    return delete_calls([hid])


@app.delete("/v1/state/history", dependencies=[v1])
def history_prune(older_than_days: float = Query(gt=0)):
    rows = db(f"select id from {HIST} where started_at < now() - make_interval(secs => %s)", (older_than_days * 86400,))
    return delete_calls([r["id"] for r in rows])


if __name__ == "__main__":
    import uvicorn
    assert HOST.startswith("100.") or HOST.startswith("127."), "bind a Tailscale or loopback address only"
    uvicorn.run(app, host=HOST, port=PORT)
