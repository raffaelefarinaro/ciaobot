import Darwin
import XCTest

@testable import CiaobotMac

/// A drainer that answers from a script, then from `fallback`. It records every
/// call, so a test reads what the gate asked the engine.
@MainActor
final class ScriptedEngineDrainer: EngineDraining {
    var script: [Result<DrainReading, DrainRefusal>] = []
    var fallback: Result<DrainReading, DrainRefusal> = .success(DrainReading(activeChats: 1, scheduleWork: 0))
    /// Cancel answers in order; once empty, every cancel is confirmed.
    var cancelResults: [Bool] = []
    /// Runs inside each drain call before it answers. A test spends clock time here.
    var onDrain: (() -> Void)?
    private(set) var drains: [(port: Int, owner: String)] = []
    private(set) var cancels: [(port: Int, owner: String)] = []
    /// Every call in order, as `drain <owner>` or `cancel <owner>`.
    private(set) var calls: [String] = []

    func drain(port: Int, owner: String) async -> Result<DrainReading, DrainRefusal> {
        drains.append((port, owner))
        calls.append("drain \(owner)")
        onDrain?()
        return script.isEmpty ? fallback : script.removeFirst()
    }

    func cancel(port: Int, owner: String) async -> Bool {
        cancels.append((port, owner))
        calls.append("cancel \(owner)")
        return cancelResults.isEmpty ? true : cancelResults.removeFirst()
    }
}

/// A drainer whose drain answers only when the test says so, so a reply can
/// arrive after its attempt has ended.
@MainActor
private final class HeldEngineDrainer: EngineDraining {
    private(set) var cancels: [(port: Int, owner: String)] = []
    private var pending: [CheckedContinuation<Result<DrainReading, DrainRefusal>, Never>] = []

    var inFlight: Int { pending.count }

    func drain(port: Int, owner: String) async -> Result<DrainReading, DrainRefusal> {
        await withCheckedContinuation { pending.append($0) }
    }

    func answerOldest(_ result: Result<DrainReading, DrainRefusal>) {
        pending.removeFirst().resume(returning: result)
    }

    func cancel(port: Int, owner: String) async -> Bool {
        cancels.append((port, owner))
        return true
    }
}

/// Lets queued main-actor work, such as a drain reply, run to completion.
@MainActor
func settleMainActor() async {
    for _ in 0..<20 { await Task.yield() }
}

/// A gate wired to fakes. Scheduled work is held, not run, so each test decides
/// when the next poll, retry or install happens. Firing work advances the fake
/// clock by its delay, as time would pass.
@MainActor
private final class GateHarness {
    static let port = 8777
    var state: LocalEngineState = .running(port: GateHarness.port)
    var now: TimeInterval = 0
    let drainer = ScriptedEngineDrainer()
    /// A ledger of this harness's own, so drains counted by one test never
    /// reach another.
    let ledger = DrainLedger()
    private(set) var events: [String] = []
    private(set) var scheduled: [(delay: TimeInterval, work: () -> Void)] = []
    private(set) lazy var gate = UpdateInstallGate(
        localState: { [unowned self] in self.state },
        drainer: drainer,
        owner: "sparkle-1.2.3",
        willInstall: { [unowned self] in self.events.append("willInstall") },
        schedule: { [unowned self] delay, work in self.scheduled.append((delay, work)) },
        clock: { [unowned self] in self.now },
        ledger: ledger
    )

    var installs: Int { events.filter { $0 == "install" }.count }
    var lastDelay: TimeInterval? { scheduled.last?.delay }

    func take() -> Bool {
        gate.takeControl { [unowned self] in self.record("install") }
    }

    func record(_ event: String) {
        events.append(event)
    }

    func settle() async {
        await settleMainActor()
    }

    /// Drops work already held, such as a poll the gate scheduled before an
    /// abort (it does nothing once its attempt has ended), so a test reads only
    /// the retries it starts.
    func dropHeldWork() {
        scheduled.removeAll()
    }

