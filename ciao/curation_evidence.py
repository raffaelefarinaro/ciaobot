"""Bounded, revision-bound evidence for one due stale note.

:func:`prepare_one_stale_note` takes the first planned stale-note item whose key
names exactly one note, reads that note through a no-follow regular-file
descriptor, and returns the note's key, its path and the revision the plan was
made against, together with the note-edit proposals that name the same note:
open ones (a live queue row and a readable sidecar) and settled ones.

It is read-only. Nothing is written, no model is called and no scheduler is
involved. Every doubt is a :class:`DeferredNote` with a fixed reason, never a
partial answer: an unreadable, oversized or malformed store means the evidence
cannot say what the note's proposals are, so it says nothing.

Trust boundary: only the final path component of each file read is opened
without following links; the components above it are checked with ``lstat``.
The vault is trusted against concurrent local writers, so a race that swaps a
parent directory between the check and the open is not defended against.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ciao import curation_run as cr
from ciao import memory_receipts as mr
from ciao import note_edit_proposals as nep
from ciao import note_receipts as nr
from ciao import proposal_tracking as pt
from ciao.os_support.files import open_fd

#: The note itself is read up to this many bytes; one more byte refuses.
MAX_NOTE_BYTES = 64 * 1024
#: The live proposal queue is read up to this many bytes; one more refuses.
MAX_QUEUE_BYTES = 256 * 1024
#: A store with more sidecar files than this refuses. The limit is inclusive.
MAX_SIDECARS = 512
#: One sidecar file, inclusive of this many bytes.
MAX_SIDECAR_BYTES = 256 * 1024
#: All sidecars together, inclusive of this many bytes.
MAX_STORE_BYTES = 8 * 1024 * 1024
#: More related references than this defer the evidence rather than truncate it.
MAX_REFERENCES = 20

_READ_CHUNK = 64 * 1024


@dataclass(frozen=True, slots=True)
class ProposalReference:
    """One note-edit proposal about the prepared note, projected to its own fields.

    ``status`` is ``open`` for an unsettled record with a live queue row, and
    ``settled`` for a record the owner decided. ``revision`` is ``current`` when
    the record was filed against the note's revision now, and ``historical``
    when it was filed against an older one. ``proposal_id`` is the live queue row
    id, or empty when the record has no live row.
    """

    sidecar_id: str
    proposal_id: str
    status: str
    revision: str
    operation: str
    expected_revision: str
    settled_at: str
    accepted: bool


@dataclass(frozen=True, slots=True)
class PreparedNote:
    item_key: str
    relative_path: str
    revision: str
    references: tuple[ProposalReference, ...] = ()


@dataclass(frozen=True, slots=True)
class NoSelectedNote:
    """The plan names no planned stale note; this is not a refusal."""


@dataclass(frozen=True, slots=True)
class DeferredNote:
    """The planned note exists but its evidence cannot be stated exactly."""

    reason: str


PreparedOutcome = PreparedNote | NoSelectedNote | DeferredNote


class _Refused(Exception):
    """An internal refusal; its message is a fixed reason safe to hand back."""


def prepare_one_stale_note(
    config: Any,
    workspace: str,
    vault_root: Path,
    plan: cr.RunPlan,
) -> PreparedOutcome:
    """The evidence for the first planned stale note, or why there is none."""
    target = _selected(plan)
    if target is None:
        return NoSelectedNote()
    key = cr.item_key(cr.PASS_STALE_NOTE, target.relative_path)
    try:
        return _prepare(config, workspace, Path(vault_root), target, key)
    except _Refused as exc:
        return DeferredNote(reason=str(exc))
    except nep.NoteEditRefused:
        return DeferredNote(reason="the workspace's vault is not usable for this note")
    except nr.NoteTargetRefused:
        return DeferredNote(reason="the planned note path does not name a plain note")
    except OSError:
        return DeferredNote(reason="a file in the vault could not be read")


def _selected(plan: cr.RunPlan) -> cr.StaleNoteTarget | None:
    """The target of the first planned stale-note item with exactly one matching key.

    Deferred work is never considered: a planned slice is the only thing this
    run may touch.
    """
    for item in plan.planned:
        target = item.target
        if item.pass_id != cr.PASS_STALE_NOTE or target is None:
            continue
        wanted = cr.item_key(cr.PASS_STALE_NOTE, target.relative_path)
        if item.keys.count(wanted) == 1:
            return target
    return None


def _prepare(
    config: Any,
    workspace: str,
    vault: Path,
    target: cr.StaleNoteTarget,
    key: str,
) -> PreparedNote:
    _bind_workspace(config, workspace, vault)
    note = nr.resolve_note_path(vault, target.relative_path)
    _check_note_revision(note, target)
    sidecars = _read_sidecars(vault, workspace)
    rows = _live_rows_for(vault, workspace, target.relative_path)
    references = _references(sidecars, rows, target)
    return PreparedNote(
        item_key=key,
        relative_path=target.relative_path,
        revision=target.revision,
        references=references,
    )


def _bind_workspace(config: Any, workspace: str, vault: Path) -> None:
    """The vault the config registry gives this workspace must be the planned one."""
    bound = Path(nep._vault_root(config, workspace))
    if bound.resolve() != vault.resolve():
        raise _Refused("the workspace's configured vault is not the planned vault")


def _check_note_revision(note: Path, target: cr.StaleNoteTarget) -> None:
    raw = _read_bounded(note, MAX_NOTE_BYTES, "the planned note")
    if raw is None:
        raise _Refused("the planned note is missing")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _Refused("the planned note is not UTF-8") from exc
    if mr.content_revision(text) != target.revision:
        raise _Refused("the note changed since it was planned")


def _read_sidecars(vault: Path, workspace: str) -> list[nep.NoteEditProposal]:
    """Every sidecar in the store, each parsed; any doubt refuses the whole store."""
    store = _plain_dir(vault, nep.SIDECAR_RELATIVE)
    if store is None:
        return []
    try:
        with os.scandir(store) as entries:
            names = sorted(
                entry.name for entry in entries if entry.name.endswith(".json")
            )
    except OSError as exc:
        raise _Refused("the note-edit store cannot be listed") from exc
    if len(names) > MAX_SIDECARS:
        raise _Refused("the note-edit store holds more records than can be read")
    proposals: list[nep.NoteEditProposal] = []
    total = 0
    for name in names:
        raw = _read_bounded(store / name, MAX_SIDECAR_BYTES, "a note-edit record")
        if raw is None:
            raise _Refused("a note-edit record disappeared during the read")
        total += len(raw)
        if total > MAX_STORE_BYTES:
            raise _Refused("the note-edit store is larger than can be read")
        try:
            text = raw.decode("utf-8")
            proposal = nep.parse_sidecar_text(
                text, where=Path(name), proposal_id=name[: -len(".json")]
            )
        except (UnicodeDecodeError, nep.NoteEditSidecarError) as exc:
            raise _Refused("a note-edit record cannot be read") from exc
        if proposal.workspace != workspace:
            raise _Refused("a note-edit record belongs to another workspace")
        proposals.append(proposal)
    return proposals


def _normal(text: str) -> str:
    """Whitespace-collapsed text, the form the queue's bullets are written in."""
    return " ".join(text.split())


