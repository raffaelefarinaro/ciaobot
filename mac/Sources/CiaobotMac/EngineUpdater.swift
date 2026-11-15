import Foundation

/// The engine command and the version the connection screen may update to.
struct EngineUpdateTarget: Equatable {
    let executable: URL
    let version: String
}

/// Decides whether the connection screen may offer the engine's own updater.
/// Only a local engine qualifies: its process is on this Mac, it answers on a
/// loopback address, and the installer receipt names a usable command.
enum EngineUpdateOffer {
    static func target(
        status: LocalServerDiscovery.Status,
        candidate: Origin?,
        installation: LocalInstallation?,
        availableVersion: String?
    ) -> EngineUpdateTarget? {
        guard case .running(_, _, onThisMac: true) = status,
              candidate?.isLoopback == true,
              let executable = installation?.executable,
              let installed = installation?.installedVersion,
              let version = availableVersion,
              isNewer(version, than: installed) else { return nil }
        return EngineUpdateTarget(executable: executable, version: version)
    }

    /// Whether `a` is a newer version than `b`. Both must be exactly three
    /// dot-separated ASCII-digit components; anything else is never newer.
    static func isNewer(_ a: String, than b: String) -> Bool {
        guard let left = numericComponents(a), let right = numericComponents(b) else { return false }
        return right.lexicographicallyPrecedes(left)
    }

    private static let asciiDigits = Set("0123456789".unicodeScalars)

    private static func numericComponents(_ version: String) -> [Int]? {
        let parts = version.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 3 else { return nil }
        var numbers: [Int] = []
        for part in parts {
            guard !part.isEmpty, part.unicodeScalars.allSatisfy({ asciiDigits.contains($0) }),
                  let number = Int(part) else { return nil }
            numbers.append(number)
        }
        return numbers
    }
}

/// What one `ciao update …` command left behind. Stdout and stderr are the
/// bytes the command wrote, bounded by the live runner.
struct EngineCommandResult: Equatable {
    let status: Int32
    let stdout: Data
    let stderr: Data
    /// True when the runner ended the command at its time limit.
    var timedOut: Bool = false

    /// The engine's last non-empty stderr line without its `Error: ` prefix,
    /// sanitized for display. Display text only; no decision reads it.
    var errorLine: String? {
        let lines = String(decoding: stderr, as: UTF8.self).split(whereSeparator: \.isNewline)
        guard var line = lines.last(where: { !$0.trimmingCharacters(in: .whitespaces).isEmpty }).map(String.init) else { return nil }
        line = line.trimmingCharacters(in: .whitespaces)
        if line.hasPrefix("Error: ") { line.removeFirst("Error: ".count) }
        let clean = MenuTextSanitizer.sanitize(line, limit: EngineUpdateRecord.maximumErrorLength, fallback: "")
        return clean.isEmpty ? nil : clean
    }
}

/// Records whether the runner's timer ended a command. Written on the timer
/// queue and read in the termination handler, so it is guarded by a lock.
private final class TimeoutMark: @unchecked Sendable {
    private let lock = NSLock()
    private var didFire = false

    func fire() {
        lock.lock()
        didFire = true
        lock.unlock()
    }

    var fired: Bool {
        lock.lock()
        defer { lock.unlock() }
        return didFire
    }
}

/// The JSON object the engine prints for an update operation. Only the fields
/// the client shows or compares are kept.
struct EngineUpdateRecord: Equatable {
    static let maximumErrorLength = 300
    static let maximumIDLength = 200
    static let maximumPhaseLength = 40
    static let terminalPhases: Set<String> = ["applied", "rolled_back", "rollback_failed", "failed"]
    private static let phaseCharacters = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyz_")

    let id: String
    let phase: String
    let toVersion: String
    let error: String

    var isTerminal: Bool { Self.terminalPhases.contains(phase) }

    /// Nil unless the data is a JSON object with a well-formed `phase` and a
    /// non-empty `id`. Plain text such as `no update staged` is nil.
    static func parse(_ data: Data) -> EngineUpdateRecord? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let phase = object["phase"] as? String,
              (1...maximumPhaseLength).contains(phase.unicodeScalars.count),
              phase.unicodeScalars.allSatisfy({ phaseCharacters.contains($0) }),
              let id = object["id"] as? String, !id.isEmpty, id.count <= maximumIDLength else { return nil }
        let toVersion = StartupInfo.sanitizedVersion(object["to_version"]) ?? ""
        let error = MenuTextSanitizer.sanitize((object["error"] as? String) ?? "", limit: maximumErrorLength, fallback: "")
        return EngineUpdateRecord(id: id, phase: phase, toVersion: toVersion, error: error)
    }
}

