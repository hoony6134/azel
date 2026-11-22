import SwiftUI

/// One remembered fact (`GET /v1/memories`, brain batch 5):
/// {id, text, source: explicit | auto, about, persona (nil = shared), in_cygnus, created}.
struct MemoryItem: Identifiable, Equatable {
    var id: String
    var text: String
    var source: String?
    var about: String?
    var personaID: String?
    var inCygnus: Bool
    var created: Date?

    init(_ d: [String: Any]) {
        id = describe(d["id"] ?? "")
        text = (d["text"]).map(describe) ?? ""
        source = d["source"] as? String
        about = (d["about"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        personaID = d["persona"] as? String
        inCygnus = (d["in_cygnus"] as? Bool) ?? false
        created = ApprovalRequest.time(d["created"])
    }

    /// 직접 기억 ("기억해" from the user) / 통화에서 (picked up automatically).
    var sourceLabel: String? {
        switch source {
        case "explicit": return "직접 기억"
        case "auto": return "통화에서"
        default: return nil
        }
    }

    /// The persona it belongs to, or "모든 비서" when shared.
    @MainActor var scopeLabel: String {
        guard let personaID else { return "모든 비서" }
        return PersonaModel.shared.label(for: personaID)?.name ?? personaID
    }
}

enum MemoryAPI {
    /// Newest first, no paging (the server caps `limit` at 200). Without `X-Azel-Result: 2` the
    /// answer is the bare list.
    static func list() async throws -> [MemoryItem] {
        let json = try JSONSerialization.jsonObject(with: try await Gateway.send("GET", "/v1/memories?limit=200"))
        return ((json as? [[String: Any]]) ?? ((json as? [String: Any])?["data"] as? [[String: Any]]) ?? []).map(MemoryItem.init)
    }

    /// 200 with the action result; 404 for an unknown id. The app's confirm dialog is the confirmation.
    static func delete(_ id: String) async throws {
        _ = try await Gateway.send("DELETE", "/v1/memories/" + (id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id))
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
            guard let s = SectionStatus.from(e) else { return }   // cancelled
            status = s
            // Any failure before a first success hides the row (the memory service may not have
            // the batch-5 routes yet); after one, the screen shows the error with 다시 시도.
            if available != true { available = false }
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
                        if m.inCygnus {
                            Text("Cygnus").font(.caption.weight(.semibold)).foregroundStyle(Theme.text2)
                                .padding(.horizontal, 6).padding(.vertical, 1).overlay(Capsule().strokeBorder(Theme.stroke))
                        }
                        Text([m.about, m.scopeLabel, m.created.map { $0.formatted(date: .abbreviated, time: .omitted) }].compactMap { $0 }.joined(separator: " · "))
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
