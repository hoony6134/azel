import os

/// Unified logging: `log show --predicate 'subsystem == "xyz.scian.biseo"'`. Never logs the token.
enum Log {
    static let call = Logger(subsystem: "xyz.scian.biseo", category: "call")
}
