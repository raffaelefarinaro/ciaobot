import XCTest
import Foundation

@testable import CiaobotMac

/// The menu's engine-update rows and its monitor. Every probe, timer, command
/// and installation read is injected, so no test runs a real `ciao`, reads the
/// home directory, contacts an address, or creates a status item.
@MainActor
final class EngineUpdateMenuTests: XCTestCase {
    private let executable = URL(fileURLWithPath: "/Users/me/.local/bin/ciao")
    private let local = Origin("http://127.0.0.1:8443")!
    private var installation: LocalInstallation? = LocalInstallation(
        hasService: true, installedVersion: "1.7.0", port: 8443,
        executable: URL(fileURLWithPath: "/Users/me/.local/bin/ciao")
    )
    private var probes: [Origin] = []
    private var probeCompletions: [(StartupInfo?) -> Void] = []
    private var clock = Date(timeIntervalSince1970: 1_800_000_000)
    private var calls: [[String]] = []
    private var completions: [(EngineCommandResult?) -> Void] = []
    private let status = AppSessionStatus()
    private let ticker = MonitorFakeTicker()
    private var updater: EngineUpdater!
    private var monitor: EngineUpdateMonitor!

    private func info(
        version: String? = "1.7.0", latest: String? = "1.8.0", available: Bool = true, pid: Int32? = 4242
    ) -> StartupInfo {
        StartupInfo(version: version, ready: true, pid: pid, latestVersion: latest, updateAvailable: available)
    }

    /// A status-0 result whose stdout is one engine-shaped record.
    private func record(_ phase: String, id: String = "op-1", to: String = "1.8.0", error: String = "") -> EngineCommandResult {
        let object: [String: Any] = [
            "id": id, "phase": phase, "from_version": "1.7.0", "to_version": to, "error": error,
        ]
        let data = try! JSONSerialization.data(withJSONObject: object)
        return EngineCommandResult(status: 0, stdout: data, stderr: Data())
    }

    private func failure(_ status: Int32, stderr: String) -> EngineCommandResult {
        EngineCommandResult(status: status, stdout: Data(), stderr: Data(stderr.utf8))
    }

    /// Builds the monitor over the test's fakes and starts it. Nothing is
    /// probed until a test connects a session.
    private func makeMonitor(processExists: Bool = true) {
        updater = EngineUpdater(
            run: { [weak self] _, arguments, _, completion in
                guard let self else { return }
                self.calls.append(arguments)
                self.completions.append(completion)
            },
            after: { _, _ in },
            isExecutable: { _ in true }
        )
        monitor = EngineUpdateMonitor(
            status: status,
            updater: updater,
            detectInstallation: { [unowned self] in self.installation },
            processExists: { _ in processExists },
            transport: { [weak self] origin, completion in
                self?.probes.append(origin)
                self?.probeCompletions.append(completion)
            },
            ticker: ticker,
            now: { [unowned self] in self.clock }
        )
        monitor.start()
    }

    // MARK: - Rows and copy

    func testIdleWithoutATargetHasNoRows() {
        XCTAssertEqual(EngineUpdateMenuCopy.rows(state: .idle, target: nil), [])
    }

    func testIdleWithATargetOffersTheUpdate() {
        let target = EngineUpdateTarget(executable: executable, version: "1.8.0")

        XCTAssertEqual(
            EngineUpdateMenuCopy.rows(state: .idle, target: target),
            [.action(title: "Update Ciaobot to 1.8.0…", target: target)]
        )
    }

    func testActiveStatesShowOnlyProgress() {
        let target = EngineUpdateTarget(executable: executable, version: "1.8.0")

        XCTAssertEqual(
            EngineUpdateMenuCopy.rows(state: .staging(version: "1.8.0"), target: target),
            [.progress("Downloading and verifying Ciaobot 1.8.0…")]
        )
        XCTAssertEqual(
            EngineUpdateMenuCopy.rows(state: .applying(version: "1.8.0", phase: "swapping"), target: target),
            [.progress("Installing Ciaobot 1.8.0…")]
        )
    }

    func testAFailureShowsTheMessageAndARetry() {
        let target = EngineUpdateTarget(executable: executable, version: "1.8.0")

        XCTAssertEqual(
            EngineUpdateMenuCopy.rows(state: .failed(message: "boom"), target: target),
            [
                .outcome(title: "boom", toolTip: "boom", failed: true),
                .action(title: "Try Updating Ciaobot to 1.8.0 Again…", target: target),
            ]
        )
        XCTAssertEqual(
            EngineUpdateMenuCopy.rows(state: .failed(message: "boom"), target: nil),
            [.outcome(title: "boom", toolTip: "boom", failed: true)]
        )
    }

