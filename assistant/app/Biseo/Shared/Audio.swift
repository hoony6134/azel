import AVFoundation

/// Wire formats from PROTOCOL.md §1: mic → server PCM16 mono 16 kHz, server → speaker PCM16 mono 24 kHz.
enum PCM {
    static let micFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true)!
    static let speakerFormat = AVAudioFormat(standardFormatWithSampleRate: 24_000, channels: 1)!

    /// Runs one mic buffer through a long-lived converter (keeps resampler state between buffers)
    /// and returns little-endian PCM16 bytes.
    static func pcm16Data(from input: AVAudioPCMBuffer, converter: AVAudioConverter) -> Data? {
        let ratio = converter.outputFormat.sampleRate / input.format.sampleRate
        let capacity = AVAudioFrameCount(Double(input.frameLength) * ratio) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: converter.outputFormat, frameCapacity: capacity) else { return nil }
        var fed = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return input
        }
        guard error == nil, out.frameLength > 0, let samples = out.int16ChannelData else { return nil }
        return Data(bytes: samples[0], count: Int(out.frameLength) * 2) // Apple platforms are little-endian
    }

    /// Server PCM16 bytes → Float32 buffer the player node can schedule.
    /// shortcut: a trailing odd byte is dropped; fine while the server sends whole samples per frame.
    static func floatBuffer(fromPCM16 data: Data, format: AVAudioFormat = speakerFormat) -> AVAudioPCMBuffer? {
        let frames = data.count / 2
        guard frames > 0, let buf = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)) else { return nil }
        buf.frameLength = AVAudioFrameCount(frames)
        let dst = buf.floatChannelData![0]
        data.withUnsafeBytes { raw in
            for i in 0..<frames {
                dst[i] = Float(Int16(littleEndian: raw.loadUnaligned(fromByteOffset: i * 2, as: Int16.self))) / 32768
            }
        }
        return buf
    }
}

extension PCM {
    /// PCM16 little-endian bytes → Float samples in -1…1 (trailing odd byte dropped).
    static func samples(fromPCM16 data: Data) -> [Float] {
        data.withUnsafeBytes { raw in
            (0..<(data.count / 2)).map { Float(Int16(littleEndian: raw.loadUnaligned(fromByteOffset: $0 * 2, as: Int16.self))) / 32768 }
        }
    }
}

/// Mic and output loudness in 0…1 for the visualizer (DESIGN §6.1). Written on audio threads, read
/// every frame by the jar's Canvas; deliberately not @Published.
final class Levels: @unchecked Sendable {
    static let silent = Levels()

    init(mic: Float = 0, out: Float = 0) { _mic = mic; _out = out }
    private let lock = NSLock()
    private var _mic: Float = 0, _out: Float = 0, _loudAt: Double = -.infinity
    var mic: Float { lock.lock(); defer { lock.unlock() }; return _mic }
    var out: Float { lock.lock(); defer { lock.unlock() }; return _out }
    /// Last time (monotonic seconds) the mic level was above 0.25: the user is talking.
    var micLoudAt: Double { lock.lock(); defer { lock.unlock() }; return _loudAt }

    /// RMS → 0…1: −50 dBFS → 0, −6 dBFS → 1.
    static func level(rms: Float) -> Float { max(0, min(1, (20 * log10(max(rms, 1e-5)) + 50) / 44)) }

    /// One-pole smoothing: attack 40 ms, release 180 ms.
    static func smooth(_ previous: Float, _ target: Float, dt: Float) -> Float {
        let tau: Float = target > previous ? 0.04 : 0.18
        return previous + (target - previous) * (1 - exp(-dt / tau))
    }

    static func rms(_ buffer: AVAudioPCMBuffer) -> Float {
        guard let data = buffer.floatChannelData, buffer.frameLength > 0 else { return 0 }
        var sum: Float = 0
        for i in 0..<Int(buffer.frameLength) { sum += data[0][i] * data[0][i] }
        return (sum / Float(buffer.frameLength)).squareRoot()
    }

    func update(mic buffer: AVAudioPCMBuffer) {
        update(\._mic, buffer)
        if mic > 0.25 { lock.lock(); _loudAt = ProcessInfo.processInfo.systemUptime; lock.unlock() }
    }
    func update(out buffer: AVAudioPCMBuffer) { update(\._out, buffer) }
    func reset() { lock.lock(); _mic = 0; _out = 0; _loudAt = -.infinity; lock.unlock() }

