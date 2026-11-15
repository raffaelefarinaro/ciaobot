import CryptoKit
import XCTest
import Foundation

@testable import CiaobotMac

final class EngineReleaseTests: XCTestCase {
    // MARK: Helpers

    private func hex(_ data: Data) -> String {
        data.map { String(format: "%02x", $0) }.joined()
    }

    // MARK: BLAKE2b

    func testBlake2bMatchesReferenceVectors() {
        XCTAssertEqual(
            hex(Blake2b.hash512(Data())),
            "786a02f742015903c6c6fd852552d272912f4740e15847618a86e217f71f5419d25e1031afee585313896444934eb04b903a685b1448b755d56f701afe9be2ce"
        )
        XCTAssertEqual(
            hex(Blake2b.hash512(Data("abc".utf8))),
            "ba80a53f981c4d0d6a2797b69f12f6e94c212f14685ac4b74b12bb6fdbffa2d17d87c5392aab792dc252d5de4533cc9518d38aa8dbf1925ab92386edd4009923"
        )
        // 512 bytes: exactly four full 128-byte blocks, so the last block is full and final.
        let fourBlocks = Data((0..<512).map { UInt8($0 % 256) })
        XCTAssertEqual(
            hex(Blake2b.hash512(fourBlocks)),
            "c59ab1095ca4579525338b6b74689ff234bc3fe9765fe26dfb04ddceaee0ab84dfd8967594cb261fcd88687f4454d80f718116c1b3c32f9f7e169357468cbe67"
        )
        let thousand = Data((0..<1000).map { UInt8($0 % 251) })
        XCTAssertEqual(
            hex(Blake2b.hash512(thousand)),
            "c11e1c0340bd7e5a1b275f1230c962fad215ecb1391486e74e31b960a2f2996381a5fad092da06841d5f26e38f6ecfeaf441acbcd1c2de61aef121e7927175f5"
        )
    }

    // MARK: Manifest

    private func manifestJSON(
        schema: Int = 1,
        version: String = "1.8.1",
        tag: String = "v1.8.1",
        sha256: String = String(repeating: "a", count: 64)
    ) throws -> Data {
        let object: [String: Any] = [
            "schema": schema,
            "version": version,
            "tag": tag,
            "artifacts": [
                ["filename": "ciaobot-1.8.1-py3-none-any.whl", "kind": "wheel", "sha256": sha256, "size": 4_981_493],
            ],
        ]
        return try JSONSerialization.data(withJSONObject: object)
    }

    func testManifestValidation() throws {
        XCTAssertEqual(try EngineManifest.decode(try manifestJSON()).version, "1.8.1")

        let invalid: [(String, Data)] = [
            ("schema 2", try manifestJSON(schema: 2)),
            ("version 1.8", try manifestJSON(version: "1.8", tag: "v1.8")),
            ("tag v1.8.2 for version 1.8.1", try manifestJSON(tag: "v1.8.2")),
            ("uppercase sha256", try manifestJSON(sha256: String(repeating: "A", count: 64))),
        ]
        for (name, data) in invalid {
            XCTAssertThrowsError(try EngineManifest.decode(data), name) { error in
                XCTAssertEqual(error as? EngineReleaseError, .malformedManifest, name)
            }
        }
    }

    // MARK: Version and resolution

    /// Records every URL a fake fetch is asked for.
    private final class FetchLog: @unchecked Sendable {
        private let lock = NSLock()
        private var urls: [URL] = []

        func record(_ url: URL) {
            lock.lock()
            urls.append(url)
            lock.unlock()
        }

        var requested: [URL] {
            lock.lock()
            defer { lock.unlock() }
            return urls
        }
    }

    private let installerDigest = String(repeating: "c", count: 64)

    private func releaseManifest(version: String, tag: String, artifacts: [[String: Any]]) throws -> Data {
        try JSONSerialization.data(withJSONObject: [
            "schema": 1,
            "version": version,
            "tag": tag,
            "artifacts": artifacts,
        ] as [String: Any])
    }

    private var installerArtifact: [String: Any] {
        ["filename": "install.sh", "kind": "installer", "sha256": installerDigest, "size": 20_480]
    }

