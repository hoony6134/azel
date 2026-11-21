import Foundation
import Security

/// Server addresses live in UserDefaults; the token only ever lives in the Keychain.
enum Config {
    /// Running as the unit-test host: no real gateway polling, no windows popping up, no sound.
    static let isTestHost = ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
    /// TLS reverse proxy on deneb: `/v1/call` goes to the voice server, the rest to the gateway.
    static let publicVoiceURL = "wss://azel.scian.xyz", publicGatewayURL = "https://azel.scian.xyz"
    // HTTPS only (PM, 2026-11-21): azel.scian.xyz works with or without Tailscale, and ATS blocks
    // plain http/ws to the tailnet anyway. The advanced fields exist only for another TLS host.
    static let addressNote = "기본은 azel.scian.xyz예요. 다른 서버를 쓸 때만 바꾸세요 (wss://, https://만 돼요)."

    /// Tests point the app at a fake server without touching the real Keychain item or defaults.
    static var testOverride: (gateway: String, token: String)?

    static var voiceURL: String {
        get { usable(UserDefaults.standard.string(forKey: "voiceURL")) ?? publicVoiceURL }
        set { UserDefaults.standard.set(newValue, forKey: "voiceURL") }
    }
    static var gatewayURL: String {
        get { testOverride?.gateway ?? usable(UserDefaults.standard.string(forKey: "gatewayURL")) ?? publicGatewayURL }
        set { UserDefaults.standard.set(newValue, forKey: "gatewayURL") }
    }

    /// A saved address only if it is TLS (wss/https); anything else falls back to the defaults.
    static func usable(_ saved: String?) -> String? {
        guard let s = saved?.trimmingCharacters(in: .whitespaces), isTLS(s) else { return nil }
        return s
    }

    static func isTLS(_ s: String) -> Bool { s.hasPrefix("wss://") || s.hasPrefix("https://") }

    /// First launch after the update: older builds saved plain tailnet addresses; drop them
    /// silently so the azel defaults apply. Returns the keys it removed.
    @discardableResult
    static func migrateAddresses(_ store: UserDefaults = .standard) -> [String] {
        ["voiceURL", "gatewayURL"].filter { key in
            guard let s = store.string(forKey: key), usable(s) == nil else { return false }
            store.removeObject(forKey: key)
            return true
        }
    }

    /// Tokens get pasted with stray spaces and line breaks; none belong in one.
    static func cleanToken(_ raw: String) -> String {
        String(raw.unicodeScalars.filter { !CharacterSet.whitespacesAndNewlines.contains($0) }.map(Character.init))
    }
    static var token: String? {
        get { testOverride?.token ?? Keychain.read() }
        set { Keychain.write(newValue) }
    }
}

enum Keychain {
    private static let base: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: "xyz.scian.biseo",
        kSecAttrAccount as String: "token",
    ]

    static func read() -> String? {
        var query = base
        query[kSecReturnData as String] = true
        var out: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func write(_ value: String?) {
        SecItemDelete(base as CFDictionary)
        guard let value, !value.isEmpty else { return }
        var item = base
        item[kSecValueData as String] = Data(value.utf8)
        #if os(iOS)
        // AfterFirstUnlock so a VoIP push can connect while the phone is locked.
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        #endif
        SecItemAdd(item as CFDictionary, nil)
    }
}

/// Conversation id sent as `session` on outgoing calls: one per prefix per local day, so the server
/// keeps memory across calls that day. "새 대화" rotates it to a fresh id for the rest of the day.
enum Session {
    static func current(_ prefix: String = "iphone", now: Date = Date(), store: UserDefaults = .standard) -> String {
        let today = "\(prefix)-\(stamp("yyyy-MM-dd", now))"
        if let saved = store.string(forKey: "session.\(prefix)"), saved.hasPrefix(today) { return saved }
        store.set(today, forKey: "session.\(prefix)")
        return today
    }

    @discardableResult
    static func rotate(_ prefix: String = "iphone", now: Date = Date(), store: UserDefaults = .standard) -> String {
        let id = "\(prefix)-\(stamp("yyyy-MM-dd-HHmmss", now))"
        store.set(id, forKey: "session.\(prefix)")
        return id
    }

    private static func stamp(_ format: String, _ date: Date) -> String {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = format
        return f.string(from: date)
    }
}
