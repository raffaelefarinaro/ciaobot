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

    private func makeApp(at url: URL, executable: URL? = nil) throws {
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
        } else {
            try Data("#!/bin/sh\nexit 0\n".utf8).write(to: executableURL)
        }
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executableURL.path)
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
