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
        didSet { if held != oldValue { held ? audio.pause() : audio.resume() } }
    }
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
    static func request(voiceURL: String, token: String, session: String, callID: String?, intent: String? = nil) throws -> URLRequest {
        guard var url = URLComponents(string: voiceURL + "/v1/call") else { throw StartError.notConfigured }
        url.queryItems = [URLQueryItem(name: "session", value: session)]
        if let callID { url.queryItems?.append(URLQueryItem(name: "call_id", value: callID)) }
        if let intent { url.queryItems?.append(URLQueryItem(name: "intent", value: intent)) }
        guard let wsURL = url.url, ["ws", "wss"].contains(wsURL.scheme) else { throw StartError.badURL }
        var req = URLRequest(url: wsURL)
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return req
    }

    /// iOS: call only once the audio session is active. If this throws `.audio` the socket is open: call `stop()`.
    func start(session: String, callID: String? = nil, intent: String? = nil) throws {
        guard let token = Config.token else { throw StartError.notConfigured }
        let task = URLSession.shared.webSocketTask(with: try Self.request(voiceURL: Config.voiceURL, token: token,
                                                                         session: session, callID: callID, intent: intent))
        socket = task
        receivedAny = false
        task.resume()
        audio.onMic = { [weak self, weak task] data in
            guard let self else { return }
            let action = sending ? Self.micAction(muted: muted, held: held) : .drop
            if action == .send { task?.send(.data(data)) { _ in } }
            if action != .drop { recorder?.mic(data, silent: action == .silence) }
        }
        do { try audio.start() } catch { throw StartError.audio }
        receive(task)
    }

    private func receive(_ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self else { return }
            // Audio and interrupts are handled right here, in arrival order; UI hops to main.
            if case .success = result { receivedAny = true }
            switch result {
            case .success(.data(let pcm)):
                audio.play(pcm)
                recorder?.assistant(pcm)
            case .success(.string(let text)):
                guard let event = try? JSONDecoder().decode(CallEvent.self, from: Data(text.utf8)) else { break }
                if event.type == "interrupt" {
                    audio.flush()
                    if held { audio.pause() }
                    recorder?.interrupt()
                }
                recorder?.note(event)
                DispatchQueue.main.async { self.onEvent?(event) }
            case .failure:
                let message = Self.closeMessage(status: (task.response as? HTTPURLResponse)?.statusCode, receivedAny: receivedAny)
                DispatchQueue.main.async { if task === self.socket { self.onClose?(message) } }
                return
            @unknown default:
                break
            }
            receive(task)
        }
    }

    private var gate = EarconGate()

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
        if held { audio.pause() }
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
        stopRecording()
        if let task = socket {
            task.send(.string(#"{"type":"hangup"}"#)) { _ in task.cancel(with: .normalClosure, reason: nil) }
        }
        socket = nil
        audio.onMic = nil
        audio.stop()
        muted = false
        held = false
        sending = true
    }

    /// iOS `didDeactivate`: the system took the audio session away.
    func stopAudio() { audio.stop() }
}
