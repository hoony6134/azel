import SwiftUI

/// The jar's look per call state (DESIGN §6.2).
enum JarState: Equatable {
    case idle, listening, thinking, speaking, held, offline

    init(callState: String, inCall: Bool, held: Bool) {
        if held { self = .held; return }
        guard inCall else { self = .idle; return }
        switch callState {
        case "thinking": self = .thinking
        case "speaking": self = .speaking
        default: self = .listening
        }
    }

    var spokenLabel: String {
        switch self {
        case .idle: return "대기"
        case .listening: return "듣고 있어요"
        case .thinking: return "생각 중"
        case .speaking: return "말하는 중"
        case .held: return "대기 중"
        case .offline: return "연결 안 됨"
        }
    }

    var symbol: String {
        switch self {
        case .listening, .idle: return "ear"
        case .thinking: return "ellipsis"
        case .speaking: return "waveform"
        case .held: return "pause.fill"
        case .offline: return "wifi.slash"
        }
    }

    var color: Color {
        switch self {
        case .idle, .listening: return Theme.celadon
        case .thinking: return Theme.indigo
        case .speaking: return Theme.amber
        case .held, .offline: return Theme.text3
        }
    }

    var glaze: [Color] {
        switch self {
        case .idle: return [Theme.glazeCeladon.opacity(0.25), Theme.porcelain.opacity(0.7)]
        case .listening: return [Theme.glazeCeladon, Theme.porcelain]
        case .thinking: return [Theme.glazeIndigo, Theme.porcelain.opacity(0.8)]
        case .speaking: return [Theme.porcelain, Theme.porcelain.opacity(0.88)]
        case .held: return [Theme.porcelain.opacity(0.5), Theme.porcelain.opacity(0.35)]
        case .offline: return [Theme.text3.opacity(0.4), Theme.text3.opacity(0.25)]
        }
    }

    var glow: Color? {
        switch self {
        case .idle, .listening: return Theme.glazeCeladon
        case .thinking: return Theme.glazeIndigo
        case .speaking: return Theme.glazeAmber
        case .held, .offline: return nil
        }
    }
}

/// Pure geometry so it can be unit-tested.
enum JarPath {
    static let points = 120

    /// The lopsided moon-jar silhouette (y down): fuller shoulder, narrower foot, hand-thrown asymmetry.
    static func jar(_ θ: Double) -> Double { 1 - 0.06 * sin(θ) - 0.035 * cos(2 * θ) + 0.015 * sin(3 * θ + 0.6) }

    static func wobble(_ state: JarState, θ: Double, t: Double, level: Double) -> Double {
        switch state {
        case .idle: return 0.008 * sin(θ * 2 + t * 0.6)
        case .listening: return level * (0.05 * sin(3 * θ + 2.1 * t) + 0.03 * sin(5 * θ - 3.3 * t))
        case .thinking: return 0.012 * sin(2 * θ + 1.2 * t)
        case .speaking: return level * (0.07 * sin(2 * θ + 2.8 * t) + 0.025 * sin(7 * θ + 5 * t))
        case .held, .offline: return 0
        }
    }

    /// Reduce Motion: no undulation; level only as a uniform scale of at most ±4%.
    static func radius(_ state: JarState, θ: Double, t: Double, level: Double, R: Double, reduceMotion: Bool) -> Double {
        if reduceMotion {
            let scale = state == .listening || state == .speaking ? 1 + 0.04 * (2 * level - 1) : 1
            return R * jar(θ) * scale
        }
        return R * jar(θ) * (1 + wobble(state, θ: θ, t: t, level: level))
    }

    /// `from` → `to` blended by `mix` (0…1) so state changes morph instead of jumping.
    static func make(center: CGPoint, R: Double, t: Double, level: Double, from: JarState, to: JarState, mix: Double,
                     reduceMotion: Bool) -> Path {
        var path = Path()
        for i in 0..<points {
            let θ = Double(i) / Double(points) * 2 * .pi
            let r = radius(from, θ: θ, t: t, level: level, R: R, reduceMotion: reduceMotion) * (1 - mix)
                + radius(to, θ: θ, t: t, level: level, R: R, reduceMotion: reduceMotion) * mix
            let p = CGPoint(x: center.x + r * cos(θ), y: center.y + r * sin(θ))
            i == 0 ? path.move(to: p) : path.addLine(to: p)
        }
        path.closeSubpath()
        return path
    }

    static func smoothstep(_ x: Double) -> Double { let c = max(0, min(1, x)); return c * c * (3 - 2 * c) }
}

