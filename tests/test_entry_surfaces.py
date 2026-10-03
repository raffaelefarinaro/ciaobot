"""Tests for the entry-level surfaces: review, history, map and the writers.

The detector itself is covered in ``test_entry_staleness.py``; what is pinned
here is that each surface *reads it* rather than re-deriving it, that a mixed
note looks mixed everywhere, that an entry-level candidate links its pending
`note_edit` proposal rather than duplicating the decision, and that an ordinary
save cannot leave a `[verified:]` stamp on text it changed.
"""

from __future__ import annotations

import datetime
import json
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

TODAY = datetime.date(2026, 9, 30)


def _person_note(*, related: str = "", updated: str = "2026-09-29") -> str:
    """A note whose second fact is from 2019, whatever its own date says.

    The shape the whole level exists for: re-stamping the file last week cleared
    the whole-note flag and silently re-certified everything in it. ``related``
    adds a resolved frontmatter ref, which is how a fixture gets rid of the
    `unlinked` signal so a test can look at one finding at a time. ``updated``
    moves the note's *own* date, which is the other axis: a test that wants the
    row queued on the whole-note signal rather than the entry one sets an old
    date, and a test that wants the row queued *only* because of an overdue fact
    leaves it current.
    """
    link = f"related: [{related}]\n" if related else ""
    # Composed rather than interpolated into a dedented template: `dedent`
    # measures its common prefix across every line, and one interpolated line
    # with no indentation pins that prefix at "" — so the whole note would keep
    # the template's own eight spaces, have no frontmatter at all, and make every
    # assertion below pass for the wrong reason.
    return (
        "---\n"
        "type: person\n"
        f"updated: {updated}\n"
        "tags: [person]\n"
        "aliases: [Ali]\n"
        f"{link}"
        "---\n"
        "# Alice\n"
        "\n"
        "- Lives in Porto [verified: 2026-09-28]\n"
        "- Landlord is Mr Silva [verified: 2019-05-01]\n"
    )


def _entry(text: str, relative: str, prefix: str):
    """The parsed entry whose text starts with ``prefix``, or a loud failure.

    Parsed with the same coordinates every other reader in these tests uses, so
    the identity it mints is the one the vault's check state is keyed by — an
    identity minted with a different `note_path` is a different key that resolves
    to nothing, which is exactly the silent failure the workspace coordinate is
    there to prevent.
    """
    from ciao import note_entries as ne

    for entry in ne.parse_note_entries(
        text, note_path=relative, workspace="personal"
    ).entries:
        if entry.text.startswith(prefix):
            return entry
    raise AssertionError(f"no entry starting {prefix!r}")


# --- vault review ----------------------------------------------------------


def _review_vault(
    tmp_path: Path, *, related: str = "People/Bob.md", updated: str = "2026-09-29"
) -> Path:
    people = tmp_path / "People"
    people.mkdir(parents=True)
    (people / "Bob.md").write_text(
        textwrap.dedent(
            """\
            ---
            type: person
            updated: 2026-09-29
            tags: [person]
            ---
            # Bob

            - Works at Radix
            """
        ),
        encoding="utf-8",
    )
    (people / "Alice.md").write_text(
        _person_note(related=related, updated=updated), encoding="utf-8"
    )
    (tmp_path / "Workspace").mkdir(exist_ok=True)
    return tmp_path


def test_a_mixed_note_is_queued_and_names_the_exact_fact(tmp_path: Path) -> None:
    """The review row is the surface a person actually reads, so it names the line.

    The note is current by the whole-note rule, which is why this has to come
    from the entry level at all: a queue that could only ask "is this file old?"
    would show a person note re-stamped yesterday as clean while it held an
    employer's name from 2019.
    """
    from ciao import vault_review as review

    vault = _review_vault(tmp_path)
    pinned = datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.UTC)
    rows = review.generate_candidates(
        vault, workspace="personal", write_queue=False, now=pinned
    )
    alice = [row for row in rows if row.path.endswith("People/Alice.md")]
    assert alice, "a note holding a fact from 2019 must be queued"
    row = alice[0]
    assert "unverified_entries" in row.signals
    # The whole-note signal did NOT fire — the file is current — which is the
    # only reason the new one has anything to add.
    assert "unverified" not in row.signals

    block = row.evidence["entry_verification"]
    assert block["entries"] == 2
    assert block["checked"] == 2
    assert block["stale"] == 1
    assert block["fully_verified"] is False
    finding = block["stale_entries"][0]
    assert "Landlord is Mr Silva" in finding["excerpt"]
    assert finding["reason"] == "aged"
    assert finding["own_date"] is True
    assert finding["last_verified"] == "2019-05-01"
    # The neighbouring line is named, because a reader deciding whether a bullet
    # is current needs the context around it and not just the sentence — but the
    # sibling fact above it is not, because a fact reprinted as another fact's
    # context reads as the same claim made twice.
    assert any(line.startswith("# ") for line in finding["context"])
    assert not any("Lives in Porto" in line for line in finding["context"])


