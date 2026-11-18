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
        XCTAssertTrue(fileManager.fileExists(atPath: staging.path),
                      "the staging copy stays so the maintainer's script can swap it")

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
