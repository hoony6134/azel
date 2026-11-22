import XCTest
@testable import Azel

final class ScheduleTests: XCTestCase {
    func testDecodesContractItemsAndEpochFallback() throws {
        let json = #"""
        [{"id":"w1","at":"2026-11-22T10:00:00+09:00","repeat":"weekly:0","kind":"wake","note":"첫 수업","status":"fired","attempts":1,"created_by":"voice"},
         {"id":7,"at":"2026-11-22T01:00:00Z","repeat":null,"kind":"reminder","status":"missed","attempts":3,"created_by":"app"},
         {"id":"old","at":1795309200,"kind":"wake"}]
        """#
        let items = try JSONDecoder().decode([ScheduledCall].self, from: Data(json.utf8))
        XCTAssertEqual(items[0].at, items[1].at, "same instant, +09:00 and Z")
        XCTAssertEqual(items[2].at, items[0].at, "epoch seconds still read")
        XCTAssertTrue(items[0].byVoice)
        XCTAssertTrue(items[0].isActive, "fired counts as active")
        XCTAssertFalse(items[1].isActive)
        XCTAssertNil(items[1].repeatRule)
        XCTAssertEqual(items[1].id, "7")
    }

    func testCreateBodyMatchesContract() throws {
        let at = Date(timeIntervalSince1970: 1_795_309_200) // 2026-11-22 10:00 KST
        let once = ScheduleAPI.body(at: at, repeatRule: nil, kind: "wake", note: "  ")
        XCTAssertEqual(once["at"] as? String, "2026-11-22T10:00:00+09:00")
        XCTAssertTrue(once["repeat"] is NSNull, "none is sent as null")
        XCTAssertNil(once["note"], "blank note omitted")
        let data = try JSONSerialization.data(withJSONObject: once)
        XCTAssertTrue(String(decoding: data, as: UTF8.self).contains(#""repeat":null"#))
        let weekly = ScheduleAPI.body(at: at, repeatRule: "weekly:5", kind: "reminder", note: "약")
        XCTAssertEqual(weekly["repeat"] as? String, "weekly:5")
        XCTAssertEqual(weekly["note"] as? String, "약")
    }

    func testKSTFormattingIgnoresDeviceZone() {
        // 2026-11-21 22:30 UTC = 2026-11-22 07:30 KST (a Sunday).
        let at = Date(timeIntervalSince1970: 1_795_300_200)
        XCTAssertEqual(KST.time(at), "오전 7:30")
        XCTAssertEqual(KST.iso(at), "2026-11-22T07:30:00+09:00")
        XCTAssertEqual(KST.dow(at), 6, "Sunday, 0 = Monday")
        XCTAssertEqual(KST.dayAndTime(at, now: at.addingTimeInterval(-3600 * 5)), "오늘 오전 7:30")
        XCTAssertEqual(KST.dayAndTime(at, now: at.addingTimeInterval(-3600 * 12)), "내일 오전 7:30")
        XCTAssertEqual(KST.dayAndTime(at, now: at.addingTimeInterval(-86_400 * 7)), "11월 22일 (일) 오전 7:30")
        XCTAssertEqual(KST.time(Date(timeIntervalSince1970: 1_795_352_400)), "오후 10:00")  // 13:00 UTC
    }

    func testRepeatAndStatusLabels() {
        XCTAssertNil(KST.repeatLabel(nil))
        XCTAssertEqual(KST.repeatLabel("weekdays"), "주중")
        XCTAssertEqual(KST.repeatLabel("weekly:0"), "매주 월요일")
        XCTAssertEqual(KST.repeatLabel("weekly:6"), "매주 일요일")
        XCTAssertEqual(KST.repeatLabel("weekly:9"), "매주")
        XCTAssertEqual(KST.statusLabel("fired"), "거는 중")
        XCTAssertEqual(KST.statusLabel("cancelled"), "취소됨")
        XCTAssertEqual(KST.statusLabel(nil), "예정")
    }

    func testServerRejectionMessageIsReadable() {
        XCTAssertEqual(GatewayError.message(in: Data(#"{"detail":"30일 넘게 남은 시각은 예약할 수 없어요"}"#.utf8)), "30일 넘게 남은 시각은 예약할 수 없어요")
        XCTAssertEqual(GatewayError.rejected("과거 시각이에요").errorDescription, "과거 시각이에요")
        XCTAssertNil(GatewayError.message(in: Data("not json".utf8)))
    }
}
