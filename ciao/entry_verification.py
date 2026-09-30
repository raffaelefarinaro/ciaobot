"""One *entry's* verification: a single fact's verdict, and the state that stops
it being asked twice.

:mod:`ciao.note_verification` answers a question about a whole note, and a note
has no single age: the office's address may be wrong while the landlord's name is
still right, and a re-stamp of the frontmatter ``updated:`` claims the whole file
was checked. This module is the same service narrowed to the unit a person
actually maintains — one Markdown list item — and it exists because two of the
whole-note path's properties are wrong for that unit:

* the **key**. A whole-note check is keyed by path, so a note that gains an
  unrelated fact loses its verdict and is asked again, and one note cannot hold
  two verdicts about two facts. :func:`ciao.note_entries.entry_identity` keys on
  the fact instead, and deliberately on no line number and no offset, so a
  verdict survives a note being reordered and a fact being inserted above it.
* the **mutation**. Rewriting a whole note to correct one bullet rewrites every
  other bullet with it, and removing one bullet by trashing the file removes the
  note. :func:`ciao.note_receipts.apply_entry_edit` splices exactly the entry's
  span, through the same whole-note receipt transaction, so the undo is whole-note
  and exact.

**Same rule, one unit finer.** The autonomy rule is 726-B's, unchanged and
deliberately not re-invented: a ``still_valid`` needs complete coverage of the
entry and a citation somebody could go and read (:mod:`ciao.note_verification`'s
:func:`_supporting_evidence`, re-used rather than copied, so an entry and a note
cannot drift into different standards); an ``update`` needs *every* evidence row
to be such a citation; a ``retire`` is **never** applied here — it is
``needs_review`` and a human deletes the span; an ``unverified`` records a
cooldown so a connector that is down is not retried every night. The vocabulary
is the same module's, imported rather than restated, because both halves of this
write the same JSON and a second set of spellings would be a second set of
mistakes.

**What the check state is.** One row per entry, in the ``entries`` map of the
same ``Workspace/Note-Checks.json`` the note checks live in
(:func:`ciao.note_verification.update_check_state` is the only writer of that
file, and both maps go through it, so neither can drop the other's cooldowns). It
stores no note text and no absolute path: ``note_path`` is the vault-relative
POSIX path the receipt records, so the state travels with the vault, and the
content it is keyed on is the entry's own fingerprint rather than a copy of the
fact. A row therefore describes exactly one version of one fact, and a re-worded
entry is a fact nobody verified.

**Guardrails.** This module imports no delete, trash or archive primitive, and
the delete it does own is a *span* inside a file it is handed the exact text of,
through a receipt that can undo it. A whole-file retirement stays
:func:`ciao.vault_review.trash_note`'s. It never reaches a bounded memory region
— those are :mod:`ciao.memory_proposals`' writes — and it never decides a
proposal: filing a ``needs_review`` verdict is :mod:`ciao.note_edit_proposals`'
``replace_entry`` / ``restamp_entry`` / ``retire_entry``, and this module only
hands it the plan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ciao import memory_receipts as mr
from ciao import note_entries as ne
from ciao import note_receipts as nr
from ciao import note_verification as nv

logger = logging.getLogger(__name__)

# 726-B's vocabulary, imported rather than restated. Every one of these strings
# crosses into the same JSON the note checks are written into and out into a
# proposal kind, so a second spelling here would be a second set of things to get
# wrong: a caller that passed `nv.STILL_VALID` and a caller that passed
# `"still_valid"` must not be two different verdicts.
STILL_VALID = nv.STILL_VALID
UPDATE = nv.UPDATE
RETIRE = nv.RETIRE
UNVERIFIED = nv.UNVERIFIED
OUTCOMES = nv.OUTCOMES

COVERAGE_COMPLETE = nv.COVERAGE_COMPLETE
COVERAGE_PARTIAL = nv.COVERAGE_PARTIAL

APPLIED = nv.APPLIED
NEEDS_REVIEW = nv.NEEDS_REVIEW
CONFLICT = nv.CONFLICT
ALREADY_CHECKED = nv.ALREADY_CHECKED
FAILED = nv.FAILED

AUTO_APPLY = nv.AUTO_APPLY
PROPOSE = nv.PROPOSE
RECORD_ONLY = nv.RECORD_ONLY

Evidence = nv.Evidence
CHECK_COOLDOWN_DAYS = nv.CHECK_COOLDOWN_DAYS
"""How long one recorded entry check keeps a later pass from asking the same
question about the same text. 726-B's constant, not a second one: a note and an
entry inside it are the same kind of question, and two cooldowns would mean a
whole-note pass and an entry pass disagreeing about how long a verdict holds."""

note_check_state_path = nv.note_check_state_path

VERIFIED_STAMP = "[verified: {date}]"
"""The exact stamp spelling :mod:`ciao.note_entries` reads back. Written in one
place so a re-stamp this module composes and the parser that reads it cannot
disagree about the tag's shape; the date itself is the caller's, fixed at the
moment the verdict was reached and never read off a clock at write time."""


# ── The request ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EntryEdit:
    """An entry's exact replacement text, as the agent planned it.

    ``before`` is the entry's own text (not the note's) and ``after`` the text
    that replaces it, whole both ways: :func:`ciao.note_receipts.compose_entry_edit`
    splices one span and a partial entry is not a thing it can splice.
    """

    before: str
    after: str


@dataclass(frozen=True, slots=True)
class EntryVerificationRequest:
    """One entry, one judgement, and the evidence behind it.

    ``identity`` is the
    :func:`ciao.note_entries.entry_identity` the caller resolved the entry under
    and ``relative_path`` the vault-relative note it lives in: both are needed to
    resolve the entry again under the note's own lock, and neither is derivable
    from the other. ``expected_revision`` is the whole note's
    :func:`ciao.memory_receipts.content_revision` at the time the caller read the
    entry, which is what makes the apply revision-checked — a note that moved
    since is a :data:`CONFLICT` with nothing written, never an overwrite.

    ``entry_fingerprint`` is the entry's own
    :func:`ciao.note_entries.refresh_fingerprint` as the caller read it. It is
    carried rather than derived because the caller read a specific version of the
    fact: without it a re-worded entry would be verified by a verdict reached
    about the old words.
    """

    workspace: str
    relative_path: str
    identity: str
    entry_fingerprint: str
    expected_revision: str
    outcome: str
    edit: EntryEdit | None = None
    evidence: tuple[Evidence, ...] = ()
    coverage: str = COVERAGE_PARTIAL
    reason: str = ""


# ── The check state ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EntryCheck:
    """One entry's last verification, as the ``entries`` map stores it.

    ``content_fingerprint`` is the *entry's* fingerprint, not a note revision:
    this row describes one fact, and the fact's own digest is what says whether
    the text is still the one somebody verified. Two notes can hold the same
    bullet and this row cannot be confused between them, because ``note_path`` and
    ``workspace`` are stored beside it and the identity already digests both.

    Everything else is 726-B's :class:`ciao.note_verification.NoteCheck` field for
    field — including ``proposal_id``, which is what holds an entry off a second
    proposal while a person is deciding the first, and ``receipt_id``, so an undo
    has a row to point at.
    """

    identity: str
    note_path: str
    workspace: str
    content_fingerprint: str
    outcome: str
    checked_at: date
    retry_after: date
    evidence: tuple[Evidence, ...] = ()
    coverage: str = ""
    reason: str = ""
    proposal_id: str = ""
    receipt_id: str = ""


def _stored_key(identity: str) -> str:
    """One entry identity as the sidecar stores it, or a refusal.

    The identity is a sha256 hex digest minted by
    :func:`ciao.note_entries.entry_identity`, so a key that is not one cannot name
    an entry — and a map keyed by anything else would let a caller file a verdict
    under a handle no reader can resolve. Refused rather than normalized, the same
    way :func:`ciao.note_verification._stored_key` refuses a path that is not
    vault-relative: a verdict filed under a key that does not name the entry it is
    about is worse than no verdict.
    """
    raw = str(identity or "").strip()
    if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
        raise EntryCheckRefused(
            f"an entry check must be keyed by an entry identity: {identity!r}"
        )
    return raw


class EntryCheckRefused(mr.MemoryReceiptError):
    """An entry check could not be stored under the key it was given.

    Raised by :func:`record_entry_check` for a key that is not an
    :func:`ciao.note_entries.entry_identity` digest, and by
    :func:`update_check_state` for a state file this version cannot read. Both
    refusals leave the file exactly as it is.
    """


def _entry_payload(check: EntryCheck) -> dict[str, Any]:
    """One entry check as the sidecar stores it."""
    return {
        "identity": check.identity,
        "note_path": check.note_path,
        "workspace": check.workspace,
        "content_fingerprint": check.content_fingerprint,
        "outcome": check.outcome,
        "checked_at": check.checked_at.isoformat(),
        "retry_after": check.retry_after.isoformat(),
        "evidence": [row.as_dict() for row in check.evidence],
        "coverage": check.coverage,
        "reason": check.reason,
        "proposal_id": check.proposal_id,
        "receipt_id": check.receipt_id,
    }


def _entry_from_mapping(key: str, raw: Any) -> EntryCheck | None:
    """One stored row as an :class:`EntryCheck`, or ``None`` when unusable.

    Total by design, the same way
    :meth:`ciao.note_verification.Evidence.from_mapping` is: the sidecar is
    written by many passes over months, so a row that has lost a field reads as
    the empty string that field was rather than failing a whole pass's
    verification. The key is authoritative for the identity, for the reason
    :func:`_check_from_mapping` gives for a path.

    A row with no readable fingerprint or date is dropped outright, though: a
    check that cannot be placed against the entry in front of a reader suppresses
    nothing, and honouring it would be taking a verdict on trust.
    """
    row = raw if isinstance(raw, dict) else {}
    fingerprint = str(row.get("content_fingerprint") or "").strip()
    checked_at = nv._stored_date(row.get("checked_at"))
    retry_after = nv._stored_date(row.get("retry_after"))
    if not fingerprint or checked_at is None:
        logger.debug(
            "entry checks: dropping the row for %s, which names no fingerprint or date",
            key,
        )
        return None
    note_path = str(row.get("note_path") or "").strip()
    try:
        nv._stored_key(note_path)
    except nv.NoteCheckRefused:
        logger.warning(
            "entry checks: the row for %s names the note %r, which is not a "
            "vault-relative path",
            key,
            note_path,
        )
        return None
    return EntryCheck(
        identity=key,
        note_path=note_path,
        workspace=str(row.get("workspace") or "").strip(),
        content_fingerprint=fingerprint,
        outcome=str(row.get("outcome") or "").strip(),
        checked_at=checked_at,
        # A row written before the cooldown existed suppresses nothing rather than
        # suppressing forever.
        retry_after=retry_after or checked_at,
        evidence=Evidence.from_mappings(row.get("evidence")),
        coverage=str(row.get("coverage") or ""),
        reason=str(row.get("reason") or ""),
        proposal_id=str(row.get("proposal_id") or "").strip(),
        receipt_id=str(row.get("receipt_id") or "").strip(),
    )


def read_entry_checks(vault_root: Path | str) -> dict[str, EntryCheck]:
    """Every recorded entry check in one vault, keyed by entry identity.

    Never raises and never fails a caller, exactly like
    :func:`ciao.note_verification.read_note_checks`: a missing file is an empty
    map, and a file this version cannot read is logged and read as no checks at
    all. That is the recoverable direction — re-asking one entry costs a pass,
    while honouring a check nobody can read would claim a verification that never
    happened.

    A row whose key is not a digest, or which names no fingerprint or date, is
    skipped and the rest of the map is returned. The document is shared with the
    note checks, so it is read through the same loader they are.
    """
    path = note_check_state_path(vault_root)
    document, skipped = nv._load_check_document(path)
    if skipped:
        logger.warning("entry checks: %s", skipped)
        return {}
    checks: dict[str, EntryCheck] = {}
    for key, row in document["entries"].items():
        if not isinstance(key, str):
            continue
        try:
            stored_key = _stored_key(key)
        except EntryCheckRefused:
            logger.warning("entry checks: %s holds an unusable key %r", path, key)
            continue
        check = _entry_from_mapping(stored_key, row)
        if check is not None:
            checks[stored_key] = check
    return checks


def record_entry_check(vault_root: Path | str, check: EntryCheck) -> None:
    """Store one entry check, replacing any earlier check of the same entry.

    Goes through :func:`ciao.note_verification.update_check_state`, so the write
    is the same atomic, locked, whole-document rewrite a note check is, and the
    note map beside it survives it. An entry keeps one check, not a history: what
    happened to it is in the receipts.

    Raises :class:`EntryCheckRefused` for a key that is not an entry identity.
    """
    key = _stored_key(check.identity)
    row = _entry_payload(replace(check, identity=key))

    def _mutate(document: dict[str, Any]) -> None:
        document["entries"][key] = row

    try:
        nv.update_check_state(vault_root, _mutate)
    except nv.NoteCheckRefused as exc:
        raise EntryCheckRefused(str(exc)) from exc


def _check_settles(
    check: EntryCheck, current_fingerprint: str, *, today: date
) -> bool:
    """Whether one recorded check already settles the question being asked.

    The same two ways a note check does not, at the same unit: the fact changed,
    so the check described text that is no longer there; or the cooldown has run
    out and the claim behind it may have gone stale since. A pending proposal
    settles it whatever the cooldown says — asking again would only produce a
    second row for the owner to decide.
    """
    if check.content_fingerprint != str(current_fingerprint or ""):
        return False
    if check.proposal_id:
        return True
    return today < check.retry_after


def should_check_entry(
    vault_root: Path | str,
    identity: str,
    current_fingerprint: str,
    *,
    today: date,
) -> bool:
    """Whether this entry, as it stands now, is due to be checked again.

    The entry-level twin of :func:`ciao.note_verification.should_check`, and the
    predicate the worklist asks before doing any work. False while a check for the
    *same* fingerprint is inside its cooldown or is waiting on a proposal; True as
    soon as the fingerprint differs, because a re-worded fact is a claim nobody
    verified. A re-*stamped* fact is not: the stamp is metadata, so
    :func:`ciao.note_entries.refresh_fingerprint` is unchanged by it, and a
    verified fact is not asked about again tonight because it was verified.

    A predicate over the sidecar: it reads nothing from the vault, so a caller
    can ask about an entry it has not resolved yet.
    """
    key = str(identity or "").strip()
    check = read_entry_checks(vault_root).get(key)
    return check is None or not _check_settles(
        check, current_fingerprint, today=today
    )


# ── Stamping ───────────────────────────────────────────────────────────────


def stamp_entry(entry_text: str, stamp_date: str) -> str:
    """The entry's exact text with its verification stamp replaced by *stamp_date*.

    Replace, never append. An entry that already carries a stamp is one whose
    earlier verification is being superseded, and appending would leave two
    claims on one line — which
    :mod:`ciao.note_entries` reports as
    :data:`ciao.note_entries.DIAG_STAMP_DUPLICATE` precisely so a tool that does
    it changes the fact's fingerprint with something to say so. Re-stamping is
    supposed to be invisible: :func:`ciao.note_entries.refresh_fingerprint` of the
    result equals the fingerprint of the entry before it, which is what lets
    :func:`should_check_entry` tell a re-stamp from a re-wording.

    The date is the caller's and is validated here rather than trusted, because a
    stamp that is not a real day would be written as
    :data:`ciao.note_entries.STAMP_REASON_IMPOSSIBLE` and read back as a fact
    nobody could have verified on a day that never happened.

    An entry whose stamp is unusable — malformed, a bare `2026-13-01`, or a
    `[verified]` tag with a typo in it — is *not* reported and refused here. The
    entry is rewritten with a good stamp and its diagnostics become empty, which
    is the honest outcome: the bad token was not a claim, and the one this writes
    is.
    """
    day = date.fromisoformat(str(stamp_date).strip())
    opening, separator, rest = str(entry_text).partition("\n")
    body = opening[:-1] if opening.endswith("\r") else opening
    carriage = "\r" if opening.endswith("\r") else ""
    span = ne._strict_stamp_span(body)
    if span is None:
        # No well-formed trailing stamp, or no stamp at all: put one on after the
        # entry's own words, which is the shape the parser reads. The
        # separator is the stamp's own — a space — so a re-stamp of a re-stamped
        # line is byte-identical, and the fingerprint is unchanged either way.
        return f"{body} {VERIFIED_STAMP.format(date=day.isoformat())}{carriage}{separator}{rest}"
    return (
        f"{body[: span[0]]} {VERIFIED_STAMP.format(date=day.isoformat())}"
        f"{body[span[1] :]}{carriage}{separator}{rest}"
    )


# ── The decision ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EntryVerificationPlan:
    """What one request would do, decided without touching the vault.

    ``action`` is the only thing a caller acts on: :data:`AUTO_APPLY` means the
    composed entry edit may go through
    :func:`ciao.note_receipts.apply_entry_edit`, :data:`PROPOSE` means a person
    decides, and :data:`RECORD_ONLY` means nothing but the check state changes.
    ``replacement`` is the entry's exact new text for an auto-applied update and
    a re-stamp's stamp date for a re-stamp; both are handed to the same splice, so
    there is one place that composes an entry's bytes.

    ``status`` is what the operation would report, and ``outcome`` the verdict
    actually recorded — which is not always the one asked for, because a
    ``still_valid`` nobody could cite is recorded as ``unverified``.
    """

    action: str
    status: str
    outcome: str
    replacement: str
    stamp_date: str
    reason: str

    def auto_applies(self) -> bool:
        """Whether this plan may write the entry without a reviewer."""
        return self.action == AUTO_APPLY


@dataclass(frozen=True, slots=True)
class EntryVerificationResult:
    """What one entry verification did.

    ``receipt_id`` is the ``note_apply`` row's id when something was written and
    ``""`` otherwise; ``check`` is the row this recorded (or the one that
    suppressed the work); ``message`` says which way it went and why.
    """

    status: str
    receipt_id: str = ""
    check: EntryCheck | None = None
    message: str = ""


_RETIRE_REASON = (
    "retirement is a human decision: an entry is never deleted unattended, so the "
    "judgement is returned for review and the span is removed only if a person "
    "accepts it"
)

_NO_ENTRY_STAMP = (
    "the entry carries no [verified:] stamp to re-stamp, and adding one to an "
    "entry nobody cited would be a claim rather than a check"
)


def _unknown_outcome(request: EntryVerificationRequest) -> str:
    """Why this request names an outcome this version does not know, else ``""``."""
    outcome = str(request.outcome or "").strip()
    if outcome in OUTCOMES:
        return ""
    return (
        f"unknown verification outcome {outcome!r}; nothing was recorded, so this "
        "is a caller bug rather than a verdict"
    )


def _as_note_request(request: EntryVerificationRequest) -> nv.VerificationRequest:
    """This entry request, wearing a whole-note request's shape.

    Not a conversion — the entry service has no whole-note request — but the
    adapter 726-B's evidence predicates are written against, and it is the whole
    point of the three calls below: the "does a citation name this" test, the
    "is every row a citation" test and the "which gap sent this to a person"
    sentence are the audit's, asked about the note the entry lives in, so an entry
    and a note cannot drift into different standards for the same evidence.

    A citation names the *file* it came from; the bullet inside it is identified by
    its own text. Demanding the bullet be named in the ``supports`` clause would
    refuse every real citation, which is the kind of rule that gets satisfied with
    a useless one and then means nothing.
    """
    return nv.VerificationRequest(
        workspace=request.workspace,
        relative_path=request.relative_path,
        expected_revision="",
        outcome=str(request.outcome or ""),
        edit=(
            None
            if request.edit is None
            else nv.NoteEdit(before=request.edit.before, after=request.edit.after)
        ),
        evidence=request.evidence,
        coverage=request.coverage,
        reason=request.reason,
    )


def _entry_evidence(request: EntryVerificationRequest) -> tuple[Evidence, ...]:
    """The evidence rows that name this entry's note and cite a source."""
    return nv._supporting_evidence(_as_note_request(request))


