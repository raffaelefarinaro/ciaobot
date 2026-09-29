"""Revision-checked note writes: one scoped Markdown-file mutation protocol.

:mod:`ciao.memory_receipts` journals region, queue and category mutations, but
an arbitrary Markdown file has no managed write of its own, so an edit to it
could be neither recovered nor undone. This module adds exactly one such write —
a single existing ``.md`` file inside the canonical configured vault — and
nothing else. It is a foundation for the note business rules, not those rules:
there is no evidence policy, no proposal or check-state settlement, no
retirement or deletion, and no caller-facing surface here.

The protocol is the receipt protocol, narrowed to a file:

* **The caller names a vault-relative path, never a path.** Absolute paths,
  ``..`` components, symlinked files, symlinked ancestors and anything that is
  not a regular ``.md`` file are refused before a byte is read. The vault root
  is canonicalized once per operation; nothing below it is ever re-resolved, so
  a symlink planted inside the vault cannot redirect the write out of it.
* **Bytes, not text.** The note is read with ``read_bytes`` and decoded with
  strict UTF-8, and the replacement is written as bytes, so a BOM, a CRLF pair
  and a final line without a newline all survive verbatim. A revision is
  :func:`ciao.memory_receipts.content_revision` over that exact decoded text,
  so the revision a caller must present is the one it read.
* **The expected revision is mandatory.** This primitive never overwrites a
  note it has not read: an empty ``expected_revision`` is refused rather than
  treated as "anything goes".
* **Never view-only.** A note whose image would exceed
  :data:`ciao.memory_receipts.MAX_IMAGE_CHARS`, or that is not valid UTF-8, is
  refused before the mutation. An image the journal cannot hold would leave an
  ``applied`` row that History can read but not undo, which is the one outcome
  this protocol exists to prevent.
* **Prepare, write, confirm, under one lock.** The per-file
  :func:`ciao.memory_receipts.queue_lock` covers the read, the revision check,
  the ``prepared`` row, the sibling-temp write and the ``os.replace``, and the
  revision is rechecked immediately before the rename. A journal that cannot
  record the ``prepared`` row aborts the write: no evidence, no mutation.
* **Honest settlement.** A write that never reached the rename is
  ``rolled_back`` only while the note still matches its before image; otherwise
  the ``prepared`` row is left for recovery rather than given a verdict the
  bytes do not support. A crash after the rename but before the ``applied`` row
  leaves a recoverable ``prepared`` row and never a terminal ``failed`` one,
  which would hide a mutation that did happen.

Recovery (:func:`reconcile_note_receipt`) never mutates note content while
reconciling a forward write: it compares the note's current revision under the
same lock and classifies it ``applied`` (matches the after image),
``rolled_back`` (matches the before image) or ``conflict`` (anything else,
including a note that is gone). Undo
(:func:`undo_note_receipt`) is the one reverse write, and it refuses unless the
note still matches the after image. Both resolve the target against the vault
derived from the *journal's own location*, never the vault a receipt claims, so
a forged or moved row cannot write outside the vault it was found in.

An undo settles the original receipt only after its own reverse write is
journaled, which leaves a window an interrupted undo can fall into. One
idempotent pass (:func:`settle_open_undo_links`) closes it on the next
recovery. Undo is not offered for undo: ``note_undo`` is not in
:data:`ciao.memory_receipts.UNDOABLE_KINDS` and its row carries ``undo_of``,
so a reverse write is a settled record rather than a second lever.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from ciao import memory_receipts as mr
from ciao.vault_index import temp_prefix

logger = logging.getLogger(__name__)

NOTE_APPLY = "note_apply"
NOTE_UNDO = "note_undo"

# The kinds this protocol owns. `memory_receipts` imports this module lazily to
# route them, so the pair of modules does not import in a cycle.
NOTE_KINDS = frozenset({NOTE_APPLY, NOTE_UNDO})


class NoteTargetRefused(mr.MemoryReceiptError):
    """The named note is not a writable Markdown file inside the vault.

    Raised before anything is read or written, for a path that is absolute,
    traverses out of the vault, names a symlink or a non-file, does not exist,
    or is not Markdown. A receipt that cannot be trusted to name such a file is
    reconciled as a conflict rather than resolved against whatever the path
    happens to point at now.
    """


# ── Path confinement ───────────────────────────────────────────────────────


def canonical_vault(vault_root: Path | str) -> Path:
    """The vault root, resolved once.

    Only the root is canonicalized. Resolving the note path would follow a
    symlink planted inside the vault and hand the write to whatever it points
    at, so :func:`resolve_note_path` walks the components instead and refuses
    any link it finds.
    """
    root = Path(vault_root).resolve()
    if not root.is_dir():
        raise NoteTargetRefused(f"vault root is not a directory: {vault_root}")
    return root


def vault_for_journal(journal: Path) -> Path | None:
    """The vault a receipt journal belongs to, derived from its own location.

    The journal is the one anchor in a recovered row that nobody wrote: it is
    named by the install's configuration, not by the receipt. The vault is
    therefore read off the journal's own path (``<vault>/Workspace/<journal>``)
    rather than off the ``vault_root`` field, which any row — including a
    forged or moved one — is free to claim. ``None`` when the journal is not in
    a vault's ``Workspace`` folder, i.e. when there is nothing to confine to.
    """
    parent = journal.parent
    if journal.name != mr.RECEIPTS_NAME or parent.name != "Workspace":
        return None
    root = parent.parent
    try:
        resolved = root.resolve()
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def resolve_note_path(vault_root: Path | str, relative_path: str) -> Path:
    """The canonical file one vault-relative path names, or a refusal.

    The path is a ``/``-separated path *inside* the vault. Empty, absolute and
    ``..``-bearing spellings are refused, as is any component that is a symlink
    (the file itself or any directory on the way to it), anything that is not a
    regular file, and anything that is not ``.md``.
    """
    root = canonical_vault(vault_root)
    raw = str(relative_path or "").strip()
    if not raw:
        raise NoteTargetRefused("a note path is required")
    candidate = Path(raw)
    if candidate.is_absolute() or raw.startswith(("/", "\\")) or candidate.drive:
        raise NoteTargetRefused(f"a note path must be relative to the vault: {raw}")
    parts = [part for part in candidate.parts if part not in ("", ".")]
    if not parts:
        raise NoteTargetRefused("a note path is required")
    if any(part == ".." for part in parts):
        raise NoteTargetRefused(
            f"a note path may not traverse out of the vault: {raw}"
        )
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise NoteTargetRefused(
                f"{current} is a symlink; a note write follows no link"
            )
    target = root.joinpath(*parts)
    if not target.exists():
        raise NoteTargetRefused(f"no such note in this vault: {raw}")
    if not target.is_file():
        raise NoteTargetRefused(f"not a regular file: {raw}")
    if target.suffix.lower() != ".md":
        raise NoteTargetRefused(f"only Markdown notes are writable here: {raw}")
    # Belt and braces: the component walk above already refuses every link, so
    # a resolved path that still left the vault would mean that walk was wrong.
    if not target.is_relative_to(root):
        raise NoteTargetRefused(f"a note path may not leave the vault: {raw}")
    return target


# ── Reading ────────────────────────────────────────────────────────────────


def _read_note_text(target: Path) -> str:
    """The note's exact decoded text, with no newline normalization.

    ``read_text`` translates line endings on read and on write, which would
    rewrite a CRLF note as it read it back. Reading bytes keeps the BOM, the
    CRLF pairs and a missing final newline exactly as they are, so a round trip
    through this protocol restores the file byte for byte.
    """
    data = target.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise mr.MemoryReceiptError(f"{target} is not valid UTF-8: {exc}") from exc


def _current_revision(target: Path) -> str:
    """The note's revision now, or ``""`` when it cannot be read as UTF-8.

    Used only where "did my before image survive?" is the question, so an
    unreadable note must read as "not what I saw" rather than raise: the
    caller's honest answer is that it no longer knows.
    """
    try:
        return mr.content_revision(_read_note_text(target))
    except (OSError, mr.MemoryReceiptError):
        return ""


def _refuse_oversize(text: str, what: str) -> None:
    """Refuse an image the journal could not keep in full.

    The region images are cap-bounded, so this only ever trips on a hand-written
    note. Refusing is the point: an image the journal cannot hold produces an
    ``applied`` row that History can show but not undo, and this protocol must
    never manufacture a view-only write.
    """
    if len(text) > mr.MAX_IMAGE_CHARS:
        raise mr.MemoryReceiptError(
            f"{what} is {len(text)} characters, over the "
            f"{mr.MAX_IMAGE_CHARS} a receipt can hold; nothing was written"
        )


# ── Writing ────────────────────────────────────────────────────────────────


def _fsync_dir(directory: Path) -> None:
    """Flush the directory entry, so a completed rename survives a crash."""
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def _replace_note_bytes(
    target: Path, payload: bytes, before_revision: str
) -> None:
    """Replace one note's bytes, atomically, keeping its mode.

    A sibling temp file plus ``os.replace`` means a reader — or a crash — sees
    either the whole old note or the whole new one. The mode is carried across
    explicitly because ``mkstemp`` creates the temp file 0600; without the
    ``chmod`` every managed write would quietly make a note private. The
    revision is rechecked immediately before the rename: the lock only
    excludes other *managed* writers, and a direct edit that lands in the gap
    would otherwise be silently overwritten.
    """
    mode = stat.S_IMODE(target.stat().st_mode)
    fd, raw_name = tempfile.mkstemp(
        prefix=temp_prefix(target.name), suffix=".tmp", dir=str(target.parent)
    )
    temporary = Path(raw_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if _current_revision(target) != before_revision:
            raise mr.RevisionConflict(
                "the note changed while the write was being prepared; "
                "nothing was written"
            )
        os.replace(temporary, target)
        os.chmod(target, mode)
        _fsync_dir(target.parent)
    finally:
        # The rename consumed the temp file; any other failure leaves it behind.
        try:
            temporary.unlink()
        except OSError:
            pass


# ── The mutation ───────────────────────────────────────────────────────────


def commit_note_change(
    *,
    vault_root: Path,
    relative_path: str,
    expected_revision: str,
    after_text: str,
    actor: str,
    source: str,
    workspace: str = "",
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replace one vault note's text, journaled, revision-checked and undoable.

    ``relative_path`` names an existing regular ``.md`` file inside
    ``vault_root``; ``expected_revision`` is the
    :func:`ciao.memory_receipts.content_revision` of the exact text the caller
    read. A stale expectation raises :class:`ciao.memory_receipts.RevisionConflict`
    with the note untouched, which is how a concurrent managed write or a
    direct edit is reported rather than overwritten.

    Returns the effective receipt. ``changed`` is ``False`` — and no file is
    replaced — when ``after_text`` already is the note's text; the row is still
    written, so a caller can tell "already satisfied" from "wrote something".

    Raises :class:`NoteTargetRefused` for a path outside the vault, and
    :class:`ciao.memory_receipts.MemoryReceiptError` for a note that is not
    valid UTF-8 or whose image is larger than a receipt can hold. Both refusals
    happen before any byte is written, so this primitive never produces a
    view-only write.
    """
    return _commit_note_change(
        root=canonical_vault(vault_root),
        relative_path=relative_path,
        expected_revision=expected_revision,
        after_text=after_text,
        actor=actor,
        source=source,
        workspace=workspace,
        provenance=provenance,
        kind=NOTE_APPLY,
        undo_of="",
    )


