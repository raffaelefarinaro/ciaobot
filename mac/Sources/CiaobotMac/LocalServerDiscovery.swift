import Combine
import Darwin
import Foundation

/// The address used when no local installation names another port.
enum LocalServerCandidate {
    static let defaultPort = 8443
    static let address = "http://127.0.0.1:\(defaultPort)"
    static let origin = Origin(address)

    static func origin(port: Int) -> Origin? {
        Origin("http://127.0.0.1:\(port)")
    }
}

/// Whether the connection screen may start the local check by itself.
/// Automatic same-Mac discovery is a production convenience only: the debug
/// app used by the test suite must never contact the real candidate.
enum LocalServerDiscoveryPolicy {
    static func automaticallyProbes(isDebugBuild: Bool, testLaunchRequested: Bool) -> Bool {
        !isDebugBuild && !testLaunchRequested
    }

    static var automaticInThisBuild: Bool {
        #if DEBUG
        automaticallyProbes(isDebugBuild: true, testLaunchRequested: true)
        #else
        automaticallyProbes(isDebugBuild: false, testLaunchRequested: false)
        #endif
    }
}

/// What the engine's installer leaves on this Mac. Every source is optional
/// and read fail-safe: a missing, unreadable, or malformed file reads as
/// absent. Only display facts and the engine's own command path are kept
/// (version, port, service presence, executable); the workspace `.env` is
/// scanned for `PWA_PORT` alone and nothing else in it is retained.
struct LocalInstallation: Equatable {
    static let serviceLabel = "com.ciao.server"

    /// The engine's LaunchAgent is registered for this user.
    var hasService: Bool
    /// Version from the installer receipt, when one exists.
    var installedVersion: String?
    /// The port the engine serves on, resolved the way the engine does:
    /// workspace `.env` `PWA_PORT`, then the service's `CIAO_PORT`, then 8443.
    var port: Int
    /// The LaunchAgent file, used to register the service if launchd has
    /// dropped it.
    var servicePlist: URL? = nil
    /// The receipt's `executable`, only when it is an absolute path to an existing, executable, non-directory file.
    var executable: URL? = nil
    /// The workspace the service runs in, resolved the way the engine does:
    /// `CIAO_WORKSPACE`, then `WorkingDirectory`. Used to reach its `.env`.
    var workspace: URL? = nil

    var origin: Origin? { LocalServerCandidate.origin(port: port) }

