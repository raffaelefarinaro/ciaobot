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

import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from ciao import curation_run as cr
from ciao import memory_proposals as mp
from ciao.learning_records import (
    SECTION_ACTIVE,
    SECTION_HEADINGS,
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


# ---- the owner's bytes survive the write -----------------------------------


CRLF_BODY = (
    "﻿# Learnings\r\n"
    "\r\n"
    "## Format\r\n"
    "Entries look like `- [key] [first -> last] (xN) text`.\r\n"
    "\r\n"
    "## Active\r\n"
    "- [a-b] [2024-01-01 → 2024-01-01] (x1) A b.\r\n"
    "\r\n"
    "## Promoted / Resolved\r\n"
    "- [pin-node] [2024-01-01 → 2024-02-01] (x4) Pin the Node version.\r\n"
)


def _crlf_vault(tmp_path: Path) -> Path:
    """A vault whose ``Learnings.md`` is BOM + CRLF, written as bytes.

    A learning file is the owner's prose, and a CRLF one is a file an editor on
    another platform produced. Reading it through a newline-translating API and
    writing it back changes bytes the writer never meant to touch — and silently
    invalidates every ``learnings-*.json`` receipt, because the receipt's
    recorded spans no longer address what is on disk.
    """
    vault = _vault(tmp_path)
    (vault / "Workspace" / "Learnings.md").write_bytes(CRLF_BODY.encode("utf-8"))
    return vault


def _raw(vault: Path) -> bytes:
    return (vault / "Workspace" / "Learnings.md").read_bytes()


def test_a_crlf_file_keeps_its_line_endings_through_a_new_entry(
    tmp_path: Path,
) -> None:
    vault = _crlf_vault(tmp_path)

    assert mp.append_learning(vault, "A brand new learning.", source="chat-9")

    raw = _raw(vault)
    # The byte-order mark and every CRLF pair survive, so the lines this write
    # did not edit are byte-identical to what the owner had.
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\n" not in raw.replace(b"\r\n", b"")
    # The new entry is filed, and the surrounding prose is untouched.
    assert "A brand new learning." in raw.decode("utf-8")
    assert "## Format\r\nEntries look like" in raw.decode("utf-8")
    assert "Pin the Node version.\r\n" in raw.decode("utf-8")


def _filed_line(existing: str, statement: str, *, workspace: str) -> str:
    """The one line :func:`render_learning_append` would add, and nothing else.

    Shared by the placement tests so each of them states where the entry is
    supposed to land rather than re-deriving the whole expected document.
    """
    after, operation = mp.render_learning_append(
        existing, statement, workspace=workspace, source="chat-9"
    )
    assert operation == "add"
    return next(
        line
        for line in after.replace("\r\n", "\n").split("\n")
        if statement in line and line.startswith("- [")
    )


def test_a_new_entry_lands_on_its_own_line_and_touches_nothing_else() -> None:
    """Whole-file equality, because a partial assertion cannot see this.

    The insertion offset is the whole of the defect this covers: an off-by-one
    on a CRLF file lands *inside* the next line, so the new entry starts with the
    existing entry's bullet marker and the owner's line loses its own. Both files
    still contain both statements and still end in a newline, so an assertion
    about substrings, heading counts or "no bare LF" passes over it.
    """
    existing = CRLF_BODY
    filed = _filed_line(existing, "A brand new learning.", workspace="personal")

    after, operation = mp.render_learning_append(
        existing, "A brand new learning.", workspace="personal", source="chat-9"
    )

    assert operation == "add"
    assert after == existing.replace("## Active\r\n", f"## Active\r\n{filed}\r\n")
    # The owner's line is not merely present but unmoved and unmodified.
    assert "\r\n- [a-b] [2024-01-01 → 2024-01-01] (x1) A b.\r\n" in after
    assert "## Promoted / Resolved\r\n" in after


def test_a_new_entry_lands_after_a_blank_line_following_the_heading() -> None:
    """A blank line after the heading is the shape the bug corrupted worst.

    The off-by-one ate the `\\n` of the heading's terminator *and* the `\\r` of the
    blank line, leaving a stray carriage return in the middle of the file and a
    lone LF where the blank line was.
    """
    existing = CRLF_BODY.replace("## Active\r\n- [a-b]", "## Active\r\n\r\n- [a-b]")
    filed = _filed_line(existing, "A brand new learning.", workspace="personal")

    after, _ = mp.render_learning_append(
        existing, "A brand new learning.", workspace="personal", source="chat-9"
    )

    assert after == existing.replace("## Active\r\n", f"## Active\r\n{filed}\r\n")
    assert "\r" not in after.replace("\r\n", "")
    assert "\r\n\r\n- [a-b]" in after


def test_a_new_entry_lands_after_an_lf_terminated_heading() -> None:
    """The one shape that already worked, pinned so it keeps working."""
    existing = CRLF_BODY.replace("\r\n", "\n")
    filed = _filed_line(existing, "A brand new learning.", workspace="personal")

    after, _ = mp.render_learning_append(
        existing, "A brand new learning.", workspace="personal", source="chat-9"
    )

    assert after == existing.replace("## Active\n", f"## Active\n{filed}\n")


def test_a_new_entry_lands_after_a_heading_that_is_the_last_line() -> None:
    """A heading with no terminator still needs the entry on a line of its own.

    There is no newline to insert after, so the writer has to supply one. The
    failure mode is silent and total: the heading and the entry end up on one
    line, and `## Active- [key] …` is not a heading and not an entry — so the
    entry is in the file and in no section at all.
    """
    # A heading that is the document's last line, with and without a terminator,
    # in an LF document and a CRLF one.
    for existing, newline in (
        ("# Learnings\n\n## Active", "\n"),
        ("# Learnings\n\n## Active\n", "\n"),
        ("# Learnings\r\n\r\n## Active", "\r\n"),
        ("# Learnings\r\n\r\n## Active\r\n", "\r\n"),
    ):
        filed = _filed_line(existing, "A brand new learning.", workspace="personal")

        after, operation = mp.render_learning_append(
            existing, "A brand new learning.", workspace="personal", source="chat-9"
        )

        assert operation == "add", existing
        assert after == f"{existing.rstrip()}{newline}{filed}{newline}", existing
        # The entry is a line of its own, so the parser can still read it.
        assert [line for line in after.replace("\r\n", "\n").split("\n") if line.startswith("- [")] == [filed]


def test_a_new_entry_lands_after_a_heading_with_trailing_whitespace() -> None:
    """`## Active   ` is still the heading.

    A hand edit leaves trailing spaces, and a pattern that requires the line to
    end immediately after the word opens a *second* ``## Active`` under a heading
    that was already there — two sections where the owner wrote one.
    """
    existing = "# Learnings\n\n## Active  \n- [a-b] [2024-01-01 → 2024-01-01] (x1) A b.\n"
    filed = _filed_line(existing, "A brand new learning.", workspace="personal")

    after, _ = mp.render_learning_append(
        existing, "A brand new learning.", workspace="personal", source="chat-9"
    )

    assert after == existing.replace("## Active  \n", f"## Active  \n{filed}\n")
    assert after.count("## Active") == 1


def test_a_new_entry_does_not_concatenate_onto_a_longer_final_heading() -> None:
    """The heading must be matched whole, not as a prefix of a longer title.

    A regex that stops at `Active` without requiring the line to end there
    matches `## Active extras and notes`, and the entry is then filed into a
    section the parser does not recognize — so the file gains a second Active
    list that only the writer can see.
    """
    existing = (
        "# Learnings\n\n"
        "## Active extras and notes\n"
        "- [a-b] [2024-01-01 → 2024-01-01] (x1) A b.\n"
    )

    after, operation = mp.render_learning_append(
        existing, "A brand new learning.", workspace="personal", source="chat-9"
    )

    assert operation == "add"
    lines = after.split("\n")
    # The heading the owner wrote is untouched, and a real one is opened for the
    # entry rather than borrowed from theirs.
    assert "## Active extras and notes" in lines
    assert [line for line in lines if line.strip() == "## Active"] == ["## Active"]
    opened = lines.index("## Active")
    entry = next(
        line for line in lines if "A brand new learning." in line and line.startswith("- [")
    )
    # Under the new heading, not under theirs, and on its own line.
    assert lines.index(entry) == opened + 2


def test_the_line_ending_comes_from_the_documents_first_terminator() -> None:
    """`_newline`'s documented rule, pinned.

    A file whose endings already disagree is not this writer's to normalize, and
    rewriting one half of it would be a change the owner never asked for. So the
    first terminator decides, and the rest of the document is left as it is.
    """
    assert mp._newline("a\r\nb\nc") == "\r\n"
    assert mp._newline("a\nb\r\nc") == "\n"
    # No terminator at all, and a lone carriage return, both fall back to `\n`.
    assert mp._newline("") == "\n"
    assert mp._newline("one line") == "\n"
    assert mp._newline("a\rb") == "\n"


def test_a_crlf_file_keeps_its_line_endings_through_a_recurrence(
    tmp_path: Path,
) -> None:
    """The update path rewrites a line in place, not the whole file's endings."""
    vault = _crlf_vault(tmp_path)

    assert mp.append_learning(vault, "A b.", source="chat-9")

    raw = _raw(vault)
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert b"x2" in raw


def test_a_crlf_file_gains_no_second_active_section(tmp_path: Path) -> None:
    """A heading the writer cannot find is a section it opens a second time.

    The old marker was the literal `\\n## Active\\n`, which never matches
    `## Active\\r\\n`. A new entry would then be filed under a second `## Active`
    at the end of the file — invisible in the preview, and a document whose
    sections no longer say what they say.
    """
    vault = _crlf_vault(tmp_path)

    assert mp.append_learning(vault, "A brand new learning.", source="chat-9")

    text = _raw(vault).decode("utf-8")
    assert text.count("## Active") == 1
    assert text.count(f"## {SECTION_HEADINGS[SECTION_PROMOTED]}") == 1


def test_a_new_entry_goes_under_active_not_at_the_end_of_the_file(
    tmp_path: Path,
) -> None:
    """Where the entry lands is the observable difference above."""
    vault = _crlf_vault(tmp_path)

    assert mp.append_learning(vault, "A brand new learning.", source="chat-9")

    text = _raw(vault).decode("utf-8")
    _, _, active = text.partition("## Active")
    active, _, resolved = active.partition(
        f"## {SECTION_HEADINGS[SECTION_PROMOTED]}"
    )
    assert "A brand new learning." in active
    assert "A brand new learning." not in resolved


def test_the_write_goes_through_the_atomic_helper(
    tmp_path: Path, monkeypatch
) -> None:
    """In-place `write_text` truncates before it writes.

    A crash or a full disk mid-write takes the whole file with it, and this file
    is the only record of what the workspace learned. The atomic helper's temp
    sibling must also be gone afterwards, or the vault grows a file nobody owns.
    """
    vault = _crlf_vault(tmp_path)
    from ciao import memory_receipts

    written: list[tuple[str, str]] = []
    real = memory_receipts.write_queue_atomically

    def _recording(path: Path, text: str) -> None:
        written.append((Path(path).name, text))
        real(path, text)

    monkeypatch.setattr(memory_receipts, "write_queue_atomically", _recording)

    assert mp.append_learning(vault, "A brand new learning.", source="chat-9")

    assert [name for name, _ in written] == ["Learnings.md"]
    assert "A brand new learning." in written[0][1]
    # And the helper's temp sibling is gone: the vault must not grow a file
    # nobody owns.
    assert [p.name for p in (vault / "Workspace").iterdir()] == ["Learnings.md"]


def test_the_write_is_serialized_against_the_migration(
    tmp_path: Path, monkeypatch
) -> None:
    """Both writers of this file take the same lock.

    The migration's whole safety argument is a revision check plus a rename; an
    accept that lands between that check and the rename would be discarded by
    the migration, or would discard the migration. They are serialized by
    `queue_lock`, so the two cannot interleave — which is only true if the accept
    takes it too.
    """
    from ciao.os_support.locks import lock_exclusive

    vault = _crlf_vault(tmp_path)
    from ciao import memory_receipts

    taken: list[str] = []
    real = memory_receipts.queue_lock

    @contextmanager
    def _recording(target, **kwargs):
        taken.append(Path(target).name)
        with real(target, **kwargs):
            # The write must happen while the lock is held, not after it is
            # released: a lock taken and dropped before the rename serializes
            # nothing.
            fd = os.open(target, os.O_RDONLY)
            try:
                lock_exclusive(fd, blocking=False)
            finally:
                os.close(fd)
            yield

    monkeypatch.setattr(memory_receipts, "queue_lock", _recording)
    assert mp.append_learning(vault, "A brand new learning.", source="chat-9")

    assert taken == ["Learnings.md"]


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
