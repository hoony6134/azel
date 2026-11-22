import Foundation

/// iPhone data (assistant/PHONE_DATA.md): categories, the stores the phone reads, and the pure parts (samples → day
/// summaries, query → result) that unit tests run with fake stores. Apple-framework stores live in iOS/PhoneData.
/// Every sentence is made by the gateway; this side only returns numbers and names.
enum PhoneCategory: String, CaseIterable, Identifiable {
    case health, sleep, workouts, cycle, calendar, reminders, contacts, location, motion, music, sync
    var id: String { rawValue }
    var title: String {
        switch self {
        case .health: "건강(걸음·심박·칼로리)"
        case .sleep: "수면"
        case .workouts: "운동"
        case .cycle: "생리 주기"
        case .calendar: "캘린더"
        case .reminders: "미리 알림"
        case .contacts: "연락처"
        case .location: "위치(물어볼 때만)"
        case .motion: "움직임(걷기·운전)"
        case .music: "음악"
        case .sync: "요약을 서버에 올리기"
        }
    }
    /// One line on what Azel can read (first-run setup and settings).
    var detail: String {
        switch self {
        case .health: "오늘 걸음 수, 걸은 거리, 활동 칼로리, 심박수와 몸무게"
        case .sleep: "몇 시에 자고 일어났는지, 깊은 잠과 렘수면"
        case .workouts: "운동 종류, 시간, 칼로리"
        case .cycle: "생리 기록이 있는 날"
        case .calendar: "아이폰 캘린더의 일정 제목과 시간"
        case .reminders: "미리 알림에서 아직 안 끝낸 항목"
        case .contacts: "이름으로 찾은 사람의 전화번호, 이메일, 생일"
        case .location: "물어볼 때 한 번, 지금 있는 동네와 장소 이름(저장·추적 안 함)"
        case .motion: "위치를 물을 때 걷는 중인지, 차로 이동 중인지"
        case .music: "지금 듣는 곡과 최근에 들은 곡"
        case .sync: "건강·수면·운동의 하루 요약을 올려 통화 밖에서도 답하기"
        }
    }
    /// Everything starts off except `sync`, which does nothing until a synced category is on.
    var defaultOn: Bool { self == .sync }
    static let synced: [PhoneCategory] = [.health, .sleep, .workouts, .cycle]
    /// The first-run screen's list and its "모두 켜기" (생리 주기 and 움직임 stay separate opt-ins in Settings).
    static let setup: [PhoneCategory] = [.health, .sleep, .workouts, .calendar, .reminders, .contacts, .location, .music]
    /// `DELETE /v1/phone/days?kind=` when this toggle is turned off; nil = nothing of it is synced.
    var syncedKind: String? { PhoneCategory.synced.contains(self) ? rawValue : nil }
}

/// What iOS says about a category's permission. HealthKit never tells whether reading was denied, so a
/// HealthKit category that was asked shows as `granted`.
enum PhoneAuth: Equatable {
    case granted, denied, notAsked
    /// The row's status text, and whether to offer "설정 앱에서 허용" (iOS never asks twice).
    static func status(on: Bool, auth: PhoneAuth) -> (text: String, openSettings: Bool) {
        switch (on, auth) {
        case (_, .denied): ("iOS에서 거부됨", true)
        case (true, .granted): ("켜짐", false)
        case (true, .notAsked): ("아직 안 물어봄", false)
        case (false, _): ("꺼짐", false)
        }
    }
}

/// The toggles. A secret call may read (nothing is stored from a live answer), so there is no pause for it.
enum PhoneData {
    static var defaults: UserDefaults = .standard
    static func key(_ c: PhoneCategory) -> String { "phone.\(c.rawValue)" }
    static func isOn(_ c: PhoneCategory) -> Bool { defaults.object(forKey: key(c)) as? Bool ?? c.defaultOn }
    static func set(_ c: PhoneCategory, _ on: Bool) { defaults.set(on, forKey: key(c)) }

