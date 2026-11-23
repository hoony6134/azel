import XCTest
@testable import Azel

final class HoldingsTests: XCTestCase {
    func testParsesNameAndCode() {
        XCTAssertEqual(Holding(entry: "SOS랩 (464080)"), Holding(name: "SOS랩", code: "464080"))
        XCTAssertEqual(Holding(entry: "  애플   (AAPL) "), Holding(name: "애플", code: "AAPL"))
        XCTAssertEqual(Holding(entry: "엔트로픽"), Holding(name: "엔트로픽", code: nil), "unlisted: a bare name")
        XCTAssertEqual(Holding(name: "SK하이닉스", code: "000660").entry, "SK하이닉스 (000660)")
    }

    /// The user's examples: a rename keeps the code, never loses or doubles the "(464080)".
    func testRenameKeepsTheCodeUnlessGivenExplicitly() {
        let sos = Holding(entry: "SOS랩 (464080)")
        XCTAssertEqual(sos.renamed("에스오에스랩")?.entry, "에스오에스랩 (464080)")
        XCTAssertEqual(sos.renamed("에스오에스랩 (464080)")?.entry, "에스오에스랩 (464080)", "not doubled")
        XCTAssertEqual(sos.renamed("에스오에스랩 (999999)")?.entry, "에스오에스랩 (999999)", "explicit code edit")
        XCTAssertEqual(Holding(entry: "엔트로픽").renamed("앤트로픽")?.entry, "앤트로픽")
        XCTAssertNil(sos.renamed("   "))
    }

    func testLimitsMatchTheGateway() {
        XCTAssertNil(Holding.problem((0..<12).map { Holding(name: "종목\($0)", code: nil) }))
        XCTAssertNotNil(Holding.problem((0..<13).map { Holding(name: "종목\($0)", code: nil) }))
        XCTAssertNotNil(Holding.problem([Holding(name: String(repeating: "가", count: 35), code: "123456")]))
    }

    /// The PUT answer: the saved list (codes resolved) replaces the optimistic one; candidates ask the user.
    @MainActor func testSaveShowsTheResolvedCodeOrAsks() {
        let m = HoldingsModel()
        let sent = [Holding(entry: "애플 (AAPL)"), Holding(entry: "에스오에스랩")]
        m.apply(response: ["holdings": ["애플 (AAPL)", "에스오에스랩 (464080)"], "candidates": [String: [String]]()], sent: sent)
        XCTAssertEqual(m.items.last?.code, "464080")
        XCTAssertNil(m.choice)
        m.apply(response: ["holdings": ["애플 (AAPL)", "삼성"], "candidates": ["삼성": ["삼성전자 (005930)", "삼성SDI (006400)"]]], sent: sent)
        XCTAssertEqual(m.choice?.name, "삼성")
        XCTAssertEqual(m.choice?.options.count, 2)
        XCTAssertEqual(m.items.map(\.entry), ["애플 (AAPL)", "삼성"], "saved bare until the user picks")
        m.apply(response: [:], sent: sent)
        XCTAssertEqual(m.items, sent, "an answer without holdings keeps what was sent")
    }
}
