import XCTest
@testable import Biseo

final class ScheduleTests: XCTestCase {
    func testDecodesEpochAndISOTimesAndDefaults() throws {
        let json = #"""
        [{"id":"w1","at":1763762400,"repeat":"weekdays","kind":"wake","note":"첫 수업","status":"pending","attempts":0},
         {"id":7,"at":"2026-11-22T09:00:00+09:00","kind":"reminder","note":"약 먹기","status":"missed","attempts":3},
         {"id":"r2","at":"2026-11-22T00:00:00.500Z"}]
        """#
        let items = try JSONDecoder().decode([ScheduledCall].self, from: Data(json.utf8))
        XCTAssertEqual(items[0].at.timeIntervalSince1970, 1_763_762_400)
        XCTAssertTrue(items[0].isWake)
        XCTAssertEqual(items[0].repeatRule, "weekdays")
        XCTAssertEqual(items[1].id, "7")
        XCTAssertEqual(items[1].at, items[2].at.addingTimeInterval(-0.5), "same instant written in two zones")
        XCTAssertEqual(items[2].kind, "reminder", "missing kind defaults to reminder")
        XCTAssertNil(items[2].status)
        // Encodes `at` as epoch seconds and `repeat` under its wire name.
        let out = String(decoding: try JSONEncoder().encode(items[0]), as: UTF8.self)
        XCTAssertTrue(out.contains(#""at":1763762400"#))
        XCTAssertTrue(out.contains(#""repeat":"weekdays""#))
    }

    func testKSTFormattingIgnoresDeviceZone() {
        // 2026-11-21 22:30 UTC = 2026-11-22 07:30 KST.
        let at = Date(timeIntervalSince1970: 1_763_764_200)
        XCTAssertEqual(KST.time(at), "오전 7:30")
        let sameKSTDay = Date(timeIntervalSince1970: 1_763_764_200 - 3600 * 5)   // 02:30 KST on the 22nd
        XCTAssertEqual(KST.dayAndTime(at, now: sameKSTDay), "오늘 오전 7:30")
        let dayBefore = Date(timeIntervalSince1970: 1_763_764_200 - 3600 * 12)   // 19:30 KST on the 21st
        XCTAssertEqual(KST.dayAndTime(at, now: dayBefore), "내일 오전 7:30")
        let weekBefore = Date(timeIntervalSince1970: 1_763_764_200 - 86_400 * 7)
        XCTAssertEqual(KST.dayAndTime(at, now: weekBefore), "11월 22일 (토) 오전 7:30")
        XCTAssertEqual(KST.time(Date(timeIntervalSince1970: 1_763_816_400)), "오후 10:00")  // 13:00 UTC
    }

    func testRepeatLabels() {
        XCTAssertNil(KST.repeatLabel(nil))
        XCTAssertNil(KST.repeatLabel("none"))
        XCTAssertEqual(KST.repeatLabel("weekdays"), "주중")
        XCTAssertEqual(KST.repeatLabel("monthly"), "monthly")
    }

    @MainActor func testUpcomingAndMissedSplit() {
        let m = ScheduleModel()
        XCTAssertNil(m.next)
        XCTAssertTrue(m.missed.isEmpty)
    }
}
