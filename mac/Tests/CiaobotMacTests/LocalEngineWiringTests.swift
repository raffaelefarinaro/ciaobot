import XCTest
@testable import CiaobotMac

/// #3129 S5b: local mode is wired into the app. The app delegate starts the
/// engine at launch in local mode and never in external mode, the connection
/// view serves the engine screen without same-Mac discovery or a saved-origin
/// auto connect, and the DEBUG launch overrides stay pure and fail closed.
@MainActor
private final class StubMainWindowPresenter: MainWindowPresenting {
    private(set) var presentCount = 0
    @discardableResult func present() -> Bool { presentCount += 1; return true }
}

@MainActor
private final class StubMenuBarController: MenuBarControlling {
    private(set) var startCount = 0
    private(set) var stopCount = 0
    private(set) var quittingCount = 0
    func start() { startCount += 1 }
    func stop() { stopCount += 1 }
    func setQuittingTitle() { quittingCount += 1 }
}

/// Records what the supervisor asked instead of running any process.
private final class CountingEngineProcesses: EngineProcessRunning {
    var onExit: ((Int32, Int32) -> Void)?
    private(set) var spawns: [(launch: BundledEngineLaunch, log: URL)] = []
    private(set) var terminated: [Int32] = []
    private(set) var killed: [Int32] = []

    func spawn(_ launch: BundledEngineLaunch, log: URL) throws -> Int32 {
        spawns.append((launch, log))
        return 5100
    }
    func terminate(_ pid: Int32) { terminated.append(pid) }
    func kill(_ pid: Int32) { killed.append(pid) }
    func inspect(_ pid: Int32) -> EngineProcessInfo? { nil }
}

private final class StubIdentity: EngineIdentityChecking {
    func identity(of origin: Origin, completion: @escaping (EngineIdentity?) -> Void) {
        completion(nil)
    }
}

/// The connection probe of `ConnectionRememberingTests`: an answer without
/// contacting anything.
private final class StubProbe: ProbeChecking {
    private let responds: Bool
    private(set) var checked: [Origin] = []

    init(responds: Bool) { self.responds = responds }

    func check(_ origin: Origin, completion: @escaping (Bool) -> Void) {
        checked.append(origin)
        completion(responds)
    }
}

/// A probe whose answers the test releases one at a time, so a probe can be
/// in flight while the engine publishes another origin.
private final class DeferredProbe: ProbeChecking {
    private(set) var checked: [Origin] = []
    private var completions: [(Bool) -> Void] = []

    func check(_ origin: Origin, completion: @escaping (Bool) -> Void) {
        checked.append(origin)
        completions.append(completion)
    }

    func answer(_ index: Int, ok: Bool) { completions[index](ok) }
}

private final class SpyTransport {
    var calls = 0
}

@MainActor
final class LocalEngineWiringTests: XCTestCase {
    private var suiteName = ""
    private var defaults: UserDefaults!
    private var lastServer: UserDefaultsLastServerStore!
    private var hostStore: UserDefaultsHostListStore!

    override func setUp() {
        super.setUp()
        suiteName = IsolatedDefaults.makeName("LocalEngineWiring")
        defaults = UserDefaults(suiteName: suiteName)
        defaults.removePersistentDomain(forName: suiteName)
        lastServer = UserDefaultsLastServerStore(defaults: defaults)
        hostStore = UserDefaultsHostListStore(defaults: defaults, lastServer: lastServer)
    }

    override func tearDown() {
        IsolatedDefaults.remove(suiteName)
        defaults = nil
        lastServer = nil
        hostStore = nil
        super.tearDown()
    }

    private func makeTempDirectory(_ label: String) throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("LocalEngineWiring-\(label)-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url.resolvingSymlinksInPath(), withIntermediateDirectories: true)
        return url
    }

