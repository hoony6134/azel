import Foundation
import UserNotifications

/// Notification buttons (ACTIONS.md §6): they call the same REST routes as the app.
/// The push sets `aps.category` to SCHEDULE (a scheduled call or reminder) or TASK (a running task),
/// with `schedule_id` + `occurrence` (ISO) or `task_id` in the payload.
enum PushAction {
    static let scheduleCategory = "SCHEDULE", taskCategory = "TASK"
    /// Approval pushes: APPROVAL with 항상 허용, APPROVAL_ONCE when the server allows only once.
    static let approvalCategory = "APPROVAL", approvalOnceCategory = "APPROVAL_ONCE"

    static var categories: Set<UNNotificationCategory> {
        [UNNotificationCategory(identifier: scheduleCategory, actions: [
            UNNotificationAction(identifier: "SNOOZE_10", title: "10분 뒤에", options: []),
            UNNotificationAction(identifier: "SKIP", title: "이번만 건너뛰기", options: [.destructive]),
         ], intentIdentifiers: []),
         UNNotificationCategory(identifier: taskCategory, actions: [
            UNNotificationAction(identifier: "STOP", title: "작업 중지", options: [.destructive]),
         ], intentIdentifiers: []),
         // Approving can run a command on a device: ask for Face ID / passcode first.
         UNNotificationCategory(identifier: approvalCategory, actions: [
            UNNotificationAction(identifier: "APPROVE_ONCE", title: "허용", options: [.authenticationRequired]),
            UNNotificationAction(identifier: "DENY", title: "거부", options: [.destructive]),
            UNNotificationAction(identifier: "APPROVE_ALWAYS", title: "항상 허용", options: [.authenticationRequired]),
         ], intentIdentifiers: []),
         UNNotificationCategory(identifier: approvalOnceCategory, actions: [
            UNNotificationAction(identifier: "APPROVE_ONCE", title: "허용", options: [.authenticationRequired]),
            UNNotificationAction(identifier: "DENY", title: "거부", options: [.destructive]),
         ], intentIdentifiers: [])]
    }

    struct Request: Equatable {
        var method: String
        var path: String
        var body: [String: AnyHashable]?   // real JSON types: `approve` must be a boolean, not "false"
    }

    static func request(action: String, userInfo: [AnyHashable: Any]) -> Request? {
        func string(_ k: String) -> String? { userInfo[k].map(describe).flatMap { $0.isEmpty ? nil : $0 } }
        switch action {
        case "SNOOZE_10", "SKIP":
            guard let id = string("schedule_id") ?? string("id"), let occ = string("occurrence") else { return nil }
            let path = ScheduleAPI.path(id) + "?occurrence=" + ScheduleAPI.query(occ)
            return action == "SKIP" ? Request(method: "DELETE", path: path)
                : Request(method: "PUT", path: path, body: ["when_text": "10분 뒤", "scope": "occurrence"])
        case "APPROVE_ONCE", "APPROVE_ALWAYS", "DENY":
            guard let id = ApprovalCenter.approvalID(from: userInfo) else { return nil }
            let d = action == "DENY" ? "deny" : action == "APPROVE_ALWAYS" ? "always" : "once"
            return Request(method: "POST", path: "/v1/approvals/\(id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id)",
                           body: ApprovalAPI.body(ApprovalDecision(rawValue: d)!).mapValues { $0 as! AnyHashable })
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
            let result = ActionResult(try await Gateway.send(r.method, r.path, json: r.body as [String: Any]?))
            note = result.needs?.question ?? (result.ok ? nil : result.spoken)
        } catch let error where ApprovalCenter.alreadyDecided(error) {
            note = nil   // decided on another device already: nothing to say
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
