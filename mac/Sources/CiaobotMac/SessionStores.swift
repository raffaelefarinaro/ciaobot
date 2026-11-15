import Foundation
import Darwin
import WebKit

/// How one connection's cookies are kept for the lifetime of the app.
enum SessionStorage: Equatable {
    /// A fresh in-memory store. Nothing is written to disk.
    case ephemeral
    /// A named WebKit store that survives relaunch.
    case persistent(UUID)
}

/// Why an explicitly requested persistent sign-in became ephemeral. The UI
/// shows this instead of silently ignoring the opt-in.
enum SessionStorageFallback: Equatable {
    case insecureOrigin
    case unsupportedSystem
    case registryUnavailable
    /// Another process changed the mapping file after this app read it. The
    /// app fails closed rather than overwrite the other process's mappings.
    case registryChangedElsewhere
    /// WebKit reported the store removed but the mapping write failed. The
    /// identifier is retired for this process so the removed store is never
    /// reopened here (retirement does not survive a restart).
    case erasedRecordNotUpdated

    /// Shown where the web view is, because the connection screen is gone by
    /// the time a fallback can be detected. An opt-in must never be silently
    /// downgraded to an in-memory session.
    var userMessage: String {
        switch self {
        case .insecureOrigin:
            return "This address is not HTTPS, so the session was not saved. It stays in memory for this launch."
        case .unsupportedSystem:
            return "Persistent sign-in needs macOS 14 or later, so the session was not saved. It stays in memory for this launch."
        case .registryUnavailable:
            return "The saved sign-in list could not be used, so the session was not saved. Persistent sign-in stays disabled until the list is repaired. Do not delete it: it is the only record of the saved sessions this app can remove."
        case .registryChangedElsewhere:
            return "Another copy of the app changed the saved sign-in list, so this session stays in memory. Restart Ciaobot to reload the list."
        case .erasedRecordNotUpdated:
            return "WebKit reported the saved sign-in store removed, but its record could not be updated, so this session stays in memory. Use Forget saved sign-in to finish, or restart."
        }
    }
}

enum ForgetFailure: Error, Equatable {
    /// The mapping file is unreadable, so no store can be identified safely.
    case registryUnavailable
    /// Another instance changed the mapping file after this one loaded it, so
    /// the cached identifier must not be used.
    case registryChangedElsewhere
    /// WebKit still has the store (removal failed, or it survived verification).
    case storeStillPresent
    /// WebKit reported the store removed but the mapping could not be written;
    /// the mapping is retained so the user can retry, and the erased identifier
    /// is retired so this process cannot reopen it.
    case mappingNotSaved
}

enum SessionStoreRegistryError: Error, Equatable {
    case unavailable
    case writeFailed
    /// The file on disk no longer matches the snapshot this app loaded. A
    /// write would silently drop the other writer's mappings, so it is refused.
    case concurrentModification
}

/// The opt-in persistence switch. Persistent sign-in needs the user's explicit
/// opt-in, an HTTPS origin, a system with named WebKit data stores (macOS 14+),
/// and a usable mapping file; failing any of them keeps the session in memory.
enum SessionStoragePolicy {
    static var systemSupportsPersistentStores: Bool {
        if #available(macOS 14.0, *) { return true }
        return false
    }
}

/// Maps canonical origins to unique WebKit store identifiers and persists only
/// that mapping. Cookie values are never read, copied, or logged here.
///
/// The file is private (`0600`) inside a private (`0700`) directory. A corrupt
/// or ambiguous file fails closed: no persistent store is opened, so two
/// origins can never end up sharing one cookie jar.
final class SessionStoreRegistry {
    private struct Snapshot: Codable {
        let version: Int
        let stores: [Entry]
    }

    private struct Entry: Codable {
        let origin: String
        let identifier: UUID
    }

    private static let zeroIdentifier = UUID(uuidString: "00000000-0000-0000-0000-000000000000")!

