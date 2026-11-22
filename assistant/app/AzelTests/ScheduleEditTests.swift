import XCTest
@testable import Azel

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

    func testThisOnlyEditGoesToTheOccurrenceRoute() async throws {
        let item = try JSONDecoder().decode(ScheduledCall.self, from: Data(newItem.utf8))
        FakeServer.routes = ["PUT /v1/schedule/a1": (200, #"{"ok":true,"spoken":"오늘 오후 12시 30분으로 바꿨어요.","data":{},"needs":null,"error":null}"#)]
        let body = ScheduleAPI.editBody(scope: "occurrence", when: item.when.addingTimeInterval(1800), repeatCron: nil, notify: "push", purpose: "물 마시기")
        let r = try await ScheduleAPI.update(item.id, occurrence: item.when, body: body)
        XCTAssertTrue(r.ok)
        XCTAssertEqual(r.spoken, "오늘 오후 12시 30분으로 바꿨어요.")
        let put = try XCTUnwrap(FakeServer.seen.first { $0.method == "PUT" })
        let sent = try JSONSerialization.jsonObject(with: put.body) as! [String: Any]
        XCTAssertEqual(sent["when"] as? String, "2026-11-23T12:30:00+09:00")
        XCTAssertEqual(sent["scope"] as? String, "occurrence")
        XCTAssertNil(sent["repeat_cron"])
        XCTAssertFalse(FakeServer.seen.contains { $0.method == "POST" || $0.method == "DELETE" }, "the server does the skip and the one-off")
    }

    func testNeedsAndActionErrors() async throws {
        let confirm = ActionResult(Data(#"{"ok":false,"spoken":"x","needs":{"kind":"confirm","question":"평일 2시간마다 전화드릴게요. 맞죠?"},"error":null}"#.utf8))
        XCTAssertEqual(confirm.needs?.kind, "confirm")
        XCTAssertFalse(confirm.ok)
        let choose = ActionResult(Data(#"{"ok":false,"needs":{"kind":"choose","question":"골라 주세요","options":[{"id":7,"spoken":"오후 10시 약"}]}}"#.utf8))
        XCTAssertEqual(choose.needs?.options, [.init(id: "7", spoken: "오후 10시 약")])
        XCTAssertNil(ActionResult(Data(#"{"id":"a1"}"#.utf8)).needs, "an old route's JSON is just ok")
        FakeServer.routes = ["PUT /v1/schedule/a1": (429, #"{"detail":"하루 20건까지예요","ok":false,"spoken":"하루 20건까지예요","error":{"code":"limit","message":"하루 20건까지예요"}}"#)]
        do {
            _ = try await ScheduleAPI.update("a1", occurrence: nil, body: [:])
            XCTFail("expected a limit error")
        } catch GatewayError.rejected(let m) { XCTAssertEqual(m, "하루 20건까지예요") }
    }

    func testCreateUsesCallUserShape() {
        let at = ISO8601DateFormatter().date(from: "2026-11-24T22:00:00Z")!   // 07:00 KST on the 25th
        let once = ScheduleAPI.actionBody(at: at, repeatRule: nil, kind: "wake", note: "첫 수업", persona: nil)
        XCTAssertEqual(once["when"] as? String, "2026-11-25T07:00:00+09:00")
        XCTAssertEqual(once["purpose"] as? String, "첫 수업")
        XCTAssertNil(once["repeat_cron"])
        let weekly = ScheduleAPI.actionBody(at: at, repeatRule: "weekly:2", kind: "wake", note: "", persona: "p1")
        XCTAssertEqual(weekly["repeat_cron"] as? String, "0 7 * * 3", "weekly:2 = Wednesday = cron 3")
        XCTAssertEqual(weekly["start_date"] as? String, "2026-11-25")
        XCTAssertNil(weekly["when"])
    }

    func testOccurrenceQueryKeepsThePlus() {
        XCTAssertEqual(ScheduleAPI.query("2026-11-23T12:00:00+09:00"), "2026-11-23T12:00:00%2B09:00")
    }

    func testAllRepeatsEditSendsCronNotAt() {
        let b = ScheduleAPI.editBody(scope: "rule", when: nil, repeatCron: "0 9 * * 1-5", notify: "call", purpose: " 출근 ")
        XCTAssertEqual(b["repeat_cron"] as? String, "0 9 * * 1-5")
        XCTAssertEqual(b["scope"] as? String, "rule")
        XCTAssertNil(b["when"])
        XCTAssertEqual(b["purpose"] as? String, "출근")
    }
}

final class PushActionTests: XCTestCase {
    func testButtonsMapToTheActionRoutes() {
        let info: [AnyHashable: Any] = ["schedule_id": "a1", "occurrence": "2026-11-23T12:00:00+09:00"]
        XCTAssertEqual(PushAction.request(action: "SNOOZE_10", userInfo: info),
                       .init(method: "PUT", path: "/v1/schedule/a1?occurrence=2026-11-23T12:00:00%2B09:00", body: ["when_text": "10분 뒤", "scope": "occurrence"]))
        XCTAssertEqual(PushAction.request(action: "SKIP", userInfo: info)?.method, "DELETE")
        XCTAssertEqual(PushAction.request(action: "STOP", userInfo: ["task_id": "t9"]), .init(method: "POST", path: "/v1/tasks/t9/cancel"))
        XCTAssertNil(PushAction.request(action: "SKIP", userInfo: ["schedule_id": "a1"]), "no occurrence: do nothing")
        XCTAssertEqual(Set(PushAction.categories.map(\.identifier)), ["SCHEDULE", "TASK", "APPROVAL", "APPROVAL_ONCE", "APPROVAL_RISKY"])
    }
}
