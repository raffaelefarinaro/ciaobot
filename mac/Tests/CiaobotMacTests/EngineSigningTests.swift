import XCTest

/// Drives `scripts/engine_sign.py` and real `/usr/bin/codesign` against fixture engines made of `/bin/echo`
/// copies, in a unique scratch directory. Nothing touches an installed app, the operator's `.build` or the network.
final class EngineSigningTests: XCTestCase {
    private var scratch: URL!
    private let fileManager = FileManager.default

    override func setUpWithError() throws {
        scratch = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("ciaobot-enginesign-\(UUID().uuidString)", isDirectory: true)
        try fileManager.createDirectory(at: scratch, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        if let scratch { try? fileManager.removeItem(at: scratch) }
    }

    /// An outer ad-hoc seal passes `--deep --strict` over unsigned engine files (they are sealed as resources),
    /// so only `engine_sign.py check` proves the engine is signed.
    func testCheckRefusesAnUnsignedEngineMachO() throws {
        let app = scratch.appendingPathComponent("Ciaobot.app", isDirectory: true)
        try makeApp(at: app)
        let engine = app.appendingPathComponent("Contents/Resources/engine", isDirectory: true)
        try makeEngine(at: engine, extraMachO: ["lib/a.so"])
        for rel in ["bin/python3.12", "lib/a.so"] {
            let stripped = try run("/usr/bin/codesign", ["--remove-signature", engine.appendingPathComponent(rel).path])
            XCTAssertEqual(stripped.status, 0, stripped.output)
        }
        let sealed = try run("/usr/bin/codesign", ["--force", "--sign", "-", "--timestamp=none", app.path])
        XCTAssertEqual(sealed.status, 0, sealed.output)

        let outer = try run("/usr/bin/codesign", ["--verify", "--deep", "--strict", app.path])
        XCTAssertEqual(outer.status, 0, "the outer seal passes over unsigned engine files: \(outer.output)")
        let refused = try engineSign(["check", engine.path])
        XCTAssertEqual(refused.status, 1, refused.output)

        let signed = try engineSign(["sign", "--adhoc", engine.path])
        XCTAssertEqual(signed.status, 0, signed.output)
        let accepted = try engineSign(["check", engine.path])
        XCTAssertEqual(accepted.status, 0, accepted.output)
        for rel in ["bin/python3.12", "lib/a.so"] {
            let verified = try run("/usr/bin/codesign", ["--verify", "--strict", engine.appendingPathComponent(rel).path])
            XCTAssertEqual(verified.status, 0, verified.output)
        }
    }

    /// The distribution gate (#3128 G) that `make-dmg.sh` and `make-appcast.sh` rely on: an ad-hoc signed engine
    /// passes the ad-hoc check but not the distribution check, and the usage forms are exact.
    func testDistributionCheckRefusesAnAdHocSignedEngine() throws {
        let app = scratch.appendingPathComponent("Ciaobot.app", isDirectory: true)
        try makeApp(at: app)
        let engine = app.appendingPathComponent("Contents/Resources/engine", isDirectory: true)
        try makeEngine(at: engine, extraMachO: ["lib/a.so"])
        let signed = try engineSign(["sign", "--adhoc", engine.path])
        XCTAssertEqual(signed.status, 0, signed.output)

        let adhoc = try engineSign(["check", engine.path])
        XCTAssertEqual(adhoc.status, 0, adhoc.output)
        let distribution = try engineSign(["check", "--distribution", "AXBFS96Y9F", engine.path])
        XCTAssertEqual(distribution.status, 1, distribution.output)
        XCTAssertTrue(distribution.output.contains("engine sign failed"), distribution.output)
        let usage = try engineSign(["check", "--distribution", engine.path])
        XCTAssertEqual(usage.status, 2, usage.output)
        let badIdentity = try engineSign(["sign", "--distribution", "AXBFS96Y9F", "-", engine.path])
        XCTAssertEqual(badIdentity.status, 1, badIdentity.output)
    }

    func testSigningRefusesAnEngineOutsideAnAppLayout() throws {
        let engine = scratch.appendingPathComponent("engine", isDirectory: true)
        try makeEngine(at: engine, extraMachO: [])

        let result = try engineSign(["sign", "--adhoc", engine.path])

        XCTAssertEqual(result.status, 1, result.output)
        XCTAssertTrue(result.output.contains("engine sign failed"), result.output)
    }

    // MARK: - Helpers

    private var packageRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    }

    private func makeApp(at app: URL) throws {
        let macos = app.appendingPathComponent("Contents/MacOS", isDirectory: true)
        try fileManager.createDirectory(at: macos, withIntermediateDirectories: true)
        let plist = """
        <?xml version="1.0" encoding="UTF-8"?>
        <plist version="1.0"><dict>
        <key>CFBundleExecutable</key><string>Scratch</string>
        <key>CFBundleIdentifier</key><string>test.ciaobot.enginesign</string>
        <key>CFBundlePackageType</key><string>APPL</string>
        </dict></plist>
        """
        try Data(plist.utf8).write(to: app.appendingPathComponent("Contents/Info.plist"))
        try fileManager.copyItem(at: URL(fileURLWithPath: "/bin/echo"), to: macos.appendingPathComponent("Scratch"))
        // A clone of a platform binary keeps Apple's platform signature, and macOS then hangs any exec inside the
        // bundle. Dropping the signature makes it an ordinary unsigned Mach-O.
        let unsigned = try run("/usr/bin/codesign", ["--remove-signature", macos.appendingPathComponent("Scratch").path])
        XCTAssertEqual(unsigned.status, 0, unsigned.output)
    }

    private func makeEngine(at engine: URL, extraMachO: [String]) throws {
        let bin = engine.appendingPathComponent("bin", isDirectory: true)
        try fileManager.createDirectory(at: bin, withIntermediateDirectories: true)
        try Data("#!/bin/sh\necho \"ciao 1.2.3\"\n".utf8).write(to: bin.appendingPathComponent("ciao"))
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: bin.appendingPathComponent("ciao").path)
        try Data(#"{"kind":"ciaobot-engine-provenance","engine":{"version":"1.2.3"}}"#.utf8)
            .write(to: engine.appendingPathComponent("provenance.json"))
        for rel in ["bin/python3.12"] + extraMachO {
            let target = engine.appendingPathComponent(rel)
            try fileManager.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
            let thin = try run("/usr/bin/lipo", ["/bin/echo", "-thin", "arm64e", "-output", target.path])
            XCTAssertEqual(thin.status, 0, thin.output)
            // A clone of a platform binary keeps Apple's platform signature, and macOS then hangs any exec inside the
            // bundle. Dropping the signature makes it an ordinary unsigned Mach-O.
            let unsigned = try run("/usr/bin/codesign", ["--remove-signature", target.path])
            XCTAssertEqual(unsigned.status, 0, unsigned.output)
            try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: target.path)
        }
    }

    private func engineSign(_ arguments: [String]) throws -> (status: Int32, output: String) {
        let script = packageRoot.appendingPathComponent("scripts/engine_sign.py").path
        return try run("/usr/bin/env", ["python3", "-I", "-S", script] + arguments)
    }

    @discardableResult
    private func run(_ executable: String, _ arguments: [String]) throws -> (status: Int32, output: String) {
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
