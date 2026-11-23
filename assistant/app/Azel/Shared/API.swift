import Foundation

// MARK: - Contract types (PROTOCOL.md §1 and §3)

struct AppSettings: Codable, Equatable {
    struct TaskDefault: Codable, Equatable { var model: String; var path: String? }
    /// When the assistant calls first. Decodes the old shape
    /// (`morning_briefing: "08:00"`, `quiet_hours`, `triggers`) and always writes the new one;
    /// quiet hours are gone.
    struct Proactive: Codable, Equatable {
        struct Timed: Codable, Equatable { var enabled: Bool; var time: String }
        struct Autonomous: Codable, Equatable {
            var enabled: Bool
            var max_per_day: Int
            var min_gap_minutes: Int
            var window: [String]          // ["08:00", "23:00"]
        }

        var enabled: Bool
        var morning_briefing: Timed
        var deadline_check: Timed
        var server_alerts: Bool
        var autonomous: Autonomous

        /// Fallbacks used only before the server's settings load (the server's values always win).
        static let defaultAutonomous = Autonomous(enabled: false, max_per_day: 3, min_gap_minutes: 120, window: ["08:00", "23:00"])
        static let defaultDeadline = Timed(enabled: true, time: "20:00")

        init(enabled: Bool = true, morning_briefing: Timed = Timed(enabled: true, time: "08:00"),
             deadline_check: Timed = defaultDeadline, server_alerts: Bool = true,
             autonomous: Autonomous = defaultAutonomous) {
            self.enabled = enabled; self.morning_briefing = morning_briefing; self.deadline_check = deadline_check
            self.server_alerts = server_alerts; self.autonomous = autonomous
        }

        private enum Keys: String, CodingKey { case enabled, morning_briefing, deadline_check, server_alerts, autonomous, triggers }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: Keys.self)
            if let timed = try? c.decode(Timed.self, forKey: .morning_briefing) {
                self.init(enabled: try c.decodeIfPresent(Bool.self, forKey: .enabled) ?? true,
                          morning_briefing: timed,
                          deadline_check: try c.decodeIfPresent(Timed.self, forKey: .deadline_check) ?? Self.defaultDeadline,
                          server_alerts: try c.decodeIfPresent(Bool.self, forKey: .server_alerts) ?? true,
                          autonomous: try c.decodeIfPresent(Autonomous.self, forKey: .autonomous) ?? Self.defaultAutonomous)
            } else {
                // Old shape: a plain briefing time and trigger switches.
                let time = try c.decodeIfPresent(String.self, forKey: .morning_briefing) ?? "08:00"
                let triggers = try c.decodeIfPresent([String: Bool].self, forKey: .triggers) ?? [:]
                self.init(morning_briefing: Timed(enabled: true, time: time),
                          deadline_check: Timed(enabled: triggers["deadline"] ?? true, time: Self.defaultDeadline.time),
                          server_alerts: triggers["server_down"] ?? true)
            }
        }

        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: Keys.self)
            try c.encode(enabled, forKey: .enabled)
            try c.encode(morning_briefing, forKey: .morning_briefing)
            try c.encode(deadline_check, forKey: .deadline_check)
            try c.encode(server_alerts, forKey: .server_alerts)
            try c.encode(autonomous, forKey: .autonomous)
        }
    }
    var default_model: String
    var claude_path: String
    var task_defaults: [String: TaskDefault]
    var monthly_budget_usd: Double
    var budget_warn_pct: Int
    var proactive: Proactive
    var default_device: String
    var device_fallback: [String]
    /// DESIGN §0: what the assistant is called (default 아젤). Optional so older gateways still decode.
    var assistant_name: String?
    /// How the assistant talks (≤ 600 chars). "" = the server's default. Optional for older gateways.
    var personality: String?
    /// normal | noisy: in noisy places the server is stricter about barge-ins. Optional for older gateways.
    var noise_mode: String?
    /// brief | normal | detailed (default normal). Optional for older gateways.
    var reply_length: String?
    /// Days the server keeps call history (0 = forever). Optional for older gateways.
    var history_days: Int?
    /// How the assistant addresses the user ("" = no 호칭). Optional for older gateways.
    var user_title: String?
    /// Whether the gateway knows `user_title` (its GET included the key). Today's gateway doesn't, and
    /// rejects unknown keys on PUT, so the 호칭 rows stay hidden and the field is never sent until it does.
    @MainActor static var supportsUserTitle = false
    /// The gateway takes `when` on tasks (its settings include `overnight`).
    @MainActor static var supportsOvernight = false

    static func reportsOvernight(_ data: Data) -> Bool {
        ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["overnight"] != nil
    }

    /// The GET /v1/settings JSON has a `user_title` key (any value, "" or null included).
    static func reportsUserTitle(_ data: Data) -> Bool {
        ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?.keys.contains("user_title") ?? false
    }
    /// claude_api | claude_code | nim | qwen: the default engine for tasks. nil on older gateways,
    /// which then never get it back (synthesized encoding omits nil).
    var task_backend: String?
    /// auto | local | claude_api | claude_code: the default brain for calls. nil on older gateways.
    var brain_mode: String?
    /// The overnight work queue (nil on older gateways: the 밤새 choices stay hidden).
    var overnight: OvernightSettings?
    /// Extra model providers; `providers.nim` is NVIDIA NIM. Optional for older gateways.
    var providers: Providers?

    struct Providers: Codable, Equatable {
        var nim: NIM?

        struct NIM: Codable, Equatable {
            var enabled: Bool
            var base_url: String?
            var model: String?
            /// Read-only: the key itself is write-only (`PUT /v1/providers/nim/key`).
            var has_key: Bool?

            private enum Keys: String, CodingKey { case enabled, base_url, model, has_key }
            init(enabled: Bool = false, base_url: String? = nil, model: String? = nil, has_key: Bool? = nil) {
                self.enabled = enabled; self.base_url = base_url; self.model = model; self.has_key = has_key
            }
            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: Keys.self)
                enabled = try c.decodeIfPresent(Bool.self, forKey: .enabled) ?? false
                base_url = try c.decodeIfPresent(String.self, forKey: .base_url)
                model = try c.decodeIfPresent(String.self, forKey: .model)
                has_key = try c.decodeIfPresent(Bool.self, forKey: .has_key)
            }
            /// PUT leaves out has_key.
            func encode(to encoder: Encoder) throws {
                var c = encoder.container(keyedBy: Keys.self)
                try c.encode(enabled, forKey: .enabled)
                try c.encodeIfPresent(base_url, forKey: .base_url)
                try c.encodeIfPresent(model, forKey: .model)
            }
        }
    }
}

