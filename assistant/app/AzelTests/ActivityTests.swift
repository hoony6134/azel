import XCTest
@testable import Azel

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
        XCTAssertNil(t.tick(now: 0.9), "no heartbeat in the first second")
        XCTAssertEqual(t.tick(now: 1.0), .pulse)
        XCTAssertNil(t.tick(now: 2.0))
        XCTAssertEqual(t.tick(now: 2.2), .pulse, "every 1.2 s")
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
        XCTAssertEqual(pulses.first, 1)
        let early = zip(pulses, pulses.dropFirst()).filter { $0.1 <= 30 }.map { $1 - $0 }
        let late = zip(pulses, pulses.dropFirst()).filter { $0.1 > 30 }.map { $1 - $0 }
        XCTAssertTrue(early.allSatisfy { abs($0 - 1.2) < 0.11 }, "every 1.2 s at first")
        XCTAssertTrue(!late.isEmpty && late.allSatisfy { abs($0 - 2.4) < 0.11 }, "every 2.4 s after 30 s")
        _ = t.event("state", value: "speaking", now: 46)
        XCTAssertEqual(t.event("state", value: "thinking", now: 50), .ack)
        XCTAssertNil(t.tick(now: 50.9), "the clock restarts on each new thinking")
        XCTAssertEqual(t.tick(now: 51), .pulse)
    }

    func testPulseSkippedRightAfterAnotherCue() {
        var gate = EarconGate()
        XCTAssertEqual(gate.decide(.working, now: 10, listening: false, userLoudAt: 0), .play)
        XCTAssertEqual(gate.decide(.pulse, now: 10.5, listening: false, userLoudAt: 0), .drop, "within 0.7 s of working")
        XCTAssertEqual(gate.decide(.pulse, now: 10.8, listening: false, userLoudAt: 0), .play)
        XCTAssertEqual(gate.decide(.pulse, now: 12, listening: false, userLoudAt: 0), .play, "a pulse doesn't silence the next pulse")
    }
}

/// User, 2026-11-23: the heartbeat also fills the silences mid-reply.
final class MidReplyHeartbeatTests: XCTestCase {
    /// thinking → first audio → the queue runs dry while the next sentence is generated.
    private func replying() -> ThinkingCues {
        var t = ThinkingCues()
        _ = t.event("state", value: "thinking", now: 0)
        _ = t.event("state", value: "speaking", now: 2)
        t.audioArrived()
        t.playback(true, now: 2)
        return t
    }

    func testGapAfterAudioBeats() {
        var t = replying()
        XCTAssertNil(t.tick(now: 5), "silent while the reply plays")
        t.playback(false, now: 6)                         // queue ran dry, turn not done
        XCTAssertNil(t.tick(now: 6.9), "not in the first second of silence")
        XCTAssertEqual(t.tick(now: 7.0), .pulse)
        XCTAssertNil(t.tick(now: 7.5))
        XCTAssertEqual(t.tick(now: 8.2), .pulse, "every 1.2 s")
    }

    func testAudioResumingStopsIt() {
        var t = replying()
        t.playback(false, now: 6)
        XCTAssertEqual(t.tick(now: 7), .pulse)
        t.audioArrived(); t.playback(true, now: 7.3)       // the next sentence
        XCTAssertNil(t.tick(now: 9))
        XCTAssertFalse(t.active)
        t.playback(false, now: 10)                         // dry again: a new wait, its own first second
        XCTAssertNil(t.tick(now: 10.9))
        XCTAssertEqual(t.tick(now: 11), .pulse)
    }

    /// Mid-reply, nothing playing, a tool starts: the ack at once and the thinking look at once.
    func testAToolStartingInADryGapAcksAtOnce() {
        var t = replying()
        t.playback(false, now: 5)                          // the queue ran dry
        XCTAssertEqual(t.event("activity", value: nil, now: 5.2, phase: "tool_start"), .ack)
        XCTAssertTrue(t.waiting(now: 5.25), "a running tool shows thinking without the 1 s delay")
        XCTAssertNil(t.event("activity", value: nil, now: 5.4, phase: "tool_start"), "one ack per wait")
        var p = replying()                                  // audio still playing: no ack over the reply
        XCTAssertNil(p.event("activity", value: nil, now: 4, phase: "tool_start"))
        var f = ThinkingCues()
        XCTAssertEqual(f.event("state", value: "thinking", now: 0), .ack)
        XCTAssertNil(f.event("activity", value: nil, now: 0.3, phase: "tool_start"), "no second ack right after thinking")
    }

    func testAToolRunningMidReplyIsAWait() {
        var t = replying()
        XCTAssertNil(t.event("activity", value: nil, now: 4, phase: "tool_start"))
        XCTAssertEqual(t.tools, 1)
        XCTAssertNil(t.tick(now: 5), "still playing: no beat over the reply")
        t.playback(false, now: 5)
        XCTAssertEqual(t.tick(now: 6), .pulse, "the tool runs after the audio drained")
        _ = t.event("activity", value: nil, now: 7, phase: "tool_end")
        XCTAssertEqual(t.tools, 0)
        // A tool before any thinking state still opens a wait.
        var u = ThinkingCues()
        _ = u.event("activity", value: nil, now: 0, phase: "tool_start")
        XCTAssertEqual(u.tick(now: 1), .pulse)
    }