    private(set) var isAvailable: Bool
    /// Set when `isAvailable` becomes false, for the UI to explain the cause.
    /// A concurrent writer is not a corrupt file, so it gets its own message.
    private(set) var storageUnavailableReason: SessionStoreRegistryError?
    private var identifiers: [String: UUID] = [:]
    /// The exact bytes this registry last read or wrote. Before every write the
    /// file is re-read; a difference means another process changed it, and the
    /// write is refused instead of silently dropping that process's mappings.
    private var lastKnownData: Data?
    let fileURL: URL?
    private let fileManager: FileManager

    init(fileURL: URL?, fileManager: FileManager = .default) {
        self.fileURL = fileURL
        self.fileManager = fileManager

        guard let fileURL else {
            isAvailable = false
            storageUnavailableReason = .unavailable
            return
        }
        guard fileManager.fileExists(atPath: fileURL.path) else {
            isAvailable = true
            return
        }
        guard let data = try? Data(contentsOf: fileURL),
              let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data),
              snapshot.version == 1 else {
            isAvailable = false
            storageUnavailableReason = .unavailable
            return
        }

        var values: [String: UUID] = [:]
        var seen = Set<UUID>()
        for entry in snapshot.stores {
            // The registry is the only authority on which identifier belongs
            // to which origin, so an entry that the app would never write is
            // treated as tampering and disables every persistent store.
            guard let origin = Origin(entry.origin),
                  origin.usesTLS,
                  origin.canonicalKey == entry.origin,
                  entry.identifier != Self.zeroIdentifier,
                  seen.insert(entry.identifier).inserted,
                  values[entry.origin] == nil else {
                isAvailable = false
                storageUnavailableReason = .unavailable
                return
            }
            values[entry.origin] = entry.identifier
        }
        identifiers = values
        lastKnownData = data
        isAvailable = true
    }

    var mappedOriginCount: Int { identifiers.count }

    func contains(_ origin: Origin) -> Bool {
        identifiers[origin.canonicalKey] != nil
    }

    /// Cached lookup for UI state and for callers that have already verified
    /// the snapshot (forget does). Never use this to open a store: another
    /// instance may have removed this mapping. Use `identifier(for:)`.
    func identifierIfPresent(for origin: Origin) -> UUID? {
        identifiers[origin.canonicalKey]
    }

    /// The only lookup safe for opening a saved store without the caller
    /// intending to allocate one: it verifies the on-disk snapshot first and
    /// never writes. A new mapping is created only by `identifier(for:)`, which
    /// is reached from an explicit sign-in opt-in.
    func verifiedIdentifierIfPresent(for origin: Origin) -> UUID? {
        guard isAvailable, verifySnapshot() else { return nil }
        return identifiers[origin.canonicalKey]
    }

    /// True while the file on disk still matches the snapshot this instance
    /// last read or wrote. A stale snapshot is failed closed, never repaired in
    /// this process: another instance may have removed the mapping, or removed
    /// and re-created it with a new identifier.
    func verifySnapshot() -> Bool {
        guard isAvailable, let fileURL else { return false }
        let current: Data?
        if fileManager.fileExists(atPath: fileURL.path) {
            guard let data = try? Data(contentsOf: fileURL) else {
                failClosed(.unavailable)
                return false
            }
            current = data
        } else {
            current = nil
        }
        guard current == lastKnownData else {
            failClosed(.concurrentModification)
            return false
        }
        return true
    }

    private func failClosed(_ reason: SessionStoreRegistryError) {
        isAvailable = false
        storageUnavailableReason = reason
    }

    /// Returns the stable identifier for this canonical origin, allocating one
    /// on first use. A write failure disables persistence rather than handing
    /// out an identifier that would change on the next launch.
    ///
    /// Every call verifies the on-disk snapshot first, including a cached hit:
    /// another instance may have removed this origin's mapping (and store), in
    /// which case returning the cached id would reopen an orphaned store.
    func identifier(for origin: Origin) -> UUID? {
        guard isAvailable, fileURL != nil, verifySnapshot() else { return nil }
        let key = origin.canonicalKey
        if let existing = identifiers[key] { return existing }

        var candidate = UUID()
        while identifiers.values.contains(candidate) { candidate = UUID() }

        var next = identifiers
        next[key] = candidate
        do {
            try save(next)
        } catch {
            isAvailable = false
            storageUnavailableReason = (error as? SessionStoreRegistryError) ?? .writeFailed
            return nil
        }
        identifiers = next
        return candidate
    }

    /// Drops the mapping. The caller must remove the WebKit store first; a
    /// failed write leaves the in-memory mapping intact so forget can retry.
    func remove(_ origin: Origin) throws {
        guard isAvailable else { throw SessionStoreRegistryError.unavailable }
        let key = origin.canonicalKey
        guard identifiers[key] != nil else { return }

        var next = identifiers
        next.removeValue(forKey: key)
        do {
            try save(next)
        } catch {
            throw (error as? SessionStoreRegistryError) ?? .writeFailed
        }
        identifiers = next
    }

    private func save(_ values: [String: UUID]) throws {
        guard let fileURL else { throw SessionStoreRegistryError.unavailable }
        let directory = fileURL.deletingLastPathComponent()
        do {
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
            try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)

            // Advisory cross-process mutex, so two instances cannot both pass
            // the snapshot comparison below and overwrite each other. The lock
            // is only a mutex; the content comparison is the authority, and a
            // writer that ignores the lock is still caught.
            let lockFD = open(directory.appendingPathComponent("session-stores.lock").path,
                              O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW,
                              0o600)
            guard lockFD >= 0 else { throw SessionStoreRegistryError.writeFailed }
            defer { close(lockFD) }
            // Non-blocking: the UI runs on the main actor, so waiting on
            // another instance's lock would freeze the app. A held lock means
            // another writer is active, so fail closed instead.
            guard flock(lockFD, LOCK_EX | LOCK_NB) == 0 else {
                throw SessionStoreRegistryError.concurrentModification
            }

            // Cross-process guard: never replace a snapshot this process did
            // not see. Re-reading the exact bytes catches another instance's
            // atomic write.
            let current: Data?
            if fileManager.fileExists(atPath: fileURL.path) {
                guard let data = try? Data(contentsOf: fileURL) else {
                    throw SessionStoreRegistryError.unavailable
                }
                current = data
            } else {
                current = nil
            }
            guard current == lastKnownData else {
                throw SessionStoreRegistryError.concurrentModification
            }

            let entries = values
                .map { Entry(origin: $0.key, identifier: $0.value) }
                .sorted { $0.origin < $1.origin }
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            let data = try encoder.encode(Snapshot(version: 1, stores: entries))
            try data.write(to: fileURL, options: [.atomic])
            try fileManager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: fileURL.path)
            lastKnownData = data
        } catch let error as SessionStoreRegistryError {
            throw error
        } catch {
            throw SessionStoreRegistryError.writeFailed
        }
    }

    /// Default mapping location. The containing directory is created only on
    /// the first opt-in, so an ordinary launch writes nothing.
    static var defaultFileURL: URL? {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first?
            .appendingPathComponent("CiaobotMac", isDirectory: true)
            .appendingPathComponent("session-stores.json")
    }
}

