import Foundation

/// A scheduled call from `GET /v1/schedule`: a wake-up call or a reminder the gateway places.
struct ScheduledCall: Codable, Identifiable, Equatable {
    let id: String
    var at: Date
    var repeatRule: String?         // nil | daily | weekdays | weekly:<dow> (0 = Monday)
    var kind: String                // wake | reminder
    var note: String?
    var status: String?             // pending | fired | answered | missed | cancelled
    var attempts: Int?
    var createdBy: String?          // voice | app
    var personaID: String?
    // Batch 5 (cron schedules); all nil on older gateways.
    var cron: String?               // 5 fields, KST; nil = one-off
    var ruleText: String?           // "평일 오전 10시부터 오후 6시까지 2시간마다"
    var nextAt: Date?
    var notify: String?             // call | push
    var purpose: String?

    /// The next time it rings: `next_at` when the server sends it.
    var when: Date { nextAt ?? at }
    var isRule: Bool { !(cron ?? "").isEmpty }
    /// The newer gateway: it reports `notify` on every item.
    var hasRuleFields: Bool { notify != nil }

    var isWake: Bool { kind == "wake" }

    var byVoice: Bool { createdBy == "voice" }
    /// Waiting or ringing: what the home card counts as upcoming.
    var isActive: Bool { status == nil || status == "pending" || status == "fired" }

    enum CodingKeys: String, CodingKey { case id, at, repeatRule = "repeat", kind, note, status, attempts, createdBy = "created_by", personaID = "persona_id",
                                       cron, ruleText = "rule_text", nextAt = "next_at", notify, purpose }

    init(id: String, at: Date, repeatRule: String? = nil, kind: String, note: String? = nil, status: String? = nil, attempts: Int? = nil,
         createdBy: String? = nil) {
        self.id = id; self.at = at; self.repeatRule = repeatRule; self.kind = kind; self.note = note; self.status = status
        self.attempts = attempts; self.createdBy = createdBy
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let s = try? c.decode(String.self, forKey: .id) { id = s } else { id = String(try c.decode(Int.self, forKey: .id)) }
        at = try Self.decodeTime(c, .at)
        cron = try? c.decodeIfPresent(String.self, forKey: .cron)
        ruleText = try? c.decodeIfPresent(String.self, forKey: .ruleText)
        nextAt = try? Self.decodeTime(c, .nextAt)
        notify = try? c.decodeIfPresent(String.self, forKey: .notify)
        purpose = try? c.decodeIfPresent(String.self, forKey: .purpose)
        repeatRule = try c.decodeIfPresent(String.self, forKey: .repeatRule)
        kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? "reminder"
        note = try c.decodeIfPresent(String.self, forKey: .note)
        status = try c.decodeIfPresent(String.self, forKey: .status)
        attempts = try c.decodeIfPresent(Int.self, forKey: .attempts)
        createdBy = try c.decodeIfPresent(String.self, forKey: .createdBy)
        if let s = try? c.decodeIfPresent(String.self, forKey: .personaID) { personaID = s }
        else if let n = try? c.decodeIfPresent(Int.self, forKey: .personaID) { personaID = String(n) }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id)
        try c.encode(KST.iso(at), forKey: .at)
        try c.encodeIfPresent(repeatRule, forKey: .repeatRule)
        try c.encode(kind, forKey: .kind)
        try c.encodeIfPresent(note, forKey: .note)
        try c.encodeIfPresent(status, forKey: .status)
        try c.encodeIfPresent(attempts, forKey: .attempts)
        try c.encodeIfPresent(createdBy, forKey: .createdBy)
        try c.encodeIfPresent(personaID, forKey: .personaID)
        try c.encodeIfPresent(cron, forKey: .cron)
        try c.encodeIfPresent(ruleText, forKey: .ruleText)
        try c.encodeIfPresent(nextAt.map(KST.iso), forKey: .nextAt)
        try c.encodeIfPresent(notify, forKey: .notify)
        try c.encodeIfPresent(purpose, forKey: .purpose)
    }

    /// The contract is ISO-8601 with an offset; epoch seconds are accepted as a fallback.
    private static func decodeTime(_ c: KeyedDecodingContainer<CodingKeys>, _ key: CodingKeys) throws -> Date {
        if let n = try? c.decode(Double.self, forKey: key) { return Date(timeIntervalSince1970: n) }
        let s = try c.decode(String.self, forKey: key)
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]
        if let d = f.date(from: s) { return d }
        throw DecodingError.dataCorruptedError(forKey: key, in: c, debugDescription: "unreadable time \(s)")
    }
}

