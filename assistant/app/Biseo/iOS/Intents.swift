import AppIntents

/// "시리야, 박비서" → opens the app and places the CallKit call through CallManager.
struct StartCallIntent: AppIntent {
    static let title: LocalizedStringResource = "박비서 부르기"
    static let openAppWhenRun = true

    @MainActor func perform() async throws -> some IntentResult {
        CallManager.shared.startCall()
        return .result()
    }
}

/// Same call with the "briefing" session prefix; the server decides what to say.
struct BriefingIntent: AppIntent {
    static let title: LocalizedStringResource = "오늘 브리핑 들려줘"
    static let openAppWhenRun = true

    @MainActor func perform() async throws -> some IntentResult {
        CallManager.shared.startCall(prefix: "briefing")
        return .result()
    }
}

struct BiseoShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: StartCallIntent(),
                    phrases: ["\(.applicationName)", "\(.applicationName) 불러", "\(.applicationName) 불러줘", "\(.applicationName)에게 전화해"],
                    shortTitle: "박비서 부르기", systemImageName: "phone.fill")
        AppShortcut(intent: BriefingIntent(),
                    phrases: ["\(.applicationName) 오늘 브리핑 들려줘", "\(.applicationName) 브리핑"],
                    shortTitle: "오늘 브리핑", systemImageName: "sun.max.fill")
    }
}
