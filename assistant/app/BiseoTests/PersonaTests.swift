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
        XCTAssertEqual(p[0].tintKey, .celadon, "an older hex colour maps to its tint")
        XCTAssertEqual(p[1].tintKey, .celadon, "unknown colours fall back to 청자")
        XCTAssertEqual(Persona(name: "x", color: "azalea").tintKey, .azalea)
        XCTAssertEqual(PersonaTint.allCases.count, 8)
        XCTAssertEqual(Persona.palette.first, "celadon")
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

    func testVoicesDeleteOrderAndDeletedLabel() throws {
        let v = try JSONDecoder().decode([Voice].self, from: Data(#"[{"id":"sohee","label":"소희","desc":"차분한 여성","lang":"ko"},{"id":"x"}]"#.utf8))
        XCTAssertEqual(v[0].desc, "차분한 여성")
        XCTAssertEqual(v[1].label, "x", "label falls back to the id")
        XCTAssertEqual(PersonaAPI.deletePath("p 2", deleteHistory: true), "/v1/personas/p%202?delete_history=true")
        XCTAssertEqual(PersonaAPI.deletePath("p3", deleteHistory: false), "/v1/personas/p3?delete_history=false")

        let store = UserDefaults(suiteName: "PersonaOrder")!
        store.removePersistentDomain(forName: "PersonaOrder")
        store.set(try JSONEncoder().encode(try personas()), forKey: "personasCache")
        let model = PersonaModel(store: store)
        XCTAssertEqual(model.ordered.first?.id, "p1", "primary first by default")
        model.move(model.ordered[2], by: -1)
        XCTAssertEqual(model.ordered.map(\.id), ["p1", "p3", "2"])
        XCTAssertEqual(PersonaModel(store: store).ordered.map(\.id), ["p1", "p3", "2"], "order survives a relaunch")
        XCTAssertEqual(model.label(for: "p3")?.name, "하나")
        XCTAssertEqual(model.label(for: "gone")?.name, "삭제된 비서")
        XCTAssertNil(model.label(for: nil))
    }

    func testSampleTextAndLimits() {
        XCTAssertEqual(VoiceSampler.sampleText(name: "아젤"), "안녕하세요, 아젤이에요. 무엇을 도와드릴까요?")
        XCTAssertEqual(VoiceSampler.sampleText(name: "하나"), "안녕하세요, 하나예요. 무엇을 도와드릴까요?")
        XCTAssertLessThanOrEqual(VoiceSampler.sampleText(name: "아주긴이름아주긴이름이에").count, 60)
        XCTAssertEqual(Persona.personalityLimit, 500)
    }
}
