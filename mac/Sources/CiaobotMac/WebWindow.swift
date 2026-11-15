import SwiftUI
import WebKit
import AppKit

typealias ExternalOpener = (URL) -> Void

enum WebWindowNavigationProfile: Equatable {
    case main
    case hostChat
}

/// One trusted click opens at most one new window or external link. WebKit has
/// no public navigation-action flag for a user gesture, so the main frame
/// reports a trusted `click`/keyboard activation through a one-way message.
/// The message carries no page data and grants no native capability.
final class UserGestureGate {
    private let validity: TimeInterval
    private var recordedAt: Date?

    init(validity: TimeInterval = 1.0) {
        self.validity = validity
    }

    func record(now: Date = Date()) {
        recordedAt = now
    }

    func consume(now: Date = Date()) -> Bool {
        guard let recordedAt, now >= recordedAt, now.timeIntervalSince(recordedAt) <= validity else {
            self.recordedAt = nil
            return false
        }
        self.recordedAt = nil
        return true
    }

    func clear() {
        recordedAt = nil
    }
}

struct FilePanelRequest: Equatable {
    let allowsMultipleSelection: Bool
    let allowsDirectories: Bool
    let isMainFrame: Bool
}

struct OpenPanelConfiguration: Equatable {
    let allowsMultipleSelection: Bool
    let canChooseDirectories: Bool
    let canChooseFiles: Bool

    init(_ request: FilePanelRequest) {
        allowsMultipleSelection = request.allowsMultipleSelection
        canChooseDirectories = request.allowsDirectories
        canChooseFiles = !request.allowsDirectories
    }
}

typealias FilePanelHandler = (FilePanelRequest, @escaping ([URL]?) -> Void) -> Void

final class FilePanelPresenter {
    static let shared = FilePanelPresenter()

    private var active = false

    func present(_ request: FilePanelRequest, completion: @escaping ([URL]?) -> Void) {
        guard !active else {
            completion(nil)
            return
        }
        active = true
        let configuration = OpenPanelConfiguration(request)
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = configuration.allowsMultipleSelection
        panel.canChooseDirectories = configuration.canChooseDirectories
        panel.canChooseFiles = configuration.canChooseFiles
        panel.canCreateDirectories = false
        panel.resolvesAliases = true
        panel.begin { response in
            self.active = false
            completion(response == .OK ? panel.urls : nil)
        }
    }
}

struct DownloadRequest {
    let response: URLResponse
    let suggestedFilename: String
}

typealias DownloadDestination = (DownloadRequest, @escaping (URL?) -> Void) -> Void

final class DownloadDestinationPresenter {
    static let shared = DownloadDestinationPresenter()

    private var active = false

    func choose(_ request: DownloadRequest, completion: @escaping (URL?) -> Void) {
        guard !active else {
            completion(nil)
            return
        }
        active = true
        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.nameFieldStringValue = Self.safeFilename(request.suggestedFilename)
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        panel.begin { response in
            self.active = false
            completion(response == .OK ? panel.url : nil)
        }
    }

    static func safeFilename(_ suggested: String) -> String {
        let last = (suggested as NSString).lastPathComponent
        guard !last.isEmpty, last != ".", last != ".." else { return "download" }
        return last
    }
}

enum ChildMode: Equatable {
    case content
    case oauthPlaceholder
}

/// WebKit reports a cancelled policy navigation as WebKitErrorDomain 102
/// (frame load interrupted by a policy change) rather than NSURLErrorCancelled.
/// It is a deliberate block, not a server or session failure.
private func isPolicyCancellation(_ error: Error) -> Bool {
    let nsError = error as NSError
    if nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled { return true }
    if nsError.domain == "WebKitErrorDomain" && nsError.code == 102 { return true }
    return false
}

typealias DownloadFailureHandler = (String) -> Void

private func presentDownloadFailure(_ message: String) {
    let alert = NSAlert()
    alert.messageText = "Could not save the download"
    alert.informativeText = message
    alert.alertStyle = .warning
    alert.addButton(withTitle: "OK")
    alert.runModal()
}

final class DownloadCoordinator: NSObject, WKDownloadDelegate {
    private struct PendingDownload {
        let temporary: URL
        let final: URL
    }

    private let origin: Origin
    private let destination: DownloadDestination
    private let failure: DownloadFailureHandler
    private var pending: [ObjectIdentifier: PendingDownload] = [:]
    private var abandoned = false
    var onFinish: (() -> Void)?

    init(origin: Origin, destination: @escaping DownloadDestination, failure: @escaping DownloadFailureHandler) {
        self.origin = origin
        self.destination = destination
        self.failure = failure
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        // The window this download belongs to is gone; no later outcome may
        // install a file or show an alert.
        guard !abandoned else {
            completionHandler(nil)
            return
        }
        // Verify the final response before asking for a destination: a
        // redirect must not turn an app download into an external-origin save.
        // Blob downloads can have no response URL, so fall back to the request
        // that started them. This URL is the download source, never the local
        // destination.
        let responseURL = response.url ?? download.originalRequest?.url
        guard let responseURL, origin.download(responseURL) == .inApp else {
            failure("The download response was not from the selected origin.")
            completionHandler(nil)
            return
        }

        destination(DownloadRequest(response: response, suggestedFilename: suggestedFilename)) { [weak self] chosenURL in
            guard let self else {
                completionHandler(nil)
                return
            }
            guard !self.abandoned else {
                completionHandler(nil)
                return
            }
            guard let finalURL = chosenURL else {
                completionHandler(nil)
                return
            }
            guard finalURL.isFileURL else {
                self.failure("The selected download destination was not a file.")
                completionHandler(nil)
                return
            }

            var isDirectory: ObjCBool = false
            if FileManager.default.fileExists(atPath: finalURL.path, isDirectory: &isDirectory) {
                guard !isDirectory.boolValue else {
                    self.failure("The selected destination is a folder.")
                    completionHandler(nil)
                    return
                }
            }

            // WKDownload refuses an existing destination. Always download
            // beside the chosen file, then replace it only after the download
            // succeeds. A failed download leaves the original file untouched.
            let temporary = self.temporaryURL(for: finalURL)
            self.pending[ObjectIdentifier(download)] = PendingDownload(temporary: temporary, final: finalURL)
            completionHandler(temporary)
        }
    }

