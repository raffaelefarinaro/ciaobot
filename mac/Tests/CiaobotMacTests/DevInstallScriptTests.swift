import XCTest

/// Exercises the real dev-install script against throwaway app bundles inside
/// a unique scratch directory. It never touches the operator's `.build`
/// output, an installed app, `~/Applications`, or the network, and it only
/// removes the scratch directory it created. Everything a real install would
/// do runs inside the scratch tree: a swap test replaces a scratch
/// `Ciaobot.app`, never a real one.
final class DevInstallScriptTests: XCTestCase {
    private var scratch: URL!
    private let fileManager = FileManager.default

    override func setUpWithError() throws {
        scratch = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("ciaobot-devinstall-\(UUID().uuidString)", isDirectory: true)
        try fileManager.createDirectory(at: scratch, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        if let scratch { try? fileManager.removeItem(at: scratch) }
    }

    /// The dry run must verify the staged bundle and print the plan without
    /// changing anything: no staging copy, no backup, an untouched install.
    func testDryRunVerifiesAndPlansWithoutChangingAnything() throws {
        let staged = try makeStagedApp()
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path, "--dry-run",
             "--expect-engine-version", "1.2.3"]
        )

        XCTAssertEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("Dry run"), result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains(".Ciaobot.app.staging"), result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains(".Ciaobot.app.prev-"), result.joinedOutput)
        XCTAssertEqual(try installedMarker(installed), "installed-old")
        XCTAssertFalse(fileManager.fileExists(atPath: installDirectory.appendingPathComponent(".Ciaobot.app.staging").path),
                       "a dry run must not stage anything")
        XCTAssertFalse(try backupExists(in: installDirectory), "a dry run must not create a backup")
    }

    /// The self-check refuses a bundle whose code signature does not verify.
    /// `codesign --verify --deep --strict` alone does not prove the engine is
    /// signed, but it proves nothing was altered after package-app.sh signed it.
    func testDryRunRefusesAnUnsignedStagedBundle() throws {
        let staged = try makeStagedApp(signed: false)
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path, "--dry-run",
             "--expect-engine-version", "1.2.3"]
        )

        XCTAssertNotEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("could not verify the code signature"), result.joinedOutput)
        XCTAssertEqual(try installedMarker(installed), "installed-old")
        XCTAssertFalse(fileManager.fileExists(atPath: installDirectory.appendingPathComponent(".Ciaobot.app.staging").path))
        XCTAssertFalse(try backupExists(in: installDirectory))
    }

    /// The engine inside the staged bundle must report the expected version:
    /// for a dev install of this checkout, the pyproject.toml version the
    /// bundle's engine was built from.
    func testDryRunRefusesAnEngineThatDoesNotReportTheExpectedVersion() throws {
        let staged = try makeStagedApp(engineVersion: "9.9.9")
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path, "--dry-run",
             "--expect-engine-version", "1.2.3"]
        )

        XCTAssertNotEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(
            result.joinedOutput.contains("the engine reports 'ciao 9.9.9', expected 'ciao 1.2.3'"),
            result.joinedOutput
        )
        XCTAssertEqual(try installedMarker(installed), "installed-old")
        XCTAssertFalse(fileManager.fileExists(atPath: installDirectory.appendingPathComponent(".Ciaobot.app.staging").path))
    }

    /// Without --expect-engine-version the expected version is the checkout's
    /// own pyproject.toml version, so the self-check is "engine == the version
    /// of the repo this bundle claims to carry".
    func testDefaultEngineExpectationComesFromTheCheckoutPyproject() throws {
        let repoVersion = try checkoutProjectVersion()
        let staged = try makeStagedApp(engineVersion: repoVersion)
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path, "--dry-run"]
        )

        XCTAssertEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("ciao \(repoVersion)"), result.joinedOutput)
    }

    /// A real swap (a scratch destination, never a real Applications folder)
    /// replaces the installed name with the staged copy and keeps the previous
    /// bundle as a `.prev-` backup: nothing is ever deleted.
    func testSwapReplacesTheInstalledAppAndKeepsABackup() throws {
        let staged = try makeStagedApp()
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path,
             "--expect-engine-version", "1.2.3"]
        )

        XCTAssertEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("swapped into place"), result.joinedOutput)
        XCTAssertEqual(try installedMarker(installed), "from-staged")
        XCTAssertFalse(fileManager.fileExists(atPath: installDirectory.appendingPathComponent(".Ciaobot.app.staging").path),
                       "the staging copy must have been moved into place, not copied")
        XCTAssertTrue(try backupExists(in: installDirectory), "the previous bundle must be kept as a backup")
        let backupURL = try XCTUnwrap(backupDirectory(in: installDirectory))
        XCTAssertEqual(try installedMarker(backupURL), "installed-old", "the backup holds the replaced bundle")
    }

    /// A leftover staging directory means an earlier run did not finish, or the
    /// maintainer keeps something there under that name: refuse instead of
    /// overwriting.
    func testRefusesAnExistingStagingDirectoryWithoutOverwritingIt() throws {
        let staged = try makeStagedApp()
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)
        let staging = installDirectory.appendingPathComponent(".Ciaobot.app.staging", isDirectory: true)
        try fileManager.createDirectory(at: staging, withIntermediateDirectories: true)
        try Data("keep this\n".utf8).write(to: staging.appendingPathComponent("sentinel"))

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path,
             "--expect-engine-version", "1.2.3"]
        )

        XCTAssertNotEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("staging bundle is already in the way"), result.joinedOutput)
        XCTAssertEqual(try String(contentsOf: staging.appendingPathComponent("sentinel"), encoding: .utf8),
                       "keep this\n", "the staging directory must not be overwritten")
        XCTAssertEqual(try installedMarker(installed), "installed-old")
        XCTAssertFalse(try backupExists(in: installDirectory))
    }

    /// The live, registered Ciaobot.app name must never be written into, and
    /// never swapped with itself.
    func testRefusesToSwapTheInstalledAppWithItself() throws {
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)
        let signedInstalled = try signAppBundle(installed)

        let result = try runDevInstall(
            ["--staged", signedInstalled.path, "--installed", installed.path, "--dry-run",
             "--expect-engine-version", "0.5.0"]
        )

        XCTAssertNotEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("with itself"), result.joinedOutput)
        XCTAssertEqual(try installedMarker(installed), "installed-old")
        XCTAssertFalse(fileManager.fileExists(atPath: installDirectory.appendingPathComponent(".Ciaobot.app.staging").path))
    }

    /// The default and only accepted installed name is Ciaobot.app: a mistyped
    /// --installed must not rename some other bundle.
    func testRefusesAnInstalledAppWithAnotherName() throws {
        let staged = try makeStagedApp()
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let other = installDirectory.appendingPathComponent("Odd.app", isDirectory: true)
        try fileManager.createDirectory(at: other.appendingPathComponent("Contents"), withIntermediateDirectories: true)

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", other.path, "--dry-run",
             "--expect-engine-version", "1.2.3"]
        )

        XCTAssertNotEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("must be named Ciaobot.app"), result.joinedOutput)
        XCTAssertFalse(fileManager.fileExists(atPath: installDirectory.appendingPathComponent(".Ciaobot.app.staging").path))
    }

    /// When the environment denies the swap (an agent sandbox, or macOS
    /// refusing the rename of a running, registered app), the run must not
    /// fail: it restores the previous state, exits 0, and prints a ready-to-run
    /// swap script on stdout. Simulate the denial with an `mv` stub on PATH,
    /// the way the other script tests fake their tools, and prove the emitted
    /// script completes the swap when the maintainer runs it.
    func testBlockedSwapPrintsAReadyToRunScriptThatCompletesTheSwap() throws {
        let staged = try makeStagedApp()
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)
        let stubPath = try makeFailingMvStub()

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path,
             "--expect-engine-version", "1.2.3"],
            stubPath: stubPath.path
        )

        XCTAssertEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("SWAP BLOCKED"), result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("ready-to-run swap script"), result.joinedOutput)
        XCTAssertEqual(try installedMarker(installed), "installed-old",
                       "the denied swap must leave the previous app in place")
        let staging = installDirectory.appendingPathComponent(".Ciaobot.app.staging", isDirectory: true)
        XCTAssertFalse(fileManager.fileExists(atPath: staging.path),
                       "the staging copy this run created must be removed; the handoff script re-stages from --staged")
        XCTAssertTrue(result.joinedOutput.contains("removed the staging copy this run created"), result.joinedOutput)

        // Hand the emitted script to the maintainer: extract it from stdout
        // and run it, and the swap must complete.
        let scriptText = try XCTUnwrap(
            result.output.range(of: "#!/bin/sh", options: .backwards).map { String(result.output[$0.lowerBound...]) },
            result.output
        )
        let swapScript = scratch.appendingPathComponent("swap-ciaobot.sh")
        try Data(scriptText.utf8).write(to: swapScript)
        let handed = try run("/bin/sh", [swapScript.path])
        XCTAssertEqual(handed.status, 0, handed.output)
        XCTAssertTrue(handed.output.contains("swapped into place"), handed.output)
        XCTAssertEqual(try installedMarker(installed), "from-staged")
        XCTAssertFalse(fileManager.fileExists(atPath: staging.path))
        XCTAssertTrue(try backupExists(in: installDirectory))
    }

    /// A failed swap must restore the previous app from the exact backup path
    /// recorded this run — never from a `.prev-*` glob, which can pick up an
    /// older backup from a previous run instead of the bundle just replaced.
    func testBlockedSwapRestoresTheRecordedBackupNotAnOlderPrevCopy() throws {
        let staged = try makeStagedApp()
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)
        let stale = installDirectory.appendingPathComponent(".Ciaobot.app.prev-20200101000000", isDirectory: true)
        try makeMarkerBundle(at: stale, marker: "stale-backup")
        // Fail every mv whose source is the staging copy: the installed app
        // moves aside successfully, the staged copy cannot be moved in.
        // Allowed moves must really move, so the on-disk state matches the
        // exit status.
        let stub = try makeStub(
            "mv",
            "case $1 in *.Ciaobot.app.staging*) echo 'Operation not permitted' >&2; exit 1 ;; esac\nexec /bin/mv \"$@\""
        )

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path,
             "--expect-engine-version", "1.2.3"],
            stubPath: stub.path
        )

        XCTAssertEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("SWAP BLOCKED"), result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("put back in place"), result.joinedOutput)
        XCTAssertEqual(try installedMarker(installed), "installed-old",
                       "the recorded backup, not the older .prev-* copy, must be restored")
        XCTAssertEqual(try installedMarker(stale), "stale-backup",
                       "the older backup must stay untouched")
        let backups = try allBackups(in: installDirectory).map(\.lastPathComponent)
        XCTAssertEqual(backups, [".Ciaobot.app.prev-20200101000000"],
                       "the fresh backup must have been moved back exactly")
        XCTAssertFalse(fileManager.fileExists(
            atPath: installDirectory.appendingPathComponent(".Ciaobot.app.staging").path))
    }

    /// When the restore itself is denied, the run must fail loudly and name
    /// the exact backup path, instead of printing a swap script that would
    /// start from a half-moved state.
    func testFailingRestoreExitsNonZeroAndPrintsTheExactBackupPath() throws {
        let staged = try makeStagedApp()
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)
        // Fail every mv whose target is the installed app: the move aside
        // succeeds, the staged copy cannot be moved in, and neither can the
        // backup be moved back. Allowed moves must really move.
        let stub = try makeStub(
            "mv",
            "case $2 in */Ciaobot.app) echo 'Operation not permitted' >&2; exit 1 ;; esac\nexec /bin/mv \"$@\""
        )

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path,
             "--expect-engine-version", "1.2.3"],
            stubPath: stub.path
        )

        XCTAssertNotEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("could NOT be moved back"), result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains(".Ciaobot.app.prev-"), result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("by hand before swapping again"), result.joinedOutput)
        XCTAssertFalse(result.joinedOutput.contains("ready-to-run swap script"),
                       "a failed restore must not hand over a swap script")
        XCTAssertFalse(fileManager.fileExists(atPath: installed.path),
                       "the previous app is in the backup, not in place")
        // The printed backup path must be exact: it exists and holds the
        // replaced bundle.
        let printed = try XCTUnwrap(
            result.joinedOutput
                .components(separatedBy: "\n")
                .first { $0.contains("its backup sits exactly at: ") }?
                .trimmingCharacters(in: .whitespaces)
                .dropFirst("dev-install: its backup sits exactly at: ".count),
            result.joinedOutput
        )
        let backupURL = URL(fileURLWithPath: String(printed))
        XCTAssertTrue(fileManager.fileExists(atPath: backupURL.path),
                      "the printed backup path must exist: \(printed)")
        XCTAssertEqual(try installedMarker(backupURL), "installed-old")
        XCTAssertFalse(fileManager.fileExists(
            atPath: installDirectory.appendingPathComponent(".Ciaobot.app.staging").path),
                       "the staging copy this run created must be removed")
        let backups = try allBackups(in: installDirectory)
        XCTAssertEqual(backups.count, 1, "the previous app sits in exactly one backup")
        XCTAssertEqual(try installedMarker(try XCTUnwrap(backups.first)), "installed-old")
    }

    /// Two swaps in the same second must not nest one backup inside another:
    /// the backup name is collision-corrected with a counter instead of
    /// overwriting or shadowing an existing `.prev-*` copy.
    func testSwapPicksACollisionFreeBackupNameWhenTheFirstIsTaken() throws {
        let staged = try makeStagedApp()
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)
        let taken = installDirectory.appendingPathComponent(".Ciaobot.app.prev-20260101000000", isDirectory: true)
        try makeMarkerBundle(at: taken, marker: "stale-backup")
        let stub = try makeStub("date", "printf '%s\\n' '20260101000000'")

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path,
             "--expect-engine-version", "1.2.3"],
            stubPath: stub.path
        )

        XCTAssertEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("swapped into place"), result.joinedOutput)
        let backups = try allBackups(in: installDirectory).map(\.lastPathComponent)
        XCTAssertTrue(backups.contains(".Ciaobot.app.prev-20260101000000"),
                      "the pre-existing backup must keep its name")
        XCTAssertTrue(backups.contains(".Ciaobot.app.prev-20260101000000-2"),
                      "the new backup must take a disambiguated name, not overwrite the old one")
        XCTAssertEqual(backups.count, 2, result.joinedOutput)
        XCTAssertEqual(try installedMarker(
            try XCTUnwrap(allBackups(in: installDirectory).first { $0.lastPathComponent == ".Ciaobot.app.prev-20260101000000-2" }
            )), "installed-old")
        XCTAssertEqual(try installedMarker(installed), "from-staged")
    }

    /// The handoff script must refuse a staging directory that is already in
    /// the way (like the main script), never `rm -rf` it: whatever the
    /// maintainer keeps at that name survives untouched.
    func testGeneratedScriptRefusesAStagingDirectoryInProgress() throws {
        let staged = try makeStagedApp()
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)
        let failingMv = try makeFailingMvStub()

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path,
             "--expect-engine-version", "1.2.3"],
            stubPath: failingMv.path
        )
        XCTAssertEqual(result.status, 0, result.joinedOutput)
        let swapScript = try extractedSwapScript(result, as: "swap-refuses-staging.sh")

        let staging = installDirectory.appendingPathComponent(".Ciaobot.app.staging", isDirectory: true)
        try fileManager.createDirectory(at: staging, withIntermediateDirectories: true)
        try Data("keep this\n".utf8).write(to: staging.appendingPathComponent("sentinel"))

        let handed = try runShell(swapScript)
        XCTAssertEqual(handed.status, 1, handed.output)
        XCTAssertTrue(handed.output.contains("staging directory is in the way"), handed.output)
        XCTAssertEqual(try String(contentsOf: staging.appendingPathComponent("sentinel"), encoding: .utf8),
                       "keep this\n", "the script must not remove an existing staging directory")
        XCTAssertEqual(try installedMarker(installed), "installed-old")
        XCTAssertFalse(try backupExists(in: installDirectory))
    }

    /// If the handoff script's own move into place fails, it must roll the
    /// previous app back from the backup it just made and say so.
    func testGeneratedScriptRollsBackWhenItsSwapMoveFails() throws {
        let staged = try makeStagedApp()
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)
        let failingMv = try makeFailingMvStub()

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path,
             "--expect-engine-version", "1.2.3"],
            stubPath: failingMv.path
        )
        XCTAssertEqual(result.status, 0, result.joinedOutput)
        let swapScript = try extractedSwapScript(result, as: "swap-rollback.sh")

        // Fail only the mv whose source is the staging copy, so the backup
        // move inside the handoff script succeeds and the swap move fails.
        // Allowed moves must really move, so the rollback has a backup to
        // restore.
        let stub = try makeStub(
            "mv",
            "case $1 in *.Ciaobot.app.staging*) echo 'Operation not permitted' >&2; exit 1 ;; esac\nexec /bin/mv \"$@\""
        )
        let handed = try runShell(swapScript, stubPath: stub.path)
        XCTAssertEqual(handed.status, 1, handed.output)
        XCTAssertTrue(handed.output.contains("the swap failed"), handed.output)
        XCTAssertTrue(handed.output.contains("put back in place from"), handed.output)
        XCTAssertEqual(try installedMarker(installed), "installed-old",
                       "the previous app must be rolled back from the backup")
        XCTAssertFalse(try backupExists(in: installDirectory),
                       "the rolled back backup no longer sits in .prev-*")
        XCTAssertTrue(fileManager.fileExists(
            atPath: installDirectory.appendingPathComponent(".Ciaobot.app.staging").path),
                       "the failed move leaves the staged copy for inspection")
    }

    /// The handoff script must take a collision-safe backup name (date and
    /// its own pid) and refuse to overwrite an existing backup.
    func testGeneratedScriptUsesACollisionSafeBackupName() throws {
        let staged = try makeStagedApp()
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)
        let failingMv = try makeFailingMvStub()

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path,
             "--expect-engine-version", "1.2.3"],
            stubPath: failingMv.path
        )
        XCTAssertEqual(result.status, 0, result.joinedOutput)
        let swapScript = try extractedSwapScript(result, as: "swap-collision-name.sh")
        let text = try String(contentsOf: swapScript, encoding: .utf8)
        XCTAssertTrue(text.contains(".Ciaobot.app.prev-$(date +%Y%m%d%H%M%S)-$$"),
                      "the backup name must carry the pid so two runs cannot collide:\n\(text)")
        XCTAssertTrue(text.contains("a backup already sits at the computed backup name"),
                      "the script must refuse an existing backup instead of overwriting:\n\(text)")
    }

    func testTheScriptKeepsItsExecutableBit() {
        let url = scriptsDirectory.appendingPathComponent("dev-install-app.sh")
        XCTAssertTrue(
            fileManager.isExecutableFile(atPath: url.path),
            "scripts/dev-install-app.sh must keep its executable bit"
        )
    }

    func testRejectsUnknownArgumentsAndAMissingStagedBundle() throws {
        let staged = try makeStagedApp()

        let unknown = try runDevInstall(["--staged", staged.path, "--bogus"])
        XCTAssertEqual(unknown.status, 2, unknown.joinedOutput)
        XCTAssertTrue(unknown.joinedOutput.contains("unknown argument: --bogus"), unknown.joinedOutput)

        let missing = try runDevInstall(["--dry-run"])
        XCTAssertEqual(missing.status, 2, missing.joinedOutput)
    }

    /// Paths with quoting, expansion or control characters are refused with
    /// exit 2 before anything is resolved: they cannot be embedded honestly
    /// in the generated swap script and they make an mv that swaps a live app
    /// unreadable.
    func testRejectsShellUnsafePathsForStagedAndInstalled() throws {
        let staged = try makeStagedApp()
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)
        let rejected = ["'", "\"", "`", "$", "\\", "\n"]

        for character in rejected {
            let badStaged = staged.path + "!" + character
            let stagedRun = try runDevInstall(["--staged", badStaged, "--installed", installed.path])
            XCTAssertEqual(stagedRun.status, 2, stagedRun.joinedOutput)
            XCTAssertTrue(stagedRun.joinedOutput.contains("are not supported"), stagedRun.joinedOutput)

            let badInstalled = installed.path + "!" + character
            let installedRun = try runDevInstall(["--staged", staged.path, "--installed", badInstalled])
            XCTAssertEqual(installedRun.status, 2, installedRun.joinedOutput)
            XCTAssertTrue(installedRun.joinedOutput.contains("are not supported"), installedRun.joinedOutput)
        }

        // Nothing was touched by any of those runs.
        XCTAssertFalse(fileManager.fileExists(
            atPath: installDirectory.appendingPathComponent(".Ciaobot.app.staging").path))
        XCTAssertFalse(try backupExists(in: installDirectory))
        XCTAssertEqual(try installedMarker(installed), "installed-old")
    }

    /// The quoting helper must let legal but awkward paths (spaces) through
    /// the whole flow, including the generated swap script.
    func testQuotedPathsSurviveTheGeneratedSwapScript() throws {
        let stagedDir = scratch.appendingPathComponent("My Staged Output", isDirectory: true)
        let staged = try makeStagedApp(at: stagedDir.appendingPathComponent("Ciaobot.app", isDirectory: true))
        let installDirectory = scratch.appendingPathComponent("My Installs", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)
        let failingMv = try makeFailingMvStub()

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path,
             "--expect-engine-version", "1.2.3"],
            stubPath: failingMv.path
        )
        XCTAssertEqual(result.status, 0, result.joinedOutput)
        let swapScript = try extractedSwapScript(result, as: "swap-quoted.sh")

        let handed = try runShell(swapScript)
        XCTAssertEqual(handed.status, 0, handed.output)
        XCTAssertTrue(handed.output.contains("swapped into place"), handed.output)
        XCTAssertEqual(try installedMarker(installed), "from-staged")
        XCTAssertTrue(try backupExists(in: installDirectory))
        XCTAssertEqual(try installedMarker(try XCTUnwrap(try backupDirectory(in: installDirectory))),
                       "installed-old")
    }

    /// When ditto fails, the staging copy this run created must not stay
    /// behind blocking the next run.
    func testFailingDittoLeavesNoStagingCopyBehind() throws {
        let staged = try makeStagedApp()
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)
        let stub = try makeStub(
            "ditto",
            "/usr/bin/ditto \"$@\"\necho 'ditto: broken' >&2\nexit 1"
        )

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path,
             "--expect-engine-version", "1.2.3"],
            stubPath: stub.path
        )

        XCTAssertNotEqual(result.status, 0, result.joinedOutput)
        XCTAssertFalse(fileManager.fileExists(
            atPath: installDirectory.appendingPathComponent(".Ciaobot.app.staging").path),
                       "the staging copy this run created must be cleaned up when ditto fails")
        XCTAssertEqual(try installedMarker(installed), "installed-old")
        XCTAssertFalse(try backupExists(in: installDirectory))
    }

    /// When the self-check of the staging copy fails, the staging copy must
    /// not stay behind either.
    func testFailingSelfCheckLeavesNoStagingCopyBehind() throws {
        let staged = try makeStagedApp(signed: false)
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path,
             "--expect-engine-version", "1.2.3"]
        )

        XCTAssertNotEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("could not verify the code signature"), result.joinedOutput)
        XCTAssertFalse(fileManager.fileExists(
            atPath: installDirectory.appendingPathComponent(".Ciaobot.app.staging").path),
                       "the staging copy this run created must be cleaned up when the self-check fails")
        XCTAssertEqual(try installedMarker(installed), "installed-old")
        XCTAssertFalse(try backupExists(in: installDirectory))
    }

    /// The installed app must be a real directory, never a symlink: following
    /// a link back to the bundle that owns it would corrupt the wrong install.
    func testRefusesASymlinkedInstalledApp() throws {
        let staged = try makeStagedApp()
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let real = try makeInstalledApp(in: installDirectory.appendingPathComponent("Real", isDirectory: true))
        let link = installDirectory.appendingPathComponent("Ciaobot.app")
        try fileManager.createSymbolicLink(at: link, withDestinationURL: real)

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installDirectory.appendingPathComponent("Ciaobot.app").path,
             "--dry-run", "--expect-engine-version", "1.2.3"]
        )

        XCTAssertNotEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("installed app is a symlink"), result.joinedOutput)
        XCTAssertFalse(fileManager.fileExists(
            atPath: installDirectory.appendingPathComponent(".Ciaobot.app.staging").path))
    }

    /// A staged bundle nested inside the installed app is refused before
    /// anything is copied.
    func testRefusesAStagedBundleInsideTheInstalledApp() throws {
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)
        let nested = installed.appendingPathComponent("Nested/Ciaobot.app", isDirectory: true)
        try fileManager.createDirectory(at: nested.appendingPathComponent("Contents"), withIntermediateDirectories: true)

        let result = try runDevInstall(
            ["--staged", nested.path, "--installed", installed.path,
             "--dry-run", "--expect-engine-version", "1.2.3"]
        )

        XCTAssertNotEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("inside the installed app"), result.joinedOutput)
        XCTAssertFalse(fileManager.fileExists(
            atPath: installDirectory.appendingPathComponent(".Ciaobot.app.staging").path))
        XCTAssertEqual(try installedMarker(installed), "installed-old")
    }

    /// And the reverse: an installed app living inside the staged bundle is
    /// refused too.
    func testRefusesAnInstalledAppInsideTheStagedBundle() throws {
        let outer = try makeStagedApp(at: scratch.appendingPathComponent("Outer/Ciaobot.app", isDirectory: true))
        let inner = try makeInstalledApp(in: outer.appendingPathComponent("Inner", isDirectory: true))

        let result = try runDevInstall(
            ["--staged", outer.path, "--installed", inner.path,
             "--dry-run", "--expect-engine-version", "1.2.3"]
        )

        XCTAssertNotEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("inside the staged bundle"), result.joinedOutput)
        XCTAssertFalse(fileManager.fileExists(
            atPath: outer.deletingLastPathComponent().appendingPathComponent(".Ciaobot.app.staging").path))
        XCTAssertEqual(try installedMarker(inner), "installed-old")
    }

    /// A staged bundle whose engine is a symlink is refused by the self-check
    /// before anything changes.
    func testRefusesAnEngineThatIsASymlink() throws {
        let staged = try makeStagedApp()
        let installDirectory = scratch.appendingPathComponent("Install", isDirectory: true)
        let installed = try makeInstalledApp(in: installDirectory)
        let engine = staged.appendingPathComponent("Contents/Resources/engine", isDirectory: true)
        try fileManager.createDirectory(
            at: scratch.appendingPathComponent("EngineDir/bin", isDirectory: true),
            withIntermediateDirectories: true
        )
        try fileManager.removeItem(at: engine)
        try fileManager.createSymbolicLink(
            at: engine,
            withDestinationURL: scratch.appendingPathComponent("EngineDir", isDirectory: true)
        )

        let result = try runDevInstall(
            ["--staged", staged.path, "--installed", installed.path,
             "--dry-run", "--expect-engine-version", "1.2.3"]
        )

        XCTAssertNotEqual(result.status, 0, result.joinedOutput)
        XCTAssertTrue(result.joinedOutput.contains("must be a real directory, not a symlink"), result.joinedOutput)
        XCTAssertFalse(fileManager.fileExists(
            atPath: installDirectory.appendingPathComponent(".Ciaobot.app.staging").path))
        XCTAssertEqual(try installedMarker(installed), "installed-old")
    }

    // MARK: - Fixtures

    private var packageRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    private var scriptsDirectory: URL {
        packageRoot.appendingPathComponent("scripts")
    }

    private func makeStagedApp(
        at directory: URL? = nil,
        engineVersion: String = "1.2.3",
        signed: Bool = true
    ) throws -> URL {
        let app = directory ?? scratch.appendingPathComponent("Staged/Ciaobot.app", isDirectory: true)
        let contents = app.appendingPathComponent("Contents", isDirectory: true)
        try fileManager.createDirectory(at: contents.appendingPathComponent("MacOS"), withIntermediateDirectories: true)
        try fileManager.createDirectory(at: contents.appendingPathComponent("Resources/engine/bin"), withIntermediateDirectories: true)
        let plist = """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0"><dict>
        <key>CFBundleExecutable</key><string>Scratch</string>
        <key>CFBundleIdentifier</key><string>test.ciaobot.devinstall</string>
        <key>CFBundlePackageType</key><string>APPL</string>
        <key>CFBundleShortVersionString</key><string>1.2.3</string>
        <key>CFBundleVersion</key><string>7</string>
        </dict></plist>
        """
        try Data(plist.utf8).write(to: contents.appendingPathComponent("Info.plist"))
        let executable = contents.appendingPathComponent("MacOS/Scratch")
        try Data("#!/bin/sh\necho from-staged\n".utf8).write(to: executable)
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path)
        let engineLauncher = contents.appendingPathComponent("Resources/engine/bin/ciao")
        try Data("#!/bin/sh\necho \"ciao \(engineVersion)\"\n".utf8).write(to: engineLauncher)
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: engineLauncher.path)
        if signed { return try signAppBundle(app) }
        return app
    }

    private func makeInstalledApp(in directory: URL) throws -> URL {
        let app = directory.appendingPathComponent("Ciaobot.app", isDirectory: true)
        let contents = app.appendingPathComponent("Contents", isDirectory: true)
        try fileManager.createDirectory(at: contents.appendingPathComponent("MacOS"), withIntermediateDirectories: true)
        let plist = """
        <?xml version="1.0" encoding="UTF-8"?>
        <plist version="1.0"><dict>
        <key>CFBundleExecutable</key><string>Scratch</string>
        <key>CFBundleIdentifier</key><string>test.ciaobot.devinstall</string>
        <key>CFBundleShortVersionString</key><string>0.5.0</string>
        </dict></plist>
        """
        try Data(plist.utf8).write(to: contents.appendingPathComponent("Info.plist"))
        let executable = contents.appendingPathComponent("MacOS/Scratch")
        try Data("#!/bin/sh\necho installed-old\n".utf8).write(to: executable)
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path)
        return app
    }

    /// A minimal bundle that only exists to report a marker through its
    /// Scratch executable, so backups can be told apart by content.
    private func makeMarkerBundle(at app: URL, marker: String) throws {
        let contents = app.appendingPathComponent("Contents", isDirectory: true)
        try fileManager.createDirectory(at: contents.appendingPathComponent("MacOS"), withIntermediateDirectories: true)
        let executable = contents.appendingPathComponent("MacOS/Scratch")
        try Data("#!/bin/sh\necho \(marker)\n".utf8).write(to: executable)
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path)
    }

    /// A single-file tool directory that sits first on PATH for one run.
    private func makeStub(_ name: String, _ body: String) throws -> URL {
        let stub = scratch.appendingPathComponent("stub-\(name)-\(UUID().uuidString)", isDirectory: true)
        try fileManager.createDirectory(at: stub, withIntermediateDirectories: true)
        let tool = stub.appendingPathComponent(name)
        try Data("#!/bin/sh\n\(body)\n".utf8).write(to: tool)
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: tool.path)
        return stub
    }

    private func signAppBundle(_ app: URL) throws -> URL {
        let result = try run("/usr/bin/codesign", ["--force", "--sign", "-", app.path])
        XCTAssertEqual(result.status, 0, result.output)
        return app
    }

    /// A stub that denies every move, standing in for a sandbox that refuses
    /// the rename of the installed app.
    private func makeFailingMvStub() throws -> URL {
        let stub = scratch.appendingPathComponent("stub", isDirectory: true)
        try fileManager.createDirectory(at: stub, withIntermediateDirectories: true)
        let mv = stub.appendingPathComponent("mv")
        try Data("#!/bin/sh\necho 'Operation not permitted' >&2\nexit 1\n".utf8).write(to: mv)
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: mv.path)
        return stub
    }

    private func installedMarker(_ app: URL) throws -> String {
        let text = try String(
            contentsOf: app.appendingPathComponent("Contents/MacOS/Scratch"),
            encoding: .utf8
        )
        return text
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .components(separatedBy: "\n").last?
            .replacingOccurrences(of: "echo ", with: "") ?? ""
    }

    private func backupExists(in directory: URL) throws -> Bool {
        try backupDirectory(in: directory) != nil
    }

    private func backupDirectory(in directory: URL) throws -> URL? {
        try fileManager.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            .first { $0.lastPathComponent.hasPrefix(".Ciaobot.app.prev-") }
    }

    private func allBackups(in directory: URL) throws -> [URL] {
        try fileManager.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            .filter { $0.lastPathComponent.hasPrefix(".Ciaobot.app.prev-") }
    }

    /// The swap script the blocked run printed on stdout, saved for the
    /// maintainer to run.
    private func extractedSwapScript(
        _ result: (status: Int32, output: String, error: String, joinedOutput: String),
        as name: String
    ) throws -> URL {
        let scriptText = try XCTUnwrap(
            result.output.range(of: "#!/bin/sh", options: .backwards).map { String(result.output[$0.lowerBound...]) },
            result.output
        )
        let url = scratch.appendingPathComponent(name)
        try Data(scriptText.utf8).write(to: url)
        return url
    }

    @discardableResult
    private func runShell(_ script: URL, stubPath: String? = nil) throws -> (status: Int32, output: String) {
        var environment = ProcessInfo.processInfo.environment
        if let stubPath {
            environment["PATH"] = "\(stubPath):" + (environment["PATH"] ?? "/usr/bin:/bin")
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = [script.path]
        process.currentDirectoryURL = packageRoot
        process.environment = environment
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus, String(decoding: data, as: UTF8.self))
    }

    /// The expected engine version derives from the checkout's pyproject.toml,
    /// exactly as the script reads it, so the test pins the real derivation.
    private func checkoutProjectVersion() throws -> String {
        let pyproject = try String(
            contentsOf: packageRoot.deletingLastPathComponent().appendingPathComponent("pyproject.toml"),
            encoding: .utf8
        )
        var inProject = false
        for line in pyproject.components(separatedBy: "\n") {
            if line == "[project]" {
                inProject = true
                continue
            }
            if inProject && line.hasPrefix("[") { break }
            if inProject, line.hasPrefix("version = ") {
                return line
                    .dropFirst("version = ".count)
                    .trimmingCharacters(in: .whitespaces)
                    .trimmingCharacters(in: CharacterSet(charactersIn: "\""))
            }
        }
        XCTFail("no [project] version found in pyproject.toml")
        return ""
    }

    // MARK: - Running the script

    @discardableResult
    private func runDevInstall(_ arguments: [String], stubPath: String? = nil) throws
        -> (status: Int32, output: String, error: String, joinedOutput: String) {
        var environment = ProcessInfo.processInfo.environment
        if let stubPath {
            environment["PATH"] = "\(stubPath):" + (environment["PATH"] ?? "/usr/bin:/bin")
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = [scriptsDirectory.appendingPathComponent("dev-install-app.sh").path] + arguments
        process.currentDirectoryURL = packageRoot
        process.environment = environment
        let outPipe = Pipe()
        let errPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = errPipe
        try process.run()
        let out = outPipe.fileHandleForReading.readDataToEndOfFile()
        let err = errPipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let output = String(decoding: out, as: UTF8.self)
        let error = String(decoding: err, as: UTF8.self)
        return (process.terminationStatus, output, error, output + "\n" + error)
    }

    @discardableResult
    private func run(
        _ executable: String,
        _ arguments: [String]
    ) throws -> (status: Int32, output: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.currentDirectoryURL = packageRoot
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus, String(decoding: data, as: UTF8.self))
    }
}
