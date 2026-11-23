import SwiftUI

/// A background task the assistant works on by itself (`/v1/tasks`).
struct AgentTask: Decodable, Identifiable, Equatable, Hashable {
    struct Step: Decodable, Equatable, Hashable {
        var ts: Date?
        var label: String
        var device: String?
        var tool: String?

        private enum Keys: String, CodingKey { case ts, label, device, tool }
        init(ts: Date? = nil, label: String, device: String? = nil, tool: String? = nil) {
            self.ts = ts; self.label = label; self.device = device; self.tool = tool
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: Keys.self)
            ts = TaskTime.decode(c, .ts)
            label = try c.decodeIfPresent(String.self, forKey: .label) ?? ""
            device = try c.decodeIfPresent(String.self, forKey: .device)
            tool = try c.decodeIfPresent(String.self, forKey: .tool)
        }
    }

    var id: String
    var goal: String
    var status: String              // queued | running | waiting_approval | done | failed | cancelled
    var steps: [Step]
    var result: String?
    var cost: Double?
    var device: String?
    var persona: String?
    var created_at: Date?
    /// The engine that actually ran it, and why it fell back if it did. Absent on older gateways.
    var backend: String?
    var fallback_reason: String?
    /// Why it's waiting ("다른 작업 2개가 끝나길 기다리는 중"), and the finish notification.
    var note: String?
    var notify: String?             // call | push | none
    var error: String?

    var isActive: Bool { ["queued", "running", "waiting_approval"].contains(status) }
    /// Waiting for its time (overnight, or an exact time).
    var isScheduled: Bool { status == "scheduled" }
    var scheduledFor: Date?
    var started: Date?
    var finished: Date?

    /// Finished with a result in the last 36 h: shown on the home card (overnight results were only under "모두 보기").
    func isRecentResult(now: Date = Date()) -> Bool {
        status == "done" && !(result ?? "").isEmpty && now.timeIntervalSince(finished ?? created_at ?? .distantPast) < 36 * 3600
    }

    /// File paths mentioned in the result (absolute or ~/…), to copy or reveal.
    var paths: [String] { Self.paths(in: result ?? "") }

    static func paths(in text: String) -> [String] {
        let pattern = #"(?<![\w/])(?:~/|/(?:Users|home|tmp|private|Volumes|mnt|data|opt|var)/)[^\s`'"()<>\[\]]+"#
        guard let re = try? NSRegularExpression(pattern: pattern) else { return [] }
        let ns = text as NSString
        var out: [String] = []
        for m in re.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            let p = ns.substring(with: m.range).trimmingCharacters(in: CharacterSet(charactersIn: ".,;:!?"))
            if !out.contains(p) { out.append(p) }
        }
        return out
    }

    private enum Keys: String, CodingKey { case id, goal, status, steps, result, cost, cost_usd, device, persona, persona_id, created_at, created, backend, fallback_reason, note, notify, error, scheduled_for, started, finished }

    init(id: String, goal: String, status: String, steps: [Step] = [], result: String? = nil, cost: Double? = nil, device: String? = nil) {
        self.id = id; self.goal = goal; self.status = status; self.steps = steps; self.result = result; self.cost = cost; self.device = device
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        if let s = try? c.decode(String.self, forKey: .id) { id = s } else { id = String(try c.decode(Int.self, forKey: .id)) }
        // Lenient: one odd field never drops a task (or the whole list).
        goal = (try? c.decodeIfPresent(String.self, forKey: .goal)) ?? ""
        status = (try? c.decodeIfPresent(String.self, forKey: .status)) ?? "queued"
        steps = (try? c.decodeIfPresent([Step].self, forKey: .steps)) ?? []
        result = try? c.decodeIfPresent(String.self, forKey: .result)
        // cost: a USD number, or an object with cost_usd
        if let n = try? c.decodeIfPresent(Double.self, forKey: .cost) { cost = n }
        else if let o = try? c.decodeIfPresent([String: Double].self, forKey: .cost) { cost = o["cost_usd"] ?? o["usd"] }
        else if let n = try? c.decodeIfPresent(Double.self, forKey: .cost_usd) { cost = n }   // the gateway's task_view
        device = (try? c.decodeIfPresent(String.self, forKey: .device)) ?? nil
        persona = (try? c.decodeIfPresent(String.self, forKey: .persona)) ?? (try? c.decodeIfPresent(String.self, forKey: .persona_id))
        created_at = TaskTime.decode(c, .created_at) ?? TaskTime.decode(c, .created)
        backend = try? c.decodeIfPresent(String.self, forKey: .backend)
        fallback_reason = try? c.decodeIfPresent(String.self, forKey: .fallback_reason)
        note = (try? c.decodeIfPresent(String.self, forKey: .note)).flatMap { $0.isEmpty ? nil : $0 }
        notify = try? c.decodeIfPresent(String.self, forKey: .notify)
        error = (try? c.decodeIfPresent(String.self, forKey: .error)).flatMap { $0.isEmpty ? nil : $0 }
        scheduledFor = TaskTime.decode(c, .scheduled_for)
        started = TaskTime.decode(c, .started)
        finished = TaskTime.decode(c, .finished)
    }

    static func == (a: AgentTask, b: AgentTask) -> Bool { a.id == b.id && a.status == b.status && a.steps == b.steps && a.result == b.result }
    func hash(into h: inout Hasher) { h.combine(id) }
}

