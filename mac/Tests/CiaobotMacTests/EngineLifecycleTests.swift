import XCTest
import AppKit

@testable import CiaobotMac

// MARK: - Fakes (own file copies, like every suite here)

/// Records what the supervisor asked instead of running any process.
private final class LifecycleProcesses: EngineProcessRunning {
    var onExit: ((Int32, Int32) -> Void)?
    var spawns: [(launch: BundledEngineLaunch, log: URL)] = []
    var terminated: [Int32] = []
    var killed: [Int32] = []
    var alive: [Int32: EngineProcessInfo] = [:]
    var nextPID: Int32 = 5000

    func spawn(_ launch: BundledEngineLaunch, log: URL) throws -> Int32 {
        spawns.append((launch, log))
        nextPID += 1
        return nextPID
    }
    func terminate(_ pid: Int32) { terminated.append(pid); alive[pid] = nil }
    func kill(_ pid: Int32) { killed.append(pid); alive[pid] = nil }
    func inspect(_ pid: Int32) -> EngineProcessInfo? { alive[pid] }
}

private final class LifecycleIdentity: EngineIdentityChecking {
    var answer: EngineIdentity?
    func identity(of origin: Origin, completion: @escaping (EngineIdentity?) -> Void) {
        completion(answer)
    }
}

/// A clock and scheduler in one: `advance` moves time and runs what falls due.
private final class LifecycleTime {
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

/// Records the confirmations and decides. No test shows a modal alert.
@MainActor
private final class LifecycleDialogSpy {
    private(set) var titles: [String] = []
    private(set) var bodies: [String] = []
    var result = false

    func present(_ dialog: EngineQuitDialog) -> Bool {
        titles.append(dialog.title)
        bodies.append(dialog.body)
        return result
    }
}

/// Holds the blockers completions so a test can release them: a deferred
/// terminate must wait for the fetch exactly like the real one does.
private final class LifecycleBlockers: RestartBlockersFetching {
    private(set) var origins: [Origin] = []
    var answer: Int?
    private var completions: [(Int?) -> Void] = []

    func activeChats(of origin: Origin, completion: @escaping (Int?) -> Void) {
        origins.append(origin)
        completions.append(completion)
    }

    func answerAll() {
        let answer = answer
        completions.forEach { $0(answer) }
        completions = []
    }
}

private final class LifecycleMenuBar: MenuBarControlling {
    private(set) var quittingCount = 0
    func start() {}
    func stop() {}
    func setQuittingTitle() { quittingCount += 1 }
}

/// A window double that records close without performing it: the login-launch
/// close must reach this window, and no test orders a real one in.
@MainActor
private final class LifecycleSpyWindow: NSWindow {
    private(set) var closeCount = 0

    override func close() {
        closeCount += 1
    }
}

/// A menu-bar item double, like the one in `MenuBarTests`.
@MainActor
private final class LifecycleStatusItem: MenuBarStatusItem {
    var title: String?
    var image: NSImage?
    var toolTip: String?
    var menu: NSMenu?
    var isVisible = false
    private(set) var removeCount = 0

    func remove() { removeCount += 1 }
}

@MainActor
private final class LifecycleStatusItemFactory: StatusItemFactory {
    private(set) var items: [LifecycleStatusItem] = []

