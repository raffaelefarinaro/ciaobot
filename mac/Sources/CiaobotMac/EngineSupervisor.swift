import Foundation
import Darwin

/// Whether this app runs the bundled engine itself (`local`) or connects to a
/// server somebody else runs (`external`). #3129 S5a; nothing wires it yet.
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
}

final class UserDefaultsEngineModeStore: EngineModeStoring {
    static let defaultKey = "CiaobotEngineMode"
    private let defaults: UserDefaults
    private let key: String

    init(defaults: UserDefaults = .standard, key: String = UserDefaultsEngineModeStore.defaultKey) {
        self.defaults = defaults
        self.key = key
    }

    var mode: EngineMode? {
        get { defaults.string(forKey: key).flatMap(EngineMode.init(rawValue:)) }
        set {
            if let newValue { defaults.set(newValue.rawValue, forKey: key) } else { defaults.removeObject(forKey: key) }
        }
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

@MainActor
final class EngineSupervisor: ObservableObject {
    static let readyPollInterval: TimeInterval = 0.25
    static let readyTimeout: TimeInterval = 90
    static let runningPollInterval: TimeInterval = 5
    static let retryDelays: [TimeInterval] = [2, 5, 15]
    static let retryWindow: TimeInterval = 300
    static let orphanGrace: TimeInterval = 35
    static let orphanPollInterval: TimeInterval = 0.5
    static let maximumLogBytes = 20 * 1024 * 1024
    static let startTimeoutMessage = "The Ciaobot engine did not start. See ~/Library/Logs/Ciaobot/engine.log."
    static let unavailableMessage = "This Mac cannot run the Ciaobot engine bundled with the app."
    static let anotherRunningMessage = "Another Ciaobot is already running this workspace."
    static let spawnFailureMessage = "The Ciaobot engine could not be started. See ~/Library/Logs/Ciaobot/engine.log."

    @Published private(set) var state: EngineState = .stopped

    private let engine: BundledEngine?
    private let home: EngineHome
    private let processes: EngineProcessRunning
    private let identityChecker: EngineIdentityChecking
    private let clock: () -> Date
    private let schedule: (TimeInterval, @escaping () -> Void) -> Void
    private let environment: [String: String]

    /// Invalidates every timer and probe callback scheduled before a spawn or stop.
    private var generation = 0
    private var child: Int32?
    private var failures: [Date] = []
    private var lastPort = 0
    private var stopCompletions: [() -> Void] = []

    init(
        engine: BundledEngine?,
        home: EngineHome,
        processes: EngineProcessRunning,
        identity: EngineIdentityChecking,
        environment: [String: String] = ProcessInfo.processInfo.environment,
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
        self.clock = clock
        self.schedule = schedule
        processes.onExit = { [weak self] pid, status in
            MainActor.assumeIsolated { self?.handleExit(pid: pid, status: status) }
        }
    }

    // MARK: Public

    func start() {
        switch state {
        case .stopped, .failed, .portInUse: break
        default: return
        }
        begin()
    }

    func restartAfterFailure() {
        guard case .failed = state else { return }
        failures = []
        begin()
    }

    func stop(grace: TimeInterval = 35, completion: @escaping () -> Void) {
        generation += 1
        guard let pid = child else {
            state = .stopped
            completion()
            return
        }
        state = .stopping
        stopCompletions.append(completion)
        processes.terminate(pid)
        let token = generation
        schedule(grace) { [weak self] in
            guard let self, self.generation == token, self.child == pid else { return }
            self.processes.kill(pid)
        }
    }

    // MARK: Start

    private func begin() {
        guard engine != nil else {
            state = .failed(Self.unavailableMessage)
            return
        }
        generation += 1
        state = .starting
        reapOrphanThenSpawn(token: generation)
    }

    private func reapOrphanThenSpawn(token: Int) {
        guard let root = engine?.root, let record = readRecord(), let pid = record.pid,
              let info = processes.inspect(pid) else {
            spawn(token: token)
            return
        }
        let underRoot = info.path.map { $0.hasPrefix(root.path + "/") } ?? false
        guard info.parentPID == 1, underRoot else {
            state = .failed(Self.anotherRunningMessage)
            return
        }
        processes.terminate(pid)
        waitForOrphan(pid, deadline: clock().addingTimeInterval(Self.orphanGrace), token: token)
    }

    private func waitForOrphan(_ pid: Int32, deadline: Date, token: Int) {
        guard generation == token else { return }
        if processes.inspect(pid) == nil {
            spawn(token: token)
        } else if clock() >= deadline {
            processes.kill(pid)
            spawn(token: token)
        } else {
            schedule(Self.orphanPollInterval) { [weak self] in
                self?.waitForOrphan(pid, deadline: deadline, token: token)
            }
        }
    }

    private func spawn(token: Int) {
        guard generation == token, let engine else { return }
        var parent = environment
        parent["HOME"] = home.home.path
        let launch = engine.launch(.run(["--app-engine"]), parentEnvironment: parent, workingDirectory: home.home)
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
        try fm.createDirectory(at: home.logFile.deletingLastPathComponent(), withIntermediateDirectories: true)
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
            pid: (object["pid"] as? Int).flatMap { Int32(exactly: $0) },
            port: (object["port"] as? Int).flatMap { (1...65535).contains($0) ? $0 : nil },
            startupID: (object["startup_id"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        )
    }

    private func pollReady(pid: Int32, deadline: Date, token: Int) {
        guard generation == token, child == pid else { return }
        if clock() >= deadline {
            child = nil
            generation += 1
            processes.terminate(pid)
            state = .failed(Self.startTimeoutMessage)
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
        if state == .stopping {
            state = .stopped
            let done = stopCompletions
            stopCompletions = []
            done.forEach { $0() }
            return
        }
        if status == Self.portInUseStatus {
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

// MARK: Real implementations (never used by tests)

/// Runs the engine with Foundation.Process, stdout and stderr appended to the log.
final class FoundationEngineProcesses: EngineProcessRunning {
    var onExit: ((Int32, Int32) -> Void)?
    private var running: [Int32: Process] = [:]

    func spawn(_ launch: BundledEngineLaunch, log: URL) throws -> Int32 {
        if !FileManager.default.fileExists(atPath: log.path) {
            FileManager.default.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        let handle = try FileHandle(forWritingTo: log)
        try handle.seekToEnd()
        let process = Process()
        process.executableURL = launch.executable
        process.arguments = launch.arguments
        process.environment = launch.environment
        process.currentDirectoryURL = launch.workingDirectory
        process.standardOutput = handle
        process.standardError = handle
        process.standardInput = FileHandle.nullDevice
        process.terminationHandler = { [weak self] finished in
            try? handle.close()
            let pid = finished.processIdentifier
            let status = finished.terminationStatus
            DispatchQueue.main.async {
                self?.running[pid] = nil
                self?.onExit?(pid, status)
            }
        }
        try process.run()
        running[process.processIdentifier] = process
        return process.processIdentifier
    }

    func terminate(_ pid: Int32) { Darwin.kill(pid, SIGTERM) }
    func kill(_ pid: Int32) { Darwin.kill(pid, SIGKILL) }

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
final class EngineIdentityProbe: EngineIdentityChecking {
    func identity(of origin: Origin, completion: @escaping (EngineIdentity?) -> Void) {
        let config = URLSessionConfiguration.ephemeral
        config.httpShouldSetCookies = false
        config.httpCookieStorage = nil
        config.urlCredentialStorage = nil
        config.timeoutIntervalForRequest = 5
        let session = URLSession(configuration: config)
        var request = URLRequest(url: origin.url.appendingPathComponent("api/startup-status"))
        request.httpShouldHandleCookies = false
        session.dataTask(with: request) { data, response, error in
            session.finishTasksAndInvalidate()
            var found: EngineIdentity?
            if error == nil, let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode),
               let data, data.count <= Probe.maximumResponseBytes,
               let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let id = object["startup_id"] as? String, !id.isEmpty,
               let pid = (object["pid"] as? Int).flatMap({ Int32(exactly: $0) }) {
                found = EngineIdentity(pid: pid, startupID: id)
            }
            DispatchQueue.main.async { completion(found) }
        }.resume()
    }
}
