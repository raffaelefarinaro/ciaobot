import Foundation
import Combine
import WebKit

/// One row from `GET /api/menubar-chats`. The server sends only these fields;
/// the client displays exactly them and never synthesises a chat.
struct MenubarChat: Equatable, Decodable {
    let chatID: String
    let title: String
    let workspace: String
    let lastActivityAt: String
    let unread: Bool
    let needsInput: Bool

    enum CodingKeys: String, CodingKey {
        case chatID = "chat_id"
        case title
        case workspace
        case lastActivityAt = "last_activity_at"
        case unread
        case needsInput = "needs_input"
    }

    var needsAttention: Bool { unread || needsInput }
}

/// The validated body of the menu-bar endpoint. `attentionCount` is the
/// server's deduplicated count over every non-archived chat, not a count of
/// the (possibly truncated) rows in `chats`. It is nil when the server omitted
/// the count or sent one that cannot be reconciled with the rows; the rows are
/// still displayed without a numeric claim.
struct ChatListPayload: Equatable {
    let chats: [MenubarChat]
    let attentionCount: Int?
    /// True when the server says this origin serves more than one workspace.
    /// It is the server's own fact about its registry, not an inference from
    /// the (possibly truncated) page: the tray needs it to head rows that this
    /// time all come from one workspace. An older server omits the field, so
    /// its absence means "unknown", not "one".
    let multipleWorkspaces: Bool

    init(chats: [MenubarChat], attentionCount: Int?, multipleWorkspaces: Bool = false) {
        self.chats = chats
        self.attentionCount = attentionCount
        self.multipleWorkspaces = multipleWorkspaces
    }

    private struct Raw: Decodable {
        let chats: [MenubarChat]
        let attentionCount: Int?
        let multipleWorkspaces: Bool?

        enum CodingKeys: String, CodingKey {
            case chats
            case attentionCount = "attention_count"
            case multipleWorkspaces = "multiple_workspaces"
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            chats = try container.decode([MenubarChat].self, forKey: .chats)
            // A missing, null, or wrongly typed count is not allowed to erase
            // otherwise valid rows.
            attentionCount = (try? container.decodeIfPresent(Int.self, forKey: .attentionCount)) ?? nil
            // Same for the workspace fact: an older server, or a malformed
            // value, leaves the tray on its own fallback rather than failing
            // the whole feed.
            multipleWorkspaces = (try? container.decodeIfPresent(Bool.self, forKey: .multipleWorkspaces)) ?? nil
        }
    }

    /// Keeps every structurally valid row and treats the count as advisory.
    /// Undocumented row ordering must not discard the feed: a count is only
    /// published when it is non-negative and compatible with the rows. A list
    /// shorter than the requested limit was not truncated, so its count must
    /// equal the visible flagged rows; a full list may be truncated and only
    /// needs to be at least the visible flagged rows.
    static func decode(_ data: Data, limit: Int = 50) -> Result<ChatListPayload, ChatListFailure> {
        guard let raw = try? JSONDecoder().decode(Raw.self, from: data) else {
            return .failure(.invalidResponse)
        }
        // The endpoint itself caps at 50. More than that is not a feed this
        // client can safely bound; that is a structural error, not a count
        // inconsistency.
        guard raw.chats.count <= 50 else {
            return .failure(.invalidResponse)
        }
        let boundedLimit = max(1, min(50, limit))
        var seen = Set<String>()
        let chats = raw.chats.filter { chat in
            guard !chat.chatID.isEmpty else { return false }
            return seen.insert(chat.chatID).inserted
        }
        let flagged = chats.filter(\.needsAttention).count
        let truncated = raw.chats.count >= boundedLimit
        let count: Int?
        if let rawCount = raw.attentionCount, rawCount >= 0 {
            if chats.isEmpty {
                count = rawCount == 0 ? 0 : nil
            } else if truncated {
                count = rawCount >= flagged ? rawCount : nil
            } else {
                count = rawCount == flagged ? rawCount : nil
            }
        } else {
            count = nil
        }
        return .success(ChatListPayload(
            chats: chats,
            attentionCount: count,
            multipleWorkspaces: raw.multipleWorkspaces ?? false
        ))
    }
}

