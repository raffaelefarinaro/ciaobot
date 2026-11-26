import CryptoKit
import XCTest

@testable import CiaobotAuthority

/// The main acceptance test: the Swift port reproduces every value in the
/// vectors the Python reference generated (canonical bytes, signatures,
/// command outcomes, anchor values, final state digest and MAC, load rules).
final class VectorTests: XCTestCase {
    func testVectorsFileIsTheCheckedInFormat() {
        XCTAssertEqual(Vectors.doc["format"], .int(1))
        XCTAssertEqual(Vectors.doc["max_counter"], .int(maxCounter))
    }

    func testKeysMatch() throws {
        let verifier = try OperatorVerifier(publicKeyX963: Vectors.operatorPublic)
        XCTAssertEqual(verifier.keyID, Vectors.string(Vectors.keys, "operator_key_id"))
        let signer = try EventSigner(seed: Vectors.eventSeed)
        XCTAssertEqual(Hex.encode(signer.publicKey), Vectors.string(Vectors.keys, "event_public_hex"))
        // The test-only operator signer derives the same public key from the scalar.
        XCTAssertEqual(OperatorTestSigner().keyID, verifier.keyID)
    }

    func testCommandSigningBytesAndSignatures() throws {
        let verifier = try OperatorVerifier(publicKeyX963: Vectors.operatorPublic)
        let commands = Vectors.object(Vectors.doc["signing"])["commands"]!.arrayValue!
        XCTAssertGreaterThanOrEqual(commands.count, 5)
        for raw in commands {
            let o = raw.objectValue!
            let signed = try SignedCommand(json: raw)
            XCTAssertEqual(Hex.encode(signed.command.signingBytes()), Vectors.string(o, "signing_bytes_hex"), "\(o["kind"]!)")
            XCTAssertTrue(verifier.verify(signed.command.signingBytes(), signature: signed.signature))
            // A flipped bit in the signed bytes or the signature must not verify.
            var bad = signed.signature
            bad[10] ^= 1
            XCTAssertFalse(verifier.verify(signed.command.signingBytes(), signature: bad))
            var tampered = signed.command
            tampered.counter += 1
            XCTAssertFalse(verifier.verify(tampered.signingBytes(), signature: signed.signature))
        }
    }

    func testEventSigningBytesAndDeterministicSignature() throws {
        let o = Vectors.object(Vectors.object(Vectors.doc["signing"])["event"])
        let f = Vectors.object(o["fields"])
        let text = Vectors.hex(f, "text_hex")
        var event = TurnEvent(
            eventID: Vectors.string(f, "event_id"), turnID: Vectors.string(f, "turn_id"), revision: f["revision"]!.intValue!,
            seq: f["seq"]!.intValue!, sendID: Vectors.string(f, "send_id"), workspace: Vectors.string(f, "workspace"),
            project: Vectors.string(f, "project"), chat: Vectors.string(f, "chat"), principal: Vectors.string(f, "principal"),
            text: text, textSHA256: sha256Hex(text), optedOut: f["opted_out"]!.boolValue!,
            grantIDs: f["grant_ids"]!.arrayValue!.map { $0.stringValue! }, epoch: f["epoch"]!.intValue!, signature: [])
        XCTAssertEqual(Hex.encode(event.signingBytes()), Vectors.string(o, "signing_bytes_hex"))
        let signer = try EventSigner(seed: Vectors.eventSeed)
        event.signature = signer.sign(event.signingBytes())
        XCTAssertEqual(Hex.encode(event.signature), Vectors.string(o, "signature_hex"))
        XCTAssertTrue(signer.verify(event.signingBytes(), signature: event.signature))
    }

    func testStateMACVector() throws {
        let o = Vectors.object(Vectors.object(Vectors.doc["signing"])["state_mac"])
        let body = AuthState().toJSON(counter: 1)
        XCTAssertEqual(String(decoding: JSON.canonical(body), as: UTF8.self), Vectors.string(o, "body_canonical"))
        let mac = try StateMAC(key: Vectors.stateKey)
        XCTAssertEqual(Hex.encode(mac.tag(stateContext + JSON.canonical(body))), Vectors.string(o, "mac_hex"))
    }

