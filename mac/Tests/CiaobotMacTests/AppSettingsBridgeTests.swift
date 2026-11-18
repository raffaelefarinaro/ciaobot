import XCTest
import Combine
import AppKit

@testable import CiaobotMac

@MainActor
final class AppSettingsFakeNotifications: NotificationMenuProviding {
    let subject: CurrentValueSubject<ChatNotificationState, Never>
    let streamSubject: CurrentValueSubject<ChatNotificationStreamStatus, Never>
    private(set) var actionCount = 0
    var performResult = true

    init(_ state: ChatNotificationState = .off, stream: ChatNotificationStreamStatus = .ready) {
        subject = CurrentValueSubject(state)
        streamSubject = CurrentValueSubject(stream)
    }

    var state: ChatNotificationState { subject.value }
    var statePublisher: AnyPublisher<ChatNotificationState, Never> { subject.eraseToAnyPublisher() }
    var streamStatus: ChatNotificationStreamStatus { streamSubject.value }
    var streamStatusPublisher: AnyPublisher<ChatNotificationStreamStatus, Never> { streamSubject.eraseToAnyPublisher() }

    func performMenuAction() -> Bool { actionCount += 1; return performResult }
    func refreshIfNeeded() {}
    func stop() {}
}

@MainActor
final class AppSettingsFakeSystemSettings: SystemSettingsOpening {
    private(set) var notificationSettingsCount = 0
    func openNotificationSettings() { notificationSettingsCount += 1 }
    func openLoginItemsSettings() {}
}

@MainActor
final class AppSettingsBridgeTests: XCTestCase {
    private let allStates: [ChatNotificationState] = [
        .off, .on, .denied, .deliveryOff, .unavailable(.unbundled),
    ]
    private let expectedNames = ["off", "on", "denied", "deliveryOff", "unavailable"]
    private let expectedActions = ["toggle", "toggle", "openSystemSettings", "openSystemSettings", "none"]

    func testSnapshotBeforeBindHasNullNotifications() {
        let snapshot = AppSettingsBridge().snapshot()
        XCTAssertEqual(snapshot["v"] as? Int, 1)
        XCTAssertTrue(snapshot["notifications"] is NSNull)
    }

    func testSnapshotMapsEveryState() throws {
        for (index, state) in allStates.enumerated() {
            for stream in [ChatNotificationStreamStatus.ready, .connecting] {
                let bridge = AppSettingsBridge()
                bridge.bind(notifications: AppSettingsFakeNotifications(state, stream: stream), systemSettings: nil)
                let value = try XCTUnwrap(bridge.snapshot()["notifications"] as? [String: Any])
                XCTAssertEqual(Set(value.keys), ["state", "ready", "action", "title", "detail"])
                XCTAssertEqual(value["state"] as? String, expectedNames[index])
                XCTAssertEqual(value["ready"] as? Bool, stream == .ready)
                XCTAssertEqual(value["action"] as? String, expectedActions[index])
                XCTAssertEqual(value["title"] as? String, MenuBarController.notificationTitle(for: state, streamStatus: stream))
                XCTAssertEqual(value["detail"] as? String, MenuBarController.notificationToolTip(for: state, streamStatus: stream))
            }
        }
    }

    func testToggleCallsPerformMenuActionOnce() {
        for state in [ChatNotificationState.off, .on] {
            let fake = AppSettingsFakeNotifications(state)
            let settings = AppSettingsFakeSystemSettings()
            let bridge = AppSettingsBridge()
            bridge.bind(notifications: fake, systemSettings: settings)
            XCTAssertTrue(bridge.perform("notifications"))
            XCTAssertEqual(fake.actionCount, 1)
            XCTAssertEqual(settings.notificationSettingsCount, 0)
        }
    }

    func testToggleThatDidNothingIsNotReportedAsSuccess() {
        let fake = AppSettingsFakeNotifications(.off)
        fake.performResult = false
        let bridge = AppSettingsBridge()
        bridge.bind(notifications: fake, systemSettings: nil)
        XCTAssertFalse(bridge.perform("notifications"))
    }

