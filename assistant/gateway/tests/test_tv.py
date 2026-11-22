"""GET /v1/tv: the Roku dashboard's read-only JSON and the separate token that opens only it."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tv  # noqa: E402
from test_gateway import gw  # noqa: E402,F401

NOW = 1_800_000_000  # a fixed instant; KST day computed by the module


def deps(**over):
    async def infra(n):
        return {"infra_health": "spark 켜짐\n\nmac 켜짐", "infra_status": "문제 없음", "infra_alerts": "경보 없음",
                "gistra_today": "10시 수업", "gistra_assignments": "과제 2개"}[n]

    async def tasks(n):
        return [{"id": "a", "goal": "보고서 정리", "status": "running", "result": "SECRET-BODY"},
                {"id": "b", "goal": "대기 중", "status": "queued"}, {"id": "c", "goal": "끝난 일", "status": "done"}]

    async def approvals():
        return [{"id": "x", "command": "rm -rf /secret", "status": "pending"}]

    async def fires(now):
        return [({"purpose": "약 먹기", "kind": "reminder"}, now), ({"kind": "wake"}, now - 10 * 86400)]

    async def persona():
        return "아젤"

    async def calendar(today):
        return "14:00 회의"

    d = {"infra": infra, "infra_ok": lambda: True, "tasks": tasks, "approvals": approvals, "fires": fires,
         "persona": persona, "in_call": lambda: False, "google_ok": lambda: True, "calendar": calendar}
    return {**d, **over}


def test_build_is_compact_and_read_only():
    out = asyncio.run(tv.build(deps(), NOW))
    assert out["infra"] == {"health": ["spark 켜짐", "mac 켜짐"], "problems": ["문제 없음"], "alerts": ["경보 없음"]}
    assert out["tasks"]["running"] == [{"goal": "보고서 정리", "status": "running"}] and out["tasks"]["queued"] == 1
    assert out["tasks"]["approvals"] == 1 and out["tasks"]["recent"] == [{"goal": "끝난 일", "status": "done"}]
    assert out["todo"] == {"today": ["10시 수업"], "assignments": ["과제 2개"]}
    assert [c["what"] for c in out["calls"]] == ["약 먹기"]  # only today's
    assert out["calendar"] == ["14:00 회의"] and out["assistant"] == {"name": "아젤", "state": "idle"}
    text = str(out)
    assert "rm -rf" not in text and "SECRET-BODY" not in text  # no command, no result body


def test_one_failing_source_only_blanks_its_own_part():
    async def boom(n):
        raise RuntimeError("down")

    out = asyncio.run(tv.build(deps(infra=boom, in_call=lambda: True, google_ok=lambda: False), NOW))
    assert out["infra"] is None and out["todo"] is None and out["calendar"] is None
    assert out["tasks"]["queued"] == 1 and out["assistant"]["state"] == "call"


def test_cache_holds_for_ttl():
    n = []

    async def make():
        n.append(1)
        return {"n": len(n)}

    c = tv.Cache()
    assert asyncio.run(c.get(make)) == asyncio.run(c.get(make)) == {"n": 1}
    assert asyncio.run(c.get(make, ttl=0)) == {"n": 2}


def test_tv_token_opens_only_the_tv_route(gw):
    g, c, tmp = gw
    path = tmp / "tv_token"
    path.write_text("t" * 40)
    path.chmod(0o600)
    tvh = {"Authorization": "Bearer " + "t" * 40}
    assert c.get("/v1/tv", headers=tvh).status_code == 200
    for method, route in (("get", "/v1/settings"), ("get", "/v1/tasks"), ("get", "/v1/approvals"), ("post", "/v1/tv")):
        assert getattr(c, method)(route, headers=tvh).status_code in (401, 405), route
    path.chmod(0o644)  # readable by others: ignored
    assert c.get("/v1/tv", headers=tvh).status_code == 401
    path.chmod(0o600)
    path.write_text("short")  # too short: ignored
    assert c.get("/v1/tv", headers={"Authorization": "Bearer short"}).status_code == 401
    assert c.get("/v1/tv", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/v1/tv").json().keys() >= {"at", "infra", "tasks", "calls", "assistant"}  # the full token works too


def test_tv_deps_infra_calls_cygnus_with_args(monkeypatch):
    """tv.py asks d["infra"](name); cygnus.call takes (name, args). A one-arg fake once hid the TypeError (empty infra)."""
    import cygnus
    import gateway
    seen = []

    async def fake_call(name, args):
        seen.append((name, args))
        return "ok"

    monkeypatch.setattr(cygnus, "call", fake_call)
    assert asyncio.run(gateway.tv_deps()["infra"]("infra_health")) == "ok" and seen == [("infra_health", {})]
