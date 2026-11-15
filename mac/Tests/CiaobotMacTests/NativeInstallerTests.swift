import Combine
import XCTest
import Foundation

@testable import CiaobotMac

/// Runs the real spawn path against small POSIX shell installers. Only the
/// network fetch is replaced; the pin's digest is the fake script's digest.
@MainActor
final class NativeInstallerTests: XCTestCase {
    private var root: URL!
    private var temporaryRoot: URL!
    private var logURL: URL!
    private var history: [NativeInstallState] = []
    private var cancellables = Set<AnyCancellable>()
    private let runID = UUID(uuidString: "6F1C2A4E-9B7D-4C3A-8E5F-0A1B2C3D4E5F")!

    private let origin8443 = Origin("http://127.0.0.1:8443")!
    private let origin8444 = Origin("http://127.0.0.1:8444")!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("native-installer-tests-\(UUID().uuidString)", isDirectory: true)
        temporaryRoot = root.appendingPathComponent("tmp", isDirectory: true)
        logURL = root.appendingPathComponent("logs/native-install.log")
        try FileManager.default.createDirectory(at: temporaryRoot, withIntermediateDirectories: true)
        history = []
    }

    override func tearDownWithError() throws {
        cancellables.removeAll()
        try? FileManager.default.removeItem(at: root)
    }

    // MARK: Helpers

    /// Shell prelude every fake installer gets. It stores the run id from
    /// `--run-id` without consuming "$@", and defines `ev SEQ CONTENT` to
    /// write one event line on the inherited descriptor 3.
    private static let prelude = #"""
    #!/bin/sh
    RID=
    prev=
    for a in "$@"; do if [ "$prev" = "--run-id" ]; then RID="$a"; fi; prev="$a"; done
    emit() { printf '%s\n' "$1" >&3; }
    ev() { emit "{\"schema\":1,\"run_id\":\"$RID\",\"seq\":$1,$2}"; }

    """#

    private func makeScript(_ body: String) -> (Data, NativeInstallPin) {
        let data = Data((Self.prelude + body).utf8)
        let pin = NativeInstallPin(
            version: NativeInstallPin.current.version,
            url: NativeInstallPin.current.url,
            sha256: NativeInstallPin.sha256Hex(data)
        )
        return (data, pin)
    }

    private func makeInstaller(
        pin: NativeInstallPin,
        fetch: @escaping NativeInstallFetch,
        makeRunID: (() -> UUID)? = nil
    ) -> NativeInstaller {
        let id = runID
        let installer = NativeInstaller(
            pin: pin,
            fetch: fetch,
            makeRunID: makeRunID ?? { id },
            temporaryRoot: temporaryRoot,
            logURL: logURL
        )
        installer.$state
            .sink { [weak self] state in self?.history.append(state) }
            .store(in: &cancellables)
        return installer
    }

    private func fetchReturning(_ data: Data) -> NativeInstallFetch {
        { _ in data }
    }

    /// Waits on `installer.$state` until a state matches, failing the test on timeout.
    private func waitForState(
        _ installer: NativeInstaller,
        timeout: TimeInterval = 10,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ predicate: @escaping (NativeInstallState) -> Bool
    ) async {
        let reached = expectation(description: "installer state reached")
        reached.assertForOverFulfill = false
        let observer = installer.$state.sink { state in
            if predicate(state) { reached.fulfill() }
        }
        await fulfillment(of: [reached], timeout: timeout)
        observer.cancel()
        if !predicate(installer.state) {
            XCTFail("timed out waiting for state; last state was \(installer.state)", file: file, line: line)
        }
    }

    /// Waits until the previous child has been reaped and a new run may start.
    private func waitForCanStart(_ installer: NativeInstaller, timeout: TimeInterval = 10) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !installer.canStart, Date() < deadline {
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        XCTAssertTrue(installer.canStart, "the previous child was never reaped")
    }

    private func temporaryInstallerEntries() -> [String] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: temporaryRoot.path)) ?? []
        return names.filter { $0.hasPrefix("ciaobot-native-install-") }
    }

    /// The run removes its private directory only after the child is reaped,
    /// so polling for that proves the run has finished.
    private func waitForTemporaryInstallerToClear(timeout: TimeInterval = 10) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !temporaryInstallerEntries().isEmpty, Date() < deadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTAssertEqual(temporaryInstallerEntries(), [], "the installer run did not finish and clean up")
    }

    // MARK: Verification and download

    func testHashMismatchNeverRunsTheScript() async throws {
        let marker = root.appendingPathComponent("marker").path
        let (original, pin) = makeScript("""
        touch '\(marker)'
        ev 1 '"type":"phase","phase":"preflight"'
        ev 2 '"type":"phase","phase":"wizard"'
        ev 3 '"type":"wizard_ready","origin":"http://127.0.0.1:8443"'
        ev 4 '"type":"setup_saved"'
        ev 5 '"type":"phase","phase":"readiness"'
        ev 6 '"type":"service_ready","origin":"http://127.0.0.1:8444"'
        exit 0

        """)
        // The pin still names the original script; the fetched bytes differ by one byte.
        var tampered = original
        tampered[0] ^= 0x01
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(tampered))

        installer.start()
        await waitForState(installer) { $0 == .failed(.checksumMismatch) }
        try await Task.sleep(nanoseconds: 300_000_000)

        XCTAssertFalse(FileManager.default.fileExists(atPath: marker))
        XCTAssertEqual(installer.state, .failed(.checksumMismatch))
        XCTAssertEqual(temporaryInstallerEntries(), [])
    }

    func testDownloadErrorFails() async {
        let (_, pin) = makeScript("exit 0\n")
        let fetch: NativeInstallFetch = { _ in throw URLError(.notConnectedToInternet) }
        let installer = makeInstaller(pin: pin, fetch: fetch)

        installer.start()
        await waitForState(installer) { $0 == .failed(.downloadFailed(.offline)) }
    }

    func testOfflineDownloadIsClassifiedAndLogged() async throws {
        let (_, pin) = makeScript("exit 0\n")
        let fetch: NativeInstallFetch = { _ in throw URLError(.notConnectedToInternet) }
        let installer = makeInstaller(pin: pin, fetch: fetch)

        installer.start()
        await waitForState(installer) { $0 == .failed(.downloadFailed(.offline)) }

        let log = try String(contentsOf: logURL, encoding: .utf8)
        let lines = log.split(separator: "\n").map(String.init)
        XCTAssertEqual(lines.count, 1)
        let line = try XCTUnwrap(lines.first)
        XCTAssertTrue(line.contains("phase=download"), line)
        XCTAssertTrue(line.contains(pin.url.absoluteString), line)
        XCTAssertTrue(line.contains("offline"), line)
        XCTAssertTrue(line.contains("-1009"), line)
    }

    func testTimedOutDownloadIsClassified() async throws {
        let (_, pin) = makeScript("exit 0\n")
        let fetch: NativeInstallFetch = { _ in throw URLError(.timedOut) }
        let installer = makeInstaller(pin: pin, fetch: fetch)

        installer.start()
        await waitForState(installer) { $0 == .failed(.downloadFailed(.timedOut)) }

        let log = try String(contentsOf: logURL, encoding: .utf8)
        XCTAssertTrue(log.contains("timedOut"), log)
    }

    func testAFailingFetchIsRetriedOnce() async throws {
        let (data, pin) = makeScript("""
        ev 1 '"type":"phase","phase":"readiness"'
        ev 2 '"type":"service_ready","origin":"http://127.0.0.1:8444"'
        exit 0

        """)
        let calls = FetchCounter()
        let fetch: NativeInstallFetch = { _ in
            calls.increment()
            if calls.count == 1 { throw URLError(.timedOut) }
            return data
        }
        let installer = makeInstaller(pin: pin, fetch: fetch)

        installer.start()
        await waitForState(installer) { $0 == .succeeded(self.origin8444) }

        XCTAssertEqual(calls.count, 2)
    }

    func testResolveFailureLogsTheFailingURL() async throws {
        let manifestURL = URL(
            string: "https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/ciaobot-engine-manifest.json"
        )!
        let fetch: NativeInstallFetch = { _ in throw URLError(.notConnectedToInternet) }
        let id = runID
        let installer = NativeInstaller(
            resolve: { fetch in
                _ = try await fetch(manifestURL)
                fatalError("unreachable")
            },
            fetch: fetch,
            makeRunID: { id },
            temporaryRoot: temporaryRoot,
            logURL: logURL
        )

        installer.start()
        await waitForState(installer) { $0 == .failed(.downloadFailed(.offline)) }

        let log = try String(contentsOf: logURL, encoding: .utf8)
        let lines = log.split(separator: "\n").map(String.init)
        XCTAssertEqual(lines.count, 1)
        let line = try XCTUnwrap(lines.first)
        XCTAssertTrue(line.contains("phase=resolve"), line)
        XCTAssertTrue(line.contains(manifestURL.absoluteString), line)
    }

    func testCancelledDownloadIsNotLogged() async throws {
        let (_, pin) = makeScript("exit 0\n")
        let fetch: NativeInstallFetch = { _ in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 10_000_000)
            }
            throw URLError(.cancelled)
        }
        let installer = makeInstaller(pin: pin, fetch: fetch)

        installer.start()
        await waitForState(installer) { $0 == .downloading }
        installer.cancel()
        XCTAssertEqual(installer.state, .failed(.cancelledBeforeLaunch))

        try await Task.sleep(nanoseconds: 300_000_000)

        XCTAssertEqual(installer.state, .failed(.cancelledBeforeLaunch))
        XCTAssertFalse(FileManager.default.fileExists(atPath: logURL.path))
    }

    // MARK: Spawn contract and event handling

    func testSuccessfulRunPassesTheExactCommandAndReachesServiceReady() async throws {
        let argsFile = root.appendingPathComponent("args.txt").path
        let (data, pin) = makeScript(#"""
        printf '%s\n' "$0" "$@" > "\#(argsFile)"
        ev 1 '"type":"phase","phase":"preflight"'
        ev 2 '"type":"phase","phase":"bootstrap"'
        ev 3 '"type":"phase","phase":"download"'
        ev 4 '"type":"phase","phase":"install"'
        ev 5 '"type":"phase","phase":"wizard"'
        ev 6 '"type":"wizard_ready","origin":"http://127.0.0.1:8443"'
        ev 7 '"type":"setup_saved"'
        ev 8 '"type":"phase","phase":"activation"'
        ev 9 '"type":"phase","phase":"readiness"'
        ev 10 '"type":"service_ready","origin":"http://127.0.0.1:8444"'
        exit 0

        """#)
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data))

        installer.start()
        await waitForState(installer) { $0 == .succeeded(self.origin8444) }

        let written = try String(contentsOfFile: argsFile, encoding: .utf8)
        let lines = written.split(separator: "\n", omittingEmptySubsequences: false).dropLast().map(String.init)
        XCTAssertEqual(lines.count, 8)
        // lines[0] is $0, the script path; the rest is the argument tail.
        XCTAssertEqual(
            Array(pin.arguments(scriptPath: lines[0], runID: runID).dropFirst()),
            lines
        )
        XCTAssertEqual(lines.last, "6f1c2a4e-9b7d-4c3a-8e5f-0a1b2c3d4e5f")

        let wizardIndex = try XCTUnwrap(history.firstIndex(of: .wizard(origin8443)))
        XCTAssertTrue(history[wizardIndex...].contains(.running(.wizard)))
        await waitForTemporaryInstallerToClear()
    }

    func testFailedEventEndsTheRun() async {
        let (data, pin) = makeScript(#"""
        ev 1 '"type":"phase","phase":"preflight"'
        ev 2 '"type":"failed","code":"activation_refused"'
        exit 1

        """#)
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data))

        installer.start()
        await waitForState(installer) { $0 == .failed(.installer(.activationRefused)) }
    }

    func testResetClearsAFailedRunOnly() async {
        let (data, pin) = makeScript(#"""
        ev 1 '"type":"phase","phase":"preflight"'
        ev 2 '"type":"failed","code":"activation_refused"'
        exit 1

        """#)
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data))
        await waitForCanStart(installer)
        installer.reset()
        XCTAssertEqual(installer.state, .idle, "A fresh installer stays idle")

        installer.start()
        await waitForState(installer) { $0 == .failed(.installer(.activationRefused)) }
        installer.reset()
        XCTAssertEqual(installer.state, .idle)
    }

    func testExitWithoutTerminalEventIsAFailure() async {
        let (data, pin) = makeScript(#"""
        ev 1 '"type":"phase","phase":"preflight"'
        ev 2 '"type":"phase","phase":"bootstrap"'
        exit 0

        """#)
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data))

        installer.start()
        await waitForState(installer) { $0 == .failed(.protocolViolation(.missingTerminalEvent)) }
    }

    func testStdoutAndStderrAreNeverParsed() async throws {
        let (data, pin) = makeScript(#"""
        printf '%s\n' '{"schema":1,"run_id":"'"$RID"'","seq":1,"type":"service_ready","origin":"http://127.0.0.1:8444"}'
        printf '%s\n' '{"schema":1,"run_id":"'"$RID"'","seq":1,"type":"service_ready","origin":"http://127.0.0.1:8444"}' >&2
        ev 1 '"type":"failed","code":"internal_error"'
        exit 0

        """#)
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data))

        installer.start()
        await waitForState(installer) { $0 == .failed(.installer(.internalError)) }

        let log = try String(contentsOf: logURL, encoding: .utf8)
        XCTAssertTrue(log.contains(#""type":"service_ready""#))
    }

    func testMalformedEventStopsAndTerminatesTheInstaller() async {
        let (data, pin) = makeScript(#"""
        emit 'not json'
        trap 'exit 0' TERM
        i=0; while [ $i -lt 300 ]; do sleep 0.1; i=$((i+1)); done

        """#)
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data))

        installer.start()
        await waitForState(installer) { $0 == .failed(.protocolViolation(.malformedJSON)) }
        // The child loops forever unless SIGTERM reaches it; the run only
        // cleans up after the child is reaped.
        await waitForTemporaryInstallerToClear()
    }

    func testOnlyTheEventDescriptorIsInherited() async {
        // Plant an inheritable descriptor (no FD_CLOEXEC) at a high number in the
        // test process. A leaking spawn would hand it to the script.
        let raw = open(root.appendingPathComponent("planted.txt").path, O_RDWR | O_CREAT, 0o600)
        XCTAssertGreaterThanOrEqual(raw, 0)
        let planted = fcntl(raw, F_DUPFD, 40)
        close(raw)
        XCTAssertGreaterThanOrEqual(planted, 40)
        defer { close(planted) }

        let openFDsFile = root.appendingPathComponent("open-fds.txt").path
        let fd3File = root.appendingPathComponent("fd3.txt").path
        let (data, pin) = makeScript(#"""
        i=4
        while [ $i -le 250 ]; do
            if { true >&$i; } 2>/dev/null; then echo $i >> "\#(openFDsFile)"; fi
            i=$((i+1))
        done
        if { true >&3; } 2>/dev/null; then echo 3 > "\#(fd3File)"; fi
        ev 1 '"type":"failed","code":"internal_error"'
        exit 0

        """#)
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data))

        installer.start()
        await waitForState(installer) { $0 == .failed(.installer(.internalError)) }

        XCTAssertTrue(FileManager.default.fileExists(atPath: fd3File), "the event descriptor (fd 3) was not open")
        let record = (try? String(contentsOfFile: openFDsFile, encoding: .utf8)) ?? ""
        let recorded = record.split(separator: "\n").map(String.init)
        XCTAssertFalse(recorded.contains(String(planted)), "the planted descriptor \(planted) leaked into the installer")
        // The shell saves the target of a redirection at fds 10 and 11 while it runs
        // `{ true >&$n; }`, even with nothing inherited. Those are the shell's own
        // descriptors, not inherited ones, so they are the only ones allowed here.
        XCTAssertTrue(Set(recorded).isSubset(of: ["10", "11"]), "unexpected open descriptors: \(recorded)")
    }

    // MARK: Cancellation

    func testCancelBeforeLaunchStopsTheDownload() async throws {
        let marker = root.appendingPathComponent("marker").path
        let (data, pin) = makeScript("touch '\(marker)'\nexit 0\n")
        // The fetch ignores cancellation and only returns when the test releases it.
        let gate = FetchGate()
        let gatedFetch: NativeInstallFetch = { _ in
            await gate.waitForRelease()
            return data
        }
        let installer = makeInstaller(pin: pin, fetch: gatedFetch)

        installer.start()
        await waitForState(installer) { $0 == .downloading }
        // `.downloading` is set before the async run starts, so cancel only once
        // the download fetch is genuinely in flight; otherwise the run can stop
        // at the post-resolve generation guard and never reach the gate.
        await gate.waitUntilEntered()
        installer.cancel()
        XCTAssertEqual(installer.state, .failed(.cancelledBeforeLaunch))

        await gate.release()
        // The run task is the explicit completion signal. For a run cancelled
        // before launch it returns right after the released fetch, so nothing
        // further can happen; no such guarantee holds once a child is launched.
        let runTask = try XCTUnwrap(installer.runTask)
        await runTask.value

        XCTAssertEqual(installer.state, .failed(.cancelledBeforeLaunch))
        XCTAssertFalse(FileManager.default.fileExists(atPath: marker))
        XCTAssertEqual(temporaryInstallerEntries(), [])
        let sawCancelled = await gate.sawCancelled
        XCTAssertEqual(sawCancelled, true)
    }

    func testCancelReportsWhateverTerminalEventTheInstallerSends() async {
        let (data, pin) = makeScript(#"""
        on_term() { ev 20 '"type":"failed","code":"internal_error"'; exit 1; }
        trap on_term TERM
        ev 1 '"type":"phase","phase":"wizard"'
        ev 2 '"type":"wizard_ready","origin":"http://127.0.0.1:8443"'
        i=0; while [ $i -lt 300 ]; do sleep 0.1; i=$((i+1)); done

        """#)
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data))

        installer.start()
        await waitForState(installer) { $0.wizardOrigin != nil }
        installer.cancel()
        await waitForState(installer) { $0 == .failed(.installer(.internalError)) }
    }

    func testCancelWhileTheWizardIsOpenRevokesItAndReportsCancelled() async {
        let (data, pin) = makeScript(#"""
        on_term() { ev 20 '"type":"failed","code":"cancelled"'; exit 1; }
        trap on_term TERM
        ev 1 '"type":"phase","phase":"preflight"'
        ev 2 '"type":"phase","phase":"wizard"'
        ev 3 '"type":"wizard_ready","origin":"http://127.0.0.1:8443"'
        i=0; while [ $i -lt 300 ]; do sleep 0.1; i=$((i+1)); done

        """#)
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data))

        installer.start()
        await waitForState(installer) { $0.wizardOrigin != nil }

        let mark = history.count
        installer.cancel()
        XCTAssertEqual(installer.state, .cancelling)
        XCTAssertNil(installer.state.wizardOrigin)

        await waitForState(installer) { $0 == .failed(.installer(.cancelled)) }
        XCTAssertFalse(history[mark...].contains { $0.wizardOrigin != nil })
    }

    func testServiceReadyWinsARaceWithCancel() async {
        let (data, pin) = makeScript(#"""
        on_term() { ev 10 '"type":"service_ready","origin":"http://127.0.0.1:8444"'; exit 0; }
        trap on_term TERM
        ev 1 '"type":"phase","phase":"readiness"'
        i=0; while [ $i -lt 300 ]; do sleep 0.1; i=$((i+1)); done

        """#)
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data))

        installer.start()
        await waitForState(installer) { $0 == .running(.readiness) }
        installer.cancel()
        await waitForState(installer) { $0 == .succeeded(self.origin8444) }
    }

    // MARK: Lifecycle

    func testStartIsIgnoredWhileARunIsActive() async throws {
        let startsFile = root.appendingPathComponent("starts.txt").path
        let (data, pin) = makeScript(#"""
        on_term() { ev 2 '"type":"failed","code":"cancelled"'; exit 1; }
        trap on_term TERM
        printf 'start\n' >> "\#(startsFile)"
        ev 1 '"type":"phase","phase":"preflight"'
        i=0; while [ $i -lt 300 ]; do sleep 0.1; i=$((i+1)); done

        """#)
        var runIDCalls = 0
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data), makeRunID: {
            runIDCalls += 1
            return UUID()
        })

        installer.start()
        await waitForState(installer) { $0 == .running(.preflight) }
        XCTAssertEqual(runIDCalls, 1)
        installer.start()
        XCTAssertEqual(runIDCalls, 1)
        XCTAssertEqual(installer.state, .running(.preflight))
        try await Task.sleep(nanoseconds: 500_000_000)

        let starts = try String(contentsOfFile: startsFile, encoding: .utf8)
        XCTAssertEqual(starts.split(separator: "\n").count, 1)

        installer.cancel()
        await waitForState(installer) { $0 == .failed(.installer(.cancelled)) }
    }

    func testALeakedEventDescriptorDoesNotStrandTheRun() async {
        // A background descendant inherits fd 3 and keeps the pipe open after the installer exits.
        let pidFile = root.appendingPathComponent("leaked-pid.txt").path
        var leakedPID: pid_t?
        defer { if let leakedPID { kill(leakedPID, SIGTERM) } }
        let (data, pin) = makeScript(#"""
        ev 1 '"type":"phase","phase":"preflight"'
        sleep 20 &
        printf '%s\n' "$!" > "\#(pidFile)"
        exit 0

        """#)
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data))

        installer.start()
        await waitForState(installer) { $0 == .failed(.protocolViolation(.missingTerminalEvent)) }
        leakedPID = (try? String(contentsOfFile: pidFile, encoding: .utf8))
            .flatMap { Int32($0.trimmingCharacters(in: .whitespacesAndNewlines)) }

        await waitForTemporaryInstallerToClear()
        await waitForCanStart(installer)
    }

    func testStartWaitsForThePreviousChildToBeReaped() async throws {
        let startsFile = root.appendingPathComponent("starts.txt").path
        let (data, pin) = makeScript(#"""
        printf 'start\n' >> "\#(startsFile)"
        ev 1 '"type":"failed","code":"internal_error"'
        trap '' TERM
        sleep 2
        exit 1

        """#)
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data))

        installer.start()
        await waitForState(installer) { $0 == .failed(.installer(.internalError)) }
        // The terminal event arrived, but the child is still alive.
        XCTAssertFalse(installer.canStart)

        installer.start()
        let starts = try String(contentsOfFile: startsFile, encoding: .utf8)
        XCTAssertEqual(starts.split(separator: "\n").count, 1)

        await waitForCanStart(installer)
    }

    func testChildEnvironmentDropsCiaobotVariables() {
        let environment = NativeInstallerProcess.childEnvironment([
            "HOME": "/Users/test",
            "PATH": "/usr/bin:/bin",
            "CIAO_WORKSPACE": "/workspace",
            "PWA_AUTH_TOKEN": "secret",
            "CIAOX": "kept",
        ])
        XCTAssertEqual(environment, ["CIAOX=kept", "HOME=/Users/test", "PATH=/usr/bin:/bin"])
    }

    func testTemporaryInstallerIsRemovedAfterTheRun() async {
        let (data, pin) = makeScript(#"""
        ev 1 '"type":"phase","phase":"preflight"'
        ev 2 '"type":"failed","code":"invalid_arguments"'
        exit 1

        """#)
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data))

        installer.start()
        await waitForState(installer) { $0 == .failed(.installer(.invalidArguments)) }
        await waitForTemporaryInstallerToClear()
    }

    // MARK: Release resolution

    func testAnUnverifiedReleaseNeverDownloadsTheInstaller() async throws {
        let (data, _) = makeScript("exit 0\n")
        let requested = FetchCounter()
        let fetch: NativeInstallFetch = { _ in
            requested.increment()
            return data
        }
        let id = runID
        let installer = NativeInstaller(
            resolve: { _ in throw EngineReleaseError.signature(.badSignature) },
            fetch: fetch,
            makeRunID: { id },
            temporaryRoot: temporaryRoot,
            logURL: logURL
        )

        installer.start()
        await waitForState(installer) { $0 == .failed(.releaseUnverified) }

        XCTAssertEqual(requested.count, 0, "the installer was downloaded for an unverified release")
        XCTAssertEqual(temporaryInstallerEntries(), [])
    }

    func testTheResolvedReleaseIsTheOneThatRuns() async throws {
        let argsFile = root.appendingPathComponent("args.txt").path
        let (data, scriptPin) = makeScript(#"""
        printf '%s\n' "$@" > "\#(argsFile)"
        ev 1 '"type":"phase","phase":"readiness"'
        ev 2 '"type":"service_ready","origin":"http://127.0.0.1:8444"'
        exit 0

        """#)
        let resolved = NativeInstallPin(version: "9.9.9", url: scriptPin.url, sha256: scriptPin.sha256)
        let id = runID
        let installer = NativeInstaller(
            resolve: { _ in resolved },
            fetch: fetchReturning(data),
            makeRunID: { id },
            temporaryRoot: temporaryRoot,
            logURL: logURL
        )

        installer.start()
        await waitForState(installer) { $0 == .succeeded(self.origin8444) }

        let written = try String(contentsOfFile: argsFile, encoding: .utf8)
        XCTAssertTrue(written.contains("--version\n9.9.9\n"), "the script ran with \(written)")
    }

    // MARK: Native repair

    func testRepairPassesTheRepairFlagAndReachesServiceReady() async throws {
        let argsFile = root.appendingPathComponent("args.txt").path
        let (data, pin) = makeScript(#"""
        printf '%s\n' "$0" "$@" > "\#(argsFile)"
        ev 1 '"type":"phase","phase":"preflight"'
        ev 2 '"type":"phase","phase":"bootstrap"'
        ev 3 '"type":"phase","phase":"download"'
        ev 4 '"type":"phase","phase":"install"'
        ev 5 '"type":"phase","phase":"activation"'
        ev 6 '"type":"phase","phase":"readiness"'
        ev 7 '"type":"service_ready","origin":"http://127.0.0.1:8443"'
        exit 0

        """#)
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data))

        installer.start(mode: .repair)
        await waitForState(installer) { $0 == .succeeded(self.origin8443) }

        XCTAssertEqual(installer.mode, .repair)
        let written = try String(contentsOfFile: argsFile, encoding: .utf8)
        let lines = written.split(separator: "\n", omittingEmptySubsequences: false).dropLast().map(String.init)
        XCTAssertEqual(lines.count, 8)
        XCTAssertTrue(lines.contains("--native-repair"))
        XCTAssertFalse(lines.contains("--native-install"))
        XCTAssertFalse(lines.contains("--workspace"))
        XCTAssertEqual(
            Array(pin.arguments(scriptPath: lines[0], runID: runID, mode: .repair).dropFirst()),
            lines
        )
        XCTAssertTrue(history.contains(.running(.install)))
        XCTAssertTrue(history.allSatisfy { $0.wizardOrigin == nil })
        await waitForTemporaryInstallerToClear()
    }

    func testAWizardEventDuringRepairIsAProtocolViolation() async {
        let (data, pin) = makeScript(#"""
        ev 1 '"type":"phase","phase":"preflight"'
        ev 2 '"type":"phase","phase":"install"'
        ev 3 '"type":"wizard_ready","origin":"http://127.0.0.1:8443"'
        exit 0

        """#)
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data))

        installer.start(mode: .repair)
        await waitForState(installer) { $0 == .failed(.protocolViolation(.unexpectedEvent)) }

        XCTAssertTrue(history.allSatisfy { $0.wizardOrigin == nil })
        await waitForTemporaryInstallerToClear()
    }

    func testNotInstalledEndsARepair() async {
        let (data, pin) = makeScript(#"""
        ev 1 '"type":"phase","phase":"preflight"'
        ev 2 '"type":"failed","code":"not_installed"'
        exit 1

        """#)
        let installer = makeInstaller(pin: pin, fetch: fetchReturning(data))

        installer.start(mode: .repair)
        await waitForState(installer) { $0 == .failed(.installer(.notInstalled)) }
        await waitForTemporaryInstallerToClear()
    }

    func testARepairWithoutAQualifyingReleaseNeverDownloadsTheInstaller() async throws {
        let (data, _) = makeScript("exit 0\n")
        let requested = FetchCounter()
        let fetch: NativeInstallFetch = { _ in
            requested.increment()
            return data
        }
        let id = runID
        let installer = NativeInstaller(
            resolveRepair: { _ in throw EngineReleaseError.repairUnavailable },
            fetch: fetch,
            makeRunID: { id },
            temporaryRoot: temporaryRoot,
            logURL: logURL
        )

        installer.start(mode: .repair)
        await waitForState(installer) { $0 == .failed(.repairUnavailable) }

        XCTAssertEqual(requested.count, 0, "the installer was downloaded for a release that cannot repair")
        XCTAssertEqual(temporaryInstallerEntries(), [])
    }

    func testEachModeRunsItsOwnResolvedRelease() async throws {
        let argsFile = root.appendingPathComponent("args.txt").path
        let (data, scriptPin) = makeScript(#"""
        printf '%s\n' "$@" > "\#(argsFile)"
        ev 1 '"type":"phase","phase":"readiness"'
        ev 2 '"type":"service_ready","origin":"http://127.0.0.1:8444"'
        exit 0

        """#)
        let id = runID
        let installer = NativeInstaller(
            resolve: { _ in NativeInstallPin(version: "8.8.8", url: scriptPin.url, sha256: scriptPin.sha256) },
            resolveRepair: { _ in NativeInstallPin(version: "9.9.9", url: scriptPin.url, sha256: scriptPin.sha256) },
            fetch: fetchReturning(data),
            makeRunID: { id },
            temporaryRoot: temporaryRoot,
            logURL: logURL
        )

        installer.start(mode: .repair)
        await waitForState(installer) { $0 == .succeeded(self.origin8444) }
        let repairArgs = try String(contentsOfFile: argsFile, encoding: .utf8)
        XCTAssertTrue(repairArgs.contains("--version\n9.9.9\n"), "the repair ran with \(repairArgs)")
        XCTAssertTrue(repairArgs.contains("--native-repair\n"))

        await waitForCanStart(installer)
        try FileManager.default.removeItem(atPath: argsFile)

        installer.start()
        XCTAssertEqual(installer.state, .downloading)
        XCTAssertEqual(installer.mode, .install)
        await waitForState(installer) { $0 == .succeeded(self.origin8444) }
        let installArgs = try String(contentsOfFile: argsFile, encoding: .utf8)
        XCTAssertTrue(installArgs.contains("--version\n8.8.8\n"), "the install ran with \(installArgs)")
        XCTAssertTrue(installArgs.contains("--native-install\n"))
        await waitForTemporaryInstallerToClear()
    }
}

/// Counts fetch calls from a @Sendable closure.
private final class FetchCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0

    func increment() {
        lock.lock()
        value += 1
        lock.unlock()
    }

    var count: Int {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

/// A fetch stand-in that holds until the test releases it. It does not honour
/// cancellation and records whether the calling task was cancelled.
private actor FetchGate {
    private var continuation: CheckedContinuation<Void, Never>?
    private var released = false
    private var entered = false
    private var entryWaiter: CheckedContinuation<Void, Never>?
    private(set) var sawCancelled: Bool?

    /// Suspends until the test releases the gate. `entered` is set before the
    /// suspension so a test can wait for the fetch to be genuinely in flight.
    func waitForRelease() async {
        entered = true
        entryWaiter?.resume()
        entryWaiter = nil
        if !released {
            await withCheckedContinuation { continuation = $0 }
        }
        sawCancelled = Task.isCancelled
    }

    /// Returns once `waitForRelease` has been called at least once.
    func waitUntilEntered() async {
        if entered { return }
        await withCheckedContinuation { entryWaiter = $0 }
    }

    func release() {
        released = true
        continuation?.resume()
        continuation = nil
    }
}
