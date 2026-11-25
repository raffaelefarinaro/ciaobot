"""Steward authority for one shared brain.

A shared brain is an immutable storage resource a workspace links. Reading it
needs no new authority, but *changing what the team shares* does: a steward is
the one principal allowed to transfer that role, and a recovery has to be
possible when the incumbent is gone. This module is that authority core and
nothing else: no route, no Settings surface, no publisher, no background job.
It is built so the integration slice can supply a protected, conditional store
and a trusted provider backend without this file deciding where either lives.

Five rules shape the design.

* **A principal string is not an identity.** Every entry point takes an
  :class:`AuthenticatedPrincipal` capability, never a caller-supplied name. The
  capability is minted only by the injected :class:`AuthorityBackend` after it
  has authenticated the provider's own credential; the engine re-verifies the
  capability's seal before it uses any field. A forged or expired capability is
  a typed refusal. A fake connector cannot become a steward by claiming a role,
  and an unverifiable connector stays member-only.
* **Authority is conditional, and its effect is admitted.** Bootstrap creates a
  record only when none exists, and every later transition replaces one only at
  the exact generation it observed. More importantly, a transition's mutation
  goes through :meth:`AuthorityBackend.commit`: the backend issues a short-lived
  single-use :class:`MutationAdmission` only after a fresh provider entitlement
  check, and performs the conditional store write under its own enforceable
  fence. A permission lookup immediately before a local compare-and-swap is not
  such a fence, so the authority core never treats one as authority.
* **Acceptance is a second signature.** An administrator may nominate a
  steward, but the nominated principal must accept the invitation itself with
  its own authenticated capability. Every challenge is bound to the brain, the
  storage identity, the actor, the successor, the operation, the observed
  generation and a single-use nonce, with a fixed expiry.
* **Generation rotation is the fence.** Bootstrap starts at generation 1 and
  every transfer or recovery increments it. Completing a transition spends its
  own nonce, invalidates every unspent invitation for that brain, and makes the
  previous generation immediately unusable. A role in one brain says nothing
  about another brain.
* **No backend means member-only, not a weaker guess.** Provider authentication
  and a provider-enforced mutation fence are integration objects. This module
  does not implement GitHub/folder membership, a provider conditional write or
  an atomic cross-system revocation fence. When that backend is absent, or it
  cannot issue an admission, every privileged transition fails closed with
  ``provider_fencing_unavailable`` and the ordinary member surface is all that
  remains. An ordinary folder or a generic Git remote is member-only even when
  a capability object is presented.

**Production activation gates.** This core is deliberately inactive for real
brains. Before any connector may enable a privileged transition, the
integration owner must supply and verify four things, and until then the
backend stays absent and every such transition fails closed with
``provider_fencing_unavailable``:

1. **Credential authentication.** ``authenticate`` exchanges a provider session
   or token for a short-lived sealed :class:`AuthenticatedPrincipal`; it must
   never mint a capability from a caller-supplied principal name.
2. **Shared atomic admissions.** ``admit``/``commit`` run against a durable
   admission store shared by every engine process. An admission is single-use,
   expiry-bounded and never revived by a restart.
3. **Provider fencing.** ``commit`` performs the conditional record mutation
   under the provider's own enforceable fence (for example an ``If-Match``
   revision or a provider transaction). A local file lock, a fresh role lookup
   or a prior successful write is not a fence for a provider-side revocation.
4. **Durable audit and crash recovery.** The old/new principals, actor and
   generation are recorded in restricted durable audit, and an interrupted
   commit is reconciled after a crash as committed or not committed without
   replaying a spent admission. A local in-memory audit trail is evidence for
   tests, not the production record.

The clock, the store and the provider backend are injected, which is how the
tests reach expiry, races and failure modes without sleeping or touching live
data.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from ciao.os_support.files import open_fd, replace_file
from ciao.os_support.locks import lock_exclusive, unlock
from ciao.os_support.private import (
    is_private_descriptor_strict,
    make_private_dir,
    mkstemp_private,
    open_private,
)

#: How long an invitation stays usable. Fixed, not sliding: accepting a
#: challenge never extends it.
CHALLENGE_TTL_S = 300.0

#: Invitations retained in memory. They are short-lived and bounded so a caller
#: that mints without accepting cannot grow the engine without limit.
MAX_CHALLENGES = 128

#: Transitions retained in the restricted in-process audit. The integration
#: slice owns durable audit; this keeps the core reviewable and bounded.
MAX_AUDIT_RECORDS = 256

#: Nonce strength: 32 random bytes, URL-safe.
_NONCE_BYTES = 32

#: File-backed store schema. A document with any other schema is corrupt and is
#: refused rather than read as an empty authority.
SCHEMA_VERSION = 1

#: Directory opens for the root checks and the post-rename durability flush.
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)


class ConnectorKind(str, Enum):
    """The storage kinds this core knows about."""

    GIT = "git"
    FOLDER = "folder"


class Operation(str, Enum):
    """The three transitions an authority record can admit."""

    BOOTSTRAP = "bootstrap"
    TRANSFER = "transfer"
    RECOVERY = "recovery"


class AuthorityError(Exception):
    """A typed refusal from the authority core.

    ``reason`` is a closed vocabulary so a route can map it to a status code
    without parsing prose. Nothing here echoes a nonce, a capability or a store
    path into the message.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StoreError(Exception):
    """The protected store could not be read, verified or written.

    ``reason`` is one of ``corrupt``, ``unsafe_root``, ``unsafe_store``,
    ``wrong_resource``, ``invalid_record`` or ``durability_unavailable``. The
    authority engine turns every one of them into ``store_unavailable``: a
    store it cannot prove is healthy and durable supplies no authority.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BrainConnector(Protocol):
    """The immutable resource a connector addresses.

    A connector is a value, not a trust decision. The engine reads ``brain``,
    ``storage`` and ``kind``; the trusted backend supplies every identity and
    role, and a method or flag on the connector itself is never authority.
    """

    brain: str
    storage: str
    kind: ConnectorKind


@dataclass(frozen=True, slots=True)
class BrainRef:
    """A concrete :class:`BrainConnector` for callers and tests."""

    brain: str
    storage: str
    kind: ConnectorKind = ConnectorKind.GIT

    def __post_init__(self) -> None:
        if not isinstance(self.brain, str) or not self.brain.strip():
            raise ValueError("a brain reference needs a non-empty brain identity")
        if not isinstance(self.storage, str) or not self.storage.strip():
            raise ValueError("a brain reference needs a non-empty storage identity")


@dataclass(frozen=True, slots=True)
class ProviderCredential:
    """An opaque provider credential (session, cookie or token).

    The authority core never reads this value. Only the injected
    :class:`AuthorityBackend` may exchange it for an authenticated capability,
    and it must verify the provider's own session rather than accept a name.
    """

    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """A provider-verified principal capability bound to one resource.

    The capability is minted only by the trusted backend. ``seal`` is an
    unforgeable backend marker over every other field; a caller-built instance
    of this dataclass is refused by :meth:`AuthorityBackend.verify_principal`.
    ``revision`` and ``source`` name the provider observation and ``expires_at``
    keeps a copied capability from acting indefinitely.
    """

    principal: str
    brain: str
    storage: str
    member: bool
    admin: bool
    revision: str
    source: str
    authenticated_at: float
    expires_at: float
    seal: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class MutationAdmission:
    """A short-lived, single-use authority to apply one exact transition.

    It is issued by the trusted backend after a fresh provider entitlement
    check, and carries the provider's own ``fence`` token. The engine verifies
    the seal and every binding before asking the backend to commit. The backend
    performs the store mutation inside its fence; a stale fence or a revocation
    observed at that moment is ``admission_rejected``, not a successful write.
    """

    admission_id: str = field(repr=False)
    actor: str
    successor: str
    brain: str
    storage: str
    operation: Operation
    generation: int
    fence: str
    issued_at: float
    expires_at: float
    seal: str = field(repr=False)


class CommitOutcome(str, Enum):
    """What a fenced mutation did."""

    COMMITTED = "committed"
    CONDITION_FAILED = "condition_failed"
    REJECTED = "rejected"


class AuthorityBackend(Protocol):
    """The trusted provider integration: authentication plus a real fence.

    An implementation must verify the provider's credential or session before
    minting an :class:`AuthenticatedPrincipal`; a role lookup for a
    caller-supplied name is exactly what this seam forbids. ``admit`` receives
    the sealed actor and successor capabilities, must verify them and re-read
    current entitlement, and returns an admission only when it can make the
    following ``commit`` conditional on an enforceable provider revision.
    ``commit`` must call ``mutation`` only while that fence is valid and must
    report ``REJECTED`` for a revocation or stale fence. When the provider
    cannot offer that guarantee, return ``None`` from ``admit``: the core then
    fails closed instead of pretending a local check is authority.
    """

    def authenticate(
        self, connector: BrainConnector, credential: ProviderCredential
    ) -> AuthenticatedPrincipal | None:
        ...

    def verify_principal(self, capability: AuthenticatedPrincipal) -> bool:
        ...

    def admit(
        self,
        *,
        connector: BrainConnector,
        operation: Operation,
        generation: int,
        actor: AuthenticatedPrincipal,
        successor: AuthenticatedPrincipal,
    ) -> MutationAdmission | None:
        ...

    def verify_admission(self, admission: MutationAdmission) -> bool:
        ...

    def commit(
        self,
        admission: MutationAdmission,
        mutation: Callable[[], bool],
    ) -> CommitOutcome:
        ...


@dataclass(frozen=True, slots=True)
class AuthorityRecord:
    """One brain's protected stewardship record.

    The record names one steward at one generation. It carries no credential:
    every privileged call presents a fresh authenticated capability and every
    mutation is admitted by :class:`AuthorityBackend`, so a copied record alone
    cannot act.
    """

    brain: str
    storage: str
    connector: str
    generation: int
    steward: str
    created_at: float
    updated_at: float

    def to_payload(self) -> dict[str, object]:
        return {
            "brain": self.brain,
            "storage": self.storage,
            "connector": self.connector,
            "generation": self.generation,
            "steward": self.steward,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class Challenge:
    """A single-use invitation to complete one authority transition.

    ``nonce`` is returned once and is excluded from the repr so a diagnostic
    dump does not spread an invitation. Every other field is the binding the
    acceptance must match: a challenge for another brain, storage resource,
    actor, successor, operation or generation is not this challenge.
    """

    nonce: str = field(repr=False)
    brain: str
    storage: str
    actor: str
    successor: str
    operation: Operation
    generation: int
    issued_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class AuthorityTransition:
    """The restricted audit row a completed transition returns.

    ``actor`` is the principal that began the transition (the administrator for
    bootstrap and recovery, the incumbent for a transfer); ``previous_steward``
    and ``steward`` are the old and new holders. It contains no nonce and no
    evidence.
    """

    operation: Operation
    brain: str
    storage: str
    actor: str
    previous_steward: str | None
    steward: str
    previous_generation: int
    generation: int
    at: float


@dataclass
class _StoredChallenge:
    """The retained half of a minted challenge: everything but the nonce.

    The authenticated actor capability stays here so acceptance can hand the
    backend a sealed identity, not a caller-supplied name. The challenge's own
    expiry, not the actor capability's, bounds the invitation.
    """

    brain: str
    storage: str
    actor: str
    actor_capability: AuthenticatedPrincipal
    successor: str
    operation: Operation
    generation: int
    issued_at: float
    expires_at: float
    used: bool = False


def _digest(value: str) -> str:
    """SHA-256 of a UTF-8 string, as lowercase hex.

    A caller can hand over a lone surrogate; that is not a nonce and names
    nothing, so it raises ``ValueError`` for the claim path to turn into
    ``challenge_unknown``.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _required_principal(value: str, reason: str) -> str:
    """A non-empty principal, or ``AuthorityError(reason)``."""
    if not isinstance(value, str) or not value.strip():
        raise AuthorityError(reason)
    return value


