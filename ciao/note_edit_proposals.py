"""The typed `note_edit` proposal a verification's `needs_review` result files.

:mod:`ciao.note_verification` decides what a verification *may* do. Four times
over it answers ``needs_review`` — a retirement, an update the evidence cannot
carry, a ``still_valid`` with no ``updated:`` to stamp — and that answer is a
verdict with nowhere to go: the check state records it and the cooldown expires,
but nobody was asked. This module is where the asking happens.

The shape is the category proposal's, deliberately
(:mod:`ciao.vocabulary_proposals`): the queue bullet is ONE line, and none of
what a note edit needs fits on one. So the bullet's payload is an id and the
sidecar beside the queue holds the operation, the exact before/after images, the
evidence that produced them and the check the filing settled. A bullet with no
sidecar is a row whose accept refuses and keeps; a sidecar with no bullet is
litter, which is the direction that fails safe, so the sidecar is written
FIRST.

**Whole-note and one-entry, in the same record.** :mod:`ciao.note_verification`
judges a whole note; :mod:`ciao.entry_verification` judges one list item inside
one. Both verdicts file here, told apart by the operation
(:data:`REPLACE`, :data:`RESTAMP`, :data:`RETIRE` against :data:`REPLACE_ENTRY`,
:data:`RESTAMP_ENTRY`, :data:`RETIRE_ENTRY`) and by the three entry fields
:attr:`NoteEditProposal.entry_identity`,
:attr:`NoteEditProposal.entry_fingerprint` and
:attr:`NoteEditProposal.entry_span`. The images stay the NOTE's full text both
ways for both, and that is deliberate rather than a leftover: a bounded patch is a
different operation with different rules, and what an accept applies is a
whole-note :func:`ciao.note_receipts.commit_note_change` either way. What an
entry operation adds is the *identity* of the entry, the *fingerprint* the
verdict was reached about, and the *span* — so the accept can prove the entry is
still the entry the reviewer read, compose the exact bytes, and refuse on any
mismatch instead of rewriting whatever sits at those offsets now.

**What is filed, and what is not.** Exactly one proposal per unit of work: for a
whole-note operation that is ``(note, expected_revision)``, for an entry operation
that is ``(note, expected_revision, entry_identity)``, so two entries of one note
at one revision are two different questions and get two rows. The id is derived
from those, so a second pass that reaches the same verdict about the same text
finds the record it already wrote and writes nothing. The proof that this actually
happened is the ``proposal_id`` on the check the filing recorded —
:func:`ciao.note_verification._check_settles` for a note, its entry-level twin
:func:`ciao.entry_verification.should_check_entry` for an entry, both of which
suppress what is waiting on a proposal whatever its cooldown says. Filing without
recording the id would let the next nightly pass file a second row for something
that already has one.

**Nothing here applies anything.** This module files and settles; the accept
lives with the other accept handlers in :mod:`ciao.web.proposal_service`, and a
retirement is a human click all the way down — ``note_verification`` itself
cannot reach a delete or a trash (its guardrail test says so), and a proposer
that could retire a note unattended would be the same defect wearing a queue.

**Why a dismissal is not permanent.** A category refusal is written to its
sidecar by id and the cluster is never offered again, because the cluster is
regenerated from the vault on every pass and the id is the only stable handle.
A note edit has a better one: the check state's ``proposal_id``, and a check
describes exactly ONE revision. So a settled row clears that id and leaves the
check's own cooldown running — the same text is not asked about again this
month, and the moment the note changes the check no longer describes it and the
note is due. There is deliberately no "rejected forever" flag here: a refusal
the owner gave about last month's text says nothing about this month's.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ciao import entry_verification as ev
from ciao import memory_receipts as mr
from ciao import note_entries as ne
from ciao import note_receipts as nr
from ciao import note_verification as nv
from ciao import proposal_tracking

# `note_verification` owns the vault-relative-path rule, and it is the same rule
# (a verdict keyed by anything else describes no note, and a proposal naming an
# absolute path names a file outside the vault). Its private name is imported
# deliberately rather than a second copy of the check written here, which is the
# kind of drift this repository keeps refusing to grow.
from ciao.note_verification import _stored_key as _check_key

logger = logging.getLogger(__name__)

#: Where the sidecars live, relative to a workspace's vault. A directory, like
#: the category sidecar beside it, because there is one record per filed
#: proposal and a vault accumulates them.
SIDECAR_RELATIVE = ("Workspace", "Memory-Note-Edit-Proposals")

SIDECAR_SCHEMA = 1
"""The sidecar file's own version, so a reader that meets a shape it was not
written for can say so instead of guessing. An unrecognized version is refused
rather than read as no record: re-filing an edit a human has not seen costs one
queue row, whereas guessing at a record would apply an operation nobody
reviewed."""

KIND = "note_edit"
"""The proposal kind this module files under. Named once so the bullet, the id
derivation and the tests cannot disagree about the spelling."""

REPLACE = "replace"
RESTAMP = "restamp"
RETIRE = "retire"

REPLACE_ENTRY = "replace_entry"
RESTAMP_ENTRY = "restamp_entry"
RETIRE_ENTRY = "retire_entry"

OPERATIONS = (REPLACE, RESTAMP, RETIRE, REPLACE_ENTRY, RESTAMP_ENTRY, RETIRE_ENTRY)
"""The six operations a note edit can carry.

``replace`` is the agent's exact replacement text, ``restamp`` is a verification
date re-stamped on the note's own frontmatter, and ``retire`` is the one operation
that removes a whole note — attended only, never from a pass.

The three ``*_entry`` operations are those same three, narrowed to one list item
inside the note: an exact replacement of that item's span, a re-stamp of that
item's own ``[verified:]`` date, and the removal of that item alone. The note
itself is never trashed by any of them — that stays
:func:`ciao.vault_review.trash_note`'s — and every one of them is a whole-note
:func:`ciao.note_receipts.commit_note_change` underneath, so undo is unchanged.
"""

ENTRY_OPERATIONS = (REPLACE_ENTRY, RESTAMP_ENTRY, RETIRE_ENTRY)
"""The operations that name an entry rather than a note.

