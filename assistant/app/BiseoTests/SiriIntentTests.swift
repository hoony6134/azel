import XCTest
@testable import Biseo

@MainActor
final class SiriIntentTests: XCTestCase {
    private var saved: ((Persona?) -> Void)!

    override func setUp() async throws { saved = StartAzelCallIntent.startCall }
    override func tearDown() async throws { StartAzelCallIntent.startCall = saved }

    func testTheIntentStartsACallWithTheNamedPersonaOrThePrimary() async throws {
        var started: [String?] = []
        StartAzelCallIntent.startCall = { started.append($0?.name ?? "nil") }
        let mia = PersonaEntity(id: "no-such-id", name: "미아")
        _ = try await StartAzelCallIntent(persona: mia).perform()
        _ = try await StartAzelCallIntent().perform()
        XCTAssertEqual(started.count, 2, "each Siri run starts exactly one call")
        // Without personas loaded both resolve to the primary (nil here); with them, 미아 by id or name.
        XCTAssertEqual(StartAzelCallIntent.resolve(nil)?.id, PersonaModel.shared.primary?.id)
    }

    func testPhrasesAllNameTheApp() {
        let shortcuts = BiseoShortcuts.appShortcuts
        XCTAssertFalse(shortcuts.isEmpty)
    }

    func testAlternativeNamesAreInTheInfoPlist() throws {
        let names = (Bundle.main.object(forInfoDictionaryKey: "INAlternativeAppNames") as? [[String: String]])?.compactMap { $0["INAlternativeAppName"] }
        XCTAssertEqual(Set(names ?? []), ["아젤", "비서"])
    }
}
