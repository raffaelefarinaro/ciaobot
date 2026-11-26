"""Failure, concurrency, recovery and undo tests for the memory receipt protocol.

These pin AI-05's acceptance criteria:

* an injected lock failure produces zero guide writes and preserves the fact;
* concurrent accepts / ``memory_update`` either both land or one reports a
  conflict — never a silent lost write;
* a crash between the receipt and the file update recovers to one consistent
  operation;
* undo refuses a changed destination, cannot remove unrelated later facts, and
  leaves unsupported legacy operations view-only;
* the journal, its lock and the trim temp are private files, the temp can only
  ever be created rather than opened, and the trim is bounded in bytes as well
  as in rows without dropping a pending receipt.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import nullcontext
from pathlib import Path

import pytest

from ciao import memory_receipts as mr
from ciao import file_kernel as fk
from ciao import memory_tool as mt
from ciao.os_support.private import is_private


def _guide(tmp_path: Path, memory: list[str] | None = None) -> Path:
    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# Guide\n\n", encoding="utf-8")
    mt.ensure_regions(guide)
    if memory:
        mt.write_region(guide, "memory", memory)
    return guide


def _entries(guide: Path, region: str = "memory") -> list[str]:
    from ciao.memory_audit import strip_learned_stamp

    entries, _ = mt.read_region(guide, region)
    return [strip_learned_stamp(e) for e in entries]


# ── Lock failure ──────────────────────────────────────────────────────────


class _HeldLock:
    """A lock another thread owns, so an acquisition attempt blocks."""

    def __enter__(self):
        self._fd = Path("/tmp")
        return self

    def __exit__(self, *exc):
        return False


def test_update_region_lock_failure_raises_and_leaves_the_region(tmp_path, monkeypatch):
    guide = _guide(tmp_path, memory=["existing fact"])
    before = guide.read_text(encoding="utf-8")

    def _denied(_guide, **_kwargs):
        raise mt.MemoryLockError("held")

    monkeypatch.setattr(mt, "guide_lock", _denied)
    with pytest.raises(mt.MemoryLockError):
        mt.update_region(guide, "memory", action="add", entry="new fact")
    assert guide.read_text(encoding="utf-8") == before


def test_guide_lock_is_reportable_not_unbounded(tmp_path):
    """A held lock times out into MemoryLockError rather than hanging."""
    from ciao.os_support.locks import lock_exclusive, unlock

    guide = _guide(tmp_path)
    lock_path = guide.with_name(f"{guide.name}.lock")
    holder = lock_path.open("a+", encoding="utf-8")
    lock_exclusive(holder.fileno())
    try:
        with pytest.raises(mt.MemoryLockError):
            mt.guide_lock(guide, timeout_s=0.2)
    finally:
        unlock(holder.fileno())
        holder.close()


# ── Concurrency ───────────────────────────────────────────────────────────


def test_competing_managed_writes_report_conflict_not_lost_write(tmp_path, monkeypatch):
    """A write that lands between our read and commit is a conflict.

    Simulated by taking the region snapshot, letting a second writer change the
    file, then committing with the stale expected revision: the commit must
    refuse and write nothing.
    """
    from ciao.memory_receipts import RevisionConflict, commit_region_change
    from ciao.memory_audit import strip_learned_stamp

    guide = _guide(tmp_path, memory=["Original."])
    entries, _ = mt.read_region(guide, "memory")
    expected = fk.content_revision(mt.serialize_entries(entries))
    # A concurrent managed write lands.
    mt.update_region(guide, "memory", action="add", entry="Concurrent fact.")

    with pytest.raises(RevisionConflict):
        commit_region_change(
            guide,
            "memory",
            entries=[*entries, "Stale append. [2026-01-01]"],
            actor="agent",
            source="archive",
            vault_root=tmp_path,
            expected_revision=expected,
            fact_text="Stale append.",
            kind="region_apply",
        )
    landed = {strip_learned_stamp(e) for e in _entries(guide)}
    assert "Concurrent fact." in landed
    assert "Stale append." not in landed


# ── Crash recovery ────────────────────────────────────────────────────────


def _journal(tmp_path: Path) -> Path:
    return mr.journal_path(tmp_path, None)


# ── Journal trimming ──────────────────────────────────────────────────────


def test_trim_keeps_a_pending_receipt_that_predates_the_cut(tmp_path, monkeypatch):
    """A prepared receipt before the retained tail must survive trimming.

    Once the journal exceeds its size cap, the newest rows are kept and the
    rest dropped — but an unresolved (non-terminal) receipt must stay
    recoverable. Computing pending ids from the retained lines alone missed a
    `prepared` row whose only line sat before the cut, so trimming deleted it.
    """
    monkeypatch.setattr(mr, "MAX_BYTES", 1)
    monkeypatch.setattr(mr, "KEEP_LINES", 3)
    journal = _journal(tmp_path)
    # Oldest line: an unresolved prepared receipt, followed by newer junk.
    mr._append(journal, {"id": "mrcpt_pending_old", "status": mr.PREPARED, "ts": "t0"})
    for i in range(6):
        mr._append(journal, {"id": f"mrcpt_done_{i}", "status": mr.APPLIED, "ts": f"t{i}"})

    rows = {r["id"] for r in mr.read_receipts(journal)}

    assert "mrcpt_pending_old" in rows, "an unresolved receipt must not be trimmed"
    assert mr.find_receipt(journal, "mrcpt_pending_old")["status"] == mr.PREPARED


def test_trim_drops_only_terminal_receipts(tmp_path, monkeypatch):
    """With every dropped id terminal, trimming proceeds."""
    # A cap the *retained* tail fits inside (two 64-byte rows), so this exercises
    # the row-count budget on its own; `test_trim_bounds_a_byte_heavy_journal`
    # is where the byte budget is pinned.
    monkeypatch.setattr(mr, "MAX_BYTES", 150)
    monkeypatch.setattr(mr, "KEEP_LINES", 2)
    journal = _journal(tmp_path)
    for i in range(5):
        mr._append(journal, {"id": f"mrcpt_done_{i}", "status": mr.APPLIED, "ts": f"t{i}"})

    rows = [r["id"] for r in mr.read_receipts(journal)]

    assert rows == ["mrcpt_done_3", "mrcpt_done_4"]


def test_trim_runs_while_holding_the_journal_lock(tmp_path, monkeypatch):
    """Trimming must run inside the append lock.

    Otherwise a concurrent append can land after the trim reads the file and
    before it replaces it, and the stale snapshot deletes that newer receipt.
    The probe: intercept `_trim_if_large` and confirm the journal lock is still
    held at that moment (a non-blocking exclusive flock must fail).
    """
    from ciao.os_support.locks import lock_exclusive, unlock

    journal = _journal(tmp_path)
    lock = journal.with_name(journal.name + ".lock")
    observed: list[bool] = []

    real_trim = mr._trim_if_large

    def probe(path: Path) -> None:
        with lock.open("a+", encoding="utf-8") as handle:
            try:
                lock_exclusive(handle.fileno(), blocking=False)
            except BlockingIOError:
                observed.append(True)
                return
            unlock(handle.fileno())
        observed.append(False)

    monkeypatch.setattr(mr, "_trim_if_large", probe)
    try:
        mr._append(journal, {"id": "mrcpt_probe", "status": mr.APPLIED, "ts": "t"})
    finally:
        monkeypatch.setattr(mr, "_trim_if_large", real_trim)

    assert observed == [True], "trim ran after the append lock was released"


def test_trim_cannot_delete_a_row_a_concurrent_append_just_wrote(tmp_path, monkeypatch):
    """A row appended while a trim is running must survive it.

    `_trim_if_large` reads the whole journal and then `os.replace`s it with
    that snapshot. Run outside the append flock, a second appender lands
    between those two steps: its row goes into the inode the replace is about
    to unlink and is gone without a trace. Lose a `prepared` row that way and
    `recover_pending` has no evidence the operation ever started, so its
    half-finished side effects can never be settled — the exact unrecoverable
    state this journal exists to rule out.

    The sibling test proves the lock is *held* during the trim; this one
    proves what that buys, by racing a real appender against it.
    """
    from ciao.os_support.locks import lock_exclusive, unlock
    import time

    monkeypatch.setattr(mr, "MAX_BYTES", 1)  # every append trims
    monkeypatch.setattr(mr, "KEEP_LINES", 50)  # ...but drops nothing
    journal = _journal(tmp_path)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps({"id": "mrcpt_seed", "status": mr.APPLIED, "ts": "t0", "v": 1}) + "\n",
        encoding="utf-8",
    )

    def concurrent_append() -> None:
        # A second appender, reduced to what matters: take the journal lock,
        # append one row. A full `_append` would also trim, and the two trims
        # would collide on the shared temp name — an accident that would mask
        # the race under test rather than exercise it.
        lock = journal.with_name(journal.name + ".lock")
        with lock.open("a+", encoding="utf-8") as handle:
            lock_exclusive(handle.fileno())
            try:
                with journal.open("a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            {"id": "mrcpt_concurrent", "status": mr.PREPARED, "ts": "t2", "v": 1}
                        )
                        + "\n"
                    )
            finally:
                unlock(handle.fileno())

    real_replace = os.replace
    started: list[threading.Thread] = []

    def replace_after_a_concurrent_append(src, dst):  # type: ignore[no-untyped-def]
        if not started:
            worker = threading.Thread(target=concurrent_append)
            started.append(worker)
            worker.start()
            # Long enough for the other appender to land when nothing
            # serializes it; simply waited out when the lock makes it queue.
            time.sleep(0.25)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace_after_a_concurrent_append)
    mr._append(journal, {"id": "mrcpt_trimmer", "status": mr.APPLIED, "ts": "t1"})
    assert started, "the trim never reached its os.replace"
    started[0].join(timeout=5)
    monkeypatch.undo()

    ids = {r["id"] for r in mr.read_receipts(journal)}
    assert "mrcpt_concurrent" in ids, "the trim deleted a receipt appended while it ran"
    assert "mrcpt_trimmer" in ids


@pytest.mark.skipif(
    not hasattr(os, "umask"), reason="no POSIX permissions on this platform"
)
def test_a_new_journal_and_its_lock_are_private(tmp_path):
    """A receipt images whole note bodies, so the journal is not shared.

    A note may be 0600 while the journal holding its before *and* after image
    was created 0666 minus the umask, and the journal lives inside the vault
    beside the queue — so on a multi-user machine every local account could read
    what the note's own permissions forbid. The journal, its lock and the trim
    temp are created 0600; a file that already exists keeps the mode its owner
    chose, because a background append is not a reason to re-chmod anything.
    """
    journal = _journal(tmp_path)
    # A permissive umask is the case that mattered: under the usual 022 the
    # `Path.open("a")` this replaced produced 0644, and the 0666 it produces
    # here is what a user with umask 000 got.
    previous = os.umask(0o000)
    try:
        mr._append(journal, {"id": "mrcpt_private", "status": mr.APPLIED, "ts": "t"})
    finally:
        os.umask(previous)

    assert is_private(journal)
    lock = journal.with_name(journal.name + ".lock")
    assert is_private(lock)

    # A journal the owner deliberately shared keeps the permissions they set,
    # and an append does not quietly narrow it.
    shared = journal.with_name("Shared-Receipts.jsonl")
    shared.write_text("", encoding="utf-8")
    os.chmod(shared, 0o644)
    assert not is_private(shared)
    mr._append(shared, {"id": "mrcpt_shared", "status": mr.APPLIED, "ts": "t2"})
    assert not is_private(shared)
    assert [row["id"] for row in mr.read_receipts(shared)] == ["mrcpt_shared"]


def test_trim_bounds_a_byte_heavy_journal_and_spares_a_pending_row(
    tmp_path, monkeypatch
):
    """The cap is bytes, not rows, and a pending row outranks the cap.

    `KEEP_LINES` is a budget of rows, but a receipt images a whole note, so a
    journal of a few large rows stayed far above `MAX_BYTES` — the size the trim
    exists to bound — and every later append re-ran the same read-and-rewrite
    over the same over-long file. The trim now also drops whole rows from the
    front until the remainder serializes within the cap, and an unresolved
    receipt is still worth more than the cap: the byte cut stops at it rather
    than dropping it, which is why the file can legitimately stay over.
    """
    cap = 2000
    monkeypatch.setattr(mr, "MAX_BYTES", cap)
    monkeypatch.setattr(mr, "KEEP_LINES", 50)  # the row budget never fires here
    journal = _journal(tmp_path)
    blob = "x" * 1500

    # An unresolved receipt first, so the byte cut walks into it.
    mr._append(
        journal,
        {"id": "mrcpt_pending_huge", "status": mr.PREPARED, "ts": "t0", "image": blob},
    )
    for i in range(2):
        mr._append(
            journal,
            {
                "id": f"mrcpt_done_{i}",
                "status": mr.APPLIED,
                "ts": f"t{i}",
                "image": blob,
            },
        )

    # Over the cap, and the trim cannot fix it without losing a `prepared` row.
    assert journal.stat().st_size > cap
    assert mr.find_receipt(journal, "mrcpt_pending_huge")["status"] == mr.PREPARED
    assert {row["id"] for row in mr.read_receipts(journal)} == {
        "mrcpt_pending_huge",
        "mrcpt_done_0",
        "mrcpt_done_1",
    }

    # It settles, and the next appends are what the byte cut is for.
    mr._append(journal, {"id": "mrcpt_pending_huge", "status": mr.APPLIED, "ts": "t3"})
    temp_private: list[bool] = []
    real_replace = os.replace

    def probe(src, dst):  # type: ignore[no-untyped-def]
        if str(dst) == str(journal):
            temp_private.append(is_private(src))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", probe)
    mr._append(
        journal,
        {"id": "mrcpt_done_2", "status": mr.APPLIED, "ts": "t4", "image": blob},
    )
    monkeypatch.setattr(os, "replace", real_replace)

    assert journal.stat().st_size <= cap
    rows = mr.read_receipts(journal)
    assert {row["id"] for row in rows} == {"mrcpt_pending_huge", "mrcpt_done_2"}
    assert all(row["status"] == mr.APPLIED for row in rows)
    # Whole rows only: the retained journal is still parseable line for line.
    assert journal.read_text(encoding="utf-8").count("\n") == len(rows)
    # The trim temp holds the same note bodies and is no wider than the journal
    # it replaces, and leaves nothing behind.
    assert temp_private and set(temp_private) == {True}
    assert is_private(journal)
    assert not list(journal.parent.glob("*.trim.tmp"))


def test_trim_temp_is_created_not_reused(tmp_path, monkeypatch):
    """The trim temp can only ever be a file this trim created.

    The whole retained journal — note bodies included — is written to the temp
    before the `os.replace`, so an open that follows whatever sits at that name
    hands the contents of an arbitrary file to anyone who can read it, and one
    that truncates it destroys a file the vault did not lose. The stale temp is
    therefore unlinked explicitly first, and the open that follows is
    `O_EXCL` and refuses links (`follow_symlinks=False`): it can only create,
    never open what is already there, and never write through a link planted
    in the window between the two.
    """
    monkeypatch.setattr(mr, "MAX_BYTES", 1)  # every append trims
    monkeypatch.setattr(mr, "KEEP_LINES", 50)  # ...but drops nothing
    journal = _journal(tmp_path)
    for i in range(3):
        mr._append(
            journal, {"id": f"mrcpt_flag_{i}", "status": mr.APPLIED, "ts": f"t{i}"}
        )

    opened: list[tuple[Path, int, bool]] = []
    real_open_private = mr._open_private

    def record(
        path: Path, *, flags: int, mode: int = 0o600, follow_symlinks: bool = True
    ) -> int:
        opened.append((path, flags, follow_symlinks))
        return real_open_private(
            path, flags=flags, mode=mode, follow_symlinks=follow_symlinks
        )

    monkeypatch.setattr(mr, "_open_private", record)
    mr._append(journal, {"id": "mrcpt_flag_3", "status": mr.APPLIED, "ts": "t3"})

    temps = [entry for entry in opened if entry[0].name.endswith(".trim.tmp")]
    assert temps, "the trim never opened its temp"
    for _path, flags, follow_symlinks in temps:
        assert flags & os.O_EXCL, "the temp open must fail on an existing name"
        assert not follow_symlinks, "the temp open must refuse a symlink"
        assert not flags & os.O_TRUNC, "an existing file is unlinked, not truncated"
    # The trim still did its job through those flags.
    assert {r["id"] for r in mr.read_receipts(journal)} == {"mrcpt_flag_3"}


def test_recovery_applied_when_crash_landed_after_the_write(tmp_path):
    """A prepared receipt whose after-image is on disk settles to applied."""
    guide = _guide(tmp_path, memory=["fact one"])
    before = mt.serialize_entries(mt.read_region(guide, "memory")[0])
    after = mt.serialize_entries(mt.read_region(guide, "memory")[0] + ["fact two"])
    receipt = {
        "id": "mrcpt_crash_after",
        "ts": mr._now(),
        "actor": "operator",
        "source": "pwa",
        "workspace": "",
        "kind": "region_apply",
        "guide": str(guide),
        "region": "memory",
        "before_revision": fk.content_revision(before),
        "after_revision": fk.content_revision(after),
        "before_text": before,
        "after_text": after,
        "fact_text": "fact two",
        "status": mr.PREPARED,
    }
    mr._append(_journal(tmp_path), receipt)
    # The crash landed after the atomic replace: disk already equals `after`.
    mt.update_region(guide, "memory", action="add", entry="fact two")

    result = mr.recover_pending(journal=_journal(tmp_path))
    assert [r["id"] for r in result.reconciled] == ["mrcpt_crash_after"]
    settled = mr.find_receipt(_journal(tmp_path), "mrcpt_crash_after")
    assert settled is not None and settled["status"] == mr.APPLIED


def test_recovery_rolled_back_when_write_never_landed(tmp_path):
    """A prepared receipt whose before-image is on disk returns to rolled_back."""
    guide = _guide(tmp_path, memory=["fact one"])
    before = mt.serialize_entries(mt.read_region(guide, "memory")[0])
    after = mt.serialize_entries(mt.read_region(guide, "memory")[0] + ["fact two"])
    receipt = {
        "id": "mrcpt_crash_before",
        "ts": mr._now(),
        "actor": "operator",
        "source": "pwa",
        "kind": "region_apply",
        "guide": str(guide),
        "region": "memory",
        "before_revision": fk.content_revision(before),
        "after_revision": fk.content_revision(after),
        "before_text": before,
        "after_text": after,
        "status": mr.PREPARED,
    }
    mr._append(_journal(tmp_path), receipt)
    result = mr.recover_pending(journal=_journal(tmp_path))
    assert [r["id"] for r in result.reconciled] == ["mrcpt_crash_before"]
    settled = mr.find_receipt(_journal(tmp_path), "mrcpt_crash_before")
    assert settled is not None and settled["status"] == mr.ROLLED_BACK


def test_recovery_conflicts_when_neither_image_matches(tmp_path):
    """An external direct edit is a conflict, never guessed at."""
    guide = _guide(tmp_path, memory=["fact one"])
    before = mt.serialize_entries(mt.read_region(guide, "memory")[0])
    after = mt.serialize_entries(mt.read_region(guide, "memory")[0] + ["fact two"])
    receipt = {
        "id": "mrcpt_crash_conflict",
        "ts": mr._now(),
        "kind": "region_apply",
        "guide": str(guide),
        "region": "memory",
        "before_revision": fk.content_revision(before),
        "after_revision": fk.content_revision(after),
        "before_text": before,
        "after_text": after,
        "status": mr.PREPARED,
    }
    mr._append(_journal(tmp_path), receipt)
    # An external editor rewrote the region.
    text = guide.read_text(encoding="utf-8")
    guide.write_text(text.replace("fact one", "externally edited"), encoding="utf-8")
    result = mr.recover_pending(journal=_journal(tmp_path))
    assert [r["id"] for r in result.conflicts] == ["mrcpt_crash_conflict"]
    settled = mr.find_receipt(_journal(tmp_path), "mrcpt_crash_conflict")
    assert settled is not None and settled["status"] == mr.CONFLICT
    # The external edit is untouched.
    assert "externally edited" in guide.read_text(encoding="utf-8")


def test_crash_between_guide_write_and_outcome_recovers_once(tmp_path):
    """One consistent operation: the applied receipt completes the decision once."""
    guide = _guide(tmp_path)
    vault = tmp_path
    before = mt.serialize_entries(mt.read_region(guide, "memory")[0])
    after = mt.serialize_entries(["Recovered fact. [2026-01-01]"])
    receipt = {
        "id": "mrcpt_outcome_gap",
        "ts": mr._now(),
        "actor": "auto",
        "source": "archive",
        "workspace": "personal",
        "kind": "region_apply",
        "guide": str(guide),
        "region": "memory",
        "vault_root": str(vault),
        "before_revision": fk.content_revision(before),
        "after_revision": fk.content_revision(after),
        "before_text": before,
        "after_text": after,
        "fact_text": "Recovered fact.",
        "destination": "ciao:memory",
        "status": mr.PREPARED,
    }
    j = mr.journal_path(vault, None)
    mr._append(j, receipt)
    # The guide write landed; the decision record did not.
    mt.update_region(guide, "memory", action="add", entry="Recovered fact. [2026-01-01]")

    first = mr.recover_pending(journal=j)
    assert first.reconciled and first.reconciled[0]["status"] == mr.APPLIED

    # Idempotent: running recovery again does nothing new.
    second = mr.recover_pending(journal=j)
    assert second.reconciled == []


def test_a_failed_region_write_settles_the_row_it_opened(tmp_path, monkeypatch):
    """One failed write must leave one receipt, not a failure plus an orphan.

    The failure row was keyed off a basis of its own — no ``before_revision``,
    and no journal for the generation step — so it landed on a different id
    than the ``prepared`` row the very same attempt had already appended.
    `read_receipts` folds by id, so a single failed write showed up twice in
    History, and the orphaned ``prepared`` row stayed non-terminal: startup
    recovery then reconciled an operation that had already reported ``failed``
    and stamped it ``rolled_back`` — a second, contradictory verdict on it.
    """
    guide = _guide(tmp_path, memory=["fact one"])
    journal = _journal(tmp_path)

    def boom(*_args, **_kwargs):
        raise OSError("the disk went away mid-write")

    monkeypatch.setattr(mt, "write_guide_atomically", boom)
    with pytest.raises(OSError):
        mr.commit_region_change(
            guide,
            "memory",
            entries=["fact one", "fact two"],
            actor="operator",
            source="pwa",
            vault_root=tmp_path,
            fact_text="fact two",
        )
    monkeypatch.undo()

    rows = mr.read_receipts(journal)
    assert [r["status"] for r in rows] == [mr.FAILED], (
        "the failure row must settle the prepared row, not open a second receipt"
    )
    # Nothing left for startup recovery to re-judge.
    assert mr.recover_pending(journal=journal).reconciled == []


# ── Undo ──────────────────────────────────────────────────────────────────


def test_unsupported_legacy_receipts_stay_view_only(tmp_path):
    """A row with no before/after image is not undoable."""
    journal = _journal(tmp_path)
    mr._append(
        journal,
        {
            "id": "mrcpt_legacy",
            "ts": mr._now(),
            "kind": "region_apply",
            "status": mr.APPLIED,
        },
    )
    receipt = mr.find_receipt(journal, "mrcpt_legacy")
    assert receipt is not None
    assert mr.is_undoable(receipt) is False
    from ciao.memory_receipts import UndoUnsupported

    with pytest.raises(UndoUnsupported):
        mr.undo_receipt("mrcpt_legacy", vault_root=tmp_path)


# ── Queue receipts ────────────────────────────────────────────────────────


# ── Category apply receipt (issue #647) ─────────────────────────────────────


def test_a_category_undo_removes_a_registry_file_it_created(tmp_path):
    """A vault with no `entity-types.yaml` before the accept must not be left
    with an override file the owner never wrote."""
    from ciao import entity_types
    from ciao.entity_types import EntityType

    registry = tmp_path / "entity-types.yaml"
    receipt = mr.record_category_apply(
        mr.journal_path(tmp_path, None),
        registry_path=registry,
        registry_existed=False,
        before_text="",
        after_text=entity_types.render_yaml([EntityType(id="recipe", label="Recipe")]),
        notes=[],
        actor="operator",
        source="pwa",
        vault_root=tmp_path,
    )
    registry.write_text(receipt["after_text"], encoding="utf-8")

    mr.undo_receipt(receipt["id"], vault_root=tmp_path)

    assert not registry.exists()


# ── Queue lock ────────────────────────────────────────────────────────────


def test_queue_lock_is_reentrant_within_a_thread(tmp_path):
    """A wrapper can hold the lock across a body that takes it again."""
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text("# Memory Proposals\n\n", encoding="utf-8")

    with fk.queue_lock(queue):
        with fk.queue_lock(queue):
            inner = True
    assert inner


def test_queue_lock_refuses_to_write_when_held(tmp_path):
    """A held lock is reportable, not silently ignored."""
    from ciao.os_support.locks import lock_exclusive, unlock
    import threading
    import time

    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text("# Memory Proposals\n\n", encoding="utf-8")
    lock_path = fk.lock_path_for(str(queue.resolve()))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    lock_exclusive(handle.fileno())
    acquired = threading.Event()

    def contender() -> None:
        try:
            with fk.queue_lock(queue, timeout_s=0.2):
                acquired.set()
        except fk.QueueLockError:
            pass

    t = threading.Thread(target=contender)
    t.start()
    t.join(timeout=10)
    unlock(handle.fileno())
    handle.close()
    assert not acquired.is_set()
    time.sleep(0)


def test_queue_lock_file_lives_outside_the_vault(tmp_path):
    """The lock must not pollute the user-owned vault tree."""
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text("# Memory Proposals\n\n", encoding="utf-8")

    with fk.queue_lock(queue):
        pass

    assert not (queue.parent / f"{queue.name}.lock").exists()
    assert fk.lock_path_for(str(queue.resolve())).exists()


def test_write_queue_atomically_keeps_the_old_file_on_failure(tmp_path, monkeypatch):
    """A failed write must not truncate or partially rewrite the queue.

    In-place `write_text` truncates before writing, so a partial write can lose
    unrelated proposals. The helper writes a temp file and renames it, so a
    failure leaves the original intact.
    """
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    original = "# Memory Proposals\n\n- [memory] Keep this.  _(from: Decisions)_\n"
    queue.write_text(original, encoding="utf-8")

    real = Path.write_text

    def boom(self, *args, **kwargs):
        if self.name.endswith(".write.tmp"):
            raise OSError("disk full")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError):
        fk.write_queue_atomically(queue, "# Memory Proposals\n\n")

    assert queue.read_text(encoding="utf-8") == original


# ── Batch queue receipts ──────────────────────────────────────────────────


# ── API routes ────────────────────────────────────────────────────────────


def _receipts_client(tmp_path: Path):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.web.routes_api import memory_receipt_undo, memory_receipts

    vault = tmp_path / "memory-vault"
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=vault,
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal")
        },
    )
    app = Starlette(
        routes=[
            Route("/api/memory/receipts", memory_receipts, methods=["GET"]),
            Route("/api/memory/receipts/{id}/undo", memory_receipt_undo, methods=["POST"]),
        ]
    )
    app.state.config = config
    return TestClient(app), config


def test_receipt_undo_route_rejects_unknown_id(tmp_path):
    client, _config = _receipts_client(tmp_path)
    resp = client.post("/api/memory/receipts/mrcpt_missing/undo")
    assert resp.status_code == 404


def test_recover_memory_journals_covers_every_workspace_vault(tmp_path):
    _client, config = _receipts_client(tmp_path)
    guide = Path(config.agent_root("personal")) / "CLAUDE.md"
    guide.parent.mkdir(parents=True, exist_ok=True)
    vault = config.workspace_vault_root("personal")
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    mt.ensure_regions(guide)
    before = mt.serialize_entries(mt.read_region(guide, "memory")[0])
    after = mt.serialize_entries(["Crash fact."])
    journal = mr.journal_path(vault, None)
    mr._append(
        journal,
        {
            "id": "mrcpt_startup",
            "ts": mr._now(),
            "kind": "region_apply",
            "guide": str(guide),
            "region": "memory",
            "before_revision": fk.content_revision(before),
            "after_revision": fk.content_revision(after),
            "before_text": before,
            "after_text": after,
            "status": mr.PREPARED,
        },
    )
    mt.update_region(guide, "memory", action="add", entry="Crash fact.")
    result = mr.recover_memory_journals(config)
    assert [r["id"] for r in result["reconciled"]] == ["mrcpt_startup"]


def test_recovery_scans_the_guide_local_fallback_journal(tmp_path):
    """A receipt written beside a guide (no vault seam) is still discovered.

    An automatic prune whose caller passes only the guide records under the
    guide's sibling ``Workspace/``. Startup recovery must scan that fallback so
    the interrupted mutation is reconciled, not silently orphaned.
    """
    _client, config = _receipts_client(tmp_path)
    guide = Path(config.agent_root("personal")) / "CLAUDE.md"
    guide.parent.mkdir(parents=True, exist_ok=True)
    mt.ensure_regions(guide)
    before = mt.serialize_entries(mt.read_region(guide, "memory")[0])
    after = mt.serialize_entries(["Fallback fact."])
    journal = mr.journal_path(None, guide)
    mr._append(
        journal,
        {
            "id": "mrcpt_fallback",
            "ts": mr._now(),
            "kind": "region_apply",
            "guide": str(guide),
            "region": "memory",
            "before_revision": fk.content_revision(before),
            "after_revision": fk.content_revision(after),
            "before_text": before,
            "after_text": after,
            "status": mr.PREPARED,
        },
    )
    mt.update_region(guide, "memory", action="add", entry="Fallback fact.")

    result = mr.recover_memory_journals(config)
    assert [r["id"] for r in result["reconciled"]] == ["mrcpt_fallback"]


def test_mcp_memory_update_lock_failure_is_retryable_not_success(tmp_path, monkeypatch):
    """The MCP path surfaces an unavailable lock as retryable, with no write.

    ``ControlPlane.memory_update`` translates ``MemoryLockError`` into a
    ``memory_update_locked`` retryable error, so a busy guide is never reported
    as a successful edit.
    """
    from ciao.control_plane import CiaoControlPlane, ControlPlaneError, McpPrincipal

    guide = _guide(tmp_path)
    before = guide.read_text(encoding="utf-8")

    class _Config:
        workspace_root = tmp_path
        memory_char_limit = 3000
        user_char_limit = 1375

        def workspace(self, _name: str) -> object:
            return object()

        def agent_root(self, _name: str) -> Path:
            return tmp_path

        def workspace_vault_root(self, _name: str) -> Path:
            return tmp_path

    cp = CiaoControlPlane(_Config(), project_chat_manager=None, schedule_manager=None)  # type: ignore[arg-type]

    def _denied(_guide, **_kwargs):
        raise mt.MemoryLockError("held")

    monkeypatch.setattr(mt, "guide_lock", _denied)
    principal = McpPrincipal(
        token_id="t", chat_id="c", project_id="p", workspace="personal", provider="claude"
    )
    with pytest.raises(ControlPlaneError) as excinfo:
        cp.memory_update(principal, "memory", action="add", entry="An MCP fact.")
    assert excinfo.value.code == "memory_update_locked"
    assert excinfo.value.retryable is True
    assert guide.read_text(encoding="utf-8") == before




# ── receipt id generations (PR #477 review) ──────────────────────────────────


def _queue_with_one_row(tmp_path: Path) -> Path:
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n- [memory] Use Python  _(from: Alpha)_\n",
        encoding="utf-8",
    )
    return queue


def test_new_receipt_id_is_unchanged_without_a_journal(tmp_path):
    """The bare form stays content-addressed for callers that want folding."""
    first = mr.new_receipt_id("a|b|c")
    assert first == mr.new_receipt_id("a|b|c")
    assert first != mr.new_receipt_id("a|b|d")


def test_journal_writable_reports_an_unusable_journal(tmp_path):
    """The accept path's pre-flight: fail before mutating, not after."""
    good = tmp_path / "Workspace" / "Memory-Receipts.jsonl"
    assert mr.journal_writable(good) is True

    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    assert mr.journal_writable(blocked / "Workspace" / "R.jsonl") is False


