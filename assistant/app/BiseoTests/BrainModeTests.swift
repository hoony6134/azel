import XCTest
@testable import Biseo

@MainActor
final class BrainModeTests: XCTestCase {
    func testChipsHintsAndSettingsGuard() throws {
        XCTAssertEqual(BrainMode.current(nil), .auto, "no report yet: 자동")
        XCTAssertEqual(BrainMode.current("local").short, "로컬")
        XCTAssertEqual(BrainMode.current("claude_code").short, "Claude Code")
        XCTAssertEqual(BrainMode.current("bogus"), .auto)
        XCTAssertEqual(BrainMode.local.hint, "Claude를 쓰지 않아요, 무료")
        XCTAssertTrue(BrainModeChoice.reports(Data(#"{"brain_mode":"auto"}"#.utf8)))
        XCTAssertFalse(BrainModeChoice.reports(Data(#"{"default_model":"auto"}"#.utf8)), "old gateway: pickers hidden")
        let base = #"{"default_model":"auto","claude_path":"auto","task_defaults":{},"monthly_budget_usd":500,"budget_warn_pct":80,"default_device":"m","device_fallback":[],"proactive":{"morning_briefing":"08:00"}}"#
        let old = try JSONDecoder().decode(AppSettings.self, from: Data(base.utf8))
        XCTAssertFalse(String(decoding: try JSONEncoder().encode(old), as: UTF8.self).contains("brain_mode"), "never sent to an old gateway")
    }

    func testNextCallChoiceIsTakenOnce() {
        let c = BrainModeChoice.shared
        c.next = .local
        XCTAssertEqual(c.takeForCall(), "local")
        XCTAssertNil(c.takeForCall(), "the call after uses the default again")
    }

    func testModeOnTheURLAndFromEvents() throws {
        let req = try CallClient.request(voiceURL: "wss://azel.example", token: "t", session: "s", callID: nil, mode: "claude_api")
        XCTAssertTrue(req.url!.absoluteString.contains("mode=claude_api"))
        let noMode = try CallClient.request(voiceURL: "wss://azel.example", token: "t", session: "s", callID: nil)
        XCTAssertFalse(noMode.url!.absoluteString.contains("mode="))
        let dec = JSONDecoder()
        XCTAssertEqual(CallClient.reportedMode(try dec.decode(CallEvent.self, from: Data(#"{"type":"state","value":"listening","mode":"local"}"#.utf8))), "local")
        XCTAssertEqual(CallClient.reportedMode(try dec.decode(CallEvent.self, from: Data(#"{"type":"mode","value":"claude_code"}"#.utf8))), "claude_code")
        XCTAssertNil(CallClient.reportedMode(try dec.decode(CallEvent.self, from: Data(#"{"type":"state","value":"thinking"}"#.utf8))))
    }
}

final class BrainModeFrameTests: XCTestCase {
    /// The confirmed contract (brain, claude/brain-next): a mode frame right after connect and after
    /// each accepted change, and a `mode` key on every state frame.
    func testConfirmedFrames() throws {
        let d = JSONDecoder()
        XCTAssertEqual(CallClient.reportedMode(try d.decode(CallEvent.self, from: Data(#"{"type":"mode","value":"local"}"#.utf8))), "local")
        XCTAssertEqual(CallClient.reportedMode(try d.decode(CallEvent.self, from: Data(#"{"type":"state","value":"listening","mode":"claude_api"}"#.utf8))), "claude_api")
        let req = try CallClient.request(voiceURL: "wss://azel.example", token: "t", session: "s", callID: nil, persona: "p1", mode: "local")
        let items = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)!.queryItems!
        XCTAssertEqual(items.first { $0.name == "mode" }?.value, "local")
        XCTAssertEqual(items.first { $0.name == "persona" }?.value, "p1")
    }
}

final class TypedTurnTests: XCTestCase {
    func testTypedLineShowsOnceEvenIfEchoed() throws {
        var log = CallLog()
        log.typed("내일 일정 알려줘")
        let echo = try JSONDecoder().decode(CallEvent.self, from: Data(#"{"type":"transcript","text":"내일 일정 알려줘"}"#.utf8))
        log.apply(echo)
        XCTAssertEqual(log.lines.count, 1, "the server's echo doesn't duplicate it")
        XCTAssertEqual(log.lines[0].typed, true)
        XCTAssertTrue(log.lines[0].mine)
        log.apply(try JSONDecoder().decode(CallEvent.self, from: Data(#"{"type":"transcript","text":"고마워"}"#.utf8)))
        XCTAssertEqual(log.lines.count, 2)
        XCTAssertNil(log.lines[1].typed, "spoken lines carry no keyboard mark")
    }

    func testSavedLinesWithoutTheFieldStillDecode() throws {
        let line = try JSONDecoder().decode(CallLine.self, from: Data(#"{"id":"6F0E2B8A-1C3D-4E5F-8A9B-0C1D2E3F4A5B","mine":true,"text":"x","interrupted":false}"#.utf8))
        XCTAssertNil(line.typed)
    }

    func testCapsFrameEnablesTyping() throws {
        let hello = try JSONDecoder().decode(CallEvent.self, from: Data(#"{"type":"hello","caps":["text","mode","played"]}"#.utf8))
        XCTAssertEqual(CallClient.caps(hello)?.contains("text"), true)
        let state = try JSONDecoder().decode(CallEvent.self, from: Data(#"{"type":"state","value":"listening"}"#.utf8))
        XCTAssertNil(CallClient.caps(state), "no caps: keep what we had")
    }

    #if os(macOS)
    @MainActor func testPanelKeysOpenTheFieldOnlyWhenTypingIsSupported() {
        let call = MacCall.shared
        XCTAssertFalse(call.typeKey("a"), "server without text: keys pass through")
        XCTAssertEqual(MacCall.panelHeight(typing: false), 112)
        XCTAssertGreaterThan(MacCall.panelHeight(typing: true), 112)
    }
    #endif
}

final class TypedRejectionTests: XCTestCase {
    func testRejectedTypedLineLeavesTheCaptions() throws {
        var log = CallLog()
        log.typed("a")
        log.apply(try JSONDecoder().decode(CallEvent.self, from: Data(#"{"type":"text_rejected","reason":"too_fast"}"#.utf8)))
        XCTAssertTrue(log.lines.isEmpty)
        XCTAssertEqual(CallLog.rejection("too_long"), "1000자까지 보낼 수 있어요")
        XCTAssertEqual(CallLog.rejection("too_fast"), "조금 천천히 보내 주세요")
    }

    /// The confirmed order: hello, then context, then mode; context needn't be first.
    func testFramesInTheConfirmedOrder() throws {
        let d = JSONDecoder()
        let hello = try d.decode(CallEvent.self, from: Data(#"{"type":"hello","caps":["text","played","mode"]}"#.utf8))
        let ctx = try d.decode(CallEvent.self, from: Data(#"{"type":"context","context":{"kind":"schedule","label":"약 먹기","id":"s1"}}"#.utf8))
        XCTAssertEqual(CallClient.caps(hello), ["text", "played", "mode"])
        XCTAssertEqual(CallContext.from(event: ctx)?.chip, "예약: 약 먹기")
    }
}
