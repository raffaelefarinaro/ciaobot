import XCTest
import Foundation
import Darwin
import WebKit
import AppKit

@testable import CiaobotMac

private final class WebKitTestServer {
    struct Request {
        let path: String
        let target: String
        let cookie: String?
    }

    let port: Int

    private let listener: Int32
    private let lock = NSLock()
    private let queue = DispatchQueue(label: "CiaobotMacTests.WebKitFixture", attributes: .concurrent)
    private var requests: [Request] = []
    private var redirects: [String: String] = [:]
    private var pages: [String: String] = [:]
    private var stalls: [String: TimeInterval] = [:]
    private var stopped = false
    private lazy var thread = Thread { [weak self] in self?.run() }

    init() throws {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw makeWebKitError("socket") }

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
            throw makeWebKitError("bind/listen")
        }

        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(fd, $0, &length)
            }
        }
        guard named == 0 else {
            close(fd)
            throw makeWebKitError("getsockname")
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

    func requests(for path: String) -> [Request] {
        observed.filter { $0.path == path }
    }

    func setRedirect(_ path: String, to location: String) {
        lock.lock()
        redirects[path] = location
        lock.unlock()
    }

    func setPage(_ path: String, body: String) {
        lock.lock()
        pages[path] = body
        lock.unlock()
    }

    /// Sends the response, then keeps the connection open for `seconds`. Use a
    /// response whose Content-Length exceeds its body so the client keeps
    /// waiting, which leaves a download in progress.
    func setStall(_ path: String, seconds: TimeInterval) {
        lock.lock()
        stalls[path] = seconds
        lock.unlock()
    }

    private func stall(for path: String) -> TimeInterval? {
        lock.lock()
        defer { lock.unlock() }
        return stalls[path]
    }

    func hasRequest(_ path: String) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return requests.contains { $0.path == path }
    }

    /// Waits for a recorded request without parking the thread WebKit needs.
    /// WebKit delivers its navigation callbacks on the main thread, so a
    /// blocking sleep here would hold the request back until the deadline
    /// expired. On the main thread the run loop keeps turning while the
    /// deadline is open; only background callers fall back to a short sleep.
    func waitForRequest(_ path: String, timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while true {
            if hasRequest(path) { return true }
            if Date() >= deadline { return false }
            if Thread.isMainThread {
                RunLoop.main.run(until: Date().addingTimeInterval(0.01))
            } else {
                usleep(10_000)
            }
        }
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
        let lines = request.components(separatedBy: "\r\n")
        guard let requestLine = lines.first else { return }
        let parts = requestLine.split(separator: " ", maxSplits: 2)
        guard parts.count >= 2 else { return }
        let target = String(parts[1])
        let path = target.split(separator: "?", maxSplits: 1).first.map(String.init) ?? target
        let cookie = lines.first { $0.lowercased().hasPrefix("cookie:") }
            .map { String($0.dropFirst("cookie:".count)).trimmingCharacters(in: .whitespaces) }

        lock.lock()
        requests.append(Request(path: path, target: target, cookie: cookie))
        lock.unlock()

        writeResponse(client, response(for: path))
        if let seconds = stall(for: path) {
            usleep(UInt32(seconds * 1_000_000))
        }
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

    private func response(for path: String) -> String {
        lock.lock()
        let redirect = redirects[path]
        let page = pages[path]
        lock.unlock()
        if let redirect {
            return "HTTP/1.1 302 Found\r\nLocation: \(redirect)\r\nContent-Length: 0\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
        }
        if let page {
            return httpResponse(status: "200 OK", contentType: "text/html; charset=utf-8", body: page)
        }
        switch path {
        case "/child":
            return httpResponse(
                status: "200 OK",
                contentType: "text/html; charset=utf-8",
                body: "<!doctype html><title>child</title><h1 id=\"child\">child</h1>"
            )
        case "/image":
            return httpResponse(
                status: "200 OK",
                contentType: "image/svg+xml",
                body: "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"32\" height=\"32\"><rect width=\"32\" height=\"32\" fill=\"#2b6\"/></svg>"
            )
        case "/download":
            return httpResponse(
                status: "200 OK",
                contentType: "application/octet-stream",
                body: "fixture-download",
                extraHeaders: ["Content-Disposition": "attachment; filename=\"fixture.txt\""]
            )
        case "/download-truncated":
            return "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Disposition: attachment; filename=\"truncated.txt\"\r\nContent-Length: 1000\r\nConnection: close\r\n\r\npartial"
        case "/download-slow":
            return "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Disposition: attachment; filename=\"slow.txt\"\r\nContent-Length: 100000\r\nConnection: close\r\n\r\npartial"
        case "/oauth":
            return httpResponse(status: "200 OK", contentType: "text/html; charset=utf-8", body: "oauth")
        case "/cookie-page":
            return httpResponse(
                status: "200 OK",
                contentType: "text/html; charset=utf-8",
                body: """
                <!doctype html><a id="child" href="/child" target="_blank">child</a>
                <script>window.__fixtureReady = true;</script>
                """,
                extraHeaders: ["Set-Cookie": "session=secret; Path=/; HttpOnly"]
            )
        case "/opener-child":
            return httpResponse(
                status: "200 OK",
                contentType: "text/html; charset=utf-8",
                body: "<!doctype html><script>fetch('/opener-seen?opener=' + (window.opener ? '1' : '0') + '&src=opener');</script>"
            )
        case "/opener-seen":
            return httpResponse(status: "204 No Content", contentType: "text/plain", body: "")
        case "/upload-frame":
            return httpResponse(
                status: "200 OK",
                contentType: "text/html; charset=utf-8",
                body: """
                <!doctype html><input id="f" type="file">
                <script>
                  if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.ciaobotCrossOriginProbe) {
                    window.webkit.messageHandlers.ciaobotCrossOriginProbe.postMessage(1);
                  }
                  document.getElementById('f').click();
                </script>
                """
            )
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

private func makeWebKitError(_ operation: String) -> NSError {
    NSError(domain: "CiaobotMacTests.WebKitFixture", code: 1, userInfo: [NSLocalizedDescriptionKey: operation])
}

@MainActor
final class WebKitDelegateTests: XCTestCase {
    // Keep WebKit windows and coordinators alive for the whole test process.
    // Closing them between test methods crashes WebKit in this XCTest host;
    // the process exits after the suite.
    private static var retainedWindows: [NSWindow] = []
    private static var retainedCoordinators: [WebWindow.Coordinator] = []
    private var servers: [WebKitTestServer] = []

    func testHostChatNavigationAllowsInitialAndSameOriginRoutesButRejectsUnsafeBranches() throws {
        let origin = try XCTUnwrap(Origin("https://host.example"))
        let coordinator = WebWindow.Coordinator(
            origin: origin,
            onFailure: nil,
            downloadFailure: { _ in },
            filePanel: { _, completion in completion(nil) },
            downloadDestination: { _, completion in completion(nil) },
            openExternal: { _ in XCTFail("host chat must not open external URLs") },
            navigationProfile: .hostChat
        )
        let first = try XCTUnwrap(ChatLink.url(origin: origin, chatID: "first"))
        let second = try XCTUnwrap(ChatLink.url(origin: origin, chatID: "second"))
        XCTAssertFalse(coordinator.permitsPopupCreation)
        XCTAssertTrue(coordinator.hostChatActionAllowed(url: origin.url.appendingPathComponent("submit"), targetFrameIsMain: true, navigationType: .formSubmitted, httpMethod: "POST"))
        coordinator.loadHostChat(first)
        XCTAssertFalse(coordinator.hostChatActionAllowed(url: first, targetFrameIsMain: nil, navigationType: .other, httpMethod: "POST"))
        XCTAssertTrue(coordinator.hostChatActionAllowed(url: first, targetFrameIsMain: nil, navigationType: .other))
        XCTAssertFalse(coordinator.hostChatActionAllowed(url: first, targetFrameIsMain: nil, navigationType: .other),
                       "The app-initiated allowance is one shot")
        XCTAssertTrue(coordinator.hostChatActionAllowed(url: first, targetFrameIsMain: true, navigationType: .other),
                      "A later main-frame reload is ordinary same-origin navigation")
        XCTAssertTrue(coordinator.hostChatActionAllowed(url: origin.url.appendingPathComponent("settings"), targetFrameIsMain: true, navigationType: .linkActivated))
        XCTAssertFalse(coordinator.hostChatActionAllowed(url: second, targetFrameIsMain: nil, navigationType: .linkActivated),
                       "Targetless actions are refused")
        coordinator.loadHostChat(second)
        XCTAssertTrue(coordinator.hostChatActionAllowed(url: second, targetFrameIsMain: nil, navigationType: .other))
        coordinator.loadHostChat(second)
        XCTAssertFalse(coordinator.hostChatActionAllowed(url: first, targetFrameIsMain: nil, navigationType: .other),
                       "An armed allowance cannot authorize a different request")
        XCTAssertTrue(coordinator.hostChatActionAllowed(url: second, targetFrameIsMain: nil, navigationType: .other))
        XCTAssertFalse(coordinator.hostChatActionAllowed(url: URL(string: "https://foreign.example/chat/third")!, targetFrameIsMain: true, navigationType: .linkActivated))
        XCTAssertFalse(coordinator.hostChatActionAllowed(url: origin.url.appendingPathComponent("download"), targetFrameIsMain: true, navigationType: .other, shouldPerformDownload: true))
        XCTAssertTrue(coordinator.hostChatResponseAllowed(url: origin.url.appendingPathComponent("settings"), canShowMIMEType: true, isAttachment: false))
        XCTAssertFalse(coordinator.hostChatResponseAllowed(url: URL(string: "https://foreign.example/redirect")!, canShowMIMEType: true, isAttachment: false))
        XCTAssertFalse(coordinator.hostChatResponseAllowed(url: first, canShowMIMEType: true, isAttachment: true))
    }

    func testHostChatDelegateDecisionSeamsCancelUnsafeRequestsWithoutSideEffects() throws {
        let origin = try XCTUnwrap(Origin("https://host.example"))
        var opened = [URL]()
        let coordinator = WebWindow.Coordinator(origin: origin, onFailure: nil, downloadFailure: { _ in },
            filePanel: { _, done in done(nil) }, downloadDestination: { _, done in done(nil) },
            openExternal: { opened.append($0) }, navigationProfile: .hostChat)
        let foreign = try XCTUnwrap(URL(string: "https://foreign.example/download"))
        var action: WKNavigationActionPolicy?
        coordinator.decideHostChatAction(url: foreign, targetFrameIsMain: true, navigationType: .linkActivated,
                                         shouldPerformDownload: true, httpMethod: "GET") { action = $0 }
        XCTAssertEqual(action, .cancel)
        XCTAssertTrue(opened.isEmpty)
        var response: WKNavigationResponsePolicy?
        coordinator.decideHostChatResponse(url: foreign, canShowMIMEType: true, isAttachment: false) { response = $0 }
        XCTAssertEqual(response, .cancel)
        var popupFallbackCalled = false
        let popup = coordinator.createAllowedPopup { popupFallbackCalled = true; return nil }
        XCTAssertNil(popup)
        XCTAssertFalse(popupFallbackCalled)
        XCTAssertTrue(opened.isEmpty)
    }

    override func tearDown() {
        for coordinator in Self.retainedCoordinators {
            coordinator.children.forEach { $0.close() }
        }
        for server in servers { server.shutdown() }
        servers.removeAll()
        super.tearDown()
    }

    func testOpenPanelConfigurationHonorsMultipleAndDirectories() {
        let multiple = OpenPanelConfiguration(FilePanelRequest(allowsMultipleSelection: true, allowsDirectories: false, isMainFrame: true))
        XCTAssertTrue(multiple.allowsMultipleSelection)
        XCTAssertFalse(multiple.canChooseDirectories)
        XCTAssertTrue(multiple.canChooseFiles)

        let directory = OpenPanelConfiguration(FilePanelRequest(allowsMultipleSelection: false, allowsDirectories: true, isMainFrame: true))
        XCTAssertFalse(directory.allowsMultipleSelection)
        XCTAssertTrue(directory.canChooseDirectories)
        XCTAssertFalse(directory.canChooseFiles)
    }

    func testOriginMatchesWebKitDefaultPort() {
        let origin = Origin("https://example.com")!
        XCTAssertTrue(origin.matches(scheme: "https", host: "example.com", port: 0))
        XCTAssertTrue(origin.matches(scheme: "https", host: "EXAMPLE.com", port: 443))
        XCTAssertFalse(origin.matches(scheme: "https", host: "example.com", port: 444))
        XCTAssertFalse(origin.matches(scheme: "http", host: "example.com", port: 0))
        XCTAssertFalse(origin.matches(scheme: "https", host: "example.com.evil", port: 443))
    }

    func testSafeDownloadFilenameStripsPaths() {
        XCTAssertEqual(DownloadDestinationPresenter.safeFilename("../../secret.txt"), "secret.txt")
        XCTAssertEqual(DownloadDestinationPresenter.safeFilename("/etc/passwd"), "passwd")
        XCTAssertEqual(DownloadDestinationPresenter.safeFilename(".."), "download")
        XCTAssertEqual(DownloadDestinationPresenter.safeFilename(""), "download")
    }

    func testDownloadDecisionsKeepOnlySessionOriginInApp() {
        let origin = Origin("https://example.com")!
        XCTAssertEqual(origin.download(URL(string: "https://example.com/file")!), .inApp)
        XCTAssertEqual(origin.download(URL(string: "data:text/plain,hello")!), .inApp)
        XCTAssertEqual(origin.download(URL(string: "blob:https://example.com/0000")!), .inApp)
        XCTAssertEqual(origin.download(URL(string: "blob:https://evil.example/0000")!), .blocked)
        XCTAssertEqual(origin.download(URL(string: "https://evil.example/file")!), .external)
        XCTAssertEqual(origin.download(URL(string: "http://example.com/file")!), .external)
        XCTAssertEqual(origin.download(URL(string: "file:///etc/passwd")!), .blocked)
    }

    func testFilePanelHonorsMultipleSelectionAndSelectedFiles() throws {
        let origin = Origin("https://example.com")!
        let first = try makeTempFile("first.txt", "one")
        let second = try makeTempFile("second.txt", "two")
        defer {
            try? FileManager.default.removeItem(at: first)
            try? FileManager.default.removeItem(at: second)
        }
        var requests: [FilePanelRequest] = []
        var selection: [URL]? = [first, second]

        let (_, webView) = try makeHarness(origin: origin, filePanel: { request, completion in
            requests.append(request)
            completion(selection)
        })
        webView.loadHTMLString(fixtureHTML("""
        <input id="multi" type="file" multiple>
        <script>window.picked = null; document.getElementById('multi').addEventListener('change', e => {
          window.picked = { count: e.target.files.length, names: Array.from(e.target.files).map(f => f.name) };
        });</script>
        """), baseURL: origin.url)

        try waitForReady(webView)
        webView.evaluateJavaScript("document.getElementById('multi').click()")
        XCTAssertTrue(waitFor(timeout: 3) { requests.count == 1 })
        XCTAssertEqual(requests.first?.allowsMultipleSelection, true)
        XCTAssertEqual(requests.first?.allowsDirectories, false)
        XCTAssertTrue(waitFor(timeout: 3) { self.jsBoolean(webView, "window.picked && window.picked.count === 2") })
        XCTAssertEqual(jsString(webView, "window.picked && window.picked.names.join(',')"), "first.txt,second.txt")

        selection = nil
        webView.evaluateJavaScript("document.getElementById('multi').click()")
        XCTAssertTrue(waitFor(timeout: 3) { requests.count == 2 })
        XCTAssertEqual(requests.last?.allowsMultipleSelection, true)
    }

    func testFilePanelHonorsDirectorySelection() throws {
        let origin = Origin("https://example.com")!
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("fixture-dir-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        _ = try makeTempFile(in: directory, name: "inside.txt", contents: "inside")

        var requests: [FilePanelRequest] = []
        let (_, webView) = try makeHarness(origin: origin, filePanel: { request, completion in
            requests.append(request)
            completion([directory])
        })
        webView.loadHTMLString(fixtureHTML("<input id=\"dir\" type=\"file\" webkitdirectory multiple>"), baseURL: origin.url)
        try waitForReady(webView)
        webView.evaluateJavaScript("document.getElementById('dir').click()")
        XCTAssertTrue(waitFor(timeout: 3) { requests.count == 1 })
        XCTAssertEqual(requests.first?.allowsDirectories, true)
        XCTAssertEqual(requests.first?.allowsMultipleSelection, true)
        XCTAssertEqual(requests.first?.isMainFrame, true)
    }

    func testFilePanelCancelReturnsNoSelection() throws {
        let origin = Origin("https://example.com")!
        var requests = 0
        let (_, webView) = try makeHarness(origin: origin, filePanel: { _, completion in
            requests += 1
            completion(nil)
        })
        webView.loadHTMLString(fixtureHTML("""
        <input id="file" type="file">
        <script>window.picked = -1; document.getElementById('file').addEventListener('change', e => { window.picked = e.target.files.length; });</script>
        """), baseURL: origin.url)
        try waitForReady(webView)
        webView.evaluateJavaScript("document.getElementById('file').click()")
        XCTAssertTrue(waitFor(timeout: 3) { requests == 1 })
    }

    func testScriptedWindowOpenOnLoadDoesNotCreateAWindow() throws {
        let origin = Origin("http://127.0.0.1:1")!
        var opens = 0
        let (coordinator, webView) = try makeHarness(origin: origin, openExternal: { _ in opens += 1 })
        webView.loadHTMLString(fixtureHTML("<script>window.open('/child', '_blank')</script>"), baseURL: origin.url)
        try waitForReady(webView)
        XCTAssertTrue(waitFor(timeout: 1) { coordinator.children.isEmpty })
        XCTAssertEqual(coordinator.trustedActivationCount, 0)
        XCTAssertEqual(opens, 0)
    }

    func testNewWindowDecisionRequiresUserGesture() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        var opened: [URL] = []
        let (coordinator, webView) = try makeHarness(origin: origin, openExternal: { opened.append($0) })

        XCTAssertNil(coordinator.newWindow(for: origin.url.appendingPathComponent("child"), userGesture: false, configuration: webView.configuration))
        XCTAssertTrue(coordinator.children.isEmpty)
        XCTAssertTrue(opened.isEmpty)

        let childView = try XCTUnwrap(coordinator.newWindow(
            for: origin.url.appendingPathComponent("child"),
            userGesture: true,
            configuration: webView.configuration
        ))
        XCTAssertTrue(waitFor(timeout: 3) { childView.url?.path == "/child" })
        XCTAssertEqual(coordinator.children.count, 1)
        XCTAssertTrue(coordinator.children[0].webView.configuration.websiteDataStore === webView.configuration.websiteDataStore)

        let external = URL(string: "https://example.test/outside")!
        XCTAssertNil(coordinator.newWindow(for: external, userGesture: false, configuration: webView.configuration))
        XCTAssertTrue(opened.isEmpty)
        XCTAssertNil(coordinator.newWindow(for: external, userGesture: true, configuration: webView.configuration))
        XCTAssertEqual(opened.map(\.absoluteString), [external.absoluteString])
    }

    func testUserGestureGateIsSingleUseAndExpires() {
        let gate = UserGestureGate(validity: 0.05)
        let now = Date()
        XCTAssertFalse(gate.consume(now: now))
        gate.record(now: now)
        XCTAssertTrue(gate.consume(now: now))
        XCTAssertFalse(gate.consume(now: now))

        gate.record(now: now)
        XCTAssertFalse(gate.consume(now: now.addingTimeInterval(0.1)))
    }

    func testScriptedExternalTargetBlankIsBlockedWithoutGesture() throws {
        let origin = Origin("https://example.com")!
        var opened: [URL] = []
        let (coordinator, webView) = try makeHarness(origin: origin, openExternal: { opened.append($0) })
        webView.loadHTMLString(fixtureHTML("""
        <a id="external" href="https://example.test/outside" target="_blank">outside</a>
        """), baseURL: origin.url)
        try waitForReady(webView)
        webView.evaluateJavaScript("document.getElementById('external').click()")
        XCTAssertTrue(waitFor(timeout: 1) { coordinator.children.isEmpty })
        XCTAssertTrue(opened.isEmpty)
    }

    func testTrustedActivationAllowsExternalTargetBlank() throws {
        let origin = Origin("https://example.com")!
        var opened: [URL] = []
        let (coordinator, webView) = try makeHarness(origin: origin, openExternal: { opened.append($0) })
        webView.loadHTMLString(fixtureHTML("""
        <a id="external" href="https://example.test/outside" target="_blank"
          onclick="window.webkit.messageHandlers.ciaobotTrustedClick.postMessage(1)">outside</a>
        """), baseURL: origin.url)
        try waitForReady(webView)
        webView.evaluateJavaScript("document.getElementById('external').click()")
        XCTAssertTrue(waitFor(timeout: 2) { opened.count == 1 })
        XCTAssertEqual(opened.first?.absoluteString, "https://example.test/outside")
        XCTAssertTrue(coordinator.children.isEmpty)
        XCTAssertEqual(coordinator.trustedActivationCount, 1)
    }

    func testSyntheticClickDoesNotRecordTrustedActivation() throws {
        let origin = Origin("https://example.com")!
        let (coordinator, webView) = try makeHarness(origin: origin)
        webView.loadHTMLString(fixtureHTML("""
        <button id="plain">plain</button>
        """), baseURL: origin.url)
        try waitForReady(webView)
        webView.evaluateJavaScript("document.getElementById('plain').click()")
        XCTAssertTrue(waitFor(timeout: 1) { coordinator.trustedActivationCount == 0 })
    }

    func testTrustedActivationAllowsSameOriginImageInChild() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        let (coordinator, webView) = try makeHarness(origin: origin, openExternal: { _ in })
        webView.loadHTMLString(fixtureHTML("""
        <a id="image" href="/image" target="_blank">image</a>
        """), baseURL: origin.url)
        try waitForReady(webView)
        coordinator.noteTrustedActivationForTesting()
        webView.evaluateJavaScript("document.getElementById('image').click()")
        XCTAssertTrue(waitFor(timeout: 3) { coordinator.children.count == 1 })
        let child = try XCTUnwrap(coordinator.children.first)
        XCTAssertTrue(child.webView.configuration.websiteDataStore === webView.configuration.websiteDataStore)
        XCTAssertTrue(waitFor(timeout: 3) { server.waitForRequest("/image", timeout: 0.1) })
        XCTAssertTrue(waitFor(timeout: 3) { child.hasWindow })
        XCTAssertEqual(child.webView.url?.path, "/image")
    }

    func testOAuthPlaceholderDecisionSeamSharesProvidedStoreButNeverLoadsExternal() throws {
        let server = try makeServer()
        let external = try makeServer()
        let origin = Origin(server.origin)!
        var opened: [URL] = []
        let (coordinator, webView) = try makeHarness(origin: origin, openExternal: { opened.append($0) })
        // This exercises the newWindow decision seam, not an actual JS
        // window.open('', '_blank'): WebKit did not deliver that blank popup
        // in the automated host, so end-to-end OAuth remains unverified.
        let authURL = external.url.appendingPathComponent("oauth")
        webView.loadHTMLString(fixtureHTML("<p>oauth</p>"), baseURL: origin.url)
        try waitForReady(webView)
        let child = try XCTUnwrap(coordinator.newWindow(
            for: nil,
            userGesture: true,
            configuration: webView.configuration
        ))
        let childController = try XCTUnwrap(coordinator.children.first)
        // WebKit requires the configuration supplied to createWebViewWith.
        // A different websiteDataStore crashes createNewPage, so the OAuth
        // placeholder shares the nonpersistent session store but never loads
        // the external authorization URL; it hands that URL to the browser.
        XCTAssertTrue(childController.webView.configuration.websiteDataStore === webView.configuration.websiteDataStore)
        XCTAssertFalse(childController.webView.configuration.websiteDataStore === WKWebsiteDataStore.default())
        child.load(URLRequest(url: authURL))
        XCTAssertTrue(waitFor(timeout: 3) { opened.count == 1 })
        XCTAssertEqual(opened.first?.absoluteString, authURL.absoluteString)
        XCTAssertTrue(waitFor(timeout: 3) { coordinator.children.isEmpty })
        XCTAssertTrue(external.observed.isEmpty)
    }

    func testSameOriginDownloadUsesSaveDestination() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        let destination = FileManager.default.temporaryDirectory.appendingPathComponent("ciaobot-download-\(UUID().uuidString).txt")
        defer { try? FileManager.default.removeItem(at: destination) }
        var downloads: [DownloadRequest] = []
        let (_, webView) = try makeHarness(origin: origin, downloadDestination: { request, completion in
            downloads.append(request)
            completion(destination)
        })
        webView.loadHTMLString(fixtureHTML("""
        <a id="download" href="/download" download>download</a>
        """), baseURL: origin.url)
        try waitForReady(webView)
        webView.load(URLRequest(url: origin.url.appendingPathComponent("download")))
        XCTAssertTrue(waitFor(timeout: 5) { downloads.count == 1 })
        XCTAssertEqual(downloads.first?.suggestedFilename, "fixture.txt")
        XCTAssertTrue(waitFor(timeout: 5) {
            (try? String(contentsOf: destination, encoding: .utf8)) == "fixture-download"
        })
    }

    func testDownloadAttributeUsesDownloadPolicy() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        let destination = FileManager.default.temporaryDirectory.appendingPathComponent("ciaobot-attr-download-\(UUID().uuidString).txt")
        defer { try? FileManager.default.removeItem(at: destination) }
        var downloads: [DownloadRequest] = []
        let (_, webView) = try makeHarness(origin: origin, downloadDestination: { request, completion in
            downloads.append(request)
            completion(destination)
        })
        webView.loadHTMLString(fixtureHTML("""
        <a id="download" href="/download" download>download</a>
        """), baseURL: origin.url)
        try waitForReady(webView)
        webView.evaluateJavaScript("document.getElementById('download').click()")
        XCTAssertTrue(waitFor(timeout: 5) { downloads.count == 1 })
        XCTAssertEqual(downloads.first?.suggestedFilename, "fixture.txt")
        XCTAssertTrue(waitFor(timeout: 5) {
            (try? String(contentsOf: destination, encoding: .utf8)) == "fixture-download"
        })
    }

    func testSameOriginBinaryChildUsesDownloadPanel() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        let destination = FileManager.default.temporaryDirectory.appendingPathComponent("ciaobot-child-download-\(UUID().uuidString).txt")
        defer { try? FileManager.default.removeItem(at: destination) }
        var downloads: [DownloadRequest] = []
        let (coordinator, webView) = try makeHarness(origin: origin, downloadDestination: { request, completion in
            downloads.append(request)
            completion(destination)
        })
        webView.loadHTMLString(fixtureHTML("""
        <a id="binary" href="/download" target="_blank">binary</a>
        """), baseURL: origin.url)
        try waitForReady(webView)
        coordinator.noteTrustedActivationForTesting()
        webView.evaluateJavaScript("document.getElementById('binary').click()")
        // The hidden child closes as soon as its download finishes, so assert
        // the download path rather than requiring the transient child object
        // to still be in the list.
        XCTAssertTrue(waitFor(timeout: 5) { downloads.count == 1 })
        XCTAssertTrue(server.waitForRequest("/download", timeout: 3))
        XCTAssertEqual(downloads.first?.suggestedFilename, "fixture.txt")
        XCTAssertTrue(waitFor(timeout: 5) {
            (try? String(contentsOf: destination, encoding: .utf8)) == "fixture-download"
        })
    }

    func testGestureBridgeIsInstalled() throws {
        let origin = Origin("https://example.com")!
        let (_, webView) = try makeHarness(origin: origin)
        webView.loadHTMLString(fixtureHTML("<p>bridge</p>"), baseURL: origin.url)
        try waitForReady(webView)
        XCTAssertTrue(jsBoolean(webView, "window.__CIAOBOT_TRUSTED_CLICK_INSTALLED__ === true"))
        XCTAssertTrue(jsBoolean(webView, "!!window.webkit.messageHandlers.ciaobotTrustedClick"))
    }

    /// WebKit paints a scroll container's scrollbar from the page's used
    /// `color-scheme`, so a dark page that declares none gets the light system
    /// track inside a dark window. The client's stylesheet must therefore set
    /// `color-scheme` from the page's own `theme-light` class while leaving
    /// scrollbar width and colour to the page.
    func testClientChromeFollowsPageTheme() throws {
        let origin = Origin("https://example.com")!
        let (_, webView) = try makeHarness(origin: origin)
        webView.loadHTMLString(fixtureHTML("""
        <style>
          :root { --bg: #1a1a2e; background: var(--bg); }
          :root.theme-light { --bg: #f4f4fa; }
          .scroller { height: 120px; overflow-y: auto; }
          .content { height: 1200px; }
        </style>
        <div class="scroller"><div class="content">nested scroller</div></div>
        <section class="home-setup" id="card">
          <ol class="home-setup-steps">
            <li class="home-setup-step" id="install">
              <div class="home-setup-text"><span class="home-setup-label">Install the app</span></div>
              <a class="btn-small home-setup-action" href="/settings#install-app">How to install</a>
            </li>
            <li class="home-setup-step" id="phone"><details><summary>Use on your phone</summary></details></li>
          </ol>
        </section>
        <section class="home-setup" id="promptable">
          <ol class="home-setup-steps">
            <li class="home-setup-step" id="promptable-step">
              <button class="btn-small btn-primary" type="button">Install</button>
            </li>
          </ol>
        </section>
        """), baseURL: origin.url)
        try waitForReady(webView)

        XCTAssertTrue(jsBoolean(webView, "!!document.getElementById('__ciaobot_client_style__')"),
                      "The client stylesheet was not installed.")
        let style = try XCTUnwrap(jsString(
            webView,
            "document.getElementById('__ciaobot_client_style__').textContent"
        ))
        XCTAssertTrue(style.contains("color-scheme: dark"),
                      "The client did not opt the page into the dark color scheme.")
        XCTAssertTrue(style.contains("html:root.theme-light"),
                      "The color scheme must follow the page's theme class.")
        XCTAssertFalse(style.contains("scrollbar-color:"))
        XCTAssertFalse(style.contains("scrollbar-width:"))
        XCTAssertFalse(style.contains("::-webkit-scrollbar"),
                       "WKWebView does not repaint native scrollers from ::-webkit-scrollbar.")

        // The page's own theme contract drives the color scheme. A nested
        // scroller inherits it, so the client does not restyle every pane from
        // native code.
        XCTAssertEqual(jsString(webView, "getComputedStyle(document.documentElement).colorScheme"), "dark")
        XCTAssertEqual(jsString(webView, "getComputedStyle(document.querySelector('.scroller')).colorScheme"), "dark")
        webView.evaluateJavaScript("document.documentElement.classList.add('theme-light')")
        XCTAssertTrue(waitFor(timeout: 2) {
            self.jsString(webView, "getComputedStyle(document.documentElement).colorScheme") == "light"
        }, "The client did not switch the color scheme with the page's light theme.")
        XCTAssertEqual(jsString(webView, "getComputedStyle(document.querySelector('.scroller')).colorScheme"), "light")

        // The browser-install form of the setup card is hidden in this client;
        // a card that still offers a real install prompt (no settings link)
        // stays, as does every other Home card.
        XCTAssertEqual(jsString(webView, "getComputedStyle(document.getElementById('card')).display"), "none")
        XCTAssertNotEqual(jsString(webView, "getComputedStyle(document.getElementById('promptable')).display"), "none")
    }

    func testSameOriginChildUsesSessionCookieStore() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        let (coordinator, webView) = try makeHarness(origin: origin, openExternal: { _ in })
        // Set the cookie from a real same-origin server response and use the
        // real target=_blank child path so this tests cookie propagation, not a
        // test-only injection.
        webView.load(URLRequest(url: origin.url.appendingPathComponent("cookie-page")))
        try waitForReady(webView)
        coordinator.noteTrustedActivationForTesting()
        webView.evaluateJavaScript("document.getElementById('child').click()")
        XCTAssertTrue(waitFor(timeout: 3) { coordinator.children.count == 1 })
        let child = try XCTUnwrap(coordinator.children.first)
        XCTAssertTrue(child.webView.configuration.websiteDataStore === webView.configuration.websiteDataStore)
        XCTAssertFalse(child.webView.configuration.websiteDataStore === WKWebsiteDataStore.default())
        XCTAssertTrue(server.waitForRequest("/child", timeout: 8))
        XCTAssertEqual(server.requests(for: "/child").first?.cookie, "session=secret")
    }

    func testActualWindowOpenPreservesOpenerAndProvidedStore() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        server.setPage("/popup-opener", body: """
        <!doctype html><script>
          window.tab = window.open('/opener-child', '_blank');
          window.__fixtureReady = true;
        </script>
        """)
        let (coordinator, webView) = try makeHarness(origin: origin, javaScriptCanOpenWindowsAutomatically: true)
        coordinator.noteTrustedActivationForTesting()
        webView.load(URLRequest(url: origin.url.appendingPathComponent("popup-opener")))
        try waitForReady(webView)

        XCTAssertTrue(waitFor(timeout: 3) { coordinator.children.count == 1 })
        let child = try XCTUnwrap(coordinator.children.first)
        XCTAssertTrue(child.webView.configuration.websiteDataStore === webView.configuration.websiteDataStore)
        XCTAssertFalse(jsBoolean(child.webView, "!!window.webkit.messageHandlers.ciaobotTrustedClick"))
        XCTAssertTrue(jsBoolean(webView, "window.tab != null"))
        XCTAssertTrue(waitFor(timeout: 3) { server.waitForRequest("/opener-seen", timeout: 0.1) })
        XCTAssertTrue(server.requests(for: "/opener-seen").contains { $0.target.contains("opener=1") },
                      "The child did not retain window.opener. Seen: \(server.observed.map(\.target))")
    }

    func testContentChildScriptedExternalNavigationIsBlocked() throws {
        let server = try makeServer()
        let external = try makeServer()
        let origin = Origin(server.origin)!
        var opened: [URL] = []
        let (coordinator, webView) = try makeHarness(origin: origin, openExternal: { opened.append($0) })
        let childView = try XCTUnwrap(coordinator.newWindow(
            for: origin.url.appendingPathComponent("child"),
            userGesture: true,
            configuration: webView.configuration
        ))
        XCTAssertTrue(waitFor(timeout: 3) { jsBoolean(childView, "document.getElementById('child') !== null") })
        childView.evaluateJavaScript("location.href = '\(external.origin)/should-not-load'")
        XCTAssertTrue(waitFor(timeout: 1) { opened.isEmpty })
        XCTAssertTrue(external.observed.isEmpty)
    }

    func testContentChildCrossOriginRedirectIsBlocked() throws {
        let server = try makeServer()
        let external = try makeServer()
        let origin = Origin(server.origin)!
        server.setRedirect("/redirect", to: "\(external.origin)/should-not-load")
        var opened: [URL] = []
        let (coordinator, webView) = try makeHarness(origin: origin, openExternal: { opened.append($0) })
        let childView = try XCTUnwrap(coordinator.newWindow(
            for: origin.url.appendingPathComponent("redirect"),
            userGesture: true,
            configuration: webView.configuration
        ))
        XCTAssertTrue(waitFor(timeout: 2) { childView.url != nil })
        XCTAssertTrue(waitFor(timeout: 1) { opened.isEmpty })
        XCTAssertTrue(external.observed.isEmpty)
    }

    func testMainFrameCrossOriginRedirectIsCancelled() throws {
        let server = try makeServer()
        let external = try makeServer()
        let origin = Origin(server.origin)!
        server.setRedirect("/redirect", to: "\(external.origin)/should-not-load")
        var opened: [URL] = []
        var failures: [String] = []
        let (_, webView) = try makeHarness(
            origin: origin,
            onFailure: { failures.append($0) },
            openExternal: { opened.append($0) }
        )
        webView.load(URLRequest(url: origin.url.appendingPathComponent("redirect")))
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        XCTAssertTrue(opened.isEmpty)
        XCTAssertTrue(external.observed.isEmpty)
        XCTAssertTrue(failures.isEmpty, "Blocked redirect surfaced as failure: \(failures)")
    }

    func testDownloadCrossOriginRedirectIsCancelled() throws {
        let server = try makeServer()
        let external = try makeServer()
        let origin = Origin(server.origin)!
        server.setRedirect("/download-redirect", to: "\(external.origin)/download")
        var downloads: [DownloadRequest] = []
        let (_, webView) = try makeHarness(origin: origin, downloadDestination: { request, completion in
            downloads.append(request)
            completion(nil)
        })
        webView.loadHTMLString(fixtureHTML("""
        <a id="download" href="/download-redirect" download>download</a>
        """), baseURL: origin.url)
        try waitForReady(webView)
        webView.evaluateJavaScript("document.getElementById('download').click()")
        XCTAssertTrue(waitFor(timeout: 2) { downloads.isEmpty })
        XCTAssertTrue(external.observed.isEmpty)
    }

    func testDownloadSameOriginRedirectIsAllowed() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        server.setRedirect("/download-redirect-same", to: "\(server.origin)/download")
        let destination = FileManager.default.temporaryDirectory.appendingPathComponent("ciaobot-redirect-download-\(UUID().uuidString).txt")
        defer { try? FileManager.default.removeItem(at: destination) }
        var downloads: [DownloadRequest] = []
        let (_, webView) = try makeHarness(origin: origin, downloadDestination: { request, completion in
            downloads.append(request)
            completion(destination)
        })
        webView.loadHTMLString(fixtureHTML("""
        <a id="download" href="/download-redirect-same" download>download</a>
        """), baseURL: origin.url)
        try waitForReady(webView)
        webView.evaluateJavaScript("document.getElementById('download').click()")
        XCTAssertTrue(waitFor(timeout: 5) { downloads.count == 1 })
        XCTAssertTrue(waitFor(timeout: 5) {
            (try? String(contentsOf: destination, encoding: .utf8)) == "fixture-download"
        })
    }

    func testPolicyCancellationDoesNotSurfaceAsFailure() throws {
        let origin = Origin("https://example.com")!
        var failures: [String] = []
        let (coordinator, webView) = try makeHarness(origin: origin, onFailure: { failures.append($0) })

        coordinator.webView(webView, didFailProvisionalNavigation: nil,
                            withError: NSError(domain: "WebKitErrorDomain", code: 102))
        coordinator.webView(webView, didFail: nil,
                            withError: NSError(domain: NSURLErrorDomain, code: NSURLErrorCancelled))
        XCTAssertTrue(failures.isEmpty)

        coordinator.webView(webView, didFail: nil,
                            withError: NSError(domain: NSURLErrorDomain, code: NSURLErrorNotConnectedToInternet))
        XCTAssertEqual(failures.count, 1)
    }

    func testGenuineNavigationErrorReportsFailure() throws {
        // A closed ephemeral loopback port is refused by the kernel, so the
        // failure comes from the network rather than from a privileged port
        // the sandbox may reject before a connection is even attempted.
        let origin = Origin(try makeClosedLoopbackOrigin())!
        var failures: [String] = []
        let (_, webView) = try makeHarness(origin: origin, onFailure: { failures.append($0) })
        webView.load(URLRequest(url: origin.url))
        XCTAssertTrue(waitFor(timeout: 5) { !failures.isEmpty })
    }

    func testDownloadReplacesPreexistingFileSafely() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("ciaobot-replace-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let destination = directory.appendingPathComponent("out.txt")
        try "original".write(to: destination, atomically: true, encoding: .utf8)

        var attempts = 0
        var failures: [String] = []
        let (_, webView) = try makeHarness(
            origin: origin,
            downloadDestination: { _, completion in
                attempts += 1
                completion(destination)
            },
            downloadFailure: { failures.append($0) }
        )
        webView.loadHTMLString(fixtureHTML("""
        <a id="download" href="/download" download>download</a>
        """), baseURL: origin.url)
        try waitForReady(webView)
        webView.evaluateJavaScript("document.getElementById('download').click()")

        XCTAssertTrue(waitFor(timeout: 5) {
            (try? String(contentsOf: destination, encoding: .utf8)) == "fixture-download"
        })
        XCTAssertGreaterThan(attempts, 0)
        XCTAssertTrue(failures.isEmpty)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: directory.path), ["out.txt"])
    }

    func testDownloadFailurePreservesPreexistingFile() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("ciaobot-fail-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let destination = directory.appendingPathComponent("out.txt")
        try "original".write(to: destination, atomically: true, encoding: .utf8)

        var failures: [String] = []
        let (_, webView) = try makeHarness(
            origin: origin,
            downloadDestination: { _, completion in completion(destination) },
            downloadFailure: { failures.append($0) }
        )
        webView.loadHTMLString(fixtureHTML("""
        <a id="download" href="/download-truncated" download>download</a>
        """), baseURL: origin.url)
        try waitForReady(webView)
        webView.evaluateJavaScript("document.getElementById('download').click()")

        XCTAssertTrue(waitFor(timeout: 5) { !failures.isEmpty })
        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "original")
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: directory.path), ["out.txt"])
    }

    func testChildCreationDoesNotMutateMainPreferences() throws {
        let origin = Origin("https://example.com")!
        let (coordinator, webView) = try makeHarness(origin: origin, javaScriptCanOpenWindowsAutomatically: true)
        XCTAssertTrue(webView.configuration.preferences.javaScriptCanOpenWindowsAutomatically)
        let childView = try XCTUnwrap(coordinator.newWindow(
            for: URL(string: "about:blank")!,
            userGesture: true,
            configuration: webView.configuration
        ))
        XCTAssertTrue(webView.configuration.preferences.javaScriptCanOpenWindowsAutomatically)
        XCTAssertTrue(childView.configuration.preferences.javaScriptCanOpenWindowsAutomatically)
    }

    func testNilWindowOpenURLIsTreatedAsPlaceholder() throws {
        let origin = Origin("https://example.com")!
        let (coordinator, webView) = try makeHarness(origin: origin)
        let childView = try XCTUnwrap(coordinator.newWindow(
            for: nil,
            userGesture: true,
            configuration: webView.configuration
        ))
        let child = try XCTUnwrap(coordinator.children.first)
        XCTAssertEqual(child.mode, .oauthPlaceholder)
        XCTAssertTrue(childView.configuration.websiteDataStore === webView.configuration.websiteDataStore)
    }

    func testActualOAuthBlankWindowOpenButtonHandoff() throws {
        let server = try makeServer()
        let external = try makeServer()
        let origin = Origin(server.origin)!
        let authURL = external.url.appendingPathComponent("oauth")
        server.setPage("/oauth-button", body: """
        <!doctype html><button id="oauth" onclick="window.tab = window.open('', '_blank');">oauth</button>
        <script>window.__fixtureReady = true;</script>
        """)
        var opened: [URL] = []
        let (coordinator, webView) = try makeHarness(
            origin: origin,
            openExternal: { opened.append($0) },
            javaScriptCanOpenWindowsAutomatically: true
        )
        webView.load(URLRequest(url: origin.url.appendingPathComponent("oauth-button")))
        try waitForReady(webView)
        coordinator.noteTrustedActivationForTesting()
        webView.evaluateJavaScript("document.getElementById('oauth').click()")

        XCTAssertTrue(waitFor(timeout: 3) { coordinator.children.count == 1 })
        let child = try XCTUnwrap(coordinator.children.first)
        XCTAssertEqual(child.mode, .oauthPlaceholder)
        XCTAssertTrue(jsBoolean(webView, "window.tab != null"))
        webView.evaluateJavaScript("window.tab.location.href = '\(authURL.absoluteString)'")
        XCTAssertTrue(waitFor(timeout: 3) { opened.count == 1 })
        XCTAssertEqual(opened.first?.absoluteString, authURL.absoluteString)
        XCTAssertTrue(waitFor(timeout: 3) { coordinator.children.isEmpty })
        XCTAssertTrue(external.observed.isEmpty)
        XCTAssertTrue(child.webView.configuration.websiteDataStore === webView.configuration.websiteDataStore)
    }

    func testOAuthPlaceholderSameOriginNavigationIsHandedToBrowser() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        var opened: [URL] = []
        let (coordinator, webView) = try makeHarness(origin: origin, openExternal: { opened.append($0) })
        let child = try XCTUnwrap(coordinator.newWindow(
            for: nil,
            userGesture: true,
            configuration: webView.configuration
        ))
        let childController = try XCTUnwrap(coordinator.children.first)
        XCTAssertEqual(childController.mode, .oauthPlaceholder)

        let sameOriginURL = origin.url.appendingPathComponent("child")
        child.load(URLRequest(url: sameOriginURL))
        XCTAssertTrue(waitFor(timeout: 3) { opened.count == 1 })
        XCTAssertEqual(opened.first?.absoluteString, sameOriginURL.absoluteString)
        XCTAssertTrue(waitFor(timeout: 3) { coordinator.children.isEmpty })
        XCTAssertTrue(server.requests(for: "/child").isEmpty)
    }

    #if DEBUG
    func testCrossOriginUploadFrameIsRejected() throws {
        let server = try makeServer()
        let external = try makeServer()
        let origin = Origin(server.origin)!
        server.setPage("/upload-page", body: """
        <!doctype html><iframe src="\(external.origin)/upload-frame"></iframe>
        <script>window.__fixtureReady = true;</script>
        """)
        var filePanels = 0
        var crossOriginFrame: WKFrameInfo?
        let (coordinator, webView) = try makeHarness(origin: origin, filePanel: { _, completion in
            filePanels += 1
            completion(nil)
        })
        coordinator.testAllowsCrossOriginFrames = true
        coordinator.testCrossOriginFrameObserver = { frame in
            if !frame.isMainFrame { crossOriginFrame = frame }
        }
        webView.load(URLRequest(url: origin.url.appendingPathComponent("upload-page")))
        try waitForReady(webView)
        XCTAssertTrue(external.waitForRequest("/upload-frame", timeout: 3))
        XCTAssertTrue(waitFor(timeout: 2) { crossOriginFrame != nil })
        let frame = try XCTUnwrap(crossOriginFrame)
        XCTAssertFalse(coordinator.filePanelAllowed(for: frame),
                       "frame origin \(frame.securityOrigin.protocol)://\(frame.securityOrigin.host):\(frame.securityOrigin.port) vs \(origin.url); external \(external.origin)")
        XCTAssertEqual(filePanels, 0)
    }

    #endif

    func testBlockedInitialChildNavigationClosesUnshownChild() throws {
        let server = try makeServer()
        let external = try makeServer()
        let origin = Origin(server.origin)!
        server.setRedirect("/redirect", to: "\(external.origin)/should-not-load")
        let (coordinator, webView) = try makeHarness(origin: origin, openExternal: { _ in })

        _ = try XCTUnwrap(coordinator.newWindow(
            for: origin.url.appendingPathComponent("redirect"),
            userGesture: true,
            configuration: webView.configuration
        ))
        let child = try XCTUnwrap(coordinator.children.first)
        XCTAssertFalse(child.hasWindow, "A child that never commits must not create a window")
        XCTAssertTrue(waitFor(timeout: 2) { child.isClosed },
                      "A child whose first navigation was blocked stayed alive without ever being shown")
        XCTAssertTrue(waitFor(timeout: 1) { coordinator.children.isEmpty })
        XCTAssertTrue(external.observed.isEmpty)
    }

    func testShownChildStaysOpenAfterBlockedScriptedNavigation() throws {
        let server = try makeServer()
        let external = try makeServer()
        let origin = Origin(server.origin)!
        let (coordinator, webView) = try makeHarness(origin: origin, openExternal: { _ in })
        let childView = try XCTUnwrap(coordinator.newWindow(
            for: origin.url.appendingPathComponent("child"),
            userGesture: true,
            configuration: webView.configuration
        ))
        XCTAssertTrue(waitFor(timeout: 3) { jsBoolean(childView, "document.getElementById('child') !== null") })
        let child = try XCTUnwrap(coordinator.children.first)
        XCTAssertTrue(child.hasWindow, "A committed child is shown")

        childView.evaluateJavaScript("location.href = '\(external.origin)/should-not-load'")
        RunLoop.main.run(until: Date().addingTimeInterval(1))

        XCTAssertEqual(coordinator.children.count, 1, "A displayed child must survive a later blocked navigation")
        XCTAssertFalse(child.isClosed)
        XCTAssertTrue(external.observed.isEmpty)
    }

    #if DEBUG
    func testWindowOpenChildIsNavigatedOnce() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        server.setPage("/single-navigation", body: """
        <!doctype html><script>window.__fixtureReady = true;</script>
        """)
        let (coordinator, webView) = try makeHarness(origin: origin, javaScriptCanOpenWindowsAutomatically: true)
        coordinator.noteTrustedActivationForTesting()
        webView.load(URLRequest(url: origin.url.appendingPathComponent("single-navigation")))
        try waitForReady(webView)

        webView.evaluateJavaScript("window.open('/child','_blank')")
        XCTAssertTrue(waitFor(timeout: 3) { coordinator.children.count == 1 })
        let child = try XCTUnwrap(coordinator.children.first)
        XCTAssertTrue(waitFor(timeout: 3) { child.webView.url?.path == "/child" })
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))

        XCTAssertEqual(child.testNavigationDecisionCount, 1,
                       "WebKit loads a returned child itself; the app must not start a second navigation")
    }
    #endif

    func testUnshownChildClosesWhenItNeverDisplays() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        let (coordinator, webView) = try makeHarness(origin: origin)
        coordinator.unshownChildTimeout = 0.3

        _ = try XCTUnwrap(coordinator.newWindow(
            for: nil,
            userGesture: true,
            configuration: webView.configuration
        ))
        let child = try XCTUnwrap(coordinator.children.first)
        XCTAssertEqual(child.mode, .oauthPlaceholder)
        XCTAssertFalse(child.hasWindow)

        XCTAssertTrue(waitFor(timeout: 2) { child.isClosed },
                      "A child that never shows a window must not live forever")
        XCTAssertTrue(waitFor(timeout: 1) { coordinator.children.isEmpty })
    }

    func testPendingDownloadKeepsUnshownChildAliveUntilItFinishes() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        let destination = FileManager.default.temporaryDirectory
            .appendingPathComponent("ciaobot-watchdog-download-\(UUID().uuidString).txt")
        defer { try? FileManager.default.removeItem(at: destination) }
        var pending: ((URL?) -> Void)?
        let (coordinator, webView) = try makeHarness(origin: origin, downloadDestination: { _, completion in
            pending = completion
        })
        coordinator.unshownChildTimeout = 0.3

        webView.loadHTMLString(fixtureHTML("""
        <a id="binary" href="/download" target="_blank">binary</a>
        """), baseURL: origin.url)
        try waitForReady(webView)
        coordinator.noteTrustedActivationForTesting()
        webView.evaluateJavaScript("document.getElementById('binary').click()")

        XCTAssertTrue(waitFor(timeout: 3) { pending != nil }, "The download did not reach the save destination")
        let child = try XCTUnwrap(coordinator.children.first)
        RunLoop.main.run(until: Date().addingTimeInterval(0.7))
        XCTAssertFalse(child.isClosed,
                       "A pending save panel outlives the unshown-child timeout without cancelling the download")

        pending?(destination)
        XCTAssertTrue(waitFor(timeout: 5) {
            (try? String(contentsOf: destination, encoding: .utf8)) == "fixture-download"
        })
        XCTAssertTrue(waitFor(timeout: 3) { child.isClosed }, "The child closes once the download completes")
    }

    func testCancelledSavePanelClosesUnshownDownloadChild() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        var pending: ((URL?) -> Void)?
        var failures: [String] = []
        let (coordinator, webView) = try makeHarness(
            origin: origin,
            downloadDestination: { _, completion in pending = completion },
            downloadFailure: { failures.append($0) }
        )
        webView.loadHTMLString(fixtureHTML("""
        <a id="binary" href="/download" target="_blank">binary</a>
        """), baseURL: origin.url)
        try waitForReady(webView)
        coordinator.noteTrustedActivationForTesting()
        webView.evaluateJavaScript("document.getElementById('binary').click()")

        XCTAssertTrue(waitFor(timeout: 3) { pending != nil }, "The save panel was never requested")
        let child = try XCTUnwrap(coordinator.children.first)
        XCTAssertFalse(child.hasWindow)

        pending?(nil)
        XCTAssertTrue(waitFor(timeout: 2) { child.isClosed },
                      "Cancelling the save panel left the never-shown download child retained")
        XCTAssertTrue(waitFor(timeout: 1) { coordinator.children.isEmpty })
        XCTAssertTrue(failures.isEmpty, "Cancelling a save panel must not show a download failure: \(failures)")
    }

    func testClosingChildDuringDownloadRemovesTemporaryFile() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        server.setStall("/download-slow", seconds: 2.0)
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("ciaobot-inprogress-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let destination = directory.appendingPathComponent("out.txt")
        var failures: [String] = []

        let (coordinator, webView) = try makeHarness(
            origin: origin,
            downloadDestination: { _, completion in completion(destination) },
            downloadFailure: { failures.append($0) }
        )
        webView.loadHTMLString(fixtureHTML("""
        <a id="binary" href="/download-slow" target="_blank">binary</a>
        """), baseURL: origin.url)
        try waitForReady(webView)
        coordinator.noteTrustedActivationForTesting()
        webView.evaluateJavaScript("document.getElementById('binary').click()")

        let temporaryFiles: () -> [String] = {
            (try? FileManager.default.contentsOfDirectory(atPath: directory.path))?
                .filter { $0.hasPrefix(".ciaobot-") } ?? []
        }
        XCTAssertTrue(waitFor(timeout: 5) { !temporaryFiles().isEmpty },
                      "The download never started writing a temporary file")
        var child: ChildWindowController? = try XCTUnwrap(coordinator.children.first)
        weak var releasedChild = child
        let temporary = directory.appendingPathComponent(try XCTUnwrap(temporaryFiles().first))
        XCTAssertTrue(FileManager.default.fileExists(atPath: temporary.path))

        child?.close()
        child = nil
        let wasReleased = waitFor(timeout: 2) { releasedChild == nil }

        XCTAssertTrue(waitFor(timeout: 6) { !FileManager.default.fileExists(atPath: temporary.path) },
                      "Closing the child left the in-progress download's temporary file on disk (child released: \(wasReleased))")
        // Let the stalled response finish failing: the abandoned download must
        // neither recreate its temporary file nor alert from a closed window.
        RunLoop.main.run(until: Date().addingTimeInterval(2.5))
        XCTAssertFalse(FileManager.default.fileExists(atPath: temporary.path),
                       "The abandoned temporary file reappeared after the download ended (child released: \(wasReleased))")
        XCTAssertFalse(FileManager.default.fileExists(atPath: destination.path),
                       "A closed child must not install a partial download")
        XCTAssertTrue(failures.isEmpty, "An abandoned download must not alert: \(failures)")
    }

    func testWebKitDrivenNavigationAcceptsOnlyPlainGET() {
        let origin = Origin("https://example.com")!
        var get = URLRequest(url: origin.url.appendingPathComponent("child"))
        get.httpMethod = "GET"
        XCTAssertTrue(WebWindow.Coordinator.webKitCanDrive(get, navigationType: .other))
        XCTAssertTrue(WebWindow.Coordinator.webKitCanDrive(get, navigationType: .linkActivated))

        var post = URLRequest(url: origin.url.appendingPathComponent("child"))
        post.httpMethod = "POST"
        XCTAssertFalse(WebWindow.Coordinator.webKitCanDrive(post, navigationType: .other),
                       "a POST must never be replayed as a GET in a child")
        XCTAssertFalse(WebWindow.Coordinator.webKitCanDrive(post, navigationType: .formSubmitted))
        XCTAssertFalse(WebWindow.Coordinator.webKitCanDrive(get, navigationType: .formSubmitted),
                       "a form submission must not be replayed as a GET")

        var blank = URLRequest(url: URL(string: "about:blank")!)
        blank.httpMethod = "GET"
        XCTAssertTrue(WebWindow.Coordinator.webKitCanDrive(blank, navigationType: .other),
                      "the blank OAuth placeholder must still be created")
    }

    func testPOSTFormTargetBlankCreatesNoChildAndIssuesNoRequest() throws {
        let server = try makeServer()
        let origin = Origin(server.origin)!
        server.setPage("/post-page", body: """
        <!doctype html>
        <form id="f" action="/submit" method="post" target="_blank"><button id="go">go</button></form>
        <script>window.__fixtureReady = true;</script>
        """)
        let (coordinator, webView) = try makeHarness(origin: origin)
        webView.load(URLRequest(url: origin.url.appendingPathComponent("post-page")))
        try waitForReady(webView)
        coordinator.noteTrustedActivationForTesting()

        webView.evaluateJavaScript("document.getElementById('go').click()")
        RunLoop.main.run(until: Date().addingTimeInterval(1))

        XCTAssertTrue(coordinator.children.isEmpty, "A POST form target=_blank must not become a child window")
        XCTAssertTrue(server.requests(for: "/submit").isEmpty,
                      "A refused POST form must not be replayed as a request")
    }

    private func makeHarness(
        origin: Origin,
        onFailure: ((String) -> Void)? = nil,
        filePanel: @escaping FilePanelHandler = { _, completion in completion(nil) },
        downloadDestination: @escaping DownloadDestination = { _, completion in completion(nil) },
        downloadFailure: @escaping DownloadFailureHandler = { _ in },
        openExternal: @escaping ExternalOpener = { _ in },
        javaScriptCanOpenWindowsAutomatically: Bool = false
    ) throws -> (WebWindow.Coordinator, WKWebView) {
        let representable = WebWindow(
            origin: origin,
            onFailure: onFailure,
            downloadFailure: downloadFailure,
            filePanel: filePanel,
            downloadDestination: downloadDestination,
            openExternal: openExternal
        )
        let coordinator = representable.makeCoordinator()
        let webView = WebWindow.Coordinator.makeWebView(
            origin: origin,
            coordinator: coordinator,
            javaScriptCanOpenWindowsAutomatically: javaScriptCanOpenWindowsAutomatically
        )
        webView.frame = NSRect(x: 0, y: 0, width: 700, height: 500)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 700, height: 500), styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = webView
        NSApplication.shared.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
        Self.retainedWindows.append(window)
        Self.retainedCoordinators.append(coordinator)
        return (coordinator, webView)
    }

    private func makeServer() throws -> WebKitTestServer {
        let server = try WebKitTestServer()
        servers.append(server)
        return server
    }

    /// Returns a loopback origin whose port has no listener, so a connection to
    /// it is refused by the kernel with a genuine network error. Port 0 asks the
    /// kernel for a free ephemeral port; the socket is never put into listen
    /// state and is closed on return, which keeps the test clear of both a
    /// privileged port (refused by policy before any connection) and a fixed
    /// port another process might already own.
    private func makeClosedLoopbackOrigin() throws -> String {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw makeWebKitError("socket") }
        defer { close(fd) }

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
        guard bound == 0 else { throw makeWebKitError("bind") }

        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(fd, $0, &length)
            }
        }
        guard named == 0 else { throw makeWebKitError("getsockname") }
        return "http://127.0.0.1:\(Int(UInt16(bigEndian: address.sin_port)))"
    }

    private func makeTempFile(_ name: String, _ contents: String) throws -> URL {
        try makeTempFile(in: FileManager.default.temporaryDirectory, name: name, contents: contents)
    }

    private func makeTempFile(in directory: URL, name: String, contents: String) throws -> URL {
        let url = directory.appendingPathComponent(name)
        try contents.write(to: url, atomically: true, encoding: .utf8)
        return url
    }

    private func fixtureHTML(_ body: String) -> String {
        "<!doctype html><html><body>\(body)<script>window.__fixtureReady = true;</script></body></html>"
    }

    private func waitForReady(_ webView: WKWebView, timeout: TimeInterval = 5) throws {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if jsBoolean(webView, "document.readyState === 'complete' && window.__fixtureReady === true") { return }
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
        XCTFail("The page did not become ready")
        throw makeWebKitError("page not ready")
    }

    private func waitFor(timeout: TimeInterval, _ condition: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
        return condition()
    }

    private func jsString(_ webView: WKWebView, _ script: String) -> String? {
        var result: String?
        var done = false
        webView.evaluateJavaScript(script) { value, _ in
            result = value as? String
            done = true
        }
        let deadline = Date().addingTimeInterval(2)
        while !done && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.01))
        }
        return result
    }

    private func jsBoolean(_ webView: WKWebView, _ script: String) -> Bool {
        var result = false
        var done = false
        webView.evaluateJavaScript(script) { value, _ in
            result = (value as? Bool) == true
            done = true
        }
        let deadline = Date().addingTimeInterval(2)
        while !done && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.01))
        }
        return result
    }
}
