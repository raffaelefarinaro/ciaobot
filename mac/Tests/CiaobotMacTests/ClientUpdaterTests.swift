import XCTest
import Sparkle

@testable import CiaobotMac

/// A Sparkle stand-in. The real `SPUStandardUpdaterController` is never created
/// here: no test may start an updater, schedule a check, or reach the network
/// feed.
@MainActor
private final class FakeUpdaterEngine: ClientUpdaterEngine {
    var canCheckForUpdates = true
    var automaticallyChecksForUpdates = false
    private(set) var startCount = 0
    private(set) var checkCount = 0
    private(set) var backgroundCheckCount = 0

    func start() { startCount += 1 }
    func checkForUpdates() { checkCount += 1 }
    func checkForUpdatesInBackground() { backgroundCheckCount += 1 }
}

@MainActor
final class ClientUpdaterTests: XCTestCase {
    private let feed = "https://example.invalid/ciaobot/appcast.xml"
    private let publicKey = "uLLvdsFBrO33Zb9zLZ+m/ZUun3D9vUf+9t1ufoa2guM="

    // MARK: - Starting

    func testStartCreatesOneEngineAndStartsItOnce() {
        let engine = FakeUpdaterEngine()
        var factoryCount = 0
        let updater = makeUpdater(engine: engine) { factoryCount += 1 }

        updater.start()
        updater.start()

        XCTAssertEqual(factoryCount, 1, "A second start must not build a second updater.")
        XCTAssertEqual(engine.startCount, 1, "Sparkle must be started exactly once.")
        XCTAssertNil(updater.unavailableReason)
    }

    func testCanCheckForUpdatesStaysFalseUntilTheUpdaterHasStarted() {
        let engine = FakeUpdaterEngine()
        let updater = makeUpdater(engine: engine)

        XCTAssertFalse(updater.canCheckForUpdates, "A menu item must not claim a check is possible before launch.")

        updater.start()
        XCTAssertTrue(updater.canCheckForUpdates)

        engine.canCheckForUpdates = false
        XCTAssertFalse(updater.canCheckForUpdates, "Sparkle reports when a check or download is in progress.")
    }

    func testManualCheckIsForwardedOnlyWhileCanCheckForUpdates() {
        let engine = FakeUpdaterEngine()
        let updater = makeUpdater(engine: engine)

        updater.checkForUpdates()
        XCTAssertEqual(engine.checkCount, 0, "An unstarted updater must ignore a manual check.")

        updater.start()
        updater.checkForUpdates()
        XCTAssertEqual(engine.checkCount, 1)

        engine.canCheckForUpdates = false
        updater.checkForUpdates()
        XCTAssertEqual(engine.checkCount, 1, "A busy Sparkle session must not be interrupted.")
    }

    // MARK: - Launch check

    func testLaunchCheckRunsOnceOnlyWhenAutomaticChecksAreEnabled() {
        let engine = FakeUpdaterEngine()
        engine.automaticallyChecksForUpdates = true
        let updater = makeUpdater(engine: engine)

        updater.start()

        XCTAssertEqual(engine.backgroundCheckCount, 1, "One immediate background check per launch.")
    }

    func testLaunchCheckIsSkippedWhenAutomaticChecksAreOff() {
        let engine = FakeUpdaterEngine()
        engine.automaticallyChecksForUpdates = false
        let updater = makeUpdater(engine: engine)

        updater.start()

        XCTAssertEqual(engine.startCount, 1)
        XCTAssertEqual(engine.backgroundCheckCount, 0, "A user who turned automatic checks off must not be checked on launch.")
    }

    // MARK: - Inert builds

    func testNoFeedLeavesTheUpdaterInert() {
        let engine = FakeUpdaterEngine()
        var factoryCount = 0
        let updater = makeUpdater(engine: engine, feedURL: nil) { factoryCount += 1 }

        updater.start()

        XCTAssertEqual(factoryCount, 0, "No Sparkle object may exist without a feed.")
        XCTAssertEqual(engine.startCount, 0)
        XCTAssertEqual(engine.backgroundCheckCount, 0)
        XCTAssertFalse(updater.canCheckForUpdates)
        XCTAssertEqual(updater.unavailableReason, ClientUpdater.noFeedReason)
    }

