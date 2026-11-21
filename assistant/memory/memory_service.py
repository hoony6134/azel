"""박비서 memory: raw records (hybrid search), task context, contacts. Long-term facts stay in Cygnus MCP.

Contract: ../PROTOCOL.md section 5. Runs on ruchba in Docker (compose.yaml), Tailscale IP only.
"""
import hmac
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import numpy as np
import onnxruntime as ort
from fastapi import Depends, FastAPI, HTTPException, Request
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


def auth(request: Request):
    got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(got.encode(), TOKEN.encode()):
        raise HTTPException(401, "bad token")


@asynccontextmanager
async def lifespan(app):
    global pool, embed
    from psycopg.rows import dict_row
    pool = ConnectionPool(DB_URL, min_size=1, max_size=4, open=True, kwargs={"autocommit": True, "row_factory": dict_row})
    db(SCHEMA)
    embed = Embedder()
    yield
    pool.close()


app = FastAPI(title="biseo-memory", lifespan=lifespan)
v1 = Depends(auth)


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


if __name__ == "__main__":
    import uvicorn
    assert HOST.startswith("100.") or HOST.startswith("127."), "bind a Tailscale or loopback address only"
    uvicorn.run(app, host=HOST, port=PORT)
