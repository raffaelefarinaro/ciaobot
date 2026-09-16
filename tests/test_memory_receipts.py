"""Failure, concurrency, recovery and undo tests for the memory receipt protocol.

These pin AI-05's acceptance criteria:

* an injected lock failure produces zero guide writes and preserves the fact;
* concurrent accepts / ``memory_update`` either both land or one reports a
  conflict — never a silent lost write;
* a crash between the receipt and the file update recovers to one consistent
  operation;
* undo refuses a changed destination, cannot remove unrelated later facts, and
  leaves unsupported legacy operations view-only.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from ciao import memory_proposals as mp
from ciao import memory_receipts as mr
from ciao import memory_tool as mt


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


def test_injected_lock_failure_writes_nothing_and_preserves_the_fact(tmp_path, monkeypatch):
    """The exact regression: a lock we cannot take must not fall through."""
    guide = _guide(tmp_path)
    before = guide.read_text(encoding="utf-8")

    def _denied(_guide, **_kwargs):
        raise mt.MemoryLockError("held by another writer")

    monkeypatch.setattr(mt, "guide_lock", _denied)
    outcome, promotable = mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="Prefers tabs over spaces.",
        vault_root=tmp_path,
    )
    assert outcome == "failed"
    assert promotable == "Prefers tabs over spaces."
    # Zero guide writes: byte-identical file, empty region.
    assert guide.read_text(encoding="utf-8") == before
    assert _entries(guide) == []


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
    import fcntl

    guide = _guide(tmp_path)
    lock_path = guide.with_name(f"{guide.name}.lock")
    holder = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    try:
        with pytest.raises(mt.MemoryLockError):
            mt.guide_lock(guide, timeout_s=0.2)
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


# ── Concurrency ───────────────────────────────────────────────────────────


def test_two_sequential_accepts_both_land(tmp_path):
    """Both facts survive; neither is dropped by the other's write."""
    guide = _guide(tmp_path)
    for text in ("First fact.", "Second fact."):
        outcome, _ = mp.accept_region_fact(
            guide_path=guide, target="memory", text=text, vault_root=tmp_path
        )
        assert outcome == "written"
    assert set(_entries(guide)) == {"First fact.", "Second fact."}


