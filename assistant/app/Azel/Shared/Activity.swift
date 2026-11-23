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
enum Cue: CaseIterable { case working, tick, done, error, wake, ack, pulse }

/// "들었어요" ack when the server starts thinking, then a small heartbeat ("두근") about every 1.2 s once a
/// wait has lasted 1 s (user, 2026-11-23: was a pulse every 3 s). A wait is any silence inside an open
/// turn: before the first reply audio, and (user, 2026-11-23) also mid-reply, when the playback queue
/// runs dry while the next sentence is generated or a tool runs. Reply audio, a server pause, and the
/// end of the turn (turn_done, interrupt, error, listening, hangup) stop it. The user's transcript does
/// not: the server sends it after `thinking` (STT finishes inside the wait), and ending the turn there
/// silenced every heartbeat (2026-11-23).
struct ThinkingCues {
    static let pulseAfter = 1.0, pulseEvery = 1.2
    /// After 30 s of waiting the heartbeat slows to every 2.4 s (long Claude tasks shouldn't tick like a metronome).
    static let slowAfter = 30.0, slowEvery = 2.4

    private(set) var since: Double?
    private var lastPulse: Double?
    /// A turn is under way: from thinking (or a tool starting) until it ends.
    private var turnOpen = false
    /// In the thinking phase (one ack each time it starts).
    private var thinkingPhase = false
    /// Reply audio is queued or playing (the playback layer's signal).
    private var playing = false
    /// The server paused playback: the user may be talking.
    private var paused = false
    /// Tools started and not yet ended; they keep the turn open.
    private(set) var tools = 0
    /// The server said it listens for the user: never a wait (no heartbeat, no thinking orb) until it
    /// thinks or speaks again, whatever arrives late (a background tool, a drained queue, a resume).
    private var listening = false
    /// This wait already had its ack (thinking's, or a tool's): one per wait.
    private var acked = false

    var active: Bool { since != nil }

    /// A wait long enough to be felt (the heartbeat's own threshold): the UI shows thinking for it, so
    /// the orb and the sound agree, and sub-second gaps between sentences don't flicker.
    /// A running tool shows at once (its ack just played); other waits after the heartbeat's 1 s.
    func waiting(now: Double) -> Bool { since.map { tools > 0 || now - $0 >= Self.pulseAfter } ?? false }

    private mutating func waitFrom(_ now: Double) {
        guard since == nil, turnOpen, !playing, !paused, !listening else { return }
        since = now
        acked = false
        lastPulse = nil
    }

    private mutating func endTurn() {
        turnOpen = false; thinkingPhase = false; tools = 0; since = nil; paused = false
    }

    mutating func event(_ type: String, value: String?, now: Double, phase: String? = nil) -> Cue? {
        switch type {
        case "state":
            switch value {
            case "thinking":
                listening = false
                turnOpen = true
                guard !thinkingPhase else { return nil }
                thinkingPhase = true
                since = nil
                waitFrom(now)
                acked = true
                return .ack
            case "speaking":
                listening = false
                // Audio is on its way: the wait ends here and starts again only when the queue runs dry.
                thinkingPhase = false
                since = nil
            default:
                endTurn()   // listening and the like
                listening = value == "listening"
            }
        case "interrupt", "error", "turn_done", "hangup", "end":
            endTurn()
        case "pause", "duck":
            paused = true
            since = nil
        case "resume", "unduck":
            paused = false
            waitFrom(now)
        case "activity" where phase == "tool_start":
            // A running tool is a wait, before or after the first audio, but not once the server listens
            // (a background step after the reply must not tick over the user's turn).
            guard !listening else { return nil }
            tools += 1
            turnOpen = true
            // Mid-reply with nothing playing: say so at once ("들었어요"), not a second later with the
            // heartbeat; once per wait, and not right after the thinking ack.
            waitFrom(now)
            let ack = !playing && !paused && since != nil && !acked
            if ack { acked = true }
            return ack ? .ack : nil
        case "activity" where phase == "tool_end":
            tools = max(0, tools - 1)
        default: break
        }
        return nil
    }

    /// Reply audio arrived: the wait ends.
    mutating func audioArrived() { since = nil; thinkingPhase = false }

    /// The playback queue started or ran dry (`AudioIO.speechPlaying`).
    mutating func playback(_ isPlaying: Bool, now: Double) {
        playing = isPlaying
        if isPlaying { since = nil } else { waitFrom(now) }
    }

    mutating func tick(now: Double) -> Cue? {
        guard let since, now - since >= Self.pulseAfter else { return nil }
        let every = now - since >= Self.slowAfter ? Self.slowEvery : Self.pulseEvery
        if let lastPulse, now - lastPulse < every - 1e-9 { return nil }
        lastPulse = now
        return .pulse
    }
}

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
