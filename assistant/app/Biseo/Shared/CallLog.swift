import Foundation

struct CallLine: Identifiable, Codable, Equatable {
    var id = UUID()
    let mine: Bool
    var text: String
    var route: String? = nil
    /// The user talked over this reply (`interrupt`): shown dimmed with a "…" tail.
    var interrupted = false
    /// Typed during the call rather than spoken (optional so saved logs still decode).
    var typed: Bool? = nil
}

/// What the call screens show, built from server events. Reply sentences of one turn join into one
/// paragraph (DESIGN §7.3); `turn_done` or a new transcript starts the next.
struct CallLog: Equatable {
    var lines: [CallLine] = []
    private var replyOpen = false
    /// A typed line waiting for the server's echo (if it sends one), so it shows once.
    private var pendingTyped: String?

    /// The user typed this; shown at once as their line, with the keyboard mark.
    mutating func typed(_ text: String) {
        lines.append(CallLine(mine: true, text: text, typed: true))
        replyOpen = false
        pendingTyped = text
    }

    mutating func apply(_ event: CallEvent) {
        switch event.type {
        case "transcript":
            guard let t = LegacyText.clean(event.text) else { return }
            replyOpen = false
            if let p = pendingTyped, p == t { pendingTyped = nil; return }   // the server echoed the typed line
            pendingTyped = nil
            lines.append(CallLine(mine: true, text: t))
        case "reply":
            guard let t = LegacyText.clean(event.text) else {
                // A bare legacy marker: mark the reply it belongs to instead of showing the text.
                if event.text != nil, let i = lines.indices.last, !lines[i].mine { lines[i].interrupted = true }
                return
            }
            if replyOpen, let i = lines.indices.last, !lines[i].mine {
                lines[i].text += " " + t
                lines[i].route = event.route ?? lines[i].route
            } else {
                lines.append(CallLine(mine: false, text: t, route: event.route))
                replyOpen = true
            }
        case "interrupt":
            if replyOpen, let i = lines.indices.last, !lines[i].mine { lines[i].interrupted = true }
            replyOpen = false
        case "turn_done":
            replyOpen = false
        default: break
        }
    }

    var lastRoute: String? { lines.last { !$0.mine && $0.route != nil }?.route }
}

/// Posted when a call ends so the history list reloads (the server writes the record).
let callEndedNotification = Notification.Name("biseo.call.ended")

/// Before a server fix, interrupted turns were stored with the literal text "(말이 끊김)". It is
/// never shown; the turn gets a subtle 끊김 mark from `interrupted` instead.
enum LegacyText {
    static let interruptedMarker = "(말이 끊김)"

    /// The text without the marker; nil when nothing else is left.
    static func clean(_ s: String?) -> String? {
        guard let s else { return nil }
        let out = s.replacingOccurrences(of: interruptedMarker, with: "")
            .replacingOccurrences(of: "  ", with: " ").trimmingCharacters(in: .whitespacesAndNewlines)
        return out.isEmpty ? nil : out
    }
}