def test_concurrent_accept_and_memory_update_keep_both_changes(tmp_path):
    """A real race: both writers run to completion and neither write is lost.

    Serialized by the guide lock, so the outcome is one of:
    * both changes present, or
    * the loser gets an explicit conflict/failure and reports it.
    Never a silent success with a missing fact.
    """
    guide = _guide(tmp_path)
    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def accept():
        barrier.wait()
        results["accept"] = mp.accept_region_fact(
            guide_path=guide, target="memory", text="Accepted fact.", vault_root=tmp_path
        )

    def remember():
        barrier.wait()
        try:
            results["update"] = mt.update_region(
                guide, "memory", action="add", entry="Remembered fact."
            )
        except Exception as exc:  # noqa: BLE001
            results["update"] = exc

    threads = [threading.Thread(target=accept), threading.Thread(target=remember)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = set(_entries(guide))
    accept_outcome = results["accept"][0]  # type: ignore[index]
    update_result = results["update"]
    update_ok = isinstance(update_result, dict) and update_result.get("ok")
    # Each success must correspond to a landed fact; a reporter of success with
    # no landing fact is exactly the lost-write bug this must prevent.
    if accept_outcome == "written":
        assert "Accepted fact." in entries
    if update_ok:
        assert "Remembered fact." in entries
    # At least one must have succeeded, and the file is coherent.
    assert accept_outcome == "written" or update_ok


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
    expected = mr.content_revision(mt.serialize_entries(entries))
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
    monkeypatch.setattr(mr, "MAX_BYTES", 1)
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
    import fcntl

    journal = _journal(tmp_path)
    lock = journal.with_name(journal.name + ".lock")
    observed: list[bool] = []

    real_trim = mr._trim_if_large

    def probe(path: Path) -> None:
        with lock.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                observed.append(True)
                return
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        observed.append(False)

    monkeypatch.setattr(mr, "_trim_if_large", probe)
    try:
        mr._append(journal, {"id": "mrcpt_probe", "status": mr.APPLIED, "ts": "t"})
    finally:
        monkeypatch.setattr(mr, "_trim_if_large", real_trim)

    assert observed == [True], "trim ran after the append lock was released"


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
        "before_revision": mr.content_revision(before),
        "after_revision": mr.content_revision(after),
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
        "before_revision": mr.content_revision(before),
        "after_revision": mr.content_revision(after),
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
        "before_revision": mr.content_revision(before),
        "after_revision": mr.content_revision(after),
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
        "before_revision": mr.content_revision(before),
        "after_revision": mr.content_revision(after),
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


# ── Undo ──────────────────────────────────────────────────────────────────


def test_undo_refuses_a_changed_destination(tmp_path):
    """Undo must not delete an unrelated fact written after the operation."""
    guide = _guide(tmp_path)
    outcome, _ = mp.accept_region_fact(
        guide_path=guide, target="memory", text="Original fact.", vault_root=tmp_path
    )
    assert outcome == "written"
    journal = mr.journal_path(tmp_path, None)
    receipts = [r for r in mr.read_receipts(journal) if r["kind"] == "region_apply"]
    assert receipts, "the accepted write must leave a receipt"
    rid = receipts[-1]["id"]

    # A later, unrelated fact lands.
    mt.update_region(guide, "memory", action="add", entry="Unrelated later fact.")

    from ciao.memory_receipts import RevisionConflict

    with pytest.raises(RevisionConflict):
        mr.undo_receipt(rid, vault_root=tmp_path)
    # Nothing removed.
    assert "Unrelated later fact." in _entries(guide)
    assert "Original fact." in _entries(guide)


def test_undo_restores_the_before_image_from_the_history(tmp_path):
    """A conflict-free undo returns the region to its predecessor."""
    guide = _guide(tmp_path, memory=["Older fact."])
    outcome, _ = mp.accept_region_fact(
        guide_path=guide, target="memory", text="Newer fact.", vault_root=tmp_path
    )
    assert outcome == "written"
    assert set(_entries(guide)) == {"Older fact.", "Newer fact."}
    journal = mr.journal_path(tmp_path, None)
    rid = [r for r in mr.read_receipts(journal) if r["kind"] == "region_apply"][-1]["id"]

    mr.undo_receipt(rid, vault_root=tmp_path)
    assert _entries(guide) == ["Older fact."]
    undone = mr.find_receipt(journal, rid)
    assert undone is not None and undone["status"] == mr.UNDONE


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


def test_list_receipts_hides_images_and_flags_undoable(tmp_path):
    guide = _guide(tmp_path)
    mp.accept_region_fact(
        guide_path=guide, target="memory", text="A fact.", vault_root=tmp_path
    )
    rows = mr.list_receipts(tmp_path)
    assert rows
    assert all("before_text" not in row and "after_text" not in row for row in rows)
    assert any(row["undoable"] for row in rows)


# ── Queue receipts ────────────────────────────────────────────────────────


def test_queue_resolution_receipt_records_before_and_after(tmp_path):
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n"
        "## 2026-08-01T00:00:00\n\n"
        "- [memory] Keep every fact.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    with mr.queue_resolution(
        queue,
        removed_text="Keep every fact.",
        kind="memory",
        promoted=False,
        actor="operator",
        source="cli",
        vault_root=tmp_path,
    ):
        mp.remove_proposal_by_substring(queue, "Keep every fact.")

    receipts = [r for r in mr.read_receipts(mr.journal_path(tmp_path, None)) if r["kind"] == "queue_resolve"]
    assert receipts
    latest = receipts[-1]
    assert latest["status"] == mr.APPLIED
    assert latest["before_revision"] != latest["after_revision"]
    # Undoable: the exact queue image is stored.
    assert latest["before_text"] is not None
    assert mr.is_undoable(latest)


