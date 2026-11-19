import XCTest

@testable import CiaobotMac

/// A drainer that answers from a script, then from `fallback`. It records every
/// call, so a test reads what the gate asked the engine.
@MainActor
final class ScriptedEngineDrainer: EngineDraining {
    var script: [Result<DrainReading, DrainRefusal>] = []
    var fallback: Result<DrainReading, DrainRefusal> = .success(DrainReading(activeChats: 1, scheduleWork: 0))
    private(set) var drains: [(port: Int, owner: String)] = []
    private(set) var cancels: [(port: Int, owner: String)] = []

    func drain(port: Int, owner: String) async -> Result<DrainReading, DrainRefusal> {
        drains.append((port, owner))
        return script.isEmpty ? fallback : script.removeFirst()
    }

    func cancel(port: Int, owner: String) async {
        cancels.append((port, owner))
    }
}

/// A gate wired to fakes. Scheduled work is held, not run, so each test decides
/// when the next poll, retry or install happens.
@MainActor
private final class GateHarness {
    static let port = 8777
    var port: Int? = GateHarness.port
    let drainer = ScriptedEngineDrainer()
    private(set) var events: [String] = []
    private(set) var scheduled: [(delay: TimeInterval, work: () -> Void)] = []
    private(set) lazy var gate = UpdateInstallGate(
        localPort: { [unowned self] in self.port },
        drainer: drainer,
        owner: "sparkle-1.2.3",
        willInstall: { [unowned self] in self.events.append("willInstall") },
        schedule: { [unowned self] delay, work in self.scheduled.append((delay, work)) }
    )

    var installs: Int { events.filter { $0 == "install" }.count }
    var lastDelay: TimeInterval? { scheduled.last?.delay }

    func take() -> Bool {
        gate.takeControl { [unowned self] in self.record("install") }
    }

    func record(_ event: String) {
        events.append(event)
    }

    /// Lets queued main-actor work, such as a drain reply, run to completion.
    func settle() async {
        for _ in 0..<20 { await Task.yield() }
    }

    /// Runs the earliest held work, then lets its consequences settle.
    func fireNext() async {
        guard !scheduled.isEmpty else { return }
        let next = scheduled.removeFirst()
        next.work()
        await settle()
    }
}

@MainActor
final class UpdateInstallGateTests: XCTestCase {
    private let idle = Result<DrainReading, DrainRefusal>.success(DrainReading(activeChats: 0, scheduleWork: 0))
    private let busy = Result<DrainReading, DrainRefusal>.success(DrainReading(activeChats: 1, scheduleWork: 0))

    /// No local engine: the install runs on the next turn, as it always did,
    /// and nothing is drained.
    func testWithoutALocalEngineTheInstallRunsOnTheNextTurnWithoutDraining() async {
        let harness = GateHarness()
        harness.port = nil

        XCTAssertTrue(harness.take(), "Returning true stops Sparkle waiting for a quit.")
        await harness.settle()
        XCTAssertEqual(harness.events, [], "Nothing runs inside the delegate call.")

        await harness.fireNext()
        XCTAssertEqual(harness.events, ["willInstall", "install"])
        XCTAssertEqual(harness.drainer.drains.count, 0, "An external server or no engine is never drained.")
        XCTAssertEqual(harness.drainer.cancels.count, 0)
    }

    /// Three idle readings in a row are required. Two idle, then busy, then
    /// three idle installs only after the sixth reading.
    func testTheInstallWaitsForThreeIdleReadingsInARow() async {
        let harness = GateHarness()
        harness.drainer.script = [idle, idle, busy, idle, idle]
        harness.drainer.fallback = idle

        XCTAssertTrue(harness.take())
        await harness.settle()
        for _ in 0..<4 { await harness.fireNext() }
        XCTAssertEqual(harness.drainer.drains.count, 5)
        XCTAssertEqual(harness.installs, 0, "Five readings with a busy one in the middle are not enough.")

        await harness.fireNext()
        XCTAssertEqual(harness.drainer.drains.count, 6)
        XCTAssertEqual(harness.lastDelay, 0, "The sixth reading schedules the install for the next turn.")
        XCTAssertEqual(harness.installs, 0, "Nothing installs before that turn.")

        await harness.fireNext()
        XCTAssertEqual(harness.events, ["willInstall", "install"], "willInstall runs before Sparkle's handler.")
    }

