import XCTest
import Foundation
import Darwin
import WebKit
import Combine
import UserNotifications

@testable import CiaobotMac

/// Notification rows live in the Settings submenu, so this test searches the
/// tree instead of assuming a top-level row.
private extension NSMenu {
    func deepItem(withTitle title: String) -> NSMenuItem? {
        if let item = item(withTitle: title) { return item }
        for item in items {
            if let submenu = item.submenu,
               let found = submenu.deepItem(withTitle: title) {
                return found
            }
        }
        return nil
    }
}

// MARK: - Doubles

@MainActor
private final class FakeNotificationClient: NotificationPermissionClient {
    var isAvailable = true
    var onActivation: ((ChatNotificationTarget) -> Void)?
    var authorization: ChatNotificationAuthorization = .notDetermined
    var grantsPermission = true
    var requestError: Error?
    var holdRequest = false
    private var pendingRequest: ((ChatNotificationAuthorizationRequest) -> Void)?

    private(set) var prepareCount = 0
    private(set) var statusCount = 0
    private(set) var requestCount = 0
    private(set) var delivered: [ChatNotificationMessage] = []
    private(set) var removed: [String] = []

    func prepare() {
        prepareCount += 1
    }

    func authorizationStatus(completion: @escaping (ChatNotificationAuthorization) -> Void) {
        statusCount += 1
        completion(authorization)
    }

    func requestAuthorization(completion: @escaping (ChatNotificationAuthorizationRequest) -> Void) {
        requestCount += 1
        guard !holdRequest else {
            pendingRequest = completion
            return
        }
        completeRequest(completion)
    }

    func finishHeldRequest(granted: Bool) {
        guard let completion = pendingRequest else { return }
        pendingRequest = nil
        grantsPermission = granted
        completeRequest(completion)
    }

    private func completeRequest(_ completion: (ChatNotificationAuthorizationRequest) -> Void) {
        if let requestError {
            completion(.failed(requestError.localizedDescription))
            return
        }
        authorization = grantsPermission ? .authorized : .denied
        completion(grantsPermission ? .granted : .denied)
    }

    func deliver(_ message: ChatNotificationMessage) {
        delivered.append(message)
    }

    func removeDelivered(identifier: String) {
        removed.append(identifier)
    }

    func activate(_ target: ChatNotificationTarget) {
        onActivation?(target)
    }
}

private final class MemoryNotificationPreference: NotificationPreferenceStoring {
    var isEnabled: Bool

    init(_ isEnabled: Bool = false) {
        self.isEnabled = isEnabled
    }
}

private final class NotificationFakeCookieProvider: ChatCookieProviding {
    var cookies: [HTTPCookie] = []
    private(set) var requestedURLs: [URL] = []
    var retainsCompletion = false
    var completion: (([HTTPCookie]) -> Void)?

    func cookies(for url: URL, completion: @escaping ([HTTPCookie]) -> Void) {
        requestedURLs.append(url)
        if retainsCompletion { self.completion = completion } else { completion(cookies) }
    }

    func completeTwice() { completion?(cookies); completion?(cookies) }
    func dropCompletion() { completion = nil }
}

private final class NotificationFakeConnection: EventStreamConnection {
    private let onEvent: (ChatEvent) -> Void
    private let onClose: (EventStreamCloseReason) -> Void
    private(set) var cancelCount = 0

    init(onEvent: @escaping (ChatEvent) -> Void, onClose: @escaping (EventStreamCloseReason) -> Void) {
        self.onEvent = onEvent
        self.onClose = onClose
    }

    func emit(_ event: ChatEvent) {
        onEvent(event)
    }

    func close(_ reason: EventStreamCloseReason) {
        onClose(reason)
    }

    func cancel() {
        cancelCount += 1
    }
}

private final class NotificationFakeTransport: EventStreamTransport {
    struct Open {
        let origin: Origin
        let cookieHeader: String
        let connection: NotificationFakeConnection
    }

    private(set) var opens: [Open] = []
    var failOpen = false

    func open(
        origin: Origin,
        cookieHeader: String,
        onEvent: @escaping (ChatEvent) -> Void,
        onClose: @escaping (EventStreamCloseReason) -> Void
    ) -> EventStreamConnection? {
        guard !failOpen else { return nil }
        let connection = NotificationFakeConnection(onEvent: onEvent, onClose: onClose)
        opens.append(Open(origin: origin, cookieHeader: cookieHeader, connection: connection))
        return connection
    }
}

@MainActor
private final class NotificationFakeStatusItem: MenuBarStatusItem {
    var title: String?
    var image: NSImage?
    var toolTip: String?
    var menu: NSMenu?
    var isVisible = false

    func remove() {}
}

@MainActor
private final class NotificationFakeStatusItemFactory: StatusItemFactory {
    func makeStatusItem(length: CGFloat) -> MenuBarStatusItem {
        NotificationFakeStatusItem()
    }
}

@MainActor
private final class RecordingNotificationNavigationDelegate: NSObject, WKNavigationDelegate {
    private(set) var requestedURL: URL?
    private let policy: WKNavigationActionPolicy

    init(policy: WKNavigationActionPolicy = .cancel) {
        self.policy = policy
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        requestedURL = navigationAction.request.url
        decisionHandler(policy)
    }
}

/// A loopback HTTP fixture for the session-navigation integration test. It
/// records the paths WebKit actually requested, so the test can prove a real
/// windowless navigation happened instead of trusting a delegate callback
/// alone. Ephemeral port, no application server, no window.
private final class NotificationFixtureServer {
    let port: Int

    private let listener: Int32
    private let lock = NSLock()
    private let queue = DispatchQueue(label: "CiaobotMacTests.NotificationFixture", attributes: .concurrent)
    private var paths: [String] = []
    private var stopped = false
    private lazy var thread = Thread { [weak self] in self?.run() }

