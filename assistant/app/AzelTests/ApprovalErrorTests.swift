import XCTest
@testable import Azel

/// The gateway explains approval failures in Korean (404 unknown, 409 already decided, 502): show it.
final class ApprovalErrorTests: XCTestCase {
    private func body(_ detail: String) -> Data { Data(#"{"detail":"\#(detail)"}"#.utf8) }

    func testApprovalErrorsCarryTheGatewaysDetail() {
        let e409 = GatewayError.from(status: 409, body: body("이미 처리된 요청이에요"), explain: ApprovalAPI.explain)
        XCTAssertEqual(e409.errorDescription, "이미 처리된 요청이에요")
        XCTAssertEqual(FriendlyError.describe(e409).title, "이미 처리된 요청이에요")
        let e502 = GatewayError.from(status: 502, body: body("기기에 연결하지 못했어요"), explain: ApprovalAPI.explain)
        XCTAssertEqual(e502.errorDescription, "기기에 연결하지 못했어요", "502 used to show nothing")
    }

    func testMissingRoutesAndOtherCallsKeepTheirCodes() {
        guard case .http(404) = GatewayError.from(status: 404, body: body("Not Found"), explain: ApprovalAPI.explain) else {
            return XCTFail("FastAPI's own Not Found is a missing route")
        }
        guard case .http(409) = GatewayError.from(status: 409, body: body("바빠요")) else { return XCTFail("other calls: unchanged") }
    }
}
