"""Transport-agnostic chat authority protocol (#3623, slice 2).

This is a protocol library, not a security boundary. It makes every decision
about grants, turn events, epochs, revocation, withdrawal, deduplication and
replay as a pure function of its injected collaborators (a signer, an operator
verifier, a storage object and an id factory). It claims no isolation: whether
an agent running as the engine's OS user can reach the signing key, the stored
state or the operator channel is decided entirely by the transport and daemon
slices that will host it. Nothing in the engine calls this module yet, and
`chat_evidence` stays unavailable until a protected host and review exist.

Model:

* The operator channel signs every command (grant, revoke, withdraw, submit).
  A command carries a single-use nonce; replaying it, also after a restart, is
  refused. The authority itself signs each stored turn event and the whole
  persisted state; a state blob that fails verification refuses to load and the
  authority stays closed (no fallback to an empty state).
* A turn event keeps the exact submitted bytes, their digest, the scope, the
  principal, the grant ids active at capture and a revision. A send id is the
  idempotency key: an identical retry (fresh nonce, same bytes and scope)
  resolves to the same event; the same send id with other bytes or scope is
  refused. A correction is a new revision of the same turn and supersedes the
  older one.
* A grant covers one workspace/project (one chat, or every chat of the project)
  for a destination, audience and a set of producers. It only enrolls turns
  captured while it was active; it never reaches older turns. Revoking or
  withdrawing advances the epoch. Withdrawing a grant or a turn also ends
  eligibility of the affected turns permanently.
* `eligible_events` applies every filter before the limit, so recent ineligible
  turns cannot hide older eligible ones, and can pin the caller's epoch.

Rollback of the stored blob to an older, validly signed copy is not detectable
here; the protected storage of the transport slice must provide that.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

STATE_VERSION = 1
MAX_TEXT_BYTES = 64 * 1024
MAX_EVENTS = 5000
MAX_GRANTS = 500
MAX_NONCES = 20000
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


class AuthorityStorage(Protocol):
    """Whole-blob persistence. `write` must be atomic and durable."""

    def read(self) -> bytes | None: ...

    def write(self, data: bytes) -> None: ...


class HmacSigner:
    """HMAC-SHA256 signer and verifier over an injected key."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("key must be at least 32 bytes")
        self._key = key

    def sign(self, data: bytes) -> bytes:
        return hmac.new(self._key, data, hashlib.sha256).digest()

    def verify(self, data: bytes, signature: bytes) -> bool:
        return hmac.compare_digest(self.sign(data), signature)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Command:
    """An operator instruction. Only `submit_turn` carries a payload."""

    kind: str
    nonce: str
    principal: str
    params: Mapping[str, Any]
    payload: bytes = b""

    def signing_bytes(self) -> bytes:
        return _CMD_CONTEXT + _canonical(
            {
                "kind": self.kind,
                "nonce": self.nonce,
                "principal": self.principal,
                "params": dict(self.params),
                "payload_sha256": _sha256(self.payload),
            }
        )


@dataclass(frozen=True)
class SignedCommand:
    command: Command
    signature: bytes


def sign_command(signer: Signer, command: Command) -> SignedCommand:
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
    events: tuple[TurnEvent, ...]


@dataclass
class _State:
    epoch: int = 0
    seq: int = 0
    grants: dict[str, Grant] | None = None
    events: list[TurnEvent] | None = None
    send_ids: dict[str, str] | None = None  # send id -> event id
    withdrawn_turns: set[str] | None = None
    latest_revision: dict[str, int] | None = None
    nonces: set[str] | None = None

    def __post_init__(self) -> None:
        self.grants = {} if self.grants is None else self.grants
        self.events = [] if self.events is None else self.events
        self.send_ids = {} if self.send_ids is None else self.send_ids
        self.withdrawn_turns = (
            set() if self.withdrawn_turns is None else self.withdrawn_turns
        )
        self.latest_revision = (
            {} if self.latest_revision is None else self.latest_revision
        )
        self.nonces = set() if self.nonces is None else self.nonces


