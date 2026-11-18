import CryptoKit
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
                    printf '%s\\n' 'Identifier=local.ciaobot.mac' >&2
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
        printf '%s\\n' "Identifier=local.ciaobot.mac" >&2
        printf '%s\\n' "Authority=${FAKE_CODESIGN_AUTHORITY:-Developer ID Application: Test Signer (AXBFS96Y9F)}" >&2
        if [ "${FAKE_CODESIGN_NO_TEAM:-0}" = 1 ]; then
            printf '%s\\n' "TeamIdentifier=not set" >&2
        else
            printf '%s\\n' "TeamIdentifier=${FAKE_CODESIGN_TEAM:-AXBFS96Y9F}" >&2
        fi
        exit 0
        """)
        try makeTool("ditto", body: """
        #!/bin/sh
        /usr/bin/ditto "$@" || exit 1
        [ "${FAKE_DITTO_SWAP_ENGINE:-0}" = 1 ] || exit 0
        # A swap between the source engine check and the copy: the staged engine
        # file ends up with bytes the codesign double reports as ad-hoc signed.
        staged_engine_file=$2/Contents/Resources/engine/bin/python3.12
        [ -f "$staged_engine_file" ] || exit 0
        printf 'CIAOBOT-TEST-ADHOC\\n' >> "$staged_engine_file"
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

    /// A bundled engine that is not distribution-signed (#3128 G; here an empty directory) or is not a real
    /// directory (a dangling symlink standing in for it) must not become an image.
    func testRefusesAnEngineBearingApp() throws {
        for dangling in [false, true] {
            let app = try makeApp()
            let engine = app.appendingPathComponent("Contents/Resources/engine")
            try fileManager.createDirectory(at: engine.deletingLastPathComponent(), withIntermediateDirectories: true)
            if dangling {
                try fileManager.createSymbolicLink(atPath: engine.path, withDestinationPath: "/nonexistent-engine")
            } else {
                try fileManager.createDirectory(at: engine.appendingPathComponent("bin"), withIntermediateDirectories: true)
            }
            let output = try outputPath()

            let result = try runScript(["--app", app.path, "--output", output, "--identity", identity])

            XCTAssertNotEqual(result.status, 0, "dangling=\(dangling): \(result.output)")
            XCTAssertTrue(
                result.output.contains(dangling ? "not a real directory" : "not distribution-signed"), result.output)
            XCTAssertFalse(fileManager.fileExists(atPath: output))
            XCTAssertNil(try hdiutilLogIfAny())
            try fileManager.removeItem(at: app.deletingLastPathComponent())
        }
    }

    // MARK: - The bundled engine

    /// An engine-bearing app whose engine is only ad-hoc signed must not become
    /// an image: the fake codesign reports `Signature=adhoc` and no team for the
    /// engine files, so `engine_sign.py check --distribution` refuses it
    /// (#3128 G). Weakening that check to a plain `check` — which accepts
    /// ad-hoc — or dropping the team argument makes this test fail.
    func testRefusesAnEngineBearingAppWhoseEngineIsOnlyAdHocSigned() throws {
        let app = try makeApp()
        try makeEngine(at: app, adHocSigned: true)
        let repo = try makeScriptRepo()
        let output = try outputPath()

        let result = try runScript(
            ["--app", app.path, "--output", output, "--identity", identity],
            scriptsRoot: repo
        )

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("not distribution-signed"), result.output)
        XCTAssertTrue(result.output.contains("AXBFS96Y9F"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output), "a refused engine must publish no image")
        XCTAssertNil(try hdiutilLogIfAny(), "the source engine is refused before any image work")
    }

    /// The positive half of the gate: an engine that is a full distribution
    /// signature (team, hardened runtime, secure timestamp per file) is accepted
    /// end to end. This is the path that exercises the real `python3 >= 3.12`
    /// lookup and the team plumbing from `release/update-config.env`, and it
    /// fails if `--distribution` is weakened to a plain `check` (which demands
    /// an ad-hoc signature instead) or the team argument is dropped.
    func testAcceptsAnEngineBearingAppWhoseEngineIsDistributionSigned() throws {
        let app = try makeApp()
        try makeEngine(at: app)
        let repo = try makeScriptRepo()
        let output = try outputPath()

        let result = try runScript(
            ["--app", app.path, "--output", output, "--identity", identity],
            scriptsRoot: repo
        )

        XCTAssertEqual(result.status, 0, result.output)
        XCTAssertTrue(fileManager.fileExists(atPath: output), result.output)
        // The engine was checked where the bytes are used: the source app, the
        // staged copy right before the image, and the app on the mounted image.
        let describes = try codesignCalls().filter {
            $0.contains("-d") && ($0.last ?? "").contains("/Contents/Resources/engine/")
        }
        let allCalls = try codesignCalls()
        XCTAssertGreaterThanOrEqual(
            describes.count, 6,
            "both engine Mach-O files are described by the source, staged and mounted checks: \(allCalls)"
        )
    }

    /// The image is built from a fresh `ditto` copy staged after the source
    /// engine check, so the staged copy is checked again right before the image
    /// is created. When the double mutates the staged engine between the two,
    /// the swap must be refused and nothing may be published.
    func testRefusesAStagedEngineWhoseBytesChangedAfterTheCheck() throws {
        let app = try makeApp()
        try makeEngine(at: app)
        let repo = try makeScriptRepo()
        let output = try outputPath()

        let result = try runScript(
            ["--app", app.path, "--output", output, "--identity", identity],
            environment: ["FAKE_DITTO_SWAP_ENGINE": "1"],
            scriptsRoot: repo
        )

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(
            result.output.contains("the staged engine is not distribution-signed"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output), "a swapped staged engine must publish no image")
        XCTAssertNil(try hdiutilLogIfAny(), "a refused staging tree is caught before any image work")
    }

    /// The image's own copy is what a user mounts, so the mounted app is
    /// engine-checked too: when the mounted engine differs from the staged one
    /// (the double mutates the mounted file), the run refuses and detaches.
    func testRefusesAMountedEngineWhoseBytesChanged() throws {
        let app = try makeApp()
        try makeEngine(at: app)
        let repo = try makeScriptRepo()
        let output = try outputPath()

        let result = try runScript(
            ["--app", app.path, "--output", output, "--identity", identity],
            environment: ["FAKE_HDIUTIL_SWAP_ENGINE": "1"],
            scriptsRoot: repo
        )

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(
            result.output.contains("the image's engine is not distribution-signed"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output), "a swapped mounted engine must publish no image")
        let calls = try hdiutilCalls()
        XCTAssertEqual(calls.filter { $0.first == "detach" }.count, 1,
                       "the failure path must still detach: \(calls)")
    }

    /// The engine checks are driven by a real `python3` with the same gate the
    /// operator's runs face: a `python3` that is older than 3.12 on PATH is
    /// refused before any image work.
    func testRefusesAnEngineCheckWithATooOldPython() throws {
        try makeTool("python3", body: "#!/bin/sh\nexit 1\n")
        let app = try makeApp()
        try makeEngine(at: app)
        let repo = try makeScriptRepo()
        let output = try outputPath()

        let result = try runScript(
            ["--app", app.path, "--output", output, "--identity", identity],
            scriptsRoot: repo
        )

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("python3 3.12 or newer is required"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output))
        XCTAssertNil(try hdiutilLogIfAny())
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
        XCTAssertTrue(probe.contains("create-stage-dsstore=yes"),
                      "the staged volume must carry the Finder layout: \(probe)")
        XCTAssertTrue(probe.contains("create-stage-background=yes"),
                      "the staged volume must carry the hidden background picture: \(probe)")
        XCTAssertEqual(
            try Data(contentsOf: hdiutilState.appendingPathComponent("stage.DS_Store")),
            try layoutBytes(),
            "the staged .DS_Store must be exactly what scripts/dmg_layout.py generates"
        )
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

    /// A volume that lost its Finder layout is refused, and still detached.
    func testRefusesAVolumeWithoutItsFinderLayout() throws {
        let app = try makeApp()
        let output = try outputPath()

        let result = try runScript(
            ["--app", app.path, "--output", output, "--identity", identity],
            environment: ["FAKE_HDIUTIL_SKIP_LAYOUT": "1"]
        )

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("Finder layout"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output), "a failed inspection must publish nothing")

        let calls = try hdiutilCalls()
        XCTAssertEqual(calls.filter { $0.first == "attach" }.count, 1)
        XCTAssertEqual(calls.filter { $0.first == "detach" }.count, 1,
                       "the failure path must still detach: \(calls)")
        XCTAssertEqual(calls.last?.first, "detach", "detach must be the last image operation: \(calls)")
    }

    /// Each mounted layout file is compared or checked on its own; every case
    /// fails with its own message, publishes nothing and still detaches.
    private func assertMountedLayoutRefused(
        _ knob: String, message: String, file: StaticString = #filePath, line: UInt = #line
    ) throws {
        let app = try makeApp()
        let output = try outputPath()

        let result = try runScript(
            ["--app", app.path, "--output", output, "--identity", identity],
            environment: [knob: "1"]
        )

        XCTAssertNotEqual(result.status, 0, result.output, file: file, line: line)
        XCTAssertTrue(result.output.contains(message), result.output, file: file, line: line)
        XCTAssertFalse(fileManager.fileExists(atPath: output), "a failed inspection must publish nothing",
                       file: file, line: line)
        let calls = try hdiutilCalls()
        XCTAssertEqual(calls.filter { $0.first == "detach" }.count, 1,
                       "the failure path must still detach: \(calls)", file: file, line: line)
        XCTAssertEqual(calls.last?.first, "detach", file: file, line: line)
    }

    func testRefusesACorruptedMountedDSStore() throws {
        try assertMountedLayoutRefused(
            "FAKE_HDIUTIL_CORRUPT_DSSTORE", message: "differs from scripts/dmg_layout.py")
    }

    func testRefusesAMissingBackgroundPictureWithAValidDSStore() throws {
        try assertMountedLayoutRefused("FAKE_HDIUTIL_SKIP_BACKGROUND", message: "no background picture")
    }

    func testRefusesACorruptedMountedBackgroundPicture() throws {
        try assertMountedLayoutRefused(
            "FAKE_HDIUTIL_CORRUPT_BACKGROUND", message: "background picture differs")
    }

    /// When the layout cannot be written, no image is created at all.
    func testALayoutFailureCreatesNoImage() throws {
        try makeTool("tiffutil", body: "#!/bin/sh\nexit 1\n")
        let app = try makeApp()
        let output = try outputPath()

        let result = try runScript(["--app", app.path, "--output", output, "--identity", identity])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("Finder layout"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output))
        XCTAssertNil(try hdiutilLogIfAny(), "no hdiutil call may follow a failed layout step")
        let leftovers = try fileManager.contentsOfDirectory(atPath: outputDirectory.path)
        XCTAssertEqual(leftovers, [String](), "the run must leave no staging directory behind: \(leftovers)")
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

        // The published image carries the layout the way Finder will read it.
        let inspect = scratch.appendingPathComponent("inspect", isDirectory: true)
        try fileManager.createDirectory(at: inspect, withIntermediateDirectories: true)
        let attach = try run(hdiutil, ["attach", "-readonly", "-nobrowse", "-mountpoint", inspect.path, output])
        XCTAssertEqual(attach.status, 0, attach.output)
        defer { _ = try? run(hdiutil, ["detach", inspect.path]) }
        XCTAssertEqual(try Data(contentsOf: inspect.appendingPathComponent(".DS_Store")), try layoutBytes(),
                       "the mounted .DS_Store must match scripts/dmg_layout.py")
        let background = inspect.appendingPathComponent(".background.tiff")
        let representations = try XCTUnwrap(NSBitmapImageRep.imageReps(withContentsOf: background))
            .compactMap { $0 as? NSBitmapImageRep }
        XCTAssertEqual(
            representations.map { [$0.pixelsWide, $0.pixelsHigh] }.sorted { $0[0] < $1[0] },
            [[640, 400], [1280, 800]],
            "the hidden background must hold the 1x and 2x pictures"
        )
        var isDirectory: ObjCBool = false
        XCTAssertTrue(fileManager.fileExists(atPath: inspect.appendingPathComponent("Ciaobot.app").path,
                                             isDirectory: &isDirectory) && isDirectory.boolValue)
        XCTAssertEqual(try fileManager.destinationOfSymbolicLink(
            atPath: inspect.appendingPathComponent("Applications").path), "/Applications")
        XCTAssertEqual(try run(hdiutil, ["detach", inspect.path]).status, 0)
        let after = try run(hdiutil, ["info"])
        XCTAssertFalse(after.output.contains(scratch.path), "a scratch volume is still mounted: \(after.output)")
    }

    // MARK: - Background art

    private let pinnedArt: [(name: String, sha256: String, pixels: Int)] = [
        ("background.png", "bd8af3f9bdbdcc7e83a91652733d00c49acdb75f779978e000b80fa07cab278a", 1),
        ("background@2x.png", "8f5cbd2748dbde7ea7291c0c9935c607b6ca3bb142b7bd2d0e80ff9245fc8333", 2),
    ]

    /// The committed art is the output of `scripts/make-dmg-background.swift`;
    /// regenerate it only when the art changes and update these hashes.
    func testBackgroundArtIsThePinnedPickedOption() throws {
        let geometry = try layoutGeometry()
        let plates = try XCTUnwrap(geometry["plates"] as? [String: [String: Int]])
        for art in pinnedArt {
            let url = packageRoot.appendingPathComponent("release/dmg/\(art.name)")
            let data = try Data(contentsOf: url)
            XCTAssertEqual(SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined(), art.sha256,
                           "\(art.name) changed; regenerate with make-dmg-background.swift and update the pin")
            let rep = try XCTUnwrap(NSBitmapImageRep(data: data))
            XCTAssertEqual(rep.pixelsWide, 640 * art.pixels, art.name)
            XCTAssertEqual(rep.pixelsHigh, 400 * art.pixels, art.name)
            XCTAssertFalse(rep.hasAlpha, "\(art.name) must be opaque")
            // Finder draws every icon label in black or white; the plate behind
            // it keeps 4.5:1 against both.
            for key in ["app", "applications"] {
                let plate = try XCTUnwrap(plates[key])
                let x = (plate["x"]! + plate["width"]! / 2) * art.pixels
                let y = (plate["y"]! + plate["height"]! / 2) * art.pixels
                // Raw samples: the PNG is sRGB, and the plate colour is an sRGB value.
                var samples = [Int](repeating: 0, count: 5)
                rep.getPixel(&samples, atX: x, y: y)
                for (actual, want) in zip(samples.prefix(3), [0x6d, 0x73, 0x9c]) {
                    XCTAssertEqual(actual, want, accuracy: 2, "\(art.name) plate \(key)")
                }
            }
        }
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
    private static let provenance = #"{"kind":"ciaobot-engine-provenance","engine":{"version":"1.2.3"}}"#

    /// A bundled engine whose two Mach-O files carry, per file, either a full
    /// distribution signature description (the default) or, with `adHocSigned`
    /// (encoded by the marker the fake codesign reads out of the file), the
    /// ad-hoc signature the distribution gate must refuse.
    @discardableResult
    private func makeEngine(at app: URL, adHocSigned: Bool = false) throws -> URL {
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
        try Data(Self.provenance.utf8).write(to: engine.appendingPathComponent("provenance.json"))
        return engine
    }

    /// A scratch checkout of the scripts and release inputs the engine tests
    /// need. `engine_sign.py` pins `/usr/bin/codesign` by path on purpose, so
    /// the copied module is pointed at this suite's fake instead — the same
    /// seam the packaging fixture uses. No test touches the real scripts.
    /// Returns the copied scripts directory, the root `runScript` wants.
    private func makeScriptRepo() throws -> URL {
        let repo = scratch.appendingPathComponent("repo", isDirectory: true)
        let scripts = repo.appendingPathComponent("scripts", isDirectory: true)
        try fileManager.createDirectory(at: scripts, withIntermediateDirectories: true)
        try fileManager.copyItem(
            at: scriptsDirectory.appendingPathComponent("make-dmg.sh"), to: scripts.appendingPathComponent("make-dmg.sh"))
        // engine_sign.py loads its sibling helpers by path, transitively; copy
        // every one of them, exactly like the packaging fixture does.
        let helpers = try fileManager.contentsOfDirectory(atPath: scriptsDirectory.path)
            .filter { $0 == "dmg_layout.py" || ($0.hasPrefix("engine_") && $0.hasSuffix(".py")) }
        for name in helpers {
            try fileManager.copyItem(
                at: scriptsDirectory.appendingPathComponent(name), to: scripts.appendingPathComponent(name))
        }
        try fileManager.setAttributes(
            [.posixPermissions: 0o755], ofItemAtPath: scripts.appendingPathComponent("make-dmg.sh").path)
        try fileManager.createDirectory(
            at: repo.appendingPathComponent("release", isDirectory: true), withIntermediateDirectories: true)
        try fileManager.copyItem(
            at: packageRoot.appendingPathComponent("release/dmg"),
            to: repo.appendingPathComponent("release/dmg", isDirectory: true))
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
        environment: [String: String] = [:],
        scriptsRoot: URL? = nil
    ) throws -> (status: Int32, output: String) {
        var childEnvironment = ProcessInfo.processInfo.environment
        // The ambient PATH follows the fakes so python3 (3.12 or newer, for the engine check) resolves as it
        // does for the operator; the fakes still shadow codesign, hdiutil and xcrun.
        var path = tools.path + ":" + (childEnvironment["PATH"] ?? "/usr/bin:/bin") + ":/usr/bin:/bin:/usr/sbin:/sbin"
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
            [(scriptsRoot ?? scriptsDirectory).appendingPathComponent("make-dmg.sh").path] + arguments,
            environment: childEnvironment
        )
    }

    /// The `.DS_Store` bytes the generator writes for the shipped volume.
    private func layoutBytes() throws -> Data {
        try runLayout(["dsstore", "--volume-name", "Ciaobot", "--app-name", "Ciaobot.app"])
    }

    private func layoutGeometry() throws -> [String: Any] {
        let data = try runLayout(["geometry", "--app-name", "Ciaobot.app"])
        return try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func runLayout(_ arguments: [String]) throws -> Data {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-I", "scripts/dmg_layout.py"] + arguments
        process.currentDirectoryURL = packageRoot
        let pipe = Pipe()
        process.standardOutput = pipe
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        XCTAssertEqual(process.terminationStatus, 0, "dmg_layout.py \(arguments) failed")
        return data
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
