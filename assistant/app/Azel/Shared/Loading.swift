import SwiftUI

/// How one section of a screen did on its last load (user bug, 2026-11-21: pull-to-refresh showed
/// "다시 시도" every time). Each section loads on its own, so one failing endpoint never takes the
/// whole screen down, and cached data stays visible whatever happens.
enum SectionStatus: Equatable {
    case ok
    /// 404: the gateway doesn't have this endpoint yet. A quiet note, not an error.
    case unavailable
    /// Network trouble or a 5xx: worth a "다시 시도".
    case failed(String)

    /// nil for cancellation (`.refreshable` cancels its task when the view updates): not an error,
    /// and the previous status and data stay as they were.
    static func from(_ error: Error) -> SectionStatus? {
        if error is CancellationError { return nil }
        if let u = error as? URLError, u.code == .cancelled { return nil }
        if (error as NSError).domain == NSURLErrorDomain, (error as NSError).code == NSURLErrorCancelled { return nil }
        if case GatewayError.http(404) = error { return .unavailable }
        return .failed(FriendlyError.describe(error).title)
    }

    var isFailed: Bool { if case .failed = self { return true }; return false }
}

/// Runs a request in its own task, so a SwiftUI update cancelling the caller (`.refreshable`)
/// doesn't cancel the network call underneath it.
func detached<T: Sendable>(_ operation: @escaping @Sendable () async throws -> T) async throws -> T {
    try await Task.detached(operation: operation).value
}

/// Inline note for a section: quiet for 404, a retry for real failures, nothing when fine.
struct SectionNote: View {
    let status: SectionStatus
    var retry: (() -> Void)? = nil

    var body: some View {
        switch status {
        case .ok:
            EmptyView()
        case .unavailable:
            Label("이 기능은 서버 업데이트 후 사용할 수 있어요", systemImage: "clock.arrow.circlepath")
                .font(.footnote).foregroundStyle(Theme.text3)
        case .failed(let why):
            HStack(spacing: Theme.s) {
                Label(why, systemImage: "exclamationmark.triangle").font(.footnote).foregroundStyle(Theme.danger)
                if let retry { Button("다시 시도", action: retry).font(.footnote).buttonStyle(.borderless).tint(Theme.celadon) }
            }
        }
    }
}

/// A request as a value: lets a screen start several with `async let` and handle each on its own.
func attempt<T: Sendable>(_ operation: @escaping @Sendable () async throws -> T) async -> Result<T, Error> {
    do { return .success(try await detached(operation)) } catch { return .failure(error) }
}
