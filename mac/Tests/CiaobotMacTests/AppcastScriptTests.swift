import XCTest

/// Exercises `scripts/make-appcast.sh` against throwaway archives and recording
/// doubles for Sparkle's `generate_appcast` and `hdiutil`. No test unzips a real
/// release, mounts a real image, touches the operator's keychain, publishes
/// anything, or contacts the network feed.
final class AppcastScriptTests: XCTestCase {
    private var scratch: URL!
    private var tools: URL!
    private let fileManager = FileManager.default
    private let downloadPrefix = "https://example.invalid/ciaobot/updates/"

    override func setUpWithError() throws {
        scratch = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("ciaobot-appcast-\(UUID().uuidString)", isDirectory: true)
        tools = scratch.appendingPathComponent("bin", isDirectory: true)
        try fileManager.createDirectory(at: tools, withIntermediateDirectories: true)
        try makeTool("ditto", body: """
        #!/bin/sh
        # ditto -x -k ARCHIVE DEST
        mkdir -p "$4" || exit 1
        cp -R "$CIAOBOT_TEST_APP" "$4/Ciaobot.app" || exit 1
        """)
        try makeTool("lipo", body: """
        #!/bin/sh
        printf '%s\\n' "${CIAOBOT_TEST_ARCHS:-arm64}"
        """)
        try makeTool("codesign", body: """
        #!/bin/sh
        verify=
        describe=
        for argument in "$@"; do
            case $argument in
                --verify) verify=1 ;;
                -d|-dv|--display) describe=1 ;;
            esac
        done
        if [ -n "$verify" ]; then
            exit "${CIAOBOT_TEST_CODESIGN_STATUS:-0}"
        fi
        [ -n "$describe" ] || exit 1
        # codesign -d writes the signed description to stderr.
        printf '%s\\n' "${CIAOBOT_TEST_CODESIGN_DESCRIPTION:-}" >&2
        exit 0
        """)
        try makeTool("xcrun", body: """
        #!/bin/sh
        [ "$1" = "stapler" ] && [ "$2" = "validate" ] || exit 1
        exit "${CIAOBOT_TEST_STAPLER_STATUS:-0}"
        """)
        try makeTool("spctl", body: """
        #!/bin/sh
        exit "${CIAOBOT_TEST_SPCTL_STATUS:-0}"
        """)
        try makeTool("hdiutil", body: """
        #!/bin/sh
        [ -n "${CIAOBOT_TEST_HDIUTIL_LOG:-}" ] && {
            for argument in "$@"; do printf '%s|' "$argument"; done
            printf '\\n'
        } >> "$CIAOBOT_TEST_HDIUTIL_LOG"
        case $1 in
            attach)
                mountpoint=
                while [ $# -gt 0 ]; do
                    case $1 in
                        -mountpoint) mountpoint=$2; shift 2 ;;
                        *) shift ;;
                    esac
                done
                [ -n "$mountpoint" ] || exit 1
                [ "${CIAOBOT_TEST_HDIUTIL_ATTACH_STATUS:-0}" = 0 ] || exit "${CIAOBOT_TEST_HDIUTIL_ATTACH_STATUS}"
                mkdir -p "$mountpoint" || exit 1
                cp -R "$CIAOBOT_TEST_APP" "$mountpoint/Ciaobot.app" || exit 1
                ;;
            detach) ;;
            *) exit 1 ;;
        esac
        exit 0
        """)
        try makeTool("generate_appcast", body: """
        #!/bin/sh
        printf '%s\\n' "$@" > "$CIAOBOT_TEST_APPCAST_ARGS" || exit 1
        for argument in "$@"; do last=$argument; done
        while [ $# -gt 0 ]; do
            [ "$1" = "-o" ] && output=$2
            [ "$1" = "--download-url-prefix" ] && prefix=$2
            shift
        done
        staged=$(ls "$last" 2>/dev/null | head -n 1)
        staged_sha=$(shasum -a 256 "$last/$staged" 2>/dev/null | cut -d' ' -f1)
        {
            printf 'dir-exists=%s\\n' "$([ -d "$last" ] && printf yes || printf no)"
            printf 'staged-files=%s\\n' "$(ls "$last" 2>/dev/null | tr '\\n' ' ')"
            printf 'staged-sha256=%s\\n' "$staged_sha"
            printf 'output=%s\\n' "$output"
        } > "$CIAOBOT_TEST_APPCAST_PROBE" || exit 1
        # Write exactly where -o points, like the real tool does. A relative -o
        # resolves against the process working directory, not the archives
        # directory, which is the bug this fixture has to be able to catch.
        printf '<rss version="2.0"><channel><item><enclosure url="%s%s" /></item></channel></rss>\\n' \\
            "$prefix" "$staged" > "$output" || exit 1
        """)
    }

