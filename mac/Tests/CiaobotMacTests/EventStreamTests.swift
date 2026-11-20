import XCTest
import Foundation
import CryptoKit

@testable import CiaobotMac

// MARK: - Loopback WebSocket fixture

private struct WebSocketFixtureRequest {
    let requestLine: String
    let headers: [String: String]
}

private enum WebSocketFixtureBehavior {
    case webSocket([String])
    case redirect(String)
    case reject(status: Int)
}

private final class WebSocketFixtureServer {
    let port: Int

    private let listener: Int32
    private let lock = NSLock()
    private let queue = DispatchQueue(label: "CiaobotMacTests.WebSocketFixture", attributes: .concurrent)
    private var handler: (WebSocketFixtureRequest) -> WebSocketFixtureBehavior
    private var requests: [WebSocketFixtureRequest] = []
    private var stopped = false
    private lazy var thread = Thread { [weak self] in self?.run() }

    init(handler: @escaping (WebSocketFixtureRequest) -> WebSocketFixtureBehavior) throws {
        self.handler = handler
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw fixtureError("socket") }

        var yes: Int32 = 1
        _ = setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))

        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        address.sin_port = 0
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0, listen(fd, 16) == 0 else {
            close(fd)
            throw fixtureError("bind/listen")
        }

        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(fd, $0, &length)
            }
        }
        guard named == 0 else {
            close(fd)
            throw fixtureError("getsockname")
        }

        self.listener = fd
        self.port = Int(UInt16(bigEndian: address.sin_port))
        thread.start()
    }

    deinit { shutdown() }

    var origin: Origin { Origin("http://127.0.0.1:\(port)")! }

    var observedRequests: [WebSocketFixtureRequest] {
        lock.lock()
        defer { lock.unlock() }
        return requests
    }

    func waitForRequest(timeout: TimeInterval = 2) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            lock.lock()
            let found = !requests.isEmpty
            lock.unlock()
            if found { return true }
            usleep(10_000)
        }
        lock.lock()
        defer { lock.unlock() }
        return !requests.isEmpty
    }

    func shutdown() {
        lock.lock()
        if stopped {
            lock.unlock()
            return
        }
        stopped = true
        lock.unlock()
        close(listener)
        thread.cancel()
    }

    private func run() {
        while true {
            lock.lock()
            let stop = stopped
            lock.unlock()
            if stop { break }

            var descriptor = pollfd(fd: listener, events: Int16(POLLIN), revents: 0)
            let ready = poll(&descriptor, 1, 100)
            if ready < 0 { break }
            if ready == 0 { continue }

            let client = accept(listener, nil, nil)
            guard client >= 0 else { continue }
            queue.async { [weak self] in
                guard let self else { close(client); return }
                self.handle(client)
            }
        }
    }

    private func handle(_ client: Int32) {
        defer { close(client) }
        var noSignal: Int32 = 1
        _ = setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
        guard let request = readRequest(client) else { return }

        lock.lock()
        requests.append(request)
        let behavior = handler(request)
        lock.unlock()

        switch behavior {
        case .redirect(let location):
            writeAll(client, Data("HTTP/1.1 302 Found\r\nLocation: \(location)\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".utf8))
        case .reject(let status):
            let reason = status == 401 ? "Unauthorized" : "Forbidden"
            writeAll(client, Data("HTTP/1.1 \(status) \(reason)\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".utf8))
        case .webSocket(let messages):
            guard let key = request.headers["sec-websocket-key"],
                  let accept = Self.acceptKey(for: key) else { return }
            let handshake = "HTTP/1.1 101 Switching Protocols\r\n"
                + "Upgrade: websocket\r\n"
                + "Connection: Upgrade\r\n"
                + "Sec-WebSocket-Accept: \(accept)\r\n\r\n"
            guard writeAll(client, Data(handshake.utf8)) else { return }
            usleep(20_000)
            for message in messages {
                guard writeAll(client, Self.textFrame(message)) else { return }
                usleep(20_000)
            }
            // Keep the socket open long enough for the client to read every
            // frame. The client cancels it when the test ends.
            var descriptor = pollfd(fd: client, events: Int16(POLLIN), revents: 0)
            _ = poll(&descriptor, 1, 2_000)
        }
    }

    private func readRequest(_ client: Int32) -> WebSocketFixtureRequest? {
        var bytes = Data()
        let deadline = Date().addingTimeInterval(3)
        while bytes.range(of: Data("\r\n\r\n".utf8)) == nil && bytes.count < 16 * 1024 {
            let remaining = Int32(max(1, deadline.timeIntervalSinceNow * 1000))
            var descriptor = pollfd(fd: client, events: Int16(POLLIN), revents: 0)
            guard poll(&descriptor, 1, remaining) == 1 else { return nil }
            var buffer = [UInt8](repeating: 0, count: 2048)
            let count = recv(client, &buffer, buffer.count, 0)
            guard count > 0 else { return nil }
            bytes.append(contentsOf: buffer.prefix(count))
        }
        guard let text = String(data: bytes, encoding: .utf8) else { return nil }
        let lines = text.components(separatedBy: "\r\n")
        guard let requestLine = lines.first else { return nil }
        var headers: [String: String] = [:]
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else { continue }
            let name = line[..<colon].trimmingCharacters(in: .whitespaces).lowercased()
            let value = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
            headers[name] = value
        }
        return WebSocketFixtureRequest(requestLine: requestLine, headers: headers)
    }

    @discardableResult
    private func writeAll(_ client: Int32, _ data: Data) -> Bool {
        var sent = 0
        let bytes = [UInt8](data)
        let deadline = Date().addingTimeInterval(3)
        while sent < bytes.count {
            let remaining = Int32(max(1, deadline.timeIntervalSinceNow * 1000))
            var descriptor = pollfd(fd: client, events: Int16(POLLOUT), revents: 0)
            guard poll(&descriptor, 1, remaining) == 1 else { return false }
            let count = bytes.withUnsafeBytes { buffer in
                send(client, buffer.baseAddress!.advanced(by: sent), bytes.count - sent, 0)
            }
            guard count > 0 else { return false }
            sent += count
        }
        return true
    }

    private static func acceptKey(for key: String) -> String? {
        let magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        let digest = Insecure.SHA1.hash(data: Data((key + magic).utf8))
        return Data(digest).base64EncodedString()
    }

    private static func textFrame(_ text: String) -> Data {
        let payload = [UInt8](text.utf8)
        var frame: [UInt8] = [0x81]
        if payload.count < 126 {
            frame.append(UInt8(payload.count))
        } else if payload.count <= 65_535 {
            frame.append(126)
            frame.append(UInt8((payload.count >> 8) & 0xFF))
            frame.append(UInt8(payload.count & 0xFF))
        } else {
            frame.append(127)
            for shift in stride(from: 56, through: 0, by: -8) {
                frame.append(UInt8((payload.count >> shift) & 0xFF))
            }
        }
        frame.append(contentsOf: payload)
        return Data(frame)
    }
}

