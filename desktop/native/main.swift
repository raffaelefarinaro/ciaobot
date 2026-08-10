// ciaobot-native — a bridge to the macOS frameworks Python cannot reach.
//
// Ciaobot's engine is Python, and none of the three APIs wrapped here are
// usable from it. Apple's on-device dictation (`SpeechAnalyzer` /
// `DictationTranscriber`, macOS 26+) and the on-device LLM (`FoundationModels`,
// macOS 26+) are Swift-only: their types are not ObjC-visible, so pyobjc cannot
// see them, and the `SFSpeechAnalyzer` ObjC class backing the former is not in
// the public headers. `AVSpeechSynthesizer` *is* ObjC, but reaching it from
// Python would mean a pyobjc-framework-AVFoundation dependency purely to pick a
// voice, so it lives here too and the engine ships no voice dependencies at all.
//
// This binary replaced three third-party dependencies with frameworks that are
// already part of the OS: mlx-whisper (hear), kokoro-onnx (speak), and the
// `apfel` Homebrew CLI (respond). The first two downloaded model weights on
// first use — 340 MB in Kokoro's case.
//
// `respond` deliberately calls FoundationModels directly rather than shelling
// out to Apple's `fm` CLI: `fm` only ships with macOS 27, while the framework it
// wraps has been present since macOS 26, so going straight to the framework
// works a full OS release earlier and depends on no external binary.
//
// Four subcommands, all one-shot and file/stdio based so the engine can treat
// it as a plain subprocess:
//
//   ciaobot-native probe                     -> JSON: what this machine supports
//   ciaobot-native hear <file> [--locale L]  -> stdout: the transcript
//   ciaobot-native speak [--voice V] [--locale L] < text -> stdout: WAV bytes
//   ciaobot-native respond [--instructions S] < prompt   -> stdout: the reply
//
// `hear` reads a file rather than the microphone on purpose: the PWA already
// records audio in the browser and hands the engine a path. Never opening an
// input device means no microphone TCC prompt, which matters because an ad-hoc
// signed bundle's permission grants reset on every update.
import AVFoundation
import Foundation
import FoundationModels
import Speech

// MARK: - Exit and output helpers

/// Sidecar exit codes. The engine maps these to actionable errors, so they are
/// part of the contract with ciao/voice.py.
enum ExitCode: Int32 {
    case ok = 0
    case usage = 64
    case unsupportedOS = 65
    case localeUnavailable = 66
    case audioUnreadable = 67
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

// MARK: - Locale resolution

/// Pick the dictation locale to actually use.
///
/// Exact BCP-47 match wins; otherwise any installed locale sharing the language
/// code does ("en-GB" for a request of "en-US"), because a user who set up
/// dictation in one English region should not be told English is unavailable.
/// Returns nil when the language is not installed at all — the caller reports
/// that rather than silently transcribing in the wrong language.
@available(macOS 26.0, *)
func resolveLocale(requested: String, installed: [Locale]) -> Locale? {
    let wanted = requested.replacingOccurrences(of: "_", with: "-").lowercased()
    if let exact = installed.first(where: {
        ($0.identifier(.bcp47)).lowercased() == wanted
    }) {
        return exact
    }
    let language = wanted.split(separator: "-").first.map(String.init) ?? wanted
    return installed.first { locale in
        (locale.identifier(.bcp47)).lowercased().hasPrefix(language)
    }
}

// MARK: - probe

/// Report what this machine can do, as JSON, so Settings can show or hide the
/// native engines without the Python side needing to know any of these rules.
func runProbe() async {
    var hear: [String: Any] = ["available": false, "reason": "requires macOS 26 or newer"]

    if #available(macOS 26.0, *) {
        // Only installedLocales: supportedLocales is a second framework call
        // whose result no caller reads, and it is awaited serially so its
        // latency lands on every probe.
        let installed = await DictationTranscriber.installedLocales.map { $0.identifier(.bcp47) }
        hear = [
            "available": !installed.isEmpty,
            "installed_locales": installed.sorted(),
        ]
        if installed.isEmpty {
            hear["reason"] = "no dictation languages are installed; add one in System Settings > Keyboard > Dictation"
        }
    }

    // Voices, best quality first, so the engine can show the same ordering the
    // synthesizer will actually pick from.
    let voices = AVSpeechSynthesisVoice.speechVoices()
        .sorted { qualityRank($0.quality) > qualityRank($1.quality) }
        .map { voice in
            [
                "id": voice.identifier,
                "name": voice.name,
                "locale": voice.language,
                "quality": qualityName(voice.quality),
            ]
        }
    // best_quality is derivable from voices[0] and nothing asks for it.
    let speak: [String: Any] = [
        "available": !voices.isEmpty,
        "voices": voices,
    ]

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

    let payload: [String: Any] = ["hear": hear, "speak": speak, "model": model]
    guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]) else {
        fail(.failure, "could not serialize the probe result")
    }
    FileHandle.standardOutput.write(data)
}

