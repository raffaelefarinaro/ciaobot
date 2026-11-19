import Foundation
import Sparkle

/// The client's only view of Sparkle. The menu bar needs the availability fact,
/// the truthful reason when updates are impossible, and the two actions.
@MainActor
protocol ClientUpdateControlling: AnyObject {
    var canCheckForUpdates: Bool { get }
    var unavailableReason: String? { get }
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

    private let makeGate: GateFactory
    private let updateQuit: UpdateQuitIntent
    /// The gate for the update Sparkle is installing now.
    private var gate: UpdateInstallGate?

    init(makeGate: GateFactory? = nil, updateQuit: UpdateQuitIntent? = nil) {
        let quit = updateQuit ?? UpdateQuitIntent()
        self.updateQuit = quit
        self.makeGate = makeGate ?? { owner in
            UpdateInstallGate(
                localPort: { nil },
                drainer: EngineDrainClient(),
                owner: owner,
                willInstall: { quit.beginInstall() },
                schedule: UpdateInstallGate.mainQueueSchedule
            )
        }
        super.init()
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
        takeControl(owner: "sparkle-\(item.versionString)", of: immediateInstallHandler)
    }

    /// Sparkle's own "Install and Relaunch" (automatic install off) has already
    /// shown its alert, which is the user's consent, so there is no drain. The
    /// terminate that follows is still an update quit: no dialog.
    @objc func updaterWillRelaunchApplication(_ updater: SPUUpdater) {
        relaunchWillStart()
    }

    func relaunchWillStart() {
        updateQuit.beginInstall()
    }

    /// Sparkle gave up on the install: release the drain this client holds.
    @objc func updater(_ updater: SPUUpdater, didAbortWithError error: Error) {
        gate?.installAborted()
    }

    /// The gate for `owner` takes the handler. A second call for the same
    /// owner replaces the stored handler and keeps the gate.
    func takeControl(owner: String, of immediateInstallHandler: @escaping () -> Void) -> Bool {
        if gate?.owner != owner {
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

    init(installDelegate: ClientUpdateInstallDelegate? = nil) {
        let delegate = installDelegate ?? ClientUpdateInstallDelegate()
        self.installDelegate = delegate
        self.controller = SPUStandardUpdaterController(
            startingUpdater: false,
            updaterDelegate: delegate,
            userDriverDelegate: nil
        )
    }

    var canCheckForUpdates: Bool { controller.updater.canCheckForUpdates }
    var automaticallyChecksForUpdates: Bool { controller.updater.automaticallyChecksForUpdates }

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
    private var started = false
    private(set) var unavailableReason: String?

    typealias EngineFactory = @MainActor () -> ClientUpdaterEngine

    /// The shipped updater: identity from the running bundle, feed and key from
    /// its Info.plist.
    convenience init(
        bundle: Bundle = .main,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        updateQuit: UpdateQuitIntent? = nil,
        localPort: @escaping () -> Int? = { nil }
    ) {
        let quit = updateQuit ?? UpdateQuitIntent()
        self.init(
            configuration: Configuration(
                bundle: bundle,
                allowsUpdates: Self.isReleaseBuild,
                isTestProcess: Self.isTestProcess(environment: environment)
            ),
            makeEngine: {
                SparkleUpdaterEngine(installDelegate: ClientUpdateInstallDelegate(
                    makeGate: { owner in
                        UpdateInstallGate(
                            localPort: localPort,
                            drainer: EngineDrainClient(),
                            owner: owner,
                            willInstall: { quit.beginInstall() },
                            schedule: UpdateInstallGate.mainQueueSchedule
                        )
                    },
                    updateQuit: quit
                ))
            }
        )
    }

    init(configuration: Configuration, makeEngine: EngineFactory? = nil) {
        self.makeEngine = makeEngine ?? { SparkleUpdaterEngine() }
        self.unavailableReason = Self.unavailableReason(for: configuration)
    }

    var canCheckForUpdates: Bool {
        started && engine?.canCheckForUpdates == true
    }

    /// Starts the retained updater exactly once. A second call is a no-op, so a
    /// launch path that runs twice cannot schedule two check cycles.
    func start() {
        guard !started else { return }
        started = true
        guard unavailableReason == nil else { return }

        let engine = makeEngine()
        self.engine = engine
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