def _all_citations(request: EntryVerificationRequest) -> bool:
    """Whether *every* evidence row is a citation, and there is at least one."""
    return nv._all_citations(_as_note_request(request))


def _missing_evidence_reason(request: EntryVerificationRequest) -> str:
    """Why this request could not be applied unattended, in one sentence."""
    return nv._missing_evidence_reason(_as_note_request(request))


def plan_entry_verification(
    request: EntryVerificationRequest, *, entry: ne.NoteEntry, today: date
) -> EntryVerificationPlan:
    """What this request would do, decided without writing anything.

    The single place the entry autonomy rule lives, beside 726-B's whole-note
    twin rather than inside it: the two differ in exactly one thing — what gets
    stamped — and everything else (complete coverage, at least one citation that
    names the note, every row a citation, the caller planning against text that is
    still there) is the same rule, checked by the same predicates.

    The one genuinely new branch is a ``still_valid`` re-stamp, which is
    refused when the entry carries no usable stamp to replace. 726-B can always
    add a frontmatter ``updated:`` to a note that lacks one; it declines to
    because a note's frontmatter is structure, whereas an entry's stamp is a
    claim *about that entry*, and writing one nobody checked is exactly the
    invention this whole path exists to prevent. A note-level ``still_valid``
    whose entry has no stamp therefore comes back ``needs_review``, which is where
    an uncitable judgement goes anyway.
    """
    outcome = str(request.outcome or "").strip()
    unknown = _unknown_outcome(request)
    if unknown:
        return EntryVerificationPlan(
            RECORD_ONLY, FAILED, outcome, "", "", unknown
        )
    if outcome == RETIRE:
        return EntryVerificationPlan(PROPOSE, NEEDS_REVIEW, RETIRE, "", "", _RETIRE_REASON)
    if outcome == UNVERIFIED:
        return EntryVerificationPlan(
            RECORD_ONLY,
            UNVERIFIED,
            UNVERIFIED,
            "",
            "",
            request.reason
            or "no source this pass reached could support or contradict this entry",
        )
    if outcome == STILL_VALID:
        return _plan_still_valid(request, entry=entry, today=today)
    return _plan_update(request, entry=entry)


