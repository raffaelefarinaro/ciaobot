import XCTest
import AppKit
import WebKit
@testable import CiaobotMac

/// The main window's workspace-colour observer and its connection lifecycle.
/// The direct handler tests pass the fields `WKScriptMessage` would carry,
/// because WebKit does not let a test construct that type. The real-page tests
/// load a local HTML string whose base URL is a fake loopback origin, so no
/// test contacts a Ciaobot server.
@MainActor
final class WorkspaceColorObserverTests: XCTestCase {
    private static var retainedWindows: [NSWindow] = []
    private static var retainedCoordinators: [WebWindow.Coordinator] = []

    private let origin = Origin("http://127.0.0.1:9471")!

    // MARK: - Validated message handling

    func testAcceptsAnAllowlistedPresetFromTheExactMainFrame() throws {
        let (coordinator, view) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        send(.emerald, coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events.count, 1)
        XCTAssertEqual(sink.events.last ?? nil, .emerald)
        XCTAssertTrue((sink.views.last ?? nil) === view)
    }

    func testRejectsAnotherHandlerName() throws {
        let (coordinator, view) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        send(.amber, coordinator: coordinator, view: view, name: "somethingElse")
        XCTAssertTrue(sink.events.isEmpty)
    }

    func testRejectsASubframeEvenOnTheSelectedOrigin() throws {
        let (coordinator, view) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        send(.cyan, coordinator: coordinator, view: view, isMainFrame: false)
        XCTAssertTrue(sink.events.isEmpty, "A same-origin iframe must not choose the Dock colour")
    }

    func testRejectsAForeignOrigin() throws {
        let (coordinator, view) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        send(.violet, coordinator: coordinator, view: view, host: "127.0.0.2")
        send(.violet, coordinator: coordinator, view: view, port: 9472)
        send(.violet, coordinator: coordinator, view: view, scheme: "https")
        XCTAssertTrue(sink.events.isEmpty)
    }

    func testRejectsAStaleView() throws {
        let (coordinator, first) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        let replacement = makeUnconfiguredView()
        coordinator.attach(replacement)
        send(.pink, coordinator: coordinator, view: first)
        XCTAssertTrue(sink.events.isEmpty, "A message from a replaced view must not reach the app")

        send(.pink, coordinator: coordinator, view: replacement)
        XCTAssertEqual(sink.events.last ?? nil, .pink)
    }

    func testAReplacementViewReportsTheSameAccentAgain() throws {
        let (coordinator, first) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        send(.pink, coordinator: coordinator, view: first)
        XCTAssertEqual(sink.events, [.pink])

        let replacement = makeUnconfiguredView()
        coordinator.attach(replacement)
        send(.pink, coordinator: coordinator, view: replacement)
        XCTAssertEqual(sink.events, [.pink, .pink],
                       "A new view's document must be able to report the same accent the old one showed")
    }

    func testTeardownStopsLateMessages() throws {
        let (coordinator, view) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        send(.blue, coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events.count, 1)

        coordinator.prepareForRelease()
        XCTAssertTrue(coordinator.isReleased)
        send(.teal, coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events.count, 1, "A late page message after teardown must be ignored")
    }

    func testNeutralUnknownAndNullResetToTheBundledIcon() throws {
        let (coordinator, view) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        send(.amber, coordinator: coordinator, view: view)
        send("neutral", coordinator: coordinator, view: view)
        XCTAssertNil(sink.events.last ?? nil)
        send(.lime, coordinator: coordinator, view: view)
        send("chartreuse", coordinator: coordinator, view: view)
        XCTAssertNil(sink.events.last ?? nil)
        send(.fuchsia, coordinator: coordinator, view: view)
        sendNull(coordinator: coordinator, view: view)
        XCTAssertNil(sink.events.last ?? nil)
        XCTAssertEqual(sink.events, [.amber, nil, .lime, nil, .fuchsia, nil])
    }

    func testMalformedPayloadsAreIgnored() throws {
        let (coordinator, view) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record
        let token = coordinator.adoptedDocumentToken ?? ""

        sendRaw(["value": 42, "token": token], coordinator: coordinator, view: view)
        sendRaw(["notValue": "amber", "token": token], coordinator: coordinator, view: view)
        sendRaw("amber", coordinator: coordinator, view: view)
        // A valid value plus any extra key is a malformed payload, not a
        // colour: the whole message is refused.
        sendRaw(["value": "amber", "extra": true, "token": token], coordinator: coordinator, view: view)
        sendRaw(["value": "amber", "other": NSNull(), "token": token], coordinator: coordinator, view: view)
        XCTAssertTrue(sink.events.isEmpty)
    }