    func makeStatusItem(length: CGFloat) -> MenuBarStatusItem {
        let item = LifecycleStatusItem()
        items.append(item)
        return item
    }
}

private final class LifecycleEraser: WebsiteDataStoreErasing {
    func remove(identifier: UUID, completion: @escaping (Error?) -> Void) {
        completion(nil)
    }
}

/// An answer without contacting anything.
private final class LifecycleProbe: ProbeChecking {
    func check(_ origin: Origin, completion: @escaping (Bool) -> Void) {
        completion(true)
    }
}

// MARK: - Apple-event construction

/// Builds the events `EngineLifecycle` decides on. Constructed descriptors are
/// the unit-test stand-in for real launch and quit events; no test launches an
/// app or a login item.
private func appleEvent(_ classID: FourCharCode, _ eventID: FourCharCode) -> NSAppleEventDescriptor {
    NSAppleEventDescriptor(
        eventClass: classID,
        eventID: eventID,
        targetDescriptor: NSAppleEventDescriptor.null(),
        returnID: AEReturnID(kAutoGenerateReturnID),
        transactionID: AETransactionID(kAnyTransactionID)
    )
}

private func quitEvent(reason: OSType?) -> NSAppleEventDescriptor {
    let event = appleEvent(kCoreEventClass, kAEQuitApplication)
    if let reason {
        event.setAttribute(NSAppleEventDescriptor(enumCode: reason), forKeyword: kAEQuitReason)
    }
    return event
}

private func launchEvent(propDataKeyword: AEKeyword?) -> NSAppleEventDescriptor {
    let event = appleEvent(kCoreEventClass, kAEOpenApplication)
    if let propDataKeyword {
        event.setParam(NSAppleEventDescriptor(enumCode: propDataKeyword), forKeyword: keyAEPropData)
    }
    return event
}

/// The presenter double, like the one in `LocalEngineWiringTests`.
@MainActor
private final class LifecycleMainWindowPresenter: MainWindowPresenting {
    private(set) var presentCount = 0
    @discardableResult func present() -> Bool { presentCount += 1; return true }
}

// MARK: - Tests

@MainActor
final class EngineLifecycleTests: XCTestCase {
    private var tmp: URL!
    private var home: EngineHome!
    private var engineRoot: URL!
    private let processes = LifecycleProcesses()
    private let identity = LifecycleIdentity()
    private let time = LifecycleTime()
    private var suiteName = ""
    private var defaults: UserDefaults!
    private var lastServer: UserDefaultsLastServerStore!
    private var hostStore: UserDefaultsHostListStore!
    private let dialogs = LifecycleDialogSpy()
    private let blockers = LifecycleBlockers()
    private var replies: [Bool] = []
    private let menuBar = LifecycleMenuBar()

    override func setUpWithError() throws {
        tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("EngineLifecycleTests-\(UUID().uuidString)", isDirectory: true)
        let physical = tmp.resolvingSymlinksInPath()
        try FileManager.default.createDirectory(at: physical, withIntermediateDirectories: true)
        tmp = physical
        home = EngineHome(home: tmp.appendingPathComponent("home", isDirectory: true))
        try FileManager.default.createDirectory(at: home.stateDir, withIntermediateDirectories: true)
        engineRoot = tmp.appendingPathComponent("Ciaobot.app/Contents/Resources/engine", isDirectory: true)
        suiteName = IsolatedDefaults.makeName("EngineLifecycle")
        defaults = UserDefaults(suiteName: suiteName)
        defaults.removePersistentDomain(forName: suiteName)
        lastServer = UserDefaultsLastServerStore(defaults: defaults)
        hostStore = UserDefaultsHostListStore(defaults: defaults, lastServer: lastServer)
        replies = []
    }

    override func tearDownWithError() throws {
        IsolatedDefaults.remove(suiteName)
        defaults = nil
        lastServer = nil
        hostStore = nil
        try? FileManager.default.removeItem(at: tmp)
    }

    // MARK: Pure facts

    func testQuitReasonForEverySessionEndingCode() {
        for reason in [kAELogOut, kAEReallyLogOut, kAEShowRestartDialog, kAERestart, kAEShowShutdownDialog, kAEShutDown] {
            XCTAssertEqual(EngineLifecycle.quitReason(quitEvent(reason: reason)), .system, "\(reason)")
        }
    }

    func testAQuitWithoutAReasonOrEventIsAUserQuit() {
        XCTAssertEqual(EngineLifecycle.quitReason(quitEvent(reason: nil)), .user,
                       "the tray and the app menu send no Apple event")
        XCTAssertEqual(EngineLifecycle.quitReason(nil), .user)
        // An unrelated value is not one of the session-ending reasons.
        XCTAssertEqual(EngineLifecycle.quitReason(quitEvent(reason: kAEQuitAll)), .user)
        // A reason only counts on a quit event.
        XCTAssertEqual(EngineLifecycle.quitReason(launchEvent(propDataKeyword: nil)), .user)
    }

    func testLoginLaunchDetection() {
        XCTAssertTrue(EngineLifecycle.isLoginLaunch(launchEvent(propDataKeyword: keyAELaunchedAsLogInItem)))
        // A plain launch, an unrelated key, another event, and no event at all.
        XCTAssertFalse(EngineLifecycle.isLoginLaunch(launchEvent(propDataKeyword: nil)))
        XCTAssertFalse(EngineLifecycle.isLoginLaunch(launchEvent(propDataKeyword: keyAELaunchedAsServiceItem)))
        XCTAssertFalse(EngineLifecycle.isLoginLaunch(quitEvent(reason: nil)))
        XCTAssertFalse(EngineLifecycle.isLoginLaunch(nil))
    }

