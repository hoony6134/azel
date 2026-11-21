import AVFoundation
import CallKit
import UIKit
import PushKit

/// CallKit + PushKit + `WS /v1/call`. Provider and push callbacks run on the main queue.
final class CallManager: NSObject, ObservableObject {
    static let shared = CallManager()

    @Published private(set) var inCall = false
    @Published private(set) var state = ""          // listening | thinking | speaking
    /// Reply audio still playing (the server can move on before the last of it is heard).
    @Published private(set) var playing = false
    /// The state the UI shows: 말하는 중 while reply audio is still playing.
    var displayState: String { CallClient.displayState(server: state, playing: playing) }
    /// The reply paragraph being heard (sentences, characters heard, sentence being spoken).
    func karaoke(stepped: Bool) -> (texts: [String], spoken: Double, current: Int?) {
        let k = client.karaokeState()
        return (k.texts, stepped ? k.stepped : k.spoken, k.current)
    }
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
    /// The thinking pulse's visual twin: a shimmer across the orb.
    @Published private(set) var shimmerAt: Date?
    /// Last interrupt (server or tap), drives the jar's ripple.
    @Published private(set) var interruptedAt: Date?
    /// Incoming calls show the push's `caller`; outgoing ones the cached assistant name.
    @Published private(set) var callerName: String?

