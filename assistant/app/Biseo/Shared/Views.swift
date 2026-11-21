import AVFoundation
import SwiftUI

// MARK: - First-run setup (DESIGN §9)

/// 환영 → 연결 (with a live check) → 권한. `startAt: 1` reopens only the connection step from settings.
struct SetupFlow: View {
    var startAt = 0
    var onDone: () -> Void
    @State private var step = 0
    @State private var voice = Config.voiceURL
    @State private var gateway = Config.gatewayURL
    @State private var token = ""
    @State private var checks: [Check] = []
    @State private var checking = false
    @State private var micGranted = SetupFlow.micAuthorized

    struct Check: Identifiable { let id: String; let ok: Bool; let reason: String? }

    var body: some View {
        VStack(spacing: 0) {
            Group {
                switch step {
                case 0: welcome
                case 1: connection
                default: permissions
                }
            }
            .frame(maxWidth: 520, maxHeight: .infinity)
            .padding(.horizontal, 20)
            if startAt == 0 {
                HStack(spacing: Theme.s) {
                    ForEach(0..<3) { Circle().fill($0 == step ? Theme.celadon : Theme.stroke).frame(width: 7, height: 7) }
                }
                .padding(.bottom, Theme.l)
                .accessibilityHidden(true)
            }
        }
        .frame(maxWidth: .infinity)
        .background(Theme.bg.ignoresSafeArea())
        .onAppear { step = startAt }
        .animation(Theme.enter, value: step)
    }

    private var welcome: some View {
        VStack(spacing: Theme.xl) {
            Spacer()
            MoonJar(state: .idle, levels: .silent).frame(width: 220, height: 220)
            Text("Azel").font(.largeTitle.weight(.semibold)).foregroundStyle(Theme.text)
            Text("\(AssistantName.current)에게 전화하듯 말을 걸면 돼요.").font(.title3).foregroundStyle(Theme.text2)
            Spacer()
            primary("시작하기") { step = 1 }
        }
    }

