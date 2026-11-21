import Foundation

#if os(macOS)
/// Files shared with the wake listener in `~/Library/Application Support/Azel/` (the real home, not
/// the sandbox container; an entitlement allows this one folder):
/// - `in-call`: a call is up (the listener stays deaf);
/// - `mic-live`: the call's mic is capturing (the listener may let go of the mic);
/// - `enrolling`: the wake-clip recorder is open (no wake detection);
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

    /// App launch and quit: nothing of ours is running.
    static func clearMarkers() { [inCall, micLive, enrolling].forEach { $0.remove() } }

    static func utterance(_ id: String) -> URL { folder.appendingPathComponent("wake-utterance-\(id).pcm") }

    /// Waits up to `timeout` for the listener to finish the file (it renames `.pcm.part` to `.pcm`),
    /// reads it and deletes both. nil when it never came or is empty.
    static func takeUtterance(_ id: String, timeout: TimeInterval = 5) async -> Data? {
        let url = utterance(id)
        let deadline = Date().addingTimeInterval(timeout)
        while !FileManager.default.fileExists(atPath: url.path) && Date() < deadline {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        let data = try? Data(contentsOf: url)
        try? FileManager.default.removeItem(at: url)
        try? FileManager.default.removeItem(at: url.appendingPathExtension("part"))
        guard let data, data.count >= 2 else { return nil }
        return data.count % 2 == 0 ? data : data.dropLast()   // whole 16-bit samples only
    }
}
#endif