/// The hero visualizer (DESIGN §6.3): TimelineView + Canvas, levels read per frame without
/// invalidating SwiftUI.
struct MoonJar: View {
    var state: JarState
    let levels: Levels
    var paused = false
    /// Set on `interrupt`: one quick ring ripple (DESIGN §5).
    var ripple: Date? = nil
    @AppStorage(AssistantName.key) private var name = AssistantName.fallback
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var phase
    @State private var previous: JarState = .idle
    @State private var changedAt = Date.distantPast

    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 60, paused: paused || phase == .background)) { tl in
            let now = tl.date
            let t = now.timeIntervalSinceReferenceDate
            Canvas { ctx, size in
                let mix = JarPath.smoothstep(now.timeIntervalSince(changedAt) / 0.45)
                let level = Double(state == .speaking ? levels.out : levels.mic)
                let R = min(size.width, size.height) * 0.34
                let center = CGPoint(x: size.width / 2, y: size.height * (state == .speaking ? 0.44 : 0.5))
                let path = JarPath.make(center: center, R: R, t: reduceMotion ? 0 : t, level: level,
                                        from: previous, to: state, mix: mix, reduceMotion: reduceMotion)
                draw(state, path: path, opacity: mix, ctx: ctx, center: center, R: R, t: t, level: level)
                if mix < 1 { draw(previous, path: path, opacity: 1 - mix, ctx: ctx, center: center, R: R, t: t, level: level) }
                if let ripple, case let e = now.timeIntervalSince(ripple), e >= 0, e < 0.4 {
                    let k = 1.05 + e * (reduceMotion ? 0 : 0.8)
                    let ring = path.applying(CGAffineTransform(translationX: center.x, y: center.y).scaledBy(x: k, y: k)
                        .translatedBy(x: -center.x, y: -center.y))
                    ctx.stroke(ring, with: .color(Theme.celadon.opacity(1 - e / 0.4)), lineWidth: 2)
                }
            }
        }
        .onChange(of: state) { old, _ in previous = old; changedAt = Date() }
        .accessibilityElement()
        .accessibilityLabel(name)
        .accessibilityValue(state.spokenLabel)
        .accessibilityAddTraits(.updatesFrequently)
    }

    private func draw(_ s: JarState, path: Path, opacity: Double, ctx: GraphicsContext, center: CGPoint, R: Double, t: Double, level: Double) {
        var ctx = ctx
        ctx.opacity = opacity
        let box = path.boundingRect
        // 1. soft glow
        if let glow = s.glow {
            var strength = 0.55
            switch s {
            case .idle: strength = reduceMotion ? 0.3 : 0.3 + 0.15 * sin(t * 2 * .pi / 6)
            case .thinking: strength = 0.6 + 0.4 * (0.5 + 0.5 * sin(t * 2 * .pi / 1.4))
            case .speaking: strength = 0.4 + 0.5 * level
            default: break
            }
            ctx.drawLayer { g in
                g.addFilter(.blur(radius: s == .speaking ? R * (0.3 + 0.5 * level) : 28))
                g.fill(path, with: .color(glow.opacity(strength)))
            }
        }
        // 2. listening ring
        if s == .listening {
            let k = 1.12 + 0.1 * level
            let ring = path.applying(CGAffineTransform(translationX: center.x, y: center.y).scaledBy(x: k, y: k)
                .translatedBy(x: -center.x, y: -center.y))
            ctx.stroke(ring, with: .color(Theme.celadon.opacity(0.25 + 0.5 * level)), lineWidth: 1)
        }
        // 3. speaking water line: mirrored, squashed, blurred copy below the jar
        if s == .speaking {
            let y = center.y + R * 1.25
            let w = 0.8 + 0.3 * level
            let mirror = path.applying(CGAffineTransform(translationX: center.x, y: y).scaledBy(x: w, y: -0.25)
                .translatedBy(x: -center.x, y: -center.y))
            ctx.drawLayer { g in
                g.addFilter(.blur(radius: 12))
                g.fill(mirror, with: .color(Theme.porcelain.opacity(0.3)))
            }
        }
        // 4. glazed body
        var bodyOpacity = 1.0
        if s == .thinking && reduceMotion { bodyOpacity = 0.85 + 0.15 * (0.5 + 0.5 * sin(t * .pi)) }
        ctx.opacity = opacity * bodyOpacity
        ctx.fill(path, with: .radialGradient(Gradient(colors: s.glaze), center: CGPoint(x: box.midX, y: box.midY - box.height * 0.25),
                                             startRadius: 0, endRadius: box.width * 0.7))
        // 5. thinking swirl
        if s == .thinking {
            ctx.clip(to: path)
            let angle = reduceMotion ? 0 : t * .pi / 2
            ctx.fill(path, with: .conicGradient(Gradient(colors: [Theme.glazeIndigo.opacity(0.7), Theme.porcelain.opacity(0.1),
                                                                   Theme.glazeIndigo.opacity(0.4), Theme.glazeIndigo.opacity(0.7)]),
                                                center: CGPoint(x: box.midX, y: box.midY), angle: .radians(angle)))
        }
    }
}

/// Full-bleed state glaze behind the call screen (§6.3): mesh on iOS 18 / macOS 15, radial fallback.
struct GlazeBackground: View {
    var state: JarState
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let c = state.glow ?? Theme.text3
        ZStack {
            Theme.bg
            if #available(iOS 18, macOS 15, *) {
                TimelineView(.animation(minimumInterval: 1 / 30, paused: reduceMotion)) { tl in
                    let d = Float(reduceMotion ? 0 : 0.04 * sin(tl.date.timeIntervalSinceReferenceDate * 0.3))
                    MeshGradient(width: 3, height: 3, points: [
                        [0, 0], [0.5, 0], [1, 0],
                        [0, 0.5], [0.5 + d, 0.45 - d], [1, 0.5],
                        [0, 1], [0.5, 1], [1, 1],
                    ], colors: [
                        .clear, c.opacity(0.5), .clear,
                        c.opacity(0.2), c.opacity(0.6), c.opacity(0.2),
                        .clear, c.opacity(0.15), .clear,
                    ])
                }
                .opacity(0.35)
            } else {
                RadialGradient(colors: [c.opacity(0.6), .clear], center: .init(x: 0.5, y: 0.35), startRadius: 0, endRadius: 420).opacity(0.35)
                RadialGradient(colors: [c.opacity(0.3), .clear], center: .bottom, startRadius: 0, endRadius: 360).opacity(0.35)
            }
        }
        .animation(Theme.state, value: state)
        .ignoresSafeArea()
    }
}