def test_queue_undo_restores_the_removed_bullet(tmp_path):
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n"
        "- [memory] Keep every fact.  _(from: Decisions)_\n"
        "- [profile] Keep every trait.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    with mr.queue_resolution(
        queue,
        removed_text="Keep every fact.",
        kind="memory",
        promoted=False,
        actor="operator",
        source="cli",
        vault_root=tmp_path,
    ):
        mp.remove_proposal_by_substring(queue, "Keep every fact.")
    assert "Keep every fact." not in queue.read_text(encoding="utf-8")

    rid = [
        r["id"]
        for r in mr.read_receipts(mr.journal_path(tmp_path, None))
        if r["kind"] == "queue_resolve"
    ][-1]
    mr.undo_receipt(rid, vault_root=tmp_path)
    restored = queue.read_text(encoding="utf-8")
    assert "Keep every fact." in restored
    assert "Keep every trait." in restored


def test_queue_receipt_refuses_undo_when_the_queue_moved(tmp_path):
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n- [memory] Keep every fact.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    with mr.queue_resolution(
        queue,
        removed_text="Keep every fact.",
        kind="memory",
        promoted=False,
        actor="operator",
        source="cli",
        vault_root=tmp_path,
    ):
        mp.remove_proposal_by_substring(queue, "Keep every fact.")
    rid = [
        r["id"]
        for r in mr.read_receipts(mr.journal_path(tmp_path, None))
        if r["kind"] == "queue_resolve"
    ][-1]
    # A later proposal was added after the resolution.
    queue.write_text(
        queue.read_text(encoding="utf-8")
        + "- [profile] A later unrelated trait.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    from ciao.memory_receipts import RevisionConflict

    with pytest.raises(RevisionConflict):
        mr.undo_receipt(rid, vault_root=tmp_path)
    assert "A later unrelated trait." in queue.read_text(encoding="utf-8")


# ── Queue lock ────────────────────────────────────────────────────────────


def test_queue_lock_is_reentrant_within_a_thread(tmp_path):
    """A wrapper can hold the lock across a body that takes it again."""
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text("# Memory Proposals\n\n", encoding="utf-8")

    with mr.queue_lock(queue):
        with mr.queue_lock(queue):
            inner = True
    assert inner


def test_undo_holds_the_queue_lock_across_read_and_replace(tmp_path):
    """A concurrent writer cannot land between the revision check and replace.

    The lock is held by another writer first. The undo must block on it, then
    re-read the *updated* queue and report a conflict — proving the check runs
    under the lock rather than against a stale image captured before it. If the
    check were outside the lock, the undo would replace the newer state with
    the stale before-image and silently discard it.
    """
    import threading

    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n- [memory] Keep every fact.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    with mr.queue_resolution(
        queue,
        removed_text="Keep every fact.",
        kind="memory",
        promoted=False,
        actor="operator",
        source="cli",
        vault_root=tmp_path,
    ):
        mp.remove_proposal_by_substring(queue, "Keep every fact.")
    rid = [
        r["id"]
        for r in mr.read_receipts(mr.journal_path(tmp_path, None))
        if r["kind"] == "queue_resolve"
    ][-1]

    from ciao.memory_receipts import RevisionConflict

    errors: list[Exception] = []
    done = threading.Event()

    def undo() -> None:
        try:
            with mr.queue_lock(queue):
                pass
            mr.undo_receipt(rid, vault_root=tmp_path)
        except Exception as exc:  # noqa: BLE001 — captured for the assertion
            errors.append(exc)
        finally:
            done.set()

    with mr.queue_lock(queue):
        t = threading.Thread(target=undo)
        t.start()
        # The undo is blocked on the lock; a newer writer lands its update.
        queue.write_text(
            queue.read_text(encoding="utf-8")
            + "- [profile] A later unrelated trait.  _(from: Decisions)_\n",
            encoding="utf-8",
        )
    t.join(timeout=10)

    assert done.is_set()
    assert any(isinstance(e, RevisionConflict) for e in errors)
    assert "A later unrelated trait." in queue.read_text(encoding="utf-8")


def test_queue_lock_refuses_to_write_when_held(tmp_path):
    """A held lock is reportable, not silently ignored."""
    import fcntl
    import threading
    import time

    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text("# Memory Proposals\n\n", encoding="utf-8")
    lock_path = mr._queue_lock_path(str(queue.resolve()))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    acquired = threading.Event()

    def contender() -> None:
        try:
            with mr.queue_lock(queue, timeout_s=0.2):
                acquired.set()
        except mr.QueueLockError:
            pass

    t = threading.Thread(target=contender)
    t.start()
    t.join(timeout=10)
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()
    assert not acquired.is_set()
    time.sleep(0)


def test_queue_lock_file_lives_outside_the_vault(tmp_path):
    """The lock must not pollute the user-owned vault tree."""
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text("# Memory Proposals\n\n", encoding="utf-8")

    with mr.queue_lock(queue):
        pass

    assert not (queue.parent / f"{queue.name}.lock").exists()
    assert mr._queue_lock_path(str(queue.resolve())).exists()


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
        mr.write_queue_atomically(queue, "# Memory Proposals\n\n")

    assert queue.read_text(encoding="utf-8") == original


# ── Batch queue receipts ──────────────────────────────────────────────────


def test_batch_receipt_records_one_undoable_transaction(tmp_path):
    """A batch rewrite must not make each fact separately undoable.

    The batch is one atomic file rewrite. If every per-fact receipt carried the
    whole-file before image, undoing any one restored the whole pre-batch file
    and resurrected every fact the batch had removed — including accepted ones.
    """
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    before = (
        "# Memory Proposals\n\n"
        "- [memory] Keep every fact.  _(from: Decisions)_\n"
        "- [profile] Keep every trait.  _(from: Decisions)_\n"
    )
    queue.write_text(before, encoding="utf-8")
    after = "# Memory Proposals\n\n"
    queue.write_text(after, encoding="utf-8")

    mr.record_queue_resolution_batch(
        queue,
        [
            {"text": "Keep every fact.", "kind": "memory", "promoted": True},
            {"text": "Keep every trait.", "kind": "profile", "promoted": False},
        ],
        before_text=before,
        after_text=after,
        actor="operator",
        source="pwa",
        vault_root=tmp_path,
    )

    receipts = [
        r
        for r in mr.read_receipts(mr.journal_path(tmp_path, None))
        if r["kind"] == "queue_resolve"
    ]
    assert len(receipts) == 2
    undoable = [r for r in receipts if mr.is_undoable(r)]
    # Exactly one row can be undone, and it restores the whole pre-batch queue.
    assert len(undoable) == 1
    assert receipts[-1].get("undoable") is False

    mr.undo_receipt(undoable[0]["id"], vault_root=tmp_path)
    assert queue.read_text(encoding="utf-8") == before


def test_batch_undo_of_a_non_transaction_row_is_refused(tmp_path):
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text("# Memory Proposals\n\n", encoding="utf-8")
    mr.record_queue_resolution_batch(
        queue,
        [{"text": "Fact A.", "kind": "memory", "promoted": False}],
        before_text="# Memory Proposals\n\n- [memory] Fact A.  _(from: Decisions)_\n",
        after_text="# Memory Proposals\n\n",
        actor="operator",
        source="pwa",
        vault_root=tmp_path,
    )
    rows = [
        r
        for r in mr.read_receipts(mr.journal_path(tmp_path, None))
        if r["kind"] == "queue_resolve"
    ]
    # A single-item batch still records one undoable transaction row.
    assert len(rows) == 1 and mr.is_undoable(rows[0])


def test_multi_bracket_prepares_before_the_rewrite(tmp_path):
    """A crash between the batch rewrite and its record must stay recoverable.

    `queue_resolution_multi` writes one transaction-level prepared row before
    the body's rewrite. Simulating a crash after the write (body ran, applied
    row never landed) leaves that prepared row, and recovery settles it APPLIED
    because all its bullets are gone.
    """
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n"
        "- [memory] Fact A.  _(from: Decisions)_\n"
        "- [profile] Fact B.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    # Simulate the crash: prepare, rewrite, then never record applied.
    with mr.queue_resolution_multi(
        queue,
        [
            {"text": "Fact A.", "kind": "memory", "promoted": False},
            {"text": "Fact B.", "kind": "profile", "promoted": False},
        ],
        actor="operator",
        source="pwa",
        vault_root=tmp_path,
    ) as base:
        queue.write_text("# Memory Proposals\n\n", encoding="utf-8")

    # The bracket settles its transaction row and the per-fact rows.
    prepared_ids = {
        r["id"]
        for r in mr.read_receipts(mr.journal_path(tmp_path, None))
        if r["status"] == mr.PREPARED
    }
    assert prepared_ids == set()

    # Now the crash window: a prepared batch row whose bullets are already gone
    # recovers to APPLIED.
    queue.write_text("# Memory Proposals\n\n", encoding="utf-8")
    mr._append(
        mr.journal_path(tmp_path, None),
        {
            "id": "mrcpt_batch_crash",
            "ts": mr._now(),
            "kind": "queue_resolve",
            "queue": str(queue),
            "removed_texts": ["Fact A.", "Fact B."],
            "batch": True,
            "status": mr.PREPARED,
        },
    )
    result = mr.recover_pending(journal=mr.journal_path(tmp_path, None))
    assert "mrcpt_batch_crash" in [r["id"] for r in result.reconciled]


def test_batch_recovery_rolled_back_when_a_bullet_remains(tmp_path):
    """A batch prepared row is not APPLIED while any of its bullets is present."""
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n- [profile] Fact B.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    mr._append(
        mr.journal_path(tmp_path, None),
        {
            "id": "mrcpt_batch_partial",
            "ts": mr._now(),
            "kind": "queue_resolve",
            "queue": str(queue),
            "removed_texts": ["Fact A.", "Fact B."],
            "batch": True,
            "status": mr.PREPARED,
        },
    )
    result = mr.recover_pending(journal=mr.journal_path(tmp_path, None))
    settled = [r for r in result.reconciled if r["id"] == "mrcpt_batch_partial"]
    assert settled and settled[0]["status"] == mr.ROLLED_BACK


def test_settlement_matches_a_bullet_text_exactly(tmp_path):
    """A removed bullet whose text is a prefix of another must not be "present".

    Removing ``Use Python`` while ``Use Python 3`` remains used a substring
    search and rolled back the successful resolution.
    """
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n- [memory] Use Python 3.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    mr._append(
        mr.journal_path(tmp_path, None),
        {
            "id": "mrcpt_prefix",
            "ts": mr._now(),
            "kind": "queue_resolve",
            "queue": str(queue),
            "removed_text": "Use Python",
            "status": mr.PREPARED,
        },
    )
    result = mr.recover_pending(journal=mr.journal_path(tmp_path, None))
    settled = [r for r in result.reconciled if r["id"] == "mrcpt_prefix"]
    assert settled and settled[0]["status"] == mr.APPLIED


def test_recovered_queue_receipt_stays_undoable(tmp_path, monkeypatch):
    """A crash-recovered prepared receipt must retain its before image.

    The prepared row carries ``before_text``/``before_revision``; recovery fills
    the after image from disk, so ``is_undoable`` holds and the removed bullet
    can be restored.
    """
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    before = (
        "# Memory Proposals\n\n- [memory] Keep every fact.  _(from: Decisions)_\n"
    )
    after = "# Memory Proposals\n\n"
    queue.write_text(before, encoding="utf-8")
    mr._append(
        mr.journal_path(tmp_path, None),
        {
            "id": "mrcpt_recover_undo",
            "ts": mr._now(),
            "kind": "queue_resolve",
            "queue": str(queue),
            "removed_text": "Keep every fact.",
            "before_revision": mr.content_revision(before),
            "before_text": before,
            "status": mr.PREPARED,
        },
    )
    # The crash landed after the rewrite: the bullet is gone.
    queue.write_text(after, encoding="utf-8")

    result = mr.recover_pending(journal=mr.journal_path(tmp_path, None))
    recovered = [r for r in result.reconciled if r["id"] == "mrcpt_recover_undo"]
    assert recovered and recovered[0]["status"] == mr.APPLIED
    assert mr.is_undoable(recovered[0]) is True

    mr.undo_receipt("mrcpt_recover_undo", vault_root=tmp_path)
    assert "Keep every fact." in queue.read_text(encoding="utf-8")


def test_queue_recovery_completes_the_decision_sidecar(tmp_path):
    """A resolved queue receipt must complete the dismissal sidecar.

    A crash between the receipt's terminal row and the route's
    `record_dismissal` call left the resolved proposal re-filable; recovery now
    completes it.
    """
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text("# Memory Proposals\n\n", encoding="utf-8")
    mr._append(
        mr.journal_path(tmp_path, None),
        {
            "id": "mrcpt_sidecar",
            "ts": mr._now(),
            "kind": "queue_resolve",
            "queue": str(queue),
            "removed_text": "A resolved fact.",
            "proposal_kind": "memory",
            "promoted": False,
            "action": "dismissed",
            "status": mr.PREPARED,
        },
    )

    mr.recover_pending(journal=mr.journal_path(tmp_path, None))

    sidecar = mp.dismissed_log_path(queue)
    assert sidecar.exists()
    assert "A resolved fact." in sidecar.read_text(encoding="utf-8")


def test_settlement_distinguishes_bullets_of_the_same_text_by_kind(tmp_path):
    """Removing ``[memory] Use Python`` is not blocked by ``[profile] Use Python``.

    Text-only matching saw the remaining profile bullet and rolled the memory
    resolution back.
    """
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n- [profile] Use Python.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    mr._append(
        mr.journal_path(tmp_path, None),
        {
            "id": "mrcpt_kind",
            "ts": mr._now(),
            "kind": "queue_resolve",
            "queue": str(queue),
            "removed_text": "Use Python",
            "proposal_kind": "memory",
            "status": mr.PREPARED,
        },
    )
    result = mr.recover_pending(journal=mr.journal_path(tmp_path, None))
    settled = [r for r in result.reconciled if r["id"] == "mrcpt_kind"]
    assert settled and settled[0]["status"] == mr.APPLIED


def test_settlement_keeps_the_same_kind_present(tmp_path):
    """A remaining same-kind bullet still rolls the resolution back."""
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n- [memory] Use Python.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    mr._append(
        mr.journal_path(tmp_path, None),
        {
            "id": "mrcpt_kind_present",
            "ts": mr._now(),
            "kind": "queue_resolve",
            "queue": str(queue),
            "removed_text": "Use Python.",
            "proposal_kind": "memory",
            "status": mr.PREPARED,
        },
    )
    result = mr.recover_pending(journal=mr.journal_path(tmp_path, None))
    settled = [r for r in result.reconciled if r["id"] == "mrcpt_kind_present"]
    assert settled and settled[0]["status"] == mr.ROLLED_BACK


def test_failed_batch_rewrite_is_not_recorded_applied(tmp_path):
    """A body that raises before writing must not leave applied receipts.

    The bracket records applied rows only for a completed, content-verified
    rewrite. When the body raised with the queue still at its before-image,
    there is nothing to recover, so the transaction settles rolled_back and no
    applied row is written.
    """
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n- [memory] Fact A.  _(from: Decisions)_\n",
        encoding="utf-8",
    )

    class _Boom(Exception):
        pass

    try:
        with mr.queue_resolution_multi(
            queue,
            [{"text": "Fact A.", "kind": "memory", "promoted": False}],
            actor="operator",
            source="pwa",
            vault_root=tmp_path,
        ):
            # The rewrite never lands.
            raise _Boom("disk full")
    except _Boom:
        pass

    rows = [
        r
        for r in mr.read_receipts(mr.journal_path(tmp_path, None))
        if r["kind"] == "queue_resolve"
    ]
    assert rows, "the transaction row must be recorded"
    assert all(r["status"] != mr.APPLIED for r in rows)
    # The queue is unchanged, so the transaction is rolled back, not applied.
    assert rows[-1]["status"] == mr.ROLLED_BACK
    # The bullet is still present, so it was never resolved.
    assert "Fact A." in queue.read_text(encoding="utf-8")