    override func tearDownWithError() throws {
        if let scratch { try? fileManager.removeItem(at: scratch) }
    }

    // MARK: - The happy path

    /// The appcast is produced by Sparkle's own tool with exactly the arguments
    /// the pinned 2.10.1 binary documents, and a published URL prefix.
    func testGeneratesTheAppcastWithTheExpectedSparkleArguments() throws {
        let archive = try makeArchive(version: "0.1.2")
        let output = try outputPath()

        let result = try runScript([
            "--archive", archive.path,
            "--download-url", downloadPrefix,
            "--output", output.path,
        ])

        XCTAssertEqual(result.status, 0, result.output)
        XCTAssertTrue(fileManager.fileExists(atPath: output.path))
        XCTAssertTrue(try String(contentsOf: output, encoding: .utf8).contains("<rss"))

        let arguments = try recordedArguments()
        XCTAssertEqual(arguments.count, 5, "exactly one staging directory is appended: \(arguments)")
        XCTAssertEqual(Array(arguments.prefix(3)), [
            "--download-url-prefix", downloadPrefix,
            "-o",
        ], "generate_appcast must receive the published URL prefix and an output flag")
        let outputArgument = arguments[3]
        XCTAssertTrue(outputArgument.hasPrefix("/"),
                      "the output path must be absolute: a relative -o resolves against the caller's working directory")
        XCTAssertEqual((outputArgument as NSString).lastPathComponent, "appcast.xml",
                       "generate_appcast must be asked for appcast.xml")
        XCTAssertEqual(URL(fileURLWithPath: arguments[4]).lastPathComponent, "feed",
                       "the tool must be pointed at the staged archives directory")
        XCTAssertEqual(URL(fileURLWithPath: outputArgument).deletingLastPathComponent().path,
                       URL(fileURLWithPath: arguments[4]).path,
                       "the feed must be written inside the staging directory, not into the caller's tree")
        let probe = try String(contentsOf: XCTUnwrap(probeFile), encoding: .utf8)
        XCTAssertTrue(probe.contains("dir-exists=yes"), "the staging directory must exist while the tool runs")
        XCTAssertTrue(probe.contains("staged-files=Ciaobot-0.1.2-notarized.zip"),
                      "the archive must be staged under the published file name: \(probe)")
        XCTAssertFalse(fileManager.fileExists(atPath: packageRoot.appendingPathComponent("appcast.xml").path),
                       "a relative output must never write the feed into the repository root")
    }

    /// Signatures are produced by the pinned tool, never by this repository.
    func testNeverPassesAPrivateKeyToTheTool() throws {
        let text = try scriptText()
        XCTAssertFalse(text.contains("--ed-key-file"), "the private EdDSA key must stay in the keychain")
        XCTAssertFalse(text.contains("generate_keys"), "the script must never create or read key files")
        XCTAssertFalse(text.contains("gh release"), "the script must not publish")
        XCTAssertFalse(text.contains("curl "), "the script must not contact the feed")
    }

    // MARK: - The DMG is the enclosure

