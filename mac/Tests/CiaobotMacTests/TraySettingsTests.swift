import XCTest
import AppKit
import Combine
import ServiceManagement

@testable import CiaobotMac

/// The settings controls live in a submenu now, so a test that only wants the
/// control itself searches the whole tree. Composition tests separately assert
/// that the controls are neither top-level rows nor duplicated in both places.
private extension NSMenu {
    func deepItem(withTitle title: String) -> NSMenuItem? {
        if let item = item(withTitle: title) { return item }
        for item in items {
            if let submenu = item.submenu,
               let found = submenu.deepItem(withTitle: title) {
                return found
            }
        }
        return nil
    }
}

@MainActor
private final class FakeLoginItem: LoginItemControlling {
    var state: LoginItemState
    private(set) var refreshCount = 0
    private(set) var registerCount = 0
    private(set) var unregisterCount = 0
    var registerError: Error?
    var unregisterError: Error?
    var stateAfterRegister: LoginItemState = .enabled
    var stateAfterUnregister: LoginItemState = .notRegistered

    init(_ state: LoginItemState = .notRegistered) {
        self.state = state
    }

    func refresh() {
        refreshCount += 1
    }

    func register() throws {
        registerCount += 1
        if let registerError {
            state = .failed(operation: .register, reason: SystemLoginItem.message(for: registerError))
            throw registerError
        }
        state = stateAfterRegister
    }

    func unregister() throws {
        unregisterCount += 1
        if let unregisterError {
            state = .failed(operation: .unregister, reason: SystemLoginItem.message(for: unregisterError))
            throw unregisterError
        }
        state = stateAfterUnregister
    }
}

@MainActor
private final class FakeDockIcon: DockIconControlling {
    var hidesDockIcon: Bool
    private(set) var setCount = 0
    private(set) var applyCount = 0
    private(set) var prepareCount = 0
    private(set) var windowCloseCount = 0

    init(hidden: Bool = false) {
        self.hidesDockIcon = hidden
    }

    func setHidesDockIcon(_ hidden: Bool) {
        hidesDockIcon = hidden
        setCount += 1
    }

    func applyPersistedPreference() { applyCount += 1 }
    func prepareForWindowPresentation() { prepareCount += 1 }
    func windowDidClose() { windowCloseCount += 1 }
}

private final class FakeDockPreference: DockIconPreferenceStoring {
    var hidesDockIcon: Bool

    init(_ hidden: Bool = false) {
        self.hidesDockIcon = hidden
    }
}

@MainActor
private final class FakeActivationPolicy: ActivationPolicyApplying {
    var currentPolicy: NSApplication.ActivationPolicy
    private(set) var setPolicies: [NSApplication.ActivationPolicy] = []

    init(_ policy: NSApplication.ActivationPolicy = .regular) {
        self.currentPolicy = policy
    }

    func setPolicy(_ policy: NSApplication.ActivationPolicy) {
        currentPolicy = policy
        setPolicies.append(policy)
    }
}

@MainActor
private final class FakeSettingsOpener: SystemSettingsOpening {
    private(set) var notificationSettingsCount = 0
    private(set) var loginItemsSettingsCount = 0

    func openNotificationSettings() { notificationSettingsCount += 1 }
    func openLoginItemsSettings() { loginItemsSettingsCount += 1 }
}

/// The menu's view of the updater. It never starts Sparkle and never contacts
/// the feed; it only records what the menu asked for.
@MainActor
private final class FakeClientUpdater: ClientUpdateControlling {
    var canCheckForUpdates: Bool
    var unavailableReason: String?
    private(set) var startCount = 0
    private(set) var checkCount = 0

    init(canCheckForUpdates: Bool = true, unavailableReason: String? = nil) {
        self.canCheckForUpdates = canCheckForUpdates
        self.unavailableReason = unavailableReason
    }

    func start() { startCount += 1 }
    func checkForUpdates() { checkCount += 1 }
}

@MainActor
private final class FakeSettingsNotifications: NotificationMenuProviding {
    let subject: CurrentValueSubject<ChatNotificationState, Never>
    let streamSubject: CurrentValueSubject<ChatNotificationStreamStatus, Never>
    private(set) var actionCount = 0
    private(set) var refreshCount = 0
    private(set) var stopCount = 0

    init(_ state: ChatNotificationState = .off, stream: ChatNotificationStreamStatus = .ready) {
        subject = CurrentValueSubject(state)
        streamSubject = CurrentValueSubject(stream)
    }

    var state: ChatNotificationState { subject.value }
    var statePublisher: AnyPublisher<ChatNotificationState, Never> { subject.eraseToAnyPublisher() }
    var streamStatus: ChatNotificationStreamStatus { streamSubject.value }
    var streamStatusPublisher: AnyPublisher<ChatNotificationStreamStatus, Never> {
        streamSubject.eraseToAnyPublisher()
    }

    func performMenuAction() { actionCount += 1 }
    func refreshIfNeeded() { refreshCount += 1 }
    func stop() { stopCount += 1 }
}

@MainActor
private final class SettingsFakeStatusItem: MenuBarStatusItem {
    var title: String?
    var image: NSImage?
    var toolTip: String?
    var menu: NSMenu?
    var isVisible = false

    func remove() {}
}

@MainActor
private final class SettingsFakeStatusItemFactory: StatusItemFactory {
    private(set) var items: [SettingsFakeStatusItem] = []

    func makeStatusItem(length: CGFloat) -> MenuBarStatusItem {
        let item = SettingsFakeStatusItem()
        items.append(item)
        return item
    }
}

@MainActor
private final class SettingsFakeMenuBar: MenuBarControlling {
    private(set) var startCount = 0
    private(set) var stopCount = 0

    func start() { startCount += 1 }
    func stop() { stopCount += 1 }
}

@MainActor
private final class SettingsSpyWindow: NSWindow {
    private(set) var orderFrontCount = 0
    private(set) var deminiaturizeCount = 0

    override func deminiaturize(_ sender: Any?) {
        deminiaturizeCount += 1
    }

    override func makeKeyAndOrderFront(_ sender: Any?) {
        orderFrontCount += 1
    }
}

@MainActor
final class TraySettingsTests: XCTestCase {
    private let origin = Origin("http://127.0.0.1:8443")!

