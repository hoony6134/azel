import AVFoundation

/// Wake-word enrollment ("호출어 등록"): short clips of the user saying the name, checked here
/// before upload. Clips live only in memory until uploaded (the server keeps them on the Mac).
enum WakeClip {
    static let rate = 16_000.0
    static let frame = 320                       // 20 ms at 16 kHz

    enum Problem: Equatable {
        case tooQuiet, clipped, tooShort, tooLong, noisy, noSpeech

        /// What the retry prompt says.
        var message: String {
            switch self {
            case .tooQuiet: return "소리가 너무 작아요. 조금 더 또렷하게 말해 주세요."
            case .clipped: return "소리가 너무 커서 찌그러졌어요. 조금 떨어져서 말해 주세요."
            case .tooShort: return "너무 짧아요. 이름을 끝까지 말해 주세요."
            case .tooLong: return "너무 길어요. 이름만 한 번 말해 주세요."
            case .noisy: return "주변이 시끄러워요. 조용한 곳에서 다시 해 주세요."
            case .noSpeech: return "목소리가 들리지 않았어요. 다시 말해 주세요."
            }
        }
    }

    /// RMS in dBFS of 20 ms frames.
    static func frameLevels(_ s: [Float]) -> [Double] {
        stride(from: 0, to: s.count, by: frame).map { start in
            let end = min(s.count, start + frame)
            var sum: Double = 0
            for i in start..<end { sum += Double(s[i]) * Double(s[i]) }
            return 20 * log10(max(1e-9, (sum / Double(max(1, end - start))).squareRoot()))
        }
    }

    /// Speech span as a frame range: frames louder than the noise floor + 12 dB (and −45 dBFS).
    static func speechSpan(_ levels: [Double]) -> Range<Int>? {
        guard !levels.isEmpty else { return nil }
        let floor = levels.sorted()[levels.count / 10]           // 10th percentile ≈ background
        let threshold = max(floor + 12, -45)
        guard let first = levels.firstIndex(where: { $0 > threshold }),
              let last = levels.lastIndex(where: { $0 > threshold }) else { return nil }
        return first..<(last + 1)
    }

    /// Quality check (first problem found), or nil when the clip is good.
    static func check(_ s: [Float]) -> Problem? {
        let levels = frameLevels(s)
        guard let span = speechSpan(levels) else { return .noSpeech }
        let seconds = Double(span.count) * Double(frame) / rate
        let clippedCount = s.lazy.filter { abs($0) >= 0.99 }.count
        if Double(clippedCount) / Double(max(1, s.count)) > 0.001 { return .clipped }
        let speech = Array(levels[span])
        let speechLevel = speech.sorted()[speech.count / 2]      // median speech frame
        if speechLevel < -38 { return .tooQuiet }
        if seconds < 0.3 { return .tooShort }
        if seconds > 2.0 { return .tooLong }
        let quiet = levels.enumerated().filter { !span.contains($0.offset) }.map(\.element)
        if let noise = quiet.sorted().dropFirst(quiet.count / 2).first, speechLevel - noise < 15 { return .noisy }
        return nil
    }

    /// The server takes 0.4–2.5 s clips: the speech span with 0.2 s of padding on each side,
    /// capped at 2.5 s. nil when there is no speech.
    static func trimmed(_ s: [Float]) -> [Float]? {
        guard let span = speechSpan(frameLevels(s)) else { return nil }
        let pad = Int(0.2 * rate)
        let start = max(0, span.lowerBound * frame - pad)
        let end = min(s.count, min(span.upperBound * frame + pad, start + Int(2.5 * rate)))
        return Array(s[start..<end])
    }

    static func uploadable(_ s: [Float]) -> Bool { (Int(0.4 * rate)...Int(2.5 * rate)).contains(s.count) }

    /// 16-bit PCM mono WAV at 16 kHz.
    static func wav(_ s: [Float]) -> Data {
        var d = Data()
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        let bytes = UInt32(s.count * 2)
        d.append(contentsOf: Array("RIFF".utf8)); u32(36 + bytes); d.append(contentsOf: Array("WAVE".utf8))
        d.append(contentsOf: Array("fmt ".utf8)); u32(16); u16(1); u16(1); u32(16_000); u32(32_000); u16(2); u16(16)
        d.append(contentsOf: Array("data".utf8)); u32(bytes)
        for v in s { u16(UInt16(bitPattern: Int16(max(-1, min(1, v)) * 32767))) }
        return d
    }
}

/// Voice-activity auto-stop while recording one prompt: stops 0.7 s after speech ends, or at 4 s.
struct WakeVAD {
    static let silenceToStop = 0.7, maxLength = 4.0, startThreshold = -40.0

    private(set) var heardSpeech = false
    private var quietFor = 0.0
    private var elapsed = 0.0

    /// Feed one chunk's level and duration; true when recording should stop.
    mutating func feed(levelDB: Double, seconds: Double) -> Bool {
        elapsed += seconds
        if levelDB > Self.startThreshold { heardSpeech = true; quietFor = 0 } else if heardSpeech { quietFor += seconds }
        return elapsed >= Self.maxLength || (heardSpeech && quietFor >= Self.silenceToStop)
    }
}

/// Records one clip into memory as 16 kHz mono floats, stopping on its own (VAD). Nothing is
/// written to disk.
final class WakeClipRecorder {
    private let engine = AVAudioEngine()
    let levels = Levels()
    private var samples: [Float] = []
    private var vad = WakeVAD()
    private let lock = NSLock()

    /// Calls `done` on the main queue with the clip.
    func record(done: @escaping ([Float]) -> Void) throws {
        guard AudioIO.hardware else { throw AVError(.unknown) }   // no mic in the test host
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard let mono = AVAudioFormat(standardFormatWithSampleRate: format.sampleRate, channels: 1),
              let target = AVAudioFormat(standardFormatWithSampleRate: WakeClip.rate, channels: 1),
              let converter = AVAudioConverter(from: format.channelCount == 1 ? format : mono, to: target) else { throw AVError(.unknown) }
        samples = []
        vad = WakeVAD()
        var finished = false
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            guard let self, !finished, let one = PCM.firstChannel(buffer) else { return }
            levels.update(mic: one)
            let ratio = WakeClip.rate / one.format.sampleRate
            guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: AVAudioFrameCount(Double(one.frameLength) * ratio) + 32) else { return }
            var fed = false
            converter.convert(to: out, error: nil) { _, status in
                if fed { status.pointee = .noDataNow; return nil }
                fed = true; status.pointee = .haveData; return one
            }
            let chunk = Array(UnsafeBufferPointer(start: out.floatChannelData![0], count: Int(out.frameLength)))
            let level = 20 * log10(max(1e-9, Double(Levels.rms(out))))
            lock.lock(); samples += chunk; let stop = vad.feed(levelDB: level, seconds: Double(chunk.count) / WakeClip.rate); lock.unlock()
            if stop {
                finished = true
                DispatchQueue.main.async {
                    input.removeTap(onBus: 0)
                    self.engine.stop()
                    self.lock.lock(); let clip = self.samples; self.samples = []; self.lock.unlock()
                    done(clip)
                }
            }
        }
        try engine.start()
    }

    func cancel() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        lock.lock(); samples = []; lock.unlock()
    }
}