class ConditionalAuthorityStore(Protocol):
    """The protected storage contract.

    The integration owner supplies the location. What this core requires is the
    conditionality: a create succeeds only when no record exists, and a replace
    succeeds only at the exact generation the caller observed. A store that
    cannot promise those two answers cannot back a steward.
    """

    def load(self, brain: str) -> AuthorityRecord | None:
        ...

    def create_if_absent(self, record: AuthorityRecord) -> bool:
        ...

    def replace_if_generation(self, record: AuthorityRecord, expected_generation: int) -> bool:
        ...


def _record_from_payload(payload: dict[str, Any]) -> AuthorityRecord:
    """A validated record, or ``StoreError("corrupt")``.

    Every field is checked rather than coerced: a generation of ``True`` or a
    non-string steward is not a record this module will act on.
    """
    try:
        brain = payload["brain"]
        storage = payload["storage"]
        connector = payload["connector"]
        generation = payload["generation"]
        steward = payload["steward"]
        created_at = payload["created_at"]
        updated_at = payload["updated_at"]
    except KeyError:
        raise StoreError("corrupt") from None
    if not all(isinstance(value, str) and value for value in (brain, storage, connector, steward)):
        raise StoreError("corrupt")
    if type(generation) is not int or generation < 1:
        raise StoreError("corrupt")
    if connector not in {kind.value for kind in ConnectorKind}:
        raise StoreError("corrupt")
    if not all(type(value) in (int, float) for value in (created_at, updated_at)):
        raise StoreError("corrupt")
    return AuthorityRecord(
        brain=brain,
        storage=storage,
        connector=connector,
        generation=generation,
        steward=steward,
        created_at=float(created_at),
        updated_at=float(updated_at),
    )


