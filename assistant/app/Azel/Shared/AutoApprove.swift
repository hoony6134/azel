import SwiftUI

/// An auto-approve grant (`/v1/auto-approve`, PROTOCOL "Auto-approve"): safe device commands run
/// without a tap for one call or task; risky ones still ask.
struct AutoGrant: Identifiable, Equatable {
    struct Entry: Identifiable, Equatable {
        var id: String { "\(at?.timeIntervalSince1970 ?? 0)-\(command)" }
        var device: String
        var command: String
        var at: Date?
        var stage: String?
    }

    var id: String
    var scope: String          // "통화" / "작업"
    var devices: [String]?
    var by: String?
    var created: Date?
    var expires: Date?
    var ran: Int
    var ended: Bool
    var log: [Entry]

    init(_ d: [String: Any]) {
        id = describe(d["id"] ?? "")
        let s = d["scope"] as? [String: Any]
        scope = s?["task"] != nil ? "작업" : "통화"
        devices = (d["devices"] as? [String])?.map(DeviceID.normalize)
        by = d["by"] as? String
        created = ApprovalRequest.time(d["created"])
        expires = ApprovalRequest.time(d["expires_at"])
        log = ((d["log"] as? [[String: Any]]) ?? []).map {
            Entry(device: DeviceID.normalize(describe($0["device"] ?? "")), command: describe($0["command"] ?? ""),
                  at: ApprovalRequest.time($0["at"]), stage: $0["stage"] as? String)
        }
        ran = (d["ran"] as? Int) ?? log.count
        ended = (d["ended"] as? Bool) ?? false
    }

    func isLive(now: Date = Date()) -> Bool { !ended && (expires.map { $0 > now } ?? false) }

    /// Which check let it run: 규칙 / 로컬 판단 / Claude 판단.
    static func stageLabel(_ s: String?) -> String? {
        switch s {
        case "rules", "rule": return "규칙"
        case "qwen", "local": return "로컬 판단"
        case "haiku", "claude": return "Claude 판단"
        case let o?: return o
        default: return nil
        }
    }

    /// "23분 남음" / "1시간 5분 남음".
    static func remaining(_ until: Date?, now: Date = Date()) -> String? {
        guard let until else { return nil }
        let m = max(0, Int((until.timeIntervalSince(now) / 60).rounded(.up)))
        return m >= 60 ? "\(m / 60)시간\(m % 60 > 0 ? " \(m % 60)분" : "") 남음" : "\(m)분 남음"
    }
}

enum AutoApproveAPI {
    static func live() async throws -> [AutoGrant] {
        let json = try JSONSerialization.jsonObject(with: try await Gateway.send("GET", "/v1/auto-approve"))
        return ((json as? [[String: Any]]) ?? []).map(AutoGrant.init)
    }

    static func get(_ id: String) async throws -> AutoGrant? {
        let json = try JSONSerialization.jsonObject(with: try await Gateway.send("GET", "/v1/auto-approve/" + escape(id)))
        return (json as? [String: Any]).map(AutoGrant.init)
    }

    /// A task's grant (the call's goes over the call socket).
    @discardableResult
    static func grant(task: String, minutes: Int = 120, devices: [String]? = nil) async throws -> AutoGrant? {
        var b: [String: Any] = ["task": task, "minutes": minutes]
        if let devices, !devices.isEmpty { b["devices"] = devices }
        let json = try JSONSerialization.jsonObject(with: try await Gateway.send("POST", "/v1/auto-approve", json: b))
        return (json as? [String: Any]).map(AutoGrant.init)
    }

    static func revoke(_ id: String) async throws { _ = try await Gateway.send("DELETE", "/v1/auto-approve/" + escape(id)) }

    private static func escape(_ s: String) -> String { s.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? s }
}

/// Grants for 설정 → 승인 and the summary push. The server lists only live grants; ended ones are
/// fetched by id, so the ids seen here are remembered (the last 30).
@MainActor
final class AutoGrantCenter: ObservableObject {
    static let shared = AutoGrantCenter()
    @Published private(set) var grants: [AutoGrant] = []
    @Published private(set) var status: SectionStatus = .ok
    /// The summary push asked to show this grant's log.
    @Published var open: AutoGrant?
    /// Call: the duration and device filter the toggle uses.
    @AppStorage("autoMinutes") var minutes = 60
    private static let key = "autoGrantIDs"

    nonisolated static func remember(_ id: String) {
        var ids = UserDefaults.standard.stringArray(forKey: key) ?? []
        ids.removeAll { $0 == id }
        ids.insert(id, at: 0)
        UserDefaults.standard.set(Array(ids.prefix(30)), forKey: key)
    }