def test_a_fresh_fact_never_clears_its_stale_siblings_badge(tmp_path: Path) -> None:
    """`fully_verified` is the claim a badge rests on, and it is strict.

    One entry checked this morning beside one from 2019 is not a verified note.
    Without this the review row, the map node and the audit would all be able to
    call the same note clean on the strength of the neighbour nobody re-read.
    """
    from ciao import vault_review as review

    vault = _review_vault(tmp_path)
    rows = review.generate_candidates(
        vault,
        workspace="personal",
        write_queue=False,
        now=datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.UTC),
    )
    alice = [row for row in rows if row.path.endswith("People/Alice.md")][0]
    block = alice.evidence["entry_verification"]
    assert block["stale"] == 1
    assert block["unverified"] == 0
    assert block["fully_verified"] is False
    # The panel reads the engine's own `retirement_offered`, so a note whose only
    # finding is an overdue fact does not get a Retire button it did not earn —
    # "go and look again" is not a claim that a note is disposable.
    assert alice.as_dict()["retirement_offered"] is False


def test_an_overdue_fact_alone_is_never_a_disposal_signal() -> None:
    """The check-only set, and the one-line test it replaced.

    `signal != "unverified"` was the rule before this, and it is exactly the bug
    a second "check this" signal walks into: the new signal is compared against
    one hard-coded name, fails the comparison, and the queue starts offering
    Retire on a note whose only finding is that nobody read one of its bullets.
    """
    from ciao.vault_review import CHECK_ONLY_SIGNALS, _retirement_offered

    assert "unverified_entries" in CHECK_ONLY_SIGNALS
    assert _retirement_offered(("unverified_entries",)) is False
    assert _retirement_offered(("unverified",)) is False
    # A real disposability signal beside it still stands: the queue must not lose
    # its strongest finding because a verification reached the same note first.
    assert _retirement_offered(("unverified_entries", "unlinked")) is True