    /// Every value must carry the token native adopted for the committed
    /// document. A missing, unknown or wrongly typed token is refused whole, so
    /// it cannot change the colour.
    func testMessagesWithoutTheAdoptedTokenAreRefused() throws {
        let (coordinator, view) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        send(.pink, coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events, [.pink])

        sendRaw(["value": "cyan"], coordinator: coordinator, view: view)
        sendRaw(["value": "cyan", "token": "not-the-adopted-document"], coordinator: coordinator, view: view)
        sendRaw(["value": "cyan", "token": 42], coordinator: coordinator, view: view)
        sendRaw(["value": "cyan", "token": NSNull()], coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events, [.pink],
                       "A message without the adopted document token changed the colour")
    }

    /// An oversized value is still an observed value: it must clear a previous
    /// colour instead of leaving it in place.
    func testOversizedValueResolvesToTheBundledIcon() throws {
        let (coordinator, view) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        send(.pink, coordinator: coordinator, view: view)
        sendValue(String(repeating: "a", count: 4096), coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events, [.pink, nil])

        send(.amber, coordinator: coordinator, view: view)
        sendValue(String(repeating: "é", count: 20), coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events, [.pink, nil, .amber, nil],
                       "The raw UTF-8 byte count, not the trimmed spelling, is bounded")
    }

    func testExtraKeysAreRejectedWithoutChangingTheColour() throws {
        let (coordinator, view) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        send(.pink, coordinator: coordinator, view: view)
        sendRaw(
            ["value": "cyan", "extra": 1, "token": coordinator.adoptedDocumentToken ?? ""],
            coordinator: coordinator,
            view: view
        )
        XCTAssertEqual(sink.events, [.pink], "A payload with extra keys must not change the accent")
    }

    func testDuplicateValuesAreReportedOnce() throws {
        let (coordinator, view) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        send(.pink, coordinator: coordinator, view: view)
        send(.pink, coordinator: coordinator, view: view)
        send(.pink, coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events, [.pink])
    }

    func testNavigationResetClearsThePreviousPageAccent() throws {
        let (coordinator, view) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        send(.cyan, coordinator: coordinator, view: view)
        coordinator.webView(view, didStartProvisionalNavigation: nil)
        XCTAssertEqual(sink.events, [.cyan, nil])
        coordinator.webView(view, didStartProvisionalNavigation: nil)
        XCTAssertEqual(sink.events, [.cyan, nil], "A repeated reset must stay deduplicated")
    }

    /// A navigation A that B supersedes must not move the gate when A fails or
    /// commits late: the still-visible old document stays refused until B is
    /// the committed navigation.
    func testASupersededNavigationCannotUnlockTheOldDocument() throws {
        let (coordinator, view) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        send(.pink, coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events, [.pink])

        let navigationA = NSObject()
        let navigationB = NSObject()
        coordinator.handleProvisionalStart(from: view, navigation: navigationA)
        XCTAssertEqual(sink.events.last ?? nil, nil)
        coordinator.handleProvisionalStart(from: view, navigation: navigationB)
        XCTAssertEqual(sink.events, [.pink, nil])

        // A fails late: a superseded navigation has no effect on B's gate.
        coordinator.handleProvisionalFailure(
            from: view,
            navigation: navigationA,
            error: NSError(domain: NSURLErrorDomain, code: NSURLErrorCancelled)
        )
        send(.pink, coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events, [.pink, nil],
                       "A superseded navigation's failure unlocked the old document")

        // A commits late too: still not B.
        coordinator.handleCommit(from: view, navigation: navigationA)
        send(.pink, coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events, [.pink, nil],
                       "A superseded navigation's commit unlocked the old document")

        // Only B's commit makes the current document reportable.
        coordinator.handleCommit(from: view, navigation: navigationB)
        send(.amber, coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events, [.pink, nil, .amber])
    }

    /// A callback from a view the session already replaced must not move the
    /// current view's navigation counters.
    func testACallbackFromAReplacedViewCannotMoveTheCurrentGeneration() throws {
        let (coordinator, oldView) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        let currentView = makeUnconfiguredView()
        coordinator.attach(currentView)
        send(.cyan, coordinator: coordinator, view: currentView)
        XCTAssertEqual(sink.events, [.cyan])

        let navigation = NSObject()
        coordinator.handleProvisionalStart(from: oldView, navigation: navigation)
        coordinator.handleCommit(from: oldView, navigation: navigation)
        send(.cyan, coordinator: coordinator, view: currentView)
        XCTAssertEqual(sink.events, [.cyan],
                       "The old view's callbacks changed the current document's gate")

        // The current view still has a usable lifecycle of its own.
        let currentNavigation = NSObject()
        coordinator.handleProvisionalStart(from: currentView, navigation: currentNavigation)
        coordinator.handleCommit(from: currentView, navigation: currentNavigation)
        send(.violet, coordinator: coordinator, view: currentView)
        XCTAssertEqual(sink.events, [.cyan, nil, .violet])
    }

