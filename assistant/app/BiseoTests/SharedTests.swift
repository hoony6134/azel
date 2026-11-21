import AVFoundation
import XCTest
@testable import Biseo

final class MacSessionTests: XCTestCase {
    private let store: UserDefaults = {
        let s = UserDefaults(suiteName: "BiseoKitTests")!
        s.removePersistentDomain(forName: "BiseoKitTests")
        return s
    }()

    private func date(_ s: String) -> Date {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return f.date(from: s)!
    }

    func testMacPrefixIsDailyAndRotatesIndependentlyOfIPhone() {
        let now = date("2026-11-21 09:15:00")
        XCTAssertEqual(Session.current("mac", now: now, store: store), "mac-2026-11-21")
        XCTAssertEqual(Session.rotate("mac", now: now, store: store), "mac-2026-11-21-091500")
        XCTAssertEqual(Session.current("mac", now: date("2026-11-21 22:00:00"), store: store), "mac-2026-11-21-091500")
        XCTAssertEqual(Session.current(now: now, store: store), "iphone-2026-11-21")
        XCTAssertEqual(Session.current("mac", now: date("2026-11-22 00:00:01"), store: store), "mac-2026-11-22")
    }
}

final class CallEventTests: XCTestCase {
    func testUnknownFieldsAndTypesStillDecode() throws {
        let e = try JSONDecoder().decode(CallEvent.self, from: Data(#"{"type":"future_thing","extra":[1,2]}"#.utf8))
        XCTAssertEqual(e, CallEvent(type: "future_thing"))
        let r = try JSONDecoder().decode(CallEvent.self, from: Data(#"{"type":"reply","text":"네","route":"qwen"}"#.utf8))
        XCTAssertEqual(r.route, "qwen")
        XCTAssertThrowsError(try JSONDecoder().decode(CallEvent.self, from: Data(#"{"text":"no type"}"#.utf8)))
    }
}

final class PCMRoundTripTests: XCTestCase {
    func testRoundTripThroughMicFormatKeepsSampleValues() {
        // Float 16 kHz mono → PCM16 16 kHz (no resampling) → bytes → floats again.
        let inFormat = AVAudioFormat(standardFormatWithSampleRate: 16_000, channels: 1)!
        let converter = AVAudioConverter(from: inFormat, to: PCM.micFormat)!
        let buf = AVAudioPCMBuffer(pcmFormat: inFormat, frameCapacity: 4)!
        buf.frameLength = 4
        for (i, v) in [Float(0), 0.5, -0.5, -1].enumerated() { buf.floatChannelData![0][i] = v }
        let data = PCM.pcm16Data(from: buf, converter: converter)!
        XCTAssertEqual(data.count, 8)
        let back = PCM.floatBuffer(fromPCM16: data, format: inFormat)!
        for (i, v) in [Float(0), 0.5, -0.5, -1].enumerated() { XCTAssertEqual(back.floatChannelData![0][i], v, accuracy: 1e-4) }
    }
}

final class CallRequestTests: XCTestCase {
    func testTokenGoesInHeaderNotQuery() throws {
        let req = try CallClient.request(voiceURL: "wss://azel.scian.xyz", token: "t0k", session: "mac-2026-11-21", callID: "c1")
        XCTAssertEqual(req.url?.absoluteString, "wss://azel.scian.xyz/v1/call?session=mac-2026-11-21&call_id=c1")
        XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer t0k")
        XCTAssertThrowsError(try CallClient.request(voiceURL: "https://azel.scian.xyz", token: "t", session: "s", callID: nil))
        let briefing = try CallClient.request(voiceURL: "wss://azel.scian.xyz", token: "t", session: "briefing-2026-11-22", callID: nil, intent: "briefing")
        XCTAssertEqual(briefing.url?.absoluteString, "wss://azel.scian.xyz/v1/call?session=briefing-2026-11-22&intent=briefing")
    }
}

final class MultichannelMicTests: XCTestCase {
    /// Mac mics arrive as 3 or 9 channels; downmixing those in AVAudioConverter gave pure zeros.
    func testNineChannelMicStillProducesSignal() {
        let layout = AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_DiscreteInOrder | 9)!
        let nine = AVAudioFormat(standardFormatWithSampleRate: 48_000, channelLayout: layout)
        let buf = AVAudioPCMBuffer(pcmFormat: nine, frameCapacity: 4800)!
        buf.frameLength = 4800
        for c in 0..<9 { for i in 0..<4800 { buf.floatChannelData![c][i] = c == 0 ? 0.5 * sin(2 * .pi * 440 * Float(i) / 48_000) : 0.01 } }
        let mono = PCM.firstChannel(buf)!
        XCTAssertEqual(mono.format.channelCount, 1)
        XCTAssertEqual(mono.floatChannelData![0][100], buf.floatChannelData![0][100])
        let converter = AVAudioConverter(from: mono.format, to: PCM.micFormat)!
        var total = 0, peak: Int16 = 0
        for _ in 0..<3 {
            guard let data = PCM.pcm16Data(from: PCM.firstChannel(buf)!, converter: converter) else { continue } // resampler warm-up
            total += data.count
            peak = max(peak, CallClient.peak(data))
        }
        XCTAssertGreaterThan(peak, 8000, "the voice channel survives")
        XCTAssertGreaterThan(total / 2, 3000, "most of 300 ms at 16 kHz")
    }

    func testMonoPassesThroughUnchanged() {
        let one = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1)!
        let buf = AVAudioPCMBuffer(pcmFormat: one, frameCapacity: 10)!
        XCTAssertTrue(PCM.firstChannel(buf) === buf)
        XCTAssertEqual(CallClient.peak(Data([0, 0, 0x00, 0x80])), Int16.max, "-32768 counts as full scale")
    }
}
