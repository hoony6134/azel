import SwiftUI

// MARK: - Setup

struct SetupView: View {
    var onDone: () -> Void
    @State private var voice = Config.voiceURL
    @State private var gateway = Config.gatewayURL
    @State private var token = ""

    var body: some View {
        Form {
            Section {
                TextField("음성 서버", text: $voice)
                TextField("게이트웨이", text: $gateway)
                DisclosureGroup("고급") {
                    #if os(macOS)
                    Button("Tailnet 주소 사용") { voice = Config.tailnetVoiceURL; gateway = Config.tailnetGatewayURL }
                    #endif
                    Button("공개 주소 사용") { voice = Config.publicVoiceURL; gateway = Config.publicGatewayURL }
                }
            } header: { Text("서버 주소") } footer: { Text(Config.addressNote) }
            .urlInput()
            Section {
                SecureField(Config.token == nil ? "토큰" : "저장됨 (바꿀 때만 입력)", text: $token)
            } header: { Text("토큰") } footer: { Text("토큰은 키체인에만 저장됩니다.") }
            Button("저장") {
                Config.voiceURL = voice.trimmingCharacters(in: .whitespaces)
                Config.gatewayURL = gateway.trimmingCharacters(in: .whitespaces)
                if !token.isEmpty { Config.token = token.trimmingCharacters(in: .whitespacesAndNewlines) }
                token = ""
                #if os(iOS)
                CallManager.shared.registerPush()
                #endif
                onDone()
            }
            .disabled(Config.token == nil && token.isEmpty)
        }
        .formStyle(.grouped)
        .navigationTitle("박비서 연결")
    }
}

// MARK: - Settings

private let models = [("qwen", "로컬 Qwen"), ("haiku", "Haiku"), ("sonnet", "Sonnet"), ("opus", "Opus"), ("auto", "자동")]
private let paths = [("api", "API"), ("claude_code", "Claude Code"), ("auto", "자동")]
private let tasks = [("chat", "잡담"), ("schedule", "일정"), ("code", "코드"), ("mac", "맥북 작업")]
private let triggerNames = ["deadline": "마감 임박", "server_down": "서버 장애"]

struct SettingsView: View {
    @State private var settings: AppSettings?
    @State private var message: String?
    @State private var showSetup = false

    var body: some View {
        NavigationStack {
            Group {
                if let binding = Binding($settings) { form(binding) } else { ProgressView() }
            }
            .navigationTitle("설정")
            .toolbar {
                ToolbarItem(placement: .leading) { Button("서버") { showSetup = true } }
                ToolbarItem(placement: .trailing) { Button("저장", action: save).disabled(settings == nil) }
            }
            .navigationDestination(isPresented: $showSetup) { SetupView { showSetup = false } }
            .task { await load() }
            .refreshable { await load() }
        }
    }

    private func form(_ s: Binding<AppSettings>) -> some View {
        Form {
            if let message { Text(message).font(.footnote) }
            Section("기본") {
                picker("기본 모델", models, s.default_model)
                picker("Claude 경로", paths, s.claude_path)
            }
            Section("작업별 기본값") {
                ForEach(tasks, id: \.0) { key, name in
                    let task = Binding(get: { s.wrappedValue.task_defaults[key] ?? .init(model: "auto") },
                                       set: { s.wrappedValue.task_defaults[key] = $0 })
                    picker(name, models, task.model)
                    picker("  경로", [("", "기본")] + paths,
                           Binding(get: { task.wrappedValue.path ?? "" }, set: { task.wrappedValue.path = $0.isEmpty ? nil : $0 }))
                }
            }
            Section("예산") {
                LabeledContent("월 예산 (USD)") {
                    TextField("500", value: s.monthly_budget_usd, format: .number).decimalInput().multilineTextAlignment(.trailing)
                }
                Stepper("경고: \(s.wrappedValue.budget_warn_pct)%", value: s.budget_warn_pct, in: 0...100, step: 5)
            }
            Section("먼저 전화하기") {
                DatePicker("아침 브리핑", selection: time(s.proactive.morning_briefing), displayedComponents: .hourAndMinute)
                DatePicker("방해 금지 시작", selection: time(quiet(s, 0)), displayedComponents: .hourAndMinute)
                DatePicker("방해 금지 끝", selection: time(quiet(s, 1)), displayedComponents: .hourAndMinute)
                ForEach(s.wrappedValue.proactive.triggers.keys.sorted(), id: \.self) { key in
                    Toggle(triggerNames[key] ?? key, isOn: Binding(get: { s.wrappedValue.proactive.triggers[key] ?? false },
                                                                    set: { s.wrappedValue.proactive.triggers[key] = $0 }))
                }
            }
            Section {
                TextField("기본 기기", text: s.default_device)
                TextField("대체 순서 (쉼표로 구분)", text: Binding(
                    get: { s.wrappedValue.device_fallback.joined(separator: ", ") },
                    set: { s.wrappedValue.device_fallback = $0.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty } }))
            } header: { Text("기기") } footer: { Text("기본 기기가 안 되면 대체 순서대로 시도합니다.") }
        }
        .formStyle(.grouped)
    }

    private func picker(_ title: String, _ options: [(String, String)], _ value: Binding<String>) -> some View {
        Picker(title, selection: value) { ForEach(options, id: \.0) { Text($0.1).tag($0.0) } }
    }

    private func quiet(_ s: Binding<AppSettings>, _ i: Int) -> Binding<String> {
        Binding(get: { let q = s.wrappedValue.proactive.quiet_hours; return i < q.count ? q[i] : "00:00" },
                set: { var q = s.wrappedValue.proactive.quiet_hours
                       while q.count < 2 { q.append("00:00") }
                       q[i] = $0
                       s.wrappedValue.proactive.quiet_hours = q })
    }

    /// "HH:mm" string ⇄ Date for DatePicker.
    private func time(_ s: Binding<String>) -> Binding<Date> {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "HH:mm"
        return Binding(get: { f.date(from: s.wrappedValue) ?? Date() }, set: { s.wrappedValue = f.string(from: $0) })
    }

    private func load() async {
        do { settings = try await Gateway.get("/v1/settings"); message = nil } catch { message = "불러오기 실패: \(error.localizedDescription)" }
    }

    private func save() {
        guard let settings else { return }
        Task {
            do { try await Gateway.saveSettings(settings); message = "저장했습니다" } catch { message = "저장 실패: \(error.localizedDescription)" }
        }
    }
}

