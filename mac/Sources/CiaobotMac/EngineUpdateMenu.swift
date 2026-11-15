import Combine
import Foundation

/// One row of the menu's engine-update block. Progress and outcome rows are
/// disabled and only show text; the action row is the single enabled item.
enum EngineUpdateMenuRow: Equatable {
    case progress(String)                                      // disabled row
    case outcome(title: String, toolTip: String, failed: Bool) // disabled row, full text in tooltip
    case action(title: String, target: EngineUpdateTarget)     // enabled row, runs the update

    var isOutcome: Bool {
        if case .outcome = self { return true }
        return false
    }
}

/// What the menu bar reads. The menu never sees the updater or the probe.
@MainActor
protocol EngineUpdateMenuProviding: AnyObject {
    var rows: [EngineUpdateMenuRow] { get }
    /// Fires (before the new value is stored, like @Published) whenever rows may change.
    var changes: AnyPublisher<Void, Never> { get }
    func start()
    func stop()
    func refreshIfNeeded()     // menu open; throttled
    func performUpdate()       // the action row's click
    func acknowledgeOutcome()  // menu closed after showing an outcome
}

/// Every title and tooltip the menu's engine-update rows show. The rows are
/// computed here, without AppKit, so each title is testable.
enum EngineUpdateMenuCopy {
    static let maximumTitleLength = 120
    static let actionToolTip = "Updates the Ciaobot engine on this Mac: downloads and verifies the new version, waits for active chats to finish, then restarts the engine. This Mac client keeps running. If the new version does not start, the previous one is restored."

    static func updateTitle(version: String) -> String { "Update Ciaobot to \(version)…" }
    static func retryTitle(version: String) -> String { "Try Updating Ciaobot to \(version) Again…" }

    static func rows(state: EngineUpdateState, target: EngineUpdateTarget?) -> [EngineUpdateMenuRow] {
        switch state {
        case .staging, .applying:
            return [.progress(clip(EngineUpdateCopy.progress(for: state) ?? ""))]
        case .succeeded:
            let text = EngineUpdateCopy.outcome(for: state) ?? ""
            return [.outcome(title: clip(text), toolTip: text, failed: false)]
                + (target.map { [.action(title: updateTitle(version: $0.version), target: $0)] } ?? [])
        case .failed(let message):
            return [.outcome(title: clip(message), toolTip: message, failed: true)]
                + (target.map { [.action(title: retryTitle(version: $0.version), target: $0)] } ?? [])
        case .idle:
            return target.map { [.action(title: updateTitle(version: $0.version), target: $0)] } ?? []
        }
    }

    private static func clip(_ text: String) -> String {
        MenuTextSanitizer.clip(text, limit: maximumTitleLength)
    }
}

/// Offers the local engine's update in the menu. It probes the connected
/// origin's `/api/startup-status` without credentials, and only when that
/// origin is loopback. The probe runs on connect, every 30 minutes, and when
/// the menu opens (at most once a minute). It never runs `ciao` except from
/// `performUpdate()`. A remote server is never probed and never offered anything.
@MainActor
final class EngineUpdateMonitor: ObservableObject, EngineUpdateMenuProviding {
    static let periodicInterval: TimeInterval = 1800
    static let menuOpenThrottle: TimeInterval = 60

    @Published private(set) var target: EngineUpdateTarget?

    private let status: AppSessionStatus
    private let updater: EngineUpdater
    private let detectInstallation: () -> LocalInstallation?
    private let processExists: (Int32) -> Bool
    private let transport: (Origin, @escaping (StartupInfo?) -> Void) -> Void
    private let ticker: MenuTicking
    private let now: () -> Date
    private var connectionSubscription: AnyCancellable?
    private var updaterSubscription: AnyCancellable?
    private var probedOriginKey: String?
    private var lastProbeAt: Date?
    /// Invalidates a probe answer from an earlier origin, connection or stop.
    private var generation = 0