    /// Serves the manifest and its `.sig` for the latest-manifest URL and fails any other URL.
    private func serving(manifest: Data, signature: Data, log: FetchLog) -> NativeInstallFetch {
        let manifestURL = EngineRelease.latestManifestURL
        let signatureURL = manifestURL.appendingPathExtension("sig")
        return { url in
            log.record(url)
            if url == manifestURL { return manifest }
            if url == signatureURL { return signature }
            throw URLError(.fileDoesNotExist)
        }
    }

    func testVersionComparison() {
        XCTAssertTrue(EngineRelease.versionAtLeast("1.8.1", "1.8.1"))
        XCTAssertTrue(EngineRelease.versionAtLeast("1.10.0", "1.9.9"))
        XCTAssertTrue(EngineRelease.versionAtLeast("2.0.0", "1.8.1"))
        XCTAssertFalse(EngineRelease.versionAtLeast("1.8.0", "1.8.1"))
    }

    func testResolveLatestUsesTheSignedInstallerEntry() async throws {
        let manifest = try releaseManifest(version: "1.9.0", tag: "v1.9.0", artifacts: [installerArtifact])
        let key = Curve25519.Signing.PrivateKey()
        let signed = try sign(data: manifest, algorithm: "ED", key: key,
                              keyID: Data([1, 2, 3, 4, 5, 6, 7, 8]), comment: "file:ciaobot-engine-manifest.json")
        let log = FetchLog()

        let pin = try await EngineRelease.resolveLatest(
            fetch: serving(manifest: manifest, signature: signed.signatureFile, log: log),
            publicKey: signed.publicKey
        )

        XCTAssertEqual(pin, NativeInstallPin(
            version: "1.9.0",
            url: URL(string: "https://github.com/raffaelefarinaro/ciaobot/releases/download/v1.9.0/install.sh")!,
            sha256: installerDigest
        ))
        // Only the manifest and its signature are fetched; install.sh is never requested from `latest`.
        XCTAssertEqual(log.requested, [
            EngineRelease.latestManifestURL,
            EngineRelease.latestManifestURL.appendingPathExtension("sig"),
        ])
    }

    func testResolveLatestFallsBackOnlyWhenTheSignedManifestHasNoInstaller() async throws {
        let wheelOnly = try releaseManifest(version: "1.8.1", tag: "v1.8.1", artifacts: [[
            "filename": "ciaobot-1.8.1-py3-none-any.whl", "kind": "wheel", "sha256": installerDigest, "size": 4_981_493,
        ]])
        let key = Curve25519.Signing.PrivateKey()
        let signed = try sign(data: wheelOnly, algorithm: "ED", key: key,
                              keyID: Data([1, 2, 3, 4, 5, 6, 7, 8]), comment: "file:ciaobot-engine-manifest.json")

        let pin = try await EngineRelease.resolveLatest(
            fetch: serving(manifest: wheelOnly, signature: signed.signatureFile, log: FetchLog()),
            publicKey: signed.publicKey
        )

        XCTAssertEqual(pin, NativeInstallPin.current)
    }

    func testResolveLatestRefusesUnverifiedOrOldReleases() async throws {
        let key = Curve25519.Signing.PrivateKey()
        let keyID = Data([1, 2, 3, 4, 5, 6, 7, 8])

        // A bad signature: the served manifest differs by one byte from the signed one.
        let manifest = try releaseManifest(version: "1.9.0", tag: "v1.9.0", artifacts: [installerArtifact])
        let signed = try sign(data: manifest, algorithm: "ED", key: key, keyID: keyID, comment: "c")
        var tampered = manifest
        tampered[tampered.count - 2] ^= 0x01
        do {
            _ = try await EngineRelease.resolveLatest(
                fetch: serving(manifest: tampered, signature: signed.signatureFile, log: FetchLog()),
                publicKey: signed.publicKey
            )
            XCTFail("a tampered manifest resolved")
        } catch {
            XCTAssertEqual(error as? EngineReleaseError, .signature(.badSignature))
        }

        // A validly signed release below the floor.
        let old = try releaseManifest(version: "1.8.0", tag: "v1.8.0", artifacts: [installerArtifact])
        let signedOld = try sign(data: old, algorithm: "ED", key: key, keyID: keyID, comment: "c")
        do {
            _ = try await EngineRelease.resolveLatest(
                fetch: serving(manifest: old, signature: signedOld.signatureFile, log: FetchLog()),
                publicKey: signedOld.publicKey
            )
            XCTFail("a release below the minimum version resolved")
        } catch {
            XCTAssertEqual(error as? EngineReleaseError, .belowMinimumVersion("1.8.0"))
        }

        // A network failure is rethrown unchanged, not wrapped as a release error.
        let offline: NativeInstallFetch = { _ in throw URLError(.notConnectedToInternet) }
        do {
            _ = try await EngineRelease.resolveLatest(fetch: offline, publicKey: signed.publicKey)
            XCTFail("an unreachable manifest resolved")
        } catch let error as URLError {
            XCTAssertEqual(error.code, .notConnectedToInternet)
        } catch {
            XCTFail("the fetch error was wrapped as \(error)")
        }
    }

