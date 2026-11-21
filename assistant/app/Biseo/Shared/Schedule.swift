import Foundation

/// A scheduled call from `GET /v1/schedule`: a wake-up call or a reminder the gateway places.
struct ScheduledCall: Codable, Identifiable, Equatable {
    let id: String
    var at: Date
    var repeatRule: String?         // none | daily | weekdays | weekly (unknown values shown as-is)
    var kind: String                // wake | reminder
    var note: String?
    var status: String?             // pending | missed | done | …
    var attempts: Int?

    var isWake: Bool { kind == "wake" }

    enum CodingKeys: String, CodingKey { case id, at, repeatRule = "repeat", kind, note, status, attempts }

    init(id: String, at: Date, repeatRule: String? = nil, kind: String, note: String? = nil, status: String? = nil, attempts: Int? = nil) {
        self.id = id; self.at = at; self.repeatRule = repeatRule; self.kind = kind; self.note = note; self.status = status; self.attempts = attempts
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
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id)
        try c.encode(at.timeIntervalSince1970, forKey: .at)
        try c.encodeIfPresent(repeatRule, forKey: .repeatRule)
        try c.encode(kind, forKey: .kind)
        try c.encodeIfPresent(note, forKey: .note)
        try c.encodeIfPresent(status, forKey: .status)
        try c.encodeIfPresent(attempts, forKey: .attempts)
    }

    /// shortcut: `at` is accepted as epoch seconds or ISO-8601 until PROTOCOL.md pins one form.
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
        case nil, "", "none": return nil
        case "daily": return "매일"
        case "weekdays": return "주중"
        case "weekly": return "매주"
        case let other?: return other
        }
    }
}

enum ScheduleAPI {
    static func list() async throws -> [ScheduledCall] {
        // Accept a bare list or {"items": [...]}.
        let data = try await Gateway.send("GET", "/v1/schedule")
        if let list = try? JSONDecoder().decode([ScheduledCall].self, from: data) { return list }
        struct Envelope: Decodable { let items: [ScheduledCall] }
        return try JSONDecoder().decode(Envelope.self, from: data).items
    }

    static func create(at: Date, repeatRule: String, kind: String, note: String) async throws {
        var body: [String: Any] = ["at": at.timeIntervalSince1970, "kind": kind, "repeat": repeatRule]
        let trimmed = note.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { body["note"] = trimmed }
        _ = try await Gateway.send("POST", "/v1/schedule", json: body)
    }

    static func cancel(_ id: String) async throws {
        _ = try await Gateway.send("DELETE", "/v1/schedule/" + (id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id))
    }
}

@MainActor
final class ScheduleModel: ObservableObject {
    @Published private(set) var items: [ScheduledCall] = []
    @Published private(set) var error: Error?
    @Published private(set) var loaded = false

    /// Pending calls soonest first, then missed ones.
    var upcoming: [ScheduledCall] {
        items.filter { $0.status == nil || $0.status == "pending" }.sorted { $0.at < $1.at }
    }
    var next: ScheduledCall? { upcoming.first { $0.at > Date().addingTimeInterval(-60) } ?? upcoming.first }
    var missed: [ScheduledCall] { items.filter { $0.status == "missed" }.sorted { $0.at > $1.at } }

    func refresh() async {
        guard Config.token != nil else { return }
        do { items = try await ScheduleAPI.list(); error = nil } catch { self.error = error }
        loaded = true
    }

    func create(at: Date, repeatRule: String, kind: String, note: String) async throws {
        try await ScheduleAPI.create(at: at, repeatRule: repeatRule, kind: kind, note: note)
        await refresh()
    }

    func cancel(_ item: ScheduledCall) async {
        items.removeAll { $0.id == item.id }
        do { try await ScheduleAPI.cancel(item.id) } catch { self.error = error }
        await refresh()
    }
}
