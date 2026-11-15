import Combine
import Foundation
import WebKit

/// The read-only surface the menu bar consumes: one settled feed per saved
/// host, ordered active first, plus the single badge total derived from them.
/// Kept as a protocol so the menu can be tested with a fake fleet.
@MainActor
protocol HostFleetProviding: AnyObject {
    var feeds: [HostFleetModel.HostFeeds] { get }
    var feedsPublisher: AnyPublisher<[HostFleetModel.HostFeeds], Never> { get }
    /// The sum of the loaded positive counts. Nil when no host has published a
    /// count at all; zero and nil both paint no number.
    var knownAttentionTotal: Int? { get }
    func start()
    func stop()
    func refreshIfNeeded()
}

/// One feed pair (`GET /api/menubar-chats` and its own `/ws/events`) per saved
/// host.
///
/// The active host uses the session's live, possibly in-memory store. Every
/// other host is bound to a fixed context built from its saved sign-in: the
/// mapping is read with a snapshot-verified, non-allocating lookup, so opening
/// the fleet never writes `session-stores.json`. A remote host without such a
/// mapping gets no model instances at all and a single disabled
/// `.unavailable(.notSignedIn)` feed, so it is never contacted. A loopback host
/// gets a fresh in-memory store and an anonymous list poll, exactly as a single
/// loopback connection does.
///
/// Feeds exist only while the session is connected. Before the first verified
/// connection, and after a disconnect, the tray shows exactly what a
/// single-host build shows: the connection line and no chat claims.
@MainActor
final class HostFleetModel: ObservableObject, HostFleetProviding {
    /// One host's settled feed, as the menu renders it.
    struct HostFeeds: Equatable {
        let originKey: String
        let label: String
        let list: ChatListState
        let activity: ChatActivityState
    }

    /// How a host is bound to a store. Only `session` follows a reconnect; the
    /// other kinds are fixed until the saved host set or the connection changes.
    enum BindingKind: Equatable {
        /// The active host: the session's own store, which may be in-memory.
        case session
        /// A saved, snapshot-verified persistent sign-in.
        case stored(UUID)
        /// A loopback origin reachable without a session. A fresh in-memory
        /// store means the list poll is anonymous and working state is unknown
        /// unless that store happens to hold a cookie.
        case anonymous
        /// A remote host with no saved sign-in: no request is ever made.
        case notSignedIn
    }

    @Published private(set) var feeds: [HostFeeds] = []

    var feedsPublisher: AnyPublisher<[HostFeeds], Never> { $feeds.eraseToAnyPublisher() }

    var knownAttentionTotal: Int? {
        var total: Int?
        for feed in feeds {
            guard case .loaded(let snapshot) = feed.list,
                  let count = snapshot.attentionCount else { continue }
            total = (total ?? 0) + max(0, count)
        }
        return total
    }

    private let hosts: HostListStoring
    private let status: AppSessionStatus
    private let signIn: SessionSignIn
    private let listTransportFactory: () -> ChatListTransport
    private let activityTransportFactory: () -> EventStreamTransport
    private let cookieProviderFactory: (WKWebsiteDataStore) -> ChatCookieProviding
    private let persistentStoreFactory: @MainActor (UUID) -> WKWebsiteDataStore
    private let ephemeralStoreFactory: @MainActor () -> WKWebsiteDataStore
    private let sessionContextFactory: @MainActor (AppSessionStatus, Origin) -> ConnectionContext
    private let staticContextFactory: @MainActor (Origin, WKWebsiteDataStore) -> ConnectionContext
    private let now: () -> Date

    private var entries: [HostFleetEntry] = []
    private var connectionSubscription: AnyCancellable?
    private var started = false