    /// A reading without a schedule count is not idle, because the engine did
    /// not say whether scheduled work is running.
    func testAReadingWithoutAScheduleCountIsNotIdle() async {
        let harness = GateHarness()
        harness.drainer.fallback = .success(DrainReading(activeChats: 0, scheduleWork: nil))

        XCTAssertTrue(harness.take())
        await harness.settle()
        for _ in 0..<5 { await harness.fireNext() }

        XCTAssertEqual(harness.drainer.drains.count, 6)
        XCTAssertEqual(harness.installs, 0, "Six readings with no schedule count still never install.")
    }

    /// A drain that never settles is cancelled with the same owner after 600 s
    /// (120 five-second readings), and the whole gate retries after 1800 s.
    func testADrainThatNeverSettlesCancelsWithTheSameOwnerAndRetries() async {
        let harness = GateHarness()
        harness.drainer.fallback = busy

        XCTAssertTrue(harness.take())
        await harness.settle()
        var guardCount = 0
        while harness.lastDelay != UpdateInstallGate.retryAfter, guardCount < 500 {
            await harness.fireNext()
            guardCount += 1
        }

        XCTAssertEqual(harness.drainer.drains.count, 120)
        XCTAssertEqual(harness.drainer.cancels.count, 1)
        XCTAssertEqual(harness.drainer.cancels.first?.owner, "sparkle-1.2.3", "The cancel names the drain's owner.")
        XCTAssertEqual(harness.drainer.cancels.first?.port, GateHarness.port)
        XCTAssertEqual(harness.lastDelay, 1800)
        XCTAssertEqual(harness.installs, 0)

        await harness.fireNext()
        XCTAssertEqual(harness.drainer.drains.count, 121, "The retry starts a new drain.")
        XCTAssertEqual(harness.installs, 0)
    }

    func testABusyRefusalCancelsAndRetriesWithoutInstalling() async {
        let harness = GateHarness()
        harness.drainer.fallback = .failure(.busyRestart)

        XCTAssertTrue(harness.take())
        await harness.settle()

        XCTAssertEqual(harness.drainer.cancels.count, 1)
        XCTAssertEqual(harness.drainer.cancels.first?.owner, "sparkle-1.2.3")
        XCTAssertEqual(harness.lastDelay, 1800)
        XCTAssertEqual(harness.installs, 0)
    }

    /// Twelve failed polls in a row, one minute, cancel and retry. The gate
    /// never installs while the engine cannot be read.
    func testTwelveUnreachableReadingsCancelAndRetryNeverInstalling() async {
        let harness = GateHarness()
        harness.drainer.fallback = .failure(.unreachable)

        XCTAssertTrue(harness.take())
        await harness.settle()
        for _ in 0..<11 { await harness.fireNext() }

        XCTAssertEqual(harness.drainer.drains.count, 12)
        XCTAssertEqual(harness.drainer.cancels.count, 1)
        XCTAssertEqual(harness.lastDelay, 1800)
        XCTAssertEqual(harness.installs, 0)
    }

    /// Sparkle giving up cancels a drain the gate holds and stops the gate: a
    /// poll scheduled before the abort reads nothing.
    func testInstallAbortedCancelsAHeldDrainAndStopsTheGate() async {
        let harness = GateHarness()
        harness.drainer.fallback = busy

        XCTAssertTrue(harness.take())
        await harness.settle()
        harness.gate.installAborted()
        await harness.settle()

        XCTAssertEqual(harness.drainer.cancels.count, 1)
        XCTAssertEqual(harness.drainer.cancels.first?.owner, "sparkle-1.2.3")
        await harness.fireNext()
        XCTAssertEqual(harness.drainer.drains.count, 1, "An aborted gate never reads the engine again.")
        XCTAssertEqual(harness.events, [])
    }

    /// A second handler replaces the first and keeps the one gate: only one
    /// install is scheduled, and it runs the second handler.
    func testASecondTakeControlReplacesTheHandlerAndKeepsTheGate() async {
        let harness = GateHarness()
        harness.port = nil
        XCTAssertTrue(harness.gate.takeControl { harness.record("first") })
        XCTAssertTrue(harness.gate.takeControl { harness.record("second") })
        XCTAssertEqual(harness.scheduled.count, 1)

        await harness.fireNext()
        XCTAssertEqual(harness.events, ["willInstall", "second"])
    }
}

/// The real drain client over a `URLProtocol` stub: the request it sends, and
/// how it reads each answer.
@MainActor
final class EngineDrainClientTests: XCTestCase {
    override func setUp() {
        super.setUp()
        StubDrainProtocol.reset()
    }

