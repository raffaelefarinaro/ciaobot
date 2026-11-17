import XCTest
import Foundation
import Darwin
import CoreGraphics

@testable import CiaobotMac

/// A tiny HTTP/1.1 server on an ephemeral loopback port. It exists only for
/// the GUI launch test: it records every path the app asks for and serves a
/// page that navigates once it has been rendered by WebKit. Window reports are
/// accepted only with this launch's secret; the secret is never logged or
/// stored with a report.
private final class IsolatedFixtureServer {
    let port: Int
    let reportSecret: String

    private let listener: Int32
    private let lock = NSLock()
    private let queue = DispatchQueue(label: "CiaobotMacTests.Fixture", attributes: .concurrent)
    private var paths: [String] = []
    private var windowReports: [Int: [String: String]] = [:]
    private var rejectedReports = 0
    private var challenge: String?
    private let redirectWindowReportsTo: String?
    private var stopped = false
    private lazy var thread = Thread { [weak self] in self?.run() }

    /// - Parameter redirectWindowReportsTo: when set, `/test/window-state`
    ///   answers with a redirect to this URL instead of recording the report.
    init(redirectWindowReportsTo: String? = nil) throws {
        self.redirectWindowReportsTo = redirectWindowReportsTo
        self.reportSecret = (0..<32).map { _ in String(format: "%02x", UInt8.random(in: .min ... .max)) }.joined()
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

    /// The authenticated main-window report with the highest sequence number,
    /// as query name/value pairs. A report that arrives after a newer one
    /// cannot replace it.
    var latestWindowReport: [String: String]? {
        lock.lock()
        defer { lock.unlock() }
        return windowReports.max { $0.key < $1.key }?.value
    }

    /// Starts a new freshness challenge. Every accepted report from now on
    /// is answered with it, so the app's next report echoes it, and the state
    /// in that report was read after this call.
    func issueChallenge() -> String {
        let value = (0..<16).map { _ in String(format: "%02x", UInt8.random(in: .min ... .max)) }.joined()
        lock.lock()
        challenge = value
        lock.unlock()
        return value
    }

    /// The highest-sequence authenticated report that echoes `challenge`,
    /// waiting up to `timeout` for one to arrive.
    func waitForReport(answering challenge: String, timeout: TimeInterval) -> [String: String]? {
        let deadline = Date().addingTimeInterval(timeout)
        repeat {
            lock.lock()
            let found = windowReports.filter { $0.value["challenge"] == challenge }.max { $0.key < $1.key }?.value
            lock.unlock()
            if let found { return found }
            usleep(20_000)
        } while Date() < deadline
        return nil
    }

    var rejectedReportCount: Int {
        lock.lock()
        defer { lock.unlock() }
        return rejectedReports
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

        #if DEBUG
        // The window reporter exists only in DEBUG builds; a release test
        // build answers this path like any other unknown one.
        if path == "/test/window-state" {
            writeResponse(client, windowReportResponse(target: target, request: request))
            return
        }
        #endif
        writeResponse(client, response(for: path))
    }

    #if DEBUG
    /// Records an authenticated report under its sequence number. A missing
    /// or wrong secret, or a report without a sequence number, is rejected
    /// with 403 and not recorded.
    private func windowReportResponse(target: String, request: String) -> String {
        if let redirectWindowReportsTo {
            return "HTTP/1.1 302 Found\r\nLocation: \(redirectWindowReportsTo)\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        }
        let query = URLComponents(string: target)?.queryItems ?? []
        let report = Dictionary(query.map { ($0.name, $0.value ?? "") }, uniquingKeysWith: { _, last in last })
        let header = TestWindowState.secretHeader.lowercased() + ":"
        let presented = request.components(separatedBy: "\r\n").dropFirst()
            .first { $0.lowercased().hasPrefix(header) }
            .map { String($0.dropFirst(header.count)).trimmingCharacters(in: .whitespaces) }
        lock.lock()
        defer { lock.unlock() }
        guard let presented, Self.constantTimeEqual(presented, reportSecret),
              let sequence = report["seq"].flatMap(Int.init) else {
            rejectedReports += 1
            return "HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        }
        windowReports[sequence] = report
        let challengeLine = challenge.map { "\(TestWindowState.challengeHeader): \($0)\r\n" } ?? ""
        return "HTTP/1.1 204 No Content\r\n\(challengeLine)Content-Length: 0\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
    }

    private static func constantTimeEqual(_ lhs: String, _ rhs: String) -> Bool {
        let a = Array(lhs.utf8), b = Array(rhs.utf8)
        guard a.count == b.count else { return false }
        return zip(a, b).reduce(UInt8(0)) { $0 | ($1.0 ^ $1.1) } == 0
    }
    #endif

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

/// Decides whether the launched app has a visible native main window.
///
/// WindowServer is the authority for the window's existence, owner, layer,
/// alpha and size. Its on-screen list holds only windows on the active Space,
/// so when another app's full-screen Space is active a correctly ordered-in
/// window is missing from it (#3174). The app's own reports (AppKit
/// `isVisible` and `isOnActiveSpace`) separate that case from a window that
/// never appeared; a window AppKit places on the active Space must still be
/// on WindowServer's on-screen list.
///
/// Only fresh reports count. The check issues a challenge, waits for a report
/// that echoes it (so its state was read after the challenge), and only then
/// snapshots WindowServer. A window that is not on screen additionally needs a
/// second report answering a challenge issued after that snapshot, naming the
/// same window as still visible on another Space, with the same AppKit
/// visibility-transition count (see `TestWindowTransitionCounter`). AppKit
/// reports every order-out as it happens, so an equal count proves the window
/// stayed ordered in for the whole interval around the snapshot; a window
/// ordered out and back in during the snapshot fails instead of passing.
enum MainWindowVisibility: Equatable {
    case onScreen(number: Int)
    case orderedInOnAnotherSpace(number: Int)
    case missing(String)

    static let minimumWidth = 480.0
    static let minimumHeight = 320.0

    var isVisible: Bool {
        if case .missing = self { return false }
        return true
    }

    private struct Reading {
        let number: Int
        let visible: Bool
        let onActiveSpace: Bool
        let transitions: Int
    }

    /// A report that answers `challenge` and is complete, or why not.
    private static func reading(_ report: [String: String]?, challenge: String, label: String) -> Result<Reading, Failure> {
        guard let report else {
            return .failure(Failure("no report answered the \(label) challenge"))
        }
        guard report["challenge"] == challenge else {
            return .failure(Failure("the \(label) report is stale: it does not answer this check's challenge"))
        }
        guard let number = report["number"].flatMap(Int.init),
              let visible = report["visible"], let activeSpace = report["activeSpace"],
              let transitions = report["transitions"].flatMap(Int.init) else {
            return .failure(Failure("the \(label) report is incomplete: \(report.filter { $0.key != "challenge" })"))
        }
        return .success(Reading(number: number, visible: visible == "1", onActiveSpace: activeSpace != "0",
                                transitions: transitions))
    }

    private struct Failure: Error { let reason: String; init(_ reason: String) { self.reason = reason } }

    /// - Parameters:
    ///   - before: the newest report answering `beforeChallenge`, which was
    ///     issued before WindowServer was snapshotted.
    ///   - after: the newest report answering `afterChallenge`, which was
    ///     issued after the snapshot. Only the other-Space case needs it.
    static func evaluate(
        pid: Int32,
        before: [String: String]?, beforeChallenge: String,
        after: [String: String]?, afterChallenge: String,
        allWindows: [[String: Any]],
        onScreenNumbers: Set<Int>
    ) -> MainWindowVisibility {
        let first: Reading
        switch reading(before, challenge: beforeChallenge, label: "first") {
        case .failure(let failure): return .missing(failure.reason)
        case .success(let value): first = value
        }
        let number = first.number
        guard first.visible else {
            return .missing("AppKit reports main window \(number) is not visible")
        }
        guard let window = allWindows.first(where: { ($0[kCGWindowNumber as String] as? NSNumber)?.intValue == number }) else {
            return .missing("WindowServer has no window \(number)")
        }
        let owner = (window[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value
        let layer = (window[kCGWindowLayer as String] as? NSNumber)?.intValue
        let alpha = (window[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 0
        let bounds = window[kCGWindowBounds as String] as? [String: Any]
        let width = (bounds?["Width"] as? NSNumber)?.doubleValue ?? 0
        let height = (bounds?["Height"] as? NSNumber)?.doubleValue ?? 0
        guard owner == pid else {
            return .missing("window \(number) belongs to pid \(owner.map(String.init) ?? "?"), not \(pid)")
        }
        guard layer == 0, alpha > 0 else {
            return .missing("window \(number) is not a normal opaque window (layer \(layer.map(String.init) ?? "?"), alpha \(alpha))")
        }
        guard width >= minimumWidth, height >= minimumHeight else {
            return .missing("window \(number) is \(width)x\(height), smaller than \(minimumWidth)x\(minimumHeight)")
        }
        if onScreenNumbers.contains(number) {
            // WindowServer itself shows the window on screen at the snapshot.
            return .onScreen(number: number)
        }
        guard !first.onActiveSpace else {
            return .missing("window \(number) is on the active Space but WindowServer does not list it on screen")
        }
        let second: Reading
        switch reading(after, challenge: afterChallenge, label: "second") {
        case .failure(let failure): return .missing(failure.reason)
        case .success(let value): second = value
        }
        guard second.number == number, second.visible, !second.onActiveSpace else {
            return .missing("main window \(number) did not stay visible on another Space across the WindowServer snapshot "
                            + "(now window \(second.number), visible \(second.visible), on active Space \(second.onActiveSpace))")
        }
        guard second.transitions == first.transitions else {
            // Visible at both readings is not enough: AppKit saw the window
            // ordered out, closed, miniaturized, hidden or replaced in
            // between, so it may not have been ordered in at the snapshot.
            return .missing("main window \(number) changed visibility \(second.transitions - first.transitions) "
                            + "time(s) between the readings around the WindowServer snapshot")
        }
        return .orderedInOnAnotherSpace(number: number)
    }
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

    // MARK: - Main window visibility decision (#3174)

    private let firstChallenge = String(repeating: "1a", count: 16)
    private let secondChallenge = String(repeating: "2b", count: 16)

    private func serverWindow(number: Int = 42, pid: Int32 = 7, layer: Int = 0, alpha: Double = 1,
                              width: Double = 1100, height: Double = 760) -> [String: Any] {
        [
            kCGWindowNumber as String: NSNumber(value: number),
            kCGWindowOwnerPID as String: NSNumber(value: pid),
            kCGWindowLayer as String: NSNumber(value: layer),
            kCGWindowAlpha as String: NSNumber(value: alpha),
            kCGWindowBounds as String: ["X": NSNumber(value: 0), "Y": NSNumber(value: 0),
                                        "Width": NSNumber(value: width), "Height": NSNumber(value: height)],
        ]
    }

    private func report(number: Int = 42, visible: String = "1", activeSpace: String = "1",
                        challenge: String? = nil, transitions: Int = 1) -> [String: String] {
        ["challenge": challenge ?? firstChallenge, "number": String(number), "visible": visible,
         "activeSpace": activeSpace, "width": "1100", "height": "760", "transitions": String(transitions)]
    }

    /// The second, post-snapshot report of a window still on another Space.
    private func stillOnAnotherSpace(number: Int = 42) -> [String: String] {
        report(number: number, activeSpace: "0", challenge: secondChallenge)
    }

    private func judge(_ before: [String: String]?, after: [String: String]? = nil,
                       windows: [[String: Any]]? = nil, onScreen: Set<Int> = []) -> MainWindowVisibility {
        MainWindowVisibility.evaluate(
            pid: 7,
            before: before, beforeChallenge: firstChallenge,
            after: after ?? stillOnAnotherSpace(), afterChallenge: secondChallenge,
            allWindows: windows ?? [serverWindow()], onScreenNumbers: onScreen
        )
    }

    func testVisibilityRequiresTheWindowOnScreenWhenAppKitPlacesItOnTheActiveSpace() {
        XCTAssertEqual(judge(report(), onScreen: [42]), .onScreen(number: 42))
        // The original strict check still applies on the active Space.
        XCTAssertFalse(judge(report()).isVisible)
        // Another window of the app being on screen is not this window.
        XCTAssertFalse(judge(report(), windows: [serverWindow(), serverWindow(number: 43)], onScreen: [43]).isVisible)
    }

    func testVisibilityAcceptsAWindowThatStaysOrderedInOnAnotherSpaceAcrossTheSnapshot() {
        // The #3174 environment: another app's full-screen Space is active.
        XCTAssertEqual(judge(report(activeSpace: "0")), .orderedInOnAnotherSpace(number: 42))
    }

    func testVisibilityRejectsAStaleVisibleReport() {
        // A report from before this check said the window was visible on
        // another Space; it does not answer the check's challenge.
        let stale = report(activeSpace: "0", challenge: String(repeating: "0f", count: 16))
        XCTAssertFalse(judge(stale).isVisible)
        XCTAssertFalse(judge(stale, onScreen: [42]).isVisible)
        var unchallenged = report(activeSpace: "0")
        unchallenged.removeValue(forKey: "challenge")
        XCTAssertFalse(judge(unchallenged).isVisible)
        // The second report must answer the second challenge, not repeat the
        // first one.
        XCTAssertFalse(judge(report(activeSpace: "0"), after: report(activeSpace: "0")).isVisible)
    }

    func testVisibilityRejectsAWindowOrderedOutAroundTheSnapshot() {
        // Ordered out before the first fresh reading.
        XCTAssertFalse(judge(report(visible: "0", activeSpace: "0")).isVisible)
        // Ordered out after the first reading: WindowServer's full list still
        // has the window, but the reading taken after the snapshot says it is
        // gone, so the earlier visible reading does not carry the check.
        XCTAssertFalse(judge(report(activeSpace: "0"),
                             after: report(visible: "0", activeSpace: "0", challenge: secondChallenge)).isVisible)
        // Moved to the active Space yet still not listed on screen.
        XCTAssertFalse(judge(report(activeSpace: "0"),
                             after: report(activeSpace: "1", challenge: secondChallenge)).isVisible)
        // Replaced by another window.
        XCTAssertFalse(judge(report(activeSpace: "0"), after: stillOnAnotherSpace(number: 43)).isVisible)
        // Visible at both readings but ordered out and back in between them
        // (A-B-A): the transition count moved.
        XCTAssertFalse(judge(report(activeSpace: "0"),
                             after: report(activeSpace: "0", challenge: secondChallenge, transitions: 3)).isVisible)
        // No transition count at all is incomplete.
        var uncounted = report(activeSpace: "0")
        uncounted.removeValue(forKey: "transitions")
        XCTAssertFalse(judge(uncounted).isVisible)
        // No reading after the snapshot at all.
        XCTAssertFalse(MainWindowVisibility.evaluate(
            pid: 7, before: report(activeSpace: "0"), beforeChallenge: firstChallenge,
            after: nil, afterChallenge: secondChallenge,
            allWindows: [serverWindow()], onScreenNumbers: []).isVisible)
    }

    func testVisibilityRejectsEveryWindowThatIsNotARealVisibleMainWindow() {
        let cases: [(String, [String: String]?, [[String: Any]])] = [
            ("no report", nil, [serverWindow()]),
            ("incomplete report", ["challenge": firstChallenge, "number": "42", "transitions": "1"], [serverWindow()]),
            ("ordered out", report(visible: "0", activeSpace: "0"), [serverWindow()]),
            ("unknown to WindowServer", report(activeSpace: "0"), [serverWindow(number: 43)]),
            ("another process", report(activeSpace: "0"), [serverWindow(pid: 8)]),
            ("not a normal window", report(activeSpace: "0"), [serverWindow(layer: 3)]),
            ("transparent", report(activeSpace: "0"), [serverWindow(alpha: 0)]),
            ("too narrow", report(activeSpace: "0"), [serverWindow(width: 479)]),
            ("too short", report(activeSpace: "0"), [serverWindow(height: 319)]),
        ]
        for (name, report, windows) in cases {
            // Rejected whether or not WindowServer lists the window on screen.
            for onScreen in [Set<Int>(), [42]] {
                XCTAssertFalse(judge(report, windows: windows, onScreen: onScreen).isVisible,
                               "\(name) must not count as a visible window")
            }
        }
    }

    #if DEBUG
    func testReportSecretIsAHexValueOfBoundedLength() {
        XCTAssertNil(TestLaunch.reportSecret(environment: [:]))
        XCTAssertNil(TestLaunch.reportSecret(environment: ["CIAOBOT_MAC_TEST_REPORT_SECRET": "abc"]))
        XCTAssertNil(TestLaunch.reportSecret(environment: ["CIAOBOT_MAC_TEST_REPORT_SECRET": String(repeating: "g", count: 64)]))
        XCTAssertNil(TestLaunch.reportSecret(environment: ["CIAOBOT_MAC_TEST_REPORT_SECRET": String(repeating: "a", count: 129)]))
        let secret = String(repeating: "0f", count: 32)
        XCTAssertEqual(TestLaunch.reportSecret(environment: ["CIAOBOT_MAC_TEST_REPORT_SECRET": secret]), secret)
    }

    func testWindowStateReportCarriesTheStateInTheQueryAndTheSecretOnlyInAHeader() throws {
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:9"))
        let secret = String(repeating: "ab", count: 32)
        let state = TestWindowState(number: 42, visible: true, onActiveSpace: false, width: 1100, height: 760)
        let request = try XCTUnwrap(state.reportRequest(origin: origin, sequence: 7, transitions: 3, challenge: firstChallenge, secret: secret))
        let url = try XCTUnwrap(request.url)
        let components = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false))
        XCTAssertEqual(components.scheme, "http")
        XCTAssertEqual(components.host, "127.0.0.1")
        XCTAssertEqual(components.port, 9)
        XCTAssertEqual(components.path, "/test/window-state")
        XCTAssertEqual(components.queryItems, [
            URLQueryItem(name: "seq", value: "7"),
            URLQueryItem(name: "challenge", value: firstChallenge),
            URLQueryItem(name: "transitions", value: "3"),
            URLQueryItem(name: "number", value: "42"),
            URLQueryItem(name: "visible", value: "1"),
            URLQueryItem(name: "activeSpace", value: "0"),
            URLQueryItem(name: "width", value: "1100"),
            URLQueryItem(name: "height", value: "760"),
        ])
        XCTAssertFalse(url.absoluteString.contains(secret), "The secret must never be in the URL.")
        XCTAssertEqual(request.value(forHTTPHeaderField: TestWindowState.secretHeader), secret)
        XCTAssertFalse(request.httpShouldHandleCookies)
    }

    func testOnlyAHexChallengeOfBoundedLengthIsEchoed() {
        XCTAssertEqual(TestWindowState.validChallenge(firstChallenge), firstChallenge)
        XCTAssertNil(TestWindowState.validChallenge(nil))
        XCTAssertNil(TestWindowState.validChallenge("abc"))
        XCTAssertNil(TestWindowState.validChallenge(String(repeating: "a", count: 65)))
        XCTAssertNil(TestWindowState.validChallenge(String(repeating: "a", count: 31) + "&x=1"))
    }

    /// Holds the reporter's sends until the test completes them.
    @MainActor
    private final class HeldSends {
        var requests: [URLRequest] = []
        var completions: [@MainActor @Sendable (TestWindowReportOutcome) -> Void] = []

        func send(_ request: URLRequest, _ completion: @escaping @MainActor @Sendable (TestWindowReportOutcome) -> Void) {
            requests.append(request)
            completions.append(completion)
        }

        func complete(_ index: Int, _ outcome: TestWindowReportOutcome) { completions[index](outcome) }

        func report(_ index: Int) -> [String: String] {
            let items = requests[index].url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false) }?.queryItems ?? []
            return Dictionary(items.map { ($0.name, $0.value ?? "") }, uniquingKeysWith: { _, last in last })
        }

        func query(_ index: Int, _ name: String) -> String? {
            requests[index].url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false) }?
                .queryItems?.first { $0.name == name }?.value
        }
    }

    @MainActor
    private func offscreenWindow() -> NSWindow {
        // Never ordered in: no test shows a window.
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 600, height: 400),
                              styleMask: [.titled], backing: .buffered, defer: true)
        window.isReleasedWhenClosed = false
        return window
    }

