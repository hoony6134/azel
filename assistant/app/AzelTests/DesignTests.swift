import XCTest
@testable import Azel

final class JarPathTests: XCTestCase {
    func testSilhouetteIsLopsidedAndStatesWobbleAsSpecified() {
        // y down: θ = 3π/2 is the shoulder, fuller than the foot at π/2.
        XCTAssertGreaterThan(JarPath.jar(3 * .pi / 2), JarPath.jar(.pi / 2))
        let R = 100.0
        // Silent listening/speaking and held/offline don't move.
        for s in [JarState.listening, .speaking, .held, .offline] {
            XCTAssertEqual(JarPath.radius(s, θ: 0, t: 3, level: 0, R: R, reduceMotion: false), R * JarPath.jar(0), accuracy: 1e-9)
        }
        // Speaking at full level moves more than idle ever can.
        let idle = (0..<60).map { abs(JarPath.wobble(.idle, θ: 0.3, t: Double($0) / 10, level: 1)) }.max()!
        let speaking = (0..<60).map { abs(JarPath.wobble(.speaking, θ: 0.3, t: Double($0) / 10, level: 1)) }.max()!
        XCTAssertLessThanOrEqual(idle, 0.028, "§6.3 undulation stays within 0.018 + 0.010")
        XCTAssertGreaterThan(speaking, 0.05)
    }

    func testReduceMotionIsAUniformScaleOfAtMostFourPercent() {
        let R = 100.0
        for θ in stride(from: 0.0, to: 2 * .pi, by: 0.5) {
            let loud = JarPath.radius(.speaking, θ: θ, t: 7, level: 1, R: R, reduceMotion: true)
            XCTAssertEqual(loud / (R * JarPath.jar(θ)), 1.04, accuracy: 1e-9)
            XCTAssertEqual(JarPath.radius(.thinking, θ: θ, t: 7, level: 1, R: R, reduceMotion: true), R * JarPath.jar(θ), accuracy: 1e-9)
        }
    }

    func testPathHas120PointsAndMorphsBetweenStates() {
        let a = JarPath.make(center: .zero, R: 50, t: 1, level: 1, from: .idle, to: .speaking, mix: 0, reduceMotion: false)
        let b = JarPath.make(center: .zero, R: 50, t: 1, level: 1, from: .speaking, to: .speaking, mix: 1, reduceMotion: false)
        var points = 0
        a.forEach { if case .line = $0 { points += 1 } }
        XCTAssertEqual(points, JarPath.points - 1)
        XCTAssertNotEqual(a.boundingRect, b.boundingRect)
        XCTAssertEqual(JarPath.smoothstep(0.5), 0.5, accuracy: 1e-9)
    }

    func testJarStateFromCall() {
        XCTAssertEqual(JarState(callState: "thinking", inCall: true, held: false), .thinking)
        XCTAssertEqual(JarState(callState: "", inCall: true, held: false), .listening)
        XCTAssertEqual(JarState(callState: "speaking", inCall: true, held: true), .held)
        XCTAssertEqual(JarState(callState: "speaking", inCall: false, held: false), .idle)
    }
}

final class LevelsTests: XCTestCase {
    func testDbMappingAndSmoothing() {
        XCTAssertEqual(Levels.level(rms: 0), 0)
        XCTAssertEqual(Levels.level(rms: pow(10, -50 / 20)), 0, accuracy: 1e-4)
        XCTAssertEqual(Levels.level(rms: pow(10, -6 / 20)), 1, accuracy: 1e-4)
        XCTAssertEqual(Levels.level(rms: 1), 1)
        // Attack (40 ms) is faster than release (180 ms) over the same 20 ms step.
        let up = Levels.smooth(0, 1, dt: 0.02), down = 1 - Levels.smooth(1, 0, dt: 0.02)
        XCTAssertGreaterThan(up, down)
        XCTAssertEqual(up, 1 - exp(-0.5), accuracy: 1e-5)
    }
}

final class CallLogTests: XCTestCase {
    private func ev(_ type: String, _ text: String? = nil, _ route: String? = nil) -> CallEvent {
        CallEvent(type: type, text: text, route: route)
    }

