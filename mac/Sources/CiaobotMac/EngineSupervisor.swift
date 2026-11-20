import Foundation
import Darwin

/// Whether this app runs the bundled engine itself (`local`) or connects to a
/// server somebody else runs (`external`). #3129 S5a; the app delegate wires
/// local mode in (S5b).
enum EngineMode: String {
    case local, external

    /// A saved choice wins. Otherwise an Intel host (the bundled engine is
    /// arm64 only) is external, and so is a last connection to a non-loopback
    /// host. No last connection, or a loopback one, is local.
    static func initial(saved: String?, lastOrigin: Origin?, hostIsArm64: Bool) -> EngineMode {
        if let saved, let mode = EngineMode(rawValue: saved) { return mode }
        guard hostIsArm64 else { return .external }
        guard let lastOrigin else { return .local }
        return lastOrigin.isLoopback ? .local : .external
    }
}

protocol EngineModeStoring: AnyObject {
    var mode: EngineMode? { get set }
    /// Consecutive launches that ended rolled back by an engine-level refusal
    /// (see `EngineTakeover.rolledBackStreak`). Reset by a completed takeover and
    /// by a mode pick.
    var rolledBackTakeovers: Int { get set }
}

/// A mode store that touches nothing on disk. The AppDelegate test
/// initializer uses it, so an ordinary test launch reads no defaults.
final class TransientEngineModeStore: EngineModeStoring {
    var mode: EngineMode?
    var rolledBackTakeovers = 0
}

final class UserDefaultsEngineModeStore: EngineModeStoring {
    static let defaultKey = "CiaobotEngineMode"
    static let rolledBackKey = "CiaobotEngineRolledBackTakeovers"
    private let defaults: UserDefaults
    private let key: String
    private let rolledBackKey: String

    init(
        defaults: UserDefaults = .standard,
        key: String = UserDefaultsEngineModeStore.defaultKey,
        rolledBackKey: String = UserDefaultsEngineModeStore.rolledBackKey
    ) {
        self.defaults = defaults
        self.key = key
        self.rolledBackKey = rolledBackKey
    }

    var mode: EngineMode? {
        get { defaults.string(forKey: key).flatMap(EngineMode.init(rawValue:)) }
        set {
            if let newValue { defaults.set(newValue.rawValue, forKey: key) } else { defaults.removeObject(forKey: key) }
        }
    }

    var rolledBackTakeovers: Int {
        get { defaults.integer(forKey: rolledBackKey) }
        set { defaults.set(newValue, forKey: rolledBackKey) }
    }
}

/// Every app-side engine path, derived from one home directory so a test (or
/// an isolated checkpoint) can point the whole set at a temporary one.
struct EngineHome: Equatable {
    let home: URL

    var stateDir: URL { home.appendingPathComponent("Library/Application Support/Ciaobot/engine", isDirectory: true) }
    var readyRecord: URL { stateDir.appendingPathComponent("ready.json") }
    var pointer: URL { stateDir.appendingPathComponent("workspace.json") }
    var takeoverDir: URL { stateDir.appendingPathComponent("takeover", isDirectory: true) }
    var logFile: URL { home.appendingPathComponent("Library/Logs/Ciaobot/engine.log") }
}

enum EngineState: Equatable {
    case stopped
    case starting
    case running(origin: Origin, pid: Int32, startupID: String)
    case stopping
    case portInUse(Int)
    case failed(String)
}

/// What the supervisor needs to know about a live process.
struct EngineProcessInfo: Equatable {
    let parentPID: Int32
    let path: String?
}

protocol EngineProcessRunning: AnyObject {
    /// Starts the launch with stdout and stderr appended to `log`; returns the pid.
    func spawn(_ launch: BundledEngineLaunch, log: URL) throws -> Int32
    func terminate(_ pid: Int32)
    func kill(_ pid: Int32)
    /// nil when no such process is alive.
    func inspect(_ pid: Int32) -> EngineProcessInfo?
    /// Called on the main thread with (pid, exit status) for a process this object spawned.
    var onExit: ((Int32, Int32) -> Void)? { get set }
}

/// The identity `/api/startup-status` reports. `ProbeChecking` answers only
/// yes or no, which cannot show that the answering engine is the one we spawned.
struct EngineIdentity: Equatable {
    let pid: Int32
    let startupID: String
}

