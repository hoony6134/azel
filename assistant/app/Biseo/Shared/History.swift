import Foundation

// MARK: - Contract (PROTOCOL §3 `/v1/history`, DESIGN §15.1)

/// One call in `GET /v1/history`. Everything past id/started_at is optional: legacy items
/// (recorded before the server added these fields) lack them.
struct HistoryItem: Codable, Identifiable, Equatable {
    let id: String
    var session: String?
    var title: String?
    var last_reply: String?
    var started_at: Double
    var duration_s: Double?
    var direction: String?          // outgoing = the user called, incoming = the assistant called
    var reason: String?
    var turns: Int?
    var routes: [String: Int]?
    var kinds: [String]?
    var match: String?              // only in search results

    var start: Date { Date(timeIntervalSince1970: started_at) }
    var incoming: Bool { direction == "incoming" }
    var routeList: [String] { ["qwen", "claude"].filter { (routes?[$0] ?? 0) > 0 } }

    enum CodingKeys: String, CodingKey {
        case id, session, title, last_reply, started_at, duration_s, direction, reason, turns, routes, kinds, match
    }

    init(id: String, session: String? = nil, title: String? = nil, last_reply: String? = nil, started_at: Double, duration_s: Double? = nil,
         direction: String? = nil, reason: String? = nil, turns: Int? = nil, routes: [String: Int]? = nil, kinds: [String]? = nil, match: String? = nil) {
        self.id = id; self.session = session; self.title = title; self.last_reply = last_reply; self.started_at = started_at
        self.duration_s = duration_s; self.direction = direction; self.reason = reason; self.turns = turns; self.routes = routes
        self.kinds = kinds; self.match = match
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeFlexibleID(.id)
        session = try c.decodeIfPresent(String.self, forKey: .session)
        title = try c.decodeIfPresent(String.self, forKey: .title)
        last_reply = try c.decodeIfPresent(String.self, forKey: .last_reply)
        started_at = try c.decode(Double.self, forKey: .started_at)
        duration_s = try c.decodeIfPresent(Double.self, forKey: .duration_s)
        direction = try c.decodeIfPresent(String.self, forKey: .direction)
        reason = try c.decodeIfPresent(String.self, forKey: .reason)
        turns = try? c.decodeIfPresent(Int.self, forKey: .turns) // the detail endpoint reuses the key for a list
        routes = try c.decodeIfPresent([String: Int].self, forKey: .routes)
        kinds = try c.decodeIfPresent([String].self, forKey: .kinds)
        match = try c.decodeIfPresent(String.self, forKey: .match)
    }
}

/// `GET /v1/history/{id}`: the item plus its turns.
struct HistoryCall: Codable, Equatable {
    struct Step: Codable, Equatable { var label: String; var kind: String?; var at: Double? }

    struct Turn: Codable, Equatable {
        var at: Double?
        var user: String?
        var assistant: String?
        var route: String?
        var activity: [Step]?
        var interrupted: Bool?

        enum CodingKeys: String, CodingKey { case at, user, assistant, route, activity, interrupted }

        init(at: Double? = nil, user: String? = nil, assistant: String? = nil, route: String? = nil, activity: [Step]? = nil, interrupted: Bool? = nil) {
            self.at = at; self.user = user; self.assistant = assistant; self.route = route; self.activity = activity; self.interrupted = interrupted
        }

        /// `assistant` arrives as a list of sentences (or, from older servers, one string).
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            at = try c.decodeIfPresent(Double.self, forKey: .at)
            user = try c.decodeIfPresent(String.self, forKey: .user)
            if let list = try? c.decodeIfPresent([String].self, forKey: .assistant) { assistant = list.joined(separator: " ") }
            else { assistant = try c.decodeIfPresent(String.self, forKey: .assistant) }
            route = try c.decodeIfPresent(String.self, forKey: .route)
            activity = try c.decodeIfPresent([Step].self, forKey: .activity)
            interrupted = try c.decodeIfPresent(Bool.self, forKey: .interrupted)
        }
    }

    var item: HistoryItem
    var turns: [Turn]

    init(item: HistoryItem, turns: [Turn]) { self.item = item; self.turns = turns }

    init(from decoder: Decoder) throws {
        item = try HistoryItem(from: decoder)
        turns = try decoder.container(keyedBy: HistoryItem.CodingKeys.self).decodeIfPresent([Turn].self, forKey: .turns) ?? []
    }

    func encode(to encoder: Encoder) throws {
        try item.encode(to: encoder)
        var c = encoder.container(keyedBy: HistoryItem.CodingKeys.self)
        try c.encode(turns, forKey: .turns)
    }

    /// Plain text for ShareLink.
    var plainText: String {
        let who = AssistantName.current
        return turns.map { t in
            [t.user.map { "나: \($0)" }, t.assistant.map { "\(who): \($0)" + (t.interrupted == true ? " …" : "") }]
                .compactMap { $0 }.joined(separator: "\n")
        }.joined(separator: "\n\n")
    }
}