    /// The published DMG is mounted read-only, its app verified exactly like an
    /// unzipped one, detached, and the DMG itself is handed to
    /// `generate_appcast` so the EdDSA signature covers the published bytes.
    func testDmgArchiveIsMountedVerifiedDetachedAndStagedAsTheEnclosure() throws {
        let archive = try makeDmg(version: "0.1.2")
        let output = try outputPath()

        let result = try runScript([
            "--archive", archive.path,
            "--download-url", downloadPrefix,
            "--output", output.path,
        ])

        XCTAssertEqual(result.status, 0, result.output)
        let feed = try String(contentsOf: output, encoding: .utf8)
        XCTAssertTrue(
            feed.contains("url=\"\(downloadPrefix)Ciaobot-0.1.2.dmg\""),
            "the appcast enclosure must be the DMG that ships: \(feed)"
        )
        XCTAssertTrue(feed.contains(".dmg\""), "the enclosure URL must end in .dmg: \(feed)")

        let calls = try hdiutilCalls()
        let attach = try XCTUnwrap(calls.first { $0.first == "attach" }, "the DMG must be mounted: \(calls)")
        XCTAssertTrue(attach.contains("-readonly"), "verification must never mount read-write: \(attach)")
        XCTAssertTrue(attach.contains("-nobrowse"), "verification must not appear in Finder: \(attach)")
        XCTAssertEqual(attach.last, archive.path, "the DMG being published must be the one mounted: \(attach)")
        let mountIndex = try XCTUnwrap(attach.firstIndex(of: "-mountpoint"))
        let mountPoint = attach[mountIndex + 1]
        let detach = try XCTUnwrap(calls.first { $0.first == "detach" }, "the DMG must be detached: \(calls)")
        XCTAssertEqual(detach.last, mountPoint, "detach must name the mount point the script created")
        XCTAssertEqual(calls.filter { $0.first == "attach" }.count,
                       calls.filter { $0.first == "detach" }.count,
                       "a successful run mounts and detaches exactly once: \(calls)")
        XCTAssertEqual(calls.last?.first, "detach", "nothing may touch the volume after detaching: \(calls)")

        let probe = try String(contentsOf: XCTUnwrap(probeFile), encoding: .utf8)
        XCTAssertTrue(probe.contains("staged-files=Ciaobot-0.1.2.dmg"),
                      "the DMG must be staged under its published name: \(probe)")
        let archiveDigest = try shasum(archive)
        XCTAssertTrue(probe.contains("staged-sha256=\(archiveDigest)"),
                      "generate_appcast must receive the published DMG bytes, not a repack: \(probe)")
    }