private func fixtureError(_ operation: String) -> NSError {
    NSError(domain: "CiaobotMacTests.WebSocketFixture", code: 1, userInfo: [NSLocalizedDescriptionKey: operation])
}

// MARK: - Tests

@MainActor
final class EventStreamTests: XCTestCase {
    private let loopback = Origin("http://127.0.0.1:8443")!
    private let remote = Origin("https://example.com")!
    private let cookie = "ciao_session=secret"

    // MARK: Endpoint and request construction

    func testEndpointUsesTheWebSocketSpellingOfTheExactOrigin() throws {
        let local = try XCTUnwrap(EventStreamEndpoint.url(origin: loopback))
        XCTAssertEqual(local.absoluteString, "ws://127.0.0.1:8443/ws/events")
        XCTAssertTrue(EventStreamEndpoint.matches(local, origin: loopback))

        let secure = try XCTUnwrap(EventStreamEndpoint.url(origin: remote))
        XCTAssertEqual(secure.absoluteString, "wss://example.com/ws/events")
        XCTAssertTrue(EventStreamEndpoint.matches(secure, origin: remote))

        XCTAssertFalse(EventStreamEndpoint.matches(
            URL(string: "ws://127.0.0.1:9443/ws/events")!, origin: loopback
        ))
        XCTAssertFalse(EventStreamEndpoint.matches(
            URL(string: "wss://127.0.0.1:8443/ws/events")!, origin: loopback
        ))
        XCTAssertFalse(EventStreamEndpoint.matches(
            URL(string: "ws://127.0.0.1:8443/ws/other")!, origin: loopback
        ))
        XCTAssertFalse(EventStreamEndpoint.matches(
            URL(string: "ws://127.0.0.1:8443/ws/events?next=other")!, origin: loopback
        ))
        XCTAssertFalse(EventStreamEndpoint.matches(
            URL(string: "wss://example.com:443/ws/events")!, origin: loopback
        ))
    }

