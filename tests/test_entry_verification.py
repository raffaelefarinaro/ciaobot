"""Entry-level verification: the autonomy rule at one entry's width, and the
check state that enforces it.

``tests/test_note_verification.py`` covers the whole-note service these results
come out of, and ``tests/test_note_receipts.py`` the write protocol underneath.
What is pinned here is the layer between them, and specifically the three
properties that only exist at this width:

* **the key**. A check is keyed by
  :func:`ciao.note_entries.entry_identity`, not by a path and not by a line
  number, so a verdict survives a note being reordered and a fact being inserted
  above it, while a re-worded entry — whose fingerprint moved — is checked again.
  That is the acceptance criterion the whole child exists for, and the reason a
  note-keyed state could not deliver it: one note cannot hold two verdicts about
  two facts;
* **the mutation**. A ``still_valid`` re-stamps *the entry's own*
  ``[verified:]`` date and changes nothing else, an ``update`` applies an exact
  replacement with citations behind it, and a ``retire`` is never applied — the
  same three-way rule 726-B reaches, with one genuinely new refusal: an entry
  with no usable stamp to replace is ``needs_review`` rather than a stamp written
  onto a fact nobody checked;
* **the refusals that keep a write honest**. An identity the note no longer
  holds, an entry whose fingerprint is not the one the caller read, a stale
  ``expected_revision`` and an edit planned against text the entry no longer has
  are all a ``conflict`` with the note untouched and *no check recorded*, so the
  caller re-reads the fact that is actually there instead of trusting a verdict
  about words that are gone.

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

from ciao import entry_verification as ev
from ciao import memory_receipts as mr
from ciao import note_entries as ne
from ciao import note_receipts as nr
from ciao import note_verification as nv
from ciao import vault_index

NOTE = "notes/office.md"
WORKSPACE = "personal"
TODAY = date(2026, 9, 19)

PLAIN = (
    "---\ntype: person\nupdated: 2026-09-01\n---\n\n"
    "# Office\n\n"
    "- The office is on Via Verdi 12, third floor [verified: 2024-01-05]\n"
    "- The landlord is Bianchi\n"
    "  and has been since 2019.\n"
)
"""Two facts, one stamped in 2024 and one never stamped and spanning two lines —
so the fixture can ask all three questions at once: a fact whose own date is past
its horizon, one that inherits the note's, and one whose span is more than the
opening line."""

CITATION = nv.Evidence(
    source_type="chat",
    source_ref="chat-2026-09-15",
    quoted="still Via Verdi 12, third floor",
    supports=f"{NOTE}: which floor the office is on",
    observed_at="2026-09-15",
)
"""A real citation: a source to re-open, the text seen there, and the assertion it
backs — which names the note, so it can carry a re-stamp."""

UNCITED = nv.Evidence(
    source_type="chat",
    source_ref="",
    quoted="",
    supports=f"{NOTE}: which floor the office is on",
    observed_at="2026-09-15",
)
"""A source nobody could go and re-open. A citation-shaped claim with nothing
behind it, which is the whole thing the rule refuses to auto-apply on."""


# ── Fixtures and helpers ───────────────────────────────────────────────────


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    (vault / "notes").mkdir(exist_ok=True)
    return vault


def _write(vault: Path, text: str = PLAIN) -> Path:
    path = vault / NOTE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _config(vault: Path) -> SimpleNamespace:
    return SimpleNamespace(workspace_vault_root=lambda _name: vault)


def _entries(text: str = PLAIN) -> tuple[ne.NoteEntry, ...]:
    return tuple(
        ne.parse_note_entries(text, note_path=NOTE, workspace=WORKSPACE).entries
    )


def _entry(text: str = PLAIN, index: int = 0) -> ne.NoteEntry:
    return _entries(text)[index]


def _request(entry: ne.NoteEntry, **overrides: Any) -> ev.EntryVerificationRequest:
    """One entry verification, as a caller that has read the note would send it."""
    fields: dict[str, Any] = {
        "workspace": WORKSPACE,
        "relative_path": NOTE,
        "identity": entry.identity,
        "entry_fingerprint": entry.fingerprint,
        "expected_revision": mr.content_revision(PLAIN),
        "outcome": ev.STILL_VALID,
        "evidence": (CITATION,),
        "coverage": ev.COVERAGE_COMPLETE,
    }
    fields.update(overrides)
    return ev.EntryVerificationRequest(**fields)


def _verify(
    vault: Path,
    request: ev.EntryVerificationRequest,
    **kwargs: Any,
) -> ev.EntryVerificationResult:
    return ev.verify_entry(
        request, vault_root=vault, config=_config(vault), today=TODAY, **kwargs
    )


def _entry_checks(vault: Path) -> dict[str, ev.EntryCheck]:
    return ev.read_entry_checks(vault)


def _note_checks(vault: Path) -> dict[str, nv.NoteCheck]:
    return nv.read_note_checks(vault)


def _payload(vault: Path) -> dict[str, Any]:
    return json.loads(ev.note_check_state_path(vault).read_text(encoding="utf-8"))


# ── The check state ────────────────────────────────────────────────────────


def test_an_entry_check_round_trips_in_the_shared_sidecar(tmp_path: Path) -> None:
    """The state is keyed by the entry's identity, in the note service's own file.

    The two halves share one document on purpose, and the acceptance criterion is
    that recording an entry check leaves the note checks alone: two writers each
    rewriting the file from what they read would drop the other map's cooldowns on
    every record.
    """
    vault = _vault(tmp_path)
    note = _write(vault)
    entry = _entry()
    nv.record_note_check(
        vault,
        nv.NoteCheck(
            relative_path=NOTE,
            content_revision=mr.content_revision(PLAIN),
            outcome=nv.UNVERIFIED,
            checked_at=TODAY,
            retry_after=TODAY + timedelta(days=nv.CHECK_COOLDOWN_DAYS),
        ),
    )

    result = _verify(vault, _request(entry))

    assert result.status == ev.APPLIED
    assert result.check is not None
    assert result.check.identity == entry.identity
    stored = _entry_checks(vault)
    assert list(stored) == [entry.identity]
    assert stored[entry.identity].content_fingerprint == entry.fingerprint
    assert stored[entry.identity].note_path == NOTE
    assert stored[entry.identity].receipt_id == result.receipt_id
    assert stored[entry.identity].checked_at == TODAY
    # The note map is intact, in the same file, under the same schema.
    assert list(_note_checks(vault)) == [NOTE]
    payload = _payload(vault)
    assert payload["schema"] == nv.CHECK_STATE_SCHEMA
    assert set(payload) == {"schema", "notes", "entries"}
    # The state travels with the vault: no absolute path anywhere in the file.
    body = ev.note_check_state_path(vault).read_text(encoding="utf-8")
    assert str(vault) not in body and str(note) not in body


def test_recording_an_entry_check_leaves_the_note_map_alone(tmp_path: Path) -> None:
    """The other direction too: a whole-note check must not drop the entry map.

    The failure this pins is silent and one-sided. A note check recorded after an
    entry check would rewrite the file from its own read, and the entry's
    cooldown — the thing holding off a second question about the same fact —
    would simply be gone, with nothing in the run's output to say so.
    """
    vault = _vault(tmp_path)
    _write(vault)
    entry = _entry()
    ev.record_entry_check(
        vault,
        ev.EntryCheck(
            identity=entry.identity,
            note_path=NOTE,
            workspace=WORKSPACE,
            content_fingerprint=entry.fingerprint,
            outcome=ev.UNVERIFIED,
            checked_at=TODAY,
            retry_after=TODAY + timedelta(days=ev.CHECK_COOLDOWN_DAYS),
        ),
    )

    nv.record_note_check(
        vault,
        nv.NoteCheck(
            relative_path=NOTE,
            content_revision=mr.content_revision(PLAIN),
            outcome=ev.UNVERIFIED,
            checked_at=TODAY,
            retry_after=TODAY + timedelta(days=nv.CHECK_COOLDOWN_DAYS),
            proposal_id="note_edit_1",
        ),
    )

    assert list(_entry_checks(vault)) == [entry.identity]
    assert _note_checks(vault)[NOTE].proposal_id == "note_edit_1"


def test_a_state_file_written_before_entries_existed_is_tolerated(
    tmp_path: Path,
) -> None:
    """A file with no ``entries`` map is a note map, not a broken file.

    A reader that demanded both maps would throw away every note check in a vault
    the moment the two were split across versions, which is the one loss this
    state must never take: the cooldowns and the `proposal_id` that is the only
    thing between a note and a second identical proposal.
    """
    vault = _vault(tmp_path)
    state = ev.note_check_state_path(vault)
    state.write_text(
        json.dumps(
            {
                "schema": nv.CHECK_STATE_SCHEMA,
                "notes": {
                    NOTE: {
                        "content_revision": "a" * 64,
                        "outcome": nv.UNVERIFIED,
                        "checked_at": TODAY.isoformat(),
                        "retry_after": (TODAY + timedelta(days=99)).isoformat(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert list(_note_checks(vault)) == [NOTE]
    assert _entry_checks(vault) == {}
    # And a writer adds the map rather than refusing the file.
    _write(vault)
    _verify(vault, _request(_entry()))
    assert list(_entry_checks(vault)) == [_entry().identity]


def test_a_foreign_schema_is_refused_for_both_maps(tmp_path: Path) -> None:
    """Unrecognized means unread, for the same reason and in the same direction."""
    vault = _vault(tmp_path)
    state = ev.note_check_state_path(vault)
    state.write_text('{"schema": 99, "notes": {}, "entries": {}}\n', encoding="utf-8")

    assert _entry_checks(vault) == {}
    with pytest.raises(ev.EntryCheckRefused):
        ev.record_entry_check(
            vault,
            ev.EntryCheck(
                identity=_entry().identity,
                note_path=NOTE,
                workspace=WORKSPACE,
                content_fingerprint=_entry().fingerprint,
                outcome=ev.UNVERIFIED,
                checked_at=TODAY,
                retry_after=TODAY,
            ),
        )
    assert state.read_text(encoding="utf-8") == (
        '{"schema": 99, "notes": {}, "entries": {}}\n'
    ), "a refused write leaves the file exactly as it was"


def test_a_key_that_is_not_an_entry_identity_is_refused(tmp_path: Path) -> None:
    """The map is keyed by a digest; anything else files a verdict nothing can read."""
    vault = _vault(tmp_path)
    _write(vault)
    row = ev.EntryCheck(
        identity=_entry().identity,
        note_path=NOTE,
        workspace=WORKSPACE,
        content_fingerprint=_entry().fingerprint,
        outcome=ev.UNVERIFIED,
        checked_at=TODAY,
        retry_after=TODAY,
    )

    for key in ("notes/office.md", "not-hex", "", "A" * 64, "a" * 63):
        from dataclasses import replace

        with pytest.raises(ev.EntryCheckRefused):
            ev.record_entry_check(vault, replace(row, identity=key))
    assert ev.note_check_state_path(vault).exists() is False


def test_a_row_without_a_fingerprint_or_a_date_is_skipped(tmp_path: Path) -> None:
    """A check nobody can place against the entry in front of them suppresses
    nothing, so honouring it would be taking a verdict on trust."""
    vault = _vault(tmp_path)
    state = ev.note_check_state_path(vault)
    entry = _entry()
    state.write_text(
        json.dumps(
            {
                "schema": nv.CHECK_STATE_SCHEMA,
                "notes": {},
                "entries": {
                    # No fingerprint and no date: a verdict with nothing to place
                    # it by.
                    entry.identity: {"outcome": ev.STILL_VALID},
                    # A date but no fingerprint.
                    "b" * 64: {"checked_at": TODAY.isoformat()},
                    # A fingerprint but no date.
                    "c" * 64: {"content_fingerprint": "d" * 64},
                    # Both, but naming a note that is not a vault-relative path —
                    # a verdict about a file in some other install's vault.
                    "e" * 64: {
                        "content_fingerprint": "f" * 64,
                        "note_path": "/elsewhere/notes/office.md",
                        "checked_at": TODAY.isoformat(),
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    assert _entry_checks(vault) == {}


# ── should_check_entry ─────────────────────────────────────────────────────


def test_should_check_entry_suppresses_only_the_same_fingerprint(
    tmp_path: Path,
) -> None:
    """The acceptance criterion in one predicate.

    A settled check suppresses the fingerprint it was recorded against and
    nothing else: same entry, re-worded, is a claim nobody verified. A re-*stamped*
    entry is not, because the stamp is metadata and
    :func:`ciao.note_entries.refresh_fingerprint` ignores it.
    """
    vault = _vault(tmp_path)
    _write(vault)
    entry = _entry()
    assert ev.should_check_entry(
        vault, entry.identity, entry.fingerprint, today=TODAY
    )

    ev.record_entry_check(
        vault,
        ev.EntryCheck(
            identity=entry.identity,
            note_path=NOTE,
            workspace=WORKSPACE,
            content_fingerprint=entry.fingerprint,
            outcome=ev.UNVERIFIED,
            checked_at=TODAY,
            retry_after=TODAY + timedelta(days=ev.CHECK_COOLDOWN_DAYS),
        ),
    )

    assert not ev.should_check_entry(
        vault, entry.identity, entry.fingerprint, today=TODAY
    )
    # A different fact, and a different note's copy of the same words, are both
    # still due: the identity is what says which entry this was.
    assert ev.should_check_entry(vault, entry.identity, "b" * 64, today=TODAY)
    assert ev.should_check_entry(vault, "c" * 64, entry.fingerprint, today=TODAY)


def test_an_expired_cooldown_re_checks_and_a_pending_proposal_does_not(
    tmp_path: Path,
) -> None:
    """Asking is answered; re-asking is the bug. A row waiting on a person is
    the answer."""
    vault = _vault(tmp_path)
    _write(vault)
    entry = _entry()
    settled = ev.EntryCheck(
        identity=entry.identity,
        note_path=NOTE,
        workspace=WORKSPACE,
        content_fingerprint=entry.fingerprint,
        outcome=ev.UNVERIFIED,
        checked_at=TODAY - timedelta(days=ev.CHECK_COOLDOWN_DAYS),
        retry_after=TODAY,
    )
    ev.record_entry_check(vault, settled)
    assert ev.should_check_entry(
        vault, entry.identity, entry.fingerprint, today=TODAY
    ), "the cooldown ran out today, so the question is open again"

    from dataclasses import replace

    ev.record_entry_check(
        vault, replace(settled, proposal_id="note_edit_9", retry_after=TODAY + timedelta(days=3650))
    )
    assert not ev.should_check_entry(
        vault, entry.identity, entry.fingerprint, today=TODAY
    ), "a proposal somebody has not decided yet settles it whatever the cooldown"


# ── The autonomy rule ──────────────────────────────────────────────────────


def test_a_still_valid_entry_restamps_only_its_own_stamp(tmp_path: Path) -> None:
    """The one mutation that needs no new text: the entry's own date.

    And it changes nothing about the fact, which is the property the check state
    depends on — so a verified entry stops being work immediately.
    """
    vault = _vault(tmp_path)
    note = _write(vault)
    entry = _entry()

    result = _verify(vault, _request(entry))

    assert result.status == ev.APPLIED
    assert result.receipt_id
    after = note.read_text(encoding="utf-8")
    assert "[verified: 2026-09-19]" in after
    assert "third floor" in after, "the words are untouched"
    assert not ev.should_check_entry(
        vault, entry.identity, entry.fingerprint, today=TODAY
    )
    receipt = [
        row
        for row in mr.read_receipts(mr.journal_path(vault, None))
        if row["kind"] == nr.NOTE_APPLY
    ][-1]
    assert receipt["provenance"]["entry_identity"] == entry.identity
    assert receipt["provenance"]["outcome"] == ev.STILL_VALID


def test_an_applied_update_is_checked_under_the_new_text_identity(
    tmp_path: Path,
) -> None:
    """The check has to name the entry the write LEFT, or it names nothing.

    :func:`ciao.note_entries.entry_identity` digests the entry's own fingerprint,
    so an ``update`` mints a different identity for the same bullet. Filing the
    new fingerprint under the old identity produced a row nothing can ever find
    again — ``should_check_entry(new_identity, new_fingerprint)`` looks up a key
    that is not in the map — while the old row went on describing text the note
    no longer held, for the length of the cooldown. The check is therefore filed
    against the identity the *written* note gives the entry, and the row for the
    entry as it was is dropped in the same locked write.
    """
    vault = _vault(tmp_path)
    note = _write(vault)
    entry = _entry()
    replacement = "- The office is on Via Verdi 12, fourth floor"

    result = _verify(
        vault,
        _request(
            entry,
            outcome=ev.UPDATE,
            edit=ev.EntryEdit(before=entry.text, after=replacement),
        ),
    )

    assert result.status == ev.APPLIED
    after = note.read_text(encoding="utf-8")
    new_identity = ne.parse_note_entries(
        after, note_path=NOTE, workspace=WORKSPACE
    ).entries[0].identity
    assert new_identity != entry.identity, "a re-worded fact is a different entry"
    checks = _entry_checks(vault)
    # Exactly one row, and it is the new identity's — findable by the very
    # predicate the worklist uses to decide there is nothing to do.
    assert set(checks) == {new_identity}
    assert entry.identity not in checks, "the old row describes text that is gone"
    assert checks[new_identity].content_fingerprint == ne.refresh_fingerprint(
        replacement
    )
    assert not ev.should_check_entry(
        vault, new_identity, ne.refresh_fingerprint(replacement), today=TODAY
    )
    # And the re-worded fact is checked, not suppressed: the check on the entry
    # as it was must not carry over to text nobody judged.
    assert result.check is not None
    assert result.check.identity == new_identity
    assert result.check.receipt_id == result.receipt_id


def test_a_restamp_needs_complete_coverage_and_a_real_citation(
    tmp_path: Path,
) -> None:
    """726-B's rule, unchanged: "nobody checked it" is not "still true".

    Recorded as ``unverified`` rather than refused, which is the point: an
    unciteable judgement is a real answer with a cooldown, and a re-ask is not.
    """
    vault = _vault(tmp_path)
    note = _write(vault)
    entry = _entry()

    partial = _verify(vault, _request(entry, coverage=ev.COVERAGE_PARTIAL))
    assert partial.status == ev.UNVERIFIED
    assert partial.check is not None
    assert partial.check.outcome == ev.UNVERIFIED
    assert not ev.should_check_entry(
        vault, entry.identity, entry.fingerprint, today=TODAY
    )



def test_a_restamp_needs_a_citation_somebody_could_re_open(tmp_path: Path) -> None:
    """A claim with a citation's shape and nothing behind it is still nothing
    behind it.

    A separate vault from the coverage case, because the first verdict already
    answered this question and a second one would come back ``already_checked`` —
    which is the cooldown doing its job, and not what this test is about.
    """
    vault = _vault(tmp_path)
    note = _write(vault)
    entry = _entry()

    result = _verify(vault, _request(entry, evidence=(UNCITED,)))

    assert result.status == ev.UNVERIFIED
    assert result.check is not None
    assert result.check.outcome == ev.UNVERIFIED
    assert "no cited source" in result.check.reason
    assert note.read_text(encoding="utf-8") == PLAIN, "nothing was written"


def test_a_still_valid_entry_with_no_stamp_to_replace_needs_a_person(
    tmp_path: Path) -> None:
    """The one rule that genuinely differs from 726-B's, and why.

    A note's frontmatter ``updated:`` can be added without asserting anything
    about the note, so 726-B declines only as a courtesy. An entry's
    ``[verified:]`` token IS the claim, and writing one nobody checked would be
    inventing a verification — so it comes back for a person instead.
    """
    vault = _vault(tmp_path)
    note = _write(vault)
    unstamped = _entry(index=1)

    result = _verify(vault, _request(unstamped))

    assert result.status == ev.NEEDS_REVIEW
    assert result.check is not None
    assert result.check.outcome == ev.STILL_VALID
    assert "stamp" in result.message
    assert note.read_text(encoding="utf-8") == PLAIN


def test_an_update_applies_an_exact_replacement(tmp_path: Path) -> None:
    """The agent's own text, verbatim, through the entry-range helper.

    The note's other fact and its prose are still there afterwards, which is the
    difference between this and the whole-note write the same verdict files
    there.
    """
    vault = _vault(tmp_path)
    note = _write(vault)
    entry = _entry()
    replacement = "- The office is on Via Verdi 12, fourth floor"

    result = _verify(
        vault,
        _request(
            entry,
            outcome=ev.UPDATE,
            edit=ev.EntryEdit(before=entry.text, after=replacement),
        ),
    )

    assert result.status == ev.APPLIED
    after = note.read_text(encoding="utf-8")
    assert replacement in after
    assert "third floor" not in after
    assert "The landlord is Bianchi" in after
    assert "Some prose" not in after  # PLAIN has no prose; the rest is untouched.
    assert after == PLAIN[: entry.start] + replacement + PLAIN[entry.end :]
    # And the check now describes the NEW fingerprint, so the re-worded fact is
    # asked about again when its own horizon runs out rather than being pinned to
    # a verdict about words that are gone.
    assert result.check is not None
    assert result.check.content_fingerprint == ne.refresh_fingerprint(replacement)


def test_an_update_with_an_uncited_row_among_citations_needs_a_person(
    tmp_path: Path,
) -> None:
    """One good row among three uncited ones leaves two claims with nothing
    behind them, so the test is on the whole set."""
    vault = _vault(tmp_path)
    note = _write(vault)
    entry = _entry()

    result = _verify(
        vault,
        _request(
            entry,
            outcome=ev.UPDATE,
            edit=ev.EntryEdit(before=entry.text, after="- Moved to the fourth floor"),
            evidence=(CITATION, UNCITED),
        ),
    )

    assert result.status == ev.NEEDS_REVIEW
    assert "3 of" not in result.message  # one row of two, not three
    assert "evidence rows" in result.message
    assert note.read_text(encoding="utf-8") == PLAIN


def test_a_retire_is_never_applied(tmp_path: Path) -> None:
    """Retirement is a human decision, so this service cannot reach one.

    The guard is structural, not a rule that could be dropped: this module
    imports no delete, trash or archive primitive at all, and the delete it does
    own is a span inside a file it was handed the exact text of, through a
    receipt that can undo it. Checked over the parsed module rather than its
    source, so a docstring *naming* ``trash_note`` to say this module does not
    reach it does not trip the guard.
    """
    import ast
    import inspect

    imported: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(ev))):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported == {
        "__future__",
        "datetime",
        "dataclasses",
        "logging",
        "pathlib",
        "typing",
        "ciao",
    }
    # And the one delete primitive it does own is a span, delegated to the range
    # helper — which is a splice plus a whole-note receipt, never a trash.
    assert "apply_entry_edit" in inspect.getsource(ev)
    assert "commit_note_change" not in inspect.getsource(ev)

    vault = _vault(tmp_path)
    note = _write(vault)
    entry = _entry()

    result = _verify(vault, _request(entry, outcome=ev.RETIRE))

    assert result.status == ev.NEEDS_REVIEW
    assert result.check is not None
    assert result.check.outcome == ev.RETIRE
    assert "human decision" in result.message
    assert note.read_text(encoding="utf-8") == PLAIN


# ── The refusals that keep a write honest ──────────────────────────────────


def test_an_identity_the_note_no_longer_holds_is_a_conflict(
    tmp_path: Path,
) -> None:
    """Nothing written and *no check recorded*, so the caller re-reads.

    Recording a check here would pin this entry's identity to a verdict about a
    fact the note no longer has, for the length of the cooldown — which is the
    one thing a durable check state must never do.
    """
    vault = _vault(tmp_path)
    note = _write(vault)
    entry = _entry()

    result = _verify(vault, _request(entry, identity="b" * 64))

    assert result.status == ev.CONFLICT
    assert result.check is None
    assert note.read_text(encoding="utf-8") == PLAIN
    assert _entry_checks(vault) == {}
    assert _note_checks(vault) == {}, "an entry check is not filed under the path"


def test_an_entry_that_is_not_the_text_the_caller_read_is_a_conflict(
    tmp_path: Path,
) -> None:
    """The second, independent binding.

    `expected_revision` already proves the note is the one the caller read, so
    the only way this can fail is a request built for a different version of the
    fact — which is the mistake this field exists to catch.
    """
    vault = _vault(tmp_path)
    note = _write(vault)
    entry = _entry()

    result = _verify(vault, _request(entry, entry_fingerprint="b" * 64))

    assert result.status == ev.CONFLICT
    assert "fingerprint" in result.message
    assert note.read_text(encoding="utf-8") == PLAIN
    assert _entry_checks(vault) == {}


def test_a_stale_expected_revision_is_a_conflict(tmp_path: Path) -> None:
    """The revision is checked before the verdict is decided, not only before the
    write — the outcomes that write nothing are the ones that reach this check by
    recording, so a verdict about a revision the note is no longer in would
    otherwise be recorded against the text that is actually there."""
    vault = _vault(tmp_path)
    note = _write(vault)
    entry = _entry()

    result = _verify(vault, _request(entry, expected_revision="c" * 64))

    assert result.status == ev.CONFLICT
    assert result.check is None
    assert "read the note again" in result.message
    assert note.read_text(encoding="utf-8") == PLAIN
    assert _entry_checks(vault) == {}


def test_a_missing_expected_revision_is_a_failure_not_a_conflict(
    tmp_path: Path,
) -> None:
    """A caller that cannot say what it read is not a stale caller."""
    vault = _vault(tmp_path)
    _write(vault)

    result = _verify(vault, _request(_entry(), expected_revision=""))

    assert result.status == ev.FAILED
    assert "expected revision" in result.message
    assert _entry_checks(vault) == {}


def test_an_outcome_this_version_does_not_know_is_a_caller_bug(
    tmp_path: Path,
) -> None:
    """Reported before the cooldown, so it cannot hide behind "already checked"
    for the rest of the month."""
    vault = _vault(tmp_path)
    note = _write(vault)
    entry = _entry()
    ev.record_entry_check(
        vault,
        ev.EntryCheck(
            identity=entry.identity,
            note_path=NOTE,
            workspace=WORKSPACE,
            content_fingerprint=entry.fingerprint,
            outcome=ev.UNVERIFIED,
            checked_at=TODAY,
            retry_after=TODAY + timedelta(days=99),
        ),
    )

    result = _verify(vault, _request(entry, outcome="probably_fine"))

    assert result.status == ev.FAILED
    assert "caller bug" in result.message


def test_a_settled_fingerprint_is_not_judged_twice(tmp_path: Path) -> None:
    """Re-running a request whose entry is already checked is a no-op, not a
    second receipt."""
    vault = _vault(tmp_path)
    note = _write(vault)
    entry = _entry()
    first = _verify(vault, _request(entry))

    again = _verify(vault, _request(entry))

    assert first.status == ev.APPLIED
    assert again.status == ev.ALREADY_CHECKED
    assert again.receipt_id == ""
    assert again.check is not None
    assert again.check.receipt_id == first.receipt_id
    rows = [
        row
        for row in mr.read_receipts(mr.journal_path(vault, None))
        if row["kind"] == nr.NOTE_APPLY
    ]
    assert len(rows) == 1
    assert note.read_text(encoding="utf-8").count("[verified: 2026-09-19]") == 1


def test_the_check_state_is_reserved_bookkeeping_and_never_indexed(
    tmp_path: Path,
) -> None:
    """Entry checks live in the note service's file, which is already reserved.

    So this costs nothing: the memory system's own paperwork cannot rank against
    the memories it manages, and there is no second name to add to the set for a
    reader to get wrong.
    """
    from ciao import fts_search

    assert vault_index.is_reserved_bookkeeping(
        Path("Workspace") / ev.note_check_state_path(tmp_path).name
    )
    assert fts_search.RESERVED_UNINDEXED_FILES is vault_index.RESERVED_UNINDEXED_FILES


# ── Stamping ───────────────────────────────────────────────────────────────


def test_a_stamp_replaces_the_token_rather_than_appending_one(tmp_path: Path) -> None:
    """An entry carrying two stamps is one
    :data:`ciao.note_entries.DIAG_STAMP_DUPLICATE` away from a fact whose
    fingerprint moved for a reason nobody asked about."""
    entry = _entry()
    once = ev.stamp_entry(entry.text, "2026-09-19")
    twice = ev.stamp_entry(once, "2026-10-01")

    assert twice.count("[verified:") == 1
    assert "[verified: 2026-10-01]" in twice
    assert ne.parse_verification_stamp(twice).date == date(2026, 10, 1)
    assert ne.refresh_fingerprint(twice) == entry.fingerprint


def test_a_stamp_on_an_unstamped_entry_keeps_the_multiline_entry_whole(
    tmp_path: Path,
) -> None:
    """The multi-line case, which is where a partial replacement would be a
    silent data loss: the stamp goes on the opening line and every continuation
    line survives."""
    entry = _entry(index=1)
    assert "\n" in entry.text

    stamped = ev.stamp_entry(entry.text, "2026-09-19")

    assert stamped.splitlines() == [
        "- The landlord is Bianchi [verified: 2026-09-19]",
        "  and has been since 2019.",
    ]
    assert ne.refresh_fingerprint(stamped) == entry.fingerprint
    after, refusal = nr.compose_entry_edit(PLAIN, entry, replacement=stamped)
    assert not refusal
    assert after == PLAIN[: entry.start] + stamped + PLAIN[entry.end :]


def test_a_stamp_preserves_a_crlf_line_ending(tmp_path: Path) -> None:
    """The carriage return is not part of the stamp, and must not be eaten by a
    text-mode round trip that thinks it is."""
    stamped = ev.stamp_entry("- A fact [verified: 2024-01-01]\r\n  more", "2026-09-19")

    assert stamped == "- A fact [verified: 2026-09-19]\r\n  more"
    assert ne.parse_verification_stamp(stamped.split("\n")[0]) is not None


def test_an_impossible_stamp_date_is_refused_rather_than_written(tmp_path: Path) -> None:
    """A stamp nobody could have verified on a day that never happened is a claim
    the parser will report as impossible, so it is not worth writing."""
    with pytest.raises(ValueError):
        ev.stamp_entry("- A fact", "2026-13-01")


def test_a_stamp_replaces_a_token_the_parser_could_not_believe(
    tmp_path: Path,
) -> None:
    """An unusable *trailing* stamp is replaced, not appended beside.

    The failure this pins is the entry's own diagnostic firing on this tool's
    write: a `[verified: 2026-13-01]` the parser reports as impossible, and a
    near-miss `[verified 2026-01-01]` spelling it reports as malformed, are both
    claims the line already makes. Appending a good stamp beside either left two
    tokens, which the next parse reports as
    :data:`ciao.note_entries.DIAG_STAMP_DUPLICATE` — the diagnostic that exists to
    say "a tool did the wrong thing here" — and changed the fact's fingerprint
    for a reason nobody asked about.

    Every *trailing* spelling of the token is covered — including the
    `[verified : 2026-01-01]` one, where the colon is separated by a space,
    which the parser reads as a claim and refuses. The result is one token, empty
    diagnostics, and a line whose words are exactly the words it had with the
    claim taken out of it.

    A token the parser does not read as trailing because words follow it is a
    different case, and
    :func:`test_a_stamp_leaves_a_mid_line_token_and_the_verdict_alone` owns it:
    those are prose, and the writer is not entitled to decide they are not.
    """
    for broken, words in (
        ("- a fact [verified: 2026-13-01]", "- a fact"),
        ("- a fact [verified 2026-01-01]", "- a fact"),
        ("- a fact [verified : 2026-01-01]", "- a fact"),
    ):
        stamped = ev.stamp_entry(broken, "2026-09-19")
        parsed = ne.parse_note_entries(
            stamped, note_path=NOTE, workspace=WORKSPACE
        ).entries[0]

        assert stamped == f"{words} [verified: 2026-09-19]", f"{broken!r} kept a token"
        assert stamped.count("[verified") == 1, f"{broken!r} kept a second token"
        assert parsed.diagnostics == (), f"{broken!r} left a diagnostic behind"
        assert ne.parse_verification_stamp(stamped) == ne.EntryStamp(
            "[verified: 2026-09-19]", date(2026, 9, 19), True, ""
        )
        # The words are the entry's own, so the repaired line fingerprints as the
        # same fact the entry always was — the claim was never part of it.
        assert ne.refresh_fingerprint(stamped) == ne.refresh_fingerprint(
            f"{words} [verified: 2024-01-01]"
        )


def test_a_stamp_leaves_a_mid_line_token_and_the_verdict_alone() -> None:
    """A `[verified: …]` in the middle of a sentence is prose, and stays prose.

    ``- Config [verified: 2020 spec] changed in v3`` states a fact *about* a 2020
    spec, and :mod:`ciao.note_entries` reads the stamp of that line as the
    trailing token only — a mid-line token is not a claim, is left in the entry's
    text, and is part of the words the fingerprint hashes. So an unattended
    `still_valid`, which is only asked to move a date, must not delete it: doing
    so rewrites the fact, changes its fingerprint and its ``entry_identity``
    (which digests the fingerprint) with nothing to say so, and the check it
    files under the *new* identity cannot be found under the old one again.

    Two things are pinned, and the second is the one that matters at the width an
    operator sees: :func:`ciao.entry_verification.stamp_entry` keeps the words,
    and the *verdict* is ``needs_review`` with nothing written — because a line
    carrying a second claim is ambiguous, and who is right about it is a reader's
    call, not a nightly pass's.
    """
    line = "- Config [verified: 2020 spec] changed in v3 [verified: 2024-01-01]"

    stamped = ev.stamp_entry(line, "2026-09-19")

    assert stamped == (
        "- Config [verified: 2020 spec] changed in v3 [verified: 2026-09-19]"
    )
    # The prose is untouched and the entry is still the same fact: a re-stamp
    # that rewrote the fact could not be invisible, which is the whole promise.
    assert ne.refresh_fingerprint(stamped) == ne.refresh_fingerprint(line)

    note = f"---\ntype: person\nupdated: 2024-01-01\n---\n\n# Config\n\n{line}\n"
    entry = _entry(note)
    plan = ev.plan_entry_verification(
        _request(entry, expected_revision=mr.content_revision(note)),
        entry=entry,
        today=TODAY,
    )

    assert plan.status == ev.NEEDS_REVIEW, plan.reason
    assert plan.action == ev.PROPOSE, "nothing may be written without a reader"
    assert plan.replacement == "", "and the text is the caller's to write"
    assert "[verified: 2020 spec]" in plan.reason, plan.reason


def test_a_release_gives_the_entry_its_question_back(tmp_path: Path) -> None:
    """A verdict nobody was asked about must not sit on its cooldown for a month.

    The check is recorded by the service *before* the proposal is filed, so a
    filing that fails leaves it settled against a question that does not exist:
    the entry is not planned again for 30 days and nothing re-files it. Releasing
    keeps the verdict and its citations on the row — this is a log — and moves
    the cooldown back to the day the check was recorded, which is exactly what
    ``_check_settles`` compares against.
    """
    vault = _vault(tmp_path)
    _write(vault)
    entry = _entry()

    result = _verify(vault, _request(entry, outcome=ev.RETIRE))

    assert result.status == ev.NEEDS_REVIEW
    assert not ev.should_check_entry(
        vault, entry.identity, entry.fingerprint, today=TODAY
    ), "a pending verdict holds the entry off"

    ev.release_entry_check(vault, entry.identity, today=TODAY)

    row = _entry_checks(vault)[entry.identity]
    assert row.outcome == ev.RETIRE, "the verdict itself is kept"
    assert row.retry_after == row.checked_at
    assert ev.should_check_entry(vault, entry.identity, entry.fingerprint, today=TODAY)
    # And a key that is not an entry identity, or a row that is not there, is
    # left exactly as it is rather than raising out of a caller's error path.
    ev.release_entry_check(vault, "not-an-identity", today=TODAY)
    ev.release_entry_check(vault, "b" * 64, today=TODAY)
    assert set(_entry_checks(vault)) == {entry.identity}
