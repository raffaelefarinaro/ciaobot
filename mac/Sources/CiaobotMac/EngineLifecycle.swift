import AppKit
import CoreServices

/// Why this app was asked to quit (#3129 S6). A quit from any user surface —
/// app menu, tray, Dock, AppleScript — is `.user`. A quit because macOS is
/// ending the session (log out, restart, shutdown) is `.system`: the session
/// ending already is the user's decision, so that path skips the quit dialog
/// and still stops the engine.
enum QuitReason {
    case user
    case system
    /// Sparkle's install is replacing the bundle: the user already chose to
    /// install, so there is no dialog, and the engine gets the full quit grace.
    case update
}

/// The quit confirmation as presented: fixed copy, fixed buttons.
struct EngineQuitDialog: Equatable {
    let title: String
    let body: String
}

/// The pure lifecycle facts and copy of #3129 S6: why the system asked to
/// quit, whether this launch came from a login item, what the confirmations
/// say, and which termination plan a launch shape calls for. Tests exercise
/// all of it with constructed Apple events and plain values; no dialog, no
/// server, and no process is involved.
enum EngineLifecycle {
    // MARK: Quit reason

    /// The `kAEQuitReason` attribute, when the event is a quit Apple event.
    /// Only the session-ending reasons count as `.system`; anything else —
    /// a user quit, a quit event without a reason, or no Apple event at all
    /// (the tray does not send one) — reads as `.user`.
    ///
    /// `updateInstall` is true while Sparkle's install is taking the app down to
    /// replace the bundle: that terminate is `.update`, never a user quit.
    static func quitReason(_ event: NSAppleEventDescriptor?, updateInstall: Bool = false) -> QuitReason {
        if updateInstall { return .update }
        guard let event, event.eventClass == kCoreEventClass, event.eventID == kAEQuitApplication,
              let reason = event.attributeDescriptor(forKeyword: kAEQuitReason) else { return .user }
        switch reason.enumCodeValue {
        case kAELogOut, kAEReallyLogOut, kAEShowRestartDialog, kAERestart, kAEShowShutdownDialog, kAEShutDown:
            return .system
        default:
            return .user
        }
    }

    // MARK: Login launch

    /// True when the launch Apple event names `keyAELaunchedAsLogInItem`: this
    /// process was started by its login item, so it opens no main window. The
    /// launch Apple event is the current one only during
    /// `applicationWillFinishLaunching`; read it there.
    static func isLoginLaunch(_ event: NSAppleEventDescriptor?) -> Bool {
        guard let event, event.eventClass == kCoreEventClass, event.eventID == kAEOpenApplication else {
            return false
        }
        return event.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem
    }

    // MARK: Quit confirmation copy

    /// The quit confirmation's two buttons, in presentation order.
    static let quitConfirmButton = "Quit"
    static let quitCancelButton = "Cancel"

    /// The quit confirmation's fixed copy. The body always states what
    /// stopping the engine means. `restart_blockers` counts more than chats
    /// (open questions and permission requests, background agents), so when
    /// it reports anything the body says "chats or background work", not a
    /// chat count.
    static func quitMessage(blockers: Int?) -> (title: String, body: String) {
        var body = "This also stops Ciaobot on this Mac: chats, schedules and access from your phone pause until you open Ciaobot again."
        if let blockers, blockers > 0 {
            body += " Chats or background work are still running and will be interrupted."
        }
        return ("Quit Ciaobot?", body)
    }

    // MARK: Termination plan

    /// What one terminate request does.
    enum TerminationPlan: Equatable {
        /// No engine stops; the app leaves at once.
        case terminateNow
        /// The quit confirmation decides; only a confirmed quit stops the engine.
        case confirm
        /// Stop the engine first — SIGTERM, SIGKILL after the grace — and
        /// leave only once it is gone.
        case stopThenTerminate(grace: TimeInterval)
    }

    /// The quit grace for a confirmed user quit (drain first, then SIGKILL).
    static let quitGrace: TimeInterval = 35
    /// The shorter grace for a system quit: logout and shutdown give the engine
    /// less time, so the stop is still bounded.
    static let systemQuitGrace: TimeInterval = 20

    /// One quit decision for every launch shape:
    /// - external mode, or no engine, leaves at once — there is nothing this
    ///   app owns to stop (`portInUse` is the same: the port is held by
    ///   something this app did not start, and quitting it does not stop that);
    /// - a stop that is already in progress is joined, so the app leaves only
    ///   after the drain finishes;
    /// - a system quit of a starting or running local engine stops it and
    ///   never asks;
    /// - a user quit of a starting or running local engine asks first.
    static func terminationPlan(mode: EngineMode, engineState: EngineState?, quitReason: QuitReason) -> TerminationPlan {
        guard mode == .local, let engineState else { return .terminateNow }
        switch engineState {
        case .stopped, .failed, .portInUse:
            return .terminateNow
        case .stopping:
            return .stopThenTerminate(grace: quitGrace)
        case .starting, .running:
            switch quitReason {
            case .user: return .confirm
            case .system: return .stopThenTerminate(grace: systemQuitGrace)
            case .update: return .stopThenTerminate(grace: quitGrace)
            }
        }
    }

    // MARK: Mode-switch confirmation copy

    /// The mode-switch confirmation's fixed copy (S6).
    struct ModeSwitchDialog: Equatable {
        let title: String
        let body: String
        let confirm: String
        let cancel: String
    }

