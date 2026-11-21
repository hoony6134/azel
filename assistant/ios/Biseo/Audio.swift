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
        return Data(bytes: samples[0], count: Int(out.frameLength) * 2) // iOS is little-endian
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

/// Mic tap → PCM16 16 kHz chunks, and an ordered PCM16 24 kHz player.
final class AudioIO {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    var onMic: ((Data) -> Void)?

    init() {
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: PCM.speakerFormat)
    }

    /// Call after CallKit activated the audio session.
    func start() throws {
        let input = engine.inputNode
        try? input.setVoiceProcessingEnabled(true) // echo cancellation on top of .voiceChat
        let format = input.outputFormat(forBus: 0)
        guard let converter = AVAudioConverter(from: format, to: PCM.micFormat) else { throw AVError(.unknown) }
        converter.downmix = true
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            if let data = PCM.pcm16Data(from: buffer, converter: converter) { self?.onMic?(data) }
        }
        try engine.start()
        player.play()
    }

    func play(_ pcm16: Data) {
        guard engine.isRunning, let buf = PCM.floatBuffer(fromPCM16: pcm16) else { return }
        player.scheduleBuffer(buf)
    }

    /// `interrupt`: stop() drops every scheduled buffer; play() makes the node ready for the next reply.
    func flush() {
        player.stop()
        if engine.isRunning { player.play() }
    }

    func stop() {
        player.stop()
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
    }
}
