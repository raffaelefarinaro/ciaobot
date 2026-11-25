"""Steward authority core: bootstrap, transfer, recovery and their fences.

Pure unit tests over an injected clock, a temporary protected store and a
synthetic verifier: no routes, no Settings, no live repository and no sleeps.
Every test names the behavior it pins.
"""

from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from ciao.shared_brain_authority import (
    CHALLENGE_TTL_S,
    AuthorityError,
    AuthorityRecord,
    BrainRef,
    ConnectorKind,
    Operation,
    PrincipalEvidence,
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


class RecordingVerifier:
    """A synthetic trusted connector verifier.

    It models the seam the integration owner will supply from a real backend:
    an answer for one (brain, storage, principal) or ``None`` when the backend
    cannot verify that principal for that resource.
    """

    def __init__(self) -> None:
        self.roles: dict[tuple[str, str, str], PrincipalEvidence] = {}

    def grant(
        self,
        connector: BrainRef,
        principal: str,
        *,
        admin: bool = False,
        member: bool = True,
        revision: str = "rev-1",
        source: str = "test-acl",
        brain: str | None = None,
        storage: str | None = None,
        evidence_principal: str | None = None,
    ) -> PrincipalEvidence:
        evidence = PrincipalEvidence(
            principal=evidence_principal if evidence_principal is not None else principal,
            brain=brain if brain is not None else connector.brain,
            storage=storage if storage is not None else connector.storage,
            member=member,
            admin=admin,
            revision=revision,
            source=source,
            observed_at=0.0,
        )
        self.roles[(connector.brain, connector.storage, principal)] = evidence
        return evidence

    def revoke(self, connector: BrainRef, principal: str) -> None:
        self.roles.pop((connector.brain, connector.storage, principal), None)

    def verify(
        self, connector: BrainRef, principal: str
    ) -> PrincipalEvidence | None:
        return self.roles.get((connector.brain, connector.storage, principal))


class StrictVerifier(RecordingVerifier):
    """A verifier that only accepts the engine's own connector value.

    It is the boundary a fake connector must fail: the fake is structurally
    callable but is not evidence this trusted verifier observed.
    """

    def verify(
        self, connector: BrainRef, principal: str
    ) -> PrincipalEvidence | None:
        if not isinstance(connector, BrainRef):
            return None
        return super().verify(connector, principal)


class BarrierVerifier(RecordingVerifier):
    """Wait for the named successors so a race reaches the store together."""

    def __init__(self, successors: set[str]) -> None:
        super().__init__()
        self.successors = set(successors)
        self.barrier = threading.Barrier(2)

    def verify(
        self, connector: BrainRef, principal: str
    ) -> PrincipalEvidence | None:
        evidence = super().verify(connector, principal)
        if principal in self.successors:
            self.barrier.wait(timeout=5)
        return evidence


@dataclass(frozen=True)
class FakeConnector:
    """A connector that claims privilege but has no verified observation."""

    brain: str
    storage: str
    kind: ConnectorKind = ConnectorKind.GIT
    verified_admin: bool = True

    def is_admin(self, principal: str) -> bool:
        return self.verified_admin


def _engine(
    tmp_path: Path,
    verifier: RecordingVerifier | None,
    clock: FakeClock | None = None,
    *,
    ttl: float = CHALLENGE_TTL_S,
) -> tuple[StewardAuthority, ProtectedAuthorityStore]:
    store = ProtectedAuthorityStore(tmp_path / "authority")
    engine = StewardAuthority(
        store,
        verifier=verifier,
        clock=clock or FakeClock(),
        challenge_ttl_s=ttl,
    )
    return engine, store


def _bootstrap(
    engine: StewardAuthority,
    connector: BrainRef,
    *,
    admin: str,
    successor: str,
) -> AuthorityRecord:
    challenge = engine.begin_bootstrap(
        connector=connector, admin=admin, successor=successor
    )
    engine.accept_bootstrap(
        challenge.nonce, connector=connector, successor=successor
    )
    return engine.authorize_steward(connector=connector, principal=successor)


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


# ── Bootstrap ───────────────────────────────────────────────────────────────


def test_bootstrap_needs_a_verified_admin_and_successor_acceptance(tmp_path: Path) -> None:
    clock = FakeClock()
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    engine, store = _engine(tmp_path, verifier, clock)

    # No record exists until the nominated successor accepts.
    with pytest.raises(AuthorityError) as missing:
        engine.authorize_steward(connector=connector, principal="bob")
    assert missing.value.reason == "not_initialized"

    challenge = engine.begin_bootstrap(
        connector=connector, admin="alice", successor="bob"
    )
    assert challenge.operation is Operation.BOOTSTRAP
    assert challenge.actor == "alice"
    assert challenge.successor == "bob"
    assert challenge.generation == 0
    assert challenge.expires_at > clock.now

    transition = engine.accept_bootstrap(
        challenge.nonce, connector=connector, successor="bob"
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
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "mallory")
    engine, _store = _engine(tmp_path, verifier)

    with pytest.raises(AuthorityError) as refused:
        engine.begin_bootstrap(
            connector=connector, admin="mallory", successor="mallory"
        )
    assert refused.value.reason == "not_admin"


def test_unverifiable_connectors_stay_member_only(tmp_path: Path) -> None:
    connector = BrainRef("brain-a", "generic.example/notes")
    verifier = RecordingVerifier()  # answers None for every principal
    engine, _store = _engine(tmp_path, verifier)

    with pytest.raises(AuthorityError) as generic:
        engine.begin_bootstrap(
            connector=connector, admin="mallory", successor="mallory"
        )
    assert generic.value.reason == "member_only"

    # An ordinary folder is member-only by policy, even if a verifier were to
    # answer with an administrator role for it.
    folder = BrainRef("brain-f", "/tmp/team-notes", ConnectorKind.FOLDER)
    verifier.grant(folder, "alice", admin=True)
    with pytest.raises(AuthorityError) as folder_refusal:
        engine.begin_bootstrap(
            connector=folder, admin="alice", successor="bob"
        )
    assert folder_refusal.value.reason == "member_only"

    # No verifier at all is the same answer, not a stronger default.
    bare = StewardAuthority(ProtectedAuthorityStore(tmp_path / "bare"))
    with pytest.raises(AuthorityError) as no_verifier:
        bare.begin_bootstrap(connector=connector, admin="alice", successor="bob")
    assert no_verifier.value.reason == "member_only"


def test_a_fake_connector_cannot_enable_a_privileged_action(tmp_path: Path) -> None:
    fake = FakeConnector("brain-a", "github.com/acme/notes")
    verifier = StrictVerifier()
    engine, store = _engine(tmp_path, verifier)

    # The fake claims ``verified_admin`` and ``is_admin()``, but the trusted
    # verifier has no observation for it and the engine never asks the fake.
    assert fake.verified_admin is True
    assert fake.is_admin("mallory") is True
    with pytest.raises(AuthorityError) as refused:
        engine.begin_bootstrap(
            connector=fake, admin="mallory", successor="mallory"
        )
    assert refused.value.reason == "member_only"
    assert store.load("brain-a") is None


def test_bootstrap_is_conditional_on_absence(tmp_path: Path) -> None:
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    engine, store = _engine(tmp_path, verifier)
    first = engine.begin_bootstrap(
        connector=connector, admin="alice", successor="bob"
    )
    engine.accept_bootstrap(first.nonce, connector=connector, successor="bob")

    with pytest.raises(AuthorityError) as again:
        engine.begin_bootstrap(
            connector=connector, admin="alice", successor="bob"
        )
    assert again.value.reason == "already_initialized"

    # A second engine sharing the same record (a copied configuration) sees
    # the same absence answer and cannot mint a second first steward.
    copied = StewardAuthority(store, verifier=verifier, clock=FakeClock())
    with pytest.raises(AuthorityError) as copied_refusal:
        copied.begin_bootstrap(
            connector=connector, admin="alice", successor="mallory"
        )
    assert copied_refusal.value.reason == "already_initialized"
    assert store.load("brain-a").steward == "bob"


def test_a_replayed_bootstrap_invitation_is_spent(tmp_path: Path) -> None:
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    engine, _store = _engine(tmp_path, verifier)
    challenge = engine.begin_bootstrap(
        connector=connector, admin="alice", successor="bob"
    )
    engine.accept_bootstrap(challenge.nonce, connector=connector, successor="bob")

    with pytest.raises(AuthorityError) as replay:
        engine.accept_bootstrap(
            challenge.nonce, connector=connector, successor="bob"
        )
    assert replay.value.reason == "challenge_used"


def test_bootstrap_acceptance_rechecks_the_admin(tmp_path: Path) -> None:
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    engine, store = _engine(tmp_path, verifier)
    challenge = engine.begin_bootstrap(
        connector=connector, admin="alice", successor="bob"
    )
    verifier.revoke(connector, "alice")

    with pytest.raises(AuthorityError) as revoked:
        engine.accept_bootstrap(challenge.nonce, connector=connector, successor="bob")
    assert revoked.value.reason == "member_only"
    assert store.load("brain-a") is None


# ── Challenge binding and expiry ────────────────────────────────────────────


def test_a_challenge_is_bound_to_its_brain_storage_and_successor(tmp_path: Path) -> None:
    clock = FakeClock()
    connector = BrainRef("brain-a", "github.com/acme/notes")
    other = BrainRef("brain-b", "github.com/acme/other")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    engine, _store = _engine(tmp_path, verifier, clock)
    challenge = engine.begin_bootstrap(
        connector=connector, admin="alice", successor="bob"
    )

    with pytest.raises(AuthorityError) as wrong_brain:
        engine.accept_bootstrap(
            challenge.nonce, connector=other, successor="bob"
        )
    assert wrong_brain.value.reason == "challenge_wrong_resource"

    with pytest.raises(AuthorityError) as wrong_successor:
        engine.accept_bootstrap(
            challenge.nonce, connector=connector, successor="carol"
        )
    assert wrong_successor.value.reason == "successor_mismatch"

    # Binding mismatches do not spend the invitation.
    transition = engine.accept_bootstrap(
        challenge.nonce, connector=connector, successor="bob"
    )
    assert transition.steward == "bob"


def test_a_challenge_expires_and_is_single_use(tmp_path: Path) -> None:
    clock = FakeClock()
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    engine, store = _engine(tmp_path, verifier, clock, ttl=30)

    expired = engine.begin_bootstrap(
        connector=connector, admin="alice", successor="bob"
    )
    clock.advance(31)
    with pytest.raises(AuthorityError) as refused:
        engine.accept_bootstrap(expired.nonce, connector=connector, successor="bob")
    assert refused.value.reason == "challenge_expired"
    assert store.load("brain-a") is None

    fresh = engine.begin_bootstrap(
        connector=connector, admin="alice", successor="bob"
    )
    assert fresh.nonce != expired.nonce
    engine.accept_bootstrap(fresh.nonce, connector=connector, successor="bob")
    with pytest.raises(AuthorityError) as replayed:
        engine.accept_bootstrap(fresh.nonce, connector=connector, successor="bob")
    assert replayed.value.reason == "challenge_used"


def test_a_challenge_is_bound_to_its_operation(tmp_path: Path) -> None:
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    verifier.grant(connector, "carol")
    engine, _store = _engine(tmp_path, verifier)
    _bootstrap(engine, connector, admin="alice", successor="bob")

    transfer = engine.begin_transfer(
        connector=connector,
        incumbent="bob",
        successor="carol",
        expected_generation=1,
    )
    with pytest.raises(AuthorityError) as wrong_operation:
        engine.accept_recovery(
            transfer.nonce, connector=connector, successor="carol"
        )
    assert wrong_operation.value.reason == "challenge_wrong_operation"


# ── Transfer ────────────────────────────────────────────────────────────────


def test_a_transfer_needs_the_incumbent_and_the_observed_generation(tmp_path: Path) -> None:
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    verifier.grant(connector, "carol")
    verifier.grant(connector, "mallory")
    engine, _store = _engine(tmp_path, verifier)
    _bootstrap(engine, connector, admin="alice", successor="bob")

    with pytest.raises(AuthorityError) as not_steward:
        engine.begin_transfer(
            connector=connector,
            incumbent="mallory",
            successor="carol",
            expected_generation=1,
        )
    assert not_steward.value.reason == "not_steward"

    with pytest.raises(AuthorityError) as stale:
        engine.begin_transfer(
            connector=connector,
            incumbent="bob",
            successor="carol",
            expected_generation=0,
        )
    assert stale.value.reason == "stale_generation"

    challenge = engine.begin_transfer(
        connector=connector,
        incumbent="bob",
        successor="carol",
        expected_generation=1,
    )
    assert challenge.actor == "bob"
    assert challenge.generation == 1


def test_transfer_acceptance_rotates_generation_and_fences_the_old_one(tmp_path: Path) -> None:
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    verifier.grant(connector, "carol")
    engine, store = _engine(tmp_path, verifier)
    _bootstrap(engine, connector, admin="alice", successor="bob")

    challenge = engine.begin_transfer(
        connector=connector,
        incumbent="bob",
        successor="carol",
        expected_generation=1,
    )
    # Before commit the incumbent still holds the record and the successor
    # holds nothing.
    engine.authorize_steward(connector=connector, principal="bob", generation=1)
    with pytest.raises(AuthorityError) as successor_early:
        engine.authorize_steward(connector=connector, principal="carol")
    assert successor_early.value.reason == "not_steward"

    transition = engine.accept_transfer(
        challenge.nonce, connector=connector, successor="carol"
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

    # An old generation is rejected immediately; the old steward is no longer
    # the steward even without a generation fence.
    with pytest.raises(AuthorityError) as old_generation:
        engine.authorize_steward(connector=connector, principal="bob", generation=1)
    assert old_generation.value.reason == "stale_generation"
    with pytest.raises(AuthorityError) as old_steward:
        engine.authorize_steward(connector=connector, principal="bob", generation=2)
    assert old_steward.value.reason == "not_steward"
    engine.authorize_steward(connector=connector, principal="carol", generation=2)

    with pytest.raises(AuthorityError) as replay:
        engine.accept_transfer(challenge.nonce, connector=connector, successor="carol")
    assert replay.value.reason == "challenge_used"


def test_transfer_acceptance_must_be_by_the_named_successor(tmp_path: Path) -> None:
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    verifier.grant(connector, "carol")
    verifier.grant(connector, "dave")
    engine, store = _engine(tmp_path, verifier)
    _bootstrap(engine, connector, admin="alice", successor="bob")
    challenge = engine.begin_transfer(
        connector=connector,
        incumbent="bob",
        successor="carol",
        expected_generation=1,
    )

    with pytest.raises(AuthorityError) as wrong_successor:
        engine.accept_transfer(challenge.nonce, connector=connector, successor="dave")
    assert wrong_successor.value.reason == "successor_mismatch"
    assert store.load("brain-a").steward == "bob"

    engine.accept_transfer(challenge.nonce, connector=connector, successor="carol")
    assert store.load("brain-a").steward == "carol"


def test_transfer_acceptance_reverifies_the_successor(tmp_path: Path) -> None:
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    verifier.grant(connector, "carol")
    engine, _store = _engine(tmp_path, verifier)
    _bootstrap(engine, connector, admin="alice", successor="bob")
    challenge = engine.begin_transfer(
        connector=connector,
        incumbent="bob",
        successor="carol",
        expected_generation=1,
    )
    verifier.revoke(connector, "carol")

    with pytest.raises(AuthorityError) as unverified:
        engine.accept_transfer(challenge.nonce, connector=connector, successor="carol")
    assert unverified.value.reason == "member_only"


def test_transfer_acceptance_rechecks_the_incumbent(tmp_path: Path) -> None:
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    verifier.grant(connector, "carol")
    engine, store = _engine(tmp_path, verifier)
    _bootstrap(engine, connector, admin="alice", successor="bob")
    challenge = engine.begin_transfer(
        connector=connector,
        incumbent="bob",
        successor="carol",
        expected_generation=1,
    )
    verifier.revoke(connector, "bob")

    with pytest.raises(AuthorityError) as revoked:
        engine.accept_transfer(challenge.nonce, connector=connector, successor="carol")
    assert revoked.value.reason == "member_only"
    record = store.load("brain-a")
    assert record is not None
    assert (record.steward, record.generation) == ("bob", 1)


def test_an_outstanding_invitation_is_invalidated_by_a_winning_transition(tmp_path: Path) -> None:
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    verifier.grant(connector, "carol")
    verifier.grant(connector, "dave")
    engine, _store = _engine(tmp_path, verifier)
    _bootstrap(engine, connector, admin="alice", successor="bob")

    first = engine.begin_transfer(
        connector=connector,
        incumbent="bob",
        successor="carol",
        expected_generation=1,
    )
    stale = engine.begin_transfer(
        connector=connector,
        incumbent="bob",
        successor="dave",
        expected_generation=1,
    )
    engine.accept_transfer(first.nonce, connector=connector, successor="carol")

    with pytest.raises(AuthorityError) as revoked:
        engine.accept_transfer(stale.nonce, connector=connector, successor="dave")
    assert revoked.value.reason == "challenge_unknown"


def test_authorize_steward_rechecks_current_verification(tmp_path: Path) -> None:
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    engine, _store = _engine(tmp_path, verifier)
    _bootstrap(engine, connector, admin="alice", successor="bob")

    verifier.revoke(connector, "bob")
    with pytest.raises(AuthorityError) as unverified:
        engine.authorize_steward(connector=connector, principal="bob", generation=1)
    assert unverified.value.reason == "member_only"


# ── Recovery ────────────────────────────────────────────────────────────────


def test_recovery_needs_a_fresh_admin_confirmation_and_successor_acceptance(tmp_path: Path) -> None:
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "mallory")
    verifier.grant(connector, "bob")
    verifier.grant(connector, "carol")
    engine, store = _engine(tmp_path, verifier)
    _bootstrap(engine, connector, admin="alice", successor="bob")
    # The incumbent is unavailable: only the connector administrator can start
    # recovery, and no incumbent evidence is consulted.
    verifier.revoke(connector, "bob")

    with pytest.raises(AuthorityError) as member_admin:
        engine.begin_recovery(
            connector=connector,
            admin="mallory",
            successor="carol",
            expected_generation=1,
            confirmed=True,
        )
    assert member_admin.value.reason == "not_admin"

    with pytest.raises(AuthorityError) as unconfirmed:
        engine.begin_recovery(
            connector=connector,
            admin="alice",
            successor="carol",
            expected_generation=1,
            confirmed=False,
        )
    assert unconfirmed.value.reason == "confirmation_required"

    challenge = engine.begin_recovery(
        connector=connector,
        admin="alice",
        successor="carol",
        expected_generation=1,
        confirmed=True,
    )
    transition = engine.accept_recovery(
        challenge.nonce, connector=connector, successor="carol"
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
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    verifier.grant(connector, "carol")
    verifier.grant(connector, "dave")
    engine, _store = _engine(tmp_path, verifier)
    _bootstrap(engine, connector, admin="alice", successor="bob")
    transfer = engine.begin_transfer(
        connector=connector,
        incumbent="bob",
        successor="carol",
        expected_generation=1,
    )
    engine.accept_transfer(transfer.nonce, connector=connector, successor="carol")

    with pytest.raises(AuthorityError) as stale:
        engine.begin_recovery(
            connector=connector,
            admin="alice",
            successor="dave",
            expected_generation=1,
            confirmed=True,
        )
    assert stale.value.reason == "stale_generation"

    challenge = engine.begin_recovery(
        connector=connector,
        admin="alice",
        successor="dave",
        expected_generation=2,
        confirmed=True,
    )
    engine.accept_recovery(challenge.nonce, connector=connector, successor="dave")
    assert _store.load("brain-a").generation == 3


def test_recovery_acceptance_rechecks_the_admin(tmp_path: Path) -> None:
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    verifier.grant(connector, "carol")
    engine, store = _engine(tmp_path, verifier)
    _bootstrap(engine, connector, admin="alice", successor="bob")
    challenge = engine.begin_recovery(
        connector=connector,
        admin="alice",
        successor="carol",
        expected_generation=1,
        confirmed=True,
    )
    verifier.revoke(connector, "alice")

    with pytest.raises(AuthorityError) as revoked:
        engine.accept_recovery(challenge.nonce, connector=connector, successor="carol")
    assert revoked.value.reason == "member_only"
    record = store.load("brain-a")
    assert record is not None
    assert (record.steward, record.generation) == ("bob", 1)


def test_an_expired_recovery_cannot_be_replayed(tmp_path: Path) -> None:
    clock = FakeClock()
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = RecordingVerifier()
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    verifier.grant(connector, "carol")
    engine, store = _engine(tmp_path, verifier, clock, ttl=20)
    _bootstrap(engine, connector, admin="alice", successor="bob")
    challenge = engine.begin_recovery(
        connector=connector,
        admin="alice",
        successor="carol",
        expected_generation=1,
        confirmed=True,
    )
    clock.advance(21)

    with pytest.raises(AuthorityError) as expired:
        engine.accept_recovery(challenge.nonce, connector=connector, successor="carol")
    assert expired.value.reason == "challenge_expired"
    assert store.load("brain-a").steward == "bob"


# ── Scoping and evidence ────────────────────────────────────────────────────


def test_a_steward_role_in_one_brain_confers_nothing_in_another(tmp_path: Path) -> None:
    first = BrainRef("brain-a", "github.com/acme/notes")
    second = BrainRef("brain-b", "github.com/acme/other")
    verifier = RecordingVerifier()
    verifier.grant(first, "alice", admin=True)
    verifier.grant(first, "bob")
    verifier.grant(second, "alice", admin=True)
    verifier.grant(second, "carol")
    engine, _store = _engine(tmp_path, verifier)
    _bootstrap(engine, first, admin="alice", successor="bob")
    _bootstrap(engine, second, admin="alice", successor="carol")

    engine.authorize_steward(connector=first, principal="bob", generation=1)
    with pytest.raises(AuthorityError) as elsewhere:
        engine.authorize_steward(connector=second, principal="bob", generation=1)
    assert elsewhere.value.reason == "not_steward"
    with pytest.raises(AuthorityError) as transfer_elsewhere:
        engine.begin_transfer(
            connector=second,
            incumbent="bob",
            successor="dave",
            expected_generation=1,
        )
    assert transfer_elsewhere.value.reason == "not_steward"


def test_evidence_must_be_for_the_same_resource_and_principal(tmp_path: Path) -> None:
    connector = BrainRef("brain-a", "github.com/acme/notes")
    other = BrainRef("brain-b", "github.com/acme/other")

    wrong_resource = RecordingVerifier()
    wrong_resource.grant(connector, "alice", admin=True, storage=other.storage)
    engine, _store = _engine(tmp_path / "resource", wrong_resource)
    with pytest.raises(AuthorityError) as resource_refusal:
        engine.begin_bootstrap(
            connector=connector, admin="alice", successor="bob"
        )
    assert resource_refusal.value.reason == "wrong_resource"

    wrong_principal = RecordingVerifier()
    wrong_principal.grant(connector, "alice", admin=True, evidence_principal="eve")
    engine, _store = _engine(tmp_path / "principal", wrong_principal)
    with pytest.raises(AuthorityError) as principal_refusal:
        engine.begin_bootstrap(
            connector=connector, admin="alice", successor="bob"
        )
    assert principal_refusal.value.reason == "wrong_principal"

    no_revision = RecordingVerifier()
    no_revision.grant(connector, "alice", admin=True, revision="")
    engine, _store = _engine(tmp_path / "revision", no_revision)
    with pytest.raises(AuthorityError) as revision_refusal:
        engine.begin_bootstrap(
            connector=connector, admin="alice", successor="bob"
        )
    assert revision_refusal.value.reason == "member_only"


# ── Races ───────────────────────────────────────────────────────────────────


def test_two_initial_enrollments_race_to_one_record(tmp_path: Path) -> None:
    store = ProtectedAuthorityStore(tmp_path / "authority")
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = BarrierVerifier({"bob", "dave"})
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    verifier.grant(connector, "carol", admin=True)
    verifier.grant(connector, "dave")
    first = StewardAuthority(store, verifier=verifier, clock=FakeClock())
    second = StewardAuthority(store, verifier=verifier, clock=FakeClock())
    first_challenge = first.begin_bootstrap(
        connector=connector, admin="alice", successor="bob"
    )
    second_challenge = second.begin_bootstrap(
        connector=connector, admin="carol", successor="dave"
    )

    outcomes = _race(
        lambda: first.accept_bootstrap(
            first_challenge.nonce, connector=connector, successor="bob"
        ),
        lambda: second.accept_bootstrap(
            second_challenge.nonce, connector=connector, successor="dave"
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


def test_two_transfers_race_and_only_one_generation_wins(tmp_path: Path) -> None:
    store = ProtectedAuthorityStore(tmp_path / "authority")
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = BarrierVerifier({"carol", "dave"})
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    verifier.grant(connector, "carol")
    verifier.grant(connector, "dave")
    first = StewardAuthority(store, verifier=verifier, clock=FakeClock())
    second = StewardAuthority(store, verifier=verifier, clock=FakeClock())
    _bootstrap(first, connector, admin="alice", successor="bob")
    first_challenge = first.begin_transfer(
        connector=connector,
        incumbent="bob",
        successor="carol",
        expected_generation=1,
    )
    second_challenge = second.begin_transfer(
        connector=connector,
        incumbent="bob",
        successor="dave",
        expected_generation=1,
    )

    outcomes = _race(
        lambda: first.accept_transfer(
            first_challenge.nonce, connector=connector, successor="carol"
        ),
        lambda: second.accept_transfer(
            second_challenge.nonce, connector=connector, successor="dave"
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

    # Only the accepted successor of the winning transfer may act afterwards.
    verifier.successors.clear()
    loser = "carol" if record.steward == "dave" else "dave"
    first.authorize_steward(
        connector=connector, principal=record.steward, generation=2
    )
    with pytest.raises(AuthorityError) as refused:
        first.authorize_steward(connector=connector, principal=loser, generation=2)
    assert refused.value.reason == "not_steward"


def test_transfer_and_recovery_race_and_one_transition_wins(tmp_path: Path) -> None:
    store = ProtectedAuthorityStore(tmp_path / "authority")
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier = BarrierVerifier({"carol", "dave"})
    verifier.grant(connector, "alice", admin=True)
    verifier.grant(connector, "bob")
    verifier.grant(connector, "carol")
    verifier.grant(connector, "dave")
    first = StewardAuthority(store, verifier=verifier, clock=FakeClock())
    second = StewardAuthority(store, verifier=verifier, clock=FakeClock())
    _bootstrap(first, connector, admin="alice", successor="bob")
    transfer = first.begin_transfer(
        connector=connector,
        incumbent="bob",
        successor="carol",
        expected_generation=1,
    )
    recovery = second.begin_recovery(
        connector=connector,
        admin="alice",
        successor="dave",
        expected_generation=1,
        confirmed=True,
    )

    outcomes = _race(
        lambda: first.accept_transfer(transfer.nonce, connector=connector, successor="carol"),
        lambda: second.accept_recovery(recovery.nonce, connector=connector, successor="dave"),
    )
    kinds = sorted(kind for kind, _value in outcomes)
    assert kinds == ["error", "ok"]
    error = next(value for kind, value in outcomes if kind == "error")
    assert error == "stale_generation"
    record = store.load("brain-a")
    assert record is not None
    assert record.generation == 2
    assert record.steward in {"carol", "dave"}
    verifier.successors.clear()


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

    # A replace must be exactly one generation forward; a caller cannot skip.
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

    verifier = RecordingVerifier()
    connector = BrainRef("brain-a", "github.com/acme/notes")
    verifier.grant(connector, "alice", admin=True)
    engine = StewardAuthority(store, verifier=verifier, clock=FakeClock())
    with pytest.raises(AuthorityError) as unavailable:
        engine.begin_bootstrap(
            connector=connector, admin="alice", successor="bob"
        )
    assert unavailable.value.reason == "store_unavailable"

    # A symlink where the authority document belongs is refused, not followed.
    path.unlink()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    os.chmod(target, 0o600)
    os.symlink(target, path)
    with pytest.raises(StoreError) as unsafe:
        store.load("brain-a")
    assert unsafe.value.reason == "unsafe_store"