    func testQuitMessageCopyForNilZeroAndThreeChats() {
        let base = "This also stops Ciaobot on this Mac: chats, schedules and access from your phone pause until you open Ciaobot again."
        let none = EngineLifecycle.quitMessage(activeChats: nil)
        XCTAssertEqual(none.title, "Quit Ciaobot?")
        XCTAssertEqual(none.body, base)
        XCTAssertEqual(EngineLifecycle.quitMessage(activeChats: 0).body, base,
                       "zero working chats state no count")
        let three = EngineLifecycle.quitMessage(activeChats: 3)
        XCTAssertTrue(three.body.hasSuffix(" 3 chats are still working and will be interrupted."),
                      "the body names the count, \(three.body)")
        XCTAssertTrue(
            EngineLifecycle.quitMessage(activeChats: 1).body.hasSuffix(" 1 chat is still working and will be interrupted.")
        )
    }

    func testTerminationPlanTable() {
        let running = EngineState.running(origin: Origin("http://127.0.0.1:8777")!, pid: 5, startupID: "x")
        // External mode never stops anything.
        XCTAssertEqual(EngineLifecycle.terminationPlan(mode: .external, engineState: running, quitReason: .user), .terminateNow)
        XCTAssertEqual(EngineLifecycle.terminationPlan(mode: .external, engineState: nil, quitReason: .system), .terminateNow)
        // No supervisor, or an engine that is not running: leave at once.
        for state in [nil, EngineState.stopped, EngineState.failed("boom"), EngineState.portInUse(8443)] as [EngineState?] {
            XCTAssertEqual(EngineLifecycle.terminationPlan(mode: .local, engineState: state, quitReason: .user), .terminateNow)
        }
        // A stop already in progress is joined, never restarted or skipped.
        XCTAssertEqual(EngineLifecycle.terminationPlan(mode: .local, engineState: .stopping, quitReason: .user),
                       .stopThenTerminate(grace: 35))
        // A system quit of a starting or running engine stops it and never asks.
        XCTAssertEqual(EngineLifecycle.terminationPlan(mode: .local, engineState: .starting, quitReason: .system),
                       .stopThenTerminate(grace: 20))
        XCTAssertEqual(EngineLifecycle.terminationPlan(mode: .local, engineState: running, quitReason: .system),
                       .stopThenTerminate(grace: 20))
        // A user quit of a starting or running engine asks first.
        XCTAssertEqual(EngineLifecycle.terminationPlan(mode: .local, engineState: .starting, quitReason: .user), .confirm)
        XCTAssertEqual(EngineLifecycle.terminationPlan(mode: .local, engineState: running, quitReason: .user), .confirm)
    }