    init(
        status: AppSessionStatus,
        updater: EngineUpdater,
        detectInstallation: @escaping () -> LocalInstallation? = { LocalInstallation.detect() },
        processExists: @escaping (Int32) -> Bool = LocalProcess.exists,
        transport: @escaping (Origin, @escaping (StartupInfo?) -> Void) -> Void = { origin, completion in
            Probe().inspect(origin, completion: completion) },
        ticker: MenuTicking = RunLoopMenuTicker(),
        now: @escaping () -> Date = Date.init
    ) {
        self.status = status
        self.updater = updater
        self.detectInstallation = detectInstallation
        self.processExists = processExists
        self.transport = transport
        self.ticker = ticker
        self.now = now
    }

    var rows: [EngineUpdateMenuRow] { EngineUpdateMenuCopy.rows(state: updater.state, target: target) }
    var changes: AnyPublisher<Void, Never> {
        Publishers.Merge($target.map { _ in () }, updater.$state.map { _ in () }).eraseToAnyPublisher()
    }

    func start() {
        guard connectionSubscription == nil else { return }
        // AppSessionStatus sets currentOrigin before assigning `connection`
        // (SessionStatus.swift reportConnected/reportDisconnected), so the
        // sink can read the new origin synchronously.
        connectionSubscription = status.$connection.sink { [weak self] _ in self?.connectionChanged() }
        updaterSubscription = updater.$state.sink { [weak self] state in
            guard let self, state.requiresRediscovery else { return }
            // `applied`: the installed version changed. Drop the stale offer now, then re-probe.
            self.target = nil
            self.refresh()
        }
    }

    func stop() {
        connectionSubscription?.cancel(); connectionSubscription = nil
        updaterSubscription?.cancel(); updaterSubscription = nil
        ticker.stop()
        generation &+= 1
        probedOriginKey = nil
        lastProbeAt = nil
    }

    func refreshIfNeeded() {
        guard connectionSubscription != nil else { return }
        if let lastProbeAt, now().timeIntervalSince(lastProbeAt) < Self.menuOpenThrottle { return }
        refresh()
    }

    func performUpdate() {
        guard let target, !updater.state.isActive else { return }
        updater.start(target)
    }

    func acknowledgeOutcome() { updater.reset() }

    private func connectionChanged() {
        let origin = status.currentOrigin
        guard origin?.canonicalKey != probedOriginKey else { return }
        probedOriginKey = origin?.canonicalKey
        generation &+= 1
        lastProbeAt = nil
        target = nil
        ticker.stop()
        // A remote server is never probed and never offered anything.
        guard let origin, origin.isLoopback else { return }
        ticker.start(interval: Self.periodicInterval) { [weak self] in self?.refresh() }
        refresh()
    }

    private func refresh() {
        guard let origin = status.currentOrigin, origin.isLoopback else {
            target = nil
            return
        }
        generation &+= 1
        let request = generation
        lastProbeAt = now()
        let installation = detectInstallation()
        transport(origin) { [weak self] info in
            guard let self, self.generation == request,
                  self.status.currentOrigin?.canonicalKey == origin.canonicalKey else { return }
            self.target = Self.target(info: info, origin: origin, installation: installation,
                                      processExists: self.processExists)
        }
    }

    static func target(info: StartupInfo?, origin: Origin, installation: LocalInstallation?,
                       processExists: (Int32) -> Bool) -> EngineUpdateTarget? {
        // Only the installed engine's own port: another loopback server must not
        // be updated through this Mac's receipt executable.
        guard let info, let installation,
              (origin.url.port ?? (origin.usesTLS ? 443 : 80)) == installation.port else { return nil }
        let onThisMac = info.pid.map(processExists) ?? false
        return EngineUpdateOffer.target(
            status: .running(version: info.version, ready: info.ready, onThisMac: onThisMac),
            candidate: origin, installation: installation,
            availableVersion: LocalServerDiscovery.availableVersion(info))
    }
}
