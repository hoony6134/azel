import XCTest
@testable import Azel

/// 2026-11-23: "US$0.83 / US$500" next to model rows adding to ~$33.7 (the subscription mixed in).
final class UsageSummaryTests: XCTestCase {
    func testBudgetAndSubscriptionAreApart() throws {
        let json = #"{"month":"2026-11","calls":120,"cost_usd":0.83,"budget_usd":500,"by_model":{"haiku":2.39,"opus":1.95,"sonnet":29.4},"by_path":{"api":0.8,"nim":0.03,"claude_code":32.91},"paid":{"cost_usd":0.83,"by_path":{"api":0.8,"nim":0.03}},"subscription":{"claude_code":{"turns":41,"equivalent_usd":32.91}}}"#
        let u = UsageSummary(try XCTUnwrap(JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any]))
        XCTAssertEqual(u.budgetCost, 0.83, accuracy: 1e-9)
        XCTAssertEqual(u.budgetByPath.map(\.name), ["Claude API", "NIM"], "budget paths only")
        XCTAssertEqual(u.subscription?.turns, 41)
        XCTAssertEqual(u.subscription?.equivalentUSD ?? 0, 32.91, accuracy: 1e-9)
    }

    func testAnOlderGatewayWithoutPaidStillLeavesTheSubscriptionOut() throws {
        let json = #"{"cost_usd":0.83,"by_path":{"api":0.8,"claude_code":32.91}}"#
        let u = UsageSummary(try XCTUnwrap(JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any]))
        XCTAssertEqual(u.budgetCost, 0.83, accuracy: 1e-9)
        XCTAssertEqual(u.budgetByPath.map(\.name), ["Claude API"])
        XCTAssertNil(u.subscription)
    }

    func testTheGatewayRowSaysWhichGatewayAnswered() throws {
        let s = try JSONDecoder().decode(ServerStatus.self, from: Data(#"{"spark":{"ok":true,"ms":374},"gateway":{"ok":true,"ms":0,"device":"deneb"},"devices":[]}"#.utf8))
        XCTAssertEqual(s.gateway.device, "deneb")
    }
}
