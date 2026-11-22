import BackgroundTasks
import Contacts
import CoreLocation
import CoreMotion
import EventKit
import HealthKit
import MediaPlayer
import SwiftUI

/// Live answers in calls and the background upload of daily summaries (PHONE_DATA.md §3).
enum PhoneDataSync {
    static let taskID = "xyz.scian.biseo.phone-sync"
    static let responder = PhoneResponder(stores: AppleStores.shared)
    static let lastUploadKey = "phone.lastUpload"
    private static var observers: [HKObserverQuery] = []

    /// At launch (BGTask handlers must be registered before launch finishes).
    static func start() {
        CallClient.phoneHello = { PhoneData.helloFrame() }
        CallClient.phoneQuery = { await responder.reply(to: $0) }
        BGTaskScheduler.shared.register(forTaskWithIdentifier: taskID, using: nil) { task in
            guard let task = task as? BGAppRefreshTask else { return task.setTaskCompleted(success: false) }
            schedule()
            let work = Task { await upload(force: true); task.setTaskCompleted(success: true) }
            task.expirationHandler = { work.cancel() }
        }
        observe()
        schedule()
    }

    static func schedule() {
        guard wantsSync else { return }
        let r = BGAppRefreshTaskRequest(identifier: taskID)
        r.earliestBeginDate = Date().addingTimeInterval(4 * 3600)
        try? BGTaskScheduler.shared.submit(r)
    }

    static var wantsSync: Bool { PhoneData.isOn(.sync) && PhoneCategory.synced.contains(where: PhoneData.isOn) }

    /// HealthKit background delivery: new steps, sleep or workouts wake the app (at most hourly) to upload.
    static func observe() {
        let store = AppleStores.shared.health
        observers.forEach(store.stop)
        observers = []
        guard wantsSync, HKHealthStore.isHealthDataAvailable() else { return }
        let types: [HKSampleType] = [(.health, HKQuantityType(.stepCount)), (.sleep, HKCategoryType(.sleepAnalysis)),
                                     (.workouts, HKObjectType.workoutType())].filter { PhoneData.isOn($0.0) }.map(\.1)
        for type in types {
            let q = HKObserverQuery(sampleType: type, predicate: nil) { _, done, _ in
                Task { await upload(force: false); done() }
            }
            store.execute(q)
            observers.append(q)
            store.enableBackgroundDelivery(for: type, frequency: .hourly) { _, _ in }
        }
    }

    /// The last 7 days' summaries → PUT /v1/phone/days. Not in a secret call; at most every 15 minutes unless forced.
    static func upload(force: Bool) async {
        guard wantsSync, !PhoneData.paused, Config.token != nil else { return }
        let last = PhoneData.defaults.double(forKey: lastUploadKey)
        if !force && Date().timeIntervalSince1970 - last < 900 { return }
        let cal = PhoneData.calendar
        let today = cal.startOfDay(for: Date())
        let r = await PhoneSummary.days(from: cal.date(byAdding: .day, value: -6, to: today)!, to: today,
                                        stores: AppleStores.shared,
                                        health: PhoneData.isOn(.health) ? HealthQuantity.allCases : [],
                                        sleep: PhoneData.isOn(.sleep), workouts: PhoneData.isOn(.workouts),
                                        cycle: PhoneData.isOn(.cycle))
        do {
            _ = try await Gateway.send("PUT", "/v1/phone/days", json: ["days": r.days])
            PhoneData.defaults.set(Date().timeIntervalSince1970, forKey: lastUploadKey)
        } catch {
            Log.call.error("phone sync failed: \(error.localizedDescription, privacy: .public)")
        }
    }
}

/// iOS permission prompts, only when a toggle is turned on.
enum PhonePermissions {
    private static let location = CLLocationManager()  // kept alive so its prompt stays up

    static func request(_ c: PhoneCategory) async {
        switch c {
        case .health, .sleep, .workouts, .cycle:
            guard HKHealthStore.isHealthDataAvailable() else { return }
            try? await AppleStores.shared.health.requestAuthorization(toShare: [], read: AppleStores.readTypes(c))
        case .calendar: _ = try? await AppleStores.shared.events.requestFullAccessToEvents()
        case .reminders: _ = try? await AppleStores.shared.events.requestFullAccessToReminders()
        case .contacts: _ = try? await AppleStores.shared.contactStore.requestAccess(for: .contacts)
        case .location: await MainActor.run { location.requestWhenInUseAuthorization() }
        case .motion:  // the first query shows the prompt
            if CMMotionActivityManager.isActivityAvailable() {
                CMMotionActivityManager().queryActivityStarting(from: Date().addingTimeInterval(-60), to: Date(), to: .main) { _, _ in }
            }
        case .music: _ = await withCheckedContinuation { c in MPMediaLibrary.requestAuthorization { c.resume(returning: $0) } }
        case .sync: break
        }
    }