def _state_to_dict(state: _State) -> dict[str, Any]:
    assert state.grants is not None and state.events is not None
    assert state.send_ids is not None and state.withdrawn_turns is not None
    assert state.latest_revision is not None and state.nonces is not None
    return {
        "version": STATE_VERSION,
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
        "nonces": sorted(state.nonces),
    }


def _state_from_dict(raw: dict[str, Any]) -> _State:
    if raw["version"] != STATE_VERSION:
        raise ValueError("version")
    state = _State(epoch=int(raw["epoch"]), seq=int(raw["seq"]))
    assert state.grants is not None and state.events is not None
    assert state.send_ids is not None and state.latest_revision is not None
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
        state.events.append(event)
        state.send_ids[event.send_id] = event.event_id
        state.latest_revision[event.turn_id] = max(
            state.latest_revision.get(event.turn_id, 0), event.revision
        )
    state.withdrawn_turns = set(raw["withdrawn_turns"])
    state.nonces = set(raw["nonces"])
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


class ChatAuthority:
    """Grants and immutable turn events behind injected collaborators."""

    def __init__(
        self,
        *,
        signer: Signer,
        operator: Verifier,
        storage: AuthorityStorage,
        new_id: Callable[[], str],
    ) -> None:
        self._signer = signer
        self._operator = operator
        self._storage = storage
        self._new_id = new_id
        self._lock = threading.Lock()
        self._state = self._load()

    # ---- persistence -------------------------------------------------

    def _load(self) -> _State:
        blob = self._storage.read()
        if blob is None:
            return _State()
        try:
            envelope = json.loads(blob.decode("utf-8"))
            body = _canonical(envelope["state"])
            signature = base64.b64decode(envelope["mac_b64"], validate=True)
            if not self._signer.verify(_STATE_CONTEXT + body, signature):
                raise ValueError("mac")
            state = _state_from_dict(envelope["state"])
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthorityError("state_invalid") from exc
        return state

    def _persist(self, state: _State) -> None:
        body = _state_to_dict(state)
        mac = self._signer.sign(_STATE_CONTEXT + _canonical(body))
        envelope = {"state": body, "mac_b64": base64.b64encode(mac).decode("ascii")}
        self._storage.write(_canonical(envelope))

    def _apply(self, signed: SignedCommand, kind: str, mutate: Callable[[_State], Any]) -> Any:
        """Verify, consume the nonce and mutate a copy; swap only once stored."""
        cmd = signed.command
        if cmd.kind != kind:
            raise AuthorityError("bad_command")
        if not self._operator.verify(cmd.signing_bytes(), signed.signature):
            raise AuthorityError("bad_signature")
        if not cmd.nonce or len(cmd.nonce) > 128 or not cmd.principal:
            raise AuthorityError("bad_command")
        with self._lock:
            draft = copy.deepcopy(self._state)
            assert draft.nonces is not None
            if cmd.nonce in draft.nonces:
                raise AuthorityError("replayed")
            if len(draft.nonces) >= MAX_NONCES:
                raise AuthorityError("capacity")
            result = mutate(draft)
            draft.nonces.add(cmd.nonce)
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
            assert state.grants is not None
            if len(state.grants) >= MAX_GRANTS:
                raise AuthorityError("capacity")
            grant = Grant(
                grant_id=self._new_id(),
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
        grant_id = _str(signed.command.params, "grant_id")

        def mutate(state: _State) -> int:
            assert state.grants is not None
            grant = state.grants.get(grant_id)
            if grant is None:
                raise AuthorityError("unknown_grant")
            if grant.status == GRANT_ACTIVE or (
                status == GRANT_WITHDRAWN and grant.status == GRANT_REVOKED
            ):
                state.epoch += 1
                state.grants[grant_id] = replace(
                    grant, status=status, ended_epoch=state.epoch
                )
            return state.epoch

        epoch: int = self._apply(signed, kind, mutate)
        return epoch

    def revoke_grant(self, signed: SignedCommand) -> int:
        """Stop eligibility under the grant; returns the new epoch."""
        return self._end_grant(signed, KIND_REVOKE, GRANT_REVOKED)

    def withdraw_grant(self, signed: SignedCommand) -> int:
        """Revoke and permanently end eligibility of its captured turns."""
        return self._end_grant(signed, KIND_WITHDRAW_GRANT, GRANT_WITHDRAWN)

    def withdraw_turn(self, signed: SignedCommand) -> int:
        """Permanently end eligibility of every revision of a turn."""
        turn_id = _str(signed.command.params, "turn_id")

        def mutate(state: _State) -> int:
            assert state.latest_revision is not None
            assert state.withdrawn_turns is not None
            if turn_id not in state.latest_revision:
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
            assert state.events is not None and state.send_ids is not None
            assert state.grants is not None and state.latest_revision is not None
            existing_id = state.send_ids.get(send_id)
            if existing_id is not None:
                prior = next(e for e in state.events if e.event_id == existing_id)
                same = (
                    (prior.workspace, prior.project, prior.chat) == scope
                    and prior.text == text
                    and prior.opted_out == opted_out
                    and prior.principal == cmd.principal
                    and (prior.revision > 1) == (corrects is not None)
                )
                if not same:
                    raise AuthorityError("retry_conflict")
                return prior
            if len(state.events) >= MAX_EVENTS:
                raise AuthorityError("capacity")
            turn_id = self._new_id()
            revision = 1
            if corrects is not None:
                base = next(
                    (e for e in state.events if e.turn_id == corrects), None
                )
                if base is None or (base.workspace, base.project, base.chat) != scope:
                    raise AuthorityError("bad_command")
                turn_id = corrects
                revision = state.latest_revision[corrects] + 1
            state.seq += 1
            grant_ids = tuple(
                sorted(
                    g.grant_id
                    for g in state.grants.values()
                    if g.status == GRANT_ACTIVE
                    and (g.workspace, g.project) == scope[:2]
                    and g.chat in (None, scope[2])
                )
            )
            unsigned = TurnEvent(
                event_id=self._new_id(),
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
                unsigned, signature=self._signer.sign(unsigned.signing_bytes())
            )
            state.events.append(event)
            state.send_ids[send_id] = event.event_id
            state.latest_revision[turn_id] = revision
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
        return event.text_sha256 == _sha256(event.text) and self._signer.verify(
            event.signing_bytes(), event.signature
        )

    def eligible_events(
        self,
        *,
        grant_id: str,
        producer: str,
        limit: int,
        chat: str | None = None,
        expected_epoch: int | None = None,
    ) -> Lookup:
        """Newest-first events a producer may use under a live grant.

        Every filter runs before the limit. A revoked, withdrawn, unknown or
        producer-mismatched grant yields nothing; a stale `expected_epoch` is
        an error so the caller re-reads instead of acting on old authority.
        """
        if not 1 <= limit <= MAX_LOOKUP:
            raise AuthorityError("bad_limit")
        with self._lock:
            state = self._state
            if expected_epoch is not None and expected_epoch != state.epoch:
                raise AuthorityError("epoch_changed")
            assert state.grants is not None and state.events is not None
            assert state.withdrawn_turns is not None
            assert state.latest_revision is not None
            grant = state.grants.get(grant_id)
            if (
                grant is None
                or grant.status != GRANT_ACTIVE
                or producer not in grant.producers
            ):
                return Lookup(state.epoch, ())
            picked: list[TurnEvent] = []
            for event in reversed(state.events):
                if len(picked) >= limit:
                    break
                if (
                    grant_id in event.grant_ids
                    and not event.opted_out
                    and event.turn_id not in state.withdrawn_turns
                    and event.revision == state.latest_revision[event.turn_id]
                    and (chat is None or event.chat == chat)
                    and self.verify_event(event)
                ):
                    picked.append(event)
            return Lookup(state.epoch, tuple(picked))

    def grant(self, grant_id: str) -> Grant | None:
        with self._lock:
            assert self._state.grants is not None
            return self._state.grants.get(grant_id)
