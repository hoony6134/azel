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
    /// Channel 0 of a multichannel mic buffer as a mono buffer. Mac mics come as 3 channels (9 with
    /// voice processing, where channel 0 is the echo-cancelled voice), and AVAudioConverter's
    /// downmix of those layouts produced pure silence (user bug, 2026-11-21).
    static func firstChannel(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        guard buffer.format.channelCount > 1 else { return buffer }
        guard let src = buffer.floatChannelData,
              let format = AVAudioFormat(standardFormatWithSampleRate: buffer.format.sampleRate, channels: 1),
              let out = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: buffer.frameLength) else { return nil }
        out.frameLength = buffer.frameLength
        out.floatChannelData![0].update(from: src[0], count: Int(buffer.frameLength))
        return out
    }

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
    private var scheduledSamples = 0 // reply samples scheduled since the last flush (24 kHz)
    private var pausedAt: Double?    // playback paused: the clock stands still
    let levels = Levels()
    var onMic: ((Data) -> Void)?
    /// The hardware route or format changed under a running engine (AirPods in or out, a picked
    /// route, 48 kHz → 16 kHz HFP). The engine has stopped itself; the owner restarts it. Main queue.
    var onConfigurationChange: (() -> Void)?
    #if os(macOS)
    /// Core Audio device ids for the next start(); nil = system default.
    var inputDevice: AudioDeviceID?, outputDevice: AudioDeviceID?
    #endif

    init() {
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: PCM.speakerFormat)
        engine.attach(cuePlayer)
        engine.connect(cuePlayer, to: engine.mainMixerNode, format: PCM.speakerFormat)
        NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main) { [weak self] _ in
            guard let self, self.started else { return }
            Log.call.notice("audio route changed: restarting the engine (queued reply audio is dropped)")
            self.onConfigurationChange?()
        }
    }

    /// working/done/error/wake replace whatever cue is playing; ticks queue.
    func playCue(_ cue: Cue) {
        guard engine.isRunning, let buf = cueBuffers[cue] else { return }
        cuePlayer.volume = speechPlaying ? 0.4 : 1
        cuePlayer.scheduleBuffer(buf, at: nil, options: cue == .tick || cue == .pulse ? [] : .interrupts) // a pulse never cuts another cue
        if !cuePlayer.isPlaying { cuePlayer.play() }
    }

    /// Reply audio is still queued or playing.
    var speechPlaying: Bool {
        speechLock.lock(); defer { speechLock.unlock() }
        return (pausedAt ?? ProcessInfo.processInfo.systemUptime) < speechEnd
    }

    /// Real audio hardware. Off in the unit-test host: touching the input node asks for the
    /// microphone, and unsigned test runs flooded the Mac with permission prompts (2026-11-22).
    /// Tests get an in-memory fake with the same bookkeeping.
    nonisolated(unsafe) static var hardware = !Config.isTestHost
    private var fakeRunning = false

    #if os(macOS)
    /// macOS runs the engine's start, restart and teardown here, never on main: voice-processing setup
    /// blocks 0.8–3 s and teardown ~0.3 s, which froze the call panel (2026-11-23). Serial, so a stop
    /// and the next start keep their order.
    private let engineQueue = DispatchQueue(label: "xyz.scian.biseo.audio-engine", qos: .userInitiated)
    private let generationLock = NSLock()
    private var generation = 0   // bumped by stop(): a start queued before it is cancelled

    private var currentGeneration: Int { generationLock.lock(); defer { generationLock.unlock() }; return generation }

    struct Cancelled: Error {}

    /// start() on the engine queue; `done` on main: nil = running, `Cancelled` = a stop() came first.
    func startInBackground(_ done: @escaping (Error?) -> Void) {
        let gen = currentGeneration
        engineQueue.async { [self] in
            var failure: Error?
            if gen != currentGeneration { failure = Cancelled() } else {
                do { try start() } catch { failure = error }
            }
            DispatchQueue.main.async { done(failure) }
        }
    }

    /// The route or device changed: stop and start again on the engine queue; `done` on main.
    func restartInBackground(_ done: @escaping (Error?) -> Void) {
        let gen = currentGeneration
        engineQueue.async { [self] in
            var failure: Error?
            if gen != currentGeneration { failure = Cancelled() } else {
                stopEngine()
                do { try start() } catch { failure = error }
            }
            DispatchQueue.main.async { done(failure) }
        }
    }

    /// Waits for queued engine work (tests).
    func drainEngineQueue() { engineQueue.sync {} }
    #endif

    /// iOS: call after CallKit activated the audio session.
    func start() throws {
        started = true   // from here on stop() has something to undo, even if start throws
        playbackReported = false
        guard Self.hardware else { fakeRunning = true; return }
        let input = engine.inputNode
        #if os(macOS)
        // Set before voice processing so the echo canceller pairs the chosen devices.
        // nil = the system default, set explicitly: a device picked earlier would otherwise stick.
        if let id = inputDevice ?? AudioDevices.defaultID(input: true) { try? input.auAudioUnit.setDeviceID(id) }
        if let id = outputDevice ?? AudioDevices.defaultID(input: false) { try? engine.outputNode.auAudioUnit.setDeviceID(id) }
        #endif
        // Voice processing = echo cancellation + noise suppression; AGC explicitly on, never bypassed.
        try? input.setVoiceProcessingEnabled(true)
        input.isVoiceProcessingBypassed = false
        input.isVoiceProcessingAGCEnabled = true
        // Duck other apps' audio as little as possible during the call (user bug, 2026-11-21).
        #if !os(watchOS)
        input.voiceProcessingOtherAudioDuckingConfiguration = .init(enableAdvancedDucking: false, duckingLevel: .min)
        #endif
        let format = input.outputFormat(forBus: 0)
        guard format.channelCount > 0, format.sampleRate > 0,
              let mono = AVAudioFormat(standardFormatWithSampleRate: format.sampleRate, channels: 1),
              let converter = AVAudioConverter(from: format.channelCount == 1 ? format : mono, to: PCM.micFormat) else {
            Log.call.error("mic format unusable: \(format.channelCount, privacy: .public) ch \(format.sampleRate, privacy: .public) Hz")
            throw AVError(.unknown)
        }
        Log.call.info("mic \(format.channelCount, privacy: .public) ch \(format.sampleRate, privacy: .public) Hz, voice processing \(input.isVoiceProcessingEnabled, privacy: .public), agc \(input.isVoiceProcessingAGCEnabled, privacy: .public)")
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            self?.levels.update(mic: buffer)
            if let one = PCM.firstChannel(buffer), let data = PCM.pcm16Data(from: one, converter: converter) { self?.onMic?(data) }
        }
        let mixer = engine.mainMixerNode
        // Taps run on the engine's messenger queue, which stop() waits on while holding the engine
        // lock: never touch a node or the engine in here (Mac hang on hang-up, 2026-11-22). Levels only.
        mixer.installTap(onBus: 0, bufferSize: 1024, format: mixer.outputFormat(forBus: 0)) { [weak self] buffer, _ in
            self?.levels.update(out: buffer)
        }
        if Config.isTestHost { mixer.outputVolume = 0 }
        try engine.start()
        player.play()
        startDucking()
    }

    private var started = false
    /// The first reply audio since start or flush is logged once: proof that playback really began (2026-11-23: a
    /// wake call's reply was scheduled and then dropped by engine restarts, and nothing in the log showed it).
    private var playbackReported = false

    private var outputName: String {
        #if os(macOS)
        guard let id = outputDevice ?? AudioDevices.defaultID(input: false) else { return "?" }
        return AudioDevices.name(id) ?? "device \(id)"
        #else
        return AVAudioSession.sharedInstance().currentRoute.outputs.first?.portName ?? "?"
        #endif
    }
    private var duckTimer: DispatchSourceTimer?

    /// Cue ducking on the main queue at 30 Hz: −8 dB in ~30 ms while speech plays, back in ~150 ms.
    private func startDucking() {
        duckTimer?.cancel()
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now(), repeating: 1.0 / 30)
        t.setEventHandler { [weak self] in
            guard let self, self.started else { return }
            let v = self.cuePlayer.volume
            self.cuePlayer.volume = Self.duck(volume: v, speech: self.speechPlaying, dt: 1.0 / 30)
        }
        t.resume()
        duckTimer = t
    }

    static func duck(volume: Float, speech: Bool, dt: Float) -> Float {
        let target: Float = speech ? 0.4 : 1
        let tau: Float = target < volume ? 0.03 : 0.15
        return volume + (target - volume) * min(1, dt / tau * 3)
    }

    func play(_ pcm16: Data) {
        guard isRunning else {
            if !playbackReported { playbackReported = true; Log.call.notice("reply audio dropped: engine not running") }
            return
        }
        guard let buf = PCM.floatBuffer(fromPCM16: pcm16) else { return }
        if Self.hardware { player.scheduleBuffer(buf) }
        if !playbackReported {
            playbackReported = true
            Log.call.notice("playback: engine \(self.isRunning, privacy: .public), player \(Self.hardware ? self.player.isPlaying : true, privacy: .public), output \(self.outputName, privacy: .public)")
        }
        let now = ProcessInfo.processInfo.systemUptime
        speechLock.lock()
        speechEnd = max(speechEnd, pausedAt ?? now) + Double(buf.frameLength) / buf.format.sampleRate
        scheduledSamples += Int(buf.frameLength)
        speechLock.unlock()
    }

    /// `interrupt`: stop() drops every scheduled buffer; play() makes the node ready for the next reply.
    func flush() {
        playbackReported = false
        player.volume = 1.0   // a ducked reply that was cut: the next one is at full volume
        player.stop()
        cuePlayer.stop() // interrupt silences cues too
        speechLock.lock(); speechEnd = 0; scheduledSamples = 0; if pausedAt != nil { pausedAt = ProcessInfo.processInfo.systemUptime }; speechLock.unlock()
        if engine.isRunning { player.play() }
    }

    /// A tentative barge-in: the reply plays on, quieter, until the server confirms (interrupt) or clears it (unduck).
    func duck(_ on: Bool) { player.volume = on ? 0.2 : 1.0 }

    /// Hold: playback pauses where it is (queued replies wait); resume() continues them.
    func pause() {
        player.pause()
        speechLock.lock(); if pausedAt == nil { pausedAt = ProcessInfo.processInfo.systemUptime }; speechLock.unlock()
    }

    func resume() {
        if engine.isRunning { player.play() }
        speechLock.lock()
        if let p = pausedAt { speechEnd += ProcessInfo.processInfo.systemUptime - p; pausedAt = nil }
        speechLock.unlock()
    }

    /// Reply samples actually heard since the last flush: scheduled minus what is still queued.
    /// Stands still while paused; gaps between replies don't count.
    var playedSamples: Int {
        speechLock.lock(); defer { speechLock.unlock() }
        return Self.played(scheduled: scheduledSamples, speechEnd: speechEnd, now: pausedAt ?? ProcessInfo.processInfo.systemUptime, rate: 24_000)
    }

    static func played(scheduled: Int, speechEnd: Double, now: Double, rate: Double) -> Int {
        max(0, scheduled - Int((max(0, speechEnd - now) * rate).rounded()))
    }

    /// True while the engine (and so the mic and voice processing) is running.
    var isRunning: Bool { Self.hardware ? engine.isRunning : fakeRunning }
    var voiceProcessingOn: Bool { Self.hardware ? engine.inputNode.isVoiceProcessingEnabled : fakeRunning }

    /// Idempotent. Taps and the ducking timer go first, then the players and the engine.
    /// macOS with real hardware: returns at once, the teardown runs on the engine queue after any
    /// start still in flight (which this cancels if it has not begun).
    func stop() {
        // Always drop what is scheduled, even when the engine already stopped (iOS didDeactivate),
        // so an old reply can't resume when the engine starts again.
        speechLock.lock(); speechEnd = 0; scheduledSamples = 0; pausedAt = nil; speechLock.unlock()
        #if os(macOS)
        generationLock.lock(); generation += 1; generationLock.unlock()
        // Hardware: async, the hang-up returns at once. Fake (tests): sync, still ordered after a queued start.
        if Self.hardware { engineQueue.async { [self] in stopEngine() } } else { engineQueue.sync { stopEngine() } }
        #else
        stopEngine()
        #endif
    }

    private func stopEngine() {
        duckTimer?.cancel(); duckTimer = nil
        defer {
            player.stop()
            cuePlayer.stop()
            speechLock.lock(); speechEnd = 0; scheduledSamples = 0; pausedAt = nil; speechLock.unlock()
        }
        guard started else { return }
        started = false
        guard Self.hardware else { fakeRunning = false; levels.reset(); return }
        engine.inputNode.removeTap(onBus: 0)
        engine.mainMixerNode.removeTap(onBus: 0)
        engine.stop()
        // Voice processing left on keeps ducking other audio after the call; it only runs in calls.
        if engine.inputNode.isVoiceProcessingEnabled { try? engine.inputNode.setVoiceProcessingEnabled(false) }
        levels.reset()
    }
}
