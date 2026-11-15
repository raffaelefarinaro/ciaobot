import AppKit
import Security
import ServiceManagement

/// Which explicit action failed. A failed stop must be retried as a stop, not
/// silently turned into an enable on the next click.
enum LoginItemOperation: Equatable {
    case register
    case unregister
}

/// The truthful state of the macOS login item. `requiresApproval` is not
/// success: the app has been registered but macOS has not started it at login
/// yet. `unavailable` and `failed` carry the exact reason shown to the user.
enum LoginItemState: Equatable {
    case notRegistered
    case enabled
    case requiresApproval
    case unavailable(LoginItemUnavailability)
    case failed(operation: LoginItemOperation, reason: String)
}

/// Why macOS cannot use this copy as a login item. The causes are not
/// interchangeable: each has exactly one next step, and only one of them is
/// settled in the Login Items pane.
enum LoginItemUnavailableCause: Equatable {
    /// A bare `swift build` executable has no app bundle to register.
    case bareExecutable
    /// The bundle is not installed in `/Applications`, or it runs translocated
    /// from a download, so `SMAppService.mainApp` has no installed app.
    case notInApplications
    /// The running code has no signing team identifier: an ad-hoc or unsigned
    /// build, which macOS will not accept as a login item.
    case unsignedOrAdHoc
}

/// A diagnosed cause plus the exact explanation shown to the user. A `nil`
/// cause means the app cannot diagnose the state, which keeps the generic
/// disabled row and its raw reason.
struct LoginItemUnavailability: Equatable {
    let cause: LoginItemUnavailableCause?
    let reason: String

    init(cause: LoginItemUnavailableCause? = nil, reason: String) {
        self.cause = cause
        self.reason = reason
    }
}

/// The exact text for each diagnosed cause. None of them is settled in System
/// Settings: moving a bundle into Applications and replacing an ad-hoc build
/// are done in Finder.
enum LoginItemUnavailableCopy {
    static func title(for cause: LoginItemUnavailableCause) -> String {
        switch cause {
        case .bareExecutable:
            return "Start at Login Unavailable"
        case .notInApplications:
            return "Start at Login — Move Ciaobot to Applications…"
        case .unsignedOrAdHoc:
            return "Start at Login — Needs the Signed Release…"
        }
    }

    static func explanation(for cause: LoginItemUnavailableCause) -> String {
        switch cause {
        case .bareExecutable:
            return SystemLoginItem.bareExecutableReason
        case .notInApplications:
            return "macOS only offers Start at Login for an app installed in Applications. Move Ciaobot.app into Applications and open it from there, or install the notarized release."
        case .unsignedOrAdHoc:
            return "This build is not signed with a Developer ID team, so macOS will not accept it as a login item. Install the signed, notarized Ciaobot release to use Start at Login."
        }
    }
}

/// The login-item surface behind the tray row. Tests inject a double so no
/// test ever registers the operator's Mac as a login item. Registration is
/// only ever reached from an explicit menu click.
@MainActor
protocol LoginItemControlling: AnyObject {
    var state: LoginItemState { get }
    func refresh()
    func register() throws
    func unregister() throws
}

/// `SMAppService.mainApp` plus the installed-and-signed guard. A bare
/// `swift build` executable, a copy outside Applications, and an ad-hoc build
/// are not login-item-capable; each reports its own fixed cause without asking
/// ServiceManagement or registering anything. Nothing here registers at launch.
@MainActor
final class SystemLoginItem: LoginItemControlling {
    /// The fixed long-standing message stays addressable from tests and the
    /// state machine, so it is not tied to the main actor.
    nonisolated static let bareExecutableReason =
        "Start at Login requires the bundled Ciaobot.app. A bare debug executable cannot register itself as a login item."
    /// `SMAppService.mainApp` resolves only for an app installed here. A copy
    /// running translocated from a download is not installed either.
    static let applicationsDirectory = "/Applications"

