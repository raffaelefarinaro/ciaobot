"""Contract for `ciao.note_entries`: what an entry is, and who it is.

The module is the shared parser and identity model for entity-note facts, so
these tests are the contract every later child (the detector, the check state,
the mutation, the worklist, the UI) inherits. They are written against the
module docstring, not against the implementation, and they never touch disk:
every note here is a synthetic string, and `parse_note_entries` takes text
rather than a path precisely so that a caller — and this file — can exercise it
without a vault.

Three things are pinned throughout:

* an entry's text is the exact slice `original[start:end]`, so an entry can
  always be found again in the file it came from;
* `uncovered` is the exact complement of the entries, so a coverage claim
  cannot silently overstate what was read;
* a stamp is metadata and a line number is not an identity, so re-stamping or
  moving a fact does not invent a new one.
"""

from __future__ import annotations

import datetime

import pytest

from ciao import note_entries as ne

# Fixed "today" so a future-dated stamp stays future-dated however long this
# test suite keeps running.
TODAY = datetime.date(2026, 9, 29)

FAKE = datetime.date(2999, 1, 1)


def _parse(text: str, **kwargs: object) -> ne.EntryDocument:
    kwargs.setdefault("today", TODAY)
    return ne.parse_note_entries(text, **kwargs)  # type: ignore[arg-type]


def _texts(doc: ne.EntryDocument) -> list[str]:
    return [entry.text for entry in doc.entries]


# ── The entry model ───────────────────────────────────────────────────────


def test_bullets_ordered_multiline_and_nested() -> None:
    text = (
        "# Facts\n"
        "\n"
        "- Mo leads the platform team\n"
        "  and joined in 2024\n"
        "  - Mo prefers terse reviews\n"
        "- Sam runs operations\n"
        "\n"
        "* Asterisk item\n"
        "+ Plus item\n"
        "1. First ordered\n"
        "2) Second ordered\n"
    )
    doc = _parse(text)

    # A nested child is its own entry, never folded into the parent, and the
    # parent's continuation line is part of the parent.
    assert _texts(doc) == [
        "- Mo leads the platform team\n  and joined in 2024",
        "  - Mo prefers terse reviews",
        "- Sam runs operations",
        "* Asterisk item",
        "+ Plus item",
        "1. First ordered",
        "2) Second ordered",
    ]

    kinds = {entry.marker: entry.kind for entry in doc.entries}
    assert kinds["-"] == ne.KIND_UNORDERED
    assert kinds["*"] == ne.KIND_UNORDERED
    assert kinds["+"] == ne.KIND_UNORDERED
    assert kinds["1."] == ne.KIND_ORDERED
    assert kinds["2)"] == ne.KIND_ORDERED

    # Every entry is locatable in the original, and the opening-line offsets
    # bound exactly the line a stamp is read from.
    for entry in doc.entries:
        assert doc.original[entry.start : entry.end] == entry.text
        assert doc.original[entry.opening_start : entry.opening_end] == (
            entry.text.split("\n")[0]
        )

    # A multi-assertion bullet is one entry: a parser that split claims out of
    # a bullet would be rewriting the note, not reading it.
    combined = _parse("- Mo leads platform\n- Mo also runs hiring\n")
    assert len(combined.entries) == 2

    # A blank-line-separated block indented into the item is still the item's
    # text (CommonMark's loose item), and is reported as not a plain fact.
    loose = _parse("- first block\n\n  second block\n- sibling\n")
    assert _texts(loose)[0] == "- first block\n\n  second block"
    assert loose.entries[0].supported is False
    assert ne.DIAG_MULTI_BLOCK in loose.entries[0].diagnostics
    assert _texts(loose)[1] == "- sibling"


