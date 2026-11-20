import XCTest
import Foundation

@testable import CiaobotMac

/// The startup decision is pure: a saved origin only becomes a launch target
/// outside a test launch, and a saved origin always prefills the field.
final class ConnectionLaunchTests: XCTestCase {
    private let saved = Origin("https://remote.example:8443")!
    private let localDefault = "http://127.0.0.1:8443"

    func testASavedOriginWinsOverTheDefaultAddress() {
        XCTAssertEqual(
            ConnectionLaunch.initialAddress(saved: saved, defaultAddress: localDefault),
            saved.url.absoluteString
        )
    }

    func testNoSavedOriginKeepsTheDefaultAddress() {
        XCTAssertEqual(
            ConnectionLaunch.initialAddress(saved: nil, defaultAddress: localDefault),
            localDefault
        )
    }

    func testATestLaunchNeverAutoConnects() {
        for isDebugBuild in [true, false] {
            XCTAssertNil(
                ConnectionLaunch.autoConnectOrigin(
                    saved: saved,
                    isTestLaunch: true,
                    isDebugBuild: isDebugBuild
                ),
                "A test launch auto-connected to a saved origin (debug: \(isDebugBuild))"
            )
        }
    }

    func testNoSavedOriginNeverAutoConnects() {
        for isDebugBuild in [true, false] {
            XCTAssertNil(ConnectionLaunch.autoConnectOrigin(
                saved: nil,
                isTestLaunch: false,
                isDebugBuild: isDebugBuild
            ))
        }
    }

    func testASavedOriginAutoConnectsOutsideATestLaunch() {
        XCTAssertEqual(
            ConnectionLaunch.autoConnectOrigin(saved: saved, isTestLaunch: false, isDebugBuild: true)?.canonicalKey,
            saved.canonicalKey
        )
        XCTAssertEqual(
            ConnectionLaunch.autoConnectOrigin(saved: saved, isTestLaunch: false, isDebugBuild: false)?.canonicalKey,
            saved.canonicalKey
        )
    }

    func testANonLoopbackHTTPOriginCannotBeSaved() {
        XCTAssertNil(Origin("http://remote.example:8443"),
                     "The parser accepted a plain-HTTP remote origin")
        XCTAssertNil(Origin("http://192.168.1.2:8443"),
                     "The parser accepted a plain-HTTP non-loopback origin")
    }
}

/// The saved origin is one canonical origin or nothing. A corrupt value is
/// discarded on read so a bad write can never become a launch target.
final class LastServerStoreTests: XCTestCase {
    private var suiteNames: [String] = []

    override func tearDown() {
        for name in suiteNames {
            IsolatedDefaults.remove(name)
        }
        suiteNames = []
        super.tearDown()
    }

    private func isolatedDefaults() throws -> UserDefaults {
        let name = IsolatedDefaults.makeName("LastServer")
        suiteNames.append(name)
        let defaults = try XCTUnwrap(UserDefaults(suiteName: name))
        defaults.removePersistentDomain(forName: name)
        return defaults
    }

    func testARememberedOriginRoundTripsThroughAnIsolatedSuite() throws {
        let defaults = try isolatedDefaults()
        let store = UserDefaultsLastServerStore(defaults: defaults)
        let origin = try XCTUnwrap(Origin("https://remote.example:8443"))

        store.remember(origin)

        XCTAssertEqual(store.savedOrigin?.canonicalKey, origin.canonicalKey)
    }

    func testOnlyTheCanonicalOriginIsWritten() throws {
        let defaults = try isolatedDefaults()
        let store = UserDefaultsLastServerStore(defaults: defaults)
        let origin = try XCTUnwrap(Origin("https://remote.example:8443"))

        store.remember(origin)

        XCTAssertEqual(
            defaults.string(forKey: UserDefaultsLastServerStore.defaultKey),
            "https://remote.example:8443"
        )
    }

    func testACorruptStoredValueReadsAsNilAndIsCleared() throws {
        let defaults = try isolatedDefaults()
        let store = UserDefaultsLastServerStore(defaults: defaults)
        defaults.set("https://remote.example:8443/some/path?token=secret", forKey: UserDefaultsLastServerStore.defaultKey)

        XCTAssertNil(store.savedOrigin, "A stored path or query was accepted as an origin")
        XCTAssertNil(defaults.string(forKey: UserDefaultsLastServerStore.defaultKey),
                     "The corrupt value was not cleared")
    }

    func testACredentialBearingStoredValueIsDiscarded() throws {
        let defaults = try isolatedDefaults()
        let store = UserDefaultsLastServerStore(defaults: defaults)
        defaults.set("https://user:secret@remote.example:8443", forKey: UserDefaultsLastServerStore.defaultKey)

        XCTAssertNil(store.savedOrigin)
        XCTAssertNil(defaults.string(forKey: UserDefaultsLastServerStore.defaultKey))
    }

    func testClearRemovesTheSavedOrigin() throws {
        let defaults = try isolatedDefaults()
        let store = UserDefaultsLastServerStore(defaults: defaults)
        store.remember(try XCTUnwrap(Origin("https://remote.example:8443")))

        store.clear()

        XCTAssertNil(store.savedOrigin)
        XCTAssertNil(defaults.string(forKey: UserDefaultsLastServerStore.defaultKey))
    }
}
