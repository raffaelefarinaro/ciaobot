"""Transport-agnostic chat authority protocol (#3623, slice 2).

This is a protocol library, not a security boundary. It makes every decision
about grants, turn events, epochs, revocation, withdrawal, deduplication and
replay as a function of its injected collaborators (a signer, an operator
verifier, a storage object and an id factory). It claims no isolation: whether
an agent running as the engine's OS user can reach the signing key, the stored
state or the operator channel is decided entirely by the transport and daemon
slices that will host it. Nothing in the engine calls this module yet, and
`chat_evidence` stays unavailable until a protected host and review exist.

Model:

* The operator channel signs every command (grant, revoke, withdraw, submit).
  A command carries a per-principal counter that must be strictly greater than
  the last one the authority accepted from that principal, so a replay is
  refused, also after a restart, with constant state. Any authenticated
  command that is refused (capacity, unknown grant, retry conflict, id
  collision) still consumes its counter and persists that fact, so a refusal is
  final and cannot be replayed later when room appears. Only a failed storage
  write leaves the counter unconsumed, because nothing was recorded.
  Revoke and withdraw have no growth limit of their own and are never blocked
  by event, byte or grant capacity; only a new, unseen principal beyond
  `MAX_PRINCIPALS` is refused.
* The authority signs each stored turn event and the whole persisted state; a
  state blob that fails verification refuses to load and the authority stays
  closed (no fallback to an empty state). Every id the injected factory returns
  (grant, turn, event) must be unused, so a repeated id can neither overwrite
  nor reactivate a revoked grant (`id_collision`).
* A turn event keeps the exact submitted bytes, their digest, the scope, the
  principal, the grant ids active at capture and a revision. A send id is the
  idempotency key: an identical retry (fresh counter, same bytes, scope and
  correction target) resolves to the same event; the same send id with other
  bytes, scope or target is refused. A correction is a new revision of the same
  turn; it carries the original turn's grant ids forward and is never enrolled
  in a grant created after the original turn, and the older revision stops being
  eligible.
* A grant covers one workspace/project (one chat, or every chat of the project)
  for a destination, audience and a set of producers. It only enrolls turns
  captured while it was active; it never reaches older turns. Revoking or
  withdrawing a grant advances the epoch and ends eligibility of turns only
  under that grant; a turn also enrolled in another active grant stays eligible
  there. Withdrawing a turn ends eligibility of every revision of that turn
  under every grant, permanently.
* `eligible_events` applies every filter before the limit, so recent ineligible
  turns cannot hide older eligible ones, and can pin the caller's epoch.
* Cost per command is bounded by documented caps (`MAX_EVENTS`,
  `MAX_TOTAL_TEXT_BYTES`, `MAX_GRANTS`, `MAX_PRINCIPALS`): a command copies
  the state's containers (immutable records are shared, not deep-copied) and
  re-serialises the capped state once.

Hardening (#3623 slice 1, #3662), closed in this library:

* Operator commands are signed with ECDSA P-256 (raw r||s) by a key the
  authority never holds: it is given an `EcdsaP256Verifier` (public key only)
  and refuses any operator object that can sign. There is no symmetric
  operator signer.
* Events are signed with an Ed25519 key; the persisted state is authenticated
  with a separate HMAC `StateMac` key. The two are distinct types and
  domain-separated, and construction refuses a state MAC as the event signer.
* `eligible_events` takes the intended destination and audience (required,
  no default) and returns nothing unless the grant carries exactly those; the
  result echoes them with the grant id.
* The persisted state carries a `state_counter`. A `CounterAnchor` (a
  monotonic value held outside the state, injected) is advanced to the next
  value before the blob is written (write-ahead). At load a blob equal to the
  anchor is normal, one below it by one is a crash between the two steps and is
  accepted, a lower one is `state_rollback`, a higher one `state_invalid`, and
  a missing blob with the anchor above zero is `state_missing`: the authority
  refuses to open (stays closed), never repairing. Two failed writes in a row
  leave the anchor two ahead of the blob and therefore also fail closed on the
  next load; recovery is the host's admin reset.
* An operator counter above `MAX_COUNTER` (2**53 - 1) is refused without being
  consumed, so a signed 2**62 cannot lock a principal out.
* `MAX_GRANTS` counts live (active) grants only. Creating a grant first drops
  ended grants that no stored event names; `MAX_GRANT_RECORDS` bounds what
  remains. A pruned grant id is forgotten after a restart (the id factory must
  not repeat ids), and a lookup or revoke of it reads as unknown.
* A correction must come from the principal that submitted the turn.
* Revoke and withdraw are never blocked by event, byte or grant capacity, but a
  new principal beyond `MAX_PRINCIPALS` is refused, so a 65th principal's
  revoke or withdraw gets `capacity`. Only the operator key can introduce
  principals, so only the key holder can cause it.

Gaps this library deliberately leaves to the transport and daemon slices:

* A lookup result is a snapshot: the caller must re-check the epoch at the
  point of use (pass `expected_epoch`, and re-check at admission).
* The anchor must be a real root-owned store; the in-memory one in tests proves
  only the protocol. Whole-volume restore restores blob and anchor together.
* The storage object must be owner-private, locked against concurrent
  processes, and durable (atomic write plus fsync); this library only calls
  `read` and `write`.

Shared vectors for canonical command, event and state bytes, signatures, command
sequences and the anchor rules are in `tests/vectors/chat_authority_v1.json`
(generated by `tests/chat_authority_vectors.py`); the Swift daemon must pass
the same file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

STATE_VERSION = 1
MAX_TEXT_BYTES = 64 * 1024
MAX_EVENTS = 2000
MAX_TOTAL_TEXT_BYTES = 4 * 1024 * 1024
MAX_GRANTS = 500  # live (active) grants
MAX_GRANT_RECORDS = 1000  # live plus ended grants that stored events still name
MAX_COUNTER = 2**53 - 1
MAX_PRINCIPALS = 64
MAX_LOOKUP = 200

_CMD_CONTEXT = b"ciao-chat-authority/command/v1\0"
_EVENT_CONTEXT = b"ciao-chat-authority/event/v1\0"
_STATE_CONTEXT = b"ciao-chat-authority/state/v1\0"

KIND_SUBMIT = "submit_turn"
KIND_GRANT = "create_grant"
KIND_REVOKE = "revoke_grant"
KIND_WITHDRAW_GRANT = "withdraw_grant"
KIND_WITHDRAW_TURN = "withdraw_turn"

GRANT_ACTIVE = "active"
GRANT_REVOKED = "revoked"
GRANT_WITHDRAWN = "withdrawn"


class AuthorityError(Exception):
    """A refused operation; `code` is stable and machine-readable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Verifier(Protocol):
    def verify(self, data: bytes, signature: bytes) -> bool: ...


