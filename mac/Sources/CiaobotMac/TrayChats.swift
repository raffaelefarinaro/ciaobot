import AppKit
import Combine
import Foundation
import WebKit

/// The three states Home puts above "quiet": a chat that is blocked on the
/// owner, a chat whose agent is still running, and a finished reply that has
/// not been read. The order here is the order the menu renders them; selection
/// precedence is different (see `TrayChatList`). The menu carries the tier as
/// each row's glyph rather than as a repeated header.
enum TrayChatTier: Int, CaseIterable, Equatable {
    case needsYou
    case working
    case unread

    /// The status phrase Home prints under the title (`statusPhrase` in
    /// `HomeRecentChats.vue`). The native row uses it as the tooltip's first
    /// line; the glyph carries the accessible description below.
    var statusPhrase: String {
        switch self {
        case .needsYou: return "waiting for you"
        case .working: return "agent is working"
        case .unread: return "new reply"
        }
    }

    var glyphDescription: String {
        switch self {
        case .needsYou: return "Needs your answer"
        case .working: return "Working"
        case .unread: return "Unread chat"
        }
    }
}

/// One relevant chat, already assigned to exactly one tier.
struct TrayChatRow: Equatable {
    let chat: MenubarChat
    let tier: TrayChatTier
    /// The homepage's compact relative unit, or nil when the server did not
    /// send a parseable timestamp. Absence is not rendered as "now": the tray
    /// must not claim an activity time it does not have.
    let relativeTime: String?
    /// True when the live event stream says this chat is running, even when
    /// `needsYou` or `unread` outranks `working` for placement.
    let working: Bool
}

struct TrayChatSection: Equatable {
    let tier: TrayChatTier
    let rows: [TrayChatRow]
}

/// The rows of one workspace, in the order `TrayChatList.sections` produced
/// them. `workspace` is the cleaned label the menu prints as the header.
struct TrayChatWorkspaceGroup: Equatable {
    let workspace: String
    let rows: [TrayChatRow]
}

/// Relative time in the same units as `web/src/lib/relativeTime.ts`:
/// `now`, `5m`, `2h`, `3d`, `1w`, `1mo`. The homepage returns "now" for an
/// unparseable timestamp; the tray returns nil instead so a missing value is
/// absent rather than invented.
enum TrayRelativeTime {
    static func label(from raw: String, now: Date = Date()) -> String? {
        guard let timestamp = parse(raw) else { return nil }
        let elapsed = max(0, Int(now.timeIntervalSince(timestamp).rounded(.down)))
        if elapsed < 60 { return "now" }
        let minutes = elapsed / 60
        if minutes < 60 { return "\(minutes)m" }
        let hours = minutes / 60
        if hours < 24 { return "\(hours)h" }
        let days = hours / 24
        if days < 7 { return "\(days)d" }
        let weeks = days / 7
        if weeks < 4 { return "\(weeks)w" }
        return "\(max(1, days / 30))mo"
    }

    private static func parse(_ raw: String) -> Date? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractional.date(from: trimmed) { return date }
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        return plain.date(from: trimmed)
    }
}

/// The menu's glyph adaptation of Home's `ChatSignals`/`ActivityGlyph`: a
/// filled alert triangle for needs-you, a 3x3 pixel grid for working, and an
/// eye for unread. Template images so macOS applies the menu-bar tint. The
/// working grid is drawn by `WorkingGlyphRenderer` so the menu can advance the
/// same chase frame the web UI animates; the other tiers stay system symbols.
@MainActor
enum TrayChatGlyph {
    static func image(
        for tier: TrayChatTier,
        litCell: Int? = 0,
        reduceMotion: Bool = false,
        renderer: MenuGlyphRendering = WorkingGlyphRenderer()
    ) -> NSImage? {
        switch tier {
        case .needsYou:
            return symbol("exclamationmark.triangle.fill", for: tier)
        case .working:
            let image = renderer.glyphImage(
                litCell: reduceMotion ? nil : litCell,
                size: WorkingGlyphRenderer.pointSize,
                reduceMotion: reduceMotion
            )
            // The drawn grid is the working glyph; only a renderer that cannot
            // make an image falls back to the symbol.
            if let image {
                image.isTemplate = true
                image.accessibilityDescription = tier.glyphDescription
                return image
            }
            return symbol("square.grid.3x3.fill", for: tier)
                ?? symbol("circle.grid.3x3.fill", for: tier)
        case .unread:
            return symbol("eye", for: tier)
        }
    }

