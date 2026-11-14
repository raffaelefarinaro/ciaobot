import Foundation

/// A uniquely named `UserDefaults` suite for one test, and its full removal.
///
/// `removePersistentDomain(forName:)` empties a suite but leaves its plist in
/// `~/Library/Preferences` as an empty `{}` file, and cfprefsd writes that file
/// again when the test process exits, for every suite the process touched.
/// With a fresh UUID per test (needed so parallel runs in other worktrees
/// never share a suite), every run added files until thousands of empty
/// `CiaobotMacTests.*.plist` domains piled up.
///
/// `remove` deletes what it can during the run, and the first `makeName` of a
/// run sweeps test plists older than `staleAge`, which earlier runs left at
/// exit. What a run leaves behind is therefore cleared by the next one.
enum IsolatedDefaults {
    static let prefix = "CiaobotMacTests."
    static let staleAge: TimeInterval = 10 * 60

    private static let sweepOnce: Void = sweepStale()

    static func makeName(_ label: String) -> String {
        _ = sweepOnce
        return "\(prefix)\(label).\(UUID().uuidString)"
    }

    /// Flushes and empties the suite, then deletes its plist.
    static func remove(_ name: String) {
        CFPreferencesAppSynchronize(name as CFString)
        UserDefaults(suiteName: name)?.removePersistentDomain(forName: name)
        CFPreferencesAppSynchronize(name as CFString)
        try? FileManager.default.removeItem(at: plistURL(for: name))
    }

    static func plistURL(for name: String) -> URL {
        preferencesDirectory.appendingPathComponent("\(name).plist")
    }

    private static var preferencesDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Preferences", isDirectory: true)
    }

    /// Deletes test plists last modified more than `staleAge` ago.
    static func sweepStale(now: Date = Date()) {
        let fm = FileManager.default
        guard let entries = try? fm.contentsOfDirectory(
            at: preferencesDirectory,
            includingPropertiesForKeys: [.contentModificationDateKey]
        ) else { return }
        for url in entries
        where url.lastPathComponent.hasPrefix(prefix) && url.pathExtension == "plist" {
            let modified = (try? url.resourceValues(forKeys: [.contentModificationDateKey]))?
                .contentModificationDate ?? now
            if now.timeIntervalSince(modified) > staleAge {
                try? fm.removeItem(at: url)
            }
        }
    }
}