protocol EngineIdentityChecking {
    func identity(of origin: Origin, completion: @escaping (EngineIdentity?) -> Void)
}

/// A JSON integer. Booleans (which bridge to 1 and 0) and fractions are not integers.
func jsonInteger(_ value: Any?) -> Int? {
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
    let double = number.doubleValue
    guard double == double.rounded(), abs(double) < 9e15 else { return nil }
    return number.intValue
}

@MainActor
final class EngineSupervisor: ObservableObject {
    static let readyPollInterval: TimeInterval = 0.25
    static let readyTimeout: TimeInterval = 90
    static let runningPollInterval: TimeInterval = 5
    static let retryDelays: [TimeInterval] = [2, 5, 15]
    static let retryWindow: TimeInterval = 300
    /// SIGTERM to SIGKILL for any engine process we signal (an orphan, or a child that failed to become ready).
    static let terminationGrace: TimeInterval = 35
    /// After SIGKILL, how long to wait for the exit report before giving up on it.
    static let killWait: TimeInterval = 5
    static let orphanPollInterval: TimeInterval = 0.5
    static let maximumLogBytes = 20 * 1024 * 1024
    static let startTimeoutMessage = "The Ciaobot engine did not start. See ~/Library/Logs/Ciaobot/engine.log."
    static let unavailableMessage = "This Mac cannot run the Ciaobot engine bundled with the app."
    static let anotherRunningMessage = "Another Ciaobot is already running this workspace. Quit it, then choose Try Again."
    static let spawnFailureMessage = "The Ciaobot engine could not be started. See ~/Library/Logs/Ciaobot/engine.log."

    @Published private(set) var state: EngineState = .stopped

    /// The loopback port of the engine while it is running, else nil. The update
    /// gate drains only what this reports: no running engine means nothing to drain.
    var runningPort: Int? {
        if case .running(let origin, _, _) = state { return origin.portNumber }
        return nil
    }

    /// The bundled engine as the update gate reads it. Only a running engine
    /// has a port to drain. A starting or stopping engine is transitioning and
    /// never idle. Stopped, failed, and port-in-use leave nothing this app owns
    /// running, so there is nothing to drain.
    var updateLocalState: LocalEngineState {
        switch state {
        case .running(let origin, _, _): return .running(port: origin.portNumber)
        case .starting, .stopping: return .transitioning
        case .stopped, .failed, .portInUse: return .external
        }
    }

    private let engine: BundledEngine?
    /// Read by the local engine screen for **Show Log** (`home.logFile`).
    let home: EngineHome
    private let processes: EngineProcessRunning
    private let identityChecker: EngineIdentityChecking
    private let clock: () -> Date
    private let schedule: (TimeInterval, @escaping () -> Void) -> Void
    private let environment: [String: String]
    /// The DEBUG test launch's `--port`; nil in a release build.
    private let enginePort: Int?

    /// Invalidates every timer and probe callback scheduled before a spawn or stop.
    private var generation = 0
    private var child: Int32?
    /// An orphan from an earlier app run that we have SIGTERMed and are waiting on.
    private var reaping: Int32?
    /// The child is being terminated after a readiness timeout: its exit must not retry.
    private var failedChildExiting = false
    /// A start requested while the previous child was still dying.
    private var startQueued = false
    private var killToken = 0
    private var failures: [Date] = []
    private var lastPort = 0
    private var stopCompletions: [() -> Void] = []

    init(
        engine: BundledEngine?,
        home: EngineHome,
        processes: EngineProcessRunning,
        identity: EngineIdentityChecking,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        enginePort: Int? = nil,
        clock: @escaping () -> Date = Date.init,
        schedule: @escaping (TimeInterval, @escaping () -> Void) -> Void = { delay, work in
            DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
        }
    ) {
        self.engine = engine
        self.home = home
        self.processes = processes
        self.identityChecker = identity
        self.environment = environment
        self.enginePort = enginePort
        self.clock = clock
        self.schedule = schedule
        processes.onExit = { [weak self] pid, status in
            MainActor.assumeIsolated { self?.handleExit(pid: pid, status: status) }
        }
    }

    // MARK: Public

    /// A no-op while starting, running or stopping: a start requested during
    /// `.stopping` is dropped, not queued, so a quit in progress cannot be undone.
    /// From `.failed` it clears the failure history, like `restartAfterFailure()`.
    func start() {
        switch state {
        case .stopped, .portInUse: break
        case .failed: failures = []
        default: return
        }
        begin()
    }

