import XCTest
@testable import CiaobotMac

/// FoundationEngineProcesses against harmless real children: shell scripts in a
/// temp directory standing in for the engine launcher. No engine is started.
final class EngineSupervisorProcessTests: XCTestCase {
    private var tmp: URL!
    private var spawned: [Int32] = []
    private let real = FoundationEngineProcesses()

    override func setUpWithError() throws {
        tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("EngineSupervisorProcessTests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        for pid in spawned { Darwin.kill(pid, SIGKILL) }
        try? FileManager.default.removeItem(at: tmp)
    }

    private func script(_ body: String) throws -> BundledEngineLaunch {
        let url = tmp.appendingPathComponent("launcher-\(UUID().uuidString).sh")
        try "#!/bin/sh\n\(body)\n".write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
        return BundledEngineLaunch(executable: url, arguments: [], environment: ["PATH": "/usr/bin:/bin"], workingDirectory: tmp)
    }

    private func awaitExit(_ pid: Int32) -> XCTestExpectation {
        let exited = expectation(description: "exit \(pid)")
        real.onExit = { [weak self] exitedPid, status in
            self?.statuses[exitedPid] = status
            if exitedPid == pid { exited.fulfill() }
        }
        return exited
    }
    private var statuses: [Int32: Int32] = [:]

    private func waitUntil(_ timeout: TimeInterval = 5, _ condition: () -> Bool) -> Bool {
        let end = Date().addingTimeInterval(timeout)
        while Date() < end {
            if condition() { return true }
            RunLoop.current.run(until: Date().addingTimeInterval(0.02))
        }
        return condition()
    }

    func testSpawnAppendsStdoutAndStderrToTheLogAndReportsTheExitStatus() throws {
        let log = tmp.appendingPathComponent("logs/engine.log")
        try FileManager.default.createDirectory(at: log.deletingLastPathComponent(), withIntermediateDirectories: true)
        try "old\n".write(to: log, atomically: true, encoding: .utf8)
        let pid = try real.spawn(script("echo to-out; echo to-err >&2; exit 7"), log: log)
        spawned.append(pid)
        wait(for: [awaitExit(pid)], timeout: 5)
        XCTAssertEqual(statuses[pid], 7)
        let text = try String(contentsOf: log, encoding: .utf8)
        XCTAssertTrue(text.hasPrefix("old\n"), "appended, not truncated: \(text)")
        XCTAssertTrue(text.contains("to-out"))
        XCTAssertTrue(text.contains("to-err"))
    }

    func testNewLogIsCreatedPrivate() throws {
        let log = tmp.appendingPathComponent("fresh.log")
        let pid = try real.spawn(script("exit 0"), log: log)
        spawned.append(pid)
        wait(for: [awaitExit(pid)], timeout: 5)
        let mode = try FileManager.default.attributesOfItem(atPath: log.path)[.posixPermissions] as? Int
        XCTAssertEqual(mode.map { $0 & 0o777 }, 0o600)
    }

    func testChildRunsInTheWorkingDirectoryWithNoStdin() throws {
        let log = tmp.appendingPathComponent("cwd.log")
        let pid = try real.spawn(script("pwd; cat; echo eof"), log: log)
        spawned.append(pid)
        wait(for: [awaitExit(pid)], timeout: 5)
        let text = try String(contentsOf: log, encoding: .utf8)
        XCTAssertTrue(text.contains(EngineSupervisor.canonical(tmp.path)), text)
        XCTAssertTrue(text.contains("eof"), "stdin is /dev/null, so cat ends at once")
    }

    func testSpawnFailureThrowsAndLeavesNoLogHandle() throws {
        let missing = BundledEngineLaunch(
            executable: tmp.appendingPathComponent("nope"), arguments: [], environment: [:], workingDirectory: tmp)
        XCTAssertThrowsError(try real.spawn(missing, log: tmp.appendingPathComponent("x.log")))
        let bad = BundledEngineLaunch(
            executable: URL(fileURLWithPath: "/bin/sh"), arguments: [], environment: [:],
            workingDirectory: tmp.appendingPathComponent("no-such-dir"))
        XCTAssertThrowsError(try real.spawn(bad, log: tmp.appendingPathComponent("y.log")))
    }

    func testChildLeadsItsOwnSessionAndKillReachesItsDescendants() throws {
        let pidFile = tmp.appendingPathComponent("grandchild.pid")
        let launch = try script("/bin/sleep 60 &\necho $! > '\(pidFile.path)'\nwait")
        let pid = try real.spawn(launch, log: tmp.appendingPathComponent("group.log"))
        spawned.append(pid)
        let exited = awaitExit(pid)
        XCTAssertEqual(getsid(pid), pid, "own session")
        XCTAssertEqual(getpgid(pid), pid, "group leader")
        XCTAssertNotEqual(getsid(pid), getsid(0))
        XCTAssertTrue(waitUntil { (try? String(contentsOf: pidFile, encoding: .utf8))?.contains("\n") == true })
        let grandchild = try XCTUnwrap(Int32(try String(contentsOf: pidFile, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)))
        spawned.append(grandchild)
        XCTAssertNotNil(real.inspect(grandchild))
        real.kill(pid)
        wait(for: [exited], timeout: 5)
        XCTAssertEqual(statuses[pid], 128 + SIGKILL)
        XCTAssertTrue(waitUntil { real.inspect(grandchild) == nil }, "killpg took the descendant too")
    }

    func testTerminateSendsSigtermToTheChild() throws {
        let pid = try real.spawn(script("exec /bin/sleep 60"), log: tmp.appendingPathComponent("t.log"))
        spawned.append(pid)
        let exited = awaitExit(pid)
        real.terminate(pid)
        wait(for: [exited], timeout: 5)
        XCTAssertEqual(statuses[pid], 128 + SIGTERM)
    }

    func testSignalsRefuseLowPids() {
        // Would signal launchd or our whole process group if the guard were missing.
        real.terminate(1)
        real.kill(1)
        real.terminate(0)
        real.kill(0)
        real.kill(-1)
    }
}

// MARK: Identity probe

private final class StubProtocol: URLProtocol {
    static var status = 200
    static var chunks: [Data] = []
    static var hangs = false
    static var sentChunks = 0
    private var stopped = false

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let url = request.url!
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: url, statusCode: Self.status, httpVersion: nil, headerFields: nil)!,
                            cacheStoragePolicy: .notAllowed)
        if Self.hangs { return }
        let chunks = Self.chunks
        DispatchQueue.global().async {
            for chunk in chunks {
                if self.stopped { return }
                Self.sentChunks += 1
                self.client?.urlProtocol(self, didLoad: chunk)
                Thread.sleep(forTimeInterval: 0.02)
            }
            if !self.stopped { self.client?.urlProtocolDidFinishLoading(self) }
        }
    }

    override func stopLoading() { stopped = true }
}

