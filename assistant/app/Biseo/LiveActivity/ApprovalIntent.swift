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

/// The same decision, but one that grants power: 항상 허용, or 허용 on an approval the server marks
/// forced-once or risky. iOS asks for Face ID, the passcode, or an unlocked Apple Watch first.
struct ApprovalAuthDecisionIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "승인 결정 (잠금 해제 필요)"
    static var openAppWhenRun = false
    static var authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication

    @Parameter(title: "승인") var approvalID: String
    @Parameter(title: "결정") var decision: String   // once | always

    init() {}
    init(approvalID: String, decision: String) { self.approvalID = approvalID; self.decision = decision }

    func perform() async throws -> some IntentResult {
        try await ApprovalDecisionIntent(approvalID: approvalID, decision: decision).perform()
        return .result()
    }
}

extension PendingApproval {
    /// 허용 here needs authentication (forced-once or risky); 항상 허용 always does; 거부 never.
    var onceNeedsAuth: Bool { forcedOnce || risky }
}
