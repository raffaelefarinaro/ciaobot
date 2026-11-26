import Foundation

extension SignedCommand {
    /// The command shape of the shared vectors and of the daemon's offline
    /// `apply` harness: kind, counter, principal, params, payload_hex, key_id,
    /// signature_hex (a `signing_bytes_hex` field is ignored; the bytes are
    /// always recomputed).
    public init(json: JSONValue) throws {
        guard let o = json.objectValue, let kind = o["kind"]?.stringValue, let counter = o["counter"]?.intValue,
            let principal = o["principal"]?.stringValue, let params = o["params"]?.objectValue,
            let payloadHex = o["payload_hex"]?.stringValue, let payload = Hex.decode(payloadHex),
            let keyID = o["key_id"]?.stringValue, let sigHex = o["signature_hex"]?.stringValue,
            let signature = Hex.decode(sigHex)
        else { throw AuthorityError("bad_command") }
        self.init(
            command: Command(kind: kind, counter: counter, principal: principal, params: params, payload: payload, keyID: keyID),
            signature: signature)
    }
}

extension ChatAuthority {
    /// Run one operator command and return its outcome in the vectors' `expect` shape.
    public func run(op: String, _ signed: SignedCommand) -> JSONValue {
        do {
            switch op {
            case CommandKind.grant:
                return .object(["grant_id": .string(try createGrant(signed).grantID)])
            case CommandKind.submit:
                let e = try submitTurn(signed)
                return .object([
                    "event_id": .string(e.eventID), "turn_id": .string(e.turnID), "revision": .int(e.revision),
                    "seq": .int(e.seq), "grant_ids": .array(e.grantIDs.map { .string($0) }),
                    "event_signature_hex": .string(Hex.encode(e.signature)),
                ])
            case CommandKind.revoke: return .object(["epoch": .int(try revokeGrant(signed))])
            case CommandKind.withdrawGrant: return .object(["epoch": .int(try withdrawGrant(signed))])
            case CommandKind.withdrawTurn: return .object(["epoch": .int(try withdrawTurn(signed))])
            default: return .object(["error": .string("bad_command")])
            }
        } catch let e as AuthorityError {
            return .object(["error": .string(e.code)])
        } catch {
            return .object(["error": .string("internal")])
        }
    }
}
