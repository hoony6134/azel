"""Gateways' shared state API. Fixture: conftest.py (real Postgres, throwaway schemas)."""
import sys
import time

from fastapi.testclient import TestClient


def test_settings_roundtrip(client):
    assert client.get("/v1/state/settings").json() == {}
    doc = {"default_model": "opus", "proactive": {"quiet_hours": ["23:30", "07:30"]}}
    assert client.put("/v1/state/settings", json=doc).json() == doc
    assert client.get("/v1/state/settings").json() == doc


def test_usage(client):
    t0 = time.time()
    client.post("/v1/state/usage", json={"device": "macbook", "model": "sonnet", "path": "claude_code",
                                         "cost_usd": 0.25})
    client.post("/v1/state/usage", json={"model": "opus", "path": "api", "cost_usd": 1, "ts": t0 - 86400 * 400})
    for _ in range(2):  # a replayed row (gateway migration) is stored once however often it is sent
        client.post("/v1/state/usage", json={"model": "opus", "path": "api", "cost_usd": 2, "ts": t0 - 86400 * 400,
                                             "import_key": "migrate:macbook:7"})
    rows = client.get(f"/v1/state/usage?since={t0 - 60}").json()
    assert [(r["model"], r["cost_usd"], r["device"]) for r in rows] == [("sonnet", 0.25, "macbook")]
    assert abs(rows[0]["ts"] - t0) < 60
    assert len(client.get("/v1/state/usage").json()) == 3


def test_devices_and_push_tokens(client):
    assert client.put("/v1/state/devices/macbook", json={"name": "맥북", "url": "http://x/mcp",
                                                         "harness": ["claude_code"], "gateway": "macbook"}).status_code == 200
    client.put("/v1/state/devices/macbook", json={"name": "맥북", "url": "http://y/mcp"})  # upsert
    [d] = client.get("/v1/state/devices").json()
    assert d["url"] == "http://y/mcp" and d["harness"] == [] and time.time() - d["last_seen"] < 60
    assert d["writer"] == "full"
    assert client.put("/v1/state/devices/x", json={"name": "", "url": "u"}).status_code == 422
    client.put("/v1/state/push_tokens/abc", json={"bundle_id": "xyz.scian.biseo", "env": "production"})
    client.put("/v1/state/push_tokens/al1", json={"bundle_id": "xyz.scian.biseo", "kind": "alert"})
    rows = client.get("/v1/state/push_tokens").json()  # newest first, with ts (the gateway evicts the oldest)
    assert [{k: r[k] for k in ("token", "bundle_id", "env", "kind")} for r in rows] == [
        {"token": "al1", "bundle_id": "xyz.scian.biseo", "env": "sandbox", "kind": "alert"},
        {"token": "abc", "bundle_id": "xyz.scian.biseo", "env": "production", "kind": "voip"}]
    assert rows[0]["ts"] >= rows[1]["ts"]
    assert client.delete("/v1/state/push_tokens/abc").json() == {"deleted": 1}
    assert [r["token"] for r in client.get("/v1/state/push_tokens").json()] == ["al1"]
    assert client.put("/v1/state/push_tokens/x", json={"bundle_id": "b", "kind": "sms"}).status_code == 422


def test_calls(client):
    client.post("/v1/state/calls", json={"id": "c1", "reason": "deadline", "opening": "마감이 있어요."})
    r = client.post("/v1/state/calls/c1/answer").json()
    assert r["opening"] == "마감이 있어요." and r["status"] == "answered"
    assert client.post("/v1/state/calls/nope/answer").status_code == 404


