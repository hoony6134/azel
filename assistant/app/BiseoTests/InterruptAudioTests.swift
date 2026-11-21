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

final class ReplyStartTests: XCTestCase {
    func testOnlyADifferentReplyIdCountsAsNew() {
        var a = AudioAdmission()
        XCTAssertTrue(a.replyStart(1))
        XCTAssertFalse(a.replyStart(1), "a repeated reply_start for the same reply keeps its audio")
        XCTAssertTrue(a.replyStart(2))
        XCTAssertTrue(a.replyStart(nil), "no id: treat as new")
    }

    func testKaraokeKeepsTheNewReplyWhenTheOldAudioIsDropped() {
        var k = KaraokeTimeline()
        k.apply(type: "reply", text: "예전 답")
        k.audio(samples: 1000)
        k.apply(type: "turn_done", text: nil)
        k.apply(type: "reply", text: "새 답")           // text first, then reply_start, then audio
        k.restartClock()
        XCTAssertEqual(k.texts, ["새 답"])
        k.audio(samples: 400)
        XCTAssertEqual(k.spoken(played: 200, stepped: false), 1.5, accuracy: 0.01, "half of \"새 답\" (3 characters) at the restarted clock")
    }
}

final class RouteChangeTests: XCTestCase {
    func testRestartOncePerBurstOfChanges() {
        var g = RouteRestartGate()
        XCTAssertTrue(g.shouldRestart(now: 10))
        XCTAssertFalse(g.shouldRestart(now: 10.2), "the restart's own change is ignored")
        XCTAssertTrue(g.shouldRestart(now: 10.6))
        g.restarted(now: 20)                       // e.g. CallKit didActivate restarted it
        XCTAssertFalse(g.shouldRestart(now: 20.3))
        XCTAssertTrue(g.shouldRestart(now: 21))
    }

    #if os(iOS)
    func testSpeakerAndHeadsetPicks() {
        XCTAssertTrue(AudioRoute.speakerOn([.builtInSpeaker]))
        XCTAssertFalse(AudioRoute.speakerOn([.bluetoothHFP]), "AirPods chosen: the 스피커 toggle turns off")
        XCTAssertEqual(AudioRoute.headsetInput([.builtInMic, .bluetoothHFP]), 1)
        XCTAssertEqual(AudioRoute.headsetInput([.builtInMic, .headsetMic]), 1)
        XCTAssertNil(AudioRoute.headsetInput([.builtInMic]))
    }
    #endif
}
