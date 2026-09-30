"""One note's verification: the outcome, the evidence behind it, and the check
state that stops the question being asked twice.

:mod:`ciao.memory_audit.find_stale_notes` already *selects* the notes whose facts
have gone unverified past their type's horizon, and :mod:`ciao.note_receipts`
already writes one note atomically with a durable, undoable receipt. What sits
between them is the judgement, and this module is it: a typed request carrying
the agent's verdict for **one** note, checked against the autonomy rule, applied
through :func:`ciao.note_receipts.commit_note_change` when that rule allows it,
and recorded in a durable per-note check state either way.

Four outcomes, one rule each (:func:`plan_note_verification`, which decides and
writes nothing, so the worklist pass and the proposal writer can both ask what
*would* happen instead of each re-implementing the rule):

* ``still_valid`` — re-stamps the note's frontmatter ``updated:`` through
  :func:`ciao.vault_review._stamp_updated`, and **only** that. It requires
  complete coverage and at least one cited piece of evidence that names the
  note; without them the verdict is recorded as ``unverified``, because "nobody
  checked" is not "still true".
* ``update`` — applies the agent's exact replacement text, and only when every
  piece of evidence is a citation somebody could go and read (a ``source_ref``,
  the ``quoted`` text seen there, a ``supports`` clause). One good row among
  three uncited ones leaves two claims with nothing behind them, so the test is
  on the whole set, not on whether one row qualifies. An uncited edit is
  returned for a human, never written quietly.
* ``retire`` — **never** applied here. Retirement is a human decision, so it is
  returned as ``needs_review`` and nothing is written.
* ``unverified`` — no mutation and no proposal. A missing, unavailable or
  unreadable source (a connector that is down) is this outcome, not a failure:
  it is recorded with a cooldown so the next pass does not re-ask for
  :data:`CHECK_COOLDOWN_DAYS`.

Two absences are load-bearing. A missing citation is *not* a failure, and a
retire is *not* an update: an unattended pass must never turn "I could not
verify this" into a deletion, and must never turn "I could not verify this" into
a silent rewrite either.

And one check runs *before* the rule, not after it: the revision the caller
presents has to be the revision the note is in. Every outcome that records
something records it against the note as it stands, so a verdict about text
that has since been replaced would pin the *new* text for the length of the
cooldown — the one thing a durable check state must never do.

**Guardrails.** This service never deletes, trashes or archives — no primitive
for it is imported, and a ``retire`` reaches the same ``needs_review`` a note
with an empty replacement text reaches. It writes exactly one note: the one its
request names, through ``commit_note_change``, so every edit it makes is
undoable (``memory_receipts.undo_receipt``); the only other file it touches is
the check state below, which describes notes and holds none of their content. It
never touches a bounded memory region — those are
:mod:`ciao.memory_proposals`' writes, under the policy of
:mod:`ciao.memory_policy`. And it never *decides* a proposal: writing one is
#726-C, which will read the ``needs_review`` results this returns and set the
``proposal_id`` on the check they recorded.

**Check state.** One JSON object at ``<vault>/Workspace/Note-Checks.json``,
versioned and workspace-scoped — it belongs to the vault whose notes it
describes, and it is in :data:`ciao.fts_search.RESERVED_UNINDEXED_FILES`, so the
memory system's own paperwork never ranks against the memories it manages. It
stores no absolute path: a check is keyed by the vault-relative POSIX path the
receipt stores, so the file moves with the vault and never names where it used
to live. A check describes *one revision*, so an edited note is checked again
(:func:`should_check`) rather than inheriting a verdict nobody gave about its
current text.

**One document, two maps.** The file also holds the ``entries`` map
:mod:`ciao.entry_verification` keeps, keyed by
:func:`ciao.note_entries.entry_identity`, and :data:`CHECK_STATE_SCHEMA` stays
:data:`CHECK_STATE_SCHEMA` rather than being bumped for it: a reader that met an
unrecognized version drops *every* check in the file, so a bump would cost the
whole vault its cooldowns over a map that is purely additive. Both maps therefore
go through :func:`update_check_state` rather than a read-modify-write of their
own — two writers each rewriting the file from what they read would drop the
other map on every record, which is the same class of loss
:func:`update_check_state` refuses to risk.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from ciao import memory_receipts as mr
from ciao import note_receipts as nr

# The stamp itself belongs to `vault_review`: it is the same parse that decides
# what pressing *Still true* writes there, and that module already folded a
# second, drifting copy of the scan into this one for exactly this reason (see
# the comment on `_stamp_updated`). Imported rather than moved so the frontmatter
# stamp stays owned by the review pass and this module owns only the
# verification rule; the coupling is one-directional and pinned by
# `tests/test_note_verification.py`.
from ciao.vault_review import _stamp_updated

logger = logging.getLogger(__name__)


# ── Vocabulary ─────────────────────────────────────────────────────────────
#
# Strings, not an Enum: every one of them crosses into JSON (the check state, a
# receipt's provenance) and into a later child's proposal kind, so the wire
# spelling has to be a plain value the two vocabularies can share.

STILL_VALID = "still_valid"
UPDATE = "update"
RETIRE = "retire"
UNVERIFIED = "unverified"
"""The four outcomes a check may reach. ``unverified`` is one of them because
"I looked and could not confirm it" is a real answer, not a failed request: it
is what a connector being down looks like, and it is recorded with a cooldown
instead of being retried every pass."""

OUTCOMES = (STILL_VALID, UPDATE, RETIRE, UNVERIFIED)

COVERAGE_COMPLETE = "complete"
COVERAGE_PARTIAL = "partial"
"""How much of the note the judgement actually covered. A re-stamp claims the
whole note is still true, so it is only auto-applied from ``complete``: a
half-checked note is recorded as ``unverified`` and asked again."""

#: What a verification did. Deliberately a different vocabulary from the
#: outcomes above: an outcome is a claim about a note, a status is this
#: operation's report about itself. ``unverified`` is the one word both use, on
#: purpose — a verdict nobody could support and a pass that could not settle one
#: are the same answer, and giving them different words would invite a caller to
#: read the second as a failure worth retrying tonight.
APPLIED = "applied"
NEEDS_REVIEW = "needs_review"
CONFLICT = "conflict"
ALREADY_CHECKED = "already_checked"
FAILED = "failed"

#: What a plan would do, for a caller that wants the decision without the write.
AUTO_APPLY = "auto_apply"
PROPOSE = "propose"
RECORD_ONLY = "record_only"

NOTE_CHECKS_NAME = "Note-Checks.json"
CHECK_STATE_SCHEMA = 1
"""The check state's own version, so a reader that meets a shape it was not
written for can say so instead of guessing. An unrecognized version is treated
as no checks at all: that re-asks a note, which is the recoverable direction,
whereas guessing at a row would suppress a check that never happened.