Split out because the two halves file genuinely different things: a whole-note
operation needs a path and a revision, an entry operation needs the entry's
identity, the fingerprint its verdict was about and the span it replaced — and
each is refused without them. A caller cannot file an entry edit for an entry it
cannot name, let alone for one that is not there.
"""

#: The queue file a `note_edit` bullet is appended to, as the row id is derived
#: from it. `ciao.web.proposal_service` owns the same pair; neither derives it
#: from a third place.
_QUEUE_RELATIVE = ("Workspace", "Memory-Proposals.md")


class NoteEditError(ValueError):
    """Base for the refusals this module raises, so a caller catches one name."""


class NoteEditRefused(NoteEditError):
    """A proposed note edit is not one that may be filed.

    Raised by :func:`file_note_edit` for an operation outside
    :data:`OPERATIONS`, a path that is not vault-relative, a missing revision,
    a ``replace`` with no replacement text (which would empty the note, and
    emptying a note is the deletion this whole path refuses to perform), a
    re-stamp that does not cover the note, an entry operation with no entry
    identity or fingerprint, a whole-note operation carrying one, or an entry
    whose span cannot be measured because the note moved, the entry is gone or
    the entry is not the text the verdict was reached about.
    """


class NoteEditSidecarError(NoteEditError):
    """A note-edit sidecar that cannot be trusted.

    Raised by the readers, never repaired: a proposal whose operation or
    replacement text cannot be read must not be applied against a guessed one,
    and a settlement that cannot be recorded must not be reported as recorded.
    """


@dataclass(frozen=True, slots=True)
class NoteEditProposal:
    """One filed note edit, as the sidecar stores it.

    ``before`` and ``after`` are the note's FULL text, full both ways, because
    this is a whole-note write and a bounded patch is a different operation with
    different rules. ``after`` is empty for a :data:`RETIRE`, which writes no
    body at all.

    ``expected_revision`` is the revision the verification planned against, and
    it is the whole of the accept's conflict check: a note that moved since is a
    conflict with nothing written, never an overwrite.

    ``stamp_date`` is the date a :data:`RESTAMP` writes into the note's
    frontmatter, fixed HERE, when the verification that filed it was dated. It
    cannot be left to the accept: a card previewed before midnight and clicked
    after it would then apply a different date than the card showed, under an
    ``exact`` label that promises the two are the same bytes. It is empty for
    every other operation, which writes a date of nobody's business or none.

    ``settled`` is when the owner decided and ``accepted`` is which way, because
    a trashed note and a rewritten one are different outcomes and a record that
    cannot tell them apart says nothing. ``receipt_id`` is the note receipt an
    accept's write handed back, so an undo has something to point at.

    The three entry fields are empty for a whole-note operation, and
    :data:`ENTRY_OPERATIONS` operations are refused without all three. They are
    the entry's :func:`ciao.note_entries.entry_identity`, the
    :func:`ciao.note_entries.refresh_fingerprint` its verdict was reached about,
    and the ``(start, end)`` character offsets that entry occupied in ``before``.
    The span is recorded rather than trusted because it is checkable: an accept
    recovers the entry's exact replacement from it (see :func:`entry_replacement`)
    and hands that to
    :func:`ciao.note_receipts.apply_entry_edit`, which resolves the entry again by
    identity and refuses on any mismatch. Nothing here is ever spliced at a bare
    offset the file happens to hold.
    """

    id: str
    workspace: str
    relative_path: str
    operation: str
    expected_revision: str
    before: str
    after: str
    outcome: str
    coverage: str
    evidence: tuple[nv.Evidence, ...]
    reason: str
    created_at: str
    proposal_id: str = ""
    receipt_id: str = ""
    settled: str = ""
    accepted: bool = False
    stamp_date: str = ""
    entry_identity: str = ""
    entry_fingerprint: str = ""
    entry_span: tuple[int, int] = (0, 0)

    def as_dict(self) -> dict[str, Any]:
        """The row the sidecar file stores."""
        return {
            "id": self.id,
            "workspace": self.workspace,
            "relative_path": self.relative_path,
            "operation": self.operation,
            "expected_revision": self.expected_revision,
            "before": self.before,
            "after": self.after,
            "outcome": self.outcome,
            "coverage": self.coverage,
            "evidence": [row.as_dict() for row in self.evidence],
            "reason": self.reason,
            "created_at": self.created_at,
            "proposal_id": self.proposal_id,
            "receipt_id": self.receipt_id,
            "settled": self.settled,
            "accepted": self.accepted,
            "stamp_date": self.stamp_date,
            "entry_identity": self.entry_identity,
            "entry_fingerprint": self.entry_fingerprint,
            "entry_span": [self.entry_span[0], self.entry_span[1]],
        }


# ── Entry operations ───────────────────────────────────────────────────────


def entry_replacement(proposal: NoteEditProposal) -> str:
    """The entry's exact new text, recovered from the recorded splice.

    The inverse of :func:`ciao.note_receipts.compose_entry_edit`. ``before`` and
    ``after`` are the whole note either way, and ``entry_span`` says which slice of
    ``before`` the edit replaced — so the replacement is exactly the slice of
    ``after`` between the same prefix and the same suffix, and the note's other
    bytes are the same bytes in both images by construction.

    A *delete* consumes the entry's own line ending along with its text, so the
    suffix it leaves is what follows the line ending rather than what follows the
    span. Accounting for that is the difference between this being the inverse of
    the delete and being a check the delete fails: the first bullet of a note with
    no frontmatter starts at offset 0, its span ends at the ``\\n``, and the byte
    immediately after the span is the newline the deletion removed — a suffix
    comparison against the raw remainder of ``before`` can never match, so the
    refusal would blame a record that is perfectly clean. The consumed line ending
    is measured with the same helper the compose used, both newline spellings
    included.

    This is what lets an accept go through
    :func:`ciao.note_receipts.apply_entry_edit` rather than writing the stored
    ``after`` verbatim: the managed helper takes the *entry's* new text, resolves
    the entry again by identity, and refuses on any mismatch, so a row that was
    hand-edited, truncated or forged cannot turn a whole-note image into an
    entry's worth of prose. The refusal here is the same check run a second time,
    on the record, before anything reaches the vault.

    Raises :class:`NoteEditError` when the row is not a clean inverse — an entry
    operation without a span, a span outside ``before``, or an ``after`` that
    does not keep the note's own prefix and suffix. A whole-note proposal has no
    entry to recover and is refused outright.
    """
    if proposal.operation not in ENTRY_OPERATIONS:
        raise NoteEditError(
            f"a {proposal.operation} is a whole-note edit and names no entry, so "
            "there is no entry replacement to recover"
        )
    start, end = proposal.entry_span
    before, after = proposal.before, proposal.after
    if not (0 <= start <= end <= len(before)):
        raise NoteEditError(
            f"the entry span ({start}, {end}) is outside the {len(before)}-character "
            "note this record was filed against, so its replacement cannot be recovered"
        )
    # What the note holds after the entry's span: the raw remainder first, and then
    # that same remainder with the entry's own line ending taken off. The order is
    # load-bearing rather than cosmetic — a *replace* is matched against the exact
    # bytes it kept, and only a *delete* falls through to the shortened tail, where
    # the recovered slice is empty by construction either way.
    tails = (before[end:], nr.after_entry_line(before, end))
    for tail in tails:
        if after.startswith(before[:start]) and after.endswith(tail):
            return after[start : len(after) - len(tail)]
    raise NoteEditError(
        "the recorded after image does not keep the note's own text outside "
        "the entry's span, so it is not the image this edit produced"
    )



# ── Identity ───────────────────────────────────────────────────────────────


def note_edit_id(
    workspace: str, relative_path: str, expected_revision: str, entry_identity: str = ""
) -> str:
    """The stable sidecar id for one edit of one revision of one note or entry.

    Derived from the things that make the proposal what it is, the way
    :func:`ciao.proposal_tracking.stable_proposal_id` derives a queue row's: so a
    second pass reaching the same verdict about the same text computes the same id
    and finds the record it already wrote, and a note that changed computes a
    different one and is a new question. The revision is in the basis because a
    check — and this proposal — describes exactly one revision.

    ``entry_identity`` is in the basis for an entry operation and contributes
    **nothing at all** to a whole-note one — not even an empty field — so a
    whole-note proposal's id is byte-identical to the id this function produced
    before entry operations existed. It has to be: a proposal already pending in
    a sidecar at upgrade is found by ``read_sidecar(new_id)``, and a changed
    basis would miss it and write a second, orphan record for a question already
    in the queue. The two entries of one note at one revision are two different
    questions, so an id shared by both would let the second filing find the
    first's record and leave a reviewer deciding about one bullet while the other
    silently goes unasked.
    """
    basis = f"{workspace}\x00{relative_path}\x00{expected_revision}"
    if entry_identity:
        basis = f"{basis}\x00{entry_identity}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]



def _now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


# ── Where the sidecar lives ────────────────────────────────────────────────


def _vault_root(config: Any, workspace: str) -> Path:
    """The vault this workspace's notes live in.

    Resolved through the install's own registry rather than from a caller's
    root, for the same reason ``note_verification`` does it: the check state
    lives in the vault whose notes it describes, and a proposal that paired one
    workspace's name with another workspace's vault would settle the wrong
    vault's check. A config that cannot be asked is refused rather than guessed
    at, because the alternative is a check filed in a vault nobody claimed.
    """
    getter = getattr(config, "workspace_vault_root", None)
    if not callable(getter):
        raise NoteEditRefused(
            "this config does not answer where workspace "
            f"{workspace!r} keeps its notes (workspace_vault_root is not callable)"
        )
    try:
        root = Path(getter(workspace))
    except (OSError, TypeError, ValueError) as exc:
        raise NoteEditRefused(
            f"the vault configured for workspace {workspace!r} is unusable ({exc})"
        ) from exc
    if not root.is_dir():
        raise NoteEditRefused(f"no vault at {root} for workspace {workspace!r}")
    return root


def sidecar_dir(config: Any, workspace: str) -> Path:
    """The directory holding this workspace's filed note edits."""
    return Path(_vault_root(config, workspace)).joinpath(*SIDECAR_RELATIVE)