def test_journal_writable_probes_without_creating(tmp_path):
    """The pre-flight must not materialise the journal it probes.

    An accept that is later rejected (400/409) used to leave a stray empty
    journal behind, because the probe created the parents and the file
    itself before validation ran.
    """
    journal = tmp_path / "Workspace" / "Memory-Receipts.jsonl"
    assert mr.journal_writable(journal) is True
    assert not journal.exists()
    assert not (tmp_path / "Workspace").exists()


def test_write_queue_atomically_stores_the_text_exactly(tmp_path):
    """The text is what goes on disk: a CRLF document stays CRLF and LF stays LF
    on every OS (a Windows text-mode write used to turn `\r\n` into `\r\r\n`)."""
    for name, text in [("crlf.md", "# L\r\n\r\n- [a] x\r\n"), ("lf.md", "# L\n\n- [a] x\n")]:
        path = tmp_path / name
        fk.write_queue_atomically(path, text)
        assert path.read_bytes() == text.encode("utf-8")


def test_the_guide_writer_stores_the_text_exactly(tmp_path):
    for name, text in [("crlf.md", "# Guide\r\nline\r\n"), ("lf.md", "# Guide\nline\n")]:
        path = tmp_path / name
        path.write_bytes(b"old\n")
        mt.write_guide_atomically(path, text)
        assert path.read_bytes() == text.encode("utf-8")