    func testRepliesOfOneTurnJoinAndInterruptMarksThem() {
        var log = CallLog()
        log.apply(ev("transcript", "내일 일정?"))
        log.apply(ev("reply", "내일은", "claude"))
        log.apply(ev("reply", "회의가 두 개예요.", "claude"))
        XCTAssertEqual(log.lines.count, 2)
        XCTAssertEqual(log.lines[1].text, "내일은 회의가 두 개예요.")
        log.apply(ev("interrupt"))
        XCTAssertTrue(log.lines[1].interrupted)
        log.apply(ev("reply", "네?", "qwen"))      // after an interrupt a new paragraph starts
        XCTAssertEqual(log.lines.count, 3)
        log.apply(ev("turn_done"))
        log.apply(ev("reply", "또", "qwen"))
        XCTAssertEqual(log.lines.count, 4)
        XCTAssertEqual(log.lastRoute, "qwen")
    }

    func testAJoinedRequestReplacesItsHalfLine() {
        var log = CallLog()
        log.apply(ev("transcript", "내가 보유한 주식"))
        log.apply(ev("transcript", "내가 보유한 주식 오늘 알려줘"))
        XCTAssertEqual(log.lines.map(\.text), ["내가 보유한 주식 오늘 알려줘"])
        log.apply(ev("reply", "네.", "qwen"))
        log.apply(ev("transcript", "내가 보유한 주식 오늘 알려줘 또"))   // after a reply it is a new turn
        XCTAssertEqual(log.lines.count, 3)
    }
}

final class GreetingTests: XCTestCase {
    func testGreetingByHour() {
        let cal = Calendar.current
        let at = { (h: Int) in cal.date(bySettingHour: h, minute: 0, second: 0, of: Date())! }
        XCTAssertEqual(greeting(at: at(8)), "좋은 아침이에요")
        XCTAssertEqual(greeting(at: at(14)), "좋은 오후예요")
        XCTAssertEqual(greeting(at: at(21)), "좋은 저녁이에요")
    }
}

final class IdleMotionTests: XCTestCase {
    func testShimmersAre8To12SecondsApartAndNeverOverlap() {
        var starts: [Double] = []
        var wasOn = false
        for i in 0..<(600 * 20) {                 // 10 minutes at 20 Hz
            let t = 1_000_000 + Double(i) / 20
            let on = JarPath.shimmer(at: t) != nil
            if on && !wasOn { starts.append(t) }
            wasOn = on
        }
        XCTAssertGreaterThanOrEqual(starts.count, 49)
        for (a, b) in zip(starts, starts.dropFirst()) {
            XCTAssertGreaterThanOrEqual(b - a, 8 - 0.06)
            XCTAssertLessThanOrEqual(b - a, 12 + 0.06)
        }
        XCTAssertNil(JarPath.shimmer(at: starts[0] + JarPath.shimmerLength + 0.05), "1.6 s long")
    }

    func testBreathAndGlowAreInPhaseAndCalmModeOnlyFades() {
        XCTAssertEqual(JarPath.breath(4.6 / 4), 1, accuracy: 1e-9)          // full inhale
        let full = JarPath.idleGlow(4.6 / 4, calm: false)
        XCTAssertEqual(full.opacity, 0.47, accuracy: 1e-9)
        XCTAssertEqual(full.radius, 0.34, accuracy: 1e-9)
        for t in stride(from: 0.0, to: 16, by: 0.5) {
            let g = JarPath.idleGlow(t, calm: true)
            XCTAssertTrue((0.35...0.45).contains(g.opacity))
            XCTAssertEqual(g.radius, 0.30)
        }
    }
}

final class RouteChipLabelTests: XCTestCase {
    func testNeverShowsRawRoutes() {
        XCTAssertEqual(RouteChip.label("claude"), "Claude")
        XCTAssertEqual(RouteChip.label("qwen"), "로컬")
        XCTAssertEqual(RouteChip.label("task"), "작업")
        XCTAssertEqual(RouteChip.label("claude_code"), "Claude Code")
        XCTAssertNil(RouteChip.label("mystery_route"), "unknown: no chip")
    }
}