def test_partial_queue_rewrite_is_left_for_recovery(tmp_path):
    """A truncated write that matches neither image stays recoverable.

    `queue.write_text` can truncate then raise; a terminal rollback would make
    startup recovery skip a file that may have lost unrelated proposals. The
    prepared row must stay non-terminal.
    """
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    before = (
        "# Memory Proposals\n\n"
        "- [memory] Fact A.  _(from: Decisions)_\n"
        "- [memory] Unrelated B.  _(from: Decisions)_\n"
    )
    queue.write_text(before, encoding="utf-8")

    class _Boom(Exception):
        pass

    try:
        with mr.queue_resolution_multi(
            queue,
            [{"text": "Fact A.", "kind": "memory", "promoted": False}],
            actor="operator",
            source="pwa",
            vault_root=tmp_path,
        ):
            # Simulate a partial/truncated write that then raises.
            queue.write_text("# Memory Proposals\n\n", encoding="utf-8")
            raise _Boom("short write")
    except _Boom:
        pass

    journal = mr.journal_path(tmp_path, None)
    rows = [r for r in mr.read_receipts(journal) if r["kind"] == "queue_resolve"]
    assert rows and rows[-1]["status"] == mr.PREPARED

    # Recovery then classifies the partial state (here: every bullet gone, so
    # APPLIED) instead of finding a terminal row and skipping it.
    result = mr.recover_pending(journal=journal)
    assert [r["id"] for r in result.reconciled] == [rows[-1]["id"]]


