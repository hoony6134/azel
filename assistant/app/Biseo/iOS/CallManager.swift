import AVFoundation
import CallKit
import PushKit

/// CallKit + PushKit + `WS /v1/call`. Provider and push callbacks run on the main queue.
final class CallManager: NSObject, ObservableObject {
    static let shared = CallManager()

    @Published private(set) var inCall = false
    @Published private(set) var state = ""          // listening | thinking | speaking
    @Published private(set) var log = CallLog()
    var lines: [CallLine] { log.lines }
    var levels: Levels { client.audio.levels }
    @Published private(set) var muted = false
    @Published private(set) var held = false
    @Published private(set) var speaker = false
    @Published private(set) var recording = false
    @Published private(set) var connectedAt: Date?
    @Published var error: String?
    /// What the assistant is doing this turn (DESIGN §13) and the jar's surge/pulse moments.
    @Published private(set) var activity = ActivityLog()
    @Published private(set) var surgeAt: Date?
    @Published private(set) var pulseAt: Date?
    /// Last interrupt (server or tap), drives the jar's ripple.
    @Published private(set) var interruptedAt: Date?
    /// Incoming calls show the push's `caller`; outgoing ones the cached assistant name.
    @Published private(set) var callerName: String?

    private let provider: CXProvider
    private let controller = CXCallController()
    private let pushRegistry = PKPushRegistry(queue: .main)
    private let client = CallClient()
    private var voipToken: Data?
    private var callUUID: UUID?
    private var callID: String?                     // set when answering a call the assistant placed
    private var sessionPrefix = "iphone"            // outgoing calls: "iphone" or "briefing"
    private var incoming: [UUID: (callID: String?, reason: String?, caller: String)] = [:]
    private var activeSession: String?

    private override init() {
        let config = CXProviderConfiguration()
        config.supportsVideo = false
        config.maximumCallsPerCallGroup = 1
        config.supportedHandleTypes = [.generic]
        provider = CXProvider(configuration: config)
        super.init()
        provider.setDelegate(self, queue: nil)
        client.onEvent = { [weak self] in self?.handle($0) }
        client.onClose = { [weak self] in self?.fail(nil) }
        pushRegistry.delegate = self
        pushRegistry.desiredPushTypes = [.voIP]
        // The system call UI's speaker button and AirPods coming and going change the route too.
        NotificationCenter.default.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] _ in
            self?.speaker = AVAudioSession.sharedInstance().currentRoute.outputs.contains { $0.portType == .builtInSpeaker }
        }
    }

    // MARK: UI actions

    func startCall(prefix: String = "iphone") {
        guard !inCall else { return }
        sessionPrefix = prefix
        callerName = nil
        let action = CXStartCallAction(call: UUID(), handle: CXHandle(type: .generic, value: AssistantName.current))
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

    /// Goes through CallKit so the system call UI shows the hold state too.
    /// Tap on the jar while it speaks.
    func interrupt() {
        guard inCall, state == "speaking" else { return }
        client.interrupt()
        log.apply(CallEvent(type: "interrupt"))
        state = "listening"
        interruptedAt = Date()
    }

    func toggleHold() {
        guard let uuid = callUUID else { return }
        controller.request(CXTransaction(action: CXSetHeldCallAction(call: uuid, onHold: !held))) { _ in }
    }

    func toggleSpeaker() {
        do { try AVAudioSession.sharedInstance().overrideOutputAudioPort(speaker ? .none : .speaker) } catch {
            self.error = "스피커 전환 실패"
        }
    }

    /// Records both sides to Documents/Recordings; only ever started by the user's tap.
    func toggleRecording() {
        guard inCall else { return }
        if client.isRecording { client.stopRecording() } else {
            do { try client.startRecording(session: callID ?? Session.current(sessionPrefix)) } catch { self.error = "녹음 시작 실패" }
        }
        recording = client.isRecording
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
        log = CallLog()
        activity = ActivityLog()
        muted = false
        held = false
        recording = false
        connectedAt = nil
        error = nil
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playAndRecord, mode: .voiceChat)
    }

    /// Runs from `didActivate`: the audio session is ours now. After a hold the socket is still up,
    /// so only the audio engine comes back.
    private func connect() {
        guard let uuid = callUUID else { return fail("서버 설정이 없습니다") }
        if client.isConnected {
            do { try client.restartAudio() } catch { fail(error.localizedDescription) }
            return
        }
        activeSession = callID ?? Session.current(sessionPrefix)
        do { try client.start(session: activeSession!, callID: callID) } catch {
            return fail(error.localizedDescription)
        }
        if callID == nil { provider.reportOutgoingCall(with: uuid, connectedAt: nil) }
        connectedAt = Date()
        let update = CXCallUpdate()
        update.supportsHolding = true
        update.supportsGrouping = false
        update.supportsUngrouping = false
        update.supportsDTMF = false
        provider.reportCall(with: uuid, updated: update)
    }

    private func handle(_ event: CallEvent) {
        if let cue = activity.apply(event) { client.cue(cue, listening: state == "listening" || state.isEmpty) }
        switch event.type {
        case "state": state = event.value ?? ""
        case "activity" where event.phase == "tool_start":
            surgeAt = Date()
            let label = event.label ?? "", thinking = state == "thinking"
            MainActor.assumeIsolated { ActivityAnnouncer.toolStarted(label, thinking: thinking) } // handle() runs on main
        case "activity" where event.phase == "tool_end":
            pulseAt = Date()
        case "error": error = event.message
        case "interrupt": log.apply(event); interruptedAt = Date()
        default: log.apply(event)
        }
    }

    /// Server closed or something broke: tell CallKit the call is over.
    private func fail(_ message: String?) {
        if let message { error = message }
        if let uuid = callUUID { provider.reportCall(with: uuid, endedAt: nil, reason: message == nil ? .remoteEnded : .failed) }
        teardown()
    }

    private func teardown() {
        client.stop()
        if let activeSession { Recents.add(CallRecord(session: activeSession, start: connectedAt ?? Date(), lines: log.lines)) }
        activeSession = nil
        callUUID = nil
        callID = nil
        sessionPrefix = "iphone"
        muted = false
        held = false
        recording = false
        connectedAt = nil
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
        callerName = info?.caller
        if let reason = info?.reason { log.lines.append(CallLine(mine: false, text: "용건: \(reason)")) }
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        incoming[action.callUUID] = nil
        if action.callUUID == callUUID { teardown() }
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXSetMutedCallAction) {
        if action.callUUID == callUUID { muted = action.isMuted; client.muted = muted }
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXSetHeldCallAction) {
        if action.callUUID == callUUID { held = action.isOnHold; client.held = held }
        action.fulfill()
    }

    func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) { connect() }

    func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) { client.stopAudio() }
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
        let caller = p["caller"] as? String ?? AssistantName.current
        let uuid = UUID()
        incoming[uuid] = (p["call_id"] as? String, p["reason"] as? String, caller)
        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .generic, value: caller)
        update.localizedCallerName = caller
        update.hasVideo = false
        update.supportsHolding = true
        update.supportsGrouping = false
        update.supportsUngrouping = false
        update.supportsDTMF = false
        provider.reportNewIncomingCall(with: uuid, update: update) { [weak self] err in
            if err != nil { DispatchQueue.main.async { self?.incoming[uuid] = nil } }
            completion()
        }
    }
}