    func restartAfterFailure() {
        guard case .failed = state else { return }
        failures = []
        begin()
    }

    /// True when this app carries a usable bundled engine; the connection
    /// screen offers **Use Ciaobot on This Mac** only then.
    var hasEngine: Bool { engine != nil }

    /// Whether this Mac can run the bundled engine at all (arm64 only).
    nonisolated static var hostIsArm64: Bool { BundledEngine.hostIsArm64 }

    /// A stop while a stop is already draining joins it: it waits for the same
    /// exit and renews nothing, so a second caller (a quit during a mode
    /// switch) neither re-sends SIGTERM nor moves the first caller's SIGKILL
    /// deadline.
    func stop(grace: TimeInterval = 35, completion: @escaping () -> Void) {
        if state == .stopping {
            stopCompletions.append(completion)
            return
        }
        generation += 1
        startQueued = false
        failedChildExiting = false
        if let pid = child {
            state = .stopping
            stopCompletions.append(completion)
            terminateChild(pid, grace: grace)
        } else if let orphan = reaping {
            state = .stopping
            stopCompletions.append(completion)
            awaitOrphan(orphan, deadline: clock().addingTimeInterval(grace), token: generation) { [weak self] in
                self?.completeStop()
            }
        } else {
            state = .stopped
            completion()
        }
    }

    /// SIGTERM now, SIGKILL after `grace`, and if the exit is still not reported
    /// `killWait` later, stop waiting for it so no state can stay stuck.
    private func terminateChild(_ pid: Int32, grace: TimeInterval) {
        guard pid > 1 else { return }
        processes.terminate(pid)
        killToken += 1
        let token = killToken
        schedule(grace) { [weak self] in
            guard let self, self.killToken == token, self.child == pid else { return }
            self.processes.kill(pid)
            self.schedule(Self.killWait) { [weak self] in
                guard let self, self.killToken == token, self.child == pid else { return }
                self.child = nil
                self.generation += 1
                self.childReleased()
            }
        }
    }

    private func completeStop() {
        state = .stopped
        let done = stopCompletions
        stopCompletions = []
        done.forEach { $0() }
    }

    /// The child we were terminating is gone (exited, or never reported).
    private func childReleased() {
        if state == .stopping {
            completeStop()
        } else if failedChildExiting {
            failedChildExiting = false
            if startQueued {
                startQueued = false
                begin()
            }
        }
    }

    // MARK: Start

    private func begin() {
        guard engine != nil else {
            state = .failed(Self.unavailableMessage)
            return
        }
        state = .starting
        guard child == nil else {
            // The engine that failed to become ready is still dying and holds the lock.
            startQueued = true
            return
        }
        generation += 1
        reapOrphanThenSpawn(token: generation)
    }

    /// The live pid in `ready.json` is ours to signal only when its parent is
    /// launchd and its executable lies under the engine root, both compared as
    /// real paths (`proc_pidpath` reports `/private/var/...`).
    private func isOurOrphan(_ pid: Int32, _ info: EngineProcessInfo) -> Bool {
        guard pid > 1, info.parentPID == 1, let root = engine?.root, let path = info.path else { return false }
        return Self.canonical(path).hasPrefix(Self.canonical(root.path) + "/")
    }

    nonisolated static func canonical(_ path: String) -> String {
        if let resolved = realpath(path, nil) {
            defer { free(resolved) }
            return String(cString: resolved)
        }
        let url = URL(fileURLWithPath: path)
        if let resolved = realpath(url.deletingLastPathComponent().path, nil) {
            defer { free(resolved) }
            return String(cString: resolved) + "/" + url.lastPathComponent
        }
        return path
    }

    private func reapOrphanThenSpawn(token: Int) {
        guard let record = readRecord(), let pid = record.pid, let info = processes.inspect(pid) else {
            spawn(token: token)
            return
        }
        guard isOurOrphan(pid, info) else {
            state = .failed(Self.anotherRunningMessage)
            return
        }
        reaping = pid
        processes.terminate(pid)
        awaitOrphan(pid, deadline: clock().addingTimeInterval(Self.terminationGrace), token: token) { [weak self] in
            self?.spawn(token: token)
        }
    }

