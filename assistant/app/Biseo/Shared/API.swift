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
    struct Proactive: Codable, Equatable {
        var morning_briefing: String
        var quiet_hours: [String]
        var triggers: [String: Bool]
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
    var errorDescription: String? { if case .http(let code) = self { return "서버 오류 (HTTP \(code))" }; return nil }
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
