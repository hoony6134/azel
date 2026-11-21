import Foundation

/// One step the assistant is taking (DESIGN §13), from an `activity` event.
struct Activity: Equatable, Codable {
    var label: String
    var glyph: String

    /// §13.2: the event's `kind` first; for missing/unknown kinds, a substring match on `tool`.
    static func glyph(kind: String?, tool: String?) -> String {
        let byKind = ["calendar": "calendar", "mail": "envelope", "memory": "brain", "schedule": "checklist",
                      "server": "server.rack", "files": "doc.text", "notes": "note.text", "web": "globe",
                      "terminal": "apple.terminal", "generic": "ellipsis.circle"]
        if let kind, let g = byKind[kind] {
            if kind == "terminal", let t = tool?.lowercased(), t.contains("device") || t.contains("delegate") { return "laptopcomputer" }
            return g
        }
        let t = tool?.lowercased() ?? ""
        let fallback: [(String, [String])] = [
            ("calendar", ["calendar", "gcal", "event"]), ("envelope", ["mail", "gmail"]),
            ("brain", ["memory", "recall", "cygnus", "context"]), ("checklist", ["gistra", "planit", "todo", "task"]),
            ("server.rack", ["status", "infra", "server", "alert", "uptime"]),
            ("doc.text", ["read_file", "list_dir", "search", "drive", "file", "doc"]), ("note.text", ["notion", "note"]),
            ("globe", ["browse", "web", "fetch", "http"]), ("apple.terminal", ["run_command", "delegate", "job", "shell"]),
        ]
        return fallback.first { $0.1.contains { t.contains($0) } }?.0 ?? "ellipsis.circle"
    }

    /// Done-step wording: the tool_end label, else the start label without a trailing " 중".
    static func doneLabel(end: String?, start: String?) -> String {
        if let end, !end.isEmpty { return end }
        let s = start ?? ""
        return s.hasSuffix(" 중") ? String(s.dropLast(2)) : s
    }
}

/// Sounds the activity flow asks for (§13.1); the earcon scheduler decides whether they play.
enum Cue: CaseIterable { case working, tick, done, error, wake }

/// Per-turn activity state: reset on the next user `transcript`.
struct ActivityLog: Equatable {
    static let maxDone = 8
    private(set) var current: Activity?
    private(set) var done: [Activity] = []
    private(set) var speaking = false
    private var routedThisTurn = false
    private var hadActivity = false
    private var startLabel: String?

    var isEmpty: Bool { current == nil && done.isEmpty }

    /// Applies one call event; returns the cue it calls for, if any.
    mutating func apply(_ e: CallEvent) -> Cue? {
        switch e.type {
        case "transcript":
            self = ActivityLog()
        case "activity":
            let glyph = Activity.glyph(kind: e.phase == "routing" || e.phase == "thinking" ? "generic" : e.kind, tool: e.tool)
            switch e.phase {
            case "routing":
                hadActivity = true
                current = Activity(label: e.label ?? "", glyph: glyph)
                if !routedThisTurn { routedThisTurn = true; return .working }
            case "thinking":
                hadActivity = true
                current = Activity(label: e.label ?? "", glyph: glyph)
            case "tool_start":
                hadActivity = true
                startLabel = e.label
                speaking = false
                current = Activity(label: e.label ?? "", glyph: glyph)
            case "tool_end":
                hadActivity = true
                let label = Activity.doneLabel(end: e.label, start: startLabel ?? current?.label)
                if !label.isEmpty { done.append(Activity(label: label, glyph: Activity.glyph(kind: e.kind, tool: e.tool))) }
                done = Array(done.suffix(Self.maxDone))
                current = nil
                startLabel = nil
                return .tick
            default:
                break // unknown phases are ignored
            }
        case "state":
            if e.value == "speaking" {
                speaking = true
                current = nil
                if hadActivity { hadActivity = false; return .done }
            } else {
                speaking = false
            }
        case "error":
            current = nil
            return .error
        default:
            break
        }
        return nil
    }
}

#if canImport(SwiftUI)
import SwiftUI

/// VoiceOver (DESIGN §13.6): only tool_start labels, only while thinking, at most one per 2.5 s
/// (a newer label replaces a pending one), low priority so VoiceOver never interrupts itself.
@MainActor
enum ActivityAnnouncer {
    private static var last = -Double.infinity
    private static var pending: Task<Void, Never>?

    static func toolStarted(_ label: String, thinking: Bool) {
        guard thinking, !label.isEmpty else { return }
        pending?.cancel()
        let wait = max(0, last + 2.5 - ProcessInfo.processInfo.systemUptime)
        pending = Task {
            if wait > 0 { try? await Task.sleep(nanoseconds: UInt64(wait * 1e9)) }
            guard !Task.isCancelled else { return }
            last = ProcessInfo.processInfo.systemUptime
            var text = AttributedString(label)
            text.accessibilitySpeechAnnouncementPriority = .low
            AccessibilityNotification.Announcement(text).post()
        }
    }
}
#endif
