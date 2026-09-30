"""Per-entry parsing and identity for the facts an entity note asserts.

Why this module exists
----------------------
A multi-fact note has no single age. `ciao/memory_audit.py` ages a whole note
from its frontmatter `updated:` or its mtime, and `find_aging_state` /
`strip_learned_stamp` read temporal stamps off bounded-region entries. Neither
can say that *one* fact inside a note is out of date while its neighbours are
current, because a note is not the unit a person maintains — the bullet is.
This module is the foundation for that: one shared, tested parser and identity
model, with no consumer yet.

It is deliberately inert. It reads a string, it writes nothing, it makes no
model call, and it imports nothing from `ciao` (a leaf module, like
`vault_links.py`, so a consumer anywhere in the vault stack can depend on it
without a cycle). The detector, the check state, the mutation, the worklist and
the UI are separate changes that build on the contract below.

The contract
------------
This docstring is the interface. Everything a later child may rely on is
stated here, and the rest of the module is the implementation of it.

**What an entry is.** One Markdown list item — unordered (`-`, `*`, `+`) or
ordered (`1.`, `1)`) — plus its continuation lines: the following lines that
are more indented than the item's marker and do not themselves start a new
block. A nested child item is its own separate entry and is *not* part of the
parent's text. A bullet carrying several assertions is one entry; splitting a
claim out of a bullet is a rewrite of the note, not something a parser may
invent. An item's text is `EntryDocument.original[start:end]`, so the entry can
always be located back in the file byte for byte.

**What is not an entry.** Headings, thematic breaks and setext underlines,
blockquotes, tables (any line whose first non-space character is `|`, plus a
pipe-delimited delimiter row), YAML frontmatter, and fenced code blocks. A
`- item` line inside a ``` or ~~~ fence is code, not a fact. Fence tracking
covers both fence characters, an info string, up to three leading spaces of
indentation, a closing fence with no info string, and CRLF — and a fence nested
*inside* a list item, at that item's content column, which is where a command
written under a bullet belongs and which is only two spaces in for `- `. Its
lines are absorbed whole: a bullet written inside someone's code sample is part
of the fact, never a fact of its own, and an unclosed one ends with its item
rather than swallowing the entries below it.

**The verification stamp.** `[verified: YYYY-MM-DD]`, *trailing* on the entry's
opening line, is the only thing this module reads as a claim that someone
re-checked the fact. Trailing is load-bearing: a token in the middle of a
bullet is prose that mentions a date, and reading it would both invent a
verification nobody made and change the entry's identity over a mention. It is
deliberately distinct from `[as-of: YYYY-MM-DD]` (a snapshot of a changing
world), from an event date in prose, and from the bounded regions' learned-at
`[YYYY-MM-DD]`; a learned or as-of stamp is never reinterpreted as
verification. The shape is strict — a real calendar date, or nothing. An
impossible date (`2025-02-30`), a malformed one (`26-1-1`) and a future one
each yield an `EntryStamp` with `valid=False`, a `reason`, and a diagnostic,
never a guess and never a raised error. A *near-miss* of the tag — the tag
spelling with a typo in its separator — is reported so a user who meant to
write one is told; ordinary text that merely contains the word is not. Two
stamps on one line are reported as a duplicate rather than resolved: the
trailing one is the claim, the earlier one stays in the text, and a tool that
appends rather than replaces changes the fingerprint with something to say so.

**Fingerprint.** sha256 over the entry's exact source text with the
verification stamp token removed, encoded as UTF-8 using the file's own
newline bytes. Nothing else is normalized: CRLF stays CRLF, trailing spaces
stay, and a byte-order mark stays. The stamp is metadata, so a pure re-stamp
of a fact changes nothing about what the fact says; editing the prose does.
The removed span is the stamp token plus the run of whitespace immediately
before it, which is exactly the separator the stamp introduced.
`refresh_fingerprint(text)` answers the same question for text the caller has
just edited, which is the only form in which it is a real question.

**Identity.** sha256 over the identity version, the workspace, the note path,
the nearest preceding heading's text, the fingerprint, and a duplicate
occurrence ordinal. The ordinal is what keeps two identical bullets in one
section apart, and the absence of any line number is the point: inserting or
reordering unrelated content moves an entry's `line_number` and changes
nothing about its identity. Line numbers are display-only.

**Coverage.** `EntryDocument.uncovered` is the exact complement of the entries
in `original`, so a caller can report what it did *not* read as honestly as
what it did: `entry_chars + uncovered_chars == len(original)` always holds. A
note whose facts live in prose paragraphs, a table, or a code block is not a
note this module covered, and saying so is the point.

**It never raises on ordinary markdown.** Unterminated fences, a `---` that
turned out not to be frontmatter, mixed markers, empty items and stray
carriage returns are diagnostics over identical text.
"""

from __future__ import annotations

import datetime
import hashlib
import re
from dataclasses import dataclass, replace

# ── Versions ──────────────────────────────────────────────────────────────
IDENTITY_VERSION = "note-entry-identity/v1"
"""Domain separator for :func:`entry_identity`. Bumped if the identity inputs
change shape, so an id minted under one contract can never be mistaken for an
id minted under another."""


# ── Vocabulary ────────────────────────────────────────────────────────────
KIND_UNORDERED = "unordered"
"""A `-`, `*` or `+` item."""

KIND_ORDERED = "ordered"
"""A `1.` or `1)` item."""

STAMP_REASON_MALFORMED = "malformed"
"""The token is shaped like a verification stamp but carries no `YYYY-MM-DD`."""

STAMP_REASON_IMPOSSIBLE = "impossible"
"""The value has the right shape and is not a real calendar date."""

STAMP_REASON_FUTURE = "future"
"""A real date that has not happened yet, so it is not a verification."""


