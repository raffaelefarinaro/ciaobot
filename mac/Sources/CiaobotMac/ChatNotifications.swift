import Combine
import Foundation
import WebKit

/// Why notifications cannot be used. A bare executable is a fact this process
/// can establish. A registration or authorization failure is a technical
/// result with the exact macOS message; it is not the same as the user denying
/// permission, and it is not proof that the build is unsigned.
enum ChatNotificationUnavailableReason: Equatable {
    case unbundled
    case permissionRequestFailed(String)
    case unknownAuthorization(String)
}

/// What the app knows about native chat notifications. `off` means the user
/// has not opted in; `on` means an explicit opt-in and an OS permission both
/// hold; `denied` means macOS actually reported denial; `deliveryOff` means
/// macOS allowed notifications but both visible delivery channels for this app
/// are disabled, so a plain "On" would be false; `unavailable` carries the
/// exact non-denial reason the client can stand behind.
enum ChatNotificationState: Equatable {
    case off
    case on
    case denied
    case deliveryOff
    case unavailable(ChatNotificationUnavailableReason)
}

/// The result of reading the OS state. `.unknown` is a future or unrecognized
/// macOS status, never silently folded into `.denied`. `.deliveryOff` is a
/// recognized permission whose visible channels are all off.
enum ChatNotificationAuthorization: Equatable {
    case notDetermined
    case authorized
    case denied
    case deliveryOff
    case unavailable
    case unknown(String)
}

/// The result of the one explicit permission request. A technical error keeps
/// its exact message instead of becoming a denial.
enum ChatNotificationAuthorizationRequest: Equatable {
    case granted
    case denied
    case failed(String)
    case unavailable
}

/// Whether the authenticated `/ws/events` socket is actually feeding
/// notifications. A plain "On" row is only truthful in `.ready`; every other
/// state says what the client knows without claiming delivery. `.retrying`
/// is used only for transport-level closes that may heal on their own.
enum ChatNotificationStreamStatus: Equatable {
    case notConnected
    case waitingForAuthentication
    case connecting
    case ready
    case retrying
    /// The server refused TLS/proxy/client authentication.
    case authenticationRefused
    /// The server rejected the exact `Origin` supplied by this client.
    case originRejected
    /// The socket was redirected; the client refused to follow and did not
    /// carry the cookie to the redirect target.
    case redirected
    /// The server sent a payload this client cannot decode.
    case protocolError
}

/// A notification may open exactly one chat on exactly one origin. The origin
/// is the canonical key of the connection that produced it, never a server
/// supplied URL.
struct ChatNotificationTarget: Equatable {
    let chatID: String
    let origin: String
}

struct ChatNotificationMessage: Equatable {
    let identifier: String
    let title: String
    /// The workspace name under the title, when the server serves more than
    /// one. Empty means no subtitle: a single-workspace install has nothing to
    /// disambiguate.
    let subtitle: String
    let body: String
    let target: ChatNotificationTarget
}

/// The OS notification surface. A fake is used by tests, so the model never
/// needs a real notification centre and never prompts from a test.
@MainActor
protocol NotificationPermissionClient: AnyObject {
    var isAvailable: Bool { get }
    /// Set by the controller. The client calls it on the main actor when the
    /// user clicks a delivered notification.
    var onActivation: ((ChatNotificationTarget) -> Void)? { get set }

    /// Installs the notification delegate without asking for permission.
    func prepare()
    func authorizationStatus(completion: @escaping (ChatNotificationAuthorization) -> Void)
    /// Only called after an explicit menu action. A technical error is
    /// returned as `.failed`, never collapsed into `.denied`.
    func requestAuthorization(completion: @escaping (ChatNotificationAuthorizationRequest) -> Void)
    func deliver(_ message: ChatNotificationMessage)
    /// Withdraws a delivered notification from Notification Center. Unknown
    /// identifiers are a no-op.
    func removeDelivered(identifier: String)
}

/// The user's explicit opt-in, remembered only after they click the menu item.
/// It is not a sign-in, a cookie store, or a launch target.
protocol NotificationPreferenceStoring: AnyObject {
    var isEnabled: Bool { get set }
}

final class UserDefaultsNotificationPreferenceStore: NotificationPreferenceStoring {
    static let defaultKey = "CiaobotChatNotificationsEnabled"

    private let defaults: UserDefaults
    private let key: String

    init(defaults: UserDefaults = .standard, key: String = UserDefaultsNotificationPreferenceStore.defaultKey) {
        self.defaults = defaults
        self.key = key
    }