    private func makeDelegate(
        mode: EngineMode?,
        processes: CountingEngineProcesses,
        home: URL
    ) -> AppDelegate {
        AppDelegate(
            session: AppSessionStatus(),
            presenter: StubMainWindowPresenter(),
            makeMenuBar: { _, _, _, _, _ in StubMenuBarController() },
            mode: mode,
            supervisor: EngineSupervisor(
                engine: BundledEngine(root: home.appendingPathComponent("engine")),
                home: EngineHome(home: home),
                processes: processes,
                identity: StubIdentity(),
                environment: ["PATH": "/usr/bin"]
            )
        )
    }

    // MARK: Mode resolution

    /// The saved choice wins, an Intel host or a remote last connection is
    /// external, and a fixture launch (`CIAOBOT_MAC_TEST_ORIGIN` or an
    /// auto-connect request) never runs the bundled engine.
    func testLaunchEngineModeTable() {
        let local = Origin("http://127.0.0.1:8443")
        let remote = Origin("https://ciaobot.example.com")
        XCTAssertEqual(AppDelegate.launchEngineMode(saved: nil, lastOrigin: nil, addressedTestLaunch: false), .local)
        XCTAssertEqual(AppDelegate.launchEngineMode(saved: nil, lastOrigin: local, addressedTestLaunch: false), .local)
        XCTAssertEqual(AppDelegate.launchEngineMode(saved: nil, lastOrigin: remote, addressedTestLaunch: false), .external)
        XCTAssertEqual(AppDelegate.launchEngineMode(saved: nil, lastOrigin: local, addressedTestLaunch: true), .external,
                       "a fixture launch never owns the engine")
        XCTAssertEqual(AppDelegate.launchEngineMode(saved: "local", lastOrigin: remote, addressedTestLaunch: false), .local,
                       "a saved choice wins")
        XCTAssertEqual(AppDelegate.launchEngineMode(saved: "external", lastOrigin: local, addressedTestLaunch: false), .external)
        XCTAssertEqual(AppDelegate.launchEngineMode(saved: "local", lastOrigin: nil, addressedTestLaunch: true), .external,
                       "an addressed fixture launch stays external even with a saved local mode")
        XCTAssertEqual(AppDelegate.launchEngineMode(saved: "external", lastOrigin: nil, addressedTestLaunch: true), .external)
    }

    // MARK: The app delegate starts (only) a local engine

    func testLocalModeStartsTheEngineAtLaunch() throws {
        let home = try makeTempDirectory("home-local")
        let processes = CountingEngineProcesses()
        let delegate = makeDelegate(mode: .local, processes: processes, home: home)
        XCTAssertEqual(delegate.mode, .local)

        delegate.applicationDidFinishLaunching(
            Notification(name: NSApplication.didFinishLaunchingNotification)
        )

        XCTAssertEqual(processes.spawns.count, 1, "Local mode starts the app-owned engine at launch, not from a view.")
        XCTAssertEqual(processes.spawns.first?.launch.arguments, ["run", "--app-engine"])
        XCTAssertEqual(processes.spawns.first?.log, EngineHome(home: home).logFile)
    }

    func testExternalModeNeverStartsTheEngineAtLaunch() throws {
        let home = try makeTempDirectory("home-external")
        let processes = CountingEngineProcesses()
        let delegate = makeDelegate(mode: .external, processes: processes, home: home)
        XCTAssertEqual(delegate.mode, .external)

        delegate.applicationDidFinishLaunching(
            Notification(name: NSApplication.didFinishLaunchingNotification)
        )

        XCTAssertTrue(processes.spawns.isEmpty, "External mode must never start the app-owned engine.")
        XCTAssertTrue(processes.terminated.isEmpty)
        XCTAssertTrue(processes.killed.isEmpty)
    }

    /// The initializer without an engine injection resolves to external, so an
    /// ordinary test launch runs nothing regardless of this Mac's facts.
    func testTheTestInitializerDefaultsToExternalMode() {
        let delegate = AppDelegate(
            session: AppSessionStatus(),
            presenter: StubMainWindowPresenter(),
            makeMenuBar: { _, _, _, _, _ in StubMenuBarController() }
        )
        XCTAssertEqual(delegate.mode, .external)
        XCTAssertNil(delegate.supervisor, "The test initializer runs no engine unless one is injected.")
    }

    // MARK: ConnectionView in local mode

