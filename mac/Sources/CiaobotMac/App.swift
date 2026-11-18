import SwiftUI
import AppKit
import Combine
import WebKit

@main
struct CiaobotMacApp: App {
    // AppKit owns the menu-bar item and the reopen path; SwiftUI keeps the
    // single Window scene. The same session object feeds both.
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        // A single Window (not a WindowGroup) keeps the client to one native
        // main window and removes File > New Window from the menu.
        Window("Ciaobot", id: "main") {
            ConnectionView(
                // One launch defaults domain for the stored mode, last server
                // and saved hosts; a DEBUG test launch keeps all three in its
                // isolated suite.
                session: appDelegate.session,
                lastServer: appDelegate.lastServer,
                hostStore: appDelegate.hostStore,
                installer: appDelegate.installer,
                engineUpdater: appDelegate.engineUpdater,
                appSettings: appDelegate.appSettings,
                supervisor: appDelegate.supervisor,
                engineMode: appDelegate.mode,
                modeStore: appDelegate.engineMode,
                onModeChange: { appDelegate.modeDidChange($0) }
            )
        }
            .defaultSize(width: 1100, height: 760)
            .commands {
                CommandGroup(after: .newItem) {
                    Button("Change Server…") {
                        NotificationCenter.default.post(name: .ciaobotChangeServer, object: nil)
                    }
                    .keyboardShortcut("l", modifiers: [.command, .shift])
                }
            }
    }
}

extension Notification.Name {
    static let ciaobotChangeServer = Notification.Name("CiaobotChangeServer")
}

#if DEBUG
/// Test-only launch controls. They are compiled out of release builds and
/// fail closed: an origin is honoured only when it is an explicit loopback
/// origin, and auto-connect never falls back to the proposed live address.
enum TestLaunch {
    /// A launch that addresses an isolated fixture — an explicit origin, or a
    /// request to auto-connect one. The app stays in external engine mode for
    /// it: a fixture launch must never run the bundled engine.
    static func addressed(environment: [String: String] = ProcessInfo.processInfo.environment) -> Bool {
        origin(environment: environment) != nil || autoConnectRequested(environment: environment)
    }

    static func origin(environment: [String: String] = ProcessInfo.processInfo.environment) -> Origin? {
        guard let raw = environment["CIAOBOT_MAC_TEST_ORIGIN"],
              let origin = Origin(raw), origin.isLoopback else { return nil }
        return origin
    }

    static func autoConnectRequested(environment: [String: String] = ProcessInfo.processInfo.environment) -> Bool {
        environment["CIAOBOT_MAC_TEST_AUTOCONNECT"] == "1"
    }

    static func autoConnect(environment: [String: String] = ProcessInfo.processInfo.environment) -> Bool {
        autoConnectRequested(environment: environment) && origin(environment: environment) != nil
    }

    /// The initial field value. A requested test launch with a missing or
    /// non-loopback origin gets an empty field, never the proposed live
    /// address, so a stray Return cannot contact it.
    static func initialAddress(defaultAddress: String, environment: [String: String] = ProcessInfo.processInfo.environment) -> String {
        if let origin = origin(environment: environment) { return origin.url.absoluteString }
        if autoConnectRequested(environment: environment) { return "" }
        return defaultAddress
    }

    /// The home directory of an isolated local-launch checkpoint: every
    /// app-side engine path (`EngineHome`) and the engine child's `HOME`
    /// derive from it. Honoured only as an absolute path to an existing
    /// directory, so a typo cannot point the whole engine at a made-up place.
    static func engineHome(environment: [String: String] = ProcessInfo.processInfo.environment) -> URL? {
        absoluteExistingDirectory(environment["CIAOBOT_MAC_TEST_HOME"])
    }

    /// The resource directory `BundledEngine.locate(resourceDirectory:)`
    /// reads instead of this bundle's own, so a checkpoint can run against the
    /// engine inside a locally built app. Absolute and existing, as above.
    static func engineResources(environment: [String: String] = ProcessInfo.processInfo.environment) -> URL? {
        absoluteExistingDirectory(environment["CIAOBOT_MAC_TEST_ENGINE_RESOURCES"])
    }

    /// The port the bundled engine serves on, passed as `--port`. Digits only
    /// and 1024-65535, as the engine itself requires; anything else is ignored
    /// and the engine keeps its default.
    static func enginePort(environment: [String: String] = ProcessInfo.processInfo.environment) -> Int? {
        guard let raw = environment["CIAOBOT_MAC_TEST_ENGINE_PORT"],
              !raw.isEmpty, raw.utf8.allSatisfy({ $0 >= 48 && $0 <= 57 }),
              let port = Int(raw), (1024...65535).contains(port) else { return nil }
        return port
    }

    /// The `UserDefaults` suite that holds the stored engine mode, last
    /// server, and saved hosts of a test launch. While it is set the app's
    /// own defaults domain is never read or written.
    static func defaultsSuiteName(environment: [String: String] = ProcessInfo.processInfo.environment) -> String? {
        guard let name = environment["CIAOBOT_MAC_TEST_DEFAULTS_SUITE"], !name.isEmpty else { return nil }
        return name
    }

    /// The defaults the launch's stores use: the isolated suite when set, else
    /// the standard domain as before.
    static func storedDefaults(environment: [String: String] = ProcessInfo.processInfo.environment) -> UserDefaults {
        defaultsSuiteName(environment: environment).flatMap { UserDefaults(suiteName: $0) } ?? .standard
    }

    private static func absoluteExistingDirectory(_ raw: String?) -> URL? {
        guard let raw, raw.hasPrefix("/"), !raw.isEmpty else { return nil }
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: raw, isDirectory: &isDirectory), isDirectory.boolValue else { return nil }
        return URL(fileURLWithPath: raw, isDirectory: true)
    }
}

