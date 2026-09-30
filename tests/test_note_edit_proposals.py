"""The `note_edit` proposal: filing it once, and settling both sides of it.

``tests/test_note_verification.py`` owns the autonomy rule and the check state;
this file owns what happens to a verdict the rule would not apply. What is
pinned here is the pair of properties the whole child exists for:

* **exactly one row per (note, revision).** A second pass that reaches the same
  verdict about the same text must find the record it already wrote and write
  nothing, and the filing must record a `NoteCheck` carrying the queue row's
  id — because that id is the only thing standing between a note and a second
  proposal while the first is still waiting.
* **a settlement is temporary, a proposal is not.** Accepting or dismissing
  settles the sidecar AND clears the check's `proposal_id`, which is what stops
  the same revision being re-proposed; it deliberately writes no "refused
  forever" flag, so an edited note is re-checkable and a different revision is
  proposed again. The category sidecar's permanent decline flag is the thing
  this design refuses to copy.

Plus the refusals that make the record trustworthy: a sidecar this version
cannot read is refused rather than applied against a guessed operation, the
reader re-asserts every per-operation rule `file_note_edit` enforced (a
`replace` with no body, a retirement carrying one, a partial-coverage re-stamp,
a re-stamp with no date, an outcome or coverage no verification produces), and
a retirement is never anything but an attended accept — `note_verification`
still cannot reach a delete primitive, and nothing here can reach one either.

Every test uses a temporary synthetic vault and a config that answers one
question about where its notes live. Nothing here reads or writes a real
install's notes, and no engine, service or scheduler is started.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao import memory_receipts as mr
from ciao import note_edit_proposals as nep
from ciao import note_entries as ne
from ciao import note_verification as nv

NOTE = "notes/office.md"
TODAY = date(2026, 3, 14)

PLAIN = (
    "---\ntype: note\nupdated: 2024-01-05\n---\n\n"
    "# Office\n\nThe office is on Via Verdi 12, third floor.\n"
)
FOURTH = PLAIN.replace("third", "fourth")
"""The exact replacement an update was planned as: a contradiction, a full
before/after, and nothing else invented on top of it."""

CITATION = nv.Evidence(
    source_type="chat",
    source_ref="chat-2026-03-02",
    quoted="still Via Verdi 12, third floor",
    supports=f"{NOTE}: which floor the office is on",
    observed_at="2026-03-02",
)
UNCITED = nv.Evidence(
    source_type="chat",
    source_ref="",
    quoted="I think reception moved",
    supports=f"{NOTE}: which floor the office is on",
)


# ── Fixtures and helpers ───────────────────────────────────────────────────


def _vault(tmp_path: Path, name: str = "vault") -> Path:
    vault = tmp_path / name
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    (vault / "notes").mkdir(exist_ok=True)
    return vault


def _config(vault: Path) -> SimpleNamespace:
    """A config whose only answer is where this workspace's notes live."""
    return SimpleNamespace(workspace_vault_root=lambda _name: vault)