def test_skip_frontmatter_fences_headings_blockquotes_and_tables() -> None:
    text = (
        "---\n"
        "type: person\n"
        "updated: 2026-01-01\n"
        "---\n"
        "\n"
        "# Mo\n"
        "\n"
        "- a real fact\n"
        "\n"
        "Some prose that is not a bullet.\n"
        "\n"
        "---\n"
        "\n"
        "> - quoted bullet is not an entry\n"
        "\n"
        "| Name | Role |\n"
        "| --- | --- |\n"
        "| Mo | lead |\n"
        "\n"
        "```markdown\n"
        "- fenced bullet is not an entry\n"
        "```\n"
        "\n"
        "Title\n"
        "=====\n"
        "\n"
        "- another real fact\n"
    )
    doc = _parse(text)

    assert _texts(doc) == ["- a real fact", "- another real fact"]
    for entry in doc.entries:
        assert "not an entry" not in entry.text

    # Frontmatter, headings, prose, quotes, tables and fences are all reported
    # as uncovered rather than dropped, and coverage still adds up to the file.
    assert doc.entry_chars + doc.uncovered_chars == doc.total_chars
    assert doc.entry_chars == sum(len(entry.text) for entry in doc.entries)
    uncovered = "".join(doc.original[start:end] for start, end in doc.uncovered)
    for skipped in (
        "type: person",
        "# Mo",
        "Some prose",
        "quoted bullet",
        "| Name | Role |",
        "fenced bullet",
        "Title",
    ):
        assert skipped in uncovered

    # And the skipped lines are not in any entry either.
    for entry in doc.entries:
        assert "type: person" not in entry.text
        assert "|" not in entry.text

    # A bare `---` between body blocks is a thematic break, not frontmatter
    # and not a setext heading under a list item.
    assert _parse("- a\n---\n- b\n").entries[0].text == "- a"

    # A setext heading is a section, from both spellings.
    for underline in ("===", "---"):
        doc = _parse(f"## Later\n\nTitle\n{underline}\n\n- a fact\n")
        assert doc.entries[0].section == "Title", underline
        assert doc.entries[0].section_level == (1 if underline == "===" else 2)
    # An underline under something that is not a paragraph is a rule, so the
    # section stays where it was.
    for base in ("- an item", "    code", "> quoted", "| a | b |"):
        doc = _parse(f"## Later\n\n{base}\n---\n\n- a fact\n")
        assert doc.entries[0].section == "Later", base

    # A rule after a *wrapped* bullet is a rule too. The line above it belongs
    # to an entry, not to a paragraph, so lifting it as a heading would file
    # every later entry in the note under a section named after a bullet's
    # continuation line — and a section name is half an entry's identity.
    wrapped = _parse("## A\n- one\n  wrapped\n---\n- two\n")
    assert [(e.text, e.section) for e in wrapped.entries] == [
        ("- one\n  wrapped", "A"),
        ("- two", "A"),
    ]
    # The same after an entry that ends in a fenced block.
    fenced = _parse("## A\n- one\n    ```\n    x\n    ```\n---\n- two\n")
    assert [e.section for e in fenced.entries] == ["A", "A"]

    # A heading indented up to three spaces still names a section; the level is
    # its own `#` run, not a difference between two differently stripped
    # strings.
    for indent in ("", " ", "  ", "   "):
        doc = _parse(f"## A\n{indent}## B\n- one\n")
        assert doc.entries[0].section == "B", repr(indent)
        assert doc.entries[0].section_level == 2, repr(indent)
    for marks, level in (("#", 1), ("###", 3), ("######", 6)):
        doc = _parse(f"{marks} Heading\n- one\n")
        assert doc.entries[0].section == "Heading", marks
        assert doc.entries[0].section_level == level, marks

    # A heading's text is its own words, not every `#` stripped from both ends.
    # A `#` only closes the heading when whitespace precedes it, so `## C#` is
    # about C# and `# #tag` is about a `#tag`; a whitespace-preceded one does
    # close it, which is what `## Trailing #` is.
    for heading, expected in (
        ("## C#", "C#"),
        ("# #tag", "#tag"),
        ("## Title ##", "Title"),
        ("### Deep ###", "Deep"),
        ("## Trailing #", "Trailing"),
        ("# ", ""),
    ):
        doc = _parse(f"{heading}\n- one\n")
        assert doc.entries[0].section == expected, heading
        # The level is the *leading* run of `#`, which a `#` inside the
        # heading's own text does not extend.
        run = heading.lstrip()
        assert doc.entries[0].section_level == len(run) - len(run.lstrip("#")), heading


