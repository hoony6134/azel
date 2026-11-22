"""Tasks by voice: a notify follow-up updates the running task, the result is told in the call the user is on (or
after the other call ends), the finish opening is a short briefing carrying the full result, tasks never ring."""
import asyncio
import json
import time

import pytest

from test_tasks import tgw, wait_for  # noqa: F401


def test_notify_request_tells_follow_ups_from_real_work(tgw):
    g, c, notes = tgw
    for said in ("그 작업 끝나면 나한테 전화해줄래?", "끝나면 전화 걸어줘", "그거 다 되면 연락 줘", "작업 끝나면 알림 줘"):
        assert g.notify_request(said) in ("call", "push"), said
    assert g.notify_request("작업 끝나면 알림 줘") == "push" and g.notify_request("끝나면 전화해 줘") == "call"
    for said in ("코어에서 창업 아이템 리서치 좀 진행해주고 그 작업 끝나면 나한테 전화 걸어줘", "다운로드 폴더 정리해 줘"):
        assert g.notify_request(said) is None, said


def test_follow_up_changes_the_running_task_instead_of_creating_one(tgw):
    g, c, notes = tgw
    g.fake.db["approvals"]["ap1"] = {"id": "ap1", "device": "macbook", "status": "pending"}
    first = c.post("/v1/tasks", json={"goal": "TASK-APPROVAL 임시 폴더 지워 줘", "notify": "push", "persona": "azel"}).json()
    wait_for(c, first["id"], ("waiting_approval",))
    r = c.post("/v1/tasks", json={"goal": "그 작업 끝나면 나한테 전화해줄래?", "persona": "azel"})
    assert r.status_code in (200, 201) and r.json()["id"] == first["id"] and r.json()["notify"] == "call", r.text
    assert [t["id"] for t in c.get("/v1/tasks").json()] == [first["id"]]  # nothing new


def test_follow_up_without_a_running_task_is_refused(tgw):
    g, c, notes = tgw
    r = c.post("/v1/tasks", json={"goal": "그 작업 끝나면 전화해 줘", "persona": "azel"})
    assert r.status_code == 409 and "진행 중인 작업이 없어요" in r.text and c.get("/v1/tasks").json() == []


def finished(**kw):
    return {"id": "abcabcabcabc", "goal": "창업 아이템 경쟁사 조사해 줘", "status": "done", "notify": "call",
            "persona": "azel", "result": "**경쟁사**\n- 한화생명 돌봄 서비스\n- 다른 두 곳", "error": None,
            "origin_session": "azel:s1", **kw}


def test_a_result_is_told_in_the_call_the_user_is_on(tgw, monkeypatch):
    g, c, notes = tgw
    sent = []

    async def link_send(m):
        sent.append(m)
        if m["type"] == "announce":
            g.announce_waits[m["id"]].set_result({"ok": True})
    monkeypatch.setattr(g, "link_send", link_send)
    monkeypatch.setitem(g.call_warm_keys, "log1", "azel:s1")
    asyncio.run(g.task_notify(finished()))
    ann = [m for m in sent if m["type"] == "announce"]
    assert ann and ann[0]["session"] == "azel:s1" and "한화생명" in ann[0]["detail"] and not notes  # no new call


def test_a_call_that_does_not_take_it_falls_back_to_ringing(tgw, monkeypatch):
    g, c, notes = tgw

    async def link_send(m):
        if m["type"] == "announce":
            g.announce_waits[m["id"]].set_result({"ok": False})
    monkeypatch.setattr(g, "link_send", link_send)
    monkeypatch.setitem(g.call_warm_keys, "log1", "azel:s1")
    asyncio.run(g.task_notify(finished()))
    assert [n[:2] for n in notes] == [("call", "task")]


def test_busy_with_another_call_rings_after_it(tgw, monkeypatch):
    g, c, notes = tgw
    monkeypatch.setitem(g.call_warm_keys, "log2", "azel:other")
    g.pending_finish.clear()
    asyncio.run(g.task_notify(finished()))
    assert not notes and len(g.pending_finish) == 1 and g.pending_finish[0]["origin_session"] == "azel:s1"
    kw = g.pending_finish.pop()
    asyncio.run(g.place_call(**kw))
    assert [n[:2] for n in notes] == [("call", "task")]


def test_finish_opening_is_a_short_briefing_with_the_title_once(tgw, monkeypatch):
    g, c, notes = tgw

    async def llm(system, user, max_tokens=300):
        assert "한화생명" in user and "목록" in system
        return {"say": "정훈님, 맡기신 창업 아이템 경쟁사 조사 끝났어요. 한화생명 돌봄 서비스가 가장 가까워요. 더 궁금한 거 있으면 물어보세요."}
    monkeypatch.setattr(g, "llm_json", llm)
    say = asyncio.run(g.task_briefing(finished(), "정훈님"))
    assert say.startswith("정훈님, 맡기신") and say.count("정훈님") == 1 and "**" not in say and "-" not in say

    async def down(system, user, max_tokens=300):
        raise RuntimeError("qwen down")
    monkeypatch.setattr(g, "llm_json", down)
    say = asyncio.run(g.task_briefing(finished(), "정훈님"))
    assert "맡기신" in say and "**" not in say and say.endswith("물어보세요.") and len(say) < 400


def test_the_finish_call_carries_the_full_result_for_follow_ups(tgw, monkeypatch):
    g, c, notes = tgw
    got = []

    async def place_call(**kw):
        got.append(kw)
        return {"call_id": "c1", "sent": 1}
    monkeypatch.setattr(g, "place_call", place_call)
    asyncio.run(g.task_notify(finished()))
    assert got[0]["origin_session"] == "azel:s1" and "한화생명" in got[0]["detail"] and got[0]["detail"].startswith("목표:")


def test_tasks_never_ring_schedule_or_notify(tgw):
    g, c, notes = tgw
    import contextvars
    ctx = contextvars.copy_context()
    ctx.run(g.requester_ctx.set, "task:abcabcabcabc")
    for name, args in [("call_user", {"reason": "다 됐어요"}), ("schedule_create", {"reason": "x", "when_text": "3시"})]:
        r = ctx.run(asyncio.run, g.bridge.run(name, args, caller="api"))
        assert not r["ok"] and "작업 중에는" in r["spoken"], (name, r)
    _, _, rs, _ = g.run_args({"connectors": {}}, [], "task:abcabcabcabc")
    deny = json.loads(rs)["permissions"]["deny"]
    assert {"mcp__azel__call_user", "mcp__azel__schedule_create", "mcp__claude_ai_Cygnus_MCP__notify_send"} <= set(deny)


def test_a_limited_device_the_user_picked_says_what_it_cannot_do(tgw, monkeypatch):
    g, c, notes = tgw

    async def devices():
        return [{"id": "core", "url": "http://100.64.0.9:8765/mcp", "limited": True},
                {"id": "macbook", "url": "http://100.64.0.8:8765/mcp"}]
    monkeypatch.setattr(g, "fresh_devices", devices)
    r = c.post("/v1/tasks", json={"goal": "코어에서 창업 아이템 리서치 해 줘", "device": "core", "notify": "none"})
    assert r.status_code == 201, r.text
    assert "읽기·진단만" in (r.json().get("note") or "") and r.json()["device"] == "core"  # the pick stands