def _write(vault: Path, relative: str, text: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _revision(text: str) -> str:
    return mr.content_revision(text)


def _queue_text(vault: Path) -> str:
    path = vault / "Workspace" / "Memory-Proposals.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _dismiss_row(vault: Path, sidecar_id: str) -> str:
    """Drop the queued bullet naming this sidecar, as an accept/dismiss does.

    Through the queue's own removal rather than by rewriting the file here, so
    the two halves of a decision — settle the record, then remove the row — are
    exercised in the order the routes take them.
    """
    from ciao.memory_proposals import remove_proposal_by_substring

    removed = remove_proposal_by_substring(
        vault / "Workspace" / "Memory-Proposals.md", sidecar_id
    )
    assert removed is not None, f"no queued row for {sidecar_id}"
    return removed[1]


def _file(
    config: Any,
    note: Path,
    *,
    operation: str = nep.REPLACE,
    outcome: str = nv.UPDATE,
    coverage: str = nv.COVERAGE_COMPLETE,
    before: str = PLAIN,
    after: str = FOURTH,
    evidence: tuple[nv.Evidence, ...] = (CITATION,),
    reason: str = "the third floor no longer exists",
) -> nep.NoteEditProposal:
    """File one note edit against a note the caller has just read."""
    return nep.file_note_edit(
        config,
        workspace="personal",
        relative_path=NOTE,
        expected_revision=_revision(before),
        operation=operation,
        before=before,
        after=after,
        outcome=outcome,
        coverage=coverage,
        evidence=evidence,
        reason=reason,
        today=TODAY,
    )


def _needs_review(
    vault: Path,
    config: Any,
    note: Path,
    **overrides: Any,
) -> nv.VerificationResult:
    """A verification the autonomy rule refuses to apply, the way a pass makes it.

    The uncited update is the honest one: an agent's replacement that no
    citation carries comes back as ``needs_review`` with nothing written, and it
    is exactly that result this module exists to file.
    """
    fields: dict[str, Any] = {
        "workspace": "personal",
        "relative_path": NOTE,
        "expected_revision": mr.content_revision(
            note.read_bytes().decode("utf-8")
        ),
        "outcome": nv.UPDATE,
        "coverage": nv.COVERAGE_COMPLETE,
        "edit": nv.NoteEdit(
            before=PLAIN,
            after=FOURTH,
        ),
        "evidence": (UNCITED,),
        "reason": "the third floor no longer exists",
    }
    fields.update(overrides)
    return nv.verify_note(
        nv.VerificationRequest(**fields),
        vault_root=vault,
        config=config,
        today=TODAY,
    )


# ── Filing: one row per (note, revision) ────────────────────────────────────


def test_filing_writes_one_bullet_and_one_sidecar(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)

    proposal = _file(config, note)

    # The bullet's payload is the sidecar id — the operation and the replacement
    # text are not one line — and its own text names the note and the exact
    # revision the verdict was about.
    bullet = (
        f"- [note_edit {proposal.id}] {NOTE} — {nep.REPLACE} "
        f"(rev {_revision(PLAIN)[:8]}): {proposal.reason}"
    )
    assert bullet in _queue_text(vault), _queue_text(vault)
    # The revision rides in the TEXT, not the source tag, because
    # `append_proposals` dedupes on the text alone: without it an accepted edit
    # would block the next question about the same note for ever.
    assert f"rev {_revision(PLAIN)[:8]}" in _queue_text(vault)
    assert "(from: note verification · replace)" in _queue_text(vault)
    # The record is where the accept reads the operation from.
    stored = nep.read_sidecar(config, "personal", proposal.id)
    assert stored == proposal
    assert stored.relative_path == NOTE
    assert stored.expected_revision == _revision(PLAIN)
    assert stored.after == FOURTH
    assert stored.settled == ""
    assert stored.accepted is False


def test_a_whole_note_id_is_the_one_this_module_always_produced() -> None:
    """Entry operations must not move the ids of the proposals already filed.

    The basis is ``workspace\\0path\\0revision`` and an entry operation appends a
    fourth field — so a whole-note proposal has to contribute *nothing at all*,
    not even a trailing separator, or its id changes. That is not cosmetic: an
    install upgrading with a proposal still pending has it in the sidecar under
    the old id, and ``read_sidecar(new_id)`` misses it, so ``file_note_edit``
    finds nothing to dedupe against and writes a second, orphan record for a
    question already in the review queue. Two rows, one question, and a queue
    the owner reads as two.

    So the whole-note id is pinned here to the old formula, computed the long way
    round rather than through the function under test.
    """
    revision = mr.content_revision(PLAIN)

    assert nep.note_edit_id("personal", NOTE, revision) == hashlib.sha256(
        f"personal\0{NOTE}\0{revision}".encode("utf-8")
    ).hexdigest()[:16]
    # The explicit empty identity is the same call, and it must not append a
    # field either — a caller that passes "" for "whole note" is the ordinary
    # shape, not an edge case.
    assert nep.note_edit_id("personal", NOTE, revision, "") == nep.note_edit_id(
        "personal", NOTE, revision
    )
    # Two entries of one note at one revision are two questions, so an id shared
    # by both would let the second filing find the first's record.
    assert nep.note_edit_id("personal", NOTE, revision, "a" * 64) != nep.note_edit_id(
        "personal", NOTE, revision, "b" * 64
    )
    assert nep.note_edit_id("personal", NOTE, revision, "a" * 64) != nep.note_edit_id(
        "personal", NOTE, revision
    )


def test_refiling_the_same_revision_writes_nothing(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)

    first = _file(config, note)
    queue_after_first = _queue_text(vault)
    sidecar_after_first = nep.sidecar_path(
        config, "personal", first.id
    ).read_text(encoding="utf-8")

    second = _file(config, note)

    assert second == first, "the same verdict about the same text is one proposal"
    assert _queue_text(vault) == queue_after_first, "no second row"
    assert (
        nep.sidecar_path(config, "personal", first.id).read_text(encoding="utf-8")
        == sidecar_after_first
    )


def test_a_later_verdict_never_overwrites_a_still_queued_question(
    tmp_path: Path,
) -> None:
    """The record IS the operation, so a queued bullet must keep naming it.

    The id is derived from the note and its revision and nothing else, so a
    retirement filed over a still-queued replacement lands on the same file. The
    replacement's bullet stays in the queue reading "replace" while the record it
    points at now says "retire" — a row whose accept removes a note the reviewer
    was asked to rewrite. So the row already in the queue wins until it is
    decided, and the later verdict is filed once the answer is in.
    """
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)

    first = _file(config, note)
    second = _file(
        config, note, operation=nep.RETIRE, outcome=nv.RETIRE, after=""
    )

    assert second == first, "the question the owner is being asked wins"
    assert second.operation == nep.REPLACE
    assert second.after == FOURTH, "not the replacement text of a retirement"
    queued = _queue_text(vault)
    assert queued.count("- [note_edit ") == 1, "one row, not a second question"
    assert f"— {nep.REPLACE} (rev {_revision(PLAIN)[:8]})" in queued
    assert nep.RETIRE not in queued, "the label matches what accepting it does"
    # And the check keeps pinning the row that is actually queued.
    assert nv.read_note_checks(vault)[NOTE].proposal_id == first.proposal_id

    # Once that row is decided the record re-arms from the verdict in hand, so a
    # retirement the later pass reached is not lost to the earlier question.
    nep.settle_note_edit(config, "personal", first.id, accepted=False)
    _dismiss_row(vault, first.id)

    third = _file(
        config, note, operation=nep.RETIRE, outcome=nv.RETIRE, after=""
    )

    assert third.id == first.id, "one record per (note, revision)"
    assert third.operation == nep.RETIRE
    assert third.after == ""
    assert _queue_text(vault).count("- [note_edit ") == 1
    assert f"— {nep.RETIRE} (rev {_revision(PLAIN)[:8]})" in _queue_text(vault)
    assert nv.read_note_checks(vault)[NOTE].proposal_id == third.proposal_id


def test_a_reason_that_spans_lines_still_pins_the_check(tmp_path: Path) -> None:
    """A multi-line reason is one bullet, so it is one question with one row.

    The queue is line-oriented and `append_proposals` collapses the reason
    through `_one_line` before writing it. Reading the row back with the RAW text
    found nothing, so the filing skipped `record_note_check` — and a check with
    no `proposal_id` holds off nothing, so every pass re-verified the note and
    re-filed it against a row that was already there. The bullet is built and
    matched through the queue's own collapse, which is what makes the row
    findable at all.
    """
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)
    reason = "the third floor no longer exists\nand reception moved to Via Leoni 4"

    proposal = _file(config, note, reason=reason)

    queued = _queue_text(vault)
    assert queued.count("- [note_edit ") == 1
    assert (
        f"— {nep.REPLACE} (rev {_revision(PLAIN)[:8]}): the third floor no longer "
        "exists and reception moved to Via Leoni 4" in queued
    ), "collapsed onto one line, exactly as the queue writes it"
    assert proposal.proposal_id, "the row id was read back, so the check can name it"
    assert nv.read_note_checks(vault)[NOTE].proposal_id == proposal.proposal_id

    # And the same verdict again is the same question, not a second row.
    again = _file(config, note, reason=reason)

    assert again == proposal
    assert _queue_text(vault).count("- [note_edit ") == 1


def test_a_filed_proposal_records_a_check_naming_its_queue_row(
    tmp_path: Path,
) -> None:
    """The id is what holds the note off a second proposal.

    `note_verification._check_settles` suppresses a revision that is waiting on a
    proposal whatever its cooldown says, so a filing that did not record this
    would let the next nightly pass file a second row for a note that already
    has one.
    """
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)

    proposal = _file(config, note)

    check = nv.read_note_checks(vault)[NOTE]
    assert check.proposal_id == proposal.proposal_id
    assert check.proposal_id, "a check with no proposal id holds off nothing"
    # It describes the revision the note is in, so it describes the text a later
    # pass will read rather than the text that was just replaced.
    assert check.content_revision == _revision(PLAIN)
    assert check.outcome == nv.UPDATE
    assert check.evidence == proposal.evidence
    # And it really does suppress, past the cooldown.
    far_off = TODAY + timedelta(days=365)
    assert not nv.should_check(vault, NOTE, _revision(PLAIN), today=far_off)
    assert nv.should_check(vault, NOTE, "e" * 64, today=far_off), (
        "a note that changed has claims nobody verified"
    )