    private func makeDiscovery(_ spy: SpyTransport) -> LocalServerDiscovery {
        LocalServerDiscovery(
            candidate: Origin("http://127.0.0.1:8443"),
            detectInstallation: { nil },
            transport: { _, completion in spy.calls += 1; completion(nil) }
        )
    }

    /// Debug builds default discovery off, so force it on: only the real guard
    /// in `startAutomaticDiscovery` can keep local mode from probing.
    func testLocalModeDoesNotStartSameMacDiscovery() {
        let localSpy = SpyTransport()
        let local = ConnectionView(
            discovery: makeDiscovery(localSpy), autoDiscovery: true,
            lastServer: lastServer, hostStore: hostStore, engineMode: .local)
        local.startAutomaticDiscovery()
        XCTAssertEqual(localSpy.calls, 0, "Local mode probes via the supervisor, never via same-Mac discovery.")

        let externalSpy = SpyTransport()
        let external = ConnectionView(
            discovery: makeDiscovery(externalSpy), autoDiscovery: true,
            lastServer: lastServer, hostStore: hostStore, engineMode: .external)
        external.startAutomaticDiscovery()
        XCTAssertEqual(externalSpy.calls, 1, "The external screen keeps its shipped discovery policy.")
    }

    /// The appear closure's external-only launch machinery (an install that
    /// finished while the window was closed) must not run in local mode.
    func testLocalModeAppearRunsNoExternalLaunchMachinery() throws {
        let service = try XCTUnwrap(Origin("http://127.0.0.1:8443"))
        let localProbe = StubProbe(responds: true)
        let local = ConnectionView(
            probe: localProbe, lastServer: lastServer, hostStore: hostStore, engineMode: .local)
        local.launchOnAppear(installState: .succeeded(service))
        XCTAssertTrue(localProbe.checked.isEmpty)

        let externalProbe = StubProbe(responds: true)
        let external = ConnectionView(
            probe: externalProbe, lastServer: lastServer, hostStore: hostStore, engineMode: .external)
        external.launchOnAppear(installState: .succeeded(service))
        XCTAssertEqual(externalProbe.checked.map(\.canonicalKey), [service.canonicalKey])
    }

    func testLocalModeHasNoSavedOriginAutoConnect() throws {
        let remote = try XCTUnwrap(Origin("https://remote.example.com"))
        lastServer.remember(remote)

        let local = ConnectionView(lastServer: lastServer, hostStore: hostStore, engineMode: .local)
        XCTAssertNil(local.autoConnectTarget,
                     "The running engine's own origin connects the session; a saved server must not.")
        let external = ConnectionView(lastServer: lastServer, hostStore: hostStore, engineMode: .external)
        XCTAssertEqual(external.autoConnectTarget?.canonicalKey, remote.canonicalKey,
                       "External keeps its saved-origin launch behaviour.")
    }

    // MARK: The running origin connects, and reconnects when it changes

    func testRunningOriginConnectsWithoutBeingSaved() throws {
        let probe = StubProbe(responds: true)
        let session = AppSessionStatus()
        let view = makeLocalView(probe: probe, session: session)

        view.connectRunningEngine(Origin("http://127.0.0.1:8777")!)

        XCTAssertEqual(probe.checked.map(\.canonicalKey), ["http://127.0.0.1:8777"])
        XCTAssertEqual(session.currentOrigin?.canonicalKey, "http://127.0.0.1:8777")
        XCTAssertNil(lastServer.savedOrigin, "The engine's ephemeral loopback origin is not a saved server.")
        XCTAssertTrue(hostStore.hosts.isEmpty, "...and not a saved host.")
    }

    func testARepeatedRunningOriginKeepsTheSession() throws {
        let probe = StubProbe(responds: true)
        let session = AppSessionStatus()
        let view = makeLocalView(probe: probe, session: session)
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:8777"))

        view.connectRunningEngine(origin)
        view.connectRunningEngine(origin)

        XCTAssertEqual(probe.checked.count, 1, "An engine restart that kept its port re-publishes the same origin.")
        XCTAssertEqual(session.currentOrigin, origin, "The session stays.")
    }

