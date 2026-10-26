// Ciaobot Server native host.
//
// This is the small, persistent macOS host that launchd will eventually own as
// the final job-group member (#1009, child A of #1008). It does exactly three
// things, and nothing else:
//
//   Ciaobot Server serve --python /absolute/interpreter
//       Launch exactly `[python, -I, -m, ciao.cli, supervise]` as a child and
//       stay alive until that child exits, then mirror its status. The Python
//       supervisor still owns restart/backoff and the engine descendants; the
//       host does not restart or relaunch anything.
//
//   Ciaobot Server request-accessibility
//       Ask macOS once, through the public AX trust prompt, whether this host
//       may control the desktop. The prompt is asynchronous, so the process
//       stays alive for five seconds and then exits. It never grants anything
//       by itself and never reads, clicks, types, or scripts.
//
//   Ciaobot Server run-move --python /absolute/interpreter --operation <id>
//       --state-dir /absolute/dir
//       Launch exactly `[python, -I, -m, ciao.workspace_move, run, --operation,
//       <id>, --state-dir, <dir>]` as a child and mirror its status. The
//       workspace move must run as a descendant of this host, so a macOS
//       folder grant the user gave the host covers the move too (#1658).
//       The command is fixed: only the interpreter, the operation id and the
//       state directory are caller-supplied, and all three are validated.
//
// There is no network, IPC, arbitrary-command, shell, exec-replacement or
// desktop-control surface. The host never spawns a shell and never sets a
// custom environment knob. The child inherits cwd, environment and stdio.
//
// The child is spawned with public `posix_spawn`, NOT `Foundation.Process`:
// Process makes the child the leader of a new process group, which would take
// the supervisor out of launchd's job group and orphan the engine (and the
// OpenCode server) when the host dies. With `posix_spawn` the child keeps the
// host's process group and session, so launchd's cleanup of the job group still
// reaches the supervisor and its dies_with_engine helpers. The host therefore
// never calls `killpg` itself: a stop is forwarded to the tracked,
// still-unreaped child PID only, and launchd is the final group owner.
//
// Protocol revision 2. Invalid argv exits 2 before AppKit is set up and before
// any permission call is reachable.

import AppKit
import ApplicationServices
import Darwin
import Foundation

/// Wire/contract revision of the fixed host protocol. Bump only with the
/// builder's `CiaobotServerHostProtocol` plist key and the docs.
let HOST_PROTOCOL_REVISION = 2

/// How long the host waits after forwarding a stop before it escalates to
/// SIGKILL, and how long the asynchronous accessibility prompt is retained.
/// The stop grace is deliberately longer than the Python supervisor's own 30 s
/// so the child's own grace period wins and the host is the outer bound. The
/// launchd job's `ExitTimeOut` must be set above this (45 s) so launchd does
/// not sweep the job group out from under the host's own stop.
let STOP_GRACE_SECONDS = 35.0
let REQUEST_LIFETIME_SECONDS = 5.0

/// The fixed arguments appended to the caller-provided interpreter. There is no
/// seam for additional engine arguments, scripts or shell strings.
let SUPERVISOR_ARGUMENTS = ["-I", "-m", "ciao.cli", "supervise"]

/// The fixed arguments of the workspace move. The operation id and the state
/// directory are the only caller-supplied parts, and they are validated before
/// the child is built.
let MOVE_ARGUMENTS = ["-I", "-m", "ciao.workspace_move", "run", "--operation"]
let MOVE_STATE_DIR_ARGUMENT = "--state-dir"

let EXIT_USAGE: Int32 = 2
let EXIT_LAUNCH_FAILURE: Int32 = 1

/// The fixed operations this host exposes.
enum Operation {
    case serve(python: String)
    case requestAccessibility
    case runMove(python: String, operation: String, stateDir: String)
}

func writeStderr(_ message: String) {
    FileHandle.standardError.write(Data(message.utf8))
}

func errnoMessage(_ code: Int32) -> String {
    guard let text = strerror(code) else { return "errno \(code)" }
    return String(cString: text)
}