    /// The installed/signed facts do not change while this process runs, so
    /// they are read once. Only the macOS status is re-read on `refresh()`.
    private let bundleCause: LoginItemUnavailableCause?
    private let service: LoginItemServicing
    private(set) var state: LoginItemState

    init(
        bundleIdentifier: String? = Bundle.main.bundleIdentifier,
        bundleURLPath: String = Bundle.main.bundleURL.path,
        // Tests default to no signing team, which is the conservative answer:
        // nothing may claim a Developer ID signature it did not read.
        signingTeam: SigningTeamReading? = nil,
        service: LoginItemServicing? = nil
    ) {
        self.bundleCause = Self.diagnoseCause(
            bundleIdentifier: bundleIdentifier,
            bundleURLPath: bundleURLPath,
            teamIdentifier: signingTeam?.teamIdentifier
        )
        self.service = service ?? SystemLoginItemService()
        self.state = .notRegistered
        refresh()
    }

    /// The causes the app can read from its own bundle and code signature.
    /// Pure: no ServiceManagement call, no code-signing read of its own.
    static func diagnoseCause(
        bundleIdentifier: String?,
        bundleURLPath: String,
        teamIdentifier: String?
    ) -> LoginItemUnavailableCause? {
        guard let bundleIdentifier, !bundleIdentifier.isEmpty,
              (bundleURLPath as NSString).pathExtension == "app" else {
            return .bareExecutable
        }
        guard isInstalledInApplications(bundleURLPath) else { return .notInApplications }
        guard let teamIdentifier, !teamIdentifier.isEmpty else { return .unsignedOrAdHoc }
        return nil
    }

    static func isInstalledInApplications(_ bundleURLPath: String) -> Bool {
        let standardized = URL(fileURLWithPath: bundleURLPath).standardizedFileURL.path
        return standardized.hasPrefix(applicationsDirectory + "/")
    }

    func refresh() {
        if let bundleCause {
            state = .unavailable(
                LoginItemUnavailability(
                    cause: bundleCause,
                    reason: LoginItemUnavailableCopy.explanation(for: bundleCause)
                )
            )
            return
        }
        state = Self.state(for: service.status)
    }

    func register() throws {
        if let bundleCause {
            throw LoginItemError.unavailable(LoginItemUnavailableCopy.explanation(for: bundleCause))
        }
        do {
            try service.register()
        } catch {
            refresh()
            // Keep a real status such as `.requiresApproval` or `.enabled`.
            // Only an unchanged not-registered state becomes a failed action.
            if state == .notRegistered {
                state = .failed(operation: .register, reason: Self.message(for: error))
            }
            throw error
        }
        refresh()
    }

    func unregister() throws {
        if let bundleCause {
            throw LoginItemError.unavailable(LoginItemUnavailableCopy.explanation(for: bundleCause))
        }
        do {
            try service.unregister()
        } catch {
            refresh()
            // Preserve the OS result. If the app is still enabled, the failed
            // action was a stop and must stay a stop for retry. If macOS now
            // reports notRegistered, the stop actually took effect.
            if state == .enabled {
                state = .failed(operation: .unregister, reason: Self.message(for: error))
            }
            throw error
        }
        refresh()
    }

    /// Pure mapping, so tests can prove every macOS status without asking the
    /// operating system to change this Mac's login items.
    static func state(for status: SMAppService.Status) -> LoginItemState {
        switch status {
        case .enabled:
            return .enabled
        case .requiresApproval:
            return .requiresApproval
        case .notRegistered, .notFound:
            // `mainApp` reports `.notFound` for an installed, signed app that
            // has simply never been registered, so it is the same offer as
            // `.notRegistered`. Only an actual `register()` call can tell
            // whether macOS accepts this copy; a refusal becomes `.failed`.
            return .notRegistered
        @unknown default:
            // An unrecognised future status is not one of the diagnosed
            // causes: keep the generic disabled row with the raw reason.
            return .unavailable(
                LoginItemUnavailability(reason: "macOS reported an unknown login-item status.")
            )
        }
    }

