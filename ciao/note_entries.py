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
indentation, a closing fence with no info string, and CRLF.

**The verification stamp.** `[verified: YYYY-MM-DD]`, trailing on the entry's
*opening line*, is the only thing this module reads as a claim that someone
re-checked the fact. It is deliberately distinct from `[as-of: YYYY-MM-DD]`
(a snapshot of a changing world), from an event date in prose, and from the
bounded regions' learned-at `[YYYY-MM-DD]`; a learned or as-of stamp is never
reinterpreted as verification. The shape is strict — a real calendar date, or
nothing. An impossible date (`2025-02-30`), a malformed one (`26-1-1`) and a
future one each yield an `EntryStamp` with `valid=False`, a `reason`, and a
diagnostic, never a guess and never a raised error.

**Fingerprint.** sha256 over the entry's exact source text with the
verification stamp token removed, encoded as UTF-8 using the file's own
newline bytes. Nothing else is normalized: CRLF stays CRLF, trailing spaces
stay, and a byte-order mark stays. The stamp is metadata, so a pure re-stamp
of a fact changes nothing about what the fact says; editing the prose does.
The removed span is the stamp token plus the run of whitespace immediately
before it, which is exactly the separator the stamp introduced.

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

**It never raises on ordinary markdown.** Unterminated fences, unclosed
frontmatter, mixed markers, empty items and stray carriage returns are
diagnostics over identical text.
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

    while index < len(lines):
        line = lines[index]
        previous = lines[index - 1] if index else None
        kind = _classify(line, previous)
        if kind == _BLOCK_BLANK:
            index += 1
            continue
        if kind == _BLOCK_FENCE:
            closed_at = _fence_close(lines, index)
            if closed_at is None:
                # Everything after an unterminated fence is code, by the same
                # rule that keeps a `- item` out of a closed fence.
                diagnostics.append(DIAG_UNTERMINATED_FENCE)
                index = len(lines)
            else:
                index = closed_at + 1
            continue
        if kind == _BLOCK_HEADING:
            # Only a top-level heading names a section: a `#` inside a list
            # item is part of that item's text, not a heading the document's
            # entries are filed under.
            if line.indent == 0:
                section = _heading_text(line, previous)
                section_level = _heading_level(line)
            index += 1
            continue
        if kind in (_BLOCK_RULE, _BLOCK_QUOTE, _BLOCK_TABLE):
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
            if entry is not None:
                entries.append(entry)
            continue
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


def refresh_fingerprint(entry: NoteEntry) -> str:
    """Recompute an entry's fingerprint from its current text.

    Useful after an edit the caller made elsewhere: a re-stamp returns the same
    value the entry already carries, a prose change returns a different one, and
    the caller does not have to know which span the old stamp occupied. The
    opening line is re-read from the text, so an entry whose offsets have gone
    stale (because the note was edited) still fingerprints correctly.
    """
    return _fingerprint(entry.text)


def parse_verification_stamp(
    text: str, *, today: datetime.date | None = None
) -> EntryStamp | None:
    """The trailing `[verified: …]` stamp on one line, or ``None``.

    ``None`` means the line carries no verification claim at all — which is the
    answer for a line carrying a learned-at `[YYYY-MM-DD]` or an
    `[as-of: YYYY-MM-DD]`, because those mean something else and are never
    reinterpreted here. A token that is shaped like a stamp but unusable comes
    back with ``valid=False`` and a ``reason`` instead of being dropped, so the
    caller can tell the user which text to fix.
    """
    stamp, _count = _scan_stamp(text, today)
    return stamp


# ── Fingerprinting ────────────────────────────────────────────────────────
# The one shape this module treats as a verification claim: the exact tag
# spelling, a value of `YYYY-MM-DD`, and nothing else. `_VERIFIED_LOOSE_RE`
# only exists so a near-miss (`[verified 2026-01-01]`) is reported rather than
# passed over in silence; it is consulted only when the strict pattern misses.
_VERIFIED_STAMP_RE = re.compile(r"\[verified:[ \t]*(?P<value>[^\]\n]*)\]")
_VERIFIED_LOOSE_RE = re.compile(r"\[verified[^\]\n]{0,64}\](?!\()")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _strict_stamp_span(line: str) -> tuple[int, int] | None:
    """Span of the whitespace the stamp introduced plus the stamp token.

    The whitespace run immediately before the token is part of what the stamp
    added to the line, so removing it too is what makes `- fact  [verified:
    …]` and `- fact [verified: …]` the same fact. Nothing else on the line is
    touched.
    """
    matches = list(_VERIFIED_STAMP_RE.finditer(line))
    if not matches:
        return None
    match = matches[-1]
    start = match.start()
    while start > 0 and line[start - 1] in " \t":
        start -= 1
    return start, match.end()


def _fingerprint(text: str) -> str:
    """sha256 of an entry's exact text with its verification stamp removed.

    The opening line is located by splitting, not by the entry's offsets, so
    this is the same function during parsing and from
    :func:`refresh_fingerprint` after an edit. Newline bytes are whatever the
    file used: a CRLF entry hashes CRLF, and the two spellings of one note
    therefore do not collide.
    """
    opening, separator, rest = text.partition("\n")
    span = _strict_stamp_span(opening)
    if span is None:
        payload = text
    else:
        start, end = span
        payload = opening[:start] + opening[end:] + separator + rest
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rejected_stamp(raw: str, reason: str) -> EntryStamp:
    """A stamp-shaped token that cannot be believed, and why."""
    return EntryStamp(raw=raw, date=None, valid=False, reason=reason)


