"""The local Qwen's read-only view of Cygnus (the user's own MCP server, https://mcp.scian.xyz), so schedule,
todo and server questions no longer need a Claude run.

The brain is an MCP client with a personal access token (PAT) kept 0600 in ~/.config/biseo/cygnus_pat on the full
host that runs the brain. Create it read-only (`cygnus mcp pat create azel-brain '*' 90 --ro`): Cygnus then refuses
every write tool server-side, and without `--sens` sensitive memories stay hidden. On top of that this module only
ever calls the curated tools below, with arguments it validates itself (Qwen's arguments are steered by what the
user says), and trims what comes back before it reaches Qwen's prompt.
"""
import json
import re
import time
from datetime import date as Date

import httpx

DAY = r"\d{4}-\d{2}-\d{2}"
KINDS = {"class", "exam", "assignment", "todo", "meeting", "event", "fixed", "academic"}
MAX_RESULT = 1500
CACHE_S = 60  # the same question twice in a call (or a follow-up) is answered from here


def _day(v):
    if not (isinstance(v, str) and re.fullmatch(DAY, v)):
        raise ValueError("날짜는 YYYY-MM-DD")
    Date.fromisoformat(v)
    return v


def _text(limit):
    def check(v):
        if not isinstance(v, str) or not v.strip() or len(v) > limit or any(ord(c) < 32 for c in v):
            raise ValueError(f"{limit}자 안쪽 글")
        return v.strip()
    return check


def _kinds(v):
    if not (isinstance(v, list) and v and len(v) <= 8 and all(k in KINDS for k in v)):
        raise ValueError("kinds: " + "·".join(sorted(KINDS)))
    return v


MEM_KINDS = ("profile", "preference", "fact", "project", "decision", "person", "episode")


def _memkind(v):
    if v not in MEM_KINDS:
        raise ValueError("kind: " + "·".join(MEM_KINDS))
    return v


# Qwen-facing tool -> (short Korean description for the prompt, {arg: check}, required args, fixed extra args)
TOOLS = {
    "gistra_today": ("오늘(또는 date) 수업·시험, 7일 안 마감, 복습할 단원", {"date": _day}, (), {}),
    "gistra_schedule": ("from~to(최대 31일) 날짜별 통합 일정: 수업·시험·과제·할 일·회의·학사", {"from": _day, "to": _day, "kinds": _kinds},
                        ("from", "to"), {}),
    "gistra_assignments": ("남은 과제와 내 할 일, course로 과목 좁히기", {"course": _text(40)}, (), {"scope": "upcoming", "limit": 15}),
    "planit_get_day": ("Planit 하루 할 일(기본 오늘)", {"date": _day}, (), {}),
    "planit_search_tasks": ("Planit 할 일 검색(query)", {"query": _text(60)}, ("query",), {"limit": 10}),
    "memory_recall": ("사용자의 장기 기억 검색(취향, 사람, 프로젝트, 결정)", {"query": _text(120), "kind": _memkind},
                      ("query",), {"limit": 5}),
    "gistra_people": ("교수·조교 목록과 맡은 과목, 연락처·사무실·면담 시간(query로 이름·과목 필터)", {"query": _text(60)}, (), {}),
    "infra_health": ("서버별 상태: 켜짐, CPU, 메모리, 디스크, 온도", {}, (), {}),
    "infra_status": ("서비스 모니터 중 문제 있는 것", {}, (), {"problems_only": True}),
    "infra_alerts": ("지금 울리고 있는 서버 경보", {}, (), {}),
}
SHARED_ONLY = {"memory_recall"}
# JSON schemas of the arguments, for models that call these tools natively (the Claude API path); check_args still
# validates every call.
ARG_SCHEMAS = {"date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "description": "YYYY-MM-DD"},
               "from": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
               "to": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
               "kinds": {"type": "array", "items": {"type": "string", "enum": sorted(KINDS)}},
               "course": {"type": "string", "maxLength": 40}, "query": {"type": "string", "maxLength": 120},
               "kind": {"type": "string", "enum": list(MEM_KINDS)}}


