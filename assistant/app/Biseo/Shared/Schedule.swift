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

    var isWake: Bool { kind == "wake" }

    var byVoice: Bool { createdBy == "voice" }
    /// Waiting or ringing: what the home card counts as upcoming.
    var isActive: Bool { status == nil || status == "pending" || status == "fired" }

    enum CodingKeys: String, CodingKey { case id, at, repeatRule = "repeat", kind, note, status, attempts, createdBy = "created_by" }

    init(id: String, at: Date, repeatRule: String? = nil, kind: String, note: String? = nil, status: String? = nil, attempts: Int? = nil,
         createdBy: String? = nil) {
        self.id = id; self.at = at; self.repeatRule = repeatRule; self.kind = kind; self.note = note; self.status = status
        self.attempts = attempts; self.createdBy = createdBy
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let s = try? c.decode(String.self, forKey: .id) { id = s } else { id = String(try c.decode(Int.self, forKey: .id)) }
        at = try Self.decodeTime(c)
        repeatRule = try c.decodeIfPresent(String.self, forKey: .repeatRule)
        kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? "reminder"
        note = try c.decodeIfPresent(String.self, forKey: .note)
        status = try c.decodeIfPresent(String.self, forKey: .status)
        attempts = try c.decodeIfPresent(Int.self, forKey: .attempts)
        createdBy = try c.decodeIfPresent(String.self, forKey: .createdBy)
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
    }

    /// The contract is ISO-8601 with an offset; epoch seconds are accepted as a fallback.
    private static func decodeTime(_ c: KeyedDecodingContainer<CodingKeys>) throws -> Date {
        if let n = try? c.decode(Double.self, forKey: .at) { return Date(timeIntervalSince1970: n) }
        let s = try c.decode(String.self, forKey: .at)
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]
        if let d = f.date(from: s) { return d }
        throw DecodingError.dataCorruptedError(forKey: .at, in: c, debugDescription: "unreadable time \(s)")
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
    static func create(at: Date, repeatRule: String?, kind: String, note: String) async throws -> ScheduledCall {
        try JSONDecoder().decode(ScheduledCall.self, from: try await Gateway.send("POST", "/v1/schedule",
                                                                                   json: body(at: at, repeatRule: repeatRule, kind: kind, note: note)))
    }

    static func body(at: Date, repeatRule: String?, kind: String, note: String) -> [String: Any] {
        var body: [String: Any] = ["at": KST.iso(at), "kind": kind, "repeat": repeatRule ?? NSNull()]
        let trimmed = note.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { body["note"] = trimmed }
        return body
    }

    static func cancel(_ id: String) async throws {
        _ = try await Gateway.send("DELETE", "/v1/schedule/" + (id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id))
    }
}

@MainActor
final class ScheduleModel: ObservableObject {
    /// Default list: pending and fired (home card).
    @Published private(set) var active: [ScheduledCall] = []
    /// `include=all`: also past items, loaded by the list sheet.
    @Published private(set) var all: [ScheduledCall] = []
    @Published private(set) var error: Error?

    var upcoming: [ScheduledCall] { (all.isEmpty ? active : all).filter(\.isActive).sorted { $0.at < $1.at } }
    var next: ScheduledCall? { active.filter(\.isActive).min { $0.at < $1.at } }
    var missed: [ScheduledCall] { all.filter { $0.status == "missed" }.sorted { $0.at > $1.at } }
    /// Answered and cancelled ones, newest first.
    var past: [ScheduledCall] { all.filter { $0.status == "answered" || $0.status == "cancelled" }.sorted { $0.at > $1.at } }

    func refresh() async {
        guard Config.token != nil else { return }
        do { active = try await ScheduleAPI.list(); error = nil } catch { self.error = error }
    }

    func refreshAll() async {
        guard Config.token != nil else { return }
        do { all = try await ScheduleAPI.list(all: true); active = all.filter(\.isActive); error = nil } catch { self.error = error }
    }

    func create(at: Date, repeatRule: String?, kind: String, note: String) async throws {
        let item = try await ScheduleAPI.create(at: at, repeatRule: repeatRule, kind: kind, note: note)
        active.append(item)
        if !all.isEmpty { all.append(item) }
        await refreshAll()
    }

    func cancel(_ item: ScheduledCall) async {
        do { try await ScheduleAPI.cancel(item.id) } catch { self.error = error }
        await refreshAll()
    }
}