    var isEnabled: Bool {
        get { defaults.bool(forKey: key) }
        set { defaults.set(newValue, forKey: key) }
    }
}

/// The menu surface for notifications. Kept separate from the controller so
/// `MenuBarController` does not need to know how the event socket works.
@MainActor
protocol NotificationMenuProviding: AnyObject {
    var state: ChatNotificationState { get }
    var statePublisher: AnyPublisher<ChatNotificationState, Never> { get }
    var streamStatus: ChatNotificationStreamStatus { get }
    var streamStatusPublisher: AnyPublisher<ChatNotificationStreamStatus, Never> { get }
    /// The single explicit user action from the menu.
    func performMenuAction()
    /// Called when the menu opens. It only re-reads OS state and retries a
    /// failed/late connection; it never prompts.
    func refreshIfNeeded()
    /// Ends the event socket with the menu-bar item's lifecycle.
    func stop()
}

/// Owns the opt-in and the `/ws/events` subscription that feeds native
/// notifications. The event socket is authenticated with a cookie selected
/// from this connection's own WebKit store for the exact selected origin.
///
/// The model ignores every event until the server's snapshot arrives. A
/// snapshot is a baseline, never a backlog to announce, and it is also what
/// makes a reconnect after a gap safe: no earlier event is replayed or
/// converted into a banner.
@MainActor
final class ChatNotificationController: ObservableObject, NotificationMenuProviding {
    @Published private(set) var state: ChatNotificationState = .off
    var statePublisher: AnyPublisher<ChatNotificationState, Never> { $state.eraseToAnyPublisher() }
    @Published private(set) var streamStatus: ChatNotificationStreamStatus = .notConnected
    var streamStatusPublisher: AnyPublisher<ChatNotificationStreamStatus, Never> { $streamStatus.eraseToAnyPublisher() }

    private static let retryDelays: [TimeInterval] = [2, 5, 15, 30, 60]

    private let status: AppSessionStatus
    private let permission: NotificationPermissionClient
    private let preferences: NotificationPreferenceStoring
    private let transport: EventStreamTransport
    private let cookieProviderFactory: (WKWebsiteDataStore) -> ChatCookieProviding
    private let presentWindow: () -> Void
    private let openChat: (String) -> Bool
    private let now: () -> Date
    private let hostStoreTracking: HostStoreTrackingBundle?

    private var started = false
    private var subscription: AnyCancellable?
    /// The connection value this controller last acted on. `@Published` replays
    /// its current value to every new subscriber, so a session that is already
    /// connected when notifications start produces an emission that is not a
    /// change. Reopening the stream for it would create a second socket for one
    /// connection.
    private var observedConnection: AppSessionStatus.Connection = .disconnected
    private var connection: EventStreamConnection?
    private var generation = 0
    private var snapshotReceived = false
    private var activeChatIDs: Set<String> = []
    /// The last result announced per origin and chat. A new
    /// `chat_streaming_started` (or a snapshot saying that chat is streaming)
    /// clears it, so a genuinely repeated answer is still announced, while the
    /// same `chat_result_ready` delivered twice — including after a reconnect —
    /// is not. The cache is small and bounded.
    private var lastResultFingerprint: [String: String] = [:]
    private var resultFingerprintOrder: [String] = []
    private static let maximumRememberedResults = 256
    private var retryWorkItem: DispatchWorkItem?
    private var retryAttempt = 0
    private var permissionRequestInFlight = false
    /// A validated notification target from a click that happened before the
    /// app was connected. It is never an autoconnect request: it waits for the
    /// user to connect the exact same origin, then opens the same-origin chat.
    private var pendingColdLaunchTarget: ChatNotificationTarget?

    init(
        status: AppSessionStatus,
        permission: NotificationPermissionClient,
        preferences: NotificationPreferenceStoring = UserDefaultsNotificationPreferenceStore(),
        transport: EventStreamTransport? = nil,
        cookieProviderFactory: @escaping (WKWebsiteDataStore) -> ChatCookieProviding = { WebKitCookieProvider(dataStore: $0) },
        presentWindow: @escaping () -> Void,
        openChat: @escaping (String) -> Bool,
        now: @escaping () -> Date = Date.init,
        hostStoreTracking: HostStoreTrackingBundle? = nil
    ) {
        self.status = status
        self.permission = permission
        self.preferences = preferences
        self.transport = transport ?? URLSessionEventStreamTransport()
        self.cookieProviderFactory = cookieProviderFactory
        self.presentWindow = presentWindow
        self.openChat = openChat
        self.now = now
        self.hostStoreTracking = hostStoreTracking
        permission.onActivation = { [weak self] target in
            self?.handleActivation(target)
        }
    }

