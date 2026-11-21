import AVFoundation
import AppKit
import SwiftUI

/// Push-to-talk call: hotkey → floating panel + `WS /v1/call` via the shared CallClient.
/// AudioIO turns on voice processing on the input node, so barge-in works on speakers.
@MainActor
final class MacCall: ObservableObject {
    static let shared = MacCall()

    struct Line: Identifiable { let id = UUID(); let mine: Bool; let text: String; var route: String? = nil }

    @Published private(set) var inCall = false
    @Published private(set) var state = ""          // listening | thinking | speaking
    @Published private(set) var lines: [Line] = []
    @Published private(set) var muted = false
    @Published private(set) var held = false
    @Published private(set) var recording = false
    @Published private(set) var connectedAt: Date?
    @Published var error: String?
    /// Device UIDs for the call engine; nil = system default. Saved across launches.
    @Published var inputUID = UserDefaults.standard.string(forKey: "inputDevice") { didSet { deviceChanged() } }
    @Published var outputUID = UserDefaults.standard.string(forKey: "outputDevice") { didSet { deviceChanged() } }

    private let client = CallClient()
    private var panel: NSPanel?

    private init() {
        client.onEvent = { [weak self] in self?.handle($0) }
        client.onClose = { [weak self] in self?.end(error: nil, keepPanel: true) }
    }

    func toggle() { inCall ? hangUp() : start() }

    func start() {
        guard !inCall else { return }
        lines = []
        state = ""
        error = nil
        showPanel()
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
            do { try client.startRecording(session: Session.current("mac")) } catch { self.error = "녹음 시작 실패" }
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
        do { try client.start(session: Session.current("mac")); connectedAt = Date() } catch {
            end(error: error.localizedDescription, keepPanel: true)
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

    /// keepPanel: leave the transcript / error on screen after the server hung up or something failed.
    private func end(error message: String?, keepPanel: Bool) {
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
            let p = CallPanel(contentRect: NSRect(x: 0, y: 0, width: 380, height: 300),
                              styleMask: [.titled, .closable, .nonactivatingPanel, .fullSizeContentView, .utilityWindow],
                              backing: .buffered, defer: false)
            p.title = "박비서"
            p.titlebarAppearsTransparent = true
            p.isFloatingPanel = true
            p.level = .floating
            p.hidesOnDeactivate = false
            p.isReleasedWhenClosed = false
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            p.contentView = NSHostingView(rootView: CallPanelView(call: self))
            panel = p
        }
        if let p = panel, let screen = NSScreen.main {
            let f = screen.visibleFrame
            p.setFrameTopLeftPoint(NSPoint(x: f.midX - p.frame.width / 2, y: f.maxY - 40))
        }
        panel?.makeKeyAndOrderFront(nil) // non-activating: takes Esc without pulling focus from the current app
    }
}

/// Esc (cancelOperation) and the close button hang up.
private final class CallPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override func cancelOperation(_ sender: Any?) { MainActor.assumeIsolated { MacCall.shared.hangUp() } }
    override func performClose(_ sender: Any?) { MainActor.assumeIsolated { MacCall.shared.hangUp() } }
    override func close() { MainActor.assumeIsolated { MacCall.shared.hangUp() } }
}

struct CallPanelView: View {
    @ObservedObject var call: MacCall

    private var stateText: String {
        if !call.inCall { return "통화 종료" }
        return ["listening": "듣는 중", "thinking": "생각 중", "speaking": "말하는 중"][call.state] ?? "연결 중"
    }

    private func icon(_ title: String, _ name: String, on: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) { Image(systemName: name).foregroundStyle(on ? Color.red : Color.primary) }
            .buttonStyle(.borderless).help(title).accessibilityLabel(title)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Image(systemName: call.inCall ? "waveform" : "phone.down").symbolEffect(.variableColor, isActive: call.state == "speaking")
                Text(call.held ? "대기 중" : stateText).font(.headline)
                if call.recording { RecordingBadge(since: call.connectedAt) }
                Spacer()
                if call.inCall {
                    icon(call.muted ? "음소거 해제" : "음소거", call.muted ? "mic.slash.fill" : "mic.fill", on: call.muted) { call.toggleMute() }
                    icon(call.held ? "대기 해제" : "대기", "pause.fill", on: call.held) { call.toggleHold() }
                    icon(call.recording ? "녹음 중지" : "녹음", "record.circle", on: call.recording) { call.toggleRecording() }
                    DeviceMenu(call: call)
                }
                Button(call.inCall ? "끊기" : "닫기") { call.hangUp() }.keyboardShortcut(.cancelAction)
            }
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(call.lines) { line in
                            HStack(alignment: .firstTextBaseline) {
                                if line.mine { Spacer(minLength: 24) }
                                Text(line.text).textSelection(.enabled)
                                    .foregroundStyle(line.mine ? Color.primary : Color.accentColor)
                                if let route = line.route {
                                    Text(route).font(.caption2.bold()).padding(.horizontal, 6).padding(.vertical, 2)
                                        .background(route == "claude" ? Color.orange.opacity(0.2) : Color.blue.opacity(0.2), in: Capsule())
                                        .accessibilityLabel("\(route) 응답")
                                }
                                if !line.mine { Spacer(minLength: 24) }
                            }
                            .id(line.id)
                        }
                    }
                }
                .onChange(of: call.lines.count) { if let last = call.lines.last { proxy.scrollTo(last.id) } }
            }
            if let error = call.error { Text(error).font(.footnote).foregroundStyle(.red) }
            Text("Esc 또는 \(HotKey.label)로 끊기").font(.caption).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 14).padding(.bottom, 12).padding(.top, 28)
        .frame(width: 380, height: 300)
    }
}

/// Input / output device for this call (macOS has no AVRoutePickerView for voice calls).
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
