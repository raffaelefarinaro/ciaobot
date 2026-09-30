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

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao import memory_receipts as mr
from ciao import note_edit_proposals as nep
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
