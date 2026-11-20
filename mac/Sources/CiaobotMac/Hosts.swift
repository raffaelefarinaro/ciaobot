import Foundation

/// One saved host. `origin` is the canonical origin string and is re-validated
/// by `Origin` on every read, so a path, query, credential, or a value written
/// by an older build can never become a saved host. `label` is display text
/// only; it is sanitised before it is stored and before it is shown.
///
/// The Codable keys are an on-disk contract (`CiaobotHosts` in UserDefaults),
/// like `session-stores.json`: rename them only with a migration.
struct HostRecord: Equatable, Codable {
    let origin: String
    var label: String

    static let labelLimit = 48

    /// The default label names the host and, when it is not the scheme's
    /// default, the effective port (`example.com:8443`).
    static func defaultLabel(for origin: String) -> String {
        guard let components = URLComponents(string: origin), let host = components.host else {
            return origin
        }
        let isSecure = components.scheme?.lowercased() == "https"
        let defaultPort = isSecure ? 443 : 80
        let port = components.port ?? defaultPort
        let displayHost = host.contains(":") ? "[\(host)]" : host
        return port == defaultPort ? displayHost : "\(displayHost):\(port)"
    }

    /// Labels are untrusted text in the same way a chat title is: line breaks,
    /// control/format characters, and bidi overrides are removed so a label
    /// cannot forge or reorder another row, and the result is bounded.
    static func sanitizedLabel(_ raw: String, origin: String) -> String {
        MenuTextSanitizer.sanitize(
            raw,
            limit: labelLimit,
            fallback: defaultLabel(for: origin)
        )
    }
}

/// The durable list of hosts this app has reached. A saved entry is trusted no
/// further than its canonical origin: an entry that no longer parses as one is
/// dropped on read, and a list that never existed migrates the single last-used
/// origin so an upgraded user never loses their host. Reads never write, so a
/// launch that only shows the connection screen leaves the defaults untouched.
@MainActor
protocol HostListStoring: AnyObject {
    var hosts: [HostRecord] { get }
    func add(_ origin: Origin)
    /// Removes the host, and clears the launch target when it was the active
    /// host, so a later launch cannot reconnect to a host that is not saved.
    func remove(_ origin: Origin)
    func rename(_ origin: Origin, label: String)
}

@MainActor
final class UserDefaultsHostListStore: HostListStoring {
    nonisolated static let defaultKey = "CiaobotHosts"

    private let defaults: UserDefaults
    private let key: String
    private let lastServer: LastServerStoring

    // Non-isolated so the store can be composed as a default argument of a
    // main-actor view; it only assigns its own storage.
    nonisolated init(
        defaults: UserDefaults = .standard,
        key: String = UserDefaultsHostListStore.defaultKey,
        lastServer: LastServerStoring
    ) {
        self.defaults = defaults
        self.key = key
        self.lastServer = lastServer
    }

    var hosts: [HostRecord] {
        guard let stored = storedRecords() else {
            // Read-through migration: with no saved list the only host this app
            // knows is the last origin that answered. Nothing is written here;
            // the first real mutation materialises the list.
            guard let origin = lastServer.savedOrigin else { return [] }
            return [HostRecord(
                origin: origin.canonicalKey,
                label: HostRecord.defaultLabel(for: origin.canonicalKey)
            )]
        }

        var seen = Set<String>()
        var records: [HostRecord] = []
        for record in stored {
            // A value this parser rejects, or one this app would never write
            // (a non-canonical spelling), is not a saved host. Drop it rather
            // than repair or keep it.
            guard let origin = Origin(record.origin),
                  origin.canonicalKey == record.origin,
                  seen.insert(origin.canonicalKey).inserted else { continue }
            records.append(HostRecord(
                origin: origin.canonicalKey,
                label: HostRecord.sanitizedLabel(record.label, origin: origin.canonicalKey)
            ))
        }
        return records
    }

    func add(_ origin: Origin) {
        var records = hosts
        guard !records.contains(where: { $0.origin == origin.canonicalKey }) else { return }
        records.append(HostRecord(
            origin: origin.canonicalKey,
            label: HostRecord.defaultLabel(for: origin.canonicalKey)
        ))
        save(records)
    }

    func remove(_ origin: Origin) {
        let key = origin.canonicalKey
        var records = hosts
        let countBefore = records.count
        records.removeAll { $0.origin == key }
        if records.count != countBefore {
            save(records)
        }
        if lastServer.savedOrigin?.canonicalKey == key {
            lastServer.clear()
        }
    }

    func rename(_ origin: Origin, label: String) {
        let key = origin.canonicalKey
        var records = hosts
        guard let index = records.firstIndex(where: { $0.origin == key }) else { return }
        let sanitized = HostRecord.sanitizedLabel(label, origin: key)
        guard records[index].label != sanitized else { return }
        records[index].label = sanitized
        save(records)
    }

    /// A missing value is "no list yet" (migration). A value that is not a
    /// decodable list is corrupt, so it reads as an empty list and never
    /// resurrects the last-used origin or invents a host. Either way nothing is
    /// written on read.
    private func storedRecords() -> [HostRecord]? {
        guard let data = defaults.data(forKey: key) else { return nil }
        return (try? JSONDecoder().decode([HostRecord].self, from: data)) ?? []
    }

    private func save(_ records: [HostRecord]) {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        guard let data = try? encoder.encode(records) else { return }
        defaults.set(data, forKey: key)
    }
}