    private let provider: CXProvider
    private let controller = CXCallController()
    private let pushRegistry = PKPushRegistry(queue: .main)
    private let client = CallClient()
    private var voipToken: Data?
    private var alertToken: Data?
    private var callUUID: UUID?
    private var callID: String?                     // set when answering a call the assistant placed
    private var sessionPrefix = "iphone"            // outgoing calls: "iphone" or "briefing"
    private var incoming: [UUID: (callID: String?, reason: String?, caller: String, persona: String?)] = [:]
    private var activeSession: String?
    /// The persona of the outgoing call (name and tint on screen, `persona=` on the socket).
    @Published private(set) var activePersona: Persona?

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
        client.onCuePlayed = { [weak self] cue in if cue == .pulse { self?.shimmerAt = Date() } }
        client.onClose = { [weak self] in self?.fail($0) }
        client.onWarning = { [weak self] in self?.error = $0 }
        pushRegistry.delegate = self
        pushRegistry.desiredPushTypes = [.voIP]
        // The system call UI's speaker button and AirPods coming and going change the route too.
        NotificationCenter.default.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] note in
            guard let self else { return }
            let session = AVAudioSession.sharedInstance()
            self.speaker = AudioRoute.speakerOn(session.currentRoute.outputs.map(\.portType))
            // A headset connected mid-call takes the call, mic included.
            let reason = (note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt).flatMap(AVAudioSession.RouteChangeReason.init)
            if self.inCall, reason == .newDeviceAvailable, let inputs = session.availableInputs,
               let i = AudioRoute.headsetInput(inputs.map(\.portType)) {
                try? session.setPreferredInput(inputs[i])
            }
        }
    }

    // MARK: UI actions

    /// `persona`: nil = the one chosen in the app (or the primary).
    @MainActor func startCall(prefix: String = "iphone", persona: Persona? = nil) {
        guard !inCall else { return }
        sessionPrefix = prefix
        let who = persona ?? PersonaModel.shared.selected
        activePersona = who
        callerName = who?.name
        // §17.6: the handle is the persona id; the system UI and Recents show localizedCallerName.
        let action = CXStartCallAction(call: UUID(), handle: CXHandle(type: .generic, value: who?.id ?? AssistantName.current))
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
    #if DEBUG
    /// `-callPreview <state>`: shows the call screen without a call, for screenshots.
    func debugPreview(state: String) {
        inCall = true
        self.state = state
        connectedAt = Date().addingTimeInterval(-72)
        log.lines = [CallLine(mine: true, text: "내일 일정 알려줘"), CallLine(mine: false, text: "내일은 오전 10시에 팀 회의가 있어요.", route: "claude")]
    }
    #endif

    /// Tap on the jar while it speaks.
    func interrupt() {
        guard inCall, displayState == "speaking" else { return }
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
    /// Sends both tokens in one register call; runs again whenever either changes (the gateway
    /// requires the VoIP token, so an alert token alone waits for it).
    func registerPush() {
        guard let token = voipToken, Config.token != nil else { return }
        let alert = alertToken
        Task { @MainActor in
            do { try await Gateway.registerPush(token: token, alertToken: alert) } catch { self.error = "푸시 등록 실패: \(error.localizedDescription)" }
        }
    }

    /// APNs alert token from `didRegisterForRemoteNotificationsWithDeviceToken`.
    func setAlertToken(_ token: Data) {
        guard token != alertToken else { return }
        alertToken = token
        registerPush()
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
        // .allowBluetooth: AirPods and other headsets (HFP, mic and speaker) can carry the call;
        // without it the route picker lists them but the route never moves (user bug, 2026-11-22).
        try? session.setCategory(.playAndRecord, mode: .voiceChat, options: [.allowBluetooth])
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
        // Outgoing briefing calls (오늘 브리핑, Siri) tell the server what to open with.
        let intent = callID == nil && sessionPrefix == "briefing" ? "briefing" : nil
        do { try client.start(session: activeSession!, callID: callID, intent: intent, persona: callID == nil ? activePersona?.id : nil) } catch {
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
        case "hangup", "end":
            // The server ended the call: tell CallKit so the system UI goes away.
            fail(nil)
            return
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
        if activeSession != nil { NotificationCenter.default.post(name: callEndedNotification, object: nil) }
        activeSession = nil
        activePersona = nil
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
        let persona = activePersona, caller = callerName
        begin(action.callUUID, callID: nil)
        activePersona = persona
        callerName = caller
        let named = CXCallUpdate()
        named.localizedCallerName = caller ?? AssistantName.current
        named.remoteHandle = action.handle
        provider.reportCall(with: action.callUUID, updated: named)
        provider.reportOutgoingCall(with: action.callUUID, startedConnectingAt: nil)
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        let info = incoming.removeValue(forKey: action.callUUID)
        begin(action.callUUID, callID: info?.callID)
        callerName = info?.caller
        // The push names the persona that is calling; its glaze tints the call screen.
        let personaID = info?.persona
        activePersona = MainActor.assumeIsolated { PersonaModel.shared.persona(id: personaID) }
        if let reason = info?.reason { log.lines.append(CallLine(mine: false, text: "용건: \(reason)")) }
        action.fulfill()
    }

    /// X on the Dynamic Island, the Lock Screen or the app (user bug, 2026-11-21). There is only
    /// ever one call, so end whatever is up even if the UUID doesn't match, and finish the hangup
    /// inside a background task so it reaches the server while the app is backgrounded.
    func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        incoming[action.callUUID] = nil
        Log.call.info("callkit end (matches current: \(action.callUUID == self.callUUID, privacy: .public))")
        if inCall || callUUID != nil { Self.inBackgroundTask { self.teardown() } }
        action.fulfill()
    }

    /// Keeps the app alive for the few hundred ms the hangup frame and socket close need.
    static func inBackgroundTask(_ work: () -> Void) {
        var task = UIBackgroundTaskIdentifier.invalid
        task = UIApplication.shared.beginBackgroundTask(withName: "hangup") { UIApplication.shared.endBackgroundTask(task) }
        work()
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { if task != .invalid { UIApplication.shared.endBackgroundTask(task) } }
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
        incoming[uuid] = (p["call_id"] as? String, p["reason"] as? String, caller,
                          (p["persona"] as? String) ?? (p["persona"] as? NSNumber).map(\.stringValue))
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

/// Which route the call is on, from the session's port types (testable without a session).
enum AudioRoute {
    static func speakerOn(_ outputs: [AVAudioSession.Port]) -> Bool { outputs.contains(.builtInSpeaker) }

    /// The headset input to prefer when one appears: Bluetooth HFP first, then a wired headset.
    static func headsetInput(_ inputs: [AVAudioSession.Port]) -> Int? {
        inputs.firstIndex(of: .bluetoothHFP) ?? inputs.firstIndex(of: .headsetMic)
    }
}
