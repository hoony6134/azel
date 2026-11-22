import SwiftUI

struct TaskBadgeView: View {
    let status: String
    var body: some View {
        let c = TaskBadge.color(status)
        Text(TaskBadge.label(status)).font(.footnote.weight(.semibold)).foregroundStyle(c)
            .padding(.horizontal, Theme.s).padding(.vertical, 2)
            .background(c.opacity(0.12), in: Capsule())
            .overlay(Capsule().strokeBorder(c.opacity(0.6), lineWidth: 1))
    }
}

/// Home / Mac home: running tasks with live status; hidden when nothing runs and the API is fine.
struct RunningTasksCard: View {
    @ObservedObject var tasks: TaskModel
    @ObservedObject var app: AppModel
    @State private var showAll = false
    @State private var detail: AgentTask?

    var body: some View {
        Card("작업", trailing: AnyView(Button("모두 보기") { showAll = true }.font(.subheadline).tint(Theme.celadon))) {
            if tasks.active.isEmpty {
                Text(tasks.status == .ok ? "진행 중인 작업이 없어요" : "").foregroundStyle(Theme.text2)
            }
            SectionNote(status: tasks.status) { Task { await tasks.refresh() } }
            ForEach(tasks.active.prefix(3)) { t in
                Button { detail = t } label: {
                    HStack(alignment: .top) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(t.goal).foregroundStyle(Theme.text).lineLimit(1)
                            if let last = t.steps.last { Text(last.label).font(.footnote).foregroundStyle(Theme.text3).lineLimit(1) }
                        }
                        Spacer()
                        TaskBadgeView(status: t.status)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityElement(children: .combine)
            }
        }
        .task { await tasks.refresh() }
        .sheet(isPresented: $showAll) { NavigationStack { TasksScreen(tasks: tasks, app: app) }.taskSheetSize() }
        .sheet(item: $detail) { t in NavigationStack { TaskDetailView(task: t, tasks: tasks, app: app) }.taskSheetSize() }
        .onChange(of: tasks.open) { _, t in if let t { detail = t; tasks.open = nil } }
    }
}

struct TasksScreen: View {
    @ObservedObject var tasks: TaskModel
    @ObservedObject var app: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var creating = false

    var body: some View {
        List {
            SectionNote(status: tasks.status) { Task { await tasks.refresh() } }
            if tasks.tasks.isEmpty && tasks.status == .ok {
                Text("아직 작업이 없어요. 오래 걸리는 일을 맡겨 보세요.").foregroundStyle(Theme.text2)
            }
            ForEach(tasks.tasks) { t in
                NavigationLink(value: t) {
                    HStack(alignment: .top) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(t.goal).foregroundStyle(Theme.text).lineLimit(2)
                            Text([t.device, t.created_at.map { $0.formatted(date: .abbreviated, time: .shortened) }].compactMap { $0 }.joined(separator: " · "))
                                .font(.footnote).foregroundStyle(Theme.text3)
                        }
                        Spacer()
                        TaskBadgeView(status: t.status)
                    }
                }
                .listRowBackground(Theme.card)
            }
        }
        .scrollContentBackground(.hidden)
        .background(Theme.bg.ignoresSafeArea())
        .navigationTitle("작업")
        .navigationDestination(for: AgentTask.self) { TaskDetailView(task: $0, tasks: tasks, app: app) }
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("닫기") { dismiss() } }
            ToolbarItem(placement: .primaryAction) { Button { creating = true } label: { Label("새 작업", systemImage: "plus") } }
        }
        .refreshable { await tasks.refresh() }
        .task { await tasks.refresh() }
        .sheet(isPresented: $creating) { NavigationStack { TaskCreateView(tasks: tasks) }.taskSheetSize() }
    }
}

struct TaskDetailView: View {
    let task: AgentTask
    @ObservedObject var tasks: TaskModel
    @ObservedObject var app: AppModel
    @State private var message: String?
    @State private var confirmCancel = false

