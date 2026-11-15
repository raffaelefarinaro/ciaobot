import AppKit

/// The Dock tile's attention label. It is a pure rule, so the menu-bar badge
/// and the Dock badge can only ever state the same fact: an unknown, zero, or
/// negative count means "no badge", and a positive count is exactly the
/// decimal count. Keeping the rule separate also means tests never need a real
/// Dock tile.
enum DockBadgeTitle {
    static func label(for count: Int?) -> String? {
        guard let count, count > 0 else { return nil }
        return String(count)
    }
}

/// The small surface the menu bar needs from the Dock. A double records the
/// label in tests; production writes the app's own tile.
@MainActor
protocol DockBadgeApplying: AnyObject {
    /// A `nil` label clears the badge instead of leaving a stale number.
    func setBadgeLabel(_ label: String?)
}

/// Writes the app's Dock tile badge from the same attention count as the
/// menu-bar item. It never fetches, counts, or schedules anything itself.
@MainActor
final class DockTileBadge: DockBadgeApplying {
    func setBadgeLabel(_ label: String?) {
        NSApp.dockTile.badgeLabel = label
    }
}
