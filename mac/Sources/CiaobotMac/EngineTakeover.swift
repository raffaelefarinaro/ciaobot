import AppKit
import Darwin
import Foundation

/// The 1.x takeover, driven by the app (#3129 S7). `ciao mac-takeover` does the
/// work and journals every step; this file only asks for it in order, shows the
/// one question the plan calls for, trashes what the engine offers after
/// re-checking each path, and rolls back when the bundled engine is not proven
/// healthy. It never touches launchd, a plist or a workspace itself.
///
/// The one rule that governs every failure: a lost or unreadable answer never
/// means "nothing happened". After `apply` the state is unknown until the
/// journal says otherwise, so the app asks `plan` again and follows that.
///
/// This file does not name the bundled engine type: AppDelegate hands over a
/// command-line builder, so every flow here runs against fakes in tests.

/// The live label of the 1.x service. Only a DEBUG launch may name another one.
enum TakeoverLabel {
    static let live = "com.ciao.server"
}

/// One `mac-takeover` invocation: the bundled launcher, its arguments and environment.
struct TakeoverCommandLine: Equatable {
    let executable: URL
    let arguments: [String]
    let environment: [String: String]
    let workingDirectory: URL
}

/// The JSON of `ciao mac-takeover plan --json` (snake_case on the wire).
struct TakeoverPlan: Decodable, Equatable {
    struct Candidate: Decodable, Equatable {
        let path: String
        let proof: String
    }

    let action: String
    let reason: String
    let label: String
    let plist: String
    let plistSha256: String
    let workspace: String
    let runtimeRoot: String
    let port: Int
    let programKind: String
    let oldVersion: String
    let oldRunning: Bool
    /// Chats an old engine would interrupt. `-1` on a resume whose old engine
    /// answers but whose chats cannot be counted: the app asks without a number.
    let activeChats: Int
    let trash: [Candidate]
    let leftInPlace: [String]
    let droppedEnv: [String]
    let resumeId: String

    static func decode(_ data: Data) -> TakeoverPlan? {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try? decoder.decode(TakeoverPlan.self, from: data)
    }
}

/// The JSON of `apply`, `finish` and `rollback`: `ok`, and on a refusal the
/// reason, id and journal state. `finish` adds the Trash offer and what stays.
/// An error document from the engine has no `ok`, so it does not decode: that
/// answer is treated as lost, like a timeout.
struct TakeoverAnswer: Decodable, Equatable {
    let ok: Bool
    let reason: String?
    let id: String?
    let state: String?
    let trash: [TakeoverPlan.Candidate]?
    let leftInPlace: [String]?

    static func decode(_ data: Data) -> TakeoverAnswer? {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try? decoder.decode(TakeoverAnswer.self, from: data)
    }
}

/// Runs one `mac-takeover` command and returns its exit status and stdout.
protocol TakeoverRunning: AnyObject {
    func run(_ arguments: [String]) async throws -> (status: Int32, json: Data)
}

/// Moves one item to the Trash (`FileManager.trashItem` in production).
protocol Trashing: AnyObject {
    func trash(_ url: URL) throws -> URL?
}

/// The part of the engine supervisor the takeover drives.
@MainActor
protocol TakeoverEngine: AnyObject {
    var state: EngineState { get }
    func start()
    func stop(grace: TimeInterval, completion: @escaping () -> Void)
}

extension EngineSupervisor: TakeoverEngine {}

/// The per-command limits. Each is above the engine's own worst case for that
/// command: apply and rollback each may wait out a 45 s teardown and a 60 s
/// bootstrap wait plus launchctl calls, so they get 150 s; plan and finish are
/// read-mostly and get 30 s.
enum TakeoverTimeouts {
    static let plan: TimeInterval = 30
    static let apply: TimeInterval = 150
    static let finish: TimeInterval = 30
    static let rollback: TimeInterval = 150
    /// Between SIGTERM and SIGKILL when a command's process group is reaped.
    static let reapGrace: TimeInterval = 2

    static func forCommand(_ arguments: [String]) -> TimeInterval {
        switch arguments.first {
        case "apply": return apply
        case "rollback": return rollback
        case "finish": return finish
        default: return plan
        }
    }
}