    func testAnIncompleteInstallerEntryIsMalformed() async throws {
        // An installer entry without `size` must fail closed, not fall back to the embedded pin.
        let manifest = try releaseManifest(version: "1.9.0", tag: "v1.9.0", artifacts: [[
            "kind": "installer", "filename": "install.sh", "sha256": installerDigest,
        ]])
        let key = Curve25519.Signing.PrivateKey()
        let signed = try sign(data: manifest, algorithm: "ED", key: key,
                              keyID: Data([1, 2, 3, 4, 5, 6, 7, 8]), comment: "file:ciaobot-engine-manifest.json")

        do {
            _ = try await EngineRelease.resolveLatest(
                fetch: serving(manifest: manifest, signature: signed.signatureFile, log: FetchLog()),
                publicKey: signed.publicKey
            )
            XCTFail("an incomplete installer entry resolved")
        } catch {
            XCTAssertEqual(error as? EngineReleaseError, .malformedManifest)
        }
    }

    func testCRLFSignatureFilesVerify() throws {
        let payload = Data("synthetic release manifest".utf8)
        let key = Curve25519.Signing.PrivateKey()
        let signed = try sign(data: payload, algorithm: "ED", key: key,
                              keyID: Data([1, 2, 3, 4, 5, 6, 7, 8]), comment: "crlf comment")
        let text = String(decoding: signed.signatureFile, as: UTF8.self)
        let crlf = Data(text.replacingOccurrences(of: "\n", with: "\r\n").utf8)

        XCTAssertEqual(
            try Minisign.verify(payload, signatureFile: crlf, publicKey: signed.publicKey),
            "crlf comment"
        )
    }

    // MARK: Minisign

    /// The real v1.8.1 release manifest, as base64 (725 bytes).
    private static let realManifestBase64 = "ewogICJhcnRpZmFjdHMiOiBbCiAgICB7CiAgICAgICJhcmNoIjogImFueSIsCiAgICAgICJmaWxlbmFtZSI6ICJjaWFvYm90LTEuOC4xLXB5My1ub25lLWFueS53aGwiLAogICAgICAia2luZCI6ICJ3aGVlbCIsCiAgICAgICJwbGF0Zm9ybSI6ICJhbnkiLAogICAgICAic2hhMjU2IjogImRkZWVkMDU3MWVmNGRhMDg3MTNjOTIzZmYxNzUwNThjYmUzM2QxZGE5ZDhiZjRmOTk4MmQ3YTZhNjMzODFkNjIiLAogICAgICAic2l6ZSI6IDQ5ODE0OTMKICAgIH0sCiAgICB7CiAgICAgICJhcmNoIjogInVuaXZlcnNhbCIsCiAgICAgICJidW5kbGVfaWQiOiAibG9jYWwuY2lhb2JvdC5zZXJ2ZXIiLAogICAgICAiZmlsZW5hbWUiOiAiY2lhb2JvdC1zZXJ2ZXItaG9zdC1tYWNvcy11bml2ZXJzYWwtdjEudGFyLmd6IiwKICAgICAgImhvc3RfcHJvdG9jb2wiOiAyLAogICAgICAiaG9zdF9yZXZpc2lvbiI6IDQsCiAgICAgICJraW5kIjogInNlcnZlci1ob3N0IiwKICAgICAgInBsYXRmb3JtIjogIm1hY29zIiwKICAgICAgInNoYTI1NiI6ICJhZDg0ODFiM2EyMjUzNmFiYjlhZDA4NmJlZWMxYWQ3Y2QyN2VlNWZiODQzZGNmMDhkYmUxZmRjOTViNzNkNDMxIiwKICAgICAgInNpemUiOiAzODgzNzcKICAgIH0KICBdLAogICJjcmVhdGVkIjogIjIwMjYtMTEtMDJUMDM6Mzc6MTQrMDA6MDAiLAogICJzY2hlbWEiOiAxLAogICJ0YWciOiAidjEuOC4xIiwKICAidmVyc2lvbiI6ICIxLjguMSIKfQo="