def _plan_still_valid(
    request: EntryVerificationRequest, *, entry: ne.NoteEntry, today: date
) -> EntryVerificationPlan:
    """The re-stamp: this entry's own ``[verified:]`` date, or nothing.

    Complete coverage of the entry and a citation somebody could go and read, or
    the verdict is recorded as ``unverified`` — the same answer a missing
    connector gets, because "nobody checked it" is not "it is still true".

    ``entry`` is the entry as the note holds it now, and its ``stamp`` is the
    thing being replaced. An entry with no usable stamp is ``needs_review`` rather
    than a stamp written onto it: see :func:`plan_entry_verification`.
    """
    if request.coverage != COVERAGE_COMPLETE:
        return EntryVerificationPlan(
            RECORD_ONLY,
            UNVERIFIED,
            UNVERIFIED,
            "",
            "",
            (
                "a re-stamp claims this entry is still true, but this check "
                f"covered {request.coverage or 'nothing'} of it"
            ),
        )
    if not _entry_evidence(request):
        return EntryVerificationPlan(
            RECORD_ONLY,
            UNVERIFIED,
            UNVERIFIED,
            "",
            "",
            _missing_evidence_reason(request),
        )
    if entry.stamp is None or not entry.stamp.valid:
        return EntryVerificationPlan(
            PROPOSE,
            NEEDS_REVIEW,
            STILL_VALID,
            "",
            today.isoformat(),
            _NO_ENTRY_STAMP,
        )
    return EntryVerificationPlan(
        AUTO_APPLY, APPLIED, STILL_VALID, stamp_entry(entry.text, today.isoformat()),
        today.isoformat(), ""
    )


