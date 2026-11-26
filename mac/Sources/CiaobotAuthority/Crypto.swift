import CryptoKit
import Foundation

func sha256Hex(_ data: [UInt8]) -> String { Hex.encode(SHA256.hash(data: data)) }
func sha256Bytes(_ data: [UInt8]) -> [UInt8] { Array(SHA256.hash(data: data)) }

/// Operator verifier: an ECDSA P-256 public key (x9.63 point), raw r||s signatures.
/// Holds no signing ability; the operator key lives in the Mac app.
public struct OperatorVerifier {
    private let key: P256.Signing.PublicKey
    public let publicKeyX963: [UInt8]

    public init(publicKeyX963: [UInt8]) throws {
        self.key = try P256.Signing.PublicKey(x963Representation: Data(publicKeyX963))
        self.publicKeyX963 = publicKeyX963
    }

    /// SHA-256 (hex) of the x9.63 public key; signed into every command.
    public var keyID: String { sha256Hex(publicKeyX963) }

    public func verify(_ data: [UInt8], signature: [UInt8]) -> Bool {
        guard signature.count == 64,
            let sig = try? P256.Signing.ECDSASignature(rawRepresentation: Data(signature))
        else { return false }
        return key.isValidSignature(sig, for: Data(data))
    }
}

/// HMAC-SHA256 over an independent key; authenticates the persisted state only.
public struct StateMAC {
    private let key: SymmetricKey
    let keyBytes: [UInt8]

    public init(key: [UInt8]) throws {
        guard key.count >= 32 else { throw AuthorityError("bad_key") }
        self.key = SymmetricKey(data: key)
        self.keyBytes = key
    }

    public func tag(_ data: [UInt8]) -> [UInt8] {
        Array(HMAC<SHA256>.authenticationCode(for: Data(data), using: key))
    }

    public func verify(_ data: [UInt8], tag: [UInt8]) -> Bool {
        HMAC<SHA256>.isValidAuthenticationCode(Data(tag), authenticating: Data(data), using: key)
    }
}

/// Ed25519 event signer. Signing is deterministic (RFC 8032, see
/// `Ed25519Deterministic`) so signatures match the Python reference byte for
/// byte; verification and the public key use CryptoKit.
public struct EventSigner {
    private let seed: [UInt8]
    public let publicKey: [UInt8]
    private let verifyKey: Curve25519.Signing.PublicKey

    public init(seed: [UInt8]) throws {
        guard seed.count == 32 else { throw AuthorityError("bad_key") }
        let priv = try Curve25519.Signing.PrivateKey(rawRepresentation: Data(seed))
        self.seed = seed
        self.verifyKey = priv.publicKey
        self.publicKey = Array(priv.publicKey.rawRepresentation)
    }

    func usesSecret(_ secret: [UInt8]) -> Bool { seed == Array(secret.prefix(32)) || seed == secret }

    /// SHA-256 (hex) of the raw public key, published through `health`.
    public var authorityKeyID: String { sha256Hex(publicKey) }

    public func sign(_ data: [UInt8]) -> [UInt8] {
        Ed25519Deterministic.sign(seed: seed, publicKey: publicKey, message: data)
    }

    public func verify(_ data: [UInt8], signature: [UInt8]) -> Bool {
        verifyKey.isValidSignature(Data(signature), for: Data(data))
    }
}
