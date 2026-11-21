import XCTest
@testable import Biseo

final class TaskTests: XCTestCase {
    func testDecodingIsTolerant() throws {
        let json = #"""
        {"tasks":[
          {"id":"t1","goal":"과제 정리","status":"running","steps":[{"ts":"2026-11-22T10:00:00+09:00","label":"캘린더 확인","device":"맥북","tool":"gcal_list"}],"cost":0.12,"device":"macbook"},
          {"id":7,"goal":"서버 점검","status":"waiting_approval","steps":[{"ts":1795309200,"label":"명령 실행 대기"}],"cost":{"cost_usd":0.5}},
          {"id":"t3","status":"done","result":"끝났어요"}]}
        """#
        let data = Data(json.utf8)
        struct Envelope: Decodable { let tasks: [AgentTask] }
        let tasks = try JSONDecoder().decode(Envelope.self, from: data).tasks
        XCTAssertEqual(tasks.map(\.id), ["t1", "7", "t3"])
        XCTAssertEqual(tasks[0].steps.first?.ts, Date(timeIntervalSince1970: 1_795_309_200))
        XCTAssertEqual(tasks[1].steps.first?.ts, tasks[0].steps.first?.ts, "epoch and ISO times both read")
        XCTAssertEqual(tasks[1].cost, 0.5)
        XCTAssertEqual(tasks[2].goal, "")
        XCTAssertEqual(tasks.filter(\.isActive).map(\.id), ["t1", "7"])
    }

    func testBadgesAlwaysHaveAWord() {
        for s in ["queued", "running", "waiting_approval", "done", "failed", "cancelled"] {
            XCTAssertFalse(TaskBadge.label(s).isEmpty)
            XCTAssertNotEqual(TaskBadge.label(s), s, "\(s) is translated")
        }
        XCTAssertEqual(TaskBadge.label("waiting_approval"), "승인 필요")
        XCTAssertEqual(TaskBadge.label("mystery"), "mystery")
    }

    func testCreateBodyAndDeepLinks() {
        let b = TaskAPI.body(goal: "  정리해 줘 \n", device: "macbook", persona: nil, notify: .call)
        XCTAssertEqual(b["goal"] as? String, "정리해 줘")
        XCTAssertEqual(b["notify"] as? String, "call")
        XCTAssertNil(b["persona"])
        XCTAssertEqual(TaskModel.taskID(from: URL(string: "azel://task/t42")!), "t42")
        XCTAssertNil(TaskModel.taskID(from: URL(string: "azel://other/t42")!))
        XCTAssertEqual(TaskModel.taskID(from: ["task_id": "t1"]), "t1")
        XCTAssertEqual(TaskModel.taskID(from: ["task": 9]), "9")
    }
}

final class PushRegisterTests: XCTestCase {
    func testRegisterBodyCarriesBothTokensAsHex() {
        let body = Gateway.registerBody(voip: Data([0xab, 0x01]), alert: Data([0x0f, 0xff]), bundleID: "xyz.scian.biseo", env: "sandbox")
        XCTAssertEqual(body["voip_token"] as? String, "ab01")
        XCTAssertEqual(body["alert_token"] as? String, "0fff")
        XCTAssertEqual(body["bundle_id"] as? String, "xyz.scian.biseo")
        XCTAssertEqual(body["env"] as? String, "sandbox")
        let voipOnly = Gateway.registerBody(voip: Data([0x01]), alert: nil, bundleID: "b", env: "production")
        XCTAssertNil(voipOnly["alert_token"], "no alert token yet: the key is left out")
        XCTAssertEqual(voipOnly["voip_token"] as? String, "01")
    }
}

final class TaskBackendTests: XCTestCase {
    private let base = #"{"default_model":"auto","claude_path":"auto","task_defaults":{},"monthly_budget_usd":500,"budget_warn_pct":80,"default_device":"macbook","device_fallback":[],"proactive":{"morning_briefing":"08:00"}"#

    func testOldGatewayNeverGetsTaskBackend() throws {
        let s = try JSONDecoder().decode(AppSettings.self, from: Data((base + "}").utf8))
        XCTAssertNil(s.task_backend, "hidden picker")
        XCTAssertFalse(String(decoding: try JSONEncoder().encode(s), as: UTF8.self).contains("task_backend"))
    }

    func testNewGatewayRoundTripsIt() throws {
        var s = try JSONDecoder().decode(AppSettings.self, from: Data((base + #","task_backend":"claude_api"}"#).utf8))
        XCTAssertEqual(s.task_backend, "claude_api")
        s.task_backend = "qwen"
        XCTAssertTrue(String(decoding: try JSONEncoder().encode(s), as: UTF8.self).contains(#""task_backend":"qwen""#))
    }

    func testNIMOnlyWhenEnabled() {
        XCTAssertEqual(TaskBackend.options(nimEnabled: false, current: "claude_api"), [.claude_api, .claude_code, .qwen])
        XCTAssertEqual(TaskBackend.options(nimEnabled: true, current: "claude_api").count, 4)
        XCTAssertTrue(TaskBackend.options(nimEnabled: false, current: "nim").contains(.nim), "the chosen one stays visible")
    }

    func testTaskShowsBackendAndFallback() throws {
        let t = try JSONDecoder().decode(AgentTask.self, from: Data(#"{"id":"t1","goal":"메일 정리","status":"done","backend":"claude_code","fallback_reason":"Google 연결이 끊겼어요"}"#.utf8))
        XCTAssertEqual(TaskBackend.label(t.backend!), "Claude Code")
        XCTAssertEqual(t.fallback_reason, "Google 연결이 끊겼어요")
        let old = try JSONDecoder().decode(AgentTask.self, from: Data(#"{"id":"t2","goal":"x","status":"running"}"#.utf8))
        XCTAssertNil(old.backend)
        XCTAssertEqual(TaskBackend.label("mystery"), "mystery")
    }
}

final class Batch5AppTests: XCTestCase {
    func testTaskViewFieldsFromTheGateway() throws {
        let t = try JSONDecoder().decode(AgentTask.self, from: Data(#"{"id":"abc123def456","goal":"메일 정리","status":"queued","notify":"push","created":1795300000,"cost_usd":0.12,"note":"다른 작업 2개가 끝나길 기다리는 중","error":"","steps":[{"label":"메일 확인"}]}"#.utf8))
        XCTAssertEqual(t.note, "다른 작업 2개가 끝나길 기다리는 중")
        XCTAssertEqual(t.notify, "push")
        XCTAssertNil(t.error, "empty error hidden")
        XCTAssertEqual(t.cost, 0.12)
        XCTAssertEqual(t.created_at, Date(timeIntervalSince1970: 1_795_300_000))
        XCTAssertEqual(t.steps.map(\.label), ["메일 확인"])
    }

    @MainActor func testMemoryItems() {
        let m = MemoryItem(["id": "123", "text": "커피는 디카페인", "source": "auto", "about": "취향", "persona": NSNull(), "in_cygnus": true, "created": "2026-11-22T10:00:00Z"])
        XCTAssertEqual(m.id, "123")
        XCTAssertEqual(m.sourceLabel, "통화에서")
        XCTAssertEqual(m.scopeLabel, "모든 비서")
        XCTAssertTrue(m.inCygnus)
        XCTAssertEqual(m.about, "취향")
        XCTAssertNotNil(m.created)
        XCTAssertEqual(MemoryItem(["id": "1", "text": "x", "source": "explicit"]).sourceLabel, "직접 기억")
        XCTAssertFalse(MemoryItem(["id": "2", "text": "y"]).inCygnus)
    }

    @MainActor func testMemoryRoutesAndHiding() async throws {
        URLProtocol.registerClass(FakeServer.self)
        Config.testOverride = (gateway: "https://fake.test", token: "test-token")
        defer { URLProtocol.unregisterClass(FakeServer.self); Config.testOverride = nil; FakeServer.routes = [:] }
        let model = MemoryModel()
        FakeServer.routes = ["GET /v1/memories": (502, "bad gateway")]
        await model.refresh()
        XCTAssertEqual(model.available, false, "memory service not redeployed yet: hidden")
        FakeServer.routes = ["GET /v1/memories": (200, #"[{"id":"5","text":"a","source":"explicit","persona":null,"in_cygnus":false,"created":"2026-11-22T10:00:00Z"}]"#),
                             "DELETE /v1/memories/5": (200, #"{"ok":true,"spoken":"지웠어요","data":{"id":"5"}}"#)]
        await model.refresh()
        XCTAssertEqual(model.available, true)
        XCTAssertEqual(model.items.map(\.id), ["5"])
        try await model.delete(model.items[0])
        XCTAssertTrue(model.items.isEmpty)
    }

    func testCallContextChip() {
        XCTAssertEqual(CallContext.from(["kind": "task", "label": "메일 정리", "id": "t1"])?.chip, "맡긴 작업: 메일 정리")
        XCTAssertEqual(CallContext.from(["kind": "schedule", "label": "약 먹기"])?.chip, "예약: 약 먹기")
        XCTAssertEqual(CallContext.from("과제 마감")?.chip, "과제 마감")
        XCTAssertNil(CallContext.from(["kind": "task"]))
        XCTAssertNil(CallContext.from(nil))
    }
}
