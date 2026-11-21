import AVFoundation
import AppKit
import SwiftUI

/// Push-to-talk call: hotkey → floating panel + `WS /v1/call` via the shared CallClient.
/// AudioIO turns on voice processing on the input node, so barge-in works on speakers.
@MainActor
final class MacCall: ObservableObject {
    static let shared = MacCall()

    @Published private(set) var inCall = false { didSet { if inCall != oldValue { inCall ? AzelFiles.inCall.create() : AzelFiles.inCall.remove() } } }
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
    /// Device UIDs for the call engine; nil = system default. Saved across launches.
    @Published var inputUID = UserDefaults.standard.string(forKey: "inputDevice") { didSet { deviceChanged() } }
    @Published var outputUID = UserDefaults.standard.string(forKey: "outputDevice") { didSet { deviceChanged() } }

    private let client = CallClient()
    private var activeSession = Session.current("mac")
    private var activeIntent: String?
    /// Name and tint of the assistant on this call.
    @Published private(set) var activePersona: Persona?
    private var panel: NSPanel?

    private init() {
        client.onEvent = { [weak self] in self?.handle($0) }
        client.onPlayback = { [weak self] playing in
            guard let self else { return }
            self.playing = playing
            if playing { fadeTask?.cancel(); drainedAt = nil } else { drainedAt = Date(); scheduleFade() }
        }
        client.onCuePlayed = { [weak self] cue in if cue == .pulse { self?.shimmerAt = Date() } }
        client.onClose = { [weak self] in self?.end(error: $0, keepPanel: true) }
        client.onWarning = { [weak self] in self?.error = $0 }
    }

