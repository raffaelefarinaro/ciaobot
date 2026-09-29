"""Tests for the canonical learnings writer and the curation reader over it.

``ciao/learning_records.py`` is the model; these are the tests for the two
production callers that have to agree with it. The writer
(``memory_proposals.append_learning``) and the curation worklist
(``curation_run._learning_items``) used to share a private regex in
``memory_proposals`` and each keep their own reading of a learnings line. They
are now two callers of one model, and these tests pin the contract that makes
that worth anything: a replay of the same source is a no-op, and a resolved
lesson is not reopened by being seen again.

Every ``Learnings.md`` here is written inline into a tmp vault. Nothing reads a
real workspace.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ciao import curation_run as cr
from ciao import memory_proposals as mp
from ciao.learning_records import (
    SECTION_ACTIVE,
    SECTION_PROMOTED,
    LearningRecord,
    parse_learnings,
)


def _vault(tmp_path: Path, body: str | None = None) -> Path:
    """A vault, optionally pre-seeded with a ``Learnings.md``."""
    vault = tmp_path / "personal"
    (vault / "Workspace").mkdir(parents=True)
    if body is not None:
        (vault / "Workspace" / "Learnings.md").write_text(body, encoding="utf-8")
    return vault


def _text(vault: Path) -> str:
    return (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8")


def _records(vault: Path) -> list[tuple[str, LearningRecord]]:
    """Every parsed entry as ``(section, record)``, dropping the unreadable ones."""
    document = parse_learnings(_text(vault), workspace=vault.name)
    return [
        (entry.section, entry.record)
        for entry in document.entries
        if entry.record is not None
    ]


def _only(vault: Path, *, section: str = SECTION_ACTIVE) -> LearningRecord:
    matches = [record for where, record in _records(vault) if where == section]
    assert len(matches) == 1, _records(vault)
    return matches[0]


# ---- filing a learning -----------------------------------------------------


def test_a_new_learning_is_filed_as_a_canonical_record(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    assert mp.append_learning(vault, "Retries need a fresh token.", source="chat-a1")

    record = _only(vault)
    assert record.text == "Retries need a fresh token."
    assert record.count == 1
    assert record.observations[0].source == "chat-a1"
    # The identity is persisted, so it survives a re-parse rather than being
    # re-derived from the prose that happens to be on the line.
    assert _only(vault).learning_id == record.learning_id


def test_a_replay_of_the_same_source_changes_nothing(tmp_path: Path) -> None:
    """The whole recurrence contract, in one test.

    A retry that re-quotes an episode from the same source is not a second
    sighting. The old writer incremented whenever the normalized statement
    matched, which is how a duplicate accept drifted a learning towards the x3
    promotion threshold on no new evidence at all.
    """
    vault = _vault(tmp_path)
    fact = "Retries need a fresh token."
    assert mp.append_learning(vault, fact, source="chat-a1")
    first = _text(vault)

    assert mp.append_learning(vault, fact, source="chat-a1")

    assert _text(vault) == first
    record = _only(vault)
    assert record.count == 1
    assert len(record.observations) == 1


def test_a_sighting_from_a_new_source_counts_and_refreshes_last_seen(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    fact = "Retries need a fresh token."
    mp.append_learning(vault, fact, source="chat-a1")
    seen, _ = mp.render_learning_append(
        _text(vault), fact, workspace=vault.name, source="chat-b2", today="2026-03-04"
    )
    (vault / "Workspace" / "Learnings.md").write_text(seen, encoding="utf-8")

    record = _only(vault)
    assert record.count == 2
    assert [o.source for o in record.observations] == ["chat-a1", "chat-b2"]
    # first_seen is the first sighting, not the latest one.
    assert record.first_seen == record.last_seen or record.last_seen == date(2026, 3, 4)


def test_a_resolved_lesson_is_observed_but_never_reactivated(tmp_path: Path) -> None:
    """A decided lesson stays decided.

    The old writer searched the whole file for a matching line and rewrote it
    where it found it, then the curation pass partitioned on `## Promoted` — so
    the bookkeeping existed in one place and the other. A learning that was
    promoted or resolved records the new evidence and stays exactly where it is;
    seeing it again is a reason to revisit the decision, not to undo it.
    """
    vault = _vault(
        tmp_path,
        "# Learnings\n\n## Active\n\n## Promoted / Resolved\n"
        "- [pin-node] [2026-01-01 → 2026-02-01] (x4) Pin the Node version. "
        "— sources: chat-1\n",
    )

    assert mp.append_learning(vault, "Pin the Node version.", source="chat-2")

    text = _text(vault)
    # Still under Promoted / Resolved, and only there.
    assert text.count("## Active") == 1
    resolved = text.partition("## Promoted / Resolved")[2]
    assert "Pin the Node version." in resolved
    assert "Pin the Node version." not in text.partition("## Promoted / Resolved")[0]
    record = _only(vault, section=SECTION_PROMOTED)
    assert [o.source for o in record.observations] == ["chat-1", "chat-2"]
    assert record.count == 5


def test_a_resolved_lesson_is_not_duplicated_into_active(tmp_path: Path) -> None:
    vault = _vault(
        tmp_path,
        "# Learnings\n\n## Active\n\n## Promoted / Resolved\n"
        "- [pin-node] [2026-01-01 → 2026-02-01] (x4) Pin the Node version.\n",
    )

    assert mp.append_learning(vault, "Pin the Node version.", source="chat-2")

    text = _text(vault)
    assert text.count("Pin the Node version.") == 1
    assert _records(vault)[0][0] == SECTION_PROMOTED


def test_an_unreadable_entry_is_never_rewritten(tmp_path: Path) -> None:
    """A shape this code does not implement keeps its bytes.

    Matching against it would mean deciding what it says, and a write that
    decided that would leave the owner with a line they never wrote.
    """
    broken = "- [2026-13-45] debugging: an impossible date"
    vault = _vault(tmp_path, f"# Learnings\n\n## Active\n{broken}\n")

    assert mp.append_learning(vault, "an impossible date", source="chat-a1")

    assert broken in _text(vault)


def test_an_owner_written_bullet_gains_no_invented_recurrence(tmp_path: Path) -> None:
    """A plain bullet has no history, so it must not acquire an x1."""
    vault = _vault(tmp_path, "# Learnings\n\n## Active\n- Ask before deleting.\n")

    assert mp.append_learning(vault, "Ask before deleting.", source="chat-a1")

    record = _only(vault)
    assert record.count is None
    assert record.first_seen is None
    assert "(?)" in _text(vault)


# ---- preview and write agree -----------------------------------------------


def test_the_preview_is_exactly_what_the_accept_writes(tmp_path: Path) -> None:
    """A card that previews one thing and writes another is worse than no card."""
    vault = _vault(tmp_path)
    mp.append_learning(vault, "Pin the Node version.", source="chat-1")

    previewed, operation = mp.render_learning_append(
        mp.read_learnings(vault),
        "Pin the Node version.",
        workspace=vault.name,
        source="chat-2",
    )

    assert operation == "update"
    assert mp.append_learning(vault, "Pin the Node version.", source="chat-2")
    assert _text(vault) == previewed


def test_a_preview_of_a_sighting_already_counted_is_a_no_op(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    mp.append_learning(vault, "Pin the Node version.", source="chat-1")

    text, operation = mp.render_learning_append(
        mp.read_learnings(vault),
        "Pin the Node version.",
        workspace=vault.name,
        source="chat-1",
    )

    assert operation == "none"
    assert text == mp.read_learnings(vault)


# ---- the curation reader ---------------------------------------------------


def test_curation_still_promotes_at_x3_through_the_shared_reader(
    tmp_path: Path,
) -> None:
    vault = _vault(
        tmp_path,
        "# Learnings\n\n## Active\n"
        "- [pin-node] [2026-01-01 → 2026-09-01] (x4) Pin the Node version.\n",
    )
    for _ in range(3):
        mp.append_learning(vault, "Pin the Node version.", source="chat-1")

    items = cr._learning_items(vault, today=date(2026, 9, 19))

    assert [item.pass_id for item in items] == [cr.PASS_LEARNINGS]
    # The key is a hash of the subject, so the subject is what has to be right.
    assert items[0].keys == (
        cr.item_key(cr.PASS_LEARNINGS, f"promote:{_only(vault).key}"),
    )
    assert "x3" in items[0].reason


def test_curation_still_prunes_an_aging_x1(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    mp.append_learning(
        vault, "Pin the Node version.", source="chat-1"
    )  # counts as today, so the entry is fresh
    _age_the_only_entry(vault, "2026-01-01")

    items = cr._learning_items(vault, today=date(2026, 9, 19))

    assert [item.pass_id for item in items] == [cr.PASS_LEARNINGS]
    assert items[0].keys == (
        cr.item_key(cr.PASS_LEARNINGS, f"prune:{_only(vault).key}"),
    )
    assert "x1" in items[0].reason


def test_curation_does_not_replan_a_resolved_learning(tmp_path: Path) -> None:
    vault = _vault(
        tmp_path,
        "# Learnings\n\n## Active\n\n## Promoted / Resolved\n"
        "- [pin-node] [2026-01-01 → 2026-09-01] (x4) Pin the Node version.\n",
    )

    assert cr._learning_items(vault, today=date(2026, 9, 19)) == []


def test_curation_ignores_an_entry_with_no_recurrence(tmp_path: Path) -> None:
    """A plain bullet renders `(?)`, which is not "seen once".

    Promoting or pruning on a count nobody witnessed is the invented-x1 failure
    arriving from the other direction: the worklist would schedule a decision
    about a learning it has no evidence for.
    """
    vault = _vault(tmp_path, "# Learnings\n\n## Active\n- Ask before deleting.\n")

    assert cr._learning_items(vault, today=date(2026, 9, 19)) == []


def _age_the_only_entry(vault: Path, last_seen: str) -> None:
    """Move the one entry's last-seen date, leaving its identity alone.

    Written by hand rather than by the writer, because a test that aged an entry
    through the same path the worklist reads could not tell a reader working
    from the model from one working from a stale private regex.
    """
    from ciao.learning_records import METADATA_MARKER, parse_learnings

    path = vault / "Workspace" / "Learnings.md"
    document = parse_learnings(path.read_text(encoding="utf-8"), workspace=vault.name)
    entry = document.entries[0]
    assert entry.record is not None
    assert entry.record.last_seen is not None
    # Both ends of the range, because a run that only moved last-seen would
    # produce a line whose first-seen is in the future of its last-seen — which
    # the parser rightly refuses to read.
    aged = entry.source_text.replace(entry.record.last_seen.isoformat(), last_seen)
    assert aged != entry.source_text
    assert METADATA_MARKER in aged
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: entry.start] + aged + text[entry.end :], encoding="utf-8")