    static func message(for error: Error) -> String {
        "macOS could not change the login item: \(error.localizedDescription)"
    }
}

/// Reads the running code's signing team identifier. An ad-hoc or unsigned
/// build has none, and macOS does not accept it as a login item. Injected so no
/// test ever inspects real code signing.
protocol SigningTeamReading {
    /// `nil` when the running code carries no signing team.
    var teamIdentifier: String? { get }
}

/// The real probe: this process's own code object and its signing information.
struct CodeSigningTeam: SigningTeamReading {
    var teamIdentifier: String? { Self.runningTeamIdentifier() }

    static func runningTeamIdentifier() -> String? {
        var code: SecCode?
        guard SecCodeCopySelf([], &code) == errSecSuccess, let code else { return nil }
        var staticCode: SecStaticCode?
        guard SecCodeCopyStaticCode(code, [], &staticCode) == errSecSuccess,
              let staticCode else { return nil }
        var information: CFDictionary?
        guard SecCodeCopySigningInformation(
            staticCode,
            SecCSFlags(rawValue: kSecCSSigningInformation),
            &information
        ) == errSecSuccess,
              let signing = information as? [String: Any] else { return nil }
        guard let team = signing[kSecCodeInfoTeamIdentifier as String] as? String,
              !team.isEmpty else { return nil }
        return team
    }
}

/// The `SMAppService.mainApp` surface behind the login-item state machine.
/// Injected so a test can prove that refreshing and the diagnostic row never
/// register anything on the operator's Mac.
@MainActor
protocol LoginItemServicing: AnyObject {
    var status: SMAppService.Status { get }
    func register() throws
    func unregister() throws
}

@MainActor
final class SystemLoginItemService: LoginItemServicing {
    var status: SMAppService.Status { SMAppService.mainApp.status }

    func register() throws {
        try SMAppService.mainApp.register()
    }

    func unregister() throws {
        try SMAppService.mainApp.unregister()
    }
}

enum LoginItemError: LocalizedError {
    case unavailable(String)

    var errorDescription: String? {
        switch self {
        case .unavailable(let reason):
            return reason
        }
    }
}

/// The persisted Dock preference. The menu-bar item is independent of the
/// activation policy, so hiding the Dock icon leaves the tray icon in place.
@MainActor
protocol DockIconControlling: AnyObject {
    var hidesDockIcon: Bool { get }
    func setHidesDockIcon(_ hidden: Bool)
    /// Apply the saved choice at launch. It never changes the saved value.
    func applyPersistedPreference()
    /// Open needs a regular app so its window can become key. The tray item is
    /// not part of the Dock and stays visible.
    func prepareForWindowPresentation()
    /// Return to the saved policy after the main window closes.
    func windowDidClose()
}

protocol DockIconPreferenceStoring: AnyObject {
    var hidesDockIcon: Bool { get set }
}

final class UserDefaultsDockIconPreferenceStore: DockIconPreferenceStoring {
    static let defaultKey = "CiaobotHideDockIcon"

    private let defaults: UserDefaults
    private let key: String

    init(
        defaults: UserDefaults = .standard,
        key: String = UserDefaultsDockIconPreferenceStore.defaultKey
    ) {
        self.defaults = defaults
        self.key = key
    }

    var hidesDockIcon: Bool {
        get { defaults.bool(forKey: key) }
        set { defaults.set(newValue, forKey: key) }
    }
}

@MainActor
protocol ActivationPolicyApplying: AnyObject {
    var currentPolicy: NSApplication.ActivationPolicy { get }
    func setPolicy(_ policy: NSApplication.ActivationPolicy)
}

@MainActor
final class ApplicationActivationPolicy: ActivationPolicyApplying {
    var currentPolicy: NSApplication.ActivationPolicy { NSApp.activationPolicy() }

    func setPolicy(_ policy: NSApplication.ActivationPolicy) {
        guard NSApp.activationPolicy() != policy else { return }
        NSApp.setActivationPolicy(policy)
    }
}

