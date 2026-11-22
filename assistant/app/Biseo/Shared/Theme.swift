import SwiftUI
#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

/// DESIGN.md §2–5 tokens ("달항아리"). Colors adapt to light/dark without an asset catalog.
enum Theme {
    static let bg = Color(dark: 0x0D1113, light: 0xF5F3EC)
    static let bgRaised = Color(dark: 0x131A1D, light: 0xEFECE3)
    static let card = Color(dark: 0x182025, light: 0xFFFFFF)
    static let stroke = Color(dark: 0x26313A, light: 0xDCD8CC)
    static let text = Color(dark: 0xECF0EC, light: 0x14191B)
    static let text2 = Color(dark: 0xA9B4B0, light: 0x4E5A56)
    static let text3 = Color(dark: 0x8A9692, light: 0x5F6B67)
    static let celadon = Color(dark: 0x82CBB4, light: 0x2C7663)
    static let indigo = Color(dark: 0x9AAAF0, light: 0x4656B8)
    static let porcelain = Color(dark: 0xF3EBDC, light: 0xFFFFFF)
    static let amber = Color(dark: 0xE9BC6E, light: 0x8F5E12)
    static let danger = Color(dark: 0xFF7A6E, light: 0xC2352B)
    static let glazeCeladon = Color(dark: 0x82CBB4, light: 0xA8DCCB)
    static let glazeIndigo = Color(dark: 0x9AAAF0, light: 0xB4BFF3)
    static let glazeAmber = Color(dark: 0xE9BC6E, light: 0xF0CB86)
    static let hangup = Color(dark: 0xC8342C, light: 0xC8342C)
    static let onAccent = Color(dark: 0x0D1113, light: 0xFFFFFF)

    // §4 spacing / radii
    static let xs: CGFloat = 4, s: CGFloat = 8, m: CGFloat = 12, l: CGFloat = 16, xl: CGFloat = 24, xxl: CGFloat = 32, hero: CGFloat = 48
    static let cardRadius: CGFloat = 22, rowRadius: CGFloat = 12, callButtonRadius: CGFloat = 28

    // §5 motion
    static let quick = Animation.snappy(duration: 0.18)
    static let state = Animation.smooth(duration: 0.45)
    static let enter = Animation.spring(duration: 0.5, bounce: 0.15)
    static let caption = Animation.easeOut(duration: 0.22)
}

extension Color {
    init(dark: UInt32, light: UInt32) {
        func comps(_ hex: UInt32) -> (CGFloat, CGFloat, CGFloat) {
            (CGFloat((hex >> 16) & 0xFF) / 255, CGFloat((hex >> 8) & 0xFF) / 255, CGFloat(hex & 0xFF) / 255)
        }
        let d = comps(dark), l = comps(light)
        #if os(iOS)
        self.init(uiColor: UIColor { $0.userInterfaceStyle == .dark
            ? UIColor(red: d.0, green: d.1, blue: d.2, alpha: 1) : UIColor(red: l.0, green: l.1, blue: l.2, alpha: 1) })
        #elseif os(watchOS)
        self.init(red: d.0, green: d.1, blue: d.2)   // the watch is always dark
        #else
        self.init(nsColor: NSColor(name: nil) { $0.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? NSColor(srgbRed: d.0, green: d.1, blue: d.2, alpha: 1) : NSColor(srgbRed: l.0, green: l.1, blue: l.2, alpha: 1) })
        #endif
    }
}

#if !os(watchOS) // the watch uses only the tokens above
// MARK: - Small shared pieces

/// Grouped card (§4, §7.5): `card` on `bg`, 1 pt stroke, radius 22, light-mode shadow only.
struct Card<Content: View>: View {
    var title: String?
    var trailing: AnyView?
    @ViewBuilder var content: Content
    @Environment(\.colorScheme) private var scheme
    @Environment(\.colorSchemeContrast) private var contrast

    init(_ title: String? = nil, trailing: AnyView? = nil, @ViewBuilder content: () -> Content) {
        self.title = title
        self.trailing = trailing
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.m) {
            if title != nil || trailing != nil {
                HStack {
                    if let title { Text(title).font(.headline).foregroundStyle(Theme.text) }
                    Spacer()
                    trailing
                }
            }
            content
        }
        .padding(Theme.l)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous)
            .strokeBorder(contrast == .increased ? Theme.text3 : Theme.stroke, lineWidth: 1))
        .shadow(color: .black.opacity(scheme == .light ? 0.05 : 0), radius: 12, y: 4)
    }
}