    func testAChangedRunningOriginReplacesTheSession() throws {
        let probe = StubProbe(responds: true)
        let session = AppSessionStatus()
        let view = makeLocalView(probe: probe, session: session)
        let first = try XCTUnwrap(Origin("http://127.0.0.1:8777"))
        let second = try XCTUnwrap(Origin("http://127.0.0.1:9001"))

        view.connectRunningEngine(first)
        XCTAssertEqual(session.currentOrigin, first)

        // The probe for the second origin stays in flight, so the only thing
        // that can have cleared the first session is the replacement itself.
        let deferred = DeferredProbe()
        let replacing = makeLocalView(probe: deferred, session: session)
        replacing.connectRunningEngine(first)
        deferred.answer(0, ok: true)
        replacing.connectRunningEngine(second)
        XCTAssertNil(session.currentOrigin, "A changed origin tears the old session down before probing.")
        deferred.answer(1, ok: true)
        XCTAssertEqual(session.currentOrigin, second)
        XCTAssertEqual(deferred.checked, [first, second])
    }

    /// A probe for the old origin is in flight when the engine publishes a new
    /// one: the stale answer must not win.
    func testAnOriginPublishedDuringAnInFlightProbeIsNotLost() throws {
        let probe = DeferredProbe()
        let session = AppSessionStatus()
        let view = makeLocalView(probe: probe, session: session)
        let old = try XCTUnwrap(Origin("http://127.0.0.1:8777"))
        let new = try XCTUnwrap(Origin("http://127.0.0.1:9001"))

        view.connectRunningEngine(old)
        view.connectRunningEngine(new)
        XCTAssertEqual(probe.checked, [old], "One probe at a time.")

        probe.answer(0, ok: true)
        XCTAssertNil(session.currentOrigin, "The stale probe must not connect the old origin.")
        XCTAssertEqual(probe.checked, [old, new], "The newer origin is probed once the old probe ends.")
        probe.answer(1, ok: true)
        XCTAssertEqual(session.currentOrigin, new)
    }

    // MARK: Failure is a state, not a retry trigger

    func testAFailedProbeShowsFailureAndDoesNotRetryByItself() throws {
        let probe = StubProbe(responds: false)
        let view = makeLocalView(probe: probe)
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:8777"))

        view.connectRunningEngine(origin)
        guard case .failed(let failed, let message) = view.localConnectionPhase else {
            return XCTFail("a failed probe is a visible failure state")
        }
        XCTAssertEqual(failed, origin)
        XCTAssertFalse(message.isEmpty)
        view.connectRunningEngine(origin) // the screen re-appearing, or a republish
        XCTAssertEqual(probe.checked.count, 1, "No automatic retry loop.")
    }

    func testManualRetryIsBounded() throws {
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:8777"))
        let model = LocalConnectionModel()
        _ = model.engineRunning(origin)
        _ = model.probeFinished(origin, ok: false)
        for attempt in 1...LocalConnectionModel.maxManualRetries {
            XCTAssertEqual(model.retry(engineOrigin: origin), [.probe(origin)], "retry \(attempt)")
            _ = model.probeFinished(origin, ok: false)
        }
        XCTAssertFalse(model.canRetry)
        XCTAssertEqual(model.retry(engineOrigin: origin), [], "No retry past the bound.")
    }

    func testAWebViewFailureBecomesAFailureStateWithoutReconnecting() throws {
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:8777"))
        let model = LocalConnectionModel()
        _ = model.engineRunning(origin)
        XCTAssertEqual(model.probeFinished(origin, ok: true), [.establish(origin)])
        XCTAssertEqual(model.sessionFailed("load failed"), [.dropSession])
        XCTAssertEqual(model.phase, .failed(origin: origin, message: "load failed"))
        XCTAssertEqual(model.engineRunning(origin), [], "Re-appearing with the same origin does not loop.")
        let moved = try XCTUnwrap(Origin("http://127.0.0.1:9001"))
        XCTAssertEqual(model.engineRunning(moved), [.probe(moved)], "A moved engine is a new attempt.")
    }