    @MainActor
    func testWindowReporterSendsTheCurrentStateOneOrderedReportAtATime() throws {
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:9"))
        let window = offscreenWindow()
        var current: NSWindow?
        let held = HeldSends()
        let reporter = TestWindowReporter(origin: origin, secret: String(repeating: "a", count: 32),
                                          mainWindow: { current }, send: held.send)

        reporter.poll()
        XCTAssertTrue(held.requests.isEmpty, "Nothing is reported before the main window exists.")

        current = window
        reporter.poll()
        XCTAssertEqual(held.requests.count, 1)
        XCTAssertEqual(held.query(0, "seq"), "1")
        XCTAssertEqual(held.query(0, "challenge"), "", "No challenge has been received yet.")

        // While a report is in flight nothing else starts, so two requests can
        // never race each other to the fixture.
        window.setContentSize(NSSize(width: 650, height: 400))
        reporter.poll()
        XCTAssertEqual(held.requests.count, 1, "A second report must not start while one is in flight.")

        // The next report carries a higher sequence number and the state read
        // at that tick, and echoes the challenge the fixture handed out.
        held.complete(0, .accepted(challenge: firstChallenge))
        reporter.poll()
        XCTAssertEqual(held.requests.count, 2)
        XCTAssertEqual(held.query(1, "seq"), "2")
        XCTAssertEqual(held.query(1, "width"), "650")
        XCTAssertEqual(held.query(1, "challenge"), firstChallenge)

        // An unchanged, accepted state is still reported on the next tick, so
        // a fresh challenge is always answered.
        held.complete(1, .accepted(challenge: secondChallenge))
        reporter.poll()
        XCTAssertEqual(held.requests.count, 3)
        XCTAssertEqual(held.query(2, "seq"), "3")
        XCTAssertEqual(held.query(2, "width"), "650")
        XCTAssertEqual(held.query(2, "challenge"), secondChallenge)

        // A failure keeps the last challenge; an answer without one does too.
        held.complete(2, .failed)
        reporter.poll()
        XCTAssertEqual(held.query(3, "challenge"), secondChallenge)
        held.complete(3, .accepted(challenge: nil))
        reporter.poll()
        XCTAssertEqual(held.query(4, "seq"), "5")
        XCTAssertEqual(held.query(4, "challenge"), secondChallenge)
    }

