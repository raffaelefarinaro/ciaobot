import XCTest
import ServiceManagement

@testable import CiaobotMac

/// A signing-team probe double. No test ever inspects real code signing.
private struct FakeSigningTeam: SigningTeamReading {
    let teamIdentifier: String?

    init(_ teamIdentifier: String?) {
        self.teamIdentifier = teamIdentifier
    }
}

/// The `SMAppService.mainApp` double. It records status reads, registrations,
/// and stops, so a test can prove that refresh and every diagnostic path leave
/// this Mac's login items alone.
@MainActor
private final class FakeLoginItemService: LoginItemServicing {
    private var currentStatus: SMAppService.Status
    private(set) var statusReads = 0
    private(set) var registerCount = 0
    private(set) var unregisterCount = 0
    var registerError: Error?
    var unregisterError: Error?
    var statusAfterRegister: SMAppService.Status?
    var statusAfterUnregister: SMAppService.Status?

    init(_ status: SMAppService.Status = .notRegistered) {
        currentStatus = status
    }

    var status: SMAppService.Status {
        statusReads += 1
        return currentStatus
    }

    func register() throws {
        registerCount += 1
        if let registerError { throw registerError }
        if let statusAfterRegister { currentStatus = statusAfterRegister }
    }

    func unregister() throws {
        unregisterCount += 1
        if let unregisterError { throw unregisterError }
        if let statusAfterUnregister { currentStatus = statusAfterUnregister }
    }
}

/// The cause `SystemLoginItem` picks from facts the app can read. Nothing here
/// reads real code signing, registers a login item, or opens a window.
@MainActor
final class LoginItemTests: XCTestCase {
    private let installedApp = "/Applications/Ciaobot.app"

    private func makeLoginItem(
        bundleIdentifier: String? = "com.ciaobot.mac",
        bundleURLPath: String = "/Applications/Ciaobot.app",
        team: String? = "ABCDE12345",
        service: FakeLoginItemService? = nil
    ) -> SystemLoginItem {
        SystemLoginItem(
            bundleIdentifier: bundleIdentifier,
            bundleURLPath: bundleURLPath,
            signingTeam: FakeSigningTeam(team),
            service: service ?? FakeLoginItemService()
        )
    }

    // MARK: - The two real causes

    func testBundleOutsideApplicationsReportsNotInApplications() {
        let service = FakeLoginItemService(.notRegistered)
        let loginItem = makeLoginItem(
            bundleURLPath: "/Users/someone/Downloads/Ciaobot.app",
            service: service
        )

        guard case .unavailable(let unavailability) = loginItem.state else {
            return XCTFail("A bundle outside Applications cannot be a login item.")
        }
        XCTAssertEqual(unavailability.cause, .notInApplications)
        XCTAssertEqual(
            unavailability.reason,
            LoginItemUnavailableCopy.explanation(for: .notInApplications)
        )
        XCTAssertNotEqual(loginItem.state, .enabled)
        XCTAssertEqual(service.statusReads, 0, "An unavailable copy never asks macOS for a status.")
        XCTAssertEqual(service.registerCount, 0)
    }

    func testDownloadedTranslocatedCopyIsNotInApplications() {
        let loginItem = makeLoginItem(
            bundleURLPath: "/private/var/folders/x9/AppTranslocation/1F2E/d/Ciaobot.app"
        )

        guard case .unavailable(let unavailability) = loginItem.state else {
            return XCTFail("A translocated copy is not an installed app.")
        }
        XCTAssertEqual(unavailability.cause, .notInApplications)
    }

    func testBundleInApplicationsWithoutATeamIdentifierReportsUnsignedOrAdHoc() {
        for team in [nil, ""] {
            let service = FakeLoginItemService(.notRegistered)
            let loginItem = makeLoginItem(
                bundleURLPath: installedApp,
                team: team,
                service: service
            )

            guard case .unavailable(let unavailability) = loginItem.state else {
                return XCTFail("An ad-hoc build cannot be a login item (team: \(String(describing: team))).")
            }
            XCTAssertEqual(unavailability.cause, .unsignedOrAdHoc)
            XCTAssertEqual(
                unavailability.reason,
                LoginItemUnavailableCopy.explanation(for: .unsignedOrAdHoc)
            )
            XCTAssertEqual(service.statusReads, 0)
            XCTAssertEqual(service.registerCount, 0)
        }
    }

    func testNeverRegisteredInstalledAppReportingNotFoundCanStillBeRegistered() throws {
        // `SMAppService.mainApp` reports `.notFound` for an installed, signed
        // app until it is registered once. That must stay an offer, not a dead end.
        let service = FakeLoginItemService(.notFound)
        service.statusAfterRegister = .enabled
        let loginItem = makeLoginItem(service: service)

        XCTAssertEqual(loginItem.state, .notRegistered)
        XCTAssertEqual(service.registerCount, 0, "Reading the status never registers.")

        try loginItem.register()

        XCTAssertEqual(service.registerCount, 1)
        XCTAssertEqual(loginItem.state, .enabled)
    }