    /// The live copy from the poll, falling back to the one we were opened with.
    private var t: AgentTask { tasks.tasks.first { $0.id == task.id } ?? task }
    private var approvals: [[String: Any]] {
        app.approvals.filter { t.device == nil || describe($0["device"] ?? "") == t.device || DeviceID.normalize(describe($0["device"] ?? "")) == t.device }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.l) {
                HStack(alignment: .top) {
                    Text(t.goal).font(.title3.weight(.semibold)).foregroundStyle(Theme.text)
                    Spacer()
                    TaskBadgeView(status: t.status)
                }
                if let d = t.device { Label(d, systemImage: "laptopcomputer").font(.footnote).foregroundStyle(Theme.text2) }
                if let note = t.note, t.isActive {
                    Label(note, systemImage: "hourglass").font(.footnote).foregroundStyle(Theme.text2).fixedSize(horizontal: false, vertical: true)
                }
                if t.isActive, t.notify != nil {
                    Toggle("끝나면 전화", isOn: Binding(get: { t.notify == "call" }, set: { on in
                        Task {
                            do { try await tasks.setNotify(t, call: on); message = nil }
                            catch GatewayError.http(let code) where code == 404 || code == 405 { message = "끝나면 전화는 서버 업데이트 후 바꿀 수 있어요" }
                            catch { message = "바꾸지 못했어요: " + FriendlyError.describe(error).title }
                        }
                    }))
                    .tint(Theme.celadon).foregroundStyle(Theme.text)
                }
                if let b = t.backend {
                    Label("엔진: " + TaskBackend.label(b), systemImage: "cpu").font(.footnote).foregroundStyle(Theme.text2)
                    if let why = t.fallback_reason, !why.isEmpty {
                        Text("대신 처리한 이유: " + why).font(.footnote).foregroundStyle(Theme.amber).fixedSize(horizontal: false, vertical: true)
                    }
                }
                if t.status == "waiting_approval" {
                    Card("승인이 필요해요") {
                        if approvals.isEmpty { Text("승인 탭에서 확인해 주세요").foregroundStyle(Theme.text2) }
                        ForEach(approvals.indices, id: \.self) { i in
                            let item = approvals[i]
                            VStack(alignment: .leading, spacing: Theme.s) {
                                Text(describe(item["command"] ?? item["tool"] ?? "")).font(.callout.monospaced()).foregroundStyle(Theme.text)
                                HStack {
                                    Button("거절") { decide(item, false) }.buttonStyle(.bordered).foregroundStyle(Theme.danger)
                                    Button("승인") { decide(item, true) }.buttonStyle(.borderedProminent).tint(Theme.celadon).foregroundStyle(Theme.onAccent)
                                }
                            }
                        }
                    }
                }
                Card("진행") {
                    if t.steps.isEmpty { Text("아직 시작 전이에요").foregroundStyle(Theme.text3) }
                    ForEach(Array(t.steps.enumerated()), id: \.offset) { i, step in
                        HStack(alignment: .top, spacing: Theme.m) {
                            VStack(spacing: 0) {
                                Image(systemName: Activity.glyph(kind: nil, tool: step.tool)).foregroundStyle(i == t.steps.count - 1 && t.isActive ? Theme.indigo : Theme.text2)
                                    .frame(width: 22)
                                if i < t.steps.count - 1 { Rectangle().fill(Theme.stroke).frame(width: 1).frame(minHeight: 14) }
                            }
                            VStack(alignment: .leading, spacing: 2) {
                                Text(step.label).foregroundStyle(Theme.text)
                                Text([step.ts.map { $0.formatted(date: .omitted, time: .shortened) }, step.device.map(DeviceID.normalize)].compactMap { $0 }.joined(separator: " · "))
                                    .font(.footnote).foregroundStyle(Theme.text3)
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
                if let e = t.error, t.status == "failed" {
                    Card("실패 이유") { Text(e).foregroundStyle(Theme.text).textSelection(.enabled) }
                }
                if let r = t.result, !r.isEmpty {
                    Card("결과") { Text(r).foregroundStyle(Theme.text).textSelection(.enabled) }
                }
                if let c = t.cost { Text("비용 \(c, format: .currency(code: "USD"))").font(.footnote.monospacedDigit()).foregroundStyle(Theme.text3) }
                if let message { Text(message).font(.footnote).foregroundStyle(Theme.danger) }
                if t.isActive {
                    Button("작업 취소", role: .destructive) { confirmCancel = true }
                }
            }
            .padding(20)
            .frame(maxWidth: 640).frame(maxWidth: .infinity)
        }
        .background(Theme.bg.ignoresSafeArea())
        .navigationTitle("작업")
        .refreshable { await tasks.refresh(); await app.refresh() }
        .task { await app.refresh() }
        .confirmationDialog("이 작업을 취소할까요?", isPresented: $confirmCancel, titleVisibility: .visible) {
            Button("작업 취소", role: .destructive) {
                Task { do { try await tasks.cancel(t) } catch { message = "취소하지 못했어요: " + FriendlyError.describe(error).title } }
            }
            Button("계속", role: .cancel) {}
        }
    }

    private func decide(_ item: [String: Any], _ approve: Bool) {
        Task {
            do { try await app.decide(item, approve); await tasks.refresh() }
            catch let error where ApprovalCenter.alreadyDecided(error) {
                message = "다른 기기에서 이미 처리했어요"
                await app.refresh(); await tasks.refresh()
            }
            catch { message = FriendlyError.describe(error).title }
        }
    }
}

struct TaskCreateView: View {
    @ObservedObject var tasks: TaskModel
    @Environment(\.dismiss) private var dismiss
    @State private var goal = ""
    @State private var device: String?
    @State private var devices: [String] = []
    @State private var notify = TaskNotify.push
    @State private var persona: String? = PersonaModel.shared.selected?.id
    @State private var autoApprove = false
    @State private var saving = false
    @State private var message: String?

    var body: some View {
        Form {
            Section("무엇을 할까요?") {
                TextField("예: 이번 주 과제 마감 정리해서 노션에 올려 줘", text: $goal, axis: .vertical).lineLimit(3...8)
            }
            Section {
                Picker("기기", selection: $device) {
                    Text("자동").tag(String?.none)
                    ForEach(devices, id: \.self) { Text($0).tag(Optional($0)) }
                }
                if PersonaModel.shared.personas.count > 1 {
                    Picker("비서", selection: $persona) {
                        ForEach(PersonaModel.shared.ordered) { Text($0.name).tag(Optional($0.id)) }
                    }
                }
                Picker("끝나면", selection: $notify) {
                    ForEach(TaskNotify.allCases) { Text($0.label).tag($0) }
                }
                .pickerStyle(.segmented)
                if BrainModeChoice.shared.lastCaps.contains("auto_approve") {
                    Toggle("자동 승인", isOn: $autoApprove)
                }
                if autoApprove {
                    Text("위험하지 않은 명령은 묻지 않고 실행해요. 위험한 건 여전히 물어봐요.").font(.footnote).foregroundStyle(Theme.text3)
                }
            }
            if let message { Text(message).font(.footnote).foregroundStyle(Theme.danger) }
        }
        .formStyle(.grouped)
        .navigationTitle("새 작업")
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("취소") { dismiss() } }
            ToolbarItem(placement: .confirmationAction) {
                Button(saving ? "맡기는 중…" : "맡기기") {
                    saving = true
                    Task {
                        do {
                            let t = try await tasks.create(goal: goal, device: device, persona: PersonaModel.shared.personas.count > 1 ? persona : nil, notify: notify)
                            if autoApprove, let id = t?.id {
                                if case .success(let g?) = await attempt({ try await AutoApproveAPI.grant(task: id, devices: device.map { [$0] }) }) {
                                    AutoGrantCenter.remember(g.id)
                                }
                            }
                            dismiss()
                        }
                        catch GatewayError.rejected(let why) { message = why }
                        catch { message = "맡기지 못했어요: " + FriendlyError.describe(error).title }
                        saving = false
                    }
                }
                .disabled(goal.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || saving)
            }
        }
        .task { if case .success(let d) = await attempt({ try await TaskAPI.devices() }) { devices = d } }
    }
}

private extension View {
    func taskSheetSize() -> some View {
        #if os(macOS)
        frame(width: 560, height: 640)
        #else
        self
        #endif
    }
}