/// The bounded verification decision after `WKWebsiteDataStore.remove`. Kept
/// pure so the "success only when WebKit no longer lists the store" rule is
/// testable without waiting on WebKit.
enum StoreRemovalVerification: Equatable {
    case removed
    case retry
    case stillPresent

    static func next(identifiers: [UUID], identifier: UUID, attemptsLeft: Int) -> StoreRemovalVerification {
        if !identifiers.contains(identifier) { return .removed }
        return attemptsLeft > 0 ? .retry : .stillPresent
    }
}

/// Removes a named WebKit store. Split from the mapping so the forget ordering
/// (release web views, remove data, then update the mapping) is testable.
@MainActor
protocol WebsiteDataStoreErasing {
    func remove(identifier: UUID, completion: @escaping (Error?) -> Void)
}

/// Real eraser. It verifies that the identifier is gone after removal, so a
/// silent WebKit failure cannot be reported as success. WebKit's list of store
/// identifiers updates asynchronously after a removal, so the verification is
/// polled for a short bounded time; success is still only reported once the
/// identifier is actually absent.
@MainActor
final class WebKitWebsiteDataStoreEraser: WebsiteDataStoreErasing {
    private enum RemovalError: Error {
        case stillPresent
    }

    static let removalAttempts = 3
    static let removalRetryInterval: TimeInterval = 0.1
    static let verificationAttempts = 10
    static let verificationInterval: TimeInterval = 0.1