    /// Between provisional start and commit the old document is still alive.
    /// Its messages may not repaint the Dock, so a destination that publishes
    /// no app root keeps the fallback the navigation already showed.
    func testAnOldDocumentCannotRepaintAfterNavigationStarts() throws {
        let (coordinator, view) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        send(.pink, coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events, [.pink])

        let navigation = NSObject()
        coordinator.handleProvisionalStart(from: view, navigation: navigation)
        XCTAssertEqual(sink.events, [.pink, nil], "The navigation must restore the bundled icon")

        // The old document keeps running until commit; anything it posts now
        // is stale even though the view and origin still match.
        send(.pink, coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events, [.pink, nil], "An old-document message repainted the Dock")

        // The destination has no `#ciao-app`, so nothing follows the reset.
        coordinator.handleCommit(from: view, navigation: navigation)
        RunLoop.main.run(until: Date().addingTimeInterval(0.2))
        XCTAssertNil(sink.events.last ?? nil, "A destination with no app root must keep the bundled icon")

        // The committed document can report its own colour.
        send(.amber, coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events.last ?? nil, .amber)
    }

    /// The previous same-origin document can queue a value just before commit;
    /// it is delivered only after native rotated the token. Its old token must
    /// be refused, so a destination with no app root stays on the fallback.
    func testAnOldDocumentTokenIsRefusedAfterCommit() throws {
        let (coordinator, view) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        let oldToken = try XCTUnwrap(coordinator.adoptedDocumentToken)
        send(.pink, coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events, [.pink])

        let navigation = NSObject()
        coordinator.handleProvisionalStart(from: view, navigation: navigation)
        XCTAssertEqual(sink.events, [.pink, nil])

        // The destination commits and has no app root, so nothing is reported
        // after the adoption.
        coordinator.handleCommit(from: view, navigation: navigation)
        XCTAssertNil(sink.events.last ?? nil)
        let newToken = try XCTUnwrap(coordinator.adoptedDocumentToken)
        XCTAssertNotEqual(newToken, oldToken, "Commit must adopt a fresh document token")

        // The old document's queued value arrives now, after commit.
        sendValue("pink", coordinator: coordinator, view: view, token: oldToken)
        XCTAssertEqual(sink.events, [.pink, nil],
                       "A stale document token repainted the Dock after commit")

        // The newly adopted document is the only one that may report.
        sendValue("amber", coordinator: coordinator, view: view, token: newToken)
        XCTAssertEqual(sink.events, [.pink, nil, .amber])
    }

    /// A nil navigation token is never an identity: two nil-token starts store
    /// different sentinels, and a nil callback matches neither.
    func testNilProvisionalTokensCannotUnlockTheGate() throws {
        let (coordinator, view) = makeAttachedCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record

        send(.pink, coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events, [.pink])

        coordinator.handleProvisionalStart(from: view, navigation: nil)
        XCTAssertEqual(sink.events, [.pink, nil])
        coordinator.handleProvisionalStart(from: view, navigation: nil)
        XCTAssertEqual(sink.events, [.pink, nil])

        // The first nil start's late failure and the second's nil commit are
        // both refused because nil is not the stored sentinel.
        coordinator.handleProvisionalFailure(
            from: view,
            navigation: nil,
            error: NSError(domain: NSURLErrorDomain, code: NSURLErrorCancelled)
        )
        coordinator.handleCommit(from: view, navigation: nil)
        send(.pink, coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events, [.pink, nil],
                       "Nil navigation tokens unlocked the gate")

        // A real navigation token still commits normally.
        let navigation = NSObject()
        coordinator.handleProvisionalStart(from: view, navigation: navigation)
        coordinator.handleCommit(from: view, navigation: navigation)
        send(.amber, coordinator: coordinator, view: view)
        XCTAssertEqual(sink.events, [.pink, nil, .amber])
    }

    // MARK: - Configuration and child isolation

