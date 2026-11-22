import XCTest
@testable import Azel

@MainActor
final class ConnectorTests: XCTestCase {
    private let json = #"""
    {"claude_ai":[{"name":"Gmail","enabled":true,"status":"connected"},{"name":"Notion","enabled":false,"status":"needs_auth"}],
     "custom":[{"name":"gistra","url":"https://gistra.example/mcp","transport":"sse","enabled":true,"has_secret":true}]}
    """#

    func testDecodeAndPutShapeDropsReadOnlyFields() throws {
        let set = try JSONDecoder().decode(ConnectorSet.self, from: Data(json.utf8))
        XCTAssertEqual(set.claude_ai.map(\.status), ["connected", "needs_auth"])
        XCTAssertEqual(set.custom.first?.has_secret, true)
        let out = String(decoding: try JSONEncoder().encode(set), as: UTF8.self)
        XCTAssertFalse(out.contains("status"))
        XCTAssertFalse(out.contains("has_secret"))
        XCTAssertTrue(out.contains(#""transport":"sse""#))
        XCTAssertEqual(try JSONDecoder().decode(ConnectorSet.self, from: Data("{}".utf8)), ConnectorSet())
    }

    func testValidation() {
        XCTAssertTrue(ConnectorSet.validName("my-server_2"))
        XCTAssertFalse(ConnectorSet.validName("../etc"))
        XCTAssertFalse(ConnectorSet.validName(""))
        XCTAssertTrue(ConnectorSet.validURL("https://x.example/mcp"))
        XCTAssertFalse(ConnectorSet.validURL("ftp://x"))
        XCTAssertFalse(ConnectorSet.validURL("https://"))
    }

    func testSecretOnlyGoesToTheWriteOnlyEndpointAndIsNotKept() async throws {
        URLProtocol.registerClass(FakeServer.self)
        Config.testOverride = (gateway: "https://fake.test", token: "test-token")
        defer { URLProtocol.unregisterClass(FakeServer.self); Config.testOverride = nil }
        FakeServer.delay = 0
        FakeServer.seen = []
        FakeServer.routes = ["GET /v1/connectors": (200, json), "PUT /v1/connectors": (200, "{}"), "PUT /v1/connectors/secret/newsrv": (204, "")]
        let model = ConnectorModel()
        await model.refresh()
        let secret = "sk-very-secret-value"
        let ok = await model.addCustom(.init(name: "newsrv", url: "https://new.example/mcp", transport: "http"), secret: secret)
        XCTAssertTrue(ok)
        let put = FakeServer.seen.first { $0.method == "PUT" && $0.path == "/v1/connectors" }
        XCTAssertNotNil(put)
        XCTAssertFalse(String(decoding: put!.body, as: UTF8.self).contains(secret), "the connector list never carries the secret")
        XCTAssertTrue(String(decoding: put!.body, as: UTF8.self).contains("newsrv"))
        let secretCall = FakeServer.seen.first { $0.path == "/v1/connectors/secret/newsrv" }
        XCTAssertEqual(secretCall?.method, "PUT")
        XCTAssertEqual(try JSONSerialization.jsonObject(with: secretCall!.body) as? [String: String], ["value": secret])
        // Nothing the app keeps holds it.
        XCTAssertFalse(String(decoding: try JSONEncoder().encode(model.set), as: UTF8.self).contains(secret))
        XCTAssertFalse(String(describing: UserDefaults.standard.dictionaryRepresentation()).contains(secret))
    }
}
