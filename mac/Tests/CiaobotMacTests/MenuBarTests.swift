import XCTest
import AppKit
import Combine

@testable import CiaobotMac

/// Notification rows now live inside the Settings submenu, so a test that only
/// wants the row searches the whole tree. Composition tests separately assert
/// that the rows are not top-level and are not duplicated.
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

/// A menu-bar item double. Controller tests never need a real `NSStatusBar`,
/// so they do not add an icon to the operator's menu bar. One adapter test at
/// the end creates and immediately removes a real item to prove that surface.
@MainActor
private final class FakeStatusItem: MenuBarStatusItem {
    var title: String?
    var image: NSImage?
    var toolTip: String?
    var menu: NSMenu?
    var isVisible = false
    private(set) var removeCount = 0

    func remove() {
        removeCount += 1
    }
}

@MainActor
private final class FakeStatusItemFactory: StatusItemFactory {
    private(set) var requestedLengths: [CGFloat] = []
    private(set) var items: [FakeStatusItem] = []

    func makeStatusItem(length: CGFloat) -> MenuBarStatusItem {
        requestedLengths.append(length)
        let item = FakeStatusItem()
        items.append(item)
        return item
    }
}

@MainActor
private final class SpyWindow: NSWindow {
    private(set) var orderFrontCount = 0

    override func makeKeyAndOrderFront(_ sender: Any?) {
        orderFrontCount += 1
    }
}

@MainActor
private final class FakeMainWindowPresenter: MainWindowPresenting {
    private(set) var presentCount = 0
    var result = true

    func present() -> Bool {
        presentCount += 1
        return result
    }
}

@MainActor
private final class FakeMenuBarController: MenuBarControlling {
    private(set) var startCount = 0
    private(set) var stopCount = 0

    func start() { startCount += 1 }
    func stop() { stopCount += 1 }
}

/// Records what the app asked of the updater. No Sparkle object exists, so no
/// test start can reach the network feed.
@MainActor
private final class FakeClientUpdater: ClientUpdateControlling {
    var canCheckForUpdates = true
    var unavailableReason: String?
    private(set) var startCount = 0
    private(set) var checkCount = 0

    func start() { startCount += 1 }
    func checkForUpdates() { checkCount += 1 }
}

/// Records what the menu asks of the engine-update provider. The rows are set
/// by the test and `subject` simulates a change; no monitor or probe exists.
@MainActor
private final class FakeEngineUpdateMenu: EngineUpdateMenuProviding {
    var rows: [EngineUpdateMenuRow] = []
    let subject = PassthroughSubject<Void, Never>()
    var changes: AnyPublisher<Void, Never> { subject.eraseToAnyPublisher() }
    private(set) var startCount = 0, stopCount = 0, refreshCount = 0, performCount = 0, acknowledgeCount = 0

    func start() { startCount += 1 }
    func stop() { stopCount += 1 }
    func refreshIfNeeded() { refreshCount += 1 }
    func performUpdate() { performCount += 1 }
    func acknowledgeOutcome() { acknowledgeCount += 1 }
}

@MainActor
private final class FakeNotificationMenu: NotificationMenuProviding {
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
    func set(_ state: ChatNotificationState) { subject.send(state) }
    func setStream(_ status: ChatNotificationStreamStatus) { streamSubject.send(status) }
}

@MainActor
private final class FakeSystemSettings: SystemSettingsOpening {
    private(set) var notificationSettingsCount = 0
    private(set) var loginItemsSettingsCount = 0

    func openNotificationSettings() { notificationSettingsCount += 1 }
    func openLoginItemsSettings() { loginItemsSettingsCount += 1 }
}

/// The login-item double. Rendering or explaining a row must never change
/// these counts.
@MainActor
private final class FakeLoginItem: LoginItemControlling {
    var state: LoginItemState
    private(set) var refreshCount = 0
    private(set) var registerCount = 0
    private(set) var unregisterCount = 0

    init(_ state: LoginItemState = .notRegistered) {
        self.state = state
    }

    func refresh() { refreshCount += 1 }
    func register() throws { registerCount += 1 }
    func unregister() throws { unregisterCount += 1 }
}

/// Records every Dock label the menu bar applies. The real `NSApp.dockTile` is
/// never touched by a test.
@MainActor
private final class FakeDockBadge: DockBadgeApplying {
    /// Every applied label in order. A `nil` entry is an explicit clear.
    private(set) var appliedLabels: [String?] = []

    /// The label the Dock would show now; `nil` means no badge.
    var currentLabel: String? { appliedLabels.last ?? nil }

    /// Only the numbers that were ever painted, so a test can prove a stale
    /// number was cleared rather than merely followed by another write.
    var appliedNumbers: [String] { appliedLabels.compactMap { $0 } }

    func setBadgeLabel(_ label: String?) {
        appliedLabels.append(label)
    }
}

@MainActor
private final class FakeChatList: ChatListProviding {
    let subject: CurrentValueSubject<ChatListState, Never>
    private(set) var refreshCount = 0

    init(_ state: ChatListState = .disconnected) {
        subject = CurrentValueSubject(state)
    }

    var state: ChatListState { subject.value }
    var statePublisher: AnyPublisher<ChatListState, Never> { subject.eraseToAnyPublisher() }

    func start() {}
    func stop() {}
    func refreshIfNeeded() { refreshCount += 1 }
    func set(_ state: ChatListState) { subject.send(state) }
}

@MainActor
private final class FakeActivity: ChatActivityProviding {
    let subject: CurrentValueSubject<ChatActivityState, Never>

    init(_ state: ChatActivityState = .inactive) {
        subject = CurrentValueSubject(state)
    }

    var state: ChatActivityState { subject.value }
    var statePublisher: AnyPublisher<ChatActivityState, Never> { subject.eraseToAnyPublisher() }

    func start() {}
    func stop() {}
    func refreshIfNeeded() {}
}

/// The fleet double: the controller only reads the ordered feeds, the one
/// total, and refreshes on menu open. It never builds a real model or store.
@MainActor
private final class FakeHostFleet: HostFleetProviding {
    let subject: CurrentValueSubject<[HostFleetModel.HostFeeds], Never>
    var feeds: [HostFleetModel.HostFeeds]
    var knownAttentionTotal: Int?
    private(set) var startCount = 0
    private(set) var stopCount = 0
    private(set) var refreshCount = 0

    init(feeds: [HostFleetModel.HostFeeds] = [], total: Int? = nil) {
        self.feeds = feeds
        self.knownAttentionTotal = total
        subject = CurrentValueSubject(feeds)
    }

    var feedsPublisher: AnyPublisher<[HostFleetModel.HostFeeds], Never> {
        subject.eraseToAnyPublisher()
    }

    func start() { startCount += 1 }
    func stop() { stopCount += 1 }
    func refreshIfNeeded() { refreshCount += 1 }

    func set(_ feeds: [HostFleetModel.HostFeeds], total: Int?) {
        self.feeds = feeds
        self.knownAttentionTotal = total
        subject.send(feeds)
    }
}

/// Records every frame the menu asks for, so a test can prove which rows were
/// redrawn and with which chase cell. It never draws a real image.
@MainActor
private final class FakeGlyphRenderer: MenuGlyphRendering {
    struct Call: Equatable {
        let litCell: Int?
        let size: CGFloat
        let reduceMotion: Bool
    }

    private(set) var calls: [Call] = []

    func glyphImage(litCell: Int?, size: CGFloat, reduceMotion: Bool) -> NSImage? {
        calls.append(Call(litCell: litCell, size: size, reduceMotion: reduceMotion))
        return NSImage(size: NSSize(width: size, height: size))
    }
}

/// A ticker the test advances by hand. Nothing is scheduled on a run loop, and
/// `fire()` after `stop()` does nothing, which is the invariant a closed menu
/// depends on.
@MainActor
private final class FakeTicker: MenuTicking {
    private(set) var isRunning = false
    private(set) var intervals: [TimeInterval] = []
    private var tick: (() -> Void)?

    var interval: TimeInterval? { intervals.last }

    func start(interval: TimeInterval, tick: @escaping () -> Void) {
        isRunning = true
        intervals.append(interval)
        self.tick = tick
    }

