import SwiftUI

/// `GET/PUT /v1/connectors`: claude.ai connectors (toggle only) and custom MCP servers.
struct ConnectorSet: Codable, Equatable {
    struct ClaudeAI: Codable, Equatable, Identifiable {
        var id: String { name }
        var name: String
        var enabled: Bool
        var status: String?             // connected | needs_auth (read-only)

        private enum Keys: String, CodingKey { case name, enabled, status }
        init(name: String, enabled: Bool, status: String? = nil) { self.name = name; self.enabled = enabled; self.status = status }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: Keys.self)
            name = try c.decode(String.self, forKey: .name)
            enabled = try c.decodeIfPresent(Bool.self, forKey: .enabled) ?? false
            status = try c.decodeIfPresent(String.self, forKey: .status)
        }
        /// PUT leaves out the read-only status.
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: Keys.self)
            try c.encode(name, forKey: .name)
            try c.encode(enabled, forKey: .enabled)
        }
    }

    struct Custom: Codable, Equatable, Identifiable {
        var id: String { name }
        var name: String
        var url: String
        var transport: String           // http | sse
        var enabled: Bool
        var has_secret: Bool?           // read-only

        private enum Keys: String, CodingKey { case name, url, transport, enabled, has_secret }
        init(name: String, url: String, transport: String, enabled: Bool = true, has_secret: Bool? = nil) {
            self.name = name; self.url = url; self.transport = transport; self.enabled = enabled; self.has_secret = has_secret
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: Keys.self)
            name = try c.decode(String.self, forKey: .name)
            url = try c.decodeIfPresent(String.self, forKey: .url) ?? ""
            transport = try c.decodeIfPresent(String.self, forKey: .transport) ?? "http"
            enabled = try c.decodeIfPresent(Bool.self, forKey: .enabled) ?? false
            has_secret = try c.decodeIfPresent(Bool.self, forKey: .has_secret)
        }
        /// PUT leaves out the read-only has_secret.
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: Keys.self)
            try c.encode(name, forKey: .name)
            try c.encode(url, forKey: .url)
            try c.encode(transport, forKey: .transport)
            try c.encode(enabled, forKey: .enabled)
        }
    }

    var claude_ai: [ClaudeAI]
    var custom: [Custom]

    private enum Keys: String, CodingKey { case claude_ai, custom }
    init(claude_ai: [ClaudeAI] = [], custom: [Custom] = []) { self.claude_ai = claude_ai; self.custom = custom }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        claude_ai = try c.decodeIfPresent([ClaudeAI].self, forKey: .claude_ai) ?? []
        custom = try c.decodeIfPresent([Custom].self, forKey: .custom) ?? []
    }

    /// Server names: letters, digits, - and _ (they become a path segment for the secret).
    static func validName(_ s: String) -> Bool {
        !s.isEmpty && s.count <= 40 && s.allSatisfy { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }
    }

    static func validURL(_ s: String) -> Bool {
        guard let u = URL(string: s), let scheme = u.scheme, u.host != nil else { return false }
        return scheme == "https" || scheme == "http"
    }
}

enum ConnectorAPI {
    static func get() async throws -> ConnectorSet {
        try JSONDecoder().decode(ConnectorSet.self, from: try await Gateway.send("GET", "/v1/connectors"))
    }

    static func put(_ set: ConnectorSet) async throws {
        _ = try await Gateway.send("PUT", "/v1/connectors", json: try JSONEncoder().encode(set))
    }

    /// Write-only: the secret goes to the gateway once and is never kept on this device.
    static func putSecret(name: String, value: String) async throws {
        let path = "/v1/connectors/secret/" + (name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name)
        _ = try await Gateway.send("PUT", path, json: ["value": value])
    }
}

@MainActor
final class ConnectorModel: ObservableObject {
    @Published private(set) var set = ConnectorSet()
    @Published private(set) var status: SectionStatus = .ok
    @Published private(set) var saving = false
    @Published var message: String?

    func refresh() async {
        switch await attempt({ try await ConnectorAPI.get() }) {
        case .success(let s): set = s; status = .ok
        case .failure(let e): status = SectionStatus.from(e) ?? status
        }
    }

    /// Applies a change and PUTs the whole set; rolls back on failure.
    func update(_ change: (inout ConnectorSet) -> Void) async {
        let before = set
        change(&set)
        saving = true
        defer { saving = false }
        do { try await ConnectorAPI.put(set); message = nil } catch {
            set = before
            message = "저장하지 못했어요: " + FriendlyError.describe(error).title
        }
    }

    /// Adds a custom server, then sends its secret (if any) to the write-only endpoint.
    /// The secret lives only in this call's argument.
    func addCustom(_ server: ConnectorSet.Custom, secret: String?) async -> Bool {
        let before = set
        set.custom.append(server)
        do {
            try await ConnectorAPI.put(set)
            if let secret, !secret.isEmpty { try await ConnectorAPI.putSecret(name: server.name, value: secret) }
            message = nil
            await refresh()
            return true
        } catch {
            set = before
            message = "추가하지 못했어요: " + FriendlyError.describe(error).title
            return false
        }
    }
}

