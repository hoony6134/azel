import AppIntents

/// A persona Siri can name ("Azel로 미아 불러줘"); the list comes from the cached personas.
struct PersonaEntity: AppEntity, Identifiable {
    static var typeDisplayRepresentation: TypeDisplayRepresentation = "비서"
    static var defaultQuery = PersonaQuery()

    var id: String
    var name: String

    var displayRepresentation: DisplayRepresentation { DisplayRepresentation(title: "\(name)") }
}

struct PersonaQuery: EntityQuery {
    @MainActor private func all() -> [PersonaEntity] { PersonaModel.shared.ordered.map { PersonaEntity(id: $0.id, name: $0.name) } }

    func entities(for identifiers: [String]) async throws -> [PersonaEntity] {
        await all().filter { identifiers.contains($0.id) }
    }

    func suggestedEntities() async throws -> [PersonaEntity] { await all() }
}

/// "시리야, 아젤 불러줘": starts a call. iPhone: the CallKit outgoing call into the call screen;
/// Mac: the call panel. From the lock screen the system asks to unlock first (the app opens).
struct StartAzelCallIntent: AppIntent {
    static let title: LocalizedStringResource = "Azel 부르기"
    static let description = IntentDescription("Azel에게 전화를 걸어요.")
    static let openAppWhenRun = true

    /// Optional: which persona answers; empty calls the primary.
    @Parameter(title: "비서") var persona: PersonaEntity?

    init() {}
    init(persona: PersonaEntity?) { self.persona = persona }

    /// Where the call starts (tests replace it).
    @MainActor static var startCall: (Persona?) -> Void = { p in
        #if os(iOS)
        CallManager.shared.startCall(persona: p)
        #else
        MacCall.shared.start(panel: true, persona: p)
        #endif
    }

    @MainActor static func resolve(_ entity: PersonaEntity?) -> Persona? {
        let m = PersonaModel.shared
        return entity.flatMap { e in m.personas.first { $0.id == e.id } ?? m.match(name: e.name) } ?? m.primary
    }

    @MainActor func perform() async throws -> some IntentResult {
        Self.startCall(Self.resolve(persona))
        return .result()
    }

    /// After each call, so Siri and Spotlight suggest calling this persona again.
    static func donate(persona: Persona?) {
        let e = persona.map { PersonaEntity(id: $0.id, name: $0.name) }
        Task { _ = try? await StartAzelCallIntent(persona: e).donate() }
    }
}

struct BiseoShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: StartAzelCallIntent(),
                    phrases: ["\(.applicationName)", "\(.applicationName) 불러", "\(.applicationName) 불러줘",
                              "\(.applicationName)이랑 통화해줘", "\(.applicationName)한테 전화해줘", "\(.applicationName)에게 전화해",
                              "\(.applicationName) 켜줘",
                              "\(.applicationName)로 \(\.$persona) 불러줘", "\(.applicationName)에서 \(\.$persona) 불러줘"],
                    shortTitle: "Azel 부르기", systemImageName: "phone.fill")
        #if os(iOS)
        AppShortcut(intent: BriefingIntent(),
                    phrases: ["\(.applicationName) 오늘 브리핑 들려줘", "\(.applicationName) 브리핑"],
                    shortTitle: "오늘 브리핑", systemImageName: "sun.max.fill")
        #endif
    }
}
