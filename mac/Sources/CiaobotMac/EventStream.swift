import Foundation

/// One parsed `/ws/events` payload. Events this client does not need are
/// reported as `.ignored` rather than as a protocol failure: the socket is
/// shared with the web UI and carries many other event types.
enum ChatEvent: Equatable {
    case snapshot(activeChatIDs: Set<String>)
    case streamingStarted(chatID: String)
    case streamingDone(chatID: String)
    /// The server's name for the chat's workspace, already limited to a
    /// display-size string. Empty when the server did not send one (a
    /// single-workspace install, or an older engine that cannot).
    case resultReady(chatID: String, title: String, snippet: String, workspace: String)
    /// The chat was read on some device (opened, or cleared from the inbox).
    case chatRead(chatID: String)
    case ignored
}

enum ChatEventDecoder {
    static let maximumMessageBytes = 512 * 1024
    static let maximumActiveStreams = 500
    static let maximumTitleCharacters = 512
    static let maximumSnippetCharacters = 2_048
    static let maximumWorkspaceCharacters = 256

    /// Decodes only the fields this client acts on. A malformed snapshot or a
    /// malformed result is `nil` (the transport closes and reconnects); an
    /// unknown or irrelevant type is `.ignored`.
    static func decode(_ data: Data) -> ChatEvent? {
        guard data.count <= maximumMessageBytes,
              let object = try? JSONSerialization.jsonObject(with: data),
              let dictionary = object as? [String: Any],
              let type = dictionary["type"] as? String else { return nil }

        switch type {
        case "snapshot":
            guard let rawStreams = dictionary["active_streams"] as? [Any] else { return nil }
            guard rawStreams.count <= maximumActiveStreams else { return nil }
            var active = Set<String>()
            for item in rawStreams {
                guard let entry = item as? [String: Any],
                      let chatID = entry["chat_id"] as? String,
                      !chatID.isEmpty,
                      chatID.utf8.count <= 200 else { continue }
                active.insert(chatID)
            }
            return .snapshot(activeChatIDs: active)

        case "chat_streaming_started":
            guard let chatID = dictionary["chat_id"] as? String, !chatID.isEmpty,
                  chatID.utf8.count <= 200 else { return nil }
            return .streamingStarted(chatID: chatID)

        case "chat_streaming_done":
            guard let chatID = dictionary["chat_id"] as? String, !chatID.isEmpty,
                  chatID.utf8.count <= 200 else { return nil }
            return .streamingDone(chatID: chatID)

        case "chat_result_ready":
            guard let chatID = dictionary["chat_id"] as? String, !chatID.isEmpty,
                  chatID.utf8.count <= 200 else { return nil }
            let title = bounded(dictionary["title"] as? String ?? "", characters: maximumTitleCharacters)
            let snippet = bounded(dictionary["snippet"] as? String ?? "", characters: maximumSnippetCharacters)
            // Optional: a single-workspace server omits it on purpose, and an
            // older one never sent it. Absence is not a defect.
            let workspace = bounded(dictionary["workspace"] as? String ?? "", characters: maximumWorkspaceCharacters)
            return .resultReady(chatID: chatID, title: title, snippet: snippet, workspace: workspace)

        case "chat_read":
            guard let chatID = dictionary["chat_id"] as? String, !chatID.isEmpty,
                  chatID.utf8.count <= 200 else { return nil }
            return .chatRead(chatID: chatID)

        default:
            return .ignored
        }
    }

    private static func bounded(_ text: String, characters: Int) -> String {
        text.count <= characters ? text : String(text.prefix(characters))
    }
}

enum EventStreamCloseReason: Equatable {
    case unauthorized
    case forbiddenOrigin
    case redirected
    case authenticationRefused
    case invalidMessage
    case ended
    case failed
}

protocol EventStreamConnection: AnyObject {
    func cancel()
}

protocol EventStreamTransport: AnyObject {
    /// Returns a cancellation handle, or `nil` when no request was made. The
    /// callbacks are delivered on the main actor and must be checked by the
    /// caller against its own generation token.
    func open(
        origin: Origin,
        cookieHeader: String,
        onEvent: @escaping (ChatEvent) -> Void,
        onClose: @escaping (EventStreamCloseReason) -> Void
    ) -> EventStreamConnection?
}

/// The fixed `/ws/events` endpoint on the exact selected origin. The scheme is
/// the WebSocket spelling of the selected HTTP(S) origin; no server-supplied
/// path or host is ever used.
enum EventStreamEndpoint {
    static let path = "/ws/events"

    static func url(origin: Origin) -> URL? {
        guard var components = URLComponents(url: origin.url, resolvingAgainstBaseURL: false) else {
            return nil
        }
        components.scheme = origin.url.scheme?.lowercased() == "https" ? "wss" : "ws"
        components.percentEncodedPath = path
        components.percentEncodedQuery = nil
        components.fragment = nil
        return components.url
    }