final class EngineSupervisorIdentityProbeTests: XCTestCase {
    private let origin = Origin("http://127.0.0.1:8777")!

    override func setUp() {
        StubProtocol.status = 200
        StubProtocol.chunks = []
        StubProtocol.hangs = false
        StubProtocol.sentChunks = 0
    }

    private func probe(timeout: TimeInterval = 5) -> EngineIdentityProbe {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubProtocol.self]
        return EngineIdentityProbe(configuration: config, timeout: timeout)
    }

    private func fetch(_ probe: EngineIdentityProbe) -> EngineIdentity? {
        var result: EngineIdentity??
        let done = expectation(description: "identity")
        probe.identity(of: origin) { result = .some($0); done.fulfill() }
        wait(for: [done], timeout: 10)
        return result ?? nil
    }

    func testParseTable() {
        func parse(_ status: Int, _ json: String) -> EngineIdentity? {
            EngineIdentityProbe.parse(status: status, data: Data(json.utf8))
        }
        XCTAssertEqual(parse(200, #"{"startup_id":"abc","pid":42}"#), EngineIdentity(pid: 42, startupID: "abc"))
        XCTAssertEqual(parse(204, #"{"startup_id":"abc","pid":42}"#), EngineIdentity(pid: 42, startupID: "abc"))
        XCTAssertNil(parse(500, #"{"startup_id":"abc","pid":42}"#))
        XCTAssertNil(parse(404, #"{"startup_id":"abc","pid":42}"#))
        XCTAssertNil(parse(0, #"{"startup_id":"abc","pid":42}"#))
        XCTAssertNil(parse(200, #"{"startup_id":"","pid":42}"#))
        XCTAssertNil(parse(200, #"{"pid":42}"#))
        XCTAssertNil(parse(200, #"{"startup_id":"abc"}"#))
        XCTAssertNil(parse(200, #"{"startup_id":"abc","pid":true}"#), "JSON true must not decode as pid 1")
        XCTAssertNil(parse(200, #"{"startup_id":"abc","pid":1}"#))
        XCTAssertNil(parse(200, #"{"startup_id":"abc","pid":-5}"#))
        XCTAssertNil(parse(200, #"{"startup_id":"abc","pid":4.5}"#))
        XCTAssertNil(parse(200, #"{"startup_id":"abc","pid":"42"}"#))
        XCTAssertNil(parse(200, #"{"startup_id":"abc","pid":99999999999}"#))
        XCTAssertNil(parse(200, "not json"))
        XCTAssertNil(parse(200, "[1,2]"))
        XCTAssertNil(parse(200, ""))
    }

    func testParseRefusesAnOversizedBody() {
        let pad = String(repeating: "x", count: Probe.maximumResponseBytes)
        let json = #"{"startup_id":"abc","pid":42,"pad":"\#(pad)"}"#
        XCTAssertNil(EngineIdentityProbe.parse(status: 200, data: Data(json.utf8)))
    }

    func testSuccessfulFetch() {
        StubProtocol.chunks = [Data(#"{"startup_id":"abc","pid":42}"#.utf8)]
        XCTAssertEqual(fetch(probe()), EngineIdentity(pid: 42, startupID: "abc"))
    }

    func testServerErrorAndMalformedBodiesAreNil() {
        StubProtocol.status = 503
        StubProtocol.chunks = [Data(#"{"startup_id":"abc","pid":42}"#.utf8)]
        XCTAssertNil(fetch(probe()))
        StubProtocol.status = 200
        StubProtocol.chunks = [Data("{oops".utf8)]
        XCTAssertNil(fetch(probe()))
        StubProtocol.chunks = [Data(#"{"startup_id":"abc","pid":true}"#.utf8)]
        XCTAssertNil(fetch(probe()))
    }

    func testOversizedBodyIsCutOffWhileStreaming() {
        let chunk = Data(repeating: UInt8(ascii: "x"), count: 16 * 1024)
        StubProtocol.chunks = Array(repeating: chunk, count: 40)
        XCTAssertNil(fetch(probe()))
        XCTAssertLessThan(StubProtocol.sentChunks, 40, "the request was cancelled once the cap was passed")
    }

    func testServerThatNeverAnswersIsCutOffByTheOverallTimer() {
        StubProtocol.hangs = true
        let started = Date()
        XCTAssertNil(fetch(probe(timeout: 0.4)))
        XCTAssertLessThan(Date().timeIntervalSince(started), 3)
    }

    func testServerThatDripsBytesIsCutOffBeforeItFinishes() {
        StubProtocol.chunks = Array(repeating: Data(" ".utf8), count: 250)  // about 5 s of dripping
        let started = Date()
        XCTAssertNil(fetch(probe(timeout: 0.4)))
        XCTAssertLessThan(Date().timeIntervalSince(started), 2.5)
        XCTAssertLessThan(StubProtocol.sentChunks, 250)
    }

    func testDefaultTimeoutIsFiveSeconds() {
        XCTAssertEqual(EngineIdentityProbe.defaultTimeout, 5)
    }
}