def test_approvals(client):
    aid = client.post("/v1/state/approvals", json={"device": "macbook", "tool": "run_command",
                                                   "command": "git push", "cwd": "~/dev/x"}).json()["approval_id"]
    assert [a["id"] for a in client.get("/v1/state/approvals").json()] == [aid]
    assert client.post(f"/v1/state/approvals/{aid}", json={"approve": True}).json()["status"] == "approved"
    assert client.post(f"/v1/state/approvals/{aid}", json={"approve": False}).status_code == 409
    got = client.get(f"/v1/state/approvals/{aid}").json()
    assert got["status"] == "approved" and got["command"] == "git push" and got["decided"]
    assert client.get("/v1/state/approvals").json() == []
    assert client.get("/v1/state/approvals/nope").status_code == 404
    use = {"device": "macbook", "command": "git push", "cwd": "~/dev/x"}
    assert client.post(f"/v1/state/approvals/{aid}/use", json={**use, "device": "albireo"}).status_code == 409
    assert client.post(f"/v1/state/approvals/{aid}/use", json={**use, "command": "git push -f"}).status_code == 409
    assert client.post(f"/v1/state/approvals/{aid}/use", json=use).json() == {"id": aid, "status": "used"}
    assert client.post(f"/v1/state/approvals/{aid}/use", json=use).status_code == 409  # once
    assert client.get(f"/v1/state/approvals/{aid}").json()["status"] == "used"
    old = client.post("/v1/state/approvals", json={**use, "tool": "run_command"}).json()["approval_id"]
    client.post(f"/v1/state/approvals/{old}", json={"approve": True})
    time.sleep(1.2)
    assert client.post(f"/v1/state/approvals/{old}/use", json={**use, "window_s": 1}).status_code == 409  # stale
    pend = client.post("/v1/state/approvals", json={**use, "tool": "run_command"}).json()["approval_id"]
    assert client.post(f"/v1/state/approvals/{pend}/use", json=use).status_code == 409  # never approved


def test_sessions(client):
    assert client.get("/v1/state/sessions/v1").status_code == 404
    client.put("/v1/state/sessions/v1", json={"claude_session": "s-1", "device": "macbook"})
    client.put("/v1/state/sessions/v1", json={"claude_session": "s-2", "device": "albireo"})
    assert client.get("/v1/state/sessions/v1").json() == {"claude_session": "s-2", "device": "albireo"}


def test_leases(client):
    a = client.post("/v1/state/leases/job", json={"holder": "macbook", "ttl_s": 60}).json()
    assert a["held"] and a["holder"] == "macbook"
    b = client.post("/v1/state/leases/job", json={"holder": "albireo", "ttl_s": 60}).json()
    assert not b["held"] and b["holder"] == "macbook"
    assert client.post("/v1/state/leases/job", json={"holder": "macbook", "ttl_s": 60}).json()["held"]  # renew
    assert client.delete("/v1/state/leases/job?holder=albireo").json() == {"released": False}
    assert client.delete("/v1/state/leases/job?holder=macbook").json() == {"released": True}
    assert client.post("/v1/state/leases/job", json={"holder": "albireo", "ttl_s": 0.01}).json()["held"]
    time.sleep(0.05)  # expired: anyone may take it
    assert client.post("/v1/state/leases/job", json={"holder": "gienah", "ttl_s": 60}).json()["held"]
    assert client.post("/v1/state/leases/job", json={"holder": "x", "ttl_s": 0}).status_code == 422
    # priority: a preferred gateway (deneb) takes an unexpired lease from a worse one (mac), never the reverse
    assert client.post("/v1/state/leases/sched", json={"holder": "mac", "ttl_s": 60, "priority": 10}).json()["held"]
    assert not client.post("/v1/state/leases/sched", json={"holder": "gienah", "ttl_s": 60, "priority": 20}).json()["held"]
    took = client.post("/v1/state/leases/sched", json={"holder": "deneb", "ttl_s": 60, "priority": 1}).json()
    assert took["held"] and took["priority"] == 1
    assert not client.post("/v1/state/leases/sched", json={"holder": "mac", "ttl_s": 60, "priority": 10}).json()["held"]


