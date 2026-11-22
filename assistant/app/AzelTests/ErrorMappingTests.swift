import XCTest
@testable import Azel

final class ErrorMappingTests: XCTestCase {
    func testOnlyNetworkAnd5xxAreUnreachable() {
        let unreachable = "\(AssistantName.current)에게 닿지 않아요"
        XCTAssertEqual(FriendlyError.describe(URLError(.timedOut)).title, unreachable)
        XCTAssertEqual(FriendlyError.describe(URLError(.notConnectedToInternet)).title, unreachable)
        XCTAssertEqual(FriendlyError.describe(GatewayError.http(502)).title, unreachable)
        XCTAssertEqual(FriendlyError.describe(GatewayError.http(500)).title, unreachable)
        XCTAssertEqual(FriendlyError.describe(GatewayError.http(401)).title, "토큰이 맞지 않아요")
        XCTAssertEqual(FriendlyError.describe(GatewayError.rejected("color: invalid")).title, "color: invalid")
        XCTAssertNotEqual(FriendlyError.describe(GatewayError.http(409)).title, unreachable)
        XCTAssertEqual(FriendlyError.describe(GatewayError.http(418)).title, "요청을 처리하지 못했어요 (HTTP 418)")
    }

    func testServerDetailIsKeptFor400And422() {
        let body400 = Data(#"{"detail":"이미 있는 이름이에요"}"#.utf8)
        guard case .rejected(let m400) = GatewayError.from(status: 400, body: body400) else { return XCTFail("400") }
        XCTAssertEqual(m400, "이미 있는 이름이에요")
        let body422 = Data(#"{"detail":[{"loc":["body","voice_style"],"msg":"none is not an allowed value","type":"type_error"}]}"#.utf8)
        guard case .rejected(let m422) = GatewayError.from(status: 422, body: body422) else { return XCTFail("422") }
        XCTAssertEqual(m422, "voice_style: none is not an allowed value")
        guard case .http(409) = GatewayError.from(status: 409, body: Data(#"{"detail":"training"}"#.utf8)) else { return XCTFail("409 stays a code") }
        guard case .http(404) = GatewayError.from(status: 404, body: Data(#"{"detail":"Not Found"}"#.utf8)) else { return XCTFail("404 stays a code") }
        guard case .http(502) = GatewayError.from(status: 502, body: Data("bad gateway".utf8)) else { return XCTFail("502") }
    }

    @MainActor func testPersonaSaveShowsTheValidationError() async {
        URLProtocol.registerClass(FakeServer.self)
        Config.testOverride = (gateway: "https://fake.test", token: "test-token")
        defer { URLProtocol.unregisterClass(FakeServer.self); Config.testOverride = nil }
        FakeServer.delay = 0
        FakeServer.routes = ["POST /v1/personas": (400, #"{"detail":"color must be one of the palette keys"}"#)]
        do {
            _ = try await PersonaAPI.create(Persona(name: "미아", voice: "ono_anna"))
            XCTFail("should throw")
        } catch {
            XCTAssertEqual("저장하지 못했어요: " + FriendlyError.describe(error).title, "저장하지 못했어요: color must be one of the palette keys")
        }
    }
}
