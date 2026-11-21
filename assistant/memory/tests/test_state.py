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
    rows = client.get(f"/v1/state/usage?since={t0 - 60}").json()
    assert [(r["model"], r["cost_usd"], r["device"]) for r in rows] == [("sonnet", 0.25, "macbook")]
    assert abs(rows[0]["ts"] - t0) < 60
    assert len(client.get("/v1/state/usage").json()) == 2


def test_devices_and_push_tokens(client):
    assert client.put("/v1/state/devices/macbook", json={"name": "맥북", "url": "http://x/mcp",
                                                         "harness": ["claude_code"], "gateway": "macbook"}).status_code == 200
    client.put("/v1/state/devices/macbook", json={"name": "맥북", "url": "http://y/mcp"})  # upsert
    [d] = client.get("/v1/state/devices").json()
    assert d["url"] == "http://y/mcp" and d["harness"] == [] and time.time() - d["last_seen"] < 60
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
                ("post", "/v1/records")]


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
        r = getattr(lim, method)(path, **({"json": {}} if method in ("put", "post") else {}))
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