    private static func symbol(_ name: String, for tier: TrayChatTier) -> NSImage? {
        guard let image = NSImage(systemSymbolName: name, accessibilityDescription: tier.glyphDescription) else {
            return nil
        }
        image.isTemplate = true
        return image
    }
}

/// The tray list: only chats Home would show above its quiet tier, one row per
/// chat, with Home's precedence. `needsYou` outranks `unread`, which outranks
/// `working`, exactly as `groupHomeTiers` does. Working is only known while the
/// authenticated event stream has delivered its snapshot for this origin; no
/// event state is inferred for another origin or while the socket is down.
enum TrayChatList {
    static func sections(
        snapshot: ChatListSnapshot,
        activity: ChatActivityState?,
        now: Date = Date()
    ) -> [TrayChatSection] {
        let workingIDs = activity?.workingChatIDs(for: snapshot.origin)
        var seen = Set<String>()
        var buckets: [TrayChatTier: [TrayChatRow]] = [:]

        // The server already orders by recency and floats attention rows to
        // the front. Sorting each tier here keeps Home's tier order even after
        // that float moves a quiet-looking chat.
        let ordered = snapshot.chats.enumerated().sorted { lhs, rhs in
            if lhs.element.lastActivityAt != rhs.element.lastActivityAt {
                return lhs.element.lastActivityAt > rhs.element.lastActivityAt
            }
            return lhs.offset < rhs.offset
        }.map(\.element)

        for chat in ordered {
            guard seen.insert(chat.chatID).inserted else { continue }
            let isWorking = workingIDs?.contains(chat.chatID) ?? false
            let tier: TrayChatTier?
            if chat.needsInput {
                tier = .needsYou
            } else if chat.unread {
                tier = .unread
            } else if isWorking {
                tier = .working
            } else {
                tier = nil
            }
            guard let tier else { continue }
            buckets[tier, default: []].append(TrayChatRow(
                chat: chat,
                tier: tier,
                relativeTime: TrayRelativeTime.label(from: chat.lastActivityAt, now: now),
                working: isWorking
            ))
        }

        return TrayChatTier.allCases.compactMap { tier in
            guard let rows = buckets[tier], !rows.isEmpty else { return nil }
            return TrayChatSection(tier: tier, rows: rows)
        }
    }

    /// True when the server's feed names more than one workspace. The menu
    /// uses this instead of the attention groups alone: when a server serves
    /// several workspaces, a page whose flagged chats all happen to come from
    /// one of them still needs its header, or those rows cannot be attributed
    /// to the workspace they came from.
    static func servesMultipleWorkspaces(_ snapshot: ChatListSnapshot) -> Bool {
        workspaceLabels(of: snapshot.chats).count > 1
    }

    /// The cleaned labels of every workspace in the feed, keyed exactly as the
    /// menu's headers are, so one label cannot appear twice under two names.
    private static func workspaceLabels(of chats: [MenubarChat]) -> Set<String> {
        Set(chats.map { MenuTextSanitizer.sanitize($0.workspace, limit: 48, fallback: "Workspace") })
    }