enum EngineUpdateState: Equatable {
    case idle
    case staging(version: String)
    case applying(version: String, phase: String)
    case succeeded(version: String)
    case failed(message: String)

    var isActive: Bool {
        switch self {
        case .staging, .applying: return true
        case .idle, .succeeded, .failed: return false
        }
    }

    /// Only a finished update changes the installed engine, so only it
    /// re-reads the local server.
    var requiresRediscovery: Bool {
        if case .succeeded = self { return true }
        return false
    }
}

/// Runs the engine's own updater: stage, then apply when staged, then poll
/// `update status` until a terminal phase or the poll limit. Every command runs
/// through the receipt executable; the runner and the delay are injected so a
/// test never runs a real `ciao`.
@MainActor
final class EngineUpdater: ObservableObject {
    typealias Runner = (_ executable: URL, _ arguments: [String], _ timeout: TimeInterval,
                        _ completion: @escaping (EngineCommandResult?) -> Void) -> Void

    static let pollInterval: TimeInterval = 2
    static let applyTimeout: TimeInterval = 600
    static let maximumPolls = 300
    /// Time limits per command. The runner ends a command that runs past its
    /// limit; a stage or apply then fails, and a status poll counts as still in progress.
    static let commandTimeout: TimeInterval = 900
    static let statusTimeout: TimeInterval = 60
    nonisolated static let maximumOutputBytes = 65_536

    @Published private(set) var state: EngineUpdateState = .idle

    private let run: Runner
    private let after: (TimeInterval, @escaping () -> Void) -> Void
    private let isExecutable: (URL) -> Bool
    /// Guards against a late completion from an update that was superseded.
    private var generation = 0
    /// The operation the engine reported at stage or apply time. Polls for any
    /// other operation are ignored.
    private var operationID: String?
    private var target: EngineUpdateTarget?

    init(
        run: @escaping Runner = EngineUpdater.liveRun(),
        after: @escaping (TimeInterval, @escaping () -> Void) -> Void = { delay, work in
            DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
        },
        isExecutable: @escaping (URL) -> Bool = EngineUpdater.isExecutableFile
    ) {
        self.run = run
        self.after = after
        self.isExecutable = isExecutable
    }

    /// Clears a finished outcome (success or failure) so the screen stops
    /// showing it. A running update is left alone.
    func reset() {
        switch state {
        case .succeeded, .failed:
            state = .idle
        case .idle, .staging, .applying:
            break
        }
    }

    func start(_ target: EngineUpdateTarget) {
        guard !state.isActive else { return }
        generation &+= 1
        operationID = nil
        self.target = target
        guard isExecutable(target.executable) else {
            state = .failed(message: EngineUpdateCopy.executableMissing(path: target.executable.path))
            return
        }
        state = .staging(version: target.version)
        let request = generation
        run(target.executable, ["update", "stage", "--version=\(target.version)", "--json"], Self.commandTimeout) { [weak self] result in
            guard let self, self.generation == request else { return }
            self.stageFinished(result)
        }
    }

    private func stageFinished(_ result: EngineCommandResult?) {
        guard let target else { return }
        let version = target.version
        guard let result else {
            state = .failed(message: EngineUpdateCopy.launchFailed(path: target.executable.path))
            return
        }
        guard !result.timedOut else {
            state = .failed(message: EngineUpdateCopy.timedOut)
            return
        }
        guard result.status == 0 else {
            state = .failed(message: EngineUpdateCopy.stageFailed(
                version: version, detail: result.errorLine ?? EngineUpdateCopy.exitDetail(status: result.status)))
            return
        }
        guard let record = EngineUpdateRecord.parse(result.stdout) else {
            state = .failed(message: EngineUpdateCopy.unreadableRecord(version: version))
            return
        }
        guard record.phase == "staged" else {
            state = .failed(message: EngineUpdateCopy.stageFailed(
                version: version,
                detail: record.error.isEmpty ? EngineUpdateCopy.exitDetail(status: result.status) : record.error))
            return
        }
        operationID = record.id
        state = .applying(version: version, phase: "draining")
        let request = generation
        run(target.executable, ["update", "apply", "--json"], Self.commandTimeout) { [weak self] result in
            guard let self, self.generation == request else { return }
            self.applyFinished(result)
        }
    }