def tool_defs(names):
    """Messages-API tool definitions for these Cygnus tools."""
    return [{"name": n, "description": TOOLS[n][0],
             "input_schema": {"type": "object", "properties": {a: ARG_SCHEMAS[a] for a in TOOLS[n][1]},
                              "required": list(TOOLS[n][2]), "additionalProperties": False}} for n in names]  # the user's long-term memory: only for personas that share memory


def check_args(name, args):
    """The arguments Cygnus gets: only known ones, each validated; ValueError with a short Korean reason."""
    desc, spec, required, fixed = TOOLS[name]
    args = args if isinstance(args, dict) else {}
    unknown = set(args) - set(spec)
    if unknown:
        raise ValueError(f"모르는 인자: {', '.join(sorted(unknown))[:60]}")
    out = {k: spec[k](v) for k, v in args.items() if v not in (None, "")}
    missing = [k for k in required if k not in out]
    if missing:
        raise ValueError(f"필요한 인자: {', '.join(missing)}")
    if name == "gistra_schedule":
        span = (Date.fromisoformat(out["to"]) - Date.fromisoformat(out["from"])).days
        if not 0 <= span <= 31:
            raise ValueError("기간은 31일 안쪽")
    return {**out, **fixed}


# Raw state words from Alertmanager, Kuma and Prometheus that a voice answer must not read out as-is. The model
# gets them already in Korean (the prompt also asks it not to read English states or keys aloud).
SPOKEN = [(re.compile(r"\bno firing alerts\.?", re.I), "지금 울리는 경보 없음."),
          (re.compile(r"\bfiring\b", re.I), "경보 울리는 중"), (re.compile(r"\bresolved\b", re.I), "해결됨"),
          (re.compile(r"\bpending\b", re.I), "대기 중"), (re.compile(r"\binactive\b", re.I), "비활성"),
          (re.compile(r"\bDOWN\b"), "멈춤"), (re.compile(r"\bUP\b"), "정상"), (re.compile(r"\bdegraded\b", re.I), "느려짐"),
          (re.compile(r"\bseverity\s*[:=]\s*critical\b", re.I), "심각도 높음"),
          (re.compile(r"\bseverity\s*[:=]\s*warning\b", re.I), "심각도 주의"),
          (re.compile(r"\b(critical)\b", re.I), "심각"), (re.compile(r"\bwarning\b", re.I), "주의"),
          (re.compile(r"✓"), "정상"), (re.compile(r"✗"), "문제"), (re.compile(r"⚠"), "주의")]


def speakable(text):
    """Tool output with raw state words and marks turned into Korean (names, numbers and units stay)."""
    for pat, ko in SPOKEN:
        text = pat.sub(ko, text)
    return text


def trim(text, limit=MAX_RESULT):
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n(이하 생략)"


class Cygnus:
    """Streamable-HTTP MCP client (Cygnus is stateless: one JSON-RPC POST per call)."""

    def __init__(self, url, pat, timeout=8.0):
        self.url, self.cache = url.rstrip("/") + "/", {}
        self.c = httpx.AsyncClient(timeout=timeout, headers={
            "Authorization": f"Bearer {pat}", "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json"})

    async def call(self, name, args):
        key = (name, json.dumps(args, sort_keys=True))
        hit = self.cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        r = await self.c.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": name, "arguments": args}})
        if r.status_code in (401, 403):
            raise PermissionError("Cygnus 토큰이 없거나 만료됐어요.")
        r.raise_for_status()
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            msgs = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
            msg = next(m for m in msgs if m.get("id") == 1)
        else:
            msg = r.json()
        if "error" in msg:
            raise RuntimeError(str(msg["error"].get("message", "MCP error"))[:120])
        res = msg["result"]
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text").strip()
        out = trim(speakable(text) if text else "(결과 없음)")
        if res.get("isError"):
            return f"Cygnus가 거절했어요: {out[:200]}"
        if len(self.cache) > 200:
            self.cache.clear()
        self.cache[key] = (time.monotonic(), out)
        return out


def tool_lines(names):
    """The tool list Qwen sees in the brain's context block."""
    return "\n".join(f"- {n}: {TOOLS[n][0]}" + (f" (인자: {', '.join(TOOLS[n][1])})" if TOOLS[n][1] else "")
                     for n in names)


