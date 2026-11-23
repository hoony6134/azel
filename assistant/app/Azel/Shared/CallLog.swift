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
    /// Cards the server attached to this reply's turn, shown under it (not saved).
    var cards: [ReplyCard] = []

    private enum CodingKeys: String, CodingKey { case id, mine, text, route, interrupted, typed }
}

/// What the call screens show, built from server events. Reply sentences of one turn join into one
/// paragraph (DESIGN §7.3); `turn_done` or a new transcript starts the next.
struct CallLog: Equatable {
    var lines: [CallLine] = []
    private var replyOpen = false
    /// Cards that arrived before their turn's reply text: they join the reply line when it starts.
    private var pendingCards: [ReplyCard] = []
    /// A typed line waiting for the server's echo (if it sends one), so it shows once.
    private var pendingTyped: String?

    /// The user typed this; shown at once as their line, with the keyboard mark.
    mutating func typed(_ text: String) {
        lines.append(CallLine(mine: true, text: text, typed: true))
        replyOpen = false
        pendingTyped = text
    }

    /// `text_rejected`: the server ignored the typed line, so it leaves the captions.
    mutating func rejectTyped() {
        if let i = lines.indices.last, lines[i].typed == true { lines.remove(at: i) }
        pendingTyped = nil
    }

    /// The hint for `{"type":"text_rejected","reason":…}`.
    static func rejection(_ reason: String?) -> String {
        switch reason {
        case "too_long": return "1000자까지 보낼 수 있어요"
        case "too_fast": return "조금 천천히 보내 주세요"
        default: return "입력을 보내지 못했어요"
        }
    }

    mutating func apply(_ event: CallEvent) {
        switch event.type {
        case "text_rejected":
            rejectTyped()
        case "transcript":
            guard let t = LegacyText.clean(event.text) else { return }
            replyOpen = false
            if let p = pendingTyped, p == t { pendingTyped = nil; return }   // the server echoed the typed line
            pendingTyped = nil
            pendingCards = []
            // The server joined a half-said request to what came next ("…주식" + "오늘 알려줘"): one line, not two.
            if let i = lines.indices.last, lines[i].mine, t.hasPrefix(lines[i].text) { lines[i].text = t; return }
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
                lines.append(CallLine(mine: false, text: t, route: event.route, cards: pendingCards))
                pendingCards = []
                replyOpen = true
            }
        case "card":
            guard let card = event.card else { return }   // unknown kind
            // This turn's reply already started (the last line is the assistant's): under it; else wait for it.
            if let i = lines.indices.last, !lines[i].mine { lines[i].cards.append(card) } else { pendingCards.append(card) }
        case "interrupt":
            if replyOpen, let i = lines.indices.last, !lines[i].mine { lines[i].interrupted = true }
            replyOpen = false
        case "turn_done":
            replyOpen = false
            // A silent turn (a card, no spoken reply): the cards still show, on an empty reply line.
            if !pendingCards.isEmpty { lines.append(ReplyCard.line(pendingCards)); pendingCards = [] }
        default: break
        }
    }

    var lastRoute: String? { lines.last { !$0.mine && $0.route != nil }?.route }
}

/// Posted when a call ends so the history list reloads (the server writes the record).
let callEndedNotification = Notification.Name("azel.call.ended")

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