    func testSequencesReplayIdenticallyToPython() throws {
        let sequences = Vectors.doc["sequences"]!.arrayValue!
        XCTAssertGreaterThanOrEqual(sequences.count, 3)
        for raw in sequences {
            let seq = raw.objectValue!
            let name = Vectors.string(seq, "name")
            let ids = seq["ids"]!.arrayValue!.map { $0.stringValue! }
            let storage = MemoryStorage()
            let anchor = MemoryAnchor()
            let authority = try makeAuthority(ids: ids, storage: storage, anchor: anchor)
            for (n, stepRaw) in seq["steps"]!.arrayValue!.enumerated() {
                let step = stepRaw.objectValue!
                let label = "\(name) step \(n)"
                let op = Vectors.string(step, "op")
                if op == "eligible_events" {
                    XCTAssertEqual(lookup(authority, Vectors.object(step["args"])), step["expect"], label)
                    continue
                }
                let signed = try SignedCommand(json: step["command"]!)
                XCTAssertEqual(authority.run(op: op, signed), step["expect"], label)
                XCTAssertEqual(anchor.record?.counter, step["anchor_after"]?.intValue, label)
            }
            let final = Vectors.object(seq["final"])
            XCTAssertEqual(authority.stateCounter, final["state_counter"]?.intValue, name)
            let blob = try JSON.parse(storage.blob!).objectValue!
            XCTAssertEqual(sha256Hex(JSON.canonical(blob["state"]!)), Vectors.string(final, "state_canonical_sha256"), name)
            XCTAssertEqual(
                Hex.encode(Array(Data(base64Encoded: blob["mac_b64"]!.stringValue!)!)), Vectors.string(final, "state_mac_hex"), name)
            // The anchor's digest is the SHA-256 of the exact blob on disk.
            XCTAssertEqual(anchor.record?.currentDigest, sha256Bytes(storage.blob!), name)
            // A restart from the persisted blob opens and keeps the counter.
            let reopened = try makeAuthority(ids: [], storage: storage, anchor: anchor)
            XCTAssertEqual(reopened.stateCounter, final["state_counter"]?.intValue, name)
            XCTAssertEqual(reopened.epoch, authority.epoch, name)
        }
    }

    func testLoadRules() throws {
        let rules = Vectors.doc["load_rules"]!.arrayValue!
        XCTAssertGreaterThanOrEqual(rules.count, 10)
        for raw in rules {
            let rule = raw.objectValue!
            let name = Vectors.string(rule, "name")
            let blob: [UInt8]? = rule["blob"]?.stringValue.map { Array($0.utf8) }
            let counter = rule["anchor"]!.intValue!
            // An honest anchor: its digests describe the blob it advanced past.
            var record = AnchorRecord(counter: counter)
            if let blob {
                let digest = sha256Bytes(blob)
                let blobCounter = (try? JSON.parse(blob))?.objectValue?["state"]?.objectValue?["state_counter"]?.intValue
                if blobCounter == counter - 1 { record.previousDigest = digest } else { record.currentDigest = digest }
            }
            var code = "ok"
            do {
                _ = try makeAuthority(ids: [], storage: MemoryStorage(blob), anchor: MemoryAnchor(record))
            } catch let e as AuthorityError { code = e.code }
            XCTAssertEqual(code, Vectors.string(rule, "expect"), name)
        }
    }

    private func lookup(_ authority: ChatAuthority, _ a: [String: JSONValue]) -> JSONValue {
        do {
            let look = try authority.eligibleEvents(
                grantID: Vectors.string(a, "grant_id"), producer: Vectors.string(a, "producer"), limit: Int(a["limit"]!.intValue!),
                destination: Vectors.string(a, "destination"), audience: Vectors.string(a, "audience"),
                chat: a["chat"]?.stringValue, expectedEpoch: a["expected_epoch"]?.intValue)
            return .object(["epoch": .int(look.epoch), "send_ids": .array(look.events.map { .string($0.sendID) })])
        } catch let e as AuthorityError {
            return .object(["error": .string(e.code)])
        } catch { return .object(["error": .string("internal")]) }
    }
}
