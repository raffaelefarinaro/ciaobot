import Combine
import Foundation
import Sparkle

/// The client's only view of Sparkle. The menu bar needs the availability fact,
/// the truthful reason when updates are impossible, and the two actions.
@MainActor
protocol ClientUpdateControlling: AnyObject {
    var canCheckForUpdates: Bool { get }
    var unavailableReason: String? { get }
    /// Whether verified updates are downloaded and installed without asking.
    /// Reads false, and the setter does nothing, until the updater has started.
    var automaticallyInstallsUpdates: Bool { get }
    func setAutomaticallyInstallsUpdates(_ on: Bool)
    /// Fires when availability, the check state or the automatic setting may
    /// have changed. It carries no data: readers re-read the properties above.
    var changes: AnyPublisher<Void, Never> { get }
    func start()
    func checkForUpdates()
}

/// The Sparkle surface the client uses. A test double is the only
/// implementation in the test suite, so no test starts a real updater or
/// reaches the network feed.
@MainActor
protocol ClientUpdaterEngine: AnyObject {
    var canCheckForUpdates: Bool { get }
    var automaticallyChecksForUpdates: Bool { get }
    /// Sparkle's `automaticallyDownloadsUpdates`: a verified update is
    /// downloaded in the background and installed through the drain gate.
    var automaticallyDownloadsUpdates: Bool { get set }
    /// Sparkle's KVO on `canCheckForUpdates` and `automaticallyDownloadsUpdates`.
    var stateChanges: AnyPublisher<Void, Never> { get }
    func start()
    func checkForUpdates()
    func checkForUpdatesInBackground()
}

/// Sparkle's updater delegate for this client. It implements one contract:
/// taking an already verified, downloaded update and installing it, with the
/// bundled engine drained first (`UpdateInstallGate`).
@MainActor
final class ClientUpdateInstallDelegate: NSObject, SPUUpdaterDelegate {
    /// The Sparkle 2.10.1 selector from `SPUUpdaterDelegate.h`. It is spelled
    /// out so the runtime contract cannot drift with a Swift rename.
    static let immediateInstallSelector = NSSelectorFromString(
        "updater:willInstallUpdateOnQuit:immediateInstallationBlock:"
    )

    typealias GateFactory = @MainActor (_ owner: String) -> UpdateInstallGate

    /// The longest drain owner the engine is asked to hold.
    static let maxOwnerLength = 64

    /// The drain owner for an install of `version`: `sparkle-` plus the version,
    /// with every character outside letters, digits, `.`, `_` and `-` replaced,
    /// and clipped to `maxOwnerLength`. The engine stores the owner as text and
    /// the cancel must name it exactly, so one function builds it for both.
    static func drainOwner(forVersion version: String) -> String {
        let allowed = Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
        let owner = String("sparkle-\(version)".map { allowed.contains($0) ? $0 : "_" })
        return String(owner.prefix(maxOwnerLength))
    }

    /// The one gate factory the shipped updater uses. Every gate reads the
    /// bundled engine through `localState`, drains it through `makeDrainer`
    /// (the loopback client in the app), and raises `updateQuit` just before
    /// Sparkle's handler runs, recording whether that install drained.
    static func productionGate(
        localState: @escaping UpdateInstallGate.LocalState,
        updateQuit: UpdateQuitIntent,
        makeDrainer: @escaping @MainActor () -> EngineDraining = { EngineDrainClient() }
    ) -> GateFactory {
        { owner in
            UpdateInstallGate(
                localState: localState,
                drainer: makeDrainer(),
                owner: owner,
                willInstall: {
                    // The gate re-read the engine just before this turn and it
                    // still matches the reading that decided the install, so a
                    // running engine here is one the gate drained.
                    let drained: Bool
                    if case .running = localState() { drained = true } else { drained = false }
                    updateQuit.beginInstall(drained: drained)
                },
                schedule: UpdateInstallGate.mainQueueSchedule
            )
        }
    }

    private let makeGate: GateFactory
    private let updateQuit: UpdateQuitIntent
    /// The gate for the update Sparkle is installing now.
    private var gate: UpdateInstallGate?

