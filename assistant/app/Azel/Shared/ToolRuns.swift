import SwiftUI

/// One tool run of a call (`activity` tool_start → tool_output… → tool_end, matched by `call_id`): which
/// command on which host, its input, and its output as it streams. Its own ObservableObject, so a chunk
/// redraws only this run's detail, and the output is kept as lines (a lazy list appends rows instead of
/// re-laying out one big text), capped at `cap` bytes from the start.
/// Main queue only (call events are delivered there).
final class ToolRun: ObservableObject, Identifiable {
    struct Line: Identifiable, Equatable { let id: Int; var text: String }

    let id: String
    let tool: String?
    let label: String
    let glyph: String
    let host: String?
    let input: String?
    let taskId: String?
    let started: Date
    @Published private(set) var lines: [Line] = []
    @Published private(set) var finished = false
    @Published private(set) var ok: Bool?
    @Published private(set) var exitCode: Int?
    @Published private(set) var durationMs: Double?
    /// The start was cut to stay under `cap` (or the server sent only the tail).
    @Published private(set) var clipped = false

    static let cap = 64 * 1024
    private var bytes = 0
    private var nextLine = 0
    private var streamed = false

    init(id: String, tool: String?, label: String, glyph: String, host: String?, input: String?, taskId: String?, started: Date = Date()) {
        self.id = id; self.tool = tool; self.label = label; self.glyph = glyph; self.host = host; self.input = input
        self.taskId = taskId; self.started = started
    }

    var text: String { lines.map(\.text).joined(separator: "\n") }

    /// tool_output: a chunk, possibly mid-line.
    func append(_ chunk: String) {
        streamed = true
        add(chunk)
    }

    /// tool_end. Its `output` (the last ~8 KB) is used only when nothing streamed.
    func finish(ok: Bool?, exitCode: Int?, durationMs: Double?, output: String?, truncated: Bool?) {
        if !streamed, let output, !output.isEmpty { add(output); if truncated == true { clipped = true } }
        self.ok = ok ?? exitCode.map { $0 == 0 }
        self.exitCode = exitCode
        self.durationMs = durationMs ?? Date().timeIntervalSince(started) * 1000
        finished = true
    }

    private func add(_ chunk: String) {
        var parts = chunk.components(separatedBy: "\n")
        if let first = parts.first, !lines.isEmpty {   // continue the last, unfinished line
            lines[lines.count - 1].text += first
            parts.removeFirst()
        }
        for p in parts { lines.append(Line(id: nextLine, text: p)); nextLine += 1 }
        bytes += chunk.utf8.count
        while bytes > Self.cap, lines.count > 1 {   // keep the newest output
            bytes -= lines[0].text.utf8.count + 1
            lines.removeFirst()
            clipped = true
        }
    }
}

/// Every tool run of the current call, in start order (kept for the whole call).
/// Main queue only.
final class ToolRunStore: ObservableObject {
    @Published private(set) var runs: [ToolRun] = []

    func reset() { runs = [] }
    func run(_ id: String?) -> ToolRun? { id.flatMap { id in runs.first { $0.id == id } } }

    func apply(_ e: CallEvent) {
        guard e.type == "activity", let id = e.call_id else { return }
        switch e.phase {
        case "tool_start":
            guard run(id) == nil else { return }
            runs.append(ToolRun(id: id, tool: e.tool, label: e.label ?? e.tool ?? "도구", glyph: Activity.glyph(kind: e.kind, tool: e.tool),
                                host: e.host, input: e.input, taskId: e.task_id))
        case "tool_output":
            if let chunk = e.output { run(id)?.append(chunk) }
        case "tool_end":
            run(id)?.finish(ok: e.ok, exitCode: e.exit_code, durationMs: e.duration_ms, output: e.output, truncated: e.truncated)
        default: break
        }
    }
}

/// What a tap on the activity line opens: one run, or the list of this call's runs.
enum ToolRunTarget: Identifiable, Hashable {
    case run(String), list
    var id: String { switch self { case .run(let id): return "run-\(id)"; case .list: return "list" } }
}

// MARK: views

struct ToolRunStatus: View {
    @ObservedObject var run: ToolRun
    var body: some View {
        if !run.finished {
            ProgressView().controlSize(.small)
        } else if run.ok == true {
            Label("완료", systemImage: "checkmark.circle.fill").labelStyle(.iconOnly).foregroundStyle(Theme.celadon)
        } else {
            Text("✗ \(run.exitCode.map(String.init) ?? "실패")").font(.caption.weight(.semibold).monospacedDigit()).foregroundStyle(Theme.danger)
        }
    }
}

