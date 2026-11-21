import SwiftUI

@main
struct BiseoApp: App {
    @State private var configured = Config.token != nil
    @State private var pendingApprovals = 0
    @Environment(\.scenePhase) private var scenePhase

    init() {
        _ = CallManager.shared // PushKit must be registered on every launch, including background VoIP launches
        Recordings.cleanup()
    }

    var body: some Scene {
        WindowGroup {
            if configured {
                TabView {
                    CallView().tabItem { Label("전화", systemImage: "phone.fill") }
                    SettingsView().tabItem { Label("설정", systemImage: "gearshape") }
                    UsageView().tabItem { Label("사용량", systemImage: "chart.bar") }
                    StatusView().tabItem { Label("상태", systemImage: "server.rack") }
                    ApprovalsView(pending: $pendingApprovals).tabItem { Label("승인", systemImage: "checkmark.shield") }
                        .badge(pendingApprovals)
                }
                .onChange(of: scenePhase, initial: true) {
                    guard scenePhase == .active else { return }
                    Task { if let items = try? await Gateway.approvals() { pendingApprovals = items.count } }
                }
            } else {
                NavigationStack { SetupView { configured = true } }
            }
        }
    }
}
