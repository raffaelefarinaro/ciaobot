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

**What is filed, and what is not.** Exactly one proposal per
``(note, expected_revision)``: the id is derived from those two, so a second
pass that reaches the same verdict about the same text finds the record it
already wrote and writes nothing. The proof that this actually happened is the
``proposal_id`` on the :class:`ciao.note_verification.NoteCheck` —
``_check_settles`` suppresses a revision that is waiting on a proposal whatever
its cooldown says, so filing without recording the id would let the next nightly
pass file a second row for a note that already has one.

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

from ciao import memory_receipts as mr
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

OPERATIONS = (REPLACE, RESTAMP, RETIRE)
"""The three operations a note edit can carry.

``replace`` is the agent's exact replacement text, ``restamp`` is a
verification date re-stamped on the note's own frontmatter, and ``retire`` is
the one operation that removes a note — attended only, never from a pass.
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
    emptying a note is the deletion this whole path refuses to perform), or a
    re-stamp that does not cover the note.
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

    ``settled`` is when the owner decided and ``accepted`` is which way, because
    a trashed note and a rewritten one are different outcomes and a record that
    cannot tell them apart says nothing. ``receipt_id`` is the note receipt an
    accept's write handed back, so an undo has something to point at.
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
        }


# ── Identity ───────────────────────────────────────────────────────────────


def note_edit_id(workspace: str, relative_path: str, expected_revision: str) -> str:
    """The stable sidecar id for one edit of one revision of one note.

    Derived from the three things that make the proposal what it is, the way
    :func:`ciao.proposal_tracking.stable_proposal_id` derives a queue row's: so
    a second pass reaching the same verdict about the same text computes the
    same id and finds the record it already wrote, and a note that changed
    computes a different one and is a new question. The revision is in the basis
    because a check — and this proposal — describes exactly one revision.
    """
    raw = f"{workspace}\x00{relative_path}\x00{expected_revision}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


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
)


def _proposal_from_mapping(raw: Any, where: Path) -> NoteEditProposal:
    """One stored row as a :class:`NoteEditProposal`, or a refusal.

    Strict rather than total, the other way round from
    :meth:`ciao.note_verification.Evidence.from_mapping`: that one drops a
    missing citation because an empty citation cannot carry an auto-apply,
    which is the direction the rule already fails toward. Here the unreadable
    field IS the accept — the operation and the replacement text are what get
    written — so a row missing one is refused rather than defaulted.
    """
    if not isinstance(raw, dict):
        raise NoteEditSidecarError(
            f"note-edit proposal {where} is a {type(raw).__name__}, not an object"
        )
    problems: list[str] = [
        f"{name} must be a string"
        for name in _STRING_FIELDS
        if not isinstance(raw.get(name), str)
    ]
    if not isinstance(raw.get("evidence"), list):
        problems.append("evidence must be a list of citations")
    if not isinstance(raw.get("accepted"), bool):
        problems.append("accepted must be a boolean")
    if problems:
        raise NoteEditSidecarError(f"note-edit proposal {where}: {'; '.join(problems)}")
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
    return proposal


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
    relative_path: str, operation: str, expected_revision: str, reason: str
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

    It is also the honest thing for the row to say. A note edit is a verdict
    about one exact text, so the reviewer is entitled to see which one — and when
    the note has moved, the row's accept says so as a conflict rather than
    quietly rewriting what is there now.
    """
    head = f"{relative_path} — {operation} (rev {expected_revision[:8]})"
    text = f"{head}: {reason}" if reason else head
    return text, f"note verification · {operation}"


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
    """
    root = _vault_root(config, workspace)
    rel_path = Path(workspace).joinpath(*_QUEUE_RELATIVE).as_posix()
    try:
        queued = root.joinpath(*_QUEUE_RELATIVE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for entry in proposal_tracking.walk_proposal_queue(workspace, rel_path, queued):
        if (
            entry.bullet.kind == KIND
            and entry.bullet.text == text
            and entry.bullet.source == source
        ):
            return entry.proposal_id
    return ""


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
) -> NoteEditProposal:
    """File one typed `note_edit` proposal and pin the check that asked for it.

    The whole operation, in order: validate, return the record already on file
    if this exact revision is already queued, write the sidecar, append the
    bullet, read the row id back, and record the check with it.

    The sidecar is written BEFORE the bullet, deliberately, the same way the
    category sidecar is: a sidecar with no bullet is litter this pass will
    overwrite, while a bullet with no sidecar is a row whose accept refuses and
    keeps — the direction that fails safe. It is written TWICE, and the second
    write is what records the queue row id: the id is not derivable here (it
    depends on the bullet's ordinal among identical ones in the queue), and a
    check pinned to a proposal id that names nothing would suppress the note for
    a month with nothing in the queue to settle.

    Idempotent per ``(relative_path, expected_revision)``: a second filing of
    the same verdict about the same text while its row is still queued returns
    the proposal already on file and writes nothing, so a nightly pass that
    reaches the same conclusion twice cannot leave the owner two identical rows
    to decide. Once that row is settled the record is re-armed from the verdict
    in hand, and the bullet is written again — the sidecar's own order is the
    order it was first asked in, and nothing about the new question depends on
    the answer to the old one.

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
    if operation == REPLACE and not str(after or "").strip():
        # An update that leaves the note with no content is a deletion, which is
        # the one thing this path refuses to do at all — see
        # `note_verification._plan_update`, which returns the same verdict for
        # the same reason.
        raise NoteEditRefused(
            "a replace needs the note's exact replacement text; a replacement "
            "that empties the note is a deletion, which is never filed"
        )
    if operation == RESTAMP and coverage != nv.COVERAGE_COMPLETE:
        raise NoteEditRefused(
            "a re-stamp claims the whole note is still true, so it is only filed "
            f"from complete coverage, not {coverage or 'nothing'}"
        )
    # No body for a retirement: keeping an after image would let a reader diff
    # an "edit" that is really a removal.
    after_text = "" if operation == RETIRE else str(after or "")

    root = _vault_root(config, workspace)
    proposal_id = note_edit_id(workspace, key, revision)
    text, source = _bullet_fields(key, operation, revision, str(reason or ""))

    existing = read_sidecar(config, workspace, proposal_id)
    if existing is not None and not existing.settled:
        if _queued_row_id(config, workspace, text, source):
            return existing
        # The record is on file but its bullet is gone — a dismissal straight
        # from the CLI, or a hand edit. Re-queue it below rather than leaving a
        # pending proposal nobody is being asked about.

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
        # A re-filing rebuilds the record from the verdict in hand, because the
        # id is derived from the note and its revision and a later pass may have
        # reached a DIFFERENT verdict about that same text — a retirement that
        # became an update, say. Keeping the settled record's operation would
        # queue a row whose accept did something nobody was asked about.
        # `created_at` is the one field carried over: the sidecar's own order is
        # the order it was first asked in, which is the order a reader wants.
        created_at=existing.created_at if existing is not None else _now(),
    )
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
    nv.record_note_check(root, _check_for(proposal, today=day))
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
    """Drop this proposal's hold on its note's check, if it still holds it.

    Only a check that names THIS proposal is touched. Another note's check, and
    this note's own next check (which may already have been filed since), are
    left exactly as they are — a settlement is not allowed to unblock something
    it knows nothing about.
    """
    check = nv.read_note_checks(root).get(proposal.relative_path)
    if check is None or not proposal.proposal_id:
        return
    if check.proposal_id != proposal.proposal_id:
        return
    try:
        nv.record_note_check(root, replace(check, proposal_id=""))
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
