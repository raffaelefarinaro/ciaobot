import Foundation

public let protocolVersion: Int64 = 1
public let helperVersion = "0.1.0-slice2"
public let maxRequestBytes = 64 * 1024
public let maxReplyBytes = 8 * 1024 * 1024

/// The read side of the wire, matching `ciao/chat_authority_client.py`: one
/// request, one reply, each a JSON object `{"v":1,"op":...,"nonce":<32 hex>,...}`.
/// Only `health` and `eligible_events` exist. Commands (grant, submit, revoke,
/// withdraw) are never accepted here: they arrive from the Mac app over XPC in a
/// later slice; this slice has only the offline `apply` harness in the daemon.
public final class AuthorityHost {
    public enum Mode {
        case ready(ChatAuthority)
        /// The authority could not open; `reason` is the stable code. Nothing is repaired.
        case closed(reason: String)
    }

    private let mode: Mode

    public init(_ mode: Mode) { self.mode = mode }

    /// nil means the request is malformed beyond answering (no usable nonce): drop the connection.
    public func handle(_ request: [UInt8]) -> [UInt8]? {
        guard request.count <= maxRequestBytes, let parsed = try? JSON.parse(request), let req = parsed.objectValue,
            let nonce = req["nonce"]?.stringValue, Self.isNonce(nonce), let op = req["op"]?.stringValue
        else { return nil }
        func reply(_ fields: [String: JSONValue]) -> [UInt8] {
            var out: [String: JSONValue] = ["v": .int(protocolVersion), "nonce": .string(nonce)]
            out.merge(fields) { _, new in new }
            let bytes = JSON.canonical(.object(out))
            return bytes.count > maxReplyBytes ? failure("reply_too_large") : bytes
        }
        func failure(_ code: String) -> [UInt8] {
            JSON.canonical(.object(["v": .int(protocolVersion), "nonce": .string(nonce), "ok": .bool(false), "code": .string(code)]))
        }
        guard req["v"]?.intValue == protocolVersion else { return failure("bad_version") }
        switch op {
        case "health":
            return reply(["ok": .bool(true), "result": health()])
        case "eligible_events":
            guard case .ready(let authority) = mode else { return failure("closed") }
            return eligible(authority, req, reply: reply, failure: failure)
        default:
            return failure("unknown_op")
        }
    }

    static func isNonce(_ s: String) -> Bool {
        s.utf8.count == 32 && s.utf8.allSatisfy { (0x30...0x39).contains($0) || (0x61...0x66).contains($0) }
    }

    private func health() -> JSONValue {
        switch mode {
        case .ready(let a):
            return .object([
                "ready": .bool(true), "helper_version": .string(helperVersion), "state_counter": .int(a.stateCounter),
                "epoch": .int(a.epoch), "authority_key_id": .string(a.authorityKeyID),
                "event_public_key": .string(Hex.encode(a.eventPublicKey)),
                "recovered_crash_window": .bool(a.recoveredCrashWindow),
            ])
        case .closed(let reason):
            return .object([
                "ready": .bool(false), "helper_version": .string(helperVersion), "state_counter": .int(0),
                "epoch": .int(0), "authority_key_id": .string(""), "code": .string(reason),
            ])
        }
    }

    private func eligible(
        _ authority: ChatAuthority, _ req: [String: JSONValue], reply: ([String: JSONValue]) -> [UInt8],
        failure: (String) -> [UInt8]
    ) -> [UInt8] {
        guard let grantID = req["grant_id"]?.stringValue, let producer = req["producer"]?.stringValue,
            let destination = req["destination"]?.stringValue, let audience = req["audience"]?.stringValue,
            let limit = req["limit"]?.intValue, (1...Int64(maxLookup)).contains(limit)
        else { return failure("bad_request") }
        var chat: String?
        if let raw = req["chat"], !raw.isNull {
            guard let s = raw.stringValue else { return failure("bad_request") }
            chat = s
        }
        var expected: Int64?
        if let raw = req["expected_epoch"], !raw.isNull {
            guard let i = raw.intValue else { return failure("bad_request") }
            expected = i
        }
        do {
            let look = try authority.eligibleEvents(
                grantID: grantID, producer: producer, limit: Int(limit), destination: destination, audience: audience,
                chat: chat, expectedEpoch: expected)
            return reply([
                "ok": .bool(true),
                "result": .object([
                    "epoch": .int(look.epoch), "grant_id": .string(look.grantID),
                    "destination": .string(look.destination), "audience": .string(look.audience),
                    "events": .array(look.events.map(Self.eventJSON)),
                ]),
            ])
        } catch let e as AuthorityError {
            return failure(e.code)
        } catch {
            return failure("internal")
        }
    }

    static func eventJSON(_ e: TurnEvent) -> JSONValue {
        .object([
            "event_id": .string(e.eventID), "turn_id": .string(e.turnID), "revision": .int(e.revision),
            "seq": .int(e.seq), "send_id": .string(e.sendID), "workspace": .string(e.workspace),
            "project": .string(e.project), "chat": .string(e.chat), "principal": .string(e.principal),
            "text_b64": .string(Data(e.text).base64EncodedString()), "text_sha256": .string(e.textSHA256),
            "opted_out": .bool(e.optedOut), "grant_ids": .array(e.grantIDs.map { .string($0) }),
            "epoch": .int(e.epoch), "signature_b64": .string(Data(e.signature).base64EncodedString()),
        ])
    }
}
