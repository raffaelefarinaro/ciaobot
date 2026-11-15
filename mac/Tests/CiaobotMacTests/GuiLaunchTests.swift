import XCTest
import Foundation
import Darwin
import CoreGraphics

@testable import CiaobotMac

/// A tiny HTTP/1.1 server on an ephemeral loopback port. It exists only for
/// the GUI launch test: it records every path the app asks for and serves a
/// page that navigates once it has been rendered by WebKit.
private final class IsolatedFixtureServer {
    let port: Int

    private let listener: Int32
    private let lock = NSLock()
    private let queue = DispatchQueue(label: "CiaobotMacTests.Fixture", attributes: .concurrent)
    private var paths: [String] = []
    private var stopped = false
    private lazy var thread = Thread { [weak self] in self?.run() }

    init() throws {
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

    var observedPaths: [String] {
        lock.lock()
        defer { lock.unlock() }
        return paths
    }

    func waitForPath(_ path: String, timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            lock.lock()
            let found = paths.contains(path)
            lock.unlock()
            if found { return true }
            usleep(20_000)
        }
        lock.lock()
        defer { lock.unlock() }
        return paths.contains(path)
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
        let path = target.split(separator: "?", maxSplits: 1).first.map(String.init) ?? target

        lock.lock()
        paths.append(path)
        lock.unlock()

        writeResponse(client, response(for: path))
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
        let status: String
        let contentType: String
        let body: String
        switch path {
        case "/api/startup-status":
            status = "200 OK"
            contentType = "application/json"
            body = #"{"startup_id":"gui-fixture","phases":[],"overall_ready":true}"#
        case "/":
            status = "200 OK"
            contentType = "text/html; charset=utf-8"
            body = """
            <!doctype html>
            <html><head><meta charset="utf-8"><title>Ciaobot isolated fixture</title></head>
            <body><h1>Isolated fixture</h1>
            <script>
              if (window.__CIAOBOT_DESKTOP__ === true) {
                new Image().src = '/desktop-marker';
              }
              const newWindow = document.createElement('a');
              newWindow.href = '/new-window';
              newWindow.target = '_blank';
              document.body.appendChild(newWindow);
              newWindow.click();
              setTimeout(() => location.replace('/loaded'), 250);
            </script></body></html>
            """
        case "/loaded":
            status = "200 OK"
            contentType = "text/html; charset=utf-8"
            body = "<!doctype html><title>fixture-loaded</title><h1 id=\"loaded\">fixture-loaded</h1>"
        case "/favicon.ico":
            status = "204 No Content"
            contentType = "text/plain"
            body = ""
        case "/desktop-marker":
            status = "204 No Content"
            contentType = "text/plain"
            body = ""
        default:
            status = "404 Not Found"
            contentType = "text/plain"
            body = "not found"
        }
        return "HTTP/1.1 \(status)\r\nContent-Type: \(contentType)\r\nContent-Length: \(body.utf8.count)\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n\(body)"
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

private func fixtureError(_ operation: String) -> NSError {
    NSError(domain: "CiaobotMacTests.Fixture", code: 1, userInfo: [NSLocalizedDescriptionKey: operation])
}

final class GuiLaunchTests: XCTestCase {
    #if DEBUG
    func testTestLaunchIsLoopbackOnlyAndFailsClosed() {
        XCTAssertNil(TestLaunch.origin(environment: [:]))
        XCTAssertNil(TestLaunch.origin(environment: ["CIAOBOT_MAC_TEST_ORIGIN": "https://example.com"]))
        XCTAssertNil(TestLaunch.origin(environment: ["CIAOBOT_MAC_TEST_ORIGIN": "http://192.168.1.2:8443"]))
        XCTAssertNil(TestLaunch.origin(environment: ["CIAOBOT_MAC_TEST_ORIGIN": "garbage"]))
        XCTAssertNotNil(TestLaunch.origin(environment: ["CIAOBOT_MAC_TEST_ORIGIN": "http://127.0.0.1:9"]))

        // Setting only the auto-connect flag must never fall back to the
        // proposed live origin.
        XCTAssertFalse(TestLaunch.autoConnect(environment: ["CIAOBOT_MAC_TEST_AUTOCONNECT": "1"]))
        XCTAssertFalse(TestLaunch.autoConnect(environment: [
            "CIAOBOT_MAC_TEST_ORIGIN": "https://example.com",
            "CIAOBOT_MAC_TEST_AUTOCONNECT": "1",
        ]))
        XCTAssertTrue(TestLaunch.autoConnect(environment: [
            "CIAOBOT_MAC_TEST_ORIGIN": "http://127.0.0.1:9",
            "CIAOBOT_MAC_TEST_AUTOCONNECT": "1",
        ]))

        let live = "http://127.0.0.1:8443"
        XCTAssertEqual(TestLaunch.initialAddress(defaultAddress: live, environment: [:]), live)
        XCTAssertEqual(TestLaunch.initialAddress(defaultAddress: live, environment: [
            "CIAOBOT_MAC_TEST_ORIGIN": "http://127.0.0.1:9",
        ]), "http://127.0.0.1:9")
        XCTAssertEqual(TestLaunch.initialAddress(defaultAddress: live, environment: [
            "CIAOBOT_MAC_TEST_ORIGIN": "https://example.com",
            "CIAOBOT_MAC_TEST_AUTOCONNECT": "1",
        ]), "")
        XCTAssertEqual(TestLaunch.initialAddress(defaultAddress: live, environment: [
            "CIAOBOT_MAC_TEST_AUTOCONNECT": "1",
        ]), "")
    }
    #endif

    func testAppOpensWindowAndLoadsIsolatedFixture() throws {
        #if !DEBUG
        throw XCTSkip("The GUI launch test uses DEBUG-only fixture controls; run `swift test` in debug configuration.")
        #endif

        let appURL = try appExecutable()
        let fixture = try IsolatedFixtureServer()
        defer { fixture.shutdown() }

        let process = Process()
        process.executableURL = appURL
        process.environment = childEnvironment(fixture: fixture)
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        defer { terminate(process) }

        try process.run()

        XCTAssertTrue(waitForWindow(pid: process.processIdentifier, timeout: 10),
                      "The app did not create a visible native window.")
        XCTAssertTrue(fixture.waitForPath("/api/startup-status", timeout: 15),
                      "The app did not probe the isolated origin. Paths seen: \(fixture.observedPaths)")
        XCTAssertTrue(fixture.waitForPath("/", timeout: 10),
                      "The app did not load the fixture page in WebKit. Paths seen: \(fixture.observedPaths)")
        XCTAssertTrue(fixture.waitForPath("/loaded", timeout: 15),
                      "The fixture page did not render and navigate. Paths seen: \(fixture.observedPaths)")
        XCTAssertTrue(fixture.waitForPath("/desktop-marker", timeout: 5),
                      "The desktop marker was not present in the real app webview. Paths seen: \(fixture.observedPaths)")
        XCTAssertFalse(fixture.waitForPath("/new-window", timeout: 0.5),
                       "A same-origin target=_blank request was loaded into the app window.")
        XCTAssertTrue(process.isRunning, "The app exited while loading the fixture.")
    }

    private func childEnvironment(fixture: IsolatedFixtureServer) -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        // Do not let the launched app inherit the XCTest injection used to run
        // this test bundle. It should start as an ordinary app process.
        for key in environment.keys where key.hasPrefix("XCTest") || key.hasPrefix("XCInject") || key.hasPrefix("DYLD_") {
            environment.removeValue(forKey: key)
        }
        environment["CIAOBOT_MAC_TEST_ORIGIN"] = fixture.origin
        environment["CIAOBOT_MAC_TEST_AUTOCONNECT"] = "1"
        return environment
    }

    private func appExecutable() throws -> URL {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let candidates = [
            packageRoot.appendingPathComponent(".build/debug/CiaobotMac"),
            URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent(".build/debug/CiaobotMac"),
        ]
        for candidate in candidates where FileManager.default.isExecutableFile(atPath: candidate.path) {
            return candidate
        }
        XCTFail("Debug executable not found; run `swift build` before `swift test`.")
        throw NSError(domain: "CiaobotMacTests", code: 1, userInfo: [NSLocalizedDescriptionKey: "debug executable missing"])
    }

    private func waitForWindow(pid: Int32, timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if windowExists(pid: pid) { return true }
            usleep(100_000)
        }
        return windowExists(pid: pid)
    }

    private func windowExists(pid: Int32) -> Bool {
        guard let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
            return false
        }
        return windows.contains { window in
            guard (window[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid,
                  let bounds = window[kCGWindowBounds as String] as? [String: Any],
                  let width = (bounds["Width"] as? NSNumber)?.doubleValue,
                  let height = (bounds["Height"] as? NSNumber)?.doubleValue else { return false }
            return width >= 480 && height >= 320
        }
    }

    private func terminate(_ process: Process) {
        guard process.isRunning else { return }
        process.terminate()
        let deadline = Date().addingTimeInterval(3)
        while process.isRunning && Date() < deadline {
            usleep(50_000)
        }
        if process.isRunning {
            kill(process.processIdentifier, SIGKILL)
        }
        process.waitUntilExit()
    }
}
