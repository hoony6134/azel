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

/// Past calls kept on this device only (DESIGN §12 Q1): the last 20, no server involved.
struct CallRecord: Codable, Identifiable, Equatable {
    var id: String { "\(session)@\(start.timeIntervalSince1970)" }
    let session: String
    let start: Date
    var lines: [CallLine]
}

enum Recents {
    static let limit = 20

    static var file: URL {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("recents.json")
    }

    static func load(from url: URL = file) -> [CallRecord] {
        guard let data = try? Data(contentsOf: url) else { return [] }
        return (try? JSONDecoder().decode([CallRecord].self, from: data)) ?? []
    }

    /// Newest first; calls where nothing was said are not kept.
    static func add(_ record: CallRecord, to url: URL = file) {
        guard !record.lines.isEmpty else { return }
        let all = Array(([record] + load(from: url)).prefix(limit))
        try? JSONEncoder().encode(all).write(to: url, options: .atomic)
        NotificationCenter.default.post(name: changed, object: nil)
    }

    static let changed = Notification.Name("biseo.recents.changed")
}