/// What AppKit knows about the main window, as the GUI launch test needs it.
/// WindowServer's on-screen list only holds windows on the active Space, so a
/// window ordered in while another app's full-screen Space is active is
/// absent from it. AppKit's `isOnActiveSpace` is the public way to tell that
/// case apart from a window that never appeared; the test still checks the
/// window's owner, layer, alpha and size in WindowServer itself.
struct TestWindowState: Equatable {
    let number: Int
    let visible: Bool
    let onActiveSpace: Bool
    let width: Int
    let height: Int

    @MainActor
    init(window: NSWindow) {
        self.init(
            number: window.windowNumber,
            visible: window.isVisible,
            onActiveSpace: window.isOnActiveSpace,
            width: Int(window.frame.width.rounded()),
            height: Int(window.frame.height.rounded())
        )
    }

    init(number: Int, visible: Bool, onActiveSpace: Bool, width: Int, height: Int) {
        self.number = number
        self.visible = visible
        self.onActiveSpace = onActiveSpace
        self.width = width
        self.height = height
    }

    static let path = "/test/window-state"
    /// The header that carries the per-launch report secret. The secret is
    /// never put in the URL, so it cannot reach a request log or a report.
    static let secretHeader = "X-Ciaobot-Test-Report-Secret"
    /// The header in which the fixture hands out its current freshness
    /// challenge. The next report echoes it, which proves that report's state
    /// was read after the fixture issued the challenge.
    static let challengeHeader = "X-Ciaobot-Test-Report-Challenge"

    /// A challenge is a 16–64 character hex value; anything else is ignored,
    /// so a fixture answer can never inject other text into a report URL.
    static func validChallenge(_ value: String?) -> String? {
        guard let value, (16...64).contains(value.count),
              value.allSatisfy({ $0.isHexDigit && $0.isASCII }) else { return nil }
        return value
    }

    /// The report request for the loopback test origin: these five values, the
    /// report's sequence number, the last challenge received and the window's
    /// visibility-transition count in the query, the launch secret in a header,
    /// no cookies and no cache.
    func reportRequest(origin: Origin, sequence: Int, transitions: Int, challenge: String?,
                       secret: String) -> URLRequest? {
        guard var components = URLComponents(url: origin.url, resolvingAgainstBaseURL: false) else { return nil }
        components.path = Self.path
        components.queryItems = [
            URLQueryItem(name: "seq", value: String(sequence)),
            URLQueryItem(name: "challenge", value: challenge ?? ""),
            URLQueryItem(name: "transitions", value: String(transitions)),
            URLQueryItem(name: "number", value: String(number)),
            URLQueryItem(name: "visible", value: visible ? "1" : "0"),
            URLQueryItem(name: "activeSpace", value: onActiveSpace ? "1" : "0"),
            URLQueryItem(name: "width", value: String(width)),
            URLQueryItem(name: "height", value: String(height)),
        ]
        guard let url = components.url else { return nil }
        var request = URLRequest(url: url)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.httpShouldHandleCookies = false
        request.setValue(secret, forHTTPHeaderField: Self.secretHeader)
        return request
    }
}

extension TestLaunch {
    /// The per-launch secret the GUI test generates so its fixture can reject
    /// window reports that did not come from the app it launched. Only a
    /// 32–128 character hex value is accepted; anything else disables the
    /// reporter.
    static func reportSecret(environment: [String: String] = ProcessInfo.processInfo.environment) -> String? {
        guard let secret = environment["CIAOBOT_MAC_TEST_REPORT_SECRET"],
              (32...128).contains(secret.count),
              secret.allSatisfy({ $0.isHexDigit && $0.isASCII }) else { return nil }
        return secret
    }
}

enum TestWindowReportOutcome: Equatable, Sendable {
    case accepted(challenge: String?)
    case failed
}

/// Sends one window report. Redirects are refused, so a report never leaves
/// the validated loopback origin, and only server-trust challenges get
/// default handling. A report counts as accepted only on a 2xx answer, which
/// may carry the fixture's next freshness challenge.
final class TestWindowReportTransport: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    static let shared = TestWindowReportTransport()

    private var session: URLSession!

    init(configuration: URLSessionConfiguration = .ephemeral) {
        super.init()
        let configuration = configuration.copy() as! URLSessionConfiguration
        configuration.timeoutIntervalForRequest = 2
        configuration.httpCookieAcceptPolicy = .never
        configuration.httpShouldSetCookies = false
        configuration.urlCache = nil
        session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
    }

    func send(_ request: URLRequest, completion: @escaping @Sendable (TestWindowReportOutcome) -> Void) {
        session.dataTask(with: request) { _, response, error in
            let http = response as? HTTPURLResponse
            guard error == nil, let http, (200...299).contains(http.statusCode) else {
                completion(.failed)
                return
            }
            completion(.accepted(challenge: TestWindowState.validChallenge(
                http.value(forHTTPHeaderField: TestWindowState.challengeHeader))))
        }.resume()
    }

    /// Ends the session; the shared transport lives for the app's lifetime.
    func invalidate() {
        session.invalidateAndCancel()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        if challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust {
            completionHandler(.performDefaultHandling, nil)
        } else {
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }
}

/// Counts every event that can change whether the main window is ordered in,
/// as AppKit delivers it, rather than inferring changes from samples. Two
/// readings with the same count prove the window was not ordered out, closed,
/// miniaturized or hidden with the app, nor replaced, at any moment between
/// them, even if it was visible at both.
///
/// AppKit posts key-value observation for `isVisible` synchronously on every
/// order-in and order-out, including `orderOut`, `close` and
/// `makeKeyAndOrderFront`; the window and app notifications cover
/// miniaturizing and hiding the app. Every change counts, in either direction.
@MainActor
final class TestWindowTransitionCounter {
    private(set) var count = 0
    private var window: NSWindow?
    private var visibility: NSKeyValueObservation?
    private var tokens: [NSObjectProtocol] = []