func usage() -> Never {
    writeStderr(
        "Usage: Ciaobot Server serve --python /absolute/interpreter\n"
            + "       Ciaobot Server request-accessibility\n"
            + "       Ciaobot Server run-move --python /absolute/interpreter"
            + " --operation <id> --state-dir /absolute/dir\n"
    )
    exit(EXIT_USAGE)
}

/// True when `path` is an absolute, normalized path: no `.`, `..`, `//` or
/// trailing `/`.
///
/// The engine's own contract refuses such a path for the interpreter
/// (`ciao.server_host._absolute_posix`), and the host refuses it too, so the
/// two agree on what a valid path is. A dot *directory* (`.local`) is ordinary
/// and is not a dot alias: only the `.` and `..` components are refused.
///
/// The engine's contract also refuses a backslash, because on Windows it is a
/// separator and on macOS it is an ordinary (and suspicious) filename byte.
/// The host runs only on macOS, where a backslash is not a separator, so it is
/// not refused here; a path carrying one still names exactly one file, and the
/// argv carries no shell to expand it.
func isNormalizedAbsolutePath(_ path: String) -> Bool {
    guard path.hasPrefix("/") else { return false }
    if path.hasSuffix("/") { return false }
    // The leading "/" is the root, so the components start after it.
    let components = path.dropFirst().split(separator: "/", omittingEmptySubsequences: false)
    for component in components {
        if component == "." || component == ".." { return false }
        if component.isEmpty { return false }
    }
    return true
}

/// True when `path` is an absolute path naming an existing, executable,
/// non-directory file. Symlinks and dot aliases are resolved by the filesystem
/// as ordinary executable paths; nothing here forbids them.
func isRunnableExecutable(_ path: String) -> Bool {
    guard isNormalizedAbsolutePath(path) else { return false }
    var isDirectory: ObjCBool = false
    guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory),
        !isDirectory.boolValue
    else { return false }
    return FileManager.default.isExecutableFile(atPath: path)
}

/// True when `value` is a plain operation id: it starts with a letter or a
/// digit and continues with letters, digits, `_` or `-`.
///
/// The engine mints these ids as a timestamp plus six hex characters
/// (`ciao.workspace_move`), so the class covers every id it can produce and
/// nothing else. A leading dash is refused on purpose: the id is handed to
/// the child as an argument value, and an id shaped like `--state-dir` would
/// make the child's own argument parser read it as an option.
func isPlainIdentifier(_ value: String) -> Bool {
    guard let first = value.first, first.isLetter || first.isNumber else { return false }
    for character in value.dropFirst() {
        guard character.isLetter || character.isNumber || character == "_" || character == "-" else {
            return false
        }
    }
    return true
}

/// Parse the fixed invocation strictly. Rejects repeated, unknown and extra
/// arguments, a relative/empty path, and a missing or non-executable target.
/// Exits 2 before AppKit setup or any permission call when the argv is invalid.
func parseInvocation(_ arguments: [String]) -> Operation {
    let args = Array(arguments.dropFirst())
    if args == ["request-accessibility"] {
        return .requestAccessibility
    }
    if args.first == "run-move" {
        guard args.count == 7, args[1] == "--python", args[3] == "--operation",
            args[5] == MOVE_STATE_DIR_ARGUMENT
        else {
            usage()
        }
        let python = args[2]
        guard isRunnableExecutable(python) else {
            writeStderr("Ciaobot Server: --python must be an absolute existing executable\n")
            exit(EXIT_USAGE)
        }
        let operation = args[4]
        guard isPlainIdentifier(operation) else {
            writeStderr("Ciaobot Server: --operation must be a plain identifier\n")
            exit(EXIT_USAGE)
        }
        let stateDir = args[6]
        guard isNormalizedAbsolutePath(stateDir) else {
            writeStderr(
                "Ciaobot Server: --state-dir must be an absolute path without '..'\n"
            )
            exit(EXIT_USAGE)
        }
        return .runMove(python: python, operation: operation, stateDir: stateDir)
    }
    guard args.count == 3, args[0] == "serve", args[1] == "--python" else {
        usage()
    }
    let python = args[2]
    guard isRunnableExecutable(python) else {
        writeStderr("Ciaobot Server: --python must be an absolute existing executable\n")
        exit(EXIT_USAGE)
    }
    return .serve(python: python)
}

