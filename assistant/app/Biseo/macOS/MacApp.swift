import AppKit
import SwiftUI

@main
struct BiseoMacApp: App {
    @StateObject private var model = AppModel(sessionPrefix: "mac")
    @StateObject private var wake = WakeModel()
    @StateObject private var history = HistoryModel()
    @StateObject private var schedule = ScheduleModel()
    @ObservedObject private var call = MacCall.shared

    init() {
        Recordings.cleanup()
        Config.migrateAddresses()
        HotKey.action = { MacCall.shared.hotkey(down: $0) }
        HotKey.register()
    }

    var body: some Scene {
        MenuBarExtra {
            Popover(model: model, wake: wake, history: history)
        } label: {
            Image(nsImage: JarIcon.make(filled: call.inCall, dot: !model.approvals.isEmpty))
                .accessibilityLabel("Azel")
        }
        .menuBarExtraStyle(.window)

        Window("Azel", id: "main") {
            MainWindow(model: model, wake: wake, history: history, schedule: schedule)
        }
        .defaultSize(width: 760, height: 720)

        Window("기록", id: "history") {
            HistorySplit(history: history).frame(minWidth: 640, minHeight: 420)
        }
        .defaultSize(width: 820, height: 560)
        .keyboardShortcut("y")
        .commands {
            CommandGroup(after: .newItem) {
                Button("새 대화") { model.newConversation() }.keyboardShortcut("n")
            }
        }
    }
}

/// The menu-bar orb (assistant/design/icon templates): outline when idle, filled during a call,
/// plus a dot when approvals are pending. Always a template image so it follows the menu bar.
enum JarIcon {
    static func make(filled: Bool, dot: Bool) -> NSImage {
        let base = NSImage(named: filled ? "MenuBarTemplateFilled" : "MenuBarTemplate") ?? NSImage()
        guard dot else { base.isTemplate = true; return base }
        let size = NSSize(width: base.size.width + 4, height: base.size.height)
        let image = NSImage(size: size, flipped: false) { _ in
            base.draw(in: NSRect(origin: .zero, size: base.size))
            NSColor.black.set()
            NSBezierPath(ovalIn: NSRect(x: size.width - 6, y: size.height - 6, width: 6, height: 6)).fill()
            return true
        }
        image.isTemplate = true
        return image
    }
}

/// Wake-word launchd agent status (the listener itself is `assistant/wake/`).
@MainActor
final class WakeModel: ObservableObject {
    @Published var state: WakeAgent.State = .unknown("확인 중")
    @Published var error: String?

    init() { Task { await refresh() } }

    func refresh() async { state = await Task.detached { WakeAgent.state() }.value }

    func toggle() {
        let current = state
        Task {
            error = await Task.detached { WakeAgent.toggle(current) }.value
            await refresh()
        }
    }

    var label: String {
        switch state {
        case .running: return "실행 중"
        case .loaded: return "로드됨 (실행 안 함)"
        case .stopped: return "꺼짐"
        case .notInstalled: return "설치 안 됨"
        case .unknown(let why): return "알 수 없음 (\(why.prefix(60)))"
        }
    }
}

// MARK: - Popover (DESIGN §8.1)

