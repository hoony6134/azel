import XCTest
@testable import Biseo

@MainActor
final class NIMTests: XCTestCase {
    private let base = #"{"default_model":"nim","claude_path":"auto","task_defaults":{"chat":{"model":"nim"}},"monthly_budget_usd":500,"budget_warn_pct":80,"default_device":"macbook","device_fallback":[],"proactive":{"morning_briefing":"08:00"}"#

    func testProvidersDecodeAndPutWithoutHasKey() throws {
        XCTAssertNil(try JSONDecoder().decode(AppSettings.self, from: Data((base + "}").utf8)).providers)
        let s = try JSONDecoder().decode(AppSettings.self, from: Data((base + #","providers":{"nim":{"enabled":true,"base_url":"https://integrate.api.nvidia.com/v1","model":"meta/llama-3.1-70b-instruct","has_key":true}}}"#).utf8))
        XCTAssertEqual(s.providers?.nim?.model, "meta/llama-3.1-70b-instruct")
        XCTAssertEqual(s.providers?.nim?.has_key, true)
        XCTAssertEqual(s.default_model, "nim")
        let out = String(decoding: try JSONEncoder().encode(s), as: UTF8.self)
        XCTAssertFalse(out.contains("has_key"))
        XCTAssertTrue(out.contains("integrate.api.nvidia.com"))
    }

    func testModelListShapesAndKeyNeverKept() async throws {
        URLProtocol.registerClass(FakeServer.self)
        Config.testOverride = (gateway: "https://fake.test", token: "test-token")
        defer { URLProtocol.unregisterClass(FakeServer.self); Config.testOverride = nil }
        FakeServer.delay = 0
        FakeServer.seen = []
        FakeServer.routes = ["GET /v1/providers/nim/models": (200, #"{"data":[{"id":"a/b"},{"id":"c/d"}]}"#),
                             "PUT /v1/providers/nim/key": (204, "")]
        let models = try await NIMAPI.models()
        XCTAssertEqual(models, ["a/b", "c/d"])
        let key = "nvapi-very-secret"
        try await NIMAPI.putKey(key)
        let put = FakeServer.seen.first { $0.path == "/v1/providers/nim/key" }
        XCTAssertEqual(try JSONSerialization.jsonObject(with: put!.body) as? [String: String], ["value": key])
        XCTAssertFalse(String(describing: UserDefaults.standard.dictionaryRepresentation()).contains(key))
    }
}
