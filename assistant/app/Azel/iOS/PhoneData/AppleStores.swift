import Contacts
import CoreLocation
import CoreMotion
import EventKit
import Foundation
import HealthKit
import MediaPlayer

/// The real `PhoneStores` on the iPhone (PHONE_DATA.md §1). Read only: nothing here writes to Health, Calendar or
/// Contacts. Location is one fix per call, never tracked.
final class AppleStores: PhoneStores {
    static let shared = AppleStores()
    let health = HKHealthStore()
    let events = EKEventStore()
    let contactStore = CNContactStore()

    // MARK: HealthKit
    static let quantityTypes: [HealthQuantity: (HKQuantityTypeIdentifier, HKUnit, HKStatisticsOptions)] = [
        .steps: (.stepCount, .count(), .cumulativeSum),
        .distance: (.distanceWalkingRunning, .meter(), .cumulativeSum),
        .activeEnergy: (.activeEnergyBurned, .kilocalorie(), .cumulativeSum),
        .exercise: (.appleExerciseTime, .minute(), .cumulativeSum),
        .heartRate: (.heartRate, HKUnit.count().unitDivided(by: .minute()), [.discreteAverage, .discreteMin, .discreteMax]),
        .restingHeartRate: (.restingHeartRate, HKUnit.count().unitDivided(by: .minute()), .discreteAverage),
        .hrv: (.heartRateVariabilitySDNN, .secondUnit(with: .milli), .discreteAverage),
        .weight: (.bodyMass, .gramUnit(with: .kilo), .mostRecent),
    ]

    static func readTypes(_ c: PhoneCategory) -> Set<HKObjectType> {
        switch c {
        case .health:
            return Set(quantityTypes.values.map { HKQuantityType($0.0) as HKObjectType } + [HKCategoryType(.mindfulSession)])
        case .sleep: return [HKCategoryType(.sleepAnalysis)]
        case .workouts: return [HKObjectType.workoutType(), HKQuantityType(.activeEnergyBurned), HKQuantityType(.distanceWalkingRunning)]
        case .cycle: return [HKCategoryType(.menstrualFlow)]
        default: return []
        }
    }

    func daily(_ q: HealthQuantity, from: Date, to: Date) async throws -> [Date: DayStat] {
        guard HKHealthStore.isHealthDataAvailable() else { throw PhoneDataError.denied }
        if q == .mindful {  // a category: minutes summed per day
            var out: [Date: DayStat] = [:]
            for s in try await samples(HKCategoryType(.mindfulSession), from: from, to: to) {
                let day = PhoneData.calendar.startOfDay(for: s.startDate)
                out[day, default: DayStat()].sum = (out[day]?.sum ?? 0) + s.endDate.timeIntervalSince(s.startDate) / 60
            }
            return out
        }
        guard let (id, unit, options) = Self.quantityTypes[q] else { return [:] }
        let anchor = PhoneData.calendar.startOfDay(for: from)
        let query = HKStatisticsCollectionQuery(quantityType: HKQuantityType(id),
                                                quantitySamplePredicate: HKQuery.predicateForSamples(withStart: from, end: to),
                                                options: options, anchorDate: anchor, intervalComponents: DateComponents(day: 1))
        return try await withCheckedThrowingContinuation { cont in
            query.initialResultsHandler = { _, result, error in
                if let error { return cont.resume(throwing: error) }
                var out: [Date: DayStat] = [:]
                result?.enumerateStatistics(from: from, to: to) { s, _ in
                    let stat = DayStat(sum: s.sumQuantity()?.doubleValue(for: unit), avg: s.averageQuantity()?.doubleValue(for: unit),
                                       min: s.minimumQuantity()?.doubleValue(for: unit), max: s.maximumQuantity()?.doubleValue(for: unit),
                                       latest: s.mostRecentQuantity()?.doubleValue(for: unit))
                    if stat != DayStat() { out[PhoneData.calendar.startOfDay(for: s.startDate)] = stat }
                }
                cont.resume(returning: out)
            }
            health.execute(query)
        }
    }

