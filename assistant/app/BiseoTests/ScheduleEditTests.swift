import XCTest
@testable import Biseo

final class RepeatPresetTests: XCTestCase {
    func testPresetsMakeCron() {
        XCTAssertEqual(RepeatPreset.daily.cron(hour: 7, minute: 30), "30 7 * * *")
        XCTAssertEqual(RepeatPreset.weekdays.cron(hour: 9, minute: 0), "0 9 * * 1-5")
        XCTAssertEqual(RepeatPreset.days([4, 2]).cron(hour: 14, minute: 0), "0 14 * * 2,4")
        XCTAssertEqual(RepeatPreset.everyHours(n: 2, from: 10, to: 18, weekdaysOnly: true).cron(hour: 0, minute: 0), "0 10-18/2 * * 1-5")
        XCTAssertEqual(RepeatPreset.monthly(day: 1).cron(hour: 9, minute: 0), "0 9 1 * *")
    }

    func testParseRoundTripsAndRefusesTheRest() {
        for (p, h, m) in [(RepeatPreset.daily, 7, 30), (.weekdays, 9, 0), (.days([0, 6]), 11, 5), (.monthly(day: 15), 20, 0)] {
            let r = RepeatPreset.parse(p.cron(hour: h, minute: m))
            XCTAssertEqual(r?.preset, p); XCTAssertEqual(r?.hour, h); XCTAssertEqual(r?.minute, m)
        }
        XCTAssertEqual(RepeatPreset.parse("0 10-18/2 * * 1-5")?.preset, .everyHours(n: 2, from: 10, to: 18, weekdaysOnly: true))
        XCTAssertEqual(RepeatPreset.parse("0 9 * * 7")?.preset, .days([0]), "7 is Sunday too")
        XCTAssertNil(RepeatPreset.parse("*/30 * * * *"))
        XCTAssertNil(RepeatPreset.parse("0 9 1 * 1"), "dom and dow together")
        XCTAssertNil(RepeatPreset.parse("0 9 * 1 *"), "month restricted")
    }
}

@MainActor
final class ScheduleEditTests: XCTestCase {
    private let newItem = #"{"id":"a1","at":"2026-11-23T10:00:00+09:00","next_at":"2026-11-23T12:00:00+09:00","cron":"0 10-18/2 * * 1-5","rule_text":"평일 오전 10시부터 오후 6시까지 2시간마다","notify":"call","kind":"reminder","purpose":"스트레칭","repeat":null,"status":"pending"}"#
    private let oldItem = #"{"id":"b2","at":"2026-11-23T07:00:00+09:00","kind":"wake","repeat":"daily","status":"pending"}"#

    override func setUp() async throws {
        URLProtocol.registerClass(FakeServer.self)
        Config.testOverride = (gateway: "https://fake.test", token: "test-token")
        FakeServer.seen = []
    }

    override func tearDown() async throws {
        URLProtocol.unregisterClass(FakeServer.self)
        Config.testOverride = nil
        FakeServer.routes = [:]
    }

    func testDecodesRuleFieldsAndGuardsOnOldGateways() async throws {
        let model = ScheduleModel()
        FakeServer.routes = ["GET /v1/schedule": (200, "[\(oldItem)]")]
        await model.refreshAll()
        XCTAssertFalse(model.canEdit, "old gateway: no editing")
        XCTAssertNil(model.upcoming.first?.ruleText)

        FakeServer.routes = ["GET /v1/schedule": (200, "[\(newItem)]")]
        await model.refreshAll()
        XCTAssertTrue(model.canEdit)
        let item = try XCTUnwrap(model.upcoming.first)
        XCTAssertTrue(item.isRule)
        XCTAssertEqual(item.ruleText, "평일 오전 10시부터 오후 6시까지 2시간마다")
        XCTAssertEqual(KST.iso(item.when), "2026-11-23T12:00:00+09:00", "the next fire, not the first")
        XCTAssertEqual(item.purpose, "스트레칭")
    }

    func testThisOnlyEditSkipsTheOccurrenceAndBooksAOneOff() async throws {
        let item = try JSONDecoder().decode(ScheduledCall.self, from: Data(newItem.utf8))
        FakeServer.routes = ["POST /v1/schedule": (201, oldItem), "DELETE /v1/schedule/a1": (204, "")]
        let at = item.when.addingTimeInterval(1800)
        try await ScheduleAPI.replaceOccurrence(of: item, at: at, notify: "push", purpose: "물 마시기")
        let post = try XCTUnwrap(FakeServer.seen.first { $0.method == "POST" })
        let body = try JSONSerialization.jsonObject(with: post.body) as! [String: Any]
        XCTAssertEqual(body["at"] as? String, "2026-11-23T12:30:00+09:00")
        XCTAssertEqual(body["notify"] as? String, "push")
        XCTAssertNil(body["cron"])
        XCTAssertTrue(FakeServer.seen.contains { $0.method == "DELETE" && $0.path == "/v1/schedule/a1" })
    }

    func testOccurrenceQueryKeepsThePlus() {
        XCTAssertEqual(ScheduleAPI.query("2026-11-23T12:00:00+09:00"), "2026-11-23T12:00:00%2B09:00")
    }

    func testAllRepeatsEditSendsCronNotAt() {
        let b = ScheduleAPI.editBody(cron: "0 9 * * 1-5", at: nil, notify: "call", purpose: " 출근 ")
        XCTAssertEqual(b["cron"] as? String, "0 9 * * 1-5")
        XCTAssertNil(b["at"])
        XCTAssertEqual(b["purpose"] as? String, "출근")
    }
}
