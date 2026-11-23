import XCTest
@testable import Azel

final class MuteTests: XCTestCase {
    /// Muted: the orb never follows the mic; reply audio and the heartbeat still drive it.
    func testMutedOrbIgnoresTheMic() {
        XCTAssertEqual(JarPath.level(.listening, mic: 0.9, out: 0, muted: true), 0)
        XCTAssertEqual(JarPath.level(.listening, mic: 0.9, out: 0, muted: false), 0.9)
        XCTAssertEqual(JarPath.level(.speaking, mic: 0.9, out: 0.6, muted: true), 0.6, "reply playback isn't the mic")
        XCTAssertEqual(JarPath.level(.thinking, mic: 0.9, out: 0.4, muted: true), 0.4, "the heartbeat isn't the mic")
        // listening + muted is drawn calm: one fixed glow whatever the level
        XCTAssertEqual(JarPath.glowStrength(.listening, level: 0.9, calm: true), JarPath.glowStrength(.listening, level: 0, calm: true))
    }

    /// Muted: the mic is not metered at all (no level, no "user is talking" for the cue gate).
    func testMutedCallMetersNoMic() {
        let client = CallClient()
        XCTAssertTrue(client.audio.meterMic)
        client.muted = true
        XCTAssertFalse(client.audio.meterMic)
        XCTAssertEqual(client.audio.levels.mic, 0)
        XCTAssertEqual(client.audio.levels.micLoudAt, -.infinity)
        client.muted = false
        XCTAssertTrue(client.audio.meterMic)
    }
}

/// 2026-11-23: on the Mac panel the activity line was drawn over the scrolled reply.
final class PanelColumnTests: XCTestCase {
    private func tops(_ hs: [CGFloat], spacing: CGFloat) -> [CGFloat] {
        var y: CGFloat = 0
        return hs.map { h in defer { y += h + spacing }; return y }
    }

    func testRowsNeverOverlapAndTheReplyGetsWhatIsLeft() {
        // header 20, user line 32, long reply 200 (flexible), approval 26, in a 112 pt column
        let hs = PanelColumn.heights([20, 32, 200, 26], flexible: 2, available: 112, spacing: 2)
        XCTAssertEqual(hs[0], 20); XCTAssertEqual(hs[1], 32); XCTAssertEqual(hs[3], 26)
        XCTAssertEqual(hs[2], 112 - 20 - 32 - 26 - 6, "the reply shrinks to the rest")
        let t = tops(hs, spacing: 2)
        for i in 1..<hs.count { XCTAssertGreaterThanOrEqual(t[i], t[i - 1] + hs[i - 1], "row \(i) starts below row \(i - 1)") }
        XCTAssertLessThanOrEqual(t.last! + hs.last!, 112)
    }

    func testShortContentKeepsItsHeight() {
        XCTAssertEqual(PanelColumn.heights([20, 18, 30], flexible: 2, available: 112, spacing: 2), [20, 18, 30])
    }

    func testFixedRowsTooTallAreTrimmedFromTheBottom() {
        let hs = PanelColumn.heights([40, 40, 60, 30], flexible: 2, available: 112, spacing: 2)
        XCTAssertEqual(hs[2], 0, "no room left for the reply")
        XCTAssertLessThanOrEqual(hs.reduce(0, +) + 6, 112)
        XCTAssertEqual(hs[0], 40, "the header is kept")
    }
}
