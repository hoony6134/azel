import Foundation

/// `WS /v1/call` (PROTOCOL.md §1): streams the mic up, plays replies, honours `interrupt`.
/// The platform shell (CallKit on iOS, the floating panel on macOS) owns the call UI and lifecycle.
final class CallClient {
    enum StartError: LocalizedError {
        case notConfigured, badURL, audio
        var errorDescription: String? {
            switch self {
            case .notConfigured: return "서버 설정이 없습니다"
            case .badURL: return "잘못된 음성 서버 주소"
            case .audio: return "오디오 시작 실패"
            }
        }
    }

    /// Server text events, on the main queue.
    var onEvent: ((CallEvent) -> Void)?
    /// The socket closed or failed (not after `stop()`), on the main queue, with a message for the
    /// user when the call never got going.
    var onClose: ((String?) -> Void)?
    private var receivedAny = false
    /// Something the user should see without the call ending (e.g. the mic sends only silence).
    var onWarning: ((String) -> Void)?
    private let statsLock = NSLock()
    private var sentBytes = 0, sentPeak: Int16 = 0, receivedAudioBytes = 0
    private var silenceCheck: DispatchWorkItem?

    static let silentMicMessage = "마이크 소리가 들어오지 않아요. 입력 장치와 마이크 권한을 확인해 주세요."

    /// Peak sample of PCM16 bytes (all-zero chunks mean the mic path is broken, not a quiet room).
    static func peak(_ pcm16: Data) -> Int16 {
        pcm16.withUnsafeBytes { raw in
            (0..<(pcm16.count / 2)).reduce(Int16(0)) { m, i in
                let v = Int16(littleEndian: raw.loadUnaligned(fromByteOffset: i * 2, as: Int16.self))
                return max(m, v == .min ? .max : abs(v))
            }
        }
    }

    /// 401/403 on the WebSocket handshake: wrong token, or a server that doesn't take the header yet.
    static func closeMessage(status: Int?, receivedAny: Bool) -> String? {
        if status == 401 || status == 403 { return "토큰이 맞지 않거나 서버가 아직 업데이트되지 않았어요" }
        if !receivedAny { return "음성 서버에 연결하지 못했어요" + (status.map { " (HTTP \($0))" } ?? "") }
        return nil
    }
    /// Read on the audio thread; set from the main thread.
    var muted = false
    /// Hold: no mic goes up, playback pauses, the recording pauses. The socket stays open.
    var held = false {
        didSet { if held != oldValue { playback.held = held; applyGate() } }
    }
    /// Playback runs only when neither the user (hold) nor the server (pause) stopped it.
    private var playback = PlaybackGate()

    private func applyGate() { playback.playing ? audio.resume() : audio.pause() }
    var isConnected: Bool { socket != nil }
    /// Mac push-to-talk: false between key presses, so nothing goes up (the socket stays open).
    var sending = true
    var isRecording: Bool { recorder != nil }

    let audio = AudioIO()
    private var socket: URLSessionWebSocketTask?
    private let lock = NSLock()
    private var _recorder: CallRecorder?
    private var recorder: CallRecorder? {
        get { lock.lock(); defer { lock.unlock() }; return _recorder }
        set { lock.lock(); _recorder = newValue; lock.unlock() }
    }

    enum MicAction: Equatable { case send, silence, drop }
    /// What happens to one mic chunk: held drops it (and pauses the recording timeline); muted keeps
    /// the recording's clock running with silence but sends nothing.
    static func micAction(muted: Bool, held: Bool) -> MicAction { held ? .drop : muted ? .silence : .send }

    init() {}

    /// The token goes in the Authorization header, never the query: query strings end up in proxy logs.
    /// `intent`: e.g. "briefing" for 오늘 브리핑 / the Siri briefing, so the server opens with it.
    static func request(voiceURL: String, token: String, session: String, callID: String?, intent: String? = nil,
                        persona: String? = nil) throws -> URLRequest {
        guard var url = URLComponents(string: voiceURL + "/v1/call") else { throw StartError.notConfigured }
        url.queryItems = [URLQueryItem(name: "session", value: session)]
        if let callID { url.queryItems?.append(URLQueryItem(name: "call_id", value: callID)) }
        if let intent { url.queryItems?.append(URLQueryItem(name: "intent", value: intent)) }
        if let persona { url.queryItems?.append(URLQueryItem(name: "persona", value: persona)) }
        guard let wsURL = url.url, ["ws", "wss"].contains(wsURL.scheme) else { throw StartError.badURL }
        var req = URLRequest(url: wsURL)
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return req
    }