def sidecar_path(config: Any, workspace: str, proposal_id: str) -> Path:
    return sidecar_dir(config, workspace) / f"{proposal_id}.json"


# ── Reading and writing ────────────────────────────────────────────────────

_STRING_FIELDS = (
    "id",
    "workspace",
    "relative_path",
    "operation",
    "expected_revision",
    "before",
    "after",
    "outcome",
    "coverage",
    "reason",
    "created_at",
    "proposal_id",
    "receipt_id",
    "settled",
    "stamp_date",
)

#: Fields an *entry* operation cannot be read without. Optional on a whole-note
#: row rather than required, because a file written before entry operations
#: existed is still a readable whole-note record and refusing it would strand a
#: proposal nobody is being asked about any more; required for an entry
#: operation, because a splice with no identity to bind it to is not an edit to
#: anything. :func:`_proposal_from_mapping` re-asserts the per-operation rules, so
#: this is a second, independent refusal rather than the only one.
_ENTRY_STRING_FIELDS = ("entry_identity", "entry_fingerprint")


def _stored_span(raw: Any, where: Path) -> tuple[int, int]:
    """One stored ``entry_span`` as a ``(start, end)`` pair, or a refusal.

    A two-element list of integers and nothing else. A span is what a splice is
    cut at, so a string, a float, a negative, an inverted pair or a three-element
    list is a record whose edit cannot be located — and the failure mode of
    guessing is writing over the wrong part of somebody's note.
    """
    if (
        not isinstance(raw, list)
        or len(raw) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in raw)
    ):
        raise NoteEditSidecarError(
            f"note-edit proposal {where} has an entry_span of {raw!r}, which is not a "
            "[start, end] pair of whole numbers, so no entry can be located in it"
        )
    start, end = int(raw[0]), int(raw[1])
    if not 0 <= start <= end:
        raise NoteEditSidecarError(
            f"note-edit proposal {where} has an entry_span of ({start}, {end}), which "
            "is not an ordered pair, so no entry can be located in it"
        )
    return start, end