    /// The query kinds this phone answers now (`phone_hello`).
    static func kinds() -> [String] {
        let map: [(String, PhoneCategory)] = [("health", .health), ("sleep", .sleep), ("workouts", .workouts),
                                              ("calendar", .calendar), ("reminders", .reminders), ("contacts", .contacts),
                                              ("location", .location), ("now_playing", .music)]
        return map.filter { isOn($0.1) || ($0.0 == "health" && isOn(.cycle)) }.map(\.0)
    }

    static func helloFrame() -> String {
        let data = try? JSONSerialization.data(withJSONObject: ["type": "phone_hello", "kinds": kinds()])
        return data.flatMap { String(data: $0, encoding: .utf8) } ?? #"{"type":"phone_hello","kinds":[]}"#
    }

    static let kst = TimeZone(identifier: "Asia/Seoul")!
    static var calendar: Calendar = { var c = Calendar(identifier: .gregorian); c.timeZone = kst; return c }()
    static func iso(_ d: Date) -> String {
        let f = ISO8601DateFormatter(); f.timeZone = kst; f.formatOptions = [.withInternetDateTime]
        return f.string(from: d)
    }
    static func dayString(_ d: Date) -> String {
        let c = calendar.dateComponents([.year, .month, .day], from: d)
        return String(format: "%04d-%02d-%02d", c.year!, c.month!, c.day!)
    }
    static func day(_ s: String) -> Date? {
        let p = s.split(separator: "-").compactMap { Int($0) }
        guard p.count == 3 else { return nil }
        return calendar.date(from: DateComponents(year: p[0], month: p[1], day: p[2]))
    }
}

// MARK: - What the stores hand back

enum HealthQuantity: String, CaseIterable {
    case steps, distance, activeEnergy, exercise, mindful, heartRate, restingHeartRate, hrv, weight
}

/// One day's statistics for a quantity (HKStatisticsCollection: the sources already de-duplicated).
struct DayStat: Equatable { var sum: Double?; var avg: Double?; var min: Double?; var max: Double?; var latest: Double? }

enum SleepStage: Equatable { case inBed, asleep, core, deep, rem, awake }
struct SleepSample: Equatable { var start: Date; var end: Date; var stage: SleepStage }
struct WorkoutSample: Equatable { var type: String; var start: Date; var minutes: Double; var kcal: Double?; var distanceM: Double? }
struct EventItem: Equatable { var title: String; var start: Date; var end: Date; var allDay: Bool; var calendar: String; var location: String? }
struct ReminderItem: Equatable { var title: String; var due: Date?; var list: String; var priority: Int }
struct ContactItem: Equatable {
    var name: String; var phones: [(label: String, number: String)]; var emails: [String]
    var birthday: DateComponents?; var organization: String?
    static func == (a: Self, b: Self) -> Bool { a.name == b.name && a.emails == b.emails && a.phones.map(\.number) == b.phones.map(\.number) }
}
struct PlaceFix: Equatable { var name: String?; var place: String?; var activity: String? }
struct MusicNow: Equatable { var title: String?; var artist: String?; var album: String?; var playing: Bool; var recent: [(title: String, artist: String?)]
    static func == (a: Self, b: Self) -> Bool { a.title == b.title && a.playing == b.playing && a.recent.map(\.title) == b.recent.map(\.title) }
}

enum PhoneDataError: Error { case denied }

/// The phone's data, one protocol so tests can fake all of it. Each call may throw `PhoneDataError.denied`.
protocol PhoneStores {
    /// Per-day stats keyed by the KST start of day, for days in [from, to].
    func daily(_ q: HealthQuantity, from: Date, to: Date) async throws -> [Date: DayStat]
    func sleep(from: Date, to: Date) async throws -> [SleepSample]
    func workouts(from: Date, to: Date) async throws -> [WorkoutSample]
    /// Menstrual flow per day start: none | light | medium | heavy | unspecified.
    func cycle(from: Date, to: Date) async throws -> [Date: String]
    func events(from: Date, to: Date) async throws -> [EventItem]
    func reminders(list: String?) async throws -> [ReminderItem]
    func contacts(named: String) async throws -> [ContactItem]
    func place(withMotion: Bool) async throws -> PlaceFix
    func music() async throws -> MusicNow
}