class ProtectedAuthorityStore:
    """A private, conditional, file-backed authority store.

    ``root`` is injected. The directory is restricted to its owner (``0700``)
    and the one document to ``0600``; it must sit outside member-editable notes,
    but that placement is the integration owner's decision, not this module's.
    Every read and write opens the directory by descriptor and runs the strict
    owner/mode/platform-ACL verifier before trusting it; a mode-private
    directory that still carries an ACL grant is refused. Reads use an atomic
    replace's guarantee and verify the document descriptor the same way.
    Writes take an interprocess ``flock`` as well as a thread lock, re-read the
    document under the lock, apply one condition, replace it atomically and
    ``fsync`` the containing directory so the committed generation survives a
    crash. A missing document is "no authority yet"; a malformed, unsafe or
    not-durable one is refused.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)
        if self._root.is_symlink():
            raise StoreError("unsafe_root")
        try:
            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
            make_private_dir(self._root)
        except OSError:
            raise StoreError("unsafe_root") from None
        if not self._root.is_dir() or self._root.is_symlink():
            raise StoreError("unsafe_root")
        self._path = self._root / "authority.json"
        self._lock_path = self._root / "authority.lock"
        self._thread_lock = threading.Lock()
        self._require_private_root()

    def load(self, brain: str) -> AuthorityRecord | None:
        return self._read_records().get(brain)

    def create_if_absent(self, record: AuthorityRecord) -> bool:
        self._validate_new_record(record)
        with self._locked():
            records = self._read_records()
            if record.brain in records:
                return False
            records[record.brain] = record
            self._write_records(records)
            return True

    def replace_if_generation(
        self, record: AuthorityRecord, expected_generation: int
    ) -> bool:
        self._validate_new_record(record)
        with self._locked():
            records = self._read_records()
            current = records.get(record.brain)
            if current is None:
                return False
            if current.storage != record.storage or current.connector != record.connector:
                raise StoreError("wrong_resource")
            if current.generation != expected_generation:
                return False
            if record.generation != expected_generation + 1:
                raise StoreError("invalid_record")
            records[record.brain] = record
            self._write_records(records)
            return True

    def _validate_new_record(self, record: AuthorityRecord) -> None:
        if (
            not record.brain
            or not record.storage
            or not record.steward
            or type(record.generation) is not int
            or record.generation < 1
            or record.connector not in {kind.value for kind in ConnectorKind}
        ):
            raise StoreError("invalid_record")

    def _private_root_fd(self) -> int:
        """An open root descriptor that passed the strict owner/mode/ACL check."""
        try:
            descriptor = open_fd(
                self._root, _DIRECTORY_FLAGS, follow_symlinks=False
            )
        except OSError:
            raise StoreError("unsafe_root") from None
        try:
            try:
                private = is_private_descriptor_strict(descriptor)
            except OSError:
                raise StoreError("unsafe_root") from None
            if not private:
                raise StoreError("unsafe_root")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _require_private_root(self) -> None:
        descriptor = self._private_root_fd()
        os.close(descriptor)

    def _fsync_root(self) -> None:
        """Flush the directory entry of the just-replaced document.

        The document itself was flushed before the rename; this makes the
        rename durable. Failure is reported, never swallowed: the caller must
        treat the committed generation as unconfirmed and reconcile it by a
        fresh read rather than claim a durable transition.
        """
        descriptor = self._private_root_fd()
        try:
            try:
                os.fsync(descriptor)
            except OSError:
                raise StoreError("durability_unavailable") from None
        finally:
            os.close(descriptor)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            self._require_private_root()
            try:
                descriptor = open_private(
                    self._lock_path,
                    os.O_RDWR,
                    0o600,
                    follow_symlinks=False,
                )
            except OSError:
                raise StoreError("unsafe_store") from None
            try:
                try:
                    if not is_private_descriptor_strict(descriptor):
                        raise StoreError("unsafe_store")
                except OSError:
                    raise StoreError("unsafe_store") from None
                lock_exclusive(descriptor)
                try:
                    yield
                finally:
                    unlock(descriptor)
            finally:
                os.close(descriptor)

    def _read_records(self) -> dict[str, AuthorityRecord]:
        self._require_private_root()
        try:
            descriptor = open_fd(self._path, os.O_RDONLY, follow_symlinks=False)
        except FileNotFoundError:
            return {}
        except OSError:
            raise StoreError("unsafe_store") from None
        try:
            try:
                private = is_private_descriptor_strict(descriptor)
            except OSError:
                raise StoreError("unsafe_store") from None
            if not private:
                raise StoreError("unsafe_store")
            handle = os.fdopen(descriptor, "r", encoding="utf-8")
            descriptor = -1
        except BaseException:
            # Only the paths before ``fdopen`` still own the raw descriptor;
            # once it owns the descriptor the ``with`` below closes it.
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise
        try:
            with handle:
                raw = handle.read()
        except (OSError, UnicodeError):
            raise StoreError("corrupt") from None
        return self._decode(raw)

    def _decode(self, raw: str) -> dict[str, AuthorityRecord]:
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise StoreError("corrupt") from None
        if (
            not isinstance(document, dict)
            or type(document.get("schema")) is not int
            or document.get("schema") != SCHEMA_VERSION
        ):
            raise StoreError("corrupt")
        brains = document.get("brains")
        if not isinstance(brains, dict):
            raise StoreError("corrupt")
        records: dict[str, AuthorityRecord] = {}
        for key, payload in brains.items():
            if not isinstance(key, str) or not isinstance(payload, dict):
                raise StoreError("corrupt")
            record = _record_from_payload(payload)
            if record.brain != key or record.brain in records:
                raise StoreError("corrupt")
            records[key] = record
        return records

    def _write_records(self, records: dict[str, AuthorityRecord]) -> None:
        self._require_private_root()
        document = {
            "schema": SCHEMA_VERSION,
            "brains": {
                brain: records[brain].to_payload() for brain in sorted(records)
            },
        }
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
        descriptor, temp_name = mkstemp_private(
            dir=self._root, prefix=".authority.", suffix=".tmp"
        )
        temporary = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            replace_file(temporary, self._path)
            self._fsync_root()
        finally:
            temporary.unlink(missing_ok=True)


class StewardAuthority:
    """Bootstrap, transfer and recovery for one protected authority store.

    The engine holds only pending invitations and a bounded audit trail; the
    durable record lives in the injected :class:`ConditionalAuthorityStore`.
    Privileged entry points accept only :class:`AuthenticatedPrincipal`
    capabilities minted by the injected :class:`AuthorityBackend`, and every
    mutation is committed through that backend's fenced admission. A missing
    backend, a forged capability, a revoked principal or an unavailable fence
    is a typed refusal; none of them is converted into a weaker local check.
    """

    def __init__(
        self,
        store: ConditionalAuthorityStore,
        *,
        backend: AuthorityBackend | None = None,
        clock: Callable[[], float] = time.monotonic,
        challenge_ttl_s: float = CHALLENGE_TTL_S,
    ) -> None:
        if challenge_ttl_s <= 0:
            raise ValueError("a challenge needs a positive lifetime")
        self._store = store
        self._backend = backend
        self._clock = clock
        self._challenge_ttl_s = float(challenge_ttl_s)
        self._lock = threading.Lock()
        self._challenges: dict[str, _StoredChallenge] = {}
        self._audit: list[AuthorityTransition] = []

    # ── Transitions ──────────────────────────────────────────────────────

    def begin_bootstrap(
        self,
        *,
        connector: BrainConnector,
        admin: AuthenticatedPrincipal,
        successor: str,
    ) -> Challenge:
        """Nominate the first steward through an authenticated administrator.

        ``successor`` is only the provider identity being invited; it is not
        authentication. The nominated principal must later accept with its own
        authenticated capability, and the backend must admit that acceptance
        under its provider fence.
        """
        self._kind(connector)
        admin = self._verified(connector, admin, admin=True)
        successor = _required_principal(successor, "invalid_successor")
        if self._record(connector) is not None:
            raise AuthorityError("already_initialized")
        return self._mint(
            connector=connector,
            actor=admin,
            successor=successor,
            operation=Operation.BOOTSTRAP,
            generation=0,
        )

    def begin_transfer(
        self,
        *,
        connector: BrainConnector,
        incumbent: AuthenticatedPrincipal,
        successor: str,
        expected_generation: int,
    ) -> Challenge:
        """The authenticated current steward nominates a successor.

        Before the successor accepts, the incumbent still holds the record and
        the successor holds nothing. The invitation is pinned to the generation
        the incumbent observed, so a transfer that loses a race cannot replace a
        newer steward.
        """
        self._kind(connector)
        incumbent = self._verified(connector, incumbent, admin=False)
        successor = _required_principal(successor, "invalid_successor")
        if successor == incumbent.principal:
            raise AuthorityError("invalid_successor")
        record = self._record_required(connector)
        if record.generation != expected_generation:
            raise AuthorityError("stale_generation")
        if record.steward != incumbent.principal:
            raise AuthorityError("not_steward")
        return self._mint(
            connector=connector,
            actor=incumbent,
            successor=successor,
            operation=Operation.TRANSFER,
            generation=record.generation,
        )

    def begin_recovery(
        self,
        *,
        connector: BrainConnector,
        admin: AuthenticatedPrincipal,
        successor: str,
        expected_generation: int,
        confirmed: bool,
    ) -> Challenge:
        """An authenticated administrator starts recovery for an absent incumbent.

        Recovery does not need the incumbent's participation, which is the
        point: it needs a fresh administrator capability, an explicit
        confirmation, the exact current generation and the successor's own
        authenticated acceptance under a provider fence.
        """
        self._kind(connector)
        admin = self._verified(connector, admin, admin=True)
        successor = _required_principal(successor, "invalid_successor")
        if not confirmed:
            raise AuthorityError("confirmation_required")
        record = self._record_required(connector)
        if record.generation != expected_generation:
            raise AuthorityError("stale_generation")
        if successor == record.steward:
            raise AuthorityError("invalid_successor")
        return self._mint(
            connector=connector,
            actor=admin,
            successor=successor,
            operation=Operation.RECOVERY,
            generation=record.generation,
        )

    def accept_bootstrap(
        self, nonce: str, *, connector: BrainConnector, successor: AuthenticatedPrincipal
    ) -> AuthorityTransition:
        """The nominated successor accepts, creating generation 1 only if absent."""
        self._kind(connector)
        if not isinstance(successor, AuthenticatedPrincipal):
            raise AuthorityError("forged_capability")
        stored = self._peek(nonce, connector, Operation.BOOTSTRAP, successor.principal)
        successor = self._verified(connector, successor, admin=False)
        self._verified(connector, stored.actor_capability, admin=True)
        stored = self._claim(nonce, connector, Operation.BOOTSTRAP, successor.principal)
        now = self._clock()
        record = AuthorityRecord(
            brain=connector.brain,
            storage=connector.storage,
            connector=self._kind(connector).value,
            generation=1,
            steward=successor.principal,
            created_at=now,
            updated_at=now,
        )
        self._fenced_commit(
            connector=connector,
            operation=Operation.BOOTSTRAP,
            generation=0,
            actor=stored.actor_capability,
            successor=successor,
            condition_reason="already_initialized",
            mutation=lambda: self._create(record),
        )
        self._invalidate_brain(connector.brain)
        return self._record_transition(
            operation=Operation.BOOTSTRAP,
            connector=connector,
            actor=stored.actor,
            previous_steward=None,
            steward=successor.principal,
            previous_generation=0,
            generation=1,
            at=now,
        )

    def accept_transfer(
        self, nonce: str, *, connector: BrainConnector, successor: AuthenticatedPrincipal
    ) -> AuthorityTransition:
        """The nominated successor accepts a transfer, rotating the generation."""
        self._kind(connector)
        if not isinstance(successor, AuthenticatedPrincipal):
            raise AuthorityError("forged_capability")
        stored = self._peek(nonce, connector, Operation.TRANSFER, successor.principal)
        successor = self._verified(connector, successor, admin=False)
        self._verified(connector, stored.actor_capability, admin=False)
        stored = self._claim(nonce, connector, Operation.TRANSFER, successor.principal)
        record = self._record_required(connector)
        if record.generation != stored.generation:
            raise AuthorityError("stale_generation")
        if record.steward != stored.actor:
            raise AuthorityError("not_steward")
        now = self._clock()
        updated = AuthorityRecord(
            brain=record.brain,
            storage=record.storage,
            connector=record.connector,
            generation=record.generation + 1,
            steward=successor.principal,
            created_at=record.created_at,
            updated_at=now,
        )
        self._fenced_commit(
            connector=connector,
            operation=Operation.TRANSFER,
            generation=record.generation,
            actor=stored.actor_capability,
            successor=successor,
            condition_reason="stale_generation",
            mutation=lambda: self._replace(updated, record.generation),
        )
        self._invalidate_brain(connector.brain)
        return self._record_transition(
            operation=Operation.TRANSFER,
            connector=connector,
            actor=stored.actor,
            previous_steward=record.steward,
            steward=successor.principal,
            previous_generation=record.generation,
            generation=updated.generation,
            at=now,
        )

    def accept_recovery(
        self, nonce: str, *, connector: BrainConnector, successor: AuthenticatedPrincipal
    ) -> AuthorityTransition:
        """The nominated successor accepts recovery, rotating the generation."""
        self._kind(connector)
        if not isinstance(successor, AuthenticatedPrincipal):
            raise AuthorityError("forged_capability")
        stored = self._peek(nonce, connector, Operation.RECOVERY, successor.principal)
        successor = self._verified(connector, successor, admin=False)
        self._verified(connector, stored.actor_capability, admin=True)
        stored = self._claim(nonce, connector, Operation.RECOVERY, successor.principal)
        record = self._record_required(connector)
        if record.generation != stored.generation:
            raise AuthorityError("stale_generation")
        now = self._clock()
        updated = AuthorityRecord(
            brain=record.brain,
            storage=record.storage,
            connector=record.connector,
            generation=record.generation + 1,
            steward=successor.principal,
            created_at=record.created_at,
            updated_at=now,
        )
        self._fenced_commit(
            connector=connector,
            operation=Operation.RECOVERY,
            generation=record.generation,
            actor=stored.actor_capability,
            successor=successor,
            condition_reason="stale_generation",
            mutation=lambda: self._replace(updated, record.generation),
        )
        self._invalidate_brain(connector.brain)
        return self._record_transition(
            operation=Operation.RECOVERY,
            connector=connector,
            actor=stored.actor,
            previous_steward=record.steward,
            steward=successor.principal,
            previous_generation=record.generation,
            generation=updated.generation,
            at=now,
        )

    # ── Authorization ────────────────────────────────────────────────────

    def authenticate(
        self, *, connector: BrainConnector, credential: ProviderCredential
    ) -> AuthenticatedPrincipal:
        """Exchange a provider credential for a verified principal capability.

        Integration calls this before a privileged entry point. The engine does
        not accept a name as a substitute: the backend must verify the provider
        session and seal the capability.
        """
        self._kind(connector)
        if self._backend is None:
            raise AuthorityError("provider_fencing_unavailable")
        capability = self._backend.authenticate(connector, credential)
        if capability is None:
            raise AuthorityError("not_authenticated")
        return self._verified(connector, capability, admin=False)

    def authorize_steward(
        self,
        *,
        connector: BrainConnector,
        principal: AuthenticatedPrincipal,
        generation: int | None = None,
    ) -> AuthorityRecord:
        """The current record, when ``principal`` is its steward right now.

        ``generation`` is the fence a caller captured before a privileged
        effect. Passing it makes a transfer or recovery that landed in between
        a ``stale_generation`` refusal rather than an effect under old
        authority. This is a read-side check only: a privileged mutation must
        still be committed through :meth:`AuthorityBackend.commit`, because a
        check immediately before a local write does not fence a provider
        revocation.
        """
        self._kind(connector)
        principal = self._verified(connector, principal, admin=False)
        record = self._record_required(connector)
        if generation is not None and record.generation != generation:
            raise AuthorityError("stale_generation")
        if record.steward != principal.principal:
            raise AuthorityError("not_steward")
        return record

    def audit_trail(self) -> tuple[AuthorityTransition, ...]:
        """The bounded in-process audit, oldest first. Integration owns durable audit."""
        with self._lock:
            return tuple(self._audit)

    # ── Internals ────────────────────────────────────────────────────────

    def _kind(self, connector: BrainConnector) -> ConnectorKind:
        try:
            kind = ConnectorKind(connector.kind)
        except (TypeError, ValueError):
            raise AuthorityError("member_only") from None
        if not isinstance(connector.brain, str) or not connector.brain.strip():
            raise AuthorityError("member_only")
        if not isinstance(connector.storage, str) or not connector.storage.strip():
            raise AuthorityError("member_only")
        return kind

    def _verified(
        self,
        connector: BrainConnector,
        capability: AuthenticatedPrincipal,
        *,
        admin: bool,
    ) -> AuthenticatedPrincipal:
        """A verified capability for ``connector``, or a typed refusal.

        A folder is member-only before any backend is consulted. A missing
        backend is ``provider_fencing_unavailable``: a local store alone is not
        a provider fence. A caller-built capability, a capability from another
        backend, an expired one or one bound to another resource is refused.
        """
        if self._kind(connector) is ConnectorKind.FOLDER:
            raise AuthorityError("member_only")
        if self._backend is None:
            raise AuthorityError("provider_fencing_unavailable")
        if not isinstance(capability, AuthenticatedPrincipal):
            raise AuthorityError("forged_capability")
        if (
            not isinstance(capability.principal, str)
            or not isinstance(capability.brain, str)
            or not isinstance(capability.storage, str)
            or not isinstance(capability.revision, str)
            or not isinstance(capability.source, str)
            or not isinstance(capability.member, bool)
            or not isinstance(capability.admin, bool)
            or type(capability.authenticated_at) not in (int, float)
            or type(capability.expires_at) not in (int, float)
        ):
            raise AuthorityError("forged_capability")
        try:
            authentic = self._backend.verify_principal(capability)
        except Exception as exc:  # noqa: BLE001 - an unverifiable backend is no authority
            raise AuthorityError("provider_fencing_unavailable") from exc
        if not authentic:
            raise AuthorityError("forged_capability")
        now = self._clock()
        if now >= capability.expires_at or now < capability.authenticated_at:
            raise AuthorityError("expired_capability")
        if capability.brain != connector.brain or capability.storage != connector.storage:
            raise AuthorityError("wrong_resource")
        if not capability.principal.strip():
            raise AuthorityError("not_authenticated")
        if not capability.revision.strip() or not capability.source.strip():
            raise AuthorityError("forged_capability")
        if not (capability.member or capability.admin):
            raise AuthorityError("not_member")
        if admin and not capability.admin:
            raise AuthorityError("not_admin")
        return capability

    def _fenced_commit(
        self,
        *,
        connector: BrainConnector,
        operation: Operation,
        generation: int,
        actor: AuthenticatedPrincipal,
        successor: AuthenticatedPrincipal,
        condition_reason: str,
        mutation: Callable[[], bool],
    ) -> None:
        """Apply one transition only through a provider-backed admission.

        The backend mints the admission from the sealed actor and successor
        capabilities after a fresh entitlement check, and runs ``mutation``
        inside its own fence. The engine validates the admission's seal and
        every binding first. A local permission check that happened earlier is
        never accepted in place of this call.
        """
        if self._backend is None:
            raise AuthorityError("provider_fencing_unavailable")
        try:
            admission = self._backend.admit(
                connector=connector,
                operation=operation,
                generation=generation,
                actor=actor,
                successor=successor,
            )
        except Exception as exc:  # noqa: BLE001 - an unavailable fence is fail-closed
            raise AuthorityError("provider_fencing_unavailable") from exc
        if admission is None:
            raise AuthorityError("provider_fencing_unavailable")
        if not isinstance(admission, MutationAdmission):
            raise AuthorityError("forged_admission")
        try:
            authentic = self._backend.verify_admission(admission)
        except Exception:  # noqa: BLE001 - unverifiable admission is no admission
            authentic = False
        if not authentic:
            raise AuthorityError("forged_admission")
        if (
            not isinstance(admission.actor, str)
            or not isinstance(admission.successor, str)
            or not isinstance(admission.brain, str)
            or not isinstance(admission.storage, str)
            or not isinstance(admission.fence, str)
            or not isinstance(admission.admission_id, str)
            or not isinstance(admission.operation, Operation)
            or type(admission.generation) is not int
            or type(admission.issued_at) not in (int, float)
            or type(admission.expires_at) not in (int, float)
        ):
            raise AuthorityError("forged_admission")
        if (
            admission.actor != actor.principal
            or admission.successor != successor.principal
            or admission.brain != connector.brain
            or admission.storage != connector.storage
            or admission.operation is not operation
            or admission.generation != generation
        ):
            raise AuthorityError("admission_mismatch")
        now = self._clock()
        if now >= admission.expires_at or now < admission.issued_at:
            raise AuthorityError("admission_expired")
        try:
            outcome = self._backend.commit(admission, mutation)
        except AuthorityError:
            raise
        except Exception as exc:  # noqa: BLE001 - an unavailable fence is fail-closed
            raise AuthorityError("provider_fencing_unavailable") from exc
        if outcome is CommitOutcome.REJECTED:
            raise AuthorityError("admission_rejected")
        if outcome is CommitOutcome.CONDITION_FAILED:
            raise AuthorityError(condition_reason)
        if outcome is not CommitOutcome.COMMITTED:
            raise AuthorityError("provider_fencing_unavailable")

    def _record(self, connector: BrainConnector) -> AuthorityRecord | None:
        try:
            return self._store.load(connector.brain)
        except StoreError as exc:
            raise AuthorityError("store_unavailable") from exc

    def _record_required(self, connector: BrainConnector) -> AuthorityRecord:
        record = self._record(connector)
        if record is None:
            raise AuthorityError("not_initialized")
        if record.storage != connector.storage or record.connector != self._kind(connector).value:
            raise AuthorityError("wrong_resource")
        return record

    def _create(self, record: AuthorityRecord) -> bool:
        try:
            return self._store.create_if_absent(record)
        except StoreError as exc:
            raise AuthorityError("store_unavailable") from exc

    def _replace(self, record: AuthorityRecord, expected_generation: int) -> bool:
        try:
            return self._store.replace_if_generation(record, expected_generation)
        except StoreError as exc:
            raise AuthorityError("store_unavailable") from exc

    def _mint(
        self,
        *,
        connector: BrainConnector,
        actor: AuthenticatedPrincipal,
        successor: str,
        operation: Operation,
        generation: int,
    ) -> Challenge:
        now = self._clock()
        nonce = secrets.token_urlsafe(_NONCE_BYTES)
        digest = _digest(nonce)
        stored = _StoredChallenge(
            brain=connector.brain,
            storage=connector.storage,
            actor=actor.principal,
            actor_capability=actor,
            successor=successor,
            operation=operation,
            generation=generation,
            issued_at=now,
            expires_at=now + self._challenge_ttl_s,
        )
        with self._lock:
            self._make_room_locked(now)
            self._challenges[digest] = stored
        return Challenge(
            nonce=nonce,
            brain=stored.brain,
            storage=stored.storage,
            actor=stored.actor,
            successor=stored.successor,
            operation=stored.operation,
            generation=stored.generation,
            issued_at=stored.issued_at,
            expires_at=stored.expires_at,
        )

    def _peek(
        self,
        nonce: str,
        connector: BrainConnector,
        operation: Operation,
        successor: str,
    ) -> _StoredChallenge:
        """Validate every binding without spending the nonce.

        Acceptance uses this before authenticating the successor and the
        stored actor. A forged capability must not be able to burn a real
        invitation by naming the invited principal.
        """
        return self._challenge(
            nonce, connector, operation, successor, consume=False
        )

    def _claim(
        self,
        nonce: str,
        connector: BrainConnector,
        operation: Operation,
        successor: str,
    ) -> _StoredChallenge:
        """Re-check every binding and spend the nonce in one locked step."""
        return self._challenge(
            nonce, connector, operation, successor, consume=True
        )

    def _challenge(
        self,
        nonce: str,
        connector: BrainConnector,
        operation: Operation,
        successor: str,
        *,
        consume: bool,
    ) -> _StoredChallenge:
        digest: str | None
        try:
            digest = _digest(nonce) if isinstance(nonce, str) and nonce else None
        except (UnicodeEncodeError, AttributeError):
            digest = None
        now = self._clock()
        with self._lock:
            if digest is None:
                raise AuthorityError("challenge_unknown")
            stored = self._challenges.get(digest)
            if stored is None:
                raise AuthorityError("challenge_unknown")
            if now >= stored.expires_at:
                del self._challenges[digest]
                raise AuthorityError("challenge_expired")
            if stored.used:
                raise AuthorityError("challenge_used")
            if stored.brain != connector.brain or stored.storage != connector.storage:
                raise AuthorityError("challenge_wrong_resource")
            if stored.operation is not operation:
                raise AuthorityError("challenge_wrong_operation")
            if stored.successor != successor:
                raise AuthorityError("successor_mismatch")
            if consume:
                stored.used = True
            return stored

    def _invalidate_brain(self, brain: str) -> None:
        """Drop every unspent invitation for ``brain``; a spent one stays spent."""
        with self._lock:
            for digest, stored in list(self._challenges.items()):
                if stored.brain == brain and not stored.used:
                    del self._challenges[digest]

    def _make_room_locked(self, now: float) -> None:
        """Keep the pending invitation map bounded without losing a fresh one."""
        if len(self._challenges) < MAX_CHALLENGES:
            return
        for digest, stored in list(self._challenges.items()):
            if stored.used or now >= stored.expires_at:
                del self._challenges[digest]
        while len(self._challenges) >= MAX_CHALLENGES:
            oldest = min(
                self._challenges,
                key=lambda digest: self._challenges[digest].issued_at,
            )
            del self._challenges[oldest]

    def _record_transition(
        self,
        *,
        operation: Operation,
        connector: BrainConnector,
        actor: str,
        previous_steward: str | None,
        steward: str,
        previous_generation: int,
        generation: int,
        at: float,
    ) -> AuthorityTransition:
        transition = AuthorityTransition(
            operation=operation,
            brain=connector.brain,
            storage=connector.storage,
            actor=actor,
            previous_steward=previous_steward,
            steward=steward,
            previous_generation=previous_generation,
            generation=generation,
            at=at,
        )
        with self._lock:
            self._audit.append(transition)
            if len(self._audit) > MAX_AUDIT_RECORDS:
                del self._audit[0]
        return transition
