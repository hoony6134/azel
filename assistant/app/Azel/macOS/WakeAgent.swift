import Foundation

/// The Python wake-word listener (`assistant/wake/`) runs as the launchd agent below; this only
/// reports and toggles it. It is never reimplemented here.
enum WakeAgent {
    enum State: Equatable { case running, loaded, stopped, notInstalled, unknown(String) }

    static let label = "xyz.scian.biseo-wake"
    static let installHint = """
        호출어 리스너가 설치되어 있지 않습니다. assistant/wake 디렉터리에서 INSTALL.md 3단계를 실행하세요:

        sed "s|__WAKE_DIR__|$PWD|g" xyz.scian.biseo-wake.plist > ~/Library/LaunchAgents/xyz.scian.biseo-wake.plist
        launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/xyz.scian.biseo-wake.plist

        처음 실행하면 macOS가 "uv"의 마이크 접근을 묻습니다. 허용을 눌러 주세요.
        """

    /// The real home, not the sandbox container's.
    static var plist: String {
        String(cString: getpwuid(getuid())!.pointee.pw_dir) + "/Library/LaunchAgents/\(label).plist"
    }
    private static var domain: String { "gui/\(getuid())" }

    /// Blocking (runs launchctl): call off the main thread.
    static func state() -> State {
        let (code, out) = launchctl(["print", "\(domain)/\(label)"])
        if code == 0 { return out.contains("state = running") ? .running : .loaded }
        if !FileManager.default.fileExists(atPath: plist) { return .notInstalled }
        return code == 113 ? .stopped : .unknown(out.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    /// Blocking. Loaded → bootout; stopped → bootstrap. Returns launchctl's message on failure.
    static func toggle(_ current: State) -> String? {
        let args = current == .running || current == .loaded
            ? ["bootout", "\(domain)/\(label)"]
            : ["bootstrap", domain, plist]
        let (code, out) = launchctl(args)
        return code == 0 ? nil : "launchctl \(args[0]) 실패 (\(code)): \(out)"
    }

    private static func launchctl(_ args: [String]) -> (Int32, String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        do { try p.run() } catch { return (-1, error.localizedDescription) }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (p.terminationStatus, String(decoding: data, as: UTF8.self))
    }
}