    /// The real order (2026-11-23 Mac call): thinking, then the user's transcript ~0.25 s later, then
    /// tools and speech seconds later. The transcript must not end the wait: it silenced every heartbeat.
    func testTranscriptAfterThinkingKeepsTheHeartbeat() {
        var t = ThinkingCues()
        XCTAssertEqual(t.event("state", value: "thinking", now: 0), .ack)
        _ = t.event("transcript", value: nil, now: 0.25)
        XCTAssertFalse(t.waiting(now: 0.5), "sub-second: no thinking flicker")
        XCTAssertEqual(t.tick(now: 1.0), .pulse)
        XCTAssertTrue(t.waiting(now: 1.0))
        XCTAssertEqual(t.tick(now: 2.2), .pulse)
        _ = t.event("state", value: "speaking", now: 5)
        t.audioArrived()
        XCTAssertFalse(t.waiting(now: 5.1))
    }

    /// Never a wait while the server listens (2026-11-23 Mac call: 생각 중 and the heartbeat while the
    /// user was meant to talk). A full turn, then everything that can arrive after `listening`.
    func testNothingWaitsWhileTheServerListens() {
        var t = ThinkingCues()
        _ = t.event("state", value: "thinking", now: 0)
        _ = t.event("transcript", value: nil, now: 0.2)
        _ = t.event("activity", value: nil, now: 0.5, phase: "tool_start")
        _ = t.event("activity", value: nil, now: 1.5, phase: "tool_end")
        _ = t.event("state", value: "speaking", now: 2)
        t.audioArrived(); t.playback(true, now: 2)
        _ = t.event("state", value: "listening", now: 4)        // the server moves on before the audio ends
        _ = t.event("turn_done", value: nil, now: 4)
        t.playback(false, now: 5)                                // the queue drains after listening
        _ = t.event("activity", value: nil, now: 5.5, phase: "tool_start")   // a background step
        _ = t.event("activity", value: nil, now: 6, phase: "tool_end")
        _ = t.event("resume", value: nil, now: 6.5)
        _ = t.event("unduck", value: nil, now: 7)
        for now in stride(from: 5.0, through: 15, by: 0.1) {
            XCTAssertNil(t.tick(now: now), "no heartbeat while listening (\(now))")
            XCTAssertFalse(t.waiting(now: now))
        }
        XCTAssertEqual(CallClient.displayState(server: "listening", playing: false, waiting: true), "listening")
        XCTAssertEqual(t.event("state", value: "thinking", now: 20), .ack, "the next turn waits again")
        XCTAssertEqual(t.tick(now: 21), .pulse)
    }

    func testFeltWaitsShowThinkingButPlayingAudioWins() {
        XCTAssertEqual(CallClient.displayState(server: "speaking", playing: false, waiting: true), "thinking", "mid-reply gap")
        XCTAssertEqual(CallClient.displayState(server: "speaking", playing: true, waiting: true), "speaking")
        XCTAssertEqual(CallClient.displayState(server: "listening", playing: true), "speaking")
        XCTAssertEqual(CallClient.displayState(server: "speaking", playing: false), "speaking")
    }

    /// Small speakers: the heartbeat's fundamental must be one they play (≥ 200 Hz), and both cues loud enough to hear.
    func testAckAndPulseAreAudibleOnSmallSpeakers() {
        XCTAssertGreaterThanOrEqual(Earcon.spec(.pulse).notes.map(\.freq).min()!, 190)
        XCTAssertGreaterThanOrEqual(Earcon.spec(.pulse).peakDB, -30)
        XCTAssertGreaterThanOrEqual(Earcon.spec(.ack).peakDB, -26)
    }

    func testTurnEndsAndPausesStopIt() {
        for end in ["turn_done", "interrupt", "error", "hangup"] {
            var t = replying()
            t.playback(false, now: 6)
            _ = t.event(end, value: nil, now: 6.5)
            XCTAssertNil(t.tick(now: 9), end)
            t.playback(false, now: 10)                     // a later drain doesn't restart a closed turn
            XCTAssertNil(t.tick(now: 12), end)
        }
        var l = replying()
        l.playback(false, now: 6)
        _ = l.event("state", value: "listening", now: 6.2)
        XCTAssertNil(l.tick(now: 9), "listening")
        var p = replying()
        p.playback(false, now: 6)
        _ = p.event("pause", value: nil, now: 6.5)          // the user may be talking
        XCTAssertNil(p.tick(now: 9))
        _ = p.event("resume", value: nil, now: 9)
        XCTAssertEqual(p.tick(now: 10), .pulse, "back after resume, its own first second")
        var d = replying()
        d.playback(false, now: 6)
        _ = d.event("duck", value: nil, now: 6.5)
        XCTAssertNil(d.tick(now: 9), "duck counts as a pause")
    }
}