def test_an_accepted_edit_does_not_block_the_next_one_about_the_same_note(
    tmp_path: Path,
) -> None:
    """The queue's own dedupe must not become a permanent "already asked".

    `append_proposals` refuses a bullet whose TEXT is already queued or already
    promoted, and the key is the text alone — `memory_proposals.
    _existing_proposal_texts` matches everything after the bracketed head, so
    neither the payload nor the source tag counts. A text that did not name the
    revision would let the edit just accepted make the next question about that
    note unaskable for ever, however much the note had changed: a sidecar on disk
    and a check pinned to a proposal id that is not in the queue.
    """
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)

    first = _file(config, note)
    nep.settle_note_edit(
        config, "personal", first.id, accepted=True, receipt_id="mrcpt_abc"
    )
    _dismiss_row(vault, first.id)
    # A later pass edits the note and reaches the same verdict about it, with the
    # same reason and therefore the same wording.
    _write(vault, NOTE, FOURTH)
    second = _file(config, note, before=FOURTH, after=FOURTH)

    assert second.id != first.id
    assert _queue_text(vault).count("- [note_edit ") == 1, (
        "the new revision is queued, not swallowed by the accepted one"
    )
    assert nv.read_note_checks(vault)[NOTE].proposal_id == second.proposal_id


def test_a_changed_revision_is_a_new_proposal(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)

    first = _file(config, note)
    nep.settle_note_edit(config, "personal", first.id, accepted=False)
    _dismiss_row(vault, first.id)
    # The owner edits the note, which is the only thing that makes it due again.
    _write(vault, NOTE, FOURTH)
    assert nv.should_check(vault, NOTE, _revision(FOURTH), today=TODAY)

    second = _file(config, note, before=FOURTH, after=FOURTH)

    assert second.id != first.id, "a different revision is a different question"
    assert _queue_text(vault).count("- [note_edit ") == 1
    assert nv.read_note_checks(vault)[NOTE].proposal_id == second.proposal_id


def test_a_settled_row_does_not_re_propose_the_same_revision(tmp_path: Path) -> None:
    """A refusal is not a "rejected forever" marker.

    The category sidecar has one, and this design must not: the check's own
    cooldown is the honest, expiring record. So while the note is unchanged a
    second filing of the same verdict finds the settled record and re-arms it
    only when the note is actually edited.
    """
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)

    first = _file(config, note)
    settled = nep.settle_note_edit(
        config, "personal", first.id, accepted=False, reason="the evidence is thin"
    )
    assert settled.settled != ""
    assert settled.accepted is False
    assert settled.reason == "the evidence is thin"

    # The check's hold is released, so the note is not suppressed FOREVER — only
    # by its own cooldown, which this settlement leaves running.
    check = nv.read_note_checks(vault)[NOTE]
    assert check.proposal_id == ""
    assert check.retry_after == TODAY + timedelta(days=nv.CHECK_COOLDOWN_DAYS)
    assert not nv.should_check(vault, NOTE, _revision(PLAIN), today=TODAY)
    assert nv.should_check(
        vault, NOTE, _revision(PLAIN), today=TODAY + timedelta(days=31)
    ), "the cooldown is an end, not a permanent suppression"
    # And the settled record is a record of a decision, not a tombstone.
    assert nep.read_sidecar(config, "personal", first.id).settled == settled.settled


def test_a_later_verdict_about_the_same_text_re_arms_the_record(
    tmp_path: Path,
) -> None:
    """The record describes the question, not the answer to the last one.

    The id is derived from the note and its revision, so a second pass reaching
    a DIFFERENT verdict about the very same text — a retirement that later
    became an update — lands on the same file. Keeping the settled record's
    operation would queue a row whose accept did something nobody was asked
    about, which is the one outcome worse than not filing at all.
    """
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)

    first = _file(
        config, note, operation=nep.RETIRE, outcome=nv.RETIRE, after=""
    )
    nep.settle_note_edit(config, "personal", first.id, accepted=False)
    # The queue row goes the way the routes take it, in the same operation as the
    # settlement — the row IS the proposal, so removing it is not optional.
    _dismiss_row(vault, first.id)

    second = _file(config, note)

    assert second.id == first.id, "one record per (note, revision)"
    assert second.operation == nep.REPLACE, "the new verdict, not the old one"
    assert second.after == FOURTH
    assert second.created_at == first.created_at, "the sidecar keeps its own order"
    assert second.settled == "", "re-armed: it is a question again"
    assert second.accepted is False
    assert _queue_text(vault).count("- [note_edit ") == 1
    assert nv.read_note_checks(vault)[NOTE].proposal_id == second.proposal_id


def test_settling_twice_keeps_the_first_decision(tmp_path: Path) -> None:
    """A retry must not rewrite the first decision's stamp or receipt."""
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)

    proposal = _file(config, note)
    first = nep.settle_note_edit(
        config, "personal", proposal.id, accepted=True, receipt_id="mrcpt_abc"
    )
    again = nep.settle_note_edit(config, "personal", proposal.id, accepted=False)

    assert again.settled == first.settled
    assert again.accepted is True
    assert again.receipt_id == "mrcpt_abc"


def test_settling_only_clears_its_own_pending_check(tmp_path: Path) -> None:
    """A settlement must not unblock a question it knows nothing about."""
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)
    other = _write(vault, "notes/other.md", PLAIN)

    filed = _file(config, note)
    # A second proposal for a DIFFERENT note, still waiting.
    other_proposal = nep.file_note_edit(
        config,
        workspace="personal",
        relative_path="notes/other.md",
        expected_revision=_revision(PLAIN),
        operation=nep.RESTAMP,
        before=PLAIN,
        after="",
        outcome=nv.STILL_VALID,
        coverage=nv.COVERAGE_COMPLETE,
        evidence=(CITATION,),
        reason="checked against the March invoice",
        today=TODAY,
    )
    assert other_proposal.id != filed.id

    nep.settle_note_edit(config, "personal", filed.id, accepted=False)

    checks = nv.read_note_checks(vault)
    assert checks[NOTE].proposal_id == ""
    assert checks["notes/other.md"].proposal_id == other_proposal.proposal_id
    assert not nv.should_check(vault, "notes/other.md", _revision(PLAIN), today=TODAY)
    assert other.exists()


# ── Filing: the refusals ────────────────────────────────────────────────────