def _commit_note_change(
    *,
    root: Path,
    relative_path: str,
    expected_revision: str,
    after_text: str,
    actor: str,
    source: str,
    workspace: str,
    provenance: dict[str, Any] | None,
    kind: str,
    undo_of: str,
) -> dict[str, Any]:
    """The shared body of the forward and the reverse write.

    Split out so :func:`undo_note_receipt` can journal its reverse write through
    exactly the same prepare/write/confirm path under a lock it already holds,
    rather than a second, weaker implementation.
    """
    target = resolve_note_path(root, relative_path)
    stored_path = target.relative_to(root).as_posix()
    expected = str(expected_revision or "").strip()
    if not expected:
        # A caller that cannot say what it read is asking for a blind
        # overwrite, which is the one thing this protocol exists to prevent.
        raise mr.MemoryReceiptError(
            "an expected revision is required; this primitive never overwrites "
            "a note it has not read"
        )
    try:
        payload = after_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise mr.MemoryReceiptError(
            f"the proposed note body is not encodable as UTF-8: {exc}"
        ) from exc
    _refuse_oversize(after_text, "the proposed note body")

    journal = mr.journal_path(root, None)
    with mr.queue_lock(target):
        before_text = _read_note_text(target)
        _refuse_oversize(before_text, "the note as it stands")
        before_revision = mr.content_revision(before_text)
        if before_revision != expected:
            raise mr.RevisionConflict(
                "the note changed since this operation was planned; nothing was written"
            )
        after_revision = mr.content_revision(after_text)
        base: dict[str, Any] = {
            "id": mr.new_receipt_id(
                f"{stored_path}|{kind}|{before_revision}|{undo_of}", journal
            ),
            "ts": mr._now(),
            "actor": actor,
            "source": source,
            "workspace": workspace,
            "kind": kind,
            # Informational only. Recovery and undo resolve the target from the
            # journal's own vault, never from this.
            "note": str(target),
            "relative_path": stored_path,
            "vault_root": str(root),
            "expected_revision": expected,
            "before_revision": before_revision,
            "after_revision": after_revision,
            "before_text": before_text,
            "after_text": after_text,
            "provenance": dict(provenance or {}),
        }
        if undo_of:
            base["undo_of"] = undo_of

        if before_text == after_text:
            receipt = {**base, "status": mr.APPLIED, "changed": False}
            mr._append(journal, receipt)
            return receipt

        prepared = {**base, "status": mr.PREPARED, "changed": True}
        try:
            mr._append(journal, prepared)
        except Exception as exc:  # noqa: BLE001 — no evidence, no mutation
            logger.error(
                "memory receipts: could not prepare note receipt for %s: %s",
                stored_path,
                exc,
            )
            raise mr.QueueReceiptUnavailable(
                "the receipt journal could not record a prepared row"
            ) from exc

        try:
            _replace_note_bytes(target, payload, before_revision)
        except mr.RevisionConflict as exc:
            # The note moved after the prepared row went down and before the
            # rename, so this write did not land and the note is now neither
            # image. A conflict is the honest verdict; rolling back would claim
            # a before image that is no longer there.
            _settle_quietly(journal, prepared, mr.CONFLICT, str(exc))
            raise
        except Exception as exc:  # noqa: BLE001 — record the failure, then surface it
            _settle_failed_write(journal, prepared, target, exc)
            raise

        applied = {**base, "status": mr.APPLIED, "changed": True}
        try:
            mr._append(journal, applied)
        except Exception:  # noqa: BLE001 — the bytes are already replaced
            # The mutation happened. A terminal `failed` row here would hide it
            # from History and from recovery, so the prepared row is left for the
            # next recovery pass to settle as `applied` from the note itself.
            logger.error(
                "memory receipts: note %s was replaced but its applied row could "
                "not be appended; leaving it recoverable",
                stored_path,
                exc_info=True,
            )
            return {**base, "status": mr.PREPARED, "changed": True}
        return applied


