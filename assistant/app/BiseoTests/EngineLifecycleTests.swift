import XCTest
@testable import Biseo

final class EngineLifecycleTests: XCTestCase {
    /// Voice processing must not outlive a call: left on, it ducks other apps' audio.
    // macOS only: on iOS the engine starts after CallKit activates the session, which a test can't.
    #if os(macOS)
    func testVoiceProcessingOnlyWhileTheCallRuns() throws {
        let audio = AudioIO()
        XCTAssertFalse(audio.isRunning, "an idle app holds no engine or mic")
        try audio.start()
        XCTAssertTrue(audio.isRunning)
        XCTAssertTrue(audio.voiceProcessingOn)
        audio.stop()
        XCTAssertFalse(audio.isRunning)
        XCTAssertFalse(audio.voiceProcessingOn)
    }

    /// Hang-up while a reply and a cue play, with the ducking timer running (Mac hang, 2026-11-22:
    /// a tap touched cuePlayer.volume while stop() held the engine lock). Hangs if it regresses.
    func testStopWhileSpeakingNeverDeadlocks() throws {
        let audio = AudioIO()
        let speech = Data(count: 24_000 * 2) // 1 s of silence as PCM16
        for _ in 0..<8 {
            try audio.start()
            audio.play(speech)
            audio.playCue(.working)
            RunLoop.main.run(until: Date().addingTimeInterval(0.15)) // taps and the ducking timer fire
            XCTAssertTrue(audio.speechPlaying)
            audio.stop()
            audio.stop()                                              // idempotent
            XCTAssertFalse(audio.isRunning)
        }
    }
    #endif

    func testDuckingRampsDownFastAndBackSlowly() {
        var v: Float = 1
        for _ in 0..<3 { v = AudioIO.duck(volume: v, speech: true, dt: 1.0 / 30) }
        XCTAssertEqual(v, 0.4, accuracy: 0.01, "down within ~100 ms")
        v = AudioIO.duck(volume: v, speech: false, dt: 1.0 / 30)
        XCTAssertLessThan(v, 0.9, "back up more slowly")
        for _ in 0..<30 { v = AudioIO.duck(volume: v, speech: false, dt: 1.0 / 30) }
        XCTAssertEqual(v, 1, accuracy: 0.01)
    }
}
