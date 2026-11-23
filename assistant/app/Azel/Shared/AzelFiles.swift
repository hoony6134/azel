import Foundation

#if os(macOS)
/// Files shared with the wake listener in `~/Library/Application Support/Azel/` (the real home, not
/// the sandbox container; an entitlement allows this one folder):
/// - `in-call`: a call is up (the listener stays deaf);
/// - `mic-live`: the call's mic is capturing (the listener may let go of the mic);
/// - `enrolling`: the wake-clip recorder is open (no wake detection);
/// - `listener-mic` (the listener's): its input stream is open; it closes it during a call or enrollment;
/// - `wake-utterance-<id>.pcm`: what the user said right after the name, PCM16LE mono 16 kHz.
enum AzelFiles {
    static var folder: URL = {
        let home = getpwuid(getuid()).flatMap { String(validatingUTF8: $0.pointee.pw_dir) } ?? NSHomeDirectory()
        return URL(fileURLWithPath: home).appendingPathComponent("Library/Application Support/Azel")
    }()

    struct Marker {
        let name: String
        var url: URL { AzelFiles.folder.appendingPathComponent(name) }

        func create() {
            try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            if !FileManager.default.createFile(atPath: url.path, contents: Data("\(getpid())\n".utf8)) {
                Log.call.error("marker not written: \(url.path, privacy: .public)")
            }
        }

        func remove() { try? FileManager.default.removeItem(at: url) }
        var exists: Bool { FileManager.default.fileExists(atPath: url.path) }
    }

    static let inCall = Marker(name: "in-call"), micLive = Marker(name: "mic-live"), enrolling = Marker(name: "enrolling")

    static let listenerMic = Marker(name: "listener-mic")

    /// Waits until the listener has closed its input stream (it does on the next 160 ms frame after
    /// `in-call` appears, or after it finished recording the utterance), at most `limit`. false = still
    /// marked: a crashed listener's leftover or an old listener; start anyway.
    static func listenerReleased(limit: TimeInterval = 1.5) async -> Bool {
        let start = Date()
        while listenerMic.exists {
            if Date().timeIntervalSince(start) >= limit { return false }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        return true
    }

    /// App launch and quit: nothing of ours is running.
    static func clearMarkers() { [inCall, micLive, enrolling].forEach { $0.remove() } }

    static func utterance(_ id: String) -> URL { folder.appendingPathComponent("wake-utterance-\(id).pcm") }

    /// The listener's capture (claude/wake-utterance): `.pcm.part` exists while it records (it stops
    /// at end of speech, when `mic-live` appears, or after 10 s), then it is renamed to `.pcm`; zero
    /// bytes = nothing was said. Waits while the `.part` exists (up to `partLimit`), or `appear` for
    /// either file to show up at all; reads the `.pcm` once and deletes both. nil = go live without it.
    /// `at`: when the finished file appeared (system uptime), i.e. when the listener stopped capturing.
    static func takeUtterance(_ id: String, appear: TimeInterval = 5, partLimit: TimeInterval = 12) async -> (data: Data?, at: Double?) {
        let url = utterance(id), part = url.appendingPathExtension("part")
        let fm = FileManager.default
        let start = Date()
        while !fm.fileExists(atPath: url.path) {
            let waited = Date().timeIntervalSince(start)
            if fm.fileExists(atPath: part.path) ? waited >= partLimit : waited >= appear { break }
            // 20 ms: the app's live mic is held (not dropped) meanwhile, and only its frames after the
            // file appeared are sent after it, so this poll sets the seam's precision, not a gap.
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        let at = fm.fileExists(atPath: url.path) ? ProcessInfo.processInfo.systemUptime : nil
        let data = try? Data(contentsOf: url)
        try? FileManager.default.removeItem(at: url)
        try? FileManager.default.removeItem(at: part)
        guard let data, data.count >= 2 else { return (nil, at) }
        return (data.count % 2 == 0 ? data : data.dropLast(), at)   // whole 16-bit samples only
    }
}
#endif