/// The outcome of preparing and running `posix_spawn`.
enum SpawnOutcome {
    case spawned(pid_t)
    case failed(String)
}

/// Spawn a fixed child with the public `posix_spawn` API.
///
/// The child's argv is the interpreter followed by `arguments`, which are fixed
/// pieces plus the values the parser already validated. There is no seam for a
/// caller-supplied command, a shell string or an extra argument.
///
/// No `POSIX_SPAWN_SETPGROUP`: the child stays in the host's process group and
/// session, so launchd's final job-group cleanup still reaches the supervisor
/// and its `dies_with_engine` helpers.
/// `POSIX_SPAWN_SETSIGDEF` resets SIGTERM/SIGINT (which the host ignores) to
/// their defaults, `POSIX_SPAWN_SETSIGMASK` clears the inherited mask, and
/// `POSIX_SPAWN_CLOEXEC_DEFAULT` closes every descriptor except the explicitly
/// re-inherited stdio. Every fallible call is checked; the spawn attributes,
/// file actions and the C-string argv are released on every path.
func spawnFixedChild(python: String, arguments: [String]) -> SpawnOutcome {
    var attributes: posix_spawnattr_t? = nil
    var fileActions: posix_spawn_file_actions_t? = nil
    var argv: [UnsafeMutablePointer<CChar>?] = []
    defer {
        for pointer in argv {
            if let pointer = pointer { free(pointer) }
        }
        if attributes != nil { posix_spawnattr_destroy(&attributes) }
        if fileActions != nil { posix_spawn_file_actions_destroy(&fileActions) }
    }

    var result = posix_spawnattr_init(&attributes)
    if result != 0 { return .failed("posix_spawnattr_init: \(errnoMessage(result))") }
    result = posix_spawn_file_actions_init(&fileActions)
    if result != 0 { return .failed("posix_spawn_file_actions_init: \(errnoMessage(result))") }

    let flags = Int16(
        POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_CLOEXEC_DEFAULT
    )
    result = posix_spawnattr_setflags(&attributes, flags)
    if result != 0 { return .failed("posix_spawnattr_setflags: \(errnoMessage(result))") }

    var defaultSignals = sigset_t()
    sigemptyset(&defaultSignals)
    sigaddset(&defaultSignals, SIGTERM)
    sigaddset(&defaultSignals, SIGINT)
    result = posix_spawnattr_setsigdefault(&attributes, &defaultSignals)
    if result != 0 { return .failed("posix_spawnattr_setsigdefault: \(errnoMessage(result))") }

    var clearedMask = sigset_t()
    sigemptyset(&clearedMask)
    result = posix_spawnattr_setsigmask(&attributes, &clearedMask)
    if result != 0 { return .failed("posix_spawnattr_setsigmask: \(errnoMessage(result))") }

    for descriptor: Int32 in [0, 1, 2] {
        result = posix_spawn_file_actions_addinherit_np(&fileActions, descriptor)
        if result != 0 {
            return .failed(
                "posix_spawn_file_actions_addinherit_np(\(descriptor)): \(errnoMessage(result))"
            )
        }
    }

    for part in [python] + arguments {
        guard let duplicate = strdup(part) else {
            return .failed("could not allocate the child argv")
        }
        argv.append(duplicate)
    }
    argv.append(nil)

    var pid: pid_t = 0
    result = posix_spawn(&pid, python, &fileActions, &attributes, &argv, environ)
    if result != 0 { return .failed("posix_spawn: \(errnoMessage(result))") }
    return .spawned(pid)
}

/// Spawn the fixed supervisor child.
func spawnSupervisor(python: String) -> SpawnOutcome {
    spawnFixedChild(python: python, arguments: SUPERVISOR_ARGUMENTS)
}

