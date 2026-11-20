import Foundation

/// The last origin that actually answered. Exactly one origin is kept and the
/// value is re-validated by `Origin` on every read, so a path, query,
/// credential, or a value written by an older build can never become a launch
/// target. Nothing is remembered until a probe has succeeded.
protocol LastServerStoring: AnyObject {
    var savedOrigin: Origin? { get }
    func remember(_ origin: Origin)
    func clear()
}

final class UserDefaultsLastServerStore: LastServerStoring {
    static let defaultKey = "CiaobotLastOrigin"

    private let defaults: UserDefaults
    private let key: String

    init(
        defaults: UserDefaults = .standard,
        key: String = UserDefaultsLastServerStore.defaultKey
    ) {
        self.defaults = defaults
        self.key = key
    }

    var savedOrigin: Origin? {
        guard let raw = defaults.string(forKey: key) else { return nil }
        guard let origin = Origin(raw) else {
            // A value this parser rejects is not an origin. Discard it so it
            // cannot be retried on the next launch.
            defaults.removeObject(forKey: key)
            return nil
        }
        return origin
    }

    func remember(_ origin: Origin) {
        // `Origin` already rejected paths, queries, fragments, and
        // credentials, so the canonical spelling is an origin and nothing else.
        defaults.set(origin.url.absoluteString, forKey: key)
    }

    func clear() {
        defaults.removeObject(forKey: key)
    }
}

/// The launch decision, kept pure so a test can prove what a launch will do
/// without rendering the connection screen or contacting anything.
enum ConnectionLaunch {
    /// A saved origin always prefills the field, in every build. When there is
    /// none the shipped default candidate is the starting value, as before.
    static func initialAddress(saved: Origin?, defaultAddress: String) -> String {
        saved?.url.absoluteString ?? defaultAddress
    }

    /// Automatic connection is one attempt at most, and never during a test
    /// launch. A debug build without a test launch behaves like production so
    /// the feature is exercisable locally; the DEBUG `TestLaunch` controls
    /// always take precedence over this decision at the call site.
    static func autoConnectOrigin(saved: Origin?, isTestLaunch: Bool, isDebugBuild: Bool) -> Origin? {
        guard !isTestLaunch else { return nil }
        // The build kind is stated by the caller; both builds may reconnect to
        // a saved origin outside a test launch.
        _ = isDebugBuild
        return saved
    }
}
