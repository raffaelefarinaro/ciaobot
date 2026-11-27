import XCTest
import Foundation

@testable import CiaobotMac

/// The saved host list is a durable, validated, fail-closed list. With no list
/// on disk the only host is the last origin that answered, so an upgraded user
/// never loses the host they were using; nothing is written until a mutation.
@MainActor
final class HostsTests: XCTestCase {
    private var suiteNames: [String] = []

    override func tearDown() {
        for name in suiteNames {
            IsolatedDefaults.remove(name)
        }
        suiteNames = []
        super.tearDown()
    }

    private func isolatedDefaults() throws -> UserDefaults {
        let name = IsolatedDefaults.makeName("Hosts")
        suiteNames.append(name)
        let defaults = try XCTUnwrap(UserDefaults(suiteName: name))
        defaults.removePersistentDomain(forName: name)
        return defaults
    }

    private func makeStore(
        defaults: UserDefaults
    ) -> (UserDefaultsHostListStore, UserDefaultsLastServerStore) {
        let lastServer = UserDefaultsLastServerStore(defaults: defaults)
        return (UserDefaultsHostListStore(defaults: defaults, lastServer: lastServer), lastServer)
    }

    func testAnEmptyStoreMigratesTheLastUsedOriginAsTheOnlyHost() throws {
        let defaults = try isolatedDefaults()
        let lastServer = UserDefaultsLastServerStore(defaults: defaults)
        let origin = try XCTUnwrap(Origin("https://remote.example:8443"))
        lastServer.remember(origin)
        let store = UserDefaultsHostListStore(defaults: defaults, lastServer: lastServer)

        XCTAssertEqual(
            store.hosts,
            [HostRecord(origin: "https://remote.example:8443", label: "remote.example:8443")]
        )
        XCTAssertNil(defaults.object(forKey: UserDefaultsHostListStore.defaultKey),
                     "A read-only launch wrote the saved host list")
    }

    func testAStoredEntryThatFailsOriginValidationIsDroppedOnRead() throws {
        let defaults = try isolatedDefaults()
        let (store, _) = makeStore(defaults: defaults)
        let stored = """
        [{"label":"ok.example:8443","origin":"https://ok.example:8443"},
         {"label":"192.168.1.2:8443","origin":"http://192.168.1.2:8443"}]
        """
        defaults.set(Data(stored.utf8), forKey: UserDefaultsHostListStore.defaultKey)

        XCTAssertEqual(store.hosts.map(\.origin), ["https://ok.example:8443"])
    }

    func testUndecodableStoredDataReadsAsNoHostsAndDoesNotResurrectTheLastOrigin() throws {
        let defaults = try isolatedDefaults()
        let lastServer = UserDefaultsLastServerStore(defaults: defaults)
        lastServer.remember(try XCTUnwrap(Origin("https://remote.example:8443")))
        let store = UserDefaultsHostListStore(defaults: defaults, lastServer: lastServer)
        defaults.set(Data([0x00, 0xFF, 0x7F]), forKey: UserDefaultsHostListStore.defaultKey)

        XCTAssertEqual(store.hosts, [], "Corrupt data fell back to the last-used origin")
    }

    func testAddDeduplicatesByCanonicalKeyAndNeverRewritesAnUnchangedList() throws {
        let defaults = try isolatedDefaults()
        let (store, _) = makeStore(defaults: defaults)
        let origin = try XCTUnwrap(Origin("https://a.example:8443"))

        store.add(origin)
        store.add(origin)
        let afterFirstAdd = try XCTUnwrap(defaults.data(forKey: UserDefaultsHostListStore.defaultKey))

        store.add(try XCTUnwrap(Origin("https://a.example:8443/")))

        XCTAssertEqual(store.hosts.map(\.origin), ["https://a.example:8443"])
        XCTAssertEqual(defaults.data(forKey: UserDefaultsHostListStore.defaultKey), afterFirstAdd,
                       "A redundant add rewrote the saved host list")
    }

    func testRemoveOfTheActiveHostClearsTheLastUsedOrigin() throws {
        let defaults = try isolatedDefaults()
        let (store, lastServer) = makeStore(defaults: defaults)
        let active = try XCTUnwrap(Origin("https://active.example:8443"))
        let other = try XCTUnwrap(Origin("https://other.example:8443"))
        lastServer.remember(active)
        store.add(other)

        store.remove(other)

        XCTAssertEqual(lastServer.savedOrigin?.canonicalKey, active.canonicalKey,
                       "Removing another host cleared the last-used origin")

        store.remove(active)

        XCTAssertNil(lastServer.savedOrigin, "Removing the active host left it as the launch target")
    }

    func testLabelIsSanitizedAndBoundedTo48Characters() throws {
        let defaults = try isolatedDefaults()
        let (store, _) = makeStore(defaults: defaults)
        let origin = try XCTUnwrap(Origin("https://label.example:8443"))
        store.add(origin)

        store.rename(origin, label: "left\u{202E}right")

        XCTAssertEqual(store.hosts.first?.label, "leftright",
                       "A bidi override survived the label sanitizer")

        store.rename(origin, label: String(repeating: "a", count: 200))

        let label = try XCTUnwrap(store.hosts.first?.label)
        XCTAssertLessThanOrEqual(label.count, 48)
        XCTAssertTrue(label.hasSuffix("…"), "A clipped label must end with an ellipsis: \(label)")
    }

    func testRemovingTheTakenOverPortDropsOnlyLoopbackHostsOnThatPort() throws {
        let defaults = try isolatedDefaults()
        let (store, _) = makeStore(defaults: defaults)
        for text in ["http://127.0.0.1:8445", "http://localhost:8445", "http://[::1]:8445",
                     "http://127.0.0.1:9000", "https://mac.tailnet.ts.net:8445", "https://vps.example:8445"] {
            store.add(try XCTUnwrap(Origin(text)))
        }

        store.removeLoopbackHosts(ports: [8445])

        XCTAssertEqual(store.hosts.map(\.origin), [
            "http://127.0.0.1:9000", "https://mac.tailnet.ts.net:8445", "https://vps.example:8445",
        ], "A Tailscale or LAN host on the same port is another server, and another loopback port is not the taken-over engine")
    }

    func testPruningOwnPortsIsIdempotentAndKeepsDevEnginesAndOtherHosts() throws {
        let defaults = try isolatedDefaults()
        let (store, _) = makeStore(defaults: defaults)
        for text in ["https://127.0.0.1:61190", "http://127.0.0.1:8445", "http://127.0.0.1:8543", "https://vps.example:8445"] {
            store.add(try XCTUnwrap(Origin(text)))
        }

        store.removeLoopbackHosts(ports: [61190, 8445])
        store.removeLoopbackHosts(ports: [61190, 8445])

        XCTAssertEqual(store.hosts.map(\.origin), ["http://127.0.0.1:8543", "https://vps.example:8445"])
    }

    func testTheOnlyHostFromTheMigratedLastOriginIsRemovedAndForgotten() throws {
        let defaults = try isolatedDefaults()
        let (store, lastServer) = makeStore(defaults: defaults)
        lastServer.remember(try XCTUnwrap(Origin("http://127.0.0.1:8445")))
        XCTAssertEqual(store.hosts.map(\.origin), ["http://127.0.0.1:8445"])

        store.removeLoopbackHosts(ports: [8445])

        XCTAssertEqual(store.hosts, [], "The migrated last origin is not offered again")
        XCTAssertNil(lastServer.savedOrigin)
    }
}
