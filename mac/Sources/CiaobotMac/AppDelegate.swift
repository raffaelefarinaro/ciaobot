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
    // Owned here so quitting can cancel a running install.
    let installer = NativeInstaller()
    // One engine updater shared by the connection screen and the menu bar.
    let engineUpdater: EngineUpdater

    typealias MenuBarFactory = @MainActor (AppSessionStatus, ClientUpdateControlling?, EngineUpdateMenuProviding?,
                                           @escaping () -> Void, @escaping () -> Void) -> MenuBarControlling

    private let presenter: MainWindowPresenting
    private let updater: ClientUpdateControlling?
    private let engineUpdates: EngineUpdateMenuProviding?
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
        let engineUpdater = EngineUpdater()
        self.init(
            session: session,
            presenter: presenter,
            // The updater lives for the app's lifetime. Creating it is inert in
            // a debug build, a test process, or a build without the release
            // updater keys; `start()` decides and records why.
            updater: ClientUpdater(),
            engineUpdater: engineUpdater,
            engineUpdates: EngineUpdateMonitor(status: session, updater: engineUpdater),
            // The signing probe is the real one here. The row explains an
            // ad-hoc or unsigned build instead of offering a registration macOS
            // would refuse.
            loginItem: SystemLoginItem(signingTeam: CodeSigningTeam()),
            dockIcon: dockIcon,
            workspaceIcon: workspaceIcon,
            dockBadge: DockTileBadge(),
            systemSettings: WorkspaceSystemSettings(
                presentHelper: { SettingsHelperPanel.shared.present(for: $0) }
            )
        )
    }

    init(
        session: AppSessionStatus,
        presenter: MainWindowPresenting,
        updater: ClientUpdateControlling? = nil,
        // Without an injected updater the test initializer creates an inert one
        // and no monitor, so no test launch probes the engine.
        engineUpdater: EngineUpdater? = nil,
        engineUpdates: EngineUpdateMenuProviding? = nil,
        makeMenuBar: MenuBarFactory? = nil,
        loginItem: LoginItemControlling? = nil,
        dockIcon: DockIconControlling? = nil,
        // The test initializer leaves the running icon out, so no test can
        // write the real `NSApp.applicationIconImage` or create a Dock image.
        workspaceIcon: WorkspaceDockIconControlling? = nil,
        // The test initializer leaves the Dock badge out, so no test can write
        // the real `NSApp.dockTile`.
        dockBadge: DockBadgeApplying? = nil,
        systemSettings: SystemSettingsOpening? = nil
    ) {
        self.session = session
        self.presenter = presenter
        self.updater = updater
        self.engineUpdater = engineUpdater ?? EngineUpdater()
        self.engineUpdates = engineUpdates
        self.makeMenuBar = makeMenuBar
        self.loginItem = loginItem
        self.dockIcon = dockIcon
        self.workspaceIcon = workspaceIcon
        self.dockBadge = dockBadge
        self.systemSettings = systemSettings
        super.init()
    }

    func applicationWillFinishLaunching(_ notification: Notification) {
        // Setting the delegate before launch finishes is what lets a click
        // that started the app be delivered. A bare debug executable reports
        // itself unavailable and this is a no-op.
        let permission = SystemNotificationPermissionClient()
        permission.prepare()
        notificationPermission = permission
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
        let engineUpdates = self.engineUpdates
        let open: () -> Void = { [weak self] in
            _ = self?.presenter.present()
        }
        let present: () -> Bool = { [weak self] in
            self?.presenter.present() ?? false
        }
        let quit = { NSApp.terminate(nil) }
        let controller: MenuBarControlling
        if let makeMenuBar {
            controller = makeMenuBar(session, updater, engineUpdates, open, quit)
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
                engineUpdates: engineUpdates
            )
        }
        controller.start()
        menuBar = controller
    }

    func applicationWillTerminate(_ notification: Notification) {
        installer.cancel()
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
}
