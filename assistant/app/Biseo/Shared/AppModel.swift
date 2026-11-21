import Foundation

/// Gateway state the home, approvals and menu-bar screens share. Refreshes on demand and every 30 s.
@MainActor
final class AppModel: ObservableObject {
    @Published var configured = Config.token != nil
    @Published var status: ServerStatus?
    @Published var statusError: Error?
    @Published var approvals: [[String: Any]] = []
    @Published var session: String
    let sessionPrefix: String
    private var timer: Timer?

    init(sessionPrefix: String) {
        self.sessionPrefix = sessionPrefix
        session = Session.current(sessionPrefix)
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refresh() }
        }
        Task { await refresh() }
    }

    /// Spark and gateway both answered.
    var online: Bool { status.map { $0.spark.ok && $0.gateway.ok } ?? false }

    func refresh() async {
        session = Session.current(sessionPrefix)
        guard configured else { return }
        do { status = try await Gateway.get("/v1/status"); statusError = nil } catch { status = nil; statusError = error }
        if let s: AppSettings = try? await Gateway.get("/v1/settings"), let name = s.assistant_name { AssistantName.current = name }
        if let items = try? await Gateway.approvals() { approvals = items }
    }

    func newConversation() { session = Session.rotate(sessionPrefix) }

    func decide(_ item: [String: Any], _ approve: Bool) async throws {
        let id = describe(item["id"] ?? "")
        let path = "/v1/approvals/" + (id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id)
        _ = try await Gateway.send("POST", path, json: ["approve": approve])
        await refresh()
    }
}

/// Numbers and strings as-is; nested objects as compact JSON (the contract leaves their shape open).
func describe(_ value: Any) -> String {
    if let s = value as? String { return s }
    if let n = value as? NSNumber { return n.stringValue }
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) { return String(decoding: data, as: UTF8.self) }
    return "\(value)"
}

/// "좋은 아침이에요" … by hour.
func greeting(at date: Date = Date()) -> String {
    switch Calendar.current.component(.hour, from: date) {
    case 5..<12: return "좋은 아침이에요"
    case 12..<18: return "좋은 오후예요"
    default: return "좋은 저녁이에요"
    }
}
