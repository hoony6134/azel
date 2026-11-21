import AVFoundation
import AppKit
import SwiftUI

/// Push-to-talk call: hotkey → floating panel + `WS /v1/call` via the shared CallClient.
/// AudioIO turns on voice processing on the input node, so barge-in works on speakers.
@MainActor
final class MacCall: ObservableObject {
    static let shared = MacCall()

    @Published private(set) var inCall = false
    @Published private(set) var state = ""          // listening | thinking | speaking
    @Published private(set) var log = CallLog()
    var lines: [CallLine] { log.lines }
    var levels: Levels { client.audio.levels }
    @Published private(set) var muted = false
    @Published private(set) var held = false
    @Published private(set) var recording = false
    @Published private(set) var connectedAt: Date?
    @Published var error: String?
    /// Device UIDs for the call engine; nil = system default. Saved across launches.
    @Published var inputUID = UserDefaults.standard.string(forKey: "inputDevice") { didSet { deviceChanged() } }
    @Published var outputUID = UserDefaults.standard.string(forKey: "outputDevice") { didSet { deviceChanged() } }

    private let client = CallClient()
    private var activeSession = Session.current("mac")
    private var panel: NSPanel?

    private init() {
        client.onEvent = { [weak self] in self?.handle($0) }
        client.onClose = { [weak self] in self?.end(error: nil, keepPanel: true) }
    }

    /// Hotkey: floating panel. The main window's home runs the call in place (DESIGN §8.3).
    func toggle() { inCall ? hangUp() : start(panel: true) }

    func start(panel usePanel: Bool, session: String? = nil) {
        guard !inCall else { return }
        activeSession = session ?? Session.current("mac")
        log = CallLog()
        state = ""
        error = nil
        if usePanel { showPanel() }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: connect()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { ok in
                Task { @MainActor in ok ? self.connect() : (self.error = Self.micDenied) }
            }
        default: error = Self.micDenied
        }
    }

    func hangUp() { end(error: nil, keepPanel: false) }

    func toggleMute() {
        muted.toggle()
        client.muted = muted
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

    private static let micDenied = "마이크 권한이 없습니다. 시스템 설정 → 개인정보 보호 및 보안 → 마이크에서 박비서를 켜 주세요."

    private func connect() {
        inCall = true
        applyDevices()
        do { try client.start(session: activeSession); connectedAt = Date() } catch {
            end(error: error.localizedDescription, keepPanel: true)
        }
    }

    private func handle(_ event: CallEvent) {
        switch event.type {
        case "state": state = event.value ?? ""
        case "error": error = event.message
        default: log.apply(event)
        }
    }

    /// keepPanel: leave the transcript / error on screen after the server hung up or something failed.
    private func end(error message: String?, keepPanel: Bool) {
        if inCall { Recents.add(CallRecord(session: activeSession, start: connectedAt ?? Date(), lines: log.lines)) }
        client.stop()
        if let message { error = message }
        inCall = false
        muted = false
        held = false
        recording = false
        connectedAt = nil
        state = ""
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
            p.setAccessibilityLabel("박비서 말하기 패널")
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
private final class CallPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override func cancelOperation(_ sender: Any?) { MainActor.assumeIsolated { MacCall.shared.hangUp() } }
    override func close() { MainActor.assumeIsolated { MacCall.shared.hangUp() } }
}

/// DESIGN §8.2: jar · captions · route chip and close, on material, radius 30.
struct CallPanelView: View {
    @ObservedObject var call: MacCall
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    private var jarState: JarState { call.inCall ? JarState(callState: call.state, inCall: true, held: call.held) : .offline }

    var body: some View {
        HStack(spacing: Theme.m) {
            MoonJar(state: jarState, levels: call.levels).frame(width: 68, height: 68)
            VStack(alignment: .leading, spacing: 2) {
                let user = call.lines.last { $0.mine }?.text
                let reply = call.lines.last { !$0.mine }
                if let error = call.error {
                    Text(error).font(.footnote).foregroundStyle(Theme.danger).lineLimit(2)
                } else if reply == nil && user == nil {
                    Label(jarState.spokenLabel, systemImage: jarState.symbol).font(.headline).foregroundStyle(jarState.color)
                }
                if let user { Text(user).font(.footnote).foregroundStyle(Theme.text2).lineLimit(2) }
                if let reply {
                    Text(reply.text + (reply.interrupted ? " …" : "")).font(.title3.weight(.medium))
                        .foregroundStyle(reply.interrupted ? Theme.text3 : Theme.text).lineLimit(2)
                }
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
                if let r = call.log.lastRoute { RouteChip(route: r) }
            }
        }
        .padding(.horizontal, Theme.l)
        .frame(width: 460, height: 112)
        .background {
            if reduceTransparency { Theme.bgRaised } else { Rectangle().fill(.ultraThinMaterial) }
        }
        .clipShape(RoundedRectangle(cornerRadius: 30, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 30, style: .continuous)
            .strokeBorder(jarState == .listening ? Theme.celadon : Theme.stroke, lineWidth: 1))
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
