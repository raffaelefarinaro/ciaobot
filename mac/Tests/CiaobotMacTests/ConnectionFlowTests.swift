import XCTest
import Combine
import Foundation
import WebKit

@testable import CiaobotMac

/// Connect and forget share one screen and one stored sign-in. They must never
/// overlap, including when Return starts a connect while a forget is running.
final class ConnectionTaskGateTests: XCTestCase {
    func testReturnCannotStartAProbeWhileForgetIsRunning() {
        var gate = ConnectionTaskGate()
        XCTAssertTrue(gate.beginForget())
        XCTAssertTrue(gate.isBusy)
        XCTAssertFalse(gate.beginCheck(), "Return started a probe while forget was running")
        XCTAssertEqual(gate.task, .forgetting)
    }

    func testForgetCannotStartWhileAProbeIsRunning() {
        var gate = ConnectionTaskGate()
        XCTAssertTrue(gate.beginCheck())
        XCTAssertFalse(gate.beginForget(), "Forget started while the probe was still running")
        XCTAssertEqual(gate.task, .checking)
    }

    func testARepeatedConnectCannotStartTwice() {
        var gate = ConnectionTaskGate()
        XCTAssertTrue(gate.beginCheck())
        XCTAssertFalse(gate.beginCheck())
        XCTAssertFalse(gate.beginForget())
    }

    func testTheGateReopensOnlyForTheMatchingCompletion() {
        var gate = ConnectionTaskGate()
        XCTAssertTrue(gate.beginCheck())
        gate.endForget()
        XCTAssertEqual(gate.task, .checking, "A foreign completion must not clear the current task")

        gate.endCheck()
        XCTAssertEqual(gate.task, .idle)
        XCTAssertTrue(gate.beginForget())
        gate.endCheck()
        XCTAssertEqual(gate.task, .forgetting)
        gate.endForget()
        XCTAssertEqual(gate.task, .idle)
    }

    func testBothOperationsAreAvailableAgainAfterCompletion() {
        var gate = ConnectionTaskGate()
        XCTAssertTrue(gate.beginCheck())
        gate.endCheck()
        XCTAssertTrue(gate.beginForget())
        gate.endForget()
        XCTAssertTrue(gate.beginCheck())
    }
}

final class PersistentSignInCopyTests: XCTestCase {
    private let https = Origin("https://example.com")!
    private let http = Origin("http://127.0.0.1:8443")!

    func testEveryExplanationSaysWhatIsMissing() {
        XCTAssertTrue(PersistentSignInCopy.explanation(origin: nil, persistentAvailable: true, registryAvailable: true).contains("HTTPS"))
        XCTAssertTrue(PersistentSignInCopy.explanation(origin: http, persistentAvailable: true, registryAvailable: true).contains("HTTPS-only"))
        XCTAssertTrue(PersistentSignInCopy.explanation(origin: https, persistentAvailable: false, registryAvailable: true).contains("macOS 14"))
        XCTAssertTrue(PersistentSignInCopy.explanation(origin: https, persistentAvailable: true, registryAvailable: false).contains("saved sign-in list"))
        XCTAssertTrue(PersistentSignInCopy.explanation(origin: https, persistentAvailable: true, registryAvailable: true).contains("WebKit store"))
    }

    func testCorruptRegistryCopyDoesNotSuggestDeletingTheOnlyRecord() {
        let explanation = PersistentSignInCopy.explanation(origin: https, persistentAvailable: true, registryAvailable: false)
        let fallback = SessionStorageFallback.registryUnavailable.userMessage

        for text in [explanation, fallback] {
            let lowered = text.lowercased()
            XCTAssertFalse(lowered.contains("delete it to"), "Copy suggests deleting the only mapping record: \(text)")
            XCTAssertFalse(lowered.contains("remove it to"), "Copy suggests removing the only mapping record: \(text)")
            XCTAssertFalse(lowered.contains("delete the file"), "Copy suggests deleting the only mapping record: \(text)")
            XCTAssertFalse(lowered.contains("remove the file"), "Copy suggests removing the only mapping record: \(text)")
            XCTAssertTrue(lowered.contains("do not delete") || lowered.contains("lose the record") || lowered.contains("only record"),
                          "Copy does not warn that the list is the only record: \(text)")
        }
    }

