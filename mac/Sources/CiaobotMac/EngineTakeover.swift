import AppKit
import Foundation

/// The 1.x takeover, driven by the app (#3129 S7). `ciao mac-takeover` does the
/// work and journals every step; this file only asks for it in order, shows the
/// one question the plan calls for, trashes what the engine offers after
/// re-checking each path, and rolls back when the bundled engine is not proven
/// healthy. It never touches launchd, a plist or a workspace itself.
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

/// Runs the takeover command as a child process: stdout is the JSON answer,
/// stderr is discarded, and a watchdog ends a command that runs past 60 s.
final class FoundationTakeoverRunner: TakeoverRunning {
    static let timeout: TimeInterval = 60

    private let commandLine: ([String]) -> TakeoverCommandLine

    init(commandLine: @escaping ([String]) -> TakeoverCommandLine) {
        self.commandLine = commandLine
    }

    func run(_ arguments: [String]) async throws -> (status: Int32, json: Data) {
        let line = commandLine(arguments)
        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                process.executableURL = line.executable
                process.arguments = line.arguments
                process.environment = line.environment
                process.currentDirectoryURL = line.workingDirectory
                let output = Pipe()
                process.standardOutput = output
                process.standardError = FileHandle.nullDevice
                do {
                    try process.run()
                } catch {
                    continuation.resume(throwing: error)
                    return
                }
                let watchdog = DispatchWorkItem {
                    if process.isRunning { process.terminate() }
                }
                DispatchQueue.global().asyncAfter(deadline: .now() + Self.timeout, execute: watchdog)
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                watchdog.cancel()
                continuation.resume(returning: (process.terminationStatus, data))
            }
        }
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

/// The fixed copy the connection screen shows for each takeover outcome.
enum TakeoverCopy {
    static let rolledBack = "Ciaobot could not finish moving into the app, so your existing Ciaobot was restarted unchanged."
    static let connected = "You are connected to your existing Ciaobot as before."
    static let rollbackFailed = "Ciaobot could not move into the app and could not restore your existing Ciaobot. Its state is kept for support."

    static func refused(code: String) -> String {
        switch code {
        case "downgrade":
            return "The Ciaobot on this Mac is newer than this app's engine, so it stays in place."
        case "custom_data_location":
            return "Your Ciaobot data is kept in a custom location that the app cannot move. The existing Ciaobot stays in place."
        case "foreign_listener":
            return "Another program answers on the Ciaobot port. The existing Ciaobot stays in place."
        case "port_mismatch":
            return "The existing Ciaobot uses a port the app cannot take over. It stays in place."
        case "legacy_desktop", "unknown_program":
            return "The existing Ciaobot was installed in a way the app cannot take over. It stays in place."
        default:
            return "Ciaobot could not be moved into the app. The existing Ciaobot stays in place."
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
            let text = code == "rollback_failed" ? rollbackFailed : refused(code: code)
            return origin == nil ? text : text + " " + connected
        case .rolledBack(_, let origin):
            return origin == nil ? rolledBack : rolledBack + " " + connected
        }
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
        /// The user said Later, or chats were running with no window to ask in.
        /// The old engine stays; the saved mode is kept so the next launch asks again.
        case deferred(origin: Origin?)
        /// The plan refused, or the apply refused before anything changed.
        case refused(code: String, oldOrigin: Origin?)
        /// The bundled engine was not proven healthy and the old one is back.
        case rolledBack(code: String, oldOrigin: Origin?)
    }

    struct Settled {
        let serial: Int
        let outcome: Outcome
    }

    /// Set when a takeover settles. A connection screen that appears later still reads it.
    @Published private(set) var settled: Settled?

    /// How long the bundled engine may take to report ready (matches the supervisor's own bound).
    static let readyTimeout: TimeInterval = 90
    static let pollInterval: TimeInterval = 0.25
    static let stopGrace: TimeInterval = 20

    private let runner: TakeoverRunning
    private let supervisor: TakeoverEngine
    private let trashing: Trashing
    private let home: EngineHome
    private let scope: [String]
    private let confirmMove: (Int) -> Bool
    private let windowVisible: () -> Bool
    private let readyTimeout: TimeInterval
    private let pollInterval: TimeInterval
    private var inFlight: Task<Outcome, Never>?
    private var serial = 0

    /// `labelOverride` (DEBUG only) names a test label; the plan and apply then
    /// read the isolated LaunchAgents folder under `home`. nil in every release build.
    init(
        runner: TakeoverRunning,
        supervisor: TakeoverEngine,
        trash: Trashing,
        home: EngineHome,
        labelOverride: String?,
        confirmMove: @escaping (Int) -> Bool = EngineTakeover.presentMoveNow,
        windowVisible: @escaping () -> Bool,
        readyTimeout: TimeInterval = EngineTakeover.readyTimeout,
        pollInterval: TimeInterval = EngineTakeover.pollInterval
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
    }

