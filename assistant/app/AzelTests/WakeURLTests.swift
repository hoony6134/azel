import XCTest
@testable import Azel

#if os(macOS)
final class WakeURLTests: XCTestCase {
    private var saved: URL!
    private var dir: URL!

    override func setUp() {
        saved = AzelFiles.folder
        dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        AzelFiles.folder = dir.appendingPathComponent("Azel")
    }

    override func tearDown() {
        AzelFiles.folder = saved
        try? FileManager.default.removeItem(at: dir)
    }

    func testParsesTheListenersURL() {
        XCTAssertEqual(WakeURL.parse(URL(string: "azel://wake?persona=azel&utterance=u_42")!), .init(persona: "azel", utterance: "u_42"))
        XCTAssertEqual(WakeURL.parse(URL(string: "azel://wake")!), .init(persona: nil, utterance: nil), "no persona: the primary")
        XCTAssertEqual(WakeURL.parse(URL(string: "azel://wake?utterance=../../etc")!)?.utterance, nil, "never a path")
        XCTAssertNil(WakeURL.parse(URL(string: "azel://task/abc")!), "other azel URLs aren't a wake")
        XCTAssertNil(WakeURL.parse(URL(string: "https://wake?persona=x")!))
    }

    func testMarkerLifecycle() {
        AzelFiles.clearMarkers()                                   // launch: nothing there, no error
        AzelFiles.inCall.create()                                  // creates the folder too
        AzelFiles.micLive.create()
        AzelFiles.enrolling.create()
        XCTAssertTrue(AzelFiles.inCall.exists && AzelFiles.micLive.exists && AzelFiles.enrolling.exists)
        AzelFiles.inCall.create()                                  // idempotent
        AzelFiles.clearMarkers()                                   // quit / next launch
        XCTAssertFalse(AzelFiles.inCall.exists || AzelFiles.micLive.exists || AzelFiles.enrolling.exists)
    }

    func testMarkersLiveInTheRealHomeNotTheContainer() {
        XCTAssertFalse(saved.path.contains("/Library/Containers/"), saved.path)
        XCTAssertTrue(saved.path.hasSuffix("/Library/Application Support/Azel"))
    }

    /// Voice processing starts only once the listener closed its stream (2026-11-23: 3 s main-thread
    /// stall while both held the mic); a leftover marker delays a call by `limit`, never forever.
    func testWaitsForTheListenerToReleaseTheMic() async throws {
        let none = await AzelFiles.listenerReleased(limit: 0.2)
        XCTAssertTrue(none, "no listener: no wait")
        AzelFiles.listenerMic.create()
        Task {
            try? await Task.sleep(nanoseconds: 300_000_000)
            AzelFiles.listenerMic.remove()                                  // the listener closed its stream
        }
        let t0 = Date()
        let released = await AzelFiles.listenerReleased(limit: 2)
        XCTAssertTrue(released)
        XCTAssertGreaterThan(Date().timeIntervalSince(t0), 0.25, "waited for the release, not a fixed sleep")
        AzelFiles.listenerMic.create()
        let stuck = await AzelFiles.listenerReleased(limit: 0.2)
        XCTAssertFalse(stuck, "a leftover marker gives up at the limit")
        AzelFiles.clearMarkers()
        XCTAssertTrue(AzelFiles.listenerMic.exists, "the listener's marker is the listener's to remove")
    }

    /// The live stream: bytes are forwarded as the listener appends them (not once at the end), whole samples
    /// only, until `.done`; the end time is the seam for our own frames; all files are removed afterwards.
    @MainActor func testStreamIsForwardedAsItGrows() async throws {
        try FileManager.default.createDirectory(at: AzelFiles.folder, withIntermediateDirectories: true)
        let url = AzelFiles.stream("s1")
        try Data().write(to: url)
        try Data().write(to: url.deletingPathExtension().appendingPathExtension("acked"))
        Task.detached {
            let h = try FileHandle(forWritingTo: url)
            for _ in 0..<3 {                                    // three 160 ms frames, written in real time
                try? await Task.sleep(nanoseconds: 160_000_000)
                h.seekToEndOfFile(); h.write(Data(count: 5120))
            }
            h.write(Data([1]))                                  // half a sample: held back
            try h.close()
            try Data().write(to: url.deletingPathExtension().appendingPathExtension("done"))
        }
        var chunks: [Int] = []
        let t0 = ProcessInfo.processInfo.systemUptime
        let end = await AzelFiles.followStream("s1") { chunks.append($0.count) }
        XCTAssertGreaterThanOrEqual(chunks.count, 3, "sent as it grew, not as one burst")
        XCTAssertEqual(chunks.reduce(0, +), 3 * 5120, "whole samples only")
        XCTAssertGreaterThan(end ?? 0, t0 + 0.4)
        for ext in ["pcm", "done", "acked"] {
            XCTAssertFalse(FileManager.default.fileExists(atPath: url.deletingPathExtension().appendingPathExtension(ext).path), ext)
        }
        let none = await AzelFiles.followStream("missing", appear: 0.1) { _ in XCTFail("nothing to send") }
        XCTAssertNil(none, "no stream: all of our own audio is sent")
    }

}
#endif