def test_a_replace_with_no_replacement_is_never_filed(tmp_path: Path) -> None:
    """An update that leaves the note with no content is a deletion.

    `note_verification._plan_update` returns the same verdict for the same
    reason, so a proposer that filed it would queue a button whose only possible
    outcome is emptying somebody's note.
    """
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)

    with pytest.raises(nep.NoteEditRefused, match="deletion"):
        _file(config, note, after="   ")
    assert _queue_text(vault) == ""
    assert not nep.sidecar_dir(config, "personal").exists() or not list(
        nep.sidecar_dir(config, "personal").iterdir()
    )


def test_a_restamp_needs_complete_coverage(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)

    with pytest.raises(nep.NoteEditRefused, match="complete coverage"):
        _file(
            config,
            note,
            operation=nep.RESTAMP,
            outcome=nv.STILL_VALID,
            coverage=nv.COVERAGE_PARTIAL,
            after="",
        )
    assert _queue_text(vault) == ""


def test_filing_refuses_what_it_cannot_key_or_check(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    config = _config(vault)
    _write(vault, NOTE, PLAIN)

    def _refuse(**overrides: Any) -> str:
        fields: dict[str, Any] = {
            "workspace": "personal",
            "relative_path": NOTE,
            "expected_revision": _revision(PLAIN),
            "operation": nep.REPLACE,
            "before": PLAIN,
            "after": FOURTH,
            "outcome": nv.UPDATE,
            "coverage": nv.COVERAGE_COMPLETE,
            "evidence": (CITATION,),
            "reason": "r",
            "today": TODAY,
        }
        fields.update(overrides)
        with pytest.raises(nep.NoteEditRefused):
            nep.file_note_edit(config, **fields)
        return str(overrides)

    _refuse(operation="delete")
    _refuse(relative_path="/etc/passwd")
    _refuse(relative_path="../../escape.md")
    _refuse(expected_revision="")
    _refuse(outcome="looks_fine_to_me")
    _refuse(coverage="most_of_it")


def test_the_sidecar_fails_closed_on_a_malformed_row(tmp_path: Path) -> None:
    """A record this version cannot read is refused, never defaulted.

    The operation and the replacement text are what get written, so a row
    missing either must not resolve to something plausible.
    """
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)
    proposal = _file(config, note)
    path = nep.sidecar_path(config, "personal", proposal.id)

    good = json.loads(path.read_text(encoding="utf-8"))

    def _write_row(payload: Any) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")

    # A field of the wrong type.
    broken = json.loads(json.dumps(good))
    broken["proposal"]["operation"] = 7
    _write_row(broken)
    with pytest.raises(nep.NoteEditSidecarError, match="operation must be a string"):
        nep.read_sidecar(config, "personal", proposal.id)

    # A field that is missing, and therefore absent rather than a string.
    broken = json.loads(json.dumps(good))
    del broken["proposal"]["after"]
    _write_row(broken)
    with pytest.raises(nep.NoteEditSidecarError, match="after must be a string"):
        nep.read_sidecar(config, "personal", proposal.id)

    # A record with no revision could not be conflict-checked at all.
    broken = json.loads(json.dumps(good))
    broken["proposal"]["expected_revision"] = ""
    _write_row(broken)
    with pytest.raises(nep.NoteEditSidecarError, match="no revision"):
        nep.read_sidecar(config, "personal", proposal.id)

    # An operation this version does not know, and a path it cannot resolve.
    broken = json.loads(json.dumps(good))
    broken["proposal"]["operation"] = "delete"
    _write_row(broken)
    with pytest.raises(nep.NoteEditSidecarError, match="not one of"):
        nep.read_sidecar(config, "personal", proposal.id)

    broken = json.loads(json.dumps(good))
    broken["proposal"]["relative_path"] = "/etc/passwd"
    _write_row(broken)
    with pytest.raises(nep.NoteEditSidecarError, match="vault-relative"):
        nep.read_sidecar(config, "personal", proposal.id)

    # A file from a newer schema is not read as "no record": re-filing an edit a
    # human has not seen costs one row, guessing would apply one nobody read.
    _write_row({**good, "schema": 99})
    with pytest.raises(nep.NoteEditSidecarError, match="carries schema"):
        nep.read_sidecar(config, "personal", proposal.id)

    # And nothing that is not JSON at all is either.
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(nep.NoteEditSidecarError, match="malformed"):
        nep.read_sidecar(config, "personal", proposal.id)

    # The good record still reads back, so the refusals above were about the
    # rows written, not about the path.
    _write_row(good)
    assert nep.read_sidecar(config, "personal", proposal.id) == proposal
    assert nep.read_sidecar(config, "personal", "nope") is None


def test_a_replace_with_no_replacement_is_refused_on_read(tmp_path: Path) -> None:
    """The reader re-asserts the rules filing enforced, one operation at a time.

    A `replace` whose `after` was emptied is the defect the reader exists to
    stop: every field is the right type, so a type-only check waves it through,
    and the accept then writes `''`. Filing refuses it, so a record that carries
    one was not filed — it was written by a hand or a truncation, and it must not
    be applied.
    """
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)
    proposal = _file(config, note)
    path = nep.sidecar_path(config, "personal", proposal.id)
    good = json.loads(path.read_text(encoding="utf-8"))

    for blank in ("", "   "):
        broken = json.loads(json.dumps(good))
        broken["proposal"]["after"] = blank
        path.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(nep.NoteEditSidecarError, match="empty the note"):
            nep.read_sidecar(config, "personal", proposal.id)
    assert note.read_bytes() == PLAIN.encode("utf-8")


def test_a_retire_that_carries_an_after_image_is_refused(tmp_path: Path) -> None:
    """A retirement writes nothing, so an after image is not a small difference.

    Filing drops it, so a record carrying one was not filed. Diffing it against
    the before image would show a body that a retirement never wrote, and the
    accept's own revision check says nothing about which text is right.
    """
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)
    proposal = _file(config, note, operation=nep.RETIRE, outcome=nv.RETIRE, after="")
    path = nep.sidecar_path(config, "personal", proposal.id)

    broken = json.loads(path.read_text(encoding="utf-8"))
    broken["proposal"]["after"] = FOURTH
    path.write_text(json.dumps(broken), encoding="utf-8")

    with pytest.raises(nep.NoteEditSidecarError, match="after image"):
        nep.read_sidecar(config, "personal", proposal.id)