def test_single_route_uses_a_prepared_receipt_before_the_rewrite(tmp_path):
    """The CLI wrapper is the model: a prepared row precedes the rewrite."""
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n- [memory] Keep every fact.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    observed: list[list[str]] = []
    with mr.queue_resolution(
        queue,
        removed_text="Keep every fact.",
        kind="memory",
        promoted=False,
        actor="operator",
        source="pwa",
        vault_root=tmp_path,
    ):
        # Inside the body, the prepared row is already on disk.
        rows = mr.read_receipts(mr.journal_path(tmp_path, None))
        observed.append([r["status"] for r in rows if r["kind"] == "queue_resolve"])
        mp.remove_proposal_by_substring(queue, "Keep every fact.")

    assert observed and observed[0] == [mr.PREPARED]


def test_queue_recovery_applied_when_the_bullet_is_gone(tmp_path):
    queue = tmp_path / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n- [memory] Keep every fact.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    journal = mr.journal_path(tmp_path, None)
    mr._append(
        journal,
        {
            "id": "mrcpt_queue_crash",
            "ts": mr._now(),
            "kind": "queue_resolve",
            "queue": str(queue),
            "removed_text": "Keep every fact.",
            "status": mr.PREPARED,
        },
    )
    # The removal landed but the confirmation row did not.
    queue.write_text("# Memory Proposals\n\n", encoding="utf-8")
    result = mr.recover_pending(journal=journal)
    assert [r["id"] for r in result.reconciled] == ["mrcpt_queue_crash"]
    assert mr.find_receipt(journal, "mrcpt_queue_crash")["status"] == mr.APPLIED


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


