import Foundation

/// The local-mode connection decisions (#3129 S5b), kept free of any view so
/// they can be tested: when a published running origin starts a probe, when
/// it replaces a session, and what happens when the probe or the web view
/// fails. `ConnectionView` executes the returned effects.
///
/// Rules this encodes:
/// - A failure is a state, not a trigger: after a failed probe or web view
///   load nothing reconnects by itself. Only a different origin from the
///   engine (a restart on another port) or a manual retry starts a new
///   attempt, and manual retries are bounded.
/// - An origin published while a probe for another one is in flight is
///   remembered and probed when that probe ends; the stale probe's result is
///   discarded, so the old origin can never overwrite the new one.
final class LocalConnectionModel {
    enum Phase: Equatable {
        case idle
        case probing(Origin)
        case connected(Origin)
        case failed(origin: Origin, message: String)
    }

    enum Effect: Equatable {
        /// Tear down the live session (web view, data store, menu bar state).
        case dropSession
        /// Run the connection probe against this origin.
        case probe(Origin)
        /// The probe answered: build the session for this origin.
        case establish(Origin)
    }

    /// Manual Try Again presses allowed for a failed connection before the
    /// button goes away; Show Log and Connect to Another Server remain.
    static let maxManualRetries = 3

    private(set) var phase: Phase = .idle
    private(set) var retries = 0
    private var pending: Origin?

    var canRetry: Bool { retries < Self.maxManualRetries }

    func engineRunning(_ candidate: Origin) -> [Effect] {
        switch phase {
        case .idle:
            phase = .probing(candidate)
            return [.probe(candidate)]
        case .probing(let current):
            pending = candidate == current ? nil : candidate
            return []
        case .connected(let current):
            guard current != candidate else { return [] }
            phase = .probing(candidate)
            return [.dropSession, .probe(candidate)]
        case .failed(let failed, _):
            // The same origin after a failure is not new information; only a
            // different one (the engine moved) is worth another attempt.
            guard failed != candidate else { return [] }
            retries = 0
            phase = .probing(candidate)
            return [.probe(candidate)]
        }
    }

    func probeFinished(_ origin: Origin, ok: Bool) -> [Effect] {
        guard case .probing(let current) = phase, current == origin else { return [] }
        if let next = pending {
            pending = nil
            phase = .probing(next)
            return [.probe(next)]
        }
        if ok {
            phase = .connected(origin)
            retries = 0
            return [.establish(origin)]
        }
        phase = .failed(origin: origin, message: Self.probeFailureMessage(origin))
        return []
    }

    /// The connected web view could not load.
    func sessionFailed(_ message: String) -> [Effect] {
        guard case .connected(let origin) = phase else { return [] }
        phase = .failed(origin: origin, message: message)
        return [.dropSession]
    }

    /// Try Again on a failed connection, against the engine's current origin
    /// when it has one. Bounded by `maxManualRetries`.
    func retry(engineOrigin: Origin?) -> [Effect] {
        guard case .failed(let failed, _) = phase, canRetry else { return [] }
        retries += 1
        let target = engineOrigin ?? failed
        phase = .probing(target)
        return [.probe(target)]
    }

    /// Connect to Another Server: forget everything.
    func reset() {
        phase = .idle
        pending = nil
        retries = 0
    }

    static func probeFailureMessage(_ origin: Origin) -> String {
        "The Ciaobot engine started, but it did not answer the connection check at \(origin.canonicalKey). Try again, open the log, or connect to another server."
    }
}

enum LocalEngineAction: Equatable {
    case tryAgain, showLog, connectElsewhere
}

/// What the local-mode screen shows for an engine state and a connection
/// phase. Pure so the state-to-button mapping is testable without rendering.
struct LocalEnginePresentation: Equatable {
    let message: String
    let showsProgress: Bool
    let actions: [LocalEngineAction]

    static let allActions: [LocalEngineAction] = [.tryAgain, .showLog, .connectElsewhere]

    static func make(
        engine: EngineState,
        connection: LocalConnectionModel.Phase,
        canRetry: Bool
    ) -> LocalEnginePresentation {
        switch engine {
        case .failed(let message):
            return LocalEnginePresentation(message: message, showsProgress: false, actions: allActions)
        case .portInUse(let port):
            return LocalEnginePresentation(message: portInUseMessage(port), showsProgress: false, actions: allActions)
        case .stopping:
            return LocalEnginePresentation(message: "Stopping the Ciaobot engine…", showsProgress: true, actions: [])
        case .stopped, .starting:
            return LocalEnginePresentation(message: "Starting Ciaobot…", showsProgress: true, actions: [])
        case .running:
            if case .failed(_, let message) = connection {
                return LocalEnginePresentation(
                    message: message,
                    showsProgress: false,
                    actions: canRetry ? allActions : [.showLog, .connectElsewhere]
                )
            }
            return LocalEnginePresentation(message: "Connecting to the Ciaobot engine…", showsProgress: true, actions: [])
        }
    }

    static func portInUseMessage(_ port: Int) -> String {
        "The Ciaobot engine could not start: port \(port) on this Mac is already in use. Quit the program using that port, then try again."
    }

    /// Whether the engine itself is down and the web view should give way to
    /// the status screen.
    static func engineIsDown(_ engine: EngineState) -> Bool {
        switch engine {
        case .failed, .portInUse: return true
        default: return false
        }
    }

    enum Retry: Equatable {
        case restartEngine, startEngine, reconnect(Origin), none
    }

    /// What Try Again does for an engine state.
    static func retry(engine: EngineState) -> Retry {
        switch engine {
        case .failed: return .restartEngine
        case .portInUse: return .startEngine
        case .running(let origin, _, _): return .reconnect(origin)
        default: return .none
        }
    }
}
