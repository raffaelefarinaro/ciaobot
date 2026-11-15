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

/// When the connection screen offers the native install. Nothing is offered
/// while a Ciaobot is found or an install is running; a failed install can be
/// retried. A failed repair offers install only when the engine said nothing
/// is installed, because any other repair failure leaves an installation that
/// install must not be offered over.
enum NativeInstallOffer {
    /// Idle → offered only when discovery is `.notDetected`. Failed → offered (retry), except that a failed
    /// repair offers install only when the engine reported `not_installed`. Any other state → not offered.
    static func isOffered(discovery: LocalServerDiscovery.Status, install: NativeInstallState,
                          mode: NativeInstallMode = .install) -> Bool {
        switch install {
        case .idle: return discovery == .notDetected
        case .failed(let failure):
            return mode == .install || failure == .installer(.notInstalled)
        case .downloading, .verifying, .running, .wizard, .cancelling, .succeeded: return false
        }
    }
}

/// When the connection screen offers native repair: an installation was detected and it is not
/// answering, did not start, or its engine update failed. Every engine update targets the local
/// engine (see `EngineUpdateOffer.target`), so a failed update is enough. Nothing is offered while
/// a run is active, after success, or after the engine said nothing is installed.
enum NativeRepairOffer {
    static func isOffered(discovery: LocalServerDiscovery.Status,
                          installState: NativeInstallState,
                          updateFailed: Bool,
                          hasInstallation: Bool) -> Bool {
        if installState.isActive { return false }
        if case .succeeded = installState { return false }
        if installState == .failed(.installer(.notInstalled)) { return false }
        guard hasInstallation else { return false }
        switch discovery {
        case .installedNotRunning, .startFailed: return true
        case .idle, .checking, .running, .starting, .notDetected: return updateFailed
        }
    }
}

/// Copy for the native install controls on the connection screen.
enum NativeInstallFlowCopy {
    static let offer = "Ciaobot is not installed on this Mac. You can install it here and finish setup in this window."
    static let installButton = "Install Ciaobot on This Mac"
    static let retryButton = "Try Installing Again"
    static let cancelButton = "Cancel Installation"
    static let repairButton = "Repair Ciaobot"
    static let repairHint = "Downloads the latest verified Ciaobot release and repairs the installation on this Mac, then restarts Ciaobot. Your workspace is not changed."
    static let cancelRepairButton = "Cancel Repair"
    static let cancelRepairHint = "Stops the repair. If Ciaobot was already stopped, it stays stopped until you repair it again."
    static let portButton = "Set Port and Start Ciaobot"
    static let portHint = "Writes PWA_PORT to the workspace .env file on this Mac and restarts Ciaobot. Other settings in .env are kept."
    static let portSaveFailed = "The new port could not be saved to the workspace .env file. Check the file's permissions and try again."
    static let portWorkspaceMissing = "The Ciaobot workspace could not be found. Select Check Again, or run Repair Ciaobot."

    static func connectFailedAfterInstall(_ origin: Origin) -> String {
        "Ciaobot was installed and is running at \(origin.canonicalKey), but this app could not verify it. Check that it is still running, then select Connect."
    }

    static func connectFailedAfterRepair(_ origin: Origin) -> String {
        "Ciaobot was repaired and is running at \(origin.canonicalKey), but this app could not verify it. Check that it is still running, then select Connect."
    }
}

/// Which install failures the connection screen can recover from with a port
/// field, and how the typed port is read. The engine installs on 8443 unless
/// the workspace `.env` says otherwise, so only the two port-shaped failures
/// have a recovery that is not another identical retry.
enum NativeInstallPortRecovery {
    static func isRecoverable(_ failure: NativeInstallFailure) -> Bool {
        switch failure {
        case .installer(.activationRefused), .installer(.serviceNotReady): return true
        default: return false
        }
    }

    /// A plain decimal port in 1...65535, or nil for anything else.
    static func port(_ text: String) -> Int? {
        guard let port = Int(text.trimmingCharacters(in: .whitespacesAndNewlines)),
              (1...65535).contains(port) else { return nil }
        return port
    }
}

/// A failed install/repair message is stale once discovery finds the engine
/// running: the failure no longer describes this Mac.
enum NativeInstallFailureReset {
    static func shouldReset(install: NativeInstallState, discovery: LocalServerDiscovery.Status) -> Bool {
        guard case .failed = install else { return false }
        guard case .running = discovery else { return false }
        return true
    }
}
