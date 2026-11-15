import Foundation
import Darwin

typealias NativeInstallFetch = @Sendable (URL) async throws -> Data

/// Produces the release to install. The default fetches and verifies the latest signed release.
typealias NativeInstallResolve = @Sendable (NativeInstallFetch) async throws -> NativeInstallPin

enum NativeInstallLaunchError: Error {
    case log
    case pipe
    case spawn(Int32)
    case script
}

/// Live child process. Only descriptors 0 to 2 and the event pipe (fd 3)
/// reach the child. Events are read from the pipe only; stdout and stderr go
/// to the log file.
final class NativeInstallerProcess: @unchecked Sendable {
    let pid: pid_t
    let events: AsyncStream<Data>

    /// The reader stops after this many bytes, so a runaway child cannot grow memory without limit.
    private static let maximumReadBytes = 65_536

    private let lock = NSLock()
    private var exited = false
    private var signalled = false
    private var stopRequested = false
    private var exitStatus: Int32?
    private var waitStarted = false
    private var waiters: [CheckedContinuation<Int32, Never>] = []

    private init(pid: pid_t, events: AsyncStream<Data>) {
        self.pid = pid
        self.events = events
    }

    /// The environment the installer receives: everything except Ciaobot's own
    /// `CIAO_` and `PWA_` variables, sorted by key.
    static func childEnvironment(_ environment: [String: String]) -> [String] {
        environment
            .filter { !$0.key.hasPrefix("CIAO_") && !$0.key.hasPrefix("PWA_") }
            .sorted { $0.key < $1.key }
            .map { "\($0.key)=\($0.value)" }
    }

    static func launch(arguments: [String], workingDirectory: URL, logURL: URL) throws -> NativeInstallerProcess {
        try FileManager.default.createDirectory(
            at: logURL.deletingLastPathComponent(),
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let logFD = open(logURL.path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC | O_NOFOLLOW, 0o600)
        guard logFD >= 0 else { throw NativeInstallLaunchError.log }
        guard fchmod(logFD, 0o600) == 0 else {
            close(logFD)
            throw NativeInstallLaunchError.log
        }

        var fds: [Int32] = [0, 0]
        guard pipe(&fds) == 0 else {
            close(logFD)
            throw NativeInstallLaunchError.pipe
        }
        // Move both ends above 9 so neither can collide with the fds the
        // child receives (0 to 3).
        let readFD = fcntl(fds[0], F_DUPFD_CLOEXEC, 10)
        let writeFD = fcntl(fds[1], F_DUPFD_CLOEXEC, 10)
        close(fds[0])
        close(fds[1])
        guard readFD >= 0, writeFD >= 0 else {
            if readFD >= 0 { close(readFD) }
            if writeFD >= 0 { close(writeFD) }
            close(logFD)
            throw NativeInstallLaunchError.pipe
        }

        // The stream exists before the child does, so a setup failure needs no cleanup beyond the fds.
        var streamContinuation: AsyncStream<Data>.Continuation?
        let events = AsyncStream<Data> { streamContinuation = $0 }
        guard let continuation = streamContinuation else {
            close(readFD)
            close(writeFD)
            close(logFD)
            throw NativeInstallLaunchError.pipe
        }

        // Any setup failure closes all three descriptors and throws.
        func check(_ rc: Int32) throws {
            guard rc == 0 else {
                close(readFD)
                close(writeFD)
                close(logFD)
                throw NativeInstallLaunchError.spawn(rc)
            }
        }

        var actions: posix_spawn_file_actions_t? = nil
        try check(posix_spawn_file_actions_init(&actions))
        defer { posix_spawn_file_actions_destroy(&actions) }
        try check(posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0))
        try check(posix_spawn_file_actions_adddup2(&actions, logFD, 1))
        try check(posix_spawn_file_actions_adddup2(&actions, logFD, 2))
        try check(posix_spawn_file_actions_adddup2(&actions, writeFD, NativeInstallPin.eventsFD))
        try check(posix_spawn_file_actions_addchdir_np(&actions, workingDirectory.path))

        var attrs: posix_spawnattr_t? = nil
        try check(posix_spawnattr_init(&attrs))
        defer { posix_spawnattr_destroy(&attrs) }
        var allSignals = sigset_t()
        sigfillset(&allSignals)
        try check(posix_spawnattr_setsigdefault(&attrs, &allSignals))
        var noSignals = sigset_t()
        sigemptyset(&noSignals)
        try check(posix_spawnattr_setsigmask(&attrs, &noSignals))
        try check(posix_spawnattr_setflags(
            &attrs,
            Int16(POSIX_SPAWN_CLOEXEC_DEFAULT | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK)
        ))

        var argv: [UnsafeMutablePointer<CChar>?] = arguments.map { strdup($0) }
        argv.append(nil)
        defer { argv.forEach { free($0) } }
        var envp: [UnsafeMutablePointer<CChar>?] = childEnvironment(ProcessInfo.processInfo.environment).map { strdup($0) }
        envp.append(nil)
        defer { envp.forEach { free($0) } }

        var pid: pid_t = 0
        let rc = posix_spawn(&pid, arguments[0], &actions, &attrs, argv, envp)

        // The parent keeps only the read end. Close the write and log ends on
        // every path so EOF arrives when the child exits.
        close(writeFD)
        close(logFD)
        guard rc == 0 else {
            close(readFD)
            continuation.finish()
            throw NativeInstallLaunchError.spawn(rc)
        }

        let process = NativeInstallerProcess(pid: pid, events: events)
        // A blocking read gets its own thread, not the cooperative pool. It polls
        // so it can notice a stop request even while no bytes arrive.
        let reader = Thread { [process] in
            var buffer = [UInt8](repeating: 0, count: 4096)
            var total = 0
            var pfd = pollfd(fd: readFD, events: Int16(POLLIN), revents: 0)
            while !process.readingShouldStop() {
                let ready = poll(&pfd, 1, 200)
                if ready < 0 {
                    if errno == EINTR { continue }
                    break
                }
                if ready == 0 { continue }
                let count = buffer.withUnsafeMutableBytes { read(readFD, $0.baseAddress, 4096) }
                if count > 0 {
                    total += count
                    continuation.yield(Data(buffer[0..<count]))
                    if total > maximumReadBytes { break }
                } else if count == 0 {
                    break
                } else if errno == EINTR {
                    continue
                } else {
                    break
                }
            }
            close(readFD)
            continuation.finish()
        }
        reader.start()

        return process
    }

