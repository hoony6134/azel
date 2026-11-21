import AVFoundation
import CallKit
import PushKit

/// CallKit + PushKit + `WS /v1/call`. Provider and push callbacks run on the main queue.
final class CallManager: NSObject, ObservableObject {
    static let shared = CallManager()

    struct Line: Identifiable { let id = UUID(); let mine: Bool; let text: String; var route: String? = nil }

    @Published private(set) var inCall = false
    @Published private(set) var state = ""          // listening | thinking | speaking
    @Published private(set) var lines: [Line] = []
    @Published private(set) var muted = false
    @Published var error: String?

    private let provider: CXProvider
    private let controller = CXCallController()
    private let pushRegistry = PKPushRegistry(queue: .main)
    private let audio = AudioIO()
    private var voipToken: Data?
    private var callUUID: UUID?
    private var callID: String?                     // set when answering a call the assistant placed
    private var sessionPrefix = "iphone"            // outgoing calls: "iphone" or "briefing"
    private var incoming: [UUID: (callID: String?, reason: String?)] = [:]
    private var socket: URLSessionWebSocketTask?

    private override init() {
        let config = CXProviderConfiguration()
        config.supportsVideo = false
        config.maximumCallsPerCallGroup = 1
        config.supportedHandleTypes = [.generic]
        provider = CXProvider(configuration: config)
        super.init()
        provider.setDelegate(self, queue: nil)
        pushRegistry.delegate = self
        pushRegistry.desiredPushTypes = [.voIP]
    }

    // MARK: UI actions

    func startCall(prefix: String = "iphone") {
        guard !inCall else { return }
        sessionPrefix = prefix
        let action = CXStartCallAction(call: UUID(), handle: CXHandle(type: .generic, value: "박비서"))
        controller.request(CXTransaction(action: action)) { [weak self] err in
            if let err { DispatchQueue.main.async { self?.error = err.localizedDescription } }
        }
    }

    func hangUp() {
        guard let uuid = callUUID else { return }
        controller.request(CXTransaction(action: CXEndCallAction(call: uuid))) { _ in }
    }

    /// Goes through CallKit so the system call UI's mute stays in sync.
    func toggleMute() {
        guard let uuid = callUUID else { return }
        controller.request(CXTransaction(action: CXSetMutedCallAction(call: uuid, muted: !muted))) { _ in }
    }

    /// Sends the PushKit token to the gateway once both it and the server token exist.
    func registerPush() {
        guard let token = voipToken, Config.token != nil else { return }
        Task { @MainActor in
            do { try await Gateway.registerPush(token: token) } catch { self.error = "푸시 등록 실패: \(error.localizedDescription)" }
        }
    }

    // MARK: Call lifecycle

    private func begin(_ uuid: UUID, callID: String?) {
        callUUID = uuid
        self.callID = callID
        inCall = true
        state = ""
        lines = []
        muted = false
        error = nil
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playAndRecord, mode: .voiceChat)
    }

    /// Runs from `didActivate`: the audio session is ours now.
    private func connect() {
        guard let uuid = callUUID, let token = Config.token,
              var url = URLComponents(string: Config.voiceURL + "/v1/call") else { return fail("서버 설정이 없습니다") }
        url.queryItems = [URLQueryItem(name: "token", value: token), URLQueryItem(name: "session", value: callID ?? Session.current(sessionPrefix))]
        if let callID { url.queryItems?.append(URLQueryItem(name: "call_id", value: callID)) }
        guard let wsURL = url.url else { return fail("잘못된 음성 서버 주소") }

        let task = URLSession.shared.webSocketTask(with: wsURL)
        socket = task
        task.resume()
        audio.onMic = { [weak self, weak task] data in
            if self?.muted == false { task?.send(.data(data)) { _ in } }
        }
        do { try audio.start() } catch { return fail("오디오 시작 실패") }
        receive(task)
        if callID == nil { provider.reportOutgoingCall(with: uuid, connectedAt: nil) }
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
                DispatchQueue.main.async { self.handle(event) }
            case .failure:
                DispatchQueue.main.async { if task === self.socket { self.fail(nil) } }
                return
            @unknown default:
                break
            }
            receive(task)
        }
    }

    private func handle(_ event: CallEvent) {
        switch event.type {
        case "state": state = event.value ?? ""
        case "transcript": if let t = event.text { lines.append(Line(mine: true, text: t)) }
        case "reply": if let t = event.text { lines.append(Line(mine: false, text: t, route: event.route)) }
        case "error": error = event.message
        default: break
        }
    }

    /// Server closed or something broke: tell CallKit the call is over.
    private func fail(_ message: String?) {
        if let message { error = message }
        if let uuid = callUUID { provider.reportCall(with: uuid, endedAt: nil, reason: message == nil ? .remoteEnded : .failed) }
        teardown()
    }

    private func teardown() {
        if let task = socket {
            task.send(.string(#"{"type":"hangup"}"#)) { _ in task.cancel(with: .normalClosure, reason: nil) }
        }
        socket = nil
        audio.onMic = nil
        audio.stop()
        callUUID = nil
        callID = nil
        sessionPrefix = "iphone"
        muted = false
        inCall = false
        state = ""
    }
}

extension CallManager: CXProviderDelegate {
    func providerDidReset(_ provider: CXProvider) { teardown() }

    func provider(_ provider: CXProvider, perform action: CXStartCallAction) {
        begin(action.callUUID, callID: nil)
        provider.reportOutgoingCall(with: action.callUUID, startedConnectingAt: nil)
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        let info = incoming.removeValue(forKey: action.callUUID)
        begin(action.callUUID, callID: info?.callID)
        if let reason = info?.reason { lines.append(Line(mine: false, text: "용건: \(reason)")) }
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        incoming[action.callUUID] = nil
        if action.callUUID == callUUID { teardown() }
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXSetMutedCallAction) {
        if action.callUUID == callUUID { muted = action.isMuted }
        action.fulfill()
    }

    func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) { connect() }

    func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) { audio.stop() }
}

extension CallManager: PKPushRegistryDelegate {
    func pushRegistry(_ registry: PKPushRegistry, didUpdate credentials: PKPushCredentials, for type: PKPushType) {
        voipToken = credentials.token
        registerPush()
    }

    func pushRegistry(_ registry: PKPushRegistry, didInvalidatePushTokenFor type: PKPushType) { voipToken = nil }

    /// iOS terminates apps that receive a VoIP push without reporting a call, so report first, always.
    func pushRegistry(_ registry: PKPushRegistry, didReceiveIncomingPushWith payload: PKPushPayload,
                      for type: PKPushType, completion: @escaping () -> Void) {
        let p = payload.dictionaryPayload
        let caller = p["caller"] as? String ?? "박비서"
        let uuid = UUID()
        incoming[uuid] = (p["call_id"] as? String, p["reason"] as? String)
        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .generic, value: caller)
        update.localizedCallerName = caller
        update.hasVideo = false
        provider.reportNewIncomingCall(with: uuid, update: update) { [weak self] err in
            if err != nil { DispatchQueue.main.async { self?.incoming[uuid] = nil } }
            completion()
        }
    }
}
