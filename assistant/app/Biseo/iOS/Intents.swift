import AppIntents

/// "시리야, Azel 불러" → opens the app and places the CallKit call through CallManager.
struct StartCallIntent: AppIntent {
    static let title: LocalizedStringResource = "전화 걸기"
    static let openAppWhenRun = true

    /// Optional: call a persona by name; empty calls the primary.
    @Parameter(title: "비서") var persona: String?

    @MainActor func perform() async throws -> some IntentResult {
        CallManager.shared.startCall(persona: PersonaModel.shared.match(name: persona) ?? (persona == nil ? PersonaModel.shared.primary : nil))
        return .result()
    }
}

/// Same call with the "briefing" session prefix; the server decides what to say.
struct BriefingIntent: AppIntent {
    static let title: LocalizedStringResource = "오늘 브리핑 들려줘"
    static let openAppWhenRun = true

    @MainActor func perform() async throws -> some IntentResult {
        CallManager.shared.startCall(prefix: "briefing", persona: PersonaModel.shared.primary)
        return .result()
    }
}

struct BiseoShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: StartCallIntent(),
                    phrases: ["\(.applicationName)", "\(.applicationName) 불러", "\(.applicationName) 불러줘", "\(.applicationName)에게 전화해"],
                    shortTitle: "전화 걸기", systemImageName: "phone.fill")
        AppShortcut(intent: BriefingIntent(),
                    phrases: ["\(.applicationName) 오늘 브리핑 들려줘", "\(.applicationName) 브리핑"],
                    shortTitle: "오늘 브리핑", systemImageName: "sun.max.fill")
    }
}
