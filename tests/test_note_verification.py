"""Note verification: the autonomy rule, and the check state that enforces it.

``tests/test_note_receipts.py`` covers the write protocol these results come out
of and is read-only regression coverage for this one. What is pinned here is the
layer on top — the rules that decide whether a note is touched at all:

* **the autonomy rule**, in its four shapes: a ``still_valid`` with complete
  coverage and a citation that names the note re-stamps and leaves an undoable
  receipt; the same request with no evidence is ``unverified`` with nothing
  written and a cooldown; an ``update`` applies its exact replacement text only
  when the evidence is a citation somebody could re-open, and comes back for a
  human otherwise; a ``retire`` is never applied and no delete/trash/archive
  primitive is even reachable from this module;
* **the refusals that keep a write honest**: a stale ``expected_revision``, and
  an edit planned against text the note no longer holds, are both a conflict
  with the note untouched and *no check recorded* — so the caller re-reads the
  text that is actually there instead of trusting a verdict about a note that
  moved;
* **the check state**: versioned, workspace-scoped, keyed by vault-relative path
  and never holding an absolute one, stored in the reserved bookkeeping set so
  it cannot enter recall, and suppressing a question only while the *same*
  revision is inside its cooldown — an edited note is checked again;
* **idempotence**: re-running a request whose revision is already checked is a
  no-op, not a second receipt.

Every test uses a temporary synthetic vault. Nothing here reads or writes a real
install's notes, and no engine, service or scheduler is started.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao import fts_search
from ciao import memory_receipts as mr
from ciao import note_verification as nv
from ciao import vault_index
from ciao import vault_review as review

NOTE = "notes/office.md"

TODAY = date(2026, 3, 14)

PLAIN = (
    "---\ntype: note\nupdated: 2024-01-05\n---\n\n"
    "# Office\n\nThe office is on Via Verdi 12, third floor.\n"
)
"""An ordinary LF note whose ``updated:`` is two years stale — what
``memory_audit.find_stale_notes`` hands a verification pass."""

# A BOM, CRLF endings and an unterminated last line: the three things a
# text-mode round trip quietly rewrites. Both images are unterminated, so a
# write that appended a final newline would fail here too.
BOM_CRLF = (
    "﻿---\r\ntype: note\r\nupdated: 2024-01-05\r\n---\r\n\r\n"
    "# Office\r\n\r\nThe office is on Via Verdi 12, third floor."
)
BOM_CRLF_AFTER = (
    "﻿---\r\ntype: note\r\nupdated: 2024-01-05\r\n---\r\n\r\n"
    "# Office\r\n\r\nThe office is on Via Verdi 12, fourth floor."
)

CITATION = nv.Evidence(
    source_type="chat",
    source_ref="chat-2026-03-02",
    quoted="still Via Verdi 12, third floor",
    supports=f"{NOTE}: which floor the office is on",
    observed_at="2026-03-02",
)
"""A real citation: a source to re-open, the text seen there, and the assertion
it backs — which names this note, so it can carry a re-stamp."""

CONTRADICTS = nv.Evidence(
    source_type="url",
    source_ref="https://example.com/reception/vi-verdi-12",
    quoted="reception moved to the fourth floor in March",
    supports=f"{NOTE}: which floor the office is on",
    observed_at="2026-03-11",
)
"""A source that contradicts the note — the only kind that may rewrite it."""

UNCITED = nv.Evidence(
    source_type="chat",
    source_ref="",  # a source nobody could go and re-open
    quoted="I think reception moved",
    supports=f"{NOTE}: which floor the office is on",
)


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


def _config(vault: Path) -> SimpleNamespace:
    """A config whose answer to "where do this workspace's notes live" is *vault*."""
    return SimpleNamespace(workspace_vault_root=lambda _name: vault)


def _request(note: Path, **overrides: Any) -> nv.VerificationRequest:
    """One request against a note the caller has just read."""
    fields: dict[str, Any] = {
        "workspace": "personal",
        "relative_path": NOTE,
        "expected_revision": _revision(note),
        "outcome": nv.STILL_VALID,
        "coverage": nv.COVERAGE_COMPLETE,
        "reason": "checked against the March invoice",
    }
    fields.update(overrides)
    return nv.VerificationRequest(**fields)


def _verify(
    vault: Path, request: nv.VerificationRequest, **overrides: Any
) -> nv.VerificationResult:
    """One verification, the way the curation pass would make it."""
    fields: dict[str, Any] = {
        "vault_root": vault,
        "config": _config(vault),
        "today": TODAY,
    }
    fields.update(overrides)
    return nv.verify_note(request, **fields)


def _rows(vault: Path) -> list[dict[str, Any]]:
    """Every receipt row in this vault's journal."""
    return mr.read_receipts(mr.journal_path(vault, None))


