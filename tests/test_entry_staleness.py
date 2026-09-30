"""Tests for ``ciao.memory_audit``'s entry-level detector.

The whole-note half is in ``test_memory_audit.py``; these cover the sibling that
measures one Markdown list item, and the properties the four surfaces reading it
depend on: only old and undated entries are selected, the exemptions are
explicit and a date is not one, a stamp that cannot be believed is `unverified`
rather than fresh, and what the parse could not read is counted instead of
swallowed.
"""

from __future__ import annotations

import datetime
import textwrap
from pathlib import Path

from ciao import memory_audit as ma
from ciao import note_entries as ne

TODAY = datetime.date(2026, 9, 30)


def _body(text: str) -> str:
    """A dedented block, so the fixtures are indented for reading only."""
    return textwrap.dedent(text).strip("\n")


def _note(body: str, *, updated: str = "2024-01-02", note_type: str = "person") -> str:
    """A whole note around ``body``, with real (unindented) frontmatter.

    Built by composition rather than by dedenting a template: `dedent` finds its
    common prefix across *all* lines, so a body that has already been dedented
    pins the prefix at "" and the template's own indentation survives — a note
    whose `---` is indented eight spaces has no frontmatter at all, and every
    assertion below about frontmatter not counting as an assertion would be
    passing for the wrong reason.
    """
    return (
        f"---\ntype: {note_type}\nupdated: {updated}\n---\n# Alice\n\n"
        f"{_body(body)}\n"
    )


def _cover(
    body: str,
    *,
    updated: str = "2024-01-02",
    note_type: str = "person",
    today: datetime.date = TODAY,
) -> ma.NoteEntryCoverage:
    return _selected(
        body, updated=updated, note_type=note_type, today=today
    )[0]


def _selected(
    body: str,
    *,
    updated: str = "2024-01-02",
    note_type: str = "person",
    today: datetime.date = TODAY,
) -> tuple[ma.NoteEntryCoverage, tuple[ma.EntryVerdict, ...], ne.EntryDocument]:
    return ma.note_entry_coverage(
        _note(body, updated=updated, note_type=note_type),
        note_type=note_type,
        updated=updated,
        mtime=0.0,
        note_path="People/Alice.md",
        rendered="memory-vault/People/Alice.md",
        workspace="work",
        today=today,
    )


def _words(verdicts: tuple[ma.EntryVerdict, ...]) -> list[str]:
    """The selected entries' own text, marker stripped, for order assertions."""
    return [v.excerpt.lstrip("-*+ ").strip() for v in verdicts]


# --- selection: only old and undated ----------------------------------------


def test_only_old_and_undated_entries_are_selected() -> None:
    """The property the whole level exists for, in one note.

    A fresh stamp inside the horizon is not a finding whatever the note's own
    `updated:` says — otherwise re-stamping a note yesterday would re-list the
    bullet somebody checked this morning, and the pass would spend its five slots
    on facts that are already done.
    """
    coverage, selected, _doc = _selected(
        textwrap.dedent(
            """\
            - Lives in Porto [verified: 2026-09-28]
            - Teaches maths [verified: 2020-01-01]
            - Landlord is Mr Silva
            - Prefers tea
            """
        )
    )
    assert [v.reason_code for v in selected] == [
        ma.STALE_ENTRY_AGED,
        ma.STALE_ENTRY_NO_STAMP,
        ma.STALE_ENTRY_NO_STAMP,
    ]
    # The first one is the aged stamp; the two undated entries inherit the note's
    # own date, and the flag says so — "unverified for 400d" about a bullet nobody
    # ever stamped would be a lie dressed as a number.
    assert selected[0].own_date is True
    assert selected[0].age_days == (TODAY - datetime.date(2020, 1, 1)).days
    assert [v.own_date for v in selected[1:]] == [False, False]
    assert coverage.stale == 1
    assert coverage.unverified == 2
    assert coverage.checked == 4


def test_the_boundary_date_counts_and_one_day_short_does_not() -> None:
    """`>=` horizon, and the off-by-one that silently doubles a queue.

    An entry aged exactly the threshold is due: a note is due every 90 days, and
    on the 90th it is due. A detector that used `>` would keep a fact alive for
    one extra night for no reason a reader could see.
    """
    horizon = 90
    exact = f"- Landlord is Mr Silva [verified: {TODAY - datetime.timedelta(days=horizon)}]"
    short = f"- Landlord is Mr Silva [verified: {TODAY - datetime.timedelta(days=horizon - 1)}]"
    assert _cover(exact).stale == 1
    assert _cover(short).stale == 0


