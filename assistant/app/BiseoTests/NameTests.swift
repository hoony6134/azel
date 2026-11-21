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
