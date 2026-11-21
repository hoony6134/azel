import SwiftUI
import UserNotifications

@main
struct BiseoApp: App {
    @StateObject private var model = AppModel(sessionPrefix: "iphone")
    @StateObject private var history = HistoryModel()
    @StateObject private var schedule = ScheduleModel()
    @Environment(\.scenePhase) private var scenePhase
    @UIApplicationDelegateAdaptor(PushAppDelegate.self) private var appDelegate

    init() {
        _ = CallManager.shared // PushKit must be registered on every launch, including background VoIP launches
        Recordings.cleanup()
        Config.migrateAddresses()
        UNUserNotificationCenter.current().delegate = TaskNotificationHandler.shared
        LiveActivities.shared.start()
        #if DEBUG
        if CommandLine.arguments.contains("-liveActivityPreview") { DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { LiveActivities.shared.debugPreview() } }
        #endif
    }

    var body: some Scene {
        WindowGroup {
            Group {
                #if DEBUG
                if let i = CommandLine.arguments.firstIndex(of: "-callPreview"), i + 1 < CommandLine.arguments.count {
                    CallScreen().onAppear { CallManager.shared.debugPreview(state: CommandLine.arguments[i + 1]) }
                } else if model.configured {
                    RootView(model: model, history: history, schedule: schedule)
                } else {
                    SetupFlow { model.configured = Config.token != nil; Task { await model.refresh() } }
                }
                #else
                if model.configured {
                    RootView(model: model, history: history, schedule: schedule)
                } else {
                    SetupFlow { model.configured = Config.token != nil; Task { await model.refresh() } }
                }
                #endif
            }
            .preferredColorScheme(nil)
            .tint(Theme.celadon)
            .onOpenURL { url in if let id = TaskModel.taskID(from: url) { Task { await TaskModel.shared.openTask(id: id) } } }
            .onChange(of: scenePhase, initial: true) {
                if scenePhase == .active { Task { await model.refresh(); await history.refresh(); await PersonaModel.shared.refresh() } }
                if scenePhase == .active { ApprovalCenter.shared.start() } else { ApprovalCenter.shared.stop() }
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
    @ObservedObject private var approvals = ApprovalCenter.shared
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
                .badge(approvals.pending.count)
            NavigationStack { SettingsScreen(model: model, history: history) }
                .tabItem { Label("설정", systemImage: "gearshape") }.tag(3)
        }
        .fullScreenCover(isPresented: Binding(get: { call.inCall }, set: { if !$0 { call.hangUp() } })) {
            CallScreen()
        }
        .approvalSheet(active: !call.inCall)
    }
}

/// Tapping a "task finished" notification opens that task (payload `task_id` or `task`); an
/// approval push (`type: "approval"`) opens the approval sheet.
final class TaskNotificationHandler: NSObject, UNUserNotificationCenterDelegate {
    static let shared = TaskNotificationHandler()

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        if let id = ApprovalCenter.approvalID(from: response.notification.request.content.userInfo) {
            await ApprovalCenter.shared.open(id: id)
            return
        }
        guard let id = TaskModel.taskID(from: response.notification.request.content.userInfo) else { return }
        await TaskModel.shared.openTask(id: id)
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }
}

/// Alert pushes (task finished, missed scheduled calls): ask for permission once, on first
/// launch, then register with APNs on every launch so a changed token is re-sent.
final class PushAppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        Task { @MainActor in
            let center = UNUserNotificationCenter.current()
            let settings = await center.notificationSettings()
            if settings.authorizationStatus == .notDetermined {
                _ = try? await center.requestAuthorization(options: [.alert, .sound, .badge])
            }
            application.registerForRemoteNotifications()
        }
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        CallManager.shared.setAlertToken(deviceToken)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        Log.call.error("alert push registration failed: \(error.localizedDescription, privacy: .public)")
    }
}
