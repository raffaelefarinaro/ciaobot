import XCTest
import Foundation

@testable import CiaobotMac

/// Same-Mac discovery finds an installed engine, asks its loopback address for
/// the running version, and offers it on the connection screen. It never
/// connects or signs in by itself. Every test injects the installation, the
/// process check, and the transport, so nothing reads the operator's files or
/// contacts a real address.
@MainActor
final class LocalServerDiscoveryTests: XCTestCase {
    private var requests: [Origin] = []
    private var completions: [(StartupInfo?) -> Void] = []
    private var starts: [(Bool) -> Void] = []
    private var delayed: [() -> Void] = []

    private func makeDiscovery(
        candidate: String? = nil,
        installation: LocalInstallation? = nil,
        processExists: @escaping (Int32) -> Bool = { _ in true }
    ) -> LocalServerDiscovery {
        LocalServerDiscovery(
            candidate: candidate.flatMap(Origin.init),
            detectInstallation: { installation },
            processExists: processExists,
            startService: { [weak self] _, completion in self?.starts.append(completion) },
            after: { [weak self] _, work in self?.delayed.append(work) }
        ) { [weak self] origin, completion in
            guard let self else { return }
            self.requests.append(origin)
            self.completions.append(completion)
        }
    }

    private let running = StartupInfo(version: "1.7.0", ready: true, pid: 4242)

    func testDefaultCandidateIsTheLoopbackDefaultPort() {
        XCTAssertEqual(LocalServerCandidate.origin?.canonicalKey, "http://127.0.0.1:8443")
        XCTAssertTrue(LocalServerCandidate.origin?.isLoopback == true)
    }

    func testWithoutAnInstallationTheDefaultAddressIsProbedOnce() {
        let discovery = makeDiscovery()
        XCTAssertEqual(discovery.status, .idle)

        XCTAssertTrue(discovery.refresh())
        XCTAssertEqual(discovery.status, .checking)
        XCTAssertEqual(requests.map(\.canonicalKey), ["http://127.0.0.1:8443"])
    }

    func testTheInstallationsPortIsProbedInsteadOfTheDefault() {
        let discovery = makeDiscovery(installation: LocalInstallation(hasService: true, installedVersion: "1.4.0", port: 9100))

        XCTAssertTrue(discovery.refresh())
        XCTAssertEqual(requests.map(\.canonicalKey), ["http://127.0.0.1:9100"])
        XCTAssertEqual(discovery.address, "http://127.0.0.1:9100")
    }

    func testARunningLocalServerReportsItsVersionAndIsConnectable() {
        var askedPid: Int32?
        let discovery = makeDiscovery(processExists: { askedPid = $0; return true })

        XCTAssertTrue(discovery.refresh())
        completions[0](running)

        XCTAssertEqual(discovery.status, .running(version: "1.7.0", ready: true, onThisMac: true))
        XCTAssertEqual(askedPid, 4242)
        XCTAssertEqual(discovery.connectableOrigin?.canonicalKey, "http://127.0.0.1:8443")
        XCTAssertEqual(discovery.statusText, "Ciaobot 1.7.0 is running on this Mac at http://127.0.0.1:8443.")
    }

    func testAServerWhoseProcessIsNotHereIsNotCalledLocal() {
        let discovery = makeDiscovery(processExists: { _ in false })

        XCTAssertTrue(discovery.refresh())
        completions[0](running)

        XCTAssertEqual(discovery.status, .running(version: "1.7.0", ready: true, onThisMac: false))
        XCTAssertFalse(discovery.statusText.contains("running on this Mac"))
        XCTAssertNotNil(discovery.connectableOrigin, "A server that answered can still be connected to")
    }

    func testAResponseWithoutAPidIsNotCalledLocal() {
        let discovery = makeDiscovery()

        XCTAssertTrue(discovery.refresh())
        completions[0](StartupInfo(version: nil, ready: false, pid: nil))

        XCTAssertEqual(discovery.status, .running(version: nil, ready: false, onThisMac: false))
        XCTAssertTrue(discovery.statusText.contains("still starting"))
    }