# --- stamps: unusable is unverified, never fresh ---------------------------


def test_a_missing_or_unusable_stamp_is_unverified_never_fresh() -> None:
    """Three spellings of one absence, and none of them is a recent check.

    The impossible day and the day that has not come are the two that matter: read
    as dates they would sort an entry at the young end, and a reader would take
    "verified next January" as better news than "verified in 2020".
    """
    coverage, selected, _doc = _selected(
        textwrap.dedent(
            """\
            - Lives in Porto [verified: 2025-02-30]
            - Teaches maths [verified: 2027-01-01]
            - Owns a cat [verified: 2020-01-01]
            - Likes tea
            """
        )
    )
    codes = [v.reason_code for v in selected]
    assert codes == [
        ma.STALE_ENTRY_BAD_STAMP,
        ma.STALE_ENTRY_BAD_STAMP,
        ma.STALE_ENTRY_AGED,
        ma.STALE_ENTRY_NO_STAMP,
    ]
    # Two bad stamps, one unstamped: three of the four are "nobody has recorded a
    # check", and none of them is counted as `stale` — a stamp that cannot be
    # believed is not an old check either.
    assert coverage.unverified == 3
    assert coverage.stale == 1
    assert coverage.checked == 4
    assert "impossible" in selected[0].reason
    assert "future" in selected[1].reason


def test_an_entry_with_no_usable_date_anywhere_is_still_selected() -> None:
    """Unverifiable is not stale — and it is not clean either.

    `find_stale_notes` skips a note with no usable date entirely, because calling
    it stale would be a guess. The entry level cannot: an entry that carries no
    stamp and whose note has no date has still never been verified, and the
    finding says so with `age_days: None` rather than inventing a number.
    """
    text = textwrap.dedent(
        """\
        ---
        type: person
        ---
        # Alice

        - Landlord is Mr Silva
        """
    )
    coverage, selected, _doc = ma.note_entry_coverage(
        text,
        note_type="person",
        updated="not-a-date",
        mtime=0.0,
        note_path="People/Alice.md",
        rendered="memory-vault/People/Alice.md",
        today=TODAY,
    )
    assert [v.reason_code for v in selected] == [ma.STALE_ENTRY_NO_STAMP]
    assert selected[0].age_days is None
    assert coverage.age_days is None
    assert coverage.fully_verified is False


# --- exemptions: explicit, and never a bare date ---------------------------


def test_an_explicit_event_section_exempts_every_entry_under_it() -> None:
    """A heading saying so is worth more than any guess from a line's shape.

    The state assertion *above* the heading is still selected — the exemption is
    the section, not the rest of the file, and a detector that exempted whatever
    followed the first `## Events` would quietly stop checking a note the moment
    somebody added a log to it.
    """
    coverage, selected, _doc = _selected(
        textwrap.dedent(
            """\
            - Landlord is Mr Silva

            ## Events

            - 2019-04-02: signed the lease
            - 2019-04-02: met the landlord
            """
        )
    )
    assert _words(selected) == ["Landlord is Mr Silva"]
    assert coverage.exempt == 2
    assert coverage.unverified == 1


def test_a_dated_record_is_exempt_and_a_bare_date_is_not() -> None:
    """The precision case: past tense alone is not an exemption.

    "The office moved to 12 Baker Street" is a claim about where the office is
    *now* — people write about changes in the past tense — and a lease that ends
    on the 30th is a current-state assertion that happens to carry a date. A
    detector that exempted either would drop exactly the facts a person most
    needs to re-read.
    """
    _coverage, selected, _doc = _selected(
        textwrap.dedent(
            """\
            - 2024-03-14: signed the lease with Mr Silva
            - 2024-03-15 — launched the new pricing page
            - Office moved to 12 Baker Street in March
            - Contract ends 2026-03-01
            - 2026-10-01: quarterly review with Sam
            """
        )
    )
    assert _words(selected) == [
        "Office moved to 12 Baker Street in March",
        "Contract ends 2026-03-01",
        "2026-10-01: quarterly review with Sam",
    ]


def test_an_exempt_note_type_contributes_no_findings() -> None:
    """A journal is as true the day it was written, and the set is not a copy.

    `note_exempt` comes from the same `note_verification` verdict the whole-note
    detector uses, so a journal's entries cannot start being flagged because this
    level re-derived the exempt set.
    """
    coverage, selected, _doc = _selected(
        "- Met the landlord [verified: 2020-01-01]", note_type="journal"
    )
    assert selected == ()
    assert coverage.stale == 0
    assert coverage.exempt == 1
    assert coverage.note_exempt is True


