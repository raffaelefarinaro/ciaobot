import XCTest

/// A test's defaults suite must leave nothing in `~/Library/Preferences`.
final class IsolatedDefaultsTests: XCTestCase {
    func testRemoveLeavesNoPlistBehind() throws {
        let name = IsolatedDefaults.makeName("IsolatedDefaults")
        let defaults = try XCTUnwrap(UserDefaults(suiteName: name))
        defaults.set("value", forKey: "key")
        XCTAssertTrue(CFPreferencesAppSynchronize(name as CFString))
        XCTAssertTrue(FileManager.default.fileExists(atPath: IsolatedDefaults.plistURL(for: name).path))

        IsolatedDefaults.remove(name)

        XCTAssertFalse(FileManager.default.fileExists(atPath: IsolatedDefaults.plistURL(for: name).path))
    }
}

final class IsolatedDefaultsSweepTests: XCTestCase {
    func testSweepDeletesOnlyStaleTestPlists() throws {
        let fm = FileManager.default
        let stale = IsolatedDefaults.plistURL(for: IsolatedDefaults.makeName("SweepStale"))
        let fresh = IsolatedDefaults.plistURL(for: IsolatedDefaults.makeName("SweepFresh"))
        let emptyPlist = try PropertyListSerialization.data(fromPropertyList: [String: Any](), format: .binary, options: 0)
        try emptyPlist.write(to: stale)
        try emptyPlist.write(to: fresh)
        defer { try? fm.removeItem(at: stale); try? fm.removeItem(at: fresh) }
        try fm.setAttributes([.modificationDate: Date(timeIntervalSinceNow: -3600)], ofItemAtPath: stale.path)

        IsolatedDefaults.sweepStale()

        XCTAssertFalse(fm.fileExists(atPath: stale.path))
        XCTAssertTrue(fm.fileExists(atPath: fresh.path))
    }
}
