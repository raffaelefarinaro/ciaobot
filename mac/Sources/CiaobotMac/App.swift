import SwiftUI
import AppKit
import Combine
import WebKit

@main
struct CiaobotMacApp: App {
    // AppKit owns the menu-bar item and the reopen path; SwiftUI keeps the
    // single Window scene. The same session object feeds both.
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        // A single Window (not a WindowGroup) keeps the client to one native
        // main window and removes File > New Window from the menu.
        Window("Ciaobot", id: "main") { ConnectionView(session: appDelegate.session, installer: appDelegate.installer, engineUpdater: appDelegate.engineUpdater) }
            .defaultSize(width: 1100, height: 760)
            .commands {
                CommandGroup(after: .newItem) {
                    Button("Change Server…") {
                        NotificationCenter.default.post(name: .ciaobotChangeServer, object: nil)
                    }
                    .keyboardShortcut("l", modifiers: [.command, .shift])
                }
            }
    }
}

extension Notification.Name {
    static let ciaobotChangeServer = Notification.Name("CiaobotChangeServer")
}

#if DEBUG
/// Test-only launch controls. They are compiled out of release builds and
/// fail closed: an origin is honoured only when it is an explicit loopback
/// origin, and auto-connect never falls back to the proposed live address.
enum TestLaunch {
    static func origin(environment: [String: String] = ProcessInfo.processInfo.environment) -> Origin? {
        guard let raw = environment["CIAOBOT_MAC_TEST_ORIGIN"],
              let origin = Origin(raw), origin.isLoopback else { return nil }
        return origin
    }

    static func autoConnectRequested(environment: [String: String] = ProcessInfo.processInfo.environment) -> Bool {
        environment["CIAOBOT_MAC_TEST_AUTOCONNECT"] == "1"
    }

    static func autoConnect(environment: [String: String] = ProcessInfo.processInfo.environment) -> Bool {
        autoConnectRequested(environment: environment) && origin(environment: environment) != nil
    }

    /// The initial field value. A requested test launch with a missing or
    /// non-loopback origin gets an empty field, never the proposed live
    /// address, so a stray Return cannot contact it.
    static func initialAddress(defaultAddress: String, environment: [String: String] = ProcessInfo.processInfo.environment) -> String {
        if let origin = origin(environment: environment) { return origin.url.absoluteString }
        if autoConnectRequested(environment: environment) { return "" }
        return defaultAddress
    }
}
#endif

@MainActor
struct ConnectionView: View {
    private static let defaultAddress = "http://127.0.0.1:8443"

    @State private var address: String
    @State private var origin: Origin?
    @State private var storage: SessionStorage = .ephemeral
    @State private var dataStore: WKWebsiteDataStore?
    @State private var storageNotice: String?
    @State private var keepSignedInChoice: Bool?
    @State private var tasks = ConnectionTaskGate()
    @State private var error: String?
    @State private var forgetMessage: String?
    @State private var didAutoConnect = false
    @State private var viewTracker = SessionViewTracker()
    @State private var wizardLoadError: String?
    @State private var portText = ""
    @State private var portError: String?
    @State private var pendingPortRestart = false
    @Environment(\.colorScheme) private var colorScheme
    @StateObject private var discovery: LocalServerDiscovery
    @StateObject private var installer: NativeInstaller
    @StateObject private var engineUpdater: EngineUpdater
    private let probe: ProbeChecking
    private let signIn: SessionSignIn
    private let session: AppSessionStatus?
    private let autoConnectTarget: Origin?
    private let autoDiscovery: Bool
    private let lastServer: LastServerStoring
    private let hostStore: HostListStoring
    /// A test launch never reads or writes the saved host list, exactly as it
    /// never reads the saved origin: the operator's real defaults stay out of
    /// the fixture run.
    private let isTestLaunch: Bool
    private var checking: Bool { tasks.task == .checking }
    private var forgetting: Bool { tasks.task == .forgetting }
    /// Connection controls are disabled while a connection task, an install, or an engine update runs.
    private var busy: Bool { tasks.isBusy || installer.state.isActive || engineUpdater.state.isActive }
    private var installFailure: NativeInstallFailure? {
        if case .failed(let failure) = installer.state { return failure }
        return nil
    }
    private var engineUpdateProgress: String { EngineUpdateCopy.progress(for: engineUpdater.state) ?? "" }
    private var engineUpdateFailed: Bool {
        if case .failed = engineUpdater.state { return true }
        return false
    }
    private var repairOffered: Bool {
        NativeRepairOffer.isOffered(discovery: discovery.status, installState: installer.state,
                                    updateFailed: engineUpdateFailed,
                                    hasInstallation: discovery.installation != nil)
    }

