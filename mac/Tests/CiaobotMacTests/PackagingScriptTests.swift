import XCTest

/// Exercises the real packaging script against throwaway app bundles inside a
/// unique scratch directory. It never touches the operator's `.build` output,
/// an installed app, an existing artifact, or the network, and it only removes
/// the scratch directory it created.
final class PackagingScriptTests: XCTestCase {
    private var scratch: URL!
    private let fileManager = FileManager.default

    override func setUpWithError() throws {
        scratch = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("ciaobot-packaging-\(UUID().uuidString)", isDirectory: true)
        try fileManager.createDirectory(at: scratch, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        if let scratch { try? fileManager.removeItem(at: scratch) }
    }

    /// The source bundle and an output directory that contains it are an
    /// overlap: copying to `DIR/Ciaobot.app` or cleaning up afterwards could
    /// destroy the build. The script must refuse before it writes anything.
    func testRefusesAnOutputDirectoryThatContainsTheSourceApp() throws {
        let build = scratch.appendingPathComponent("Build", isDirectory: true)
        let app = build.appendingPathComponent("Ciaobot.app", isDirectory: true)
        try makeApp(at: app)
        let plist = app.appendingPathComponent("Contents/Info.plist")
        let plistBefore = try Data(contentsOf: plist)

        let result = try runPackaging(["--no-build", "--app", app.path, "--output", build.path])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("contains the source app"), result.output)
        XCTAssertTrue(fileManager.fileExists(atPath: app.path), "packaging must not delete the source app")
        XCTAssertEqual(try Data(contentsOf: plist), plistBefore)
        XCTAssertFalse(fileManager.fileExists(atPath: build.appendingPathComponent("MANIFEST.txt").path))
    }

    /// An output directory inside the source bundle is refused too, and the
    /// refusal happens before the script creates anything there.
    func testRefusesAnOutputDirectoryInsideTheSourceApp() throws {
        let app = scratch.appendingPathComponent("Build/Ciaobot.app", isDirectory: true)
        try makeApp(at: app)
        let output = app.appendingPathComponent("Contents/Artifacts", isDirectory: true)

        let result = try runPackaging(["--no-build", "--app", app.path, "--output", output.path])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertTrue(result.output.contains("inside the source app"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output.path), "a refused output must not be created")
        XCTAssertTrue(
            fileManager.fileExists(atPath: app.appendingPathComponent("Contents/MacOS/Scratch").path),
            "packaging must leave the source executable intact"
        )
    }

    /// The script has no force option and never replaces an existing archive,
    /// manifest or checksum file. A second run into the same directory fails
    /// and leaves every artifact byte-identical.
    func testRefusesANonEmptyOutputDirectoryWithoutOverwritingArtifacts() throws {
        let app = scratch.appendingPathComponent("Build/Ciaobot.app", isDirectory: true)
        try makeApp(at: app)
        let output = scratch.appendingPathComponent("out", isDirectory: true)

        let first = try runPackaging(["--no-build", "--app", app.path, "--output", output.path])
        XCTAssertEqual(first.status, 0, first.output)

        let names = ["MANIFEST.txt", "SHA256SUMS", "Ciaobot-1.2.3-adhoc.zip"]
        var before: [String: Data] = [:]
        for name in names {
            before[name] = try Data(contentsOf: output.appendingPathComponent(name))
        }

        let second = try runPackaging(["--no-build", "--app", app.path, "--output", output.path])
        XCTAssertNotEqual(second.status, 0, second.output)
        XCTAssertTrue(second.output.contains("non-empty"), second.output)
        for name in names {
            XCTAssertEqual(
                try Data(contentsOf: output.appendingPathComponent(name)),
                before[name],
                "\(name) must not be overwritten"
            )
        }
        XCTAssertTrue(fileManager.fileExists(atPath: app.path))
    }

    func testRefusesAnExistingManifestWithoutOverwritingIt() throws {
        let app = scratch.appendingPathComponent("Build/Ciaobot.app", isDirectory: true)
        try makeApp(at: app)
        let output = scratch.appendingPathComponent("out", isDirectory: true)
        try fileManager.createDirectory(at: output, withIntermediateDirectories: true)
        let manifest = output.appendingPathComponent("MANIFEST.txt")
        try Data("keep this\n".utf8).write(to: manifest)

        let result = try runPackaging(["--no-build", "--app", app.path, "--output", output.path])

        XCTAssertNotEqual(result.status, 0, result.output)
        XCTAssertEqual(try String(contentsOf: manifest, encoding: .utf8), "keep this\n")
        XCTAssertFalse(fileManager.fileExists(atPath: output.appendingPathComponent("Ciaobot.app").path))
    }

