import AppIntents

/// Same call with the "briefing" session prefix; the server decides what to say.
struct BriefingIntent: AppIntent {
    static let title: LocalizedStringResource = "오늘 브리핑 들려줘"
    static let openAppWhenRun = true

    @MainActor func perform() async throws -> some IntentResult {
        CallManager.shared.startCall(prefix: "briefing", persona: PersonaModel.shared.primary)
        return .result()
    }
}