/// Times are shown in KST whatever the device's time zone (the assistant and the user live there).
enum KST {
    static let zone = TimeZone(identifier: "Asia/Seoul")!

    /// "2026-11-22T10:00:00+09:00", the form `/v1/schedule` expects.
    static func iso(_ d: Date) -> String {
        let f = ISO8601DateFormatter()
        f.timeZone = zone
        f.formatOptions = [.withInternetDateTime]
        return f.string(from: d)
    }

    /// 0 = Monday … 6 = Sunday, as in `weekly:<dow>`.
    static let weekdays = ["월", "화", "수", "목", "금", "토", "일"]

    /// The KST weekday of `d` in the contract's numbering.
    static func dow(_ d: Date) -> Int {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = zone
        return (cal.component(.weekday, from: d) + 5) % 7 // Calendar: 1 = Sunday
    }

    private static func formatter(_ format: String) -> DateFormatter {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "ko_KR")
        f.timeZone = zone
        f.dateFormat = format
        return f
    }

    /// "오전 7:30"
    static func time(_ d: Date) -> String { formatter("a h:mm").string(from: d) }

    /// "오늘 오전 7:30" / "내일 오전 7:30" / "11월 24일 (월) 오전 7:30"
    static func dayAndTime(_ d: Date, now: Date = Date()) -> String {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = zone
        let days = cal.dateComponents([.day], from: cal.startOfDay(for: now), to: cal.startOfDay(for: d)).day ?? 0
        let day = days == 0 ? "오늘" : days == 1 ? "내일" : days == -1 ? "어제" : formatter("M월 d일 (E)").string(from: d)
        return "\(day) \(time(d))"
    }

    static func repeatLabel(_ rule: String?) -> String? {
        switch rule {
        case nil, "": return nil
        case "daily": return "매일"
        case "weekdays": return "주중"
        case let r? where r.hasPrefix("weekly:"):
            guard let d = Int(r.dropFirst(7)), weekdays.indices.contains(d) else { return "매주" }
            return "매주 \(weekdays[d])요일"
        case let other?: return other
        }
    }

    static func statusLabel(_ status: String?) -> String {
        switch status {
        case nil, "pending": return "예정"
        case "fired": return "거는 중"
        case "answered": return "받음"
        case "missed": return "놓침"
        case "cancelled": return "취소됨"
        case let other?: return other
        }
    }
}

enum ScheduleAPI {
    /// Default: pending and fired only (home). `all`: past items too (the list's missed history).
    static func list(all: Bool = false) async throws -> [ScheduledCall] {
        // Accept a bare list or {"items": [...]}.
        let data = try await Gateway.send("GET", "/v1/schedule" + (all ? "?include=all" : ""))
        if let list = try? JSONDecoder().decode([ScheduledCall].self, from: data) { return list }
        struct Envelope: Decodable { let items: [ScheduledCall] }
        return try JSONDecoder().decode(Envelope.self, from: data).items
    }

    /// `repeatRule` nil = once. Returns the created item (201); a 400 (past, or > 30 days out)
    /// throws `GatewayError.rejected` with the server's message.
    @discardableResult
    static func create(at: Date, repeatRule: String?, kind: String, note: String, persona: String? = nil) async throws -> ScheduledCall {
        try JSONDecoder().decode(ScheduledCall.self, from: try await Gateway.send("POST", "/v1/schedule",
                                                                                   json: body(at: at, repeatRule: repeatRule, kind: kind, note: note, persona: persona)))
    }

    static func body(at: Date, repeatRule: String?, kind: String, note: String, persona: String? = nil) -> [String: Any] {
        var body: [String: Any] = ["at": KST.iso(at), "kind": kind, "repeat": repeatRule ?? NSNull()]
        if let persona { body["persona_id"] = persona }
        let trimmed = note.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { body["note"] = trimmed }
        return body
    }

    static func cancel(_ id: String) async throws {
        _ = try await Gateway.send("DELETE", path(id))
    }

    /// Skips one fire of a rule; the rule stays.
    static func skip(_ id: String, occurrence: Date) async throws {
        _ = try await Gateway.send("DELETE", path(id) + "?occurrence=" + query(KST.iso(occurrence)))
    }

