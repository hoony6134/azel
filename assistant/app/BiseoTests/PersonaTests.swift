import XCTest
@testable import Biseo

@MainActor
final class PersonaTests: XCTestCase {
    private let json = #"""
    [{"id":"p1","name":"아젤","voice":"sohee","voice_style":"차분하게","personality":"","color":"#82CBB4","default_model":"auto","share_memory":true,"primary":true},
     {"id":2,"name":"Rin","color":"nonsense","share_memory":false},
     {"id":"p3","name":"하나"}]
    """#

    private func personas() throws -> [Persona] { try JSONDecoder().decode([Persona].self, from: Data(json.utf8)) }

    func testDecodingIsTolerant() throws {
        let p = try personas()
        XCTAssertEqual(p.map(\.id), ["p1", "2", "p3"])
        XCTAssertTrue(p[0].primary)
        XCTAssertFalse(p[1].share_memory)
        XCTAssertTrue(p[2].share_memory, "share_memory defaults to true")
        XCTAssertNotNil(Persona.color(hex: p[0].color))
        XCTAssertNil(Persona.color(hex: p[1].color), "bad colors fall back to the accent")
        let body = PersonaAPI.body(p[0])
        XCTAssertNil(body["id"]); XCTAssertNil(body["primary"])
        XCTAssertEqual(body["voice"] as? String, "sohee")
        XCTAssertTrue(PersonaAPI.body(p[2])["color"] is NSNull)
    }

    func testSelectionFallsBackToPrimaryAndSiriMatchesByName() throws {
        let p = try personas()
        XCTAssertEqual(PersonaModel.resolve("p3", in: p)?.name, "하나")
        XCTAssertEqual(PersonaModel.resolve("deleted", in: p)?.id, "p1", "a deleted choice falls back to the primary")
        XCTAssertEqual(PersonaModel.resolve(nil, in: p)?.id, "p1")
        XCTAssertNil(PersonaModel.resolve(nil, in: []))
        let store = UserDefaults(suiteName: "PersonaTests")!
        store.removePersistentDomain(forName: "PersonaTests")
        store.set(try JSONEncoder().encode(p), forKey: "personasCache")
        let model = PersonaModel(store: store)
        model.selectedID = "2"
        XCTAssertEqual(PersonaModel(store: store).selected?.name, "Rin", "the choice survives a relaunch")
        XCTAssertEqual(model.match(name: "rin")?.id, "2")
        XCTAssertEqual(model.match(name: "하나")?.id, "p3")
        XCTAssertNil(model.match(name: "없는 이름"))
        XCTAssertEqual(model.callName, "Rin")
    }

    func testCallHistoryAndScheduleCarryThePersona() throws {
        let req = try CallClient.request(voiceURL: "wss://azel.scian.xyz", token: "t", session: "iphone-x", callID: nil, persona: "p3")
        XCTAssertEqual(req.url?.query, "session=iphone-x&persona=p3")
        let items = try JSONDecoder().decode([HistoryItem].self, from: Data(#"[{"id":"a","started_at":1,"persona_id":"p1"},{"id":"b","started_at":2,"persona_id":3},{"id":"c","started_at":3}]"#.utf8))
        XCTAssertEqual(items[1].persona_id, "3")
        XCTAssertEqual(HistoryCache.filter(items, persona: "p1").map(\.id), ["a"])
        XCTAssertEqual(HistoryCache.filter(items, persona: nil).count, 3)
        let body = ScheduleAPI.body(at: Date(timeIntervalSince1970: 1_795_309_200), repeatRule: nil, kind: "wake", note: "", persona: "p3")
        XCTAssertEqual(body["persona_id"] as? String, "p3")
        let item = try JSONDecoder().decode(ScheduledCall.self, from: Data(#"{"id":"s","at":"2026-11-22T10:00:00+09:00","kind":"wake","persona_id":"p3"}"#.utf8))
        XCTAssertEqual(item.personaID, "p3")
    }

    func testRefreshFromFakeServer() async throws {
        URLProtocol.registerClass(FakeServer.self)
        Config.testOverride = (gateway: "https://fake.test", token: "test-token")
        defer { URLProtocol.unregisterClass(FakeServer.self); Config.testOverride = nil }
        FakeServer.delay = 0
        FakeServer.routes = ["/v1/personas": (200, #"{"personas":"# + json + "}")]
        let store = UserDefaults(suiteName: "PersonaFake")!
        store.removePersistentDomain(forName: "PersonaFake")
        let model = PersonaModel(store: store)
        await model.refresh()
        XCTAssertEqual(model.personas.count, 3)
        XCTAssertEqual(model.primary?.id, "p1")
        FakeServer.routes = [:]                       // 404: an older gateway
        await model.refresh()
        XCTAssertEqual(model.status, .unavailable)
        XCTAssertEqual(model.personas.count, 3, "cached personas stay")
    }
}
