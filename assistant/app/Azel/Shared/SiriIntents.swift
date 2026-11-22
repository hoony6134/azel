import AppIntents
#if os(iOS)
import UIKit
#endif

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
/// Mac: the call panel. Never opens the app, so it works on a locked iPhone (user report,
/// 2026-11-22: "기기 잠금을 해제해야 합니다" with AirPods in a pocket): when the app isn't open and
/// unlocked, the gateway rings back through the VoIP push, and an incoming CallKit call works
/// fully on the lock screen.
struct StartAzelCallIntent: AppIntent {
    static let title: LocalizedStringResource = "Azel 부르기"
    static let description = IntentDescription("Azel에게 전화를 걸어요.")
    static let openAppWhenRun = false
    static let authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    /// How a Siri request becomes a call.
    enum Route: Equatable {
        /// The app is open and unlocked: the CallKit outgoing call, as from the call button.
        case local
        /// Locked or in the background: `POST /v1/calls`; the gateway rings back.
        case ringback
    }

    static func route(appActive: Bool, protectedDataAvailable: Bool) -> Route {
        appActive && protectedDataAvailable ? .local : .ringback
    }

    static let ringbackReason = "사용자가 Siri로 불렀어요"

    static func ringbackBody(persona: Persona?) -> [String: Any] {
        var b: [String: Any] = ["reason": ringbackReason, "intent": "siri"]
        if let id = persona?.id { b["persona_id"] = id }
        return b
    }

    /// Asks the gateway to call now (call_user: the same daily cap and in-call refusal).
    static func ringback(persona: Persona?) async throws -> String {
        let r = ActionResult(try await Gateway.send("POST", "/v1/calls", json: ringbackBody(persona: persona)))
        if let q = r.needs?.question { return q }
        return r.ok ? "곧 전화드릴게요." : (r.spoken ?? "지금은 전화할 수 없어요.")
    }

    /// Optional: which persona answers; empty calls the primary.
    @Parameter(title: "비서") var persona: PersonaEntity?

    init() {}
    init(persona: PersonaEntity?) { self.persona = persona }

    /// Where the call starts (tests replace it); returns what Siri says.
    @MainActor static var startCall: (Persona?) async -> String = { p in
        #if os(iOS)
        switch route(appActive: UIApplication.shared.applicationState == .active,
                     protectedDataAvailable: UIApplication.shared.isProtectedDataAvailable) {
        case .local:
            CallManager.shared.startCall(persona: p)
            return ""
        case .ringback:
            do { return try await ringback(persona: p) } catch { return "\(AssistantName.current)에게 닿지 않아요." }
        }
        #else
        MacCall.shared.start(panel: true, persona: p)
        return ""
        #endif
    }

    @MainActor static func resolve(_ entity: PersonaEntity?) -> Persona? {
        let m = PersonaModel.shared
        return entity.flatMap { e in m.personas.first { $0.id == e.id } ?? m.match(name: e.name) } ?? m.primary
    }

    @MainActor func perform() async throws -> some IntentResult & ProvidesDialog {
        let said = await Self.startCall(Self.resolve(persona))
        return .result(dialog: IntentDialog(stringLiteral: said))
    }

    /// After each call, so Siri and Spotlight suggest calling this persona again.
    static func donate(persona: Persona?) {
        let e = persona.map { PersonaEntity(id: $0.id, name: $0.name) }
        Task { _ = try? await StartAzelCallIntent(persona: e).donate() }
    }
}

struct AzelShortcuts: AppShortcutsProvider {
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