struct ChatListSnapshot: Equatable {
    let origin: String
    let chats: [MenubarChat]
    let attentionCount: Int?
    /// The server's own statement that it serves more than one workspace,
    /// carried from the payload. False when an older server did not say, which
    /// leaves the menu on the page-based fallback.
    let multipleWorkspaces: Bool
    let fetchedAt: Date

    init(
        origin: String,
        chats: [MenubarChat],
        attentionCount: Int?,
        multipleWorkspaces: Bool = false,
        fetchedAt: Date
    ) {
        self.origin = origin
        self.chats = chats
        self.attentionCount = attentionCount
        self.multipleWorkspaces = multipleWorkspaces
        self.fetchedAt = fetchedAt
    }
}

enum ChatListFailure: Error, Equatable {
    /// The server answered 401/403, or a remote origin has no session cookie
    /// in its own WebKit store. No request is sent in the latter case.
    case notSignedIn
    /// A network failure, timeout, or non-200 response.
    case requestFailed
    /// A 2xx body that is not the expected, internally consistent JSON shape.
    case invalidResponse
    /// URLSession was asked to follow a redirect. The client refuses, so no
    /// credential is ever carried to a new origin.
    case redirected
    /// The request was superseded or the connection changed.
    case cancelled
}

enum ChatListState: Equatable {
    case disconnected
    case loading
    case loaded(ChatListSnapshot)
    case unavailable(ChatListFailure)
}

/// Builds the fixed endpoint on the exact selected origin. The path and query
/// are constants; no server-provided value is used to choose a request target.
enum ChatListEndpoint {
    static let defaultLimit = 12
    static let path = "/api/menubar-chats"

    static func url(origin: Origin, limit: Int = defaultLimit) -> URL? {
        guard var components = URLComponents(url: origin.url, resolvingAgainstBaseURL: false) else {
            return nil
        }
        components.percentEncodedPath = path
        components.percentEncodedQuery = "limit=\(max(1, min(50, limit)))"
        components.fragment = nil
        return components.url
    }

    static func matches(_ url: URL, origin: Origin) -> Bool {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let scheme = components.scheme?.lowercased(),
              let host = components.host?.lowercased(), !host.isEmpty,
              components.user == nil, components.password == nil,
              components.percentEncodedPath == path,
              components.fragment == nil,
              let query = components.queryItems,
              query.count == 1,
              query[0].name == "limit",
              let rawLimit = query[0].value,
              let limit = Int(rawLimit), (1...50).contains(limit) else { return false }
        let port = components.port ?? (scheme == "https" ? 443 : 80)
        return origin.matches(scheme: scheme, host: host, port: port)
    }
}

/// Builds a chat URL inside the exact selected origin. A chat id is one path
/// segment: only unreserved ASCII is accepted, so `..`, `/`, `?`, `#`, control
/// characters, and percent-encoded traversal cannot escape `/chat/`.
enum ChatLink {
    static func isValidChatID(_ chatID: String) -> Bool {
        guard !chatID.isEmpty, chatID.utf8.count <= 200 else { return false }
        return chatID.utf8.allSatisfy { byte in
            (48...57).contains(byte) || (65...90).contains(byte) || (97...122).contains(byte)
                || byte == 45 || byte == 95
        }
    }

    static func url(origin: Origin, chatID: String) -> URL? {
        guard isValidChatID(chatID),
              var components = URLComponents(url: origin.url, resolvingAgainstBaseURL: false) else {
            return nil
        }
        components.percentEncodedPath = "/chat/\(chatID)"
        components.percentEncodedQuery = nil
        components.fragment = nil
        guard let url = components.url,
              origin.navigation(url, userInitiated: false) == .inWindow else { return nil }
        return url
    }
}

