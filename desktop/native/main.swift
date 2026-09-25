// ciaobot-native — a bridge to the macOS frameworks Python cannot reach.
//
// Ciaobot's engine is Python, and Apple's on-device model API is Swift-only.
// FoundationModels is not ObjC-visible, so pyobjc cannot access it.
//
// `respond` deliberately calls FoundationModels directly rather than shelling
// out to Apple's `fm` CLI: `fm` only ships with macOS 27, while the framework it
// wraps has been present since macOS 26, so going straight to the framework
// works a full OS release earlier and depends on no external binary.
//
// Two subcommands, all one-shot and file/stdio based so the engine can treat
// it as a plain subprocess:
//
//   ciaobot-native probe                     -> JSON: what this machine supports
//   ciaobot-native respond [--instructions S] < prompt   -> stdout: the reply
import Foundation
import FoundationModels

// MARK: - Exit and output helpers

/// Sidecar exit codes. The engine maps these to actionable errors, so they are
/// part of the native sidecar contract.
enum ExitCode: Int32 {
    case ok = 0
    case usage = 64
    case unsupportedOS = 65
    case emptyResult = 68
    case failure = 69
    case modelUnavailable = 70
}

func fail(_ code: ExitCode, _ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code.rawValue)
}

func emit(_ text: String) {
    FileHandle.standardOutput.write(Data(text.utf8))
}

// MARK: - Argument parsing

struct Arguments {
    var command = ""
    var positional: [String] = []
    var options: [String: String] = [:]

    init(_ argv: [String]) {
        var rest = argv
        command = rest.first ?? ""
        if !rest.isEmpty { rest.removeFirst() }
        var index = 0
        while index < rest.count {
            let token = rest[index]
            if token.hasPrefix("--") {
                let key = String(token.dropFirst(2))
                let next = index + 1 < rest.count ? rest[index + 1] : ""
                options[key] = next
                index += 2
            } else {
                positional.append(token)
                index += 1
            }
        }
    }

    func option(_ name: String, default fallback: String = "") -> String {
        let value = options[name] ?? ""
        return value.isEmpty ? fallback : value
    }
}

// MARK: - probe

/// Report on-device model availability.
func runProbe() async {
    var model: [String: Any] = ["available": false, "reason": "requires macOS 26 or newer"]
    if #available(macOS 26.0, *) {
        switch SystemLanguageModel.default.availability {
        case .available:
            model = ["available": true]
        case .unavailable(let reason):
            model = ["available": false, "reason": modelUnavailableReason(reason)]
        @unknown default:
            model = ["available": false, "reason": "the on-device model is unavailable"]
        }
    }
    guard let data = try? JSONSerialization.data(withJSONObject: ["model": model], options: [.sortedKeys]) else {
        fail(.failure, "could not serialize the probe result")
    }
    FileHandle.standardOutput.write(data)
}

// MARK: - respond (on-device LLM)

/// Why the model cannot be used, phrased for a settings screen.
@available(macOS 26.0, *)
func modelUnavailableReason(_ reason: SystemLanguageModel.Availability.UnavailableReason) -> String {
    switch reason {
    case .deviceNotEligible:
        return "this Mac does not support Apple Intelligence"
    case .appleIntelligenceNotEnabled:
        return "Apple Intelligence is off; enable it in System Settings > Apple Intelligence & Siri"
    case .modelNotReady:
        return "the on-device model is still downloading; try again shortly"
    @unknown default:
        return "the on-device model is unavailable"
    }
}

@available(macOS 26.0, *)
func runRespond(instructions: String) async {
    let input = FileHandle.standardInput.readDataToEndOfFile()
    guard let prompt = String(data: input, encoding: .utf8)?
        .trimmingCharacters(in: CharacterSet.whitespacesAndNewlines), !prompt.isEmpty
    else {
        fail(.usage, "respond expects the prompt on stdin")
    }

    let model = SystemLanguageModel.default
    if case .unavailable(let reason) = model.availability {
        // Its own exit code: "Apple Intelligence is switched off" is a
        // different problem from "the model errored", and the engine falls
        // back to a cloud model quietly in the first case.
        fail(.modelUnavailable, modelUnavailableReason(reason))
    }

    let session = instructions.isEmpty
        ? LanguageModelSession()
        : LanguageModelSession(instructions: instructions)
    // `sampling:` is deliberate even though the macOS 27 SDK deprecates it in
    // favour of `samplingMode:`. It is the only spelling that compiles on both:
    // CI's newest available toolchain is Xcode 26.6 (Swift 6.3.3, macOS 26 SDK),
    // where `samplingMode:` does not exist yet, and on macOS 27 this still
    // builds with only a deprecation warning. Switch to `samplingMode:` once
    // the GitHub macOS runner image ships Xcode 27 — not before, or CI cannot
    // build the sidecar at all.
    let options = GenerationOptions(
        sampling: .greedy,
        maximumResponseTokens: 512
    )
    do {
        let response = try await session.respond(to: prompt, options: options)
        // Explicit CharacterSet: the macOS 26 SDK cannot infer the contextual
        // base for a bare `.whitespacesAndNewlines` here.
        let text = response.content.trimmingCharacters(in: CharacterSet.whitespacesAndNewlines)
        if text.isEmpty {
            fail(.emptyResult, "the model returned no text")
        }
        emit(text)
    } catch {
        fail(.failure, "the on-device model failed: \(error.localizedDescription)")
    }
}

// MARK: - Entry point

let usage = """
usage:
  ciaobot-native probe
  ciaobot-native respond [--instructions <system prompt>] < prompt
"""

/// Runs an async body from synchronous `main`, keeping the main thread free.
///
/// Async subcommands run on the cooperative pool and signal back.
func runBlocking(_ body: @escaping @Sendable () async -> Void) {
    let done = DispatchSemaphore(value: 0)
    Task.detached {
        await body()
        done.signal()
    }
    done.wait()
}

@main
struct CiaobotNative {
    static func main() {
        let arguments = Arguments(Array(CommandLine.arguments.dropFirst()))
        switch arguments.command {
        case "probe":
            runBlocking { await runProbe() }
        case "respond":
            guard #available(macOS 26.0, *) else {
                fail(.unsupportedOS, "the on-device model requires macOS 26 or newer")
            }
            let instructions = arguments.option("instructions")
            runBlocking { await runRespond(instructions: instructions) }
        default:
            fail(.usage, usage)
        }
    }
}
