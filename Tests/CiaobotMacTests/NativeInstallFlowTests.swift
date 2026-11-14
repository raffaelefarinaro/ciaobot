import XCTest
import Foundation

@testable import CiaobotMac

/// The connection screen's install offer and the rules for turning an install
/// outcome into a connection. Nothing here starts an install or touches the
/// network: the installer's fetch always throws and the probe is a stub.
@MainActor
final class NativeInstallFlowTests: XCTestCase {
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

    private var suiteName = ""
    private var defaults: UserDefaults!
    private var store: UserDefaultsLastServerStore!
    private var registryURL: URL!

    override func setUp() {
        super.setUp()
        suiteName = IsolatedDefaults.makeName("NativeInstallFlow")
        defaults = UserDefaults(suiteName: suiteName)
        defaults.removePersistentDomain(forName: suiteName)
        store = UserDefaultsLastServerStore(defaults: defaults)
        registryURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("CiaobotMacTests-InstallRegistry-\(UUID().uuidString).json")
    }

    override func tearDown() {
        IsolatedDefaults.remove(suiteName)
        defaults = nil
        store = nil
        if let registryURL { try? FileManager.default.removeItem(at: registryURL) }
        super.tearDown()
    }

    private func makeView(probe: ProbeChecking) -> ConnectionView {
        let signIn = SessionSignIn(
            registry: SessionStoreRegistry(fileURL: registryURL),
            eraser: NoopEraser(),
            persistentAvailable: false
        )
        // The fetch always throws, so no test can reach the real release.
        let installer = NativeInstaller(fetch: { _ in throw URLError(.notConnectedToInternet) })
        return ConnectionView(
            probe: probe,
            signIn: signIn,
            lastServer: store,
            hostStore: UserDefaultsHostListStore(defaults: defaults, lastServer: store),
            installer: installer
        )
    }

    private func origin(_ raw: String) throws -> Origin {
        try XCTUnwrap(Origin(raw))
    }

    func testInstallIsOfferedOnlyWhenNothingIsFound() {
        XCTAssertTrue(NativeInstallOffer.isOffered(discovery: .notDetected, install: .idle))

        let notOffered: [LocalServerDiscovery.Status] = [
            .idle,
            .checking,
            .installedNotRunning(version: nil),
            .running(version: nil, ready: true, onThisMac: true),
            .startFailed(version: nil),
        ]
        for status in notOffered {
            XCTAssertFalse(
                NativeInstallOffer.isOffered(discovery: status, install: .idle),
                "Install was offered while discovery reported \(status)"
            )
        }
    }

    func testAFailedInstallCanBeRetriedAndARunningOneCannotBeRestarted() throws {
        let failed = NativeInstallState.failed(.installer(.serviceNotReady))
        let discoveries: [LocalServerDiscovery.Status] = [
            .idle, .notDetected, .running(version: nil, ready: true, onThisMac: true),
        ]
        for status in discoveries {
            XCTAssertTrue(
                NativeInstallOffer.isOffered(discovery: status, install: failed),
                "A failed install was not offered for retry while discovery reported \(status)"
            )
        }

        let wizard = try origin("http://127.0.0.1:8443")
        let service = try origin("http://127.0.0.1:8444")
        let active: [NativeInstallState] = [
            .downloading,
            .running(.install),
            .wizard(wizard),
            .cancelling,
            .succeeded(service),
        ]
        for state in active {
            XCTAssertFalse(
                NativeInstallOffer.isOffered(discovery: .notDetected, install: state),
                "Install was offered while the state was \(state)"
            )
        }
    }

    func testServiceReadyConnectsToTheServiceOriginAndRemembersIt() throws {
        let probe = StubProbe(responds: true)
        let view = makeView(probe: probe)

        view.handleInstallState(.succeeded(try origin("http://127.0.0.1:8444")))

        XCTAssertEqual(probe.checked.map(\.canonicalKey), ["http://127.0.0.1:8444"])
        XCTAssertEqual(store.savedOrigin?.canonicalKey, "http://127.0.0.1:8444")
    }