    func testALongFailureIsClippedInTheTitleAndKeptInTheTooltip() {
        let message = String(repeating: "x", count: 400)

        let rows = EngineUpdateMenuCopy.rows(state: .failed(message: message), target: nil)

        guard case let .outcome(title, toolTip, failed)? = rows.first else {
            return XCTFail("A failure must show an outcome row first.")
        }
        XCTAssertEqual(title.count, EngineUpdateMenuCopy.maximumTitleLength)
        XCTAssertTrue(title.hasSuffix("…"))
        XCTAssertEqual(toolTip, message)
        XCTAssertTrue(failed)
    }

    func testSuccessShowsTheOutcome() {
        XCTAssertEqual(
            EngineUpdateMenuCopy.rows(state: .succeeded(version: "1.8.0"), target: nil),
            [.outcome(title: "Ciaobot was updated to 1.8.0.", toolTip: "Ciaobot was updated to 1.8.0.", failed: false)]
        )
    }

    // MARK: - Monitor: probing and offers

    func testConnectingToTheLocalEngineProbesItAndOffersTheUpdate() {
        makeMonitor()

        status.reportConnected(local)
        XCTAssertEqual(probes, [local])

        probeCompletions[0](info())
        let target = EngineUpdateTarget(executable: executable, version: "1.8.0")
        XCTAssertEqual(monitor.target, target)
        XCTAssertEqual(monitor.rows, [.action(title: "Update Ciaobot to 1.8.0…", target: target)])
    }

    func testARemoteServerIsNeverProbedAndOffersNothing() {
        makeMonitor()

        status.reportConnected(Origin("https://example.com")!)
        XCTAssertTrue(probes.isEmpty)
        XCTAssertFalse(ticker.isRunning)
        XCTAssertEqual(monitor.rows, [])

        monitor.refreshIfNeeded()
        XCTAssertTrue(probes.isEmpty)
    }

    func testAProcessNotOnThisMacOffersNothing() {
        makeMonitor(processExists: false)

        status.reportConnected(local)
        probeCompletions[0](info())

        XCTAssertNil(monitor.target)
    }

    func testAResponseWithoutAPidOffersNothing() {
        makeMonitor()

        status.reportConnected(local)
        probeCompletions[0](info(pid: nil))

        XCTAssertNil(monitor.target)
    }

    func testAnotherLoopbackPortThanTheInstallationOffersNothing() {
        makeMonitor()

        status.reportConnected(Origin("http://127.0.0.1:9443")!)
        XCTAssertEqual(probes.count, 1)
        probeCompletions[0](info())

        XCTAssertNil(monitor.target)
    }

    func testNoExecutableInTheReceiptOffersNothing() {
        makeMonitor()
        installation?.executable = nil

        status.reportConnected(local)
        probeCompletions[0](info())

        XCTAssertNil(monitor.target)
    }

    func testNoNewerVersionOffersNothing() {
        makeMonitor()

        status.reportConnected(local)
        probeCompletions[0](info(latest: "1.7.0"))
        XCTAssertNil(monitor.target)

        status.reportDisconnected()
        status.reportConnected(local)
        probeCompletions[1](info(available: false))
        XCTAssertNil(monitor.target)
    }

    func testAFailedProbeClearsAnEarlierOffer() {
        makeMonitor()
        status.reportConnected(local)
        probeCompletions[0](info())
        XCTAssertNotNil(monitor.target)

        ticker.fire()
        XCTAssertEqual(probes.count, 2)
        probeCompletions[1](nil)

        XCTAssertNil(monitor.target)
    }

    func testThePeriodicProbeRunsEvery30MinutesWhileConnected() {
        makeMonitor()

        status.reportConnected(local)
        XCTAssertTrue(ticker.isRunning)
        XCTAssertEqual(ticker.interval, EngineUpdateMonitor.periodicInterval)
        XCTAssertEqual(EngineUpdateMonitor.periodicInterval, 1800)

        ticker.fire()
        XCTAssertEqual(probes.count, 2)
    }

