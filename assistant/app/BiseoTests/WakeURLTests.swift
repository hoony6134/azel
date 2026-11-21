import XCTest
@testable import Biseo

#if os(macOS)
final class WakeURLTests: XCTestCase {
    func testParsesTheListenersURL() {
        XCTAssertEqual(WakeURL.persona(URL(string: "azel://wake?persona=azel")!), .some("azel"))
        XCTAssertEqual(WakeURL.persona(URL(string: "azel://wake")!), .some(nil), "no persona: the primary")
        XCTAssertEqual(WakeURL.persona(URL(string: "azel://wake?persona=")!), .some(nil))
        XCTAssertNil(WakeURL.persona(URL(string: "azel://task/abc")!), "other azel URLs aren't a wake")
        XCTAssertNil(WakeURL.persona(URL(string: "https://wake?persona=x")!))
    }

    func testMarkerLifecycle() throws {
        let saved = InCallMarker.url
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        InCallMarker.url = dir.appendingPathComponent("Azel/in-call")
        defer { InCallMarker.url = saved; try? FileManager.default.removeItem(at: dir) }
        InCallMarker.remove()                                      // launch: nothing there, no error
        InCallMarker.create()                                      // call starts (creates the folder too)
        XCTAssertTrue(FileManager.default.fileExists(atPath: InCallMarker.url.path))
        InCallMarker.create()                                      // idempotent
        InCallMarker.remove()                                      // call ends
        XCTAssertFalse(FileManager.default.fileExists(atPath: InCallMarker.url.path))
    }

    func testMarkerIsInTheRealHomeNotTheContainer() {
        XCTAssertFalse(InCallMarker.url.path.contains("/Library/Containers/"), InCallMarker.url.path)
        XCTAssertTrue(InCallMarker.url.path.hasSuffix("/Library/Application Support/Azel/in-call"))
    }
}
#endif