/// Rank and label for a voice tier, in one place so they cannot drift apart.
/// Rank drives "best installed voice"; the label is what Settings shows.
func qualityTier(_ quality: AVSpeechSynthesisVoiceQuality) -> (rank: Int, name: String) {
    switch quality {
    case .premium: return (3, "premium")
    case .enhanced: return (2, "enhanced")
    default: return (1, "default")
    }
}

func qualityRank(_ quality: AVSpeechSynthesisVoiceQuality) -> Int {
    qualityTier(quality).rank
}

func qualityName(_ quality: AVSpeechSynthesisVoiceQuality) -> String {
    qualityTier(quality).name
}

// MARK: - hear

@available(macOS 26.0, *)
func runHear(path: String, requested: String) async {
    let url = URL(fileURLWithPath: path)
    guard let file = try? AVAudioFile(forReading: url) else {
        fail(.audioUnreadable, "could not read audio at \(path)")
    }

    let installed = await DictationTranscriber.installedLocales
    guard let locale = resolveLocale(requested: requested, installed: installed) else {
        let names = installed.map { $0.identifier(.bcp47) }.sorted().joined(separator: ", ")
        fail(
            .localeUnavailable,
            "no installed dictation language matches \(requested)"
                + (names.isEmpty
                    ? "; add one in System Settings > Keyboard > Dictation"
                    : "; installed: \(names)")
        )
    }

    // DictationTranscriber rather than SpeechTranscriber: it reuses the assets
    // the OS already downloaded for system dictation, so transcription needs no
    // extra model download. SpeechTranscriber reports an install request for
    // configurations whose assets are absent, which is exactly the "external
    // installation" this replaced.
    let transcriber = DictationTranscriber(locale: locale, preset: .longDictation)
    let analyzer = SpeechAnalyzer(modules: [transcriber])

    // Results must be consumed while analysis runs, so start collecting first.
    let collector = Task { () -> String in
        var pieces: [String] = []
        for try await result in transcriber.results {
            let chunk = String(result.text.characters)
            if !chunk.isEmpty { pieces.append(chunk) }
        }
        return pieces.joined(separator: " ")
    }

    do {
        _ = try await analyzer.analyzeSequence(from: file)
        try await analyzer.finalizeAndFinishThroughEndOfInput()
    } catch {
        collector.cancel()
        fail(.failure, "transcription failed: \(error.localizedDescription)")
    }

    let text: String
    do {
        text = try await collector.value
    } catch {
        fail(.failure, "transcription failed while reading results: \(error.localizedDescription)")
    }

    let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
    if trimmed.isEmpty {
        fail(.emptyResult, "transcription produced no text")
    }
    emit(trimmed)
}

// MARK: - speak

/// Highest-quality installed voice for a language.
///
/// Ordered premium > enhanced > default so the sidecar automatically improves if
/// Apple ships better voices, or the user downloads them in System Settings,
/// without any change here. Falls back to any voice for the language, then to
/// the system default, so speech never fails purely over voice selection.
func bestVoice(locale requested: String, explicit: String) -> AVSpeechSynthesisVoice? {
    if !explicit.isEmpty {
        if let exact = AVSpeechSynthesisVoice(identifier: explicit) {
            return exact
        }
        if let named = AVSpeechSynthesisVoice.speechVoices().first(where: {
            $0.name.caseInsensitiveCompare(explicit) == .orderedSame
        }) {
            return named
        }
    }
    let wanted = requested.replacingOccurrences(of: "_", with: "-").lowercased()
    let language = wanted.split(separator: "-").first.map(String.init) ?? wanted
    let candidates = AVSpeechSynthesisVoice.speechVoices().filter { voice in
        let id = voice.language.lowercased()
        return id == wanted || id.hasPrefix(language)
    }
    let ranked = candidates.sorted { lhs, rhs in
        // Prefer an exact locale before a language-family fallback, then the
        // highest installed quality. The old max-by-quality comparison could
        // pick a different regional voice when both tiers were equal.
        let lhsExact = lhs.language.lowercased() == wanted
        let rhsExact = rhs.language.lowercased() == wanted
        if lhsExact != rhsExact { return lhsExact }
        let lhsQuality = qualityRank(lhs.quality)
        let rhsQuality = qualityRank(rhs.quality)
        if lhsQuality != rhsQuality { return lhsQuality > rhsQuality }
        return lhs.identifier < rhs.identifier
    }
    return ranked.first ?? AVSpeechSynthesisVoice(language: requested)
}

/// Little-endian 16-bit mono WAV around raw PCM frames.
func wavContainer(pcm: Data, sampleRate: Int) -> Data {
    var out = Data()
    func le32(_ value: Int) { out.append(contentsOf: withUnsafeBytes(of: UInt32(value).littleEndian, Array.init)) }
    func le16(_ value: Int) { out.append(contentsOf: withUnsafeBytes(of: UInt16(value).littleEndian, Array.init)) }

    out.append(contentsOf: Array("RIFF".utf8))
    le32(36 + pcm.count)
    out.append(contentsOf: Array("WAVEfmt ".utf8))
    le32(16)            // fmt chunk size
    le16(1)             // PCM
    le16(1)             // mono
    le32(sampleRate)
    le32(sampleRate * 2) // byte rate: 1 channel * 2 bytes
    le16(2)             // block align
    le16(16)            // bits per sample
    out.append(contentsOf: Array("data".utf8))
    le32(pcm.count)
    out.append(pcm)
    return out
}