    func testRemovedForceOptionIsRejected() throws {
        let app = scratch.appendingPathComponent("Build/Ciaobot.app", isDirectory: true)
        try makeApp(at: app)
        let output = scratch.appendingPathComponent("out", isDirectory: true)

        let result = try runPackaging(["--no-build", "--app", app.path, "--output", output.path, "--force"])

        XCTAssertEqual(result.status, 2, result.output)
        XCTAssertTrue(result.output.contains("unknown argument: --force"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output.path))
    }

    /// The public manifest describes the artifact, not this Mac: it records the
    /// executable's real architectures and contains no filesystem path.
    func testManifestRecordsArchitecturesAndNoPrivatePaths() throws {
        let app = scratch.appendingPathComponent("Build/Ciaobot.app", isDirectory: true)
        try makeApp(at: app, executable: URL(fileURLWithPath: "/bin/echo"))
        let output = scratch.appendingPathComponent("out", isDirectory: true)

        let result = try runPackaging(["--no-build", "--app", app.path, "--output", output.path])
        XCTAssertEqual(result.status, 0, result.output)

        let manifest = try String(contentsOf: output.appendingPathComponent("MANIFEST.txt"), encoding: .utf8)
        let architectures = try run(
            "/usr/bin/lipo",
            ["-archs", app.appendingPathComponent("Contents/MacOS/Scratch").path]
        )
        XCTAssertEqual(architectures.status, 0, architectures.output)
        let expected = architectures.output
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: " ", with: ",")
        XCTAssertTrue(manifest.contains("architectures=\(expected)"), manifest)
        XCTAssertFalse(manifest.lowercased().contains("universal"), manifest)
        for privatePath in [scratch.path, NSHomeDirectory(), "/Users/", "/private/", "/var/folders/"] {
            XCTAssertFalse(manifest.contains(privatePath), "manifest leaks \(privatePath):\n\(manifest)")
        }