def test_a_restamp_needs_complete_coverage_on_read_too(tmp_path: Path) -> None:
    """A re-stamp from partial coverage claims more than the check looked at.

    The rule is 726-B's, and a record that drops it would re-stamp a note whose
    claims were only half checked — the one outcome the partial-coverage verdict
    exists to prevent.
    """
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)
    proposal = _file(
        config,
        note,
        operation=nep.RESTAMP,
        outcome=nv.STILL_VALID,
        after="",
    )
    path = nep.sidecar_path(config, "personal", proposal.id)

    broken = json.loads(path.read_text(encoding="utf-8"))
    broken["proposal"]["coverage"] = nv.COVERAGE_PARTIAL
    path.write_text(json.dumps(broken), encoding="utf-8")

    with pytest.raises(nep.NoteEditSidecarError, match="coverage"):
        nep.read_sidecar(config, "personal", proposal.id)


def test_a_restamp_naming_no_stamp_date_is_refused(tmp_path: Path) -> None:
    """The stamp date is on the record, so a blank one leaves the accept a guess.

    `date.today()` at accept time is what this field replaced: a card previewed
    on one day and clicked on the next applied bytes the reviewer never saw, under
    an `exact` label. A record with no date of its own can only be applied that
    way, so it is refused instead.
    """
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)
    proposal = _file(
        config,
        note,
        operation=nep.RESTAMP,
        outcome=nv.STILL_VALID,
        after="",
    )
    path = nep.sidecar_path(config, "personal", proposal.id)

    for blank in ("", "the other day"):
        broken = json.loads(json.dumps(json.loads(path.read_text(encoding="utf-8"))))
        broken["proposal"]["stamp_date"] = blank
        path.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(nep.NoteEditSidecarError, match="stamp date"):
            nep.read_sidecar(config, "personal", proposal.id)


def test_a_verdict_no_verification_reaches_is_refused(tmp_path: Path) -> None:
    """`outcome` and `coverage` are a verification's, read against its own lists.

    Neither is a free-text label: the outcome names which verdict the proposal
    was filed from and the coverage is the limit on what that verdict may do, so
    a row carrying anything else describes a verification that did not happen.
    """
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)
    proposal = _file(config, note)
    path = nep.sidecar_path(config, "personal", proposal.id)
    good = json.loads(path.read_text(encoding="utf-8"))

    broken = json.loads(json.dumps(good))
    broken["proposal"]["outcome"] = "looks_fine_to_me"
    path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(nep.NoteEditSidecarError, match="outcome"):
        nep.read_sidecar(config, "personal", proposal.id)

    broken = json.loads(json.dumps(good))
    broken["proposal"]["coverage"] = "most_of_it"
    path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(nep.NoteEditSidecarError, match="coverage"):
        nep.read_sidecar(config, "personal", proposal.id)

    path.write_text(json.dumps(good), encoding="utf-8")
    assert nep.read_sidecar(config, "personal", proposal.id) == proposal


def test_a_restamp_records_the_date_it_was_filed_on(tmp_path: Path) -> None:
    """The date is the verification's, written down when it was asked.

    A re-stamp is the one operation whose bytes are computed rather than quoted,
    so the date has to travel with the proposal or the accept would stamp the day
    the button was pressed.
    """
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)

    restamp = _file(
        config, note, operation=nep.RESTAMP, outcome=nv.STILL_VALID, after=""
    )

    assert restamp.stamp_date == TODAY.isoformat()
    assert nep.read_sidecar(config, "personal", restamp.id).stamp_date == TODAY.isoformat()
    # And no other operation carries one, so there is nothing to apply to a
    # rewrite or a retirement.
    replaced = _file(config, note, before=FOURTH, after=FOURTH)
    assert replaced.stamp_date == ""
    assert nep.read_sidecar(config, "personal", replaced.id).stamp_date == ""


def test_a_corrupt_record_does_not_stop_the_others_from_settling(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)
    proposal = _file(config, note)
    nep.sidecar_path(config, "personal", "corrupt").write_text(
        "{not json", encoding="utf-8"
    )

    listed = nep.list_sidecars(config, "personal")

    assert [item.id for item in listed] == [proposal.id]
    assert nep.settle_note_edit(
        config, "personal", proposal.id, accepted=False
    ).settled != ""


def test_settling_something_that_was_never_filed_is_refused(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    config = _config(vault)
    _write(vault, NOTE, PLAIN)

    with pytest.raises(nep.NoteEditSidecarError, match="no note-edit proposal"):
        nep.settle_note_edit(config, "personal", "0" * 16, accepted=False)


# ── Retirement is attended only ────────────────────────────────────────────


def test_a_retire_proposal_carries_no_body(tmp_path: Path) -> None:
    """A retirement writes nothing, so keeping an after image would let a reader
    diff an "edit" that is really a removal."""
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)

    proposal = _file(
        config, note, operation=nep.RETIRE, outcome=nv.RETIRE, after=""
    )

    assert proposal.operation == nep.RETIRE
    assert proposal.after == ""
    assert nep.read_sidecar(config, "personal", proposal.id).after == ""


def test_nothing_in_this_module_can_reach_a_delete_primitive() -> None:
    """The guardrail `note_verification` holds, extended to the proposer.

    The proposer is the layer above the verifier: a defect here would reach a
    trash or a delete with nothing but a queue row in front of it, so the module
    imports no such primitive at all. `vault_review` (whose `trash_note` is
    attended and reversible) is reached from the accept handler, not from here.
    """
    for primitive in ("shutil", "send2trash", "os.remove", "os.unlink"):
        assert not hasattr(nep, primitive), f"{primitive} is reachable here"