    /// Runs the takeover check once. A second call while one is running joins it.
    @discardableResult
    func runIfNeeded() async -> Outcome {
        if let inFlight { return await inFlight.value }
        let task = Task { await self.perform() }
        inFlight = task
        let outcome = await task.value
        inFlight = nil
        serial += 1
        settled = Settled(serial: serial, outcome: outcome)
        return outcome
    }

    // MARK: Flow

    private func perform() async -> Outcome {
        guard let plan = await readPlan() else {
            return .refused(code: "plan_failed", oldOrigin: nil)
        }
        switch plan.action {
        case "none":
            supervisor.start()
            return .notNeeded
        case "refuse":
            return .refused(code: plan.reason, oldOrigin: oldOrigin(plan))
        case "takeover":
            // With chats running, the user is asked once in a window they can
            // see. With no window (a login launch) the move waits, silently.
            if plan.activeChats > 0 {
                guard windowVisible(), confirmMove(plan.activeChats) else {
                    return .deferred(origin: oldOrigin(plan))
                }
            }
            return await move(plan)
        case "resume":
            return await move(plan)
        default:
            return .refused(code: "unknown_action", oldOrigin: nil)
        }
    }

    private func move(_ plan: TakeoverPlan) async -> Outcome {
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
            return .refused(code: "takeover_failed", oldOrigin: nil)
        }
        guard applied.ok, let id = applied.id, !id.isEmpty else {
            return await refusedApply(applied, plan: plan)
        }

        supervisor.start()
        guard await awaitRunning(), readyWorkspaceMatches(plan) else {
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
        _ = await call(["finish", "--json", "--id", id, "--done"])
        return .done(left: left)
    }

    private func refusedApply(_ answer: TakeoverAnswer, plan: TakeoverPlan) async -> Outcome {
        let code = answer.reason ?? "takeover_failed"
        guard let id = answer.id, !id.isEmpty else {
            return .refused(code: code, oldOrigin: nil)
        }
        // The engine already restored the old service and said so.
        if answer.state == "rolled_back" {
            return .rolledBack(code: code, oldOrigin: oldOrigin(plan))
        }
        return await rollBack(id: id, plan: plan, code: code)
    }

    /// Stops the bundled engine, then restores the old service. The old
    /// origin is reported only when the engine says the old one is back.
    private func rollBack(id: String, plan: TakeoverPlan, code: String) async -> Outcome {
        await stopEngine()
        guard let answer = await call(["rollback", "--json", "--id", id]), answer.ok else {
            return .refused(code: "rollback_failed", oldOrigin: nil)
        }
        return .rolledBack(code: code, oldOrigin: oldOrigin(plan))
    }

    // MARK: Engine

    private func stopEngine() async {
        await withCheckedContinuation { continuation in
            supervisor.stop(grace: Self.stopGrace) { continuation.resume() }
        }
    }

    private func awaitRunning() async -> Bool {
        let deadline = Date().addingTimeInterval(readyTimeout)
        while true {
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

    /// The running engine's ready record must name the plan's workspace and
    /// the pid the supervisor is watching.
    private func readyWorkspaceMatches(_ plan: TakeoverPlan) -> Bool {
        guard case .running(_, let pid, _) = supervisor.state,
              let data = try? Data(contentsOf: home.readyRecord),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let workspace = object["workspace"] as? String,
              let readyPID = object["pid"] as? Int else { return false }
        return readyPID == Int(pid) && Self.standard(workspace) == Self.standard(plan.workspace)
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
    /// root, nor inside or above either. The engine checked this too; the app
    /// checks again before it trashes anything.
    func trashable(_ path: String, plan: TakeoverPlan) -> URL? {
        guard path.hasPrefix("/") else { return nil }
        let candidate = Self.standard(path)
        guard candidate.hasPrefix(Self.standard(home.home.path) + "/") else { return nil }
        for guarded in [plan.workspace, plan.runtimeRoot] where !guarded.isEmpty {
            let other = Self.standard(guarded)
            if candidate == other || candidate.hasPrefix(other + "/") || other.hasPrefix(candidate + "/") {
                return nil
            }
        }
        guard FileManager.default.fileExists(atPath: candidate) else { return nil }
        return URL(fileURLWithPath: candidate)
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

    private func oldOrigin(_ plan: TakeoverPlan) -> Origin? {
        guard plan.oldRunning else { return nil }
        return Origin("http://127.0.0.1:\(plan.port)")
    }

    private static func standard(_ path: String) -> String {
        URL(fileURLWithPath: path).standardizedFileURL.path
    }

    /// The one question for chats that are working: Move Now or Later.
    @MainActor
    static func presentMoveNow(activeChats: Int) -> Bool {
        let count = activeChats == 1 ? "1 chat is" : "\(activeChats) chats are"
        return EngineLifecycle.presentConfirmation(
            title: "Move Ciaobot into the app now?",
            body: "\(count) working and will be interrupted. Your workspaces, chats, schedules and settings stay where they are.",
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