def _proposal_from_mapping(raw: Any, where: Path) -> NoteEditProposal:
    """One stored row as a :class:`NoteEditProposal`, or a refusal.

    Strict rather than total, the other way round from
    :meth:`ciao.note_verification.Evidence.from_mapping`: that one drops a
    missing citation because an empty citation cannot carry an auto-apply,
    which is the direction the rule already fails toward. Here the unreadable
    field IS the accept — the operation and the replacement text are what get
    written — so a row missing one is refused rather than defaulted.

    The per-operation rules below are :func:`file_note_edit`'s, re-asserted here.
    They have to be: a file this wrote is not the only way to get one, and a
    hand-edited or truncated row that kept every field's TYPE could still carry
    a ``replace`` with no replacement text, which the accept would happily write
    as an emptied note. Checking what filing checked means the reader cannot be
    talked into an operation nobody filed.

    The entry rules are the same idea one level in, and the strictest of them is
    :func:`entry_replacement`: the row has to be a clean inverse of the splice it
    claims, so an accept can hand the managed helper an entry's own text rather
    than the note's. :data:`RESTAMP_ENTRY` adds the check that the replacement's
    fingerprint is the one the verdict was about, which is what makes a re-stamp
    a re-stamp — a record that changes the fact while calling itself a stamp is
    refused rather than quietly written.
    """
    if not isinstance(raw, dict):
        raise NoteEditSidecarError(
            f"note-edit proposal {where} is a {type(raw).__name__}, not an object"
        )
    is_entry = raw.get("operation") in ENTRY_OPERATIONS
    entry_names = _ENTRY_STRING_FIELDS if is_entry else ()
    problems: list[str] = [
        f"{name} must be a string"
        for name in _STRING_FIELDS + entry_names
        if not isinstance(raw.get(name), str)
    ]
    if not isinstance(raw.get("evidence"), list):
        problems.append("evidence must be a list of citations")
    if not isinstance(raw.get("accepted"), bool):
        problems.append("accepted must be a boolean")
    if problems:
        raise NoteEditSidecarError(f"note-edit proposal {where}: {'; '.join(problems)}")
    span: tuple[int, int] = (0, 0)
    if is_entry:
        span = _stored_span(raw.get("entry_span"), where)
    elif raw.get("entry_span") not in (None, [], [0, 0]):
        raise NoteEditSidecarError(
            f"note-edit proposal {where} is a whole-note edit that carries an entry "
            "span, so it is not the record that was filed; nothing was written"
        )
    proposal = NoteEditProposal(
        id=str(raw["id"]),
        workspace=str(raw["workspace"]),
        relative_path=str(raw["relative_path"]),
        operation=str(raw["operation"]),
        expected_revision=str(raw["expected_revision"]),
        before=str(raw["before"]),
        after=str(raw["after"]),
        outcome=str(raw["outcome"]),
        coverage=str(raw["coverage"]),
        evidence=nv.Evidence.from_mappings(raw.get("evidence")),
        reason=str(raw["reason"]),
        created_at=str(raw["created_at"]),
        proposal_id=str(raw["proposal_id"]),
        receipt_id=str(raw["receipt_id"]),
        settled=str(raw["settled"]),
        accepted=bool(raw["accepted"]),
        stamp_date=str(raw["stamp_date"]),
        entry_identity=str(raw.get("entry_identity") or ""),
        entry_fingerprint=str(raw.get("entry_fingerprint") or ""),
        entry_span=span,
    )
    if not proposal.expected_revision:
        raise NoteEditSidecarError(
            f"note-edit proposal {where} names no revision, so applying it could "
            "not be conflict-checked; nothing was written"
        )
    if proposal.operation not in OPERATIONS:
        raise NoteEditSidecarError(
            f"note-edit proposal {where} names operation {proposal.operation!r}, "
            f"which is not one of {', '.join(OPERATIONS)}"
        )
    if proposal.outcome not in nv.OUTCOMES:
        raise NoteEditSidecarError(
            f"note-edit proposal {where} names outcome {proposal.outcome!r}, "
            f"which is not a verification's, so it describes no verdict; nothing "
            f"was written (expected one of {', '.join(nv.OUTCOMES)})"
        )
    if proposal.coverage not in (nv.COVERAGE_COMPLETE, nv.COVERAGE_PARTIAL):
        raise NoteEditSidecarError(
            f"note-edit proposal {where} names coverage {proposal.coverage!r}, "
            f"which is neither {nv.COVERAGE_COMPLETE!r} nor "
            f"{nv.COVERAGE_PARTIAL!r}; nothing was written"
        )
    if proposal.operation in (REPLACE, REPLACE_ENTRY) and not proposal.after.strip():
        raise NoteEditSidecarError(
            f"note-edit proposal {where} is a replace with no replacement text, "
            "so applying it would empty the note; nothing was written"
        )
    if proposal.operation == RETIRE and proposal.after:
        raise NoteEditSidecarError(
            f"note-edit proposal {where} is a retirement that carries an after "
            "image, so it is not the record that was filed; nothing was written"
        )
    if is_entry:
        _check_entry_fields(proposal, where)
    if proposal.operation in (RESTAMP, RESTAMP_ENTRY):
        if proposal.coverage != nv.COVERAGE_COMPLETE:
            raise NoteEditSidecarError(
                f"note-edit proposal {where} is a re-stamp from "
                f"{proposal.coverage!r} coverage, and a re-stamp claims the whole "
                "note is still true; nothing was written"
            )
        try:
            date.fromisoformat(proposal.stamp_date)
        except ValueError:
            raise NoteEditSidecarError(
                f"note-edit proposal {where} is a re-stamp naming stamp date "
                f"{proposal.stamp_date!r}, which is not a date, so the date it "
                "would stamp is not the one that was filed; nothing was written"
            ) from None
    return proposal


def _entry_refusal(proposal: NoteEditProposal, where: str) -> str:
    """Why this entry record may not be applied, or ``""`` when it may.

    Five rules, and each names a way a record could describe an edit that is not
    the one filed. Deliberately *pure* — it returns a reason rather than raising —
    because both sides need it and neither may hold the other's exception: filing
    raises :class:`NoteEditRefused`, the reader raises
    :class:`NoteEditSidecarError`, and a record that passes filing but not reading
    is a file somebody edited between the two.

    * an entry **identity**, which is the only thing the accept can resolve the
      entry by — an edit with no identity is a splice at an offset in a file, and
      that is the whole thing this path exists to avoid;
    * the **fingerprint** the verdict was about, so the accept can prove the entry
      is still the entry the reviewer read rather than a bullet that happens to sit
      at the same offsets now;
    * a **clean inverse** — :func:`entry_replacement` must recover the entry's own
      text from the recorded span, which is what stops a whole-note image being
      passed off as an entry's replacement;
    * for :data:`RETIRE_ENTRY`, an **empty** replacement: the record removes that
      span and nothing else, so a "retirement" that also carries new text is not
      the record that was filed. And a non-empty ``after`` overall, because a
      retire whose splice empties the note is a note deletion wearing a bullet's
      clothes — the refusal Vault Review's trash exists to be asked about instead;
    * for :data:`RESTAMP_ENTRY`, a replacement whose fingerprint **equals** the
      recorded one. A re-stamp changes the fact's date and nothing else, so a
      record that changes the words while calling itself a re-stamp is refused
      rather than written: a reviewer clicking "re-stamp this" must never get a
      rewrite.
    """
    where = f"note-edit proposal {where}" if where else "this note edit"
    identity = proposal.entry_identity.strip()
    if not identity:
        return (
            f"{where} is a {proposal.operation} naming no entry identity, so there "
            "is nothing to splice"
        )
    try:
        ev._stored_key(identity)
    except ev.EntryCheckRefused as exc:
        return f"{where}: {exc}"
    if not proposal.entry_fingerprint.strip():
        return (
            f"{where} is a {proposal.operation} naming no entry fingerprint, so it "
            "cannot be checked against the text it was judged on"
        )
    try:
        replacement = entry_replacement(proposal)
    except NoteEditError as exc:
        return f"{where}: {exc}"
    if proposal.operation == RETIRE_ENTRY:
        if replacement:
            return (
                f"{where} is an entry retirement that also replaces the entry with "
                f"{replacement!r}, so it is not the record that was filed"
            )
        if not proposal.after:
            return (
                f"{where} is an entry retirement whose after image is empty, so "
                "applying it would empty the whole note; a note is retired through "
                "Vault Review, not by deleting one of its bullets"
            )
        return ""
    if proposal.operation == RESTAMP_ENTRY:
        stamped = ne.refresh_fingerprint(replacement)
        if stamped != proposal.entry_fingerprint.strip():
            return (
                f"{where} is a re-stamp whose replacement changes the entry's own "
                f"text (fingerprint {stamped[:12]} is not "
                f"{proposal.entry_fingerprint[:12]}), so it is a rewrite rather than "
                "a re-stamp"
            )
    return ""


def _check_entry_fields(proposal: NoteEditProposal, where: Path) -> None:
    """:func:`_entry_refusal` as a reader's exception, or nothing."""
    reason = _entry_refusal(proposal, str(where))
    if reason:
        raise NoteEditSidecarError(f"{reason}; nothing was written")