    func download(_ download: WKDownload, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, decisionHandler: @escaping (WKDownload.RedirectPolicy) -> Void) {
        guard let url = request.url, origin.download(url) == .inApp else {
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func downloadDidFinish(_ download: WKDownload) {
        if let pending = pending.removeValue(forKey: ObjectIdentifier(download)) {
            do {
                try install(pending)
            } catch {
                try? FileManager.default.removeItem(at: pending.temporary)
                failure("The download finished, but it could not be saved over the existing file. The original file was left unchanged.")
            }
        }
        onFinish?()
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        if let pending = pending.removeValue(forKey: ObjectIdentifier(download)) {
            try? FileManager.default.removeItem(at: pending.temporary)
        }
        if !abandoned, !isPolicyCancellation(error) {
            failure("The download failed. Any existing file was left unchanged.")
        }
        onFinish?()
    }

    /// Called when the owning child window closes. An in-progress download has
    /// no user watching it any more, so its temporary file must not be left
    /// beside the chosen destination, and a late finish or failure must not
    /// install a file or show an alert for a closed window.
    func abandonPendingDownloads() {
        abandoned = true
        for pending in pending.values {
            try? FileManager.default.removeItem(at: pending.temporary)
        }
        pending.removeAll()
    }

    private func temporaryURL(for finalURL: URL) -> URL {
        let directory = finalURL.deletingLastPathComponent()
        for _ in 0..<16 {
            let candidate = directory.appendingPathComponent(".ciaobot-\(UUID().uuidString).download")
            if !FileManager.default.fileExists(atPath: candidate.path) { return candidate }
        }
        return directory.appendingPathComponent(".ciaobot-\(UUID().uuidString).download")
    }

    private func install(_ pending: PendingDownload) throws {
        let manager = FileManager.default
        if manager.fileExists(atPath: pending.final.path) {
            _ = try manager.replaceItemAt(pending.final, withItemAt: pending.temporary)
        } else {
            try manager.moveItem(at: pending.temporary, to: pending.final)
        }
    }
}

final class ChildCoordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
    let origin: Origin
    let mode: ChildMode
    let filePanel: FilePanelHandler
    let openExternal: ExternalOpener
    let downloads: DownloadCoordinator
    weak var owner: ChildWindowController?

    #if DEBUG
    /// Test seam: how many navigation decisions this child has seen. WebKit
    /// loads a child returned from createWebViewWith itself, so an app-issued
    /// second navigation shows up here as an extra decision.
    private(set) var testNavigationDecisionCount = 0
    #endif

    init(origin: Origin, mode: ChildMode, filePanel: @escaping FilePanelHandler, downloadDestination: @escaping DownloadDestination, downloadFailure: @escaping DownloadFailureHandler, openExternal: @escaping ExternalOpener) {
        self.origin = origin
        self.mode = mode
        self.filePanel = filePanel
        self.openExternal = openExternal
        self.downloads = DownloadCoordinator(origin: origin, destination: downloadDestination, failure: downloadFailure)
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        #if DEBUG
        testNavigationDecisionCount += 1
        #endif
        guard let url = action.request.url else {
            decisionHandler(.cancel)
            return
        }

        if mode == .oauthPlaceholder {
            // A placeholder exists only to receive the user's OAuth target
            // after a gated window.open. It never loads that target in the app:
            // the exact URL is handed to the system browser and the child
            // closes, so the app's session cookie store is never used for an
            // external request. Content children never reach this branch.
            if url.absoluteString == "about:blank" {
                decisionHandler(.allow)
                return
            }
            guard action.targetFrame?.isMainFrame ?? true, !action.shouldPerformDownload else {
                decisionHandler(.cancel)
                return
            }
            switch origin.navigation(url, userInitiated: true) {
            case .inWindow, .external:
                openExternal(url)
                owner?.close()
                decisionHandler(.cancel)
            case .blocked:
                decisionHandler(.cancel)
            }
            return
        }

        // A content child is selected-origin content (image, PDF, HTML,
        // download). It may never open an external URL, including through a
        // redirect or a scripted navigation.
        if action.shouldPerformDownload {
            switch origin.download(url) {
            case .inApp:
                owner?.downloadsWillBegin()
                decisionHandler(.download)
            case .external, .blocked:
                decisionHandler(.cancel)
            }
            return
        }

        if url.absoluteString == "about:blank" {
            decisionHandler(.cancel)
            return
        }

        switch origin.navigation(url, userInitiated: false) {
        case .inWindow:
            decisionHandler(.allow)
        case .external, .blocked:
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        guard let url = response.response.url else {
            decisionHandler(.cancel)
            return
        }
        if mode == .oauthPlaceholder {
            decisionHandler(url.absoluteString == "about:blank" ? .allow : .cancel)
            return
        }
        if url.absoluteString == "about:blank" {
            decisionHandler(.cancel)
            return
        }
        let disposition = (response.response as? HTTPURLResponse)?
            .value(forHTTPHeaderField: "Content-Disposition")?
            .lowercased()
        let isAttachment = disposition?.contains("attachment") == true
        if !response.canShowMIMEType || isAttachment {
            switch origin.download(url) {
            case .inApp:
                owner?.downloadsWillBegin()
                decisionHandler(.download)
            case .external, .blocked:
                decisionHandler(.cancel)
            }
            return
        }
        guard origin.navigation(url, userInitiated: false) == .inWindow else {
            decisionHandler(.cancel)
            return
        }
        owner?.show()
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        nil
    }

    func filePanelAllowed(for frame: WKFrameInfo) -> Bool {
        mode == .content
            && origin.matches(scheme: frame.securityOrigin.protocol, host: frame.securityOrigin.host, port: frame.securityOrigin.port)
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        guard filePanelAllowed(for: frame) else {
            completionHandler(nil)
            return
        }
        filePanel(FilePanelRequest(
            allowsMultipleSelection: parameters.allowsMultipleSelection,
            allowsDirectories: parameters.allowsDirectories,
            isMainFrame: frame.isMainFrame
        ), completionHandler)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = downloads
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = downloads
    }

    func webViewDidClose(_ webView: WKWebView) {
        owner?.close()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        // A policy cancellation is a deliberate block, not a session failure.
        // But a child blocked before it displayed a window has nothing to show,
        // so it must not stay retained in the coordinator's children. A
        // navigation that became a download is cancelled for the same reason
        // while the download (and its save panel) is still running.
        if !isPolicyCancellation(error) || (owner?.wasShown == false && owner?.isAwaitingDownload == false) {
            owner?.close()
        }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        owner?.close()
    }
}

final class ChildWindowController: NSObject, NSWindowDelegate {
    let webView: WKWebView
    let coordinator: ChildCoordinator
    let mode: ChildMode

    private let initialURL: URL
    private let loadsInitialURL: Bool
    private let unshownTimeout: TimeInterval
    private var window: NSWindow?
    private var closed = false
    private(set) var wasShown = false
    private var downloadPending = false
    private var blankTimeout: DispatchWorkItem?
    var onClose: (() -> Void)?

    var hasWindow: Bool { window != nil }
    var isClosed: Bool { closed }

    #if DEBUG
    var testNavigationDecisionCount: Int { coordinator.testNavigationDecisionCount }
    #endif

    init(origin: Origin, mode: ChildMode, configuration providedConfiguration: WKWebViewConfiguration, initialURL: URL, loadsInitialURL: Bool = true, unshownTimeout: TimeInterval = 60, filePanel: @escaping FilePanelHandler, downloadDestination: @escaping DownloadDestination, downloadFailure: @escaping DownloadFailureHandler, openExternal: @escaping ExternalOpener) {
        self.mode = mode
        self.initialURL = initialURL
        self.loadsInitialURL = loadsInitialURL
        self.unshownTimeout = unshownTimeout

        // WebKit requires the configuration supplied to createWebViewWith.
        // Replacing its websiteDataStore crashes createNewPage. Copy it and
        // replace only the user content controller so the child does not
        // inherit the main frame's desktop marker or trusted-click bridge.
        // The result still shares the main nonpersistent session store; see
        // See docs/reference.md: JS popup windows cannot be cookie-isolated with public API.
        let configuration = providedConfiguration.copy() as! WKWebViewConfiguration
        configuration.userContentController = WKUserContentController()
        let webView = WKWebView(frame: .zero, configuration: configuration)
        self.webView = webView
        self.coordinator = ChildCoordinator(
            origin: origin,
            mode: mode,
            filePanel: filePanel,
            downloadDestination: downloadDestination,
            downloadFailure: downloadFailure,
            openExternal: openExternal
        )
        super.init()

        webView.navigationDelegate = coordinator
        webView.uiDelegate = coordinator
        if #available(macOS 13.3, *) { webView.isInspectable = false }
        coordinator.owner = self
        coordinator.downloads.onFinish = { [weak self] in
            guard let self, !self.wasShown else { return }
            self.close()
        }
    }

    deinit {
        blankTimeout?.cancel()
        coordinator.downloads.abandonPendingDownloads()
    }

    func start() {
        // WebKit loads the child it is handed from createWebViewWith itself,
        // using the navigation action's full request. Loading the URL again
        // would start a second navigation and would replay the request as a
        // bare GET.
        if loadsInitialURL {
            webView.load(URLRequest(url: initialURL))
        }
        armUnshownTimeout()
    }

    /// A child that never displays a window must not live forever. This covers
    /// a first navigation blocked by policy (a cancellation, not a load
    /// failure) as well as one that never completes.
    private func armUnshownTimeout() {
        blankTimeout?.cancel()
        let timeout = DispatchWorkItem { [weak self] in
            guard let self, !self.closed, !self.wasShown else { return }
            self.close()
        }
        blankTimeout = timeout
        DispatchQueue.main.asyncAfter(deadline: .now() + unshownTimeout, execute: timeout)
    }

    /// A save panel can stay open indefinitely, so a pending download keeps
    /// the unshown child alive until the download reports back. A download
    /// decision also interrupts the navigation, which must not be mistaken for
    /// a blocked child.
    func downloadsWillBegin() {
        downloadPending = true
        blankTimeout?.cancel()
        blankTimeout = nil
    }

    var isAwaitingDownload: Bool { downloadPending }

    func show() {
        guard !closed else { return }
        wasShown = true
        blankTimeout?.cancel()
        blankTimeout = nil

        if window == nil {
            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 900, height: 700),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered,
                defer: false
            )
            window.title = "Ciaobot"
            window.isReleasedWhenClosed = false
            window.contentView = webView
            window.delegate = self
            window.center()
            self.window = window
        }
        window?.makeKeyAndOrderFront(nil)
    }

