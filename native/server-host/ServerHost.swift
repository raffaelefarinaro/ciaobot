// Ciaobot Server native host.
//
// This is the small, persistent macOS host that launchd will eventually own as
// the final job-group member (#1009, child A of #1008). It does exactly two
// things, and nothing else:
//
//   CiaobotServerHost serve --python /absolute/interpreter
//       Launch exactly `[python, -I, -m, ciao.cli, supervise]` as a child and
//       stay alive until that child exits, then mirror its status. The Python
//       supervisor still owns restart/backoff and the engine descendants; the
//       host does not restart or relaunch anything.
//
//   CiaobotServerHost request-accessibility
//       Ask macOS once, through the public AX trust prompt, whether this host
//       may control the desktop. The prompt is asynchronous, so the process
//       stays alive for five seconds and then exits. It never grants anything
//       by itself and never reads, clicks, types, or scripts.
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
// reaches every descendant. The host therefore never calls `killpg` itself: a
// stop is forwarded to the tracked, still-unreaped child PID only, and launchd
// is the final group owner.
//
// Protocol revision 1. Invalid argv exits 2 before AppKit is set up and before
// any permission call is reachable.

import AppKit
import ApplicationServices
import Darwin
import Foundation

/// Wire/contract revision of the fixed host protocol. Bump only with the
/// builder's `CiaobotServerHostProtocol` plist key and the docs.
let HOST_PROTOCOL_REVISION = 1

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

let EXIT_USAGE: Int32 = 2
let EXIT_LAUNCH_FAILURE: Int32 = 1

/// The fixed operations this host exposes.
enum Operation {
    case serve(python: String)
    case requestAccessibility
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
        "Usage: CiaobotServerHost serve --python /absolute/interpreter\n"
            + "       CiaobotServerHost request-accessibility\n"
    )
    exit(EXIT_USAGE)
}

/// True when `path` is an absolute path naming an existing, executable,
/// non-directory file. Symlinks and dot aliases are resolved by the filesystem
/// as ordinary executable paths; nothing here forbids them.
func isRunnableExecutable(_ path: String) -> Bool {
    guard path.hasPrefix("/") else { return false }
    var isDirectory: ObjCBool = false
    guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory),
        !isDirectory.boolValue
    else { return false }
    return FileManager.default.isExecutableFile(atPath: path)
}

/// Parse the fixed invocation strictly. Rejects repeated, unknown and extra
/// arguments, a relative/empty path, and a missing or non-executable target.
/// Exits 2 before AppKit setup or any permission call when the argv is invalid.
func parseInvocation(_ arguments: [String]) -> Operation {
    let args = Array(arguments.dropFirst())
    if args == ["request-accessibility"] {
        return .requestAccessibility
    }
    guard args.count == 3, args[0] == "serve", args[1] == "--python" else {
        usage()
    }
    let python = args[2]
    guard isRunnableExecutable(python) else {
        writeStderr("CiaobotServerHost: --python must be an absolute existing executable\n")
        exit(EXIT_USAGE)
    }
    return .serve(python: python)
}

/// The outcome of preparing and running `posix_spawn`.
enum SpawnOutcome {
    case spawned(pid_t)
    case failed(String)
}

/// Spawn the fixed supervisor with the public `posix_spawn` API.
///
/// No `POSIX_SPAWN_SETPGROUP`: the child stays in the host's process group and
/// session, so launchd's final job-group cleanup still reaches the whole tree.
/// `POSIX_SPAWN_SETSIGDEF` resets SIGTERM/SIGINT (which the host ignores) to
/// their defaults, `POSIX_SPAWN_SETSIGMASK` clears the inherited mask, and
/// `POSIX_SPAWN_CLOEXEC_DEFAULT` closes every descriptor except the explicitly
/// re-inherited stdio. Every fallible call is checked; the spawn attributes,
/// file actions and the C-string argv are released on every path.
func spawnSupervisor(python: String) -> SpawnOutcome {
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

    for part in [python] + SUPERVISOR_ARGUMENTS {
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

/// Owns the serve branch: launch the fixed child, keep the event loop
/// responsive, forward one stop, escalate once, reap and mirror the exit.
final class ServeController {
    private let python: String
    /// The child PID, held from spawn until it has been reaped. Holding the
    /// unreaped PID is what makes "never signal a recycled PID" true: the
    /// kernel cannot reuse the PID while this process still owns the zombie.
    private var childPid: pid_t?
    private var exitSource: DispatchSourceProcess?
    private var signalSources: [DispatchSourceSignal] = []
    private var escalation: DispatchSourceTimer?
    private var stopRequested = false

    init(python: String) {
        self.python = python
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
        for sig in [SIGTERM, SIGINT] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { [weak self] in self?.handleStop() }
            source.resume()
            signalSources.append(source)
        }
        DispatchQueue.main.async { [weak self] in
            DispatchQueue.main.async { self?.launch() }
        }
    }

    /// A stop that lands before this runs exits the host from `handleStop`, so
    /// reaching here means no stop has been requested yet.
    private func launch() {
        switch spawnSupervisor(python: python) {
        case .failed(let message):
            writeStderr(
                "CiaobotServerHost: could not launch supervisor \(python): \(message)\n"
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

    private func handleStop() {
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
    /// only when the child exited 0 or died by the forwarded SIGTERM. Every
    /// other outcome (a stalled child killed by the escalation, a non-zero exit
    /// after the stop, a crash) is preserved so launchd and logs see the failed
    /// shutdown instead of a false success.
    private func exitCode(_ status: Int32) -> Int32 {
        let signalNumber = status & 0x7f
        let exitedNormally = signalNumber == 0
        let exitStatus = (status >> 8) & 0xff
        if stopRequested {
            if exitedNormally && exitStatus == 0 { return 0 }
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

func runServe(python: String) -> Never {
    // Install stop handlers and defer the launch BEFORE AppKit is set up, so a
    // stop that lands during startup is seen and no child is ever launched.
    let controller = ServeController(python: python)
    controller.begin()
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    // Every handler holds the controller weakly, and Swift may release a local
    // after its last use, so pin it for the whole event loop.
    withExtendedLifetime(controller) {
        app.run()
    }
    exit(0)
}

let operation = parseInvocation(CommandLine.arguments)
switch operation {
case .serve(let python):
    runServe(python: python)
case .requestAccessibility:
    runRequestAccessibility()
}