    /// Nil when nothing suggests an engine is installed for this user.
    static func detect(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> LocalInstallation? {
        let plistURL = home.appendingPathComponent("Library/LaunchAgents/\(serviceLabel).plist")
        let plist = (try? Data(contentsOf: plistURL))
            .flatMap { try? PropertyListSerialization.propertyList(from: $0, format: nil) as? [String: Any] }
        let receiptURL = home.appendingPathComponent(".local/state/ciaobot/install-receipt.json")
        let receipt = (try? Data(contentsOf: receiptURL))
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        guard plist != nil || receipt != nil else { return nil }

        let environment = plist?["EnvironmentVariables"] as? [String: Any] ?? [:]
        let workspace = (environment["CIAO_WORKSPACE"] as? String).flatMap(nonEmpty)
            ?? (plist?["WorkingDirectory"] as? String).flatMap(nonEmpty)
        let workspaceURL = workspace.map { URL(fileURLWithPath: $0) }
        let dotenvPort = workspaceURL.flatMap { dotenvValue("PWA_PORT", in: $0.appendingPathComponent(".env")) }
        let servicePort = environment["CIAO_PORT"] as? String
        let port = [dotenvPort, servicePort].lazy.compactMap { $0.flatMap(validPort) }.first
            ?? LocalServerCandidate.defaultPort

        let version = (receipt?["version"] as? String).flatMap(nonEmpty)
            .flatMap { $0.count <= StartupInfo.maximumVersionLength ? $0 : nil }
        let executable = (receipt?["executable"] as? String).flatMap(nonEmpty)
            .flatMap { path -> URL? in
                let url = URL(fileURLWithPath: path)
                guard path.hasPrefix("/"), EngineUpdater.isExecutableFile(url) else { return nil }
                return url
            }
        return LocalInstallation(
            hasService: plist != nil,
            installedVersion: version,
            port: port,
            servicePlist: plist != nil ? plistURL : nil,
            executable: executable,
            workspace: workspaceURL
        )
    }

    private static func nonEmpty(_ text: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private static func validPort(_ text: String) -> Int? {
        guard let port = Int(text.trimmingCharacters(in: .whitespacesAndNewlines)), (1...65535).contains(port) else { return nil }
        return port
    }

    /// One `KEY=value` line from a dotenv file, ignoring comments, `export`,
    /// and surrounding quotes. Other lines are never kept.
    static func dotenvValue(_ key: String, in url: URL) -> String? {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        var found: String?
        for rawLine in text.split(whereSeparator: \.isNewline) {
            var line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("export ") { line = String(line.dropFirst(7)).trimmingCharacters(in: .whitespaces) }
            guard !line.hasPrefix("#"), let equals = line.firstIndex(of: "="),
                  line[..<equals].trimmingCharacters(in: .whitespaces) == key else { continue }
            var value = line[line.index(after: equals)...].trimmingCharacters(in: .whitespaces)
            if value.count >= 2, let first = value.first, first == value.last, first == "\"" || first == "'" {
                value = String(value.dropFirst().dropLast())
            }
            found = value
        }
        return found
    }
}

/// Whether a process id names a live process on this Mac. `EPERM` still means
/// the process exists (owned by another user).
enum LocalProcess {
    static func exists(_ pid: Int32) -> Bool {
        guard pid > 0 else { return false }
        return kill(pid, 0) == 0 || errno == EPERM
    }
}

/// Starts the engine's existing LaunchAgent. It never installs or edits a
/// service: `kickstart` runs the registered job, and only if launchd has no
/// such job is the installer's own plist bootstrapped first.
enum LocalServiceStarter {
    typealias Runner = (_ arguments: [String], _ completion: @escaping (Int32) -> Void) -> Void

    static func start(
        _ installation: LocalInstallation,
        uid: uid_t = getuid(),
        run: @escaping Runner = launchctl,
        completion: @escaping (Bool) -> Void
    ) {
        let domain = "gui/\(uid)"
        let job = "\(domain)/\(LocalInstallation.serviceLabel)"
        run(["kickstart", job]) { status in
            guard status != 0, let plist = installation.servicePlist else { return completion(status == 0) }
            run(["bootstrap", domain, plist.path]) { _ in
                run(["kickstart", job]) { completion($0 == 0) }
            }
        }
    }

    static func launchctl(_ arguments: [String], completion: @escaping (Int32) -> Void) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = arguments
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { finished in
            let status = finished.terminationStatus
            DispatchQueue.main.async { completion(status) }
        }
        do {
            try process.run()
        } catch {
            DispatchQueue.main.async { completion(-1) }
        }
    }
}

/// Finds Ciaobot on this Mac for the connection screen: reads the installer's
/// files for the port and installed version, asks that loopback address for
/// its startup status with the credential-free `Probe`, and checks that the
/// reported process lives on this Mac. It never navigates or signs in; the
/// user chooses whether to connect.
@MainActor
final class LocalServerDiscovery: ObservableObject {
    enum Status: Equatable {
        case idle
        case checking
        /// A Ciaobot server answered. `onThisMac` is false when the process it
        /// reported does not exist here (a forwarded port, a container).
        case running(version: String?, ready: Bool, onThisMac: Bool)
        /// The installer's files are present but nothing answered.
        case installedNotRunning(version: String?)
        /// Start was requested; waiting for the server to answer.
        case starting
        /// launchd refused to start the service, or it never answered.
        case startFailed(version: String?)
        case notDetected
    }

    /// How long a started server has to answer, and how often it is asked.
    static let startTimeout: TimeInterval = 60
    static let startPollInterval: TimeInterval = 2

    @Published private(set) var status: Status = .idle
    /// The address that was checked last.
    @Published private(set) var candidate: Origin?
    @Published private(set) var installation: LocalInstallation?
    /// A newer version the running server reported, or nil.
    @Published private(set) var availableVersion: String?

    private let fixedCandidate: Origin?
    private let detectInstallation: () -> LocalInstallation?
    private let processExists: (Int32) -> Bool
    private let transport: (Origin, @escaping (StartupInfo?) -> Void) -> Void
    private let startService: (LocalInstallation, @escaping (Bool) -> Void) -> Void
    private let after: (TimeInterval, @escaping () -> Void) -> Void
    /// Guards against a late response from a check that belonged to a screen
    /// that is no longer displayed, or to an older check on the same screen.
    private var generation = 0

    /// `candidate` overrides the address the installation would name.
    init(
        candidate: Origin? = nil,
        detectInstallation: @escaping () -> LocalInstallation? = { LocalInstallation.detect() },
        processExists: @escaping (Int32) -> Bool = LocalProcess.exists,
        startService: @escaping (LocalInstallation, @escaping (Bool) -> Void) -> Void = { installation, completion in
            LocalServiceStarter.start(installation, completion: completion)
        },
        after: @escaping (TimeInterval, @escaping () -> Void) -> Void = { delay, work in
            DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
        },
        transport: @escaping (Origin, @escaping (StartupInfo?) -> Void) -> Void = { origin, completion in
            Probe().inspect(origin, completion: completion)
        }
    ) {
        self.fixedCandidate = candidate
        self.detectInstallation = detectInstallation
        self.processExists = processExists
        self.startService = startService
        self.after = after
        self.transport = transport
    }

    var isChecking: Bool { status == .checking || status == .starting }