    func testDisconnectingStopsTheTickerAndClearsTheOffer() {
        makeMonitor()
        status.reportConnected(local)
        probeCompletions[0](info())
        XCTAssertNotNil(monitor.target)

        status.reportDisconnected()

        XCTAssertFalse(ticker.isRunning)
        XCTAssertNil(monitor.target)
        ticker.fire()
        XCTAssertEqual(probes.count, 1)
    }

    func testMenuOpenRefreshIsThrottledToOncePerMinute() {
        makeMonitor()
        status.reportConnected(local)
        XCTAssertEqual(probes.count, 1)

        clock += 59
        monitor.refreshIfNeeded()
        XCTAssertEqual(probes.count, 1)

        clock += 1
        monitor.refreshIfNeeded()
        XCTAssertEqual(probes.count, 2)
    }

    func testMenuOpenWhileDisconnectedDoesNotProbe() {
        makeMonitor()

        monitor.refreshIfNeeded()

        XCTAssertTrue(probes.isEmpty)
    }

    func testALateAnswerFromAnEarlierConnectionIsIgnored() {
        makeMonitor()
        status.reportConnected(local)
        status.reportDisconnected()
        status.reportConnected(local)
        XCTAssertEqual(probes.count, 2)

        probeCompletions[0](info())
        XCTAssertNil(monitor.target)

        probeCompletions[1](info())
        XCTAssertNotNil(monitor.target)
    }

    // MARK: - Monitor: the click and its outcome

    func testPerformUpdateRunsTheSharedUpdaterOnce() {
        makeMonitor()
        status.reportConnected(local)
        probeCompletions[0](info())

        monitor.performUpdate()
        monitor.performUpdate()

        XCTAssertEqual(calls, [["update", "stage", "--version=1.8.0", "--json"]])
        XCTAssertEqual(monitor.rows, [.progress("Downloading and verifying Ciaobot 1.8.0…")])
    }

    func testPerformUpdateWithoutATargetDoesNothing() {
        makeMonitor()
        status.reportConnected(local)

        monitor.performUpdate()

        XCTAssertTrue(calls.isEmpty)
    }

    func testAppliedReprobesAndTheItemDisappears() {
        makeMonitor()
        status.reportConnected(local)
        probeCompletions[0](info())
        monitor.performUpdate()

        completions[0](record("staged"))
        completions[1](record("applied", to: "1.8.0"))

        XCTAssertEqual(updater.state, .succeeded(version: "1.8.0"))
        XCTAssertNil(monitor.target)
        XCTAssertEqual(probes.count, 2)

        installation?.installedVersion = "1.8.0"
        probeCompletions[1](info(version: "1.8.0", latest: "1.8.0", available: false))

        XCTAssertNil(monitor.target)
        XCTAssertEqual(
            monitor.rows,
            [.outcome(title: "Ciaobot was updated to 1.8.0.", toolTip: "Ciaobot was updated to 1.8.0.", failed: false)]
        )

        monitor.acknowledgeOutcome()
        XCTAssertEqual(monitor.rows, [])
    }

    func testAStageFailureShowsTheEngineTextAndARetry() {
        makeMonitor()
        status.reportConnected(local)
        probeCompletions[0](info())
        monitor.performUpdate()

        completions[0](failure(1, stderr: "Error: uv not found\n"))

        let message = EngineUpdateCopy.stageFailed(version: "1.8.0", detail: "uv not found")
        let target = EngineUpdateTarget(executable: executable, version: "1.8.0")
        XCTAssertEqual(
            monitor.rows,
            [
                .outcome(title: message, toolTip: message, failed: true),
                .action(title: "Try Updating Ciaobot to 1.8.0 Again…", target: target),
            ]
        )
    }

    func testStopIgnoresALateAnswerAndStopsTheTicker() {
        makeMonitor()
        status.reportConnected(local)

        monitor.stop()
        XCTAssertFalse(ticker.isRunning)

        probeCompletions[0](info())
        XCTAssertNil(monitor.target)
    }
}

/// A ticker the test advances by hand. Nothing is scheduled on a run loop, and
/// `fire()` after `stop()` does nothing.
@MainActor
private final class MonitorFakeTicker: MenuTicking {
    private(set) var isRunning = false
    private(set) var intervals: [TimeInterval] = []
    private var tick: (() -> Void)?

    var interval: TimeInterval? { intervals.last }

    func start(interval: TimeInterval, tick: @escaping () -> Void) {
        isRunning = true
        intervals.append(interval)
        self.tick = tick
    }

    func stop() {
        isRunning = false
        tick = nil
    }

    func fire() {
        guard isRunning else { return }
        tick?()
    }
}
