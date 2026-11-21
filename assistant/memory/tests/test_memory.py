"""Records, context and contacts. Fixture: conftest.py (real Postgres, throwaway schemas)."""


def test_auth(client):
    assert client.get("/health", headers={"Authorization": ""}).json() == {"ok": True}
    for h in ({"Authorization": "Bearer nope"}, {"Authorization": ""}):
        assert client.post("/v1/search", json={"query": "x"}, headers=h).status_code == 401
        assert client.get("/v1/contacts", headers=h).status_code == 401
        assert client.get("/v1/context", headers=h).status_code == 401


def test_record_search_korean(client):
    texts = ["다음 주 화요일 오후 3시에 치과 예약을 잡아 두었다", "서버 디스크 사용량이 90%를 넘어 경고가 발생했다",
             "엄마 생신 선물로 목도리를 사기로 했다", "GISTRA 앱 배포는 금요일로 미뤘다"]
    for i, t in enumerate(texts):
        r = client.post("/v1/records", json={"kind": "transcript", "text": t, "session": "s1", "device": "맥북",
                                             "meta": {"i": i}})
        assert r.status_code == 200 and r.json()["id"]
    assert client.post("/v1/records", json={"kind": "bogus", "text": "x"}).status_code == 400
    for query, want in [("치아 진료 언제였지?", 0), ("스토리지 용량이 부족하다는 알림", 1),
                        ("어머니 생일 선물 뭐 사기로 했더라", 2)]:
        hits = client.post("/v1/search", json={"query": query, "limit": 4}).json()
        assert hits[0]["text"] == texts[want], (query, hits)
        assert hits[0]["score"] > hits[1]["score"]
    hits = client.post("/v1/search", json={"query": "치과", "kinds": ["note"]}).json()
    assert hits == []
    hits = client.post("/v1/search", json={"query": "치과", "since": "2999-01-01T00:00:00Z"}).json()
    assert hits == []


def test_context_upsert(client):
    r = client.put("/v1/context/t1", json={"summary": "치과 예약 중", "open_questions": ["몇 시?"]}).json()
    assert r["summary"] == "치과 예약 중" and r["open_questions"] == ["몇 시?"]
    first = r["updated"]
    r = client.put("/v1/context/t1", json={"summary": "예약 완료", "open_questions": []}).json()
    assert r["summary"] == "예약 완료" and r["open_questions"] == [] and r["updated"] > first
    client.put("/v1/context/t2", json={"summary": "두 번째"})
    assert client.get("/v1/context/t1").json()["summary"] == "예약 완료"
    assert client.get("/v1/context/missing").status_code == 404
    assert [c["task_id"] for c in client.get("/v1/context?recent=1").json()] == ["t2"]


def test_contacts_crud(client):
    c = client.post("/v1/contacts", json={"name": "김철수", "phone": "010-1234-5678", "notes": "치과"}).json()
    assert c["id"] and c["email"] is None
    assert client.get(f"/v1/contacts/{c['id']}").json()["name"] == "김철수"
    assert [x["id"] for x in client.get("/v1/contacts?q=철수").json()] == [c["id"]]
    u = client.put(f"/v1/contacts/{c['id']}", json={"name": "김철수", "email": "cs@example.com"}).json()
    assert u["email"] == "cs@example.com" and u["phone"] is None
    assert client.post("/v1/contacts", json={"name": ""}).status_code == 422
    assert client.delete(f"/v1/contacts/{c['id']}").json() == {"deleted": c["id"]}
    assert client.get(f"/v1/contacts/{c['id']}").status_code == 404
    assert client.put("/v1/contacts/999999", json={"name": "x"}).status_code == 404


def test_memory_list_and_delete_touch_only_remembered_facts(client):
    fact = client.post("/v1/records", json={"kind": "note", "text": "좋아하는 커피는 아이스 라떼",
                                            "meta": {"memory": True, "source": "explicit"}}).json()
    summary = client.post("/v1/records", json={"kind": "note", "text": "통화 요약", "meta": {}}).json()
    listed = client.get("/v1/records?kind=note&memory=true").json()
    assert [r["id"] for r in listed][:1] == [fact["id"]] and summary["id"] not in [r["id"] for r in listed]
    assert client.delete(f"/v1/records/{summary['id']}").status_code == 404  # not a memory: kept
    assert client.delete(f"/v1/records/{fact['id']}").json()["id"] == fact["id"]
    assert client.delete(f"/v1/records/{fact['id']}").status_code == 404
