import Foundation

/// What the anchor holds: the monotonic counter plus the digests of the blob
/// written under it and under the previous counter.
public struct AnchorRecord: Equatable {
    public var counter: Int64
    /// SHA-256 of the blob that carries `counter` (nil only at counter 0).
    public var currentDigest: [UInt8]?
    /// SHA-256 of the blob that carried `counter - 1` (nil at counters 0 and 1).
    public var previousDigest: [UInt8]?

    public init(counter: Int64, currentDigest: [UInt8]? = nil, previousDigest: [UInt8]? = nil) {
        self.counter = counter
        self.currentDigest = currentDigest
        self.previousDigest = previousDigest
    }
}

/// A monotonic counter held outside the state (root-owned in production).
public protocol CounterAnchor: AnyObject {
    /// Throws `AuthorityError("anchor_missing" | "anchor_invalid")` when the anchor cannot be trusted.
    func read() throws -> AnchorRecord
    /// Durably record `to` (which must be exactly `read().counter + 1`) with the digest of the blob about to be written.
    func advance(to: Int64, blobSHA256: [UInt8]) throws
}

/// Whole-blob persistence. `write` must be atomic and durable.
public protocol AuthorityStorage: AnyObject {
    func read() throws -> [UInt8]?
    func write(_ data: [UInt8]) throws
}

/// The protocol core: grants and immutable turn events behind injected
/// collaborators. A line-for-line port of `ChatAuthority` in
/// `ciao/chat_authority.py` plus the digest-anchor rules from
/// `docs/CHAT_AUTHORITY_HELPER.md`. Reads are only possible on an instance
/// that finished opening, and opening finishes only after any crash-window
/// recovery has rewritten the state under a new counter.
public final class ChatAuthority {
    private let eventSigner: EventSigner
    private let stateMAC: StateMAC
    private let operatorKey: OperatorVerifier
    private let storage: AuthorityStorage
    private let anchor: CounterAnchor
    private let newID: () -> String
    private let lock = NSLock()
    private var counter: Int64 = 0
    private var state: AuthState
    /// True when opening found the blob one behind the anchor and rewrote it.
    public private(set) var recoveredCrashWindow = false

    public init(
        eventSigner: EventSigner, stateMAC: StateMAC, operatorKey: OperatorVerifier,
        storage: AuthorityStorage, anchor: CounterAnchor, newID: @escaping () -> String
    ) throws {
        if eventSigner.usesSecret(stateMAC.keyBytes) { throw AuthorityError("bad_key") }
        self.eventSigner = eventSigner
        self.stateMAC = stateMAC
        self.operatorKey = operatorKey
        self.storage = storage
        self.anchor = anchor
        self.newID = newID
        self.state = AuthState()
        let loaded = try Self.load(storage: storage, anchor: anchor, mac: stateMAC)
        self.state = loaded.state
        self.counter = loaded.anchored
        if loaded.crashWindow {
            // The blob is one behind the anchor. Rewrite it under a new counter
            // before anything can be read (write-ahead: anchor first).
            try persist(state)
            recoveredCrashWindow = true
        }
    }

    // MARK: persistence

    private static func load(storage: AuthorityStorage, anchor: CounterAnchor, mac: StateMAC) throws
        -> (state: AuthState, anchored: Int64, crashWindow: Bool)
    {
        let record = try anchor.read()
        guard record.counter >= 0 else { throw AuthorityError("state_invalid") }
        let blob: [UInt8]?
        do { blob = try storage.read() } catch { throw AuthorityError("state_invalid") }
        guard let blob else {
            if record.counter != 0 { throw AuthorityError("state_missing") }
            return (AuthState(), 0, false)
        }
        let blobCounter: Int64
        let state: AuthState
        do {
            let envelope = try JSON.parse(blob)
            guard let outer = envelope.objectValue, let body = outer["state"],
                let tagText = outer["mac_b64"]?.stringValue,
                let tagData = Data(base64Encoded: tagText), Data(tagData).base64EncodedString() == tagText
            else { throw AuthorityError("state_invalid") }
            guard mac.verify(stateContext + JSON.canonical(body), tag: Array(tagData)) else {
                throw AuthorityError("state_invalid")
            }
            guard let c = body.objectValue?["state_counter"]?.intValue, c >= 1 else {
                throw AuthorityError("state_invalid")
            }
            blobCounter = c
            state = try AuthState.from(json: body)
        } catch {
            throw AuthorityError("state_invalid")
        }
        if blobCounter > record.counter { throw AuthorityError("state_invalid") }
        if blobCounter < record.counter - 1 { throw AuthorityError("state_rollback") }
        let digest = sha256Bytes(blob)
        if blobCounter == record.counter {
            guard record.currentDigest == digest else { throw AuthorityError("state_invalid") }
            return (state, record.counter, false)
        }
        // One behind: a crash between the anchor advance and the blob write.
        // Accept it only if it is exactly the previous blob the anchor saw.
        guard record.previousDigest == digest else { throw AuthorityError("state_rollback") }
        return (state, record.counter, true)
    }

    /// Highest value handed to the anchor.
    public var stateCounter: Int64 { lock.withLock { counter } }

