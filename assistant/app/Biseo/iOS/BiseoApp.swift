import SwiftUI

@main
struct BiseoApp: App {
    @StateObject private var model = AppModel(sessionPrefix: "iphone")
    @StateObject private var history = HistoryModel()
    @StateObject private var schedule = ScheduleModel()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        _ = CallManager.shared // PushKit must be registered on every launch, including background VoIP launches
        Recordings.cleanup()
    }

    var body: some Scene {
        WindowGroup {
            Group {
                if model.configured {
                    RootView(model: model, history: history, schedule: schedule)
                } else {
                    SetupFlow { model.configured = Config.token != nil; Task { await model.refresh() } }
                }
            }
            .preferredColorScheme(nil)
            .tint(Theme.celadon)
            .onChange(of: scenePhase, initial: true) {
                if scenePhase == .active { Task { await model.refresh(); await history.refresh() } }
            }
        }
    }
}

/// Three tabs (DESIGN §7.1); the call screen covers them whenever a call is up, including one
/// CallKit answered from the lock screen.
struct RootView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var history: HistoryModel
    @ObservedObject var schedule: ScheduleModel
    @ObservedObject private var call = CallManager.shared
    @State private var tab = 0

    var body: some View {
        TabView(selection: $tab) {
            NavigationStack { HomeView(model: model, history: history, schedule: schedule, tab: $tab) }
                .tabItem { Label("홈", systemImage: "circle.circle") }.tag(0)
            NavigationStack {
                HistoryList(model: history, onCall: { CallManager.shared.startCall() })
                    .navigationTitle("기록")
                    .navigationDestination(for: HistoryItem.self) { HistoryDetailView(item: $0, history: history) }
            }
            .tabItem { Label("기록", systemImage: "clock") }.tag(1)
            NavigationStack { ApprovalsScreen(model: model) }
                .tabItem { Label("승인", systemImage: "checkmark.shield") }.tag(2)
                .badge(model.approvals.count)
            NavigationStack { SettingsScreen(model: model, history: history) }
                .tabItem { Label("설정", systemImage: "gearshape") }.tag(3)
        }
        .fullScreenCover(isPresented: Binding(get: { call.inCall }, set: { if !$0 { call.hangUp() } })) {
            CallScreen()
        }
    }
}
