import XCTest
@testable import Biseo

final class AssistantNameTests: XCTestCase {
    func testValidation() {
        XCTAssertEqual(AssistantName.validated("  아젤 "), "아젤")
        XCTAssertEqual(AssistantName.validated("Azel Kim"), "Azel Kim")
        XCTAssertNil(AssistantName.validated(""))
        XCTAssertNil(AssistantName.validated("   "))
        XCTAssertNil(AssistantName.validated("열세글자가넘는아주긴이름이에요"))
        XCTAssertNil(AssistantName.validated("아젤!"))
        XCTAssertNil(AssistantName.validated("R2D2"))
    }

    func testJosaFollowsBatchim() {
        XCTAssertEqual(AssistantName.josa("아젤", "이", "가"), "아젤이")   // ㄹ batchim
        XCTAssertEqual(AssistantName.josa("하나", "이", "가"), "하나가")
        XCTAssertEqual(AssistantName.josa("Azel", "을", "를"), "Azel를")   // non-Hangul: no batchim
    }

    func testSettingsCarryOptionalName() throws {
        let base = #"{"default_model":"auto","claude_path":"auto","task_defaults":{},"monthly_budget_usd":500,"budget_warn_pct":80,"proactive":{"morning_briefing":"08:00","quiet_hours":[],"triggers":{}},"default_device":"맥북","device_fallback":[]"#
        XCTAssertNil(try JSONDecoder().decode(AppSettings.self, from: Data((base + "}").utf8)).assistant_name)
        let named = try JSONDecoder().decode(AppSettings.self, from: Data((base + #","assistant_name":"아젤"}"#).utf8))
        XCTAssertEqual(named.assistant_name, "아젤")
        XCTAssertTrue(String(decoding: try JSONEncoder().encode(named), as: UTF8.self).contains("assistant_name"))
    }

    func testPushToTalkSilenceIs700msOfZeroPCM() {
        let d = CallClient.silence(milliseconds: 700)
        XCTAssertEqual(d.count, 22_400)
        XCTAssertTrue(d.allSatisfy { $0 == 0 })
    }
}

final class SetupTests: XCTestCase {
    func testPastedTokensLoseWhitespace() {
        XCTAssertEqual(Config.cleanToken("  abc\n def\t\n"), "abcdef")
        XCTAssertEqual(Config.cleanToken("abc"), "abc")
    }

    func testOnlyTLSAddressesAreUsedAndPlainOnesMigrate() {
        XCTAssertNil(Config.usable("ws://100.64.0.11:8891"))
        XCTAssertNil(Config.usable("http://100.64.0.8:8892"))
        XCTAssertNil(Config.usable("  "))
        XCTAssertNil(Config.usable(nil))
        XCTAssertEqual(Config.usable("wss://azel.scian.xyz"), "wss://azel.scian.xyz")
        let store = UserDefaults(suiteName: "MigrateTests")!
        store.removePersistentDomain(forName: "MigrateTests")
        store.set("ws://100.64.0.11:8891", forKey: "voiceURL")
        store.set("https://other.example", forKey: "gatewayURL")
        XCTAssertEqual(Config.migrateAddresses(store), ["voiceURL"])
        XCTAssertNil(store.string(forKey: "voiceURL"))
        XCTAssertEqual(store.string(forKey: "gatewayURL"), "https://other.example", "a custom TLS host stays")
    }

    func testCallSocketRejectionsAreExplained() {
        XCTAssertEqual(CallClient.closeMessage(status: 401, receivedAny: false), "토큰이 맞지 않거나 서버가 아직 업데이트되지 않았어요")
        XCTAssertEqual(CallClient.closeMessage(status: 403, receivedAny: false), "토큰이 맞지 않거나 서버가 아직 업데이트되지 않았어요")
        XCTAssertEqual(CallClient.closeMessage(status: 502, receivedAny: false), "음성 서버에 연결하지 못했어요 (HTTP 502)")
        XCTAssertNil(CallClient.closeMessage(status: nil, receivedAny: true), "a normal remote hang-up stays quiet")
    }
}

final class PersonalityTests: XCTestCase {
    func testPersonalityIsOptionalTrimmedAndCapped() throws {
        let base = #"{"default_model":"auto","claude_path":"auto","task_defaults":{},"monthly_budget_usd":500,"budget_warn_pct":80,"default_device":"macbook","device_fallback":[],"proactive":{"morning_briefing":"08:00"}"#
        XCTAssertNil(try JSONDecoder().decode(AppSettings.self, from: Data((base + "}").utf8)).personality)
        var s = try JSONDecoder().decode(AppSettings.self, from: Data((base + #","personality":"차분하고 다정하게"}"#).utf8))
        XCTAssertEqual(s.personality, "차분하고 다정하게")
        s.personality = ""                                   // 기본값으로 되돌리기
        XCTAssertTrue(String(decoding: try JSONEncoder().encode(s), as: UTF8.self).contains(#""personality":"""#))
        XCTAssertEqual(AssistantName.personality("  밝게\n"), "밝게")
        let withDefault = Data((base + #","personality":"","personality_default":"차분하고 짧게 말해요"}"#).utf8)
        XCTAssertEqual(PersonalityDefault.read(withDefault), "차분하고 짧게 말해요")
        let decoded = try JSONDecoder().decode(AppSettings.self, from: withDefault)
        XCTAssertFalse(String(decoding: try JSONEncoder().encode(decoded), as: UTF8.self).contains("personality_default"),
                       "the read-only default is never PUT back")
        XCTAssertEqual(AssistantName.personality(String(repeating: "가", count: 700)).count, 600)
    }
}