// MARK: - Samples → day summaries (PHONE_DATA.md §3b shape)

enum PhoneSummary {
    /// Union length of intervals in minutes (iPhone and Watch both write sleep: overlaps count once).
    static func unionMinutes(_ spans: [(Date, Date)]) -> Double {
        var total = 0.0, cur: (Date, Date)?
        for s in spans.sorted(by: { $0.0 < $1.0 }) where s.1 > s.0 {
            if let c = cur, s.0 <= c.1 { cur = (c.0, max(c.1, s.1)) } else {
                if let c = cur { total += c.1.timeIntervalSince(c.0) }
                cur = s
            }
        }
        if let c = cur { total += c.1.timeIntervalSince(c.0) }
        return (total / 60).rounded()
    }

    /// The night that ends on the morning of `day` (its wake-up date): samples ending in (day-1 18:00, day 18:00].
    static func night(_ samples: [SleepSample], wakeDay day: Date) -> [String: Any]? {
        let cal = PhoneData.calendar
        let hi = cal.date(byAdding: .hour, value: 18, to: day)!, lo = cal.date(byAdding: .day, value: -1, to: hi)!
        let mine = samples.filter { $0.end > lo && $0.end <= hi }
        let asleepStages: [SleepStage] = [.asleep, .core, .deep, .rem]
        let asleep = mine.filter { asleepStages.contains($0.stage) }
        func mins(_ st: [SleepStage]) -> Double { unionMinutes(mine.filter { st.contains($0.stage) }.map { ($0.start, $0.end) }) }
        let asleepMin = mins(asleepStages), inBed = mins([.inBed])
        guard asleepMin > 0 || inBed > 0 else { return nil }
        var out: [String: Any] = ["asleep_min": asleepMin, "in_bed_min": inBed]
        for (k, st) in [("core_min", SleepStage.core), ("deep_min", .deep), ("rem_min", .rem), ("awake_min", .awake)] {
            let m = mins([st]); if m > 0 { out[k] = m }
        }
        // Bedtime and wake-up from the longest block (gaps under an hour joined), so a nap doesn't move them.
        let spans = (asleep.isEmpty ? mine.filter { $0.stage == .inBed } : asleep).map { ($0.start, $0.end) }.sorted { $0.0 < $1.0 }
        var blocks: [(Date, Date)] = []
        for sp in spans {
            if let last = blocks.last, sp.0.timeIntervalSince(last.1) < 3600 { blocks[blocks.count - 1].1 = max(last.1, sp.1) }
            else { blocks.append(sp) }
        }
        if let main = blocks.max(by: { $0.1.timeIntervalSince($0.0) < $1.1.timeIntervalSince($1.0) }) {
            out["start"] = PhoneData.iso(main.0); out["end"] = PhoneData.iso(main.1)
        }
        return out
    }

    static let keys: [(HealthQuantity, String, KeyPath<DayStat, Double?>)] = [
        (.steps, "steps", \.sum), (.distance, "distance_m", \.sum), (.activeEnergy, "active_kcal", \.sum),
        (.exercise, "exercise_min", \.sum), (.mindful, "mindful_min", \.sum), (.heartRate, "hr_avg", \.avg),
        (.heartRate, "hr_min", \.min), (.heartRate, "hr_max", \.max), (.restingHeartRate, "resting_hr", \.avg),
        (.hrv, "hrv_ms", \.avg), (.weight, "weight_kg", \.latest)]

    /// Which quantities a gateway metric needs ("summary" = steps, energy, exercise, distance).
    static func quantities(for metrics: [String]?) -> [HealthQuantity] {
        guard let metrics, !metrics.isEmpty else { return HealthQuantity.allCases }
        let m: [String: [HealthQuantity]] = ["steps": [.steps], "distance": [.distance], "active_energy": [.activeEnergy],
            "exercise": [.exercise], "mindful": [.mindful], "heart_rate": [.heartRate], "resting_heart_rate": [.restingHeartRate],
            "hrv": [.hrv], "weight": [.weight], "summary": [.steps, .activeEnergy, .exercise, .distance]]
        return metrics.flatMap { m[$0] ?? [] }
    }

