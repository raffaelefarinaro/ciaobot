import XCTest
import Foundation
@testable import CiaobotMac

private final class ProbeProtocol: URLProtocol {
    static var handler: ((URLRequest, ProbeProtocol) -> Void)?
    static var stopped: (() -> Void)?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() { Self.handler?(request, self) }
    override func stopLoading() { Self.stopped?() }
}

final class ProbeTransportTests: XCTestCase {
    override func tearDown() { ProbeProtocol.handler = nil; ProbeProtocol.stopped = nil; super.tearDown() }

    func testProbeDoesNotSendCookiesOrCredentials() {
        let done = expectation(description: "probe completed")
        let origin = Origin("https://probe.example")!
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [ProbeProtocol.self]
        config.httpAdditionalHeaders = [
            "Authorization": "Bearer private",
            "Cookie": "session=private",
        ]
        let cookie = HTTPCookie(properties: [.domain: "probe.example", .path: "/", .name: "session", .value: "private", .secure: "TRUE"])!
        config.httpCookieStorage?.setCookie(cookie)
        let space = URLProtectionSpace(host: "probe.example", port: 443, protocol: "https", realm: nil, authenticationMethod: NSURLAuthenticationMethodHTTPBasic)
        config.urlCredentialStorage?.setDefaultCredential(URLCredential(user: "user", password: "private", persistence: .forSession), for: space)
        ProbeProtocol.handler = { request, fixture in
            XCTAssertEqual(request.url?.path, "/api/startup-status")
            XCTAssertNil(request.value(forHTTPHeaderField: "Cookie"))
            XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
            XCTAssertFalse(request.httpShouldHandleCookies)
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            fixture.client?.urlProtocol(fixture, didReceive: response, cacheStoragePolicy: .notAllowed)
            fixture.client?.urlProtocol(fixture, didLoad: Data(#"{"startup_id":"ok"}"#.utf8))
            fixture.client?.urlProtocolDidFinishLoading(fixture)
        }
        Probe().check(origin, configuration: config) { result in XCTAssertTrue(result); done.fulfill() }
        wait(for: [done], timeout: 3)
    }

    func testRedirectDelegateRejectsRedirect() {
        let origin = URL(string: "https://probe.example/api/startup-status")!
        let target = URL(string: "https://elsewhere.example/")!
        let response = HTTPURLResponse(url: origin, statusCode: 302, httpVersion: nil, headerFields: ["Location": target.absoluteString])!
        let config = URLSessionConfiguration.ephemeral
        let session = URLSession(configuration: config)
        let task = session.dataTask(with: origin)
        let done = expectation(description: "redirect rejected")
        Probe().urlSession(session, task: task, willPerformHTTPRedirection: response, newRequest: URLRequest(url: target)) { request in
            XCTAssertNil(request)
            done.fulfill()
        }
        wait(for: [done], timeout: 1)
        session.invalidateAndCancel()
    }
}

import Darwin

extension ProbeTransportTests {
    func testOversizedResponseIsRejected() {
        let done = expectation(description: "oversized response rejected")
        let stopped = expectation(description: "oversized request cancelled")
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [ProbeProtocol.self]
        ProbeProtocol.stopped = { stopped.fulfill() }
        ProbeProtocol.handler = { request, fixture in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            fixture.client?.urlProtocol(fixture, didReceive: response, cacheStoragePolicy: .notAllowed)
            fixture.client?.urlProtocol(fixture, didLoad: Data(repeating: 32, count: Probe.maximumResponseBytes))
            fixture.client?.urlProtocol(fixture, didLoad: Data(#"{"startup_id":"ok"}"#.utf8))
            // Deliberately never finish: cancellation must complete the probe.
        }
        Probe().check(Origin("https://probe.example")!, configuration: config) { result in XCTAssertFalse(result); done.fulfill() }
        wait(for: [done, stopped], timeout: 3)
    }
}

private final class HTTPFixture {
    let listener: Int32
    let port: Int
    init() throws {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw NSError(domain: "fixture socket", code: 1) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_addr.s_addr = in_addr_t(0x7f000001).bigEndian
        let bound = withUnsafePointer(to: &address) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) } }
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        guard bound == 0, listen(fd, 2) == 0,
              withUnsafeMutablePointer(to: &address, { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &length) } }) == 0 else {
            close(fd); throw NSError(domain: "fixture bind", code: 1)
        }
        listener = fd
        port = Int(UInt16(bigEndian: address.sin_port))
    }
    deinit { close(listener) }
    func acceptRequest(timeout: Int32 = 2000) -> (Int32, String)? {
        var item = pollfd(fd: listener, events: Int16(POLLIN), revents: 0)
        guard poll(&item, 1, timeout) == 1 else { return nil }
        let client = accept(listener, nil, nil)
        guard client >= 0 else { return nil }
        var bytes = Data()
        let deadline = Date().addingTimeInterval(2)
        while bytes.range(of: Data("\r\n\r\n".utf8)) == nil && bytes.count < 16384 {
            let remaining = Int32(max(0, deadline.timeIntervalSinceNow * 1000))
            var readable = pollfd(fd: client, events: Int16(POLLIN), revents: 0)
            guard remaining > 0, poll(&readable, 1, remaining) == 1 else { close(client); return nil }
            var buffer = [UInt8](repeating: 0, count: 1024)
            let count = recv(client, &buffer, buffer.count, 0)
            guard count > 0 else { close(client); return nil }
            bytes.append(contentsOf: buffer.prefix(count))
        }
        guard bytes.range(of: Data("\r\n\r\n".utf8)) != nil else { close(client); return nil }
        return (client, String(decoding: bytes, as: UTF8.self))
    }
    func sendResponse(_ client: Int32, _ response: String) {
        defer { close(client) }
        var noSignal: Int32 = 1
        _ = setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
        let bytes = Array(response.utf8)
        var sent = 0
        let deadline = Date().addingTimeInterval(2)
        while sent < bytes.count {
            let remaining = Int32(max(0, deadline.timeIntervalSinceNow * 1000))
            var writable = pollfd(fd: client, events: Int16(POLLOUT), revents: 0)
            guard remaining > 0, poll(&writable, 1, remaining) == 1 else { XCTFail("Fixture write timed out"); return }
            let count = bytes.withUnsafeBytes { buffer in
                send(client, buffer.baseAddress!.advanced(by: sent), bytes.count - sent, 0)
            }
            guard count > 0 else { XCTFail("Fixture write failed"); return }
            sent += count
        }
    }
}