    /// Sends SIGTERM once. A no-op after the child has exited.
    func terminate() {
        lock.lock()
        defer { lock.unlock() }
        guard !exited, !signalled else { return }
        signalled = true
        kill(pid, SIGTERM)
    }

    /// Ends the event stream at the next poll, whether or not the pipe is still open.
    func stopReading() {
        lock.lock()
        stopRequested = true
        lock.unlock()
    }

    private func readingShouldStop() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return stopRequested
    }

    /// Returns the raw wait status. Exit status never decides success.
    func waitForExit() async -> Int32 {
        await withCheckedContinuation { (continuation: CheckedContinuation<Int32, Never>) in
            lock.lock()
            if let exitStatus {
                lock.unlock()
                continuation.resume(returning: exitStatus)
                return
            }
            waiters.append(continuation)
            let startWaiter = !waitStarted
            waitStarted = true
            lock.unlock()
            guard startWaiter else { return }

            let waiter = Thread { [self] in
                var status: Int32 = 0
                var result: pid_t
                repeat {
                    result = waitpid(pid, &status, 0)
                } while result == -1 && errno == EINTR
                lock.lock()
                exited = true
                exitStatus = status
                let pending = waiters
                waiters.removeAll()
                lock.unlock()
                for pendingWaiter in pending {
                    pendingWaiter.resume(returning: status)
                }
            }
            waiter.start()
        }
    }
}

/// Which release fetch failed, as written to the install log.
enum NativeInstallDownloadPhase: String {
    case resolve, download
}

/// Remembers the URL of the last failed fetch so a resolve-phase failure can
/// name it in the log. `EngineRelease` rethrows fetch errors unchanged, so the
/// URL is not part of the error there.
private final class FetchFailureLog: @unchecked Sendable {
    private let lock = NSLock()
    private var value: URL?

