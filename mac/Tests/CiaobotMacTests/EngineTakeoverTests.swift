import Combine
import Darwin
import XCTest
@testable import CiaobotMac

/// Ordered record of what the takeover asked for, shared by every fake so a
/// test can assert the sequence across the runner, engine and Trash.
private final class EventLog {
    var events: [String] = []
    func add(_ event: String) { events.append(event) }
    func index(of event: String) -> Int? { events.firstIndex(of: event) }
    func count(prefix: String) -> Int { events.filter { $0.hasPrefix(prefix) }.count }
}

/// Answers each `mac-takeover` command with canned JSON, keyed by the command
/// word (`plan`, `apply`, `finish`, `finish --done`, `rollback`). `sequences`
/// answers a command's calls in turn (a plan before and after a lost apply),
/// `failing` makes a command's answer lost (the runner throws, as a timeout or
/// a launch failure does), and `onRun` runs before each answer.
private final class FakeRunner: TakeoverRunning {
    var answers: [String: String] = [:]
    var sequences: [String: [String]] = [:]
    var failing: Set<String> = []
    /// The first N calls of a command are lost (the runner throws) before the rest answer.
    var lostFirst: [String: Int] = [:]
    /// Models the engine: a `finish` journals the Trash offer before its answer
    /// leaves, and from then on a `rollback` is refused with `trash_offered`.
    var engineJournalsTrash = false
    private(set) var journalTrashOffered = false
    var onRun: (@Sendable (String) async -> Void)?
    var calls: [[String]] = []
    let log: EventLog

    init(log: EventLog) { self.log = log }

    static func key(for arguments: [String]) -> String {
        let command = arguments.first ?? ""
        return command == "finish" && arguments.contains("--done") ? "finish --done" : command
    }

    func run(_ arguments: [String]) async throws -> (status: Int32, json: Data) {
        calls.append(arguments)
        let key = Self.key(for: arguments)
        log.add("cmd:" + key)
        if let onRun { await onRun(key) }
        if key == "finish", engineJournalsTrash { journalTrashOffered = true }
        if let lost = lostFirst[key], lost > 0 {
            lostFirst[key] = lost - 1
            throw CocoaError(.executableLoad)
        }
        if failing.contains(key) { throw CocoaError(.executableLoad) }
        var answer = answers[key] ?? "{}"
        if var queue = sequences[key], !queue.isEmpty {
            answer = queue.removeFirst()
            sequences[key] = queue
        }
        if key == "rollback", journalTrashOffered {
            answer = #"{"ok": false, "reason": "trash_offered", "id": "T1", "state": "healthy"}"#
        }
        return (0, Data(answer.utf8))
    }
}

/// Records each settled publication with the generation the takeover was on at that moment.
private final class SettledRecorder {
    private(set) var values: [(settled: Int, current: Int)] = []
    func add(settled: Int, current: Int) { values.append((settled, current)) }
}

/// Records every signal a watchdog sends, from whichever queue sends it.
private final class SignalRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var sent: [Int32] = []
    func add(_ signal: Int32) {
        lock.lock()
        sent.append(signal)
        lock.unlock()
    }
    var signals: [Int32] {
        lock.lock()
        defer { lock.unlock() }
        return sent
    }
}

@MainActor
private final class FakeEngine: TakeoverEngine {
    var state: EngineState = .stopped
    /// False leaves the engine `.starting` forever (a readiness timeout).
    var startsRunning = true
    let log: EventLog
    static let origin = Origin("http://127.0.0.1:8443")!

    init(log: EventLog) { self.log = log }

    func start() {
        log.add("start")
        state = startsRunning
            ? .running(origin: Self.origin, pid: 4242, startupID: "abc")
            : .starting
    }

    func stop(grace: TimeInterval, completion: @escaping () -> Void) {
        log.add("stop")
        state = .stopped
        completion()
    }
}

private final class FakeTrash: Trashing {
    var trashed: [URL] = []
    var failing: Set<String> = []
    let log: EventLog

    init(log: EventLog) { self.log = log }

    func trash(_ url: URL) throws -> URL? {
        log.add("trash:" + url.lastPathComponent)
        if failing.contains(url.lastPathComponent) {
            throw CocoaError(.fileWriteNoPermission)
        }
        trashed.append(url)
        return URL(fileURLWithPath: "/Trash/" + url.lastPathComponent)
    }
}

@MainActor
final class EngineTakeoverTests: XCTestCase {
    private var tmp: URL!
    private var home: EngineHome!
    private var workspace: URL!
    private var runtime: URL!
    private var envRoot: URL!
    private var log = EventLog()
    private var runner: FakeRunner!
    private var engine: FakeEngine!
    private var trash: FakeTrash!
    private var prompted: [Int] = []
    private var current: EngineTakeover?

