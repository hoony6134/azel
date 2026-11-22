import XCTest
@testable import Azel

final class HistoryTests: XCTestCase {
    private var dir: URL!

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("hist-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDown() { try? FileManager.default.removeItem(at: dir) }

    private func item(_ id: String, _ at: Double, session: String? = nil, title: String? = nil) -> HistoryItem {
        HistoryItem(id: id, session: session, title: title, started_at: at)
    }

    func testDecodesCurrentAndLegacyItems() throws {
        let json = #"""
        [{"id":"c1","session":"iphone-2026-11-21","title":"오늘 일정 뭐 있어?","last_reply":"오전에 회의가 있어요.","started_at":1763700000.5,
          "duration_s":240,"direction":"outgoing","reason":null,"client":"iphone","turns":6,"routes":{"qwen":2,"claude":4},"kinds":["calendar"]},
         {"id":42,"title":"옛날 통화","started_at":1760000000}]
        """#
        let items = try JSONDecoder().decode([HistoryItem].self, from: Data(json.utf8))
        XCTAssertEqual(items[0].routeList, ["qwen", "claude"])
        XCTAssertEqual(items[0].turns, 6)
        XCTAssertFalse(items[0].incoming)
        XCTAssertEqual(items[1].id, "42", "numeric ids become strings")
        XCTAssertNil(items[1].session)
        XCTAssertNil(items[1].kinds)
    }

    func testDecodesDetailWithSentenceListsAndActivity() throws {
        let json = #"""
        {"id":"c1","session":"s","started_at":1763700000,"direction":"incoming","turns":[
          {"at":0,"user":"내일 일정?","assistant":["내일은","회의가 있어요."],"route":"claude",
           "activity":[{"label":"캘린더 확인","kind":"calendar","at":1.2}],"interrupted":true},
          {"at":9.5,"user":"고마워","assistant":"천만에요.","route":"qwen"}]}
        """#
        let call = try JSONDecoder().decode(HistoryCall.self, from: Data(json.utf8))
        XCTAssertTrue(call.item.incoming)
        XCTAssertNil(call.item.turns, "the turn list doesn't leak into the count")
        XCTAssertEqual(call.turns.count, 2)
        XCTAssertEqual(call.turns[0].assistant, "내일은 회의가 있어요.")
        XCTAssertEqual(call.turns[0].activity?.first?.at, 1.2)
        XCTAssertEqual(call.turns[1].assistant, "천만에요.")
        // Round trip through the offline cache keeps the turns.
        let back = try JSONDecoder().decode(HistoryCall.self, from: JSONEncoder().encode(call))
        XCTAssertEqual(back, call)
    }

    func testPagerMergesPagesWithoutDuplicatesAndStopsOnShortPage() {
        var pager = HistoryPager()
        pager.reset(with: [item("a", 300), item("b", 200)], pageSize: 2)
        XCTAssertFalse(pager.exhausted)
        XCTAssertEqual(pager.nextBefore, 200)
        pager.append([item("b", 200), item("c", 100)], pageSize: 2) // overlap at the page boundary
        XCTAssertEqual(pager.items.map(\.id), ["a", "b", "c"])
        XCTAssertFalse(pager.exhausted)
        pager.append([item("d", 50)], pageSize: 2)
        XCTAssertTrue(pager.exhausted)
        pager.remove("b")
        pager.insert(item("b", 200))
        XCTAssertEqual(pager.items.map(\.id), ["a", "b", "c", "d"], "a restored row goes back in place")
    }

    func testOfflineCacheKeepsListAndOpenedCallsAndForgetsDeletedOnes() {
        let cache = HistoryCache(directory: dir)
        let when = Date(timeIntervalSince1970: 1_763_700_000)
        cache.saveList([item("a", 300, title: "회의 일정"), item("b", 200, title: "메일 요약")], at: when)
        cache.saveCall(HistoryCall(item: item("a", 300), turns: [HistoryCall.Turn(at: 0, user: "안녕")]))
        XCTAssertEqual(cache.loadList()?.items.map(\.id), ["a", "b"])
        XCTAssertEqual(cache.loadList()?.fetchedAt, when)
        XCTAssertEqual(cache.loadCall("a")?.turns.first?.user, "안녕")
        XCTAssertNil(cache.loadCall("b"))
        XCTAssertEqual(HistoryCache.filter(cache.loadList()!.items, query: "메일").map(\.id), ["b"], "offline search is titles only")
        cache.remove("a")
        XCTAssertNil(cache.loadCall("a"))
        XCTAssertEqual(cache.loadList()?.items.map(\.id), ["b"])
    }

    func testRecordingsLinkBySessionOrByStartTimeForLegacyItems() throws {
        let start = Date(timeIntervalSince1970: 1_763_700_000)
        let mine = dir.appendingPathComponent(Recordings.fileName(session: "iphone-2026-11-21", start: start))
        let other = dir.appendingPathComponent(Recordings.fileName(session: "iphone-2026-11-21-090000", start: start))
        let underscored = dir.appendingPathComponent(Recordings.fileName(session: "a_b", start: start))
        for url in [mine, other, underscored] { try Data([1]).write(to: url) }
        XCTAssertEqual(Recordings.matching(item("1", 0, session: "iphone-2026-11-21"), in: dir), [mine])
        XCTAssertEqual(Recordings.matching(item("2", 0, session: "a_b"), in: dir), [underscored])
        XCTAssertEqual(Set(Recordings.matching(item("3", start.timeIntervalSince1970 + 45), in: dir)), Set([mine, other, underscored]),
                       "legacy item: within ±60 s")
        XCTAssertEqual(Recordings.matching(item("4", start.timeIntervalSince1970 + 120), in: dir), [])
    }

    @MainActor func testRetentionCountUsesCachedList() {
        let cache = HistoryCache(directory: dir)
        let now = Date(timeIntervalSince1970: 1_763_700_000)
        cache.saveList([item("new", now.timeIntervalSince1970 - 86_400), item("old", now.timeIntervalSince1970 - 40 * 86_400)])
        let model = HistoryModel(cache: cache)
        XCTAssertEqual(model.count(olderThan: 30, now: now), 1)
        XCTAssertEqual(model.count(olderThan: 7, now: now), 1)
        XCTAssertEqual(model.count(olderThan: 0, now: now), 2)
    }

    func testDayTitles() {
        let now = Date()
        XCTAssertEqual(HistoryFormat.dayTitle(now, now: now), "오늘")
        XCTAssertEqual(HistoryFormat.dayTitle(now.addingTimeInterval(-86_400), now: now), "어제")
        let grouped = HistoryFormat.byDay([item("a", now.timeIntervalSince1970), item("b", now.timeIntervalSince1970 - 60),
                                           item("c", now.timeIntervalSince1970 - 86_400)])
        XCTAssertEqual(grouped.map(\.0), ["오늘", "어제"])
        XCTAssertEqual(grouped[0].1.map(\.id), ["a", "b"])
    }
}
