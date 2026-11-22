import AVFoundation

/// The five work sounds (DESIGN §14.1), synthesized in-app at the speaker rate. Pure, so the
/// envelope/level rules are unit-tested.
enum Earcon {
    static let rate = 24_000.0

    enum Wave { case sine, triangle }
    struct Note { var freq: Double; var start: Double; var attack: Double; var hold: Double; var decay: Double }

    static func spec(_ cue: Cue) -> (notes: [Note], wave: Wave, partial: (mult: Double, db: Double)?, lowpass: Double?, peakDB: Double, maxLength: Double) {
        switch cue {
        case .working:
            return ([Note(freq: 587.33, start: 0, attack: 0.008, hold: 0.020, decay: 0.070),
                     Note(freq: 880.00, start: 0.118, attack: 0.008, hold: 0.020, decay: 0.070)],
                    .sine, (2, -18), nil, -26, 0.220)
        case .tick:
            return ([Note(freq: 1318.5, start: 0, attack: 0.002, hold: 0, decay: 0.045)], .triangle, nil, nil, -32, 0.050)
        case .done:
            return ([Note(freq: 880.00, start: 0, attack: 0.006, hold: 0, decay: 0.090),
                     Note(freq: 739.99, start: 0.076, attack: 0.006, hold: 0, decay: 0.130)],
                    .sine, (2, -20), nil, -24, 0.200)
        case .error:
            return ([Note(freq: 392.00, start: 0, attack: 0.010, hold: 0.040, decay: 0.100),
                     Note(freq: 369.99, start: 0.150, attack: 0.010, hold: 0.040, decay: 0.100)],
                    .triangle, nil, 2000, -22, 0.300)
        case .wake:
            return ([Note(freq: 1174.66, start: 0, attack: 0.003, hold: 0, decay: 0.220)], .sine, (2.76, -20), nil, -20, 0.250)
        // Provisional (designer to confirm): a soft single-note "들었어요" and a barely-there pulse.
        case .ack:
            return ([Note(freq: 783.99, start: 0, attack: 0.004, hold: 0, decay: 0.060)], .sine, (2, -20), nil, -30, 0.080)
        case .pulse: // a heartbeat: a soft "lub", then a lighter "dub" (small: the whole thing peaks at −34 dB)
            return ([Note(freq: 110, start: 0, attack: 0.012, hold: 0.010, decay: 0.110),
                     Note(freq: 98, start: 0.19, attack: 0.012, hold: 0.004, decay: 0.090)],
                    .sine, (2, -9), nil, -34, 0.400)
        }
    }

    static func render(_ cue: Cue) -> [Float] {
        let s = spec(cue)
        let end = s.notes.map { $0.start + $0.attack + $0.hold + $0.decay }.max()!
        let count = Int(min(end, s.maxLength) * rate)
        var out = [Double](repeating: 0, count: count)
        func osc(_ phase: Double) -> Double {
            switch s.wave {
            case .sine: return sin(2 * .pi * phase)
            case .triangle: let p = phase - floor(phase); return 4 * abs(p - 0.5) - 1
            }
        }
        for n in s.notes {
            let first = Int(n.start * rate)
            var gain = 1.0
            let decayStep = exp(-1 / (n.decay / 5 * rate)) // τ = decay/5 → about −43 dB at `decay`
            for i in 0..<Int((n.attack + n.hold + n.decay) * rate) where first + i < count {
                let t = Double(i) / rate
                let env: Double
                if t < n.attack { env = t / n.attack }
                else if t < n.attack + n.hold { env = 1 }
                else { gain *= decayStep; env = gain }
                var v = osc(n.freq * t)
                if let p = s.partial { v += pow(10, p.db / 20) * sin(2 * .pi * n.freq * p.mult * t) }
                out[first + i] += v * env
            }
        }
        if let cutoff = s.lowpass { // one-pole low-pass
            let a = exp(-2 * .pi * cutoff / rate)
            var y = 0.0
            for i in out.indices { y = (1 - a) * out[i] + a * y; out[i] = y }
        }
        let peak = out.map(abs).max() ?? 0
        let scale = peak > 0 ? pow(10, s.peakDB / 20) / peak : 0
        let fade = Int(0.005 * rate) // 5 ms linear fade to zero: no clicks
        return out.enumerated().map { i, v in
            let left = count - 1 - i
            return Float(v * scale * (left < fade ? Double(left) / Double(fade) : 1))
        }
    }