The ``entries`` map added by :mod:`ciao.entry_verification` did **not** bump
this. It is purely additive — a document with both maps is the same document,
read by the same reader — and a bump would make every note's cooldown unreadable
to a version that has the note map and not the entry one."""

CHECK_COOLDOWN_DAYS = 30
"""How long one recorded check keeps a later pass from asking the same question
about the same revision. Long enough that a nightly run is not re-reading
evidence nobody changed, short enough that a note which went stale behind our
back is asked about again within a season."""

_ALREADY_CURRENT = "already_current"
"""`_stamp_updated`'s name for "there is nothing to write because the note
already carries today's date" — a verification that has done its job, as
distinct from ``no_frontmatter``, which is a note this service must not fix."""


class NoteCheckRefused(mr.MemoryReceiptError):
    """A check could not be stored because its target is not vault-relative.

    Raised by :func:`record_note_check` for a path that is absolute or escapes
    the vault. The check state is keyed by vault-relative path precisely so it
    can never carry an absolute one, and quietly normalizing the path instead
    would record a verdict under a key that does not name the note it is about.
    """


# ── The request and its evidence ───────────────────────────────────────────


def _text(value: Any) -> str:
    """A stored field as text, or ``""`` when it is not one."""
    return value if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class Evidence:
    """One cited source for one assertion in a note.

    ``source_type`` is the kind of place the evidence came from (``path``,
    ``chat``, ``calendar``, ``email``, ``url``), ``source_ref`` identifies it
    there — a vault-relative path, a message id, an opaque handle — and
    ``quoted`` is the text actually seen at that source. ``supports`` says which
    assertion it backs, and is what the re-stamp rule matches against the note's
    own name: an evidence row that does not say which note it is about supports
    nothing here.

    Empty fields are kept rather than dropped. A caller that could not reach the
    source still records the attempt, and the rule below refuses to auto-apply
    on the emptiness instead of the service guessing which field was the one
    that mattered.
    """

    source_type: str
    source_ref: str
    quoted: str
    supports: str
    observed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The row a receipt's provenance and the check state both store."""
        return {
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "quoted": self.quoted,
            "supports": self.supports,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> Evidence:
        """One evidence row read back out of the check state.

        Total by design: the sidecar is written by many passes over months, so a
        row that has lost a field reads as the empty string that field was
        rather than failing a whole pass's verification. An empty ``source_ref``
        cannot carry an auto-apply, which is the direction the rule already fails
        toward.
        """
        row = raw if isinstance(raw, dict) else {}
        return cls(
            source_type=_text(row.get("source_type")),
            source_ref=_text(row.get("source_ref")),
            quoted=_text(row.get("quoted")),
            supports=_text(row.get("supports")),
            observed_at=_text(row.get("observed_at")),
        )

    @classmethod
    def from_mappings(cls, rows: Any) -> tuple[Evidence, ...]:
        """Every evidence row in a stored list, or none when there is no list."""
        if not isinstance(rows, list):
            return ()
        return tuple(cls.from_mapping(row) for row in rows)


@dataclass(frozen=True, slots=True)
class NoteEdit:
    """A note's exact replacement, as the agent planned it.

    ``before`` is the full text the edit was planned against and ``after`` the
    full text that replaces it — full both ways, because a bounded patch is a
    different operation with different rules (and belongs to the region writers).
    """

    before: str
    after: str


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    """One note, one judgement, and the evidence behind it.

    ``expected_revision`` is the
    :func:`ciao.memory_receipts.content_revision` of the text the caller read,
    which is what makes the apply revision-checked: a note that moved since is a
    :data:`CONFLICT` with nothing written, never an overwrite. ``edit`` is
    required for an ``update`` and ignored elsewhere — a re-stamp's text is
    derived from the note itself, so a caller cannot smuggle an edit in behind a
    verdict that only claimed the note was still true.
    """

    workspace: str
    relative_path: str
    expected_revision: str
    outcome: str
    edit: NoteEdit | None = None
    evidence: tuple[Evidence, ...] = ()
    coverage: str = COVERAGE_PARTIAL
    reason: str = ""


# ── The decision ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    """What one request would do, decided without touching the vault.

    ``action`` is the only thing a caller acts on: :data:`AUTO_APPLY` means the
    text in ``after_text`` may go through ``commit_note_change``, :data:`PROPOSE`
    means a human decides (and #726-C files the proposal), and
    :data:`RECORD_ONLY` means nothing but the check state changes.
    ``status`` is what the operation would report, and ``outcome`` is the
    verdict actually recorded — which is not always the one requested, because a
    ``still_valid`` nobody could cite is recorded as ``unverified``. A
    :data:`FAILED` plan is a caller bug rather than a verdict and records
    nothing at all, so it can never suppress a later, correct re-ask.
    """

    action: str
    status: str
    outcome: str
    after_text: str
    reason: str

    def auto_applies(self) -> bool:
        """Whether this plan may write the note without a reviewer."""
        return self.action == AUTO_APPLY


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """What one verification did.

    ``receipt_id`` is the ``note_apply`` row's id when something was written and
    ``""`` otherwise; ``check`` is the state row this recorded (or the one that
    suppressed the work); ``message`` says which way it went and why, for a log
    line and for the human the review surface will show.
    """

    status: str
    receipt_id: str = ""
    check: NoteCheck | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class NoteCheck:
    """One note's last verification, as the check state stores it.

    ``content_revision`` is the revision the note was in *after* this operation:
    an applied edit leaves the note at the revision it produced, so the check
    describes exactly the text a later pass will read. That is what makes a
    stale check harmless — the moment the note changes, the revision no longer
    matches and the note is checked again.

    ``retry_after`` is the end of the cooldown in ISO-date form.
    ``proposal_id`` is set by #726-C when it files a proposal from a
    ``needs_review`` result; a check with one is suppressed until that proposal
    is settled, whatever its cooldown says.
    """

    relative_path: str
    content_revision: str
    outcome: str
    checked_at: date
    retry_after: date
    evidence: tuple[Evidence, ...] = ()
    coverage: str = ""
    reason: str = ""
    proposal_id: str = ""
    receipt_id: str = ""


# ── Evidence adequacy ──────────────────────────────────────────────────────


def _note_tokens(relative_path: str) -> tuple[str, ...]:
    """The spellings that count as naming this note, case-folded.

    A caller writes ``supports`` in its own words, so the match is against the
    note's relative path, the same path without its extension, and its file
    stem. It is a *word* match, not a substring one: a stem is a short enough
    string that containment would let ``al`` match the middle of ``calendar``,
    and a verdict that rested on a clause about somebody else's calendar would
    be a verdict about this note that nothing supports. ``-`` counts as part of a
    word so ``verdi`` cannot match inside ``vi-verdi`` either.
    """
    path = Path(str(relative_path or ""))
    tokens = {path.as_posix().casefold()}
    if path.suffix:
        tokens.add(path.with_suffix("").as_posix().casefold())
    if path.stem:
        tokens.add(path.stem.casefold())
    return tuple(sorted(token for token in tokens if token))


def _names_note(evidence: Evidence, relative_path: str) -> bool:
    """Whether one evidence row says which note's facts it supports.

    Each spelling has to appear as a whole word — bounded by anything that is
    not a word character or a hyphen — so only a clause that actually names this
    note, or one of its path segments, counts.
    """
    supports = evidence.supports.casefold()
    return any(
        re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", supports)
        for token in _note_tokens(relative_path)
    )


def _is_citation(evidence: Evidence) -> bool:
    """Whether one evidence row is a citation somebody could go and read.

    All three of ``source_ref``, ``quoted`` and ``supports`` or it is not a
    citation: a source nobody can re-open is an assertion, and a quoted text with
    no claim attached supports nothing in particular.
    """
    return bool(
        evidence.source_ref.strip()
        and evidence.quoted.strip()
        and evidence.supports.strip()
    )


def _supporting_evidence(request: VerificationRequest) -> tuple[Evidence, ...]:
    """The evidence rows that name this note and cite a source."""
    return tuple(
        row
        for row in request.evidence
        if _names_note(row, request.relative_path) and _is_citation(row)
    )


def _all_citations(request: VerificationRequest) -> bool:
    """Whether *every* evidence row is a citation, and there is at least one.

    Not "is there one". A note rewritten on the strength of evidence carries
    every row it was rewritten with, so a single uncited row in the set is a
    claim in the new text with nothing behind it — which is the outcome the
    update rule exists to prevent, and which an "at least one citation" test
    would wave through.
    """
    return bool(request.evidence) and all(
        _is_citation(row) for row in request.evidence
    )


def _missing_evidence_reason(request: VerificationRequest) -> str:
    """Why this request could not be applied unattended, in one sentence.

    Names the specific gap rather than "insufficient evidence", because the
    caller's next action differs: a missing ``source_ref`` is a citation to go
    and write, while partial coverage is a note to finish reading.
    """
    if not request.evidence:
        return "no evidence was cited for this note"
    if not any(row.source_ref.strip() for row in request.evidence):
        return "no cited source: every evidence row has an empty source_ref"
    if not any(row.quoted.strip() for row in request.evidence):
        return "no cited source: no evidence row quotes what was seen there"
    if not any(row.supports.strip() for row in request.evidence):
        return "no cited source: no evidence row says which assertion it supports"
    if not _supporting_evidence(request):
        return (
            "no evidence row names this note "
            f"({request.relative_path}), so nothing supports a re-stamp of it"
        )
    uncited = sum(1 for row in request.evidence if not _is_citation(row))
    if uncited:
        # Named per row rather than as "insufficient evidence": one good row in
        # the set does not make the uncited ones go away, and the caller's next
        # action is to write the citation that row is missing.
        return (
            f"{uncited} of {len(request.evidence)} evidence rows are not citations: "
            "each needs a source_ref, the quoted text seen there, and the "
            "assertion it supports"
        )
    return "the evidence cited does not cover the whole note"


# ── The autonomy rule ──────────────────────────────────────────────────────


_RETIRE_REASON = (
    "retirement is a human decision: this service never retires, deletes, "
    "trashes or archives a note, so the judgement is returned for review"
)


def _unknown_outcome(request: VerificationRequest) -> str:
    """Why this request names an outcome this version does not know, else ``""``.

    The one validation in :func:`plan_note_verification` that is not a rule but
    a guard against a caller bug, so it is a named predicate rather than an
    ``if`` buried in the middle: :func:`verify_note` asks it *before* it decides
    whether the note is due, so a garbage outcome is reported as the bug it is
    instead of coming back as "already checked" and hiding for a month behind a
    cooldown.
    """
    outcome = str(request.outcome or "").strip()
    if outcome in OUTCOMES:
        return ""
    return (
        f"unknown verification outcome {outcome!r}; nothing was recorded, so this "
        "is a caller bug rather than a verdict"
    )


def plan_note_verification(
    request: VerificationRequest, *, note_text: str, today: date
) -> VerificationPlan:
    """What this request would do, decided without writing anything.

    The single place the autonomy rule lives: :func:`verify_note` applies it,
    the curation worklist asks it which notes are worth looking at, and the
    proposal writer asks it whether a verdict is one it should file. Three
    callers, one rule, so none of them can drift into a stricter or a laxer
    version of it.

    ``note_text`` is the note's exact decoded text, which the caller has already
    read — a re-stamp is derived from it and an edit is checked against it. The
    function takes the text rather than the vault so that deciding cannot touch
    anything.
    """
    outcome = str(request.outcome or "").strip()
    unknown = _unknown_outcome(request)
    if unknown:
        return VerificationPlan(
            action=RECORD_ONLY,
            status=FAILED,
            outcome=outcome,
            after_text="",
            reason=unknown,
        )
    if outcome == RETIRE:
        return VerificationPlan(
            action=PROPOSE,
            status=NEEDS_REVIEW,
            outcome=RETIRE,
            after_text="",
            reason=_RETIRE_REASON,
        )
    if outcome == UNVERIFIED:
        return VerificationPlan(
            action=RECORD_ONLY,
            status=UNVERIFIED,
            outcome=UNVERIFIED,
            after_text="",
            # The caller's own reason when it gave one — "the calendar connector
            # was down" says more than anything this module could reconstruct —
            # and a plain statement otherwise.
            reason=request.reason
            or "no source this pass reached could support or contradict this note",
        )
    if outcome == STILL_VALID:
        return _plan_still_valid(request, note_text=note_text, today=today)
    return _plan_update(request, note_text=note_text)


def _plan_still_valid(
    request: VerificationRequest, *, note_text: str, today: date
) -> VerificationPlan:
    """The re-stamp: a verdict about the whole note, or nothing.

    A re-stamp asserts every claim in the note is still true, which is only
    worth writing when the check actually covered the note and cited something
    that supports it. Without that it is recorded as ``unverified`` — the same
    answer a missing connector gets — rather than as a verification nobody made.
    """
    if request.coverage != COVERAGE_COMPLETE:
        return VerificationPlan(
            action=RECORD_ONLY,
            status=UNVERIFIED,
            outcome=UNVERIFIED,
            after_text="",
            reason=(
                "a re-stamp claims the whole note is still true, but this check "
                f"covered {request.coverage or 'nothing'} of it"
            ),
        )
    if not _supporting_evidence(request):
        return VerificationPlan(
            action=RECORD_ONLY,
            status=UNVERIFIED,
            outcome=UNVERIFIED,
            after_text="",
            reason=_missing_evidence_reason(request),
        )
    stamped, stamp_status = _stamp_updated(note_text, today.isoformat())
    if stamped is None:
        if stamp_status == _ALREADY_CURRENT:
            # Nothing to write, but the verification happened and is still
            # worth a receipt: `commit_note_change` records the row with
            # `changed=False`, which is exactly this case.
            return VerificationPlan(
                action=AUTO_APPLY,
                status=APPLIED,
                outcome=STILL_VALID,
                after_text=note_text,
                reason="the note already carries today's verification date",
            )
        return VerificationPlan(
            action=PROPOSE,
            status=NEEDS_REVIEW,
            outcome=STILL_VALID,
            after_text="",
            reason=(
                "the note has no frontmatter `updated:` to stamp; adding one "
                "would restructure a file this service was asked to verify"
            ),
        )
    return VerificationPlan(
        action=AUTO_APPLY,
        status=APPLIED,
        outcome=STILL_VALID,
        after_text=stamped,
        reason="",
    )


def _plan_update(request: VerificationRequest, *, note_text: str) -> VerificationPlan:
    """An update: the agent's exact replacement, or a human's decision.

    An update rewrites claims, so every claim it writes has to name a source
    somebody can re-open, and every row it was rewritten *with* has to be such a
    citation — one good row among three uncited ones leaves two claims in the
    new text with nothing behind them, which is the outcome this rule exists to
    prevent: the edit comes back as ``needs_review`` and nothing is written.

    The replacement is the caller's, verbatim. In particular it is **not**
    re-stamped here: the caller knows what it changed and what the note's
    ``updated:`` should therefore say, and a service that rewrote the text it
    was handed would stop writing the exact bytes the caller approved. A caller
    must therefore carry the fresh ``updated:`` into its own replacement —
    otherwise the note reads as stale the moment it is applied, and the next
    audit lists it again.
    """
    edit = request.edit
    if edit is None or not edit.after.strip():
        return VerificationPlan(
            action=PROPOSE,
            status=NEEDS_REVIEW,
            outcome=UPDATE,
            after_text="",
            reason=(
                "an update needs the note's exact replacement text"
                if edit is None
                else "an update that leaves the note with no content is a "
                "deletion, which this service never performs"
            ),
        )
    if not _supporting_evidence(request) or not _all_citations(request):
        return VerificationPlan(
            action=PROPOSE,
            status=NEEDS_REVIEW,
            outcome=UPDATE,
            after_text="",
            reason=_missing_evidence_reason(request),
        )
    if edit.before and edit.before != note_text:
        return VerificationPlan(
            action=RECORD_ONLY,
            status=CONFLICT,
            outcome=UPDATE,
            after_text="",
            reason=(
                "the note no longer holds the text this edit was planned "
                "against; nothing was written and no check was recorded"
            ),
        )
    return VerificationPlan(
        action=AUTO_APPLY,
        status=APPLIED,
        outcome=UPDATE,
        after_text=edit.after,
        reason="",
    )


# ── Check state ────────────────────────────────────────────────────────────


def note_check_state_path(vault_root: Path | str) -> Path:
    """Where one workspace's note-check state lives: ``<vault>/Workspace/Note-Checks.json``.

    Scoped to the vault rather than to a workspace name, because a workspace's
    notes are in its vault and the state describes those notes. The file is the
    memory pipeline's own bookkeeping, so it is in
    :data:`ciao.fts_search.RESERVED_UNINDEXED_FILES` and never enters recall.
    """
    return Path(vault_root) / "Workspace" / NOTE_CHECKS_NAME


def _stored_key(relative_path: str) -> str:
    """One vault-relative POSIX path, or a refusal.

    Absolute and ``..``-bearing paths are refused rather than normalized: this
    file is keyed by the same relative path ``note_receipts`` records, so a
    caller storing anything else would file the verdict under a key that does not
    name the note it is about.
    """
    raw = str(relative_path or "").strip()
    candidate = Path(raw)
    if (
        not raw
        or candidate.is_absolute()
        or raw.startswith(("/", "\\"))
        or candidate.drive
        or any(part == ".." for part in candidate.parts)
    ):
        raise NoteCheckRefused(
            f"a note check must be keyed by a vault-relative path: {relative_path!r}"
        )
    return candidate.as_posix()


def _check_payload(check: NoteCheck) -> dict[str, Any]:
    """One check as the sidecar stores it."""
    return {
        "relative_path": check.relative_path,
        "content_revision": check.content_revision,
        "outcome": check.outcome,
        "checked_at": check.checked_at.isoformat(),
        "retry_after": check.retry_after.isoformat(),
        "evidence": [row.as_dict() for row in check.evidence],
        "coverage": check.coverage,
        "reason": check.reason,
        "proposal_id": check.proposal_id,
        "receipt_id": check.receipt_id,
    }


def _stored_date(raw: Any) -> date | None:
    """A stored ISO date, or ``None`` when it is not one."""
    text = _text(raw).strip()
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _check_from_mapping(key: str, raw: Any) -> NoteCheck | None:
    """One stored row as a :class:`NoteCheck`, or ``None`` when unusable.

    The key is authoritative for the path and the row's own copy is ignored: the
    map is what a reader looks the note up by, so a row disagreeing with it is a
    row to drop rather than a second truth about where the note lives. A row with
    no readable revision or date is dropped for the same reason — a check nobody
    can place against the note in front of them suppresses nothing, and
    honouring it would be taking a verdict on trust.
    """
    if not isinstance(raw, dict):
        return None
    revision = _text(raw.get("content_revision")).strip()
    checked_at = _stored_date(raw.get("checked_at"))
    retry_after = _stored_date(raw.get("retry_after"))
    if not revision or checked_at is None:
        logger.debug(
            "note checks: dropping the row for %s, which names no revision or date", key
        )
        return None
    return NoteCheck(
        relative_path=key,
        content_revision=revision,
        outcome=_text(raw.get("outcome")).strip(),
        checked_at=checked_at,
        # A row written before the cooldown existed suppresses nothing rather
        # than suppressing forever.
        retry_after=retry_after or checked_at,
        evidence=Evidence.from_mappings(raw.get("evidence")),
        coverage=_text(raw.get("coverage")),
        reason=_text(raw.get("reason")),
        proposal_id=_text(raw.get("proposal_id")).strip(),
        receipt_id=_text(raw.get("receipt_id")).strip(),
    )


def _readable_note_rows(
    path: Path, notes: dict[str, Any]
) -> dict[str, NoteCheck]:
    """The ``notes`` map's readable rows, keyed by the stored path.

    The key is authoritative for the path and the row's own copy is ignored, so a
    row disagreeing with the map it is filed under is a row to drop rather than a
    second truth about where the note lives. A key that is not a vault-relative
    path, and a row with no readable revision or date, are dropped for the same
    reason: a check nobody can place against the note in front of them suppresses
    nothing, and honouring it would be taking a verdict on trust.
    """
    checks: dict[str, NoteCheck] = {}
    for key, row in notes.items():
        if not isinstance(key, str):
            continue
        try:
            stored_key = _stored_key(key)
        except NoteCheckRefused:
            logger.warning("note checks: %s holds an unusable key %r", path, key)
            continue
        check = _check_from_mapping(stored_key, row)
        if check is not None:
            checks[stored_key] = check
    return checks


def _empty_check_document() -> dict[str, Any]:
    """The document a vault with no check state yet reads as."""
    return {"schema": CHECK_STATE_SCHEMA, "notes": {}, "entries": {}}


def _load_check_document(path: Path) -> tuple[dict[str, Any], str]:
    """The whole check-state document, and why the file was skipped if it was.

    Both maps come back RAW — unparsed, unfiltered — because a writer rewrites
    the file from what it read, and a writer that had parsed only its own map
    would drop the other's rows on every single record. ``note_verification``
    owns the ``notes`` rows and :mod:`ciao.entry_verification`` the ``entries``
    ones; each parses its own half through this.

    A missing ``entries`` map is not a reason to skip anything: it is what a file
    written before entries existed looks like, and a reader that demanded one
    would throw away every note check in a vault the moment the two maps were
    split across versions.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_check_document(), ""
    except (OSError, UnicodeDecodeError) as exc:
        return _empty_check_document(), f"{path} could not be read ({exc})"
    try:
        payload = json.loads(raw_text)
    except ValueError as exc:
        return _empty_check_document(), f"{path} is not valid JSON ({exc})"
    if not isinstance(payload, dict):
        return _empty_check_document(), f"{path} is not a JSON object"
    schema = payload.get("schema")
    if isinstance(schema, bool) or schema != CHECK_STATE_SCHEMA:
        return _empty_check_document(), (
            f"{path} carries schema {schema!r}, not {CHECK_STATE_SCHEMA}, so its "
            "checks cannot be read"
        )
    notes = payload.get("notes")
    if not isinstance(notes, dict):
        return _empty_check_document(), f"{path} holds no note map"
    entries = payload.get("entries")
    return {
        "schema": CHECK_STATE_SCHEMA,
        "notes": dict(notes),
        "entries": dict(entries) if isinstance(entries, dict) else {},
    }, ""


def read_note_checks(vault_root: Path | str) -> dict[str, NoteCheck]:
    """Every recorded check in one vault, keyed by vault-relative note path.

    Never raises and never fails a caller: a missing file is an empty map, and a
    file that is unreadable, truncated, from a newer schema or holding a row
    this version cannot read is logged and skipped. Every one of those cases
    means the same thing to a caller — no suppression — which is the safe
    direction, because re-asking a note costs a pass while silently honouring a
    check we could not read would claim a verification nobody made.
    """
    path = note_check_state_path(vault_root)
    document, skipped = _load_check_document(path)
    if skipped:
        logger.warning("note checks: %s", skipped)
        return {}
    return _readable_note_rows(path, document["notes"])


def update_check_state(
    vault_root: Path | str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    """Read-modify-write the whole check-state document under its queue lock.

    The one writer of this file, and the reason there is exactly one. Both maps
    are rewritten from what was read, so a writer that had parsed only its own
    half would drop the other half's cooldowns on every single record, which is
    the loss that makes writing over state this version cannot read a refusal
    rather than a fallback. The callback gets the live document and mutates it
    in place; the file is then written through
    :func:`ciao.memory_receipts.write_queue_atomically` under that module's
    per-file lock, so a read-modify-write without it cannot drop whichever check
    landed in between.

    The ``notes`` map is normalized on the way through, so a file that has
    accumulated a row this version cannot read is not stuck behind it. A reader
    is the opposite: it keeps the rest and skips the row, because a reader drops
    one row at a time while a writer rewrites the whole file.

    Raises :class:`NoteCheckRefused` for state this version cannot read, having
    written nothing, so a foreign schema is left exactly as it is instead of being
    replaced by a file that describes none of it. A callback that raises has
    already left the file untouched for the same reason.
    """
    path = note_check_state_path(vault_root)
    with mr.queue_lock(path):
        document, skipped = _load_check_document(path)
        if skipped:
            raise NoteCheckRefused(
                f"refusing to write a note check over state this version cannot "
                f"read: {skipped}. Nothing was written, so no other check was lost."
            )
        document["notes"] = {
            name: _check_payload(row)
            for name, row in _readable_note_rows(path, document["notes"]).items()
        }
        mutate(document)
        mr.write_queue_atomically(
            path,
            json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        )


def record_note_check(vault_root: Path | str, check: NoteCheck) -> None:
    """Store one check, replacing any earlier check of the same note.

    The whole map is read, one row replaced and the file rewritten under
    :func:`ciao.memory_receipts.queue_lock`: the state is shared by every pass
    over the vault, so a read-modify-write without the lock would drop whichever
    check landed between this read and this write. A note keeps one check, not a
    history — what happened to it is in the receipts.

    Raises :class:`NoteCheckRefused` rather than writing over a state file this
    version cannot read (see :func:`update_check_state`).
    """
    key = _stored_key(check.relative_path)
    row = _check_payload(replace(check, relative_path=key))

    def _mutate(document: dict[str, Any]) -> None:
        notes = document.setdefault("notes", {})
        notes[key] = row

    update_check_state(vault_root, _mutate)


def _check_settles(check: NoteCheck, current_revision: str, *, today: date) -> bool:
    """Whether one recorded check already settles the question being asked.

    Two ways it does not: the note changed, so the check described text that is
    no longer there; or its cooldown has run out and the claims behind it may
    have gone stale since. A pending proposal settles the question whatever the
    cooldown says — it is waiting to be settled, and asking again would only
    produce a second one.
    """
    if check.content_revision != str(current_revision or ""):
        return False
    if check.proposal_id:
        return True
    return today < check.retry_after


def should_check(
    vault_root: Path | str,
    relative_path: str,
    current_revision: str,
    *,
    today: date,
) -> bool:
    """Whether this note, as it stands now, is due to be checked again.

    The one question the worklist asks before doing any work. False while a
    check for the *same* revision is inside its cooldown or is waiting on a
    proposal; True as soon as the revision differs, because a note that changed
    has new claims nobody verified.

    ``relative_path`` is looked up exactly as given, against the keys
    :func:`note_check_state_path` stores — the vault-relative POSIX path. It is
    a predicate over the sidecar and reads nothing from the vault, so a caller
    can ask it about a note it has not resolved yet.
    """
    key = str(relative_path or "").strip()
    check = read_note_checks(vault_root).get(key)
    return check is None or not _check_settles(check, current_revision, today=today)


# ── Verification ───────────────────────────────────────────────────────────


def _foreign_workspace_vault(config: Any, workspace: str, root: Path) -> str:
    """Why *root* is not the vault this workspace's notes live in, or ``""``.

    The check state lives in the vault it describes, so a request that paired
    one workspace's name with another workspace's vault would record its verdict
    in the wrong vault, and the workspace the caller meant would go on being
    asked about the same note forever. ``config`` is the install's own answer to
    where a workspace's notes live, and there is no answer from it that counts as
    permission: a config that cannot be asked is refused rather than waved
    through, because the alternative is a check filed in a vault nobody claimed.
    """
    getter = getattr(config, "workspace_vault_root", None)
    if not callable(getter):
        return (
            f"this config does not answer where workspace {workspace!r} keeps its "
            f"notes (workspace_vault_root is not callable), so {root} cannot be "
            "confirmed as its vault"
        )
    try:
        configured = Path(getter(workspace)).resolve()
    except (OSError, TypeError, ValueError) as exc:
        return f"the vault configured for workspace {workspace!r} is unusable ({exc})"
    if configured == root:
        return ""
    return (
        f"{root} is not the vault configured for workspace {workspace!r} ({configured})"
    )


def _provenance(
    request: VerificationRequest, plan: VerificationPlan, *, today: date
) -> dict[str, Any]:
    """The evidence chain stamped onto an applied receipt.

    Everything a reader of History needs to judge the write without re-running
    it: which outcome was claimed, whether the rule auto-applied it or a human
    was asked, how much of the note the check covered, and the citations it
    rested on.
    """
    return {
        "outcome": plan.outcome,
        "action": plan.action,
        "coverage": request.coverage,
        "reason": request.reason,
        "checked_at": today.isoformat(),
        "evidence": [row.as_dict() for row in request.evidence],
    }


def _new_check(
    request: VerificationRequest,
    plan: VerificationPlan,
    *,
    key: str,
    revision: str,
    receipt_id: str,
    today: date,
) -> NoteCheck:
    """The check one outcome leaves behind, before it is stored.

    The reason is the caller's when the recorded outcome is the one it asked
    for, and the rule's own when the rule downgraded it: a ``still_valid`` that
    could not be cited is stored as ``unverified``, and "verified against the
    March invoice" beside that outcome would be a claim this module cannot
    support about a verdict it refused to make.
    """
    requested = str(request.outcome or "").strip()
    reason = (
        plan.reason
        if plan.outcome != requested
        else request.reason or plan.reason
    )
    return NoteCheck(
        relative_path=key,
        content_revision=revision,
        outcome=plan.outcome,
        checked_at=today,
        retry_after=today + timedelta(days=CHECK_COOLDOWN_DAYS),
        evidence=request.evidence,
        coverage=request.coverage,
        reason=reason,
        receipt_id=receipt_id,
    )


def _store(vault_root: Path, check: NoteCheck) -> tuple[NoteCheck, str]:
    """Store one check; return it with the reason it could not be stored.

    A lock it cannot take is reported rather than raised, unlike a note write's.
    The sidecar is a ledger, and the worst a caller loses by not writing it is a
    note that gets asked about again; the note write this may already have
    performed is journaled under its own lock, with the receipt as its recovery
    evidence, and raising out of here would report a *failure* for a mutation
    that did land.
    """
    try:
        record_note_check(vault_root, check)
    except (mr.MemoryReceiptError, mr.QueueLockError, OSError) as exc:
        logger.error("note verification: could not record the check: %s", exc)
        return check, str(exc)
    return check, ""


def verify_note(
    request: VerificationRequest,
    *,
    vault_root: Path | str,
    config: Any,
    actor: str = "agent",
    source: str = "curation",
    today: date | None = None,
) -> VerificationResult:
    """Record one note's verdict, applying the edit only when the rule allows it.

    The whole operation in order: refuse a vault that is not this workspace's,
    read the note's exact bytes, skip the work if a check for *this* revision
    already answers it, compare the revision the caller read, ask
    :func:`plan_note_verification` what the rule says, and then either write
    through :func:`ciao.note_receipts.commit_note_change` — the only mutation this
    service performs, so the edit is journaled and undoable — or record the
    verdict and change nothing else.

    What comes back is the honest report of which of those happened:

    * ``applied`` — the note was written (or already held the text), with the
      receipt id and a check describing the revision it left behind.
    * ``needs_review`` — the rule refused to write this unattended. No receipt,
      no proposal; a caller with one (#726-C) files it.
    * ``unverified`` — no evidence, or a check that did not cover the note.
      Nothing written, recorded with a cooldown so the next pass does not ask
      again this month.
    * ``conflict`` — the note moved since the caller read it, or an update was
      planned against text it no longer holds. The revision is compared before
      the rule runs, so this is reached whether or not the verdict would have
      written anything. Nothing written and **no check recorded**, so the caller
      can re-read and decide again.
    * ``already_checked`` — a check for this exact revision is in its cooldown or
      awaiting a proposal. A no-op: nothing written, no receipt, and the check
      left exactly as it was.
    * ``failed`` — the note could not be used at all (unreadable, not a writable
      note in this vault, the journal could not record it, or the outcome was not
      one of the four). Distinct from ``unverified`` on purpose: an unavailable
      source is a verdict about the note, and an unwritable note is not.

    Never raises for any of those; a caller running a pass over a vault gets a
    result per note.
    """
    day = today or date.today()
    try:
        root = nr.canonical_vault(vault_root)
    except mr.MemoryReceiptError as exc:
        return VerificationResult(status=FAILED, message=f"unusable vault root: {exc}")
    foreign = _foreign_workspace_vault(config, request.workspace, root)
    if foreign:
        return VerificationResult(status=FAILED, message=foreign)
    try:
        target = nr.resolve_note_path(root, request.relative_path)
    except mr.MemoryReceiptError as exc:
        return VerificationResult(status=FAILED, message=str(exc))
    # The same spelling `note_receipts` records the receipt under, so the check
    # state is keyed exactly as a reader looks a note up in it.
    key = target.relative_to(root).as_posix()
    try:
        note_text = target.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return VerificationResult(
            status=FAILED, message=f"the note could not be read as UTF-8: {exc}"
        )
    current = mr.content_revision(note_text)

    # A caller bug is reported before anything else, including before the
    # cooldown: an outcome this version cannot interpret must say so, not hide
    # behind "already checked" for the rest of the month.
    unknown = _unknown_outcome(request)
    if unknown:
        return VerificationResult(status=FAILED, message=unknown)

    existing = read_note_checks(root).get(key)
    if existing is not None and _check_settles(existing, current, today=day):
        # First, and deliberately ahead of the revision check below: this
        # short-circuit writes and records nothing, so the note keeps the verdict
        # it already has for the revision it is in. A caller re-sending the same
        # request therefore learns it was settled, which is the answer it wanted.
        return VerificationResult(
            status=ALREADY_CHECKED,
            check=existing,
            message=(
                f"{key} was already checked as {existing.outcome} on "
                f"{existing.checked_at.isoformat()}; nothing was asked or written"
            ),
        )

    # The revision is checked before the verdict is decided, not only before the
    # write. A verdict reached about a revision the note is no longer in would
    # otherwise be recorded against the text that is actually there and suppress
    # it for the whole cooldown — and the outcomes that write nothing are exactly
    # the ones that reach this check by recording.
    expected = str(request.expected_revision or "").strip()
    if not expected:
        # Not a stale revision but a missing one: a caller that cannot say what
        # it read is asking for a judgement on text it never saw, which is what
        # `commit_note_change` refuses as a blind overwrite. Said here, in the
        # protocol's own terms, rather than reported as a conflict it is not.
        return VerificationResult(
            status=FAILED,
            message=(
                "an expected revision is required; this service never judges a "
                "note the caller has not read"
            ),
        )
    if expected != current:
        return VerificationResult(
            status=CONFLICT,
            message=(
                "the note changed since this request was planned, so its verdict "
                "is about text that is no longer there; nothing was written and no "
                "check was recorded, and the caller must read the note again"
            ),
        )

    plan = plan_note_verification(request, note_text=note_text, today=day)
    if plan.status == FAILED:
        # A caller bug, not a verdict: nothing is written and nothing recorded,
        # so nothing can suppress a later, correct re-ask of this note.
        return VerificationResult(status=FAILED, message=plan.reason)
    if not plan.auto_applies():
        if plan.status == CONFLICT:
            return VerificationResult(status=CONFLICT, message=plan.reason)
        check, failure = _store(
            root,
            _new_check(
                request, plan, key=key, revision=current, receipt_id="", today=day
            ),
        )
        if failure:
            return VerificationResult(status=FAILED, message=failure)
        return VerificationResult(status=plan.status, check=check, message=plan.reason)

    try:
        receipt = nr.commit_note_change(
            vault_root=root,
            relative_path=key,
            expected_revision=request.expected_revision,
            after_text=plan.after_text,
            actor=actor,
            source=source,
            workspace=request.workspace,
            provenance=_provenance(request, plan, today=day),
        )
    except mr.RevisionConflict as exc:
        # The note moved under a read nobody rechecked. Nothing was written and
        # nothing is recorded, so the caller can re-read it and judge the text
        # that is actually there.
        return VerificationResult(status=CONFLICT, message=str(exc))
    except (mr.MemoryReceiptError, mr.QueueLockError, OSError) as exc:
        # A journal that would not record, a lock this thread could not take, a
        # filesystem that said no. All three are reported rather than raised:
        # this function's contract is one result per note, and a caller running
        # a pass over a vault must not lose a whole run to one unwritable file.
        return VerificationResult(status=FAILED, message=str(exc))
    receipt_id = str(receipt.get("id", ""))
    # The check describes the revision this operation LEFT the note at, not the
    # one it read: the text a later pass opens is the replacement, and a check
    # pinned to the revision that was just replaced would suppress nothing while
    # looking exactly like a verification.
    check, failure = _store(
        root,
        _new_check(
            request,
            plan,
            key=key,
            revision=mr.content_revision(plan.after_text),
            receipt_id=receipt_id,
            today=day,
        ),
    )
    return VerificationResult(
        status=APPLIED,
        receipt_id=receipt_id,
        check=check,
        message=failure
        or plan.reason
        or f"{plan.outcome} applied to {key}",
    )