# ---------- importable bridge for other model paths (gateway: Claude API, NIM) ----------
# The same allowlist, validation and trimming as the brain's tools; the PAT never leaves this host.
_shared_client: "Cygnus | None" = None


def _pat_file(name="cygnus_pat"):
    import os
    from pathlib import Path
    return Path(os.environ.get("BISEO_HOME", Path.home() / ".config/biseo")) / name


def available():
    """True when this host has the read-only PAT (0600; a file others can read counts as missing)."""
    p = _pat_file()
    return p.is_file() and p.stat().st_mode & 0o077 == 0 and p.stat().st_size > 0


def tools(shared=True):
    """Messages-API tool definitions ([] without a PAT). shared=False leaves out memory_recall, for a persona that
    keeps its memory apart."""
    return tool_defs([n for n in TOOLS if shared or n not in SHARED_ONLY]) if available() else []


async def call(name, args):
    """Run one Cygnus tool for a model; always a short string, never an exception."""
    global _shared_client
    if name not in TOOLS:
        return f"모르는 도구예요: {str(name)[:40]}"
    if not available():
        return "이 기기에는 Cygnus 토큰이 없어요."
    try:
        checked = check_args(name, args)
    except ValueError as e:
        return f"도구 인자가 틀렸어요: {e}"
    if _shared_client is None:
        import os
        _shared_client = Cygnus(os.environ.get("BRAIN_CYGNUS_URL", "https://mcp.scian.xyz"),
                                _pat_file().read_text().strip())
    try:
        return await _shared_client.call(name, checked)
    except (PermissionError, RuntimeError, httpx.HTTPError) as e:
        return f"지금은 Cygnus를 볼 수 없어요. ({type(e).__name__})"


# ---------- memory writes: a separate memory-scoped PAT (cygnus_pat_rw), on the Mac gateway only ----------
# Only these two tools, only these shapes; the read path above never sees this token. Nothing is cached.
_write_client: "Cygnus | None" = None
SECRET = re.compile(r"비밀번호|패스워드|password|passwd|\bpin\b|카드 ?번호|카드번호|cvc|cvv|계좌 ?번호|주민(등록)? ?번호|"
                    r"api ?key|토큰|token|secret|sk-[A-Za-z0-9]|\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b|"
                    r"\b\d{6}-?[1-4]\d{6}\b", re.I)


def can_write():
    p = _pat_file("cygnus_pat_rw")
    return p.is_file() and p.stat().st_mode & 0o077 == 0 and p.stat().st_size > 0


async def write(name, args):
    """memory_save {text, kind} or memory_edit {id, op: archive|supersede, text?} with the memory-scoped PAT.
    Returns Cygnus's text (it names the new id); raises on refusal so the caller can say so."""
    global _write_client
    if name == "memory_save":
        text, kind = str(args.get("text") or "").strip(), args.get("kind", "fact")
        if not 0 < len(text) <= 700 or kind not in MEM_KINDS or SECRET.search(text):
            raise ValueError("memory_save: 700자 이하의 비밀 아닌 사실 하나")
        body = {"items": [{"text": text, "kind": kind, "tags": ["azel"]}]}
    elif name == "memory_edit":
        mid, op = str(args.get("id") or ""), args.get("op")
        if not re.fullmatch(r"[\w-]{1,40}", mid) or op not in ("archive", "supersede"):
            raise ValueError("memory_edit: id와 archive|supersede")
        body = {"id": mid, "op": op, **({"text": str(args["text"])[:700]} if op == "supersede" else {})}
    else:
        raise ValueError(f"쓰기 도구가 아니에요: {str(name)[:40]}")
    if not can_write():
        raise PermissionError("Cygnus 쓰기 토큰이 없어요.")
    if _write_client is None:
        import os
        # the write PAT is audience-bound to the memory endpoint (resource …/memory); the read PAT is "*"
        _write_client = Cygnus(os.environ.get("CYGNUS_MEMORY_URL", "https://mcp.scian.xyz/memory"),
                               _pat_file("cygnus_pat_rw").read_text().strip())
    _write_client.cache.clear()
    out = await _write_client.call(name, body)
    _write_client.cache.clear()
    if out.startswith("Cygnus가 거절했어요"):
        raise RuntimeError(out[:200])
    return out