    private var connection: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.l) {
                Text("연결").font(.title.weight(.semibold)).foregroundStyle(Theme.text).padding(.top, Theme.xl)
                Card("토큰") {
                    HStack {
                        SecureField(Config.token == nil ? "토큰" : "저장됨 (바꿀 때만 입력)", text: $token)
                            .textFieldStyle(.roundedBorder)
                            .onChange(of: token) { let clean = Config.cleanToken(token); if clean != token { token = clean } }
                        Button("붙여넣기") { if let s = SetupFlow.pasteboardString() { token = Config.cleanToken(s) } }
                            .buttonStyle(.bordered).tint(Theme.celadon)
                    }
                    Text("키체인에만 저장돼요").font(.footnote).foregroundStyle(Theme.text3)
                }
                Card("서버") {
                    Text(voice == Config.publicVoiceURL && gateway == Config.publicGatewayURL ? "azel.scian.xyz (기본)" : "\(voice)\n\(gateway)")
                        .font(.footnote.monospaced()).foregroundStyle(Theme.text2)
                    DisclosureGroup("고급") {
                        VStack(alignment: .leading, spacing: Theme.s) {
                            field("음성 서버", $voice)
                            field("게이트웨이", $gateway)
                            Button("공개 주소 사용") { voice = Config.publicVoiceURL; gateway = Config.publicGatewayURL }
                        }
                        .padding(.top, Theme.s)
                    }
                    .tint(Theme.celadon)
                    Text(Config.addressNote).font(.footnote).foregroundStyle(Theme.text3)
                }
                if !checks.isEmpty {
                    Card("연결 확인") {
                        ForEach(checks) { c in
                            HStack(alignment: .firstTextBaseline) {
                                Image(systemName: c.ok ? "checkmark.circle.fill" : "xmark.circle.fill").foregroundStyle(c.ok ? Theme.celadon : Theme.danger)
                                Text(c.id).foregroundStyle(Theme.text)
                                if let r = c.reason { Text(r).font(.footnote).foregroundStyle(Theme.text2) }
                            }
                            .accessibilityElement(children: .combine)
                        }
                    }
                }
                HStack {
                    Button(checking ? "확인 중…" : "연결 확인") { Task { await check() } }
                        .buttonStyle(.bordered).tint(Theme.celadon).disabled(checking || (Config.token == nil && token.isEmpty))
                    Spacer()
                    Button(startAt == 1 ? "완료" : "다음") { startAt == 1 ? onDone() : (step = 2) }
                        .buttonStyle(.borderedProminent).tint(Theme.celadon).foregroundStyle(Theme.onAccent)
                        .disabled(checks.isEmpty || checks.contains { !$0.ok })
                }
            }
        }
    }

    private var permissions: some View {
        VStack(alignment: .leading, spacing: Theme.l) {
            Text("권한").font(.title.weight(.semibold)).foregroundStyle(Theme.text).padding(.top, Theme.xl)
            Card {
                HStack {
                    Image(systemName: "mic.fill").foregroundStyle(Theme.celadon).frame(width: 28)
                    VStack(alignment: .leading) {
                        Text("마이크").foregroundStyle(Theme.text)
                        Text("말을 들으려면 필요해요").font(.subheadline).foregroundStyle(Theme.text2)
                    }
                    Spacer()
                    if micGranted { Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.celadon).accessibilityLabel("허용됨") }
                    else { Button("허용") { Task { micGranted = await SetupFlow.requestMic() } }.buttonStyle(.bordered).tint(Theme.celadon) }
                }
                #if os(iOS)
                Text("\(AssistantName.josa(AssistantName.current, "이", "가")) 먼저 거는 전화는 VoIP 푸시로 와서 따로 허용할 필요가 없어요.").font(.footnote).foregroundStyle(Theme.text3)
                #else
                Text("전역 단축키(⌥Space)는 손쉬운 사용 권한 없이 동작해요.").font(.footnote).foregroundStyle(Theme.text3)
                #endif
            }
            Spacer()
            primary("완료", action: onDone)
        }
    }

    private func check() async {
        checking = true
        defer { checking = false }
        let v = voice.trimmingCharacters(in: .whitespaces), g = gateway.trimmingCharacters(in: .whitespaces)
        guard Config.isTLS(v), Config.isTLS(g) else {
            checks = [Check(id: "서버 주소", ok: false, reason: "wss://, https:// 주소만 쓸 수 있어요")]
            return
        }
        Config.voiceURL = v
        Config.gatewayURL = g
        if !token.isEmpty { Config.token = Config.cleanToken(token) }
        token = ""
        #if os(iOS)
        CallManager.shared.registerPush()
        #endif
        do {
            let s: ServerStatus = try await Gateway.get("/v1/status")
            checks = [Check(id: "게이트웨이", ok: true, reason: nil), Check(id: "토큰", ok: true, reason: nil),
                      Check(id: "spark", ok: s.spark.ok, reason: s.spark.ok ? nil : "음성 서버가 응답하지 않아요")]
        } catch GatewayError.http(let code) where code == 401 || code == 403 {
            checks = [Check(id: "게이트웨이", ok: true, reason: nil), Check(id: "토큰", ok: false, reason: "토큰이 맞지 않아요")]
        } catch {
            checks = [Check(id: "게이트웨이", ok: false, reason: error.localizedDescription)]
        }
    }

    private func field(_ title: String, _ text: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: Theme.xs) {
            Text(title).font(.subheadline).foregroundStyle(Theme.text2)
            TextField(title, text: text).textFieldStyle(.roundedBorder).urlInput().font(.body.monospaced())
        }
    }

    private func primary(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.headline).frame(maxWidth: .infinity).frame(height: 52)
                .foregroundStyle(Theme.onAccent)
                .background(Theme.celadon, in: RoundedRectangle(cornerRadius: Theme.callButtonRadius, style: .continuous))
        }
        .buttonStyle(.plain)
        .padding(.bottom, Theme.l)
    }

    static func pasteboardString() -> String? {
        #if os(iOS)
        UIPasteboard.general.string
        #else
        NSPasteboard.general.string(forType: .string)
        #endif
    }

    static var micAuthorized: Bool {
        #if os(iOS)
        AVAudioApplication.shared.recordPermission == .granted
        #else
        AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
        #endif
    }

    static func requestMic() async -> Bool {
        #if os(iOS)
        await AVAudioApplication.requestRecordPermission()
        #else
        await AVCaptureDevice.requestAccess(for: .audio)
        #endif
    }
}

// MARK: - Settings (DESIGN §7.5): usage + status + settings as cards, auto-saved

private let models = [("qwen", "로컬 Qwen"), ("haiku", "Haiku"), ("sonnet", "Sonnet"), ("opus", "Opus"), ("auto", "자동")]
private let paths = [("api", "API"), ("claude_code", "Claude Code"), ("auto", "자동")]
private let tasks = [("chat", "잡담"), ("schedule", "일정"), ("code", "코드"), ("mac", "macbook 작업")]

struct SettingsScreen<Extra: View>: View {
    @ObservedObject var model: AppModel
    @ObservedObject var history: HistoryModel
    @ViewBuilder var extra: Extra
    @State private var settings: AppSettings?
    @State private var loaded: AppSettings?
    @State private var usage: [String: Any] = [:]
    @State private var loadError: Error?
    @State private var usageStatus: SectionStatus = .ok
    /// Server's default personality text (read-only, never sent back), shown as the placeholder.
    @State private var personalityDefault: String?
    @State private var saveState: SaveState = .idle
    @State private var saveTask: Task<Void, Never>?
    @State private var showSetup = false
    @State private var showRecordings = false
    @State private var showPaths = false
    @State private var pendingDays: Int?
    @State private var showProactiveLog = false
    @ObservedObject private var personas = PersonaModel.shared
    @State private var showConnectors = false
    @State private var nameDraft = AssistantName.current
    @State private var nameError: String?
    @AppStorage("workSounds") private var workSounds = true
    /// Cached so the call screen can show the tap-to-interrupt hint without a fetch.
    @AppStorage("noiseMode") private var noiseMode = "normal"

    enum SaveState: Equatable { case idle, saving, saved, failed(String) }

