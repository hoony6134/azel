import XCTest
@testable import Biseo

@MainActor
final class UserTitleTests: XCTestCase {
    func testValidation() {
        XCTAssertEqual(AssistantName.validatedTitle(""), "", "empty = 호칭 없이")
        XCTAssertEqual(AssistantName.validatedTitle("  정훈님 "), "정훈님")
        XCTAssertEqual(AssistantName.validatedTitle("Boss"), "Boss")
        XCTAssertNil(AssistantName.validatedTitle("열세글자가넘는아주긴호칭이에요"))
        XCTAssertNil(AssistantName.validatedTitle("대표님!"))
    }

    func testSettingAndPersonaBody() throws {
        let base = #"{"default_model":"auto","claude_path":"auto","task_defaults":{},"monthly_budget_usd":500,"budget_warn_pct":80,"default_device":"macbook","device_fallback":[],"proactive":{"morning_briefing":"08:00"}"#
        XCTAssertNil(try JSONDecoder().decode(AppSettings.self, from: Data((base + "}").utf8)).user_title)
        var s = try JSONDecoder().decode(AppSettings.self, from: Data((base + #","user_title":"정훈님"}"#).utf8))
        XCTAssertEqual(s.user_title, "정훈님")
        s.user_title = ""
        XCTAssertTrue(String(decoding: try JSONEncoder().encode(s), as: UTF8.self).contains(#""user_title":"""#), "clearing sends \"\"")
        XCTAssertNil(PersonaAPI.body(Persona(name: "미아", user_title: nil))["user_title"])
        XCTAssertNil(PersonaAPI.body(Persona(name: "미아", user_title: ""))["user_title"], "\"\" follows the shared setting: left out")
        XCTAssertEqual(PersonaAPI.body(Persona(name: "미아", user_title: "오빠"))["user_title"] as? String, "오빠")
    }

    func testOlderGatewayStillSavesTheOtherFields() async throws {
        URLProtocol.registerClass(FakeServer.self)
        Config.testOverride = (gateway: "https://fake.test", token: "test-token")
        defer { URLProtocol.unregisterClass(FakeServer.self); Config.testOverride = nil }
        FakeServer.delay = 0
        FakeServer.seen = []
        FakeServer.routes = ["GET /v1/personas": (200, "[]")]
        FakeServer.queued = ["PUT /v1/personas/p2": [(400, #"{"detail":"unknown persona fields: user_title"}"#), (200, "{}")]]
        defer { FakeServer.queued = [:] }
        let store = UserDefaults(suiteName: "UserTitleTests")!
        store.removePersistentDomain(forName: "UserTitleTests")
        let model = PersonaModel(store: store)
        let note = try await model.save(Persona(id: "p2", name: "미아", voice: "ono_anna", user_title: "오빠"))
        XCTAssertEqual(note, "호칭은 서버 업데이트 후 저장돼요")
        let puts = FakeServer.seen.filter { $0.method == "PUT" }
        XCTAssertEqual(puts.count, 2)
        XCTAssertTrue(String(decoding: puts[0].body, as: UTF8.self).contains("user_title"))
        XCTAssertFalse(String(decoding: puts[1].body, as: UTF8.self).contains("user_title"))
        XCTAssertTrue(String(decoding: puts[1].body, as: UTF8.self).contains("ono_anna"), "the other fields still saved")
    }
}