def test_frontmatter_needs_to_look_like_frontmatter() -> None:
    """A leading `---` is only frontmatter if what follows is frontmatter.

    A note that opens with a thematic break and closes with another one is an
    ordinary shape, and treating the pair as a frontmatter block dropped every
    bullet between them without a word of complaint. Refusing to guess is the
    point: the opener stays a rule, the body is read, and the diagnostic says
    what was seen.
    """
    rules = _parse("---\n\n- a\n\n---\n- b\n")
    assert _texts(rules) == ["- a", "- b"]
    assert ne.DIAG_UNCLOSED_FRONTMATTER in rules.diagnostics
    # Nothing was silently dropped, and coverage still adds up.
    assert rules.entry_chars + rules.uncovered_chars == rules.total_chars

    # Real frontmatter, closed either way, is still frontmatter.
    for closer in ("---", "..."):
        doc = _parse(f"---\ntype: person\nupdated: 2026-01-01\n{closer}\n- a fact\n")
        assert _texts(doc) == ["- a fact"], closer
        assert doc.diagnostics == (), closer
    # An empty block is frontmatter too.
    assert _texts(_parse("---\n---\n- a fact\n")) == ["- a fact"]

    # Only `---` opens it. A leading `...` is a closing delimiter, and treating
    # it as an opener swallowed the note.
    leading_dots = _parse("...\n- a fact\n- another fact\n")
    assert _texts(leading_dots) == ["- a fact", "- another fact"]

    # A `key:`-shaped line is what makes it frontmatter; content where a key
    # was expected leaves the opener as a rule.
    for body in ("- a fact", "# Heading", "just prose", "> quoted"):
        doc = _parse(f"---\n{body}\n---\n- a fact\n")
        assert _texts(doc)[-1] == "- a fact", body
        assert ne.DIAG_UNCLOSED_FRONTMATTER in doc.diagnostics, body

    # An unterminated but key-shaped block is a diagnostic, never a reason to
    # drop the body.
    unclosed = _parse("---\ntype: person\n- a fact\n")
    assert _texts(unclosed) == ["- a fact"]
    assert unclosed.diagnostics == (ne.DIAG_UNCLOSED_FRONTMATTER,)

    # Keys all the way to the end of the note is the same case with nowhere
    # left to read, and the same answer.
    keys_to_eof = _parse("---\ntype: person\n")
    assert keys_to_eof.entries == ()
    assert keys_to_eof.diagnostics == (ne.DIAG_UNCLOSED_FRONTMATTER,)
    assert keys_to_eof.entry_chars + keys_to_eof.uncovered_chars == (
        keys_to_eof.total_chars
    )


# ── The verification stamp ────────────────────────────────────────────────