/// Collects synthesizer output. AVSpeechSynthesizer.write delivers buffers on an
/// internal queue and signals completion with an empty one, so the accumulation
/// is serialized behind a lock and the caller waits on a semaphore.
final class SpeechCollector {
    private let lock = NSLock()
    private var pcm = Data()
    private var rate = 0
    private let done = DispatchSemaphore(value: 0)
    private var finished = false

    func append(_ buffer: AVAudioBuffer) {
        guard let pcmBuffer = buffer as? AVAudioPCMBuffer else { return }
        let frames = Int(pcmBuffer.frameLength)
        if frames == 0 {
            complete()
            return
        }
        lock.lock()
        rate = Int(pcmBuffer.format.sampleRate)
        if let ints = pcmBuffer.int16ChannelData {
            pcm.append(Data(bytes: ints[0], count: frames * 2))
        } else if let floats = pcmBuffer.floatChannelData {
            // AVSpeechSynthesizer hands back float32 on current macOS; convert
            // to the 16-bit PCM the PWA's audio element expects.
            var scratch = Data(capacity: frames * 2)
            for index in 0..<frames {
                let clamped = max(-1.0, min(1.0, floats[0][index]))
                let sample = Int16(clamped * 32767)
                scratch.append(contentsOf: withUnsafeBytes(of: sample.littleEndian, Array.init))
            }
            pcm.append(scratch)
        }
        lock.unlock()
    }

    func complete() {
        lock.lock()
        let alreadyDone = finished
        finished = true
        lock.unlock()
        if !alreadyDone { done.signal() }
    }

    var isFinished: Bool {
        lock.lock()
        defer { lock.unlock() }
        return finished
    }

    func collected() -> (Data, Int) {
        lock.lock()
        defer { lock.unlock() }
        return (pcm, rate)
    }
}

func runSpeak(requested: String, explicit: String) {
    let input = FileHandle.standardInput.readDataToEndOfFile()
    guard let text = String(data: input, encoding: .utf8)?
        .trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty
    else {
        fail(.usage, "speak expects the text to read on stdin")
    }

    let utterance = AVSpeechUtterance(string: text)
    guard let voice = bestVoice(locale: requested, explicit: explicit) else {
        fail(.localeUnavailable, "no installed voice matches \(requested)")
    }
    utterance.voice = voice

    let synthesizer = AVSpeechSynthesizer()
    let collector = SpeechCollector()
    synthesizer.write(utterance) { buffer in
        collector.append(buffer)
    }

    // AVSpeechSynthesizer delivers buffers through the run loop, so the main
    // thread has to keep spinning it rather than blocking on a semaphore —
    // blocking deadlocks: the callback that would signal it never runs.
    let deadline = Date().addingTimeInterval(120)
    while !collector.isFinished && Date() < deadline {
        RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
    }
    if !collector.isFinished {
        fail(.failure, "speech synthesis timed out")
    }
    let (pcm, rate) = collector.collected()
    if pcm.isEmpty || rate == 0 {
        fail(.emptyResult, "speech synthesis produced no audio")
    }
    FileHandle.standardOutput.write(wavContainer(pcm: pcm, sampleRate: rate))
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
        .trimmingCharacters(in: .whitespacesAndNewlines), !prompt.isEmpty
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
    let options = GenerationOptions(
        samplingMode: .greedy,
        maximumResponseTokens: 512
    )
    do {
        let response = try await session.respond(to: prompt, options: options)
        let text = response.content.trimmingCharacters(in: .whitespacesAndNewlines)
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
  ciaobot-native hear <audio-file> [--locale en-US]
  ciaobot-native speak [--locale en-US] [--voice <identifier-or-name>] < text
  ciaobot-native respond [--instructions <system prompt>] < prompt
"""

/// Runs an async body from synchronous `main`, keeping the main thread free.
///
/// `main` is deliberately not `async`: `speak` needs to spin the main run loop
/// for AVSpeechSynthesizer's callbacks, which an async entry point does not do.
/// The async subcommands run on the cooperative pool and signal back.
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
        case "hear":
            guard let path = arguments.positional.first else {
                fail(.usage, usage)
            }
            guard #available(macOS 26.0, *) else {
                fail(.unsupportedOS, "on-device dictation requires macOS 26 or newer")
            }
            let locale = arguments.option("locale", default: "en-US")
            runBlocking { await runHear(path: path, requested: locale) }
        case "speak":
            runSpeak(
                requested: arguments.option("locale", default: "en-US"),
                explicit: arguments.option("voice")
            )
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