    /// "누르고 말하기" (default) or "눌러서 켜고 끄기".
    enum PTTMode: String, CaseIterable { case hold, toggle }
    static var pttMode: PTTMode {
        get { PTTMode(rawValue: UserDefaults.standard.string(forKey: "pttMode") ?? "") ?? .hold }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: "pttMode") }
    }

    /// True while the hold-to-talk key is down (panel border goes celadon).
    @Published private(set) var talking = false
    /// The brain answering this call (server-reported; the picked one until it says).
    @Published private(set) var brainMode: String?
    private var pttCall = false
    /// Started by the wake word: the mic is live from the start and the call ends after 30 s of quiet.
    private var wakeCall = false
    private var fadeTask: Task<Void, Never>?
    private var idleTask: Task<Void, Never>?

    /// Hotkey: floating panel. The main window's home runs the call in place (DESIGN §8.3).
    func toggle() { inCall ? hangUp() : start(panel: true) }

    func hotkey(down: Bool) {
        switch Self.pttMode {
        case .toggle: if down { toggle() }
        case .hold: down ? pressBegan() : pressEnded()
        }
    }

    /// Key down: open the call if needed and stream the mic while the key is held.
    private func pressBegan() {
        fadeTask?.cancel(); idleTask?.cancel()
        talking = true
        turnDone = false
        drainedAt = nil
        if inCall {
            client.sending = true
            client.cue(.wake, listening: false)
            showPanel()
        } else {
            start(panel: true)
            pttCall = true
        }
    }

    /// Key up: close the turn with silence, stop sending, keep the socket for the reply.
    private func pressEnded() {
        guard talking else { return }
        talking = false
        guard inCall, pttCall else { return }
        client.sending = false
        client.sendSilence()
        scheduleIdle()
    }

    /// Hold-to-talk calls end ~30 s after the last activity.
    private func scheduleIdle() {
        guard pttCall || wakeCall else { return }
        idleTask?.cancel()
        idleTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 30_000_000_000)
            guard let self, !Task.isCancelled, !self.talking else { return }
            self.end(error: nil, keepPanel: false)
        }
    }

    /// The panel fades 4 s after `turn_done` unless the pointer is over it.
    /// The server finished this turn; the panel waits for the audio to finish playing.
    private var turnDone = false
    private var drainedAt: Date?

    /// Hands-free calls (push-to-talk, wake) end on their own: once the last reply has played, 8 s
    /// with no user speech, then the panel lingers 1.5 s, fades and the call ends (the in-call and
    /// mic-live files go with it). A new turn, the key, or the pointer over the panel cancels it.
    private func scheduleFade() {
        guard pttCall || wakeCall else { return }
        fadeTask?.cancel()
        guard turnDone, !playing else { return }
        let since = drainedAt ?? Date()
        drainedAt = since
        let wait = CallOverlay.quiet + CallOverlay.linger
        fadeTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(wait * 1e9))
            guard let self, !Task.isCancelled, !self.talking, let p = self.panel else { return }
            guard CallOverlay.shouldClose(turnDone: turnDone, playing: playing, drainedFor: Date().timeIntervalSince(since), after: wait) else { return }
            if NSPointInRect(NSEvent.mouseLocation, p.frame) { drainedAt = nil; return self.scheduleFade() }
            NSAnimationContext.runAnimationGroup({ $0.duration = 0.3; p.animator().alphaValue = 0 }) { [weak self] in
                MainActor.assumeIsolated { self?.end(error: nil, keepPanel: false) }
            }
        }
    }

    /// `azel://wake?persona=…` from the wake listener: it already said "네?", so no earcon; the panel
    /// shows without taking focus and the mic is live at once for the sentence after the name.
    func wake(persona key: String?, utterance: String? = nil) {
        if inCall { showPanel(); return }
        let p = key.flatMap { k in PersonaModel.shared.personas.first { $0.id == k || $0.name == k } } ?? PersonaModel.shared.primary
        wakeCall = true
        pendingUtterance = utterance
        start(panel: true, persona: p)
        scheduleIdle()
    }

    /// The listener's recording of what followed the name; sent before the live mic.
    private var pendingUtterance: String?

    /// Sends the waiting utterance, then opens the live mic.
    private func sendUtterance(_ id: String) {
        client.sending = false
        Task { [weak self] in
            let data = await AzelFiles.takeUtterance(id)
            guard let self, self.inCall else { return }
            if let data { self.client.sendUserAudio(data) }
            self.client.sending = true
        }
    }

    func start(panel usePanel: Bool, session: String? = nil, intent: String? = nil, persona: Persona? = nil) {
        guard !inCall else { return }
        activeIntent = intent
        activePersona = persona ?? PersonaModel.shared.selected
        activeSession = session ?? Session.current("mac")
        log = CallLog()
        activity = ActivityLog()
        state = ""
        error = nil
        if usePanel { showPanel() }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: connect()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { ok in
                Task { @MainActor in ok ? self.connect() : (self.error = Self.micDenied) }
            }
        default:
            Log.call.error("mic permission denied")
            error = Self.micDenied
        }
    }

    func hangUp() { end(error: nil, keepPanel: false) }

    func chooseBrain(_ m: BrainMode) {
        BrainModeChoice.shared.choose(m, inCall: inCall, client: client, current: { [weak self] in self?.brainMode }) { [weak self] in self?.error = $0 }
    }

    func toggleMute() {
        muted.toggle()
        client.muted = muted
    }

    /// Tap on the jar while it speaks.
    func interrupt() {
        guard inCall, displayState == "speaking" else { return }
        client.interrupt()
        log.apply(CallEvent(type: "interrupt"))
        state = "listening"
        interruptedAt = Date()
    }

    func toggleHold() {
        guard inCall else { return }
        held.toggle()
        client.held = held
    }

    /// Records both sides to Documents/Recordings (inside the app container); only on the user's click.
    func toggleRecording() {
        guard inCall else { return }
        if client.isRecording { client.stopRecording() } else {
            do { try client.startRecording(session: activeSession) } catch { self.error = "녹음 시작 실패" }
        }
        recording = client.isRecording
    }

    private func applyDevices() {
        client.audio.inputDevice = AudioDevices.id(forUID: inputUID, input: true)
        client.audio.outputDevice = AudioDevices.id(forUID: outputUID, input: false)
    }

    private func deviceChanged() {
        UserDefaults.standard.set(inputUID, forKey: "inputDevice")
        UserDefaults.standard.set(outputUID, forKey: "outputDevice")
        applyDevices()
        guard inCall else { return }
        do { try client.restartAudio() } catch { end(error: error.localizedDescription, keepPanel: true) }
    }

    private static var micDenied: String { "\(AssistantName.josa(AssistantName.current, "이", "가")) 말을 들으려면 마이크를 허용해 주세요. 시스템 설정 → 개인정보 보호 및 보안 → 마이크에서 Azel을 켜 주세요." }

    private func connect() {
        inCall = true
        applyDevices()
        do {
            let mode = BrainModeChoice.shared.takeForCall()
            brainMode = mode
            if pendingUtterance != nil { client.sending = false }   // nothing live goes ahead of the utterance
            try client.start(session: activeSession, intent: activeIntent, persona: activePersona?.id, mode: mode)
            connectedAt = Date()
            AzelFiles.micLive.create()
            if let id = pendingUtterance { pendingUtterance = nil; sendUtterance(id) }
            if pttCall || talking { client.cue(.wake, listening: false) }
        } catch {
            end(error: error.localizedDescription, keepPanel: true)
        }
    }

    private func handle(_ event: CallEvent) {
        if let m = CallClient.reportedMode(event) { brainMode = m }
        if let cue = activity.apply(event) { client.cue(cue, listening: state == "listening" || state.isEmpty) }
        scheduleIdle()
        switch event.type {
        case "turn_done": turnDone = true; scheduleFade()
        case "transcript", "state" where event.value == "thinking": turnDone = false; drainedAt = nil; fadeTask?.cancel()
        default: break
        }
        switch event.type {
        case "state": state = event.value ?? ""
        case "activity" where event.phase == "tool_start":
            surgeAt = Date()
            ActivityAnnouncer.toolStarted(event.label ?? "", thinking: state == "thinking")
        case "activity" where event.phase == "tool_end":
            pulseAt = Date()
        case "hangup", "end":
            end(error: nil, keepPanel: false)
            return
        case "error": error = event.message
        case "interrupt": log.apply(event); interruptedAt = Date()
        default: log.apply(event)
        }
    }

    /// keepPanel: leave the transcript / error on screen after the server hung up or something failed.
    private func end(error message: String?, keepPanel: Bool) {
        if inCall { NotificationCenter.default.post(name: callEndedNotification, object: nil) }
        client.stop()
        if let message { error = message }
        inCall = false
        muted = false
        held = false
        recording = false
        connectedAt = nil
        state = ""
        talking = false
        pttCall = false
        wakeCall = false
        pendingUtterance = nil
        AzelFiles.micLive.remove()
        brainMode = nil
        fadeTask?.cancel(); idleTask?.cancel()
        if !keepPanel { panel?.orderOut(nil) }
    }

    // MARK: Panel

    private func showPanel() {
        if panel == nil {
            let p = CallPanel(contentRect: NSRect(x: 0, y: 0, width: 460, height: 112),
                              styleMask: [.borderless, .nonactivatingPanel],
                              backing: .buffered, defer: false)
            p.isFloatingPanel = true
            p.level = .floating
            p.backgroundColor = .clear
            p.isOpaque = false
            p.hasShadow = true
            p.hidesOnDeactivate = false
            p.isReleasedWhenClosed = false
            p.isMovableByWindowBackground = true
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            p.setAccessibilityLabel("\(AssistantName.current) 말하기 패널")
            p.contentView = NSHostingView(rootView: CallPanelView(call: self))
            panel = p
        }
        if let p = panel, let screen = NSScreen.main {
            // Bottom-centre, 80 pt above the Dock (visibleFrame already excludes it).
            let f = screen.visibleFrame
            p.setFrameOrigin(NSPoint(x: f.midX - p.frame.width / 2, y: f.minY + 80))
        }
        panel?.alphaValue = 0
        panel?.makeKeyAndOrderFront(nil) // non-activating: takes Esc without pulling focus from the current app
        NSAnimationContext.runAnimationGroup { $0.duration = 0.25; panel?.animator().alphaValue = 1 }
    }
}

