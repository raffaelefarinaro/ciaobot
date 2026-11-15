import XCTest
import Foundation

@testable import CiaobotMac

/// The engine update runs `ciao update stage/apply/status` through an injected
/// runner and an injected delay, so every step is driven by hand. No test runs
/// a real `ciao`, reads the home directory, or contacts an address.
@MainActor
final class EngineUpdaterTests: XCTestCase {
    private let target = EngineUpdateTarget(
        executable: URL(fileURLWithPath: "/Users/me/.local/bin/ciao"), version: "1.8.0"
    )
    private var executables: [URL] = []
    private var calls: [[String]] = []
    private var completions: [(EngineCommandResult?) -> Void] = []
    private var delays: [TimeInterval] = []
    private var delayed: [() -> Void] = []

    private func makeUpdater(isExecutable: @escaping (URL) -> Bool = { _ in true }) -> EngineUpdater {
        EngineUpdater(
            run: { [weak self] executable, arguments, _, completion in
                guard let self else { return }
                self.executables.append(executable)
                self.calls.append(arguments)
                self.completions.append(completion)
            },
            after: { [weak self] delay, work in
                self?.delays.append(delay)
                self?.delayed.append(work)
            },
            isExecutable: isExecutable
        )
    }

    private func resetFakes() {
        executables = []
        calls = []
        completions = []
        delays = []
        delayed = []
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

    private func message(_ state: EngineUpdateState) -> String? {
        if case .failed(let message) = state { return message }
        return nil
    }

    func testStageRunsTheReceiptExecutableWithJSON() {
        let updater = makeUpdater()

        updater.start(target)

        XCTAssertEqual(updater.state, .staging(version: "1.8.0"))
        XCTAssertEqual(executables, [target.executable])
        XCTAssertEqual(calls, [["update", "stage", "--version=1.8.0", "--json"]])
    }

    func testAStagedRecordStartsApply() {
        let updater = makeUpdater()

        updater.start(target)
        completions[0](record("staged"))

        XCTAssertEqual(calls[1], ["update", "apply", "--json"])
        XCTAssertEqual(updater.state, .applying(version: "1.8.0", phase: "draining"))
    }

    func testApplyPollsStatusEveryTwoSecondsUntilApplied() {
        let updater = makeUpdater()

        updater.start(target)
        completions[0](record("staged"))
        completions[1](record("applying"))
        XCTAssertEqual(updater.state, .applying(version: "1.8.0", phase: "applying"))
        XCTAssertEqual(delays, [2])

        delayed[0]()
        XCTAssertEqual(calls.last, ["update", "status", "--json"])
        completions[2](record("swapping"))
        XCTAssertEqual(updater.state, .applying(version: "1.8.0", phase: "swapping"))
        XCTAssertEqual(delays, [2, 2])

        delayed[1]()
        completions[3](record("applied", to: "1.8.0"))
        XCTAssertEqual(updater.state, .succeeded(version: "1.8.0"))
        XCTAssertEqual(delays, [2, 2], "No further poll is scheduled after applied")
    }

    func testTransientPollFailuresKeepWaiting() {
        let updater = makeUpdater()
        let badPolls: [EngineCommandResult?] = [
            nil,
            failure(1, stderr: "boom"),
            EngineCommandResult(status: 0, stdout: Data("no update staged\n".utf8), stderr: Data()),
        ]

        updater.start(target)
        completions[0](record("staged"))
        completions[1](record("applying"))

        for poll in badPolls {
            delayed.removeFirst()()
            completions.last?(poll)
            XCTAssertEqual(updater.state, .applying(version: "1.8.0", phase: "applying"))
            XCTAssertEqual(delayed.count, 1, "Exactly one new poll is scheduled")
        }
    }

    func testARecordFromAnotherOperationIsIgnored() {
        let updater = makeUpdater()

        updater.start(target)
        completions[0](record("staged"))
        completions[1](record("applying", id: "op-1"))

        delayed.removeFirst()()
        completions[2](record("applied", id: "op-2"))

        XCTAssertEqual(updater.state, .applying(version: "1.8.0", phase: "applying"))
        XCTAssertEqual(delayed.count, 1)
    }

    func testTerminalFailuresShowTheEnginesError() {
        let cases: [(phase: String, expected: String)] = [
            ("rolled_back", EngineUpdateCopy.rolledBack(version: "1.8.0", detail: "engine did not start")),
            ("rollback_failed", EngineUpdateCopy.rollbackFailed(version: "1.8.0", detail: "engine did not start")),
            ("failed", EngineUpdateCopy.updateFailed(version: "1.8.0", detail: "engine did not start")),
        ]
        for testCase in cases {
            resetFakes()
            let updater = makeUpdater()

            updater.start(target)
            completions[0](record("staged"))
            completions[1](record(testCase.phase, error: "engine did not start"))

            XCTAssertTrue(message(updater.state)?.contains("engine did not start") == true, testCase.phase)
            XCTAssertEqual(updater.state, .failed(message: testCase.expected), testCase.phase)
        }
    }

    func testPollingGivesUpAfterTenMinutes() {
        XCTAssertEqual(EngineUpdater.maximumPolls, Int(EngineUpdater.applyTimeout / EngineUpdater.pollInterval))
        XCTAssertEqual(EngineUpdater.maximumPolls, 300)
        let updater = makeUpdater()

        updater.start(target)
        completions[0](record("staged"))
        completions[1](record("draining"))

        var rounds = 0
        while !delayed.isEmpty, rounds < 1_000 {
            delayed.removeFirst()()
            completions.last?(record("draining"))
            rounds += 1
        }

        let statusCalls = calls.filter { $0 == ["update", "status", "--json"] }.count
        XCTAssertEqual(statusCalls, 300)
        XCTAssertEqual(updater.state, .failed(message: EngineUpdateCopy.timedOut))
        XCTAssertTrue(delayed.isEmpty)
    }

    func testStageFailureShowsTheEnginesErrorLine() {
        let updater = makeUpdater()

        updater.start(target)
        completions[0](failure(1, stderr: "warning: slow\nError: could not find uv on PATH\n"))

        XCTAssertEqual(updater.state, .failed(message: EngineUpdateCopy.stageFailed(version: "1.8.0", detail: "could not find uv on PATH")))
        XCTAssertFalse(message(updater.state)?.contains("Error:") == true)
        XCTAssertEqual(calls.count, 1, "No apply after a failed stage")
    }

    func testStageFailureWithoutStderrNamesTheExitStatus() {
        let updater = makeUpdater()

        updater.start(target)
        completions[0](failure(2, stderr: ""))

        XCTAssertTrue(message(updater.state)?.contains("status 2") == true)
    }

    func testUnreadableStageOutputFails() {
        let updater = makeUpdater()

        updater.start(target)
        completions[0](EngineCommandResult(status: 0, stdout: Data("staged 1.8.0 in /x".utf8), stderr: Data()))

        XCTAssertEqual(updater.state, .failed(message: EngineUpdateCopy.unreadableRecord(version: "1.8.0")))
        XCTAssertEqual(calls.count, 1, "No apply after an unreadable stage")
    }

    func testAStageRecordThatIsNotStagedFails() {
        let updater = makeUpdater()

        updater.start(target)
        completions[0](record("failed", error: "digest mismatch"))

        XCTAssertTrue(message(updater.state)?.contains("digest mismatch") == true)
        XCTAssertEqual(calls.count, 1, "No apply after a stage that did not stage")
    }

    func testApplyFailureShowsTheEnginesErrorLine() {
        let updater = makeUpdater()

        updater.start(target)
        completions[0](record("staged"))
        completions[1](failure(1, stderr: "Error: active chats did not finish\n"))

        XCTAssertEqual(updater.state, .failed(message: EngineUpdateCopy.applyFailed(version: "1.8.0", detail: "active chats did not finish")))
        XCTAssertTrue(delayed.isEmpty)
    }

    func testAMissingExecutableFailsWithoutRunning() {
        let updater = makeUpdater(isExecutable: { _ in false })

        updater.start(target)

        XCTAssertEqual(updater.state, .failed(message: EngineUpdateCopy.executableMissing(path: "/Users/me/.local/bin/ciao")))
        XCTAssertTrue(calls.isEmpty)
    }

    func testALaunchFailureFails() {
        let updater = makeUpdater()

        updater.start(target)
        completions[0](nil)

        XCTAssertEqual(updater.state, .failed(message: EngineUpdateCopy.launchFailed(path: "/Users/me/.local/bin/ciao")))
    }

    func testStartIsIgnoredWhileActive() {
        let updater = makeUpdater()

        updater.start(target)
        updater.start(target)

        XCTAssertEqual(calls.count, 1)
    }

    func testAFailedUpdateCanBeRetried() {
        let updater = makeUpdater()

        updater.start(target)
        completions[0](failure(1, stderr: "Error: nope"))
        updater.start(target)

        XCTAssertEqual(calls.count, 2)
        XCTAssertEqual(updater.state, .staging(version: "1.8.0"))
    }

    func testAStageTimeoutReportsTheTimeOut() {
        let updater = makeUpdater()

        updater.start(target)
        completions[0](EngineCommandResult(status: 15, stdout: Data(), stderr: Data(), timedOut: true))

        XCTAssertEqual(updater.state, .failed(message: EngineUpdateCopy.timedOut))
    }

    func testAnApplyTimeoutReportsTheTimeOut() {
        let updater = makeUpdater()

        updater.start(target)
        completions[0](record("staged"))
        completions[1](EngineCommandResult(status: 15, stdout: Data(), stderr: Data(), timedOut: true))

        XCTAssertEqual(updater.state, .failed(message: EngineUpdateCopy.timedOut))
        XCTAssertTrue(delayed.isEmpty)
    }

    func testAPollTimeoutCountsAsStillInProgress() {
        let updater = makeUpdater()

        updater.start(target)
        completions[0](record("staged"))
        completions[1](record("applying"))
        delayed.removeFirst()()
        completions.last?(EngineCommandResult(status: 15, stdout: Data(), stderr: Data(), timedOut: true))

        XCTAssertEqual(updater.state, .applying(version: "1.8.0", phase: "applying"))
        XCTAssertEqual(delayed.count, 1)
    }

    func testResetClearsAFinishedUpdate() {
        let updater = makeUpdater()

        updater.start(target)
        completions[0](failure(1, stderr: "Error: nope"))
        XCTAssertNotEqual(updater.state, .idle)
        updater.reset()
        XCTAssertEqual(updater.state, .idle)

        resetFakes()
        updater.start(target)
        completions[0](record("staged"))
        completions[1](record("applied"))
        XCTAssertEqual(updater.state, .succeeded(version: "1.8.0"))
        updater.reset()
        XCTAssertEqual(updater.state, .idle)
    }

    func testResetDoesNothingWhileAnUpdateRuns() {
        let updater = makeUpdater()

        updater.start(target)
        updater.reset()
        XCTAssertEqual(updater.state, .staging(version: "1.8.0"))

        completions[0](record("staged"))
        completions[1](record("applying"))
        updater.reset()
        XCTAssertEqual(updater.state, .applying(version: "1.8.0", phase: "applying"))
    }

    func testOnlySuccessRequiresRediscovery() {
        XCTAssertTrue(EngineUpdateState.succeeded(version: "1.8.0").requiresRediscovery)
        let others: [EngineUpdateState] = [
            .idle, .staging(version: "1.8.0"), .applying(version: "1.8.0", phase: "draining"), .failed(message: "x"),
        ]
        for state in others {
            XCTAssertFalse(state.requiresRediscovery, "\(state)")
        }
    }
}

final class EngineUpdateRecordTests: XCTestCase {
    func testAJSONRecordIsParsed() throws {
        let json = """
        {
          "error": "",
          "from_version": "1.7.0",
          "id": "op-7",
          "phase": "applied",
          "stage_dir": "/tmp/stage",
          "to_version": "1.8.0",
          "wheel": "ciao.whl"
        }
        """
        let parsed = try XCTUnwrap(EngineUpdateRecord.parse(Data(json.utf8)))

        XCTAssertEqual(parsed.id, "op-7")
        XCTAssertEqual(parsed.phase, "applied")
        XCTAssertEqual(parsed.toVersion, "1.8.0")
        XCTAssertEqual(parsed.error, "")
    }