    private func makeClient() -> EngineDrainClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubDrainProtocol.self]
        return EngineDrainClient(session: URLSession(configuration: configuration))
    }

    func testDrainPostsTheOwnerToTheLoopbackEngineWithoutCookiesOrCredentials() async throws {
        StubDrainProtocol.answer = (200, Data(#"{"draining":true,"active_chat_ids":["c-1"],"schedule_work_count":0}"#.utf8))

        let result = await makeClient().drain(port: 8777, owner: "sparkle-1.2.3")

        XCTAssertEqual(result, .success(DrainReading(activeChats: 1, scheduleWork: 0)))
        let request = try XCTUnwrap(StubDrainProtocol.requests.first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url?.absoluteString, "http://127.0.0.1:8777/api/admin/drain")
        let owner = try StubDrainProtocol.owner(of: request)
        XCTAssertEqual(owner, "sparkle-1.2.3")
        XCTAssertNil(request.value(forHTTPHeaderField: "Cookie"))
        XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
    }

    func testCancelPostsTheSameOwnerToTheCancelRoute() async throws {
        StubDrainProtocol.answer = (200, Data(#"{"draining":false}"#.utf8))

        await makeClient().cancel(port: 8777, owner: "sparkle-1.2.3")

        let request = try XCTUnwrap(StubDrainProtocol.requests.first)
        XCTAssertEqual(request.url?.absoluteString, "http://127.0.0.1:8777/api/admin/drain/cancel")
        let owner = try StubDrainProtocol.owner(of: request)
        XCTAssertEqual(owner, "sparkle-1.2.3")
    }

    func testAConflictIsABusyRestartAndOtherFailuresAreUnreachable() async {
        StubDrainProtocol.answer = (409, Data(#"{"error":"a restart is already draining"}"#.utf8))
        let busy = await makeClient().drain(port: 8777, owner: "sparkle-1.2.3")
        XCTAssertEqual(busy, .failure(.busyRestart))

        StubDrainProtocol.answer = (503, Data(#"{"error":"drain unavailable"}"#.utf8))
        let unavailable = await makeClient().drain(port: 8777, owner: "sparkle-1.2.3")
        XCTAssertEqual(unavailable, .failure(.unreachable))

        StubDrainProtocol.answer = (200, Data(#"{"draining":true}"#.utf8))
        let noChatList = await makeClient().drain(port: 8777, owner: "sparkle-1.2.3")
        XCTAssertEqual(noChatList, .failure(.unreachable), "An answer without active_chat_ids is not a reading.")

        StubDrainProtocol.fails = true
        let refused = await makeClient().drain(port: 8777, owner: "sparkle-1.2.3")
        XCTAssertEqual(refused, .failure(.unreachable))
    }

    func testAScheduleCountMustBeAWholeNumberToCountAsReported() {
        XCTAssertEqual(
            EngineDrainClient.reading(from: Data(#"{"active_chat_ids":[],"schedule_work_count":2}"#.utf8)),
            DrainReading(activeChats: 0, scheduleWork: 2)
        )
        XCTAssertEqual(
            EngineDrainClient.reading(from: Data(#"{"active_chat_ids":[]}"#.utf8)),
            DrainReading(activeChats: 0, scheduleWork: nil)
        )
        XCTAssertEqual(
            EngineDrainClient.reading(from: Data(#"{"active_chat_ids":[],"schedule_work_count":true}"#.utf8)),
            DrainReading(activeChats: 0, scheduleWork: nil),
            "A boolean is not a count."
        )
        XCTAssertNil(EngineDrainClient.reading(from: Data(#"{"draining":true}"#.utf8)))
    }
}

/// Answers every request with one canned status and body, and records the request.
private final class StubDrainProtocol: URLProtocol {
    static var answer: (Int, Data) = (200, Data())
    static var fails = false
    static var requests: [URLRequest] = []

    static func reset() {
        answer = (200, Data())
        fails = false
        requests = []
    }

    /// The `owner` a drain request carries. The body arrives as a stream when
    /// URLSession hands the request to a protocol.
    static func owner(of request: URLRequest) throws -> String? {
        var data = request.httpBody ?? Data()
        if data.isEmpty, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 1024)
            while stream.hasBytesAvailable {
                let read = stream.read(&buffer, maxLength: buffer.count)
                if read <= 0 { break }
                data.append(buffer, count: read)
            }
        }
        let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        return object?["owner"] as? String
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.requests.append(request)
        if Self.fails {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
            return
        }
        let (status, data) = Self.answer
        let response = HTTPURLResponse(
            url: request.url ?? URL(fileURLWithPath: "/"),
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