private extension KeyedDecodingContainer {
    /// Ids may come as strings or numbers.
    func decodeFlexibleID(_ key: Key) throws -> String {
        if let s = try? decode(String.self, forKey: key) { return s }
        return String(try decode(Int.self, forKey: key))
    }
}

// MARK: - API

enum HistoryAPI {
    static let pageSize = 20

    static func page(before: Double? = nil, query: String? = nil, limit: Int = pageSize) async throws -> [HistoryItem] {
        var c = URLComponents()
        c.queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        if let before { c.queryItems?.append(URLQueryItem(name: "before", value: String(before))) }
        if let query, !query.isEmpty { c.queryItems?.append(URLQueryItem(name: "q", value: query)) }
        return try await Gateway.get("/v1/history" + (c.url?.absoluteString ?? ""))
    }

    static func call(_ id: String) async throws -> HistoryCall {
        try await Gateway.get("/v1/history/" + (id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id))
    }

    static func delete(_ id: String) async throws {
        _ = try await Gateway.send("DELETE", "/v1/history/" + (id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id))
    }
}

// MARK: - Paging and offline cache

/// Merges pages newest-first without duplicates; `before` for the next page is the oldest start.
struct HistoryPager: Equatable {
    private(set) var items: [HistoryItem] = []
    private(set) var exhausted = false

    var nextBefore: Double? { items.last?.started_at }

    mutating func reset(with first: [HistoryItem], pageSize: Int = HistoryAPI.pageSize) {
        items = []
        exhausted = false
        append(first, pageSize: pageSize)
    }

    mutating func append(_ page: [HistoryItem], pageSize: Int = HistoryAPI.pageSize) {
        let known = Set(items.map(\.id))
        items += page.filter { !known.contains($0.id) }
        items.sort { $0.started_at > $1.started_at }
        if page.count < pageSize { exhausted = true }
    }

    mutating func remove(_ id: String) { items.removeAll { $0.id == id } }
    /// Puts back a row whose delete failed or was undone.
    mutating func insert(_ item: HistoryItem) { append([item], pageSize: 0) }
}

/// The last loaded list and every opened transcript, as JSON in Caches (a mirror: the server wins).
struct HistoryCache {
    let directory: URL

    static let standard = HistoryCache(directory: {
        let dir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("history")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }())

    struct ListSnapshot: Codable { var items: [HistoryItem]; var fetchedAt: Date }

    func saveList(_ items: [HistoryItem], at date: Date = Date()) {
        try? JSONEncoder().encode(ListSnapshot(items: items, fetchedAt: date)).write(to: directory.appendingPathComponent("list.json"), options: .atomic)
    }

    func loadList() -> ListSnapshot? {
        guard let data = try? Data(contentsOf: directory.appendingPathComponent("list.json")) else { return nil }
        return try? JSONDecoder().decode(ListSnapshot.self, from: data)
    }

    func saveCall(_ call: HistoryCall) {
        try? JSONEncoder().encode(call).write(to: file(call.item.id), options: .atomic)
    }

    func loadCall(_ id: String) -> HistoryCall? {
        guard let data = try? Data(contentsOf: file(id)) else { return nil }
        return try? JSONDecoder().decode(HistoryCall.self, from: data)
    }

    func remove(_ id: String) {
        try? FileManager.default.removeItem(at: file(id))
        if var snap = loadList() { snap.items.removeAll { $0.id == id }; saveList(snap.items, at: snap.fetchedAt) }
    }

    private func file(_ id: String) -> URL {
        directory.appendingPathComponent("call-" + id.map { $0.isLetter || $0.isNumber || $0 == "-" ? String($0) : "_" }.joined() + ".json")
    }

    /// Offline search: titles only.
    static func filter(_ items: [HistoryItem], query: String) -> [HistoryItem] {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return items }
        return items.filter { ($0.title ?? "").localizedCaseInsensitiveContains(q) }
    }
}

// MARK: - Local recordings ↔ calls

extension Recordings {
    /// Recordings of this call on this device: by session id, or for legacy items without one,
    /// by a recording that started within ±60 s of the call.
    static func matching(_ item: HistoryItem, in dir: URL = directory) -> [URL] {
        let files = list(in: dir)
        if let session = item.session {
            let prefix = String(fileName(session: session, start: Date()).split(separator: "_").dropLast().joined(separator: "_")) + "_"
            return files.filter { $0.lastPathComponent.hasPrefix(prefix) }
        }
        return files.filter { url in
            guard let start = startDate(of: url) else { return false }
            return abs(start.timeIntervalSince1970 - item.started_at) <= 60
        }
    }

    /// The timestamp in `<session>_<yyyyMMdd-HHmmss>.m4a`.
    static func startDate(of url: URL) -> Date? {
        guard let stamp = url.deletingPathExtension().lastPathComponent.split(separator: "_").last else { return nil }
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyyMMdd-HHmmss"
        return f.date(from: String(stamp))
    }
}