# --- coverage: what the parse could not read -------------------------------


def test_prose_and_a_table_are_uncovered_and_headings_are_not() -> None:
    """The difference between "nothing wrong here" and "nothing measured here".

    A note whose facts live in a paragraph cannot show a fresh badge, and a note
    that is merely frontmatter, a heading and a bullet must not be reported as
    permanently incomplete — otherwise the count is noise and gets ignored.
    """
    prose = ma.note_entry_coverage(
        textwrap.dedent(
            """\
            ---
            type: person
            updated: 2026-09-25
            ---
            # Bob

            The office is now on Rua da Prata, second floor.

            | Field | Value |
            | --- | --- |
            | Phone | 912 345 678 |
            """
        ),
        note_type="person",
        updated="2026-09-25",
        mtime=0.0,
        note_path="People/Bob.md",
        rendered="memory-vault/People/Bob.md",
        today=TODAY,
    )[0]
    assert prose.entries == 0
    # The paragraph and the table are two places the detector did not look.
    assert prose.uncovered == 2
    assert prose.uncovered_chars > 0
    assert prose.fully_verified is False

    clean = _cover(
        textwrap.dedent(
            """\
            - Lives in Porto [verified: 2026-09-28]
            - Likes tea [verified: 2026-09-20]
            """
        ),
        updated="2026-09-29",
    )
    assert clean.uncovered == 0
    assert clean.fully_verified is True


def test_an_entry_carrying_a_construct_is_uncovered_not_judged() -> None:
    """A bullet holding a code block is a shape the entry model does not describe.

    Hashing it and reporting an age would be a verdict about text the parser
    cannot read as one atomic claim. It is counted as uncovered instead, so it is
    never counted as verified either.
    """
    coverage = _cover(
        textwrap.dedent(
            """\
            - Lives in Porto [verified: 2026-09-28]

              ```sh
              - this is a command, not a fact
              ```

            - Likes tea
            """
        ),
        updated="2026-09-29",
    )
    assert coverage.uncovered >= 1
    assert coverage.fully_verified is False


def test_one_fresh_bullet_never_clears_a_stale_siblings_badge() -> None:
    """The mixed note, and the property every surface leans on.

    `fully_verified` is deliberately stricter than "no stale entry": the reader
    of a badge, a map node or a review row has to be able to trust that the note
    is clean, and a note with one fresh bullet beside a two-year-old one is not.

    The sibling is stale on its **own** stamp rather than by inheriting the
    note's, which is the shape the property is really about: a note re-stamped
    yesterday with a bullet last checked in 2019 inside it. An *unstamped*
    sibling in a fresh note is not stale — see
    `test_an_unstamped_bullet_in_a_fresh_note_is_current`.
    """
    mixed = _cover(
        textwrap.dedent(
            """\
            - Lives in Porto [verified: 2026-09-28]
            - Landlord is Mr Silva [verified: 2019-05-01]
            """
        ),
        updated="2026-09-29",
    )
    assert mixed.stale == 1
    assert mixed.unverified == 0
    assert mixed.fully_verified is False
    assert mixed.coverage_ratio < 1.0


def test_an_unstamped_bullet_in_a_fresh_note_is_current() -> None:
    """Inheriting the note's date is what makes an unstamped bullet current.

    Nearly every bullet in a vault written before `[verified:]` stamps existed
    has no stamp, so selecting them unconditionally made every note read as
    "never checked" and filled a nightly plan with one-day-old entries in any
    vault that had been re-stamped. The note *is* the claim at that width: a note
    re-stamped yesterday has been re-read, and its unstamped bullets are as
    current as the file is.
    """
    coverage, selected, _doc = _selected(
        textwrap.dedent(
            """\
            - Lives in Porto [verified: 2026-09-28]
            - Landlord is Mr Silva
            """
        ),
        updated="2026-09-29",
    )
    assert selected == ()
    assert coverage.checked == 2
    # Counted as judged and current, never as `unverified`: the count a reader
    # sees is "how much of this note has been looked at", and this note has.
    assert coverage.unverified == 0
    assert coverage.stale == 0
    assert coverage.fully_verified is True


