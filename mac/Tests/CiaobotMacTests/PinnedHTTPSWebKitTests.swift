import AppKit
import Foundation
import Network
import Security
import WebKit
import XCTest
import CryptoKit
@testable import CiaobotMac

/// Isolated TLS endpoint for the WebKit trust proof. The certificate is a
/// short-lived self-signed fixture; the test delegate accepts only its exact
/// leaf bytes for the selected loopback host, never normal system trust.
private final class PinnedHTTPSFixture {
    struct Request {
        let path: String
        let cookie: String?
    }

    private(set) var port: UInt16 = 0
    let certificate: Data
    let identity: SecIdentity
    private let listener: NWListener
    private let queue = DispatchQueue(label: "CiaobotMacTests.PinnedHTTPSFixture")
    private let lock = NSLock()
    private var requests: [Request] = []
    private var pages: [String: String] = [:]
    private var pageHeaders: [String: [String: String]] = [:]
    private var redirects: [String: String] = [:]

    init(directory: URL, identity suppliedIdentity: SecIdentity? = nil) throws {
        let identity = try suppliedIdentity ?? Self.makeIdentity(directory: directory)
        self.identity = identity
        var certificateRef: SecCertificate?
        guard SecIdentityCopyCertificate(identity, &certificateRef) == errSecSuccess,
              let certificateRef else {
            throw Self.error("read fixture certificate")
        }
        certificate = SecCertificateCopyData(certificateRef) as Data

        let tls = NWProtocolTLS.Options()
        guard let networkIdentity = sec_identity_create(identity) else {
            throw Self.error("create Network.framework identity")
        }
        sec_protocol_options_set_local_identity(tls.securityProtocolOptions, networkIdentity)
        let parameters = NWParameters(tls: tls, tcp: NWProtocolTCP.Options())
        listener = try NWListener(using: parameters, on: .any)
        let ready = DispatchSemaphore(value: 0)
        listener.stateUpdateHandler = { state in
            if case .ready = state { ready.signal() }
        }
        listener.newConnectionHandler = { [weak self] connection in
            guard let self else { connection.cancel(); return }
            connection.start(queue: self.queue)
            self.receive(connection, data: Data())
        }
        listener.start(queue: queue)
        guard ready.wait(timeout: .now() + 5) == .success,
              let assignedPort = listener.port?.rawValue else {
            listener.cancel()
            throw Self.error("start TLS fixture listener")
        }
        port = assignedPort
    }

    deinit { listener.cancel() }

    var origin: String { "https://127.0.0.1:\(port)" }

    var observed: [Request] {
        lock.lock()
        defer { lock.unlock() }
        return requests
    }

    func requests(for path: String) -> [Request] { observed.filter { $0.path == path } }

    func setPage(_ path: String, body: String, headers: [String: String] = [:]) {
        lock.lock()
        pages[path] = body
        pageHeaders[path] = headers
        lock.unlock()
    }

    func setRedirect(_ path: String, to destination: String) {
        lock.lock()
        redirects[path] = destination
        lock.unlock()
    }