    /// Splits the tier-ordered rows by workspace. A workspace takes the
    /// position of its first row, so the workspace holding the most urgent
    /// chat leads, and each group keeps Home's tier order inside it.
    static func workspaceGroups(_ sections: [TrayChatSection]) -> [TrayChatWorkspaceGroup] {
        var order: [String] = []
        var grouped: [String: [TrayChatRow]] = [:]
        for row in sections.flatMap(\.rows) {
            // Group by the cleaned label, not the raw server string, so two
            // names that only differ by invisible/control characters cannot
            // present duplicate-looking workspace headers.
            let workspace = MenuTextSanitizer.sanitize(row.chat.workspace, limit: 48, fallback: "Workspace")
            if grouped[workspace] == nil {
                order.append(workspace)
                grouped[workspace] = []
            }
            grouped[workspace]?.append(row)
        }
        return order.map { TrayChatWorkspaceGroup(workspace: $0, rows: grouped[$0] ?? []) }
    }
}

enum ChatActivityFailure: Equatable {
    /// `/ws/events` always requires `ciao_session`; there was no safe cookie
    /// to offer, or the server rejected it.
    case notSignedIn
    /// The socket could not open, closed, or sent a payload this client cannot
    /// use. Working state is unknown, not empty.
    case unavailable
}

/// What the tray knows about in-flight turns for one origin. Every non-live
/// state is explicit about not knowing; only `.live` carries active chat ids.
enum ChatActivityState: Equatable {
    case inactive
    case connecting(origin: String)
    case live(origin: String, activeChatIDs: Set<String>)
    case unavailable(origin: String, failure: ChatActivityFailure)

    func workingChatIDs(for origin: String) -> Set<String>? {
        guard case .live(let stateOrigin, let activeChatIDs) = self,
              stateOrigin == origin else { return nil }
        return activeChatIDs
    }

    func isLive(for origin: String) -> Bool {
        workingChatIDs(for: origin) != nil
    }

    /// A truthful status row for the menu when working state is not known.
    /// Nil for a live feed and for a state that belongs to another origin, so
    /// a switch cannot briefly claim the old server's state.
    func note(for origin: String) -> String? {
        switch self {
        case .inactive:
            return nil
        case .connecting(let stateOrigin):
            return stateOrigin == origin ? "Working status connecting…" : nil
        case .unavailable(let stateOrigin, let failure):
            guard stateOrigin == origin else { return nil }
            switch failure {
            case .notSignedIn: return "Working status unavailable — sign in"
            case .unavailable: return "Working status unavailable"
            }
        case .live:
            return nil
        }
    }
}

@MainActor
protocol ChatActivityProviding: AnyObject {
    var state: ChatActivityState { get }
    var statePublisher: AnyPublisher<ChatActivityState, Never> { get }
    func start()
    func stop()
    /// The menu is about to open. A recent live feed is reused; a failed or
    /// signed-out attempt gets one immediate retry instead of waiting for the
    /// backoff timer.
    func refreshIfNeeded()
}

/// Owns the tray's own authenticated `/ws/events` subscription. It is
/// deliberately independent of native notification opt-in: notifications may
/// be off, denied, or unavailable, and the working tier still reads the same
/// snapshot and streaming events. It never asks the OS for notification
/// permission and never delivers a banner.
@MainActor
final class ChatActivityModel: ObservableObject, ChatActivityProviding {
    private static let retryDelays: [TimeInterval] = [2, 5, 15, 30, 60]

    @Published private(set) var state: ChatActivityState = .inactive
    var statePublisher: AnyPublisher<ChatActivityState, Never> { $state.eraseToAnyPublisher() }

    private let context: ConnectionContext
    private let transport: EventStreamTransport
    private let cookieProviderFactory: (WKWebsiteDataStore) -> ChatCookieProviding
    private let now: () -> Date
    private let hostStoreTracking: HostStoreTrackingBundle?

    private var started = false
    private var subscription: AnyCancellable?
    private var connection: EventStreamConnection?
    private var generation = 0
    private var currentOrigin: Origin?
    private var activeChatIDs: Set<String> = []
    private var snapshotReceived = false
    private var opening = false
    private var retryWorkItem: DispatchWorkItem?
    private var retryAttempt = 0