    func testTheObserverIsADedicatedMainFrameOnlyScript() throws {
        let coordinator = makeCoordinator()
        coordinator.onWorkspaceColor = { _, _ in }
        let configuration = WebWindow.Coordinator.makeConfiguration(coordinator: coordinator, storage: .ephemeral)
        let scripts = configuration.userContentController.userScripts
        let observer = try XCTUnwrap(
            scripts.first { $0.source == WebWindow.Coordinator.workspaceColorScript },
            "The workspace observer is not registered"
        )
        XCTAssertTrue(observer.isForMainFrameOnly)
        XCTAssertEqual(observer.injectionTime, .atDocumentStart)
        XCTAssertTrue(observer.source.contains("data-workspace-color"))
        XCTAssertTrue(observer.source.contains("MutationObserver"))
        XCTAssertTrue(observer.source.contains("seenRoot"),
                      "A removed root must report a fallback while an older server stays silent")
        XCTAssertTrue(observer.source.contains("TextEncoder"),
                      "The observer must bound the value in UTF-8 bytes, like the native check")
        XCTAssertTrue(observer.source.contains("const limit = \(WorkspaceColorPreset.maximumMessageLength);"))
        XCTAssertTrue(observer.source.contains("__ciaobotWorkspaceColorRefresh"),
                      "A failed navigation needs an entry point that resends the current colour")
        XCTAssertTrue(observer.source.contains("__ciaobotWorkspaceColorAdopt"),
                      "The native side must be able to adopt the committed document")
        XCTAssertTrue(observer.source.contains("token: documentToken"),
                      "Every value message must carry the adopted document token")
        XCTAssertTrue(observer.source.contains("addEventListener('pageshow'") &&
                      observer.source.contains("event.persisted"),
                      "A restored page must ask native to re-adopt it")
        XCTAssertTrue(observer.source.contains("{ adopt: true }"),
                      "The restore request must carry no colour")
        XCTAssertFalse(
            WebWindow.Coordinator.clientStyleScript.lowercased().contains("messagehandlers"),
            "The stylesheet script must not gain a bridge"
        )
        XCTAssertNotEqual(WebWindow.Coordinator.workspaceColorScript,
                          WebWindow.Coordinator.clientStyleScript)
    }

    /// The setup wizard uses the same `WebWindow` but passes no sink, so it gets
    /// neither the observer nor its bridge.
    func testASetupWindowWithoutASinkGetsNoObserver() {
        let coordinator = makeCoordinator()
        let configuration = WebWindow.Coordinator.makeConfiguration(coordinator: coordinator, storage: .ephemeral)
        XCTAssertFalse(
            configuration.userContentController.userScripts.contains {
                $0.source == WebWindow.Coordinator.workspaceColorScript
            },
            "A view with no workspace sink must not receive the observer"
        )
        XCTAssertFalse(configuration.userContentController.userScripts.contains {
            $0.source.contains(WebWindow.Coordinator.workspaceColorMessageName)
        })
    }

    func testChildWindowsNeverReceiveTheObserverOrItsBridge() throws {
        let coordinator = makeCoordinator()
        coordinator.onWorkspaceColor = { _, _ in }
        let configuration = WebWindow.Coordinator.makeConfiguration(coordinator: coordinator, storage: .ephemeral)
        let child = ChildWindowController(
            origin: origin,
            mode: .content,
            configuration: configuration,
            initialURL: origin.url,
            unshownTimeout: 30,
            filePanel: { _, completion in completion(nil) },
            downloadDestination: { _, completion in completion(nil) },
            downloadFailure: { _ in },
            openExternal: { _ in }
        )
        let scripts = child.webView.configuration.userContentController.userScripts
        XCTAssertTrue(scripts.isEmpty, "A child window must not inherit the main frame's scripts")
        XCTAssertFalse(scripts.contains { $0.source.contains("ciaobotWorkspaceColor") })
        child.close()
    }

    // MARK: - Real page observer

    func testReportsAnInitialValueAndLaterChanges() throws {
        let (coordinator, view, sink) = makePageHarness()
        view.loadHTMLString(
            Self.page("<div id=\"ciao-app\" data-workspace-color=\"pink\"></div>"),
            baseURL: origin.url
        )
        XCTAssertTrue(waitForAccent(sink) { $0 == .pink }, "The initial workspace colour was not reported")
        // The navigation reset is the only earlier event.
        XCTAssertEqual(sink.events.first ?? nil, nil)
        XCTAssertEqual(sink.events.last ?? nil, .pink)

        runJS(view, "document.getElementById('ciao-app').setAttribute('data-workspace-color', 'violet')")
        XCTAssertTrue(waitForAccent(sink) { $0 == .violet })

        runJS(view, "document.getElementById('ciao-app').removeAttribute('data-workspace-color')")
        XCTAssertTrue(waitForAccent(sink) { $0 == nil })
    }

    func testReportsALateRootCreation() throws {
        let (_, view, sink) = makePageHarness()
        view.loadHTMLString(Self.page("<p>no app root yet</p>"), baseURL: origin.url)
        XCTAssertTrue(waitFor(timeout: 5) { sink.events.count >= 1 })

        runJS(view, """
        const root = document.createElement('div');
        root.id = 'ciao-app';
        root.setAttribute('data-workspace-color', 'emerald');
        document.body.appendChild(root);
        true;
        """)
        XCTAssertTrue(waitForAccent(sink) { $0 == .emerald }, "A root created after document start was missed")
    }

    func testReportsAReplacedRoot() throws {
        let (_, view, sink) = makePageHarness()
        view.loadHTMLString(
            Self.page("<div id=\"ciao-app\" data-workspace-color=\"amber\"></div>"),
            baseURL: origin.url
        )
        XCTAssertTrue(waitForAccent(sink) { $0 == .amber })

        runJS(view, """
        const replacement = document.createElement('div');
        replacement.id = 'ciao-app';
        replacement.setAttribute('data-workspace-color', 'teal');
        document.getElementById('ciao-app').replaceWith(replacement);
        """)
        XCTAssertTrue(waitForAccent(sink) { $0 == .teal }, "A replaced app root was missed")
    }