LIMITED_ONLY = [("put", "/v1/state/settings"), ("get", "/v1/state/push_tokens"), ("get", "/v1/state/approvals"),
                ("post", "/v1/state/approvals/x"), ("post", "/v1/state/leases/x"), ("get", "/v1/state/sessions/v1"),
                ("post", "/v1/state/calls"), ("post", "/v1/search"), ("get", "/v1/contacts"), ("get", "/v1/context"),
                ("post", "/v1/records"), ("get", "/v1/state/history"), ("get", "/v1/state/history/h1"),
                ("post", "/v1/state/history/h1/start"), ("post", "/v1/state/history/h1/turns"),
                ("post", "/v1/state/history/h1/end"), ("delete", "/v1/state/history/h1")]


def limited_client(peer, token):
    return TestClient(sys.modules["memory_service"].app, client=(peer, 1), headers={"Authorization": f"Bearer {token}"})


def assert_limited(lim):
    assert lim.get("/v1/state/settings").status_code == 200
    assert lim.post("/v1/state/usage", json={"model": "haiku", "path": "api"}).status_code == 200
    assert lim.put("/v1/state/devices/spark", json={"name": "스파크", "url": "http://s/mcp"}).status_code == 200
    assert lim.get("/v1/state/devices").status_code == 200
    aid = lim.post("/v1/state/approvals", json={"device": "spark", "command": "rm x"}).json()["approval_id"]
    assert lim.get(f"/v1/state/approvals/{aid}").status_code == 200
    for method, path in LIMITED_ONLY:
        r = getattr(lim, method)(path, **({"json": {}} if method in ("put", "post") else {}))  # incl. history
        assert r.status_code == 403, (method, path, r.status_code)


def test_limited_ip_with_shared_token(client):
    assert_limited(limited_client("203.0.113.9", "t0k"))


def test_limited_token_from_a_full_peer(client):
    assert_limited(limited_client("100.64.0.8", "l1m"))


def test_ip_gate_fails_closed(client):
    # Docker's userland proxy rewrites peers to bridge addresses; never let those count as full.
    for peer in ["172.19.0.1", "10.0.0.5", "192.168.1.2", "127.0.0.1", "8.8.8.8", "testclient", ""]:
        assert_limited(limited_client(peer, "t0k"))
    assert limited_client("100.64.0.4", "t0k").get("/v1/state/push_tokens").status_code == 200


def test_limited_writer_cannot_take_over_a_full_device(client):
    client.put("/v1/state/devices/mbp", json={"name": "macbook", "url": "http://100.64.0.8:8893/mcp"})
    for lim in [limited_client("100.64.0.8", "l1m"), limited_client("203.0.113.9", "t0k")]:
        r = lim.put("/v1/state/devices/mbp", json={"name": "macbook", "url": "http://evil/mcp"})
        assert r.status_code == 403
        assert lim.put("/v1/state/devices/spark2", json={"name": "spark", "url": "http://s/mcp"}).status_code == 200
    rows = {d["id"]: d for d in client.get("/v1/state/devices").json()}
    assert rows["mbp"]["url"] == "http://100.64.0.8:8893/mcp" and rows["mbp"]["writer"] == "full"
    assert rows["spark2"]["writer"] == "limited"
    client.put("/v1/state/devices/spark2", json={"name": "spark", "url": "http://s2/mcp"})  # a full writer may claim
    assert {d["id"]: d for d in client.get("/v1/state/devices").json()}["spark2"]["writer"] == "full"


def test_approval_ids_kept_on_migration(client):
    body = {"id": "abcdef012345", "device": "macbook", "command": "git push"}
    assert client.post("/v1/state/approvals", json=body).json() == {"approval_id": "abcdef012345"}
    assert client.post("/v1/state/approvals", json={**body, "command": "rm -rf"}).status_code == 200  # no-op
    assert client.get("/v1/state/approvals/abcdef012345").json()["command"] == "git push"
    assert limited_client("100.64.0.8", "l1m").post("/v1/state/approvals", json={**body, "id": "0" * 12}).status_code == 403
    assert client.post("/v1/state/approvals", json={**body, "id": "nope"}).status_code == 422