# ── Diagnostics ───────────────────────────────────────────────────────────
#
# Plain kebab codes, so a caller can count them without parsing prose and a
# test can pin one. They are evidence, never control flow: no code makes the
# parse take a different branch.

DIAG_UNCLOSED_FRONTMATTER = "unclosed-frontmatter"
DIAG_UNTERMINATED_FENCE = "unterminated-fence"
DIAG_EMPTY_LIST_ITEM = "empty-list-item"
DIAG_BARE_CR = "bare-cr-line-ending"
DIAG_STAMP_MALFORMED = "verified-stamp-malformed"
DIAG_STAMP_IMPOSSIBLE = "verified-stamp-impossible"
DIAG_STAMP_FUTURE = "verified-stamp-future"
DIAG_STAMP_DUPLICATE = "verified-stamp-duplicate"
DIAG_NESTED_CONSTRUCT = "entry-nested-construct"
DIAG_MULTI_BLOCK = "entry-multi-block"
DIAG_STAMP_ONLY = "entry-stamp-only"

_STAMP_DIAGNOSTICS = {
    STAMP_REASON_MALFORMED: DIAG_STAMP_MALFORMED,
    STAMP_REASON_IMPOSSIBLE: DIAG_STAMP_IMPOSSIBLE,
    STAMP_REASON_FUTURE: DIAG_STAMP_FUTURE,
}


# ── Block structure ───────────────────────────────────────────────────────
#
# The patterns below are deliberately line-anchored and none of them is
# tolerant of "roughly": a `- ` line is an item or it is prose, and a guess
# either way becomes a phantom fact in a note about a person.

# A fence opener: up to three leading spaces (CommonMark's limit), then three
# or more backticks or tildes, then an optional info string. A backtick fence
# may not carry a backtick in its info string (see `_fence_match`); a tilde
# fence may, which is the usual escape hatch for showing markdown in markdown.
_FENCE_RE = re.compile(r"^(?P<indent>[ ]{0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

_ATX_HEADING_RE = re.compile(r"^[ ]{0,3}#{1,6}(?:[ \t]+|$)")

# An ATX heading's optional closing sequence. CommonMark allows a trailing run
# of `#` only when whitespace precedes it, which is the whole difference
# between `## Title ##` (title) and `## C#` (C#).
_ATX_CLOSING_RE = re.compile(r"[ \t]+#+[ \t]*$")

# A setext underline. Only an underline under a plain paragraph line is a
# heading; on its own, `---` is a thematic break and `-` is an empty item.
_SETEXT_RE = re.compile(r"^[ ]{0,3}(?:=+|-+)[ \t]*$")

# Three or more `*`, `-` or `_` with optional whitespace between them.
_THEMATIC_BREAK_RE = re.compile(
    r"^(?:[ ]{0,3}\*[ \t]*){3,}$"
    r"|^(?:[ ]{0,3}-[ \t]*){3,}$"
    r"|^(?:[ ]{0,3}_[ \t]*){3,}$"
)

_BLOCKQUOTE_RE = re.compile(r"^[ ]{0,3}>")

# A table row: the first non-space character is a pipe.
_TABLE_ROW_RE = re.compile(r"^[ ]{0,3}\|")

# A table delimiter row. The lookahead and the mandatory inner group both
# require a pipe, so `---` alone stays a thematic break and a bare `-` stays an
# empty list item instead of becoming a one-column table.
_TABLE_DELIMITER_RE = re.compile(
    r"^(?=[^\n]*\|)\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)+\|?[ \t]*$"
)

# One list item marker. The lookahead is what separates an item from prose:
# `- item` and a bare `-` are items, `-1kg of rope` and `*emphasis*` are not,
# and neither is a date like `2026-09-29` (a bare digit run is never a marker
# without `.` or `)` behind it).
_LIST_ITEM_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>[-*+]|\d{1,9}[.)])(?=[ \t]|$)"
)

# Internal block kinds. Not part of the public vocabulary: a caller reports on
# entries and coverage, not on how the line walk classified a line.
_BLOCK_BLANK = "blank"
_BLOCK_PLAIN = "plain"
_BLOCK_FENCE = "fence"
_BLOCK_HEADING = "heading"
_BLOCK_RULE = "rule"
_BLOCK_QUOTE = "quote"
_BLOCK_TABLE = "table"
_BLOCK_ITEM = "item"

# The kinds that open a block and therefore end an item's continuation run.
_NEW_BLOCK_KINDS = frozenset(
    {_BLOCK_FENCE, _BLOCK_HEADING, _BLOCK_RULE, _BLOCK_QUOTE, _BLOCK_TABLE}
)

_FRONTMATTER_DELIMITERS = ("---", "...")

# A frontmatter key line. What separates real frontmatter from a thematic break
# that happens to open the note: frontmatter is `key: value` from its first
# non-blank line.
_FRONTMATTER_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+[ \t]*:")

# Tabs advance to the next four-column stop rather than counting as one
# character, so an item opened with a tab and continued with spaces compares
# the way a reader sees it.
_TAB_WIDTH = 4

_BOM = "\ufeff"
_BARE_CR_RE = re.compile(r"\r(?!\n)")


