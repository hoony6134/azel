import XCTest
@testable import Azel

final class CustomTintTests: XCTestCase {
    override func tearDown() { CustomTint.setLocal("p-test", nil) }

    func testHexRoundTripAndNearest() {
        XCTAssertEqual(CustomTint.hex("#FF8800"), 0xFF8800)
        XCTAssertNil(CustomTint.hex("celadon"))
        XCTAssertEqual(CustomTint.string(0x00ff88), "#00FF88")
        XCTAssertEqual(CustomTint.nearestTint(0x80CAB3), .celadon)
        XCTAssertEqual(CustomTint.nearestTint(0xF0A0B5), .azalea)
    }

    func testAccentIsReadableOnBothBackgrounds() {
        for c: UInt32 in [0xFFFF00, 0x000080, 0x808080, 0x00FF00, 0xFFFFFF, 0x000000] {
            for dark in [true, false] {
                let l = CustomTint.luminance(CustomTint.readable(c, dark: dark))
                let bg = CustomTint.luminance(dark ? 0x0D1113 : 0xF5F3EC)
                XCTAssertGreaterThanOrEqual((max(l, bg) + 0.05) / (min(l, bg) + 0.05), 4.5, "\(String(c, radix: 16)) dark=\(dark)")
            }
        }
    }

    func testLocalColourOnlyWhileTheServerHoldsItsStandIn() {
        CustomTint.setLocal("p-test", 0xF0A0B5)
        var p = Persona(id: "p-test", name: "A", color: "azalea")
        XCTAssertEqual(p.customHex, 0xF0A0B5)
        XCTAssertNil(p.pickedHex, "the editor starts from the colour field")
        p.color = "sky"                     // changed elsewhere: the server's tint wins
        XCTAssertNil(p.customHex)
        p.color = "#123456"                 // a gateway that stores hex
        XCTAssertEqual(p.customHex, 0x123456)
        XCTAssertEqual(p.pickedHex, 0x123456)
        p.color = "#82CBB4"                 // an old build's hex of a palette tint is that tint
        XCTAssertNil(p.pickedHex)
        XCTAssertEqual(p.tintKey, .celadon)
    }

    #if os(iOS)
    func testTiltLeansThenSettles() {
        var f = TiltFollower()
        XCTAssertEqual(f.step(x: 0, y: -0.7).x, 0, "the first sample is the rest pose")
        let lean = f.step(x: 0.2, y: -0.7)
        XCTAssertGreaterThan(lean.x, 5)
        XCTAssertEqual(f.step(x: 1, y: -0.7).x, TiltFollower.limit, "clamped")
        var last = 0.0
        for _ in 0..<400 { last = f.step(x: 1, y: -0.7).x }
        XCTAssertLessThan(abs(last), 0.5, "back to centre once the pose holds")
        XCTAssertLessThan(f.step(x: 1, y: -0.4).y, 0, "top tilted away: orb moves up")
    }
    #endif
}

#if os(iOS)
final class TiltGateTests: XCTestCase {
    func testMotionRunsOnlyOnTheVisibleForegroundHome() {
        XCTAssertTrue(TiltModel.shouldRun(active: true, inCall: false, reduceMotion: false, visible: true))
        XCTAssertFalse(TiltModel.shouldRun(active: false, inCall: false, reduceMotion: false, visible: true), "background")
        XCTAssertFalse(TiltModel.shouldRun(active: true, inCall: true, reduceMotion: false, visible: true), "under the call cover")
        XCTAssertFalse(TiltModel.shouldRun(active: true, inCall: false, reduceMotion: true, visible: true))
        XCTAssertFalse(TiltModel.shouldRun(active: true, inCall: false, reduceMotion: false, visible: false), "another tab")
    }

    func testFinishedTasksEndTheirActivities() throws {
        let tasks = try JSONDecoder().decode([AgentTask].self, from: Data(#"[{"id":"a","goal":"x","status":"running"},{"id":"b","goal":"y","status":"done"}]"#.utf8))
        XCTAssertEqual(Set(LiveActivities.stale(["a", "b", "gone"], tasks: tasks)), ["b", "gone"])
    }
}
#endif