def test_verified_stamp_parse_and_invalid_diagnostics() -> None:
    doc = _parse(
        "- checked yesterday [verified: 2026-09-28]\n"
        "- impossible [verified: 2025-02-30]\n"
        "- malformed [verified: 26-1-1]\n"
        "- missing value [verified: ]\n"
        "- future [verified: 2999-01-01]\n"
        "- trailing and spare [verified: 2026-06-30]   \n"
        "- promoted today [2026-09-01]\n"
        "- snapshot [as-of: 2026-09-01]\n"
        "- evented on 2026-09-01 with no tag\n"
        "- unverified altogether\n"
    )
    entries = {entry.text: entry for entry in doc.entries}
    assert len(entries) == 10

    def entry_containing(needle: str) -> ne.NoteEntry:
        """The one entry whose text names this case, so a rename cannot pass
        the rest of the test by accident."""
        matches = [e for e in doc.entries if needle in e.text]
        assert len(matches) == 1, needle
        return matches[0]

    good = entry_containing("checked yesterday")
    assert good.stamp == ne.EntryStamp(
        "[verified: 2026-09-28]", datetime.date(2026, 9, 28), True, ""
    )
    assert good.verified == datetime.date(2026, 9, 28)
    assert good.diagnostics == ()

    impossible = entry_containing("impossible")
    assert impossible.stamp is not None
    assert impossible.stamp.valid is False
    assert impossible.stamp.reason == ne.STAMP_REASON_IMPOSSIBLE
    assert impossible.stamp.date is None
    assert impossible.verified is None
    assert ne.DIAG_STAMP_IMPOSSIBLE in impossible.diagnostics
    # The raw text is kept so a caller can show what was rejected.
    assert impossible.stamp.raw == "[verified: 2025-02-30]"

    malformed = entry_containing("malformed")
    assert malformed.stamp is not None
    assert malformed.stamp.reason == ne.STAMP_REASON_MALFORMED
    assert ne.DIAG_STAMP_MALFORMED in malformed.diagnostics

    missing = entry_containing("missing value")
    assert missing.stamp is not None
    assert missing.stamp.reason == ne.STAMP_REASON_MALFORMED
    assert missing.verified is None

    future = entry_containing("future")
    assert future.stamp is not None
    # A future date is a real date and still not a verification: nobody
    # checked a fact on a day that has not come.
    assert future.stamp.date == FAKE
    assert future.stamp.valid is False
    assert future.stamp.reason == ne.STAMP_REASON_FUTURE
    assert future.verified is None
    assert ne.DIAG_STAMP_FUTURE in future.diagnostics

    # Trailing whitespace after the stamp is still trailing, and the separator
    # it introduced is part of what the fingerprint removes.
    trailing = entry_containing("trailing and spare")
    assert trailing.stamp is not None
    assert trailing.stamp.valid is True
    assert trailing.verified == datetime.date(2026, 6, 30)
    assert ne.refresh_fingerprint("- trailing and spare") == trailing.fingerprint

    # A learned-at stamp and an as-of stamp are different claims and are never
    # reinterpreted as a verification.
    for needle in ("promoted today", "snapshot", "evented on", "unverified"):
        assert entry_containing(needle).stamp is None
        assert entry_containing(needle).verified is None
    # …and they stay part of the fact's text, so a learned stamp still changes
    # the fingerprint when it changes.
    assert (
        entry_containing("promoted today").fingerprint
        != entry_containing("unverified").fingerprint
    )

    # The standalone seam agrees with the document parse: a real date is a
    # valid stamp, and the other two temporal tags are not stamps at all.
    assert (
        ne.parse_verification_stamp("x [verified: 2026-09-28]", today=TODAY)
        == good.stamp
    )
    assert ne.parse_verification_stamp("x [2026-01-01]", today=TODAY) is None
    assert ne.parse_verification_stamp("x [as-of: 2026-01-01]", today=TODAY) is None
    loose = ne.parse_verification_stamp("x [verified 2026-01-01]", today=TODAY)
    assert loose is not None
    assert loose.valid is False
    assert loose.reason == ne.STAMP_REASON_MALFORMED

    # "Future" is relative to the day the caller passes, so one fixed stamp is
    # a valid verification on one day and not on an earlier one.
    same_text = f"- fact [verified: {TODAY.isoformat()}]\n"
    assert _parse(same_text).entries[0].verified == TODAY
    yesterday = _parse(same_text, today=TODAY - datetime.timedelta(days=1))
    assert yesterday.entries[0].verified is None
    assert ne.DIAG_STAMP_FUTURE in yesterday.entries[0].diagnostics


def test_stamp_must_be_trailing_to_count() -> None:
    """A `[verified: …]` in the middle of a bullet is prose, not a claim.

    The plan says *trailing inline stamp on the opening line*, and the
    difference is not cosmetic: cutting a mid-line token out of the text for
    the fingerprint would change an entry's identity because of a mention of a
    date, and reading a link label as a verification would report a
    verification nobody made.
    """
    mid_line = (
        "- [verified: 2026-01-01] fact",
        "- a [verified: 2026-01-01] then more",
        "- fact [verified: 2026-01-01](http://x)",
        "- fact [verified: 2026-01-01] and [as-of: 2026-01-01]",
    )
    for text in mid_line:
        entry = _parse(f"{text}\n").entries[0]
        assert entry.stamp is None, text
        assert entry.verified is None, text
        # No diagnostic either: a mention of the tag is not a typo in a stamp.
        assert entry.diagnostics == (), text
        # And the token stays in the fingerprint payload untouched.
        assert ne.refresh_fingerprint(entry.text) == entry.fingerprint, text

    # The trailing form is still read, so the distinction is the position and
    # not the token.
    trailing = _parse("- fact [verified: 2026-01-01]\n").entries[0]
    assert trailing.stamp is not None
    assert trailing.stamp.valid is True

    # A CRLF line's carriage return is not what makes a stamp non-trailing.
    crlf = _parse("- fact [verified: 2026-01-01]\r\n  continued\r\n").entries[0]
    assert crlf.stamp is not None
    assert crlf.stamp.valid is True