    func start() {
        guard !started else { return }
        started = true
        permission.prepare()

        guard permission.isAvailable else {
            state = .unavailable(.unbundled)
            streamStatus = .notConnected
            return
        }

        // The publisher hands a new subscriber the current value. Baseline it
        // here so only a real transition counts as a connection change, and the
        // persisted opt-in's connect is the only attempt for this start.
        observedConnection = status.connection
        subscription = status.$connection.sink { [weak self] connection in
            Task { @MainActor in self?.connectionChanged(to: connection) }
        }

        guard preferences.isEnabled else {
            state = .off
            streamStatus = .notConnected
            // Reading the current OS state does not prompt. It keeps a user
            // who denied permission in System Settings from being told the
            // row can turn notifications on when it cannot.
            permission.authorizationStatus { [weak self] authorization in
                Task { @MainActor in
                    guard let self, !self.preferences.isEnabled, self.state == .off else { return }
                    switch authorization {
                    case .denied:
                        self.state = .denied
                    case .deliveryOff:
                        // macOS allows notifications, but this app's banners
                        // and Notification Center entries are both off. That
                        // is not something the menu may call "On".
                        self.state = .deliveryOff
                    case .unavailable:
                        self.state = .unavailable(.unbundled)
                    case .unknown(let reason):
                        self.state = .unavailable(.unknownAuthorization(reason))
                    case .authorized, .notDetermined:
                        break
                    }
                }
            }
            return
        }

        // A remembered opt-in is re-validated against the OS without
        // prompting. If permission was reset, the user has to opt in again.
        permission.authorizationStatus { [weak self] authorization in
            Task { @MainActor in self?.resolvePersistedOptIn(authorization) }
        }
    }

    func stop() {
        guard started else { return }
        started = false
        subscription?.cancel()
        subscription = nil
        cancelRetry()
        resetStream()
        clearRememberedResults()
        permissionRequestInFlight = false
        streamStatus = .notConnected
    }

    func performMenuAction() {
        guard started, !permissionRequestInFlight else { return }

        switch state {
        case .on:
            preferences.isEnabled = false
            state = .off
            cancelRetry()
            resetStream()
            clearRememberedResults()
            streamStatus = .notConnected
        case .off:
            guard permission.isAvailable else {
                state = .unavailable(.unbundled)
                return
            }
            // This is the only path that may show an OS permission prompt.
            permissionRequestInFlight = true
            permission.requestAuthorization { [weak self] result in
                Task { @MainActor in
                    guard let self else { return }
                    self.permissionRequestInFlight = false
                    switch result {
                    case .granted:
                        self.preferences.isEnabled = true
                        self.state = .on
                        self.connectIfPossible()
                    case .denied:
                        self.preferences.isEnabled = false
                        self.state = .denied
                        self.cancelRetry()
                        self.resetStream()
                    case .failed(let reason):
                        // Keep the technical result exactly as macOS reported
                        // it. It is not a denial, and it is not proof that the
                        // build is unsigned.
                        self.preferences.isEnabled = false
                        self.state = .unavailable(.permissionRequestFailed(reason))
                        self.cancelRetry()
                        self.resetStream()
                    case .unavailable:
                        self.preferences.isEnabled = false
                        self.state = .unavailable(.unbundled)
                        self.cancelRetry()
                        self.resetStream()
                    }
                }
            }
        case .denied, .deliveryOff:
            // The menu row is disabled while blocked, but a caller that
            // reaches this path still gets a safe status re-check rather than a
            // second authorization request.
            refreshAuthorizationOnly()
        case .unavailable:
            break
        }
    }

    func refreshIfNeeded() {
        guard started else { return }
        guard permission.isAvailable else {
            state = .unavailable(.unbundled)
            return
        }
        refreshAuthorizationOnly()
        if state == .on, connection == nil {
            connectIfPossible()
        }
    }

