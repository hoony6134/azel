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
    now = time.time()
    g.fake.db["kv"][f"gateway-start-{g.DEVICE}"] = {"started": now - 600, "since": now - 3600}  # the previous run
    base = {"goal": "조사", "notify": "call", "owner": g.DEVICE, "finished": now - 60, "result": "끝"}
    g.fake.db["kv"]["task-aaaaaaaaaaaa"] = {**base, "id": "aaaaaaaaaaaa", "status": "done"}
    g.fake.db["kv"]["task-bbbbbbbbbbbb"] = {**base, "id": "bbbbbbbbbbbb", "status": "done", "notified_at": now}
    g.fake.db["kv"]["task-cccccccccccc"] = {**base, "id": "cccccccccccc", "status": "done", "finished": now - 1800}
    g.fake.db["kv"]["tasks-index"] = ["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"]

    async def go():
        await g.fail_orphan_tasks(startup=True)
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert told == ["aaaaaaaaaaaa"]  # finished during the previous run, never told: again
    assert g.fake.db["kv"]["task-cccccccccccc"]["notified_at"]  # finished before that run started: backfilled, silent


def test_the_first_start_with_notified_at_retells_nothing_and_backfills(tgw, monkeypatch):
    g, c, notes = tgw
    told = []

    async def notify(t, approval=False):
        told.append(t["id"])
    monkeypatch.setattr(g, "task_notify", notify)
    g.fake.db["kv"].pop(f"gateway-start-{g.DEVICE}", None)
    for tid in ("dddddddddddd", "eeeeeeeeeeee"):  # finished an hour ago, before this code: the live case
        g.fake.db["kv"][f"task-{tid}"] = {"id": tid, "goal": "조사", "notify": "call", "owner": g.DEVICE,
                                          "status": "done", "finished": time.time() - 3600, "result": "끝"}
    g.fake.db["kv"]["tasks-index"] = ["dddddddddddd", "eeeeeeeeeeee"]

    async def go():
        await g.fail_orphan_tasks(startup=True)
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert told == [] and all(g.fake.db["kv"][f"task-{t}"].get("notified_at") for t in ("dddddddddddd", "eeeeeeeeeeee"))
    assert g.fake.db["kv"][f"gateway-start-{g.DEVICE}"]["since"] > time.time() - 5


def test_several_undelivered_finishes_become_one_call(tgw, monkeypatch):
    g, c, notes = tgw
    now = time.time()
    g.fake.db["kv"][f"gateway-start-{g.DEVICE}"] = {"started": now - 600, "since": now - 3600}
    ids = ["111111111111", "222222222222", "333333333333"]
    for i, tid in enumerate(ids):
        g.fake.db["kv"][f"task-{tid}"] = {"id": tid, "goal": f"조사 {i}", "notify": "call", "owner": g.DEVICE,
                                          "status": "done", "finished": now - 60, "result": "끝"}
    g.fake.db["kv"]["tasks-index"] = ids

    async def go():
        await g.fail_orphan_tasks(startup=True)
        await asyncio.sleep(0.1)
    asyncio.run(go())
    assert len(notes) == 1 and notes[0][:2] == ("call", "task") and "완료된 작업이 3개" in notes[0][2], notes
    assert all(g.fake.db["kv"][f"task-{t}"].get("notified_at") for t in ids)


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


def test_a_delegated_job_keeps_the_task_running_until_it_ends(tgw, monkeypatch):
    g, c, notes = tgw
    polls, seen = [], []

    async def round_(t, prompt, resume, deadline):  # the run hands the work to the Mac and stops
        g.note_job(t, "macbook", '{"status": "ok", "job_id": "abcdefabcdef"}')
        t["result"] = "맥북에서 시작했어요. 지금 진행 중이에요. 저는 전화를 직접 걸 수 없어요."
        return None, None

    async def devices():
        return [{"id": "macbook", "url": "http://100.64.0.8:8765/mcp"}]

    async def tools(devs):
        return [], {"macbook__job_status": ("http://100.64.0.8:8765/mcp", "job_status")}

    async def run_tool(block, where):
        polls.append(block["input"]["job_id"])
        seen.append((g.fake.db["kv"][f"task-{tid[0]}"]["status"], list(notes)))
        done = len(polls) >= 3
        body = {"status": "ok", "job_id": "abcdefabcdef", "job_status": "done" if done else "running",
                "result": "연구를 마쳤어요. ~/dev/sandbox/dashboard-research.md 에 세 가지 방법을 정리했어요." if done else None}
        return {"content": __import__("json").dumps(body, ensure_ascii=False), "is_error": False}
    monkeypatch.setattr(g, "task_round", round_)
    monkeypatch.setattr(g, "fresh_devices", devices)
    monkeypatch.setattr(g, "device_tools", tools)
    monkeypatch.setattr(g, "run_tool", run_tool)
    monkeypatch.setattr(g, "JOB_POLL_S", 0.05)
    tid = [None]
    tid[0] = c.post("/v1/tasks", json={"goal": "대시보드 구현 방법 연구 맥북에서 해 줘", "notify": "call", "persona": "azel"}).json()["id"]
    t = wait_for(c, tid[0], ("done", "failed"))
    assert polls == ["abcdefabcdef"] * 3 and all(st == "running" and not n for st, n in seen)  # running, no call yet
    assert t["status"] == "done" and "dashboard-research.md" in t["result"] and "전화" not in t["result"]
    end = time.time() + 3
    while not notes and time.time() < end:
        time.sleep(0.02)
    assert len(notes) == 1 and notes[0][:2] == ("call", "task")  # notified only after the job ended
    assert any(s["kind"] == "job" for s in t["steps"]) and any(s["kind"] == "job_done" for s in t["steps"])


