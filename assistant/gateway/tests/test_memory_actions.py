"""remember / memory_recall / people_lookup / forget / memory_list through the bridge, with an in-memory store and a
fake Cygnus (read and memory-scoped write)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tools_bridge import World  # noqa: E402


class Mem(World):
    def __init__(self, can_write=True):
        super().__init__()
        self.recs, self.cyg_writes, self.can = {}, [], can_write
        self.save_reply = '[{"status":"saved","id":"m_abc123"}]'

    async def search(self, q, n):
        words = q.split()
        rows = [{**r, "score": sum(w in r["text"] for w in words) / len(words)} for r in self.recs.values()]
        return sorted([r for r in rows if r["score"] > 0], key=lambda r: -r["score"])[:n]

    async def add(self, text, meta):
        rid = max(self.recs, default=0) + 1
        self.recs[rid] = {"id": rid, "text": text, "meta": {k: v for k, v in meta.items() if v is not None},
                          "created": "2026-11-23"}
        return {"id": rid}

    async def listing(self, n):
        return sorted(self.recs.values(), key=lambda r: -r["id"])[:n]

    async def delete(self, rid):
        return self.recs.pop(int(rid))

    async def cyg_read(self, name, args):
        if name == "gistra_people":
            return "김민수 교수 — 운영체제, 오룡관 301호, 화 3-5시 면담"
        if name == "memory_recall":
            return "지도교수는 김민수 교수 (person)" if "교수" in args["query"] or "사람" in args["query"] else "(결과 없음)"
        return ""

    async def cyg_write(self, name, args):
        self.cyg_writes.append((name, args))
        if name == "memory_save":
            return self.save_reply
        return '{"ok":true,"message":"archived (restorable)"}'

    def bridge(self):
        b = super().bridge()
        b.d.update(memory_search=self.search, memory_add=self.add, memory_list=self.listing,
                   memory_delete=self.delete, cygnus=lambda: ([], self.cyg_read),
                   cygnus_can_write=lambda: self.can, cygnus_write=self.cyg_write)
        return b


def run(coro):
    return asyncio.run(coro)


def test_remember_then_recall_with_sources():
    w = Mem()
    b = w.bridge()
    r = run(b.run("remember", {"fact": "좋아하는 커피는 아이스 바닐라 라떼"}, "azel", caller="qwen"))
    assert r["ok"] and r["spoken"] == "기억해 둘게요: 좋아하는 커피는 아이스 바닐라 라떼."
    assert w.recs[1]["meta"] == {"memory": True, "source": "explicit", "cygnus_id": "m_abc123"}
    assert w.cyg_writes == [("memory_save", {"text": "좋아하는 커피는 아이스 바닐라 라떼", "kind": "preference"})]
    r = run(b.run("memory_recall", {"query": "커피"}, "azel", caller="qwen"))
    assert r["ok"] and "아이스 바닐라 라떼" in r["spoken"] and "(아젤 기억)" in r["spoken"]
    assert "[아젤 기억" in run(b.call("memory_recall", {"query": "커피"}, "azel"))["content"]  # what a model reads


def test_secrets_are_refused_everywhere():
    w = Mem()
    b = w.bridge()
    for fact in ("내 카드번호는 1234 5678 9012 3456", "와이파이 비밀번호는 hunter2", "깃허브 토큰 ghp_x 기억해",
                 "api key는 sk-abc"):
        r = run(b.run("remember", {"fact": fact}, "azel", caller="qwen"))
        assert r["error"]["code"] == "invalid" and "비밀" in r["spoken"], fact
    assert not w.recs and not w.cyg_writes


def test_persona_scope_stays_local_and_out_of_cygnus():
    w = Mem()
    b = w.bridge()
    run(b.run("remember", {"fact": "미아랑은 반말로 얘기해", "persona_scope": "persona"}, "mia", caller="qwen"))
    assert w.recs[1]["meta"]["persona"] == "mia" and not w.cyg_writes  # Cygnus is shared memory
    assert run(b.run("memory_recall", {"query": "반말로"}, "azel"))["spoken"] == "기억에서 찾지 못했어요."
    assert "반말" in run(b.run("memory_recall", {"query": "반말로"}, "mia"))["spoken"]


def test_without_a_write_token_azel_still_remembers():
    w = Mem(can_write=False)
    r = run(w.bridge().run("remember", {"fact": "주말엔 늦잠"}, "azel"))
    assert r["ok"] and w.recs and not w.cyg_writes


def test_people_lookup_answers_from_cygnus_with_sources():
    w = Mem()
    r = run(w.bridge().call("people_lookup", {"relation": "지도교수"}, "azel", caller="qwen"))
    assert not r["is_error"] and "[Cygnus 사람 목록]" in r["content"] and "김민수" in r["content"]
    assert "지어내지 마" in r["content"]
    assert run(w.bridge().run("people_lookup", {}, "azel"))["error"]["code"] == "invalid"


def test_forget_asks_first_then_archives_in_cygnus():
    w = Mem()
    b = w.bridge()
    run(b.run("remember", {"fact": "좋아하는 커피는 아이스 라떼"}, "azel"))
    r = run(b.run("forget", {"query": "커피"}, "azel", caller="qwen"))
    assert r["needs"]["kind"] == "confirm" and r["needs"]["args"] == {"id": "1", "confirmed": True} and w.recs
    r = run(b.run("forget", r["needs"]["args"], "azel", caller="qwen"))
    assert r["ok"] and not w.recs and w.cyg_writes[-1] == ("memory_edit", {"id": "m_abc123", "op": "archive"})
    assert run(b.run("forget", {"query": "커피", "confirmed": True}, "azel"))["error"]["code"] == "not_found"


def test_memory_list_for_the_app():
    w = Mem()
    b = w.bridge()
    run(b.run("remember", {"fact": "아침엔 차를 마셔"}, "azel"))
    r = run(b.run("memory_list", {}, "azel", caller="app"))
    assert r["data"][0]["text"] == "아침엔 차를 마셔" and r["data"][0]["source"] == "explicit" and r["data"][0]["in_cygnus"]


def test_post_call_facts_are_auto_local_and_filtered(monkeypatch):
    from test_gateway import start  # noqa: E402  the gateway against the fake state service
    import tempfile
    from fastapi.testclient import TestClient
    tmp = Path(tempfile.mkdtemp())
    g = start(tmp, monkeypatch)
    with TestClient(g.app, headers={"Authorization": "Bearer t0k"}):
        g.POST_CALL_DELAY_S = 0
        g.fake.db["history"]["log1"] = {"id": "log1", "turns": [
            {"user": "나는 아이스 바닐라 라떼를 제일 좋아해", "assistant": ["기억할게요."]},
            {"user": "그리고 요즘 허리 디스크 때문에 병원 다녀", "assistant": ["저런."]}]}
        calls = []

        async def qwen(system, user):
            calls.append(user)
            return {"facts": ["사용자는 아이스 바닐라 라떼를 제일 좋아한다", "사용자는 허리 디스크로 병원에 다닌다",
                              "사용자의 카드번호는 1234-5678-9012-3456", "a", "b", "c"]}
        monkeypatch.setattr(g, "llm_json", qwen)
        writes = []
        monkeypatch.setattr(g, "cygnus_write", lambda *a: writes.append(a))
        asyncio.run(g.post_call_facts("log1", "azel"))
        recs = list(g.fake.db["records"].values())
        assert [r["text"] for r in recs] == ["사용자는 아이스 바닐라 라떼를 제일 좋아한다"]  # health and secrets out
        assert recs[0]["meta"] == {"memory": True, "source": "auto", "call": "log1", "persona": "azel"} and not writes
        asyncio.run(g.post_call_facts("log1", "azel"))
        assert len(g.fake.db["records"]) == 1  # the same fact twice: kept once


def test_a_similar_cygnus_memory_is_never_claimed():
    w = Mem()
    w.save_reply = '[{"status":"similar","reason":"very similar memory exists","similar":[{"id":"m_users1","similarity":0.99}]}]'
    b = w.bridge()
    r = run(b.run("remember", {"fact": "좋아하는 커피는 라떼"}, "azel"))
    assert r["ok"] and "비슷한 기억이 이미" in r["spoken"] and w.recs[1]["meta"].get("cygnus_id") is None
    r = run(b.run("forget", {"id": "1", "confirmed": True}, "azel"))
    assert r["ok"] and not any(n == "memory_edit" for n, _ in w.cyg_writes)  # the user's own memory stays
    w.save_reply = '[{"status":"대기 중","id":"m_held","reason":"held for owner review"}]'
    r = run(b.run("remember", {"fact": "주말엔 등산"}, "azel"))
    assert "확인을 기다려요" in r["spoken"] and list(w.recs.values())[-1]["meta"].get("cygnus_id") is None


def test_recall_puts_facts_first_and_never_returns_past_replies():  # live 2026-11-23: failed replies outranked the fact
    w = Mem()
    w.recs[91] = {"id": 91, "kind": "transcript", "created": "2026-11-23T03:22",
                  "text": "사용자: 내가 갖고 있는 주식 보유 현황 알려줘\n비서: 네, 대표님. 저장된 주식 보유 내역이 없어서 확인이 안 돼요. "
                          "바로 찾아볼게요."}
    w.recs[92] = {"id": 92, "kind": "note", "created": "2026-11-23T02:50", "meta": {"memory": True},
                  "text": "사용자는 애플, 토스, 엔비디아, 삼성전자, SK하이닉스 주식을 보유하고 있다."}
    b = w.bridge()
    out = run(b.call("memory_recall", {"query": "주식 보유"}, "azel"))["content"]
    assert out.index("애플, 토스") < out.index("[지난 통화") and "확인이 안 돼요" not in out and "찾아볼게요" not in out, out
    assert "내가 갖고 있는 주식 보유 현황 알려줘" in out  # the user's own words from that call stay
    assert run(b.run("memory_recall", {"query": "주식 보유"}, "azel"))["spoken"].startswith("사용자는 애플")
