import ActivityKit
import Combine
import Foundation

/// Starts, updates and ends the call and task Live Activities from the app's own state.
/// shortcut: task activities update only while the app runs; background updates need the gateway to
/// send ActivityKit pushes (not built yet).
@MainActor
final class LiveActivities {
    static let shared = LiveActivities()
    private var call: ActivityKit.Activity<CallActivityAttributes>?
    private var tasks: [String: ActivityKit.Activity<TaskActivityAttributes>] = [:]
    private var bag: Set<AnyCancellable> = []
    private var lastCall: CallActivityAttributes.ContentState?

    func start() {
        guard bag.isEmpty else { return }
        // Coalesce the many published changes of a call into at most ~2 updates a second.
        CallManager.shared.objectWillChange
            .throttle(for: .milliseconds(500), scheduler: RunLoop.main, latest: true)
            .sink { [weak self] _ in DispatchQueue.main.async { self?.syncCall() } }
            .store(in: &bag)
        TaskModel.shared.$tasks
            .receive(on: RunLoop.main)
            .sink { [weak self] in self?.syncTasks($0) }
            .store(in: &bag)
        // Activities left over from a previous run (the app was killed mid-call).
        for a in ActivityKit.Activity<CallActivityAttributes>.activities { Task { await a.end(nil, dismissalPolicy: .immediate) } }
        for a in ActivityKit.Activity<TaskActivityAttributes>.activities { tasks[a.attributes.taskID] = a }
        // Tasks that finished (or vanished) while the app was closed: end their cards now.
        Task { [weak self] in
            guard let self, !tasks.isEmpty, case .success(let list) = await attempt({ try await TaskAPI.list() }) else { return }
            for id in Self.stale(Array(tasks.keys), tasks: list) {
                guard let a = tasks.removeValue(forKey: id) else { continue }
                let t = list.first { $0.id == id }
                let final = t.map { TaskActivityAttributes.ContentState(status: $0.status, step: $0.steps.last?.label, steps: $0.steps.count) }
                await a.end(final.map { .init(state: $0, staleDate: nil) }, dismissalPolicy: .immediate)
            }
        }
    }

    /// Activity task ids whose task is no longer running (finished, cancelled, or gone).
    nonisolated static func stale(_ ids: [String], tasks: [AgentTask]) -> [String] {
        ids.filter { id in !(tasks.first { $0.id == id }?.isActive ?? false) }
    }

    private var enabled: Bool { ActivityAuthorizationInfo().areActivitiesEnabled }

    static func glaze(_ p: Persona?) -> UInt32 { p.map { $0.customHex ?? $0.tintKey.glazeHex } ?? PersonaTint.celadon.glazeHex }

    static func callState(_ c: CallManager) -> CallActivityAttributes.ContentState {
        let last = c.lines.last
        return .init(state: c.held ? "held" : (c.connectedAt == nil ? "connecting" : c.displayState),
                     caption: last.map { String($0.text.suffix(120)) }, mine: last?.mine ?? false, muted: c.muted)
    }

    private func syncCall() {
        let c = CallManager.shared
        if c.inCall {
            let s = Self.callState(c)
            if let call {
                guard s != lastCall else { return }
                lastCall = s
                Task { await call.update(.init(state: s, staleDate: nil)) }
            } else if enabled {
                let p = c.activePersona ?? PersonaModel.shared.selected
                let attrs = CallActivityAttributes(name: p?.name ?? AssistantName.current, glaze: Self.glaze(p), startedAt: Date())
                call = try? ActivityKit.Activity.request(attributes: attrs, content: .init(state: s, staleDate: nil))
                lastCall = s
            }
        } else if let call {
            self.call = nil
            lastCall = nil
            Task { await call.end(nil, dismissalPolicy: .immediate) }
        }
    }

    private func syncTasks(_ list: [AgentTask]) {
        for t in list {
            let s = TaskActivityAttributes.ContentState(status: t.status, step: t.steps.last?.label, steps: t.steps.count)
            if let a = tasks[t.id] {
                if t.isActive {
                    if a.content.state != s { Task { await a.update(.init(state: s, staleDate: nil)) } }
                } else {
                    tasks[t.id] = nil
                    // The result stays on the Lock Screen a while so it can be seen.
                    Task { await a.end(.init(state: s, staleDate: nil), dismissalPolicy: .after(Date().addingTimeInterval(15 * 60))) }
                }
            } else if t.isActive, enabled, tasks.count < 3 {
                let p = PersonaModel.shared.persona(id: t.persona) ?? PersonaModel.shared.selected
                let attrs = TaskActivityAttributes(taskID: t.id, goal: t.goal, glaze: Self.glaze(p))
                tasks[t.id] = (try? ActivityKit.Activity.request(attributes: attrs, content: .init(state: s, staleDate: nil)))
                    ?? ActivityKit.Activity<TaskActivityAttributes>.activities.first { $0.attributes.taskID == t.id }
            }
        }
    }

    #if DEBUG
    /// `-liveActivityPreview`: sample call and task activities for screenshots.
    func debugPreview() {
        let p = PersonaModel.shared.selected
        _ = try? ActivityKit.Activity.request(
            attributes: CallActivityAttributes(name: p?.name ?? "아젤", glaze: Self.glaze(p), startedAt: Date().addingTimeInterval(-83)),
            content: .init(state: .init(state: "speaking", caption: "내일 오전 10시에 팀 회의가 있고, 오후 3시까지 과제 마감이에요.", mine: false, muted: false), staleDate: nil))
        _ = try? ActivityKit.Activity.request(
            attributes: TaskActivityAttributes(taskID: "demo", goal: "이번 주 과제 마감 정리해서 노션에 올리기", glaze: PersonaTint.indigo.glazeHex),
            content: .init(state: .init(status: "running", step: "노션 페이지 만드는 중", steps: 3), staleDate: nil))
    }
    #endif
}
