"""A voice-made task that works on a device asks once how approvals should go (PM 2026-11-23), on every engine."""
from test_tasks import tgw  # noqa: F401  (fixture)

Q = "진행하다 승인이 필요하면 자동 승인으로 할까요, 아니면 그때마다 물어볼까요?"


def call(g, c, args, who="ask:azel:call-1"):
    async def go():
        g.requester_ctx.set(who)
        return await g.bridge.run("create_task", args, None, "voice")
    return c.portal.call(go)


def tasks(g):
    return [v for k, v in g.fake.db["kv"].items() if k.startswith("task-") and isinstance(v, dict)]


def test_device_work_asks_once_then_takes_the_answer(tgw):  # noqa: F811
    g, c, _ = tgw
    assert c.get("/v1/settings").json()["task_auto_approve_default"] == "ask"
    r = call(g, c, {"goal": "실버텔러 시장 조사해서 PDF로 맥북에 저장해 줘", "notify": "none"})
    assert r["needs"]["kind"] == "choose" and r["needs"]["question"] == Q and not tasks(g)  # nothing made yet
    r = call(g, c, {"goal": "실버텔러 시장 조사해서 PDF로 맥북에 저장해 줘", "notify": "none", "auto_approve": True})
    assert r["ok"] and r["data"]["auto_approve"] is True  # asked in this run, so its yes counts
    r = call(g, c, {"goal": "맥북 다운로드 폴더 정리해 줘", "notify": "none", "auto_approve": True}, who="ask:azel:call-2")
    assert r["needs"] and r["needs"]["question"] == Q  # a true nobody asked for: asked anyway
    r = call(g, c, {"goal": "맥북 다운로드 폴더 정리해 줘", "notify": "none", "auto_approve": False}, who="ask:azel:call-3")
    assert r["ok"] and r["data"]["auto_approve"] is False  # "물어보고 해": the safe way needs no question


def test_no_question_when_the_user_said_or_set_a_default_or_it_is_not_device_work(tgw):  # noqa: F811
    g, c, _ = tgw
    r = call(g, c, {"goal": "맥북에서 빌드 돌려 줘. 자동으로 해", "notify": "none"})
    assert r["ok"] and r["data"]["auto_approve"] is True
    r = call(g, c, {"goal": "맥북에서 빌드 돌려 줘. 그때마다 물어봐 줘", "notify": "none"})
    assert r["ok"] and r["data"]["auto_approve"] is False
    r = call(g, c, {"goal": "오늘 통화 내용 요약해 줘", "notify": "none"})
    assert r["ok"] and r["data"]["auto_approve"] is False  # not device work: nothing to ask
    assert c.put("/v1/settings", json={"task_auto_approve_default": "auto"}).status_code == 200
    r = call(g, c, {"goal": "맥북 파일 정리해 줘", "notify": "none"})
    assert r["ok"] and r["data"]["auto_approve"] is True
    assert c.put("/v1/settings", json={"task_auto_approve_default": "ask_each"}).status_code == 200
    r = call(g, c, {"goal": "맥북 파일 정리해 줘", "notify": "none"})
    assert r["ok"] and r["data"]["auto_approve"] is False
    assert c.put("/v1/settings", json={"task_auto_approve_default": "sometimes"}).status_code == 400
    r = c.post("/v1/tasks", json={"goal": "맥북 파일 정리해 줘", "notify": "none"})  # the app's sheet has its own toggle
    assert r.status_code in (200, 201) and r.json()["auto_approve"] is False


def test_from_now_on_sets_the_default(tgw):  # noqa: F811
    g, c, _ = tgw
    call(g, c, {"goal": "맥북에서 보고서 만들어 줘", "notify": "none"})
    r = call(g, c, {"goal": "맥북에서 보고서 만들어 줘", "notify": "none", "auto_approve": True, "remember_choice": True})
    assert r["ok"] and c.get("/v1/settings").json()["task_auto_approve_default"] == "auto"  # "앞으로는 다 자동으로 해"
    r = call(g, c, {"goal": "서버 로그 정리해 줘", "notify": "none", "auto_approve": False, "remember_choice": True})
    assert r["ok"] and c.get("/v1/settings").json()["task_auto_approve_default"] == "ask_each"


def test_a_run_that_just_read_outside_text_never_turns_it_on(tgw):  # noqa: F811
    g, c, _ = tgw
    assert c.put("/v1/settings", json={"task_auto_approve_default": "auto"}).status_code == 200
    import time
    g.bridge.tainted["ask:azel:web"] = time.monotonic()  # just read a web page in this run
    r = call(g, c, {"goal": "맥북 파일 정리해 줘", "notify": "none", "confirmed": True}, who="ask:azel:web")
    assert r["ok"] and r["data"]["auto_approve"] is False
