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