    func testHealthyCopyDoesNotPromiseCrossPortCookieIsolation() {
        let text = PersistentSignInCopy.explanation(origin: https, persistentAvailable: true, registryAvailable: true)
        XCTAssertFalse(text.lowercased().contains("different port"),
                       "The UI must not promise isolation from subresources on another port: \(text)")
        XCTAssertFalse(text.lowercased().contains("never shared"),
                       "The UI must not promise absolute cookie isolation: \(text)")
    }
}

/// Persistence may only start on where it can actually work: an HTTPS origin
/// with persistent stores available. Everywhere else stays off even when the
/// stored explicit choice says otherwise, because the choice cannot create a
/// store the system will not provide.
final class PersistentSignInDefaultTests: XCTestCase {
    private let https = Origin("https://example.com")!
    private let http = Origin("http://127.0.0.1:8443")!

    func testPersistentSignInDefaultsOnForHTTPSWhenAvailable() {
        let enabled = PersistentSignInDefault.isEnabled(
            origin: https,
            persistentAvailable: true,
            explicitChoice: nil
        )

        XCTAssertTrue(enabled, "An HTTPS origin with persistent stores should arrive opted in")
    }

    func testPersistentSignInDefaultsOffForHTTPSWithoutPersistentStores() {
        let enabled = PersistentSignInDefault.isEnabled(
            origin: https,
            persistentAvailable: false,
            explicitChoice: nil
        )

        XCTAssertFalse(enabled, "macOS 13 has no named WebKit stores, so the default must stay off")
    }

    func testPersistentSignInDefaultsOffForPlainHTTP() {
        let enabled = PersistentSignInDefault.isEnabled(
            origin: http,
            persistentAvailable: true,
            explicitChoice: nil
        )

        XCTAssertFalse(enabled, "HTTP always uses an in-memory store, so the default must stay off")
    }

    func testExplicitChoiceWinsOverTheDefault() {
        XCTAssertFalse(PersistentSignInDefault.isEnabled(
            origin: https,
            persistentAvailable: true,
            explicitChoice: false
        ), "Turning persistence off for an HTTPS origin must be honoured")

        XCTAssertTrue(PersistentSignInDefault.isEnabled(
            origin: https,
            persistentAvailable: true,
            explicitChoice: true
        ), "An explicit opt-in for an HTTPS origin must be honoured")
    }

    func testExplicitChoiceCannotEnablePersistenceWhereItIsImpossible() {
        XCTAssertFalse(PersistentSignInDefault.isEnabled(
            origin: https,
            persistentAvailable: false,
            explicitChoice: true
        ), "No stored choice may enable persistence on a system without named stores")

        XCTAssertFalse(PersistentSignInDefault.isEnabled(
            origin: http,
            persistentAvailable: true,
            explicitChoice: true
        ), "No stored choice may enable persistence for an HTTP origin")

        XCTAssertFalse(PersistentSignInDefault.isEnabled(
            origin: nil,
            persistentAvailable: true,
            explicitChoice: true
        ), "No stored choice may enable persistence before an address is entered")
    }
}

@MainActor
final class LocalSignInForgetFlowTests: XCTestCase {
    func testReleasesErasesOnlyLocalStoreAndReconnectsCurrentGeneration() {
        let origin = Origin("https://127.0.0.1:8778")!
        let identity = LocalEngineTLSIdentity(pid: 731, startupID: "new-launch", port: 8778,
                                              sha256: String(repeating: "b", count: 64))!
        var steps: [String] = []
        var erasedOrigin: Origin?
        var erasedKey: SessionStoreKey?
        var outcome: Result<Void, ForgetFailure>?

        LocalSignInForgetFlow.perform(
            origin: origin,
            releaseView: { completion in
                steps.append("release")
                completion(true)
            },
            eraseStore: { candidate, key, completion in
                steps.append("erase")
                erasedOrigin = candidate
                erasedKey = key
                completion(.success(()))
            },
            currentIdentity: {
                steps.append("identity")
                return identity
            },
            reconnect: { current in
                XCTAssertEqual(current, identity)
                steps.append("reconnect")
            },
            completion: { outcome = $0 }
        )

        XCTAssertEqual(steps, ["release", "erase", "identity", "reconnect"])
        XCTAssertEqual(erasedOrigin, origin)
        XCTAssertEqual(erasedKey, .localEngine)
        guard case .success? = outcome else { XCTFail("Forget should complete successfully"); return }
    }

