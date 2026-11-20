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
        last=
        for argument in "$@"; do last=$argument; done
        case $last in
            */Contents/Resources/engine/*)
                # Engine fixtures carry their own verdict: a marker in the file
                # makes the double report the ad-hoc, team-less signature the
                # distribution check must refuse; any other engine file reports
                # a full distribution signature of the team in
                # release/update-config.env.
                if /usr/bin/grep -aq 'CIAOBOT-TEST-ADHOC' "$last" 2>/dev/null; then
                    printf '%s\\n' 'CodeDirectory v=20400 size=999 flags=0x2(adhoc) hashes=3+7 location=embedded' >&2
                    printf '%s\\n' 'Signature=adhoc' >&2
                    printf '%s\\n' 'TeamIdentifier=not set' >&2
                else
                    printf '%s\\n' 'CodeDirectory v=20500 size=1000 flags=0x10000(runtime) hashes=3+7 location=embedded' >&2
                    printf '%s\\n' 'Authority=Developer ID Application: Test Signer (AXBFS96Y9F)' >&2
                    printf '%s\\n' 'Authority=Developer ID Certification Authority' >&2
                    printf '%s\\n' 'Authority=Apple Root CA' >&2
                    printf '%s\\n' 'Timestamp=Nov 18, 2026 at 10:00:00' >&2
                    printf '%s\\n' 'TeamIdentifier=AXBFS96Y9F' >&2
                fi
                exit 0
                ;;
            *) ;;
        esac
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
                archive=
                while [ $# -gt 0 ]; do
                    case $1 in
                        -mountpoint) mountpoint=$2; shift 2 ;;
                        -*) shift ;;
                        *) archive=$1; shift ;;
                    esac
                done
                [ -n "$mountpoint" ] || exit 1
                [ "${CIAOBOT_TEST_HDIUTIL_ATTACH_STATUS:-0}" = 0 ] || exit "${CIAOBOT_TEST_HDIUTIL_ATTACH_STATUS}"
                mkdir -p "$mountpoint" || exit 1
                cp -R "$CIAOBOT_TEST_APP" "$mountpoint/Ciaobot.app" || exit 1
                if [ "${CIAOBOT_TEST_ARCHIVE_APPEND:-0}" = 1 ] && [ -n "$archive" ]; then
                    # A swap between the inspection and the staging copy: the
                    # archive's bytes change while its image is mounted.
                    printf 'swapped\\n' >> "$archive"
                fi
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

    /// A bundled engine that is not distribution-signed (#3128 G; here an empty directory) or is not a real
    /// directory (a dangling symlink standing in for it) must not become an update.
    func testRefusesAnArchiveThatBundlesTheEngine() throws {
        for dangling in [false, true] {
            let archive = try makeDmg(version: "0.1.2")
            let engine = scratch.appendingPathComponent("fixture/Ciaobot.app/Contents/Resources/engine")
            try fileManager.createDirectory(at: engine.deletingLastPathComponent(), withIntermediateDirectories: true)
            if dangling {
                try fileManager.createSymbolicLink(atPath: engine.path, withDestinationPath: "/nonexistent-engine")
            } else {
                try fileManager.createDirectory(at: engine, withIntermediateDirectories: true)
            }
            let output = try outputPath()

            let result = try runScript(
                ["--archive", archive.path, "--download-url", downloadPrefix, "--output", output.path]
            )

            XCTAssertNotEqual(result.status, 0, "dangling=\(dangling): \(result.output)")
            XCTAssertTrue(
                result.output.contains(dangling ? "not a real directory" : "not distribution-signed"), result.output)
            XCTAssertNil(try recordedArgumentsIfAny())
            try fileManager.removeItem(at: engine)
        }
    }

    // MARK: - The bundled engine

    /// An engine-bearing archive whose engine is only ad-hoc signed must not be
    /// advertised: the fake codesign reports `Signature=adhoc` and no team for
    /// the engine files, so `engine_sign.py check --distribution` refuses it
    /// (#3128 G). Weakening that check to a plain `check` — which accepts
    /// ad-hoc — or dropping the team argument makes the acceptance test below
    /// (and the whole gate) fail.
    func testRefusesAnArchiveWhoseEngineIsOnlyAdHocSigned() throws {
        let repo = try makeScriptRepo()
        let archive = try makeDmg(version: "0.1.2", engine: .adHocSigned)
        let output = try outputPath()

        let result = try runScript(
            ["--archive", archive.path, "--download-url", downloadPrefix, "--output", output.path],
            scriptsRoot: repo
        )

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("not distribution-signed"), result.output)
        XCTAssertTrue(result.output.contains("AXBFS96Y9F"), result.output)
        XCTAssertNil(try recordedArgumentsIfAny(), "a refused engine must never reach the appcast")
        let calls = try hdiutilCalls()
        XCTAssertEqual(calls.filter { $0.first == "detach" }.count, 1,
                       "the mounted image must be detached on the refusal: \(calls)")
    }

    /// The positive half of the gate: an archive whose engine files carry a full
    /// distribution signature (team, hardened runtime, secure timestamp) is
    /// advertised. This exercises the real `python3 >= 3.12` lookup and the team
    /// plumbing from `release/update-config.env`, and it fails when
    /// `check --distribution` is weakened to a plain `check` (which demands an
    /// ad-hoc signature instead) or the team argument is dropped.
    func testAcceptsAnArchiveWhoseEngineIsDistributionSigned() throws {
        let repo = try makeScriptRepo()
        let archive = try makeDmg(version: "0.1.2", engine: .distributionSigned)
        let output = try outputPath()

        let result = try runScript(
            ["--archive", archive.path, "--download-url", downloadPrefix, "--output", output.path],
            scriptsRoot: repo
        )

        XCTAssertEqual(result.status, 0, result.output)
        XCTAssertTrue(fileManager.fileExists(atPath: output.path), result.output)
        let feed = try String(contentsOf: output, encoding: .utf8)
        XCTAssertTrue(feed.contains("url=\"\(downloadPrefix)Ciaobot-0.1.2.dmg\""),
                      "the enclosure stays the published DMG: \(feed)")
        let calls = try hdiutilCalls()
        XCTAssertEqual(calls.filter { $0.first == "attach" }.count,
                       calls.filter { $0.first == "detach" }.count,
                       "the mounted image must be detached: \(calls)")
    }

    /// The enclosure must be the bytes the checks verified: the archive is
    /// hashed before it is mounted, and the staged copy is hashed against that
    /// digest. When the double swaps bytes into the archive while its image is
    /// mounted — between inspection and staging — the run refuses and nothing
    /// reaches `generate_appcast`.
    func testRefusesAnArchiveThatChangedWhileItWasInspected() throws {
        let archive = try makeDmg(version: "0.1.2")
        let output = try outputPath()

        let result = try runScript(
            ["--archive", archive.path, "--download-url", downloadPrefix, "--output", output.path],
            environment: ["CIAOBOT_TEST_ARCHIVE_APPEND": "1"]
        )

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(
            result.output.contains("changed while it was being inspected"), result.output)
        XCTAssertNotNil(try hdiutilCallsIfAny(), "the changed bytes are caught after the inspection")
        XCTAssertNil(try recordedArgumentsIfAny(), "a swapped archive must never reach the appcast")
        XCTAssertFalse(fileManager.fileExists(atPath: output.path))
        let calls = try hdiutilCalls()
        XCTAssertEqual(calls.filter { $0.first == "detach" }.count, 1,
                       "the mounted image must still be detached: \(calls)")
    }

    /// The engine check is driven by a real `python3` with the same gate the
    /// operator's runs face: a `python3` that is older than 3.12 on PATH is
    /// refused before the appcast can be generated.
    func testRefusesAnEngineCheckWithATooOldPython() throws {
        try makeTool("python3", body: "#!/bin/sh\nexit 1\n")
        let repo = try makeScriptRepo()
        let archive = try makeDmg(version: "0.1.2", engine: .distributionSigned)
        let output = try outputPath()

        let result = try runScript(
            ["--archive", archive.path, "--download-url", downloadPrefix, "--output", output.path],
            scriptsRoot: repo
        )

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("python3 3.12 or newer is required"), result.output)
        XCTAssertNil(try recordedArgumentsIfAny())
    }

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
        bundleIdentifier: String = "local.ciaobot.mac",
        engine: EngineFixture = .none
    ) throws -> URL {
        try makeAppFixture(version: version, bundleIdentifier: bundleIdentifier, engine: engine)
        let archive = scratch.appendingPathComponent(name ?? "Ciaobot-\(version)-notarized.zip")
        try Data("archive placeholder".utf8).write(to: archive)
        return archive
    }

    /// A stand-in disk image. The fake `hdiutil` mounts the same fixture app the
    /// fake `ditto` extracts, so the DMG path is verified without a real image.
    private func makeDmg(
        version: String,
        named name: String? = nil,
        bundleIdentifier: String = "local.ciaobot.mac",
        engine: EngineFixture = .none
    ) throws -> URL {
        try makeAppFixture(version: version, bundleIdentifier: bundleIdentifier, engine: engine)
        let archive = scratch.appendingPathComponent(name ?? "Ciaobot-\(version).dmg")
        try Data("disk image placeholder".utf8).write(to: archive)
        return archive
    }

    private func makeAppFixture(
        version: String,
        bundleIdentifier: String,
        engine: EngineFixture = .none
    ) throws {
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
        if engine != .none {
            try makeEngine(at: app, adHocSigned: engine == .adHocSigned)
        }
    }

    /// A thin 64-bit arm64 Mach-O header: enough for the engine scripts' magic
    /// test, and bytes no one executes, so no real identity is ever used.
    private static let machoBytes = Data([
        0xcf, 0xfa, 0xed, 0xfe, 0x0c, 0x00, 0x00, 0x01,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    ])
    /// Marks an engine fixture file the codesign double must describe as an
    /// ad-hoc, team-less signature instead of a distribution one.
    private static let adHocMarker = "CIAOBOT-TEST-ADHOC\n"
    private static let engineProvenance = #"{"kind":"ciaobot-engine-provenance","engine":{"version":"1.2.3"}}"#

    private enum EngineFixture { case none, adHocSigned, distributionSigned }

    /// A bundled engine whose two Mach-O files carry, per file, either a full
    /// distribution signature description or, with `adHocSigned` (encoded by
    /// the marker the fake codesign reads out of the file), the ad-hoc
    /// signature the distribution gate must refuse.
    private func makeEngine(at app: URL, adHocSigned: Bool) throws {
        let engine = app.appendingPathComponent("Contents/Resources/engine", isDirectory: true)
        try fileManager.createDirectory(
            at: engine.appendingPathComponent("bin"), withIntermediateDirectories: true)
        try Data("#!/bin/sh\necho \"ciao 1.2.3\"\n".utf8)
            .write(to: engine.appendingPathComponent("bin/ciao"))
        try fileManager.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: engine.appendingPathComponent("bin/ciao").path)
        for rel in ["bin/python3.12", "lib/a.so"] {
            let target = engine.appendingPathComponent(rel)
            try fileManager.createDirectory(
                at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
            var bytes = Self.machoBytes
            if adHocSigned { bytes.append(contentsOf: Data(Self.adHocMarker.utf8)) }
            try bytes.write(to: target)
            if rel == "bin/python3.12" {
                try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: target.path)
            }
        }
        try Data(Self.engineProvenance.utf8).write(to: engine.appendingPathComponent("provenance.json"))
    }

    /// A scratch checkout of the scripts and the team config the engine tests
    /// need. `engine_sign.py` pins `/usr/bin/codesign` by path on purpose, so
    /// the copied module is pointed at this suite's fake instead — the same
    /// seam the packaging fixture uses. No test touches the real scripts.
    /// Returns the copied scripts directory, the root `runScript` wants.
    private func makeScriptRepo() throws -> URL {
        let repo = scratch.appendingPathComponent("repo", isDirectory: true)
        let scripts = repo.appendingPathComponent("scripts", isDirectory: true)
        try fileManager.createDirectory(at: scripts, withIntermediateDirectories: true)
        try fileManager.copyItem(
            at: scriptsDirectory.appendingPathComponent("make-appcast.sh"), to: scripts.appendingPathComponent("make-appcast.sh"))
        // engine_sign.py loads its sibling helpers by path, transitively; copy
        // every one of them, exactly like the packaging fixture does.
        let helpers = try fileManager.contentsOfDirectory(atPath: scriptsDirectory.path)
            .filter { $0.hasPrefix("engine_") && $0.hasSuffix(".py") }
        for name in helpers {
            try fileManager.copyItem(
                at: scriptsDirectory.appendingPathComponent(name), to: scripts.appendingPathComponent(name))
        }
        try fileManager.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: scripts.appendingPathComponent("make-appcast.sh").path)
        try fileManager.createDirectory(
            at: repo.appendingPathComponent("release", isDirectory: true), withIntermediateDirectories: true)
        try fileManager.copyItem(
            at: packageRoot.appendingPathComponent("release/update-config.env"),
            to: repo.appendingPathComponent("release/update-config.env"))
        let signer = scripts.appendingPathComponent("engine_sign.py")
        let pinned = #"CODESIGN = "/usr/bin/codesign""#
        let source = try String(contentsOf: signer, encoding: .utf8)
        XCTAssertTrue(source.contains(pinned), "engine_sign.py's pinned codesign path is the patch seam")
        try source.replacingOccurrences(
            of: pinned, with: "CODESIGN = \"\(tools.appendingPathComponent("codesign").path)\"")
            .write(to: signer, atomically: true, encoding: .utf8)
        return scripts
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
        environment: [String: String] = [:],
        scriptsRoot: URL? = nil
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
            [(scriptsRoot ?? scriptsDirectory).appendingPathComponent("make-appcast.sh").path] + arguments,
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
