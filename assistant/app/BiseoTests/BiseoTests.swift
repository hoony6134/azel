import AVFoundation
import XCTest
@testable import Biseo

final class PCMTests: XCTestCase {
    func testPCM16BytesDecodeLittleEndianToFloat() {
        // 0, 16384 (0x4000), -32768 (0x8000), 32767 (0x7fff); trailing odd byte is ignored.
        let data = Data([0x00, 0x00, 0x00, 0x40, 0x00, 0x80, 0xff, 0x7f, 0x12])
        let buf = PCM.floatBuffer(fromPCM16: data)!
        XCTAssertEqual(buf.format.sampleRate, 24_000)
        XCTAssertEqual(buf.format.channelCount, 1)
        XCTAssertEqual(buf.frameLength, 4)
        let s = buf.floatChannelData![0]
        XCTAssertEqual(s[0], 0)
        XCTAssertEqual(s[1], 0.5)
        XCTAssertEqual(s[2], -1)
        XCTAssertEqual(s[3], 32767 / 32768, accuracy: 1e-6)
        XCTAssertNil(PCM.floatBuffer(fromPCM16: Data([0x01])))
    }

    func testMicConversionTo16kMonoPCM16() {
        // 100 ms of a 440 Hz stereo float tone at 48 kHz, like a typical mic tap.
        let inFormat = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 2)!
        let converter = AVAudioConverter(from: inFormat, to: PCM.micFormat)!
        converter.downmix = true
        var total = 0
        var peak: Int16 = 0
        for _ in 0..<5 {
            let buf = AVAudioPCMBuffer(pcmFormat: inFormat, frameCapacity: 4800)!
            buf.frameLength = 4800
            for ch in 0..<2 { for i in 0..<4800 { buf.floatChannelData![ch][i] = 0.5 * sin(2 * .pi * 440 * Float(i) / 48_000) } }
            let data = PCM.pcm16Data(from: buf, converter: converter)!
            XCTAssertEqual(data.count % 2, 0, "whole PCM16 samples only")
            total += data.count / 2
            data.withUnsafeBytes { raw in
                for i in 0..<(data.count / 2) { peak = max(peak, Int16(littleEndian: raw.loadUnaligned(fromByteOffset: i * 2, as: Int16.self))) }
            }
        }
        // 500 ms in → about 8000 samples at 16 kHz (resampler latency may hold back a few).
        XCTAssertEqual(Double(total), 8000, accuracy: 200)
        XCTAssertGreaterThan(peak, 8000, "tone survives downmix + resample")
    }
}

final class EventDecodingTests: XCTestCase {
    private func decode(_ json: String) throws -> CallEvent {
        try JSONDecoder().decode(CallEvent.self, from: Data(json.utf8))
    }

    func testCallEvents() throws {
        XCTAssertEqual(try decode(#"{"type":"state","value":"thinking"}"#).value, "thinking")
        XCTAssertEqual(try decode(#"{"type":"transcript","text":"내일 일정 알려줘"}"#).text, "내일 일정 알려줘")
        let reply = try decode(#"{"type":"reply","text":"네.","route":"claude"}"#)
        XCTAssertEqual(reply.route, "claude")
        XCTAssertEqual(try decode(#"{"type":"interrupt"}"#).type, "interrupt")
        XCTAssertEqual(try decode(#"{"type":"turn_done","timings":{"stt_ms":120}}"#).type, "turn_done")
        XCTAssertEqual(try decode(#"{"type":"error","message":"boom"}"#).message, "boom")
    }

    func testSettingsRoundTripMatchesContractExample() throws {
        let json = #"""
        {"default_model":"auto","claude_path":"auto",
         "task_defaults":{"chat":{"model":"qwen"},"schedule":{"model":"sonnet"},
                          "code":{"model":"opus","path":"claude_code"},"mac":{"model":"sonnet"}},
         "monthly_budget_usd":500,"budget_warn_pct":80,
         "proactive":{"morning_briefing":"08:00","quiet_hours":["23:30","07:30"],
                      "triggers":{"deadline":true,"server_down":true}},
         "default_device":"맥북","device_fallback":["알비레오","스파크"]}
        """#
        let s = try JSONDecoder().decode(AppSettings.self, from: Data(json.utf8))
        XCTAssertEqual(s.task_defaults["code"]?.path, "claude_code")
        XCTAssertNil(s.task_defaults["chat"]?.path)
        // Old proactive shape migrates: briefing time kept, triggers become switches, quiet hours dropped.
        XCTAssertEqual(s.proactive.morning_briefing, .init(enabled: true, time: "08:00"))
        XCTAssertTrue(s.proactive.deadline_check.enabled)
        XCTAssertTrue(s.proactive.server_alerts)
        XCTAssertFalse(s.proactive.autonomous.enabled)
        XCTAssertEqual(s.device_fallback, ["알비레오", "스파크"])
        XCTAssertEqual(try JSONDecoder().decode(AppSettings.self, from: JSONEncoder().encode(s)), s)
    }

    func testStatusDecoding() throws {
        let json = #"{"spark":{"ok":true,"ms":12},"gateway":{"ok":true,"ms":3},"devices":[{"name":"맥북","ok":false,"ms":null}]}"#
        let s = try JSONDecoder().decode(ServerStatus.self, from: Data(json.utf8))
        XCTAssertEqual(s.spark.ms, 12)
        XCTAssertFalse(s.devices[0].ok)
    }
}

final class SessionTests: XCTestCase {
    private var store: UserDefaults!

    override func setUp() {
        store = UserDefaults(suiteName: "SessionTests")!
        store.removePersistentDomain(forName: "SessionTests")
    }

    private func date(_ s: String) -> Date {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return f.date(from: s)!
    }

    func testStableWithinDayAndNewNextDay() {
        let morning = date("2026-11-21 08:00:00"), night = date("2026-11-21 23:59:00")
        XCTAssertEqual(Session.current(now: morning, store: store), "iphone-2026-11-21")
        XCTAssertEqual(Session.current(now: night, store: store), "iphone-2026-11-21")
        XCTAssertEqual(Session.current(now: date("2026-11-22 00:01:00"), store: store), "iphone-2026-11-22")
    }

    func testRotateSticksForTheDayThenResets() {
        let id = Session.rotate(now: date("2026-11-21 14:32:05"), store: store)
        XCTAssertEqual(id, "iphone-2026-11-21-143205")
        XCTAssertEqual(Session.current(now: date("2026-11-21 20:00:00"), store: store), id)
        XCTAssertEqual(Session.current(now: date("2026-11-22 09:00:00"), store: store), "iphone-2026-11-22")
    }

    func testPrefixesAreIndependent() {
        let now = date("2026-11-21 08:00:00")
        Session.rotate(now: now, store: store)
        XCTAssertEqual(Session.current("briefing", now: now, store: store), "briefing-2026-11-21")
        XCTAssertEqual(Session.current(now: now, store: store), "iphone-2026-11-21-080000")
    }
}