    func testNoPublicKeyLeavesTheUpdaterInert() {
        let engine = FakeUpdaterEngine()
        var factoryCount = 0
        let updater = makeUpdater(engine: engine, publicEDKey: nil) { factoryCount += 1 }

        updater.start()

        XCTAssertEqual(factoryCount, 0, "An unsigned update feed cannot be trusted.")
        XCTAssertEqual(updater.unavailableReason, ClientUpdater.noKeyReason)
        XCTAssertFalse(updater.canCheckForUpdates)
    }

    func testAFeedThatIsNotHTTPSLeavesTheUpdaterInert() {
        let engine = FakeUpdaterEngine()
        var factoryCount = 0
        let updater = makeUpdater(engine: engine, feedURL: "http://example.invalid/appcast.xml") { factoryCount += 1 }

        updater.start()

        XCTAssertEqual(factoryCount, 0)
        XCTAssertEqual(updater.unavailableReason, ClientUpdater.noFeedReason)
    }

    func testAForeignBundleIdentifierLeavesTheUpdaterInert() {
        let engine = FakeUpdaterEngine()
        var factoryCount = 0
        let updater = makeUpdater(engine: engine, bundleIdentifier: "com.example.other") { factoryCount += 1 }

        updater.start()

        XCTAssertEqual(factoryCount, 0, "Only the Ciaobot bundle may update itself.")
        XCTAssertEqual(
            updater.unavailableReason,
            ClientUpdater.foreignBundleReason("com.example.other")
        )
        XCTAssertTrue(updater.unavailableReason?.contains("com.example.other") == true)
    }

    func testDebugAndTestProcessesNeverBuildAnUpdater() {
        for configuration in [
            makeUpdaterConfiguration(allowsUpdates: false),
            makeUpdaterConfiguration(isTestProcess: true),
        ] {
            var factoryCount = 0
            let updater = ClientUpdater(configuration: configuration) {
                factoryCount += 1
                return FakeUpdaterEngine()
            }

            updater.start()

            XCTAssertEqual(factoryCount, 0, "A debug or test process must never start Sparkle.")
            XCTAssertFalse(updater.canCheckForUpdates)
            XCTAssertNotNil(updater.unavailableReason)
        }
    }

    /// The shipped initializer is the one the app uses. Inside a test process it
    /// must be inert on its own, without a test double, so even a launch path
    /// that created it would never start Sparkle.
    func testTheShippedUpdaterIsInertInsideATestProcess() {
        let updater = ClientUpdater(localState: { .external })

        updater.start()

        XCTAssertFalse(updater.canCheckForUpdates)
        XCTAssertEqual(updater.unavailableReason, ClientUpdater.debugBuildReason)
    }

    func testUnavailableReasonStaysBoundedForAHostileBundleIdentifier() throws {        let hostile = String(repeating: "a", count: 4096) + "\u{202E}evil\nrow"
        let updater = makeUpdater(bundleIdentifier: hostile)

        let reason = try XCTUnwrap(updater.unavailableReason)
        XCTAssertLessThanOrEqual(reason.count, ClientUpdater.maxReasonLength)
        XCTAssertFalse(reason.contains("\n"), "A reason becomes a menu tooltip and must stay one line.")
        XCTAssertFalse(reason.contains("\u{202E}"), "A reason must not carry a bidi override into the menu.")
    }

    // MARK: - Immediate install

    func testInstallDelegateImplementsTheSparkleImmediateInstallContract() {
        let delegate = makeInstallDelegate(drainer: ScriptedEngineDrainer())
        XCTAssertTrue(
            delegate.responds(to: ClientUpdateInstallDelegate.immediateInstallSelector),
            "The Swift signature must map to Sparkle 2.10.1's updater:willInstallUpdateOnQuit:immediateInstallationBlock:, or Sparkle will silently wait for quit."
        )
        XCTAssertTrue(delegate.conforms(to: SPUUpdaterDelegate.self))
        XCTAssertTrue(delegate.responds(to: NSSelectorFromString("updaterWillRelaunchApplication:")))
        XCTAssertTrue(delegate.responds(to: NSSelectorFromString("updater:didAbortWithError:")))
    }

    /// Sparkle's own "Install and Relaunch" has already shown its alert, so the
    /// relaunch raises the update reason and needs no drain.
    func testSparkleRelaunchRaisesTheUpdateReason() {
        let updateQuit = UpdateQuitIntent()
        let delegate = makeInstallDelegate(drainer: ScriptedEngineDrainer(), updateQuit: updateQuit)

        XCTAssertFalse(updateQuit.installing)
        delegate.relaunchWillStart()
        XCTAssertTrue(updateQuit.installing)
    }