# ── Records ───────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class EntryStamp:
    """One `[verified: …]` token as written, and what could be read out of it.

    ``raw`` is the exact token, so a caller can show the user the text that was
    rejected rather than a normalized guess. ``date`` is set whenever the value
    is a real calendar date — including a future one, which is a real date and
    still not a verification. ``valid`` is the only field that says "this fact
    was checked on this day"; ``reason`` says why it was not.
    """

    raw: str
    date: datetime.date | None
    valid: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class NoteEntry:
    """One list item: its text, where it is, what it says, and who it is.

    ``text`` is the exact source slice ``original[start:end]`` — nothing
    trimmed, nothing normalized — so a caller can rewrite the note, quote the
    entry, or re-derive the fingerprint from the text alone. ``start`` /
    ``end`` are character offsets into :attr:`EntryDocument.original`;
    ``opening_start`` / ``opening_end`` bound the opening line, which is the
    only line a verification stamp is read from. ``line_number`` is the 0-based
    index of the opening line and exists for display only: it takes no part in
    ``identity`` and must never key state.

    ``stamp`` is the parsed verification stamp (or ``None``), ``verified`` is
    its date when the stamp is valid and ``None`` otherwise, and ``supported``
    is False for an entry whose own text carries a construct this entry model
    does not describe (a nested code block, heading, quote or table, a second
    blank-line-separated block, or nothing but a stamp). A later consumer
    answers differently about an unsupported entry, so it is reported rather
    than quietly treated as a plain fact.
    """

    text: str
    fingerprint: str
    identity: str
    section: str
    section_level: int
    kind: str
    marker: str
    indent: int
    ordinal: int
    line_number: int
    start: int
    end: int
    opening_start: int
    opening_end: int
    stamp: EntryStamp | None
    verified: datetime.date | None
    diagnostics: tuple[str, ...]
    supported: bool
    note_path: str = ""
    workspace: str = ""


@dataclass(frozen=True, slots=True)
class EntryDocument:
    """Everything one parse of one note produced.

    ``original`` is the text exactly as handed in — never rewritten, never
    re-encoded, BOM and CRLF included. ``entries`` are the facts in document
    order. ``uncovered`` is the exact complement of the entries, as
    ``(start, end)`` character offsets, so ``original[start:end]`` is the
    material this parse did not read as an entry: prose paragraphs, headings,
    tables, quotes, frontmatter, fences, and the blank lines between items.
    That is what makes a coverage claim checkable — an empty finding list over
    a note whose facts are all in a table is not a clean note, and this
    document says so.
    """

    original: str
    entries: tuple[NoteEntry, ...]
    uncovered: tuple[tuple[int, int], ...]
    diagnostics: tuple[str, ...]
    note_path: str = ""
    workspace: str = ""

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    @property
    def total_chars(self) -> int:
        return len(self.original)

    @property
    def entry_chars(self) -> int:
        """Characters covered by at least one entry."""
        return sum(entry.end - entry.start for entry in self.entries)

    @property
    def uncovered_chars(self) -> int:
        """Characters in no entry. Always ``total_chars - entry_chars``."""
        return sum(end - start for start, end in self.uncovered)

    @property
    def coverage_ratio(self) -> float:
        """Share of the file read as entries, ``0.0`` for an empty file."""
        if not self.original:
            return 0.0
        return self.entry_chars / len(self.original)


# ── Public surface ────────────────────────────────────────────────────────
def parse_note_entries(
    text: str,
    *,
    note_path: str = "",
    workspace: str = "",
    today: datetime.date | None = None,
) -> EntryDocument:
    """Parse one note's text into entries, coverage and diagnostics.

    ``note_path`` and ``workspace`` take no part in parsing; they are the
    identity coordinates, and they are stored on the document and on every
    entry so a caller never has to carry them separately. ``today`` exists so a
    caller (and a test) can decide what "future" means; it defaults to the real
    current date.
    """
    current_day = today or datetime.date.today()
    lines = _split_lines(text)

    diagnostics: list[str] = []
    if _BARE_CR_RE.search(text):
        diagnostics.append(DIAG_BARE_CR)

    entries: list[NoteEntry] = []
    ordinals: dict[tuple[str, str], int] = {}
    section = ""
    section_level = 0

    index = 0
    closing = _frontmatter_end(lines)
    if closing is not None:
        index = closing + 1
    elif lines and _is_frontmatter_opener(lines[0]):
        # An unclosed `---` is a horizontal rule, not a frontmatter block
        # nobody finished. Say so and keep reading the body: dropping every
        # line under a stray rule would hide the note's actual facts.
        diagnostics.append(DIAG_UNCLOSED_FRONTMATTER)

    # The line a setext underline would lift, if the current line turns out to
    # be one. Tracked rather than re-read as `lines[index - 1]` because the
    # line before a `---` is very often not a paragraph at all: it may have been
    # consumed by an entry, and then the rule is a rule, not a heading that
    # renames every later identity in the note under a wrapped bullet's last
    # line.
    base: str | None = None

    while index < len(lines):
        line = lines[index]
        kind = _classify_text(line.view, base) if not line.blank else _BLOCK_BLANK
        if kind == _BLOCK_BLANK:
            base = None
            index += 1
            continue
        if kind == _BLOCK_FENCE:
            closed_at = _fence_close(lines, index)
            base = None
            if closed_at is None:
                # Everything after an unterminated fence is code, by the same
                # rule that keeps a `- item` out of a closed fence.
                diagnostics.append(DIAG_UNTERMINATED_FENCE)
                index = len(lines)
            else:
                index = closed_at + 1
            continue
        if kind == _BLOCK_HEADING:
            # A heading up to three spaces in names a section. Deeper than
            # that it is an indented code block, which the walk never reaches
            # as a heading anyway, and a `#` inside a list item is that item's
            # text and never reaches here at all.
            section = _heading_text(line, base)
            section_level = _heading_level(line)
            base = None
            index += 1
            continue
        if kind == _BLOCK_ITEM:
            entry, next_index = _build_entry(
                lines,
                index,
                text,
                section=section,
                section_level=section_level,
                note_path=note_path,
                workspace=workspace,
                ordinals=ordinals,
                diagnostics=diagnostics,
                today=current_day,
            )
            index = next_index
            # The lines the entry consumed are not a paragraph a following
            # rule could lift, whatever they look like on their own.
            base = None
            if entry is not None:
                entries.append(entry)
            continue
        # A rule, a quote, a table or a paragraph line. Only the last of those
        # can be a setext base; the others are cleared above by their branch.
        base = line.view if kind == _BLOCK_PLAIN else None
        index += 1

    return EntryDocument(
        original=text,
        entries=tuple(entries),
        uncovered=_uncovered_spans(len(text), entries),
        diagnostics=tuple(diagnostics),
        note_path=note_path,
        workspace=workspace,
    )


