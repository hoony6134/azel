import AppKit
import SwiftUI

@main
struct BiseoMacApp: App {
    @StateObject private var model = AppModel()
    @ObservedObject private var call = MacCall.shared

    init() {
        HotKey.action = { MacCall.shared.toggle() }
        HotKey.register()
    }

    var body: some Scene {
        MenuBarExtra {
            MenuContent().environmentObject(model)
        } label: {
            Image(systemName: call.inCall ? "waveform.circle.fill" : "waveform.circle")
                .accessibilityLabel("박비서")
        }
        Window("박비서", id: "main") {
            MainWindow().environmentObject(model)
        }
        .windowResizability(.contentMinSize)
    }
}

@MainActor
final class AppModel: ObservableObject {
    @Published var configured = Config.token != nil
    @Published var status: ServerStatus?
    @Published var statusError: String?
    @Published var approvals: [[String: Any]] = []
    @Published var wake: WakeAgent.State = .unknown("확인 중")
    @Published var wakeError: String?
    @Published var session = Session.current("mac")
    private var timer: Timer?

    init() {
        Task { await refresh() }
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { _ in Task { @MainActor in await self.refresh() } }
    }

    func refresh() async {
        session = Session.current("mac")
        wake = await Task.detached { WakeAgent.state() }.value
        guard configured else { return }
        do { status = try await Gateway.get("/v1/status"); statusError = nil } catch { status = nil; statusError = error.localizedDescription }
        approvals = (try? await Gateway.approvals()) ?? approvals
    }

    func decide(_ item: [String: Any], _ approve: Bool) {
        let id = describe(item["id"] ?? "")
        Task {
            let path = "/v1/approvals/" + (id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id)
            do { _ = try await Gateway.send("POST", path, json: ["approve": approve]) } catch { statusError = error.localizedDescription }
            await refresh()
        }
    }

    func toggleWake() {
        let current = wake
        Task {
            wakeError = await Task.detached { WakeAgent.toggle(current) }.value
            await refresh()
        }
    }
}

// MARK: - Menu

struct MenuContent: View {
    @EnvironmentObject private var model: AppModel
    @ObservedObject private var call = MacCall.shared
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        if !model.configured {
            Button("연결 설정…") { show() }
        } else {
            statusSection
            Divider()
            Button(call.inCall ? "끊기 (\(HotKey.label))" : "박비서 부르기 (\(HotKey.label))") { call.toggle() }
            Button("새 대화") { model.session = Session.rotate("mac") }
            Text("대화: \(model.session)")
            Divider()
            approvalsSection
        }
        Divider()
        wakeSection
        Divider()
        Button("새로고침") { Task { await model.refresh() } }.keyboardShortcut("r")
        Button("설정…") { show() }.keyboardShortcut(",")
        Button("종료") { NSApp.terminate(nil) }.keyboardShortcut("q")
    }

    @ViewBuilder private var statusSection: some View {
        if let s = model.status {
            Text(line("음성 서버", s.spark.ok, s.spark.ms))
            Text(line("게이트웨이", s.gateway.ok, s.gateway.ms))
            if s.devices.isEmpty { Text("기기 없음") }
            ForEach(s.devices, id: \.name) { Text(line("  \($0.name)", $0.ok, $0.ms)) }
        } else {
            Text("상태: \(model.statusError ?? "불러오는 중")")
        }
    }

    @ViewBuilder private var approvalsSection: some View {
        if model.approvals.isEmpty {
            Text("승인 대기 없음")
        } else {
            Menu("승인 대기 \(model.approvals.count)건") {
                ForEach(model.approvals.indices, id: \.self) { i in
                    let item = model.approvals[i]
                    Menu(summary(item)) {
                        if let cwd = item["cwd"] { Text("위치: \(describe(cwd))") }
                        Button("승인") { model.decide(item, true) }
                        Button("거절") { model.decide(item, false) }
                    }
                }
            }
        }
    }

    @ViewBuilder private var wakeSection: some View {
        switch model.wake {
        case .running: Text("호출어 리스너: 실행 중")
        case .loaded: Text("호출어 리스너: 로드됨 (실행 안 함)")
        case .stopped: Text("호출어 리스너: 꺼짐")
        case .notInstalled: Text("호출어 리스너: 설치 안 됨")
        case .unknown(let why): Text("호출어 리스너: 알 수 없음 (\(why.prefix(60)))")
        }
        switch model.wake {
        case .running, .loaded: Button("호출어 리스너 끄기") { model.toggleWake() }
        case .stopped: Button("호출어 리스너 켜기") { model.toggleWake() }
        case .notInstalled: Button("설치 방법…") { installAlert() }
        case .unknown: EmptyView()
        }
        if let e = model.wakeError { Text(e.prefix(80)) }
    }

    private func line(_ name: String, _ ok: Bool, _ ms: Double?) -> String {
        "\(ok ? "🟢" : "🔴") \(name)" + (ms.map { " · \(Int($0)) ms" } ?? "")
    }

    private func summary(_ item: [String: Any]) -> String {
        let parts = ["device", "tool", "command"].compactMap { item[$0].map(describe) }.filter { !$0.isEmpty }
        return String((parts.isEmpty ? describe(item["id"] ?? "?") : parts.joined(separator: " · ")).prefix(80))
    }

    private func show() {
        openWindow(id: "main")
        NSApp.activate(ignoringOtherApps: true)
    }

    private func installAlert() {
        let alert = NSAlert()
        alert.messageText = "호출어 리스너 설치"
        alert.informativeText = WakeAgent.installHint
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }
}

// MARK: - Window (setup, settings, usage, status, approvals, shortcut)

struct MainWindow: View {
    @EnvironmentObject private var model: AppModel
    @State private var hotkey = HotKey.index

    var body: some View {
        Group {
            if model.configured {
                TabView {
                    SettingsView().tabItem { Label("설정", systemImage: "gearshape") }
                    UsageView().tabItem { Label("사용량", systemImage: "chart.bar") }
                    StatusView().tabItem { Label("상태", systemImage: "server.rack") }
                    ApprovalsView(pending: Binding(get: { model.approvals.count }, set: { _ in Task { await model.refresh() } }))
                        .tabItem { Label("승인", systemImage: "checkmark.shield") }
                    Form {
                        Picker("통화 단축키", selection: $hotkey) {
                            ForEach(HotKey.presets.indices, id: \.self) { Text(HotKey.presets[$0].label).tag($0) }
                        }
                        .onChange(of: hotkey) { HotKey.index = hotkey }
                        Text("다른 앱이 같은 단축키를 쓰고 있으면 등록되지 않습니다.").font(.footnote).foregroundStyle(.secondary)
                    }
                    .formStyle(.grouped)
                    .tabItem { Label("단축키", systemImage: "keyboard") }
                }
            } else {
                NavigationStack {
                    SetupView {
                        model.configured = Config.token != nil
                        Task { await model.refresh() }
                    }
                }
            }
        }
        .frame(minWidth: 520, minHeight: 560)
    }
}