    init(
        context: ConnectionContext,
        transport: EventStreamTransport = URLSessionEventStreamTransport(),
        cookieProviderFactory: @escaping (WKWebsiteDataStore) -> ChatCookieProviding = { WebKitCookieProvider(dataStore: $0) },
        now: @escaping () -> Date = Date.init,
        hostStoreTracking: HostStoreTrackingBundle? = nil
    ) {
        self.context = context
        self.transport = transport
        self.cookieProviderFactory = cookieProviderFactory
        self.now = now
        self.hostStoreTracking = hostStoreTracking
    }

    deinit {
        retryWorkItem?.cancel()
        connection?.cancel()
    }

    func start() {
        guard !started else { return }
        started = true
        subscription = context.connectionPublisher.sink { [weak self] _ in
            Task { @MainActor in self?.connectionChanged() }
        }
        connectionChanged()
    }

    func stop() {
        guard started else { return }
        started = false
        subscription?.cancel()
        subscription = nil
        cancelRetry()
        resetStream(.inactive)
        currentOrigin = nil
    }

    func refreshIfNeeded() {
        guard started, context.isConnected, let origin = context.currentOrigin else { return }
        guard connection == nil else { return }
        retryAttempt = 0
        openIfPossible(origin: origin)
    }

    private func connectionChanged() {
        guard started else { return }
        guard context.isConnected, let origin = context.currentOrigin else {
            currentOrigin = nil
            cancelRetry()
            resetStream(.inactive)
            return
        }

        if currentOrigin?.canonicalKey == origin.canonicalKey {
            // Re-reporting the same connection is not a new session. Only
            // recover if the socket is already gone and no attempt is in
            // flight or waiting on its backoff.
            if connection == nil, retryWorkItem == nil {
                openIfPossible(origin: origin)
            }
            return
        }

        currentOrigin = origin
        retryAttempt = 0
        cancelRetry()
        resetStream(.connecting(origin: origin.canonicalKey))
        openIfPossible(origin: origin)
    }

    private func openIfPossible(origin: Origin) {
        guard started,
              context.isConnected,
              context.currentOrigin?.canonicalKey == origin.canonicalKey,
              connection == nil,
              !opening else { return }

        cancelRetry()
        generation += 1
        let generation = self.generation
        opening = true
        snapshotReceived = false
        activeChatIDs = []
        state = .connecting(origin: origin.canonicalKey)

        let acquisition = hostStoreTracking?.acquire(origin: origin, store: context.dataStore)
        if case .refused? = acquisition {
            opening = false
            state = .unavailable(origin: origin.canonicalKey, failure: .unavailable)
            scheduleRetry(origin: origin)
            return
        }

        guard let dataStore = context.dataStore else {
            opening = false
            state = .unavailable(origin: origin.canonicalKey, failure: .notSignedIn)
            scheduleRetry(origin: origin)
            return
        }
        guard let url = EventStreamEndpoint.url(origin: origin) else {
            opening = false
            state = .unavailable(origin: origin.canonicalKey, failure: .unavailable)
            scheduleRetry(origin: origin)
            return
        }

        let operation: UUID? = { if case .operation(let id)? = acquisition { id } else { nil } }()
        let registry = hostStoreTracking?.registry
        cookieProviderFactory(dataStore).cookies(for: url) { [weak self] cookies in
            Task { @MainActor in
                if let operation { guard registry?.claimCompletion(id: operation) == true else { return } }
                defer { if let operation { registry?.finishOperation(id: operation) } }
                guard let self, self.isCurrent(generation: generation, origin: origin) else { return }
                self.opening = false
                guard let cookieHeader = EventStreamEndpoint.cookieHeader(
                    for: url,
                    cookies: cookies,
                    now: self.now()
                ) else {
                    // The socket has no anonymous mode. Do not send a
                    // handshake without this exact origin's session cookie.
                    self.state = .unavailable(origin: origin.canonicalKey, failure: .notSignedIn)
                    self.scheduleRetry(origin: origin)
                    return
                }
                self.open(origin: origin, cookieHeader: cookieHeader, generation: generation)
            }
        }
    }