    func testModelReplacesAndKeepsSessions() throws {
        let a = try XCTUnwrap(Origin("http://127.0.0.1:8777"))
        let b = try XCTUnwrap(Origin("http://127.0.0.1:9001"))
        let model = LocalConnectionModel()
        XCTAssertEqual(model.engineRunning(a), [.probe(a)])
        _ = model.probeFinished(a, ok: true)
        XCTAssertEqual(model.engineRunning(a), [], "the same origin keeps the session")
        XCTAssertEqual(model.engineRunning(b), [.dropSession, .probe(b)], "a changed origin replaces it")
    }

    // MARK: Status screen mapping

    func testStatusScreenButtonsFollowTheState() throws {
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:8777"))
        let all = LocalEnginePresentation.allActions
        func make(_ engine: EngineState, _ phase: LocalConnectionModel.Phase = .idle, retry: Bool = true) -> LocalEnginePresentation {
            LocalEnginePresentation.make(engine: engine, connection: phase, canRetry: retry)
        }
        XCTAssertEqual(make(.starting).actions, [])
        XCTAssertTrue(make(.starting).showsProgress)
        XCTAssertEqual(make(.stopped).actions, [])
        XCTAssertEqual(make(.stopping).actions, [])
        XCTAssertEqual(make(.failed("boom")).actions, all)
        XCTAssertEqual(make(.failed("boom")).message, "boom")
        XCTAssertEqual(make(.portInUse(8443)).actions, all)
        XCTAssertTrue(make(.portInUse(8443)).message.contains("8443"))
        let running = EngineState.running(origin: origin, pid: 1, startupID: "x")
        XCTAssertEqual(make(running, .probing(origin)).actions, [])
        XCTAssertEqual(make(running, .failed(origin: origin, message: "no answer")).actions, all)
        XCTAssertEqual(make(running, .failed(origin: origin, message: "no answer")).message, "no answer")
        XCTAssertEqual(make(running, .failed(origin: origin, message: "no answer"), retry: false).actions,
                       [.showLog, .connectElsewhere], "Try Again goes away when the bound is used")
        XCTAssertTrue(LocalEnginePresentation.engineIsDown(.failed("x")))
        XCTAssertTrue(LocalEnginePresentation.engineIsDown(.portInUse(1)))
        XCTAssertFalse(LocalEnginePresentation.engineIsDown(running))
        XCTAssertFalse(LocalEnginePresentation.engineIsDown(.starting))
        XCTAssertEqual(LocalEnginePresentation.retry(engine: .failed("x")), .restartEngine)
        XCTAssertEqual(LocalEnginePresentation.retry(engine: .portInUse(1)), .startEngine)
        XCTAssertEqual(LocalEnginePresentation.retry(engine: running), .reconnect(origin))
        XCTAssertEqual(LocalEnginePresentation.retry(engine: .starting), .none)
    }

    // MARK: Connect to Another Server

    func testConnectToAnotherServerStopsTheEngineAndSavesExternal() throws {
        let home = try makeTempDirectory("home-connect-elsewhere")
        let processes = CountingEngineProcesses()
        let supervisor = EngineSupervisor(
            engine: BundledEngine(root: home.appendingPathComponent("engine")),
            home: EngineHome(home: home),
            processes: processes,
            identity: StubIdentity(),
            environment: ["PATH": "/usr/bin"]
        )
        supervisor.start()
        XCTAssertEqual(processes.spawns.count, 1)
        let store = TransientEngineModeStore()
        store.mode = .local
        let session = AppSessionStatus()
        let view = ConnectionView(
            probe: StubProbe(responds: true), session: session,
            lastServer: lastServer, hostStore: hostStore,
            supervisor: supervisor, engineMode: .local, modeStore: store)
        view.connectRunningEngine(try XCTUnwrap(Origin("http://127.0.0.1:8777")))
        XCTAssertNotNil(session.currentOrigin)

        view.connectToAnotherServer()

        XCTAssertEqual(store.mode, .external, "The choice is saved for the next launch.")
        XCTAssertEqual(processes.terminated, [5100], "The engine is stopped.")
        XCTAssertNil(session.currentOrigin, "The dead session is dropped.")
        XCTAssertEqual(view.localConnectionPhase, .idle)
    }

