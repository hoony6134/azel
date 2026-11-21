import Foundation

/// The 기록 list (DESIGN §15): server pages, offline cache, search, undoable deletes. Shared by
/// home, the history tab/window and the Mac popover.
@MainActor
final class HistoryModel: ObservableObject {
    @Published private(set) var pager = HistoryPager()
    @Published private(set) var loading = false
    @Published private(set) var loadedOnce = false
    /// Set when the list on screen is the cache because the server didn't answer.
    @Published private(set) var offlineSince: Date?
    @Published private(set) var error: Error?
    @Published var query = "" { didSet { if query != oldValue { scheduleSearch() } } }
    @Published private(set) var results: [HistoryItem]?
    @Published private(set) var searching = false
    @Published private(set) var offlineSearch = false
    /// Row removed from view, DELETE sent when the 4 s undo toast expires.
    @Published private(set) var pendingDelete: HistoryItem?
    @Published private(set) var failedDelete: String?

    let cache: HistoryCache
    private var searchTask: Task<Void, Never>?
    private var deleteTask: Task<Void, Never>?
    private var deleteAlsoRecording = false

    init(cache: HistoryCache = .standard) {
        self.cache = cache
        if let snap = cache.loadList() { pager.reset(with: snap.items, pageSize: 0) }
        // The server writes the record when a call ends; give it a moment, then reload.
        NotificationCenter.default.addObserver(forName: callEndedNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in try? await Task.sleep(nanoseconds: 2_000_000_000); await self?.refresh() }
        }
    }

    /// What the list shows: search results or pages, minus a row waiting for its DELETE.
    var items: [HistoryItem] { (results ?? pager.items).filter { $0.id != pendingDelete?.id } }
    var recent: [HistoryItem] { Array(pager.items.filter { $0.id != pendingDelete?.id }.prefix(3)) }
    var isOffline: Bool { offlineSince != nil }

    func refresh() async {
        guard Config.token != nil else { return }
        loading = true
        defer { loading = false; loadedOnce = true }
        do {
            let first = try await HistoryAPI.page()
            pager.reset(with: first)
            cache.saveList(pager.items)
            offlineSince = nil
            error = nil
        } catch {
            self.error = error
            if let snap = cache.loadList() {
                pager.reset(with: snap.items, pageSize: 0)
                offlineSince = snap.fetchedAt
            }
        }
    }

    /// Called when the last section appears.
    func loadMore() async {
        guard !loading, !pager.exhausted, !isOffline, results == nil, let before = pager.nextBefore else { return }
        loading = true
        defer { loading = false }
        if let page = try? await HistoryAPI.page(before: before) {
            pager.append(page)
            cache.saveList(pager.items)
        }
    }

    /// 300 ms debounce; offline → cached titles only.
    private func scheduleSearch() {
        searchTask?.cancel()
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { results = nil; offlineSearch = false; searching = false; return }
        searchTask = Task {
            try? await Task.sleep(nanoseconds: 300_000_000)
            guard !Task.isCancelled else { return }
            searching = true
            defer { searching = false }
            do {
                let hits = try await HistoryAPI.page(query: q, limit: 50)
                guard !Task.isCancelled else { return }
                results = hits
                offlineSearch = false
            } catch {
                guard !Task.isCancelled else { return }
                results = HistoryCache.filter(pager.items, query: q)
                offlineSearch = true
            }
        }
    }

    // MARK: Delete with undo

    func delete(_ item: HistoryItem, withRecording: Bool = false) {
        commitPendingDelete() // only one undo at a time
        failedDelete = nil
        pendingDelete = item
        deleteAlsoRecording = withRecording
        deleteTask = Task {
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            guard !Task.isCancelled else { return }
            commitPendingDelete()
        }
    }

    func undoDelete() {
        deleteTask?.cancel()
        pendingDelete = nil
    }

    /// Sends the DELETE now (toast expired, or the user left the screen).
    func commitPendingDelete() {
        deleteTask?.cancel()
        guard let item = pendingDelete else { return }
        let withRecording = deleteAlsoRecording
        Task {
            do {
                try await HistoryAPI.delete(item.id)
                pager.remove(item.id)
                results?.removeAll { $0.id == item.id }
                cache.remove(item.id)
                if withRecording { Recordings.matching(item).forEach(Recordings.delete) }
            } catch GatewayError.http(404) {
                pager.remove(item.id)          // already gone elsewhere
                cache.remove(item.id)
            } catch {
                failedDelete = item.id         // the row comes back with a retry note
            }
            if pendingDelete?.id == item.id { pendingDelete = nil }
        }
    }

    /// Detail found the call deleted elsewhere (404).
    func forget(_ id: String) {
        pager.remove(id)
        results?.removeAll { $0.id == id }
        cache.remove(id)
    }

    /// Retention shortened: apply now, then reload.
    func deleteOlder(than days: Int) async {
        _ = try? await Gateway.send("DELETE", "/v1/history?older_than_days=\(days)")
        await refresh()
    }

    /// For the retention confirmation: cached calls older than `days`.
    func count(olderThan days: Int, now: Date = Date()) -> Int {
        let cutoff = now.addingTimeInterval(-Double(days) * 86_400).timeIntervalSince1970
        return pager.items.filter { $0.started_at < cutoff }.count
    }
}

/// One opened call: server first, cached transcript when offline, "deleted" on 404.
@MainActor
final class HistoryDetailModel: ObservableObject {
    enum State: Equatable { case loading, loaded, offlineCached(Date?), unavailable, deleted }

    @Published private(set) var call: HistoryCall?
    @Published private(set) var state: State = .loading
    let item: HistoryItem
    private let cache: HistoryCache

    init(item: HistoryItem, cache: HistoryCache = .standard) {
        self.item = item
        self.cache = cache
        call = cache.loadCall(item.id)
    }

    func load() async {
        do {
            let fresh = try await HistoryAPI.call(item.id)
            call = fresh
            cache.saveCall(fresh)
            state = .loaded
        } catch GatewayError.http(404) {
            state = .deleted
        } catch {
            if let cached = cache.loadCall(item.id) { call = cached; state = .offlineCached(cache.loadList()?.fetchedAt) }
            else { state = .unavailable }
        }
    }
}