    func testNothingButServiceReadyConnects() throws {
        let states: [NativeInstallState] = [
            .wizard(try origin("http://127.0.0.1:8443")),
            .running(.activation),
            .cancelling,
            .failed(.installer(.activationRefused)),
            .failed(.checksumMismatch),
        ]
        for state in states {
            let probe = StubProbe(responds: true)
            let view = makeView(probe: probe)

            view.handleInstallState(state)

            XCTAssertTrue(probe.checked.isEmpty, "\(state) started a connection")
            XCTAssertNil(store.savedOrigin, "\(state) was remembered")
        }
    }

    func testAnUnverifiedServiceAfterInstallIsNotRemembered() throws {
        let probe = StubProbe(responds: false)
        let view = makeView(probe: probe)

        view.handleInstallState(.succeeded(try origin("http://127.0.0.1:8444")))

        XCTAssertEqual(probe.checked.count, 1)
        XCTAssertNil(store.savedOrigin, "An unverified service after install was remembered")
    }

    func testAfterInstallFailureCopyNamesTheServiceOrigin() throws {
        let service = try origin("http://127.0.0.1:8444")
        let text = NativeInstallFlowCopy.connectFailedAfterInstall(service)

        XCTAssertTrue(text.contains("http://127.0.0.1:8444"), "The copy does not name the service origin: \(text)")
        XCTAssertNotEqual(
            text,
            "Could not verify the server’s startup response. Check the address and try again.",
            "The after-install failure must not reuse the ordinary failure text"
        )
    }

    func testInstallIsOfferedAfterARepairOnlyWhenNothingIsInstalled() {
        let discoveries: [LocalServerDiscovery.Status] = [
            .idle,
            .notDetected,
            .installedNotRunning(version: nil),
            .running(version: nil, ready: true, onThisMac: true),
        ]
        for status in discoveries {
            XCTAssertTrue(
                NativeInstallOffer.isOffered(
                    discovery: status, install: .failed(.installer(.notInstalled)), mode: .repair
                ),
                "Install was not offered after a repair reported not_installed while discovery was \(status)"
            )
            XCTAssertFalse(
                NativeInstallOffer.isOffered(
                    discovery: status, install: .failed(.installer(.serviceNotReady)), mode: .repair
                ),
                "Install was offered after a failed repair while discovery was \(status)"
            )
            XCTAssertFalse(
                NativeInstallOffer.isOffered(
                    discovery: status, install: .failed(.repairUnavailable), mode: .repair
                ),
                "Install was offered after a repair that could not run while discovery was \(status)"
            )
        }
        XCTAssertTrue(
            NativeInstallOffer.isOffered(discovery: .notDetected, install: .idle, mode: .repair),
            "The idle install offer must not depend on the mode"
        )
        XCTAssertTrue(
            NativeInstallOffer.isOffered(discovery: .idle, install: .failed(.installer(.serviceNotReady))),
            "A failed install must still be retried without a mode argument"
        )
    }

    func testRepairIsOfferedForAStuckOrFailedLocalInstallation() throws {
        let service = try origin("http://127.0.0.1:8443")
        let rows: [(LocalServerDiscovery.Status, NativeInstallState, Bool, Bool, Bool)] = [
            // (discovery, install state, update failed, installation detected, offered)
            (.installedNotRunning(version: "1.8.1"), .idle, false, true, true),
            (.startFailed(version: nil), .idle, false, true, true),
            (.running(version: "1.8.1", ready: true, onThisMac: true), .idle, true, true, true),
            (.installedNotRunning(version: nil), .failed(.installer(.serviceNotReady)), false, true, true),
            (.startFailed(version: nil), .failed(.repairUnavailable), false, true, true),
            (.installedNotRunning(version: nil), .failed(.installer(.existingInstall)), false, true, true),

            (.installedNotRunning(version: nil), .idle, false, false, false),
            (.running(version: "1.8.1", ready: true, onThisMac: true), .idle, false, true, false),
            (.notDetected, .idle, true, false, false),
            (.idle, .idle, false, true, false),
            (.checking, .idle, false, true, false),
            (.starting, .idle, false, true, false),
            (.installedNotRunning(version: nil), .failed(.installer(.notInstalled)), true, true, false),
            (.installedNotRunning(version: nil), .succeeded(service), true, true, false),
        ]
        for (discovery, state, updateFailed, hasInstallation, expected) in rows {
            XCTAssertEqual(
                NativeRepairOffer.isOffered(
                    discovery: discovery, installState: state,
                    updateFailed: updateFailed, hasInstallation: hasInstallation
                ),
                expected,
                "Repair offer was wrong for discovery \(discovery), state \(state), updateFailed \(updateFailed), installation \(hasInstallation)"
            )
        }

        let active: [NativeInstallState] = [.downloading, .verifying, .running(nil), .running(.install), .cancelling]
        for state in active {
            XCTAssertFalse(
                NativeRepairOffer.isOffered(
                    discovery: .installedNotRunning(version: nil), installState: state,
                    updateFailed: true, hasInstallation: true
                ),
                "Repair was offered while the state was \(state)"
            )
        }
    }