    func testAnInvalidValueIsTreatedAsUnknown() throws {
        let (_, view, sink) = makePageHarness()
        view.loadHTMLString(
            Self.page("<div id=\"ciao-app\" data-workspace-color=\"amber\"></div>"),
            baseURL: origin.url
        )
        XCTAssertTrue(waitForAccent(sink) { $0 == .amber })

        runJS(view, "document.getElementById('ciao-app').setAttribute('data-workspace-color', 'chartreuse')")
        XCTAssertTrue(waitForAccent(sink) { $0 == nil }, "An unknown attribute value must restore the bundled icon")
    }

    /// An unmounted app root is an explicit unknown: the previous colour must
    /// not survive it. The report is sent once, then stays quiet.
    func testRemovingTheAppRootRestoresTheBundledIcon() throws {
        let (_, view, sink) = makePageHarness()
        view.loadHTMLString(
            Self.page("<div id=\"ciao-app\" data-workspace-color=\"pink\"></div>"),
            baseURL: origin.url
        )
        XCTAssertTrue(waitForAccent(sink) { $0 == .pink })

        runJS(view, "document.getElementById('ciao-app').remove(); true;")
        XCTAssertTrue(waitForAccent(sink) { $0 == nil }, "Removing the app root did not restore the bundled icon")
        let count = sink.events.count
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        XCTAssertEqual(sink.events.count, count, "A removed root must report its fallback only once")
        XCTAssertNil(sink.events.last ?? nil)
    }

    /// A destination that never publishes `#ciao-app` must not inherit the
    /// previous document's colour, even though the previous document is still
    /// running between provisional start and commit and can still post.
    func testNavigatingToADocumentWithoutAnAppRootKeepsTheBundledIcon() throws {
        let (coordinator, view, sink) = makePageHarness()
        view.loadHTMLString(
            Self.page("<div id=\"ciao-app\" data-workspace-color=\"violet\"></div>"),
            baseURL: origin.url
        )
        XCTAssertTrue(waitForAccent(sink) { $0 == .violet })

        // A navigation starts while the coloured document is still visible;
        // that document posts again before the new one commits.
        let navigation = NSObject()
        coordinator.handleProvisionalStart(from: view, navigation: navigation)
        XCTAssertTrue(waitForAccent(sink) { $0 == nil }, "The navigation did not restore the bundled icon")
        send(.violet, coordinator: coordinator, view: view)
        XCTAssertNil(sink.events.last ?? nil,
                     "The old document repainted the Dock before the new one committed")

        // The real destination has no app root and never reports anything.
        view.loadHTMLString(Self.page("<p>destination without an app root</p>"), baseURL: origin.url)
        RunLoop.main.run(until: Date().addingTimeInterval(0.6))
        XCTAssertNil(sink.events.last ?? nil, "The destination inherited the previous document's colour")
    }

    /// A cancelled provisional navigation leaves the coloured document on
    /// screen. Its deduplicated observer is resynced so the Dock returns to the
    /// workspace colour instead of staying orange.
    func testACancelledNavigationRestoresTheWorkspaceColour() throws {
        let (coordinator, view, sink) = makePageHarness()
        view.loadHTMLString(
            Self.page("<div id=\"ciao-app\" data-workspace-color=\"amber\"></div>"),
            baseURL: origin.url
        )
        XCTAssertTrue(waitForAccent(sink) { $0 == .amber })

        let navigation = NSObject()
        coordinator.handleProvisionalStart(from: view, navigation: navigation)
        XCTAssertTrue(waitForAccent(sink) { $0 == nil })

        // WebKit reports a policy cancellation in didFailProvisionalNavigation.
        coordinator.handleProvisionalFailure(
            from: view,
            navigation: navigation,
            error: NSError(domain: "WebKitErrorDomain", code: 102)
        )
        XCTAssertTrue(waitForAccent(sink) { $0 == .amber },
                      "The still-visible page did not repaint its workspace colour")
    }