    func testBlockersParseCountsAndRefusesEverythingElse() {
        XCTAssertEqual(RestartBlockersProbe.activeChats(
            status: 200,
            data: Data(#"{"restart_blockers":[{"chat_id":"a"},{"chat_id":"b"},{"chat_id":"c"}]}"#.utf8)
        ), 3)
        XCTAssertEqual(RestartBlockersProbe.activeChats(status: 200, data: Data(#"{"restart_blockers":[]}"#.utf8)), 0)
        // Anything else is no count, not zero.
        XCTAssertNil(RestartBlockersProbe.activeChats(status: 404, data: Data(#"{"restart_blockers":[]}"#.utf8)))
        XCTAssertNil(RestartBlockersProbe.activeChats(status: 200, data: Data("not json".utf8)))
        XCTAssertNil(RestartBlockersProbe.activeChats(status: 200, data: Data(#"{"restart_blockers":42}"#.utf8)))
        XCTAssertNil(RestartBlockersProbe.activeChats(status: 200, data: Data(#"{"other":[]}"#.utf8)))
        let oversized = Data(count: RestartBlockersProbe.maximumResponseBytes + 1)
        XCTAssertNil(RestartBlockersProbe.activeChats(status: 200, data: oversized), "an oversized body is no count")
    }

    // MARK: Helpers for the delegate

    private func makeSupervisor() -> EngineSupervisor {
        EngineSupervisor(
            engine: BundledEngine(root: engineRoot),
            home: home,
            processes: processes,
            identity: identity,
            environment: ["PATH": "/usr/bin"],
            clock: { [time] in time.now },
            schedule: { [time] delay, work in time.schedule(delay, work) }
        )
    }

    private func writeReady(pid: Int32, port: Int = 8777, startupID: String = "abc123") throws {
        let json = #"{"schema":1,"pid":\#(pid),"port":\#(port),"startup_id":"\#(startupID)","workspace":"/w","bootstrap":false}"#
        try json.write(to: home.readyRecord, atomically: true, encoding: .utf8)
    }

    /// Brings the fake engine to `running` and returns its pid.
    private func bringUp(_ supervisor: EngineSupervisor, port: Int = 8777) throws -> Int32 {
        supervisor.start()
        let pid = processes.nextPID
        try writeReady(pid: pid, port: port)
        identity.answer = EngineIdentity(pid: pid, startupID: "abc123")
        time.advance(0.5)
        return pid
    }

    /// Brings the fake engine into `portInUse`: it started, found the port
    /// held, and exited 69.
    private func collidePort(_ supervisor: EngineSupervisor) -> Int32 {
        supervisor.start()
        let pid = processes.nextPID
        processes.onExit?(pid, 69)
        return pid
    }

    private func makeDelegate(
        mode: EngineMode,
        supervisor: EngineSupervisor? = nil,
        launchEvent: (() -> NSAppleEventDescriptor?)? = nil,
        mainWindow: (@MainActor () -> NSWindow?)? = nil,
        quitDialog: (@MainActor (EngineQuitDialog) -> Bool)? = nil,
        launched: Bool = false
    ) -> AppDelegate {
        let delegate = AppDelegate(
            session: AppSessionStatus(),
            presenter: LifecycleMainWindowPresenter(),
            makeMenuBar: { _, _, _, _, _ in self.menuBar },
            engineMode: TransientEngineModeStore(),
            mode: mode,
            supervisor: supervisor,
            lastServer: lastServer,
            hostStore: hostStore,
            launchEvent: launchEvent ?? { nil },
            mainWindow: mainWindow,
            quitDialog: quitDialog ?? { [dialogs] dialog in dialogs.present(dialog) },
            restartBlockers: blockers,
            replyToTerminate: { [self] terminate in replies.append(terminate) }
        )
        // The tray surfaces are handed over at launch; a started app runs no
        // second engine (a running or starting supervisor ignores `start()`).
        if launched {
            delegate.applicationDidFinishLaunching(Notification(name: NSApplication.didFinishLaunchingNotification))
        }
        return delegate
    }

    /// The terminate reply for a delegate call, as AppKit would consume it.
    private func shouldTerminate(_ delegate: AppDelegate) -> NSApplication.TerminateReply {
        delegate.applicationShouldTerminate(NSApplication.shared)
    }

    /// **External mode** never asks and never stops anything: there is no
    /// engine the app owns, whatever the supervisor reports.
    func testExternalModeTerminatesNowWithoutAskingOrStopping() throws {
        let supervisor = makeSupervisor()
        _ = try bringUp(supervisor)
        dialogs.result = true
        let delegate = makeDelegate(mode: .external, supervisor: supervisor)

        XCTAssertEqual(shouldTerminate(delegate), .terminateNow)
        XCTAssertEqual(dialogs.titles, [])
        XCTAssertEqual(blockers.origins, [])
        XCTAssertEqual(processes.terminated, [])
        XCTAssertEqual(replies, [])
    }

    /// A user quit of a running local engine: the count is fetched, the one
    /// confirmation decides, and a confirmed quit stops the engine and leaves
    /// only after it has exited.
    func testAUserQuitOfARunningEngineAsksAndStopsAfterTheConfirm() throws {
        let supervisor = makeSupervisor()
        let pid = try bringUp(supervisor)
        dialogs.result = true
        blockers.answer = 3
        let delegate = makeDelegate(mode: .local, supervisor: supervisor, launched: true)

        XCTAssertEqual(shouldTerminate(delegate), .terminateLater, "the confirmation defers termination")
        XCTAssertEqual(blockers.origins.count, 1, "the fetch starts with the terminate request")
        blockers.answerAll()
        XCTAssertEqual(dialogs.titles, ["Quit Ciaobot?"])
        XCTAssertTrue(dialogs.bodies.first?.hasSuffix(" 3 chats are still working and will be interrupted.") ?? false,
                      "the dialog names the engine's working chats")
        XCTAssertEqual(blockers.origins.map { $0.canonicalKey }, ["http://127.0.0.1:8777"],
                       "the count comes from the engine's own origin")
        XCTAssertEqual(processes.terminated, [pid], "a confirmed quit SIGTERMs the engine")
        XCTAssertEqual(menuBar.quittingCount, 1, "the tray shows the stopping line")
        XCTAssertEqual(replies, [], "no reply before the engine is gone")
        processes.onExit?(pid, 0)
        XCTAssertEqual(replies, [true], "the app leaves only after the drain")
    }

    /// **Cancel** keeps the engine and the choice: no stop, a false reply, and
    /// a second quit that starts a fresh flow.
    func testACancelledQuitStopsNothingAndLeavesTheFlowFresh() throws {
        let supervisor = makeSupervisor()
        let pid = try bringUp(supervisor)
        dialogs.result = false
        blockers.answer = 2
        let delegate = makeDelegate(mode: .local, supervisor: supervisor)

        XCTAssertEqual(shouldTerminate(delegate), .terminateLater)
        blockers.answerAll()
        XCTAssertEqual(dialogs.titles.count, 1)
        XCTAssertEqual(processes.terminated, [], "cancel never stops the engine")
        XCTAssertEqual(replies, [false])
        XCTAssertEqual(supervisor.state,
                       EngineState.running(origin: Origin("http://127.0.0.1:8777")!, pid: pid, startupID: "abc123"),
                       "the engine keeps draining nothing; it runs on")

        // The cancellation reset the flow, so a second quit is a fresh one.
        XCTAssertEqual(shouldTerminate(delegate), .terminateLater)
        blockers.answerAll()
        XCTAssertEqual(dialogs.titles.count, 2)
        XCTAssertEqual(blockers.origins.count, 2)
        XCTAssertEqual(replies, [false, false])
        XCTAssertEqual(processes.terminated, [])
    }

    /// The plan's mutation target: a system quit skips the alert entirely and
    /// still stops the engine, replying true when it is gone.
    func testASystemQuitSkipsTheDialogAndStopsTheEngine() throws {
        let supervisor = makeSupervisor()
        let pid = try bringUp(supervisor)
        let reason = quitEvent(reason: kAELogOut)
        let delegate = makeDelegate(mode: .local, supervisor: supervisor, launchEvent: { reason }, launched: true)

        XCTAssertEqual(shouldTerminate(delegate), .terminateLater)
        XCTAssertEqual(dialogs.titles, [], "logout, restart and shutdown never ask")
        XCTAssertEqual(blockers.origins, [])
        XCTAssertEqual(processes.terminated, [pid])
        XCTAssertEqual(menuBar.quittingCount, 1)
        XCTAssertEqual(replies, [])
        processes.onExit?(pid, 0)
        XCTAssertEqual(replies, [true])
    }

    /// A quit while an earlier stop (a mode switch, or a previous confirmed
    /// quit) is still draining joins it: the app leaves when the drain
    /// finishes, never before, and asks nothing.
    func testAQuitDuringAStopInFlightJoinsIt() throws {
        let supervisor = makeSupervisor()
        let pid = try bringUp(supervisor)
        supervisor.stop(grace: 35) {}
        guard case .stopping = supervisor.state else {
            return XCTFail("expected stopping")
        }
        let delegate = makeDelegate(mode: .local, supervisor: supervisor, launched: true)
        XCTAssertEqual(shouldTerminate(delegate), .terminateLater)
        XCTAssertEqual(dialogs.titles, [], "there is nothing left to ask about")
        XCTAssertEqual(blockers.origins, [])
        XCTAssertEqual(processes.terminated, [pid, pid], "the joined stop renews the SIGTERM")
        XCTAssertEqual(menuBar.quittingCount, 1)
        processes.onExit?(pid, 0)
        XCTAssertEqual(replies, [true])
    }

    /// A confirmed quit of an engine that refuses to drain: SIGTERM first, the
    /// SIGKILL only after the 35 s grace, then the reply.
    func testAResistingEngineIsKilledOnlyAfterTheGrace() throws {
        let supervisor = makeSupervisor()
        let pid = try bringUp(supervisor)
        dialogs.result = true
        blockers.answer = nil
        let delegate = makeDelegate(mode: .local, supervisor: supervisor)

        XCTAssertEqual(shouldTerminate(delegate), .terminateLater)
        blockers.answerAll()
        XCTAssertEqual(dialogs.titles.count, 1, "a failed count still asks")
        XCTAssertEqual(processes.terminated, [pid])
        time.advance(20)
        XCTAssertEqual(processes.killed, [], "SIGTERM drains first: nothing is killed at 20 s")
        time.advance(15)
        XCTAssertEqual(processes.killed, [pid], "the grace ends with SIGKILL")
        XCTAssertEqual(replies, [])
        time.advance(5)
        XCTAssertEqual(replies, [true], "the stop always ends, so the app leaves")
    }

    /// A quit of an engine that is not running needs no ask and no stop.
    func testAQuitOfAStoppedEngineLeavesAtOnce() {
        let delegate = makeDelegate(mode: .local, supervisor: makeSupervisor())
        XCTAssertEqual(shouldTerminate(delegate), .terminateNow)
        XCTAssertEqual(dialogs.titles, [])
        XCTAssertEqual(blockers.origins, [])
        XCTAssertEqual(replies, [])
    }

    /// `portInUse` is the same: the engine this app owns is gone, and the
    /// process holding the port is not ours to stop.
    func testAQuitOfAPortInUseEngineLeavesAtOnce() {
        let supervisor = makeSupervisor()
        _ = collidePort(supervisor)
        guard case .portInUse = supervisor.state else {
            return XCTFail("expected portInUse")
        }
        dialogs.result = true
        let delegate = makeDelegate(mode: .local, supervisor: supervisor)

        XCTAssertEqual(shouldTerminate(delegate), .terminateNow)
        XCTAssertEqual(dialogs.titles, [])
        XCTAssertEqual(processes.terminated, [])
    }

    /// A second terminate request while the first one is deferred waits: no
    /// second fetch, and the single dialog decides both.
    func testASecondQuitWhileDeferredMakesNoSecondDialog() throws {
        let supervisor = makeSupervisor()
        let pid = try bringUp(supervisor)
        dialogs.result = true
        blockers.answer = 1
        let delegate = makeDelegate(mode: .local, supervisor: supervisor)

        XCTAssertEqual(shouldTerminate(delegate), .terminateLater)
        XCTAssertEqual(shouldTerminate(delegate), .terminateLater)
        XCTAssertEqual(blockers.origins.count, 1, "one fetch at a time")
        blockers.answerAll()
        XCTAssertEqual(dialogs.titles.count, 1)
        XCTAssertEqual(processes.terminated, [pid])
        processes.onExit?(pid, 0)
        XCTAssertEqual(replies, [true])
    }

    // MARK: Login launch

    /// A login launch closes the main window once SwiftUI has created it; an
    /// ordinary launch leaves it alone.
    func testALoginLaunchClosesTheMainWindowAndAnOrdinaryLaunchDoesNot() {
        let window = makeSpyWindow()
        let login = launchEvent(propDataKeyword: keyAELaunchedAsLogInItem)
        let delegate = makeDelegate(mode: .external,
                                    launchEvent: { login },
                                    mainWindow: { [weak window] in window })

        delegate.applicationWillFinishLaunching(Notification(name: NSApplication.willFinishLaunchingNotification))
        delegate.applicationDidFinishLaunching(Notification(name: NSApplication.didFinishLaunchingNotification))
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        XCTAssertEqual(window.closeCount, 1, "a login launch opens no main window")

        let ordinary = makeDelegate(mode: .external,
                                    launchEvent: { nil },
                                    mainWindow: { [weak window] in window })
        ordinary.applicationWillFinishLaunching(Notification(name: NSApplication.willFinishLaunchingNotification))
        ordinary.applicationDidFinishLaunching(Notification(name: NSApplication.didFinishLaunchingNotification))
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        XCTAssertEqual(window.closeCount, 1, "an ordinary launch leaves the window alone")
    }

    /// The fact is read in `applicationWillFinishLaunching`, where the launch
    /// Apple event is still current: a delegate that skipped that callback must
    /// not close anything.
    func testTheLoginLaunchFactIsReadAtWillFinishLaunching() {
        let window = makeSpyWindow()
        let login = launchEvent(propDataKeyword: keyAELaunchedAsLogInItem)
        let delegate = makeDelegate(mode: .external,
                                    launchEvent: { login },
                                    mainWindow: { [weak window] in window })

        delegate.applicationDidFinishLaunching(Notification(name: NSApplication.didFinishLaunchingNotification))
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        XCTAssertEqual(window.closeCount, 0)
    }

    /// The engine starts with no window: a login launch only skips the window,
    /// never the local mode's engine (S5b behaviour kept).
    func testALoginLaunchInLocalModeStillStartsTheEngine() throws {
        let login = launchEvent(propDataKeyword: keyAELaunchedAsLogInItem)
        let window = makeSpyWindow()
        let supervisor = makeSupervisor()
        let delegate = makeDelegate(mode: .local, supervisor: supervisor,
                                    launchEvent: { login }, mainWindow: { [weak window] in window })

        delegate.applicationWillFinishLaunching(Notification(name: NSApplication.willFinishLaunchingNotification))
        delegate.applicationDidFinishLaunching(Notification(name: NSApplication.didFinishLaunchingNotification))
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))

        XCTAssertEqual(processes.spawns.count, 1, "the engine starts regardless")
        XCTAssertEqual(processes.spawns.first?.launch.arguments, ["run", "--app-engine"])
        XCTAssertEqual(window.closeCount, 1)
    }

    private func makeSpyWindow() -> LifecycleSpyWindow {
        let window = LifecycleSpyWindow(
            contentRect: NSRect(x: 0, y: 0, width: 10, height: 10),
            styleMask: .borderless, backing: .buffered, defer: false
        )
        window.identifier = MainWindowPresenter.mainWindowIdentifier
        return window
    }

    // MARK: Mode switch

    private func makeLocalView(
        supervisor: EngineSupervisor,
        modeStore: EngineModeStoring,
        presenter: @escaping (EngineLifecycle.ModeSwitchDialog) -> Bool
    ) -> ConnectionView {
        ConnectionView(
            probe: LifecycleProbe(),
            signIn: SessionSignIn(
                registry: SessionStoreRegistry(fileURL: FileManager.default.temporaryDirectory
                    .appendingPathComponent("EngineLifecycle-Registry-\(UUID().uuidString).json")),
                eraser: LifecycleEraser(),
                persistentAvailable: false
            ),
            session: AppSessionStatus(),
            lastServer: lastServer,
            hostStore: hostStore,
            supervisor: supervisor,
            engineMode: .local,
            modeStore: modeStore,
            modeSwitchDialog: presenter
        )
    }

    /// Change Server in local mode stops the supervisor **only after** the
    /// confirm: a cancel leaves the engine running and the local choice saved,
    /// a confirm stops it and shows the connection screen.
    func testChangeServerInLocalModeStopsTheSupervisorOnlyAfterTheConfirm() throws {
        let supervisor = makeSupervisor()
        let pid = try bringUp(supervisor)
        let store = TransientEngineModeStore()
        store.mode = .local
        let decisions: [Bool] = [false, true]
        var call = 0
        let view = makeLocalView(supervisor: supervisor, modeStore: store) { dialog in
            XCTAssertEqual(dialog, EngineLifecycle.modeSwitchDialog)
            let decided = decisions[call]
            call += 1
            return decided
        }

        view.confirmAndConnectToAnotherServer()
        XCTAssertEqual(store.mode, .local)
        XCTAssertEqual(processes.terminated, [], "cancel never stops the engine")

        view.confirmAndConnectToAnotherServer()
        XCTAssertEqual(store.mode, .external, "confirm saves the external choice")
        XCTAssertEqual(processes.terminated, [pid], "confirm stops the engine")
        XCTAssertEqual(view.localConnectionPhase, .idle)
    }

    /// External mode never shows the mode-switch dialog at all.
    func testAnExternalSessionNeverAsksForTheModeSwitch() {
        let store = TransientEngineModeStore()
        store.mode = .external
        var asked = 0
        let view = ConnectionView(
            session: AppSessionStatus(),
            lastServer: lastServer,
            hostStore: hostStore,
            engineMode: .external,
            modeStore: store,
            modeSwitchDialog: { _ in asked += 1; return true }
        )
        view.confirmAndConnectToAnotherServer()
        XCTAssertEqual(asked, 0)
        XCTAssertEqual(store.mode, .external)
    }

    // MARK: Tray (local engine)

    private func makeController(
        status: AppSessionStatus,
        supervisor: EngineSupervisor? = nil,
        localEngineMode: Bool = false
    ) -> MenuBarController {
        MenuBarController(
            status: status,
            statusItemFactory: LifecycleStatusItemFactory(),
            openWindow: {},
            quit: {},
            changeHost: {},
            engineSupervisor: supervisor,
            localEngineMode: localEngineMode
        )
    }

    /// While the quit path stops the engine, the tray shows the fixed stopping
    /// line — the real entry title AppKit draws — and connection updates
    /// cannot overwrite it mid-drain.
    func testTheQuittingTitleHoldsThroughConnectionUpdates() {
        let status = AppSessionStatus()
        status.reportConnected(Origin("http://127.0.0.1:8777")!)
        let controller = makeController(status: status)
        controller.start()
        XCTAssertEqual(controller.currentTitle, "Connected to http://127.0.0.1:8777")
        XCTAssertEqual(controller.menu?.items.first?.title, "Connected to http://127.0.0.1:8777")

        controller.setQuittingTitle()
        XCTAssertEqual(controller.currentTitle, "Stopping Ciaobot…")
        XCTAssertEqual(controller.menu?.items.first?.title, "Stopping Ciaobot…")

        status.reportDisconnected()
        XCTAssertEqual(controller.menu?.items.first?.title, "Stopping Ciaobot…",
                       "the immediate connection update keeps the line")
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        XCTAssertEqual(controller.currentTitle, "Stopping Ciaobot…", "the drain keeps the stopping line")
        XCTAssertEqual(controller.menu?.items.first?.title, "Stopping Ciaobot…",
                       "the deferred rebuild keeps the line AppKit draws")

        // A stopped and restarted item shows the connection line again.
        controller.stop()
        controller.start()
        XCTAssertEqual(controller.currentTitle, "Not connected")
    }

    /// The restart row exists exactly while the local engine is down, and
    /// clicking it starts the engine again.
    func testTheRestartRowAppearsOnlyWhenTheEngineIsStopped() throws {
        let supervisor = makeSupervisor()
        let controller = makeController(status: AppSessionStatus(), supervisor: supervisor, localEngineMode: true)
        controller.start()

        // Running: no row.
        _ = try bringUp(supervisor)
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertNil(controller.menu?.deepRow(withTitle: MenuBarController.engineStoppedRowTitle))

        // portInUse: the row is there and restarts the engine on click.
        _ = collidePort(supervisor)
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        let row = controller.menu?.deepRow(withTitle: MenuBarController.engineStoppedRowTitle)
        XCTAssertEqual(row?.isEnabled, true)
        let spawnsBefore = processes.spawns.count
        controller.restartEngine(row)
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertEqual(processes.spawns.count, spawnsBefore + 1, "the row starts the engine again")

        // The respawned child becomes ready, so the row goes away again.
        let respawned = processes.nextPID
        try writeReady(pid: respawned)
        identity.answer = EngineIdentity(pid: respawned, startupID: "abc123")
        time.advance(0.6)
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertEqual(supervisor.state,
                       EngineState.running(origin: Origin("http://127.0.0.1:8777")!, pid: respawned, startupID: "abc123"))
        XCTAssertNil(controller.menu?.deepRow(withTitle: MenuBarController.engineStoppedRowTitle),
                     "the row goes when the engine runs again")

        // A copy of the running engine that dies while an unrelated process
        // holds its record cannot be replaced: the supervisor fails, and the
        // row is there too.
        processes.alive[respawned] = EngineProcessInfo(parentPID: 42, path: nil)
        processes.onExit?(respawned, 1)
        time.advance(2)
        guard case .failed = supervisor.state else {
            return XCTFail("expected failed after the unexpected exit")
        }
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertNotNil(controller.menu?.deepRow(withTitle: MenuBarController.engineStoppedRowTitle))
    }

    /// A tray without a supervisor never shows the restart row.
    func testAnExternalTrayHasNoRestartRow() {
        let controller = makeController(status: AppSessionStatus())
        controller.start()
        XCTAssertNil(controller.menu?.deepRow(withTitle: MenuBarController.engineStoppedRowTitle))
    }

    /// In local mode the change-server row works before a connection exists,
    /// because it switches the engine mode itself; external mode keeps its
    /// shipped availability.
    func testTheChangeServerRowIsAvailableBeforeAConnectionOnlyInLocalMode() throws {
        do {
            let controller = makeController(status: AppSessionStatus(), localEngineMode: false)
            controller.start()
            let row = try XCTUnwrap(controller.menu?.deepRow(withTitle: MenuBarController.changeHostTitle))
            XCTAssertFalse(row.isEnabled)
            XCTAssertNil(row.target)
        }
        do {
            let controller = makeController(status: AppSessionStatus(), localEngineMode: true)
            controller.start()
            let row = try XCTUnwrap(controller.menu?.deepRow(withTitle: MenuBarController.changeHostTitle))
            XCTAssertTrue(row.isEnabled)
            XCTAssertTrue(row.target === controller)
        }
    }
}

private extension NSMenu {
    /// Searches the whole tree, like the helper in `MenuBarTests`.
    func deepRow(withTitle title: String) -> NSMenuItem? {
        if let item = item(withTitle: title) { return item }
        for item in items {
            if let submenu = item.submenu, let found = submenu.deepRow(withTitle: title) {
                return found
            }
        }
        return nil
    }
}
