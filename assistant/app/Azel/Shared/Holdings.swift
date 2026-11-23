import SwiftUI

/// One entry of the `holdings` setting (the portfolio the voice tools and the TV read): "이름 (CODE)", or
/// a bare name for an unlisted holding. Renaming keeps the code; only an explicit "(CODE)" changes it.
struct Holding: Equatable, Identifiable {
    var name: String
    var code: String?
    var id: String { entry }

    var entry: String { code.map { "\(name) (\($0))" } ?? name }

    init(name: String, code: String?) { self.name = name; self.code = code }

    /// "SOS랩 (464080)" → name SOS랩, code 464080. Spaces collapse as the gateway does.
    init(entry: String) {
        let s = entry.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        if let m = s.firstMatch(of: /^(.*?)\s*\(([^()]+)\)$/), !m.1.isEmpty {
            self.init(name: String(m.1), code: String(m.2).trimmingCharacters(in: .whitespaces))
        } else {
            self.init(name: s, code: nil)
        }
    }

    /// The edit field's text as a new entry for this row: a plain name keeps the old code (a rename);
    /// "이름 (CODE)" sets the code explicitly. nil = nothing usable.
    func renamed(_ text: String) -> Holding? {
        let h = Holding(entry: text)
        guard !h.name.isEmpty else { return nil }
        return h.code == nil ? Holding(name: h.name, code: code) : h
    }

    static let maxCount = 12, maxLength = 40

    /// The gateway's own limits (PUT /v1/settings): at most 12, each 1–40 characters.
    static func problem(_ list: [Holding]) -> String? {
        if list.count > maxCount { return "보유 종목은 \(maxCount)개까지 둘 수 있어요" }
        if let long = list.first(where: { $0.entry.count > maxLength }) { return "‘\(long.name)’ 이름이 너무 길어요 (코드 포함 \(maxLength)자까지)" }
        return nil
    }
}

/// Reads and edits `holdings` (user, 2026-11-23: rename 'SOS랩' → '에스오에스랩' by voice made a duplicate or
/// nothing, and the app had no editor). Rename, remove and reorder write the whole list with
/// `PUT /v1/settings`, codes kept. Adding a name without a code goes through `POST /v1/holdings`, where
/// the gateway resolves the ticker; an older gateway without it gets the bare name.
/// Every change shows at once and rolls back with a message if the save fails.
@MainActor
final class HoldingsModel: ObservableObject {
    @Published private(set) var items: [Holding] = []
    @Published private(set) var loaded = false
    @Published var error: String?
    /// The gateway found several listings for a name: pick one (then it is added with that code).
    @Published var choice: (name: String, options: [String])?
    @Published private(set) var adding = false

    func load() async {
        do {
            let obj = try JSONSerialization.jsonObject(with: try await Gateway.send("GET", "/v1/settings")) as? [String: Any]
            items = ((obj?["holdings"] as? [String]) ?? []).map(Holding.init(entry:))
            loaded = true
            error = nil
        } catch { self.error = FriendlyError.describe(error).title }
    }

    func rename(_ h: Holding, to text: String) async {
        guard let i = items.firstIndex(of: h), let new = h.renamed(text), new != h else { return }
        var next = items
        next[i] = new
        await save(next)
    }

    func remove(_ h: Holding) async { await save(items.filter { $0 != h }) }

    func move(from: IndexSet, to: Int) async {
        var next = items
        next.move(fromOffsets: from, toOffset: to)
        await save(next)
    }