    /// Runs the earliest held work, then lets its consequences settle.
    func fireNext() async {
        guard !scheduled.isEmpty else { return }
        let next = scheduled.removeFirst()
        now += next.delay
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
        harness.state = .external

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

    /// A drain that never settles is cancelled with the same owner once the
    /// clock reaches 600 s, and the whole gate retries after 1800 s. Readings
    /// are five seconds apart, so the reading at 600 s is the 121st.
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

        XCTAssertEqual(harness.drainer.drains.count, 121)
        XCTAssertEqual(harness.drainer.cancels.count, 1)
        XCTAssertEqual(harness.drainer.cancels.first?.owner, "sparkle-1.2.3", "The cancel names the drain's owner.")
        XCTAssertEqual(harness.drainer.cancels.first?.port, GateHarness.port)
        XCTAssertEqual(harness.lastDelay, 1800)
        XCTAssertEqual(harness.installs, 0)

        await harness.fireNext()
        XCTAssertEqual(harness.drainer.drains.count, 122, "The retry starts a new drain.")
        XCTAssertEqual(harness.installs, 0)
    }

    /// The drain timeout is counted on the clock, not in poll intervals. Each
    /// drain here takes 25 s on top of the 5 s poll, so 600 s is reached after
    /// 21 readings rather than 121.
    func testTheDrainTimeoutIsCountedOnTheClock() async {
        let harness = GateHarness()
        harness.drainer.fallback = busy
        harness.drainer.onDrain = { [unowned harness] in harness.now += 25 }

        XCTAssertTrue(harness.take())
        await harness.settle()
        var guardCount = 0
        while harness.lastDelay != UpdateInstallGate.retryAfter, guardCount < 500 {
            await harness.fireNext()
            guardCount += 1
        }

        XCTAssertEqual(harness.drainer.drains.count, 21)
        XCTAssertEqual(harness.drainer.cancels.count, 1)
        XCTAssertEqual(harness.lastDelay, 1800)
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

    /// An unreachable reading breaks the idle streak: idle, idle, unreachable,
    /// idle, idle is not an install. One more idle reading is.
    func testAnUnreachableReadingResetsTheIdleStreak() async {
        let harness = GateHarness()
        harness.drainer.script = [idle, idle, .failure(.unreachable), idle, idle]
        harness.drainer.fallback = idle

        XCTAssertTrue(harness.take())
        await harness.settle()
        for _ in 0..<4 { await harness.fireNext() }

        XCTAssertEqual(harness.drainer.drains.count, 5)
        XCTAssertEqual(harness.installs, 0, "Two idle readings after the failure are not three.")
        XCTAssertEqual(harness.lastDelay, UpdateInstallGate.pollInterval, "The gate keeps polling.")

        await harness.fireNext()
        XCTAssertEqual(harness.lastDelay, 0, "The third idle reading after the failure schedules the install.")
        await harness.fireNext()
        XCTAssertEqual(harness.events, ["willInstall", "install"])
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

    /// A drain reply that lands after an abort may have reopened admission
    /// under the abort's cancel, so it cancels again. It never installs.
    func testAStaleDrainReplyAfterAnAbortCancelsAgain() async {
        let drainer = HeldEngineDrainer()
        let gate = UpdateInstallGate(
            localState: { .running(port: 8777) },
            drainer: drainer,
            owner: "sparkle-1.2.3",
            willInstall: {},
            schedule: { _, _ in },
            clock: { 0 }
        )

        XCTAssertTrue(gate.takeControl {})
        await settleMainActor()
        XCTAssertEqual(drainer.inFlight, 1, "The first drain is still in flight.")

        gate.installAborted()
        await settleMainActor()
        XCTAssertEqual(drainer.cancels.count, 1)

        drainer.answerOldest(.success(DrainReading(activeChats: 0, scheduleWork: 0)))
        await settleMainActor()
        XCTAssertEqual(drainer.cancels.map(\.owner), ["sparkle-1.2.3", "sparkle-1.2.3"], "The stale reply cancels again.")
        XCTAssertEqual(drainer.inFlight, 0, "The stale reply schedules no further poll.")
    }

    /// An abort during the 30-minute retry wait stops the timer: the retry
    /// closure finds the attempt ended and never reads the engine again.
    func testAnAbortDuringTheRetryWaitStopsTheTimer() async {
        let harness = GateHarness()
        harness.drainer.fallback = .failure(.busyRestart)

        XCTAssertTrue(harness.take())
        await harness.settle()
        XCTAssertEqual(harness.lastDelay, 1800)

        harness.gate.installAborted()
        await harness.settle()
        XCTAssertEqual(harness.drainer.cancels.count, 1, "The retry already cancelled the drain; the abort holds none.")

        await harness.fireNext()
        XCTAssertEqual(harness.drainer.drains.count, 1, "The retry wait ended with the abort.")
        XCTAssertEqual(harness.events, [])
    }

    /// A cancel the engine does not confirm is sent again five seconds later,
    /// keyed by the same owner, and stops once it is confirmed.
    func testACancelThatFailsOnceIsRetried() async {
        let harness = GateHarness()
        harness.drainer.fallback = busy
        harness.drainer.cancelResults = [false]

        XCTAssertTrue(harness.take())
        await harness.settle()
        harness.gate.installAborted()
        await harness.settle()
        XCTAssertEqual(harness.drainer.cancels.count, 1)
        XCTAssertEqual(harness.lastDelay, UpdateInstallGate.cancelRetryInterval, "The failed cancel is scheduled again in five seconds.")

        await harness.fireNext()
        await harness.fireNext()
        XCTAssertEqual(harness.drainer.cancels.count, 2)
        XCTAssertEqual(harness.drainer.cancels.map(\.owner), ["sparkle-1.2.3", "sparkle-1.2.3"])
        XCTAssertTrue(harness.scheduled.isEmpty, "A confirmed cancel is not sent again.")
    }

    /// A cancel that is never confirmed backs off: five seconds, then doubling
    /// to a two-minute step. The retries stop once ten minutes have passed since
    /// the first cancel, so the last attempt is at 635 s and nothing follows it.
    func testAnUnconfirmedCancelBacksOffUntilTheTenMinuteCap() async {
        let harness = GateHarness()
        harness.drainer.fallback = busy
        harness.drainer.cancelResults = Array(repeating: false, count: 100)

        XCTAssertTrue(harness.take())
        await harness.settle()
        harness.dropHeldWork()
        harness.gate.installAborted()
        await harness.settle()

        var delays: [TimeInterval] = []
        while let next = harness.scheduled.first, delays.count < 50 {
            delays.append(next.delay)
            await harness.fireNext()
        }

        XCTAssertEqual(delays, [5, 10, 20, 40, 80, 120, 120, 120, 120])
        XCTAssertEqual(harness.drainer.cancels.count, 10, "The first cancel plus one retry per delay.")
        XCTAssertEqual(harness.drainer.cancels.map(\.owner), Array(repeating: "sparkle-1.2.3", count: 10))
        XCTAssertTrue(harness.scheduled.isEmpty, "Past the cap nothing else is scheduled.")
        XCTAssertEqual(harness.now, 635)
    }

    /// A retry is dropped once the engine is no longer running on the drained
    /// port: a starting or stopping engine has no admission to reopen.
    func testAnUnconfirmedCancelStopsWhenTheEngineIsNoLongerRunning() async {
        let harness = GateHarness()
        harness.drainer.fallback = busy
        harness.drainer.cancelResults = [false, false, false]

        XCTAssertTrue(harness.take())
        await harness.settle()
        harness.dropHeldWork()
        harness.gate.installAborted()
        await harness.settle()
        XCTAssertEqual(harness.lastDelay, UpdateInstallGate.cancelRetryInterval)

        harness.state = .transitioning
        await harness.fireNext()

        XCTAssertEqual(harness.drainer.cancels.count, 2)
        XCTAssertTrue(harness.scheduled.isEmpty, "A transitioning engine is not retried.")
    }

    /// A retry queued before a newer drain on the same port is dropped: the
    /// newer drain closed admission again, and the stale cancel must not reopen it.
    func testAQueuedCancelRetryIsDroppedOnceANewerDrainStarts() async {
        let harness = GateHarness()
        harness.drainer.fallback = busy
        harness.drainer.cancelResults = [false, false]

        XCTAssertTrue(harness.take())
        await harness.settle()
        harness.dropHeldWork()
        harness.gate.installAborted()
        await harness.settle()
        XCTAssertEqual(harness.scheduled.count, 1, "The failed cancel has a retry queued.")

        harness.ledger.noteDrain(port: GateHarness.port)
        await harness.fireNext()

        XCTAssertEqual(harness.drainer.cancels.count, 1, "The stale retry sends nothing.")
        XCTAssertTrue(harness.scheduled.isEmpty)
    }

    /// A second handler replaces the first and keeps the one gate: only one
    /// install is scheduled, and it runs the second handler.
    func testASecondTakeControlReplacesTheHandlerAndKeepsTheGate() async {
        let harness = GateHarness()
        harness.state = .external
        XCTAssertTrue(harness.gate.takeControl { harness.record("first") })
        XCTAssertTrue(harness.gate.takeControl { harness.record("second") })
        XCTAssertEqual(harness.scheduled.count, 1)

        await harness.fireNext()
        XCTAssertEqual(harness.events, ["willInstall", "second"])
    }

    /// A starting or stopping engine is never idle and never drained. The gate
    /// waits, and installs only after three idle readings once it is running.
    func testATransitioningEngineIsNeverIdle() async {
        let harness = GateHarness()
        harness.state = .transitioning
        harness.drainer.fallback = idle

        XCTAssertTrue(harness.take())
        await harness.settle()
        for _ in 0..<5 { await harness.fireNext() }
        XCTAssertEqual(harness.drainer.drains.count, 0, "A starting or stopping engine is not read.")
        XCTAssertEqual(harness.installs, 0)

        harness.state = .running(port: GateHarness.port)
        for _ in 0..<3 { await harness.fireNext() }
        XCTAssertEqual(harness.drainer.drains.count, 3)
        XCTAssertEqual(harness.lastDelay, 0, "Three idle readings after it runs schedule the install.")
        XCTAssertEqual(harness.installs, 0)

        await harness.fireNext()
        XCTAssertEqual(harness.events, ["willInstall", "install"])
    }

    /// The idle streak is per port. An engine that moves to another port starts
    /// its count again, so two idle readings on each port never install.
    func testAPortChangeRestartsTheIdleStreak() async {
        let harness = GateHarness()
        harness.state = .running(port: 8001)
        harness.drainer.fallback = idle

        XCTAssertTrue(harness.take())
        await harness.settle()
        await harness.fireNext()
        XCTAssertEqual(harness.drainer.drains.count, 2)

        harness.state = .running(port: 8002)
        await harness.fireNext()
        await harness.fireNext()
        XCTAssertEqual(harness.drainer.drains.count, 4)
        XCTAssertEqual(harness.installs, 0, "Two idle readings on the new port are not three in a row.")

        await harness.fireNext()
        XCTAssertEqual(harness.lastDelay, 0, "Three idle readings on the new port install.")
    }

    /// The install turn reads the engine again. A change since the deciding
    /// reading starts the gate over instead of installing.
    func testTheInstallTurnRestartsTheGateWhenTheEngineChanged() async {
        let harness = GateHarness()
        harness.drainer.fallback = idle

        XCTAssertTrue(harness.take())
        await harness.settle()
        await harness.fireNext()
        await harness.fireNext()
        XCTAssertEqual(harness.lastDelay, 0, "Three idle readings schedule the install.")

        harness.state = .transitioning
        await harness.fireNext()
        XCTAssertEqual(harness.installs, 0, "The engine changed before the install turn, so nothing installs.")
        XCTAssertEqual(harness.lastDelay, UpdateInstallGate.pollInterval, "The gate polls again from a fresh streak.")

        harness.state = .running(port: GateHarness.port)
        for _ in 0..<3 { await harness.fireNext() }
        XCTAssertEqual(harness.lastDelay, 0)
        await harness.fireNext()
        XCTAssertEqual(harness.events, ["willInstall", "install"])
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
        // The host is loopback by design: the drain routes exist only there.
        XCTAssertEqual(request.url?.absoluteString, "http://127.0.0.1:8777/api/admin/drain")
        let owner = try StubDrainProtocol.owner(of: request)
        XCTAssertEqual(owner, "sparkle-1.2.3")
        XCTAssertEqual(request.timeoutInterval, 5, "Each request carries the five-second bound itself.")
        XCTAssertNil(request.value(forHTTPHeaderField: "Cookie"))
        XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
    }

    func testCancelPostsTheSameOwnerToTheCancelRoute() async throws {
        StubDrainProtocol.answer = (200, Data(#"{"draining":false}"#.utf8))

        let confirmed = await makeClient().cancel(port: 8777, owner: "sparkle-1.2.3")

        XCTAssertTrue(confirmed)
        let request = try XCTUnwrap(StubDrainProtocol.requests.first)
        XCTAssertEqual(request.url?.absoluteString, "http://127.0.0.1:8777/api/admin/drain/cancel")
        let owner = try StubDrainProtocol.owner(of: request)
        XCTAssertEqual(owner, "sparkle-1.2.3")
    }

    /// A cancel is confirmed only by a 2xx answer that says the drain is closed.
    func testACancelIsConfirmedOnlyByAnOpenAdmissionAnswer() async {
        StubDrainProtocol.answer = (200, Data(#"{"draining":true}"#.utf8))
        let stillDraining = await makeClient().cancel(port: 8777, owner: "sparkle-1.2.3")
        XCTAssertFalse(stillDraining, "A cancel that leaves the drain on is not confirmed.")

        StubDrainProtocol.answer = (409, Data(#"{"error":"drain owned by another operation"}"#.utf8))
        let refused = await makeClient().cancel(port: 8777, owner: "sparkle-1.2.3")
        XCTAssertFalse(refused)

        StubDrainProtocol.fails = true
        let unreachable = await makeClient().cancel(port: 8777, owner: "sparkle-1.2.3")
        XCTAssertFalse(unreachable)
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

    /// A redirect is never followed: the answer must come from the engine that
    /// was asked, so the 3xx is no reading.
    func testARedirectIsRefusedNotFollowed() async {
        StubDrainProtocol.answer = (302, Data())
        StubDrainProtocol.location = "http://127.0.0.1:9999/elsewhere"

        let result = await makeClient().drain(port: 8777, owner: "sparkle-1.2.3")

        XCTAssertEqual(result, .failure(.unreachable))
        XCTAssertEqual(StubDrainProtocol.requests.count, 1, "The redirect target is never requested.")
    }

    /// An answer over the shared 64 KiB cap is no reading, even when it is valid
    /// JSON with the right fields.
    func testAnAnswerOverTheResponseCapIsUnreachable() async {
        let padding = String(repeating: "x", count: RestartBlockersProbe.maximumResponseBytes)
        StubDrainProtocol.answer = (200, Data(#"{"draining":true,"active_chat_ids":[],"pad":"\#(padding)"}"#.utf8))

        let result = await makeClient().drain(port: 8777, owner: "sparkle-1.2.3")

        XCTAssertEqual(result, .failure(.unreachable))
    }

    func testAScheduleCountMustBeAWholeNumberToCountAsReported() {
        XCTAssertEqual(
            EngineDrainClient.reading(from: drainAnswer(#""schedule_work_count":2"#)),
            DrainReading(activeChats: 0, scheduleWork: 2)
        )
        XCTAssertEqual(
            EngineDrainClient.reading(from: drainAnswer("")),
            DrainReading(activeChats: 0, scheduleWork: nil)
        )
        XCTAssertEqual(
            EngineDrainClient.reading(from: drainAnswer(#""schedule_work_count":true"#)),
            DrainReading(activeChats: 0, scheduleWork: nil),
            "A boolean is not a count."
        )
        // A count Int cannot hold, or one that is not whole or not positive,
        // is no count. Converting 1e30 with a trapping cast would crash the app.
        for count in ["1e30", "1.5", "-1"] {
            XCTAssertEqual(
                EngineDrainClient.reading(from: drainAnswer(#""schedule_work_count":\#(count)"#)),
                DrainReading(activeChats: 0, scheduleWork: nil),
                "schedule_work_count \(count) is not a count."
            )
        }
        XCTAssertNil(EngineDrainClient.reading(from: Data(#"{"draining":true}"#.utf8)))
    }

    /// The answer must say the drain is on: a drain answer without `draining`
    /// true is no reading, so it can never count as idle.
    func testADrainAnswerMustSayItIsDraining() {
        XCTAssertNil(EngineDrainClient.reading(from: Data(#"{"active_chat_ids":[]}"#.utf8)))
        XCTAssertNil(EngineDrainClient.reading(from: Data(#"{"draining":false,"active_chat_ids":[]}"#.utf8)))
        XCTAssertNil(EngineDrainClient.reading(from: Data(#"{"draining":"true","active_chat_ids":[]}"#.utf8)))
        XCTAssertNil(EngineDrainClient.reading(from: Data(#"{"draining":1,"active_chat_ids":[]}"#.utf8)))
        XCTAssertNotNil(EngineDrainClient.reading(from: drainAnswer("")))
    }

    /// The production session is session-free with five-second bounds. Each
    /// value is pinned so a longer timeout or a restored cookie jar fails here.
    func testTheProductionConfigurationIsSessionFreeWithFiveSecondBounds() {
        let configuration = EngineDrainClient.makeConfiguration()

        XCTAssertEqual(configuration.timeoutIntervalForRequest, 5)
        XCTAssertEqual(configuration.timeoutIntervalForResource, 5)
        XCTAssertFalse(configuration.httpShouldSetCookies)
        XCTAssertEqual(configuration.httpCookieAcceptPolicy, .never)
        XCTAssertNil(configuration.httpCookieStorage)
        XCTAssertNil(configuration.urlCredentialStorage)
        XCTAssertNil(configuration.urlCache)
    }

    /// The client the app builds by default uses that configuration.
    func testTheDefaultClientUsesTheProductionConfiguration() {
        let session = EngineDrainClient().session.configuration

        XCTAssertEqual(session.timeoutIntervalForRequest, 5)
        XCTAssertFalse(session.httpShouldSetCookies)
        XCTAssertNil(session.httpCookieStorage)
        XCTAssertNil(session.urlCredentialStorage)
    }

    /// A never-answering engine is unreachable within the five-second request
    /// bound. The loopback listen backlog completes the TCP handshake, so the
    /// request is sent and then waits for an answer that never comes.
    func testANeverAnsweringEngineIsUnreachableWithinTheRequestBound() async throws {
        let silent = try silentEnginePort()
        defer { silent.close() }

        let started = Date()
        let result = await EngineDrainClient().drain(port: silent.port, owner: "sparkle-1.2.3")

        XCTAssertEqual(result, .failure(.unreachable))
        XCTAssertLessThan(
            Date().timeIntervalSince(started), 8,
            "Five seconds by the request bound, with slack for a loaded machine."
        )
    }

    /// A drain answer for the reading tests: `draining` true and an empty
    /// chat list, plus any extra member.
    private func drainAnswer(_ extra: String) -> Data {
        let tail = extra.isEmpty ? "" : ",\(extra)"
        return Data(#"{"draining":true,"active_chat_ids":[]\#(tail)}"#.utf8)
    }

    /// A loopback listening socket on a free port that never accepts or
    /// answers. Closing it is the caller's job.
    private func silentEnginePort() throws -> (port: Int, close: () -> Void) {
        let descriptor = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else { throw XCTSkip("No socket for a silent listener.") }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        address.sin_port = 0
        let bound = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0, Darwin.listen(descriptor, 1) == 0 else {
            Darwin.close(descriptor)
            throw XCTSkip("No loopback port for a silent listener.")
        }
        var bound4 = sockaddr_in()
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &bound4) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.getsockname(descriptor, $0, &length)
            }
        }
        return (Int(UInt16(bigEndian: bound4.sin_port)), { Darwin.close(descriptor) })
    }
}

/// Answers every request with one canned status and body, and records the request.
private final class StubDrainProtocol: URLProtocol {
    static var answer: (Int, Data) = (200, Data())
    static var fails = false
    static var location: String?
    static var requests: [URLRequest] = []

    static func reset() {
        answer = (200, Data())
        fails = false
        location = nil
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
        let url = request.url ?? URL(fileURLWithPath: "/")
        var headers = ["Content-Type": "application/json"]
        if let location = Self.location { headers["Location"] = location }
        let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers)!
        if (300...399).contains(status), let location = Self.location, let target = URL(string: location) {
            // A redirect is reported the way a protocol reports one, so the
            // session's own redirect handling runs and asks the task's delegate.
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, wasRedirectedTo: URLRequest(url: target), redirectResponse: response)
            client?.urlProtocolDidFinishLoading(self)
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
