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
        case .idle: return 0.018 * sin(2 * θ + 0.45 * t) + 0.010 * sin(3 * θ - 0.31 * t + 1.3) // §6.3 undulation
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

    /// Extra swirl turns from a tool_start surge (0.25 → 0.6 rev/s, easing back over 0.4 s) and
    /// extra glow from a tool_end pulse.
    static func workBoost(now: Date, surge: Date?, pulse: Date?) -> (turns: Double, glow: Double) {
        var turns = 0.0, glow = 0.0
        if let surge {
            let e = now.timeIntervalSince(surge)
            if e >= 0 { turns = 0.35 * (e < 0.4 ? e - e * e / 0.8 : 0.2) } // integral of a linear ease-out
        }
        if let pulse {
            let e = now.timeIntervalSince(pulse)
            if e >= 0 && e < 0.3 { glow = 0.25 * sin(.pi * e / 0.3) }
        }
        return (turns, glow)
    }

    // MARK: Idle, "gently alive" (DESIGN §6.3)

    /// The shared breath, period 4.6 s.
    static func breath(_ t: Double) -> Double { sin(2 * .pi * t / 4.6) }

    /// Idle glow (opacity, blur radius in units of R): in phase with the breath; when calm
    /// (Reduce Motion / Low Power) only a slow 0.35 ↔ 0.45 fade over 8 s.
    static func idleGlow(_ t: Double, calm: Bool) -> (opacity: Double, radius: Double) {
        if calm { return (0.40 + 0.05 * sin(2 * .pi * t / 8), 0.30) }
        let b = breath(t)
        return (0.35 + 0.12 * b, 0.30 + 0.04 * b)
    }

    static let shimmerLength = 1.6

    /// Shimmer progress 0…1 if one is running at `t`. One per 10 s slot, starting at a pseudo-random
    /// offset of 0–2 s, so consecutive shimmers are 8–12 s apart and never overlap.
    static func shimmer(at t: Double) -> Double? {
        let slot = (t / 10).rounded(.down)
        let r = (sin(slot * 12.9898) * 43_758.5453).truncatingRemainder(dividingBy: 1)
        let start = slot * 10 + 2 * abs(r)
        let p = (t - start) / shimmerLength
        return (0..<1).contains(p) ? p : nil
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
    /// tool_start: the thinking swirl speeds up and eases back over 400 ms (DESIGN §13.3).
    var surge: Date? = nil
    /// tool_end: one glow pulse, +0.25 opacity over 300 ms.
    var pulse: Date? = nil
    /// Mac popover only: an extra-slow sway (§6.3).
    var sway = false
    @AppStorage(AssistantName.key) private var name = AssistantName.fallback
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var phase
    @State private var previous: JarState = .idle
    @State private var changedAt = Date.distantPast
    @State private var visible = false

    /// Reduce Motion or Low Power: a static orb with only a slow glow fade.
    private var calm: Bool { reduceMotion || ProcessInfo.processInfo.isLowPowerModeEnabled }
    /// Idle runs at 30 fps and only while on screen in an active app; call states keep 60 fps.
    private var interval: Double { state == .idle || state == .offline ? 1 / 30 : 1 / 60 }
    private var isPaused: Bool {
        if paused || !visible || phase == .background { return true }
        return (state == .idle || state == .offline) && phase != .active
    }

    var body: some View {
        // The glow's blur and the water line reach well past the jar, so the canvas overscans its
        // layout frame by `pad` on every side; drawing inside a tight frame cut them with hard
        // edges (user screenshot, 2026-11-21). Layout and hit testing keep the original frame.
        GeometryReader { geo in
            let pad = max(geo.size.width, geo.size.height) * 0.6
            TimelineView(.animation(minimumInterval: interval, paused: isPaused)) { tl in
                canvas(now: tl.date, frame: CGRect(origin: CGPoint(x: pad, y: pad), size: geo.size))
            }
            .frame(width: geo.size.width + 2 * pad, height: geo.size.height + 2 * pad)
            .offset(x: -pad, y: -pad)
            .allowsHitTesting(false)
        }
        .onChange(of: state) { old, _ in previous = old; changedAt = Date() }
        .onAppear { visible = true }
        .onDisappear { visible = false }
        .accessibilityElement()
        .accessibilityLabel(name)
        .accessibilityValue(state.spokenLabel)
        .accessibilityAddTraits(.updatesFrequently)
    }

    /// `frame` is the jar's layout frame inside the larger canvas.
    private func canvas(now: Date, frame: CGRect) -> some View {
        let t = now.timeIntervalSinceReferenceDate
        return Canvas { ctx, _ in
                let mix = JarPath.smoothstep(now.timeIntervalSince(changedAt) / 0.45)
                let level = Double(state == .speaking ? levels.out : levels.mic)
                let R = min(frame.width, frame.height) * 0.34
                let center = CGPoint(x: frame.midX, y: frame.minY + frame.height * (state == .speaking ? 0.44 : 0.5))
                var ctx = ctx
                // Idle breath: the whole orb scales ±2.5%, easing out as a call takes over.
                let idleWeight = (state == .idle ? mix : 0) + (previous == .idle ? 1 - mix : 0)
                let scale = calm ? 1 : 1 + 0.025 * JarPath.breath(t) * idleWeight
                if sway && !calm && state == .idle {
                    ctx.translateBy(x: center.x + 1.5 * sin(2 * .pi * t / 11), y: center.y)
                    ctx.rotate(by: .degrees(1.5 * sin(2 * .pi * t / 9 + 0.8)))
                    ctx.translateBy(x: -center.x, y: -center.y)
                }
                let path = JarPath.make(center: center, R: R, t: calm ? 0 : t, level: level,
                                        from: previous, to: state, mix: mix, reduceMotion: calm)
                    .applying(CGAffineTransform(translationX: center.x, y: center.y).scaledBy(x: scale, y: scale)
                        .translatedBy(x: -center.x, y: -center.y))
                let boost = calm ? (0.0, 0.0) : JarPath.workBoost(now: now, surge: surge, pulse: pulse)
                draw(state, path: path, opacity: mix, ctx: ctx, center: center, R: R, t: t, level: level, boost: boost)
                if mix < 1 { draw(previous, path: path, opacity: 1 - mix, ctx: ctx, center: center, R: R, t: t, level: level, boost: boost) }
                if state == .idle, !calm, let p = JarPath.shimmer(at: t) { shimmer(p, path: path, ctx: ctx, center: center, R: R) }
                if let ripple, case let e = now.timeIntervalSince(ripple), e >= 0, e < 0.4 {
                    let k = 1.05 + e * (reduceMotion ? 0 : 0.8)
                    let ring = path.applying(CGAffineTransform(translationX: center.x, y: center.y).scaledBy(x: k, y: k)
                        .translatedBy(x: -center.x, y: -center.y))
                    ctx.stroke(ring, with: .color(Theme.celadon.opacity(1 - e / 0.4)), lineWidth: 2)
                }
            }
    }

    /// The idle shimmer: a soft white highlight (18% peak, radius 0.45R) crossing the body from the
    /// upper left to the right, ease-in-out, opacity sin(πp), clipped to the orb.
    private func shimmer(_ p: Double, path: Path, ctx: GraphicsContext, center: CGPoint, R: Double) {
        var ctx = ctx
        ctx.clip(to: path)
        let e = p < 0.5 ? 2 * p * p : 1 - pow(-2 * p + 2, 2) / 2
        let at = CGPoint(x: center.x + R * (-0.6 + 1.3 * e), y: center.y + R * (-0.5 + 0.5 * e))
        ctx.opacity = sin(.pi * p)
        ctx.fill(path, with: .radialGradient(Gradient(colors: [Color.white.opacity(0.18), .clear]), center: at,
                                             startRadius: 0, endRadius: R * 0.45))
    }

    /// boost: (extra swirl turns, extra glow opacity) from tool activity.
    private func draw(_ s: JarState, path: Path, opacity: Double, ctx: GraphicsContext, center: CGPoint, R: Double, t: Double, level: Double,
                      boost: (Double, Double) = (0, 0)) {
        var ctx = ctx
        ctx.opacity = opacity
        let box = path.boundingRect
        // 1. soft glow
        if let glow = s.glow {
            var strength = 0.55
            switch s {
            case .idle: strength = JarPath.idleGlow(t, calm: calm).opacity
            case .thinking: strength = 0.6 + 0.4 * (0.5 + 0.5 * sin(t * 2 * .pi / 1.4))
            case .speaking: strength = 0.4 + 0.5 * level
            default: break
            }
            strength += boost.1
            ctx.drawLayer { g in
                let radius = s == .speaking ? R * (0.3 + 0.5 * level) : s == .idle ? R * JarPath.idleGlow(t, calm: calm).radius : 28
                g.addFilter(.blur(radius: radius))
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
        if s == .thinking && calm { bodyOpacity = 0.85 + 0.15 * (0.5 + 0.5 * sin(t * .pi)) }
        ctx.opacity = opacity * bodyOpacity
        ctx.fill(path, with: .radialGradient(Gradient(colors: s.glaze), center: CGPoint(x: box.midX, y: box.midY - box.height * 0.25),
                                             startRadius: 0, endRadius: box.width * 0.7))
        // 5. thinking swirl
        if s == .thinking {
            ctx.clip(to: path)
            let angle = calm ? 0 : t * .pi / 2 + boost.0 * 2 * .pi
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
