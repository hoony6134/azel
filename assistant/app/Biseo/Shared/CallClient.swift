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
    /// The socket closed or failed (not after `stop()`), on the main queue.
    var onClose: (() -> Void)?
    /// Read on the audio thread; set from the main thread.
    var muted = false

    private let audio = AudioIO()
    private var socket: URLSessionWebSocketTask?

    init() {}

    /// The token goes in the Authorization header, never the query: query strings end up in proxy logs.
    static func request(voiceURL: String, token: String, session: String, callID: String?) throws -> URLRequest {
        guard var url = URLComponents(string: voiceURL + "/v1/call") else { throw StartError.notConfigured }
        url.queryItems = [URLQueryItem(name: "session", value: session)]
        if let callID { url.queryItems?.append(URLQueryItem(name: "call_id", value: callID)) }
        guard let wsURL = url.url, ["ws", "wss"].contains(wsURL.scheme) else { throw StartError.badURL }
        var req = URLRequest(url: wsURL)
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return req
    }

    /// iOS: call only once the audio session is active. If this throws `.audio` the socket is open: call `stop()`.
    func start(session: String, callID: String? = nil) throws {
        guard let token = Config.token else { throw StartError.notConfigured }
        let task = URLSession.shared.webSocketTask(with: try Self.request(voiceURL: Config.voiceURL, token: token,
                                                                         session: session, callID: callID))
        socket = task
        task.resume()
        audio.onMic = { [weak self, weak task] data in
            if self?.muted == false { task?.send(.data(data)) { _ in } }
        }
        do { try audio.start() } catch { throw StartError.audio }
        receive(task)
    }

    private func receive(_ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self else { return }
            // Audio and interrupts are handled right here, in arrival order; UI hops to main.
            switch result {
            case .success(.data(let pcm)):
                audio.play(pcm)
            case .success(.string(let text)):
                guard let event = try? JSONDecoder().decode(CallEvent.self, from: Data(text.utf8)) else { break }
                if event.type == "interrupt" { audio.flush() }
                DispatchQueue.main.async { self.onEvent?(event) }
            case .failure:
                DispatchQueue.main.async { if task === self.socket { self.onClose?() } }
                return
            @unknown default:
                break
            }
            receive(task)
        }
    }

    /// Says goodbye, closes the socket and stops audio. Safe to call twice.
    func stop() {
        if let task = socket {
            task.send(.string(#"{"type":"hangup"}"#)) { _ in task.cancel(with: .normalClosure, reason: nil) }
        }
        socket = nil
        audio.onMic = nil
        audio.stop()
        muted = false
    }

    /// iOS `didDeactivate`: the system took the audio session away.
    func stopAudio() { audio.stop() }
}
