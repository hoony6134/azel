import XCTest
@testable import Azel

/// 2026-11-23: an overnight task's result wasn't visible in the app.
final class TaskResultTests: XCTestCase {
    func testAnOddTaskNeverEmptiesTheList() throws {
        let json = ##"[{"id":"32be6b34a794","goal":"밤새 조사","status":"done","when":"overnight","result":"요약\n- 끝","summary":null,"created":1795400000,"finished":1795403600,"device":"vd-spark"},{"id":"bad","goal":{"oops":1},"status":7}]"##
        let list = try JSONDecoder().decode([Lenient<AgentTask>].self, from: Data(json.utf8)).compactMap(\.value)
        XCTAssertEqual(list.count, 2, "the odd one decodes leniently too")
        XCTAssertEqual(list[0].result, "요약\n- 끝")
        XCTAssertNotNil(list[0].finished)
        XCTAssertTrue(list[0].isRecentResult(now: Date(timeIntervalSince1970: 1795403600 + 3600)))
        XCTAssertFalse(list[0].isRecentResult(now: Date(timeIntervalSince1970: 1795403600 + 40 * 3600)))
        XCTAssertNotNil(TaskDetailView.timing(list[0]))
    }

    func testPathsInAResultAreFound() {
        let r = "보고서를 `/Users/jh/Downloads/report.md`에 저장했어요. 사본: ~/Documents/a b.txt, 그리고 https://x.y/z"
        XCTAssertEqual(AgentTask.paths(in: r), ["/Users/jh/Downloads/report.md", "~/Documents/a"])
        XCTAssertEqual(AgentTask.paths(in: "결과는 /tmp/out/log.txt."), ["/tmp/out/log.txt"])
        XCTAssertTrue(AgentTask.paths(in: "경로 없음").isEmpty)
    }
}