    private func makeController(
        status: AppSessionStatus? = nil,
        loginItem: LoginItemControlling? = nil,
        dockIcon: DockIconControlling? = nil,
        systemSettings: SystemSettingsOpening? = nil,
        notifications: NotificationMenuProviding? = nil,
        changeHost: (() -> Void)? = nil,
        updates: ClientUpdateControlling? = nil,
        openWindow: @escaping () -> Void = {}
    ) -> MenuBarController {
        MenuBarController(
            status: status ?? AppSessionStatus(),
            statusItemFactory: SettingsFakeStatusItemFactory(),
            openWindow: openWindow,
            quit: {},
            notifications: notifications,
            loginItem: loginItem,
            dockIcon: dockIcon,
            systemSettings: systemSettings,
            changeHost: changeHost,
            updates: updates
        )
    }

    // MARK: - Start at Login

    func testLoginItemIsOptInAndNeverRegistersAtLaunchOrOnMenuOpen() throws {
        let loginItem = FakeLoginItem(.notRegistered)
        let controller = makeController(loginItem: loginItem)
        controller.start()

        let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.loginItemTitle))
        XCTAssertTrue(item.isEnabled)
        XCTAssertEqual(item.state, .off)
        XCTAssertTrue(item.target === controller)
        XCTAssertEqual(item.action, #selector(MenuBarController.toggleLoginItem(_:)))
        XCTAssertEqual(loginItem.registerCount, 0, "Launch must not enable the login item automatically.")

        controller.menuWillOpen(try XCTUnwrap(controller.menu))
        XCTAssertEqual(loginItem.refreshCount, 1)
        XCTAssertEqual(loginItem.registerCount, 0, "Opening the menu only re-reads the OS state.")
        XCTAssertEqual(loginItem.state, .notRegistered)
    }

    func testLoginItemRegistersOnlyAfterTheExplicitClickAndShowsOn() throws {
        let loginItem = FakeLoginItem(.notRegistered)
        let controller = makeController(loginItem: loginItem)
        controller.start()

        controller.toggleLoginItem(try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.loginItemTitle)))

        XCTAssertEqual(loginItem.registerCount, 1)
        let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.loginItemTitle))
        XCTAssertEqual(item.state, .on)
        XCTAssertEqual(item.action, #selector(MenuBarController.toggleLoginItem(_:)))
    }

    func testLoginItemEnabledRowTurnsTheLoginItemOff() throws {
        let loginItem = FakeLoginItem(.enabled)
        let controller = makeController(loginItem: loginItem)
        controller.start()

        let enabled = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.loginItemTitle))
        XCTAssertEqual(enabled.state, .on)
        controller.toggleLoginItem(enabled)

        XCTAssertEqual(loginItem.unregisterCount, 1)
        XCTAssertEqual(loginItem.registerCount, 0)
        let off = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.loginItemTitle))
        XCTAssertEqual(off.state, .off)
        XCTAssertFalse(loginItem.state == .enabled)
    }

    func testLoginItemApprovalNeedsActionOpensLoginItemsAndNeverClaimsOn() throws {
        let loginItem = FakeLoginItem(.requiresApproval)
        let opener = FakeSettingsOpener()
        let controller = makeController(loginItem: loginItem, systemSettings: opener)
        controller.start()

        let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.loginItemApprovalTitle))
        XCTAssertTrue(item.isEnabled)
        XCTAssertNotEqual(item.state, .on, "Registration requiring approval is not success.")
        XCTAssertTrue(item.toolTip?.contains("Login Items") == true)

        controller.toggleLoginItem(item)

        XCTAssertEqual(opener.loginItemsSettingsCount, 1)
        XCTAssertEqual(opener.notificationSettingsCount, 0, "The notification Settings link is never used for a login item.")
        XCTAssertEqual(loginItem.registerCount, 0, "Approval is not a reason to register again.")
        XCTAssertNotEqual(loginItem.state, .enabled)
    }

    func testLoginItemRegistrationFailureIsShownWithTheExactReason() throws {
        let loginItem = FakeLoginItem(.notRegistered)
        loginItem.registerError = NSError(
            domain: "CiaobotTests",
            code: 1,
            userInfo: [NSLocalizedDescriptionKey: "Operation not permitted"]
        )
        let opener = FakeSettingsOpener()
        let controller = makeController(loginItem: loginItem, systemSettings: opener)
        controller.start()

        controller.toggleLoginItem(try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.loginItemTitle)))

        XCTAssertEqual(opener.loginItemsSettingsCount, 1, "A refused registration opens Login Items with the drag tile.")
        XCTAssertEqual(opener.notificationSettingsCount, 0)
        let failed = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.loginItemFailedTitle))
        XCTAssertTrue(failed.isEnabled, "A failed registration can be retried.")
        XCTAssertNotEqual(failed.state, .on)
        XCTAssertTrue(failed.toolTip?.contains("Operation not permitted") == true)
        XCTAssertTrue(failed.toolTip?.contains("macOS could not change the login item") == true)
    }

    func testLoginItemUnregisterFailureRetriesAStopInsteadOfEnabling() throws {
        let loginItem = FakeLoginItem(.enabled)
        loginItem.unregisterError = NSError(
            domain: "CiaobotTests",
            code: 2,
            userInfo: [NSLocalizedDescriptionKey: "Operation not permitted"]
        )
        let controller = makeController(loginItem: loginItem)
        controller.start()

        controller.toggleLoginItem(
            try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.loginItemTitle))
        )

        XCTAssertEqual(loginItem.unregisterCount, 1)
        XCTAssertEqual(loginItem.registerCount, 0)
        let failed = try XCTUnwrap(
            controller.menu?.deepItem(withTitle: MenuBarController.loginItemRemoveFailedTitle)
        )
        XCTAssertTrue(failed.isEnabled)
        XCTAssertTrue(failed.toolTip?.contains("Operation not permitted") == true)

        // Retrying the failed stop must not turn into an enable action.
        controller.toggleLoginItem(failed)
        XCTAssertEqual(loginItem.unregisterCount, 2)
        XCTAssertEqual(loginItem.registerCount, 0)
    }

    func testLoginItemUnavailableRowIsDiagnosticAndNeverRegisters() throws {
        let reason = LoginItemUnavailableCopy.explanation(for: .bareExecutable)
        let loginItem = FakeLoginItem(
            .unavailable(LoginItemUnavailability(cause: .bareExecutable, reason: reason))
        )
        let opener = FakeSettingsOpener()
        let controller = makeController(loginItem: loginItem, systemSettings: opener)
        controller.start()

        // A bare executable keeps the wording it has always had, but the row is
        // now a click that can explain itself instead of a dead greyed row.
        let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.loginItemUnavailableTitle))
        XCTAssertTrue(item.isEnabled)
        XCTAssertEqual(item.action, #selector(MenuBarController.explainLoginItemUnavailable(_:)))
        XCTAssertEqual(item.toolTip, reason)
        XCTAssertNotEqual(item.state, .on)

        controller.toggleLoginItem(item)
        XCTAssertEqual(loginItem.registerCount, 0)
        XCTAssertEqual(loginItem.unregisterCount, 0)
        XCTAssertEqual(opener.loginItemsSettingsCount, 0)
    }

    func testLoginItemStatusMappingIsTruthful() {
        XCTAssertEqual(SystemLoginItem.state(for: .enabled), .enabled)
        XCTAssertEqual(SystemLoginItem.state(for: .requiresApproval), .requiresApproval)
        XCTAssertEqual(SystemLoginItem.state(for: .notRegistered), .notRegistered)
        XCTAssertEqual(
            SystemLoginItem.state(for: .notFound),
            .notRegistered,
            "A never-registered installed app reports notFound; registering is still the next step."
        )
    }

    // MARK: - Hide Dock Icon

    func testDockIconToggleWithWindowClosedPersistsAndAppliesAccessory() throws {
        let preferences = FakeDockPreference(false)
        let policy = FakeActivationPolicy(.regular)
        let dockIcon = DockIconController(
            preferences: preferences,
            policy: policy,
            mainWindowIsOpen: { false }
        )
        let controller = makeController(dockIcon: dockIcon)
        controller.start()

        let off = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.hideDockIconTitle))
        XCTAssertEqual(off.state, .off)

        controller.toggleDockIcon(off)
        XCTAssertTrue(preferences.hidesDockIcon)
        XCTAssertEqual(policy.setPolicies, [.accessory])

        let on = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.hideDockIconTitle))
        XCTAssertEqual(on.state, .on)

        controller.toggleDockIcon(on)
        XCTAssertFalse(preferences.hidesDockIcon)
        XCTAssertEqual(policy.setPolicies, [.accessory, .regular])
    }

    func testDockIconToggleWithWindowOpenStaysRegularUntilTheWindowCloses() throws {
        let preferences = FakeDockPreference(false)
        let policy = FakeActivationPolicy(.regular)
        var windowOpen = true
        let dockIcon = DockIconController(
            preferences: preferences,
            policy: policy,
            mainWindowIsOpen: { windowOpen }
        )
        let controller = makeController(dockIcon: dockIcon)
        controller.start()

        controller.toggleDockIcon(
            try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.hideDockIconTitle))
        )

        XCTAssertTrue(preferences.hidesDockIcon)
        XCTAssertEqual(policy.setPolicies, [.regular], "An open window must stay a regular app.")
        let row = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.hideDockIconTitle))
        XCTAssertEqual(row.state, .on)

        windowOpen = false
        dockIcon.windowDidClose()
        XCTAssertEqual(policy.setPolicies, [.regular, .accessory])
    }

    func testDockIconRowReflectsTheSavedPreferenceAtLaunch() throws {
        let controller = makeController(dockIcon: FakeDockIcon(hidden: true))
        controller.start()

        let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.hideDockIconTitle))
        XCTAssertEqual(item.state, .on)
    }

    func testDockPreferenceAppliesOnlyWhileTheMainWindowIsClosed() {
        let preferences = FakeDockPreference(true)
        let policy = FakeActivationPolicy(.regular)
        var windowOpen = true
        let dockIcon = DockIconController(
            preferences: preferences,
            policy: policy,
            mainWindowIsOpen: { windowOpen }
        )

        dockIcon.applyPersistedPreference()
        XCTAssertEqual(policy.setPolicies, [.regular], "An open window must not hide the Dock icon.")

        windowOpen = false
        dockIcon.applyPersistedPreference()
        XCTAssertEqual(policy.setPolicies, [.regular, .accessory])
        XCTAssertTrue(preferences.hidesDockIcon, "Applying the saved choice must not change it.")
    }

    func testOpeningTheWindowRestoresRegularAndClosingItHidesTheDock() {
        let preferences = FakeDockPreference(true)
        let policy = FakeActivationPolicy(.accessory)
        let dockIcon = DockIconController(
            preferences: preferences,
            policy: policy,
            mainWindowIsOpen: { true }
        )
        let window = SettingsSpyWindow(
            contentRect: NSRect(x: 0, y: 0, width: 200, height: 200),
            styleMask: [.titled],
            backing: .buffered,
            defer: true
        )
        window.identifier = MainWindowPresenter.mainWindowIdentifier
        let presenter = MainWindowPresenter(
            windows: { [window] },
            activate: {},
            prepareActivation: { dockIcon.prepareForWindowPresentation() }
        )

        XCTAssertTrue(presenter.present())
        XCTAssertEqual(policy.setPolicies, [.regular], "Open must restore a regular app so the window can become key.")
        XCTAssertEqual(window.orderFrontCount, 1)
        XCTAssertEqual(window.deminiaturizeCount, 1, "A miniaturized window must be restored, not merely ordered.")
        XCTAssertTrue(preferences.hidesDockIcon, "Opening the window must not silently disable the saved choice.")

        dockIcon.windowDidClose()
        XCTAssertEqual(policy.setPolicies, [.regular, .accessory])
    }

    func testPresenterFailureRestoresTheHiddenDockPolicyAndReportsIt() {
        let preferences = FakeDockPreference(true)
        let policy = FakeActivationPolicy(.accessory)
        let dockIcon = DockIconController(
            preferences: preferences,
            policy: policy,
            mainWindowIsOpen: { false }
        )
        var activated = 0
        var reported = 0
        let presenter = MainWindowPresenter(
            windows: { [] },
            activate: { activated += 1 },
            prepareActivation: { dockIcon.prepareForWindowPresentation() },
            restoreActivationOnFailure: { dockIcon.applyPersistedPreference() },
            reportFailure: { reported += 1 }
        )

        XCTAssertFalse(presenter.present())
        XCTAssertEqual(activated, 1)
        XCTAssertEqual(reported, 1, "A missing window must not be a silent no-op.")
        XCTAssertEqual(policy.setPolicies, [.accessory], "A failed open must leave the saved accessory policy, not .regular.")
        XCTAssertEqual(policy.currentPolicy, .accessory)
        XCTAssertTrue(preferences.hidesDockIcon)
    }

    func testPresenterWithoutHiddenPreferenceDoesNotChangePolicyOnFailure() {
        var reported = 0
        let presenter = MainWindowPresenter(
            windows: { [] },
            activate: {},
            restoreActivationOnFailure: {},
            reportFailure: { reported += 1 }
        )

        XCTAssertFalse(presenter.present())
        XCTAssertEqual(reported, 1)
    }

    func testAppLaunchAppliesTheSavedDockChoiceWithoutEnablingTheLoginItem() {
        _ = NSApplication.shared
        let preferences = FakeDockPreference(true)
        let policy = FakeActivationPolicy(.regular)
        let dockIcon = DockIconController(
            preferences: preferences,
            policy: policy,
            mainWindowIsOpen: { true }
        )
        let loginItem = FakeLoginItem(.notRegistered)
        let menuBar = SettingsFakeMenuBar()
        let delegate = AppDelegate(
            session: AppSessionStatus(),
            presenter: MainWindowPresenter(windows: { [] }, activate: {}),
            makeMenuBar: { _, _, _, _, _ in menuBar },
            loginItem: loginItem,
            dockIcon: dockIcon
        )

        delegate.applicationDidFinishLaunching(
            Notification(name: NSApplication.didFinishLaunchingNotification)
        )
        RunLoop.main.run(until: Date().addingTimeInterval(0.02))

        XCTAssertEqual(policy.setPolicies, [.regular], "The initial visible window keeps the app regular.")
        XCTAssertEqual(menuBar.startCount, 1)
        XCTAssertEqual(loginItem.registerCount, 0, "Launch must not opt in to a login item.")

        delegate.applicationWillTerminate(
            Notification(name: NSApplication.willTerminateNotification)
        )
        XCTAssertEqual(menuBar.stopCount, 1)
    }

    // MARK: - Change Connected Host

    func testChangeConnectedHostIsDisabledUntilConnected() throws {
        let status = AppSessionStatus()
        let controller = makeController(status: status, changeHost: {})
        controller.start()

        let disconnected = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.changeHostTitle))
        XCTAssertFalse(disconnected.isEnabled)
        XCTAssertNil(disconnected.action)
        XCTAssertTrue(disconnected.toolTip?.contains("Connect") == true)

        status.reportConnected(origin)
        RunLoop.main.run(until: Date().addingTimeInterval(0.02))

        let connected = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.changeHostTitle))
        XCTAssertTrue(connected.isEnabled)
        XCTAssertEqual(connected.action, #selector(MenuBarController.changeConnectedHost(_:)))
    }

    func testChangeConnectedHostCallsOnlyItsAction() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        var changeCount = 0
        var openCount = 0
        let controller = makeController(
            status: status,
            changeHost: { changeCount += 1 },
            openWindow: { openCount += 1 }
        )
        controller.start()

        let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.changeHostTitle))
        controller.changeConnectedHost(item)

        XCTAssertEqual(changeCount, 1)
        XCTAssertEqual(openCount, 0, "The menu item delegates the window presentation with the change request.")
    }

    func testConnectionMenuActionPresentsAndRequestsTheConnectionScreen() {
        var presentCount = 0
        let expectation = expectation(forNotification: .ciaobotChangeServer, object: nil)
        let action = ConnectionMenuActions.changeConnectedHost(present: {
            presentCount += 1
            return true
        })

        action()

        wait(for: [expectation], timeout: 1)
        XCTAssertEqual(presentCount, 1)
    }

    func testConnectionMenuActionDoesNotPostWhenTheWindowCannotBePresented() {
        let expectation = expectation(forNotification: .ciaobotChangeServer, object: nil)
        expectation.isInverted = true
        var presentCount = 0
        let action = ConnectionMenuActions.changeConnectedHost(present: {
            presentCount += 1
            return false
        })

        action()

        wait(for: [expectation], timeout: 0.1)
        XCTAssertEqual(presentCount, 1, "The presenter is asked once and owns the failure feedback.")
    }

    // MARK: - Updates

    func testUpdatesRowIsDisabledAndDoesNotPretendToCheck() throws {
        let controller = makeController(loginItem: FakeLoginItem(.notRegistered))
        controller.start()

        let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.updatesUnavailableTitle))
        XCTAssertFalse(item.isEnabled)
        XCTAssertNil(item.action)
        XCTAssertTrue(item.toolTip?.contains("no updater") == true)
        XCTAssertTrue(item.toolTip?.contains("does not download or install updates") == true)
    }

    /// A build with a started updater gets the real action, enabled only while
    /// Sparkle says a check can run.
    func testAvailableUpdaterOffersCheckForUpdatesAndForwardsTheClick() throws {
        let updater = FakeClientUpdater(canCheckForUpdates: true)
        let controller = makeController(updates: updater)
        controller.start()

        let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.checkForUpdatesTitle))
        XCTAssertTrue(item.isEnabled)
        XCTAssertNil(controller.menu?.deepItem(withTitle: MenuBarController.updatesUnavailableTitle))
        XCTAssertEqual(item.action, #selector(MenuBarController.checkForUpdates(_:)))
        XCTAssertTrue(item.toolTip?.contains("restarts this Mac client only") == true,
                      "The tip must not imply that the engine or the web app restarts.")
        XCTAssertTrue(item.toolTip?.contains("verified") == true)
        XCTAssertEqual(updater.startCount, 0, "Rendering the menu must not start the updater.")

        controller.checkForUpdates(item)
        XCTAssertEqual(updater.checkCount, 1)
        XCTAssertEqual(updater.startCount, 0)
    }

    func testBusyUpdaterKeepsTheRealRowVisibleButDisabled() throws {
        let updater = FakeClientUpdater(canCheckForUpdates: false)
        let controller = makeController(updates: updater)
        controller.start()

        let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.checkForUpdatesTitle))
        XCTAssertFalse(item.isEnabled, "Sparkle is already checking or downloading.")
        XCTAssertNotNil(item.action)

        controller.checkForUpdates(item)
        XCTAssertEqual(updater.checkCount, 0, "A disabled row must not ask Sparkle to check.")
    }

    /// An updater object can exist and still be unusable. The row then keeps its
    /// truthful disabled shape and names the reason instead of offering a check.
    func testUnavailableUpdaterKeepsTheDisabledRowWithItsReason() throws {
        let reason = "This build has no update feed configured."
        let updater = FakeClientUpdater(canCheckForUpdates: false, unavailableReason: reason)
        let controller = makeController(updates: updater)
        controller.start()

        XCTAssertNil(controller.menu?.deepItem(withTitle: MenuBarController.checkForUpdatesTitle))
        let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.updatesUnavailableTitle))
        XCTAssertFalse(item.isEnabled)
        XCTAssertNil(item.action)
        XCTAssertTrue(item.toolTip?.contains(reason) == true)
        XCTAssertTrue(item.toolTip?.contains("does not download or install updates") == true)

        controller.checkForUpdates(item)
        XCTAssertEqual(updater.checkCount, 0)
    }

    func testOpeningTheMenuNeverStartsAnUpdateCheck() throws {
        let updater = FakeClientUpdater(canCheckForUpdates: true)
        let controller = makeController(updates: updater)
        controller.start()
        let menu = try XCTUnwrap(controller.menu)

        controller.menuWillOpen(menu)
        controller.menuDidClose(menu)
        controller.menuWillOpen(menu)
        controller.menuDidClose(menu)

        XCTAssertEqual(updater.startCount, 0, "Opening the menu must never start the updater.")
        XCTAssertEqual(updater.checkCount, 0, "Opening the menu must never check for updates.")
    }

    func testAnAvailableUpdaterBringsTheSettingsSubmenuWithIt() throws {
        let updater = FakeClientUpdater(canCheckForUpdates: true)
        let controller = makeController(updates: updater)
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        XCTAssertEqual(
            menu.items.filter { !$0.isSeparatorItem }.map(\.title),
            [
                MenuBarController.disconnectedTitle,
                MenuBarController.openTitle,
                MenuBarController.settingsTitle,
                MenuBarController.quitTitle,
            ]
        )
        let settings = try XCTUnwrap(menu.item(withTitle: MenuBarController.settingsTitle))
        let submenu = try XCTUnwrap(settings.submenu)
        XCTAssertEqual(submenu.items.map(\.title), [MenuBarController.checkForUpdatesTitle])
    }

    // MARK: - Notification guidance

    func testDeniedNotificationsExplainWhyAndHowInTheVisibleRow() throws {
        let notifications = FakeSettingsNotifications(.denied)
        let opener = FakeSettingsOpener()
        let controller = makeController(systemSettings: opener, notifications: notifications)
        controller.start()

        XCTAssertTrue(MenuBarController.notificationsBlockedTitle.contains("blocked by macOS"))
        XCTAssertTrue(MenuBarController.notificationsBlockedTitle.contains("Open Settings"))
        let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.notificationsBlockedTitle))
        XCTAssertTrue(item.isEnabled)
        XCTAssertEqual(item.action, #selector(MenuBarController.openNotificationSettings(_:)))
        XCTAssertTrue(item.toolTip?.contains("System Settings > Notifications") == true)
        XCTAssertEqual(notifications.actionCount, 0, "Rendering the row must not request permission.")

        controller.openNotificationSettings(item)
        XCTAssertEqual(opener.notificationSettingsCount, 1)
        XCTAssertEqual(opener.loginItemsSettingsCount, 0)
    }

    func testUnbundledNotificationsNameTheBareExecutableWithoutClaimingUnsigned() throws {
        let notifications = FakeSettingsNotifications(.unavailable(.unbundled))
        let opener = FakeSettingsOpener()
        let controller = makeController(systemSettings: opener, notifications: notifications)
        controller.start()

        XCTAssertTrue(MenuBarController.notificationsUnbundledTitle.contains("bare executable"))
        XCTAssertFalse(MenuBarController.notificationsUnbundledTitle.contains("unsigned"))
        let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.notificationsUnbundledTitle))
        XCTAssertFalse(item.isEnabled)
        XCTAssertNil(item.action)
        XCTAssertTrue(item.toolTip?.contains("bundled") == true)
        XCTAssertTrue(item.toolTip?.contains("System Settings cannot") == true)

        controller.openNotificationSettings(item)
        XCTAssertEqual(opener.notificationSettingsCount, 0, "System Settings cannot make a bare executable a bundled app.")
        XCTAssertEqual(opener.loginItemsSettingsCount, 0)
    }

    func testRegistrationFailurePreservesTheTechnicalErrorWithoutClaimingDenialOrSignature() throws {
        let reason = "Notifications are not available for this application"
        let notifications = FakeSettingsNotifications(.unavailable(.permissionRequestFailed(reason)))
        let opener = FakeSettingsOpener()
        let controller = makeController(systemSettings: opener, notifications: notifications)
        controller.start()

        let title = MenuBarController.notificationsPermissionRequestFailedTitle
        XCTAssertTrue(title.contains("registration failed"))
        XCTAssertFalse(title.contains("blocked"), "A technical failure is not a macOS denial.")
        XCTAssertFalse(title.contains("unsigned"), "The client cannot determine the signature, so it must not assert it.")
        let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: title))
        XCTAssertFalse(item.isEnabled)
        XCTAssertNil(item.action)
        XCTAssertTrue(item.toolTip?.contains(reason) == true)
        XCTAssertTrue(item.toolTip?.contains("cannot tell") == true)
        XCTAssertTrue(item.toolTip?.contains("Ciaobot.app") == true)
        XCTAssertEqual(opener.notificationSettingsCount, 0)
    }

    func testUnknownAuthorizationStateIsNotPresentedAsBlocked() throws {
        let reason = "A future macOS authorization state"
        let notifications = FakeSettingsNotifications(.unavailable(.unknownAuthorization(reason)))
        let opener = FakeSettingsOpener()
        let controller = makeController(systemSettings: opener, notifications: notifications)
        controller.start()

        let title = MenuBarController.notificationsUnknownAuthorizationTitle
        XCTAssertTrue(title.contains("unknown"))
        XCTAssertFalse(title.contains("blocked"))
        let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: title))
        XCTAssertFalse(item.isEnabled)
        XCTAssertNil(item.action)
        XCTAssertTrue(item.toolTip?.contains(reason) == true)
        XCTAssertEqual(opener.notificationSettingsCount, 0)
    }

    func testSignInNeededNotificationRowKeepsEnableDisableAndTheRecoveryRow() throws {
        let notifications = FakeSettingsNotifications(.on, stream: .waitingForAuthentication)
        let opener = FakeSettingsOpener()
        var openCount = 0
        let controller = makeController(
            systemSettings: opener,
            notifications: notifications,
            openWindow: { openCount += 1 }
        )
        controller.start()

        XCTAssertTrue(MenuBarController.notificationsSignInTitle.contains("sign in"))
        let row = try XCTUnwrap(
            controller.menu?.deepItem(withTitle: MenuBarController.notificationsSignInTitle)
        )
        XCTAssertTrue(row.isEnabled)
        XCTAssertEqual(row.state, .on)
        XCTAssertEqual(
            row.action,
            #selector(MenuBarController.toggleNotifications(_:)),
            "The status row must keep the explicit disable action."
        )
        XCTAssertTrue(row.toolTip?.contains("signed-in session") == true)

        let recovery = try XCTUnwrap(
            controller.menu?.deepItem(withTitle: MenuBarController.signInForNotificationsTitle)
        )
        XCTAssertTrue(recovery.isEnabled)
        XCTAssertEqual(recovery.action, #selector(MenuBarController.openCiaobotForNotifications(_:)))
        XCTAssertTrue(recovery.toolTip?.contains("sign in") == true)
        XCTAssertNil(
            controller.menu?.deepItem(withTitle: MenuBarController.notificationsOnTitle),
            "A plain On row would hide the missing sign-in."
        )
        XCTAssertEqual(notifications.actionCount, 0, "The row is shown without requesting permission.")

        controller.toggleNotifications(row)
        XCTAssertEqual(notifications.actionCount, 1, "The status row still turns notifications off.")
        controller.openCiaobotForNotifications(recovery)
        XCTAssertEqual(openCount, 1)
        XCTAssertEqual(opener.notificationSettingsCount, 0)
        XCTAssertEqual(opener.loginItemsSettingsCount, 0)
    }

    func testConnectionNeededNotificationRowKeepsEnableDisableAndTheRecoveryRow() throws {
        let notifications = FakeSettingsNotifications(.on, stream: .notConnected)
        let opener = FakeSettingsOpener()
        var openCount = 0
        let controller = makeController(
            systemSettings: opener,
            notifications: notifications,
            openWindow: { openCount += 1 }
        )
        controller.start()

        XCTAssertTrue(MenuBarController.notificationsWaitingTitle.contains("connect"))
        let row = try XCTUnwrap(
            controller.menu?.deepItem(withTitle: MenuBarController.notificationsWaitingTitle)
        )
        XCTAssertTrue(row.isEnabled)
        XCTAssertEqual(row.action, #selector(MenuBarController.toggleNotifications(_:)))
        XCTAssertTrue(row.toolTip?.contains("Connect") == true)

        let recovery = try XCTUnwrap(
            controller.menu?.deepItem(withTitle: MenuBarController.connectForNotificationsTitle)
        )
        XCTAssertTrue(recovery.isEnabled)
        XCTAssertEqual(recovery.action, #selector(MenuBarController.openCiaobotForNotifications(_:)))

        controller.openCiaobotForNotifications(recovery)
        XCTAssertEqual(openCount, 1)
        XCTAssertEqual(opener.notificationSettingsCount, 0)
        XCTAssertEqual(notifications.actionCount, 0)
    }

    func testConnectingNotificationRowExplainsItselfAndNeverOffersSettings() throws {
        for (stream, title) in [
            (ChatNotificationStreamStatus.connecting, MenuBarController.notificationsConnectingTitle),
            (.retrying, MenuBarController.notificationsRetryingTitle),
        ] {
            let notifications = FakeSettingsNotifications(.on, stream: stream)
            let opener = FakeSettingsOpener()
            let controller = makeController(systemSettings: opener, notifications: notifications)
            controller.start()

            let row = try XCTUnwrap(controller.menu?.deepItem(withTitle: title))
            XCTAssertTrue(row.isEnabled, "The row stays clickable so notifications can be turned off.")
            XCTAssertEqual(row.action, #selector(MenuBarController.toggleNotifications(_:)))
            XCTAssertNotEqual(row.action, #selector(MenuBarController.openNotificationSettings(_:)))
            XCTAssertEqual(opener.notificationSettingsCount, 0)
            XCTAssertNil(controller.menu?.deepItem(withTitle: MenuBarController.notificationsOnTitle))
            XCTAssertNil(controller.menu?.deepItem(withTitle: MenuBarController.checkServerForNotificationsTitle))
        }
    }

    func testTerminalStreamFailuresExplainTheirCauseAndOfferASafeNextStep() throws {
        let cases: [(ChatNotificationStreamStatus, String)] = [
            (.authenticationRefused, MenuBarController.notificationsAuthenticationRefusedTitle),
            (.originRejected, MenuBarController.notificationsOriginRejectedTitle),
            (.redirected, MenuBarController.notificationsRedirectedTitle),
            (.protocolError, MenuBarController.notificationsProtocolErrorTitle),
        ]

        for (stream, title) in cases {
            let notifications = FakeSettingsNotifications(.on, stream: stream)
            let opener = FakeSettingsOpener()
            var openCount = 0
            let controller = makeController(
                systemSettings: opener,
                notifications: notifications,
                openWindow: { openCount += 1 }
            )
            controller.start()

            let row = try XCTUnwrap(controller.menu?.deepItem(withTitle: title))
            XCTAssertTrue(row.isEnabled)
            XCTAssertEqual(row.action, #selector(MenuBarController.toggleNotifications(_:)))
            XCTAssertNotEqual(row.action, #selector(MenuBarController.openNotificationSettings(_:)))
            XCTAssertTrue(
                row.toolTip?.contains("not follow") == true
                    || row.toolTip?.contains("did not") == true,
                "The row must not imply that the unsafe attempt was accepted."
            )

            let recovery = try XCTUnwrap(
                controller.menu?.deepItem(withTitle: MenuBarController.checkServerForNotificationsTitle)
            )
            XCTAssertTrue(recovery.isEnabled)
            XCTAssertEqual(recovery.action, #selector(MenuBarController.openCiaobotForNotifications(_:)))
            controller.openCiaobotForNotifications(recovery)
            XCTAssertEqual(openCount, 1)
            XCTAssertEqual(opener.notificationSettingsCount, 0)
            XCTAssertEqual(notifications.actionCount, 0)
        }
    }

    func testSystemSettingsLinksAreFixedAppOwnedURLs() {
        var opened: [URL] = []
        let settings = WorkspaceSystemSettings(open: { opened.append($0) })

        settings.openNotificationSettings()
        settings.openLoginItemsSettings()

        XCTAssertEqual(opened.map(\.absoluteString), [
            "x-apple.systempreferences:com.apple.Notifications-Settings.extension",
            "x-apple.systempreferences:com.apple.LoginItems-Settings.extension",
        ])
        XCTAssertEqual(opened[0], WorkspaceSystemSettings.notificationSettingsURL)
    }

    func testEachSettingsPaneOpensWithItsOwnHelper() {
        var helpers: [SettingsHelperPane] = []
        let settings = WorkspaceSystemSettings(open: { _ in }, presentHelper: { helpers.append($0) })

        settings.openLoginItemsSettings()
        settings.openNotificationSettings()

        XCTAssertEqual(helpers, [.loginItems, .notifications])
        XCTAssertTrue(SettingsHelperPane.loginItems.acceptsDrop)
        XCTAssertFalse(
            SettingsHelperPane.notifications.acceptsDrop,
            "The Notifications pane does not accept a dropped app."
        )
    }

    // MARK: - Menu composition

    func testSettingsControlsLiveInOneCompactSubmenu() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let controller = makeController(
            status: status,
            loginItem: FakeLoginItem(.notRegistered),
            dockIcon: FakeDockIcon(),
            systemSettings: FakeSettingsOpener(),
            changeHost: {}
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        let topLevel = menu.items.filter { !$0.isSeparatorItem }.map(\.title)
        XCTAssertEqual(topLevel, [
            "Connected to http://127.0.0.1:8443",
            MenuBarController.openTitle,
            MenuBarController.settingsTitle,
            MenuBarController.quitTitle,
        ])
        XCTAssertNil(menu.item(withTitle: MenuBarController.loginItemTitle), "Settings controls are not top-level noise.")
        XCTAssertNil(menu.item(withTitle: MenuBarController.hideDockIconTitle))
        XCTAssertNil(menu.item(withTitle: MenuBarController.changeHostTitle))
        XCTAssertNil(menu.item(withTitle: MenuBarController.updatesUnavailableTitle))

        let settings = try XCTUnwrap(menu.item(withTitle: MenuBarController.settingsTitle))
        XCTAssertTrue(settings.isEnabled)
        let submenu = try XCTUnwrap(settings.submenu, "Settings must be a submenu, not a dead row.")
        XCTAssertFalse(submenu.autoenablesItems)
        XCTAssertEqual(submenu.items.map(\.title), [
            MenuBarController.loginItemTitle,
            MenuBarController.hideDockIconTitle,
            MenuBarController.changeHostTitle,
            MenuBarController.updatesUnavailableTitle,
        ])

        let login = try XCTUnwrap(submenu.item(withTitle: MenuBarController.loginItemTitle))
        XCTAssertTrue(login.isEnabled)
        XCTAssertEqual(login.action, #selector(MenuBarController.toggleLoginItem(_:)))
        let changeHost = try XCTUnwrap(submenu.item(withTitle: MenuBarController.changeHostTitle))
        XCTAssertTrue(changeHost.isEnabled, "A connected app can reach the connection screen.")
        XCTAssertEqual(changeHost.action, #selector(MenuBarController.changeConnectedHost(_:)))
        let updates = try XCTUnwrap(submenu.item(withTitle: MenuBarController.updatesUnavailableTitle))
        XCTAssertFalse(updates.isEnabled, "There is still no updater in this build.")
        XCTAssertNil(updates.action)
    }

    func testNoSettingsSubmenuIsAddedWithoutSettingsControls() throws {
        let controller = makeController()
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        XCTAssertNil(menu.item(withTitle: MenuBarController.settingsTitle))
        XCTAssertEqual(
            menu.items.filter { !$0.isSeparatorItem }.map(\.title),
            [MenuBarController.disconnectedTitle, MenuBarController.openTitle, MenuBarController.quitTitle]
        )
    }

    // MARK: - Notifications inside Settings

    func testSettingsAppearsWhenNotificationsAreTheOnlyProvider() throws {
        let notifications = FakeSettingsNotifications(.off)
        let controller = makeController(notifications: notifications)
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        XCTAssertEqual(
            menu.items.filter { !$0.isSeparatorItem }.map(\.title),
            [
                MenuBarController.disconnectedTitle,
                MenuBarController.openTitle,
                MenuBarController.settingsTitle,
                MenuBarController.quitTitle,
            ],
            "Notifications must bring the Settings submenu with them."
        )
        XCTAssertNil(
            menu.item(withTitle: MenuBarController.notificationsOffTitle),
            "The notification status row is not a top-level tray row any more."
        )

        let settings = try XCTUnwrap(menu.item(withTitle: MenuBarController.settingsTitle))
        let submenu = try XCTUnwrap(settings.submenu)
        XCTAssertEqual(
            submenu.items.filter { !$0.isSeparatorItem }.map(\.title),
            [MenuBarController.notificationsOffTitle, MenuBarController.updatesUnavailableTitle]
        )
        let row = try XCTUnwrap(submenu.item(withTitle: MenuBarController.notificationsOffTitle))
        XCTAssertTrue(row.isEnabled)
        XCTAssertEqual(row.action, #selector(MenuBarController.toggleNotifications(_:)))
        XCTAssertEqual(notifications.actionCount, 0, "Rendering the row must not request permission.")
    }

    func testNotificationStateAndRecoveryLiveOnlyInSettingsWithOtherControls() throws {
        let notifications = FakeSettingsNotifications(.on, stream: .waitingForAuthentication)
        let opener = FakeSettingsOpener()
        var openCount = 0
        let controller = makeController(
            loginItem: FakeLoginItem(.notRegistered),
            dockIcon: FakeDockIcon(),
            systemSettings: opener,
            notifications: notifications,
            changeHost: {},
            openWindow: { openCount += 1 }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        for title in [
            MenuBarController.notificationsSignInTitle,
            MenuBarController.signInForNotificationsTitle,
        ] {
            XCTAssertNil(
                menu.item(withTitle: title),
                "\(title) must not be duplicated at the top level."
            )
            XCTAssertEqual(
                menu.items.filter { $0.title == title }.count,
                0,
                "Notification rows belong to the Settings submenu only."
            )
        }

        let settings = try XCTUnwrap(menu.item(withTitle: MenuBarController.settingsTitle))
        let submenu = try XCTUnwrap(settings.submenu)
        XCTAssertEqual(submenu.items.map(\.title), [
            MenuBarController.notificationsSignInTitle,
            MenuBarController.signInForNotificationsTitle,
            "",
            MenuBarController.loginItemTitle,
            MenuBarController.hideDockIconTitle,
            MenuBarController.changeHostTitle,
            MenuBarController.updatesUnavailableTitle,
        ])

        let row = try XCTUnwrap(submenu.item(withTitle: MenuBarController.notificationsSignInTitle))
        XCTAssertEqual(row.action, #selector(MenuBarController.toggleNotifications(_:)))
        XCTAssertEqual(row.state, .on)
        let recovery = try XCTUnwrap(
            submenu.item(withTitle: MenuBarController.signInForNotificationsTitle)
        )
        XCTAssertEqual(recovery.action, #selector(MenuBarController.openCiaobotForNotifications(_:)))

        controller.openCiaobotForNotifications(recovery)
        XCTAssertEqual(openCount, 1)
        XCTAssertEqual(opener.notificationSettingsCount, 0)
        XCTAssertEqual(notifications.actionCount, 0)
    }

    func testBlockedNotificationSettingsLinkMovesIntoTheSubmenu() throws {
        let opener = FakeSettingsOpener()
        let notifications = FakeSettingsNotifications(.denied)
        let controller = makeController(systemSettings: opener, notifications: notifications)
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        XCTAssertNil(menu.item(withTitle: MenuBarController.notificationsBlockedTitle))
        let row = try XCTUnwrap(
            menu.deepItem(withTitle: MenuBarController.notificationsBlockedTitle)
        )
        XCTAssertEqual(row.action, #selector(MenuBarController.openNotificationSettings(_:)))

        controller.openNotificationSettings(row)
        XCTAssertEqual(opener.notificationSettingsCount, 1)
        XCTAssertEqual(notifications.actionCount, 0, "Only the explicit state row request is made.")
    }

    func testDeliveryOffNotificationSettingsLinkMovesIntoTheSubmenu() throws {
        let opener = FakeSettingsOpener()
        let notifications = FakeSettingsNotifications(.deliveryOff)
        let controller = makeController(systemSettings: opener, notifications: notifications)
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        XCTAssertNil(menu.item(withTitle: MenuBarController.notificationsDeliveryOffTitle))
        let row = try XCTUnwrap(
            menu.deepItem(withTitle: MenuBarController.notificationsDeliveryOffTitle)
        )
        XCTAssertTrue(row.isEnabled)
        XCTAssertEqual(row.action, #selector(MenuBarController.openNotificationSettings(_:)))
        XCTAssertTrue(row.toolTip?.contains("Focus") == true)

        controller.openNotificationSettings(row)
        XCTAssertEqual(opener.notificationSettingsCount, 1)
        XCTAssertEqual(opener.loginItemsSettingsCount, 0)
        XCTAssertEqual(notifications.actionCount, 0, "Only the explicit state row request is made.")
    }

    func testSettingsSubmenuHasNoDuplicatedRowsOrSeparators() throws {
        let notifications = FakeSettingsNotifications(.on, stream: .notConnected)
        let controller = makeController(
            loginItem: FakeLoginItem(.notRegistered),
            notifications: notifications
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        let submenu = try XCTUnwrap(menu.item(withTitle: MenuBarController.settingsTitle)?.submenu)

        XCTAssertFalse(submenu.items.first?.isSeparatorItem ?? false, "No leading separator.")
        XCTAssertFalse(submenu.items.last?.isSeparatorItem ?? false, "No trailing separator.")
        for (previous, next) in zip(submenu.items, submenu.items.dropFirst()) {
            XCTAssertFalse(
                previous.isSeparatorItem && next.isSeparatorItem,
                "Two separators in a row would be a blank row."
            )
        }
        let titles = submenu.items.filter { !$0.isSeparatorItem }.map(\.title)
        XCTAssertEqual(Set(titles).count, titles.count, "No row may appear twice: \(titles).")

        XCTAssertEqual(
            menu.items.filter { !$0.isSeparatorItem }.map(\.title),
            [
                MenuBarController.disconnectedTitle,
                MenuBarController.openTitle,
                MenuBarController.settingsTitle,
                MenuBarController.quitTitle,
            ]
        )
        let topLevelSeparators = menu.items.filter(\.isSeparatorItem).count
        XCTAssertEqual(topLevelSeparators, 3)
        for (previous, next) in zip(menu.items, menu.items.dropFirst()) {
            XCTAssertFalse(previous.isSeparatorItem && next.isSeparatorItem)
        }
    }

    func testOpeningTheMenuNeverPromptsEvenWhenNotificationsAreInSettings() throws {
        let notifications = FakeSettingsNotifications(.off)
        let controller = makeController(notifications: notifications)
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        controller.menuWillOpen(menu)

        XCTAssertEqual(notifications.refreshCount, 1, "Opening only re-reads the OS state.")
        XCTAssertEqual(notifications.actionCount, 0, "Opening the menu must never request permission.")
        XCTAssertNotNil(menu.deepItem(withTitle: MenuBarController.notificationsOffTitle))
        controller.menuDidClose(menu)
        XCTAssertEqual(notifications.actionCount, 0)
    }
}