    func stop() {
        isRunning = false
        tick = nil
    }

    func fire() {
        guard isRunning else { return }
        tick?()
    }
}

@MainActor
final class MenuBarTests: XCTestCase {
    private let origin = Origin("http://127.0.0.1:8443")!
    private let now = Date(timeIntervalSince1970: 1_800_000_000)

    // MARK: - Connection state

    func testConnectionStateStartsDisconnectedAndReportsTheCanonicalOrigin() {
        let status = AppSessionStatus()
        XCTAssertEqual(status.connection, .disconnected)
        XCTAssertFalse(status.isConnected)

        status.reportConnected(origin)
        XCTAssertEqual(status.connection, .connected("http://127.0.0.1:8443"))
        XCTAssertTrue(status.isConnected)

        status.reportDisconnected()
        XCTAssertEqual(status.connection, .disconnected)
        XCTAssertFalse(status.isConnected)
    }

    func testConnectionStateUsesTheExactOriginRatherThanTheProposedCandidate() {
        let otherPort = Origin("http://127.0.0.1:9443")!
        let https = Origin("https://example.com")!
        let status = AppSessionStatus()

        status.reportConnected(otherPort)
        XCTAssertEqual(status.connection, .connected("http://127.0.0.1:9443"))
        status.reportConnected(https)
        XCTAssertEqual(status.connection, .connected("https://example.com:443"))
    }

    // MARK: - Menu bar lifecycle

    func testStartCreatesOneMenuBarItemAndASecondStartIsIdempotent() throws {
        let factory = FakeStatusItemFactory()
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: factory,
            openWindow: {},
            quit: {}
        )

        controller.start()
        controller.start()

