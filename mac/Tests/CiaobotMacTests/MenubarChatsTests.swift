import XCTest
import Foundation
import AppKit
import WebKit
import Combine

@testable import CiaobotMac

// MARK: - Doubles

private final class FakeChatTransport: ChatListTransport {
    struct Call {
        let origin: Origin
        let limit: Int
        let cookieHeader: String?
    }

    private(set) var calls: [Call] = []
    private(set) var cancelCount = 0
    private var pending: [Int: (Result<ChatListPayload, ChatListFailure>) -> Void] = [:]
    var automaticResult: Result<ChatListPayload, ChatListFailure>?

    func fetch(
        origin: Origin,
        limit: Int,
        cookieHeader: String?,
        completion: @escaping (Result<ChatListPayload, ChatListFailure>) -> Void
    ) -> (() -> Void)? {
        calls.append(Call(origin: origin, limit: limit, cookieHeader: cookieHeader))
        let index = calls.count - 1
        if let automaticResult {
            completion(automaticResult)
        } else {
            pending[index] = completion
        }
        return { [weak self] in self?.cancelCount += 1 }
    }

    func complete(_ index: Int, with result: Result<ChatListPayload, ChatListFailure>) {
        guard let completion = pending.removeValue(forKey: index) else { return }
        completion(result)
    }

    var pendingCount: Int { pending.count }
}

private final class FakeCookieProvider: ChatCookieProviding {
    var cookies: [HTTPCookie] = []
    private(set) var callCount = 0
    var retainsCompletion = false
    private(set) var completion: (([HTTPCookie]) -> Void)?

    func cookies(for url: URL, completion: @escaping ([HTTPCookie]) -> Void) {
        callCount += 1
        if retainsCompletion { self.completion = completion } else { completion(cookies) }
    }

    func completeTwice() { completion?(cookies); completion?(cookies) }
    func dropCompletion() { completion = nil }
}

private final class TrackingEventConnection: EventStreamConnection {
    func cancel() {}
}

private final class TrackingEventTransport: EventStreamTransport {
    private(set) var count = 0
    func open(origin: Origin, cookieHeader: String, onEvent: @escaping (ChatEvent) -> Void,
              onClose: @escaping (EventStreamCloseReason) -> Void) -> EventStreamConnection? {
        count += 1
        return TrackingEventConnection()
    }
}

@MainActor
private final class FakeChatList: ChatListProviding {
    let subject: CurrentValueSubject<ChatListState, Never>
    private(set) var startCount = 0
    private(set) var stopCount = 0
    private(set) var refreshCount = 0

    init(_ state: ChatListState = .disconnected) {
        subject = CurrentValueSubject(state)
    }

    var state: ChatListState { subject.value }
    var statePublisher: AnyPublisher<ChatListState, Never> { subject.eraseToAnyPublisher() }

    func start() { startCount += 1 }
    func stop() { stopCount += 1 }
    func refreshIfNeeded() { refreshCount += 1 }
    func set(_ state: ChatListState) { subject.send(state) }
}

@MainActor
private final class ChatFakeStatusItem: MenuBarStatusItem {
    var title: String?
    var image: NSImage?
    var toolTip: String?
    var menu: NSMenu?
    var isVisible = false
    private(set) var removeCount = 0

    func remove() { removeCount += 1 }
}

@MainActor
private final class ChatFakeStatusItemFactory: StatusItemFactory {
    private(set) var items: [ChatFakeStatusItem] = []

    func makeStatusItem(length: CGFloat) -> MenuBarStatusItem {
        let item = ChatFakeStatusItem()
        items.append(item)
        return item
    }
}

@MainActor
private final class RecordingNavigationDelegate: NSObject, WKNavigationDelegate {
    private(set) var requestedURL: URL?

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        requestedURL = navigationAction.request.url
        decisionHandler(.cancel)
    }
}

// MARK: - Fixture server and URL protocol

private struct ChatFixtureResponse {
    let status: Int
    let headers: [String: String]
    let body: Data
}

private final class ChatFixtureServer {
    let port: Int

    private let listener: Int32
    private let lock = NSLock()
    private let queue = DispatchQueue(label: "CiaobotMacTests.ChatFixture", attributes: .concurrent)
    private var handler: (String) -> ChatFixtureResponse
    private var targets: [String] = []
    private var cookieHeaders: [String?] = []
    private var stopped = false
    private lazy var thread = Thread { [weak self] in self?.run() }

    init(handler: @escaping (String) -> ChatFixtureResponse) throws {
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

    var origin: String { "http://127.0.0.1:\(port)" }

    var observedTargets: [String] {
        lock.lock()
        defer { lock.unlock() }
        return targets
    }

    var observedCookieHeaders: [String?] {
        lock.lock()
        defer { lock.unlock() }
        return cookieHeaders
    }

    func setHandler(_ handler: @escaping (String) -> ChatFixtureResponse) {
        lock.lock()
        self.handler = handler
        lock.unlock()
    }

    func waitForRequest(timeout: TimeInterval = 2) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            lock.lock()
            let found = !targets.isEmpty
            lock.unlock()
            if found { return true }
            usleep(10_000)
        }
        lock.lock()
        defer { lock.unlock() }
        return !targets.isEmpty
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
        guard let request = readRequest(client),
              let requestLine = request.components(separatedBy: "\r\n").first else { return }
        let parts = requestLine.split(separator: " ", maxSplits: 2)
        guard parts.count >= 2 else { return }
        let target = String(parts[1])
        let cookie = request.components(separatedBy: "\r\n")
            .first { $0.lowercased().hasPrefix("cookie:") }
            .map { String($0.dropFirst("cookie:".count)).trimmingCharacters(in: .whitespaces) }

        lock.lock()
        targets.append(target)
        cookieHeaders.append(cookie)
        let response = handler(target)
        lock.unlock()

        writeResponse(client, response)
    }

    private func readRequest(_ client: Int32) -> String? {
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
        guard bytes.range(of: Data("\r\n\r\n".utf8)) != nil else { return nil }
        return String(decoding: bytes, as: UTF8.self)
    }

    private func writeResponse(_ client: Int32, _ response: ChatFixtureResponse) {
        var headers = response.headers
        headers["Content-Length"] = String(response.body.count)
        headers["Cache-Control"] = "no-store"
        headers["Connection"] = "close"
        let reason: String
        switch response.status {
        case 200: reason = "OK"
        case 401: reason = "Unauthorized"
        case 302: reason = "Found"
        default: reason = "Error"
        }
        var text = "HTTP/1.1 \(response.status) \(reason)\r\n"
        for (key, value) in headers {
            text += "\(key): \(value)\r\n"
        }
        text += "\r\n"
        var bytes = Array(text.utf8)
        bytes.append(contentsOf: response.body)
        var sent = 0
        let deadline = Date().addingTimeInterval(3)
        while sent < bytes.count {
            let remaining = Int32(max(1, deadline.timeIntervalSinceNow * 1000))
            var descriptor = pollfd(fd: client, events: Int16(POLLOUT), revents: 0)
            guard poll(&descriptor, 1, remaining) == 1 else { return }
            let count = bytes.withUnsafeBytes { buffer in
                send(client, buffer.baseAddress!.advanced(by: sent), bytes.count - sent, 0)
            }
            guard count > 0 else { return }
            sent += count
        }
    }
}

private func fixtureError(_ operation: String) -> NSError {
    NSError(domain: "CiaobotMacTests.ChatFixture", code: 1, userInfo: [NSLocalizedDescriptionKey: operation])
}

private final class ChatURLProtocol: URLProtocol {
    static var handler: ((URLRequest, ChatURLProtocol) -> Void)?
    static var requests: [URLRequest] = []

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.requests.append(request)
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: fixtureError("missing handler"))
            return
        }
        handler(request, self)
    }

    override func stopLoading() {}
}

// MARK: - Tests

@MainActor
final class MenubarChatsTests: XCTestCase {
    private let loopback = Origin("http://127.0.0.1:8443")!
    private let remote = Origin("https://example.com")!

    override func tearDown() {
        ChatURLProtocol.handler = nil
        ChatURLProtocol.requests = []
        super.tearDown()
    }

    // MARK: Endpoint and link construction

    func testEndpointIsFixedOnTheExactSelectedOrigin() throws {
        let url = try XCTUnwrap(ChatListEndpoint.url(origin: loopback, limit: 12))
        XCTAssertEqual(url.absoluteString, "http://127.0.0.1:8443/api/menubar-chats?limit=12")
        XCTAssertTrue(ChatListEndpoint.matches(url, origin: loopback))
        XCTAssertFalse(ChatListEndpoint.matches(url, origin: Origin("http://127.0.0.1:9443")!))
        XCTAssertFalse(ChatListEndpoint.matches(url, origin: Origin("https://127.0.0.1:8443")!))
        XCTAssertFalse(ChatListEndpoint.matches(URL(string: "https://example.com/api/menubar-chats?limit=12")!, origin: loopback))
        XCTAssertFalse(ChatListEndpoint.matches(URL(string: "http://127.0.0.1:8443/api/other")!, origin: loopback))
    }

    func testEndpointClampsTheServerLimit() throws {
        let low = try XCTUnwrap(ChatListEndpoint.url(origin: loopback, limit: 0))
        let high = try XCTUnwrap(ChatListEndpoint.url(origin: loopback, limit: 500))
        XCTAssertTrue(low.absoluteString.hasSuffix("limit=1"))
        XCTAssertTrue(high.absoluteString.hasSuffix("limit=50"))
    }