    static func matches(_ url: URL, origin: Origin) -> Bool {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let scheme = components.scheme?.lowercased(),
              let host = components.host?.lowercased(), !host.isEmpty,
              components.user == nil, components.password == nil,
              components.percentEncodedPath == path,
              components.percentEncodedQuery == nil,
              components.fragment == nil else { return false }
        let expectedScheme = origin.url.scheme?.lowercased() == "https" ? "wss" : "ws"
        guard scheme == expectedScheme else { return false }
        let port = components.port ?? (scheme == "wss" ? 443 : 80)
        return origin.matches(
            scheme: scheme == "wss" ? "https" : "http",
            host: host,
            port: port
        )
    }

    /// The handshake's `Origin` value. The engine compares this against its own
    /// host/scheme/effective port, and a native client has no browser to add it.
    static func originHeader(origin: Origin) -> String { origin.canonicalKey }

    /// Only the connection's exact `ciao_session` cookie, selected for this
    /// URL. No ambient cookie storage is involved.
    ///
    /// Cookie matching is defined in HTTP terms, so the WebSocket URL is
    /// normalised back to its HTTP(S) spelling first. Without that, a Secure
    /// session cookie on an HTTPS origin would be rejected merely because the
    /// socket URL says `wss`.
    static func cookieHeader(for url: URL, cookies: [HTTPCookie], now: Date = Date()) -> String? {
        guard let selectionURL = httpEquivalent(url) else { return nil }
        return SessionCookiePolicy.header(for: selectionURL, cookies: cookies, now: now)
    }

    private static func httpEquivalent(_ url: URL) -> URL? {
        guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let scheme = components.scheme?.lowercased() else { return nil }
        switch scheme {
        case "ws":
            components.scheme = "http"
        case "wss":
            components.scheme = "https"
        case "http", "https":
            break
        default:
            return nil
        }
        return components.url
    }

    /// The request carries only the selected cookie and an exact same-origin
    /// `Origin`. It never carries Authorization, a shared cache, or a default
    /// header set.
    static func makeRequest(origin: Origin, cookieHeader: String) -> URLRequest? {
        guard let url = url(origin: origin), matches(url, origin: origin) else { return nil }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.httpShouldHandleCookies = false
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 15
        request.setValue(cookieHeader, forHTTPHeaderField: "Cookie")
        request.setValue(originHeader(origin: origin), forHTTPHeaderField: "Origin")
        return request
    }

    static func closeReason(for code: URLSessionWebSocketTask.CloseCode) -> EventStreamCloseReason {
        switch code.rawValue {
        case 4001: return .unauthorized
        case 4003: return .forbiddenOrigin
        default: return .ended
        }
    }
}

/// The production socket. It uses an ephemeral URL session with no cookie
/// storage, cache, or credential store; the only credential is the cookie
/// header selected for this exact URL. Redirects and non-server-trust
/// authentication challenges are refused.
final class URLSessionEventStreamTransport: EventStreamTransport {
    /// Normal TLS server-trust validation only. A client certificate, HTTP
    /// auth, or proxy auth challenge cancels the handshake.
    static func acceptsServerTrust(_ authenticationMethod: String) -> Bool {
        authenticationMethod == NSURLAuthenticationMethodServerTrust
    }

    private let configuration: URLSessionConfiguration

    init(configuration: URLSessionConfiguration = .ephemeral) {
        self.configuration = configuration
    }

    func open(
        origin: Origin,
        cookieHeader: String,
        onEvent: @escaping (ChatEvent) -> Void,
        onClose: @escaping (EventStreamCloseReason) -> Void
    ) -> EventStreamConnection? {
        guard let request = EventStreamEndpoint.makeRequest(origin: origin, cookieHeader: cookieHeader) else {
            return nil
        }
        let stream = URLSessionEventStreamRequest(
            request: request,
            origin: origin,
            configuration: configuration,
            onEvent: onEvent,
            onClose: onClose
        )
        stream.start()
        return stream
    }
}

final class URLSessionEventStreamRequest: NSObject, URLSessionWebSocketDelegate, URLSessionTaskDelegate, EventStreamConnection {
    private let request: URLRequest
    private let expectedURL: URL
    private let origin: Origin
    private let configuration: URLSessionConfiguration
    private let onEvent: (ChatEvent) -> Void
    private let onClose: (EventStreamCloseReason) -> Void
    private let queue: OperationQueue

    private var session: URLSession?
    private var task: URLSessionWebSocketTask?
    private var finished = false
    private var redirected = false
    private var pingTimer: DispatchSourceTimer?

    init(
        request: URLRequest,
        origin: Origin,
        configuration: URLSessionConfiguration,
        onEvent: @escaping (ChatEvent) -> Void,
        onClose: @escaping (EventStreamCloseReason) -> Void
    ) {
        self.request = request
        self.expectedURL = request.url!
        self.origin = origin
        self.configuration = configuration
        self.onEvent = onEvent
        self.onClose = onClose
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        queue.name = "CiaobotMac.EventStreamRequest"
        self.queue = queue
        super.init()
    }