final class QuietCloseTests: XCTestCase {
    func testHandsFreeCallsCloseAfterQuietThenLinger() {
        let wait = CallOverlay.quiet + CallOverlay.linger
        XCTAssertEqual(wait, 9.5)
        XCTAssertFalse(CallOverlay.shouldClose(turnDone: true, playing: false, drainedFor: 9.0, after: wait))
        XCTAssertTrue(CallOverlay.shouldClose(turnDone: true, playing: false, drainedFor: 9.5, after: wait))
        XCTAssertFalse(CallOverlay.shouldClose(turnDone: false, playing: false, drainedFor: 20, after: wait), "the user spoke again")
        XCTAssertFalse(CallOverlay.shouldClose(turnDone: true, playing: true, drainedFor: 20, after: wait), "still speaking")
    }
}

#if os(macOS)
final class ListeningGlowTests: XCTestCase {
    func testWakeCallsGlowLikePushToTalk() {
        XCTAssertTrue(MacCall.hears(talking: true, wake: false, inCall: true, muted: false, held: false, state: "thinking"), "key held")
        XCTAssertTrue(MacCall.hears(talking: false, wake: true, inCall: true, muted: false, held: false, state: ""), "wake: from the handoff on")
        XCTAssertTrue(MacCall.hears(talking: false, wake: true, inCall: true, muted: false, held: false, state: "listening"))
        XCTAssertFalse(MacCall.hears(talking: false, wake: true, inCall: true, muted: false, held: false, state: "thinking"), "the turn ended")
        XCTAssertFalse(MacCall.hears(talking: false, wake: true, inCall: true, muted: true, held: false, state: "listening"), "muted")
        XCTAssertFalse(MacCall.hears(talking: false, wake: true, inCall: false, muted: false, held: false, state: "listening"), "dismissed")
        XCTAssertFalse(MacCall.hears(talking: false, wake: false, inCall: true, muted: false, held: false, state: "listening"), "a hotkey toggle call keeps the plain border")
    }
}
#endif

#if os(macOS)
final class WakeIntentTests: XCTestCase {
    /// A recorded command must not open as intent=wake: the server's 2 s noisy-VAD guard swallowed it.
    @MainActor func testRecordedCommandSkipsTheWakeGuard() {
        XCTAssertNil(MacCall.callIntent(MacCall.wakeIntent, recorded: true))
        XCTAssertEqual(MacCall.callIntent(MacCall.wakeIntent, recorded: false), MacCall.wakeIntent, "only the name: keep the guard")
        XCTAssertEqual(MacCall.callIntent("briefing", recorded: true), "briefing")
        XCTAssertNil(MacCall.callIntent(nil, recorded: false))
    }

    func testWakeCallsSayIntentWake() throws {
        let req = try CallClient.request(voiceURL: "wss://azel.example", token: "t", session: "mac-1", callID: nil, intent: MacCall.wakeIntent, persona: "azel")
        let items = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)!.queryItems!
        XCTAssertEqual(items.first { $0.name == "intent" }?.value, "wake")
    }
}
#endif

/// User bug, 2026-11-23: "아젤, 내일 날씨 알려줘" in one breath lost words where the listener's
/// recording ended and the app's own mic began (the app dropped its frames until it read the file).
final class UtteranceSeamTests: XCTestCase {
    private func f(_ at: Double, _ byte: UInt8) -> (at: Double, pcm: Data) { (at, Data([byte, byte])) }

    func testOnlyFramesAfterTheListenerStoppedFollowTheRecording() {
        let held = [f(10.00, 1), f(10.02, 2), f(10.20, 3), f(10.22, 4)]   // the app's mic while waiting
        // The listener's file appeared at 10.10: it covers up to there, so 3 and 4 go next.
        XCTAssertEqual(CallClient.seam(held, cutoff: 10.10), [Data([3, 3]), Data([4, 4])])
        XCTAssertEqual(CallClient.seam(held, cutoff: nil).count, 4, "no recording: all of the app's own audio")
        XCTAssertEqual(CallClient.seam([], cutoff: 10).count, 0)
    }

    func testHeldFramesAreSentInOrderAfterTheRecordingAndHoldEnds() {
        let client = CallClient()
        client.holdMic()
        client.releaseMic(first: Data(count: 6400), cutoff: 0)   // no socket here: nothing sent, no crash
        client.releaseMic(first: nil, cutoff: nil)                // a second release is harmless
    }
}
