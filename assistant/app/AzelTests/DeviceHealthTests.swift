import XCTest
@testable import Azel

/// 2026-11-23: albireo and ruchba were red while healthy (their relayed tailnet path outlasts the probe).
final class DeviceHealthTests: XCTestCase {
    private func dev(_ json: String) throws -> ServerStatus.Device { try JSONDecoder().decode(ServerStatus.Device.self, from: Data(json.utf8)) }

    func testThreeStates() throws {
        XCTAssertEqual(try dev(#"{"name":"mac","ok":true,"ms":40,"fresh":true}"#).health, .ok)
        XCTAssertEqual(try dev(#"{"name":"albireo","ok":false,"ms":null,"fresh":true}"#).health, .slow, "probe timed out, but registered recently")
        XCTAssertEqual(try dev(#"{"name":"old","ok":false,"fresh":false}"#).health, .down)
        XCTAssertEqual(try dev(#"{"name":"legacy","ok":false}"#).health, .down, "an older gateway without fresh")
    }

    func testTheGatewaysOwnStatusWins() throws {
        XCTAssertEqual(try dev(#"{"name":"ruchba","ok":false,"fresh":true,"status":"down","latency_ms":12}"#).health, .down, "refused: down even if fresh")
        let slow = try dev(#"{"name":"ruchba","ok":true,"ms":2100,"status":"slow","latency_ms":2100}"#)
        XCTAssertEqual(slow.health, .slow)
        XCTAssertEqual(slow.latency, 2100)
        XCTAssertEqual(try dev(#"{"name":"a","ok":false,"status":"ok"}"#).health, .ok)
        XCTAssertEqual(DeviceHealth.slow.label, "느림")
    }
}