    // MARK: - Install gate ownership

    /// Owner A holds a drain. Taking control for owner B first aborts A's gate,
    /// so A's cancel is sent under A's owner before B's first drain.
    func testAnOwnerChangeCancelsTheHeldDrainBeforeTheNewOwnerDrains() async {
        let drainer = ScriptedEngineDrainer()
        drainer.fallback = .success(DrainReading(activeChats: 1, scheduleWork: 0))
        let delegate = makeInstallDelegate(drainer: drainer)

        XCTAssertTrue(delegate.takeControl(owner: "sparkle-1.0.0") {})
        await settleMainActor()
        XCTAssertTrue(delegate.takeControl(owner: "sparkle-2.0.0") {})
        await settleMainActor()

        XCTAssertEqual(
            drainer.calls,
            ["drain sparkle-1.0.0", "cancel sparkle-1.0.0", "drain sparkle-2.0.0"],
            "The old owner's cancel is issued before the new owner's drain."
        )
    }

    /// While an install is already scheduled, another owner is refused and the
    /// first install is left to run: Sparkle keeps the second update.
    func testAnotherOwnerIsRefusedWhileAnInstallIsScheduled() {
        let drainer = ScriptedEngineDrainer()
        let delegate = makeInstallDelegate(drainer: drainer, state: .external)

        XCTAssertTrue(delegate.takeControl(owner: "sparkle-1.0.0") {})
        XCTAssertFalse(delegate.takeControl(owner: "sparkle-2.0.0") {}, "A scheduled install is not replaced by another owner.")
        XCTAssertEqual(drainer.calls, [])
    }

    // MARK: - Abort handling

    /// Only an install-phase Sparkle error releases the held drain. A background
    /// check's "no update", a download error, or an authorize-later notice does not.
    func testOnlyAnInstallPhaseErrorReleasesTheHeldDrain() async {
        let drainer = ScriptedEngineDrainer()
        drainer.fallback = .success(DrainReading(activeChats: 1, scheduleWork: 0))
        let delegate = makeInstallDelegate(drainer: drainer)
        XCTAssertTrue(delegate.takeControl(owner: "sparkle-1.2.3") {})
        await settleMainActor()

        delegate.installDidAbort(sparkleError(1001))   // SUNoUpdateError from a background check
        delegate.installDidAbort(sparkleError(2001))   // SUDownloadError
        delegate.installDidAbort(sparkleError(4008))   // SUInstallationAuthorizeLaterError
        await settleMainActor()
        XCTAssertEqual(drainer.cancels.count, 0, "Non-install errors leave the held drain alone.")

        delegate.installDidAbort(sparkleError(4005))   // SUInstallationError
        await settleMainActor()
        XCTAssertEqual(drainer.cancels.map(\.owner), ["sparkle-1.2.3"])
    }

    /// An abandoned install ends the update quit reason, so a later quit asks
    /// as a user quit again.
    func testAnAbandonedInstallEndsTheUpdateQuitReason() {
        let updateQuit = UpdateQuitIntent()
        let delegate = makeInstallDelegate(drainer: ScriptedEngineDrainer(), state: .external, updateQuit: updateQuit)
        updateQuit.beginInstall()
        XCTAssertEqual(EngineLifecycle.quitReason(nil, updateInstall: updateQuit.installing), .update)

        delegate.installDidAbort(sparkleError(4005))

        XCTAssertEqual(EngineLifecycle.quitReason(nil, updateInstall: updateQuit.installing), .user)
    }

    private func sparkleError(_ code: Int) -> NSError {
        NSError(domain: SUSparkleErrorDomain, code: code)
    }

    /// The delegate under test, with its gates reading a fixed engine state and
    /// draining through `drainer`. Scheduled work is dropped, so a test drives
    /// only the calls it makes.
    private func makeInstallDelegate(
        drainer: EngineDraining,
        state: LocalEngineState = .running(port: 8777),
        updateQuit: UpdateQuitIntent? = nil
    ) -> ClientUpdateInstallDelegate {
        let updateQuit = updateQuit ?? UpdateQuitIntent()
        return ClientUpdateInstallDelegate(
            makeGate: { owner in
                UpdateInstallGate(
                    localState: { state },
                    drainer: drainer,
                    owner: owner,
                    willInstall: { updateQuit.beginInstall() },
                    schedule: { _, _ in },
                    clock: { 0 }
                )
            },
            updateQuit: updateQuit
        )
    }

