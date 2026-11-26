import XCTest

@testable import CiaobotAuthority

/// The counter anchor and its digest design: rollback, missing blob, crash
/// windows, and "reads only after the state was rewritten".
final class AnchorTests: XCTestCase {
    let op = OperatorTestSigner()
    var params: [String: JSONValue] {
        [
            "workspace": .string("w1"), "project": .string("p1"), "chat": .string("c1"), "destination": .string("brain"),
            "audience": .string("team"), "producers": .array([.string("memory_extraction")]),
        ]
    }
    let scope: [String: JSONValue] = ["workspace": .string("w1"), "project": .string("p1"), "chat": .string("c1")]

    /// grant (counter 1) then n submits; returns the authority.
    private func history(_ storage: MemoryStorage, _ anchor: MemoryAnchor, submits: Int, prefix: String = "id") throws -> ChatAuthority {
        let authority = try makeAuthority(ids: (0..<40).map { "\(prefix)\($0)" }, storage: storage, anchor: anchor)
        _ = try authority.createGrant(op.sign(CommandKind.grant, 1, params))
        for n in 0..<submits {
            var p = scope
            p["send_id"] = .string("s\(n)")
            _ = try authority.submitTurn(op.sign(CommandKind.submit, Int64(n + 2), p, payload: Array("m\(n)".utf8)))
        }
        return authority
    }

    func testFreshStartNeedsAnchorAtZero() throws {
        let anchor = MemoryAnchor()
        let authority = try makeAuthority(ids: [], anchor: anchor)
        XCTAssertEqual(authority.stateCounter, 0)
        XCTAssertFalse(authority.recoveredCrashWindow)
    }

    func testMissingAnchorFailsClosed() {
        XCTAssertThrowsError(try makeAuthority(ids: [], anchor: MemoryAnchor(nil))) {
            XCTAssertEqual(($0 as? AuthorityError)?.code, "anchor_missing")
        }
    }

    func testMissingBlobWithAnchorAboveZeroFailsClosed() throws {
        let anchor = MemoryAnchor()
        _ = try history(MemoryStorage(), anchor, submits: 1)
        XCTAssertThrowsError(try makeAuthority(ids: [], storage: MemoryStorage(nil), anchor: anchor)) {
            XCTAssertEqual(($0 as? AuthorityError)?.code, "state_missing")
        }
    }

    func testBlobWithAnchorZeroIsInvalid() throws {
        let storage = MemoryStorage()
        _ = try history(storage, MemoryAnchor(), submits: 1)
        XCTAssertThrowsError(try makeAuthority(ids: [], storage: storage, anchor: MemoryAnchor())) {
            XCTAssertEqual(($0 as? AuthorityError)?.code, "state_invalid")
        }
    }

    func testRestoringAnOlderBlobIsARollback() throws {
        let storage = MemoryStorage()
        let anchor = MemoryAnchor()
        _ = try history(storage, anchor, submits: 1)
        let old = storage.blob  // counter 2
        let authority = try makeAuthority(ids: ["x1", "x2", "x3"], storage: storage, anchor: anchor)
        var p = scope
        p["send_id"] = .string("later")
        _ = try authority.submitTurn(op.sign(CommandKind.submit, 3, p, payload: Array("later".utf8)))
        _ = try authority.submitTurn(op.sign(CommandKind.submit, 4, p.merging(["send_id": .string("l2")]) { $1 }, payload: Array("l2".utf8)))
        storage.blob = old  // anchor is 4, blob is 2: two behind
        XCTAssertThrowsError(try makeAuthority(ids: [], storage: storage, anchor: anchor)) {
            XCTAssertEqual(($0 as? AuthorityError)?.code, "state_rollback")
        }
    }

