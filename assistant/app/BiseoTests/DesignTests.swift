import XCTest
@testable import Biseo

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
        XCTAssertLessThanOrEqual(idle, 0.008)
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
}

final class RecentsTests: XCTestCase {
    func testKeepsNewestTwentyAndSkipsSilentCalls() {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("recents-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        Recents.add(CallRecord(session: "silent", start: Date(), lines: []), to: url)
        XCTAssertTrue(Recents.load(from: url).isEmpty)
        for i in 0..<25 {
            Recents.add(CallRecord(session: "s\(i)", start: Date(timeIntervalSince1970: Double(i)), lines: [CallLine(mine: true, text: "\(i)")]), to: url)
        }
        let all = Recents.load(from: url)
        XCTAssertEqual(all.count, Recents.limit)
        XCTAssertEqual(all.first?.session, "s24")
        XCTAssertEqual(all.last?.session, "s5")
    }

    func testGreetingByHour() {
        let cal = Calendar.current
        let at = { (h: Int) in cal.date(bySettingHour: h, minute: 0, second: 0, of: Date())! }
        XCTAssertEqual(greeting(at: at(8)), "좋은 아침이에요")
        XCTAssertEqual(greeting(at: at(14)), "좋은 오후예요")
        XCTAssertEqual(greeting(at: at(21)), "좋은 저녁이에요")
    }
}