    func testAnInstalledEngineThatDoesNotAnswerIsInstalledNotRunning() {
        let discovery = makeDiscovery(installation: LocalInstallation(hasService: true, installedVersion: "1.4.0", port: 8443))

        XCTAssertTrue(discovery.refresh())
        completions[0](nil)

        XCTAssertEqual(discovery.status, .installedNotRunning(version: "1.4.0"))
        XCTAssertNil(discovery.connectableOrigin)
        XCTAssertTrue(discovery.statusText.contains("Ciaobot 1.4.0 is installed on this Mac but not answering"))
    }

    private let service = LocalInstallation(hasService: true, installedVersion: "1.4.0", port: 8443)

    private func stoppedDiscovery() -> LocalServerDiscovery {
        let discovery = makeDiscovery(installation: service)
        XCTAssertTrue(discovery.refresh())
        completions[0](nil)
        return discovery
    }

    func testStartIsOfferedOnlyForAnInstalledServiceThatIsNotAnswering() {
        XCTAssertTrue(stoppedDiscovery().canStart)

        let receiptOnly = makeDiscovery(installation: LocalInstallation(hasService: false, installedVersion: "1.4.0", port: 8443))
        XCTAssertTrue(receiptOnly.refresh())
        completions[1](nil)
        XCTAssertFalse(receiptOnly.canStart, "Without a LaunchAgent there is nothing to start")

        let runningDiscovery = makeDiscovery(installation: service)
        XCTAssertTrue(runningDiscovery.refresh())
        completions[2](running)
        XCTAssertFalse(runningDiscovery.canStart)
        XCTAssertFalse(runningDiscovery.start())
        XCTAssertTrue(starts.isEmpty)
    }

    func testStartPollsUntilTheServerAnswers() {
        let discovery = stoppedDiscovery()

        XCTAssertTrue(discovery.start())
        XCTAssertEqual(discovery.status, .starting)
        XCTAssertFalse(discovery.start(), "A second start while one is in flight")
        XCTAssertEqual(starts.count, 1)

        starts[0](true)
        XCTAssertEqual(completions.count, 2, "A started service is probed")
        completions[1](nil)
        XCTAssertEqual(discovery.status, .starting)
        XCTAssertEqual(delayed.count, 1)

        delayed[0]()
        completions[2](running)
        XCTAssertEqual(discovery.status, .running(version: "1.7.0", ready: true, onThisMac: true))
        XCTAssertNotNil(discovery.connectableOrigin)
    }

    func testARefusedStartIsReported() {
        let discovery = stoppedDiscovery()

        XCTAssertTrue(discovery.start())
        starts[0](false)

        XCTAssertEqual(discovery.status, .startFailed(version: "1.4.0"))
        XCTAssertTrue(discovery.canStart, "The user can try again")
        XCTAssertTrue(discovery.statusText.contains("did not start"))
    }

    func testAServerThatNeverAnswersTimesOut() {
        let discovery = stoppedDiscovery()

        XCTAssertTrue(discovery.start())
        starts[0](true)
        let attempts = Int(LocalServerDiscovery.startTimeout / LocalServerDiscovery.startPollInterval)
        for index in 0...attempts {
            completions[index + 1](nil)
            if index < attempts { delayed[index]() }
        }

        XCTAssertEqual(discovery.status, .startFailed(version: "1.4.0"))
        XCTAssertEqual(delayed.count, attempts)
    }

    func testLeavingTheScreenStopsPollingAfterStart() {
        let discovery = stoppedDiscovery()

        XCTAssertTrue(discovery.start())
        starts[0](true)
        completions[1](nil)
        discovery.invalidate()
        delayed[0]()

        XCTAssertEqual(completions.count, 2, "Polling continued after the screen went away")
        XCTAssertEqual(discovery.status, .idle)
    }

    func testNothingInstalledAndNothingAnsweringIsNotDetected() {
        let discovery = makeDiscovery()

        XCTAssertTrue(discovery.refresh())
        completions[0](nil)

        XCTAssertEqual(discovery.status, .notDetected)
        XCTAssertNil(discovery.connectableOrigin)
    }

