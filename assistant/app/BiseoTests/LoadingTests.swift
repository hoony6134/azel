import XCTest
@testable import Biseo

/// Answers requests to https://fake.test from a route table, optionally after a delay.
final class FakeServer: URLProtocol {
    nonisolated(unsafe) static var routes: [String: (Int, String)] = [:]
    nonisolated(unsafe) static var delay: TimeInterval = 0
    /// Every request seen: method, path and body.
    /// Binary bodies (wav) keyed like `routes`; checked first.
    nonisolated(unsafe) static var rawRoutes: [String: Data] = [:]
    /// Responses served once each, in order, before falling back to `routes`.
    nonisolated(unsafe) static var queued: [String: [(Int, String)]] = [:]
    nonisolated(unsafe) static var seen: [(method: String, path: String, body: Data)] = []

    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "fake.test" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let url = request.url!
        var sent = request.httpBody ?? Data()
        if sent.isEmpty, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var buf = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable { let n = stream.read(&buf, maxLength: buf.count); if n <= 0 { break }; sent.append(buf, count: n) }
        }
        Self.seen.append((request.httpMethod ?? "GET", url.path, sent))
        let key = "\(request.httpMethod ?? "GET") \(url.path)"
        let raw = Self.rawRoutes[key]
        var next: (Int, String)?
        if var q = Self.queued[key], !q.isEmpty { next = q.removeFirst(); Self.queued[key] = q }
        let (code, body) = raw.map { _ in (200, "") } ?? next ?? Self.routes[key] ?? Self.routes[url.path] ?? (404, #"{"detail":"Not Found"}"#)
        DispatchQueue.global().asyncAfter(deadline: .now() + Self.delay) { [self] in
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: url, statusCode: code, httpVersion: nil, headerFields: nil)!,
                                cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: raw ?? Data(body.utf8))
            client?.urlProtocolDidFinishLoading(self)
        }
    }

    override func stopLoading() {}
}

@MainActor
final class LoadingTests: XCTestCase {
    private var dir: URL!

    override func setUp() async throws {
        URLProtocol.registerClass(FakeServer.self)
        Config.testOverride = (gateway: "https://fake.test", token: "test-token")
        FakeServer.delay = 0
        FakeServer.routes = [
            "/v1/status": (200, #"{"spark":{"ok":true,"ms":12},"gateway":{"ok":true,"ms":3},"devices":[]}"#),
            "/v1/settings": (500, "oops"),
            // /v1/approvals and /v1/history are missing: 404, like an old gateway
            "/v1/schedule": (502, "bad gateway"),
        ]
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("load-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDown() async throws {
        URLProtocol.unregisterClass(FakeServer.self)
        Config.testOverride = nil
        FakeServer.rawRoutes = [:]
        try? FileManager.default.removeItem(at: dir)
    }

    func testClassification() {
        XCTAssertNil(SectionStatus.from(CancellationError()))
        XCTAssertNil(SectionStatus.from(URLError(.cancelled)))
        XCTAssertEqual(SectionStatus.from(GatewayError.http(404)), .unavailable)
        XCTAssertTrue(SectionStatus.from(GatewayError.http(503))!.isFailed)
        XCTAssertTrue(SectionStatus.from(URLError(.timedOut))!.isFailed)
    }

    func testSectionsLoadIndependently() async {
        let app = AppModel(sessionPrefix: "unittest")
        await app.refresh()
        XCTAssertNotNil(app.status, "200 section loads although settings returned 500")
        XCTAssertNil(app.statusError)
        XCTAssertEqual(app.approvalsStatus, .unavailable, "404 is a quiet note, not an error")

        let schedule = ScheduleModel()
        await schedule.refresh()
        XCTAssertTrue(schedule.status.isFailed, "502 gets a retry")

        let cache = HistoryCache(directory: dir)
        cache.saveList([HistoryItem(id: "cached", title: "어제 통화", started_at: 1)])
        let history = HistoryModel(cache: cache)
        await history.refresh()
        XCTAssertEqual(history.status, .unavailable)
        XCTAssertNil(history.error)
        XCTAssertFalse(history.isOffline)
        XCTAssertEqual(history.items.map(\.id), ["cached"], "cached rows stay visible")
    }

    func testCancelledRefreshLeavesNoError() async {
        FakeServer.delay = 0.3
        FakeServer.routes["/v1/history"] = (200, #"[{"id":"new","started_at":5}]"#)
        let history = HistoryModel(cache: HistoryCache(directory: dir))
        let refresh = Task { await history.refresh() }
        refresh.cancel()                          // what .refreshable does when the view updates
        await refresh.value
        XCTAssertNil(history.error)
        XCTAssertEqual(history.status, .ok)
        XCTAssertEqual(history.items.map(\.id), ["new"], "the request itself was not cancelled")
    }
}
