import XCTest
@testable import Biseo

final class LegacyTests: XCTestCase {
    func testInterruptedMarkerIsNeverShown() throws {
        XCTAssertNil(LegacyText.clean("(말이 끊김)"))
        XCTAssertNil(LegacyText.clean("  (말이 끊김) "))
        XCTAssertEqual(LegacyText.clean("내일은 회의가 (말이 끊김)"), "내일은 회의가")
        XCTAssertEqual(LegacyText.clean("보통 문장"), "보통 문장")

        let json = #"{"id":"c","started_at":0,"turns":[{"user":"일정?","assistant":["내일은","(말이 끊김)"],"interrupted":true},{"user":"(말이 끊김)","assistant":"(말이 끊김)","interrupted":true}]}"#
        let call = try JSONDecoder().decode(HistoryCall.self, from: Data(json.utf8))
        XCTAssertEqual(call.turns[0].assistant, "내일은")
        XCTAssertNil(call.turns[1].assistant)
        XCTAssertNil(call.turns[1].user)
        XCTAssertEqual(call.turns[1].interrupted, true, "the turn keeps its 끊김 mark")
        let item = try JSONDecoder().decode(HistoryItem.self, from: Data(#"{"id":"i","started_at":0,"last_reply":"(말이 끊김)"}"#.utf8))
        XCTAssertNil(item.last_reply)
    }

    func testLiveCaptionMarkerMarksTheReplyInstead() {
        var log = CallLog()
        log.apply(CallEvent(type: "transcript", text: "일정?"))
        log.apply(CallEvent(type: "reply", text: "내일은", route: "qwen"))
        log.apply(CallEvent(type: "reply", text: "(말이 끊김)", route: "qwen"))
        XCTAssertEqual(log.lines.map(\.text), ["일정?", "내일은"])
        XCTAssertTrue(log.lines[1].interrupted)
    }

    func testCallDirectionFromTheUsersSide() {
        // incoming = the assistant called the user; outgoing = the user called.
        let incoming = HistoryItem(id: "a", started_at: 0, direction: "incoming")
        let outgoing = HistoryItem(id: "b", started_at: 0, direction: "outgoing")
        XCTAssertTrue(incoming.incoming)
        XCTAssertFalse(outgoing.incoming)
        XCTAssertEqual(HistoryFormat.directionGlyph(incoming: true), "phone.arrow.down.left")
        XCTAssertEqual(HistoryFormat.directionLabel(incoming: true), "받은 전화")
        XCTAssertEqual(HistoryFormat.directionGlyph(incoming: false), "phone.arrow.up.right")
        XCTAssertEqual(HistoryFormat.directionLabel(incoming: false), "건 전화")
    }

    func testReplyLengthAndServerDefaults() throws {
        let base = #"{"default_model":"auto","claude_path":"auto","task_defaults":{},"monthly_budget_usd":500,"budget_warn_pct":80,"default_device":"macbook","device_fallback":[],"proactive":{"morning_briefing":"08:00"}"#
        XCTAssertNil(try JSONDecoder().decode(AppSettings.self, from: Data((base + "}").utf8)).reply_length)
        let s = try JSONDecoder().decode(AppSettings.self, from: Data((base + #","reply_length":"brief"}"#).utf8))
        XCTAssertEqual(s.reply_length, "brief")
        XCTAssertEqual(s.proactive.deadline_check.time, "20:00")
        XCTAssertEqual(s.proactive.autonomous, .init(enabled: false, max_per_day: 3, min_gap_minutes: 120, window: ["08:00", "23:00"]))
    }
}
