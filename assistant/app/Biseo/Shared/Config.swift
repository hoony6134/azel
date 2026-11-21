import Foundation
import Security

/// Server addresses live in UserDefaults; the token only ever lives in the Keychain.
enum Config {
    /// TLS reverse proxy on deneb: `/v1/call` goes to the voice server, the rest to the gateway.
    static let publicVoiceURL = "wss://azel.scian.xyz", publicGatewayURL = "https://azel.scian.xyz"
    // No tailnet shortcut: ATS blocks plain http/ws to the tailnet (checked 2026-11-21: iOS refuses
    // 100.64.x.x; macOS lets only this Mac's own 100.64.0.8 through, not vd-spark's voice server).
    static let addressNote = "기본은 공개 주소(TLS)예요. 암호화되지 않은 Tailnet 주소(http/ws)는 앱 보안 정책 때문에 쓸 수 없어요."

    static var voiceURL: String {
        get { usable(UserDefaults.standard.string(forKey: "voiceURL")) ?? publicVoiceURL }
        set { UserDefaults.standard.set(newValue, forKey: "voiceURL") }
    }
    static var gatewayURL: String {
        get { usable(UserDefaults.standard.string(forKey: "gatewayURL")) ?? publicGatewayURL }
        set { UserDefaults.standard.set(newValue, forKey: "gatewayURL") }
    }

    /// A saved address, unless it can't work here: blank, or (iOS) plain ws/http, which ATS
    /// blocks. Older iPhone builds saved the tailnet addresses, so those fall back to the public ones.
    /// macOS keeps them: a plain address to this Mac itself still passes ATS there.
    static func usable(_ saved: String?, iOS: Bool = isIOS) -> String? {
        guard let s = saved?.trimmingCharacters(in: .whitespaces), !s.isEmpty else { return nil }
        if iOS, s.hasPrefix("ws://") || s.hasPrefix("http://") { return nil }
        return s
    }

    #if os(iOS)
    static let isIOS = true
    #else
    static let isIOS = false
    #endif

    /// Tokens get pasted with stray spaces and line breaks; none belong in one.
    static func cleanToken(_ raw: String) -> String {
        String(raw.unicodeScalars.filter { !CharacterSet.whitespacesAndNewlines.contains($0) }.map(Character.init))
    }
    static var token: String? {
        get { Keychain.read() }
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