    /// A back/forward-cached restore does not rerun the document-start script,
    /// so the observer resyncs from its `pageshow` listener.
    /// A restored document asks native to re-adopt and then reports its
    /// unchanged colour with the fresh token. The payload observer proves the
    /// script did the reporting; the app deduplicates a colour it already
    /// shows, but a restored page starts from the navigation's fallback.
    func testAPageshowAdoptRequestRepaintsTheCurrentColour() throws {
        let (coordinator, view, sink) = makePageHarness()
        var payloads: [Any] = []
        coordinator.workspaceColorPayloadObserver = { payloads.append($0) }
        view.loadHTMLString(
            Self.page("<div id=\"ciao-app\" data-workspace-color=\"cyan\"></div>"),
            baseURL: origin.url
        )
        XCTAssertTrue(waitForAccent(sink) { $0 == .cyan })
        let firstToken = try XCTUnwrap(coordinator.adoptedDocumentToken)
        payloads.removeAll()

        runJS(view, "window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })); true;")

        XCTAssertTrue(waitFor(timeout: 5) {
            payloads.contains { ($0 as? [String: Any])?["adopt"] as? Bool == true }
        }, "A restored page did not ask native to re-adopt it")

        XCTAssertTrue(waitFor(timeout: 5) {
            guard let body = payloads.compactMap({ $0 as? [String: Any] }).first(where: {
                $0["value"] as? String == "cyan" && $0["token"] is String
            }), let token = body["token"] as? String else { return false }
            return token == coordinator.adoptedDocumentToken && token != firstToken
        }, "The re-adopted page did not report its unchanged colour with a fresh token")
        XCTAssertNotEqual(coordinator.adoptedDocumentToken, firstToken,
                          "Re-adoption must rotate the document token")
    }

    /// An adopt request carries no colour: re-adopting a document with no app
    /// root must not invent one.
    func testAnAdoptRequestDoesNotInventAColour() throws {
        let (coordinator, view, sink) = makePageHarness()
        view.loadHTMLString(Self.page("<p>no app root</p>"), baseURL: origin.url)
        XCTAssertTrue(waitFor(timeout: 5) { sink.events.count >= 1 })
        XCTAssertNil(sink.events.last ?? nil)

        sendRaw(["adopt": true], coordinator: coordinator, view: view)
        RunLoop.main.run(until: Date().addingTimeInterval(0.4))
        XCTAssertNil(sink.events.last ?? nil, "An adopt request must not set a colour by itself")
    }

    /// The injected observer itself must not forward an out-of-bound value;
    /// it reports the bundled fallback instead.
    func testAnOversizedAttributeIsTreatedAsUnknown() throws {
        let (_, view, sink) = makePageHarness()
        view.loadHTMLString(
            Self.page("<div id=\"ciao-app\" data-workspace-color=\"amber\"></div>"),
            baseURL: origin.url
        )
        XCTAssertTrue(waitForAccent(sink) { $0 == .amber })

        runJS(view, "document.getElementById('ciao-app').setAttribute('data-workspace-color', 'a'.repeat(200))")
        XCTAssertTrue(waitForAccent(sink) { $0 == nil },
                      "An oversized attribute value must restore the bundled icon")
    }

    /// Prove the observer itself does not forward the oversized string. The
    /// native fallback alone would hide a forwarding bug.
    func testAnOversizedAttributeIsSentAsNull() throws {
        let (coordinator, view, sink) = makePageHarness()
        var payloads: [Any] = []
        coordinator.workspaceColorPayloadObserver = { payloads.append($0) }
        view.loadHTMLString(
            Self.page("<div id=\"ciao-app\" data-workspace-color=\"amber\"></div>"),
            baseURL: origin.url
        )
        XCTAssertTrue(waitForAccent(sink) { $0 == .amber })
        payloads.removeAll()

        runJS(view, "document.getElementById('ciao-app').setAttribute('data-workspace-color', 'a'.repeat(200))")
        XCTAssertTrue(waitForAccent(sink) { $0 == nil })
        XCTAssertTrue(waitFor(timeout: 2) { payloads.contains { ($0 as? [String: Any])?["value"] is NSNull } },
                      "The observer did not send an explicit null for the oversized value")

        let bodies = payloads.compactMap { $0 as? [String: Any] }
        XCTAssertFalse(bodies.contains { ($0["value"] as? String)?.count == 200 },
                       "The observer forwarded the oversized string to the app")
        XCTAssertTrue(bodies.allSatisfy { $0["value"] is NSNull },
                      "Every post-oversized payload must be an explicit null")
    }

    func testDuplicateMutationsDoNotRepaintTheDock() throws {
        let (_, view, sink) = makePageHarness()
        view.loadHTMLString(
            Self.page("<div id=\"ciao-app\" data-workspace-color=\"blue\"></div>"),
            baseURL: origin.url
        )
        XCTAssertTrue(waitForAccent(sink) { $0 == .blue })
        let count = sink.events.count

        runJS(view, """
        const root = document.getElementById('ciao-app');
        root.setAttribute('data-workspace-color', 'blue');
        root.setAttribute('data-workspace-color', 'blue');
        """)
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        XCTAssertEqual(sink.events.count, count, "An unchanged accent must not produce another event")
    }

