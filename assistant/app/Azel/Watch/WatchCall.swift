import AVFoundation
import CallKit

/// CallKit + `WS /v1/call` on the watch. watchOS allows a WebSocket only while a CallKit call holds
/// the audio session, so the socket opens from `didActivate`. Callbacks run on the main queue.
final class WatchCall: NSObject, ObservableObject {
    static let shared = WatchCall()

    @Published private(set) var inCall = false
    @Published private(set) var state = ""
    @Published private(set) var playing = false
    @Published private(set) var log = CallLog()
    @Published private(set) var muted = false
    @Published var error: String?
    /// Who answers this call (nil = the server's primary).
    @Published private(set) var persona: WatchPersona?
    var displayState: String { CallClient.displayState(server: state, playing: playing) }
    var levels: Levels { client.audio.levels }
    /// Which part of the reply has been heard, for the caption (same as the iPhone call screen).
    func karaoke(stepped: Bool) -> (texts: [String], spoken: Double) {
        let k = client.karaokeState()
        return (k.texts, stepped ? k.stepped : k.spoken)
    }
    /// The step trail, for the working / tick / done / error earcons (the iPhone's flow).
    private var activity = ActivityLog()

    private let provider: CXProvider
    private let controller = CXCallController()
    private let client = CallClient()
    private var callUUID: UUID?

    private override init() {
        let config = CXProviderConfiguration()
        config.supportsVideo = false
        config.maximumCallsPerCallGroup = 1
        config.supportedHandleTypes = [.generic]
        provider = CXProvider(configuration: config)
        super.init()
        provider.setDelegate(self, queue: nil)
        client.onEvent = { [weak self] in self?.handle($0) }
        client.onPlayback = { [weak self] in self?.playing = $0 }
        client.onClose = { [weak self] in self?.fail($0) }
    }

    func start(persona: WatchPersona? = WatchPersona.selected) {
        guard !inCall else { return }
        self.persona = persona
        guard Config.token != nil else { error = "아이폰에서 Azel을 한 번 열어 주세요"; return }
        error = nil
        let action = CXStartCallAction(call: UUID(), handle: CXHandle(type: .generic, value: persona?.id ?? AssistantName.current))
        controller.request(CXTransaction(action: action)) { [weak self] err in
            if let err { DispatchQueue.main.async { self?.error = err.localizedDescription } }
        }
    }

    func hangUp() {
        guard let uuid = callUUID else { return }
        controller.request(CXTransaction(action: CXEndCallAction(call: uuid))) { _ in }
    }

    func toggleMute() {
        guard let uuid = callUUID else { return }
        controller.request(CXTransaction(action: CXSetMutedCallAction(call: uuid, muted: !muted))) { _ in }
    }

    private func handle(_ event: CallEvent) {
        if let cue = activity.apply(event) { client.cue(cue, listening: state == "listening" || state.isEmpty) }
        switch event.type {
        case "state": state = event.value ?? ""
        case "hangup", "end": fail(nil)
        case "error": error = event.message
        default: log.apply(event)
        }
    }

    private func fail(_ message: String?) {
        if let message { error = message }
        if let uuid = callUUID { provider.reportCall(with: uuid, endedAt: nil, reason: message == nil ? .remoteEnded : .failed) }
        teardown()
    }

    private func teardown() {
        client.stop()
        callUUID = nil
        inCall = false
        muted = false
        state = ""
    }
}

extension WatchCall: CXProviderDelegate {
    func providerDidReset(_ provider: CXProvider) { teardown() }

    func provider(_ provider: CXProvider, perform action: CXStartCallAction) {
        callUUID = action.callUUID
        inCall = true
        log = CallLog()
        activity = ActivityLog()
        try? AVAudioSession.sharedInstance().setCategory(.playAndRecord, mode: .voiceChat)
        let named = CXCallUpdate()
        named.localizedCallerName = persona?.name ?? AssistantName.current
        named.remoteHandle = action.handle
        provider.reportCall(with: action.callUUID, updated: named)
        provider.reportOutgoingCall(with: action.callUUID, startedConnectingAt: nil)
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        teardown()
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXSetMutedCallAction) {
        if action.callUUID == callUUID { muted = action.isMuted; client.muted = muted }
        action.fulfill()
    }

    func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        guard let uuid = callUUID else { return }
        do { try client.start(session: Session.current("watch"), persona: persona?.id) } catch { return fail(error.localizedDescription) }
        provider.reportOutgoingCall(with: uuid, connectedAt: nil)
    }

    func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) { client.stopAudio() }
}