/// Runs the takeover command as its own process group: stdout is the JSON
/// answer and stderr is discarded. A command that runs past its limit has its
/// whole group ended (SIGTERM, then SIGKILL), because the launcher is a shell
/// wrapper whose child engine would otherwise survive `terminate()`.
final class FoundationTakeoverRunner: TakeoverRunning {
    private let commandLine: ([String]) -> TakeoverCommandLine
    private let timeout: ([String]) -> TimeInterval

    init(
        commandLine: @escaping ([String]) -> TakeoverCommandLine,
        timeout: @escaping ([String]) -> TimeInterval = TakeoverTimeouts.forCommand
    ) {
        self.commandLine = commandLine
        self.timeout = timeout
    }

    func run(_ arguments: [String]) async throws -> (status: Int32, json: Data) {
        let line = commandLine(arguments)
        let limit = timeout(arguments)
        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    continuation.resume(returning: try Self.runGroup(line, timeout: limit))
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    /// Spawns the command as the leader of a new process group, reads stdout to
    /// its end, and ends the group if the read is still blocked after `timeout`.
    /// The leader is reaped only after the watchdogs are cancelled, so a group
    /// signal can never reach a reused pid.
    static func runGroup(_ line: TakeoverCommandLine, timeout: TimeInterval) throws -> (status: Int32, json: Data) {
        var fds: [Int32] = [-1, -1]
        guard pipe(&fds) == 0 else { throw posixFailure() }

        // Darwin types both spawn structs as opaque pointers, so they start as nil.
        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        posix_spawn_file_actions_adddup2(&actions, fds[1], 1)
        posix_spawn_file_actions_addopen(&actions, 2, "/dev/null", O_WRONLY, 0)
        posix_spawn_file_actions_addclose(&actions, fds[0])
        posix_spawn_file_actions_addclose(&actions, fds[1])
        posix_spawn_file_actions_addchdir_np(&actions, line.workingDirectory.path)

        var attributes: posix_spawnattr_t?
        posix_spawnattr_init(&attributes)
        defer { posix_spawnattr_destroy(&attributes) }
        // The child starts with every signal at its default and an empty mask, so
        // a disposition inherited from this process cannot stop the group's SIGTERM.
        var defaults = sigset_t()
        sigfillset(&defaults)
        var unmasked = sigset_t()
        sigemptyset(&unmasked)
        posix_spawnattr_setsigdefault(&attributes, &defaults)
        posix_spawnattr_setsigmask(&attributes, &unmasked)
        posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK))
        posix_spawnattr_setpgroup(&attributes, 0)

        let argv: [UnsafeMutablePointer<CChar>?] = ([line.executable.path] + line.arguments).map { strdup($0) } + [nil]
        let envp: [UnsafeMutablePointer<CChar>?] = line.environment.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer {
            argv.forEach { free($0) }
            envp.forEach { free($0) }
        }

        var pid: pid_t = 0
        let spawned = posix_spawn(&pid, line.executable.path, &actions, &attributes, argv, envp)
        close(fds[1])
        guard spawned == 0 else {
            close(fds[0])
            throw POSIXError(POSIXErrorCode(rawValue: spawned) ?? .EIO)
        }

        let reader = FileHandle(fileDescriptor: fds[0], closeOnDealloc: true)
        let forceKill = DispatchWorkItem { _ = killpg(pid, SIGKILL) }
        let terminate = DispatchWorkItem {
            _ = killpg(pid, SIGTERM)
            DispatchQueue.global().asyncAfter(deadline: .now() + TakeoverTimeouts.reapGrace, execute: forceKill)
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: terminate)
        let data = reader.readDataToEndOfFile()
        terminate.cancel()
        forceKill.cancel()

        var status: Int32 = 0
        while waitpid(pid, &status, 0) == -1 && errno == EINTR {}
        let exit: Int32 = (status & 0x7f) == 0 ? (status >> 8) & 0xff : 128 + (status & 0x7f)
        return (exit, data)
    }

    private static func posixFailure() -> Error {
        POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
    }
}

/// The real Trash: `FileManager.trashItem`, which never deletes outright.
final class FileManagerTrashing: Trashing {
    func trash(_ url: URL) throws -> URL? {
        var resulting: NSURL?
        try FileManager.default.trashItem(at: url, resultingItemURL: &resulting)
        return resulting as URL?
    }
}