def _scan_stamp(
    line: str, today: datetime.date | None
) -> tuple[EntryStamp | None, int]:
    """The stamp on a line, plus how many strict stamps it carried.

    Returns ``(None, 0)`` when the line makes no verification claim at all,
    which is the answer for a learned-at or ``[as-of:]`` tag. A claim it cannot
    believe comes back with ``valid=False`` and the reason, never as a guess.
    """
    matches = list(_VERIFIED_STAMP_RE.finditer(line))
    if not matches:
        loose = _VERIFIED_LOOSE_RE.search(line)
        if loose is None:
            return None, 0
        return (
            _rejected_stamp(loose.group(0), STAMP_REASON_MALFORMED),
            0,
        )
    # The last stamp is the most recent claim, matching how the bounded-region
    # learned stamp is read; an earlier one on the same line is a duplicate the
    # caller is told about.
    match = matches[-1]
    raw = match.group(0)
    count = len(matches)
    value = match.group("value").strip()
    if not _ISO_DATE_RE.match(value):
        return _rejected_stamp(raw, STAMP_REASON_MALFORMED), count
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError:
        # Right shape, not a real day (2025-02-30). Reported, never rounded.
        return _rejected_stamp(raw, STAMP_REASON_IMPOSSIBLE), count
    if parsed > (today or datetime.date.today()):
        # A real date, but one that has not happened: nobody verified a fact
        # on a day that has not come.
        return EntryStamp(raw, parsed, False, STAMP_REASON_FUTURE), count
    return EntryStamp(raw, parsed, True, ""), count


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

    Not a blank line, not indented as code, and not itself a block opener: a
    rule under a bullet is a rule, not a heading that turns the bullet's
    heading text into a section name. A line can only be a setext base if it
    classified as plain, so this is the plain test written out.
    """
    if previous is None or not previous.strip():
        return False
    if _indent_width(previous) > 3:
        return False
    return _classify_text(previous, None) == _BLOCK_PLAIN


def _classify(line: _Line, previous: _Line | None) -> str:
    """What block this line opens. Blank first, so callers can skip cheaply."""
    if line.blank:
        return _BLOCK_BLANK
    return _classify_text(line.view, previous.view if previous is not None else None)


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


def _fence_close(lines: list[_Line], start: int) -> int | None:
    """Index of the line closing the fence opened at ``start``, or ``None``.

    The caller only reaches this for a line already classified as a fence, so
    there is no separate guard here: a `None` answer means the fence runs to
    the end of the note, which the caller reports.
    """
    opening = _fence_match(lines[start].view)
    if opening is None:  # pragma: no cover - the caller classified this line
        return None
    char = opening.group("fence")[0]
    length = len(opening.group("fence"))
    for index in range(start + 1, len(lines)):
        match = _fence_match(lines[index].view)
        if match is None:
            continue
        fence = match.group("fence")
        if fence[0] != char or len(fence) < length:
            continue
        if match.group("info").strip():
            # A closing fence carries no info string.
            continue
        return index
    return None


def _is_frontmatter_opener(line: _Line) -> bool:
    return line.index == 0 and line.view.rstrip() in _FRONTMATTER_DELIMITERS


def _frontmatter_end(lines: list[_Line]) -> int | None:
    """Index of the line closing a frontmatter block, or ``None``."""
    if not lines or not _is_frontmatter_opener(lines[0]):
        return None
    for index in range(1, len(lines)):
        line = lines[index]
        if line.indent == 0 and line.view.rstrip() in _FRONTMATTER_DELIMITERS:
            return index
    return None


def _heading_level(line: _Line) -> int:
    marks = _ATX_HEADING_RE.match(line.view)
    if marks is not None:
        return len(line.view.lstrip()) - len(line.view.lstrip("#"))
    return 1 if line.view.lstrip().startswith("=") else 2


def _heading_text(line: _Line, previous: _Line | None) -> str:
    if _ATX_HEADING_RE.match(line.view):
        stripped = line.view.strip()
        return stripped.lstrip("#").strip().strip("#").strip()
    return previous.text.strip() if previous is not None else ""


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
    """Index of the last line belonging to the item opened at ``start``."""
    opening = lines[start]
    marker = _item_marker(opening)
    if marker is None:  # pragma: no cover - the caller classified this line
        return start
    marker_indent = opening.indent
    content_indent = _content_indent(opening, marker)
    last = start
    pending_blank = False
    previous: _Line | None = opening
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if line.blank:
            # A blank line does not end the item yet: a following block
            # indented into it is a second paragraph of the same fact.
            pending_blank = True
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

    Reported instead of silently accepted: an entry that carries a nested code
    block or a second block is text no consumer should read as one atomic
    claim, and a caller has to be able to see that before it decides what to do
    with the entry. Only the entry's own lines are examined — a nested child
    item is a different entry and cannot make its parent unsupported.
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
    stamp, count = _scan_stamp(opening.view, today)
    if stamp is not None:
        if not stamp.valid:
            found.append(_STAMP_DIAGNOSTICS[stamp.reason])
        if count > 1:
            found.append(DIAG_STAMP_DUPLICATE)

    # The stamp is metadata: it is removed from the fingerprint along with the
    # separator it introduced. When what remains of the opening line is empty,
    # the bullet asserts nothing, which is a different fact from a claim whose
    # verification has expired.
    span = _strict_stamp_span(opening.view)
    bare = (
        opening.view
        if span is None
        else opening.view[: span[0]] + opening.view[span[1] :]
    )
    supported, support_diagnostics = _entry_support(text, bare[content_start:])
    found.extend(support_diagnostics)

    fingerprint = _fingerprint(text)
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