    /// A is accepted; B reaches the fixture but its answer is lost; the window
    /// returns to A before the failure is known. The fixture's newest record
    /// is B, so the reporter must send A again rather than treat it as
    /// already delivered.
    @MainActor
    func testWindowReporterCorrectsAFixtureLeftOnAStateWhoseAnswerWasLost() throws {
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:9"))
        let window = offscreenWindow()
        let held = HeldSends()
        let reporter = TestWindowReporter(origin: origin, secret: String(repeating: "a", count: 32),
                                          mainWindow: { window }, send: held.send)
        var fixtureNewest: [String: String] = [:]
        func record(_ index: Int) {
            let items = held.requests[index].url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false) }?.queryItems ?? []
            fixtureNewest = Dictionary(items.map { ($0.name, $0.value ?? "") }, uniquingKeysWith: { _, last in last })
        }

        reporter.poll()                                  // A
        record(0)
        held.complete(0, .accepted(challenge: nil))

        window.setContentSize(NSSize(width: 650, height: 400))
        reporter.poll()                                  // B
        record(1)                                        // the fixture has B ...
        window.setContentSize(NSSize(width: 600, height: 400))
        held.complete(1, .failed)                        // ... but the answer was lost
        XCTAssertEqual(fixtureNewest["width"], "650")

        reporter.poll()
        XCTAssertEqual(held.requests.count, 3, "The returned-to state A must be reported again.")
        record(2)
        XCTAssertEqual(fixtureNewest["width"], "600")
        XCTAssertEqual(fixtureNewest["seq"], "3")
    }

    @MainActor
    func testWindowReporterStopsAfterItsReportBound() throws {
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:9"))
        let window = offscreenWindow()
        let held = HeldSends()
        let reporter = TestWindowReporter(origin: origin, secret: String(repeating: "a", count: 32),
                                          mainWindow: { window }, send: held.send)
        for step in 0..<(TestWindowReporter.maximumReports + 5) {
            reporter.poll()
            if step < held.completions.count { held.complete(step, .accepted(challenge: nil)) }
        }
        XCTAssertEqual(held.requests.count, TestWindowReporter.maximumReports)
    }

    /// A real window that can be ordered in without anything appearing: fully
    /// transparent, far outside every display, never key or main.
    @MainActor
    private func invisibleOrderableWindow() -> NSWindow {
        _ = NSApplication.shared
        let window = NSWindow(contentRect: NSRect(x: -30000, y: -30000, width: 600, height: 400),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.alphaValue = 0
        window.ignoresMouseEvents = true
        window.isReleasedWhenClosed = false
        return window
    }

    @MainActor
    func testTransitionCounterCatchesARealOrderOutAndBackInThatSamplingMisses() {
        let window = invisibleOrderableWindow()
        defer { window.orderOut(nil) }
        window.orderFrontRegardless()
        let counter = TestWindowTransitionCounter()
        defer { counter.stop() }
        counter.track(window)
        XCTAssertEqual(counter.count, 0)
        counter.track(window)
        XCTAssertEqual(counter.count, 0, "Tracking the same window again is not a transition.")

        // A, B, A: visible, ordered out, visible again, with no sample in
        // between. Both ends read visible; only the observer sees the change.
        XCTAssertTrue(window.isVisible)
        window.orderOut(nil)
        window.orderFrontRegardless()
        XCTAssertTrue(window.isVisible)
        XCTAssertEqual(counter.count, 2, "Each real order-out and order-in is counted as it happens.")

        window.close()
        XCTAssertGreaterThan(counter.count, 2, "Closing the window is counted.")

        let replacement = invisibleOrderableWindow()
        let beforeReplacement = counter.count
        counter.track(replacement)
        XCTAssertEqual(counter.count, beforeReplacement + 1, "A different main window is counted.")
    }

    /// End to end through the reporter with a real window: the window is
    /// ordered out and back in between the two fresh readings around the
    /// snapshot, so the check must fail even though both readings say visible.
    @MainActor
    func testReporterTransitionCountMakesTheCheckRejectAnOrderOutBetweenReadings() throws {
        let origin = try XCTUnwrap(Origin("http://127.0.0.1:9"))
        let window = invisibleOrderableWindow()
        defer { window.orderOut(nil) }
        window.orderFrontRegardless()
        let held = HeldSends()
        let reporter = TestWindowReporter(origin: origin, secret: String(repeating: "a", count: 32),
                                          mainWindow: { window }, send: held.send)
        defer { reporter.stop() }

        func readings(orderOutBetween: Bool) -> ([String: String], [String: String]) {
            let start = held.requests.count
            reporter.poll()
            held.complete(start, .accepted(challenge: firstChallenge))
            reporter.poll()                              // answers the first challenge
            held.complete(start + 1, .accepted(challenge: secondChallenge))
            if orderOutBetween {
                window.orderOut(nil)                     // during the snapshot
                window.orderFrontRegardless()
            }
            reporter.poll()                              // answers the second challenge
            held.complete(start + 2, .accepted(challenge: nil))
            // The test process cannot open a full-screen Space, so the
            // readings are marked as on another Space, the #3174 case.
            var before = held.report(start + 1), after = held.report(start + 2)
            before["activeSpace"] = "0"
            after["activeSpace"] = "0"
            return (before, after)
        }

        func verdict(_ pair: ([String: String], [String: String])) -> MainWindowVisibility {
            MainWindowVisibility.evaluate(
                pid: 7, before: pair.0, beforeChallenge: firstChallenge, after: pair.1, afterChallenge: secondChallenge,
                allWindows: [serverWindow(number: window.windowNumber)], onScreenNumbers: [])
        }

        let steady = readings(orderOutBetween: false)
        XCTAssertEqual(steady.0["visible"], "1")
        XCTAssertEqual(verdict(steady), .orderedInOnAnotherSpace(number: window.windowNumber))

        let flickered = readings(orderOutBetween: true)
        XCTAssertEqual(flickered.0["visible"], "1")
        XCTAssertEqual(flickered.1["visible"], "1", "Both readings say visible.")
        XCTAssertNotEqual(flickered.0["transitions"], flickered.1["transitions"])
        XCTAssertFalse(verdict(flickered).isVisible, "An order-out between the readings must fail the check.")
    }

    /// Sends one report over a real transport and returns the outcome.
    private func deliver(_ request: URLRequest, over transport: TestWindowReportTransport) -> TestWindowReportOutcome {
        let done = expectation(description: "report delivered")
        let result = OutcomeBox()
        transport.send(request) { outcome in
            result.set(outcome)
            done.fulfill()
        }
        wait(for: [done], timeout: 5)
        return result.value
    }

    private final class OutcomeBox: @unchecked Sendable {
        private let lock = NSLock()
        private var stored = TestWindowReportOutcome.failed
        var value: TestWindowReportOutcome { lock.lock(); defer { lock.unlock() }; return stored }
        func set(_ value: TestWindowReportOutcome) { lock.lock(); stored = value; lock.unlock() }
    }

    private func reportRequest(_ fixture: IsolatedFixtureServer, sequence: Int, width: Int,
                               challenge: String? = nil, secret: String? = nil) throws -> URLRequest {
        let origin = try XCTUnwrap(Origin(fixture.origin))
        let state = TestWindowState(number: 42, visible: true, onActiveSpace: true, width: width, height: 760)
        return try XCTUnwrap(state.reportRequest(origin: origin, sequence: sequence, transitions: 0, challenge: challenge,
                                                 secret: secret ?? fixture.reportSecret))
    }

    func testFixtureKeepsTheNewestReportWhenReportsArriveOutOfOrder() throws {
        let fixture = try IsolatedFixtureServer()
        defer { fixture.shutdown() }
        let transport = TestWindowReportTransport()
        defer { transport.invalidate() }

        XCTAssertEqual(deliver(try reportRequest(fixture, sequence: 2, width: 1100), over: transport), .accepted(challenge: nil))
        XCTAssertEqual(deliver(try reportRequest(fixture, sequence: 1, width: 10), over: transport), .accepted(challenge: nil))
        XCTAssertEqual(fixture.latestWindowReport?["seq"], "2")
        XCTAssertEqual(fixture.latestWindowReport?["width"], "1100", "A stale report must not replace a newer one.")
    }

    func testFixtureHandsOutChallengesAndFindsOnlyReportsAnsweringThem() throws {
        let fixture = try IsolatedFixtureServer()
        defer { fixture.shutdown() }
        let transport = TestWindowReportTransport()
        defer { transport.invalidate() }

        let challenge = fixture.issueChallenge()
        XCTAssertEqual(deliver(try reportRequest(fixture, sequence: 1, width: 1100), over: transport),
                       .accepted(challenge: challenge), "An accepted report is answered with the current challenge.")
        XCTAssertNil(fixture.waitForReport(answering: challenge, timeout: 0.1),
                     "A report that does not echo the challenge is not fresh.")
        XCTAssertEqual(deliver(try reportRequest(fixture, sequence: 2, width: 900, challenge: challenge), over: transport),
                       .accepted(challenge: challenge))
        XCTAssertEqual(fixture.waitForReport(answering: challenge, timeout: 0.1)?["width"], "900")

        let next = fixture.issueChallenge()
        XCTAssertNil(fixture.waitForReport(answering: next, timeout: 0.1), "Earlier reports never answer a new challenge.")
    }

    func testFixtureRejectsForgedReports() throws {
        let fixture = try IsolatedFixtureServer()
        defer { fixture.shutdown() }
        let transport = TestWindowReportTransport()
        defer { transport.invalidate() }
        let challenge = fixture.issueChallenge()

        var unsigned = try reportRequest(fixture, sequence: 1, width: 1100, challenge: challenge)
        unsigned.setValue(nil, forHTTPHeaderField: TestWindowState.secretHeader)
        XCTAssertEqual(deliver(unsigned, over: transport), .failed)
        let wrong = String(fixture.reportSecret.reversed())
        XCTAssertEqual(deliver(try reportRequest(fixture, sequence: 2, width: 1100, challenge: challenge, secret: wrong),
                               over: transport), .failed)
        var secretInQuery = unsigned
        secretInQuery.url = URL(string: unsigned.url!.absoluteString + "&secret=\(fixture.reportSecret)")
        XCTAssertEqual(deliver(secretInQuery, over: transport), .failed)
        XCTAssertNil(fixture.latestWindowReport, "A forged report must not be recorded.")
        XCTAssertNil(fixture.waitForReport(answering: challenge, timeout: 0.1), "A forged report must not answer a challenge.")
        XCTAssertEqual(fixture.rejectedReportCount, 3)

        XCTAssertEqual(deliver(try reportRequest(fixture, sequence: 3, width: 1100), over: transport), .accepted(challenge: challenge))
        XCTAssertEqual(fixture.latestWindowReport?["seq"], "3")
        XCTAssertNil(fixture.latestWindowReport?[TestWindowState.secretHeader], "The secret is not stored with a report.")
    }

    func testTransportRefusesRedirectsAwayFromTheTestOrigin() throws {
        let elsewhere = try IsolatedFixtureServer()
        defer { elsewhere.shutdown() }
        let redirecting = try IsolatedFixtureServer(
            redirectWindowReportsTo: "\(elsewhere.origin)/test/window-state?seq=9&number=42&visible=1&activeSpace=1&width=1100&height=760"
        )
        defer { redirecting.shutdown() }
        let transport = TestWindowReportTransport()
        defer { transport.invalidate() }

        XCTAssertEqual(deliver(try reportRequest(redirecting, sequence: 1, width: 1100), over: transport), .failed,
                       "A redirect is not an accepted report.")
        XCTAssertEqual(redirecting.observedPaths, ["/test/window-state"])
        XCTAssertFalse(elsewhere.waitForPath("/test/window-state", timeout: 0.5),
                       "The redirect target was contacted.")
        XCTAssertTrue(elsewhere.observedPaths.isEmpty)
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

        let window = waitForWindow(pid: process.processIdentifier, fixture: fixture, timeout: 10)
        XCTAssertTrue(window.isVisible, "The app did not create a visible native window: \(window)")
        if case .orderedInOnAnotherSpace(let number) = window {
            // Not a failure: another app's full-screen Space is active, so
            // WindowServer's on-screen list cannot include this window.
            print("GuiLaunchTests: main window \(number) is visible on a Space other than the active one.")
        }
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
        environment["CIAOBOT_MAC_TEST_REPORT_SECRET"] = fixture.reportSecret
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

    /// Repeats the fresh check (see `MainWindowVisibility`) until it passes
    /// or the timeout ends, and returns the last verdict.
    private func waitForWindow(pid: Int32, fixture: IsolatedFixtureServer, timeout: TimeInterval) -> MainWindowVisibility {
        let deadline = Date().addingTimeInterval(timeout)
        var result = MainWindowVisibility.missing("the check did not run")
        repeat {
            result = freshMainWindowVisibility(pid: pid, fixture: fixture, deadline: deadline)
            if result.isVisible { return result }
            usleep(100_000)
        } while Date() < deadline
        return result
    }

    private func freshMainWindowVisibility(pid: Int32, fixture: IsolatedFixtureServer, deadline: Date) -> MainWindowVisibility {
        // Each wait is bounded; the app reports every 0.1 s, so a fresh answer
        // normally arrives within two ticks.
        func budget() -> TimeInterval { max(0.2, min(2, deadline.timeIntervalSinceNow)) }
        let beforeChallenge = fixture.issueChallenge()
        let before = fixture.waitForReport(answering: beforeChallenge, timeout: budget())
        // Snapshot WindowServer only after the first fresh reading arrived.
        let all = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
        let onScreen = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
        let onScreenNumbers = Set(onScreen.compactMap { ($0[kCGWindowNumber as String] as? NSNumber)?.intValue })
        // A reading taken after the snapshot, needed when the window is not
        // on screen: it must still be visible on another Space.
        let afterChallenge = fixture.issueChallenge()
        let needsAfter = before.flatMap { $0["number"] }.flatMap(Int.init).map { !onScreenNumbers.contains($0) } ?? false
        let after = needsAfter ? fixture.waitForReport(answering: afterChallenge, timeout: budget()) : nil
        return MainWindowVisibility.evaluate(
            pid: pid,
            before: before, beforeChallenge: beforeChallenge,
            after: after, afterChallenge: afterChallenge,
            allWindows: all, onScreenNumbers: onScreenNumbers
        )
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