    private func samples(_ type: HKSampleType, from: Date, to: Date) async throws -> [HKSample] {
        try await withCheckedThrowingContinuation { cont in
            let q = HKSampleQuery(sampleType: type, predicate: HKQuery.predicateForSamples(withStart: from, end: to),
                                  limit: HKObjectQueryNoLimit, sortDescriptors: nil) { _, s, error in
                if let error { cont.resume(throwing: error) } else { cont.resume(returning: s ?? []) }
            }
            health.execute(q)
        }
    }

    func sleep(from: Date, to: Date) async throws -> [SleepSample] {
        // HKCategoryValueSleepAnalysis raw values: 0 in bed, 1 asleep, 2 awake, 3 core, 4 deep, 5 REM
        let stages: [Int: SleepStage] = [0: .inBed, 1: .asleep, 2: .awake, 3: .core, 4: .deep, 5: .rem]
        return try await samples(HKCategoryType(.sleepAnalysis), from: from, to: to).compactMap {
            guard let c = $0 as? HKCategorySample, let st = stages[c.value] else { return nil }
            return SleepSample(start: c.startDate, end: c.endDate, stage: st)
        }
    }

    static let workoutNames: [HKWorkoutActivityType: String] = [
        .running: "running", .walking: "walking", .cycling: "cycling", .swimming: "swimming", .hiking: "hiking",
        .traditionalStrengthTraining: "strength", .functionalStrengthTraining: "strength",
        .highIntensityIntervalTraining: "hiit", .yoga: "yoga", .socialDance: "dance", .cardioDance: "dance",
        .elliptical: "elliptical", .rowing: "rowing", .stairClimbing: "stairs", .coreTraining: "core", .pilates: "pilates",
        .soccer: "soccer", .basketball: "basketball", .tennis: "tennis", .badminton: "badminton"]

    func workouts(from: Date, to: Date) async throws -> [WorkoutSample] {
        try await samples(.workoutType(), from: from, to: to).compactMap {
            guard let w = $0 as? HKWorkout else { return nil }
            let kcal = w.statistics(for: HKQuantityType(.activeEnergyBurned))?.sumQuantity()?.doubleValue(for: .kilocalorie())
            let dist = w.totalDistance?.doubleValue(for: .meter())
            return WorkoutSample(type: Self.workoutNames[w.workoutActivityType] ?? "other", start: w.startDate,
                                 minutes: w.duration / 60, kcal: kcal, distanceM: dist)
        }
    }

    func cycle(from: Date, to: Date) async throws -> [Date: String] {
        let names = [1: "unspecified", 2: "light", 3: "medium", 4: "heavy", 5: "none"]  // HKCategoryValueVaginalBleeding raw values
        var out: [Date: String] = [:]
        for s in try await samples(HKCategoryType(.menstrualFlow), from: from, to: to) {
            if let c = s as? HKCategorySample, let n = names[c.value] { out[PhoneData.calendar.startOfDay(for: c.startDate)] = n }
        }
        return out
    }

    // MARK: EventKit
    func events(from: Date, to: Date) async throws -> [EventItem] {
        guard EKEventStore.authorizationStatus(for: .event) == .fullAccess else { throw PhoneDataError.denied }
        return events.events(matching: events.predicateForEvents(withStart: from, end: to, calendars: nil)).map {
            EventItem(title: $0.title ?? "", start: $0.startDate, end: $0.endDate, allDay: $0.isAllDay,
                      calendar: $0.calendar?.title ?? "", location: $0.location)
        }
    }

