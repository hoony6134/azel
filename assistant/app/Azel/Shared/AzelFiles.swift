import Foundation

#if os(macOS)
/// Files shared with the wake listener in `~/Library/Application Support/Azel/` (the real home, not
/// the sandbox container; an entitlement allows this one folder):
/// - `in-call`: a call is up (the listener stays deaf);
/// - `mic-live`: the call's mic is capturing (the listener may let go of the mic);
/// - `enrolling`: the wake-clip recorder is open (no wake detection);
/// - `listener-mic` (the listener's): its input stream is open; it closes it during a call or enrollment;
/// - `wake-stream-<id>.pcm`: what the listener hears from the wake on, growing live until our mic is live.
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

    static func stream(_ id: String) -> URL { folder.appendingPathComponent("wake-stream-\(id).pcm") }

    /// Forwards the listener's live stream (`wake-stream-<id>.pcm`, PCM16LE mono 16 kHz, appended frame by frame from
    /// the wake on) as it grows, so the server hears one natural utterance at real-time pace, not a recorded burst
    /// (2026-11-23: a ~10 s wait for the recording, a burst split into turns). Ends once the listener wrote
    /// `.done` (it stops on the frame after our `mic-live`) and the rest is sent, or after `limit`; then deletes the
    /// files. Returns when it ended (system uptime): our held live frames from then on follow it. nil = no stream
    /// appeared within `appear` (the listener couldn't write it): send all of our own audio.
    static func followStream(_ id: String, appear: TimeInterval = 2, limit: TimeInterval = 16,
                             send: @MainActor (Data) -> Void) async -> Double? {
        let url = stream(id), done = url.deletingPathExtension().appendingPathExtension("done")
        let start = Date()
        defer { for ext in ["pcm", "done", "acked"] { try? FileManager.default.removeItem(at: url.deletingPathExtension().appendingPathExtension(ext)) } }
        var handle: FileHandle?
        while handle == nil {
            handle = try? FileHandle(forReadingFrom: url)
            if handle == nil {
                if Date().timeIntervalSince(start) >= appear { return nil }
                try? await Task.sleep(nanoseconds: 20_000_000)
            }
        }
        guard let handle else { return nil }
        defer { try? handle.close() }
        var carry = Data()   // an odd byte waits for its pair: whole 16-bit samples only
        while true {
            let finished = FileManager.default.fileExists(atPath: done.path) || Date().timeIntervalSince(start) >= limit
            let chunk = carry + ((try? handle.readToEnd()) ?? Data())   // after `finished`: everything written before .done
            let whole = chunk.count - chunk.count % 2
            if whole > 0 { await send(chunk.prefix(whole)) }
            carry = chunk.suffix(from: chunk.startIndex + whole)
            if finished { return ProcessInfo.processInfo.systemUptime }
            // 20 ms: the listener writes a 160 ms frame at a time, so this forwards at the pace it was spoken.
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    /// The listener's "네?" has played (`.acked`): our voice processing starts only after it (the 3 s stall of
    /// 2026-11-23 coincided with that sound). Gives up after `limit`; with no stream file at all, at once.
    static func streamAcked(_ id: String, limit: TimeInterval = 1.5) async {
        let acked = stream(id).deletingPathExtension().appendingPathExtension("acked")
        let start = Date()
        while !FileManager.default.fileExists(atPath: acked.path), Date().timeIntervalSince(start) < limit {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }
}
#endif
