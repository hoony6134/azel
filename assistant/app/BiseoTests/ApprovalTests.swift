import XCTest
@testable import Biseo

@MainActor
final class ApprovalTests: XCTestCase {
    override func setUp() async throws {
        URLProtocol.registerClass(FakeServer.self)
        Config.testOverride = (gateway: "https://fake.test", token: "test-token")
        FakeServer.seen = []
        FakeServer.routes = [:]
    }

    override func tearDown() async throws {
        URLProtocol.unregisterClass(FakeServer.self)
        Config.testOverride = nil
        FakeServer.routes = [:]
    }

    private func item(_ json: String) throws -> ApprovalRequest {
        ApprovalRequest(try JSONSerialization.jsonObject(with: Data(json.utf8)) as! [String: Any])
    }

    func testParsesNewAndOldShapes() throws {
        let a = try item(#"{"id":"ap1","device":"jeonghun-macbook","command":"rm -rf build && make","cwd":"/Users/jh/dev","persona":"p1","created_at":"2026-11-22T10:00:00Z","expires_at":"2026-11-22T10:05:00Z","status":"pending"}"#)
        XCTAssertEqual(a.command, "rm -rf build && make")
        XCTAssertEqual(a.cwd, "/Users/jh/dev")
        XCTAssertEqual(a.persona, "p1")
        XCTAssertEqual(a.secondsLeft(now: a.created!), 300)
        XCTAssertEqual(ApprovalRequest.countdown(299), "4:59")
        XCTAssertTrue(a.isExpired(now: a.expires!))
        // The old gateway: numeric id, epoch created, no expiry, no status.
        let old = try item(#"{"id":7,"device":"mac","tool":"run_command","command":"ls","created":1795000000}"#)
        XCTAssertEqual(old.id, "7")
        XCTAssertTrue(old.isPending)
        XCTAssertNil(old.secondsLeft())
        XCTAssertFalse(old.isExpired(), "no expiry never expires on the client")
    }

    func testDecisionBodyKeepsTheOldApproveFlag() {
        XCTAssertEqual(ApprovalAPI.body(.once)["approve"] as? Bool, true)
        XCTAssertEqual(ApprovalAPI.body(.always)["approve"] as? Bool, true)
        XCTAssertEqual(ApprovalAPI.body(.deny)["approve"] as? Bool, false)
        XCTAssertEqual(ApprovalAPI.body(.always)["decision"] as? String, "always")
    }

    func testNextToShowIsTheOldestUnseenLiveOne() throws {
        let now = ISO8601DateFormatter().date(from: "2026-11-22T10:01:00Z")!
        let list = try [
            item(#"{"id":"b","created_at":"2026-11-22T10:00:30Z","expires_at":"2026-11-22T10:05:30Z","status":"pending"}"#),
            item(#"{"id":"a","created_at":"2026-11-22T10:00:00Z","expires_at":"2026-11-22T10:05:00Z","status":"pending"}"#),
            item(#"{"id":"x","created_at":"2026-11-22T09:00:00Z","expires_at":"2026-11-22T09:05:00Z","status":"pending"}"#),
            item(#"{"id":"d","created_at":"2026-11-22T08:00:00Z","status":"denied"}"#),
        ]
        XCTAssertEqual(ApprovalCenter.nextToShow(list, seen: [], now: now)?.id, "a", "expired and decided ones skipped")
        XCTAssertEqual(ApprovalCenter.nextToShow(list, seen: ["a"], now: now)?.id, "b")
        XCTAssertNil(ApprovalCenter.nextToShow(list, seen: ["a", "b"], now: now))
    }

    func testPushPayload() {
        XCTAssertEqual(ApprovalCenter.approvalID(from: ["type": "approval", "approval_id": "ap1"]), "ap1")
        XCTAssertEqual(ApprovalCenter.approvalID(from: ["type": "approval", "approval_id": 3]), "3")
        XCTAssertNil(ApprovalCenter.approvalID(from: ["type": "task", "task_id": "t1"]))
        XCTAssertNil(TaskModel.taskID(from: ["type": "approval", "approval_id": "ap1"]), "an approval push is not a task push")
    }

    func testOutcomeLabels() throws {
        XCTAssertEqual(ApprovalRequest.outcome(try item(#"{"id":"1","status":"approved","decision":"always"}"#)), "항상 허용")
        XCTAssertEqual(ApprovalRequest.outcome(try item(#"{"id":"1","status":"approved"}"#)), "허용")
        XCTAssertEqual(ApprovalRequest.outcome(try item(#"{"id":"1","status":"denied"}"#)), "거부")
        XCTAssertEqual(ApprovalRequest.outcome(try item(#"{"id":"1","status":"expired"}"#)), "만료")
    }

    func testDecideRulesAndRevokeHitTheGateway() async throws {
        FakeServer.routes = [
            "POST /v1/approvals/ap 1": (200, "{}"),
            "GET /v1/approvals/rules": (200, #"{"rules":[{"id":"r1","device":"mac","command":"make test"}]}"#),
            "DELETE /v1/approvals/rules/r1": (204, ""),
        ]
        try await ApprovalAPI.decide("ap 1", .always)
        let sent = try JSONSerialization.jsonObject(with: FakeServer.seen.last!.body) as! [String: Any]
        XCTAssertEqual(sent["decision"] as? String, "always")
        XCTAssertEqual(sent["approve"] as? Bool, true)
        let rules = try await ApprovalAPI.rules()
        XCTAssertEqual(rules.map(\.command), ["make test"])
        try await ApprovalAPI.revoke("r1")
        XCTAssertEqual(FakeServer.seen.last?.method, "DELETE")
    }

    func testListAcceptsPendingOnlyFromAnOldGateway() async throws {
        FakeServer.routes = ["GET /v1/approvals": (200, #"[{"id":"1","device":"mac","command":"ls"}]"#)]
        let list = try await ApprovalAPI.list()
        XCTAssertEqual(list.map(\.id), ["1"])
        XCTAssertTrue(list[0].isPending)
    }
}
