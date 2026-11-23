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

    func testUtteranceIsReadOnceAndDeleted() async throws {
        try FileManager.default.createDirectory(at: AzelFiles.folder, withIntermediateDirectories: true)
        let pcm = Data((0..<3201).map { UInt8($0 % 251) })      // an odd byte at the end
        try pcm.write(to: AzelFiles.utterance("u1"))
        let got = await AzelFiles.takeUtterance("u1", appear: 0.5)
        XCTAssertEqual(got.data?.count, 3200, "whole samples only")
        XCTAssertNotNil(got.at, "when it appeared: the seam's cutoff")
        XCTAssertFalse(FileManager.default.fileExists(atPath: AzelFiles.utterance("u1").path))
        let missing = await AzelFiles.takeUtterance("nope", appear: 0.2)
        XCTAssertNil(missing.data)
        XCTAssertNil(missing.at, "never appeared: the app sends all of its own held audio")
        try Data().write(to: AzelFiles.utterance("empty"))
        let empty = await AzelFiles.takeUtterance("empty", appear: 0.2)
        XCTAssertNil(empty.data, "zero bytes: nobody spoke after the name")
        XCTAssertFalse(FileManager.default.fileExists(atPath: AzelFiles.utterance("empty").path))
    }

    func testUtteranceArrivingLateIsWaitedFor() async throws {
        try FileManager.default.createDirectory(at: AzelFiles.folder, withIntermediateDirectories: true)
        let url = AzelFiles.utterance("late")
        try Data(count: 640).write(to: url.appendingPathExtension("part"))
        Task {
            try? await Task.sleep(nanoseconds: 700_000_000)              // longer than `appear`: the .part keeps it waiting
            try? FileManager.default.moveItem(at: url.appendingPathExtension("part"), to: url)   // the listener finishes
        }
        let got = await AzelFiles.takeUtterance("late", appear: 0.3, partLimit: 3)
        XCTAssertEqual(got.data?.count, 640)
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

    /// The bridge: the tail the listener recorded until our mic went live is read once and deleted; with
    /// no listener (no marker, no files) nothing is waited for.
    func testBridgeTailIsTakenOrSkipped() async throws {
        let t0 = Date()
        let none = await AzelFiles.takeTail("old", limit: 1)
        XCTAssertNil(none.data)
        XCTAssertLessThan(Date().timeIntervalSince(t0), 0.2, "an old listener writes no tail: no wait")
        try FileManager.default.createDirectory(at: AzelFiles.folder, withIntermediateDirectories: true)
        let url = AzelFiles.folder.appendingPathComponent("wake-utterance-b1.tail")
        try Data(count: 10).write(to: url.appendingPathExtension("part"))
        Task {
            try? await Task.sleep(nanoseconds: 300_000_000)
            try? FileManager.default.removeItem(at: url.appendingPathExtension("part"))
            try? Data(count: 6400).write(to: url)                           // the listener stopped: 200 ms of tail
        }
        let got = await AzelFiles.takeTail("b1", limit: 2)
        XCTAssertEqual(got.data?.count, 6400)
        XCTAssertNotNil(got.at, "when it stopped: live frames from then on follow")
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
        XCTAssertEqual(MacCall.joined(Data([1]), Data([2, 3])), Data([1, 2, 3]))
        XCTAssertEqual(MacCall.joined(nil, Data([2])), Data([2]))
        XCTAssertNil(MacCall.joined(nil, nil))
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