def test_call_history_round_trip(client):
    t0 = time.time()
    client.post("/v1/state/history/h1/start", json={"session": "s1", "call_id": "c1", "direction": "incoming",
                                                    "reason": "morning_briefing", "client": "iphone",
                                                    "opening": "좋은 아침이에요.", "started_at": t0})
    client.post("/v1/state/history/h1/turns", json={
        "user": "오늘 일정 알려줘", "assistant": ["10시에 회의가 있어요."], "route": "claude", "at": t0 + 12,
        "activity": [{"label": "캘린더 확인 중", "kind": "calendar", "at": t0 + 13.5},
                     {"label": "메일 확인 중", "kind": "mail", "at": t0 + 15}], "timings": {"total_ms": 4200}})
    client.post("/v1/state/history/h1/turns", json={"user": "고마워", "assistant": ["네, 언제든 불러 주세요."],
                                                    "route": "qwen", "interrupted": True, "at": t0 + 40,
                                                    "activity": ["예전 형식"]})
    assert client.post("/v1/state/history/h1/turns", json={"user": ""}).status_code == 422  # no empty turns
    assert client.post("/v1/state/history/h1/end", json={"ended_at": t0 + 95}).json() == {"ok": True, "turns": 2}
    [row] = client.get("/v1/state/history").json()
    assert row["id"] == "h1" and row["title"] == "오늘 일정 알려줘" and row["turns"] == 2
    assert row["routes"] == {"claude": 1, "qwen": 1} and abs(row["duration_s"] - 95) < 1 and row["direction"] == "incoming"
    assert row["session"] == "s1" and row["last_reply"] == "네, 언제든 불러 주세요." and sorted(row["kinds"]) == ["calendar", "mail"]
    full = client.get("/v1/state/history/h1").json()
    assert [t["user"] for t in full["turns"]] == ["오늘 일정 알려줘", "고마워"]
    assert [t["at"] for t in full["turns"]] == [12.0, 40.0]  # seconds from the call start
    assert full["turns"][0]["activity"] == [{"label": "캘린더 확인 중", "kind": "calendar", "at": 13.5},
                                            {"label": "메일 확인 중", "kind": "mail", "at": 15.0}]
    assert full["turns"][1]["activity"] == [{"label": "예전 형식", "kind": "generic", "at": None}]
    assert full["turns"][1]["interrupted"] is True
    assert full["opening"] == "좋은 아침이에요." and full["client"] == "iphone"
    hits = client.get("/v1/state/history?q=회의").json()  # full transcript, not just the title
    assert [r["id"] for r in hits] == ["h1"] and "회의" in hits[0]["match"]
    assert client.get("/v1/state/history?q=언제든").json()[0]["match"].startswith("비서: 네, 언제든")
    assert client.get("/v1/state/history?q=없는말").json() == []
    assert client.get(f"/v1/state/history?before={t0 - 1}").json() == []
    recs = client.post("/v1/search", json={"query": "오늘 일정 회의", "kinds": ["transcript"]}).json()
    assert [r["meta"]["call"] for r in recs] == ["h1"] and "10시에 회의" in recs[0]["text"]


def test_call_history_search_running_call(client):
    client.post("/v1/state/history/live1/start", json={"session": "s9"})
    client.post("/v1/state/history/live1/turns", json={"user": "주차 요금 알려줘", "assistant": ["시간당 삼천 원이에요."]})
    try:  # the ended call h1 from test_call_history_round_trip exists and must not show up for "주차"
        hits = client.get("/v1/state/history?q=주차").json()  # not ended: no transcript record yet, still found
        assert [h["id"] for h in hits] == ["live1"] and "주차" in hits[0]["match"]
    finally:
        client.delete("/v1/state/history/live1")  # never leak into later tests