    /// Polls a SIGTERMed orphan. The full identity check runs on every poll and
    /// again before SIGKILL, so a pid recycled for an unrelated process is never signalled.
    private func awaitOrphan(_ pid: Int32, deadline: Date, token: Int, then done: @escaping () -> Void) {
        guard generation == token else { return }
        guard let info = processes.inspect(pid), isOurOrphan(pid, info) else {
            reaping = nil
            done()
            return
        }
        if clock() >= deadline {
            processes.kill(pid)
            reaping = nil
            done()
        } else {
            schedule(Self.orphanPollInterval) { [weak self] in
                self?.awaitOrphan(pid, deadline: deadline, token: token, then: done)
            }
        }
    }

    private func spawn(token: Int) {
        guard generation == token, let engine else { return }
        reaping = nil
        var parent = environment
        parent["HOME"] = home.home.path
        let launch = engine.launch(.run(["--app-engine"]), port: enginePort, parentEnvironment: parent, workingDirectory: home.home)
        do {
            try prepareLog()
            let pid = try processes.spawn(launch, log: home.logFile)
            child = pid
        } catch {
            state = .failed(Self.spawnFailureMessage)
            return
        }
        state = .starting
        let deadline = clock().addingTimeInterval(Self.readyTimeout)
        pollReady(pid: child!, deadline: deadline, token: token)
    }

    private func prepareLog() throws {
        let fm = FileManager.default
        try fm.createDirectory(at: home.logFile.deletingLastPathComponent(), withIntermediateDirectories: true,
                               attributes: [.posixPermissions: 0o700])
        let size = (try? fm.attributesOfItem(atPath: home.logFile.path)[.size] as? Int) ?? 0
        if size > Self.maximumLogBytes {
            let rotated = home.logFile.appendingPathExtension("1")
            try? fm.removeItem(at: rotated)
            try fm.moveItem(at: home.logFile, to: rotated)
        }
    }

    // MARK: Readiness

    private struct ReadyRecord {
        let pid: Int32?
        let port: Int?
        let startupID: String?
    }