    init(
        hosts: HostListStoring,
        status: AppSessionStatus,
        signIn: SessionSignIn,
        listTransportFactory: @escaping () -> ChatListTransport = { URLSessionChatListTransport() },
        activityTransportFactory: @escaping () -> EventStreamTransport = { URLSessionEventStreamTransport() },
        cookieProviderFactory: @escaping (WKWebsiteDataStore) -> ChatCookieProviding = {
            WebKitCookieProvider(dataStore: $0)
        },
        persistentStoreFactory: @escaping @MainActor (UUID) -> WKWebsiteDataStore = { identifier in
            if #available(macOS 14.0, *) {
                return WKWebsiteDataStore(forIdentifier: identifier)
            }
            return WKWebsiteDataStore.nonPersistent()
        },
        ephemeralStoreFactory: @escaping @MainActor () -> WKWebsiteDataStore = {
            WKWebsiteDataStore.nonPersistent()
        },
        sessionContextFactory: @escaping @MainActor (AppSessionStatus, Origin) -> ConnectionContext = {
            SessionHostContext($0, origin: $1)
        },
        staticContextFactory: @escaping @MainActor (Origin, WKWebsiteDataStore) -> ConnectionContext = {
            StaticHostContext(origin: $0, dataStore: $1)
        },
        now: @escaping () -> Date = Date.init
    ) {
        self.hosts = hosts
        self.status = status
        self.signIn = signIn
        self.listTransportFactory = listTransportFactory
        self.activityTransportFactory = activityTransportFactory
        self.cookieProviderFactory = cookieProviderFactory
        self.persistentStoreFactory = persistentStoreFactory
        self.ephemeralStoreFactory = ephemeralStoreFactory
        self.sessionContextFactory = sessionContextFactory
        self.staticContextFactory = staticContextFactory
        self.now = now
    }

    func start() {
        guard !started else { return }
        started = true
        connectionSubscription = status.$connection.sink { [weak self] _ in
            // `$connection` fires from its property's `willSet`, so the new
            // value is not readable inside the callback. Re-key on the next
            // turn, when `currentOrigin` and `connection` are both settled.
            DispatchQueue.main.async { self?.syncEntries() }
        }
        syncEntries()
    }

    func stop() {
        guard started else { return }
        started = false
        connectionSubscription?.cancel()
        connectionSubscription = nil
        for entry in entries { entry.stop() }
        entries = []
        if !feeds.isEmpty { feeds = [] }
    }

    func refreshIfNeeded() {
        guard started else { return }
        for entry in entries {
            entry.list?.refreshIfNeeded()
            entry.activity?.refreshIfNeeded()
        }
    }

    // MARK: - Entry construction

    /// Reconciles the bound entries with the settled connection and saved
    /// hosts. Entries whose origin and binding are unchanged are reused, so a
    /// reconnect only re-keys the two hosts involved instead of discarding
    /// every other host's rows.
    private func syncEntries() {
        guard started else { return }

        var remaining = entries
        var next: [HostFleetEntry] = []
        for host in desiredHosts() {
            let kind = bindingKind(for: host.origin, isActive: host.isActive)
            let key = host.origin.canonicalKey
            if let index = remaining.firstIndex(where: {
                $0.originKey == key && $0.kind == kind && $0.label == host.label
            }) {
                next.append(remaining.remove(at: index))
                continue
            }
            if let index = remaining.firstIndex(where: { $0.originKey == key }) {
                remaining.remove(at: index).stop()
            }
            next.append(makeEntry(origin: host.origin, label: host.label, kind: kind))
        }
        for leftover in remaining { leftover.stop() }
        entries = next
        for entry in entries { entry.start() }
        publishFeeds()
    }

    private struct DesiredHost {
        let origin: Origin
        let label: String
        let isActive: Bool
    }

    /// The active host always has a feed, even before the saved list has
    /// migrated it, so a single-host launch keeps today's chat section. Every
    /// other saved host follows in saved order.
    private func desiredHosts() -> [DesiredHost] {
        // `currentOrigin` (not `connection`) is the connection fact here: it is
        // already settled when `$connection` fires and it is cleared before the
        // disconnect value is published.
        guard let active = status.currentOrigin else { return [] }
        var desired = [DesiredHost(origin: active, label: savedLabel(for: active), isActive: true)]
        for record in hosts.hosts {
            guard let origin = Origin(record.origin),
                  origin.canonicalKey != active.canonicalKey else { continue }
            desired.append(DesiredHost(origin: origin, label: record.label, isActive: false))
        }
        return desired
    }

    private func savedLabel(for origin: Origin) -> String {
        if let record = hosts.hosts.first(where: { $0.origin == origin.canonicalKey }) {
            return record.label
        }
        return HostRecord.defaultLabel(for: origin.canonicalKey)
    }

    private func bindingKind(for origin: Origin, isActive: Bool) -> BindingKind {
        if isActive { return .session }
        // The snapshot-verified lookup is the only one that may open a store
        // this process does not intend to create: it never allocates a mapping.
        if let identifier = signIn.registry.verifiedIdentifierIfPresent(for: origin),
           !signIn.retiredIdentifiers.contains(identifier) {
            return .stored(identifier)
        }
        return origin.isLoopback ? .anonymous : .notSignedIn
    }

    private func makeEntry(origin: Origin, label: String, kind: BindingKind) -> HostFleetEntry {
        guard let context = context(for: origin, kind: kind) else {
            // Remote and unsigned: no fetch, no socket, no store.
            return HostFleetEntry(
                originKey: origin.canonicalKey,
                label: label,
                kind: kind,
                list: nil,
                activity: nil,
                owner: self
            )
        }
        let list = ChatListModel(
            context: context,
            transport: listTransportFactory(),
            cookieProviderFactory: cookieProviderFactory,
            now: now
        )
        let activity = ChatActivityModel(
            context: context,
            transport: activityTransportFactory(),
            cookieProviderFactory: cookieProviderFactory,
            now: now
        )
        return HostFleetEntry(
            originKey: origin.canonicalKey,
            label: label,
            kind: kind,
            list: list,
            activity: activity,
            owner: self
        )
    }

    private func context(for origin: Origin, kind: BindingKind) -> ConnectionContext? {
        switch kind {
        case .session:
            return sessionContextFactory(status, origin)
        case .stored(let identifier):
            return staticContextFactory(origin, persistentStoreFactory(identifier))
        case .anonymous:
            return staticContextFactory(origin, ephemeralStoreFactory())
        case .notSignedIn:
            return nil
        }
    }

    fileprivate func entryChanged() {
        publishFeeds()
    }

    private func publishFeeds() {
        let next = entries.map { entry -> HostFeeds in
            let list: ChatListState
            if case .loaded(let snapshot) = entry.listState, snapshot.origin != entry.originKey {
                // A late result from a replaced connection never renders under
                // another host's header.
                list = .loading
            } else {
                list = entry.listState
            }
            return HostFeeds(
                originKey: entry.originKey,
                label: entry.label,
                list: list,
                activity: entry.activityState
            )
        }
        guard next != feeds else { return }
        feeds = next
    }
}