    func testRefusedRegistrationFromNotFoundIsAFailedRegister() {
        let service = FakeLoginItemService(.notFound)
        let error = NSError(domain: "SMAppServiceErrorDomain", code: 1)
        service.registerError = error
        let loginItem = makeLoginItem(service: service)

        XCTAssertThrowsError(try loginItem.register())

        XCTAssertEqual(
            loginItem.state,
            .failed(operation: .register, reason: SystemLoginItem.message(for: error))
        )
    }

    func testBareExecutableKeepsTheFixedReasonAndNeverAsksMacOS() throws {
        let service = FakeLoginItemService(.enabled)
        let loginItem = makeLoginItem(
            bundleIdentifier: nil,
            bundleURLPath: "/Users/someone/ciaobot-mac/.build/debug/CiaobotMac",
            service: service
        )

        guard case .unavailable(let unavailability) = loginItem.state else {
            return XCTFail("A bare executable cannot be a login item.")
        }
        XCTAssertEqual(unavailability.cause, .bareExecutable)
        XCTAssertEqual(unavailability.reason, SystemLoginItem.bareExecutableReason)

        loginItem.refresh()
        XCTAssertEqual(service.statusReads, 0, "It must not even ask ServiceManagement.")

        XCTAssertThrowsError(try loginItem.register())
        XCTAssertEqual(service.registerCount, 0, "A bare executable can never reach registration.")
    }

    func testAnEmptyBundleIdentifierIsABareExecutable() {
        let loginItem = makeLoginItem(bundleIdentifier: "")

        guard case .unavailable(let unavailability) = loginItem.state else {
            return XCTFail("A process without a bundle identifier is not an app.")
        }
        XCTAssertEqual(unavailability.cause, .bareExecutable)
    }

    // MARK: - The normal path still works

    func testNotarizedBuildInApplicationsReachesTheNormalRegisterPath() throws {
        let service = FakeLoginItemService(.notRegistered)
        service.statusAfterRegister = .requiresApproval
        let loginItem = makeLoginItem(service: service)

        XCTAssertEqual(loginItem.state, .notRegistered)
        XCTAssertEqual(service.registerCount, 0, "Even a usable copy registers only on an explicit call.")

        try loginItem.register()

        XCTAssertEqual(service.registerCount, 1)
        XCTAssertEqual(loginItem.state, .requiresApproval, "macOS approval is not success.")
        XCTAssertNotEqual(loginItem.state, .enabled)
    }

    func testInstalledSignedBundleMapsEveryUsableStatusTruthfully() {
        for (status, expected) in [
            (SMAppService.Status.enabled, LoginItemState.enabled),
            (.requiresApproval, .requiresApproval),
            (.notRegistered, .notRegistered),
        ] {
            let loginItem = makeLoginItem(service: FakeLoginItemService(status))
            XCTAssertEqual(loginItem.state, expected)
        }
    }

    func testAnEnabledLoginItemStopsOnlyWhenExplicitlyAsked() throws {
        let service = FakeLoginItemService(.enabled)
        service.statusAfterUnregister = .notRegistered
        let loginItem = makeLoginItem(service: service)

        XCTAssertEqual(loginItem.state, .enabled)
        XCTAssertEqual(service.unregisterCount, 0)

        try loginItem.unregister()

        XCTAssertEqual(service.unregisterCount, 1)
        XCTAssertEqual(loginItem.state, .notRegistered)
    }

    func testFailedRegistrationKeepsTheExactReasonAndStaysRetryable() {
        let service = FakeLoginItemService(.notRegistered)
        let error = NSError(
            domain: "CiaobotTests",
            code: 1,
            userInfo: [NSLocalizedDescriptionKey: "Operation not permitted"]
        )
        service.registerError = error
        let loginItem = makeLoginItem(service: service)

        XCTAssertThrowsError(try loginItem.register())

        XCTAssertEqual(
            loginItem.state,
            .failed(operation: .register, reason: SystemLoginItem.message(for: error))
        )
        XCTAssertEqual(service.registerCount, 1)
    }

    // MARK: - Refresh is read-only

    func testRefreshNeverRegistersOrUnregistersAnything() {
        for status in [
            SMAppService.Status.enabled,
            .requiresApproval,
            .notRegistered,
            .notFound,
        ] {
            let service = FakeLoginItemService(status)
            let loginItem = makeLoginItem(service: service)
            let readsAfterInit = service.statusReads

            loginItem.refresh()
            loginItem.refresh()

            XCTAssertEqual(service.registerCount, 0, "Refresh is a read, not an opt-in (\(status)).")
            XCTAssertEqual(service.unregisterCount, 0, "Refresh never turns Start at Login off (\(status)).")
            XCTAssertEqual(service.statusReads, readsAfterInit + 2)
        }
    }

    func testAnUnavailableCopyRefreshesToTheSameCauseWithoutRegistering() {
        let service = FakeLoginItemService(.enabled)
        let loginItem = makeLoginItem(bundleURLPath: "/Users/someone/Downloads/Ciaobot.app", service: service)
        let first = loginItem.state

        loginItem.refresh()

        XCTAssertEqual(loginItem.state, first)
        XCTAssertEqual(service.statusReads, 0)
        XCTAssertEqual(service.registerCount, 0)
        XCTAssertNotEqual(loginItem.state, .enabled)
    }
}