def _settle_quietly(
    journal: Path, row: dict[str, Any], status: str, detail: str
) -> None:
    """Settle a row, never letting the settlement hide the original failure."""
    try:
        mr._append(journal, {**row, "status": status, "detail": detail})
    except Exception:  # noqa: BLE001
        logger.debug("memory receipts: could not settle note row", exc_info=True)


def _settle_failed_write(
    journal: Path,
    prepared: dict[str, Any],
    target: Path,
    error: Exception,
) -> None:
    """Settle a write that never reached the rename, as honestly as the bytes allow.

    ``rolled_back`` is a claim that the note is still exactly what this
    operation read. That is checkable, so it is checked; when it is not true the
    prepared row is left non-terminal, because recovery can still tell the
    difference and a verdict written here could not.
    """
    detail = f"the note was not replaced: {error}"
    if _current_revision(target) == str(prepared.get("before_revision", "")):
        _settle_quietly(journal, prepared, mr.ROLLED_BACK, detail)
        return
    logger.error(
        "memory receipts: note write failed and the note no longer matches its "
        "before image; leaving %s recoverable",
        prepared.get("id"),
        exc_info=True,
    )


# ── Recovery ───────────────────────────────────────────────────────────────


def reconcile_note_receipt(
    receipt: dict[str, Any], journal: Path
) -> dict[str, Any] | None:
    """Classify one interrupted note receipt against the note on disk.

    The target is resolved against the journal's own vault, so a receipt that
    was forged, or that was written before the vault moved, is reconciled
    against the vault it is actually found in — and a path that is not a
    writable note in that vault is a conflict, never a write.

    Under the same lock the apply took, the note's current revision decides:
    the after image means the crash landed after the rename, the before image
    means the write never happened, and anything else — including a note that is
    gone or no longer decodes — is a conflict left for a human. A forward write
    is only ever *classified* here: recovery never changes note content.
    """
    if str(receipt.get("kind", "")) not in NOTE_KINDS:
        return None
    root = vault_for_journal(journal)
    if root is None:
        return mr._settle(
            journal,
            receipt,
            mr.CONFLICT,
            "the journal is not inside a vault, so the note target cannot be resolved",
        )
    try:
        target = resolve_note_path(root, str(receipt.get("relative_path", "")))
    except mr.MemoryReceiptError as exc:
        return mr._settle(
            journal, receipt, mr.CONFLICT, f"the recorded target is unusable: {exc}"
        )
    status, detail = _classify_note(target, receipt)
    return mr._settle(journal, receipt, status, detail)