/// Selects only the cookies a browser would send to this exact URL. WebKit
/// cookies are host-scoped, not port-scoped, so every candidate is re-checked
/// against the literal request host, path, scheme, and expiry before it is
/// attached. The value is never logged or persisted by this client.
enum SessionCookiePolicy {
    static let sessionCookieName = "ciao_session"

    static func applicableCookies(for url: URL, cookies: [HTTPCookie], now: Date = Date()) -> [HTTPCookie] {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let scheme = components.scheme?.lowercased(),
              let rawHost = components.host?.lowercased(), !rawHost.isEmpty else { return [] }
        let host = normalizedHost(rawHost)
        let path = components.percentEncodedPath.isEmpty ? "/" : components.percentEncodedPath
        return cookies.filter { cookie in
            guard cookie.isSecure == false || scheme == "https",
                  cookie.expiresDate.map({ $0 > now }) ?? true,
                  domainMatches(host: host, domain: cookie.domain),
                  pathMatches(requestPath: path, cookiePath: cookie.path),
                  isValidToken(cookie.name),
                  isSafeValue(cookie.value) else { return false }
            return true
        }
    }

    static func header(for url: URL, cookies: [HTTPCookie], now: Date = Date()) -> String? {
        // Only the session cookie is a credential this feed needs. Other
        // cookies that happen to match are left behind rather than copied into
        // a native request.
        let applicable = applicableCookies(for: url, cookies: cookies, now: now)
            .filter { $0.name == sessionCookieName }
            .sorted {
                if $0.path.count != $1.path.count { return $0.path.count > $1.path.count }
                return $0.name < $1.name
            }
        guard !applicable.isEmpty else { return nil }
        return applicable.map { "\($0.name)=\($0.value)" }.joined(separator: "; ")
    }

    static func containsSessionCookie(for url: URL, cookies: [HTTPCookie], now: Date = Date()) -> Bool {
        applicableCookies(for: url, cookies: cookies, now: now)
            .contains { $0.name == sessionCookieName }
    }

    private static func domainMatches(host: String, domain: String) -> Bool {
        var candidate = domain.lowercased()
        if candidate.hasPrefix(".") {
            let bare = normalizedHost(String(candidate.dropFirst()))
            return host == bare || host.hasSuffix("." + bare)
        }
        candidate = normalizedHost(candidate)
        return host == candidate
    }

    private static func normalizedHost(_ host: String) -> String {
        guard host.hasPrefix("["), host.hasSuffix("]") else { return host }
        return String(host.dropFirst().dropLast())
    }

    private static func pathMatches(requestPath: String, cookiePath: String) -> Bool {
        guard cookiePath.hasPrefix("/") else { return false }
        if requestPath == cookiePath { return true }
        guard requestPath.hasPrefix(cookiePath) else { return false }
        return cookiePath.hasSuffix("/") || requestPath.dropFirst(cookiePath.count).first == "/"
    }

    private static func isValidToken(_ name: String) -> Bool {
        guard !name.isEmpty else { return false }
        return name.utf8.allSatisfy { byte in
            (48...57).contains(byte) || (65...90).contains(byte) || (97...122).contains(byte)
                || [33, 35, 36, 37, 38, 39, 42, 43, 45, 46, 94, 95, 96, 124, 126].contains(Int(byte))
        }
    }

    private static func isSafeValue(_ value: String) -> Bool {
        // WebKit normally hands back a validated value. Refuse anything that
        // could break the header or inject a second one rather than "repairing"
        // a cookie this client never set.
        return value.utf8.allSatisfy { byte in
            byte > 0x20 && byte < 0x7F && byte != 0x3B && byte != 0x2C && byte != 0x22 && byte != 0x5C
        }
    }
}

protocol ChatCookieProviding: AnyObject {
    func cookies(for url: URL, completion: @escaping ([HTTPCookie]) -> Void)
}