    private func refreshAuthorizationOnly() {
        permission.authorizationStatus { [weak self] authorization in
            Task { @MainActor in
                guard let self else { return }
                switch authorization {
                case .authorized:
                    // Permission was granted outside the app. Opting in is
                    // still a click on the menu item, not an implicit switch.
                    if self.state == .denied || self.state == .deliveryOff {
                        self.state = .off
                    } else if case .unavailable = self.state {
                        self.state = .off
                    }
                    if self.state == .on {
                        self.connectIfPossible()
                    }
                case .denied:
                    // Only an actual `.denied` from macOS reaches this branch.
                    if self.state == .on {
                        self.preferences.isEnabled = false
                        self.cancelRetry()
                        self.resetStream()
                    }
                    self.state = .denied
                case .deliveryOff:
                    // The permission still exists, but nothing would be shown.
                    // Count it as blocked exactly like a denial, so no socket
                    // runs and no menu row claims delivery.
                    if self.state == .on {
                        self.preferences.isEnabled = false
                        self.cancelRetry()
                        self.resetStream()
                    }
                    self.state = .deliveryOff
                case .notDetermined:
                    // Permission was reset outside the app. Stop claiming
                    // notifications are on and require a fresh click. Keep a
                    // technical registration failure, though: the OS can still
                    // report notDetermined after refusing to register, and
                    // silently replacing that error with "Turn On…" would
                    // invite the same failing prompt again.
                    if self.state == .on {
                        self.preferences.isEnabled = false
                        self.cancelRetry()
                        self.resetStream()
                    }
                    if case .unavailable(.permissionRequestFailed) = self.state {
                        break
                    }
                    self.state = .off
                case .unavailable:
                    self.preferences.isEnabled = false
                    self.cancelRetry()
                    self.resetStream()
                    self.state = .unavailable(.unbundled)
                case .unknown(let reason):
                    if self.state == .on {
                        self.preferences.isEnabled = false
                        self.cancelRetry()
                        self.resetStream()
                    }
                    self.state = .unavailable(.unknownAuthorization(reason))
                }
            }
        }
    }

    private func resolvePersistedOptIn(_ authorization: ChatNotificationAuthorization) {
        guard started else { return }
        switch authorization {
        case .authorized:
            state = .on
            connectIfPossible()
        case .denied:
            preferences.isEnabled = false
            state = .denied
        case .deliveryOff:
            // The stored opt-in is not proof of delivery when macOS has every
            // visible channel switched off. The user has to fix Settings and
            // then opt in again explicitly.
            preferences.isEnabled = false
            state = .deliveryOff
        case .notDetermined:
            // A remembered preference without permission is not consent for
            // this launch. The user must click the menu item again.
            preferences.isEnabled = false
            state = .off
        case .unavailable:
            preferences.isEnabled = false
            state = .unavailable(.unbundled)
        case .unknown(let reason):
            preferences.isEnabled = false
            state = .unavailable(.unknownAuthorization(reason))
        }
    }

    private func connectionChanged(to connection: AppSessionStatus.Connection) {
        // A subscribe-time replay or a repeated report of the same connection is
        // not a change: resetting here tore down the live socket and made a
        // second cookie request and socket for the same connect.
        guard connection != observedConnection else { return }
        observedConnection = connection
        cancelRetry()
        resetStream()
        guard started else { return }
        openPendingTargetIfPossible()
        guard state == .on else { return }
        connectIfPossible()
    }

    private func connectIfPossible() {
        guard started,
              state == .on,
              status.isConnected,
              let origin = status.currentOrigin,
              connection == nil,
              let url = EventStreamEndpoint.url(origin: origin) else {
            if started, state == .on, !status.isConnected {
                streamStatus = .notConnected
            }
            return
        }

        cancelRetry()
        generation += 1
        let generation = self.generation
        snapshotReceived = false
        activeChatIDs = []

        let acquisition = hostStoreTracking?.acquire(origin: origin, store: status.dataStore)
        if case .refused? = acquisition {
            streamStatus = .retrying
            scheduleRetry()
            return
        }

        guard let dataStore = status.dataStore else {
            // `/ws/events` always requires a session cookie, including from a
            // loopback peer. Without this connection's store there is nothing
            // safe to send, so no handshake is attempted.
            streamStatus = .waitingForAuthentication
            scheduleRetry()
            return
        }

        streamStatus = .connecting
        let operation: UUID? = { if case .operation(let id)? = acquisition { id } else { nil } }()
        let registry = hostStoreTracking?.registry
        cookieProviderFactory(dataStore).cookies(for: url) { [weak self] cookies in
            Task { @MainActor in
                if let operation { guard registry?.claimCompletion(id: operation) == true else { return } }
                defer { if let operation { registry?.finishOperation(id: operation) } }
                guard let self else { return }
                guard self.isCurrent(generation: generation, origin: origin) else { return }
                guard let cookieHeader = EventStreamEndpoint.cookieHeader(
                    for: url,
                    cookies: cookies,
                    now: self.now()
                ) else {
                    // `/ws/events` rejects an unauthenticated handshake for
                    // every origin. Do not send one and wait for the web view
                    // to finish signing in.
                    self.streamStatus = .waitingForAuthentication
                    self.scheduleRetry()
                    return
                }
                self.open(origin: origin, cookieHeader: cookieHeader, generation: generation)
            }
        }
    }