def _classify_note(target: Path, receipt: dict[str, Any]) -> tuple[str, str]:
    """The verdict one note's current state supports, read under its lock."""
    with mr.queue_lock(target):
        try:
            current = mr.content_revision(_read_note_text(target))
        except (OSError, mr.MemoryReceiptError) as exc:
            return mr.CONFLICT, f"the note is unreadable: {exc}"
        if current == str(receipt.get("after_revision", "")):
            return mr.APPLIED, "recovered after crash"
        if current == str(receipt.get("before_revision", "")):
            return mr.ROLLED_BACK, "the write never landed"
    return mr.CONFLICT, "the note changed while the operation was interrupted"


def settle_open_undo_links(journal: Path) -> list[dict[str, Any]]:
    """Settle originals whose undo landed but whose own settlement did not.

    An undo writes the note back and *then* marks the original ``undone``, so a
    crash in that window leaves a journaled, applied reverse write attached to
    an original that still reads as ``applied`` — and therefore still offers an
    undo that can only fail, against a note that is already restored. This pass
    closes that window.

    Idempotent, and safe to run on a journal with no note receipts: an original
    that is already ``undone``, is not ``applied``, or is absent is left alone.
    """
    rows = mr.read_receipts(journal)
    originals = {str(row.get("id", "")): row for row in rows}
    settled: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("kind", "")) not in NOTE_KINDS:
            continue
        if str(row.get("status", "")) != mr.APPLIED:
            continue
        original_id = str(row.get("undo_of", ""))
        original = originals.get(original_id)
        if original is None or str(original.get("status", "")) != mr.APPLIED:
            continue
        undone = {
            **{k: v for k, v in original.items() if k != "v"},
            "status": mr.UNDONE,
            "undo_of": original_id,
            "undo_receipt": str(row.get("id", "")),
            "settled_at": mr._now(),
        }
        try:
            mr._append(journal, undone)
        except Exception:  # noqa: BLE001 — the next pass retries
            logger.debug("memory receipts: could not settle undo link", exc_info=True)
            continue
        settled.append(undone)
    return settled


