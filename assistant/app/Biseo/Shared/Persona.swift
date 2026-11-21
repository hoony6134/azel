import SwiftUI

/// One assistant (up to 8). `GET/POST /v1/personas`, `GET/PUT/DELETE /v1/personas/{id}`,
/// `POST /v1/personas/{id}/primary`.
struct Persona: Codable, Identifiable, Equatable, Hashable {
    var id: String
    var name: String
    var voice: String?
    var voice_style: String?
    var personality: String?
    var color: String?
    var default_model: String?
    var share_memory: Bool
    var primary: Bool

    static let limit = 8

    enum CodingKeys: String, CodingKey { case id, name, voice, voice_style, personality, color, default_model, share_memory, primary }

    init(id: String = "", name: String, voice: String? = nil, voice_style: String? = nil, personality: String? = nil, color: String? = nil,
         default_model: String? = nil, share_memory: Bool = true, primary: Bool = false) {
        self.id = id; self.name = name; self.voice = voice; self.voice_style = voice_style; self.personality = personality
        self.color = color; self.default_model = default_model; self.share_memory = share_memory; self.primary = primary
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let s = try? c.decode(String.self, forKey: .id) { id = s } else { id = String(try c.decode(Int.self, forKey: .id)) }
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? AssistantName.fallback
        voice = try c.decodeIfPresent(String.self, forKey: .voice)
        voice_style = try c.decodeIfPresent(String.self, forKey: .voice_style)
        personality = try c.decodeIfPresent(String.self, forKey: .personality)
        color = try c.decodeIfPresent(String.self, forKey: .color)
        default_model = try c.decodeIfPresent(String.self, forKey: .default_model)
        share_memory = try c.decodeIfPresent(Bool.self, forKey: .share_memory) ?? true
        primary = try c.decodeIfPresent(Bool.self, forKey: .primary) ?? false
    }

    /// The persona's tint; the accent when the color is missing or unreadable.
    var tint: Color { Persona.color(hex: color) ?? Theme.celadon }

    /// "#82CBB4" / "82cbb4" → Color.
    static func color(hex: String?) -> Color? {
        guard var s = hex?.trimmingCharacters(in: .whitespaces) else { return nil }
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
        return Color(dark: v, light: v)
    }

    /// Swatches offered in the editor (the design's glaze family).
    static let palette = ["#82CBB4", "#9AAAF0", "#E9BC6E", "#F3A6A0", "#B7C98A", "#C6A2E8", "#8EC5E8", "#E8D7B0"]
}

enum PersonaAPI {
    static func list() async throws -> [Persona] {
        let data = try await Gateway.send("GET", "/v1/personas")
        if let l = try? JSONDecoder().decode([Persona].self, from: data) { return l }
        struct Envelope: Decodable { let personas: [Persona]?; let items: [Persona]? }
        let e = try JSONDecoder().decode(Envelope.self, from: data)
        return e.personas ?? e.items ?? []
    }

    /// The fields the server takes (no id/primary).
    static func body(_ p: Persona) -> [String: Any] {
        var b: [String: Any] = ["name": p.name, "share_memory": p.share_memory]
        b["voice"] = p.voice as Any? ?? NSNull(); b["voice_style"] = p.voice_style as Any? ?? NSNull()
        b["personality"] = p.personality as Any? ?? NSNull(); b["color"] = p.color as Any? ?? NSNull()
        b["default_model"] = p.default_model as Any? ?? NSNull()
        return b
    }

    static func create(_ p: Persona) async throws -> Persona {
        try JSONDecoder().decode(Persona.self, from: try await Gateway.send("POST", "/v1/personas", json: body(p)))
    }

    static func update(_ p: Persona) async throws {
        _ = try await Gateway.send("PUT", "/v1/personas/" + escape(p.id), json: body(p))
    }

    static func delete(_ id: String) async throws { _ = try await Gateway.send("DELETE", "/v1/personas/" + escape(id)) }
    static func makePrimary(_ id: String) async throws { _ = try await Gateway.send("POST", "/v1/personas/\(escape(id))/primary") }

    /// TTS sample of a voice (WAV).
    static func voiceSample(voice: String?, style: String?, text: String) async throws -> Data {
        let body: [String: Any] = ["voice": voice as Any? ?? NSNull(), "voice_style": style as Any? ?? NSNull(), "text": text]
        return try await Gateway.send("POST", "/v1/personas/voice-sample", json: body)
    }

    private static func escape(_ s: String) -> String { s.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? s }
}

/// Personas and which one the next call goes to. The choice survives launches; if that persona is
/// gone the primary takes over.
@MainActor
final class PersonaModel: ObservableObject {
    @Published private(set) var personas: [Persona] = []
    @Published private(set) var status: SectionStatus = .ok
    @Published var selectedID: String? {
        didSet { store.set(selectedID, forKey: Self.key) }
    }

    /// One instance per app: calls, Siri and history rows read it too.
    static let shared = PersonaModel()
    static let key = "selectedPersona"
    private let store: UserDefaults

    init(store: UserDefaults = .standard) {
        self.store = store
        selectedID = store.string(forKey: Self.key)
        if let data = store.data(forKey: "personasCache"), let cached = try? JSONDecoder().decode([Persona].self, from: data) {
            personas = cached
        }
    }

    var primary: Persona? { personas.first(where: \.primary) ?? personas.first }

    /// The persona for the next call: the chosen one if it still exists, else the primary.
    var selected: Persona? { Self.resolve(selectedID, in: personas) }

    static func resolve(_ id: String?, in list: [Persona]) -> Persona? {
        list.first { $0.id == id } ?? list.first(where: \.primary) ?? list.first
    }

    /// Name for CallKit and the call screen: the selected persona, else the cached assistant name.
    var callName: String { selected?.name ?? AssistantName.current }

    func persona(id: String?) -> Persona? { id.flatMap { id in personas.first { $0.id == id } } }

    /// Siri's optional "persona by name": exact, then case-insensitive prefix.
    func match(name: String?) -> Persona? {
        guard let name = name?.trimmingCharacters(in: .whitespaces), !name.isEmpty else { return nil }
        return personas.first { $0.name == name } ?? personas.first { $0.name.lowercased().hasPrefix(name.lowercased()) }
    }

    func refresh() async {
        guard Config.token != nil else { return }
        switch await attempt({ try await PersonaAPI.list() }) {
        case .success(let list):
            personas = list
            status = .ok
            if let data = try? JSONEncoder().encode(list) { store.set(data, forKey: "personasCache") }
            if let p = primary { AssistantName.current = p.name }
        case .failure(let e): status = SectionStatus.from(e) ?? status
        }
    }

    func save(_ p: Persona) async throws {
        if p.id.isEmpty { _ = try await PersonaAPI.create(p) } else { try await PersonaAPI.update(p) }
        await refresh()
    }

    func delete(_ p: Persona) async throws {
        try await PersonaAPI.delete(p.id)
        if selectedID == p.id { selectedID = nil }
        await refresh()
    }

    func makePrimary(_ p: Persona) async throws {
        try await PersonaAPI.makePrimary(p.id)
        await refresh()
    }
}
