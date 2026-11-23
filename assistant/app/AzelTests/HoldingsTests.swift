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

    /// POST /v1/holdings answer: the resolved list replaces the optimistic one; candidates ask the user.
    @MainActor func testAddShowsTheResolvedCodeOrAsks() {
        let m = HoldingsModel()
        m.apply(added: "에스오에스랩", response: ["holdings": ["애플 (AAPL)", "에스오에스랩 (464080)"]], before: [])
        XCTAssertEqual(m.items.last?.code, "464080")
        XCTAssertNil(m.choice)
        m.apply(added: "삼성", response: ["holdings": ["애플 (AAPL)"], "candidates": ["삼성": ["삼성전자 (005930)", "삼성SDI (006400)"]]],
                before: [Holding(entry: "애플 (AAPL)")])
        XCTAssertEqual(m.choice?.options.count, 2)
        XCTAssertEqual(m.items.map(\.entry), ["애플 (AAPL)"])
    }
}