    override func setUpWithError() throws {
        let scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("EngineTakeoverTests-\(UUID().uuidString)", isDirectory: true)
        tmp = scratch.resolvingSymlinksInPath()
        let fm = FileManager.default
        home = EngineHome(home: tmp.appendingPathComponent("home", isDirectory: true))
        workspace = home.home.appendingPathComponent("Projects/ws", isDirectory: true)
        runtime = workspace.appendingPathComponent(".runtime", isDirectory: true)
        envRoot = home.home.appendingPathComponent(".local/share/uv/tools/ciaobot", isDirectory: true)
        let directories: [URL] = [home.stateDir, home.takeoverDir, workspace.appendingPathComponent("data"), runtime, envRoot]
        for directory in directories {
            try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        }
        log = EventLog()
        runner = FakeRunner(log: log)
        engine = FakeEngine(log: log)
        trash = FakeTrash(log: log)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: tmp)
    }

    // MARK: Fixtures

    private func planJSON(
        action: String, reason: String = "", sha: String = "abc123", activeChats: Int = 0,
        oldRunning: Bool = false, trash: [[String: String]] = [], resumeId: String = "",
        runtimePath: String? = nil, workspacePath: String? = nil
    ) -> String {
        let object: [String: Any] = [
            "action": action, "reason": reason, "label": "com.ciao.server",
            "plist": home.home.appendingPathComponent("Library/LaunchAgents/com.ciao.server.plist").path,
            "plist_sha256": sha, "workspace": workspacePath ?? workspace.path, "runtime_root": runtimePath ?? runtime.path,
            "port": 8443, "program_kind": "installer_env", "old_version": "1.10.0",
            "old_running": oldRunning, "active_chats": activeChats, "trash": trash,
            "left_in_place": [String](), "dropped_env": [String](), "resume_id": resumeId,
        ]
        return json(object)
    }

    private func json(_ object: [String: Any]) -> String {
        String(decoding: try! JSONSerialization.data(withJSONObject: object), as: UTF8.self)
    }

    private func candidate(_ url: URL, proof: String = "installer_env") -> [String: String] {
        ["path": url.path, "proof": proof]
    }

    private func writeReadyRecord(workspace path: String? = nil, pid: Int = 4242) throws {
        let data = try JSONSerialization.data(withJSONObject: [
            "schema": 1, "pid": pid, "port": 8443, "startup_id": "abc",
            "workspace": path ?? workspace.path, "bootstrap": false,
        ])
        try data.write(to: home.readyRecord)
    }

    /// `confirm` answers the Move Now question, `answering` what the old origin's probe says.
    private func makeTakeover(
        labelOverride: String? = nil,
        windowVisible: Bool = true,
        readyTimeout: TimeInterval = 1,
        confirm: Bool = true,
        answering: Bool = false
    ) -> EngineTakeover {
        let takeover = EngineTakeover(
            runner: runner, supervisor: engine, trash: trash, home: home,
            labelOverride: labelOverride,
            confirmMove: { [self] chats in
                prompted.append(chats)
                return confirm
            },
            windowVisible: { windowVisible },
            readyTimeout: readyTimeout,
            pollInterval: 0.001,
            probe: { _ in answering }
        )
        current = takeover
        return takeover
    }

    private func writeJournal(_ id: String, sha: String = "journalsha") throws {
        let directory = home.takeoverDir.appendingPathComponent(id, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try json(["plist_sha256": sha]).write(to: directory.appendingPathComponent("journal.json"),
                                              atomically: true, encoding: .utf8)
    }

    private func applyOf(_ calls: [[String]]) -> [String]? {
        calls.first { $0.first == "apply" }
    }

    /// A happy-path takeover: apply, healthy engine, finish, Trash, close.
    private func answerHappyPath(trash candidates: [[String: String]]) throws {
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true, trash: candidates)
        runner.answers["apply"] = json(["ok": true, "id": "T1", "state": "pointer_written"])
        runner.answers["finish"] = json(["ok": true, "id": "T1", "trash": candidates, "left_in_place": [String]()])
        runner.answers["finish --done"] = json(["ok": true, "id": "T1", "trash": [String](), "left_in_place": [String]()])
        try FileManager.default.createDirectory(
            at: home.takeoverDir.appendingPathComponent("T1", isDirectory: true), withIntermediateDirectories: true)
        try writeReadyRecord()
    }

    private func describe(_ outcome: EngineTakeover.Outcome) -> String {
        switch outcome {
        case .notNeeded: return "notNeeded"
        case .done(let left): return "done:\(left.count)"
        case .deferred(let origin): return "deferred:\(origin?.url.absoluteString ?? "-")"
        case .refused(let code, let origin): return "refused:\(code):\(origin?.url.absoluteString ?? "-")"
        case .rolledBack(let code, let origin): return "rolledBack:\(code):\(origin?.url.absoluteString ?? "-")"
        }
    }

    private var oldOriginText: String { FakeEngine.origin.url.absoluteString }

    // MARK: Settling without a move

    func testNoOldInstallStartsTheEngineAndAsksNothing() async {
        runner.answers["plan"] = planJSON(action: "none", reason: "already_app")
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "notNeeded")
        XCTAssertEqual(log.events, ["cmd:plan", "start"])
        XCTAssertEqual(runner.calls, [["plan", "--json"]])
    }

    func testRefusalNeverStartsTheEngineAndKeepsTheOldOrigin() async {
        runner.answers["plan"] = planJSON(action: "refuse", reason: "downgrade", oldRunning: true)
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:downgrade:\(oldOriginText)")
        XCTAssertNil(log.index(of: "start"))
        XCTAssertEqual(log.count(prefix: "cmd:apply"), 0)
    }

    func testActiveChatsInAVisibleWindowAreAskedOnceAndLaterDefers() async {
        runner.answers["plan"] = planJSON(action: "takeover", activeChats: 2, oldRunning: true)
        let takeover = EngineTakeover(
            runner: runner, supervisor: engine, trash: trash, home: home, labelOverride: nil,
            confirmMove: { [self] chats in prompted.append(chats); return false },
            windowVisible: { true }, readyTimeout: 1, pollInterval: 0.001
        )
        let outcome = await takeover.runIfNeeded()
        XCTAssertEqual(prompted, [2])
        XCTAssertEqual(describe(outcome), "deferred:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:apply"), 0)
        XCTAssertNil(log.index(of: "start"))
    }

    func testActiveChatsWithNoWindowDeferSilently() async {
        runner.answers["plan"] = planJSON(action: "takeover", activeChats: 1, oldRunning: true)
        let outcome = await makeTakeover(windowVisible: false).runIfNeeded()
        XCTAssertTrue(prompted.isEmpty, "a login launch must not show the question")
        XCTAssertEqual(describe(outcome), "deferred:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:apply"), 0)
    }

    func testPlanFailureIsRefusedWithoutTouchingAnything() async {
        runner.answers["plan"] = "not json"
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:plan_failed:-")
        XCTAssertEqual(log.events, ["cmd:plan"])
    }

    // MARK: The move

    func testHappyPathRunsApplyStartFinishTrashThenClose() async throws {
        try answerHappyPath(trash: [candidate(envRoot)])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "done:0")
        XCTAssertEqual(log.events, [
            "cmd:plan", "cmd:apply", "start", "cmd:finish",
            "trash:ciaobot", "cmd:finish --done",
        ])
        XCTAssertEqual(trash.trashed.map(\.path), [envRoot.path])
    }

    func testApplyCarriesThePlanHash() async throws {
        try answerHappyPath(trash: [])
        _ = await makeTakeover().runIfNeeded()
        let apply = try XCTUnwrap(runner.calls.first { $0.first == "apply" })
        XCTAssertEqual(apply.drop(while: { $0 != "--expect-sha256" }).dropFirst().first, "abc123")
    }

    func testTrashRecordNamesWhatWentToTheTrash() async throws {
        try answerHappyPath(trash: [candidate(envRoot)])
        _ = await makeTakeover().runIfNeeded()
        let record = home.takeoverDir.appendingPathComponent("T1/trash.json")
        let data = try Data(contentsOf: record)
        let entries = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [[String: Any]])
        XCTAssertEqual(entries.count, 1)
        XCTAssertEqual(entries[0]["path"] as? String, envRoot.path)
        XCTAssertEqual(entries[0]["trashed_to"] as? String, "/Trash/ciaobot")
    }

    func testCandidatesAreRecheckedBeforeAnyTrashing() async throws {
        let outside = tmp.appendingPathComponent("outside", isDirectory: true)
        try FileManager.default.createDirectory(at: outside, withIntermediateDirectories: true)
        let projects = workspace.deletingLastPathComponent()
        let offered = [
            candidate(workspace),                                              // equal to the workspace
            candidate(workspace.appendingPathComponent("data")),              // inside the workspace
            candidate(projects),                                              // an ancestor of the workspace
            candidate(home.home),                                             // the home folder itself
            candidate(outside),                                               // outside the home folder
            candidate(envRoot),                                               // the one that may go
        ]
        try answerHappyPath(trash: offered)
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(trash.trashed.map(\.path), [envRoot.path])
        guard case .done(let left) = outcome else { return XCTFail("expected done, got \(describe(outcome))") }
        XCTAssertEqual(Set(left), [workspace.path, workspace.appendingPathComponent("data").path,
                                   projects.path, home.home.path, outside.path])
    }

    /// With the runtime root elsewhere, only the equality guard can refuse the workspace itself.
    func testTheWorkspaceItselfIsNeverTrashedWhenTheRuntimeRootIsElsewhere() async throws {
        let elsewhere = home.home.appendingPathComponent("Library/Application Support/Ciaobot/runtime", isDirectory: true)
        try FileManager.default.createDirectory(at: elsewhere, withIntermediateDirectories: true)
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true,
                                          trash: [candidate(workspace)], runtimePath: elsewhere.path)
        runner.answers["apply"] = json(["ok": true, "id": "T1", "state": "pointer_written"])
        runner.answers["finish"] = json(["ok": true, "id": "T1", "trash": [candidate(workspace)],
                                         "left_in_place": [String]()])
        runner.answers["finish --done"] = json(["ok": true, "id": "T1"])
        try FileManager.default.createDirectory(
            at: home.takeoverDir.appendingPathComponent("T1", isDirectory: true), withIntermediateDirectories: true)
        try writeReadyRecord()
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertTrue(trash.trashed.isEmpty)
        guard case .done(let left) = outcome else { return XCTFail("expected done") }
        XCTAssertEqual(left, [workspace.path])
    }

    func testTrashFailureIsReportedAsLeftAndNeverRetried() async throws {
        trash.failing = [envRoot.lastPathComponent]
        try answerHappyPath(trash: [candidate(envRoot)])
        let outcome = await makeTakeover().runIfNeeded()
        guard case .done(let left) = outcome else { return XCTFail("expected done") }
        XCTAssertEqual(left, [envRoot.path])
        XCTAssertEqual(log.count(prefix: "trash:"), 1, "a failed trash is not retried")
        XCTAssertTrue(trash.trashed.isEmpty)
    }

    func testApplyRefusalWithoutAnIdDoesNotRollBack() async {
        runner.answers["plan"] = planJSON(action: "takeover")
        runner.answers["apply"] = json(["ok": false, "reason": "sha_mismatch"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:sha_mismatch:-")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 0)
    }

    func testApplyRefusalWithAnIdRollsBackAndReportsTheOldOrigin() async {
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true)
        runner.answers["apply"] = json(["ok": false, "reason": "old_engine_did_not_stop", "id": "T1", "state": "stopping"])
        runner.answers["rollback"] = json(["ok": true, "id": "T1", "state": "rolled_back"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "rolledBack:old_engine_did_not_stop:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 1)
    }

    func testApplyThatAlreadyRolledBackIsNotRolledBackAgain() async {
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true)
        runner.answers["apply"] = json(["ok": false, "reason": "old_engine_did_not_start", "id": "T1", "state": "rolled_back"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "rolledBack:old_engine_did_not_start:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 0)
    }

    func testReadinessTimeoutStopsTheEngineThenRollsBack() async throws {
        engine.startsRunning = false
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true)
        runner.answers["apply"] = json(["ok": true, "id": "T1", "state": "pointer_written"])
        runner.answers["rollback"] = json(["ok": true, "id": "T1", "state": "rolled_back"])
        let outcome = await makeTakeover(readyTimeout: 0.05).runIfNeeded()
        XCTAssertEqual(describe(outcome), "rolledBack:engine_not_ready:\(oldOriginText)")
        let stop = try XCTUnwrap(log.index(of: "stop"))
        let rollback = try XCTUnwrap(log.index(of: "cmd:rollback"))
        XCTAssertLessThan(stop, rollback, "the engine must be stopped before the old one comes back")
    }

    func testReadyRecordForAnotherWorkspaceRollsBack() async throws {
        try answerHappyPath(trash: [])
        try writeReadyRecord(workspace: home.home.appendingPathComponent("elsewhere").path)
        runner.answers["rollback"] = json(["ok": true, "id": "T1", "state": "rolled_back"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "rolledBack:engine_not_ready:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:finish"), 0, "an unproven engine is never finished")
    }

    func testFinishRefusalRollsBackBeforeAnythingIsTrashed() async throws {
        try answerHappyPath(trash: [candidate(envRoot)])
        runner.answers["finish"] = json(["ok": false, "reason": "not_healthy", "id": "T1"])
        runner.answers["rollback"] = json(["ok": true, "id": "T1", "state": "rolled_back"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "rolledBack:not_healthy:\(oldOriginText)")
        XCTAssertTrue(trash.trashed.isEmpty)
        XCTAssertEqual(log.count(prefix: "cmd:finish --done"), 0)
    }

    func testRollbackThatFailsIsRefusedNotReportedAsRolledBack() async {
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true)
        runner.answers["apply"] = json(["ok": false, "reason": "old_engine_did_not_stop", "id": "T1"])
        runner.answers["rollback"] = json(["ok": false, "reason": "bundled_engine_running", "id": "T1"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:rollback_failed:-")
    }

    // MARK: Resume and labels

    func testResumeTakesTheHashFromTheJournal() async throws {
        runner.answers["plan"] = planJSON(action: "resume", resumeId: "T9")
        let journal = home.takeoverDir.appendingPathComponent("T9", isDirectory: true)
        try FileManager.default.createDirectory(at: journal, withIntermediateDirectories: true)
        try json(["plist_sha256": "journalsha"]).write(to: journal.appendingPathComponent("journal.json"),
                                                       atomically: true, encoding: .utf8)
        runner.answers["apply"] = json(["ok": false, "reason": "already_settled"])
        _ = await makeTakeover().runIfNeeded()
        let apply = try XCTUnwrap(runner.calls.first { $0.first == "apply" })
        XCTAssertEqual(apply.drop(while: { $0 != "--expect-sha256" }).dropFirst().first, "journalsha")
    }

    func testResumeWithoutAReadableJournalIsRefusedBeforeApply() async {
        runner.answers["plan"] = planJSON(action: "resume", resumeId: "T9")
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:resume_unreadable:-")
        XCTAssertEqual(log.count(prefix: "cmd:apply"), 0)
    }

    func testTestLabelScopesEveryPlanAndApplyToTheIsolatedFolder() async throws {
        try answerHappyPath(trash: [])
        let agents = home.home.appendingPathComponent("Library/LaunchAgents").path
        _ = await makeTakeover(labelOverride: "com.ciao.server.t3129").runIfNeeded()
        XCTAssertEqual(runner.calls.first, ["plan", "--json", "--label", "com.ciao.server.t3129",
                                            "--launch-agents-dir", agents])
        let apply = try XCTUnwrap(runner.calls.first { $0.first == "apply" })
        XCTAssertTrue(apply.containsSequence(["--label", "com.ciao.server.t3129", "--launch-agents-dir", agents]))
    }

    func testWithoutAnOverrideNoLabelOrFolderIsPassed() async {
        runner.answers["plan"] = planJSON(action: "none")
        _ = await makeTakeover().runIfNeeded()
        XCTAssertEqual(runner.calls.first, ["plan", "--json"])
    }

    func testAConcurrentCallJoinsTheRunInFlight() async {
        runner.answers["plan"] = planJSON(action: "none")
        let takeover = makeTakeover()
        async let first = takeover.runIfNeeded()
        async let second = takeover.runIfNeeded()
        let (a, b) = await (first, second)
        XCTAssertEqual(describe(a), "notNeeded")
        XCTAssertEqual(describe(b), "notNeeded")
        XCTAssertEqual(runner.calls.filter { $0.first == "plan" }.count, 1)
    }

    func testSettledIsPublishedWithAIncreasingSerial() async {
        runner.answers["plan"] = planJSON(action: "none")
        let takeover = makeTakeover()
        XCTAssertNil(takeover.settled)
        _ = await takeover.runIfNeeded()
        _ = await takeover.runIfNeeded()
        XCTAssertEqual(takeover.settled?.serial, 2)
    }

    // MARK: Copy

    func testRefusalCopyNamesTheOldOriginOnlyWhenItAnswers() {
        let text = TakeoverCopy.notice(for: .refused(code: "downgrade", oldOrigin: FakeEngine.origin))
        XCTAssertEqual(text, TakeoverCopy.refused(code: "downgrade") + " " + TakeoverCopy.connected)
        XCTAssertEqual(TakeoverCopy.notice(for: .refused(code: "downgrade", oldOrigin: nil)),
                       TakeoverCopy.refused(code: "downgrade"))
    }

    func testRolledBackCopyIsTheFixedSentence() {
        XCTAssertEqual(TakeoverCopy.notice(for: .rolledBack(code: "not_healthy", oldOrigin: nil)),
                       TakeoverCopy.rolledBack)
        XCTAssertEqual(TakeoverCopy.rolledBack,
                       "Ciaobot could not finish moving into the app, so your existing Ciaobot was restarted unchanged. Use Ciaobot on This Mac to try again.")
    }

    func testEachNewOutcomeHasItsOwnCopy() {
        XCTAssertEqual(TakeoverCopy.refused(code: "already_settled"), "The move into the app had already completed.")
        XCTAssertEqual(TakeoverCopy.refused(code: "plist_changed_stopped"), TakeoverCopy.plistChangedWhileStopped)
        XCTAssertEqual(TakeoverCopy.refused(code: "moved_not_ready"), TakeoverCopy.movedNotReady)
        XCTAssertEqual(TakeoverCopy.refused(code: "plist_changed"), TakeoverCopy.plistChangedWhileRunning)
        XCTAssertEqual(TakeoverCopy.refused(code: "cancelled_past_trash"), TakeoverCopy.cancelledPastTrash)
        XCTAssertFalse(TakeoverCopy.movedNotReady.contains("not changed"))
        XCTAssertFalse(TakeoverCopy.plistChangedWhileStopped.contains("not changed"))
        XCTAssertFalse(TakeoverCopy.plistChangedWhileStopped.contains("Use Ciaobot on This Mac"))
        XCTAssertFalse(TakeoverCopy.cancelledPastTrash.contains("could not start"))
    }

    /// The trimmed table: codes that can be transient races no longer persist the
    /// external choice, and they get the retry copy.
    func testTheRemovedRefusalCodesAreTransientAndUnknownCodesToo() {
        for code in ["unknown_version", "foreign_listener", "no_registry", "invented_code"] {
            XCTAssertFalse(EngineTakeover.isDeliberateRefusal(code), code)
            XCTAssertEqual(TakeoverCopy.refused(code: code), TakeoverCopy.retry, code)
        }
        XCTAssertEqual(TakeoverRefusal.deliberate, [
            "downgrade", "engine_too_old", "custom_data_location", "port_mismatch",
            "legacy_desktop", "unknown_program", "no_workspace",
        ])
    }

    /// Which outcomes persist the external choice. Only a deliberate verdict, or a
    /// rollback that completed after the bundled engine failed, does.
    func testOnlyDeliberateRefusalsAndBundledFailureRollbacksPersistExternal() {
        let persisting: [EngineTakeover.Outcome] = [
            .refused(code: "downgrade", oldOrigin: nil),
            .refused(code: "no_workspace", oldOrigin: nil),
            .rolledBack(code: "engine_not_ready", oldOrigin: nil),
            .rolledBack(code: "not_healthy", oldOrigin: nil),
            .refused(code: "plist_changed_stopped", oldOrigin: nil),
        ]
        for outcome in persisting {
            XCTAssertTrue(EngineTakeover.savesExternalChoice(outcome), describe(outcome))
        }
        let keeping: [EngineTakeover.Outcome] = [
            .notNeeded,
            .done(left: []),
            .deferred(origin: nil),
            .refused(code: "cancelled", oldOrigin: nil),
            .refused(code: "takeover_failed", oldOrigin: nil),
            .refused(code: "rollback_failed", oldOrigin: nil),
            .refused(code: "state_unknown", oldOrigin: nil),
            .refused(code: "plan_failed", oldOrigin: nil),
            .refused(code: "moved_not_ready", oldOrigin: nil),
            .refused(code: "plist_changed", oldOrigin: nil),
            .refused(code: "cancelled_past_trash", oldOrigin: nil),
            .refused(code: "already_settled", oldOrigin: nil),
            .rolledBack(code: "cancelled", oldOrigin: nil),
            .rolledBack(code: "takeover_failed", oldOrigin: nil),
            .rolledBack(code: "old_engine_did_not_start", oldOrigin: nil),
        ]
        for outcome in keeping {
            XCTAssertFalse(EngineTakeover.savesExternalChoice(outcome), describe(outcome))
        }
    }

    /// A bundled-engine failure whose rollback really completes is the outcome that
    /// persists, from the flow itself, not from a constructed outcome.
    func testAReadinessFailureThatRollsBackPersistsExternal() async {
        engine.startsRunning = false
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true)
        runner.answers["apply"] = json(["ok": true, "id": "T1", "state": "pointer_written"])
        runner.answers["rollback"] = json(["ok": true, "id": "T1", "state": "rolled_back"])
        let outcome = await makeTakeover(readyTimeout: 0.05).runIfNeeded()
        XCTAssertTrue(EngineTakeover.savesExternalChoice(outcome))
    }

    /// A rollback that the engine refuses is not a completed rollback: nothing persists.
    func testAFailedRollbackAfterABundledFailureDoesNotPersistExternal() async {
        engine.startsRunning = false
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true)
        runner.answers["apply"] = json(["ok": true, "id": "T1", "state": "pointer_written"])
        runner.answers["rollback"] = json(["ok": false, "reason": "plist_conflict", "id": "T1"])
        let outcome = await makeTakeover(readyTimeout: 0.05).runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:rollback_failed:-")
        XCTAssertFalse(EngineTakeover.savesExternalChoice(outcome))
    }

    func testSilentOutcomesShowNoNotice() {
        XCTAssertNil(TakeoverCopy.notice(for: .notNeeded))
        XCTAssertNil(TakeoverCopy.notice(for: .deferred(origin: FakeEngine.origin)))
        XCTAssertNil(TakeoverCopy.notice(for: .done(left: [])))
        XCTAssertNotNil(TakeoverCopy.notice(for: .done(left: ["/x/checkout"])))
    }

    #if DEBUG
    func testTestLabelIsAcceptedOnlyForAPlainNonLiveLabel() {
        func label(_ raw: String) -> String? {
            EngineTakeover.testLabel(environment: ["CIAOBOT_MAC_TEST_TAKEOVER_LABEL": raw])
        }
        XCTAssertEqual(label("com.ciao.server.t3129"), "com.ciao.server.t3129")
        XCTAssertNil(label("com.ciao.server"))
        XCTAssertNil(label(""))
        XCTAssertNil(label("a/b"))
        XCTAssertNil(label(String(repeating: "a", count: 129)))
        XCTAssertNil(EngineTakeover.testLabel(environment: [:]))
    }
    #endif

    // MARK: The process runner

    func testFoundationRunnerReturnsStatusAndStdoutOfTheCommand() async throws {
        let script = tmp.appendingPathComponent("takeover-fake")
        try "#!/bin/sh\nprintf '{\"ok\":true,\"n\":%s}' \"$#\"\n".write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        let runner = FoundationTakeoverRunner { arguments in
            TakeoverCommandLine(executable: script, arguments: arguments, environment: [:], workingDirectory: self.tmp)
        }
        let result = try await runner.run(["plan", "--json", "--label", "x"])
        XCTAssertEqual(result.status, 0)
        XCTAssertEqual(TakeoverAnswer.decode(result.json)?.ok, true)
        XCTAssertEqual(String(decoding: result.json, as: UTF8.self), "{\"ok\":true,\"n\":4}")
    }

    // MARK: Lost answers: the journal decides, never the missing answer

    func testLostApplyWhilePlanStillSaysTakeoverRefusesWithoutRollingBack() async {
        runner.sequences["plan"] = [planJSON(action: "takeover", oldRunning: true),
                                    planJSON(action: "takeover", oldRunning: true)]
        runner.failing = ["apply"]
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:takeover_failed:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 0, "nothing was journalled, so there is nothing to roll back")
        XCTAssertNil(log.index(of: "start"))
    }

    func testLostApplyWithAResumablePlanRollsTheJournalBackOnce() async throws {
        runner.sequences["plan"] = [planJSON(action: "takeover", oldRunning: true),
                                    planJSON(action: "resume", resumeId: "T1")]
        runner.answers["apply"] = "not json"
        runner.answers["rollback"] = json(["ok": true, "id": "T1", "state": "rolled_back"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "rolledBack:takeover_failed:\(oldOriginText)")
        let stop = try XCTUnwrap(log.index(of: "stop"))
        let rollback = try XCTUnwrap(log.index(of: "cmd:rollback"))
        XCTAssertLessThan(stop, rollback)
        XCTAssertNil(log.index(of: "start"), "the bundled engine is never started on a lost apply")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 1)
    }

    func testAnErrorDocumentFromTheEngineIsALostAnswer() async {
        runner.sequences["plan"] = [planJSON(action: "takeover", oldRunning: true),
                                    planJSON(action: "resume", resumeId: "T1")]
        runner.answers["apply"] = json(["action": "error", "reason": "error", "message": "boom",
                                        "id": "T1", "state": "stopping"])
        runner.answers["rollback"] = json(["ok": true, "id": "T1", "state": "rolled_back"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "rolledBack:takeover_failed:\(oldOriginText)")
    }

    func testAnOkAnswerWithoutAnIdIsAlsoALostAnswer() async {
        runner.sequences["plan"] = [planJSON(action: "takeover", oldRunning: true),
                                    planJSON(action: "resume", resumeId: "T1")]
        runner.answers["apply"] = json(["ok": true])
        runner.answers["rollback"] = json(["ok": true, "id": "T1", "state": "rolled_back"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "rolledBack:takeover_failed:\(oldOriginText)")
    }

    func testLostApplyAndAFailedPlanSaysTheStateIsUnknown() async {
        runner.sequences["plan"] = [planJSON(action: "takeover", oldRunning: true), "not json"]
        runner.failing = ["apply"]
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:state_unknown:-")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 0)
        XCTAssertNil(log.index(of: "start"))
    }

    func testLostApplyThenAPlanThatRefusesReportsTheRefusal() async {
        runner.sequences["plan"] = [planJSON(action: "takeover", oldRunning: true),
                                    planJSON(action: "refuse", reason: "downgrade", oldRunning: true)]
        runner.failing = ["apply"]
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:downgrade:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 0)
    }

    /// Both finish answers are lost and the engine never journalled a Trash offer:
    /// the rollback is allowed, and nothing is trashed.
    func testALostFinishTwiceWithoutATrashOfferRollsBack() async throws {
        try answerHappyPath(trash: [candidate(envRoot)])
        runner.lostFirst = ["finish": 2]
        runner.answers["rollback"] = json(["ok": true, "id": "T1", "state": "rolled_back"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "rolledBack:not_healthy:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:finish"), 2, "the finish is asked again once, then the move rolls back")
        XCTAssertTrue(trash.trashed.isEmpty)
    }

    /// A lost finish answer that is asked again and answered: the retry's answer is used.
    func testALostFinishIsRetriedOnceAndTheRetryAnswerIsUsed() async throws {
        try answerHappyPath(trash: [candidate(envRoot)])
        runner.lostFirst = ["finish": 1]
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "done:0")
        XCTAssertEqual(log.events, [
            "cmd:plan", "cmd:apply", "start", "cmd:finish", "cmd:finish",
            "trash:ciaobot", "cmd:finish --done",
        ])
        XCTAssertEqual(trash.trashed.map(\.path), [envRoot.path])
    }

    /// The engine journalled the Trash offer, and both finish answers were lost. The
    /// rollback is refused with `trash_offered`, so the app does not report
    /// rollback_failed: it starts the bundled engine again, finishes, and closes.
    func testALostFinishAfterTheTrashWasOfferedFinishesWithTheBundledEngine() async throws {
        try answerHappyPath(trash: [candidate(envRoot)])
        runner.engineJournalsTrash = true
        runner.lostFirst = ["finish": 2]
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "done:0")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 1, "the rollback was asked and refused")
        XCTAssertEqual(log.events.filter { $0 == "start" }.count, 2, "the bundled engine is started again")
        XCTAssertEqual(log.count(prefix: "cmd:finish"), 4, "two lost, one answered, then the close")
        XCTAssertEqual(trash.trashed.map(\.path), [envRoot.path])
    }

    /// The same refusal when the bundled engine does not come back: the outcome is
    /// neutral, the engine is stopped, and nothing was trashed.
    func testTrashOfferedAndTheBundledEngineDoesNotComeUpIsNotRolledBackAsFailed() async throws {
        try answerHappyPath(trash: [candidate(envRoot)])
        runner.engineJournalsTrash = true
        runner.lostFirst = ["finish": 2]
        runner.onRun = { [weak self] key in
            guard key == "rollback" else { return }
            await MainActor.run { self?.engine.startsRunning = false }
        }
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:moved_not_ready:-")
        XCTAssertEqual(log.count(prefix: "cmd:finish"), 2, "no finish is asked once the engine did not come up")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 1)
        XCTAssertTrue(trash.trashed.isEmpty)
        let stop = try XCTUnwrap(log.events.lastIndex(of: "stop"))
        XCTAssertGreaterThan(stop, log.index(of: "cmd:rollback") ?? 0, "the engine that failed to start is stopped again")
        XCTAssertEqual(TakeoverCopy.notice(for: outcome), TakeoverCopy.movedNotReady)
    }

    /// A user who left local mode after the Trash offer is not given a local
    /// engine back: the start is skipped and the next launch's resume takes over.
    func testTrashOfferedAfterACancelDoesNotStartTheBundledEngine() async throws {
        try answerHappyPath(trash: [candidate(envRoot)])
        runner.engineJournalsTrash = true
        runner.lostFirst = ["finish": 2]
        runner.onRun = { [weak self] key in
            guard key == "rollback" else { return }
            await MainActor.run { self?.current?.cancel() }
        }
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:cancelled_past_trash:-")
        XCTAssertEqual(log.events.filter { $0 == "start" }.count, 1, "only the first start ran")
        XCTAssertTrue(trash.trashed.isEmpty)
        let text = try XCTUnwrap(TakeoverCopy.notice(for: outcome))
        XCTAssertEqual(text, TakeoverCopy.cancelledPastTrash)
        XCTAssertFalse(text.contains("could not start"), "a cancel is not a failure to start")
        XCTAssertFalse(EngineTakeover.savesExternalChoice(outcome))
    }

    /// A lost rollback answer is asked once more, as finish is. The retry is refused
    /// with `trash_offered` here, so the move is taken forward on the bundled engine.
    func testALostRollbackIsAskedOnceMoreAndTheRetryIsUsed() async throws {
        try answerHappyPath(trash: [candidate(envRoot)])
        runner.engineJournalsTrash = true
        runner.lostFirst = ["finish": 2, "rollback": 1]
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "done:0")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 2, "the lost rollback was asked again")
        XCTAssertEqual(trash.trashed.map(\.path), [envRoot.path])
    }

    /// Both rollback answers lost: the state is unknown, the copy says so neutrally,
    /// and nothing is trashed. The next launch's resume repairs it.
    func testALostRollbackTwiceIsUnknownWithNeutralCopy() async throws {
        try answerHappyPath(trash: [candidate(envRoot)])
        runner.lostFirst = ["finish": 2, "rollback": 2]
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:state_unknown:-")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 2)
        XCTAssertTrue(trash.trashed.isEmpty)
        XCTAssertEqual(TakeoverCopy.notice(for: outcome), TakeoverCopy.stateUnknown)
        XCTAssertFalse(TakeoverCopy.stateUnknown.contains("kept for support"))
        XCTAssertFalse(EngineTakeover.savesExternalChoice(outcome))
    }

    /// The engine's resume refusal for a journal whose hash differs carries an id
    /// and no state: the app rolls back, which is safe.
    func testAResumeShaMismatchWithOnlyAnIdRollsBack() async throws {
        try writeJournal("T1")
        runner.answers["plan"] = planJSON(action: "resume", oldRunning: true, resumeId: "T1")
        runner.answers["apply"] = json(["ok": false, "reason": "sha_mismatch", "id": "T1"])
        runner.answers["rollback"] = json(["ok": true, "id": "T1", "state": "rolled_back"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "rolledBack:sha_mismatch:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 1)
        XCTAssertNil(log.index(of: "start"))
    }

    // MARK: Refusals before anything was stopped

    func testBackupUnusableBeforeTheStopKeepsTheOldEngineAndDoesNotRollBack() async {
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true)
        runner.answers["apply"] = json(["ok": false, "reason": "backup_unusable", "id": "T1", "state": "started"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:backup_unusable:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 0)
        XCTAssertNil(log.index(of: "stop"))
    }

    func testAPlistChangedBeforeTheStopIsNotRolledBack() async {
        runner.answers["plan"] = planJSON(action: "resume", oldRunning: true, resumeId: "T1")
        writeJournalOnDisk("T1")
        runner.answers["apply"] = json(["ok": false, "reason": "plist_changed", "id": "T1", "state": "started"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:plist_changed:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 0)
    }

    func testAShaMismatchWithAnIdBeforeTheStopIsNotRolledBack() async {
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true)
        runner.answers["apply"] = json(["ok": false, "reason": "sha_mismatch", "id": "T1", "state": "started"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:sha_mismatch:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 0)
    }

    func testAShaMismatchWithoutAnIdReportsTheOldOriginItLeftRunning() async {
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true)
        runner.answers["apply"] = json(["ok": false, "reason": "sha_mismatch"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:sha_mismatch:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 0)
    }

    func testAnOldEngineThatWasNotRestartedIsRolledBackEvenAtStarted() async {
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true)
        runner.answers["apply"] = json(["ok": false, "reason": "old_engine_not_restarted", "id": "T1", "state": "started"])
        runner.answers["rollback"] = json(["ok": true, "id": "T1", "state": "rolled_back"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "rolledBack:old_engine_not_restarted:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 1)
    }

    /// A `started` refusal before the stop: with the old engine running it is left
    /// as it was; with it down (a watchdog ended apply after the unload) it is rolled back.
    func testAStartedRefusalBeforeTheStopDependsOnWhetherTheOldEngineRuns() async throws {
        for code in ["backup_unusable", "plist_changed", "sha_mismatch"] {
            for oldRunning in [true, false] {
                log.events = []
                runner.calls = []
                runner.answers["plan"] = planJSON(action: "takeover", oldRunning: oldRunning)
                runner.answers["apply"] = json(["ok": false, "reason": code, "id": "T1", "state": "started"])
                runner.answers["rollback"] = json(["ok": true, "id": "T1", "state": "rolled_back"])
                let outcome = await makeTakeover().runIfNeeded()
                let context = "\(code) with oldRunning \(oldRunning)"
                if oldRunning {
                    XCTAssertEqual(describe(outcome), "refused:\(code):\(oldOriginText)", context)
                    XCTAssertEqual(log.count(prefix: "cmd:rollback"), 0, context)
                } else {
                    XCTAssertEqual(describe(outcome), "rolledBack:\(code):\(oldOriginText)", context)
                    XCTAssertEqual(log.count(prefix: "cmd:rollback"), 1, context)
                }
            }
        }
    }

    /// The old service is down (the plan says so) and its definition changed under
    /// it: the rollback would refuse a changed definition, so none is attempted, the
    /// copy says the old install must be restored, and the choice persists so the
    /// next launch does not repeat the refusal.
    func testPlistChangedWhileTheOldEngineIsStoppedSaysSoAndNeverRollsBack() async {
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: false)
        runner.answers["apply"] = json(["ok": false, "reason": "plist_changed", "id": "T1", "state": "stopping"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:plist_changed_stopped:-")
        let text = TakeoverCopy.notice(for: outcome)
        XCTAssertEqual(text, TakeoverCopy.plistChangedWhileStopped)
        XCTAssertFalse(text?.contains("continue") ?? true, "nothing here offers to continue the move")
        XCTAssertTrue(text?.contains("restore") ?? false)
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 0)
        XCTAssertNil(log.index(of: "start"))
        XCTAssertTrue(EngineTakeover.savesExternalChoice(outcome), "the next launch must not retry")
    }

    /// The definition changed while the old service still runs (a login brought it
    /// back, or the stop never took): the app connects to that engine and does not
    /// roll back or start the bundled one.
    func testPlistChangedWhileTheOldEngineRunsConnectsToIt() async {
        runner.answers["plan"] = planJSON(action: "resume", oldRunning: true, resumeId: "T1")
        writeJournalOnDisk("T1")
        runner.answers["apply"] = json(["ok": false, "reason": "plist_changed", "id": "T1", "state": "stopping"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:plist_changed:\(oldOriginText)")
        XCTAssertEqual(TakeoverCopy.notice(for: outcome), TakeoverCopy.plistChangedWhileRunning + " " + TakeoverCopy.connected)
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 0)
        XCTAssertNil(log.index(of: "start"))
        XCTAssertFalse(EngineTakeover.savesExternalChoice(outcome))
    }

    /// The same apply answer, with the plan's old-engine verdict as the only difference.
    func testPlistChangedInStoppingBranchesOnWhetherTheOldEngineRuns() async {
        for oldRunning in [true, false] {
            log.events = []
            runner.calls = []
            runner.answers["plan"] = planJSON(action: "takeover", oldRunning: oldRunning)
            runner.answers["apply"] = json(["ok": false, "reason": "plist_changed", "id": "T1", "state": "stopping"])
            let outcome = await makeTakeover().runIfNeeded()
            let expected = oldRunning ? "refused:plist_changed:\(oldOriginText)" : "refused:plist_changed_stopped:-"
            XCTAssertEqual(describe(outcome), expected, "oldRunning \(oldRunning)")
            XCTAssertEqual(log.count(prefix: "cmd:rollback"), 0, "oldRunning \(oldRunning)")
        }
    }

    /// The rolled-back streak: two engine-level rollbacks in a row persist external,
    /// a completed takeover resets it, other outcomes leave it, and a bundled-engine
    /// rollback (already persisting) or a cancel does not count.
    func testTheRolledBackStreakPersistsExternalOnTheSecondLaunchInARow() {
        let rolled = EngineTakeover.Outcome.rolledBack(code: "takeover_failed", oldOrigin: nil)
        let first = EngineTakeover.rolledBackStreak(after: rolled, previous: 0)
        XCTAssertEqual(first.count, 1)
        XCTAssertFalse(first.persistsExternal)
        let second = EngineTakeover.rolledBackStreak(after: rolled, previous: first.count)
        XCTAssertEqual(second.count, 2)
        XCTAssertTrue(second.persistsExternal)
    }

    func testTheRolledBackStreakResetsOnACompletedTakeoverAndIgnoresOtherOutcomes() {
        XCTAssertEqual(EngineTakeover.rolledBackStreak(after: .done(left: []), previous: 1).count, 0)
        let unchanged: [EngineTakeover.Outcome] = [
            .deferred(origin: nil), .refused(code: "takeover_failed", oldOrigin: nil),
            .rolledBack(code: "cancelled", oldOrigin: nil), .notNeeded,
        ]
        for outcome in unchanged {
            let step = EngineTakeover.rolledBackStreak(after: outcome, previous: 1)
            XCTAssertEqual(step.count, 1, describe(outcome))
            XCTAssertFalse(step.persistsExternal, describe(outcome))
        }
        let persisted = EngineTakeover.rolledBackStreak(
            after: .rolledBack(code: "engine_not_ready", oldOrigin: nil), previous: 1)
        XCTAssertEqual(persisted.count, 1, "a bundled-engine rollback persists by itself and is not counted")
    }

    /// An apply refused because the move had already finished says so, and never
    /// claims that nothing changed.
    func testAnApplyRefusedBecauseTheMoveFinishedSaysItHadCompleted() async {
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true)
        runner.answers["apply"] = json(["ok": false, "reason": "already_settled", "id": "T1", "state": "finished"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:already_settled:-")
        let text = TakeoverCopy.notice(for: outcome)
        XCTAssertEqual(text, "The move into the app had already completed.")
        XCTAssertFalse(text?.contains("not changed") ?? true)
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 0)
    }

    // MARK: A failed rollback probes the old origin

    func testAFailedRollbackConnectsToTheOldOriginWhenItAnswers() async {
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true)
        runner.answers["apply"] = json(["ok": false, "reason": "old_engine_did_not_stop", "id": "T1", "state": "stopping"])
        runner.answers["rollback"] = json(["ok": false, "reason": "plist_conflict", "id": "T1"])
        let outcome = await makeTakeover(answering: true).runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:rollback_failed:\(oldOriginText)")
    }

    func testAFailedRollbackWithAnOriginThatDoesNotAnswerReportsNone() async {
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true)
        runner.answers["apply"] = json(["ok": false, "reason": "old_engine_did_not_stop", "id": "T1", "state": "stopping"])
        runner.answers["rollback"] = json(["ok": false, "reason": "plist_conflict", "id": "T1"])
        let outcome = await makeTakeover(answering: false).runIfNeeded()
        XCTAssertEqual(describe(outcome), "refused:rollback_failed:-")
    }

    // MARK: Resume and the active-chats question

    private func resumedHappyPath(trash candidates: [[String: String]] = []) throws {
        try writeJournal("T9")
        runner.answers["apply"] = json(["ok": true, "id": "T9", "state": "pointer_written"])
        runner.answers["finish"] = json(["ok": true, "id": "T9", "trash": candidates, "left_in_place": [String]()])
        runner.answers["finish --done"] = json(["ok": true, "id": "T9"])
        try FileManager.default.createDirectory(
            at: home.takeoverDir.appendingPathComponent("T9", isDirectory: true), withIntermediateDirectories: true)
        try writeReadyRecord()
    }

    func testAResumeWithChatsAsksAndDefersWhenDeclined() async throws {
        try writeJournal("T9")
        runner.answers["plan"] = planJSON(action: "resume", activeChats: 2, oldRunning: true, resumeId: "T9")
        let outcome = await makeTakeover(confirm: false).runIfNeeded()
        XCTAssertEqual(prompted, [2], "a resume of a running old engine asks like a fresh takeover")
        XCTAssertEqual(describe(outcome), "deferred:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:apply"), 0)
    }

    func testAResumeWithChatsMovesWhenConfirmed() async throws {
        try resumedHappyPath()
        runner.answers["plan"] = planJSON(action: "resume", activeChats: 2, oldRunning: true, resumeId: "T9")
        let outcome = await makeTakeover(confirm: true).runIfNeeded()
        XCTAssertEqual(prompted, [2])
        XCTAssertEqual(describe(outcome), "done:0")
        let apply = try XCTUnwrap(applyOf(runner.calls))
        XCTAssertEqual(apply.drop(while: { $0 != "--expect-sha256" }).dropFirst().first, "journalsha")
    }

    func testAResumeWhoseChatsCannotBeCountedAsksWithoutANumber() async throws {
        try writeJournal("T9")
        runner.answers["plan"] = planJSON(action: "resume", activeChats: -1, oldRunning: true, resumeId: "T9")
        _ = await makeTakeover(confirm: false).runIfNeeded()
        XCTAssertEqual(prompted, [-1])
        XCTAssertEqual(log.count(prefix: "cmd:apply"), 0)
    }

    func testAResumeWithChatsAndNoWindowDefersSilently() async throws {
        try writeJournal("T9")
        runner.answers["plan"] = planJSON(action: "resume", activeChats: 1, oldRunning: true, resumeId: "T9")
        let outcome = await makeTakeover(windowVisible: false).runIfNeeded()
        XCTAssertTrue(prompted.isEmpty)
        XCTAssertEqual(describe(outcome), "deferred:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:apply"), 0)
    }

    func testAResumeWithNoChatsMovesWithoutAsking() async throws {
        try resumedHappyPath()
        runner.answers["plan"] = planJSON(action: "resume", activeChats: 0, oldRunning: false, resumeId: "T9")
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertTrue(prompted.isEmpty)
        XCTAssertEqual(describe(outcome), "done:0")
    }

    // MARK: Cancellation on a mode switch

    func testCancelBeforeApplyDefersWithoutApplyingOrStarting() async {
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true)
        runner.onRun = { [weak self] key in
            guard key == "plan" else { return }
            await MainActor.run { self?.current?.cancel() }
        }
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "deferred:\(oldOriginText)")
        XCTAssertEqual(log.count(prefix: "cmd:apply"), 0)
        XCTAssertNil(log.index(of: "start"))
    }

    func testCancelAfterApplyRollsBackAndNeverStarts() async {
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true)
        runner.answers["apply"] = json(["ok": true, "id": "T1", "state": "pointer_written"])
        runner.answers["rollback"] = json(["ok": true, "id": "T1", "state": "rolled_back"])
        runner.onRun = { [weak self] key in
            guard key == "apply" else { return }
            await MainActor.run { self?.current?.cancel() }
        }
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "rolledBack:cancelled:\(oldOriginText)")
        XCTAssertNil(log.index(of: "start"), "a cancelled move never starts the bundled engine")
        XCTAssertEqual(log.count(prefix: "cmd:finish"), 0)
    }

    func testCancelOutsideARunDoesNothing() async {
        runner.answers["plan"] = planJSON(action: "none")
        let takeover = makeTakeover()
        takeover.cancel()
        let outcome = await takeover.runIfNeeded()
        XCTAssertEqual(describe(outcome), "notNeeded")
    }

    func testARunStartedAfterACancelWaitsForTheCancelledOneAndRunsFresh() async {
        runner.sequences["plan"] = [planJSON(action: "takeover", oldRunning: true), planJSON(action: "none")]
        runner.onRun = { [weak self] key in
            guard key == "plan" else { return }
            await MainActor.run { self?.current?.cancel() }
        }
        let takeover = makeTakeover()
        let first = await takeover.runIfNeeded()
        XCTAssertEqual(describe(first), "deferred:\(oldOriginText)")
        runner.onRun = nil
        let second = await takeover.runIfNeeded()
        XCTAssertEqual(describe(second), "notNeeded")
        XCTAssertEqual(takeover.settled?.generation, 2)
        XCTAssertEqual(takeover.generation, 2)
    }

    func testSettledCarriesTheGenerationOfItsRun() async {
        runner.answers["plan"] = planJSON(action: "none")
        let takeover = makeTakeover()
        _ = await takeover.runIfNeeded()
        XCTAssertEqual(takeover.settled?.generation, 1)
        _ = await takeover.runIfNeeded()
        XCTAssertEqual(takeover.settled?.generation, 2)
        XCTAssertEqual(takeover.generation, 2)
    }

    /// The generation race: a run cancelled on a mode switch settles after a new
    /// start on the same turn. The new start takes its generation at once, so the
    /// cancelled run's settle is never the current generation and cannot flip the
    /// new attempt back to external.
    func testACancelledRunSettlingAfterASameTurnStartIsNotTheCurrentGeneration() async {
        runner.answers["plan"] = planJSON(action: "none")
        let takeover = makeTakeover()
        let seen = SettledRecorder()
        let subscription = takeover.$settled.compactMap { $0 }.sink { settled in
            seen.add(settled: settled.generation, current: takeover.generation)
        }
        takeover.startLocal()
        takeover.cancel()
        takeover.startLocal()
        XCTAssertEqual(takeover.generation, 2, "the new start takes its generation on this turn")
        for _ in 0..<500 where takeover.settled?.generation != 2 {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        subscription.cancel()
        XCTAssertEqual(seen.values.map { $0.settled }, [1, 2])
        XCTAssertEqual(seen.values.map { $0.settled == $0.current }, [false, true],
                       "only the run that is current may settle as current")
    }

    func testStartLocalRunsTheCheckWithoutAnyoneAwaitingIt() async {
        runner.answers["plan"] = planJSON(action: "none")
        let takeover = makeTakeover()
        takeover.startLocal()
        XCTAssertTrue(takeover.isRunning)
        for _ in 0..<500 where takeover.settled == nil {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertEqual(describe(takeover.settled?.outcome ?? .refused(code: "missing", oldOrigin: nil)), "notNeeded")
        XCTAssertEqual(takeover.settled?.generation, 1)
        XCTAssertEqual(log.events, ["cmd:plan", "start"])
    }

    /// The lexical path is the one trashed, and the check is made on it. A candidate
    /// whose parent is one place lexically and another on disk (a `..` after a
    /// symlink) is not trashed: the decoy at the lexical location stays where it is.
    func testACandidateWhoseLexicalAndOnDiskParentsDisagreeStaysInPlace() async throws {
        let other = home.home.appendingPathComponent("Projects/other", isDirectory: true)
        try FileManager.default.createDirectory(at: other, withIntermediateDirectories: true)
        let alias = home.home.appendingPathComponent("alias")
        try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: other)
        let decoy = home.home.appendingPathComponent("ws/data", isDirectory: true)
        try FileManager.default.createDirectory(at: decoy, withIntermediateDirectories: true)
        let raw = home.home.path + "/alias/../ws/data"
        try answerHappyPath(trash: [["path": raw, "proof": "installer_env"]])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertTrue(trash.trashed.isEmpty)
        XCTAssertTrue(FileManager.default.fileExists(atPath: decoy.path))
        guard case .done(let left) = outcome else { return XCTFail("expected done, got \(describe(outcome))") }
        XCTAssertEqual(left, [raw])
    }

    /// The on-disk location of `alias/../decoy` is Projects/decoy, which is not in
    /// the guarded set. The lexical `home/decoy` is a real, unrelated item. The item
    /// the candidate names on disk is not the one that would be trashed, so nothing is.
    func testADotDotCandidateWhoseOnDiskTargetDiffersFromItsLexicalTargetIsNotTrashed() async throws {
        let other = home.home.appendingPathComponent("Projects/other", isDirectory: true)
        try FileManager.default.createDirectory(at: other, withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(at: home.home.appendingPathComponent("alias"), withDestinationURL: other)
        let decoy = home.home.appendingPathComponent("decoy", isDirectory: true)
        try FileManager.default.createDirectory(at: decoy, withIntermediateDirectories: true)
        let raw = home.home.path + "/alias/../decoy"
        try answerHappyPath(trash: [["path": raw, "proof": "installer_env"]])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertTrue(trash.trashed.isEmpty, "the lexical decoy is not what the candidate names")
        XCTAssertTrue(FileManager.default.fileExists(atPath: decoy.path))
        guard case .done(let left) = outcome else { return XCTFail("expected done, got \(describe(outcome))") }
        XCTAssertEqual(left, [raw])
    }

    /// A `..` whose two readings agree is trashed as its lexical path.
    func testADotDotPathThatAgreesIsTrashedAsItsLexicalPath() async throws {
        try FileManager.default.createDirectory(at: home.home.appendingPathComponent("Library"), withIntermediateDirectories: true)
        let raw = home.home.path + "/Library/../.local/share/uv/tools/ciaobot"
        try answerHappyPath(trash: [["path": raw, "proof": "installer_env"]])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "done:0")
        XCTAssertEqual(trash.trashed.map(\.path), [envRoot.path])
    }

    // MARK: Symlinked workspace and dangling candidates

    func testASymlinkedWorkspaceMatchesTheResolvedPathTheEngineWrites() async throws {
        let link = home.home.appendingPathComponent("Projects/link-ws", isDirectory: true)
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: workspace)
        runner.answers["plan"] = planJSON(action: "takeover", oldRunning: true,
                                          runtimePath: link.appendingPathComponent(".runtime").path, workspacePath: link.path)
        runner.answers["apply"] = json(["ok": true, "id": "T1", "state": "pointer_written"])
        runner.answers["finish"] = json(["ok": true, "id": "T1", "trash": [String](), "left_in_place": [String]()])
        runner.answers["finish --done"] = json(["ok": true, "id": "T1"])
        try FileManager.default.createDirectory(
            at: home.takeoverDir.appendingPathComponent("T1", isDirectory: true), withIntermediateDirectories: true)
        // The engine writes Path(workspace).resolve(), the plan names the link.
        try writeReadyRecord(workspace: workspace.resolvingSymlinksInPath().path)
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "done:0")
        XCTAssertEqual(log.count(prefix: "cmd:rollback"), 0)
    }

    func testSymlinksToTheWorkspaceAreNeverTrashed() async throws {
        let link = home.home.appendingPathComponent("Projects/link-ws", isDirectory: true)
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: workspace)
        try answerHappyPath(trash: [candidate(link), candidate(workspace), candidate(envRoot)])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(trash.trashed.map(\.path), [envRoot.path])
        guard case .done(let left) = outcome else { return XCTFail("expected done, got \(describe(outcome))") }
        XCTAssertEqual(Set(left), [link.path, workspace.path])
    }

    func testADanglingShimLinkIsTrashed() async throws {
        let shim = home.home.appendingPathComponent(".local/bin/ciao")
        try FileManager.default.createDirectory(at: shim.deletingLastPathComponent(), withIntermediateDirectories: true)
        // Its target sits in the env root that the engine trashes first, so it dangles.
        try FileManager.default.createSymbolicLink(at: shim, withDestinationURL: envRoot.appendingPathComponent("bin/ciao"))
        try answerHappyPath(trash: [candidate(shim, proof: "installer_shim")])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "done:0")
        XCTAssertEqual(trash.trashed.map(\.path), [shim.path])
    }

    // MARK: Process runner: timeouts and the process group

    private func scriptRunner(
        _ body: String, timeout: TimeInterval, exitWait: TimeInterval = TakeoverTimeouts.exitGrace
    ) throws -> FoundationTakeoverRunner {
        let script = tmp.appendingPathComponent("takeover-\(UUID().uuidString)")
        try "#!/bin/sh\n\(body)\n".write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        return FoundationTakeoverRunner(
            commandLine: { arguments in
                TakeoverCommandLine(executable: script, arguments: arguments, environment: [:], workingDirectory: self.tmp)
            },
            timeout: { _ in timeout },
            exitWait: exitWait
        )
    }

    /// A command that closes its stdout and keeps running is not waited for
    /// forever: after the exit wait its group is SIGKILLed and it reads as a lost answer.
    func testAChildThatClosesStdoutAndKeepsRunningIsKilledAfterTheExitWait() async throws {
        let runner = try scriptRunner("exec >&-\n/bin/sleep 30", timeout: 60, exitWait: 0.3)
        let started = Date()
        let result = try await runner.run(["apply", "--json"])
        let elapsed = Date().timeIntervalSince(started)
        XCTAssertLessThan(elapsed, 10, "the group is ended after the exit wait, not after the 30 s sleep")
        XCTAssertEqual(result.status, 128 + SIGKILL, "the command was ended by SIGKILL")
        XCTAssertNil(TakeoverAnswer.decode(result.json))
    }

    /// A command that exits on its own after closing stdout is reaped normally,
    /// with its own status, and is never signalled.
    func testAChildThatClosesStdoutAndExitsIsReapedWithItsOwnStatus() async throws {
        let runner = try scriptRunner("exec >&-\nexit 7", timeout: 60, exitWait: 5)
        let started = Date()
        let result = try await runner.run(["apply", "--json"])
        XCTAssertLessThan(Date().timeIntervalSince(started), 4)
        XCTAssertEqual(result.status, 7)
    }

    func testARunPastItsLimitIsEndedAndReadsAsALostAnswer() async throws {
        let runner = try scriptRunner("/bin/sleep 5", timeout: 0.3)
        let started = Date()
        let result = try await runner.run(["apply", "--json"])
        XCTAssertLessThan(Date().timeIntervalSince(started), 3, "the limit ends the command, not the sleep")
        XCTAssertNotEqual(result.status, 0)
        XCTAssertNil(TakeoverAnswer.decode(result.json))
    }

    func testARunPastItsLimitEndsItsWholeProcessGroup() async throws {
        let pidFile = tmp.appendingPathComponent("grandchild.pid")
        let runner = try scriptRunner("/bin/sleep 30 &\necho $! > \"\(pidFile.path)\"\nwait", timeout: 0.5)
        let started = Date()
        _ = try await runner.run(["apply", "--json"])
        // Ended at the limit by SIGTERM to the group; the SIGKILL fallback comes 2 s later.
        XCTAssertLessThan(Date().timeIntervalSince(started), 2, "the group must end at the limit")
        let text = try String(contentsOf: pidFile, encoding: .utf8)
        let pid = try XCTUnwrap(pid_t(text.trimmingCharacters(in: .whitespacesAndNewlines)))
        // The launcher's child is reaped with it: the group, not only the launcher, is ended.
        var gone = false
        for _ in 0..<60 where !gone {
            gone = kill(pid, 0) == -1 && errno == ESRCH
            if !gone { try await Task.sleep(nanoseconds: 50_000_000) }
        }
        XCTAssertTrue(gone, "the grandchild must not survive the command's limit")
    }

    func testALaunchFailureThrowsSoTheAnswerIsLost() async throws {
        let runner = FoundationTakeoverRunner(commandLine: { arguments in
            TakeoverCommandLine(executable: self.tmp.appendingPathComponent("no-such-launcher"), arguments: arguments,
                                environment: [:], workingDirectory: self.tmp)
        })
        do {
            _ = try await runner.run(["plan", "--json"])
            XCTFail("a launcher that cannot start must throw")
        } catch {}
    }

    func testTheCommandLimitsAreAboveTheEngineWorstCase() {
        XCTAssertEqual(TakeoverTimeouts.apply, 180)
        XCTAssertEqual(TakeoverTimeouts.rollback, 180)
        XCTAssertGreaterThanOrEqual(TakeoverTimeouts.forCommand(["apply", "--json"]), 180)
        XCTAssertGreaterThanOrEqual(TakeoverTimeouts.forCommand(["rollback", "--json"]), 180)
        XCTAssertEqual(TakeoverTimeouts.forCommand(["plan", "--json"]), 30)
        XCTAssertEqual(TakeoverTimeouts.forCommand(["finish", "--json", "--id", "T1"]), 30)
    }

    /// A child that ignores SIGTERM (and passes its ignore on to its own children)
    /// is ended by the SIGKILL that follows the grace, and reads as a lost answer.
    func testAChildThatIgnoresTermIsKilledAfterTheGrace() async throws {
        let runner = try scriptRunner("trap '' TERM\nwhile :; do /bin/sleep 0.1; done", timeout: 0.3)
        let started = Date()
        let result = try await runner.run(["apply", "--json"])
        let elapsed = Date().timeIntervalSince(started)
        XCTAssertLessThan(elapsed, 0.3 + TakeoverTimeouts.reapGrace + 4, "SIGKILL follows SIGTERM after the grace")
        XCTAssertEqual(result.status, 128 + SIGKILL, "the leader was ended by SIGKILL")
        XCTAssertNil(TakeoverAnswer.decode(result.json))
    }

    /// Output far larger than a pipe buffer is read whole, so the command is not
    /// blocked on a full pipe and its answer decodes.
    func testLargeOutputIsReadWholeThroughTheRunner() async throws {
        let runner = try scriptRunner(#"printf '{"ok":true,"pad":"'; head -c 300000 /dev/zero | tr '\0' a; printf '"}'"#, timeout: 20)
        let result = try await runner.run(["plan", "--json"])
        XCTAssertEqual(result.status, 0)
        XCTAssertGreaterThanOrEqual(result.json.count, 300_000)
        XCTAssertEqual(TakeoverAnswer.decode(result.json)?.ok, true)
    }

    /// A descriptor the app holds without close-on-exec is not handed to the command.
    /// The spawn sets POSIX_SPAWN_CLOEXEC_DEFAULT on every macOS the app supports,
    /// the same as NativeInstaller, so this holds unconditionally.
    func testAnInheritableDescriptorOfTheAppIsNotHandedToTheCommand() async throws {
        let leaked = open("/dev/null", O_RDONLY)
        XCTAssertGreaterThan(leaked, 2)
        defer { close(leaked) }
        XCTAssertEqual(fcntl(leaked, F_GETFD) & FD_CLOEXEC, 0, "the precondition: this descriptor is inheritable")
        let runner = try scriptRunner("if [ -e /dev/fd/\(leaked) ]; then printf leaked; else printf clean; fi", timeout: 10)
        let result = try await runner.run(["plan", "--json"])
        XCTAssertEqual(String(decoding: result.json, as: UTF8.self), "clean")
    }

    /// The watchdog's TERM fires at its deadline and the KILL one grace later.
    func testTheWatchdogSendsTermThenKillWhileTheCommandRuns() async throws {
        let sent = SignalRecorder()
        let watchdog = ProcessGroupWatchdog(pid: 1234, grace: 0.05, send: { _, signal in sent.add(signal) })
        watchdog.arm(after: 0.01)
        try await Task.sleep(nanoseconds: 400_000_000)
        XCTAssertEqual(sent.signals, [SIGTERM, SIGKILL])
    }

    /// A command that ends before its limit is never signalled, even when its
    /// deadline passes afterwards: the reap cannot be followed by a group signal.
    func testAFinishedCommandIsNeverSignalledByItsWatchdog() async throws {
        let sent = SignalRecorder()
        let watchdog = ProcessGroupWatchdog(pid: 1234, grace: 0.05, send: { _, signal in sent.add(signal) })
        watchdog.arm(after: 0.05)
        watchdog.finish()
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(sent.signals, [])
    }

    /// The race the lock closes: the command ends after its TERM and before the
    /// KILL is due. The KILL must not follow, and a KILL that is already due
    /// after `finish()` sends nothing.
    func testACommandThatEndsAfterTermNeverGetsTheKill() async throws {
        let sent = SignalRecorder()
        let watchdog = ProcessGroupWatchdog(pid: 1234, grace: 0.2, send: { _, signal in sent.add(signal) })
        watchdog.arm(after: 0.01)
        try await Task.sleep(nanoseconds: 80_000_000)
        XCTAssertEqual(sent.signals, [SIGTERM])
        watchdog.finish()
        watchdog.forceKill()
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(sent.signals, [SIGTERM], "no SIGKILL once the command has finished")
    }

    // MARK: Copy for each failure

    func testTheStateUnknownCopyNeverClaimsTheOldEngineStaysPut() {
        XCTAssertFalse(TakeoverCopy.stateUnknown.contains("stays in place"))
        XCTAssertEqual(TakeoverCopy.notice(for: .refused(code: "state_unknown", oldOrigin: nil)), TakeoverCopy.stateUnknown)
        XCTAssertEqual(TakeoverCopy.notice(for: .refused(code: "plan_failed", oldOrigin: nil)), TakeoverCopy.stateUnknown)
    }

    func testATransientRefusalGetsTheRetryCopy() {
        XCTAssertEqual(TakeoverCopy.refused(code: "blockers_unavailable"), TakeoverCopy.retry)
        XCTAssertEqual(TakeoverCopy.notice(for: .refused(code: "takeover_failed", oldOrigin: nil)), TakeoverCopy.retry)
    }

    func testEveryDeliberateRefusalHasItsOwnCopyAndTheTablesAgree() {
        for code in TakeoverRefusal.deliberate {
            XCTAssertNotEqual(TakeoverCopy.refused(code: code), TakeoverCopy.retry, code)
        }
        XCTAssertTrue(TakeoverRefusal.deliberate.isDisjoint(with: TakeoverRefusal.stateUnknown))
        XCTAssertTrue(EngineTakeover.isDeliberateRefusal("downgrade"))
        XCTAssertFalse(EngineTakeover.isDeliberateRefusal("plan_failed"))
        XCTAssertFalse(EngineTakeover.isDeliberateRefusal("blockers_unavailable"))
    }

    private func writeJournalOnDisk(_ id: String) {
        try? writeJournal(id)
    }
}

private extension Array where Element: Equatable {
    /// True when `needle` appears as a contiguous run.
    func containsSequence(_ needle: [Element]) -> Bool {
        guard !needle.isEmpty, count >= needle.count else { return false }
        return (0...(count - needle.count)).contains { start in
            Array(self[start..<(start + needle.count)]) == needle
        }
    }
}
