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
            // Finished results (overnight ones too) right here, not only under "모두 보기".
            ForEach(tasks.tasks.filter { $0.isRecentResult() }.prefix(2)) { t in
                Button { detail = t } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(t.goal).foregroundStyle(Theme.text).lineLimit(1)
                            Spacer()
                            TaskBadgeView(status: t.status)
                        }
                        Text(ChatSession.plain(t.result ?? "").replacingOccurrences(of: "\n", with: " "))
                            .font(.footnote).foregroundStyle(Theme.text2).lineLimit(2)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityElement(children: .combine)
                .accessibilityHint("결과 보기")
            }
            if tasks.active.isEmpty {
                Text(tasks.status == .ok ? "진행 중인 작업이 없어요" : "").foregroundStyle(Theme.text2)
            }
            SectionNote(status: tasks.status) { Task { await tasks.refresh() } }
            if !tasks.scheduled.isEmpty {
                Label("예약된 작업 \(tasks.scheduled.count)개" + (tasks.scheduled.first?.scheduledFor.map { " · 다음 " + KST.dayAndTime($0) } ?? ""),
                      systemImage: "moon.zzz").font(.footnote).foregroundStyle(Theme.text2)
            }
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
        .onChange(of: tasks.showList) { _, on in if on { showAll = true; tasks.showList = false } }
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
                            Text([t.device, t.scheduledFor.map { "예정 " + KST.dayAndTime($0) } ?? t.created_at.map { $0.formatted(date: .abbreviated, time: .shortened) }].compactMap { $0 }.joined(separator: " · "))
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
                if t.isScheduled, let at = t.scheduledFor {
                    Label("\(KST.dayAndTime(at))에 시작해요", systemImage: "moon.zzz").font(.footnote).foregroundStyle(Theme.text2)
                }
                if let note = t.note, t.isActive {
                    Label(note, systemImage: "hourglass").font(.footnote).foregroundStyle(Theme.text2).fixedSize(horizontal: false, vertical: true)
                }
                if t.isActive || t.isScheduled, t.notify != nil {
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
                    Card("결과") { ChatMarkdownView(text: r) }   // markdown, as the brain writes it
                    if !t.paths.isEmpty {
                        Card("파일") { ForEach(t.paths, id: \.self) { TaskPathRow(path: $0) } }
                    }
                }
                if let line = TaskDetailView.timing(t) { Label(line, systemImage: "clock").font(.footnote).foregroundStyle(Theme.text3) }
                if let c = t.cost { Text("비용 \(c, format: .currency(code: "USD"))").font(.footnote.monospacedDigit()).foregroundStyle(Theme.text3) }
                if let message { Text(message).font(.footnote).foregroundStyle(Theme.danger) }
                if t.isActive || t.isScheduled {
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
    @State private var whenChoice = 0              // 0 지금, 1 밤새, 2 시간 지정
    @State private var whenAt = Date().addingTimeInterval(3600)
    private var when: TaskWhen { whenChoice == 1 ? .overnight : whenChoice == 2 ? .at(whenAt) : .now }
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
                if AppSettings.supportsOvernight {
                    Picker("언제", selection: $whenChoice) { Text("지금").tag(0); Text("밤새").tag(1); Text("시간 지정").tag(2) }
                        .pickerStyle(.segmented)
                    if whenChoice == 2 {
                        DatePicker("시작", selection: $whenAt, in: Date()...Date().addingTimeInterval(7 * 86_400), displayedComponents: [.date, .hourAndMinute])
                            .environment(\.timeZone, KST.zone)
                    }
                }
                Picker("끝나면", selection: $notify) {
                    ForEach(TaskNotify.allCases) { Text($0.label).tag($0) }
                }
                .pickerStyle(.segmented)
                if BrainModeChoice.shared.lastCaps.contains("auto_approve") || (AppSettings.supportsOvernight && whenChoice != 0) {
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
                            // A gateway with the overnight queue takes auto_approve on the task itself;
                            // an older one gets a separate grant right after.
                            let inBody = AppSettings.supportsOvernight
                            let t = try await tasks.create(goal: goal, device: device, persona: PersonaModel.shared.personas.count > 1 ? persona : nil,
                                                           notify: notify, when: when, autoApprove: autoApprove && inBody)
                            if autoApprove, !inBody, let id = t?.id {
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

extension View {
    func taskSheetSize() -> some View {
        #if os(macOS)
        frame(width: 560, height: 640)
        #else
        self
        #endif
    }
}

extension TaskDetailView {
    /// "시작 23:10 · 끝 23:42 · 32분".
    nonisolated static func timing(_ t: AgentTask) -> String? {
        guard let start = t.started ?? t.created_at else { return nil }
        var parts = ["시작 " + start.formatted(date: .abbreviated, time: .shortened)]
        if let end = t.finished {
            parts.append("끝 " + end.formatted(date: .omitted, time: .shortened))
            let m = Int(end.timeIntervalSince(start) / 60)
            parts.append(m < 1 ? "1분 미만" : m < 60 ? "\(m)분" : "\(m / 60)시간 \(m % 60)분")
        }
        return parts.joined(separator: " · ")
    }
}

/// A file a task mentions: copy its path; on the Mac, reveal it when it is on this Mac.
struct TaskPathRow: View {
    let path: String
    @State private var copied = false
    var body: some View {
        HStack(spacing: Theme.s) {
            Image(systemName: "doc").foregroundStyle(Theme.text3)
            Text(path).font(.footnote.monospaced()).foregroundStyle(Theme.text).lineLimit(2).truncationMode(.middle).textSelection(.enabled)
            Spacer()
            #if os(macOS)
            if FileManager.default.fileExists(atPath: (path as NSString).expandingTildeInPath) {
                Button("Finder에서 보기") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: (path as NSString).expandingTildeInPath)]) }
                    .font(.caption).buttonStyle(.borderless)
            }
            #endif
            Button(copied ? "복사됨" : "복사") {
                #if os(macOS)
                NSPasteboard.general.clearContents(); NSPasteboard.general.setString(path, forType: .string)
                #else
                UIPasteboard.general.string = path
                #endif
                copied = true
            }
            .font(.caption).buttonStyle(.borderless)
        }
        .accessibilityElement(children: .combine)
    }
}