/// Reads cookies from the connection's own data store. It never uses
/// `WKWebsiteDataStore.default()` and never inspects WebKit's on-disk bytes.
final class WebKitCookieProvider: ChatCookieProviding {
    private let dataStore: WKWebsiteDataStore

    init(dataStore: WKWebsiteDataStore) {
        self.dataStore = dataStore
    }

    func cookies(for url: URL, completion: @escaping ([HTTPCookie]) -> Void) {
        dataStore.httpCookieStore.getAllCookies(completion)
    }
}

protocol ChatListTransport: AnyObject {
    /// Returns a cancellation closure. Completion may arrive after cancel and
    /// is then ignored by the caller's generation check.
    @discardableResult
    func fetch(
        origin: Origin,
        limit: Int,
        cookieHeader: String?,
        completion: @escaping (Result<ChatListPayload, ChatListFailure>) -> Void
    ) -> (() -> Void)?
}

/// The production transport. It uses an ephemeral URL session with no cookie
/// storage, no credential storage, no cache, and no default headers. The only
/// credential is the cookie value the caller selected for this exact URL.
final class URLSessionChatListTransport: NSObject, ChatListTransport {
    static let maximumResponseBytes = 512 * 1024

    /// Only normal TLS server-trust validation is accepted. Any other
    /// challenge (client certificate, HTTP auth, proxy auth) is cancelled.
    static func acceptsServerTrust(_ authenticationMethod: String) -> Bool {
        authenticationMethod == NSURLAuthenticationMethodServerTrust
    }

    private let configuration: URLSessionConfiguration

    init(configuration: URLSessionConfiguration = .ephemeral) {
        self.configuration = configuration
        super.init()
    }

    func fetch(
        origin: Origin,
        limit: Int,
        cookieHeader: String?,
        completion: @escaping (Result<ChatListPayload, ChatListFailure>) -> Void
    ) -> (() -> Void)? {
        guard let url = ChatListEndpoint.url(origin: origin, limit: limit) else {
            completion(.failure(.invalidResponse))
            return nil
        }
        // Double-check the model's authentication decision in the transport.
        // A remote request without a session cookie would be an unauthenticated
        // read of private titles, so it is never sent.
        guard origin.isLoopback || cookieHeader != nil else {
            completion(.failure(.notSignedIn))
            return nil
        }
        let request = ChatListRequest(
            origin: origin,
            expectedURL: url,
            limit: limit,
            cookieHeader: cookieHeader,
            configuration: configuration,
            completion: completion
        )
        return request.start()
    }
}

private final class ChatListRequest: NSObject, URLSessionDataDelegate {
    private let origin: Origin
    private let expectedURL: URL
    private let limit: Int
    private let cookieHeader: String?
    private let configuration: URLSessionConfiguration
    private let completion: (Result<ChatListPayload, ChatListFailure>) -> Void

    private var body = Data()
    private var session: URLSession?
    private var task: URLSessionDataTask?
    private var delegateQueue: OperationQueue?
    private var finished = false
    private var sawRedirect = false

    init(
        origin: Origin,
        expectedURL: URL,
        limit: Int,
        cookieHeader: String?,
        configuration: URLSessionConfiguration,
        completion: @escaping (Result<ChatListPayload, ChatListFailure>) -> Void
    ) {
        self.origin = origin
        self.expectedURL = expectedURL
        self.limit = limit
        self.cookieHeader = cookieHeader
        self.configuration = configuration
        self.completion = completion
        super.init()
    }