    static func buffer(_ cue: Cue) -> AVAudioPCMBuffer {
        let samples = render(cue)
        let buf = AVAudioPCMBuffer(pcmFormat: PCM.speakerFormat, frameCapacity: AVAudioFrameCount(samples.count))!
        buf.frameLength = AVAudioFrameCount(samples.count)
        samples.withUnsafeBufferPointer { buf.floatChannelData![0].update(from: $0.baseAddress!, count: samples.count) }
        return buf
    }

    /// App-local "작업 효과음" toggle, default on.
    static var enabled: Bool { UserDefaults.standard.object(forKey: "workSounds") as? Bool ?? true }
}

/// When a cue may play (DESIGN §14.2): never over the user's voice, ticks rate-limited.
struct EarconGate {
    enum Decision: Equatable { case play, drop, wait }
    static let tickInterval = 0.35, maxWait = 0.3, micHold = 0.25

    private var lastTick = -Double.infinity
    /// Last time a cue other than the pulse played; a pulse within 0.7 s of one is skipped.
    private var lastOther = -Double.infinity
    static let pulseQuiet = 0.7

    /// - userLoudAt: last time the mic was above 0.25 while listening.
    /// - waited: how long this cue has already been held back.
    mutating func decide(_ cue: Cue, now: Double, listening: Bool, userLoudAt: Double, waited: Double = 0, enabled: Bool = true) -> Decision {
        guard enabled else { return .drop }
        if cue == .wake { lastOther = now; return .play }
        let userTalking = listening && now - userLoudAt < Self.micHold
        if cue == .pulse { // never queued, never over the user, never right after another cue
            return userTalking || now - lastOther < Self.pulseQuiet ? .drop : .play
        }
        if cue == .tick {
            guard !userTalking, now - lastTick >= Self.tickInterval else { return .drop }
            lastTick = now
            lastOther = now
            return .play
        }
        if userTalking { return waited + 0.05 > Self.maxWait ? .drop : .wait }
        lastOther = now
        return .play
    }
}

/// Outside a call, previews need an active audio session of their own (user bug, 2026-11-21:
/// 들어보기 and 작업 효과음 previews were silent). `.playback` plays even with the ring switch off,
/// as a preview the user just tapped should.
enum PreviewAudio {
    static func activate() {
        #if os(iOS)
        guard !CallManager.shared.inCall else { return } // in a call the CallKit session already plays
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .default)
        try? AVAudioSession.sharedInstance().setActive(true)
        #endif
    }
}

/// "들어 보기": the five cues in a row outside a call, on a fresh engine each time (an engine kept
/// across a call's session changes can come back silent).
enum EarconPreview {
    private static var engine: AVAudioEngine?
    private static var player: AVAudioPlayerNode?
    private static let lock = NSLock()
    private static var _peak: Float = 0
    /// Loudest output sample since the last play(); lets a test confirm sound actually came out.
    static var peak: Float { lock.lock(); defer { lock.unlock() }; return _peak }

    static func play() {
        guard AudioIO.hardware else {
            // Test host: no engine at all; the peak is what the player would have been fed.
            let m = Cue.allCases.map { Earcon.render($0).map(abs).max() ?? 0 }.max() ?? 0
            lock.lock(); _peak = m; lock.unlock()
            return
        }
        PreviewAudio.activate()
        engine?.stop()
        let e = AVAudioEngine(), p = AVAudioPlayerNode()
        e.attach(p)
        e.connect(p, to: e.mainMixerNode, format: PCM.speakerFormat)
        lock.lock(); _peak = 0; lock.unlock()
        if Config.isTestHost { e.mainMixerNode.outputVolume = 0 }   // tests check the level, not the speaker
        p.installTap(onBus: 0, bufferSize: 1024, format: PCM.speakerFormat) { b, _ in
            guard let d = b.floatChannelData else { return }
            var m: Float = 0
            for i in 0..<Int(b.frameLength) { m = max(m, abs(d[0][i])) }
            lock.lock(); _peak = max(_peak, m); lock.unlock()
        }
        do { try e.start() } catch { Log.call.error("earcon preview start failed: \(error.localizedDescription, privacy: .public)"); return }
        let gap = AVAudioPCMBuffer(pcmFormat: PCM.speakerFormat, frameCapacity: 9600)!
        gap.frameLength = 9600 // 400 ms between cues
        gap.floatChannelData![0].update(repeating: 0, count: 9600)
        for cue in Cue.allCases {
            p.scheduleBuffer(Earcon.buffer(cue))
            p.scheduleBuffer(gap)
        }
        p.play()
        engine = e
        player = p
    }
}