    func testChatLinkAcceptsOnlyOneUnreservedPathSegment() throws {
        XCTAssertEqual(
            ChatLink.url(origin: loopback, chatID: "chat-0123_ab")?.absoluteString,
            "http://127.0.0.1:8443/chat/chat-0123_ab"
        )
        for bad in ["", "..", ".", "a/b", "a?b", "a#b", "a%2Fb", "a b", "a\nb", "é"] {
            XCTAssertNil(ChatLink.url(origin: loopback, chatID: bad), "accepted unsafe chat id: \(bad)")
            XCTAssertFalse(ChatLink.isValidChatID(bad))
        }
        let tooLong = String(repeating: "a", count: 201)
        XCTAssertNil(ChatLink.url(origin: loopback, chatID: tooLong))
    }

    func testChatLinkNeverLeavesTheSelectedOrigin() {
        let url = ChatLink.url(origin: remote, chatID: "chat-1")
        XCTAssertEqual(url?.host, "example.com")
        XCTAssertEqual(url?.scheme, "https")
        XCTAssertEqual(url?.path, "/chat/chat-1")
        XCTAssertNil(url?.query)
        XCTAssertNil(url?.fragment)
        XCTAssertEqual(remote.navigation(url!, userInitiated: false), .inWindow)
    }

    // MARK: Cookie selection

    func testCookieHeaderIsScopedToHostPathSchemeAndExpiry() {
        let url = URL(string: "https://example.com/api/menubar-chats?limit=12")!
        let now = Date(timeIntervalSince1970: 1_000_000)
        let valid = makeCookie(name: "ciao_session", value: "secret", domain: "example.com", secure: true)
        let sibling = makeCookie(name: "ciao_session", value: "sibling", domain: ".evil.example")
        let otherHost = makeCookie(name: "ciao_session", value: "other", domain: "other.example.com")
        let wrongPath = makeCookie(name: "ciao_session", value: "path", domain: "example.com", path: "/private")
        let expired = makeCookie(
            name: "ciao_session",
            value: "expired",
            domain: "example.com",
            expires: now.addingTimeInterval(-1)
        )
        let unsafe = makeCookie(name: "ciao_session", value: "bad;value", domain: "example.com")

        let cookies = [valid, sibling, otherHost, wrongPath, expired, unsafe]
        let header = SessionCookiePolicy.header(for: url, cookies: cookies, now: now)
        XCTAssertEqual(header, "ciao_session=secret")
        XCTAssertTrue(SessionCookiePolicy.containsSessionCookie(for: url, cookies: cookies, now: now))

        let insecure = URL(string: "http://example.com/api/menubar-chats")!
        XCTAssertNil(SessionCookiePolicy.header(for: insecure, cookies: [valid], now: now))
    }