    private func update(_ key: ReferenceWritableKeyPath<Levels, Float>, _ buffer: AVAudioPCMBuffer) {
        let target = Self.level(rms: Self.rms(buffer))
        let dt = Float(buffer.frameLength) / Float(buffer.format.sampleRate)
        lock.lock(); self[keyPath: key] = Self.smooth(self[keyPath: key], target, dt: dt); lock.unlock()
    }
}

/// Mic tap → PCM16 16 kHz chunks, and an ordered PCM16 24 kHz player.
final class AudioIO {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    /// Earcons (DESIGN §14.2): a second player under the speech, ducked while speech plays.
    private let cuePlayer = AVAudioPlayerNode()
    private lazy var cueBuffers: [Cue: AVAudioPCMBuffer] = Dictionary(uniqueKeysWithValues: Cue.allCases.map { ($0, Earcon.buffer($0)) })
    private let speechLock = NSLock()
    private var speechEnd = 0.0      // monotonic time the queued speech runs out
    let levels = Levels()
    var onMic: ((Data) -> Void)?
    #if os(macOS)
    /// Core Audio device ids for the next start(); nil = system default.
    var inputDevice: AudioDeviceID?, outputDevice: AudioDeviceID?
    #endif

    init() {
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: PCM.speakerFormat)
        engine.attach(cuePlayer)
        engine.connect(cuePlayer, to: engine.mainMixerNode, format: PCM.speakerFormat)
    }

    /// working/done/error/wake replace whatever cue is playing; ticks queue.
    func playCue(_ cue: Cue) {
        guard engine.isRunning, let buf = cueBuffers[cue] else { return }
        cuePlayer.volume = speechPlaying ? 0.4 : 1
        cuePlayer.scheduleBuffer(buf, at: nil, options: cue == .tick ? [] : .interrupts)
        if !cuePlayer.isPlaying { cuePlayer.play() }
    }

    private var speechPlaying: Bool {
        speechLock.lock(); defer { speechLock.unlock() }
        return ProcessInfo.processInfo.systemUptime < speechEnd
    }

    /// iOS: call after CallKit activated the audio session.
    func start() throws {
        let input = engine.inputNode
        #if os(macOS)
        // Set before voice processing so the echo canceller pairs the chosen devices.
        if let id = inputDevice { try? input.auAudioUnit.setDeviceID(id) }
        if let id = outputDevice { try? engine.outputNode.auAudioUnit.setDeviceID(id) }
        #endif
        try? input.setVoiceProcessingEnabled(true) // echo cancellation on top of .voiceChat
        let format = input.outputFormat(forBus: 0)
        guard let converter = AVAudioConverter(from: format, to: PCM.micFormat) else { throw AVError(.unknown) }
        converter.downmix = true
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            self?.levels.update(mic: buffer)
            if let data = PCM.pcm16Data(from: buffer, converter: converter) { self?.onMic?(data) }
        }
        let mixer = engine.mainMixerNode
        mixer.installTap(onBus: 0, bufferSize: 1024, format: mixer.outputFormat(forBus: 0)) { [weak self] buffer, _ in
            guard let self else { return }
            levels.update(out: buffer)
            // Ducking: −8 dB in ~30 ms while speech plays, back in ~150 ms.
            let target: Float = speechPlaying ? 0.4 : 1
            let dt = Float(buffer.frameLength) / Float(buffer.format.sampleRate)
            let tau: Float = target < cuePlayer.volume ? 0.03 : 0.15
            cuePlayer.volume += (target - cuePlayer.volume) * min(1, dt / tau * 3)
        }
        try engine.start()
        player.play()
    }

    func play(_ pcm16: Data) {
        guard engine.isRunning, let buf = PCM.floatBuffer(fromPCM16: pcm16) else { return }
        player.scheduleBuffer(buf)
        let now = ProcessInfo.processInfo.systemUptime
        speechLock.lock(); speechEnd = max(speechEnd, now) + Double(buf.frameLength) / buf.format.sampleRate; speechLock.unlock()
    }

    /// `interrupt`: stop() drops every scheduled buffer; play() makes the node ready for the next reply.
    func flush() {
        player.stop()
        cuePlayer.stop() // interrupt silences cues too
        speechLock.lock(); speechEnd = 0; speechLock.unlock()
        if engine.isRunning { player.play() }
    }

    /// Hold: playback pauses where it is (queued replies wait); resume() continues them.
    func pause() { player.pause() }
    func resume() { if engine.isRunning { player.play() } }

    func stop() {
        player.stop()
        cuePlayer.stop()
        engine.inputNode.removeTap(onBus: 0)
        engine.mainMixerNode.removeTap(onBus: 0)
        engine.stop()
        levels.reset()
    }
}