    func close() {
        guard !closed else { return }
        blankTimeout?.cancel()
        blankTimeout = nil
        if let window {
            window.close()
        }
        finish()
    }

    func windowWillClose(_ notification: Notification) {
        finish()
    }

    private func finish() {
        guard !closed else { return }
        closed = true
        coordinator.downloads.abandonPendingDownloads()
        window?.orderOut(nil)
        onClose?()
        onClose = nil
    }
}

struct WebWindow: NSViewRepresentable {
    let origin: Origin
    var storage: SessionStorage = .ephemeral
    /// The connection's already-created store. Passing it here (rather than
    /// calling `.nonPersistent()` a second time) keeps the tray's cookie reads
    /// and the web view on one cookie jar.
    var websiteDataStore: WKWebsiteDataStore? = nil
    var navigationProfile: WebWindowNavigationProfile = .main
    var onFailure: ((String) -> Void)? = nil
    var downloadFailure: DownloadFailureHandler = { presentDownloadFailure($0) }
    var filePanel: FilePanelHandler = FilePanelPresenter.shared.present
    var downloadDestination: DownloadDestination = DownloadDestinationPresenter.shared.choose
    var openExternal: ExternalOpener = { NSWorkspace.shared.open($0) }
    /// Lets the connection screen prove the WKWebView is gone before a store is
    /// erased. Nil for tests that only need the coordinator.
    var viewTracker: SessionViewTracker? = nil
    /// Reports the live view to the app session so the tray can open a chat
    /// path. It carries no page data.
    var onWebViewReady: ((WKWebView) -> Void)? = nil
    /// The validated active-workspace accent from this view's main frame, or
    /// nil for the bundled fallback. The view is passed back so the session can
    /// refuse a callback from a view it already replaced.
    var onWorkspaceColor: ((WorkspaceColorPreset?, WKWebView?) -> Void)? = nil

    func makeCoordinator() -> Coordinator {
        Coordinator(
            origin: origin,
            onFailure: onFailure,
            downloadFailure: downloadFailure,
            filePanel: filePanel,
            downloadDestination: downloadDestination,
            openExternal: openExternal,
            viewTracker: viewTracker,
            navigationProfile: navigationProfile,
            onWorkspaceColor: onWorkspaceColor
        )
    }