def test_a_delegated_job_blocked_by_permissions_fails_the_task_with_what_is_missing(tgw, monkeypatch):
    """Live 2026-11-23, task d77336c7ed5f: the job was refused WebSearch, WebFetch and python3, and the task read done."""
    g, c, notes = tgw
    replies = [{"job_status": "failed", "needs_input": True, "blocked": ["Bash", "WebFetch", "WebSearch"],
                "result": "Allow WebSearch/WebFetch/python3 so I can finish the research."},
               {"job_status": "done", "result": "I couldn't search: permission to use WebSearch was denied."}]  # old daemon

    async def round_(t, prompt, resume, deadline):
        g.note_job(t, "macbook", '{"status": "ok", "job_id": "60ac064254d1"}')
        return None, None

    async def devices():
        return [{"id": "macbook", "url": "http://100.64.0.8:8765/mcp"}]

    async def tools(devs):
        return [], {"macbook__job_status": ("http://100.64.0.8:8765/mcp", "job_status")}

    async def run_tool(block, where):
        return {"content": __import__("json").dumps({"status": "ok", "job_id": "60ac064254d1", **replies[0]}), "is_error": False}
    monkeypatch.setattr(g, "task_round", round_)
    monkeypatch.setattr(g, "fresh_devices", devices)
    monkeypatch.setattr(g, "device_tools", tools)
    monkeypatch.setattr(g, "run_tool", run_tool)
    monkeypatch.setattr(g, "JOB_POLL_S", 0.05)
    tid = c.post("/v1/tasks", json={"goal": "실버텔러 시장 조사해서 PDF로 맥북에 저장하고 전화해 줘", "notify": "call"}).json()["id"]
    t = wait_for(c, tid, ("done", "failed"))
    assert t["status"] == "failed" and "권한 때문에 막혀서 끝내지 못했어요(명령 실행, 웹 페이지 읽기, 웹 검색)" in t["error"], t
    assert any(s["kind"] == "job_blocked" for s in t["steps"])
    replies.pop(0)
    t = wait_for(c, c.post("/v1/tasks", json={"goal": "다시 조사해 줘", "notify": "none"}).json()["id"], ("done", "failed"))
    assert t["status"] == "failed" and "권한 때문에 막혀서" in t["error"]  # a daemon that still says done


def test_meta_lines_and_tool_labels(tgw):
    g, c, notes = tgw
    assert g.strip_meta("정리했어요. 저는 전화를 직접 걸 수 없어요. 끝나면 시스템이 알려 드릴 거예요.") == "정리했어요."
    assert g.tool_label("macbook__delegate") == "macbook에서 작업 위임" and g.tool_label("macbook__read_file") == "macbook 파일 읽는 중"
    assert g.tool_label("macbook__run_command") == "macbook에서 명령 실행 중" and g.tool_label("memory_recall") == "기억 찾는 중"
    assert g.tool_label("mcp__macbook__list_dir") == "macbook 파일 보는 중" and g.tool_label("web_search") == "웹 검색 중"


def test_an_instruction_in_a_finish_call_continues_the_same_task(tgw, monkeypatch):
    g, c, notes = tgw
    sent = []

    async def link_send(m):
        sent.append(m)

    async def voip(payload):
        return {"sent": 1}
    monkeypatch.setattr(g, "link_send", link_send)
    monkeypatch.setattr(g, "send_voip_push", voip)
    parent = {"id": "aaaabbbbcccc", "goal": "대시보드 구현 방법 연구", "status": "done", "notify": "call", "device": "macbook",
              "owner": g.DEVICE, "steps": [], "result": "세 가지 방법", "persona": "azel",
              "jobs": [{"device": "macbook", "job_id": "abcdefabcdef", "status": "done",
                        "session_id": "0f8c2b4e-1111-2222-3333-444455556666", "cwd": "/Users/jh/dev/sandbox"}]}
    c.portal.call(g.task_save, parent)
    c.portal.call(g.real_place_call, "task", "맡기신 연구 끝났어요.", "call-fc-1", None, None,
                  {"kind": "task", "id": parent["id"], "label": "연구"})
    assert g.call_task["call-fc-1"] == parent["id"]
    g.session_task["azel:fc"] = (g.call_task.pop("call-fc-1"), time.time())  # what the call's start log does

    async def in_call(body, origin):
        g.origin_ctx.set(origin)
        return await g.task_create(body, "voice")
    t = c.portal.call(in_call, {"goal": "그거 2번 방법으로 프로토타입까지 만들어 줘", "persona": "azel"}, "azel:fc")
    assert t["parent_task"] == parent["id"] and t["device"] == "macbook" and t["notify"] == "call"
    assert t["resume"] == {"device": "macbook", "session_id": "0f8c2b4e-1111-2222-3333-444455556666",
                           "cwd": "/Users/jh/dev/sandbox"}
    prompt = g.task_prompt(t, [])
    assert 'resume="0f8c2b4e-1111-2222-3333-444455556666"' in prompt and 'cwd="/Users/jh/dev/sandbox"' in prompt
    other = c.portal.call(in_call, {"goal": "새로운 조사 해 줘", "persona": "azel"}, "azel:elsewhere")
    assert not other.get("parent_task")  # another call: a new task
