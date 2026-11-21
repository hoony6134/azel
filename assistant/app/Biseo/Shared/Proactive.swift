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

/// One entry of `GET /v1/proactive/log`: why the assistant did (or didn't) call.
struct ProactiveDecision: Decodable, Identifiable, Equatable {
    var id: String { "\(ts.timeIntervalSince1970)-\(reason)" }
    let ts: Date
    let call: Bool
    let urgency: String?
    let reason: String

    private enum Keys: String, CodingKey { case ts, call, urgency, reason }

    init(ts: Date, call: Bool, urgency: String?, reason: String) {
        self.ts = ts; self.call = call; self.urgency = urgency; self.reason = reason
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        if let n = try? c.decode(Double.self, forKey: .ts) { ts = Date(timeIntervalSince1970: n) }
        else {
            let s = try c.decode(String.self, forKey: .ts)
            let f = ISO8601DateFormatter()
            f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            let g = ISO8601DateFormatter()
            guard let d = f.date(from: s) ?? g.date(from: s) else {
                throw DecodingError.dataCorruptedError(forKey: .ts, in: c, debugDescription: "unreadable ts \(s)")
            }
            ts = d
        }
        call = try c.decodeIfPresent(Bool.self, forKey: .call) ?? false
        // Urgency may be a word or a number; keep it as text.
        if let s = try? c.decode(String.self, forKey: .urgency) { urgency = s }
        else if let n = try? c.decode(Double.self, forKey: .urgency) { urgency = n == n.rounded() ? String(Int(n)) : String(n) }
        else { urgency = nil }
        reason = try c.decodeIfPresent(String.self, forKey: .reason) ?? ""
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
                            Image(systemName: d.call ? "phone.fill" : "phone.down").foregroundStyle(d.call ? Theme.celadon : Theme.text3)
                            Text(d.call ? "전화함" : "전화 안 함").font(.headline).foregroundStyle(Theme.text)
                            if let u = d.urgency { Text("긴급도 \(u)").font(.footnote.weight(.semibold)).foregroundStyle(Theme.text2) }
                            Spacer()
                            Text(KST.dayAndTime(d.ts)).font(.footnote.monospacedDigit()).foregroundStyle(Theme.text3)
                        }
                        Text(d.reason).foregroundStyle(Theme.text2).textSelection(.enabled)
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
