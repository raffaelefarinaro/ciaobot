import XCTest
import AppKit
import Combine
import WebKit

@testable import CiaobotMac

// MARK: - Doubles

/// Records every feed request. The fleet shares one instance across hosts, so
/// the recorded origin is what proves which host was contacted.
private final class FleetChatTransport: ChatListTransport {
    struct Call {
        let origin: Origin
        let cookieHeader: String?
    }

    private(set) var calls: [Call] = []
    /// The body each origin answers with. A host with no entry answers an
    /// empty list and a zero count.
    var payloads: [String: ChatListPayload] = [:]

    func fetch(
        origin: Origin,
        limit: Int,
        cookieHeader: String?,
        completion: @escaping (Result<ChatListPayload, ChatListFailure>) -> Void
    ) -> (() -> Void)? {
        calls.append(Call(origin: origin, cookieHeader: cookieHeader))
        let payload = payloads[origin.canonicalKey]
            ?? ChatListPayload(chats: [], attentionCount: 0)
        completion(.success(payload))
        return {}
    }
}

private final class FleetEventConnection: EventStreamConnection {
    private let onEvent: (ChatEvent) -> Void
    private let onClose: (EventStreamCloseReason) -> Void
    private(set) var cancelCount = 0

    init(onEvent: @escaping (ChatEvent) -> Void, onClose: @escaping (EventStreamCloseReason) -> Void) {
        self.onEvent = onEvent
        self.onClose = onClose
    }

    func emit(_ event: ChatEvent) { onEvent(event) }
    func close(_ reason: EventStreamCloseReason) { onClose(reason) }
    func cancel() { cancelCount += 1 }
}

private final class FleetEventTransport: EventStreamTransport {
    struct Open {
        let origin: Origin
        let cookieHeader: String
        let connection: FleetEventConnection
    }

    private(set) var opens: [Open] = []

    func open(
        origin: Origin,
        cookieHeader: String,
        onEvent: @escaping (ChatEvent) -> Void,
        onClose: @escaping (EventStreamCloseReason) -> Void
    ) -> EventStreamConnection? {
        let connection = FleetEventConnection(onEvent: onEvent, onClose: onClose)
        opens.append(Open(origin: origin, cookieHeader: cookieHeader, connection: connection))
        return connection
    }
}

/// Answers with one `ciao_session` cookie for the queried host, and records
/// which WebKit store the fleet asked for it. That pairing is the only way to
/// prove which host was bound to the session store and which to a saved one.
private final class FleetCookieProvider: ChatCookieProviding {
    let store: WKWebsiteDataStore
    var hasSessionCookie = true
    private(set) var queriedHosts: [String] = []

    init(store: WKWebsiteDataStore) {
        self.store = store
    }

    func cookies(for url: URL, completion: @escaping ([HTTPCookie]) -> Void) {
        queriedHosts.append(url.host ?? "")
        guard hasSessionCookie, let cookie = Self.cookie(for: url) else {
            completion([])
            return
        }
        completion([cookie])
    }

    static func cookie(for url: URL) -> HTTPCookie? {
        guard let host = url.host else { return nil }
        var properties: [HTTPCookiePropertyKey: Any] = [
            .name: "ciao_session",
            .value: "test-token",
            .domain: host,
            .path: "/",
            .expires: Date().addingTimeInterval(3600),
        ]
        let scheme = url.scheme?.lowercased() ?? ""
        if scheme == "https" || scheme == "wss" {
            properties[.secure] = "TRUE"
        }
        return HTTPCookie(properties: properties)
    }
}

@MainActor
private final class FleetCookieFactory {
    private(set) var providers: [FleetCookieProvider] = []
    var hasSessionCookie = true

    func make(store: WKWebsiteDataStore) -> ChatCookieProviding {
        let provider = FleetCookieProvider(store: store)
        provider.hasSessionCookie = hasSessionCookie
        providers.append(provider)
        return provider
    }

    var storeCalls: [(store: WKWebsiteDataStore, host: String)] {
        providers.flatMap { provider in
            provider.queriedHosts.map { (provider.store, $0) }
        }
    }
}

/// Hands out one in-memory store per saved identifier so the test can compare
/// store identity without creating a real named WebKit store.
@MainActor
private final class FleetStoreFactory {
    private(set) var identifiers: [UUID] = []
    private(set) var ephemeralCount = 0
    private var byIdentifier: [UUID: WKWebsiteDataStore] = [:]

    func store(for identifier: UUID) -> WKWebsiteDataStore {
        if let existing = byIdentifier[identifier] { return existing }
        identifiers.append(identifier)
        let store = WKWebsiteDataStore.nonPersistent()
        byIdentifier[identifier] = store
        return store
    }

