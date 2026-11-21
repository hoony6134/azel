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

/// Today's gateway has no `user_title` and answers 400 "unknown keys" to it on PUT.
@MainActor
final class UserTitleGuardTests: XCTestCase {
    func testSavingAnotherSettingOnAnOldGatewayWorks() async throws {
        URLProtocol.registerClass(FakeServer.self)
        Config.testOverride = (gateway: "https://fake.test", token: "test-token")
        defer { URLProtocol.unregisterClass(FakeServer.self); Config.testOverride = nil; FakeServer.routes = [:]; AppSettings.supportsUserTitle = false }
        let old = #"{"default_model":"auto","claude_path":"auto","task_defaults":{},"monthly_budget_usd":500,"budget_warn_pct":80,"default_device":"macbook","device_fallback":[],"proactive":{"morning_briefing":"08:00"}}"#
        FakeServer.seen = []
        FakeServer.routes = ["GET /v1/settings": (200, old), "PUT /v1/settings": (200, "{}")]

        var s = try await Gateway.get("/v1/settings") as AppSettings
        AppSettings.supportsUserTitle = s.user_title != nil
        XCTAssertFalse(AppSettings.supportsUserTitle)
        s.monthly_budget_usd = 300
        s.user_title = "정훈님"            // even if something set it, it must not go out
        try await Gateway.saveSettings(s)
        let body = String(decoding: FakeServer.seen.last!.body, as: UTF8.self)
        XCTAssertFalse(body.contains("user_title"), body)
        XCTAssertTrue(body.contains("300"))

        AppSettings.supportsUserTitle = true
        try await Gateway.saveSettings(s)
        XCTAssertTrue(String(decoding: FakeServer.seen.last!.body, as: UTF8.self).contains("user_title"))
    }
}

@MainActor
final class ReviewFixTests: XCTestCase {
    func testAnApprovalCountsAsSeenOnlyOnceShown() throws {
        let a = ApprovalRequest(["id": "ap9", "status": "pending", "command": "ls"])
        let c = ApprovalCenter.shared
        c.show(a)                                        // e.g. under the call cover: not on screen yet
        XCTAssertEqual(ApprovalCenter.nextToShow([a], seen: [])?.id, "ap9")
        c.markShown("ap9")
        c.presented = nil
    }

    func testForcedOnceNoteAndParsing() {
        let a = ApprovalRequest(["id": "1", "command": "rm -rf build", "forced_once": true, "forced_reason": "삭제 명령"])
        XCTAssertTrue(a.forcedOnce)
        XCTAssertEqual(a.forcedNote, "이 명령은 매번 확인이 필요해요: 삭제 명령")
        XCTAssertNil(ApprovalRequest(["id": "2", "command": "ls"]).forcedNote)
    }
}