/// Spawn the fixed workspace-move child.
///
/// The argv is the fixed move command with the two validated values: the
/// operation id and the state directory. Nothing else is caller-supplied.
func spawnMove(python: String, operation: String, stateDir: String) -> SpawnOutcome {
    spawnFixedChild(
        python: python,
        arguments: MOVE_ARGUMENTS + [operation, MOVE_STATE_DIR_ARGUMENT, stateDir]
    )
}

/// Owns the serve and move branches: launch the fixed child, keep the event
/// loop responsive, forward one stop, escalate once, reap and mirror the exit.
///
/// The two branches differ only in what they spawn and in the wording of the
/// launch failure, so the child lifecycle lives here once: the same stop
/// handling, the same escalation, the same reaping and the same exit-status
/// mapping. The move child is a descendant of the host for the same reason the
/// supervisor is: a macOS folder grant follows the responsible process.
final class ChildController {
    /// What to spawn, and how to name it in a launch failure.
    private let python: String
    private let spawn: () -> SpawnOutcome
    private let childName: String
    /// The child PID, held from spawn until it has been reaped. Holding the
    /// unreaped PID is what makes "never signal a recycled PID" true: the
    /// kernel cannot reuse the PID while this process still owns the zombie.
    private var childPid: pid_t?
    private var exitSource: DispatchSourceProcess?
    private var signalSources: [DispatchSourceSignal] = []
    private var escalation: DispatchSourceTimer?
    private var stopRequested = false
    /// A private kqueue that counts SIGTERM/SIGINT from the moment `begin`
    /// registers it. The dispatch signal sources deliver on the main queue, so
    /// a stop that arrives after the launch block is queued would run after it;
    /// this queue is read synchronously by `launch` instead.
    private var stopQueue: Int32 = -1

    init(python: String, childName: String, spawn: @escaping () -> SpawnOutcome) {
        self.python = python
        self.childName = childName
        self.spawn = spawn
    }

    /// Install the stop handlers and schedule the child launch on the event
    /// loop. The launch is deferred so a stop that arrives during startup is
    /// seen before any child exists.
    ///
    /// A signal source's handler is queued on the main queue when the signal
    /// arrives, behind anything already queued there. A single deferred launch
    /// is queued before AppKit starts, so it would run ahead of a stop that
    /// arrived during AppKit setup and launch the child anyway. The launch is
    /// therefore queued again from the first main-queue turn: by then every stop
    /// delivered during startup is already queued ahead of it.
    func begin() {
        // Registered before the signal sources and before SIG_IGN, so no stop
        // that either of them sees can be missed here. Every stop signal
        // sent from now on stays readable until `launch` looks.
        stopQueue = kqueue()
        if stopQueue >= 0 {
            for sig in [SIGTERM, SIGINT] {
                var change = kevent(
                    ident: UInt(sig), filter: Int16(EVFILT_SIGNAL),
                    flags: UInt16(EV_ADD | EV_ENABLE), fflags: 0, data: 0,
                    udata: nil
                )
                _ = kevent(stopQueue, &change, 1, nil, 0, nil)
            }
        }
        for sig in [SIGTERM, SIGINT] {
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { [weak self] in self?.handleStop() }
            source.resume()
            // Register the source BEFORE ignoring the signal's disposition, so
            // the signal is never both ignored and unobserved. Until SIG_IGN a
            // signal takes the default action and ends the host, which is safe
            // because no child exists yet; from SIG_IGN on, the kqueue source
            // records the arrival even though the disposition is ignore.
            // Ignoring the disposition stops the default action (terminate)
            // from racing the handler.
            signal(sig, SIG_IGN)
            signalSources.append(source)
        }
        DispatchQueue.main.async { [weak self] in
            DispatchQueue.main.async { self?.launch() }
        }
    }

    /// True when a stop signal has arrived since `begin`, whether or not its
    /// dispatch source has run its handler yet. A non-blocking poll.
    private func stopSignalArrived() -> Bool {
        guard stopQueue >= 0 else { return false }
        var event = kevent()
        var immediately = timespec(tv_sec: 0, tv_nsec: 0)
        return kevent(stopQueue, nil, 0, &event, 1, &immediately) > 0
    }

