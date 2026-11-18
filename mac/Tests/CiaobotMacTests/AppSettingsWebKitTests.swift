import XCTest
import Combine
import WebKit
import AppKit

@testable import CiaobotMac

/// Drives the real `ciaobotAppSettings` reply handler in a real `WKWebView`.
@MainActor
final class AppSettingsWebKitTests: XCTestCase {
    private static var retained: [AnyObject] = []
    private let origin = Origin("https://example.com")!

    private struct Harness {
        let coordinator: WebWindow.Coordinator
        let webView: WKWebView
        let fake: AppSettingsFakeNotifications
        let settings: AppSettingsFakeSystemSettings
    }

    private func makeHarness(withBridge: Bool = true, state: ChatNotificationState = .off) throws -> Harness {
        let fake = AppSettingsFakeNotifications(state)
        let settings = AppSettingsFakeSystemSettings()
        var representable = WebWindow(origin: origin)
        if withBridge {
            let bridge = AppSettingsBridge()
            bridge.bind(notifications: fake, systemSettings: settings)
            representable.appSettings = bridge
        }
        let coordinator = representable.makeCoordinator()
        let webView = WebWindow.Coordinator.makeWebView(origin: origin, coordinator: coordinator)
        webView.frame = NSRect(x: 0, y: 0, width: 400, height: 300)
        let window = NSWindow(contentRect: webView.frame, styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = webView
        window.orderFrontRegardless()
        Self.retained.append(window)
        Self.retained.append(coordinator)
        return Harness(coordinator: coordinator, webView: webView, fake: fake, settings: settings)
    }

    private func load(_ harness: Harness, body: String = "<p>settings</p>") throws {
        harness.webView.loadHTMLString(
            "<!doctype html><html><body>\(body)<script>window.__ready = true;</script></body></html>",
            baseURL: origin.url
        )
        let deadline = Date().addingTimeInterval(20)
        while Date() < deadline {
            if js(harness.webView, "document.readyState === 'complete' && window.__ready === true") as? Bool == true { return }
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
        XCTFail("The page did not become ready")
    }

    private func js(_ webView: WKWebView, _ script: String) -> Any? {
        var result: Any?
        var done = false
        webView.evaluateJavaScript(script) { value, _ in
            result = value
            done = true
        }
        let deadline = Date().addingTimeInterval(2)
        while !done && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
        return result
    }

    /// Posts `payload` (a JS expression) and returns the settled outcome as
    /// JSON: `{"ok": <reply>}` or `{"err": <rejection>}`.
    private func post(_ webView: WKWebView, _ payload: String, in target: String = "window") throws -> [String: Any] {
        _ = js(webView, """
        window.__outcome = null;
        \(target).webkit.messageHandlers.ciaobotAppSettings.postMessage(\(payload)).then(
          v => { window.__outcome = JSON.stringify({ ok: v }); },
          e => { window.__outcome = JSON.stringify({ err: (e && e.message !== undefined) ? e.message : String(e) }); });
        """)
        let deadline = Date().addingTimeInterval(3)
        while Date() < deadline {
            if let raw = js(webView, "window.__outcome") as? String {
                return try XCTUnwrap(JSONSerialization.jsonObject(with: Data(raw.utf8)) as? [String: Any])
            }
            RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        }
        XCTFail("The handler never settled")
        return [:]
    }

    private func assertRefused(_ outcome: [String: Any], file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertEqual(outcome["err"] as? String, "refused", "\(outcome)", file: file, line: line)
        XCTAssertNil(outcome["ok"], file: file, line: line)
    }

    func testHandlerIsPresentOnlyWithABridge() throws {
        let with = try makeHarness(withBridge: true)
        try load(with)
        XCTAssertEqual(js(with.webView, "typeof window.webkit.messageHandlers.ciaobotAppSettings") as? String, "object")

        let without = try makeHarness(withBridge: false)
        try load(without)
        XCTAssertEqual(js(without.webView, "typeof window.webkit.messageHandlers.ciaobotAppSettings") as? String, "undefined")
    }

    func testGetResolvesToTheSnapshotWithoutAGesture() throws {
        let h = try makeHarness(state: .on)
        try load(h)
        let outcome = try post(h.webView, "{op: 'get'}")
        let snapshot = try XCTUnwrap(outcome["ok"] as? [String: Any], "\(outcome)")
        XCTAssertEqual(snapshot["v"] as? Int, 1)
        let notifications = try XCTUnwrap(snapshot["notifications"] as? [String: Any])
        XCTAssertEqual(notifications["state"] as? String, "on")
        XCTAssertEqual(notifications["action"] as? String, "toggle")
        XCTAssertEqual(h.fake.actionCount, 0)
    }

    func testNotificationsWithoutAGestureIsRefused() throws {
        let h = try makeHarness()
        try load(h)
        assertRefused(try post(h.webView, "{op: 'notifications'}"))
        XCTAssertEqual(h.fake.actionCount, 0)
    }

    func testNotificationsWithAGestureActsOnce() throws {
        let h = try makeHarness()
        try load(h)
        h.coordinator.gestureGate.record()
        let outcome = try post(h.webView, "{op: 'notifications'}")
        XCTAssertNotNil(outcome["ok"], "\(outcome)")
        XCTAssertEqual(h.fake.actionCount, 1)
        // The gesture is consumed: a second call needs a new click.
        assertRefused(try post(h.webView, "{op: 'notifications'}"))
        XCTAssertEqual(h.fake.actionCount, 1)
    }

    func testDeniedOpensSystemSettingsWithAGesture() throws {
        let h = try makeHarness(state: .denied)
        try load(h)
        h.coordinator.gestureGate.record()
        XCTAssertNotNil(try post(h.webView, "{op: 'notifications'}")["ok"])
        XCTAssertEqual(h.settings.notificationSettingsCount, 1)
        XCTAssertEqual(h.fake.actionCount, 0)
    }

    func testMalformedBodiesAreRefusedEvenWithAGesture() throws {
        let h = try makeHarness()
        try load(h)
        let bodies = [
            "{op: 'notifications', extra: 1}",
            "{op: 'get', extra: 1}",
            "{op: 1}",
            "{op: null}",
            "{}",
            "{op: 'checkForUpdates'}",
            "{op: 'nope'}",
            "'notifications'",
            "['notifications']",
            "null",
        ]
        for body in bodies {
            h.coordinator.gestureGate.record()
            assertRefused(try post(h.webView, body))
        }
        XCTAssertEqual(h.fake.actionCount, 0)
    }

    func testIframeCallIsRefusedEvenWithAGesture() throws {
        let h = try makeHarness()
        try load(h, body: "<iframe id='f' srcdoc='<p>frame</p>'></iframe>")
        let frameReady = js(h.webView, "document.getElementById('f').contentDocument.readyState") as? String
        XCTAssertNotNil(frameReady)
        h.coordinator.gestureGate.record()
        let outcome = try post(
            h.webView, "{op: 'notifications'}",
            in: "document.getElementById('f').contentWindow"
        )
        assertRefused(outcome)
        XCTAssertEqual(h.fake.actionCount, 0)
    }

    func testSubFrameAndForeignOriginAreRefusedByTheGuard() throws {
        let h = try makeHarness()
        try load(h)
        let body: [String: Any] = ["op": "notifications"]
        let view = h.webView
        func call(main: Bool = true, host: String = "example.com", view: WKWebView? = view) -> Bool {
            h.coordinator.gestureGate.record()
            if case .success = h.coordinator.handleAppSettingsMessage(
                isMainFrame: main, scheme: "https", host: host, port: 443, view: view, body: body
            ) { return true }
            return false
        }
        XCTAssertFalse(call(main: false))
        XCTAssertFalse(call(host: "evil.example"))
        XCTAssertFalse(call(view: nil))
        XCTAssertFalse(call(view: WKWebView()))
        XCTAssertEqual(h.fake.actionCount, 0)
        XCTAssertTrue(call(), "The same call from the real frame must succeed, so the refusals above are the guards.")
        XCTAssertEqual(h.fake.actionCount, 1)
    }

    func testUncommittedDocumentIsRefusedUntilCommit() throws {
        let h = try makeHarness()
        try load(h)
        func get() -> Bool {
            if case .success = h.coordinator.handleAppSettingsMessage(
                isMainFrame: true, scheme: "https", host: "example.com", port: 443, view: h.webView, body: ["op": "get"]
            ) { return true }
            return false
        }
        XCTAssertTrue(get())
        // A new navigation has started but not committed: the old document
        // must not reach native in between.
        h.coordinator.webView(h.webView, didStartProvisionalNavigation: nil)
        XCTAssertFalse(get())
    }

    func testChangeSignalReachesThePage() throws {
        let h = try makeHarness()
        try load(h)
        _ = js(h.webView, "window.__changes = 0; window.addEventListener('ciaobot-app-settings-changed', () => { window.__changes += 1; });")
        h.fake.subject.send(.on)
        let deadline = Date().addingTimeInterval(3)
        while Date() < deadline, (js(h.webView, "window.__changes") as? Int ?? 0) < 1 {
            RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        }
        XCTAssertEqual(js(h.webView, "window.__changes") as? Int, 1)
    }

    func testChangeSignalIsHeldBackWhileADocumentIsUncommitted() throws {
        let h = try makeHarness()
        try load(h)
        _ = js(h.webView, "window.__changes = 0; window.addEventListener('ciaobot-app-settings-changed', () => { window.__changes += 1; });")
        h.coordinator.webView(h.webView, didStartProvisionalNavigation: nil)
        h.fake.subject.send(.on)
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        XCTAssertEqual(js(h.webView, "window.__changes") as? Int, 0)
    }

    func testCallAfterReleaseIsRefused() throws {
        let h = try makeHarness()
        try load(h)
        h.coordinator.prepareForRelease()
        h.coordinator.gestureGate.record()
        if case .success = h.coordinator.handleAppSettingsMessage(
            isMainFrame: true, scheme: "https", host: "example.com", port: 443, view: h.webView, body: ["op": "get"]
        ) { XCTFail("A released coordinator must refuse") }
        // WebKit also drops the handler itself, so the page cannot post at all.
        XCTAssertEqual(js(h.webView, "(!window.webkit || !window.webkit.messageHandlers || !window.webkit.messageHandlers.ciaobotAppSettings) ? 'gone' : 'present'") as? String, "gone")
        XCTAssertEqual(h.fake.actionCount, 0)
    }
}