    func testCheckCannotStartTwiceWhileOneIsInFlight() {
        let discovery = makeDiscovery()

        XCTAssertTrue(discovery.refresh())
        XCTAssertFalse(discovery.refresh(), "A second check started while the first was in flight")
        XCTAssertEqual(requests.count, 1)
    }

    func testAResponseAfterTheConnectionScreenDisappearsIsIgnored() {
        let discovery = makeDiscovery()

        XCTAssertTrue(discovery.refresh())
        discovery.invalidate()
        completions[0](running)

        XCTAssertEqual(discovery.status, .idle)
        XCTAssertNil(discovery.connectableOrigin)
    }

    func testAnOlderResponseCannotOverwriteANewerCheck() {
        let discovery = makeDiscovery()

        XCTAssertTrue(discovery.refresh())
        discovery.invalidate()
        XCTAssertTrue(discovery.refresh())

        completions[0](running)
        XCTAssertEqual(discovery.status, .checking)

        completions[1](nil)
        XCTAssertEqual(discovery.status, .notDetected)
    }

    func testCopyNeverMentionsAStartupIdentifier() {
        let statuses: [LocalServerDiscovery.Status] = [
            .idle, .checking, .running(version: "1.0", ready: true, onThisMac: true),
            .running(version: nil, ready: false, onThisMac: false), .installedNotRunning(version: nil),
            .starting, .startFailed(version: nil), .notDetected,
        ]
        for status in statuses {
            let text = LocalServerDiscoveryCopy.text(for: status, address: "http://127.0.0.1:8443").lowercased()
            XCTAssertFalse(text.contains("startup id") || text.contains("startup_id"), "\(status) copy mentions a startup identifier")
        }
        XCTAssertTrue(LocalServerDiscoveryCopy.hint.contains("does not sign in"))
    }

    func testAutomaticDiscoveryRunsOnlyInProductionAndNotForTestLaunches() {
        XCTAssertTrue(LocalServerDiscoveryPolicy.automaticallyProbes(isDebugBuild: false, testLaunchRequested: false))
        XCTAssertFalse(LocalServerDiscoveryPolicy.automaticallyProbes(isDebugBuild: true, testLaunchRequested: false))
        XCTAssertFalse(LocalServerDiscoveryPolicy.automaticallyProbes(isDebugBuild: false, testLaunchRequested: true))
        XCTAssertFalse(LocalServerDiscoveryPolicy.automaticallyProbes(isDebugBuild: true, testLaunchRequested: true))
    }

    func testShippedDefaultTracksTheBuildConfiguration() {
        #if DEBUG
        XCTAssertFalse(LocalServerDiscoveryPolicy.automaticInThisBuild)
        #else
        XCTAssertTrue(LocalServerDiscoveryPolicy.automaticInThisBuild)
        #endif
    }

    func testShippedConnectionViewDefaultFollowsTheSamePolicy() throws {
        // The host store is isolated even though this view never connects, so
        // a future connect here cannot write the operator's real host list.
        let name = IsolatedDefaults.makeName("ShippedView")
        let defaults = try XCTUnwrap(UserDefaults(suiteName: name))
        defaults.removePersistentDomain(forName: name)
        defer { IsolatedDefaults.remove(name) }
        let view = ConnectionView(hostStore: UserDefaultsHostListStore(
            defaults: defaults,
            lastServer: UserDefaultsLastServerStore(defaults: defaults)
        ))

        XCTAssertEqual(view.automaticallyStartsDiscovery,
                       LocalServerDiscoveryPolicy.automaticInThisBuild)
    }

    private let executable = URL(fileURLWithPath: "/Users/me/.local/bin/ciao")

    private var updatableInstallation: LocalInstallation {
        LocalInstallation(hasService: true, installedVersion: "1.7.0", port: 8443, executable: executable)
    }

    private var newerReply: StartupInfo {
        StartupInfo(version: "1.7.0", ready: true, pid: 4242, latestVersion: "1.8.0", updateAvailable: true)
    }