    /// Day summaries for [from, to] of the categories that are on. A denied category is left out (`denied` lists it).
    static func days(from: Date, to: Date, stores: PhoneStores, health: [HealthQuantity], sleep: Bool, workouts: Bool,
                     cycle: Bool) async -> (days: [[String: Any]], denied: Set<String>) {
        let cal = PhoneData.calendar
        var dates: [Date] = [], d = cal.startOfDay(for: from)
        while d <= to { dates.append(d); d = cal.date(byAdding: .day, value: 1, to: d)! }
        var out: [Date: [String: Any]] = Dictionary(uniqueKeysWithValues: dates.map { ($0, ["date": PhoneData.dayString($0)]) })
        var denied = Set<String>()
        let end = cal.date(byAdding: .day, value: 1, to: dates.last ?? to)!
        for q in Set(health) {
            do {
                let stats = try await stores.daily(q, from: dates.first ?? from, to: end)
                for (kq, k, path) in keys where kq == q {
                    for (day, s) in stats { if let v = s[keyPath: path], out[day] != nil { out[day]![k] = round(v, k) } }
                }
            } catch { denied.insert("health") }
        }
        if sleep {
            do {
                let samples = try await stores.sleep(from: cal.date(byAdding: .hour, value: -6, to: dates.first ?? from)!, to: cal.date(byAdding: .hour, value: 18, to: dates.last ?? to)!)
                for day in dates { if let n = night(samples, wakeDay: day) { out[day]!["sleep"] = n } }
            } catch { denied.insert("sleep") }
        }
        if workouts {
            do {
                for w in try await stores.workouts(from: dates.first ?? from, to: end) {
                    let day = cal.startOfDay(for: w.start)
                    guard out[day] != nil else { continue }
                    var item: [String: Any] = ["type": w.type, "start": PhoneData.iso(w.start), "minutes": w.minutes.rounded()]
                    if let k = w.kcal { item["kcal"] = k.rounded() }
                    if let m = w.distanceM { item["distance_m"] = m.rounded() }
                    out[day]!["workouts"] = (out[day]!["workouts"] as? [[String: Any]] ?? []) + [item]
                }
            } catch { denied.insert("workouts") }
        }
        if cycle {
            do { for (day, flow) in try await stores.cycle(from: dates.first ?? from, to: end) where out[day] != nil { out[day]!["cycle"] = flow } }
            catch { denied.insert("cycle") }
        }
        return (dates.map { out[$0]! }, denied)
    }

    static func round(_ v: Double, _ key: String) -> Double {
        key == "weight_kg" ? (v * 10).rounded() / 10 : key == "distance_m" || key == "steps" || key.hasSuffix("kcal") ? v.rounded() : (v * 10).rounded() / 10
    }
}

// MARK: - One live query → one `phone_result`

struct PhoneResponder {
    var stores: PhoneStores
    var isOn: (PhoneCategory) -> Bool = PhoneData.isOn

    /// The reply frame for a `phone_query` frame, or nil when it is not one.
    func reply(to frame: Data) async -> String? {
        guard let obj = try? JSONSerialization.jsonObject(with: frame) as? [String: Any],
              obj["type"] as? String == "phone_query", let id = obj["id"] as? String else { return nil }
        var out = await answer(kind: obj["kind"] as? String ?? "", args: obj["args"] as? [String: Any] ?? [:])
        out["type"] = "phone_result"; out["id"] = id
        let data = try? JSONSerialization.data(withJSONObject: out)
        return data.flatMap { String(data: $0, encoding: .utf8) }
    }