    func record(url: URL) {
        lock.lock()
        value = url
        lock.unlock()
    }

    var lastURL: URL? {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

/// One formatter for install-log timestamps; formatters are expensive to build.
private enum InstallLog {
    static let timestampFormatter = ISO8601DateFormatter()
}

/// Resolves the latest signed engine release, downloads its installer, verifies it, and runs it in install or
/// repair mode with an event pipe on fd 3. The UI reads `state` only.
@MainActor
final class NativeInstaller: ObservableObject {
    @Published private(set) var state: NativeInstallState = .idle

    /// True from a successful launch until the child is reaped. A new run waits for this.
    @Published private(set) var childRunning = false

    /// The mode of the current or last run. Set by `start(mode:)` before the state changes.
    @Published private(set) var mode: NativeInstallMode = .install

    nonisolated static let defaultLogURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/Ciaobot/native-install.log")

    nonisolated static let liveFetch: NativeInstallFetch = { url in
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 60
        configuration.waitsForConnectivity = true
        configuration.urlCache = nil
        let session = URLSession(configuration: configuration)
        defer { session.finishTasksAndInvalidate() }
        let (data, response) = try await session.data(from: url)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw NativeInstallFetchError.unexpectedStatus((response as? HTTPURLResponse)?.statusCode ?? 0)
        }
        guard data.count <= 1_048_576 else {
            throw NativeInstallFetchError.responseTooLarge(bytes: data.count, limit: 1_048_576)
        }
        return data
    }

    private let resolve: NativeInstallResolve
    private let resolveRepair: NativeInstallResolve
    private let fetch: NativeInstallFetch
    private let fetchFailureLog: FetchFailureLog
    private let makeRunID: () -> UUID
    private let temporaryRoot: URL
    private let logURL: URL

    private var generation = 0
    private var task: Task<Void, Never>?
    /// The current run's task, readable so a test can await its completion.
    var runTask: Task<Void, Never>? { task }
    private var process: NativeInstallerProcess?
    private var cancelRequested = false

    /// `resolve` decides the release for install runs and `resolveRepair` for repair runs. Without them,
    /// `pin` answers for that mode; with neither, the latest signed release for that mode is resolved.
    init(pin: NativeInstallPin? = nil,
         resolve: NativeInstallResolve? = nil,
         resolveRepair: NativeInstallResolve? = nil,
         fetch: @escaping NativeInstallFetch = NativeInstaller.liveFetch,
         makeRunID: @escaping () -> UUID = UUID.init,
         temporaryRoot: URL = FileManager.default.temporaryDirectory,
         logURL: URL = NativeInstaller.defaultLogURL) {
        let resolver: NativeInstallResolve
        if let resolve {
            resolver = resolve
        } else if let pin {
            resolver = { _ in pin }
        } else {
            resolver = { fetch in try await EngineRelease.resolveLatest(fetch: fetch) }
        }
        let repairResolver: NativeInstallResolve
        if let resolveRepair {
            repairResolver = resolveRepair
        } else if let pin {
            repairResolver = { _ in pin }
        } else {
            repairResolver = { fetch in try await EngineRelease.resolveLatestForRepair(fetch: fetch) }
        }
        self.resolve = resolver
        self.resolveRepair = repairResolver
        let failureLog = FetchFailureLog()
        self.fetch = Self.withSingleRetry(fetch, failureLog: failureLog)
        self.fetchFailureLog = failureLog
        self.makeRunID = makeRunID
        self.temporaryRoot = temporaryRoot
        self.logURL = logURL
    }

    /// A run may start only when none is active and the previous child has been reaped.
    var canStart: Bool { !state.isActive && !childRunning }

    /// Clears a failed run's message when the failure no longer applies (for
    /// example discovery found the engine running). Active, succeeded and idle
    /// runs are untouched.
    func reset() {
        if case .failed = state {
            state = .idle
        }
    }

    /// No-op unless `canStart`.
    func start(mode: NativeInstallMode = .install) {
        guard canStart else { return }
        generation += 1
        let gen = generation
        let runID = makeRunID()
        cancelRequested = false
        self.mode = mode
        state = .downloading
        task = Task { await self.run(gen, runID: runID, mode: mode) }
    }

    /// Before launch, the run is abandoned and the script never runs. After
    /// launch, SIGTERM is sent and the wizard origin is dropped at once.
    func cancel() {
        switch state {
        case .downloading, .verifying:
            cancelRequested = true
            task?.cancel()
            generation += 1
            state = .failed(.cancelledBeforeLaunch)
        case .running, .wizard:
            cancelRequested = true
            state = .cancelling
            process?.terminate()
        case .idle, .cancelling, .succeeded, .failed:
            return
        }
    }

    private var isTerminalState: Bool {
        switch state {
        case .succeeded, .failed: return true
        case .idle, .downloading, .verifying, .running, .wizard, .cancelling: return false
        }
    }

    private var isCancelling: Bool {
        if case .cancelling = state { return true }
        return false
    }

    private func run(_ gen: Int, runID: UUID, mode: NativeInstallMode) async {
        let pin: NativeInstallPin
        do {
            pin = try await (mode == .repair ? resolveRepair : resolve)(fetch)
        } catch EngineReleaseError.repairUnavailable {
            finish(gen, .repairUnavailable)
            return
        } catch is EngineReleaseError {
            finish(gen, .releaseUnverified)
            return
        } catch {
            if Task.isCancelled || cancelRequested {
                finish(gen, .cancelledBeforeLaunch)
                return
            }
            let cause = NativeInstallDownloadCause.classify(error)
            Self.logDownloadFailure(phase: .resolve, url: fetchFailureLog.lastURL, cause: cause,
                                    runID: runID, error: error, to: logURL)
            finish(gen, .downloadFailed(cause))
            return
        }
        guard gen == generation else { return }

        let data: Data
        do {
            data = try await fetch(pin.url)
        } catch {
            if Task.isCancelled || cancelRequested {
                finish(gen, .cancelledBeforeLaunch)
                return
            }
            let cause = NativeInstallDownloadCause.classify(error)
            Self.logDownloadFailure(phase: .download, url: pin.url, cause: cause,
                                    runID: runID, error: error, to: logURL)
            finish(gen, .downloadFailed(cause))
            return
        }
        // A cancel that arrived during the download bumped the generation.
        guard gen == generation else { return }

        state = .verifying
        // Nothing is written to disk and nothing runs before the digest matches.
        guard NativeInstallPin.sha256Hex(data) == pin.sha256 else {
            finish(gen, .checksumMismatch)
            return
        }

        let directory = temporaryRoot.appendingPathComponent(
            "ciaobot-native-install-\(runID.uuidString.lowercased())",
            isDirectory: true
        )
        let scriptURL = directory.appendingPathComponent("install.sh")
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
                                                    attributes: [.posixPermissions: 0o700])
            try Self.writeScript(data, to: scriptURL)
        } catch {
            try? FileManager.default.removeItem(at: directory)
            finish(gen, .launchFailed)
            return
        }