    func start() -> (() -> Void)? {
        guard ChatListEndpoint.matches(expectedURL, origin: origin) else {
            finish(.failure(.invalidResponse))
            return nil
        }
        let config = configuration.copy() as! URLSessionConfiguration
        config.httpShouldSetCookies = false
        config.httpCookieAcceptPolicy = .never
        config.httpCookieStorage = nil
        config.httpAdditionalHeaders = nil
        config.urlCredentialStorage = nil
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = min(config.timeoutIntervalForRequest, 8)
        config.timeoutIntervalForResource = min(config.timeoutIntervalForResource, 10)

        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        queue.name = "CiaobotMac.ChatListRequest"
        self.delegateQueue = queue
        let session = URLSession(configuration: config, delegate: self, delegateQueue: queue)
        self.session = session
        var request = URLRequest(url: expectedURL)
        request.httpShouldHandleCookies = false
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let cookieHeader {
            request.setValue(cookieHeader, forHTTPHeaderField: "Cookie")
        }
        let task = session.dataTask(with: request)
        self.task = task
        task.resume()
        return { [weak self] in self?.cancel() }
    }

    /// Cancellation can come from the main actor while URLSession is
    /// delivering delegate callbacks on another queue. Both paths are
    /// serialized: the request owns a one-operation delegate queue, and cancel
    /// is enqueued onto it instead of mutating delegate state directly.
    private func cancel() {
        delegateQueue?.addOperation { [weak self] in self?.cancelOnDelegateQueue() }
    }

    private func cancelOnDelegateQueue() {
        guard !finished else { return }
        finished = true
        task?.cancel()
        session?.invalidateAndCancel()
        session = nil
        task = nil
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        // A redirect can move the request (and its Cookie header) to another
        // origin. Refuse it; the task then fails and reports .redirected.
        sawRedirect = true
        completionHandler(nil)
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        if URLSessionChatListTransport.acceptsServerTrust(challenge.protectionSpace.authenticationMethod) {
            completionHandler(.performDefaultHandling, nil)
        } else {
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard !finished else { return }
        guard data.count <= URLSessionChatListTransport.maximumResponseBytes - body.count else {
            dataTask.cancel()
            finish(.failure(.invalidResponse))
            return
        }
        body.append(data)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard !finished else { return }
        if sawRedirect {
            finish(.failure(.redirected))
            return
        }
        if let error {
            let nsError = error as NSError
            if nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled {
                finish(.failure(.cancelled))
            } else {
                finish(.failure(.requestFailed))
            }
            return
        }
        guard let response = task.response as? HTTPURLResponse else {
            finish(.failure(.invalidResponse))
            return
        }
        guard let responseURL = response.url,
              ChatListEndpoint.matches(responseURL, origin: origin) else {
            finish(.failure(.redirected))
            return
        }
        if (300...399).contains(response.statusCode) {
            finish(.failure(.redirected))
            return
        }
        guard response.statusCode == 200 else {
            finish(.failure(response.statusCode == 401 || response.statusCode == 403 ? .notSignedIn : .requestFailed))
            return
        }
        finish(ChatListPayload.decode(body, limit: limit))
    }

    private func finish(_ result: Result<ChatListPayload, ChatListFailure>) {
        guard !finished else { return }
        finished = true
        session?.finishTasksAndInvalidate()
        session = nil
        task = nil
        completion(result)
    }
}

@MainActor
protocol ChatListProviding: AnyObject {
    var state: ChatListState { get }
    var statePublisher: AnyPublisher<ChatListState, Never> { get }
    func start()
    func stop()
    func refreshIfNeeded()
}

/// The tray feed's cadence. These live outside the main-actor model so the
/// initializer's default argument can read them without crossing actors.
enum ChatListTiming {
    /// A chat started or finished in the web app reaches the tray in about ten
    /// seconds even when the menu is closed.
    static let refreshInterval: TimeInterval = 10
    /// Opening the menu reuses a fetch younger than this, so opening and
    /// reopening it does not hammer the server.
    static let menuRefreshMinimumInterval: TimeInterval = 2
}

/// Owns the tray feed for one connected origin. Every fetch carries a
/// generation token; a response from a previous connection, a logout, or a
/// superseded refresh is dropped before it can touch the published state.
@MainActor
final class ChatListModel: ObservableObject, ChatListProviding {
    static let defaultRefreshInterval: TimeInterval = ChatListTiming.refreshInterval
    static let menuRefreshMinimumInterval: TimeInterval = ChatListTiming.menuRefreshMinimumInterval