    func makeNSView(context: Context) -> WKWebView {
        let view = Coordinator.makeWebView(
            origin: origin,
            coordinator: context.coordinator,
            storage: storage,
            websiteDataStore: websiteDataStore
        )
        context.coordinator.viewTracker?.attach(view)
        onWebViewReady?(view)
        view.load(URLRequest(url: origin.url))
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {}

    /// SwiftUI calls this when it replaces the view (Change Server) or tears
    /// the window down. The WebKit view must be released before a persistent
    /// store can be forgotten, so the coordinator drops its children and
    /// message handlers here and the returned view is released by SwiftUI.
    static func dismantleNSView(_ nsView: WKWebView, coordinator: Coordinator) {
        coordinator.prepareForRelease()
        nsView.stopLoading()
        nsView.navigationDelegate = nil
        nsView.uiDelegate = nil
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        static let trustedClickMessageName = "ciaobotTrustedClick"
        static let workspaceColorMessageName = "ciaobotWorkspaceColor"

        private static let desktopMarkerScript = "window.__CIAOBOT_DESKTOP__ = true;"

        /// The client's own page chrome: the scrollbar theme and the PWA
        /// install card, which a WKWebView cannot satisfy.
        ///
        /// The white sidebar scrollbar is AppKit's scroller, not a page
        /// scrollbar. WebKit paints a scroll container's scrollbar from the
        /// page's **used `color-scheme`** when the system is set to always
        /// show (legacy) scrollbars, so a dark page that never declares one
        /// still gets the light system track inside a dark window. The engine
        /// declares no `color-scheme`, and the `::-webkit-scrollbar` rules
        /// used earlier did not repaint this scroller: current WebKit disables
        /// them when the page's `scrollbar-width: thin` applies to every
        /// element, and Safari does not implement `scrollbar-color` (18.2
        /// added only `scrollbar-width` and `scrollbar-gutter`). Declaring
        /// `color-scheme` here, following the page's own `theme-light` class,
        /// is the actual switch. Scrollbar width and colour remain under the
        /// page's control.
        ///
        /// The setup card is the PWA install surface: "Install the app — Use
        /// your browser's Install option in the address bar or menu". This
        /// client is not a browser, has no such menu, and cannot install the
        /// PWA; the engine already ships native notifications and the phone
        /// setup it also links to lives in Settings. The card is therefore
        /// hidden whole, and only while it is in that browser-instructions
        /// state: the rule requires the card's install link (the client has no
        /// `beforeinstallprompt`, so that link is always the install row), so
        /// a future client where a real install prompt exists keeps the card.
        /// It matches the setup card's own class, so a renamed component only
        /// brings the card back; it cannot hide unrelated Home content.
        ///
        /// The stylesheet reads only the page's existing theme class. It
        /// carries no page data, adds no message handler,
        /// and offers no native capability. It exists only in this client: the
        /// PWA in a browser never receives it.
        static let clientStyleScript = """
        (() => {
          const install = () => {
            const root = document.documentElement;
            if (!root) return false;
            if (document.getElementById('__ciaobot_client_style__')) return true;
            const style = document.createElement('style');
            style.id = '__ciaobot_client_style__';
            style.textContent = `
              html:root {
                color-scheme: dark;
              }
              html:root.theme-light {
                color-scheme: light;
              }
              .home-setup:has(a[href*="/settings#install-app"]) {
                display: none;
              }
            `;
            (root.head || root).appendChild(style);
            return true;
          };
          if (!install()) {
            document.addEventListener('DOMContentLoaded', install, { once: true });
          }
        })();
        """

        /// Reports the active workspace accent the web UI already renders on
        /// `#ciao-app[data-workspace-color]`. It is a separate script from the
        /// stylesheet above: the stylesheet must never gain a bridge, and this
        /// observer carries only one bounded attribute value.
        ///
        /// The root may be created after document start and may be replaced or
        /// unmounted by the SPA, so the observer watches the document subtree
        /// and re-reads the current element each time. It reports the initial
        /// value, later changes, an explicit `null` when the attribute is
        /// removed, and one explicit `null` after a root that existed is
        /// unmounted. While no root has ever appeared it stays silent, so an
        /// older server that never publishes a colour leaves the bundled icon
        /// in place. Only a change is sent, so re-renders that keep the same
        /// accent are quiet.
        ///
        /// A string longer than the native bound is sent as `null` rather than
        /// forwarded; the coordinator enforces the same bound on the raw value
        /// and treats an oversized value as unknown.
        ///
        /// The script does not trust its own document identity. It starts with
        /// no token and posts no value until native adoption installs one at
        /// commit; every value message carries that token, so a message the
        /// previous same-origin document queued before commit is refused after
        /// native rotates the token. A back/forward-cached restore asks native
        /// to re-adopt instead of assuming the old token still counts.
        ///
        /// It is injected into the main frame only, and the coordinator
        /// re-checks the handler name, view identity, main frame, exact origin,
        /// committed document, token and value before the app sees anything.
        /// Child windows get a fresh user content controller, so this bridge
        /// never reaches them.
        static let workspaceColorScript = """
        (() => {
          const bridge = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.\(workspaceColorMessageName);
          if (!bridge) return;
          const limit = \(WorkspaceColorPreset.maximumMessageLength);
          let sent = false;
          let last;
          let seenRoot = false;
          // Null until native adoption. Without it this document posts no
          // value, so a pre-commit message cannot cross the commit boundary.
          let documentToken = null;
          const root = () => document.getElementById('ciao-app');
          // known: false while no root has ever appeared (an older server).
          // value: null for an absent colour or after a known root is gone.
          const currentValue = () => {
            const element = root();
            if (!element) return seenRoot ? { known: true, value: null } : { known: false };
            seenRoot = true;
            const raw = element.getAttribute('data-workspace-color');
            if (typeof raw !== 'string') return { known: true, value: null };
            const bytes = new TextEncoder().encode(raw).length;
            return { known: true, value: bytes > limit ? null : raw };
          };
          const report = () => {
            if (typeof documentToken !== 'string' || documentToken.length === 0) return;
            const next = currentValue();
            if (!next.known) return;
            if (sent && next.value === last) return;
            sent = true;
            last = next.value;
            try { bridge.postMessage({ value: next.value, token: documentToken }); } catch (_) {}
          };
          // A failed or cancelled navigation leaves this document on screen
          // after the native side reset the icon. Clearing the dedupe baseline
          // makes it report its current colour again with its existing token.
          const refresh = () => {
            sent = false;
            last = undefined;
            report();
          };
          window.__ciaobotWorkspaceColorRefresh = refresh;
          // Native adoption for this committed document. Rotating the token
          // also makes any queued pre-commit message from the previous
          // document stale.
          window.__ciaobotWorkspaceColorAdopt = (token) => {
            if (typeof token !== 'string' || token.length === 0) return;
            documentToken = token;
            sent = false;
            last = undefined;
            report();
          };
          // A back/forward-cached restore does not run the document-start
          // script again. Ask native to re-adopt this document with a fresh
          // token; the request itself carries no colour.
          addEventListener('pageshow', (event) => {
            if (!event.persisted) return;
            if (typeof documentToken !== 'string' || documentToken.length === 0) return;
            try { bridge.postMessage({ adopt: true }); } catch (_) {}
          });
          const observe = () => {
            const target = document.documentElement;
            if (!target) return false;
            report();
            new MutationObserver(report).observe(target, {
              subtree: true,
              childList: true,
              attributes: true,
              attributeFilter: ['data-workspace-color']
            });
            return true;
          };
          if (!observe()) document.addEventListener('DOMContentLoaded', observe, { once: true });
        })();
        """

        private static let trustedClickScript = """
        (() => {
          if (window.__CIAOBOT_TRUSTED_CLICK_INSTALLED__) return;
          window.__CIAOBOT_TRUSTED_CLICK_INSTALLED__ = true;
          const bridge = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.\(trustedClickMessageName);
          if (!bridge) return;
          const send = () => { try { bridge.postMessage(1); } catch (_) {} };
          const mark = (event) => { if (event.isTrusted) send(); };
          addEventListener('click', mark, true);
          addEventListener('keydown', (event) => {
            if (event.isTrusted && (event.key === 'Enter' || event.key === ' ')) send();
          }, true);
        })();
        """

        let origin: Origin
        let onFailure: ((String) -> Void)?
        let filePanel: FilePanelHandler
        let downloadDestination: DownloadDestination
        let downloadFailure: DownloadFailureHandler
        let openExternal: ExternalOpener
        let downloads: DownloadCoordinator
        let viewTracker: SessionViewTracker?
        let navigationProfile: WebWindowNavigationProfile
        let gestureGate = UserGestureGate()

        private(set) var children: [ChildWindowController] = []
        private(set) var trustedActivationCount = 0
        private(set) weak var webView: WKWebView?
        private var reportedFailure = false
        private var armedHostChatLoad: URL?
        /// The validated accent last handed to the app. Deduplicates re-renders
        /// that repeat the same attribute value.
        private var lastReportedWorkspaceColor: WorkspaceColorPreset?
        private var hasReportedWorkspaceColor = false
        /// The document that owns the view. Incremented when a new provisional
        /// navigation starts; a page message is accepted only while the
        /// committed document is the current one. Both start at zero so a
        /// coordinator driven directly (tests, and callers with no navigation)
        /// still accepts the current document; the first real navigation makes
        /// them differ until WebKit commits.
        private var documentGeneration = 0
        private var committedDocumentGeneration = 0
        /// The provisional navigation currently in flight, as the identity
        /// WebKit handed us at `didStartProvisionalNavigation`. Commit and
        /// failure callbacks act only when they belong to this navigation and
        /// this view; a superseded navigation's late callback is ignored. The
        /// token is `AnyObject` so tests can hand in plain objects where WebKit
        /// itself would pass a `WKNavigation`.
        private var provisionalNavigation: AnyObject?
        private weak var provisionalNavigationView: WKWebView?
        private var provisionalNavigationGeneration = 0
        /// The identity native adopted for the current committed document. A
        /// fresh value is issued at every commit and bfcache re-adoption; a
        /// value message carrying any other token is refused, even when its
        /// view, origin and generation still match.
        private(set) var adoptedDocumentToken: String? = UUID().uuidString
        /// Set by `prepareForRelease`; a late page callback after teardown must
        /// not reach the app.
        private(set) var isReleased = false
        /// The app-facing sink for this view's workspace accent.
        var onWorkspaceColor: ((WorkspaceColorPreset?, WKWebView?) -> Void)?
        /// Test/observability hook: receives each page payload before any
        /// validation, so a test can prove what the injected observer sent
        /// rather than only what survived the native checks.
        var workspaceColorPayloadObserver: ((Any) -> Void)?

        /// Test/observability hook: called before the file-panel origin check.
        var filePanelFrameOriginObserver: ((String, String, Int) -> Void)?

        /// Test seam: how long a child may stay without a displayed window
        /// before it is closed. Production uses the default.
        var unshownChildTimeout: TimeInterval = 60

        #if DEBUG
        /// Test-only: allow a cross-origin subframe to load so the file-panel
        /// origin rejection can be exercised with a real cross-origin frame.
        /// Never compiled into a release build.
        var testAllowsCrossOriginFrames = false
        var testCrossOriginFrameObserver: ((WKFrameInfo) -> Void)?
        #endif

        init(origin: Origin, onFailure: ((String) -> Void)?, downloadFailure: @escaping DownloadFailureHandler, filePanel: @escaping FilePanelHandler, downloadDestination: @escaping DownloadDestination, openExternal: @escaping ExternalOpener, viewTracker: SessionViewTracker? = nil, navigationProfile: WebWindowNavigationProfile = .main, onWorkspaceColor: ((WorkspaceColorPreset?, WKWebView?) -> Void)? = nil) {
            self.origin = origin
            self.onFailure = onFailure
            self.downloadFailure = downloadFailure
            self.filePanel = filePanel
            self.downloadDestination = downloadDestination
            self.openExternal = openExternal
            self.viewTracker = viewTracker
            self.navigationProfile = navigationProfile
            self.onWorkspaceColor = onWorkspaceColor
            self.downloads = DownloadCoordinator(origin: origin, destination: downloadDestination, failure: downloadFailure)
        }

        deinit {
            children.forEach { $0.close() }
        }

        /// Builds the view's WebKit configuration. Main-actor isolated: the
        /// configuration, its user content controller and the connection's
        /// data store are WebKit UI state, and the script message handlers are
        /// installed here. The nested `Coordinator` type is not main-actor
        /// isolated on its own, so the isolation is declared on the factory.
        @MainActor
        static func makeConfiguration(coordinator: Coordinator, storage: SessionStorage, websiteDataStore: WKWebsiteDataStore? = nil, javaScriptCanOpenWindowsAutomatically: Bool = false) -> WKWebViewConfiguration {
            let configuration = WKWebViewConfiguration()
            // Never the default shared store: each connection gets its own
            // ephemeral store, or its own named store when the user opted in.
            configuration.websiteDataStore = websiteDataStore ?? WebKitSessionStores.make(storage)
            configuration.preferences.javaScriptCanOpenWindowsAutomatically = javaScriptCanOpenWindowsAutomatically

            let controller = configuration.userContentController
            controller.addUserScript(WKUserScript(
                source: desktopMarkerScript,
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true
            ))
            controller.addUserScript(WKUserScript(
                source: clientStyleScript,
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true
            ))
            // Only the connected main window wires a colour sink. Setup and
            // child windows get neither the observer script nor its bridge.
            if coordinator.onWorkspaceColor != nil {
                controller.addUserScript(WKUserScript(
                    source: workspaceColorScript,
                    injectionTime: .atDocumentStart,
                    forMainFrameOnly: true
                ))
                controller.add(coordinator, name: workspaceColorMessageName)
            }
            controller.addUserScript(WKUserScript(
                source: trustedClickScript,
                injectionTime: .atDocumentStart,
                forMainFrameOnly: false
            ))
            controller.add(coordinator, name: trustedClickMessageName)
            #if DEBUG
            controller.add(coordinator, name: "ciaobotCrossOriginProbe")
            #endif
            return configuration
        }

        /// Creates the connection's `WKWebView`. Main-actor isolated for the
        /// same reason as `makeConfiguration`: it builds the configuration,
        /// creates WebKit's view and installs the coordinator on it.
        @MainActor
        static func makeWebView(origin: Origin, coordinator: Coordinator, storage: SessionStorage = .ephemeral, websiteDataStore: WKWebsiteDataStore? = nil, javaScriptCanOpenWindowsAutomatically: Bool = false) -> WKWebView {
            let configuration = makeConfiguration(
                coordinator: coordinator,
                storage: storage,
                websiteDataStore: websiteDataStore,
                javaScriptCanOpenWindowsAutomatically: javaScriptCanOpenWindowsAutomatically
            )

            let webView = WKWebView(frame: .zero, configuration: configuration)
            webView.navigationDelegate = coordinator
            webView.uiDelegate = coordinator
            if #available(macOS 13.3, *) { webView.isInspectable = false }
            coordinator.attach(webView)
            return webView
        }

        /// Releases app-owned state that would otherwise outlive the SwiftUI
        /// view. Called before the WKWebView is released so a later forget can
        /// remove its store.
        func prepareForRelease() {
            isReleased = true
            armedHostChatLoad = nil
            children.forEach { $0.close() }
            children.removeAll()
            gestureGate.clear()
            clearProvisionalNavigation()
            adoptedDocumentToken = nil
            onWorkspaceColor = nil
            webView?.configuration.userContentController.removeAllScriptMessageHandlers()
        }

        func attach(_ webView: WKWebView) {
            if self.webView !== webView {
                // A replacement view starts a new page and a new dedupe
                // baseline; otherwise the same colour as the old page would be
                // deduplicated away after the session already reset.
                hasReportedWorkspaceColor = false
                lastReportedWorkspaceColor = nil
                documentGeneration = 0
                committedDocumentGeneration = 0
                provisionalNavigation = nil
                provisionalNavigationView = nil
                provisionalNavigationGeneration = 0
                adoptedDocumentToken = UUID().uuidString
            }
            self.webView = webView
        }

        func loadHostChat(_ url: URL) {
            guard navigationProfile == .hostChat, !isReleased else { return }
            armedHostChatLoad = url
            webView?.load(URLRequest(url: url))
        }

        func hostChatActionAllowed(url: URL, targetFrameIsMain: Bool?, navigationType: WKNavigationType, shouldPerformDownload: Bool = false, httpMethod: String = "GET") -> Bool {
            guard navigationProfile == .hostChat else { return false }
            let isNativeLoad = navigationType == .other
            let isArmedLoad = isNativeLoad && armedHostChatLoad == url
            if isArmedLoad && httpMethod.uppercased() == "GET" && !shouldPerformDownload && origin.navigation(url, userInitiated: false) == .inWindow {
                armedHostChatLoad = nil
                return true
            }
            return targetFrameIsMain != nil && !shouldPerformDownload
                && origin.navigation(url, userInitiated: false) == .inWindow
        }

        func hostChatResponseAllowed(url: URL, canShowMIMEType: Bool, isAttachment: Bool) -> Bool {
            navigationProfile == .hostChat && canShowMIMEType && !isAttachment
                && origin.navigation(url, userInitiated: false) == .inWindow
        }

        var permitsPopupCreation: Bool { navigationProfile != .hostChat }

        func decideHostChatAction(url: URL, targetFrameIsMain: Bool?, navigationType: WKNavigationType,
                                  shouldPerformDownload: Bool, httpMethod: String,
                                  decision: (WKNavigationActionPolicy) -> Void) {
            decision(hostChatActionAllowed(url: url, targetFrameIsMain: targetFrameIsMain, navigationType: navigationType,
                                           shouldPerformDownload: shouldPerformDownload, httpMethod: httpMethod) ? .allow : .cancel)
        }

        func decideHostChatResponse(url: URL, canShowMIMEType: Bool, isAttachment: Bool,
                                    decision: (WKNavigationResponsePolicy) -> Void) {
            decision(hostChatResponseAllowed(url: url, canShowMIMEType: canShowMIMEType, isAttachment: isAttachment) ? .allow : .cancel)
        }

        func createAllowedPopup(_ create: () -> WKWebView?) -> WKWebView? {
            guard permitsPopupCreation else { return nil }
            return create()
        }

        func noteTrustedActivationForTesting() {
            trustedActivationCount += 1
            gestureGate.record()
        }

        func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
            #if DEBUG
            if message.name == "ciaobotCrossOriginProbe" {
                testCrossOriginFrameObserver?(message.frameInfo)
                return
            }
            #endif
            if message.name == Self.workspaceColorMessageName {
                workspaceColorPayloadObserver?(message.body)
                let frameOrigin = message.frameInfo.securityOrigin
                handleWorkspaceColorMessage(
                    name: message.name,
                    isMainFrame: message.frameInfo.isMainFrame,
                    scheme: frameOrigin.protocol,
                    host: frameOrigin.host,
                    port: frameOrigin.port,
                    view: message.webView,
                    body: message.body
                )
                return
            }
            guard message.name == Self.trustedClickMessageName else { return }
            let frameOrigin = message.frameInfo.securityOrigin
            guard message.frameInfo.isMainFrame || origin.matches(scheme: frameOrigin.protocol, host: frameOrigin.host, port: frameOrigin.port) else {
                return
            }
            trustedActivationCount += 1
            gestureGate.record()
        }

        /// Validates one workspace-colour message. `WKScriptMessage` cannot be
        /// constructed in a test, so the fields are passed explicitly and the
        /// production dispatch above is the only caller.
        ///
        /// Every field is treated as untrusted: the handler name must be this
        /// client's, the sending view must be the coordinator's current view
        /// (not merely the same origin), the frame must be the main frame of
        /// the exact selected origin, the document must be the committed one,
        /// and a value payload must carry the exact token native adopted for
        /// that document. A value message with a missing, unknown or non-string
        /// token, or any key besides `value` and `token`, is refused whole, so
        /// a message queued by the previous document before commit cannot
        /// survive into the new one. `adopt: true` asks native to rotate the
        /// token for the committed document; it carries no colour itself.
        /// `neutral`, an unknown word, an oversized string and `null` all mean
        /// the bundled icon. Nothing is accepted after release.
        func handleWorkspaceColorMessage(
            name: String,
            isMainFrame: Bool,
            scheme: String,
            host: String,
            port: Int,
            view: WKWebView?,
            body: Any
        ) {
            guard !isReleased,
                  name == Self.workspaceColorMessageName,
                  let view,
                  self.webView === view,
                  isMainFrame,
                  // The previous document must not repaint the Dock while a
                  // new provisional navigation is between commit points.
                  committedDocumentGeneration == documentGeneration,
                  origin.matches(scheme: scheme, host: host, port: port),
                  let payload = body as? [String: Any] else { return }

            // A cached page asks for a fresh adoption; this request changes no
            // colour by itself.
            if payload.count == 1, (payload["adopt"] as? Bool) == true {
                adoptCurrentDocument(in: view)
                return
            }

            guard payload.count == 2,
                  let token = payload["token"] as? String,
                  !token.isEmpty,
                  token == adoptedDocumentToken,
                  let value = payload["value"] else { return }

            let accent: WorkspaceColorPreset?
            if value is NSNull {
                accent = nil
            } else if let raw = value as? String {
                // Bound the raw value before trimming: an oversized or
                // otherwise unusable value is unknown, not a reason to keep
                // the previous colour.
                if raw.utf8.count > WorkspaceColorPreset.maximumMessageLength {
                    accent = nil
                } else {
                    let normalized = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                    accent = WorkspaceColorPreset(rawValue: normalized)
                }
            } else {
                return
            }
            reportWorkspaceColor(accent, from: view)
        }

        private func reportWorkspaceColor(_ accent: WorkspaceColorPreset?, from view: WKWebView) {
            guard !isReleased, self.webView === view else { return }
            guard !hasReportedWorkspaceColor || lastReportedWorkspaceColor != accent else { return }
            hasReportedWorkspaceColor = true
            lastReportedWorkspaceColor = accent
            onWorkspaceColor?(accent, view)
        }

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = action.request.url else {
                decisionHandler(.cancel)
                return
            }

            if navigationProfile == .hostChat {
                decideHostChatAction(url: url, targetFrameIsMain: action.targetFrame?.isMainFrame,
                                     navigationType: action.navigationType, shouldPerformDownload: action.shouldPerformDownload,
                                     httpMethod: action.request.httpMethod ?? "GET", decision: decisionHandler)
                return
            }

            if action.shouldPerformDownload {
                switch origin.download(url) {
                case .inApp:
                    decisionHandler(.download)
                case .external:
                    if gestureGate.consume() { openExternal(url) }
                    decisionHandler(.cancel)
                case .blocked:
                    decisionHandler(.cancel)
                }
                return
            }

            let isNewWindow = action.targetFrame == nil
            let isTopLevel = action.targetFrame?.isMainFrame ?? true

            #if DEBUG
            if testAllowsCrossOriginFrames, !isNewWindow, !isTopLevel {
                decisionHandler(.allow)
                return
            }
            #endif

            switch origin.navigation(url, userInitiated: action.navigationType == .linkActivated) {
            case .inWindow:
                if isNewWindow {
                    // A link activation only. A form target=_blank would need
                    // its POST body preserved; refusing is safer than opening
                    // the URL with a GET in a child window.
                    guard action.navigationType == .linkActivated, gestureGate.consume() else {
                        decisionHandler(.cancel)
                        return
                    }
                    _ = openChild(url: url, mode: .content)
                    decisionHandler(.cancel)
                } else {
                    decisionHandler(.allow)
                }
            case .external:
                guard isTopLevel, action.navigationType == .linkActivated else {
                    decisionHandler(.cancel)
                    return
                }
                if gestureGate.consume() { openExternal(url) }
                decisionHandler(.cancel)
            case .blocked:
                decisionHandler(.cancel)
            }
        }

        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            return createAllowedPopup {
                // WebKit performs this exact request in the web view we return. The
                // decision seam below only carries a URL, so anything that is not a
                // plain GET would silently be replayed as one. Refuse instead of
                // downgrading the request, and spend the gesture.
                guard Self.webKitCanDrive(navigationAction.request, navigationType: navigationAction.navigationType) else {
                    _ = gestureGate.consume()
                    return nil
                }
                return newWindow(
                    for: navigationAction.request.url,
                    userGesture: gestureGate.consume(),
                    configuration: configuration,
                    webKitDrivesNavigation: true
                )
            }
        }

        /// Whether WebKit can be left to perform this navigation in a returned
        /// child. Only a request WebKit can deliver unchanged is accepted: a
        /// form submission (which may carry a body) is never replayed as a GET.
        static func webKitCanDrive(_ request: URLRequest, navigationType: WKNavigationType) -> Bool {
            guard navigationType != .formSubmitted else { return false }
            let method = (request.httpMethod ?? "GET").uppercased()
            return method == "GET" || method == "HEAD"
        }

        /// The decision behind `createWebViewWith`. Kept separate so the
        /// delegate seam can be tested without WebKit's popup-host state.
        /// `webKitDrivesNavigation` is true only when WebKit will load the
        /// returned view itself; direct callers still need the initial load.
        func newWindow(for url: URL?, userGesture: Bool, configuration: WKWebViewConfiguration? = nil, webKitDrivesNavigation: Bool = false) -> WKWebView? {
            guard userGesture else { return nil }
            let target: URL
            if let url, !url.absoluteString.isEmpty {
                target = url
            } else {
                target = URL(string: "about:blank")!
            }

            if target.absoluteString == "about:blank" {
                return openChild(
                    url: target,
                    mode: .oauthPlaceholder,
                    configuration: configuration,
                    loadsInitialURL: !webKitDrivesNavigation
                )
            }

            switch origin.navigation(target, userInitiated: true) {
            case .inWindow:
                return openChild(
                    url: target,
                    mode: .content,
                    configuration: configuration,
                    loadsInitialURL: !webKitDrivesNavigation
                )
            case .external:
                openExternal(target)
                return nil
            case .blocked:
                return nil
            }
        }

        func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
            guard let url = response.response.url else {
                decisionHandler(.cancel)
                return
            }
            if navigationProfile == .hostChat {
                let disposition = (response.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition")?.lowercased()
                decideHostChatResponse(url: url, canShowMIMEType: response.canShowMIMEType,
                                       isAttachment: disposition?.contains("attachment") == true, decision: decisionHandler)
                return
            }
            #if DEBUG
            if testAllowsCrossOriginFrames, !response.isForMainFrame {
                decisionHandler(.allow)
                return
            }
            #endif
            let disposition = (response.response as? HTTPURLResponse)?
                .value(forHTTPHeaderField: "Content-Disposition")?
                .lowercased()
            let isAttachment = disposition?.contains("attachment") == true
            if !response.canShowMIMEType || isAttachment {
                switch origin.download(url) {
                case .inApp:
                    decisionHandler(.download)
                case .external, .blocked:
                    decisionHandler(.cancel)
                }
                return
            }
            guard origin.navigation(url, userInitiated: false) == .inWindow else {
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
        }

        func filePanelAllowed(for frame: WKFrameInfo) -> Bool {
            filePanelFrameOriginObserver?(frame.securityOrigin.protocol, frame.securityOrigin.host, frame.securityOrigin.port)
            return origin.matches(scheme: frame.securityOrigin.protocol, host: frame.securityOrigin.host, port: frame.securityOrigin.port)
        }

        func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
            guard filePanelAllowed(for: frame) else {
                completionHandler(nil)
                return
            }
            filePanel(FilePanelRequest(
                allowsMultipleSelection: parameters.allowsMultipleSelection,
                allowsDirectories: parameters.allowsDirectories,
                isMainFrame: frame.isMainFrame
            ), completionHandler)
        }

        func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
            download.delegate = downloads
        }

        func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
            download.delegate = downloads
        }

        /// A new document owns its own accent. The previous page's colour must
        /// not survive a navigation, and a document that never reports one
        /// leaves the bundled icon in place. Bumping the generation also
        /// refuses anything the old document still posts before WebKit commits
        /// the new one.
        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            handleProvisionalStart(from: webView, navigation: navigation)
        }

        /// The new document is now the committed one, so its own observer may
        /// report. Until this point every page message belongs to the old
        /// document.
        func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
            handleCommit(from: webView, navigation: navigation)
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            handleProvisionalFailure(from: webView, navigation: navigation, error: error)
        }

        /// The testable bodies of the navigation callbacks. Each one is tied to
        /// the view and to the exact provisional navigation it reports; a
        /// callback from a replaced view or a navigation a later one superseded
        /// must not move the current document's counters. `AnyObject` keeps the
        /// production token (a `WKNavigation`) and a test token comparable by
        /// identity.
        func handleProvisionalStart(from view: WKWebView, navigation: AnyObject?) {
            guard self.webView === view else { return }
            documentGeneration += 1
            // A nil token is never an identity. Store a unique sentinel so two
            // nil-token starts cannot match each other or any nil callback.
            provisionalNavigation = navigation ?? NSObject()
            provisionalNavigationView = view
            provisionalNavigationGeneration = documentGeneration
            reportWorkspaceColor(nil, from: view)
        }

        func handleCommit(from view: WKWebView, navigation: AnyObject?) {
            guard isCurrentProvisionalNavigation(from: view, navigation: navigation) else { return }
            committedDocumentGeneration = provisionalNavigationGeneration
            clearProvisionalNavigation()
            adoptCurrentDocument(in: view)
        }

        func handleProvisionalFailure(from view: WKWebView, navigation: AnyObject?, error: Error) {
            guard isCurrentProvisionalNavigation(from: view, navigation: navigation) else { return }
            clearProvisionalNavigation()
            // A failed or policy-cancelled provisional navigation leaves the
            // previously committed document on screen, so that document counts
            // as current again instead of being treated as stale forever.
            committedDocumentGeneration = provisionalNavigationGeneration
            reportFailure(error)
            // Its observer already deduplicated the colour it showed before the
            // reset; ask it to report that value again now.
            resyncWorkspaceColor(in: view)
        }

        private func isCurrentProvisionalNavigation(from view: WKWebView, navigation: AnyObject?) -> Bool {
            guard self.webView === view, provisionalNavigationView === view else { return false }
            // A nil callback token is not identity, even when the start token
            // was nil too: the start stored a fresh sentinel that nil cannot
            // equal.
            guard let current = provisionalNavigation, let navigation else { return false }
            return current === navigation
        }

        private func clearProvisionalNavigation() {
            provisionalNavigation = nil
            provisionalNavigationView = nil
        }

        /// Rotates the document identity for the committed view and installs it
        /// in the page. The adoption call also makes the page report its current
        /// colour once, so a document that has not reported yet (late root) is
        /// covered by the same path.
        private func adoptCurrentDocument(in view: WKWebView) {
            guard self.webView === view else { return }
            let token = UUID().uuidString
            adoptedDocumentToken = token
            view.evaluateJavaScript(
                "window.__ciaobotWorkspaceColorAdopt && window.__ciaobotWorkspaceColorAdopt(\"\(token)\");",
                completionHandler: nil
            )
        }

        private func resyncWorkspaceColor(in view: WKWebView) {
            view.evaluateJavaScript(
                "window.__ciaobotWorkspaceColorRefresh && window.__ciaobotWorkspaceColorRefresh();",
                completionHandler: nil
            )
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            reportFailure(error)
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            reportFailure(NSError(domain: NSURLErrorDomain, code: NSURLErrorUnknown))
        }

        private func openChild(url: URL, mode: ChildMode, configuration: WKWebViewConfiguration? = nil, loadsInitialURL: Bool = true) -> WKWebView? {
            guard let configuration = configuration ?? webView?.configuration else { return nil }
            let child = ChildWindowController(
                origin: origin,
                mode: mode,
                configuration: configuration,
                initialURL: url,
                loadsInitialURL: loadsInitialURL,
                unshownTimeout: unshownChildTimeout,
                filePanel: filePanel,
                downloadDestination: downloadDestination,
                downloadFailure: downloadFailure,
                openExternal: openExternal
            )
            child.onClose = { [weak self, weak child] in
                guard let self else { return }
                self.children.removeAll { $0 === child }
            }
            children.append(child)
            child.start()
            return child.webView
        }

        private func reportFailure(_ error: Error) {
            guard !reportedFailure, !isPolicyCancellation(error) else { return }
            reportedFailure = true
            onFailure?("Could not load the server’s interface. Check that the server is still running and try again.")
        }
    }
}