    private func readRecord() -> ReadyRecord? {
        guard let data = try? Data(contentsOf: home.readyRecord),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        return ReadyRecord(
            pid: jsonInteger(object["pid"]).flatMap { Int32(exactly: $0) }.flatMap { $0 > 1 ? $0 : nil },
            port: jsonInteger(object["port"]).flatMap { (1...65535).contains($0) ? $0 : nil },
            startupID: (object["startup_id"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        )
    }

    private func pollReady(pid: Int32, deadline: Date, token: Int) {
        guard generation == token, child == pid else { return }
        if clock() >= deadline {
            // Keep the child: a stuck engine holds server.lock, so it is terminated
            // and killed like any other, and a later start waits for it to go.
            generation += 1
            failedChildExiting = true
            state = .failed(Self.startTimeoutMessage)
            terminateChild(pid, grace: Self.terminationGrace)
            return
        }
        if let record = readRecord(), record.pid == pid, let port = record.port, let id = record.startupID {
            lastPort = port
            verify(pid: pid, port: port, startupID: id, token: token) { [weak self] ok in
                guard let self else { return }
                if ok {
                    self.markRunning(pid: pid, port: port, startupID: id, token: token)
                } else {
                    self.schedule(Self.readyPollInterval) {
                        self.pollReady(pid: pid, deadline: deadline, token: token)
                    }
                }
            }
        } else {
            schedule(Self.readyPollInterval) { [weak self] in
                self?.pollReady(pid: pid, deadline: deadline, token: token)
            }
        }
    }

    private func verify(pid: Int32, port: Int, startupID: String, token: Int, completion: @escaping (Bool) -> Void) {
        guard let origin = Origin("http://127.0.0.1:\(port)") else { completion(false); return }
        identityChecker.identity(of: origin) { [weak self] found in
            guard let self, self.generation == token, self.child == pid else { return }
            completion(found == EngineIdentity(pid: pid, startupID: startupID))
        }
    }

    private func markRunning(pid: Int32, port: Int, startupID: String, token: Int) {
        guard generation == token, child == pid, let origin = Origin("http://127.0.0.1:\(port)") else { return }
        state = .running(origin: origin, pid: pid, startupID: startupID)
        schedule(Self.runningPollInterval) { [weak self] in self?.watch(pid: pid, token: token) }
    }

    /// While running: a new startup id (an execv restart) is probed again, and
    /// a new port publishes a new origin.
    private func watch(pid: Int32, token: Int) {
        guard generation == token, child == pid, case .running(let origin, _, let currentID) = state else { return }
        if let record = readRecord(), record.pid == pid, let port = record.port, let id = record.startupID,
           id != currentID || Origin("http://127.0.0.1:\(port)") != origin {
            lastPort = port
            verify(pid: pid, port: port, startupID: id, token: token) { [weak self] ok in
                guard let self else { return }
                if ok, let newOrigin = Origin("http://127.0.0.1:\(port)") {
                    self.state = .running(origin: newOrigin, pid: pid, startupID: id)
                }
                self.schedule(Self.runningPollInterval) { self.watch(pid: pid, token: token) }
            }
            return
        }
        schedule(Self.runningPollInterval) { [weak self] in self?.watch(pid: pid, token: token) }
    }

    // MARK: Exit

    private func handleExit(pid: Int32, status: Int32) {
        guard pid == child else { return }
        child = nil
        generation += 1
        if state == .stopping || failedChildExiting {
            childReleased()
            return
        }
        if status == Self.portInUseStatus {
            // The record's port, else the last port probed; 0 when neither was ever read.
            state = .portInUse(readRecord()?.port ?? lastPort)
            return
        }
        let now = clock()
        failures = failures.filter { now.timeIntervalSince($0) < Self.retryWindow } + [now]
        guard failures.count <= Self.retryDelays.count else {
            state = .failed(lastLogLine() ?? Self.spawnFailureMessage)
            return
        }
        state = .starting
        let token = generation
        schedule(Self.retryDelays[failures.count - 1]) { [weak self] in
            guard let self, self.generation == token else { return }
            self.reapOrphanThenSpawn(token: token)
        }
    }

    static let portInUseStatus: Int32 = 69

    private func lastLogLine() -> String? {
        guard let handle = try? FileHandle(forReadingFrom: home.logFile) else { return nil }
        defer { try? handle.close() }
        let size = (try? handle.seekToEnd()) ?? 0
        try? handle.seek(toOffset: size > 8192 ? size - 8192 : 0)
        guard let data = try? handle.readToEnd() else { return nil }
        let text = String(decoding: data, as: UTF8.self)
        guard let line = text.split(whereSeparator: \.isNewline)
            .map({ $0.trimmingCharacters(in: .whitespaces) }).last(where: { !$0.isEmpty }) else { return nil }
        return String(line.prefix(200))
    }
}

// MARK: Real implementations (never used by tests of the supervisor itself)

struct EngineSpawnError: Error, Equatable { let code: Int32 }

/// Runs the engine with `posix_spawn` in its own session (`POSIX_SPAWN_SETSID`),
/// so the engine is a process-group leader and SIGKILL can reach the whole group
/// (`killpg`). stdout and stderr are opened `O_APPEND` onto the log by the spawn
/// itself, so there is no handle to leak when the spawn fails.
///
/// Known limit: macOS has no parent-death signal. If the app itself is
/// SIGKILLed or crashes, the engine keeps running; the next launch finds it
/// through `ready.json` and reaps it (ppid 1 and a path under the engine root).
/// Its descendants are reaped only by the group kill of that engine, which the
/// orphan path does when the pid leads its own group.
final class FoundationEngineProcesses: EngineProcessRunning {
    var onExit: ((Int32, Int32) -> Void)?
    private var sources: [Int32: DispatchSourceProcess] = [:]

    func spawn(_ launch: BundledEngineLaunch, log: URL) throws -> Int32 {
        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        var attributes: posix_spawnattr_t?
        posix_spawnattr_init(&attributes)
        defer { posix_spawnattr_destroy(&attributes) }

        posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)
        posix_spawn_file_actions_addopen(&actions, 1, log.path, O_WRONLY | O_CREAT | O_APPEND, 0o600)
        posix_spawn_file_actions_adddup2(&actions, 1, 2)
        let chdir = posix_spawn_file_actions_addchdir_np(&actions, launch.workingDirectory.path)
        guard chdir == 0 else { throw EngineSpawnError(code: chdir) }
        posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETSID))

        let argv = ([launch.executable.path] + launch.arguments).map { strdup($0) } + [nil]
        let envp = launch.environment.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer { argv.forEach { free($0) }; envp.forEach { free($0) } }