    /// Starts observing `window`. A different window than the one observed so
    /// far counts as a transition, since the earlier window's history no
    /// longer applies.
    func track(_ window: NSWindow) {
        guard window !== self.window else { return }
        if self.window != nil { count += 1 }
        stop()
        self.window = window
        visibility = window.observe(\.isVisible, options: [.old, .new]) { [weak self] _, change in
            guard change.oldValue != change.newValue else { return }
            Self.onMain { self?.count += 1 }
        }
        let center = NotificationCenter.default
        let windowEvents: [Notification.Name] = [
            NSWindow.willCloseNotification, NSWindow.didMiniaturizeNotification, NSWindow.didDeminiaturizeNotification,
        ]
        let appEvents: [Notification.Name] = [NSApplication.didHideNotification, NSApplication.didUnhideNotification]
        for name in windowEvents {
            tokens.append(center.addObserver(forName: name, object: window, queue: nil) { [weak self] _ in
                Self.onMain { self?.count += 1 }
            })
        }
        for name in appEvents {
            tokens.append(center.addObserver(forName: name, object: nil, queue: nil) { [weak self] _ in
                Self.onMain { self?.count += 1 }
            })
        }
    }

    func stop() {
        visibility?.invalidate()
        visibility = nil
        tokens.forEach(NotificationCenter.default.removeObserver)
        tokens = []
    }

    /// AppKit delivers these on the main thread; anything else is counted
    /// there too, after the fact, which only makes a check stricter.
    private nonisolated static func onMain(_ body: @escaping @MainActor @Sendable () -> Void) {
        if Thread.isMainThread {
            MainActor.assumeIsolated { body() }
        } else {
            DispatchQueue.main.async { body() }
        }
    }
}

/// Reports the main window's current state to the loopback test origin on
/// every tick. It runs only in a DEBUG test launch with a loopback origin and
/// a report secret, sends nothing until the main window exists, and stops
/// after a bounded number of reports.
///
/// Nothing is suppressed as "already delivered": a lost response cannot tell
/// the reporter what the fixture recorded, so every tick reports the state as
/// it is now. One report is in flight at a time and each carries a higher
/// sequence number, so the fixture can keep the newest state even if requests
/// arrive out of order. Each report echoes the last challenge the fixture
/// handed out; the GUI test accepts only a report that answers a challenge it
/// issued during the current check, so a stale report never counts. Each
/// report also carries the window's transition count, read in the same main
/// actor turn as its state.
@MainActor
final class TestWindowReporter {
    typealias Send = @MainActor (URLRequest, @escaping @MainActor @Sendable (TestWindowReportOutcome) -> Void) -> Void

    /// About two minutes of 0.1 s ticks, longer than the GUI test runs.
    static let maximumReports = 1200

    private let origin: Origin
    private let secret: String
    private let mainWindow: () -> NSWindow?
    private let send: Send
    private var challenge: String?
    private var inFlight = false
    private var sequence = 0
    private var timer: Timer?
    private let transitions = TestWindowTransitionCounter()

    init(
        origin: Origin,
        secret: String,
        mainWindow: @escaping @MainActor () -> NSWindow? = { MainWindowPresenter.mainWindow(in: NSApp.windows) },
        send: @escaping Send = TestWindowReporter.sendOverSharedTransport
    ) {
        self.origin = origin
        self.secret = secret
        self.mainWindow = mainWindow
        self.send = send
    }

    func start() {
        guard timer == nil else { return }
        let timer = Timer(timeInterval: 0.1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.poll() }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        transitions.stop()
    }

    /// Sends the state read now, unless a report is still in flight.
    func poll() {
        guard !inFlight else { return }
        guard sequence < Self.maximumReports else {
            stop()
            return
        }
        guard let window = mainWindow() else { return }
        transitions.track(window)
        let state = TestWindowState(window: window)
        sequence += 1
        guard let request = state.reportRequest(origin: origin, sequence: sequence, transitions: transitions.count,
                                                challenge: challenge, secret: secret) else { return }
        inFlight = true
        send(request) { [weak self] outcome in
            guard let self else { return }
            self.inFlight = false
            if case .accepted(let next?) = outcome { self.challenge = next }
        }
    }

    static func sendOverSharedTransport(_ request: URLRequest,
                                        completion: @escaping @MainActor @Sendable (TestWindowReportOutcome) -> Void) {
        TestWindowReportTransport.shared.send(request) { outcome in
            Task { @MainActor in completion(outcome) }
        }
    }
}
#endif

@MainActor
struct ConnectionView: View {
    private static let defaultAddress = "http://127.0.0.1:8443"