    func ephemeral() -> WKWebsiteDataStore {
        ephemeralCount += 1
        return WKWebsiteDataStore.nonPersistent()
    }
}

@MainActor
private final class FleetEraser: WebsiteDataStoreErasing {
    func remove(identifier: UUID, completion: @escaping (Error?) -> Void) {
        completion(nil)
    }
}

@MainActor
private final class FleetHostList: HostListStoring {
    var hosts: [HostRecord]

    init(_ hosts: [HostRecord]) {
        self.hosts = hosts
    }

    func add(_ origin: Origin) {}
    func remove(_ origin: Origin) {}
    func rename(_ origin: Origin, label: String) {}
}

@MainActor
final class HostFleetTests: XCTestCase {
    private let activeLoopback = Origin("http://127.0.0.1:8443")!
    private let secondLoopback = Origin("http://127.0.0.1:9443")!
    private let remote = Origin("https://remote.example.com")!
    private let now = Date()

    // MARK: - Feed construction

    func testAFleetWithTwoSignedInHostsStartsOneFeedPairPerHost() throws {
        let (registry, _) = try makeRegistry()
        let first = Origin("https://alpha.example.com")!
        let second = Origin("https://beta.example.com")!
        XCTAssertNotNil(registry.identifier(for: first))
        XCTAssertNotNil(registry.identifier(for: second))

        let status = AppSessionStatus()
        status.reportConnected(first, dataStore: WKWebsiteDataStore.nonPersistent())
        let chatTransport = FleetChatTransport()
        let eventTransport = FleetEventTransport()
        let cookieFactory = FleetCookieFactory()
        let fleet = makeFleet(
            hosts: [record(first, "Alpha"), record(second, "Beta")],
            status: status,
            registry: registry,
            chatTransport: chatTransport,
            eventTransport: eventTransport,
            cookieFactory: cookieFactory
        )

        fleet.start()

        XCTAssertTrue(
            waitUntil { eventTransport.opens.count == 2 },
            "Each signed-in host must open its own event socket"
        )
        XCTAssertEqual(
            Set(chatTransport.calls.map { $0.origin.canonicalKey }),
            Set([first.canonicalKey, second.canonicalKey]),
            "The list feed is fetched once per host"
        )
        XCTAssertEqual(
            Set(eventTransport.opens.map { $0.origin.canonicalKey }),
            Set([first.canonicalKey, second.canonicalKey]),
            "The activity feed is opened once per host"
        )
        XCTAssertEqual(
            fleet.feeds.map { $0.originKey },
            [first.canonicalKey, second.canonicalKey],
            "The active host is first, then the saved order"
        )
    }

    func testARemoteHostWithoutASavedSignInIsNeverContacted() throws {
        let (registry, _) = try makeRegistry()
        let status = AppSessionStatus()
        status.reportConnected(activeLoopback, dataStore: WKWebsiteDataStore.nonPersistent())
        let chatTransport = FleetChatTransport()
        let eventTransport = FleetEventTransport()
        let fleet = makeFleet(
            hosts: [record(activeLoopback, "Home"), record(remote, "Remote")],
            status: status,
            registry: registry,
            chatTransport: chatTransport,
            eventTransport: eventTransport
        )

        fleet.start()

        XCTAssertTrue(
            waitUntil { chatTransport.calls.contains { $0.origin.canonicalKey == self.activeLoopback.canonicalKey } },
            "The reachable host still loads"
        )
        XCTAssertFalse(
            chatTransport.calls.contains { $0.origin.canonicalKey == remote.canonicalKey },
            "A remote host with no verified store must never reach /api/menubar-chats"
        )
        XCTAssertFalse(
            eventTransport.opens.contains { $0.origin.canonicalKey == remote.canonicalKey },
            "A remote host with no verified store must never reach /ws/events"
        )
        let remoteFeed = try XCTUnwrap(fleet.feeds.first { $0.originKey == remote.canonicalKey })
        XCTAssertEqual(remoteFeed.list, .unavailable(.notSignedIn))
        XCTAssertEqual(remoteFeed.activity, .inactive)
    }