enum TaskTime {
    /// ISO-8601 (with or without fractions) or epoch seconds.
    static func decode<K: CodingKey>(_ c: KeyedDecodingContainer<K>, _ key: K) -> Date? {
        if let n = try? c.decodeIfPresent(Double.self, forKey: key) { return Date(timeIntervalSince1970: n) }
        guard let s = try? c.decodeIfPresent(String.self, forKey: key) else { return nil }
        let f = ISO8601DateFormatter()
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f.date(from: s)
    }
}

/// Status badge text and colour; the word is always shown (never colour alone).
enum TaskBadge {
    static func label(_ status: String) -> String {
        switch status {
        case "scheduled": return "예약됨"
        case "queued": return "대기"
        case "running": return "진행 중"
        case "waiting_approval": return "승인 필요"
        case "done": return "완료"
        case "failed": return "실패"
        case "cancelled": return "취소됨"
        default: return status
        }
    }

    static func color(_ status: String) -> Color {
        switch status {
        case "running", "queued": return Theme.indigo
        case "scheduled": return Theme.text2
        case "waiting_approval": return Theme.amber
        case "done": return Theme.celadon
        case "failed": return Theme.danger
        default: return Theme.text3
        }
    }
}

/// The default task engine (`task_backend`).
enum TaskBackend: String, CaseIterable, Identifiable {
    case claude_api, claude_code, nim, qwen
    var id: String { rawValue }

    var label: String {
        switch self {
        case .claude_api: return "Claude API"
        case .claude_code: return "Claude Code"
        case .nim: return "NIM"
        case .qwen: return "로컬 Qwen"
        }
    }

    var detail: String {
        switch self {
        case .claude_api: return "빠름, 예산에서 차감"
        case .claude_code: return "구독 사용"
        case .nim: return "실험적"
        case .qwen: return "무료, 조회 위주"
        }
    }

    /// NIM only when it's switched on (or already the chosen one, so the picker still shows it).
    static func options(nimEnabled: Bool, current: String?) -> [TaskBackend] {
        allCases.filter { $0 != .nim || nimEnabled || current == TaskBackend.nim.rawValue }
    }

    /// A task's `backend` for display; unknown values as-is.
    static func label(_ raw: String) -> String { TaskBackend(rawValue: raw)?.label ?? raw }
}

/// When a task runs: now, in the overnight window, or at a set time (≤ 7 days, app only).
enum TaskWhen: Hashable {
    case now, overnight, at(Date)
}

/// `overnight` in /v1/settings: the window and how the morning report arrives.
struct OvernightSettings: Codable, Equatable {
    var window: [String]          // ["01:00", "06:00"]
    var report_push: Bool
    var report_call: Bool
}

enum TaskNotify: String, CaseIterable, Identifiable {
    case call, push, none
    var id: String { rawValue }
    var label: String { switch self { case .call: return "전화"; case .push: return "알림"; case .none: return "없음" } }
}