def test_call_history_delete_and_prune(client):
    for hid, age in [("old1", 400), ("new1", 1)]:
        client.post(f"/v1/state/history/{hid}/start", json={"started_at": time.time() - age * 86400})
        client.post(f"/v1/state/history/{hid}/turns", json={"user": f"{hid} 안녕", "assistant": ["네"]})
        client.post(f"/v1/state/history/{hid}/end", json={})
    assert client.delete("/v1/state/history?older_than_days=365").json() == {"deleted": 1}
    ids = [r["id"] for r in client.get("/v1/state/history?limit=100").json()]
    assert "old1" not in ids and "new1" in ids
    assert client.delete("/v1/state/history/new1").json() == {"deleted": 1}
    assert client.get("/v1/state/history/new1").status_code == 404
    assert client.delete("/v1/state/history/new1").status_code == 404
    recs = client.post("/v1/search", json={"query": "안녕", "kinds": ["transcript"], "limit": 100}).json()
    assert not {"old1", "new1"} & {r["meta"].get("call") for r in recs}  # their transcripts went too
    assert client.delete("/v1/state/history?older_than_days=0").status_code == 422


def test_state_schema_upgrades_an_older_table(client):
    m = sys.modules["memory_service"]
    m.db(f"alter table {m.STATE}.devices drop column writer; alter table {m.STATE}.usage drop column import_key")
    m.db(m.STATE_SCHEMA)  # what the service runs on start
    cols = {r["column_name"] for r in m.db("select column_name from information_schema.columns where table_schema=%s",
                                           (m.STATE,))}
    assert {"writer", "import_key"} <= cols
    m.db(m.STATE_SCHEMA)  # and again: idempotent


def test_scheduled_calls(client):
    at = time.time() + 3600
    a = client.post("/v1/state/schedule", json={"at": at, "repeat": "weekly:0", "kind": "wake",
                                                "created_by": "voice"}).json()
    b = client.post("/v1/state/schedule", json={"at": at - 600, "kind": "reminder", "note": "약 먹기"}).json()
    assert a["status"] == "pending" and a["attempts"] == 0 and abs(a["at"] - at) < 1
    assert [r["id"] for r in client.get("/v1/state/schedule").json()] == [b["id"], a["id"]]  # soonest first
    for bad in [{"at": at, "repeat": "hourly"}, {"at": at, "repeat": "weekly:mon"}, {"at": at, "repeat": "weekly:7"},
                {"at": at, "kind": "nap"}, {"at": at, "note": "x" * 201}, {"at": at, "created_by": "evil"}]:
        assert client.post("/v1/state/schedule", json=bad).status_code == 422
    f = client.post(f"/v1/state/schedule/{a['id']}", json={"status": "fired", "attempts": 1, "call_id": "c9",
                                                           "last_attempt": time.time(), "expect_status": "pending"})
    assert f.json()["status"] == "fired" and f.json()["call_id"] == "c9"
    again = client.post(f"/v1/state/schedule/{a['id']}", json={"status": "fired", "expect_status": "pending"})
    assert again.status_code == 409  # compare-and-set: a second firing gateway loses
    assert client.post("/v1/state/schedule/nope", json={"status": "fired"}).status_code == 404
    assert client.post(f"/v1/state/schedule/{a['id']}", json={}).status_code == 400
    assert client.delete(f"/v1/state/schedule/{b['id']}").json()["status"] == "cancelled"
    assert client.delete(f"/v1/state/schedule/{b['id']}").status_code == 404
    assert [r["id"] for r in client.get("/v1/state/schedule?status=cancelled").json()] == [b["id"]]
    assert limited_client("100.64.0.8", "l1m").get("/v1/state/schedule").status_code == 403