    /// The real `.sig` file bytes as served (a base64 blob of the minisign text).
    /// They are passed to the verifier unchanged.
    private static let realSignatureFile = "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIHRhdXJpIHNlY3JldCBrZXkKUlVTRFVuSWVRRG5wbWkyeUc1enNYYzlHbGxtS01DaG5raTUrZysxMGZoVW9YTVREcE5MeDU5U2JSN1R4NnlWVzZGWXE2a1p0QnA2YVl4UWN2QmxnUm1zNnpEdWV5V3ZNZkFnPQp0cnVzdGVkIGNvbW1lbnQ6IHRpbWVzdGFtcDoxNzkzNTkwNjM2CWZpbGU6Y2lhb2JvdC1lbmdpbmUtbWFuaWZlc3QuanNvbgo5cXpIY0EvaDQySzZ1Um44SHl0SmthZ1FhbWFNanlqSm1kb0MrTmFqbDdFcUp1bUx5d1B4ZlJwTG9aK2tpVitIZ0NKcFdZaElVcSs2WjcwQ2psSjBDZz09Cg=="

    func testMinimumRepairVersionIsAboveTheInstallFloor() {
        XCTAssertEqual(EngineRelease.minimumRepairVersion, "1.9.0")
        XCTAssertTrue(EngineRelease.versionAtLeast(EngineRelease.minimumRepairVersion, EngineRelease.minimumVersion))
    }

    func testResolveForRepairUsesTheSignedInstallerEntry() async throws {
        let manifest = try releaseManifest(version: "1.9.0", tag: "v1.9.0", artifacts: [installerArtifact])
        let key = Curve25519.Signing.PrivateKey()
        let signed = try sign(data: manifest, algorithm: "ED", key: key,
                              keyID: Data([1, 2, 3, 4, 5, 6, 7, 8]), comment: "file:ciaobot-engine-manifest.json")
        let log = FetchLog()

        let pin = try await EngineRelease.resolveLatestForRepair(
            fetch: serving(manifest: manifest, signature: signed.signatureFile, log: log),
            publicKey: signed.publicKey
        )

        XCTAssertEqual(pin, NativeInstallPin(
            version: "1.9.0",
            url: URL(string: "https://github.com/raffaelefarinaro/ciaobot/releases/download/v1.9.0/install.sh")!,
            sha256: installerDigest
        ))
        XCTAssertEqual(log.requested, [
            EngineRelease.latestManifestURL,
            EngineRelease.latestManifestURL.appendingPathExtension("sig"),
        ])
    }

    func testResolveForRepairRefusesAReleaseWithoutRepair() async throws {
        let key = Curve25519.Signing.PrivateKey()
        let keyID = Data([1, 2, 3, 4, 5, 6, 7, 8])
        let wheel: [String: Any] = [
            "filename": "ciaobot-1.9.0-py3-none-any.whl", "kind": "wheel", "sha256": installerDigest, "size": 4_981_493,
        ]
        let cases: [(version: String, artifacts: [[String: Any]])] = [
            // One patch below the repair floor, even with an installer.
            ("1.8.1", [installerArtifact]),
            // Newer, but no installer: no fallback pin may be returned.
            ("1.9.0", [wheel]),
            // Below the install floor.
            ("1.8.0", [installerArtifact]),
        ]
        for (version, artifacts) in cases {
            let manifest = try releaseManifest(version: version, tag: "v\(version)", artifacts: artifacts)
            let signed = try sign(data: manifest, algorithm: "ED", key: key, keyID: keyID, comment: "c")
            do {
                let pin = try await EngineRelease.resolveLatestForRepair(
                    fetch: serving(manifest: manifest, signature: signed.signatureFile, log: FetchLog()),
                    publicKey: signed.publicKey
                )
                XCTFail("release \(version) resolved for repair as \(pin)")
            } catch {
                XCTAssertEqual(error as? EngineReleaseError, .repairUnavailable, "release \(version)")
            }
        }
    }

