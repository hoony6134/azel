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
    assert client.get("/v1/state/push_tokens").json() == [
        {"token": "abc", "bundle_id": "xyz.scian.biseo", "env": "production"}]


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
    client.post("/v1/state/history/h1/start", json={"session": "s1", "call_id": "c1", "direction": "outgoing",
                                                    "reason": "morning_briefing", "client": "iphone",
                                                    "opening": "좋은 아침이에요.", "started_at": t0})
    client.post("/v1/state/history/h1/turns", json={"user": "오늘 일정 알려줘", "assistant": ["10시에 회의가 있어요."],
                                                    "route": "claude", "activity": ["캘린더 확인 중"],
                                                    "timings": {"total_ms": 4200}})
    client.post("/v1/state/history/h1/turns", json={"user": "고마워", "assistant": ["네."], "route": "qwen",
                                                    "interrupted": True})
    assert client.post("/v1/state/history/h1/turns", json={"user": ""}).status_code == 422  # no empty turns
    assert client.post("/v1/state/history/h1/end", json={"ended_at": t0 + 95}).json() == {"ok": True, "turns": 2}
    [row] = client.get("/v1/state/history").json()
    assert row["id"] == "h1" and row["title"] == "오늘 일정 알려줘" and row["turns"] == 2
    assert row["routes"] == {"claude": 1, "qwen": 1} and abs(row["duration_s"] - 95) < 1 and row["direction"] == "outgoing"
    full = client.get("/v1/state/history/h1").json()
    assert [t["user"] for t in full["turns"]] == ["오늘 일정 알려줘", "고마워"]
    assert full["turns"][0]["activity"] == ["캘린더 확인 중"] and full["turns"][1]["interrupted"] is True
    assert full["opening"] == "좋은 아침이에요." and full["client"] == "iphone"
    assert [r["id"] for r in client.get("/v1/state/history?q=회의").json()] == ["h1"]
    assert client.get("/v1/state/history?q=없는말").json() == []
    assert client.get(f"/v1/state/history?before={t0 - 1}").json() == []
    recs = client.post("/v1/search", json={"query": "오늘 일정 회의", "kinds": ["transcript"]}).json()
    assert [r["meta"]["call"] for r in recs] == ["h1"] and "10시에 회의" in recs[0]["text"]


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
