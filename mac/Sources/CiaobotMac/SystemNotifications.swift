import Foundation
import UserNotifications

/// Only the default click opens a chat. Dismiss, clear, and any custom action
/// must never navigate.
enum NotificationResponsePolicy {
    static func target(actionIdentifier: String, userInfo: [AnyHashable: Any]) -> ChatNotificationTarget? {
        guard actionIdentifier == UNNotificationDefaultActionIdentifier,
              let chatID = userInfo["chat_id"] as? String,
              let origin = userInfo["origin"] as? String else { return nil }
        return ChatNotificationTarget(chatID: chatID, origin: origin)
    }
}

/// The pure view of the OS notification settings the assessment needs. It
/// exists so the decision is testable without a bundle, a window, or a real
/// notification centre.
struct NotificationSettingsSnapshot: Equatable {
    var authorization: UNAuthorizationStatus
    var alertSetting: UNNotificationSetting
    var notificationCenterSetting: UNNotificationSetting
}

/// Maps a settings snapshot to what the app may claim. Authorization granted
/// while both visible delivery channels for this app are disabled is
/// `.deliveryOff`, not `.authorized`: macOS reports the permission, but no
/// banner or Notification Center entry will appear. Focus / Do Not Disturb has
/// no public API, so it is never inferred here.
enum NotificationAuthorizationAssessment {
    static func assess(_ snapshot: NotificationSettingsSnapshot) -> ChatNotificationAuthorization {
        switch snapshot.authorization {
        case .notDetermined:
            return .notDetermined
        case .denied:
            return .denied
        case .authorized, .provisional, .ephemeral:
            guard snapshot.alertSetting == .disabled,
                  snapshot.notificationCenterSetting == .disabled else {
                return .authorized
            }
            return .deliveryOff
        @unknown default:
            return .unknown("macOS reported an unrecognized notification authorization status.")
        }
    }
}

/// The real UserNotifications surface. `isAvailable` means only that this
/// process has a bundle identifier and a `.app` wrapper: the one condition
/// this client can establish before touching UserNotifications. It is not a
/// code-signature check. An unsigned or improperly provisioned bundle can
/// still be available here and then fail at the OS boundary; that failure is
/// preserved as a technical error, never folded into a user denial.
///
/// Nothing here requests permission except `requestAuthorization`, which the
/// controller calls only from the explicit menu action.
@MainActor
final class SystemNotificationPermissionClient: NSObject, NotificationPermissionClient, UNUserNotificationCenterDelegate {
    let isAvailable: Bool
    var onActivation: ((ChatNotificationTarget) -> Void)? {
        didSet {
            // A click that launched the app can be delivered before the
            // controller exists. Hold it until the handler is installed.
            guard let target = pendingActivation, let onActivation else { return }
            pendingActivation = nil
            DispatchQueue.main.async {
                onActivation(target)
            }
        }
    }

    private var notificationCenter: UNUserNotificationCenter?
    private var pendingActivation: ChatNotificationTarget?

    init(bundle: Bundle = .main) {
        // A debug executable run from `.build/debug` has no usable app bundle.
        // Treat it as unavailable rather than crashing in UserNotifications.
        self.isAvailable = bundle.bundleIdentifier != nil && bundle.bundleURL.pathExtension == "app"
        super.init()
    }

    func prepare() {
        guard isAvailable else { return }
        _ = resolvedCenter()
    }

    func authorizationStatus(completion: @escaping (ChatNotificationAuthorization) -> Void) {
        guard isAvailable else {
            completion(.unavailable)
            return
        }
        resolvedCenter().getNotificationSettings { settings in
            let snapshot = NotificationSettingsSnapshot(
                authorization: settings.authorizationStatus,
                alertSetting: settings.alertSetting,
                notificationCenterSetting: settings.notificationCenterSetting
            )
            let status = NotificationAuthorizationAssessment.assess(snapshot)
            Task { @MainActor in completion(status) }
        }
    }

    func requestAuthorization(completion: @escaping (ChatNotificationAuthorizationRequest) -> Void) {
        guard isAvailable else {
            completion(.unavailable)
            return
        }
        // `.alert` and `.badge` only. No provisional request and no sound: the
        // user asked for a visible completion notice, not a quiet or audible
        // subscription.
        resolvedCenter().requestAuthorization(options: [.alert, .badge]) { granted, error in
            let result: ChatNotificationAuthorizationRequest
            if let error {
                // A missing identity, an unsigned bundle, or a provisioning
                // failure arrives here. Preserve the exact message; it is not
                // a user denial.
                result = .failed(error.localizedDescription)
            } else {
                result = granted ? .granted : .denied
            }
            Task { @MainActor in completion(result) }
        }
    }

    func deliver(_ message: ChatNotificationMessage) {
        guard isAvailable else { return }
        let content = UNMutableNotificationContent()
        content.title = message.title
        if !message.subtitle.isEmpty {
            // The workspace name, so a banner from a server with several
            // workspaces says which one it came from. An empty subtitle stays
            // unset rather than reserving a blank line.
            content.subtitle = message.subtitle
        }
        content.body = message.body
        content.threadIdentifier = "ciaobot.chat"
        content.userInfo = [
            "chat_id": message.target.chatID,
            "origin": message.target.origin,
        ]
        if #available(macOS 12.0, *) {
            content.interruptionLevel = .active
        }
        let request = UNNotificationRequest(
            identifier: message.identifier,
            content: content,
            trigger: nil
        )
        resolvedCenter().add(request) { error in
            // The title and body are user data. Log only the failure, never
            // the notification content.
            if let error {
                NSLog("Ciaobot notification delivery failed: %@", error.localizedDescription)
            }
        }
    }

    func removeDelivered(identifier: String) {
        guard isAvailable else { return }
        resolvedCenter().removeDeliveredNotifications(withIdentifiers: [identifier])
    }

    private func resolvedCenter() -> UNUserNotificationCenter {
        if let notificationCenter { return notificationCenter }
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        notificationCenter = center
        return center
    }

    // MARK: UNUserNotificationCenterDelegate

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        if #available(macOS 11.0, *) {
            completionHandler([.banner, .list])
        } else {
            completionHandler([.alert])
        }
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let info = response.notification.request.content.userInfo
        guard let target = NotificationResponsePolicy.target(
            actionIdentifier: response.actionIdentifier,
            userInfo: info
        ) else {
            completionHandler()
            return
        }
        Task { @MainActor [weak self] in
            defer { completionHandler() }
            guard let self else { return }
            if let onActivation = self.onActivation {
                onActivation(target)
            } else {
                self.pendingActivation = target
            }
        }
    }
}