def test_an_unstamped_bullet_becomes_work_once_the_note_ages() -> None:
    """The other half of the same rule, and why it is a horizon and not a
    blanket exemption.

    The same bullet in the same note is current for as long as the note is, and
    overdue the day after it is not — which is the note-level verdict read one
    bullet in, and the reason the plan still finds unstamped facts eventually.
    """
    fresh, none_fresh, _doc = _selected(
        "- Landlord is Mr Silva", updated="2026-09-29"
    )
    assert none_fresh == ()
    old, selected, _doc = _selected("- Landlord is Mr Silva", updated="2024-01-02")
    assert [v.reason_code for v in selected] == [ma.STALE_ENTRY_NO_STAMP]
    assert old.unverified == 1
    assert old.fully_verified is False


# --- horizons: aliases and custom categories -------------------------------


def test_a_custom_category_horizon_reaches_the_entry_level(tmp_path: Path) -> None:
    """A category's own `stale_after_days`, and the alias resolution with it.

    The detector resolves the type through the same registry `note_verification`
    does, so `type: Person` and `type: hackathon-log` behave here exactly as they
    do at note level — the one thing a per-entry re-derivation would get wrong.
    """
    from ciao.entity_types import EntityType, EntityTypeRegistry

    registry = EntityTypeRegistry(
        [
            EntityType(id="person", label="Person", folder="People", stale_after_days=90),
            EntityType(id="hackathon", label="Hackathon", folder="Hackathons"),
            EntityType(
                id="hackathon-log",
                label="Hackathon log",
                folder="Hackathons",
                aliases=["hackathon"],
            ),
            EntityType(
                id="project", label="Project", folder="Projects", stale_after_days=3650
            ),
            EntityType(id="journal", label="Journal", folder="Logs"),
        ]
    )
    _coverage, selected, _doc = _selected(
        "- Speaks Italian [verified: 2026-09-28]", note_type="Person", today=TODAY
    )
    # The capitalised spelling resolves to `person`, so the horizon is 90 days and
    # not the 180-day default a spelling nobody claims would age on.
    assert _coverage.threshold_days == 90
    assert selected == ()
    aged = ma.note_entry_coverage(
        _note("- Speaks Italian [verified: 2020-01-01]", note_type="Person"),
        note_type="Person",
        updated="2024-01-02",
        mtime=0.0,
        note_path="People/A.md",
        rendered="memory-vault/People/A.md",
        today=TODAY,
        registry=registry,
    )[1]
    assert [v.reason_code for v in aged] == [ma.STALE_ENTRY_AGED]
    # An exempt type exempts at note level, so it exempts here too — the set is
    # `memory_audit`'s own, not a second copy of it at this level.
    assert (
        ma.note_entry_coverage(
            _note("- Met the CTO [verified: 2020-01-01]", note_type="journal"),
            note_type="journal",
            updated="2024-01-02",
            mtime=0.0,
            note_path="Logs/l.md",
            rendered="memory-vault/Logs/l.md",
            today=TODAY,
            registry=registry,
        )[1]
        == ()
    )
    # A project's own 3650-day horizon applies to its entries.
    project = ma.note_entry_coverage(
        _note("- Ships in March [verified: 2020-01-01]", note_type="project"),
        note_type="project",
        updated="2024-01-02",
        mtime=0.0,
        note_path="Projects/p.md",
        rendered="memory-vault/Projects/p.md",
        today=TODAY,
        registry=registry,
    )
    assert project[0].threshold_days == 3650
    assert project[1] == ()


# --- the batch form --------------------------------------------------------


class _Entry:
    """The four fields `find_stale_entries` reads off a `vault_index.Entry`."""

    def __init__(self, path: str, title: str, type_: str, updated: str) -> None:
        self.path = Path(path)
        self.title = title
        self.type = type_
        self.updated = updated