    func testResolveForRepairRefusesAnUnverifiedRelease() async throws {
        let key = Curve25519.Signing.PrivateKey()
        let keyID = Data([1, 2, 3, 4, 5, 6, 7, 8])
        let manifest = try releaseManifest(version: "1.9.0", tag: "v1.9.0", artifacts: [installerArtifact])
        let signed = try sign(data: manifest, algorithm: "ED", key: key, keyID: keyID, comment: "c")

        var tampered = manifest
        tampered[tampered.count - 2] ^= 0x01
        do {
            _ = try await EngineRelease.resolveLatestForRepair(
                fetch: serving(manifest: tampered, signature: signed.signatureFile, log: FetchLog()),
                publicKey: signed.publicKey
            )
            XCTFail("a tampered manifest resolved for repair")
        } catch {
            XCTAssertEqual(error as? EngineReleaseError, .signature(.badSignature))
        }

        let offline: NativeInstallFetch = { _ in throw URLError(.notConnectedToInternet) }
        do {
            _ = try await EngineRelease.resolveLatestForRepair(fetch: offline, publicKey: signed.publicKey)
            XCTFail("an unreachable manifest resolved for repair")
        } catch let error as URLError {
            XCTAssertEqual(error.code, .notConnectedToInternet)
        } catch {
            XCTFail("the fetch error was wrapped as \(error)")
        }
    }

    /// The real engine v1.9.0 manifest and `.sig`, the first release whose signed manifest lists
    /// `install.sh` (engine #2483) and ships native repair (engine #2485).
    private static let realV190ManifestBase64 = "ewogICJhcnRpZmFjdHMiOiBbCiAgICB7CiAgICAgICJhcmNoIjogImFueSIsCiAgICAgICJmaWxlbmFtZSI6ICJjaWFvYm90LTEuOS4wLXB5My1ub25lLWFueS53aGwiLAogICAgICAia2luZCI6ICJ3aGVlbCIsCiAgICAgICJwbGF0Zm9ybSI6ICJhbnkiLAogICAgICAic2hhMjU2IjogIjM0ZDYzNjkzNDllZjIyY2M1MDZhOWYxMTg5OTQxZTNiYmQwNDU5ODgzZjJhOWVkNjhjZGM3YWQyNzA2YjcwOTYiLAogICAgICAic2l6ZSI6IDQ5OTgxMzAKICAgIH0sCiAgICB7CiAgICAgICJhcmNoIjogInVuaXZlcnNhbCIsCiAgICAgICJidW5kbGVfaWQiOiAibG9jYWwuY2lhb2JvdC5zZXJ2ZXIiLAogICAgICAiZmlsZW5hbWUiOiAiY2lhb2JvdC1zZXJ2ZXItaG9zdC1tYWNvcy11bml2ZXJzYWwtdjEudGFyLmd6IiwKICAgICAgImhvc3RfcHJvdG9jb2wiOiAyLAogICAgICAiaG9zdF9yZXZpc2lvbiI6IDQsCiAgICAgICJraW5kIjogInNlcnZlci1ob3N0IiwKICAgICAgInBsYXRmb3JtIjogIm1hY29zIiwKICAgICAgInNoYTI1NiI6ICJhOTgwMzM3OTZiMjM5MjNjMTkxNzU3YjVkOWJlOTlkZDQ1YzJmZjYxZTRjMTBhZjQ2ZDIxNTkxMzc2MDlhOGI0IiwKICAgICAgInNpemUiOiAzODgzODAKICAgIH0sCiAgICB7CiAgICAgICJhcmNoIjogImFueSIsCiAgICAgICJmaWxlbmFtZSI6ICJpbnN0YWxsLnNoIiwKICAgICAgImtpbmQiOiAiaW5zdGFsbGVyIiwKICAgICAgInBsYXRmb3JtIjogIm1hY29zIiwKICAgICAgInNoYTI1NiI6ICI4NDhjMTQ3NjU5YzMzMzAxYzE1MTA3ODM4ZTRjZmIzNGU5NGY3OWJkYmU3ZTcyZDFlMmQ3MzI1YWUzOGY2NmY1IiwKICAgICAgInNpemUiOiAxOTgyNzcKICAgIH0sCiAgICB7CiAgICAgICJhcmNoIjogImFueSIsCiAgICAgICJmaWxlbmFtZSI6ICJpbnN0YWxsLWVuZ2luZS5zaCIsCiAgICAgICJraW5kIjogImluc3RhbGxlciIsCiAgICAgICJwbGF0Zm9ybSI6ICJtYWNvcyIsCiAgICAgICJzaGEyNTYiOiAiODQ4YzE0NzY1OWMzMzMwMWMxNTEwNzgzOGU0Y2ZiMzRlOTRmNzliZGJlN2U3MmQxZTJkNzMyNWFlMzhmNjZmNSIsCiAgICAgICJzaXplIjogMTk4Mjc3CiAgICB9LAogICAgewogICAgICAiYXJjaCI6ICJhbnkiLAogICAgICAiZmlsZW5hbWUiOiAiaW5zdGFsbC5wczEiLAogICAgICAia2luZCI6ICJpbnN0YWxsZXIiLAogICAgICAicGxhdGZvcm0iOiAid2luZG93cyIsCiAgICAgICJzaGEyNTYiOiAiOTk2ZDdlMzhiNjY5MzRiYWYxYTg3OWRmZDg3NjMxYmQyOTA4MTlkMjRjNzRhNWVlYjI5MWM1ZDQ0OWNhNDRhZCIsCiAgICAgICJzaXplIjogNjY3MzEKICAgIH0KICBdLAogICJjcmVhdGVkIjogIjIwMjYtMTEtMDJUMjM6MzM6MjErMDA6MDAiLAogICJzY2hlbWEiOiAxLAogICJ0YWciOiAidjEuOS4wIiwKICAidmVyc2lvbiI6ICIxLjkuMCIKfQo="
    private static let realV190SignatureFile = "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIHRhdXJpIHNlY3JldCBrZXkKUlVTRFVuSWVRRG5wbWpwM3R3emlCMUZManRYYUs3RUhOT002c0F6ekhyWFVURUpFenpESFJ1eFRsdjUxdStqOTlIdG1ZRXpYWmMyL0I2MlBxZUEvYmtETlFLc1kra0NyN3c0PQp0cnVzdGVkIGNvbW1lbnQ6IHRpbWVzdGFtcDoxNzkzNjYyNDAyCWZpbGU6Y2lhb2JvdC1lbmdpbmUtbWFuaWZlc3QuanNvbgpWUVVEQklja1FpNWtUUTVsRHdGUXF2SXpqNHVXL21mYVhvSGcwWnlLSXlVb3d1bW5PNk56QnN2Zk83Y1lDajErZlRQZUtXV085aEFmZlZmZzZFaUVEdz09Cg=="