    init(makeGate: @escaping GateFactory, updateQuit: UpdateQuitIntent) {
        self.makeGate = makeGate
        self.updateQuit = updateQuit
        super.init()
        // A quit that abandons a raised install (a cancelled quit) cancels the
        // gate's held drain too, through the same path as Sparkle's abort.
        updateQuit.onAbandon = { [weak self] in self?.gate?.installAborted() }
    }

    /// Hands the install to this client's gate. The appcast item is unused
    /// beyond naming the drain owner: Sparkle has already verified the archive
    /// signature and notarization before this point.
    @objc(updater:willInstallUpdateOnQuit:immediateInstallationBlock:)
    func updater(
        _ updater: SPUUpdater,
        willInstallUpdateOnQuit item: SUAppcastItem,
        immediateInstallationBlock immediateInstallHandler: @escaping () -> Void
    ) -> Bool {
        takeControl(owner: Self.drainOwner(forVersion: item.versionString), of: immediateInstallHandler)
    }

    /// Sparkle's own "Install and Relaunch" (automatic install off) has already
    /// shown its alert, which is the user's consent, so there is no drain. The
    /// terminate that follows is still an update quit: no dialog. Nothing was
    /// drained, so the app's quit path may still ask about running chats.
    @objc func updaterWillRelaunchApplication(_ updater: SPUUpdater) {
        relaunchWillStart()
    }

    func relaunchWillStart() {
        updateQuit.beginInstall(drained: false)
    }

    /// Sparkle reports a driver that stopped with an error. Only an install-phase
    /// error ends the install this client holds.
    @objc func updater(_ updater: SPUUpdater, didAbortWithError error: Error) {
        installDidAbort(error)
    }

    /// Sparkle's background checks report "no update" and appcast or download
    /// trouble through the same method. Those must not release a drain that a
    /// pending install is still holding, so they are ignored.
    func installDidAbort(_ error: Error) {
        let nsError = error as NSError
        guard nsError.domain == SUSparkleErrorDomain,
              UpdateInstallGate.installAbortCodes.contains(nsError.code) else { return }
        updateQuit.abandonInstall()
    }

    /// The gate for `owner` takes the handler. A second call for the same
    /// owner replaces the stored handler and keeps the gate. A call for another
    /// owner first aborts the old gate, which cancels its drain under the old
    /// owner, so that cancel is sent before the new owner's first drain. While
    /// an install is already scheduled, another owner is refused: Sparkle keeps
    /// that update for its install on quit.
    func takeControl(owner: String, of immediateInstallHandler: @escaping () -> Void) -> Bool {
        if let current = gate, current.owner != owner {
            if current.isInstalling { return false }
            current.installAborted()
            gate = nil
        }
        if gate == nil {
            gate = makeGate(owner)
        }
        return gate?.takeControl(immediateInstallHandler) ?? false
    }
}

/// The real Sparkle engine. Sparkle reads the feed URL and public key from the
/// host bundle's Info.plist; `scripts/build-app.sh` writes them only for a
/// `CIAOBOT_RELEASE_UPDATES=1` build. Creating the controller is inert: it
/// schedules nothing until `start()` is called.
@MainActor
final class SparkleUpdaterEngine: ClientUpdaterEngine {
    // Sparkle holds both of these weakly, so the engine owns them.
    private let installDelegate: ClientUpdateInstallDelegate
    private let controller: SPUStandardUpdaterController

    init(installDelegate: ClientUpdateInstallDelegate) {
        self.installDelegate = installDelegate
        self.controller = SPUStandardUpdaterController(
            startingUpdater: false,
            updaterDelegate: installDelegate,
            userDriverDelegate: nil
        )
    }

    var canCheckForUpdates: Bool { controller.updater.canCheckForUpdates }
    var automaticallyChecksForUpdates: Bool { controller.updater.automaticallyChecksForUpdates }
    var automaticallyDownloadsUpdates: Bool {
        get { controller.updater.automaticallyDownloadsUpdates }
        set { controller.updater.automaticallyDownloadsUpdates = newValue }
    }
    var stateChanges: AnyPublisher<Void, Never> {
        Publishers.Merge(
            controller.updater.publisher(for: \.canCheckForUpdates).map { _ in () },
            controller.updater.publisher(for: \.automaticallyDownloadsUpdates).map { _ in () }
        )
        .eraseToAnyPublisher()
    }