def _live_rows_for(
    vault: Path, workspace: str, relative_path: str
) -> list[pt.QueuedProposal]:
    """The live note-edit queue rows whose bullet names this note.

    Compared in the queue's own whitespace-collapsed form: the bullet was written
    through ``_one_line``, so a path with a double space must match its own row.
    """
    queue_dir = _plain_dir(vault, nep._QUEUE_RELATIVE[:-1])
    if queue_dir is None:
        return []
    raw = _read_bounded(
        queue_dir / nep._QUEUE_RELATIVE[-1], MAX_QUEUE_BYTES, "the proposal queue"
    )
    if raw is None:
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _Refused("the proposal queue is not UTF-8") from exc
    queue_rel = Path(workspace).joinpath(*nep._QUEUE_RELATIVE).as_posix()
    head = f"{_normal(relative_path)} —"
    return [
        entry
        for entry in pt.walk_proposal_queue(workspace, queue_rel, text)
        if entry.bullet.kind == nep.KIND and _normal(entry.bullet.text).startswith(head)
    ]


def _references(
    sidecars: list[nep.NoteEditProposal],
    rows: list[pt.QueuedProposal],
    target: cr.StaleNoteTarget,
) -> tuple[ProposalReference, ...]:
    """The related records for the target, linked to the queue by proposal id.

    The link is the record's ``proposal_id``. Anything that makes the link
    ambiguous defers: two records sharing one queue row, a queue row for this
    note whose record is missing, or whose record names another note.
    """
    by_row: dict[str, list[nep.NoteEditProposal]] = {}
    for proposal in sidecars:
        if proposal.proposal_id:
            by_row.setdefault(proposal.proposal_id, []).append(proposal)
    if any(len(records) > 1 for records in by_row.values()):
        raise _Refused("two note-edit records name one queue row")
    for row in rows:
        records = by_row.get(row.proposal_id, [])
        if not records:
            raise _Refused("a queued proposal for this note has no readable record")
        if records[0].relative_path != target.relative_path:
            raise _Refused("a queued proposal for this note is filed against another note")
    live = {row.proposal_id for row in rows}
    references: list[ProposalReference] = []
    for proposal in sidecars:
        if proposal.relative_path != target.relative_path:
            continue
        revision = (
            "current" if proposal.expected_revision == target.revision else "historical"
        )
        if proposal.settled:
            # A settled record stays settled whatever bullet is still queued.
            status = "settled"
        elif proposal.proposal_id in live:
            status = "open"
        else:
            # An unsettled record with no live row is an orphan, not open work.
            continue
        references.append(
            ProposalReference(
                sidecar_id=proposal.id,
                proposal_id=proposal.proposal_id if proposal.proposal_id in live else "",
                status=status,
                revision=revision,
                operation=proposal.operation,
                expected_revision=proposal.expected_revision,
                settled_at=proposal.settled,
                accepted=proposal.accepted,
            )
        )
    if len(references) > MAX_REFERENCES:
        raise _Refused("more related proposals than the evidence can carry")
    return tuple(sorted(references, key=lambda ref: ref.sidecar_id))


def _plain_dir(vault: Path, parts: tuple[str, ...]) -> Path | None:
    """The directory at ``vault/parts``, or None when it is absent.

    Every component must be a real directory: a link anywhere on the way is a
    refusal, because a read through it would leave the vault.
    """
    current = vault
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _Refused("the vault's store cannot be inspected") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise _Refused("the vault's store passes through a link or a file")
    return current


def _read_bounded(path: Path, limit: int, what: str) -> bytes | None:
    """The bytes of one regular file of at most ``limit`` bytes, or None if absent.

    Opened without following links. ``limit`` bytes exactly pass; one more refuses.
    """
    try:
        fd = open_fd(path, os.O_RDONLY, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _Refused(f"{what} cannot be opened without following a link") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Refused(f"{what} is not a regular file")
        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            chunk = os.read(fd, min(_READ_CHUNK, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > limit:
            raise _Refused(f"{what} is larger than the read ceiling")
        return b"".join(chunks)
    finally:
        os.close(fd)
