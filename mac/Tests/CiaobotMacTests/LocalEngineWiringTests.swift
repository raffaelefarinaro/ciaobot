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
    func start() { startCount += 1 }
    func stop() { stopCount += 1 }
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

    func testLocalModeDoesNotStartSameMacDiscovery() {
        // Debug builds default discovery off, so force it on: local mode must
        // still refuse it.
        let local = ConnectionView(autoDiscovery: true, lastServer: lastServer, hostStore: hostStore, engineMode: .local)
        XCTAssertFalse(local.automaticallyStartsDiscovery,
                       "Local mode probes via the supervisor, never via same-Mac discovery.")
        let external = ConnectionView(autoDiscovery: true, lastServer: lastServer, hostStore: hostStore, engineMode: .external)
        XCTAssertTrue(external.automaticallyStartsDiscovery,
                       "The external screen keeps its shipped discovery policy.")
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

    func testRunningOriginConnectsAutoWithoutThePrefillPath() throws {
        let probe = StubProbe(responds: true)
        let view = makeLocalView(probe: probe)

        view.connectRunningEngine(Origin("http://127.0.0.1:8777")!)

        XCTAssertEqual(probe.checked.map(\.canonicalKey), ["http://127.0.0.1:8777"])
        XCTAssertEqual(lastServer.savedOrigin?.canonicalKey, "http://127.0.0.1:8777",
                       "The ordinary connect path remembers the origin that answered.")
        XCTAssertEqual(hostStore.hosts.map(\.origin), ["http://127.0.0.1:8777"])
    }

    func testARepeatedRunningOriginDoesNotConnectAgain() throws {
        let local = try XCTUnwrap(Origin("http://127.0.0.1:8777"))
        XCTAssertFalse(ConnectionView.runningOriginNeedsSession(local, candidate: local),
                       "An engine restart that kept its port re-publishes the same origin; the session stays.")
        XCTAssertTrue(ConnectionView.runningOriginNeedsSession(nil, candidate: local),
                      "The first published origin connects.")
        XCTAssertTrue(ConnectionView.runningOriginNeedsSession(try XCTUnwrap(Origin("http://127.0.0.1:9001")), candidate: local),
                      "A different origin reconnects.")
    }

    func testAChangedRunningOriginReplacesTheSession() throws {
        let probe = StubProbe(responds: true)
        let view = makeLocalView(probe: probe)

        view.connectRunningEngine(Origin("http://127.0.0.1:8777")!)
        view.connectRunningEngine(Origin("http://127.0.0.1:9001")!)

        XCTAssertEqual(probe.checked.map(\.canonicalKey), ["http://127.0.0.1:8777", "http://127.0.0.1:9001"],
                       "An engine restart on a new port reconnects by itself.")
        XCTAssertEqual(lastServer.savedOrigin?.canonicalKey, "http://127.0.0.1:9001")
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
    private func makeLocalView(probe: ProbeChecking) -> CiaobotMac.ConnectionView {
        ConnectionView(
            probe: probe,
            signIn: SessionSignIn(
                registry: SessionStoreRegistry(fileURL: FileManager.default.temporaryDirectory
                    .appendingPathComponent("LocalEngineWiring-Registry-\(UUID().uuidString).json")),
                eraser: LocalEngineWiringNoopEraser(),
                persistentAvailable: false
            ),
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