    func waitForRequest(_ path: String, timeout: TimeInterval = 5) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if requests(for: path).isEmpty == false { return true }
            RunLoop.main.run(until: Date().addingTimeInterval(0.01))
        }
        return !requests(for: path).isEmpty
    }

    private func receive(_ connection: NWConnection, data: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 16 * 1024) { [weak self] chunk, _, complete, error in
            guard let self else { connection.cancel(); return }
            var bytes = data
            if let chunk { bytes.append(chunk) }
            if bytes.range(of: Data("\r\n\r\n".utf8)) == nil, !complete, error == nil, bytes.count < 64 * 1024 {
                self.receive(connection, data: bytes)
                return
            }
            self.respond(connection, request: bytes)
        }
    }

    private func respond(_ connection: NWConnection, request data: Data) {
        let text = String(decoding: data, as: UTF8.self)
        let lines = text.components(separatedBy: "\r\n")
        guard let requestLine = lines.first,
              requestLine.hasPrefix("GET "),
              let target = requestLine.split(separator: " ").dropFirst().first.map(String.init) else {
            // A rejected TLS handshake can close before a decrypted HTTP
            // request exists. Such bytes are not an application request and
            // must not be counted as one by this fixture.
            connection.cancel()
            return
        }
        let path = target.split(separator: "?", maxSplits: 1).first.map(String.init) ?? target
        let cookie = lines.first { $0.lowercased().hasPrefix("cookie:") }
            .map { String($0.dropFirst("cookie:".count)).trimmingCharacters(in: .whitespaces) }

        lock.lock()
        requests.append(Request(path: path, cookie: cookie))
        let page = pages[path]
        let headers = pageHeaders[path] ?? [:]
        let redirect = redirects[path]
        lock.unlock()

        let response: String
        if let redirect {
            response = "HTTP/1.1 302 Found\r\nLocation: \(redirect)\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        } else if path == "/cookie" {
            let body = "<!doctype html><script>fetch('/same-origin').catch(()=>{});</script>"
            response = Self.response(body: body, extra: ["Set-Cookie": "ciaobot_session=secret; Secure; HttpOnly; SameSite=Lax; Path=/"])
        } else if path == "/login" {
            response = Self.response(body: "<!doctype html><script>fetch('/same-origin').catch(()=>{});</script>",
                                     extra: ["Set-Cookie": "ciao_session=secret; Secure; HttpOnly; SameSite=Lax; Path=/"])
        } else if path == "/logout" {
            response = Self.response(body: "signed out", extra: ["Set-Cookie": "ciao_session=; Max-Age=0; Secure; HttpOnly; SameSite=Lax; Path=/"])
        } else {
            response = Self.response(body: page ?? "ok", extra: headers)
        }
        connection.send(content: Data(response.utf8), completion: .contentProcessed { _ in connection.cancel() })
    }

    private static func response(body: String, extra: [String: String] = [:]) -> String {
        var headers = ["Content-Type": "text/html; charset=utf-8", "Content-Length": String(body.utf8.count), "Cache-Control": "no-store", "Connection": "close"]
        headers.merge(extra) { _, new in new }
        let rawHeaders = headers.map { "\($0.key): \($0.value)" }.joined(separator: "\r\n")
        return "HTTP/1.1 200 OK\r\n\(rawHeaders)\r\n\r\n\(body)"
    }

    private static func makeIdentity(directory: URL) throws -> SecIdentity {
        guard #available(macOS 15.0, *) else {
            throw XCTSkip("The fixture imports its temporary TLS identity in memory, which requires macOS 15")
        }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let key = directory.appendingPathComponent("key.pem")
        let cert = directory.appendingPathComponent("cert.pem")
        let p12 = directory.appendingPathComponent("identity.p12")
        try runOpenSSL([
            "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", key.path, "-out", cert.path, "-days", "2",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"
        ])
        try runOpenSSL([
            "pkcs12", "-export", "-in", cert.path, "-inkey", key.path,
            "-out", p12.path, "-passout", "pass:fixture"
        ])
        var imported: CFArray?
        let importStatus = SecPKCS12Import(
            try Data(contentsOf: p12) as CFData,
            [
                kSecImportExportPassphrase as String: "fixture",
                kSecImportToMemoryOnly as String: kCFBooleanTrue as Any
            ] as CFDictionary,
            &imported
        )
        guard importStatus == errSecSuccess,
              let item = (imported as? [[String: Any]])?.first,
              let identityObject = item[kSecImportItemIdentity as String] else {
            throw error("import fixture identity")
        }
        return identityObject as! SecIdentity
    }

    private static func runOpenSSL(_ arguments: [String]) throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/openssl")
        process.arguments = arguments
        let errors = Pipe()
        process.standardError = errors
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            let data = errors.fileHandleForReading.readDataToEndOfFile()
            throw NSError(domain: "PinnedHTTPSFixture", code: Int(process.terminationStatus), userInfo: [NSLocalizedDescriptionKey: String(decoding: data, as: UTF8.self)])
        }
    }

    private static func error(_ operation: String) -> NSError {
        NSError(domain: "PinnedHTTPSFixture", code: 1, userInfo: [NSLocalizedDescriptionKey: operation])
    }
}

/// Plain HTTP listener used to determine whether WebKit's loopback Secure
/// cookie exception sends the session cookie outside the pinned TLS listener.
private final class LoopbackHTTPProbe {
    struct Request { let path: String; let cookie: String? }
    private let listener: NWListener
    private let queue = DispatchQueue(label: "CiaobotMacTests.LoopbackHTTPProbe")
    private let lock = NSLock()
    private var requests: [Request] = []
    private var pages: [String: String] = [:]
    private var redirects: [String: String] = [:]
    private(set) var port: UInt16 = 0

    init() throws {
        listener = try NWListener(using: .tcp, on: .any)
        let ready = DispatchSemaphore(value: 0)
        listener.stateUpdateHandler = { if case .ready = $0 { ready.signal() } }
        listener.newConnectionHandler = { [weak self] connection in
            guard let self else { connection.cancel(); return }
            connection.start(queue: self.queue)
            connection.receive(minimumIncompleteLength: 1, maximumLength: 16 * 1024) { data, _, _, _ in
                guard let data else { connection.cancel(); return }
                let lines = String(decoding: data, as: UTF8.self).components(separatedBy: "\r\n")
                guard let first = lines.first, first.hasPrefix("GET "),
                      let target = first.split(separator: " ").dropFirst().first else { connection.cancel(); return }
                let cookie = lines.first { $0.lowercased().hasPrefix("cookie:") }
                    .map { String($0.dropFirst("cookie:".count)).trimmingCharacters(in: .whitespaces) }
                let path = String(target).split(separator: "?").first.map(String.init) ?? String(target)
                self.lock.lock()
                self.requests.append(Request(path: path, cookie: cookie))
                let body = self.pages[path]
                let redirect = self.redirects[path]
                self.lock.unlock()
                let response: String
                if let redirect {
                    response = "HTTP/1.1 302 Found\r\nLocation: \(redirect)\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                } else {
                    let body = body ?? "ok"
                    response = "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-store\r\nContent-Length: \(body.utf8.count)\r\nConnection: close\r\n\r\n\(body)"
                }
                connection.send(content: Data(response.utf8), completion: .contentProcessed { _ in connection.cancel() })
            }
        }
        listener.start(queue: queue)
        guard ready.wait(timeout: .now() + 5) == .success, let port = listener.port?.rawValue else {
            listener.cancel()
            throw NSError(domain: "LoopbackHTTPProbe", code: 1)
        }
        self.port = port
    }