def test_loose_stamp_pattern_ignores_ordinary_text() -> None:
    """The malformed-stamp report must not fire on text that is not a stamp.

    The loose pattern exists so a *typo in the real tag* gets reported. A
    wikilink, an attribution and a bracketed word all contain the letters
    "verified" and none of them is a broken verification, so a false positive
    here trains the reader to skip the report.
    """
    for text in (
        "- see [[verified plan]] now",
        "- see [[verified plan]]",
        "- [verified-by-bob] x",
        "- [verified] x",
        "- [verified plan] x",
        "- the [verified] tag",
    ):
        entry = _parse(f"{text}\n").entries[0]
        assert entry.stamp is None, text
        assert entry.diagnostics == (), text

    # A near-miss of the real tag is still reported, so a user who meant to
    # write one is told.
    for text in (
        "- fact [verified 2026-01-01]",
        "- fact [verified:26-1-1]",
        "- fact [verified 26-1-1]",
    ):
        entry = _parse(f"{text}\n").entries[0]
        assert entry.stamp is not None, text
        assert entry.stamp.valid is False, text
        assert entry.stamp.reason == ne.STAMP_REASON_MALFORMED, text
        assert ne.DIAG_STAMP_MALFORMED in entry.diagnostics, text

    # A link whose label is a near-miss is a link, not a broken stamp.
    linked = _parse("- fact [verified 2026-01-01](http://x)\n").entries[0]
    assert linked.stamp is None
    assert linked.diagnostics == ()


def test_restamp_does_not_change_fingerprint_but_prose_edit_does() -> None:
    first = _parse("- Mo leads the platform team [verified: 2026-01-01]\n").entries[0]
    restamped = _parse(
        "- Mo leads the platform team [verified: 2026-06-30]\n"
    ).entries[0]
    edited = _parse("- Mo leads the platform group [verified: 2026-01-01]\n").entries[0]

    # The stamp is metadata about the fact, not part of it.
    assert first.fingerprint == restamped.fingerprint
    assert first.verified != restamped.verified
    # Editing the prose is editing the fact.
    assert first.fingerprint != edited.fingerprint

    # `refresh_fingerprint` answers for an entry's *text*, which is the only
    # form in which the question is real: a frozen NoteEntry cannot be asked
    # what its own id will be after an edit.
    assert ne.refresh_fingerprint(restamped.text) == restamped.fingerprint
    assert ne.refresh_fingerprint(edited.text) == edited.fingerprint
    assert ne.refresh_fingerprint(first.text) == first.fingerprint
    # So a caller can compute the id the note will have *after* a re-stamp: the
    # new text fingerprints to the entry's existing identity.
    restamped_text = "- Mo leads the platform team [verified: 2027-01-01]"
    assert ne.refresh_fingerprint(restamped_text) == first.fingerprint
    # And after a prose edit it will not be the same fact.
    assert (
        ne.refresh_fingerprint(
            "- Mo leads the platform group [verified: 2027-01-01]"
        )
        != first.fingerprint
    )

    # A fact that is nothing but a stamp asserts nothing, and says so — but a
    # fact whose words are on a continuation line is a fact.
    stamp_only = _parse("- [verified: 2026-01-01]\n").entries[0]
    assert stamp_only.supported is False
    assert ne.DIAG_STAMP_ONLY in stamp_only.diagnostics
    for text in (
        "- [verified: 2026-01-01]\n  actual fact",
        "-\n  text [verified: 2026-01-01]",
        "-\n  actual fact\n",
    ):
        entry = _parse(f"{text}\n").entries[0]
        assert entry.supported is True, text
        assert ne.DIAG_STAMP_ONLY not in entry.diagnostics, text

    # The stamp is only read on the opening line: a `[verified:]` mentioned in
    # a continuation line is prose.
    mentioned = _parse("- a fact\n  re-verified [verified: 2026-01-01]\n")
    assert mentioned.entries[0].stamp is None
    assert mentioned.entries[0].verified is None


# ── Identity ──────────────────────────────────────────────────────────────