def _plan_update(
    request: EntryVerificationRequest, *, entry: ne.NoteEntry
) -> EntryVerificationPlan:
    """An update: the agent's exact replacement for this entry, or a human's.

    Every claim the replacement writes has to name a source somebody can re-open,
    and every evidence row it was written *with* has to be such a citation — one
    good row among three uncited ones leaves two claims in the new text with
    nothing behind them. The replacement is the caller's, verbatim, and is not
    re-stamped here: the caller knows what it changed, and a service that rewrote
    the text it was handed would stop writing the exact bytes it was shown.

    ``edit.before`` is the entry's own text, so a replacement planned against a
    version of the fact that is no longer there is a :data:`CONFLICT` with nothing
    written and no check recorded — the caller re-reads the entry and decides
    again, rather than having a verdict about old words pinned to new ones.
    """
    edit = request.edit
    if edit is None or not edit.after.strip():
        return EntryVerificationPlan(
            PROPOSE,
            NEEDS_REVIEW,
            UPDATE,
            "",
            "",
            (
                "an update needs the entry's exact replacement text"
                if edit is None
                else "an update that leaves the entry with no content is a deletion, "
                "which this service never performs"
            ),
        )
    if not _entry_evidence(request) or not _all_citations(request):
        return EntryVerificationPlan(
            PROPOSE, NEEDS_REVIEW, UPDATE, "", "", _missing_evidence_reason(request)
        )
    if edit.before and edit.before != entry.text:
        return EntryVerificationPlan(
            RECORD_ONLY,
            CONFLICT,
            UPDATE,
            "",
            "",
            (
                "the entry no longer holds the text this edit was planned against; "
                "nothing was written and no check was recorded"
            ),
        )
    return EntryVerificationPlan(AUTO_APPLY, APPLIED, UPDATE, edit.after, "", "")


