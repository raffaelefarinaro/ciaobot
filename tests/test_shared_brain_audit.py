"""Durable steward audit: persistence, rotation, tamper, crash and restart replay (#3628).

Pure unit tests over a temporary directory, an injected clock and the synthetic
fenced backend from the authority tests. No routes, no Settings, no live data.
"""

from __future__ import annotations

import json
import math
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ciao.shared_brain_audit import (
    INTENT_MAX_AGE_S,
    MAX_FIELD_BYTES,
    MAX_LINE_BYTES,
    AuditError,
    AuditKind,
    DurableAuditLog,
)
from ciao.shared_brain_authority import (
    _storage_digest,
    AuthorityError,
    BrainRef,
    ProtectedAuthorityStore,
    StewardAuthority,
)
from tests.test_shared_brain_authority import (
    BarrierBackend,
    FakeClock,
    FencedBackend,
    _add_people,
    _race,
)

CONNECTOR = BrainRef("team-brain", "git@example.invalid:team/brain.git")


def _fields(n: int = 1, **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "intent": f"intent-{n}",
        "operation": "transfer",
        "brain": "b",
        "storage": "s",
        "actor": "bob",
        "previous_steward": "bob",
        "steward": "carol",
        "previous_generation": 1,
        "generation": 2,
        "at": 1.5,
    }
    base.update(over)
    return base


def _append(log: DurableAuditLog, n: int = 1, kind: AuditKind = AuditKind.INTENT):
    return log.append(kind, **_fields(n))  # type: ignore[arg-type]


def _files(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.name.startswith("audit"))


# ── The log on its own ───────────────────────────────────────────────────


