import Foundation
import CryptoKit

/// BLAKE2b (RFC 7693). Minisign's prehashed `ED` algorithm signs this digest.
enum Blake2b {
    private static let iv: [UInt64] = [
        0x6a09e667f3bcc908, 0xbb67ae8584caa73b, 0x3c6ef372fe94f82b, 0xa54ff53a5f1d36f1,
        0x510e527fade682d1, 0x9b05688c2b3e6c1f, 0x1f83d9abfb41bd6b, 0x5be0cd19137e2179,
    ]

    /// The message schedule. BLAKE2b runs 12 rounds; round r uses row r % 10.
    private static let sigma: [[Int]] = [
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
        [14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3],
        [11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4],
        [7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8],
        [9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13],
        [2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9],
        [12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11],
        [13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10],
        [6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5],
        [10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0],
    ]

    /// BLAKE2b with a 64-byte digest, no key (RFC 7693).
    static func hash512(_ data: Data) -> Data {
        var h = iv
        h[0] ^= 0x0101_0000 ^ 64   // parameter block: no key, 64-byte digest, fanout 1, depth 1

        let bytes = [UInt8](data)
        // The last block is zero-padded and compressed with the final flag. Empty input is one zero block.
        let blockCount = max(1, (bytes.count + 127) / 128)
        var counter: UInt64 = 0      // low 64 bits of the 128-bit byte counter
        var counterHigh: UInt64 = 0  // high 64 bits; inputs this size never reach it, but the counter is 128-bit
        for index in 0..<blockCount {
            let start = index * 128
            let isLast = index == blockCount - 1
            var block = [UInt8](repeating: 0, count: 128)
            let available = isLast ? bytes.count - start : 128
            for offset in 0..<available {
                block[offset] = bytes[start + offset]
            }
            let (sum, overflow) = counter.addingReportingOverflow(UInt64(available))
            counter = sum
            if overflow { counterHigh &+= 1 }
            compress(&h, block: block, counter: counter, counterHigh: counterHigh, isLast: isLast)
        }

        var digest = Data(capacity: 64)
        for word in h {
            for shift in stride(from: 0, to: 64, by: 8) {
                digest.append(UInt8((word >> UInt64(shift)) & 0xFF))
            }
        }
        return digest
    }

    private static func compress(_ h: inout [UInt64], block: [UInt8], counter: UInt64, counterHigh: UInt64, isLast: Bool) {
        var m = [UInt64](repeating: 0, count: 16)
        for word in 0..<16 {
            var value: UInt64 = 0
            for byte in 0..<8 {
                value |= UInt64(block[word * 8 + byte]) << UInt64(8 * byte)
            }
            m[word] = value
        }

        var v = h + iv
        v[12] ^= counter
        v[13] ^= counterHigh
        if isLast { v[14] = ~v[14] }

        for round in 0..<12 {
            let s = sigma[round % 10]
            mix(&v, 0, 4, 8, 12, m[s[0]], m[s[1]])
            mix(&v, 1, 5, 9, 13, m[s[2]], m[s[3]])
            mix(&v, 2, 6, 10, 14, m[s[4]], m[s[5]])
            mix(&v, 3, 7, 11, 15, m[s[6]], m[s[7]])
            mix(&v, 0, 5, 10, 15, m[s[8]], m[s[9]])
            mix(&v, 1, 6, 11, 12, m[s[10]], m[s[11]])
            mix(&v, 2, 7, 8, 13, m[s[12]], m[s[13]])
            mix(&v, 3, 4, 9, 14, m[s[14]], m[s[15]])
        }

        for i in 0..<8 {
            h[i] ^= v[i] ^ v[i + 8]
        }
    }

    private static func mix(_ v: inout [UInt64], _ a: Int, _ b: Int, _ c: Int, _ d: Int, _ x: UInt64, _ y: UInt64) {
        v[a] = v[a] &+ v[b] &+ x
        v[d] = rotateRight(v[d] ^ v[a], 32)
        v[c] = v[c] &+ v[d]
        v[b] = rotateRight(v[b] ^ v[c], 24)
        v[a] = v[a] &+ v[b] &+ y
        v[d] = rotateRight(v[d] ^ v[a], 16)
        v[c] = v[c] &+ v[d]
        v[b] = rotateRight(v[b] ^ v[c], 63)
    }

    private static func rotateRight(_ x: UInt64, _ n: UInt64) -> UInt64 {
        (x >> n) | (x << (64 - n))
    }
}

enum MinisignError: Error, Equatable {
    case malformedSignature, malformedPublicKey, unsupportedAlgorithm, wrongKey, badSignature
}

enum EngineRelease {
    /// The engine release key (minisign, Ed25519). It is the engine's existing release trust root.
    static let publicKey = "RWSDUnIeQDnpmnNJiTjLmN6XOVFqgn1A0EXvTVG7AJIZXJxhyFN9osxm"
    static let minimumVersion = "1.8.1"
    /// The first engine release that ships native repair (`install.sh --native-repair`, engine #2485).
    static let minimumRepairVersion = "1.9.0"
    static let latestManifestURL = URL(string: "https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/ciaobot-engine-manifest.json")!