    private func open(origin: Origin, cookieHeader: String, generation: Int) {
        opening = false
        let opened = transport.open(
            origin: origin,
            cookieHeader: cookieHeader,
            onEvent: { [weak self] event in
                self?.receive(event, origin: origin, generation: generation)
            },
            onClose: { [weak self] reason in
                self?.streamClosed(reason, origin: origin, generation: generation)
            }
        )
        guard isCurrent(generation: generation, origin: origin) else {
            opened?.cancel()
            return
        }
        connection = opened
        if opened == nil {
            state = .unavailable(origin: origin.canonicalKey, failure: .unavailable)
            scheduleRetry(origin: origin)
        }
    }

    private func receive(_ event: ChatEvent, origin: Origin, generation: Int) {
        guard isCurrent(generation: generation, origin: origin) else { return }

        switch event {
        case .snapshot(let active):
            // The snapshot is the baseline. Active streams it lists are
            // currently working; events before it are never used to invent a
            // working chat from a previous connection's tail.
            snapshotReceived = true
            activeChatIDs = active
            state = .live(origin: origin.canonicalKey, activeChatIDs: active)
            retryAttempt = 0
            cancelRetry()

        case .streamingStarted(let chatID):
            guard snapshotReceived else { return }
            activeChatIDs.insert(chatID)
            state = .live(origin: origin.canonicalKey, activeChatIDs: activeChatIDs)

        case .streamingDone(let chatID):
            guard snapshotReceived else { return }
            activeChatIDs.remove(chatID)
            state = .live(origin: origin.canonicalKey, activeChatIDs: activeChatIDs)

        case .resultReady(let chatID, _, _, _):
            guard snapshotReceived else { return }
            // A finished result is no longer a running turn, even if the
            // stream's own done event is delayed or lost.
            activeChatIDs.remove(chatID)
            state = .live(origin: origin.canonicalKey, activeChatIDs: activeChatIDs)

        case .chatRead, .ignored:
            break
        }
    }

    private func streamClosed(_ reason: EventStreamCloseReason, origin: Origin, generation: Int) {
        guard isCurrent(generation: generation, origin: origin) else { return }
        connection = nil
        snapshotReceived = false
        activeChatIDs = []
        opening = false
        self.generation += 1
        let failure: ChatActivityFailure = reason == .unauthorized ? .notSignedIn : .unavailable
        state = .unavailable(origin: origin.canonicalKey, failure: failure)
        scheduleRetry(origin: origin)
    }

    private func isCurrent(generation: Int, origin: Origin) -> Bool {
        started
            && generation == self.generation
            && context.isConnected
            && currentOrigin?.canonicalKey == origin.canonicalKey
            && context.currentOrigin?.canonicalKey == origin.canonicalKey
    }

    private func scheduleRetry(origin: Origin) {
        guard started, context.isConnected, connection == nil, currentOrigin?.canonicalKey == origin.canonicalKey else { return }
        cancelRetry()
        let delay = Self.retryDelays[min(retryAttempt, Self.retryDelays.count - 1)]
        retryAttempt += 1
        let item = DispatchWorkItem { [weak self] in
            guard let self,
                  self.started,
                  self.context.isConnected,
                  self.currentOrigin?.canonicalKey == origin.canonicalKey,
                  self.connection == nil else { return }
            self.retryWorkItem = nil
            self.openIfPossible(origin: origin)
        }
        retryWorkItem = item
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: item)
    }

    private func cancelRetry() {
        retryWorkItem?.cancel()
        retryWorkItem = nil
    }

    private func resetStream(_ newState: ChatActivityState) {
        generation += 1
        connection?.cancel()
        connection = nil
        snapshotReceived = false
        activeChatIDs = []
        opening = false
        state = newState
    }
}
