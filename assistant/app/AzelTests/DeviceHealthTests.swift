import XCTest
@testable import Azel

/// Status from the actual answer (user, 2026-11-23): green within 2 s, yellow slower, red no answer.
final class DeviceHealthTests: XCTestCase {
    private func dev(_ json: String) throws -> ServerStatus.Device { try JSONDecoder().decode(ServerStatus.Device.self, from: Data(json.utf8)) }

    func testThreeStatesFromTheAnswer() throws {
        XCTAssertEqual(try dev(#"{"name":"mac","ok":true,"ms":40}"#).health, .ok)
        XCTAssertEqual(try dev(#"{"name":"albireo","ok":true,"ms":2600}"#).health, .slow, "answered, slowly")
        XCTAssertEqual(try dev(#"{"name":"x","ok":true,"ms":2000}"#).health, .ok, "2 s is still green")
        XCTAssertEqual(try dev(#"{"name":"ruchba","ok":false,"fresh":true}"#).health, .down, "no answer: red, whatever last_seen says")
    }

    func testTheGatewaysOwnStatusWins() throws {
        let slow = try dev(#"{"name":"ruchba","ok":true,"ms":2100,"status":"slow","latency_ms":2100}"#)
        XCTAssertEqual(slow.health, .slow); XCTAssertEqual(slow.latency, 2100)
        XCTAssertEqual(try dev(#"{"name":"a","ok":false,"status":"down"}"#).health, .down)
        XCTAssertEqual(try dev(#"{"name":"b","ok":true,"ms":3000,"status":"ok"}"#).health, .ok)
        XCTAssertEqual(DeviceHealth.slow.label, "느림")
    }
}