    /// The installer is always fetched from its tag, never from `latest`.
    static func installerURL(tag: String) -> URL {
        URL(string: "https://github.com/raffaelefarinaro/ciaobot/releases/download/\(tag)/install.sh")!
    }

    /// true when a >= b, comparing three numeric components.
    static func versionAtLeast(_ a: String, _ b: String) -> Bool {
        let left = a.split(separator: ".").map { Int($0) ?? 0 }
        let right = b.split(separator: ".").map { Int($0) ?? 0 }
        for index in 0..<3 {
            let l = index < left.count ? left[index] : 0
            let r = index < right.count ? right[index] : 0
            if l != r { return l > r }
        }
        return true
    }

    /// Fetches the manifest and "<manifest URL>.sig" with `fetch`, verifies, validates, and returns
    /// the pin to run. Throws EngineReleaseError for verification/validation failures; rethrows
    /// fetch errors unchanged.
    /// Fetches the manifest and "<manifest URL>.sig", verifies the signature over the exact bytes, then decodes.
    private static func verifiedManifest(fetch: NativeInstallFetch,
                                         publicKey: String,
                                         manifestURL: URL) async throws -> EngineManifest {
        let manifestData = try await fetch(manifestURL)
        let signature = try await fetch(manifestURL.appendingPathExtension("sig"))
        do {
            _ = try Minisign.verify(manifestData, signatureFile: signature, publicKey: publicKey)
        } catch let error as MinisignError {
            throw EngineReleaseError.signature(error)
        }
        return try EngineManifest.decode(manifestData)
    }

    static func resolveLatest(fetch: NativeInstallFetch,
                              publicKey: String = EngineRelease.publicKey,
                              manifestURL: URL = EngineRelease.latestManifestURL,
                              fallback: NativeInstallPin = NativeInstallPin.current) async throws -> NativeInstallPin {
        let manifest = try await verifiedManifest(fetch: fetch, publicKey: publicKey, manifestURL: manifestURL)
        guard versionAtLeast(manifest.version, minimumVersion) else {
            throw EngineReleaseError.belowMinimumVersion(manifest.version)
        }
        // A validly signed release without an installer predates the native installer: use the embedded pin.
        guard let installer = manifest.artifacts.first(where: { $0.kind == "installer" && $0.filename == "install.sh" }) else {
            return fallback
        }
        return NativeInstallPin(version: manifest.version,
                                url: installerURL(tag: manifest.tag),
                                sha256: installer.sha256)
    }

    /// Like `resolveLatest`, for native repair: the same signature and manifest checks, then the release
    /// must be at least `minimumRepairVersion` and list `install.sh`. There is no fallback pin: the
    /// embedded pin predates repair. Throws `repairUnavailable` for a verified release that cannot repair.
    static func resolveLatestForRepair(fetch: NativeInstallFetch,
                                       publicKey: String = EngineRelease.publicKey,
                                       manifestURL: URL = EngineRelease.latestManifestURL) async throws -> NativeInstallPin {
        let manifest = try await verifiedManifest(fetch: fetch, publicKey: publicKey, manifestURL: manifestURL)
        guard versionAtLeast(manifest.version, minimumRepairVersion),
              let installer = manifest.artifacts.first(where: { $0.kind == "installer" && $0.filename == "install.sh" }) else {
            throw EngineReleaseError.repairUnavailable
        }
        return NativeInstallPin(version: manifest.version,
                                url: installerURL(tag: manifest.tag),
                                sha256: installer.sha256)
    }
}

/// Verifies minisign signatures (https://jedisct1.github.io/minisign/) with Ed25519.
enum Minisign {
    /// Verifies `data` against a minisign signature file and returns the trusted comment.
    /// `signatureFile` is either minisign text (4 lines) or that text base64-encoded as one
    /// blob (the Tauri signer's `.sig` format). Accepts algorithm "ED" (BLAKE2b-512 prehashed)
    /// and "Ed" (pure Ed25519). Checks the key id, the signature and the global signature
    /// over (signature bytes + trusted comment UTF-8).
    static func verify(_ data: Data, signatureFile: Data, publicKey: String) throws -> String {
        let (keyID, keyBytes) = try parsePublicKey(publicKey)
        let lines = try signatureLines(signatureFile)

        guard lines.count >= 4,
              lines[0].hasPrefix("untrusted comment:"),
              lines[2].hasPrefix("trusted comment: ") else {
            throw MinisignError.malformedSignature
        }
        guard let signatureBlob = Data(base64Encoded: lines[1]), signatureBlob.count == 74 else {
            throw MinisignError.malformedSignature
        }
        guard let globalSignature = Data(base64Encoded: lines[3]), globalSignature.count == 64 else {
            throw MinisignError.malformedSignature
        }
        guard signatureBlob[2..<10] == keyID else { throw MinisignError.wrongKey }

        let message: Data
        switch String(decoding: signatureBlob[0..<2], as: UTF8.self) {
        case "ED": message = Blake2b.hash512(data)
        case "Ed": message = data
        default: throw MinisignError.unsupportedAlgorithm
        }

        let signature = Data(signatureBlob[10..<74])
        let trustedComment = String(lines[2].dropFirst("trusted comment: ".count))
        guard let key = try? Curve25519.Signing.PublicKey(rawRepresentation: keyBytes) else {
            throw MinisignError.malformedPublicKey
        }
        guard key.isValidSignature(signature, for: message),
              key.isValidSignature(globalSignature, for: signature + Data(trustedComment.utf8)) else {
            throw MinisignError.badSignature
        }
        return trustedComment
    }