    deinit { listener.cancel() }
    var origin: String { "http://127.0.0.1:\(port)" }
    var observed: [Request] { lock.lock(); defer { lock.unlock() }; return requests }

    func setPage(_ path: String, body: String) {
        lock.lock(); pages[path] = body; lock.unlock()
    }

    func setRedirect(_ path: String, to destination: String) {
        lock.lock(); redirects[path] = destination; lock.unlock()
    }

    func resetRequests() { lock.lock(); requests.removeAll(); lock.unlock() }

    func waitForPath(_ path: String, timeout: TimeInterval = 5) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if observed.contains(where: { $0.path == path }) { return true }
            RunLoop.main.run(until: Date().addingTimeInterval(0.01))
        }
        return observed.contains(where: { $0.path == path })
    }
}

@MainActor
private final class PinningNavigationDelegate: NSObject, WKNavigationDelegate {
    let host: String
    let certificate: Data
    private(set) var challengeHosts: [String] = []
    private(set) var rejectedHosts: [String] = []
    private(set) var rejectedAfterTrustEvaluation = 0
    private(set) var navigationFailures: [String] = []

    init(host: String = "127.0.0.1", certificate: Data) {
        self.host = host
        self.certificate = certificate
    }

    static func accepts(host: String, scheme: String?, trust: SecTrust, pinnedCertificate: Data) -> Bool {
        guard scheme?.lowercased() == "https",
              host == "127.0.0.1",
              let leaf = (SecTrustCopyCertificateChain(trust) as? [SecCertificate])?.first else { return false }
        return (SecCertificateCopyData(leaf) as Data) == pinnedCertificate
    }

    func webView(
        _ webView: WKWebView,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        let protection = challenge.protectionSpace
        challengeHosts.append(protection.host)
        if protection.authenticationMethod == NSURLAuthenticationMethodServerTrust,
           let trust = protection.serverTrust,
           let leaf = (SecTrustCopyCertificateChain(trust) as? [SecCertificate])?.first,
           (SecCertificateCopyData(leaf) as Data) != certificate,
           Self.trustsAsExplicitAnchor(leaf) {
            // Model a certificate whose chain validates to an OS trust anchor:
            // the pin policy still rejects it because its leaf is not ours.
            rejectedAfterTrustEvaluation += 1
        }
        guard protection.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = protection.serverTrust,
              Self.accepts(host: protection.host, scheme: protection.protocol, trust: trust, pinnedCertificate: certificate) else {
            rejectedHosts.append(protection.host)
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        // The fixture pin is the only acceptance condition. In particular,
        // neither default handling nor a system-trusted but differently pinned
        // certificate can pass this branch.
        completionHandler(.useCredential, URLCredential(trust: trust))
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let value = error as NSError
        navigationFailures.append("\(value.domain):\(value.code) \(value.localizedDescription)")
    }

    private static func trustsAsExplicitAnchor(_ certificate: SecCertificate) -> Bool {
        var trust: SecTrust?
        guard SecTrustCreateWithCertificates(certificate, SecPolicyCreateBasicX509(), &trust) == errSecSuccess,
              let trust else { return false }
        guard SecTrustSetAnchorCertificates(trust, [certificate] as CFArray) == errSecSuccess,
              SecTrustSetAnchorCertificatesOnly(trust, true) == errSecSuccess else { return false }
        return SecTrustEvaluateWithError(trust, nil)
    }
}

@MainActor
final class PinnedHTTPSWebKitTests: XCTestCase {

    func testExternalTLSResourcesUsePlatformTrustWhileLoopbackStillRequiresPin() {
        XCTAssertTrue(LocalEngineTLSIdentity.usesPlatformTrust(authenticationMethod: NSURLAuthenticationMethodServerTrust,
                                                               host: "fonts.example"))
        XCTAssertFalse(LocalEngineTLSIdentity.usesPlatformTrust(authenticationMethod: NSURLAuthenticationMethodServerTrust,
                                                                host: "127.0.0.1"))
        XCTAssertFalse(LocalEngineTLSIdentity.usesPlatformTrust(authenticationMethod: NSURLAuthenticationMethodHTTPBasic,
                                                                host: "fonts.example"))
    }

    func testLateCallbacksFromSamePortPreviousGenerationAreRejected() throws {
        let origin = try XCTUnwrap(Origin("https://127.0.0.1:8778"))
        let old = try XCTUnwrap(LocalEngineTLSIdentity(pid: 501, startupID: "old", port: 8778,
                                                       sha256: String(repeating: "a", count: 64)))
        let current = try XCTUnwrap(LocalEngineTLSIdentity(pid: 501, startupID: "new", port: 8778,
                                                           sha256: String(repeating: "b", count: 64)))
        XCTAssertFalse(LocalEngineTLSIdentity.isCurrentWebView(expectedIdentity: old, currentIdentity: current,
                                                               expectedOrigin: origin, currentOrigin: origin),
                       "the same port cannot make a stale startup callback current")
        XCTAssertTrue(LocalEngineTLSIdentity.isCurrentWebView(expectedIdentity: current, currentIdentity: current,
                                                              expectedOrigin: origin, currentOrigin: origin))
        XCTAssertFalse(LocalEngineTLSIdentity.isCurrentWebView(expectedIdentity: nil, currentIdentity: nil,
                                                               expectedOrigin: origin, currentOrigin: origin),
                       "an unpinned local view is never treated as a current generation")
    }