        var pid: pid_t = 0
        let status = posix_spawn(&pid, launch.executable.path, &actions, &attributes, argv, envp)
        guard status == 0 else { throw EngineSpawnError(code: status) }

        let source = DispatchSource.makeProcessSource(identifier: pid, eventMask: .exit, queue: .main)
        source.setEventHandler { [weak self] in
            var raw: Int32 = 0
            waitpid(pid, &raw, 0)
            // Exit code, or 128 + signal for a signalled child.
            let code: Int32 = (raw & 0x7f) == 0 ? (raw >> 8) & 0xff : 128 + (raw & 0x7f)
            self?.sources[pid]?.cancel()
            self?.sources[pid] = nil
            self?.onExit?(pid, code)
        }
        sources[pid] = source
        source.resume()
        return pid
    }

    func terminate(_ pid: Int32) {
        guard pid > 1 else { return }
        Darwin.kill(pid, SIGTERM)
    }

    /// The whole group when the process leads its own (the engine we spawn does), else the pid.
    func kill(_ pid: Int32) {
        guard pid > 1 else { return }
        if getpgid(pid) == pid { Darwin.killpg(pid, SIGKILL) } else { Darwin.kill(pid, SIGKILL) }
    }

    func inspect(_ pid: Int32) -> EngineProcessInfo? {
        var info = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.size)
        guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size) == size else { return nil }
        var buffer = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
        let length = proc_pidpath(pid, &buffer, UInt32(buffer.count))
        let path = length > 0 ? String(cString: buffer) : nil
        return EngineProcessInfo(parentPID: Int32(bitPattern: info.pbi_ppid), path: path)
    }
}

/// Reads `startup_id` and `pid` from `/api/startup-status` with no credentials.
/// The body is capped while it streams, redirects are refused, and a wall-clock
/// timer plus `timeoutIntervalForResource` bound a server that drips bytes.
final class EngineIdentityProbe: EngineIdentityChecking {
    static let defaultTimeout: TimeInterval = 5

    private let configuration: URLSessionConfiguration
    private let timeout: TimeInterval

    init(configuration: URLSessionConfiguration = .ephemeral, timeout: TimeInterval = EngineIdentityProbe.defaultTimeout) {
        self.configuration = configuration
        self.timeout = timeout
    }

    /// The identity in a `/api/startup-status` answer, or nil for any other status or shape.
    static func parse(status: Int, data: Data) -> EngineIdentity? {
        guard (200...299).contains(status), data.count <= Probe.maximumResponseBytes,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let id = object["startup_id"] as? String, !id.isEmpty,
              let raw = jsonInteger(object["pid"]), raw > 1,
              let pid = Int32(exactly: raw) else { return nil }
        return EngineIdentity(pid: pid, startupID: id)
    }

    func identity(of origin: Origin, completion: @escaping (EngineIdentity?) -> Void) {
        let config = configuration.copy() as! URLSessionConfiguration
        config.httpShouldSetCookies = false
        config.httpCookieAcceptPolicy = .never
        config.httpCookieStorage = nil
        config.urlCredentialStorage = nil
        config.httpAdditionalHeaders = nil
        config.timeoutIntervalForRequest = timeout
        config.timeoutIntervalForResource = timeout
        let request = IdentityRequest(completion: completion)
        let session = URLSession(configuration: config, delegate: request, delegateQueue: nil)
        var urlRequest = URLRequest(url: origin.url.appendingPathComponent("api/startup-status"))
        urlRequest.httpShouldHandleCookies = false
        let task = session.dataTask(with: urlRequest)
        DispatchQueue.main.asyncAfter(deadline: .now() + timeout) { task.cancel() }
        task.resume()
    }
}

private final class IdentityRequest: NSObject, URLSessionDataDelegate {
    private var body = Data()
    private var completion: ((EngineIdentity?) -> Void)?

    init(completion: @escaping (EngineIdentity?) -> Void) { self.completion = completion }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        completionHandler(.cancelAuthenticationChallenge, nil)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard data.count <= Probe.maximumResponseBytes - body.count else {
            dataTask.cancel()
            return
        }
        body.append(data)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        let status = (task.response as? HTTPURLResponse)?.statusCode ?? 0
        let found = error == nil ? EngineIdentityProbe.parse(status: status, data: body) : nil
        let callback = completion
        completion = nil
        session.finishTasksAndInvalidate()
        DispatchQueue.main.async { callback?(found) }
    }
}