    /// Advance the anchor first (with the digest of the blob about to be written), then write the blob.
    private func persist(_ next: AuthState) throws {
        let n = counter + 1
        let body = next.toJSON(counter: n)
        let tag = stateMAC.tag(stateContext + JSON.canonical(body))
        let blob = JSON.canonical(.object(["state": body, "mac_b64": .string(Data(tag).base64EncodedString())]))
        do { try anchor.advance(to: n, blobSHA256: sha256Bytes(blob)) } catch { throw AuthorityError("anchor_failed") }
        counter = n
        do { try storage.write(blob) } catch { throw AuthorityError("storage_failed") }
    }

    private func freshID(_ state: inout AuthState) throws -> String {
        let new = newID()
        if new.isEmpty || scalarCount(new) > 128 || state.usedIDs.contains(new) {
            throw AuthorityError("id_collision")
        }
        state.usedIDs.insert(new)
        return new
    }

    /// Verify, consume the counter and mutate a copy; swap only once stored.
    private func apply<T>(_ signed: SignedCommand, kind: String, _ mutate: (inout AuthState) throws -> T) throws -> T {
        let cmd = signed.command
        if cmd.kind != kind { throw AuthorityError("bad_command") }
        guard cmd.keyID == operatorKey.keyID, operatorKey.verify(cmd.signingBytes(), signature: signed.signature)
        else { throw AuthorityError("bad_signature") }
        guard (1...maxCounter).contains(cmd.counter), !cmd.principal.isEmpty, scalarCount(cmd.principal) <= 128 else {
            throw AuthorityError("bad_command")
        }
        return try lock.withLock {
            let base = state
            let last = base.counters[cmd.principal]
            if let last, cmd.counter <= last { throw AuthorityError("replayed") }
            if last == nil && base.counters.count >= maxPrincipals { throw AuthorityError("capacity") }
            var draft = base
            draft.counters[cmd.principal] = cmd.counter
            let result: T
            do {
                result = try mutate(&draft)
            } catch let refusal as AuthorityError {
                // A refusal is final: record only the consumed counter.
                var refused = base
                refused.counters[cmd.principal] = cmd.counter
                try persist(refused)
                state = refused
                throw refusal
            }
            try persist(draft)
            state = draft
            return result
        }
    }

    // MARK: operator commands

    public func createGrant(_ signed: SignedCommand) throws -> Grant {
        let cmd = signed.command
        guard let raw = cmd.params["producers"]?.arrayValue, !raw.isEmpty else { throw AuthorityError("bad_command") }
        var names: [String] = []
        for item in raw {
            guard let s = item.stringValue, !s.isEmpty else { throw AuthorityError("bad_command") }
            names.append(s)
        }
        let producers = JSON.sortedKeys(Set(names))
        return try apply(signed, kind: CommandKind.grant) { state in
            state.pruneEndedGrants()
            let live = state.grants.filter { $0.status == GrantStatus.active }.count
            if live >= maxGrants || state.grants.count >= maxGrantRecords { throw AuthorityError("capacity") }
            let grant = Grant(
                grantID: try freshID(&state), workspace: try requireString(cmd.params, "workspace"),
                project: try requireString(cmd.params, "project"), chat: try optionalString(cmd.params, "chat"),
                destination: try requireString(cmd.params, "destination"),
                audience: try requireString(cmd.params, "audience"), producers: producers,
                principal: cmd.principal, status: GrantStatus.active, createdEpoch: state.epoch, endedEpoch: nil)
            state.grants.append(grant)
            return grant
        }
    }

    private func endGrant(_ signed: SignedCommand, kind: String, status: String) throws -> Int64 {
        try apply(signed, kind: kind) { state in
            guard let index = state.grantIndex(try requireString(signed.command.params, "grant_id")) else {
                throw AuthorityError("unknown_grant")
            }
            let grant = state.grants[index]
            if grant.status == GrantStatus.active || (status == GrantStatus.withdrawn && grant.status == GrantStatus.revoked) {
                state.epoch += 1
                state.grants[index].status = status
                state.grants[index].endedEpoch = state.epoch
            }
            return state.epoch
        }
    }

    /// Stop eligibility under the grant; returns the new epoch.
    public func revokeGrant(_ signed: SignedCommand) throws -> Int64 {
        try endGrant(signed, kind: CommandKind.revoke, status: GrantStatus.revoked)
    }

    /// Revoke and permanently end eligibility under this grant only.
    public func withdrawGrant(_ signed: SignedCommand) throws -> Int64 {
        try endGrant(signed, kind: CommandKind.withdrawGrant, status: GrantStatus.withdrawn)
    }

    /// Permanently end eligibility of every revision of a turn.
    public func withdrawTurn(_ signed: SignedCommand) throws -> Int64 {
        try apply(signed, kind: CommandKind.withdrawTurn) { state in
            let turnID = try requireString(signed.command.params, "turn_id")
            guard state.turns[turnID] != nil else { throw AuthorityError("unknown_turn") }
            if !state.withdrawnTurns.contains(turnID) {
                state.withdrawnTurns.insert(turnID)
                state.epoch += 1
            }
            return state.epoch
        }
    }