    func testLateLocalFailureAfterExternalModeConnectionCannotReplaceIt() throws {
        let localOrigin = try XCTUnwrap(Origin("https://127.0.0.1:8778"))
        let externalOrigin = try XCTUnwrap(Origin("https://ciaobot.example"))
        let localIdentity = try XCTUnwrap(LocalEngineTLSIdentity(pid: 502, startupID: "old-local", port: 8778,
                                                                 sha256: String(repeating: "c", count: 64)))
        XCTAssertFalse(LocalEngineTLSIdentity.acceptsConnectionCallback(
            expectedMode: .local, currentMode: .external,
            expectedOrigin: localOrigin, currentOrigin: externalOrigin,
            expectedIdentity: localIdentity, currentIdentity: localIdentity
        ), "a delayed local failure must not clear a newly established external session")
        XCTAssertFalse(LocalEngineTLSIdentity.acceptsConnectionCallback(
            expectedMode: .local, currentMode: .local,
            expectedOrigin: localOrigin, currentOrigin: externalOrigin,
            expectedIdentity: localIdentity, currentIdentity: localIdentity
        ), "an old callback must not affect another connected origin")
        XCTAssertTrue(LocalEngineTLSIdentity.acceptsConnectionCallback(
            expectedMode: .external, currentMode: .external,
            expectedOrigin: externalOrigin, currentOrigin: externalOrigin,
            expectedIdentity: nil, currentIdentity: nil
        ))
    }