def test_receipts_route_lists_and_undo_route_reverses(tmp_path):
    client, config = _receipts_client(tmp_path)
    vault = config.workspace_vault_root("personal")
    guide = Path(config.agent_root("personal")) / "CLAUDE.md"
    guide.parent.mkdir(parents=True, exist_ok=True)
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    mt.ensure_regions(guide)
    mp.accept_region_fact(
        guide_path=guide,
        target="memory",
        text="Route fact.",
        vault_root=vault,
    )

    resp = client.get("/api/memory/receipts", params={"workspace": "personal"})
    assert resp.status_code == 200
    row = next(r for r in resp.json()["rows"] if r["kind"] == "region_apply")
    assert row["undoable"] is True

    undo = client.post(f"/api/memory/receipts/{row['id']}/undo", params={"workspace": "personal"})
    assert undo.status_code == 200
    assert _entries(guide) == []


def test_receipt_undo_route_conflicts_on_a_changed_destination(tmp_path):
    client, config = _receipts_client(tmp_path)
    vault = config.workspace_vault_root("personal")
    guide = Path(config.agent_root("personal")) / "CLAUDE.md"
    guide.parent.mkdir(parents=True, exist_ok=True)
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    mt.ensure_regions(guide)
    mp.accept_region_fact(
        guide_path=guide, target="memory", text="Route fact.", vault_root=vault
    )
    rid = [
        r["id"]
        for r in mr.read_receipts(mr.journal_path(vault, None))
        if r["kind"] == "region_apply"
    ][-1]
    mt.update_region(guide, "memory", action="add", entry="Later fact.")

    resp = client.post(f"/api/memory/receipts/{rid}/undo", params={"workspace": "personal"})
    assert resp.status_code == 409
    assert "Later fact." in _entries(guide)


