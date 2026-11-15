import XCTest
import UserNotifications

@testable import CiaobotMac

/// The whole assessment is a pure function over the snapshot, so these tests
/// never touch a bundle, a notification centre, or the operator's Mac.
final class NotificationAuthorizationAssessmentTests: XCTestCase {
    private func snapshot(
        _ authorization: UNAuthorizationStatus,
        alert: UNNotificationSetting = .enabled,
        center: UNNotificationSetting = .enabled
    ) -> NotificationSettingsSnapshot {
        NotificationSettingsSnapshot(
            authorization: authorization,
            alertSetting: alert,
            notificationCenterSetting: center
        )
    }

    func testAuthorizedWithBothChannelsEnabledIsAuthorized() {
        let snapshot = snapshot(.authorized, alert: .enabled, center: .enabled)

        XCTAssertEqual(NotificationAuthorizationAssessment.assess(snapshot), .authorized)
    }

    func testAuthorizedWithBothChannelsDisabledIsDeliveryOff() {
        let snapshot = snapshot(.authorized, alert: .disabled, center: .disabled)

        XCTAssertEqual(NotificationAuthorizationAssessment.assess(snapshot), .deliveryOff)
    }

    func testAuthorizedWithOnlyAlertsEnabledIsAuthorized() {
        let snapshot = snapshot(.authorized, alert: .enabled, center: .disabled)

        XCTAssertEqual(NotificationAuthorizationAssessment.assess(snapshot), .authorized)
    }

    func testAuthorizedWithOnlyNotificationCentreEnabledIsAuthorized() {
        let snapshot = snapshot(.authorized, alert: .disabled, center: .enabled)

        XCTAssertEqual(NotificationAuthorizationAssessment.assess(snapshot), .authorized)
    }

    func testDeniedStaysDeniedEvenWhenBothChannelsAreDisabled() {
        let snapshot = snapshot(.denied, alert: .disabled, center: .disabled)

        XCTAssertEqual(NotificationAuthorizationAssessment.assess(snapshot), .denied)
    }

    func testNotDeterminedStaysNotDeterminedEvenWhenBothChannelsAreDisabled() {
        let snapshot = snapshot(.notDetermined, alert: .disabled, center: .disabled)

        XCTAssertEqual(NotificationAuthorizationAssessment.assess(snapshot), .notDetermined)
    }

    func testProvisionalWithBothChannelsDisabledIsDeliveryOff() {
        let snapshot = snapshot(.provisional, alert: .disabled, center: .disabled)

        XCTAssertEqual(NotificationAuthorizationAssessment.assess(snapshot), .deliveryOff)
    }

    func testProvisionalWithBothChannelsEnabledIsAuthorized() {
        let snapshot = snapshot(.provisional, alert: .enabled, center: .enabled)

        XCTAssertEqual(NotificationAuthorizationAssessment.assess(snapshot), .authorized)
    }
}