enum TaskAPI {
    static func list() async throws -> [AgentTask] {
        let data = try await Gateway.send("GET", "/v1/tasks")
        if let l = try? JSONDecoder().decode([Lenient<AgentTask>].self, from: data) { return l.compactMap(\.value) }
        struct Envelope: Decodable { let tasks: [Lenient<AgentTask>]?; let items: [Lenient<AgentTask>]? }
        let e = try JSONDecoder().decode(Envelope.self, from: data)
        return (e.tasks ?? e.items ?? []).compactMap(\.value)
    }

    static func get(_ id: String) async throws -> AgentTask {
        try JSONDecoder().decode(AgentTask.self, from: try await Gateway.send("GET", "/v1/tasks/" + escape(id)))
    }

    static func body(goal: String, device: String?, persona: String?, notify: TaskNotify, when: TaskWhen = .now, autoApprove: Bool = false) -> [String: Any] {
        var b: [String: Any] = ["goal": goal.trimmingCharacters(in: .whitespacesAndNewlines), "notify": notify.rawValue]
        switch when {
        case .now: break
        case .overnight: b["when"] = "overnight"
        case .at(let d): b["when"] = KST.iso(d)
        }
        // The task's own auto-approve grant (brain aeb70e6: the batch 8 grant, ends with the task).
        if autoApprove { b["auto_approve"] = true }
        if let device { b["device"] = device }
        if let persona { b["persona"] = persona }
        return b
    }

    static func create(goal: String, device: String?, persona: String?, notify: TaskNotify, when: TaskWhen = .now, autoApprove: Bool = false) async throws -> AgentTask? {
        let data = try await Gateway.send("POST", "/v1/tasks", json: body(goal: goal, device: device, persona: persona, notify: notify, when: when, autoApprove: autoApprove))
        return try? JSONDecoder().decode(AgentTask.self, from: data)
    }

    /// "끝나면 전화": change how a running task reports back.
    static func setNotify(_ id: String, _ notify: String) async throws {
        _ = try await Gateway.send("PATCH", "/v1/tasks/" + escape(id), json: ["notify": notify])
    }

    static func cancel(_ id: String) async throws { _ = try await Gateway.send("POST", "/v1/tasks/\(escape(id))/cancel") }

    /// Device names for the picker (`GET /v1/devices`): a list of objects with `name`, or plain names.
    static func devices() async throws -> [String] {
        let json = try JSONSerialization.jsonObject(with: try await Gateway.send("GET", "/v1/devices"))
        let list = (json as? [Any]) ?? ((json as? [String: Any])?["devices"] as? [Any]) ?? []
        return list.compactMap { ($0 as? [String: Any])?["name"] as? String ?? $0 as? String }.map(DeviceID.normalize)
    }

    private static func escape(_ s: String) -> String { s.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? s }
}

/// Tasks for home, the popover and the 작업 screen; polls every 3 s while one is active.
@MainActor
final class TaskModel: ObservableObject {
    static let shared = TaskModel()
    @Published private(set) var tasks: [AgentTask] = []
    @Published private(set) var status: SectionStatus = .ok
    /// A finish notification asked to open this task.
    @Published var open: AgentTask?
    /// The morning "밤사이 한 일" push asked for the task list.
    @Published var showList = false

    /// Waiting for overnight or a set time, soonest first.
    var scheduled: [AgentTask] { tasks.filter(\.isScheduled).sorted { ($0.scheduledFor ?? .distantFuture) < ($1.scheduledFor ?? .distantFuture) } }

    nonisolated static func isOvernightPush(_ userInfo: [AnyHashable: Any]) -> Bool { (userInfo["type"] as? String) == "overnight" }
    private var poll: Task<Void, Never>?

    var active: [AgentTask] { tasks.filter(\.isActive) }

    func refresh() async {
        guard Config.token != nil else { return }
        switch await attempt({ try await TaskAPI.list() }) {
        case .success(let l): tasks = l.sorted { ($0.created_at ?? .distantPast) > ($1.created_at ?? .distantPast) }; status = .ok
        case .failure(let e): status = SectionStatus.from(e) ?? status
        }
        schedulePoll()
    }

