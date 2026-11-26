import Foundation

public let stateVersion = 1
public let maxTextBytes = 64 * 1024
public let maxEvents = 2000
public let maxTotalTextBytes = 4 * 1024 * 1024
public let maxGrants = 500  // live (active) grants
public let maxGrantRecords = 1000  // live plus ended grants that stored events still name
public let maxCounter: Int64 = (1 << 53) - 1
public let maxPrincipals = 64
public let maxLookup = 200

let commandContext = Array("ciao-chat-authority/command/v1\0".utf8)
let eventContext = Array("ciao-chat-authority/event/v1\0".utf8)
let stateContext = Array("ciao-chat-authority/state/v1\0".utf8)

public enum CommandKind {
    public static let submit = "submit_turn"
    public static let grant = "create_grant"
    public static let revoke = "revoke_grant"
    public static let withdrawGrant = "withdraw_grant"
    public static let withdrawTurn = "withdraw_turn"
}

public enum GrantStatus {
    public static let active = "active"
    public static let revoked = "revoked"
    public static let withdrawn = "withdrawn"
}

/// A refused operation; `code` is stable and machine-readable (same codes as the Python reference).
public struct AuthorityError: Error, Equatable, CustomStringConvertible {
    public let code: String
    public init(_ code: String) { self.code = code }
    public var description: String { code }
}

public struct Command: Equatable {
    public var kind: String
    public var counter: Int64
    public var principal: String
    public var params: [String: JSONValue]
    public var payload: [UInt8]
    public var keyID: String

    public init(
        kind: String, counter: Int64, principal: String, params: [String: JSONValue],
        payload: [UInt8] = [], keyID: String
    ) {
        self.kind = kind
        self.counter = counter
        self.principal = principal
        self.params = params
        self.payload = payload
        self.keyID = keyID
    }

    public func signingBytes() -> [UInt8] {
        commandContext
            + JSON.canonical(
                .object([
                    "key_id": .string(keyID),
                    "kind": .string(kind),
                    "counter": .int(counter),
                    "principal": .string(principal),
                    "params": .object(params),
                    "payload_sha256": .string(sha256Hex(payload)),
                ]))
    }
}

public struct SignedCommand: Equatable {
    public var command: Command
    public var signature: [UInt8]
    public init(command: Command, signature: [UInt8]) {
        self.command = command
        self.signature = signature
    }
}

public struct Grant: Equatable {
    public var grantID: String
    public var workspace: String
    public var project: String
    public var chat: String?
    public var destination: String
    public var audience: String
    public var producers: [String]
    public var principal: String
    public var status: String
    public var createdEpoch: Int64
    public var endedEpoch: Int64?
}

public struct TurnEvent: Equatable {
    public var eventID: String
    public var turnID: String
    public var revision: Int64
    public var seq: Int64
    public var sendID: String
    public var workspace: String
    public var project: String
    public var chat: String
    public var principal: String
    public var text: [UInt8]
    public var textSHA256: String
    public var optedOut: Bool
    public var grantIDs: [String]
    public var epoch: Int64
    public var signature: [UInt8]

    public func signingBytes() -> [UInt8] {
        eventContext
            + JSON.canonical(
                .object([
                    "event_id": .string(eventID),
                    "turn_id": .string(turnID),
                    "revision": .int(revision),
                    "seq": .int(seq),
                    "send_id": .string(sendID),
                    "workspace": .string(workspace),
                    "project": .string(project),
                    "chat": .string(chat),
                    "principal": .string(principal),
                    "text_sha256": .string(textSHA256),
                    "opted_out": .bool(optedOut),
                    "grant_ids": .array(grantIDs.map { .string($0) }),
                    "epoch": .int(epoch),
                ]))
    }
}

public struct Lookup: Equatable {
    public var epoch: Int64
    public var grantID: String
    public var destination: String
    public var audience: String
    public var events: [TurnEvent]
}

/// Mutable protocol state. Grants keep insertion order (it is part of the
/// persisted bytes); events are append-only and `bySend`/`turns` index into them.
struct AuthState {
    var epoch: Int64 = 0
    var seq: Int64 = 0
    var grants: [Grant] = []
    var events: [TurnEvent] = []
    var bySend: [String: Int] = [:]
    var turns: [String: Int] = [:]  // latest revision
    var usedIDs: Set<String> = []
    var withdrawnTurns: Set<String> = []
    var counters: [String: Int64] = [:]
    var textBytes = 0

    func grantIndex(_ id: String) -> Int? { grants.firstIndex { $0.grantID == id } }

    mutating func addEvent(_ event: TurnEvent) {
        events.append(event)
        let index = events.count - 1
        bySend[event.sendID] = index
        turns[event.turnID] = index
        usedIDs.insert(event.eventID)
        usedIDs.insert(event.turnID)
        textBytes += event.text.count
    }

    mutating func pruneEndedGrants() {
        var named: Set<String> = []
        for e in events { named.formUnion(e.grantIDs) }
        grants.removeAll { $0.status != GrantStatus.active && !named.contains($0.grantID) }
    }
}

private func sortedScalars(_ items: some Sequence<String>) -> [String] { JSON.sortedKeys(items) }

private func b64(_ bytes: [UInt8]) -> String { Data(bytes).base64EncodedString() }

