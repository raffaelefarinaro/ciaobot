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
/// so the child's own grace period wins and the host is the outer bound.
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

/// Owns the serve branch: launch the fixed child, keep the event loop
/// responsive, forward one stop, escalate once, reap and mirror the exit.
final class ServeController {
    private let python: String
    private var process: Process?
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
            DispatchQueue.main.async { self?.launchIfNeeded() }
        }
    }

    private func launchIfNeeded() {
        // A stop that landed during startup has already exited the host through
        // handleStop(), which runs on this same main queue.
        let child = Process()
        child.executableURL = URL(fileURLWithPath: python)
        child.arguments = SUPERVISOR_ARGUMENTS
        // cwd, environment and stdio are deliberately left unset: the child
        // inherits all three. Nothing is captured, buffered, rotated or deleted.
        child.terminationHandler = { [weak self] exited in
            DispatchQueue.main.async { self?.childExited(exited) }
        }
        do {
            try child.run()
        } catch {
            writeStderr(
                "CiaobotServerHost: could not launch supervisor \(python): \(error)\n"
            )
            finish(EXIT_LAUNCH_FAILURE)
        }
        process = child
    }

    private func handleStop() {
        if stopRequested { return }
        stopRequested = true
        guard let child = process, child.isRunning else {
            // Either no child was launched yet (a stop during startup: nothing is
            // ever launched) or it is already gone; there is nothing to forward
            // and no stale pid to signal.
            finish(0)
        }
        kill(child.processIdentifier, SIGTERM)
        armEscalation(for: child.processIdentifier)
    }

    /// Arm exactly one SIGKILL escalation for the child we forwarded to. The
    /// handler re-checks the pid and liveness, so a recycled pid is never
    /// signalled and a replaced child is never killed by a stale timer.
    ///
    /// Foundation.Process starts the child as the leader of its own process
    /// group, and the supervisor keeps the engine in that group, so the kill
    /// targets the whole group: signalling the pid alone would orphan the
    /// engine, which launchd's cleanup of the host's group can never reach.
    private func armEscalation(for pid: pid_t) {
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + STOP_GRACE_SECONDS)
        timer.setEventHandler { [weak self] in
            guard let self = self, let child = self.process,
                child.processIdentifier == pid, child.isRunning
            else { return }
            killpg(pid, SIGKILL)
        }
        timer.resume()
        escalation = timer
    }

    private func childExited(_ child: Process) {
        escalation?.cancel()
        escalation = nil
        process = nil
        let code: Int32
        if stopRequested {
            // A stop was requested and the child was terminated normally or by
            // the signal we forwarded; report a clean service stop.
            code = 0
        } else if child.terminationReason == .uncaughtSignal {
            code = 128 + child.terminationStatus
        } else {
            code = child.terminationStatus
        }
        finish(code)
    }

    private func finish(_ code: Int32) -> Never {
        // Process reaps its child before terminationHandler runs, so the child
        // is already reaped by the time we get here.
        exit(code)
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
    app.run()
    exit(0)
}

let operation = parseInvocation(CommandLine.arguments)
switch operation {
case .serve(let python):
    runServe(python: python)
case .requestAccessibility:
    runRequestAccessibility()
}