// MARK: - Usage

struct UsageView: View {
    @State private var usage: [String: Any] = [:]
    @State private var message: String?

    var body: some View {
        NavigationStack {
            List {
                if let message { Text(message).foregroundStyle(.red) }
                LabeledContent("월", value: describe(usage["month"] ?? "-"))
                LabeledContent("비용 (USD)", value: describe(usage["cost_usd"] ?? "-"))
                group("모델별", usage["by_model"])
                group("경로별", usage["by_path"])
            }
            .navigationTitle("사용량")
            .task { await load() }
            .refreshable { await load() }
        }
    }

    @ViewBuilder private func group(_ title: String, _ value: Any?) -> some View {
        if let dict = value as? [String: Any] {
            Section(title) {
                ForEach(dict.keys.sorted(), id: \.self) { LabeledContent($0, value: describe(dict[$0]!)) }
            }
        }
    }

    private func load() async {
        do { usage = try await Gateway.getJSON("/v1/usage") as? [String: Any] ?? [:]; message = nil }
        catch { message = error.localizedDescription }
    }
}

/// Numbers and strings as-is; nested objects as compact JSON (the contract leaves their shape open).
func describe(_ value: Any) -> String {
    if let s = value as? String { return s }
    if let n = value as? NSNumber { return n.stringValue }
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) { return String(decoding: data, as: UTF8.self) }
    return "\(value)"
}

// MARK: - Status

struct StatusView: View {
    @State private var status: ServerStatus?
    @State private var message: String?

    var body: some View {
        NavigationStack {
            List {
                if let message { Text(message).foregroundStyle(.red) }
                if let status {
                    row("스파크", status.spark.ok, status.spark.ms)
                    row("게이트웨이", status.gateway.ok, status.gateway.ms)
                    Section("기기") { ForEach(status.devices, id: \.name) { row($0.name, $0.ok, $0.ms) } }
                }
            }
            .navigationTitle("상태")
            .task { await load() }
            .refreshable { await load() }
        }
    }

    private func row(_ name: String, _ ok: Bool, _ ms: Double?) -> some View {
        LabeledContent {
            Text(ms.map { "\(Int($0)) ms" } ?? "-")
        } label: {
            Label(name, systemImage: ok ? "checkmark.circle.fill" : "xmark.octagon.fill").foregroundStyle(ok ? .green : .red)
        }
        .accessibilityLabel("\(name) \(ok ? "정상" : "장애")")
    }

    private func load() async {
        do { status = try await Gateway.get("/v1/status"); message = nil } catch { message = error.localizedDescription }
    }
}

// MARK: - Approvals

struct ApprovalsView: View {
    @Binding var pending: Int
    @State private var items: [[String: Any]] = []
    @State private var message: String?

    var body: some View {
        NavigationStack {
            List {
                if let message { Text(message).foregroundStyle(.red) }
                if items.isEmpty { Text("대기 중인 승인이 없습니다").foregroundStyle(.secondary) }
                ForEach(items.indices, id: \.self) { i in
                    let item = items[i]
                    let id = describe(item["id"] ?? "")
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(item.keys.filter { $0 != "id" }.sorted(), id: \.self) { key in
                            Text("\(key): \(describe(item[key]!))").font(.callout)
                        }
                        HStack {
                            Button("승인") { decide(id, true) }.buttonStyle(.borderedProminent)
                            Button("거절", role: .destructive) { decide(id, false) }.buttonStyle(.bordered)
                        }
                    }
                }
            }
            .navigationTitle("승인 대기")
            .task { await load() }
            .refreshable { await load() }
        }
    }

    private func load() async {
        do {
            items = try await Gateway.approvals()
            pending = items.count
            message = nil
        } catch { message = error.localizedDescription }
    }

    private func decide(_ id: String, _ approve: Bool) {
        Task {
            let path = "/v1/approvals/" + (id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id)
            do { _ = try await Gateway.send("POST", path, json: ["approve": approve]) } catch { message = error.localizedDescription }
            await load()
        }
    }
}

// MARK: - Platform shims

private extension ToolbarItemPlacement {
    #if os(iOS)
    static let leading = topBarLeading, trailing = topBarTrailing
    #else
    static let leading = navigation, trailing = primaryAction
    #endif
}

private extension View {
    func urlInput() -> some View {
        #if os(iOS)
        textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
        #else
        autocorrectionDisabled()
        #endif
    }

    func decimalInput() -> some View {
        #if os(iOS)
        keyboardType(.decimalPad)
        #else
        self
        #endif
    }
}
