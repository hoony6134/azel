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

final class HangupAudioTests: XCTestCase {
    private func frame(_ n: Int) -> URLSessionWebSocketTask.Message { .data(Data(count: n)) }

    func testLateFramesOfAHungUpCallNeverPlay() {
        let client = CallClient()
        let ended = client.currentCall
        client.deliver(frame(1000), call: ended)                    // the call still up: plays
        client.stop()                                               // hang up
        client.deliver(frame(2000), call: ended)                    // still in flight from the old socket
        client.deliver(.string(#"{"type":"reply_start","reply_id":9}"#), call: ended)
        client.deliver(frame(3000), call: ended)
        XCTAssertEqual(client.audioCounts.accepted, 1000)
        XCTAssertEqual(client.audioCounts.dropped, 2000 + 3000)
        client.stop()                                               // twice is fine
    }

    #if os(macOS)
    func testHangupDropsEverythingScheduled() throws {
        let audio = AudioIO()
        try audio.start()
        audio.play(Data(count: 24_000 * 2 * 3))                     // 3 s of a reply queued
        XCTAssertTrue(audio.speechPlaying)
        audio.stop()
        XCTAssertFalse(audio.speechPlaying, "nothing left to play")
        XCTAssertEqual(audio.playedSamples, 0)
        try audio.start()                                           // the next call
        XCTAssertFalse(audio.speechPlaying, "the old reply doesn't come back")
        audio.stop()
    }
    #endif
}

final class PlayedReportTests: XCTestCase {
    func testReportsEachReplyOnceItsLastSamplePlayed() {
        var t = PlayedTracker()
        t.start(1); t.audio(samples: 1000); t.audio(samples: 500)
        XCTAssertEqual(t.due(played: 1500), [], "still streaming: not done even though playback caught up")
        t.start(2)                                   // the next reply closes #1 and queues after it
        t.audio(samples: 800)
        XCTAssertEqual(t.due(played: 1400), [])
        XCTAssertEqual(t.due(played: 1500), [1])
        XCTAssertEqual(t.due(played: 1600), [], "once only")
        t.close()                                    // turn_done
        XCTAssertEqual(t.due(played: 2299), [])
        XCTAssertEqual(t.due(played: 2300), [2])
    }

    func testInterruptedAndSilentRepliesAreNotReported() {
        var t = PlayedTracker()
        t.start(1); t.audio(samples: 1000)
        t.flush()                                    // interrupt
        XCTAssertEqual(t.due(played: 5000), [])
        t.start(2); t.close()                        // text-only reply, no audio
        t.start(3); t.audio(samples: 100); t.close()
        XCTAssertEqual(t.due(played: 100), [3])
    }

    func testANewReplyStartQueuesAfterTheTail() {
        let client = CallClient()
        client.process(.string(#"{"type":"reply_start","reply_id":1}"#))
        client.process(.data(Data(count: 2000)))
        client.process(.string(#"{"type":"reply_start","reply_id":2}"#))  // no interrupt: nothing dropped
        client.process(.data(Data(count: 2000)))
        XCTAssertEqual(client.audioCounts.accepted, 4000)
        XCTAssertEqual(client.audioCounts.dropped, 0)
    }
}