        XCTAssertEqual(factory.items.count, 1)
        XCTAssertEqual(
            factory.requestedLengths,
            [NSStatusItem.variableLength],
            "The tray item must fit the attention count beside the face."
        )
        let item = try XCTUnwrap(controller.statusItem as? FakeStatusItem)
        XCTAssertTrue(item === factory.items[0])
        XCTAssertTrue(item.isVisible)
        XCTAssertEqual(item.toolTip, MenuBarController.toolTip)
        XCTAssertTrue(item.menu === controller.menu)
        XCTAssertNotNil(controller.menu)
    }

    func testMenuItemUsesTheBundledTemplateArtwork() throws {
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            image: AppAssets.menuBarImage,
            openWindow: {},
            quit: {}
        )
        controller.start()

        let item = try XCTUnwrap(controller.statusItem as? FakeStatusItem)
        let image = try XCTUnwrap(item.image, "The tray icon must be packaged with the client.")
        XCTAssertTrue(image.isTemplate, "A non-template image would not receive the macOS menu-bar tint.")
        XCTAssertEqual(image.size, NSSize(width: 18, height: 18))
    }

    func testControllerForcesATemplateImageOfTheExpectedPointSize() throws {
        // Even an untemplated source image must never be drawn in its original
        // colours in the menu bar.
        let source = NSImage(size: NSSize(width: 40, height: 40))
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            image: source,
            openWindow: {},
            quit: {}
        )
        controller.start()

        let item = try XCTUnwrap(controller.statusItem as? FakeStatusItem)
        let image = try XCTUnwrap(item.image)
        XCTAssertTrue(image.isTemplate)
        XCTAssertEqual(image.size, NSSize(width: 18, height: 18))
        XCTAssertFalse(source.isTemplate, "The packaged image must not be mutated in place.")
        XCTAssertEqual(source.size, NSSize(width: 40, height: 40))
    }

    func testStartWithoutArtworkStillProducesAVisibleFallbackItem() throws {
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {}
        )
        controller.start()

        let item = try XCTUnwrap(controller.statusItem as? FakeStatusItem)
        XCTAssertNil(item.image)
        XCTAssertEqual(item.title, MenuBarController.fallbackTitle)
        XCTAssertNotNil(item.menu)
    }

    /// Short-lived by design: this is the only test that touches the real
    /// `NSStatusBar`, and it removes the item before returning.
    func testSystemFactoryCreatesAButtonBackedItemThatCanBeRemoved() {
        let item = SystemStatusItemFactory().makeStatusItem(length: NSStatusItem.squareLength)
        defer { item.remove() }

        // Reading values back proves the status item exposes a real button,
        // which is where the controller installs the template artwork.
        item.title = "probe"
        XCTAssertEqual(item.title, "probe")
        item.toolTip = "Ciaobot"
        XCTAssertEqual(item.toolTip, "Ciaobot")

        let menu = NSMenu()
        item.menu = menu
        XCTAssertTrue(item.menu === menu)

        let image = NSImage(size: NSSize(width: 18, height: 18))
        item.image = image
        XCTAssertTrue(item.image === image)

        item.isVisible = true
        XCTAssertTrue(item.isVisible)
    }

    func testStopRemovesTheStatusItemAndCanBeStartedAgain() throws {
        let factory = FakeStatusItemFactory()
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: factory,
            openWindow: {},
            quit: {}
        )

        controller.start()
        let first = try XCTUnwrap(controller.statusItem as? FakeStatusItem)
        controller.stop()

        XCTAssertEqual(first.removeCount, 1)
        XCTAssertNil(controller.statusItem)
        XCTAssertNil(controller.menu)

        controller.start()
        XCTAssertEqual(factory.items.count, 2)
        let second = try XCTUnwrap(controller.statusItem as? FakeStatusItem)
        XCTAssertTrue(second === factory.items[1])
        XCTAssertEqual(second.removeCount, 0)
    }

    // MARK: - Menu contents and actions

    func testMenuContainsOnlyTheTruthfulStatusOpenAndQuit() throws {
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {}
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        XCTAssertFalse(menu.autoenablesItems, "The static tray menu must keep its explicit enabled state.")
        XCTAssertEqual(menu.items.filter { !$0.isSeparatorItem }.map(\.title), [
            MenuBarController.disconnectedTitle,
            MenuBarController.openTitle,
            MenuBarController.quitTitle,
        ])
        // No chat list, unread badge, notification or server lifecycle entry.
        XCTAssertEqual(menu.items.count, 5)

        let statusEntry = try XCTUnwrap(menu.items.first)
        XCTAssertFalse(statusEntry.isEnabled)
        XCTAssertNil(statusEntry.action)
    }

    func testOpenItemCallsOnlyTheOpenHandler() throws {
        var openCount = 0
        var quitCount = 0
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: { openCount += 1 },
            quit: { quitCount += 1 }
        )
        controller.start()

        let openItem = try XCTUnwrap(controller.menu?.item(withTitle: MenuBarController.openTitle))
        XCTAssertTrue(openItem.isEnabled)
        XCTAssertTrue(openItem.target === controller)
        XCTAssertEqual(openItem.action, #selector(MenuBarController.openCiaobot(_:)))

        controller.openCiaobot(openItem)

        XCTAssertEqual(openCount, 1)
        XCTAssertEqual(quitCount, 0, "Opening the window must never quit the app.")
    }

    func testQuitItemCallsOnlyTheQuitHandler() throws {
        var openCount = 0
        var quitCount = 0
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: { openCount += 1 },
            quit: { quitCount += 1 }
        )
        controller.start()

        let quitItem = try XCTUnwrap(controller.menu?.item(withTitle: MenuBarController.quitTitle))
        XCTAssertTrue(quitItem.isEnabled)
        XCTAssertTrue(quitItem.target === controller)
        XCTAssertEqual(quitItem.action, #selector(MenuBarController.quitCiaobot(_:)))

        controller.quitCiaobot(quitItem)

        XCTAssertEqual(quitCount, 1)
        XCTAssertEqual(openCount, 0, "Quitting must not reopen the window.")
    }

    func testStatusTitleTracksTheConnectionAndStopsUpdatingAfterStop() throws {
        let status = AppSessionStatus()
        let controller = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {}
        )
        controller.start()

        XCTAssertEqual(controller.statusTitle, MenuBarController.disconnectedTitle)

        status.reportConnected(origin)
        XCTAssertEqual(controller.statusTitle, "Connected to http://127.0.0.1:8443")

        status.reportDisconnected()
        XCTAssertEqual(controller.statusTitle, MenuBarController.disconnectedTitle)

        status.reportConnected(origin)
        controller.stop()
        XCTAssertNil(controller.statusTitle)
        // A late connection update after teardown must not resurrect the item.
        status.reportDisconnected()
        XCTAssertNil(controller.statusItem)
    }

    // MARK: - Reopening the window

    func testMainWindowPresenterActivatesAndReordersTheMainWindow() {
        let window = SpyWindow(
            contentRect: NSRect(x: 0, y: 0, width: 200, height: 200),
            styleMask: [.titled],
            backing: .buffered,
            defer: true
        )
        window.identifier = MainWindowPresenter.mainWindowIdentifier
        var activated = 0
        let presenter = MainWindowPresenter(
            windows: { [window] },
            activate: { activated += 1 }
        )

        XCTAssertTrue(presenter.present())
        XCTAssertEqual(activated, 1)
        XCTAssertEqual(window.orderFrontCount, 1)
    }

    func testMainWindowPresenterIgnoresOtherWindowsAndReportsAMissingMainWindow() {
        let child = NSWindow()
        child.identifier = NSUserInterfaceItemIdentifier("child")
        child.title = "Ciaobot"
        var activated = 0
        let presenter = MainWindowPresenter(
            windows: { [child] },
            activate: { activated += 1 }
        )

        XCTAssertFalse(presenter.present())
        XCTAssertEqual(activated, 1, "The app should still be activated when no window can be found.")
        XCTAssertNil(MainWindowPresenter.mainWindow(in: [child]))
    }

    func testApplicationLaunchStartsTheMenuBarAndOpenRoutesToThePresenter() {
        _ = NSApplication.shared
        let presenter = FakeMainWindowPresenter()
        let menuBar = FakeMenuBarController()
        var capturedOpen: (() -> Void)?
        let delegate = AppDelegate(
            session: AppSessionStatus(),
            presenter: presenter,
            makeMenuBar: { _, _, _, open, _ in
                capturedOpen = open
                return menuBar
            }
        )

        delegate.applicationDidFinishLaunching(
            Notification(name: NSApplication.didFinishLaunchingNotification)
        )
        XCTAssertEqual(menuBar.startCount, 1, "The menu bar must exist from app launch.")

        capturedOpen?()
        XCTAssertEqual(presenter.presentCount, 1, "Open Ciaobot must use the window presenter.")

        delegate.applicationWillTerminate(
            Notification(name: NSApplication.willTerminateNotification)
        )
        XCTAssertEqual(menuBar.stopCount, 1)
    }

    /// The updater is started once at launch, before the menu exists, and the
    /// menu receives that same retained object. Nothing here starts Sparkle:
    /// the double only records the calls.
    func testApplicationLaunchStartsTheRetainedUpdaterAndHandsItToTheMenuBar() {
        _ = NSApplication.shared
        let updater = FakeClientUpdater()
        let menuBar = FakeMenuBarController()
        var handedUpdater: ClientUpdateControlling?
        let delegate = AppDelegate(
            session: AppSessionStatus(),
            presenter: FakeMainWindowPresenter(),
            updater: updater,
            makeMenuBar: { _, updates, _, _, _ in
                handedUpdater = updates
                return menuBar
            }
        )

        delegate.applicationDidFinishLaunching(
            Notification(name: NSApplication.didFinishLaunchingNotification)
        )

        XCTAssertEqual(updater.startCount, 1, "Launch starts the retained updater exactly once.")
        XCTAssertTrue(handedUpdater === updater, "The menu must see the same retained updater.")
        XCTAssertEqual(menuBar.startCount, 1)

        delegate.applicationWillTerminate(
            Notification(name: NSApplication.willTerminateNotification)
        )
        XCTAssertEqual(updater.startCount, 1, "Termination must not start another updater.")
    }

    /// The test initializer defaults to no updater, so an ordinary test launch
    /// cannot start Sparkle or contact the feed.
    func testAppDelegateWithoutAnUpdaterStaysInert() {
        _ = NSApplication.shared
        var handedUpdater: ClientUpdateControlling?
        let delegate = AppDelegate(
            session: AppSessionStatus(),
            presenter: FakeMainWindowPresenter(),
            makeMenuBar: { _, updates, _, _, _ in
                handedUpdater = updates
                return FakeMenuBarController()
            }
        )

        delegate.applicationDidFinishLaunching(
            Notification(name: NSApplication.didFinishLaunchingNotification)
        )

        XCTAssertNil(handedUpdater)
    }

    /// The monitor is created once by the app and handed to the menu bar as its
    /// third factory argument, so the menu and the factory see the same object.
    func testApplicationLaunchHandsTheSharedEngineUpdatesToTheMenuBar() {
        _ = NSApplication.shared
        let engine = FakeEngineUpdateMenu()
        var handed: EngineUpdateMenuProviding?
        let delegate = AppDelegate(
            session: AppSessionStatus(),
            presenter: FakeMainWindowPresenter(),
            engineUpdates: engine,
            makeMenuBar: { _, _, engineUpdates, _, _ in
                handed = engineUpdates
                return FakeMenuBarController()
            }
        )

        delegate.applicationDidFinishLaunching(
            Notification(name: NSApplication.didFinishLaunchingNotification)
        )

        XCTAssertTrue(handed === engine)
    }

    /// The connection screen and the menu share one updater, so a test can
    /// inject it and see the app keep that exact instance.
    func testAnInjectedEngineUpdaterIsTheOneTheAppShares() {
        let updater = EngineUpdater(run: { _, _, _, _ in }, after: { _, _ in }, isExecutable: { _ in true })
        let delegate = AppDelegate(
            session: AppSessionStatus(),
            presenter: FakeMainWindowPresenter(),
            engineUpdater: updater
        )

        XCTAssertTrue(delegate.engineUpdater === updater)
    }

    /// The test initializer creates no monitor, so no test launch can probe
    /// the engine or offer an update.
    func testTheTestAppDelegateHasNoEngineUpdateMonitor() {
        _ = NSApplication.shared
        var handed: EngineUpdateMenuProviding?
        let delegate = AppDelegate(
            session: AppSessionStatus(),
            presenter: FakeMainWindowPresenter(),
            makeMenuBar: { _, _, engineUpdates, _, _ in
                handed = engineUpdates
                return FakeMenuBarController()
            }
        )

        delegate.applicationDidFinishLaunching(
            Notification(name: NSApplication.didFinishLaunchingNotification)
        )

        XCTAssertNil(handed)
    }

    // MARK: - Engine update rows

    private func makeEngineController(_ engine: FakeEngineUpdateMenu) -> MenuBarController {
        MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            engineUpdates: engine
        )
    }

    /// The item directly below Open Ciaobot, which is where the engine rows sit.
    private func itemBelowOpen(in menu: NSMenu) -> NSMenuItem {
        let index = menu.items.firstIndex { $0.title == MenuBarController.openTitle }!
        return menu.items[index + 1]
    }

    func testEngineUpdateRowSitsDirectlyBelowOpen() {
        let target = EngineUpdateTarget(executable: URL(fileURLWithPath: "/Users/me/.local/bin/ciao"), version: "1.8.0")
        let engine = FakeEngineUpdateMenu()
        engine.rows = [.action(title: "Update Ciaobot to 1.8.0…", target: target)]
        let controller = makeEngineController(engine)
        controller.start()

        let row = itemBelowOpen(in: controller.menu!)

        XCTAssertEqual(row.title, "Update Ciaobot to 1.8.0…")
        XCTAssertTrue(row.isEnabled)
        XCTAssertEqual(row.toolTip, EngineUpdateMenuCopy.actionToolTip)
        XCTAssertTrue(row.target === controller)
    }

    func testClickingTheEngineUpdateRowRunsTheUpdate() {
        let target = EngineUpdateTarget(executable: URL(fileURLWithPath: "/Users/me/.local/bin/ciao"), version: "1.8.0")
        let engine = FakeEngineUpdateMenu()
        engine.rows = [.action(title: "Update Ciaobot to 1.8.0…", target: target)]
        let controller = makeEngineController(engine)
        controller.start()
        let row = itemBelowOpen(in: controller.menu!)

        controller.updateEngine(row)

        XCTAssertEqual(engine.performCount, 1)
    }

    func testEngineUpdateProgressAndOutcomeRowsAreDisabled() {
        let outcome = FakeEngineUpdateMenu()
        outcome.rows = [.outcome(title: "boom", toolTip: "boom long", failed: true)]
        let outcomeController = makeEngineController(outcome)
        outcomeController.start()

        let outcomeRow = itemBelowOpen(in: outcomeController.menu!)
        XCTAssertEqual(outcomeRow.title, "boom")
        XCTAssertFalse(outcomeRow.isEnabled)
        XCTAssertEqual(outcomeRow.toolTip, "boom long")

        let progress = FakeEngineUpdateMenu()
        progress.rows = [.progress("Installing Ciaobot 1.8.0…")]
        let progressController = makeEngineController(progress)
        progressController.start()

        let progressRow = itemBelowOpen(in: progressController.menu!)
        XCTAssertEqual(progressRow.title, "Installing Ciaobot 1.8.0…")
        XCTAssertFalse(progressRow.isEnabled)
    }

    func testNoEngineUpdateRowsLeaveTheMenuUnchanged() {
        let engine = FakeEngineUpdateMenu()
        engine.rows = []
        let controller = makeEngineController(engine)
        controller.start()

        XCTAssertTrue(itemBelowOpen(in: controller.menu!).isSeparatorItem)
    }

    func testTheMenuStartsAndStopsTheEngineUpdateProvider() {
        let engine = FakeEngineUpdateMenu()
        let controller = makeEngineController(engine)

        controller.start()
        XCTAssertEqual(engine.startCount, 1)

        controller.stop()
        XCTAssertEqual(engine.stopCount, 1)
    }

    func testOpeningTheMenuRefreshesTheEngineOffer() {
        let engine = FakeEngineUpdateMenu()
        let controller = makeEngineController(engine)
        controller.start()

        controller.menuWillOpen(controller.menu!)

        XCTAssertEqual(engine.refreshCount, 1)
        controller.menuDidClose(controller.menu!)
    }

    func testClosingAfterShowingAnOutcomeAcknowledgesItOnce() {
        let engine = FakeEngineUpdateMenu()
        engine.rows = [.outcome(title: "Ciaobot was updated to 1.8.0.", toolTip: "Ciaobot was updated to 1.8.0.", failed: false)]
        let controller = makeEngineController(engine)
        controller.start()
        let menu = controller.menu!

        controller.menuWillOpen(menu)
        controller.menuDidClose(menu)
        XCTAssertEqual(engine.acknowledgeCount, 1)

        engine.rows = []
        controller.menuWillOpen(menu)
        controller.menuDidClose(menu)
        XCTAssertEqual(engine.acknowledgeCount, 1, "A later close with no outcome shown must not acknowledge again.")
    }

    func testClosingWithoutAnOutcomeDoesNotAcknowledge() {
        let engine = FakeEngineUpdateMenu()
        engine.rows = [.action(title: "Update Ciaobot to 1.8.0…", target: EngineUpdateTarget(
            executable: URL(fileURLWithPath: "/Users/me/.local/bin/ciao"), version: "1.8.0"))]
        let controller = makeEngineController(engine)
        controller.start()
        let menu = controller.menu!

        controller.menuWillOpen(menu)
        controller.menuDidClose(menu)

        XCTAssertEqual(engine.acknowledgeCount, 0)
    }

    func testAnEngineUpdateChangeRebuildsTheClosedMenu() {
        let engine = FakeEngineUpdateMenu()
        let controller = makeEngineController(engine)
        controller.start()
        let menu = controller.menu!
        XCTAssertFalse(menu.items.contains { $0.title == "Update Ciaobot to 1.8.0…" })

        engine.rows = [.action(title: "Update Ciaobot to 1.8.0…", target: EngineUpdateTarget(
            executable: URL(fileURLWithPath: "/Users/me/.local/bin/ciao"), version: "1.8.0"))]
        engine.subject.send(())
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))

        XCTAssertEqual(itemBelowOpen(in: menu).title, "Update Ciaobot to 1.8.0…")
    }

    func testAppDelegateDoesNotTerminateWhenTheLastWindowCloses() {        let delegate = AppDelegate(
            session: AppSessionStatus(),
            presenter: FakeMainWindowPresenter()
        )
        XCTAssertFalse(delegate.applicationShouldTerminateAfterLastWindowClosed(NSApplication.shared),
                       "Closing the window must leave the tray app running.")
    }

    func testAppDelegateReopensOnlyWhenNoWindowIsVisible() {
        let presenter = FakeMainWindowPresenter()
        let delegate = AppDelegate(session: AppSessionStatus(), presenter: presenter)

        XCTAssertTrue(delegate.applicationShouldHandleReopen(NSApplication.shared, hasVisibleWindows: true))
        XCTAssertEqual(presenter.presentCount, 0, "A visible window must not be re-presented.")

        XCTAssertTrue(delegate.applicationShouldHandleReopen(NSApplication.shared, hasVisibleWindows: false))
        XCTAssertEqual(presenter.presentCount, 1)
    }

    // MARK: - Notification opt-in row

    func testNotificationRowOffersOptInAndOnlyTheClickActs() throws {
        let notifications = FakeNotificationMenu(.off)
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            notifications: notifications
        )
        controller.start()

        let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.notificationsOffTitle))
        XCTAssertTrue(item.isEnabled)
        XCTAssertTrue(item.target === controller)
        XCTAssertEqual(item.action, #selector(MenuBarController.toggleNotifications(_:)))
        XCTAssertEqual(notifications.actionCount, 0, "Showing the row must not request permission.")
        XCTAssertEqual(notifications.refreshCount, 0)

        controller.toggleNotifications(item)
        XCTAssertEqual(notifications.actionCount, 1)
    }

    func testNotificationRowShowsTheEnabledStateAndOffersToTurnOff() throws {
        let notifications = FakeNotificationMenu(.on)
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            notifications: notifications
        )
        controller.start()

        let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.notificationsOnTitle))
        XCTAssertTrue(item.isEnabled)
        XCTAssertEqual(item.state, .on)
        XCTAssertEqual(item.action, #selector(MenuBarController.toggleNotifications(_:)))

        controller.toggleNotifications(item)
        XCTAssertEqual(notifications.actionCount, 1)

        notifications.set(.off)
        RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        XCTAssertNotNil(controller.menu?.deepItem(withTitle: MenuBarController.notificationsOffTitle))
        XCTAssertNil(controller.menu?.deepItem(withTitle: MenuBarController.notificationsOnTitle))
    }

    func testNotificationRowDoesNotSayOnUntilTheEventStreamIsReady() throws {
        for (stream, title) in [
            (ChatNotificationStreamStatus.notConnected, MenuBarController.notificationsWaitingTitle),
            (.waitingForAuthentication, MenuBarController.notificationsSignInTitle),
            (.connecting, MenuBarController.notificationsConnectingTitle),
            (.retrying, MenuBarController.notificationsRetryingTitle),
        ] {
            let notifications = FakeNotificationMenu(.on, stream: stream)
            let controller = MenuBarController(
                status: AppSessionStatus(),
                statusItemFactory: FakeStatusItemFactory(),
                openWindow: {},
                quit: {},
                notifications: notifications
            )
            controller.start()

            let item = try XCTUnwrap(controller.menu?.deepItem(withTitle: title))
            XCTAssertTrue(item.isEnabled, "The row must stay clickable so notifications can be turned off.")
            XCTAssertEqual(item.state, .on)
            XCTAssertNil(
                controller.menu?.deepItem(withTitle: MenuBarController.notificationsOnTitle),
                "A plain On row would claim an authenticated stream that is not ready."
            )
        }
    }

    func testNotificationRowBecomesPlainOnAfterTheSnapshot() throws {
        let notifications = FakeNotificationMenu(.on, stream: .connecting)
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            notifications: notifications
        )
        controller.start()
        XCTAssertNotNil(controller.menu?.deepItem(withTitle: MenuBarController.notificationsConnectingTitle))

        notifications.setStream(.ready)
        RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        let ready = try XCTUnwrap(controller.menu?.deepItem(withTitle: MenuBarController.notificationsOnTitle))
        XCTAssertEqual(ready.state, .on)
    }

    func testBlockedNotificationRowOpensSettingsAndUnavailableRowStaysDisabled() throws {
        let blockedSettings = FakeSystemSettings()
        let blocked = FakeNotificationMenu(.denied)
        let blockedController = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            notifications: blocked,
            systemSettings: blockedSettings
        )
        blockedController.start()

        let blockedItem = try XCTUnwrap(
            blockedController.menu?.deepItem(withTitle: MenuBarController.notificationsBlockedTitle)
        )
        XCTAssertTrue(blockedItem.isEnabled)
        XCTAssertTrue(blockedItem.target === blockedController)
        XCTAssertEqual(blockedItem.action, #selector(MenuBarController.openNotificationSettings(_:)))

        blockedController.openNotificationSettings(blockedItem)
        XCTAssertEqual(blockedSettings.notificationSettingsCount, 1)
        XCTAssertEqual(blockedSettings.loginItemsSettingsCount, 0)

        let unavailable = FakeNotificationMenu(.unavailable(.unbundled))
        let unavailableController = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            notifications: unavailable,
            systemSettings: FakeSystemSettings()
        )
        unavailableController.start()

        let unavailableItem = try XCTUnwrap(
            unavailableController.menu?.deepItem(withTitle: MenuBarController.notificationsUnbundledTitle)
        )
        XCTAssertFalse(unavailableItem.isEnabled)
        XCTAssertNil(unavailableItem.action)
        XCTAssertTrue(unavailableItem.toolTip?.contains("bundled") == true)
    }

    func testMenuOpenRefreshesNotificationsWithoutAnActionAndStopEndsTheController() {
        let notifications = FakeNotificationMenu(.on)
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            notifications: notifications
        )
        controller.start()

        let menu = controller.menu!
        controller.menuWillOpen(menu)
        XCTAssertEqual(notifications.refreshCount, 1)
        XCTAssertEqual(notifications.actionCount, 0, "Opening the menu must not opt in.")
        controller.menuDidClose(menu)

        controller.stop()
        XCTAssertEqual(notifications.stopCount, 1)
    }

    func testDeliveryOffRowNamesTheBlockedChannelsAndOpensSettings() throws {
        let settings = FakeSystemSettings()
        let notifications = FakeNotificationMenu(.deliveryOff)
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            notifications: notifications,
            systemSettings: settings
        )
        controller.start()

        XCTAssertTrue(MenuBarController.notificationsDeliveryOffTitle.contains("allowed but turned off"))
        XCTAssertTrue(MenuBarController.notificationsDeliveryOffTitle.contains("Open Settings"))
        XCTAssertNotEqual(
            MenuBarController.notificationsDeliveryOffTitle,
            MenuBarController.notificationsOnTitle,
            "An allowed-but-blocked snapshot must never read as On."
        )

        let item = try XCTUnwrap(
            controller.menu?.deepItem(withTitle: MenuBarController.notificationsDeliveryOffTitle)
        )
        XCTAssertTrue(item.isEnabled)
        XCTAssertTrue(item.target === controller)
        XCTAssertEqual(item.action, #selector(MenuBarController.openNotificationSettings(_:)))
        let toolTip = try XCTUnwrap(item.toolTip)
        XCTAssertTrue(toolTip.contains("Notifications"))
        XCTAssertTrue(toolTip.contains("banners"))
        XCTAssertTrue(toolTip.contains("Focus"))
        XCTAssertTrue(toolTip.contains("cannot detect Focus"))

        controller.openNotificationSettings(item)
        XCTAssertEqual(settings.notificationSettingsCount, 1)
        XCTAssertEqual(settings.loginItemsSettingsCount, 0)
        XCTAssertEqual(notifications.actionCount, 0, "Rendering the row must not request permission.")
    }

    func testSettingsActionNeverOpensForStatesThatAreNotBlocked() throws {
        let onSettings = FakeSystemSettings()
        let onNotifications = FakeNotificationMenu(.on)
        let onController = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            notifications: onNotifications,
            systemSettings: onSettings
        )
        onController.start()
        XCTAssertNotNil(onController.menu?.deepItem(withTitle: MenuBarController.notificationsOnTitle))

        let offSettings = FakeSystemSettings()
        let offNotifications = FakeNotificationMenu(.off)
        let offController = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            notifications: offNotifications,
            systemSettings: offSettings
        )
        offController.start()
        XCTAssertNotNil(offController.menu?.deepItem(withTitle: MenuBarController.notificationsOffTitle))

        let unavailableSettings = FakeSystemSettings()
        let unavailableNotifications = FakeNotificationMenu(.unavailable(.unbundled))
        let unavailableController = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            notifications: unavailableNotifications,
            systemSettings: unavailableSettings
        )
        unavailableController.start()
        XCTAssertNotNil(
            unavailableController.menu?.deepItem(withTitle: MenuBarController.notificationsUnbundledTitle)
        )

        onController.openNotificationSettings(nil)
        offController.openNotificationSettings(nil)
        unavailableController.openNotificationSettings(nil)

        XCTAssertEqual(onSettings.notificationSettingsCount, 0)
        XCTAssertEqual(offSettings.notificationSettingsCount, 0)
        XCTAssertEqual(unavailableSettings.notificationSettingsCount, 0)
    }

    // MARK: - Dock badge

    /// The Dock badge is the same attention count and the same loaded snapshot
    /// as the menu-bar badge. The recording double stands in for
    /// `NSApp.dockTile`, so no test touches a real Dock tile.
    func testDockBadgeMirrorsTheLoadedAttentionCount() {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let dockBadge = FakeDockBadge()
        let controller = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            image: NSImage(size: NSSize(width: 40, height: 40)),
            openWindow: {},
            quit: {},
            chatList: FakeChatList(.loaded(dockSnapshot(count: 2))),
            dockBadge: dockBadge,
            now: { self.now }
        )
        controller.start()

        XCTAssertEqual(controller.statusItem?.title, "2")
        XCTAssertEqual(dockBadge.currentLabel, "2", "The Dock badge must be the menu bar's count.")
        XCTAssertEqual(dockBadge.appliedNumbers, ["2"], "One snapshot paints one Dock number.")
    }

    func testDockBadgeClearsWhenTheAttentionCountDropsToZero() {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let chatList = FakeChatList(.loaded(dockSnapshot(count: 3)))
        let dockBadge = FakeDockBadge()
        let controller = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            image: NSImage(size: NSSize(width: 40, height: 40)),
            openWindow: {},
            quit: {},
            chatList: chatList,
            dockBadge: dockBadge,
            now: { self.now }
        )
        controller.start()
        XCTAssertEqual(dockBadge.currentLabel, "3")

        chatList.set(.loaded(dockSnapshot(count: 0)))
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))

        XCTAssertEqual(controller.statusItem?.title, "")
        XCTAssertNil(dockBadge.currentLabel, "Zero attention must clear the Dock badge.")
        XCTAssertEqual(dockBadge.appliedNumbers, ["3"], "The old number is cleared, not repainted.")
    }

    func testDockBadgeClearsOnDisconnectAndDropsALateSnapshot() {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let chatList = FakeChatList(.loaded(dockSnapshot(count: 4)))
        let dockBadge = FakeDockBadge()
        let controller = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            image: NSImage(size: NSSize(width: 40, height: 40)),
            openWindow: {},
            quit: {},
            chatList: chatList,
            dockBadge: dockBadge,
            now: { self.now }
        )
        controller.start()
        XCTAssertEqual(dockBadge.currentLabel, "4")

        chatList.set(.loaded(dockSnapshot(count: 5)))
        status.reportDisconnected()

        XCTAssertNil(dockBadge.currentLabel, "A disconnect clears the Dock badge before the deferred rebuild.")
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertNil(dockBadge.currentLabel, "The old origin's count never returns after a disconnect.")

        chatList.set(.loaded(dockSnapshot(count: 6)))
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertNil(dockBadge.currentLabel, "A late snapshot cannot paint a badge while disconnected.")
    }

    func testDockBadgeClearsWhenTheChatListIsUnavailableOrLoading() {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let chatList = FakeChatList(.loaded(dockSnapshot(count: 7)))
        let dockBadge = FakeDockBadge()
        let controller = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            image: NSImage(size: NSSize(width: 40, height: 40)),
            openWindow: {},
            quit: {},
            chatList: chatList,
            dockBadge: dockBadge,
            now: { self.now }
        )
        controller.start()
        XCTAssertEqual(dockBadge.currentLabel, "7")

        for state in [ChatListState.unavailable(.notSignedIn), .loading, .disconnected] {
            chatList.set(.loaded(dockSnapshot(count: 8)))
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
            XCTAssertEqual(dockBadge.currentLabel, "8")

            chatList.set(state)
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
            XCTAssertNil(dockBadge.currentLabel, "No Dock number for \(state).")
        }
    }

    func testDockBadgeStaysEmptyBeforeTheFirstSnapshotLoads() {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let chatList = FakeChatList(.loading)
        let dockBadge = FakeDockBadge()
        let controller = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            image: NSImage(size: NSSize(width: 40, height: 40)),
            openWindow: {},
            quit: {},
            chatList: chatList,
            dockBadge: dockBadge,
            now: { self.now }
        )
        controller.start()

        XCTAssertTrue(dockBadge.appliedNumbers.isEmpty, "No Dock number before a snapshot decides one.")

        chatList.set(.loaded(dockSnapshot(count: 1)))
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertEqual(dockBadge.currentLabel, "1")
    }

    func testDockBadgeStaysEmptyWhenThereIsNoChatListAtAll() {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let dockBadge = FakeDockBadge()
        let controller = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            image: NSImage(size: NSSize(width: 40, height: 40)),
            openWindow: {},
            quit: {},
            dockBadge: dockBadge,
            now: { self.now }
        )
        controller.start()

        XCTAssertNil(dockBadge.currentLabel, "An unloaded feed never paints a Dock number.")
        XCTAssertTrue(dockBadge.appliedNumbers.isEmpty)
    }

    // MARK: - Start at Login diagnostics

    func testUnavailableLoginItemRowNamesTheCauseAndExplainsInsteadOfRegistering() throws {
        let cause = LoginItemUnavailableCause.notInApplications
        let loginItem = FakeLoginItem(
            .unavailable(
                LoginItemUnavailability(cause: cause, reason: LoginItemUnavailableCopy.explanation(for: cause))
            )
        )
        var explained: [LoginItemUnavailability] = []
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            loginItem: loginItem,
            systemSettings: FakeSystemSettings(),
            loginItemExplanation: { explained.append($0) }
        )
        controller.start()

        let row = try XCTUnwrap(
            controller.menu?.deepItem(withTitle: LoginItemUnavailableCopy.title(for: cause))
        )
        XCTAssertTrue(row.isEnabled, "The row is clickable so it can name the one next step.")
        XCTAssertEqual(row.action, #selector(MenuBarController.explainLoginItemUnavailable(_:)))
        XCTAssertNotEqual(row.action, #selector(MenuBarController.toggleLoginItem(_:)))
        XCTAssertNotEqual(row.state, .on, "An unavailable copy never claims Start at Login is on.")
        XCTAssertEqual(row.toolTip, LoginItemUnavailableCopy.explanation(for: cause))
        XCTAssertNil(controller.menu?.deepItem(withTitle: MenuBarController.loginItemTitle))

        controller.explainLoginItemUnavailable(row)

        XCTAssertEqual(explained.count, 1)
        XCTAssertEqual(explained.first?.cause, cause)
        XCTAssertEqual(loginItem.registerCount, 0, "A diagnostic row never registers a login item.")
        XCTAssertEqual(loginItem.unregisterCount, 0)
    }

    func testAdHocLoginItemRowExplainsWithoutOpeningSettings() throws {
        let settings = FakeSystemSettings()
        var explained: [LoginItemUnavailability] = []
        let cause = LoginItemUnavailableCause.unsignedOrAdHoc
        let loginItem = FakeLoginItem(
            .unavailable(
                LoginItemUnavailability(cause: cause, reason: LoginItemUnavailableCopy.explanation(for: cause))
            )
        )
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            loginItem: loginItem,
            systemSettings: settings,
            loginItemExplanation: { explained.append($0) }
        )
        controller.start()

        let row = try XCTUnwrap(
            controller.menu?.deepItem(withTitle: LoginItemUnavailableCopy.title(for: cause))
        )
        XCTAssertTrue(row.toolTip?.contains("Developer ID") == true)
        controller.explainLoginItemUnavailable(row)

        XCTAssertEqual(explained.count, 1)
        XCTAssertEqual(settings.loginItemsSettingsCount, 0, "Settings cannot sign a build.")
        XCTAssertEqual(loginItem.registerCount, 0)
    }

    func testUndiagnosedLoginItemRowStaysDisabledAndSaysNothingItCannotProve() throws {
        let reason = "macOS reported an unknown login-item status."
        let loginItem = FakeLoginItem(.unavailable(LoginItemUnavailability(reason: reason)))
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            loginItem: loginItem,
            loginItemExplanation: { _ in XCTFail("No explanation exists for an undiagnosed state.") }
        )
        controller.start()

        let row = try XCTUnwrap(
            controller.menu?.deepItem(withTitle: MenuBarController.loginItemUnavailableTitle)
        )
        XCTAssertFalse(row.isEnabled)
        XCTAssertNil(row.action)
        XCTAssertEqual(row.toolTip, reason)
        XCTAssertNotEqual(row.state, .on)
    }

    func testOpeningTheMenuRefreshesTheLoginItemWithoutRegisteringIt() throws {
        let loginItem = FakeLoginItem(
            .unavailable(
                LoginItemUnavailability(
                    cause: .unsignedOrAdHoc,
                    reason: LoginItemUnavailableCopy.explanation(for: .unsignedOrAdHoc)
                )
            )
        )
        let controller = MenuBarController(
            status: AppSessionStatus(),
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            loginItem: loginItem,
            loginItemExplanation: { _ in }
        )
        controller.start()

        controller.menuWillOpen(try XCTUnwrap(controller.menu))

        XCTAssertEqual(loginItem.refreshCount, 1, "Opening only re-reads the OS state.")
        XCTAssertEqual(loginItem.registerCount, 0)
        XCTAssertEqual(loginItem.unregisterCount, 0)
        XCTAssertNotNil(
            controller.menu?.deepItem(withTitle: LoginItemUnavailableCopy.title(for: .unsignedOrAdHoc))
        )
    }

    // MARK: - Working glyph animation and menu-open freshness

    func testAMenuTickRedrawsOnlyTheWorkingRow() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let renderer = FakeGlyphRenderer()
        let ticker = FakeTicker()
        var now = self.now
        let controller = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: FakeChatList(.loaded(workingSnapshot())),
            activity: FakeActivity(
                .live(origin: origin.canonicalKey, activeChatIDs: ["working", "needs"])
            ),
            now: { now },
            reduceMotion: { false },
            glyphRenderer: renderer,
            glyphTicker: ticker
        )
        controller.start()
        let menu = try XCTUnwrap(controller.menu)

        XCTAssertFalse(ticker.isRunning, "A closed menu must not animate.")
        XCTAssertEqual(renderer.calls.count, 1, "Only the working row uses the pixel grid.")
        XCTAssertEqual(renderer.calls.first?.litCell, WorkingGlyphFrame.litCell(at: 0))
        XCTAssertEqual(renderer.calls.first?.size, WorkingGlyphRenderer.pointSize)
        XCTAssertEqual(renderer.calls.first?.reduceMotion, false)

        controller.menuWillOpen(menu)
        XCTAssertTrue(ticker.isRunning)
        XCTAssertEqual(ticker.interval, WorkingGlyphFrame.step)

        let workingRow = try XCTUnwrap(menu.item(withTitle: "Working · now"))
        let needsRow = try XCTUnwrap(menu.item(withTitle: "Needs · now"))
        let unreadRow = try XCTUnwrap(menu.item(withTitle: "Unread · now"))
        let workingImage = workingRow.image
        let needsImage = needsRow.image
        let unreadImage = unreadRow.image
        let callsAtOpen = renderer.calls.count

        now = now.addingTimeInterval(WorkingGlyphFrame.step)
        ticker.fire()

        XCTAssertEqual(
            renderer.calls.count,
            callsAtOpen + 1,
            "One tick redraws one glyph per working row and nothing else."
        )
        XCTAssertEqual(renderer.calls.last?.litCell, WorkingGlyphFrame.litCell(at: WorkingGlyphFrame.step))
        XCTAssertFalse(workingRow.image === workingImage, "The working row advances to the next frame.")
        XCTAssertTrue(needsRow.image === needsImage, "A needs-you row keeps its alert triangle.")
        XCTAssertTrue(unreadRow.image === unreadImage, "An unread row keeps its eye.")
    }

    func testGlyphTickerStartsOnOpenAndStopsOnClose() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let renderer = FakeGlyphRenderer()
        let glyphTicker = FakeTicker()
        let refreshTicker = FakeTicker()
        let controller = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: FakeChatList(.loaded(workingSnapshot())),
            activity: FakeActivity(.live(origin: origin.canonicalKey, activeChatIDs: ["working"])),
            now: { self.now },
            reduceMotion: { false },
            glyphRenderer: renderer,
            glyphTicker: glyphTicker,
            menuRefreshTicker: refreshTicker
        )
        controller.start()
        let menu = try XCTUnwrap(controller.menu)
        XCTAssertFalse(glyphTicker.isRunning)
        XCTAssertFalse(refreshTicker.isRunning)

        controller.menuWillOpen(menu)
        XCTAssertTrue(glyphTicker.isRunning)
        XCTAssertTrue(refreshTicker.isRunning)

        controller.menuDidClose(menu)
        XCTAssertFalse(glyphTicker.isRunning, "Closing the menu stops the 0.15 s animation.")
        XCTAssertFalse(refreshTicker.isRunning, "Closing the menu stops the 5 s refresh.")

        let callsAtClose = renderer.calls.count
        glyphTicker.fire()
        refreshTicker.fire()
        XCTAssertEqual(renderer.calls.count, callsAtClose, "A stopped menu runs no ticker.")
    }

    func testStoppingTheControllerStopsEveryMenuTicker() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let glyphTicker = FakeTicker()
        let refreshTicker = FakeTicker()
        let controller = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: FakeChatList(.loaded(workingSnapshot())),
            activity: FakeActivity(.live(origin: origin.canonicalKey, activeChatIDs: ["working"])),
            now: { self.now },
            glyphRenderer: FakeGlyphRenderer(),
            glyphTicker: glyphTicker,
            menuRefreshTicker: refreshTicker
        )
        controller.start()
        controller.menuWillOpen(try XCTUnwrap(controller.menu))

        controller.stop()

        XCTAssertFalse(glyphTicker.isRunning)
        XCTAssertFalse(refreshTicker.isRunning)
    }

    func testReducedMotionDrawsTheCheckerAndNeverAdvances() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let renderer = FakeGlyphRenderer()
        let ticker = FakeTicker()
        let controller = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: FakeChatList(.loaded(workingSnapshot())),
            activity: FakeActivity(.live(origin: origin.canonicalKey, activeChatIDs: ["working"])),
            now: { self.now },
            reduceMotion: { true },
            glyphRenderer: renderer,
            glyphTicker: ticker
        )
        controller.start()
        let menu = try XCTUnwrap(controller.menu)

        XCTAssertEqual(renderer.calls.count, 1)
        XCTAssertEqual(renderer.calls.first?.reduceMotion, true, "Reduce Motion freezes the grid.")
        XCTAssertNil(renderer.calls.first?.litCell, "The checker does not carry a chase frame.")

        controller.menuWillOpen(menu)
        XCTAssertFalse(ticker.isRunning, "Reduced motion never advances frames.")
        XCTAssertTrue(renderer.calls.allSatisfy { $0.reduceMotion })
        XCTAssertTrue(renderer.calls.allSatisfy { $0.litCell == nil })
    }

    func testOpenMenuRefreshesEveryFiveSecondsAndAgesLabelsWithoutFetching() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        var now = self.now
        let chatList = FakeChatList(.loaded(timedSnapshot(lastActivityAt: now)))
        let refreshTicker = FakeTicker()
        let controller = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: chatList,
            now: { now },
            reduceMotion: { false },
            menuRefreshTicker: refreshTicker
        )
        controller.start()
        let menu = try XCTUnwrap(controller.menu)
        XCTAssertNotNil(menu.item(withTitle: "Chat · now"))
        XCTAssertFalse(refreshTicker.isRunning)

        controller.menuWillOpen(menu)
        XCTAssertEqual(chatList.refreshCount, 1)
        XCTAssertTrue(refreshTicker.isRunning)
        XCTAssertEqual(MenuBarController.menuRefreshInterval, 5)
        XCTAssertEqual(refreshTicker.interval, 5)

        // The fake feed never changes, so a new title can only come from the
        // rebuilt elapsed label, not from another network response.
        now = now.addingTimeInterval(60)
        refreshTicker.fire()

        XCTAssertEqual(chatList.refreshCount, 2, "One open-menu tick makes exactly one throttled refresh.")
        XCTAssertNotNil(menu.item(withTitle: "Chat · 1m"), "Elapsed labels age while the menu stays open.")
        XCTAssertNil(menu.item(withTitle: "Chat · now"))
    }

    func testAMenuOpenedWhileDisconnectedRunsNoRefreshTicker() throws {
        let status = AppSessionStatus()
        let chatList = FakeChatList(.disconnected)
        let refreshTicker = FakeTicker()
        let controller = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: chatList,
            now: { self.now },
            menuRefreshTicker: refreshTicker
        )
        controller.start()
        let menu = try XCTUnwrap(controller.menu)

        controller.menuWillOpen(menu)

        XCTAssertFalse(refreshTicker.isRunning, "Never poll while disconnected.")
        let refreshesAtOpen = chatList.refreshCount
        refreshTicker.fire()
        XCTAssertEqual(chatList.refreshCount, refreshesAtOpen)
    }

    // MARK: - Host fleet

    func testTwoHostsRenderTwoSectionsWithAHostHeaderEach() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let other = Origin("https://other.example.com")!
        let feeds = [
            HostFleetModel.HostFeeds(
                originKey: origin.canonicalKey,
                label: "Home",
                list: .loaded(fleetSnapshot(origin: origin, id: "home-1", count: 1)),
                activity: .inactive
            ),
            HostFleetModel.HostFeeds(
                originKey: other.canonicalKey,
                label: "Office",
                list: .loaded(fleetSnapshot(origin: other, id: "office-1", count: 2)),
                activity: .inactive
            ),
        ]
        let controller = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            fleet: FakeHostFleet(feeds: feeds, total: 3),
            openChat: { _ in true },
            now: { self.now }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        for header in ["Home", "Office"] {
            let item = try XCTUnwrap(menu.items.first { $0.title == header }, "\(header) needs a host header")
            XCTAssertFalse(item.isEnabled, "A host header is a label, not an action.")
            XCTAssertNil(item.action)
        }
        XCTAssertNotNil(row(menu, id: "home-1"))
        XCTAssertNotNil(row(menu, id: "office-1"))

        // One host keeps today's menu: no header row.
        let single = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            fleet: FakeHostFleet(feeds: [feeds[0]], total: 1),
            openChat: { _ in true },
            now: { self.now }
        )
        single.start()
        let singleMenu = try XCTUnwrap(single.menu)
        XCTAssertNil(singleMenu.items.first { $0.title == "Home" }, "A single host never grows a header")
        XCTAssertNotNil(row(singleMenu, id: "home-1"))
    }

    func testASingleHostFleetMenuMatchesTheLegacyMenuItemForItem() throws {
        let key = origin.canonicalKey
        let twoWorkspaces = ChatListSnapshot(
            origin: key,
            chats: [
                MenubarChat(chatID: "a", title: "Alpha", workspace: "work", lastActivityAt: iso(now),
                            unread: false, needsInput: true),
                MenubarChat(chatID: "b", title: "Beta", workspace: "personal", lastActivityAt: iso(now),
                            unread: true, needsInput: false),
            ],
            attentionCount: 2,
            fetchedAt: now
        )
        let empty = ChatListSnapshot(origin: key, chats: [], attentionCount: 0, fetchedAt: now)
        let cases: [(name: String, list: ChatListState, activity: ChatActivityState)] = [
            ("working rows with a live feed", .loaded(workingSnapshot()),
             .live(origin: key, activeChatIDs: ["working", "needs"])),
            ("rows across two workspaces", .loaded(twoWorkspaces), .inactive),
            ("rows with working status unavailable", .loaded(workingSnapshot()),
             .connecting(origin: key)),
            ("no chats", .loaded(empty), .inactive),
            ("loading", .loading, .inactive),
            ("not signed in", .unavailable(.notSignedIn), .inactive),
            ("not yet started", .disconnected, .inactive),
        ]

        for testCase in cases {
            let legacyBadge = FakeDockBadge()
            let legacyStatus = AppSessionStatus()
            legacyStatus.reportConnected(origin)
            let legacyFactory = FakeStatusItemFactory()
            let legacy = MenuBarController(
                status: legacyStatus,
                statusItemFactory: legacyFactory,
                image: nil,
                openWindow: {},
                quit: {},
                chatList: FakeChatList(testCase.list),
                dockBadge: legacyBadge,
                openChat: { _ in true },
                activity: FakeActivity(testCase.activity),
                now: { self.now }
            )
            legacy.start()

            let feed = HostFleetModel.HostFeeds(
                originKey: key,
                label: "Home",
                list: testCase.list,
                activity: testCase.activity
            )
            let fleetBadge = FakeDockBadge()
            let fleetStatus = AppSessionStatus()
            fleetStatus.reportConnected(origin)
            let fleetFactory = FakeStatusItemFactory()
            let fleet = MenuBarController(
                status: fleetStatus,
                statusItemFactory: fleetFactory,
                image: nil,
                openWindow: {},
                quit: {},
                fleet: FakeHostFleet(feeds: [feed], total: nil),
                dockBadge: fleetBadge,
                openChat: { _ in true },
                now: { self.now }
            )
            fleet.start()

            XCTAssertEqual(
                describe(try XCTUnwrap(fleet.menu)),
                describe(try XCTUnwrap(legacy.menu)),
                "\(testCase.name): a single-host fleet must render today's menu"
            )
            XCTAssertEqual(
                fleetFactory.items.first?.toolTip,
                legacyFactory.items.first?.toolTip,
                "\(testCase.name): the status item tooltip must match"
            )
        }
    }

    func testTheBadgeShowsTheSumAndNothingWhenZeroOrUnknown() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)

        let zero = FakeDockBadge()
        let zeroController = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            image: NSImage(size: NSSize(width: 40, height: 40)),
            openWindow: {},
            quit: {},
            fleet: FakeHostFleet(feeds: [], total: 0),
            dockBadge: zero,
            now: { self.now }
        )
        zeroController.start()
        XCTAssertEqual(zeroController.statusItem?.title, "")
        XCTAssertNil(zero.currentLabel, "A known zero paints nothing")

        let unknown = FakeDockBadge()
        let unknownController = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            image: NSImage(size: NSSize(width: 40, height: 40)),
            openWindow: {},
            quit: {},
            fleet: FakeHostFleet(feeds: [], total: nil),
            dockBadge: unknown,
            now: { self.now }
        )
        unknownController.start()
        XCTAssertEqual(unknownController.statusItem?.title, "")
        XCTAssertNil(unknown.currentLabel, "No known count paints nothing")

        let sum = FakeDockBadge()
        let sumController = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            image: NSImage(size: NSSize(width: 40, height: 40)),
            openWindow: {},
            quit: {},
            fleet: FakeHostFleet(feeds: [], total: 5),
            dockBadge: sum,
            now: { self.now }
        )
        sumController.start()
        XCTAssertEqual(sumController.statusItem?.title, "5")
        XCTAssertEqual(sum.currentLabel, "5", "The Dock badge and the status bar state the same total")
    }

    func testANonActiveHostChatRowIsDisabledWithASwitchTooltip() throws {
        let status = AppSessionStatus()
        status.reportConnected(origin)
        let other = Origin("https://other.example.com")!
        let feeds = [
            HostFleetModel.HostFeeds(
                originKey: origin.canonicalKey,
                label: "Home",
                list: .loaded(fleetSnapshot(origin: origin, id: "home-1", count: 1)),
                activity: .inactive
            ),
            HostFleetModel.HostFeeds(
                originKey: other.canonicalKey,
                label: "Office",
                list: .loaded(fleetSnapshot(origin: other, id: "office-1", count: 1)),
                activity: .inactive
            ),
        ]
        var opened: [String] = []
        let controller = MenuBarController(
            status: status,
            statusItemFactory: FakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            fleet: FakeHostFleet(feeds: feeds, total: 2),
            openChat: { opened.append($0); return true },
            now: { self.now }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        let activeRow = try XCTUnwrap(row(menu, id: "home-1"))
        let otherRow = try XCTUnwrap(row(menu, id: "office-1"))
        XCTAssertTrue(activeRow.isEnabled)
        XCTAssertFalse(otherRow.isEnabled, "This slice does not open another host's chat")
        XCTAssertEqual(otherRow.toolTip, "Switch to this host to open")
        XCTAssertEqual(opened, [])
    }

    // MARK: - Working glyph helpers

    private func workingSnapshot() -> ChatListSnapshot {
        ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [
                MenubarChat(
                    chatID: "needs",
                    title: "Needs",
                    workspace: "work",
                    lastActivityAt: iso(now),
                    unread: false,
                    needsInput: true
                ),
                MenubarChat(
                    chatID: "working",
                    title: "Working",
                    workspace: "work",
                    lastActivityAt: iso(now),
                    unread: false,
                    needsInput: false
                ),
                MenubarChat(
                    chatID: "unread",
                    title: "Unread",
                    workspace: "work",
                    lastActivityAt: iso(now),
                    unread: true,
                    needsInput: false
                ),
            ],
            attentionCount: 2,
            fetchedAt: now
        )
    }

    private func timedSnapshot(lastActivityAt: Date) -> ChatListSnapshot {
        ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [
                MenubarChat(
                    chatID: "chat-1",
                    title: "Chat",
                    workspace: "work",
                    lastActivityAt: iso(lastActivityAt),
                    unread: true,
                    needsInput: false
                ),
            ],
            attentionCount: 1,
            fetchedAt: now
        )
    }

    private func iso(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: date)
    }

    // MARK: - Fleet helpers

    /// Everything a user can see or click on one row, recursively, so two menus
    /// compare item for item in order.
    private func describe(_ menu: NSMenu) -> [String] {
        menu.items.flatMap { item -> [String] in
            let line = [
                item.isSeparatorItem ? "---" : item.title,
                "enabled=\(item.isEnabled)",
                "action=\(item.action.map { NSStringFromSelector($0) } ?? "nil")",
                "tooltip=\(item.toolTip ?? "nil")",
                "represented=\(item.representedObject.map { String(describing: $0) } ?? "nil")",
                "image=\(item.image != nil)",
            ].joined(separator: " | ")
            return [line] + (item.submenu.map(describe) ?? [])
        }
    }

    private func fleetSnapshot(origin: Origin, id: String, count: Int) -> ChatListSnapshot {
        ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [
                MenubarChat(
                    chatID: id,
                    title: id,
                    workspace: "work",
                    lastActivityAt: iso(now),
                    unread: true,
                    needsInput: false
                ),
            ],
            attentionCount: count,
            fetchedAt: now
        )
    }

    /// The row is identified by its chat id, not by a title that also carries a
    /// relative time.
    private func row(_ menu: NSMenu, id: String) -> NSMenuItem? {
        menu.items.first { $0.representedObject as? String == id }
    }

    // MARK: - Dock badge helpers
    private func dockSnapshot(count: Int?) -> ChatListSnapshot {
        ChatListSnapshot(
            origin: origin.canonicalKey,
            chats: [
                MenubarChat(
                    chatID: "chat-1",
                    title: "Chat",
                    workspace: "work",
                    lastActivityAt: ISO8601DateFormatter().string(from: now),
                    unread: true,
                    needsInput: false
                ),
            ],
            attentionCount: count,
            fetchedAt: now
        )
    }
}