/// The refusal codes the app acts on, in one table that mirrors the codes
/// `ciao/mac_takeover.py` returns. Change the engine's codes and this table together.
enum TakeoverRefusal {
    /// The plan's verdicts on the old install itself. A retry gets the same
    /// answer, so only these persist the external choice.
    static let deliberate: Set<String> = [
        "downgrade", "engine_too_old", "unknown_version", "custom_data_location",
        "foreign_listener", "port_mismatch", "legacy_desktop", "unknown_program",
        "no_workspace", "no_registry",
    ]
    /// The app cannot tell what the engine did: the plan or the apply answer was lost.
    static let stateUnknown: Set<String> = ["plan_failed", "resume_unreadable", "state_unknown"]
    /// Apply refusals that come before anything is stopped, so the old engine still runs.
    static let beforeStop: Set<String> = ["backup_unusable", "plist_changed", "sha_mismatch"]
}

/// The fixed copy the connection screen shows for each takeover outcome.
enum TakeoverCopy {
    static let rolledBack = "Ciaobot could not finish moving into the app, so your existing Ciaobot was restarted unchanged."
    static let connected = "You are connected to your existing Ciaobot as before."
    static let rollbackFailed = "Ciaobot could not move into the app and could not restore your existing Ciaobot. Its state is kept for support."
    static let stateUnknown = "Ciaobot could not confirm the state of your existing Ciaobot, so it was not moved into the app. Nothing was deleted. Use Ciaobot on This Mac to check again."
    /// A refusal that changed nothing and that a later try may get past.
    static let retry = "Ciaobot could not be moved into the app just now. Your existing Ciaobot was not changed. Use Ciaobot on This Mac to try again."

    static func refused(code: String) -> String {
        switch code {
        case "downgrade":
            return "The Ciaobot on this Mac is newer than this app's engine, so it stays in place."
        case "engine_too_old":
            return "The Ciaobot on this Mac is older than this app can take over. It stays in place."
        case "unknown_version":
            return "The Ciaobot on this Mac has a version this app cannot confirm. It stays in place."
        case "custom_data_location":
            return "Your Ciaobot data is kept in a custom location that the app cannot move. The existing Ciaobot stays in place."
        case "foreign_listener":
            return "Another program answers on the Ciaobot port. The existing Ciaobot stays in place."
        case "port_mismatch":
            return "The existing Ciaobot uses a port the app cannot take over. It stays in place."
        case "legacy_desktop", "unknown_program":
            return "The existing Ciaobot was installed in a way the app cannot take over. It stays in place."
        case "no_workspace", "no_registry":
            return "The existing Ciaobot's data could not be found by this app. It stays in place."
        default:
            return retry
        }
    }

    /// The notice for an outcome, or nil when the screen says nothing.
    static func notice(for outcome: EngineTakeover.Outcome) -> String? {
        switch outcome {
        case .notNeeded, .deferred:
            return nil
        case .done(let left):
            guard !left.isEmpty else { return nil }
            return "Ciaobot moved into the app. These 1.x items were left in place: " + left.joined(separator: ", ")
        case .refused(let code, let origin):
            let text: String
            if code == "rollback_failed" {
                text = rollbackFailed
            } else if TakeoverRefusal.stateUnknown.contains(code) {
                text = stateUnknown
            } else {
                text = refused(code: code)
            }
            return withConnection(text, origin)
        case .rolledBack(_, let origin):
            return withConnection(rolledBack, origin)
        }
    }

    private static func withConnection(_ text: String, _ origin: Origin?) -> String {
        origin == nil ? text : text + " " + connected
    }
}

/// Moves the old install into the app. Call `runIfNeeded()` before the local
/// engine starts. Every outcome says what the app should do next, and only
/// `notNeeded` and `done` leave the engine running.
@MainActor
final class EngineTakeover: ObservableObject {
    enum Outcome {
        /// No old install. The engine was started.
        case notNeeded
        /// The engine owns the workspace; `left` are 1.x items that stay in place.
        case done(left: [String])
        /// The user said Later, chats were running with no window to ask in, or
        /// the user left local mode first. The old engine stays; the saved mode
        /// is kept so the next launch asks again.
        case deferred(origin: Origin?)
        /// The plan or the apply refused, or the answer was lost and nothing was
        /// journalled. The old engine is as it was.
        case refused(code: String, oldOrigin: Origin?)
        /// The old install was restored after the bundled engine failed, or the
        /// user left local mode mid-move. The old service is back.
        case rolledBack(code: String, oldOrigin: Origin?)
    }

