import Foundation
import WebKit
import Security
import CryptoKit

/// The private identity emitted by the app-owned engine's inherited pipe.
/// The certificate pin is never read from ready.json.
struct LocalEngineTLSIdentity: Equatable {
    let pid: Int32
    let startupID: String
    let port: Int
    let sha256: String

    var origin: Origin? { Origin("https://127.0.0.1:\(port)") }

    init?(pid: Int32, startupID: String, port: Int, sha256: String) {
        guard pid > 1, !startupID.isEmpty, (1...65535).contains(port),
              sha256.count == 64, sha256.utf8.allSatisfy({ ($0 >= 48 && $0 <= 57) || ($0 >= 97 && $0 <= 102) }) else { return nil }
        self.pid = pid
        self.startupID = startupID
        self.port = port
        self.sha256 = sha256
    }

    func accepts(_ trust: SecTrust?) -> Bool {
        Self.certificateMatches(trust, pin: sha256)
    }

    func accepts(_ challenge: URLAuthenticationChallenge) -> Bool {
        Self.accepts(trust: challenge.protectionSpace.serverTrust,
                     authenticationMethod: challenge.protectionSpace.authenticationMethod,
                     host: challenge.protectionSpace.host, port: challenge.protectionSpace.port,
                     expectedPort: port, pin: sha256)
    }

    static func accepts(trust: SecTrust?, authenticationMethod: String, host: String, port: Int,
                        expectedPort: Int, pin: String) -> Bool {
        authenticationMethod == NSURLAuthenticationMethodServerTrust
            && host == "127.0.0.1"
            && port == expectedPort
            && certificateMatches(trust, pin: pin)
    }

    /// The local listener is self-signed and must match its inherited pin.
    /// Other hosts referenced by the page keep WebKit's normal trust policy.
    static func usesPlatformTrust(authenticationMethod: String, host: String) -> Bool {
        authenticationMethod == NSURLAuthenticationMethodServerTrust && host != "127.0.0.1"
    }

    static func isCurrentWebView(expectedIdentity: LocalEngineTLSIdentity?, currentIdentity: LocalEngineTLSIdentity?,
                                 expectedOrigin: Origin, currentOrigin: Origin?) -> Bool {
        guard let expectedIdentity else { return false }
        return expectedIdentity == currentIdentity
            && expectedIdentity.origin == expectedOrigin
            && currentOrigin == expectedOrigin
    }

    static func acceptsConnectionCallback(expectedMode: EngineMode, currentMode: EngineMode,
                                          expectedOrigin: Origin, currentOrigin: Origin?,
                                          expectedIdentity: LocalEngineTLSIdentity?,
                                          currentIdentity: LocalEngineTLSIdentity?) -> Bool {
        guard expectedMode == currentMode, currentOrigin == expectedOrigin else { return false }
        if expectedMode == .local {
            return isCurrentWebView(expectedIdentity: expectedIdentity, currentIdentity: currentIdentity,
                                    expectedOrigin: expectedOrigin, currentOrigin: currentOrigin)
        }
        return true
    }

    static func certificateMatches(_ trust: SecTrust?, pin: String) -> Bool {
        guard let trust,
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
              let certificate = chain.first else { return false }
        let digest = SHA256.hash(data: SecCertificateCopyData(certificate) as Data)
        return digest.map { String(format: "%02x", $0) }.joined() == pin
    }
}

/// Cookie host scoping ignores ports, including for loopback. The first rule
/// blocks cookies on every network request; the second restores cookies only
/// for this exact pinned HTTPS origin.
@MainActor
enum LocalEngineWebSecurity {
    private static var compilingPorts = Set<Int>()
    private static var queuedCompiles: [Int: [( (@escaping () -> Void) -> Void )]] = [:]

    static func compileRules(for identity: LocalEngineTLSIdentity,
                             completion: @escaping (WKContentRuleList?) -> Void) {
        guard let origin = identity.origin else { completion(nil); return }
        let secureOrigin = "^https://127\\.0\\.0\\.1:\(origin.portNumber)/"
        let socketOrigin = "^wss://127\\.0\\.0\\.1:\(origin.portNumber)/"
        let rules = [
            ["trigger": ["url-filter": ".*"], "action": ["type": "block-cookies"]],
            ["trigger": ["url-filter": secureOrigin], "action": ["type": "ignore-previous-rules"]],
            ["trigger": ["url-filter": socketOrigin], "action": ["type": "ignore-previous-rules"]],
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: rules),
              let json = String(data: data, encoding: .utf8) else { completion(nil); return }
        guard let store = WKContentRuleListStore.default() else { completion(nil); return }
        let identifier = "ciaobot-local-engine-\(identity.port)"
        enqueue(port: identity.port) { done in
            store.removeContentRuleList(forIdentifier: identifier) { _ in
                store.compileContentRuleList(forIdentifier: identifier, encodedContentRuleList: json) { list, error in
                    DispatchQueue.main.async {
                        completion(error == nil ? list : nil)
                        done()
                    }
                }
            }
        }
    }

    private static func enqueue(port: Int, operation: @escaping (@escaping () -> Void) -> Void) {
        queuedCompiles[port, default: []].append(operation)
        startNext(port: port)
    }

    private static func startNext(port: Int) {
        guard !compilingPorts.contains(port), var queue = queuedCompiles[port], !queue.isEmpty else { return }
        compilingPorts.insert(port)
        let operation = queue.removeFirst()
        queuedCompiles[port] = queue
        operation {
            compilingPorts.remove(port)
            startNext(port: port)
        }
    }
}
