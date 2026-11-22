import XCTest
@testable import Azel

final class WakeClipTests: XCTestCase {
    private var seed: UInt32 = 1
    private func noise(_ seconds: Double, amplitude: Float) -> [Float] {
        (0..<Int(seconds * 16_000)).map { _ in
            seed = seed &* 1_664_525 &+ 1_013_904_223
            return (Float(seed >> 8) / Float(1 << 24) * 2 - 1) * amplitude
        }
    }
    private func tone(_ seconds: Double, amplitude: Float) -> [Float] {
        (0..<Int(seconds * 16_000)).map { max(-1, min(1, amplitude * sin(2 * .pi * 220 * Float($0) / 16_000))) }
    }
    /// background, "speech", background
    private func clip(speech: Double = 0.8, amplitude: Float = 0.35, background: Float = 0.0017) -> [Float] {
        noise(0.4, amplitude: background) + zip(tone(speech, amplitude: amplitude), noise(speech, amplitude: background)).map(+)
            + noise(0.4, amplitude: background)
    }

    func testGoodClipPasses() { XCTAssertNil(WakeClip.check(clip())) }
    func testTooQuiet() { XCTAssertEqual(WakeClip.check(clip(amplitude: 0.015)), .tooQuiet) }
    func testClipped() { XCTAssertEqual(WakeClip.check(clip(amplitude: 1.5)), .clipped) }
    func testTooShort() { XCTAssertEqual(WakeClip.check(clip(speech: 0.15)), .tooShort) }
    func testTooLong() { XCTAssertEqual(WakeClip.check(clip(speech: 2.6)), .tooLong) }
    func testNoisy() { XCTAssertEqual(WakeClip.check(clip(amplitude: 0.35, background: 0.1)), .noisy) }
    func testSilence() { XCTAssertEqual(WakeClip.check(noise(1.5, amplitude: 0.0017)), .noSpeech) }

    func testTrimmedClipsFitTheServersLimits() {
        let t = WakeClip.trimmed(clip())!
        XCTAssertTrue(WakeClip.uploadable(t))
        XCTAssertEqual(Double(t.count) / 16_000, 0.8 + 0.4, accuracy: 0.05, "speech + 0.2 s each side")
        let long = WakeClip.trimmed(noise(0.4, amplitude: 0.0017) + tone(3.0, amplitude: 0.35))!
        XCTAssertLessThanOrEqual(long.count, 40_000, "capped at 2.5 s")
        XCTAssertNil(WakeClip.trimmed(noise(1, amplitude: 0.0017)))
    }

    func testEveryProblemHasAPrompt() {
        for p in [WakeClip.Problem.tooQuiet, .clipped, .tooShort, .tooLong, .noisy, .noSpeech] { XCTAssertFalse(p.message.isEmpty) }
    }

    func testVADStopsAfterSpeechThenSilenceOrAtFourSeconds() {
        var vad = WakeVAD()
        XCTAssertFalse(vad.feed(levelDB: -60, seconds: 1), "waiting for speech doesn't stop")
        XCTAssertFalse(vad.feed(levelDB: -20, seconds: 0.5))
        XCTAssertFalse(vad.feed(levelDB: -60, seconds: 0.4))
        XCTAssertTrue(vad.feed(levelDB: -60, seconds: 0.4), "0.8 s of silence after speech stops it")
        var never = WakeVAD()
        XCTAssertFalse(never.feed(levelDB: -60, seconds: 3.9))
        XCTAssertTrue(never.feed(levelDB: -60, seconds: 0.2), "hard stop at 4 s")
    }

    func testWavIs16kMonoPCM16() {
        let wav = WakeClip.wav([0, 0.5, -1, 1])
        XCTAssertEqual(wav.count, 44 + 8)
        XCTAssertEqual(String(decoding: wav[0..<4], as: UTF8.self), "RIFF")
        XCTAssertEqual(String(decoding: wav[8..<12], as: UTF8.self), "WAVE")
        let rate = wav[24..<28].withUnsafeBytes { $0.loadUnaligned(as: UInt32.self) }
        XCTAssertEqual(UInt32(littleEndian: rate), 16_000)
        let channels = wav[22..<24].withUnsafeBytes { $0.loadUnaligned(as: UInt16.self) }
        XCTAssertEqual(UInt16(littleEndian: channels), 1)
        let last = wav[50..<52].withUnsafeBytes { $0.loadUnaligned(as: Int16.self) }
        XCTAssertEqual(Int16(littleEndian: last), 32767)
    }
}