    func testCiaoSessionSurvivesReopenedPersistentStoreAndLogoutRemovesIt() async throws {
        guard #available(macOS 14.0, *) else { throw XCTSkip("Named persistent WebKit stores require macOS 14") }
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("PinnedLogin-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let fixture = try PinnedHTTPSFixture(directory: root)
        let origin = try XCTUnwrap(Origin(fixture.origin))
        let (registry, _) = try makeRegistry(in: root)
        let signIn = SessionSignIn(registry: registry, eraser: WebKitWebsiteDataStoreEraser(), persistentAvailable: true)
        guard case .persistent(let storeID) = signIn.storage(for: origin, keepSignedIn: true) else {
            return XCTFail("HTTPS sign-in opt-in did not allocate its persistent store")
        }
        let delegate = PinningNavigationDelegate(certificate: fixture.certificate)
        let first = makeWebView(delegate: delegate, dataStore: WKWebsiteDataStore(forIdentifier: storeID))
        first.load(URLRequest(url: URL(string: "\(fixture.origin)/login")!))
        let loginRequested = await waitForRequest(fixture, "/login")
        XCTAssertTrue(loginRequested)
        let loginCookieReturned = await waitForRequest(fixture, "/same-origin")
        XCTAssertTrue(loginCookieReturned)
        XCTAssertEqual(fixture.requests(for: "/same-origin").last?.cookie, "ciao_session=secret")
        first.stopLoading()
        first.navigationDelegate = nil

        // A fresh WebKit view reopens the named store, as it does after an app
        // relaunch. The engine's real session cookie must still authenticate.
        let reopened = makeWebView(delegate: delegate, dataStore: WKWebsiteDataStore(forIdentifier: storeID))
        reopened.load(URLRequest(url: URL(string: "\(fixture.origin)/same-origin")!))
        let reopenedRequest = await waitForRequestCount(fixture, "/same-origin", count: 2)
        XCTAssertTrue(reopenedRequest)
        XCTAssertEqual(fixture.requests(for: "/same-origin").last?.cookie, "ciao_session=secret")

        reopened.load(URLRequest(url: URL(string: "\(fixture.origin)/logout")!))
        let logoutRequested = await waitForRequest(fixture, "/logout")
        XCTAssertTrue(logoutRequested)
        reopened.load(URLRequest(url: URL(string: "\(fixture.origin)/same-origin")!))
        let afterLogout = await waitForRequestCount(fixture, "/same-origin", count: 3)
        XCTAssertTrue(afterLogout)
        XCTAssertNil(fixture.requests(for: "/same-origin").last?.cookie,
                     "logout left ciao_session in the persistent store")
        first.stopLoading()
        reopened.stopLoading()
        try? await WKWebsiteDataStore.remove(forIdentifier: storeID)
    }

    func testRestartBlockersUsesVerifiedPinnedHTTPSIdentity() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("PinnedBlockers-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let fixture = try PinnedHTTPSFixture(directory: root)
        fixture.setPage("/api/admin/restart-blockers", body: #"{"restart_blockers":[{"id":"one"},{"id":"two"}]}"#,
                        headers: ["Content-Type": "application/json"])
        let pin = SHA256.hash(data: fixture.certificate).map { String(format: "%02x", $0) }.joined()
        let identity = try XCTUnwrap(LocalEngineTLSIdentity(pid: 410, startupID: "blockers", port: Int(fixture.port), sha256: pin))
        let probe = RestartBlockersProbe()
        let fetched = expectation(description: "pinned blocker count")
        var count: Int?
        probe.activeChats(of: identity) { count = $0; fetched.fulfill() }
        wait(for: [fetched], timeout: 5)
        XCTAssertEqual(count, 2)
        XCTAssertEqual(fixture.requests(for: "/api/admin/restart-blockers").count, 1)

        let wrongPin = try XCTUnwrap(LocalEngineTLSIdentity(pid: 410, startupID: "blockers", port: Int(fixture.port),
                                                            sha256: String(repeating: "0", count: 64)))
        let rejected = expectation(description: "wrong blocker pin rejected")
        probe.activeChats(of: wrongPin) { value in
            XCTAssertNil(value)
            rejected.fulfill()
        }
        wait(for: [rejected], timeout: 5)
        XCTAssertEqual(fixture.requests(for: "/api/admin/restart-blockers").count, 1,
                       "a listener with the wrong leaf certificate receives no request")
    }

    func testPinnedDownloadAuthenticationRequiresExactHostPortAndLeaf() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("PinnedHTTPS-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: directory) }
        let fixture = try PinnedHTTPSFixture(directory: directory)
        let certificate = try XCTUnwrap(SecCertificateCreateWithData(nil, fixture.certificate as CFData))
        var trust: SecTrust?
        XCTAssertEqual(SecTrustCreateWithCertificates(certificate, SecPolicyCreateBasicX509(), &trust), errSecSuccess)
        let pin = SHA256.hash(data: fixture.certificate).map { String(format: "%02x", $0) }.joined()

        XCTAssertTrue(LocalEngineTLSIdentity.accepts(trust: trust, authenticationMethod: NSURLAuthenticationMethodServerTrust,
                                                     host: "127.0.0.1", port: Int(fixture.port), expectedPort: Int(fixture.port), pin: pin))
        XCTAssertFalse(LocalEngineTLSIdentity.accepts(trust: trust, authenticationMethod: NSURLAuthenticationMethodServerTrust,
                                                      host: "127.0.0.1", port: Int(fixture.port) + 1, expectedPort: Int(fixture.port), pin: pin))
        XCTAssertFalse(LocalEngineTLSIdentity.accepts(trust: trust, authenticationMethod: NSURLAuthenticationMethodServerTrust,
                                                      host: "localhost", port: Int(fixture.port), expectedPort: Int(fixture.port), pin: pin))
        XCTAssertFalse(LocalEngineTLSIdentity.accepts(trust: trust, authenticationMethod: NSURLAuthenticationMethodServerTrust,
                                                      host: "127.0.0.1", port: Int(fixture.port), expectedPort: Int(fixture.port),
                                                      pin: String(repeating: "0", count: 64)))
        XCTAssertFalse(LocalEngineTLSIdentity.accepts(trust: trust, authenticationMethod: NSURLAuthenticationMethodHTTPBasic,
                                                      host: "127.0.0.1", port: Int(fixture.port), expectedPort: Int(fixture.port), pin: pin))
    }

    @MainActor
    func testProductionRuleCompilerAllowsOnlySamePortHTTPSAndWSSCookies() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("PinnedHTTPS-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let trusted = try PinnedHTTPSFixture(directory: root.appendingPathComponent("trusted"))
        let other = try PinnedHTTPSFixture(directory: root.appendingPathComponent("other"), identity: trusted.identity)
        let pin = SHA256.hash(data: trusted.certificate).map { String(format: "%02x", $0) }.joined()
        let identity = try XCTUnwrap(LocalEngineTLSIdentity(pid: 400, startupID: "fixture", port: Int(trusted.port), sha256: pin))
        let list: WKContentRuleList? = await withCheckedContinuation { continuation in
            LocalEngineWebSecurity.compileRules(for: identity) { continuation.resume(returning: $0) }
        }
        let delegate = PinningNavigationDelegate(certificate: trusted.certificate)
        trusted.setPage("/ws-probes", body: """
        <!doctype html><script>
          try { new WebSocket('wss://127.0.0.1:\(trusted.port)/same-socket'); } catch (_) {}
          try { new WebSocket('wss://127.0.0.1:\(other.port)/other-socket'); } catch (_) {}
        </script>
        """)
        let view = makeWebView(delegate: delegate, contentRuleList: try XCTUnwrap(list), dataStore: .nonPersistent())
        view.load(URLRequest(url: URL(string: "\(trusted.origin)/cookie")!))
        let cookieSet = await waitForRequest(trusted, "/same-origin")
        XCTAssertTrue(cookieSet)
        view.load(URLRequest(url: URL(string: "\(trusted.origin)/ws-probes")!))
        let pageLoaded = await waitForRequest(trusted, "/ws-probes")
        XCTAssertTrue(pageLoaded)
        let sameSocket = await waitForRequest(trusted, "/same-socket")
        XCTAssertTrue(sameSocket)
        let otherSocket = await waitForRequest(other, "/other-socket")
        XCTAssertTrue(otherSocket)
        XCTAssertEqual(trusted.requests(for: "/same-socket").first?.cookie, "ciaobot_session=secret")
        XCTAssertNil(other.requests(for: "/other-socket").first?.cookie)
    }
    private var windows: [NSWindow] = []

    func testPinnedHTTPSAllowsTheSelectedCertificateAndSameOriginSecureCookie() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("PinnedHTTPS-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: directory) }
        let server = try PinnedHTTPSFixture(directory: directory)
        let delegate = PinningNavigationDelegate(certificate: server.certificate)
        let webView = makeWebView(delegate: delegate, dataStore: .nonPersistent())
        webView.load(URLRequest(url: URL(string: "\(server.origin)/cookie")!))

        XCTAssertTrue(server.waitForRequest("/same-origin"), "The pinned HTTPS page did not load its same-origin request")
        XCTAssertEqual(server.requests(for: "/same-origin").first?.cookie, "ciaobot_session=secret")
        XCTAssertTrue(delegate.challengeHosts.contains("127.0.0.1"), "WebKit did not expose the fixture's server-trust challenge")
        XCTAssertTrue(delegate.rejectedHosts.isEmpty)
    }

    func testCrossPortSubresourcesRedirectsAndWebSocketsCannotReachAnUnpinnedListener() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("PinnedHTTPS-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let trusted = try PinnedHTTPSFixture(directory: root.appendingPathComponent("trusted"))
        let untrusted = try PinnedHTTPSFixture(directory: root.appendingPathComponent("untrusted"))
        let delegate = PinningNavigationDelegate(certificate: trusted.certificate)
        let page = """
        <!doctype html><img id="img" src="https://127.0.0.1:\(untrusted.port)/image">
        <script>
          fetch('https://127.0.0.1:\(untrusted.port)/fetch', {mode:'no-cors', credentials:'include'}).catch(()=>{});
          fetch('/redirect', {mode:'no-cors', credentials:'include'}).catch(()=>{});
          try { new WebSocket('wss://127.0.0.1:\(untrusted.port)/ws'); } catch (_) {}
          window.__fixtureReady = true;
        </script>
        """
        trusted.setPage("/attack", body: page)
        trusted.setRedirect("/redirect", to: "https://127.0.0.1:\(untrusted.port)/redirect-target")
        let webView = makeWebView(delegate: delegate, dataStore: .nonPersistent())
        webView.load(URLRequest(url: URL(string: "\(trusted.origin)/attack")!))

        XCTAssertTrue(trusted.waitForRequest("/attack"), "The pinned page did not load")
        let deadline = Date().addingTimeInterval(5)
        while Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
        XCTAssertTrue(trusted.waitForRequest("/redirect"))
        XCTAssertTrue(untrusted.observed.isEmpty, "An unpinned certificate received an HTTP request or cookie: \(untrusted.observed.map(\.path))")
        XCTAssertTrue(delegate.rejectedHosts.contains("127.0.0.1"), "The mismatched certificate did not reach the pin rejection path")
        XCTAssertGreaterThan(delegate.rejectedAfterTrustEvaluation, 0,
                             "A platform-acceptable certificate with a different leaf must still be rejected by the pin")
        XCTAssertTrue(delegate.challengeHosts.allSatisfy { $0 == "127.0.0.1" })
    }

    func testAValidTrustEvaluationDoesNotAcceptAnUnpinnedCertificate() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("PinnedHTTPS-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: directory) }
        let fixture = try PinnedHTTPSFixture(directory: directory)
        let certificate = try XCTUnwrap(SecCertificateCreateWithData(nil, fixture.certificate as CFData))
        let policy = SecPolicyCreateBasicX509()
        var trust: SecTrust?
        XCTAssertEqual(SecTrustCreateWithCertificates(certificate, policy, &trust), errSecSuccess)
        let acceptedAnchor = [certificate] as CFArray
        XCTAssertEqual(SecTrustSetAnchorCertificates(trust!, acceptedAnchor), errSecSuccess)
        XCTAssertEqual(SecTrustSetAnchorCertificatesOnly(trust!, true), errSecSuccess)
        var evaluationError: CFError?
        XCTAssertTrue(SecTrustEvaluateWithError(trust!, &evaluationError), "The platform trust evaluator did not accept the explicitly trusted fixture anchor: \(String(describing: evaluationError))")

        XCTAssertFalse(
            PinningNavigationDelegate.accepts(
                host: "127.0.0.1",
                scheme: "https",
                trust: trust!,
                pinnedCertificate: Data("a different app identity".utf8)
            ),
            "A valid platform trust result must not bypass the app-specific certificate pin"
        )
        XCTAssertFalse(
            PinningNavigationDelegate.accepts(
                host: "127.0.0.2",
                scheme: "https",
                trust: trust!,
                pinnedCertificate: fixture.certificate
            ),
            "The app pin is scoped to the selected loopback hostname"
        )
    }

    func testWebKitSendsSecureLoopbackCookieToPlainHTTPWithoutRuleFence() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("PinnedHTTPS-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: directory) }
        let trusted = try PinnedHTTPSFixture(directory: directory)
        let http = try LoopbackHTTPProbe()
        let delegate = PinningNavigationDelegate(certificate: trusted.certificate)
        trusted.setPage("/http-probes", body: """
        <!doctype html><img src="http://127.0.0.1:\(http.port)/image">
        """)
        let webView = makeWebView(delegate: delegate, dataStore: .nonPersistent())
        webView.load(URLRequest(url: URL(string: "\(trusted.origin)/cookie")!))
        XCTAssertTrue(trusted.waitForRequest("/same-origin"), "The Secure cookie was not set by the pinned HTTPS origin")
        webView.load(URLRequest(url: URL(string: "\(trusted.origin)/http-probes")!))
        XCTAssertTrue(trusted.waitForRequest("/http-probes"))

        XCTAssertTrue(http.waitForPath("/image"))
        XCTAssertTrue(http.observed.contains(where: { $0.cookie == "ciaobot_session=secret" }),
                      "This negative control documents WebKit's loopback Secure-cookie exception")
    }

    func testCookieRuleFenceSuppressesPlainHTTPAndCrossPortWebSocketCookies() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("PinnedHTTPS-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: directory) }
        let trusted = try PinnedHTTPSFixture(directory: directory)
        let http = try LoopbackHTTPProbe()
        let delegate = PinningNavigationDelegate(certificate: trusted.certificate)
        trusted.setPage("/http-probes", body: """
        <!doctype html><img src="http://127.0.0.1:\(http.port)/image">
        <script>
          fetch('http://127.0.0.1:\(http.port)/fetch', {mode:'no-cors', credentials:'include'}).catch(()=>{});
          try { new WebSocket('ws://127.0.0.1:\(http.port)/socket'); } catch (_) {}
        </script>
        """)
        let list = try await compileUnencryptedCookieFence(in: directory.appendingPathComponent("rules"))
        let webView = makeWebView(delegate: delegate, contentRuleList: list, dataStore: .nonPersistent())
        webView.load(URLRequest(url: URL(string: "\(trusted.origin)/cookie")!))
        let sawSameOrigin = await waitForRequest(trusted, "/same-origin")
        XCTAssertTrue(sawSameOrigin, "Allowed origin requests: \(trusted.observed.map(\.path)); rejected challenges: \(delegate.rejectedHosts); navigation failures: \(delegate.navigationFailures)")
        XCTAssertEqual(trusted.requests(for: "/same-origin").first?.cookie, "ciaobot_session=secret",
                       "The exact TLS listener must retain the session cookie")
        webView.load(URLRequest(url: URL(string: "\(trusted.origin)/http-probes")!))
        let sawProbePage = await waitForRequest(trusted, "/http-probes")
        XCTAssertTrue(sawProbePage)

        try await Task.sleep(nanoseconds: 3_000_000_000)
        XCTAssertFalse(http.observed.isEmpty, "The control listener did not receive any probe request")
        XCTAssertTrue(http.observed.allSatisfy { $0.cookie != "ciaobot_session=secret" },
                      "The content rule failed to strip the Secure cookie from HTTP image requests: \(http.observed)")
        let observedPaths = Set(http.observed.map(\.path))
        for requiredPath in ["/image"] {
            XCTAssertTrue(observedPaths.contains(requiredPath), "The fixture did not observe \(requiredPath); requests were \(http.observed)")
        }
    }

    func testUnencryptedCookieFenceCoversHTTPFetchImagesRedirectsAndWebSocketHandshake() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("PinnedHTTPS-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let tls = try PinnedHTTPSFixture(directory: root.appendingPathComponent("tls"))
        let source = try LoopbackHTTPProbe()
        let target = try LoopbackHTTPProbe()
        source.setPage("/probe", body: """
        <!doctype html><img src="http://127.0.0.1:\(target.port)/image">
        <img src="/redirect-image">
        <script>
          fetch('http://127.0.0.1:\(target.port)/fetch', {mode:'no-cors', credentials:'include'}).catch(()=>{});
          try { new WebSocket('ws://127.0.0.1:\(target.port)/socket'); } catch (_) {}
        </script>
        """)
        source.setRedirect("/redirect-image", to: "http://127.0.0.1:\(target.port)/redirect-target")
        let paths = ["/image", "/fetch", "/socket", "/redirect-target"]

        // Negative control: the same stored Secure cookie crosses ports on
        // loopback when no network rule is installed, including the WebSocket
        // handshake header.
        let openStore = WKWebsiteDataStore.nonPersistent()
        let openDelegate = PinningNavigationDelegate(certificate: tls.certificate)
        let openView = makeWebView(delegate: openDelegate, dataStore: openStore)
        openView.load(URLRequest(url: URL(string: "\(tls.origin)/cookie")!))
        let openCookieSet = await waitForRequest(tls, "/same-origin")
        XCTAssertTrue(openCookieSet)
        openView.load(URLRequest(url: URL(string: "\(source.origin)/probe")!))
        let openPageLoaded = await waitForRequest(source, "/probe")
        XCTAssertTrue(openPageLoaded)
        for path in paths {
            let observed = await waitForRequest(target, path)
            XCTAssertTrue(observed, "Unfenced request \(path) was not observed")
        }
        XCTAssertTrue(target.observed.allSatisfy { $0.cookie == "ciaobot_session=secret" },
                      "The negative control did not expose loopback cookie sharing: \(target.observed)")

        source.resetRequests()
        target.resetRequests()
        let list = try await compileUnencryptedCookieFence(in: root.appendingPathComponent("rules"))
        let protectedStore = WKWebsiteDataStore.nonPersistent()
        let protectedDelegate = PinningNavigationDelegate(certificate: tls.certificate)
        let protectedView = makeWebView(
            delegate: protectedDelegate,
            contentRuleList: list,
            dataStore: protectedStore
        )
        protectedView.load(URLRequest(url: URL(string: "\(tls.origin)/cookie")!))
        let protectedCookieSet = await waitForRequest(tls, "/same-origin")
        XCTAssertTrue(protectedCookieSet)
        XCTAssertEqual(tls.requests(for: "/same-origin").last?.cookie, "ciaobot_session=secret")
        protectedView.load(URLRequest(url: URL(string: "\(source.origin)/probe")!))
        let protectedPageLoaded = await waitForRequest(source, "/probe")
        XCTAssertTrue(protectedPageLoaded)
        for path in paths {
            let observed = await waitForRequest(target, path)
            XCTAssertTrue(observed, "Fenced request \(path) was not observed")
        }
        XCTAssertTrue(target.observed.allSatisfy { $0.cookie != "ciaobot_session=secret" },
                      "A cookie crossed the fence to an HTTP resource or WebSocket: \(target.observed)")
    }

    func testExactOriginCSPBlocksCrossPortImageFetchRedirectAndWebSocket() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("PinnedHTTPS-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let trusted = try PinnedHTTPSFixture(directory: root.appendingPathComponent("tls"))
        let untrusted = try LoopbackHTTPProbe()
        let delegate = PinningNavigationDelegate(certificate: trusted.certificate)
        let exactHTTPS = "https://127.0.0.1:\(trusted.port)"
        let exactWSS = "wss://127.0.0.1:\(trusted.port)"
        let policy = "default-src 'none'; script-src 'unsafe-inline'; img-src \(exactHTTPS); connect-src \(exactHTTPS) \(exactWSS)"
        trusted.setPage("/csp", body: """
        <!doctype html><img src="http://127.0.0.1:\(untrusted.port)/image">
        <script>
          document.addEventListener('securitypolicyviolation', () => fetch('/csp-report').catch(()=>{}));
          fetch('http://127.0.0.1:\(untrusted.port)/fetch', {mode:'no-cors', credentials:'include'}).catch(()=>{});
          fetch('/redirect-http', {mode:'no-cors', credentials:'include'}).catch(()=>{});
          try { new WebSocket('ws://127.0.0.1:\(untrusted.port)/socket'); } catch (_) {}
        </script>
        """, headers: ["Content-Security-Policy": policy])
        trusted.setRedirect("/redirect-http", to: "http://127.0.0.1:\(untrusted.port)/redirect-target")
        let webView = makeWebView(delegate: delegate, dataStore: .nonPersistent())
        webView.load(URLRequest(url: URL(string: "\(trusted.origin)/csp")!))

        let loaded = await waitForRequest(trusted, "/csp")
        XCTAssertTrue(loaded)
        let reportDeadline = Date().addingTimeInterval(5)
        while trusted.requests(for: "/csp-report").count < 3, Date() < reportDeadline {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertGreaterThanOrEqual(trusted.requests(for: "/csp-report").count, 3,
                                    "CSP should report the blocked image, fetch/redirect, and WebSocket channels")
        XCTAssertTrue(untrusted.observed.isEmpty,
                      "CSP allowed a cross-port request to reach an HTTP listener: \(untrusted.observed)")
        XCTAssertTrue(trusted.requests(for: "/redirect-http").count > 0,
                      "The same-origin redirect route was not reached before the cross-origin destination was blocked")
    }

    private func makeWebView(
        delegate: PinningNavigationDelegate,
        contentRuleList: WKContentRuleList? = nil,
        dataStore: WKWebsiteDataStore
    ) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = dataStore
        if let contentRuleList { configuration.userContentController.add(contentRuleList) }
        let view = WKWebView(frame: NSRect(x: 0, y: 0, width: 500, height: 400), configuration: configuration)
        view.navigationDelegate = delegate
        let window = NSWindow(contentRect: view.frame, styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = view
        window.makeKeyAndOrderFront(nil)
        windows.append(window)
        return view
    }

    private func waitForRequest(_ fixture: LoopbackHTTPProbe, _ path: String, timeout: TimeInterval = 8) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if fixture.observed.contains(where: { $0.path == path }) { return true }
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        return fixture.observed.contains(where: { $0.path == path })
    }

    private func waitForRequest(_ fixture: PinnedHTTPSFixture, _ path: String, timeout: TimeInterval = 8) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if !fixture.requests(for: path).isEmpty { return true }
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        return !fixture.requests(for: path).isEmpty
    }

    private func waitForRequestCount(_ fixture: PinnedHTTPSFixture, _ path: String, count: Int,
                                     timeout: TimeInterval = 8) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if fixture.requests(for: path).count >= count { return true }
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        return fixture.requests(for: path).count >= count
    }

    private func compileUnencryptedCookieFence(in directory: URL) async throws -> WKContentRuleList {
        _ = directory
        guard let store = WKContentRuleListStore.default() else {
            throw NSError(domain: "PinnedHTTPSWebKitTests", code: 1)
        }
        let source = """
        [
          {"trigger":{"url-filter":"^http:"},"action":{"type":"block-cookies"}},
          {"trigger":{"url-filter":"^ws:"},"action":{"type":"block-cookies"}}
        ]
        """
        return try await withCheckedThrowingContinuation { continuation in
            store.compileContentRuleList(forIdentifier: UUID().uuidString, encodedContentRuleList: source) { list, error in
                if let list { continuation.resume(returning: list) }
                else { continuation.resume(throwing: error ?? NSError(domain: "PinnedHTTPSWebKitTests", code: 2)) }
            }
        }
    }
}
