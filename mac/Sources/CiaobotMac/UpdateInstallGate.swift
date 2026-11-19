import Foundation

/// What one drain poll reported. `scheduleWork` is nil when the engine sent no
/// count, which the gate reads as "not idle".
struct DrainReading: Equatable {
    let activeChats: Int
    let scheduleWork: Int?
}

/// Why a drain poll gave no reading. `busyRestart` is the engine's 409: a
/// Settings restart or another operation owns the drain. Every other failure
/// is `unreachable`.
enum DrainRefusal: Error, Equatable {
    case busyRestart
    case unreachable
}

/// The engine's drain handshake as the install gate uses it.
@MainActor
protocol EngineDraining: AnyObject {
    func drain(port: Int, owner: String) async -> Result<DrainReading, DrainRefusal>
    func cancel(port: Int, owner: String) async
}

/// Holds a verified Sparkle install until the bundled engine is idle, then
/// lets Sparkle install and relaunch.
///
/// Without a local engine (external mode, or no engine running) the install
/// goes ahead on the next main turn, as it always did. With a running engine
/// the gate drains it every `pollInterval`: admission closes on the engine,
/// and the install waits for `idleReadingsRequired` idle readings in a row. A
/// drain that does not settle within `drainTimeout`, a busy refusal, or
/// `unreachableLimit` failed polls in a row cancels the drain and retries the
/// whole gate after `retryAfter`. The gate never installs blind while the
/// supervisor reports a running engine.
@MainActor
final class UpdateInstallGate {
    static let pollInterval: TimeInterval = 5
    static let idleReadingsRequired = 3
    static let drainTimeout: TimeInterval = 600
    static let retryAfter: TimeInterval = 1800
    static let unreachableLimit = 12

    typealias Schedule = (TimeInterval, @escaping () -> Void) -> Void

    /// Runs work on the main queue after `delay`, outside any delegate call.
    static let mainQueueSchedule: Schedule = { delay, work in
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
            MainActor.assumeIsolated { work() }
        }
    }

    /// The operation that owns this gate's drain: `sparkle-<appcast version>`.
    let owner: String

    private let localPort: () -> Int?
    private let drainer: EngineDraining
    private let willInstall: () -> Void
    private let schedule: Schedule

    /// The handler Sparkle is waiting on. A second `takeControl` replaces it.
    private var install: (() -> Void)?
    /// Advanced whenever an attempt ends, so work an earlier attempt scheduled does nothing.
    private var attempt = 0
    private var running = false
    /// True from the moment the install is scheduled until the gate is reset.
    private var installing = false
    /// The port of the engine this gate has closed admission on, if any.
    private var drainingPort: Int?
    private var idleReadings = 0
    private var unreachableReadings = 0
    /// Time spent draining, counted in poll intervals so the timeout is
    /// deterministic under test and never depends on the wall clock.
    private var elapsed: TimeInterval = 0

    init(
        localPort: @escaping () -> Int?,
        drainer: EngineDraining,
        owner: String,
        willInstall: @escaping () -> Void,
        schedule: @escaping Schedule
    ) {
        self.localPort = localPort
        self.drainer = drainer
        self.owner = owner
        self.willInstall = willInstall
        self.schedule = schedule
    }

    /// Takes Sparkle's install handler. Always `true`: this client installs
    /// the update itself, so Sparkle must not wait for a quit. Nothing runs
    /// inside the delegate call.
    func takeControl(_ install: @escaping () -> Void) -> Bool {
        self.install = install
        guard !running else { return true }
        running = true
        begin()
        return true
    }

    /// Sparkle gave up on the install. A drain this gate holds is cancelled,
    /// and the gate returns to idle.
    func installAborted() {
        let port = drainingPort
        reset()
        guard let port else { return }
        cancelDrain(port: port)
    }

    // MARK: - Attempts

    private func begin() {
        idleReadings = 0
        unreachableReadings = 0
        elapsed = 0
        step(attempt)
    }

    /// Takes one reading now; its outcome decides what happens next.
    private func step(_ id: Int) {
        guard id == attempt else { return }
        guard let port = localPort() else {
            // No local engine to drain, so nothing can be running: install.
            installNow()
            return
        }
        drainingPort = port
        Task { @MainActor in
            let result = await self.drainer.drain(port: port, owner: self.owner)
            self.absorb(result, port: port, attempt: id)
        }
    }

    private func absorb(_ result: Result<DrainReading, DrainRefusal>, port: Int, attempt id: Int) {
        guard id == attempt else {
            // A reading that landed after its attempt ended may have closed
            // admission on the engine after the cancel. Cancelling again is
            // harmless when it did not, and a stale reading never cancels an
            // install that is already on its way.
            if !installing { cancelDrain(port: port) }
            return
        }
        switch result {
        case .success(let reading):
            unreachableReadings = 0
            let idle = reading.activeChats == 0 && reading.scheduleWork == 0
            idleReadings = idle ? idleReadings + 1 : 0
            if idleReadings >= Self.idleReadingsRequired {
                installNow()
                return
            }
        case .failure(.busyRestart):
            retry(port: port)
            return
        case .failure(.unreachable):
            idleReadings = 0
            unreachableReadings += 1
            if unreachableReadings >= Self.unreachableLimit {
                retry(port: port)
                return
            }
        }
        elapsed += Self.pollInterval
        if elapsed >= Self.drainTimeout {
            retry(port: port)
            return
        }
        schedule(Self.pollInterval) { [weak self] in self?.step(id) }
    }

    /// Runs `willInstall` and then Sparkle's handler on the next main turn, not
    /// inside the drain reply. The drain stays held until the install is over,
    /// so `installAborted` still cancels it if Sparkle gives up first.
    private func installNow() {
        guard install != nil else { return }
        installing = true
        attempt += 1
        let id = attempt
        schedule(0) { [weak self] in
            // Reads the handler at the turn, so a second `takeControl` that
            // arrived meanwhile is the one that runs.
            guard let self, self.installing, self.attempt == id, let handler = self.install else { return }
            self.install = nil
            self.running = false
            self.willInstall()
            handler()
        }
    }

    /// Cancels the drain and starts the whole gate again after `retryAfter`.
    private func retry(port: Int) {
        cancelDrain(port: port)
        attempt += 1
        drainingPort = nil
        let next = attempt
        schedule(Self.retryAfter) { [weak self] in
            guard let self, self.attempt == next, self.running else { return }
            self.begin()
        }
    }

    private func reset() {
        attempt += 1
        running = false
        installing = false
        install = nil
        drainingPort = nil
        idleReadings = 0
        unreachableReadings = 0
        elapsed = 0
    }

    private func cancelDrain(port: Int) {
        let owner = self.owner
        let drainer = self.drainer
        Task { @MainActor in
            await drainer.cancel(port: port, owner: owner)
        }
    }
}