    struct Settled {
        let serial: Int
        /// The run that produced the outcome. A connection screen ignores a
        /// settled run older than the current one (a replay from before a switch).
        let generation: Int
        let outcome: Outcome
    }

    /// Set when a takeover settles. A connection screen that appears later still reads it.
    @Published private(set) var settled: Settled?

    /// How long the bundled engine may take to report ready (matches the supervisor's own bound).
    nonisolated static let readyTimeout: TimeInterval = 90
    nonisolated static let pollInterval: TimeInterval = 0.25
    nonisolated static let stopGrace: TimeInterval = 20

    /// Counts runs. The latest run's `settled.generation` equals it once that run settles.
    private(set) var generation = 0

    private let runner: TakeoverRunning
    private let supervisor: TakeoverEngine
    private let trashing: Trashing
    private let home: EngineHome
    private let scope: [String]
    private let confirmMove: @MainActor (Int) -> Bool
    private let windowVisible: @MainActor () -> Bool
    private let probe: (Origin) async -> Bool
    private let readyTimeout: TimeInterval
    private let pollInterval: TimeInterval
    private var inFlight: Task<Outcome, Never>?
    private var cancelledGeneration: Int?
    private var serial = 0

    /// `labelOverride` (DEBUG only) names a test label; the plan and apply then
    /// read the isolated LaunchAgents folder under `home`. nil in every release build.
    init(
        runner: TakeoverRunning,
        supervisor: TakeoverEngine,
        trash: Trashing,
        home: EngineHome,
        labelOverride: String?,
        confirmMove: @escaping @MainActor (Int) -> Bool = EngineTakeover.presentMoveNow,
        windowVisible: @escaping @MainActor () -> Bool,
        readyTimeout: TimeInterval = EngineTakeover.readyTimeout,
        pollInterval: TimeInterval = EngineTakeover.pollInterval,
        probe: @escaping (Origin) async -> Bool = EngineTakeover.probeOrigin
    ) {
        self.runner = runner
        self.supervisor = supervisor
        self.trashing = trash
        self.home = home
        if let labelOverride {
            let agents = home.home.appendingPathComponent("Library/LaunchAgents", isDirectory: true)
            scope = ["--label", labelOverride, "--launch-agents-dir", agents.path]
        } else {
            scope = []
        }
        self.confirmMove = confirmMove
        self.windowVisible = windowVisible
        self.readyTimeout = readyTimeout
        self.pollInterval = pollInterval
        self.probe = probe
    }

    /// Whether a run is in flight.
    var isRunning: Bool { inFlight != nil }

    /// Runs the takeover check once. A second call while one is running joins it,
    /// unless that run was cancelled: then the new run starts after it settles.
    @discardableResult
    func runIfNeeded() async -> Outcome {
        if let inFlight, !isCancelled(generation) { return await inFlight.value }
        let previous = inFlight
        generation += 1
        let run = generation
        let task = Task { () -> Outcome in
            if let previous { _ = await previous.value }
            return await self.perform(run)
        }
        inFlight = task
        let outcome = await task.value
        if generation == run { inFlight = nil }
        serial += 1
        settled = Settled(serial: serial, generation: run, outcome: outcome)
        return outcome
    }

    /// The user left local mode (another server, or the tray's switch) while a run
    /// is in flight. Nothing new starts: a run that has not applied yet defers, and
    /// one that has applied is rolled back, so the old install is never left half-moved.
    func cancel() {
        guard inFlight != nil else { return }
        cancelledGeneration = generation
    }

    private func isCancelled(_ run: Int) -> Bool { cancelledGeneration == run }

    // MARK: Flow

