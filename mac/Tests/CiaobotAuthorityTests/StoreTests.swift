import Darwin
import XCTest

@testable import CiaobotAuthority

final class StoreTests: XCTestCase {
    let op = OperatorTestSigner()
    var roots: [String] = []

    override func tearDown() {
        for r in roots { try? FileManager.default.removeItem(atPath: r) }
        roots = []
    }

    /// A provisioned (and optionally enrolled) root inside a fresh temp directory.
    private func makeRoot(enroll: Bool = true) throws -> String {
        let parent = try temporaryDirectory("store")
        roots.append(parent)
        let root = parent + "/authority"
        try AuthorityStore.provision(root: root)
        if enroll {
            try AuthorityStore.enroll(root: root, operatorPublicKeyX963: Vectors.operatorPublic, readerUID: geteuid())
        }
        return root
    }

    private func mode(_ path: String) -> mode_t {
        var info = stat()
        XCTAssertEqual(lstat(path, &info), 0, path)
        return info.st_mode & 0o7777
    }

    private func grantParams() -> [String: JSONValue] {
        [
            "workspace": .string("w1"), "project": .string("p1"), "chat": .string("c1"), "destination": .string("brain"),
            "audience": .string("team"), "producers": .array([.string("memory_extraction")]),
        ]
    }

    func testProvisionCreatesTheOwnerPrivateLayout() throws {
        let root = try makeRoot(enroll: false)
        XCTAssertEqual(mode(root), 0o700)
        XCTAssertEqual(mode(root + "/state"), 0o700)
        XCTAssertEqual(mode(root + "/anchor"), 0o700)
        for f in ["state/event.seed", "state/state.key", "anchor/counter"] { XCTAssertEqual(mode(root + "/" + f), 0o600, f) }
        let seed = try Data(contentsOf: URL(fileURLWithPath: root + "/state/event.seed"))
        let key = try Data(contentsOf: URL(fileURLWithPath: root + "/state/state.key"))
        XCTAssertEqual(seed.count, 32)
        XCTAssertNotEqual(seed, key)
        let store = try AuthorityStore(root: root)
        XCTAssertEqual(try store.anchor.read(), AnchorRecord(counter: 0))
        XCTAssertNil(store.enrollment)
        XCTAssertThrowsError(try store.openAuthority()) { XCTAssertEqual(($0 as? AuthorityError)?.code, "not_enrolled") }
    }

    func testProvisionRefusesAnExistingRoot() throws {
        let root = try makeRoot(enroll: false)
        XCTAssertThrowsError(try AuthorityStore.provision(root: root))
    }

    func testEnrollmentHappensOnceAndNeverReplaces() throws {
        let root = try makeRoot()
        let other = OperatorTestSigner(scalarHex: String(repeating: "0", count: 63) + "7")
        XCTAssertThrowsError(
            try AuthorityStore.enroll(root: root, operatorPublicKeyX963: Array(other.key.publicKey.x963Representation), readerUID: 1))
        let store = try AuthorityStore(root: root)
        XCTAssertEqual(store.enrollment?.operatorPublicKeyX963, Vectors.operatorPublic)
        XCTAssertEqual(store.enrollment?.readerUID, geteuid())
    }

    func testEnrollRejectsAnInvalidKey() throws {
        let root = try makeRoot(enroll: false)
        XCTAssertThrowsError(try AuthorityStore.enroll(root: root, operatorPublicKeyX963: [4, 1, 2, 3], readerUID: 1))
        XCTAssertNil(try AuthorityStore(root: root).enrollment)
    }

    func testStatePersistsAcrossReopenAndAnchorTracksDigests() throws {
        let root = try makeRoot()
        var grantID = ""
        do {
            let store = try AuthorityStore(root: root)
            let authority = try store.openAuthority()
            grantID = try authority.createGrant(op.sign(CommandKind.grant, 1, grantParams())).grantID
            var p: [String: JSONValue] = ["workspace": .string("w1"), "project": .string("p1"), "chat": .string("c1")]
            p["send_id"] = .string("s1")
            _ = try authority.submitTurn(op.sign(CommandKind.submit, 2, p, payload: Array("hi".utf8)))
            let record = try store.anchor.read()
            XCTAssertEqual(record.counter, 2)
            XCTAssertEqual(record.currentDigest, sha256Bytes(try store.storage.read()!))
        }
        let store = try AuthorityStore(root: root)
        let authority = try store.openAuthority()
        XCTAssertEqual(authority.stateCounter, 2)
        let look = try authority.eligibleEvents(
            grantID: grantID, producer: "memory_extraction", limit: 5, destination: "brain", audience: "team")
        XCTAssertEqual(look.events.map(\.sendID), ["s1"])
        XCTAssertEqual(mode(root + "/state/state.blob"), 0o600)
        XCTAssertEqual(mode(root + "/anchor/counter"), 0o600)
        // No temp files are left behind.
        for dir in ["state", "anchor"] {
            XCTAssertFalse(try FileManager.default.contentsOfDirectory(atPath: root + "/" + dir).contains { $0.hasPrefix(".tmp-") })
        }
    }

