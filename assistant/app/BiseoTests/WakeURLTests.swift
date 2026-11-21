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
        let got = await AzelFiles.takeUtterance("u1", timeout: 0.5)
        XCTAssertEqual(got?.count, 3200, "whole samples only")
        XCTAssertFalse(FileManager.default.fileExists(atPath: AzelFiles.utterance("u1").path))
        let missing = await AzelFiles.takeUtterance("nope", timeout: 0.2)
        XCTAssertNil(missing)
    }

    func testUtteranceArrivingLateIsWaitedFor() async throws {
        try FileManager.default.createDirectory(at: AzelFiles.folder, withIntermediateDirectories: true)
        let url = AzelFiles.utterance("late")
        try Data(count: 640).write(to: url.appendingPathExtension("part"))
        Task {
            try? await Task.sleep(nanoseconds: 300_000_000)
            try? FileManager.default.moveItem(at: url.appendingPathExtension("part"), to: url)   // the listener finishes
        }
        let got = await AzelFiles.takeUtterance("late", timeout: 2)
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