    static let modeSwitchDialog = ModeSwitchDialog(
        title: "Connect to another server?",
        body: "Ciaobot on this Mac stops while you use another server.",
        confirm: "Connect to Another Server",
        cancel: "Cancel"
    )

    /// The real AppKit confirmation: a two-button modal alert with `confirm`
    /// first, so Return confirms and Escape cancels.
    @MainActor
    static func presentConfirmation(title: String, body: String, confirm: String, cancel: String) -> Bool {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = body
        alert.alertStyle = .warning
        alert.addButton(withTitle: confirm)
        alert.addButton(withTitle: cancel)
        return alert.runModal() == .alertFirstButtonReturn
    }
}

/// The running-chat count for the quit confirmation, from the engine's
/// `/api/admin/restart-blockers`: a loopback-only route that needs no session
/// (`ciao service` reads it the same way). nil on any failure — the dialog
/// then simply says nothing about chats.
protocol RestartBlockersFetching: AnyObject {
    func activeChats(of origin: Origin, completion: @escaping (Int?) -> Void)
}

/// Reads the blockers count with a one-second bound. The count is display
/// copy only: a redirect, an oversized body, a wrong status, or any error is
/// no count rather than zero, so a foreign listener cannot silently claim no
/// chats are working.
final class RestartBlockersProbe: NSObject, URLSessionDataDelegate, RestartBlockersFetching {
    /// One second, per the S6 plan: a quit path must not wait longer for the count.
    static let timeout: TimeInterval = 1
    static let maximumResponseBytes = 64 * 1024

    private var body = Data()
    private var completion: ((Int?) -> Void)?

    /// The count in a blockers answer, or nil for anything else.
    static func activeChats(status: Int, data: Data) -> Int? {
        guard (200...299).contains(status), data.count <= maximumResponseBytes,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let blockers = object["restart_blockers"] as? [Any] else { return nil }
        return blockers.count
    }

    func activeChats(of origin: Origin, completion: @escaping (Int?) -> Void) {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.httpCookieAcceptPolicy = .never
        configuration.httpCookieStorage = nil
        configuration.urlCredentialStorage = nil
        configuration.httpAdditionalHeaders = nil
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = Self.timeout
        configuration.timeoutIntervalForResource = Self.timeout
        let session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
        // One probe serves every quit: a previous answer's bytes must never
        // prefix this one's.
        self.body = Data()
        self.completion = completion
        var request = URLRequest(url: origin.url.appendingPathComponent("api/admin/restart-blockers"))
        request.httpShouldHandleCookies = false
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let task = session.dataTask(with: request)
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.timeout) { task.cancel() }
        task.resume()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard data.count <= Self.maximumResponseBytes - body.count else {
            dataTask.cancel()
            return
        }
        body.append(data)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        let status = (task.response as? HTTPURLResponse)?.statusCode ?? 0
        let found = error == nil ? Self.activeChats(status: status, data: body) : nil
        let callback = completion
        completion = nil
        session.finishTasksAndInvalidate()
        DispatchQueue.main.async { callback?(found) }
    }
}

/// Set while Sparkle's immediate install is terminating the app to replace
/// the bundle. The install handler raises it just before it runs, so the
/// terminate that follows reads as an update quit (`QuitReason.update`): no
/// dialog stands in front of an update the user already chose, and the engine
/// still drains with the full quit grace. It is cleared when Sparkle abandons
/// the install, so a later quit is a user quit again. One instance is shared by
/// the updater and the app delegate.
@MainActor
final class UpdateQuitIntent {
    private(set) var installing = false
    /// True when the raised install drained the bundled engine first. Sparkle's
    /// own "Install and Relaunch" raises an install with no drain, so its
    /// terminate still asks about running chats.
    private(set) var drained = false
    /// Runs when a raised install is abandoned by a cancelled quit. The updater
    /// sets it to cancel the gate's held drain.
    var onAbandon: (@MainActor () -> Void)?

    func beginInstall(drained: Bool) {
        installing = true
        self.drained = drained
    }

    func endInstall() {
        installing = false
        drained = false
    }

    /// Ends a raised install that will not happen, and lets the updater release
    /// what it holds for it.
    func abandonInstall() {
        let hook = onAbandon
        endInstall()
        hook?()
    }
}

/// Closes the main window of a login launch once SwiftUI has created it.
///
/// The scene window does not exist at a fixed point of launch, so a single
/// deferred close can run before it does and silently close nothing. This
/// retries on a short interval until the window exists, closes it once, and
/// gives up after a bound so a launch that never creates it leaves nothing
/// polling. The caller's own `willClose` observer applies the Dock preference.
@MainActor
final class LoginLaunchWindowCloser {
    static let interval: TimeInterval = 0.05
    static let maximumAttempts = 100   // 5 s

    typealias Schedule = (TimeInterval, @escaping @MainActor () -> Void) -> Void

    private let window: @MainActor () -> NSWindow?
    private let schedule: Schedule
    private(set) var attempts = 0
    private(set) var closed = false

    init(window: @escaping @MainActor () -> NSWindow?, schedule: Schedule? = nil) {
        self.window = window
        self.schedule = schedule ?? { delay, work in
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                MainActor.assumeIsolated { work() }
            }
        }
    }

    func start() { attempt() }

    private func attempt() {
        guard !closed, attempts < Self.maximumAttempts else { return }
        attempts += 1
        if let window = window() {
            closed = true
            window.close()
            return
        }
        schedule(Self.interval) { [self] in attempt() }
    }
}
