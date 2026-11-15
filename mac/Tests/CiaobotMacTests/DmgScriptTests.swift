import XCTest

/// Exercises `scripts/make-dmg.sh` against throwaway staged apps with recording
/// doubles for `hdiutil`, `codesign` and `xcrun`. Nothing here signs with a real
/// identity, submits to Apple, mounts a system volume, or writes outside a
/// scratch directory. The one real `hdiutil` run creates and detaches its own
/// image.
final class DmgScriptTests: XCTestCase {
    private var scratch: URL!
    private var tools: URL!
    private let fileManager = FileManager.default
    private let identity = "Developer ID Application: Test Signer (AXBFS96Y9F)"

    override func setUpWithError() throws {
        scratch = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("ciaobot-dmg-\(UUID().uuidString)", isDirectory: true)
        tools = scratch.appendingPathComponent("bin", isDirectory: true)
        try fileManager.createDirectory(at: tools, withIntermediateDirectories: true)
        try fileManager.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
        try makeTool("codesign", body: """
        #!/bin/sh
        [ -n "${FAKE_CODESIGN_LOG:-}" ] && {
            for argument in "$@"; do printf '%s|' "$argument"; done
            printf '\\n'
        } >> "$FAKE_CODESIGN_LOG"
        describe=
        for argument in "$@"; do
            case $argument in
                -d|--display|--verbose=4) describe=1 ;;
            esac
        done
        case "$*" in
            *--verify*) exit "${FAKE_CODESIGN_VERIFY_STATUS:-0}" ;;
        esac
        [ -n "$describe" ] || exit 0
        printf '%s\\n' "Identifier=local.ciaobot.mac" >&2
        printf '%s\\n' "Authority=${FAKE_CODESIGN_AUTHORITY:-Developer ID Application: Test Signer (AXBFS96Y9F)}" >&2
        if [ "${FAKE_CODESIGN_NO_TEAM:-0}" = 1 ]; then
            printf '%s\\n' "TeamIdentifier=not set" >&2
        else
            printf '%s\\n' "TeamIdentifier=${FAKE_CODESIGN_TEAM:-AXBFS96Y9F}" >&2
        fi
        exit 0
        """)
        try makeTool("xcrun", body: """
        #!/bin/sh
        [ -n "${FAKE_XCRUN_LOG:-}" ] && {
            for argument in "$@"; do printf '%s|' "$argument"; done
            printf '\\n'
        } >> "$FAKE_XCRUN_LOG"
        [ "$1" = "stapler" ] || exit 1
        case $2 in
            validate) exit "${FAKE_STAPLER_STATUS:-0}" ;;
            staple) exit "${FAKE_STAPLE_STATUS:-0}" ;;
        esac
        exit 1
        """)
        try makeTool("hdiutil", body: FakeHdiutil.script)
    }

    override func tearDownWithError() throws {
        if let scratch { try? fileManager.removeItem(at: scratch) }
    }

    // MARK: - Refusals