class Signer(Verifier, Protocol):
    def sign(self, data: bytes) -> bytes: ...


class _CommandSigner(Protocol):
    def sign(self, data: bytes) -> bytes: ...


class CounterAnchor(Protocol):
    """A monotonic counter held outside the state (root-owned in production)."""

    def current(self) -> int: ...

    def advance(self, to: int) -> None:
        """Durably record `to`, which must exceed `current()`; raise otherwise."""


class AuthorityStorage(Protocol):
    """Whole-blob persistence. `write` must be atomic and durable."""

    def read(self) -> bytes | None: ...

    def write(self, data: bytes) -> None: ...


class StateMac:
    """HMAC-SHA256 over an injected key; authenticates the persisted state only."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("key must be at least 32 bytes")
        self._key = key

    def tag(self, data: bytes) -> bytes:
        return hmac.new(self._key, data, hashlib.sha256).digest()

    def verify(self, data: bytes, tag: bytes) -> bool:
        return hmac.compare_digest(self.tag(data), tag)


class Ed25519Verifier:
    """Public half of an Ed25519 key: checks signatures, cannot make them."""

    def __init__(self, public_key: bytes) -> None:
        self._key = ed25519.Ed25519PublicKey.from_public_bytes(public_key)
        self._raw = public_key

    def public_key(self) -> bytes:
        return self._raw

    def verify(self, data: bytes, signature: bytes) -> bool:
        try:
            self._key.verify(signature, data)
        except (InvalidSignature, ValueError):
            return False
        return True


class Ed25519Signer(Ed25519Verifier):
    """Authority event signer (Ed25519 is deterministic)."""

    def __init__(self, private: ed25519.Ed25519PrivateKey) -> None:
        self._private = private
        super().__init__(
            private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        )

    @classmethod
    def from_seed(cls, seed: bytes) -> Ed25519Signer:
        if len(seed) != 32:
            raise ValueError("seed must be 32 bytes")
        return cls(ed25519.Ed25519PrivateKey.from_private_bytes(seed))

    def sign(self, data: bytes) -> bytes:
        return self._private.sign(data)


class EcdsaP256Verifier:
    """Operator verifier: an ECDSA P-256 public key (x9.63 point), raw r||s signatures.

    Holds no signing ability, so whoever can read the authority's verifier
    cannot forge operator commands.
    """

    def __init__(self, public_key_x963: bytes) -> None:
        self._key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), public_key_x963
        )
        self._raw = public_key_x963

    def public_key_x963(self) -> bytes:
        return self._raw

    def verify(self, data: bytes, signature: bytes) -> bool:
        if len(signature) != 64:
            return False
        r = int.from_bytes(signature[:32], "big")
        s = int.from_bytes(signature[32:], "big")
        if not (0 < r and 0 < s):
            return False
        try:
            self._key.verify(
                encode_dss_signature(r, s), data, ec.ECDSA(hashes.SHA256())
            )
        except (InvalidSignature, ValueError):
            return False
        return True


class EcdsaP256Signer:
    """Operator-side reference signer (RFC 6979 deterministic, raw r||s).

    The production operator key lives in the Mac app's Secure Enclave; the
    engine and the authority never hold one. This class exists to sign the
    shared vectors and to drive tests.
    """

    def __init__(self, private: ec.EllipticCurvePrivateKey) -> None:
        self._private = private

    @classmethod
    def generate(cls) -> EcdsaP256Signer:
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_private_scalar(cls, scalar: int) -> EcdsaP256Signer:
        return cls(ec.derive_private_key(scalar, ec.SECP256R1()))

    def public_key_x963(self) -> bytes:
        return self._private.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint
        )

    def verifier(self) -> EcdsaP256Verifier:
        return EcdsaP256Verifier(self.public_key_x963())

    def sign(self, data: bytes) -> bytes:
        der = self._private.sign(
            data, ec.ECDSA(hashes.SHA256(), deterministic_signing=True)
        )
        r, s = decode_dss_signature(der)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Command:
    """An operator instruction. Only `submit_turn` carries a payload."""

    kind: str
    counter: int
    principal: str
    params: Mapping[str, Any]
    payload: bytes = b""

    def signing_bytes(self) -> bytes:
        return _CMD_CONTEXT + _canonical(
            {
                "kind": self.kind,
                "counter": self.counter,
                "principal": self.principal,
                "params": dict(self.params),
                "payload_sha256": _sha256(self.payload),
            }
        )


@dataclass(frozen=True)
class SignedCommand:
    command: Command
    signature: bytes


def sign_command(signer: _CommandSigner, command: Command) -> SignedCommand:
    """Operator-side helper: sign a command with the operator key."""
    return SignedCommand(command, signer.sign(command.signing_bytes()))


@dataclass(frozen=True)
class Grant:
    grant_id: str
    workspace: str
    project: str
    chat: str | None  # None: every chat of the project captured while active
    destination: str
    audience: str
    producers: tuple[str, ...]
    principal: str
    status: str
    created_epoch: int
    ended_epoch: int | None


@dataclass(frozen=True)
class TurnEvent:
    event_id: str
    turn_id: str
    revision: int
    seq: int
    send_id: str
    workspace: str
    project: str
    chat: str
    principal: str
    text: bytes
    text_sha256: str
    opted_out: bool
    grant_ids: tuple[str, ...]
    epoch: int
    signature: bytes

    def signing_bytes(self) -> bytes:
        return _EVENT_CONTEXT + _canonical(
            {
                "event_id": self.event_id,
                "turn_id": self.turn_id,
                "revision": self.revision,
                "seq": self.seq,
                "send_id": self.send_id,
                "workspace": self.workspace,
                "project": self.project,
                "chat": self.chat,
                "principal": self.principal,
                "text_sha256": self.text_sha256,
                "opted_out": self.opted_out,
                "grant_ids": list(self.grant_ids),
                "epoch": self.epoch,
            }
        )


@dataclass(frozen=True)
class Lookup:
    epoch: int
    grant_id: str
    destination: str
    audience: str
    events: tuple[TurnEvent, ...]


@dataclass
class _State:
    epoch: int = 0
    seq: int = 0
    grants: dict[str, Grant] = field(default_factory=dict)
    events: list[TurnEvent] = field(default_factory=list)
    by_send: dict[str, TurnEvent] = field(default_factory=dict)
    turns: dict[str, TurnEvent] = field(default_factory=dict)  # latest revision
    used_ids: set[str] = field(default_factory=set)
    withdrawn_turns: set[str] = field(default_factory=set)
    counters: dict[str, int] = field(default_factory=dict)
    text_bytes: int = 0

    def copy(self) -> _State:
        """Copy the containers; Grant and TurnEvent are frozen and shared."""
        return _State(
            epoch=self.epoch,
            seq=self.seq,
            grants=dict(self.grants),
            events=list(self.events),
            by_send=dict(self.by_send),
            turns=dict(self.turns),
            used_ids=set(self.used_ids),
            withdrawn_turns=set(self.withdrawn_turns),
            counters=dict(self.counters),
            text_bytes=self.text_bytes,
        )

    def add_event(self, event: TurnEvent) -> None:
        self.events.append(event)
        self.by_send[event.send_id] = event
        self.turns[event.turn_id] = event
        self.used_ids.update((event.event_id, event.turn_id))
        self.text_bytes += len(event.text)


def _state_to_dict(state: _State, counter: int) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "state_counter": counter,
        "epoch": state.epoch,
        "seq": state.seq,
        "grants": [
            {
                "grant_id": g.grant_id,
                "workspace": g.workspace,
                "project": g.project,
                "chat": g.chat,
                "destination": g.destination,
                "audience": g.audience,
                "producers": list(g.producers),
                "principal": g.principal,
                "status": g.status,
                "created_epoch": g.created_epoch,
                "ended_epoch": g.ended_epoch,
            }
            for g in state.grants.values()
        ],
        "events": [
            {
                "event_id": e.event_id,
                "turn_id": e.turn_id,
                "revision": e.revision,
                "seq": e.seq,
                "send_id": e.send_id,
                "workspace": e.workspace,
                "project": e.project,
                "chat": e.chat,
                "principal": e.principal,
                "text_b64": base64.b64encode(e.text).decode("ascii"),
                "opted_out": e.opted_out,
                "grant_ids": list(e.grant_ids),
                "epoch": e.epoch,
                "signature_b64": base64.b64encode(e.signature).decode("ascii"),
            }
            for e in state.events
        ],
        "withdrawn_turns": sorted(state.withdrawn_turns),
        "counters": dict(sorted(state.counters.items())),
    }


def _state_from_dict(raw: dict[str, Any]) -> _State:
    if raw["version"] != STATE_VERSION:
        raise ValueError("version")
    state = _State(epoch=int(raw["epoch"]), seq=int(raw["seq"]))
    for g in raw["grants"]:
        grant = Grant(
            grant_id=g["grant_id"],
            workspace=g["workspace"],
            project=g["project"],
            chat=g["chat"],
            destination=g["destination"],
            audience=g["audience"],
            producers=tuple(g["producers"]),
            principal=g["principal"],
            status=g["status"],
            created_epoch=int(g["created_epoch"]),
            ended_epoch=g["ended_epoch"],
        )
        state.grants[grant.grant_id] = grant
        state.used_ids.add(grant.grant_id)
    for e in raw["events"]:
        text = base64.b64decode(e["text_b64"], validate=True)
        event = TurnEvent(
            event_id=e["event_id"],
            turn_id=e["turn_id"],
            revision=int(e["revision"]),
            seq=int(e["seq"]),
            send_id=e["send_id"],
            workspace=e["workspace"],
            project=e["project"],
            chat=e["chat"],
            principal=e["principal"],
            text=text,
            text_sha256=_sha256(text),
            opted_out=bool(e["opted_out"]),
            grant_ids=tuple(e["grant_ids"]),
            epoch=int(e["epoch"]),
            signature=base64.b64decode(e["signature_b64"], validate=True),
        )
        prior = state.turns.get(event.turn_id)
        if prior is not None and prior.revision >= event.revision:
            raise ValueError("order")
        state.add_event(event)
    state.withdrawn_turns = set(raw["withdrawn_turns"])
    state.counters = {str(k): int(v) for k, v in raw["counters"].items()}
    return state


def _str(params: Mapping[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value or len(value) > 512:
        raise AuthorityError("bad_command")
    return value


def _opt_str(params: Mapping[str, Any], key: str) -> str | None:
    if params.get(key) is None:
        return None
    return _str(params, key)


def _prune_ended_grants(state: _State) -> None:
    """Drop ended grants that no stored event names (their ids stay in `used_ids`)."""
    named = {gid for e in state.events for gid in e.grant_ids}
    for gid in [
        g.grant_id
        for g in state.grants.values()
        if g.status != GRANT_ACTIVE and g.grant_id not in named
    ]:
        del state.grants[gid]


class ChatAuthority:
    """Grants and immutable turn events behind injected collaborators."""

    def __init__(
        self,
        *,
        event_signer: Signer,
        state_mac: StateMac,
        operator: Verifier,
        storage: AuthorityStorage,
        anchor: CounterAnchor,
        new_id: Callable[[], str],
    ) -> None:
        if isinstance(event_signer, StateMac) or not hasattr(event_signer, "sign"):
            raise ValueError("event signer must be its own signing key, not the state key")
        if hasattr(operator, "sign"):
            raise ValueError("operator must be a verifier without signing ability")
        self._event_signer = event_signer
        self._state_mac = state_mac
        self._operator = operator
        self._storage = storage
        self._anchor = anchor
        self._new_id = new_id
        self._lock = threading.Lock()
        self._counter = 0
        self._state = self._load()

    # ---- persistence -------------------------------------------------

    def _load(self) -> _State:
        blob = self._storage.read()
        anchored = self._anchor.current()
        if not isinstance(anchored, int) or isinstance(anchored, bool) or anchored < 0:
            raise AuthorityError("state_invalid")
        if blob is None:
            if anchored != 0:
                raise AuthorityError("state_missing")
            return _State()
        try:
            envelope = json.loads(blob.decode("utf-8"))
            body = _canonical(envelope["state"])
            signature = base64.b64decode(envelope["mac_b64"], validate=True)
            if not self._state_mac.verify(_STATE_CONTEXT + body, signature):
                raise ValueError("mac")
            counter = envelope["state"]["state_counter"]
            if not isinstance(counter, int) or isinstance(counter, bool) or counter < 1:
                raise ValueError("counter")
            state = _state_from_dict(envelope["state"])
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthorityError("state_invalid") from exc
        if counter > anchored:
            raise AuthorityError("state_invalid")
        if counter < anchored - 1:
            raise AuthorityError("state_rollback")
        self._counter = anchored
        return state

    @property
    def state_counter(self) -> int:
        """Highest value handed to the anchor (the blob's, or one ahead in a crash window)."""
        with self._lock:
            return self._counter

    def _persist(self, state: _State) -> None:
        """Advance the anchor first, then write the blob that carries the value."""
        nxt = self._counter + 1
        self._anchor.advance(nxt)
        self._counter = nxt
        body = _state_to_dict(state, nxt)
        mac = self._state_mac.tag(_STATE_CONTEXT + _canonical(body))
        envelope = {"state": body, "mac_b64": base64.b64encode(mac).decode("ascii")}
        self._storage.write(_canonical(envelope))

    def _fresh_id(self, state: _State) -> str:
        new = self._new_id()
        if not isinstance(new, str) or not new or len(new) > 128:
            raise AuthorityError("id_collision")
        if new in state.used_ids:
            raise AuthorityError("id_collision")
        state.used_ids.add(new)
        return new

    def _apply(
        self, signed: SignedCommand, kind: str, mutate: Callable[[_State], Any]
    ) -> Any:
        """Verify, consume the counter and mutate a copy; swap only once stored."""
        cmd = signed.command
        try:
            signing = cmd.signing_bytes()
        except (TypeError, ValueError) as exc:
            raise AuthorityError("bad_command") from exc
        if cmd.kind != kind:
            raise AuthorityError("bad_command")
        if not self._operator.verify(signing, signed.signature):
            raise AuthorityError("bad_signature")
        if (
            not isinstance(cmd.counter, int)
            or isinstance(cmd.counter, bool)
            or not 1 <= cmd.counter <= MAX_COUNTER
            or not cmd.principal
            or len(cmd.principal) > 128
        ):
            raise AuthorityError("bad_command")
        with self._lock:
            base = self._state
            last = base.counters.get(cmd.principal)
            if last is not None and cmd.counter <= last:
                raise AuthorityError("replayed")
            if last is None and len(base.counters) >= MAX_PRINCIPALS:
                raise AuthorityError("capacity")
            draft = base.copy()
            draft.counters[cmd.principal] = cmd.counter
            try:
                result = mutate(draft)
            except AuthorityError:
                # A refusal is final: record only the consumed counter.
                refused = base.copy()
                refused.counters[cmd.principal] = cmd.counter
                self._persist(refused)
                self._state = refused
                raise
            self._persist(draft)
            self._state = draft
            return result

    # ---- operator commands -------------------------------------------

    def create_grant(self, signed: SignedCommand) -> Grant:
        cmd = signed.command
        raw = cmd.params.get("producers")
        if not isinstance(raw, list) or not raw or not all(
            isinstance(p, str) and p for p in raw
        ):
            raise AuthorityError("bad_command")
        producers = tuple(sorted(set(raw)))

        def mutate(state: _State) -> Grant:
            _prune_ended_grants(state)
            live = sum(1 for g in state.grants.values() if g.status == GRANT_ACTIVE)
            if live >= MAX_GRANTS or len(state.grants) >= MAX_GRANT_RECORDS:
                raise AuthorityError("capacity")
            grant = Grant(
                grant_id=self._fresh_id(state),
                workspace=_str(cmd.params, "workspace"),
                project=_str(cmd.params, "project"),
                chat=_opt_str(cmd.params, "chat"),
                destination=_str(cmd.params, "destination"),
                audience=_str(cmd.params, "audience"),
                producers=producers,
                principal=cmd.principal,
                status=GRANT_ACTIVE,
                created_epoch=state.epoch,
                ended_epoch=None,
            )
            state.grants[grant.grant_id] = grant
            return grant

        result: Grant = self._apply(signed, KIND_GRANT, mutate)
        return result

    def _end_grant(self, signed: SignedCommand, kind: str, status: str) -> int:
        def mutate(state: _State) -> int:
            grant = state.grants.get(_str(signed.command.params, "grant_id"))
            if grant is None:
                raise AuthorityError("unknown_grant")
            if grant.status == GRANT_ACTIVE or (
                status == GRANT_WITHDRAWN and grant.status == GRANT_REVOKED
            ):
                state.epoch += 1
                state.grants[grant.grant_id] = replace(
                    grant, status=status, ended_epoch=state.epoch
                )
            return state.epoch

        epoch: int = self._apply(signed, kind, mutate)
        return epoch

    def revoke_grant(self, signed: SignedCommand) -> int:
        """Stop eligibility under the grant; returns the new epoch."""
        return self._end_grant(signed, KIND_REVOKE, GRANT_REVOKED)

    def withdraw_grant(self, signed: SignedCommand) -> int:
        """Revoke and permanently end eligibility under this grant only."""
        return self._end_grant(signed, KIND_WITHDRAW_GRANT, GRANT_WITHDRAWN)

    def withdraw_turn(self, signed: SignedCommand) -> int:
        """Permanently end eligibility of every revision of a turn."""

        def mutate(state: _State) -> int:
            turn_id = _str(signed.command.params, "turn_id")
            if turn_id not in state.turns:
                raise AuthorityError("unknown_turn")
            if turn_id not in state.withdrawn_turns:
                state.withdrawn_turns.add(turn_id)
                state.epoch += 1
            return state.epoch

        epoch: int = self._apply(signed, KIND_WITHDRAW_TURN, mutate)
        return epoch

    def submit_turn(self, signed: SignedCommand) -> TurnEvent:
        cmd = signed.command
        text = cmd.payload
        if not text or len(text) > MAX_TEXT_BYTES:
            raise AuthorityError("invalid_text")
        try:
            text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AuthorityError("invalid_text") from exc
        send_id = _str(cmd.params, "send_id")
        opted_out = cmd.params.get("opted_out", False)
        if not isinstance(opted_out, bool):
            raise AuthorityError("bad_command")
        corrects = _opt_str(cmd.params, "corrects")
        scope = (
            _str(cmd.params, "workspace"),
            _str(cmd.params, "project"),
            _str(cmd.params, "chat"),
        )

        def mutate(state: _State) -> TurnEvent:
            prior = state.by_send.get(send_id)
            if prior is not None:
                same = (
                    (prior.workspace, prior.project, prior.chat) == scope
                    and prior.text == text
                    and prior.opted_out == opted_out
                    and prior.principal == cmd.principal
                    and (
                        prior.revision == 1
                        if corrects is None
                        else prior.turn_id == corrects and prior.revision > 1
                    )
                )
                if not same:
                    raise AuthorityError("retry_conflict")
                return prior
            if (
                len(state.events) >= MAX_EVENTS
                or state.text_bytes + len(text) > MAX_TOTAL_TEXT_BYTES
            ):
                raise AuthorityError("capacity")
            revision = 1
            if corrects is None:
                grant_ids = tuple(
                    sorted(
                        g.grant_id
                        for g in state.grants.values()
                        if g.status == GRANT_ACTIVE
                        and (g.workspace, g.project) == scope[:2]
                        and g.chat in (None, scope[2])
                    )
                )
                turn_id = self._fresh_id(state)
            else:
                base = state.turns.get(corrects)
                if (
                    base is None
                    or (base.workspace, base.project, base.chat) != scope
                    or base.principal != cmd.principal
                ):
                    raise AuthorityError("bad_command")
                # Carry the original enrollment forward; never widen it.
                grant_ids = base.grant_ids
                turn_id = corrects
                revision = base.revision + 1
            state.seq += 1
            unsigned = TurnEvent(
                event_id=self._fresh_id(state),
                turn_id=turn_id,
                revision=revision,
                seq=state.seq,
                send_id=send_id,
                workspace=scope[0],
                project=scope[1],
                chat=scope[2],
                principal=cmd.principal,
                text=text,
                text_sha256=_sha256(text),
                opted_out=opted_out,
                grant_ids=grant_ids,
                epoch=state.epoch,
                signature=b"",
            )
            event = replace(
                unsigned, signature=self._event_signer.sign(unsigned.signing_bytes())
            )
            state.add_event(event)
            return event

        result: TurnEvent = self._apply(signed, KIND_SUBMIT, mutate)
        return result

    # ---- reads -------------------------------------------------------

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._state.epoch

    def verify_event(self, event: TurnEvent) -> bool:
        """True when the event's bytes, digest and signature are intact."""
        return event.text_sha256 == _sha256(event.text) and self._event_signer.verify(
            event.signing_bytes(), event.signature
        )

    def eligible_events(
        self,
        *,
        grant_id: str,
        producer: str,
        limit: int,
        destination: str,
        audience: str,
        chat: str | None = None,
        expected_epoch: int | None = None,
    ) -> Lookup:
        """Newest-first events a producer may use under a live grant.

        Every filter runs before the limit. A revoked, withdrawn, unknown,
        producer-mismatched or destination/audience-mismatched grant yields
        nothing; a stale `expected_epoch` is
        an error so the caller re-reads instead of acting on old authority.
        """
        if not 1 <= limit <= MAX_LOOKUP:
            raise AuthorityError("bad_limit")
        with self._lock:
            state = self._state
            if expected_epoch is not None and expected_epoch != state.epoch:
                raise AuthorityError("epoch_changed")
            grant = state.grants.get(grant_id)
            if (
                grant is None
                or grant.status != GRANT_ACTIVE
                or producer not in grant.producers
                or grant.destination != destination
                or grant.audience != audience
            ):
                return Lookup(state.epoch, grant_id, destination, audience, ())
            picked: list[TurnEvent] = []
            for event in reversed(state.events):
                if len(picked) >= limit:
                    break
                if (
                    grant_id in event.grant_ids
                    and not event.opted_out
                    and event.turn_id not in state.withdrawn_turns
                    and event.revision == state.turns[event.turn_id].revision
                    and (chat is None or event.chat == chat)
                    and self.verify_event(event)
                ):
                    picked.append(event)
            return Lookup(state.epoch, grant_id, destination, audience, tuple(picked))

    def grant(self, grant_id: str) -> Grant | None:
        with self._lock:
            return self._state.grants.get(grant_id)
