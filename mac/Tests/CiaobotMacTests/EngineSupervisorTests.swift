import XCTest
@testable import CiaobotMac

private final class FakeProcesses: EngineProcessRunning {
    var onExit: ((Int32, Int32) -> Void)?
    var spawns: [(launch: BundledEngineLaunch, log: URL)] = []
    var terminated: [Int32] = []
    var killed: [Int32] = []
    var alive: [Int32: EngineProcessInfo] = [:]
    var nextPID: Int32 = 5000
    /// Pids that disappear when terminated.
    var diesOnTerminate = true

    func spawn(_ launch: BundledEngineLaunch, log: URL) throws -> Int32 {
        spawns.append((launch, log))
        nextPID += 1
        return nextPID
    }
    func terminate(_ pid: Int32) {
        terminated.append(pid)
        if diesOnTerminate { alive[pid] = nil }
    }
    func kill(_ pid: Int32) { killed.append(pid); alive[pid] = nil }
    func inspect(_ pid: Int32) -> EngineProcessInfo? { alive[pid] }
}

private final class FakeIdentity: EngineIdentityChecking {
    var answer: EngineIdentity?
    var origins: [Origin] = []
    func identity(of origin: Origin, completion: @escaping (EngineIdentity?) -> Void) {
        origins.append(origin)
        completion(answer)
    }
}

/// A clock and scheduler in one: `advance` moves time and runs what falls due.
private final class FakeTime {
    var now = Date(timeIntervalSince1970: 1_800_000_000)
    private var queue: [(due: Date, work: () -> Void)] = []

    func schedule(_ delay: TimeInterval, _ work: @escaping () -> Void) {
        queue.append((now.addingTimeInterval(delay), work))
    }

    func advance(_ seconds: TimeInterval, step: TimeInterval = 0.25) {
        let end = now.addingTimeInterval(seconds)
        while now < end {
            now = min(now.addingTimeInterval(step), end)
            while let index = queue.firstIndex(where: { $0.due <= now }) {
                queue.remove(at: index).work()
            }
        }
    }
}

@MainActor
final class EngineSupervisorTests: XCTestCase {
    private var tmp: URL!
    private var home: EngineHome!
    private var engineRoot: URL!
    private let processes = FakeProcesses()
    private let identity = FakeIdentity()
    private let time = FakeTime()

    override func setUpWithError() throws {
        tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("EngineSupervisorTests-\(UUID().uuidString)", isDirectory: true)
        let physical = tmp.resolvingSymlinksInPath()
        try FileManager.default.createDirectory(at: physical, withIntermediateDirectories: true)
        tmp = physical
        home = EngineHome(home: tmp.appendingPathComponent("home", isDirectory: true))
        try FileManager.default.createDirectory(at: home.stateDir, withIntermediateDirectories: true)
        engineRoot = tmp.appendingPathComponent("Ciaobot.app/Contents/Resources/engine", isDirectory: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: tmp)
    }

    private func makeSupervisor(
        engine: BundledEngine? = nil,
        environment: [String: String] = ["PATH": "/usr/bin"]
    ) -> EngineSupervisor {
        EngineSupervisor(
            engine: engine ?? BundledEngine(root: engineRoot),
            home: home,
            processes: processes,
            identity: identity,
            environment: environment,
            clock: { [time] in time.now },
            schedule: { [time] delay, work in time.schedule(delay, work) }
        )
    }

    private func writeReady(pid: Int32, port: Int = 8777, startupID: String = "abc123") throws {
        let json = #"{"schema":1,"pid":\#(pid),"port":\#(port),"startup_id":"\#(startupID)","workspace":"/w","bootstrap":false}"#
        try json.write(to: home.readyRecord, atomically: true, encoding: .utf8)
    }