    func testASameOriginIframeCannotReport() throws {
        let (_, view, sink) = makePageHarness()
        view.loadHTMLString(
            Self.page("<div id=\"ciao-app\" data-workspace-color=\"cyan\"></div>"),
            baseURL: origin.url
        )
        XCTAssertTrue(waitForAccent(sink) { $0 == .cyan })
        let count = sink.events.count

        runJS(view, """
        const frame = document.createElement('iframe');
        frame.srcdoc = '<div id="ciao-app" data-workspace-color="lime"></div>' +
          '<script>setTimeout(function () {' +
          '  if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.ciaobotWorkspaceColor) {' +
          '    window.webkit.messageHandlers.ciaobotWorkspaceColor.postMessage({ value: "violet" });' +
          '  }' +
          '}, 100);' +
          '<\\/script>';
        document.body.appendChild(frame);
        true;
        """)
        RunLoop.main.run(until: Date().addingTimeInterval(0.8))
        XCTAssertEqual(sink.events.count, count, "A same-origin iframe must not change the Dock colour")
    }

    // MARK: - Connection lifecycle

    func testSessionAppliesOnlyTheConnectionsOwnView() throws {
        let icon = FakeWorkspaceIcon()
        let session = AppSessionStatus(workspaceIcon: icon)
        session.reportConnected(origin)
        let view = makeUnconfiguredView()
        session.attachWebView(view, for: origin)

        session.reportWorkspaceAccent(.violet, from: view, for: origin)
        XCTAssertEqual(session.workspaceAccent, .violet)
        XCTAssertEqual(icon.current, .violet)

        // Same value again: no second repaint.
        let applied = icon.applied.count
        session.reportWorkspaceAccent(.violet, from: view, for: origin)
        XCTAssertEqual(icon.applied.count, applied)

        let otherView = makeUnconfiguredView()
        session.reportWorkspaceAccent(.pink, from: otherView, for: origin)
        XCTAssertEqual(icon.current, .violet, "A replaced view must not repaint the Dock")

        session.reportWorkspaceAccent(.pink, from: view, for: Origin("http://127.0.0.1:9472")!)
        XCTAssertEqual(icon.current, .violet, "Another origin must not repaint the Dock")
    }

    func testDisconnectAndReplacementRestoreTheBundledIcon() throws {
        let icon = FakeWorkspaceIcon()
        let session = AppSessionStatus(workspaceIcon: icon)
        session.reportConnected(origin)
        let first = makeUnconfiguredView()
        session.attachWebView(first, for: origin)
        session.reportWorkspaceAccent(.amber, from: first, for: origin)
        XCTAssertEqual(icon.current, .amber)

        // A new view for the same origin starts from the bundled icon.
        let second = makeUnconfiguredView()
        session.attachWebView(second, for: origin)
        XCTAssertNil(session.workspaceAccent)
        XCTAssertNil(icon.current)

        session.reportWorkspaceAccent(.teal, from: second, for: origin)
        session.reportDisconnected()
        XCTAssertNil(session.workspaceAccent)
        XCTAssertNil(icon.current)

        // A callback that settles after the disconnect is refused.
        session.reportWorkspaceAccent(.lime, from: second, for: origin)
        XCTAssertNil(session.workspaceAccent)
        XCTAssertNil(icon.current)
    }

    func testANewConnectionRestoresTheBundledIconBeforeThePageReports() throws {
        let icon = FakeWorkspaceIcon()
        let session = AppSessionStatus(workspaceIcon: icon)
        session.reportConnected(origin)
        let first = makeUnconfiguredView()
        session.attachWebView(first, for: origin)
        session.reportWorkspaceAccent(.blue, from: first, for: origin)

        session.reportConnected(Origin("https://example.com")!)
        XCTAssertNil(session.workspaceAccent)
        XCTAssertNil(icon.current)
    }

    // MARK: - Harness

    private final class AccentSink {
        private(set) var events: [WorkspaceColorPreset?] = []
        private(set) var views: [WKWebView?] = []

        func record(_ accent: WorkspaceColorPreset?, _ view: WKWebView?) {
            events.append(accent)
            views.append(view)
        }
    }

    private final class FakeWorkspaceIcon: WorkspaceDockIconControlling {
        private(set) var current: WorkspaceColorPreset?
        private(set) var applied: [WorkspaceColorPreset?] = []

        func apply(_ preset: WorkspaceColorPreset?) {
            current = preset
            applied.append(preset)
        }

        func reset() {
            apply(nil)
        }
    }

    private func makeCoordinator() -> WebWindow.Coordinator {
        WebWindow.Coordinator(
            origin: origin,
            onFailure: nil,
            downloadFailure: { _ in },
            filePanel: { _, completion in completion(nil) },
            downloadDestination: { _, completion in completion(nil) },
            openExternal: { _ in }
        )
    }

    private func makeAttachedCoordinator() -> (WebWindow.Coordinator, WKWebView) {
        let coordinator = makeCoordinator()
        let view = makeUnconfiguredView()
        coordinator.attach(view)
        return (coordinator, view)
    }