    func reminders(list: String?) async throws -> [ReminderItem] {
        guard EKEventStore.authorizationStatus(for: .reminder) == .fullAccess else { throw PhoneDataError.denied }
        let cals = events.calendars(for: .reminder).filter { list == nil || $0.title.localizedCaseInsensitiveContains(list!) }
        if cals.isEmpty { return [] }
        let pred = events.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: cals)
        let found: [EKReminder] = await withCheckedContinuation { cont in events.fetchReminders(matching: pred) { cont.resume(returning: $0 ?? []) } }
        return found.map {
            ReminderItem(title: $0.title ?? "", due: $0.dueDateComponents.flatMap { PhoneData.calendar.date(from: $0) },
                         list: $0.calendar?.title ?? "", priority: $0.priority)
        }
    }

    // MARK: Contacts
    func contacts(named name: String) async throws -> [ContactItem] {
        guard CNContactStore.authorizationStatus(for: .contacts) == .authorized else { throw PhoneDataError.denied }
        let keys: [CNKeyDescriptor] = [CNContactFormatter.descriptorForRequiredKeys(for: .fullName),
                                       CNContactPhoneNumbersKey as CNKeyDescriptor, CNContactEmailAddressesKey as CNKeyDescriptor,
                                       CNContactBirthdayKey as CNKeyDescriptor, CNContactOrganizationNameKey as CNKeyDescriptor]
        let store = contactStore
        return try await Task.detached {
            try store.unifiedContacts(matching: CNContact.predicateForContacts(matchingName: name), keysToFetch: keys).map { c in
                ContactItem(name: CNContactFormatter.string(from: c, style: .fullName) ?? name,
                            phones: c.phoneNumbers.map { (CNLabeledValue<NSString>.localizedString(forLabel: $0.label ?? "") , $0.value.stringValue) },
                            emails: c.emailAddresses.map { $0.value as String }, birthday: c.birthday,
                            organization: c.organizationName.isEmpty ? nil : c.organizationName)
            }
        }.value
    }

    // MARK: Location and motion (on demand only)
    func place(withMotion: Bool) async throws -> PlaceFix {
        let loc = try await LocationOnce().fix()
        let mark = try? await CLGeocoder().reverseGeocodeLocation(loc, preferredLocale: Locale(identifier: "ko_KR")).first
        let place = [mark?.administrativeArea, mark?.locality, mark?.subLocality].compactMap { $0 }
            .reduce(into: [String]()) { if $0.last != $1 { $0.append($1) } }.joined(separator: " ")
        let name = mark?.areasOfInterest?.first
        return PlaceFix(name: name, place: place.isEmpty ? mark?.name : place, activity: withMotion ? await motion() : nil)
    }

    private func motion() async -> String? {
        guard CMMotionActivityManager.isActivityAvailable() else { return nil }
        let now = Date()
        return await withCheckedContinuation { cont in
            CMMotionActivityManager().queryActivityStarting(from: now.addingTimeInterval(-300), to: now, to: .main) { acts, _ in
                guard let a = acts?.last else { return cont.resume(returning: nil) }
                cont.resume(returning: a.automotive ? "automotive" : a.cycling ? "cycling" : a.running ? "running"
                            : a.walking ? "walking" : a.stationary ? "stationary" : nil)
            }
        }
    }

    // MARK: Music
    func music() async throws -> MusicNow {
        guard MPMediaLibrary.authorizationStatus() == .authorized else { throw PhoneDataError.denied }
        return await MainActor.run {
            let player = MPMusicPlayerController.systemMusicPlayer
            let item = player.nowPlayingItem
            let recent = (MPMediaQuery.songs().items ?? []).filter { $0.lastPlayedDate != nil }
                .sorted { $0.lastPlayedDate! > $1.lastPlayedDate! }.prefix(5)
                .compactMap { i in i.title.map { (title: $0, artist: i.artist) } }
            return MusicNow(title: item?.title, artist: item?.artist, album: item?.albumTitle,
                            playing: player.playbackState == .playing, recent: Array(recent))
        }
    }
}

/// One `requestLocation` fix (when-in-use), then the manager goes away: never tracked.
@MainActor
private final class LocationOnce: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private var cont: CheckedContinuation<CLLocation, Error>?
    private var keep: LocationOnce?

    func fix() async throws -> CLLocation {
        guard [.authorizedWhenInUse, .authorizedAlways].contains(manager.authorizationStatus) else { throw PhoneDataError.denied }
        return try await withCheckedThrowingContinuation { c in
            cont = c; keep = self
            manager.delegate = self
            manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
            manager.requestLocation()
        }
    }

    nonisolated func locationManager(_ m: CLLocationManager, didUpdateLocations locs: [CLLocation]) {
        Task { @MainActor in if let l = locs.last { finish(.success(l)) } }
    }

    nonisolated func locationManager(_ m: CLLocationManager, didFailWithError error: Error) {
        Task { @MainActor in finish(.failure(error)) }
    }

    private func finish(_ r: Result<CLLocation, Error>) {
        cont?.resume(with: r); cont = nil; keep = nil
    }
}