    func testReconnectsTheVerifiedGenerationThatIsRunningAfterErase() {
        let origin = Origin("https://127.0.0.1:8778")!
        let replacement = LocalEngineTLSIdentity(pid: 731, startupID: "new-launch", port: 8778,
                                                  sha256: String(repeating: "c", count: 64))!
        var reconnectedWith: LocalEngineTLSIdentity?

        LocalSignInForgetFlow.perform(
            origin: origin,
            releaseView: { $0(true) },
            eraseStore: { _, key, completion in
                XCTAssertEqual(key, .localEngine)
                completion(.success(()))
            },
            currentIdentity: { replacement },
            reconnect: { reconnectedWith = $0 },
            completion: { _ in }
        )

        XCTAssertEqual(reconnectedWith, replacement)
    }

    func testViewReleaseTimeoutKeepsTheStoreAndOffersACompletionToRetry() {
        let origin = Origin("https://127.0.0.1:8778")!
        var erased = false
        var identityLookups = 0
        var completion: Result<Void, ForgetFailure>??

        LocalSignInForgetFlow.perform(
            origin: origin,
            releaseView: { $0(false) },
            eraseStore: { _, _, _ in erased = true },
            currentIdentity: { identityLookups += 1; return nil },
            reconnect: { _ in XCTFail("must not reconnect after a release timeout") },
            completion: { completion = $0 }
        )

        XCTAssertFalse(erased, "store removal must wait until the WebKit view releases it")
        XCTAssertEqual(identityLookups, 0)
        guard let completion else { return XCTFail("release timeout did not return control for retry") }
        switch completion {
        case nil: break
        case .some: XCTFail("release timeout must complete with the retryable no-result state")
        }
    }
}

/// Only a connection that actually answered is remembered, and a remembered
/// origin always comes back prefilled. The probe is injected so the test never
/// contacts anything.
@MainActor
final class ConnectionRememberingTests: XCTestCase {
    private final class StubProbe: ProbeChecking {
        private let responds: Bool
        private(set) var checked: [Origin] = []

        init(responds: Bool) {
            self.responds = responds
        }

        func check(_ origin: Origin, completion: @escaping (Bool) -> Void) {
            checked.append(origin)
            completion(responds)
        }
    }

    private final class NoopEraser: WebsiteDataStoreErasing {
        func remove(identifier: UUID, completion: @escaping (Error?) -> Void) {
            completion(nil)
        }
    }

    @MainActor
    private final class FakeHostStore: HostListStoring {
        private(set) var added: [Origin] = []
        var hosts: [HostRecord] = []

        func add(_ origin: Origin) {
            added.append(origin)
            let record = HostRecord(origin: origin.canonicalKey,
                                    label: HostRecord.defaultLabel(for: origin.canonicalKey))
            if !hosts.contains(record) { hosts.append(record) }
        }

        func remove(_ origin: Origin) {
            hosts.removeAll { $0.origin == origin.canonicalKey }
        }

        func rename(_ origin: Origin, label: String) {
            guard let index = hosts.firstIndex(where: { $0.origin == origin.canonicalKey }) else { return }
            hosts[index].label = label
        }
    }

    private var suiteName = ""
    private var defaults: UserDefaults!
    private var store: UserDefaultsLastServerStore!
    private var hostStore: FakeHostStore!
    private var registryURL: URL!