def test_scheduled_cron_columns(client):
    """Batch 5: cron rules ride on the same table; rows without them read back with the defaults."""
    at = time.time() + 3600
    old = client.post("/v1/state/schedule", json={"at": at, "repeat": "daily"}).json()
    assert old["cron"] is None and old["notify"] == "call" and old["purpose"] == "" and old["skips"] == [] \
        and old["start_date"] is None
    r = client.post("/v1/state/schedule", json={"at": at, "kind": "reminder", "cron": "0 10-18/2 * * 1-5",
                                                "start_date": "2026-12-01", "end_date": "2026-12-31", "notify": "push",
                                                "purpose": "스트레칭", "request": "평일 10시부터 6시까지 2시간마다 스트레칭하라고 알림으로"})
    item = r.json()
    assert item["cron"] == "0 10-18/2 * * 1-5" and item["start_date"] == "2026-12-01" and item["notify"] == "push"
    for bad in [{"at": at, "cron": "rm -rf /"}, {"at": at, "notify": "sms"}, {"at": at, "purpose": "x" * 61},
                {"at": at, "start_date": "내일"}]:
        assert client.post("/v1/state/schedule", json=bad).status_code == 422
    u = client.post(f"/v1/state/schedule/{item['id']}", json={"cron": "0 9 * * 1-5", "end_date": None,
                                                              "skips": [at], "purpose": "물 마시기"}).json()
    assert u["cron"] == "0 9 * * 1-5" and u["end_date"] is None and u["start_date"] == "2026-12-01" \
        and u["skips"] == [at] and u["purpose"] == "물 마시기"
    for sid in (old["id"], item["id"]):
        client.delete(f"/v1/state/schedule/{sid}")


def test_proactive_log_and_kv(client):
    t0 = time.time()
    client.post("/v1/state/proactive_log", json={"call": True, "urgency": 5, "reason": "서버 다운", "reason_hash": "ab",
                                                 "called": True, "call_id": "c1", "signals": {"alerts": ["NodeDown"]}})
    client.post("/v1/state/proactive_log", json={"call": False, "urgency": 2, "reason": "별일 없음",
                                                 "blocked_by": "no_call"})
    rows = client.get(f"/v1/state/proactive_log?since={t0 - 5}").json()
    assert [r["reason"] for r in rows] == ["별일 없음", "서버 다운"]  # newest first
    assert [r["reason"] for r in client.get("/v1/state/proactive_log?called=true").json()] == ["서버 다운"]
    assert rows[1]["signals"] == {"alerts": ["NodeDown"]} and rows[0]["blocked_by"] == "no_call"
    assert client.post("/v1/state/proactive_log", json={"urgency": 9}).status_code == 422
    assert client.get("/v1/state/kv/alerts_recent").status_code == 404
    assert client.put("/v1/state/kv/alerts_recent", json=[{"names": "NodeDown", "at": t0}]).json()[0]["names"] == "NodeDown"
    assert client.get("/v1/state/kv/alerts_recent").json() == [{"names": "NodeDown", "at": t0}]
    assert client.put("/v1/state/kv/settings", json={}).status_code == 400  # settings have their own route
    assert client.get("/v1/state/kv/Bad-Key").status_code == 422
    lim = limited_client("100.64.0.8", "l1m")
    assert lim.get("/v1/state/proactive_log").status_code == 403 and lim.get("/v1/state/kv/alerts_recent").status_code == 403


def test_fix_call_directions_is_idempotent(client):
    m = sys.modules["memory_service"]
    client.post("/v1/state/history/old_in/start", json={"call_id": "c7", "direction": "outgoing"})  # written reversed
    client.post("/v1/state/history/old_out/start", json={"call_id": "", "direction": "incoming"})
    client.post("/v1/state/history/new_in/start", json={"call_id": "c8", "direction": "incoming"})  # already right
    assert m.fix_call_directions() == 2
    assert m.fix_call_directions() == 0  # again: nothing changes
    rows = {r["id"]: r["direction"] for r in client.get("/v1/state/history?limit=100").json()}
    assert rows["old_in"] == "incoming" and rows["old_out"] == "outgoing" and rows["new_in"] == "incoming"
    for hid in ("old_in", "old_out", "new_in"):
        client.delete(f"/v1/state/history/{hid}")