def test_the_verification_itself_still_cannot_reach_a_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule #726-B set, unchanged: a retire is a human decision.

    A `needs_review` retirement is what this module files, and the verifier that
    produced it must still be unable to act on it. Its only mutation stays the
    note write, and no delete/trash library is imported into it.
    """
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)
    before_bytes = note.read_bytes()

    def _no_writes(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a retire must never reach a write")

    monkeypatch.setattr(nv.nr, "commit_note_change", _no_writes)
    for primitive in ("shutil", "send2trash"):
        assert not hasattr(nv, primitive)

    result = _needs_review(
        vault, config, note, outcome=nv.RETIRE, edit=None, evidence=(CITATION,)
    )

    assert result.status == nv.NEEDS_REVIEW
    assert note.read_bytes() == before_bytes
    assert mr.read_receipts(mr.journal_path(vault, None)) == []
    # And it is a verdict with nowhere to go until a proposal carries it.
    assert result.check is not None
    assert result.check.proposal_id == ""


def test_a_needs_review_verdict_files_a_proposal_that_settles_its_check(
    tmp_path: Path,
) -> None:
    """The end-to-end path this child exists for.

    A `needs_review` result, filed and then accepted, settles the check: the
    `proposal_id` is cleared and the note is no longer waiting on a proposal
    that is not in the queue any more.
    """
    vault = _vault(tmp_path)
    config = _config(vault)
    note = _write(vault, NOTE, PLAIN)

    result = _needs_review(vault, config, note)
    assert result.status == nv.NEEDS_REVIEW, result.message
    assert result.check is not None and result.check.proposal_id == ""

    proposal = _file(
        config,
        note,
        evidence=result.check.evidence,
        outcome=result.check.outcome,
        coverage=result.check.coverage,
        reason=result.check.reason,
    )
    filed = nep.read_sidecar(config, "personal", proposal.id)
    assert filed is not None and filed.proposal_id != ""
    # The filed proposal carries the verdict's own outcome and coverage, so a
    # reader of the queue can tell a re-stamp from a rewrite.
    assert filed.outcome == nv.UPDATE
    assert filed.coverage == nv.COVERAGE_COMPLETE

    nep.settle_note_edit(
        config, "personal", proposal.id, accepted=True, receipt_id="mrcpt_abc"
    )

    settled = nv.read_note_checks(vault)[NOTE]
    assert settled.proposal_id == ""
    assert settled.outcome == nv.UPDATE
    assert settled.reason == "the third floor no longer exists"


# ── Entry operations ───────────────────────────────────────────────────────
#
# The same proposal kind, one list item in. What is pinned here is the
# difference that matters: an entry operation is bound to an *entry*, and every
# way a record could describe an edit to no particular fact is refused on the
# way in and again on the way out.

ENTRY_PLAIN = (
    "---\ntype: note\nupdated: 2026-01-05\n---\n\n"
    "# Office\n\n"
    "- The office is on Via Verdi 12, third floor [verified: 2024-01-05]\n"
    "- The landlord is Bianchi\n"
)
"""The two facts an entry operation is about, one stamped and one not."""


def _entry_vault(
    tmp_path: Path, text: str = ENTRY_PLAIN, name: str = "entry-vault"
) -> tuple[Path, SimpleNamespace, Any]:
    """A vault holding a bullet list, and the entry the caller would name.

    A second vault rather than a second filing, because a queued row wins over a
    later verdict about the same entry by design — so a refusal to test needs no
    row in the way.
    """
    vault = _vault(tmp_path, name)
    _write(vault, NOTE, text)
    entry = ne.parse_note_entries(text, note_path=NOTE, workspace="personal").entries[0]
    return vault, _config(vault), entry


def _entry(text: str = ENTRY_PLAIN, index: int = 0) -> Any:
    return ne.parse_note_entries(text, note_path=NOTE, workspace="personal").entries[index]


def _file_entry(
    config: Any,
    entry: Any,
    *,
    operation: str = nep.REPLACE_ENTRY,
    outcome: str = nv.UPDATE,
    coverage: str = nv.COVERAGE_COMPLETE,
    after: str = "",
    delete: bool = False,
    today: date = TODAY,
    identity: str = "",
    fingerprint: str = "",
    text: str = ENTRY_PLAIN,
) -> nep.NoteEditProposal:
    """File one entry operation, with its images composed the way a caller would.

    The `after` image is the whole note with the entry spliced — that is what the
    record stores, and :func:`ciao.note_receipts.compose_entry_edit` is what
    produces it. ``delete`` is separate from ``operation`` so a test can file the
    record an operation and a body that contradict each other, which is exactly
    the case both the filing rules and the reader rules refuse. ``text`` is the
    note the record is filed against, for the notes whose first bullet starts at
    offset 0.
    """
    from ciao import note_receipts as nr

    composed = nr.compose_entry_edit(
        text, entry, replacement=after or None, delete=delete
    )[0]
    return nep.file_note_edit(
        config,
        workspace="personal",
        relative_path=NOTE,
        expected_revision=_revision(text),
        operation=operation,
        before=text,
        after=composed,
        outcome=outcome,
        coverage=coverage,
        evidence=(CITATION,),
        reason="the third floor no longer exists",
        today=today,
        entry_identity=identity or entry.identity,
        entry_fingerprint=fingerprint or entry.fingerprint,
    )


def _queued(vault: Path) -> int:
    """How many `[note_edit]` rows the queue is asking the owner about."""
    return _queue_text(vault).count(f"[{nep.KIND} ")


def test_an_entry_operation_files_one_row_and_pins_an_entry_check(
    tmp_path: Path,
) -> None:
    """One question per entry, and the check that holds it is the entry's.

    Two entries of one note at one revision are two different questions, and each
    gets its own row and its own check. An entry check keyed by the note path
    would be one row suppressing the other — the acceptance criterion this whole
    child exists to meet.
    """
    from ciao import entry_verification as ev

    vault, config, entry = _entry_vault(tmp_path)
    second = _entry(index=1)

    first = _file_entry(
        config, entry, after="- The office is on Via Verdi 12, fourth floor"
    )
    other = _file_entry(config, second, after="- The landlord is Bianchi Rossi")

    assert first.proposal_id and other.proposal_id
    assert first.id != other.id, "two entries of one note are two questions"
    assert _queued(vault) == 2
    checks = ev.read_entry_checks(vault)
    assert set(checks) == {entry.identity, second.identity}
    assert checks[entry.identity].proposal_id == first.proposal_id
    assert checks[entry.identity].note_path == NOTE
    assert checks[entry.identity].content_fingerprint == entry.fingerprint
    # The note map is untouched: an entry verdict is not a note verdict.
    assert nv.read_note_checks(vault) == {}


def test_the_bullet_names_the_entry_so_two_rows_stay_distinct(tmp_path: Path) -> None:
    """The head is the queue's dedupe key.

    A row whose text did not name the entry would collapse two bullets of one
    note into one row as soon as their reasons collapsed to the same words — and
    the one left out is silently never asked about.
    """
    vault, config, entry = _entry_vault(tmp_path)
    second = _entry(index=1)

    _file_entry(config, entry, after="- Moved to the fourth floor")
    _file_entry(config, second, after="- The landlord is Bianchi Rossi")

    queue = _queue_text(vault)
    assert entry.identity[:12] in queue
    assert second.identity[:12] in queue
    assert _queued(vault) == 2


def test_an_entry_record_recovers_its_own_replacement_from_the_span(
    tmp_path: Path,
) -> None:
    """The inverse of the splice, which is what lets an accept use the range
    helper instead of writing the stored image verbatim.

    An accept that trusted `after` would be writing a whole note's text on the
    strength of a record; going through the entry's own text means the managed
    helper resolves the entry again and refuses on any mismatch.
    """
    from ciao import note_receipts as nr

    vault, config, entry = _entry_vault(tmp_path)
    replacement = "- The office is on Via Verdi 12, fourth floor"
    proposal = _file_entry(config, entry, after=replacement)

    assert nep.entry_replacement(proposal) == replacement
    composed, refusal = nr.compose_entry_edit(
        ENTRY_PLAIN, entry, replacement=nep.entry_replacement(proposal)
    )
    assert not refusal
    assert composed == proposal.after
    assert proposal.before == ENTRY_PLAIN, "both images are the whole note"


def test_a_retire_entry_needs_an_empty_replacement_and_a_living_note(
    tmp_path: Path,
) -> None:
    """An entry retirement removes that span and nothing else.

    A "retirement" that also carries new text is not the record that was filed,
    and one whose splice empties the whole note is a note deletion wearing a
    bullet's clothes — the refusal Vault Review's trash exists to be asked about
    instead.
    """
    vault, config, entry = _entry_vault(tmp_path)

    proposal = _file_entry(
        config, entry, operation=nep.RETIRE_ENTRY, outcome=nv.RETIRE, delete=True
    )

    assert nep.entry_replacement(proposal) == ""
    assert proposal.after == ENTRY_PLAIN[: entry.start] + ENTRY_PLAIN[entry.end + 1 :]
    assert "The landlord is Bianchi" in proposal.after

    # A retirement that also replaces the entry is not the record that was
    # filed. A second vault, because the first filing's row is still queued and a
    # second verdict about the same entry loses to it by design.
    other_vault, other_config, other_entry = _entry_vault(tmp_path, ENTRY_PLAIN, "entry-vault-2")
    with pytest.raises(nep.NoteEditRefused, match="also replaces the entry"):
        _file_entry(
            other_config,
            other_entry,
            operation=nep.RETIRE_ENTRY,
            outcome=nv.RETIRE,
            after="- something else entirely",
        )


def test_the_first_bullet_of_a_bare_note_can_be_retired(tmp_path: Path) -> None:
    """A delete at offset 0 takes the line ending with it, and the inverse must agree.

    The bug this pins is a *refusal of a clean record*. A first-line bullet in a
    note with no frontmatter has a span ending immediately before its own ``\\n``,
    and the delete consumes that newline — so the after image does not end with
    the raw remainder of ``before`` after the span, it ends with that remainder
    minus its first character. Comparing against the raw remainder can never
    match, so the first bullet of a bare note could never be retired, and the
    error blamed the record rather than the check.
    """
    for line_ending in ("\n", "\r\n"):
        text = (
            f"- The office is on Via Verdi 12{line_ending}"
            f"- The landlord is Bianchi{line_ending}"
        )
        vault = _vault(tmp_path, f"bare-{len(line_ending)}")
        _write(vault, NOTE, text)
        entry = ne.parse_note_entries(
            text, note_path=NOTE, workspace="personal"
        ).entries[0]
        assert entry.start == 0, "this test is only about offset 0"

        proposal = _file_entry(
            _config(vault),
            entry,
            operation=nep.RETIRE_ENTRY,
            outcome=nv.RETIRE,
            delete=True,
            text=text,
        )

        assert proposal.after == text[entry.end + len(line_ending) :]
        assert nep.entry_replacement(proposal) == ""
        # And the recovered (empty) replacement composes back to the same bytes,
        # which is what the accept goes on to apply.
        from ciao import note_receipts as nr

        composed, refusal = nr.compose_entry_edit(
            text, entry, delete=True
        )
        assert not refusal
        assert composed == proposal.after


def test_a_forged_after_image_is_still_refused_by_the_inverse(tmp_path: Path) -> None:
    """Widening the inverse to account for the consumed newline must not widen it past
    the record it is checking.

    Both tails are tried, so the guard that matters is that neither one matches an
    ``after`` that kept text it should not have: a truncated image, one missing
    the note's own suffix, and one missing its prefix all have to stay refused.
    """
    text = "- a\n- b\n"
    entry = ne.parse_note_entries(text, note_path=NOTE, workspace="personal").entries[0]
    base = {
        "id": "x",
        "workspace": "personal",
        "relative_path": NOTE,
        "operation": nep.RETIRE_ENTRY,
        "expected_revision": _revision(text),
        "before": text,
        "outcome": nv.RETIRE,
        "coverage": nv.COVERAGE_COMPLETE,
        "evidence": (),
        "reason": "",
        "created_at": "2026-01-01T00:00:00Z",
        "proposal_id": "row-1",
        "receipt_id": "",
        "settled": "",
        "accepted": False,
        "stamp_date": "",
        "entry_identity": entry.identity,
        "entry_fingerprint": entry.fingerprint,
        "entry_span": (entry.start, entry.end),
    }

    for after in ("", "- a is still here\n", "- b\n- c\n", "- "):
        with pytest.raises(nep.NoteEditError, match="not the image this edit produced"):
            nep.entry_replacement(nep.NoteEditProposal(after=after, **base))


def test_a_restamp_entry_may_not_change_the_words(tmp_path: Path) -> None:
    """A reviewer clicking "re-stamp this entry" must never get a rewrite.

    The check is on the fingerprint rather than on the operation's name, because
    the fingerprint is what the entry's identity and its check are keyed on: a
    record that moved the words while claiming to only move the date would leave
    a check pointing at a fact that no longer exists.
    """
    vault, config, entry = _entry_vault(tmp_path)
    stamped = _entry(ENTRY_PLAIN.replace("2024-01-05", TODAY.isoformat()))

    good = _file_entry(
        config,
        entry,
        operation=nep.RESTAMP_ENTRY,
        outcome=nv.STILL_VALID,
        after=stamped.text,
    )

    assert nep.entry_replacement(good) == stamped.text
    assert ne.refresh_fingerprint(stamped.text) == entry.fingerprint
    assert good.stamp_date == TODAY.isoformat(), "the date is fixed at filing"

    _, other_config, other_entry = _entry_vault(tmp_path, ENTRY_PLAIN, "entry-vault-2")
    with pytest.raises(nep.NoteEditRefused, match="rewrite rather than a re-stamp"):
        _file_entry(
            other_config,
            other_entry,
            operation=nep.RESTAMP_ENTRY,
            outcome=nv.STILL_VALID,
            after="- The office moved to the fourth floor",
        )


def test_an_entry_operation_needs_an_identity_and_a_fingerprint(
    tmp_path: Path,
) -> None:
    """An edit with nothing to bind it to is a splice at an offset in a file."""
    vault, config, entry = _entry_vault(tmp_path)
    _, other_config, _ = _entry_vault(tmp_path, ENTRY_PLAIN, "entry-vault-2")

    with pytest.raises(nep.NoteEditRefused, match="identity"):
        # A key that is not an entry identity at all, and a key that is empty.
        _file_entry(config, entry, after="- Moved", identity="x" * 64)
    with pytest.raises(nep.NoteEditRefused, match="identity"):
        nep.file_note_edit(
            other_config,
            workspace="personal",
            relative_path=NOTE,
            expected_revision=_revision(ENTRY_PLAIN),
            operation=nep.REPLACE_ENTRY,
            before=ENTRY_PLAIN,
            after="- The office is on Via Verdi 12, fourth floor",
            outcome=nv.UPDATE,
            coverage=nv.COVERAGE_COMPLETE,
            evidence=(CITATION,),
            reason="",
            today=TODAY,
            entry_fingerprint=entry.fingerprint,
        )
    with pytest.raises(nep.NoteEditRefused, match="fingerprint"):
        _file_entry(config, entry, after="- Moved", fingerprint="a" * 64)
    with pytest.raises(nep.NoteEditRefused, match="whole-note"):
        _file_entry(config, entry, after="- Moved", operation=nep.REPLACE)


def test_filing_an_entry_edit_against_a_note_that_moved_is_refused(
    tmp_path: Path,
) -> None:
    """The span is measured, and a stale note's offsets describe different words.

    A whole-note `replace` can be filed against a revision the note has since
    left — the accept reports a conflict and the row survives. An entry operation
    cannot: the span it records would be the span of *other text*, so it is
    refused here, with nothing written.
    """
    vault, config, entry = _entry_vault(tmp_path)
    _write(vault, NOTE, ENTRY_PLAIN + "\nA new paragraph.\n")

    with pytest.raises(nep.NoteEditRefused, match="changed since this verdict"):
        _file_entry(
            config, entry, after="- The office is on Via Verdi 12, fourth floor"
        )
    assert _queue_text(vault) == ""


def test_filing_an_entry_edit_for_an_entry_that_is_not_there_is_refused(
    tmp_path: Path,
) -> None:
    """And for one whose fingerprint is not the text the verdict was about."""
    vault, config, entry = _entry_vault(tmp_path)

    with pytest.raises(nep.NoteEditRefused, match="no entry with identity"):
        _file_entry(config, entry, after="- Moved", identity="b" * 64)
    with pytest.raises(nep.NoteEditRefused, match="not the text this verdict"):
        _file_entry(config, entry, after="- Moved", fingerprint="c" * 64)


def test_the_reader_refuses_an_entry_record_whose_splice_does_not_add_up(
    tmp_path: Path,
) -> None:
    """Fail-closed on read, the other way round from the note map's drop-a-row.

    The images and the span are what get written, so a record that is not a clean
    inverse of its own splice is a record whose accept could only be a guess. And
    a `restamp_entry` whose replacement changes the fingerprint is refused here
    even though it is a well-formed row of the right types — which is the case a
    hand-edited file gets past filing.
    """
    from dataclasses import replace as _replace

    vault, config, entry = _entry_vault(tmp_path)
    proposal = _file_entry(config, entry, after="- The office is on Via Verdi 12, fourth")
    path = nep.sidecar_path(config, "personal", proposal.id)
    good = proposal.as_dict()

    def _write_row(row: dict[str, Any]) -> None:
        path.write_text(
            json.dumps({"schema": nep.SIDECAR_SCHEMA, "proposal": row}),
            encoding="utf-8",
        )

    _write_row({**good, "entry_span": "0,1"})
    with pytest.raises(nep.NoteEditSidecarError, match="entry_span"):
        nep.read_sidecar(config, "personal", proposal.id)

    _write_row({**good, "entry_span": [0, 1]})
    with pytest.raises(nep.NoteEditSidecarError, match="not the image this edit produced"):
        nep.read_sidecar(config, "personal", proposal.id)

    _write_row({**good, "entry_identity": ""})
    with pytest.raises(nep.NoteEditSidecarError, match="naming no entry identity"):
        nep.read_sidecar(config, "personal", proposal.id)

    _write_row(
        _replace(
            proposal, operation=nep.RESTAMP_ENTRY, stamp_date=TODAY.isoformat()
        ).as_dict()
    )
    with pytest.raises(nep.NoteEditSidecarError, match="rewrite rather than a re-stamp"):
        nep.read_sidecar(config, "personal", proposal.id)

    # A whole-note row that grew an entry span is not the record that was filed.
    whole = _file(config, _write(vault, NOTE, PLAIN))
    payload = json.loads(
        nep.sidecar_path(config, "personal", whole.id).read_text(encoding="utf-8")
    )
    payload["proposal"]["entry_span"] = [3, 9]
    nep.sidecar_path(config, "personal", whole.id).write_text(
        json.dumps(payload), encoding="utf-8"
    )
    with pytest.raises(nep.NoteEditSidecarError, match="whole-note edit that carries"):
        nep.read_sidecar(config, "personal", whole.id)

    # And the good row is still readable, so the refusals above are about the
    # record rather than about the file having been damaged.
    _write_row(good)
    assert nep.read_sidecar(config, "personal", proposal.id) == proposal


def test_a_settled_entry_row_clears_its_own_entry_check(tmp_path: Path) -> None:
    """A settlement is temporary, an entry exactly as a note.

    It clears the `proposal_id` and leaves the cooldown running, so the same fact
    is not asked about again this month — and the moment it is re-worded the
    fingerprint no longer matches and it is due again. There is deliberately no
    "refused forever" flag.
    """
    from dataclasses import replace as _replace

    from ciao import entry_verification as ev

    vault, config, entry = _entry_vault(tmp_path)
    proposal = _file_entry(config, entry, after="- The office is on Via Verdi 12, fourth")
    _dismiss_row(vault, proposal.id)

    settled = nep.settle_note_edit(config, "personal", proposal.id, accepted=True)

    assert settled.settled and settled.accepted
    check = ev.read_entry_checks(vault)[entry.identity]
    assert check.proposal_id == ""
    # The cooldown is still running, so this is not a second question tonight.
    assert not ev.should_check_entry(
        vault, entry.identity, entry.fingerprint, today=TODAY
    )
    # A re-worded fact is due again, because the check describes the words.
    assert ev.should_check_entry(vault, entry.identity, "b" * 64, today=TODAY)
    # And a settlement only ever clears the check that names it.
    ev.record_entry_check(
        vault,
        ev.EntryCheck(
            identity="c" * 64,
            note_path=NOTE,
            workspace="personal",
            content_fingerprint="d" * 64,
            outcome=nv.UNVERIFIED,
            checked_at=TODAY,
            retry_after=TODAY + timedelta(days=99),
            proposal_id=proposal.proposal_id,
        ),
    )
    nep.settle_note_edit(config, "personal", proposal.id, accepted=False)
    assert ev.read_entry_checks(vault)["c" * 64].proposal_id == proposal.proposal_id
    assert _replace