    func testCrashBetweenAnchorAndWriteIsRecoveredByRewritingBeforeAnyRead() throws {
        let storage = MemoryStorage()
        let anchor = MemoryAnchor()
        let authority = try history(storage, anchor, submits: 1)
        let gid = authority.eligibleEventsGrantID
        XCTAssertEqual(anchor.record?.counter, 2)
        // The next command advances the anchor, then the blob write "crashes".
        storage.failWrites = true
        XCTAssertThrowsError(try authority.revokeGrant(op.sign(CommandKind.revoke, 3, ["grant_id": .string(gid)]))) {
            XCTAssertEqual(($0 as? AuthorityError)?.code, "storage_failed")
        }
        XCTAssertEqual(anchor.record?.counter, 3)
        XCTAssertEqual(try JSON.parse(storage.blob!).objectValue?["state"]?.objectValue?["state_counter"], .int(2))
        storage.failWrites = false

        let reopened = try makeAuthority(ids: [], storage: storage, anchor: anchor)
        XCTAssertTrue(reopened.recoveredCrashWindow)
        // Rewritten under a new counter before the instance existed: anchor 4, blob 4.
        XCTAssertEqual(anchor.record?.counter, 4)
        XCTAssertEqual(reopened.stateCounter, 4)
        XCTAssertEqual(try JSON.parse(storage.blob!).objectValue?["state"]?.objectValue?["state_counter"], .int(4))
        XCTAssertEqual(anchor.record?.currentDigest, sha256Bytes(storage.blob!))
        // The unwritten revoke is lost: the grant is still active and eligible.
        XCTAssertEqual(reopened.grant(gid)?.status, GrantStatus.active)
        XCTAssertEqual(reopened.epoch, 0)
        // And a normal restart afterwards is plain equal-counter.
        XCTAssertFalse(try makeAuthority(ids: [], storage: storage, anchor: anchor).recoveredCrashWindow)
    }

    func testFailedRecoveryRewriteKeepsTheDaemonClosed() throws {
        let storage = MemoryStorage()
        let anchor = MemoryAnchor()
        let authority = try history(storage, anchor, submits: 1)
        storage.failWrites = true
        _ = try? authority.revokeGrant(op.sign(CommandKind.revoke, 3, ["grant_id": .string(authority.eligibleEventsGrantID)]))
        storage.failWrites = false
        anchor.failAdvance = true
        XCTAssertThrowsError(try makeAuthority(ids: [], storage: storage, anchor: anchor)) {
            XCTAssertEqual(($0 as? AuthorityError)?.code, "anchor_failed")
        }
        anchor.failAdvance = false
        storage.failWrites = true
        XCTAssertThrowsError(try makeAuthority(ids: [], storage: storage, anchor: anchor)) {
            XCTAssertEqual(($0 as? AuthorityError)?.code, "storage_failed")
        }
    }

    func testOneBehindBlobThatIsNotThePreviousDigestIsRefused() throws {
        // A genuine, MAC-valid blob of counter n-1 from a different history.
        let storageA = MemoryStorage(), anchorA = MemoryAnchor()
        _ = try history(storageA, anchorA, submits: 1, prefix: "other")  // counter 2
        let storageB = MemoryStorage(), anchorB = MemoryAnchor()
        let b = try history(storageB, anchorB, submits: 2)  // counter 3
        XCTAssertEqual(b.stateCounter, 3)
        storageB.blob = storageA.blob  // counter 2, valid MAC, but not the blob the anchor saw
        XCTAssertThrowsError(try makeAuthority(ids: [], storage: storageB, anchor: anchorB)) {
            XCTAssertEqual(($0 as? AuthorityError)?.code, "state_rollback")
        }
    }

    func testEqualCounterBlobWithWrongDigestIsInvalid() throws {
        let storage = MemoryStorage(), anchor = MemoryAnchor()
        _ = try history(storage, anchor, submits: 1)
        anchor.record?.currentDigest = [UInt8](repeating: 7, count: 32)
        XCTAssertThrowsError(try makeAuthority(ids: [], storage: storage, anchor: anchor)) {
            XCTAssertEqual(($0 as? AuthorityError)?.code, "state_invalid")
        }
    }

