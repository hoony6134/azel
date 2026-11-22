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

/// Live bug, 2026-11-22: the log decoded `ts` but the server sends `at`, and the decoding error read
/// as "아젤에게 닿지 않아요".
final class ProactiveLogShapeTests: XCTestCase {
    private let server = #"""
    [{"id":"p7","at":1795309200,"call":true,"urgency":3,"reason":"과제 마감이 2시간 남았어요","opening":"과제 마감이 두 시간 남았어요.","called":false,"blocked_by":"min_gap","signals":{"deadline":true},"call_id":null,"persona_id":"azel"},
     {"id":"p8","at":1795312800.5,"call":true,"urgency":4,"reason":"회의 10분 전","opening":"곧 회의예요.","called":true,"blocked_by":null,"signals":{},"call_id":"c1","persona_id":"azel"},
     {"id":"p9","at":1795316400,"call":false,"urgency":1,"reason":"급한 일 없음","opening":"","called":false,"blocked_by":"no_call","signals":{},"call_id":null,"persona_id":null}]
    """#

    func testDecodesTheServersShape() throws {
        let items = try JSONDecoder().decode([ProactiveDecision].self, from: Data(server.utf8))
        XCTAssertEqual(items.map(\.id), ["p7", "p8", "p9"])
        XCTAssertEqual(items[0].ts, Date(timeIntervalSince1970: 1_795_309_200))
        XCTAssertEqual(items[0].urgency, "3")
        XCTAssertEqual(items[0].notCalledBecause, "간격 제한으로 안 걸었어요")
        XCTAssertEqual(items[0].opening, "과제 마감이 두 시간 남았어요.")
        XCTAssertTrue(items[1].called)
        XCTAssertNil(items[1].notCalledBecause)
        XCTAssertEqual(items[2].notCalledBecause, "전화할 필요 없다고 판단했어요")
        XCTAssertNil(items[2].opening, "empty opening hidden")
        // The old shape still reads.
        let old = try JSONDecoder().decode(ProactiveDecision.self, from: Data(#"{"ts":1795309200,"call":true,"reason":"x"}"#.utf8))
        XCTAssertTrue(old.called)
    }

    func testADecodingErrorIsNeverUnreachable() {
        do {
            _ = try JSONDecoder().decode([ProactiveDecision].self, from: Data(#"[{"call":true}]"#.utf8))
            XCTFail("expected a decoding error")
        } catch {
            XCTAssertEqual(FriendlyError.describe(error).title, "응답 형식이 달라요 (앱 업데이트 필요)")
            XCTAssertEqual(FriendlyError.keyPath(error as! DecodingError), "[0].ts")
        }
    }
}
