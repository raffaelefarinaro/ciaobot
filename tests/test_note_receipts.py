"""Revision-checked note receipts: apply, recovery and undo.

``tests/test_memory_receipts.py`` covers the region/queue/category protocol and
is read-only regression coverage for this one. These tests pin what the note
protocol adds on top of it — the things a Markdown file needs that a fenced
region does not:

* bytes, not text: a BOM, CRLF pairs and a missing final newline survive an
  apply and come back byte-identical through an undo, and a note's permissions
  are carried across the replacement;
* confinement: a caller names a vault-relative path, and an absolute, ``..``,
  symlinked, missing, non-Markdown or non-file target is refused, as is the app's
  own bookkeeping under ``Workspace/`` and a name two on-disk files claim on a
  case-sensitive filesystem; both spellings of a name on a case-insensitive one
  resolve to a single file, so two writers cannot reach one note through two
  locks. A *receipt* whose recorded target does not resolve inside the vault the
  journal is actually found in is refused just the same;
* never view-only: content that is not UTF-8, or whose image is larger than a
  receipt can hold, is refused before the mutation rather than applied and left
  un-undoable; a write that changed nothing is not offered an undo;
* honest settlement: a journal that cannot record the prepared row aborts the
  write; a write that replaced the note but could not confirm it leaves a
  recoverable row, never a terminal one that would hide the mutation; and an
  interrupted undo, including the window between its reverse rename and the
  original's own settlement, recovers to one consistent result. An undo whose
  final settlement row could not be appended says the undo *landed* instead of
  reporting a write error, and an undo refuses a before image that no longer
  hashes to the revision its receipt records;

Every test uses a temporary synthetic vault. Nothing here reads or writes a
real install's notes, and no engine, service or scheduler is started.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from ciao import memory_receipts as mr
from ciao import note_receipts as nr

NOTE = "notes/topic.md"


# ── Fixtures and helpers ───────────────────────────────────────────────────


def _vault(tmp_path: Path, name: str = "vault") -> Path:
    """An empty synthetic vault with a ``Workspace`` folder for the journal."""
    vault = tmp_path / name
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    (vault / "notes").mkdir(exist_ok=True)
    return vault


def _write(vault: Path, relative: str, text: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _revision(path: Path) -> str:
    """The revision of a note's exact bytes, as a caller would have read them."""
    return mr.content_revision(path.read_bytes().decode("utf-8"))