    @State private var address: String
    @State private var origin: Origin?
    @State private var storage: SessionStorage = .ephemeral
    @State private var dataStore: WKWebsiteDataStore?
    @State private var storageNotice: String?
    @State private var keepSignedInChoice: Bool?
    @State private var tasks = ConnectionTaskGate()
    @State private var error: String?
    @State private var forgetMessage: String?
    @State private var didAutoConnect = false
    @State private var viewTracker = SessionViewTracker()
    @State private var wizardLoadError: String?
    @State private var portText = ""
    @State private var portError: String?
    @State private var pendingPortRestart = false
    /// The engine mode this connection session runs in. Initialized from the
    /// launch's resolved mode; **Connect to Another Server…** switches it to
    /// external mid-session, behind the confirmation (S6).
    @State private var engineMode: EngineMode
    /// The local-mode connection decisions (probe, replace, failure, bounded
    /// retry). A class so the decisions stay testable without a rendered view.
    @State private var localModel = LocalConnectionModel()
    /// Mirror of `localModel.phase` that drives the status screen.
    @State private var localPhase: LocalConnectionModel.Phase = .idle
    /// The engine itself failed or lost its port while the web view was
    /// connected; the status screen replaces the dead web view.
    @State private var engineDown = false
    @Environment(\.colorScheme) private var colorScheme
    @StateObject private var discovery: LocalServerDiscovery
    @StateObject private var installer: NativeInstaller
    @StateObject private var engineUpdater: EngineUpdater
    private let probe: ProbeChecking
    private let signIn: SessionSignIn
    private let session: AppSessionStatus?
    private let appSettings: AppSettingsBridge?
    /// The app-owned bundled engine, non-nil in production. Local mode drives
    /// the connection from its published running origin; external mode never
    /// starts or observes it.
    private let supervisor: EngineSupervisor?
    /// Writes a mid-session engine-mode switch so the next launch starts in it.
    private let modeStore: EngineModeStoring?
    /// Internal so a test can prove local mode has no saved-origin auto
    /// connect without rendering the view.
    let autoConnectTarget: Origin?
    private let autoDiscovery: Bool
    private let lastServer: LastServerStoring
    private let hostStore: HostListStoring
    /// Presents the mode-switch confirmation (S6). The real one is the AppKit
    /// dialog; tests record the call and decide.
    private let presentModeSwitchDialog: @MainActor (EngineLifecycle.ModeSwitchDialog) -> Bool
    /// Tells the app delegate (quit path, tray) about an in-session mode switch.
    private let onModeChange: (@MainActor (EngineMode) -> Void)?
    /// Arm64 only: an Intel Mac has no bundled engine to use.
    private let hostIsArm64: Bool
    /// A test launch never reads or writes the saved host list, exactly as it
    /// never reads the saved origin: the operator's real defaults stay out of
    /// the fixture run.
    private let isTestLaunch: Bool
    private var checking: Bool { tasks.task == .checking }
    private var forgetting: Bool { tasks.task == .forgetting }
    /// Connection controls are disabled while a connection task, an install, or an engine update runs.
    private var busy: Bool { tasks.isBusy || installer.state.isActive || engineUpdater.state.isActive }
    private var installFailure: NativeInstallFailure? {
        if case .failed(let failure) = installer.state { return failure }
        return nil
    }
    private var engineUpdateProgress: String { EngineUpdateCopy.progress(for: engineUpdater.state) ?? "" }
    private var engineUpdateFailed: Bool {
        if case .failed = engineUpdater.state { return true }
        return false
    }
    private var repairOffered: Bool {
        NativeRepairOffer.isOffered(discovery: discovery.status, installState: installer.state,
                                    updateFailed: engineUpdateFailed,
                                    hasInstallation: discovery.installation != nil)
    }

    /// Whether this connection session may start the same-Mac discovery by
    /// itself. Local mode never probes: the app's own engine is the only
    /// local server there, and its origin comes from the supervisor.
    /// `startAutomaticDiscovery` is guarded by this same value.
    var automaticallyStartsDiscovery: Bool { engineMode == .external && autoDiscovery }

    /// The value the address field starts with. Exposed so a test can prove a
    /// saved origin is prefilled without rendering the view.
    let launchAddress: String

    init(
        probe: ProbeChecking = Probe(),
        signIn: SessionSignIn? = nil,
        discovery: LocalServerDiscovery? = nil,
        autoDiscovery: Bool = LocalServerDiscoveryPolicy.automaticInThisBuild,
        session: AppSessionStatus? = nil,
        lastServer: LastServerStoring = UserDefaultsLastServerStore(),
        hostStore: HostListStoring = UserDefaultsHostListStore(lastServer: UserDefaultsLastServerStore()),
        installer: NativeInstaller? = nil,
        engineUpdater: EngineUpdater? = nil,
        appSettings: AppSettingsBridge? = nil,
        supervisor: EngineSupervisor? = nil,
        engineMode: EngineMode = .external,
        modeStore: EngineModeStoring? = nil,
        modeSwitchDialog: (@MainActor (EngineLifecycle.ModeSwitchDialog) -> Bool)? = nil,
        onModeChange: (@MainActor (EngineMode) -> Void)? = nil,
        hostIsArm64: Bool = EngineSupervisor.hostIsArm64
    ) {
        self.onModeChange = onModeChange
        self.hostIsArm64 = hostIsArm64
        self.probe = probe
        self.signIn = signIn ?? .live
        self.autoDiscovery = autoDiscovery
        self.session = session
        self.appSettings = appSettings
        self.lastServer = lastServer
        self.hostStore = hostStore
        self.presentModeSwitchDialog = modeSwitchDialog ?? { dialog in
            EngineLifecycle.presentConfirmation(
                title: dialog.title, body: dialog.body, confirm: dialog.confirm, cancel: dialog.cancel
            )
        }
        _discovery = StateObject(wrappedValue: discovery ?? LocalServerDiscovery())
        _installer = StateObject(wrappedValue: installer ?? NativeInstaller())
        _engineUpdater = StateObject(wrappedValue: engineUpdater ?? EngineUpdater())
        #if DEBUG
        let testOrigin = TestLaunch.origin()
        let testLaunchRequested = TestLaunch.autoConnectRequested()
        // Any test launch — an addressed one, or one that only asked to
        // auto-connect — wins over the saved origin. Nothing in the store may
        // turn a test launch into a connection.
        let isTestLaunch = testOrigin != nil || testLaunchRequested
        // A test launch does not even read the saved value, so it cannot be
        // influenced by (or mutate) the operator's real defaults.
        let savedOrigin = isTestLaunch ? nil : lastServer.savedOrigin
        let initialAddress = isTestLaunch
            ? TestLaunch.initialAddress(defaultAddress: Self.defaultAddress)
            : ConnectionLaunch.initialAddress(saved: savedOrigin, defaultAddress: Self.defaultAddress)
        let autoConnectTarget: Origin?
        if isTestLaunch {
            autoConnectTarget = TestLaunch.autoConnect() ? testOrigin : nil
        } else {
            autoConnectTarget = ConnectionLaunch.autoConnectOrigin(
                saved: savedOrigin,
                isTestLaunch: false,
                isDebugBuild: true
            )
        }
        let testError = testLaunchRequested && testOrigin == nil
            ? "Test mode requires CIAOBOT_MAC_TEST_ORIGIN set to a loopback origin; nothing was contacted."
            : nil
        self.isTestLaunch = isTestLaunch
        #else
        let savedOrigin = lastServer.savedOrigin
        let initialAddress = ConnectionLaunch.initialAddress(saved: savedOrigin, defaultAddress: Self.defaultAddress)
        let autoConnectTarget = ConnectionLaunch.autoConnectOrigin(
            saved: savedOrigin,
            isTestLaunch: false,
            isDebugBuild: false
        )
        let testError: String? = nil
        self.isTestLaunch = false
        #endif
        self.autoConnectTarget = engineMode == .local ? nil : autoConnectTarget
        self.supervisor = supervisor
        self.modeStore = modeStore
        self.launchAddress = initialAddress
        _engineMode = State(initialValue: engineMode)
        _address = State(initialValue: initialAddress)
        _error = State(initialValue: testError)
    }