    init() throws {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw notificationFixtureError("socket") }

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
            throw notificationFixtureError("bind/listen")
        }

        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(fd, $0, &length)
            }
        }
        guard named == 0 else {
            close(fd)
            throw notificationFixtureError("getsockname")
        }

        self.listener = fd
        self.port = Int(UInt16(bigEndian: address.sin_port))
        thread.start()
    }

    deinit { shutdown() }

    var origin: String { "http://127.0.0.1:\(port)" }

    var observedPaths: [String] {
        lock.lock()
        defer { lock.unlock() }
        return paths
    }

    func requests(for path: String) -> Int {
        observedPaths.filter { $0 == path }.count
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
        let requestLine = request.components(separatedBy: "\r\n").first ?? ""
        let parts = requestLine.split(separator: " ", maxSplits: 2)
        guard parts.count >= 2 else { return }
        let target = String(parts[1])
        let path = target.split(separator: "?", maxSplits: 1).first.map(String.init) ?? target

        lock.lock()
        paths.append(path)
        lock.unlock()

        let body = "<!doctype html><title>fixture</title><h1 id=\"chat\">chat</h1>"
        let response = "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: \(body.utf8.count)\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n\(body)"
        write(client, response)
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

    private func write(_ client: Int32, _ response: String) {
        let bytes = Array(response.utf8)
        var sent = 0
        while sent < bytes.count {
            let count = bytes.withUnsafeBytes { buffer in
                send(client, buffer.baseAddress!.advanced(by: sent), bytes.count - sent, 0)
            }
            guard count > 0 else { return }
            sent += count
        }
    }
}

private func notificationFixtureError(_ operation: String) -> NSError {
    NSError(domain: "CiaobotMacTests.NotificationFixture", code: 1, userInfo: [NSLocalizedDescriptionKey: operation])
}

// MARK: - Tests

@MainActor
final class ChatNotificationsTests: XCTestCase {
    /// A loopback origin no application serves: the suite must never target the
    /// live Ciaobot server on 127.0.0.1:8443, and these tests either hand the URL
    /// to an injected loader or cancel it before WebKit issues a request.
    private let origin = Origin("http://127.0.0.1:45999")!
    private let remote = Origin("https://example.com")!

    private func settle() async {
        try? await Task.sleep(nanoseconds: 20_000_000)
    }

    /// Synchronous counterpart of `settle()`. A synchronous test method owns the
    /// main thread, so pumping the main run loop here runs queued main-queue work
    /// (such as `AppSessionStatus`'s deferred chat load). An async test body only
    /// lets the main queue drain while it is suspended, and whether it drains at
    /// all depends on how the runner waits, so a chat-navigation test uses the
    /// explicit pump instead of a `Task.sleep` deadline.
    private func settleSync() {
        RunLoop.main.run(until: Date().addingTimeInterval(0.02))
    }