    /// nil when the status is fine or unknowable (HealthKit never says whether reading was denied).
    static func problem(_ c: PhoneCategory) -> String? {
        let denied: Bool
        switch c {
        case .calendar: denied = [.denied, .restricted, .writeOnly].contains(EKEventStore.authorizationStatus(for: .event))
        case .reminders: denied = [.denied, .restricted, .writeOnly].contains(EKEventStore.authorizationStatus(for: .reminder))
        case .contacts: denied = [.denied, .restricted].contains(CNContactStore.authorizationStatus(for: .contacts))
        case .location: denied = [.denied, .restricted].contains(location.authorizationStatus)
        case .motion: denied = [.denied, .restricted].contains(CMMotionActivityManager.authorizationStatus())
        case .music: denied = [.denied, .restricted].contains(MPMediaLibrary.authorizationStatus())
        default: denied = false
        }
        return denied ? "설정 앱에서 허용해 주세요" : nil
    }
}

/// Settings → "아이폰 데이터": one row on the settings screen.
struct PhoneDataCard: View {
    var body: some View {
        Card("아이폰 데이터") {
            NavigationLink { PhoneDataScreen() } label: {
                HStack {
                    Text("건강·수면·캘린더·위치 등").foregroundStyle(Theme.text)
                    Spacer()
                    Text("\(PhoneCategory.allCases.filter { $0 != .sync && PhoneData.isOn($0) }.count)개 켬").foregroundStyle(Theme.text3)
                    Image(systemName: "chevron.right").foregroundStyle(Theme.text3)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
    }
}

struct PhoneDataScreen: View {
    @State private var on: [PhoneCategory: Bool] = Dictionary(uniqueKeysWithValues: PhoneCategory.allCases.map { ($0, PhoneData.isOn($0)) })
    @State private var confirmDelete = false
    @State private var note: String?

    var body: some View {
        Form {
            Section {
                ForEach(PhoneCategory.allCases.filter { $0 != .sync }) { c in toggle(c) }
            } footer: {
                Text("켜는 항목만 아젤이 읽어요. 위치는 물어볼 때 한 번만 확인하고 저장하지 않아요. 비밀 통화 중에는 아무것도 읽지 않아요.")
            }
            Section {
                toggle(.sync)
                Button("서버에 올린 요약 모두 지우기", role: .destructive) { confirmDelete = true }
            } header: { Text("통화 밖에서도 답하기") } footer: {
                Text("건강·수면·운동의 하루 요약만 게이트웨이에 올리고 30일 뒤 지워요. 원래 기록은 아이폰에만 있어요." + lastUpload)
            }
            if let note { Section { Text(note).foregroundStyle(Theme.text2) } }
        }
        .navigationTitle("아이폰 데이터")
        .confirmationDialog("서버에 올린 아이폰 요약을 모두 지울까요?", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("모두 지우기", role: .destructive) { Task { await deleteAll() } }
        }
    }

    private var lastUpload: String {
        let t = PhoneData.defaults.double(forKey: PhoneDataSync.lastUploadKey)
        return t > 0 ? "\n마지막으로 올린 시각: " + Date(timeIntervalSince1970: t).formatted(date: .abbreviated, time: .shortened) : ""
    }

    private func toggle(_ c: PhoneCategory) -> some View {
        Toggle(isOn: Binding(get: { on[c] ?? false }, set: { v in on[c] = v; changed(c, v) })) {
            VStack(alignment: .leading) {
                Text(c.title)
                if on[c] == true, let p = PhonePermissions.problem(c) { Text(p).font(.caption).foregroundStyle(Theme.amber) }
            }
        }
    }

    private func changed(_ c: PhoneCategory, _ value: Bool) {
        PhoneData.set(c, value)
        Task {
            if value { await PhonePermissions.request(c) }
            if c == .sync && !value { await deleteAll() }
            PhoneDataSync.observe()
            PhoneDataSync.schedule()
            if value && (PhoneCategory.synced.contains(c) || c == .sync) { await PhoneDataSync.upload(force: true) }
            on = on  // re-read permission notes
        }
    }

    private func deleteAll() async {
        do {
            _ = try await Gateway.send("DELETE", "/v1/phone/days")
            PhoneData.defaults.removeObject(forKey: PhoneDataSync.lastUploadKey)
            note = "서버에 올린 요약을 모두 지웠어요."
        } catch { note = "지우지 못했어요: \(error.localizedDescription)" }
    }
}
