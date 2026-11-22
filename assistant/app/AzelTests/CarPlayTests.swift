import XCTest
@testable import Azel

#if os(iOS)
final class CarPlayMenuTests: XCTestCase {
    private func history(_ json: String) throws -> HistoryItem { try JSONDecoder().decode(HistoryItem.self, from: Data(json.utf8)) }

    func testPersonasBriefingAndResume() throws {
        let mia = Persona(id: "p1", name: "미아", primary: true), jun = Persona(id: "p2", name: "준")
        let recent = try [
            history(#"{"id":"h1","session":"iphone-1","title":"내일 일정과 날씨, 그리고 오후 회의 준비 이야기","started_at":1795300000}"#),
            history(#"{"id":"h2","session":"secret-9","title":"비밀","started_at":1795300100}"#),
        ]
        let s = CarPlayMenu.sections(personas: [mia, jun], assistant: "아젤", recent: recent)
        XCTAssertEqual(s.map(\.title), ["비서", "지금", "이어서 통화"])
        XCTAssertEqual(s[0].items.map(\.text), ["미아 부르기", "준 부르기"])
        XCTAssertEqual(s[0].items[0].action, .call(personaID: "p1"))
        XCTAssertEqual(s[1].items[0].action, .briefing)
        XCTAssertEqual(s[2].items.map(\.action), [.resume(historyID: "h1")], "secret calls have nothing to continue")
        XCTAssertLessThanOrEqual(s[2].items[0].text.count, 24, "short lines for the car")
    }

    func testNoPersonasYetAndNoHistory() {
        let s = CarPlayMenu.sections(personas: [], assistant: "아젤", recent: [])
        XCTAssertEqual(s.map(\.title), ["비서", "지금"])
        XCTAssertEqual(s[0].items, [.init(text: "아젤 부르기", detail: nil, action: .call(personaID: nil))])
    }

    @MainActor func testRecentsCallBackByPersonaHandle() {
        XCTAssertEqual(StartCallIntentHandler.persona(handle: nil, named: nil)?.id, PersonaModel.shared.primary?.id)
    }

    func testTheSceneIsDeclared() {
        let manifest = Bundle.main.object(forInfoDictionaryKey: "UIApplicationSceneManifest") as? [String: Any]
        let configs = manifest?["UISceneConfigurations"] as? [String: Any]
        let carplay = (configs?["CPTemplateApplicationSceneSessionRoleApplication"] as? [[String: String]])?.first
        XCTAssertEqual(carplay?["UISceneDelegateClassName"], "Azel.CarPlaySceneDelegate")
    }
}
#endif