    @discardableResult
    private func waitUntil(timeout: TimeInterval = 2, _ condition: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            RunLoop.main.run(until: Date().addingTimeInterval(0.01))
        }
        return condition()
    }

    private func connectedStatus(_ origin: Origin) -> AppSessionStatus {
        let status = AppSessionStatus()
        status.reportConnected(origin, dataStore: WKWebsiteDataStore.nonPersistent())
        return status
    }

    private func makeCookie(
        name: String = "ciao_session",
        value: String = "secret",
        domain: String = "127.0.0.1",
        path: String = "/",
        secure: Bool = false
    ) -> HTTPCookie {
        var properties: [HTTPCookiePropertyKey: Any] = [
            .name: name,
            .value: value,
            .domain: domain,
            .path: path,
        ]
        if secure { properties[.secure] = "TRUE" }
        return HTTPCookie(properties: properties)!
    }

    private func makeController(
        status: AppSessionStatus,
        client: FakeNotificationClient,
        preferences: MemoryNotificationPreference,
        provider: NotificationFakeCookieProvider,
        transport: NotificationFakeTransport,
        present: @escaping () -> Void = {},
        openChat: @escaping (String) -> Bool = { _ in true },
        tracking: HostStoreTrackingBundle? = nil
    ) -> ChatNotificationController {
        ChatNotificationController(
            status: status,
            permission: client,
            preferences: preferences,
            transport: transport,
            cookieProviderFactory: { _ in provider },
            presentWindow: present,
            openChat: openChat,
            now: { Date(timeIntervalSince1970: 1_000_000) },
            hostStoreTracking: tracking
        )
    }

    func testNotificationCookieReadTracksUntilFirstCompletionAfterDisconnect() async throws {
        let status = AppSessionStatus()
        let store = WKWebsiteDataStore.nonPersistent()
        status.reportConnected(origin, dataStore: store)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let provider = NotificationFakeCookieProvider()
        provider.retainsCompletion = true
        let transport = NotificationFakeTransport()
        let registry = HostStoreUseRegistry()
        let source = MutableHostStoreSource(.init(origin: origin, store: store, identity: .ephemeral(UUID())))
        var controller: ChatNotificationController? = makeController(status: status, client: client, preferences: .init(true), provider: provider, transport: transport, tracking: .init(source: source, registry: registry))
        controller?.start()
        await settle()
        XCTAssertEqual(provider.requestedURLs.count, 1)
        status.reportDisconnected()
        await settle()
        controller?.stop()
        controller = nil
        XCTAssertFalse(registry.allReleased(for: origin))
        provider.completeTwice()
        await settle()
        XCTAssertTrue(registry.allReleased(for: origin))
        XCTAssertTrue(transport.opens.isEmpty)
    }

    func testNotificationCookieReadRejectsMismatchedSnapshotAndRetries() async throws {
        let status = AppSessionStatus()
        let store = WKWebsiteDataStore.nonPersistent()
        status.reportConnected(origin, dataStore: store)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let registry = HostStoreUseRegistry()
        let source = MutableHostStoreSource(.init(origin: origin, store: .nonPersistent(), identity: .ephemeral(UUID())))
        let controller = makeController(status: status, client: client, preferences: .init(true), provider: provider, transport: transport, tracking: .init(source: source, registry: registry))
        controller.start()
        await settle()
        XCTAssertEqual(controller.streamStatus, .retrying)
        XCTAssertTrue(provider.requestedURLs.isEmpty)
        XCTAssertTrue(transport.opens.isEmpty)
        source.value = .init(origin: origin, store: store, identity: .ephemeral(UUID()))
        status.reportDisconnected()
        await settle()
        status.reportConnected(origin, dataStore: store)
        await settle()
        XCTAssertEqual(transport.opens.count, 1)
        controller.stop()
    }

    func testNotificationDisableAndDroppedCookieCallbackRemainTracked() async throws {
        let status = AppSessionStatus()
        let store = WKWebsiteDataStore.nonPersistent()
        status.reportConnected(origin, dataStore: store)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let provider = NotificationFakeCookieProvider()
        provider.retainsCompletion = true
        let transport = NotificationFakeTransport()
        let registry = HostStoreUseRegistry()
        let source = MutableHostStoreSource(.init(origin: origin, store: store, identity: .ephemeral(UUID())))
        let controller = makeController(status: status, client: client, preferences: .init(true), provider: provider, transport: transport, tracking: .init(source: source, registry: registry))
        controller.start()
        await settle()
        XCTAssertEqual(provider.requestedURLs.count, 1)
        controller.performMenuAction()
        await settle()
        XCTAssertEqual(controller.state, .off)
        provider.dropCompletion()
        controller.stop()
        XCTAssertFalse(registry.allReleased(for: origin))
        XCTAssertTrue(transport.opens.isEmpty)
    }

    func testNotificationCookieReadIgnoresDuplicateCompletion() async throws {
        let status = AppSessionStatus()
        let store = WKWebsiteDataStore.nonPersistent()
        status.reportConnected(origin, dataStore: store)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let provider = NotificationFakeCookieProvider()
        provider.retainsCompletion = true
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let registry = HostStoreUseRegistry()
        let source = MutableHostStoreSource(.init(origin: origin, store: store, identity: .ephemeral(UUID())))
        let controller = makeController(status: status, client: client, preferences: .init(true), provider: provider, transport: transport, tracking: .init(source: source, registry: registry))
        controller.start()
        await settle()
        XCTAssertEqual(provider.requestedURLs.count, 1)
        provider.completeTwice()
        await settle()
        XCTAssertTrue(registry.allReleased(for: origin))
        XCTAssertEqual(transport.opens.count, 1)
        controller.stop()
    }

    // MARK: Opt-in and permission

    func testStartDoesNotPromptAndTheMenuClickIsTheOnlyPrompt() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        let preferences = MemoryNotificationPreference(false)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()

        XCTAssertEqual(controller.state, .off)
        XCTAssertEqual(client.prepareCount, 1, "The delegate is installed without prompting.")
        XCTAssertEqual(client.requestCount, 0, "Start must never request notification permission.")
        XCTAssertTrue(transport.opens.isEmpty, "No socket before an explicit opt-in.")
        XCTAssertFalse(preferences.isEnabled)

        controller.performMenuAction()
        await settle()

        XCTAssertEqual(client.requestCount, 1)
        XCTAssertEqual(controller.state, .on)
        XCTAssertTrue(preferences.isEnabled)
        XCTAssertEqual(transport.opens.count, 1)
        controller.stop()
    }

    func testASecondClickWhileThePermissionPromptIsPendingDoesNotPromptAgain() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.holdRequest = true
        let preferences = MemoryNotificationPreference(false)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        controller.performMenuAction()
        controller.performMenuAction()
        await settle()

        XCTAssertEqual(client.requestCount, 1, "Only the first explicit click may open the OS prompt.")
        XCTAssertEqual(controller.state, .off, "The state stays off until macOS answers.")
        XCTAssertTrue(transport.opens.isEmpty)

        client.finishHeldRequest(granted: true)
        await settle()
        XCTAssertEqual(controller.state, .on)
        XCTAssertTrue(preferences.isEnabled)
        controller.stop()
    }

    func testStartShowsBlockedWhenTheOSAlreadyDeniedPermission() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .denied
        let preferences = MemoryNotificationPreference(false)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()

        XCTAssertEqual(controller.state, .denied)
        XCTAssertEqual(client.requestCount, 0, "Reading the OS state must not prompt.")
        XCTAssertTrue(transport.opens.isEmpty)
        controller.stop()
    }

    func testDeliveryOffIsReportedAsBlockedAndNeverConnectsOrClaimsOn() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .deliveryOff
        let preferences = MemoryNotificationPreference(false)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()

        XCTAssertEqual(controller.state, .deliveryOff)
        XCTAssertEqual(client.requestCount, 0, "Reading the OS state must not prompt.")
        XCTAssertTrue(transport.opens.isEmpty, "A blocked snapshot is not delivering notifications.")
        XCTAssertTrue(client.delivered.isEmpty)
        controller.stop()
    }

    func testRememberedOptInWithDeliveryOffDoesNotStayOn() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .deliveryOff
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()

        XCTAssertEqual(controller.state, .deliveryOff)
        XCTAssertFalse(preferences.isEnabled, "An allowed-but-blocked snapshot is not an opt-in that still delivers.")
        XCTAssertEqual(client.requestCount, 0)
        XCTAssertTrue(transport.opens.isEmpty)
        controller.stop()
    }

    func testDeliveryOffNeedsANewOptInOnceTheOSChannelsAreEnabled() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .deliveryOff
        let preferences = MemoryNotificationPreference(false)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        XCTAssertEqual(controller.state, .deliveryOff)

        client.authorization = .authorized
        controller.refreshIfNeeded()
        await settle()

        XCTAssertEqual(controller.state, .off, "Fixing the OS channels does not silently opt in.")
        XCTAssertEqual(client.requestCount, 0)
        XCTAssertTrue(transport.opens.isEmpty)
        controller.stop()
    }

    func testDeniedPermissionLeavesNotificationsOffAndDoesNotRememberOptIn() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.grantsPermission = false
        let preferences = MemoryNotificationPreference(false)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        controller.performMenuAction()
        await settle()

        XCTAssertEqual(controller.state, .denied)
        XCTAssertFalse(preferences.isEnabled)
        XCTAssertTrue(transport.opens.isEmpty)
        XCTAssertTrue(client.delivered.isEmpty)
        controller.stop()
    }

    func testAuthorizationRequestErrorIsPreservedAndNeverReportedAsDenied() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.requestError = NSError(
            domain: "CiaobotTests",
            code: 1,
            userInfo: [NSLocalizedDescriptionKey: "Notifications are not available for this application"]
        )
        let preferences = MemoryNotificationPreference(false)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        controller.performMenuAction()
        await settle()

        XCTAssertEqual(
            controller.state,
            .unavailable(.permissionRequestFailed("Notifications are not available for this application")),
            "A technical request failure is not a macOS denial."
        )
        XCTAssertFalse(preferences.isEnabled)
        XCTAssertTrue(transport.opens.isEmpty)
        XCTAssertTrue(client.delivered.isEmpty)
        controller.stop()
    }

    func testRegistrationFailureSurvivesAMenuRefreshWithoutASecondPrompt() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.requestError = NSError(
            domain: "CiaobotTests",
            code: 2,
            userInfo: [NSLocalizedDescriptionKey: "Notifications are not available for this application"]
        )
        let preferences = MemoryNotificationPreference(false)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        controller.performMenuAction()
        await settle()
        let failure = ChatNotificationState.unavailable(
            .permissionRequestFailed("Notifications are not available for this application")
        )
        XCTAssertEqual(controller.state, failure)

        controller.refreshIfNeeded()
        await settle()

        XCTAssertEqual(controller.state, failure, "A menu refresh must not erase the technical error.")
        XCTAssertEqual(client.requestCount, 1, "Refreshing the menu never asks macOS again.")
        XCTAssertFalse(preferences.isEnabled)
        XCTAssertTrue(transport.opens.isEmpty)
        controller.stop()
    }

    func testUnknownAuthorizationStatusIsUnavailableAndNotBlocked() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .unknown("A future macOS authorization state")
        let preferences = MemoryNotificationPreference(false)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()

        XCTAssertEqual(
            controller.state,
            .unavailable(.unknownAuthorization("A future macOS authorization state")),
            "An unrecognized status must not be shown as 'blocked by macOS'."
        )
        XCTAssertEqual(client.requestCount, 0)
        XCTAssertTrue(transport.opens.isEmpty)
        controller.stop()
    }

    func testRememberedOptInUsesExistingPermissionWithoutPrompting() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()

        XCTAssertEqual(client.requestCount, 0)
        XCTAssertEqual(controller.state, .on)
        XCTAssertEqual(transport.opens.count, 1)
        controller.stop()
    }

    func testRememberedOptInWithoutPermissionNeedsANewClick() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .notDetermined
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()

        XCTAssertEqual(controller.state, .off)
        XCTAssertFalse(preferences.isEnabled, "A stored flag without permission is not consent for this launch.")
        XCTAssertEqual(client.requestCount, 0)
        XCTAssertTrue(transport.opens.isEmpty)
        controller.stop()
    }

    func testUnavailableClientNeverPromptsOrConnects() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.isAvailable = false
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()

        XCTAssertEqual(controller.state, .unavailable(.unbundled))
        XCTAssertEqual(client.requestCount, 0)
        XCTAssertEqual(client.statusCount, 0)
        XCTAssertTrue(transport.opens.isEmpty)
        controller.stop()
    }

    func testUnbundledProcessReportsUnavailableWithoutTouchingUserNotifications() {
        let client = SystemNotificationPermissionClient(bundle: Bundle(for: ChatNotificationsTests.self))
        XCTAssertFalse(client.isAvailable, "The test host is not a .app bundle.")
        client.prepare()

        var status: ChatNotificationAuthorization?
        client.authorizationStatus { status = $0 }
        XCTAssertEqual(status, .unavailable)

        var request: ChatNotificationAuthorizationRequest?
        client.requestAuthorization { request = $0 }
        XCTAssertEqual(request, .unavailable, "An unbundled process must fail closed without prompting.")
    }

    func testOnlyTheDefaultNotificationClickOpensAChat() {
        let userInfo: [AnyHashable: Any] = [
            "chat_id": "chat-1",
            "origin": origin.canonicalKey,
        ]
        XCTAssertEqual(
            NotificationResponsePolicy.target(
                actionIdentifier: UNNotificationDefaultActionIdentifier,
                userInfo: userInfo
            ),
            ChatNotificationTarget(chatID: "chat-1", origin: origin.canonicalKey)
        )
        XCTAssertNil(
            NotificationResponsePolicy.target(
                actionIdentifier: UNNotificationDismissActionIdentifier,
                userInfo: userInfo
            ),
            "Dismiss must never navigate."
        )
        XCTAssertNil(
            NotificationResponsePolicy.target(
                actionIdentifier: "CUSTOM_ACTION",
                userInfo: userInfo
            ),
            "A custom action must never navigate."
        )
        XCTAssertNil(
            NotificationResponsePolicy.target(
                actionIdentifier: UNNotificationDefaultActionIdentifier,
                userInfo: ["chat_id": "chat-1"]
            )
        )
    }

    // MARK: Socket authentication and lifecycle

    func testStreamStatusDescribesWaitingSignInConnectingAndRetry() async {
        let status = AppSessionStatus()
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        XCTAssertEqual(controller.state, .on)
        XCTAssertEqual(controller.streamStatus, .notConnected)

        status.reportConnected(origin, dataStore: WKWebsiteDataStore.nonPersistent())
        await settle()
        XCTAssertEqual(controller.streamStatus, .waitingForAuthentication)

        provider.cookies = [makeCookie()]
        controller.refreshIfNeeded()
        await settle()
        XCTAssertEqual(transport.opens.count, 1)
        XCTAssertEqual(controller.streamStatus, .connecting)

        transport.opens[0].connection.emit(.snapshot(activeChatIDs: []))
        XCTAssertEqual(controller.streamStatus, .ready)

        transport.opens[0].connection.close(.failed)
        await settle()
        XCTAssertEqual(controller.streamStatus, .retrying)

        status.reportDisconnected()
        await settle()
        XCTAssertEqual(controller.streamStatus, .notConnected)
        controller.stop()
    }

    func testSocketCloseReasonsMapToTruthfulMenuStates() async {
        let cases: [(EventStreamCloseReason, ChatNotificationStreamStatus)] = [
            (.unauthorized, .waitingForAuthentication),
            (.forbiddenOrigin, .originRejected),
            (.redirected, .redirected),
            (.authenticationRefused, .authenticationRefused),
            (.invalidMessage, .protocolError),
            (.ended, .retrying),
            (.failed, .retrying),
        ]

        for (reason, expected) in cases {
            let status = connectedStatus(origin)
            let client = FakeNotificationClient()
            client.authorization = .authorized
            let preferences = MemoryNotificationPreference(true)
            let provider = NotificationFakeCookieProvider()
            provider.cookies = [makeCookie()]
            let transport = NotificationFakeTransport()
            let controller = makeController(
                status: status,
                client: client,
                preferences: preferences,
                provider: provider,
                transport: transport
            )

            controller.start()
            await settle()
            XCTAssertEqual(transport.opens.count, 1, "Expected one socket before \(reason)")
            transport.opens[0].connection.close(reason)
            await settle()

            XCTAssertEqual(controller.streamStatus, expected, "Close reason \(reason) must be reported truthfully")
            XCTAssertEqual(
                transport.opens.count,
                1,
                "A terminal \(reason) must not be retried behind the user's back."
            )
            controller.stop()
        }
    }

    func testUnauthorizedCloseKeepsTheExplicitOptOutAndSignInRecovery() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        transport.opens[0].connection.close(.unauthorized)
        await settle()

        XCTAssertEqual(controller.streamStatus, .waitingForAuthentication)
        XCTAssertEqual(controller.state, .on)

        // The stream failure must not remove the user's explicit opt-out.
        controller.performMenuAction()
        await settle()
        XCTAssertEqual(controller.state, .off)
        XCTAssertFalse(preferences.isEnabled)
        controller.stop()
    }

    func testForbiddenAndRedirectedClosuresDoNotAutoRetry() async {
        for reason in [EventStreamCloseReason.forbiddenOrigin, .redirected] {
            let status = connectedStatus(origin)
            let client = FakeNotificationClient()
            client.authorization = .authorized
            let preferences = MemoryNotificationPreference(true)
            let provider = NotificationFakeCookieProvider()
            provider.cookies = [makeCookie()]
            let transport = NotificationFakeTransport()
            let controller = makeController(
                status: status,
                client: client,
                preferences: preferences,
                provider: provider,
                transport: transport
            )

            controller.start()
            await settle()
            transport.opens[0].connection.close(reason)
            await settle()
            XCTAssertEqual(transport.opens.count, 1, "\(reason) must not be silently retried.")

            // Opening the menu may retry once, but only the same selected origin.
            controller.refreshIfNeeded()
            await settle()
            XCTAssertEqual(transport.opens.count, 2)
            XCTAssertEqual(transport.opens[1].origin.canonicalKey, origin.canonicalKey)
            controller.stop()
        }
    }

    func testEnabledNotificationsWaitForAConnection() async {
        let status = AppSessionStatus()
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        XCTAssertEqual(controller.state, .on)
        XCTAssertTrue(transport.opens.isEmpty, "No socket before the app has a selected origin.")

        status.reportConnected(origin, dataStore: WKWebsiteDataStore.nonPersistent())
        await settle()
        XCTAssertEqual(transport.opens.count, 1)
        let connection = transport.opens[0].connection

        status.reportDisconnected()
        await settle()
        XCTAssertEqual(connection.cancelCount, 1)
        controller.stop()
    }

    func testNoSocketIsAttemptedWithoutAPerConnectionSessionCookie() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()

        XCTAssertEqual(controller.state, .on)
        XCTAssertTrue(transport.opens.isEmpty)
        XCTAssertEqual(provider.requestedURLs.first?.absoluteString, "ws://127.0.0.1:45999/ws/events")

        provider.cookies = [makeCookie()]
        controller.refreshIfNeeded()
        await settle()

        XCTAssertEqual(transport.opens.count, 1, "A later sign-in recovers without a new opt-in.")
        XCTAssertEqual(transport.opens[0].origin.canonicalKey, origin.canonicalKey)
        XCTAssertEqual(transport.opens[0].cookieHeader, "ciao_session=secret")
        controller.stop()
    }

    func testSocketCarriesOnlyTheExactSessionCookie() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [
            makeCookie(),
            makeCookie(name: "other", value: "not-a-credential"),
        ]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()

        let url = EventStreamEndpoint.url(origin: origin)!
        XCTAssertEqual(
            EventStreamEndpoint.cookieHeader(for: url, cookies: provider.cookies, now: Date(timeIntervalSince1970: 1_000_000)),
            "ciao_session=secret"
        )
        XCTAssertEqual(transport.opens.count, 1)
        XCTAssertEqual(transport.opens[0].cookieHeader, "ciao_session=secret")
        XCTAssertEqual(provider.requestedURLs, [EventStreamEndpoint.url(origin: origin)!])
        controller.stop()
    }

    func testDisconnectCancelsTheSocketAndDropsLateEvents() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        let connection = transport.opens[0].connection
        connection.emit(.snapshot(activeChatIDs: []))

        status.reportDisconnected()
        await settle()

        XCTAssertEqual(connection.cancelCount, 1)
        connection.emit(.resultReady(chatID: "chat-1", title: "Old", snippet: "Old answer", workspace: ""))
        XCTAssertTrue(client.delivered.isEmpty, "A stale connection cannot notify after disconnect.")
        controller.stop()
    }

    func testTurningNotificationsOffCancelsAndForgetsThePreference() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        let connection = transport.opens[0].connection

        controller.performMenuAction()
        await settle()

        XCTAssertEqual(controller.state, .off)
        XCTAssertFalse(preferences.isEnabled)
        XCTAssertEqual(connection.cancelCount, 1)
        connection.emit(.snapshot(activeChatIDs: []))
        connection.emit(.resultReady(chatID: "chat-1", title: "Late", snippet: "Late", workspace: ""))
        XCTAssertTrue(client.delivered.isEmpty)
        controller.stop()
    }

    // MARK: Baseline, backlog and deduplication

    func testSnapshotIsABaselineAndEventsBeforeItNeverNotify() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        let connection = transport.opens[0].connection

        connection.emit(.resultReady(chatID: "chat-1", title: "Backlog", snippet: "Never announce this", workspace: ""))
        XCTAssertTrue(client.delivered.isEmpty, "No result before the snapshot is a live result.")

        connection.emit(.snapshot(activeChatIDs: ["chat-1"]))
        XCTAssertTrue(client.delivered.isEmpty, "The snapshot itself is never a banner.")

        connection.emit(.resultReady(chatID: "chat-1", title: "Fresh", snippet: "A real answer", workspace: ""))
        XCTAssertEqual(client.delivered.count, 1)
        XCTAssertEqual(client.delivered[0].title, "Fresh")
        XCTAssertEqual(
            client.delivered[0].subtitle,
            "",
            "No workspace from the server means no subtitle line in the banner."
        )
        controller.stop()
    }

    func testTheSameResultIsAnnouncedOnceAndANewTurnCanRepeatIt() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        let connection = transport.opens[0].connection
        connection.emit(.snapshot(activeChatIDs: []))

        let result = ChatEvent.resultReady(chatID: "chat-1", title: "Same", snippet: "Same", workspace: "")
        connection.emit(result)
        connection.emit(result)
        XCTAssertEqual(client.delivered.count, 1, "A duplicated event is not a second completion.")

        connection.emit(.streamingStarted(chatID: "chat-1"))
        connection.emit(result)
        XCTAssertEqual(client.delivered.count, 2, "A new turn may legitimately repeat the same text.")
        controller.stop()
    }

    func testReadingAChatWithdrawsItsBannerAndAReplayDoesNotRestoreIt() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        let connection = transport.opens[0].connection
        connection.emit(.snapshot(activeChatIDs: []))

        let result = ChatEvent.resultReady(chatID: "chat-1", title: "Answer", snippet: "Done", workspace: "")
        connection.emit(result)
        XCTAssertEqual(client.delivered.count, 1)

        connection.emit(.chatRead(chatID: "chat-1"))
        XCTAssertEqual(client.removed, [client.delivered[0].identifier])

        connection.emit(result)
        XCTAssertEqual(client.delivered.count, 1, "A replayed result for a read chat is not a new banner.")

        connection.emit(.chatRead(chatID: "../bad"))
        XCTAssertEqual(client.removed.count, 1, "An invalid chat id withdraws nothing.")
        controller.stop()
    }

    func testAReadBeforeTheSnapshotStillWithdrawsTheBanner() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        transport.opens[0].connection.emit(.chatRead(chatID: "chat-1"))
        XCTAssertEqual(
            client.removed,
            [ChatNotificationController.notificationIdentifier(origin: origin.canonicalKey, chatID: "chat-1")],
            "Withdrawing announces nothing, so it does not wait for the baseline."
        )
        XCTAssertTrue(client.delivered.isEmpty)
        controller.stop()
    }

    func testMemoryPassLikeTitleIsNotHeuristicallySuppressed() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        let connection = transport.opens[0].connection
        connection.emit(.snapshot(activeChatIDs: []))
        // The event has no helper kind. A title-prefix heuristic would hide a
        // legitimate chat with this title, so the client does not guess.
        connection.emit(.resultReady(
            chatID: "chat-1",
            title: "Memory pass · Archived discussion",
            snippet: "Filed two notes",
            workspace: ""
        ))
        XCTAssertEqual(client.delivered.count, 1)
        controller.stop()
    }

    func testReconnectStartsANewBaselineAndOldGenerationCannotNotify() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        let first = transport.opens[0].connection
        first.emit(.snapshot(activeChatIDs: []))
        first.emit(.resultReady(chatID: "chat-1", title: "First", snippet: "Done", workspace: ""))
        XCTAssertEqual(client.delivered.count, 1)

        status.reportDisconnected()
        await settle()
        status.reportConnected(origin, dataStore: WKWebsiteDataStore.nonPersistent())
        await settle()

        XCTAssertEqual(transport.opens.count, 2)
        let second = transport.opens[1].connection

        // A late event from the previous socket must not notify.
        first.emit(.resultReady(chatID: "chat-2", title: "Stale", snippet: "Stale", workspace: ""))
        XCTAssertEqual(client.delivered.count, 1)

        // A reconnect's snapshot is a baseline: active streams are not backlog
        // banners, and a completion after it is fresh.
        second.emit(.snapshot(activeChatIDs: ["chat-2"]))
        XCTAssertEqual(client.delivered.count, 1)
        second.emit(.resultReady(chatID: "chat-2", title: "Fresh", snippet: "Done", workspace: ""))
        XCTAssertEqual(client.delivered.count, 2)
        controller.stop()
    }

    func testIdenticalResultAfterReconnectIsStillSuppressed() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        let result = ChatEvent.resultReady(chatID: "chat-1", title: "Same", snippet: "Same", workspace: "")
        transport.opens[0].connection.emit(.snapshot(activeChatIDs: []))
        transport.opens[0].connection.emit(result)
        XCTAssertEqual(client.delivered.count, 1)

        status.reportDisconnected()
        await settle()
        status.reportConnected(origin, dataStore: WKWebsiteDataStore.nonPersistent())
        await settle()
        XCTAssertEqual(transport.opens.count, 2)
        let second = transport.opens[1].connection
        second.emit(.snapshot(activeChatIDs: []))
        second.emit(result)
        XCTAssertEqual(client.delivered.count, 1, "A duplicate replayed after reconnect is not a new completion.")

        second.emit(.streamingStarted(chatID: "chat-1"))
        second.emit(result)
        XCTAssertEqual(client.delivered.count, 2, "A new turn clears the remembered result.")
        controller.stop()
    }

    func testPermissionResetStopsTheSocketAndRequiresANewClick() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        let connection = transport.opens[0].connection
        XCTAssertEqual(controller.state, .on)

        client.authorization = .notDetermined
        controller.refreshIfNeeded()
        await settle()

        XCTAssertEqual(controller.state, .off)
        XCTAssertFalse(preferences.isEnabled)
        XCTAssertEqual(connection.cancelCount, 1)
        XCTAssertEqual(transport.opens.count, 1, "A reset permission must not start another socket.")
        controller.stop()
    }

    func testClosedSocketCanBeRetriedFromTheMenu() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        XCTAssertEqual(controller.state, .on)
        XCTAssertTrue(status.isConnected)
        XCTAssertNotNil(status.dataStore)
        XCTAssertEqual(provider.requestedURLs.count, 1)
        XCTAssertEqual(transport.opens.count, 1, "Expected an authenticated socket for the retry test.")
        guard let firstConnection = transport.opens.first?.connection else {
            XCTFail("No socket was opened")
            return
        }
        firstConnection.close(.failed)
        await settle()
        XCTAssertEqual(transport.opens.count, 1)

        controller.refreshIfNeeded()
        await settle()
        XCTAssertEqual(transport.opens.count, 2)
        controller.stop()
    }

    /// The session's connection publisher replays its current value to a new
    /// subscriber, and can report the same connection again. Neither is a new
    /// connection, so neither may reset the live stream and open a second one.
    func testARepeatedConnectionReportDoesNotOpenASecondSocket() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        XCTAssertEqual(controller.state, .on)
        XCTAssertEqual(provider.requestedURLs.count, 1)
        XCTAssertEqual(transport.opens.count, 1)
        guard let connection = transport.opens.first?.connection else {
            XCTFail("No socket was opened")
            return
        }

        status.reportConnected(origin, dataStore: status.dataStore)
        await settle()

        XCTAssertEqual(
            provider.requestedURLs.count,
            1,
            "One connection asks for cookies once."
        )
        XCTAssertEqual(
            transport.opens.count,
            1,
            "A repeated connection report is not a new connection."
        )
        XCTAssertEqual(
            connection.cancelCount,
            0,
            "A repeated connection report must not tear down the live socket."
        )
        controller.stop()
    }

    // MARK: Click handling

    func testNotificationClickOpensOnlyTheSameOriginAndASafeChatID() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        var presented = 0
        var opened: [String] = []
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport,
            present: { presented += 1 },
            openChat: { opened.append($0); return true }
        )

        controller.start()
        await settle()
        let connection = transport.opens[0].connection
        connection.emit(.snapshot(activeChatIDs: []))
        connection.emit(.resultReady(chatID: "chat-1", title: "Answer", snippet: "Done", workspace: ""))

        let target = try! XCTUnwrap(client.delivered.first?.target)
        XCTAssertEqual(target.origin, origin.canonicalKey)

        client.activate(target)
        XCTAssertEqual(presented, 1)
        XCTAssertEqual(opened, ["chat-1"])

        client.activate(ChatNotificationTarget(chatID: "chat-1", origin: remote.canonicalKey))
        XCTAssertEqual(presented, 2, "A valid click presents the window even when it needs to wait.")
        XCTAssertEqual(opened, ["chat-1"], "A notification from another origin must not navigate.")

        client.activate(ChatNotificationTarget(chatID: "../escape", origin: origin.canonicalKey))
        XCTAssertEqual(presented, 2, "An invalid target is refused before it can present or wait.")
        XCTAssertEqual(opened, ["chat-1"])

        status.reportDisconnected()
        await settle()
        client.activate(target)
        XCTAssertEqual(presented, 3)
        XCTAssertEqual(opened, ["chat-1"], "A click never signs in or reconnects.")
        XCTAssertFalse(status.isConnected)

        // A different origin must not consume the retained target.
        status.reportConnected(remote, dataStore: WKWebsiteDataStore.nonPersistent())
        await settle()
        XCTAssertEqual(opened, ["chat-1"])

        // The next manual connection to the exact origin opens the retained chat.
        status.reportConnected(origin, dataStore: WKWebsiteDataStore.nonPersistent())
        await settle()
        XCTAssertEqual(opened, ["chat-1", "chat-1"])
        controller.stop()
    }

    func testSessionQueuesAChosenChatAfterAnOriginChange() throws {
        // Real windowless WebKit integration: both sides are loopback fixture
        // servers, so the assertions can see the requests WebKit actually sends.
        // A queued chat must reach the exact origin's server and nothing may
        // reach the origin the session was connected to before.
        let server = try NotificationFixtureServer()
        let wrongServer = try NotificationFixtureServer()
        defer {
            server.shutdown()
            wrongServer.shutdown()
        }
        let exactOrigin = Origin(server.origin)!
        let wrongOrigin = Origin(wrongServer.origin)!

        let status = AppSessionStatus()
        status.reportConnected(wrongOrigin)
        let wrongConfiguration = WKWebViewConfiguration()
        wrongConfiguration.websiteDataStore = .nonPersistent()
        let wrong = WKWebView(frame: .zero, configuration: wrongConfiguration)
        // Allow, so a mistaken navigation would really reach the wrong server
        // and be caught by the request assertions below.
        let wrongDelegate = RecordingNotificationNavigationDelegate(policy: .allow)
        wrong.navigationDelegate = wrongDelegate
        status.attachWebView(wrong, for: wrongOrigin)

        status.reportConnected(exactOrigin)
        XCTAssertTrue(status.openChat(chatID: "chat-1"))
        XCTAssertTrue(server.observedPaths.isEmpty, "A queued chat must not load before a web view attaches.")

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let webView = WKWebView(frame: .zero, configuration: configuration)
        let delegate = RecordingNotificationNavigationDelegate(policy: .allow)
        webView.navigationDelegate = delegate
        status.attachWebView(webView, for: exactOrigin)

        // Wait for the positive event itself: the fixture server receiving the
        // queued path. The first real WebKit navigation in a cold test process
        // can take seconds, so a delegate-only deadline is not a stable signal.
        XCTAssertTrue(
            waitUntil(timeout: 20) { server.requests(for: "/chat/chat-1") >= 1 },
            "The exact-origin view never requested the queued chat; seen \(server.observedPaths)"
        )
        XCTAssertEqual(server.observedPaths.first, "/chat/chat-1",
                       "The queued chat must be the first request, not the origin root.")
        XCTAssertEqual(delegate.requestedURL?.absoluteString, "\(server.origin)/chat/chat-1")
        XCTAssertEqual(delegate.requestedURL?.scheme, "http")
        XCTAssertEqual(delegate.requestedURL?.host, "127.0.0.1")
        XCTAssertEqual(delegate.requestedURL?.port, server.port)
        XCTAssertEqual(delegate.requestedURL?.path, "/chat/chat-1")
        XCTAssertNil(wrongDelegate.requestedURL, "A different origin's view must never be navigated.")
        XCTAssertTrue(wrongServer.observedPaths.isEmpty,
                      "The previous origin's server must receive nothing; seen \(wrongServer.observedPaths)")
    }

    func testColdLaunchClickWaitsForExactOriginAndWebViewAttach() throws {
        let status = AppSessionStatus()
        // Observe the single production hand-off to `WKWebView.load` instead of
        // waiting for a WebKit navigation delegate callback. A view that is not
        // in a window does not deliver that callback in every test environment,
        // so the delegate is not a trustworthy signal for a headless test. The
        // real delegate path stays covered by
        // `testSessionQueuesAChosenChatAfterAnOriginChange` and by
        // WebKitDelegateTests.
        var loads: [(webView: WKWebView, url: URL)] = []
        status.loadChatURL = { webView, url in loads.append((webView, url)) }
        let client = FakeNotificationClient()
        let preferences = MemoryNotificationPreference(false)
        let provider = NotificationFakeCookieProvider()
        let transport = NotificationFakeTransport()
        var presented = 0
        var opened: [String] = []
        var openResults: [Bool] = []
        let controller = ChatNotificationController(
            status: status,
            permission: client,
            preferences: preferences,
            transport: transport,
            cookieProviderFactory: { _ in provider },
            presentWindow: { presented += 1 },
            openChat: { chatID in
                let accepted = status.openChat(chatID: chatID)
                opened.append(chatID)
                openResults.append(accepted)
                return accepted
            },
            now: { Date(timeIntervalSince1970: 1_000_000) }
        )
        controller.start()
        settleSync()

        let target = ChatNotificationTarget(chatID: "chat-1", origin: origin.canonicalKey)
        client.activate(target)
        XCTAssertEqual(presented, 1, "A cold-launch click presents the window immediately.")
        XCTAssertFalse(status.isConnected)
        XCTAssertTrue(transport.opens.isEmpty, "A cold-launch click must not autoconnect or authenticate.")
        XCTAssertTrue(loads.isEmpty, "A cold-launch click must not load anything before a session exists.")

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let wrongWebView = WKWebView(frame: NSRect(x: 0, y: 0, width: 200, height: 200), configuration: configuration)
        let wrongNavigation = RecordingNotificationNavigationDelegate()
        wrongWebView.navigationDelegate = wrongNavigation

        // A different origin is not the retained target and must not navigate.
        status.reportConnected(remote, dataStore: WKWebsiteDataStore.nonPersistent())
        settleSync()
        status.attachWebView(wrongWebView, for: remote)
        XCTAssertFalse(
            waitUntil(timeout: 0.3) { wrongNavigation.requestedURL != nil },
            "A different origin must not open the retained chat."
        )
        XCTAssertTrue(loads.isEmpty, "A different origin must not receive the retained chat load.")

        // Connecting the exact origin only queues the navigation until the view attaches.
        status.reportConnected(origin, dataStore: WKWebsiteDataStore.nonPersistent())
        XCTAssertTrue(
            waitUntil(timeout: 2) { opened == ["chat-1"] },
            "The retained target is applied only on the exact origin; opened \(opened)"
        )
        XCTAssertEqual(opened, ["chat-1"], "The retained target is applied only on the exact origin.")
        XCTAssertEqual(openResults, [true])
        XCTAssertNil(wrongNavigation.requestedURL, "The previous origin's view must never be navigated.")
        XCTAssertTrue(loads.isEmpty, "A chat must wait for the matching web view instead of loading early.")

        let exactConfiguration = WKWebViewConfiguration()
        exactConfiguration.websiteDataStore = .nonPersistent()
        let webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 200, height: 200), configuration: exactConfiguration)
        let navigation = RecordingNotificationNavigationDelegate()
        webView.navigationDelegate = navigation
        XCTAssertNil(navigation.requestedURL, "The chat waits for the matching web view to attach.")

        status.attachWebView(webView, for: origin)
        XCTAssertEqual(status.currentOrigin?.canonicalKey, origin.canonicalKey)
        // The pending load is deferred one main-queue turn so the chat path is
        // the last navigation; attach itself must not load synchronously.
        XCTAssertTrue(loads.isEmpty, "The pending chat load must not run inside attachWebView.")
        XCTAssertTrue(
            waitUntil(timeout: 2) { loads.count == 1 },
            "The attached view was never asked to load the queued chat: \(loads.map(\.url))"
        )
        let load = try XCTUnwrap(loads.first)
        XCTAssertTrue(load.webView === webView, "The queued chat must load the view attached for the exact origin.")
        XCTAssertTrue(
            loads.allSatisfy { $0.webView !== wrongWebView },
            "The previous origin's view must never receive a load."
        )
        XCTAssertEqual(load.url.scheme, "http")
        XCTAssertEqual(load.url.host, "127.0.0.1")
        XCTAssertEqual(load.url.port, 45999)
        XCTAssertEqual(load.url.path, "/chat/chat-1")
        XCTAssertEqual(load.url.absoluteString, "http://127.0.0.1:45999/chat/chat-1")
        XCTAssertEqual(loads.count, 1, "The queued chat load must happen exactly once.")
        XCTAssertNil(navigation.requestedURL, "The seam receives the queued chat; WebKit is not asked to navigate it here.")
        XCTAssertEqual(presented, 1, "Waiting and attaching do not re-present the window.")
        XCTAssertTrue(transport.opens.isEmpty, "Notifications stayed off; no event-stream credentials were sent.")
        controller.stop()
    }

    func testDeliveredTextIsSanitizedAndCarriesOnlyChatAndOrigin() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        let connection = transport.opens[0].connection
        connection.emit(.snapshot(activeChatIDs: []))
        connection.emit(.resultReady(
            chatID: "chat-1",
            title: "Hello\nWorld\u{202E}",
            snippet: "Done\u{0007} now",
            workspace: "work"
        ))

        let message = try! XCTUnwrap(client.delivered.first)
        XCTAssertEqual(message.title, "Hello World")
        XCTAssertEqual(message.subtitle, "work", "The workspace names where the banner came from.")
        XCTAssertEqual(message.body, "Done now")
        XCTAssertEqual(message.target, ChatNotificationTarget(chatID: "chat-1", origin: origin.canonicalKey))
        XCTAssertEqual(
            message.identifier,
            ChatNotificationController.notificationIdentifier(origin: origin.canonicalKey, chatID: "chat-1")
        )
        XCTAssertNotEqual(
            message.identifier,
            ChatNotificationController.notificationIdentifier(origin: remote.canonicalKey, chatID: "chat-1")
        )
        controller.stop()
    }

    func testIgnoredEventsDoNothing() async {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        client.authorization = .authorized
        let preferences = MemoryNotificationPreference(true)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )

        controller.start()
        await settle()
        let connection = transport.opens[0].connection
        connection.emit(.snapshot(activeChatIDs: []))
        connection.emit(.ignored)
        XCTAssertTrue(client.delivered.isEmpty)
        controller.stop()
    }

    func testMenuRowTogglesTheRealNotificationController() async throws {
        let status = connectedStatus(origin)
        let client = FakeNotificationClient()
        let preferences = MemoryNotificationPreference(false)
        let provider = NotificationFakeCookieProvider()
        provider.cookies = [makeCookie()]
        let transport = NotificationFakeTransport()
        let controller = makeController(
            status: status,
            client: client,
            preferences: preferences,
            provider: provider,
            transport: transport
        )
        let menuBar = MenuBarController(
            status: status,
            statusItemFactory: NotificationFakeStatusItemFactory(),
            openWindow: {},
            quit: {},
            notifications: controller
        )

        menuBar.start()
        controller.start()
        await settle()

        let offItem = try XCTUnwrap(menuBar.menu?.deepItem(withTitle: MenuBarController.notificationsOffTitle))
        menuBar.toggleNotifications(offItem)
        await settle()

        XCTAssertEqual(controller.state, .on)
        XCTAssertEqual(client.requestCount, 1)
        XCTAssertEqual(controller.streamStatus, .connecting)
        XCTAssertNotNil(menuBar.menu?.deepItem(withTitle: MenuBarController.notificationsConnectingTitle))
        XCTAssertEqual(transport.opens.count, 1)

        transport.opens[0].connection.emit(.snapshot(activeChatIDs: []))
        await settle()
        let onItem = try XCTUnwrap(menuBar.menu?.deepItem(withTitle: MenuBarController.notificationsOnTitle))
        XCTAssertEqual(onItem.state, .on)
        menuBar.stop()
    }
}