def test_identity_survives_line_moves_and_duplicates_stay_distinct() -> None:
    before = _parse(
        "# Work\n\n- Mo leads the platform team\n- Sam runs operations\n",
        note_path="People/Mo.md",
        workspace="personal",
    )
    after = _parse(
        "# Work\n\n"
        "An unrelated paragraph inserted above the facts.\n\n"
        "- Sam runs operations\n"
        "- An unrelated fact\n"
        "  with a continuation line added\n"
        "- Mo leads the platform team\n"
        "\n"
        "## A section added below\n\n"
        "- A fact from the new section\n",
        note_path="People/Mo.md",
        workspace="personal",
    )
    moved = {entry.text: entry for entry in after.entries}
    original = {entry.text: entry for entry in before.entries}

    for text in original:
        # Reordering the note and inserting unrelated content move an entry's
        # line number and its offsets and change nothing about which fact it is.
        assert moved[text].identity == original[text].identity
        assert moved[text].fingerprint == original[text].fingerprint
    assert (
        moved["- Mo leads the platform team"].line_number
        != original["- Mo leads the platform team"].line_number
    )
    # The facts really did move, so the check above is not vacuous.
    assert _texts(after)[:3] != _texts(before)

    # Identity is a function of the coordinates the note is filed under, so a
    # different note, workspace or heading is a different fact.
    mo = original["- Mo leads the platform team"]
    elsewhere = _parse(
        "# Work\n\n- Mo leads the platform team\n",
        note_path="People/Sam.md",
        workspace="personal",
    )
    assert elsewhere.entries[0].identity != mo.identity
    other_workspace = _parse(
        "# Work\n\n- Mo leads the platform team\n",
        note_path="People/Mo.md",
        workspace="studio",
    )
    assert other_workspace.entries[0].identity != mo.identity
    other_section = _parse(
        "# Home\n\n- Mo leads the platform team\n",
        note_path="People/Mo.md",
        workspace="personal",
    )
    assert other_section.entries[0].identity != mo.identity

    # Two identical bullets under one heading say the same thing and are
    # distinct facts, so they must not collapse into one identity.
    twins = _parse(
        "# Work\n\n- Mo leads the platform team\n- Mo leads the platform team\n",
        note_path="People/Mo.md",
        workspace="personal",
    )
    first, second = twins.entries
    assert first.text == second.text
    assert first.fingerprint == second.fingerprint
    assert first.identity != second.identity
    assert (first.ordinal, second.ordinal) == (0, 1)

    # The same bullet under two headings is not a duplicate either.
    split = _parse(
        "# Work\n\n- Mo leads the platform team\n"
        "\n# Home\n\n- Mo leads the platform team\n",
        note_path="People/Mo.md",
        workspace="personal",
    )
    assert split.entries[0].identity != split.entries[1].identity

    # Identity is a pure function of the entry: recomputing it changes nothing.
    assert ne.entry_identity(first) == first.identity


# ── Bytes ─────────────────────────────────────────────────────────────────


def test_crlf_and_bom_preserved() -> None:
    crlf = "﻿- first fact [verified: 2026-01-01]\r\n- second fact\r\n"
    doc = _parse(crlf)

    # Nothing is rewritten, and offsets index the original exactly, BOM and all.
    assert doc.original == crlf
    assert doc.original.startswith("﻿")
    for entry in doc.entries:
        assert doc.original[entry.start : entry.end] == entry.text
    assert doc.entries[0].text.startswith("﻿")
    assert doc.entries[0].line_number == 0

    # A single-line entry carries no terminator in its own text, so a CRLF note
    # and an LF note with the same one-line bullets are the same fact.
    lf = _parse(crlf.replace("\r\n", "\n"))
    assert doc.entries[0].fingerprint == lf.entries[0].fingerprint
    assert "\r" not in doc.entries[0].text

    # A multi-line entry does carry its own terminators, and there the file's
    # bytes are the bytes that are hashed — the two spellings of one note are
    # not the same note, so they must not collide.
    wrapped = _parse("- first fact\r\n  continued\r\n")
    assert wrapped.entries[0].text == "- first fact\r\n  continued"
    assert "\r" in wrapped.entries[0].text
    assert wrapped.entries[0].fingerprint != _parse(
        "- first fact\n  continued\n"
    ).entries[0].fingerprint
    # And the hash is over exactly those bytes, so a caller can reproduce it.
    import hashlib

    assert wrapped.entries[0].fingerprint == hashlib.sha256(
        "- first fact\r\n  continued".encode("utf-8")
    ).hexdigest()

    # Coverage accounting is over the same bytes.
    assert doc.entry_chars + doc.uncovered_chars == len(crlf)