    // MARK: - Configuration

    func testConfigurationIsReadFromTheBundlesInfoPlist() throws {
        let bundle = try makeBundledApp(
            identifier: "local.ciaobot.mac",
            feedURL: feed,
            publicEDKey: publicKey
        )
        let configuration = ClientUpdater.Configuration(
            bundle: bundle,
            allowsUpdates: true,
            isTestProcess: false
        )

        XCTAssertEqual(configuration.bundleIdentifier, "local.ciaobot.mac")
        XCTAssertEqual(configuration.feedURL, feed)
        XCTAssertEqual(configuration.publicEDKey, publicKey)
        XCTAssertNil(ClientUpdater.unavailableReason(for: configuration))
    }

    func testBundleWithoutUpdaterKeysProducesAnUnavailableConfiguration() throws {
        let bundle = try makeBundledApp(identifier: "local.ciaobot.mac", feedURL: nil, publicEDKey: nil)
        let configuration = ClientUpdater.Configuration(
            bundle: bundle,
            allowsUpdates: true,
            isTestProcess: false
        )

        XCTAssertNil(configuration.feedURL)
        XCTAssertNil(configuration.publicEDKey)
        XCTAssertEqual(ClientUpdater.unavailableReason(for: configuration), ClientUpdater.noFeedReason)
    }

    // MARK: - Helpers

    private func makeUpdaterConfiguration(
        bundleIdentifier: String? = "local.ciaobot.mac",
        feedURL: String? = "https://example.invalid/ciaobot/appcast.xml",
        publicEDKey: String? = "uLLvdsFBrO33Zb9zLZ+m/ZUun3D9vUf+9t1ufoa2guM=",
        allowsUpdates: Bool = true,
        isTestProcess: Bool = false
    ) -> ClientUpdater.Configuration {
        ClientUpdater.Configuration(
            bundleIdentifier: bundleIdentifier,
            feedURL: feedURL,
            publicEDKey: publicEDKey,
            allowsUpdates: allowsUpdates,
            isTestProcess: isTestProcess
        )
    }

    private func makeUpdater(
        engine: FakeUpdaterEngine? = nil,
        bundleIdentifier: String? = "local.ciaobot.mac",
        feedURL: String? = "https://example.invalid/ciaobot/appcast.xml",
        publicEDKey: String? = "uLLvdsFBrO33Zb9zLZ+m/ZUun3D9vUf+9t1ufoa2guM=",
        didMakeEngine: (() -> Void)? = nil
    ) -> ClientUpdater {
        let configuration = makeUpdaterConfiguration(
            bundleIdentifier: bundleIdentifier,
            feedURL: feedURL,
            publicEDKey: publicEDKey
        )
        let engine = engine ?? FakeUpdaterEngine()
        return ClientUpdater(configuration: configuration) {
            didMakeEngine?()
            return engine
        }
    }

    /// Writes a minimal `.app` with the updater keys the release build writes,
    /// so the configuration path is tested against a real `Bundle` instead of a
    /// dictionary that stands in for one.
    private func makeBundledApp(
        identifier: String,
        feedURL: String?,
        publicEDKey: String?
    ) throws -> Bundle {
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("ciaobot-updater-\(UUID().uuidString)", isDirectory: true)
            .appendingPathComponent("Ciaobot.app", isDirectory: true)
        let contents = root.appendingPathComponent("Contents", isDirectory: true)
        try FileManager.default.createDirectory(at: contents, withIntermediateDirectories: true)
        addTeardownBlock {
            try? FileManager.default.removeItem(at: root.deletingLastPathComponent())
        }

        var keys = """
        <key>CFBundleIdentifier</key><string>\(identifier)</string>
        <key>CFBundlePackageType</key><string>APPL</string>
        """
        if let feedURL {
            keys += "<key>SUFeedURL</key><string>\(feedURL)</string>"
        }
        if let publicEDKey {
            keys += "<key>SUPublicEDKey</key><string>\(publicEDKey)</string>"
        }
        let plist = """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0"><dict>\(keys)</dict></plist>
        """
        try Data(plist.utf8).write(to: contents.appendingPathComponent("Info.plist"))

        return try XCTUnwrap(Bundle(path: root.path))
    }
}
