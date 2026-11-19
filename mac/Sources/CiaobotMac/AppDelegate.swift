import AppKit

@MainActor
protocol MainWindowPresenting: AnyObject {
    @discardableResult func present() -> Bool
}

@MainActor
enum AppActivation {
    static func activate() {
        if #available(macOS 14.0, *) {
            NSApp.activate()
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }
    }
}

/// Reopens the single SwiftUI `Window` scene after the user closed it. The
/// scene keeps its `NSWindow` (identifier `main`) for the lifetime of the app
/// once it has been created, so reordering that window restores the existing
/// connection state instead of silently building a second one.
@MainActor
final class MainWindowPresenter: MainWindowPresenting {
    static let mainWindowIdentifier = NSUserInterfaceItemIdentifier("main")

    private let windows: () -> [NSWindow]
    private let activate: () -> Void
    private let prepareActivation: () -> Void
    private let restoreActivationOnFailure: () -> Void
    private let reportFailure: () -> Void

    init(
        windows: (@MainActor () -> [NSWindow])? = nil,
        activate: (@MainActor () -> Void)? = nil,
        prepareActivation: (@MainActor () -> Void)? = nil,
        restoreActivationOnFailure: (@MainActor () -> Void)? = nil,
        reportFailure: (@MainActor () -> Void)? = nil
    ) {
        self.windows = windows ?? { NSApp.windows }
        self.activate = activate ?? AppActivation.activate
        self.prepareActivation = prepareActivation ?? {}
        self.restoreActivationOnFailure = restoreActivationOnFailure ?? {}
        self.reportFailure = reportFailure ?? {}
    }

    @discardableResult
    func present() -> Bool {
        guard let window = Self.mainWindow(in: windows()) else {
            // Do not put a hidden-Dock app into `.regular` for a window that
            // does not exist. Restore the saved policy and tell the user
            // instead of returning a silent no-op.
            activate()
            restoreActivationOnFailure()
            reportFailure()
            return false
        }
        // A persisted "Hide Dock Icon" choice leaves the app in the accessory
        // policy. Opening the window restores a regular app first so the
        // window can become key; the menu-bar item is unaffected.
        prepareActivation()
        activate()
        // `makeKeyAndOrderFront` does not reliably restore a miniaturized
        // window; deminiaturize first. It is a no-op for a normal window.
        window.deminiaturize(nil)
        window.makeKeyAndOrderFront(nil)
        return true
    }