    func testAFailedSnapshotVerificationFallsBackToNotSignedInWithoutContactingTheHost() throws {
        let (registry, file) = try makeRegistry()
        XCTAssertNotNil(registry.identifier(for: remote), "The remote host has a saved sign-in")

        // Another process rewrites the file, so this registry's snapshot is stale.
        let other = SessionStoreRegistry(fileURL: file)
        XCTAssertNotNil(other.identifier(for: Origin("https://other.example.com")!))

        let status = AppSessionStatus()
        status.reportConnected(activeLoopback, dataStore: WKWebsiteDataStore.nonPersistent())
        let chatTransport = FleetChatTransport()
        let eventTransport = FleetEventTransport()
        let cookieFactory = FleetCookieFactory()
        let stores = FleetStoreFactory()
        let fleet = makeFleet(
            hosts: [record(activeLoopback, "Home"), record(remote, "Remote")],
            status: status,
            registry: registry,
            chatTransport: chatTransport,
            eventTransport: eventTransport,
            cookieFactory: cookieFactory,
            stores: stores
        )

        fleet.start()

        XCTAssertTrue(
            waitUntil { self.isLoaded(fleet, origin: self.activeLoopback.canonicalKey) },
            "The active host still loads"
        )
        XCTAssertEqual(
            registry.storageUnavailableReason,
            .concurrentModification,
            "The fleet's own lookup is what detected the stale snapshot"
        )
        let remoteFeed = try XCTUnwrap(fleet.feeds.first { $0.originKey == remote.canonicalKey })
        XCTAssertEqual(remoteFeed.list, .unavailable(.notSignedIn))
        XCTAssertEqual(remoteFeed.activity, .inactive)
        XCTAssertFalse(
            chatTransport.calls.contains { $0.origin.canonicalKey == remote.canonicalKey },
            "A host whose store cannot be verified must never reach /api/menubar-chats"
        )
        XCTAssertFalse(
            eventTransport.opens.contains { $0.origin.canonicalKey == remote.canonicalKey },
            "A host whose store cannot be verified must never reach /ws/events"
        )
        XCTAssertEqual(stores.identifiers, [], "No saved store is opened for the unverified host")
        XCTAssertFalse(
            cookieFactory.storeCalls.contains { $0.host == remote.url.host },
            "No cookie is read for the unverified host"
        )
    }

    func testKnownAttentionTotalSumsOnlyLoadedPositiveCountsAndIsNilWhenAllUnknown() throws {
        let (registry, _) = try makeRegistry()
        let status = AppSessionStatus()
        status.reportConnected(activeLoopback, dataStore: WKWebsiteDataStore.nonPersistent())
        let chatTransport = FleetChatTransport()
        chatTransport.payloads[activeLoopback.canonicalKey] = ChatListPayload(
            chats: [chat("one", unread: true)],
            attentionCount: 2
        )
        chatTransport.payloads[secondLoopback.canonicalKey] = ChatListPayload(chats: [], attentionCount: 0)
        let fleet = makeFleet(
            hosts: [record(activeLoopback, "Home"), record(secondLoopback, "Office"), record(remote, "Remote")],
            status: status,
            registry: registry,
            chatTransport: chatTransport,
            eventTransport: FleetEventTransport()
        )

        fleet.start()

        XCTAssertTrue(
            waitUntil {
                self.isLoaded(fleet, origin: self.activeLoopback.canonicalKey)
                    && self.isLoaded(fleet, origin: self.secondLoopback.canonicalKey)
            },
            "Both reachable feeds must settle"
        )
        XCTAssertEqual(fleet.knownAttentionTotal, 2, "A zero count adds nothing; an unknown host adds nothing")

        // No host publishes a count: the total is unknown, not zero.
        let unknownTransport = FleetChatTransport()
        unknownTransport.payloads[activeLoopback.canonicalKey] = ChatListPayload(chats: [], attentionCount: nil)
        let unknownFleet = makeFleet(
            hosts: [record(activeLoopback, "Home"), record(remote, "Remote")],
            status: status,
            registry: registry,
            chatTransport: unknownTransport,
            eventTransport: FleetEventTransport()
        )
        unknownFleet.start()
        XCTAssertTrue(waitUntil { self.isLoaded(unknownFleet, origin: self.activeLoopback.canonicalKey) })
        XCTAssertNil(unknownFleet.knownAttentionTotal)

        // Every loaded count is zero: the sum is a known zero.
        let zeroTransport = FleetChatTransport()
        zeroTransport.payloads[activeLoopback.canonicalKey] = ChatListPayload(chats: [], attentionCount: 0)
        let zeroFleet = makeFleet(
            hosts: [record(activeLoopback, "Home")],
            status: status,
            registry: registry,
            chatTransport: zeroTransport,
            eventTransport: FleetEventTransport()
        )
        zeroFleet.start()
        XCTAssertTrue(waitUntil { self.isLoaded(zeroFleet, origin: self.activeLoopback.canonicalKey) })
        XCTAssertEqual(zeroFleet.knownAttentionTotal, 0)
    }

