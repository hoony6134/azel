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
    #endif
}
