import XCTest
import Foundation
import Darwin
import WebKit
import AppKit

@testable import CiaobotMac

/// A minimal HTTP/1.1 server bound to an ephemeral loopback port. It records
/// the Cookie header of every request so tests can prove which cookie store a
/// page load used. It has no relationship to a real Ciaobot server.
private final class CookieFixtureServer {
    struct Request {
        let target: String
        let cookie: String?
    }

    let port: Int

    private let listener: Int32
    private let lock = NSLock()
    private let queue = DispatchQueue(label: "CiaobotMacTests.CookieFixture", attributes: .concurrent)
    private var requests: [Request] = []
    private var stopped = false
    private lazy var thread = Thread { [weak self] in self?.run() }

    init() throws {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw CookieFixtureError("socket") }

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
            throw CookieFixtureError("bind/listen")
        }

        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(fd, $0, &length)
            }
        }
        guard named == 0 else {
            close(fd)
            throw CookieFixtureError("getsockname")
        }

        self.listener = fd
        self.port = Int(UInt16(bigEndian: address.sin_port))
        thread.start()
    }

    deinit { shutdown() }

    var origin: String { "http://127.0.0.1:\(port)" }
    var url: URL { URL(string: origin)! }

    var observed: [Request] {
        lock.lock()
        defer { lock.unlock() }
        return requests
    }

    func cookie(forTargetContaining fragment: String) -> String? {
        observed.last { $0.target.contains(fragment) }?.cookie
    }

    func waitForTarget(containing fragment: String, timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if hasTarget(containing: fragment) { return true }
            // The WebKit view loads on the main run loop, so polling must not
            // block it.
            RunLoop.current.run(until: Date().addingTimeInterval(0.02))
        }
        return hasTarget(containing: fragment)
    }

    func hasTarget(containing fragment: String) -> Bool {
        observed.contains { $0.target.contains(fragment) }
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
        requests.append(Request(target: target, cookie: cookie))
        lock.unlock()

        writeResponse(client, response(for: target))
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

    private func response(for target: String) -> String {
        let parts = target.split(separator: "?", maxSplits: 1).map(String.init)
        let path = parts.first ?? target
        let query = parts.count > 1 ? parts[1] : ""
        switch path {
        case "/set":
            return httpResponse(
                status: "200 OK",
                contentType: "text/html; charset=utf-8",
                body: "<!doctype html><p>cookie set</p><script>window.__fixtureReady = true;</script>",
                extraHeaders: ["Set-Cookie": "session=secret; Path=/"]
            )
        case "/echo":
            return httpResponse(status: "204 No Content", contentType: "text/plain", body: "")
        case "/subresource":
            // A same-host page that requests an image from another port. The
            // navigation delegates never see this request, so it is the
            // subresource caveat in concrete form.
            guard let port = query.split(separator: "&")
                .map(String.init)
                .first(where: { $0.hasPrefix("port=") })
                .map({ String($0.dropFirst("port=".count)) }),
                  Int(port) != nil else {
                return httpResponse(status: "400 Bad Request", contentType: "text/plain", body: "port required")
            }
            let body = "<!doctype html><img id=\"cross-port\" src=\"http://127.0.0.1:\(port)/pixel\">"
            return httpResponse(status: "200 OK", contentType: "text/html; charset=utf-8", body: body)
        case "/pixel":
            return httpResponse(status: "204 No Content", contentType: "text/plain", body: "")
        default:
            return httpResponse(status: "404 Not Found", contentType: "text/plain", body: "not found")
        }
    }

    private func httpResponse(status: String, contentType: String, body: String, extraHeaders: [String: String] = [:]) -> String {
        var headers = [
            "Content-Type": contentType,
            "Content-Length": String(body.utf8.count),
            "Cache-Control": "no-store",
            "Connection": "close",
        ]
        for (key, value) in extraHeaders { headers[key] = value }
        let headerLines = headers.map { "\($0.key): \($0.value)" }.joined(separator: "\r\n")
        return "HTTP/1.1 \(status)\r\n\(headerLines)\r\n\r\n\(body)"
    }

    private func writeResponse(_ client: Int32, _ response: String) {
        let bytes = Array(response.utf8)
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

private func CookieFixtureError(_ operation: String) -> NSError {
    NSError(domain: "CiaobotMacTests.CookieFixture", code: 1, userInfo: [NSLocalizedDescriptionKey: operation])
}

@MainActor
final class PersistentSignInWebKitTests: XCTestCase {
    // Closing WebKit views between test methods crashes WebKit in this XCTest
    // host, so ephemeral-store views are retained for the process. They are
    // kept offscreen: this test only needs networking and the cookie store.
    private static var retainedWebViews: [WKWebView] = []
    private static var retainedCoordinators: [WebWindow.Coordinator] = []

    private var servers: [CookieFixtureServer] = []

    override func tearDown() {
        // These views are offscreen and have no window, so releasing them does
        // not hit the window-teardown crash seen with the on-screen harness.
        for webView in Self.retainedWebViews {
            webView.stopLoading()
            webView.navigationDelegate = nil
            webView.uiDelegate = nil
        }
        Self.retainedWebViews.removeAll()
        Self.retainedCoordinators.removeAll()
        for server in servers { server.shutdown() }
        servers.removeAll()
        super.tearDown()
    }

    func testCrossPortCookiesStayInPerOriginStores() throws {
        let a = try makeServer()
        let b = try makeServer()
        XCTAssertNotEqual(a.port, b.port, "The two fixtures must differ only by port")
        let originA = Origin(a.origin)!
        let originB = Origin(b.origin)!

        let (registry, _) = try makeRegistry()
        let signIn = SessionSignIn(registry: registry, eraser: NoopEraser(), persistentAvailable: true)
        // Persistent sign-in is requested, but HTTP must stay ephemeral.
        XCTAssertEqual(signIn.storage(for: originA, keepSignedIn: true), .ephemeral)
        XCTAssertEqual(signIn.storage(for: originB, keepSignedIn: true), .ephemeral)
        XCTAssertEqual(registry.mappedOriginCount, 0)

        let viewA = try makeWebView(originA, storage: .ephemeral)
        let viewB = try makeWebView(originB, storage: .ephemeral)
        XCTAssertFalse(viewA.configuration.websiteDataStore === viewB.configuration.websiteDataStore)
        XCTAssertFalse(viewA.configuration.websiteDataStore === WKWebsiteDataStore.default())
        XCTAssertFalse(viewB.configuration.websiteDataStore === WKWebsiteDataStore.default())

        viewA.load(URLRequest(url: originA.url.appendingPathComponent("set")))
        XCTAssertTrue(a.waitForTarget(containing: "/set", timeout: 5))
        XCTAssertTrue(waitForCookie(viewA, "session=secret", timeout: 5),
                      "The fixture cookie was not stored by the first origin")

        viewA.load(URLRequest(url: originA.url.appendingPathComponent("echo?from=a")))
        XCTAssertTrue(a.waitForTarget(containing: "from=a", timeout: 5))
        XCTAssertEqual(a.cookie(forTargetContaining: "from=a"), "session=secret")

        // Control: the same store loading the other port does send the cookie.
        // WebKit cookies ignore ports, so port isolation only holds because the
        // app gives each canonical origin its own store.
        viewA.load(URLRequest(url: originB.url.appendingPathComponent("echo?from=shared")))
        XCTAssertTrue(b.waitForTarget(containing: "from=shared", timeout: 5))
        XCTAssertEqual(b.cookie(forTargetContaining: "from=shared"), "session=secret",
                       "The control did not reproduce WebKit's port-insensitive cookie scope")

        viewB.load(URLRequest(url: originB.url.appendingPathComponent("echo?from=isolated")))
        XCTAssertTrue(b.waitForTarget(containing: "from=isolated", timeout: 5))
        XCTAssertNil(b.cookie(forTargetContaining: "from=isolated"),
                     "A cookie from port \(a.port) leaked to port \(b.port)")
    }

    /// A per-origin store isolates top-level views, but a page can still ask
    /// its own store to fetch a subresource from another port on the same host.
    /// Subresource loads never reach the navigation delegates, and WebKit
    /// cookies are host-scoped, not port-scoped. This test records the real
    /// behaviour so the documentation does not promise subresource isolation.
    func testCrossPortSubresourceCarriesTheSameHostCookie() throws {
        let pageServer = try makeServer()
        let resourceServer = try makeServer()
        XCTAssertNotEqual(pageServer.port, resourceServer.port)
        let origin = Origin(pageServer.origin)!
        XCTAssertFalse(origin.usesTLS, "This fixture is HTTP, so the store is ephemeral")

        let coordinator = makeCoordinator(origin)
        let view = WebWindow.Coordinator.makeWebView(origin: origin, coordinator: coordinator, storage: .ephemeral)
        Self.retainedWebViews.append(view)
        Self.retainedCoordinators.append(coordinator)

        view.load(URLRequest(url: origin.url.appendingPathComponent("set")))
        XCTAssertTrue(pageServer.waitForTarget(containing: "/set", timeout: 5))
        XCTAssertTrue(waitForCookie(view, "session=secret", timeout: 5),
                      "The fixture cookie was not stored")

        view.load(URLRequest(url: URL(string: "\(pageServer.origin)/subresource?port=\(resourceServer.port)")!))
        XCTAssertTrue(pageServer.waitForTarget(containing: "/subresource", timeout: 5))
        XCTAssertTrue(resourceServer.waitForTarget(containing: "/pixel", timeout: 5),
                      "WebKit did not request the cross-port subresource, so the caveat was not exercised")
        XCTAssertEqual(resourceServer.cookie(forTargetContaining: "/pixel"), "session=secret",
                       "WebKit did not send the host-scoped cookie to the other port")
    }

    func testForgetWhileAStoreIsInUseNeverReportsSuccessWithItStillListed() async throws {
        guard #available(macOS 14.0, *) else { throw XCTSkip("WKWebsiteDataStore(forIdentifier:) requires macOS 14") }

        let (registry, _) = try makeRegistry()
        let origin = Origin("https://127.0.0.1:8443")!
        let identifier = try XCTUnwrap(registry.identifier(for: origin))
        let signIn = SessionSignIn(registry: registry, eraser: WebKitWebsiteDataStoreEraser(), persistentAvailable: true)

        let handle = StoreHandle()
        await seedCookie(in: identifier, handle: handle)
        XCTAssertNil(handle.store, "The seeding store was still alive")

        // Keep a live WebKit view on the same store.
        let coordinator = makeCoordinator(origin)
        let configuration = WebWindow.Coordinator.makeConfiguration(
            coordinator: coordinator,
            storage: .persistent(identifier)
        )
        let view = WKWebView(frame: NSRect(x: 0, y: 0, width: 320, height: 240), configuration: configuration)
        Self.retainedWebViews.append(view)
        Self.retainedCoordinators.append(coordinator)
        view.load(URLRequest(url: origin.url))

        let result: Result<Void, ForgetFailure> = await withCheckedContinuation { continuation in
            signIn.forget(origin: origin, releaseWebViews: { $0() }) { continuation.resume(returning: $0) }
        }
        let listed = await WKWebsiteDataStore.allDataStoreIdentifiers

        switch result {
        case .success:
            XCTAssertFalse(listed.contains(identifier),
                           "forget reported success while WebKit still listed the store")
        case .failure(.storeStillPresent):
            XCTAssertTrue(listed.contains(identifier),
                          "A reported still-present failure must match WebKit's store list")
            XCTAssertTrue(registry.contains(origin), "A failed forget must retain the mapping")
            XCTAssertTrue(signIn.hasSavedSignIn(for: origin))
        case .failure(let other):
            XCTFail("Unexpected forget failure: \(other)")
        }
    }

    func testOptedInHTTPSConfigurationUsesItsOwnPersistentStore() async throws {
        guard #available(macOS 14.0, *) else { throw XCTSkip("WKWebsiteDataStore(forIdentifier:) requires macOS 14") }

        let (registry, _) = try makeRegistry()
        let signIn = SessionSignIn(registry: registry, eraser: WebKitWebsiteDataStoreEraser(), persistentAvailable: true)
        let origin = Origin("https://127.0.0.1:8443")!
        let other = Origin("https://127.0.0.1:8444")!

        var identifiers: [UUID] = []
        var sharedStore = false
        do {
            let storage = signIn.storage(for: origin, keepSignedIn: true)
            let otherStorage = signIn.storage(for: other, keepSignedIn: true)
            guard case .persistent(let identifier) = storage,
                  case .persistent(let otherIdentifier) = otherStorage else {
                return XCTFail("An opted-in HTTPS origin did not get a persistent store")
            }
            XCTAssertNotEqual(identifier, otherIdentifier)

            let first = WebWindow.Coordinator.makeConfiguration(
                coordinator: makeCoordinator(origin),
                storage: storage
            )
            let second = WebWindow.Coordinator.makeConfiguration(
                coordinator: makeCoordinator(other),
                storage: otherStorage
            )

            XCTAssertTrue(first.websiteDataStore.isPersistent)
            XCTAssertEqual(first.websiteDataStore.identifier, identifier)
            XCTAssertFalse(first.websiteDataStore === WKWebsiteDataStore.default())
            XCTAssertEqual(second.websiteDataStore.identifier, otherIdentifier)
            sharedStore = first.websiteDataStore === second.websiteDataStore
            identifiers = [identifier, otherIdentifier]
        }

        XCTAssertFalse(sharedStore, "Different ports must not share one persistent store")
        for identifier in identifiers {
            try? await WKWebsiteDataStore.remove(forIdentifier: identifier)
        }
    }

    func testForgetDeletesThePersistentStoreAndItsCookies() async throws {
        guard #available(macOS 14.0, *) else { throw XCTSkip("WKWebsiteDataStore(forIdentifier:) requires macOS 14") }

        let (registry, _) = try makeRegistry()
        let origin = Origin("https://127.0.0.1:8443")!
        let identifier = try XCTUnwrap(registry.identifier(for: origin))
        let signIn = SessionSignIn(registry: registry, eraser: WebKitWebsiteDataStoreEraser(), persistentAvailable: true)
        XCTAssertEqual(signIn.storage(for: origin, keepSignedIn: true), .persistent(identifier))

        // Seed from a helper so the store instance is released before forget:
        // WebKit will not remove a store that is still held.
        let handle = StoreHandle()
        await seedCookie(in: identifier, handle: handle)
        XCTAssertNil(handle.store, "The seeding store was still alive")
        let listed = await WKWebsiteDataStore.allDataStoreIdentifiers
        XCTAssertTrue(listed.contains(identifier))

        let result: Result<Void, ForgetFailure> = await withCheckedContinuation { continuation in
            signIn.forget(origin: origin, releaseWebViews: { $0() }) { continuation.resume(returning: $0) }
        }

        assertForgetSucceeded(result)
        XCTAssertFalse(registry.contains(origin))
        XCTAssertFalse(signIn.hasSavedSignIn(for: origin))
        let remaining = await WKWebsiteDataStore.allDataStoreIdentifiers
        XCTAssertFalse(remaining.contains(identifier), "WebKit still lists the store after forget")

        // The whole point of forget is that WebKit no longer lists or serves
        // the store, not just that the mapping is gone. The app does not
        // inspect on-disk bytes, so no stronger claim is made here.

        // Recreating the identifier now yields a brand-new empty store; the
        // authoritative proof of removal is that WebKit no longer lists it.
        try? await WKWebsiteDataStore.remove(forIdentifier: identifier)
    }

    func testForgetRemovesMappingWhenTheStoreWasNeverCreated() async throws {
        guard #available(macOS 14.0, *) else { throw XCTSkip("WKWebsiteDataStore(forIdentifier:) requires macOS 14") }

        let (registry, _) = try makeRegistry()
        let origin = Origin("https://127.0.0.1:8443")!
        let identifier = try XCTUnwrap(registry.identifier(for: origin))
        let signIn = SessionSignIn(registry: registry, eraser: WebKitWebsiteDataStoreEraser(), persistentAvailable: true)

        let result: Result<Void, ForgetFailure> = await withCheckedContinuation { continuation in
            signIn.forget(origin: origin, releaseWebViews: { $0() }) { continuation.resume(returning: $0) }
        }

        assertForgetSucceeded(result)
        XCTAssertFalse(registry.contains(origin))
        let identifiers = await WKWebsiteDataStore.allDataStoreIdentifiers
        XCTAssertFalse(identifiers.contains(identifier))
    }

    private func waitForCookie(_ webView: WKWebView, _ value: String, timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            var found = false
            var done = false
            webView.evaluateJavaScript("document.cookie") { result, _ in
                found = (result as? String)?.contains(value) == true
                done = true
            }
            while !done && Date() < deadline {
                RunLoop.main.run(until: Date().addingTimeInterval(0.02))
            }
            if found { return true }
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
        return false
    }

    private func makeWebView(_ origin: Origin, storage: SessionStorage) throws -> WKWebView {
        let coordinator = makeCoordinator(origin)
        let configuration = WebWindow.Coordinator.makeConfiguration(
            coordinator: coordinator,
            storage: storage
        )
        // No navigation delegate: this test measures the cookie store, not
        // navigation policy.
        let webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 700, height: 500), configuration: configuration)
        Self.retainedWebViews.append(webView)
        Self.retainedCoordinators.append(coordinator)
        return webView
    }

    private func makeCoordinator(_ origin: Origin) -> WebWindow.Coordinator {
        WebWindow(origin: origin).makeCoordinator()
    }

    private func makeServer() throws -> CookieFixtureServer {
        let server = try CookieFixtureServer()
        servers.append(server)
        return server
    }
}

@MainActor
private final class NoopEraser: WebsiteDataStoreErasing {
    func remove(identifier: UUID, completion: @escaping (Error?) -> Void) {
        completion(nil)
    }
}

@MainActor
private final class StoreHandle {
    weak var store: WKWebsiteDataStore?
}

@MainActor
@available(macOS 14.0, *)
private func seedCookie(in identifier: UUID, handle: StoreHandle) async {
    guard let cookie = HTTPCookie(properties: [
        .domain: "127.0.0.1", .path: "/", .name: "session", .value: "secret",
    ]) else {
        return XCTFail("Could not build the fixture cookie")
    }
    let store = WKWebsiteDataStore(forIdentifier: identifier)
    handle.store = store
    await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
        store.httpCookieStore.setCookie(cookie) { continuation.resume() }
    }
    let cookies = await store.httpCookieStore.allCookies()
    XCTAssertTrue(cookies.contains { $0.name == "session" })
    let records = await store.dataRecords(ofTypes: [WKWebsiteDataTypeCookies])
    XCTAssertTrue(records.contains { $0.displayName == "127.0.0.1" },
                  "The seeded cookie was not part of the named store's website data")
}