    override func setUp() {
        super.setUp()
        suiteName = IsolatedDefaults.makeName("ConnectionRemembering")
        defaults = UserDefaults(suiteName: suiteName)
        defaults.removePersistentDomain(forName: suiteName)
        store = UserDefaultsLastServerStore(defaults: defaults)
        hostStore = FakeHostStore()
        registryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("CiaobotMacTests-Registry-\(UUID().uuidString).json")
    }

    override func tearDown() {
        IsolatedDefaults.remove(suiteName)
        defaults = nil
        store = nil
        hostStore = nil
        if let registryURL { try? FileManager.default.removeItem(at: registryURL) }
        super.tearDown()
    }

    private func makeView(probe: ProbeChecking) -> ConnectionView {
        let signIn = SessionSignIn(
            registry: SessionStoreRegistry(fileURL: registryURL),
            eraser: NoopEraser(),
            persistentAvailable: false
        )
        return ConnectionView(probe: probe, signIn: signIn, lastServer: store, hostStore: hostStore)
    }

    func testASuccessfulConnectionRemembersItsOrigin() {
        let probe = StubProbe(responds: true)
        let view = makeView(probe: probe)

        view.connect()

        XCTAssertEqual(probe.checked.count, 1, "The connection screen did not probe exactly once")
        XCTAssertEqual(store.savedOrigin?.canonicalKey, "http://127.0.0.1:8443")
        XCTAssertEqual(hostStore.added.map(\.canonicalKey), ["http://127.0.0.1:8443"],
                       "A successful connection was not recorded in the saved host list")
    }

    func testAFailedConnectionDoesNotRememberTheAddress() {
        let probe = StubProbe(responds: false)
        let view = makeView(probe: probe)

        view.connect()

        XCTAssertEqual(probe.checked.count, 1)
        XCTAssertNil(store.savedOrigin, "A failed attempt was stored as the launch target")
        XCTAssertTrue(hostStore.added.isEmpty, "A failed probe was recorded in the saved host list")
    }

    func testAFailedConnectionLeavesThePreviouslySavedOriginAlone() throws {
        let previous = try XCTUnwrap(Origin("https://remote.example:8443"))
        store.remember(previous)
        let view = makeView(probe: StubProbe(responds: false))

        view.connect()

        XCTAssertEqual(store.savedOrigin?.canonicalKey, previous.canonicalKey)
    }

    func testARememberedOriginPrefillsTheField() throws {
        let saved = try XCTUnwrap(Origin("https://remote.example:8443"))
        store.remember(saved)

        let view = makeView(probe: StubProbe(responds: false))

        XCTAssertEqual(view.launchAddress, saved.url.absoluteString)
    }

    func testNoSavedOriginPrefillsTheLocalCandidate() {
        let view = makeView(probe: StubProbe(responds: false))

        XCTAssertEqual(view.launchAddress, "http://127.0.0.1:8443")
    }
}

@MainActor
final class SessionViewTrackerTests: XCTestCase {
    func testNothingAttachedCountsAsReleased() {
        let tracker = SessionViewTracker()
        var result: Bool?
        tracker.whenReleased(timeout: 1) { result = $0 }
        XCTAssertEqual(result, true, "With no tracked view, forget must not wait")
    }

    func testTrackerWaitsUntilTheTrackedViewIsDeallocated() {
        let tracker = SessionViewTracker()
        var view: NSObject? = NSObject()
        tracker.attach(view!)

        var result: Bool?
        let reported = expectation(description: "release reported")
        tracker.whenReleased(timeout: 5) { ok in
            result = ok
            reported.fulfill()
        }

        RunLoop.main.run(until: Date().addingTimeInterval(0.15))
        XCTAssertNil(result, "The tracker reported release while the view was still strongly held")
        XCTAssertFalse(tracker.isReleased)

        view = nil
        wait(for: [reported], timeout: 5)
        XCTAssertEqual(result, true)
        XCTAssertTrue(tracker.isReleased)
    }

