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
    async def in_call(body, origin="azel:s1"):  # as the voice server's task frame would, from that call
        g.origin_ctx.set(origin)
        return await g.task_create(body, "voice")
    first = c.portal.call(in_call, {"goal": "TASK-APPROVAL 임시 폴더 지워 줘", "notify": "push", "persona": "azel"})
    wait_for(c, first["id"], ("waiting_approval",))
    with pytest.raises(g.HTTPException):  # another call's follow-up never touches this call's task
        c.portal.call(in_call, {"goal": "그 작업 끝나면 전화해줘", "persona": "azel"}, "azel:s2")
    t = c.portal.call(in_call, {"goal": "그 작업 끝나면 나한테 전화해줄래?", "persona": "azel"})
    assert t["id"] == first["id"] and t["notify"] == "call" and t["notify_updated"]
    assert [x["id"] for x in c.get("/v1/tasks").json()] == [first["id"]]  # nothing new


def test_follow_up_without_a_running_task_is_refused(tgw):
    g, c, notes = tgw
    r = c.post("/v1/tasks", json={"goal": "그 작업 끝나면 전화해 줘", "persona": "azel"})
    assert r.status_code == 409 and "어떤 작업 말씀이세요?" in r.text and c.get("/v1/tasks").json() == []


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
            g.announced_waits[m["id"]].set_result({"ok": True})  # and the voice server reports it spoken
    monkeypatch.setattr(g, "link_send", link_send)
    monkeypatch.setitem(g.call_warm_keys, "log1", "azel:s1")
    monkeypatch.setitem(g.call_started, "log1", time.time())
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
    monkeypatch.setitem(g.call_started, "log1", time.time())
    asyncio.run(g.task_notify(finished()))
    assert [n[:2] for n in notes] == [("call", "task")]


def test_busy_with_another_call_rings_after_it_one_at_a_time(tgw, monkeypatch):
    g, c, notes = tgw
    monkeypatch.setattr(g, "STAGGER_S", 0.05)

    async def no_wait(*a):
        pass
    monkeypatch.setattr(g, "place_held_call", lambda kw: g.place_call(**kw))

    async def go():
        g.call_warm_keys["log2"], g.call_started["log2"] = "azel:other", time.time()
        g.pending_finish.clear()
        await g.task_notify(finished())
        await g.task_notify(finished(id="defdefdefdef"))
        await asyncio.sleep(0.05)
        assert not notes and len(g.pending_finish) == 2  # held while the other call goes on
        g.call_warm_keys.pop("log2"), g.call_started.pop("log2")
        g.line_free.set()  # that call ended
        await asyncio.sleep(0.02)
        assert len(notes) == 1  # one rings first, the next after the stagger
        await asyncio.wait_for(g.finish_worker[0], 2)
    asyncio.run(go())
    assert [n[:2] for n in notes] == [("call", "task"), ("call", "task")] and not g.pending_finish


def test_a_held_call_rings_after_three_minutes_even_if_the_line_looks_busy(tgw, monkeypatch):
    g, c, notes = tgw
    monkeypatch.setattr(g, "HOLD_MAX_S", 0.1)
    monkeypatch.setattr(g, "place_held_call", lambda kw: g.place_call(**kw))

    async def go():
        g.call_warm_keys["stale"], g.call_started["stale"] = "azel:other", time.time()  # its end never came
        g.pending_finish.clear()
        await g.task_notify(finished())
        await asyncio.wait_for(g.finish_worker[0], 2)
    asyncio.run(go())
    assert [n[:2] for n in notes] == [("call", "task")]
    g.call_warm_keys.clear(), g.call_started.clear()


def test_stale_calls_expire_and_a_dropped_voice_link_clears_them(tgw, monkeypatch):
    g, c, notes = tgw
    g.call_warm_keys["old"], g.call_started["old"] = "azel:x", time.time() - g.CALL_TTL_S - 1
    g.call_warm_keys["new"], g.call_started["new"] = "azel:y", time.time()
    assert g.live_calls() == {"azel:y"} and "old" not in g.call_warm_keys
    g.call_warm_keys.clear(), g.call_started.clear()


