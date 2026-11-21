import Foundation

/// One switch the tray asks the connection screen to make. The screen owns the
/// probe, the teardown and the session; the tray only names the target.
enum ServerSwitchRequest: Equatable {
    /// A saved external host. The screen probes it before it replaces anything.
    case host(Origin)
    /// The bundled engine on this Mac (local mode).
    case thisMac
}

extension Notification.Name {
    /// Posted after a tray row queues its request. The connection screen takes
    /// the queued request from `ServerSwitchQueue`; the notification only wakes it.
    static let ciaobotSwitchServer = Notification.Name("CiaobotSwitchServer")
}

/// Holds a tray request until the connection screen takes it. A window whose
/// view was torn down has no observer when the tray posts, so the notification
/// alone could be lost; the queue keeps the request until a view is present.
@MainActor
final class ServerSwitchQueue {
    static let shared = ServerSwitchQueue()

    private(set) var pending: ServerSwitchRequest?

    init() {}

    /// The latest request wins: a second row click before the first is taken
    /// replaces it rather than running both.
    func enqueue(_ request: ServerSwitchRequest) {
        pending = request
    }

    func take() -> ServerSwitchRequest? {
        defer { pending = nil }
        return pending
    }
}

/// The tray's Servers rows, as pure data. Built from the saved host list and
/// the live connection, so a test can prove the rows without a menu.
enum ServerSwitchChoices {
    /// The This Mac row's title. A saved host with this label is disambiguated.
    static let thisMacLabel = "This Mac"

    enum Row: Equatable {
        case thisMac(checked: Bool)
        case host(origin: Origin, label: String, checked: Bool)
    }

    /// `connected` is the live origin only while the app is connected. In local
    /// mode no saved host is checked: the connected server is This Mac.
    static func rows(
        hosts: [HostRecord],
        connected: Origin?,
        localMode: Bool,
        localAvailable: Bool
    ) -> [Row] {
        var hostRows: [(origin: Origin, label: String, checked: Bool)] = []
        var seen = Set<String>()
        for record in hosts {
            // A saved value that no longer parses as its canonical origin is
            // not a choice, and a duplicate never becomes a second row.
            guard let origin = Origin(record.origin),
                  origin.canonicalKey == record.origin,
                  seen.insert(origin.canonicalKey).inserted else { continue }
            let checked = !localMode && connected?.canonicalKey == origin.canonicalKey
            hostRows.append((
                origin: origin,
                label: MenuTextSanitizer.sanitize(
                    record.label,
                    limit: HostRecord.labelLimit,
                    fallback: HostRecord.defaultLabel(for: origin.canonicalKey)
                ),
                checked: checked
            ))
        }
        // Labels must tell the rows apart. A label that repeats another row's
        // label, or the This Mac row's, gains the origin's host and port, so the
        // row can be told apart and the tooltip names the full origin.
        var counts: [String: Int] = [:]
        if localAvailable { counts[thisMacLabel] = 1 }
        for row in hostRows { counts[row.label, default: 0] += 1 }

        var rows: [Row] = []
        if localAvailable {
            rows.append(.thisMac(checked: localMode))
        }
        for row in hostRows {
            let label = counts[row.label, default: 0] > 1
                ? "\(row.label) (\(HostRecord.defaultLabel(for: row.origin.canonicalKey)))"
                : row.label
            rows.append(.host(origin: row.origin, label: label, checked: row.checked))
        }
        return rows
    }

    /// A section is a choice. With one choice there is nothing to switch to, so
    /// the menu stays exactly as it was.
    static func showsSection(_ rows: [Row]) -> Bool {
        rows.count > 1
    }
}