    /// `schedule_update` (ACTIONS.md): exact `when` from the UI, `repeat_cron` for rules, and
    /// `scope` for repeating items. One fire of a rule goes to `?occurrence=` and the server skips it
    /// and books the one-off itself.
    static func editBody(scope: String?, when: Date?, repeatCron: String?, notify: String, purpose: String, confirmed: Bool = false) -> [String: Any] {
        var b: [String: Any] = ["notify": notify, "purpose": purpose.trimmingCharacters(in: .whitespacesAndNewlines)]
        if let scope { b["scope"] = scope }
        if let when { b["when"] = KST.iso(when) }
        if let repeatCron { b["repeat_cron"] = repeatCron }
        if confirmed { b["confirmed"] = true }
        return b
    }

    static func update(_ id: String, occurrence: Date?, body: [String: Any]) async throws -> ActionResult {
        let q = occurrence.map { "?occurrence=" + query(KST.iso($0)) } ?? ""
        return ActionResult(try await Gateway.send("PUT", path(id) + q, json: body))
    }

    /// `call_user` through `POST /v1/schedule` (the batch-5 gateway): `when` for a one-off, or
    /// `repeat_cron` + `start_date` for a rule.
    static func actionBody(at: Date, repeatCron: String?, kind: String, purpose: String, notify: String = "call",
                           persona: String?, confirmed: Bool = false) -> [String: Any] {
        var b: [String: Any] = ["kind": kind, "notify": notify, "purpose": purpose.trimmingCharacters(in: .whitespacesAndNewlines)]
        if let repeatCron {
            b["repeat_cron"] = repeatCron
            b["start_date"] = String(KST.iso(at).prefix(10))
        } else {
            b["when"] = KST.iso(at)
        }
        if let persona { b["persona_id"] = persona }
        if confirmed { b["confirmed"] = true }
        return b
    }

    static func actionBody(at: Date, repeatRule: String?, kind: String, note: String, persona: String?, confirmed: Bool = false) -> [String: Any] {
        actionBody(at: at, repeatCron: cron(repeatRule: repeatRule, at: at), kind: kind, purpose: note, persona: persona, confirmed: confirmed)
    }

    static func createAction(_ body: [String: Any]) async throws -> ActionResult {
        ActionResult(try await Gateway.send("POST", "/v1/schedule", json: body))
    }

    /// The create form's old repeat values as cron, at the picked time (weekly: 0 = Monday).
    static func cron(repeatRule: String?, at: Date) -> String? {
        var cal = Calendar(identifier: .gregorian); cal.timeZone = KST.zone
        let c = cal.dateComponents([.hour, .minute], from: at)
        let (h, m) = (c.hour ?? 0, c.minute ?? 0)
        switch repeatRule {
        case "daily": return RepeatPreset.daily.cron(hour: h, minute: m)
        case "weekdays": return RepeatPreset.weekdays.cron(hour: h, minute: m)
        case let r? where r.hasPrefix("weekly:"): return Int(r.dropFirst(7)).map { RepeatPreset.days([($0 + 1) % 7]).cron(hour: h, minute: m) }
        default: return nil
        }
    }

    static func path(_ id: String) -> String { "/v1/schedule/" + (id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id) }

    /// "+09:00" must stay a plus, not a space.
    static func query(_ s: String) -> String {
        var allowed = CharacterSet.urlQueryAllowed
        allowed.remove(charactersIn: "+&=")
        return s.addingPercentEncoding(withAllowedCharacters: allowed) ?? s
    }
}

@MainActor
final class ScheduleModel: ObservableObject {
    /// Default list: pending and fired (home card).
    @Published private(set) var active: [ScheduledCall] = []
    /// `include=all`: also past items, loaded by the list sheet.
    @Published private(set) var all: [ScheduledCall] = []
    @Published private(set) var error: Error?
    @Published private(set) var status: SectionStatus = .ok

    var upcoming: [ScheduledCall] { (all.isEmpty ? active : all).filter(\.isActive).sorted { $0.when < $1.when } }
    var next: ScheduledCall? { active.filter(\.isActive).min { $0.when < $1.when } }
    /// Editing needs the batch-5 gateway (`notify`, `cron`, `next_at` on items).
    var canEdit: Bool { (all.isEmpty ? active : all).contains(where: \.hasRuleFields) }
    var missed: [ScheduledCall] { all.filter { $0.status == "missed" }.sorted { $0.at > $1.at } }
    /// Answered and cancelled ones, newest first.
    var past: [ScheduledCall] { all.filter { $0.status == "answered" || $0.status == "cancelled" }.sorted { $0.at > $1.at } }

