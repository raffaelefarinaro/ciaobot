// Ciaobot Server Host experiment: a native, ad-hoc-signed accessory process that
// proves whether desktop permission requests are attributed to a recognizable
// Ciaobot Server host or to the Python child it spawns.
//
// This is a developer-operated spike. It is not a production launcher, daemon,
// IPC listener, or arbitrary-command broker. It never requests a grant in the
// `native` or child modes, never reads text/value/window titles/screenshots, and
// never executes through a shell. Everything it does is a fixed, read-only probe.
//
// Modes:
//   request   -> AXIsProcessTrustedWithOptions(prompt: true), five-second lifetime
//   native    -> read the focused UI element's role via the AX API
//   python    -> spawn the Python child sidecar in its Python-AX mode
//   osascript -> spawn the Python child sidecar in its osascript mode
//
// The Python child lives at the FIXED sidecar path <app-parent>/child/child_probe.py,
// deliberately outside the signed bundle, so updating the mutable child script
// cannot change the host executable or its signature.
//
// This file's receipt validation is a hand-maintained Swift mirror of
// child_probe.REQUIRED_FIELDS. The child also self-checks with
// parse_child_receipt before emitting, so both sides enforce the same contract.

import AppKit
import ApplicationServices
import CoreFoundation
import Darwin
import Foundation

let CHILD_DIR_NAME = "child"
let CHILD_SCRIPT_NAME = "child_probe.py"
let CHILD_TIMEOUT_SECONDS = 30.0
let CHILD_TERMINATE_GRACE_SECONDS = 2.0
let MAX_CHILD_OUTPUT_BYTES = 65536
let REQUEST_LIFETIME_SECONDS = 5.0

// Fixed mode vocabulary. Request/native run in-process; python/osascript spawn a child.
let MODES = ["request", "native", "python", "osascript"]
let CHILD_MODES = ["python", "osascript"]

func usage() -> Never {
    fputs(
        "Usage: CiaobotServerHost {request|native|python|osascript} "
            + "--output /absolute/receipt.json [--python /absolute/interpreter]\n"
            + "  --python is required for python/osascript and rejected otherwise.\n",
        stderr
    )
    exit(2)
}

/// Bounded, lock-protected capture of a child pipe. Keeps draining after the cap
/// so the writer can never deadlock on a full pipe, but records the total byte
/// count and whether anything past the cap was dropped, so a truncation is never
/// silently mistaken for a parse failure.
final class OutputBuffer {
    private let lock = NSLock()
    private var data = Data()
    private var total = 0
    private let limit: Int

    init(limit: Int) {
        self.limit = limit
    }

    func append(_ chunk: Data) {
        lock.lock()
        defer { lock.unlock() }
        total += chunk.count
        let room = limit - data.count
        if room > 0 {
            data.append(chunk.prefix(room))
        }
    }

    func text() -> String {
        lock.lock()
        defer { lock.unlock() }
        // String(decoding:as:) replaces invalid bytes (a cut at the cap or a
        // non-UTF-8 stream) with U+FFFD instead of erasing the evidence the way
        // String(data:encoding:.utf8) would by returning nil.
        return String(decoding: data, as: UTF8.self)
    }

    func totalBytes() -> Int {
        lock.lock()
        defer { lock.unlock() }
        return total
    }

    func truncated() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return total > limit
    }
}

/// Canonicalize an EXISTING path with realpath(3). Returns nil on any resolution
/// error so the caller can fail closed.
func resolveExistingPath(_ path: String) -> String? {
    guard let pointer = realpath(path, nil) else { return nil }
    defer { free(pointer) }
    return String(cString: pointer)
}