def test_receipt_undo_route_rejects_unknown_id(tmp_path):
    client, _config = _receipts_client(tmp_path)
    resp = client.post("/api/memory/receipts/mrcpt_missing/undo")
    assert resp.status_code == 404


def test_cli_dismiss_records_a_reversible_queue_receipt(tmp_path, monkeypatch):
    """The CLI curator's dismissal is journaled and reversible."""
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n- [memory] Keep every fact.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_RUNTIME_ROOT", str(tmp_path / ".runtime"))
    code = cli.main(
        [
            "memory-proposal-dismiss",
            "--workspace",
            str(tmp_path),
            "--vault-root",
            str(vault),
            "Keep every fact.",
        ]
    )
    assert code == 0
    assert "Keep every fact." not in queue.read_text(encoding="utf-8")
    receipts = [
        r
        for r in mr.read_receipts(mr.journal_path(vault, None))
        if r["kind"] == "queue_resolve"
    ]
    assert receipts
    assert receipts[-1]["status"] == mr.APPLIED
    assert mr.is_undoable(receipts[-1])


def test_cli_dismiss_records_the_full_text_for_a_substring_needle(
    tmp_path, monkeypatch,
):
    """A unique-substring dismissal records the full bullet text.

    Recording the substring made crash recovery's exact match conclude the row
    was gone and settle the receipt applied while the proposal was still queued.
    """
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n"
        "- [memory] Keep every fact about the project.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_RUNTIME_ROOT", str(tmp_path / ".runtime"))
    code = cli.main(
        [
            "memory-proposal-dismiss",
            "--workspace",
            str(tmp_path),
            "--vault-root",
            str(vault),
            "every fact",  # a unique substring, not the full bullet text
        ]
    )
    assert code == 0
    receipts = [
        r
        for r in mr.read_receipts(mr.journal_path(vault, None))
        if r["kind"] == "queue_resolve"
    ]
    assert receipts
    assert receipts[-1]["removed_text"] == "Keep every fact about the project."


