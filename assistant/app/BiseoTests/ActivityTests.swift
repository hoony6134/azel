import XCTest
@testable import Biseo

final class ActivityTests: XCTestCase {
    private func decode(_ json: String) throws -> CallEvent { try JSONDecoder().decode(CallEvent.self, from: Data(json.utf8)) }

    func testDecodesActivityEvents() throws {
        let e = try decode(#"{"type":"activity","phase":"tool_start","label":"캘린더 확인 중","tool":"gcal_list","kind":"calendar","route":"claude"}"#)
        XCTAssertEqual(e.phase, "tool_start")
        XCTAssertEqual(e.label, "캘린더 확인 중")
        XCTAssertEqual(e.kind, "calendar")
        XCTAssertEqual(e.route, "claude")
    }

    func testTurnFlowAndCues() throws {
        var log = ActivityLog()
        XCTAssertEqual(log.apply(try decode(#"{"type":"activity","phase":"routing","label":"Claude에게 묻는 중"}"#)), .working)
        XCTAssertNil(log.apply(try decode(#"{"type":"activity","phase":"routing","label":"다시"}"#)), "working once per turn")
        XCTAssertNil(log.apply(try decode(#"{"type":"activity","phase":"tool_start","label":"캘린더 확인 중","kind":"calendar"}"#)))
        XCTAssertEqual(log.current, Activity(label: "캘린더 확인 중", glyph: "calendar"))
        XCTAssertEqual(log.apply(try decode(#"{"type":"activity","phase":"tool_end","kind":"calendar"}"#)), .tick)
        XCTAssertNil(log.current)
        XCTAssertEqual(log.done.map(\.label), ["캘린더 확인"])         // " 중" dropped
        XCTAssertNil(log.apply(try decode(#"{"type":"activity","phase":"tool_start","label":"노션 확인 중","kind":"notes"}"#)))
        XCTAssertEqual(log.apply(try decode(#"{"type":"activity","phase":"tool_end","label":"노션 3건 확인"}"#)), .tick)
        XCTAssertEqual(log.done.last?.label, "노션 3건 확인")          // the server's end label wins
        XCTAssertNil(log.apply(try decode(#"{"type":"activity","phase":"mystery","label":"?"}"#)), "unknown phases ignored")
        XCTAssertEqual(log.apply(try decode(#"{"type":"state","value":"speaking"}"#)), .done)
        XCTAssertTrue(log.speaking)
        XCTAssertEqual(log.done.count, 2, "trail stays while speaking")
        XCTAssertNil(log.apply(try decode(#"{"type":"state","value":"speaking"}"#)), "done once per turn")
        _ = log.apply(try decode(#"{"type":"transcript","text":"다음"}"#))
        XCTAssertTrue(log.isEmpty)
        XCTAssertNil(log.apply(try decode(#"{"type":"state","value":"speaking"}"#)), "qwen turn without activity: no done cue")
        XCTAssertEqual(log.apply(try decode(#"{"type":"error","message":"x"}"#)), .error)
    }

    func testDoneTrailKeepsLastEight() throws {
        var log = ActivityLog()
        for i in 0..<10 { _ = log.apply(try decode(#"{"type":"activity","phase":"tool_end","label":"step \#(i)"}"#)) }
        XCTAssertEqual(log.done.count, 8)
        XCTAssertEqual(log.done.first?.label, "step 2")
    }

    func testGlyphsByKindThenToolFallback() {
        XCTAssertEqual(Activity.glyph(kind: "schedule", tool: nil), "checklist")
        XCTAssertEqual(Activity.glyph(kind: "notes", tool: "whatever"), "note.text")
        XCTAssertEqual(Activity.glyph(kind: "nope", tool: "Gmail_search"), "envelope")
        XCTAssertEqual(Activity.glyph(kind: nil, tool: "cygnus_recall"), "brain")
        XCTAssertEqual(Activity.glyph(kind: nil, tool: "mystery"), "ellipsis.circle")
        XCTAssertEqual(Activity.glyph(kind: "terminal", tool: "run_command"), "apple.terminal")
    }
}

final class EarconTests: XCTestCase {
    func testCuesFitLengthPeakAndEndAtZero() {
        for cue in Cue.allCases {
            let s = Earcon.spec(cue)
            let samples = Earcon.render(cue)
            XCTAssertLessThanOrEqual(Double(samples.count) / Earcon.rate, s.maxLength + 1e-9, "\(cue) length")
            let peak = samples.map(abs).max()!
            XCTAssertEqual(20 * log10(Double(peak)), s.peakDB, accuracy: 0.5, "\(cue) peak")
            XCTAssertEqual(samples.last!, 0, accuracy: 1e-6, "\(cue) ends at zero")
            XCTAssertEqual(samples.first!, 0, accuracy: 1e-3, "\(cue) starts silent")
        }
    }

    func testGateNeverPlaysOverTheUserAndRateLimitsTicks() {
        var gate = EarconGate()
        // The user spoke 0.1 s ago while listening.
        XCTAssertEqual(gate.decide(.tick, now: 10, listening: true, userLoudAt: 9.9), .drop)
        XCTAssertEqual(gate.decide(.done, now: 10, listening: true, userLoudAt: 9.9), .wait)
        XCTAssertEqual(gate.decide(.done, now: 10.3, listening: true, userLoudAt: 10.2, waited: 0.3), .drop, "waits at most 300 ms")
        XCTAssertEqual(gate.decide(.wake, now: 10, listening: true, userLoudAt: 9.99), .play, "wake is exempt")
        // Quiet mic, or not listening: plays.
        XCTAssertEqual(gate.decide(.working, now: 20, listening: true, userLoudAt: 9.9), .play)
        XCTAssertEqual(gate.decide(.done, now: 20, listening: false, userLoudAt: 19.99), .play)
        // Ticks: one per 350 ms, extras dropped.
        XCTAssertEqual(gate.decide(.tick, now: 30, listening: false, userLoudAt: 0), .play)
        XCTAssertEqual(gate.decide(.tick, now: 30.2, listening: false, userLoudAt: 0), .drop)
        XCTAssertEqual(gate.decide(.tick, now: 30.36, listening: false, userLoudAt: 0), .play)
        // Toggle off: nothing.
        XCTAssertEqual(gate.decide(.wake, now: 40, listening: false, userLoudAt: 0, enabled: false), .drop)
    }

    func testWorkBoostEasesBack() {
        let t0 = Date()
        XCTAssertEqual(JarPath.workBoost(now: t0, surge: t0, pulse: nil).turns, 0)
        XCTAssertEqual(JarPath.workBoost(now: t0.addingTimeInterval(1), surge: t0, pulse: nil).turns, 0.35 * 0.2, accuracy: 1e-9)
        XCTAssertEqual(JarPath.workBoost(now: t0.addingTimeInterval(0.15), surge: nil, pulse: t0).glow, 0.25, accuracy: 1e-9)
        XCTAssertEqual(JarPath.workBoost(now: t0.addingTimeInterval(0.5), surge: nil, pulse: t0).glow, 0)
    }
}

final class ThinkingCueTests: XCTestCase {
    func testAckThenPulsesUntilReplyAudio() {
        var t = ThinkingCues()
        XCTAssertEqual(t.event("state", value: "thinking", now: 0), .ack)
        XCTAssertNil(t.event("state", value: "thinking", now: 0.5), "one ack per wait")
        XCTAssertNil(t.tick(now: 2.9), "no pulse in the first 3 s")
        XCTAssertEqual(t.tick(now: 3.0), .pulse)
        XCTAssertNil(t.tick(now: 4.5))
        XCTAssertEqual(t.tick(now: 6.0), .pulse, "every 3 s")
        t.audioArrived()
        XCTAssertNil(t.tick(now: 9.0), "the first reply audio stops it")
        XCTAssertFalse(t.active)
    }

    func testErrorsAndInterruptsStopThePulse() {
        for end in ["interrupt", "error", "turn_done"] {
            var t = ThinkingCues()
            _ = t.event("state", value: "thinking", now: 0)
            _ = t.event(end, value: nil, now: 1)
            XCTAssertNil(t.tick(now: 10), end)
        }
        var t = ThinkingCues()
        _ = t.event("state", value: "thinking", now: 0)
        _ = t.event("state", value: "speaking", now: 1)
        XCTAssertNil(t.tick(now: 10))
    }

    func testPulseNeverOverTheUserAndUnderTheToggle() {
        var gate = EarconGate()
        XCTAssertEqual(gate.decide(.pulse, now: 10, listening: true, userLoudAt: 9.9), .drop)
        XCTAssertEqual(gate.decide(.pulse, now: 10, listening: false, userLoudAt: 0), .play)
        XCTAssertEqual(gate.decide(.ack, now: 10, listening: true, userLoudAt: 9.9), .wait)
        XCTAssertEqual(gate.decide(.ack, now: 10, listening: false, userLoudAt: 0, enabled: false), .drop)
        for cue in [Cue.ack, .pulse] {
            let s = Earcon.render(cue)
            XCTAssertEqual(20 * log10(Double(s.map(abs).max()!)), Earcon.spec(cue).peakDB, accuracy: 0.5)
        }
    }
}

final class PulseScheduleTests: XCTestCase {
    func testPulseSlowsAfterThirtySecondsAndRestartsPerThinking() {
        var t = ThinkingCues()
        _ = t.event("state", value: "thinking", now: 0)
        var pulses: [Double] = []
        for i in 0...450 { let now = Double(i) / 10; if t.tick(now: now) == .pulse { pulses.append(now) } }
        XCTAssertEqual(pulses.first, 3)
        let early = zip(pulses, pulses.dropFirst()).filter { $0.1 <= 30 }.map { $1 - $0 }
        let late = zip(pulses, pulses.dropFirst()).filter { $0.1 > 30 }.map { $1 - $0 }
        XCTAssertTrue(early.allSatisfy { abs($0 - 3) < 0.11 }, "every 3 s at first")
        XCTAssertTrue(!late.isEmpty && late.allSatisfy { abs($0 - 6) < 0.11 }, "every 6 s after 30 s")
        _ = t.event("state", value: "speaking", now: 46)
        XCTAssertEqual(t.event("state", value: "thinking", now: 50), .ack)
        XCTAssertNil(t.tick(now: 52.9), "the clock restarts on each new thinking")
        XCTAssertEqual(t.tick(now: 53), .pulse)
    }

    func testPulseSkippedRightAfterAnotherCue() {
        var gate = EarconGate()
        XCTAssertEqual(gate.decide(.working, now: 10, listening: false, userLoudAt: 0), .play)
        XCTAssertEqual(gate.decide(.pulse, now: 11, listening: false, userLoudAt: 0), .drop, "within 1.5 s of working")
        XCTAssertEqual(gate.decide(.pulse, now: 11.6, listening: false, userLoudAt: 0), .play)
        XCTAssertEqual(gate.decide(.pulse, now: 12, listening: false, userLoudAt: 0), .play, "a pulse doesn't silence the next pulse")
    }
}