/// The reason an `--output` path is refused, or nil when it is acceptable.
///
/// The path must be absolute. Both the output's parent and the bundle root are
/// canonicalized with realpath, so `..` segments, a symlinked parent and an
/// alias such as `/var` -> `/private/var` cannot bypass the in-bundle guard. The
/// parent must already exist; the atomic write renames over a final-component
/// symlink rather than following it, so resolving the parent is sufficient.
/// Fails closed on any resolution error and on a `.`/`..`/empty final component.
///
/// Kept free of `Bundle`/`AppKit` so the offline tests can compile this exact
/// logic with an injected bundle root and no launch.
func outputPathRejection(_ rawOutput: String, bundleRoot rawBundleRoot: String) -> String? {
    guard rawOutput.hasPrefix("/") else { return "output must be an absolute path" }
    let output = rawOutput as NSString
    let lastComponent = output.lastPathComponent
    if lastComponent.isEmpty || lastComponent == "." || lastComponent == ".." {
        return "output must name a file, not '.' or '..'"
    }
    guard let canonicalParent = resolveExistingPath(output.deletingLastPathComponent) else {
        return "output directory does not exist"
    }
    guard let bundleRoot = resolveExistingPath(rawBundleRoot) else {
        return "could not resolve the bundle root"
    }
    let candidate = (canonicalParent as NSString).appendingPathComponent(lastComponent)
    if candidate == bundleRoot || candidate.hasPrefix(bundleRoot + "/") {
        return "output is inside the signed bundle"
    }
    return nil
}

/// Parse the fixed invocation. Rejects unknown/repeated arguments, invalid
/// mode/path combinations, and an output path inside the signed bundle, before
/// any probe or filesystem work happens.
func parseInvocation(_ arguments: [String]) -> (mode: String, output: String, python: String?) {
    guard arguments.count >= 4 else { usage() }
    let mode = arguments[1]
    guard MODES.contains(mode) else { usage() }
    var output: String? = nil
    var python: String? = nil
    var index = 2
    while index < arguments.count {
        let key = arguments[index]
        guard index + 1 < arguments.count else { usage() }
        let value = arguments[index + 1]
        switch key {
        case "--output":
            if output != nil { usage() }
            output = value
        case "--python":
            if python != nil { usage() }
            python = value
        default:
            usage()
        }
        index += 2
    }
    guard let resolvedOutput = output, resolvedOutput.hasPrefix("/") else { usage() }
    // A receipt written inside the signed bundle would break its strict signature
    // on the next verify. Compare canonical paths so `..` segments, a symlinked
    // parent and an alias such as /tmp -> /private/tmp cannot bypass this.
    if let reason = outputPathRejection(resolvedOutput, bundleRoot: Bundle.main.bundleURL.path) {
        fputs("CiaobotServerHost: refusing --output: \(reason)\n", stderr)
        exit(2)
    }
    if CHILD_MODES.contains(mode) {
        guard let resolvedPython = python, resolvedPython.hasPrefix("/") else { usage() }
        return (mode, resolvedOutput, resolvedPython)
    }
    guard python == nil else { usage() }
    return (mode, resolvedOutput, nil)
}

/// Integral, non-boolean NSNumber -> Int. `true` and `4242.0` are rejected the
/// same way the Python validator rejects them, so the two contracts cannot drift.
func integralNumber(_ value: Any) -> Int? {
    guard let number = value as? NSNumber else { return nil }
    if CFGetTypeID(number) == CFBooleanGetTypeID() { return nil }
    if CFNumberIsFloatType(number) { return nil }
    return number.intValue
}

func baseReceipt(mode: String) -> [String: Any] {
    let bundle = Bundle.main
    return [
        "mode": mode,
        "host_pid": Int(getpid()),
        "bundle_id": bundle.bundleIdentifier ?? "missing",
        "display_name": (bundle.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String) ?? "missing",
        "bundle_path": bundle.bundleURL.path,
        // The HOST's own trust state, never prompted here (request mode owns the
        // prompt). Named apart from the child's `accessibility_trusted` so a true
        // host value is never read as proof that the child had access.
        "host_accessibility_trusted": AXIsProcessTrusted(),
        "timestamp": ISO8601DateFormatter().string(from: Date()),
    ]
}

/// Atomically replace the receipt at an explicit absolute path. Returns false on
/// failure; callers keep the malformed-invocation path separate (no file written).
func writeReceipt(_ receipt: [String: Any], to path: String) -> Bool {
    do {
        let data = try JSONSerialization.data(withJSONObject: receipt, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: URL(fileURLWithPath: path), options: [.atomic])
        return true
    } catch {
        fputs("CiaobotServerHost: could not write receipt at \(path): \(error)\n", stderr)
        return false
    }
}

