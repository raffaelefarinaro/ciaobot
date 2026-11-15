import Foundation
import Darwin

enum NavigationDecision { case inWindow, external, blocked }
enum DownloadDecision { case inApp, external, blocked }

struct Origin {
    // HTTP is permitted only for these explicit loopback names. A host that
    // merely resolves to a loopback address is not enough: the origin check
    // compares the literal host, so a DNS name cannot quietly become local.
    static let loopbackHosts: Set<String> = ["localhost", "127.0.0.1", "::1"]

    let url: URL
    private let scheme: String
    private let host: String
    private let port: Int

    var isLoopback: Bool { Self.loopbackHosts.contains(host) }

    /// Cookies ignore ports, so a persistent sign-in store is keyed by the
    /// full canonical origin: scheme, literal host, and effective port.
    var canonicalKey: String { "\(scheme)://\(Self.bracketedHost(host)):\(port)" }

    /// Persistent sign-in is HTTPS-only. HTTP stays on an ephemeral store even
    /// for loopback, so a local session is never written to disk.
    var usesTLS: Bool { scheme == "https" }

    private static func bracketedHost(_ host: String) -> String {
        host.contains(":") ? "[\(host)]" : host
    }

    init?(_ input: String) {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let components = URLComponents(string: text),
              let scheme = components.scheme?.lowercased(), ["http", "https"].contains(scheme),
              let host = components.host?.lowercased(), !host.isEmpty,
              components.user == nil, components.password == nil,
              let normalizedPath = Self.canonicalPath(components.percentEncodedPath),
              components.query == nil, components.fragment == nil,
              !text.hasSuffix(":"),
              components.port.map({ (1...65535).contains($0) }) ?? true else { return nil }
        let plainHost = Self.unbracketedHost(host)
        guard Self.validHost(plainHost), scheme == "https" || Self.loopbackHosts.contains(plainHost) else { return nil }
        // Store the canonical spelling so two inputs that denote the same
        // origin also produce the same URL (and not "/" versus "").
        var canonical = components
        canonical.percentEncodedPath = normalizedPath
        guard let url = canonical.url else { return nil }
        self.url = url
        self.scheme = scheme
        self.host = plainHost
        self.port = components.port ?? (scheme == "https" ? 443 : 80)
    }

    /// An origin has no path. The empty path and one ordinary trailing root
    /// slash (`https://example.com/`) are the same spelling of that origin;
    /// every other path, including `//` and `/a`, is not an origin.
    private static func canonicalPath(_ path: String) -> String? {
        if path.isEmpty || path == "/" { return "" }
        return nil
    }

    private static func unbracketedHost(_ host: String) -> String {
        guard host.hasPrefix("["), host.hasSuffix("]") else { return host }
        return String(host.dropFirst().dropLast())
    }

    private static func validHost(_ host: String) -> Bool {
        var ipv6 = in6_addr()
        if host.contains(":") { return host.withCString { inet_pton(AF_INET6, $0, &ipv6) == 1 } }
        if host.allSatisfy({ $0.isASCII && ($0.isNumber || $0 == ".") }) {
            var ipv4 = in_addr()
            return host.withCString { inet_pton(AF_INET, $0, &ipv4) == 1 }
        }
        guard host.utf8.count <= 253 else { return false }
        return host.split(separator: ".", omittingEmptySubsequences: false).allSatisfy { label in
            guard (1...63).contains(label.utf8.count), label.first != "-", label.last != "-" else { return false }
            return label.utf8.allSatisfy { (48...57).contains($0) || (97...122).contains($0) || $0 == 45 }
        }
    }

    func navigation(_ url: URL, userInitiated: Bool) -> NavigationDecision {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let targetScheme = parts.scheme?.lowercased(), parts.user == nil, parts.password == nil else { return .blocked }
        if targetScheme == "mailto" { return userInitiated ? .external : .blocked }
        guard ["http", "https"].contains(targetScheme), let targetHost = parts.host?.lowercased(), !targetHost.isEmpty,
              parts.port.map({ (1...65535).contains($0) }) ?? true else { return .blocked }
        let targetPort = parts.port ?? (targetScheme == "https" ? 443 : 80)
        if targetScheme == scheme && Self.unbracketedHost(targetHost) == host && targetPort == port { return .inWindow }
        // Everything past this point is another origin, so it can never load
        // in the window. An HTTP link from an HTTPS session is not a downgrade
        // of the app: it opens in the system browser like any other link.
        return userInitiated ? .external : .blocked
    }

    /// Compares a WebKit security origin, whose port is 0 when the URL used
    /// the scheme's default port. This is the frame-origin check for file
    /// panels and other frame-scoped UI.
    func matches(scheme: String, host: String, port: Int) -> Bool {
        let normalizedScheme = scheme.lowercased()
        let normalizedHost = Self.unbracketedHost(host.lowercased())
        let effectivePort = port == 0 ? (normalizedScheme == "https" ? 443 : 80) : port
        return normalizedScheme == self.scheme && normalizedHost == self.host && effectivePort == self.port
    }

    /// A download may run inside the app only for content it generated
    /// (`data:` and same-origin `blob:`) or for the exact selected origin.
    /// Everything else stays in the system browser or is refused.
    func download(_ url: URL) -> DownloadDecision {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let targetScheme = parts.scheme?.lowercased() else { return .blocked }
        if targetScheme == "data" { return .inApp }
        if targetScheme == "blob" {
            let text = url.absoluteString
            guard text.hasPrefix("blob:"), let inner = URL(string: String(text.dropFirst("blob:".count))) else {
                return .blocked
            }
            return navigation(inner, userInitiated: false) == .inWindow ? .inApp : .blocked
        }
        switch navigation(url, userInitiated: true) {
        case .inWindow: return .inApp
        case .external: return .external
        case .blocked: return .blocked
        }
    }
}