    func testTheRealV190ReleaseResolvesForInstallAndRepair() async throws {
        let manifest = try XCTUnwrap(Data(base64Encoded: Self.realV190ManifestBase64))
        let signature = Data(Self.realV190SignatureFile.utf8)
        let expected = NativeInstallPin(
            version: "1.9.0",
            url: URL(string: "https://github.com/raffaelefarinaro/ciaobot/releases/download/v1.9.0/install.sh")!,
            sha256: "848c147659c33301c15107838e4cfb34e94f79bdbe7e72d1e2d7325ae38f66f5"
        )
        let install = try await EngineRelease.resolveLatest(
            fetch: serving(manifest: manifest, signature: signature, log: FetchLog()))
        XCTAssertEqual(install, expected, "the real signed v1.9.0 manifest must replace the v1.8.1 fallback")
        let repair = try await EngineRelease.resolveLatestForRepair(
            fetch: serving(manifest: manifest, signature: signature, log: FetchLog()))
        XCTAssertEqual(repair, expected)
    }

    private func realManifest() throws -> Data {
        try XCTUnwrap(Data(base64Encoded: Self.realManifestBase64))
    }

    /// Builds a minisign text signature file and its public key the way `minisign -S` does.
    private func sign(
        data: Data,
        algorithm: String,
        key: Curve25519.Signing.PrivateKey,
        keyID: Data,
        comment: String
    ) throws -> (publicKey: String, signatureFile: Data) {
        let message = algorithm == "ED" ? Blake2b.hash512(data) : data
        let signature = try key.signature(for: message)
        let global = try key.signature(for: signature + Data(comment.utf8))
        let publicBlob = Data("Ed".utf8) + keyID + key.publicKey.rawRepresentation
        let signatureBlob = Data(algorithm.utf8) + keyID + signature
        let text = "untrusted comment: test signature\n"
            + signatureBlob.base64EncodedString() + "\n"
            + "trusted comment: " + comment + "\n"
            + global.base64EncodedString() + "\n"
        return (publicBlob.base64EncodedString(), Data(text.utf8))
    }