    /// "이름 (CODE)" is stored as typed; a bare name is resolved by the gateway.
    func add(_ text: String) async {
        let h = Holding(entry: text)
        guard !h.name.isEmpty else { return }
        if h.code != nil { return await save(items.filter { Self.key($0) != Self.key(h) } + [h]) }
        let before = items
        items.append(h)                      // optimistic: the bare name, the code arrives with the answer
        adding = true
        defer { adding = false }
        do {
            let data = try await Gateway.send("POST", "/v1/holdings", json: ["add": [h.name], "remove": [String]()])
            apply(added: h.name, response: try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:], before: before)
        } catch GatewayError.http(404) {
            items = before                   // an older gateway: no resolver, store the bare name (비상장)
            await save(before.filter { Self.key($0) != Self.key(h) } + [h])
        } catch {
            items = before
            self.error = "추가하지 못했어요: \(FriendlyError.describe(error).title)"
        }
    }

    func apply(added name: String, response: [String: Any], before: [Holding]) {
        if let list = response["holdings"] as? [String] { items = list.map(Holding.init(entry:)) } else { items = before }
        if let options = (response["candidates"] as? [String: Any])?[name] as? [String], !options.isEmpty {
            choice = (name, options)
        }
    }

    private func save(_ next: [Holding]) async {
        if let p = Holding.problem(next) { error = p; return }
        let before = items
        items = next
        do {
            _ = try await Gateway.send("PUT", "/v1/settings", json: ["holdings": next.map(\.entry)])
            error = nil
        } catch {
            items = before
            self.error = "저장하지 못했어요: \(FriendlyError.describe(error).title)"
        }
    }

    /// What the gateway compares entries by: the name without the code, spaces and case ignored.
    nonisolated static func key(_ h: Holding) -> String { h.name.filter { !$0.isWhitespace }.lowercased() }
}

struct HoldingsView: View {
    @ObservedObject var model: HoldingsModel
    @State private var draft = ""
    @State private var editing: Holding?
    @State private var editText = ""

    var body: some View {
        List {
            Section {
                ForEach(model.items) { h in
                    Button { editing = h; editText = h.name } label: {
                        HStack {
                            Text(h.name).foregroundStyle(Theme.text)
                            Spacer()
                            Text(h.code ?? "비상장").font(.subheadline.monospacedDigit()).foregroundStyle(Theme.text3)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint("이름 바꾸기")
                    .contextMenu { Button("삭제", role: .destructive) { Task { await model.remove(h) } } }
                }
                .onDelete { idx in
                    let gone = idx.map { model.items[$0] }
                    Task { for h in gone { await model.remove(h) } }
                }
                .onMove { from, to in Task { await model.move(from: from, to: to) } }
            } footer: {
                Text("이름만 바꾸면 종목 코드는 그대로예요. 코드를 바꾸려면 ‘이름 (코드)’로 적어 주세요.")
            }
            Section {
                HStack {
                    TextField("종목 이름 또는 ‘이름 (코드)’", text: $draft)
                        .onSubmit(add)
                    if model.adding { ProgressView().controlSize(.small) }
                    Button("추가", action: add).disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty || model.items.count >= Holding.maxCount)
                }
            } footer: {
                Text("코드 없이 적으면 서버가 종목 코드를 찾아요 · 최대 \(Holding.maxCount)개")
            }
        }
        .navigationTitle("보유 종목")
        .overlay {
            if !model.loaded && model.error == nil { ProgressView() }
        }
        .safeAreaInset(edge: .bottom) {
            if let e = model.error {
                Text(e).font(.footnote).foregroundStyle(Theme.danger).padding(.vertical, Theme.s)
                    .frame(maxWidth: .infinity).background(.bar)
                    .onTapGesture { model.error = nil }
            }
        }
        .task { await model.load() }
        .alert("이름 바꾸기", isPresented: Binding(get: { editing != nil }, set: { if !$0 { editing = nil } })) {
            TextField("이름", text: $editText)
            Button("저장") { if let h = editing { Task { await model.rename(h, to: editText) } }; editing = nil }
            Button("취소", role: .cancel) { editing = nil }
        } message: {
            if let c = editing?.code { Text("종목 코드 \(c)는 그대로 둬요") }
        }
        .confirmationDialog("어느 종목인가요?", isPresented: Binding(get: { model.choice != nil }, set: { if !$0 { model.choice = nil } }),
                            presenting: model.choice) { c in
            // The user's own name with the picked listing's code.
            ForEach(c.options, id: \.self) { o in Button(o) { Task { await model.add("\(c.name) (\(Holding(entry: o).code ?? o))") } } }
        }
    }

    private func add() {
        let text = draft
        draft = ""
        Task { await model.add(text) }
    }
}