        let launched: NativeInstallerProcess
        do {
            launched = try NativeInstallerProcess.launch(
                arguments: pin.arguments(scriptPath: scriptURL.path, runID: runID, mode: mode),
                workingDirectory: directory,
                logURL: logURL
            )
        } catch {
            try? FileManager.default.removeItem(at: directory)
            finish(gen, .launchFailed)
            return
        }
        process = launched
        childRunning = true
        state = .running(nil)
        // A descendant that inherited fd 3 can hold the pipe open after the
        // installer exits. Stop reading shortly after the child is reaped.
        Task {
            _ = await launched.waitForExit()
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            launched.stopReading()
        }

        var decoder = NativeInstallEventDecoder(runID: runID.uuidString.lowercased(), mode: mode)
        var violated = false
        for await chunk in launched.events {
            guard gen == generation else { break }
            // After a violation, ignore further chunks until the stream ends.
            if violated { continue }
            do {
                for event in try decoder.feed(chunk) {
                    apply(event, gen)
                }
            } catch {
                // The decoder throws only NativeInstallProtocolError.
                violated = true
                if !isTerminalState {
                    finish(gen, .protocolViolation(error as? NativeInstallProtocolError ?? .malformedJSON))
                }
                launched.stopReading()
                launched.terminate()
            }
        }