    @Published private(set) var state: ChatListState = .disconnected
    var statePublisher: AnyPublisher<ChatListState, Never> { $state.eraseToAnyPublisher() }

    private let context: ConnectionContext
    private let transport: ChatListTransport
    private let cookieProviderFactory: (WKWebsiteDataStore) -> ChatCookieProviding
    private let now: () -> Date
    private let limit: Int
    private let refreshInterval: TimeInterval
    private let hostStoreTracking: HostStoreTrackingBundle?

    private var subscription: AnyCancellable?
    private var inFlight: (() -> Void)?
    private var generation = 0
    private var currentKey: String?
    private var lastFetchAt: Date?
    private var timer: Timer?
    private var started = false

    init(
        context: ConnectionContext,
        transport: ChatListTransport = URLSessionChatListTransport(),
        cookieProviderFactory: @escaping (WKWebsiteDataStore) -> ChatCookieProviding = { WebKitCookieProvider(dataStore: $0) },
        limit: Int = ChatListEndpoint.defaultLimit,
        refreshInterval: TimeInterval = ChatListTiming.refreshInterval,
        now: @escaping () -> Date = Date.init,
        hostStoreTracking: HostStoreTrackingBundle? = nil
    ) {
        self.context = context
        self.transport = transport
        self.cookieProviderFactory = cookieProviderFactory
        self.limit = limit
        self.refreshInterval = refreshInterval
        self.now = now
        self.hostStoreTracking = hostStoreTracking
    }

    deinit {
        timer?.invalidate()
        inFlight?()
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
        generation += 1
        cancelInFlight()
        timer?.invalidate()
        timer = nil
        state = .disconnected
        currentKey = nil
        lastFetchAt = nil
    }

    /// Called when the menu is about to open. It does not hammer the server:
    /// a recent successful or attempted fetch is reused for a few seconds. A
    /// not-signed-in state is retried immediately so finishing a web sign-in
    /// does not leave a stale "Sign in" row until the next timer tick.
    func refreshIfNeeded() {
        guard started, context.isConnected, context.currentOrigin != nil else { return }
        if case .unavailable(.notSignedIn) = state {
            refresh()
            return
        }
        if let lastFetchAt, now().timeIntervalSince(lastFetchAt) < Self.menuRefreshMinimumInterval {
            return
        }
        refresh()
    }

    func refresh() {
        guard started, let origin = context.currentOrigin, context.isConnected else {
            resetForDisconnect()
            return
        }
        generation += 1
        let generation = self.generation
        cancelInFlight()
        lastFetchAt = now()
        if !hasSnapshot(for: origin) {
            state = .loading
        }
        guard let url = ChatListEndpoint.url(origin: origin, limit: limit) else {
            finish(.failure(.invalidResponse), generation: generation, origin: origin)
            return
        }
        let acquisition = hostStoreTracking?.acquire(origin: origin, store: context.dataStore)
        if case .refused? = acquisition {
            lastFetchAt = nil
            finish(.failure(.invalidResponse), generation: generation, origin: origin)
            return
        }
        guard let dataStore = context.dataStore else {
            if case .noStore? = acquisition {
                if origin.isLoopback {
                    send(origin: origin, url: url, cookieHeader: nil, generation: generation)
                } else {
                    finish(.failure(.invalidResponse), generation: generation, origin: origin)
                }
                return
            }
            if origin.isLoopback {
                // The local endpoint is normally reachable without a session.
                // No store means no per-session cookie to offer, not a reason
                // to suppress the local feed.
                send(origin: origin, url: url, cookieHeader: nil, generation: generation)
            } else {
                finish(.failure(.notSignedIn), generation: generation, origin: origin)
            }
            return
        }
        let operation: UUID? = { if case .operation(let id)? = acquisition { id } else { nil } }()
        let registry = hostStoreTracking?.registry
        cookieProviderFactory(dataStore).cookies(for: url) { [weak self] cookies in
            Task { @MainActor in
                if let operation { guard registry?.claimCompletion(id: operation) == true else { return } }
                defer { if let operation { registry?.finishOperation(id: operation) } }
                guard let self, self.isCurrent(generation: generation, origin: origin) else { return }
                let header = SessionCookiePolicy.header(for: url, cookies: cookies, now: self.now())
                if !origin.isLoopback {
                    guard SessionCookiePolicy.containsSessionCookie(for: url, cookies: cookies, now: self.now()) else {
                        self.finish(.failure(.notSignedIn), generation: generation, origin: origin)
                        return
                    }
                }
                // A loopback origin sends the connection's exact session cookie
                // when one exists (for example behind a local reverse proxy),
                // but never requires it: the server's loopback peer protection
                // still applies. A remote origin requires it.
                self.send(origin: origin, url: url, cookieHeader: header, generation: generation)
            }
        }
    }