def test_crlf_fence_and_inline_code_do_not_create_entries() -> None:
    doc = _parse(
        "```markdown\r\n"
        "- fenced bullet\r\n"
        "\r\n"
        "- still fenced\r\n"
        "```\r\n"
        "- real fact [verified: 2026-01-01]\r\n"
    )
    assert _texts(doc) == ["- real fact [verified: 2026-01-01]"]
    assert doc.diagnostics == ()
    assert doc.entries[0].stamp is not None
    assert doc.entries[0].stamp.valid is True

    # A tilde fence, a four-backtick fence (so a three-backtick run inside it is
    # content, not a premature close), and a fence indented inside a list item.
    tilde = _parse("~~~yaml\n- not a fact\n~~~\n- a fact\n")
    assert _texts(tilde) == ["- a fact"]
    long_fence = _parse(
        "````markdown\n- not a fact\n```\n- also not a fact\n````\n- a fact\n"
    )
    assert _texts(long_fence) == ["- a fact"]
    # A fence indented two or three spaces is a document-level fence to
    # CommonMark, so it ends the item and stays out of it.
    for indent in ("  ", "   "):
        indented = _parse(f"- a fact\n{indent}```\n{indent}code\n{indent}```\n- next\n")
        assert _texts(indented) == ["- a fact", "- next"], indent
        for entry in indented.entries:
            assert "code" not in entry.text, indent

    # Indented four or more it is nested *in* the item, which is past the three
    # spaces a document-level fence may use — so the item owns it, and the
    # bullets inside it are code within that fact rather than facts of their
    # own.
    nested = _parse("- Run:\n    ```\n    - not an entry\n    ```\n- next\n")
    assert _texts(nested) == [
        "- Run:\n    ```\n    - not an entry\n    ```",
        "- next",
    ]
    assert nested.entry_count == 2
    # The owner still reports itself: one fingerprint over a bullet with a code
    # block in it is a fingerprint over a shape this model does not describe.
    assert nested.entries[0].supported is False
    assert ne.DIAG_NESTED_CONSTRUCT in nested.entries[0].diagnostics
    assert nested.entries[1].supported is True

    # The same inside a child item: the child owns the fence, the parent and
    # the sibling are untouched, and nothing inside the fence is an entry.
    child = _parse("- p\n  - child\n    ```\n    - inside\n    ```\n- z\n")
    assert _texts(child) == [
        "- p",
        "  - child\n    ```\n    - inside\n    ```",
        "- z",
    ]
    assert child.entry_count == 3

    # A nested fence is closed by the item that owns it, not by a line at the
    # parent's indentation.
    deep = _parse(
        "- a\n  - b\n    ```\n    - inside\n    ```\n  - c\n- d\n"
    )
    assert _texts(deep) == [
        "- a",
        "  - b\n    ```\n    - inside\n    ```",
        "  - c",
        "- d",
    ]

    # A tilde fence nested in an item behaves the same way.
    tilde_nested = _parse(
        "- Run:\n    ~~~\n    - not an entry\n    ~~~\n- next\n"
    )
    assert _texts(tilde_nested) == [
        "- Run:\n    ~~~\n    - not an entry\n    ~~~",
        "- next",
    ]

    # An unterminated nested fence runs to the end of the item's indented run,
    # not to the end of the note: the lines after the item are the next block,
    # not more of this one.
    unterminated = _parse("- Run:\n    ```\n    - inside\n- next\n- another\n")
    assert _texts(unterminated) == [
        "- Run:\n    ```\n    - inside",
        "- next",
        "- another",
    ]

    # A tilde fence does not close on backticks, and a backtick fence with an
    # info string carrying one is not a fence at all.
    mixed = _parse("```\n~~~\n- not a fact\n```\n- a fact\n")
    assert _texts(mixed) == ["- a fact"]
    not_a_fence = _parse("```a`b\n- a fact\n```\n")
    assert ne.DIAG_UNTERMINATED_FENCE in not_a_fence.diagnostics

    # A closing fence carries no info string, so a fence-looking line that has
    # one is content: the fence runs on, and the fact after it stays inside.
    info_on_close = _parse("```\n- not a fact\n``` py\n- still not a fact\n")
    assert _texts(info_on_close) == []
    assert info_on_close.diagnostics == (ne.DIAG_UNTERMINATED_FENCE,)

    # Backticks inside inline code never open a fence and never hide a fact.
    inline = _parse(
        "- uses `-like-this` inline and ```three``` inline\n"
        "- plain fact\n"
        "text with a `- ` marker inside `code`\n"
    )
    assert _texts(inline) == [
        "- uses `-like-this` inline and ```three``` inline",
        "- plain fact",
    ]
    # A fence-looking run inside inline code does not swallow the rest.
    for entry in inline.entries:
        assert entry.supported is True
        assert entry.diagnostics == ()