    private func perform(_ run: Int) async -> Outcome {
        guard let plan = await readPlan() else {
            // Plan is read-only, but an earlier unfinished journal may be what it
            // could not read, so the state is not known.
            return .refused(code: "plan_failed", oldOrigin: nil)
        }
        switch plan.action {
        case "none":
            guard !isCancelled(run) else { return .deferred(origin: nil) }
            supervisor.start()
            return .notNeeded
        case "refuse":
            return .refused(code: plan.reason, oldOrigin: oldOrigin(plan))
        case "takeover", "resume":
            // Chats running, or an old engine whose chats cannot be counted (-1):
            // asked once in a window the user can see. With no window (a login
            // launch) the move waits, silently. A resume gets the same question,
            // because its old engine may still be serving chats.
            if plan.activeChats != 0 {
                guard windowVisible(), confirmMove(plan.activeChats) else {
                    return .deferred(origin: oldOrigin(plan))
                }
            }
            guard !isCancelled(run) else { return .deferred(origin: oldOrigin(plan)) }
            return await move(plan, run: run)
        default:
            return .refused(code: "unknown_action", oldOrigin: nil)
        }
    }

    private func move(_ plan: TakeoverPlan, run: Int) async -> Outcome {
        let expect: String
        if plan.action == "resume" {
            // The plan does not carry the hash of a resumed takeover; the
            // journal the engine wrote for it does.
            guard let sha = journalSha(id: plan.resumeId) else {
                return .refused(code: "resume_unreadable", oldOrigin: nil)
            }
            expect = sha
        } else {
            expect = plan.plistSha256
        }
        guard let applied = await call(["apply", "--json", "--expect-sha256", expect] + scope) else {
            // Lost, timed out or unreadable: the engine may have moved already.
            return await stateUnknown(plan)
        }
        guard applied.ok else { return await refusedApply(applied, plan: plan) }
        guard let id = applied.id, !id.isEmpty else { return await stateUnknown(plan) }

        // A cancel after apply still ends in a safe state: the bundled engine is
        // not started, and the old service comes back.
        guard !isCancelled(run) else { return await rollBack(id: id, plan: plan, code: "cancelled") }
        supervisor.start()
        guard await awaitRunning(run), readyWorkspaceMatches(plan) else {
            return await rollBack(id: id, plan: plan, code: "engine_not_ready")
        }
        guard let found = await call(["finish", "--json", "--id", id]), found.ok else {
            return await rollBack(id: id, plan: plan, code: "not_healthy")
        }

        var left = found.leftInPlace ?? []
        let entries = trashOffered(found.trash ?? [], plan: plan, left: &left)
        writeTrashRecord(id: id, entries: entries)
        // The journal closes only after the trashing, so a crash in between
        // leaves it open and the next launch resumes instead of forgetting.
        // If this record is not written, the journal stays healthy and the next
        // launch's resume finishes it with nothing left to trash.
        if await call(["finish", "--json", "--id", id, "--done"])?.ok != true {
            NSLog("Ciaobot takeover: the finish record was not written; the next launch closes the takeover.")
        }
        return .done(left: left)
    }

    /// An apply answer that is `ok: false`. What it means depends on how far the
    /// journal got, so it is read, not assumed.
    private func refusedApply(_ answer: TakeoverAnswer, plan: TakeoverPlan) async -> Outcome {
        let code = answer.reason ?? "takeover_failed"
        guard let id = answer.id, !id.isEmpty else {
            // Nothing was journalled, so the old install is as the plan saw it.
            return .refused(code: code, oldOrigin: oldOrigin(plan))
        }
        switch answer.state {
        case "rolled_back":
            // The engine already restored the old service and said so.
            return .rolledBack(code: code, oldOrigin: restoredOrigin(plan))
        case "finished":
            // Settled by someone else; nothing here is left to undo.
            return .refused(code: code, oldOrigin: nil)
        case "started" where TakeoverRefusal.beforeStop.contains(code):
            // Refused before the old engine was touched: it still runs.
            return .refused(code: code, oldOrigin: oldOrigin(plan))
        default:
            return await rollBack(id: id, plan: plan, code: code)
        }
    }

    /// The apply answer was lost. The journal decides what happened: the plan is
    /// asked again, and a resumable journal is rolled back. The bundled engine is
    /// never started on this path.
    private func stateUnknown(_ plan: TakeoverPlan) async -> Outcome {
        await stopEngine()
        guard let again = await readPlan() else {
            return .refused(code: "state_unknown", oldOrigin: nil)
        }
        switch again.action {
        case "resume":
            return await rollBack(id: again.resumeId, plan: again, code: "takeover_failed")
        case "refuse":
            return .refused(code: again.reason, oldOrigin: oldOrigin(again))
        case "takeover", "none":
            // Nothing is journalled: a plain refusal is the true answer.
            return .refused(code: "takeover_failed", oldOrigin: oldOrigin(again))
        default:
            return .refused(code: "state_unknown", oldOrigin: nil)
        }
    }