# ── Caller-bound region writes: expected revision, authority, operation id ──


def _grant(allowed: bool = True):
    """An ``authorize`` factory whose context answers ``allowed`` on entry."""
    return lambda: nullcontext(allowed)


def _consolidations(vault: Path) -> str:
    path = vault / "Workspace" / "Memory-Consolidations.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


_UPDATE = {"action": "update", "index": 1, "text": "Merged fact.", "old": "Existing fact."}


class _Crash(BaseException):
    """A process death: not an Exception, so no handler records a row for it."""


def _interrupted_region_receipt(tmp_path, guide, **extra):
    """A prepared region receipt whose guide write landed before the crash."""
    entries = mt.read_region(guide, "memory")[0]
    before = mt.serialize_entries(entries)
    after = mt.serialize_entries(entries + ["Recovered fact. [2026-01-01]"])
    return {
        "id": "mrcpt_interrupted",
        "ts": mr._now(),
        "actor": "agent",
        "source": "cli",
        "workspace": "personal",
        "kind": "region_apply",
        "guide": str(guide),
        "region": "memory",
        "vault_root": str(tmp_path),
        "before_revision": fk.content_revision(before),
        "after_revision": fk.content_revision(after),
        "before_text": before,
        "after_text": after,
        "fact_text": "Recovered fact.",
        "destination": "ciao:memory",
        "status": mr.PREPARED,
        **extra,
    }


