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
        if failing.contains(key) { throw CocoaError(.executableLoad) }
        var answer = answers[key] ?? "{}"
        if var queue = sequences[key], !queue.isEmpty {
            answer = queue.removeFirst()
            sequences[key] = queue
        }
        return (0, Data(answer.utf8))
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
                       "Ciaobot could not finish moving into the app, so your existing Ciaobot was restarted unchanged.")
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

    func testALostFinishAnswerRollsBackBeforeAnythingIsTrashed() async throws {
        try answerHappyPath(trash: [candidate(envRoot)])
        runner.failing = ["finish"]
        runner.answers["rollback"] = json(["ok": true, "id": "T1", "state": "rolled_back"])
        let outcome = await makeTakeover().runIfNeeded()
        XCTAssertEqual(describe(outcome), "rolledBack:not_healthy:\(oldOriginText)")
        XCTAssertTrue(trash.trashed.isEmpty)
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

    private func scriptRunner(_ body: String, timeout: TimeInterval) throws -> FoundationTakeoverRunner {
        let script = tmp.appendingPathComponent("takeover-\(UUID().uuidString)")
        try "#!/bin/sh\n\(body)\n".write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        return FoundationTakeoverRunner(
            commandLine: { arguments in
                TakeoverCommandLine(executable: script, arguments: arguments, environment: [:], workingDirectory: self.tmp)
            },
            timeout: { _ in timeout }
        )
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
        XCTAssertGreaterThanOrEqual(TakeoverTimeouts.forCommand(["apply", "--json"]), 150)
        XCTAssertGreaterThanOrEqual(TakeoverTimeouts.forCommand(["rollback", "--json"]), 150)
        XCTAssertEqual(TakeoverTimeouts.forCommand(["plan", "--json"]), 30)
        XCTAssertEqual(TakeoverTimeouts.forCommand(["finish", "--json", "--id", "T1"]), 30)
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