    func testDomainCookieMatchesOnlyItsOwnSubtree() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let parent = makeCookie(name: "ciao_session", value: "parent", domain: ".example.com", secure: true)
        let evil = makeCookie(name: "ciao_session", value: "evil", domain: ".evil.example", secure: true)
        let sub = URL(string: "https://sub.example.com/api/menubar-chats")!
        XCTAssertEqual(SessionCookiePolicy.header(for: sub, cookies: [parent, evil], now: now), "ciao_session=parent")
    }

    func testIPv6LoopbackCookieDomainIsNormalized() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        let url = URL(string: "http://[::1]:8443/api/menubar-chats?limit=12")!
        let cookie = makeCookie(name: "ciao_session", value: "v6", domain: "::1")
        XCTAssertEqual(SessionCookiePolicy.header(for: url, cookies: [cookie], now: now), "ciao_session=v6")
    }

    // MARK: Payload validation

    func testPayloadKeepsRowsWhenTheCountIsMissingOrInvalid() {
        let flagged = chat("c1", unread: true)
        XCTAssertEqual(
            ChatListPayload.decode(data(chats: [flagged], attention: 1)),
            .success(ChatListPayload(chats: [flagged], attentionCount: 1))
        )
        for invalid in [
            data(chats: [flagged], attention: nil),
            data(chats: [flagged], attention: -1),
            data(chats: [flagged], attention: 0),
            data(chats: [flagged], attention: 2),
            data(chats: [], attention: 2),
        ] {
            guard case .success(let payload) = ChatListPayload.decode(invalid, limit: 12) else {
                return XCTFail("an invalid count must not discard valid rows")
            }
            XCTAssertNil(payload.attentionCount, "an invalid count must not be displayed")
        }
        guard case .success(let missing) = ChatListPayload.decode(data(chats: [flagged], attention: nil), limit: 12) else {
            return XCTFail("a missing count must not discard valid rows")
        }
        XCTAssertEqual(missing.chats, [flagged])

        var wrongTypeObject = try! JSONSerialization.jsonObject(
            with: data(chats: [flagged], attention: nil)
        ) as! [String: Any]
        wrongTypeObject["attention_count"] = "many"
        let wrongType = try! JSONSerialization.data(withJSONObject: wrongTypeObject)
        guard case .success(let wrongTyped) = ChatListPayload.decode(wrongType, limit: 12) else {
            return XCTFail("a wrongly typed count must not discard valid rows")
        }
        XCTAssertEqual(wrongTyped.chats, [flagged])
        XCTAssertNil(wrongTyped.attentionCount)

        // No undocumented ordering assumption: a full 12-row page may be
        // truncated even when only three of the returned rows are flagged.
        var twelve = (0..<12).map { chat("c\($0)") }
        twelve[0] = chat("c0", unread: true)
        twelve[1] = chat("c1", unread: true)
        twelve[2] = chat("c2", unread: true)
        guard case .success(let truncated) = ChatListPayload.decode(data(chats: twelve, attention: 20), limit: 12) else {
            return XCTFail("a documented full page must not be rejected for row ordering")
        }
        XCTAssertEqual(truncated.attentionCount, 20)
        XCTAssertEqual(truncated.chats, twelve)

        // Duplicate/empty ids are row-level defects; keep the first valid row
        // and drop the ambiguous duplicate, without discarding the feed.
        guard case .success(let duplicate) = ChatListPayload.decode(data(chats: [flagged, flagged, chat("")], attention: 2), limit: 12) else {
            return XCTFail("duplicate ids must not discard the valid feed")
        }
        XCTAssertEqual(duplicate.chats, [flagged])
        XCTAssertNil(duplicate.attentionCount)

        if case .failure = ChatListPayload.decode(Data("not json".utf8)) {} else {
            XCTFail("non-JSON must fail closed")
        }
        if case .failure = ChatListPayload.decode(Data(#"{"attention_count":0}"#.utf8)) {} else {
            XCTFail("a missing chats array must fail closed")
        }
    }

    func testPayloadAcceptanceAllowsAnUnflaggedOnlyList() {
        let quiet = chat("c1")
        XCTAssertEqual(
            ChatListPayload.decode(data(chats: [quiet], attention: 0)),
            .success(ChatListPayload(chats: [quiet], attentionCount: 0))
        )
    }

    func testPayloadCarriesTheServersWorkspaceFactAndDefaultsToUnknown() {
        let quiet = chat("c1")
        XCTAssertEqual(
            ChatListPayload.decode(data(chats: [quiet], attention: 0, multipleWorkspaces: true)),
            .success(ChatListPayload(chats: [quiet], attentionCount: 0, multipleWorkspaces: true))
        )
        guard case .success(let absent) = ChatListPayload.decode(data(chats: [quiet], attention: 0)) else {
            return XCTFail("an absent workspace fact must not discard valid rows")
        }
        XCTAssertFalse(absent.multipleWorkspaces, "An older server's silence means unknown, not multiple.")

        var wrongType = try! JSONSerialization.jsonObject(
            with: data(chats: [quiet], attention: 0)
        ) as! [String: Any]
        wrongType["multiple_workspaces"] = "yes"
        guard case .success(let wrongTyped) = ChatListPayload.decode(
            try! JSONSerialization.data(withJSONObject: wrongType)
        ) else {
            return XCTFail("a wrongly typed workspace fact must not discard valid rows")
        }
        XCTAssertFalse(wrongTyped.multipleWorkspaces)
        XCTAssertEqual(wrongTyped.chats, [quiet])
    }

    // MARK: Text safety

    func testMenuTextRemovesControlsBidiAndLineBreaksAndBounds() {
        let raw = "  Alpha\nQuit Ciaobot\u{202E}\u{200B}\u{0007}  "
        let cleaned = MenuTextSanitizer.sanitize(raw, limit: 100, fallback: "Untitled chat")
        XCTAssertEqual(cleaned, "Alpha Quit Ciaobot")
        XCTAssertFalse(cleaned.unicodeScalars.contains { scalar in
            let category = scalar.properties.generalCategory
            return category == .control || category == .format || category == .lineSeparator || category == .paragraphSeparator
        })

        let long = String(repeating: "a", count: 150)
        let clipped = MenuTextSanitizer.sanitize(long, limit: 20, fallback: "Untitled chat")
        XCTAssertEqual(clipped.count, 20)
        XCTAssertTrue(clipped.hasSuffix("…"))
        let combining = "a" + String(repeating: "\u{0301}", count: 1000)
        let boundedCombining = MenuTextSanitizer.sanitize(combining, limit: 10, fallback: "Untitled chat")
        XCTAssertLessThanOrEqual(boundedCombining.count, 10)
        XCTAssertLessThanOrEqual(boundedCombining.unicodeScalars.count, 80)
        XCTAssertEqual(MenuTextSanitizer.sanitize(" \n\t ", limit: 20, fallback: "Fallback"), "Fallback")
    }

    func testMenuRowsUseSanitizedBoundedTitleAndWorkspaceText() throws {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        let snapshot = ChatListSnapshot(
            origin: "http://127.0.0.1:8443",
            chats: [
                chat("chat-1", title: "Title\nQuit Ciaobot\u{202E}", workspace: "Work\nQuit Ciaobot\u{202E}", unread: true),
                chat("chat-2", title: String(repeating: "x", count: 150), workspace: String(repeating: "w", count: 80), unread: true),
            ],
            attentionCount: 2,
            fetchedAt: Date()
        )
        let controller = MenuBarController(
            status: status,
            statusItemFactory: ChatFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: FakeChatList(.loaded(snapshot)),
            openChat: { _ in true }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        let sanitized = try XCTUnwrap(row(menu, id: "chat-1"))
        XCTAssertEqual(titleWithoutTime(sanitized), "Title Quit Ciaobot")
        XCTAssertEqual(sanitized.toolTip, "new reply · Open in Work Quit Ciaobot")
        XCTAssertFalse(sanitized.title.unicodeScalars.contains { scalar in
            let category = scalar.properties.generalCategory
            return category == .control || category == .format || category == .lineSeparator || category == .paragraphSeparator
        })

        let longRow = try XCTUnwrap(row(menu, id: "chat-2"))
        XCTAssertEqual(titleWithoutTime(longRow).count, 96, "untrusted chat titles must be bounded")
        let tooltip = try XCTUnwrap(longRow.toolTip)
        XCTAssertLessThanOrEqual(tooltip.count, "new reply · Open in ".count + 48, "untrusted workspace names must be bounded")
        XCTAssertFalse(tooltip.unicodeScalars.contains { scalar in
            let category = scalar.properties.generalCategory
            return category == .control || category == .format || category == .lineSeparator || category == .paragraphSeparator
        })
    }

    func testCleanedWorkspaceLabelsStayInTooltipsWithoutDuplicatingRows() throws {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        let snapshot = ChatListSnapshot(
            origin: "http://127.0.0.1:8443",
            chats: [
                chat("chat-1", title: "One", workspace: "work", unread: true),
                chat("chat-2", title: "Two", workspace: "work\u{202E}\n", unread: true),
            ],
            attentionCount: 2,
            fetchedAt: Date()
        )
        let controller = MenuBarController(
            status: status,
            statusItemFactory: ChatFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: FakeChatList(.loaded(snapshot)),
            openChat: { _ in true }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        XCTAssertEqual(menu.items.filter { !$0.isSeparatorItem && $0.representedObject != nil }.count, 2)
        let one = try XCTUnwrap(row(menu, id: "chat-1"))
        let two = try XCTUnwrap(row(menu, id: "chat-2"))
        XCTAssertEqual(one.toolTip, "new reply · Open in work")
        XCTAssertEqual(two.toolTip, "new reply · Open in work")
        XCTAssertNotEqual(one.title, two.title)
        XCTAssertFalse(menu.items.contains { $0.title == "work" }, "One cleaned workspace means no header.")
    }

    func testSingleWorkspaceMenuHasNoWorkspaceHeader() throws {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        let snapshot = ChatListSnapshot(
            origin: "http://127.0.0.1:8443",
            chats: [
                chat("chat-1", title: "One", workspace: "personal", unread: true),
                chat("chat-2", title: "Two", workspace: "personal", unread: true),
            ],
            attentionCount: 2,
            fetchedAt: Date()
        )
        let controller = MenuBarController(
            status: status,
            statusItemFactory: ChatFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: FakeChatList(.loaded(snapshot)),
            openChat: { _ in true },
            now: { self.fixedNow }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        let titles = menu.items.filter { !$0.isSeparatorItem }.map(\.title)
        XCTAssertEqual(titles, [
            "Connected to http://127.0.0.1:8443",
            "One · now",
            "Two · now",
            "Open Ciaobot",
            "Quit Ciaobot",
        ])
    }

    func testMultiWorkspaceMenuHeadersASingleFlaggedWorkspace() throws {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        // The server serves both workspaces, but every chat needing attention
        // this time comes from one of them. Without the header the rows would
        // not say which workspace they came from.
        let snapshot = ChatListSnapshot(
            origin: "http://127.0.0.1:8443",
            chats: [
                chat("chat-1", title: "One", workspace: "work", unread: true),
                chat("chat-2", title: "Two", workspace: "work", unread: true),
                chat("chat-3", title: "Quiet", workspace: "personal", unread: false),
            ],
            attentionCount: 2,
            fetchedAt: Date()
        )
        let controller = MenuBarController(
            status: status,
            statusItemFactory: ChatFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: FakeChatList(.loaded(snapshot)),
            openChat: { _ in true },
            now: { self.fixedNow }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        let titles = menu.items.filter { !$0.isSeparatorItem }.map(\.title)
        XCTAssertEqual(titles, [
            "Connected to http://127.0.0.1:8443",
            "work",
            "One · now",
            "Two · now",
            "Open Ciaobot",
            "Quit Ciaobot",
        ])
        let header = try XCTUnwrap(menu.items.first { $0.title == "work" })
        XCTAssertFalse(header.isEnabled, "A workspace header is a label, not an action.")
        XCTAssertNil(header.action)
        XCTAssertNil(header.representedObject)
        XCTAssertFalse(menu.items.contains { $0.title == "personal" },
                       "A quiet workspace has no row to head, so it gets no empty header.")
    }

    func testServersWorkspaceFactHeadsAFullySingleWorkspacePage() throws {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        // The page is full of work chats, so it cannot itself name more than
        // one workspace; the server says it serves two.
        let snapshot = ChatListSnapshot(
            origin: "http://127.0.0.1:8443",
            chats: [
                chat("chat-1", title: "One", workspace: "work", unread: true),
                chat("chat-2", title: "Two", workspace: "work", unread: true),
            ],
            attentionCount: 2,
            multipleWorkspaces: true,
            fetchedAt: Date()
        )
        let controller = MenuBarController(
            status: status,
            statusItemFactory: ChatFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: FakeChatList(.loaded(snapshot)),
            openChat: { _ in true },
            now: { self.fixedNow }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        let titles = menu.items.filter { !$0.isSeparatorItem }.map(\.title)
        XCTAssertEqual(titles, [
            "Connected to http://127.0.0.1:8443",
            "work",
            "One · now",
            "Two · now",
            "Open Ciaobot",
            "Quit Ciaobot",
        ])
    }

    // MARK: ChatListModel

    func testListCookieReadTracksUntilFirstCompletionAfterStop() throws {
        let status = AppSessionStatus()
        let origin = try XCTUnwrap(Origin("https://tracked.example"))
        let store = WKWebsiteDataStore.nonPersistent()
        status.reportConnected(origin, dataStore: store)
        let provider = FakeCookieProvider()
        provider.retainsCompletion = true
        let registry = HostStoreUseRegistry()
        let source = MutableHostStoreSource(.init(origin: origin, store: store, identity: .ephemeral(UUID())))
        let bundle = HostStoreTrackingBundle(source: source, registry: registry)
        let model = ChatListModel(context: status, transport: FakeChatTransport(), cookieProviderFactory: { _ in provider }, hostStoreTracking: bundle)
        model.start()
        XCTAssertTrue(waitUntil { provider.callCount == 1 })
        XCTAssertFalse(registry.allReleased(for: origin))
        model.stop()
        XCTAssertFalse(registry.allReleased(for: origin))
        provider.completeTwice()
        XCTAssertTrue(waitUntil { registry.allReleased(for: origin) })
        XCTAssertEqual(provider.callCount, 1)
    }

    func testListCookieReadRejectsMismatchedSnapshotAndRecovers() throws {
        let status = AppSessionStatus()
        let origin = try XCTUnwrap(Origin("https://tracked.example"))
        let store = WKWebsiteDataStore.nonPersistent()
        status.reportConnected(origin, dataStore: store)
        let provider = FakeCookieProvider()
        let transport = FakeChatTransport()
        let registry = HostStoreUseRegistry()
        let source = MutableHostStoreSource(.init(origin: origin, store: .nonPersistent(), identity: .ephemeral(UUID())))
        let bundle = HostStoreTrackingBundle(source: source, registry: registry)
        let model = ChatListModel(context: status, transport: transport, cookieProviderFactory: { _ in provider }, hostStoreTracking: bundle)
        model.start()
        XCTAssertTrue(waitUntil { if case .unavailable(.invalidResponse) = model.state { return true }; return false })
        XCTAssertEqual(provider.callCount, 0)
        XCTAssertTrue(transport.calls.isEmpty)
        source.value = .init(origin: origin, store: store, identity: .ephemeral(UUID()))
        model.refresh()
        XCTAssertTrue(waitUntil { provider.callCount == 1 })
        model.stop()
    }

    func testDroppedListCookieCallbackRemainsTrackedAfterDeallocation() throws {
        let status = AppSessionStatus()
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:45999"))
        let store = WKWebsiteDataStore.nonPersistent()
        status.reportConnected(origin, dataStore: store)
        let provider = FakeCookieProvider()
        provider.retainsCompletion = true
        let registry = HostStoreUseRegistry()
        let source = MutableHostStoreSource(.init(origin: origin, store: store, identity: .ephemeral(UUID())))
        var model: ChatListModel? = ChatListModel(context: status, transport: FakeChatTransport(), cookieProviderFactory: { _ in provider }, hostStoreTracking: .init(source: source, registry: registry))
        model?.start()
        XCTAssertTrue(waitUntil { provider.callCount == 1 })
        provider.dropCompletion()
        model?.stop()
        model = nil
        XCTAssertFalse(registry.allReleased(for: origin))
    }

    func testListTrackedLoopbackFailsClosedAndReplacesLoadedSnapshot() throws {
        let status = AppSessionStatus()
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:45999"))
        status.reportConnected(origin)
        let transport = FakeChatTransport()
        let registry = HostStoreUseRegistry()
        let source = MutableHostStoreSource(.init(origin: origin, store: nil, identity: .none))
        var excluded = false
        let model = ChatListModel(context: status, transport: transport, hostStoreTracking: .init(source: source, registry: registry, isExcluded: { _ in excluded }))
        model.start()
        XCTAssertTrue(waitUntil { transport.calls.count == 1 })
        transport.complete(0, with: .success(ChatListPayload(chats: [], attentionCount: 0)))
        XCTAssertTrue(waitUntil { if case .loaded = model.state { return true }; return false })
        excluded = true
        model.refresh()
        XCTAssertEqual(model.state, .unavailable(.invalidResponse))
        XCTAssertEqual(transport.calls.count, 1)
        excluded = false
        source.value = nil
        model.refresh()
        XCTAssertEqual(model.state, .unavailable(.invalidResponse))
        XCTAssertEqual(transport.calls.count, 1)
        source.value = .init(origin: origin, store: nil, identity: .none)
        model.refreshIfNeeded()
        XCTAssertTrue(waitUntil { transport.calls.count == 2 })
        model.stop()
    }

    func testListCookieReadIgnoresDuplicateCompletion() throws {
        let status = AppSessionStatus()
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:45999"))
        let store = WKWebsiteDataStore.nonPersistent()
        status.reportConnected(origin, dataStore: store)
        let provider = FakeCookieProvider()
        provider.retainsCompletion = true
        let transport = FakeChatTransport()
        let registry = HostStoreUseRegistry()
        let source = MutableHostStoreSource(.init(origin: origin, store: store, identity: .ephemeral(UUID())))
        let model = ChatListModel(context: status, transport: transport, cookieProviderFactory: { _ in provider }, hostStoreTracking: .init(source: source, registry: registry))
        model.start()
        XCTAssertTrue(waitUntil { provider.callCount == 1 })
        provider.completeTwice()
        XCTAssertTrue(waitUntil { registry.allReleased(for: origin) })
        XCTAssertEqual(transport.calls.count, 1)
        model.stop()
    }

    func testActivityCookieReadTracksUntilFirstCompletionAfterStop() throws {
        let status = AppSessionStatus()
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:45999"))
        let store = WKWebsiteDataStore.nonPersistent()
        status.reportConnected(origin, dataStore: store)
        let provider = FakeCookieProvider()
        provider.retainsCompletion = true
        let transport = TrackingEventTransport()
        let registry = HostStoreUseRegistry()
        let source = MutableHostStoreSource(.init(origin: origin, store: store, identity: .ephemeral(UUID())))
        var model: ChatActivityModel? = ChatActivityModel(context: status, transport: transport, cookieProviderFactory: { _ in provider }, hostStoreTracking: .init(source: source, registry: registry))
        model?.start()
        XCTAssertTrue(waitUntil { provider.callCount == 1 })
        model?.stop()
        model = nil
        XCTAssertFalse(registry.allReleased(for: origin))
        provider.completeTwice()
        XCTAssertTrue(waitUntil { registry.allReleased(for: origin) })
        XCTAssertEqual(transport.count, 0)
    }

    func testActivityCookieReadRejectsMismatchedSnapshotAndRetries() throws {
        let status = AppSessionStatus()
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:45999"))
        let store = WKWebsiteDataStore.nonPersistent()
        status.reportConnected(origin, dataStore: store)
        let provider = FakeCookieProvider()
        provider.cookies = [makeCookie(name: "ciao_session", value: "secret", domain: "127.0.0.1")]
        let transport = TrackingEventTransport()
        let registry = HostStoreUseRegistry()
        let source = MutableHostStoreSource(.init(origin: origin, store: .nonPersistent(), identity: .ephemeral(UUID())))
        let model = ChatActivityModel(context: status, transport: transport, cookieProviderFactory: { _ in provider }, hostStoreTracking: .init(source: source, registry: registry))
        model.start()
        XCTAssertEqual(model.state, .unavailable(origin: origin.canonicalKey, failure: .unavailable))
        XCTAssertEqual(provider.callCount, 0)
        XCTAssertEqual(transport.count, 0)
        source.value = .init(origin: origin, store: store, identity: .ephemeral(UUID()))
        model.refreshIfNeeded()
        XCTAssertTrue(waitUntil { transport.count == 1 })
        model.stop()
    }

    func testActivityDroppedAndStaleCookieCallbacksKeepOtherReadsTracked() throws {
        let status = AppSessionStatus()
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:45999"))
        let store = WKWebsiteDataStore.nonPersistent()
        status.reportConnected(origin, dataStore: store)
        let provider = FakeCookieProvider()
        provider.retainsCompletion = true
        let registry = HostStoreUseRegistry()
        let source = MutableHostStoreSource(.init(origin: origin, store: store, identity: .ephemeral(UUID())))
        let model = ChatActivityModel(context: status, transport: TrackingEventTransport(), cookieProviderFactory: { _ in provider }, hostStoreTracking: .init(source: source, registry: registry))
        model.start()
        XCTAssertTrue(waitUntil { provider.callCount == 1 })
        let stale = try XCTUnwrap(provider.completion)
        model.stop()
        model.start()
        XCTAssertTrue(waitUntil { provider.callCount == 2 })
        stale([])
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertFalse(registry.allReleased(for: origin), "A stale completion must not finish the newer read")
        let snapshot = HostStoreUseSnapshot(origin: origin, store: store, identity: .ephemeral(UUID()))
        XCTAssertNotNil(registry.beginOperation(snapshot: snapshot))
        XCTAssertNotNil(registry.beginOperation(snapshot: snapshot))
        XCTAssertNil(registry.beginOperation(snapshot: snapshot), "Only the stale read released a slot")
        provider.dropCompletion()
        model.stop()
        XCTAssertFalse(registry.allReleased(for: origin), "A dropped completion remains tracked")
    }

    func testActivityCookieReadIgnoresDuplicateCompletion() throws {
        let status = AppSessionStatus()
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:45999"))
        let store = WKWebsiteDataStore.nonPersistent()
        status.reportConnected(origin, dataStore: store)
        let provider = FakeCookieProvider()
        provider.retainsCompletion = true
        provider.cookies = [makeCookie(name: "ciao_session", value: "secret", domain: "127.0.0.1")]
        let transport = TrackingEventTransport()
        let registry = HostStoreUseRegistry()
        let source = MutableHostStoreSource(.init(origin: origin, store: store, identity: .ephemeral(UUID())))
        let model = ChatActivityModel(context: status, transport: transport, cookieProviderFactory: { _ in provider }, hostStoreTracking: .init(source: source, registry: registry))
        model.start()
        XCTAssertTrue(waitUntil { provider.callCount == 1 })
        provider.completeTwice()
        XCTAssertTrue(waitUntil { registry.allReleased(for: origin) })
        XCTAssertEqual(transport.count, 1)
        model.stop()
    }

    func testLoopbackFetchLoadsRowsWithoutAStoreOrCredentials() {
        let status = AppSessionStatus()
        let transport = FakeChatTransport()
        let model = ChatListModel(context: status, transport: transport)
        model.start()
        status.reportConnected(loopback)

        XCTAssertTrue(waitUntil { transport.calls.count == 1 })
        XCTAssertNil(transport.calls[0].cookieHeader, "With no store there is no per-session cookie; the local peer gate still applies.")
        XCTAssertEqual(transport.calls[0].limit, ChatListEndpoint.defaultLimit)

        let payload = ChatListPayload(chats: [chat("c1", title: "One", workspace: "work")], attentionCount: 0)
        transport.complete(0, with: .success(payload))
        XCTAssertTrue(waitUntil {
            if case .loaded(let snapshot) = model.state {
                return snapshot.chats.first?.chatID == "c1" && snapshot.origin == "http://127.0.0.1:8443"
            }
            return false
        })
        model.stop()
    }

    func testLoopbackSendsItsExistingSessionCookieButDoesNotRequireIt() {
        let status = AppSessionStatus()
        let transport = FakeChatTransport()
        let provider = FakeCookieProvider()
        provider.cookies = [
            makeCookie(name: "other", value: "keep", domain: "127.0.0.1"),
            makeCookie(name: "ciao_session", value: "local-session", domain: "127.0.0.1"),
            makeCookie(name: "ciao_session", value: "evil", domain: ".evil.example"),
        ]
        let model = ChatListModel(
            context: status,
            transport: transport,
            cookieProviderFactory: { _ in provider }
        )
        model.start()
        status.reportConnected(loopback, dataStore: .nonPersistent())

        XCTAssertTrue(waitUntil { transport.calls.count == 1 })
        XCTAssertEqual(transport.calls[0].cookieHeader, "ciao_session=local-session")
        XCTAssertEqual(provider.callCount, 1)
    }

    func testLoopbackWithoutACookieStillFetchesAndReportsSignInOn401() {
        let status = AppSessionStatus()
        let transport = FakeChatTransport()
        let provider = FakeCookieProvider()
        let model = ChatListModel(
            context: status,
            transport: transport,
            cookieProviderFactory: { _ in provider }
        )
        model.start()
        status.reportConnected(loopback, dataStore: .nonPersistent())

        XCTAssertTrue(waitUntil { transport.calls.count == 1 })
        XCTAssertNil(transport.calls[0].cookieHeader)
        transport.complete(0, with: .failure(.notSignedIn))
        XCTAssertTrue(waitUntil { model.state == .unavailable(.notSignedIn) })
    }

    func testRemoteFetchSendsOnlyTheApplicableSessionCookie() throws {
        let status = AppSessionStatus()
        let transport = FakeChatTransport()
        let provider = FakeCookieProvider()
        provider.cookies = [
            makeCookie(name: "other", value: "keep", domain: "example.com", secure: true),
            makeCookie(name: "ciao_session", value: "secret", domain: "example.com", secure: true),
            makeCookie(name: "ciao_session", value: "evil", domain: ".evil.example", secure: true),
        ]
        let model = ChatListModel(
            context: status,
            transport: transport,
            cookieProviderFactory: { _ in provider }
        )
        model.start()
        status.reportConnected(remote, dataStore: .nonPersistent())

        XCTAssertTrue(waitUntil { transport.calls.count == 1 })
        XCTAssertEqual(transport.calls[0].cookieHeader, "ciao_session=secret")
        XCTAssertEqual(provider.callCount, 1)
    }

    func testRemoteWithoutSessionCookieNeverIssuesARequest() {
        let status = AppSessionStatus()
        let transport = FakeChatTransport()
        let provider = FakeCookieProvider()
        provider.cookies = [makeCookie(name: "other", value: "keep", domain: "example.com", secure: true)]
        let model = ChatListModel(
            context: status,
            transport: transport,
            cookieProviderFactory: { _ in provider }
        )
        model.start()
        status.reportConnected(remote, dataStore: .nonPersistent())

        XCTAssertTrue(waitUntil {
            model.state == .unavailable(.notSignedIn)
        })
        XCTAssertTrue(transport.calls.isEmpty, "A remote title list must never be requested without a session.")
    }

    func testRemoteWithoutADataStoreIsNotSignedIn() {
        let status = AppSessionStatus()
        let transport = FakeChatTransport()
        let model = ChatListModel(context: status, transport: transport)
        model.start()
        status.reportConnected(remote)

        XCTAssertTrue(waitUntil { model.state == .unavailable(.notSignedIn) })
        XCTAssertTrue(transport.calls.isEmpty)
    }

    func testLogoutAndExpiredSessionAreReportedAsNotSignedIn() {
        let status = AppSessionStatus()
        let transport = FakeChatTransport()
        let provider = FakeCookieProvider()
        provider.cookies = [makeCookie(name: "ciao_session", value: "secret", domain: "example.com", secure: true)]
        let model = ChatListModel(
            context: status,
            transport: transport,
            cookieProviderFactory: { _ in provider }
        )
        model.start()
        status.reportConnected(remote, dataStore: .nonPersistent())
        XCTAssertTrue(waitUntil { transport.pendingCount == 1 })
        transport.complete(0, with: .failure(.notSignedIn))
        XCTAssertTrue(waitUntil { model.state == .unavailable(.notSignedIn) })
    }

    func testLogoutAfterLoadedClearsRowsAndShowsSignIn() {
        let status = AppSessionStatus()
        let transport = FakeChatTransport()
        let model = ChatListModel(context: status, transport: transport)
        model.start()
        status.reportConnected(loopback)
        XCTAssertTrue(waitUntil { transport.pendingCount == 1 })
        transport.complete(0, with: .success(ChatListPayload(chats: [chat("c1", title: "Before logout")], attentionCount: 0)))
        XCTAssertTrue(waitUntil {
            if case .loaded = model.state { return true }
            return false
        })

        model.refresh()
        XCTAssertTrue(waitUntil { transport.pendingCount == 1 })
        transport.complete(1, with: .failure(.notSignedIn))
        XCTAssertTrue(waitUntil { model.state == .unavailable(.notSignedIn) })
    }

    func testServerSwitchCancelsAndDropsTheOldResponse() {
        let status = AppSessionStatus()
        let transport = FakeChatTransport()
        let first = loopback
        let second = Origin("https://other.example")!
        let model = ChatListModel(context: status, transport: transport)
        model.start()
        status.reportConnected(first)
        XCTAssertTrue(waitUntil { transport.pendingCount == 1 })
        // Supply a cookie for the second origin so the model reaches transport.
        let provider = FakeCookieProvider()
        provider.cookies = [makeCookie(name: "ciao_session", value: "second", domain: "other.example", secure: true)]
        model.stop()
        status.reportDisconnected()
        let switched = ChatListModel(
            context: status,
            transport: transport,
            cookieProviderFactory: { _ in provider }
        )
        switched.start()
        status.reportConnected(second, dataStore: .nonPersistent())
        XCTAssertTrue(waitUntil { transport.pendingCount == 2 })

        transport.complete(0, with: .success(ChatListPayload(chats: [chat("old")], attentionCount: 0)))
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        if case .loaded(let snapshot) = switched.state {
            XCTAssertNotEqual(snapshot.chats.first?.chatID, "old", "A response from the previous origin must be dropped")
        }
        XCTAssertEqual(transport.pendingCount, 1)
    }

    func testDisconnectCancelsAndLateResponseCannotResurrectRows() {
        let status = AppSessionStatus()
        let transport = FakeChatTransport()
        let model = ChatListModel(context: status, transport: transport)
        model.start()
        status.reportConnected(loopback)
        XCTAssertTrue(waitUntil { transport.pendingCount == 1 })

        status.reportDisconnected()
        XCTAssertTrue(waitUntil { model.state == .disconnected })
        XCTAssertEqual(transport.cancelCount, 1)

        transport.complete(0, with: .success(ChatListPayload(chats: [chat("late")], attentionCount: 0)))
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        XCTAssertEqual(model.state, .disconnected)
    }

    func testRefreshKeepsTheLoadedRowsUntilTheNewResultArrives() {
        let status = AppSessionStatus()
        let transport = FakeChatTransport()
        let model = ChatListModel(context: status, transport: transport)
        model.start()
        status.reportConnected(loopback)
        XCTAssertTrue(waitUntil { transport.pendingCount == 1 })
        transport.complete(0, with: .success(ChatListPayload(chats: [chat("first")], attentionCount: 0)))
        XCTAssertTrue(waitUntil {
            if case .loaded = model.state { return true }
            return false
        })

        model.refresh()
        XCTAssertTrue(waitUntil { transport.calls.count == 2 })
        guard case .loaded(let snapshot) = model.state else {
            return XCTFail("Refreshing must not blank the tray while a request is in flight")
        }
        XCTAssertEqual(snapshot.chats.first?.chatID, "first")

        transport.complete(1, with: .failure(.requestFailed))
        XCTAssertTrue(waitUntil { model.state == .unavailable(.requestFailed) })
    }

    func testRefreshIfNeededHonoursTheMenuThrottleAndRefreshesLater() {
        var now = Date(timeIntervalSince1970: 1_000_000)
        let status = AppSessionStatus()
        let transport = FakeChatTransport()
        transport.automaticResult = .success(ChatListPayload(chats: [chat("c1")], attentionCount: 0))
        let model = ChatListModel(context: status, transport: transport, now: { now })
        model.start()
        status.reportConnected(loopback)
        XCTAssertTrue(waitUntil { transport.calls.count == 1 })

        now = now.addingTimeInterval(1)
        model.refreshIfNeeded()
        XCTAssertEqual(transport.calls.count, 1)

        now = now.addingTimeInterval(6)
        model.refreshIfNeeded()
        XCTAssertEqual(transport.calls.count, 2)
    }

    func testTheTrayPollsEveryTenSecondsAndDeduplicatesMenuOpensForTwoSeconds() {
        XCTAssertEqual(
            ChatListModel.defaultRefreshInterval,
            10,
            "A chat started or finished in the web app must reach the tray in about ten seconds."
        )
        XCTAssertEqual(
            ChatListModel.menuRefreshMinimumInterval,
            2,
            "Opening the menu refreshes almost immediately without hammering the server."
        )

        var now = Date(timeIntervalSince1970: 1_000_000)
        let status = AppSessionStatus()
        let transport = FakeChatTransport()
        transport.automaticResult = .success(ChatListPayload(chats: [chat("c1")], attentionCount: 0))
        let model = ChatListModel(context: status, transport: transport, now: { now })
        model.start()
        status.reportConnected(loopback)
        XCTAssertTrue(waitUntil { transport.calls.count == 1 })

        now = now.addingTimeInterval(1.9)
        model.refreshIfNeeded()
        XCTAssertEqual(transport.calls.count, 1, "A menu opened inside the two-second window reuses the fetch.")

        now = now.addingTimeInterval(0.2)
        model.refreshIfNeeded()
        XCTAssertEqual(transport.calls.count, 2, "Past two seconds the menu-open refresh fetches again.")
    }

    func testRefreshIfNeededNeverPollsWhileDisconnected() {
        let status = AppSessionStatus()
        let transport = FakeChatTransport()
        let model = ChatListModel(context: status, transport: transport)
        model.start()

        model.refreshIfNeeded()
        XCTAssertTrue(transport.calls.isEmpty, "A disconnected tray never asks the server for chats.")

        status.reportConnected(loopback)
        XCTAssertTrue(waitUntil { transport.calls.count == 1 })
        status.reportDisconnected()
        XCTAssertTrue(waitUntil { model.state == .disconnected })

        model.refreshIfNeeded()
        XCTAssertEqual(transport.calls.count, 1, "A disconnect cancels the feed and the menu-open refresh.")
    }

    func testMenuOpenRetriesNotSignedInImmediatelyAfterSignIn() {
        let status = AppSessionStatus()
        let transport = FakeChatTransport()
        let provider = FakeCookieProvider()
        let model = ChatListModel(
            context: status,
            transport: transport,
            cookieProviderFactory: { _ in provider }
        )
        model.start()
        status.reportConnected(remote, dataStore: .nonPersistent())
        XCTAssertTrue(waitUntil { model.state == .unavailable(.notSignedIn) })
        XCTAssertTrue(transport.calls.isEmpty)

        // The web view has now finished signing in; the same store returns the
        // newly set cookie. Opening the menu must not wait for the 10s timer.
        provider.cookies = [makeCookie(name: "ciao_session", value: "fresh", domain: "example.com", secure: true)]
        model.refreshIfNeeded()
        XCTAssertTrue(waitUntil { transport.calls.count == 1 })
        XCTAssertEqual(transport.calls[0].cookieHeader, "ciao_session=fresh")
    }

    func testActiveConnectionRefreshesOnItsTimer() {
        let status = AppSessionStatus()
        let transport = FakeChatTransport()
        transport.automaticResult = .success(ChatListPayload(chats: [chat("c1")], attentionCount: 0))
        let model = ChatListModel(context: status, transport: transport, refreshInterval: 0.05)
        model.start()
        status.reportConnected(loopback)

        XCTAssertTrue(waitUntil(timeout: 2) { transport.calls.count >= 2 })
        model.stop()
        let countAtStop = transport.calls.count
        RunLoop.main.run(until: Date().addingTimeInterval(0.15))
        XCTAssertEqual(transport.calls.count, countAtStop, "Stopping the menu must stop the refresh timer")
    }

    // MARK: Transport

    func testTransportSendsOnlyTheExactCookieAndNoAmbientCredentials() {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [ChatURLProtocol.self]
        config.httpAdditionalHeaders = ["Authorization": "Bearer ambient"]
        let ambient = HTTPCookieStorage()
        ambient.setCookie(makeCookie(name: "ciao_session", value: "ambient", domain: "example.com", secure: true))
        config.httpCookieStorage = ambient
        config.httpShouldSetCookies = true
        let transport = URLSessionChatListTransport(configuration: config)
        let done = expectation(description: "fixture response")

        ChatURLProtocol.handler = { request, fixture in
            XCTAssertEqual(request.url?.absoluteString, "https://example.com/api/menubar-chats?limit=12")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Cookie"), "ciao_session=exact")
            XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
            XCTAssertFalse(request.httpShouldHandleCookies)
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            fixture.client?.urlProtocol(fixture, didReceive: response, cacheStoragePolicy: .notAllowed)
            fixture.client?.urlProtocol(fixture, didLoad: self.data(chats: [self.chat("c1")], attention: 0))
            fixture.client?.urlProtocolDidFinishLoading(fixture)
        }

        _ = transport.fetch(origin: remote, limit: 12, cookieHeader: "ciao_session=exact") { result in
            guard case .success(let payload) = result else { return XCTFail("expected success") }
            XCTAssertEqual(payload.chats.first?.chatID, "c1")
            done.fulfill()
        }
        wait(for: [done], timeout: 3)
        XCTAssertEqual(ChatURLProtocol.requests.count, 1)
    }

    func testTransportRefusesRedirectsAndCrossOriginFinalURLs() {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [ChatURLProtocol.self]
        let transport = URLSessionChatListTransport(configuration: config)

        ChatURLProtocol.handler = { request, fixture in
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 302,
                httpVersion: nil,
                headerFields: ["Location": "https://evil.example/steal"]
            )!
            fixture.client?.urlProtocol(fixture, didReceive: response, cacheStoragePolicy: .notAllowed)
            fixture.client?.urlProtocolDidFinishLoading(fixture)
        }
        let redirected = expectation(description: "redirect refused")
        _ = transport.fetch(origin: remote, limit: 12, cookieHeader: "ciao_session=secret") { result in
            XCTAssertEqual(result.failure, .redirected)
            redirected.fulfill()
        }
        wait(for: [redirected], timeout: 3)
        XCTAssertEqual(ChatURLProtocol.requests.count, 1, "The redirect target must never be requested")

        ChatURLProtocol.requests = []
        ChatURLProtocol.handler = { request, fixture in
            let response = HTTPURLResponse(
                url: URL(string: "https://evil.example/steal")!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: nil
            )!
            fixture.client?.urlProtocol(fixture, didReceive: response, cacheStoragePolicy: .notAllowed)
            fixture.client?.urlProtocol(fixture, didLoad: self.data(chats: [self.chat("c1")], attention: 0))
            fixture.client?.urlProtocolDidFinishLoading(fixture)
        }
        let crossOrigin = expectation(description: "cross-origin response refused")
        _ = transport.fetch(origin: remote, limit: 12, cookieHeader: "ciao_session=secret") { result in
            XCTAssertEqual(result.failure, .redirected)
            crossOrigin.fulfill()
        }
        wait(for: [crossOrigin], timeout: 3)
        XCTAssertEqual(ChatURLProtocol.requests.count, 1)
    }

    func testTransportMapsLogoutAndRejectsOversizedBodies() {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [ChatURLProtocol.self]
        let transport = URLSessionChatListTransport(configuration: config)

        ChatURLProtocol.handler = { request, fixture in
            let response = HTTPURLResponse(url: request.url!, statusCode: 401, httpVersion: nil, headerFields: nil)!
            fixture.client?.urlProtocol(fixture, didReceive: response, cacheStoragePolicy: .notAllowed)
            fixture.client?.urlProtocolDidFinishLoading(fixture)
        }
        let loggedOut = expectation(description: "logout")
        _ = transport.fetch(origin: remote, limit: 12, cookieHeader: "ciao_session=secret") { result in
            XCTAssertEqual(result.failure, .notSignedIn)
            loggedOut.fulfill()
        }
        wait(for: [loggedOut], timeout: 3)

        ChatURLProtocol.handler = { request, fixture in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            fixture.client?.urlProtocol(fixture, didReceive: response, cacheStoragePolicy: .notAllowed)
            fixture.client?.urlProtocol(
                fixture,
                didLoad: Data(repeating: 32, count: URLSessionChatListTransport.maximumResponseBytes + 1)
            )
            fixture.client?.urlProtocolDidFinishLoading(fixture)
        }
        let oversized = expectation(description: "oversized")
        _ = transport.fetch(origin: remote, limit: 12, cookieHeader: "ciao_session=secret") { result in
            XCTAssertEqual(result.failure, .invalidResponse)
            oversized.fulfill()
        }
        wait(for: [oversized], timeout: 3)
    }

    func testTransportRefusesRemoteWithoutCookieBeforeAnyRequest() {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [ChatURLProtocol.self]
        let transport = URLSessionChatListTransport(configuration: config)
        var result: Result<ChatListPayload, ChatListFailure>?
        _ = transport.fetch(origin: remote, limit: 12, cookieHeader: nil) { result = $0 }
        XCTAssertEqual(result?.failure, .notSignedIn)
        XCTAssertTrue(ChatURLProtocol.requests.isEmpty)
    }

    func testOnlyNormalTLSValidationIsAccepted() {
        XCTAssertTrue(URLSessionChatListTransport.acceptsServerTrust(NSURLAuthenticationMethodServerTrust))
        XCTAssertFalse(URLSessionChatListTransport.acceptsServerTrust(NSURLAuthenticationMethodHTTPBasic))
        XCTAssertFalse(URLSessionChatListTransport.acceptsServerTrust(NSURLAuthenticationMethodClientCertificate))
    }

    // MARK: Isolated real HTTP fixture

    func testRealLoopbackFixtureLoadsRowsAndReportsLogout() throws {
        let payload = data(chats: [chat("c1", title: "Fixture chat", workspace: "work")], attention: 0)
        let server = try ChatFixtureServer { target in
            guard target.hasPrefix("/api/menubar-chats") else {
                return ChatFixtureResponse(status: 404, headers: [:], body: Data("no".utf8))
            }
            return ChatFixtureResponse(
                status: 200,
                headers: ["Content-Type": "application/json"],
                body: payload
            )
        }
        defer { server.shutdown() }

        let origin = try XCTUnwrap(Origin(server.origin))
        let status = AppSessionStatus()
        let model = ChatListModel(context: status, transport: URLSessionChatListTransport())
        model.start()
        status.reportConnected(origin)
        XCTAssertTrue(waitUntil { !server.observedTargets.isEmpty })
        XCTAssertTrue(waitUntil {
            if case .loaded(let snapshot) = model.state {
                return snapshot.chats.first?.title == "Fixture chat"
            }
            return false
        })

        let countAfterLoad = server.observedTargets.count
        server.setHandler { _ in
            ChatFixtureResponse(status: 401, headers: [:], body: Data())
        }
        model.refresh()
        XCTAssertTrue(waitUntil { server.observedTargets.count > countAfterLoad })
        XCTAssertTrue(waitUntil { model.state == .unavailable(.notSignedIn) })
        model.stop()
    }

    func testRealLoopbackFixtureReceivesTheExactSessionCookie() throws {
        let payload = data(chats: [chat("c1", title: "Cookie chat", workspace: "work")], attention: 0)
        let server = try ChatFixtureServer { target in
            guard target.hasPrefix("/api/menubar-chats") else {
                return ChatFixtureResponse(status: 404, headers: [:], body: Data())
            }
            return ChatFixtureResponse(
                status: 200,
                headers: ["Content-Type": "application/json"],
                body: payload
            )
        }
        defer { server.shutdown() }

        let origin = try XCTUnwrap(Origin(server.origin))
        let status = AppSessionStatus()
        let provider = FakeCookieProvider()
        provider.cookies = [makeCookie(name: "ciao_session", value: "loopback-cookie", domain: "127.0.0.1")]
        let model = ChatListModel(
            context: status,
            transport: URLSessionChatListTransport(),
            cookieProviderFactory: { _ in provider }
        )
        model.start()
        status.reportConnected(origin, dataStore: .nonPersistent())

        XCTAssertTrue(waitUntil { server.observedCookieHeaders.contains { $0 == "ciao_session=loopback-cookie" } })
        XCTAssertTrue(waitUntil {
            if case .loaded(let snapshot) = model.state { return snapshot.chats.first?.title == "Cookie chat" }
            return false
        })
        model.stop()
    }

    func testRealFixtureRedirectToAnotherOriginIsNeverFollowed() throws {
        let target = try ChatFixtureServer { _ in
            ChatFixtureResponse(status: 200, headers: [:], body: self.data(chats: [self.chat("stolen")], attention: 0))
        }
        defer { target.shutdown() }
        let source = try ChatFixtureServer { _ in
            ChatFixtureResponse(
                status: 302,
                headers: ["Location": "\(target.origin)/api/menubar-chats?limit=12"],
                body: Data()
            )
        }
        defer { source.shutdown() }

        let origin = try XCTUnwrap(Origin(source.origin))
        let transport = URLSessionChatListTransport()
        let done = expectation(description: "redirect refused")
        _ = transport.fetch(origin: origin, limit: 12, cookieHeader: nil) { result in
            XCTAssertEqual(result.failure, .redirected)
            done.fulfill()
        }
        wait(for: [done], timeout: 5)
        XCTAssertEqual(source.observedTargets.count, 1)
        XCTAssertTrue(target.observedTargets.isEmpty, "A cross-origin redirect must not be followed")
    }

    // MARK: Menu bar presentation

    func testMenuShowsAttentionRowsWithoutARepeatedCountOrTierHeaders() throws {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        let chats = [
            chat("chat-alpha", title: "Alpha", workspace: "work", unread: true),
            chat("chat-beta", title: "Beta", workspace: "personal", needsInput: true),
            chat("chat-gamma", title: "Gamma", workspace: "work", unread: true),
        ]
        let snapshot = ChatListSnapshot(
            origin: "http://127.0.0.1:8443",
            chats: chats,
            attentionCount: 3,
            fetchedAt: Date()
        )
        let chatList = FakeChatList(.loaded(snapshot))
        let factory = ChatFakeStatusItemFactory()
        var openedWindow = 0
        var openedChat: String?
        let controller = MenuBarController(
            status: status,
            statusItemFactory: factory,
            image: nil,
            openWindow: { openedWindow += 1 },
            quit: {},
            chatList: chatList,
            openChat: { id in openedChat = id; return true },
            now: { self.fixedNow }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        let titles = menu.items.filter { !$0.isSeparatorItem }.map(\.title)
        XCTAssertEqual(titles, [
            "Connected to http://127.0.0.1:8443",
            "personal",
            "Beta · now",
            "work",
            "Alpha · now",
            "Gamma · now",
            "Open Ciaobot",
            "Quit Ciaobot",
        ])
        for header in ["personal", "work"] {
            let item = try XCTUnwrap(menu.items.first { $0.title == header })
            XCTAssertFalse(item.isEnabled, "A workspace header is a label, not an action.")
            XCTAssertNil(item.action)
            XCTAssertNil(item.representedObject)
        }
        XCTAssertEqual(
            factory.items.first?.title,
            "\(MenuBarController.fallbackTitle) 3",
            "This controller has no artwork, so its fallback mark carries the count."
        )
        XCTAssertEqual(factory.items.first?.toolTip, "Ciaobot — 3 chats need attention on the server")

        let alpha = try XCTUnwrap(row(menu, id: "chat-alpha"))
        XCTAssertTrue(alpha.isEnabled)
        controller.openChat(alpha)
        XCTAssertEqual(openedWindow, 1)
        XCTAssertEqual(openedChat, "chat-alpha")
        XCTAssertEqual(chatList.refreshCount, 0)
    }

    func testAttentionBadgeShowsTheServerCountForATruncatedList() throws {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        // A limit of 12 means 12 returned rows may be a truncated view; the
        // server's 20 is the count over every non-archived chat. The badge must
        // show 20, not the 12 flagged rows it can see.
        let chats = (0..<12).map { index in
            chat("chat-\(index)", title: "Chat \(index)", unread: true)
        }
        let snapshot = ChatListSnapshot(
            origin: "http://127.0.0.1:8443",
            chats: chats,
            attentionCount: 20,
            fetchedAt: Date()
        )
        let factory = ChatFakeStatusItemFactory()
        let controller = MenuBarController(
            status: status,
            statusItemFactory: factory,
            image: nil,
            openWindow: {},
            quit: {},
            chatList: FakeChatList(.loaded(snapshot)),
            openChat: { _ in true }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        XCTAssertEqual(factory.items.first?.title, "\(MenuBarController.fallbackTitle) 20")
        XCTAssertFalse(menu.items.contains { $0.title.contains("need attention") })
        XCTAssertEqual(factory.items.first?.toolTip, "Ciaobot — 20 chats need attention on the server")
    }

    func testMenuReportsWhenAChatCannotActuallyOpen() throws {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        var openedWindow = 0
        var unavailable: [String] = []
        let controller = MenuBarController(
            status: status,
            statusItemFactory: ChatFakeStatusItemFactory(),
            image: nil,
            openWindow: { openedWindow += 1 },
            quit: {},
            chatList: FakeChatList(.loaded(snapshot(title: "Chat", id: "chat-1"))),
            openChat: { _ in false },
            chatUnavailable: { unavailable.append($0) }
        )
        controller.start()

        let row = try XCTUnwrap(row(try XCTUnwrap(controller.menu), id: "chat-1"))
        controller.openChat(row)
        XCTAssertEqual(openedWindow, 1)
        XCTAssertEqual(unavailable, ["chat-1"], "A failed selection must not be silent")
    }

    func testMenuShowsTruthfulUnavailableStatesAndNoCount() throws {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        let chatList = FakeChatList(.unavailable(.notSignedIn))
        let controller = MenuBarController(
            status: status,
            statusItemFactory: ChatFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: chatList,
            openChat: { _ in true }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        XCTAssertTrue(menu.items.contains { $0.title == MenuBarController.notSignedInTitle })
        XCTAssertFalse(menu.items.contains { $0.title.contains("need attention") })
        XCTAssertEqual(controller.statusItem?.toolTip, MenuBarController.toolTip)
    }

    func testLocalFeedRowsDoNotClaimASignedInState() throws {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        let snapshot = ChatListSnapshot(
            origin: "http://127.0.0.1:8443",
            chats: [chat("chat-1", title: "Local chat", unread: true)],
            attentionCount: 1,
            fetchedAt: Date()
        )
        let controller = MenuBarController(
            status: status,
            statusItemFactory: ChatFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: FakeChatList(.loaded(snapshot)),
            openChat: { _ in true }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        XCTAssertNotNil(row(menu, id: "chat-1"))
        XCTAssertFalse(menu.items.contains { $0.title == MenuBarController.notSignedInTitle })
        XCTAssertFalse(menu.items.contains { $0.title.lowercased().contains("signed in") })
    }

    func testMenuDisplaysRowsButNoCountWhenServerCountIsInvalid() throws {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        let snapshot = ChatListSnapshot(
            origin: "http://127.0.0.1:8443",
            chats: [chat("chat-1", title: "Valid row", unread: true)],
            attentionCount: nil,
            fetchedAt: Date()
        )
        let factory = ChatFakeStatusItemFactory()
        let controller = MenuBarController(
            status: status,
            statusItemFactory: factory,
            image: nil,
            openWindow: {},
            quit: {},
            chatList: FakeChatList(.loaded(snapshot)),
            openChat: { _ in true }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        XCTAssertNotNil(row(menu, id: "chat-1"))
        XCTAssertFalse(menu.items.contains { $0.title.contains("need attention") })
        XCTAssertEqual(factory.items.first?.toolTip, MenuBarController.toolTip)
        for (first, second) in zip(menu.items, menu.items.dropFirst()) {
            XCTAssertFalse(first.isSeparatorItem && second.isSeparatorItem, "A missing count must not leave adjacent separators")
        }
    }

    func testStaleSnapshotFromAnotherOriginIsNotShown() throws {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        let foreign = ChatListSnapshot(
            origin: "https://other.example:443",
            chats: [chat("old", title: "Other server chat", workspace: "work")],
            attentionCount: 1,
            fetchedAt: Date()
        )
        let controller = MenuBarController(
            status: status,
            statusItemFactory: ChatFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: FakeChatList(.loaded(foreign)),
            openChat: { _ in true }
        )
        controller.start()

        let menu = try XCTUnwrap(controller.menu)
        XCTAssertTrue(menu.items.contains { $0.title == MenuBarController.loadingChatsTitle })
        XCTAssertFalse(menu.items.contains { $0.title == "Other server chat" })
    }

    func testInvalidChatIDIsShownButNotSelectable() throws {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        let invalid = chat("../escape", title: "Bad id", workspace: "work", unread: true)
        let snapshot = ChatListSnapshot(
            origin: "http://127.0.0.1:8443",
            chats: [invalid],
            attentionCount: 1,
            fetchedAt: Date()
        )
        let controller = MenuBarController(
            status: status,
            statusItemFactory: ChatFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: FakeChatList(.loaded(snapshot)),
            openChat: { _ in true }
        )
        controller.start()

        let row = try XCTUnwrap(row(try XCTUnwrap(controller.menu), id: "../escape"))
        XCTAssertFalse(row.isEnabled, "A chat id that is not one safe path segment must not be clickable")
    }

    func testMenuOpenRefreshesAndDefersRebuildUntilClose() throws {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        let chatList = FakeChatList(.loaded(snapshot(title: "Old", id: "old")))
        let controller = MenuBarController(
            status: status,
            statusItemFactory: ChatFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: chatList,
            openChat: { _ in true }
        )
        controller.start()
        let menu = try XCTUnwrap(controller.menu)
        XCTAssertNotNil(row(menu, id: "old"))

        controller.menuWillOpen(menu)
        XCTAssertEqual(chatList.refreshCount, 1)

        chatList.set(.loaded(snapshot(title: "New", id: "new")))
        XCTAssertNotNil(row(menu, id: "old"))
        XCTAssertNil(row(menu, id: "new"), "An open menu is not mutated underneath the user")

        controller.menuDidClose(menu)
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        XCTAssertNotNil(row(menu, id: "new"))
        XCTAssertNil(row(menu, id: "old"))
    }

    func testClosedMenuRebuildsFromTheSettledPublishedChatState() throws {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        let old = snapshot(title: "Old", id: "old")
        let new = ChatListSnapshot(
            origin: loopback.canonicalKey,
            chats: [
                chat("new-1", title: "New one", unread: true),
                chat("new-2", title: "New two", unread: true),
            ],
            attentionCount: 2,
            fetchedAt: Date()
        )
        let chatList = FakeChatList(.loaded(old))
        let factory = ChatFakeStatusItemFactory()
        let controller = MenuBarController(
            status: status,
            statusItemFactory: factory,
            image: nil,
            openWindow: {},
            quit: {},
            chatList: chatList,
            openChat: { _ in true }
        )
        controller.start()
        let menu = try XCTUnwrap(controller.menu)
        XCTAssertNotNil(row(menu, id: "old"))

        chatList.set(.loaded(new))
        RunLoop.main.run(until: Date().addingTimeInterval(0.05))

        XCTAssertNil(row(menu, id: "old"), "The closed menu must use the new published value, not the stale snapshot.")
        XCTAssertNotNil(row(menu, id: "new-1"))
        XCTAssertNotNil(row(menu, id: "new-2"))
        XCTAssertEqual(
            factory.items.first?.toolTip,
            "Ciaobot — 2 chats need attention on the server",
            "The tooltip must be rebuilt from the settled published value too."
        )
        XCTAssertEqual(
            factory.items.first?.title,
            "\(MenuBarController.fallbackTitle) 2",
            "The badge must come from the settled published value too."
        )
    }

    func testConnectionChangeWhileMenuIsOpenInvalidatesRows() throws {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        let chatList = FakeChatList(.loaded(snapshot(title: "Old", id: "old")))
        let controller = MenuBarController(
            status: status,
            statusItemFactory: ChatFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: chatList,
            openChat: { _ in true }
        )
        controller.start()
        let menu = try XCTUnwrap(controller.menu)
        controller.menuWillOpen(menu)
        XCTAssertNotNil(row(menu, id: "old"))

        status.reportDisconnected()
        controller.menuDidClose(menu)
        XCTAssertNil(row(menu, id: "old"))
        XCTAssertTrue(menu.items.contains { $0.title == MenuBarController.disconnectedTitle })
    }

    func testStopStopsTheChatListAndRemovesTheItem() {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        let chatList = FakeChatList(.disconnected)
        let controller = MenuBarController(
            status: status,
            statusItemFactory: ChatFakeStatusItemFactory(),
            image: nil,
            openWindow: {},
            quit: {},
            chatList: chatList,
            openChat: { _ in true }
        )
        controller.start()
        XCTAssertEqual(chatList.startCount, 1)
        controller.stop()
        XCTAssertEqual(chatList.stopCount, 1)
        XCTAssertNil(controller.statusItem)
    }

    // MARK: Session navigation

    func testSessionQueuesOneChatUntilAWebViewAttaches() {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        XCTAssertTrue(status.openChat(chatID: "chat-queued"), "A missing view queues the intended path instead of claiming a navigation")

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let webView = WKWebView(frame: .zero, configuration: configuration)
        let delegate = RecordingNavigationDelegate()
        webView.navigationDelegate = delegate
        status.attachWebView(webView, for: loopback)

        XCTAssertTrue(waitUntil { delegate.requestedURL != nil })
        XCTAssertEqual(delegate.requestedURL?.absoluteString, "http://127.0.0.1:8443/chat/chat-queued")
    }

    func testNewerChatSelectionSupersedesAQueuedAttachLoad() {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        XCTAssertTrue(status.openChat(chatID: "chat-old"))

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let webView = WKWebView(frame: .zero, configuration: configuration)
        let delegate = RecordingNavigationDelegate()
        webView.navigationDelegate = delegate
        status.attachWebView(webView, for: loopback)
        XCTAssertTrue(status.openChat(chatID: "chat-new"))

        XCTAssertTrue(waitUntil { delegate.requestedURL != nil })
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        XCTAssertEqual(delegate.requestedURL?.absoluteString, "http://127.0.0.1:8443/chat/chat-new")
    }

    func testQueuedChatUsesTheLatestAttachedViewForTheSameOrigin() {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        XCTAssertTrue(status.openChat(chatID: "chat-queued"))

        let first = WKWebView(frame: .zero, configuration: {
            let configuration = WKWebViewConfiguration()
            configuration.websiteDataStore = .nonPersistent()
            return configuration
        }())
        status.attachWebView(first, for: loopback)

        let second = WKWebView(frame: .zero, configuration: {
            let configuration = WKWebViewConfiguration()
            configuration.websiteDataStore = .nonPersistent()
            return configuration
        }())
        let delegate = RecordingNavigationDelegate()
        second.navigationDelegate = delegate
        status.attachWebView(second, for: loopback)

        XCTAssertTrue(waitUntil { delegate.requestedURL != nil })
        XCTAssertEqual(delegate.requestedURL?.absoluteString, "http://127.0.0.1:8443/chat/chat-queued")
    }

    func testSessionOpensOnlyASameOriginChatPath() {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        XCTAssertFalse(status.openChat(chatID: "../escape"))
        XCTAssertFalse(status.openChat(chatID: "bad/id"))

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let webView = WKWebView(frame: .zero, configuration: configuration)
        let delegate = RecordingNavigationDelegate()
        webView.navigationDelegate = delegate
        status.attachWebView(webView, for: loopback)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        XCTAssertNil(delegate.requestedURL, "An invalid id must not queue a pending navigation")

        XCTAssertTrue(status.openChat(chatID: "chat-1"))
        XCTAssertTrue(waitUntil { delegate.requestedURL != nil })
        XCTAssertEqual(delegate.requestedURL?.absoluteString, "http://127.0.0.1:8443/chat/chat-1")

        status.reportDisconnected()
        XCTAssertFalse(status.openChat(chatID: "chat-1"))
    }

    func testLateWebViewFromAReplacedConnectionIsRefused() {
        let status = AppSessionStatus()
        status.reportConnected(loopback)
        let other = Origin("https://other.example")!
        status.reportConnected(other)

        let webView = WKWebView(frame: .zero, configuration: {
            let configuration = WKWebViewConfiguration()
            configuration.websiteDataStore = .nonPersistent()
            return configuration
        }())
        let delegate = RecordingNavigationDelegate()
        webView.navigationDelegate = delegate
        status.attachWebView(webView, for: loopback)
        XCTAssertTrue(status.openChat(chatID: "chat-1"), "The path is queued for the current origin, not the late view")
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        XCTAssertNil(delegate.requestedURL, "A late view from the old origin must not be adopted")
    }

    // MARK: Cookie store isolation

    func testCookieProviderReadsOnlyTheConnectionStore() {
        let first = WKWebsiteDataStore.nonPersistent()
        let second = WKWebsiteDataStore.nonPersistent()
        let cookie = makeCookie(name: "ciao_session", value: "store-a", domain: "example.com", secure: true)
        let set = expectation(description: "cookie set")
        first.httpCookieStore.setCookie(cookie) { set.fulfill() }
        wait(for: [set], timeout: 3)

        let firstRead = expectation(description: "first store")
        let secondRead = expectation(description: "second store")
        var firstCookies: [HTTPCookie] = []
        var secondCookies: [HTTPCookie] = []
        WebKitCookieProvider(dataStore: first).cookies(for: URL(string: "https://example.com/")!) {
            firstCookies = $0
            firstRead.fulfill()
        }
        WebKitCookieProvider(dataStore: second).cookies(for: URL(string: "https://example.com/")!) {
            secondCookies = $0
            secondRead.fulfill()
        }
        wait(for: [firstRead, secondRead], timeout: 3)

        XCTAssertTrue(firstCookies.contains { $0.name == "ciao_session" && $0.value == "store-a" })
        XCTAssertFalse(secondCookies.contains { $0.name == "ciao_session" })
    }

    // MARK: Helpers

    private func chat(
        _ id: String,
        title: String? = nil,
        workspace: String = "work",
        unread: Bool = false,
        needsInput: Bool = false
    ) -> MenubarChat {
        MenubarChat(
            chatID: id,
            title: title ?? id,
            workspace: workspace,
            lastActivityAt: "2026-10-29T12:00:00Z",
            unread: unread,
            needsInput: needsInput
        )
    }

    private func snapshot(title: String, id: String, unread: Bool = true) -> ChatListSnapshot {
        ChatListSnapshot(
            origin: "http://127.0.0.1:8443",
            chats: [chat(id, title: title, unread: unread)],
            attentionCount: unread ? 1 : 0,
            fetchedAt: Date()
        )
    }

    /// The tray row is identified by its chat id, not by a title that now also
    /// carries a relative time.
    private func row(_ menu: NSMenu, id: String) -> NSMenuItem? {
        menu.items.first { $0.representedObject as? String == id }
    }

    /// The row title without its " · <relative time>" suffix.
    private func titleWithoutTime(_ row: NSMenuItem) -> String {
        row.title.components(separatedBy: " · ").first ?? row.title
    }

    private var fixedNow: Date {
        ISO8601DateFormatter().date(from: "2026-10-29T12:00:00Z")!
    }

    private func data(
        chats: [MenubarChat],
        attention: Int?,
        multipleWorkspaces: Bool? = nil
    ) -> Data {
        let rows = chats.map { chat -> [String: Any] in
            [
                "chat_id": chat.chatID,
                "title": chat.title,
                "workspace": chat.workspace,
                "last_activity_at": chat.lastActivityAt,
                "unread": chat.unread,
                "needs_input": chat.needsInput,
            ]
        }
        var object: [String: Any] = ["chats": rows]
        if let attention { object["attention_count"] = attention }
        if let multipleWorkspaces { object["multiple_workspaces"] = multipleWorkspaces }
        return try! JSONSerialization.data(withJSONObject: object)
    }

    private func makeCookie(
        name: String,
        value: String,
        domain: String,
        path: String = "/",
        secure: Bool = false,
        expires: Date? = nil
    ) -> HTTPCookie {
        var properties: [HTTPCookiePropertyKey: Any] = [
            .name: name,
            .value: value,
            .domain: domain,
            .path: path,
        ]
        if secure { properties[.secure] = "TRUE" }
        if let expires { properties[.expires] = expires }
        return HTTPCookie(properties: properties)!
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

private extension Result where Success == ChatListPayload, Failure == ChatListFailure {
    var failure: ChatListFailure? {
        if case .failure(let failure) = self { return failure }
        return nil
    }
}
