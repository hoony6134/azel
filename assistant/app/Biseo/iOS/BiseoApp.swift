import SwiftUI

@main
struct BiseoApp: App {
    @StateObject private var model = AppModel(sessionPrefix: "iphone")
    @Environment(\.scenePhase) private var scenePhase

    init() {
        _ = CallManager.shared // PushKit must be registered on every launch, including background VoIP launches
        Recordings.cleanup()
    }

    var body: some Scene {
        WindowGroup {
            Group {
                if model.configured {
                    RootView(model: model)
                } else {
                    SetupFlow { model.configured = Config.token != nil; Task { await model.refresh() } }
                }
            }
            .preferredColorScheme(nil)
            .tint(Theme.celadon)
            .onChange(of: scenePhase, initial: true) {
                if scenePhase == .active { Task { await model.refresh() } }
            }
        }
    }
}

/// Three tabs (DESIGN §7.1); the call screen covers them whenever a call is up, including one
/// CallKit answered from the lock screen.
struct RootView: View {
    @ObservedObject var model: AppModel
    @ObservedObject private var call = CallManager.shared
    @State private var tab = 0

    var body: some View {
        TabView(selection: $tab) {
            NavigationStack { HomeView(model: model, tab: $tab) }
                .tabItem { Label("홈", systemImage: "circle.circle") }.tag(0)
            NavigationStack { ApprovalsScreen(model: model) }
                .tabItem { Label("승인", systemImage: "checkmark.shield") }.tag(1)
                .badge(model.approvals.count)
            NavigationStack { SettingsScreen(model: model) }
                .tabItem { Label("설정", systemImage: "gearshape") }.tag(2)
        }
        .fullScreenCover(isPresented: Binding(get: { call.inCall }, set: { if !$0 { call.hangUp() } })) {
            CallScreen()
        }
    }
}