    func testRefusesAMissingApp() throws {
        let output = try outputPath()

        let result = try runScript(["--app", missingApp.path, "--output", output, "--identity", identity])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("no app bundle"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output), "a refused run must create no image")
        XCTAssertNil(try hdiutilLogIfAny(), "nothing may be mounted or created for a missing app")
    }

    func testRefusesAnExistingOutputWithoutOverwritingIt() throws {
        let app = try makeApp()
        let output = try outputPath()
        try Data("keep this image\n".utf8).write(to: URL(fileURLWithPath: output))

        let result = try runScript(["--app", app.path, "--output", output, "--identity", identity])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("refusing to overwrite"), result.output)
        XCTAssertEqual(
            try String(contentsOfFile: output, encoding: .utf8),
            "keep this image\n",
            "an existing image must never be replaced"
        )
        XCTAssertNil(try hdiutilLogIfAny())
    }

    func testRefusesANonStapledApp() throws {
        let app = try makeApp()
        let output = try outputPath()

        let result = try runScript(
            ["--app", app.path, "--output", output, "--identity", identity],
            environment: ["FAKE_STAPLER_STATUS": "1"]
        )

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("not notarized"), result.output)
        XCTAssertTrue(result.output.contains("stapler"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output))
        XCTAssertNil(try hdiutilLogIfAny(), "an unstapled app must be refused before any image work")
    }

    func testRefusesAnAppInsideAnApplicationsFolder() throws {
        let app = try makeApp(inside: "Applications")
        let output = try outputPath()

        let result = try runScript(["--app", app.path, "--output", output, "--identity", identity])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("Applications folder"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output))
        XCTAssertNil(try hdiutilLogIfAny())
    }

    func testRefusesAWrongBundleIdentifier() throws {
        let app = try makeApp(identifier: "local.ciaobot.local")
        let output = try outputPath()

        let result = try runScript(["--app", app.path, "--output", output, "--identity", identity])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("local.ciaobot.mac"), result.output)
        XCTAssertTrue(result.output.contains("local.ciaobot.local"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output))
        XCTAssertNil(try hdiutilLogIfAny())
    }

    /// A notarized app another developer signed must never become this
    /// project's download, exactly as the appcast refuses it.
    func testRefusesAnAppSignedByAnotherTeam() throws {
        let app = try makeApp()
        let output = try outputPath()

        let result = try runScript(
            ["--app", app.path, "--output", output, "--identity", identity],
            environment: ["FAKE_CODESIGN_TEAM": "ZZZZZZZZZZ"]
        )

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("ZZZZZZZZZZ"), result.output)
        XCTAssertTrue(result.output.contains("AXBFS96Y9F"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output))
        XCTAssertNil(try hdiutilLogIfAny())
    }

    func testRefusesAnAdHocIdentity() throws {
        let app = try makeApp()
        let output = try outputPath()

        let result = try runScript(["--app", app.path, "--output", output, "--identity", "-"])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("Developer ID"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output))
        XCTAssertNil(try hdiutilLogIfAny())
    }

    func testRefusesAnOutputInsideTheSourceApp() throws {
        let app = try makeApp()
        let output = app.appendingPathComponent("Contents/Ciaobot.dmg").path

        let result = try runScript(["--app", app.path, "--output", output, "--identity", identity])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("inside the source app"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output))
        XCTAssertNil(try hdiutilLogIfAny())
    }

    func testUnknownArgumentsAreRejected() throws {
        let result = try runScript(["--force"])

        XCTAssertEqual(result.status, 2, result.output)
        XCTAssertTrue(result.output.contains("unknown argument: --force"), result.output)
    }

    func testScriptIsExecutable() {
        XCTAssertTrue(
            fileManager.isExecutableFile(atPath: scriptsDirectory.appendingPathComponent("make-dmg.sh").path),
            "scripts/make-dmg.sh is invoked directly and must keep its executable bit"
        )
    }

    // MARK: - The image

    /// The image is created from a staged copy with the expected `hdiutil`
    /// arguments, signed with the identity, verified, mounted read-only,
    /// inspected for the app and the `Applications` symlink, detached, and only
    /// then moved to the requested output.
    func testCreatesSignsAndVerifiesTheImageWithTheExpectedArguments() throws {
        let app = try makeApp()
        let output = try outputPath()

        let result = try runScript(["--app", app.path, "--output", output, "--identity", identity])

        XCTAssertEqual(result.status, 0, result.output)
        XCTAssertTrue(fileManager.fileExists(atPath: output), result.output)
        XCTAssertEqual(try String(contentsOfFile: output, encoding: .utf8), "stand-in disk image\n")

        let calls = try hdiutilCalls()
        let create = try XCTUnwrap(calls.first { $0.first == "create" })
        XCTAssertEqual(Array(create.prefix(4)), ["create", "-volname", "Ciaobot", "-srcfolder"],
                       "the volume name and source folder flags are fixed: \(create)")
        XCTAssertEqual(Array(create.dropFirst(5).prefix(2)), ["-fs", "HFS+"],
                       "the image filesystem is fixed: \(create)")
        let stage = create[4]
        let formatIndex = try XCTUnwrap(create.firstIndex(of: "-format"))
        XCTAssertEqual(create[formatIndex + 1], "UDZO", "the shipped image must be compressed: \(create)")
        let pending = try XCTUnwrap(create.last)
        XCTAssertEqual((pending as NSString).pathExtension, "dmg")
        let resolvedOutputDirectory = try realPath(of: outputDirectory)
        let pendingDirectory = URL(fileURLWithPath: pending).deletingLastPathComponent()
        XCTAssertEqual(pendingDirectory.deletingLastPathComponent().path, resolvedOutputDirectory,
                       "the image is built beside the output so publishing is a rename on one volume")
        XCTAssertTrue(pendingDirectory.lastPathComponent.hasPrefix(".ciaobot-dmg."),
                      "the staging directory is private to this run: \(pendingDirectory.path)")
        let stageRoot = URL(fileURLWithPath: stage).deletingLastPathComponent().deletingLastPathComponent()
        XCTAssertEqual(stageRoot.path, resolvedOutputDirectory,
                       "the staging tree lives beside the output, never in the caller's tree")
        XCTAssertNotEqual(pending, output, "nothing may appear at the published path before the image is verified")

        let probe = try String(contentsOf: probeFile, encoding: .utf8)
        XCTAssertTrue(probe.contains("create-stage-app=yes"), "the staged volume must contain Ciaobot.app: \(probe)")
        XCTAssertTrue(probe.contains("create-stage-link=/Applications"),
                      "the staged volume must contain Applications -> /Applications: \(probe)")
        XCTAssertTrue(probe.contains("detach-published=no"),
                      "nothing may be published before the image is inspected and detached: \(probe)")
        XCTAssertFalse(probe.contains(stage), "the probe must not leak the staging path")

        let verify = try XCTUnwrap(calls.first { $0.first == "verify" })
        XCTAssertEqual(verify.last, pending, "hdiutil verify must check the image that was just created")

        let attach = try XCTUnwrap(calls.first { $0.first == "attach" })
        XCTAssertTrue(attach.contains("-readonly"), "verification must never mount read-write: \(attach)")
        XCTAssertTrue(attach.contains("-nobrowse"), "verification must not appear in Finder: \(attach)")
        XCTAssertEqual(attach.last, pending)
        let mountPoint = try XCTUnwrap(attach.firstIndex(of: "-mountpoint")).map { attach[$0 + 1] }
        let detach = try XCTUnwrap(calls.first { $0.first == "detach" })
        XCTAssertEqual(detach.last, mountPoint, "the mounted volume must be detached by its own mount point")
        XCTAssertEqual(calls.filter { $0.first == "attach" }.count,
                       calls.filter { $0.first == "detach" }.count,
                       "a successful run mounts and detaches exactly once: \(calls)")

        let signed = try XCTUnwrap(try codesignCalls().first { $0.contains(pending) })
        XCTAssertEqual(Array(signed.prefix(3)), ["--force", "--timestamp", "--sign"],
                       "the image must be signed with a timestamp: \(signed)")
        XCTAssertEqual(signed[3], identity)
        XCTAssertFalse(signed.contains("--deep"), "signing must not use --deep")

        let leftovers = try fileManager.contentsOfDirectory(atPath: outputDirectory.path)
            .filter { $0 != (output as NSString).lastPathComponent }
        XCTAssertEqual(leftovers, [String](), "the run must leave no staging directory behind: \(leftovers)")
    }

    /// A volume that does not contain what was staged must fail the run and must
    /// still be detached; a left-mounted image breaks every later build.
    func testDetachesTheVolumeWhenTheMountedContentIsWrong() throws {
        let app = try makeApp()
        let output = try outputPath()

        let result = try runScript(
            ["--app", app.path, "--output", output, "--identity", identity],
            environment: ["FAKE_HDIUTIL_SKIP_LINK": "1"]
        )

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("Applications"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output), "a failed verification must publish nothing")

        let calls = try hdiutilCalls()
        XCTAssertEqual(calls.filter { $0.first == "attach" }.count, 1)
        XCTAssertEqual(calls.filter { $0.first == "detach" }.count, 1,
                       "the failure path must still detach: \(calls)")
        XCTAssertEqual(calls.last?.first, "detach", "detach must be the last image operation: \(calls)")
        let probe = try String(contentsOf: probeFile, encoding: .utf8)
        XCTAssertTrue(probe.contains("attach-mountpoint="), "the image must have been mounted for inspection: \(probe)")
    }

    func testDetachesTheVolumeWhenAHdiutilVerificationFails() throws {
        let app = try makeApp()
        let output = try outputPath()

        let result = try runScript(
            ["--app", app.path, "--output", output, "--identity", identity],
            environment: ["FAKE_HDIUTIL_VERIFY_STATUS": "1"]
        )

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output))
        let calls = try hdiutilCalls()
        XCTAssertTrue(calls.contains { $0.first == "verify" })
        XCTAssertFalse(calls.contains { $0.first == "detach" },
                       "a failed verify never mounted anything, so nothing may be detached: \(calls)")
    }

    /// One real image is created, mounted read-only, inspected and detached with
    /// the system `hdiutil`; the signing and stapling tools stay doubles so no
    /// identity or Apple credential is needed.
    func testTheRealHdiutilCreatesAndDetachesItsOwnImage() throws {
        let hdiutil = "/usr/bin/hdiutil"
        try XCTSkipUnless(fileManager.isExecutableFile(atPath: hdiutil),
                          "hdiutil is not installed on this machine")
        let app = try makeApp()
        let output = try outputPath()
        // Only the signing doubles: hdiutil comes from the system.
        let realTools = scratch.appendingPathComponent("real-bin", isDirectory: true)
        try fileManager.createDirectory(at: realTools, withIntermediateDirectories: true)
        for name in ["codesign", "xcrun"] {
            try fileManager.copyItem(at: tools.appendingPathComponent(name),
                                     to: realTools.appendingPathComponent(name))
        }

        let result = try runScript(
            ["--app", app.path, "--output", output, "--identity", identity],
            extraPath: realTools.path,
            environment: ["FAKE_HDIUTIL_LOG": "", "FAKE_HDIUTIL_STATE": "", "FAKE_HDIUTIL_PROBE": ""]
        )

        XCTAssertEqual(result.status, 0, result.output)
        XCTAssertTrue(fileManager.fileExists(atPath: output))
        let verify = try run(hdiutil, ["verify", output])
        XCTAssertEqual(verify.status, 0, "the published image must pass hdiutil verify: \(verify.output)")
        // The run detaches what it mounted; prove nothing of ours is still
        // attached to the scratch tree.
        let attached = try run(hdiutil, ["info"])
        XCTAssertEqual(attached.status, 0, attached.output)
        XCTAssertFalse(attached.output.contains(scratch.path),
                       "the run left a volume mounted: \(attached.output)")
    }

    // MARK: - Helpers

    private var packageRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    private var scriptsDirectory: URL {
        packageRoot.appendingPathComponent("scripts")
    }

    private var outputDirectory: URL {
        scratch.appendingPathComponent("out", isDirectory: true)
    }

    private var missingApp: URL {
        scratch.appendingPathComponent("Missing/Ciaobot.app", isDirectory: true)
    }

    private var hdiutilLog: URL {
        scratch.appendingPathComponent("hdiutil.log")
    }

    private var hdiutilState: URL {
        scratch.appendingPathComponent("hdiutil-state", isDirectory: true)
    }

    private var probeFile: URL {
        scratch.appendingPathComponent("hdiutil-probe.txt")
    }

    private var codesignLog: URL {
        scratch.appendingPathComponent("codesign.log")
    }

    private var xcrunLog: URL {
        scratch.appendingPathComponent("xcrun.log")
    }

    private func outputPath() throws -> String {
        outputDirectory.appendingPathComponent("Ciaobot-1.2.3.dmg").path
    }

    private func makeTool(_ name: String, body: String) throws {
        let url = tools.appendingPathComponent(name)
        try Data(body.utf8).write(to: url)
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
    }

    /// A minimal staged app with the real bundle identifier. `inside` chooses a
    /// parent directory name, so an Applications folder can be simulated.
    @discardableResult
    private func makeApp(
        identifier: String = "local.ciaobot.mac",
        inside parent: String? = nil
    ) throws -> URL {
        let directory = scratch
            .appendingPathComponent(parent ?? "Build", isDirectory: true)
            .appendingPathComponent("Ciaobot.app", isDirectory: true)
        let contents = directory.appendingPathComponent("Contents", isDirectory: true)
        let macos = contents.appendingPathComponent("MacOS", isDirectory: true)
        try fileManager.createDirectory(at: macos, withIntermediateDirectories: true)
        let plist = """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0"><dict>
        <key>CFBundleExecutable</key><string>CiaobotMac</string>
        <key>CFBundleIdentifier</key><string>\(identifier)</string>
        <key>CFBundlePackageType</key><string>APPL</string>
        <key>CFBundleShortVersionString</key><string>1.2.3</string>
        <key>CFBundleVersion</key><string>7</string>
        </dict></plist>
        """
        try Data(plist.utf8).write(to: contents.appendingPathComponent("Info.plist"))
        let executable = macos.appendingPathComponent("CiaobotMac")
        try Data("#!/bin/sh\nexit 0\n".utf8).write(to: executable)
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path)
        return directory
    }

    @discardableResult
    private func runScript(
        _ arguments: [String],
        extraPath: String? = nil,
        environment: [String: String] = [:]
    ) throws -> (status: Int32, output: String) {
        var childEnvironment = ProcessInfo.processInfo.environment
        var path = tools.path + ":/usr/bin:/bin:/usr/sbin:/sbin"
        if let extraPath { path = extraPath + ":/usr/bin:/bin:/usr/sbin:/sbin" }
        childEnvironment["PATH"] = path
        childEnvironment["FAKE_CODESIGN_LOG"] = codesignLog.path
        childEnvironment["FAKE_XCRUN_LOG"] = xcrunLog.path
        childEnvironment["FAKE_HDIUTIL_LOG"] = hdiutilLog.path
        childEnvironment["FAKE_HDIUTIL_STATE"] = hdiutilState.path
        childEnvironment["FAKE_HDIUTIL_PROBE"] = probeFile.path
        childEnvironment["FAKE_STAPLER_STATUS"] = "0"
        // Let the fake record whether anything had been published at the moment
        // the image was detached.
        if let outputIndex = arguments.firstIndex(of: "--output"), arguments.indices.contains(outputIndex + 1) {
            childEnvironment["FAKE_HDIUTIL_OUTPUT"] = arguments[outputIndex + 1]
        }
        for (key, value) in environment { childEnvironment[key] = value }
        try fileManager.createDirectory(at: hdiutilState, withIntermediateDirectories: true)
        return try run(
            "/bin/sh",
            [scriptsDirectory.appendingPathComponent("make-dmg.sh").path] + arguments,
            environment: childEnvironment
        )
    }

    private func hdiutilCalls() throws -> [[String]] {
        try calls(in: hdiutilLog)
    }

    /// `nil` when the script never invoked `hdiutil` at all: a refusal must be
    /// decided before any image work.
    private func hdiutilLogIfAny() throws -> [[String]]? {
        guard fileManager.fileExists(atPath: hdiutilLog.path) else { return nil }
        let calls = try hdiutilCalls()
        return calls.isEmpty ? nil : calls
    }

    private func realPath(of url: URL) throws -> String {
        let result = try run("/bin/realpath", [url.path])
        XCTAssertEqual(result.status, 0, result.output)
        return result.output.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func codesignCalls() throws -> [[String]] {
        try calls(in: codesignLog)
    }

    private func calls(in log: URL) throws -> [[String]] {
        let text = try String(contentsOf: log, encoding: .utf8)
        return text.split(separator: "\n").map { line in
            line.split(separator: "|").map(String.init)
        }
    }

    @discardableResult
    private func run(
        _ executable: String,
        _ arguments: [String],
        environment: [String: String] = [:]
    ) throws -> (status: Int32, output: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.currentDirectoryURL = packageRoot
        process.environment = environment.isEmpty
            ? ProcessInfo.processInfo.environment
            : environment
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus, String(decoding: data, as: UTF8.self))
    }
}
