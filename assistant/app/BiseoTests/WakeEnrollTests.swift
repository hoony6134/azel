import XCTest
@testable import Biseo

final class WakeEnrollTests: XCTestCase {
    func testStatusAndResultDecode() throws {
        let s = try JSONDecoder().decode(WakeEnrollStatus.self, from: Data(#"{"clips":8,"trained_at":"2026-11-22T10:00:00+09:00","status":"ready"}"#.utf8))
        XCTAssertEqual(s.status, "ready")
        XCTAssertEqual(s.trained_at, Date(timeIntervalSince1970: 1_795_309_200))
        let none = try JSONDecoder().decode(WakeEnrollStatus.self, from: Data(#"{"clips":0,"trained_at":null,"status":"none"}"#.utf8))
        XCTAssertNil(none.trained_at)
        let r = try JSONDecoder().decode(WakeEnrollResult.self, from: Data(#"{"accepted":6,"rejected":[{"index":2,"reason":"too_quiet"}],"training":true}"#.utf8))
        XCTAssertEqual(r.rejected, [.init(index: 2, reason: "too_quiet")])
        XCTAssertTrue(r.training)
    }

    func testMultipartCarriesEveryClipAsClipsArray() {
        let body = WakeEnrollAPI.multipart([WakeClip.wav([0, 0.1]), WakeClip.wav([0.2])], boundary: "B")
        let text = String(decoding: body, as: UTF8.self)
        XCTAssertEqual(text.components(separatedBy: "name=\"clips\";").count - 1, 2, "the field is `clips`, repeated")
        XCTAssertFalse(text.contains("clips[]"))
        XCTAssertTrue(text.contains("Content-Type: audio/wav"))
        XCTAssertTrue(text.hasSuffix("--B--\r\n"))
    }

    @MainActor func testPromptsUseTheNameWithTheRightParticle() {
        XCTAssertEqual(WakeEnrollModel.prompts("아젤").first, "‘아젤’이라고 말해 주세요")
        XCTAssertEqual(WakeEnrollModel.prompts("하나").first, "‘하나’라고 말해 주세요")
        XCTAssertTrue((6...8).contains(WakeEnrollModel.prompts("아젤").count))
    }
}