/// `request`: prompt once, then keep the responsible process alive for five
/// seconds because the prompt is asynchronous. Never claims the grant landed.
func runRequest(output: String) -> Int32 {
    var receipt = baseReceipt(mode: "request")
    let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true]
    let trusted = AXIsProcessTrustedWithOptions(options as CFDictionary)
    receipt["accessibility_trusted_at_request"] = trusted
    receipt["ax_prompt_enabled"] = true
    receipt["request_lifetime_seconds"] = REQUEST_LIFETIME_SECONDS
    return writeReceipt(receipt, to: output) ? 0 : 1
}

/// `native`: read only the focused UI element's role. Never text, value, window
/// titles or screenshots. A missing focused element is distinct from no trust.
func runNative(output: String) -> Int32 {
    var receipt = baseReceipt(mode: "native")
    var focused: CFTypeRef?
    let focusedResult = AXUIElementCopyAttributeValue(
        AXUIElementCreateSystemWide(), kAXFocusedUIElementAttribute as CFString, &focused
    )
    receipt["focused_element_result"] = focusedResult.rawValue
    if focusedResult == .success, let focusedElement = focused {
        var role: CFTypeRef?
        let roleResult = AXUIElementCopyAttributeValue(
            focusedElement as! AXUIElement, kAXRoleAttribute as CFString, &role
        )
        receipt["role_result"] = roleResult.rawValue
        if roleResult == .success { receipt["focused_role"] = role as? String }
    }
    return writeReceipt(receipt, to: output) ? 0 : 1
}