    private func schedulePoll() {
        poll?.cancel()
        guard !active.isEmpty else { return }
        poll = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            guard !Task.isCancelled else { return }
            await self?.refresh()
        }
    }

    @discardableResult
    func create(goal: String, device: String?, persona: String?, notify: TaskNotify, when: TaskWhen = .now, autoApprove: Bool = false) async throws -> AgentTask? {
        let t = try await TaskAPI.create(goal: goal, device: device, persona: persona, notify: notify, when: when, autoApprove: autoApprove)
        await refresh()
        return t
    }

    func setNotify(_ t: AgentTask, call: Bool) async throws {
        try await TaskAPI.setNotify(t.id, call ? "call" : "push")
        await refresh()
    }

    func cancel(_ t: AgentTask) async throws {
        try await TaskAPI.cancel(t.id)
        await refresh()
    }

    /// Deep link from a finish notification (`task` / `task_id` in the payload, or azel://task/<id>).
    func openTask(id: String) async {
        if case .success(let t) = await attempt({ try await TaskAPI.get(id) }) { open = t }
    }

    nonisolated static func taskID(from userInfo: [AnyHashable: Any]) -> String? {
        (userInfo["task_id"] ?? userInfo["task"]).flatMap { ($0 as? String) ?? ($0 as? NSNumber)?.stringValue }
    }

    nonisolated static func taskID(from url: URL) -> String? {
        guard url.scheme == "azel", url.host == "task" else { return nil }
        let id = url.pathComponents.dropFirst().first
        return id?.isEmpty == false ? id : nil
    }
}

/// What a call opened from a task or a reminder is about (the call's `context`):
/// `{kind: "task" | "schedule", label, id}`, or plain text.
struct CallContext: Equatable {
    var kind: String?
    var label: String
    var id: String?

    /// From the call's first WS frame (`{"type":"context","context":{…}}`).
    static func from(event e: CallEvent) -> CallContext? {
        guard e.type == "context", let c = e.context, let label = c.label, !label.isEmpty else { return nil }
        return CallContext(kind: c.kind, label: label, id: c.id)
    }

    static func from(_ v: Any?) -> CallContext? {
        if let s = v as? String { return s.isEmpty ? nil : CallContext(kind: nil, label: s, id: nil) }
        guard let d = v as? [String: Any], let label = (d["label"] ?? d["text"] ?? d["goal"] ?? d["purpose"]) as? String, !label.isEmpty else { return nil }
        return CallContext(kind: d["kind"] as? String, label: label, id: d["id"].map(describe))
    }

    /// The incoming-call screen's short reason (≤ 14 characters, then …): "작업: 창업 리서치",
    /// "알림: 역사학특강 과제", "아침 브리핑", "마감 알림", "서버 알림".
    var reason: String {
        let r: String
        switch kind {
        case "task": r = "작업: " + label
        case "schedule", "reminder": r = "알림: " + label
        case "wake": r = "깨우기"
        case "briefing", "morning_briefing": r = "아침 브리핑"
        case "deadline", "deadline_check": r = "마감 알림"
        case "server", "server_alert", "server_alerts": r = "서버 알림"
        default: r = label
        }
        return Self.clip(r, 14)
    }

    static func clip(_ s: String, _ n: Int) -> String { s.count <= n ? s : String(s.prefix(n - 1)) + "…" }

    /// "아젤 · 작업: 창업 리서치" for CallKit, recents and the Live Activity; the plain name without context.
    static func callerLine(_ name: String, _ context: CallContext?) -> String {
        guard let context else { return name }
        return "\(name) · \(context.reason)"
    }

    /// "맡긴 작업: …" / "예약: …".
    var chip: String {
        switch kind {
        case "task": return "맡긴 작업: " + label
        case "schedule", "reminder", "wake": return "예약: " + label
        default: return label
        }
    }
}

/// One array element that may fail to decode without failing the array.
struct Lenient<T: Decodable>: Decodable {
    let value: T?
    init(from decoder: Decoder) throws { value = try? T(from: decoder) }
}
