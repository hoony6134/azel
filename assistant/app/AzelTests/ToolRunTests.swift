import XCTest
@testable import Azel

@MainActor
final class ToolRunTests: XCTestCase {
    private func ev(_ json: String) throws -> CallEvent { try JSONDecoder().decode(CallEvent.self, from: Data(json.utf8)) }

    func testActivityEventsDecodeTheRunFields() throws {
        let s = try ev(#"{"type":"activity","phase":"tool_start","call_id":"c1","tool":"run_command","label":"빌드 중","host":"vf-spark","input":"make -j8","kind":"terminal"}"#)
        XCTAssertEqual(s.call_id, "c1"); XCTAssertEqual(s.host, "vf-spark"); XCTAssertEqual(s.input, "make -j8")
        let j = try ev(#"{"type":"activity","phase":"tool_start","call_id":7,"tool":"gcal_list","input":{"from":"2026-11-24","to":"2026-11-24"}}"#)
        XCTAssertEqual(j.call_id, "7", "a numeric id too")
        XCTAssertTrue(j.input?.contains("\"from\" : \"2026-11-24\"") == true, "JSON input pretty-printed: \(j.input ?? "")")
        let compact = try ev(#"{"type":"activity","phase":"tool_start","call_id":"c2","tool":"web_fetch","input":"{\"url\":\"https://a.b/c\"}"}"#)
        XCTAssertEqual(compact.input, "{\n  \"url\" : \"https://a.b/c\"\n}", "compact JSON text (the contract) pretty-printed")
        let notJSON = try ev(#"{"type":"activity","phase":"tool_start","call_id":"c3","tool":"run_command","input":"{ echo hi; }"}"#)
        XCTAssertEqual(notJSON.input, "{ echo hi; }", "a command that only looks like JSON stays as is")
        let e = try ev(#"{"type":"activity","phase":"tool_end","call_id":"c1","exit_code":2,"ok":false,"duration_ms":1530,"output":"err","truncated":true,"task_id":"t9"}"#)
        XCTAssertEqual(e.exit_code, 2); XCTAssertEqual(e.ok, false); XCTAssertEqual(e.duration_ms, 1530); XCTAssertEqual(e.truncated, true)
    }

    func testARunStreamsAndFinishes() throws {
        let store = ToolRunStore()
        store.apply(try ev(#"{"type":"activity","phase":"tool_start","call_id":"c1","tool":"run_command","label":"빌드 중","host":"vf-spark","input":"make"}"#))
        store.apply(try ev(#"{"type":"activity","phase":"tool_output","call_id":"c1","output":"cc a.c\ncc b"}"#))
        store.apply(try ev(#"{"type":"activity","phase":"tool_output","call_id":"c1","output":".c\nld"}"#))
        let run = try XCTUnwrap(store.run("c1"))
        XCTAssertEqual(run.lines.map(\.text), ["cc a.c", "cc b.c", "ld"], "a chunk can end mid-line")
        XCTAssertFalse(run.finished)
        store.apply(try ev(#"{"type":"activity","phase":"tool_end","call_id":"c1","exit_code":0,"ok":true,"duration_ms":2100,"output":"IGNORED tail"}"#))
        XCTAssertTrue(run.finished); XCTAssertEqual(run.ok, true); XCTAssertEqual(run.durationMs, 2100)
        XCTAssertEqual(run.text, "IGNORED tail", "untruncated end output is the whole output: it replaces the stream")
        store.apply(try ev(#"{"type":"activity","phase":"tool_output","call_id":"other","output":"x"}"#))   // unknown run: ignored
        XCTAssertEqual(store.runs.count, 1)
    }

    /// Streamed chunks can skip a stretch (more than 2 KB in half a second); tool_end's last 8 KB fixes the tail.
    func testTheEndOutputRepairsASkippedStretch() {
        let streamed = ["line 1", "line 2", "line 3", "line 7", "line 8"]          // 4–6 were skipped
        let end = "ne 5\nline 6\nline 7\nline 8\nline 9"                          // last 8 KB, starts mid-line
        XCTAssertEqual(ToolRun.reconcile(streamed: streamed, end: end, endIsWhole: false),
                       ["line 1", "line 2", "line 3", "ne 5", "line 6", "line 7", "line 8", "line 9"])
        XCTAssertEqual(ToolRun.reconcile(streamed: ["a b c d"], end: "zzzz\nyyyy", endIsWhole: false), ["a b c d", "⋯", "zzzz", "yyyy"], "no anchor")
        XCTAssertEqual(ToolRun.reconcile(streamed: ["x"], end: "x\ny", endIsWhole: true), ["x", "y"])
    }

    func testEndOutputIsUsedWhenNothingStreamedAndFailuresShowTheCode() throws {
        let store = ToolRunStore()
        store.apply(try ev(#"{"type":"activity","phase":"tool_start","call_id":"c2","tool":"read_file"}"#))
        store.apply(try ev(#"{"type":"activity","phase":"tool_end","call_id":"c2","exit_code":1,"output":"no such file","truncated":true}"#))
        let run = try XCTUnwrap(store.run("c2"))
        XCTAssertEqual(run.text, "no such file"); XCTAssertTrue(run.clipped); XCTAssertEqual(run.ok, false, "exit 1 without ok: failed")
    }

    func testOutputIsCappedKeepingTheNewest() throws {
        let store = ToolRunStore()
        store.apply(try ev(#"{"type":"activity","phase":"tool_start","call_id":"big","tool":"run_command"}"#))
        let run = try XCTUnwrap(store.run("big"))
        let line = String(repeating: "x", count: 1023) + "\n"
        for _ in 0..<100 { run.append(line) }           // 100 KB
        run.append("last")
        XCTAssertLessThanOrEqual(run.text.utf8.count, ToolRun.cap + 1100)
        XCTAssertTrue(run.clipped)
        XCTAssertEqual(run.lines.last?.text, "last")
    }

    func testTapOpensTheRunningStepElseTheList() throws {
        let store = ToolRunStore()
        var log = ActivityLog()
        let start = try ev(#"{"type":"activity","phase":"tool_start","call_id":"c3","tool":"run_command","label":"실행 중"}"#)
        _ = log.apply(start); store.apply(start)
        XCTAssertEqual(log.tapTarget(store), .run("c3"))
        let end = try ev(#"{"type":"activity","phase":"tool_end","call_id":"c3","label":"실행함","exit_code":0}"#)
        _ = log.apply(end); store.apply(end)
        XCTAssertEqual(log.done.last?.callId, "c3", "the done step keeps its run")
        XCTAssertEqual(log.tapTarget(store), .list)
        XCTAssertEqual(ToolRunDetailView.duration(1530), "1.5초")
        XCTAssertEqual(ToolRunDetailView.duration(450), "450ms")
    }
}