    /// The 42-byte public key: "Ed", an 8-byte key id, then the 32-byte Ed25519 key.
    private static func parsePublicKey(_ publicKey: String) throws -> (keyID: Data, keyBytes: Data) {
        guard let raw = Data(base64Encoded: publicKey.trimmingCharacters(in: .whitespacesAndNewlines)),
              raw.count == 42,
              raw[0..<2] == Data("Ed".utf8) else {
            throw MinisignError.malformedPublicKey
        }
        return (Data(raw[2..<10]), Data(raw[10..<42]))
    }

    /// The signature file as non-empty lines. A file that does not start with
    /// `untrusted comment:` is taken as the base64 blob of the minisign text.
    private static func signatureLines(_ signatureFile: Data) throws -> [String] {
        guard let text = String(data: signatureFile, encoding: .utf8) else {
            throw MinisignError.malformedSignature
        }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        var minisignText = trimmed
        if !trimmed.hasPrefix("untrusted comment:") {
            guard let decoded = Data(base64Encoded: trimmed, options: .ignoreUnknownCharacters),
                  let decodedText = String(data: decoded, encoding: .utf8) else {
                throw MinisignError.malformedSignature
            }
            minisignText = decodedText.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        // Trim CR so CRLF signature files parse the same as LF ones (matches the engine's verifier).
        // Split on the newline string: a "\r\n" pair is one Character, so a Character split would miss it.
        var lines = minisignText.components(separatedBy: "\n").map {
            $0.trimmingCharacters(in: CharacterSet(charactersIn: "\r"))
        }
        while lines.last?.isEmpty == true { lines.removeLast() }
        return lines
    }
}

enum EngineReleaseError: Error, Equatable {
    case signature(MinisignError)
    case malformedManifest
    case belowMinimumVersion(String)
    case repairUnavailable
}

/// The signed release manifest (`ciaobot-engine-manifest.json`).
struct EngineManifest: Equatable {
    struct Artifact: Equatable {
        let filename: String
        let kind: String
        let sha256: String
        let size: Int
    }

    let schema: Int
    let version: String
    let tag: String
    let artifacts: [Artifact]

    /// Decodes and validates: schema == 1, version matches ^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$,
    /// tag == "v" + version, every artifact sha256 matches ^[0-9a-f]{64}$ and size > 0.
    /// Unknown keys and artifact kinds are ignored.
    static func decode(_ data: Data) throws -> EngineManifest {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let schema = object["schema"] as? Int,
              let version = object["version"] as? String,
              let tag = object["tag"] as? String,
              let rawArtifacts = object["artifacts"] as? [Any] else {
            throw EngineReleaseError.malformedManifest
        }
        guard schema == 1,
              version.range(of: #"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\z"#, options: .regularExpression) != nil,
              tag == "v" + version else {
            throw EngineReleaseError.malformedManifest
        }

        var artifacts: [Artifact] = []
        for entry in rawArtifacts {
            guard let fields = entry as? [String: Any] else { throw EngineReleaseError.malformedManifest }
            // A digest that is present must be well formed, even on an entry that is skipped below.
            if let digest = fields["sha256"] {
                guard let hex = digest as? String, hex.range(of: #"^[0-9a-f]{64}\z"#, options: .regularExpression) != nil else {
                    throw EngineReleaseError.malformedManifest
                }
            }
            let isInstaller = fields["kind"] as? String == "installer" && fields["filename"] as? String == "install.sh"
            // An incomplete installer entry fails closed. Any other entry missing a field is skipped.
            guard let filename = fields["filename"] as? String,
                  let kind = fields["kind"] as? String,
                  let sha256 = fields["sha256"] as? String,
                  let size = fields["size"] as? Int else {
                if isInstaller { throw EngineReleaseError.malformedManifest }
                continue
            }
            guard size > 0 else { throw EngineReleaseError.malformedManifest }
            artifacts.append(Artifact(filename: filename, kind: kind, sha256: sha256, size: size))
        }
        return EngineManifest(schema: schema, version: version, tag: tag, artifacts: artifacts)
    }
}