    private func applyFinished(_ result: EngineCommandResult?) {
        guard let target else { return }
        guard let result else {
            state = .failed(message: EngineUpdateCopy.launchFailed(path: target.executable.path))
            return
        }
        guard !result.timedOut else {
            state = .failed(message: EngineUpdateCopy.timedOut)
            return
        }
        guard result.status == 0 else {
            state = .failed(message: EngineUpdateCopy.applyFailed(
                version: target.version, detail: result.errorLine ?? EngineUpdateCopy.exitDetail(status: result.status)))
            return
        }
        if let record = EngineUpdateRecord.parse(result.stdout), operationID == nil || record.id == operationID {
            operationID = record.id
            if record.isTerminal {
                finish(record)
                return
            }
            state = .applying(version: target.version, phase: record.phase)
        }
        schedulePoll(remaining: Self.maximumPolls)
    }

    private func schedulePoll(remaining: Int) {
        let request = generation
        after(Self.pollInterval) { [weak self] in
            guard let self, self.generation == request, let target = self.target else { return }
            self.run(target.executable, ["update", "status", "--json"], Self.statusTimeout) { [weak self] result in
                guard let self, self.generation == request else { return }
                self.pollFinished(result, remaining: remaining)
            }
        }
    }

    /// A poll that fails, exits nonzero, cannot be parsed, or names another
    /// operation is "still in progress": the swap can briefly hide both the
    /// executable and the record. Only a terminal phase or the limit ends the wait.
    private func pollFinished(_ result: EngineCommandResult?, remaining: Int) {
        guard let target else { return }
        if let result, result.status == 0, !result.timedOut,
           let record = EngineUpdateRecord.parse(result.stdout),
           operationID == nil || record.id == operationID {
            if record.isTerminal {
                finish(record)
                return
            }
            state = .applying(version: target.version, phase: record.phase)
        }
        if remaining <= 1 {
            state = .failed(message: EngineUpdateCopy.timedOut)
        } else {
            schedulePoll(remaining: remaining - 1)
        }
    }

    private func finish(_ record: EngineUpdateRecord) {
        guard let target else { return }
        let version = target.version
        switch record.phase {
        case "applied":
            state = .succeeded(version: record.toVersion.isEmpty ? version : record.toVersion)
        case "rolled_back":
            state = .failed(message: EngineUpdateCopy.rolledBack(version: version, detail: record.error))
        case "rollback_failed":
            state = .failed(message: EngineUpdateCopy.rollbackFailed(version: version, detail: record.error))
        default:
            state = .failed(message: EngineUpdateCopy.updateFailed(version: version, detail: record.error))
        }
    }

    /// The installer's child environment: everything except Ciaobot's own
    /// `CIAO_` and `PWA_` variables. Reuses the installer's filter as is.
    nonisolated static func childEnvironment(_ environment: [String: String]) -> [String: String] {
        let entries = NativeInstallerProcess.childEnvironment(environment).compactMap { entry -> (String, String)? in
            guard let equals = entry.firstIndex(of: "=") else { return nil }
            return (String(entry[..<equals]), String(entry[entry.index(after: equals)...]))
        }
        return Dictionary(entries, uniquingKeysWith: { first, _ in first })
    }

    /// Exists, is not a directory, and is executable.
    nonisolated static func isExecutableFile(_ url: URL) -> Bool {
        var isDirectory: ObjCBool = false
        return FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory)
            && !isDirectory.boolValue
            && FileManager.default.isExecutableFile(atPath: url.path)
    }

    /// Runs one command with its output in temporary files, never pipes: the
    /// apply step hands off to a detached launchd job, and a descendant that
    /// inherited a pipe could hold a read open forever. The command is
    /// terminated once `timeout` seconds pass, and its completion then reports
    /// `timedOut`. The completion is delivered on the main queue; nil means the
    /// command could not be run.
    nonisolated static func liveRun(environment: [String: String] = ProcessInfo.processInfo.environment) -> Runner {
        { executable, arguments, timeout, completion in
            let fileManager = FileManager.default
            let base = fileManager.temporaryDirectory.appendingPathComponent("ciaobot-engine-update-\(UUID().uuidString)")
            let outURL = base.appendingPathExtension("out")
            let errURL = base.appendingPathExtension("err")
            let cleanup = {
                try? fileManager.removeItem(at: outURL)
                try? fileManager.removeItem(at: errURL)
            }
            let privateFile: [FileAttributeKey: Any] = [.posixPermissions: 0o600]
            guard fileManager.createFile(atPath: outURL.path, contents: nil, attributes: privateFile),
                  fileManager.createFile(atPath: errURL.path, contents: nil, attributes: privateFile) else {
                cleanup()
                DispatchQueue.main.async { completion(nil) }
                return
            }
            guard let outHandle = try? FileHandle(forWritingTo: outURL) else {
                cleanup()
                DispatchQueue.main.async { completion(nil) }
                return
            }
            guard let errHandle = try? FileHandle(forWritingTo: errURL) else {
                try? outHandle.close()
                cleanup()
                DispatchQueue.main.async { completion(nil) }
                return
            }

            let process = Process()
            process.executableURL = executable
            process.arguments = arguments
            process.environment = EngineUpdater.childEnvironment(environment)
            process.currentDirectoryURL = fileManager.homeDirectoryForCurrentUser
            process.standardInput = FileHandle.nullDevice
            process.standardOutput = outHandle
            process.standardError = errHandle
            let timeoutMark = TimeoutMark()
            process.terminationHandler = { finished in
                try? outHandle.close()
                try? errHandle.close()
                let stdout = ((try? Data(contentsOf: outURL)) ?? Data()).prefix(EngineUpdater.maximumOutputBytes)
                let stderr = ((try? Data(contentsOf: errURL)) ?? Data()).prefix(EngineUpdater.maximumOutputBytes)
                cleanup()
                let result = EngineCommandResult(
                    status: finished.terminationStatus,
                    stdout: Data(stdout),
                    stderr: Data(stderr),
                    timedOut: timeoutMark.fired
                )
                DispatchQueue.main.async { completion(result) }
            }
            do {
                try process.run()
            } catch {
                try? outHandle.close()
                try? errHandle.close()
                cleanup()
                DispatchQueue.main.async { completion(nil) }
                return
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
                guard process.isRunning else { return }
                timeoutMark.fire()
                process.terminate()
            }
        }
    }
}