struct ServerStatus: Decodable {
    struct Health: Decodable {
        var ok: Bool; var ms: Double?
        /// The gateway's own entry names the machine that answered (deneb, the Mac …).
        var device: String?
    }
    struct Device: Decodable {
        var name: String; var ok: Bool; var ms: Double?
        /// When the gateway decides itself: ok | slow | down, and the probe time.
        var status: String?
        var latency_ms: Double?

        /// From the actual answer, never from last_seen (user, 2026-11-23): green within 2 s, yellow when it
        /// answers but slower, red when nothing answers. The gateway's `status` when it sends one.
        var health: DeviceHealth {
            switch status {
            case "ok": return .ok
            case "slow": return .slow
            case "down": return .down
            default: return !ok ? .down : (latency ?? 0) > Self.slowMs ? .slow : .ok
            }
        }
        static let slowMs = 2000.0
        var latency: Double? { latency_ms ?? ms }
    }
    var spark: Health
    var gateway: Health
    var devices: [Device]
}

// MARK: - Gateway client

enum GatewayError: LocalizedError {
    case http(Int)
    /// 400 with a server explanation (FastAPI `detail`, or `message`/`error`), meant for the user.
    case rejected(String)

    var errorDescription: String? {
        switch self {
        case .http(let code): return "서버 오류 (HTTP \(code))"
        case .rejected(let message): return message
        }
    }

    /// FastAPI `detail` (a string, or for 422 a list of {loc, msg}), else `message`/`error`.
    static func message(in body: Data) -> String? {
        guard let obj = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else { return nil }
        if let list = obj["detail"] as? [[String: Any]] {
            let parts = list.compactMap { item -> String? in
                guard let msg = item["msg"] as? String else { return nil }
                let field = (item["loc"] as? [Any])?.last.map { "\($0)" }
                return field.map { "\($0): \(msg)" } ?? msg
            }
            if !parts.isEmpty { return parts.joined(separator: ", ") }
        }
        if let e = obj["error"] as? [String: Any], let m = e["message"] as? String { return m }   // action result
        return ["detail", "message", "error"].lazy.compactMap { obj[$0] as? String }.first
    }

    /// 400 and 422 carry an explanation for the user; other codes stay codes (404 = missing
    /// endpoint, 409 = busy, 401/403 = token, 5xx = unreachable).
    static func from(status code: Int, body: Data, explain: Set<Int> = []) -> GatewayError {
        if code == 400 || code == 422, let m = message(in: body) { return .rejected(m) }
        // FastAPI's own "Not Found" means a missing route: that stays a code (callers test .http(404)).
        if explain.contains(code), let m = message(in: body), m != "Not Found" { return .rejected(m) }
        // Action errors (ACTIONS.md §2: 404/409/429/503 with {detail, …result}) carry a Korean message too.
        if (400..<600).contains(code), ((try? JSONSerialization.jsonObject(with: body)) as? [String: Any])?["error"] is [String: Any],
           let m = message(in: body) { return .rejected(m) }
        return .http(code)
    }
}