def _apply(
    vault: Path,
    *,
    after: str,
    expected: str | None = None,
    path: Path | None = None,
    relative: str = NOTE,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One managed note write, the way a caller would make it."""
    target = path if path is not None else vault / relative
    return nr.commit_note_change(
        vault_root=vault,
        relative_path=relative,
        expected_revision=expected if expected is not None else _revision(target),
        after_text=after,
        actor="operator",
        source="pwa",
        workspace="personal",
        provenance=provenance,
    )


def _journal(vault: Path) -> Path:
    return mr.journal_path(vault, None)


def _rows(vault: Path) -> list[dict[str, Any]]:
    return mr.read_receipts(_journal(vault))


# ── Exact bytes and mode ───────────────────────────────────────────────────

# A BOM, CRLF line endings, and no trailing newline: the three things a
# text-mode round trip quietly rewrites. Both images are unterminated, so a
# write that appended a final newline would fail here too.
BOM_CRLF = "﻿# Topic\r\n\r\nfirst line\r\nlast line, unterminated"
BOM_CRLF_AFTER = "﻿# Topic\r\n\r\nfirst line\r\nedited line, unterminated"


def test_note_apply_and_undo_preserve_exact_bytes_and_mode(tmp_path):
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, BOM_CRLF)
    os.chmod(path, 0o640)
    before_bytes = path.read_bytes()
    mode = stat.S_IMODE(path.stat().st_mode)

    receipt = _apply(
        vault,
        after=BOM_CRLF_AFTER,
        provenance={"message_id": "m-1", "policy_version": 2},
    )

    assert receipt["status"] == mr.APPLIED
    assert receipt["changed"] is True
    # Every byte the caller handed us, and the bytes it read, unchanged.
    assert path.read_bytes() == BOM_CRLF_AFTER.encode("utf-8")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf"), "the BOM must survive"
    assert b"\r\n" in path.read_bytes(), "CRLF must not be rewritten to LF"
    assert not path.read_bytes().endswith(b"\n"), "no newline may be appended"
    assert stat.S_IMODE(path.stat().st_mode) == mode

    # The receipt describes exactly what happened.
    assert receipt["relative_path"] == NOTE
    assert receipt["before_text"] == BOM_CRLF
    assert receipt["after_text"] == BOM_CRLF_AFTER
    assert receipt["before_revision"] == mr.content_revision(BOM_CRLF)
    assert receipt["after_revision"] == mr.content_revision(BOM_CRLF_AFTER)
    assert receipt["workspace"] == "personal"
    assert receipt["actor"] == "operator"
    assert receipt["source"] == "pwa"
    assert receipt["provenance"] == {"message_id": "m-1", "policy_version": 2}
    assert mr.is_undoable(receipt)

    undone = mr.undo_receipt(receipt["id"], vault_root=vault)
    assert undone["status"] == mr.UNDONE
    assert undone["undo_of"] == receipt["id"]
    assert undone["undo_receipt"]

    # The undo restores the predecessor byte for byte, permissions included.
    assert path.read_bytes() == before_bytes
    assert stat.S_IMODE(path.stat().st_mode) == mode

    journal = _journal(vault)
    original = mr.find_receipt(journal, receipt["id"])
    assert original is not None and original["status"] == mr.UNDONE
    assert not mr.is_undoable(original), "a settled receipt offers no second undo"

    reverse = mr.find_receipt(journal, undone["undo_receipt"])
    assert reverse is not None
    assert reverse["kind"] == "note_undo"
    assert reverse["undo_of"] == receipt["id"]
    assert reverse["before_text"] == BOM_CRLF_AFTER
    assert reverse["after_text"] == BOM_CRLF
    # Undo is not offered for undo.
    assert not mr.is_undoable(reverse)

    # History discovers the new rows through the unchanged public surface, and
    # still withholds the multi-kilobyte images from a list view.
    history = {row["id"]: row for row in mr.list_receipts(vault, limit=10)}
    assert receipt["id"] in history
    assert undone["undo_receipt"] in history
    assert history[receipt["id"]]["undoable"] is False
    assert all("before_text" not in row for row in history.values())


def test_note_apply_noop(tmp_path, monkeypatch):
    """An unchanged note gets a receipt and no replacement at all."""
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, "already correct\n")
    original_bytes = path.read_bytes()
    inode = path.stat().st_ino
    replaced: list[str] = []
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        replaced.append(str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy)

    receipt = _apply(vault, after="already correct\n")

    assert receipt["status"] == mr.APPLIED
    assert receipt["changed"] is False
    assert replaced == [], "a no-op must not replace the file"
    assert path.stat().st_ino == inode
    assert path.read_bytes() == original_bytes
    # It is still a real record, so a caller can tell this apart from a write.
    assert mr.find_receipt(_journal(vault), receipt["id"])["changed"] is False


def test_note_noop_apply_offers_no_undo(tmp_path):
    """A write that changed nothing must not be offered an undo.

    The no-op row carries the note's own text as both images, so it passed
    every "is this reversible?" test and History offered an Undo that rewrote the
    file with the bytes it already held. It stays a real record — a caller can
    still tell "already satisfied" from "wrote something" — it just has nothing
    to reverse.
    """
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, "already correct\n")

    noop = _apply(vault, after="already correct\n")

    assert noop["status"] == mr.APPLIED
    assert noop["changed"] is False
    assert noop["undoable"] is False
    assert mr.is_undoable(noop) is False
    row = mr.find_receipt(_journal(vault), noop["id"])
    assert row is not None and row["changed"] is False
    assert mr.is_undoable(row) is False
    # History reads the flag off the same predicate, so the affordance is gone
    # there too.
    listed = {r["id"]: r for r in mr.list_receipts(vault)}
    assert listed[noop["id"]]["undoable"] is False
    with pytest.raises(mr.UndoUnsupported):
        mr.undo_receipt(noop["id"], vault_root=vault)
    assert path.read_bytes() == b"already correct\n"

    # A real change to the same note is untouched by that, and still reverses.
    changed = _apply(vault, after="now different\n")
    assert changed["changed"] is True
    assert "undoable" not in changed
    assert mr.is_undoable(changed) is True
    mr.undo_receipt(changed["id"], vault_root=vault)
    assert path.read_bytes() == b"already correct\n"


def test_note_revision_conflict_preserves_external_edit(tmp_path):
    """A stale expectation refuses; the hand edit that made it stale survives."""
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, "mine\n")
    stale = _revision(path)
    path.write_bytes(b"theirs, written by hand\n")
    their_bytes = path.read_bytes()

    with pytest.raises(mr.RevisionConflict):
        _apply(vault, after="mine, updated\n", expected=stale)

    assert path.read_bytes() == their_bytes
    # Refused before the prepared row, so there is nothing to recover.
    assert _rows(vault) == []
    assert mr.recover_pending(journal=_journal(vault)).reconciled == []


def test_note_blank_expected_revision_is_refused(tmp_path):
    """This primitive never overwrites a note it has not read."""
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, "read me first\n")
    before_bytes = path.read_bytes()

    for blank in ("", "   "):
        with pytest.raises(mr.MemoryReceiptError):
            _apply(vault, after="blind overwrite\n", expected=blank)
        assert path.read_bytes() == before_bytes
    assert _rows(vault) == []


# ── Confinement ────────────────────────────────────────────────────────────


def _confined_vault(tmp_path: Path) -> tuple[Path, Path]:
    """A vault next to an outside folder, wired with every trap worth trying."""
    vault = _vault(tmp_path)
    _write(vault, NOTE, "inside\n")
    _write(vault, "notes/plain.txt", "not markdown\n")
    (vault / "folder.md").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = _write(outside, "secret.md", "secret\n")
    (vault / "link-dir").symlink_to(outside, target_is_directory=True)
    (vault / "link-file.md").symlink_to(secret)
    return vault, secret


@pytest.mark.parametrize(
    "relative_path, why",
    [
        ("", "an empty path names nothing"),
        ("../outside/secret.md", "traversal out of the vault"),
        ("notes/../../outside/secret.md", "traversal in the middle"),
        ("/etc/hosts", "an absolute path"),
        ("link-dir/secret.md", "a symlinked ancestor"),
        ("link-file.md", "a symlinked file"),
        ("notes/missing.md", "a note that does not exist"),
        ("notes/plain.txt", "a file that is not Markdown"),
        ("folder.md", "a directory named like a note"),
    ],
)
def test_note_target_confinement(tmp_path, relative_path, why):
    vault, secret = _confined_vault(tmp_path)
    inside_before = (vault / NOTE).read_bytes()
    secret_before = secret.read_bytes()

    with pytest.raises(nr.NoteTargetRefused):
        nr.commit_note_change(
            vault_root=vault,
            relative_path=relative_path,
            expected_revision=mr.content_revision(""),
            after_text="overwritten\n",
            actor="agent",
            source="cli",
        )

    assert (vault / NOTE).read_bytes() == inside_before
    assert secret.read_bytes() == secret_before, f"nothing may be written for {why}"
    assert _rows(vault) == []


def _case_insensitive_filesystem(tmp_path: Path) -> bool:
    """Whether this tmp filesystem resolves two spellings of one name together.

    Probed rather than assumed: the same suite runs on a case-sensitive CI
    filesystem, where the two spellings really are two paths and there is
    nothing to canonicalize.
    """
    probe = tmp_path / "CaseProbe"
    probe.write_text("x", encoding="utf-8")
    return (tmp_path / "caseprobe").exists()


def test_note_both_spellings_of_a_name_are_one_file_one_lock(tmp_path):
    """`Notes/A.md` and `notes/a.md` are one note, so they are one lock.

    On a case-insensitive filesystem both spellings open the same file while the
    two *strings* stay different, and the per-file lock is keyed on the resolved
    path string. Two writers naming the note differently therefore took two
    locks, both read the same bytes, both compared equal to the same expected
    revision, and both landed — one write silently discarded, and two
    ``relative_path`` spellings in history for one file. Resolving the name from
    the directory listing fixes the lock key and the recorded path at once.
    """
    if not _case_insensitive_filesystem(tmp_path):
        pytest.skip("a case-sensitive filesystem has two genuinely different names")
    vault = _vault(tmp_path)
    # A directory the fixture does not already create, so the on-disk spelling
    # here is the one this test writes.
    path = _write(vault, "Topics/Alpha.md", "base\n")
    root = nr.canonical_vault(vault)

    canonical = nr.resolve_note_path(vault, "Topics/Alpha.md")
    other = nr.resolve_note_path(vault, "topics/alpha.md")

    assert canonical == root / "Topics" / "Alpha.md"
    assert other == canonical, "both spellings must name one file"
    assert other.relative_to(root).as_posix() == "Topics/Alpha.md"
    # The lock is keyed on this string, so this is the whole of the fix.
    assert str(canonical.resolve()) == str(other.resolve())

    # One stored spelling in history, whichever the caller used.
    first = _apply(vault, after="one\n", relative="Topics/Alpha.md")
    second = _apply(vault, after="two\n", relative="topics/alpha.md")
    assert first["relative_path"] == second["relative_path"] == "Topics/Alpha.md"
    assert {row["relative_path"] for row in _rows(vault)} == {"Topics/Alpha.md"}

    # And one lock, proven the only way it shows: two writers, one expected
    # revision, two spellings. Exactly one may land.
    path.write_bytes(b"base\n")
    expected = _revision(path)
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def writer(name: str, relative: str, text: str) -> None:
        barrier.wait()
        try:
            _apply(vault, after=text, expected=expected, relative=relative)
        except mr.RevisionConflict:
            outcomes[name] = "conflict"
        except Exception as exc:  # noqa: BLE001 — surfaced by the assertion below
            outcomes[name] = f"unexpected: {exc!r}"
        else:
            outcomes[name] = "applied"

    threads = [
        threading.Thread(target=writer, args=("upper", "Topics/Alpha.md", "upper\n")),
        threading.Thread(target=writer, args=("lower", "topics/alpha.md", "lower\n")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert sorted(outcomes.values()) == ["applied", "conflict"], outcomes
    assert path.read_bytes().decode("utf-8") in {"upper\n", "lower\n"}
    assert {row["relative_path"] for row in _rows(vault)} == {"Topics/Alpha.md"}


def test_note_refuses_a_name_two_files_claim(tmp_path):
    """A name two on-disk entries match is not a name to write through.

    Only reachable on a case-sensitive filesystem, where ``A.md`` and ``a.md``
    can both exist and a caller asking for ``A.MD`` matches neither exactly.
    Picking one would be a coin flip the caller cannot see, so the walk refuses
    instead of writing a file the caller did not name.
    """
    if _case_insensitive_filesystem(tmp_path):
        pytest.skip("a case-insensitive filesystem cannot hold both spellings")
    vault = _vault(tmp_path)
    upper = _write(vault, "Topics/Alpha.md", "the upper one\n")
    lower = _write(vault, "Topics/alpha.md", "the lower one\n")

    with pytest.raises(nr.NoteTargetRefused):
        nr.resolve_note_path(vault, "Topics/ALPHA.md")

    # The exact spellings are still fine: one of them is what the caller named.
    assert nr.resolve_note_path(vault, "Topics/Alpha.md") == upper
    assert nr.resolve_note_path(vault, "Topics/alpha.md") == lower


@pytest.mark.parametrize(
    "relative_path",
    [
        "Workspace/Memory-Proposals.md",
        "Workspace/Curation-Log.md",
        "workspace/memory-consolidations.md",
    ],
)
def test_note_target_refuses_vault_bookkeeping(tmp_path, relative_path):
    """The app's own bookkeeping is written by the pipeline that owns it.

    Every ``.md`` in the vault used to be writable through this protocol,
    including the proposal queue and the curation logs the memory pipeline
    maintains with their own atomic writes. A note edit there bypasses every
    guard those writers rely on, and History would offer an ordinary-looking undo
    of a file the user never edited. A note that merely *shares* a name outside
    ``Workspace/`` — and a note at the vault root — stays writable.
    """
    vault = _vault(tmp_path)
    body = "- a queue bullet the pipeline owns\n"
    target = _write(vault, relative_path, body)
    root = nr.canonical_vault(vault)
    _write(vault, NOTE, "inside\n")
    _write(vault, "projects/acme/workspace/Curation-Log.md", "a real note\n")
    _write(vault, "Top-Level.md", "a real note at the root\n")

    with pytest.raises(nr.NoteTargetRefused):
        nr.resolve_note_path(vault, relative_path)

    # The write path refuses before a byte is read, and journals nothing.
    with pytest.raises(nr.NoteTargetRefused):
        nr.commit_note_change(
            vault_root=vault,
            relative_path=relative_path,
            expected_revision=mr.content_revision(body),
            after_text="overwritten\n",
            actor="agent",
            source="cli",
        )
    assert target.read_bytes() == body.encode("utf-8")
    assert _rows(vault) == []

    # Everything else in the vault is still writable, including the same
    # filename one directory away from where the app writes it.
    for allowed in (NOTE, "projects/acme/workspace/Curation-Log.md", "Top-Level.md"):
        assert nr.resolve_note_path(vault, allowed).is_file()
    assert nr.resolve_note_path(vault, NOTE).relative_to(root).as_posix() == NOTE


def test_note_forged_receipt_target_is_refused(tmp_path):
    """A receipt's own fields never decide what recovery touches.

    The row below is hand-written and names an absolute target outside the
    vault, a vault-relative path that traverses out of it, and a ``vault_root``
    that is not the vault its journal lives in. Recovery resolves the target
    against the journal's own vault, finds nothing writable there, and settles
    a conflict — the outside file is never opened.
    """
    vault, secret = _confined_vault(tmp_path)
    journal = _journal(vault)
    forged = {
        "id": "mrcpt_note_forged",
        "ts": mr._now(),
        "actor": "auto",
        "source": "mcp",
        "workspace": "personal",
        "kind": "note_apply",
        "note": str(secret),
        "relative_path": "../outside/secret.md",
        "vault_root": str(tmp_path),
        "before_revision": mr.content_revision("a"),
        "after_revision": mr.content_revision("b"),
        "before_text": "a",
        "after_text": "b",
        "status": mr.PREPARED,
    }
    mr._append(journal, forged)
    secret_before = secret.read_bytes()

    result = mr.recover_pending(journal=journal)

    assert [row["id"] for row in result.conflicts] == ["mrcpt_note_forged"]
    settled = mr.find_receipt(journal, "mrcpt_note_forged")
    assert settled is not None and settled["status"] == mr.CONFLICT
    assert secret.read_bytes() == secret_before

    # The same refusal governs undo: a forged applied row is not reversible.
    forged_applied = {**forged, "id": "mrcpt_note_forged_applied", "status": mr.APPLIED}
    mr._append(journal, forged_applied)
    with pytest.raises(mr.MemoryReceiptError):
        mr.undo_receipt("mrcpt_note_forged_applied", journal=journal)
    assert secret.read_bytes() == secret_before


def test_note_recovery_uses_the_journals_vault_not_the_receipts(tmp_path):
    """A moved vault reconciles against where its journal is now found."""
    home = _vault(tmp_path, "home")
    elsewhere = _vault(tmp_path, "elsewhere")
    home_note = _write(home, NOTE, "home copy\n")
    other_note = _write(elsewhere, NOTE, "elsewhere copy\n")

    journal = _journal(home)
    # A row claiming the note lives in the other vault, with that vault's
    # revisions — it can only be true of the file in `elsewhere`.
    mr._append(
        journal,
        {
            "id": "mrcpt_note_moved",
            "ts": mr._now(),
            "actor": "agent",
            "source": "cli",
            "workspace": "personal",
            "kind": "note_apply",
            "note": str(other_note),
            "relative_path": NOTE,
            "vault_root": str(elsewhere),
            "before_revision": mr.content_revision("elsewhere copy\n"),
            "after_revision": mr.content_revision("elsewhere edited\n"),
            "before_text": "elsewhere copy\n",
            "after_text": "elsewhere edited\n",
            "status": mr.PREPARED,
        },
    )

    result = mr.recover_pending(journal=journal)

    # The journal's own vault holds a file matching neither image, so this is a
    # conflict — and the file in the vault the row claimed is never touched.
    assert [row["id"] for row in result.conflicts] == ["mrcpt_note_moved"]
    assert other_note.read_bytes() == b"elsewhere copy\n"
    assert home_note.read_bytes() == b"home copy\n"

    # The same row, aimed at a home note that does match, settles as applied.
    _write(home, NOTE, "elsewhere edited\n")
    mr._append(
        journal,
        {
            "id": "mrcpt_note_moved_ok",
            "ts": mr._now(),
            "actor": "agent",
            "source": "cli",
            "workspace": "personal",
            "kind": "note_apply",
            "relative_path": NOTE,
            "before_revision": mr.content_revision("elsewhere copy\n"),
            "after_revision": mr.content_revision("elsewhere edited\n"),
            "before_text": "elsewhere copy\n",
            "after_text": "elsewhere edited\n",
            "status": mr.PREPARED,
        },
    )
    result = mr.recover_pending(journal=journal)
    assert [row["id"] for row in result.reconciled] == ["mrcpt_note_moved_ok"]
    assert other_note.read_bytes() == b"elsewhere copy\n"


# ── Content that must never be written ─────────────────────────────────────


def test_note_invalid_or_oversize_content_is_not_written(tmp_path):
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, "readable\n")
    # The revision a caller still holds, from before the file turned to bytes
    # no revision can be computed over.
    held = mr.content_revision("readable\n")
    readable_before = path.read_bytes()

    # A note that is not UTF-8: refused, because a revision over bytes that
    # cannot decode is not a revision at all.
    path.write_bytes(b"\xff\xfe\x00 not utf-8 \x80")
    binary_before = path.read_bytes()
    with pytest.raises(mr.MemoryReceiptError):
        _apply(vault, after="would have replaced it\n", expected=held)
    assert path.read_bytes() == binary_before

    # A body larger than a receipt can hold: refused rather than applied into a
    # row that History could show but no one could undo.
    with pytest.raises(mr.MemoryReceiptError):
        _apply(vault, after="x" * (mr.MAX_IMAGE_CHARS + 1), expected=held)
    assert path.read_bytes() == binary_before

    # And the same limit on the note as it already stands.
    path.write_bytes(b"y" * (mr.MAX_IMAGE_CHARS + 1))
    oversized_before = path.read_bytes()
    with pytest.raises(mr.MemoryReceiptError):
        _apply(vault, after="small replacement\n", expected=held)
    assert path.read_bytes() == oversized_before

    # Nothing was journaled for any of the three refusals, and the note is
    # still writable once it is back within the limit.
    assert _rows(vault) == []
    path.write_bytes(readable_before)
    assert _apply(vault, after="now it fits\n")["status"] == mr.APPLIED
    assert len(_rows(vault)) == 1


def test_note_prepare_failure_leaves_file_unchanged(tmp_path, monkeypatch):
    """No prepared row means no mutation: the crash-safety boundary is real."""
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, "original\n")
    original_bytes = path.read_bytes()

    def boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("the journal volume is full")

    monkeypatch.setattr(mr, "_append", boom)
    with pytest.raises(mr.QueueReceiptUnavailable):
        _apply(vault, after="never written\n")
    monkeypatch.undo()

    assert path.read_bytes() == original_bytes
    assert _rows(vault) == []
    assert mr.recover_pending(journal=_journal(vault)).reconciled == []


# ── Recovery ───────────────────────────────────────────────────────────────


def test_note_recovery_before_after_and_conflict(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, "before\n")
    journal = _journal(vault)
    before, after = "before\n", "after\n"

    def prepared(rid: str, **overrides: Any) -> dict[str, Any]:
        row = {
            "id": rid,
            "ts": mr._now(),
            "actor": "agent",
            "source": "cli",
            "workspace": "personal",
            "kind": "note_apply",
            "note": str(path),
            "relative_path": NOTE,
            "vault_root": str(vault),
            "before_revision": mr.content_revision(before),
            "after_revision": mr.content_revision(after),
            "before_text": before,
            "after_text": after,
            "status": mr.PREPARED,
        }
        row.update(overrides)
        return row

    # Each prepared row is reconciled against the note as it actually stands.
    for rid, on_disk, expected in (
        ("mrcpt_note_before", before, mr.ROLLED_BACK),
        ("mrcpt_note_after", after, mr.APPLIED),
        ("mrcpt_note_neither", "an unrelated hand edit\n", mr.CONFLICT),
    ):
        path.write_bytes(on_disk.encode("utf-8"))
        mr._append(journal, prepared(rid))
        result = mr.recover_pending(journal=journal)
        settled_ids = (
            [row["id"] for row in result.conflicts]
            if expected == mr.CONFLICT
            else [row["id"] for row in result.reconciled]
        )
        assert settled_ids == [rid], rid
        settled = mr.find_receipt(journal, rid)
        assert settled is not None and settled["status"] == expected, rid
        # A forward write is classified, never replayed: recovery does not
        # rewrite note content.
        assert path.read_bytes() == on_disk.encode("utf-8"), rid

    # A note that is gone is a conflict, not a silent rollback.
    path.unlink()
    mr._append(journal, prepared("mrcpt_note_missing"))
    result = mr.recover_pending(journal=journal)
    assert [row["id"] for row in result.conflicts] == ["mrcpt_note_missing"]
    assert not path.exists()

    # The write-failed case, where the note was replaced but the confirmation
    # could not be appended. A terminal `failed` row here would hide a mutation
    # that did happen; the prepared row must stay for recovery to settle.
    path.write_bytes(before.encode("utf-8"))
    real_append = mr._append

    def flaky(journal_path: Path, payload: dict[str, Any]) -> None:  # type: ignore[no-untyped-def]
        if str(payload.get("status")) == mr.APPLIED:
            raise OSError("journal momentarily unwritable")
        real_append(journal_path, payload)

    monkeypatch.setattr(mr, "_append", flaky)
    receipt = _apply(vault, after=after)
    monkeypatch.undo()

    assert path.read_bytes() == after.encode("utf-8"), "the bytes did land"
    rid = receipt["id"]
    assert receipt["status"] == mr.PREPARED
    assert mr.find_receipt(journal, rid) is not None
    row = mr.find_receipt(journal, rid)
    assert row is not None and row["status"] == mr.PREPARED

    result = mr.recover_pending(journal=journal)
    assert [settled["id"] for settled in result.reconciled] == [rid]
    assert result.conflicts == []
    settled = mr.find_receipt(journal, rid)
    assert settled is not None and settled["status"] == mr.APPLIED
    assert mr.is_undoable(settled)
    assert path.read_bytes() == after.encode("utf-8")


# ── Undo ───────────────────────────────────────────────────────────────────


def test_note_undo_refuses_later_edit(tmp_path):
    """Undo must not discard whatever landed after the write it reverses."""
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, "first\n")
    first = _apply(vault, after="second\n")

    # A hand edit.
    path.write_bytes(b"first\nsecond\nhand-written\n")
    hand_bytes = path.read_bytes()
    with pytest.raises(mr.RevisionConflict):
        mr.undo_receipt(first["id"], vault_root=vault)
    assert path.read_bytes() == hand_bytes

    # And another managed write, which is the lost-update case the revision
    # check exists for: a blind restore would drop the newer apply.
    path.write_bytes(b"first\n")
    second = _apply(vault, after="second\n")
    third = _apply(vault, after="second\nthird\n")
    with pytest.raises(mr.RevisionConflict):
        mr.undo_receipt(second["id"], vault_root=vault)
    assert path.read_bytes() == b"second\nthird\n"
    # The refused undo settled nothing: both receipts are still undoable, and
    # undoing the newest one still works.
    assert mr.is_undoable(mr.find_receipt(_journal(vault), third["id"]) or {})
    mr.undo_receipt(third["id"], vault_root=vault)
    assert path.read_bytes() == b"second\n"


def test_note_undo_that_cannot_settle_reports_that_it_landed(tmp_path, monkeypatch):
    """The reverse write is done; losing the settlement row is not a failed undo.

    An undo writes the note back and *then* marks the original ``undone``. When
    that final append fails the note is already restored and the reverse write
    is already journaled in full, so a raw ``OSError`` told the caller the undo
    had failed — and the retry it invites cannot succeed: the note no longer
    matches the after image, so the second attempt is a revision conflict raised
    against a note that is already exactly where the user asked for it.
    """
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, "first\n")
    receipt = _apply(vault, after="second\n")
    journal = _journal(vault)
    real_append = mr._append

    def flaky(journal_path: Path, payload: dict[str, Any]) -> None:
        if str(payload.get("status")) == mr.UNDONE:
            raise OSError("journal momentarily unwritable")
        real_append(journal_path, payload)

    monkeypatch.setattr(mr, "_append", flaky)
    with pytest.raises(mr.MemoryReceiptError) as failure:
        mr.undo_receipt(receipt["id"], vault_root=vault)
    monkeypatch.undo()

    assert not isinstance(failure.value, OSError), "a raw write error escaped"
    message = str(failure.value)
    assert "the undo landed" in message
    assert "unwritable" in message, "the underlying write error belongs there too"

    # The note is restored and the reverse write is journaled; only the
    # original's own settlement is missing, so history still offers an undo that
    # can only fail.
    assert path.read_bytes() == b"first\n"
    assert (mr.find_receipt(journal, receipt["id"]) or {})["status"] == mr.APPLIED
    reverse = [row for row in _rows(vault) if row["kind"] == "note_undo"]
    assert len(reverse) == 1 and reverse[0]["status"] == mr.APPLIED

    # Which is what the next recovery pass is for: it settles the original from
    # the reverse row, and does not replay a write that already landed.
    result = mr.recover_pending(journal=journal)
    assert [row["id"] for row in result.reconciled] == [receipt["id"]]
    settled = mr.find_receipt(journal, receipt["id"])
    assert settled is not None and settled["status"] == mr.UNDONE
    assert settled["undo_receipt"] == reverse[0]["id"]
    assert path.read_bytes() == b"first\n"
    assert mr.recover_pending(journal=journal).reconciled == []


def test_note_undo_refuses_a_before_image_that_does_not_hash_to_its_revision(tmp_path):
    """The reverse write replaces the note with the row's own before image.

    The revision guard proves the note still holds this operation's *after* image
    and says nothing at all about the image being written back, so a journal row
    that was edited, truncated or forged in place passes every other check and
    installs content the receipt never recorded. The image must still hash to
    the ``before_revision`` the row carries, or there is nothing trustworthy to
    restore and the note stays as it stands.
    """
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, "first\n")
    receipt = _apply(vault, after="second\n")
    journal = _journal(vault)
    # The fold takes an id's last row, so this doctored row is the receipt undo
    # reads: the revisions still describe the real write, the image does not.
    mr._append(
        journal,
        {**receipt, "before_text": "an unrelated body\n", "status": mr.APPLIED},
    )
    tampered = mr.find_receipt(journal, receipt["id"])
    assert tampered is not None
    assert tampered["before_revision"] == receipt["before_revision"]
    assert mr.is_undoable(tampered), "the row is still offered an undo"

    with pytest.raises(mr.UndoUnsupported):
        mr.undo_receipt(receipt["id"], vault_root=vault)

    # Refused before the reverse write: the note is untouched and no `note_undo`
    # row was journaled, so nothing is left half-done.
    assert path.read_bytes() == b"second\n"
    assert [row["kind"] for row in _rows(vault)] == ["note_apply"]
    assert mr.find_receipt(journal, receipt["id"])["status"] == mr.APPLIED

    # A missing image is refused the same way — which also means the hash check
    # above it must never be handed a non-string — and an intact one reverses.
    mr._append(journal, {**tampered, "before_text": None, "status": mr.APPLIED})
    missing = mr.find_receipt(journal, receipt["id"])
    assert missing is not None and missing["before_text"] is None
    with pytest.raises(mr.UndoUnsupported):
        nr.undo_note_receipt(
            missing, journal, vault_root=vault, actor="operator", source="pwa"
        )
    assert path.read_bytes() == b"second\n"
    mr._append(journal, receipt)
    mr.undo_receipt(receipt["id"], vault_root=vault)
    assert path.read_bytes() == b"first\n"


def test_note_concurrent_writers_have_one_winner(tmp_path):
    """Two writers at the same expected revision: one lands, one is refused."""
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, "base\n")
    journal = _journal(vault)
    expected = _revision(path)
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def writer(name: str, text: str) -> None:
        barrier.wait()
        try:
            _apply(vault, after=text, expected=expected)
        except mr.RevisionConflict:
            outcomes[name] = "conflict"
        except Exception as exc:  # noqa: BLE001 — surfaced by the assertion below
            outcomes[name] = f"unexpected: {exc!r}"
        else:
            outcomes[name] = "applied"

    threads = [
        threading.Thread(target=writer, args=(name, f"{name} wins\n"))
        for name in ("alpha", "beta")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert sorted(outcomes.values()) == ["applied", "conflict"], outcomes
    # The winner's bytes are the file's bytes: nothing was silently overwritten.
    assert path.read_bytes().decode("utf-8") in {"alpha wins\n", "beta wins\n"}
    rows = [row for row in _rows(vault) if row["kind"] == "note_apply"]
    assert len(rows) == 1
    assert rows[0]["status"] == mr.APPLIED
    # The loser refused before the prepared row, so recovery has nothing to do.
    assert mr.recover_pending(journal=journal).reconciled == []


# ── Interrupted undo ───────────────────────────────────────────────────────


def _undo_case(
    tmp_path: Path, name: str, *, landed: bool, reverse_status: str
) -> tuple[Path, Path, dict[str, Any]]:
    """A vault whose undo was interrupted in one of its crash windows.

    ``landed`` decides whether the reverse write's rename reached the disk;
    ``reverse_status`` decides how much of it the journal recorded. Together
    they are the three windows: before the rename, after it, and after the
    reverse row but before the original was marked undone.
    """
    vault = _vault(tmp_path, name)
    path = _write(vault, NOTE, "first\n")
    original = _apply(vault, after="second\n")
    journal = _journal(vault)
    if landed:
        path.write_bytes(b"first\n")
    mr._append(
        journal,
        {
            "id": f"mrcpt_note_undo_{name}",
            "ts": mr._now(),
            "actor": "operator",
            "source": "pwa",
            "workspace": "personal",
            "kind": "note_undo",
            "undo_of": original["id"],
            "note": str(path),
            "relative_path": NOTE,
            "before_revision": original["after_revision"],
            "after_revision": original["before_revision"],
            "before_text": original["after_text"],
            "after_text": original["before_text"],
            "status": reverse_status,
        },
    )
    return vault, path, original


def test_note_interrupted_undo_recovers_original_settlement(tmp_path):
    # (1) The undo died before its rename: nothing was reversed, so the
    #     original is untouched and still undoable.
    vault, path, original = _undo_case(
        tmp_path, "pre", landed=False, reverse_status=mr.PREPARED
    )
    result = mr.recover_pending(journal=_journal(vault))
    reverse = mr.find_receipt(_journal(vault), "mrcpt_note_undo_pre")
    assert [row["id"] for row in result.reconciled] == ["mrcpt_note_undo_pre"]
    assert reverse is not None and reverse["status"] == mr.ROLLED_BACK
    original_row = mr.find_receipt(_journal(vault), original["id"])
    assert original_row is not None and original_row["status"] == mr.APPLIED
    assert mr.is_undoable(original_row), "an un-reversed change stays undoable"
    assert path.read_bytes() == b"second\n"

    # (2) The undo died after its rename but before its own applied row: the
    #     reverse write settles applied and the original is settled with it.
    vault, path, original = _undo_case(
        tmp_path, "mid", landed=True, reverse_status=mr.PREPARED
    )
    result = mr.recover_pending(journal=_journal(vault))
    settled_ids = [row["id"] for row in result.reconciled]
    assert "mrcpt_note_undo_mid" in settled_ids
    assert original["id"] in settled_ids
    reverse = mr.find_receipt(_journal(vault), "mrcpt_note_undo_mid")
    assert reverse is not None and reverse["status"] == mr.APPLIED
    original_row = mr.find_receipt(_journal(vault), original["id"])
    assert original_row is not None and original_row["status"] == mr.UNDONE
    assert original_row["undo_receipt"] == "mrcpt_note_undo_mid"
    assert not mr.is_undoable(original_row)
    assert path.read_bytes() == b"first\n"
    rows = _rows(vault)
    assert len(rows) == 2, "one consistent result, not a second opinion"

    # (3) The undo died after its applied row but before the original's own
    #     settlement — the same stuck state, one window later, and now with a
    #     terminal reverse row for the reconcile loop to skip.
    vault, path, original = _undo_case(
        tmp_path, "post", landed=True, reverse_status=mr.APPLIED
    )
    result = mr.recover_pending(journal=_journal(vault))
    assert [row["id"] for row in result.reconciled] == [original["id"]]
    original_row = mr.find_receipt(_journal(vault), original["id"])
    assert original_row is not None and original_row["status"] == mr.UNDONE
    assert original_row["undo_receipt"] == "mrcpt_note_undo_post"
    assert path.read_bytes() == b"first\n"
    assert len(_rows(vault)) == 2

    # Every window settles once and stays settled.
    for name in ("pre", "mid", "post"):
        journal = _journal(_vault(tmp_path, name))
        assert mr.recover_pending(journal=journal).reconciled == []
        assert len(_rows(_vault(tmp_path, name))) == 2


def test_note_recovery_is_idempotent(tmp_path):
    """Repeated recovery settles once, writes nothing, and adds no receipts."""
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, "first\n")
    journal = _journal(vault)
    receipt = _apply(vault, after="second\n")

    # Rewind the journal to its prepared row, as a crash before the applied
    # row would have left it.
    prepared_lines = [
        line
        for line in journal.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["status"] == mr.PREPARED
    ]
    assert len(prepared_lines) == 1
    journal.write_text("\n".join(prepared_lines) + "\n", encoding="utf-8")
    assert (mr.find_receipt(journal, receipt["id"]) or {})["status"] == mr.PREPARED

    first = mr.recover_pending(journal=journal)
    assert [row["id"] for row in first.reconciled] == [receipt["id"]]
    assert first.conflicts == []
    settled_bytes = path.read_bytes()
    settled_journal = journal.read_text(encoding="utf-8")
    assert settled_bytes == b"second\n"
    assert len(_rows(vault)) == 1
    assert (mr.find_receipt(journal, receipt["id"]) or {})["status"] == mr.APPLIED

    for _ in range(3):
        repeat = mr.recover_pending(journal=journal)
        assert repeat.reconciled == []
        assert repeat.conflicts == []
        assert path.read_bytes() == settled_bytes
        assert journal.read_text(encoding="utf-8") == settled_journal
        assert len(_rows(vault)) == 1


# ── Review round 1: journal readers, receipt identity, mode, journal anchoring


def test_note_with_unicode_line_separators_stays_in_the_journal(tmp_path):
    """A note body holding U+2028/U+2029/U+0085 must not split its own row.

    The journal is JSON Lines and `_append` writes those characters literally
    (`ensure_ascii=False`), but `str.splitlines` treats all three as line
    breaks. Reading the journal that way cut the row in half, so the receipt
    was invisible: undo reported "unknown receipt" and a prepared note write
    could never be recovered.
    """
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, "before\n")
    journal = _journal(vault)
    separators = "line separator paragraph separator next lineend\n"
    receipt = _apply(vault, after=separators)
    assert path.read_bytes() == separators.encode("utf-8")

    # One row, and it is the receipt: the images round-tripped intact.
    found = mr.find_receipt(journal, receipt["id"])
    assert found is not None
    assert found["status"] == mr.APPLIED
    assert found["after_text"] == separators
    assert found["after_revision"] == mr.content_revision(separators)
    assert len(_rows(vault)) == 1, "the row must not be split into unreadable halves"

    # An interrupted row with the same characters is still recoverable.
    mr._append(
        journal,
        {
            "id": "mrcpt_note_separators_prepared",
            "ts": mr._now(),
            "actor": "agent",
            "source": "cli",
            "workspace": "personal",
            "kind": "note_apply",
            "relative_path": NOTE,
            "before_revision": mr.content_revision("elsewhere\n"),
            "after_revision": mr.content_revision(separators),
            "before_text": "elsewhere\n",
            "after_text": separators,
            "status": mr.PREPARED,
        },
    )
    result = mr.recover_pending(journal=journal)
    assert [row["id"] for row in result.reconciled] == [
        "mrcpt_note_separators_prepared"
    ]
    assert path.read_bytes() == separators.encode("utf-8")

    # And the undo works, so the note is reversible to the byte.
    mr.undo_receipt(receipt["id"], vault_root=vault)
    assert path.read_bytes() == b"before\n"


def test_note_receipt_id_covers_both_revisions(tmp_path):
    """A note that returns to an earlier state still gets a distinct receipt.

    The id basis carries the before revision and the path, so a note edited
    back to R0 and then edited differently from R0 produced the *same* id —
    and `read_receipts` folds by id, so the second row overwrote the first
    one's images. Both writes must remain independently listed.
    """
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, "R0\n")
    journal = _journal(vault)

    first = _apply(vault, after="R1\n")
    # Something takes the note back to R0 — a hand edit, or the undo below.
    path.write_bytes(b"R0\n")
    second = _apply(vault, after="R2\n")

    assert first["id"] != second["id"], "two edits must not collapse onto one id"
    listed = {row["id"]: row for row in _rows(vault)}
    assert set(listed) == {first["id"], second["id"]}
    assert listed[first["id"]]["after_text"] == "R1\n"
    assert listed[second["id"]]["after_text"] == "R2\n"
    assert listed[first["id"]]["before_text"] == "R0\n"
    assert listed[second["id"]]["before_text"] == "R0\n"
    # Both are still undoable in their own right.
    assert mr.is_undoable(listed[first["id"]])
    assert mr.is_undoable(listed[second["id"]])


def test_note_undo_refuses_a_journal_from_another_vault(tmp_path):
    """The reverse write and the original's settlement must share a journal."""
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, "first\n")
    receipt = _apply(vault, after="second\n")
    other = _vault(tmp_path, "other")

    with pytest.raises(mr.UndoUnsupported):
        mr.undo_receipt(receipt["id"], vault_root=other, journal=_journal(vault))

    # Refused before anything was written, in either vault.
    assert path.read_bytes() == b"second\n"
    assert not list(_vault(tmp_path, "other").rglob("*.jsonl"))
    assert (mr.find_receipt(_journal(vault), receipt["id"]) or {})["status"] == (
        mr.APPLIED
    )
    # The agreeing pair still works.
    mr.undo_receipt(receipt["id"], vault_root=vault, journal=_journal(vault))
    assert path.read_bytes() == b"first\n"


def test_note_undo_of_a_deleted_note_is_a_revision_conflict(tmp_path):
    """A note that is gone is a moved destination, not an unhandled error.

    `undo_receipt` callers handle the protocol's own exceptions; a bare
    `NoteTargetRefused` escaping an ordinary user action (delete a note, then
    press Undo) would surface as a 500 instead of a refusal.
    """
    vault = _vault(tmp_path)
    path = _write(vault, NOTE, "first\n")
    receipt = _apply(vault, after="second\n")
    path.unlink()

    with pytest.raises(mr.RevisionConflict):
        mr.undo_receipt(receipt["id"], vault_root=vault)

    # A relinked note is refused the same way: a link is not the note.
    outside = _write(tmp_path, "outside/other.md", "elsewhere\n")
    (vault / NOTE).symlink_to(outside)
    with pytest.raises(mr.RevisionConflict):
        mr.undo_receipt(receipt["id"], vault_root=vault)
    assert outside.read_bytes() == b"elsewhere\n"
