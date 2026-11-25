"""Steward authority core: capabilities, fenced admissions and their fences.

Pure unit tests over an injected clock, a temporary protected store and a
synthetic provider backend: no routes, no Settings, no live repository, no
sleeps. The synthetic backend stands in for the integration seam Terra required:
it authenticates provider credentials into sealed capabilities and commits a
mutation only inside its own admission fence.

The local file store tests prove conditional persistence and durability only.
They do not claim provider fencing; the engine with no backend refuses every
privileged transition, which is the fail-closed answer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import multiprocessing
import os
import secrets
import stat
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

import pytest

from ciao.shared_brain_authority import (
    CHALLENGE_TTL_S,
    AuthorityError,
    AuthorityRecord,
    AuthenticatedPrincipal,
    BrainRef,
    CommitOutcome,
    ConnectorKind,
    MutationAdmission,
    Operation,
    ProviderCredential,
    ProtectedAuthorityStore,
    StewardAuthority,
    StoreError,
)


class FakeClock:
    """A monotonic clock the test moves by hand. No sleeps, no wall time."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _principal_material(capability: AuthenticatedPrincipal) -> bytes:
    return json.dumps(
        {
            "principal": capability.principal,
            "brain": capability.brain,
            "storage": capability.storage,
            "member": capability.member,
            "admin": capability.admin,
            "revision": capability.revision,
            "source": capability.source,
            "authenticated_at": capability.authenticated_at,
            "expires_at": capability.expires_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _admission_material(admission: MutationAdmission) -> bytes:
    return json.dumps(
        {
            "admission_id": admission.admission_id,
            "actor": admission.actor,
            "successor": admission.successor,
            "brain": admission.brain,
            "storage": admission.storage,
            "operation": admission.operation.value,
            "generation": admission.generation,
            "fence": admission.fence,
            "issued_at": admission.issued_at,
            "expires_at": admission.expires_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


class FencedBackend:
    """A synthetic trusted provider: sealed capabilities plus a real admission fence.

    This is the shape a Git/folder integration must supply. ``admit`` re-reads
    current entitlement; ``commit`` is the place a revocation observed at the
    effect is enforced, before the store mutation runs. The sealed values are
    HMACs, so a caller-built capability or admission is not accepted.
    """

    def __init__(self, clock: FakeClock, *, secret: bytes = b"test-fence-secret") -> None:
        self.clock = clock
        self._secret = secret
        self.tokens: dict[str, tuple[str, bool, bool]] = {}
        self.principals: set[str] = set()
        self.revoked: set[str] = set()
        self.admissions: dict[str, MutationAdmission] = {}
        self.used: set[str] = set()
        self.lock = threading.Lock()
        self.fail_admit = False
        self.forge_admission = False
        self.mismatch_admission = False
        self.revoke_before_commit: str | None = None
        self.fence_counter = 0

    # ── Identity ─────────────────────────────────────────────────────────

    def add_token(
        self, token: str, principal: str, *, admin: bool = False, member: bool = True
    ) -> None:
        self.tokens[token] = (principal, admin, member)
        self.principals.add(principal)

    def revoke(self, principal: str) -> None:
        with self.lock:
            self.revoked.add(principal)

    def credential(self, token: str) -> ProviderCredential:
        return ProviderCredential(token=token)

    def sign_in(
        self,
        connector: BrainRef,
        token: str,
        *,
        expires_in: float = 60.0,
    ) -> AuthenticatedPrincipal:
        principal, admin, member = self.tokens[token]
        return self._capability(
            connector,
            principal,
            admin=admin,
            member=member,
            expires_in=expires_in,
        )

    def forged_principal(
        self,
        connector: BrainRef,
        principal: str,
        *,
        admin: bool = True,
    ) -> AuthenticatedPrincipal:
        now = self.clock()
        return AuthenticatedPrincipal(
            principal=principal,
            brain=connector.brain,
            storage=connector.storage,
            member=True,
            admin=admin,
            revision="forged",
            source="forged",
            authenticated_at=now,
            expires_at=now + 60,
            seal="forged-seal",
        )

    def _capability(
        self,
        connector: BrainRef,
        principal: str,
        *,
        admin: bool,
        member: bool,
        expires_in: float,
    ) -> AuthenticatedPrincipal:
        now = self.clock()
        capability = AuthenticatedPrincipal(
            principal=principal,
            brain=connector.brain,
            storage=connector.storage,
            member=member,
            admin=admin,
            revision=f"rev-{self.fence_counter}",
            source="test-provider",
            authenticated_at=now,
            expires_at=now + expires_in,
            seal="",
        )
        return replace(
            capability,
            seal=hmac.new(
                self._secret, _principal_material(capability), hashlib.sha256
            ).hexdigest(),
        )

    # ── AuthorityBackend protocol ────────────────────────────────────────

    def authenticate(
        self, connector: BrainRef, credential: ProviderCredential
    ) -> AuthenticatedPrincipal | None:
        found = self.tokens.get(credential.token)
        if found is None:
            return None
        principal, admin, member = found
        return self._capability(
            connector, principal, admin=admin, member=member, expires_in=60
        )

    def verify_principal(self, capability: AuthenticatedPrincipal) -> bool:
        expected = hmac.new(
            self._secret, _principal_material(capability), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(capability.seal, expected)

    def admit(
        self,
        *,
        connector: BrainRef,
        operation: Operation,
        generation: int,
        actor: AuthenticatedPrincipal,
        successor: AuthenticatedPrincipal,
    ) -> MutationAdmission | None:
        if self.fail_admit:
            return None
        if not self.verify_principal(actor) or not self.verify_principal(successor):
            return None
        actor_id = actor.principal
        successor_id = successor.principal
        with self.lock:
            if actor_id in self.revoked or successor_id in self.revoked:
                return None
            if actor_id not in self.principals or successor_id not in self.principals:
                return None
            self.fence_counter += 1
            now = self.clock()
            admission = MutationAdmission(
                admission_id=secrets.token_urlsafe(16),
                actor=actor_id,
                successor=successor_id,
                brain=connector.brain,
                storage=connector.storage,
                operation=operation,
                generation=generation,
                fence=f"provider-rev-{self.fence_counter}",
                issued_at=now,
                expires_at=now + 30,
                seal="",
            )
            if self.mismatch_admission:
                admission = replace(admission, successor="mallory")
            sealed = replace(
                admission,
                seal="forged-seal"
                if self.forge_admission
                else hmac.new(
                    self._secret, _admission_material(admission), hashlib.sha256
                ).hexdigest(),
            )
            self.admissions[sealed.admission_id] = sealed
            return sealed

    def verify_admission(self, admission: MutationAdmission) -> bool:
        stored = self.admissions.get(admission.admission_id)
        if stored is None or stored != admission:
            return False
        expected = hmac.new(
            self._secret, _admission_material(admission), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(admission.seal, expected)

    def commit(
        self, admission: MutationAdmission, mutation: Callable[[], bool]
    ) -> CommitOutcome:
        with self.lock:
            if self.revoke_before_commit is not None:
                self.revoked.add(self.revoke_before_commit)
                self.revoke_before_commit = None
            stored = self.admissions.get(admission.admission_id)
            if (
                stored is None
                or stored != admission
                or admission.admission_id in self.used
                or not self.verify_admission(admission)
            ):
                return CommitOutcome.REJECTED
            if admission.actor in self.revoked or admission.successor in self.revoked:
                return CommitOutcome.REJECTED
            self.used.add(admission.admission_id)
            return (
                CommitOutcome.COMMITTED
                if mutation()
                else CommitOutcome.CONDITION_FAILED
            )


class BarrierBackend(FencedBackend):
    """Wait for named successors so two admissions reach the store together."""

    def __init__(self, clock: FakeClock, successors: set[str]) -> None:
        super().__init__(clock)
        self.successors = set(successors)
        self.barrier = threading.Barrier(2)

    def admit(
        self,
        *,
        connector: BrainRef,
        operation: Operation,
        generation: int,
        actor: AuthenticatedPrincipal,
        successor: AuthenticatedPrincipal,
    ) -> MutationAdmission | None:
        admission = super().admit(
            connector=connector,
            operation=operation,
            generation=generation,
            actor=actor,
            successor=successor,
        )
        if successor.principal in self.successors:
            self.barrier.wait(timeout=5)
        return admission


def _engine(
    tmp_path: Path,
    backend: FencedBackend | None,
    clock: FakeClock | None = None,
    *,
    ttl: float = CHALLENGE_TTL_S,
) -> tuple[StewardAuthority, ProtectedAuthorityStore]:
    store = ProtectedAuthorityStore(tmp_path / "authority")
    engine = StewardAuthority(
        store,
        backend=backend,
        clock=clock or FakeClock(),
        challenge_ttl_s=ttl,
    )
    return engine, store


def _add_people(backend: FencedBackend, connector: BrainRef) -> None:
    backend.add_token("alice-token", "alice", admin=True)
    backend.add_token("bob-token", "bob")
    backend.add_token("carol-token", "carol")
    backend.add_token("dave-token", "dave")
    backend.add_token("mallory-token", "mallory")


def _bootstrap(
    engine: StewardAuthority,
    connector: BrainRef,
    backend: FencedBackend,
    *,
    admin_token: str = "alice-token",
    successor_token: str = "bob-token",
) -> AuthorityRecord:
    admin = backend.sign_in(connector, admin_token)
    successor = backend.sign_in(connector, successor_token)
    challenge = engine.begin_bootstrap(
        connector=connector, admin=admin, successor=successor.principal
    )
    engine.accept_bootstrap(
        challenge.nonce, connector=connector, successor=successor
    )
    return engine.authorize_steward(
        connector=connector, principal=successor, generation=1
    )


def _transfer(
    engine: StewardAuthority,
    connector: BrainRef,
    backend: FencedBackend,
    *,
    incumbent_token: str = "bob-token",
    successor_token: str = "carol-token",
    generation: int = 1,
) -> AuthorityRecord:
    incumbent = backend.sign_in(connector, incumbent_token)
    successor = backend.sign_in(connector, successor_token)
    challenge = engine.begin_transfer(
        connector=connector,
        incumbent=incumbent,
        successor=successor.principal,
        expected_generation=generation,
    )
    engine.accept_transfer(
        challenge.nonce, connector=connector, successor=successor
    )
    return engine.authorize_steward(
        connector=connector, principal=successor, generation=generation + 1
    )


def _race(*calls):
    """Run each call in its own thread and return (kind, value) per call."""
    outcomes: list[tuple[str, object]] = []
    lock = threading.Lock()

    def run(call) -> None:
        try:
            value: object = call()
            item = ("ok", value)
        except AuthorityError as exc:
            item = ("error", exc.reason)
        except Exception as exc:  # pragma: no cover - a broken test, not a path
            item = ("unexpected", repr(exc))
        with lock:
            outcomes.append(item)

    threads = [threading.Thread(target=run, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(outcomes) == len(calls)
    return outcomes


# ── Authenticated capabilities ──────────────────────────────────────────────


def test_bootstrap_needs_authenticated_admin_and_successor_capabilities(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)

    with pytest.raises(AuthorityError) as missing:
        engine.authorize_steward(
            connector=connector,
            principal=backend.sign_in(connector, "bob-token"),
        )
    assert missing.value.reason == "not_initialized"

    admin = backend.sign_in(connector, "alice-token")
    successor = backend.sign_in(connector, "bob-token")
    challenge = engine.begin_bootstrap(
        connector=connector, admin=admin, successor=successor.principal
    )
    assert challenge.operation is Operation.BOOTSTRAP
    assert challenge.actor == "alice"
    assert challenge.successor == "bob"
    assert challenge.generation == 0
    assert challenge.expires_at > clock.now

    transition = engine.accept_bootstrap(
        challenge.nonce, connector=connector, successor=successor
    )
    assert transition.operation is Operation.BOOTSTRAP
    assert transition.actor == "alice"
    assert transition.previous_steward is None
    assert transition.steward == "bob"
    assert transition.previous_generation == 0
    assert transition.generation == 1

    record = store.load("brain-a")
    assert record is not None
    assert (record.steward, record.generation) == ("bob", 1)
    assert engine.audit_trail() == (transition,)


def test_a_member_cannot_bootstrap(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, _store = _engine(tmp_path, backend, clock)
    mallory = backend.sign_in(connector, "mallory-token")

    with pytest.raises(AuthorityError) as refused:
        engine.begin_bootstrap(
            connector=connector, admin=mallory, successor=mallory.principal
        )
    assert refused.value.reason == "not_admin"


def test_an_unknown_credential_is_not_authenticated(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, _store = _engine(tmp_path, backend, clock)

    with pytest.raises(AuthorityError) as refused:
        engine.authenticate(
            connector=connector, credential=backend.credential("not-a-token")
        )
    assert refused.value.reason == "not_authenticated"


def test_a_forged_admin_capability_is_refused(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)
    forged = backend.forged_principal(connector, "alice", admin=True)

    with pytest.raises(AuthorityError) as refused:
        engine.begin_bootstrap(
            connector=connector, admin=forged, successor="bob"
        )
    assert refused.value.reason == "forged_capability"
    assert store.load("brain-a") is None


def test_a_forged_successor_capability_is_refused(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)
    admin = backend.sign_in(connector, "alice-token")
    challenge = engine.begin_bootstrap(
        connector=connector, admin=admin, successor="bob"
    )
    forged = backend.forged_principal(connector, "bob", admin=False)

    with pytest.raises(AuthorityError) as refused:
        engine.accept_bootstrap(
            challenge.nonce, connector=connector, successor=forged
        )
    assert refused.value.reason == "forged_capability"
    assert store.load("brain-a") is None


def test_a_forged_incumbent_capability_is_refused(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, _store = _engine(tmp_path, backend, clock)
    _bootstrap(engine, connector, backend)
    forged = backend.forged_principal(connector, "bob", admin=False)

    with pytest.raises(AuthorityError) as refused:
        engine.begin_transfer(
            connector=connector,
            incumbent=forged,
            successor="carol",
            expected_generation=1,
        )
    assert refused.value.reason == "forged_capability"


def test_a_capability_from_another_backend_is_a_forgery(tmp_path: Path) -> None:
    clock = FakeClock()
    trusted = FencedBackend(clock)
    foreign = FencedBackend(clock, secret=b"foreign-secret")
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(trusted, connector)
    _add_people(foreign, connector)
    engine, _store = _engine(tmp_path, trusted, clock)
    foreign_admin = foreign.sign_in(connector, "alice-token")

    with pytest.raises(AuthorityError) as refused:
        engine.begin_bootstrap(
            connector=connector, admin=foreign_admin, successor="bob"
        )
    assert refused.value.reason == "forged_capability"


def test_an_expired_capability_is_refused(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)
    admin = backend.sign_in(connector, "alice-token")
    expired = backend.sign_in(connector, "bob-token", expires_in=-1)
    challenge = engine.begin_bootstrap(
        connector=connector, admin=admin, successor="bob"
    )

    with pytest.raises(AuthorityError) as refused:
        engine.accept_bootstrap(
            challenge.nonce, connector=connector, successor=expired
        )
    assert refused.value.reason == "expired_capability"
    assert store.load("brain-a") is None


def test_a_missing_backend_fails_closed(tmp_path: Path) -> None:
    clock = FakeClock()
    signer = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(signer, connector)
    admin = signer.sign_in(connector, "alice-token")
    engine, store = _engine(tmp_path, None, clock)

    with pytest.raises(AuthorityError) as refused:
        engine.begin_bootstrap(
            connector=connector, admin=admin, successor="bob"
        )
    assert refused.value.reason == "provider_fencing_unavailable"
    assert store.load("brain-a") is None


def test_an_unverifiable_connector_stays_member_only(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    folder = BrainRef("brain-f", "/tmp/team-notes", ConnectorKind.FOLDER)
    _add_people(backend, folder)
    engine, _store = _engine(tmp_path, backend, clock)
    admin = backend.sign_in(folder, "alice-token")

    with pytest.raises(AuthorityError) as refused:
        engine.begin_bootstrap(
            connector=folder, admin=admin, successor="bob"
        )
    assert refused.value.reason == "member_only"


def test_a_capability_for_another_resource_is_refused(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    first = BrainRef("brain-a", "github.com/acme/notes")
    second = BrainRef("brain-b", "github.com/acme/other")
    _add_people(backend, first)
    engine, _store = _engine(tmp_path, backend, clock)
    wrong = backend.sign_in(second, "alice-token")

    with pytest.raises(AuthorityError) as refused:
        engine.begin_bootstrap(
            connector=first, admin=wrong, successor="bob"
        )
    assert refused.value.reason == "wrong_resource"


# ── Admission boundary ──────────────────────────────────────────────────────


def test_no_admission_boundary_fails_closed(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)
    admin = backend.sign_in(connector, "alice-token")
    challenge = engine.begin_bootstrap(
        connector=connector, admin=admin, successor="bob"
    )
    backend.fail_admit = True
    successor = backend.sign_in(connector, "bob-token")

    with pytest.raises(AuthorityError) as refused:
        engine.accept_bootstrap(
            challenge.nonce, connector=connector, successor=successor
        )
    assert refused.value.reason == "provider_fencing_unavailable"
    assert store.load("brain-a") is None


def test_a_forged_admission_is_refused(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)
    admin = backend.sign_in(connector, "alice-token")
    challenge = engine.begin_bootstrap(
        connector=connector, admin=admin, successor="bob"
    )
    backend.forge_admission = True
    successor = backend.sign_in(connector, "bob-token")

    with pytest.raises(AuthorityError) as refused:
        engine.accept_bootstrap(
            challenge.nonce, connector=connector, successor=successor
        )
    assert refused.value.reason == "forged_admission"
    assert store.load("brain-a") is None


def test_an_admission_bound_to_another_successor_is_refused(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)
    admin = backend.sign_in(connector, "alice-token")
    challenge = engine.begin_bootstrap(
        connector=connector, admin=admin, successor="bob"
    )
    backend.mismatch_admission = True
    successor = backend.sign_in(connector, "bob-token")

    with pytest.raises(AuthorityError) as refused:
        engine.accept_bootstrap(
            challenge.nonce, connector=connector, successor=successor
        )
    assert refused.value.reason == "admission_mismatch"
    assert store.load("brain-a") is None


def test_revocation_before_acceptance_refuses_the_admission(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)
    _bootstrap(engine, connector, backend)
    incumbent = backend.sign_in(connector, "bob-token")
    successor = backend.sign_in(connector, "carol-token")
    challenge = engine.begin_transfer(
        connector=connector,
        incumbent=incumbent,
        successor=successor.principal,
        expected_generation=1,
    )
    backend.revoke("bob")

    with pytest.raises(AuthorityError) as refused:
        engine.accept_transfer(
            challenge.nonce, connector=connector, successor=successor
        )
    assert refused.value.reason == "provider_fencing_unavailable"
    record = store.load("brain-a")
    assert record is not None
    assert (record.steward, record.generation) == ("bob", 1)


def test_revocation_at_the_effect_is_rejected_by_the_commit_fence(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)
    _bootstrap(engine, connector, backend)
    incumbent = backend.sign_in(connector, "bob-token")
    successor = backend.sign_in(connector, "carol-token")
    challenge = engine.begin_transfer(
        connector=connector,
        incumbent=incumbent,
        successor=successor.principal,
        expected_generation=1,
    )
    # The admission is issued while bob is still entitled; the provider
    # revocation lands between the check and the store mutation. Only the
    # commit fence can catch this, which is why the core requires it.
    backend.revoke_before_commit = "bob"

    with pytest.raises(AuthorityError) as refused:
        engine.accept_transfer(
            challenge.nonce, connector=connector, successor=successor
        )
    assert refused.value.reason == "admission_rejected"
    record = store.load("brain-a")
    assert record is not None
    assert (record.steward, record.generation) == ("bob", 1)


def test_recovery_revocation_at_the_effect_is_rejected(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)
    _bootstrap(engine, connector, backend)
    admin = backend.sign_in(connector, "alice-token")
    successor = backend.sign_in(connector, "carol-token")
    challenge = engine.begin_recovery(
        connector=connector,
        admin=admin,
        successor=successor.principal,
        expected_generation=1,
        confirmed=True,
    )
    backend.revoke_before_commit = "alice"

    with pytest.raises(AuthorityError) as refused:
        engine.accept_recovery(
            challenge.nonce, connector=connector, successor=successor
        )
    assert refused.value.reason == "admission_rejected"
    record = store.load("brain-a")
    assert record is not None
    assert (record.steward, record.generation) == ("bob", 1)


# ── Bootstrap ───────────────────────────────────────────────────────────────


def test_bootstrap_is_conditional_on_absence(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)
    _bootstrap(engine, connector, backend)

    admin = backend.sign_in(connector, "alice-token")
    with pytest.raises(AuthorityError) as again:
        engine.begin_bootstrap(
            connector=connector, admin=admin, successor="bob"
        )
    assert again.value.reason == "already_initialized"

    copied = StewardAuthority(store, backend=backend, clock=FakeClock())
    with pytest.raises(AuthorityError) as copied_refusal:
        copied.begin_bootstrap(
            connector=connector, admin=admin, successor="mallory"
        )
    assert copied_refusal.value.reason == "already_initialized"
    assert store.load("brain-a").steward == "bob"


def test_two_bootstrap_invitations_still_create_only_one_record(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)
    first = backend.sign_in(connector, "alice-token")
    bob = backend.sign_in(connector, "bob-token")
    dave = backend.sign_in(connector, "dave-token")
    first_challenge = engine.begin_bootstrap(
        connector=connector, admin=first, successor=bob.principal
    )
    # alice is the admin for the first invitation; carol is a second admin.
    backend.add_token("carol-admin-token", "carol", admin=True)
    carol_admin = backend.sign_in(connector, "carol-admin-token")
    second_challenge = engine.begin_bootstrap(
        connector=connector, admin=carol_admin, successor=dave.principal
    )
    engine.accept_bootstrap(
        first_challenge.nonce, connector=connector, successor=bob
    )

    with pytest.raises(AuthorityError) as refused:
        engine.accept_bootstrap(
            second_challenge.nonce, connector=connector, successor=dave
        )
    assert refused.value.reason == "challenge_unknown"
    record = store.load("brain-a")
    assert record is not None
    assert record.generation == 1
    assert record.steward == "bob"


def test_a_replayed_bootstrap_invitation_is_spent(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, _store = _engine(tmp_path, backend, clock)
    admin = backend.sign_in(connector, "alice-token")
    successor = backend.sign_in(connector, "bob-token")
    challenge = engine.begin_bootstrap(
        connector=connector, admin=admin, successor=successor.principal
    )
    engine.accept_bootstrap(
        challenge.nonce, connector=connector, successor=successor
    )

    with pytest.raises(AuthorityError) as replay:
        engine.accept_bootstrap(
            challenge.nonce, connector=connector, successor=successor
        )
    assert replay.value.reason == "challenge_used"


# ── Challenge binding and expiry ────────────────────────────────────────────


def test_a_challenge_is_bound_to_brain_storage_and_successor(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    other = BrainRef("brain-b", "github.com/acme/other")
    _add_people(backend, connector)
    _add_people(backend, other)
    engine, _store = _engine(tmp_path, backend, clock)
    admin = backend.sign_in(connector, "alice-token")
    bob = backend.sign_in(connector, "bob-token")
    dave = backend.sign_in(connector, "dave-token")
    challenge = engine.begin_bootstrap(
        connector=connector, admin=admin, successor=bob.principal
    )

    with pytest.raises(AuthorityError) as wrong_brain:
        engine.accept_bootstrap(
            challenge.nonce, connector=other, successor=bob
        )
    assert wrong_brain.value.reason == "challenge_wrong_resource"

    with pytest.raises(AuthorityError) as wrong_successor:
        engine.accept_bootstrap(
            challenge.nonce, connector=connector, successor=dave
        )
    assert wrong_successor.value.reason == "successor_mismatch"

    transition = engine.accept_bootstrap(
        challenge.nonce, connector=connector, successor=bob
    )
    assert transition.steward == "bob"


def test_a_challenge_expires_and_is_single_use(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock, ttl=30)
    admin = backend.sign_in(connector, "alice-token")
    bob = backend.sign_in(connector, "bob-token")

    expired = engine.begin_bootstrap(
        connector=connector, admin=admin, successor=bob.principal
    )
    clock.advance(31)
    with pytest.raises(AuthorityError) as refused:
        engine.accept_bootstrap(
            expired.nonce, connector=connector, successor=bob
        )
    assert refused.value.reason == "challenge_expired"
    assert store.load("brain-a") is None

    fresh = engine.begin_bootstrap(
        connector=connector, admin=admin, successor=bob.principal
    )
    assert fresh.nonce != expired.nonce
    engine.accept_bootstrap(fresh.nonce, connector=connector, successor=bob)
    with pytest.raises(AuthorityError) as replayed:
        engine.accept_bootstrap(fresh.nonce, connector=connector, successor=bob)
    assert replayed.value.reason == "challenge_used"


def test_a_challenge_is_bound_to_its_operation(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, _store = _engine(tmp_path, backend, clock)
    _bootstrap(engine, connector, backend)
    incumbent = backend.sign_in(connector, "bob-token")
    successor = backend.sign_in(connector, "carol-token")
    transfer = engine.begin_transfer(
        connector=connector,
        incumbent=incumbent,
        successor=successor.principal,
        expected_generation=1,
    )

    with pytest.raises(AuthorityError) as wrong_operation:
        engine.accept_recovery(
            transfer.nonce, connector=connector, successor=successor
        )
    assert wrong_operation.value.reason == "challenge_wrong_operation"


# ── Transfer ────────────────────────────────────────────────────────────────


def test_a_transfer_needs_the_incumbent_and_the_observed_generation(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, _store = _engine(tmp_path, backend, clock)
    _bootstrap(engine, connector, backend)
    mallory = backend.sign_in(connector, "mallory-token")
    bob = backend.sign_in(connector, "bob-token")
    carol = backend.sign_in(connector, "carol-token")

    with pytest.raises(AuthorityError) as not_steward:
        engine.begin_transfer(
            connector=connector,
            incumbent=mallory,
            successor=carol.principal,
            expected_generation=1,
        )
    assert not_steward.value.reason == "not_steward"

    with pytest.raises(AuthorityError) as stale:
        engine.begin_transfer(
            connector=connector,
            incumbent=bob,
            successor=carol.principal,
            expected_generation=0,
        )
    assert stale.value.reason == "stale_generation"

    challenge = engine.begin_transfer(
        connector=connector,
        incumbent=bob,
        successor=carol.principal,
        expected_generation=1,
    )
    assert challenge.actor == "bob"
    assert challenge.generation == 1


def test_transfer_acceptance_rotates_generation_and_fences_the_old_one(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)
    _bootstrap(engine, connector, backend)
    bob = backend.sign_in(connector, "bob-token")
    carol = backend.sign_in(connector, "carol-token")
    challenge = engine.begin_transfer(
        connector=connector,
        incumbent=bob,
        successor=carol.principal,
        expected_generation=1,
    )
    engine.authorize_steward(connector=connector, principal=bob, generation=1)
    with pytest.raises(AuthorityError) as successor_early:
        engine.authorize_steward(connector=connector, principal=carol)
    assert successor_early.value.reason == "not_steward"

    transition = engine.accept_transfer(
        challenge.nonce, connector=connector, successor=carol
    )
    assert transition.operation is Operation.TRANSFER
    assert transition.actor == "bob"
    assert transition.previous_steward == "bob"
    assert transition.steward == "carol"
    assert transition.previous_generation == 1
    assert transition.generation == 2
    record = store.load("brain-a")
    assert record is not None
    assert (record.steward, record.generation) == ("carol", 2)

    with pytest.raises(AuthorityError) as old_generation:
        engine.authorize_steward(connector=connector, principal=bob, generation=1)
    assert old_generation.value.reason == "stale_generation"
    with pytest.raises(AuthorityError) as old_steward:
        engine.authorize_steward(connector=connector, principal=bob, generation=2)
    assert old_steward.value.reason == "not_steward"
    engine.authorize_steward(connector=connector, principal=carol, generation=2)

    with pytest.raises(AuthorityError) as replay:
        engine.accept_transfer(
            challenge.nonce, connector=connector, successor=carol
        )
    assert replay.value.reason == "challenge_used"


def test_transfer_acceptance_must_be_by_the_named_successor(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)
    _bootstrap(engine, connector, backend)
    bob = backend.sign_in(connector, "bob-token")
    carol = backend.sign_in(connector, "carol-token")
    dave = backend.sign_in(connector, "dave-token")
    challenge = engine.begin_transfer(
        connector=connector,
        incumbent=bob,
        successor=carol.principal,
        expected_generation=1,
    )

    with pytest.raises(AuthorityError) as wrong_successor:
        engine.accept_transfer(
            challenge.nonce, connector=connector, successor=dave
        )
    assert wrong_successor.value.reason == "successor_mismatch"
    assert store.load("brain-a").steward == "bob"

    engine.accept_transfer(challenge.nonce, connector=connector, successor=carol)
    assert store.load("brain-a").steward == "carol"


def test_transfer_acceptance_rechecks_the_successor(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)
    _bootstrap(engine, connector, backend)
    bob = backend.sign_in(connector, "bob-token")
    carol = backend.sign_in(connector, "carol-token")
    challenge = engine.begin_transfer(
        connector=connector,
        incumbent=bob,
        successor=carol.principal,
        expected_generation=1,
    )
    backend.revoke("carol")

    with pytest.raises(AuthorityError) as unverified:
        engine.accept_transfer(
            challenge.nonce, connector=connector, successor=carol
        )
    assert unverified.value.reason == "provider_fencing_unavailable"
    assert store.load("brain-a").steward == "bob"


def test_an_outstanding_invitation_is_invalidated_by_a_winning_transition(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, _store = _engine(tmp_path, backend, clock)
    _bootstrap(engine, connector, backend)
    bob = backend.sign_in(connector, "bob-token")
    carol = backend.sign_in(connector, "carol-token")
    dave = backend.sign_in(connector, "dave-token")
    first = engine.begin_transfer(
        connector=connector,
        incumbent=bob,
        successor=carol.principal,
        expected_generation=1,
    )
    stale = engine.begin_transfer(
        connector=connector,
        incumbent=bob,
        successor=dave.principal,
        expected_generation=1,
    )
    engine.accept_transfer(first.nonce, connector=connector, successor=carol)

    with pytest.raises(AuthorityError) as revoked:
        engine.accept_transfer(
            stale.nonce, connector=connector, successor=dave
        )
    assert revoked.value.reason == "challenge_unknown"


# ── Recovery ────────────────────────────────────────────────────────────────


def test_recovery_needs_a_fresh_admin_confirmation_and_successor_acceptance(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)
    _bootstrap(engine, connector, backend)
    admin = backend.sign_in(connector, "alice-token")
    mallory = backend.sign_in(connector, "mallory-token")
    carol = backend.sign_in(connector, "carol-token")

    with pytest.raises(AuthorityError) as member_admin:
        engine.begin_recovery(
            connector=connector,
            admin=mallory,
            successor=carol.principal,
            expected_generation=1,
            confirmed=True,
        )
    assert member_admin.value.reason == "not_admin"

    with pytest.raises(AuthorityError) as unconfirmed:
        engine.begin_recovery(
            connector=connector,
            admin=admin,
            successor=carol.principal,
            expected_generation=1,
            confirmed=False,
        )
    assert unconfirmed.value.reason == "confirmation_required"

    challenge = engine.begin_recovery(
        connector=connector,
        admin=admin,
        successor=carol.principal,
        expected_generation=1,
        confirmed=True,
    )
    transition = engine.accept_recovery(
        challenge.nonce, connector=connector, successor=carol
    )
    assert transition.operation is Operation.RECOVERY
    assert transition.actor == "alice"
    assert transition.previous_steward == "bob"
    assert transition.steward == "carol"
    assert transition.previous_generation == 1
    assert transition.generation == 2
    record = store.load("brain-a")
    assert record is not None
    assert (record.steward, record.generation) == ("carol", 2)


def test_recovery_is_conditional_on_the_current_generation(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock)
    _bootstrap(engine, connector, backend)
    _transfer(engine, connector, backend)
    admin = backend.sign_in(connector, "alice-token")
    dave = backend.sign_in(connector, "dave-token")

    with pytest.raises(AuthorityError) as stale:
        engine.begin_recovery(
            connector=connector,
            admin=admin,
            successor=dave.principal,
            expected_generation=1,
            confirmed=True,
        )
    assert stale.value.reason == "stale_generation"

    challenge = engine.begin_recovery(
        connector=connector,
        admin=admin,
        successor=dave.principal,
        expected_generation=2,
        confirmed=True,
    )
    engine.accept_recovery(challenge.nonce, connector=connector, successor=dave)
    assert store.load("brain-a").generation == 3


def test_an_expired_recovery_cannot_be_replayed(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, store = _engine(tmp_path, backend, clock, ttl=20)
    _bootstrap(engine, connector, backend)
    admin = backend.sign_in(connector, "alice-token")
    carol = backend.sign_in(connector, "carol-token")
    challenge = engine.begin_recovery(
        connector=connector,
        admin=admin,
        successor=carol.principal,
        expected_generation=1,
        confirmed=True,
    )
    clock.advance(21)

    with pytest.raises(AuthorityError) as expired:
        engine.accept_recovery(
            challenge.nonce, connector=connector, successor=carol
        )
    assert expired.value.reason == "challenge_expired"
    assert store.load("brain-a").steward == "bob"


# ── Scoping and authorization ───────────────────────────────────────────────


def test_a_steward_role_in_one_brain_confers_nothing_in_another(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    first = BrainRef("brain-a", "github.com/acme/notes")
    second = BrainRef("brain-b", "github.com/acme/other")
    _add_people(backend, first)
    _add_people(backend, second)
    engine, _store = _engine(tmp_path, backend, clock)
    _bootstrap(engine, first, backend)
    _bootstrap(engine, second, backend, admin_token="alice-token", successor_token="carol-token")
    bob_first = backend.sign_in(first, "bob-token")
    bob_second = backend.sign_in(second, "bob-token")

    engine.authorize_steward(connector=first, principal=bob_first, generation=1)
    with pytest.raises(AuthorityError) as elsewhere:
        engine.authorize_steward(connector=second, principal=bob_second, generation=1)
    assert elsewhere.value.reason == "not_steward"
    with pytest.raises(AuthorityError) as transfer_elsewhere:
        engine.begin_transfer(
            connector=second,
            incumbent=bob_second,
            successor="dave",
            expected_generation=1,
        )
    assert transfer_elsewhere.value.reason == "not_steward"


def test_authorize_steward_requires_a_current_capability(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    engine, _store = _engine(tmp_path, backend, clock)
    _bootstrap(engine, connector, backend)
    bob = backend.sign_in(connector, "bob-token")

    with pytest.raises(AuthorityError) as forged:
        engine.authorize_steward(
            connector=connector,
            principal=backend.forged_principal(connector, "bob", admin=False),
            generation=1,
        )
    assert forged.value.reason == "forged_capability"

    expired = backend.sign_in(connector, "bob-token", expires_in=-1)
    with pytest.raises(AuthorityError) as expired_refusal:
        engine.authorize_steward(
            connector=connector, principal=expired, generation=1
        )
    assert expired_refusal.value.reason == "expired_capability"

    engine.authorize_steward(connector=connector, principal=bob, generation=1)


# ── Races ───────────────────────────────────────────────────────────────────


def test_two_initial_enrollments_race_to_one_record(tmp_path: Path) -> None:
    clock = FakeClock()
    store = ProtectedAuthorityStore(tmp_path / "authority")
    connector = BrainRef("brain-a", "github.com/acme/notes")
    backend = BarrierBackend(clock, {"bob", "dave"})
    _add_people(backend, connector)
    backend.add_token("carol-admin-token", "carol", admin=True)
    alice = backend.sign_in(connector, "alice-token")
    bob = backend.sign_in(connector, "bob-token")
    carol_admin = backend.sign_in(connector, "carol-admin-token")
    dave = backend.sign_in(connector, "dave-token")
    first = StewardAuthority(store, backend=backend, clock=clock)
    second = StewardAuthority(store, backend=backend, clock=clock)
    first_challenge = first.begin_bootstrap(
        connector=connector, admin=alice, successor=bob.principal
    )
    second_challenge = second.begin_bootstrap(
        connector=connector, admin=carol_admin, successor=dave.principal
    )

    outcomes = _race(
        lambda: first.accept_bootstrap(
            first_challenge.nonce, connector=connector, successor=bob
        ),
        lambda: second.accept_bootstrap(
            second_challenge.nonce, connector=connector, successor=dave
        ),
    )
    kinds = sorted(kind for kind, _value in outcomes)
    assert kinds == ["error", "ok"]
    error = next(value for kind, value in outcomes if kind == "error")
    assert error == "already_initialized"
    record = store.load("brain-a")
    assert record is not None
    assert record.generation == 1
    assert record.steward in {"bob", "dave"}
    backend.successors.clear()


def test_two_transfers_race_and_only_one_generation_wins(tmp_path: Path) -> None:
    clock = FakeClock()
    store = ProtectedAuthorityStore(tmp_path / "authority")
    connector = BrainRef("brain-a", "github.com/acme/notes")
    backend = BarrierBackend(clock, {"carol", "dave"})
    _add_people(backend, connector)
    first = StewardAuthority(store, backend=backend, clock=clock)
    second = StewardAuthority(store, backend=backend, clock=clock)
    _bootstrap(first, connector, backend)
    bob = backend.sign_in(connector, "bob-token")
    carol = backend.sign_in(connector, "carol-token")
    dave = backend.sign_in(connector, "dave-token")
    first_challenge = first.begin_transfer(
        connector=connector,
        incumbent=bob,
        successor=carol.principal,
        expected_generation=1,
    )
    second_challenge = second.begin_transfer(
        connector=connector,
        incumbent=bob,
        successor=dave.principal,
        expected_generation=1,
    )

    outcomes = _race(
        lambda: first.accept_transfer(
            first_challenge.nonce, connector=connector, successor=carol
        ),
        lambda: second.accept_transfer(
            second_challenge.nonce, connector=connector, successor=dave
        ),
    )
    kinds = sorted(kind for kind, _value in outcomes)
    assert kinds == ["error", "ok"]
    error = next(value for kind, value in outcomes if kind == "error")
    assert error == "stale_generation"
    record = store.load("brain-a")
    assert record is not None
    assert record.generation == 2
    assert record.steward in {"carol", "dave"}
    backend.successors.clear()

    loser = "carol" if record.steward == "dave" else "dave"
    winner = backend.sign_in(connector, f"{record.steward}-token")
    loser_cap = backend.sign_in(connector, f"{loser}-token")
    first.authorize_steward(
        connector=connector, principal=winner, generation=2
    )
    with pytest.raises(AuthorityError) as refused:
        first.authorize_steward(
            connector=connector, principal=loser_cap, generation=2
        )
    assert refused.value.reason == "not_steward"


def test_transfer_and_recovery_race_and_one_transition_wins(tmp_path: Path) -> None:
    clock = FakeClock()
    store = ProtectedAuthorityStore(tmp_path / "authority")
    connector = BrainRef("brain-a", "github.com/acme/notes")
    backend = BarrierBackend(clock, {"carol", "dave"})
    _add_people(backend, connector)
    first = StewardAuthority(store, backend=backend, clock=clock)
    second = StewardAuthority(store, backend=backend, clock=clock)
    _bootstrap(first, connector, backend)
    bob = backend.sign_in(connector, "bob-token")
    alice = backend.sign_in(connector, "alice-token")
    carol = backend.sign_in(connector, "carol-token")
    dave = backend.sign_in(connector, "dave-token")
    transfer = first.begin_transfer(
        connector=connector,
        incumbent=bob,
        successor=carol.principal,
        expected_generation=1,
    )
    recovery = second.begin_recovery(
        connector=connector,
        admin=alice,
        successor=dave.principal,
        expected_generation=1,
        confirmed=True,
    )

    outcomes = _race(
        lambda: first.accept_transfer(
            transfer.nonce, connector=connector, successor=carol
        ),
        lambda: second.accept_recovery(
            recovery.nonce, connector=connector, successor=dave
        ),
    )
    kinds = sorted(kind for kind, _value in outcomes)
    assert kinds == ["error", "ok"]
    error = next(value for kind, value in outcomes if kind == "error")
    assert error == "stale_generation"
    record = store.load("brain-a")
    assert record is not None
    assert record.generation == 2
    assert record.steward in {"carol", "dave"}
    backend.successors.clear()


# ── Protected store ─────────────────────────────────────────────────────────


def _record(steward: str = "bob", generation: int = 1) -> AuthorityRecord:
    return AuthorityRecord(
        brain="brain-a",
        storage="github.com/acme/notes",
        connector=ConnectorKind.GIT.value,
        generation=generation,
        steward=steward,
        created_at=10.0,
        updated_at=10.0,
    )


def test_the_protected_store_is_private_and_conditional(tmp_path: Path) -> None:
    store = ProtectedAuthorityStore(tmp_path / "authority")
    assert stat.S_IMODE(os.stat(tmp_path / "authority").st_mode) == 0o700

    assert store.create_if_absent(_record()) is True
    path = tmp_path / "authority" / "authority.json"
    lock_path = tmp_path / "authority" / "authority.lock"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(lock_path).st_mode) == 0o600
    assert store.create_if_absent(_record(steward="mallory")) is False

    assert store.replace_if_generation(_record("carol", 2), 0) is False
    assert store.replace_if_generation(_record("carol", 2), 1) is True
    assert store.load("brain-a") == _record("carol", 2)
    assert store.replace_if_generation(_record("dave", 3), 1) is False

    with pytest.raises(StoreError) as skipped:
        store.replace_if_generation(_record("dave", 4), 2)
    assert skipped.value.reason == "invalid_record"


def test_the_store_refuses_a_world_readable_document(tmp_path: Path) -> None:
    store = ProtectedAuthorityStore(tmp_path / "authority")
    store.create_if_absent(_record())
    path = tmp_path / "authority" / "authority.json"
    os.chmod(path, 0o644)

    with pytest.raises(StoreError) as unsafe:
        store.load("brain-a")
    assert unsafe.value.reason == "unsafe_store"


def test_a_malformed_or_unsafe_store_supplies_no_authority(tmp_path: Path) -> None:
    store = ProtectedAuthorityStore(tmp_path / "authority")
    path = tmp_path / "authority" / "authority.json"
    path.write_text("{not json", encoding="utf-8")
    os.chmod(path, 0o600)

    with pytest.raises(StoreError) as corrupt:
        store.load("brain-a")
    assert corrupt.value.reason == "corrupt"

    clock = FakeClock()
    backend = FencedBackend(clock)
    connector = BrainRef("brain-a", "github.com/acme/notes")
    _add_people(backend, connector)
    admin = backend.sign_in(connector, "alice-token")
    engine = StewardAuthority(store, backend=backend, clock=clock)
    with pytest.raises(AuthorityError) as unavailable:
        engine.begin_bootstrap(
            connector=connector, admin=admin, successor="bob"
        )
    assert unavailable.value.reason == "store_unavailable"

    path.unlink()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    os.chmod(target, 0o600)
    os.symlink(target, path)
    with pytest.raises(StoreError) as unsafe:
        store.load("brain-a")
    assert unsafe.value.reason == "unsafe_store"


# ── Cross-process store ─────────────────────────────────────────────────────


def _create_worker(root: str, steward: str, queue) -> None:
    try:
        store = ProtectedAuthorityStore(root)
        record = AuthorityRecord(
            brain="brain-a",
            storage="github.com/acme/notes",
            connector=ConnectorKind.GIT.value,
            generation=1,
            steward=steward,
            created_at=1.0,
            updated_at=1.0,
        )
        queue.put(("ok", store.create_if_absent(record)))
    except Exception as exc:  # pragma: no cover - a failed child is the assertion
        queue.put(("error", repr(exc)))


def _read_worker(root: str, queue) -> None:
    try:
        record = ProtectedAuthorityStore(root).load("brain-a")
        queue.put(("ok", None if record is None else record.steward))
    except Exception as exc:  # pragma: no cover - a failed child is the assertion
        queue.put(("error", repr(exc)))


def _replace_worker(root: str, steward: str, queue) -> None:
    try:
        store = ProtectedAuthorityStore(root)
        record = AuthorityRecord(
            brain="brain-a",
            storage="github.com/acme/notes",
            connector=ConnectorKind.GIT.value,
            generation=2,
            steward=steward,
            created_at=1.0,
            updated_at=2.0,
        )
        queue.put(("ok", store.replace_if_generation(record, 1)))
    except Exception as exc:  # pragma: no cover - a failed child is the assertion
        queue.put(("error", repr(exc)))


def test_two_processes_share_one_conditional_store(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    root = str(tmp_path / "authority")
    first = context.Process(target=_create_worker, args=(root, "bob", queue))
    second = context.Process(target=_create_worker, args=(root, "carol", queue))
    for process in (first, second):
        process.start()
    for process in (first, second):
        process.join(timeout=20)
        assert process.exitcode == 0
    results = [queue.get(timeout=5), queue.get(timeout=5)]
    assert all(kind == "ok" for kind, _value in results)
    assert sorted(bool(value) for _kind, value in results) == [False, True]
    record = ProtectedAuthorityStore(root).load("brain-a")
    assert record is not None
    assert record.steward in {"bob", "carol"}
    assert record.generation == 1


def test_a_record_survives_a_fresh_process(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    store = ProtectedAuthorityStore(root)
    assert store.create_if_absent(_record()) is True
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    reader = context.Process(target=_read_worker, args=(str(root), queue))
    reader.start()
    reader.join(timeout=20)
    assert reader.exitcode == 0
    assert queue.get(timeout=5) == ("ok", "bob")


def test_two_processes_share_one_generation_fence(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    store = ProtectedAuthorityStore(root)
    assert store.create_if_absent(_record()) is True
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    first = context.Process(target=_replace_worker, args=(str(root), "carol", queue))
    second = context.Process(target=_replace_worker, args=(str(root), "dave", queue))
    for process in (first, second):
        process.start()
    for process in (first, second):
        process.join(timeout=20)
        assert process.exitcode == 0
    results = [queue.get(timeout=5), queue.get(timeout=5)]
    assert all(kind == "ok" for kind, _value in results)
    assert sorted(bool(value) for _kind, value in results) == [False, True]
    record = ProtectedAuthorityStore(root).load("brain-a")
    assert record is not None
    assert record.generation == 2
    assert record.steward in {"carol", "dave"}