/// The real drain client: two POSTs to this Mac's engine on loopback. It is
/// session-free: no cookie, no credential, no bearer token, and nothing from a
/// signed-in web view.
@MainActor
final class EngineDrainClient: EngineDraining {
    /// Five seconds per request: a drain answer is a small JSON document.
    static let timeout: TimeInterval = 5

    private let session: URLSession

    init(session: URLSession? = nil) {
        if let session {
            self.session = session
            return
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.httpCookieAcceptPolicy = .never
        configuration.httpCookieStorage = nil
        configuration.urlCredentialStorage = nil
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = Self.timeout
        configuration.timeoutIntervalForResource = Self.timeout
        self.session = URLSession(configuration: configuration)
    }

    func drain(port: Int, owner: String) async -> Result<DrainReading, DrainRefusal> {
        guard let answer = await post("api/admin/drain", port: port, owner: owner) else {
            return .failure(.unreachable)
        }
        let (status, data) = answer
        if status == 409 { return .failure(.busyRestart) }
        guard (200...299).contains(status), let reading = Self.reading(from: data) else {
            return .failure(.unreachable)
        }
        return .success(reading)
    }

    func cancel(port: Int, owner: String) async {
        _ = await post("api/admin/drain/cancel", port: port, owner: owner)
    }

    /// The reading in a drain answer. `active_chat_ids` is required; a
    /// `schedule_work_count` that is missing, boolean or not a whole number
    /// stays nil, which the gate reads as not idle.
    static func reading(from data: Data) -> DrainReading? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let active = object["active_chat_ids"] as? [Any] else { return nil }
        var scheduleWork: Int?
        if let number = object["schedule_work_count"] as? NSNumber,
           CFGetTypeID(number) != CFBooleanGetTypeID() {
            let value = number.doubleValue
            if value >= 0, value == value.rounded(.towardZero) { scheduleWork = Int(value) }
        }
        return DrainReading(activeChats: active.count, scheduleWork: scheduleWork)
    }

    private func post(_ path: String, port: Int, owner: String) async -> (Int, Data)? {
        guard let url = URL(string: "http://127.0.0.1:\(port)/\(path)") else { return nil }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.httpShouldHandleCookies = false
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["owner": owner])
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { return nil }
            return (http.statusCode, data)
        } catch {
            return nil
        }
    }
}