    func testRequestCarriesOnlyTheExactOriginAndSelectedCookie() throws {
        let request = try XCTUnwrap(EventStreamEndpoint.makeRequest(origin: loopback, cookieHeader: cookie))
        XCTAssertEqual(request.url?.absoluteString, "ws://127.0.0.1:8443/ws/events")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Origin"), "http://127.0.0.1:8443")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Cookie"), cookie)
        XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
        XCTAssertNil(request.value(forHTTPHeaderField: "Accept"))
        XCTAssertFalse(request.httpShouldHandleCookies)
        XCTAssertEqual(request.cachePolicy, .reloadIgnoringLocalCacheData)
    }

    func testSecureCookieSelectionUsesTheHTTPSMeaningOfWSS() throws {
        let url = try XCTUnwrap(EventStreamEndpoint.url(origin: remote))
        let secure = try XCTUnwrap(HTTPCookie(properties: [
            .name: "ciao_session",
            .value: "secret",
            .domain: "example.com",
            .path: "/",
            .secure: "TRUE",
        ]))
        XCTAssertEqual(EventStreamEndpoint.cookieHeader(for: url, cookies: [secure]), cookie)

        let insecure = try XCTUnwrap(HTTPCookie(properties: [
            .name: "ciao_session",
            .value: "plain",
            .domain: "example.com",
            .path: "/",
        ]))
        // A non-Secure cookie is valid on both schemes, exactly as a browser
        // treats it.
        XCTAssertEqual(EventStreamEndpoint.cookieHeader(for: url, cookies: [insecure]), "ciao_session=plain")

        let plainSocket = try XCTUnwrap(EventStreamEndpoint.url(origin: loopback))
        let loopbackSecure = try XCTUnwrap(HTTPCookie(properties: [
            .name: "ciao_session",
            .value: "secret",
            .domain: "127.0.0.1",
            .path: "/",
            .secure: "TRUE",
        ]))
        XCTAssertNil(
            EventStreamEndpoint.cookieHeader(for: plainSocket, cookies: [loopbackSecure]),
            "A Secure cookie must not be sent over a plain ws socket."
        )
    }

    func testStreamHasNoLifetimeCap() {
        // URLSession leaves the TCP connection open when it times out a
        // WebSocket task itself; a short resource cap leaked one connection
        // per reconnect until new connections failed with ENOMEM.
        let base = URLSessionConfiguration.ephemeral
        let config = URLSessionEventStreamRequest.sessionConfiguration(from: base)
        XCTAssertEqual(config.timeoutIntervalForResource, base.timeoutIntervalForResource)
        XCTAssertGreaterThanOrEqual(config.timeoutIntervalForResource, 24 * 60 * 60)
        XCTAssertLessThanOrEqual(config.timeoutIntervalForRequest, 15)
        XCTAssertNil(config.httpCookieStorage)
        XCTAssertLessThan(URLSessionEventStreamRequest.pingInterval, 60)
    }

    func testTLSOnlyAcceptsNormalServerTrust() {
        XCTAssertTrue(URLSessionEventStreamTransport.acceptsServerTrust(NSURLAuthenticationMethodServerTrust))
        XCTAssertFalse(URLSessionEventStreamTransport.acceptsServerTrust(NSURLAuthenticationMethodHTTPBasic))
        XCTAssertFalse(URLSessionEventStreamTransport.acceptsServerTrust(NSURLAuthenticationMethodClientCertificate))
        XCTAssertFalse(URLSessionEventStreamTransport.acceptsServerTrust(NSURLAuthenticationMethodHTTPDigest))
    }

