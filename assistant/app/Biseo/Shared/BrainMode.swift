import SwiftUI

/// Which brain answers (`brain_mode`): auto | local | claude_api | claude_code.
enum BrainMode: String, CaseIterable, Identifiable {
    case auto, local, claude_api, claude_code
    var id: String { rawValue }

    var label: String {
        switch self {
        case .auto: return "자동 (기본)"
        case .local: return "로컬만"
        case .claude_api: return "Claude API"
        case .claude_code: return "Claude Code"
        }
    }

    var hint: String? {
        switch self {
        case .auto: return nil
        case .local: return "Claude를 쓰지 않아요, 무료"
        case .claude_api: return "빠름, 예산 차감"
        case .claude_code: return "구독 사용"
        }
    }

    /// The call-screen chip: nil for 자동.
    static func chip(_ raw: String?) -> String? {
        guard let raw, let m = BrainMode(rawValue: raw), m != .auto else { return nil }
        return m == .local ? "로컬만" : m.label
    }
}

/// The per-call choice from the long-press picker, and whether the gateway knows modes at all.
@MainActor
final class BrainModeChoice: ObservableObject {
    static let shared = BrainModeChoice()
    /// The gateway's settings include `brain_mode` (otherwise the pickers stay hidden).
    @Published var supported = false
    /// For the next call only; nil = the setting's default.
    @Published var next: BrainMode?

    /// Taken once by the call that starts next.
    func takeForCall() -> String? {
        defer { next = nil }
        return next?.rawValue
    }

    /// The picker's choice. Before a call: the next call. During one: ask the server to switch; if it
    /// hasn't confirmed within 3 s (no mid-call switch), keep it for the next call and say so.
    func choose(_ m: BrainMode, inCall: Bool, client: CallClient, current: @escaping () -> String?, notice: @escaping (String) -> Void) {
        guard inCall else { next = m; return }
        client.switchMode(m.rawValue)
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            guard let self, (current() ?? BrainMode.auto.rawValue) != m.rawValue else { return }
            next = m
            notice("이번 통화에는 바꿀 수 없어서 다음 통화부터 \(m.label)로 해요")
        }
    }

    nonisolated static func reports(_ settingsJSON: Data) -> Bool {
        ((try? JSONSerialization.jsonObject(with: settingsJSON)) as? [String: Any])?["brain_mode"] != nil
    }
}

/// Long-press (iPhone) / right-click (Mac) menu with the four modes.
struct BrainModeMenu: ViewModifier {
    @ObservedObject private var choice = BrainModeChoice.shared
    /// The mode in effect, to tick it.
    var current: String?
    var pick: (BrainMode) -> Void

    func body(content: Content) -> some View {
        if choice.supported {
            content.contextMenu {
                ForEach(BrainMode.allCases) { m in
                    Button { pick(m) } label: {
                        if let h = m.hint { Text(m.label); Text(h) } else { Text(m.label) }
                        if (current ?? "auto") == m.rawValue { Image(systemName: "checkmark") }
                    }
                }
            }
        } else {
            content
        }
    }
}

extension View {
    func brainModeMenu(current: String?, pick: @escaping (BrainMode) -> Void) -> some View {
        modifier(BrainModeMenu(current: current, pick: pick))
    }
}