    func testStoreLookupNeverAllocatesAMapping() throws {
        let (registry, file) = try makeRegistry()
        XCTAssertNotNil(registry.identifier(for: Origin("https://other.example.com")!))
        let before = try Data(contentsOf: file)
        let status = AppSessionStatus()
        status.reportConnected(activeLoopback, dataStore: WKWebsiteDataStore.nonPersistent())
        let fleet = makeFleet(
            hosts: [record(activeLoopback, "Home"), record(remote, "Remote")],
            status: status,
            registry: registry,
            chatTransport: FleetChatTransport(),
            eventTransport: FleetEventTransport()
        )

        fleet.start()
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))

        XCTAssertEqual(
            try Data(contentsOf: file),
            before,
            "Opening a fleet must never write session-stores.json"
        )
        let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
        XCTAssertEqual(attributes[.posixPermissions] as? NSNumber, NSNumber(value: 0o600))
    }

    func testSwitchingTheActiveHostRekeysTheSessionBackedEntry() throws {
        let (registry, _) = try makeRegistry()
        let first = Origin("https://alpha.example.com")!
        let second = Origin("https://beta.example.com")!
        let firstIdentifier = try XCTUnwrap(registry.identifier(for: first))
        let secondIdentifier = try XCTUnwrap(registry.identifier(for: second))

        let firstSessionStore = WKWebsiteDataStore.nonPersistent()
        let secondSessionStore = WKWebsiteDataStore.nonPersistent()
        let status = AppSessionStatus()
        status.reportConnected(first, dataStore: firstSessionStore)

        let chatTransport = FleetChatTransport()
        let cookieFactory = FleetCookieFactory()
        let stores = FleetStoreFactory()
        let fleet = HostFleetModel(
            hosts: FleetHostList([record(first, "Alpha"), record(second, "Beta")]),
            status: status,
            signIn: SessionSignIn(registry: registry, eraser: FleetEraser(), persistentAvailable: true),
            listTransportFactory: { chatTransport },
            activityTransportFactory: { FleetEventTransport() },
            cookieProviderFactory: { cookieFactory.make(store: $0) },
            persistentStoreFactory: { stores.store(for: $0) },
            ephemeralStoreFactory: { stores.ephemeral() },
            now: { self.now }
        )

        fleet.start()

        XCTAssertTrue(
            waitUntil {
                self.uses(cookieFactory, store: firstSessionStore, for: first)
                    && self.uses(cookieFactory, store: stores.store(for: secondIdentifier), for: second)
            },
            "The active host uses the session store; the other uses its saved store"
        )

        status.reportConnected(second, dataStore: secondSessionStore)

        XCTAssertTrue(
            waitUntil { self.uses(cookieFactory, store: secondSessionStore, for: second) },
            "The new active host takes the session store"
        )
        XCTAssertTrue(
            waitUntil { self.uses(cookieFactory, store: stores.store(for: firstIdentifier), for: first) },
            "The previously active host falls back to its saved store"
        )
        XCTAssertEqual(
            Set(stores.identifiers),
            Set([firstIdentifier, secondIdentifier]),
            "Both saved stores are addressed"
        )
    }

    // MARK: - Helpers

    private func record(_ origin: Origin, _ label: String) -> HostRecord {
        HostRecord(origin: origin.canonicalKey, label: label)
    }

    private func makeFleet(
        hosts: [HostRecord],
        status: AppSessionStatus,
        registry: SessionStoreRegistry,
        chatTransport: FleetChatTransport,
        eventTransport: FleetEventTransport,
        cookieFactory: FleetCookieFactory? = nil,
        stores: FleetStoreFactory? = nil
    ) -> HostFleetModel {
        let cookieFactory = cookieFactory ?? FleetCookieFactory()
        let stores = stores ?? FleetStoreFactory()
        return HostFleetModel(
            hosts: FleetHostList(hosts),
            status: status,
            signIn: SessionSignIn(registry: registry, eraser: FleetEraser(), persistentAvailable: true),
            listTransportFactory: { chatTransport },
            activityTransportFactory: { eventTransport },
            cookieProviderFactory: { cookieFactory.make(store: $0) },
            persistentStoreFactory: { stores.store(for: $0) },
            ephemeralStoreFactory: { stores.ephemeral() },
            now: { self.now }
        )
    }

    private func isLoaded(_ fleet: HostFleetModel, origin: String) -> Bool {
        guard let feed = fleet.feeds.first(where: { $0.originKey == origin }) else { return false }
        if case .loaded = feed.list { return true }
        return false
    }

    private func uses(_ factory: FleetCookieFactory, store: WKWebsiteDataStore, for origin: Origin) -> Bool {
        factory.storeCalls.contains { call in
            call.store === store && call.host == origin.url.host
        }
    }

    private func chat(_ id: String, unread: Bool = false) -> MenubarChat {
        MenubarChat(
            chatID: id,
            title: id,
            workspace: "work",
            lastActivityAt: ISO8601DateFormatter().string(from: now),
            unread: unread,
            needsInput: false
        )
    }

    @discardableResult
    private func waitUntil(timeout: TimeInterval = 3, _ condition: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            RunLoop.main.run(until: Date().addingTimeInterval(0.01))
        }
        return condition()
    }
}