    private func writeLog(_ text: String) throws {
        try FileManager.default.createDirectory(at: home.logFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        try text.write(to: home.logFile, atomically: true, encoding: .utf8)
    }

    private func bringUp(_ supervisor: EngineSupervisor, port: Int = 8777, startupID: String = "abc123") throws -> Int32 {
        supervisor.start()
        let pid = processes.nextPID
        XCTAssertEqual(processes.spawns.count >= 1, true)
        try writeReady(pid: pid, port: port, startupID: startupID)
        identity.answer = EngineIdentity(pid: pid, startupID: startupID)
        time.advance(0.5)
        return pid
    }

    // MARK: EngineMode

    func testInitialModeTable() {
        let local = Origin("http://127.0.0.1:8443")
        let remote = Origin("https://ciao.example.com")
        XCTAssertEqual(EngineMode.initial(saved: "external", lastOrigin: local, hostIsArm64: true), .external)
        XCTAssertEqual(EngineMode.initial(saved: "local", lastOrigin: remote, hostIsArm64: true), .local)
        XCTAssertEqual(EngineMode.initial(saved: nil, lastOrigin: nil, hostIsArm64: false), .external)
        XCTAssertEqual(EngineMode.initial(saved: nil, lastOrigin: local, hostIsArm64: false), .external)
        XCTAssertEqual(EngineMode.initial(saved: nil, lastOrigin: remote, hostIsArm64: true), .external)
        XCTAssertEqual(EngineMode.initial(saved: nil, lastOrigin: local, hostIsArm64: true), .local)
        XCTAssertEqual(EngineMode.initial(saved: nil, lastOrigin: nil, hostIsArm64: true), .local)
        XCTAssertEqual(EngineMode.initial(saved: "bogus", lastOrigin: nil, hostIsArm64: true), .local)
    }

    func testModeStoreUsesTheDocumentedKeyInAnIsolatedSuite() throws {
        let name = IsolatedDefaults.makeName("EngineMode")
        defer { IsolatedDefaults.remove(name) }
        let defaults = try XCTUnwrap(UserDefaults(suiteName: name))
        let store = UserDefaultsEngineModeStore(defaults: defaults)
        XCTAssertNil(store.mode)
        store.mode = .external
        XCTAssertEqual(defaults.string(forKey: "CiaobotEngineMode"), "external")
        XCTAssertEqual(store.mode, .external)
        store.mode = nil
        XCTAssertNil(store.mode)
    }

    func testHomePathsDeriveFromTheInjectedHome() {
        let h = EngineHome(home: URL(fileURLWithPath: "/tmp/h", isDirectory: true))
        XCTAssertEqual(h.stateDir.path, "/tmp/h/Library/Application Support/Ciaobot/engine")
        XCTAssertEqual(h.readyRecord.path, "/tmp/h/Library/Application Support/Ciaobot/engine/ready.json")
        XCTAssertEqual(h.pointer.lastPathComponent, "workspace.json")
        XCTAssertEqual(h.takeoverDir.lastPathComponent, "takeover")
        XCTAssertEqual(h.logFile.path, "/tmp/h/Library/Logs/Ciaobot/engine.log")
    }

    // MARK: Spawn

    func testSpawnRunsAppEngineWithInjectedHomeAndNoEngineVariables() throws {
        let supervisor = makeSupervisor(environment: [
            "HOME": "/Users/someone-else", "PATH": "/usr/bin", "CIAO_WORKSPACE": "/leak", "PWA_X": "1",
        ])
        supervisor.start()
        let spawn = try XCTUnwrap(processes.spawns.first)
        XCTAssertEqual(spawn.launch.arguments, ["run", "--app-engine"])
        XCTAssertEqual(spawn.launch.executable, engineRoot.appendingPathComponent("bin/ciao"))
        XCTAssertEqual(spawn.launch.environment["HOME"], home.home.path)
        XCTAssertEqual(spawn.launch.environment["PATH"], "/usr/bin")
        XCTAssertTrue(spawn.launch.environment.keys.allSatisfy { !$0.hasPrefix("CIAO_") && !$0.hasPrefix("PWA_") })
        XCTAssertEqual(spawn.launch.workingDirectory, home.home)
        XCTAssertEqual(spawn.log, home.logFile)
        XCTAssertEqual(supervisor.state, .starting)
    }

    func testLogOverTwentyMegabytesRotatesBeforeSpawn() throws {
        try FileManager.default.createDirectory(at: home.logFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: home.logFile.path, contents: Data(count: EngineSupervisor.maximumLogBytes + 1))
        makeSupervisor().start()
        XCTAssertFalse(FileManager.default.fileExists(atPath: home.logFile.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: home.logFile.path + ".1"))
    }

    func testSmallLogIsNotRotated() throws {
        try writeLog("old line\n")
        makeSupervisor().start()
        XCTAssertTrue(FileManager.default.fileExists(atPath: home.logFile.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: home.logFile.path + ".1"))
    }

    func testMissingEngineFailsWithoutSpawning() {
        let supervisor = EngineSupervisor(
            engine: nil, home: home, processes: processes, identity: identity,
            clock: { [time] in time.now }, schedule: { [time] d, w in time.schedule(d, w) })
        supervisor.start()
        XCTAssertEqual(supervisor.state, .failed(EngineSupervisor.unavailableMessage))
        XCTAssertTrue(processes.spawns.isEmpty)
    }

    // MARK: Readiness

    func testRunningRequiresPidAndStartupIDToMatch() throws {
        let supervisor = makeSupervisor()
        supervisor.start()
        let pid = processes.nextPID
        try writeReady(pid: pid)
        identity.answer = EngineIdentity(pid: pid, startupID: "other")
        time.advance(2)
        XCTAssertEqual(supervisor.state, .starting, "a different startup id is a different engine")
        identity.answer = EngineIdentity(pid: pid + 1, startupID: "abc123")
        time.advance(2)
        XCTAssertEqual(supervisor.state, .starting, "a different pid is a different engine")
        identity.answer = EngineIdentity(pid: pid, startupID: "abc123")
        time.advance(1)
        XCTAssertEqual(supervisor.state, .running(origin: Origin("http://127.0.0.1:8777")!, pid: pid, startupID: "abc123"))
        XCTAssertEqual(identity.origins.first?.url.absoluteString, "http://127.0.0.1:8777")
    }

    func testReadyRecordForAnotherPidIsIgnored() throws {
        let supervisor = makeSupervisor()
        supervisor.start()
        try writeReady(pid: processes.nextPID + 99)
        identity.answer = EngineIdentity(pid: processes.nextPID + 99, startupID: "abc123")
        time.advance(3)
        XCTAssertEqual(supervisor.state, .starting)
        XCTAssertTrue(identity.origins.isEmpty, "a stale record is not even probed")
    }

    func testReadinessTimeoutFailsAndTerminatesTheChild() throws {
        try writeLog("starting\nTraceback boom\n")
        let supervisor = makeSupervisor()
        supervisor.start()
        let pid = processes.nextPID
        time.advance(89)
        XCTAssertEqual(supervisor.state, .starting)
        time.advance(2)
        XCTAssertEqual(supervisor.state, .failed(EngineSupervisor.startTimeoutMessage))
        XCTAssertEqual(processes.terminated, [pid])
        // The late exit of that child must not start a retry.
        processes.onExit?(pid, 1)
        time.advance(60)
        XCTAssertEqual(processes.spawns.count, 1)
    }

    // MARK: Exit and retry

    func testExit69IsPortInUseWithoutRetry() throws {
        let supervisor = makeSupervisor()
        let pid = try bringUp(supervisor, port: 8443)
        processes.onExit?(pid, 69)
        XCTAssertEqual(supervisor.state, .portInUse(8443))
        time.advance(60)
        XCTAssertEqual(processes.spawns.count, 1)
    }

    func testBackoffSchedulesAndFourthExitFails() throws {
        try writeLog("noise\n\n  ModuleNotFoundError: no module named x  \n\n")
        let supervisor = makeSupervisor()
        supervisor.start()
        // First exit: respawn after 2 s, not before.
        processes.onExit?(processes.nextPID, 1)
        XCTAssertEqual(supervisor.state, .starting)
        time.advance(1.75)
        XCTAssertEqual(processes.spawns.count, 1)
        time.advance(0.5)
        XCTAssertEqual(processes.spawns.count, 2)
        // Second exit: 5 s.
        processes.onExit?(processes.nextPID, 1)
        time.advance(4.75)
        XCTAssertEqual(processes.spawns.count, 2)
        time.advance(0.5)
        XCTAssertEqual(processes.spawns.count, 3)
        // Third exit: 15 s.
        processes.onExit?(processes.nextPID, 1)
        time.advance(14.75)
        XCTAssertEqual(processes.spawns.count, 3)
        time.advance(0.5)
        XCTAssertEqual(processes.spawns.count, 4)
        // Fourth exit inside five minutes: failed with the last log line, no more spawns.
        processes.onExit?(processes.nextPID, 1)
        XCTAssertEqual(supervisor.state, .failed("ModuleNotFoundError: no module named x"))
        time.advance(120)
        XCTAssertEqual(processes.spawns.count, 4)
    }

    func testFailureMessageIsBoundedToTwoHundredCharacters() throws {
        try writeLog(String(repeating: "x", count: 500) + "\n")
        let supervisor = makeSupervisor()
        supervisor.start()
        for _ in 0..<4 {
            processes.onExit?(processes.nextPID, 1)
            time.advance(16)
        }
        guard case .failed(let message) = supervisor.state else { return XCTFail("\(supervisor.state)") }
        XCTAssertEqual(message.count, 200)
    }

    func testExitsOlderThanFiveMinutesDoNotCount() throws {
        let supervisor = makeSupervisor()
        supervisor.start()
        for _ in 0..<3 {
            processes.onExit?(processes.nextPID, 1)
            time.advance(16)
        }
        XCTAssertEqual(processes.spawns.count, 4)
        try writeReady(pid: processes.nextPID)
        identity.answer = EngineIdentity(pid: processes.nextPID, startupID: "abc123")
        time.advance(400)
        guard case .running = supervisor.state else { return XCTFail("\(supervisor.state)") }
        processes.onExit?(processes.nextPID, 1)
        XCTAssertEqual(supervisor.state, .starting, "the old failures aged out")
        time.advance(3)
        XCTAssertEqual(processes.spawns.count, 5)
    }

    func testRestartAfterFailureClearsHistoryAndSpawns() throws {
        let supervisor = makeSupervisor()
        supervisor.start()
        for _ in 0..<4 {
            processes.onExit?(processes.nextPID, 1)
            time.advance(16)
        }
        guard case .failed = supervisor.state else { return XCTFail("\(supervisor.state)") }
        supervisor.restartAfterFailure()
        XCTAssertEqual(supervisor.state, .starting)
        XCTAssertEqual(processes.spawns.count, 5)
        processes.onExit?(processes.nextPID, 1)
        XCTAssertEqual(supervisor.state, .starting, "history restarted from zero")
    }

    // MARK: Orphans

    private func orphanSetup(parent: Int32, path: String?) throws -> Int32 {
        let orphan: Int32 = 4242
        try writeReady(pid: orphan)
        processes.alive[orphan] = EngineProcessInfo(parentPID: parent, path: path)
        return orphan
    }

    func testOrphanWithPpid1UnderTheRootIsReapedThenStarted() throws {
        let orphan = try orphanSetup(parent: 1, path: engineRoot.appendingPathComponent("bin/python3.12").path)
        let supervisor = makeSupervisor()
        supervisor.start()
        XCTAssertEqual(processes.terminated, [orphan])
        time.advance(1)
        XCTAssertEqual(processes.spawns.count, 1)
        XCTAssertTrue(processes.killed.isEmpty)
    }

    func testOrphanThatIgnoresSigtermIsKilledAfterTheGrace() throws {
        let orphan = try orphanSetup(parent: 1, path: engineRoot.appendingPathComponent("bin/python3.12").path)
        processes.diesOnTerminate = false
        let supervisor = makeSupervisor()
        supervisor.start()
        time.advance(34)
        XCTAssertTrue(processes.spawns.isEmpty, "waits for the grace")
        time.advance(2)
        XCTAssertEqual(processes.killed, [orphan])
        XCTAssertEqual(processes.spawns.count, 1)
    }

    func testLivePidWithAnotherParentIsNeverKilled() throws {
        _ = try orphanSetup(parent: 777, path: engineRoot.appendingPathComponent("bin/python3.12").path)
        let supervisor = makeSupervisor()
        supervisor.start()
        XCTAssertEqual(supervisor.state, .failed(EngineSupervisor.anotherRunningMessage))
        XCTAssertTrue(processes.terminated.isEmpty)
        XCTAssertTrue(processes.killed.isEmpty)
        XCTAssertTrue(processes.spawns.isEmpty)
    }

    func testLivePidOutsideTheRootIsNeverKilled() throws {
        _ = try orphanSetup(parent: 1, path: "/Applications/Other.app/Contents/Resources/engine/bin/python3.12")
        let supervisor = makeSupervisor()
        supervisor.start()
        XCTAssertEqual(supervisor.state, .failed(EngineSupervisor.anotherRunningMessage))
        XCTAssertTrue(processes.terminated.isEmpty)
        XCTAssertTrue(processes.spawns.isEmpty)
    }

    func testPathThatOnlySharesTheRootPrefixIsOutsideTheRoot() throws {
        _ = try orphanSetup(parent: 1, path: engineRoot.path + "-evil/bin/python3.12")
        let supervisor = makeSupervisor()
        supervisor.start()
        XCTAssertEqual(supervisor.state, .failed(EngineSupervisor.anotherRunningMessage))
        XCTAssertTrue(processes.terminated.isEmpty)
    }

    func testDeadPidInTheRecordIsIgnored() throws {
        try writeReady(pid: 4242)
        makeSupervisor().start()
        XCTAssertEqual(processes.spawns.count, 1)
        XCTAssertTrue(processes.terminated.isEmpty)
    }

    // MARK: Stop

    func testStopSendsSigtermThenSigkillOnlyAfterTheGrace() throws {
        let supervisor = makeSupervisor()
        let pid = try bringUp(supervisor)
        var completed = 0
        supervisor.stop(grace: 35) { completed += 1 }
        XCTAssertEqual(supervisor.state, .stopping)
        XCTAssertEqual(processes.terminated, [pid])
        time.advance(34)
        XCTAssertTrue(processes.killed.isEmpty)
        time.advance(2)
        XCTAssertEqual(processes.killed, [pid])
        XCTAssertEqual(completed, 0, "completion waits for the exit")
        processes.onExit?(pid, 9)
        XCTAssertEqual(supervisor.state, .stopped)
        XCTAssertEqual(completed, 1)
        time.advance(60)
        XCTAssertEqual(processes.spawns.count, 1, "a stop never respawns")
    }

    func testGracefulExitWithinTheGraceSkipsSigkill() throws {
        let supervisor = makeSupervisor()
        let pid = try bringUp(supervisor)
        var completed = 0
        supervisor.stop(grace: 35) { completed += 1 }
        time.advance(5)
        processes.onExit?(pid, 0)
        time.advance(60)
        XCTAssertTrue(processes.killed.isEmpty)
        XCTAssertEqual(completed, 1)
        XCTAssertEqual(supervisor.state, .stopped)
    }

    func testStopWithNothingRunningCompletesAtOnce() {
        let supervisor = makeSupervisor()
        var completed = 0
        supervisor.stop { completed += 1 }
        XCTAssertEqual(completed, 1)
        XCTAssertEqual(supervisor.state, .stopped)
    }

    func testStopDuringBackoffCancelsTheRetry() throws {
        let supervisor = makeSupervisor()
        supervisor.start()
        processes.onExit?(processes.nextPID, 1)
        var completed = 0
        supervisor.stop { completed += 1 }
        time.advance(30)
        XCTAssertEqual(completed, 1)
        XCTAssertEqual(processes.spawns.count, 1)
        XCTAssertEqual(supervisor.state, .stopped)
    }

    func testStartIsIgnoredWhileStartingOrRunning() throws {
        let supervisor = makeSupervisor()
        let pid = try bringUp(supervisor)
        supervisor.start()
        XCTAssertEqual(processes.spawns.count, 1)
        XCTAssertEqual(supervisor.state, .running(origin: Origin("http://127.0.0.1:8777")!, pid: pid, startupID: "abc123"))
    }

    // MARK: Running

    func testPortChangePublishesANewOrigin() throws {
        let supervisor = makeSupervisor()
        let pid = try bringUp(supervisor)
        try writeReady(pid: pid, port: 9001)
        identity.answer = EngineIdentity(pid: pid, startupID: "abc123")
        time.advance(5.5)
        XCTAssertEqual(supervisor.state, .running(origin: Origin("http://127.0.0.1:9001")!, pid: pid, startupID: "abc123"))
    }

    func testNewStartupIDIsProbedAgainBeforeItIsPublished() throws {
        let supervisor = makeSupervisor()
        let pid = try bringUp(supervisor)
        try writeReady(pid: pid, startupID: "second")
        identity.answer = EngineIdentity(pid: pid, startupID: "abc123")  // still the old engine answering
        time.advance(5.5)
        XCTAssertEqual(supervisor.state, .running(origin: Origin("http://127.0.0.1:8777")!, pid: pid, startupID: "abc123"))
        identity.answer = EngineIdentity(pid: pid, startupID: "second")
        time.advance(5.5)
        XCTAssertEqual(supervisor.state, .running(origin: Origin("http://127.0.0.1:8777")!, pid: pid, startupID: "second"))
    }

    // MARK: Real process inspection

    func testRealInspectionReportsThisProcessAndNothingForADeadPid() throws {
        let real = FoundationEngineProcesses()
        let me = ProcessInfo.processInfo.processIdentifier
        let info = try XCTUnwrap(real.inspect(me))
        XCTAssertEqual(info.parentPID, getppid())
        XCTAssertNotNil(info.path)
        XCTAssertNil(real.inspect(Int32.max - 1))
    }
}
