import XCTest
@testable import Biseo

final class ProactiveTests: XCTestCase {
    private let base = #"{"default_model":"auto","claude_path":"auto","task_defaults":{},"monthly_budget_usd":500,"budget_warn_pct":80,"default_device":"맥북","device_fallback":["알비레오","스파크","albireo"],"proactive":"#

    func testNewShapeDecodesAndEncodesWithoutQuietHours() throws {
        let json = base + #"{"enabled":true,"morning_briefing":{"enabled":false,"time":"07:30"},"deadline_check":{"enabled":true,"time":"20:00"},"server_alerts":false,"autonomous":{"enabled":true,"max_per_day":3,"min_gap_minutes":90,"window":["10:00","22:00"]}}}"#
        let s = try JSONDecoder().decode(AppSettings.self, from: Data(json.utf8))
        XCTAssertEqual(s.proactive.morning_briefing, .init(enabled: false, time: "07:30"))
        XCTAssertEqual(s.proactive.deadline_check.time, "20:00")
        XCTAssertFalse(s.proactive.server_alerts)
        XCTAssertEqual(s.proactive.autonomous, .init(enabled: true, max_per_day: 3, min_gap_minutes: 90, window: ["10:00", "22:00"]))
        let out = String(decoding: try JSONEncoder().encode(s), as: UTF8.self)
        XCTAssertFalse(out.contains("quiet_hours"))
        XCTAssertFalse(out.contains("triggers"))
        XCTAssertEqual(try JSONDecoder().decode(AppSettings.self, from: Data(out.utf8)), s)
    }

    func testOldShapeMigrates() throws {
        let json = base + #"{"morning_briefing":"06:45","quiet_hours":["23:30","07:30"],"triggers":{"deadline":false,"server_down":true}}}"#
        let p = try JSONDecoder().decode(AppSettings.self, from: Data(json.utf8)).proactive
        XCTAssertTrue(p.enabled)
        XCTAssertEqual(p.morning_briefing, .init(enabled: true, time: "06:45"))
        XCTAssertFalse(p.deadline_check.enabled)
        XCTAssertTrue(p.server_alerts)
        XCTAssertFalse(p.autonomous.enabled, "autonomous calls stay off unless turned on")
    }

    func testDevicesShowAsEnglishIds() throws {
        let s = DeviceID.normalize(try JSONDecoder().decode(AppSettings.self, from: Data((base + #"{"morning_briefing":"08:00"}}"#).utf8)))
        XCTAssertEqual(s.default_device, "macbook")
        XCTAssertEqual(s.device_fallback, ["albireo", "spark"], "mapped, order kept, duplicates dropped")
        XCTAssertEqual(DeviceID.normalize("Deneb"), "deneb")
        XCTAssertEqual(DeviceID.normalize("기에나"), "gienah")
    }

    func testDecisionLogDecodes() throws {
        let json = #"[{"ts":1795309200,"call":true,"urgency":"high","reason":"과제 마감 1시간 전"},{"ts":"2026-11-22T09:00:00+09:00","call":false,"urgency":2,"reason":"조용한 시간"}]"#
        let items = try JSONDecoder().decode([ProactiveDecision].self, from: Data(json.utf8))
        XCTAssertTrue(items[0].call)
        XCTAssertEqual(items[0].urgency, "high")
        XCTAssertEqual(items[1].urgency, "2")
        XCTAssertEqual(items[1].ts, Date(timeIntervalSince1970: 1_795_305_600))
    }
}