/// Esc (cancelOperation) hangs up; Space toggles mute.
final class CallPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override func cancelOperation(_ sender: Any?) { MainActor.assumeIsolated { MacCall.shared.hangUp() } }
    override func close() { MainActor.assumeIsolated { MacCall.shared.hangUp() } }
}

/// DESIGN §8.2: jar · captions · route chip and close, on material, radius 30.
struct CallPanelView: View {
    @ObservedObject var call: MacCall
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    private var jarState: JarState { call.inCall ? JarState(callState: call.displayState, inCall: true, held: call.held) : .offline }

    var body: some View {
        HStack(spacing: Theme.m) {
            MoonJar(state: jarState, levels: call.levels, ripple: call.interruptedAt, surge: call.surgeAt, pulse: call.pulseAt, shimmerAt: call.shimmerAt,
                    tint: call.activePersona?.glaze ?? Theme.glazeCeladon).frame(width: 68, height: 68)
                .contentShape(Circle())
                .onTapGesture { call.interrupt() }
                .brainModeMenu(current: call.brainMode) { call.chooseBrain($0) }
                .accessibilityAction(named: "말 끊기") { call.interrupt() }
            VStack(alignment: .leading, spacing: 2) {
                let user = call.lines.last { $0.mine }?.text
                let reply = call.lines.last { !$0.mine }
                if let error = call.error {
                    Text(error).font(.footnote).foregroundStyle(Theme.danger).lineLimit(2)
                } else if reply == nil && user == nil {
                    Label(jarState.spokenLabel, systemImage: jarState.symbol).font(.headline).foregroundStyle(jarState.color)
                }
                if let current = call.activity.current {
                    // §13.4: the activity line replaces the transcript while a step runs.
                    HStack(spacing: Theme.xs) {
                        Image(systemName: current.glyph).font(.system(size: 13)).foregroundStyle(Theme.indigo)
                        Text(current.label).font(.footnote.weight(.semibold)).foregroundStyle(Theme.text)
                        if !call.activity.done.isEmpty { Text("· ✓\(call.activity.done.count)").font(.footnote).foregroundStyle(Theme.text3) }
                    }
                    .lineLimit(1)
                    .accessibilityElement(children: .combine)
                } else if let user { Text(user).font(.footnote).foregroundStyle(Theme.text2).lineLimit(2) }
                if let reply { PanelReply(call: call, reply: reply) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .animation(Theme.caption, value: call.lines)
            VStack(alignment: .trailing, spacing: Theme.s) {
                HStack(spacing: Theme.xs) {
                    if call.recording { Circle().fill(Theme.danger).frame(width: 8, height: 8).accessibilityLabel("녹음 중") }
                    Menu {
                        Button(call.muted ? "음소거 해제" : "음소거") { call.toggleMute() }.keyboardShortcut(.space, modifiers: [])
                        Button(call.held ? "대기 해제" : "대기") { call.toggleHold() }
                        Button(call.recording ? "녹음 중지" : "녹음") { call.toggleRecording() }
                    } label: { Image(systemName: "ellipsis.circle") }
                    .menuStyle(.borderlessButton).fixedSize().accessibilityLabel("통화 제어")
                    Button { call.hangUp() } label: {
                        Image(systemName: "xmark").font(.caption.bold()).frame(width: 28, height: 28)
                            .background(Circle().fill(Theme.hangup)).foregroundStyle(.white)
                    }
                    .buttonStyle(.plain).keyboardShortcut(.cancelAction).accessibilityLabel(call.inCall ? "통화 끊기" : "닫기")
                }
                HStack(spacing: Theme.xs) {
                    // §17.8: the persona's name before the route chip.
                    if let p = call.activePersona, PersonaModel.shared.personas.count > 1 { PersonaChip(name: p.name, tint: p.tint, outlined: false) }
                    if let m = BrainMode.chip(call.brainMode) {
                        Text(m).font(.caption.weight(.semibold)).foregroundStyle(Theme.indigo)
                            .onTapGesture { if call.brainMode == BrainMode.local.rawValue { call.chooseBrain(.auto) } }
                            .help(call.brainMode == BrainMode.local.rawValue ? "클릭하면 자동으로 바꿔요" : "")
                    }
                    if let r = call.log.lastRoute { RouteChip(route: r) }
                }
            }
        }
        .padding(.horizontal, Theme.l)
        .frame(width: 460, height: 112)
        .background {
            if reduceTransparency { Theme.bgRaised } else { Rectangle().fill(.ultraThinMaterial) }
        }
        .clipShape(RoundedRectangle(cornerRadius: 30, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 30, style: .continuous)
            .strokeBorder(call.talking ? Theme.celadon : Theme.stroke, lineWidth: call.talking ? 1.5 : 1))
        .environment(\.colorScheme, .dark)
    }
}

/// Input / output device for calls (macOS has no AVRoutePickerView for voice calls).
struct DeviceMenu: View {
    @ObservedObject var call: MacCall

    var body: some View {
        Menu {
            Picker("입력", selection: $call.inputUID) {
                Text("시스템 기본값").tag(String?.none)
                ForEach(AudioDevices.list(input: true), id: \.uid) { Text($0.name).tag(Optional($0.uid)) }
            }
            Picker("출력", selection: $call.outputUID) {
                Text("시스템 기본값").tag(String?.none)
                ForEach(AudioDevices.list(input: false), id: \.uid) { Text($0.name).tag(Optional($0.uid)) }
            }
        } label: { Image(systemName: "hifispeaker.and.homepod") }
        .menuStyle(.borderlessButton).fixedSize()
        .help("오디오 장치").accessibilityLabel("오디오 장치")
    }
}

/// The reply in the push-to-talk panel: scrolls instead of truncating, highlights what has been
/// heard, follows the sentence being spoken, and stops following once the user scrolls (until the
/// next reply).
struct PanelReply: View {
    @ObservedObject var call: MacCall
    let reply: CallLine
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var following = true
    @State private var paragraph = ""
    @State private var monitor: Any?

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView(.vertical, showsIndicators: true) {
                TimelineView(.animation(minimumInterval: 1 / 20, paused: !call.playing)) { _ in
                    let k = call.karaoke(stepped: reduceMotion)
                    VStack(alignment: .leading, spacing: 2) {
                        if k.texts.joined(separator: " ") == reply.text {
                            ForEach(Array(k.texts.enumerated()), id: \.offset) { i, t in
                                KaraokeText(text: t, heard: KaraokeText.heard(in: k.texts, index: i, spoken: k.spoken),
                                            color: reply.interrupted ? Theme.text3 : Theme.text)
                                    .id("seg-\(i)")
                            }
                        } else {
                            Text(reply.text + (reply.interrupted ? " …" : "")).foregroundStyle(reply.interrupted ? Theme.text3 : Theme.text)
                        }
                    }
                    .font(.title3.weight(.medium))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .onChange(of: k.current) { _, current in
                        guard following, let current else { return }
                        if reduceMotion { proxy.scrollTo("seg-\(current)", anchor: .center) }
                        else { withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo("seg-\(current)", anchor: .center) } }
                    }
                }
            }
            .frame(maxHeight: 56)
        }
        .onChange(of: reply.text.prefix(24)) { _, start in
            // A new reply paragraph: follow again.
            if String(start) != paragraph { paragraph = String(start); following = true }
        }
        .onAppear {
            // Trackpad or wheel scrolling over the panel pauses auto-follow until the next reply.
            monitor = NSEvent.addLocalMonitorForEvents(matching: .scrollWheel) { event in
                if event.window is CallPanel { following = false }
                return event
            }
        }
        .onDisappear { if let monitor { NSEvent.removeMonitor(monitor) } }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(reply.text)
    }
}

/// `azel://wake?persona=<id or name>&utterance=<id>` from the wake listener.
enum WakeURL {
    struct Request: Equatable { var persona: String?; var utterance: String? }

    /// nil for any URL that isn't a wake.
    static func parse(_ url: URL) -> Request? {
        guard url.scheme == "azel", url.host == "wake" else { return nil }
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ k: String) -> String? { items.first { $0.name == k }?.value.flatMap { $0.isEmpty ? nil : $0 } }
        // The id becomes part of a file name: only [A-Za-z0-9_-], at most 64.
        let u = value("utterance").flatMap { $0.range(of: #"^[A-Za-z0-9_-]{1,64}$"#, options: .regularExpression) != nil ? $0 : nil }
        return Request(persona: value("persona"), utterance: u)
    }
}