/// Every string the engine update shows. Version and detail text come from the
/// engine's own answers; the client adds no workaround for a refused update.
enum EngineUpdateCopy {
    static let updateButton = "Update Ciaobot"
    static let retryButton = "Try Updating Again"
    static let accessibilityLabel = "Ciaobot update status"
    static let hint = "Downloads and verifies the new Ciaobot version, waits for active chats to finish, then restarts Ciaobot. If the new version does not start, the previous one is restored."

    static func availability(version: String, updatable: Bool) -> String {
        let base = "Ciaobot \(version) is available."
        return updatable ? base : base + " Update it from its Settings."
    }

    static func progress(for state: EngineUpdateState) -> String? {
        switch state {
        case .idle, .succeeded, .failed:
            return nil
        case let .staging(version):
            return "Downloading and verifying Ciaobot \(version)…"
        case let .applying(version, phase):
            switch phase {
            case "staged", "draining":
                return "Waiting for active chats to finish before updating to Ciaobot \(version)…"
            case "applying", "stopping", "swapping":
                return "Installing Ciaobot \(version)…"
            case "starting", "verifying_start":
                return "Restarting Ciaobot \(version)…"
            case "rolling_back":
                return "Ciaobot \(version) did not start. Restoring the previous version…"
            default:
                return "Updating Ciaobot to \(version)…"
            }
        }
    }

    static func outcome(for state: EngineUpdateState) -> String? {
        switch state {
        case let .succeeded(version):
            return "Ciaobot was updated to \(version)."
        case let .failed(message):
            return message
        case .idle, .staging, .applying:
            return nil
        }
    }

    static func exitDetail(status: Int32) -> String {
        "the updater exited with status \(status)."
    }

    static func stageFailed(version: String, detail: String) -> String {
        "Ciaobot \(version) could not be downloaded or prepared: \(detail)"
    }

    static func applyFailed(version: String, detail: String) -> String {
        "Ciaobot \(version) could not be installed: \(detail)"
    }

    static func rolledBack(version: String, detail: String) -> String {
        "The update to Ciaobot \(version) failed and the previous version was restored." + suffix(detail)
    }

    static func rollbackFailed(version: String, detail: String) -> String {
        "The update to Ciaobot \(version) failed and the previous version could not be restored."
            + suffix(detail) + " Run `ciao update status` in Terminal for details."
    }

    static func updateFailed(version: String, detail: String) -> String {
        "The update to Ciaobot \(version) failed." + suffix(detail)
    }

    static let timedOut = "Ciaobot did not finish updating in time. Run `ciao update status` in Terminal to see where it stopped."

    static func launchFailed(path: String) -> String {
        "The Ciaobot command at \(path) could not be run."
    }

    static func executableMissing(path: String) -> String {
        "The Ciaobot command at \(path) is missing or not executable. Check this Mac’s Ciaobot installation."
    }

    static func unreadableRecord(version: String) -> String {
        "Ciaobot \(version) could not be updated: the updater’s answer could not be read."
    }

    private static func suffix(_ detail: String) -> String {
        detail.isEmpty ? "" : " \(detail)"
    }
}