enum Gateway {
    /// `explain`: statuses whose server `detail` is shown to the user (e.g. approvals: 404 unknown, 409 already decided).
    static func send(_ method: String, _ path: String, json: Any? = nil, timeout: TimeInterval = 15, explain: Set<Int> = []) async throws -> Data {
        guard let url = URL(string: Config.gatewayURL + path) else { throw URLError(.badURL) }
        var req = URLRequest(url: url, timeoutInterval: timeout)
        req.httpMethod = method
        req.setValue("Bearer \(Config.token ?? "")", forHTTPHeaderField: "Authorization")
        if let json {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try (json as? Data) ?? JSONSerialization.data(withJSONObject: json)
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else { throw GatewayError.from(status: code, body: data, explain: explain) }
        return data
    }

    static func get<T: Decodable>(_ path: String, as: T.Type = T.self) async throws -> T {
        try JSONDecoder().decode(T.self, from: try await send("GET", path))
    }

    /// For shapes the contract leaves open (`by_model`, approval items).
    static func getJSON(_ path: String) async throws -> Any {
        try JSONSerialization.jsonObject(with: try await send("GET", path))
    }

    static func approvals() async throws -> [[String: Any]] {
        try parseApprovals(try await send("GET", "/v1/approvals"))
    }

    static func parseApprovals(_ data: Data) throws -> [[String: Any]] {
        let json = try JSONSerialization.jsonObject(with: data)
        // The contract doesn't pin the envelope: accept a bare list or {"approvals": [...]}.
        return (json as? [[String: Any]]) ?? ((json as? [String: Any])?["approvals"] as? [[String: Any]]) ?? []
    }

    static func saveSettings(_ s: AppSettings) async throws {
        var s = s
        if await !AppSettings.supportsUserTitle { s.user_title = nil }   // an old gateway 400s on unknown keys
        _ = try await send("PUT", "/v1/settings", json: try JSONEncoder().encode(s))
    }

    /// `POST /v1/push/register`: the VoIP token (required by the gateway) plus, when there is one,
    /// the alert token for ordinary notifications (task finished, missed scheduled calls).
    static func registerPush(token: Data, alertToken: Data? = nil) async throws {
        #if DEBUG
        let env = "sandbox"
        #else
        let env = "production"
        #endif
        _ = try await send("POST", "/v1/push/register",
                           json: registerBody(voip: token, alert: alertToken, bundleID: Bundle.main.bundleIdentifier ?? "xyz.scian.biseo", env: env))
    }

    static func hex(_ token: Data) -> String { token.map { String(format: "%02x", $0) }.joined() }

    /// A task's Live Activity push token: the gateway updates that card (an approval's buttons) while the app sleeps.
    static func registerActivity(token: Data, taskID: String) async throws {
        #if DEBUG
        let env = "sandbox"
        #else
        let env = "production"
        #endif
        _ = try await send("POST", "/v1/push/register",
                           json: ["bundle_id": Bundle.main.bundleIdentifier ?? "xyz.scian.biseo", "activity_token": hex(token),
                                  "task_id": taskID, "env": env] as [String: Any])
    }

    static func registerBody(voip: Data, alert: Data?, bundleID: String, env: String) -> [String: Any] {
        var body: [String: Any] = ["voip_token": hex(voip), "bundle_id": bundleID, "env": env]
        if let alert { body["alert_token"] = hex(alert) }
        return body
    }
}

enum NIMAPI {
    /// Model ids: a list of strings or of objects with `id`/`name`.
    static func models() async throws -> [String] {
        let json = try JSONSerialization.jsonObject(with: try await Gateway.send("GET", "/v1/providers/nim/models"))
        let list = (json as? [Any]) ?? ((json as? [String: Any])?["models"] as? [Any]) ?? ((json as? [String: Any])?["data"] as? [Any]) ?? []
        return list.compactMap { ($0 as? String) ?? ($0 as? [String: Any]).flatMap { ($0["id"] ?? $0["name"]) as? String } }
    }

    /// Write-only: the key goes to the gateway once and is never kept on this device.
    static func putKey(_ value: String) async throws {
        _ = try await Gateway.send("PUT", "/v1/providers/nim/key", json: ["value": value])
    }
}

/// The unified action result (ACTIONS.md §2): `{ok, spoken, data, needs, error}`. Older routes
/// answer with their own JSON; that counts as ok with nothing to ask.
struct ActionResult: Equatable {
    struct Option: Equatable { var id: String; var spoken: String }
    struct Needs: Equatable {
        var kind: String          // confirm | clarify | choose
        var question: String
        var options: [Option] = []
    }

    var ok = true
    var spoken: String?
    var needs: Needs?

    init(ok: Bool = true, spoken: String? = nil, needs: Needs? = nil) { self.ok = ok; self.spoken = spoken; self.needs = needs }

    init(_ data: Data) {
        guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
        ok = (o["ok"] as? Bool) ?? true
        spoken = (o["spoken"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        if let n = o["needs"] as? [String: Any], let kind = n["kind"] as? String {
            let options = (n["options"] as? [[String: Any]] ?? []).compactMap { d -> Option? in
                guard let id = d["id"].map(describe), let s = d["spoken"] as? String else { return nil }
                return Option(id: id, spoken: s)
            }
            needs = Needs(kind: kind, question: (n["question"] as? String) ?? spoken ?? "", options: options)
        }
    }
}