    func testTheRealV181ManifestVerifiesWithTheReleaseKey() throws {
        let comment = try Minisign.verify(
            try realManifest(),
            signatureFile: Data(Self.realSignatureFile.utf8),
            publicKey: EngineRelease.publicKey
        )

        XCTAssertTrue(comment.contains("file:ciaobot-engine-manifest.json"))

        let manifest = try EngineManifest.decode(try realManifest())
        XCTAssertEqual(manifest.version, "1.8.1")
        XCTAssertEqual(manifest.tag, "v1.8.1")
        XCTAssertEqual(manifest.artifacts.map(\.kind), ["wheel", "server-host"])
    }

    func testATamperedManifestFailsVerification() throws {
        var tampered = try realManifest()
        tampered[0] ^= 0x01

        XCTAssertThrowsError(try Minisign.verify(
            tampered,
            signatureFile: Data(Self.realSignatureFile.utf8),
            publicKey: EngineRelease.publicKey
        )) { error in
            XCTAssertEqual(error as? MinisignError, .badSignature)
        }
    }

    func testAnotherKeyIsRejected() throws {
        let other = try sign(
            data: Data("another release".utf8),
            algorithm: "ED",
            key: Curve25519.Signing.PrivateKey(),
            keyID: Data([9, 8, 7, 6, 5, 4, 3, 2]),
            comment: "another key"
        )

        XCTAssertThrowsError(try Minisign.verify(
            try realManifest(),
            signatureFile: Data(Self.realSignatureFile.utf8),
            publicKey: other.publicKey
        )) { error in
            XCTAssertEqual(error as? MinisignError, .wrongKey)
        }
    }

    func testSyntheticSignaturesInBothAlgorithmsVerify() throws {
        let key = Curve25519.Signing.PrivateKey()
        let keyID = Data([1, 2, 3, 4, 5, 6, 7, 8])
        let payload = Data("synthetic release manifest".utf8)

        for algorithm in ["ED", "Ed"] {
            let signed = try sign(
                data: payload,
                algorithm: algorithm,
                key: key,
                keyID: keyID,
                comment: "trusted \(algorithm)"
            )

            XCTAssertEqual(
                try Minisign.verify(payload, signatureFile: signed.signatureFile, publicKey: signed.publicKey),
                "trusted \(algorithm)"
            )
            // The Tauri-style form: the whole minisign text, base64-encoded as one blob.
            XCTAssertEqual(
                try Minisign.verify(payload, signatureFile: signed.signatureFile.base64EncodedData(), publicKey: signed.publicKey),
                "trusted \(algorithm)"
            )
        }
    }

    func testRejectsUnsupportedAlgorithmAndMalformedInput() throws {
        let key = Curve25519.Signing.PrivateKey()
        let keyID = Data([1, 2, 3, 4, 5, 6, 7, 8])
        let payload = Data("synthetic release manifest".utf8)

        let unsupported = try sign(data: payload, algorithm: "XX", key: key, keyID: keyID, comment: "c")
        XCTAssertThrowsError(try Minisign.verify(
            payload, signatureFile: unsupported.signatureFile, publicKey: unsupported.publicKey
        )) { error in
            XCTAssertEqual(error as? MinisignError, .unsupportedAlgorithm)
        }

        let signed = try sign(data: payload, algorithm: "ED", key: key, keyID: keyID, comment: "c")
        let text = String(decoding: signed.signatureFile, as: UTF8.self)
        let threeLines = Data((text.split(separator: "\n").prefix(3).joined(separator: "\n") + "\n").utf8)
        XCTAssertThrowsError(try Minisign.verify(
            payload, signatureFile: threeLines, publicKey: signed.publicKey
        )) { error in
            XCTAssertEqual(error as? MinisignError, .malformedSignature)
        }

        // "RWSD" decodes to 3 bytes; a public key is exactly 42.
        XCTAssertThrowsError(try Minisign.verify(
            payload, signatureFile: signed.signatureFile, publicKey: "RWSD"
        )) { error in
            XCTAssertEqual(error as? MinisignError, .malformedPublicKey)
        }

        let tamperedComment = Data(text.replacingOccurrences(of: "trusted comment: c", with: "trusted comment: d").utf8)
        XCTAssertThrowsError(try Minisign.verify(
            payload, signatureFile: tamperedComment, publicKey: signed.publicKey
        )) { error in
            XCTAssertEqual(error as? MinisignError, .badSignature)
        }
    }
}
