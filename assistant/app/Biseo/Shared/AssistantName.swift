import Foundation

/// The assistant's name is the server setting `assistant_name` (DESIGN §0). The app caches the
/// last value it saw so CallKit, menus and offline screens have it before the first fetch.
enum AssistantName {
    static let key = "assistantName"
    static let fallback = "아젤"

    static var current: String {
        get { UserDefaults.standard.string(forKey: key).flatMap(validated) ?? fallback }
        set { UserDefaults.standard.set(validated(newValue) ?? fallback, forKey: key) }
    }

    /// 1–12 characters of Hangul, Latin letters and spaces, trimmed; nil if not acceptable.
    static func validated(_ raw: String) -> String? {
        let name = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard (1...12).contains(name.count) else { return nil }
        let ok = name.unicodeScalars.allSatisfy { u in
            (0xAC00...0xD7A3).contains(u.value) || (0x3131...0x318E).contains(u.value)   // syllables, jamo
                || ("a"..."z").contains(u) || ("A"..."Z").contains(u) || u == " "
        }
        return ok ? name : nil
    }

    static let personalityLimit = 600

    /// What gets saved for `personality`: trimmed and cut to the limit; "" means the server default.
    static func personality(_ raw: String) -> String {
        String(raw.trimmingCharacters(in: .whitespacesAndNewlines).prefix(personalityLimit))
    }

    /// Korean particle after `name`: 이/가, 은/는, 을/를, 과/와, 아/야 by the last syllable's batchim.
    /// Non-Hangul endings count as no batchim (DESIGN §0).
    static func josa(_ name: String, _ withBatchim: String, _ without: String) -> String {
        guard let u = name.unicodeScalars.last, (0xAC00...0xD7A3).contains(u.value) else { return name + without }
        return name + ((u.value - 0xAC00) % 28 != 0 ? withBatchim : without)
    }
}
