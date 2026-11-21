import SwiftUI

/// One remembered fact (`GET /v1/memory`). Decoded loosely until the brain's shape is final.
struct MemoryItem: Identifiable, Equatable {
    var id: String
    var text: String
    var source: String?
    var personaID: String?
    var created: Date?

    init(_ d: [String: Any]) {
        id = describe(d["id"] ?? "")
        text = (d["text"] ?? d["content"] ?? d["fact"]).map(describe) ?? ""
        source = d["source"] as? String
        personaID = (d["persona_id"] ?? d["persona"]) as? String
        created = ApprovalRequest.time(d["created_at"] ?? d["created"])
    }

    /// 직접 기억 / 통화에서 / Cygnus.
    var sourceLabel: String? {
        switch source {
        case nil: return nil
        case "direct", "manual", "user", "explicit": return "직접 기억"
        case "call", "voice", "conversation", "auto": return "통화에서"
        case "cygnus": return "Cygnus"
        case let s?: return s
        }
    }

    /// The persona it belongs to, or "모든 비서" when shared.
    @MainActor var scopeLabel: String {
        guard let personaID else { return "모든 비서" }
        return PersonaModel.shared.label(for: personaID)?.name ?? personaID
    }
}

enum MemoryAPI {
    static func list() async throws -> [MemoryItem] {
        let json = try JSONSerialization.jsonObject(with: try await Gateway.send("GET", "/v1/memory"))
        let o = json as? [String: Any]
        let rows = (json as? [[String: Any]]) ?? (o?["items"] as? [[String: Any]]) ?? (o?["memories"] as? [[String: Any]]) ?? []
        return rows.map(MemoryItem.init)
    }

    static func delete(_ id: String) async throws {
        _ = try await Gateway.send("DELETE", "/v1/memory/" + (id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id))
    }
}

@MainActor
final class MemoryModel: ObservableObject {
    @Published private(set) var items: [MemoryItem] = []
    @Published private(set) var status: SectionStatus = .ok
    /// nil until the first answer; false when the gateway has no memory route (the row hides).
    @Published private(set) var available: Bool?

    func refresh() async {
        switch await attempt({ try await MemoryAPI.list() }) {
        case .success(let l): items = l.sorted { ($0.created ?? .distantPast) > ($1.created ?? .distantPast) }; status = .ok; available = true
        case .failure(let e):
            let s = SectionStatus.from(e) ?? status
            status = s
            if s == .unavailable { available = false }
        }
    }

    func delete(_ m: MemoryItem) async throws {
        try await MemoryAPI.delete(m.id)
        items.removeAll { $0.id == m.id }
    }
}

/// 설정 → 기억: what the assistant remembers, with delete.
struct MemoryScreen: View {
    @ObservedObject var model: MemoryModel
    @State private var deleting: MemoryItem?
    @State private var message: String?

    var body: some View {
        List {
            SectionNote(status: model.status) { Task { await model.refresh() } }
            if let message { Text(message).font(.footnote).foregroundStyle(Theme.danger) }
            if model.items.isEmpty && model.status == .ok { Text("아직 기억하는 게 없어요").foregroundStyle(Theme.text2) }
            ForEach(model.items) { m in
                VStack(alignment: .leading, spacing: 4) {
                    Text(m.text).foregroundStyle(Theme.text).fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: Theme.s) {
                        if let s = m.sourceLabel {
                            Text(s).font(.caption.weight(.semibold)).foregroundStyle(Theme.indigo)
                                .padding(.horizontal, 6).padding(.vertical, 1).overlay(Capsule().strokeBorder(Theme.indigo.opacity(0.6)))
                        }
                        Text([m.scopeLabel, m.created.map { $0.formatted(date: .abbreviated, time: .omitted) }].compactMap { $0 }.joined(separator: " · "))
                            .font(.footnote).foregroundStyle(Theme.text3)
                    }
                }
                .padding(.vertical, 2)
                .listRowBackground(Theme.card)
                .swipeActions { Button("지우기", role: .destructive) { deleting = m } }
                .contextMenu { Button("지우기", role: .destructive) { deleting = m } }
                .accessibilityElement(children: .combine)
                .accessibilityAction(named: "지우기") { deleting = m }
            }
        }
        .scrollContentBackground(.hidden)
        .background(Theme.bg.ignoresSafeArea())
        .navigationTitle("기억")
        .refreshable { await model.refresh() }
        .task { await model.refresh() }
        .confirmationDialog("이 기억을 지울까요?", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }),
                            titleVisibility: .visible, presenting: deleting) { m in
            Button("지우기", role: .destructive) {
                Task { do { try await model.delete(m); message = nil } catch { message = "지우지 못했어요: " + FriendlyError.describe(error).title } }
            }
            Button("취소", role: .cancel) {}
        } message: { m in Text(m.text) }
    }
}