@MainActor
final class DockIconController: DockIconControlling {
    private let preferences: DockIconPreferenceStoring
    private let policy: ActivationPolicyApplying
    private let mainWindowIsOpen: () -> Bool

    init(
        preferences: DockIconPreferenceStoring = UserDefaultsDockIconPreferenceStore(),
        policy: ActivationPolicyApplying? = nil,
        mainWindowIsOpen: (@MainActor () -> Bool)? = nil
    ) {
        self.preferences = preferences
        self.policy = policy ?? ApplicationActivationPolicy()
        self.mainWindowIsOpen = mainWindowIsOpen ?? Self.systemMainWindowIsOpen
    }

    var hidesDockIcon: Bool { preferences.hidesDockIcon }

    func setHidesDockIcon(_ hidden: Bool) {
        preferences.hidesDockIcon = hidden
        guard hidden else {
            // Turning the preference off must show the Dock icon now, even if
            // the window is closed.
            policy.setPolicy(.regular)
            return
        }
        applyPersistedPreference()
    }

    func applyPersistedPreference() {
        guard hidesDockIcon else { return }
        // The saved choice only hides the Dock while the main window is
        // closed. An open window stays a regular app so it can be key.
        policy.setPolicy(mainWindowIsOpen() ? .regular : .accessory)
    }

    func prepareForWindowPresentation() {
        policy.setPolicy(.regular)
    }

    func windowDidClose() {
        guard hidesDockIcon else { return }
        // `willClose` still reports the window as visible, so do not consult
        // `mainWindowIsOpen` here: the window is closing right now.
        policy.setPolicy(.accessory)
    }

    private static func systemMainWindowIsOpen() -> Bool {
        NSApp.windows.contains {
            $0.identifier == MainWindowPresenter.mainWindowIdentifier && $0.isVisible
        }
    }
}

/// Fixed, app-owned destinations for macOS Settings. A server, page, or chat
/// never supplies these URLs. The notification link is used only when macOS
/// itself is blocking notifications; it is never offered for a missing sign-in
/// or a missing server connection.
@MainActor
protocol SystemSettingsOpening: AnyObject {
    func openNotificationSettings()
    func openLoginItemsSettings()
}

@MainActor
final class WorkspaceSystemSettings: SystemSettingsOpening {
    static let notificationSettingsURL = URL(
        string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension"
    )!
    static let loginItemsSettingsURL = URL(
        string: "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"
    )!

    private let open: (URL) -> Void
    private let presentHelper: (SettingsHelperPane) -> Void

    /// `presentHelper` shows the floating Ciaobot tile beside the pane. Tests
    /// leave it out, so no test ever opens a panel.
    init(
        open: @escaping (URL) -> Void = { NSWorkspace.shared.open($0) },
        presentHelper: @escaping (SettingsHelperPane) -> Void = { _ in }
    ) {
        self.open = open
        self.presentHelper = presentHelper
    }

    func openNotificationSettings() {
        open(Self.notificationSettingsURL)
        presentHelper(.notifications)
    }

    func openLoginItemsSettings() {
        open(Self.loginItemsSettingsURL)
        presentHelper(.loginItems)
    }
}

/// The one action behind the tray's "Change Connected Host" item. It presents
/// the client window and, only after that presentation is confirmed, asks its
/// existing connection screen to take over; it never contacts, restarts, or
/// reconfigures the separately installed server. A failed presentation is
/// reported by the presenter and this action does nothing else, so the change
/// request cannot be silently dropped. The connection screen clears the
/// previous sign-in consent itself, so a new host still requires a fresh,
/// explicit opt-in.
@MainActor
enum ConnectionMenuActions {
    static func changeConnectedHost(present: @escaping () -> Bool) -> () -> Void {
        return {
            guard present() else { return }
            NotificationCenter.default.post(name: .ciaobotChangeServer, object: nil)
        }
    }
}
