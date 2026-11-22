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

/// User bug, 2026-11-22: two stale approvals flapped pending ↔ expired between polls and the sheet
/// flashed open and shut.
final class ApprovalFlapTests: XCTestCase {
    private func list(_ statuses: [String: String]) -> [ApprovalRequest] {
        statuses.map { ApprovalRequest(["id": $0.key, "status": $0.value, "command": "ls ~/Downloads"]) }
    }

    func testFlappingListNeverPresents() {
        var previous: Set<String> = []
        var shown: [String] = []
        for statuses in [["e1": "pending", "e2": "expired"], ["e1": "expired", "e2": "pending"],
                         ["e1": "pending", "e2": "expired"], [:], ["e1": "expired", "e2": "pending"]] {
            let (next, pending) = ApprovalCenter.stableNext(list(statuses), previous: previous, seen: [])
            previous = pending
            if let next { shown.append(next.id) }
        }
        XCTAssertEqual(shown, [], "never pending twice in a row")
    }

    func testStablePendingPresentsOnceAndNeverAgain() {
        var previous: Set<String> = []
        var seen: Set<String> = []
        var shown: [String] = []
        for statuses in [["a": "pending"], ["a": "pending"], ["a": "pending"], [:], ["a": "pending"], ["a": "pending"]] {
            let (next, pending) = ApprovalCenter.stableNext(list(statuses), previous: previous, seen: seen)
            previous = pending
            if let next { shown.append(next.id); seen.insert(next.id) }   // markShown
        }
        XCTAssertEqual(shown, ["a"], "second poll presents it; a reappearance after vanishing doesn't")
    }
}

