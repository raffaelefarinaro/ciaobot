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
    /// Posted by the tray's server rows with a `ServerSwitchRequest` as its
    /// object. Only the connection screen observes it.
    static let ciaobotSwitchServer = Notification.Name("CiaobotSwitchServer")
}

/// The tray's Servers rows, as pure data. Built from the saved host list and
/// the live connection, so a test can prove the rows without a menu.
enum ServerSwitchChoices {
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
        var rows: [Row] = []
        if localAvailable {
            rows.append(.thisMac(checked: localMode))
        }
        var seen = Set<String>()
        for record in hosts {
            // A saved value that no longer parses as its canonical origin is
            // not a choice, and a duplicate never becomes a second row.
            guard let origin = Origin(record.origin),
                  origin.canonicalKey == record.origin,
                  seen.insert(origin.canonicalKey).inserted else { continue }
            let checked = !localMode && connected?.canonicalKey == origin.canonicalKey
            rows.append(.host(
                origin: origin,
                label: MenuTextSanitizer.sanitize(
                    record.label,
                    limit: HostRecord.labelLimit,
                    fallback: HostRecord.defaultLabel(for: origin.canonicalKey)
                ),
                checked: checked
            ))
        }
        return rows
    }

    /// A section is a choice. With one choice there is nothing to switch to, so
    /// the menu stays exactly as it was.
    static func showsSection(_ rows: [Row]) -> Bool {
        rows.count > 1
    }
}