    func refresh() async {
        guard Config.token != nil else { return }
        switch await attempt({ try await ScheduleAPI.list() }) {
        case .success(let items): active = items; error = nil; status = .ok
        case .failure(let e): if let s = SectionStatus.from(e) { status = s; error = s.isFailed ? e : nil }
        }
    }

    func refreshAll() async {
        guard Config.token != nil else { return }
        switch await attempt({ try await ScheduleAPI.list(all: true) }) {
        case .success(let items): all = items; active = items.filter(\.isActive); error = nil; status = .ok
        case .failure(let e): if let s = SectionStatus.from(e) { status = s; error = s.isFailed ? e : nil }
        }
    }

    /// The batch-5 gateway: `call_user` with a possible `needs` (the caller shows it and resends).
    func createAction(at: Date, repeatRule: String?, kind: String, note: String, persona: String?, confirmed: Bool = false) async throws -> ActionResult {
        let r = try await ScheduleAPI.createAction(ScheduleAPI.actionBody(at: at, repeatRule: repeatRule, kind: kind, note: note, persona: persona, confirmed: confirmed))
        if r.needs == nil { await refreshAll() }
        return r
    }

    func create(at: Date, repeatRule: String?, kind: String, note: String, persona: String? = nil) async throws {
        let item = try await ScheduleAPI.create(at: at, repeatRule: repeatRule, kind: kind, note: note, persona: persona)
        active.append(item)
        if !all.isEmpty { all.append(item) }
        await refreshAll()
    }

    func cancel(_ item: ScheduledCall) async {
        do { try await ScheduleAPI.cancel(item.id) } catch { self.error = error }
        await refreshAll()
    }

    func skip(_ item: ScheduledCall) async throws {
        try await ScheduleAPI.skip(item.id, occurrence: item.when)
        await refreshAll()
    }
}

/// The editor's repeat presets and their cron (KST; cron dow 0 = Sunday).
enum RepeatPreset: Equatable {
    case daily
    case weekdays
    /// Cron weekdays, 0 = Sunday … 6 = Saturday.
    case days(Set<Int>)
    /// Every `n` hours from `from` to `to` o'clock, weekdays only or every day.
    case everyHours(n: Int, from: Int, to: Int, weekdaysOnly: Bool)
    /// Day `day` of every month.
    case monthly(day: Int)

    func cron(hour: Int, minute: Int) -> String {
        switch self {
        case .daily: return "\(minute) \(hour) * * *"
        case .weekdays: return "\(minute) \(hour) * * 1-5"
        case .days(let d): return "\(minute) \(hour) * * " + (d.isEmpty ? "*" : d.sorted().map(String.init).joined(separator: ","))
        case let .everyHours(n, from, to, weekdaysOnly): return "\(minute) \(from)-\(to)/\(n) * * " + (weekdaysOnly ? "1-5" : "*")
        case .monthly(let day): return "\(minute) \(hour) \(day) * *"
        }
    }

    /// Reads back what the editor can show; anything else (ranges of days, lists of hours…) is nil.
    static func parse(_ cron: String) -> (preset: RepeatPreset, hour: Int, minute: Int)? {
        let f = cron.split(separator: " ").map(String.init)
        guard f.count == 5, let minute = Int(f[0]), f[3] == "*" else { return nil }
        let (h, dom, dow) = (f[1], f[2], f[4])
        if let hour = Int(h) {
            if dom != "*" { return dow == "*" ? Int(dom).map { (.monthly(day: $0), hour, minute) } : nil }
            switch dow {
            case "*": return (.daily, hour, minute)
            case "1-5": return (.weekdays, hour, minute)
            default:
                let days = dow.split(separator: ",").compactMap { Int($0) }.map { $0 % 7 }
                return days.count == dow.split(separator: ",").count ? (.days(Set(days)), hour, minute) : nil
            }
        }
        // a-b/n
        let parts = h.split(separator: "/")
        guard dom == "*", parts.count == 2, let n = Int(parts[1]) else { return nil }
        let range = parts[0].split(separator: "-").compactMap { Int($0) }
        guard range.count == 2, dow == "*" || dow == "1-5" else { return nil }
        return (.everyHours(n: n, from: range[0], to: range[1], weekdaysOnly: dow == "1-5"), range[0], minute)
    }
}