    /// Stops the bundled engine, then restores the old service. The old origin is
    /// reported when the engine says the old one is back; when the rollback itself
    /// fails, the origin is reported only if it answers now.
    private func rollBack(id: String, plan: TakeoverPlan, code: String) async -> Outcome {
        await stopEngine()
        if let answer = await call(["rollback", "--json", "--id", id]), answer.ok {
            return .rolledBack(code: code, oldOrigin: restoredOrigin(plan))
        }
        return .refused(code: "rollback_failed", oldOrigin: await answeringOrigin(restoredOrigin(plan)))
    }

    // MARK: Engine

    private func stopEngine() async {
        await withCheckedContinuation { continuation in
            supervisor.stop(grace: Self.stopGrace) { continuation.resume() }
        }
    }

    private func awaitRunning(_ run: Int) async -> Bool {
        let deadline = Date().addingTimeInterval(readyTimeout)
        while true {
            if isCancelled(run) { return false }
            switch supervisor.state {
            case .running:
                return true
            case .failed, .portInUse:
                return false
            case .stopped, .starting, .stopping:
                break
            }
            if Date() >= deadline { return false }
            try? await Task.sleep(nanoseconds: UInt64(pollInterval * 1_000_000_000))
        }
    }

    /// The running engine's ready record must name the plan's workspace and the
    /// pid the supervisor is watching. The engine writes the resolved workspace
    /// path, the plan carries the one the plist names, so both are resolved.
    private func readyWorkspaceMatches(_ plan: TakeoverPlan) -> Bool {
        guard case .running(_, let pid, _) = supervisor.state,
              let data = try? Data(contentsOf: home.readyRecord),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let workspace = object["workspace"] as? String,
              let readyPID = object["pid"] as? Int else { return false }
        return readyPID == Int(pid) && Self.resolved(workspace) == Self.resolved(plan.workspace)
    }

    // MARK: Trash

    struct TrashEntry: Equatable {
        let path: String
        let trashedTo: String?
        let error: String?
    }

    /// Trashes what the engine offered, after re-checking each path here. A
    /// path that fails the check, or cannot be trashed, stays in place and is
    /// reported through `left`; nothing is deleted outright.
    private func trashOffered(
        _ offered: [TakeoverPlan.Candidate], plan: TakeoverPlan, left: inout [String]
    ) -> [TrashEntry] {
        var entries: [TrashEntry] = []
        var seen = Set<String>()
        for candidate in offered where seen.insert(candidate.path).inserted {
            guard let url = trashable(candidate.path, plan: plan) else {
                left.append(candidate.path)
                continue
            }
            do {
                let resulting = try trashing.trash(url)
                entries.append(TrashEntry(path: url.path, trashedTo: resulting?.path, error: nil))
            } catch {
                entries.append(TrashEntry(path: url.path, trashedTo: nil, error: error.localizedDescription))
                left.append(url.path)
            }
        }
        return entries
    }

    /// Absolute, inside the home folder, and neither the workspace or runtime
    /// root, nor inside or above either, with symlinks resolved on both sides.
    /// The candidate itself may be a symlink (the shim is one), so it is checked
    /// with lstat: a dangling link is still there to be trashed. The engine
    /// checked this too; the app checks again before it trashes anything.
    func trashable(_ path: String, plan: TakeoverPlan) -> URL? {
        guard path.hasPrefix("/") else { return nil }
        let lexical = Self.standard(path)
        let resolvedPath = Self.resolved(path)
        guard lexical.hasPrefix(Self.standard(home.home.path) + "/"),
              resolvedPath.hasPrefix(Self.resolved(home.home.path) + "/") else { return nil }
        for guarded in [plan.workspace, plan.runtimeRoot] where !guarded.isEmpty {
            for other in [Self.standard(guarded), Self.resolved(guarded)]
            where Self.overlaps(lexical, other) || Self.overlaps(resolvedPath, other) {
                return nil
            }
        }
        var status = stat()
        guard lstat(lexical, &status) == 0 else { return nil }
        return URL(fileURLWithPath: lexical)
    }