def _checks(vault: Path) -> dict[str, nv.NoteCheck]:
    return nv.read_note_checks(vault)


def _payload(vault: Path) -> dict[str, Any]:
    """The check-state sidecar as JSON."""
    return json.loads(nv.note_check_state_path(vault).read_text(encoding="utf-8"))


def _move_office_to_fourth_floor(
    note: Path, **overrides: Any
) -> nv.VerificationRequest:
    """The update a contradicting source authorises for this note."""
    return _request(
        note,
        outcome=nv.UPDATE,
        evidence=(CONTRADICTS,),
        edit=nv.NoteEdit(
            before=note.read_bytes().decode("utf-8"),
            after=note.read_bytes().decode("utf-8").replace("third", "fourth"),
        ),
        **overrides,
    )


# ── still_valid: a re-stamp, undoable ──────────────────────────────────────


def test_still_valid_with_cited_evidence_restamps_and_undoes(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    note = _write(vault, NOTE, PLAIN)
    before_bytes = note.read_bytes()
    stamped = review._stamp_updated(PLAIN, TODAY.isoformat())[0]
    assert stamped is not None, "the fixture note must have frontmatter to stamp"

    result = _verify(vault, _request(note, evidence=(CITATION,)))

    assert result.status == nv.APPLIED, result.message
    assert result.receipt_id, "an applied re-stamp must name its receipt"
    # The whole note is what changed, and only the date: the stamp is
    # `vault_review`'s own parse, not a second copy of it that could drift.
    assert note.read_bytes() == stamped.encode("utf-8")
    assert f"updated: {TODAY.isoformat()}" in note.read_text(encoding="utf-8")

    # One journaled, undoable row — the same protocol every other note write uses.
    rows = _rows(vault)
    assert [row["kind"] for row in rows] == ["note_apply"]
    receipt = rows[0]
    assert receipt["status"] == mr.APPLIED and receipt["changed"] is True
    assert receipt["relative_path"] == NOTE
    assert mr.is_undoable(receipt)
    assert receipt["before_text"] == PLAIN
    # The evidence chain rides the receipt, so History can show why this write
    # happened without re-running the check.
    assert receipt["provenance"]["outcome"] == nv.STILL_VALID
    assert receipt["provenance"]["action"] == nv.AUTO_APPLY
    assert receipt["provenance"]["evidence"] == [CITATION.as_dict()]

    # The check describes the revision the write LEFT behind, so the next pass
    # reads a note it has a verdict for.
    check = result.check
    assert check is not None
    assert check.relative_path == NOTE
    assert check.outcome == nv.STILL_VALID
    assert check.content_revision == mr.content_revision(stamped)
    assert check.receipt_id == receipt["id"]
    assert _checks(vault) == {NOTE: check}

    mr.undo_receipt(receipt["id"], vault_root=vault)

    assert note.read_bytes() == before_bytes, "undo must restore the exact bytes"


def test_still_valid_without_evidence_is_unverified_and_not_written(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    note = _write(vault, NOTE, PLAIN)
    before_bytes = note.read_bytes()

    result = _verify(vault, _request(note, evidence=()))

    assert result.status == nv.UNVERIFIED, result.message
    assert result.receipt_id == ""
    assert note.read_bytes() == before_bytes, "nothing may be written"
    assert _rows(vault) == []
    # A verdict nobody could support is recorded as "I could not verify this",
    # not as a verification — and it does not come back tomorrow.
    check = result.check
    assert check is not None
    assert check.outcome == nv.UNVERIFIED
    assert check.receipt_id == ""
    assert nv.CHECK_COOLDOWN_DAYS == 30
    assert check.retry_after == TODAY + timedelta(days=nv.CHECK_COOLDOWN_DAYS)
    assert not nv.should_check(vault, NOTE, _revision(note), today=TODAY), (
        "an unverified note is not asked again inside its cooldown"
    )


# ── update: an edit only with a citation behind it ─────────────────────────


def test_update_with_a_contradicting_source_applies_the_exact_text(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    note = _write(vault, NOTE, BOM_CRLF)

    result = _verify(
        vault,
        nv.VerificationRequest(
            workspace="personal",
            relative_path=NOTE,
            expected_revision=_revision(note),
            outcome=nv.UPDATE,
            edit=nv.NoteEdit(before=BOM_CRLF, after=BOM_CRLF_AFTER),
            evidence=(CONTRADICTS,),
            coverage=nv.COVERAGE_COMPLETE,
            reason="the reception notice of 11 March",
        ),
    )

    assert result.status == nv.APPLIED, result.message
    # Byte-exact: the caller's replacement, with the BOM, the CRLF pairs and the
    # missing final newline all intact.
    assert note.read_bytes() == BOM_CRLF_AFTER.encode("utf-8")
    assert note.read_bytes().startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in note.read_bytes()
    assert not note.read_bytes().endswith(b"\n")

    receipts = [row for row in _rows(vault) if row["kind"] == "note_apply"]
    assert len(receipts) == 1
    assert receipts[0]["status"] == mr.APPLIED
    assert mr.is_undoable(receipts[0])
    assert receipts[0]["after_text"] == BOM_CRLF_AFTER
    assert receipts[0]["id"] == result.receipt_id
    assert result.check is not None
    assert result.check.outcome == nv.UPDATE
    assert result.check.content_revision == mr.content_revision(BOM_CRLF_AFTER)


def test_update_without_a_cited_source_needs_review_and_writes_nothing(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    note = _write(vault, NOTE, BOM_CRLF)
    before_bytes = note.read_bytes()

    result = _verify(
        vault,
        nv.VerificationRequest(
            workspace="personal",
            relative_path=NOTE,
            expected_revision=_revision(note),
            outcome=nv.UPDATE,
            edit=nv.NoteEdit(before=BOM_CRLF, after=BOM_CRLF_AFTER),
            evidence=(UNCITED,),
            coverage=nv.COVERAGE_COMPLETE,
        ),
    )

    assert result.status == nv.NEEDS_REVIEW, result.message
    assert result.receipt_id == ""
    assert note.read_bytes() == before_bytes, "the note must be untouched"
    assert _rows(vault) == [], "no receipt: nothing was written"
    assert result.check is not None
    assert result.check.outcome == nv.UPDATE
    assert result.check.receipt_id == ""
    assert "source_ref" in result.message, "the refusal must name the missing citation"


# ── retire: never applied, never deleted ───────────────────────────────────


def test_retire_is_never_applied_and_no_delete_primitive_is_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    note = _write(vault, NOTE, PLAIN)
    before_bytes = note.read_bytes()

    # The one mutation this module has. If a retire reaches it, the rule above
    # it stopped being the thing that decides.
    def _no_writes(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a retire must never reach a write")

    monkeypatch.setattr(nv.nr, "commit_note_change", _no_writes)
    # Nor is any delete/trash/archive library imported here at all, so there is
    # nothing in this module for a later edit to call by accident.
    for primitive in ("shutil", "send2trash"):
        assert not hasattr(nv, primitive), f"{primitive} is imported by this module"

    result = _verify(vault, _request(note, outcome=nv.RETIRE, evidence=(CONTRADICTS,)))

    assert result.status == nv.NEEDS_REVIEW, result.message
    assert result.receipt_id == ""
    assert note.exists(), "the note must still be there"
    assert note.read_bytes() == before_bytes, "byte-identical, untouched"
    assert _rows(vault) == []
    assert result.check is not None
    assert result.check.outcome == nv.RETIRE


# ── A moved note is a conflict, never an overwrite ──────────────────────────


def test_stale_expected_revision_is_a_conflict_that_records_nothing(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    note = _write(vault, NOTE, PLAIN)
    before_bytes = note.read_bytes()

    result = _verify(
        vault,
        _move_office_to_fourth_floor(note, expected_revision="0" * 64),
    )

    assert result.status == nv.CONFLICT, result.message
    assert result.receipt_id == ""
    assert note.read_bytes() == before_bytes
    assert _rows(vault) == [], "a refused write leaves no receipt"
    # No check either: the verdict was reached about text that is no longer
    # there, and recording it would suppress the re-read that has to happen.
    assert result.check is None
    assert _checks(vault) == {}


def test_an_edit_planned_against_other_text_is_a_conflict(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    note = _write(vault, NOTE, PLAIN)
    before_bytes = note.read_bytes()

    # The revision is current but the edit's before image is not what the note
    # holds: a caller that mixed two reads together. Writing would land a
    # replacement planned against text nobody had.
    result = _verify(
        vault,
        _request(
            note,
            outcome=nv.UPDATE,
            evidence=(CONTRADICTS,),
            edit=nv.NoteEdit(before="an older revision of this note", after="fourth"),
        ),
    )

    assert result.status == nv.CONFLICT, result.message
    assert note.read_bytes() == before_bytes
    assert _rows(vault) == []
    assert _checks(vault) == {}


# ── The check state ────────────────────────────────────────────────────────


def _recorded_check(
    vault: Path, *, revision: str, retry_after: date, proposal_id: str = ""
) -> nv.NoteCheck:
    """Store one check directly, as a pass would, and return it."""
    check = nv.NoteCheck(
        relative_path=NOTE,
        content_revision=revision,
        outcome=nv.UPDATE,
        checked_at=TODAY,
        retry_after=retry_after,
        evidence=(CONTRADICTS,),
        coverage=nv.COVERAGE_COMPLETE,
        reason="the third floor no longer exists",
        proposal_id=proposal_id,
        receipt_id="mrcpt_abc",
    )
    nv.record_note_check(vault, check)
    return check


def test_should_check_suppresses_only_the_same_revision(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    note = _write(vault, NOTE, PLAIN)
    revision = _revision(note)

    # Nothing recorded yet.
    assert nv.should_check(vault, NOTE, revision, today=TODAY)

    recorded = _recorded_check(vault, revision=revision, retry_after=TODAY + timedelta(days=30))
    # The same revision, inside the cooldown: the question was already answered.
    assert not nv.should_check(vault, NOTE, revision, today=TODAY)
    assert not nv.should_check(vault, NOTE, revision, today=TODAY + timedelta(days=29))
    # A note that changed has claims nobody verified, so it is due again
    # immediately: a check describes one revision and only that one.
    assert nv.should_check(vault, NOTE, "f" * 64, today=TODAY)
    # And the cooldown is an end, not a permanent suppression.
    assert nv.should_check(vault, NOTE, revision, today=TODAY + timedelta(days=30))
    # The stored row reads back as the record that was written.
    assert _checks(vault) == {NOTE: recorded}


def test_a_pending_proposal_suppresses_beyond_the_cooldown(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    note = _write(vault, NOTE, PLAIN)
    revision = _revision(note)
    later = TODAY + timedelta(days=365)

    _recorded_check(
        vault,
        revision=revision,
        retry_after=TODAY + timedelta(days=30),
        proposal_id="note_edit_7",  # set by #726-C, the proposal writer
    )

    # The proposal is waiting to be settled, so the note is not asked about
    # again whatever the cooldown says: asking would only file a second one.
    assert not nv.should_check(vault, NOTE, revision, today=later)
    # And it is still only about this revision.
    assert nv.should_check(vault, NOTE, "e" * 64, today=later)


def test_the_check_state_is_a_versioned_relative_path_sidecar(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    note = _write(vault, NOTE, PLAIN)
    result = _verify(vault, _request(note, evidence=(CITATION,)))
    check = result.check
    assert check is not None

    state = nv.note_check_state_path(vault)
    assert state.relative_to(vault).as_posix() == "Workspace/Note-Checks.json"
    payload = _payload(vault)
    assert payload["schema"] == nv.CHECK_STATE_SCHEMA == 1
    assert list(payload["notes"]) == [NOTE]
    row = payload["notes"][NOTE]
    assert row["content_revision"] == check.content_revision
    assert row["checked_at"] == TODAY.isoformat()
    assert row["retry_after"] == check.retry_after.isoformat()
    assert row["evidence"] == [CITATION.as_dict()]
    assert row["receipt_id"] == result.receipt_id
    # Read back into the same record it was written from.
    assert _checks(vault) == {NOTE: check}
    # No absolute path anywhere in the file: the state describes notes in the
    # vault it travels with, never where that vault used to live.
    body = state.read_text(encoding="utf-8")
    assert str(vault) not in body and str(note) not in body
    # And an absolute key is refused outright rather than quietly normalized into
    # a verdict filed under a key that does not name the note.
    with pytest.raises(nv.NoteCheckRefused):
        nv.record_note_check(
            vault,
            nv.NoteCheck(
                relative_path=str(note),
                content_revision="a" * 64,
                outcome=nv.STILL_VALID,
                checked_at=TODAY,
                retry_after=TODAY,
            ),
        )


def test_a_foreign_schema_honours_no_check(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    note = _write(vault, NOTE, PLAIN)
    state = nv.note_check_state_path(vault)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps({"schema": 99, "notes": {NOTE: {"content_revision": "a" * 64}}}),
        encoding="utf-8",
    )

    # A shape this version cannot read is not guessed at. Re-asking the note
    # costs a pass; honouring a check nobody wrote would be a verdict nobody
    # gave.
    assert _checks(vault) == {}
    assert nv.should_check(vault, NOTE, "a" * 64, today=TODAY)


def test_the_check_state_is_reserved_bookkeeping_and_never_indexed(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    state = nv.note_check_state_path(vault)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('{"schema": 1, "notes": {}}\n', encoding="utf-8")

    # In the reserved set both indexers consult (`fts_search` re-exports
    # `vault_index`'s), so the memory system's own paperwork can never rank
    # against the memories it manages.
    assert fts_search.RESERVED_UNINDEXED_FILES is vault_index.RESERVED_UNINDEXED_FILES
    assert state.name.casefold() in fts_search.RESERVED_UNINDEXED_FILES
    assert fts_search._is_reserved_key(
        (Path("Workspace") / nv.NOTE_CHECKS_NAME).as_posix()
    )
    assert vault_index.is_reserved_bookkeeping(Path("Workspace") / nv.NOTE_CHECKS_NAME)
    assert not vault_index.is_reserved_bookkeeping(Path("projects/Note-Checks.json")), (
        "the reservation is exact: a user's own note that shares the name stays "
        "searchable"
    )


# ── Idempotence ────────────────────────────────────────────────────────────


def test_re_running_a_checked_request_is_a_no_op(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    note = _write(vault, NOTE, PLAIN)
    request = _move_office_to_fourth_floor(note)

    first = _verify(vault, request)
    assert first.status == nv.APPLIED, first.message
    applied_bytes = note.read_bytes()

    second = _verify(vault, request)

    assert second.status == nv.ALREADY_CHECKED, second.message
    assert second.receipt_id == ""
    assert second.check == first.check
    assert note.read_bytes() == applied_bytes, "nothing may be rewritten"
    assert len(_rows(vault)) == 1, "the same revision must not be applied twice"
    # And once the note changes again, the recorded verdict no longer covers it.
    assert not nv.should_check(vault, NOTE, _revision(note), today=TODAY)
    note.write_bytes(PLAIN.encode("utf-8"))
    assert nv.should_check(vault, NOTE, _revision(note), today=TODAY)


# ── What the service refuses ───────────────────────────────────────────────


def test_verify_note_refuses_what_it_cannot_verify_or_must_not_write(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    other = _vault(tmp_path, "other-vault")
    other_note = _write(other, NOTE, PLAIN)
    note = _write(vault, NOTE, PLAIN)
    before_bytes = note.read_bytes()

    # An update whose replacement is empty would delete the note's body. That is
    # a deletion wearing an edit's clothes, and this service never deletes.
    emptied = _verify(
        vault,
        _request(
            note,
            outcome=nv.UPDATE,
            evidence=(CONTRADICTS,),
            edit=nv.NoteEdit(before=PLAIN, after=""),
        ),
    )
    assert emptied.status == nv.NEEDS_REVIEW
    assert note.read_bytes() == before_bytes
    assert [check.outcome for check in _checks(vault).values()] == [nv.UPDATE], (
        "the refusal is recorded, as the review row a caller turns into a proposal"
    )
    assert _rows(vault) == []

    # An outcome this version does not know is a caller bug, not a verdict: it
    # is reported and recorded nowhere, so nothing suppresses a later re-ask.
    unknown = _verify(other, _request(other_note, outcome="probably_fine"))
    assert unknown.status == nv.FAILED
    assert "unknown verification outcome" in unknown.message
    assert _checks(other) == {}
    assert other_note.read_bytes() == before_bytes

    # A vault that is not the one this workspace's notes live in: the check
    # state lives in the vault it describes, so pairing the two wrongly would
    # file the verdict in someone else's vault.
    foreign = _verify(
        other, _request(other_note, evidence=(CITATION,)), config=_config(vault)
    )
    assert foreign.status == nv.FAILED
    assert "not the vault configured" in foreign.message
    assert other_note.read_bytes() == before_bytes
    assert _checks(other) == {}
    assert _rows(other) == []