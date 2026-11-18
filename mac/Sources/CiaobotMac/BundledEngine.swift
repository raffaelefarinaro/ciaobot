import Foundation
import Darwin

/// The engine shipped in Ciaobot.app/Contents/Resources/engine (#3128).
/// It only locates the launcher and describes how to start it. Nothing calls
/// it at launch or login yet; #3129 wires it in.
struct BundledEngine: Equatable {
    enum LocateError: Error, Equatable {
        case noResourceDirectory
        case unsupportedArchitecture
        case missing(String)
        case symlinked(String)
        case notADirectory(String)
        case notARegularFile(String)
        case notExecutable(String)
    }

    enum Command: Equatable {
        case version
        case run([String])
    }

    static let directoryName = "engine"
    static let launcherPath = "bin/ciao"
    static let interpreterPath = "bin/python3.12"
    static let provenancePath = "provenance.json"
    static let strippedPrefixes = ["CIAO_", "PWA_", "PYTHON", "DYLD_"]
    static let strippedNames: Set<String> = [
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    ]

    static var hostIsArm64: Bool {
        #if arch(arm64)
        return true
        #else
        return false
        #endif
    }

    /// Physical path of Contents/Resources/engine.
    let root: URL
    var launcher: URL { root.appendingPathComponent(Self.launcherPath) }

    private enum Kind { case directory, regular, executable }

    static func locate(
        resourceDirectory: URL?,
        hostIsArm64: Bool = BundledEngine.hostIsArm64
    ) throws -> BundledEngine {
        guard hostIsArm64 else { throw LocateError.unsupportedArchitecture }
        guard let resourceDirectory else { throw LocateError.noResourceDirectory }
        let base = resourceDirectory.resolvingSymlinksInPath()
        let entries: [(String, Kind)] = [
            (directoryName, .directory),
            ("\(directoryName)/bin", .directory),
            ("\(directoryName)/\(launcherPath)", .executable),
            ("\(directoryName)/\(interpreterPath)", .regular),
            ("\(directoryName)/\(provenancePath)", .regular),
        ]
        for (relative, kind) in entries {
            let path = base.appendingPathComponent(relative).path
            var info = stat()
            guard lstat(path, &info) == 0 else {
                throw LocateError.missing(relative)
            }
            let mode = info.st_mode & S_IFMT
            if mode == S_IFLNK { throw LocateError.symlinked(relative) }
            if kind == .directory {
                guard mode == S_IFDIR else { throw LocateError.notADirectory(relative) }
            } else {
                guard mode == S_IFREG else { throw LocateError.notARegularFile(relative) }
            }
            if kind == .executable, access(path, X_OK) != 0 {
                throw LocateError.notExecutable(relative)
            }
        }
        return BundledEngine(root: base.appendingPathComponent(directoryName))
    }

    static func locateInMainBundle() throws -> BundledEngine {
        try locate(resourceDirectory: Bundle.main.resourceURL)
    }

    /// The parent environment minus engine and interpreter variables. Nothing is added.
    static func environment(from parent: [String: String]) -> [String: String] {
        parent.filter { key, _ in
            !strippedNames.contains(key) && !strippedPrefixes.contains { key.hasPrefix($0) }
        }
    }

    func launch(
        _ command: Command,
        parentEnvironment: [String: String],
        workingDirectory: URL
    ) -> BundledEngineLaunch {
        let arguments: [String]
        switch command {
        case .version: arguments = ["--version"]
        case .run(let extra): arguments = ["run"] + extra
        }
        return BundledEngineLaunch(
            executable: launcher,
            arguments: arguments,
            environment: Self.environment(from: parentEnvironment),
            workingDirectory: workingDirectory
        )
    }
}

struct BundledEngineLaunch: Equatable {
    let executable: URL
    let arguments: [String]
    let environment: [String: String]
    let workingDirectory: URL
}