    /// The enclosure name is derived from the archive name, so a DMG that is not
    /// named `Ciaobot-<version>.dmg` would advertise the wrong download.
    func testRefusesADmgWhoseNameIsNotCiaobotVersion() throws {
        let archive = try makeDmg(version: "0.1.2", named: "Ciaobot-0.1.2-notarized.dmg")
        let output = try outputPath()

        let result = try runScript([
            "--archive", archive.path,
            "--download-url", downloadPrefix,
            "--output", output.path,
        ])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("Ciaobot-<version>.dmg"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output.path))
        XCTAssertNil(try hdiutilCallsIfAny(), "a refused name must be rejected before anything is mounted")
        XCTAssertNil(try recordedArgumentsIfAny())
    }

    func testRefusesADmgWhoseNameDisagreesWithTheBundleVersion() throws {
        let archive = try makeDmg(version: "0.1.2", named: "Ciaobot-0.1.1.dmg")
        let output = try outputPath()

        let result = try runScript([
            "--archive", archive.path,
            "--download-url", downloadPrefix,
            "--output", output.path,
        ])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("version mismatch"), result.output)
        XCTAssertNil(try recordedArgumentsIfAny())
        XCTAssertEqual(try hdiutilCalls().filter { $0.first == "detach" }.count, 1,
                       "the mounted image must still be detached when the version disagrees")
    }

    func testRefusesAnUnstapledDmgAndStillDetachesIt() throws {
        let archive = try makeDmg(version: "0.1.2")
        let output = try outputPath()

        let result = try runScript(
            ["--archive", archive.path, "--download-url", downloadPrefix, "--output", output.path],
            environment: ["CIAOBOT_TEST_STAPLER_STATUS": "1"]
        )

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("not notarized"), result.output)
        XCTAssertNil(try recordedArgumentsIfAny(), "an unstapled image must never reach the appcast")
        let calls = try hdiutilCalls()
        XCTAssertEqual(calls.filter { $0.first == "attach" }.count, 1)
        XCTAssertEqual(calls.filter { $0.first == "detach" }.count, 1,
                       "the failure path must still detach: \(calls)")
    }

    /// A notarized DMG another team signed is still not this project's update.
    func testRefusesADmgSignedByAnotherTeamAndStillDetachesIt() throws {
        let archive = try makeDmg(version: "0.1.2")
        let output = try outputPath()

        let result = try runScript(
            ["--archive", archive.path, "--download-url", downloadPrefix, "--output", output.path],
            environment: [
                "CIAOBOT_TEST_CODESIGN_DESCRIPTION": codesignDescription(
                    authority: "Developer ID Application: Some Other Developer (ZZZZZZZZZZ)",
                    teamIdentifier: "ZZZZZZZZZZ"
                ),
            ]
        )

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("signed by team ZZZZZZZZZZ"), result.output)
        XCTAssertTrue(result.output.contains("AXBFS96Y9F"), result.output)
        XCTAssertNil(try recordedArgumentsIfAny(), "another team's image must never reach the appcast")
        XCTAssertEqual(try hdiutilCalls().filter { $0.first == "detach" }.count, 1,
                       "the mounted image must be detached even when its team is refused")
    }

    // MARK: - Refusals that need no archive

    func testRefusesANonHTTPSDownloadURL() throws {
        let archive = try makeArchive(version: "0.1.2")
        let output = try outputPath()

        let result = try runScript([
            "--archive", archive.path,
            "--download-url", "http://example.invalid/ciaobot/updates/",
            "--output", output.path,
        ])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("must be https"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output.path))
        XCTAssertNil(try recordedArgumentsIfAny(), "no tool may run for a refused URL")
    }

    func testRefusesACredentialBearingDownloadURL() throws {
        let archive = try makeArchive(version: "0.1.2")
        let output = try outputPath()

        let result = try runScript([
            "--archive", archive.path,
            "--download-url", "https://operator:token@example.invalid/ciaobot/updates/",
            "--output", output.path,
        ])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("credentials"), result.output)
        XCTAssertNil(try recordedArgumentsIfAny())
    }

    func testRefusesAPrefixWithoutATrailingSlash() throws {
        let archive = try makeArchive(version: "0.1.2")
        let output = try outputPath()

        let result = try runScript([
            "--archive", archive.path,
            "--download-url", "https://example.invalid/ciaobot/updates",
            "--output", output.path,
        ])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("must end with /"), result.output)
    }

    func testRefusesANonEmptyOutputWithoutOverwritingIt() throws {
        let archive = try makeArchive(version: "0.1.2")
        let output = try outputPath()
        try Data("keep this appcast\n".utf8).write(to: output)

        let result = try runScript([
            "--archive", archive.path,
            "--download-url", downloadPrefix,
            "--output", output.path,
        ])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("non-empty output"), result.output)
        XCTAssertEqual(try String(contentsOf: output, encoding: .utf8), "keep this appcast\n")
        XCTAssertNil(try recordedArgumentsIfAny(), "a refused output must stop before the tool runs")
    }

    // MARK: - Refusals about the archive's contents

    func testRefusesAForeignBundleIdentity() throws {
        let result = try runAgainstArchive(bundleIdentifier: "com.example.other")

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("not the Ciaobot app"), result.output)
        XCTAssertNil(try recordedArgumentsIfAny())
    }

    func testRefusesAVersionMismatchBetweenTheArchiveNameAndTheBundle() throws {
        // Named 0.1.1, but the bundle inside is 0.1.2.
        let archive = try makeArchive(version: "0.1.2", named: "Ciaobot-0.1.1-notarized.zip")
        let output = try outputPath()

        let result = try runScript([
            "--archive", archive.path,
            "--download-url", downloadPrefix,
            "--output", output.path,
        ])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("version mismatch"), result.output)
        XCTAssertNil(try recordedArgumentsIfAny())
    }

    func testRefusesANonArm64Archive() throws {
        let result = try runAgainstArchive(architectures: "arm64 x86_64")

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("thin arm64"), result.output)
        XCTAssertNil(try recordedArgumentsIfAny())
    }

    func testRefusesAnArchiveWithoutAValidSignature() throws {
        let result = try runAgainstArchive(environment: ["CIAOBOT_TEST_CODESIGN_STATUS": "1"])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("not validly signed"), result.output)
    }

    func testRefusesAnArchiveWithoutAStapledNotarizationTicket() throws {
        let result = try runAgainstArchive(environment: ["CIAOBOT_TEST_STAPLER_STATUS": "1"])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("not notarized"), result.output)
    }

    func testRefusesAnArchiveGatekeeperRejects() throws {
        let result = try runAgainstArchive(environment: ["CIAOBOT_TEST_SPCTL_STATUS": "1"])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("Gatekeeper"), result.output)
    }

    /// A valid, notarized, Gatekeeper-accepted archive signed by someone else is
    /// still not this project's update.
    func testRefusesAnArchiveSignedByAnotherTeam() throws {
        let result = try runAgainstArchive(environment: [
            "CIAOBOT_TEST_CODESIGN_DESCRIPTION": codesignDescription(
                authority: "Developer ID Application: Some Other Developer (ZZZZZZZZZZ)",
                teamIdentifier: "ZZZZZZZZZZ"
            ),
        ])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("signed by team ZZZZZZZZZZ"), result.output)
        XCTAssertTrue(result.output.contains("AXBFS96Y9F"), result.output)
        XCTAssertNil(try recordedArgumentsIfAny(), "an update signed by another team must never reach the appcast")
    }

    func testRefusesAnArchiveWithNoTeamIdentifier() throws {
        let result = try runAgainstArchive(environment: [
            "CIAOBOT_TEST_CODESIGN_DESCRIPTION": codesignDescription(teamIdentifier: nil),
        ])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("no TeamIdentifier"), result.output)
        XCTAssertNil(try recordedArgumentsIfAny())
    }

    func testRefusesANonDeveloperIDAuthority() throws {
        let result = try runAgainstArchive(environment: [
            "CIAOBOT_TEST_CODESIGN_DESCRIPTION": codesignDescription(
                authority: "Apple Development: Raffaele Farinaro (AXBFS96Y9F)"
            ),
        ])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("Developer ID Application"), result.output)
        XCTAssertNil(try recordedArgumentsIfAny())
    }

    // MARK: - Interface

    func testUnknownArgumentsAreRejected() throws {
        let result = try runScript(["--publish"])

        XCTAssertEqual(result.status, 2, result.output)
        XCTAssertTrue(result.output.contains("unknown argument: --publish"), result.output)
    }

    func testScriptIsExecutable() {
        XCTAssertTrue(
            fileManager.isExecutableFile(atPath: scriptsDirectory.appendingPathComponent("make-appcast.sh").path),
            "scripts/make-appcast.sh must keep its executable bit"
        )
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

    private func scriptText() throws -> String {
        try String(contentsOf: scriptsDirectory.appendingPathComponent("make-appcast.sh"), encoding: .utf8)
    }

    private func runAgainstArchive(
        bundleIdentifier: String = "local.ciaobot.mac",
        architectures: String = "arm64",
        environment: [String: String] = [:]
    ) throws -> (status: Int32, output: String) {
        let archive = try makeArchive(version: "0.1.2", bundleIdentifier: bundleIdentifier)
        let output = try outputPath()
        var overrides = environment
        overrides["CIAOBOT_TEST_ARCHS"] = architectures
        return try runScript(
            [
                "--archive", archive.path,
                "--download-url", downloadPrefix,
                "--output", output.path,
            ],
            environment: overrides
        )
    }

    /// The `codesign -d --verbose=4` description of a Developer ID signed app,
    /// trimmed to the lines the script reads.
    private func codesignDescription(
        authority: String = "Developer ID Application: Raffaele Farinaro (AXBFS96Y9F)",
        teamIdentifier: String? = "AXBFS96Y9F"
    ) -> String {
        var lines = [
            "Executable=/tmp/Ciaobot.app/Contents/MacOS/CiaobotMac",
            "Identifier=local.ciaobot.mac",
            "Format=app bundle with Mach-O thin (arm64)",
            "Authority=\(authority)",
            "Authority=Developer ID Certification Authority",
            "Authority=Apple Root CA",
            "Timestamp=Oct 30, 2026 at 12:00:00",
        ]
        if let teamIdentifier {
            lines.append("TeamIdentifier=\(teamIdentifier)")
        }
        return lines.joined(separator: "\n")
    }

    private func outputPath() throws -> URL {        let directory = scratch.appendingPathComponent("out", isDirectory: true)
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory.appendingPathComponent("appcast.xml")
    }

    /// Builds the packaged app the script inspects, then a stand-in archive.
    /// The fake `ditto` "extracts" the app directory; the archive itself only
    /// has to exist and be non-empty.
    private func makeArchive(
        version: String,
        named name: String? = nil,
        bundleIdentifier: String = "local.ciaobot.mac"
    ) throws -> URL {
        try makeAppFixture(version: version, bundleIdentifier: bundleIdentifier)
        let archive = scratch.appendingPathComponent(name ?? "Ciaobot-\(version)-notarized.zip")
        try Data("archive placeholder".utf8).write(to: archive)
        return archive
    }

    /// A stand-in disk image. The fake `hdiutil` mounts the same fixture app the
    /// fake `ditto` extracts, so the DMG path is verified without a real image.
    private func makeDmg(
        version: String,
        named name: String? = nil,
        bundleIdentifier: String = "local.ciaobot.mac"
    ) throws -> URL {
        try makeAppFixture(version: version, bundleIdentifier: bundleIdentifier)
        let archive = scratch.appendingPathComponent(name ?? "Ciaobot-\(version).dmg")
        try Data("disk image placeholder".utf8).write(to: archive)
        return archive
    }

    private func makeAppFixture(version: String, bundleIdentifier: String) throws {
        let app = scratch.appendingPathComponent("fixture/Ciaobot.app", isDirectory: true)
        let contents = app.appendingPathComponent("Contents", isDirectory: true)
        let macos = contents.appendingPathComponent("MacOS", isDirectory: true)
        try fileManager.createDirectory(at: macos, withIntermediateDirectories: true)
        let plist = """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0"><dict>
        <key>CFBundleExecutable</key><string>CiaobotMac</string>
        <key>CFBundleIdentifier</key><string>\(bundleIdentifier)</string>
        <key>CFBundlePackageType</key><string>APPL</string>
        <key>CFBundleShortVersionString</key><string>\(version)</string>
        <key>CFBundleVersion</key><string>3</string>
        </dict></plist>
        """
        try Data(plist.utf8).write(to: contents.appendingPathComponent("Info.plist"))
        try Data("#!/bin/sh\nexit 0\n".utf8).write(to: macos.appendingPathComponent("CiaobotMac"))
    }

    private func shasum(_ url: URL) throws -> String {
        let result = try run("/usr/bin/shasum", ["-a", "256", url.path])
        XCTAssertEqual(result.status, 0, result.output)
        return String(result.output.prefix(64))
    }

    private func hdiutilCalls() throws -> [[String]] {
        try calls(in: XCTUnwrap(hdiutilLog))
    }

    /// `nil` when the script never invoked `hdiutil` at all.
    private func hdiutilCallsIfAny() throws -> [[String]]? {
        guard let hdiutilLog, fileManager.fileExists(atPath: hdiutilLog.path) else { return nil }
        let calls = try calls(in: hdiutilLog)
        return calls.isEmpty ? nil : calls
    }

    private func calls(in log: URL) throws -> [[String]] {
        let text = try String(contentsOf: log, encoding: .utf8)
        return text.split(separator: "\n").map { line in
            line.split(separator: "|").map(String.init)
        }
    }

    private var hdiutilLog: URL? {
        scratch?.appendingPathComponent("hdiutil.log")
    }

    private func recordedArguments() throws -> [String] {
        let url = try XCTUnwrap(argsFile)
        let text = try String(contentsOf: url, encoding: .utf8)
        return text.split(separator: "\n").map(String.init)
    }

    private func recordedArgumentsIfAny() throws -> [String]? {
        guard let argsFile, fileManager.fileExists(atPath: argsFile.path) else { return nil }
        return try recordedArguments()
    }

    private var argsFile: URL? {
        scratch?.appendingPathComponent("generate-appcast-args.txt")
    }

    private var probeFile: URL? {
        scratch?.appendingPathComponent("generate-appcast-probe.txt")
    }

    private func makeTool(_ name: String, body: String) throws {
        let url = tools.appendingPathComponent(name)
        try Data(body.utf8).write(to: url)
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
    }

    @discardableResult
    private func runScript(
        _ arguments: [String],
        environment: [String: String] = [:]
    ) throws -> (status: Int32, output: String) {
        var childEnvironment = ProcessInfo.processInfo.environment
        childEnvironment["PATH"] = tools.path + ":" + (childEnvironment["PATH"] ?? "/usr/bin:/bin")
        // Keep the real keychain and the real pinned tool out of this run.
        childEnvironment["CIAOBOT_GENERATE_APPCAST"] = tools.appendingPathComponent("generate_appcast").path
        childEnvironment["CIAOBOT_TEST_APP"] = scratch.appendingPathComponent("fixture/Ciaobot.app").path
        childEnvironment["CIAOBOT_TEST_APPCAST_ARGS"] = argsFile?.path ?? ""
        childEnvironment["CIAOBOT_TEST_APPCAST_PROBE"] = probeFile?.path ?? ""
        childEnvironment["CIAOBOT_TEST_CODESIGN_DESCRIPTION"] = codesignDescription()
        childEnvironment["CIAOBOT_TEST_HDIUTIL_LOG"] = hdiutilLog?.path ?? ""
        for (key, value) in environment { childEnvironment[key] = value }
        return try run(
            "/bin/sh",
            [scriptsDirectory.appendingPathComponent("make-appcast.sh").path] + arguments,
            environment: childEnvironment
        )
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