    var body: some View {
        Group {
            if let local = localEngineScreen {
                LocalEngineView(
                    supervisor: local,
                    connection: localPhase,
                    canRetry: localModel.canRetry,
                    onTryAgain: { self.tryAgainLocal() },
                    onConnectExternal: { self.confirmAndConnectToAnotherServer() }
                )
            } else if let origin {
                // A fallback is reported where the session actually lives: the
                // connection screen is already gone once the probe succeeds.
                VStack(spacing: 0) {
                    if let storageNotice {
                        Text(storageNotice)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 6)
                    }
                    WebWindow(
                        origin: origin,
                        storage: storage,
                        websiteDataStore: dataStore,
                        onFailure: { message in
                            if self.engineMode == .local {
                                // A failed load is shown on the status screen
                                // with its own actions; it never reconnects
                                // by itself.
                                self.apply(self.localModel.sessionFailed(message))
                                return
                            }
                            self.origin = nil
                            self.storage = .ephemeral
                            self.dataStore = nil
                            self.storageNotice = nil
                            self.error = message
                            self.session?.reportDisconnected()
                        },
                        viewTracker: viewTracker,
                        onWebViewReady: { view in
                            self.session?.attachWebView(view, for: origin)
                        },
                        onWorkspaceColor: { accent, view in
                            // Setup and child windows never reach this: only
                            // the connection's own main view is wired, and the
                            // session re-checks that view and origin.
                            guard let view else { return }
                            self.session?.reportWorkspaceAccent(accent, from: view, for: origin)
                        },
                        appSettings: appSettings
                    )
                }
            } else if let wizard = installer.state.wizardOrigin {
                wizardView(wizard)
            } else {
                VStack(alignment: .leading, spacing: 16) {
                    // The supplied full wordmark, not the mark beside typed
                    // text. Its two ink variants follow the window appearance.
                    if let wordmark = colorScheme == .dark
                        ? AppAssets.wordmarkLightImage
                        : AppAssets.wordmarkImage {
                        Image(nsImage: wordmark)
                            .resizable()
                            .interpolation(.high)
                            .scaledToFit()
                            .frame(height: 28)
                            .accessibilityLabel("Ciaobot")
                            .accessibilityAddTraits(.isHeader)
                    } else {
                        // A missing asset drops only the image; the heading
                        // stays, and the screen keeps working.
                        Text("Ciaobot").font(.title.bold())
                    }
                    Text("Enter your server’s address, or connect to the Ciaobot found on this Mac.")
                        .foregroundStyle(.secondary)
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        if discovery.isChecking {
                            ProgressView().controlSize(.small)
                                .accessibilityHidden(true)
                        }
                        Text(discovery.statusText)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityLabel(LocalServerDiscoveryCopy.accessibilityLabel)
                            .accessibilityValue(discovery.statusText)
                        Spacer(minLength: 8)
                        if discovery.canStart {
                            Button("Start Ciaobot") { discovery.start() }
                                .disabled(busy)
                                .accessibilityHint("Starts the Ciaobot service installed on this Mac.")
                        }
                        if repairOffered {
                            Button(NativeInstallFlowCopy.repairButton) {
                                wizardLoadError = nil
                                installer.start(mode: .repair)
                            }
                            // A previous installer child may still be exiting.
                            .disabled(busy || !installer.canStart)
                            .accessibilityHint(NativeInstallFlowCopy.repairHint)
                        }
                        if let local = discovery.connectableOrigin {
                            Button("Connect") {
                                address = local.url.absoluteString
                                connect(to: local)
                            }
                            .disabled(busy)
                            .accessibilityHint("Connects to the Ciaobot found on this Mac.")
                        }
                        if offersThisMac {
                            Button(Self.useThisMacTitle) { useThisMac() }
                                .disabled(busy)
                                .accessibilityHint("Runs the Ciaobot engine bundled with this app on this Mac.")
                        }
                        Button(discovery.status == .idle ? "Find Ciaobot on This Mac" : "Check Again") {
                            engineUpdater.reset()
                            discovery.refresh()
                        }
                        .disabled(discovery.isChecking || busy)
                        .accessibilityHint(LocalServerDiscoveryCopy.hint)
                    }
                    if engineUpdater.state.isActive {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small).accessibilityHidden(true)
                            Text(engineUpdateProgress).font(.caption)
                                .fixedSize(horizontal: false, vertical: true)
                                .accessibilityLabel(EngineUpdateCopy.accessibilityLabel)
                                .accessibilityValue(engineUpdateProgress)
                        }
                    } else if let target = discovery.updateTarget {
                        HStack(spacing: 8) {
                            Spacer(minLength: 8)
                            Button(engineUpdateFailed ? EngineUpdateCopy.retryButton : EngineUpdateCopy.updateButton) {
                                engineUpdater.start(target)
                            }
                            .disabled(busy)
                            .accessibilityHint(EngineUpdateCopy.hint)
                        }
                    }
                    if let outcome = EngineUpdateCopy.outcome(for: engineUpdater.state) {
                        Text(outcome).font(.caption)
                            .foregroundStyle(engineUpdateFailed ? Color.red : Color.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if installer.state.isActive {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small).accessibilityHidden(true)
                            Text(NativeInstallCopy.progress(for: installer.state, mode: installer.mode) ?? "").font(.caption)
                                .accessibilityLabel("Installation status")
                            Spacer(minLength: 8)
                            Button(installer.mode == .repair ? NativeInstallFlowCopy.cancelRepairButton : NativeInstallFlowCopy.cancelButton) { installer.cancel() }
                                .disabled(installer.state == .cancelling)
                                .accessibilityHint(installer.mode == .repair
                                    ? NativeInstallFlowCopy.cancelRepairHint
                                    : "Stops the installer. A Ciaobot service that already started keeps running.")
                        }
                    } else if NativeInstallOffer.isOffered(discovery: discovery.status, install: installer.state, mode: installer.mode) {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(NativeInstallFlowCopy.offer).font(.caption).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 8)
                            // After a repair that found nothing installed, this is the first install attempt, not a retry.
                            Button(installFailure == nil || installer.mode == .repair ? NativeInstallFlowCopy.installButton : NativeInstallFlowCopy.retryButton) {
                                wizardLoadError = nil
                                installer.start()
                            }
                            // A previous installer child may still be exiting.
                            .disabled(busy || !installer.canStart)
                            .accessibilityHint("Downloads the pinned Ciaobot installer, verifies it, and installs Ciaobot on this Mac.")
                        }
                    }
                    if let installFailure {
                        Text(NativeInstallCopy.message(for: installFailure, mode: installer.mode))
                            .foregroundStyle(.red)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let installFailure, NativeInstallPortRecovery.isRecoverable(installFailure) {
                        HStack(spacing: 8) {
                            TextField("Port", text: $portText)
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 80)
                                .disabled(busy || discovery.isChecking)
                            Button(NativeInstallFlowCopy.portButton) { applyPort() }
                                .disabled(NativeInstallPortRecovery.port(portText) == nil || busy || discovery.isChecking)
                                .accessibilityHint(NativeInstallFlowCopy.portHint)
                        }
                        if let portError {
                            Text(portError).font(.caption)
                                .foregroundStyle(.red)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    if let wizardLoadError {
                        Text(wizardLoadError).font(.caption).foregroundStyle(.secondary)
                    }
                    TextField("Server address", text: $address).textFieldStyle(.roundedBorder)
                        .onSubmit(connect).disabled(busy)
                    Toggle("Keep me signed in on this Mac", isOn: Binding(
                        get: { keepSignedIn },
                        set: { keepSignedInChoice = $0 }
                    ))
                        .disabled(!persistentOptionAvailable || busy)
                    Text(persistenceExplanation)
                        .font(.caption).foregroundStyle(.secondary)
                    if let error { Text(error).foregroundStyle(.red) }
                    if let candidate = addressOrigin, signIn.hasSavedSignIn(for: candidate) {
                        Button(forgetting ? "Forgetting…" : "Forget saved sign-in") { forget() }
                            .disabled(busy)
                    }
                    if let forgetMessage {
                        Text(forgetMessage).font(.caption).foregroundStyle(.secondary)
                    }
                    HStack {
                        Button(checking ? "Checking…" : "Connect", action: connect)
                            .keyboardShortcut(.defaultAction).disabled(busy)
                        if checking { ProgressView().controlSize(.small) }
                    }
                    Text("The connection check confirms the server’s response format, not its identity. Sign in only if you trust the address.")
                        .font(.caption).foregroundStyle(.secondary)
                }.padding(32).frame(maxWidth: 520)
                    .onAppear { startAutomaticDiscovery() }
            }
        }
        // The running-origin watcher stays mounted for the whole local-mode
        // session (it observes the engine even while the web view is
        // connected, when the status screen is not); external mode mounts
        // nothing.
        .background(alignment: .center) { engineRunningWatcher }
        .frame(minWidth: 480, minHeight: 320)
        .onAppear { launchOnAppear() }
        .onReceive(NotificationCenter.default.publisher(for: .ciaobotChangeServer)) { _ in
            if engineMode == .local {
                // The app-menu command and the tray row land here too. Only
                // the confirmation stops the engine (S6), and the dialog must
                // not run inside a view update, so it steps out first.
                DispatchQueue.main.async { self.confirmAndConnectToAnotherServer() }
                return
            }
            guard origin != nil else { return }
            address = origin?.url.absoluteString ?? address
            discovery.invalidate()
            origin = nil
            storage = .ephemeral
            dataStore = nil
            storageNotice = nil
            // Consent belongs to one connection. A different server must be
            // opted in again (or use the default) rather than inheriting the
            // previous choice.
            keepSignedInChoice = nil
            error = nil
            forgetMessage = nil
            session?.reportDisconnected()
            startAutomaticDiscovery()
        }
        // Editing the address to another server is a different connection
        // too. Clear the click so the new address starts from the default
        // instead of inheriting consent given for an earlier host.
        .onChange(of: addressOrigin?.canonicalKey) { _ in
            keepSignedInChoice = nil
        }
        .onChange(of: installer.state) { handleInstallState($0) }
        .onChange(of: engineUpdater.state) { handleEngineUpdateState($0) }
        .onChange(of: discovery.status) { handleDiscoveryStatus($0) }
    }

    /// What the connection view does when it appears. Internal so a test can
    /// prove local mode runs none of the external launch machinery.
    func launchOnAppear(installState: NativeInstallState? = nil) {
        // A fresh connection screen is disconnected. Do not clear a live
        // connection when SwiftUI merely re-shows the existing window.
        if origin == nil { session?.reportDisconnected() }
        // Local mode connects from the supervisor's running origin, never
        // from the saved server: the launch machinery below is external-only.
        guard engineMode == .external else { return }
        // An install may have finished while the window was closed.
        if origin == nil, autoConnectTarget == nil { handleInstallState(installState ?? installer.state) }
        guard let target = autoConnectTarget, !didAutoConnect else { return }
        didAutoConnect = true
        address = target.url.absoluteString
        connect(to: target)
    }

    @ViewBuilder
    private func wizardView(_ wizard: Origin) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Text(NativeInstallCopy.progress(for: installer.state) ?? "")
                    .font(.caption).foregroundStyle(.secondary)
                    .accessibilityLabel("Installation status")
                Spacer(minLength: 8)
                Button(NativeInstallFlowCopy.cancelButton) { installer.cancel() }
                    .accessibilityHint("Stops the installer. A Ciaobot service that already started keeps running.")
            }
            .padding(.horizontal, 12).padding(.vertical, 6)
            // Setup only: an ephemeral store of its own, never remembered,
            // never reported as a connection, torn down when setup ends.
            WebWindow(
                origin: wizard,
                storage: .ephemeral,
                onFailure: { message in
                    self.wizardLoadError = message
                    self.installer.cancel()
                }
            )
            .id(wizard.canonicalKey)
        }
    }

    /// Automatic same-Mac discovery starts only on the connection screen,
    /// only in production, and never in local mode (the app's own engine is
    /// the only local server there, and its origin comes from the supervisor).
    /// Debug builds and test launches leave it to an explicit click so the
    /// test suite never contacts the real candidate.
    func startAutomaticDiscovery() {
        guard automaticallyStartsDiscovery, origin == nil else { return }
        discovery.refresh()
    }

    /// The supervisor to show the engine status screen for: local mode with
    /// the engine not yet connected. Once connected, the web view branch shows
    /// instead and `EngineRunningWatcher` (always mounted in local mode) keeps
    /// observing the engine. A failed engine takes the screen back from the
    /// web view, since the web view would be dead.
    private var localEngineScreen: EngineSupervisor? {
        guard engineMode == .local, origin == nil || engineDown, let supervisor else { return nil }
        return supervisor
    }

    /// Silently observant in local mode: every running origin the supervisor
    /// publishes, including one that changes while the web view is connected,
    /// goes through the same connection path.
    @ViewBuilder
    private var engineRunningWatcher: some View {
        if engineMode == .local, let supervisor {
            EngineRunningWatcher(
                supervisor: supervisor,
                onRunning: connectRunningEngine,
                onEngineDown: { self.engineDown = $0 }
            )
        }
    }

    private var addressOrigin: Origin? { Origin(address) }

    /// Persistence starts on where it can work. `canPersistSignIn` includes a
    /// usable registry, so an HTTPS address on a Mac with a broken mapping
    /// file stays off and the toggle stays disabled. An explicit click is
    /// stored separately and wins for this address only.
    private var keepSignedIn: Bool {
        PersistentSignInDefault.isEnabled(
            origin: addressOrigin,
            persistentAvailable: signIn.canPersistSignIn,
            explicitChoice: keepSignedInChoice
        )
    }

    private var persistentOptionAvailable: Bool {
        guard let candidate = addressOrigin else { return false }
        return candidate.usesTLS && signIn.canPersistSignIn
    }

    private var persistenceExplanation: String {
        PersistentSignInCopy.explanation(
            origin: addressOrigin,
            persistentAvailable: signIn.persistentAvailable,
            registryAvailable: signIn.registry.isAvailable
        )
    }

    private func forget() {
        guard let candidate = addressOrigin, tasks.beginForget() else { return }
        forgetMessage = nil
        // WebKit refuses to remove a store that a view still uses. The tracker
        // only reports release when the WKWebView weak reference is actually
        // nil; if that never happens, forget is not attempted and nothing is
        // claimed to have been released.
        viewTracker.whenReleased(timeout: 2) { released in
            guard released else {
                self.tasks.endForget()
                self.forgetMessage = "The session view has not finished closing. Try Forget again in a moment."
                return
            }
            self.signIn.forget(origin: candidate, releaseWebViews: { $0() }) { result in
                self.tasks.endForget()
                switch result {
                case .success:
                    self.storage = .ephemeral
                    self.forgetMessage = "Saved sign-in removed for \(candidate.canonicalKey)."
                case .failure(.registryUnavailable):
                    self.forgetMessage = "Could not use the saved sign-in list. Persistent sign-in is disabled."
                case .failure(.registryChangedElsewhere):
                    self.forgetMessage = "Another copy of the app changed the saved sign-in list. Restart Ciaobot to reload it before forgetting."
                case .failure(.storeStillPresent):
                    self.forgetMessage = "Could not remove the saved sign-in. Nothing was forgotten; try again."
                case .failure(.mappingNotSaved):
                    self.forgetMessage = "WebKit reported the saved sign-in store removed, but its record could not be updated. This session stays in memory; try again or restart Ciaobot to finish."
                }
            }
        }
    }

    /// Only `service_ready` is success. The service origin goes through the
    /// normal probe, so it is remembered and reported only if it answers.
    func handleInstallState(_ state: NativeInstallState) {
        if case .succeeded(let service) = state, origin == nil {
            address = service.url.absoluteString
            connect(to: service, afterInstall: true)
            return
        }
        // A port failure has a recovery that is not another identical retry:
        // offer a port field, prefilled with a port that is free right now.
        if case .failed(let failure) = state, NativeInstallPortRecovery.isRecoverable(failure) {
            portError = nil
            portText = FreePort.pick().map(String.init) ?? ""
        }
    }

    /// Writes the confirmed port to the workspace `.env`, clears the failed
    /// run, then re-detects and starts the existing service.
    private func applyPort() {
        guard let port = NativeInstallPortRecovery.port(portText) else { return }
        guard let workspace = LocalInstallation.detect()?.workspace else {
            portError = NativeInstallFlowCopy.portWorkspaceMissing
            return
        }
        do {
            try WorkspaceDotenvPort.setPort(port, in: workspace.appendingPathComponent(".env"))
        } catch {
            portError = NativeInstallFlowCopy.portSaveFailed
            return
        }
        portError = nil
        installer.reset()
        pendingPortRestart = true
        discovery.refresh()
    }

    /// A port change restarts the service once discovery has re-read the new
    /// port. Separately, a failed install/repair message is stale as soon as
    /// discovery finds the engine running.
    func handleDiscoveryStatus(_ status: LocalServerDiscovery.Status) {
        if pendingPortRestart {
            switch status {
            case .installedNotRunning:
                pendingPortRestart = false
                discovery.start()
            case .running:
                pendingPortRestart = false
            case .idle, .checking, .starting, .startFailed, .notDetected:
                break
            }
        }
        if NativeInstallFailureReset.shouldReset(install: installer.state, discovery: status) {
            installer.reset()
        }
    }

    /// After a successful update the version line is re-read from the restarted engine.
    func handleEngineUpdateState(_ state: EngineUpdateState) {
        guard state.requiresRediscovery, origin == nil else { return }
        discovery.refresh()
    }

    /// Internal so the connection outcome — and therefore the remembered
    /// origin — can be tested without rendering the view.
    func connect() {
        guard !tasks.isBusy else { return }
        guard let candidate = Origin(address) else {
            error = "Enter only an origin, such as https://example.com:8443. Do not include a path, query, fragment, or credentials. HTTP is allowed only for localhost, 127.0.0.1, or ::1; other hosts require HTTPS."
            return
        }
        connect(to: candidate)
    }

    private func connect(to candidate: Origin, afterInstall: Bool = false) {
        guard tasks.beginCheck() else { return }
        error = nil
        wizardLoadError = nil
        storageNotice = nil
        probe.check(candidate) { ok in
            self.tasks.endCheck()
            if ok {
                // Only a connection that actually answered is remembered. A
                // typo, an unreachable host, or a hostile address never
                // becomes the startup target or a saved host.
                self.lastServer.remember(candidate)
                if !self.isTestLaunch {
                    self.hostStore.add(candidate)
                }
                self.establishSession(candidate)
            } else {
                self.error = afterInstall
                    ? (self.installer.mode == .repair
                        ? NativeInstallFlowCopy.connectFailedAfterRepair(candidate)
                        : NativeInstallFlowCopy.connectFailedAfterInstall(candidate))
                    : "Could not verify the server’s startup response. Check the address and try again."
                self.session?.reportDisconnected()
            }
        }
    }

    /// The probe answered: build the one session for `candidate`.
    private func establishSession(_ candidate: Origin) {
        // The connection screen is about to disappear. A discovery
        // response that is still in flight must not update the screen
        // that replaces it.
        discovery.invalidate()
        // Resolve the store only after the probe succeeds, so a typo
        // cannot create a persistent mapping or empty store. If the
        // user opted in but cannot get a persistent store, say so
        // instead of silently using memory only.
        storage = signIn.storage(for: candidate, keepSignedIn: keepSignedIn)
        storageNotice = signIn.fallback(for: candidate, keepSignedIn: keepSignedIn)?.userMessage
        // Create the one store this connection uses. The web view and
        // the menu-bar chat feed share it; no second nonpersistent
        // store and never the default shared store.
        let store = WebKitSessionStores.make(storage)
        dataStore = store
        origin = candidate
        // The probe verified the protocol shape, not an identity or a
        // signed-in session. The menu bar says only this much.
        session?.reportConnected(candidate, dataStore: store)
    }

    /// Tears down the live session the way Change Server does.
    static let useThisMacTitle = "Use Ciaobot on This Mac"

    /// The connection screen offers the bundled engine only in external mode,
    /// on an arm64 Mac, when the app actually carries a usable engine.
    var offersThisMac: Bool {
        engineMode == .external && hostIsArm64 && supervisor?.hasEngine == true
    }

    /// **Use Ciaobot on This Mac** (external → local): saves the local choice,
    /// tells the app delegate, then starts the engine; the running-origin
    /// watcher connects when it is up. (The 1.x takeover check, S7, runs
    /// before the start once it exists.) Internal for tests.
    func useThisMac() {
        guard offersThisMac, let supervisor else { return }
        discovery.invalidate()
        dropSession()
        modeStore?.mode = .local
        engineMode = .local
        localModel.reset()
        localPhase = .idle
        engineDown = false
        onModeChange?(.local)
        supervisor.start()
    }

    private func dropSession() {
        origin = nil
        storage = .ephemeral
        dataStore = nil
        storageNotice = nil
        keepSignedInChoice = nil
        error = nil
        forgetMessage = nil
        session?.reportDisconnected()
    }

    /// The local engine published a running origin: connect to it
    /// automatically, without the saved-origin prefill or the LastServer
    /// prompt, and without saving the engine's ephemeral loopback origin as a
    /// host or the last server. The decisions (connect once per origin,
    /// replace the session on a changed origin, no retry after a failure) are
    /// `LocalConnectionModel`'s; this only executes them.
    func connectRunningEngine(_ candidate: Origin) {
        apply(localModel.engineRunning(candidate))
    }

    /// The decision model's current phase. Internal for tests.
    var localConnectionPhase: LocalConnectionModel.Phase { localModel.phase }

    private func apply(_ effects: [LocalConnectionModel.Effect]) {
        for effect in effects {
            switch effect {
            case .dropSession:
                dropSession()
            case .probe(let target):
                probe.check(target) { ok in
                    self.apply(self.localModel.probeFinished(target, ok: ok))
                }
            case .establish(let target):
                establishSession(target)
            }
        }
        localPhase = localModel.phase
    }

    /// Try Again on the local status screen. Internal for tests.
    func tryAgainLocal() {
        guard let supervisor else { return }
        switch LocalEnginePresentation.retry(engine: supervisor.state) {
        case .restartEngine: supervisor.restartAfterFailure()
        case .startEngine: supervisor.start()
        case .reconnect(let origin): apply(localModel.retry(engineOrigin: origin))
        case .none: break
        }
    }

    /// Every connect-to-another-server surface in local mode (the status
    /// screen's button, the app-menu command ⇧⌘L, and the tray row) confirms
    /// once before the engine stops (S6). Cancel leaves the engine and the
    /// running session exactly as they were. External mode never shows this
    /// dialog, so its surfaces keep their shipped behaviour.
    func confirmAndConnectToAnotherServer() {
        guard engineMode == .local else { return }
        guard presentModeSwitchDialog(EngineLifecycle.modeSwitchDialog) else { return }
        connectToAnotherServer()
    }

    /// **Connect to Another Server…** from the engine status screen (after
    /// confirmation): stop the engine, save the external choice, and show the
    /// ordinary connection screen. Internal for tests.
    func connectToAnotherServer() {
        if let supervisor { supervisor.stop {} }
        modeStore?.mode = .external
        engineMode = .external
        onModeChange?(.external)
        localModel.reset()
        localPhase = .idle
        engineDown = false
        dropSession()
    }
}