def test_persona_tags(client):
    client.post("/v1/state/history/pA/start", json={"session": "s", "persona_id": "luna"})
    client.post("/v1/state/history/pB/start", json={"session": "s"})  # older voice servers: the primary
    ids = lambda q: [r["id"] for r in client.get(f"/v1/state/history?limit=100{q}").json()]  # noqa: E731
    assert "pA" in ids("&persona=luna") and "pB" not in ids("&persona=luna") and "pB" in ids("&persona=azel")
    assert client.get("/v1/state/history/pA").json()["persona_id"] == "luna"
    assert client.post("/v1/state/history/pC/start", json={"persona_id": "Bad Id!"}).status_code == 422
    at = time.time() + 3600
    a = client.post("/v1/state/schedule", json={"at": at, "persona_id": "luna"}).json()
    b = client.post("/v1/state/schedule", json={"at": at + 60}).json()
    assert a["persona_id"] == "luna" and b["persona_id"] == "azel"
    assert [r["id"] for r in client.get("/v1/state/schedule?persona=luna").json()] == [a["id"]]
    client.post("/v1/state/proactive_log", json={"reason": "x", "persona_id": "azel"})
    assert client.get("/v1/state/proactive_log").json()[0]["persona_id"] == "azel"
    for hid in ("pA", "pB"):
        client.delete(f"/v1/state/history/{hid}")
    client.delete(f"/v1/state/schedule/{a['id']}")
    client.delete(f"/v1/state/schedule/{b['id']}")


def test_delete_persona_data(client):
    for hid in ("dz1", "dz2"):
        client.post(f"/v1/state/history/{hid}/start", json={"persona_id": "doomed"})
        client.post(f"/v1/state/history/{hid}/turns", json={"user": "안녕 둠드", "assistant": ["네"]})
        client.post(f"/v1/state/history/{hid}/end", json={})
    client.post("/v1/state/history/keep1/start", json={"persona_id": "azel"})
    client.post("/v1/records", json={"kind": "note", "text": "둠드의 메모", "meta": {"persona_id": "doomed"}})
    client.post("/v1/records", json={"kind": "note", "text": "아젤의 메모", "meta": {"persona_id": "azel"}})
    client.put("/v1/context/call-followup-dz1", json={"summary": "후속", "open_questions": ["?"]})
    client.put("/v1/context/azel-todos-doomed", json={"summary": "둠드 할 일", "open_questions": []})
    client.put("/v1/context/azel-todos", json={"summary": "아젤 할 일", "open_questions": []})
    r = client.delete("/v1/state/personas/doomed/data").json()
    assert r == {"notes_deleted": 1, "calls": 2, "history_deleted": False}
    rows = {h["id"]: h for h in client.get("/v1/state/history?limit=100&persona=doomed").json()}
    assert set(rows) == {"dz1", "dz2"} and all(h["persona_deleted"] for h in rows.values())  # "삭제된 비서"
    assert client.get("/v1/state/history/keep1").json()["persona_deleted"] is False
    assert client.get("/v1/context/call-followup-dz1").status_code == 404
    assert client.get("/v1/context/azel-todos-doomed").status_code == 404
    assert client.get("/v1/context/azel-todos").json()["summary"] == "아젤 할 일"  # the primary's todos stay
    notes = [h["text"] for h in client.post("/v1/search", json={"query": "메모", "kinds": ["note"], "limit": 50}).json()]
    assert "둠드의 메모" not in notes and "아젤의 메모" in notes
    assert client.delete("/v1/state/personas/doomed/data?history=true").json()["history_deleted"]
    assert client.get("/v1/state/history?limit=100&persona=doomed").json() == []
    assert client.delete("/v1/state/personas/Bad!/data").status_code == 422
    client.delete("/v1/state/history/keep1")


def test_bad_tokens(client):
    for token in ["nope", "", "l1m-", "t0k-"]:
        assert limited_client("100.64.0.8", token).get("/v1/state/settings").status_code == 401


def test_limited_token_must_differ(tmp_path):
    import pytest
    m = sys.modules["memory_service"]
    assert m.load_limited_token(tmp_path, "t0k") is None  # opt-in
    (tmp_path / "state_token_limited").write_text("t0k\n")
    with pytest.raises(AssertionError, match="differ"):
        m.load_limited_token(tmp_path, "t0k")
    (tmp_path / "state_token_limited").write_text("other\n")
    assert m.load_limited_token(tmp_path, "t0k") == "other"