    func answer(kind: String, args: [String: Any]) async -> [String: Any] {
        let from = (args["from"] as? String).flatMap(PhoneData.day) ?? PhoneData.calendar.startOfDay(for: Date())
        let to = (args["to"] as? String).flatMap(PhoneData.day) ?? from
        func fail(_ e: Error) -> [String: Any] { ["ok": false, "error": e is PhoneDataError ? "denied" : "failed"] }
        do {
            switch kind {
            case "health", "sleep", "workouts":
                let metrics = args["metrics"] as? [String]
                let wantsCycle = metrics?.contains("cycle") == true
                let needed: PhoneCategory = kind == "health" ? (wantsCycle ? .cycle : .health) : kind == "sleep" ? .sleep : .workouts
                guard isOn(needed) else { return ["ok": false, "error": "off"] }
                let health = kind == "health" && !wantsCycle ? PhoneSummary.quantities(for: metrics) : []
                let r = await PhoneSummary.days(from: from, to: to, stores: stores, health: health, sleep: kind == "sleep",
                                                workouts: kind == "workouts", cycle: wantsCycle)
                if !r.denied.isEmpty { return ["ok": false, "error": "denied"] }
                return ["ok": true, "data": ["days": r.days]]
            case "calendar":
                guard isOn(.calendar) else { return ["ok": false, "error": "off"] }
                let end = PhoneData.calendar.date(byAdding: .day, value: 1, to: to)!
                let ev = try await stores.events(from: from, to: end).sorted { $0.start < $1.start }.prefix(30)
                return ["ok": true, "data": ["events": ev.map { e -> [String: Any] in
                    var d: [String: Any] = ["title": e.title, "start": PhoneData.iso(e.start), "end": PhoneData.iso(e.end),
                                            "all_day": e.allDay, "calendar": e.calendar]
                    if let l = e.location, !l.isEmpty { d["location"] = l }
                    return d
                }]]
            case "reminders":
                guard isOn(.reminders) else { return ["ok": false, "error": "off"] }
                let items = try await stores.reminders(list: args["list"] as? String)
                    .sorted { ($0.due ?? .distantFuture) < ($1.due ?? .distantFuture) }.prefix(30)
                return ["ok": true, "data": ["items": items.map { r -> [String: Any] in
                    var d: [String: Any] = ["title": r.title, "list": r.list]
                    if let due = r.due { d["due"] = PhoneData.iso(due) }
                    if r.priority > 0 { d["priority"] = r.priority }
                    return d
                }]]
            case "contacts":
                guard isOn(.contacts) else { return ["ok": false, "error": "off"] }
                let name = (args["name"] as? String ?? "").trimmingCharacters(in: .whitespaces)
                guard !name.isEmpty else { return ["ok": true, "data": ["matches": []]] }
                let found = try await stores.contacts(named: name).prefix(5)
                return ["ok": true, "data": ["matches": found.map { c -> [String: Any] in
                    var d: [String: Any] = ["name": c.name, "phones": c.phones.prefix(3).map { ["label": $0.label, "number": $0.number] },
                                            "emails": Array(c.emails.prefix(2))]
                    if let o = c.organization, !o.isEmpty { d["organization"] = o }
                    if let b = c.birthday, let m = b.month, let dd = b.day {
                        d["birthday"] = String(format: "%04d-%02d-%02d", b.year ?? 1, m, dd)
                    }
                    return d
                }]]
            case "location":
                guard isOn(.location) else { return ["ok": false, "error": "off"] }
                let fix = try await stores.place(withMotion: isOn(.motion))
                var d: [String: Any] = [:]
                if let n = fix.name { d["name"] = n }
                if let p = fix.place { d["place"] = p }
                if let a = fix.activity { d["activity"] = a }
                return ["ok": true, "data": d]
            case "now_playing":
                guard isOn(.music) else { return ["ok": false, "error": "off"] }
                let m = try await stores.music()
                var d: [String: Any] = ["playing": m.playing, "recent": m.recent.prefix(5).map { r -> [String: Any] in
                    var x: [String: Any] = ["title": r.title]; if let a = r.artist { x["artist"] = a }; return x }]
                if let t = m.title { d["title"] = t }
                if let a = m.artist { d["artist"] = a }
                if let a = m.album { d["album"] = a }
                return ["ok": true, "data": d]
            default:
                return ["ok": false, "error": "failed"]
            }
        } catch { return fail(error) }
    }
}