/// Route chip (§2): qwen → celadon "로컬", claude → indigo "Claude".
struct RouteChip: View {
    let route: String
    @Environment(\.colorSchemeContrast) private var contrast

    /// Korean words only; an unknown route shows no chip (user bug, 2026-11-22: a raw "task").
    static func label(_ route: String) -> String? {
        switch route {
        case "claude", "claude_api", "api": return "Claude"
        case "claude_code": return "Claude Code"
        case "qwen", "local": return "로컬"
        case "nim": return "NIM"
        case "task": return "작업"
        default: return nil
        }
    }

    var body: some View {
        if let text = Self.label(route) {
            let claude = route.hasPrefix("claude") || route == "api"
            let color = claude ? Theme.indigo : Theme.celadon
            Text(text)
                .font(.footnote.weight(.semibold)).foregroundStyle(color)
                .padding(.horizontal, Theme.s).padding(.vertical, 3)
                .background(color.opacity(contrast == .increased ? 0.2 : 0.12), in: Capsule())
                .overlay(Capsule().strokeBorder(color.opacity(0.6), lineWidth: 1))
                .accessibilityLabel("\(text) 응답")
        }
    }
}

struct StatusDot: View {
    let ok: Bool
    var body: some View { Circle().fill(ok ? Theme.celadon : Theme.danger).frame(width: 8, height: 8) }
}

/// §10 pattern: small offline jar, title, one sentence, optional action, raw error under "자세히".
struct EmptyState: View {
    let title: String
    let message: String
    var detail: String? = nil
    var action: (String, () -> Void)? = nil

    var body: some View {
        VStack(spacing: Theme.m) {
            MoonJar(state: .offline, levels: .silent).frame(width: 96, height: 96)
            Text(title).font(.headline).foregroundStyle(Theme.text)
            Text(message).font(.subheadline).foregroundStyle(Theme.text2).multilineTextAlignment(.center)
            if let action { Button(action.0, action: action.1).buttonStyle(.bordered).tint(Theme.celadon) }
            if let detail {
                DisclosureGroup("자세히") { Text(detail).font(.footnote).textSelection(.enabled).foregroundStyle(Theme.text3) }
                    .font(.footnote).foregroundStyle(Theme.text3)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(Theme.xl)
    }
}

/// §10: map a raw error to the user-facing title/body.
/// Error text for the UI. Only network trouble and 5xx mean "unreachable"; a 4xx means the server
/// answered and said no (user bug, 2026-11-21: a 400 on persona save read "닿지 않아요").
/// `title` is short: callers prefix it ("저장하지 못했어요: …").
enum FriendlyError {
    /// "items[3].ts" for a decoding failure.
    static func keyPath(_ e: DecodingError) -> String {
        let path: [CodingKey]
        switch e {
        case .keyNotFound(let k, let c): path = c.codingPath + [k]
        case .typeMismatch(_, let c), .valueNotFound(_, let c), .dataCorrupted(let c): path = c.codingPath
        @unknown default: path = []
        }
        return path.map { $0.intValue.map { "[\($0)]" } ?? ".\($0.stringValue)" }.joined().trimmingCharacters(in: CharacterSet(charactersIn: "."))
    }

    static func describe(_ error: Error) -> (title: String, message: String) {
        switch error {
        case GatewayError.rejected(let detail):
            return (detail, "서버가 요청을 받지 않았어요.")
        case GatewayError.http(let code) where code == 401 || code == 403:
            return ("토큰이 맞지 않아요", "설정의 토큰을 다시 입력해 주세요.")
        case GatewayError.http(404):
            return ("이 기능은 서버 업데이트 후 사용할 수 있어요", "서버가 아직 이 기능을 모르는 것 같아요.")
        case GatewayError.http(409):
            return ("지금은 처리할 수 없어요. 잠시 후 다시 시도해 주세요", "서버가 다른 작업 중이에요.")
        case GatewayError.http(let code) where (400..<500).contains(code):
            return ("요청을 처리하지 못했어요 (HTTP \(code))", "서버가 요청을 받지 않았어요.")
        case let e as DecodingError:
            // The server answered; the app couldn't read it. Never "닿지 않아요" (live bug, 2026-11-22).
            Log.call.error("response not decodable at \(Self.keyPath(e), privacy: .public): \(String(describing: e), privacy: .public)")
            return ("응답 형식이 달라요 (앱 업데이트 필요)", "서버는 답했지만 이 앱이 읽지 못했어요.")
        default:
            return ("\(AssistantName.current)에게 닿지 않아요", "서버에 연결할 수 없어요. 네트워크와 서버 주소를 확인해 주세요.")
        }
    }
}
#endif
