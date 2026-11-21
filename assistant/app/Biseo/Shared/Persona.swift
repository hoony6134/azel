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
    /// 호칭 for this persona; nil/"" = follow the shared `user_title`.
    var user_title: String?

    static let limit = 8

    enum CodingKeys: String, CodingKey { case id, name, voice, voice_style, personality, color, default_model, share_memory, primary, user_title }

    init(id: String = "", name: String, voice: String? = nil, voice_style: String? = nil, personality: String? = nil, color: String? = nil,
         default_model: String? = nil, share_memory: Bool = true, primary: Bool = false, user_title: String? = nil) {
        self.id = id; self.name = name; self.voice = voice; self.voice_style = voice_style; self.personality = personality
        self.color = color; self.default_model = default_model; self.share_memory = share_memory; self.primary = primary
        self.user_title = user_title
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
        user_title = try? c.decodeIfPresent(String.self, forKey: .user_title)
    }

    static let personalityLimit = 500

    /// The persona's tint key (§17.2); unknown values fall back to 청자.
    var tintKey: PersonaTint { PersonaTint(rawValue: color ?? "") ?? PersonaTint.nearest(hex: color) ?? .celadon }
    /// A colour the user picked (#RRGGBB) that isn't one of the eight tints: from the server, or
    /// kept on this device while the gateway only accepts palette keys.
    var customHex: UInt32? {
        if let h = CustomTint.hex(color), PersonaTint.nearest(hex: color) == nil { return h }
        // The local colour counts only while the server still holds the stand-in tint we sent for it.
        guard !id.isEmpty, let l = CustomTint.local(id), color == CustomTint.nearestTint(l).rawValue else { return nil }
        return l
    }
    /// Only the colour field itself (the editor, where a swatch tap must win over the local colour).
    var pickedHex: UInt32? { PersonaTint.nearest(hex: color) == nil ? CustomTint.hex(color) : nil }
    var pickedGlaze: Color { pickedHex.map { Color(dark: $0, light: $0) } ?? tintKey.glaze }
    /// Text, chips and the call button.
    var tint: Color { customHex.map(CustomTint.accent) ?? tintKey.accent }
    /// The orb, mini orbs, idle/listening glaze and ring.
    var glaze: Color { customHex.map { Color(dark: $0, light: $0) } ?? tintKey.glaze }

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
        if let v = p.user_title, !v.isEmpty { b["user_title"] = v }   // "" = shared setting: left out
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

    /// Saves; if an older gateway refuses the 호칭 field, saves everything else and says so.
    /// Returns a note for the user when the 호칭 couldn't be saved.
    @discardableResult
    func save(_ p: Persona) async throws -> String? {
        var note: String?
        var p = p
        // A picked colour the gateway may not take yet (it validates palette keys): remember it here
        // and send the nearest tint, so other devices still get something close.
        let custom = CustomTint.hex(p.color).flatMap { PersonaTint.nearest(hex: p.color) == nil ? $0 : nil }
        if let custom { p.color = CustomTint.nearestTint(custom).rawValue }
        do {
            p = try await write(p)
        } catch GatewayError.rejected(let detail) where p.user_title?.isEmpty == false && Self.isUnknownField(detail) {
            var rest = p
            rest.user_title = nil
            p = try await write(rest)
            note = "호칭은 서버 업데이트 후 저장돼요"
        }
        if !p.id.isEmpty { CustomTint.setLocal(p.id, custom) }
        await refresh()
        return note
    }

    /// Returns the saved persona (with its new id after a create).
    private func write(_ p: Persona) async throws -> Persona {
        if p.id.isEmpty { return try await PersonaAPI.create(p) }
        try await PersonaAPI.update(p)
        return p
    }

    /// "unknown persona fields: user_title" and the like.
    static func isUnknownField(_ detail: String) -> Bool {
        let d = detail.lowercased()
        return d.contains("user_title") || d.contains("unknown") || d.contains("extra")
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
    var glazeHex: UInt32 { hex.glazeDark }
    var accent: Color { Color(dark: hex.accentDark, light: hex.accentLight) }

    /// Older builds stored hex colours: map a hex to the tint whose glaze it matches.
    static func nearest(hex: String?) -> PersonaTint? {
        guard var s = hex?.trimmingCharacters(in: .whitespaces) else { return nil }
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
        return allCases.first { $0.hex.glazeDark == v || $0.hex.glazeLight == v || $0.hex.accentLight == v }
    }
}

/// User-picked persona colours (#RRGGBB).
enum CustomTint {
    private static let key = "customPersonaColors"

    static func hex(_ s: String?) -> UInt32? {
        guard var s = s?.trimmingCharacters(in: .whitespaces), s.hasPrefix("#") else { return nil }
        s.removeFirst()
        return s.count == 6 ? UInt32(s, radix: 16) : nil
    }

    /// The sRGB value of a colour from the picker.
    static func hex(of c: Color) -> UInt32? {
        let r = c.resolve(in: EnvironmentValues())
        return pack((Double(r.red), Double(r.green), Double(r.blue)))
    }

    static func string(_ v: UInt32) -> String { String(format: "#%06X", v & 0xFFFFFF) }

    static func local(_ id: String) -> UInt32? { (UserDefaults.standard.dictionary(forKey: key)?[id] as? String).flatMap(hex) }

    static func setLocal(_ id: String, _ v: UInt32?) {
        var d = UserDefaults.standard.dictionary(forKey: key) ?? [:]
        d[id] = v.map(string)
        UserDefaults.standard.set(d, forKey: key)
    }

    private static func rgb(_ v: UInt32) -> (Double, Double, Double) {
        (Double((v >> 16) & 0xFF) / 255, Double((v >> 8) & 0xFF) / 255, Double(v & 0xFF) / 255)
    }

    private static func pack(_ c: (Double, Double, Double)) -> UInt32 {
        func b(_ x: Double) -> UInt32 { UInt32((min(1, max(0, x)) * 255).rounded()) }
        return b(c.0) << 16 | b(c.1) << 8 | b(c.2)
    }

    static func luminance(_ v: UInt32) -> Double {
        func lin(_ c: Double) -> Double { c <= 0.03928 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4) }
        let (r, g, b) = rgb(v)
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    }

    /// Mixes toward black (light mode) or white (dark mode) until text in it reads on the background
    /// (4.5:1 against the light bg F5F3EC / dark bg 0D1113).
    static func readable(_ v: UInt32, dark: Bool) -> UInt32 {
        let bg = dark ? luminance(0x0D1113) : luminance(0xF5F3EC)
        let (r, g, b) = rgb(v)
        for step in 0...20 {
            let t = Double(step) / 20, k = dark ? 1.0 : 0.0
            let c = pack((r + (k - r) * t, g + (k - g) * t, b + (k - b) * t))
            let l = luminance(c)
            if (max(l, bg) + 0.05) / (min(l, bg) + 0.05) >= 4.5 { return c }
        }
        return dark ? 0xFFFFFF : 0x000000
    }

    static func accent(_ v: UInt32) -> Color { Color(dark: readable(v, dark: true), light: readable(v, dark: false)) }

    /// The palette tint closest to a custom colour, for gateways that only take palette keys.
    static func nearestTint(_ v: UInt32) -> PersonaTint {
        let (r, g, b) = rgb(v)
        return PersonaTint.allCases.min { a, c in
            func d(_ t: PersonaTint) -> Double { let (x, y, z) = rgb(t.glazeHex); return (x - r) * (x - r) + (y - g) * (y - g) + (z - b) * (z - b) }
            return d(a) < d(c)
        }!
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
