import XCTest
@testable import Biseo

final class KaraokeTests: XCTestCase {
    /// Two sentences: "잠시만요" (4 chars, 2 chunks of 1000) and "정상이에요" (5 chars, 1 chunk of 2000).
    private func timeline() -> KaraokeTimeline {
        var k = KaraokeTimeline()
        k.apply(type: "transcript", text: "인프라 상태 체크해줘")
        k.apply(type: "reply", text: "잠시만요")
        k.audio(samples: 1000); k.audio(samples: 1000)
        k.apply(type: "reply", text: "정상이에요")
        k.audio(samples: 2000)
        return k
    }

    func testHighlightFollowsPlayedSamples() {
        let k = timeline()
        XCTAssertEqual(k.texts, ["잠시만요", "정상이에요"])
        XCTAssertEqual(k.spoken(played: 0, stepped: false), 0)
        XCTAssertEqual(k.spoken(played: 1000, stepped: false), 2, accuracy: 1e-9, "half the first sentence")
        XCTAssertEqual(k.spoken(played: 2000, stepped: false), 4, accuracy: 1e-9)
        XCTAssertEqual(k.spoken(played: 3000, stepped: false), 4 + 1 + 2.5, accuracy: 1e-9, "joined with a space, like CallLog")
        XCTAssertEqual(k.spoken(played: 9999, stepped: false), 10, accuracy: 1e-9, "all of \"잠시만요 정상이에요\"")
        XCTAssertEqual(k.currentSegment(played: 1500), 0)
        XCTAssertEqual(k.currentSegment(played: 2500), 1)
        XCTAssertNil(k.currentSegment(played: 4000))
    }

    func testReduceMotionStepsPerChunk() {
        let k = timeline()
        XCTAssertEqual(k.spoken(played: 999, stepped: true), 0, "inside the first chunk: nothing yet")
        XCTAssertEqual(k.spoken(played: 1000, stepped: true), 2, accuracy: 1e-9)
        XCTAssertEqual(k.spoken(played: 3500, stepped: true), 5, accuracy: 1e-9, "second sentence's single chunk not done")
    }

    func testInterruptFreezesAndNextReplyStartsAParagraph() {
        var k = timeline()
        k.flush(heard: 1000)
        XCTAssertEqual(k.spoken(played: 0, stepped: false), 2, accuracy: 1e-9, "frozen at the interrupt")
        k.apply(type: "reply", text: "네?")
        k.audio(samples: 500)
        XCTAssertEqual(k.texts, ["네?"])
        XCTAssertEqual(k.spoken(played: 500, stepped: false), 2, accuracy: 1e-9)
        k.apply(type: "reply", text: "(말이 끊김)")
        XCTAssertEqual(k.texts, ["네?"], "legacy marker never shown")
    }

    func testPlaybackClockStandsStillWhilePausedAndIgnoresGaps() {
        // 1 s scheduled at 24 kHz, 0.4 s left in the queue → 0.6 s heard.
        XCTAssertEqual(AudioIO.played(scheduled: 24_000, speechEnd: 10.4, now: 10.0, rate: 24_000), 14_400)
        XCTAssertEqual(AudioIO.played(scheduled: 24_000, speechEnd: 10.4, now: 20.0, rate: 24_000), 24_000, "drained; idle gaps don't overcount")
        XCTAssertEqual(AudioIO.played(scheduled: 24_000, speechEnd: 12.0, now: 10.0, rate: 24_000), 0)
    }

    func testOverlayClosesOnlyAfterPlaybackAndLinger() {
        XCTAssertFalse(CallOverlay.shouldClose(turnDone: false, playing: false, drainedFor: 10))
        XCTAssertFalse(CallOverlay.shouldClose(turnDone: true, playing: true, drainedFor: nil), "server done, audio still playing")
        XCTAssertFalse(CallOverlay.shouldClose(turnDone: true, playing: false, drainedFor: 1.0), "lingers 1.5 s")
        XCTAssertTrue(CallOverlay.shouldClose(turnDone: true, playing: false, drainedFor: 1.5))
    }

    func testSplit() {
        let s = KaraokeStyle.split("잠시만요 정상이에요", spoken: 6.7)
        XCTAssertEqual(s.heard, "잠시만요 정")
        XCTAssertEqual(s.rest, "상이에요")
        XCTAssertEqual(KaraokeStyle.split("abc", spoken: 99).rest, "")
    }
}