extension AuthState {
    func toJSON(counter: Int64) -> JSONValue {
        func opt(_ s: String?) -> JSONValue { s.map { .string($0) } ?? .null }
        func intOpt(_ i: Int64?) -> JSONValue { i.map { .int($0) } ?? .null }
        return .object([
            "version": .int(Int64(stateVersion)),
            "state_counter": .int(counter),
            "epoch": .int(epoch),
            "seq": .int(seq),
            "grants": .array(
                grants.map { g in
                    .object([
                        "grant_id": .string(g.grantID),
                        "workspace": .string(g.workspace),
                        "project": .string(g.project),
                        "chat": opt(g.chat),
                        "destination": .string(g.destination),
                        "audience": .string(g.audience),
                        "producers": .array(g.producers.map { .string($0) }),
                        "principal": .string(g.principal),
                        "status": .string(g.status),
                        "created_epoch": .int(g.createdEpoch),
                        "ended_epoch": intOpt(g.endedEpoch),
                    ])
                }),
            "events": .array(
                events.map { e in
                    .object([
                        "event_id": .string(e.eventID),
                        "turn_id": .string(e.turnID),
                        "revision": .int(e.revision),
                        "seq": .int(e.seq),
                        "send_id": .string(e.sendID),
                        "workspace": .string(e.workspace),
                        "project": .string(e.project),
                        "chat": .string(e.chat),
                        "principal": .string(e.principal),
                        "text_b64": .string(b64(e.text)),
                        "opted_out": .bool(e.optedOut),
                        "grant_ids": .array(e.grantIDs.map { .string($0) }),
                        "epoch": .int(e.epoch),
                        "signature_b64": .string(b64(e.signature)),
                    ])
                }),
            "withdrawn_turns": .array(sortedScalars(withdrawnTurns).map { .string($0) }),
            "counters": .object(counters.mapValues { .int($0) }),
        ])
    }

    /// Strict: any missing key or wrong type is an error (the caller maps it to `state_invalid`).
    static func from(json raw: JSONValue) throws -> AuthState {
        struct Bad: Error {}
        func obj(_ v: JSONValue?) throws -> [String: JSONValue] { guard let o = v?.objectValue else { throw Bad() }; return o }
        func arr(_ v: JSONValue?) throws -> [JSONValue] { guard let a = v?.arrayValue else { throw Bad() }; return a }
        func str(_ o: [String: JSONValue], _ k: String) throws -> String { guard let s = o[k]?.stringValue else { throw Bad() }; return s }
        func int(_ o: [String: JSONValue], _ k: String) throws -> Int64 { guard let i = o[k]?.intValue else { throw Bad() }; return i }
        func optStr(_ o: [String: JSONValue], _ k: String) throws -> String? {
            guard let v = o[k] else { throw Bad() }
            if v.isNull { return nil }
            guard let s = v.stringValue else { throw Bad() }
            return s
        }
        func strList(_ o: [String: JSONValue], _ k: String) throws -> [String] {
            try arr(o[k]).map { guard let s = $0.stringValue else { throw Bad() }; return s }
        }
        func bytes(_ o: [String: JSONValue], _ k: String) throws -> [UInt8] {
            guard let d = Data(base64Encoded: try str(o, k)), b64(Array(d)) == (try str(o, k)) else { throw Bad() }
            return Array(d)
        }

        let root = try obj(raw)
        guard try int(root, "version") == Int64(stateVersion) else { throw Bad() }
        var state = AuthState()
        state.epoch = try int(root, "epoch")
        state.seq = try int(root, "seq")
        for item in try arr(root["grants"]) {
            let g = try obj(item)
            var ended: Int64?
            if let e = g["ended_epoch"], !e.isNull {
                guard let i = e.intValue else { throw Bad() }
                ended = i
            } else if g["ended_epoch"] == nil { throw Bad() }
            let status = try str(g, "status")
            guard [GrantStatus.active, GrantStatus.revoked, GrantStatus.withdrawn].contains(status) else { throw Bad() }
            let grant = Grant(
                grantID: try str(g, "grant_id"), workspace: try str(g, "workspace"),
                project: try str(g, "project"), chat: try optStr(g, "chat"),
                destination: try str(g, "destination"), audience: try str(g, "audience"),
                producers: try strList(g, "producers"), principal: try str(g, "principal"),
                status: status, createdEpoch: try int(g, "created_epoch"), endedEpoch: ended)
            guard state.grantIndex(grant.grantID) == nil else { throw Bad() }
            state.grants.append(grant)
            state.usedIDs.insert(grant.grantID)
        }
        for item in try arr(root["events"]) {
            let e = try obj(item)
            let text = try bytes(e, "text_b64")
            let event = TurnEvent(
                eventID: try str(e, "event_id"), turnID: try str(e, "turn_id"),
                revision: try int(e, "revision"), seq: try int(e, "seq"), sendID: try str(e, "send_id"),
                workspace: try str(e, "workspace"), project: try str(e, "project"),
                chat: try str(e, "chat"), principal: try str(e, "principal"), text: text,
                textSHA256: sha256Hex(text), optedOut: try { guard let b = e["opted_out"]?.boolValue else { throw Bad() }; return b }(),
                grantIDs: try strList(e, "grant_ids"), epoch: try int(e, "epoch"),
                signature: try bytes(e, "signature_b64"))
            if let prior = state.turns[event.turnID], state.events[prior].revision >= event.revision { throw Bad() }
            state.addEvent(event)
        }
        state.withdrawnTurns = Set(try strList(root, "withdrawn_turns"))
        for (k, v) in try obj(root["counters"]) {
            guard let i = v.intValue else { throw Bad() }
            state.counters[k] = i
        }
        return state
    }
}

func scalarCount(_ s: String) -> Int { s.unicodeScalars.count }

func requireString(_ params: [String: JSONValue], _ key: String) throws -> String {
    guard let s = params[key]?.stringValue, !s.isEmpty, scalarCount(s) <= 512 else {
        throw AuthorityError("bad_command")
    }
    return s
}

func optionalString(_ params: [String: JSONValue], _ key: String) throws -> String? {
    guard let v = params[key], !v.isNull else { return nil }
    return try requireString(params, key)
}