    var body: some View {
        ScrollView {
            LazyVStack(spacing: Theme.l) {
                if let loadError, settings == nil {
                    let f = FriendlyError.describe(loadError)
                    EmptyState(title: f.title, message: f.message, detail: loadError.localizedDescription, action: ("다시 시도", { Task { await load() } }))
                }
                if let s = Binding($settings) { assistantCard(s) }
                // The wake listener runs only on the Mac, so enrollment lives there.
                #if os(macOS)
                Card("호출어") { WakeEnrollRow() }
                #else
                Card("호출어") { Text("호출어는 Mac에서 등록해요").foregroundStyle(Theme.text2) }
                #endif
                Card("커넥터") {
                    Button { showConnectors = true } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Claude가 쓸 커넥터").foregroundStyle(Theme.text)
                                Text("적을수록 답이 빨라요").font(.footnote).foregroundStyle(Theme.text3)
                            }
                            Spacer()
                            Image(systemName: "chevron.right").foregroundStyle(Theme.text3)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .sheet(isPresented: $showConnectors) {
                        NavigationStack { ConnectorsView() }
                            #if os(macOS)
                            .frame(width: 560, height: 640)
                            #endif
                    }
                }
                usageCard
                statusCard
                if let s = Binding($settings) {
                    modelCard(s)
                    taskCard(s)
                    budgetCard(s)
                    proactiveCard(s)
                    deviceCard(s)
                }
                connectionCard
                if let s = Binding($settings) { historyCard(s) }
                if !Recordings.list().isEmpty { recordingsCard }
                extra
            }
            .padding(.horizontal, 20)
            .padding(.vertical, Theme.l)
            .frame(maxWidth: 640)
            .frame(maxWidth: .infinity)
        }
        .background(Theme.bg.ignoresSafeArea())
        .navigationTitle("설정")
        .task { await load() }
        .refreshable { await load() }
        .onChange(of: settings) { _, new in scheduleSave(new) }
        .sheet(isPresented: $showSetup) {
            SetupFlow(startAt: 1) { showSetup = false; Task { await load() } }
                #if os(macOS)
                .frame(width: 520, height: 620)
                #endif
        }
        .sheet(isPresented: $showRecordings) {
            NavigationStack {
                RecordingsView().toolbar { ToolbarItem(placement: .confirmationAction) { Button("닫기") { showRecordings = false } } }
            }
            #if os(macOS)
            .frame(width: 520, height: 520)
            #endif
        }
    }

    // 비서 (DESIGN §7.5 card 1)
    /// §17.4: with personas, the top card lists them (names and personalities are edited per
    /// persona); an older gateway without /v1/personas keeps the single name and 성격 fields.
    @ViewBuilder private func assistantCard(_ s: Binding<AppSettings>) -> some View {
        if personas.status == .unavailable || personas.personas.isEmpty {
            legacyAssistantCard(s)
        } else {
            PersonaListCard(model: personas)
        }
        Card("대화") {
            row("답변 길이") {
                Picker("답변 길이", selection: Binding(get: { s.wrappedValue.reply_length ?? "normal" },
                                                   set: { s.wrappedValue.reply_length = $0 })) {
                    Text("간결").tag("brief"); Text("보통").tag("normal"); Text("자세히").tag("detailed")
                }
                .pickerStyle(.segmented).labelsHidden().frame(maxWidth: 220)
            }
            Toggle("시끄러운 곳 모드", isOn: Binding(get: { s.wrappedValue.noise_mode == "noisy" },
                                                 set: { s.wrappedValue.noise_mode = $0 ? "noisy" : "normal"; noiseMode = $0 ? "noisy" : "normal" }))
                .tint(Theme.celadon).foregroundStyle(Theme.text)
            if s.wrappedValue.noise_mode == "noisy" {
                Text("주변 소리에 말이 끊기지 않게 해요. 말을 끊으려면 통화 중에 구를 눌러 주세요.").font(.footnote).foregroundStyle(Theme.text3)
            }
        }
        Card("소리") {
            HStack {
                Toggle("작업 효과음", isOn: $workSounds).tint(Theme.celadon).foregroundStyle(Theme.text)
                Button("들어 보기") { EarconPreview.play() }.font(.subheadline).tint(Theme.celadon).disabled(!workSounds)
            }
            .accessibilityHint("\(AssistantName.josa(AssistantName.current, "이", "가")) 일하는 동안 짧은 소리를 냅니다")
        }
    }

    private func legacyAssistantCard(_ s: Binding<AppSettings>) -> some View {
        Card("비서") {
            HStack {
                Text("이름").foregroundStyle(Theme.text)
                Spacer()
                TextField(AssistantName.fallback, text: $nameDraft)
                    .multilineTextAlignment(.trailing).frame(maxWidth: 160)
                    .onSubmit { commitName(s) }
                    .onChange(of: nameDraft) { commitName(s) }
                    .accessibilityLabel("비서 이름")
                    .accessibilityHint("호출어는 바뀌지 않습니다")
            }
            if let nameError { Text(nameError).font(.footnote).foregroundStyle(Theme.danger) }
            VStack(alignment: .leading, spacing: Theme.xs) {
                HStack {
                    Text("성격").foregroundStyle(Theme.text)
                    Spacer()
                    Text("\((s.wrappedValue.personality ?? "").count)/\(AssistantName.personalityLimit)")
                        .font(.footnote.monospacedDigit()).foregroundStyle(Theme.text3)
                }
                TextField(personalityDefault ?? "기본 성격", text: Binding(
                    get: { s.wrappedValue.personality ?? "" },
                    set: { s.wrappedValue.personality = String($0.prefix(AssistantName.personalityLimit)) }), axis: .vertical)
                    .lineLimit(3...8)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { s.wrappedValue.personality = AssistantName.personality(s.wrappedValue.personality ?? "") }
                    .accessibilityLabel("성격")
                HStack {
                    Text("\(AssistantName.josa(AssistantName.current, "이", "가")) 말하는 성격이에요. 진지한 일에는 장난치지 않아요.")
                        .font(.footnote).foregroundStyle(Theme.text3)
                    Spacer()
                    Button("기본값으로 되돌리기") { s.wrappedValue.personality = "" }
                        .font(.footnote).buttonStyle(.borderless).tint(Theme.celadon)
                        .disabled((s.wrappedValue.personality ?? "").isEmpty)
                }
            }
            Text("호출어는 다시 학습하기 전까지 '\(AssistantName.fallback)'로 유지돼요.").font(.footnote).foregroundStyle(Theme.text3)
        }
    }

    /// Empty → the default name; anything else must pass validation before it is saved.
    private func commitName(_ s: Binding<AppSettings>) {
        let trimmed = nameDraft.trimmingCharacters(in: .whitespaces)
        guard let name = trimmed.isEmpty ? AssistantName.fallback : AssistantName.validated(trimmed) else {
            nameError = "한글, 영문, 공백으로 12자까지 쓸 수 있어요"
            return
        }
        nameError = nil
        if s.wrappedValue.assistant_name != name { s.wrappedValue.assistant_name = name }
    }

    // Usage
    private var usageCard: some View {
        let cost = (usage["cost_usd"] as? NSNumber)?.doubleValue ?? 0
        let budget = (usage["budget_usd"] as? NSNumber)?.doubleValue ?? settings?.monthly_budget_usd ?? 0
        let warn = Double(settings?.budget_warn_pct ?? 80) / 100
        let frac = budget > 0 ? cost / budget : 0
        let barColor = frac >= 1 ? Theme.danger : frac >= warn ? Theme.amber : Theme.celadon
        let calls = (usage["calls"] as? NSNumber).map { " · 통화 \($0.intValue)회" } ?? ""
        return Card("사용량", trailing: AnyView(Text(monthLabel + calls).font(.subheadline.monospacedDigit()).foregroundStyle(Theme.text2))) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.xs) {
                Text(cost, format: .currency(code: "USD")).font(.title.monospacedDigit()).foregroundStyle(Theme.text)
                if budget > 0 { Text("/ \(budget, format: .currency(code: "USD").precision(.fractionLength(0)))").foregroundStyle(Theme.text2) }
            }
            if budget > 0 {
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Theme.stroke)
                        Capsule().fill(barColor).frame(width: geo.size.width * min(1, frac))
                        Rectangle().fill(Theme.text3).frame(width: 1.5, height: 10).offset(x: geo.size.width * warn)
                    }
                }
                .frame(height: 6)
                .accessibilityElement()
                .accessibilityLabel("이번 달 사용량")
                .accessibilityValue("\(Int(budget))달러 중 \(Int(cost))달러, \(Int(frac * 100))퍼센트")
            }
            SectionNote(status: usageStatus) { Task { await load() } }
            breakdown(usage["by_model"])
            if usage["by_path"] is [String: Any] {
                DisclosureGroup("경로별 보기", isExpanded: $showPaths) { breakdown(usage["by_path"]) }
                    .font(.subheadline).tint(Theme.celadon)
            }
        }
    }

    private var monthLabel: String {
        guard let m = usage["month"] as? String, let n = Int(m.suffix(2)) else { return "" }
        return "\(n)월"
    }

    /// §12 Q3: each value is a number (USD), an object with `cost_usd`, or anything else (shown as text).
    @ViewBuilder private func breakdown(_ value: Any?) -> some View {
        if let dict = value as? [String: Any] {
            let rows = dict.keys.sorted().map { k -> (String, Double?, String) in
                let v = dict[k]!
                let n = (v as? NSNumber)?.doubleValue ?? ((v as? [String: Any])?["cost_usd"] as? NSNumber)?.doubleValue
                return (k, n, n.map { String(format: "$%.2f", $0) } ?? describe(v))
            }
            let maxValue = rows.compactMap(\.1).max() ?? 0
            VStack(spacing: Theme.s) {
                ForEach(rows, id: \.0) { k, n, text in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack {
                            Text(k).font(.subheadline).foregroundStyle(Theme.text2)
                            Spacer()
                            Text(text).font(.subheadline.monospacedDigit()).foregroundStyle(Theme.text)
                        }
                        if let n, maxValue > 0 {
                            GeometryReader { g in Capsule().fill(Theme.celadon.opacity(0.5)).frame(width: g.size.width * n / maxValue) }.frame(height: 3)
                        }
                    }
                    .accessibilityElement(children: .combine)
                }
            }
        }
    }

    // Status
    private var statusCard: some View {
        Card("상태", trailing: AnyView(Button("새로고침") { Task { await model.refresh() } }.font(.subheadline).tint(Theme.celadon))) {
            if let s = model.status {
                statusRow("spark", s.spark.ok, s.spark.ms)
                statusRow("게이트웨이", s.gateway.ok, s.gateway.ms)
                ForEach(s.devices, id: \.name) { statusRow(DeviceID.normalize($0.name), $0.ok, $0.ms) }
            } else if let e = model.statusError {
                Text(FriendlyError.describe(e).title).foregroundStyle(Theme.danger)
            } else {
                ProgressView()
            }
        }
    }

    private func statusRow(_ name: String, _ ok: Bool, _ ms: Double?) -> some View {
        HStack {
            StatusDot(ok: ok)
            Text(name).foregroundStyle(Theme.text)
            Spacer()
            Text(ms.map { "\(Int($0)) ms" } ?? "-").font(.body.monospacedDigit()).foregroundStyle(Theme.text2)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(name), \(ok ? "정상" : "장애")" + (ms.map { ", \(Int($0))밀리초" } ?? ""))
    }

    // Settings cards
    private var saveBadge: AnyView {
        switch saveState {
        case .saving: return AnyView(ProgressView().controlSize(.small))
        case .saved: return AnyView(Label("저장됨", systemImage: "checkmark").font(.footnote.weight(.semibold)).foregroundStyle(Theme.celadon))
        default: return AnyView(EmptyView())
        }
    }

    private func modelCard(_ s: Binding<AppSettings>) -> some View {
        Card("모델", trailing: saveBadge) {
            if case .failed(let why) = saveState {
                HStack {
                    Text("저장하지 못했어요: \(why)").font(.footnote).foregroundStyle(Theme.danger)
                    Spacer()
                    Button("다시 시도") { scheduleSave(settings, delay: 0) }.font(.footnote).tint(Theme.celadon)
                }
            }
            row("기본 모델") { picker(models, s.default_model) }
            row("Claude 경로") { picker(paths, s.claude_path) }
        }
    }

    private func taskCard(_ s: Binding<AppSettings>) -> some View {
        Card("작업별") {
            ForEach(tasks, id: \.0) { key, name in
                let task = Binding(get: { s.wrappedValue.task_defaults[key] ?? .init(model: "auto") },
                                   set: { s.wrappedValue.task_defaults[key] = $0 })
                row(name) {
                    HStack(spacing: Theme.s) {
                        picker(models, task.model)
                        picker([("", "기본 경로")] + paths, Binding(get: { task.wrappedValue.path ?? "" },
                                                                 set: { task.wrappedValue.path = $0.isEmpty ? nil : $0 }))
                    }
                }
            }
        }
    }

    private func budgetCard(_ s: Binding<AppSettings>) -> some View {
        Card("예산") {
            row("월 예산 (USD)") {
                TextField("500", value: s.monthly_budget_usd, format: .number).decimalInput()
                    .multilineTextAlignment(.trailing).frame(maxWidth: 120).font(.body.monospacedDigit())
            }
            Stepper("경고 기준 \(s.wrappedValue.budget_warn_pct)%", value: s.budget_warn_pct, in: 0...100, step: 5).foregroundStyle(Theme.text)
        }
    }

    /// §7.5 먼저 전화하기 (new shape): master switch, briefing, deadline check, server alerts,
    /// autonomous calls with limits, and the decision log. No quiet hours.
    private func proactiveCard(_ s: Binding<AppSettings>) -> some View {
        let p = s.proactive
        return Card("먼저 전화하기") {
            Toggle("먼저 전화하기", isOn: p.enabled).tint(Theme.celadon).foregroundStyle(Theme.text)
            if p.wrappedValue.enabled {
                timedRow("아침 브리핑", p.morning_briefing)
                timedRow("마감 확인", p.deadline_check)
                Toggle("서버 장애 알림", isOn: p.server_alerts).tint(Theme.celadon).foregroundStyle(Theme.text)
                Toggle("스스로 판단해서 전화", isOn: p.autonomous.enabled).tint(Theme.celadon).foregroundStyle(Theme.text)
                if p.wrappedValue.autonomous.enabled {
                    VStack(alignment: .leading, spacing: Theme.s) {
                        Stepper("하루 최대 \(p.wrappedValue.autonomous.max_per_day)번", value: p.autonomous.max_per_day, in: 1...10)
                        Stepper("최소 간격 \(gapLabel(p.wrappedValue.autonomous.min_gap_minutes))", value: p.autonomous.min_gap_minutes, in: 30...720, step: 30)
                        row("허용 시간") {
                            HStack(spacing: Theme.xs) {
                                DatePicker("시작", selection: time(window(p, 0)), displayedComponents: .hourAndMinute).labelsHidden()
                                Text("–").foregroundStyle(Theme.text2)
                                DatePicker("끝", selection: time(window(p, 1)), displayedComponents: .hourAndMinute).labelsHidden()
                            }
                        }
                    }
                    .foregroundStyle(Theme.text)
                    .padding(.leading, Theme.l)
                }
            }
            Button { showProactiveLog = true } label: {
                HStack {
                    Text("왜 전화했는지 보기").foregroundStyle(Theme.text)
                    Spacer()
                    Image(systemName: "chevron.right").foregroundStyle(Theme.text3)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
        .sheet(isPresented: $showProactiveLog) {
            NavigationStack { ProactiveLogView() }
                #if os(macOS)
                .frame(width: 520, height: 560)
                #endif
        }
    }

    private func timedRow(_ title: String, _ t: Binding<AppSettings.Proactive.Timed>) -> some View {
        HStack {
            Toggle(title, isOn: t.enabled).tint(Theme.celadon).foregroundStyle(Theme.text).fixedSize()
            Spacer()
            DatePicker(title, selection: time(t.time), displayedComponents: .hourAndMinute).labelsHidden()
                .disabled(!t.wrappedValue.enabled)
        }
    }

    private func gapLabel(_ minutes: Int) -> String {
        minutes % 60 == 0 ? "\(minutes / 60)시간" : "\(minutes / 60)시간 \(minutes % 60)분"
    }

    private func window(_ p: Binding<AppSettings.Proactive>, _ i: Int) -> Binding<String> {
        Binding(get: { let w = p.wrappedValue.autonomous.window; return i < w.count ? w[i] : (i == 0 ? "08:00" : "23:00") },
                set: { var w = p.wrappedValue.autonomous.window
                       while w.count < 2 { w.append(w.isEmpty ? "08:00" : "23:00") }
                       w[i] = $0
                       p.wrappedValue.autonomous.window = w })
    }

    private func deviceCard(_ s: Binding<AppSettings>) -> some View {
        let known = (model.status?.devices.map { DeviceID.normalize($0.name) } ?? []) + DeviceID.known
            + [s.wrappedValue.default_device] + s.wrappedValue.device_fallback
        let names = known.reduce(into: [String]()) { if !$0.contains($1) && !$1.isEmpty { $0.append($1) } }
        return Card("기기") {
            row("기본 기기") { picker(names.map { ($0, $0) }, s.default_device) }
            Text("기본 기기가 안 되면 이 순서대로 시도해요").font(.footnote).foregroundStyle(Theme.text3)
            FlowChips(items: s.device_fallback, candidates: names.filter { !s.wrappedValue.device_fallback.contains($0) })
        }
    }

    private var connectionCard: some View {
        Card("연결") {
            VStack(alignment: .leading, spacing: Theme.xs) {
                Text(Config.voiceURL).font(.footnote.monospaced()).foregroundStyle(Theme.text2).textSelection(.enabled)
                Text(Config.gatewayURL).font(.footnote.monospaced()).foregroundStyle(Theme.text2).textSelection(.enabled)
            }
            HStack {
                Button("토큰 바꾸기") { showSetup = true }
                Spacer()
                Button("연결 다시 확인") { Task { await model.refresh() } }
            }
            .font(.subheadline).tint(Theme.celadon)
        }
    }

    // 기록 보관 (§7.5 card 10)
    private static var retention: [(Int, String)] { [(7, "7일"), (30, "30일"), (90, "90일"), (365, "1년"), (0, "계속")] }
    private static func rank(_ d: Int) -> Int { d == 0 ? .max : d }

    private func historyCard(_ s: Binding<AppSettings>) -> some View {
        let current = s.wrappedValue.history_days ?? 365
        return Card("기록") {
            HStack {
                Text("기록 보관").foregroundStyle(Theme.text)
                Spacer()
                Picker("", selection: Binding(get: { current }, set: { new in
                    if Self.rank(new) < Self.rank(current) { pendingDays = new } else { s.wrappedValue.history_days = new }
                })) { ForEach(Self.retention, id: \.0) { Text($0.1).tag($0.0) } }
                    .pickerStyle(.menu).labelsHidden().tint(Theme.celadon).fixedSize()
            }
            Text("보관 기간이 지난 통화 기록은 서버에서 지워져요. 녹음은 이 기기에만 있어요.").font(.footnote).foregroundStyle(Theme.text3)
        }
        .confirmationDialog(pendingDays.map { d in
            let n = history.count(olderThan: d)
            return "\(Self.retention.first { $0.0 == d }?.1 ?? "\(d)일")보다 오래된 기록" + (n > 0 ? " \(n)개가" : "이") + " 지워져요"
        } ?? "", isPresented: Binding(get: { pendingDays != nil }, set: { if !$0 { pendingDays = nil } }), titleVisibility: .visible) {
            Button("지우기", role: .destructive) {
                guard let d = pendingDays else { return }
                s.wrappedValue.history_days = d
                pendingDays = nil
                Task { await history.deleteOlder(than: d) }
            }
            Button("취소", role: .cancel) { pendingDays = nil }
        }
    }

    private var recordingsCard: some View {
        Card("녹음") {
            Button { showRecordings = true } label: {
                HStack {
                    Text("녹음 \(Recordings.list().count)개").foregroundStyle(Theme.text)
                    Spacer()
                    Image(systemName: "chevron.right").foregroundStyle(Theme.text3)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
    }

    // Helpers
    private func row<V: View>(_ title: String, @ViewBuilder _ value: () -> V) -> some View {
        ViewThatFits(in: .horizontal) {
            HStack { Text(title).foregroundStyle(Theme.text); Spacer(); value() }
            VStack(alignment: .leading, spacing: Theme.xs) { Text(title).foregroundStyle(Theme.text); value() }
        }
    }

    private func picker(_ options: [(String, String)], _ value: Binding<String>) -> some View {
        Picker("", selection: value) { ForEach(options, id: \.0) { Text($0.1).tag($0.0) } }
            .pickerStyle(.menu).labelsHidden().tint(Theme.celadon).fixedSize()
    }

    /// "HH:mm" string ⇄ Date for DatePicker.
    private func time(_ s: Binding<String>) -> Binding<Date> {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "HH:mm"
        return Binding(get: { f.date(from: s.wrappedValue) ?? Date() }, set: { s.wrappedValue = f.string(from: $0) })
    }

    /// Settings, usage and the shared status load in parallel and independently; the old values
    /// stay on screen until new ones arrive, and a cancelled pull-to-refresh changes nothing.
    private func load() async {
        async let settingsResult = attempt { try await Gateway.send("GET", "/v1/settings") }
        async let usageResult = attempt { try await Gateway.send("GET", "/v1/usage") }
        async let refreshed: Void = model.refresh()
        switch await settingsResult {
        case .success(let data):
            guard let raw = try? JSONDecoder().decode(AppSettings.self, from: data) else { loadError = URLError(.cannotParseResponse); break }
            personalityDefault = PersonalityDefault.read(data) ?? personalityDefault
            // Device names migrate to their ids for display; nothing is saved until the user edits.
            let s = DeviceID.normalize(raw)
            loaded = s
            settings = s
            if let n = s.assistant_name { AssistantName.current = n; nameDraft = AssistantName.current }
            noiseMode = s.noise_mode ?? "normal"
            loadError = nil
        case .failure(let e): if SectionStatus.from(e) != nil { loadError = e }
        }
        switch await usageResult {
        case .success(let data):
            usage = (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? usage
            usageStatus = .ok
        case .failure(let e): usageStatus = SectionStatus.from(e) ?? usageStatus
        }
        await refreshed
    }

    /// Debounced auto-save: 600 ms after the last change (§7.5).
    private func scheduleSave(_ new: AppSettings?, delay: UInt64 = 600) {
        guard let new, new != loaded else { return }
        saveTask?.cancel()
        saveTask = Task {
            try? await Task.sleep(nanoseconds: delay * 1_000_000)
            guard !Task.isCancelled else { return }
            saveState = .saving
            do {
                try await Gateway.saveSettings(new)
                loaded = new
                if let n = new.assistant_name { AssistantName.current = n }
                saveState = .saved
            } catch { saveState = .failed(FriendlyError.describe(error).title) }
        }
    }
}

extension SettingsScreen where Extra == EmptyView {
    init(model: AppModel, history: HistoryModel) { self.init(model: model, history: history) { EmptyView() } }
}

/// Fallback order as chips: tap a chip to move it earlier, × to remove, + to add (§7.5).
private struct FlowChips: View {
    @Binding var items: [String]
    let candidates: [String]

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: Theme.s) {
                ForEach(Array(items.enumerated()), id: \.element) { i, name in
                    HStack(spacing: Theme.xs) {
                        Text("\(i + 1). \(name)").font(.subheadline).foregroundStyle(Theme.text)
                        Button { items.remove(at: i) } label: { Image(systemName: "xmark").font(.caption2) }
                            .buttonStyle(.plain).foregroundStyle(Theme.text3).accessibilityLabel("\(name) 빼기")
                    }
                    .padding(.horizontal, Theme.m).padding(.vertical, 6)
                    .overlay(Capsule().strokeBorder(Theme.stroke))
                    .onTapGesture { if i > 0 { items.swapAt(i, i - 1) } }
                    .accessibilityAction(named: "앞으로") { if i > 0 { items.swapAt(i, i - 1) } }
                }
                if !candidates.isEmpty {
                    Menu {
                        ForEach(candidates, id: \.self) { c in Button(c) { items.append(c) } }
                    } label: { Image(systemName: "plus").padding(6) }
                    .menuStyle(.borderlessButton).fixedSize().tint(Theme.celadon).accessibilityLabel("기기 추가")
                }
            }
        }
    }
}

// MARK: - Approvals (DESIGN §7.6)

struct ApprovalsScreen: View {
    @ObservedObject var model: AppModel
    @State private var confirm: [String: Any]?
    @State private var done: Set<String> = []
    @State private var error: String?
    @State private var succeeded = 0

    private static let risky = ["rm ", "rm\t", "git push", "sudo", ">"]

    var body: some View {
        ScrollView {
            LazyVStack(spacing: Theme.l) {
                if let error { Text(error).font(.footnote).foregroundStyle(Theme.danger) }
                SectionNote(status: model.approvalsStatus) { Task { await model.refresh() } }
                if model.approvals.isEmpty && model.approvalsStatus == .ok {
                    EmptyState(title: "모두 처리했어요", message: "승인이 필요하면 알려 드릴게요.")
                }
                ForEach(model.approvals.indices, id: \.self) { i in card(model.approvals[i]) }
            }
            .padding(.horizontal, 20).padding(.vertical, Theme.l)
            .frame(maxWidth: 640).frame(maxWidth: .infinity)
        }
        .background(Theme.bg.ignoresSafeArea())
        .navigationTitle("승인")
        .refreshable { await model.refresh() }
        .task { await model.refresh() }
        #if os(iOS)
        .sensoryFeedback(.success, trigger: succeeded)
        #endif
        .confirmationDialog("이 명령을 승인할까요?", isPresented: Binding(get: { confirm != nil }, set: { if !$0 { confirm = nil } }),
                            titleVisibility: .visible, presenting: confirm) { item in
            Button("승인") { decide(item, true) }
            Button("취소", role: .cancel) {}
        } message: { item in Text(describe(item["command"] ?? "")) }
    }

    private func card(_ item: [String: Any]) -> some View {
        let id = describe(item["id"] ?? "")
        let command = item["command"].map(describe) ?? ""
        let known: Set = ["id", "device", "tool", "command", "cwd", "created", "status", "decided"]
        return Card {
            if done.contains(id) {
                Label("처리했어요", systemImage: "checkmark").foregroundStyle(Theme.celadon)
            } else {
                HStack(spacing: Theme.s) {
                    Text(item["device"].map(describe) ?? "기기").font(.headline).foregroundStyle(Theme.text)
                    if let tool = item["tool"] {
                        Text(describe(tool)).font(.footnote.weight(.semibold)).foregroundStyle(Theme.text2)
                            .padding(.horizontal, Theme.s).padding(.vertical, 2).overlay(Capsule().strokeBorder(Theme.stroke))
                    }
                    Spacer()
                    if let created = (item["created"] as? NSNumber)?.doubleValue {
                        Text(Date(timeIntervalSince1970: created), style: .relative).font(.footnote).foregroundStyle(Theme.text3)
                    }
                }
                if !command.isEmpty {
                    ScrollView(.horizontal) {
                        Text(command).font(.callout.monospaced()).foregroundStyle(Theme.text).textSelection(.enabled).padding(Theme.m)
                    }
                    .background(Theme.bg, in: RoundedRectangle(cornerRadius: Theme.rowRadius, style: .continuous))
                }
                if let cwd = item["cwd"] {
                    Label(describe(cwd), systemImage: "folder").font(.footnote).foregroundStyle(Theme.text2)
                }
                ForEach(item.keys.filter { !known.contains($0) }.sorted(), id: \.self) { key in
                    Text("\(key): \(describe(item[key]!))").font(.footnote).foregroundStyle(Theme.text2)
                }
                HStack(spacing: Theme.m) {
                    Button { decide(item, false) } label: { Text("거절").frame(maxWidth: .infinity).frame(height: 44) }
                        .buttonStyle(.bordered).foregroundStyle(Theme.danger)
                    Button { Self.risky.contains { command.contains($0) } ? (confirm = item) : decide(item, true) } label: {
                        Text("승인").frame(maxWidth: .infinity).frame(height: 44)
                    }
                    .buttonStyle(.borderedProminent).tint(Theme.celadon).foregroundStyle(Theme.onAccent)
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel([item["device"], item["tool"], item["command"].map { "명령: \(describe($0))" }, item["cwd"].map { "위치: \(describe($0))" }]
            .compactMap { $0.map(describe) }.joined(separator: ", "))
        .accessibilityAction(named: "승인") { decide(item, true) }
        .accessibilityAction(named: "거절") { decide(item, false) }
    }

    private func decide(_ item: [String: Any], _ approve: Bool) {
        let id = describe(item["id"] ?? "")
        Task {
            do {
                withAnimation(Theme.enter) { _ = done.insert(id) }
                try await model.decide(item, approve)
                succeeded += 1
                error = nil
            } catch {
                done.remove(id)
                self.error = FriendlyError.describe(error).title
            }
        }
    }
}

// MARK: - Transcript (call sheet)

struct TranscriptView: View {
    let lines: [CallLine]

    var body: some View {
        ScrollView {
            LazyVStack(spacing: Theme.m) {
                ForEach(lines) { line in
                    HStack(alignment: .top) {
                        if line.mine { Spacer(minLength: 40) }
                        VStack(alignment: line.mine ? .trailing : .leading, spacing: Theme.xs) {
                            Text(line.text + (line.interrupted ? " …" : ""))
                                .foregroundStyle(line.mine ? Theme.text2 : line.interrupted ? Theme.text3 : Theme.text)
                                .multilineTextAlignment(line.mine ? .trailing : .leading).textSelection(.enabled)
                            if let route = line.route { RouteChip(route: route) }
                        }
                        if !line.mine { Spacer(minLength: 40) }
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel((line.mine ? "나: " : "\(AssistantName.current): ") + line.text)
                }
            }
            .padding(20)
        }
        .background(Theme.bg.ignoresSafeArea())
    }
}

// MARK: - Platform shims

extension View {
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