# ── Undo ───────────────────────────────────────────────────────────────────


def undo_note_receipt(
    receipt: dict[str, Any],
    journal: Path,
    *,
    vault_root: Path | None,
    actor: str,
    source: str,
) -> dict[str, Any]:
    """Put one note back to the bytes its ``note_apply`` receipt replaced.

    The note is re-read under the per-file lock and must still match the
    receipt's after image. A note that moved since is a
    :class:`ciao.memory_receipts.RevisionConflict`, not an undo: restoring the
    before image would discard whatever landed in between, whether that was
    another managed write or the user's own hand.

    The reverse write goes through the same journaled protocol as a forward one
    — ``prepared``, replace, confirm — as a ``note_undo`` row carrying
    ``undo_of``. That row is what makes an interrupted undo recoverable: if the
    process dies before the replace, it settles ``rolled_back`` and the original
    stays undoable; if it dies after, it settles ``applied`` and
    :func:`settle_open_undo_links` finishes the original's settlement. Only once
    the reverse write is journaled does the original become ``undone``.

    Returns the original's ``undone`` row, which carries ``undo_receipt`` for
    the reverse write.
    """
    if str(receipt.get("kind", "")) != NOTE_APPLY:
        raise mr.UndoUnsupported("only a note apply can be undone")
    before = receipt.get("before_text")
    after = receipt.get("after_text")
    if not isinstance(before, str) or not isinstance(after, str):
        raise mr.UndoUnsupported("this receipt carries no complete note image")
    root = (
        canonical_vault(vault_root)
        if vault_root is not None
        else vault_for_journal(journal)
    )
    if root is None:
        raise mr.UndoUnsupported(
            "the vault this receipt belongs to could not be resolved"
        )
    target = resolve_note_path(root, str(receipt.get("relative_path", "")))
    with mr.queue_lock(target):
        try:
            current = mr.content_revision(_read_note_text(target))
        except (OSError, mr.MemoryReceiptError) as exc:
            raise mr.RevisionConflict(
                f"the note is unreadable, so the change cannot be reversed: {exc}"
            ) from exc
        if current != str(receipt.get("after_revision", "")):
            raise mr.RevisionConflict(
                "the note changed after this operation; undo was refused"
            )
        # Re-entrant: this thread already holds the lock, so the reverse write
        # is serialized against the same competitors as the check above. Its
        # returned id is the linkage the original's settlement points at, and is
        # valid even when the row is still `prepared` — a reverse write that
        # cannot be confirmed is recoverable, and the next pass settles it.
        reverse = _commit_note_change(
            root=root,
            relative_path=str(receipt.get("relative_path", "")),
            expected_revision=str(receipt.get("after_revision", "")),
            after_text=before,
            actor=actor,
            source=source,
            workspace=str(receipt.get("workspace", "")),
            provenance={"undo_of": str(receipt.get("id", ""))},
            kind=NOTE_UNDO,
            undo_of=str(receipt.get("id", "")),
        )
    undone = {
        **{k: v for k, v in receipt.items() if k != "v"},
        "status": mr.UNDONE,
        "undo_of": str(receipt.get("id", "")),
        "undo_receipt": str(reverse.get("id", "")),
        "settled_at": mr._now(),
    }
    mr._append(journal, undone)
    return undone
