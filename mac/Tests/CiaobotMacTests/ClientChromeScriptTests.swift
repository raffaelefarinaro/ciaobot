import XCTest
@testable import CiaobotMac

/// Non-GUI checks for the stylesheet the Mac client injects into the page.
/// They read the script source only; the rendered result needs a WKWebView and
/// is covered by `WebKitDelegateTests.testClientChromeFollowsPageTheme`.
final class ClientChromeScriptTests: XCTestCase {
    private var script: String { WebWindow.Coordinator.clientStyleScript }

    /// WebKit paints a native scroller from the page's used `color-scheme`, so
    /// that declaration is the fix for the light track in a dark window. It
    /// must follow the server UI's own `theme-light` class rather than a
    /// native appearance or a script bridge.
    func testThemeFollowsThePagesLightClass() {
        XCTAssertTrue(script.contains("html:root {") && script.contains("color-scheme: dark;"),
                      "The dark theme must declare its color scheme.")
        XCTAssertTrue(script.contains("html:root.theme-light {") && script.contains("color-scheme: light;"),
                      "The light theme must declare its color scheme.")
        XCTAssertFalse(script.contains("::-webkit-scrollbar"),
                       "WKWebView does not repaint native scrollers from ::-webkit-scrollbar.")
        XCTAssertFalse(script.lowercased().contains("messagehandlers"),
                       "The stylesheet must not add a script bridge.")
    }

    func testScrollbarStylesAreLeftToThePage() {
        XCTAssertFalse(script.contains("scrollbar-width:"))
        XCTAssertFalse(script.contains("scrollbar-color:"))
        XCTAssertFalse(script.contains("::-webkit-scrollbar"))
    }

    /// The PWA install step tells the user to use a browser menu this client
    /// does not have. Hide that row only: the phone step stays, and a card
    /// with no step left is hidden whole. A row that offers a real install
    /// prompt (no settings link) is not matched.
    func testOnlyThePWASetupCardIsHidden() {
        XCTAssertTrue(script.contains(".home-setup:has(a[href*=\"/settings#install-app\"])"),
                      "The setup card must be identified by its install link.")
        XCTAssertFalse(script.contains(".home-setup {"),
                       "The setup card must not be hidden unconditionally.")
        XCTAssertFalse(script.contains("home-setup-step:only-child"),
                       "Hiding must not depend on the card's current step count.")
        XCTAssertFalse(script.contains("Use on your phone"),
                       "The phone step must not be matched by this client.")
    }

    /// One stable id keeps the sheet idempotent across repeated injection and
    /// lets the client be diagnosed from a fixture without matching on text.
    func testStylesheetIsInsertedOnceUnderAStableId() {
        XCTAssertTrue(script.contains("'__ciaobot_client_style__'"))
        XCTAssertTrue(script.contains("if (document.getElementById('__ciaobot_client_style__')) return true;"),
                      "A second injection must not append a second sheet.")
    }
}