    // MARK: The DEBUG launch overrides stay fail closed

    func testLaunchOverridesWithoutTheVariablesAreNil() throws {
        XCTAssertNil(TestLaunch.engineHome(environment: [:]))
        XCTAssertNil(TestLaunch.engineResources(environment: [:]))
        XCTAssertNil(TestLaunch.defaultsSuiteName(environment: [:]))
        XCTAssertEqual(TestLaunch.defaultsSuiteName(environment: ["CIAOBOT_MAC_TEST_DEFAULTS_SUITE": "suite"]), "suite")
        XCTAssertTrue(TestLaunch.addressed(environment: ["CIAOBOT_MAC_TEST_AUTOCONNECT": "1"]))
        XCTAssertFalse(TestLaunch.addressed(environment: [:]))
    }

    func testEngineHomeOverrideRequiresAnExistingAbsoluteDirectory() throws {
        let directory = try makeTempDirectory("engine-home")
        let environment = ["CIAOBOT_MAC_TEST_HOME": directory.resolvingSymlinksInPath().path]
        XCTAssertEqual(TestLaunch.engineHome(environment: environment)?.path,
                       directory.resolvingSymlinksInPath().path)
        for raw in ["", "relative/home", ".", "/private/tmp/LocalEngineWiring-missing-\(UUID().uuidString)"] {
            XCTAssertNil(TestLaunch.engineHome(environment: ["CIAOBOT_MAC_TEST_HOME": raw]), raw)
        }
    }

    func testEngineResourcesOverrideRequiresAnExistingAbsoluteDirectory() throws {
        let directory = try makeTempDirectory("engine-resources")
        let environment = ["CIAOBOT_MAC_TEST_ENGINE_RESOURCES": directory.resolvingSymlinksInPath().path]
        XCTAssertEqual(TestLaunch.engineResources(environment: environment)?.path,
                       directory.resolvingSymlinksInPath().path)
        // A regular file is not a resource directory.
        let file = directory.appendingPathComponent("provenance.json")
        try Data("{}".utf8).write(to: file)
        XCTAssertNil(TestLaunch.engineResources(
            environment: ["CIAOBOT_MAC_TEST_ENGINE_RESOURCES": file.path]))
    }

    func testStoredDefaultsFollowTheSuiteAndLeaveTheAppDefaultsAlone() throws {
        let name = IsolatedDefaults.makeName("StoredDefaults")
        defer { IsolatedDefaults.remove(name) }
        let suite = TestLaunch.storedDefaults(environment: ["CIAOBOT_MAC_TEST_DEFAULTS_SUITE": name])
        suite.set("v", forKey: "probe-key")
        let direct = try XCTUnwrap(UserDefaults(suiteName: name))
        XCTAssertEqual(direct.string(forKey: "probe-key"), "v", "all three stored decisions share the suite")
        XCTAssertTrue(TestLaunch.storedDefaults(environment: [:]) === UserDefaults.standard,
                      "without the suite the standard domain is unchanged")
    }
}

extension LocalEngineWiringTests {
    /// A local-mode view driving the engine's own origin through the ordinary
    /// connect path, with isolated stores and an isolated sign-in registry.
    private func makeLocalView(probe: ProbeChecking, session: AppSessionStatus? = nil) -> CiaobotMac.ConnectionView {
        ConnectionView(
            probe: probe,
            signIn: SessionSignIn(
                registry: SessionStoreRegistry(fileURL: FileManager.default.temporaryDirectory
                    .appendingPathComponent("LocalEngineWiring-Registry-\(UUID().uuidString).json")),
                eraser: LocalEngineWiringNoopEraser(),
                persistentAvailable: false
            ),
            session: session,
            lastServer: lastServer,
            hostStore: hostStore,
            engineMode: .local
        )
    }
}

private final class LocalEngineWiringNoopEraser: WebsiteDataStoreErasing {
    func remove(identifier: UUID, completion: @escaping (Error?) -> Void) {
        completion(nil)
    }
}
