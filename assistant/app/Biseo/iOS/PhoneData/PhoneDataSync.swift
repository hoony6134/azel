import BackgroundTasks
import HealthKit
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
        let all: [(PhoneCategory, HKSampleType)] = [(.health, HKQuantityType(.stepCount)), (.sleep, HKCategoryType(.sleepAnalysis)),
                                                    (.workouts, HKObjectType.workoutType())]
        let types = all.filter { PhoneData.isOn($0.0) }.map(\.1)
        for type in types {
            let q = HKObserverQuery(sampleType: type, predicate: nil) { _, done, _ in
                Task { await upload(force: false); done() }
            }
            store.execute(q)
            observers.append(q)
            store.enableBackgroundDelivery(for: type, frequency: .hourly) { _, _ in }
        }
    }

    /// The last 7 days' summaries → PUT /v1/phone/days, at most every 15 minutes unless forced.
    static func upload(force: Bool) async {
        guard wantsSync, Config.token != nil else { return }
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

    /// `DELETE /v1/phone/days` (all), or one category's synced fields when its toggle goes off.
    static func deleteSynced(kind: String? = nil) async throws {
        _ = try await Gateway.send("DELETE", "/v1/phone/days" + (kind.map { "?kind=\($0)" } ?? ""))
        if kind == nil { PhoneData.defaults.removeObject(forKey: lastUploadKey) }
    }
}
