import Combine
import Foundation
import WebKit

/// The only connection fact the menu bar is allowed to show. It is reported by
/// the connection screen after its credential-free probe succeeds and cleared
/// when that session is left or fails. It is not a sign-in state, a chat count,
/// a notification source, or a server-health claim.
@MainActor
final class AppSessionStatus: ObservableObject {
    enum Connection: Equatable {
        case disconnected
        case connected(String)
    }

    @Published private(set) var connection: Connection = .disconnected

    /// The active workspace accent currently drawn on the running Dock icon,
    /// or nil while the bundled artwork is shown. Only the main web view of
    /// this exact connection can set it.
    private(set) var workspaceAccent: WorkspaceColorPreset?
    /// The running Dock icon, whose controller also owns the launch fallback.
    /// Nil in test processes that never construct one, so no test touches the
    /// real Dock.
    private let workspaceIcon: WorkspaceDockIconControlling?

    init(workspaceIcon: WorkspaceDockIconControlling? = nil) {
        self.workspaceIcon = workspaceIcon
    }

    /// The exact origin the user selected, kept beside the display string so
    /// the tray can build only same-origin URLs. Nil while disconnected.
    private(set) var currentOrigin: Origin?
    /// The connection's own WebKit store. Never the default shared store.
    private(set) var dataStore: WKWebsiteDataStore?
    /// The live main web view, used only to open a chat path on the exact
    /// selected origin. Weak: closing or replacing the view clears it naturally.
    private weak var webView: WKWebView?
    /// One selected chat path waiting for a web view to attach. It is cleared
    /// by a connection change and never claimed to have opened until loaded.
    private var pendingChatID: String?
    /// Invalidates a queued attach load when a newer selection or connection
    /// change supersedes it.
    private var chatNavigationGeneration = 0

    /// The one place a chat URL is handed to this session's own view.
    /// Production calls `WKWebView.load`; tests replace it to observe the exact
    /// view and URL at that boundary. A test process cannot rely on WebKit
    /// delivering a navigation delegate callback before the assertion deadline,
    /// so the load decision itself has to be observable without a server.
    var loadChatURL: (WKWebView, URL) -> Void = { webView, url in
        webView.load(URLRequest(url: url))
    }

    var isConnected: Bool {
        if case .connected = connection { return true }
        return false
    }

    func reportConnected(_ origin: Origin, dataStore: WKWebsiteDataStore? = nil) {
        if currentOrigin?.canonicalKey != origin.canonicalKey {
            webView = nil
        }
        resetWorkspaceAccent()
        pendingChatID = nil
        chatNavigationGeneration += 1
        currentOrigin = origin
        self.dataStore = dataStore
        connection = .connected(origin.canonicalKey)
    }

    func reportDisconnected() {
        resetWorkspaceAccent()
        currentOrigin = nil
        dataStore = nil
        webView = nil
        pendingChatID = nil
        chatNavigationGeneration += 1
        connection = .disconnected
    }

    /// Called by the WebWindow once WebKit created the connection's view. A
    /// late callback from a replaced connection is refused by origin and by
    /// view identity.
    func attachWebView(_ webView: WKWebView, for origin: Origin) {
        guard currentOrigin?.canonicalKey == origin.canonicalKey else { return }
        if self.webView !== webView {
            // A new view has not reported anything yet; its document replaces
            // the previous page's accent.
            resetWorkspaceAccent()
        }
        self.webView = webView
        guard let pendingChatID else { return }
        self.pendingChatID = nil
        guard let url = ChatLink.url(origin: origin, chatID: pendingChatID) else { return }
        chatNavigationGeneration += 1
        let generation = chatNavigationGeneration
        // The representable loads its root after this callback; wait one turn
        // so the pending chat path is the last navigation. Re-check the
        // generation, origin, and that a live view still exists; a newer
        // selection or connection change supersedes this load.
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  self.chatNavigationGeneration == generation,
                  self.currentOrigin?.canonicalKey == origin.canonicalKey,
                  let webView = self.webView else { return }
            self.loadChatURL(webView, url)
        }
    }

    /// Applies the accent reported by the connection's own main frame. A value
    /// from another origin, a replaced view, or a disconnected session is
    /// ignored: view identity and the exact origin both have to match, so a
    /// stale page callback cannot repaint the Dock. `nil` (neutral, unknown, or
    /// a navigation reset) restores the bundled icon.
    func reportWorkspaceAccent(_ accent: WorkspaceColorPreset?, from view: WKWebView, for origin: Origin) {
        guard currentOrigin?.canonicalKey == origin.canonicalKey,
              self.webView === view else { return }
        guard workspaceAccent != accent else { return }
        workspaceAccent = accent
        workspaceIcon?.apply(accent)
    }

    /// Restores the bundled icon. The controller deduplicates repeats, so a
    /// connect/attach/disconnect sequence does not repaint for no reason.
    private func resetWorkspaceAccent() {
        workspaceAccent = nil
        workspaceIcon?.reset()
    }

    /// Opens the intended chat in the existing in-app window. The URL is built
    /// from the selected origin and a single validated path segment; the web
    /// view's navigation delegate then re-checks it like any other navigation.
    /// If the view is not attached yet, the request is queued for the next
    /// attach on this same origin and reported as accepted, not as opened.
    @discardableResult
    func openChat(chatID: String) -> Bool {
        guard let origin = currentOrigin,
              let url = ChatLink.url(origin: origin, chatID: chatID) else { return false }
        chatNavigationGeneration += 1
        guard let webView else {
            pendingChatID = chatID
            return true
        }
        pendingChatID = nil
        loadChatURL(webView, url)
        return true
    }
}

/// The active host's entry in a `HostFleetModel`: a `ConnectionContext` that
/// forwards the session's live origin and store.
///
/// `origin` pins the entry to the host it was built for. A connection change
/// fires one event that both the fleet and the previous entry observe, and the
/// fleet re-keys on the next turn; without the pin, the old entry would read
/// the new origin in that gap and could fetch, or render, another host's chats
/// under its own label. A reconnect to the same origin still forwards the new
/// store, because only the origin is pinned.
@MainActor
final class SessionHostContext: ConnectionContext {
    private let status: AppSessionStatus
    private let pinnedOrigin: Origin?

    init(_ status: AppSessionStatus, origin: Origin? = nil) {
        self.status = status
        self.pinnedOrigin = origin
    }

    var isConnected: Bool {
        guard status.isConnected else { return false }
        guard let pinnedOrigin else { return true }
        return status.currentOrigin?.canonicalKey == pinnedOrigin.canonicalKey
    }

    var currentOrigin: Origin? {
        isConnected ? (pinnedOrigin ?? status.currentOrigin) : nil
    }

    var dataStore: WKWebsiteDataStore? {
        isConnected ? status.dataStore : nil
    }

    var connectionPublisher: AnyPublisher<Void, Never> {
        status.$connection.map { _ in () }.eraseToAnyPublisher()
    }
}