def test_a_failed_guide_write_keeps_its_operation_id(tmp_path, monkeypatch):
    """The failure row settles the prepared receipt, so it must name the same operation."""
    import ciao.memory_tool as tool

    guide = _guide(tmp_path)

    def disk_full(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tool, "write_guide_atomically", disk_full)
    with pytest.raises(OSError):
        mr.commit_region_change(
            guide,
            "memory",
            entries=["Fact one."],
            actor="agent",
            source="care",
            workspace="personal",
            vault_root=tmp_path,
            fact_text="Fact one.",
            operation_id="op-x",
        )

    (row,) = mr.read_receipts(mr.journal_path(tmp_path, None))
    assert row["status"] == mr.FAILED
    assert row["operation_id"] == "op-x"
    assert row["workspace"] == "personal"


# ── S2: a managed write stamps the run and the item it settles ──


def test_a_crashed_stamped_region_write_keeps_run_and_item_through_recovery(tmp_path, monkeypatch):
    guide = _guide(tmp_path, memory=["fact one"])
    journal = mr.journal_path(tmp_path, None)
    real_append = mr._append

    def crash_before_applied(j, payload):
        if payload.get("status") == mr.APPLIED:
            raise _Crash()
        real_append(j, payload)

    monkeypatch.setattr(mr, "_append", crash_before_applied)
    with pytest.raises(_Crash):
        mr.commit_region_change(
            guide,
            "memory",
            entries=["fact one", "Stamped fact. [2026-01-01]"],
            actor="agent",
            source="cli",
            vault_root=tmp_path,
            fact_text="Stamped fact.",
            destination="ciao:memory",
            operation_id="op-stamped",
            run_id="run-1",
            item_key="proposals:stamped",
        )
    monkeypatch.undo()
    (prepared,) = mr.find_receipts_by_operation_id(tmp_path, "op-stamped")
    assert (prepared["run_id"], prepared["item_key"]) == ("run-1", "proposals:stamped")

    result = mr.recover_pending(journal=journal)

    (settled,) = [r for r in result.reconciled if r["operation_id"] == "op-stamped"]
    assert settled["status"] == mr.APPLIED
    assert (settled["run_id"], settled["item_key"]) == ("run-1", "proposals:stamped")