// MARK: - Screen

struct ConnectorsView: View {
    @StateObject private var model = ConnectorModel()
    @Environment(\.dismiss) private var dismiss
    @State private var adding = false

    var body: some View {
        ScrollView {
            VStack(spacing: Theme.l) {
                Text("커넥터가 적을수록 Claude가 더 빨리 답해요.").font(.footnote).foregroundStyle(Theme.text3)
                    .frame(maxWidth: .infinity, alignment: .leading)
                SectionNote(status: model.status) { Task { await model.refresh() } }
                if let m = model.message { Text(m).font(.footnote).foregroundStyle(Theme.danger) }
                Card("Claude.ai 커넥터") {
                    if model.set.claude_ai.isEmpty { Text("찾은 커넥터가 없어요").foregroundStyle(Theme.text3) }
                    ForEach(model.set.claude_ai) { c in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(c.name).foregroundStyle(Theme.text)
                                Text(c.status == "needs_auth" ? "인증 필요" : "연결됨").font(.footnote)
                                    .foregroundStyle(c.status == "needs_auth" ? Theme.amber : Theme.celadon)
                            }
                            Spacer()
                            Toggle("", isOn: Binding(get: { c.enabled }, set: { on in
                                Task { await model.update { s in if let i = s.claude_ai.firstIndex(where: { $0.name == c.name }) { s.claude_ai[i].enabled = on } } }
                            }))
                            .labelsHidden().tint(Theme.celadon)
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
                Card("직접 추가한 MCP 서버", trailing: AnyView(Button("추가") { adding = true }.font(.subheadline).tint(Theme.celadon))) {
                    if model.set.custom.isEmpty { Text("추가한 서버가 없어요").foregroundStyle(Theme.text3) }
                    ForEach(model.set.custom) { c in
                        HStack(alignment: .top) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(c.name).foregroundStyle(Theme.text)
                                Text(c.url).font(.footnote.monospaced()).foregroundStyle(Theme.text2).lineLimit(1)
                                Text([c.transport.uppercased(), c.has_secret == true ? "비밀값 있음" : nil].compactMap { $0 }.joined(separator: " · "))
                                    .font(.footnote).foregroundStyle(Theme.text3)
                            }
                            Spacer()
                            Toggle("", isOn: Binding(get: { c.enabled }, set: { on in
                                Task { await model.update { s in if let i = s.custom.firstIndex(where: { $0.name == c.name }) { s.custom[i].enabled = on } } }
                            }))
                            .labelsHidden().tint(Theme.celadon)
                            Button(role: .destructive) {
                                Task { await model.update { s in s.custom.removeAll { $0.name == c.name } } }
                            } label: { Image(systemName: "trash") }
                            .buttonStyle(.borderless).accessibilityLabel("\(c.name) 삭제")
                        }
                    }
                }
            }
            .padding(20)
            .frame(maxWidth: 640).frame(maxWidth: .infinity)
        }
        .background(Theme.bg.ignoresSafeArea())
        .navigationTitle("커넥터")
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("닫기") { dismiss() } } }
        .task { await model.refresh() }
        .refreshable { await model.refresh() }
        .disabled(model.saving)
        .sheet(isPresented: $adding) {
            NavigationStack { AddServerView(model: model) }
                #if os(macOS)
                .frame(width: 460, height: 440)
                #endif
        }
    }
}

private struct AddServerView: View {
    @ObservedObject var model: ConnectorModel
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var url = "https://"
    @State private var transport = "http"
    @State private var secret = ""          // cleared as soon as it is sent; never stored
    @State private var working = false

    private var valid: Bool { ConnectorSet.validName(name) && ConnectorSet.validURL(url) && !model.set.custom.contains { $0.name == name } }

    var body: some View {
        Form {
            TextField("이름 (영문, 숫자, -, _)", text: $name).urlInput()
            TextField("URL", text: $url).urlInput()
            Picker("방식", selection: $transport) { Text("HTTP").tag("http"); Text("SSE").tag("sse") }.pickerStyle(.segmented)
            SecureField("인증 헤더나 토큰 (선택)", text: $secret)
            Text("비밀값은 게이트웨이로 한 번만 보내고 이 기기에는 남기지 않아요. 다시 볼 수 없어요.").font(.footnote).foregroundStyle(Theme.text3)
            if let m = model.message { Text(m).font(.footnote).foregroundStyle(Theme.danger) }
        }
        .formStyle(.grouped)
        .navigationTitle("MCP 서버 추가")
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("취소") { secret = ""; dismiss() } }
            ToolbarItem(placement: .confirmationAction) {
                Button(working ? "추가 중…" : "추가") {
                    let value = secret
                    secret = ""
                    working = true
                    Task {
                        let ok = await model.addCustom(.init(name: name, url: url.trimmingCharacters(in: .whitespaces), transport: transport),
                                                       secret: value.trimmingCharacters(in: .whitespacesAndNewlines))
                        working = false
                        if ok { dismiss() }
                    }
                }
                .disabled(!valid || working)
            }
        }
        .onDisappear { secret = "" }
    }
}