    func testCloseCodesMapToTruthfulReasons() {
        XCTAssertEqual(EventStreamEndpoint.closeReason(for: .init(rawValue: 4001)!), .unauthorized)
        XCTAssertEqual(EventStreamEndpoint.closeReason(for: .init(rawValue: 4003)!), .forbiddenOrigin)
        XCTAssertEqual(EventStreamEndpoint.closeReason(for: .normalClosure), .ended)
        XCTAssertEqual(EventStreamEndpoint.closeReason(for: .goingAway), .ended)
    }

    // MARK: Decoder

    func testDecoderReadsTheSnapshotAndOnlyTheFieldsThisClientUses() {
        let snapshot = Data("""
        {"type":"snapshot","active_streams":[
          {"chat_id":"chat-1","project_id":"p1"},
          {"chat_id":"chat-2","project_id":"p1","background":true},
          {"project_id":"missing-id"},
          {"chat_id":""}
        ],"chat_pins":{}}
        """.utf8)
        XCTAssertEqual(
            ChatEventDecoder.decode(snapshot),
            .snapshot(activeChatIDs: ["chat-1", "chat-2"])
        )

        let result = Data("""
        {"type":"chat_result_ready","chat_id":"chat-1","project_id":"p1","title":"Answer","snippet":"Done"}
        """.utf8)
        XCTAssertEqual(
            ChatEventDecoder.decode(result),
            .resultReady(chatID: "chat-1", title: "Answer", snippet: "Done", workspace: "")
        )
    }

