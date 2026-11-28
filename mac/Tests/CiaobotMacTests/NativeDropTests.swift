import XCTest
import AppKit
import WebKit

@testable import CiaobotMac

/// "Use in place": the grant file the engine consumes, the one-shot drop
/// capture, and the `ciaobotNativeDrop` reply handler.
@MainActor
final class NativeDropTests: XCTestCase {
    private static var retained: [AnyObject] = []
    private let origin = Origin("https://127.0.0.1:8443")!
    private var temp: URL!

    override func setUpWithError() throws {
        temp = FileManager.default.temporaryDirectory
            .appendingPathComponent("native-drop-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: temp)
    }

    private func pointer(runtimeRoot: String) throws -> URL {
        let url = temp.appendingPathComponent("workspace.json")
        let data = try JSONSerialization.data(withJSONObject: ["schema": 1, "workspace": temp.path, "runtime_root": runtimeRoot])
        try data.write(to: url)
        return url
    }

    private func dropped(_ name: String, directory: Bool = false) throws -> URL {
        let url = temp.appendingPathComponent(name, isDirectory: directory)
        if directory {
            try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        } else {
            try Data("x".utf8).write(to: url)
        }
        return url
    }

    // MARK: grant file

    func testWriterWritesAnOwnerOnlyGrantTheEngineReads() throws {
        let runtime = temp.appendingPathComponent(".runtime", isDirectory: true)
        let writer = NativeDropGrantWriter(pointer: try pointer(runtimeRoot: runtime.path))
        let file = try dropped("report.pdf")
        let folder = try dropped("data set", directory: true)
        let id = UUID()
        let now = Date(timeIntervalSince1970: 1_800_000_000)

        let grant = try writer.write([file, folder], now: now, id: id)

        XCTAssertEqual(grant.id, id.uuidString.lowercased(), "the engine requires the canonical lower-case UUID")
        XCTAssertEqual(grant.names, ["report.pdf", "data set"])
        XCTAssertEqual(Set(grant.payload.keys), ["grantId", "names"], "the page never receives a path")
        let directory = runtime.appendingPathComponent("desktop-drop-grants")
        let grantFile = directory.appendingPathComponent("\(grant.id).json")
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: grantFile)) as? [String: Any])
        XCTAssertEqual(object["created_at"] as? Double, 1_800_000_000)
        XCTAssertEqual(object["paths"] as? [String], [file.standardizedFileURL.path, folder.standardizedFileURL.path])
        let fileMode = try FileManager.default.attributesOfItem(atPath: grantFile.path)[.posixPermissions] as? Int
        let dirMode = try FileManager.default.attributesOfItem(atPath: directory.path)[.posixPermissions] as? Int
        XCTAssertEqual(fileMode, 0o600)
        XCTAssertEqual(dirMode, 0o700)
        let leftovers = try FileManager.default.contentsOfDirectory(atPath: directory.path)
        XCTAssertEqual(leftovers, ["\(grant.id).json"], "the staging file is renamed into place")
    }

    func testWriterRefusesWithoutAnAbsoluteRuntimeRoot() throws {
        let file = try dropped("a.txt")
        let missing = NativeDropGrantWriter(pointer: temp.appendingPathComponent("absent.json"))
        XCTAssertThrowsError(try missing.write([file])) { XCTAssertEqual($0 as? NativeDropError, .noRuntimeRoot) }
        let relative = NativeDropGrantWriter(pointer: try pointer(runtimeRoot: ".runtime"))
        XCTAssertThrowsError(try relative.write([file])) { XCTAssertEqual($0 as? NativeDropError, .noRuntimeRoot) }
        let traversal = NativeDropGrantWriter(pointer: try pointer(runtimeRoot: temp.path + "/../elsewhere"))
        XCTAssertThrowsError(try traversal.write([file])) { XCTAssertEqual($0 as? NativeDropError, .noRuntimeRoot) }
    }

    func testWriterRefusesNonFileAndOversizedDrops() throws {
        let writer = NativeDropGrantWriter(pointer: try pointer(runtimeRoot: temp.appendingPathComponent(".runtime").path))
        XCTAssertThrowsError(try writer.write([])) { XCTAssertEqual($0 as? NativeDropError, .invalidPaths) }
        XCTAssertThrowsError(try writer.write([URL(string: "https://example.com/a.txt")!])) {
            XCTAssertEqual($0 as? NativeDropError, .invalidPaths)
        }
        let many = Array(repeating: try dropped("a.txt"), count: NativeDropGrantWriter.maxPaths + 1)
        XCTAssertThrowsError(try writer.write(many)) { XCTAssertEqual($0 as? NativeDropError, .invalidPaths) }
    }

    func testDisplayNameDropsControlCharacters() {
        XCTAssertEqual(NativeDropGrantWriter.displayName(URL(fileURLWithPath: "/tmp/line\nbreak.txt")), "linebreak.txt")
    }

    // MARK: capture

    func testCaptureIsSingleUseAndShortLived() {
        let capture = NativeDropCapture()
        let url = URL(fileURLWithPath: "/tmp/a.txt")
        let now = Date()
        capture.record([url], now: now)
        XCTAssertEqual(capture.take(now: now.addingTimeInterval(1)), [url])
        XCTAssertNil(capture.take(now: now.addingTimeInterval(2)), "a drop is handed out once")
        capture.record([url], now: now)
        XCTAssertNil(capture.take(now: now.addingTimeInterval(NativeDropCapture.validity + 1)))
    }

    func testFileURLsReadsOnlyFileURLsFromTheDragPasteboard() {
        let pasteboard = NSPasteboard(name: NSPasteboard.Name("ciaobot-native-drop-\(UUID().uuidString)"))
        defer { pasteboard.releaseGlobally() }
        pasteboard.clearContents()
        let file = URL(fileURLWithPath: "/tmp/report.pdf")
        pasteboard.writeObjects([file as NSURL, URL(string: "https://example.com/x")! as NSURL])
        XCTAssertEqual(FileDropWebView.fileURLs(on: pasteboard), [file])
    }

    // MARK: reply handler

    private struct Harness {
        let coordinator: WebWindow.Coordinator
        let webView: WKWebView
        let bridge: NativeDropBridge?
    }

    private func makeHarness(bridge: NativeDropBridge?) -> Harness {
        var representable = WebWindow(origin: origin)
        representable.nativeDrop = bridge
        let coordinator = representable.makeCoordinator()
        let webView = WebWindow.Coordinator.makeWebView(origin: origin, coordinator: coordinator)
        Self.retained.append(coordinator)
        Self.retained.append(webView)
        return Harness(coordinator: coordinator, webView: webView, bridge: bridge)
    }

    private func send(_ h: Harness, _ body: Any, main: Bool = true, port: Int = 8443) -> Result<Any, WebWindow.Coordinator.RefusedError>? {
        var outcome: Result<Any, WebWindow.Coordinator.RefusedError>?
        h.coordinator.handleNativeDropMessage(
            isMainFrame: main, scheme: "https", host: "127.0.0.1", port: port,
            view: h.webView, body: body
        ) { outcome = $0 }
        return outcome
    }

    private func grantPayload(_ result: Result<Any, WebWindow.Coordinator.RefusedError>?) -> [String: Any]? {
        guard case .success(let value)? = result else { return nil }
        return value as? [String: Any]
    }

    private func refused(_ result: Result<Any, WebWindow.Coordinator.RefusedError>?) -> Bool {
        if case .failure? = result { return true }
        return false
    }

    func testTheWebViewRecordsDropsForTheBridge() throws {
        let bridge = NativeDropBridge(writer: NativeDropGrantWriter(pointer: try pointer(runtimeRoot: temp.path)))
        let h = makeHarness(bridge: bridge)
        let view = try XCTUnwrap(h.webView as? FileDropWebView)
        let url = URL(fileURLWithPath: "/tmp/a.txt")
        view.onFileDrop?([url])
        XCTAssertEqual(bridge.capture.take(), [url])
    }

    func testGrantTurnsTheLastDropIntoAGrantOnce() throws {
        let runtime = temp.appendingPathComponent(".runtime")
        let bridge = NativeDropBridge(writer: NativeDropGrantWriter(pointer: try pointer(runtimeRoot: runtime.path)))
        let h = makeHarness(bridge: bridge)
        bridge.capture.record([try dropped("notes.md")])

        let payload = try XCTUnwrap(grantPayload(send(h, ["op": "grant"])))
        XCTAssertEqual(payload["names"] as? [String], ["notes.md"])
        let id = try XCTUnwrap(payload["grantId"] as? String)
        XCTAssertTrue(FileManager.default.fileExists(atPath: runtime.appendingPathComponent("desktop-drop-grants/\(id).json").path))

        guard case .success(let second)? = send(h, ["op": "grant"]) else { return XCTFail("expected a null reply") }
        XCTAssertTrue(second is NSNull, "no drop, no grant")
    }

    func testRequestsFromAnythingButTheMainFrameOfTheOriginAreRefused() throws {
        let bridge = NativeDropBridge(writer: NativeDropGrantWriter(pointer: try pointer(runtimeRoot: temp.path)))
        let h = makeHarness(bridge: bridge)
        let file = try dropped("a.txt")
        bridge.capture.record([file])
        XCTAssertTrue(refused(send(h, ["op": "grant"], main: false)))
        XCTAssertTrue(refused(send(h, ["op": "grant"], port: 9443)))
        XCTAssertTrue(refused(send(h, ["op": "grant", "path": "/etc/passwd"])), "a page cannot name a path")
        XCTAssertTrue(refused(send(h, ["op": "delete"])))
        XCTAssertTrue(refused(send(h, "grant")))
        XCTAssertEqual(bridge.capture.take(), [file], "a refused request leaves the drop untouched")
        XCTAssertTrue(refused(send(makeHarness(bridge: nil), ["op": "grant"])), "a window without the bridge refuses")
    }

    func testPickNeedsATrustedClickAndRepliesWithTheGrant() throws {
        var pickerCalls = 0
        let file = try dropped("picked.txt")
        let bridge = NativeDropBridge(
            writer: NativeDropGrantWriter(pointer: try pointer(runtimeRoot: temp.appendingPathComponent(".runtime").path)),
            picker: { completion in pickerCalls += 1; completion([file]) }
        )
        let h = makeHarness(bridge: bridge)

        XCTAssertTrue(refused(send(h, ["op": "pick"])))
        XCTAssertEqual(pickerCalls, 0, "no panel without a click")

        h.coordinator.handleTrustedActivationMessage(isMainFrame: true, scheme: "https", host: "127.0.0.1", port: 8443, body: "click")
        let payload = try XCTUnwrap(grantPayload(send(h, ["op": "pick"])))
        XCTAssertEqual(payload["names"] as? [String], ["picked.txt"])
        XCTAssertEqual(pickerCalls, 1)
        XCTAssertTrue(refused(send(h, ["op": "pick"])), "the click is spent")
    }

    func testCancelledPickRepliesNull() throws {
        let bridge = NativeDropBridge(
            writer: NativeDropGrantWriter(pointer: try pointer(runtimeRoot: temp.path)),
            picker: { completion in completion(nil) }
        )
        let h = makeHarness(bridge: bridge)
        h.coordinator.handleTrustedActivationMessage(isMainFrame: true, scheme: "https", host: "127.0.0.1", port: 8443, body: "click")
        guard case .success(let value)? = send(h, ["op": "pick"]) else { return XCTFail("expected a reply") }
        XCTAssertTrue(value is NSNull)
    }
}