    func start() { controller.startUpdater() }
    func checkForUpdates() { controller.updater.checkForUpdates() }
    func checkForUpdatesInBackground() { controller.updater.checkForUpdatesInBackground() }
}

/// The single retained updater for the app's lifetime.
///
/// It refuses to start unless the process is a release build of the shipped
/// bundle, is not a test process, and carries a feed and public key. None of
/// those checks contacts the network; a build without them stays inert and
/// reports why in the menu.
@MainActor
final class ClientUpdater: ClientUpdateControlling {
    /// The public facts the decision is made from. `allowsUpdates` is false in
    /// a DEBUG build; `isTestProcess` covers `swift test` and a GUI test launch.
    struct Configuration {
        var bundleIdentifier: String?
        var feedURL: String?
        var publicEDKey: String?
        var allowsUpdates: Bool
        var isTestProcess: Bool

        /// Reads the keys the release build wrote into the app's Info.plist.
        init(bundle: Bundle, allowsUpdates: Bool, isTestProcess: Bool) {
            self.init(
                bundleIdentifier: bundle.bundleIdentifier,
                feedURL: bundle.object(forInfoDictionaryKey: "SUFeedURL") as? String,
                publicEDKey: bundle.object(forInfoDictionaryKey: "SUPublicEDKey") as? String,
                allowsUpdates: allowsUpdates,
                isTestProcess: isTestProcess
            )
        }

        init(
            bundleIdentifier: String?,
            feedURL: String?,
            publicEDKey: String?,
            allowsUpdates: Bool,
            isTestProcess: Bool
        ) {
            self.bundleIdentifier = bundleIdentifier
            self.feedURL = feedURL
            self.publicEDKey = publicEDKey
            self.allowsUpdates = allowsUpdates
            self.isTestProcess = isTestProcess
        }
    }

    /// The bundle identifier WebKit stores, saved sign-in and notification
    /// permission are tied to. Only this app may update itself.
    static let supportedBundleIdentifier = "local.ciaobot.mac"
    /// A reason becomes a menu tooltip; keep it one short line.
    static let maxReasonLength = 120

    static let debugBuildReason = "Updates are unavailable in a debug build."
    static let testProcessReason = "Updates are disabled during tests."
    static let foreignBundleReasonPrefix = "Updates are only available to the Ciaobot app"
    static let noFeedReason = "This build has no update feed configured."
    static let noKeyReason = "This build has no update signing key configured."

    private let makeEngine: EngineFactory
    private var engine: ClientUpdaterEngine?
    private var engineChanges: AnyCancellable?
    private let changeSubject = PassthroughSubject<Void, Never>()
    private var started = false
    private(set) var unavailableReason: String?
    /// The install delegate the shipped initializer wired to the engine. Nil
    /// for an updater built from a test double.
    private(set) var installDelegate: ClientUpdateInstallDelegate?

    typealias EngineFactory = @MainActor () -> ClientUpdaterEngine

    /// The shipped updater: identity from the running bundle, feed and key from
    /// its Info.plist. `localState` reads the bundled engine and `makeDrainer`
    /// makes the drain client; the app passes neither's default for real use.
    convenience init(
        bundle: Bundle = .main,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        updateQuit: UpdateQuitIntent? = nil,
        localState: @escaping UpdateInstallGate.LocalState,
        makeDrainer: @escaping @MainActor () -> EngineDraining = { EngineDrainClient() }
    ) {
        let quit = updateQuit ?? UpdateQuitIntent()
        let gates = ClientUpdateInstallDelegate.productionGate(
            localState: localState,
            updateQuit: quit,
            makeDrainer: makeDrainer
        )
        let delegate = ClientUpdateInstallDelegate(makeGate: gates, updateQuit: quit)
        self.init(
            configuration: Configuration(
                bundle: bundle,
                allowsUpdates: Self.isReleaseBuild,
                isTestProcess: Self.isTestProcess(environment: environment)
            ),
            makeEngine: { SparkleUpdaterEngine(installDelegate: delegate) }
        )
        installDelegate = delegate
    }

    init(configuration: Configuration, makeEngine: @escaping EngineFactory) {
        self.makeEngine = makeEngine
        self.unavailableReason = Self.unavailableReason(for: configuration)
    }