    func testRestoringTheExactPreviousBlobOnceIsInherentButIsFlaggedAndCannotRepeat() throws {
        // Documented residual (docs/CHAT_AUTHORITY_HELPER.md): a one-behind blob that
        // matches the previous digest is indistinguishable from a crash window.
        // The daemon accepts it once, flags it, and moves the counter past it.
        let storage = MemoryStorage(), anchor = MemoryAnchor()
        let authority = try history(storage, anchor, submits: 1)
        let previous = storage.blob  // counter 2
        let gid = authority.eligibleEventsGrantID
        _ = try authority.revokeGrant(op.sign(CommandKind.revoke, 3, ["grant_id": .string(gid)]))  // counter 3
        storage.blob = previous
        let restored = try makeAuthority(ids: [], storage: storage, anchor: anchor)
        XCTAssertTrue(restored.recoveredCrashWindow, "health reports recovered_crash_window")
        XCTAssertEqual(restored.grant(gid)?.status, GrantStatus.active)  // the revoke was undone
        storage.blob = previous  // restoring it again is now two behind
        XCTAssertThrowsError(try makeAuthority(ids: [], storage: storage, anchor: anchor)) {
            XCTAssertEqual(($0 as? AuthorityError)?.code, "state_rollback")
        }
    }

    func testTwoFailedWritesInARowLeaveTheAnchorTwoAheadAndFailClosed() throws {
        let storage = MemoryStorage(), anchor = MemoryAnchor()
        let authority = try history(storage, anchor, submits: 1)
        let gid = authority.eligibleEventsGrantID
        storage.failWrites = true
        _ = try? authority.revokeGrant(op.sign(CommandKind.revoke, 3, ["grant_id": .string(gid)]))
        _ = try? authority.revokeGrant(op.sign(CommandKind.revoke, 4, ["grant_id": .string(gid)]))
        storage.failWrites = false
        XCTAssertEqual(anchor.record?.counter, 4)
        XCTAssertThrowsError(try makeAuthority(ids: [], storage: storage, anchor: anchor)) {
            XCTAssertEqual(($0 as? AuthorityError)?.code, "state_rollback")
        }
    }

    func testFailedAnchorAdvanceWritesNothingAndDoesNotConsumeTheCounter() throws {
        let storage = MemoryStorage(), anchor = MemoryAnchor()
        let authority = try history(storage, anchor, submits: 0)
        let before = storage.blob
        anchor.failAdvance = true
        let revoke = op.sign(CommandKind.revoke, 2, ["grant_id": .string(authority.eligibleEventsGrantID)])
        XCTAssertThrowsError(try authority.revokeGrant(revoke)) {
            XCTAssertEqual(($0 as? AuthorityError)?.code, "anchor_failed")
        }
        XCTAssertEqual(storage.blob, before)
        XCTAssertEqual(authority.epoch, 0)
        anchor.failAdvance = false
        XCTAssertEqual(try authority.revokeGrant(revoke), 1)  // same signed command, same counter: not consumed
    }

    func testAnchorMovesOnlyForwardByOne() {
        let anchor = MemoryAnchor()
        XCTAssertThrowsError(try anchor.advance(to: 2, blobSHA256: [UInt8](repeating: 0, count: 32)))
        XCTAssertNoThrow(try anchor.advance(to: 1, blobSHA256: [UInt8](repeating: 1, count: 32)))
        XCTAssertThrowsError(try anchor.advance(to: 1, blobSHA256: [UInt8](repeating: 2, count: 32)))
    }

    func testStateAndEventKeysMustDiffer() throws {
        XCTAssertThrowsError(
            try ChatAuthority(
                eventSigner: EventSigner(seed: Vectors.eventSeed), stateMAC: StateMAC(key: Vectors.eventSeed),
                operatorKey: OperatorVerifier(publicKeyX963: Vectors.operatorPublic), storage: MemoryStorage(),
                anchor: MemoryAnchor(), newID: { "x" }))
    }

    func testACommandSignedForAnotherKeyIsRefused() throws {
        let authority = try makeAuthority(ids: ["g1"])
        let other = OperatorTestSigner(scalarHex: String(repeating: "0", count: 63) + "7")
        XCTAssertThrowsError(try authority.createGrant(other.sign(CommandKind.grant, 1, params))) {
            XCTAssertEqual(($0 as? AuthorityError)?.code, "bad_signature")
        }
        // Not consumed: the real key can still use counter 1.
        XCTAssertNoThrow(try authority.createGrant(op.sign(CommandKind.grant, 1, params)))
    }
}

extension ChatAuthority {
    /// The only grant a test history created.
    var eligibleEventsGrantID: String { "id0" }
}
