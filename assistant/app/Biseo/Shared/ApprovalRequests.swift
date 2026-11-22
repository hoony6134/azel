import SwiftUI

/// A device command waiting for the user (`/v1/approvals`). Built from the loosely shaped JSON the
/// gateway returns, so older items (no persona or expiry) still show.
struct ApprovalRequest: Identifiable, Equatable {
    var id: String
    var device: String
    var command: String
    var cwd: String?
    var persona: String?
    var created: Date?
    var expires: Date?
    var status: String              // pending | approved | denied | expired | …
    var decision: String?
    /// The server allows only "once" for this command (it never becomes an always-allow rule).
    var forcedOnce: Bool
    var forcedReason: String?

    init(_ d: [String: Any]) {
        id = describe(d["id"] ?? "")
        device = DeviceID.normalize(describe(d["device"] ?? ""))
        command = describe(d["command"] ?? d["tool"] ?? "")
        cwd = (d["cwd"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        persona = (d["persona"] as? String) ?? (d["persona_id"] as? String)
        created = Self.time(d["created_at"] ?? d["created"])
        expires = Self.time(d["expires_at"])
        status = (d["status"] as? String) ?? "pending"
        decision = d["decision"] as? String
        forcedOnce = (d["forced_once"] as? Bool) ?? false
        forcedReason = (d["forced_reason"] as? String).flatMap { $0.isEmpty ? nil : $0 }
    }

    var isPending: Bool { status == "pending" }

    var forcedNote: String? {
        guard forcedOnce else { return nil }
        return "이 명령은 매번 확인이 필요해요" + (forcedReason.map { ": " + $0 } ?? "")
    }

    func secondsLeft(now: Date = Date()) -> Int? { expires.map { max(0, Int($0.timeIntervalSince(now).rounded(.up))) } }
    func isExpired(now: Date = Date()) -> Bool { secondsLeft(now: now) == 0 || status == "expired" }

    /// History label: the decision when the gateway records one, else the status.
    static func outcome(_ a: ApprovalRequest) -> String {
        switch a.decision ?? a.status {
        case "once", "approved": return "허용"
        case "always": return "항상 허용"
        case "deny", "denied": return "거부"
        case "expired": return "만료"
        default: return a.status
        }
    }

    /// "4:59"
    static func countdown(_ seconds: Int) -> String { String(format: "%d:%02d", seconds / 60, seconds % 60) }

    static func time(_ v: Any?) -> Date? {
        if let n = v as? NSNumber { return Date(timeIntervalSince1970: n.doubleValue) }
        guard let s = v as? String else { return nil }
        let f = ISO8601DateFormatter()
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f.date(from: s)
    }

    static func == (a: ApprovalRequest, b: ApprovalRequest) -> Bool { a.id == b.id && a.status == b.status }
}

/// An "always allow on this device" rule.
struct ApprovalRule: Identifiable, Equatable {
    var id: String
    var device: String
    var command: String
    var created: Date?

    init(_ d: [String: Any]) {
        id = describe(d["id"] ?? "")
        device = DeviceID.normalize(describe(d["device"] ?? ""))
        command = describe(d["command"] ?? d["pattern"] ?? "")
        created = ApprovalRequest.time(d["created_at"] ?? d["created"])
    }
}

enum ApprovalDecision: String { case once, always, deny }

enum ApprovalAPI {
    /// The new `decision` plus the old `approve` flag: the deployed gateway reads only `approve`,
    /// and a body without it would be taken as a deny.
    static func body(_ d: ApprovalDecision) -> [String: Any] { ["decision": d.rawValue, "approve": d != .deny] }

    /// Returns the server's note when it allowed less than asked (`forced_once`: e.g. an "always" from
    /// a secret call becomes once).
    @discardableResult
    static func decide(_ id: String, _ d: ApprovalDecision) async throws -> String? {
        forcedNote(try await Gateway.send("POST", "/v1/approvals/" + escape(id), json: body(d)))
    }

    static func forcedNote(_ data: Data) -> String? {
        guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], (o["forced_once"] as? Bool) == true else { return nil }
        return (o["forced_reason"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "이번 한 번만 허용했어요"
    }

    /// Pending plus recent history where the gateway offers it (an older one returns pending only).
    static func list() async throws -> [ApprovalRequest] {
        try Gateway.parseApprovals(try await Gateway.send("GET", "/v1/approvals?include=all")).map(ApprovalRequest.init)
    }

    static func get(_ id: String) async throws -> ApprovalRequest? {
        let json = try JSONSerialization.jsonObject(with: try await Gateway.send("GET", "/v1/approvals/" + escape(id)))
        return (json as? [String: Any]).map(ApprovalRequest.init)
    }

    static func rules() async throws -> [ApprovalRule] {
        let json = try JSONSerialization.jsonObject(with: try await Gateway.send("GET", "/v1/approvals/rules"))
        let list = (json as? [[String: Any]]) ?? ((json as? [String: Any])?["rules"] as? [[String: Any]]) ?? []
        return list.map(ApprovalRule.init)
    }

    static func revoke(_ id: String) async throws { _ = try await Gateway.send("DELETE", "/v1/approvals/rules/" + escape(id)) }

    private static func escape(_ s: String) -> String { s.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? s }
}

/// Watches for new pending approvals while the app is open and presents each one once (iPhone:
/// a sheet; Mac: a small floating window). A push tap opens the same sheet.
@MainActor
final class ApprovalCenter: ObservableObject {
    static let shared = ApprovalCenter()
    @Published var presented: ApprovalRequest?
    @Published private(set) var all: [ApprovalRequest] = []
    @Published private(set) var status: SectionStatus = .ok
    /// Approvals already put in front of the user: never again, across launches.
    private var seen: Set<String> = Set(UserDefaults.standard.stringArray(forKey: ApprovalCenter.seenKey) ?? [])
    /// The pending ids of the previous poll: an approval must be pending twice in a row to pop up,
    /// so a list flapping pending ↔ expired (user bug, 2026-11-22) can't flash the sheet.
    private var lastPending: Set<String> = []
    private static let seenKey = "approvalsShown"
    private var poll: Task<Void, Never>?
    /// Mac: shows the sheet in its own window.
    var present: ((ApprovalRequest) -> Void)?

    var pending: [ApprovalRequest] { all.filter { $0.isPending && !$0.isExpired() } }
    var history: [ApprovalRequest] { all.filter { !$0.isPending }.sorted { ($0.created ?? .distantPast) > ($1.created ?? .distantPast) } }

    /// Poll every 5 s while the app is in the foreground (approvals expire in 5 minutes).
    func start() {
        guard poll == nil, !Config.isTestHost else { return }
        poll = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
    }

    func stop() { poll?.cancel(); poll = nil }

    func refresh() async {
        guard Config.token != nil else { return }
        switch await attempt({ try await ApprovalAPI.list() }) {
        case .success(let list):
            all = list
            status = .ok
            let (next, pendingNow) = Self.stableNext(list, previous: lastPending, seen: seen)
            lastPending = pendingNow
            // A sheet already up stays up: a poll never replaces or closes it (it shows the live state).
            if presented == nil, let next { show(next) }
        case .failure(let e): status = SectionStatus.from(e) ?? status
        }
    }

    /// Pending in this poll and the one before, and never shown: the oldest such one. Also returns
    /// this poll's pending ids, to compare against next time.
    nonisolated static func stableNext(_ list: [ApprovalRequest], previous: Set<String>, seen: Set<String>, now: Date = Date()) -> (ApprovalRequest?, Set<String>) {
        let pending = Set(list.filter { $0.isPending && !$0.isExpired(now: now) }.map(\.id))
        let next = nextToShow(list.filter { previous.contains($0.id) }, seen: seen, now: now)
        return (next, pending)
    }

    /// The oldest pending, unexpired approval not presented yet.
    nonisolated static func nextToShow(_ list: [ApprovalRequest], seen: Set<String>, now: Date = Date()) -> ApprovalRequest? {
        list.filter { $0.isPending && !$0.isExpired(now: now) && !seen.contains($0.id) }
            .min { ($0.created ?? .distantPast) < ($1.created ?? .distantPast) }
    }

    /// Asks for the sheet; it counts as seen only once it is on screen (`markShown`), so one that
    /// couldn't present (e.g. under the call cover) comes back on the next poll.
    func show(_ a: ApprovalRequest) {
        presented = a                  // also on the Mac, so a poll never swaps the window's content
        present?(a)
    }

    func markShown(_ id: String) {
        guard seen.insert(id).inserted else { return }
        UserDefaults.standard.set(Array(seen.suffix(300)), forKey: Self.seenKey)
    }

    /// The latest copy of an approval from the last poll; nil when the poll didn't list it.
    func live(_ id: String) -> ApprovalRequest? { all.first { $0.id == id } }

    /// A fresher copy of one approval (the open sheet's own check): replaces it in the list, so
    /// the pending list and the badges drop it at once.
    func update(_ a: ApprovalRequest) {
        if let i = all.firstIndex(where: { $0.id == a.id }) { all[i] = a } else { all.append(a) }
    }

    /// What the other device did, for a sheet that was still open here.
    nonisolated static func elsewhereText(_ a: ApprovalRequest) -> String {
        switch a.decision ?? a.status {
        case "once", "always", "approved", "used": return "다른 기기에서 허용했어요"
        case "deny", "denied": return "다른 기기에서 거부했어요"
        case "expired": return "만료됐어요"
        default: return a.isExpired() ? "만료됐어요" : "이미 처리됐어요"
        }
    }

    /// The server's "already decided" (409, or a 400 saying so).
    nonisolated static func alreadyDecided(_ error: Error) -> Bool {
        if case GatewayError.http(409) = error { return true }
        if case GatewayError.rejected(let d) = error { let l = d.lowercased(); return l.contains("already") || d.contains("이미") }
        return false
    }

    /// Push tap (`type: "approval"`, `approval_id`). Decided already (here or elsewhere): ignored.
    func open(id: String) async {
        let fresh: ApprovalRequest?
        if case .success(let a?) = await attempt({ try await ApprovalAPI.get(id) }) { fresh = a; update(a) } else { fresh = all.first { $0.id == id } }
        guard let a = fresh, a.isPending, !a.isExpired() else { return }
        show(a)
    }

    @discardableResult
    func decide(_ a: ApprovalRequest, _ d: ApprovalDecision) async throws -> String? {
        let note = try await ApprovalAPI.decide(a.id, d)
        await refresh()
        return note
    }

    nonisolated static func approvalID(from userInfo: [AnyHashable: Any]) -> String? {
        guard (userInfo["type"] as? String) == "approval" else { return nil }
        return (userInfo["approval_id"] as? String) ?? (userInfo["approval_id"] as? NSNumber)?.stringValue
    }
}

// MARK: - Views

/// The decision sheet: device, the exact command (wrapped, never cut), folder, persona, countdown.
struct ApprovalSheet: View {
    let request: ApprovalRequest
    var onDone: () -> Void
    @ObservedObject private var center = ApprovalCenter.shared
    @State private var message: String?
    @State private var working = false
    /// The server allowed only once (`forced_reason`): shown with 닫기 instead of closing.
    @State private var forced: String?
    /// Decided on another device (or expired) while open here: shown briefly, then the sheet closes.
    @State private var elsewhere: String?
    @State private var decidedHere = false

    /// Decided or expired on the server: the sheet says so and offers 닫기 instead of closing by
    /// itself. A poll that merely misses the item changes nothing.
    private func settled(now: Date) -> String? {
        let live = center.live(request.id) ?? request
        if live.isPending && !live.isExpired(now: now) && !request.isExpired(now: now) { return nil }
        return live.isPending || live.status == "expired" ? "만료됐어요" : "이미 처리됐어요 (\(ApprovalRequest.outcome(live)))"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.l) {
            HStack {
                Label(request.device.isEmpty ? "기기" : request.device, systemImage: "laptopcomputer").font(.headline).foregroundStyle(Theme.text)
                Spacer()
                if request.expires != nil {
                    TimelineView(.periodic(from: .now, by: 1)) { ctx in
                        let left = request.secondsLeft(now: ctx.date) ?? 0
                        Text(left > 0 ? ApprovalRequest.countdown(left) : "만료됨").font(.subheadline.monospacedDigit())
                            .foregroundStyle(left > 30 ? Theme.text2 : Theme.amber)
                            .accessibilityLabel(left > 0 ? "\(left)초 남음" : "만료됨")
                    }
                }
            }
            Text("이 명령을 실행해도 될까요?").foregroundStyle(Theme.text2)
            ScrollView {
                Text(request.command).font(.callout.monospaced()).foregroundStyle(Theme.text)
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(Theme.m)
            }
            .frame(maxHeight: 220)
            .background(Theme.bg, in: RoundedRectangle(cornerRadius: Theme.rowRadius, style: .continuous))
            if let cwd = request.cwd { Label(cwd, systemImage: "folder").font(.footnote).foregroundStyle(Theme.text2) }
            if let p = PersonaModel.shared.label(for: request.persona) { PersonaChip(name: p.name, tint: p.tint) }
            if let message { Text(message).font(.footnote).foregroundStyle(Theme.danger) }
            if let note = request.forcedNote {
                Label(note, systemImage: "exclamationmark.shield").font(.footnote).foregroundStyle(Theme.amber)
                    .fixedSize(horizontal: false, vertical: true)
            }
            TimelineView(.periodic(from: .now, by: 1)) { ctx in
                if let elsewhere {
                    Label(elsewhere, systemImage: "checkmark.circle").foregroundStyle(Theme.text2)
                        .frame(maxWidth: .infinity).frame(height: 48)
                } else if let forced {
                    VStack(spacing: Theme.s) {
                        Label(forced, systemImage: "checkmark.circle").foregroundStyle(Theme.text2).fixedSize(horizontal: false, vertical: true)
                        Button("닫기") { onDone() }.frame(maxWidth: .infinity).frame(height: 40)
                    }
                } else if let state = settled(now: ctx.date) {
                    VStack(spacing: Theme.s) {
                        Label(state, systemImage: "checkmark.circle").foregroundStyle(Theme.text2)
                        Button("닫기") { onDone() }.frame(maxWidth: .infinity).frame(height: 40)
                    }
                } else {
                let expired = request.isExpired(now: ctx.date)
                VStack(spacing: Theme.s) {
                    Button { decide(.once) } label: {
                        Text("이번만 허용").font(.headline).foregroundStyle(Theme.onAccent).frame(maxWidth: .infinity).frame(height: 48)
                            .background(Theme.celadon, in: RoundedRectangle(cornerRadius: Theme.rowRadius, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    // A forced command can't become a rule: the second button is gone, the note above says why.
                    if !request.forcedOnce {
                        Button("이 기기에서 항상 허용") { decide(.always) }.frame(maxWidth: .infinity).frame(height: 40)
                    }
                    Button("거부", role: .destructive) { decide(.deny) }.frame(maxWidth: .infinity).frame(height: 40)
                }
                .disabled(expired || working)
                .opacity(expired ? 0.5 : 1)
                }
            }
        }
        .padding(24)
        .background(Theme.bgRaised.ignoresSafeArea())
        .accessibilityElement(children: .contain)
        .onAppear { ApprovalCenter.shared.markShown(request.id) }
        .task(id: request.id) { await watch() }
    }

    /// While open: check this approval every 2 s; once it's decided elsewhere or expired, say so
    /// for 1.5 s and close (user bug, 2026-11-22: the Mac kept a sheet the iPhone had answered).
    private func watch() async {
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            guard !Task.isCancelled, !working, !decidedHere, forced == nil, elsewhere == nil else { continue }
            if case .success(let a?) = await attempt({ try await ApprovalAPI.get(request.id) }) {
                center.update(a)
                if !a.isPending || a.isExpired() { await closeSaying(ApprovalCenter.elsewhereText(a)); return }
            } else if request.isExpired() {
                await closeSaying("만료됐어요"); return
            }
        }
    }

    private func closeSaying(_ text: String) async {
        elsewhere = text
        try? await Task.sleep(nanoseconds: 1_500_000_000)
        onDone()
    }

    private func decide(_ d: ApprovalDecision) {
        working = true
        Task {
            do {
                decidedHere = true
                if let note = try await ApprovalCenter.shared.decide(request, d) { forced = note } else { onDone() }
            } catch let error where ApprovalCenter.alreadyDecided(error) {
                // Someone answered on another device first: no error, just say what happened.
                let fresh = (try? await ApprovalAPI.get(request.id)) ?? nil
                if let fresh { center.update(fresh) }
                await closeSaying(fresh.map(ApprovalCenter.elsewhereText) ?? "이미 처리됐어요")
            }
            catch { decidedHere = false; message = "보내지 못했어요: " + FriendlyError.describe(error).title }
            working = false
        }
    }
}

/// 설정 → 항상 허용한 명령.
struct ApprovalRulesCard: View {
    @State private var rules: [ApprovalRule] = []
    @State private var status: SectionStatus = .ok
    @State private var message: String?

    var body: some View {
        Card("항상 허용한 명령") {
            SectionNote(status: status) { Task { await load() } }
            if rules.isEmpty && status == .ok { Text("항상 허용한 명령이 없어요").foregroundStyle(Theme.text3) }
            ForEach(rules) { r in
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(r.command).font(.callout.monospaced()).foregroundStyle(Theme.text).fixedSize(horizontal: false, vertical: true)
                        Text(r.device).font(.footnote).foregroundStyle(Theme.text3)
                    }
                    Spacer()
                    Button("해제", role: .destructive) { Task { await revoke(r) } }.font(.subheadline).buttonStyle(.borderless)
                }
                .accessibilityElement(children: .combine)
                .accessibilityAction(named: "해제") { Task { await revoke(r) } }
            }
            if let message { Text(message).font(.footnote).foregroundStyle(Theme.danger) }
        }
        .task { await load() }
    }

    private func load() async {
        switch await attempt({ try await ApprovalAPI.rules() }) {
        case .success(let r): rules = r; status = .ok
        case .failure(let e): status = SectionStatus.from(e) ?? status
        }
    }

    private func revoke(_ r: ApprovalRule) async {
        do { try await ApprovalAPI.revoke(r.id); message = nil; await load() }
        catch { message = "해제하지 못했어요: " + FriendlyError.describe(error).title }
    }
}

extension View {
    /// The approval sheet from this view, while `active`: the root when no call is up, the call
    /// screen during a call (a sheet under a full-screen cover can't present).
    func approvalSheet(active: Bool) -> some View {
        modifier(ApprovalSheetHost(active: active))
    }
}

private struct ApprovalSheetHost: ViewModifier {
    let active: Bool
    @ObservedObject private var center = ApprovalCenter.shared

    func body(content: Content) -> some View {
        content.sheet(item: Binding(get: { active ? center.presented : nil },
                                    set: { if active && $0 == nil { center.presented = nil } })) { a in
            ApprovalSheet(request: a) { center.presented = nil }
                #if os(iOS)
                .presentationDetents([.medium, .large])
                #endif
        }
    }
}