    func refresh() async {
        switch await attempt({ try await AutoApproveAPI.live() }) {
        case .failure(let e): status = SectionStatus.from(e) ?? status; return
        case .success(let live):
            status = .ok
            live.forEach { Self.remember($0.id) }
            var out = live
            for id in (UserDefaults.standard.stringArray(forKey: Self.key) ?? []) where !out.contains(where: { $0.id == id }) {
                if case .success(let g?) = await attempt({ try await AutoApproveAPI.get(id) }) { out.append(g) }
            }
            grants = out.sorted { ($0.created ?? .distantPast) > ($1.created ?? .distantPast) }
        }
    }

    func revoke(_ g: AutoGrant) async throws {
        try await AutoApproveAPI.revoke(g.id)
        await refresh()
    }

    /// The `auto_summary` push (`grant_id`, no command text).
    func openGrant(id: String) async {
        Self.remember(id)
        if case .success(let g?) = await attempt({ try await AutoApproveAPI.get(id) }) { open = g }
    }

    nonisolated static func grantID(from userInfo: [AnyHashable: Any]) -> String? {
        guard (userInfo["type"] as? String) == "auto_summary" else { return nil }
        return userInfo["grant_id"].map(describe)
    }
}

/// One grant and what ran under it.
struct AutoGrantLog: View {
    let grant: AutoGrant

    var body: some View {
        List {
            Section {
                LabeledContent("범위", value: grant.scope)
                if let d = grant.devices, !d.isEmpty { LabeledContent("기기", value: d.joined(separator: ", ")) }
                if grant.isLive(), let r = AutoGrant.remaining(grant.expires) { LabeledContent("상태", value: r) }
                else { LabeledContent("상태", value: "끝남") }
            }
            Section("자동으로 실행한 명령") {
                if grant.log.isEmpty { Text("아직 없어요").foregroundStyle(Theme.text3) }
                ForEach(grant.log) { e in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(e.command).font(.callout.monospaced()).foregroundStyle(Theme.text).textSelection(.enabled)
                        Text([e.device, e.at.map { $0.formatted(date: .abbreviated, time: .shortened) }, AutoGrant.stageLabel(e.stage)]
                            .compactMap { $0 }.joined(separator: " · ")).font(.footnote).foregroundStyle(Theme.text3)
                    }
                    .accessibilityElement(children: .combine)
                }
            }
        }
        .navigationTitle("자동 승인")
    }
}

/// 설정 → 승인: live and past grants.
struct AutoGrantsCard: View {
    @ObservedObject private var center = AutoGrantCenter.shared
    @State private var message: String?

    var body: some View {
        if center.status == .unavailable { EmptyView() } else { card }
    }

    private var card: some View {
        Card("자동 승인") {
            SectionNote(status: center.status) { Task { await center.refresh() } }
            if center.grants.isEmpty && center.status == .ok { Text("자동 승인 기록이 없어요").foregroundStyle(Theme.text3) }
            ForEach(center.grants) { g in
                HStack {
                    Button { center.open = g } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(g.scope) · 명령 \(g.ran)개").foregroundStyle(Theme.text)
                            Text([g.created.map { $0.formatted(date: .abbreviated, time: .shortened) },
                                  g.isLive() ? AutoGrant.remaining(g.expires) : "끝남"].compactMap { $0 }.joined(separator: " · "))
                                .font(.footnote).foregroundStyle(Theme.text3)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    if g.isLive() {
                        Button("끄기", role: .destructive) {
                            Task { do { try await center.revoke(g) } catch { message = "끄지 못했어요: " + FriendlyError.describe(error).title } }
                        }
                        .font(.subheadline).buttonStyle(.borderless)
                    }
                }
            }
            if let message { Text(message).font(.footnote).foregroundStyle(Theme.danger) }
        }
        .task { await center.refresh() }
        #if os(macOS)
        .autoGrantSheet()   // iPhone: hosted at the root, so the summary push can open it anywhere
        #endif
    }
}

extension View {
    func autoGrantSheet() -> some View { modifier(AutoGrantSheetHost()) }
}

private struct AutoGrantSheetHost: ViewModifier {
    @ObservedObject private var center = AutoGrantCenter.shared
    func body(content: Content) -> some View {
        content.sheet(item: $center.open) { g in
            NavigationStack { AutoGrantLog(grant: g).toolbar { ToolbarItem(placement: .cancellationAction) { Button("닫기") { center.open = nil } } } }
                #if os(macOS)
                .frame(width: 520, height: 560)
                #endif
        }
    }
}