final class ForcedOnceDecideTests: XCTestCase {
    func testForcedReasonFromASecretCall() {
        let body = Data(#"{"ok":true,"forced_once":true,"forced_reason":"시크릿 통화에서 온 요청이라 이번 한 번만 허용했어요."}"#.utf8)
        XCTAssertEqual(ApprovalAPI.forcedNote(body), "시크릿 통화에서 온 요청이라 이번 한 번만 허용했어요.")
        XCTAssertEqual(ApprovalAPI.forcedNote(Data(#"{"forced_once":true}"#.utf8)), "이번 한 번만 허용했어요")
        XCTAssertNil(ApprovalAPI.forcedNote(Data(#"{"ok":true}"#.utf8)))
        XCTAssertNil(ApprovalAPI.forcedNote(Data()), "an empty answer is fine")
    }
}

/// User bug, 2026-11-22: an approval answered on the iPhone stayed open on the Mac, and tapping it
/// there showed "처리할 수 없어요" (409).
@MainActor
final class DecidedElsewhereTests: XCTestCase {
    override func setUp() async throws {
        URLProtocol.registerClass(FakeServer.self)
        Config.testOverride = (gateway: "https://fake.test", token: "test-token")
        FakeServer.routes = [:]
    }

    override func tearDown() async throws {
        URLProtocol.unregisterClass(FakeServer.self)
        Config.testOverride = nil
        FakeServer.routes = [:]
        ApprovalCenter.shared.presented = nil
    }

    func testWhatTheOtherDeviceDidAndAlreadyDecided() {
        XCTAssertEqual(ApprovalCenter.elsewhereText(ApprovalRequest(["id": "1", "status": "approved", "decision": "once"])), "다른 기기에서 허용했어요")
        XCTAssertEqual(ApprovalCenter.elsewhereText(ApprovalRequest(["id": "1", "status": "denied"])), "다른 기기에서 거부했어요")
        XCTAssertEqual(ApprovalCenter.elsewhereText(ApprovalRequest(["id": "1", "status": "expired"])), "만료됐어요")
        XCTAssertEqual(ApprovalCenter.elsewhereText(ApprovalRequest(["id": "1", "status": "used"])), "다른 기기에서 허용했어요")
        XCTAssertTrue(ApprovalCenter.alreadyDecided(GatewayError.http(409)))
        XCTAssertTrue(ApprovalCenter.alreadyDecided(GatewayError.rejected("approval already decided")))
        XCTAssertFalse(ApprovalCenter.alreadyDecided(GatewayError.http(500)))
    }

    func testAFresherCopyLeavesThePendingListAtOnce() {
        let c = ApprovalCenter.shared
        c.update(ApprovalRequest(["id": "zz9", "status": "pending", "command": "ls"]))
        XCTAssertTrue(c.pending.contains { $0.id == "zz9" })
        c.update(ApprovalRequest(["id": "zz9", "status": "approved", "command": "ls"]))
        XCTAssertFalse(c.pending.contains { $0.id == "zz9" }, "the badge drops it")
    }

    func testAPushForADecidedApprovalIsIgnored() async {
        FakeServer.routes = ["GET /v1/approvals/ap77": (200, #"{"id":"ap77","status":"denied","command":"rm x"}"#)]
        await ApprovalCenter.shared.open(id: "ap77")
        XCTAssertNil(ApprovalCenter.shared.presented, "stale: never shown")
        FakeServer.routes = ["GET /v1/approvals/ap78": (200, #"{"id":"ap78","status":"pending","command":"ls"}"#)]
        await ApprovalCenter.shared.open(id: "ap78")
        XCTAssertEqual(ApprovalCenter.shared.presented?.id, "ap78")
    }

    func testForegroundRefreshDropsWhatWasDecidedMeanwhile() async {
        let c = ApprovalCenter.shared
        c.update(ApprovalRequest(["id": "fg1", "status": "pending", "command": "ls"]))
        FakeServer.routes = ["GET /v1/approvals": (200, #"[{"id":"fg1","status":"approved","decision":"once","command":"ls"}]"#)]
        await c.refresh()                                      // what the app does on becoming active
        XCTAssertFalse(c.pending.contains { $0.id == "fg1" })
        XCTAssertNil(c.presented, "nothing stale pops up")
    }

    func testTappingADecidedOneIsA409NotAnError() async {
        FakeServer.routes = ["POST /v1/approvals/ap79": (409, #"{"detail":"approval already decided"}"#)]
        do {
            _ = try await ApprovalAPI.decide("ap79", .once)
            XCTFail("expected 409")
        } catch {
            XCTAssertTrue(ApprovalCenter.alreadyDecided(error), "the sheet turns this into 다른 기기에서 …, not an error")
        }
    }
}

final class AutoApproveTests: XCTestCase {
    func testFramesAndGrantShape() throws {
        let d = JSONDecoder()
        let on = try d.decode(CallEvent.self, from: Data(#"{"type":"auto_approve","value":true,"expires_at":1795310000}"#.utf8))
        XCTAssertEqual(CallClient.reportedAuto(on)?.on, true)
        XCTAssertEqual(CallClient.reportedAuto(on)?.expires, Date(timeIntervalSince1970: 1_795_310_000))
        let state = try d.decode(CallEvent.self, from: Data(#"{"type":"state","value":"listening","auto":true}"#.utf8))
        XCTAssertEqual(CallClient.reportedAuto(state)?.on, true, "a voice grant just flips the state frame")
        XCTAssertNil(CallClient.reportedAuto(try d.decode(CallEvent.self, from: Data(#"{"type":"state","value":"listening"}"#.utf8))))

        let g = AutoGrant(["id": "a1b2c3d4e5", "scope": ["task": "abc123def456"], "devices": ["맥북"], "by": "app",
                           "created": 1_795_300_000, "expires_at": 1_795_307_200, "ran": 2,
                           "log": [["device": "macbook", "command": "brew services restart redis", "at": 1_795_300_100, "stage": "rules"]]])
        XCTAssertEqual(g.scope, "작업")
        XCTAssertEqual(g.devices, ["macbook"])
        XCTAssertEqual(g.log.first?.command, "brew services restart redis")
        XCTAssertEqual(AutoGrant.stageLabel(g.log.first?.stage), "규칙")
        XCTAssertTrue(g.isLive(now: Date(timeIntervalSince1970: 1_795_300_200)))
        XCTAssertFalse(AutoGrant(["id": "x", "ended": true, "log": []]).isLive())
    }

    func testRemainingTimeAndPush() {
        let now = Date(timeIntervalSince1970: 0)
        XCTAssertEqual(AutoGrant.remaining(now.addingTimeInterval(23 * 60), now: now), "23분 남음")
        XCTAssertEqual(AutoGrant.remaining(now.addingTimeInterval(65 * 60), now: now), "1시간 5분 남음")
        XCTAssertEqual(AutoGrant.remaining(now.addingTimeInterval(120 * 60), now: now), "2시간 남음")
        XCTAssertEqual(AutoGrantCenter.grantID(from: ["type": "auto_summary", "grant_id": "a1b2c3d4e5"]), "a1b2c3d4e5")
        XCTAssertNil(AutoGrantCenter.grantID(from: ["type": "approval", "approval_id": "x"]))
    }

    func testCallToggleFrame() throws {
        let client = CallClient()
        client.sendAutoApprove(true, minutes: 30, devices: ["macbook"])   // no socket: nothing sent, no crash
    }
}

final class ApprovalButtonsTests: XCTestCase {
    func testNotificationButtonsSendRealBooleans() throws {
        let info: [AnyHashable: Any] = ["type": "approval", "approval_id": "ap5"]
        let deny = try XCTUnwrap(PushAction.request(action: "DENY", userInfo: info))
        XCTAssertEqual(deny.path, "/v1/approvals/ap5")
        XCTAssertEqual(deny.body?["decision"], "deny")
        XCTAssertEqual(deny.body?["approve"], false as AnyHashable, "a JSON false: Python's bool(\"false\") would approve")
        let sent = try JSONSerialization.data(withJSONObject: deny.body!)
        XCTAssertTrue(String(decoding: sent, as: UTF8.self).contains(#""approve":false"#))
        XCTAssertEqual(PushAction.request(action: "APPROVE_ALWAYS", userInfo: info)?.body?["decision"], "always")
        XCTAssertNil(PushAction.request(action: "APPROVE_ONCE", userInfo: ["type": "task"]))
        XCTAssertTrue(PushAction.categories.map(\.identifier).contains("APPROVAL_ONCE"))
    }

    #if os(macOS)
    @MainActor func testPanelGrowsForTheApprovalRow() {
        XCTAssertEqual(MacCall.panelHeight(typing: false), 112)
        XCTAssertEqual(MacCall.panelHeight(typing: false, approval: true), 142)
    }
    #endif
}