    private func makeUnconfiguredView() -> WKWebView {
        WKWebView(frame: NSRect(x: 0, y: 0, width: 200, height: 160), configuration: WKWebViewConfiguration())
    }

    private func makePageHarness() -> (WebWindow.Coordinator, WKWebView, AccentSink) {
        let representable = WebWindow(origin: origin)
        let coordinator = representable.makeCoordinator()
        let sink = AccentSink()
        coordinator.onWorkspaceColor = sink.record
        let view = WebWindow.Coordinator.makeWebView(origin: origin, coordinator: coordinator)
        view.frame = NSRect(x: 0, y: 0, width: 700, height: 500)
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 700, height: 500),
            styleMask: [.titled],
            backing: .buffered,
            defer: false
        )
        window.contentView = view
        Self.retainedWindows.append(window)
        Self.retainedCoordinators.append(coordinator)
        return (coordinator, view, sink)
    }

    private func send(
        _ preset: WorkspaceColorPreset?,
        coordinator: WebWindow.Coordinator,
        view: WKWebView?,
        token: String? = nil,
        name: String = WebWindow.Coordinator.workspaceColorMessageName,
        isMainFrame: Bool = true,
        scheme: String = "http",
        host: String = "127.0.0.1",
        port: Int = 9471
    ) {
        sendValue(preset?.rawValue as Any, coordinator: coordinator, view: view, token: token,
                  name: name, isMainFrame: isMainFrame, scheme: scheme, host: host, port: port)
    }

    private func send(
        _ value: String,
        coordinator: WebWindow.Coordinator,
        view: WKWebView?,
        token: String? = nil,
        name: String = WebWindow.Coordinator.workspaceColorMessageName,
        isMainFrame: Bool = true,
        scheme: String = "http",
        host: String = "127.0.0.1",
        port: Int = 9471
    ) {
        sendValue(value, coordinator: coordinator, view: view, token: token,
                  name: name, isMainFrame: isMainFrame, scheme: scheme, host: host, port: port)
    }

    private func sendNull(
        coordinator: WebWindow.Coordinator,
        view: WKWebView?,
        token: String? = nil,
        name: String = WebWindow.Coordinator.workspaceColorMessageName,
        isMainFrame: Bool = true,
        scheme: String = "http",
        host: String = "127.0.0.1",
        port: Int = 9471
    ) {
        sendValue(NSNull(), coordinator: coordinator, view: view, token: token,
                  name: name, isMainFrame: isMainFrame, scheme: scheme, host: host, port: port)
    }

    /// Sends a value with the coordinator's currently adopted document token
    /// unless a test names one explicitly.
    private func sendValue(
        _ value: Any,
        coordinator: WebWindow.Coordinator,
        view: WKWebView?,
        token: String? = nil,
        name: String = WebWindow.Coordinator.workspaceColorMessageName,
        isMainFrame: Bool = true,
        scheme: String = "http",
        host: String = "127.0.0.1",
        port: Int = 9471
    ) {
        coordinator.handleWorkspaceColorMessage(
            name: name,
            isMainFrame: isMainFrame,
            scheme: scheme,
            host: host,
            port: port,
            view: view,
            body: ["value": value, "token": token ?? coordinator.adoptedDocumentToken ?? ""]
        )
    }

    /// Sends an arbitrary payload, for malformed shapes and explicit token
    /// cases. Nothing is added.
    private func sendRaw(
        _ body: Any,
        coordinator: WebWindow.Coordinator,
        view: WKWebView?,
        name: String = WebWindow.Coordinator.workspaceColorMessageName,
        isMainFrame: Bool = true,
        scheme: String = "http",
        host: String = "127.0.0.1",
        port: Int = 9471
    ) {
        coordinator.handleWorkspaceColorMessage(
            name: name,
            isMainFrame: isMainFrame,
            scheme: scheme,
            host: host,
            port: port,
            view: view,
            body: body
        )
    }

    private static func page(_ body: String) -> String {
        "<!doctype html><html><body>\(body)</body></html>"
    }

    private func runJS(_ view: WKWebView, _ script: String) {
        var done = false
        view.evaluateJavaScript(script) { _, error in
            if let error { XCTFail("JavaScript failed: \(error)") }
            done = true
        }
        let deadline = Date().addingTimeInterval(5)
        while !done && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.01))
        }
        XCTAssertTrue(done, "JavaScript did not finish")
    }

    private func waitForAccent(
        _ sink: AccentSink,
        timeout: TimeInterval = 5,
        _ predicate: (WorkspaceColorPreset?) -> Bool
    ) -> Bool {
        waitFor(timeout: timeout) { !sink.events.isEmpty && predicate(sink.events.last ?? nil) }
    }

    private func waitFor(timeout: TimeInterval, _ condition: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
        return condition()
    }
}