    func testARunningLocalServerWithANewerVersionOffersTheUpdate() {
        let discovery = makeDiscovery(installation: updatableInstallation)

        XCTAssertTrue(discovery.refresh())
        completions[0](newerReply)

        XCTAssertEqual(discovery.availableVersion, "1.8.0")
        XCTAssertEqual(discovery.updateTarget, EngineUpdateTarget(executable: executable, version: "1.8.0"))
        XCTAssertEqual(discovery.statusText,
                       "Ciaobot 1.7.0 is running on this Mac at http://127.0.0.1:8443. Ciaobot 1.8.0 is available.")
    }

    func testAServerNotOnThisMacMentionsTheUpdateButDoesNotOfferIt() {
        let discovery = makeDiscovery(installation: updatableInstallation, processExists: { _ in false })

        XCTAssertTrue(discovery.refresh())
        completions[0](newerReply)

        XCTAssertNil(discovery.updateTarget)
        XCTAssertTrue(discovery.statusText.hasSuffix("Ciaobot 1.8.0 is available. Update it from its Settings."))
    }

    func testWithoutAReceiptExecutableNoUpdateIsOffered() {
        let installation = LocalInstallation(hasService: true, installedVersion: "1.7.0", port: 8443)
        let discovery = makeDiscovery(installation: installation)

        XCTAssertTrue(discovery.refresh())
        completions[0](newerReply)

        XCTAssertNil(discovery.updateTarget)
        XCTAssertTrue(discovery.statusText.hasSuffix("Update it from its Settings."))
    }

    func testAHintEqualToTheRunningVersionIsIgnored() {
        let discovery = makeDiscovery(installation: updatableInstallation)

        XCTAssertTrue(discovery.refresh())
        completions[0](StartupInfo(version: "1.7.0", ready: true, pid: 4242, latestVersion: "1.7.0", updateAvailable: true))

        XCTAssertNil(discovery.availableVersion)
        XCTAssertFalse(discovery.statusText.contains("available"))
    }

    func testTheHintIsClearedWhenTheScreenGoesAwayOrRechecks() {
        let discovery = makeDiscovery(installation: updatableInstallation)

        XCTAssertTrue(discovery.refresh())
        completions[0](newerReply)
        XCTAssertEqual(discovery.availableVersion, "1.8.0")

        discovery.invalidate()
        XCTAssertNil(discovery.availableVersion)

        XCTAssertTrue(discovery.refresh())
        completions[1](newerReply)
        XCTAssertEqual(discovery.availableVersion, "1.8.0")

        XCTAssertTrue(discovery.refresh())
        XCTAssertEqual(discovery.status, .checking)
        XCTAssertNil(discovery.availableVersion)
    }
}

/// Installation detection reads a fixture home directory, never the real one.
final class LocalInstallationTests: XCTestCase {
    private var home: URL!

    override func setUpWithError() throws {
        home = FileManager.default.temporaryDirectory.appendingPathComponent("ciao-home-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: home)
    }

    private func write(_ text: String, to path: String) throws {
        let url = home.appendingPathComponent(path)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(text.utf8).write(to: url)
    }

    private func writeService(environment: [String: String], workingDirectory: String? = nil) throws {
        var plist: [String: Any] = ["Label": "com.ciao.server", "EnvironmentVariables": environment]
        if let workingDirectory { plist["WorkingDirectory"] = workingDirectory }
        let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
        let url = home.appendingPathComponent("Library/LaunchAgents/com.ciao.server.plist")
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: url)
    }

    func testNothingInstalledIsNil() {
        XCTAssertNil(LocalInstallation.detect(home: home))
    }

