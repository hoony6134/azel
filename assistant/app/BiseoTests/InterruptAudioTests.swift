import XCTest
@testable import Biseo

/// After an interrupt, the old reply's audio must never play over the new one.
final class InterruptAudioTests: XCTestCase {
    private func frame(_ bytes: Int) -> URLSessionWebSocketTask.Message { .data(Data(count: bytes)) }
    private func event(_ json: String) -> URLSessionWebSocketTask.Message { .string(json) }

    func testOnlyAudioAfterReplyStartPlays() {
        let client = CallClient()
        client.process(event(#"{"type":"state","value":"speaking"}"#))
        client.process(frame(1000))                                  // old answer, before the interrupt
        client.process(event(#"{"type":"interrupt"}"#))
        client.process(frame(2000))                                  // stale old-answer frames still in flight
        client.process(frame(2000))
        client.process(event(#"{"type":"state","value":"listening"}"#))
        client.process(frame(500))                                   // still stale
        client.process(event(#"{"type":"reply_start","reply_id":2}"#))
        client.process(frame(3000))                                  // the new answer
        XCTAssertEqual(client.audioCounts.accepted, 1000 + 3000)
        XCTAssertEqual(client.audioCounts.dropped, 2000 + 2000 + 500)
    }

    func testOlderServerWithoutReplyStartResumesAtSpeaking() {
        let client = CallClient()
        client.process(frame(100))
        client.process(event(#"{"type":"interrupt"}"#))
        client.process(frame(200))
        client.process(event(#"{"type":"state","value":"thinking"}"#))
        client.process(frame(300))
        client.process(event(#"{"type":"state","value":"speaking"}"#))
        client.process(frame(400))
        XCTAssertEqual(client.audioCounts.accepted, 100 + 400)
        XCTAssertEqual(client.audioCounts.dropped, 200 + 300)
    }

    func testTapInterruptAlsoDropsStaleAudio() {
        let client = CallClient()
        client.process(frame(100))
        client.interrupt()                                           // no socket: just the local part
        client.process(frame(200))
        client.process(event(#"{"type":"reply_start","reply_id":5}"#))
        client.process(frame(300))
        XCTAssertEqual(client.audioCounts.accepted, 100 + 300)
        XCTAssertEqual(client.audioCounts.dropped, 200)
    }

    func testAdmissionRules() {
        var a = AudioAdmission()
        XCTAssertTrue(a.admits)
        a.apply(type: "interrupt", value: nil)
        XCTAssertFalse(a.admits)
        a.apply(type: "pause", value: nil); a.apply(type: "resume", value: nil); a.apply(type: "state", value: "listening")
        XCTAssertFalse(a.admits, "pause/resume/listening don't reopen it")
        a.apply(type: "reply_start", value: nil)
        XCTAssertTrue(a.admits)
    }
}
