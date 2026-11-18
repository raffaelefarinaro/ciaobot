import Combine
import Foundation

/// The native side of the in-window Settings bridge. It owns no state: it reads
/// the same `NotificationMenuProviding` object the tray row reads and performs
/// exactly the action the tray row performs, through the shared
/// `ChatNotificationState.settingAction` rule.
///
/// Created before any window so the connected main window can be handed it;
/// `bind` supplies the notification provider once the menu-bar controller
/// exists. Until then the snapshot reports `notifications: null`.
@MainActor
final class AppSettingsBridge {
    private var notifications: NotificationMenuProviding?
    private var systemSettings: SystemSettingsOpening?
    private let changeSubject = PassthroughSubject<Void, Never>()
    private var bindings: [AnyCancellable] = []

    init() {}

    func bind(notifications: NotificationMenuProviding, systemSettings: SystemSettingsOpening?) {
        bindings.removeAll()
        self.notifications = notifications
        self.systemSettings = systemSettings
        bindings = [
            notifications.statePublisher.dropFirst().sink { [changeSubject] _ in changeSubject.send() },
            notifications.streamStatusPublisher.dropFirst().sink { [changeSubject] _ in changeSubject.send() },
        ]
    }

    /// Fires on every notification state or stream-status change after bind.
    var changes: AnyPublisher<Void, Never> { changeSubject.eraseToAnyPublisher() }

    /// The v1 contract. Every string is native-authored.
    func snapshot() -> [String: Any] {
        var notificationsValue: Any = NSNull()
        if let notifications {
            let state = notifications.state
            let status = notifications.streamStatus
            notificationsValue = [
                "state": Self.stateName(state),
                "ready": status == .ready,
                "action": state.settingAction.rawValue,
                "title": MenuBarController.notificationTitle(for: state, streamStatus: status),
                "detail": MenuBarController.notificationToolTip(for: state, streamStatus: status),
            ] as [String: Any]
        }
        return ["v": 1, "notifications": notificationsValue]
    }

    /// Runs one gesture-gated op. Returns false for an unknown op, an unbound
    /// bridge, a state with no action, or a toggle the notification controller
    /// did not act on (not started, request in flight); none of those has a
    /// side effect.
    func perform(_ op: String) -> Bool {
        guard op == "notifications", let notifications else { return false }
        switch notifications.state.settingAction {
        case .toggle:
            return notifications.performMenuAction()
        case .openSystemSettings:
            guard let systemSettings else { return false }
            systemSettings.openNotificationSettings()
            return true
        case .none:
            return false
        }
    }

    private static func stateName(_ state: ChatNotificationState) -> String {
        switch state {
        case .off: return "off"
        case .on: return "on"
        case .denied: return "denied"
        case .deliveryOff: return "deliveryOff"
        case .unavailable: return "unavailable"
        }
    }
}
