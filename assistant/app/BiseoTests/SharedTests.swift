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
    }
}
