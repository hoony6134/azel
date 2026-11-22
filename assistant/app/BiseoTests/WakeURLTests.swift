import XCTest
@testable import Biseo

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
        XCTAssertEqual(got?.count, 3200, "whole samples only")
        XCTAssertFalse(FileManager.default.fileExists(atPath: AzelFiles.utterance("u1").path))
        let missing = await AzelFiles.takeUtterance("nope", appear: 0.2)
        XCTAssertNil(missing)
        try Data().write(to: AzelFiles.utterance("empty"))
        let empty = await AzelFiles.takeUtterance("empty", appear: 0.2)
        XCTAssertNil(empty, "zero bytes: nobody spoke after the name")
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
        XCTAssertEqual(got?.count, 640)
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