    func testTrackerTimesOutWhileTheViewIsHeld() {
        let tracker = SessionViewTracker()
        let view = NSObject()
        tracker.attach(view)

        var result: Bool?
        let reported = expectation(description: "tracker timed out")
        tracker.whenReleased(timeout: 0.1) { ok in
            result = ok
            reported.fulfill()
        }

        wait(for: [reported], timeout: 2)
        XCTAssertEqual(result, false, "A view that never releases must not be reported as released")
        withExtendedLifetime(view) {}
    }
}

/// A second host has no session object, so the chat models read a fixed origin
/// and store instead. The session has to keep working as the same context, and
/// its publisher has to fire only after the stored connection it describes.
@MainActor
final class ConnectionFlowTests: XCTestCase {
    func testStaticHostContextReportsItsFixedOriginAndStore() throws {
        let origin = try XCTUnwrap(Origin("https://fixed.example:8443"))
        let dataStore = WKWebsiteDataStore.nonPersistent()
        let context = StaticHostContext(origin: origin, dataStore: dataStore)

        XCTAssertTrue(context.isConnected)
        XCTAssertEqual(context.currentOrigin?.canonicalKey, origin.canonicalKey)
        XCTAssertTrue(context.dataStore === dataStore, "The context must report exactly the store it was given")

        var emissions = 0
        let subscription = context.connectionPublisher.sink { emissions += 1 }
        XCTAssertEqual(emissions, 1, "A fixed host replays its connection once on subscribe")

        let settled = expectation(description: "no repeat emission")
        DispatchQueue.main.async { settled.fulfill() }
        wait(for: [settled], timeout: 1)
        XCTAssertEqual(emissions, 1, "A fixed host must not publish a second connection")

        subscription.cancel()
    }

    func testSessionAsContextPublishesAfterTheStoredConnection() throws {
        let status = AppSessionStatus()
        let context: ConnectionContext = status
        let origin = try XCTUnwrap(Origin("https://remote.example:8443"))

        var observed: Origin?
        let subscription = context.connectionPublisher.sink {
            observed = context.currentOrigin
        }
        XCTAssertNil(observed, "Nothing is connected before the first report")

        status.reportConnected(origin)

        XCTAssertEqual(observed?.canonicalKey, origin.canonicalKey,
                       "Subscribers read currentOrigin after the published connection has settled")
        XCTAssertTrue(context.isConnected)

        subscription.cancel()
    }

    // MARK: External connection screen

    /// The connection screen is the external mode's only surface, and it offers
    /// no install, repair, update or same-Mac discovery control. The view is
    /// AppKit/SwiftUI, so this reads its source the way BundledEngineTests does.
    /// #3130 M3 deleted the engine installer, updater and local discovery. The
    /// compiler proves a deleted type is not referenced, but not that a removed
    /// name or UI string is absent from the sources, so every source file is
    /// scanned. Type names match anywhere, ignoring case. UI strings match only
    /// where a string literal starts, so running text such as "Restart Ciaobot"
    /// or "to check again." stays allowed.
    func testNoEngineInstallerRemnantsInAnySource() throws {
        let sources = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/CiaobotMac")
        let files = try FileManager.default.contentsOfDirectory(at: sources, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }
        XCTAssertFalse(files.isEmpty, "The source scan found no Swift files under \(sources.path).")

        let banned = [
            "NativeInstall", "EngineUpdat", "EngineRelease", "LocalServerDiscovery",
            "WorkspacePort", "FreePort", "WorkspaceDotenvPort", "Ciaobot installer",
            "\"Start Ciaobot", "\"Repair Ciaobot", "\"Find Ciaobot", "\"Install Ciaobot",
            "\"Update Ciaobot", "\"Try Updating Ciaobot", "\"Installing Ciaobot", "\"Ciaobot was updated",
            "\"Check Again",
        ]
        for file in files {
            let text = try String(contentsOf: file, encoding: .utf8)
            for term in banned {
                XCTAssertNil(text.range(of: term, options: .caseInsensitive),
                             "\(file.lastPathComponent) still mentions the removed engine installer: \(term)")
            }
        }
    }
}
