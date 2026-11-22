import AVFoundation
import XCTest
@testable import Azel

final class RecordingMixTests: XCTestCase {
    func testAssistantLandsAtMicClockAndQueuesAfterPreviousReply() {
        var mix = RecordingMix()
        XCTAssertEqual(mix.addMic([0.1, 0.1]), [0.1, 0.1])
        mix.addAssistant([0.5, 0.5, 0.5])          // starts at clock 2
        mix.addAssistant([0.25])                   // queued after the first reply, not on top of it
        XCTAssertEqual(mix.addMic([0, 0]), [0.5, 0.5])
        XCTAssertEqual(mix.addMic([0.1, 0, 0]), [0.6, 0.25, 0])
        XCTAssertEqual(mix.clock, 7)
    }

    func testInterruptDropsQueuedAssistantAudio() {
        var mix = RecordingMix()
        mix.addAssistant([0.5, 0.5, 0.5, 0.5])
        XCTAssertEqual(mix.addMic([0]), [0.5])
        mix.interrupt()
        XCTAssertEqual(mix.addMic([0, 0]), [0, 0])
        mix.addAssistant([0.2])                    // the next reply starts at the clock again
        XCTAssertEqual(mix.finish(), [0.2])
    }

    func testMixIsClipped() {
        var mix = RecordingMix()
        mix.addAssistant([0.9])
        XCTAssertEqual(mix.addMic([0.9]), [1])
    }

    func testDownsample24kTo16k() {
        let out = RecordingMix.downsample24k([0, 0.3, 0.6, 0.9, 1.2, 1.5])
        XCTAssertEqual(out.count, 4)
        XCTAssertEqual(out[1], 0.45, accuracy: 1e-6)   // x = 1.5 → halfway between 0.3 and 0.6
        XCTAssertEqual(out[2], 0.9, accuracy: 1e-6)
    }
}

final class RecordingFileTests: XCTestCase {
    private var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("rec-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDown() { try? FileManager.default.removeItem(at: dir) }

    func testFileNameIsSessionPlusTimestampAndSafe() {
        var c = DateComponents(); c.year = 2026; c.month = 11; c.day = 21; c.hour = 14; c.minute = 3; c.second = 9
        let date = Calendar(identifier: .gregorian).date(from: c)!
        XCTAssertEqual(Recordings.fileName(session: "mac-2026-11-21", start: date), "mac-2026-11-21_20261121-140309.m4a")
        XCTAssertEqual(Recordings.fileName(session: "../call id/x", start: date), "---call-id-x_20261121-140309.m4a")
        XCTAssertEqual(Recordings.fileName(session: "", start: date), "call_20261121-140309.m4a")
    }

    func testRecorderWritesAudioAndTranscriptThenDeleteRemovesBoth() throws {
        let url = dir.appendingPathComponent(Recordings.fileName(session: "iphone-test", start: Date()))
        let rec = try CallRecorder(url: url)
        let second = Data(repeating: 0x10, count: 32_000)   // 1 s of 16 kHz PCM16
        rec.mic(second, silent: false)
        rec.assistant(Data(repeating: 0x20, count: 24_000)) // 0.5 s at 24 kHz
        rec.note(CallEvent(type: "transcript", text: "안녕"))
        rec.note(CallEvent(type: "reply", text: "네", route: "qwen"))
        rec.mic(second, silent: true)
        rec.finish()

        let file = try AVAudioFile(forReading: url)
        XCTAssertEqual(Double(file.length) / file.fileFormat.sampleRate, 2, accuracy: 0.1)
        let text = try String(contentsOf: Recordings.transcriptURL(for: url), encoding: .utf8)
        XCTAssertTrue(text.contains("나: 안녕"))
        XCTAssertTrue(text.contains("\(AssistantName.current) (qwen): 네"))
        XCTAssertEqual(Recordings.list(in: dir), [url])

        Recordings.delete(url)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: dir.path), [])
    }

    func testCleanupRemovesEmptyRecordingsAndOrphanTranscripts() throws {
        let good = dir.appendingPathComponent("a_20261121-000001.m4a"), empty = dir.appendingPathComponent("b_20261121-000002.m4a")
        try Data(repeating: 1, count: 4096).write(to: good)
        try Data("x".utf8).write(to: Recordings.transcriptURL(for: good))
        try Data().write(to: empty)
        try Data("x".utf8).write(to: Recordings.transcriptURL(for: empty))
        try Data("x".utf8).write(to: dir.appendingPathComponent("orphan_20261121-000003.txt"))
        Recordings.cleanup(in: dir)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: dir.path).sorted(),
                       ["a_20261121-000001.m4a", "a_20261121-000001.txt"])
    }
}

final class HoldTests: XCTestCase {
    func testMicActionForMuteAndHold() {
        XCTAssertEqual(CallClient.micAction(muted: false, held: false), .send)
        XCTAssertEqual(CallClient.micAction(muted: true, held: false), .silence)
        XCTAssertEqual(CallClient.micAction(muted: false, held: true), .drop)
        XCTAssertEqual(CallClient.micAction(muted: true, held: true), .drop)
    }

    func testStopClearsHoldMuteAndRecording() throws {
        let client = CallClient()
        client.held = true
        client.muted = true
        XCTAssertTrue(client.held)
        client.stop()
        XCTAssertFalse(client.held)
        XCTAssertFalse(client.muted)
        XCTAssertFalse(client.isRecording)
        XCTAssertFalse(client.isConnected)
    }
}

final class HangUpTests: XCTestCase {
    /// What CXEndCallAction relies on: stop() always closes the socket, stops audio and finishes
    /// a running recording, and is safe to call twice.
    func testStopEndsEverything() throws {
        let client = CallClient()
        try client.startRecording(session: "hangup-test")
        XCTAssertTrue(client.isRecording)
        client.stop()
        XCTAssertFalse(client.isRecording)
        XCTAssertFalse(client.isConnected)
        XCTAssertFalse(client.audio.isRunning)
        client.stop()
        Recordings.list().filter { $0.lastPathComponent.hasPrefix("hangup-test") }.forEach(Recordings.delete)
    }

    func testServerHangupDecodes() throws {
        XCTAssertEqual(try JSONDecoder().decode(CallEvent.self, from: Data(#"{"type":"hangup"}"#.utf8)).type, "hangup")
    }
}