# ── Verification ───────────────────────────────────────────────────────────


def _provenance(
    request: EntryVerificationRequest, plan: EntryVerificationPlan, *, today: date
) -> dict[str, Any]:
    """The evidence chain stamped onto an applied receipt.

    Everything a reader of History needs to judge the write without re-running it:
    the entry it was about, which outcome was claimed, whether the rule applied it
    or a human was asked, how much of the entry the check covered, and the
    citations it rested on.
    """
    return {
        "entry_identity": request.identity,
        "entry_fingerprint": request.entry_fingerprint,
        "outcome": plan.outcome,
        "action": plan.action,
        "coverage": request.coverage,
        "reason": request.reason,
        "checked_at": today.isoformat(),
        "evidence": [row.as_dict() for row in request.evidence],
    }


def _new_check(
    request: EntryVerificationRequest,
    plan: EntryVerificationPlan,
    *,
    key: str,
    note_path: str,
    fingerprint: str,
    receipt_id: str,
    today: date,
) -> EntryCheck:
    """The check one outcome leaves behind, before it is stored.

    The reason is the caller's when the recorded outcome is the one it asked for,
    and the rule's own when the rule downgraded it — 726-B's rule, applied at the
    same place, for the same reason.
    """
    requested = str(request.outcome or "").strip()
    reason = (
        plan.reason if plan.outcome != requested else request.reason or plan.reason
    )
    return EntryCheck(
        identity=key,
        note_path=note_path,
        workspace=request.workspace,
        content_fingerprint=fingerprint,
        outcome=plan.outcome,
        checked_at=today,
        retry_after=today + timedelta(days=CHECK_COOLDOWN_DAYS),
        evidence=request.evidence,
        coverage=request.coverage,
        reason=reason,
        receipt_id=receipt_id,
    )