def test_malformed_markdown_reports_without_raising() -> None:
    # An unterminated fence: everything after it is code, and that is said.
    unterminated = "```\n- never an entry\n"
    doc = _parse(unterminated)
    assert doc.original == unterminated
    assert doc.diagnostics == (ne.DIAG_UNTERMINATED_FENCE,)
    assert doc.entries == ()
    assert doc.entry_chars == 0
    assert doc.uncovered_chars == len(unterminated)

    # Unclosed frontmatter is a rule, not a block nobody finished: the body is
    # still read rather than silently dropped.
    unclosed = "---\ntype: person\n- a fact\n"
    doc = _parse(unclosed)
    assert doc.diagnostics == (ne.DIAG_UNCLOSED_FRONTMATTER,)
    assert _texts(doc) == ["- a fact"]

    # Mixed markers and bare markers are all read as items, and a bare marker
    # is an empty bullet: reported, never hashed.
    mixed = "- a\n*\n+ b\n1. c\n3) d\n-   \n"
    doc = _parse(mixed)
    assert [(entry.marker, entry.kind) for entry in doc.entries] == [
        ("-", ne.KIND_UNORDERED),
        ("+", ne.KIND_UNORDERED),
        ("1.", ne.KIND_ORDERED),
        ("3)", ne.KIND_ORDERED),
    ]
    assert doc.diagnostics.count(ne.DIAG_EMPTY_LIST_ITEM) == 2
    assert doc.original == mixed

    # A bare marker that carries a continuation line is not empty.
    continued = _parse("-\n  only a continuation\n")
    assert _texts(continued) == ["-\n  only a continuation"]
    assert ne.DIAG_EMPTY_LIST_ITEM not in continued.diagnostics

    # A date, an emphasis run and a stray pipe are not markers.
    for text in ("2026-09-29 was a Tuesday\n", "*emphasis* here\n", "| not | a |\n"):
        doc = _parse(text)
        assert doc.entries == ()
        assert doc.diagnostics == ()

    # A stray carriage return is reported, and the text is unchanged.
    cr = "- a fact\r- another fact\n"
    doc = _parse(cr)
    assert doc.original == cr
    assert ne.DIAG_BARE_CR in doc.diagnostics

    # Degenerate inputs are documents, not failures.
    for text in ("", "\n", "\n\n\n", "-", "- \n", "   \n"):
        doc = _parse(text)
        assert doc.original == text
        assert doc.entry_chars + doc.uncovered_chars == len(text)
    assert _parse("").coverage_ratio == 0.0
    assert _parse("").entries == ()

    # An item holding a nested construct is reported, so a caller can refuse to
    # read it as one atomic claim instead of trusting a fingerprint over it.
    # Each kind is checked, since the report is per-entry rather than per-note.
    for nested_line in (
        "    > quoted inside the bullet",
        "    # heading inside the bullet",
        "    | a | table inside the bullet |",
        "    ---",
        "    ~~~",
        "    ```",
    ):
        entry = _parse(f"- a fact\n{nested_line}\n").entries[0]
        assert entry.supported is False, nested_line
        assert ne.DIAG_NESTED_CONSTRUCT in entry.diagnostics, nested_line
        # A plain continuation line is none of these, and stays supported.
    assert _parse("- a fact\n    continued\n").entries[0].supported is True

    # A tab-indented item is an item, and its continuation is compared by
    # display width rather than by character count.
    tabbed = _parse("-\tfact\n\tcontinued\n")
    assert _texts(tabbed) == ["-\tfact\n\tcontinued"]

    # A blank line that only partially dedents ends the item, so the text below
    # it is not quietly absorbed into the fact it no longer belongs to.
    dedented = _parse("- a fact\n\n not indented enough\n- next fact\n")
    assert _texts(dedented) == ["- a fact", "- next fact"]
    # …while a block indented to the item's content column is still its own.
    kept = _parse("- a fact\n\n  still inside\n")
    assert _texts(kept) == ["- a fact\n\n  still inside"]


# ── The records ──────────────────────────────────────────────────────────


def test_records_are_frozen_and_self_consistent() -> None:
    doc = _parse("- a fact\n", note_path="People/Mo.md", workspace="personal")
    entry = doc.entries[0]

    with pytest.raises(AttributeError):
        entry.text = "- something else"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        doc.original = ""  # type: ignore[misc]

    # The document carries the coordinates its entries were parsed under, so a
    # caller never has to carry them separately.
    assert doc.note_path == entry.note_path == "People/Mo.md"
    assert doc.workspace == entry.workspace == "personal"
    assert doc.entry_count == 1
    assert doc.total_chars == len(doc.original)
    assert 0.0 < doc.coverage_ratio < 1.0