/// `python` / `osascript`: spawn the fixed Python sidecar outside the bundle,
/// keep the host alive while it runs, bound and drain its pipes without deadlock,
/// enforce a finite timeout, reap it, then classify the transport outcome. The
/// child's own JSON receipt is preserved verbatim and never upgraded to a success.
func runChild(mode: String, python: String, output: String) -> Int32 {
    var receipt = baseReceipt(mode: mode)
    let appParent = Bundle.main.bundleURL.deletingLastPathComponent()
    let scriptURL = appParent.appendingPathComponent(CHILD_DIR_NAME).appendingPathComponent(CHILD_SCRIPT_NAME)
    receipt["child_script"] = scriptURL.path
    receipt["python_path"] = python

    guard FileManager.default.fileExists(atPath: scriptURL.path) else {
        receipt["transport_status"] = "launch"
        receipt["transport_error"] = "child script missing at fixed sidecar path"
        _ = writeReceipt(receipt, to: output)
        return 1
    }

    let process = Process()
    process.executableURL = URL(fileURLWithPath: python)
    process.arguments = [scriptURL.path, mode]
    let stdoutPipe = Pipe()
    let stderrPipe = Pipe()
    process.standardOutput = stdoutPipe
    process.standardError = stderrPipe

    let stdoutBuffer = OutputBuffer(limit: MAX_CHILD_OUTPUT_BYTES)
    let stderrBuffer = OutputBuffer(limit: MAX_CHILD_OUTPUT_BYTES)
    let drainGroup = DispatchGroup()
    drainGroup.enter()
    DispatchQueue.global().async {
        let handle = stdoutPipe.fileHandleForReading
        while true {
            let chunk = handle.availableData
            if chunk.isEmpty { break }
            stdoutBuffer.append(chunk)
        }
        drainGroup.leave()
    }
    drainGroup.enter()
    DispatchQueue.global().async {
        let handle = stderrPipe.fileHandleForReading
        while true {
            let chunk = handle.availableData
            if chunk.isEmpty { break }
            stderrBuffer.append(chunk)
        }
        drainGroup.leave()
    }

    do {
        try process.run()
    } catch {
        receipt["transport_status"] = "launch"
        receipt["transport_error"] = "\(error)"
        try? stdoutPipe.fileHandleForWriting.close()
        try? stderrPipe.fileHandleForWriting.close()
        _ = drainGroup.wait(timeout: .now() + CHILD_TERMINATE_GRACE_SECONDS)
        _ = writeReceipt(receipt, to: output)
        return 1
    }

    // The parent must drop its copy of each write end, or the readers never see
    // EOF when the child exits and availableData() blocks forever.
    try? stdoutPipe.fileHandleForWriting.close()
    try? stderrPipe.fileHandleForWriting.close()

    receipt["child_pid"] = Int(process.processIdentifier)

    let deadline = Date().addingTimeInterval(CHILD_TIMEOUT_SECONDS)
    var timedOut = false
    while process.isRunning {
        if Date() >= deadline {
            timedOut = true
            break
        }
        Thread.sleep(forTimeInterval: 0.05)
    }
    if process.isRunning {
        // This reaches only the direct child. A grandchild (e.g. an in-flight
        // osascript) is not reaped here; its own 15 s timeout is shorter than the
        // host's 30 s, and close_fds keeps the host's pipes from being held open,
        // so the orphan is bounded. Process-group handling is deliberately out
        // of scope for this spike.
        process.terminate()
        let graceDeadline = Date().addingTimeInterval(CHILD_TERMINATE_GRACE_SECONDS)
        while process.isRunning && Date() < graceDeadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if process.isRunning { kill(process.processIdentifier, SIGKILL) }
    }
    process.waitUntilExit()
    let drained = drainGroup.wait(timeout: .now() + 5) == .success

    let stdoutText = stdoutBuffer.text()
    let stderrText = stderrBuffer.text()
    let stdoutTruncated = stdoutBuffer.truncated()
    let terminationReason = process.terminationReason == .uncaughtSignal ? "uncaught_signal" : "exit"

    receipt["child_exit_status"] = Int(process.terminationStatus)
    receipt["child_termination_reason"] = terminationReason
    receipt["child_timed_out"] = timedOut
    receipt["child_output_drained"] = drained
    receipt["child_stdout"] = stdoutText
    receipt["child_stderr"] = stderrText
    receipt["child_stdout_bytes"] = stdoutBuffer.totalBytes()
    receipt["child_stderr_bytes"] = stderrBuffer.totalBytes()
    receipt["child_stdout_truncated"] = stdoutTruncated
    receipt["child_stderr_truncated"] = stderrBuffer.truncated()

    var childReceipt: [String: Any]? = nil
    var transportStatus = "ok"
    if timedOut {
        transportStatus = "timeout"
    } else if stdoutTruncated {
        // Too large to be a valid child receipt, so it cannot be trusted anyway.
        transportStatus = "oversized"
    } else if process.terminationStatus != 0 {
        transportStatus = "exit"
    } else if let data = stdoutText.data(using: .utf8),
              let parsed = try? JSONSerialization.jsonObject(with: data),
              let dict = parsed as? [String: Any],
              let childPid = integralNumber(dict["pid"] as Any),
              let childPpid = integralNumber(dict["ppid"] as Any),
              let childMode = dict["mode"] as? String,
              let childPlatform = dict["platform"] as? String,
              childMode == mode,
              !childPlatform.isEmpty,
              childPid > 0,
              childPpid >= 0 {
        childReceipt = dict
        receipt["child_self_pid"] = childPid
        receipt["child_reported_parent_pid"] = childPpid
        // Parent proof: the child must report the host as its parent, and its
        // self-reported pid must match the pid the host spawned. A mismatch means
        // the probe ran in a grandchild whose responsible process is unproven.
        let parentMatches = (childPpid == Int(getpid()))
        let pidMatches = (childPid == Int(process.processIdentifier))
        receipt["child_parent_matches_host"] = parentMatches
        receipt["child_pid_matches_spawn"] = pidMatches
        if !parentMatches || !pidMatches {
            transportStatus = "parent_mismatch"
        }
    } else {
        transportStatus = "parse"
    }
    if let childReceipt = childReceipt { receipt["child_receipt"] = childReceipt }
    receipt["transport_status"] = transportStatus

    let wrote = writeReceipt(receipt, to: output)
    return (wrote && transportStatus == "ok") ? 0 : 1
}

let invocation = parseInvocation(CommandLine.arguments)
let app = NSApplication.shared
app.setActivationPolicy(.accessory)

DispatchQueue.main.async {
    switch invocation.mode {
    case "request":
        let code = runRequest(output: invocation.output)
        if code != 0 { exit(code) }
        // Give macOS time to register/present the asynchronous request.
        DispatchQueue.main.asyncAfter(deadline: .now() + REQUEST_LIFETIME_SECONDS) {
            app.terminate(nil)
        }
    case "native":
        exit(runNative(output: invocation.output))
    default:
        exit(runChild(mode: invocation.mode, python: invocation.python ?? "", output: invocation.output))
    }
}

app.run()