    private func open(origin: Origin, cookieHeader: String, generation: Int) {
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
            streamStatus = .retrying
            scheduleRetry()
        } else {
            streamStatus = .connecting
        }
    }

    private func receive(_ event: ChatEvent, origin: Origin, generation: Int) {
        guard isCurrent(generation: generation, origin: origin) else { return }

        switch event {
        case .snapshot(let active):
            snapshotReceived = true
            streamStatus = .ready
            activeChatIDs = active
            // A chat in the snapshot is running a new turn. Its old result
            // must not suppress this turn's completion.
            for chatID in active {
                forgetRememberedResult(origin: origin, chatID: chatID)
            }
            retryAttempt = 0
            cancelRetry()

        case .streamingStarted(let chatID):
            guard snapshotReceived else { return }
            activeChatIDs.insert(chatID)
            // A start is the boundary between two turns: the same text can be
            // a legitimate answer twice.
            forgetRememberedResult(origin: origin, chatID: chatID)

        case .streamingDone(let chatID):
            guard snapshotReceived else { return }
            activeChatIDs.remove(chatID)

        case .resultReady(let chatID, let title, let snippet, let workspace):
            // Before the snapshot the client is still catching up. Those
            // events are not a fresh result and must never become a banner.
            guard snapshotReceived else { return }
            activeChatIDs.remove(chatID)
            announce(
                chatID: chatID,
                title: title,
                snippet: snippet,
                workspace: workspace,
                origin: origin
            )

        case .chatRead(let chatID):
            // The chat was read on some device, so its banner is stale.
            // Withdrawing is idempotent and announces nothing, so it does not
            // wait for the snapshot. The remembered result is kept: a replay
            // of the same result must not bring the banner back.
            guard ChatLink.isValidChatID(chatID) else { return }
            permission.removeDelivered(identifier: Self.notificationIdentifier(
                origin: origin.canonicalKey,
                chatID: chatID
            ))

        case .ignored:
            break
        }
    }

    private func announce(
        chatID: String,
        title: String,
        snippet: String,
        workspace: String,
        origin: Origin
    ) {
        guard ChatLink.isValidChatID(chatID) else { return }

        let key = Self.resultKey(origin: origin, chatID: chatID)
        let fingerprint = title + "\u{1F}" + snippet
        guard lastResultFingerprint[key] != fingerprint else { return }
        rememberResult(key: key, fingerprint: fingerprint)

        let cleanTitle = MenuTextSanitizer.sanitize(title, limit: 110, fallback: "Ciaobot")
        let cleanBody = MenuTextSanitizer.sanitize(snippet, limit: 240, fallback: "Your chat finished.")
        // The server only sends a workspace when it serves more than one, so a
        // subtitle that arrives is always worth showing. It is cleaned exactly
        // like a menu label: untrusted text cannot forge a line in a banner.
        let cleanSubtitle = MenuTextSanitizer.sanitize(workspace, limit: 48, fallback: "")
        let target = ChatNotificationTarget(chatID: chatID, origin: origin.canonicalKey)
        permission.deliver(ChatNotificationMessage(
            identifier: Self.notificationIdentifier(origin: origin.canonicalKey, chatID: chatID),
            title: cleanTitle,
            subtitle: cleanSubtitle,
            body: cleanBody,
            target: target
        ))
    }

    private func rememberResult(key: String, fingerprint: String) {
        lastResultFingerprint[key] = fingerprint
        resultFingerprintOrder.removeAll { $0 == key }
        resultFingerprintOrder.append(key)
        while resultFingerprintOrder.count > Self.maximumRememberedResults {
            let evicted = resultFingerprintOrder.removeFirst()
            lastResultFingerprint.removeValue(forKey: evicted)
        }
    }

    private func forgetRememberedResult(origin: Origin, chatID: String) {
        let key = Self.resultKey(origin: origin, chatID: chatID)
        lastResultFingerprint.removeValue(forKey: key)
        resultFingerprintOrder.removeAll { $0 == key }
    }

    private func clearRememberedResults() {
        lastResultFingerprint = [:]
        resultFingerprintOrder = []
    }

    private static func resultKey(origin: Origin, chatID: String) -> String {
        origin.canonicalKey + "\u{0}" + chatID
    }

    private func streamClosed(_ reason: EventStreamCloseReason, origin: Origin, generation: Int) {
        guard isCurrent(generation: generation, origin: origin) else { return }
        cancelRetry()
        connection = nil
        snapshotReceived = false
        activeChatIDs = []
        self.generation += 1

        switch reason {
        case .unauthorized:
            // The socket's authenticated handshake was refused. The next safe
            // step is a sign-in, not a generic "reconnecting" claim. Retry
            // after the backoff so a completed web sign-in can heal it.
            streamStatus = .waitingForAuthentication
            scheduleRetry()
        case .forbiddenOrigin:
            // The server rejected the exact Origin this client supplied.
            // Retrying the same handshake would not change that.
            streamStatus = .originRejected
        case .redirected:
            // The client refused the redirect and did not send the cookie to
            // the new target. Do not hide that behind "reconnecting".
            streamStatus = .redirected
        case .authenticationRefused:
            // A non-server-trust TLS/proxy/client-auth challenge was refused.
            streamStatus = .authenticationRefused
        case .invalidMessage:
            // The server sent a payload this client cannot decode. A tight
            // retry loop would only recreate the same failure.
            streamStatus = .protocolError
        case .ended, .failed:
            // A transport-level close can heal on its own, so it is the only
            // family that keeps the bounded automatic reconnect.
            streamStatus = .retrying
            scheduleRetry()
        }
    }

    private func isCurrent(generation: Int, origin: Origin) -> Bool {
        started
            && state == .on
            && generation == self.generation
            && status.isConnected
            && status.currentOrigin?.canonicalKey == origin.canonicalKey
    }

    private func scheduleRetry() {
        guard started, state == .on, status.isConnected, connection == nil else { return }
        cancelRetry()
        let delay = Self.retryDelays[min(retryAttempt, Self.retryDelays.count - 1)]
        retryAttempt += 1
        let item = DispatchWorkItem { [weak self] in
            guard let self, self.started, self.state == .on else { return }
            self.retryWorkItem = nil
            self.connectIfPossible()
        }
        retryWorkItem = item
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: item)
    }

    private func cancelRetry() {
        retryWorkItem?.cancel()
        retryWorkItem = nil
    }

    private func resetStream() {
        generation += 1
        connection?.cancel()
        connection = nil
        snapshotReceived = false
        activeChatIDs = []
        streamStatus = .notConnected
    }

    /// A notification click is only a navigation request. It is refused unless
    /// the target itself is a valid canonical origin and safe chat id. If the
    /// app is already connected to that origin it opens at once; otherwise the
    /// target is retained for the next manual connection to the exact same
    /// origin. It never signs in, reconnects, or accepts a server-supplied
    /// destination.
    private func handleActivation(_ target: ChatNotificationTarget) {
        guard Self.isValidTarget(target) else { return }
        presentWindow()
        pendingColdLaunchTarget = target
        openPendingTargetIfPossible()
    }

    private func openPendingTargetIfPossible() {
        guard let target = pendingColdLaunchTarget,
              status.isConnected,
              let origin = status.currentOrigin,
              origin.canonicalKey == target.origin,
              ChatLink.isValidChatID(target.chatID) else { return }
        pendingColdLaunchTarget = nil
        _ = openChat(target.chatID)
    }

    private static func isValidTarget(_ target: ChatNotificationTarget) -> Bool {
        guard let origin = Origin(target.origin),
              origin.canonicalKey == target.origin,
              ChatLink.isValidChatID(target.chatID),
              ChatLink.url(origin: origin, chatID: target.chatID) != nil else {
            return false
        }
        return true
    }

    /// Stable per chat and origin. Two servers cannot overwrite each other's
    /// notification for a chat id that happens to be the same.
    static func notificationIdentifier(origin: String, chatID: String) -> String {
        var hash: UInt64 = 14_695_981_039_346_656_037
        for byte in (origin + "\u{0}" + chatID).utf8 {
            hash ^= UInt64(byte)
            hash = hash &* 1_099_511_628_211
        }
        return "ciaobot-chat-\(String(hash, radix: 16))"
    }
}