    /// Start is offered only for an installed LaunchAgent that is not answering.
    var canStart: Bool {
        guard installation?.hasService == true else { return false }
        switch status {
        case .installedNotRunning, .startFailed: return true
        default: return false
        }
    }
    var address: String { candidate?.canonicalKey ?? LocalServerCandidate.address }
    /// The engine update this screen may offer, or nil when none qualifies.
    var updateTarget: EngineUpdateTarget? {
        EngineUpdateOffer.target(status: status, candidate: candidate, installation: installation,
                                 availableVersion: availableVersion)
    }

    var statusText: String {
        let base = LocalServerDiscoveryCopy.text(for: status, address: address)
        guard case .running = status, let latest = availableVersion else { return base }
        return base + " " + EngineUpdateCopy.availability(version: latest, updatable: updateTarget != nil)
    }

    /// A newer version the running server reported, unless it equals the running one.
    static func availableVersion(_ info: StartupInfo) -> String? {
        guard info.updateAvailable, let latest = info.latestVersion, latest != info.version else { return nil }
        return latest
    }

    /// The origin to offer as a one-click connection, when a server answered.
    var connectableOrigin: Origin? {
        if case .running = status { return candidate }
        return nil
    }

    /// Starts one check. Returns false when a check is already in flight, so a
    /// double click or a repeated Return cannot multiply the request.
    @discardableResult
    func refresh() -> Bool {
        guard !isChecking else { return false }
        let found = detectInstallation()
        guard let target = fixedCandidate ?? found?.origin ?? LocalServerCandidate.origin else { return false }
        installation = found
        candidate = target
        generation &+= 1
        let request = generation
        availableVersion = nil
        status = .checking
        transport(target) { [weak self] info in
            guard let self, self.generation == request, self.status == .checking else { return }
            if let info {
                self.status = self.running(info, at: target)
            } else if let found {
                self.status = .installedNotRunning(version: found.installedVersion)
            } else {
                self.status = .notDetected
            }
        }
        return true
    }

    /// Asks launchd to start the installed service, then polls the address
    /// until it answers or `startTimeout` passes. Returns false when start is
    /// not offered.
    @discardableResult
    func start() -> Bool {
        guard canStart, let installation, let target = candidate else { return false }
        generation &+= 1
        let request = generation
        availableVersion = nil
        status = .starting
        startService(installation) { [weak self] started in
            guard let self, self.generation == request else { return }
            guard started else {
                self.status = .startFailed(version: installation.installedVersion)
                return
            }
            self.poll(target, request: request, remaining: Int(Self.startTimeout / Self.startPollInterval))
        }
        return true
    }

    private func poll(_ target: Origin, request: Int, remaining: Int) {
        transport(target) { [weak self] info in
            guard let self, self.generation == request, self.status == .starting else { return }
            if let info {
                self.status = self.running(info, at: target)
            } else if remaining <= 0 {
                self.status = .startFailed(version: self.installation?.installedVersion)
            } else {
                self.after(Self.startPollInterval) { [weak self] in
                    guard let self, self.generation == request, self.status == .starting else { return }
                    self.poll(target, request: request, remaining: remaining - 1)
                }
            }
        }
    }

    private func running(_ info: StartupInfo, at target: Origin) -> Status {
        let local = target.isLoopback && (info.pid.map(processExists) ?? false)
        availableVersion = Self.availableVersion(info)
        return .running(version: info.version, ready: info.ready, onThisMac: local)
    }

    /// The connection screen went away (or the selected server changed). A
    /// response that arrives afterwards must not update the new screen.
    func invalidate() {
        generation &+= 1
        availableVersion = nil
        status = .idle
    }
}

enum LocalServerDiscoveryCopy {
    static let accessibilityLabel = "Local Ciaobot status"
    static let hint = "Reads this Mac's Ciaobot service files and asks the local address for its version, without credentials. It does not sign in."

    static func text(for status: LocalServerDiscovery.Status, address: String) -> String {
        switch status {
        case .idle:
            return "Check whether Ciaobot is installed and running on this Mac."
        case .checking:
            return "Looking for Ciaobot on this Mac…"
        case let .running(version, ready, onThisMac):
            let name = version.map { "Ciaobot \($0)" } ?? "Ciaobot"
            let starting = ready ? "" : " It is still starting."
            if onThisMac {
                return "\(name) is running on this Mac at \(address).\(starting)"
            }
            return "\(name) answered at \(address), but its process is not on this Mac (for example a forwarded port).\(starting)"
        case let .installedNotRunning(version):
            let name = version.map { "Ciaobot \($0)" } ?? "Ciaobot"
            return "\(name) is installed on this Mac but not answering at \(address)."
        case .starting:
            return "Starting Ciaobot…"
        case let .startFailed(version):
            let name = version.map { "Ciaobot \($0)" } ?? "Ciaobot"
            return "\(name) did not start at \(address). Check its logs, or start it from Terminal with `ciao service start`."
        case .notDetected:
            return "Ciaobot was not found on this Mac. Enter your server’s address below."
        }
    }
}
