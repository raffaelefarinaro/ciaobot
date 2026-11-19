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

/// What the bundled engine is doing, as the update gate reads it.
enum LocalEngineState: Equatable {
    /// Nothing this app owns is running: an external server, a stopped or
    /// failed engine, or a port held by another process. Nothing to drain.
    case external
    /// The bundled engine answers on this loopback port.
    case running(port: Int)
    /// The bundled engine is starting or stopping. No reading of it is idle.
    case transitioning
}

/// The engine's drain handshake as the install gate uses it.
@MainActor
protocol EngineDraining: AnyObject {
    func drain(port: Int, owner: String) async -> Result<DrainReading, DrainRefusal>
    /// True only when the engine confirms admission is open again: a 2xx answer
    /// with `draining` false.
    func cancel(port: Int, owner: String) async -> Bool
}

/// Holds a verified Sparkle install until the bundled engine is idle, then
/// lets Sparkle install and relaunch.
///
/// Without a local engine (external mode, or no engine running) the install
/// goes ahead on the next main turn, as it always did. With a running engine
/// the gate drains it every `pollInterval`: admission closes on the engine,
/// and the install waits for `idleReadingsRequired` idle readings in a row. A
/// starting or stopping engine is never idle. A drain that does not settle
/// within `drainTimeout` (measured on the clock), a busy refusal, or
/// `unreachableLimit` failed polls in a row cancels the drain and retries the
/// whole gate after `retryAfter`. The gate never installs blind while the
/// engine may be running.
@MainActor
final class UpdateInstallGate {
    static let pollInterval: TimeInterval = 5
    static let idleReadingsRequired = 3
    static let drainTimeout: TimeInterval = 600
    static let retryAfter: TimeInterval = 1800
    static let unreachableLimit = 12
    /// A cancel the engine does not confirm is sent again: three attempts in
    /// all, five seconds apart.
    static let cancelAttempts = 3
    static let cancelRetryInterval: TimeInterval = 5
    /// Sparkle install-phase error codes (`SUError` in SUErrors.h, domain
    /// `SUSparkleErrorDomain`) that mean the install this gate holds is over.
    /// Everything else, such as "no update" or appcast and download trouble
    /// from a background check, leaves a held install alone.
    static let installAbortCodes: Set<Int> = [4002, 4003, 4004, 4005, 4006, 4007, 4009, 4010, 4012]

    typealias Schedule = (TimeInterval, @escaping () -> Void) -> Void
    typealias LocalState = () -> LocalEngineState
    /// Seconds on a monotonic clock.
    typealias Clock = () -> TimeInterval