struct Popover: View {
    @ObservedObject var model: AppModel
    @ObservedObject var wake: WakeModel
    @ObservedObject var history: HistoryModel
    @ObservedObject private var call = MacCall.shared
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.l) {
            HStack {
                Text("Azel").font(.headline).foregroundStyle(Theme.text)
                Spacer()
                StatusDot(ok: model.online)
                Text(model.configured ? (model.online ? "연결됨" : model.statusError.map { FriendlyError.describe($0).title } ?? "확인 중") : "설정 필요")
                    .font(.footnote).foregroundStyle(Theme.text2)
            }
            .accessibilityElement(children: .combine)
            if !model.configured {
                Button("연결 설정…") { show() }.buttonStyle(.borderedProminent).tint(Theme.celadon)
            } else {
                HStack(spacing: Theme.m) {
                    MoonJar(state: call.inCall ? JarState(callState: call.state, inCall: true, held: call.held) : (model.statusError == nil ? .idle : .offline),
                            levels: call.levels, sway: true).frame(width: 64, height: 64)
                    VStack(alignment: .leading, spacing: Theme.s) {
                        Text(MacCall.pttMode == .hold ? "\(HotKey.label)를 누른 채 말하세요" : "\(HotKey.label)를 눌러 말하세요").font(.subheadline).foregroundStyle(Theme.text2)
                        Button { call.inCall ? call.hangUp() : call.start(panel: true) } label: {
                            Label(call.inCall ? "끊기" : "전화", systemImage: call.inCall ? "phone.down.fill" : "phone.fill")
                        }
                        .buttonStyle(.borderedProminent).tint(call.inCall ? Theme.hangup : Theme.celadon)
                        .foregroundStyle(call.inCall ? Color.white : Theme.onAccent)
                    }
                }
                PersonaSwitcher(model: PersonaModel.shared)
                Grid(horizontalSpacing: Theme.s) {
                    GridRow {
                        small("오늘 브리핑", "sun.max") { startBriefing() }
                        small("새 대화", "square.and.pencil") { model.newConversation() }
                    }
                }
                if !model.approvals.isEmpty {
                    Button { show() } label: {
                        Label("승인 \(model.approvals.count)건", systemImage: "checkmark.shield").frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .buttonStyle(.bordered).tint(Theme.amber)
                }
                VStack(alignment: .leading, spacing: Theme.xs) {
                    if !history.recent.isEmpty { Text("최근 대화").font(.footnote.weight(.semibold)).foregroundStyle(Theme.text3) }
                    ForEach(history.recent) { item in
                        HStack(alignment: .firstTextBaseline) {
                            Text(item.start, format: .dateTime.hour().minute()).font(.footnote.monospacedDigit()).foregroundStyle(Theme.text3)
                            Text(item.title ?? "제목 없음").font(.subheadline).foregroundStyle(Theme.text).lineLimit(1)
                        }
                        .accessibilityElement(children: .combine)
                    }
                    Button("기록 모두 보기…") { showHistory() }.buttonStyle(.borderless).font(.footnote).tint(Theme.celadon)
                        .keyboardShortcut("y")
                }
            }
            Divider()
            HStack {
                Text("호출어: \(wake.label)").font(.footnote).foregroundStyle(Theme.text2).lineLimit(1)
                Spacer()
                wakeButton(wake)
            }
            HStack {
                Button("설정…") { show() }.keyboardShortcut(",")
                Spacer()
                Button("종료") { NSApp.terminate(nil) }.keyboardShortcut("q")
            }
            .buttonStyle(.borderless).font(.footnote).foregroundStyle(Theme.text2)
        }
        .padding(Theme.l)
        .frame(width: 340)
        .background(Theme.bgRaised)
        .task { await model.refresh(); await wake.refresh(); await history.refresh(); await PersonaModel.shared.refresh() }
    }

    private func small(_ title: String, _ icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) { Label(title, systemImage: icon).frame(maxWidth: .infinity) }.buttonStyle(.bordered)
    }

    private func startBriefing() {
        // Briefing uses its own session prefix, like the iPhone's 오늘 브리핑.
        call.start(panel: true, session: Session.current("briefing"), intent: "briefing")
    }

    private func show() {
        openWindow(id: "main")
        NSApp.activate(ignoringOtherApps: true)
    }

    private func showHistory() {
        openWindow(id: "history")
        NSApp.activate(ignoringOtherApps: true)
    }
}

/// §15.5: history list as a sidebar, detail on the right.
struct HistorySplit: View {
    @ObservedObject var history: HistoryModel
    @State private var selection: HistoryItem?

    var body: some View {
        NavigationSplitView {
            HistoryList(model: history, selection: $selection, onCall: { MacCall.shared.start(panel: true) })
                .navigationSplitViewColumnWidth(min: 260, ideal: 300)
                .onDeleteCommand { if let s = selection { history.delete(s); selection = nil } }
        } detail: {
            if let selection {
                HistoryDetailView(item: selection, history: history).id(selection.id)
            } else {
                Text("기록을 고르세요").foregroundStyle(Theme.text3).frame(maxWidth: .infinity, maxHeight: .infinity).background(Theme.bg)
            }
        }
        .task { await history.refresh() }
        .tint(Theme.celadon)
    }
}

@MainActor @ViewBuilder func wakeButton(_ wake: WakeModel) -> some View {
    switch wake.state {
    case .running, .loaded: Button("끄기") { wake.toggle() }.font(.footnote)
    case .stopped: Button("켜기") { wake.toggle() }.font(.footnote)
    case .notInstalled: Button("설치 방법…") { installAlert() }.font(.footnote)
    case .unknown: EmptyView()
    }
}

@MainActor func installAlert() {
    let alert = NSAlert()
    alert.messageText = "호출어 리스너 설치"
    alert.informativeText = WakeAgent.installHint
    NSApp.activate(ignoringOtherApps: true)
    alert.runModal()
}

// MARK: - Main window (DESIGN §8.3)