    /// The socket's session configuration: no ambient cookies, credentials
    /// or cache, a bounded handshake, and deliberately no lifetime cap.
    static func sessionConfiguration(from configuration: URLSessionConfiguration) -> URLSessionConfiguration {
        let config = configuration.copy() as! URLSessionConfiguration
        config.httpShouldSetCookies = false
        config.httpCookieAcceptPolicy = .never
        config.httpCookieStorage = nil
        config.httpAdditionalHeaders = nil
        config.urlCredentialStorage = nil
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = min(config.timeoutIntervalForRequest, 15)
        // No lifetime cap: when URLSession times out a WebSocket task itself,
        // it reports the failure but never closes the TCP connection, and
        // neither cancel nor invalidating the session releases it. A capped
        // stream therefore leaked one connection per reconnect until the
        // process hit its flow limit and every new connection failed with
        // ENOMEM. A dead peer is detected by `pingInterval` pings instead.
        return config
    }

    func start() {
        guard !finished else { return }
        let config = Self.sessionConfiguration(from: configuration)

        let session = URLSession(configuration: config, delegate: self, delegateQueue: queue)
        self.session = session
        let task = session.webSocketTask(with: request)
        self.task = task
        task.resume()
        receiveNext()
        startPinging()
    }

    /// How often a live stream is pinged. A ping that fails closes the stream
    /// as `.failed`, so a silently dropped connection is noticed and retried.
    static let pingInterval: TimeInterval = 30

    private func startPinging() {
        let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        timer.schedule(deadline: .now() + Self.pingInterval, repeating: Self.pingInterval, leeway: .seconds(5))
        timer.setEventHandler { [weak self] in
            self?.queue.addOperation { self?.ping() }
        }
        pingTimer = timer
        timer.resume()
    }

    private func ping() {
        guard !finished, let task else { return }
        task.sendPing { [weak self] error in
            guard let self, error != nil else { return }
            self.queue.addOperation { self.finish(.failed) }
        }
    }

    func cancel() {
        queue.addOperation { [weak self] in
            self?.cancelOnQueue()
        }
    }

    private func cancelOnQueue() {
        guard !finished else { return }
        finished = true
        pingTimer?.cancel()
        pingTimer = nil
        task?.cancel(with: .goingAway, reason: nil)
        session?.invalidateAndCancel()
        session = nil
        task = nil
    }

    private func receiveNext() {
        task?.receive { [weak self] result in
            guard let self else { return }
            guard !self.finished else { return }
            switch result {
            case .failure:
                // `didCloseWith` usually reports the close code first. A
                // receive failure that wins the race is still a closed stream.
                self.finish(self.redirected ? .redirected : .failed)
            case .success(let message):
                let data: Data
                switch message {
                case .data(let value):
                    data = value
                case .string(let value):
                    data = Data(value.utf8)
                @unknown default:
                    self.finish(.invalidMessage)
                    return
                }
                guard data.count <= ChatEventDecoder.maximumMessageBytes,
                      let event = ChatEventDecoder.decode(data) else {
                    self.finish(.invalidMessage)
                    return
                }
                self.emit(event)
                self.receiveNext()
            }
        }
    }

    private func emit(_ event: ChatEvent) {
        let handler = onEvent
        Task { @MainActor in handler(event) }
    }

    private func finish(_ reason: EventStreamCloseReason) {
        guard !finished else { return }
        finished = true
        pingTimer?.cancel()
        pingTimer = nil
        task?.cancel(with: .normalClosure, reason: nil)
        session?.invalidateAndCancel()
        session = nil
        task = nil
        let handler = onClose
        Task { @MainActor in handler(reason) }
    }

    // MARK: URLSessionWebSocketDelegate

    func urlSession(
        _ session: URLSession,
        webSocketTask: URLSessionWebSocketTask,
        didOpenWithProtocol protocol: String?
    ) {
        guard !finished else { return }
        // A proxy or a redirect must not move this authenticated handshake to
        // another origin. Re-check the URL that actually opened.
        if let opened = webSocketTask.originalRequest?.url, opened != expectedURL {
            finish(.redirected)
            return
        }
        guard EventStreamEndpoint.matches(expectedURL, origin: origin) else {
            finish(.redirected)
            return
        }
    }

    func urlSession(
        _ session: URLSession,
        webSocketTask: URLSessionWebSocketTask,
        didCloseWith closeCode: URLSessionWebSocketTask.CloseCode,
        reason: Data?
    ) {
        guard !finished else { return }
        finish(EventStreamEndpoint.closeReason(for: closeCode))
    }

    // MARK: URLSessionTaskDelegate

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        // Never let the Cookie header travel to a redirect target.
        redirected = true
        completionHandler(nil)
        finish(.redirected)
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        if URLSessionEventStreamTransport.acceptsServerTrust(challenge.protectionSpace.authenticationMethod) {
            completionHandler(.performDefaultHandling, nil)
        } else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            finish(.authenticationRefused)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard !finished else { return }
        if redirected {
            finish(.redirected)
            return
        }
        if let error {
            let nsError = error as NSError
            if nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled {
                finish(.ended)
            } else {
                finish(.failed)
            }
        } else {
            finish(.ended)
        }
    }
}