    func testReceiptAloneGivesTheVersionAndDefaultPort() throws {
        try write(#"{"version":"1.4.0","executable":"/x/ciao"}"#, to: ".local/state/ciaobot/install-receipt.json")
        XCTAssertEqual(LocalInstallation.detect(home: home),
                       LocalInstallation(hasService: false, installedVersion: "1.4.0", port: 8443))
    }

    func testServicePortIsUsed() throws {
        try writeService(environment: ["CIAO_PORT": "9001"])
        let found = LocalInstallation.detect(home: home)
        XCTAssertEqual(found?.hasService, true)
        XCTAssertEqual(found?.port, 9001)
        XCTAssertEqual(found?.servicePlist?.lastPathComponent, "com.ciao.server.plist")
    }

    func testWorkspaceDotenvPortWinsLikeTheEngine() throws {
        let workspace = home.appendingPathComponent("ws").path
        try write("SECRET_KEY=hunter2\nexport PWA_PORT=\"9555\"\n# PWA_PORT=1\n", to: "ws/.env")
        try writeService(environment: ["CIAO_PORT": "9001", "CIAO_WORKSPACE": workspace])
        XCTAssertEqual(LocalInstallation.detect(home: home)?.port, 9555)
    }

    func testWorkingDirectoryIsTheWorkspaceFallback() throws {
        try write("PWA_PORT=9777\n", to: "ws/.env")
        try writeService(environment: [:], workingDirectory: home.appendingPathComponent("ws").path)
        XCTAssertEqual(LocalInstallation.detect(home: home)?.port, 9777)
    }

    func testTheWorkspaceComesFromTheServiceFiles() throws {
        let workspace = home.appendingPathComponent("ws").path
        try writeService(environment: ["CIAO_WORKSPACE": workspace])
        XCTAssertEqual(LocalInstallation.detect(home: home)?.workspace?.path, workspace)

        try writeService(environment: [:], workingDirectory: workspace)
        XCTAssertEqual(LocalInstallation.detect(home: home)?.workspace?.path, workspace)

        try writeService(environment: [:])
        XCTAssertNil(LocalInstallation.detect(home: home)?.workspace)
    }

    func testInvalidPortsFallBack() throws {
        try write("PWA_PORT=99999\n", to: "ws/.env")
        try writeService(environment: ["CIAO_PORT": "abc"], workingDirectory: home.appendingPathComponent("ws").path)
        XCTAssertEqual(LocalInstallation.detect(home: home)?.port, 8443)
    }

    func testCorruptFilesReadAsAbsent() throws {
        try write("not a plist", to: "Library/LaunchAgents/com.ciao.server.plist")
        try write("{", to: ".local/state/ciaobot/install-receipt.json")
        XCTAssertNil(LocalInstallation.detect(home: home))
    }

    func testAnExecutableReceiptEntryIsKept() throws {
        let binary = home.appendingPathComponent("bin/ciao")
        try write("#!/bin/sh\n", to: "bin/ciao")
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: binary.path)
        try write(#"{"version":"1.4.0","executable":"\#(binary.path)"}"#, to: ".local/state/ciaobot/install-receipt.json")
        XCTAssertEqual(LocalInstallation.detect(home: home)?.executable?.path, binary.path)
    }

    func testUnusableReceiptExecutablesAreDropped() throws {
        let plain = home.appendingPathComponent("bin/plain")
        try write("#!/bin/sh\n", to: "bin/plain")
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: plain.path)
        let directory = home.appendingPathComponent("bin").path
        let missing = home.appendingPathComponent("bin/gone").path
        for executable in [plain.path, "bin/ciao", directory, missing] {
            try write(#"{"version":"1.4.0","executable":"\#(executable)"}"#, to: ".local/state/ciaobot/install-receipt.json")
            XCTAssertNil(LocalInstallation.detect(home: home)?.executable, executable)
        }
    }
}

/// Start uses launchd's existing job and falls back to bootstrapping the
/// installer's own plist only when launchd has no such job.
final class LocalServiceStarterTests: XCTestCase {
    private let installation = LocalInstallation(
        hasService: true, installedVersion: nil, port: 8443,
        servicePlist: URL(fileURLWithPath: "/Users/me/Library/LaunchAgents/com.ciao.server.plist")
    )

    private func start(statuses: [Int32]) -> (calls: [[String]], result: Bool?) {
        var calls: [[String]] = []
        var remaining = statuses
        var result: Bool?
        LocalServiceStarter.start(installation, uid: 501, run: { arguments, completion in
            calls.append(arguments)
            completion(remaining.removeFirst())
        }) { result = $0 }
        return (calls, result)
    }