    func testSecondOpenerIsRefusedWhileTheFirstHoldsTheLock() throws {
        let root = try makeRoot()
        let first = try AuthorityStore(root: root)
        XCTAssertThrowsError(try AuthorityStore(root: root))
        withExtendedLifetime(first) {}
    }

    func testDirectoryModeAndSymlinkRefused() throws {
        let root = try makeRoot()
        XCTAssertEqual(chmod(root, 0o755), 0)
        XCTAssertThrowsError(try AuthorityStore(root: root))
        XCTAssertEqual(chmod(root, 0o700), 0)
        XCTAssertEqual(chmod(root + "/state", 0o750), 0)
        XCTAssertThrowsError(try AuthorityStore(root: root))
        XCTAssertEqual(chmod(root + "/state", 0o700), 0)
        let link = root + "-link"
        XCTAssertEqual(symlink(root, link), 0)
        defer { unlink(link) }
        XCTAssertThrowsError(try AuthorityStore(root: link))
        XCTAssertNoThrow(try AuthorityStore(root: root))
    }

    func testLooseFileModeIsRefused() throws {
        let root = try makeRoot()
        XCTAssertEqual(chmod(root + "/state/state.key", 0o644), 0)
        XCTAssertThrowsError(try AuthorityStore(root: root))
    }

    func testSymlinkedKeyFileIsRefused() throws {
        let root = try makeRoot()
        let seed = root + "/state/event.seed"
        let target = root + "/elsewhere"
        XCTAssertEqual(rename(seed, target), 0)
        XCTAssertEqual(symlink(target, seed), 0)
        XCTAssertThrowsError(try AuthorityStore(root: root))
    }

    func testMissingKeyFailsClosed() throws {
        let root = try makeRoot()
        unlink(root + "/state/state.key")
        XCTAssertThrowsError(try AuthorityStore(root: root))
    }

    private func populated() throws -> (root: String, blob: [UInt8]) {
        let root = try makeRoot()
        let store = try AuthorityStore(root: root)
        let authority = try store.openAuthority()
        _ = try authority.createGrant(op.sign(CommandKind.grant, 1, grantParams()))
        let first = try store.storage.read()!
        _ = try authority.createGrant(op.sign(CommandKind.grant, 2, grantParams()))
        _ = try authority.createGrant(op.sign(CommandKind.grant, 3, grantParams()))
        return (root, first)
    }

    private func openCode(_ root: String) -> String? {
        do { _ = try AuthorityStore(root: root).openAuthority(); return nil } catch let e as AuthorityError { return e.code } catch { return "other:\(error)" }
    }

    func testTamperedBlobFailsClosed() throws {
        let (root, _) = try populated()
        let path = root + "/state/state.blob"
        var data = try Data(contentsOf: URL(fileURLWithPath: path))
        data[data.count / 2] ^= 1
        try data.write(to: URL(fileURLWithPath: path))
        XCTAssertEqual(openCode(root), "state_invalid")
    }

    func testDeletedBlobFailsClosed() throws {
        let (root, _) = try populated()
        unlink(root + "/state/state.blob")
        XCTAssertEqual(openCode(root), "state_missing")
    }

    func testDeletedAnchorFailsClosed() throws {
        let (root, _) = try populated()
        unlink(root + "/anchor/counter")
        XCTAssertEqual(openCode(root), "anchor_missing")
    }

    func testCorruptAnchorFailsClosed() throws {
        let (root, _) = try populated()
        try Data("{\"counter\":9}".utf8).write(to: URL(fileURLWithPath: root + "/anchor/counter"))
        chmod(root + "/anchor/counter", 0o600)
        XCTAssertEqual(openCode(root), "anchor_invalid")
    }

    func testOlderBlobFailsClosedAndNothingIsRepaired() throws {
        let (root, old) = try populated()  // anchor at 3, old blob is counter 1
        try Data(old).write(to: URL(fileURLWithPath: root + "/state/state.blob"))
        XCTAssertEqual(openCode(root), "state_rollback")
        let anchor = try AuthorityStore(root: root).anchor.read()
        XCTAssertEqual(anchor.counter, 3)  // untouched
        XCTAssertEqual(try Data(contentsOf: URL(fileURLWithPath: root + "/state/state.blob")), Data(old))
    }

    func testAnchorAdvanceRecordsTheDigestOfTheBlobItPassed() throws {
        let (root, _) = try populated()
        let store = try AuthorityStore(root: root)
        // The crash window: the anchor advances with a digest, the blob is never written.
        let blob2 = try store.storage.read()!
        try store.anchor.advance(to: 4, blobSHA256: [UInt8](repeating: 9, count: 32))
        XCTAssertEqual(try store.storage.read(), blob2)
        XCTAssertEqual(try store.anchor.read().previousDigest, sha256Bytes(blob2))
    }
}