def read_sidecar(
    config: Any, workspace: str, proposal_id: str
) -> NoteEditProposal | None:
    """The filed proposal with this id, or None when there is none.

    Raises :class:`NoteEditSidecarError` for a file that is there and cannot be
    believed — including one this version cannot read. There is no repair path
    and no default operation: an accept reads the replacement text from here,
    and a guessed one would rewrite a note on a coin toss.
    """
    if not proposal_id:
        raise NoteEditSidecarError("a note-edit proposal id is required")
    path = sidecar_path(config, workspace, proposal_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise NoteEditSidecarError(
            f"unreadable note-edit proposal {path}: {exc}"
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NoteEditSidecarError(
            f"malformed note-edit proposal {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise NoteEditSidecarError(
            f"note-edit proposal {path} is a {type(payload).__name__}, not an object"
        )
    schema = payload.get("schema")
    if isinstance(schema, bool) or schema != SIDECAR_SCHEMA:
        raise NoteEditSidecarError(
            f"note-edit proposal {path} carries schema {schema!r}, not "
            f"{SIDECAR_SCHEMA}, so it cannot be read"
        )
    proposal = _proposal_from_mapping(payload.get("proposal"), path)
    if proposal.id != proposal_id:
        raise NoteEditSidecarError(
            f"note-edit proposal {path} is filed under id {proposal.id!r}, not "
            f"{proposal_id!r}"
        )
    # The path is the field the accept writes, and it must be one this vault can
    # resolve. A record naming an absolute path or a `..` is refused here, with
    # this module's own name for it, rather than handed on to be discovered as
    # a note that could not be found.
    try:
        _check_key(proposal.relative_path)
    except nv.NoteCheckRefused as exc:
        raise NoteEditSidecarError(f"note-edit proposal {path}: {exc}") from exc
    return proposal


def write_sidecar(config: Any, proposal: NoteEditProposal) -> Path:
    """Write one filed proposal beside the queue; return the path.

    Atomic, under the same per-file queue lock every other managed writer of a
    vault's own bookkeeping takes, for the reason the queue is written that way:
    the accept reads this file to learn which note to rewrite and with what, and
    a half-written one is not "the previous record" but a truncated one.
    """
    path = sidecar_path(config, proposal.workspace, proposal.id)
    payload = {"schema": SIDECAR_SCHEMA, "proposal": proposal.as_dict()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with mr.queue_lock(path):
            tmp = path.with_name(f".{path.name}.write.tmp")
            try:
                tmp.write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp, path)
            except OSError:
                tmp.unlink(missing_ok=True)
                raise
    except OSError as exc:
        raise NoteEditRefused(f"could not write the note-edit sidecar: {exc}") from exc
    return path


def list_sidecars(config: Any, workspace: str) -> list[NoteEditProposal]:
    """Every filed proposal in this workspace's sidecar directory, oldest first.

    A record that cannot be read is skipped, not fatal: a corrupt file for one
    proposal must not stop a pass from settling the others, and an unreadable
    record is a proposal nobody is being asked about anyway. The queue rows are
    the thing a caller acts on, and those are parsed from the queue itself.
    """
    directory = sidecar_dir(config, workspace)
    try:
        names = sorted(p.name for p in directory.iterdir() if p.suffix == ".json")
    except OSError:
        return []
    proposals: list[NoteEditProposal] = []
    for name in names:
        try:
            proposal = read_sidecar(config, workspace, name[: -len(".json")])
        except NoteEditSidecarError as exc:
            logger.warning("ignoring %s", exc)
            continue
        if proposal is not None:
            proposals.append(proposal)
    return sorted(proposals, key=lambda item: (item.created_at, item.id))


# ── Filing ─────────────────────────────────────────────────────────────────


def _bullet_fields(
    relative_path: str,
    operation: str,
    expected_revision: str,
    reason: str,
    entry_identity: str = "",
) -> tuple[str, str]:
    """The one line a `note_edit` bullet carries, and its source tag.

    The revision is IN THE TEXT, and that is load-bearing rather than
    decorative. `append_proposals` dedupes a queued or already-promoted
    proposal on the bullet's text alone — `memory_proposals._existing_proposal_texts`
    matches everything after the bracketed head, so neither the payload nor the
    source tag is part of the key. A text that did not name the revision would
    therefore make an ACCEPTED edit permanently block the next one about the
    same note and the same reason, however much the note had changed since: a
    sidecar nobody is being asked about, and a check that stays pinned to a
    proposal id that is not in the queue.

    An entry operation's text names the entry too, for the same reason and one
    more: the head is the dedupe key, so two entries of one note at one revision
    whose reasons collapsed to the same words would be one question to the queue
    even though they are two to the person deciding. The identity's first twelve
    hex characters are what tells those two bullets apart, and they are the same
    twelve the worklist's own key is built from.

    It is also the honest thing for the row to say. A note edit is a verdict
    about one exact text, so the reviewer is entitled to see which one — and when
    the note has moved, the row's accept says so as a conflict rather than
    quietly rewriting what is there now.

    The text is built through :func:`ciao.memory_proposals._one_line`, the queue's
    own collapse, because ``append_proposals`` writes the bullet that way: a
    reason carrying a newline or a run of spaces reaches the file collapsed, and a
    text that was not built the same way is not the text the queue then holds —
    so :func:`_queued_row_id` would never match it, ``record_note_check`` would be
    skipped, and every pass would re-verify and re-file the same note. Built the
    same way, a multi-line reason is the same question as the one-line one, which
    is what the reviewer filed and what dedupe is entitled to call a duplicate.
    """
    from ciao.memory_proposals import _one_line

    head = f"{relative_path} — {operation} (rev {expected_revision[:8]}"
    if entry_identity:
        head = f"{head}, entry {entry_identity[:12]}"
    head = f"{head})"
    text = f"{head}: {reason}" if reason else head
    return _one_line(text), f"note verification · {operation}"



def _queued_row_id(
    config: Any, workspace: str, text: str, source: str
) -> str:
    """The queue row id for the bullet just written, or ``""`` if it is not there.

    Read back rather than assumed from the call, for the reason
    :func:`ciao.vocabulary_proposals.generate_category_proposals` does it:
    `append_proposals` dedupes the bullet text against the decision history as
    well as the live queue, so a bullet it declined to write would otherwise be
    reported as filed — and the check would then be pinned to a proposal id that
    names nothing.

    Compared through the queue's own :func:`ciao.memory_proposals._one_line` on
    both sides, because that is how the file was written: matching the raw text
    against a parsed bullet misses every reason carrying a newline or a run of
    spaces, and a miss here is not a cosmetic one — it is an unpinned check, so
    the next pass re-verifies and re-files a note that is already on file.
    """
    from ciao.memory_proposals import _one_line

    root = _vault_root(config, workspace)
    rel_path = Path(workspace).joinpath(*_QUEUE_RELATIVE).as_posix()
    try:
        queued = root.joinpath(*_QUEUE_RELATIVE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    wanted = _one_line(text)
    for entry in proposal_tracking.walk_proposal_queue(workspace, rel_path, queued):
        if (
            entry.bullet.kind == KIND
            and _one_line(entry.bullet.text) == wanted
            and entry.bullet.source == source
        ):
            return entry.proposal_id
    return ""


def _same_question(
    existing: NoteEditProposal, *, operation: str, after: str, stamp_date: str
) -> bool:
    """Whether a re-filing asks the question the record on file already asks.

    Only what ACCEPTING it would do is compared: the operation, the exact bytes
    it would write, and the date a re-stamp would stamp. The outcome, the
    coverage and the evidence explain the question rather than change the write,
    so a later pass that reached the same operation with better evidence is
    still the same question and re-arms the record rather than queuing a second
    row for it.
    """
    return (
        existing.operation == operation
        and existing.after == after
        and existing.stamp_date == stamp_date
    )


def _check_for(proposal: NoteEditProposal, *, today: date) -> nv.NoteCheck:
    """The check that filing *records*: this verdict, pinned to this revision,
    waiting on this proposal.

    The revision is the one the verification planned against, which is the
    revision the note is in — ``verify_note`` refuses a request whose
    ``expected_revision`` has moved before it decides anything. So a check
    written here describes the text a later pass will read, and the note is not
    asked about again while the proposal is unsettled.
    """
    return nv.NoteCheck(
        relative_path=proposal.relative_path,
        content_revision=proposal.expected_revision,
        outcome=proposal.outcome,
        checked_at=today,
        retry_after=today + timedelta(days=nv.CHECK_COOLDOWN_DAYS),
        evidence=proposal.evidence,
        coverage=proposal.coverage,
        reason=proposal.reason,
        proposal_id=proposal.proposal_id,
    )


def record_pending_check(
    root: Path, proposal: NoteEditProposal, *, today: date
) -> None:
    """Pin the check this proposal is waiting on, in whichever map owns it.

    The two halves write different records for one reason — 726-B judges a note
    and this proposal may be about one entry of it — and both suppress the same
    way: a ``proposal_id`` holds the question off while a person decides, whatever
    its cooldown says. Writing both from one place is what keeps a caller from
    having to remember which map a given operation lands in, and a proposal that
    pins neither is a proposal whose verdict is re-asked every night.
    """
    if proposal.operation in ENTRY_OPERATIONS:
        ev.record_entry_check(root, _entry_check_for(proposal, today=today))
        return
    nv.record_note_check(root, _check_for(proposal, today=today))


def _entry_check_for(proposal: NoteEditProposal, *, today: date) -> ev.EntryCheck:
    """The entry-level twin of :func:`_check_for`, keyed by the entry's identity.

    Same fields, same arguments, and the same reason they are the *entry's* words
    rather than the note's: the record describes one fact, and the fact's own
    fingerprint is what says whether the text a later pass opens is still the one
    somebody verified. A whole-note re-stamp moves the note's revision and this
    row does not care; a re-worded bullet changes this row's fingerprint and it
    stops suppressing, which is exactly when the question should come back.
    """
    return ev.EntryCheck(
        identity=proposal.entry_identity,
        note_path=proposal.relative_path,
        workspace=proposal.workspace,
        content_fingerprint=proposal.entry_fingerprint,
        outcome=proposal.outcome,
        checked_at=today,
        retry_after=today + timedelta(days=ev.CHECK_COOLDOWN_DAYS),
        evidence=proposal.evidence,
        coverage=proposal.coverage,
        reason=proposal.reason,
        proposal_id=proposal.proposal_id,
    )


def _entry_span(
    root: Path,
    *,
    key: str,
    revision: str,
    workspace: str,
    identity: str,
    fingerprint: str,
) -> tuple[int, int]:
    """Where the named entry sits in the note as it stands, or a refusal.

    Measured, never taken from the caller. Three things have to hold and all three
    are checked against the note's own bytes:

    * the note is still at ``revision``, because the span is only meaningful
      relative to the text the verdict was reached about, and a stale note's
      offsets describe different words;
    * an entry with this :func:`ciao.note_entries.entry_identity` is there at all
      — which is a question about the note, and the note may have been rewritten
      around the entry without the entry changing;
    * its :func:`ciao.note_entries.refresh_fingerprint` is the one the caller read.
      A re-*stamped* entry passes this, because the stamp is metadata; a re-worded
      one does not, and that is the difference between a re-stamp and a rewrite
      arriving wearing a re-stamp's name.

    All three refusals leave the file untouched, and a queue row whose span could
    not be measured is one an accept would have to refuse anyway — so the work is
    done once, here, where the reason can name what it was.
    """
    try:
        target = nr.resolve_note_path(root, key)
        text = target.read_bytes().decode("utf-8")
    except (mr.MemoryReceiptError, OSError, UnicodeDecodeError) as exc:
        raise NoteEditRefused(f"the note could not be read to place its entry: {exc}")
    if mr.content_revision(text) != revision:
        raise NoteEditRefused(
            f"{key} changed since this verdict was reached, so the entry's span "
            "cannot be recorded against text that is no longer there; nothing was "
            "filed, and the entry must be read again"
        )
    entry = nr.find_entry(text, identity=identity, note_path=key, workspace=workspace)
    if entry is None:
        raise NoteEditRefused(
            f"{key} holds no entry with identity {identity[:12]}, so there is no "
            "entry to propose an edit about; nothing was filed"
        )
    if entry.fingerprint != fingerprint:
        raise NoteEditRefused(
            f"the entry {identity[:12]} in {key} is not the text this verdict was "
            f"reached about (its fingerprint is {entry.fingerprint[:12]}, not "
            f"{fingerprint[:12]}); nothing was filed"
        )
    return entry.start, entry.end


def file_note_edit(
    config: Any,
    *,
    workspace: str,
    relative_path: str,
    expected_revision: str,
    operation: str,
    before: str,
    after: str,
    outcome: str,
    coverage: str,
    evidence: tuple[nv.Evidence, ...],
    reason: str,
    today: date | None = None,
    entry_identity: str = "",
    entry_fingerprint: str = "",
) -> NoteEditProposal:
    """File one typed `note_edit` proposal and pin the check that asked for it.

    The whole operation, in order: validate, resolve the entry if this is an entry
    operation, return the record already on file if this exact unit of work is
    already queued, write the sidecar, append the bullet, read the row id back,
    and record the check with it.

    The sidecar is written BEFORE the bullet, deliberately, the same way the
    category sidecar is: a sidecar with no bullet is litter this pass will
    overwrite, while a bullet with no sidecar is a row whose accept refuses and
    keeps — the direction that fails safe. It is written TWICE, and the second
    write is what records the queue row id: the id is not derivable here (it
    depends on the bullet's ordinal among identical ones in the queue), and a
    check pinned to a proposal id that names nothing would suppress the note for
    a month with nothing in the queue to settle.

    Idempotent per ``(relative_path, expected_revision)`` — and per
    ``entry_identity`` as well for an entry operation, so two bullets of one note
    at one revision are two questions. A second filing of the same verdict about
    the same text while its row is still queued returns the proposal already on
    file and writes nothing, so a nightly pass that reaches the same conclusion
    twice cannot leave the owner two identical rows to decide.

    A second filing that reaches a DIFFERENT verdict about that text — a
    retirement that became an update, or the other way round — is the case the id
    cannot answer on its own, because the id is the note and its revision and
    nothing about which question is being asked. So the row already in the queue
    wins until it is decided: the record is not overwritten, because the record
    is the operation an accept applies and the queued bullet names that record.
    Once the row is gone the record is re-armed from the verdict in hand — the
    sidecar's own order is the order it was first asked in, and nothing about the
    new question depends on the answer to the old one.

    **An entry operation reads the note, to record the span.** A whole-note
    operation needs no body: the ``after`` image it stores IS the write. An entry
    operation stores both images whole (see this module's docstring) *and* the
    slice of ``before`` the entry occupied, because an accept recovers the entry's
    own replacement from that slice rather than splicing the note at it. A span
    a caller supplied would be a span nobody checked, so the span is measured here
    from the note as it stands, and the note must still be at
    ``expected_revision`` for that measurement to describe the text the verdict
    was reached about. The entry must also still be there, under the identity the
    caller named, with the fingerprint they read. Both refusals are
    :class:`NoteEditRefused` and nothing is written, which is the direction that
    fails safe: a queue row nobody can act on is better than one that removes the
    wrong bullet.

    ``today`` is the verification's date and it is recorded, not merely used: it
    becomes a re-stamp's :attr:`NoteEditProposal.stamp_date`, so the accept
    stamps the day the verdict was reached rather than the day the button was
    pressed.

    Raises :class:`NoteEditRefused` for anything that is not a note edit that
    may be filed — see that class for the list. It never applies anything: the
    edit waits for a person, and a retirement is a human click all the way down.
    """
    day = today or date.today()
    operation = str(operation or "").strip()
    if operation not in OPERATIONS:
        raise NoteEditRefused(
            f"unknown note-edit operation {operation!r}; expected one of "
            f"{', '.join(OPERATIONS)}"
        )
    try:
        # The same refusal the check state applies, so a path that cannot key a
        # check cannot key a proposal either.
        key = _check_key(relative_path)
    except nv.NoteCheckRefused as exc:
        raise NoteEditRefused(str(exc)) from exc
    revision = str(expected_revision or "").strip()
    if not revision:
        raise NoteEditRefused(
            "an expected revision is required; a note edit is never planned "
            "against text nobody read"
        )
    if outcome not in nv.OUTCOMES:
        raise NoteEditRefused(
            f"unknown verification outcome {outcome!r}; a note edit is filed "
            f"from a needs_review verdict, whose outcomes are "
            f"{', '.join(nv.OUTCOMES)}"
        )
    coverage = str(coverage or "")
    if coverage not in (nv.COVERAGE_COMPLETE, nv.COVERAGE_PARTIAL):
        raise NoteEditRefused(
            f"unknown verification coverage {coverage!r}; expected "
            f"{nv.COVERAGE_COMPLETE!r} or {nv.COVERAGE_PARTIAL!r}"
        )
    if operation in (REPLACE, REPLACE_ENTRY) and not str(after or "").strip():
        # An update that leaves the note with no content is a deletion, which is
        # the one thing this path refuses to do at all — see
        # `note_verification._plan_update`, which returns the same verdict for
        # the same reason.
        raise NoteEditRefused(
            "a replace needs the note's exact replacement text; a replacement "
            "that empties the note is a deletion, which is never filed"
        )
    if operation in (RESTAMP, RESTAMP_ENTRY) and coverage != nv.COVERAGE_COMPLETE:
        raise NoteEditRefused(
            "a re-stamp claims the whole note is still true, so it is only filed "
            f"from complete coverage, not {coverage or 'nothing'}"
        )
    # No body for a whole-note retirement: keeping an after image would let a
    # reader diff an "edit" that is really a removal. An *entry* retirement keeps
    # one, because it is the note with that entry spliced out and the span is what
    # says so — see `entry_replacement`.
    after_text = "" if operation == RETIRE else str(after or "")
    # And the re-stamp's date is THIS verification's date, recorded rather than
    # left to the accept: the card is labelled `exact`, so the date the reviewer
    # read and the date the note gets must be the same string, and a click on the
    # other side of midnight would otherwise stamp a day nobody agreed to.
    stamp_date = day.isoformat() if operation in (RESTAMP, RESTAMP_ENTRY) else ""

    root = _vault_root(config, workspace)
    span: tuple[int, int] = (0, 0)
    identity = str(entry_identity or "").strip()
    if operation in ENTRY_OPERATIONS:
        if not identity:
            raise NoteEditRefused(
                f"a {operation} names an entry, so it needs that entry's identity; "
                "a splice with nothing to bind it to is an edit to no particular fact"
            )
        fingerprint = str(entry_fingerprint or "").strip()
        if not fingerprint:
            raise NoteEditRefused(
                f"a {operation} needs the entry fingerprint the verdict was reached "
                "about, so the accept can prove the entry is still the one that was "
                "judged"
            )
        span = _entry_span(
            root,
            key=key,
            revision=revision,
            workspace=workspace,
            identity=identity,
            fingerprint=fingerprint,
        )
    elif identity or str(entry_fingerprint or "").strip():
        raise NoteEditRefused(
            f"a {operation} is a whole-note edit and names no entry, so it cannot "
            "carry an entry identity or fingerprint"
        )

    proposal_id = note_edit_id(workspace, key, revision, identity)
    text, source = _bullet_fields(
        key, operation, revision, str(reason or ""), identity
    )

    existing = read_sidecar(config, workspace, proposal_id)
    if existing is not None and not existing.settled:
        if _same_question(
            existing, operation=operation, after=after_text, stamp_date=stamp_date
        ):
            if _queued_row_id(config, workspace, text, source):
                return existing
            # The record is on file but its bullet is gone — a dismissal straight
            # from the CLI, or a hand edit. Re-queue it below rather than leaving a
            # pending proposal nobody is being asked about.
        else:
            # A DIFFERENT verdict about the very same text, while the question the
            # record on file asks is still in the queue. The record IS the
            # operation an accept applies, so writing this one over it would leave
            # the queued bullet reading "replace" and resolve to a retirement
            # nobody was asked about — the one outcome worse than filing nothing.
            # The pending row wins until it is decided; a later pass re-arms the
            # record from the verdict in hand once that row is gone, which is the
            # same path a settled record takes.
            pending_text, pending_source = _bullet_fields(
                existing.relative_path,
                existing.operation,
                existing.expected_revision,
                existing.reason,
                existing.entry_identity,
            )
            if _queued_row_id(config, workspace, pending_text, pending_source):
                logger.info(
                    "note edit %s: a %s for %s is still queued, so this %s is not "
                    "filed over it",
                    proposal_id,
                    existing.operation,
                    key,
                    operation,
                )
                return existing

    proposal = NoteEditProposal(
        id=proposal_id,
        workspace=workspace,
        relative_path=key,
        operation=operation,
        expected_revision=revision,
        before=str(before or ""),
        after=after_text,
        outcome=outcome,
        coverage=coverage,
        evidence=tuple(evidence or ()),
        reason=str(reason or ""),
        stamp_date=stamp_date,
        entry_identity=identity,
        entry_fingerprint=str(entry_fingerprint or "").strip(),
        entry_span=span,
        # A re-filing rebuilds the record from the verdict in hand, because the
        # id is derived from the note and its revision and a later pass may have
        # reached a DIFFERENT verdict about that same text — a retirement that
        # became an update, say. Keeping the settled record's operation would
        # queue a row whose accept did something nobody was asked about.
        # `created_at` is the one field carried over: the sidecar's own order is
        # the order it was first asked in, which is the order a reader wants.
        created_at=existing.created_at if existing is not None else _now(),
    )
    if operation in ENTRY_OPERATIONS:
        # The same rules the reader re-asserts, run before the record is written
        # rather than only when somebody comes to accept it. A `retire_entry` that
        # also carries new text, or a `restamp_entry` that moves the words, is
        # refused here — so a queue row nobody can act on never reaches the owner.
        reason = _entry_refusal(proposal, "")
        if reason:
            raise NoteEditRefused(f"{reason}; nothing was filed")
    write_sidecar(config, proposal)

    from ciao.memory_proposals import MemoryProposal, append_proposals

    # `allow_dismissed=True` is load-bearing, and the reason is that a dismissal
    # here is NOT permanent (see this module's docstring): letting the queue's
    # own dedupe refuse it forever would make that sidecar the "rejected"
    # marker this design deliberately refuses to keep, and a note edited after a
    # refusal could never be proposed again. The live queue is still deduped on,
    # so two identical rows cannot sit there at once — and the text names the
    # revision, so "identical" cannot swallow a genuinely new question.
    append_proposals(
        [
            MemoryProposal(
                target=KIND,
                payload=proposal.id,
                text=text,
                source_section=source,
            )
        ],
        root,
        allow_dismissed=True,
    )
    row_id = _queued_row_id(config, workspace, text, source)
    if not row_id:
        logger.warning(
            "note edit %s: no queue row for %s; the proposal is on file but "
            "nothing is asking the owner about it",
            proposal.id,
            key,
        )
        return proposal
    proposal = replace(proposal, proposal_id=row_id)
    write_sidecar(config, proposal)
    record_pending_check(root, proposal, today=day)
    return proposal


# ── Settling ───────────────────────────────────────────────────────────────


def settle_note_edit(
    config: Any,
    workspace: str,
    proposal_id: str,
    *,
    accepted: bool,
    receipt_id: str = "",
    reason: str = "",
) -> NoteEditProposal:
    """Record the owner's decision on one filed proposal; return it settled.

    Two writes, and the order matters. The sidecar is settled FIRST, so a crash
    between the two leaves a record that says the row was decided (the caller
    removes the bullet either way, so nothing is re-asked) rather than a record
    that still claims to be pending. The check's ``proposal_id`` is cleared
    SECOND: that is the thing that suppresses the note while the proposal is
    open, and clearing it is what lets the note be asked about again once its
    cooldown ends — or immediately, because the revision no longer matches, the
    moment the note is edited.

    ``receipt_id`` is the note receipt an accept's write handed back, so an undo
    has something to point at. A decision already on record is returned
    unchanged: a retry must not rewrite the first decision's stamp or receipt.

    Deliberately no "refused forever" flag. A refusal the owner gave about last
    month's text says nothing about this month's, and a marker that outlived the
    revision would keep a note that was later corrected out of the queue for
    good. The check state's own cooldown is the honest, expiring record.
    """
    root = _vault_root(config, workspace)
    proposal = read_sidecar(config, workspace, proposal_id)
    if proposal is None:
        raise NoteEditSidecarError(
            f"no note-edit proposal filed under {proposal_id!r} in {workspace!r}"
        )
    if proposal.settled:
        return proposal
    settled = replace(
        proposal,
        settled=_now(),
        accepted=bool(accepted),
        receipt_id=str(receipt_id or ""),
        reason=str(reason or proposal.reason),
    )
    write_sidecar(config, settled)
    _clear_pending_check(root, settled)
    return settled


def _clear_pending_check(root: Path, proposal: NoteEditProposal) -> None:
    """Drop this proposal's hold on its check, if it still holds it.

    Which check that is depends on the operation: an entry edit holds an
    :class:`ciao.entry_verification.EntryCheck` off by the entry's identity, and a
    whole-note edit a :class:`ciao.note_verification.NoteCheck` off by the path.
    Both halves are in one place because the two are the same bookkeeping and
    forgetting one leaves a proposal whose verdict is re-asked every night for as
    long as its cooldown runs.

    Only a check that names THIS proposal is touched. Another note's check, another
    entry's check, and this unit's own next check (which may already have been
    filed since) are left exactly as they are — a settlement is not allowed to
    unblock something it knows nothing about.

    **The read and the write are one call.** The obvious spelling reads the row,
    compares it and writes it back, and a check recorded between those two halves
    is overwritten by the write — the settlement of one proposal erasing the
    cooldown of the next, which is precisely the row this function exists to let
    through. :func:`ciao.note_verification.update_check_state` is the one writer of
    the file and re-reads and rewrites the whole document under its own lock, so
    the comparison happens on the same snapshot the write is composed from. The
    mutate gets the raw document rather than a parsed row, so the comparison is on
    the stored ``proposal_id`` string: a row this version cannot parse is left
    alone rather than half-cleared, which is the direction a reader is supposed to
    fail in.
    """
    if not proposal.proposal_id:
        return
    held = proposal.proposal_id
    if proposal.operation in ENTRY_OPERATIONS:
        map_name, key = "entries", proposal.entry_identity
    else:
        map_name, key = "notes", proposal.relative_path

    def _mutate(document: dict[str, Any]) -> None:
        row = document.get(map_name, {}).get(key)
        if isinstance(row, dict) and str(row.get("proposal_id") or "") == held:
            row["proposal_id"] = ""

    try:
        nv.update_check_state(root, _mutate)
    except (nv.NoteCheckRefused, mr.MemoryReceiptError, mr.QueueLockError) as exc:
        # Reported, not raised: the sidecar is already settled and the queue row
        # is about to go, and a raised error here would tell the owner their
        # decision did not happen. The worst a caller loses by not clearing it
        # is one note's cooldown, which expires.
        logger.warning(
            "note edit %s: could not clear the pending check for %s: %s",
            proposal.id,
            proposal.relative_path,
            exc,
        )
