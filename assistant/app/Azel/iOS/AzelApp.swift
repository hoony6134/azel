import Intents
import SwiftUI
import UserNotifications

@main
struct AzelApp: App {
    @StateObject private var model = AppModel(sessionPrefix: "iphone")
    @StateObject private var history = HistoryModel()
    @StateObject private var schedule = ScheduleModel()
    @Environment(\.scenePhase) private var scenePhase
    @UIApplicationDelegateAdaptor(PushAppDelegate.self) private var appDelegate

    init() {
        _ = CallManager.shared // PushKit must be registered on every launch, including background VoIP launches
        Recordings.cleanup()
        Config.migrateAddresses()
        WatchSync.shared.start()
        UNUserNotificationCenter.current().delegate = TaskNotificationHandler.shared
        UNUserNotificationCenter.current().setNotificationCategories(PushAction.categories)
        LiveActivities.shared.start()
        PhoneDataSync.start()
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
                    RootView(model: model, history: history, schedule: schedule).phoneDataFirstRun()
                } else {
                    SetupFlow { model.configured = Config.token != nil; Task { await model.refresh() } }
                }
                #else
                if model.configured {
                    RootView(model: model, history: history, schedule: schedule).phoneDataFirstRun()
                } else {
                    SetupFlow { model.configured = Config.token != nil; Task { await model.refresh() } }
                }
                #endif
            }
            .preferredColorScheme(nil)
            .tint(Theme.celadon)
            .onOpenURL { url in
                if let id = TaskModel.taskID(from: url) { Task { await TaskModel.shared.openTask(id: id) } }
                // the Azel 부르기 widget; during a call the Live Activity's azel://call just opens the app
                if url.host == "call", model.configured { CallManager.shared.startCall() }
            }
            // "아젤한테 전화해줘" through Siri's calling intent: the documented way to start an
            // outgoing CallKit call while the app is in the background or the phone is locked.
            .onContinueUserActivity(NSStringFromClass(INStartCallIntent.self)) { activity in
                let who = (activity.interaction?.intent as? INStartCallIntent)?.contacts?.first
                CallManager.shared.startCall(persona: StartCallIntentHandler.persona(handle: who?.personHandle?.value, named: who?.displayName),
                                             ringbackOnFailure: true)
            }
            .onChange(of: scenePhase, initial: true) {
                if scenePhase == .active { WatchSync.shared.push(); Task { await model.refresh(); await history.refresh(); await PersonaModel.shared.refresh(); WatchSync.shared.push() } }
                if scenePhase == .active {
                    ApprovalCenter.shared.start()
                    Task { await ApprovalCenter.shared.refresh() }   // stale sheets never survive a return
                    Task { await PhoneDataSync.upload(force: false) }
                } else if !CallManager.shared.inCall {
                    ApprovalCenter.shared.stop()   // during a call the app keeps running, and so does the check
                }
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
            ChatScreen()   // its own split view: threads | chat (side by side on iPad)
                .tabItem { Label("채팅", systemImage: "bubble.left.and.text.bubble.right") }.tag(4)
            NavigationStack {
                HistoryList(model: history, onCall: { CallManager.shared.startCall() })
                    .navigationTitle("기록")
                    .navigationDestination(for: HistoryItem.self) { HistoryDetailView(item: $0, history: history) }
            }
            .tabItem { Label("기록", systemImage: "clock") }.tag(1)
            NavigationStack { ApprovalsScreen(model: model) }
                .tabItem { Label("승인", systemImage: "checkmark.shield") }.tag(2)
                .badge(approvals.pending.count)
            NavigationStack { SettingsScreen(model: model, history: history) { PhoneDataCard() } }
                .tabItem { Label("설정", systemImage: "gearshape") }.tag(3)
        }
        .fullScreenCover(isPresented: Binding(get: { call.inCall }, set: { if !$0 { call.hangUp() } })) {
            CallScreen()
        }
        .approvalSheet(active: !call.inCall)
        .autoGrantSheet()
    }
}

/// Tapping a "task finished" notification opens that task (payload `task_id` or `task`); an
/// approval push (`type: "approval"`) opens the approval sheet.
final class TaskNotificationHandler: NSObject, UNUserNotificationCenterDelegate {
    static let shared = TaskNotificationHandler()

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        let action = response.actionIdentifier
        if action != UNNotificationDefaultActionIdentifier && action != UNNotificationDismissActionIdentifier {
            await PushAction.perform(action: action, userInfo: response.notification.request.content.userInfo)
            return
        }
        if TaskModel.isOvernightPush(response.notification.request.content.userInfo) {
            await TaskModel.shared.refresh()
            await MainActor.run { TaskModel.shared.showList = true }   // the 작업 list, from the home card
            return
        }
        if let id = AutoGrantCenter.grantID(from: response.notification.request.content.userInfo) {
            await AutoGrantCenter.shared.openGrant(id: id)
            return
        }
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
    /// Siri's INStartCallIntent is handled in the app (no Intents extension).
    func application(_ application: UIApplication, handlerFor intent: INIntent) -> Any? {
        intent is INStartCallIntent ? StartCallIntentHandler() : nil
    }

    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        if application.isProtectedDataAvailable { Keychain.ensureReadableWhileLocked() }   // a locked phone must reach the gateway
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

/// "아젤한테 전화해줘": any contact Siri hears is taken as the assistant (or a persona by name);
/// the call itself starts in the app (continueInApp → onContinueUserActivity).
final class StartCallIntentHandler: NSObject, INStartCallIntentHandling {
    /// CarPlay's and the Phone app's recents call back with the handle the call was made with (the
    /// persona id); Siri gives a spoken name.
    @MainActor static func persona(handle: String? = nil, named name: String?) -> Persona? {
        PersonaModel.shared.persona(id: handle) ?? PersonaModel.shared.match(name: name) ?? PersonaModel.shared.primary
    }

    func resolveContacts(for intent: INStartCallIntent, with completion: @escaping ([INStartCallContactResolutionResult]) -> Void) {
        let people = intent.contacts ?? []
        if people.isEmpty {
            let me = INPerson(personHandle: INPersonHandle(value: AssistantName.current, type: .unknown), nameComponents: nil,
                              displayName: AssistantName.current, image: nil, contactIdentifier: nil, customIdentifier: "azel")
            completion([.success(with: me)])
        } else {
            completion(people.map { .success(with: $0) })
        }
    }

    func handle(intent: INStartCallIntent, completion: @escaping (INStartCallIntentResponse) -> Void) {
        completion(INStartCallIntentResponse(code: .continueInApp, userActivity: nil))
    }
}