        XCTAssertTrue(fileManager.fileExists(atPath: app.path), "packaging must not delete the source app")
        let check = try run("/usr/bin/shasum", ["-a", "256", "-c", "SHA256SUMS"], directory: output)
        XCTAssertEqual(check.status, 0, check.output)
    }

    func testPackagingSignsTheEngineAndRecordsIt() throws {
        let app = scratch.appendingPathComponent("Build/Ciaobot.app", isDirectory: true)
        try makeApp(at: app)
        let output = scratch.appendingPathComponent("out", isDirectory: true)

        let result = try runPackaging(["--no-build", "--app", app.path, "--output", output.path])

        XCTAssertEqual(result.status, 0, result.output)
        let manifest = try String(contentsOf: output.appendingPathComponent("MANIFEST.txt"), encoding: .utf8)
        XCTAssertTrue(manifest.contains("engine_version=1.2.3\n"), manifest)
        let line = try XCTUnwrap(manifest.split(separator: "\n").first { $0.hasPrefix("engine_provenance_sha256=") })
        XCTAssertEqual(line.count, "engine_provenance_sha256=".count + 64, String(line))
        XCTAssertFalse(manifest.contains("/Contents/Resources/engine"), "the manifest must not record a path")
        let extracted = scratch.appendingPathComponent("extracted", isDirectory: true)
        try fileManager.createDirectory(at: extracted, withIntermediateDirectories: true)
        let unzip = try run("/usr/bin/ditto", ["-x", "-k",
                                               output.appendingPathComponent("Ciaobot-1.2.3-adhoc.zip").path, extracted.path])
        XCTAssertEqual(unzip.status, 0, unzip.output)
        let python = extracted.appendingPathComponent("Ciaobot.app/Contents/Resources/engine/bin/python3.12")
        let display = try run("/usr/bin/codesign", ["-d", "--verbose=4", python.path])
        XCTAssertTrue(display.output.contains("Signature=adhoc"), display.output)
    }

    func testPackagingRefusesAnAppWithoutEngine() throws {
        let app = scratch.appendingPathComponent("Build/Ciaobot.app", isDirectory: true)
        try makeApp(at: app, withEngine: false)
        let output = scratch.appendingPathComponent("out", isDirectory: true)

        let result = try runPackaging(["--no-build", "--app", app.path, "--output", output.path])

        XCTAssertEqual(result.status, 1, result.output)
        XCTAssertTrue(result.output.contains("no bundled engine"), result.output)
        XCTAssertFalse(fileManager.fileExists(atPath: output.appendingPathComponent("MANIFEST.txt").path))
        let leftovers = (try? fileManager.contentsOfDirectory(atPath: output.path)) ?? []
        XCTAssertFalse(leftovers.contains { $0.hasSuffix(".zip") || $0 == "SHA256SUMS" }, "\(leftovers)")
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

    private func makeApp(at url: URL, executable: URL? = nil, withEngine: Bool = true) throws {
        let contents = url.appendingPathComponent("Contents", isDirectory: true)
        let macos = contents.appendingPathComponent("MacOS", isDirectory: true)
        try fileManager.createDirectory(at: macos, withIntermediateDirectories: true)
        let plist = """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0"><dict>
        <key>CFBundleExecutable</key><string>Scratch</string>
        <key>CFBundleIdentifier</key><string>test.ciaobot.packaging</string>
        <key>CFBundlePackageType</key><string>APPL</string>
        <key>CFBundleShortVersionString</key><string>1.2.3</string>
        <key>CFBundleVersion</key><string>7</string>
        </dict></plist>
        """
        try Data(plist.utf8).write(to: contents.appendingPathComponent("Info.plist"))
        let executableURL = macos.appendingPathComponent("Scratch")
        if let executable {
            try fileManager.copyItem(at: executable, to: executableURL)
            // A clone of a platform binary keeps Apple's platform signature, and macOS then hangs any exec inside the
            // bundle. Dropping the signature makes it an ordinary unsigned Mach-O.
            let unsigned = try run("/usr/bin/codesign", ["--remove-signature", executableURL.path])
            XCTAssertEqual(unsigned.status, 0, unsigned.output)
        } else {
            try Data("#!/bin/sh\nexit 0\n".utf8).write(to: executableURL)
        }
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executableURL.path)
        if withEngine { try makeFixtureEngine(in: url) }
    }

    /// A thin arm64 Mach-O stand-in: `/bin/echo` is fat and may carry several arm64 slices, which the engine audit refuses.
    private func thinArm64Binary(at destination: URL) throws {
        let result = try run("/usr/bin/lipo", ["/bin/echo", "-thin", "arm64e", "-output", destination.path])
        XCTAssertEqual(result.status, 0, result.output)
        // A clone of a platform binary keeps Apple's platform signature, and macOS then hangs any exec inside the
        // bundle. Dropping the signature makes it an ordinary unsigned Mach-O.
        let unsigned = try run("/usr/bin/codesign", ["--remove-signature", destination.path])
        XCTAssertEqual(unsigned.status, 0, unsigned.output)
    }

    /// A fixture engine: a launcher script, a Mach-O stand-in for the interpreter and the provenance record.
    private func makeFixtureEngine(in app: URL) throws {
        let engine = app.appendingPathComponent("Contents/Resources/engine", isDirectory: true)
        let bin = engine.appendingPathComponent("bin", isDirectory: true)
        try fileManager.createDirectory(at: bin, withIntermediateDirectories: true)
        try Data("#!/bin/sh\necho \"ciao 1.2.3\"\n".utf8).write(to: bin.appendingPathComponent("ciao"))
        try thinArm64Binary(at: bin.appendingPathComponent("python3.12"))
        try Data(#"{"kind":"ciaobot-engine-provenance","engine":{"version":"1.2.3"}}"#.utf8)
            .write(to: engine.appendingPathComponent("provenance.json"))
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: bin.appendingPathComponent("ciao").path)
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: bin.appendingPathComponent("python3.12").path)
    }

    @discardableResult
    private func runPackaging(_ arguments: [String]) throws -> (status: Int32, output: String) {
        try run("/bin/sh", [scriptsDirectory.appendingPathComponent("package-app.sh").path] + arguments)
    }

    @discardableResult
    private func run(
        _ executable: String,
        _ arguments: [String],
        directory: URL? = nil
    ) throws -> (status: Int32, output: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.currentDirectoryURL = directory ?? packageRoot
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus, String(decoding: data, as: UTF8.self))
    }
}