        do {
            try decoder.finish()
        } catch {
            if !isTerminalState {
                finish(gen, .protocolViolation(error as? NativeInstallProtocolError ?? .missingTerminalEvent))
            }
            // The stream can end while the child still runs (for example at the read cap).
            launched.terminate()
        }

        _ = await launched.waitForExit()
        try? FileManager.default.removeItem(at: directory)
        if process === launched {
            process = nil
            childRunning = false
        }
    }

    /// Runs `fetch` once more after a failure. A cancelled fetch is rethrown at
    /// once, and the failing URL is recorded only after the second failure.
    private nonisolated static func withSingleRetry(_ fetch: @escaping NativeInstallFetch,
                                                    failureLog: FetchFailureLog) -> NativeInstallFetch {
        { url in
            do {
                return try await fetch(url)
            } catch {
                if Task.isCancelled || (error as? URLError)?.code == .cancelled { throw error }
                do {
                    return try await fetch(url)
                } catch {
                    if Task.isCancelled || (error as? URLError)?.code == .cancelled { throw error }
                    failureLog.record(url: url)
                    throw error
                }
            }
        }
    }

    /// Appends one line naming why a release fetch failed. Best effort: any I/O
    /// failure is swallowed, so a broken log never changes the outcome.
    private nonisolated static func logDownloadFailure(phase: NativeInstallDownloadPhase,
                                                       url: URL?,
                                                       cause: NativeInstallDownloadCause,
                                                       runID: UUID,
                                                       error: Error,
                                                       to logURL: URL) {
        try? FileManager.default.createDirectory(
            at: logURL.deletingLastPathComponent(),
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let line = "\(InstallLog.timestampFormatter.string(from: Date()))"
            + " run=\(runID.uuidString.lowercased())"
            + " phase=\(phase.rawValue)"
            + " url=\(url?.absoluteString ?? "-")"
            + " cause=\(String(describing: cause))"
            + " error=\(String(reflecting: error))\n"
        let fd = open(logURL.path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { return }
        defer { close(fd) }
        _ = fchmod(fd, 0o600)
        let bytes = Data(line.utf8)
        bytes.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            var offset = 0
            while offset < raw.count {
                let written = write(fd, raw.baseAddress! + offset, raw.count - offset)
                if written > 0 {
                    offset += written
                } else if written < 0 && errno == EINTR {
                    continue
                } else {
                    return
                }
            }
        }
    }

    /// Creates the script with its final mode in one step: exclusive, no-follow, 0600.
    private static func writeScript(_ data: Data, to url: URL) throws {
        let fd = open(url.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw NativeInstallLaunchError.script }
        defer { close(fd) }
        try data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            var offset = 0
            while offset < raw.count {
                let written = write(fd, raw.baseAddress! + offset, raw.count - offset)
                if written > 0 {
                    offset += written
                } else if written < 0 && errno == EINTR {
                    continue
                } else {
                    throw NativeInstallLaunchError.script
                }
            }
        }
    }

    private func apply(_ event: NativeInstallEvent, _ gen: Int) {
        guard gen == generation, !isTerminalState else { return }
        switch event {
        case .serviceReady(let origin):
            // Still a success while cancelling: the service won the race.
            state = .succeeded(origin)
        case .failed(let code):
            state = .failed(.installer(code))
        case .wizardReady(let origin):
            guard !isCancelling else { return }
            state = .wizard(origin)
        case .phase(let phase):
            guard !isCancelling else { return }
            state = .running(phase)
        case .setupSaved:
            guard !isCancelling else { return }
            // Trust in the wizard origin ends once setup is saved.
            state = .running(.wizard)
        }
    }

    private func finish(_ gen: Int, _ failure: NativeInstallFailure) {
        guard gen == generation, !isTerminalState else { return }
        state = .failed(failure)
    }
}