def test_an_announcement_taken_but_never_spoken_rings_instead(tgw, monkeypatch):
    g, c, notes = tgw

    async def link_send(m):
        if m["type"] == "announce":
            g.announce_waits[m["id"]].set_result({"ok": True})
            g.announced_waits[m["id"]].set_result({"ok": False})  # the call ended before it was said
    monkeypatch.setattr(g, "link_send", link_send)
    monkeypatch.setitem(g.call_warm_keys, "log1", "azel:s1")
    monkeypatch.setitem(g.call_started, "log1", time.time())
    asyncio.run(g.task_notify(finished()))
    assert [n[:2] for n in notes] == [("call", "task")]


def test_an_undelivered_finish_is_told_again_after_a_restart(tgw, monkeypatch):
    g, c, notes = tgw
    told = []

    async def notify(t, approval=False):
        told.append(t["id"])
    monkeypatch.setattr(g, "task_notify", notify)
    base = {"goal": "조사", "notify": "call", "owner": g.DEVICE, "finished": time.time() - 60, "result": "끝"}
    g.fake.db["kv"]["task-aaaaaaaaaaaa"] = {**base, "id": "aaaaaaaaaaaa", "status": "done"}
    g.fake.db["kv"]["task-bbbbbbbbbbbb"] = {**base, "id": "bbbbbbbbbbbb", "status": "done", "notified_at": time.time()}
    g.fake.db["kv"]["task-cccccccccccc"] = {**base, "id": "cccccccccccc", "status": "done", "finished": time.time() - 99999}
    g.fake.db["kv"]["tasks-index"] = ["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"]

    async def go():
        await g.fail_orphan_tasks(startup=True)
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert told == ["aaaaaaaaaaaa"]  # never told, recent: again; told, or too old: no


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
    assert "승인" in (r.json().get("note") or "") and r.json()["device"] == "core"  # the pick stands; changes need approval


def test_every_placed_call_is_labeled_for_callkit(tgw, monkeypatch):
    g, c, notes = tgw
    for reason, hint, want in [("morning_briefing", None, "아침 브리핑"), ("deadline", None, "마감 알림"),
                               ("server_down", None, "서버 알림"), ("wake", "아침 7시", "깨우기"),
                               ("task_approval", "정리", "승인 필요"), ("task", "경쟁사 조사", "작업 완료: 경쟁사 조사"),
                               ("reminder", "약 먹기", "알림: 약 먹기"), ("autonomous", "토론지 마감", "토론지 마감")]:
        assert g.call_label(reason, hint) == want, reason
    assert len(g.call_label("task", "아주 긴 창업 아이템 경쟁사 리서치와 정리")) <= 14
    assert g.call_label("task", "**경쟁사** 조사").startswith("작업 완료: 경쟁사")  # no markdown on the lock screen


def test_the_voip_push_always_carries_a_context_label(tmp_path, monkeypatch):
    from test_gateway import TestClient, start
    g = start(tmp_path, monkeypatch, BISEO_DEVICE="macbook")
    pushes = []

    async def voip(payload):
        pushes.append(payload)
        return {"sent": 1}

    async def link_send(m):
        pass
    monkeypatch.setattr(g, "send_voip_push", voip)
    monkeypatch.setattr(g, "link_send", link_send)
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}):
        asyncio.run(g.place_call("morning_briefing", "좋은 아침이에요."))
        asyncio.run(g.place_call("task", "끝났어요.", context={"kind": "task", "id": "abcabcabcabc", "label": "경쟁사 조사"}))
    assert pushes[0]["context"] == {"kind": "morning_briefing", "label": "아침 브리핑"}
    assert pushes[1]["context"] == {"kind": "task", "id": "abcabcabcabc", "label": "작업 완료: 경쟁사 조사"}