def _store(vault_root: Path, check: EntryCheck) -> tuple[EntryCheck, str]:
    """Store one check; return it with the reason it could not be stored.

    A lock it cannot take is reported rather than raised, like 726-B's: the
    sidecar is a ledger, and the worst a caller loses by not writing it is an
    entry that gets asked about again. The note write this may already have
    performed is journaled under its own lock, with the receipt as its recovery
    evidence, and raising out of here would report a *failure* for a mutation that
    did land.
    """
    try:
        record_entry_check(vault_root, check)
    except (mr.MemoryReceiptError, mr.QueueLockError, OSError) as exc:
        logger.error("entry verification: could not record the check: %s", exc)
        return check, str(exc)
    return check, ""


def verify_entry(
    request: EntryVerificationRequest,
    *,
    vault_root: Path | str,
    config: Any = None,
    actor: str = "agent",
    source: str = "curation",
    today: date | None = None,
) -> EntryVerificationResult:
    """Record one entry's verdict, applying its edit only when the rule allows it.

    The whole operation in order, and 726-B's order exactly: refuse a vault that
    is not this workspace's, read the note's exact bytes, resolve the entry by
    identity under the note's own lock, confirm the fingerprint the caller judged,
    skip the work if a check for *this* fingerprint already answers it, compare the
    revision the caller read, ask :func:`plan_entry_verification` what the rule
    says, and then either splice through
    :func:`ciao.note_receipts.apply_entry_edit` — the only mutation, so the edit is
    journaled and undoable — or record the verdict and change nothing else.

    What comes back is the honest report of which of those happened, in
    :mod:`ciao.note_verification`'s own words so a caller handles the two the same
    way: ``applied``, ``needs_review``, ``unverified``, ``conflict``,
    ``already_checked`` or ``failed``. The statuses mean what they mean there, with
    two refinements: ``conflict`` additionally covers an identity the note no
    longer holds, and a ``still_valid`` re-stamp describes the fingerprint the
    re-stamp left behind, which is the *same* fingerprint by construction.

    ``config`` is optional here, unlike 726-B's :func:`ciao.note_verification.verify_note`:
    that service resolves the vault from the install's own registry so a verdict
    cannot be filed in a vault the workspace does not claim, and its callers
    always have one. An entry verification is reached through a caller that has
    already resolved that same vault — :func:`ciao.note_receipts.apply_entry_edit`
    re-checks it is a real directory and every path inside it is confined — so a
    missing ``config`` is a pure seam rather than a second, weaker version of the
    same rule. A caller that *has* a config should pass it, and then
    ``workspace_vault_root`` is consulted exactly as 726-B consults it.
    """
    day = today or date.today()
    try:
        root = nr.canonical_vault(vault_root)
    except mr.MemoryReceiptError as exc:
        return EntryVerificationResult(FAILED, message=f"unusable vault root: {exc}")
    if config is not None:
        foreign = nv._foreign_workspace_vault(config, request.workspace, root)
        if foreign:
            return EntryVerificationResult(FAILED, message=foreign)
    try:
        target = nr.resolve_note_path(root, request.relative_path)
    except mr.MemoryReceiptError as exc:
        return EntryVerificationResult(FAILED, message=str(exc))
    # The same spelling `note_receipts` records the receipt under, so the check
    # state and the receipt name one file.
    key = target.relative_to(root).as_posix()
    try:
        note_text = target.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return EntryVerificationResult(
            FAILED, message=f"the note could not be read as UTF-8: {exc}"
        )
    current = mr.content_revision(note_text)

    # A caller bug is reported before anything else, including before the
    # cooldown: an outcome this version cannot interpret must say so rather than
    # hide behind "already checked" for the rest of the month.
    unknown = _unknown_outcome(request)
    if unknown:
        return EntryVerificationResult(FAILED, message=unknown)

    entry = nr.find_entry(
        note_text,
        identity=request.identity,
        note_path=key,
        workspace=request.workspace,
    )
    if entry is None:
        return EntryVerificationResult(
            CONFLICT,
            message=(
                f"{key} holds no entry with identity {str(request.identity)[:12]}, so "
                "it is not the fact this verdict was about; nothing was written and "
                "no check was recorded, and the caller must read the note again"
            ),
        )
    if entry.fingerprint != str(request.entry_fingerprint or "").strip():
        return EntryVerificationResult(
            CONFLICT,
            message=(
                f"the entry {str(request.identity)[:12]} in {key} is not the text this "
                "verdict was planned against (its fingerprint has changed); nothing "
                "was written and no check was recorded"
            ),
        )

    existing = read_entry_checks(root).get(entry.identity)
    if existing is not None and _check_settles(
        existing, entry.fingerprint, today=day
    ):
        return EntryVerificationResult(
            ALREADY_CHECKED,
            check=existing,
            message=(
                f"{entry.identity[:12]} in {key} was already checked as "
                f"{existing.outcome} on {existing.checked_at.isoformat()}; nothing was "
                "asked or written"
            ),
        )

    # The revision is checked before the verdict is decided, not only before the
    # write, for 726-B's reason: a verdict reached about a revision the note is no
    # longer in would be recorded against the text that is actually there and
    # suppress it for the whole cooldown.
    expected = str(request.expected_revision or "").strip()
    if not expected:
        return EntryVerificationResult(
            FAILED,
            message=(
                "an expected revision is required; this service never judges an "
                "entry the caller has not read"
            ),
        )
    if expected != current:
        return EntryVerificationResult(
            CONFLICT,
            message=(
                "the note changed since this request was planned, so its verdict is "
                "about text that is no longer there; nothing was written and no check "
                "was recorded, and the caller must read the note again"
            ),
        )

    plan = plan_entry_verification(request, entry=entry, today=day)
    if plan.status == FAILED:
        return EntryVerificationResult(FAILED, message=plan.reason)
    if not plan.auto_applies():
        if plan.status == CONFLICT:
            return EntryVerificationResult(CONFLICT, message=plan.reason)
        check, failure = _store(
            root,
            _new_check(
                request,
                plan,
                key=entry.identity,
                note_path=key,
                fingerprint=entry.fingerprint,
                receipt_id="",
                today=day,
            ),
        )
        if failure:
            return EntryVerificationResult(FAILED, message=failure)
        return EntryVerificationResult(plan.status, check=check, message=plan.reason)

    try:
        receipt = nr.apply_entry_edit(
            vault_root=root,
            relative_path=key,
            expected_revision=expected,
            identity=entry.identity,
            fingerprint=entry.fingerprint,
            replacement=plan.replacement,
            actor=actor,
            source=source,
            workspace=request.workspace,
            provenance=_provenance(request, plan, today=day),
        )
    except mr.RevisionConflict as exc:
        # The note moved, or the entry is not the one that was judged, under a
        # read nobody rechecked. Nothing is written and nothing recorded, so the
        # caller can re-read and decide again.
        return EntryVerificationResult(CONFLICT, message=str(exc))
    except (mr.MemoryReceiptError, mr.QueueLockError, OSError) as exc:
        # A journal that would not record, a lock this thread could not take, a
        # filesystem that said no. All three are reported rather than raised: this
        # function's contract is one result per entry, and a caller running a pass
        # over a vault must not lose a whole run to one unwritable file.
        return EntryVerificationResult(FAILED, message=str(exc))
    receipt_id = str(receipt.get("id", ""))
    # The check describes the fingerprint this operation LEFT the entry at. A
    # re-stamp's is the same one — the stamp is metadata and the fingerprint
    # ignores it — so a re-stamped entry is checked against the fact it was, and
    # an update's is the caller's new text, which only the next read can compute.
    check, failure = _store(
        root,
        _new_check(
            request,
            plan,
            key=entry.identity,
            note_path=key,
            fingerprint=ne.refresh_fingerprint(plan.replacement)
            if plan.outcome == UPDATE
            else entry.fingerprint,
            receipt_id=receipt_id,
            today=day,
        ),
    )
    return EntryVerificationResult(
        APPLIED,
        receipt_id=receipt_id,
        check=check,
        message=failure
        or plan.reason
        or f"{plan.outcome} applied to entry {entry.identity[:12]} of {key}",
    )