def test_find_stale_entries_reports_counts_coverage_and_diagnostics(
    tmp_path: Path,
) -> None:
    """An empty finding list is only clean beside the counts that explain it.

    A note the detector could not read, a note whose body is a table and a note
    with one stale bullet all produce different reports, and a caller that only
    looked at `stale_entries` would call all three the same.
    """
    people = tmp_path / "People"
    people.mkdir(parents=True)
    (people / "Alice.md").write_text(
        _note(
            textwrap.dedent(
                """\
                - Lives in Porto [verified: 2026-09-28]
                - Landlord is Mr Silva [verified: 2019-05-01]
                """
            )
        ),
        encoding="utf-8",
    )
    (people / "Bob.md").write_text(
        textwrap.dedent(
            """\
            ---
            type: person
            updated: 2026-09-25
            ---
            # Bob

            | Field | Value |
            | --- | --- |
            | Phone | 912 |
            """
        ),
        encoding="utf-8",
    )
    entries = [
        _Entry("memory-vault/People/Alice.md", "Alice", "person", "2026-09-29"),
        _Entry("memory-vault/People/Bob.md", "Bob", "person", "2026-09-25"),
        _Entry("memory-vault/People/Gone.md", "Gone", "person", "2026-09-25"),
    ]
    result = ma.find_stale_entries(entries, vault_root=tmp_path, workspace="work", today=TODAY)

    assert [row["path"] for row in result["stale_entries"]] == [
        "memory-vault/People/Alice.md"
    ]
    # Both of Alice's bullets were judged and the aged one is the one selected.
    # The four counts are what make "no findings" honest: an empty list beside
    # `uncovered: 0` is a clean note, and beside `uncovered: 1` it is a note
    # nobody read.
    assert result["entries_checked"] == 2
    assert result["entries_unverified"] == 0
    assert result["entries_uncovered"] == 1  # Bob's table
    assert result["notes_unreadable"] == ["memory-vault/People/Gone.md"]
    coverage = {row["relative_path"]: row for row in result["entry_coverage"]}
    assert coverage["People/Alice.md"]["fully_verified"] is False
    assert coverage["People/Bob.md"]["fully_verified"] is False
    assert coverage["People/Alice.md"]["last_verified"] == "2026-09-29"
    # The finding carries everything a caller cannot rederive.
    finding = result["stale_entries"][0]
    assert finding["identity"] and finding["fingerprint"] and finding["revision"]
    assert finding["reason"] == ma.STALE_ENTRY_AGED
    assert "Landlord" in finding["excerpt"]
    assert finding["context"]  # the neighbouring line is named
    assert finding["own_date"] is True


def test_find_stale_entries_is_a_pure_seam_over_caller_supplied_texts() -> None:
    """No vault, no scan: the same detector over bodies the caller already has.

    The entry pass reads every note anyway, so it hands the bodies over rather
    than paying a second decode — and a caller with no vault at all can still use
    the detector rather than re-deriving its rule.
    """
    body = _note("- Landlord is Mr Silva", updated="2020-01-02")
    entries = [_Entry("memory-vault/People/Alice.md", "Alice", "person", "2020-01-02")]
    result = ma.find_stale_entries(
        entries,
        workspace="work",
        today=TODAY,
        texts={"memory-vault/People/Alice.md": body},
    )
    assert len(result["stale_entries"]) == 1
    assert result["entries_unverified"] == 1


# --- the entry finding is the same claim everywhere ------------------------


def test_a_curation_plan_offers_exactly_the_detector_s_findings(tmp_path: Path) -> None:
    """Selection and detection agree, by construction rather than by care.

    This is the acceptance criterion the whole refactor exists for: before it,
    `curation_run._due_entries` aged entries its own way and nothing compared the
    two. A test that walks the real worklist is what keeps them one function.
    """
    from ciao import curation_run as cr
    from ciao.entity_types import EntityType, EntityTypeRegistry
    from ciao.vault_index import scan_vault

    people = tmp_path / "People"
    people.mkdir(parents=True)
    (people / "Alice.md").write_text(
        _note(
            textwrap.dedent(
                """\
                - Lives in Porto [verified: 2026-09-28]
                - Landlord is Mr Silva
                - 2024-03-14: signed the lease with Mr Silva
                """
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / "Workspace").mkdir(exist_ok=True)
    registry = EntityTypeRegistry(
        [EntityType(id="person", label="Person", folder="People", stale_after_days=90)]
    )
    scanned, note = cr._stale_findings(
        vault_root=tmp_path,
        registry=registry,
        path_prefix=None,
        today=TODAY,
    )
    assert note == ""
    items, _why = cr._stale_entry_items(
        vault_root=tmp_path, workspace="work", scanned=scanned, today=TODAY
    )
    planned = {item.keys[0] for item in items}

    entries = scan_vault(tmp_path, registry=registry)
    detected = ma.find_stale_entries(
        entries, vault_root=tmp_path, workspace="work", today=TODAY, registry=registry
    )
    expected = {
        cr.item_key(cr.PASS_STALE_ENTRY, row["identity"])
        for row in detected["stale_entries"]
    }
    assert planned == expected
    assert len(planned) == 1  # the fresh bullet and the signed lease are not work
    # And the reason says what kind of staleness it is, rather than a number
    # about a date the fact never had.
    assert "nobody has recorded a [verified:] check" in items[0].reason
    assert "entry identity" in items[0].reason