    /// Runs work on the main queue after `delay`, outside any delegate call.
    static let mainQueueSchedule: Schedule = { delay, work in
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
            MainActor.assumeIsolated { work() }
        }
    }

    /// The real clock. It does not move with the wall clock, so the drain
    /// timeout keeps its meaning across a sleep or a time change.
    static let monotonicClock: Clock = { ProcessInfo.processInfo.systemUptime }

    /// The operation that owns this gate's drain: `sparkle-<appcast version>`.
    let owner: String

    private let localState: LocalState
    private let drainer: EngineDraining
    private let willInstall: () -> Void
    private let schedule: Schedule
    private let clock: Clock

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
    /// When the current drain began, on `clock`.
    private var drainStartedAt: TimeInterval = 0

    /// True once an install is scheduled and has not yet run or been reset.
    var isInstalling: Bool { installing }

    init(
        localState: @escaping LocalState,
        drainer: EngineDraining,
        owner: String,
        willInstall: @escaping () -> Void,
        schedule: @escaping Schedule,
        clock: @escaping Clock = UpdateInstallGate.monotonicClock
    ) {
        self.localState = localState
        self.drainer = drainer
        self.owner = owner
        self.willInstall = willInstall
        self.schedule = schedule
        self.clock = clock
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
        drainStartedAt = clock()
        step(attempt)
    }

    /// Takes one reading now; its outcome decides what happens next.
    private func step(_ id: Int) {
        guard id == attempt else { return }
        switch localState() {
        case .external:
            // Nothing this app owns is running, so nothing is draining: install.
            installNow()
        case .transitioning:
            // A starting or stopping engine has no trustworthy reading. It
            // counts as not idle, and the drain timeout still applies.
            idleReadings = 0
            nextReading(id)
        case .running(let port):
            if let held = drainingPort, held != port {
                // The engine moved to another port. The drain held on the old
                // one is cancelled, and no idle streak carries over.
                cancelDrain(port: held)
                idleReadings = 0
            }
            drainingPort = port
            Task { @MainActor in
                let result = await self.drainer.drain(port: port, owner: self.owner)
                self.absorb(result, port: port, attempt: id)
            }
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
            retry()
            return
        case .failure(.unreachable):
            idleReadings = 0
            unreachableReadings += 1
            if unreachableReadings >= Self.unreachableLimit {
                retry()
                return
            }
        }
        nextReading(id)
    }

    /// Schedules the next reading, or retries when the drain has run out of time.
    private func nextReading(_ id: Int) {
        if clock() - drainStartedAt >= Self.drainTimeout {
            retry()
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
        let decided = localState()
        schedule(0) { [weak self] in
            // Reads the handler at the turn, so a second `takeControl` that
            // arrived meanwhile is the one that runs.
            guard let self, self.installing, self.attempt == id, let handler = self.install else { return }
            guard self.localState() == decided else {
                // The engine changed state after the reading that decided the
                // install. That reading no longer describes it, so the gate
                // starts again from a fresh streak.
                self.installing = false
                self.begin()
                return
            }
            self.install = nil
            self.running = false
            self.willInstall()
            handler()
        }
    }

    /// Cancels the drain and starts the whole gate again after `retryAfter`.
    private func retry() {
        if let port = drainingPort { cancelDrain(port: port) }
        drainingPort = nil
        attempt += 1
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
    }

    private func cancelDrain(port: Int) {
        Self.sendCancel(port: port, owner: owner, drainer: drainer, schedule: schedule)
    }

    /// Sends a drain cancel, and sends it again while the engine has not
    /// confirmed admission is open. It captures no gate state, so an abort or a
    /// replacement gate cannot stop it. A cancel still unconfirmed after the
    /// last attempt is logged, not retried forever.
    private static func sendCancel(
        port: Int,
        owner: String,
        drainer: EngineDraining,
        schedule: @escaping Schedule,
        attempt: Int = 1
    ) {
        Task { @MainActor in
            if await drainer.cancel(port: port, owner: owner) { return }
            guard attempt < cancelAttempts else {
                NSLog("Ciaobot update: the engine did not confirm closing the drain for %@ after %d attempts.", owner, attempt)
                return
            }
            schedule(cancelRetryInterval) {
                sendCancel(port: port, owner: owner, drainer: drainer, schedule: schedule, attempt: attempt + 1)
            }
        }
    }
}

/// Refuses every redirect: the drain answer must come from the engine that was
/// asked, never from wherever a 3xx points.
private final class RefuseRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}

/// The real drain client: two POSTs to this Mac's engine on loopback. It is
/// session-free: no cookie, no credential, no bearer token, and nothing from a
/// signed-in web view.
@MainActor
final class EngineDrainClient: EngineDraining {
    /// Five seconds per request: a drain answer is a small JSON document.
    static let timeout: TimeInterval = 5

    /// The one session configuration the drain client uses: ephemeral, with no
    /// cookies, no credential storage, no cache, and the five-second bound.
    static func makeConfiguration() -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.httpCookieAcceptPolicy = .never
        configuration.httpCookieStorage = nil
        configuration.urlCredentialStorage = nil
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = timeout
        configuration.timeoutIntervalForResource = timeout
        return configuration
    }

    private static let refusesRedirects = RefuseRedirects()

    private let session: URLSession

    init(session: URLSession? = nil) {
        self.session = session ?? URLSession(configuration: Self.makeConfiguration())
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

    func cancel(port: Int, owner: String) async -> Bool {
        guard let (status, data) = await post("api/admin/drain/cancel", port: port, owner: owner),
              (200...299).contains(status),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              (object["draining"] as? Bool) == false else {
            return false
        }
        return true
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

    /// One POST to the loopback engine. The host is `127.0.0.1` by design: the
    /// engine's drain routes exist only on this Mac's loopback interface. Any
    /// redirect is refused, and a body over the shared cap is no answer.
    private func post(_ path: String, port: Int, owner: String) async -> (Int, Data)? {
        guard let url = URL(string: "http://127.0.0.1:\(port)/\(path)") else { return nil }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.httpShouldHandleCookies = false
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["owner": owner])
        do {
            let (bytes, response) = try await session.bytes(for: request, delegate: Self.refusesRedirects)
            guard let http = response as? HTTPURLResponse else { return nil }
            var data = Data()
            for try await byte in bytes {
                data.append(byte)
                if data.count > RestartBlockersProbe.maximumResponseBytes { return nil }
            }
            return (http.statusCode, data)
        } catch {
            return nil
        }
    }
}
