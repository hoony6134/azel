import SwiftUI

/// Devices are shown by their English ids (macbook, albireo, gienah, deneb, spark). Older settings
/// and gateways used Korean names; those map to the ids.
enum DeviceID {
    static let known = ["macbook", "albireo", "gienah", "deneb", "spark"]
    private static let korean = ["맥북": "macbook", "알비레오": "albireo", "기에나": "gienah", "지에나": "gienah",
                                 "데네브": "deneb", "스파크": "spark"]

    static func normalize(_ name: String) -> String {
        let trimmed = name.trimmingCharacters(in: .whitespaces)
        return korean[trimmed] ?? trimmed.lowercased()
    }

    /// Settings with every device field in id form (order kept, duplicates dropped).
    static func normalize(_ s: AppSettings) -> AppSettings {
        var out = s
        out.default_device = normalize(s.default_device)
        out.device_fallback = s.device_fallback.map(normalize).reduce(into: []) { if !$0.contains($1) { $0.append($1) } }
        return out
    }
}

/// One entry of `GET /v1/proactive/log`: why the assistant did (or didn't) call. The server sends
/// {id, at (epoch), call, urgency (number), reason, opening, called, blocked_by, signals, call_id,
/// persona_id}; older builds sent `ts`.
struct ProactiveDecision: Decodable, Identifiable, Equatable {
    var id: String { serverID ?? "\(ts.timeIntervalSince1970)-\(reason)" }
    let serverID: String?
    let ts: Date
    /// The model wanted to call.
    let call: Bool
    /// A call actually went out (`called`); falls back to `call` on older servers.
    let called: Bool
    let urgency: String?
    let reason: String
    let opening: String?
    let blockedBy: String?

    private enum Keys: String, CodingKey { case id, at, ts, call, called, urgency, reason, opening, blocked_by }

    init(ts: Date, call: Bool, urgency: String?, reason: String, called: Bool? = nil, opening: String? = nil, blockedBy: String? = nil) {
        serverID = nil; self.ts = ts; self.call = call; self.called = called ?? call; self.urgency = urgency
        self.reason = reason; self.opening = opening; self.blockedBy = blockedBy
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        if let s = try? c.decode(String.self, forKey: .id) { serverID = s }
        else if let n = try? c.decode(Int.self, forKey: .id) { serverID = String(n) }
        else { serverID = nil }
        let key: Keys = c.contains(.at) ? .at : .ts
        if let n = try? c.decode(Double.self, forKey: key) { ts = Date(timeIntervalSince1970: n) }
        else {
            let s = try c.decode(String.self, forKey: key)
            let f = ISO8601DateFormatter()
            f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            let g = ISO8601DateFormatter()
            guard let d = f.date(from: s) ?? g.date(from: s) else {
                throw DecodingError.dataCorruptedError(forKey: key, in: c, debugDescription: "unreadable time \(s)")
            }
            ts = d
        }
        call = (try? c.decodeIfPresent(Bool.self, forKey: .call)) ?? false
        called = (try? c.decodeIfPresent(Bool.self, forKey: .called)) ?? call
        // Urgency may be a word or a number; keep it as text.
        if let s = try? c.decode(String.self, forKey: .urgency) { urgency = s }
        else if let n = try? c.decode(Double.self, forKey: .urgency) { urgency = n == n.rounded() ? String(Int(n)) : String(n) }
        else { urgency = nil }
        reason = (try? c.decodeIfPresent(String.self, forKey: .reason)) ?? ""
        opening = (try? c.decodeIfPresent(String.self, forKey: .opening)).flatMap { $0.isEmpty ? nil : $0 }
        blockedBy = (try? c.decodeIfPresent(String.self, forKey: .blocked_by)).flatMap { $0.isEmpty ? nil : $0 }
    }

    /// Why no call went out, in words; nil when it called.
    var notCalledBecause: String? {
        guard !called else { return nil }
        switch blockedBy {
        case "min_gap", "gap": return "간격 제한으로 안 걸었어요"
        case "daily_cap", "max_per_day", "cap": return "하루 횟수 제한으로 안 걸었어요"
        case "window", "outside_window": return "전화할 수 있는 시간이 아니라서 안 걸었어요"
        case "no_call", nil: return call ? "걸지 못했어요" : "전화할 필요 없다고 판단했어요"
        case let other?: return "안 걸었어요 (\(other))"
        }
    }

    static func load() async throws -> [ProactiveDecision] {
        let data = try await Gateway.send("GET", "/v1/proactive/log")
        if let list = try? JSONDecoder().decode([ProactiveDecision].self, from: data) { return list.sorted { $0.ts > $1.ts } }
        struct Envelope: Decodable { let items: [ProactiveDecision] }
        return try JSONDecoder().decode(Envelope.self, from: data).items.sorted { $0.ts > $1.ts }
    }
}

/// "왜 전화했는지 보기": recent proactive decisions, called or not.
struct ProactiveLogView: View {
    @State private var items: [ProactiveDecision] = []
    @State private var status: SectionStatus = .ok
    @State private var loaded = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: Theme.m) {
                SectionNote(status: status) { Task { await load() } }
                if loaded && items.isEmpty && status == .ok {
                    EmptyState(title: "아직 기록이 없어요", message: "\(AssistantName.josa(AssistantName.current, "이", "가")) 먼저 전화할지 판단하면 여기에 남아요.")
                }
                ForEach(items) { d in
                    Card {
                        HStack(spacing: Theme.s) {
                            Image(systemName: d.called ? "phone.fill" : "phone.down").foregroundStyle(d.called ? Theme.celadon : Theme.text3)
                            Text(d.called ? "전화함" : "전화 안 함").font(.headline).foregroundStyle(Theme.text)
                            if let u = d.urgency { Text("긴급도 \(u)").font(.footnote.weight(.semibold)).foregroundStyle(Theme.text2) }
                            Spacer()
                            Text(KST.dayAndTime(d.ts)).font(.footnote.monospacedDigit()).foregroundStyle(Theme.text3)
                        }
                        if let why = d.notCalledBecause { Text(why).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.amber) }
                        if !d.reason.isEmpty { Text(d.reason).foregroundStyle(Theme.text2).textSelection(.enabled) }
                        if let o = d.opening { Text("첫마디: “\(o)”").font(.footnote).foregroundStyle(Theme.text3).textSelection(.enabled) }
                    }
                    .accessibilityElement(children: .combine)
                }
            }
            .padding(20)
            .frame(maxWidth: 640).frame(maxWidth: .infinity)
        }
        .background(Theme.bg.ignoresSafeArea())
        .navigationTitle("왜 전화했는지")
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("닫기") { dismiss() } } }
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        switch await attempt({ try await ProactiveDecision.load() }) {
        case .success(let list): items = list; status = .ok
        case .failure(let e): guard let s = SectionStatus.from(e) else { return }; status = s
        }
        loaded = true
    }
}
