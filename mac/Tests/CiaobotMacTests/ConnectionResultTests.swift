import XCTest
@testable import CiaobotMac

final class ConnectionResultTests: XCTestCase {
    private let origin = Origin("http://127.0.0.1:8443")!
    private let now = Date(timeIntervalSince1970: 1_000_000)

    func testNoticeFreeOutcomesProduceNoCard() {
        XCTAssertNil(ConnectionResult(outcome: .notNeeded))
        XCTAssertNil(ConnectionResult(outcome: .deferred(origin: origin)))
        XCTAssertNil(ConnectionResult(outcome: .done(left: [])))
    }

    func testCompletedMoveCopyNamesTheUntouchedOldInstall() {
        let card = ConnectionResult(outcome: .done(left: ["/Users/me/repos/ciaobot"]), now: now)!
        XCTAssertEqual(card.title, "Ciaobot now runs inside this app")
        XCTAssertEqual(card.reason, "Your old install at /Users/me/repos/ciaobot was left untouched.")
        XCTAssertEqual(card.kind, .info)
        XCTAssertFalse(card.reason.contains("1.x"))
        XCTAssertFalse(card.canTryAgain)
        let several = ConnectionResult(outcome: .done(left: ["/a", "/b"]))!
        XCTAssertEqual(several.reason, "Your old install was left untouched: /a, /b")
    }

    func testOnlyACompletedMoveClearsItselfAndHidesTheCheckedLine() {
        let done = ConnectionResult(outcome: .done(left: ["/a"]))!
        XCTAssertTrue(done.succeeded)
        XCTAssertEqual(done.autoClearDelay, 8)
        let info = ConnectionResult(outcome: .refused(code: "downgrade", oldOrigin: nil))!
        let warning = ConnectionResult(outcome: .refused(code: "engine_too_old", oldOrigin: nil))!
        let failure = ConnectionResult(outcome: .refused(code: "rollback_failed", oldOrigin: nil))!
        let rolledBack = ConnectionResult(outcome: .rolledBack(code: "not_healthy", oldOrigin: nil))!
        for card in [info, warning, failure, rolledBack, ConnectionResult.checking()] {
            XCTAssertFalse(card.succeeded)
            XCTAssertNil(card.autoClearDelay)
        }
        XCTAssertEqual(ConnectionResult.dismissTitle, "Dismiss")
    }

    func testEveryRefusalCodeHasATitleAndTheFixedReason() {
        let codes = ["downgrade", "engine_too_old", "custom_data_location", "port_mismatch", "legacy_desktop",
                     "unknown_program", "no_workspace", "unfinished_migration", "already_settled", "plist_changed",
                     "plist_changed_stopped", "moved_not_ready", "cancelled_past_trash", "rollback_failed",
                     "plan_failed", "state_unknown", "invented_code"]
        for code in codes {
            let outcome = EngineTakeover.Outcome.refused(code: code, oldOrigin: nil)
            let card = ConnectionResult(outcome: outcome)
            XCTAssertNotNil(card, code)
            XCTAssertFalse(card?.title.isEmpty ?? true, code)
            XCTAssertEqual(card?.reason, TakeoverCopy.notice(for: outcome), code)
        }
    }

    func testRetryOnlyWhereANewCheckCanGetPastTheResult() {
        func retry(_ code: String) -> Bool? {
            ConnectionResult(outcome: .refused(code: code, oldOrigin: nil))?.canTryAgain
        }
        XCTAssertEqual(retry("engine_too_old"), true)
        XCTAssertEqual(retry("state_unknown"), true)
        XCTAssertEqual(retry("invented_code"), true)
        XCTAssertEqual(retry("downgrade"), false)
        XCTAssertEqual(retry("custom_data_location"), false)
        XCTAssertEqual(retry("plist_changed_stopped"), false)
        XCTAssertEqual(retry("rollback_failed"), false)
        XCTAssertEqual(ConnectionResult(outcome: .rolledBack(code: "not_healthy", oldOrigin: nil))?.canTryAgain, true)
    }

    func testSeverityFollowsWhatHappenedToTheExistingInstall() {
        func kind(_ code: String) -> ConnectionResult.Kind? {
            ConnectionResult(outcome: .refused(code: code, oldOrigin: nil))?.kind
        }
        XCTAssertEqual(kind("downgrade"), .info)
        XCTAssertEqual(kind("engine_too_old"), .attention)
        XCTAssertEqual(kind("rollback_failed"), .failure)
        XCTAssertEqual(kind("plist_changed_stopped"), .failure)
    }

    func testTheExistingAddressIsOfferedOnlyWhenItAnswered() {
        let with = ConnectionResult(outcome: .refused(code: "engine_too_old", oldOrigin: origin))
        XCTAssertEqual(with?.existingAddress, "http://127.0.0.1:8443")
        XCTAssertNil(ConnectionResult(outcome: .refused(code: "engine_too_old", oldOrigin: nil))?.existingAddress)
    }

    func testTheCheckingCardIsNotARetryableResult() {
        let card = ConnectionResult.checking(now: now)
        XCTAssertEqual(card.kind, .checking)
        XCTAssertEqual(card.title, "Checking the Ciaobot on this Mac…")
        XCTAssertFalse(card.canTryAgain)
        XCTAssertEqual(card.announcement, card.title)
    }

    func testARepeatedIdenticalRefusalIsAVisiblyNewResult() {
        let outcome = EngineTakeover.Outcome.refused(code: "engine_too_old", oldOrigin: nil)
        let first = ConnectionResult(outcome: outcome, now: now)!
        let second = ConnectionResult(outcome: outcome, now: now.addingTimeInterval(30))!
        XCTAssertNotEqual(first.id, second.id)
        XCTAssertEqual(first.title, second.title)
        XCTAssertEqual(first.checkedLabel(now: now.addingTimeInterval(2)), "Checked just now")
        XCTAssertEqual(second.checkedLabel(now: now.addingTimeInterval(45)), "Checked 15 seconds ago")
        XCTAssertTrue(first.checkedLabel(now: now.addingTimeInterval(300)).hasPrefix("Checked at "))
    }

    func testPersistenceControlAppearsOnlyWhereItCanMatter() {
        let https = Origin("https://example.com")!
        let http = Origin("http://127.0.0.1:8443")!
        XCTAssertEqual(SignInPersistenceHint.make(origin: nil, canPersist: false, explanation: "x"), .hidden)
        XCTAssertEqual(SignInPersistenceHint.make(origin: http, canPersist: false, explanation: "x"),
                       .line("Signed-in sessions are kept only for https addresses."))
        XCTAssertEqual(SignInPersistenceHint.make(origin: https, canPersist: true, explanation: "full"), .toggle(help: "full"))
        // An https address whose store is unusable keeps its explanation visible.
        XCTAssertEqual(SignInPersistenceHint.make(origin: https, canPersist: false, explanation: "broken list"), .line("broken list"))
    }
}