extension ProbeTransportTests {
    func testRealRedirectIsNotFollowed() throws {
        let fixture = try HTTPFixture()
        let done = expectation(description: "redirect rejected")
        let server = expectation(description: "redirect target observed")
        DispatchQueue.global().async {
            defer { server.fulfill() }
            guard let (client, request) = fixture.acceptRequest() else { XCTFail("No initial request"); return }
            XCTAssertTrue(request.hasPrefix("GET /api/startup-status "))
            fixture.sendResponse(client, "HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:\(fixture.port)/redirected\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            if let (target, request) = fixture.acceptRequest(timeout: 2000) {
                XCTAssertFalse(request.contains("/redirected"), "Redirect was followed")
                let body = #"{"startup_id":"valid"}"#
                fixture.sendResponse(target, "HTTP/1.1 200 OK\r\nContent-Length: \(body.utf8.count)\r\nConnection: close\r\n\r\n\(body)")
                XCTFail("Unexpected second request")
            }
        }
        Probe().check(Origin("http://127.0.0.1:\(fixture.port)")!) { result in XCTAssertFalse(result); done.fulfill() }
        wait(for: [done, server], timeout: 6)
    }

    func testHTTPChallengeNeverUsesSharedCredentialsOrCookies() throws {
        let fixture = try HTTPFixture()
        let cookieStorage = HTTPCookieStorage.shared
        let cookie = HTTPCookie(properties: [.domain: "127.0.0.1", .path: "/", .name: "probe-\(UUID().uuidString)", .value: "private"])!
        cookieStorage.setCookie(cookie)
        defer { cookieStorage.deleteCookie(cookie) }
        let space = URLProtectionSpace(host: "127.0.0.1", port: fixture.port, protocol: "http", realm: "fixture", authenticationMethod: NSURLAuthenticationMethodHTTPBasic)
        let credentials = URLCredentialStorage.shared
        let previous = credentials.defaultCredential(for: space)
        let credential = URLCredential(user: "fixture-user", password: "fixture-password", persistence: .forSession)
        credentials.setDefaultCredential(credential, for: space)
        defer {
            credentials.remove(credential, for: space)
            if let previous { credentials.setDefaultCredential(previous, for: space) }
        }
        let done = expectation(description: "challenge rejected")
        let server = expectation(description: "challenge attempts inspected")
        DispatchQueue.global().async {
            defer { server.fulfill() }
            guard let (client, request) = fixture.acceptRequest() else { XCTFail("No initial request"); return }
            let headers = request.lowercased()
            XCTAssertFalse(headers.contains("\r\ncookie:"))
            XCTAssertFalse(headers.contains("\r\nauthorization:"))
            fixture.sendResponse(client, "HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Basic realm=\"fixture\"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            if let (retry, request) = fixture.acceptRequest(timeout: 2000) {
                XCTAssertFalse(request.lowercased().contains("\r\nauthorization:"), "Stored credential leaked")
                close(retry)
                XCTFail("Unexpected authentication retry")
            }
        }
        Probe().check(Origin("http://127.0.0.1:\(fixture.port)")!) { result in XCTAssertFalse(result); done.fulfill() }
        wait(for: [done, server], timeout: 6)
    }

    func testProbeTimesOutWhenServerDoesNotRespond() throws {
        let fixture = try HTTPFixture()
        let done = expectation(description: "request timed out")
        let server = expectation(description: "fixture closed")
        let completed = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            defer { server.fulfill() }
            guard let (client, _) = fixture.acceptRequest() else { XCTFail("No initial request"); return }
            // Keep the connection open until the callback, or fail before closing.
            // Closing it must never be what makes the probe fail the test.
            XCTAssertEqual(completed.wait(timeout: .now() + 2), .success, "Probe did not time out while the server remained open")
            close(client)
        }
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 0.3
        config.timeoutIntervalForResource = 1
        Probe().check(Origin("http://127.0.0.1:\(fixture.port)")!, configuration: config) { result in
            XCTAssertFalse(result)
            completed.signal()
            done.fulfill()
        }
        wait(for: [done, server], timeout: 5)
    }
}