    /// The shipped default only. Exposed so a test can prove the view used by
    /// the app matches the build policy without rendering it.
    var automaticallyStartsDiscovery: Bool { autoDiscovery }

    /// The value the address field starts with. Exposed so a test can prove a
    /// saved origin is prefilled without rendering the view.
    let launchAddress: String

    init(
        probe: ProbeChecking = Probe(),
        signIn: SessionSignIn? = nil,
        discovery: LocalServerDiscovery? = nil,
        autoDiscovery: Bool = LocalServerDiscoveryPolicy.automaticInThisBuild,
        session: AppSessionStatus? = nil,
        lastServer: LastServerStoring = UserDefaultsLastServerStore(),
        hostStore: HostListStoring = UserDefaultsHostListStore(lastServer: UserDefaultsLastServerStore()),
        installer: NativeInstaller? = nil,
        engineUpdater: EngineUpdater? = nil
    ) {
        self.probe = probe
        self.signIn = signIn ?? .live
        self.autoDiscovery = autoDiscovery
        self.session = session
        self.lastServer = lastServer
        self.hostStore = hostStore
        _discovery = StateObject(wrappedValue: discovery ?? LocalServerDiscovery())
        _installer = StateObject(wrappedValue: installer ?? NativeInstaller())
        _engineUpdater = StateObject(wrappedValue: engineUpdater ?? EngineUpdater())
        #if DEBUG
        let testOrigin = TestLaunch.origin()
        let testLaunchRequested = TestLaunch.autoConnectRequested()
        // Any test launch — an addressed one, or one that only asked to
        // auto-connect — wins over the saved origin. Nothing in the store may
        // turn a test launch into a connection.
        let isTestLaunch = testOrigin != nil || testLaunchRequested
        // A test launch does not even read the saved value, so it cannot be
        // influenced by (or mutate) the operator's real defaults.
        let savedOrigin = isTestLaunch ? nil : lastServer.savedOrigin
        let initialAddress = isTestLaunch
            ? TestLaunch.initialAddress(defaultAddress: Self.defaultAddress)
            : ConnectionLaunch.initialAddress(saved: savedOrigin, defaultAddress: Self.defaultAddress)
        let autoConnectTarget: Origin?
        if isTestLaunch {
            autoConnectTarget = TestLaunch.autoConnect() ? testOrigin : nil
        } else {
            autoConnectTarget = ConnectionLaunch.autoConnectOrigin(
                saved: savedOrigin,
                isTestLaunch: false,
                isDebugBuild: true
            )
        }
        let testError = testLaunchRequested && testOrigin == nil
            ? "Test mode requires CIAOBOT_MAC_TEST_ORIGIN set to a loopback origin; nothing was contacted."
            : nil
        self.isTestLaunch = isTestLaunch
        #else
        let savedOrigin = lastServer.savedOrigin
        let initialAddress = ConnectionLaunch.initialAddress(saved: savedOrigin, defaultAddress: Self.defaultAddress)
        let autoConnectTarget = ConnectionLaunch.autoConnectOrigin(
            saved: savedOrigin,
            isTestLaunch: false,
            isDebugBuild: false
        )
        let testError: String? = nil
        self.isTestLaunch = false
        #endif
        self.autoConnectTarget = autoConnectTarget
        self.launchAddress = initialAddress
        _address = State(initialValue: initialAddress)
        _error = State(initialValue: testError)
    }

