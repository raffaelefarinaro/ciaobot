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
        let updates: AppSettingsFakeUpdates
    }

    private func makeHarness(
        withBridge: Bool = true,
        state: ChatNotificationState = .off,
        updates: AppSettingsFakeUpdates? = nil
    ) throws -> Harness {
        let updates = updates ?? AppSettingsFakeUpdates()
        let fake = AppSettingsFakeNotifications(state)
        let settings = AppSettingsFakeSystemSettings()
        var representable = WebWindow(origin: origin)
        if withBridge {
            let bridge = AppSettingsBridge()
            bridge.bind(notifications: fake, systemSettings: settings, updates: updates)
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
        return Harness(coordinator: coordinator, webView: webView, fake: fake, settings: settings, updates: updates)
    }

    /// A trusted click from the main frame of the selected origin, delivered
    /// through the same entry the injected script's message reaches.
    private func click(_ h: Harness, main: Bool = true, body: Any = "click", at now: Date = Date()) {
        h.coordinator.handleTrustedActivationMessage(
            isMainFrame: main, scheme: "https", host: "example.com", port: 443, body: body, now: now
        )
    }

    private func notify(_ h: Harness, scheme: String = "https", port: Int = 443, now: Date = Date()) -> Bool {
        if case .success = h.coordinator.handleAppSettingsMessage(
            isMainFrame: true, scheme: scheme, host: "example.com", port: port,
            view: h.webView, body: ["op": "notifications"], now: now
        ) { return true }
        return false
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
        click(h)
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
        click(h)
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
            "{op: 'checkForUpdates', extra: 1}",
            "{op: 'setAutomaticUpdates', on: true, extra: 1}",
            "{op: 'nope'}",
            "'notifications'",
            "['notifications']",
            "null",
        ]
        for body in bodies {
            click(h)
            assertRefused(try post(h.webView, body))
        }
        XCTAssertEqual(h.fake.actionCount, 0)
    }

    func testSnapshotCarriesUpdatesWithoutAGesture() throws {
        let h = try makeHarness(updates: AppSettingsFakeUpdates(canCheck: true, automatic: true))
        try load(h)
        let outcome = try post(h.webView, "{op: 'get'}")
        let snapshot = try XCTUnwrap(outcome["ok"] as? [String: Any], "\(outcome)")
        let updates = try XCTUnwrap(snapshot["updates"] as? [String: Any])
        XCTAssertEqual(updates["available"] as? Bool, true)
        XCTAssertEqual(updates["automatic"] as? Bool, true)
        XCTAssertEqual(updates["canCheck"] as? Bool, true)
        XCTAssertEqual(h.updates.checkCount, 0)
    }

    func testCheckForUpdatesWithoutAGestureIsRefused() throws {
        let h = try makeHarness()
        try load(h)
        assertRefused(try post(h.webView, "{op: 'checkForUpdates'}"))
        XCTAssertEqual(h.updates.checkCount, 0)
    }

    func testCheckForUpdatesWithAGestureActsOnce() throws {
        let h = try makeHarness()
        try load(h)
        click(h)
        XCTAssertNotNil(try post(h.webView, "{op: 'checkForUpdates'}")["ok"])
        XCTAssertEqual(h.updates.checkCount, 1)
        assertRefused(try post(h.webView, "{op: 'checkForUpdates'}"))
        XCTAssertEqual(h.updates.checkCount, 1)
    }

    /// Available but busy: the check is refused, so the click must survive it
    /// and still back the automatic-install change that is allowed.
    func testCheckForUpdatesWhileBusyLeavesTheClickUnspent() throws {
        let h = try makeHarness(updates: AppSettingsFakeUpdates(canCheck: false))
        try load(h)
        click(h)
        assertRefused(try post(h.webView, "{op: 'checkForUpdates'}"))
        XCTAssertEqual(h.updates.checkCount, 0)
        XCTAssertNotNil(try post(h.webView, "{op: 'setAutomaticUpdates', on: true}")["ok"])
        XCTAssertEqual(h.updates.automaticSets, [true])
        assertRefused(try post(h.webView, "{op: 'checkForUpdates'}"))
        XCTAssertEqual(h.updates.checkCount, 0)
    }

    /// The converse: an available updater refuses a malformed automatic-install
    /// request, and that refusal must leave the click for the check.
    func testRefusedSetAutomaticUpdatesLeavesTheClickUnspent() throws {
        let h = try makeHarness(updates: AppSettingsFakeUpdates(canCheck: true))
        try load(h)
        click(h)
        assertRefused(try post(h.webView, "{op: 'setAutomaticUpdates', on: 'true'}"))
        XCTAssertTrue(h.updates.automaticSets.isEmpty)
        XCTAssertNotNil(try post(h.webView, "{op: 'checkForUpdates'}")["ok"])
        XCTAssertEqual(h.updates.checkCount, 1)
    }

    func testUnavailableUpdaterRefusesBothUpdateOps() throws {
        let h = try makeHarness(updates: AppSettingsFakeUpdates(reason: "No feed."))
        try load(h)
        click(h)
        assertRefused(try post(h.webView, "{op: 'checkForUpdates'}"))
        assertRefused(try post(h.webView, "{op: 'setAutomaticUpdates', on: true}"))
        XCTAssertEqual(h.updates.checkCount, 0)
        XCTAssertTrue(h.updates.automaticSets.isEmpty)
    }

    /// Each action op goes through the same three refusals: a cross-document
    /// click, a sub-frame call and a foreign-origin call. A control call from
    /// the right place succeeds, so the refusals are the guards and not a
    /// broken harness.
    func testEveryActionOpIsRefusedAcrossDocumentSubFrameAndForeignOrigin() throws {
        let ops: [(body: String, dict: [String: Any])] = [
            ("{op: 'notifications'}", ["op": "notifications"]),
            ("{op: 'checkForUpdates'}", ["op": "checkForUpdates"]),
            ("{op: 'setAutomaticUpdates', on: true}", ["op": "setAutomaticUpdates", "on": true]),
        ]
        func effects(_ h: Harness) -> Int {
            h.fake.actionCount + h.updates.checkCount + h.updates.automaticSets.count
        }
        for (body, dict) in ops {
            // Cross-document: the click belongs to the previous committed document.
            let crossed = try makeHarness()
            try load(crossed)
            click(crossed)
            let token = NSObject()
            crossed.coordinator.handleProvisionalStart(from: crossed.webView, navigation: token)
            crossed.coordinator.handleCommit(from: crossed.webView, navigation: token)
            assertRefused(try post(crossed.webView, body), file: #filePath, line: #line)
            XCTAssertEqual(effects(crossed), 0, "cross-document \(body)")
            click(crossed)
            XCTAssertNotNil(try post(crossed.webView, body)["ok"], "control \(body)")

            // Sub-frame: a same-origin frame has the handler but is not the main frame.
            let framed = try makeHarness()
            try load(framed, body: "<iframe id='f' srcdoc='<p>frame</p>'></iframe>")
            click(framed)
            assertRefused(
                try post(framed.webView, body, in: "document.getElementById('f').contentWindow"),
                file: #filePath, line: #line
            )
            XCTAssertEqual(effects(framed), 0, "sub-frame \(body)")

            // Foreign origin: the sending frame's host is not the selected one.
            let foreign = try makeHarness()
            try load(foreign)
            click(foreign)
            let refused = foreign.coordinator.handleAppSettingsMessage(
                isMainFrame: true, scheme: "https", host: "evil.example", port: 443,
                view: foreign.webView, body: dict
            )
            if case .success = refused { XCTFail("foreign origin \(body)") }
            XCTAssertEqual(effects(foreign), 0, "foreign origin \(body)")
            if case .success = foreign.coordinator.handleAppSettingsMessage(
                isMainFrame: true, scheme: "https", host: "example.com", port: 443,
                view: foreign.webView, body: dict
            ) {} else { XCTFail("control from the selected origin \(body)") }
            XCTAssertEqual(effects(foreign), 1, "control \(body)")
        }
    }

    func testSetAutomaticUpdatesWithoutAGestureIsRefused() throws {
        let h = try makeHarness()
        try load(h)
        assertRefused(try post(h.webView, "{op: 'setAutomaticUpdates', on: true}"))
        XCTAssertTrue(h.updates.automaticSets.isEmpty)
    }

    func testSetAutomaticUpdatesWithAGestureActsOnce() throws {
        let h = try makeHarness()
        try load(h)
        click(h)
        let outcome = try post(h.webView, "{op: 'setAutomaticUpdates', on: true}")
        let snapshot = try XCTUnwrap(outcome["ok"] as? [String: Any], "\(outcome)")
        XCTAssertEqual((snapshot["updates"] as? [String: Any])?["automatic"] as? Bool, true)
        XCTAssertEqual(h.updates.automaticSets, [true])
        assertRefused(try post(h.webView, "{op: 'setAutomaticUpdates', on: false}"))
        XCTAssertEqual(h.updates.automaticSets, [true])
    }

    func testSetAutomaticUpdatesRequiresAStrictBooleanAndExactKeys() throws {
        let h = try makeHarness()
        try load(h)
        let bodies = [
            "{op: 'setAutomaticUpdates', on: 1}",
            "{op: 'setAutomaticUpdates', on: 0}",
            "{op: 'setAutomaticUpdates', on: 'true'}",
            "{op: 'setAutomaticUpdates', on: null}",
            "{op: 'setAutomaticUpdates', on: [true]}",
            "{op: 'setAutomaticUpdates'}",
            "{op: 'setAutomaticUpdates', on: true, extra: 1}",
            "{op: 'setAutomaticUpdates', on: true, off: false}",
        ]
        for body in bodies {
            click(h)
            assertRefused(try post(h.webView, body))
        }
        XCTAssertTrue(h.updates.automaticSets.isEmpty)
        click(h)
        XCTAssertNotNil(try post(h.webView, "{op: 'setAutomaticUpdates', on: false}")["ok"])
        XCTAssertEqual(h.updates.automaticSets, [false])
    }

    func testIframeCallIsRefusedEvenWithAGesture() throws {
        let h = try makeHarness()
        try load(h, body: "<iframe id='f' srcdoc='<p>frame</p>'></iframe>")
        // The frame really has the handler, so the refusal below comes from the
        // main-frame guard and not from a missing handler.
        XCTAssertEqual(
            js(h.webView, "typeof document.getElementById('f').contentWindow.webkit.messageHandlers.ciaobotAppSettings") as? String,
            "object"
        )
        click(h)
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
            click(h)
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

    func testKeyboardActivationAndSubframeClickDoNotAuthorize() throws {
        let h = try makeHarness()
        try load(h)
        click(h, body: 1)
        XCTAssertFalse(notify(h), "A key press is not a click.")
        click(h, main: false)
        XCTAssertFalse(notify(h), "A subframe click is not a main-frame click.")
        XCTAssertEqual(h.fake.actionCount, 0)
        click(h)
        XCTAssertTrue(notify(h))
        XCTAssertEqual(h.fake.actionCount, 1)
    }

    func testClickExpiresAfterOneSecond() throws {
        let h = try makeHarness()
        try load(h)
        let t0 = Date()
        click(h, at: t0)
        XCTAssertFalse(notify(h, now: t0.addingTimeInterval(1.5)))
        XCTAssertEqual(h.fake.actionCount, 0)
        click(h, at: t0)
        XCTAssertTrue(notify(h, now: t0.addingTimeInterval(0.5)))
        XCTAssertEqual(h.fake.actionCount, 1)
    }

    func testSchemeAndPortMismatchAreRefused() throws {
        let h = try makeHarness()
        try load(h)
        click(h)
        XCTAssertFalse(notify(h, scheme: "http", port: 443))
        XCTAssertFalse(notify(h, port: 8443))
        XCTAssertEqual(h.fake.actionCount, 0)
        XCTAssertTrue(notify(h), "The gesture was untouched by the refusals.")
    }

    func testClickBeforeANavigationDoesNotAuthorizeTheNextDocument() throws {
        let h = try makeHarness()
        try load(h)
        click(h)
        let token = NSObject()
        h.coordinator.handleProvisionalStart(from: h.webView, navigation: token)
        h.coordinator.handleCommit(from: h.webView, navigation: token)
        XCTAssertFalse(notify(h), "The click belongs to the previous document.")
        XCTAssertEqual(h.fake.actionCount, 0)
        click(h)
        XCTAssertTrue(notify(h))
    }

    func testClickWhileUncommittedIsNotRecordedAndNotificationsIsRefused() throws {
        let h = try makeHarness()
        try load(h)
        click(h)
        h.coordinator.handleProvisionalStart(from: h.webView, navigation: NSObject())
        XCTAssertFalse(notify(h), "An uncommitted document must not reach native, notifications included.")
        click(h)
        XCTAssertFalse(notify(h))
        XCTAssertEqual(h.fake.actionCount, 0)
    }

    func testRefusedActionLeavesTheClickUnspent() throws {
        let h = try makeHarness()
        try load(h)
        h.fake.performResult = false
        click(h)
        XCTAssertFalse(notify(h), "A toggle that did nothing must not report success.")
        h.fake.performResult = true
        XCTAssertTrue(notify(h), "The refusal must not have spent the click.")
        XCTAssertEqual(h.fake.actionCount, 2)
        XCTAssertFalse(notify(h), "Success spends it.")
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

    /// WebKit's navigation policy cancels an off-origin load, so the guard is
    /// exercised on the URL check the dispatch uses.
    func testChangeSignalOnlyTargetsADocumentOnTheSelectedOrigin() throws {
        let h = try makeHarness()
        let c = h.coordinator
        XCTAssertTrue(c.documentIsOnOrigin(URL(string: "https://example.com/chat")))
        XCTAssertTrue(c.documentIsOnOrigin(URL(string: "https://example.com:443/")))
        XCTAssertFalse(c.documentIsOnOrigin(URL(string: "https://other.example/")))
        XCTAssertFalse(c.documentIsOnOrigin(URL(string: "http://example.com/")))
        XCTAssertFalse(c.documentIsOnOrigin(URL(string: "https://example.com:8443/")))
        XCTAssertFalse(c.documentIsOnOrigin(URL(string: "about:blank")))
        XCTAssertFalse(c.documentIsOnOrigin(nil))
    }

    func testChangeSignalIsNotDispatchedToAnOffOriginDocument() throws {
        let h = try makeHarness()
        try load(h)
        _ = js(h.webView, "window.__changes = 0; window.addEventListener('ciaobot-app-settings-changed', () => { window.__changes += 1; });")
        h.coordinator.currentDocumentURL = { URL(string: "https://other.example/") }
        h.fake.subject.send(.on)
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        XCTAssertEqual(js(h.webView, "window.__changes") as? Int, 0)
        h.coordinator.currentDocumentURL = { URL(string: "https://example.com/") }
        h.fake.subject.send(.off)
        let deadline = Date().addingTimeInterval(3)
        while Date() < deadline, (js(h.webView, "window.__changes") as? Int ?? 0) < 1 {
            RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        }
        XCTAssertEqual(js(h.webView, "window.__changes") as? Int, 1, "The same change on the origin must dispatch.")
    }

    func testChangeSignalStopsAfterRelease() throws {
        let h = try makeHarness()
        try load(h)
        _ = js(h.webView, "window.__changes = 0; window.addEventListener('ciaobot-app-settings-changed', () => { window.__changes += 1; });")
        h.coordinator.prepareForRelease()
        h.fake.subject.send(.on)
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        XCTAssertEqual(js(h.webView, "window.__changes") as? Int, 0)
    }

    func testCallAfterReleaseIsRefused() throws {
        let h = try makeHarness()
        try load(h)
        h.coordinator.prepareForRelease()
        click(h)
        if case .success = h.coordinator.handleAppSettingsMessage(
            isMainFrame: true, scheme: "https", host: "example.com", port: 443, view: h.webView, body: ["op": "get"]
        ) { XCTFail("A released coordinator must refuse") }
        // WebKit also drops the handler itself, so the page cannot post at all.
        XCTAssertEqual(js(h.webView, "(!window.webkit || !window.webkit.messageHandlers || !window.webkit.messageHandlers.ciaobotAppSettings) ? 'gone' : 'present'") as? String, "gone")
        XCTAssertEqual(h.fake.actionCount, 0)
    }
}
