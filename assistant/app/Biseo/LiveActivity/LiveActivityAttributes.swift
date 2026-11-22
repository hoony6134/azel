import ActivityKit
import Foundation

/// A call on the Lock Screen and in the Dynamic Island. The app runs for the whole call (audio
/// background mode), so it updates this itself; no push needed.
struct CallActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        /// listening | thinking | speaking | held | connecting
        var state: String
        /// The latest line, the reply or what the user said.
        var caption: String?
        var mine: Bool
        var muted: Bool
        /// Secret call: the Lock Screen and Dynamic Island show no captions.
        var secret: Bool = false
        /// A device command waiting for the user, with 허용 / 거부 / 항상 허용 buttons.
        var approval: PendingApproval? = nil
    }

    var name: String
    /// Persona glaze, 0xRRGGBB.
    var glaze: UInt32
    var startedAt: Date
}

/// The approval a Live Activity shows. `command` is nil in a secret call ("승인 필요" only).
struct PendingApproval: Codable, Hashable {
    var id: String
    var device: String
    var command: String?
    /// "항상 허용" isn't allowed for this one.
    var forcedOnce: Bool

    /// The command cut for the Lock Screen.
    var shortCommand: String? { command.map { $0.count <= 40 ? $0 : String($0.prefix(39)) + "…" } }
}

/// A background task (`/v1/tasks`) while it runs.
struct TaskActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        /// queued | running | waiting_approval | done | failed | cancelled
        var status: String
        var step: String?
        var steps: Int
        var approval: PendingApproval? = nil
    }

    var taskID: String
    var goal: String
    var glaze: UInt32
}

enum LiveActivityText {
    static func callState(_ s: String) -> String {
        switch s {
        case "listening": return "듣는 중"
        case "thinking": return "생각 중"
        case "speaking": return "말하는 중"
        case "held": return "보류 중"
        default: return "연결 중"
        }
    }

    static func taskStatus(_ s: String) -> String {
        switch s {
        case "queued": return "대기"
        case "running": return "진행 중"
        case "waiting_approval": return "승인 필요"
        case "done": return "완료"
        case "failed": return "실패"
        case "cancelled": return "취소됨"
        default: return s
        }
    }

    static func symbol(callState s: String) -> String {
        switch s {
        case "listening": return "waveform"
        case "thinking": return "ellipsis"
        case "speaking": return "speaker.wave.2.fill"
        case "held": return "pause.fill"
        default: return "phone.fill"
        }
    }
}