    /// A stop whose handler already ran exits the host from `handleStop`. A stop
    /// that arrived after this block was queued has not run its handler yet
    /// (the handler would run after this block), so it is read from the stop
    /// queue first: the launch decision always sees every stop already sent.
    private func launch() {
        if stopSignalArrived() {
            handleStop()
            return
        }
        switch spawn() {
        case .failed(let message):
            writeStderr(
                "Ciaobot Server: could not launch \(childName) \(python): \(message)\n"
            )
            exit(EXIT_LAUNCH_FAILURE)
        case .spawned(let pid):
            childPid = pid
            let source = DispatchSource.makeProcessSource(
                identifier: pid, eventMask: .exit, queue: .main
            )
            source.setEventHandler { [weak self] in self?.reap(pid, blocking: true) }
            source.resume()
            exitSource = source
            // A child that exited before the source was armed is still a
            // zombie; reap it now so a very fast exit is never missed.
            reap(pid, blocking: false)
        }
    }

    /// Collect the child's status exactly once, whether the dispatch exit
    /// source fired or an immediate `WNOHANG` check found it already gone.
    private func reap(_ pid: pid_t, blocking: Bool) {
        guard childPid == pid else { return }
        var status: Int32 = 0
        var result: pid_t
        let options = blocking ? 0 : WNOHANG
        repeat {
            result = waitpid(pid, &status, options)
        } while result == -1 && errno == EINTR
        if result == pid {
            collect(status: status)
        }
    }

    /// A stop requested by a signal source or by the replaced Quit Apple Event
    /// handler. Internal so the delegate's quit handler can reach it.
    func handleStop() {
        if stopRequested { return }
        stopRequested = true
        guard let pid = childPid else {
            // A stop during startup: no child was ever launched.
            exit(0)
        }
        // Forward to the tracked, still-unreaped PID only. Never `killpg`: the
        // host shares launchd's job group, so signalling the group here would
        // also signal the host itself and every sibling the supervisor started.
        _ = kill(pid, SIGTERM)
        armEscalation(pid)
    }

    /// Arm exactly one SIGKILL escalation for the child we forwarded to. The
    /// handler re-checks the still-unreaped PID, so the kill can never target a
    /// recycled PID or a replaced child. The process-exit source reaps the
    /// killed child, so the main queue never blocks in `waitpid` here. It does
    /// not kill the shared group: launchd's final job-group cleanup is what
    /// reaches the descendants after the host exits.
    private func armEscalation(_ pid: pid_t) {
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + STOP_GRACE_SECONDS)
        timer.setEventHandler { [weak self] in
            guard let self = self, self.childPid == pid else { return }
            _ = kill(pid, SIGKILL)
        }
        timer.resume()
        escalation = timer
    }

    private func collect(status: Int32) {
        escalation?.cancel()
        escalation = nil
        exitSource?.cancel()
        exitSource = nil
        childPid = nil
        exit(exitCode(status))
    }

    /// The status the host exits with. A requested stop is a clean stop — 0 —
    /// for the codes the real supervisor answers a stop with: exit 0, exit 130
    /// (the supervisor saw the stop before its first launch or during its
    /// restart backoff) and exit 143 (the engine it terminated died by the
    /// forwarded SIGTERM), plus death by the forwarded SIGTERM itself. Every
    /// other outcome (a stalled child killed by the escalation, any other
    /// non-zero exit, a crash) is preserved so launchd and logs see the failed
    /// shutdown instead of a false success.
    private func exitCode(_ status: Int32) -> Int32 {
        let signalNumber = status & 0x7f
        let exitedNormally = signalNumber == 0
        let exitStatus = (status >> 8) & 0xff
        if stopRequested {
            if exitedNormally && (exitStatus == 0 || exitStatus == 130 || exitStatus == 143) {
                return 0
            }
            if !exitedNormally && signalNumber == SIGTERM { return 0 }
        }
        if exitedNormally { return exitStatus }
        return 128 + signalNumber
    }
}