def test_a_failed_stamped_region_write_records_its_run_and_item(tmp_path, monkeypatch):
    guide = _guide(tmp_path, memory=["fact one"])

    def disk_full(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mt, "write_guide_atomically", disk_full)
    with pytest.raises(OSError):
        mr.commit_region_change(
            guide,
            "memory",
            entries=["fact one", "Failed fact. [2026-01-01]"],
            actor="agent",
            source="cli",
            vault_root=tmp_path,
            fact_text="Failed fact.",
            destination="ciao:memory",
            operation_id="op-failed",
            run_id="run-2",
            item_key="proposals:failed",
        )

    (effective,) = mr.find_receipts_by_operation_id(tmp_path, "op-failed")
    assert effective["status"] == mr.FAILED
    assert (effective["run_id"], effective["item_key"]) == ("run-2", "proposals:failed")


def test_an_unstamped_region_write_carries_no_run_or_item_keys(tmp_path):
    guide = _guide(tmp_path, memory=["fact one"])

    receipt = mr.commit_region_change(
        guide,
        "memory",
        entries=["fact one", "Plain fact. [2026-01-01]"],
        actor="agent",
        source="cli",
        vault_root=tmp_path,
        fact_text="Plain fact.",
        destination="ciao:memory",
    )

    assert "run_id" not in receipt and "item_key" not in receipt