def entry_identity(entry: NoteEntry) -> str:
    """The stable id of one entry: what it is, and where it lives.

    A digest over :data:`IDENTITY_VERSION`, the workspace, the note path, the
    nearest preceding heading, the entry's fingerprint and a duplicate
    occurrence ordinal. Deliberately absent: any line number, any offset, and
    the entry's text itself. Moving an entry down a note, or inserting
    unrelated content above it, leaves the identity untouched, while two
    identical bullets under one heading get different ordinals and so stay
    distinct. Nothing is read from disk to compute it — check-state storage
    belongs to a later change.
    """
    payload = "\x00".join(
        (
            IDENTITY_VERSION,
            entry.workspace,
            entry.note_path,
            entry.section,
            entry.fingerprint,
            str(entry.ordinal),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def refresh_fingerprint(text: str) -> str:
    """The fingerprint an entry's current source text has.

    Takes the *text*, not a parsed entry, because that is the only form in
    which the question is real: a caller holding a fact that has just been
    re-verified or re-worded needs the id the note will have after the edit,
    and a frozen :class:`NoteEntry` cannot be asked that question about its own
    new text. A re-stamp returns the same fingerprint the entry already carries,
    a prose change returns a different one, and the caller does not have to
    know which span the old stamp occupied — the opening line is re-read from
    the text given, so an entry whose offsets went stale with the note still
    fingerprints correctly.
    """
    return _fingerprint(text)


def parse_verification_stamp(
    text: str, *, today: datetime.date | None = None
) -> EntryStamp | None:
    """The trailing `[verified: …]` stamp on one line, or ``None``.

    ``None`` means the line carries no verification claim at all — which is the
    answer for a line carrying a learned-at `[YYYY-MM-DD]` or an
    `[as-of: YYYY-MM-DD]`, because those mean something else and are never
    reinterpreted here, and for a stamp-shaped token that is not trailing, which
    is prose about a date rather than a claim about the line. A token that is
    trailing and shaped like a stamp but unusable comes back with
    ``valid=False`` and a ``reason`` instead of being dropped, so the caller can
    tell the user which text to fix.
    """
    stamp, _span = _scan_stamp(text, today)
    return stamp


# ── Fingerprinting ────────────────────────────────────────────────────────
# The one shape this module treats as a verification claim: the exact tag
# spelling, a value of `YYYY-MM-DD`, and nothing else — and *trailing*, which
# is the word doing the work. A stamp in the middle of a bullet is prose about
# a date, not a claim about the bullet, so anchoring is what keeps
# `- [verified: 2026-01-01] fact` and a Markdown link whose label happens to be
# `[verified: 2026-01-01]` from being read as verifications.
_VERIFIED_STAMP_RE = re.compile(r"\[verified:[ \t]*(?P<value>[^\]\n]*)\][ \t]*$")

# A near-miss of the real tag, and only that: the tag spelling, then the
# separator, then something that starts like a date. The `(?<!\[)` keeps a
# wikilink (`[[verified plan]]`) out, the digit keeps ordinary prose out
# (`[verified-by-bob]`, `[verified plan]`), and the same trailing anchor plus
# the `(?![(\]])` guard keeps it from swallowing a link. Consulted only when
# the strict pattern misses, so a real stamp is never reported as a typo.
_VERIFIED_LOOSE_RE = re.compile(
    r"(?<!\[)\[verified[ \t]*:?[ \t]*\d[^\]\n]{0,32}\](?![(\]])[ \t]*$"
)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Any stamp-shaped token, wherever on the line. Only ever used to ask whether a
# *second* one exists before the trailing one this module reads — a looser
# pattern than the strict one on purpose, since the question is "is there
# another `[verified:` here at all", not "is it a usable stamp".
_VERIFIED_ANY_RE = re.compile(r"\[verified:[^\]\n]*\]")


def _stamp_line(line: str) -> str:
    """``line`` without a trailing ``\\r``, so a CRLF line still ends the stamp.

    A real caller hands this module the file's own bytes, and a CRLF note's
    opening line ends ``\\r`` before the newline. The carriage return is not
    part of the stamp and must not stop the anchor from matching; it stays in
    the text the fingerprint hashes.
    """
    return line[:-1] if line.endswith("\r") else line


def _strict_stamp_span(line: str) -> tuple[int, int] | None:
    """Span of the whitespace the stamp introduced plus the stamp token.

    The whitespace run immediately before the token is part of what the stamp
    added to the line, so removing it too is what makes `- fact  [verified:
    …]` and `- fact [verified: …]` the same fact. Nothing else on the line is
    touched. ``None`` when the line makes no trailing verification claim.
    """
    body = _stamp_line(line)
    match = _VERIFIED_STAMP_RE.search(body)
    if match is None:
        return None
    return _token_span(body, match)


def _token_span(body: str, match: "re.Match[str]") -> tuple[int, int]:
    """``match``'s span extended left over the whitespace run before it.

    The separator a stamp introduced belongs to the stamp, not to the words
    before it, so cutting the two together is what leaves the entry's own text
    identical whichever spelling the stamp had.
    """
    start = match.start()
    while start > 0 and body[start - 1] in " \t":
        start -= 1
    return start, match.end()


def strip_stamp_tokens(line: str) -> str:
    """This line with every verification-stamp token removed, and nothing else.

    The writer's counterpart to :func:`_strict_stamp_span`, and deliberately
    wider than it. That function answers "is this a claim?", which is a reading
    question: a well-formed *trailing* stamp is the entry's metadata and a
    ``[verified: …]`` token in the middle of a sentence is prose about a date,
    so the fingerprint removes one and keeps the other. A writer is not asking
    which token is the claim — it is replacing the claim, and anything else on
    the line that still reads as one is what :data:`DIAG_STAMP_DUPLICATE`
    reports, what changes the entry's fingerprint for a reason nobody asked
    about, and what leaves a line carrying two claims after a re-stamp.

    So every token the three patterns recognise is cut out, with the whitespace
    run in front of it: the trailing stamp (strict or near-miss spelling) and
    any ``[verified: …]`` token the line also carries further left. What is left
    is the entry's own words, which is what the fingerprint was computed over in
    the first place — so a re-stamp of a stamped entry and a stamp of an
    unstamped one differ only in the date.
    """
    body = _stamp_line(line)
    spans: list[tuple[int, int]] = []
    for pattern in (_VERIFIED_STAMP_RE, _VERIFIED_LOOSE_RE, _VERIFIED_ANY_RE):
        for match in pattern.finditer(body):
            start, end = _token_span(body, match)
            overlaps = any(
                start < other_end and other_start < end
                for other_start, other_end in spans
            )
            if overlaps:
                # The strict pattern and the any-token pattern both match a
                # trailing stamp; cutting it twice would eat the words between.
                continue
            spans.append((start, end))
    if not spans:
        return body
    kept: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        kept.append(body[cursor:start])
        cursor = end
    kept.append(body[cursor:])
    return "".join(kept)


def _fingerprint(text: str, span: tuple[int, int] | None = None) -> str:
    """sha256 of an entry's exact text with its verification stamp removed.

    The opening line is located by splitting, not by the entry's offsets, so
    this is the same function during parsing and from
    :func:`refresh_fingerprint` after an edit. ``span`` is that line's stamp
    span, passed in by a caller that already computed it, so the payload the
    fingerprint hashes and the payload the support check reads are the same
    slice and cannot drift apart. Newline bytes are whatever the file used: a
    CRLF entry hashes CRLF, and the two spellings of one note therefore do not
    collide. Cutting the span out of the raw line leaves its ``\\r`` in place.
    """
    opening, separator, rest = text.partition("\n")
    shift = 1 if opening.startswith(_BOM) else 0
    if span is None:
        span = _strict_stamp_span(opening[shift:])
    if span is None:
        payload = text
    else:
        start, end = span[0] + shift, span[1] + shift
        payload = opening[:start] + opening[end:] + separator + rest
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rejected_stamp(raw: str, reason: str) -> EntryStamp:
    """A stamp-shaped token that cannot be believed, and why."""
    return EntryStamp(raw=raw, date=None, valid=False, reason=reason)


def _scan_stamp(
    line: str, today: datetime.date | None
) -> tuple[EntryStamp | None, tuple[int, int] | None]:
    """The trailing stamp on a line, and the span the fingerprint removes.

    Returns ``(None, None)`` when the line makes no verification claim at all,
    which is the answer for a learned-at or ``[as-of:]`` tag and for a
    mid-line token — a bullet that merely mentions a date has not been
    verified. A claim it cannot believe comes back with ``valid=False`` and the
    reason, never as a guess.

    The span is returned rather than recomputed by the caller so the payload
    the fingerprint hashes and the payload the support check reads are the same
    slice by construction.

    A second stamp earlier on the line is *not* folded into this one: the
    trailing token is the entry's claim, and the earlier one is left in the
    text. :data:`DIAG_STAMP_DUPLICATE` is how a caller hears about it, because
    the alternative is a tool that appends a stamp instead of replacing one
    changing the fingerprint with nothing to say so.
    """
    body = _stamp_line(line)
    match = _VERIFIED_STAMP_RE.search(body)
    if match is None:
        loose = _VERIFIED_LOOSE_RE.search(body)
        if loose is None:
            return None, None
        return _rejected_stamp(loose.group(0), STAMP_REASON_MALFORMED), None
    span = _strict_stamp_span(body)
    value = match.group("value").strip()
    if not _ISO_DATE_RE.match(value):
        return _rejected_stamp(match.group(0), STAMP_REASON_MALFORMED), span
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError:
        # Right shape, not a real day (2025-02-30). Reported, never rounded.
        return _rejected_stamp(match.group(0), STAMP_REASON_IMPOSSIBLE), span
    if parsed > (today or datetime.date.today()):
        # A real date, but one that has not happened: nobody verified a fact
        # on a day that has not come.
        return (
            EntryStamp(match.group(0), parsed, False, STAMP_REASON_FUTURE),
            span,
        )
    return EntryStamp(match.group(0), parsed, True, ""), span


# ── Line model ────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class _Line:
    """One physical line and the offsets the document reports in.

    ``text`` is the line without its terminator; ``view`` is the same string
    with a byte-order mark removed from the first line, so the structure walk
    can see a frontmatter delimiter that a UTF-8 editor left behind. ``view``
    never changes an offset: the mark stays in ``text`` and in the document.
    """

    index: int
    start: int
    end: int
    full_end: int
    text: str
    view: str
    indent: int
    blank: bool


def _indent_width(line: str) -> int:
    """Display width of a leading whitespace run, tabs to four-column stops."""
    width = 0
    for char in line:
        if char == " ":
            width += 1
        elif char == "\t":
            width += _TAB_WIDTH - (width % _TAB_WIDTH)
        else:
            break
    return width


def _split_lines(text: str) -> list[_Line]:
    """Split on ``\\n`` only, keeping every byte's offset.

    A lone ``\\r`` does not split: old-Mac line endings are reported as
    :data:`DIAG_BARE_CR` rather than silently reinterpreted, and a
    ``\\r\\n`` terminator keeps its ``\\r`` inside the line's content so the
    fingerprint sees the file's own bytes.
    """
    lines: list[_Line] = []
    total = len(text)
    start = 0
    index = 0
    while start < total:
        newline_at = text.find("\n", start)
        if newline_at == -1:
            full_end = total
        else:
            full_end = newline_at + 1
        body = text[start:full_end]
        if body.endswith("\n"):
            body = body[:-1]
        if body.endswith("\r"):
            body = body[:-1]
        view = body[1:] if index == 0 and body.startswith(_BOM) else body
        lines.append(
            _Line(
                index=index,
                start=start,
                end=start + len(body),
                full_end=full_end,
                text=body,
                view=view,
                indent=_indent_width(view),
                blank=not view.strip(),
            )
        )
        start = full_end
        index += 1
    return lines


def _fence_match(line: str) -> re.Match[str] | None:
    match = _FENCE_RE.match(line)
    if match is None:
        return None
    fence = match.group("fence")
    if fence[0] == "`" and "`" in match.group("info"):
        # A backtick fence's info string may not contain a backtick, so a line
        # like ``` ```` ```` is prose that happens to start with three of them.
        return None
    return match


def _classify_text(view: str, previous: str | None) -> str:
    """What block the line ``view`` opens, given the line above it.

    Text-level so the same decision can be made about a line already sliced
    out of an entry, not only about a line still in the document walk. This is
    the only place a line's block kind is decided; the document walk and the
    support check both come through here, so the two cannot disagree about
    what a fence or a table is.
    """
    if _fence_match(view) is not None:
        return _BLOCK_FENCE
    if _ATX_HEADING_RE.match(view):
        return _BLOCK_HEADING
    # A setext underline is checked before a thematic break because CommonMark
    # gives the underline to the paragraph above it: `text` / `---` is a
    # heading, and only an underline with no paragraph under it is a rule.
    if _SETEXT_RE.match(view) and _is_setext_base(previous):
        return _BLOCK_HEADING
    if _THEMATIC_BREAK_RE.match(view):
        return _BLOCK_RULE
    if _BLOCKQUOTE_RE.match(view):
        return _BLOCK_QUOTE
    if _TABLE_ROW_RE.match(view) or _TABLE_DELIMITER_RE.match(view):
        return _BLOCK_TABLE
    if _LIST_ITEM_RE.match(view):
        return _BLOCK_ITEM
    return _BLOCK_PLAIN


def _is_setext_base(previous: str | None) -> bool:
    """True when ``previous`` is the paragraph line a setext underline lifts.

    Not a blank line, not indented, and not itself a block opener. The indent
    test is what stops an underline from lifting a bullet's *continuation*:
    `"## A" / "- one" / "  wrapped" / "---"` is a rule, and reading it as a
    heading would file every later entry in the note under a section named
    after a wrapped line. A line can only be a setext base if it classified as
    plain, so this is the plain test written out.
    """
    if previous is None or not previous.strip():
        return False
    if _indent_width(previous) > 0:
        return False
    return _classify_text(previous, None) == _BLOCK_PLAIN


def _opens_new_block(line: _Line, previous: _Line | None) -> bool:
    """True when this line ends an item's continuation run.

    A *top-level* construct ends the item: a heading, rule, quote, table or
    fence at the document's own indentation starts a new block, and folding it
    into the bullet above it would report a heading as part of a fact. A
    construct indented four or more spaces in is left inside the item, because
    there it cannot end the item's block — CommonMark puts it inside — and
    because dropping it would silently lose text from the note. An item that
    ended up holding one is reported as unsupported, so the caller sees it
    instead of trusting a fingerprint over a shape this module does not
    describe.
    """
    if line.indent > 3:
        return False
    return _classify_text(
        line.view, previous.view if previous is not None else None
    ) in _NEW_BLOCK_KINDS


def _looks_like_construct(view: str) -> bool:
    """True when an entry's own continuation line is a block marker.

    Indentation-insensitive on purpose. The line walk only lets a *top-level*
    construct end an item, so this is what catches one that stayed inside: a
    four-space `> quoted` under a bullet is a lazy continuation as far as the
    block structure goes and a blockquote as far as anyone reading the text
    goes. The entry is reported as unsupported rather than either reading
    being asserted.
    """
    return _classify_text(view.lstrip(), None) in _NEW_BLOCK_KINDS


def _dedented_fence(line: _Line) -> re.Match[str] | None:
    """A fence opener on ``line`` once its own indentation is dropped.

    A fenced block nested in a list item is indented past the three spaces
    CommonMark allows at the document level, so the document-level pattern
    cannot see it. Dropping the indentation is what makes a ``- item`` inside
    a code block under a bullet recognisable as code rather than as a fact.
    """
    return _fence_match(line.view.lstrip(" \t"))


def _fence_closes(opening: re.Match[str], line: _Line) -> bool:
    """True when ``line`` closes the fence ``opening`` started."""
    match = _dedented_fence(line)
    if match is None:
        return False
    fence = match.group("fence")
    if fence[0] != opening.group("fence")[0]:
        return False
    if len(fence) < len(opening.group("fence")):
        return False
    return not match.group("info").strip()


def _fence_close(lines: list[_Line], start: int) -> int | None:
    """Index of the line closing the fence opened at ``start``, or ``None``.

    The caller only reaches this for a line already classified as a fence, so
    there is no separate guard here: a `None` answer means the fence runs to
    the end of the note, which the caller reports.
    """
    opening = _fence_match(lines[start].view)
    if opening is None:  # pragma: no cover - the caller classified this line
        return None
    for index in range(start + 1, len(lines)):
        if _fence_closes(opening, lines[index]):
            return index
    return None


def _is_frontmatter_opener(line: _Line) -> bool:
    """True for a note's opening ``---``.

    Only ``---`` opens. A leading ``...`` is a closing delimiter, and treating
    it as an opener swallowed every bullet in a note that happened to start
    with one.
    """
    return line.index == 0 and line.view.rstrip() == "---"


def _frontmatter_end(lines: list[_Line]) -> int | None:
    """Index of the line closing a frontmatter block, or ``None``.

    A leading ``---`` only opens frontmatter if what follows it looks like
    frontmatter: the first non-blank line has to be a ``key:`` line, or the
    block has to be empty. Without that check a note that opens with a thematic
    break and closes with another one — an ordinary shape — had every bullet
    between them read as frontmatter and silently dropped. Refusing to guess is
    the whole point; ``None`` leaves the caller treating the opener as the rule
    it almost certainly is and reading the body.
    """
    if not lines or not _is_frontmatter_opener(lines[0]):
        return None
    for index in range(1, len(lines)):
        line = lines[index]
        if line.indent == 0 and line.view.rstrip() in _FRONTMATTER_DELIMITERS:
            return index
        if line.blank:
            continue
        if not _FRONTMATTER_KEY_RE.match(line.view):
            # Real content where a key was expected: this `---` was a rule.
            return None
    # An unterminated block is reported by the caller; the body is still read.
    return None


def _heading_level(line: _Line) -> int:
    """Level of a heading line: the number of `#`, or 1/2 for a setext pair."""
    if _ATX_HEADING_RE.match(line.view):
        # Measured on one string, so the count cannot be computed by comparing
        # a stripped line against an unstripped one.
        stripped = line.view.lstrip()
        return len(stripped) - len(stripped.lstrip("#"))
    return 1 if line.view.lstrip().startswith("=") else 2


def _heading_text(line: _Line, base: str | None) -> str:
    """The heading's own words, which is the section entries are filed under.

    CommonMark's rule, not a strip-everything rule: the leading `#` run and one
    following space go, and then only a *trailing* run of `#` preceded by
    whitespace is a closing sequence. `"## C#"` is about C#, `"# #tag"` is about
    a `#tag`, and `"## Title ##"` is about the title. Over-stripping turned
    both real subjects into different ones, and a section name is half an
    entry's identity.

    ``base`` is the paragraph line a setext underline lifts, and is ``None``
    when the line is not a setext heading.
    """
    if _ATX_HEADING_RE.match(line.view):
        stripped = line.view.strip().lstrip("#")
        if stripped.startswith((" ", "\t")):
            stripped = stripped[1:]
        return _ATX_CLOSING_RE.sub("", stripped).strip()
    return base.strip() if base is not None else ""


# ── Entry construction ────────────────────────────────────────────────────
def _item_marker(line: _Line) -> re.Match[str] | None:
    return _LIST_ITEM_RE.match(line.view)


def _content_indent(line: _Line, marker: re.Match[str]) -> int:
    """Column where the item's content begins, marker and gap included."""
    run = marker.group("indent")
    text = marker.group("marker")
    after = line.view[len(run) + len(text) :]
    return line.indent + len(text) + _indent_width(after)


def _entry_last_line(lines: list[_Line], start: int) -> int:
    """Index of the last line belonging to the item opened at ``start``.

    A fenced block nested in the item is part of the item, and every line of it
    is absorbed whole: a ``- item`` inside a code block is code, and testing it
    for a list marker or a new block would turn code into a fact. "Nested"
    means indented to the item's own *content column* — the column right after
    ``- `` — which is where a fenced code block belongs under a bullet, and not
    merely four or more spaces in: an item's content column is 2 for ``- x``
    and 3 for ``1. x``, and code written one space in is still the item's code.
    A fence shallower than that is a document-level block and ends the item.

    An unterminated nested fence therefore runs to the end of the item's
    indented run rather than to the end of the note — the lines after the item
    are the next block, not more of this one, and a sibling entry below an
    unclosed code block is a fact in its own right, not a casualty.
    """
    opening = lines[start]
    marker = _item_marker(opening)
    if marker is None:  # pragma: no cover - the caller classified this line
        return start
    marker_indent = opening.indent
    content_indent = _content_indent(opening, marker)
    last = start
    pending_blank = False
    previous: _Line | None = opening
    fence: re.Match[str] | None = None
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if fence is not None:
            # Inside a nested fence: nothing here is a block opener, and only
            # dedenting back to the marker column ends the item.
            if _fence_closes(fence, line):
                fence = None
            elif not line.blank and line.indent <= marker_indent:
                break
            last = index
            index += 1
            continue
        if line.blank:
            # A blank line does not end the item yet: a following block
            # indented into it is a second paragraph of the same fact.
            pending_blank = True
            index += 1
            continue
        # A fence at the item's content column is *inside* the item, and that
        # is the normal way to write a command under a bullet: the content
        # column of `- ` is 2, so the check has to run before
        # `_opens_new_block`, which would otherwise claim a fence indented one
        # to three spaces as a document-level block and end the item. Dropping
        # the code from the entry there costs more than it looks: editing a
        # command would not change the fact's identity, and an unterminated
        # fence would swallow the sibling entries below it. Shallower than the
        # content column it really is a document-level fence, and ends the item.
        if line.indent >= content_indent:
            nested = _dedented_fence(line)
            if nested is not None:
                fence = nested
                last = index
                previous = line
                index += 1
                continue
        if _opens_new_block(line, previous):
            break
        if _item_marker(line) is not None:
            # A child item, or the next sibling. Either way it is its own
            # entry, and the parent must not swallow it.
            break
        if line.indent <= marker_indent:
            break
        if pending_blank and line.indent < content_indent:
            # A blank line already closed the item's block; a line that only
            # clears the marker's own column is the next block, not a
            # continuation of this one.
            break
        last = index
        pending_blank = False
        previous = line
        index += 1
    return last


def _entry_support(text: str, content: str) -> tuple[bool, tuple[str, ...]]:
    """Whether this entry is a plain single-block fact, and why not if not.

    ``content`` is everything the entry says with the marker and the stamp
    removed — the opening line's words *and* its continuation lines — so a
    bullet whose words sit on a continuation line is a fact, not an empty
    stamp. Reported rather than silently accepted: an entry that carries a
    nested code block or a second block is text no consumer should read as one
    atomic claim, and a caller has to be able to see that before it decides
    what to do with the entry. Only the entry's own lines are examined — a
    nested child item is a different entry and cannot make its parent
    unsupported.
    """
    found: list[str] = []
    body = text.split("\n")[1:]
    if any(not line.strip() for line in body):
        # A blank line inside the entry's own text: the item is a loose list
        # item holding more than one block, and one fingerprint over all of it
        # is a fingerprint over a shape the entry model does not describe.
        found.append(DIAG_MULTI_BLOCK)
    for raw in body:
        if _looks_like_construct(raw):
            found.append(DIAG_NESTED_CONSTRUCT)
            break
    if not content.strip():
        found.append(DIAG_STAMP_ONLY)
    return not found, tuple(found)


def _build_entry(
    lines: list[_Line],
    start: int,
    original: str,
    *,
    section: str,
    section_level: int,
    note_path: str,
    workspace: str,
    ordinals: dict[tuple[str, str], int],
    diagnostics: list[str],
    today: datetime.date,
) -> tuple[NoteEntry | None, int]:
    """Assemble the entry opened at ``start``, and the index after it.

    Returns ``(None, next)`` for a bullet with nothing in it: an empty item is
    a formatting artefact, and a fact with no text has no fingerprint worth
    hashing. It is still reported, because a note full of empty bullets is a
    note whose facts are somewhere this parser did not look.
    """
    opening = lines[start]
    marker = _item_marker(opening)
    if marker is None:  # pragma: no cover - the caller classified this line
        return None, start + 1
    last = _entry_last_line(lines, start)
    marker_text = marker.group("marker")
    content_start = len(marker.group("indent")) + len(marker_text)

    start_offset = opening.start
    end_offset = lines[last].end
    text = original[start_offset:end_offset]

    # Whether the item asserts anything. The marker itself is not content, so a
    # bare `-` is an empty bullet however much whitespace follows it — and a
    # bullet that only carries a continuation line still says something, which
    # is why the whole span is checked rather than the opening line alone.
    opening_content = opening.view[content_start:]
    continued = any(
        lines[index].view.strip() for index in range(start + 1, last + 1)
    )
    if not opening_content.strip() and not continued:
        diagnostics.append(DIAG_EMPTY_LIST_ITEM)
        return None, last + 1

    found: list[str] = []
    stamp, span = _scan_stamp(opening.view, today)
    if stamp is not None:
        if not stamp.valid:
            found.append(_STAMP_DIAGNOSTICS[stamp.reason])
        if span is not None and _VERIFIED_ANY_RE.search(opening.view[: span[0]]):
            # An earlier stamp on the same line. Only the trailing one is this
            # entry's claim; the other is left in the text, so a tool that
            # appends rather than replaces changes the fingerprint — and this
            # is the diagnostic saying so instead of letting it pass quietly.
            found.append(DIAG_STAMP_DUPLICATE)

    # The stamp is metadata: it is removed from the fingerprint along with the
    # separator it introduced. `span` is the one span both this slice and the
    # fingerprint below are cut with, so what the support check calls the
    # entry's content and what the hash covers can never disagree.
    bare = (
        opening.view
        if span is None
        else opening.view[: span[0]] + opening.view[span[1] :]
    )
    content = "\n".join(
        [bare[content_start:]]
        + [lines[index].view for index in range(start + 1, last + 1)]
    )
    supported, support_diagnostics = _entry_support(text, content)
    found.extend(support_diagnostics)

    fingerprint = _fingerprint(text, span)
    key = (section, fingerprint)
    ordinal = ordinals.get(key, 0)
    ordinals[key] = ordinal + 1

    entry = NoteEntry(
        text=text,
        fingerprint=fingerprint,
        # Filled in from the finished entry below, since the digest covers
        # fields this constructor is still assembling.
        identity="",
        section=section,
        section_level=section_level,
        kind=KIND_ORDERED if marker_text[-1:] in (".", ")") else KIND_UNORDERED,
        marker=marker_text,
        indent=opening.indent,
        ordinal=ordinal,
        line_number=opening.index,
        start=start_offset,
        end=end_offset,
        opening_start=opening.start,
        opening_end=opening.end,
        stamp=stamp,
        verified=stamp.date if stamp is not None and stamp.valid else None,
        diagnostics=tuple(found),
        supported=supported,
        note_path=note_path,
        workspace=workspace,
    )
    return replace(entry, identity=entry_identity(entry)), last + 1


def _uncovered_spans(
    total: int, entries: list[NoteEntry]
) -> tuple[tuple[int, int], ...]:
    """The exact complement of the entry spans within ``original``.

    Entries are non-overlapping by construction (an item's text ends where the
    next item begins), so walking them in order and taking what falls between
    them is the whole remainder: prose, structure and blank lines alike. A
    caller that wants only the prose can filter the spans, but a caller that
    wants to be honest about coverage takes this one.
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for entry in sorted(entries, key=lambda item: item.start):
        if entry.start > cursor:
            spans.append((cursor, entry.start))
        cursor = max(cursor, entry.end)
    if cursor < total:
        spans.append((cursor, total))
    return tuple(spans)