def test_round_trip_private_modes_and_restart_replay(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    log = DurableAuditLog(root)
    for n in range(3):
        _append(log, n)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for name in _files(root):
        assert stat.S_IMODE((root / name).stat().st_mode) == 0o600
    reopened = DurableAuditLog(root).view()
    assert [e.seq for e in reopened.entries] == [1, 2, 3]
    assert reopened.pruned == 0
    assert reopened.entries == log.view().entries
    # The chain continues across a restart.
    assert _append(DurableAuditLog(root), 4).seq == 4


def test_rotation_is_bounded_and_counts_what_it_drops(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    log = DurableAuditLog(root, segment_records=3, max_segments=2)
    for n in range(20):
        _append(log, n)
    view = DurableAuditLog(root, segment_records=3, max_segments=2).view()
    sealed = [name for name in _files(root) if name.count(".") == 2 and name[6].isdigit()]
    assert len(sealed) <= 2
    assert view.pruned > 0
    assert view.pruned + len(view.entries) == 20
    assert view.first_seq == view.pruned + 1
    assert [e.seq for e in view.entries] == list(range(view.first_seq, 21))
    assert len(view.entries) <= 3 * 3  # two sealed segments plus the active one


def test_pruning_is_never_silent_without_an_anchor(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    log = DurableAuditLog(root, segment_records=2, max_segments=1)
    for n in range(10):
        _append(log, n)
    state = json.loads((root / "audit.state.json").read_text())
    assert state["pruned"] == log.view().pruned > 0
    # Deleting the oldest retained segment (a silent truncation) is detected.
    oldest = min(root.glob("audit.0*.log"))
    oldest.unlink()
    with pytest.raises(AuditError) as exc:
        log.view()
    assert exc.value.reason == "corrupt"


@pytest.mark.parametrize(
    "mutate",
    [
        "edit_field",
        "drop_middle_line",
        "swap_lines",
        "truncate_tail_record",
        "delete_state",
        "bad_state_schema",
        "insert_forged_line",
        "garbage_middle",
    ],
)
def test_tamper_is_refused_never_read_as_empty(tmp_path: Path, mutate: str) -> None:
    root = tmp_path / "audit"
    log = DurableAuditLog(root)
    for n in range(4):
        _append(log, n)
    active = root / "audit.log"
    lines = active.read_text().splitlines(keepends=True)
    if mutate == "edit_field":
        doc = json.loads(lines[1])
        doc["body"]["steward"] = "mallory"
        lines[1] = json.dumps(doc, sort_keys=True) + "\n"
        active.write_text("".join(lines))
    elif mutate == "drop_middle_line":
        del lines[1]
        active.write_text("".join(lines))
    elif mutate == "swap_lines":
        lines[1], lines[2] = lines[2], lines[1]
        active.write_text("".join(lines))
    elif mutate == "truncate_tail_record":
        del lines[-2:]
        active.write_text("".join(lines))
    elif mutate == "delete_state":
        (root / "audit.state.json").unlink()
    elif mutate == "bad_state_schema":
        path = root / "audit.state.json"
        doc = json.loads(path.read_text())
        doc["schema"] = 2
        path.write_text(json.dumps(doc))
    elif mutate == "insert_forged_line":
        doc = json.loads(lines[0])
        doc["body"]["seq"] = 5
        lines.append(json.dumps(doc, sort_keys=True) + "\n")
        active.write_text("".join(lines))
    elif mutate == "garbage_middle":
        lines[2] = "not json\n"
        active.write_text("".join(lines))
    os.chmod(active, 0o600)
    with pytest.raises(AuditError) as exc:
        DurableAuditLog(root).view()
    assert exc.value.reason == "corrupt"
    with pytest.raises(AuditError):
        _append(DurableAuditLog(root), 9)


def test_unsafe_files_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    log = DurableAuditLog(root)
    _append(log)
    active = root / "audit.log"
    os.chmod(active, 0o644)
    with pytest.raises(AuditError) as exc:
        log.view()
    assert exc.value.reason == "unsafe_store"
    os.chmod(active, 0o600)
    moved = tmp_path / "elsewhere"
    active.rename(moved)
    active.symlink_to(moved)
    with pytest.raises(AuditError) as exc2:
        log.view()
    assert exc2.value.reason == "unsafe_store"
    active.unlink()
    moved.rename(active)
    os.chmod(root, 0o755)
    with pytest.raises(AuditError) as exc3:
        log.view()
    assert exc3.value.reason == "unsafe_root"


def test_root_symlink_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(AuditError) as exc:
        DurableAuditLog(link)
    assert exc.value.reason == "unsafe_root"


def test_torn_final_line_is_truncated_and_the_chain_resumes(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    log = DurableAuditLog(root)
    _append(log, 1)
    _append(log, 2)
    with open(root / "audit.log", "ab") as handle:
        handle.write(b'{"body": {"seq": 3, "kin')
    reopened = DurableAuditLog(root)
    assert [e.seq for e in reopened.view().entries] == [1, 2]
    assert _append(reopened, 3).seq == 3
    assert [e.seq for e in DurableAuditLog(root).view().entries] == [1, 2, 3]


def test_crash_after_record_before_state_adopts_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "audit"
    log = DurableAuditLog(root)
    _append(log, 1)

    def boom(self: DurableAuditLog, state: object) -> None:
        raise AuditError("durability_unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(DurableAuditLog, "_write_state", boom)
        with pytest.raises(AuditError):
            _append(log, 2)
    reopened = DurableAuditLog(root)
    assert [e.seq for e in reopened.view().entries] == [1, 2]
    assert _append(reopened, 3).seq == 3


def test_first_ever_record_survives_a_crash_before_any_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "audit"
    log = DurableAuditLog(root)
    monkeypatch.setattr(
        DurableAuditLog,
        "_write_state",
        lambda self, state: (_ for _ in ()).throw(AuditError("durability_unavailable")),
    )
    with pytest.raises(AuditError):
        _append(log, 1)
    monkeypatch.undo()
    assert [e.seq for e in DurableAuditLog(root).view().entries] == [1]


def test_two_records_beyond_the_state_is_corruption(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    log = DurableAuditLog(root)
    for n in range(3):
        _append(log, n)
    state = root / "audit.state.json"
    saved = state.read_text()
    for n in range(2):
        _append(log, 10 + n)
    state.write_text(saved)
    os.chmod(state, 0o600)
    with pytest.raises(AuditError) as exc:
        DurableAuditLog(root).view()
    assert exc.value.reason == "corrupt"


def test_crash_between_rotation_and_next_write(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    log = DurableAuditLog(root, segment_records=2, max_segments=3)
    _append(log, 1)
    _append(log, 2)
    (root / "audit.log").rename(root / "audit.000000000001.log")  # sealed, no active
    reopened = DurableAuditLog(root, segment_records=2, max_segments=3)
    assert [e.seq for e in reopened.view().entries] == [1, 2]
    assert _append(reopened, 3).seq == 3


def test_crash_between_prune_anchor_and_unlink(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    log = DurableAuditLog(root, segment_records=2, max_segments=1)
    for n in range(4):
        _append(log, n)
    survivors = {p.name: p.read_bytes() for p in root.glob("audit.0*.log")}
    for n in range(4, 8):
        _append(log, n)
    pruned_before = log.view().pruned
    assert pruned_before > 0
    # Resurrect a segment the anchor already gave up, as a crash before unlink would.
    for name, data in survivors.items():
        if not (root / name).exists():
            (root / name).write_bytes(data)
            os.chmod(root / name, 0o600)
    healed = DurableAuditLog(root, segment_records=2, max_segments=1).view()
    assert healed.pruned == pruned_before
    assert healed.entries[0].seq == healed.first_seq


def test_fsync_failure_is_reported_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = DurableAuditLog(tmp_path / "audit")

    def boom(fd: int) -> None:
        raise OSError("disk")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(AuditError) as exc:
        _append(log)
    assert exc.value.reason == "durability_unavailable"


def test_invalid_entries_are_refused_before_any_write(tmp_path: Path) -> None:
    log = DurableAuditLog(tmp_path / "audit")
    with pytest.raises(AuditError) as exc:
        log.append(AuditKind.INTENT, **_fields(steward=""))  # type: ignore[arg-type]
    assert exc.value.reason == "invalid_entry"
    with pytest.raises(AuditError):
        log.append(AuditKind.INTENT, **_fields(generation=0))  # type: ignore[arg-type]
    assert log.view().entries == ()


def test_concurrent_appends_stay_contiguous(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    log = DurableAuditLog(root, segment_records=5, max_segments=100)
    other = DurableAuditLog(root, segment_records=5, max_segments=100)

    def work(handle: DurableAuditLog, base: int) -> None:
        for n in range(10):
            _append(handle, base + n)

    threads = [
        threading.Thread(target=work, args=(log, 0)),
        threading.Thread(target=work, args=(other, 100)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [e.seq for e in log.view().entries] == list(range(1, 21))


def test_pending_lists_only_unresolved_intents(tmp_path: Path) -> None:
    log = DurableAuditLog(tmp_path / "audit")
    _append(log, 1)
    _append(log, 2)
    _append(log, 1, AuditKind.COMMITTED)
    assert [e.intent for e in log.pending()] == ["intent-2"]


# ── The engine with a durable audit ──────────────────────────────────────


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.clock = FakeClock()
        self.backend = FencedBackend(self.clock)
        _add_people(self.backend, CONNECTOR)
        self.audit_root = tmp_path / "audit"
        self.engine = self.new_engine()

    def new_engine(self, backend: FencedBackend | None = None) -> StewardAuthority:
        return StewardAuthority(
            ProtectedAuthorityStore(self.tmp / "authority"),
            backend=backend or self.backend,
            clock=self.clock,
            audit=DurableAuditLog(self.audit_root),
            wall_clock=lambda: 1_700_000_000.0,
        )

    def sign(self, token: str):
        return self.backend.sign_in(CONNECTOR, token)

    def bootstrap(self, engine: StewardAuthority | None = None) -> None:
        engine = engine or self.engine
        successor = self.sign("bob-token")
        challenge = engine.begin_bootstrap(
            connector=CONNECTOR, admin=self.sign("alice-token"), successor="bob"
        )
        engine.accept_bootstrap(challenge.nonce, connector=CONNECTOR, successor=successor)

    def begin_transfer(self, engine: StewardAuthority | None = None, to: str = "carol", gen: int = 1):
        engine = engine or self.engine
        return engine.begin_transfer(
            connector=CONNECTOR,
            incumbent=self.sign("bob-token"),
            successor=to,
            expected_generation=gen,
        )

    def transfer(self, engine: StewardAuthority | None = None) -> None:
        engine = engine or self.engine
        challenge = self.begin_transfer(engine)
        engine.accept_transfer(
            challenge.nonce, connector=CONNECTOR, successor=self.sign("carol-token")
        )

    def entries(self):
        return DurableAuditLog(self.audit_root).view().entries


def test_bootstrap_transfer_and_recovery_leave_durable_records(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.bootstrap()
    rig.transfer()
    challenge = rig.engine.begin_recovery(
        connector=CONNECTOR,
        admin=rig.sign("alice-token"),
        successor="dave",
        expected_generation=2,
        confirmed=True,
    )
    rig.engine.accept_recovery(
        challenge.nonce, connector=CONNECTOR, successor=rig.sign("dave-token")
    )
    entries = rig.entries()
    assert [(e.operation, e.kind.value) for e in entries] == [
        ("bootstrap", "intent"),
        ("bootstrap", "committed"),
        ("transfer", "intent"),
        ("transfer", "committed"),
        ("recovery", "intent"),
        ("recovery", "committed"),
    ]
    transfer = entries[3]
    assert (transfer.actor, transfer.previous_steward, transfer.steward) == ("bob", "bob", "carol")
    assert (transfer.previous_generation, transfer.generation) == (1, 2)
    assert transfer.at == 1_700_000_000.0 and not transfer.recovered
    recovery = entries[5]
    assert (recovery.actor, recovery.previous_steward, recovery.steward) == ("alice", "carol", "dave")
    assert entries[0].previous_steward is None and entries[0].generation == 1
    # Intent and resolution share an id; the in-memory trail still works.
    assert entries[2].intent == entries[3].intent != entries[4].intent
    assert len(rig.engine.audit_trail()) == 3
    assert rig.engine.durable_audit() == entries


def test_restart_replay_keeps_audit_and_authority(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.bootstrap()
    rig.transfer()
    before = rig.entries()
    restarted = rig.new_engine()
    assert restarted.reconcile_audit() == 0
    assert restarted.durable_audit() == before
    record = restarted.authorize_steward(
        connector=CONNECTOR, principal=rig.sign("carol-token"), generation=2
    )
    assert record.steward == "carol"
    with pytest.raises(AuthorityError) as exc:
        restarted.authorize_steward(connector=CONNECTOR, principal=rig.sign("bob-token"))
    assert exc.value.reason == "not_steward"


def test_audit_never_records_nonces_or_seals(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.bootstrap()
    challenge = rig.begin_transfer()
    rig.engine.accept_transfer(
        challenge.nonce, connector=CONNECTOR, successor=rig.sign("carol-token")
    )
    capability = rig.sign("carol-token")
    raw = "".join(p.read_text() for p in rig.audit_root.glob("audit*"))
    assert challenge.nonce not in raw
    assert capability.seal not in raw


def test_crash_after_commit_before_confirmation_is_completed_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    rig.bootstrap()
    challenge = rig.begin_transfer()
    original = DurableAuditLog.append

    def flaky(self: DurableAuditLog, kind: AuditKind, **kw: object):
        if kind is AuditKind.COMMITTED:
            raise AuditError("durability_unavailable")
        return original(self, kind, **kw)  # type: ignore[arg-type]

    with monkeypatch.context() as patch:
        patch.setattr(DurableAuditLog, "append", flaky)
        with pytest.raises(AuthorityError) as exc:
            rig.engine.accept_transfer(
                challenge.nonce, connector=CONNECTOR, successor=rig.sign("carol-token")
            )
    assert exc.value.reason == "audit_unconfirmed"
    assert [e.kind.value for e in rig.entries()][-1] == "intent"
    # The spent admission is not replayed: the restart only writes the resolution.
    admissions_before = len(rig.backend.used)
    restarted = rig.new_engine()
    last = rig.entries()[-1]
    assert last.kind is AuditKind.COMMITTED and last.recovered
    assert len(rig.backend.used) == admissions_before
    assert restarted.authorize_steward(
        connector=CONNECTOR, principal=rig.sign("carol-token"), generation=2
    ).steward == "carol"


def test_crash_before_mutation_is_recorded_aborted_on_restart(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.bootstrap()
    challenge = rig.begin_transfer()

    def explode(admission: object, mutation: object) -> None:
        raise RuntimeError("process died inside the fence")

    original = rig.backend.commit
    rig.backend.commit = explode  # type: ignore[method-assign,assignment]
    with pytest.raises(AuthorityError) as exc:
        rig.engine.accept_transfer(
            challenge.nonce, connector=CONNECTOR, successor=rig.sign("carol-token")
        )
    assert exc.value.reason == "provider_fencing_unavailable"
    assert rig.entries()[-1].kind is AuditKind.INTENT
    rig.backend.commit = original  # type: ignore[method-assign]
    rig.new_engine()
    last = rig.entries()[-1]
    assert last.kind is AuditKind.ABORTED and last.recovered
    # The incumbent still holds generation 1.
    assert rig.new_engine().authorize_steward(
        connector=CONNECTOR, principal=rig.sign("bob-token"), generation=1
    )


def test_crash_before_first_bootstrap_commit_is_aborted(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    challenge = rig.engine.begin_bootstrap(
        connector=CONNECTOR, admin=rig.sign("alice-token"), successor="bob"
    )
    rig.backend.commit = lambda a, m: (_ for _ in ()).throw(RuntimeError("died"))  # type: ignore[method-assign,assignment]
    with pytest.raises(AuthorityError):
        rig.engine.accept_bootstrap(
            challenge.nonce, connector=CONNECTOR, successor=rig.sign("bob-token")
        )
    rig.new_engine()
    assert rig.entries()[-1].kind is AuditKind.ABORTED


def test_intent_failure_blocks_the_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    rig.bootstrap()
    challenge = rig.begin_transfer()
    used_before = set(rig.backend.used)
    original = DurableAuditLog.append

    def no_intent(self: DurableAuditLog, kind: AuditKind, **kw: object):
        if kind is AuditKind.INTENT:
            raise AuditError("durability_unavailable")
        return original(self, kind, **kw)  # type: ignore[arg-type]

    with monkeypatch.context() as patch:
        patch.setattr(DurableAuditLog, "append", no_intent)
        with pytest.raises(AuthorityError) as exc:
            rig.engine.accept_transfer(
                challenge.nonce, connector=CONNECTOR, successor=rig.sign("carol-token")
            )
    assert exc.value.reason == "audit_unavailable"
    assert rig.backend.used == used_before  # the fence was never reached
    assert rig.new_engine().authorize_steward(
        connector=CONNECTOR, principal=rig.sign("bob-token"), generation=1
    ).steward == "bob"


def test_definite_refusal_is_aborted_immediately(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.bootstrap()
    challenge = rig.begin_transfer()
    rig.backend.revoke_before_commit = "carol"
    with pytest.raises(AuthorityError) as exc:
        rig.engine.accept_transfer(
            challenge.nonce, connector=CONNECTOR, successor=rig.sign("carol-token")
        )
    assert exc.value.reason == "admission_rejected"
    kinds = [e.kind.value for e in rig.entries()][-2:]
    assert kinds == ["intent", "aborted"]
    assert not rig.entries()[-1].recovered
    assert DurableAuditLog(rig.audit_root).pending() == ()


def test_racing_transfers_leave_one_commit_and_one_abort_not_a_reconcile(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.backend = BarrierBackend(rig.clock, {"carol", "dave"})
    _add_people(rig.backend, CONNECTOR)
    rig.engine = rig.new_engine(rig.backend)
    rig.bootstrap()
    carol = rig.begin_transfer(to="carol")
    dave = rig.begin_transfer(to="dave")
    outcomes = _race(
        lambda: rig.engine.accept_transfer(
            carol.nonce, connector=CONNECTOR, successor=rig.sign("carol-token")
        ),
        lambda: rig.engine.accept_transfer(
            dave.nonce, connector=CONNECTOR, successor=rig.sign("dave-token")
        ),
    )
    assert sorted(kind for kind, _ in outcomes) == ["error", "ok"]
    tail = [e for e in rig.entries() if e.operation == "transfer"]
    assert sorted(e.kind.value for e in tail) == ["aborted", "committed", "intent", "intent"]
    assert not any(e.recovered for e in tail)


def test_inconsistent_record_keeps_the_engine_closed(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.bootstrap()
    rig.transfer()  # record is generation 2, steward carol
    DurableAuditLog(rig.audit_root).append(
        AuditKind.INTENT,
        intent="ghost",
        operation="transfer",
        brain=CONNECTOR.brain,
        storage=CONNECTOR.storage,
        actor="bob",
        previous_steward="bob",
        steward="mallory",
        previous_generation=7,
        generation=8,
        at=1.0,
    )
    with pytest.raises(AuthorityError) as exc:
        rig.new_engine()
    assert exc.value.reason == "audit_inconsistent"


def test_a_corrupt_audit_fails_closed_for_construction_and_for_transitions(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.bootstrap()
    challenge = rig.begin_transfer()
    active = rig.audit_root / "audit.log"
    lines = active.read_text().splitlines(keepends=True)
    del lines[0]
    active.write_text("".join(lines))
    os.chmod(active, 0o600)
    with pytest.raises(AuthorityError) as exc:
        rig.engine.accept_transfer(
            challenge.nonce, connector=CONNECTOR, successor=rig.sign("carol-token")
        )
    assert exc.value.reason == "audit_unavailable"
    with pytest.raises(AuthorityError) as exc2:
        rig.new_engine()
    assert exc2.value.reason == "audit_unavailable"


def test_without_a_durable_audit_the_engine_is_unchanged(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = FencedBackend(clock)
    _add_people(backend, CONNECTOR)
    engine = StewardAuthority(
        ProtectedAuthorityStore(tmp_path / "authority"), backend=backend, clock=clock
    )
    assert engine.reconcile_audit() == 0
    assert engine.durable_audit() == ()


# ── Review round: leases, single resolution, caps, digest, temporaries ───

REPO = str(Path(__file__).resolve().parents[1])
_CHILD = """
import sys
from ciao.shared_brain_audit import AuditKind, DurableAuditLog
from ciao.shared_brain_authority import _storage_digest
root, brain, storage = sys.argv[1:4]
DurableAuditLog(root).append(
    AuditKind.INTENT, intent="live-intent", operation="bootstrap", brain=brain,
    storage=_storage_digest(storage), actor="alice", previous_steward=None, steward="bob",
    previous_generation=0, generation=1, at=__import__("time").time(),
)
print("ready", flush=True)
sys.stdin.read()
"""


def _spawn_owner(rig: Rig) -> subprocess.Popen[str]:
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(rig.audit_root), CONNECTOR.brain, CONNECTOR.storage],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": REPO},
    )
    assert child.stdout is not None and child.stdout.readline().strip() == "ready"
    return child


def _resolutions(rig: Rig, intent: str):
    return [e for e in rig.entries() if e.intent == intent and e.kind is not AuditKind.INTENT]


def test_another_process_live_intent_is_never_settled_and_an_orphan_is_settled_once(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    child = _spawn_owner(rig)
    try:
        other = rig.new_engine()  # engine B starts while A's intent is live
        assert other.reconcile_audit() == 0
        assert rig.new_engine().reconcile_audit() == 0
        assert _resolutions(rig, "live-intent") == []
        assert [e.intent for e in DurableAuditLog(rig.audit_root).pending()] == ["live-intent"]
    finally:
        assert child.stdin is not None
        child.kill()
        child.wait()
    # The owner is gone: it is a genuine orphan, settled exactly once.
    settler = rig.new_engine()  # construction replays and settles the orphan
    resolved = _resolutions(rig, "live-intent")
    assert len(resolved) == 1 and resolved[0].kind is AuditKind.ABORTED and resolved[0].recovered
    assert settler.reconcile_audit() == 0
    assert rig.new_engine().reconcile_audit() == 0
    assert len(_resolutions(rig, "live-intent")) == 1


def test_a_live_owner_is_never_settled_by_age_or_a_clock_step(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    child = _spawn_owner(rig)
    try:
        for jump in (INTENT_MAX_AGE_S + 5, 10 * 365 * 86400.0):
            late = StewardAuthority(
                ProtectedAuthorityStore(rig.tmp / "authority"),
                backend=rig.backend,
                clock=rig.clock,
                audit=DurableAuditLog(rig.audit_root),
                wall_clock=lambda jump=jump: time.time() + jump,
            )
            assert late.reconcile_audit() == 0
            assert _resolutions(rig, "live-intent") == []
    finally:
        child.kill()
        child.wait()


def test_an_intent_with_undeterminable_liveness_is_settled_only_by_age(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    log = DurableAuditLog(rig.audit_root)
    parent = os.getppid()  # exists, but under a boot id this process cannot confirm
    log.append(
        AuditKind.INTENT,
        intent="maybe",
        operation="bootstrap",
        brain=CONNECTOR.brain,
        storage=_storage_digest(CONNECTOR.storage),
        actor="alice",
        previous_steward=None,
        steward="bob",
        previous_generation=0,
        generation=1,
        at=1_000.0,
        owner=f"{parent}:another-boot",
    )
    young = StewardAuthority(
        ProtectedAuthorityStore(rig.tmp / "authority"),
        backend=rig.backend,
        clock=rig.clock,
        audit=DurableAuditLog(rig.audit_root),
        wall_clock=lambda: 1_000.0 + INTENT_MAX_AGE_S - 1,
    )
    assert _resolutions(rig, "maybe") == [] and young.reconcile_audit() == 0
    old = StewardAuthority(
        ProtectedAuthorityStore(rig.tmp / "authority"),
        backend=rig.backend,
        clock=rig.clock,
        audit=DurableAuditLog(rig.audit_root),
        wall_clock=lambda: 1_000.0 + INTENT_MAX_AGE_S,
    )
    assert len(_resolutions(rig, "maybe")) == 1 and old.reconcile_audit() == 0


def test_a_hung_committer_in_this_process_is_not_settled_by_a_later_engine(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.bootstrap()
    challenge = rig.begin_transfer()
    entered, release = threading.Event(), threading.Event()
    original = rig.backend.commit

    def slow(admission: object, mutation: object):
        entered.set()
        assert release.wait(10)
        return original(admission, mutation)  # type: ignore[arg-type]

    rig.backend.commit = slow  # type: ignore[method-assign,assignment]
    outcome: list[object] = []
    worker = threading.Thread(
        target=lambda: outcome.append(
            rig.engine.accept_transfer(
                challenge.nonce, connector=CONNECTOR, successor=rig.sign("carol-token")
            )
        )
    )
    worker.start()
    assert entered.wait(10)
    late = StewardAuthority(
        ProtectedAuthorityStore(rig.tmp / "authority"),
        backend=rig.backend,
        clock=rig.clock,
        audit=DurableAuditLog(rig.audit_root),
        wall_clock=lambda: time.time() + 10 * INTENT_MAX_AGE_S,
    )
    assert late.reconcile_audit() == 0
    release.set()
    worker.join(10)
    kinds = [(e.kind.value, e.recovered) for e in rig.entries() if e.operation == "transfer"]
    assert kinds == [("intent", False), ("committed", False)]


def test_a_post_commit_duplicate_resolution_is_an_inconsistency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    rig.bootstrap()
    challenge = rig.begin_transfer()
    original = DurableAuditLog.append

    def settled_by_someone_else(self: DurableAuditLog, kind: AuditKind, **kw: object):
        if kind is AuditKind.COMMITTED:
            raise AuditError("duplicate_resolution")
        return original(self, kind, **kw)  # type: ignore[arg-type]

    with monkeypatch.context() as patch:
        patch.setattr(DurableAuditLog, "append", settled_by_someone_else)
        with pytest.raises(AuthorityError) as exc:
            rig.engine.accept_transfer(
                challenge.nonce, connector=CONNECTOR, successor=rig.sign("carol-token")
            )
    assert exc.value.reason == "audit_inconsistent"


def test_a_recovered_abort_that_contradicts_the_record_is_flagged(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.bootstrap()  # record: generation 1, steward bob
    log = DurableAuditLog(rig.audit_root)
    fields = dict(
        operation="bootstrap",
        brain=CONNECTOR.brain,
        storage=_storage_digest(CONNECTOR.storage),
        actor="alice",
        previous_steward=None,
        steward="bob",
        previous_generation=0,
        generation=1,
        at=1.0,
    )
    log.append(AuditKind.INTENT, intent="late", owner="999999:x", **fields)  # type: ignore[arg-type]
    log.append(AuditKind.ABORTED, intent="late", recovered=True, **fields)  # type: ignore[arg-type]
    with pytest.raises(AuthorityError) as exc:
        rig.new_engine()
    assert exc.value.reason == "audit_inconsistent"


def test_boot_id_failures_are_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao import shared_brain_audit as audit

    monkeypatch.setattr(audit, "_BOOT_CACHE", [])
    real_open, real_run = open, subprocess.run

    def no_proc(path, *args, **kw):  # type: ignore[no-untyped-def]
        if str(path).startswith("/proc/"):
            raise FileNotFoundError(path)
        return real_open(path, *args, **kw)

    monkeypatch.setattr("builtins.open", no_proc)
    monkeypatch.setattr(
        audit.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("transient")),
    )
    assert audit._boot_id() == "unknown" and audit._BOOT_CACHE == []

    class Done:
        returncode = 0
        stdout = "{ sec = 1, usec = 2 }"

    monkeypatch.setattr(audit.subprocess, "run", lambda *a, **k: Done())
    assert audit._boot_id() == "{sec=1,usec=2}"
    assert audit._BOOT_CACHE == ["{sec=1,usec=2}"]
    del real_run


def test_owner_state_distinguishes_dead_self_and_unknown() -> None:
    from ciao.shared_brain_audit import current_owner, owner_state

    assert owner_state(current_owner()) == "self"
    assert owner_state(f"{os.getpid()}:some-other-boot") == "self"
    assert owner_state("not-a-pid:x") == "dead"
    assert owner_state(f"{os.getppid()}:some-other-boot") == "unknown"


@pytest.mark.parametrize(
    "variant",
    [
        "https://deploy:hunter2@Example.invalid/team/brain.git",
        "https://example.invalid/team/brain.git?token=hunter2",
        "https://deploy@example.invalid/team/brain.git#frag",
        "HTTPS://example.invalid/team/brain.git?access_token=hunter2&x=1",
    ],
)
def test_storage_digest_ignores_userinfo_and_query(variant: str) -> None:
    assert _storage_digest(variant) == _storage_digest("https://example.invalid/team/brain.git")
    assert _storage_digest("https://example.invalid/other.git") != _storage_digest(variant)


def test_scp_and_folder_locators_and_ports() -> None:
    assert _storage_digest("git@host.invalid:team/brain.git") == _storage_digest(
        "other@host.invalid:team/brain.git?token=x"
    )
    assert _storage_digest("https://h.invalid:8443/p") != _storage_digest("https://h.invalid/p")
    assert _storage_digest("https://h.invalid:bad/p")  # a malformed port still digests
    assert _storage_digest("/Users/a@b/x") != _storage_digest("/Users/c/x")


def test_racing_reconcilers_settle_an_orphan_exactly_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    log = DurableAuditLog(rig.audit_root)
    log.append(
        AuditKind.INTENT,
        intent="orphan",
        operation="bootstrap",
        brain=CONNECTOR.brain,
        storage=_storage_digest(CONNECTOR.storage),
        actor="alice",
        previous_steward=None,
        steward="bob",
        previous_generation=0,
        generation=1,
        at=1.0,
        owner="999999:" + "x",  # a different boot: dead
    )
    engines = [
        StewardAuthority(
            ProtectedAuthorityStore(rig.tmp / "authority"),
            backend=rig.backend,
            clock=rig.clock,
        )
        for _ in range(4)
    ]
    for engine in engines:
        engine._durable = DurableAuditLog(rig.audit_root)
    counts: list[int] = []
    barrier = threading.Barrier(len(engines))

    def go(engine: StewardAuthority) -> None:
        barrier.wait()
        counts.append(engine.reconcile_audit())

    threads = [threading.Thread(target=go, args=(e,)) for e in engines]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(counts) == 1
    assert len(_resolutions(rig, "orphan")) == 1


def test_second_resolution_for_one_intent_is_refused_on_write(tmp_path: Path) -> None:
    log = DurableAuditLog(tmp_path / "audit")
    _append(log, 1)
    _append(log, 1, AuditKind.COMMITTED)
    with pytest.raises(AuditError) as exc:
        _append(log, 1, AuditKind.ABORTED)
    assert exc.value.reason == "duplicate_resolution"
    with pytest.raises(AuditError) as exc2:
        _append(log, 1)  # an intent id is used once
    assert exc2.value.reason == "invalid_entry"
    with pytest.raises(AuditError) as exc3:
        _append(log, 7, AuditKind.COMMITTED)  # no such intent
    assert exc3.value.reason == "invalid_entry"
    with pytest.raises(AuditError) as exc4:
        _append(log, 2)
        log.append(AuditKind.COMMITTED, **_fields(2, steward="mallory"))  # type: ignore[arg-type]
    assert exc4.value.reason == "invalid_entry"


def test_a_forged_second_resolution_on_disk_is_corrupt(tmp_path: Path) -> None:
    from ciao.shared_brain_audit import _chain

    root = tmp_path / "audit"
    log = DurableAuditLog(root)
    _append(log, 1)
    _append(log, 1, AuditKind.COMMITTED)
    last = log.view().entries[-1]
    body = {
        "seq": 3,
        "kind": "aborted",
        "intent": "intent-1",
        "operation": "transfer",
        "brain": "b",
        "storage": "s",
        "actor": "bob",
        "previous_steward": "bob",
        "steward": "carol",
        "previous_generation": 1,
        "generation": 2,
        "at": 1.5,
        "recovered": False,
        "owner": last.owner,
    }
    forged = {"body": body, "hash": _chain(last.hash, body)}
    with open(root / "audit.log", "a") as handle:
        handle.write(json.dumps(forged, sort_keys=True) + "\n")
    state = json.loads((root / "audit.state.json").read_text())
    state["last_seq"], state["last_hash"] = 3, forged["hash"]
    (root / "audit.state.json").write_text(json.dumps(state))
    os.chmod(root / "audit.state.json", 0o600)
    with pytest.raises(AuditError) as exc:
        DurableAuditLog(root).view()
    assert exc.value.reason == "corrupt"


def test_field_length_and_line_size_are_bounded(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    log = DurableAuditLog(root)
    with pytest.raises(AuditError) as exc:
        log.append(AuditKind.INTENT, **_fields(actor="a" * (MAX_FIELD_BYTES + 1)))  # type: ignore[arg-type]
    assert exc.value.reason == "invalid_entry"
    with pytest.raises(AuditError):
        log.append(AuditKind.INTENT, **_fields(actor="é" * MAX_FIELD_BYTES))  # type: ignore[arg-type]
    log.append(AuditKind.INTENT, **_fields(actor="a" * MAX_FIELD_BYTES))  # type: ignore[arg-type]
    with open(root / "audit.log", "a") as handle:
        handle.write("x" * (MAX_LINE_BYTES + 1) + "\n")
    with pytest.raises(AuditError) as exc2:
        log.view()
    assert exc2.value.reason == "corrupt"


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_time_is_refused(tmp_path: Path, bad: float) -> None:
    log = DurableAuditLog(tmp_path / "audit")
    with pytest.raises(AuditError) as exc:
        log.append(AuditKind.INTENT, **_fields(at=bad))  # type: ignore[arg-type]
    assert exc.value.reason == "invalid_entry"
    assert log.view().entries == ()


def test_storage_is_audited_as_a_storage_digest_without_userinfo(tmp_path: Path) -> None:
    secret = "https://deploy:hunter2@example.invalid/team/brain.git?token=s3cr3t"
    connector = BrainRef("secret-brain", secret)
    rig = Rig(tmp_path)
    successor = rig.sign("bob-token")  # capability binding is per connector below
    backend = rig.backend
    challenge = rig.engine.begin_bootstrap(
        connector=connector,
        admin=backend.sign_in(connector, "alice-token"),
        successor="bob",
    )
    rig.engine.accept_bootstrap(
        challenge.nonce, connector=connector, successor=backend.sign_in(connector, "bob-token")
    )
    del successor
    raw = "".join(p.read_text() for p in rig.audit_root.glob("audit*"))
    assert not any(bit in raw for bit in ("hunter2", "deploy", "s3cr3t", "token"))
    assert _storage_digest(secret) in raw
    assert rig.entries()[0].storage == _storage_digest(secret)


def test_stale_state_temporaries_are_removed_on_load(tmp_path: Path) -> None:
    root = tmp_path / "audit"
    log = DurableAuditLog(root)
    _append(log, 1)
    stale = root / ".audit.crashed.tmp"
    stale.write_text("{}")
    os.chmod(stale, 0o600)
    assert [e.seq for e in DurableAuditLog(root).view().entries] == [1]
    assert not stale.exists()