struct MainWindow: View {
    @ObservedObject var model: AppModel
    @ObservedObject var wake: WakeModel
    @ObservedObject var history: HistoryModel
    @ObservedObject var schedule: ScheduleModel
    @State private var section: String? = "home"

    var body: some View {
        Group {
            if model.configured {
                NavigationSplitView {
                    List(selection: $section) {
                        Label("홈", systemImage: "circle.circle").tag("home")
                        Label("기록", systemImage: "clock").tag("history")
                        Label("승인", systemImage: "checkmark.shield").tag("approvals").badge(model.approvals.count)
                        Label("설정", systemImage: "gearshape").tag("settings")
                    }
                    .navigationSplitViewColumnWidth(min: 160, ideal: 180)
                } detail: {
                    switch section {
                    case "approvals": ApprovalsScreen(model: model)
                    case "history": HistorySplit(history: history)
                    case "settings": SettingsScreen(model: model, history: history) { MacSettingsCard(wake: wake) }
                    default: MacHome(model: model, history: history, schedule: schedule) { section = "history" }
                    }
                }
            } else {
                SetupFlow { model.configured = Config.token != nil; Task { await model.refresh() } }
            }
        }
        .frame(minWidth: 640, minHeight: 600)
        .tint(Theme.celadon)
    }
}

/// Home with the call in place: the jar grows and the dock appears when a call is up.
struct MacHome: View {
    @ObservedObject var model: AppModel
    @ObservedObject var history: HistoryModel
    @ObservedObject var schedule: ScheduleModel
    var showAll: () -> Void
    @ObservedObject private var call = MacCall.shared
    @State private var openItem: HistoryItem?

    private var jarState: JarState {
        call.inCall ? JarState(callState: call.state, inCall: true, held: call.held) : (model.statusError == nil ? .idle : .offline)
    }