    func testDeniedAndDeliveryOffOpenSystemSettingsOnly() {
        for state in [ChatNotificationState.denied, .deliveryOff] {
            let fake = AppSettingsFakeNotifications(state)
            let settings = AppSettingsFakeSystemSettings()
            let bridge = AppSettingsBridge()
            bridge.bind(notifications: fake, systemSettings: settings)
            XCTAssertTrue(bridge.perform("notifications"))
            XCTAssertEqual(settings.notificationSettingsCount, 1)
            XCTAssertEqual(fake.actionCount, 0, "Opening System Settings must not toggle the opt-in.")
        }
    }

    func testUnavailableRefuses() {
        let fake = AppSettingsFakeNotifications(.unavailable(.unbundled))
        let settings = AppSettingsFakeSystemSettings()
        let bridge = AppSettingsBridge()
        bridge.bind(notifications: fake, systemSettings: settings)
        XCTAssertFalse(bridge.perform("notifications"))
        XCTAssertEqual(fake.actionCount, 0)
        XCTAssertEqual(settings.notificationSettingsCount, 0)
    }

    func testUnknownOpRefuses() {
        let fake = AppSettingsFakeNotifications(.off)
        let bridge = AppSettingsBridge()
        bridge.bind(notifications: fake, systemSettings: nil)
        for op in ["get", "checkForUpdates", "", "Notifications"] {
            XCTAssertFalse(bridge.perform(op), op)
        }
        XCTAssertEqual(fake.actionCount, 0)
        XCTAssertFalse(AppSettingsBridge().perform("notifications"), "An unbound bridge has nothing to act on.")
    }

    func testChangesFireOnStateAndStreamChanges() {
        let fake = AppSettingsFakeNotifications(.off, stream: .connecting)
        let bridge = AppSettingsBridge()
        bridge.bind(notifications: fake, systemSettings: nil)
        var count = 0
        let token = bridge.changes.sink { count += 1 }
        XCTAssertEqual(count, 0, "Binding replays the current value but is not a change.")
        fake.subject.send(.on)
        XCTAssertEqual(count, 1)
        fake.streamSubject.send(.ready)
        XCTAssertEqual(count, 2)
        token.cancel()
    }

    func testMenuAndBridgeShareTheRule() throws {
        for state in allStates {
            let fake = AppSettingsFakeNotifications(state)
            let settings = AppSettingsFakeSystemSettings()
            let controller = MenuBarController(
                status: AppSessionStatus(),
                statusItemFactory: NoopStatusItemFactory(),
                openWindow: {},
                quit: {},
                notifications: fake,
                systemSettings: settings
            )
            controller.start()
            let title = MenuBarController.notificationTitle(for: state, streamStatus: .ready)
            let item = try XCTUnwrap(Self.find(title, in: try XCTUnwrap(controller.menu)), title)
            switch state.settingAction {
            case .toggle:
                XCTAssertEqual(item.action, #selector(MenuBarController.toggleNotifications(_:)))
                XCTAssertTrue(item.isEnabled)
            case .openSystemSettings:
                XCTAssertEqual(item.action, #selector(MenuBarController.openNotificationSettings(_:)))
                XCTAssertTrue(item.isEnabled)
            case .none:
                XCTAssertNil(item.action)
                XCTAssertFalse(item.isEnabled)
            }
        }
    }

    private static func find(_ title: String, in menu: NSMenu) -> NSMenuItem? {
        for item in menu.items {
            if item.title == title { return item }
            if let submenu = item.submenu, let found = find(title, in: submenu) { return found }
        }
        return nil
    }
}

@MainActor
private final class NoopStatusItem: MenuBarStatusItem {
    var title: String?
    var image: NSImage?
    var toolTip: String?
    var menu: NSMenu?
    var isVisible = false
    func remove() {}
}

@MainActor
private final class NoopStatusItemFactory: StatusItemFactory {
    func makeStatusItem(length: CGFloat) -> MenuBarStatusItem { NoopStatusItem() }
}
