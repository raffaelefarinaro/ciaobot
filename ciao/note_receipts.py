"""Revision-checked note writes: one scoped Markdown-file mutation protocol.

:mod:`ciao.memory_receipts` journals region, queue and category mutations, but
an arbitrary Markdown file has no managed write of its own, so an edit to it
could be neither recovered nor undone. This module adds exactly one such write —
a single existing ``.md`` file inside the canonical configured vault — and
nothing else. It is a foundation for the note business rules, not those rules:
there is no evidence policy, no proposal or check-state settlement, no
retirement or deletion, and no caller-facing surface here.

**The transaction is whole-note; a range is a composition.** There is no range
parameter on :func:`commit_note_change`, and there is not going to be one. A
receipt keeps a full before image and a full after image precisely so an undo
can put a file back without knowing anything about how it changed, and a
partial-image receipt would make that impossible for the one operation most
likely to need it. :func:`apply_entry_edit` is therefore a *composer*, not a
second protocol: it re-reads the note under the note's own lock, resolves one
entry by :func:`ciao.note_entries.entry_identity`, confirms its
:func:`ciao.note_entries.refresh_fingerprint` still matches what the caller
judged, and splices exactly that entry's span into an ``after_text`` — which is
then handed to :func:`commit_note_change` unchanged. The journal, the revision
check, the atomic replace and undo are the same ones a whole-note write gets.

The protocol is the receipt protocol, narrowed to a file:

* **The caller names a vault-relative path, never a path.** Absolute paths,
  ``..`` components, symlinked files, symlinked ancestors and anything that is
  not a regular ``.md`` file are refused before a byte is read, as is the app's
  own bookkeeping under ``Workspace/``. The vault root is canonicalized once per
  operation; nothing below it is ever re-resolved, so a symlink planted inside
  the vault cannot redirect the write out of it. Each component is spelled the
  way the vault has it, so both spellings of a name on a case-insensitive
  filesystem resolve to one file — one lock, one recorded path — instead of two
  writers reaching one note without serializing against each other.
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
recovery — by *settling the original*, not by replaying the undo, which either
already landed and is recorded in full or never did and is settled
``rolled_back``. Undo is not offered for undo: ``note_undo`` is not in
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
from ciao import note_entries as ne
from ciao.vault_index import is_reserved_bookkeeping, temp_prefix

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


def _on_disk_name(parent: Path, part: str) -> Path:
    """The entry under *parent* that *part* names, spelled the way it is on disk.

    A case-insensitive filesystem resolves ``Notes/A.md`` and ``notes/a.md`` to
    one file while the two strings stay different, and everything keyed off the
    string then splits in two: the per-file lock (keyed by the resolved path),
    the ``relative_path`` a receipt records, the key a verification check is
    filed under. A second writer reaching the same note through the other
    spelling takes a different lock and both revisions compare equal, so one
    write silently lands on top of the other. The directory listing is the only
    place the real spelling exists, so the walk reads it and takes that.

    An exact match wins, so this is a no-op on a case-sensitive filesystem
    (where the caller already spelled the name correctly). A component the
    *caller's own spelling does not resolve* is returned unchanged, so it
    reaches the walk's "no such note" refusal below: canonicalizing a name that
    is not there would answer a different question, and on a case-sensitive
    filesystem the two spellings are different paths — asking for
    ``notes/a.md`` with only ``Notes/A.md`` on disk would write a file nobody
    named. So the case-insensitive match runs only where the filesystem itself
    already resolved the caller's spelling, which is exactly the
    case-insensitive case. The single entry matching case-insensitively is then
    used, and a *tie* — two entries differing only in case under a filesystem
    that resolved one spelling to a file — is refused rather than guessed at: a
    name the caller cannot pick unambiguously is not a name this protocol
    should write through. A component that is not there at all, and an
    unreadable directory, are reported the same way they always were.
    """
    try:
        with os.scandir(parent) as entries:
            names = {entry.name for entry in entries}
    except OSError:
        return parent / part
    if part in names:
        return parent / part
    if not (parent / part).exists():
        return parent / part
    folded = part.casefold()
    matches = sorted(name for name in names if name.casefold() == folded)
    if not matches:
        return parent / part
    if len(matches) > 1:
        raise NoteTargetRefused(
            f"{parent} holds more than one entry named {part} up to case "
            f"({', '.join(matches)}); a note write must name one file"
        )
    return parent / matches[0]


def resolve_note_path(vault_root: Path | str, relative_path: str) -> Path:
    """The canonical file one vault-relative path names, or a refusal.

    The path is a ``/``-separated path *inside* the vault. Empty, absolute and
    ``..``-bearing spellings are refused, as is any component that is a symlink
    (the file itself or any directory on the way to it), anything that is not a
    regular file, and anything that is not ``.md``.

    The returned path is spelled the way the vault has it, so both spellings of
    a case-insensitive name resolve to one file, one lock and one recorded
    ``relative_path`` (see :func:`_on_disk_name`).

    The app's own bookkeeping under ``Workspace/`` is refused as a target: the
    proposal queue, the curation logs and their neighbours are managed by the
    pipelines that own them, each with its own atomic write, and a note write
    bypassing those would be an ordinary-looking undo of a file the user never
    edited.
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
        current = _on_disk_name(current, part)
        if current.is_symlink():
            raise NoteTargetRefused(
                f"{current} is a symlink; a note write follows no link"
            )
    target = current
    if not target.exists():
        raise NoteTargetRefused(f"no such note in this vault: {raw}")
    if not target.is_file():
        raise NoteTargetRefused(f"not a regular file: {raw}")
    if target.suffix.lower() != ".md":
        raise NoteTargetRefused(f"only Markdown notes are writable here: {raw}")
    # Belt and braces: the component walk above already refuses every link, so
    # a resolved path that still left the vault would mean that walk was wrong.
    # Checked before the first `relative_to`, which would raise a bare
    # `ValueError` for exactly that regression — a 500 for an ordinary user
    # action, where every other way of naming the wrong file is a refusal.
    if not target.is_relative_to(root):
        raise NoteTargetRefused(f"a note path may not leave the vault: {raw}")
    if is_reserved_bookkeeping(target.relative_to(root)):
        raise NoteTargetRefused(
            f"{target.name} is the app's own vault bookkeeping under "
            f"{target.parent.name}/, not a note: it is written by the pipeline "
            f"that owns it, not by a note edit"
        )
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
    with ``os.fchmod`` on the temp file's own descriptor, before the rename:
    ``mkstemp`` creates it 0600, and restoring the mode *after* the replace
    would leave the live note briefly 0600 — a private note for as long as the
    process takes to chmod it — and would raise for a write that had already
    landed. The revision is rechecked immediately before the rename: the lock
    only excludes other *managed* writers, and a direct edit that lands in the
    gap would otherwise be silently overwritten.
    """
    mode = stat.S_IMODE(target.stat().st_mode)
    fd, raw_name = tempfile.mkstemp(
        prefix=temp_prefix(target.name), suffix=".tmp", dir=str(target.parent)
    )
    temporary = Path(raw_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if _current_revision(target) != before_revision:
            raise mr.RevisionConflict(
                "the note changed while the write was being prepared; "
                "nothing was written"
            )
        os.replace(temporary, target)
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
    root = canonical_vault(vault_root)
    return _commit_note_change(
        root=root,
        relative_path=relative_path,
        expected_revision=expected_revision,
        after_text=after_text,
        actor=actor,
        source=source,
        workspace=workspace,
        provenance=provenance,
        kind=NOTE_APPLY,
        undo_of="",
        journal=mr.journal_path(root, None),
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
    journal: Path,
) -> dict[str, Any]:
    """The shared body of the forward and the reverse write.

    Split out so :func:`undo_note_receipt` can journal its reverse write through
    exactly the same prepare/write/confirm path under a lock it already holds,
    rather than a second, weaker implementation.

    ``journal`` is a parameter rather than derived from ``root`` because the
    caller already holds one. An undo appends its ``undone`` row to the journal
    it was handed, so a reverse write that derived its own would leave the
    reverse row and the original's settlement in two different journals — a
    receipt no single recovery pass could ever finish.
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
            # The after revision is part of the identity, not just the record.
            # With only the before revision, a note that returned to an earlier
            # state (an undo, or a hand edit back) made the next, different
            # edit from that state collide on one id — and `read_receipts`
            # folds by id, so the new row silently overwrote the earlier
            # one's images. Both endpoints of the write belong in its identity.
            "id": mr.new_receipt_id(
                f"{stored_path}|{kind}|{before_revision}|{after_revision}|{undo_of}",
                journal,
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
            # Nothing was replaced, so there is no change to reverse: the
            # before image is the after image and restoring it would write the
            # note's own bytes back over themselves. `undoable=False` is what
            # keeps History from offering an undo that can only be a no-op.
            receipt = {
                **base,
                "status": mr.APPLIED,
                "changed": False,
                "undoable": False,
            }
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


# ── Managed entry-range edits ──────────────────────────────────────────────
#
# One list item is the smallest thing a person maintains in a note, and it is
# the smallest thing a verification can act on: a whole-note write to change one
# fact rewrites every other fact in the file, and a whole-file retirement to
# drop one fact takes the rest with it. So there is a managed edit for one entry's
# exact span — and it is a *composition*, not a second transaction.


def find_entry(
    text: str,
    *,
    identity: str,
    note_path: str = "",
    workspace: str = "",
) -> ne.NoteEntry | None:
    """The entry in *text* carrying this identity, or ``None``.

    ``note_path`` and ``workspace`` are load-bearing rather than cosmetic: they
    are two of the six inputs :func:`ciao.note_entries.entry_identity` digests, so
    an identity minted for one note resolves to nothing in another. A caller that
    omitted them would be asking a question whose answer is always "no such
    entry", and the failure that produces — a conflict, or a "the entry is gone"
    refusal — is the right answer for a caller that got it wrong, which is why
    there is no default that guesses: a caller with no coordinates to hand is a
    caller that cannot resolve anything, and saying so is better than returning
    an entry the caller did not name.

    The fingerprint is *not* checked here. This answers "is this entry in this
    text"; whether it is the entry a caller judged is
    :func:`apply_entry_edit`'s question, and it is a different one.
    """
    wanted = str(identity or "").strip()
    if not wanted:
        return None
    document = ne.parse_note_entries(
        text, note_path=note_path, workspace=workspace
    )
    for entry in document.entries:
        if entry.identity == wanted:
            return entry
    return None


def compose_entry_edit(
    text: str, entry: ne.NoteEntry, *, replacement: str | None = None, delete: bool = False
) -> tuple[str, str]:
    """The note's exact text after one entry edit, and why it cannot be composed.

    The whole edit is ``text[:entry.start] + replacement + text[entry.end:]``, and
    the rest of the note is not touched: a BOM, a CRLF pair, a trailing space and
    a note's last line without a newline all come back exactly as they were. That
    is the whole point of splicing a span rather than rewriting a document, and
    the reason the receipt journal is handed a *full* after image to store even
    though the write moved a few dozen characters.

    A delete takes the entry's terminating newline with it. A list item's line is
    what delimits it; leaving the newline behind would turn every removed bullet
    into a blank line, and a note that has been emptied of facts would read as
    full of gaps. Both newline spellings are taken whole, so a CRLF note loses its
    ``\\r\\n`` pair rather than a lone ``\\n`` and keeps a blank line where the
    bullet was.

    Deleting a *parent* bullet does not take its children. The parser is flat — a
    nested bullet is an entry of its own (:func:`ciao.note_entries.parse_note_entries`
    reports one entry per line, indentation and all) — so removing the parent leaves
    its children in place as re-indented orphans rather than as a subtree the
    deletion could have removed with it. That is the honest reading of "delete this
    list item", and a caller who means the whole subtree retires the children first.

    A replacement must be *one* list item and must be nothing else. A bullet
    carrying several assertions is one entry by
    :mod:`ciao.note_entries`'s contract, so an edit that splices prose in where a
    bullet was, or several bullets in where one was, is not a change to one fact
    and is refused rather than written. The check goes through the same parser the
    entry came from, so the two cannot disagree about what a list item is, and it
    requires that parsed item to cover the *whole* replacement: a trailing newline
    (which splices a blank line in), a paragraph, or a heading after the bullet is
    text the caller did not ask to write. The item's own indentation must match the
    entry's, because a splice that re-indents a bullet silently re-parents it under
    the bullet above it.

    A re-stamp is not a separate case here: it is a replacement whose
    :func:`ciao.note_entries.refresh_fingerprint` is unchanged, which is exactly
    what :func:`ciao.entry_verification.stamp_entry` produces. Splicing the
    opening line alone would be byte-identical for that call, and strictly more
    dangerous for every other one: a caller who passed just the new opening line
    for a multi-line entry would silently lose the entry's continuation lines,
    which no fingerprint check runs before the write.

    Returns ``("", reason)`` for a refusal, so a caller never writes the empty
    string it would get from a failed composition.
    """
    if delete and replacement is not None:
        return "", (
            "an entry edit either replaces the entry or deletes it; both were asked "
            "for, so nothing was composed"
        )
    if delete:
        return text[: entry.start] + _after_entry_line(text, entry.end), ""
    if replacement is None:
        return "", (
            "an entry edit needs the entry's exact replacement text, or delete=True "
            "to remove it; neither was given"
        )
    if not str(replacement).strip():
        # An empty replacement is a deletion wearing a different name, and the
        # only reason to spell it this way would be to reach one without the
        # delete's accounting. Refused, and the refusal says what to do instead.
        return "", (
            "the replacement text is empty, which would delete the entry; pass "
            "delete=True so the removal is composed as one"
        )
    if not (0 <= entry.start <= entry.end <= len(text)) or text[
        entry.start : entry.end
    ] != entry.text:
        return "", (
            "the entry's span no longer matches the note's text, so the splice "
            "would corrupt a different part of the file"
        )
    document = ne.parse_note_entries(
        str(replacement), note_path=entry.note_path, workspace=entry.workspace
    )
    if len(document.entries) != 1 or document.entries[0].start != 0:
        return "", (
            f"the replacement text holds {len(document.entries)} list items rather "
            "than one, so it is not an edit to a single fact; nothing was composed"
        )
    only = document.entries[0]
    if only.end != len(replacement) or document.uncovered:
        # The item parsed, and something beside it did not. A trailing newline
        # splices a blank line into the note; a paragraph or a heading splices
        # content the caller never named. Either way the replacement is not one
        # list item, it is one list item *and* something else.
        return "", (
            f"the replacement text holds {len(replacement) - only.end} character(s) "
            "beside its one list item — a trailing newline, a paragraph or a heading "
            "is not part of the entry being replaced; nothing was composed"
        )
    if only.indent != entry.indent:
        return "", (
            f"the replacement is indented {only.indent} columns and the entry it "
            f"replaces is indented {entry.indent}, so the splice would re-parent "
            "the bullet under the one above it; nothing was composed"
        )
    return text[: entry.start] + str(replacement) + text[entry.end :], ""


def _after_entry_line(text: str, end: int) -> str:
    """``text`` from *end* onwards, with the list item's own line ending removed.

    A list item is delimited by its line, so a delete has to take the terminator
    with it — both bytes of a CRLF pair, not the ``\\n`` alone. A note's last item
    may have no terminator at all, and then there is nothing to consume and the
    tail is returned unchanged.

    Public because :func:`ciao.note_edit_proposals.entry_replacement` inverts the
    same splice: it has to know which bytes the delete consumed to recognise the
    after image this function produced, or the two disagree about a first-line
    bullet in a note with no frontmatter.
    """
    if text[end : end + 2] == "\r\n":
        return text[end + 2 :]
    if text[end : end + 1] == "\n":
        return text[end + 1 :]
    return text[end:]


def apply_entry_edit(
    *,
    vault_root: Path,
    relative_path: str,
    expected_revision: str,
    identity: str,
    fingerprint: str,
    replacement: str | None = None,
    delete: bool = False,
    actor: str,
    source: str,
    workspace: str = "",
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replace or delete one entry's exact span, journaled like any note write.

    Four checks stand between the caller's judgement and the write, in this order,
    and the first three write nothing at all:

    * ``expected_revision`` is compared against the note's exact bytes *under the
      note's own lock*, so a caller that read a different revision is a
      :class:`ciao.memory_receipts.RevisionConflict` rather than an overwrite. A
      note that moved is not the note anybody judged, whatever the entry is.
    * the entry is resolved **by identity**, which
      :func:`ciao.note_entries.entry_identity` derives from the workspace, the
      note path, the nearest heading, the entry's own fingerprint and a duplicate
      ordinal — and from no line number and no offset. So a caller whose offsets
      went stale with the note still gets the entry it named, a note whose
      unrelated content moved still gets the same entry, and an identity that
      names nothing here is a conflict, never a guess at the nearest bullet.
    * the entry's current :func:`ciao.note_entries.refresh_fingerprint` must equal
      the one the caller judged. The identity already covers the fingerprint, so
      this is the second half of the same binding stated where a reader can see
      it: it is what makes a *re-stamp* provably the same fact, since a re-stamp
      leaves the fingerprint alone by construction and a re-worded entry does not.
    * the composed ``after_text`` is then written by :func:`commit_note_change`
      unchanged, so the receipt holds both full images, the write is atomic under
      the same lock, and :func:`undo_note_receipt` restores the whole file byte
      for byte.

    Returns that same receipt. ``delete=True`` removes the entry and its line, and
    is reversible exactly like any other edit: the whole file comes back through
    undo, and the row says which entry it was. A *whole-note* retirement is
    still :func:`ciao.vault_review.trash_note`'s, and this module has no trash,
    no delete and no archive of its own.
    """
    root = canonical_vault(vault_root)
    target = resolve_note_path(root, relative_path)
    stored_path = target.relative_to(root).as_posix()
    expected = str(expected_revision or "").strip()
    if not expected:
        # Not a stale revision but a missing one: a caller that cannot say what it
        # read is asking for a blind overwrite, which is the one thing this
        # protocol exists to prevent.
        raise mr.MemoryReceiptError(
            "an expected revision is required; this primitive never overwrites a "
            "note it has not read"
        )
    with mr.queue_lock(target):
        text = _read_note_text(target)
        current = mr.content_revision(text)
        if current != expected:
            raise mr.RevisionConflict(
                "the note changed since this entry edit was planned; nothing was "
                "written"
            )
        entry = find_entry(
            text, identity=identity, note_path=stored_path, workspace=workspace
        )
        if entry is None:
            raise mr.RevisionConflict(
                f"{stored_path} holds no entry with identity {str(identity)[:12]}, so "
                "there is nothing this edit can be applied to; nothing was written"
            )
        if entry.fingerprint != str(fingerprint or "").strip():
            raise mr.RevisionConflict(
                f"the entry {str(identity)[:12]} in {stored_path} is not the text this "
                "edit was planned against (its fingerprint has changed); nothing was "
                "written, and the caller must read the entry again"
            )
        after_text, refusal = compose_entry_edit(
            text, entry, replacement=replacement, delete=delete
        )
    if refusal:
        raise mr.MemoryReceiptError(
            f"the entry edit was refused and nothing was written: {refusal}"
        )
    # Outside the lock, which this thread still holds (it is re-entrant) but which
    # `commit_note_change` re-takes around the rename and re-checks the revision
    # against: the `after_text` above was composed from exactly the bytes whose
    # revision is `expected`, so the two images in the receipt describe one
    # operation even though the write is a second, journaled step.
    return commit_note_change(
        vault_root=root,
        relative_path=stored_path,
        expected_revision=expected,
        after_text=after_text,
        actor=actor,
        source=source,
        workspace=workspace,
        provenance=provenance,
    )


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


def _undone_row(original: dict[str, Any], undo_receipt_id: str) -> dict[str, Any]:
    """The settlement row that retires one original receipt.

    One builder, because two paths write it — the undo that completes normally
    and the recovery pass that settles an interrupted one — and the journal
    folds by id, so a receipt is only trustworthy if both paths agree on what
    the same undo produced.
    """
    return {
        **{k: v for k, v in original.items() if k != "v"},
        "status": mr.UNDONE,
        "undo_of": str(original.get("id", "")),
        "undo_receipt": undo_receipt_id,
        "settled_at": mr._now(),
    }


def settle_open_undo_links(journal: Path) -> list[dict[str, Any]]:
    """Settle originals whose undo landed but whose own settlement did not.

    An undo writes the note back and *then* marks the original ``undone``, so a
    crash in that window leaves a journaled, applied reverse write attached to
    an original that still reads as ``applied`` — and therefore still offers an
    undo that can only fail, against a note that is already restored. This pass
    settles the original; it does **not** replay or re-attempt the reverse
    write, which already landed and is recorded in full.

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
        undone = _undone_row(original, str(row.get("id", "")))
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
    another managed write or the user's own hand. The before image must also
    still hash to the revision the receipt records, so a journal row that was
    edited or forged cannot install content the receipt never wrote, and a row
    that changed nothing is refused outright — its before image is the note's
    own text, so reversing it would write the file over itself.

    The reverse write goes through the same journaled protocol as a forward one
    — ``prepared``, replace, confirm — as a ``note_undo`` row carrying
    ``undo_of``, journaled to the *same* journal this settlement is appended to.
    That row is what makes an interrupted undo recoverable: if the process dies
    before the replace, it settles ``rolled_back`` and the original stays
    undoable; if it dies after, it settles ``applied`` and
    :func:`settle_open_undo_links` settles the original — it does not replay
    the undo, which already landed. Only once the reverse write is journaled
    does the original become ``undone``.

    A note that is gone, renamed or replaced by a link is a
    :class:`ciao.memory_receipts.RevisionConflict` like any other moved
    destination, never a bare :class:`NoteTargetRefused`: callers of
    :func:`ciao.memory_receipts.undo_receipt` handle the protocol's own
    exceptions, and an unhandled one is a 500 for an ordinary user action.

    Returns the original's ``undone`` row, which carries ``undo_receipt`` for
    the reverse write. If the reverse write landed but that final row could not
    be appended, this raises :class:`ciao.memory_receipts.MemoryReceiptError`
    saying so: the undo is done, and the caller must re-read rather than retry.
    """
    if str(receipt.get("kind", "")) != NOTE_APPLY:
        raise mr.UndoUnsupported("only a note apply can be undone")
    # The no-op write is journaled with both images, so it passes every image
    # and revision check below and the reverse write would put the note's own
    # bytes back over themselves. `memory_receipts.undo_receipt` refuses it via
    # `is_undoable`, but that is the only caller guarding it: this is the
    # primitive, so it refuses the row itself rather than trusting every caller
    # to have asked first.
    if receipt.get("undoable", True) is False or receipt.get("changed", True) is False:
        raise mr.UndoUnsupported(
            "this write changed nothing, so there is no change to reverse; it "
            "stays view-only"
        )
    before = receipt.get("before_text")
    after = receipt.get("after_text")
    if not isinstance(before, str) or not isinstance(after, str):
        raise mr.UndoUnsupported("this receipt carries no complete note image")
    # The revision guard below proves the note still holds this operation's
    # *after* image; it says nothing about the before image, which this then
    # writes back as the replacement text. A row that was edited, truncated or
    # forged in the journal would pass every other check and replace the note
    # with content the receipt never recorded. Hashing the image against the
    # revision the receipt itself carries is the only thing that binds them, so
    # a mismatch is "this receipt has no usable before image" — the same
    # refusal as a missing one, and the note is left exactly as it stands.
    if mr.content_revision(before) != str(receipt.get("before_revision", "")):
        raise mr.UndoUnsupported(
            "this receipt's before image does not hash to the before revision "
            "it records, so it cannot be restored; the note was not touched"
        )
    journal_vault = vault_for_journal(journal)
    if vault_root is not None and journal_vault is not None:
        # The reverse write and the original's settlement must land in one
        # journal, or the linkage between them is a claim no recovery pass can
        # check. A caller that passes a vault other than the one its journal
        # belongs to has two incompatible anchors; refuse rather than pick one.
        caller_vault = canonical_vault(vault_root)
        if caller_vault != journal_vault:
            raise mr.UndoUnsupported(
                f"the receipt's journal belongs to {journal_vault}, not to the "
                f"vault {caller_vault} given for the undo"
            )
    root = canonical_vault(vault_root) if vault_root is not None else journal_vault
    if root is None:
        raise mr.UndoUnsupported(
            "the vault this receipt belongs to could not be resolved"
        )
    try:
        target = resolve_note_path(root, str(receipt.get("relative_path", "")))
    except NoteTargetRefused as exc:
        raise mr.RevisionConflict(
            f"the note is no longer a writable note in this vault: {exc}"
        ) from exc
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
            journal=journal,
        )
    undone = _undone_row(receipt, str(reverse.get("id", "")))
    try:
        mr._append(journal, undone)
    except OSError as exc:
        # The reverse write is already on disk and journaled as its own
        # `note_undo` row; only the original's settlement is missing. A raw
        # OSError here would read as "the undo failed" and invite a retry that
        # cannot succeed — the note no longer matches the after image, so the
        # retry is a revision conflict against a note that is already restored.
        # Say what actually happened, so the caller reads the journal instead:
        # one recovery pass settles the link from the reverse row.
        raise mr.MemoryReceiptError(
            "the undo landed: the note was restored to its before image, but "
            f"the journal could not record that ({exc}); read the receipt "
            "again rather than retrying the undo"
        ) from exc
    return undone
