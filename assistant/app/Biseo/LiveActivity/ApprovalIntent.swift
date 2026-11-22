import AppIntents
import Foundation

/// 허용 / 거부 / 항상 허용 on the Lock Screen and in the Dynamic Island. A LiveActivityIntent runs in
/// the app's process, so the decision goes out with the app's token; the widget only shows it.
struct ApprovalDecisionIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "승인 결정"
    static var openAppWhenRun = false

    @Parameter(title: "승인") var approvalID: String
    @Parameter(title: "결정") var decision: String   // once | deny | always

    init() {}
    init(approvalID: String, decision: String) { self.approvalID = approvalID; self.decision = decision }

    func perform() async throws -> some IntentResult {
        #if !WIDGET
        guard let d = ApprovalDecision(rawValue: decision) else { return .result() }
        do { try await ApprovalAPI.decide(approvalID, d) }
        catch let error where ApprovalCenter.alreadyDecided(error) { _ = error }   // answered elsewhere: the buttons just go away
        await ApprovalCenter.shared.refresh()
        await LiveActivities.shared.syncNow()
        #endif
        return .result()
    }
}