    var body: some View {
        Group {
            if let origin {
                // A fallback is reported where the session actually lives: the
                // connection screen is already gone once the probe succeeds.
                VStack(spacing: 0) {
                    if let storageNotice {
                        Text(storageNotice)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 6)
                    }
                    WebWindow(
                        origin: origin,
                        storage: storage,
                        websiteDataStore: dataStore,
                        onFailure: { message in
                            self.origin = nil
                            self.storage = .ephemeral
                            self.dataStore = nil
                            self.storageNotice = nil
                            self.error = message
                            self.session?.reportDisconnected()
                        },
                        viewTracker: viewTracker,
                        onWebViewReady: { view in
                            self.session?.attachWebView(view, for: origin)
                        },
                        onWorkspaceColor: { accent, view in
                            // Setup and child windows never reach this: only
                            // the connection's own main view is wired, and the
                            // session re-checks that view and origin.
                            guard let view else { return }
                            self.session?.reportWorkspaceAccent(accent, from: view, for: origin)
                        }
                    )
                }
            } else if let wizard = installer.state.wizardOrigin {
                wizardView(wizard)
            } else {
                VStack(alignment: .leading, spacing: 16) {
                    // The supplied full wordmark, not the mark beside typed
                    // text. Its two ink variants follow the window appearance.
                    if let wordmark = colorScheme == .dark
                        ? AppAssets.wordmarkLightImage
                        : AppAssets.wordmarkImage {
                        Image(nsImage: wordmark)
                            .resizable()
                            .interpolation(.high)
                            .scaledToFit()
                            .frame(height: 28)
                            .accessibilityLabel("Ciaobot")
                            .accessibilityAddTraits(.isHeader)
                    } else {
                        // A missing asset drops only the image; the heading
                        // stays, and the screen keeps working.
                        Text("Ciaobot").font(.title.bold())
                    }
                    Text("Enter your server’s address, or connect to the Ciaobot found on this Mac.")
                        .foregroundStyle(.secondary)
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        if discovery.isChecking {
                            ProgressView().controlSize(.small)
                                .accessibilityHidden(true)
                        }
                        Text(discovery.statusText)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityLabel(LocalServerDiscoveryCopy.accessibilityLabel)
                            .accessibilityValue(discovery.statusText)
                        Spacer(minLength: 8)
                        if discovery.canStart {
                            Button("Start Ciaobot") { discovery.start() }
                                .disabled(busy)
                                .accessibilityHint("Starts the Ciaobot service installed on this Mac.")
                        }
                        if repairOffered {
                            Button(NativeInstallFlowCopy.repairButton) {
                                wizardLoadError = nil
                                installer.start(mode: .repair)
                            }
                            // A previous installer child may still be exiting.
                            .disabled(busy || !installer.canStart)
                            .accessibilityHint(NativeInstallFlowCopy.repairHint)
                        }
                        if let local = discovery.connectableOrigin {
                            Button("Connect") {
                                address = local.url.absoluteString
                                connect(to: local)
                            }
                            .disabled(busy)
                            .accessibilityHint("Connects to the Ciaobot found on this Mac.")
                        }
                        Button(discovery.status == .idle ? "Find Ciaobot on This Mac" : "Check Again") {
                            engineUpdater.reset()
                            discovery.refresh()
                        }
                        .disabled(discovery.isChecking || busy)
                        .accessibilityHint(LocalServerDiscoveryCopy.hint)
                    }
                    if engineUpdater.state.isActive {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small).accessibilityHidden(true)
                            Text(engineUpdateProgress).font(.caption)
                                .fixedSize(horizontal: false, vertical: true)
                                .accessibilityLabel(EngineUpdateCopy.accessibilityLabel)
                                .accessibilityValue(engineUpdateProgress)
                        }
                    } else if let target = discovery.updateTarget {
                        HStack(spacing: 8) {
                            Spacer(minLength: 8)
                            Button(engineUpdateFailed ? EngineUpdateCopy.retryButton : EngineUpdateCopy.updateButton) {
                                engineUpdater.start(target)
                            }
                            .disabled(busy)
                            .accessibilityHint(EngineUpdateCopy.hint)
                        }
                    }
                    if let outcome = EngineUpdateCopy.outcome(for: engineUpdater.state) {
                        Text(outcome).font(.caption)
                            .foregroundStyle(engineUpdateFailed ? Color.red : Color.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if installer.state.isActive {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small).accessibilityHidden(true)
                            Text(NativeInstallCopy.progress(for: installer.state, mode: installer.mode) ?? "").font(.caption)
                                .accessibilityLabel("Installation status")
                            Spacer(minLength: 8)
                            Button(installer.mode == .repair ? NativeInstallFlowCopy.cancelRepairButton : NativeInstallFlowCopy.cancelButton) { installer.cancel() }
                                .disabled(installer.state == .cancelling)
                                .accessibilityHint(installer.mode == .repair
                                    ? NativeInstallFlowCopy.cancelRepairHint
                                    : "Stops the installer. A Ciaobot service that already started keeps running.")
                        }
                    } else if NativeInstallOffer.isOffered(discovery: discovery.status, install: installer.state, mode: installer.mode) {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(NativeInstallFlowCopy.offer).font(.caption).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 8)
                            // After a repair that found nothing installed, this is the first install attempt, not a retry.
                            Button(installFailure == nil || installer.mode == .repair ? NativeInstallFlowCopy.installButton : NativeInstallFlowCopy.retryButton) {
                                wizardLoadError = nil
                                installer.start()
                            }
                            // A previous installer child may still be exiting.
                            .disabled(busy || !installer.canStart)
                            .accessibilityHint("Downloads the pinned Ciaobot installer, verifies it, and installs Ciaobot on this Mac.")
                        }
                    }
                    if let installFailure {
                        Text(NativeInstallCopy.message(for: installFailure, mode: installer.mode))
                            .foregroundStyle(.red)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let installFailure, NativeInstallPortRecovery.isRecoverable(installFailure) {
                        HStack(spacing: 8) {
                            TextField("Port", text: $portText)
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 80)
                                .disabled(busy || discovery.isChecking)
                            Button(NativeInstallFlowCopy.portButton) { applyPort() }
                                .disabled(NativeInstallPortRecovery.port(portText) == nil || busy || discovery.isChecking)
                                .accessibilityHint(NativeInstallFlowCopy.portHint)
                        }
                        if let portError {
                            Text(portError).font(.caption)
                                .foregroundStyle(.red)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    if let wizardLoadError {
                        Text(wizardLoadError).font(.caption).foregroundStyle(.secondary)
                    }
                    TextField("Server address", text: $address).textFieldStyle(.roundedBorder)
                        .onSubmit(connect).disabled(busy)
                    Toggle("Keep me signed in on this Mac", isOn: Binding(
                        get: { keepSignedIn },
                        set: { keepSignedInChoice = $0 }
                    ))
                        .disabled(!persistentOptionAvailable || busy)
                    Text(persistenceExplanation)
                        .font(.caption).foregroundStyle(.secondary)
                    if let error { Text(error).foregroundStyle(.red) }
                    if let candidate = addressOrigin, signIn.hasSavedSignIn(for: candidate) {
                        Button(forgetting ? "Forgetting…" : "Forget saved sign-in") { forget() }
                            .disabled(busy)
                    }
                    if let forgetMessage {
                        Text(forgetMessage).font(.caption).foregroundStyle(.secondary)
                    }
                    HStack {
                        Button(checking ? "Checking…" : "Connect", action: connect)
                            .keyboardShortcut(.defaultAction).disabled(busy)
                        if checking { ProgressView().controlSize(.small) }
                    }
                    Text("The connection check confirms the server’s response format, not its identity. Sign in only if you trust the address.")
                        .font(.caption).foregroundStyle(.secondary)
                }.padding(32).frame(maxWidth: 520)
                    .onAppear { startAutomaticDiscovery() }
            }
        }
        .frame(minWidth: 480, minHeight: 320)
        .onAppear {
            // A fresh connection screen is disconnected. Do not clear a live
            // connection when SwiftUI merely re-shows the existing window.
            if origin == nil { session?.reportDisconnected() }
            // An install may have finished while the window was closed.
            if origin == nil, autoConnectTarget == nil { handleInstallState(installer.state) }
            guard let target = autoConnectTarget, !didAutoConnect else { return }
            didAutoConnect = true
            address = target.url.absoluteString
            connect(to: target)
        }
        .onReceive(NotificationCenter.default.publisher(for: .ciaobotChangeServer)) { _ in
            guard origin != nil else { return }
            address = origin?.url.absoluteString ?? address
            discovery.invalidate()
            origin = nil
            storage = .ephemeral
            dataStore = nil
            storageNotice = nil
            // Consent belongs to one connection. A different server must be
            // opted in again (or use the default) rather than inheriting the
            // previous choice.
            keepSignedInChoice = nil
            error = nil
            forgetMessage = nil
            session?.reportDisconnected()
            startAutomaticDiscovery()
        }
        // Editing the address to another server is a different connection
        // too. Clear the click so the new address starts from the default
        // instead of inheriting consent given for an earlier host.
        .onChange(of: addressOrigin?.canonicalKey) { _ in
            keepSignedInChoice = nil
        }
        .onChange(of: installer.state) { handleInstallState($0) }
        .onChange(of: engineUpdater.state) { handleEngineUpdateState($0) }
        .onChange(of: discovery.status) { handleDiscoveryStatus($0) }
    }

    @ViewBuilder
    private func wizardView(_ wizard: Origin) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Text(NativeInstallCopy.progress(for: installer.state) ?? "")
                    .font(.caption).foregroundStyle(.secondary)
                    .accessibilityLabel("Installation status")
                Spacer(minLength: 8)
                Button(NativeInstallFlowCopy.cancelButton) { installer.cancel() }
                    .accessibilityHint("Stops the installer. A Ciaobot service that already started keeps running.")
            }
            .padding(.horizontal, 12).padding(.vertical, 6)
            // Setup only: an ephemeral store of its own, never remembered,
            // never reported as a connection, torn down when setup ends.
            WebWindow(
                origin: wizard,
                storage: .ephemeral,
                onFailure: { message in
                    self.wizardLoadError = message
                    self.installer.cancel()
                }
            )
            .id(wizard.canonicalKey)
        }
    }

    /// Automatic same-Mac discovery starts only on the connection screen and
    /// only in production. Debug builds and test launches leave it to an
    /// explicit click so the test suite never contacts the real candidate.
    private func startAutomaticDiscovery() {
        guard autoDiscovery, origin == nil else { return }
        discovery.refresh()
    }

    private var addressOrigin: Origin? { Origin(address) }

    /// Persistence starts on where it can work. `canPersistSignIn` includes a
    /// usable registry, so an HTTPS address on a Mac with a broken mapping
    /// file stays off and the toggle stays disabled. An explicit click is
    /// stored separately and wins for this address only.
    private var keepSignedIn: Bool {
        PersistentSignInDefault.isEnabled(
            origin: addressOrigin,
            persistentAvailable: signIn.canPersistSignIn,
            explicitChoice: keepSignedInChoice
        )
    }

    private var persistentOptionAvailable: Bool {
        guard let candidate = addressOrigin else { return false }
        return candidate.usesTLS && signIn.canPersistSignIn
    }

    private var persistenceExplanation: String {
        PersistentSignInCopy.explanation(
            origin: addressOrigin,
            persistentAvailable: signIn.persistentAvailable,
            registryAvailable: signIn.registry.isAvailable
        )
    }

    private func forget() {
        guard let candidate = addressOrigin, tasks.beginForget() else { return }
        forgetMessage = nil
        // WebKit refuses to remove a store that a view still uses. The tracker
        // only reports release when the WKWebView weak reference is actually
        // nil; if that never happens, forget is not attempted and nothing is
        // claimed to have been released.
        viewTracker.whenReleased(timeout: 2) { released in
            guard released else {
                self.tasks.endForget()
                self.forgetMessage = "The session view has not finished closing. Try Forget again in a moment."
                return
            }
            self.signIn.forget(origin: candidate, releaseWebViews: { $0() }) { result in
                self.tasks.endForget()
                switch result {
                case .success:
                    self.storage = .ephemeral
                    self.forgetMessage = "Saved sign-in removed for \(candidate.canonicalKey)."
                case .failure(.registryUnavailable):
                    self.forgetMessage = "Could not use the saved sign-in list. Persistent sign-in is disabled."
                case .failure(.registryChangedElsewhere):
                    self.forgetMessage = "Another copy of the app changed the saved sign-in list. Restart Ciaobot to reload it before forgetting."
                case .failure(.storeStillPresent):
                    self.forgetMessage = "Could not remove the saved sign-in. Nothing was forgotten; try again."
                case .failure(.mappingNotSaved):
                    self.forgetMessage = "WebKit reported the saved sign-in store removed, but its record could not be updated. This session stays in memory; try again or restart Ciaobot to finish."
                }
            }
        }
    }

    /// Only `service_ready` is success. The service origin goes through the
    /// normal probe, so it is remembered and reported only if it answers.
    func handleInstallState(_ state: NativeInstallState) {
        if case .succeeded(let service) = state, origin == nil {
            address = service.url.absoluteString
            connect(to: service, afterInstall: true)
            return
        }
        // A port failure has a recovery that is not another identical retry:
        // offer a port field, prefilled with a port that is free right now.
        if case .failed(let failure) = state, NativeInstallPortRecovery.isRecoverable(failure) {
            portError = nil
            portText = FreePort.pick().map(String.init) ?? ""
        }
    }

    /// Writes the confirmed port to the workspace `.env`, clears the failed
    /// run, then re-detects and starts the existing service.
    private func applyPort() {
        guard let port = NativeInstallPortRecovery.port(portText) else { return }
        guard let workspace = LocalInstallation.detect()?.workspace else {
            portError = NativeInstallFlowCopy.portWorkspaceMissing
            return
        }
        do {
            try WorkspaceDotenvPort.setPort(port, in: workspace.appendingPathComponent(".env"))
        } catch {
            portError = NativeInstallFlowCopy.portSaveFailed
            return
        }
        portError = nil
        installer.reset()
        pendingPortRestart = true
        discovery.refresh()
    }

    /// A port change restarts the service once discovery has re-read the new
    /// port. Separately, a failed install/repair message is stale as soon as
    /// discovery finds the engine running.
    func handleDiscoveryStatus(_ status: LocalServerDiscovery.Status) {
        if pendingPortRestart {
            switch status {
            case .installedNotRunning:
                pendingPortRestart = false
                discovery.start()
            case .running:
                pendingPortRestart = false
            case .idle, .checking, .starting, .startFailed, .notDetected:
                break
            }
        }
        if NativeInstallFailureReset.shouldReset(install: installer.state, discovery: status) {
            installer.reset()
        }
    }

    /// After a successful update the version line is re-read from the restarted engine.
    func handleEngineUpdateState(_ state: EngineUpdateState) {
        guard state.requiresRediscovery, origin == nil else { return }
        discovery.refresh()
    }

    /// Internal so the connection outcome — and therefore the remembered
    /// origin — can be tested without rendering the view.
    func connect() {
        guard !tasks.isBusy else { return }
        guard let candidate = Origin(address) else {
            error = "Enter only an origin, such as https://example.com:8443. Do not include a path, query, fragment, or credentials. HTTP is allowed only for localhost, 127.0.0.1, or ::1; other hosts require HTTPS."
            return
        }
        connect(to: candidate)
    }

    private func connect(to candidate: Origin, afterInstall: Bool = false) {
        guard tasks.beginCheck() else { return }
        error = nil
        wizardLoadError = nil
        storageNotice = nil
        probe.check(candidate) { ok in
            self.tasks.endCheck()
            if ok {
                // Only a connection that actually answered is remembered. A
                // typo, an unreachable host, or a hostile address never
                // becomes the startup target or a saved host.
                self.lastServer.remember(candidate)
                if !self.isTestLaunch {
                    self.hostStore.add(candidate)
                }
                // The connection screen is about to disappear. A discovery
                // response that is still in flight must not update the screen
                // that replaces it.
                self.discovery.invalidate()
                // Resolve the store only after the probe succeeds, so a typo
                // cannot create a persistent mapping or empty store. If the
                // user opted in but cannot get a persistent store, say so
                // instead of silently using memory only.
                self.storage = self.signIn.storage(for: candidate, keepSignedIn: self.keepSignedIn)
                self.storageNotice = self.signIn.fallback(for: candidate, keepSignedIn: self.keepSignedIn)?.userMessage
                // Create the one store this connection uses. The web view and
                // the menu-bar chat feed share it; no second nonpersistent
                // store and never the default shared store.
                let store = WebKitSessionStores.make(self.storage)
                self.dataStore = store
                self.origin = candidate
                // The probe verified the protocol shape, not an identity or a
                // signed-in session. The menu bar says only this much.
                self.session?.reportConnected(candidate, dataStore: store)
            } else {
                self.error = afterInstall
                    ? (self.installer.mode == .repair
                        ? NativeInstallFlowCopy.connectFailedAfterRepair(candidate)
                        : NativeInstallFlowCopy.connectFailedAfterInstall(candidate))
                    : "Could not verify the server’s startup response. Check the address and try again."
                self.session?.reportDisconnected()
            }
        }
    }
}
