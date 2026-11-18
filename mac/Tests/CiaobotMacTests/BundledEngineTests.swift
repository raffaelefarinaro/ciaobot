import XCTest
@testable import CiaobotMac

final class BundledEngineTests: XCTestCase {
    private var scratch: URL!
    private var resources: URL!

    override func setUpWithError() throws {
        scratch = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("BundledEngineTests-\(UUID().uuidString)")
            .resolvingSymlinksInPath()
        resources = scratch.appendingPathComponent("Fixture.app/Contents/Resources")
        try makeEngine(at: resources)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: scratch)
    }

    private let rawScript = "#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done\n/usr/bin/env\n"

    private func makeEngine(at resources: URL) throws {
        let fm = FileManager.default
        let bin = resources.appendingPathComponent("engine/bin")
        try fm.createDirectory(at: bin, withIntermediateDirectories: true)
        let ciao = bin.appendingPathComponent("ciao")
        try rawScript.write(to: ciao, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: ciao.path)
        try "py".write(to: bin.appendingPathComponent("python3.12"), atomically: true, encoding: .utf8)
        try "{}".write(
            to: resources.appendingPathComponent("engine/provenance.json"),
            atomically: true, encoding: .utf8)
    }

    private func entry(_ rel: String) -> URL { resources.appendingPathComponent("engine/\(rel)") }

    private func locate(_ dir: URL? = nil) throws -> BundledEngine {
        try BundledEngine.locate(resourceDirectory: dir ?? resources, hostIsArm64: true)
    }

    private func assertLocateError(
        _ expected: BundledEngine.LocateError, _ dir: URL? = nil,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        XCTAssertThrowsError(try locate(dir), file: file, line: line) {
            XCTAssertEqual($0 as? BundledEngine.LocateError, expected, file: file, line: line)
        }
    }

    private func runProcess(_ launch: BundledEngineLaunch) throws -> (Int32, String) {
        let process = Process()
        process.executableURL = launch.executable
        process.arguments = launch.arguments
        process.environment = launch.environment
        process.currentDirectoryURL = launch.workingDirectory
        let pipe = Pipe()
        process.standardOutput = pipe
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus, String(decoding: data, as: UTF8.self))
    }

    func testLocatesTheEngineAndReturnsThePhysicalRoot() throws {
        let engine = try locate()
        XCTAssertEqual(engine.root.path, resources.appendingPathComponent("engine").path)
        XCTAssertTrue(engine.launcher.path.hasSuffix("engine/bin/ciao"))
    }

    func testRefusesNonArm64BeforeAnyIO() {
        let nowhere = scratch.appendingPathComponent("nonexistent")
        XCTAssertThrowsError(try BundledEngine.locate(resourceDirectory: nowhere, hostIsArm64: false)) {
            XCTAssertEqual($0 as? BundledEngine.LocateError, .unsupportedArchitecture)
        }
    }

    func testNilResourceDirectoryIsRefused() {
        XCTAssertThrowsError(try BundledEngine.locate(resourceDirectory: nil, hostIsArm64: true)) {
            XCTAssertEqual($0 as? BundledEngine.LocateError, .noResourceDirectory)
        }
    }

    func testEachRequiredEntryMustExist() throws {
        let rels = ["engine", "engine/bin", "engine/bin/ciao", "engine/bin/python3.12", "engine/provenance.json"]
        for rel in rels {
            let copy = scratch.appendingPathComponent("copy-\(rel.replacingOccurrences(of: "/", with: "-"))/Resources")
            try makeEngine(at: copy)
            try FileManager.default.removeItem(at: copy.appendingPathComponent(rel))
            assertLocateError(.missing(rel), copy)
        }
    }

    func testSymlinkedComponentsAreRefused() throws {
        let fm = FileManager.default
        for rel in ["engine", "engine/bin", "engine/bin/ciao"] {
            let dir = scratch.appendingPathComponent("link-\(rel.replacingOccurrences(of: "/", with: "-"))/Resources")
            try makeEngine(at: dir)
            let elsewhere = scratch.appendingPathComponent("elsewhere-\(rel.replacingOccurrences(of: "/", with: "-"))")
            try makeEngine(at: elsewhere)
            let target = dir.appendingPathComponent(rel)
            try fm.removeItem(at: target)
            try fm.createSymbolicLink(at: target, withDestinationURL: elsewhere.appendingPathComponent(rel))
            assertLocateError(.symlinked(rel), dir)
        }
    }

    func testAncestorSymlinkIsAcceptedAndResolved() throws {
        let link = scratch.appendingPathComponent("link-to-contents")
        try FileManager.default.createSymbolicLink(
            at: link, withDestinationURL: resources.deletingLastPathComponent())
        let engine = try locate(link.appendingPathComponent("Resources"))
        XCTAssertEqual(engine.root.path, resources.appendingPathComponent("engine").path)
    }

    func testWrongTypesAndModes() throws {
        let fm = FileManager.default
        let ciao = entry("bin/ciao")
        try fm.setAttributes([.posixPermissions: 0o644], ofItemAtPath: ciao.path)
        assertLocateError(.notExecutable("engine/bin/ciao"))
        try fm.removeItem(at: ciao)
        try fm.createDirectory(at: ciao, withIntermediateDirectories: false)
        assertLocateError(.notARegularFile("engine/bin/ciao"))

        let other = scratch.appendingPathComponent("other/Resources")
        try makeEngine(at: other)
        let bin = other.appendingPathComponent("engine/bin")
        try fm.removeItem(at: bin)
        try "x".write(to: bin, atomically: true, encoding: .utf8)
        assertLocateError(.notADirectory("engine/bin"), other)
    }

    func testLaunchValuesAreExact() throws {
        let engine = try locate()
        let home = scratch.appendingPathComponent("home")
        let version = engine.launch(.version, parentEnvironment: [:], workingDirectory: home)
        XCTAssertEqual(version.arguments, ["--version"])
        XCTAssertEqual(version.executable, engine.launcher)
        XCTAssertTrue(version.executable.path.hasPrefix("/"))
        XCTAssertEqual(version.workingDirectory, home)
        let run = engine.launch(.run(["--port", "1"]), parentEnvironment: [:], workingDirectory: home)
        XCTAssertEqual(run.arguments, ["run", "--port", "1"])
        XCTAssertEqual(run.executable, engine.launcher)
        let withPort = engine.launch(.run(["--app-engine"]), port: 9100, parentEnvironment: [:], workingDirectory: home)
        XCTAssertEqual(withPort.arguments, ["run", "--app-engine", "--port", "9100"])
        let versionWithPort = engine.launch(.version, port: 9100, parentEnvironment: [:], workingDirectory: home)
        XCTAssertEqual(versionWithPort.arguments, ["--version"], "a port belongs to a run only")
    }

    func testEnvironmentStripsEngineAndInterpreterVariables() {
        let parent = [
            "CIAO_MEMORY_DIR": "a", "PWA_PORT": "b", "PYTHONPATH": "c", "PYTHONHOME": "d",
            "DYLD_INSERT_LIBRARIES": "e", "SSL_CERT_FILE": "f", "SSL_CERT_DIR": "g",
            "REQUESTS_CA_BUNDLE": "h", "CURL_CA_BUNDLE": "i",
            "HOME": "/h", "PATH": "/bin", "LANG": "C",
        ]
        XCTAssertEqual(
            BundledEngine.environment(from: parent),
            ["HOME": "/h", "PATH": "/bin", "LANG": "C"])
    }

    func testLaunchValueRunsDirectlyWithoutAShell() throws {
        let engine = try locate()
        let launch = engine.launch(
            .run(["a b"]),
            parentEnvironment: ["CIAO_X": "1", "PYTHONPATH": "p", "KEEP": "yes"],
            workingDirectory: scratch)
        let (status, out) = try runProcess(launch)
        XCTAssertEqual(status, 0)
        let lines = out.split(separator: "\n").map(String.init)
        XCTAssertEqual(Array(lines.prefix(2)), ["run", "a b"])
        XCTAssertTrue(lines.contains("KEEP=yes"))
        XCTAssertFalse(lines.contains { $0.hasPrefix("CIAO_X=") || $0.hasPrefix("PYTHONPATH=") })
    }

    func testEnvironmentStripsExactlyTheExpectedNames() {
        let stripped = [
            "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
            "OPENSSL_CONF", "OPENSSL_MODULES", "OPENSSL_ENGINES", "SSLKEYLOGFILE",
            "NODE_EXTRA_CA_CERTS", "NODE_OPTIONS", "NODE_TLS_REJECT_UNAUTHORIZED",
            "PYTHONDONTWRITEBYTECODE", "PYTHONPATH", "DYLD_LIBRARY_PATH",
            "CIAO_HOME", "PWA_PORT",
        ]
        let kept = ["MYCIAO_X": "1", "pythonpath": "2", "XDYLD_FOO": "3", "HOME": "/h"]
        var parent = kept
        for name in stripped { parent[name] = "v" }
        XCTAssertEqual(BundledEngine.environment(from: parent), kept)
    }

    func testLaunchExecutableIsTheLauncherNotAShell() throws {
        let engine = try locate()
        for command in [BundledEngine.Command.version, .run(["x"])] {
            let launch = engine.launch(command, parentEnvironment: [:], workingDirectory: scratch)
            XCTAssertEqual(launch.executable.path, engine.root.appendingPathComponent("bin/ciao").path)
            XCTAssertNotEqual(launch.executable.path, "/bin/sh")
            XCTAssertFalse(launch.arguments.contains("-c"))
        }
    }

    func testSourceAvoidsInstallerPathsAndShells() throws {
        let text = try String(contentsOf: Self.sourcesDirectory.appendingPathComponent("BundledEngine.swift"))
        for banned in ["NativeInstaller", "EngineUpdater", "EngineRelease", "/usr/bin/env", "Process(",
                       "NSTask", "posix_spawn", "/bin/sh", "/bin/zsh", "/bin/bash"] {
            XCTAssertFalse(text.contains(banned), "BundledEngine.swift mentions \(banned)")
        }
        XCTAssertNil(text.range(of: "\\bwhich\\b", options: .regularExpression), "mentions which")
    }

    func testOnlyTheSupervisorAndAppDelegateReferenceTheBundledEngine() throws {
        let enumerator = try XCTUnwrap(FileManager.default.enumerator(atPath: Self.sourcesDirectory.path))
        var checked = 0
        while let rel = enumerator.nextObject() as? String {
            // EngineSupervisor runs the engine; AppDelegate (#3129 S5b) locates it
            // and hands it over. The installer, updater and views stay clear of it.
            let name = URL(fileURLWithPath: rel).lastPathComponent
            guard rel.hasSuffix(".swift"),
                  !["BundledEngine.swift", "EngineSupervisor.swift", "AppDelegate.swift"].contains(name) else { continue }
            checked += 1
            let text = try String(contentsOf: Self.sourcesDirectory.appendingPathComponent(rel))
            let code = text.split(separator: "\n", omittingEmptySubsequences: false)
                .filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix("//") }
            XCTAssertFalse(code.contains { $0.contains("BundledEngine") }, "\(rel) references BundledEngine")
        }
        XCTAssertGreaterThan(checked, 0)
    }

    func testBuiltAppEngineReportsItsProvenanceVersion() throws {
        let resourcesDir = Self.packageRoot
            .appendingPathComponent(".build/Ciaobot.app/Contents/Resources")
        guard FileManager.default.fileExists(atPath: resourcesDir.appendingPathComponent("engine").path) else {
            throw XCTSkip("no engine-bearing app at \(resourcesDir.path); run scripts/build-app.sh first")
        }
        let engine = try BundledEngine.locate(resourceDirectory: resourcesDir)
        let data = try Data(contentsOf: engine.root.appendingPathComponent("provenance.json"))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let version = try XCTUnwrap((object["engine"] as? [String: Any])?["version"] as? String)
        let launch = engine.launch(.version, parentEnvironment: [:], workingDirectory: scratch)
        let (status, out) = try runProcess(launch)
        XCTAssertEqual(status, 0)
        XCTAssertEqual(out, "ciao \(version)\n")
        let enumerator = FileManager.default.enumerator(atPath: engine.root.path)
        while let rel = enumerator?.nextObject() as? String {
            XCTAssertFalse(rel.hasSuffix("__pycache__"), "running --version wrote \(rel)")
        }
    }

    private static var packageRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    }

    private static var sourcesDirectory: URL {
        packageRoot.appendingPathComponent("Sources/CiaobotMac")
    }
}
