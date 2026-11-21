import XCTest
@testable import Biseo

final class PlaybackGateTests: XCTestCase {
    func testPauseKeepsQueueResumeContinuesInterruptClears() {
        var g = PlaybackGate()
        XCTAssertTrue(g.playing)
        g.apply("pause")
        XCTAssertFalse(g.playing, "tentative barge-in stops playback")
        g.apply("resume")
        XCTAssertTrue(g.playing, "false alarm: continue where it stopped")
        g.apply("pause")
        g.apply("interrupt")
        XCTAssertTrue(g.playing, "a real interrupt drops the queue and clears the pause")
        g.apply("turn_done")
        XCTAssertTrue(g.playing)
    }

    func testHoldAndServerPauseAreIndependent() {
        var g = PlaybackGate()
        g.held = true
        g.apply("resume")
        XCTAssertFalse(g.playing, "a server resume doesn't end the user's hold")
        g.apply("pause")
        g.held = false
        XCTAssertFalse(g.playing, "unhold doesn't override the server's pause")
        g.apply("resume")
        XCTAssertTrue(g.playing)
    }

    func testEventsDecodeAndNoiseModeIsOptional() throws {
        XCTAssertEqual(try JSONDecoder().decode(CallEvent.self, from: Data(#"{"type":"pause"}"#.utf8)).type, "pause")
        let base = #"{"default_model":"auto","claude_path":"auto","task_defaults":{},"monthly_budget_usd":500,"budget_warn_pct":80,"default_device":"macbook","device_fallback":[],"proactive":{"morning_briefing":"08:00"}"#
        XCTAssertNil(try JSONDecoder().decode(AppSettings.self, from: Data((base + "}").utf8)).noise_mode)
        XCTAssertEqual(try JSONDecoder().decode(AppSettings.self, from: Data((base + #","noise_mode":"noisy"}"#).utf8)).noise_mode, "noisy")
    }
}