    func testDecoderIgnoresUnrelatedEventsAndFailsClosedOnBadShapes() {
        XCTAssertEqual(
            ChatEventDecoder.decode(Data(#"{"type":"chat_unread","chat_id":"chat-1"}"#.utf8)),
            .ignored
        )
        XCTAssertEqual(
            ChatEventDecoder.decode(Data(#"{"type":"chat_read","chat_id":"chat-1","last_read_at":"2026-11-01T10:00:00Z"}"#.utf8)),
            .chatRead(chatID: "chat-1")
        )
        XCTAssertNil(ChatEventDecoder.decode(Data(#"{"type":"chat_read"}"#.utf8)))
        XCTAssertNil(ChatEventDecoder.decode(Data(#"{"type":"snapshot"}"#.utf8)))
        XCTAssertNil(ChatEventDecoder.decode(Data(#"{"type":"chat_result_ready"}"#.utf8)))
        XCTAssertNil(ChatEventDecoder.decode(Data("not json".utf8)))
        XCTAssertNil(ChatEventDecoder.decode(Data(repeating: 0x20, count: ChatEventDecoder.maximumMessageBytes + 1)))
    }

    func testDecoderBoundsServerStrings() {
        let longTitle = String(repeating: "t", count: ChatEventDecoder.maximumTitleCharacters + 10)
        let longSnippet = String(repeating: "s", count: ChatEventDecoder.maximumSnippetCharacters + 10)
        let longWorkspace = String(repeating: "w", count: ChatEventDecoder.maximumWorkspaceCharacters + 10)
        let payload = """
        {"type":"chat_result_ready","chat_id":"chat-1","title":"\(longTitle)","snippet":"\(longSnippet)","workspace":"\(longWorkspace)"}
        """
        guard case .resultReady(_, let title, let snippet, let workspace)? = ChatEventDecoder.decode(Data(payload.utf8)) else {
            return XCTFail("expected a result event")
        }
        XCTAssertEqual(title.count, ChatEventDecoder.maximumTitleCharacters)
        XCTAssertEqual(snippet.count, ChatEventDecoder.maximumSnippetCharacters)
        XCTAssertEqual(workspace.count, ChatEventDecoder.maximumWorkspaceCharacters)
    }

    func testDecoderAcceptsAResultWithoutAWorkspace() {
        // A single-workspace server omits the field on purpose and an older one
        // never sent it; both must decode as "no workspace", not fail.
        let payload = #"{"type":"chat_result_ready","chat_id":"chat-1","title":"Answer","snippet":"Done"}"#
        guard case .resultReady(_, _, _, let workspace)? = ChatEventDecoder.decode(Data(payload.utf8)) else {
            return XCTFail("a result without a workspace is still a result")
        }
        XCTAssertEqual(workspace, "")
    }

    // MARK: Real loopback WebSocket

    func testHandshakeCarriesOnlyTheExactOriginAndSessionCookie() throws {
        let snapshot = #"{"type":"snapshot","active_streams":[{"chat_id":"chat-1","project_id":"p1"}]}"#
        let result = #"{"type":"chat_result_ready","chat_id":"chat-1","project_id":"p1","title":"Answer","snippet":"Done"}"#
        let fixture = try WebSocketFixtureServer { _ in .webSocket([snapshot, result]) }
        defer { fixture.shutdown() }

        let received = expectation(description: "received snapshot and result")
        received.expectedFulfillmentCount = 2
        var events: [ChatEvent] = []
        let transport = URLSessionEventStreamTransport()
        let connection = transport.open(
            origin: fixture.origin,
            cookieHeader: cookie,
            onEvent: { event in
                events.append(event)
                received.fulfill()
            },
            onClose: { _ in }
        )
        XCTAssertNotNil(connection)
        wait(for: [received], timeout: 5)
        connection?.cancel()

        XCTAssertEqual(events.first, .snapshot(activeChatIDs: ["chat-1"]))
        XCTAssertEqual(events.last, .resultReady(chatID: "chat-1", title: "Answer", snippet: "Done", workspace: ""))

        XCTAssertTrue(fixture.waitForRequest())
        let request = try XCTUnwrap(fixture.observedRequests.first)
        XCTAssertEqual(request.requestLine, "GET /ws/events HTTP/1.1")
        XCTAssertEqual(request.headers["origin"], "http://127.0.0.1:\(fixture.port)")
        XCTAssertEqual(request.headers["cookie"], cookie)
        XCTAssertNil(request.headers["authorization"])
        XCTAssertFalse(request.headers["cookie"]?.contains("other") ?? false)
    }

    func testRedirectIsRefusedWithoutContactingTheNewOrigin() throws {
        let second = try WebSocketFixtureServer { _ in .webSocket([]) }
        defer { second.shutdown() }
        let first = try WebSocketFixtureServer { _ in
            .redirect("http://127.0.0.1:\(second.port)/ws/events")
        }
        defer { first.shutdown() }

        let closed = expectation(description: "redirect closed the socket")
        var reason: EventStreamCloseReason?
        let transport = URLSessionEventStreamTransport()
        let connection = transport.open(
            origin: first.origin,
            cookieHeader: cookie,
            onEvent: { _ in },
            onClose: { value in
                reason = value
                closed.fulfill()
            }
        )
        XCTAssertNotNil(connection)
        wait(for: [closed], timeout: 5)

        // The exact reason can be the delegate's refusal or the session's
        // refusal of the redirect; either way the credential must not travel.
        XCTAssertTrue(reason == .redirected || reason == .failed, "unexpected close: \(String(describing: reason))")
        XCTAssertFalse(second.waitForRequest(timeout: 0.5), "A redirect must not authenticate to another origin.")
    }

    func testUnauthorizedHandshakeClosesWithoutDelivery() throws {
        let fixture = try WebSocketFixtureServer { _ in .reject(status: 401) }
        defer { fixture.shutdown() }

        let closed = expectation(description: "rejected socket closed")
        var reason: EventStreamCloseReason?
        let transport = URLSessionEventStreamTransport()
        let connection = transport.open(
            origin: fixture.origin,
            cookieHeader: cookie,
            onEvent: { _ in XCTFail("no event may arrive from a rejected handshake") },
            onClose: { value in
                reason = value
                closed.fulfill()
            }
        )
        XCTAssertNotNil(connection)
        wait(for: [closed], timeout: 5)
        XCTAssertTrue(reason == .ended || reason == .failed || reason == .unauthorized,
                      "unexpected close: \(String(describing: reason))")
        XCTAssertTrue(fixture.waitForRequest())
    }
}
