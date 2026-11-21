import Foundation

struct CallLine: Identifiable, Codable, Equatable {
    var id = UUID()
    let mine: Bool
    var text: String
    var route: String? = nil
    /// The user talked over this reply (`interrupt`): shown dimmed with a "…" tail.
    var interrupted = false
}

/// What the call screens show, built from server events. Reply sentences of one turn join into one
/// paragraph (DESIGN §7.3); `turn_done` or a new transcript starts the next.
struct CallLog: Equatable {
    var lines: [CallLine] = []
    private var replyOpen = false

    mutating func apply(_ event: CallEvent) {
        switch event.type {
        case "transcript":
            guard let t = event.text else { return }
            lines.append(CallLine(mine: true, text: t))
            replyOpen = false
        case "reply":
            guard let t = event.text else { return }
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