    /// iOS: call only once the audio session is active. If this throws `.audio` the socket is open: call `stop()`.
    func start(session: String, callID: String? = nil, intent: String? = nil, persona: String? = nil) throws {
        guard let token = Config.token else { throw StartError.notConfigured }
        let task = URLSession.shared.webSocketTask(with: try Self.request(voiceURL: Config.voiceURL, token: token,
                                                                         session: session, callID: callID, intent: intent, persona: persona))
        socket = task
        receivedAny = false
        statsLock.lock(); sentBytes = 0; sentPeak = 0; receivedAudioBytes = 0; statsLock.unlock()
        Log.call.info("call start session=\(session, privacy: .public) intent=\(intent ?? "-", privacy: .public) host=\(task.originalRequest?.url?.host ?? "?", privacy: .public)")
        task.resume()
        audio.onMic = { [weak self, weak task] data in
            guard let self else { return }
            let action = sending ? Self.micAction(muted: muted, held: held) : .drop
            if action == .send {
                task?.send(.data(data)) { error in if let error { Log.call.error("mic send failed: \(error.localizedDescription, privacy: .public)") } }
                let p = Self.peak(data)
                statsLock.lock(); sentBytes += data.count; sentPeak = max(sentPeak, p); statsLock.unlock()
            }
            if action != .drop { recorder?.mic(data, silent: action == .silence) }
        }
        do { try audio.start() } catch {
            Log.call.error("audio start failed: \(error.localizedDescription, privacy: .public)")
            throw StartError.audio
        }
        receive(task)
        pulseTimer?.invalidate()
        wasPlaying = false
        pulseTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self else { return }
            let playing = audio.speechPlaying
            if playing != wasPlaying { wasPlaying = playing; onPlayback?(playing) }
            if let cue = thinking.tick(now: ProcessInfo.processInfo.systemUptime) { self.cue(cue, listening: false) }
        }
        // 4 s in: if everything sent so far is exact zeros, the mic path is broken; say so.
        let check = DispatchWorkItem { [weak self] in
            guard let self else { return }
            statsLock.lock(); let bytes = sentBytes, peak = sentPeak, got = receivedAudioBytes; statsLock.unlock()
            Log.call.info("call 4s: sent \(bytes, privacy: .public) B peak \(peak, privacy: .public), received audio \(got, privacy: .public) B")
            if bytes > 0 && peak == 0 { onWarning?(Self.silentMicMessage) }
        }
        silenceCheck = check
        DispatchQueue.main.asyncAfter(deadline: .now() + 4, execute: check)
    }

    private func receive(_ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self else { return }
            // Audio and interrupts are handled right here, in arrival order; UI hops to main.
            if case .success = result, !receivedAny { receivedAny = true; Log.call.info("call socket open, first message") }
            switch result {
            case .success(.data(let pcm)):
                audio.play(pcm)
                DispatchQueue.main.async { self.thinking.audioArrived() }
                statsLock.lock(); receivedAudioBytes += pcm.count; statsLock.unlock()
                recorder?.assistant(pcm)
            case .success(.string(let text)):
                guard let event = try? JSONDecoder().decode(CallEvent.self, from: Data(text.utf8)) else { break }
                if event.type != "activity" { Log.call.info("event \(event.type, privacy: .public) \(event.value ?? "", privacy: .public)") }
                if event.type == "error" { Log.call.error("server error: \(event.message ?? "", privacy: .public)") }
                // Event order matters for audio, so these run here, before the UI hop.
                switch event.type {
                case "interrupt":
                    audio.flush()
                    playback.apply(event.type)
                    if !playback.playing { audio.pause() }
                    recorder?.interrupt()
                case "pause", "resume":
                    playback.apply(event.type)   // tentative barge-in: stop playback, keep the queue
                    applyGate()
                default: break
                }
                recorder?.note(event)
                DispatchQueue.main.async {
                    if let cue = self.thinking.event(event.type, value: event.value, now: ProcessInfo.processInfo.systemUptime) {
                        self.cue(cue, listening: false)
                    }
                    self.onEvent?(event)
                }
            case .failure:
                let status = (task.response as? HTTPURLResponse)?.statusCode
                let message = Self.closeMessage(status: status, receivedAny: receivedAny)
                Log.call.error("call socket closed: status=\(status ?? -1, privacy: .public) code=\(task.closeCode.rawValue, privacy: .public)")
                DispatchQueue.main.async { if task === self.socket { self.onClose?(message) } }
                return
            @unknown default:
                break
            }
            receive(task)
        }
    }

    private var gate = EarconGate()
    /// Main thread only.
    private var thinking = ThinkingCues()
    private var pulseTimer: Timer?
    /// Playback started or drained (main queue): the UI keeps 말하는 중 until the queue is empty.
    var onPlayback: ((Bool) -> Void)?
    private var wasPlaying = false

    /// What the call UI shows: the server may report listening/turn_done before the reply's last
    /// audio has played, so playing audio wins over those.
    static func displayState(server: String, playing: Bool) -> String {
        playing && (server == "listening" || server.isEmpty) ? "speaking" : server
    }

    /// Plays a work sound if the gate allows; cues that would land on the user's voice wait up to
    /// 300 ms, then drop. Main thread.
    func cue(_ cue: Cue, listening: Bool, waited: Double = 0) {
        let now = ProcessInfo.processInfo.systemUptime
        switch gate.decide(cue, now: now, listening: listening, userLoudAt: audio.levels.micLoudAt, waited: waited, enabled: Earcon.enabled) {
        case .play: audio.playCue(cue)
        case .drop: break
        case .wait:
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in self?.cue(cue, listening: listening, waited: waited + 0.05) }
        }
    }

    /// Push-to-talk key-up: ~700 ms of zero PCM so the server VAD closes the turn (DESIGN §12.5).
    func sendSilence(milliseconds: Int = 700) {
        socket?.send(.data(Self.silence(milliseconds: milliseconds))) { _ in }
    }

    /// Zero PCM16 at 16 kHz mono.
    static func silence(milliseconds: Int) -> Data { Data(count: 16 * milliseconds * 2) }

    /// Tap-to-interrupt (DESIGN §12.3): tell the server and drop local playback at once, without
    /// waiting for its `interrupt` echo.
    func interrupt() {
        socket?.send(.string(#"{"type":"interrupt"}"#)) { _ in }
        audio.flush()
        playback.apply("interrupt")
        if !playback.playing { audio.pause() }
        recorder?.interrupt()
    }

    /// Starts writing both sides to Documents/Recordings. Only on the user's tap.
    func startRecording(session: String) throws {
        guard recorder == nil else { return }
        recorder = try CallRecorder(url: Recordings.directory.appendingPathComponent(Recordings.fileName(session: session, start: Date())))
    }

    func stopRecording() {
        let r = recorder
        recorder = nil
        r?.finish()
    }

    /// iOS: CallKit gave the audio session back (after a hold or interruption); macOS: device change.
    func restartAudio() throws {
        audio.stop()
        do { try audio.start() } catch { throw StartError.audio }
        if held { audio.pause() }
    }

    /// Says goodbye, closes the socket and stops audio. Safe to call twice.
    func stop() {
        silenceCheck?.cancel()
        pulseTimer?.invalidate()
        pulseTimer = nil
        thinking = ThinkingCues()
        if wasPlaying { wasPlaying = false; onPlayback?(false) }
        if socket != nil {
            statsLock.lock(); let bytes = sentBytes, got = receivedAudioBytes; statsLock.unlock()
            Log.call.info("call stop: sent \(bytes, privacy: .public) B, received audio \(got, privacy: .public) B")
        }
        stopRecording()
        if let task = socket {
            task.send(.string(#"{"type":"hangup"}"#)) { _ in task.cancel(with: .normalClosure, reason: nil) }
        }
        socket = nil
        audio.onMic = nil
        audio.stop()
        muted = false
        held = false
        playback = PlaybackGate()
        sending = true
    }

    /// iOS `didDeactivate`: the system took the audio session away.
    func stopAudio() { audio.stop() }
}

/// Who may stop playback: the user (hold) and the server (`pause` on a tentative barge-in).
/// `pause` keeps the queued audio; `resume` continues it; `interrupt` drops it and clears pause.
struct PlaybackGate: Equatable {
    var held = false
    private(set) var serverPaused = false

    var playing: Bool { !held && !serverPaused }

    mutating func apply(_ eventType: String) {
        switch eventType {
        case "pause": serverPaused = true
        case "resume", "interrupt": serverPaused = false
        default: break
        }
    }
}
