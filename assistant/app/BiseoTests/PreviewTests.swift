import XCTest
@testable import Biseo

@MainActor
final class PreviewTests: XCTestCase {
    func testEarconPreviewProducesSound() async throws {
        EarconPreview.play()
        try await Task.sleep(nanoseconds: 600_000_000)
        XCTAssertGreaterThan(EarconPreview.peak, 0.01, "the working cue (-26 dBFS) reached the output")
    }

    func testVoiceSampleStartsPlaying() async throws {
        URLProtocol.registerClass(FakeServer.self)
        Config.testOverride = (gateway: "https://fake.test", token: "test-token")
        defer { URLProtocol.unregisterClass(FakeServer.self); Config.testOverride = nil; FakeServer.rawRoutes = [:] }
        // A 0.5 s 440 Hz wav, like the TTS server returns.
        let tone = (0..<8000).map { 0.3 * sin(2 * Float.pi * 440 * Float($0) / 16_000) }
        FakeServer.delay = 0
        FakeServer.rawRoutes = ["POST /v1/personas/voice-sample": WakeClip.wav(tone)]
        let sampler = VoiceSampler()
        sampler.toggle(voice: "sohee", style: nil, name: "아젤")
        for _ in 0..<40 where sampler.playing == nil { try await Task.sleep(nanoseconds: 50_000_000) }
        XCTAssertEqual(sampler.playing, "sohee")
        XCTAssertNil(sampler.message)
        XCTAssertTrue(sampler.coolingDown, "2 s rate-limit cooldown")
        sampler.stop()
    }
}
