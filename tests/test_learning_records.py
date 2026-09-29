"""Tests for ``ciao.learning_records``.

The module is pure, so every example here is a synthetic ``Learnings.md`` body
written inline. Nothing in this file touches a vault, and nothing is read from
one: the production writer, the curation worklist and the migration command are
a later child, and what is under test now is the record model and the parser
that has to survive whatever an installed vault already contains.

The order of the tests follows the contract the module docstring states —
identity, unknown-stays-unknown, evidence-not-prose, untouched-bytes — because
that is the order in which a failure explains itself.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date

import pytest

from ciao.learning_records import (
    FORMAT_CANONICAL,
    FORMAT_CONFLICT,
    FORMAT_LEGACY,
    FORMAT_MALFORMED,
    FORMAT_PLAIN,
    ID_NAMESPACE,
    MAX_DISPLAY_SOURCES,
    METADATA_MARKER,
    SECTION_ACTIVE,
    SECTION_PROMOTED,
    LearningEntry,
    LearningObservation,
    LearningRecord,
    migrate_learnings,
    observe_learning,
    parse_learnings,
    render_learning,
)

# A syntactically valid identifier, for records built by hand rather than
# parsed, and for the metadata payloads the malformed cases hand-write.
_UUID = "11111111-2222-3333-4444-555555555555"

# A well-formed current line, as the existing writer emits it.
CURRENT = "- [airtable-sort] [2024-01-02 → 2024-03-04] (x3) Airtable sort param returns 400 — sources: chat-a, chat-b"
# A historical date-only line with the category and confidence it carried.
LEGACY = "- [2024-05-01] debugging: retries need a fresh token — confidence: high"
# A bullet with no structure at all: no key, no dates, no count.
PLAIN = "- A plain bullet with no structure at all."


def document(body: str) -> str:
    """A minimal learnings file around *body*, with no frontmatter surprises."""
    return f"# Learnings\n\n## Active\n{body}\n"


def records(text: str, *, workspace: str = "work") -> list[LearningRecord]:
    """Every record the parser read, in document order, failing loudly if any did not."""
    parsed = parse_learnings(text, workspace=workspace)
    assert not parsed.diagnostics, parsed.diagnostics
    assert all(entry.record is not None for entry in parsed.entries)
    return [entry.record for entry in parsed.entries if entry.record is not None]


# ── The three shapes ───────────────────────────────────────────────────────


def test_parse_current_legacy_and_plain_entries() -> None:
    text = document(f"{CURRENT}\n{LEGACY}\n{PLAIN}\n")
    parsed = parse_learnings(text, workspace="work")
    assert [entry.format for entry in parsed.entries] == [
        FORMAT_CANONICAL,
        FORMAT_LEGACY,
        FORMAT_PLAIN,
    ]
    current, legacy, plain = [entry.record for entry in parsed.entries]

    assert (current.key, current.text) == (
        "airtable-sort",
        "Airtable sort param returns 400",
    )
    assert (current.first_seen, current.last_seen) == (
        date(2024, 1, 2),
        date(2024, 3, 4),
    )
    assert (current.count, current.baseline_count) == (3, 3)
    assert [o.source for o in current.observations] == ["chat-a", "chat-b"]

    # A date-only legacy entry knows when it was filed and nothing else. The
    # count stays unknown rather than becoming an invented x1, and the
    # category and confidence survive as explicit legacy metadata.
    assert (legacy.first_seen, legacy.last_seen) == (date(2024, 5, 1), date(2024, 5, 1))
    assert legacy.count is None and legacy.baseline_count is None
    assert legacy.text == "retries need a fresh token"
    assert legacy.legacy_fields == {"category": "debugging", "confidence": "high"}

    # A plain bullet has no dates either, and they render as the explicit
    # canonical tokens rather than as today.
    assert plain.first_seen is None and plain.last_seen is None
    assert plain.count is None
    assert plain.text == "A plain bullet with no structure at all."

    migrated, diagnostics = migrate_learnings(text, workspace="work")
    assert not diagnostics
    lines = [line for line in migrated.splitlines() if line.startswith("- [")]
    # A date-only legacy entry keeps the one date it had and admits it has no
    # count; a plain bullet admits both, rather than being stamped with today
    # and an invented x1.
    assert lines[1].startswith(
        "- [retries-need-a-fresh] [2024-05-01 → 2024-05-01] (?) retries need a fresh token "
    )
    assert lines[2].startswith(
        "- [a-plain-bullet-with] [unknown → unknown] (?) A plain bullet with no structure at all. "
    )
    assert "debugging" in lines[1] and "confidence" in lines[1]  # retained, not dropped
    # A current line is left exactly as it reads; only a comment is added.
    assert lines[0].startswith(CURRENT + " ")

    # A cited phrase is a citation the owner wrote, not a chat id. Promoting
    # English to an identifier would fabricate provenance that could then be
    # counted, so it is retained as legacy text and is not an observation.
    (mixed,) = records(
        document(
            "- [k] [2024-01-01 → 2024-01-02] (x1) t — sources: chat-a, the migration thread\n"
        )
    )
    assert [o.source for o in mixed.observations] == ["chat-a"]
    assert mixed.legacy_fields == {"sources": "the migration thread"}

    # The model is frozen, so a caller cannot mutate a record another caller
    # is reasoning about, and an entry keeps the whole line its offsets name.
    (entry,) = parse_learnings(document(f"{PLAIN}\n"), workspace="work").entries
    assert isinstance(entry, LearningEntry)
    assert entry.source_text == PLAIN
    for frozen in (current, legacy, plain, entry, mixed.observations[0]):
        with pytest.raises((AttributeError, TypeError)):
            frozen.anything = "mutated"  # type: ignore[attr-defined]


# ── Scope ──────────────────────────────────────────────────────────────────


def test_parse_scopes_active_resolved_and_fences() -> None:
    text = (
        "---\n"
        "tags: [ciao, learnings]\n"
        "- [frontmatter-decoy] [2024-01-01 → 2024-01-01] (x9) not a learning\n"
        "---\n"
        "# Learnings\n"
        "\n"
        "- [preamble-decoy] [2024-01-01 → 2024-01-01] (x9) above every section\n"
        "\n"
        "## Active\n"
        "\n"
        "```markdown\n"
        "- [fenced-decoy] [2024-01-01 → 2024-01-01] (x9) inside a code block\n"
        "```\n"
        "\n"
        f"{PLAIN}\n"
        "\n"
        "## Promoted / Resolved\n"
        f"{CURRENT}\n"
        "\n"
        "## Format\n"
        "- [example-decoy] [2024-01-01 → 2024-01-01] (x9) a worked example\n"
    )
    parsed = parse_learnings(text, workspace="work")

    assert not parsed.diagnostics
    assert [(entry.section, entry.format) for entry in parsed.entries] == [
        (SECTION_ACTIVE, FORMAT_PLAIN),
        (SECTION_PROMOTED, FORMAT_CANONICAL),
    ]
    assert [entry.record.text for entry in parsed.entries if entry.record] == [
        "A plain bullet with no structure at all.",
        "Airtable sort param returns 400",
    ]
    # Nothing outside those two sections became an entry, so none of the
    # decoys can be migrated into a real learning.
    for decoy in (
        "frontmatter-decoy",
        "fenced-decoy",
        "example-decoy",
        "preamble-decoy",
    ):
        assert decoy not in [
            entry.record.key for entry in parsed.entries if entry.record
        ]


# ── Migration ──────────────────────────────────────────────────────────────


def test_migration_preserves_nonactive_bytes() -> None:
    promoted = (
        "## Promoted / Resolved\n"
        f"{CURRENT}\n"
        "- a resolved plain bullet, left alone\n"
    )
    format_notes = "## Format\n\nEntries are `- [key] [first → last] (xN) text`.\n"
    body = f"{format_notes}\n## Active\n{PLAIN}\n\n{promoted}"
    crlf = "﻿" + body.replace("\n", "\r\n")
    before = "﻿---\r\ntags: [ciao, learnings]\r\n---\r\n# Learnings\r\n\r\n" + crlf

    migrated, diagnostics = migrate_learnings(before, workspace="work")

    assert not diagnostics
    # Byte-order mark, frontmatter, line endings and every unrecognized line.
    assert migrated.startswith("﻿---\r\ntags: [ciao, learnings]\r\n---\r\n")
    assert migrated.count("\r\n") == before.count("\r\n")
    assert "\n" not in migrated.replace("\r\n", "")
    # The format note and the resolved section survive verbatim: only the
    # recognized Active entry span was rewritten.
    assert format_notes.replace("\n", "\r\n") in migrated
    assert promoted.replace("\n", "\r\n") in migrated
    assert f"{PLAIN} " not in migrated
    assert (
        "[unknown → unknown] (?) A plain bullet with no structure at all." in migrated
    )

    # A file with no Active section at all is a no-op.
    resolved_only = "# Learnings\n\n## Promoted / Resolved\n" + f"{CURRENT}\n"
    assert migrate_learnings(resolved_only, workspace="work") == (resolved_only, [])


def test_trailing_whitespace_after_metadata_keeps_identity() -> None:
    clean, _ = migrate_learnings(document(f"{CURRENT}\n"), workspace="work")
    (before,) = records(clean)
    line = next(item for item in clean.splitlines() if item.startswith("- ["))

    # An editor or a hand edit that pads the closing marker has not broken the
    # comment. The stored identifier is still on the line and is still read from
    # there, and the evidence it carries stays evidence rather than being
    # folded back into the statement as prose.
    for padding in (" ", "\t", " \t "):
        edited = document(f"{line}{padding}\n")
        (kept,) = records(edited)
        assert kept.learning_id == before.learning_id
        assert kept.observations == before.observations
        assert kept.legacy == before.legacy

        # The padding does not survive into the next write, no second comment is
        # appended beside the old one, and the pass after that changes nothing.
        once, diagnostics = migrate_learnings(edited, workspace="work")
        assert not diagnostics
        assert once == clean
        assert once.count(METADATA_MARKER) == 1
        assert migrate_learnings(once, workspace="work")[0] == once


@pytest.mark.parametrize("cited", [False, True], ids=["bare", "cited"])
def test_statement_containing_a_sources_clause_round_trips(cited: bool) -> None:
    # A statement is free to use the words the writer uses for a citation; only
    # the clause that runs to the end of the line is one. Reading the first
    # match instead would shorten this sentence to "use foo — sources: x and"
    # and turn "y" into a source, changing what the learning says with no
    # diagnostic to say so.
    statement = "use foo — sources: x and -- sources: y"
    record = LearningRecord(learning_id=_UUID, key="k", text=statement)
    if cited:
        record = observe_learning(
            record, LearningObservation(source="chat-a", turn=2), today=date(2024, 1, 1)
        )

    line = render_learning(record)
    visible = line.split(" " + METADATA_MARKER, 1)[0]
    if cited:
        # The real citation clause is written after the sentence, so it is the
        # last one on the line and the sentence in front of it is intact.
        assert visible.endswith("-- sources: y — sources: chat-a#2")
    assert statement in visible
    (reloaded,) = records(document(line + "\n"))
    assert reloaded.text == statement
    assert reloaded.learning_id == _UUID
    assert reloaded.observations == record.observations

    # The same statement typed by hand in a legacy bullet is read whole.
    (migrated,) = records(
        migrate_learnings(document(f"- {statement}\n"), workspace="work")[0]
    )
    assert migrated.text == statement


def test_subheading_under_active_keeps_its_bullets() -> None:
    text = document("### Work\n- A grouped bullet still migrates.\n")

    # A sub-heading groups inside the section it sits under. Treating it as the
    # end of the section would skip every bullet beneath it: no entry, no
    # record, and nothing to complain about, because nothing on those lines is
    # wrong.
    parsed = parse_learnings(text, workspace="work")
    assert not parsed.diagnostics
    assert [entry.section for entry in parsed.entries] == [SECTION_ACTIVE]
    assert [entry.record.text for entry in parsed.entries if entry.record] == [
        "A grouped bullet still migrates."
    ]

    migrated, diagnostics = migrate_learnings(text, workspace="work")
    assert not diagnostics
    assert migrated.startswith("# Learnings\n\n## Active\n### Work\n- [")
    (migrated_record,) = records(migrated)
    assert migrated_record.learning_id == records(text)[0].learning_id
    assert migrate_learnings(migrated, workspace="work")[0] == migrated

    # A deeper heading cannot move a bullet out of its section either, not even
    # when it is titled with another recognized section's name.
    nested = document("#### Promoted / Resolved\n- still active\n")
    entries = parse_learnings(nested, workspace="work").entries
    assert [entry.section for entry in entries] == [SECTION_ACTIVE]


def test_migration_is_deterministic_and_idempotent() -> None:
    text = document(f"{CURRENT}\n{LEGACY}\n{PLAIN}\n")

    first, _ = migrate_learnings(text, workspace="work")
    second, _ = migrate_learnings(first, workspace="work")
    assert first == second
    # Deterministic: the same input mints the same identifiers every time, in
    # a fresh process-equivalent call with no carried state.
    assert migrate_learnings(text, workspace="work")[0] == first

    # Source spans address the original text exactly, so a caller can splice
    # an entry without re-finding it.
    parsed = parse_learnings(text, workspace="work")
    assert parsed.text == text
    for entry in parsed.entries:
        assert text[entry.start : entry.end] == entry.source_text
        assert entry.source_text in text
    plain_entry = parsed.entries[-1]
    assert text[plain_entry.start : plain_entry.end] == PLAIN
    assert plain_entry.start == text.index(PLAIN)

    # A second pass changes nothing at all, and re-reading the result yields
    # the same identities, statement and evidence.
    before = records(text)
    after = records(first)
    assert [r.learning_id for r in after] == [r.learning_id for r in before]
    assert [r.text for r in after] == [r.text for r in before]
    assert [r.observations for r in after] == [r.observations for r in before]
    assert [r.legacy for r in after] == [r.legacy for r in before]


# ── Identity ───────────────────────────────────────────────────────────────


def test_learning_identity_survives_rewording_and_workspace_collisions() -> None:
    statement = "- Airtable sort param returns 400; filter by field ID."
    legacy = document(f"{statement}\n")

    work, _ = migrate_learnings(legacy, workspace="work")
    personal, _ = migrate_learnings(legacy, workspace="personal")
    prefix, _ = migrate_learnings(legacy, workspace="work-2")

    work_id = records(work)[0].learning_id
    # The workspace is part of the identity: a different workspace is a
    # different learning, and a workspace whose name starts with another's is
    # not the same one either.
    assert records(personal)[0].learning_id != work_id
    assert records(prefix)[0].learning_id != work_id

    # Rewording the statement on a migrated line is a visible edit only. The
    # stored identifier is read from the comment, never regenerated from text.
    reworded = work.replace(
        "Airtable sort param returns 400; filter by field ID.",
        "Filtering by field id avoids the Airtable sort 400.",
    )
    changed = records(reworded)[0]
    assert changed.learning_id == work_id
    assert changed.text != records(work)[0].text
    # The key is display, and it lives on the line: rewording the statement
    # leaves it where the owner put it. Only the identity is load-bearing.
    assert changed.key == records(work)[0].key
    # And a migrated reworded line is left alone rather than re-identified.
    assert migrate_learnings(reworded, workspace="work")[0] == reworded

    # An identifier with no comment yet is a uuid5 over a frozen namespace, the
    # workspace, the exact entry and its occurrence ordinal. The namespace is
    # fixed forever: regenerating it would re-identify every installed vault.
    assert ID_NAMESPACE == uuid.uuid5(uuid.NAMESPACE_DNS, "ciaobot.learning-records/v1")
    assert work_id == str(
        uuid.uuid5(
            ID_NAMESPACE,
            f"ciaobot/learning-record/v1\nwork\n{statement}\n0",
        )
    )


def test_duplicate_entries_and_duplicate_explicit_ids() -> None:
    duplicated = document(f"{PLAIN}\n{PLAIN}\n")
    migrated, _ = migrate_learnings(duplicated, workspace="work")
    first, second = records(migrated)

    # Two byte-identical legacy bullets stay two entries, not one entry
    # counted twice, and the occurrence ordinal is what separates them.
    assert first.learning_id != second.learning_id
    assert first.text == second.text == "A plain bullet with no structure at all."
    assert migrate_learnings(migrated, workspace="work")[0] == migrated

    # Copying a migrated line copies its identifier, which this document now
    # claims twice. That is a diagnosed conflict, never a silent merge: the
    # first line keeps the record and both sets of bytes survive.
    copied = next(line for line in migrated.splitlines() if line.startswith("- ["))
    collided = migrated + copied + "\n"
    parsed = parse_learnings(collided, workspace="work")
    assert [entry.format for entry in parsed.entries] == [
        FORMAT_CANONICAL,
        FORMAT_CANONICAL,
        FORMAT_CONFLICT,
    ]
    assert parsed.entries[2].record is None
    assert any(first.learning_id in problem for problem in parsed.diagnostics)
    assert any("already used" in problem for problem in parsed.diagnostics)
    assert migrate_learnings(collided, workspace="work")[0] == collided

    # Deciding what merges is a later job. All this does is carry merged ids
    # forward verbatim, so a consumer that made the decision can act on them.
    merged = replace(first, aliases=(second.learning_id,), count=first.count)
    (reloaded,) = records(document(render_learning(merged) + "\n"))
    assert reloaded.aliases == (second.learning_id,)
    assert reloaded.learning_id == first.learning_id
    assert len({reloaded.learning_id, *reloaded.aliases}) == 2


# ── Evidence ───────────────────────────────────────────────────────────────


def test_replayed_observation_does_not_inflate_count_or_date() -> None:
    migrated, _ = migrate_learnings(document(f"{CURRENT}\n"), workspace="work")
    (record,) = records(migrated)

    first = observe_learning(
        record, LearningObservation(source="chat-z"), today=date(2024, 6, 1)
    )
    assert (first.count, first.last_seen) == (4, date(2024, 6, 1))
    assert first.baseline_count == 3
    assert first.observed_count == 3

    # The same chat again, quoting the episode differently and on a later day.
    # Identity is the source, never the wording, and a retry is not a sighting.
    replay = observe_learning(
        first,
        LearningObservation(source="chat-z", excerpt="a completely different quote"),
        today=date(2024, 7, 1),
    )
    assert replay == first
    assert (replay.count, replay.last_seen) == (4, date(2024, 6, 1))

    # The same chat at a different turn is a different exchange, and a user
    # request id identifies on its own.
    turn = observe_learning(
        first, LearningObservation(source="chat-z", turn=7), today=date(2024, 7, 1)
    )
    request = observe_learning(
        first, LearningObservation(request="req-42"), today=date(2024, 7, 1)
    )
    assert (turn.count, turn.observed_count) == (5, 4)
    assert (request.count, request.observed_count) == (5, 4)
    assert turn.observations != request.observations


def test_display_citations_map_back_to_observation_identity() -> None:
    # `source#turn` and `req:<id>` are how the writer spells an observation on
    # the visible line. Reading them back as plain source ids made the line and
    # the machine record two different things, so replaying a source the
    # baseline already counted became a second sighting.
    text = document(
        "- [k] [2024-01-01 → 2024-02-01] (x3) t — sources: chat-a#3, req:r1\n"
    )
    (record,) = records(text)
    assert record.observations == (
        LearningObservation(source="chat-a", turn=3),
        LearningObservation(request="r1"),
    )

    # Both replays are the retry they are: nothing counted, no date moved.
    for sighting in (
        LearningObservation(source="chat-a", turn=3, excerpt="re-quoted"),
        LearningObservation(request="r1", excerpt="re-quoted"),
    ):
        replayed = observe_learning(record, sighting, today=date(2025, 1, 1))
        assert replayed == record
        assert (replayed.count, replayed.last_seen) == (3, date(2024, 2, 1))
        assert replayed.observed_count == 2

    # And the round trip keeps both spellings intact rather than flattening
    # them into one source with no turn.
    line = render_learning(record)
    assert " — sources: chat-a#3, req:r1 " in line
    assert records(document(line + "\n"))[0].observations == record.observations


def test_more_than_eight_observations_remain_deduplicated() -> None:
    record = LearningRecord(learning_id=_UUID, key="k", text="t", count=0)
    for index in range(10):
        record = observe_learning(
            record, LearningObservation(source=f"chat-{index}"), today=date(2024, 1, 1)
        )
    assert record.observed_count == 10

    line = render_learning(record)
    cited = line.split("— sources: ", 1)[1].split(" " + METADATA_MARKER, 1)[0]
    assert len(cited.split(", ")) == MAX_DISPLAY_SOURCES

    # The display cap is a display cap. Every observation survives the
    # round-trip, and the ones past the eighth are still no-ops on replay.
    (reloaded,) = records(document(line + "\n"))
    assert reloaded.observed_count == 10
    assert [o.source for o in reloaded.observations] == [
        f"chat-{index}" for index in range(10)
    ]
    for index in range(10):
        replayed = observe_learning(
            reloaded,
            LearningObservation(source=f"chat-{index}", excerpt="re-quoted"),
            today=date(2025, 1, 1),
        )
        assert replayed == reloaded
        assert replayed.count == 10


def test_unknown_history_and_baseline_do_not_invent_recurrence() -> None:
    # A known baseline is preserved as a baseline, and the sources it already
    # counted seed the seen set rather than becoming new sightings.
    migrated, _ = migrate_learnings(document(f"{CURRENT}\n"), workspace="work")
    (baseline,) = records(migrated)
    assert (baseline.count, baseline.baseline_count) == (3, 3)
    assert baseline.observed_count == 2  # two sources, not five sightings

    for source in ("chat-a", "chat-b"):
        replayed = observe_learning(
            baseline, LearningObservation(source=source), today=date(2024, 6, 1)
        )
        assert replayed == baseline
        assert (replayed.count, replayed.last_seen) == (3, date(2024, 3, 4))

    # A genuinely new sighting moves the count once, and the baseline stays put.
    fresh = observe_learning(
        baseline, LearningObservation(source="chat-z"), today=date(2024, 6, 1)
    )
    assert (fresh.count, fresh.baseline_count) == (4, 3)

    # An unknown history is not a count of one. A source-less retry is not a
    # sighting, and a dated one moves the date without inventing recurrence.
    (plain,) = records(document(f"{PLAIN}\n"))
    assert plain.count is None
    anonymous = observe_learning(
        plain,
        LearningObservation(excerpt="prose is not an identity"),
        today=date(2024, 6, 1),
    )
    assert anonymous == plain
    dated = observe_learning(
        plain, LearningObservation(source="chat-a"), today=date(2024, 6, 1)
    )
    assert dated.count is None and dated.last_seen == date(2024, 6, 1)
    assert dated.observed_count == 1


# ── Malformed input ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "prefix, entry, complaint, untouched",
    [
        # Dates, ranges and counts that do not hold up.
        (
            f"{PLAIN}\n",
            "- [k] [2024-02-30 → 2024-03-01] (x3) impossible day",
            "is neither a real date",
            False,
        ),
        (
            f"{PLAIN}\n",
            "- [k] [2024-03-01 → 2024-02-01] (x3) backwards",
            "is before first-seen",
            False,
        ),
        (
            f"{PLAIN}\n",
            "- [k] [2024-01-01 → 2024-02-01] (x0) zero",
            "not a positive count",
            False,
        ),
        (
            f"{PLAIN}\n",
            "- [k] [2024-01-01 → 2024-02-01] (xmany) garbage",
            "neither xN nor",
            False,
        ),
        (
            f"{PLAIN}\n",
            "- [2024-13-01] debugging: impossible month",
            "is not a real date",
            False,
        ),
        # Metadata that is not this schema, or is internally inconsistent.
        ("", f"{CURRENT} <!-- ciao:learning {{not json}} -->", "not valid JSON", True),
        ("", f"{CURRENT} <!-- ciao:learning {{'id':'x'}} -->", "not valid JSON", True),
        (
            "",
            f'{CURRENT} <!-- ciao:learning {{"schema":2,"id":"x"}} -->',
            "is not 1",
            True,
        ),
        (
            "",
            f'{CURRENT} <!-- ciao:learning {{"schema":1,"id":"not-a-uuid"}} -->',
            "is not a UUID",
            True,
        ),
        (
            "",
            f'{CURRENT} <!-- ciao:learning {{"schema":1,"id":"{_UUID}","observations":[{{"excerpt":"none"}}]}} -->',
            "no stable identity",
            True,
        ),
        (
            "",
            f'{CURRENT} <!-- ciao:learning {{"schema":1,"id":"{_UUID}","unknown_field":1}} -->',
            "unknown field",
            True,
        ),
        (
            "",
            f'{CURRENT} <!-- ciao:learning {{"schema":1,"id":"{_UUID}","baseline":0}} -->',
            "baseline must be a positive integer",
            True,
        ),
        (
            "",
            f'{CURRENT} <!-- ciao:learning {{"schema":1,"id":"{_UUID}","baseline":9}} -->',
            "exceeds the recurrence",
            True,
        ),
    ],
)
def test_malformed_dates_metadata_and_unknown_shapes_are_preserved(
    prefix: str, entry: str, complaint: str, untouched: bool
) -> None:
    text = document(f"{prefix}{entry}\n")

    parsed = parse_learnings(text, workspace="work")
    broken = parsed.entries[-1]
    assert broken.format == FORMAT_MALFORMED
    assert broken.record is None
    assert broken.source_text == entry
    assert any(complaint in problem for problem in parsed.diagnostics)
    assert any(complaint in problem for problem in broken.diagnostics)

    # An unreadable shape is not guessed at and not erased: the migration
    # keeps its exact bytes and reports why. Malformed metadata in particular
    # is never read as an ordinary bullet, which would drop the identity and
    # the evidence the comment carried.
    migrated, diagnostics = migrate_learnings(text, workspace="work")
    assert entry in migrated.splitlines()
    assert any(complaint in problem for problem in diagnostics)
    assert (migrated == text) is untouched
    assert parse_learnings(migrated, workspace="work").entries[-1].format == (
        FORMAT_MALFORMED
    )
    if not untouched:
        # The readable entry beside it still migrates.
        assert METADATA_MARKER in migrated


def test_metadata_comment_cannot_escape_or_corrupt_roundtrip() -> None:
    hostile = (
        'Check <!-- ciao:learning {"id":"forged"} --> and --> and <script>alert(1)</script> '
        "plus a -- comment terminator"
    )
    text = document(f"- {hostile}\n")

    migrated, diagnostics = migrate_learnings(text, workspace="work")
    assert not diagnostics
    line = next(item for item in migrated.splitlines() if item.startswith("- "))
    # The statement survives verbatim, and the payload cannot close the
    # comment that holds it: the real comment is the last one on the line, and
    # no raw angle bracket reaches the machine record.
    payload = line.rpartition(METADATA_MARKER)[2]
    assert payload.endswith(" -->")
    assert payload[:-4].count("-->") == 0
    assert payload[:-4].count("<!--") == 0
    assert "<" not in payload[:-4] and ">" not in payload[:-4]
    assert parse_learnings(migrated, workspace="work").entries[0].record.text == hostile

    # An identifier carrying comment syntax round-trips the same way, and the
    # whole line survives a second migration byte for byte.
    record = observe_learning(
        LearningRecord(learning_id=_UUID, key="k", text=hostile),
        LearningObservation(source="chat<!--a-->b"),
        today=date(2024, 1, 1),
    )
    rendered = render_learning(record)
    payload = rendered.rpartition(METADATA_MARKER)[2]
    assert payload[:-4].count("-->") == 0
    (reloaded,) = records(document(rendered + "\n"))
    assert reloaded.text == hostile
    assert reloaded.observations[0].source == "chat<!--a-->b"
    assert migrate_learnings(document(rendered + "\n"), workspace="work")[
        0
    ] == document(rendered + "\n")

    # A record with no readable identity is refused at the write, rather than
    # producing a line this module would diagnose as broken much later.
    for bad in ("", "not-a-uuid", "k"):
        with pytest.raises(ValueError, match="is not a UUID"):
            render_learning(LearningRecord(learning_id=bad, key="k", text="t"))


def test_render_flattens_the_key_and_refuses_brackets() -> None:
    # A key is written between brackets on a line-oriented bullet, so it gets
    # the same flattening the statement does: a newline would split the entry in
    # two and the continuation would be read as its own learning.
    record = LearningRecord(learning_id=_UUID, key="two\n  words  here", text="t")
    line = render_learning(record)
    assert line.startswith("- [two words here] ")
    assert "\n" not in line
    (reloaded,) = records(document(line + "\n"))
    assert reloaded.key == "two words here"
    assert reloaded.learning_id == _UUID

    # A bracket cannot be flattened away, and a key carrying one is written to
    # a line the canonical shape cannot match — so the key would be re-read as
    # prose and the learning would lose its identity. Refuse it at the write,
    # where the caller still knows which record it was.
    for bad in ("br[ack]ets", "]nested[", "["):
        with pytest.raises(ValueError, match="contains a bracket"):
            render_learning(replace(record, key=bad))