    /// A no-window failure is user-visible. The client does not create a
    /// replacement `NSWindow` behind SwiftUI's back: the existing scene owns
    /// the window's lifetime and its connection state.
    static func presentFailureAlert() {
        let alert = NSAlert()
        alert.messageText = "Ciaobot window is unavailable"
        alert.informativeText = "Ciaobot could not reopen its main window. Quit Ciaobot and open it again; the menu-bar item stays available."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    /// The SwiftUI scene identifier is stable and distinguishes the main
    /// window from WebKit child windows that may also be titled "Ciaobot".
    static func mainWindow(in windows: [NSWindow]) -> NSWindow? {
        windows.first { $0.identifier == mainWindowIdentifier }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let session: AppSessionStatus
    /// The in-window Settings bridge. Created here so the connected main window
    /// can be handed it before the menu-bar controller exists.
    let appSettings = AppSettingsBridge()
    /// The stored engine-mode choice (`CiaobotEngineMode`).
    let engineMode: EngineModeStoring
    /// The engine mode this app runs in: `local` owns the bundled engine
    /// (started at launch, below), `external` is the unchanged connection
    /// screen. Resolved at construction, then kept current by the connection
    /// view's in-session switches (`modeDidChange`), so the quit path and the
    /// tray never act on a stale mode.
    private(set) var mode: EngineMode
    /// The app-owned engine and everything needed to run it. Never started in
    /// external mode. The test initializer leaves it out unless injected.
    let supervisor: EngineSupervisor?
    /// The 1.x takeover for this launch (#3129 S7): run before the local engine
    /// starts, so an old install is moved into the app first. Nil when there is
    /// no bundled engine, or in the test initializer unless injected.
    let takeover: EngineTakeover?
    /// The launch's last-server and saved-host stores, handed to the
    /// connection view so one defaults domain (or one DEBUG test suite) holds
    /// all three stored decisions.
    let lastServer: LastServerStoring
    let hostStore: HostListStoring

    /// Reads the launch Apple event (`applicationWillFinishLaunching` reads
    /// the login launch there, the only moment it is current).
    private let launchEvent: () -> NSAppleEventDescriptor?
    /// The main window, for the login-launch close. The production default
    /// resolves the same identifier the presenter reopens.
    private let mainWindow: @MainActor () -> NSWindow?
    /// Presents the quit confirmation. Tests record the call and decide.
    private let presentQuitDialog: @MainActor (EngineQuitDialog) -> Bool
    /// The running-chat count for the quit confirmation.
    private let blockers: RestartBlockersFetching
    /// Responds to a deferred `applicationShouldTerminate`. Tests record it.
    private let replyToTerminate: @MainActor (Bool) -> Void
    /// Raised by Sparkle's immediate install: that terminate is an update quit
    /// (`QuitReason.update`), with no dialog and the full quit grace.
    private let updateQuit: UpdateQuitIntent
    /// Schedules the login-launch window close retries. Tests drive it.
    private let loginCloseSchedule: LoginLaunchWindowCloser.Schedule?
    private var loginCloser: LoginLaunchWindowCloser?
    /// True only while `applicationShouldTerminate` is on the stack. AppKit
    /// ignores a reply sent before that call returns `.terminateLater` and
    /// then waits forever, so a reply requested in that window is sent from a
    /// later main-queue turn instead.
    private var insideShouldTerminate = false
    /// True when this launch came from the login item: no main window is
    /// opened, while the engine starts as usual (S6).
    private var launchedAtLogin = false
    /// One terminate request at a time: while the quit dialog or the
    /// stop-and-exit flow is pending, further quit events wait for its reply.
    private var terminationDeferred = false

    typealias MenuBarFactory = @MainActor (AppSessionStatus, ClientUpdateControlling?,
                                           @escaping () -> Void, @escaping () -> Void) -> MenuBarControlling

    private let presenter: MainWindowPresenting
    private let updater: ClientUpdateControlling?
    private let makeMenuBar: MenuBarFactory?
    private let loginItem: LoginItemControlling?
    private let dockIcon: DockIconControlling?
    /// Owns the running app icon: the launch fallback and the live workspace
    /// accent. The Finder/Launchpad icon is still the bundled `AppIcon.icns`.
    private let workspaceIcon: WorkspaceDockIconControlling?
    private let dockBadge: DockBadgeApplying?
    private let systemSettings: SystemSettingsOpening?
    private var menuBar: MenuBarControlling?
    private var windowCloseObserver: NSObjectProtocol?
    #if DEBUG
    private var testWindowReporter: TestWindowReporter?
    #endif
    /// Installed during `applicationWillFinishLaunching` so a notification
    /// click that launched the app can still reach the delegate. It never
    /// prompts; permission is requested only from the menu action.
    private var notificationPermission: SystemNotificationPermissionClient?

    override convenience init() {
        let dockIcon = DockIconController()
        let workspaceIcon = WorkspaceDockIconController()
        let presenter = MainWindowPresenter(
            prepareActivation: { dockIcon.prepareForWindowPresentation() },
            restoreActivationOnFailure: { dockIcon.applyPersistedPreference() },
            reportFailure: MainWindowPresenter.presentFailureAlert
        )
        let session = AppSessionStatus(workspaceIcon: workspaceIcon)
        let updateQuit = UpdateQuitIntent()
        // One launch defaults domain for the stored engine mode, last server
        // and saved hosts. A DEBUG test launch with an isolated suite keeps
        // all three away from the app's own defaults; every other launch uses
        // the standard domain, as before.
        let launchDefaults: UserDefaults
        #if DEBUG
        launchDefaults = TestLaunch.storedDefaults()
        #else
        launchDefaults = .standard
        #endif
        let modeStore = UserDefaultsEngineModeStore(defaults: launchDefaults)
        let lastServerStore = UserDefaultsLastServerStore(defaults: launchDefaults)
        let addressedTestLaunch: Bool
        #if DEBUG
        addressedTestLaunch = TestLaunch.addressed()
        #else
        addressedTestLaunch = false
        #endif
        let engineParts = Self.makeEngineParts()
        self.init(
            session: session,
            presenter: presenter,
            // The updater lives for the app's lifetime. Creating it is inert in
            // a debug build, a test process, or a build without the release
            // updater keys; `start()` decides and records why.
            updater: ClientUpdater(updateQuit: updateQuit, localState: { engineParts.supervisor.updateLocalState }),
            // The signing probe is the real one here. The row explains an
            // ad-hoc or unsigned build instead of offering a registration macOS
            // would refuse.
            loginItem: SystemLoginItem(signingTeam: CodeSigningTeam()),
            dockIcon: dockIcon,
            workspaceIcon: workspaceIcon,
            dockBadge: DockTileBadge(),
            systemSettings: WorkspaceSystemSettings(
                presentHelper: { SettingsHelperPanel.shared.present(for: $0) }
            ),
            engineMode: modeStore,
            mode: Self.launchEngineMode(
                saved: modeStore.mode?.rawValue,
                lastOrigin: lastServerStore.savedOrigin,
                addressedTestLaunch: addressedTestLaunch
            ),
            supervisor: engineParts.supervisor,
            takeover: engineParts.takeover,
            lastServer: lastServerStore,
            hostStore: UserDefaultsHostListStore(defaults: launchDefaults, lastServer: lastServerStore),
            updateQuit: updateQuit
        )
    }

    init(
        session: AppSessionStatus,
        presenter: MainWindowPresenting,
        updater: ClientUpdateControlling? = nil,
        // Without an injected updater the test initializer creates an inert one
        // and no monitor, so no test launch probes the engine.
        makeMenuBar: MenuBarFactory? = nil,
        loginItem: LoginItemControlling? = nil,
        dockIcon: DockIconControlling? = nil,
        // The test initializer leaves the running icon out, so no test can
        // write the real `NSApp.applicationIconImage` or create a Dock image.
        workspaceIcon: WorkspaceDockIconControlling? = nil,
        // The test initializer leaves the Dock badge out, so no test can write
        // the real `NSApp.dockTile`.
        dockBadge: DockBadgeApplying? = nil,
        systemSettings: SystemSettingsOpening? = nil,
        engineMode: EngineModeStoring? = nil,
        // The resolved engine mode, or the injected store's choice, else
        // external: an ordinary test launch runs no engine.
        mode: EngineMode? = nil,
        supervisor: EngineSupervisor? = nil,
        takeover: EngineTakeover? = nil,
        lastServer: LastServerStoring? = nil,
        hostStore: HostListStoring? = nil,
        // The lifecycle surfaces (S6). Defaults are the real behaviour; the
        // test initializer injects doubles, so no test shows a dialog, reads
        // the loopback route, closes a real window, or replies to AppKit.
        // `launchEvent`'s default reads the current Apple event, which is only
        // non-nil while one is being handled — never in a test process.
        launchEvent: @escaping () -> NSAppleEventDescriptor? = { NSAppleEventManager.shared().currentAppleEvent },
        mainWindow: (@MainActor () -> NSWindow?)? = nil,
        quitDialog: (@MainActor (EngineQuitDialog) -> Bool)? = nil,
        restartBlockers: RestartBlockersFetching? = nil,
        replyToTerminate: (@MainActor (Bool) -> Void)? = nil,
        updateQuit: UpdateQuitIntent? = nil,
        loginCloseSchedule: LoginLaunchWindowCloser.Schedule? = nil
    ) {
        self.session = session
        self.presenter = presenter
        self.updater = updater
        self.makeMenuBar = makeMenuBar
        self.loginItem = loginItem
        self.dockIcon = dockIcon
        self.workspaceIcon = workspaceIcon
        self.dockBadge = dockBadge
        self.systemSettings = systemSettings
        self.engineMode = engineMode ?? TransientEngineModeStore()
        self.mode = mode ?? self.engineMode.mode ?? .external
        self.supervisor = supervisor
        self.takeover = takeover
        let lastServer = lastServer ?? UserDefaultsLastServerStore()
        self.lastServer = lastServer
        self.hostStore = hostStore ?? UserDefaultsHostListStore(lastServer: lastServer)
        self.launchEvent = launchEvent
        // The real window resolution is the presenter's own rule: the scene
        // keeper with the stable `main` identifier.
        self.mainWindow = mainWindow ?? { MainWindowPresenter.mainWindow(in: NSApp.windows) }
        self.presentQuitDialog = quitDialog ?? { dialog in
            EngineLifecycle.presentConfirmation(
                title: dialog.title, body: dialog.body,
                confirm: EngineLifecycle.quitConfirmButton, cancel: EngineLifecycle.quitCancelButton
            )
        }
        self.blockers = restartBlockers ?? RestartBlockersProbe()
        self.replyToTerminate = replyToTerminate ?? { NSApp.reply(toApplicationShouldTerminate: $0) }
        self.updateQuit = updateQuit ?? UpdateQuitIntent()
        self.loginCloseSchedule = loginCloseSchedule
        super.init()
    }

    /// The engine mode for this launch: a stored choice wins over the default
    /// `EngineMode.initial` rule (loopback or no last connection is local), and
    /// a launch addressed at a DEBUG test fixture stays external — a fixture
    /// launch must never run the bundled engine.
    static func launchEngineMode(
        saved: String?,
        lastOrigin: Origin?,
        addressedTestLaunch: Bool
    ) -> EngineMode {
        if addressedTestLaunch { return .external }
        return EngineMode.initial(saved: saved, lastOrigin: lastOrigin, hostIsArm64: BundledEngine.hostIsArm64)
    }

    /// The bundled engine's supervisor and the 1.x takeover that runs before
    /// it starts, from one locate. The takeover is nil without a usable engine
    /// (the supervisor then reports the fixed unavailable copy). A DEBUG test
    /// launch can point the home and the engine resources somewhere isolated.
    private static func makeEngineParts(
        mainWindow: @escaping @MainActor () -> NSWindow? = { MainWindowPresenter.mainWindow(in: NSApp.windows) }
    ) -> (supervisor: EngineSupervisor, takeover: EngineTakeover?) {
        let resourceDirectory: URL?
        let home: URL
        let enginePort: Int?
        #if DEBUG
        enginePort = TestLaunch.enginePort()
        resourceDirectory = TestLaunch.engineResources() ?? Bundle.main.resourceURL
        home = TestLaunch.engineHome() ?? FileManager.default.homeDirectoryForCurrentUser
        #else
        enginePort = nil
        resourceDirectory = Bundle.main.resourceURL
        home = FileManager.default.homeDirectoryForCurrentUser
        #endif
        let engine = try? BundledEngine.locate(resourceDirectory: resourceDirectory)
        let engineHome = EngineHome(home: home)
        let supervisor = EngineSupervisor(
            engine: engine,
            home: engineHome,
            processes: FoundationEngineProcesses(),
            identity: EngineIdentityProbe(),
            enginePort: enginePort
        )
        guard let engine else { return (supervisor, nil) }
        // The takeover command runs the same launcher as the engine, with HOME
        // pinned to the engine's home so a test launch stays inside its folder.
        let commandLine: ([String]) -> TakeoverCommandLine = { arguments in
            var parent = ProcessInfo.processInfo.environment
            parent["HOME"] = home.path
            let launch = engine.launch(.takeover(arguments), parentEnvironment: parent, workingDirectory: home)
            return TakeoverCommandLine(
                executable: launch.executable,
                arguments: launch.arguments,
                environment: launch.environment,
                workingDirectory: launch.workingDirectory
            )
        }
        #if DEBUG
        let labelOverride = EngineTakeover.testLabel(environment: ProcessInfo.processInfo.environment)
        #else
        let labelOverride: String? = nil
        #endif
        let takeover = EngineTakeover(
            runner: FoundationTakeoverRunner(commandLine: commandLine),
            supervisor: supervisor,
            trash: FileManagerTrashing(),
            home: engineHome,
            labelOverride: labelOverride,
            windowVisible: takeoverWindowVisible(mainWindow)
        )
        return (supervisor, takeover)
    }

    /// The takeover's one question is asked only in a window the user can see:
    /// the main window, if it is on screen. Injected so the wiring is testable.
    static func takeoverWindowVisible(_ mainWindow: @escaping @MainActor () -> NSWindow?) -> @MainActor () -> Bool {
        { mainWindow()?.isVisible ?? false }
    }

    func applicationWillFinishLaunching(_ notification: Notification) {
        // Setting the delegate before launch finishes is what lets a click
        // that started the app be delivered. A bare debug executable reports
        // itself unavailable and this is a no-op.
        let permission = SystemNotificationPermissionClient()
        permission.prepare()
        notificationPermission = permission
        // The launch Apple event is the current event only now, so the login
        // launch fact is read here (S6): a login launch opens no window.
        launchedAtLogin = EngineLifecycle.isLoginLaunch(launchEvent())
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // The controller owns the running icon's launch fallback: the bundled
        // rounded artwork (`app-icon.png`), which also covers the bare debug
        // executable produced by `swift build`. Finder and Launchpad keep
        // reading the unchanged `AppIcon.icns` through `CFBundleIconFile`.
        workspaceIcon?.reset()

        // One retained updater for the app's lifetime. It starts here, before
        // the menu is built, so the Settings row can report its real
        // availability. An inert build starts nothing and never contacts the
        // feed.
        updater?.start()

        #if DEBUG
        // A test launch with a loopback origin and a report secret reports the
        // main window's state to its fixture; see `TestWindowReporter`.
        // Ordinary launches start nothing.
        if let origin = TestLaunch.origin(), let secret = TestLaunch.reportSecret() {
            let reporter = TestWindowReporter(origin: origin, secret: secret)
            reporter.start()
            testWindowReporter = reporter
        }
        #endif

        // Local mode owns the engine's lifetime, so the app starts it here and
        // not from a view: a login launch with no window still runs the
        // engine. External mode runs nothing (S6 adds the closed-window and
        // quit handling).
        if mode == .local {
            // The 1.x takeover (S7) runs first and starts the engine itself
            // when it settles on "no takeover" or "done".
            if let takeover {
                takeover.startLocal()
            } else {
                supervisor?.start()
            }
        }

        // A login launch opens no main window: the engine starts as usual and
        // the tray is the way back in. SwiftUI creates the scene window during
        // launch at no fixed moment, so the closer retries until it exists
        // (bounded) — and the window-close observer below then applies the
        // saved Dock preference.
        // The scene keeps its window for the app's lifetime, so the tray
        // reopens the same one.
        if launchedAtLogin {
            let closer = LoginLaunchWindowCloser(window: mainWindow, schedule: loginCloseSchedule)
            loginCloser = closer
            closer.start()
        }

        // A saved "Hide Dock Icon" choice applies after the initial SwiftUI
        // window has had a chance to appear. It hides the Dock only while the
        // main window is closed; it never turns the login item on and never
        // changes the stored choice.
        if let dockIcon {
            DispatchQueue.main.async {
                dockIcon.applyPersistedPreference()
            }
        }
        if dockIcon != nil {
            let mainWindowIdentifier = MainWindowPresenter.mainWindowIdentifier
            windowCloseObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.willCloseNotification,
                object: nil,
                queue: .main
            ) { [weak self] note in
                // The block is a Sendable, nonisolated closure: read the
                // notification here, then resolve the main window and update
                // the Dock policy on the main actor, where the window's
                // identifier is legitimately readable. `self` and the window
                // are captured as immutable values for the hop, not as the
                // outer closure's weak variable.
                let closing = note.object as? NSWindow
                guard let self else { return }
                Task { @MainActor in
                    guard let closing, closing.identifier == mainWindowIdentifier else { return }
                    self.dockIcon?.windowDidClose()
                }
            }
        }

        let session = self.session
        let open: () -> Void = { [weak self] in
            _ = self?.presenter.present()
        }
        let present: () -> Bool = { [weak self] in
            self?.presenter.present() ?? false
        }
        let quit = { NSApp.terminate(nil) }
        let controller: MenuBarControlling
        if let makeMenuBar {
            controller = makeMenuBar(session, updater, open, quit)
        } else {
            // The menu bar keeps this controller alive for the app's lifetime
            // and stops it with the item. The socket itself only exists after
            // the user opts in from the menu.
            let permission = notificationPermission ?? SystemNotificationPermissionClient()
            permission.prepare()
            let notifications = ChatNotificationController(
                status: session,
                permission: permission,
                presentWindow: open,
                openChat: { session.openChat(chatID: $0) }
            )
            notifications.start()
            appSettings.bind(notifications: notifications, systemSettings: systemSettings)
            // One feed pair per saved host. The active host's entry uses the
            // session's own store; every other host uses its snapshot-verified
            // saved sign-in, and a remote host without one is never contacted.
            let fleet = HostFleetModel(
                hosts: UserDefaultsHostListStore(lastServer: UserDefaultsLastServerStore()),
                status: session,
                signIn: .live
            )
            controller = MenuBarController(
                status: session,
                openWindow: open,
                quit: quit,
                fleet: fleet,
                dockBadge: dockBadge,
                openChat: { session.openChat(chatID: $0) },
                notifications: notifications,
                loginItem: loginItem,
                dockIcon: dockIcon,
                systemSettings: systemSettings,
                changeHost: ConnectionMenuActions.changeConnectedHost(present: present),
                updates: updater,
                engineSupervisor: supervisor,
                localEngineMode: mode == .local
            )
        }
        controller.start()
        menuBar = controller
    }

    func applicationWillTerminate(_ notification: Notification) {
        #if DEBUG
        testWindowReporter?.stop()
        testWindowReporter = nil
        #endif
        if let windowCloseObserver {
            NotificationCenter.default.removeObserver(windowCloseObserver)
            self.windowCloseObserver = nil
        }
        menuBar?.stop()
        menuBar = nil
    }

    /// Closing the only window must not quit the client: the menu-bar item is
    /// the way back in, matching the app's client-only lifecycle.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            presenter.present()
        }
        return true
    }

    // MARK: Quit (S6)

    /// One quit decision for every terminate request (#3129 S6). The tray and
    /// Cmd-Q converge here like the plan says, so they get exactly one
    /// confirmation; a session-ending quit never asks and still stops the
    /// engine; external mode leaves at once because it stopped nothing.
    ///
    /// While a deferred flow is pending (dialog open, or the engine draining),
    /// further terminate requests return `.terminateLater` and wait for the
    /// one reply the pending flow makes.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if terminationDeferred { return .terminateLater }
        // A reply made before this method returns is ignored by AppKit, so
        // every reply requested while it is on the stack waits one turn.
        insideShouldTerminate = true
        defer { insideShouldTerminate = false }
        // Sparkle's install terminates the app to swap the bundle, after the
        // update gate has drained the engine: it is an update quit, so no
        // dialog stands in front of it.
        let reason = EngineLifecycle.quitReason(launchEvent(), updateInstall: updateQuit.installing)
        switch EngineLifecycle.terminationPlan(mode: mode, engineState: supervisor?.state, quitReason: reason) {
        case .terminateNow:
            return .terminateNow
        case .stopThenTerminate(let grace):
            guard let supervisor else { return .terminateNow }
            terminationDeferred = true
            stopEngineThenTerminate(supervisor, grace: grace)
            return .terminateLater
        case .confirm:
            guard let supervisor else { return .terminateNow }
            terminationDeferred = true
            beginConfirmedQuit(supervisor)
            return .terminateLater
        }
    }

    /// Stops the engine and ends the app only after the stop completes, so the
    /// engine drains before exit. The tray shows the fixed stopping line for
    /// the whole stop.
    private func stopEngineThenTerminate(_ supervisor: EngineSupervisor, grace: TimeInterval) {
        menuBar?.setQuittingTitle()
        supervisor.stop(grace: grace) { [weak self] in
            self?.finishDeferredTermination(terminate: true)
        }
    }

    /// A user quit of a starting or running local engine: read the blocker
    /// count from the engine's own loopback route (no session, one second,
    /// nil on anything else), show the one confirmation, and act on it. Cancel
    /// replies false, which puts the app back where it was.
    private func beginConfirmedQuit(_ supervisor: EngineSupervisor) {
        let decide: (Int?) -> Void = { [weak self] count in
            // The probe resolves on the main queue, like every connection fetch.
            MainActor.assumeIsolated {
                guard let self else { return }
                let message = EngineLifecycle.quitMessage(blockers: count)
                guard self.presentQuitDialog(EngineQuitDialog(title: message.title, body: message.body)) else {
                    self.finishDeferredTermination(terminate: false)
                    return
                }
                self.stopEngineThenTerminate(supervisor, grace: EngineLifecycle.quitGrace)
            }
        }
        // A starting engine has no origin yet, so it shows the dialog with no
        // count rather than fetching one.
        if case .running(let origin, _, _) = supervisor.state {
            blockers.activeChats(of: origin, completion: decide)
        } else {
            decide(nil)
        }
    }

    /// `terminationDeferred` stays set until the reply is actually sent, so a
    /// quit that arrives during the hop still joins this flow.
    private func finishDeferredTermination(terminate: Bool) {
        let send: @MainActor () -> Void = { [self] in
            terminationDeferred = false
            replyToTerminate(terminate)
        }
        if insideShouldTerminate {
            DispatchQueue.main.async { MainActor.assumeIsolated { send() } }
        } else {
            send()
        }
    }

    // MARK: Mode switch (S6)

    /// The connection view switched the engine mode in this session (Connect
    /// to Another Server, Use Ciaobot on This Mac). The quit path and the tray
    /// read the mode from here, so they follow it immediately.
    func modeDidChange(_ newMode: EngineMode) {
        mode = newMode
        // Leaving local mode while a takeover runs cancels it: nothing new starts,
        // and an applied move is rolled back (S7).
        if newMode == .external { takeover?.cancel() }
        menuBar?.setLocalEngineMode(newMode == .local)
    }
}
