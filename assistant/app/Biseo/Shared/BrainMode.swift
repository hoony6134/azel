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

    /// The call header's mode text: 자동 / 로컬 / Claude API / Claude Code.
    var short: String {
        switch self {
        case .auto: return "자동"
        case .local: return "로컬"
        case .claude_api: return "Claude API"
        case .claude_code: return "Claude Code"
        }
    }

    static func current(_ raw: String?) -> BrainMode { raw.flatMap(BrainMode.init(rawValue:)) ?? .auto }
}

/// The per-call choice from the long-press picker, and whether the gateway knows modes at all.
@MainActor
final class BrainModeChoice: ObservableObject {
    static let shared = BrainModeChoice()
    /// The gateway's settings include `brain_mode` (otherwise the pickers stay hidden).
    @Published var supported = false
    /// For the next call only; nil = the setting's default.
    @Published var next: BrainMode?
    /// The next call starts secret (home's "시크릿으로 걸기").
    @Published var nextSecret = false
    /// The caps the last call's server listed, so home can offer what calls support.
    @Published var lastCaps: Set<String> = Set(UserDefaults.standard.stringArray(forKey: "callCaps") ?? [])

    func remember(caps: [String]) {
        lastCaps = Set(caps)
        UserDefaults.standard.set(caps, forKey: "callCaps")
    }

    func takeSecretForCall() -> Bool {
        defer { nextSecret = false }
        return nextSecret
    }

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
    /// Home only: "시크릿으로 걸기", when the last call's server supported secret calls.
    var secretCall: (() -> Void)? = nil

    func body(content: Content) -> some View {
        let secret = secretCall != nil && choice.lastCaps.contains("secret")
        if choice.supported || secret {
            content.contextMenu {
                if choice.supported {
                    ForEach(BrainMode.allCases) { m in
                        Button { pick(m) } label: {
                            if let h = m.hint { Text(m.label); Text(h) } else { Text(m.label) }
                            if (current ?? "auto") == m.rawValue { Image(systemName: "checkmark") }
                        }
                    }
                }
                if secret, let secretCall {
                    Divider()
                    Button { secretCall() } label: { Label("시크릿으로 걸기", systemImage: "eye.slash") }
                }
            }
        } else {
            content
        }
    }
}

extension View {
    func brainModeMenu(current: String?, pick: @escaping (BrainMode) -> Void, secretCall: (() -> Void)? = nil) -> some View {
        modifier(BrainModeMenu(current: current, pick: pick, secretCall: secretCall))
    }
}

/// The call header's quiet mode control: the mode as plain text with a chevron, opening a native
/// menu with a check on the current one (PM, 2026-11-22: no pill, no sub-text).
struct BrainModeControl: View {
    @ObservedObject private var choice = BrainModeChoice.shared
    var current: String?
    var pick: (BrainMode) -> Void
    /// This call is secret / the server can make it secret (`secret` in caps).
    var secret = false
    var canSecret = false
    var setSecret: (Bool) -> Void = { _ in }

    var body: some View {
        if choice.supported || canSecret {
            let m = BrainMode.current(current)
            Menu {
                if choice.supported {
                    Picker("두뇌", selection: Binding(get: { m }, set: { pick($0) })) {
                        ForEach(BrainMode.allCases) { Text($0.short).tag($0) }
                    }
                    .pickerStyle(.inline)
                }
                if canSecret {
                    Divider()
                    Toggle(isOn: Binding(get: { secret }, set: { setSecret($0) })) { Label("시크릿", systemImage: "eye.slash") }
                }
            } label: {
                HStack(spacing: 3) {
                    if secret { Image(systemName: "eye.slash").font(.caption2).accessibilityLabel("시크릿") }
                    Text(choice.supported ? m.short : (secret ? "시크릿" : "옵션"))
                    Image(systemName: "chevron.down").font(.caption2.weight(.semibold))
                }
                .font(.footnote)
                .foregroundStyle(m == .auto ? Theme.text3 : Theme.text2)
            }
            #if os(macOS)
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            #endif
            .accessibilityLabel("두뇌: \(m.short)")
        }
    }
}

/// Typing during a call: a field that sends on return, and 말로 답하기.
struct CallTypingField: View {
    @Binding var text: String
    @AppStorage("typedSpeak") private var speak = true
    var focus: FocusState<Bool>.Binding
    var send: (String, Bool) -> Void
    var close: () -> Void

    var body: some View {
        VStack(spacing: Theme.xs) {
            HStack(spacing: Theme.s) {
                TextField("입력해서 말하기", text: $text)
                    .textFieldStyle(.roundedBorder)
                    .focused(focus)
                    .onSubmit(submit)
                    .submitLabel(.send)
                Button { submit() } label: { Image(systemName: "arrow.up.circle.fill").font(.title2) }
                    .buttonStyle(.plain).foregroundStyle(Theme.celadon)
                    .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityLabel("보내기")
                Button { close() } label: { Image(systemName: "xmark.circle.fill").font(.title3) }
                    .buttonStyle(.plain).foregroundStyle(Theme.text3).accessibilityLabel("입력 닫기")
            }
            Toggle("말로 답하기", isOn: $speak).font(.footnote).foregroundStyle(Theme.text2)
                .toggleStyle(.switch).controlSize(.mini)
        }
    }

    private func submit() {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        send(t, speak)
        text = ""
    }
}