    func testRepairCopy() throws {
        XCTAssertEqual(NativeInstallFlowCopy.repairButton, "Repair Ciaobot")
        XCTAssertTrue(NativeInstallFlowCopy.repairHint.contains("workspace"),
                      "The repair hint must say the workspace is not changed")
        XCTAssertNotEqual(NativeInstallFlowCopy.cancelRepairButton, NativeInstallFlowCopy.cancelButton)
        XCTAssertFalse(NativeInstallFlowCopy.cancelRepairHint.contains("keeps running"),
                       "Cancelling a repair must not promise that a service keeps running")

        let service = try origin("http://127.0.0.1:8444")
        let text = NativeInstallFlowCopy.connectFailedAfterRepair(service)
        XCTAssertTrue(text.contains("http://127.0.0.1:8444"), "The copy does not name the service origin: \(text)")
        XCTAssertTrue(text.contains("repaired"), "The copy does not say the service was repaired: \(text)")
        XCTAssertNotEqual(text, NativeInstallFlowCopy.connectFailedAfterInstall(service))
    }

    func testTechnicalReferenceDocumentsTheCurrentInstallerPin() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let reference = try String(
            contentsOf: packageRoot.appendingPathComponent("docs/reference.md"),
            encoding: .utf8
        )

        XCTAssertTrue(reference.contains(NativeInstallPin.current.sha256),
                      "Technical reference does not state the pinned installer digest")
        XCTAssertTrue(reference.contains("v\(NativeInstallPin.current.version)"),
                      "Technical reference does not name the pinned release tag")
        XCTAssertTrue(reference.contains("--native-install --events-fd 3"),
                      "Technical reference does not document the native install command")
        XCTAssertTrue(reference.contains("pick another port"),
                      "Technical reference does not explain the port failures")
        XCTAssertTrue(reference.contains(EngineRelease.publicKey),
                      "Technical reference does not state the engine release key")
        XCTAssertFalse(reference.contains("It does not install, manage, restart, or replace the server"),
                       "Technical reference still says the app never installs anything")
    }

    func testTechnicalReferenceDocumentsNativeRepair() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let reference = try String(
            contentsOf: packageRoot.appendingPathComponent("docs/reference.md"),
            encoding: .utf8
        )

        XCTAssertTrue(reference.contains("## Repairing Ciaobot on this Mac"), "Technical reference has no repair section")
        XCTAssertTrue(reference.contains("--native-repair --events-fd 3"), "Technical reference does not document the repair command")
        XCTAssertTrue(reference.contains("v\(EngineRelease.minimumRepairVersion)"),
                      "Technical reference does not name the minimum repair release")
        XCTAssertTrue(reference.contains("Repair never touches the workspace"),
                      "Technical reference does not state that repair leaves the workspace alone")
        XCTAssertTrue(reference.contains("not_installed"), "Technical reference does not explain the not_installed outcome")
        XCTAssertTrue(reference.contains("Native repair is tested with fake installer scripts"),
                      "Technical reference does not state the native repair test boundary")
        XCTAssertFalse(reference.contains("it never repairs, restarts, or replaces an existing server"),
                       "Technical reference still says the app never repairs")
    }
}
