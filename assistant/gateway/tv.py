"""GET /v1/tv: one compact, read-only JSON for the Roku dashboard (assistant/roku/).

Built from pieces the gateway already has, each fetched on its own so one failing source leaves only its own part
`null`. Nothing here shows a command, a token or a message body: tasks by goal and status only, approvals as a
count. The answer is cached for TTL_S seconds, so a TV polling every 30 s never costs more than one build per window.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
TTL_S = 30
SECTION_TIMEOUT_S = 8
LINES, WIDTH = 10, 90


def lines(text, limit=LINES):
    """Free text from a Cygnus/Google read → a few short display lines."""
    out = [" ".join(l.split())[:WIDTH] for l in str(text or "").splitlines()]
    return [l for l in out if l][:limit]


async def section(fn):
    """One source: its value, or None when it is missing, slow or failing (never an exception)."""
    try:
        return await asyncio.wait_for(fn(), SECTION_TIMEOUT_S)
    except Exception:  # noqa: BLE001 a dashboard shows what it can
        return None


async def build(d, now=None):
    """`d`: async callables name → data (see gateway.tv_deps). `now`: epoch seconds."""
    now = now or time.time()
    today = datetime.fromtimestamp(now, KST).date().isoformat()

    async def infra():
        if not d["infra_ok"]():
            return None
        h, s, a = await asyncio.gather(*(d["infra"](n) for n in ("infra_health", "infra_status", "infra_alerts")))
        return {"health": lines(h), "problems": lines(s), "alerts": lines(a)}

    async def tasks():
        rows = await d["tasks"](20)
        run = [t for t in rows if t.get("status") == "running"]
        return {"running": [{"goal": str(t.get("goal", ""))[:WIDTH], "status": t["status"]} for t in run[:5]],
                "queued": sum(t.get("status") in ("queued", "waiting_approval") for t in rows),
                "recent": [{"goal": str(t.get("goal", ""))[:WIDTH], "status": t.get("status")}
                           for t in rows if t.get("status") in ("done", "failed")][:3],
                "approvals": len(await d["approvals"]())}

    async def todo():
        if not d["infra_ok"]():
            return None
        t, a = await asyncio.gather(d["infra"]("gistra_today"), d["infra"]("gistra_assignments"))
        return {"today": lines(t), "assignments": lines(a)}

    async def calls():
        out = []
        for item, at in await d["fires"](now):
            if datetime.fromtimestamp(at, KST).date().isoformat() == today:
                out.append((at, str(item.get("purpose") or ("기상" if item.get("kind") == "wake" else "알림"))[:WIDTH]))
        return [{"at": datetime.fromtimestamp(at, KST).strftime("%H:%M"), "what": w} for at, w in sorted(out)[:8]]

    async def calendar():
        if not d["google_ok"]():
            return None
        return lines(await d["calendar"](today))

    async def assistant():
        return {"name": await d["persona"](), "state": "call" if d["in_call"]() else "idle"}

    names = ("infra", "tasks", "todo", "calls", "calendar", "assistant")
    vals = await asyncio.gather(*(section(f) for f in (infra, tasks, todo, calls, calendar, assistant)))
    return {"at": datetime.fromtimestamp(now, KST).isoformat(timespec="seconds"), **dict(zip(names, vals))}


class Cache:
    def __init__(self):
        self.at, self.value, self.lock = 0.0, None, asyncio.Lock()

    async def get(self, make, ttl=TTL_S):
        async with self.lock:
            if self.value is None or time.monotonic() - self.at >= ttl:
                self.value, self.at = await make(), time.monotonic()
            return self.value