struct ToolRunDetailView: View {
    @ObservedObject var run: ToolRun
    @State private var following = true
    @State private var copied = false
    @State private var task: AgentTask?
    @State private var taskError: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.m) {
                header
                if let input = run.input, !input.isEmpty {
                    block(title: "입력", text: input)
                }
                outputBlock
                if let id = run.taskId {
                    Button { Task { await openTask(id) } } label: { Label("전체 로그", systemImage: "doc.text.magnifyingglass") }
                    if let taskError { Text(taskError).font(.caption).foregroundStyle(Theme.danger) }
                }
            }
            .padding(Theme.l)
        }
        .navigationTitle(run.label)
        .sheet(item: $task) { t in
            NavigationStack { TaskDetailView(task: t, tasks: TaskModel.shared, app: AppModel(sessionPrefix: "tool")) }.taskSheetSize()
        }
    }

    private var header: some View {
        HStack(spacing: Theme.s) {
            Image(systemName: run.glyph).foregroundStyle(Theme.indigo)
            Text(run.tool ?? run.label).font(.headline.monospaced()).foregroundStyle(Theme.text).lineLimit(1)
            if let host = run.host {
                Text(host).font(.caption.weight(.semibold)).padding(.horizontal, 6).padding(.vertical, 2)
                    .background(Theme.indigo.opacity(0.15), in: Capsule()).foregroundStyle(Theme.indigo)
            }
            Spacer()
            ToolRunStatus(run: run)
            if let ms = run.durationMs { Text(Self.duration(ms)).font(.caption.monospacedDigit()).foregroundStyle(Theme.text3) }
        }
        .accessibilityElement(children: .combine)
    }

    private func block(title: String, text: String) -> some View {
        VStack(alignment: .leading, spacing: Theme.xs) {
            Text(title).font(.caption.weight(.semibold)).foregroundStyle(Theme.text3)
            Text(text).font(.system(.footnote, design: .monospaced)).textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(Theme.s).background(Theme.bgRaised, in: RoundedRectangle(cornerRadius: 8))
        }
    }

    private var outputBlock: some View {
        VStack(alignment: .leading, spacing: Theme.xs) {
            HStack {
                Text("출력").font(.caption.weight(.semibold)).foregroundStyle(Theme.text3)
                if run.clipped { Text("앞부분 생략").font(.caption2).foregroundStyle(Theme.text3) }
                Spacer()
                Button(copied ? "복사됨" : "복사") { copy(run.text); copied = true }.font(.caption).disabled(run.lines.isEmpty)
            }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(run.lines) { l in
                            Text(l.text.isEmpty ? " " : l.text).font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading).id(l.id)
                        }
                        Color.clear.frame(height: 1).id(FollowScroll.bottomID)
                    }
                    .padding(Theme.s)
                }
                .frame(minHeight: 120, maxHeight: 320)
                .background(Theme.bgRaised, in: RoundedRectangle(cornerRadius: 8))
                // follows the newest output while running; a scroll by hand stops it until back at the bottom
                .followScroll(proxy, following: $following, trigger: run.lines.last?.id ?? -1, animated: false) { (FollowScroll.bottomID, .bottom) }
            }
            if run.lines.isEmpty { Text(run.finished ? "출력 없음" : "기다리는 중…").font(.caption).foregroundStyle(Theme.text3) }
        }
    }

    private func copy(_ s: String) {
        #if os(macOS)
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(s, forType: .string)
        #else
        UIPasteboard.general.string = s
        #endif
    }

    private func openTask(_ id: String) async {
        do { task = try await TaskAPI.get(id); taskError = nil } catch { taskError = "작업을 불러오지 못했어요" }
    }

    nonisolated static func duration(_ ms: Double) -> String {
        ms < 1000 ? "\(Int(ms))ms" : ms < 60_000 ? String(format: "%.1f초", ms / 1000) : "\(Int(ms / 60_000))분 \(Int(ms / 1000) % 60)초"
    }
}

/// This call's runs, newest first; each opens its detail.
struct ToolRunsView: View {
    @ObservedObject var store: ToolRunStore
    var body: some View {
        List(store.runs.reversed()) { run in
            NavigationLink { ToolRunDetailView(run: run) } label: { ToolRunRow(run: run) }
        }
        .overlay { if store.runs.isEmpty { Text("이번 통화에서 실행한 도구가 없어요").foregroundStyle(Theme.text3) } }
        .navigationTitle("실행한 도구")
    }
}

struct ToolRunRow: View {
    @ObservedObject var run: ToolRun
    var body: some View {
        HStack(spacing: Theme.s) {
            Image(systemName: run.glyph).foregroundStyle(Theme.indigo)
            VStack(alignment: .leading, spacing: 2) {
                Text(run.label).font(.subheadline).foregroundStyle(Theme.text).lineLimit(1)
                Text([run.tool, run.host].compactMap { $0 }.joined(separator: " · ")).font(.caption.monospaced()).foregroundStyle(Theme.text3).lineLimit(1)
            }
            Spacer()
            ToolRunStatus(run: run)
        }
    }
}

/// The sheet (iPhone) or popover (Mac) a tap on the activity line opens.
struct ToolRunSheet: View {
    @ObservedObject var store: ToolRunStore
    let target: ToolRunTarget
    var body: some View {
        NavigationStack {
            switch target {
            case .run(let id): if let r = store.run(id) { ToolRunDetailView(run: r) } else { ToolRunsView(store: store) }
            case .list: ToolRunsView(store: store)
            }
        }
        #if os(macOS)
        .frame(width: 520, height: 520)
        #endif
    }
}

extension View {
    /// Opens a run (or the list) on tap of the activity line: a popover on the Mac, a sheet on iPhone.
    func toolRuns(_ store: ToolRunStore, target: Binding<ToolRunTarget?>) -> some View {
        #if os(macOS)
        popover(item: target, arrowEdge: .bottom) { ToolRunSheet(store: store, target: $0) }
        #else
        sheet(item: target) { ToolRunSheet(store: store, target: $0).presentationDetents([.medium, .large]) }
        #endif
    }
}

extension ActivityLog {
    /// What a tap opens: the running step's detail, else the list.
    func tapTarget(_ store: ToolRunStore) -> ToolRunTarget {
        if let id = current?.callId, store.run(id) != nil { return .run(id) }
        return .list
    }
}