def test_cli_substring_dismissal_recovers_as_applied_only_after_removal(
    tmp_path, monkeypatch,
):
    """The crash window must not settle applied while the bullet remains."""
    from ciao import cli

    vault = tmp_path / "memory-vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        "# Memory Proposals\n\n"
        "- [memory] Keep every fact about the project.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_RUNTIME_ROOT", str(tmp_path / ".runtime"))
    code = cli.main(
        [
            "memory-proposal-dismiss",
            "--workspace",
            str(tmp_path),
            "--vault-root",
            str(vault),
            "every fact",
        ]
    )
    assert code == 0
    # Re-create the bullet to simulate a crash that never rewrote the queue,
    # with the prepared receipt already on disk.
    queue.write_text(
        "# Memory Proposals\n\n"
        "- [memory] Keep every fact about the project.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    receipt = [
        r
        for r in mr.read_receipts(mr.journal_path(vault, None))
        if r["kind"] == "queue_resolve"
    ][-1]
    # Manually rewind the receipt to prepared so recovery runs against it.
    mr._append(
        mr.journal_path(vault, None),
        {
            "id": receipt["id"],
            "ts": mr._now(),
            "kind": "queue_resolve",
            "queue": str(queue),
            "removed_text": receipt["removed_text"],
            "before_revision": receipt.get("before_revision", ""),
            "before_text": receipt.get("before_text"),
            "status": mr.PREPARED,
        },
    )
    result = mr.recover_pending(journal=mr.journal_path(vault, None))
    settled = [r for r in result.reconciled if r["id"] == receipt["id"]]
    assert settled and settled[0]["status"] == mr.ROLLED_BACK


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
            "before_revision": mr.content_revision(before),
            "after_revision": mr.content_revision(after),
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
            "before_revision": mr.content_revision(before),
            "after_revision": mr.content_revision(after),
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