    public func submitTurn(_ signed: SignedCommand) throws -> TurnEvent {
        let cmd = signed.command
        let text = cmd.payload
        if text.isEmpty || text.count > maxTextBytes { throw AuthorityError("invalid_text") }
        guard Array(String(decoding: text, as: UTF8.self).utf8) == text else { throw AuthorityError("invalid_text") }
        let sendID = try requireString(cmd.params, "send_id")
        var optedOut = false
        if let raw = cmd.params["opted_out"] {
            guard let b = raw.boolValue else { throw AuthorityError("bad_command") }
            optedOut = b
        }
        let corrects = try optionalString(cmd.params, "corrects")
        let scope = (
            workspace: try requireString(cmd.params, "workspace"), project: try requireString(cmd.params, "project"),
            chat: try requireString(cmd.params, "chat")
        )
        return try apply(signed, kind: CommandKind.submit) { state in
            if let priorIndex = state.bySend[sendID] {
                let prior = state.events[priorIndex]
                let revisionOK = corrects == nil ? prior.revision == 1 : (prior.turnID == corrects && prior.revision > 1)
                let same =
                    prior.workspace == scope.workspace && prior.project == scope.project && prior.chat == scope.chat
                    && prior.text == text && prior.optedOut == optedOut && prior.principal == cmd.principal && revisionOK
                if !same { throw AuthorityError("retry_conflict") }
                return prior
            }
            if state.events.count >= maxEvents || state.textBytes + text.count > maxTotalTextBytes {
                throw AuthorityError("capacity")
            }
            var revision: Int64 = 1
            let grantIDs: [String]
            let turnID: String
            if let corrects {
                guard let baseIndex = state.turns[corrects] else { throw AuthorityError("bad_command") }
                let base = state.events[baseIndex]
                guard base.workspace == scope.workspace, base.project == scope.project, base.chat == scope.chat,
                    base.principal == cmd.principal
                else { throw AuthorityError("bad_command") }
                // Carry the original enrollment forward; never widen it.
                grantIDs = base.grantIDs
                turnID = corrects
                revision = base.revision + 1
            } else {
                grantIDs = JSON.sortedKeys(
                    state.grants.filter {
                        $0.status == GrantStatus.active && $0.workspace == scope.workspace
                            && $0.project == scope.project && ($0.chat == nil || $0.chat == scope.chat)
                    }.map(\.grantID))
                turnID = try freshID(&state)
            }
            state.seq += 1
            var event = TurnEvent(
                eventID: try freshID(&state), turnID: turnID, revision: revision, seq: state.seq, sendID: sendID,
                workspace: scope.workspace, project: scope.project, chat: scope.chat, principal: cmd.principal,
                text: text, textSHA256: sha256Hex(text), optedOut: optedOut, grantIDs: grantIDs,
                epoch: state.epoch, signature: [])
            event.signature = eventSigner.sign(event.signingBytes())
            state.addEvent(event)
            return event
        }
    }

    // MARK: reads

    public var epoch: Int64 { lock.withLock { state.epoch } }

    public var eventPublicKey: [UInt8] { eventSigner.publicKey }
    public var authorityKeyID: String { eventSigner.authorityKeyID }
    public var operatorKeyID: String { operatorKey.keyID }

    /// True when the event's bytes, digest and signature are intact.
    public func verifyEvent(_ event: TurnEvent) -> Bool {
        event.textSHA256 == sha256Hex(event.text) && eventSigner.verify(event.signingBytes(), signature: event.signature)
    }

    /// Newest-first events a producer may use under a live grant. Every filter
    /// runs before the limit; a stale `expectedEpoch` is an error.
    public func eligibleEvents(
        grantID: String, producer: String, limit: Int, destination: String, audience: String,
        chat: String? = nil, expectedEpoch: Int64? = nil
    ) throws -> Lookup {
        guard (1...maxLookup).contains(limit) else { throw AuthorityError("bad_limit") }
        return try lock.withLock {
            if let expectedEpoch, expectedEpoch != state.epoch { throw AuthorityError("epoch_changed") }
            let empty = Lookup(epoch: state.epoch, grantID: grantID, destination: destination, audience: audience, events: [])
            guard let index = state.grantIndex(grantID) else { return empty }
            let grant = state.grants[index]
            guard grant.status == GrantStatus.active, grant.producers.contains(producer),
                grant.destination == destination, grant.audience == audience
            else { return empty }
            var picked: [TurnEvent] = []
            for event in state.events.reversed() {
                if picked.count >= limit { break }
                if event.grantIDs.contains(grantID) && !event.optedOut && !state.withdrawnTurns.contains(event.turnID)
                    && event.revision == state.events[state.turns[event.turnID]!].revision
                    && (chat == nil || event.chat == chat) && verifyEvent(event)
                {
                    picked.append(event)
                }
            }
            return Lookup(epoch: state.epoch, grantID: grantID, destination: destination, audience: audience, events: picked)
        }
    }

    public func grant(_ id: String) -> Grant? { lock.withLock { state.grants.first { $0.grantID == id } } }
}