def test_a_prose_note_is_reported_unmeasured_not_wrong(tmp_path: Path) -> None:
    """Coverage is evidence here, and deliberately not a queueing signal.

    A note whose facts live in a paragraph is not a note with something wrong in
    it — it is a note this queue cannot measure. Making that a row would fill a
    queue whose terminal action is deletion with every short note in the vault.
    """
    from ciao import vault_review as review

    vault = tmp_path / "People"
    vault.mkdir(parents=True)
    (vault / "Bob.md").write_text(
        textwrap.dedent(
            """\
            ---
            type: person
            updated: 2026-09-25
            tags: [person]
            ---
            # Bob

            The office is on Rua da Prata, second floor, and the phone is
            912 345 678.
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "Workspace").mkdir(exist_ok=True)
    rows = review.generate_candidates(
        vault,
        workspace="personal",
        write_queue=False,
        now=datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.UTC),
    )
    assert [row.path for row in rows] == []


def test_coverage_is_reported_for_a_queued_note_whose_facts_are_prose(
    tmp_path: Path,
) -> None:
    """The block a reader needs, on a note that is queued for another reason.

    A note that is also unlinked is a real review candidate, and when it is shown
    the reader is told that its facts are in a paragraph rather than being shown
    a confident-looking empty list.
    """
    from ciao import vault_review as review
    from ciao import memory_audit as ma

    text = textwrap.dedent(
        """\
        ---
        type: person
        updated: 2026-09-25
        tags: [person]
        ---
        # Bob

        The office is on Rua da Prata, second floor.
        """
    )
    coverage, selected, document = ma.note_entry_coverage(
        text,
        note_type="person",
        updated="2026-09-25",
        mtime=0.0,
        note_path="People/Bob.md",
        rendered="memory-vault/People/Bob.md",
        today=TODAY,
    )
    block, due = review._entry_evidence(
        coverage, selected, document, relative="People/Bob.md", root=tmp_path
    )
    assert block["entries"] == 0
    assert block["uncovered"] == 1
    assert block["fully_verified"] is False
    assert block["stale_entries"] == []
    # Nothing due: the note is unmeasured, not overdue, so it is not a question
    # the queue puts to anybody.
    assert due == 0


def test_an_entry_candidate_links_its_pending_proposal_rather_than_duplicating(
    tmp_path: Path,
) -> None:
    """One finding, one decision — and the link says which one.

    A `needs_review` entry verdict files a `note_edit` proposal whose accept
    rewrites or removes exactly that bullet. The review row's own buttons are
    about the whole file, so offering a second accept of the same question in two
    places is how a person answers it twice.

    The note is written with an **old `updated:`** so the row is queued on the
    whole-note signal whatever the entry findings say: the entry has a pending
    proposal, so its entry finding is settled and must not queue the row (see
    `test_a_settled_entry_is_reported_but_not_re_asked_about`). What is under test
    is the link, so the row has to be there for some other reason.
    """
    from ciao import entry_verification as ev
    from ciao import vault_review as review

    vault = _review_vault(tmp_path, updated="2024-01-02")
    text = _person_note(updated="2024-01-02")
    entry = _entry(text, "People/Alice.md", "- Landlord")
    identity, fingerprint = entry.identity, entry.fingerprint
    ev.record_entry_check(
        vault,
        ev.EntryCheck(
            identity=identity,
            note_path="People/Alice.md",
            workspace="personal",
            content_fingerprint=fingerprint,
            outcome=ev.STILL_VALID,
            checked_at=TODAY,
            retry_after=TODAY + datetime.timedelta(days=30),
            coverage=ev.COVERAGE_PARTIAL,
            reason="no stamp to re-stamp",
            proposal_id="pr1",
        ),
    )
    rows = review.generate_candidates(
        vault,
        workspace="personal",
        write_queue=False,
        now=datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.UTC),
    )
    alice = [row for row in rows if row.path.endswith("People/Alice.md")][0]
    block = alice.evidence["entry_verification"]
    proposals = block["proposals"]
    assert [p["identity"] for p in proposals] == [identity]
    assert proposals[0]["proposal_id"] == "pr1"
    # A conflict is reported rather than hidden: an accept that can only fail is
    # worse than one that says what went wrong.
    assert proposals[0]["conflicted"] is False
    # The finding is still *visible* — the fact is still old — and the check is
    # on it, so the row can say why the note is not being asked about again.
    assert [f["settled"] for f in block["stale_entries"]] == [True]
    assert block["stale_entries"][0]["checked_outcome"] == ev.STILL_VALID
    assert block["due"] == 0
    # And the signal did not fire: the worklist filters its plan by the very same
    # predicate, and a queue that offered this question would be asking about it
    # twice.
    assert "unverified_entries" not in alice.signals
    assert ev.should_check_entry(vault, identity, fingerprint, today=TODAY) is False


def test_a_settled_entry_is_reported_but_not_re_asked_about(
    tmp_path: Path,
) -> None:
    """The disagreement this fixes, in one note.

    The nightly `stale_entry` pass drops an entry whose `EntryCheck` settles it
    — inside its 30-day cooldown, or waiting on a proposal. A queue that raised
    `unverified_entries` for the same entry anyway would promise a person a
    question the pass has already put to somebody else, and the two lists would
    disagree about the same fact in the same vault on the same night.

    Nothing is hidden: the entry is still old, so it stays in the block with
    ``settled`` and the verdict beside it. What changes is the signal.

    The note is written with an old `updated:`, so the row survives the entry
    finding being settled: it is queued on the whole-note signal instead, which
    is what lets the test read the entry block on a row that exists.
    """
    from ciao import entry_verification as ev
    from ciao import vault_review as review

    vault = _review_vault(tmp_path, updated="2024-01-02")
    rows = review.generate_candidates(
        vault,
        workspace="personal",
        write_queue=False,
        now=datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.UTC),
    )
    alice = [row for row in rows if row.path.endswith("People/Alice.md")][0]
    assert "unverified_entries" in alice.signals
    assert alice.evidence["entry_verification"]["due"] == 1
    assert [
        f["settled"] for f in alice.evidence["entry_verification"]["stale_entries"]
    ] == [False]

    # Now somebody answers it, the way the pass would.
    entry = _entry(_person_note(updated="2024-01-02"), "People/Alice.md", "- Landlord")
    ev.record_entry_check(
        vault,
        ev.EntryCheck(
            identity=entry.identity,
            note_path="People/Alice.md",
            workspace="personal",
            content_fingerprint=entry.fingerprint,
            outcome=ev.STILL_VALID,
            checked_at=TODAY,
            retry_after=TODAY + datetime.timedelta(days=30),
            coverage=ev.COVERAGE_COMPLETE,
            reason="the March release notes still name her",
        ),
    )
    rows = review.generate_candidates(
        vault,
        workspace="personal",
        write_queue=False,
        now=datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.UTC),
    )
    alice = [row for row in rows if row.path.endswith("People/Alice.md")][0]
    block = alice.evidence["entry_verification"]
    assert block["due"] == 0
    assert "unverified_entries" not in alice.signals
    # Visible, with the verdict, so the row explains the silence.
    assert block["stale_entries"][0]["settled"] is True
    assert block["stale_entries"][0]["checked_at"] == TODAY.isoformat()
    assert ev.should_check_entry(
        vault, entry.identity, entry.fingerprint, today=TODAY
    ) is False


def test_an_entry_proposal_the_note_has_left_is_reported_as_a_conflict(
    tmp_path: Path,
) -> None:
    """The note moved under the proposal; the row says so instead of offering it.

    The accept resolves the entry again by identity and refuses on any mismatch,
    so a link to a proposal pinned to text the note no longer holds is a button
    that can only fail. A row that hid that would be sending somebody to click it.
    """
    from ciao import entry_verification as ev
    from ciao import vault_review as review

    vault = _review_vault(tmp_path)
    text = _person_note()
    identity = _entry(text, "People/Alice.md", "- Landlord").identity
    ev.record_entry_check(
        vault,
        ev.EntryCheck(
            identity=identity,
            note_path="People/Alice.md",
            workspace="personal",
            content_fingerprint="0" * 64,
            outcome=ev.STILL_VALID,
            checked_at=TODAY,
            retry_after=TODAY + datetime.timedelta(days=30),
            coverage=ev.COVERAGE_PARTIAL,
            reason="stale",
            proposal_id="pr1",
        ),
    )
    rows = review.generate_candidates(
        vault,
        workspace="personal",
        write_queue=False,
        now=datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.UTC),
    )
    alice = [row for row in rows if row.path.endswith("People/Alice.md")][0]
    proposals = alice.evidence["entry_verification"]["proposals"]
    assert proposals[0]["conflicted"] is True


# --- history ---------------------------------------------------------------


def _history_detail(proposal: object) -> dict:
    rows: list[dict] = [
        {
            "kind": "note_edit",
            "action": "accepted",
            "proposal_id": proposal.proposal_id,
            "destination": proposal.relative_path,
        }
    ]
    with pytest.MonkeyPatch.context() as patch:
        import ciao.web.routes_api as routes

        patch.setattr(
            routes,
            "list_sidecars",
            lambda config, workspace: [proposal],
            raising=False,
        )
        import ciao.note_edit_proposals as nep

        patch.setattr(nep, "list_sidecars", lambda config, workspace: [proposal])
        routes._attach_note_edit_detail(SimpleNamespace(), "personal", rows)
    return rows[0]["note_edit"]


def test_history_shows_an_entry_decision_at_the_entrys_scale(
    tmp_path: Path,
) -> None:
    """Two whole-note images differing by one line are unreadable.

    A `retire_entry` is the sharpest case: the whole-note before/after differ by
    a line that is simply gone, and nothing in the pair says which. The entry's
    own before/after does, and `scope` is what tells the client to show it.
    """
    from ciao.note_edit_proposals import NoteEditProposal

    before = _person_note()
    after = textwrap.dedent(
        """\
        ---
        type: person
        updated: 2026-09-29
        tags: [person]
        aliases: [Ali]
        ---
        # Alice

        - Lives in Porto [verified: 2026-09-28]
        """
    )
    entry_before = "- Landlord is Mr Silva [verified: 2019-05-01]\n"
    start = before.index(entry_before)
    detail = _history_detail(
        NoteEditProposal(
            id="nep-1",
            workspace="personal",
            relative_path="People/Alice.md",
            operation="retire_entry",
            expected_revision="rev",
            before=before,
            after=after,
            outcome="retire",
            coverage="complete",
            evidence=(),
            reason="the employer's name is not this person any more",
            created_at="2026-09-30T00:00:00Z",
            proposal_id="pr1",
            entry_identity="a" * 64,
            entry_fingerprint="b" * 64,
            entry_span=(start, start + len(entry_before)),
        )
    )
    assert detail["scope"] == "entry"
    assert detail["entry_before"] == entry_before
    assert detail["entry_removed"] is True
    assert detail["entry_after"] == ""
    assert detail["entry_recovery_error"] == ""
    # The whole-note pair is still there, because the undo restores the file from
    # it; a removal that cannot be put back would be a different claim.
    assert detail["before"] == before
    assert detail["after"] == after


def test_history_reports_an_unrecoverable_entry_diff_rather_than_guessing(
    tmp_path: Path,
) -> None:
    """A record whose splice does not invert keeps the note pair and says why.

    "Removed" and "we could not work out what it would have been" are very
    different things to show where a diff would be, so `entry_removed` and
    `entry_recovery_error` are separate fields rather than one empty string.
    """
    from ciao.note_edit_proposals import NoteEditProposal

    before = _person_note()
    detail = _history_detail(
        NoteEditProposal(
            id="nep-1",
            workspace="personal",
            relative_path="People/Alice.md",
            operation="retire_entry",
            expected_revision="rev",
            before=before,
            after="a note that is not the image this edit produced",
            outcome="retire",
            coverage="complete",
            evidence=(),
            reason="",
            created_at="2026-09-30T00:00:00Z",
            proposal_id="pr1",
            entry_identity="a" * 64,
            entry_fingerprint="b" * 64,
            entry_span=(10, 20),
        )
    )
    assert detail["entry_removed"] is False
    assert detail["entry_recovery_error"]


# --- the memory map --------------------------------------------------------


def test_the_map_node_reports_coverage_and_never_raises_the_stale_flag(
    tmp_path: Path,
) -> None:
    """The map cannot stop at the whole-note flag, and does not start at it either.

    `stale` is the file-level verdict and the nightly worklist is where an
    overdue bullet becomes a plan, so the map reports coverage *beside* the flag
    rather than raising it — otherwise the map and the plan would be answering
    two different questions about the same note.
    """
    from ciao.memory_audit import note_entry_coverage

    text = _person_note()
    coverage, selected, _doc = note_entry_coverage(
        text,
        note_type="person",
        updated="2026-09-29",
        mtime=0.0,
        note_path="People/Alice.md",
        rendered="memory-vault/People/Alice.md",
        today=TODAY,
    )
    # The file is one day old against a 90-day horizon: not stale *as a file*.
    assert coverage.age_days == 1
    # And the fact beside it is seven years old: selected, and not clean.
    assert [v.reason_code for v in selected] == ["aged"]
    assert coverage.fully_verified is False
    assert coverage.stale == 1
    assert coverage.coverage_ratio > 0.5


# --- the writers -----------------------------------------------------------


def test_an_ordinary_save_cannot_carry_a_stamp_it_did_not_earn() -> None:
    """The rule the accept path enforces, and the prompt only asks for.

    A fold rewrites the whole file, and a rewrite is exactly how a
    `[verified: today]` ends up on the sentence most likely to have changed. The
    test pins all three outcomes: a changed bullet loses its stamp, an untouched
    neighbour keeps it, and a pure re-stamp is untouched (which is what makes the
    rule safe to run on every save).
    """
    from ciao import note_entries as ne

    before = textwrap.dedent(
        """\
        - Lives in Porto [verified: 2026-09-01]
        - Landlord is Mr Silva [verified: 2020-01-01]
        """
    )
    after = textwrap.dedent(
        """\
        - Lives in Lisbon now [verified: 2026-09-30]
        - Landlord is Mr Silva [verified: 2020-01-01]
        - Works at Radix [verified: 2026-09-30]
        """
    )
    cleaned, dropped = ne.invalidate_stale_stamps(before, after)
    assert dropped == 2
    assert cleaned == (
        "- Lives in Lisbon now\n"
        "- Landlord is Mr Silva [verified: 2020-01-01]\n"
        "- Works at Radix\n"
    )
    # The changed bullets now read as unverified to the detector, which is the
    # honest state for a fact nobody checked. The note is aged on purpose: an
    # unstamped bullet inherits the note's date, so in a fresh note it is current
    # exactly as far as the file is and there is nothing left to re-read.
    from ciao.memory_audit import note_entry_coverage

    _coverage, selected, _doc = note_entry_coverage(
        cleaned,
        note_type="person",
        updated="2024-01-02",
        mtime=0.0,
        note_path="People/A.md",
        rendered="memory-vault/People/A.md",
        today=TODAY,
    )
    assert {v.reason_code for v in selected} == {"no-stamp", "aged"}


def test_an_untouched_bullet_survives_an_unrelated_save(tmp_path: Path) -> None:
    """The case the rule must NOT touch, which is the whole risk of adding it.

    A save that rewrites one bullet in a file must leave every other bullet's
    stamp exactly as it was — including the case this rule is *not* about: a
    bullet whose own date moved because the `still_valid` verdict wrote it. That
    write is a verification and goes through
    `ciao.note_receipts.apply_entry_edit`, which never runs this function; an
    ordinary save that merely happens to leave the token alone must be a no-op.
    """
    from ciao import note_entries as ne

    before = (
        "- Lives in Porto [verified: 2026-09-20]\n"
        "- Landlord is Mr Silva [verified: 2020-01-01]\n"
    )
    after = (
        "- Lives in Lisbon [verified: 2026-09-20]\n"
        "- Landlord is Mr Silva [verified: 2020-01-01]\n"
    )
    cleaned, changed = ne.invalidate_stale_stamps(before, after)
    # Only the bullet whose words changed, and only because its claim is gone.
    assert changed == 1
    assert cleaned == (
        "- Lives in Lisbon\n- Landlord is Mr Silva [verified: 2020-01-01]\n"
    )


def test_a_redated_unchanged_bullet_is_put_back() -> None:
    """The half a fingerprint cannot see, and the one that mattered.

    The fingerprint ignores the stamp by design — that is what makes a re-stamp
    invisible to it — so "the words are unchanged" cannot also mean "the stamp is
    fine". A save that re-dates a bullet it did not touch is the exact failure
    the writers exist to stop: today's date, attached to a sentence somebody
    checked six years ago. The date ``before`` carried is restored, not merely
    removed, because removing it would also lose a real verification.
    """
    from ciao import note_entries as ne

    before = "- Lives in Berlin [verified: 2020-01-01]\n"
    after = "- Lives in Berlin [verified: 2026-09-30]\n"
    cleaned, changed = ne.invalidate_stale_stamps(before, after)
    assert changed == 1
    assert cleaned == before


def test_a_stamp_added_to_an_unchanged_bullet_is_cut() -> None:
    """The other shape of the same failure: none before, one after.

    Nothing was verified about those exact words on any day, and a save is not a
    check, so the token goes rather than being dated back to something `before`
    never held.
    """
    from ciao import note_entries as ne

    cleaned, changed = ne.invalidate_stale_stamps(
        "- Lives in Berlin\n", "- Lives in Berlin [verified: 2026-09-30]\n"
    )
    assert changed == 1
    assert cleaned == "- Lives in Berlin\n"


def test_a_neighbour_is_left_alone_while_a_redated_sibling_is_put_back() -> None:
    """The two rules together, on one file.

    A save that rewrote one bullet and re-dated another has to produce a file
    where the untouched bullet keeps its real date, the changed one has none, and
    the re-dated one is back to what it was — the whole point being that the
    writer is a filter, not a reset.
    """
    from ciao import note_entries as ne

    before = "- Speaks Greek [verified: 2020-01-01]\n- Works at Acme [verified: 2019-01-01]\n"
    after = "- Speaks Greek [verified: 2026-09-30]\n- Works at Helios [verified: 2026-09-30]\n"
    cleaned, changed = ne.invalidate_stale_stamps(before, after)
    assert changed == 2
    assert cleaned == (
        "- Speaks Greek [verified: 2020-01-01]\n- Works at Helios\n"
    )


def test_the_fold_writer_strips_stamps_before_it_writes(tmp_path: Path) -> None:
    """The accept path, not the prompt, is what makes the rule true.

    The system prompt asks the model to leave the stamps alone; a model asked to
    rewrite a note will sometimes not. So the writer drops the stamps a save did
    not earn, on the result, before it reaches the disk.
    """
    from ciao.project_doc_update import _invalidate_stamps

    current = "- Landlord is Mr Silva [verified: 2020-01-01]\n"
    updated = "- Landlord is Mr Costa [verified: 2020-01-01]\n"
    assert _invalidate_stamps(current, updated) == "- Landlord is Mr Costa\n"


def test_the_fold_writer_puts_a_redated_bullet_back(tmp_path: Path) -> None:
    """The `people` accept, on the re-date: a bullet nobody touched.

    A model rewriting a person note is the most likely source of this, because
    it re-emits the whole file and has today's date in whatever context it was
    given. The writer is shared by the two accept paths, so this asserts the
    shared helper here and the `project` path below pins that the call site is
    wired in both places.
    """
    from ciao.project_doc_update import _invalidate_stamps

    current = "- Landlord is Mr Silva [verified: 2020-01-01]\n"
    updated = "- Landlord is Mr Silva [verified: 2026-09-30]\n"
    assert _invalidate_stamps(current, updated) == current


@pytest.mark.parametrize(
    "writer, current, updated, expected",
    [
        # `update_project_doc`: a project doc folded from a session's insights.
        (
            "project",
            "- Ships monthly [verified: 2024-03-01]\n",
            "- Ships monthly [verified: 2026-09-30]\n",
            "- Ships monthly [verified: 2024-03-01]\n",
        ),
        (
            "project",
            "- Owner is Dana [verified: 2024-03-01]\n",
            "- Owner is Rae [verified: 2026-09-30]\n",
            "- Owner is Rae\n",
        ),
        # `fold_fact_into_person_note`: a fact merged into a person note.
        (
            "people",
            "- Prefers terse replies [verified: 2024-03-01]\n",
            "- Prefers terse replies [verified: 2026-09-30]\n",
            "- Prefers terse replies [verified: 2024-03-01]\n",
        ),
        (
            "people",
            "- Based in Porto [verified: 2024-03-01]\n",
            "- Based in Lisbon [verified: 2026-09-30]\n",
            "- Based in Lisbon\n",
        ),
    ],
)
def test_both_accept_paths_run_the_invalidator(
    writer: str, current: str, updated: str, expected: str
) -> None:
    """Both writers, both shapes, through the helper they each call.

    `update_project_doc` and `fold_fact_into_person_note` are separate call
    sites for one rule, and a rule enforced at one of them is a rule the other
    does not have. Asserting the *shared* helper once would not catch a call site
    that stopped calling it, which is the failure this pair exists to prevent.
    """
    import inspect

    from ciao import project_doc_update as pdu

    source = inspect.getsource(
        pdu.update_project_doc if writer == "project" else pdu.fold_fact_into_person_note
    )
    assert "_invalidate_stamps(" in source, f"the {writer} writer stopped calling it"
    assert pdu._invalidate_stamps(current, updated) == expected


def test_the_authoring_guidance_names_the_entry_contract() -> None:
    """The stock prompts, pinned on the words the detector depends on.

    A prompt is prose and prose rots, so the two things a writer must not get
    wrong — a bullet is the unit, and a stamp is a claim about actual
    verification — are asserted rather than left to be re-read.
    """
    from importlib import resources

    skill = (
        resources.files("ciao.stock")
        .joinpath("skills/ciao-memory/SKILL.md")
        .read_text(encoding="utf-8")
    )
    command = (
        resources.files("ciao.stock")
        .joinpath("commands/remember.md")
        .read_text(encoding="utf-8")
    )
    for text in (skill, command):
        assert "one fact per bullet" in text or "One assertion per `-` bullet" in text
        assert "after actually going and checking" in text or (
            "only after actual" in text
        ) or "you did not earn" in text
        assert "updated:" in text
    # The care prompt is JSON, so its guidance is asserted through the parsed
    # value rather than through a substring of the file.
    schedules = json.loads(
        resources.files("ciao.stock").joinpath("schedules.json").read_text("utf-8")
    )
    prompt = schedules["schedules"][0]["prompt"]
    assert "one fact per bullet" in prompt
    assert "you did not earn" in prompt
    assert "uncovered" in prompt