    func testNonRecordsAreRejected() {
        let bodies = [
            "no update staged",
            "[]",
            "{}",
            #"{"id":"a"}"#,
            #"{"phase":"applied"}"#,
            #"{"id":"a","phase":"Applied"}"#,
            #"{"id":"a","phase":"applied now"}"#,
            #"{"id":"a","phase":"\#(String(repeating: "a", count: 41))"}"#,
            #"{"id":"","phase":"applied"}"#,
        ]
        for body in bodies {
            XCTAssertNil(EngineUpdateRecord.parse(Data(body.utf8)), body)
        }
    }

    func testErrorTextIsSanitizedAndBounded() throws {
        let error = "first\nsecond\u{7}" + String(repeating: "x", count: 1_000)
        let data = try JSONSerialization.data(withJSONObject: ["id": "op-1", "phase": "failed", "error": error])
        let parsed = try XCTUnwrap(EngineUpdateRecord.parse(data))

        XCTAssertLessThanOrEqual(parsed.error.count, 300)
        XCTAssertFalse(parsed.error.contains("\n"))
        XCTAssertFalse(parsed.error.unicodeScalars.contains { $0.properties.generalCategory == .control })
    }

    func testAnUnsafeTargetVersionReadsAsEmpty() throws {
        let parsed = try XCTUnwrap(EngineUpdateRecord.parse(Data(#"{"id":"op-1","phase":"applied","to_version":"1.0 <b>"}"#.utf8)))

        XCTAssertEqual(parsed.toVersion, "")
    }

    func testErrorLineIsTheLastNonEmptyStderrLine() {
        func line(_ text: String) -> String? {
            EngineCommandResult(status: 1, stdout: Data(), stderr: Data(text.utf8)).errorLine
        }
        XCTAssertEqual(line("a\nError: b\n\n"), "b")
        XCTAssertNil(line(""))
        XCTAssertEqual(line("plain\n"), "plain")
    }
}

final class EngineUpdateOfferTests: XCTestCase {
    private let executable = URL(fileURLWithPath: "/Users/me/.local/bin/ciao")

    private func offer(
        status: LocalServerDiscovery.Status = .running(version: "1.7.0", ready: true, onThisMac: true),
        candidate: Origin? = Origin("http://127.0.0.1:8443"),
        installation: LocalInstallation? = LocalInstallation(
            hasService: true, installedVersion: "1.7.0", port: 8443, executable: URL(fileURLWithPath: "/Users/me/.local/bin/ciao")
        ),
        availableVersion: String? = "1.8.0"
    ) -> EngineUpdateTarget? {
        EngineUpdateOffer.target(
            status: status,
            candidate: candidate,
            installation: installation,
            availableVersion: availableVersion
        )
    }

    func testALocalRunningEngineWithANewerVersionIsOffered() {
        XCTAssertEqual(offer(), EngineUpdateTarget(executable: executable, version: "1.8.0"))
    }

    func testIsNewerComparesThreeNumericComponents() {
        XCTAssertTrue(EngineUpdateOffer.isNewer("1.8.0", than: "1.7.0"))
        XCTAssertTrue(EngineUpdateOffer.isNewer("1.10.0", than: "1.9.0"))
        XCTAssertFalse(EngineUpdateOffer.isNewer("1.7.0", than: "1.7.0"))
        XCTAssertFalse(EngineUpdateOffer.isNewer("1.6.9", than: "1.7.0"))
        XCTAssertFalse(EngineUpdateOffer.isNewer("1.9.0-rc1", than: "1.8.0"))
        XCTAssertFalse(EngineUpdateOffer.isNewer("1.9.0", than: "1.8.0-rc1"))
        XCTAssertFalse(EngineUpdateOffer.isNewer("1.9", than: "1.8.0"))
        XCTAssertFalse(EngineUpdateOffer.isNewer("1.9.0", than: "1.8.0.1"))
        XCTAssertFalse(EngineUpdateOffer.isNewer("1.9.x", than: "1.8.0"))
    }

    func testOnlyANewerNumericVersionIsOffered() {
        let running = LocalServerDiscovery.Status.running(version: "1.7.0", ready: true, onThisMac: true)
        let candidate = Origin("http://127.0.0.1:8443")
        let installed = LocalInstallation(hasService: true, installedVersion: "1.7.0", port: 8443, executable: executable)
        func offered(_ available: String, installation: LocalInstallation? = installed) -> EngineUpdateTarget? {
            EngineUpdateOffer.target(status: running, candidate: candidate, installation: installation, availableVersion: available)
        }

        XCTAssertNotNil(offered("1.8.0"))
        XCTAssertNil(offered("1.7.0"))
        XCTAssertNil(offered("1.6.0"))
        XCTAssertNil(offered("1.9.0-rc1"))
        XCTAssertNil(offered("1.8.0", installation: LocalInstallation(hasService: true, installedVersion: nil, port: 8443, executable: executable)))
    }

    func testEachMissingConditionWithdrawsTheOffer() {
        XCTAssertNil(offer(status: .running(version: "1.7.0", ready: true, onThisMac: false)))
        XCTAssertNil(offer(status: .installedNotRunning(version: nil)))
        XCTAssertNil(offer(candidate: Origin("https://example.com")))
        XCTAssertNil(offer(installation: LocalInstallation(hasService: true, installedVersion: "1.7.0", port: 8443)))
        XCTAssertNil(offer(installation: nil))
        XCTAssertNil(offer(availableVersion: nil))
    }
}

@MainActor
final class EngineUpdateCopyTests: XCTestCase {
    func testAvailabilityNamesTheUpdateOnlyWhenItIsOffered() {
        XCTAssertEqual(EngineUpdateCopy.availability(version: "1.8.0", updatable: true), "Ciaobot 1.8.0 is available.")
        XCTAssertEqual(EngineUpdateCopy.availability(version: "1.8.0", updatable: false),
                       "Ciaobot 1.8.0 is available. Update it from its Settings.")
    }

    func testProgressCoversEveryActivePhaseOnly() {
        XCTAssertNotNil(EngineUpdateCopy.progress(for: .staging(version: "1.8.0")))
        let phases = ["staged", "draining", "applying", "stopping", "swapping", "starting", "verifying_start", "rolling_back", "unknown_phase"]
        for phase in phases {
            XCTAssertNotNil(EngineUpdateCopy.progress(for: .applying(version: "1.8.0", phase: phase)), phase)
        }
        XCTAssertNil(EngineUpdateCopy.progress(for: .idle))
        XCTAssertNil(EngineUpdateCopy.progress(for: .succeeded(version: "1.8.0")))
        XCTAssertNil(EngineUpdateCopy.progress(for: .failed(message: "x")))
    }

    func testTimeoutCopyDoesNotPromiseTheTenMinuteWindow() {
        XCTAssertTrue(EngineUpdateCopy.timedOut.contains("did not finish updating in time"))
        XCTAssertFalse(EngineUpdateCopy.timedOut.contains("10 minutes"))
    }

    func testOutcomeNamesTheVersionAfterSuccess() {
        XCTAssertEqual(EngineUpdateCopy.outcome(for: .succeeded(version: "1.8.0")), "Ciaobot was updated to 1.8.0.")
    }
}

/// The live runner launches only `/bin/sh` and `/usr/bin/env`.
final class EngineUpdaterLiveRunTests: XCTestCase {
    func testLiveRunCapturesStatusStdoutAndStderr() {
        let done = expectation(description: "command finished")
        var outcome: EngineCommandResult?
        var onMainThread = false

        EngineUpdater.liveRun(environment: [:])(URL(fileURLWithPath: "/bin/sh"), ["-c", "printf out; printf err >&2; exit 3"], 60) { result in
            outcome = result
            onMainThread = Thread.isMainThread
            done.fulfill()
        }

        wait(for: [done], timeout: 10)
        XCTAssertEqual(outcome?.status, 3)
        XCTAssertEqual(outcome?.stdout, Data("out".utf8))
        XCTAssertEqual(outcome?.stderr, Data("err".utf8))
        XCTAssertTrue(onMainThread)
    }

    func testAHungCommandIsTerminatedAtItsTimeout() {
        let done = expectation(description: "hung command ended")
        var outcome: EngineCommandResult?

        EngineUpdater.liveRun(environment: [:])(URL(fileURLWithPath: "/bin/sh"), ["-c", "sleep 30"], 1) { result in
            outcome = result
            done.fulfill()
        }

        wait(for: [done], timeout: 5)
        XCTAssertNotNil(outcome)
        XCTAssertNotEqual(outcome?.status, 0, "A terminated command reports a nonzero (signal) status")
        XCTAssertEqual(outcome?.timedOut, true)
    }

    func testLiveRunStripsCiaoAndPwaVariables() {
        let done = expectation(description: "environment printed")
        var output = ""

        EngineUpdater.liveRun(environment: ["CIAO_TOKEN": "x", "PWA_PORT": "1", "KEEP": "a=b"])(
            URL(fileURLWithPath: "/usr/bin/env"), [], 60
        ) { result in
            output = String(decoding: result?.stdout ?? Data(), as: UTF8.self)
            done.fulfill()
        }

        wait(for: [done], timeout: 10)
        XCTAssertTrue(output.contains("KEEP=a=b"))
        XCTAssertFalse(output.contains("CIAO_TOKEN"))
        XCTAssertFalse(output.contains("PWA_PORT"))
    }

    func testLiveRunReportsALaunchFailureAsNil() {
        let done = expectation(description: "launch attempted")
        var outcome: EngineCommandResult? = EngineCommandResult(status: 0, stdout: Data(), stderr: Data())

        EngineUpdater.liveRun(environment: [:])(URL(fileURLWithPath: "/nonexistent/ciao-\(UUID().uuidString)"), [], 60) { result in
            outcome = result
            done.fulfill()
        }

        wait(for: [done], timeout: 10)
        XCTAssertNil(outcome)
    }

    func testChildEnvironmentReusesTheInstallerFilter() {
        XCTAssertEqual(
            EngineUpdater.childEnvironment(["CIAO_A": "1", "PWA_B": "2", "PATH": "/bin", "X": "a=b"]),
            ["PATH": "/bin", "X": "a=b"]
        )
    }
}
