"""In-memory stand-in for the memory service's state API (PROTOCOL.md section 5, "State").

`down = True` makes every call answer 503, which the gateway treats as ruchba unreachable.
"""
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


def make():
    app = FastAPI()
    app.state.down = False
    db = {"settings": {}, "usage": [], "devices": {}, "push": {}, "calls": {}, "approvals": {}, "sessions": {},
          "leases": {}, "history": {}, "schedule": {}}
    app.db = db

    @app.middleware("http")
    async def outage(request: Request, call_next):
        if app.state.down:
            return JSONResponse({"detail": "down"}, status_code=503)
        return await call_next(request)

    @app.get("/v1/state/settings")
    def get_settings():
        return db["settings"]

    @app.put("/v1/state/settings")
    def put_settings(body: dict):
        db["settings"] = body
        return body

    @app.post("/v1/state/usage")
    def add_usage(body: dict):
        if body.get("model") == "bad":
            raise HTTPException(422, "bad row")
        if body.get("import_key") and any(u.get("import_key") == body["import_key"] for u in db["usage"]):
            return {"ok": True}
        db["usage"].append({"ts": time.time(), **body})
        return {"ok": True}

    @app.get("/v1/state/usage")
    def usage(since: float = 0):
        return [u for u in db["usage"] if u["ts"] >= since]

    @app.put("/v1/state/devices/{did}")
    def put_device(did: str, body: dict, request: Request):
        writer = "limited" if request.headers.get("authorization") == "Bearer l1m" else "full"
        if writer == "limited" and db["devices"].get(did, {}).get("writer") == "full":
            raise HTTPException(403, "device id belongs to a full gateway")
        db["devices"][did] = {"id": did, "harness": [], **body, "last_seen": time.time(), "writer": writer}
        return {"ok": True}

    @app.get("/v1/state/devices")
    def devices():
        return list(db["devices"].values())

    @app.put("/v1/state/push_tokens/{tok}")
    def put_push(tok: str, body: dict):
        db["push"][tok] = {"token": tok, **body}
        return {"ok": True}

    @app.get("/v1/state/push_tokens")
    def push():
        return list(db["push"].values())

    @app.post("/v1/state/calls")
    def add_call(body: dict):
        db["calls"][body["id"]] = {**body, "status": "ringing", "created": time.time()}
        return {"ok": True}

    @app.post("/v1/state/calls/{cid}/answer")
    def answer(cid: str):
        if cid not in db["calls"]:
            raise HTTPException(404)
        db["calls"][cid]["status"] = "answered"
        return db["calls"][cid]

    @app.post("/v1/state/approvals")
    def add_approval(body: dict):
        aid = body.pop("id", None) or uuid.uuid4().hex[:12]
        db["approvals"].setdefault(aid, {"id": aid, **body, "status": "pending", "created": time.time(), "decided": None})
        return {"approval_id": aid}

    @app.get("/v1/state/approvals")
    def approvals(status: str = "pending"):
        return [a for a in db["approvals"].values() if a["status"] == status]

    @app.get("/v1/state/approvals/{aid}")
    def approval(aid: str):
        if aid not in db["approvals"]:
            raise HTTPException(404)
        return db["approvals"][aid]

    @app.post("/v1/state/approvals/{aid}")
    def decide(aid: str, body: dict):
        a = db["approvals"].get(aid)
        if not a or a["status"] != "pending":
            raise HTTPException(409, "not pending")
        a.update(status="approved" if body["approve"] else "rejected", decided=time.time())
        return {"id": aid, "status": a["status"]}

    @app.get("/v1/state/sessions/{vs}")
    def session(vs: str):
        if vs not in db["sessions"]:
            raise HTTPException(404)
        return db["sessions"][vs]

    @app.put("/v1/state/sessions/{vs}")
    def put_session(vs: str, body: dict):
        db["sessions"][vs] = body
        return {"ok": True}

    @app.post("/v1/state/leases/{name}")
    def lease(name: str, body: dict):
        cur, prio = db["leases"].get(name), body.get("priority", 100)
        if cur and cur["expires"] > time.time() and cur["holder"] != body["holder"] and prio >= cur["priority"]:
            return {"held": False, **cur}
        db["leases"][name] = {"holder": body["holder"], "expires": time.time() + body["ttl_s"], "priority": prio}
        return {"held": True, **db["leases"][name]}

    @app.post("/v1/state/schedule")
    def s_add(body: dict):
        sid = uuid.uuid4().hex[:12]
        db["schedule"][sid] = {"id": sid, **body, "status": "pending", "attempts": 0, "call_id": None,
                               "last_attempt": None}
        return db["schedule"][sid]

    @app.get("/v1/state/schedule")
    def s_list(status: str = "pending,fired"):
        return sorted((i for i in db["schedule"].values() if i["status"] in status.split(",")), key=lambda i: i["at"])

    @app.post("/v1/state/schedule/{sid}")
    def s_update(sid: str, body: dict):
        item = db["schedule"].get(sid)
        if not item:
            raise HTTPException(404)
        expect = body.pop("expect_status", None)
        if expect and item["status"] != expect:
            raise HTTPException(409)
        item.update({k: v for k, v in body.items() if v is not None})
        return item

    @app.delete("/v1/state/schedule/{sid}")
    def s_cancel(sid: str):
        item = db["schedule"].get(sid)
        if not item or item["status"] not in ("pending", "fired"):
            raise HTTPException(404)
        item["status"] = "cancelled"
        return item

    @app.post("/v1/state/history/{hid}/start")
    def h_start(hid: str, body: dict):
        db["history"].setdefault(hid, {"id": hid, "turns": []}).update(body)
        return {"ok": True}

    @app.post("/v1/state/history/{hid}/turns")
    def h_turn(hid: str, body: dict):
        if not body.get("user"):
            raise HTTPException(422, "empty turn")
        db["history"].setdefault(hid, {"id": hid, "turns": []})["turns"].append(body)
        return {"ok": True}

    @app.post("/v1/state/history/{hid}/end")
    def h_end(hid: str, body: dict):
        if hid not in db["history"]:
            raise HTTPException(404)
        db["history"][hid].update(body, ended=True)
        return {"ok": True, "turns": len(db["history"][hid]["turns"])}

    @app.get("/v1/state/history")
    def h_list(limit: int = 20, q: str = ""):
        rows = [{"id": h["id"], "title": h["turns"][0]["user"] if h["turns"] else None, "turns": len(h["turns"])}
                for h in db["history"].values() if not q or any(q in t["user"] for t in h["turns"])]
        return rows[:limit]

    @app.get("/v1/state/history/{hid}")
    def h_get(hid: str):
        if hid not in db["history"]:
            raise HTTPException(404)
        return db["history"][hid]

    @app.delete("/v1/state/history/{hid}")
    def h_delete(hid: str):
        if db["history"].pop(hid, None) is None:
            raise HTTPException(404)
        return {"deleted": 1}

    @app.delete("/v1/state/history")
    def h_prune(older_than_days: float):
        app.pruned = older_than_days
        return {"deleted": 0}

    return app