    var body: some View {
        ZStack {
            if call.inCall { GlazeBackground(state: jarState) } else { Theme.bg.ignoresSafeArea() }
            ScrollView {
                VStack(spacing: Theme.xl) {
                    if !call.inCall {
                        VStack(spacing: Theme.xs) {
                            Text(greeting()).font(.title.weight(.semibold)).foregroundStyle(Theme.text)
                            Text(Date(), format: .dateTime.month().day().weekday(.wide)).font(.subheadline).foregroundStyle(Theme.text2)
                        }
                    } else {
                        if let p = call.activePersona {
                            HStack(spacing: 6) {
                                Circle().fill(p.tint).frame(width: 8, height: 8)
                                Text(p.name).font(.headline).foregroundStyle(Theme.text)
                            }
                        }
                        Label(jarState.spokenLabel, systemImage: jarState.symbol).font(.headline).foregroundStyle(jarState.color)
                    }
                    MoonJar(state: jarState, levels: call.levels, ripple: call.interruptedAt, surge: call.surgeAt, pulse: call.pulseAt)
                        .contentShape(Rectangle())
                        .onTapGesture { call.interrupt() }
                        .accessibilityHint(jarState == .speaking ? "두 번 탭하면 말을 끊습니다" : "")
                        .accessibilityAction(named: "말 끊기") { call.interrupt() }
                        .frame(width: call.inCall ? 320 : 200, height: call.inCall ? 320 : 200)
                        .animation(Theme.enter, value: call.inCall)
                    if call.inCall {
                        if !call.activity.isEmpty { ActivityRow(activity: call.activity) }
                        captions; dock
                    } else { idle }
                    if let e = call.error { Text(e).font(.footnote).foregroundStyle(Theme.danger) }
                }
                .padding(Theme.xl)
                .frame(maxWidth: 640)
                .frame(maxWidth: .infinity)
            }
        }
        .sheet(item: $openItem) { item in
            NavigationStack { HistoryDetailView(item: item, history: history) }.frame(width: 600, height: 600)
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("닫기") { openItem = nil } } }
        }
        .task { await history.refresh() }
    }

    private var idle: some View {
        VStack(spacing: Theme.l) {
            Button { call.start(panel: false) } label: {
                Label("\(PersonaModel.shared.callName)에게 전화", systemImage: "phone.fill").font(.headline).foregroundStyle(Theme.onAccent)
                    .frame(maxWidth: .infinity).frame(height: 52)
                    .background(Theme.celadon, in: RoundedRectangle(cornerRadius: Theme.callButtonRadius, style: .continuous))
            }
            .buttonStyle(.plain).disabled(model.statusError != nil)
            Text("어디서든 \(HotKey.label)로 부를 수 있어요").font(.footnote).foregroundStyle(Theme.text3)
            ScheduleCard(schedule: schedule)
            VStack(alignment: .leading, spacing: Theme.m) {
                HStack {
                    Text("최근 대화").font(.headline).foregroundStyle(Theme.text)
                    Spacer()
                    if !history.recent.isEmpty { Button("모두 보기", action: showAll).buttonStyle(.borderless).tint(Theme.celadon) }
                }
                if history.recent.isEmpty {
                    Text("아직 대화가 없어요 · 첫 전화를 걸어 보세요").foregroundStyle(Theme.text3)
                }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 280), spacing: Theme.l)], spacing: Theme.l) {
                    ForEach(history.recent) { item in
                        Button { openItem = item } label: { HistoryCard(item: item) }.buttonStyle(.plain)
                    }
                }
            }
        }
    }

    private var captions: some View {
        VStack(alignment: .leading, spacing: Theme.m) {
            ForEach(call.lines.suffix(4)) { line in
                Text(line.text + (line.interrupted ? " …" : ""))
                    .font(line.mine ? .body : .title2.weight(.medium))
                    .foregroundStyle(line.mine ? Theme.text2 : line.interrupted ? Theme.text3 : Theme.text)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }
        }
        .animation(Theme.caption, value: call.lines)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("대화 내용")
    }

    /// Same dock as iPhone minus speaker and route picker (DESIGN §8.3); device menu instead.
    private var dock: some View {
        HStack(spacing: Theme.l) {
            dockButton("음소거", call.muted ? "mic.slash.fill" : "mic.fill", on: call.muted, color: Theme.amber) { call.toggleMute() }
                .keyboardShortcut(.space, modifiers: [])
            dockButton("녹음", call.recording ? "stop.circle.fill" : "record.circle", on: call.recording, color: Theme.danger) { call.toggleRecording() }
            dockButton("대기", call.held ? "play.fill" : "pause", on: call.held) { call.toggleHold() }
            VStack(spacing: Theme.xs) { DeviceMenu(call: call).frame(width: 44, height: 44); Text("장치").font(.footnote).foregroundStyle(Theme.text2) }
            Button { call.hangUp() } label: {
                Image(systemName: "phone.down.fill").font(.title2).foregroundStyle(.white).frame(width: 56, height: 56)
                    .background(Circle().fill(Theme.hangup))
            }
            .buttonStyle(.plain).keyboardShortcut(.cancelAction).accessibilityLabel("통화 끊기")
        }
        .padding(.horizontal, Theme.xl).padding(.vertical, Theme.m)
        .background(.ultraThinMaterial, in: Capsule())
    }

    private func dockButton(_ title: String, _ icon: String, on: Bool, color: Color = Theme.celadon, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(spacing: Theme.xs) {
                Image(systemName: icon).font(.title3).foregroundStyle(on ? Theme.onAccent : Theme.text)
                    .frame(width: 44, height: 44).background(Circle().fill(on ? color : Theme.card))
                Text(title).font(.footnote).foregroundStyle(Theme.text2)
            }
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
        .accessibilityValue(on ? "켜짐" : "꺼짐")
    }
}

/// Mac-only settings: hotkey and the wake-word listener.
struct MacSettingsCard: View {
    @ObservedObject var wake: WakeModel
    @State private var hotkey = HotKey.index
    @State private var mode = MacCall.pttMode

    var body: some View {
        Card("맥") {
            HStack {
                Text("통화 단축키").foregroundStyle(Theme.text)
                Spacer()
                Picker("", selection: $hotkey) {
                    ForEach(HotKey.presets.indices, id: \.self) { Text(HotKey.presets[$0].label).tag($0) }
                }
                .labelsHidden().fixedSize()
                .onChange(of: hotkey) { HotKey.index = hotkey }
            }
            Picker("단축키 동작", selection: $mode) {
                Text("누르고 말하기").tag(MacCall.PTTMode.hold)
                Text("눌러서 켜고 끄기").tag(MacCall.PTTMode.toggle)
            }
            .pickerStyle(.segmented)
            .onChange(of: mode) { MacCall.pttMode = mode }
            Text("다른 앱이 같은 단축키를 쓰고 있으면 등록되지 않아요.").font(.footnote).foregroundStyle(Theme.text3)
            HStack {
                Text("호출어 리스너: \(wake.label)").foregroundStyle(Theme.text)
                Spacer()
                wakeButton(wake)
            }
            if let e = wake.error { Text(e).font(.footnote).foregroundStyle(Theme.danger).textSelection(.enabled) }
        }
    }
}
