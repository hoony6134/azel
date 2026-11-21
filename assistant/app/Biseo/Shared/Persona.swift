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

    static let personalityLimit = 500

    /// The persona's tint key (§17.2); unknown values fall back to 청자.
    var tintKey: PersonaTint { PersonaTint(rawValue: color ?? "") ?? PersonaTint.nearest(hex: color) ?? .celadon }
    /// Text, chips and the call button.
    var tint: Color { tintKey.accent }
    /// The orb, mini orbs, idle/listening glaze and ring.
    var glaze: Color { tintKey.glaze }

    /// Swatch keys offered in the editor, in order.
    static let palette = PersonaTint.allCases.map(\.rawValue)
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
    /// The fields the server takes (no id/primary); unset optionals are left out rather than sent
    /// as null, which the deployed gateway rejects.
    static func body(_ p: Persona) -> [String: Any] {
        var b: [String: Any] = ["name": p.name, "share_memory": p.share_memory]
        if let v = p.voice { b["voice"] = v }
        if let v = p.voice_style { b["voice_style"] = v }
        if let v = p.personality { b["personality"] = v }
        if let v = p.color { b["color"] = v }
        if let v = p.default_model { b["default_model"] = v }
        return b
    }

    static func create(_ p: Persona) async throws -> Persona {
        try JSONDecoder().decode(Persona.self, from: try await Gateway.send("POST", "/v1/personas", json: body(p)))
    }

    static func update(_ p: Persona) async throws {
        _ = try await Gateway.send("PUT", "/v1/personas/" + escape(p.id), json: body(p))
    }

    static func deletePath(_ id: String, deleteHistory: Bool) -> String {
        "/v1/personas/\(escape(id))?delete_history=\(deleteHistory)"
    }

    static func delete(_ id: String, deleteHistory: Bool = false) async throws {
        _ = try await Gateway.send("DELETE", deletePath(id, deleteHistory: deleteHistory))
    }

    static func voices() async throws -> [Voice] {
        try JSONDecoder().decode([Voice].self, from: try await Gateway.send("GET", "/v1/voices"))
    }
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

    /// Display order (§17.4 reorder is local to this device); new personas go last.
    var ordered: [Persona] {
        let order = store.stringArray(forKey: "personaOrder") ?? []
        return personas.sorted { a, b in
            (order.firstIndex(of: a.id) ?? Int.max, a.primary ? 0 : 1) < (order.firstIndex(of: b.id) ?? Int.max, b.primary ? 0 : 1)
        }
    }

    func move(_ p: Persona, by offset: Int) {
        var ids = ordered.map(\.id)
        guard let i = ids.firstIndex(of: p.id), ids.indices.contains(i + offset) else { return }
        ids.swapAt(i, i + offset)
        store.set(ids, forKey: "personaOrder")
        objectWillChange.send()
    }

    /// History label for a persona id: its name, or "삭제된 비서" when it no longer exists.
    func label(for id: String?, deleted: Bool = false) -> (name: String, tint: Color)? {
        guard let id else { return nil }
        if !deleted, let p = persona(id: id) { return (p.name, p.tint) }
        return ("삭제된 비서", PersonaTint.ash.accent)
    }

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

    func delete(_ p: Persona, deleteHistory: Bool = false) async throws {
        try await PersonaAPI.delete(p.id, deleteHistory: deleteHistory)
        if selectedID == p.id { selectedID = nil }
        await refresh()
    }

    func makePrimary(_ p: Persona) async throws {
        try await PersonaAPI.makePrimary(p.id)
        await refresh()
    }
}

/// The eight curated orb tints (DESIGN §17.2): glaze for the orb, accent for text, chips and the
/// call button, per mode. Every accent passes AA on bg and card.
enum PersonaTint: String, CaseIterable, Identifiable {
    case celadon, indigo, amber, azalea, sprout, sky, plum, ash
    var id: String { rawValue }

    var name: String {
        switch self {
        case .celadon: return "청자"
        case .indigo: return "쪽빛"
        case .amber: return "호박"
        case .azalea: return "진달래"
        case .sprout: return "연두"
        case .sky: return "하늘"
        case .plum: return "자주"
        case .ash: return "먹"
        }
    }

    private var hex: (glazeDark: UInt32, glazeLight: UInt32, accentDark: UInt32, accentLight: UInt32) {
        switch self {
        case .celadon: return (0x82CBB4, 0xA8DCCB, 0x82CBB4, 0x2C7663)
        case .indigo: return (0x9AAAF0, 0xB4BFF3, 0x9AAAF0, 0x4656B8)
        case .amber: return (0xE9BC6E, 0xF0CB86, 0xE9BC6E, 0x8F5E12)
        case .azalea: return (0xF2A7B8, 0xF6C1CD, 0xF2A7B8, 0xA63D5A)
        case .sprout: return (0xB9D77A, 0xCDE39C, 0xB9D77A, 0x55721C)
        case .sky: return (0x8FCBEA, 0xB3DCF1, 0x8FCBEA, 0x28698C)
        case .plum: return (0xC4A2E6, 0xD6BEEE, 0xC4A2E6, 0x6E4A9A)
        case .ash: return (0xB8C0BD, 0xCDD3D1, 0xB8C0BD, 0x56605D)
        }
    }

    var glaze: Color { Color(dark: hex.glazeDark, light: hex.glazeLight) }
    var accent: Color { Color(dark: hex.accentDark, light: hex.accentLight) }

    /// Older builds stored hex colours: map a hex to the tint whose glaze it matches.
    static func nearest(hex: String?) -> PersonaTint? {
        guard var s = hex?.trimmingCharacters(in: .whitespaces) else { return nil }
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
        return allCases.first { $0.hex.glazeDark == v || $0.hex.glazeLight == v || $0.hex.accentLight == v }
    }
}

/// `GET /v1/voices`: the TTS speakers for the voice picker.
struct Voice: Decodable, Identifiable, Equatable {
    var id: String
    var label: String
    var desc: String?
    var lang: String?

    private enum Keys: String, CodingKey { case id, label, desc, lang }
    init(id: String, label: String, desc: String? = nil, lang: String? = nil) { self.id = id; self.label = label; self.desc = desc; self.lang = lang }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        id = try c.decode(String.self, forKey: .id)
        label = try c.decodeIfPresent(String.self, forKey: .label) ?? id
        desc = try c.decodeIfPresent(String.self, forKey: .desc)
        lang = try c.decodeIfPresent(String.self, forKey: .lang)
    }
}