    func remove(identifier: UUID, completion: @escaping (Error?) -> Void) {
        guard #available(macOS 14.0, *) else {
            // No named store can exist on this system.
            completion(nil)
            return
        }
        WKWebsiteDataStore.fetchAllDataStoreIdentifiers { identifiers in
            guard identifiers.contains(identifier) else {
                completion(nil)
                return
            }
            self.attemptRemoval(identifier, attemptsLeft: Self.removalAttempts, completion: completion)
        }
    }

    /// WebKit can refuse a removal while a store is still cached or in use, and
    /// that state can clear a moment later. A short bounded retry avoids a
    /// false "still present" without ever reporting success until the store is
    /// gone. A view that keeps the store alive still fails after the retries.
    @available(macOS 14.0, *)
    private func attemptRemoval(_ identifier: UUID, attemptsLeft: Int, completion: @escaping (Error?) -> Void) {
        WKWebsiteDataStore.remove(forIdentifier: identifier) { error in
            WKWebsiteDataStore.fetchAllDataStoreIdentifiers { remaining in
                if !remaining.contains(identifier) {
                    // Removed, even if WebKit reported an error for the call.
                    completion(nil)
                    return
                }
                if error == nil {
                    self.verifyRemoved(identifier, attemptsLeft: Self.verificationAttempts, completion: completion)
                    return
                }
                guard attemptsLeft > 1 else {
                    completion(error)
                    return
                }
                Task { @MainActor [self] in
                    try? await Task.sleep(nanoseconds: UInt64(Self.removalRetryInterval * 1_000_000_000))
                    self.attemptRemoval(identifier, attemptsLeft: attemptsLeft - 1, completion: completion)
                }
            }
        }
    }

    @available(macOS 14.0, *)
    private func verifyRemoved(_ identifier: UUID, attemptsLeft: Int, completion: @escaping (Error?) -> Void) {
        WKWebsiteDataStore.fetchAllDataStoreIdentifiers { remaining in
            switch StoreRemovalVerification.next(identifiers: remaining, identifier: identifier, attemptsLeft: attemptsLeft) {
            case .removed:
                completion(nil)
            case .stillPresent:
                completion(RemovalError.stillPresent)
            case .retry:
                Task { @MainActor [self] in
                    try? await Task.sleep(nanoseconds: UInt64(Self.verificationInterval * 1_000_000_000))
                    self.verifyRemoved(identifier, attemptsLeft: attemptsLeft - 1, completion: completion)
                }
            }
        }
    }
}

/// Resolves the WebKit data store for a connection. The default store is never
/// used: every connection is either its own ephemeral store or its own named
/// store, so no store is shared between origins. WebKit cookies are still
/// host-scoped inside that store, so a page can send them to a subresource on
/// another port of the same host.
@MainActor
enum WebKitSessionStores {
    static func make(_ storage: SessionStorage) -> WKWebsiteDataStore {
        switch storage {
        case .ephemeral:
            return .nonPersistent()
        case .persistent(let identifier):
            if #available(macOS 14.0, *) {
                return WKWebsiteDataStore(forIdentifier: identifier)
            }
            // Unreachable while the policy gates persistence on macOS 14, but
            // fails closed rather than falling back to a shared store.
            return .nonPersistent()
        }
    }
}