/// One host's pair of feed models and their last settled states. A placeholder
/// entry (remote, no saved sign-in) holds no models.
@MainActor
private final class HostFleetEntry {
    let originKey: String
    let label: String
    let kind: HostFleetModel.BindingKind
    let list: ChatListModel?
    let activity: ChatActivityModel?
    private(set) var listState: ChatListState
    private(set) var activityState: ChatActivityState

    private weak var owner: HostFleetModel?
    private var listSubscription: AnyCancellable?
    private var activitySubscription: AnyCancellable?
    private var started = false

    init(
        originKey: String,
        label: String,
        kind: HostFleetModel.BindingKind,
        list: ChatListModel?,
        activity: ChatActivityModel?,
        owner: HostFleetModel
    ) {
        self.originKey = originKey
        self.label = label
        self.kind = kind
        self.list = list
        self.activity = activity
        self.listState = list?.state ?? .unavailable(.notSignedIn)
        self.activityState = activity?.state ?? .inactive
        self.owner = owner
    }

    func start() {
        guard let list, let activity, !started else { return }
        started = true
        // `@Published` sends before the value is stored, so the sent value is
        // the settled one for this entry; reading the model back here would see
        // the previous state.
        listSubscription = list.$state.sink { [weak self] state in
            guard let self else { return }
            self.listState = state
            self.owner?.entryChanged()
        }
        activitySubscription = activity.$state.sink { [weak self] state in
            guard let self else { return }
            self.activityState = state
            self.owner?.entryChanged()
        }
        list.start()
        activity.start()
    }

    func stop() {
        listSubscription?.cancel()
        listSubscription = nil
        activitySubscription?.cancel()
        activitySubscription = nil
        list?.stop()
        activity?.stop()
        started = false
    }
}