    func testARegisteredJobIsOnlyKickstarted() {
        let outcome = start(statuses: [0])
        XCTAssertEqual(outcome.calls, [["kickstart", "gui/501/com.ciao.server"]])
        XCTAssertEqual(outcome.result, true)
    }

    func testAMissingJobIsBootstrappedFromTheInstallersPlist() {
        let outcome = start(statuses: [113, 0, 0])
        XCTAssertEqual(outcome.calls, [
            ["kickstart", "gui/501/com.ciao.server"],
            ["bootstrap", "gui/501", "/Users/me/Library/LaunchAgents/com.ciao.server.plist"],
            ["kickstart", "gui/501/com.ciao.server"],
        ])
        XCTAssertEqual(outcome.result, true)
    }

    func testAFailedRetryReportsFailure() {
        XCTAssertEqual(start(statuses: [113, 5, 113]).result, false)
    }
}

/// The startup response parser keeps only display fields and drops a version
/// that is not a short version-like string.
final class StartupInfoParsingTests: XCTestCase {
    private func parse(_ body: String, status: Int = 200) -> StartupInfo? {
        Probe.startupInfo(status: status, data: Data(body.utf8))
    }

    func testVersionReadinessAndPidAreRead() {
        XCTAssertEqual(parse(#"{"startup_id":"a","version":"1.7.0","overall_ready":true,"pid":79236}"#),
                       StartupInfo(version: "1.7.0", ready: true, pid: 79236))
    }

    func testMissingFieldsAreNil() {
        XCTAssertEqual(parse(#"{"startup_id":"a"}"#), StartupInfo(version: nil, ready: false, pid: nil))
    }

    func testUnsafeVersionsAndPidsAreDropped() {
        for version in [#""""#, #""1.0 <b>""#, "\"\(String(repeating: "9", count: 41))\"", "7"] {
            XCTAssertNil(parse(#"{"startup_id":"a","version":\#(version)}"#)?.version, version)
        }
        for pid in ["0", "-3", "99999999999", #""12""#] {
            XCTAssertNil(parse(#"{"startup_id":"a","pid":\#(pid)}"#)?.pid, pid)
        }
    }

    func testNonASCIIVersionsAreDropped() {
        // U+0661 ARABIC-INDIC DIGIT ONE is alphanumeric but not ASCII.
        XCTAssertNil(parse("{\"startup_id\":\"a\",\"version\":\"1.8.\u{0661}\"}")?.version)
        XCTAssertNil(parse("{\"startup_id\":\"a\",\"latest_version\":\"1.8.\u{0661}\",\"update_available\":true}")?.latestVersion)
    }

    func testAnInvalidResponseIsNil() {
        XCTAssertNil(parse(#"{"version":"1.0"}"#))
        XCTAssertNil(parse(#"{"startup_id":"a"}"#, status: 401))
    }

    func testLatestVersionAndUpdateAvailableAreRead() {
        let info = parse(#"{"startup_id":"a","version":"1.7.0","latest_version":"1.8.0","update_available":true}"#)
        XCTAssertEqual(info?.latestVersion, "1.8.0")
        XCTAssertEqual(info?.updateAvailable, true)
    }

    func testAColdStartHasNoUpdateHint() {
        for body in [#"{"startup_id":"a","version":"1.7.0"}"#, #"{"startup_id":"a","latest_version":"","update_available":false}"#] {
            XCTAssertNil(parse(body)?.latestVersion, body)
            XCTAssertEqual(parse(body)?.updateAvailable, false, body)
        }
    }

    func testUnsafeLatestVersionAndNonBooleanFlagAreDropped() {
        let longVersion = String(repeating: "9", count: 41)
        for latest in [#""1.0 <b>""#, "\"\(longVersion)\"", "7"] {
            XCTAssertNil(parse(#"{"startup_id":"a","latest_version":\#(latest)}"#)?.latestVersion, latest)
        }
        for flag in ["1", #""true""#, "null"] {
            XCTAssertEqual(parse(#"{"startup_id":"a","update_available":\#(flag)}"#)?.updateAvailable, false, flag)
        }
    }
}