    private func writeTrashRecord(id: String, entries: [TrashEntry]) {
        let records: [[String: Any]] = entries.map { entry in
            [
                "path": entry.path,
                "trashed_to": entry.trashedTo as Any? ?? NSNull(),
                "error": entry.error as Any? ?? NSNull(),
            ]
        }
        guard let data = try? JSONSerialization.data(withJSONObject: records, options: [.prettyPrinted, .sortedKeys]) else {
            return
        }
        let url = home.takeoverDir.appendingPathComponent(id, isDirectory: true)
            .appendingPathComponent("trash.json")
        try? data.write(to: url, options: .atomic)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    // MARK: Helpers

    private func readPlan() async -> TakeoverPlan? {
        guard let result = try? await runner.run(["plan", "--json"] + scope) else { return nil }
        return TakeoverPlan.decode(result.json)
    }

    private func call(_ arguments: [String]) async -> TakeoverAnswer? {
        guard let result = try? await runner.run(arguments) else { return nil }
        return TakeoverAnswer.decode(result.json)
    }

    private func journalSha(id: String) -> String? {
        guard !id.isEmpty, !id.contains("/"), !id.hasPrefix(".") else { return nil }
        let journal = home.takeoverDir.appendingPathComponent(id, isDirectory: true)
            .appendingPathComponent("journal.json")
        guard let data = try? Data(contentsOf: journal),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let sha = object["plist_sha256"] as? String, !sha.isEmpty else { return nil }
        return sha
    }

    /// Where the old engine serves while the old install is still in place.
    private func oldOrigin(_ plan: TakeoverPlan) -> Origin? {
        guard plan.oldRunning else { return nil }
        return Origin("http://127.0.0.1:\(plan.port)")
    }

    /// Where the old engine serves once a rollback has proven it back.
    private func restoredOrigin(_ plan: TakeoverPlan) -> Origin? {
        Origin("http://127.0.0.1:\(plan.port)")
    }

    private func answeringOrigin(_ candidate: Origin?) async -> Origin? {
        guard let candidate, await probe(candidate) else { return nil }
        return candidate
    }

    /// One credential-free status request to the origin, through the app's own probe.
    static func probeOrigin(_ origin: Origin) async -> Bool {
        await withCheckedContinuation { continuation in
            Probe().check(origin) { continuation.resume(returning: $0) }
        }
    }

    /// Whether a refusal code persists the external choice.
    static func isDeliberateRefusal(_ code: String) -> Bool {
        TakeoverRefusal.deliberate.contains(code)
    }

    /// Standardized, symlinks left as they are: what the candidate is called on disk.
    private static func standard(_ path: String) -> String {
        URL(fileURLWithPath: path).standardizedFileURL.path
    }

    /// Standardized with every symlink resolved: where the path really is.
    static func resolved(_ path: String) -> String {
        URL(fileURLWithPath: path).resolvingSymlinksInPath().standardizedFileURL.path
    }

    /// True when the two paths are the same, or one lies inside the other.
    private static func overlaps(_ a: String, _ b: String) -> Bool {
        a == b || a.hasPrefix(b + "/") || b.hasPrefix(a + "/")
    }

    /// The one question for chats that are working: Move Now or Later.
    @MainActor
    static func presentMoveNow(activeChats: Int) -> Bool {
        let working: String
        if activeChats < 0 {
            working = "Chats may be working in the old Ciaobot and would be interrupted."
        } else {
            let count = activeChats == 1 ? "1 chat is" : "\(activeChats) chats are"
            working = "\(count) working and will be interrupted."
        }
        return EngineLifecycle.presentConfirmation(
            title: "Move Ciaobot into the app now?",
            body: working + " Your workspaces, chats, schedules and settings stay where they are.",
            confirm: "Move Now",
            cancel: "Later"
        )
    }

    #if DEBUG
    /// `CIAOBOT_MAC_TEST_TAKEOVER_LABEL`: a test label for the takeover command.
    /// Compiled out of release builds. Only a plain label, and never the live one.
    static func testLabel(environment: [String: String]) -> String? {
        guard let raw = environment["CIAOBOT_MAC_TEST_TAKEOVER_LABEL"], !raw.isEmpty,
              raw.count <= 128, raw != TakeoverLabel.live,
              raw.allSatisfy({ ($0.isASCII && ($0.isLetter || $0.isNumber)) || $0 == "." || $0 == "-" || $0 == "_" })
        else { return nil }
        return raw
    }
    #endif
}
