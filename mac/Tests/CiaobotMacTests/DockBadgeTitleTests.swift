import XCTest

@testable import CiaobotMac

/// The Dock label is a pure rule, so the same count that drives the menu-bar
/// badge decides the Dock badge and no test needs a real Dock tile.
final class DockBadgeTitleTests: XCTestCase {
    func testUnknownZeroAndNegativeCountsHaveNoLabel() {
        XCTAssertNil(DockBadgeTitle.label(for: nil), "An unknown count must not invent a Dock number.")
        XCTAssertNil(DockBadgeTitle.label(for: 0), "Zero attention clears the Dock badge.")
        XCTAssertNil(DockBadgeTitle.label(for: -4), "A negative count is not a badge.")
    }

    func testAPositiveCountIsExactlyTheDecimalCount() {
        XCTAssertEqual(DockBadgeTitle.label(for: 1), "1")
        XCTAssertEqual(DockBadgeTitle.label(for: 12), "12")
    }
}
