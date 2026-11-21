import Foundation

// MARK: - Contract types (PROTOCOL.md §1 and §3)

/// One JSON text message from `WS /v1/call`.
struct CallEvent: Decodable, Equatable {
    let type: String          // state | transcript | reply | interrupt | turn_done | error | activity
    var value: String?        // state
    var text: String?         // transcript, reply
    var route: String?        // reply
    var message: String?      // error
    var phase: String?        // activity: routing | tool_start | tool_end | thinking (others ignored)
    var label: String?        // activity, server wording
    var tool: String?         // activity
    var kind: String?         // activity: calendar | mail | memory | … (DESIGN §13.2)
}

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
            var window: [String]          // ["09:00", "22:00"]
        }

        var enabled: Bool
        var morning_briefing: Timed
        var deadline_check: Timed
        var server_alerts: Bool
        var autonomous: Autonomous

        static let defaultAutonomous = Autonomous(enabled: false, max_per_day: 2, min_gap_minutes: 120, window: ["09:00", "21:00"])

        init(enabled: Bool = true, morning_briefing: Timed = Timed(enabled: true, time: "08:00"),
             deadline_check: Timed = Timed(enabled: true, time: "18:00"), server_alerts: Bool = true,
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
                          deadline_check: try c.decodeIfPresent(Timed.self, forKey: .deadline_check) ?? Timed(enabled: true, time: "18:00"),
                          server_alerts: try c.decodeIfPresent(Bool.self, forKey: .server_alerts) ?? true,
                          autonomous: try c.decodeIfPresent(Autonomous.self, forKey: .autonomous) ?? Self.defaultAutonomous)
            } else {
                // Old shape: a plain briefing time and trigger switches.
                let time = try c.decodeIfPresent(String.self, forKey: .morning_briefing) ?? "08:00"
                let triggers = try c.decodeIfPresent([String: Bool].self, forKey: .triggers) ?? [:]
                self.init(morning_briefing: Timed(enabled: true, time: time),
                          deadline_check: Timed(enabled: triggers["deadline"] ?? true, time: "18:00"),
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
    /// Days the server keeps call history (0 = forever). Optional for older gateways.
    var history_days: Int?
}

struct ServerStatus: Decodable {
    struct Health: Decodable { var ok: Bool; var ms: Double? }
    struct Device: Decodable { var name: String; var ok: Bool; var ms: Double? }
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

    static func message(in body: Data) -> String? {
        guard let obj = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else { return nil }
        return ["detail", "message", "error"].lazy.compactMap { obj[$0] as? String }.first
    }
}

enum Gateway {
    static func send(_ method: String, _ path: String, json: Any? = nil) async throws -> Data {
        guard let url = URL(string: Config.gatewayURL + path) else { throw URLError(.badURL) }
        var req = URLRequest(url: url, timeoutInterval: 15)
        req.httpMethod = method
        req.setValue("Bearer \(Config.token ?? "")", forHTTPHeaderField: "Authorization")
        if let json {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try (json as? Data) ?? JSONSerialization.data(withJSONObject: json)
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if code == 400, let message = GatewayError.message(in: data) { throw GatewayError.rejected(message) }
        guard (200..<300).contains(code) else { throw GatewayError.http(code) }
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
        let json = try await getJSON("/v1/approvals")
        // The contract doesn't pin the envelope: accept a bare list or {"approvals": [...]}.
        return (json as? [[String: Any]]) ?? ((json as? [String: Any])?["approvals"] as? [[String: Any]]) ?? []
    }

    static func saveSettings(_ s: AppSettings) async throws {
        _ = try await send("PUT", "/v1/settings", json: try JSONEncoder().encode(s))
    }

    static func registerPush(token: Data) async throws {
        #if DEBUG
        let env = "sandbox"
        #else
        let env = "production"
        #endif
        _ = try await send("POST", "/v1/push/register", json: [
            "voip_token": token.map { String(format: "%02x", $0) }.joined(),
            "bundle_id": Bundle.main.bundleIdentifier ?? "xyz.scian.biseo",
            "env": env,
        ])
    }
}