    private func connectionChanged() {
        guard started else { return }
        guard context.isConnected, let origin = context.currentOrigin else {
            resetForDisconnect()
            return
        }
        let key = origin.canonicalKey
        if key != currentKey {
            currentKey = key
            lastFetchAt = nil
            state = .loading
            scheduleTimer()
            refresh()
        } else if state == .disconnected {
            state = .loading
            refresh()
        }
    }

    private func resetForDisconnect() {
        generation += 1
        cancelInFlight()
        timer?.invalidate()
        timer = nil
        currentKey = nil
        lastFetchAt = nil
        state = .disconnected
    }

    private func scheduleTimer() {
        timer?.invalidate()
        let timer = Timer(timeInterval: refreshInterval, repeats: true) { [weak self] _ in
            // The timer block is a Sendable, nonisolated callback: bind the
            // model to an immutable strong reference for the hop and refresh
            // on the main actor instead of reading the weak capture there.
            guard let self else { return }
            Task { @MainActor in self.refresh() }
        }
        timer.tolerance = 5
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    private func hasSnapshot(for origin: Origin) -> Bool {
        if case .loaded(let snapshot) = state {
            return snapshot.origin == origin.canonicalKey
        }
        return false
    }

    private func send(origin: Origin, url: URL, cookieHeader: String?, generation: Int) {
        let cancel = transport.fetch(origin: origin, limit: limit, cookieHeader: cookieHeader) { [weak self] result in
            Task { @MainActor in
                self?.finish(result, generation: generation, origin: origin)
            }
        }
        guard isCurrent(generation: generation, origin: origin) else {
            cancel?()
            return
        }
        inFlight = cancel
    }

    private func finish(_ result: Result<ChatListPayload, ChatListFailure>, generation: Int, origin: Origin) {
        guard isCurrent(generation: generation, origin: origin) else { return }
        inFlight = nil
        switch result {
        case .success(let payload):
            state = .loaded(ChatListSnapshot(
                origin: origin.canonicalKey,
                chats: payload.chats,
                attentionCount: payload.attentionCount,
                multipleWorkspaces: payload.multipleWorkspaces,
                fetchedAt: now()
            ))
        case .failure(let failure):
            // A cancellation caused by a newer refresh, a disconnect, or stop
            // is dropped by the generation/status check above. A cancellation
            // that reaches here is the current request failing, so it is shown
            // as unavailable rather than leaving "Loading chats…" stuck.
            state = .unavailable(failure)
        }
    }

    private func isCurrent(generation: Int, origin: Origin) -> Bool {
        started
            && generation == self.generation
            && context.isConnected
            && context.currentOrigin?.canonicalKey == origin.canonicalKey
    }

    private func cancelInFlight() {
        inFlight?()
        inFlight = nil
    }
}
