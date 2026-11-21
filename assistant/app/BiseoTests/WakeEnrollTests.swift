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

final class WakeRetrainTests: XCTestCase {
    private func status(_ json: String) throws -> WakeEnrollStatus { try JSONDecoder().decode(WakeEnrollStatus.self, from: Data(json.utf8)) }

    func testResultCard() throws {
        let better = try status(#"{"clips":24,"status":"ready","min_clips":20,"improved":true,"error":null,"metrics":{"recall_before":0.71,"recall_after":0.93,"fa_nearmiss":0.02,"threshold":0.6,"clips_train":19,"clips_held_out":5,"fp_per_hour_before":null}}"#)
        XCTAssertTrue(better.retrains)
        XCTAssertEqual(better.resultText, "인식률 71% → 93%")
        XCTAssertEqual(better.metrics?["clips_held_out"], 5)
        XCTAssertNil(better.metrics?["fp_per_hour_before"], "null metrics are dropped")
        XCTAssertEqual(try status(#"{"clips":24,"status":"ready","min_clips":20,"improved":false,"metrics":{}}"#).resultText,
                       "기존 모델을 유지했어요 (오인식이 늘어서)")
        XCTAssertEqual(try status(#"{"clips":24,"status":"failed","min_clips":20,"improved":null,"error":"학습 데이터가 없어요"}"#).resultText, "학습 데이터가 없어요")
        XCTAssertNil(try status(#"{"clips":12,"status":"none","min_clips":20,"improved":null,"error":null,"metrics":null}"#).resultText)
    }

    func testOldServerHidesTheNewFields() throws {
        let old = try status(#"{"clips":8,"status":"ready","trained_at":"2026-11-21T10:00:00Z"}"#)
        XCTAssertFalse(old.retrains)
        XCTAssertNil(old.resultText)
    }

    func testSessionPlanAndPrompts() {
        XCTAssertEqual(WakeEnrollModel.sessionLength(stored: 0, minClips: 20), 20)
        XCTAssertEqual(WakeEnrollModel.sessionLength(stored: 12, minClips: 20), 8, "finish the first 20")
        XCTAssertEqual(WakeEnrollModel.sessionLength(stored: 24, minClips: 20), 10, "add more later")
        XCTAssertEqual(WakeEnrollModel.sessionLength(stored: 55, minClips: 20), 5, "never past 60")
        XCTAssertEqual(WakeEnrollModel.sessionLength(stored: 60, minClips: 20), 0)
        let p = WakeEnrollModel.retrainPrompts("아젤", count: 7)
        XCTAssertEqual(p[0], "‘아젤’ — 보통 목소리로")
        XCTAssertEqual(p[4], "‘아젤’ — 피곤한 톤으로")
        XCTAssertEqual(p[5], p[0], "the five tones cycle")
        XCTAssertEqual(WakeEnrollStatus.percent(93), "93%")
    }
}