    var canCheckForUpdates: Bool {
        started && engine?.canCheckForUpdates == true
    }

    /// The engine exists only for a started, available updater, so an
    /// unstarted or unavailable updater reads false and ignores the setter.
    var automaticallyInstallsUpdates: Bool {
        engine?.automaticallyDownloadsUpdates ?? false
    }

    func setAutomaticallyInstallsUpdates(_ on: Bool) {
        guard let engine else { return }
        engine.automaticallyDownloadsUpdates = on
        changeSubject.send()
    }

    var changes: AnyPublisher<Void, Never> { changeSubject.eraseToAnyPublisher() }

    /// Starts the retained updater exactly once. A second call is a no-op, so a
    /// launch path that runs twice cannot schedule two check cycles.
    func start() {
        guard !started else { return }
        started = true
        guard unavailableReason == nil else { return }

        let engine = makeEngine()
        self.engine = engine
        engineChanges = engine.stateChanges.sink { [changeSubject] _ in changeSubject.send() }
        engine.start()
        // Sparkle's own guidance for a launch check: only immediately after
        // starting, and only while automatic checks are on. Turning the option
        // off is the user's decision and must not be overridden.
        if engine.automaticallyChecksForUpdates {
            engine.checkForUpdatesInBackground()
        }
    }

    func checkForUpdates() {
        guard canCheckForUpdates else { return }
        engine?.checkForUpdates()
    }

    // MARK: - Availability

    /// The first reason updates cannot run, or nil when every public fact is
    /// present. Order matters: a test or debug process is reported as such even
    /// if it somehow carries a release plist. Every reason is bounded menu text.
    static func unavailableReason(for configuration: Configuration) -> String? {
        guard let reason = reason(for: configuration) else { return nil }
        return bounded(reason)
    }

    private static func reason(for configuration: Configuration) -> String? {
        if !configuration.allowsUpdates { return debugBuildReason }
        if configuration.isTestProcess { return testProcessReason }
        if configuration.bundleIdentifier != supportedBundleIdentifier {
            return foreignBundleReason(configuration.bundleIdentifier)
        }
        if !isUpdateFeed(configuration.feedURL) { return noFeedReason }
        if !isPublicKey(configuration.publicEDKey) { return noKeyReason }
        return nil
    }

    /// A reason is shown as a menu tooltip, so it is sanitized and clipped like
    /// any other untrusted menu text.
    static func bounded(_ reason: String) -> String {
        MenuTextSanitizer.sanitize(
            reason,
            limit: maxReasonLength,
            fallback: "Updates are unavailable in this build."
        )
    }

    /// Names the offending identity, sanitized and clipped: a bundle identifier
    /// is data the client did not author.
    static func foreignBundleReason(_ identifier: String?) -> String {
        let detail = MenuTextSanitizer.sanitize(
            identifier ?? "this process",
            limit: 48,
            fallback: "this process"
        )
        return bounded("\(foreignBundleReasonPrefix), not \(detail).")
    }

    /// A feed is only usable when it is an https URL. A bare http feed would let
    /// an on-path attacker replace the appcast, and Sparkle only trusts the
    /// archive signature after download.
    static func isUpdateFeed(_ raw: String?) -> Bool {
        guard let raw = raw?.trimmingCharacters(in: .whitespacesAndNewlines),
              let url = URL(string: raw),
              url.scheme?.lowercased() == "https",
              url.host?.isEmpty == false else { return false }
        return true
    }

    /// A Sparkle EdDSA public key is the 32-byte key in base64. The check is
    /// deliberately a shape check: the key itself is public.
    static func isPublicKey(_ raw: String?) -> Bool {
        guard let raw = raw?.trimmingCharacters(in: .whitespacesAndNewlines),
              raw.count == 44,
              raw.hasSuffix("=") else { return false }
        return Data(base64Encoded: raw)?.count == 32
    }

    static var isReleaseBuild: Bool {
        #if DEBUG
        return false
        #else
        return true
        #endif
    }

    /// `swift test` injects `XCTest*` variables into the host process. A GUI test
    /// launch strips them, but that process is a DEBUG build of a bare
    /// executable, which is already vetoed above.
    static func isTestProcess(environment: [String: String]) -> Bool {
        environment.keys.contains { $0.hasPrefix("XCTest") }
    }
}
