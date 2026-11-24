import Combine
import Foundation
import WebKit

/// What the chat feed needs from whatever it is pointed at: one origin and the
/// WebKit store that holds its sign-in, plus a signal for when either changes.
/// The app session is the first implementation; a second host has no session
/// object, so it supplies a fixed pair instead. Nothing here is a sign-in
/// state, a chat count, or a health claim.
@MainActor
protocol ConnectionContext: AnyObject {
    var isConnected: Bool { get }
    var currentOrigin: Origin? { get }
    var dataStore: WKWebsiteDataStore? { get }
    var tlsIdentity: LocalEngineTLSIdentity? { get }
    /// Fires whenever `currentOrigin`/`dataStore` may have changed. Subscribers
    /// must re-read those properties; the signal carries no payload because a
    /// disconnect and a move to another origin look the same here.
    var connectionPublisher: AnyPublisher<Void, Never> { get }
}

extension AppSessionStatus: ConnectionContext {
    /// `$connection` fires from the property's `willSet`, so its `connection`
    /// value is not settled during the callback even though `currentOrigin` and
    /// `dataStore` already are (`reportConnected` writes them first). The chat
    /// models hop to the main actor and only then read `currentOrigin`, which is
    /// the ordering `testSessionAsContextPublishesAfterTheStoredConnection`
    /// pins down.
    var connectionPublisher: AnyPublisher<Void, Never> {
        $connection.map { _ in () }.eraseToAnyPublisher()
    }
}

/// A connection that is fixed for the lifetime of the instance: one origin and
/// its own WebKit store, always connected. The publisher replays once on
/// subscribe and never fires again; the chat feed deduplicates by origin key,
/// so a replayed connection is not a second session.
@MainActor
final class StaticHostContext: ConnectionContext {
    let currentOrigin: Origin?
    let dataStore: WKWebsiteDataStore?
    let tlsIdentity: LocalEngineTLSIdentity? = nil

    var isConnected: Bool { true }

    private let connectionSubject = CurrentValueSubject<Void, Never>(())

    var connectionPublisher: AnyPublisher<Void, Never> { connectionSubject.eraseToAnyPublisher() }

    init(origin: Origin, dataStore: WKWebsiteDataStore) {
        self.currentOrigin = origin
        self.dataStore = dataStore
    }
}

/// One connection screen operation at a time. Connect can be started from the
/// button or from Return, so a boolean is not enough: forget and connect must
/// not overlap, and a late completion from one task must not clear the other.
enum ConnectionTask: Equatable {
    case idle
    case checking
    case forgetting
}

struct ConnectionTaskGate: Equatable {
    private(set) var task: ConnectionTask = .idle

    var isBusy: Bool { task != .idle }

    mutating func beginCheck() -> Bool {
        guard task == .idle else { return false }
        task = .checking
        return true
    }

    mutating func endCheck() {
        if task == .checking { task = .idle }
    }

    mutating func beginForget() -> Bool {
        guard task == .idle else { return false }
        task = .forgetting
        return true
    }

    mutating func endForget() {
        if task == .forgetting { task = .idle }
    }
}

/// The starting position of the **Keep me signed in** toggle. Persistence can
/// only work for an HTTPS origin on a system that can hand out named WebKit
/// stores, and the connection screen already disables the toggle everywhere
/// else. Asking for a click that cannot change the outcome only adds friction,
/// so the toggle starts on exactly where it can be honoured. An explicit click
/// still wins for the address it was made on; switching servers clears that
/// choice, so one host's consent is never inherited by another.
enum PersistentSignInDefault {
    /// `persistentAvailable` means persistent stores are usable for this
    /// connection (supported system and a usable registry). `explicitChoice`
    /// is nil until the user clicks the toggle, so a stored choice can never
    /// turn persistence on where it is impossible.
    static func isEnabled(origin: Origin?, persistentAvailable: Bool, explicitChoice: Bool?) -> Bool {
        guard let origin, origin.usesTLS, persistentAvailable else { return false }
        return explicitChoice ?? true
    }
}

/// Orders local-store erasure after view release and reconciles the currently
/// verified engine generation after erasure finishes.
@MainActor
enum LocalSignInForgetFlow {
    static func perform(
        origin: Origin,
        releaseView: @escaping (@escaping (Bool) -> Void) -> Void,
        eraseStore: @escaping (Origin, SessionStoreKey, @escaping (Result<Void, ForgetFailure>) -> Void) -> Void,
        currentIdentity: @escaping () -> LocalEngineTLSIdentity?,
        reconnect: @escaping (LocalEngineTLSIdentity) -> Void,
        completion: @escaping (Result<Void, ForgetFailure>?) -> Void
    ) {
        releaseView { released in
            guard released else {
                completion(nil)
                return
            }
            eraseStore(origin, .localEngine) { result in
                if let currentIdentity = currentIdentity() { reconnect(currentIdentity) }
                completion(result)
            }
        }
    }
}

/// Copy for the connection screen. Kept out of the SwiftUI view so the words
/// that describe a fail-closed state can be tested.
enum PersistentSignInCopy {
    static func explanation(origin: Origin?, persistentAvailable: Bool, registryAvailable: Bool) -> String {
        guard let origin else {
            return "Persistent sign-in applies only to HTTPS addresses on macOS 14 or later."
        }
        if !origin.usesTLS {
            return "Persistent sign-in is HTTPS-only. This address uses an in-memory store that is discarded when the app quits."
        }
        if !persistentAvailable {
            return "Persistent sign-in requires macOS 14 or later. This session stays in memory."
        }
        if !registryAvailable {
            return "The saved sign-in list on this Mac could not be used, so this session stays in memory. Persistent sign-in stays unavailable until the list is repaired. Do not delete it: it is the only record of the saved sign-ins this app can remove."
        }
        return "Stores this server’s sign-in in its own WebKit store, keyed by scheme, host, and port. Another address gets a separate store, and a saved session is used only when this option is on for the launch."
    }
}

/// Evidence that the connection's WKWebView is gone before its store is
/// erased. WebKit refuses to remove a store that a view still uses, so forget
/// must not claim the view was released unless the weak reference is actually
/// nil. The tracker holds the view weakly and polls the main queue.
@MainActor
final class SessionViewTracker {
    static let pollInterval: TimeInterval = 0.01

    private weak var view: AnyObject?

    var isReleased: Bool { view == nil }

    func attach(_ view: AnyObject) {
        self.view = view
    }

    func whenReleased(timeout: TimeInterval, completion: @escaping (Bool) -> Void) {
        poll(deadline: Date().addingTimeInterval(timeout), completion: completion)
    }

    private func poll(deadline: Date, completion: @escaping (Bool) -> Void) {
        if view == nil {
            completion(true)
            return
        }
        if Date() >= deadline {
            completion(false)
            return
        }
        Task { @MainActor [self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.pollInterval * 1_000_000_000))
            self.poll(deadline: deadline, completion: completion)
        }
    }
}