/// Decides whether a connection gets a persistent store and performs the
/// explicit forget flow. Persistent sign-in never connects on its own: this
/// type only answers store questions after the user has entered an address and
/// chosen to keep the session.
@MainActor
final class SessionSignIn {
    let registry: SessionStoreRegistry
    private let eraser: WebsiteDataStoreErasing
    let persistentAvailable: Bool
    /// Identifiers whose WebKit store was reported removed but whose mapping
    /// write failed. They are never handed out again in this process: WebKit
    /// can still serve a removed store's cached cookies, and a fresh instance
    /// could resurrect them. A retry that completes the mapping write, or a
    /// restart (which forgets this in-memory set), clears it.
    private(set) var retiredIdentifiers: Set<UUID> = []

    init(registry: SessionStoreRegistry, eraser: WebsiteDataStoreErasing, persistentAvailable: Bool) {
        self.registry = registry
        self.eraser = eraser
        self.persistentAvailable = persistentAvailable
    }

    static let live = SessionSignIn(
        registry: SessionStoreRegistry(fileURL: SessionStoreRegistry.defaultFileURL),
        eraser: WebKitWebsiteDataStoreEraser(),
        persistentAvailable: SessionStoragePolicy.systemSupportsPersistentStores
    )

    func storage(for origin: Origin, keepSignedIn: Bool) -> SessionStorage {
        guard keepSignedIn else { return .ephemeral }
        guard origin.usesTLS else { return .ephemeral }
        guard canPersistSignIn else { return .ephemeral }
        guard let identifier = registry.identifier(for: origin) else { return .ephemeral }
        guard !retiredIdentifiers.contains(identifier) else { return .ephemeral }
        return .persistent(identifier)
    }

    /// Whether the UI may offer the opt-in at all. False means the choice is
    /// unavailable, not that it will be silently ignored.
    var canPersistSignIn: Bool { persistentAvailable && registry.isAvailable }

    /// Non-nil only when the user opted in but did not get a persistent store.
    func fallback(for origin: Origin, keepSignedIn: Bool) -> SessionStorageFallback? {
        guard keepSignedIn else { return nil }
        guard origin.usesTLS else { return .insecureOrigin }
        guard persistentAvailable else { return .unsupportedSystem }
        if let identifier = registry.identifierIfPresent(for: origin), retiredIdentifiers.contains(identifier) {
            return .erasedRecordNotUpdated
        }
        guard registry.isAvailable else {
            return registry.storageUnavailableReason == .concurrentModification
                ? .registryChangedElsewhere
                : .registryUnavailable
        }
        return nil
    }

    func hasSavedSignIn(for origin: Origin) -> Bool {
        registry.identifierIfPresent(for: origin) != nil
    }

    /// Forget is explicit and three-phase. The caller first proves the web
    /// views that used the store are gone (WebKit refuses to remove a store
    /// still in use), then the store is deleted and its absence verified, and
    /// only then is the mapping dropped. Any failure keeps the mapping so the
    /// operation can be retried; a completed erase with a failed mapping write
    /// also retires the identifier so it cannot be reopened in this process.
    func forget(origin: Origin,
                releaseWebViews: @escaping (@escaping () -> Void) -> Void,
                completion: @escaping (Result<Void, ForgetFailure>) -> Void) {
        guard registry.isAvailable else {
            completion(.failure(.registryUnavailable))
            return
        }
        guard registry.verifySnapshot() else {
            completion(.failure(registry.storageUnavailableReason == .concurrentModification
                ? .registryChangedElsewhere
                : .registryUnavailable))
            return
        }
        guard let identifier = registry.identifierIfPresent(for: origin) else {
            completion(.success(()))
            return
        }
        releaseWebViews { [self] in
            self.eraser.remove(identifier: identifier) { error in
                guard error == nil else {
                    completion(.failure(.storeStillPresent))
                    return
                }
                do {
                    try self.registry.remove(origin)
                    self.retiredIdentifiers.remove(identifier)
                    completion(.success(()))
                } catch {
                    // WebKit reported the store removed but the record still
                    // names it. Keep the record so forget can be retried, and
                    // retire the id so this process never reopens it.
                    self.retiredIdentifiers.insert(identifier)
                    completion(.failure(.mappingNotSaved))
                }
            }
        }
    }
}
