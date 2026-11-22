import WatchConnectivity

/// Hands the Apple Watch app what it needs to call on its own: the token and the server addresses.
/// The latest context replaces the previous one, so pushing on every foreground is cheap.
final class WatchSync: NSObject, WCSessionDelegate {
    static let shared = WatchSync()

    func start() {
        guard WCSession.isSupported() else { return }
        WCSession.default.delegate = self
        WCSession.default.activate()
    }

    @MainActor func push() {
        guard WCSession.isSupported() else { return }
        let s = WCSession.default
        guard s.activationState == .activated, s.isPaired, s.isWatchAppInstalled, let token = Config.token else { return }
        try? s.updateApplicationContext(["token": token, "voiceURL": Config.voiceURL, "gatewayURL": Config.gatewayURL,
                                         "name": AssistantName.current, "personas": personas()])
    }

    /// What the watch's picker shows: id, name, the orb glaze (dark-mode hex, the watch is always dark).
    @MainActor private func personas() -> [[String: Any]] {
        PersonaModel.shared.ordered.map { p in
            ["id": p.id, "name": p.name, "glaze": Int(p.customHex ?? p.tintKey.glazeHex), "primary": p.primary]
        }
    }

    func session(_ session: WCSession, activationDidCompleteWith state: WCSessionActivationState, error: Error?) {
        Task { @MainActor in push() }
    }
    func sessionWatchStateDidChange(_ session: WCSession) { Task { @MainActor in push() } }
    func sessionDidBecomeInactive(_ session: WCSession) {}
    func sessionDidDeactivate(_ session: WCSession) { session.activate() }   // switched to another watch
}
