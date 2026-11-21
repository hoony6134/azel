import AVFoundation

/// Both sides of a call on one 16 kHz timeline. The mic stream is continuous, so its sample count is
/// the clock; assistant audio is laid in at the clock (or after the previous reply, like the player
/// queues it) and written once the mic catches up.
struct RecordingMix {
    static let rate = 16_000.0
    private(set) var clock = 0              // mic samples seen = samples written
    private var pending: [Float] = []       // assistant audio ahead of the clock; index 0 = clock
    private var assistantEnd = 0            // timeline index where queued assistant audio ends

    /// Adds mic samples (zeros while muted) and returns the mixed samples now ready to write.
    mutating func addMic(_ mic: [Float]) -> [Float] {
        var out = mic
        for i in 0..<min(mic.count, pending.count) { out[i] += pending[i] }
        pending.removeFirst(min(mic.count, pending.count))
        clock += mic.count
        return out.map { max(-1, min(1, $0)) }
    }

    /// Assistant audio already at 16 kHz.
    mutating func addAssistant(_ samples: [Float]) {
        let start = max(assistantEnd, clock) - clock
        if pending.count < start + samples.count { pending += [Float](repeating: 0, count: start + samples.count - pending.count) }
        for (i, s) in samples.enumerated() { pending[start + i] += s }
        assistantEnd = clock + start + samples.count
    }

    /// `interrupt`: playback dropped everything queued, so the recording does too.
    mutating func interrupt() {
        pending = []
        assistantEnd = clock
    }

    /// Whatever assistant audio is still queued when the call ends.
    mutating func finish() -> [Float] {
        defer { pending = []; assistantEnd = clock }
        return pending.map { max(-1, min(1, $0)) }
    }

    /// 24 kHz → 16 kHz by linear interpolation.
    /// shortcut: each chunk is resampled on its own (tiny seams at chunk edges); fine for a voice log.
    static func downsample24k(_ s: [Float]) -> [Float] {
        let n = s.count * 2 / 3
        return (0..<n).map { j in
            let x = Double(j) * 1.5, i = Int(x), f = Float(x - Double(i))
            return i + 1 < s.count ? s[i] * (1 - f) + s[i + 1] * f : s[i]
        }
    }
}

/// Writes one call to an AAC .m4a plus a transcript .txt next to it. Thread-safe: the mic tap and the
/// socket callback both feed it.
final class CallRecorder {
    let url: URL
    let started = Date()
    private var file: AVAudioFile?
    private var mix = RecordingMix()
    private var transcript: [String] = []
    private let lock = NSLock()
    private let format = AVAudioFormat(standardFormatWithSampleRate: RecordingMix.rate, channels: 1)!

    init(url: URL) throws {
        self.url = url
        file = try AVAudioFile(forWriting: url, settings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: RecordingMix.rate,
            AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: 32_000,
        ], commonFormat: .pcmFormatFloat32, interleaved: false)
    }

    func mic(_ pcm16: Data, silent: Bool) {
        var s = PCM.samples(fromPCM16: pcm16)
        if silent { s = [Float](repeating: 0, count: s.count) }
        locked { write(mix.addMic(s)) }
    }

    func assistant(_ pcm16: Data) {
        let s = RecordingMix.downsample24k(PCM.samples(fromPCM16: pcm16))
        locked { mix.addAssistant(s) }
    }

    func interrupt() { locked { mix.interrupt() } }

    func note(_ event: CallEvent) {
        guard let text = event.text, event.type == "transcript" || event.type == "reply" else { return }
        let t = Int(Date().timeIntervalSince(started))
        let who = event.type == "transcript" ? "나" : "박비서" + (event.route.map { " (\($0))" } ?? "")
        locked { transcript.append(String(format: "[%02d:%02d] ", t / 60, t % 60) + "\(who): \(text)") }
    }

    /// Flushes the tail, closes the file and writes the transcript sidecar.
    func finish() {
        locked {
            write(mix.finish())
            file = nil // AVAudioFile finalizes the m4a when released
            let text = transcript.joined(separator: "\n") + "\n"
            try? text.write(to: Recordings.transcriptURL(for: url), atomically: true, encoding: .utf8)
        }
    }

    private func write(_ samples: [Float]) {
        guard let file, !samples.isEmpty,
              let buf = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count)) else { return }
        buf.frameLength = AVAudioFrameCount(samples.count)
        samples.withUnsafeBufferPointer { buf.floatChannelData![0].update(from: $0.baseAddress!, count: samples.count) }
        try? file.write(from: buf)
    }

    private func locked(_ body: () -> Void) { lock.lock(); body(); lock.unlock() }
}

/// Recordings live in Documents/Recordings as `<session>_<yyyyMMdd-HHmmss>.m4a` + `.txt`.
enum Recordings {
    static var directory: URL {
        let dir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("Recordings")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    static func fileName(session: String, start: Date) -> String {
        let safe = String(session.map { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" ? $0 : "-" }).prefix(60)
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyyMMdd-HHmmss"
        return "\(safe.isEmpty ? "call" : String(safe))_\(f.string(from: start)).m4a"
    }

    static func transcriptURL(for audio: URL) -> URL { audio.deletingPathExtension().appendingPathExtension("txt") }

    /// Newest first.
    static func list(in dir: URL = directory) -> [URL] {
        let files = (try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.creationDateKey])) ?? []
        return files.filter { $0.pathExtension == "m4a" }.sorted { $0.lastPathComponent > $1.lastPathComponent }
    }

    static func delete(_ audio: URL) {
        try? FileManager.default.removeItem(at: audio)
        try? FileManager.default.removeItem(at: transcriptURL(for: audio))
    }

    /// Removes recordings that never got audio (an app killed mid-call leaves an empty file) and
    /// transcripts whose audio is gone.
    static func cleanup(in dir: URL = directory, minBytes: Int = 1024) {
        let fm = FileManager.default
        for url in (try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.fileSizeKey])) ?? [] {
            switch url.pathExtension {
            case "m4a":
                let size = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
                if size < minBytes { delete(url) }
            case "txt":
                if !fm.fileExists(atPath: url.deletingPathExtension().appendingPathExtension("m4a").path) { try? fm.removeItem(at: url) }
            default: break
            }
        }
    }
}