/// The request branch's only permission call. Kept in one named function so the
/// serve branch can never reach it, and so a source-level check can prove it.
func requestAccessibilityTrust() {
    let options = [
        kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true
    ] as CFDictionary
    _ = AXIsProcessTrustedWithOptions(options)
}

func runRequestAccessibility() -> Never {
    requestAccessibilityTrust()
    // The settings prompt is asynchronous; keep the responsible process alive
    // long enough for macOS to register and present it, then exit.
    DispatchQueue.main.asyncAfter(deadline: .now() + REQUEST_LIFETIME_SECONDS) {
        exit(0)
    }
    NSApplication.shared.run()
    exit(0)
}

/// Replaces AppKit's default Quit Apple Event handler so a graceful macOS quit
/// (logout, restart, shutdown, or `quit app "Ciaobot Server"`) forwards a stop
/// to the supervisor instead of calling `terminate:`/`exit(0)` and orphaning it.
final class QuitEventHandler: NSObject {
    private let onQuit: () -> Void

    init(onQuit: @escaping () -> Void) {
        self.onQuit = onQuit
    }

    @objc func handle(_ event: NSAppleEventDescriptor, withReply reply: NSAppleEventDescriptor) {
        onQuit()
    }
}

/// Pins the quit handler and installs it while AppKit is still launching.
/// `NSAppleEventManager.setEventHandler` replaces the default
/// `kAEQuitApplication` handler, so `onQuit` runs instead of
/// `NSApplication.terminate(_:)`. It is installed in `willFinishLaunching`, not
/// `didFinishLaunching`: the deferred child launch can run on the main queue
/// before `didFinishLaunching` is delivered, and a quit in that gap would hit
/// AppKit's default handler and exit without forwarding the stop.
/// `NSApp.delegate` is weak, so the caller pins this object for the whole
/// event loop.
final class HostApplicationDelegate: NSObject, NSApplicationDelegate {
    private let quitHandler: QuitEventHandler

    init(onQuit: @escaping () -> Void) {
        self.quitHandler = QuitEventHandler(onQuit: onQuit)
    }

    func applicationWillFinishLaunching(_ notification: Notification) {
        NSAppleEventManager.shared().setEventHandler(
            quitHandler,
            andSelector: #selector(QuitEventHandler.handle(_:withReply:)),
            forEventClass: AEEventClass(kCoreEventClass),
            andEventID: AEEventID(kAEQuitApplication)
        )
    }
}

/// Run a fixed child under the host's event loop, mirroring its exit status.
func runChild(
    python: String,
    childName: String,
    spawn: @escaping () -> SpawnOutcome
) -> Never {
    // Install stop handlers and defer the launch BEFORE AppKit is set up, so a
    // stop that lands during startup is seen and no child is ever launched.
    let controller = ChildController(python: python, childName: childName, spawn: spawn)
    controller.begin()
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let delegate = HostApplicationDelegate(onQuit: { controller.handleStop() })
    app.delegate = delegate
    // Every handler holds the controller weakly, and AppKit's delegate is weak,
    // so pin both for the whole event loop.
    withExtendedLifetime((controller, delegate)) {
        app.run()
    }
    exit(0)
}

func runServe(python: String) -> Never {
    runChild(python: python, childName: "supervisor") {
        spawnSupervisor(python: python)
    }
}

/// The move branch: the workspace move runs as a descendant of the host, so a
/// macOS folder grant the user gave the host covers the move too (#1658). The
/// child is the fixed move command and nothing else.
func runMove(python: String, operation: String, stateDir: String) -> Never {
    runChild(python: python, childName: "workspace move") {
        spawnMove(python: python, operation: operation, stateDir: stateDir)
    }
}

let operation = parseInvocation(CommandLine.arguments)
switch operation {
case .serve(let python):
    runServe(python: python)
case .requestAccessibility:
    runRequestAccessibility()
case .runMove(let python, let moveOperation, let stateDir):
    runMove(python: python, operation: moveOperation, stateDir: stateDir)
}
