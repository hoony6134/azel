import Foundation
import UserNotifications

/// Notification buttons (ACTIONS.md §6): they call the same REST routes as the app.
/// The push sets `aps.category` to SCHEDULE (a scheduled call or reminder) or TASK (a running task),
/// with `schedule_id` + `occurrence` (ISO) or `task_id` in the payload.
enum PushAction {
    static let scheduleCategory = "SCHEDULE", taskCategory = "TASK"

    static var categories: Set<UNNotificationCategory> {
        [UNNotificationCategory(identifier: scheduleCategory, actions: [
            UNNotificationAction(identifier: "SNOOZE_10", title: "10분 뒤에", options: []),
            UNNotificationAction(identifier: "SKIP", title: "이번만 건너뛰기", options: [.destructive]),
         ], intentIdentifiers: []),
         UNNotificationCategory(identifier: taskCategory, actions: [
            UNNotificationAction(identifier: "STOP", title: "작업 중지", options: [.destructive]),
         ], intentIdentifiers: [])]
    }

    struct Request: Equatable {
        var method: String
        var path: String
        var body: [String: String]?
    }

    static func request(action: String, userInfo: [AnyHashable: Any]) -> Request? {
        func string(_ k: String) -> String? { userInfo[k].map(describe).flatMap { $0.isEmpty ? nil : $0 } }
        switch action {
        case "SNOOZE_10", "SKIP":
            guard let id = string("schedule_id") ?? string("id"), let occ = string("occurrence") else { return nil }
            let path = ScheduleAPI.path(id) + "?occurrence=" + ScheduleAPI.query(occ)
            return action == "SKIP" ? Request(method: "DELETE", path: path)
                : Request(method: "PUT", path: path, body: ["when_text": "10분 뒤", "scope": "occurrence"])
        case "STOP":
            guard let id = TaskModel.taskID(from: userInfo) else { return nil }
            return Request(method: "POST", path: "/v1/tasks/\(id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id)/cancel")
        default: return nil
        }
    }

    /// Runs the button's request. Silent on success; otherwise a local notification with what the
    /// server said (its `spoken`, a question, or the error).
    static func perform(action: String, userInfo: [AnyHashable: Any]) async {
        guard let r = request(action: action, userInfo: userInfo) else { return }
        let note: String?
        do {
            let result = ActionResult(try await Gateway.send(r.method, r.path, json: r.body))
            note = result.needs?.question ?? (result.ok ? nil : result.spoken)
        } catch {
            note = (error as? GatewayError)?.errorDescription ?? FriendlyError.describe(error).title
        }
        guard let note else { return }
        let c = UNMutableNotificationContent()
        c.title = AssistantName.current
        c.body = note
        try? await UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
    }
}
