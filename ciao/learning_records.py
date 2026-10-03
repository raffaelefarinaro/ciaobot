"""Stable learning records and lossless parsing of ``Workspace/Learnings.md``.

Why this exists
---------------
``ciao/memory_proposals.py`` used to own the one regex that reads a learnings
entry, mint a four-word key from the statement, truncate the cited sources to
eight, and count recurrence by comparing normalized prose, and
``ciao/curation_run.py`` imported that same regex to decide what to promote.
That was the shape a retry grew: re-filing the same fact twice inflated a
count, rewording a statement minted a different key, and a promoted entry could
not be traced back to the episode that produced it because the prose itself was
the only identity there is. Both readers are gone; this model is what reads a
learnings document now, and the private writer that shadowed it was deleted.

This module is the pure half of that. It defines what a learning *is* — a
record with a stable identifier, a statement, honest recurrence bookkeeping and
a deduplicated set of observations — and it reads and writes the
``Workspace/Learnings.md`` shapes that already exist in installed vaults
without losing a byte of anything it does not understand.

Nothing here touches disk, the network, or a model. :func:`migrate_learnings`
returns text; a caller decides whether to write it. The production writer
(``memory_proposals.append_learning``), the curation worklist
(``curation_run._learning_items``) and the migration command
(``ciao learnings-migrate``) are all callers of this module, not copies of it.

The four rules everything else follows from
-------------------------------------------

**Identity is persisted, not derived.** A canonical entry carries its
identifier in a machine-readable HTML comment on the line
(``<!-- ciao:learning {...} -->``). Rewording the statement on that line keeps
the identifier, because the identifier is read from the comment and never
regenerated from the text. A legacy entry has no comment yet, so
:func:`parse_learnings` mints one deterministically: ``uuid5`` over a frozen
namespace, the workspace, the *exact* original entry text, and the ordinal of
that text among identical duplicates in the same document. Two identical
legacy bullets therefore get two different identifiers and stay two entries;
the same file migrated twice produces byte-identical output. The workspace is
part of the name, so ``work`` and ``work-2`` — or ``work`` and ``personal`` —
never collide, and a stored identifier is never recomputed from a reworded
statement.

**Unknown stays unknown.** A legacy plain bullet has no recurrence history and
a date-only entry has no sources, so neither gets an ``x1``, a date of today,
or a chat id invented to fill a field. They render with the explicit canonical
tokens ``unknown`` and ``?``. Malformed dates, inverted date ranges,
non-positive counts and broken metadata are *diagnosed*, not guessed at: the
entry keeps its exact bytes and carries no record, because a consumer that
silently repaired an unparseable line would be making a decision nobody made.

**Evidence, not prose, is recurrence.** :func:`observe_learning` deduplicates
on an observation's *identity* — its source, its turn, or its user-request id
— and never on the excerpt wording, so a retry that re-quotes the episode
differently is still one observation. An observation with no stable identity is
refused outright and the record comes back untouched, because a source-less
retry cannot be told apart from a new sighting.

A migrated ``xN`` is kept as ``baseline_count`` and its cited sources seed the
seen set, so replaying one of those sources is a no-op instead of a second
sighting: the baseline already counted it. That is the rule that keeps
``x3, sources: a, b`` at three rather than drifting to five, and it holds for
every replay, however differently the retry phrased the excerpt.
``len(record.observations)`` is therefore the *separately known*
unique-observation tally, and a worklist that wants real ``x3`` evidence reads
it rather than ``count``, which may include history nobody can attribute. The
human citation on the line is capped at eight entries; the observations in the
comment never are.

**An unreadable byte is still the owner's byte.** :func:`migrate_learnings`
rewrites only the spans of recognized Active entries. Frontmatter, fenced code,
format notes, the ``## Promoted / Resolved`` section, every other byte and the
line endings stay exactly as they were — CRLF files stay CRLF, a BOM stays a
BOM. A shape it cannot read yields a diagnostic and the identical input, so a
failed migration is detectable and a successful one is reviewable.

The comment is the only way machine data reaches the file, so it has to survive
hostile prose. The payload is compact sorted JSON with ``<`` and ``>`` escaped
to ``\\u003c`` / ``\\u003e``, which makes it impossible for a statement or a
source id to open or close an HTML comment. The visible line keeps the shape
the existing writer emits, so a migrated entry reads the way its author wrote
it and an installed vault's surrounding lines are untouched.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from ciao.memory_receipts import content_revision

# ── Constants ──────────────────────────────────────────────────────────────

SCHEMA_VERSION = 1
"""Version stamped into every ``ciao:learning`` comment.

An integer, because the payload is JSON and a number is the one token a reader
can compare without first deciding what a missing key meant. A payload carrying
any other value is a schema this code does not implement, and is reported
rather than coerced.
"""

METADATA_MARKER = "<!-- ciao:learning "
"""Opening of the machine-readable comment appended to a canonical entry."""

METADATA_SUFFIX = " -->"
"""Closing of that comment, including its trailing space."""

BOM = "\ufeff"
"""A UTF-8 byte-order mark, which is a file property and never content."""

UNKNOWN_DATE = "unknown"
UNKNOWN_COUNT = "?"

MAX_DISPLAY_SOURCES = 8
"""Citations shown in the human-readable line.

Display only. The observations in the comment are never truncated, so a ninth
sighting is invisible on the line and fully present in the record. The cap is
the existing writer's, kept so a migrated line looks like the lines around it.
"""

KEY_WORDS = 4
"""Distinctive words an unkeyed statement contributes to its generated key."""

FALLBACK_KEY = "learning"

ID_NAMESPACE = uuid.UUID("77f44126-f9fe-5db0-b2b5-6c27dd6be632")
"""Frozen namespace for every identifier this module mints.

Allocated once, from the DNS namespace under the name
``ciaobot.learning-records/v1``, and never regenerated: changing it would
re-identify every learning in every installed vault. It is written out
literally so a reader can verify it instead of trusting a comment.
"""

ID_NAME_VERSION = "ciaobot/learning-record/v1"
"""First line of the ``uuid5`` name, so the derivation is versioned."""

SECTION_ACTIVE = "active"
SECTION_PROMOTED = "promoted"

SECTION_HEADINGS = {
    SECTION_ACTIVE: "Active",
    SECTION_PROMOTED: "Promoted / Resolved",
}
"""The heading text each recognized section is written under.

Public because the writer creates the ``## Active`` section when a file has
none, and a heading spelled here rather than in the caller is what keeps the
section the writer opens and the section the parser reads the same one.
"""

FORMAT_CANONICAL = "canonical"
"""A current ``- [key] [first → last] (xN) statement`` line."""

FORMAT_LEGACY = "legacy"
"""A historical ``- [date] category: statement — confidence: …`` line."""

FORMAT_PLAIN = "plain"
"""A bare Active bullet carrying no structure at all."""

FORMAT_MALFORMED = "malformed"
"""A recognized shape whose contents do not hold up. Bytes are kept as-is."""

FORMAT_CONFLICT = "conflict"
"""A canonical line whose stored identifier another line in this document already used."""

LEARNINGS_RELATIVE = "Workspace/Learnings.md"
"""Where a learnings document lives inside a vault, as a vault-relative path."""

LEARNINGS_STUB = (
    "---\n"
    "tags: [ciao, learnings]\n"
    "---\n"
    "# Learnings\n\n"
    "Reusable cross-project knowledge. Active entries are candidates "
    "for promotion into canonical guidance once they recur (x3 or "
    "more).\n"
)
"""The body a first write starts from.

Owned here rather than by the writer because the writer, the migration command
and the reader all need it to be the same text: two stubs would mean the first
:func:`ciao.memory_proposals.append_learning` into one vault and the first read
of another disagree about what an empty learnings file says.
"""


# ── The record ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LearningObservation:
    """One sighting of a learning, identified by where it was seen.

    ``source`` is the chat or archive identifier. ``turn`` narrows it to one
    exchange inside that source. ``request`` is the user-request identifier
    for an attended origin, used when the sighting belongs to a request rather
    than to a stored transcript.

    A request id identifies on its own, and a source without a turn identifies
    the source as a whole. :attr:`identity` is what :func:`observe_learning`
    deduplicates on, and it deliberately excludes :attr:`excerpt` — the excerpt
    is what a human reads when deciding whether two sightings are the same
    evidence, and a retry that re-quotes the episode with different wording
    must not become a second sighting. Nothing here is invented: an observation
    with neither a ``source`` nor a ``request`` has no stable identity at all,
    and is refused rather than accepted on the strength of its prose.
    """

    source: str = ""
    turn: int | None = None
    request: str = ""
    excerpt: str = ""

    @property
    def identity(self) -> tuple[str, ...] | None:
        """The stable identity of this sighting, or ``None`` when it has none."""
        if self.request:
            return ("request", self.request)
        if self.source:
            return ("source", self.source, "" if self.turn is None else str(self.turn))
        return None

    @property
    def citation(self) -> str:
        """The human form of this sighting, as it appears after ``— sources:``."""
        if self.source and self.turn is not None:
            return f"{self.source}#{self.turn}"
        if self.source:
            return self.source
        return f"req:{self.request}"

    def to_dict(self) -> dict[str, Any]:
        """The compact mapping stored in the comment; absent fields are omitted."""
        raw: dict[str, Any] = {}
        if self.source:
            raw["source"] = self.source
        if self.turn is not None:
            raw["turn"] = self.turn
        if self.request:
            raw["request"] = self.request
        if self.excerpt:
            raw["excerpt"] = self.excerpt
        return raw


@dataclass(frozen=True, slots=True)
class LearningRecord:
    """One learning, as this module understands it.

    ``learning_id`` is the immutable, workspace-scoped identity. ``key`` and
    ``text`` are display: the key is read from the line when there is one and
    generated from the first distinctive words otherwise, so both move when the
    statement is reworded while the identity does not.

    ``first_seen`` / ``last_seen`` are calendar dates or ``None``. ``count`` is
    the recurrence the line shows, or ``None`` when the history is unknown — a
    plain or date-only legacy bullet has never had a count and does not get an
    invented ``x1``. ``baseline_count`` is the ``xN`` a migrated canonical line
    already accounted for, kept separate so :func:`observe_learning` can add a
    genuinely new sighting without ever re-adding one the baseline covers.

    ``observations`` is the deduplicated evidence and ``aliases`` the
    identifiers of records merged into this one, retained for a later consumer
    to resolve. ``legacy`` holds what an older shape carried that is not
    representable as an observation — a category, a confidence, a source that
    was prose rather than an identifier — as sorted ``(name, value)`` pairs so
    the record stays frozen and hashable. Merging is a later decision; this
    only refuses to drop what it read.
    """

    learning_id: str
    key: str
    text: str
    first_seen: date | None = None
    last_seen: date | None = None
    count: int | None = None
    observations: tuple[LearningObservation, ...] = ()
    baseline_count: int | None = None
    aliases: tuple[str, ...] = ()
    legacy: tuple[tuple[str, str], ...] = ()

    @property
    def legacy_fields(self) -> dict[str, str]:
        """The retained legacy provenance as a plain mapping."""
        return dict(self.legacy)

    @property
    def observed_count(self) -> int:
        """How many distinct sightings are actually known.

        The recurrence on the line may include history nobody can attribute to
        a source; this is the count of evidence that exists. Worklist code that
        wants real ``x3`` evidence reads this rather than ``count``.
        """
        return len(self.observations)


@dataclass(frozen=True, slots=True)
class LearningEntry:
    """One list item found in a learnings document, and what it turned out to be.

    ``source_text`` and ``start`` / ``end`` are the exact bytes of the line and
    the character offsets they occupy in the document the entries came from,
    with ``end`` excluding the line terminator. A caller rewriting an entry can
    splice ``source_text[start:end]`` without re-finding it, and a caller that
    changes nothing still has a byte-exact account of what it saw.

    ``record`` is ``None`` when the line could not be read — an unparseable
    date, an inverted range, a count of zero, broken metadata, or an identifier
    this document already used. ``diagnostics`` says which.
    """

    source_text: str
    start: int
    end: int
    section: str
    format: str
    record: LearningRecord | None = None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LearningDocument:
    """A learnings file as :func:`parse_learnings` read it.

    ``text`` is the document exactly as given, ``entries`` only the list items
    that sat inside a recognized section, and ``diagnostics`` every problem
    found, in document order.
    """

    text: str
    entries: tuple[LearningEntry, ...] = ()
    diagnostics: tuple[str, ...] = ()


class _MetadataError(ValueError):
    """A ``ciao:learning`` comment that does not satisfy the schema."""


# ── Line scanning ──────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^ {0,3}(?P<hashes>#{1,6}) +(.*?)(?: +#+)? *$")
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")
_BULLET_RE = re.compile(r"^ {0,3}(?:[-*+]) +(?P<body>\S.*)$")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATE_ATTEMPT_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]*$")

# The two structured shapes are matched with the writer's own `-` marker, not
# with any bullet marker: `* [2024-05-01] …` is prose someone typed, and reading
# it as a legacy entry would invent a first-seen date the owner never wrote.
_LEGACY_RE = re.compile(r"^ {0,3}-+ +\[(?P<date>[^\]]+)\] *(?P<body>.*)$")
_CANONICAL_RE = re.compile(
    r"^ {0,3}-+ +\[(?P<key>[^\[\]]+)\] +"
    r"\[(?P<first>\S+) *(?:→|->) *(?P<last>\S+)\] +"
    r"\((?P<count>[^)]*)\) +"
    r"(?P<body>\S.*)$"
)
_SOURCES_RE = re.compile(r" *(?:—|–|--) +sources: *")
_CONFIDENCE_RE = re.compile(r" *(?:—|–|--) +confidence: *(?P<confidence>.*)$")

# A citation the writer emitted for a source that has a turn, and one it
# emitted for a user request. Both are display spellings of an identity that is
# stored explicitly, so reading them back has to land on the same observation
# rather than on a source id that happens to contain the same characters.
_TURN_CITATION_RE = re.compile(r"^(?P<source>.+)#(?P<turn>[1-9]\d*)$")
_REQUEST_CITATION_RE = re.compile(r"^req:(?P<request>.+)$")

_SECTION_LABELS = dict(SECTION_HEADINGS)


@dataclass(frozen=True, slots=True)
class _Line:
    """One physical line, with the context needed to decide if it is an entry."""

    start: int
    end: int
    number: int
    section: str
    parseable: bool


def _line_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every line's content, excluding its terminator.

    ``\\n`` terminates a line and the ``\\r`` of a ``\\r\\n`` pair belongs to the
    terminator rather than the content. Without that split a CRLF file's every
    line ends in a carriage return a strict date or key match would reject, and
    the migration would have to invent a reason to keep it.
    """
    spans: list[tuple[int, int]] = []
    position = 0
    length = len(text)
    while position < length:
        newline = text.find("\n", position)
        end = length if newline == -1 else newline
        if end > position and text[end - 1] == "\r":
            end -= 1
        spans.append((position, end))
        position = length if newline == -1 else newline + 1
    return spans


def _section_of(title: str) -> str:
    """The recognized section a heading opens, or ``""`` for anything else."""
    normalized = " ".join(title.split()).strip().lower()
    if normalized == "active":
        return SECTION_ACTIVE
    # `## Promoted / Resolved` is what the curation skill writes and what
    # `curation_run` partitions on. Both words are accepted so a workspace
    # that split the two headings is still read; every other heading — format
    # notes and worked examples included — is content this module leaves alone.
    if normalized.startswith("promoted") or normalized.startswith("resolved"):
        return SECTION_PROMOTED
    return ""


def _scan(text: str) -> list[_Line]:
    """Index every line with its section, skipping frontmatter and code fences.

    A fenced block is tracked document-wide rather than per section: a fence
    opened inside a section and closed outside it is malformed, and reading the
    rest of the file as code is the interpretation that invents no entries. A
    frontmatter block that never closes swallows the remainder of the file,
    which is what an unterminated frontmatter means.

    Only a heading at level two or above opens or closes a section; a deeper
    heading keeps the section it sits in. ``### Work`` under ``## Active`` is
    a group inside the active list, and treating it as the end of the section
    would skip every bullet under it — no entry, no record, and no diagnostic,
    because nothing about those lines is wrong.
    """
    lines: list[_Line] = []
    section = ""
    fence = ""
    frontmatter = "before"
    # A byte-order mark is not content. It stays untouched in the returned
    # offsets; it is only stepped over so a BOM cannot hide the frontmatter
    # fence from the scan and turn the whole file into one block of metadata.
    bom = 1 if text.startswith(BOM) else 0

    for index, (start, end) in enumerate(_line_spans(text)):
        content = text[start:end]
        if index == 0 and bom:
            content = content[bom:]
        number = index + 1

        if frontmatter == "before":
            frontmatter = "inside" if content.strip() == "---" else "skipped"
            if frontmatter == "inside":
                lines.append(_Line(start, end, number, "", False))
                continue
        if frontmatter == "inside":
            if content.strip() in {"---", "..."}:
                frontmatter = "done"
            lines.append(_Line(start, end, number, "", False))
            continue

        marker = _FENCE_RE.match(content)
        if marker is not None:
            token = marker.group("fence")
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            lines.append(_Line(start, end, number, section, False))
            continue
        if fence:
            lines.append(_Line(start, end, number, section, False))
            continue

        heading = _HEADING_RE.match(content)
        if heading is not None:
            if len(heading.group("hashes")) <= 2:
                section = _section_of(heading.group(2))
            lines.append(_Line(start, end, number, section, False))
            continue
        lines.append(_Line(start, end, number, section, True))
    return lines


# ── Text helpers ───────────────────────────────────────────────────────────


def _flatten(value: str) -> str:
    """Collapse every whitespace run into a single space.

    Learnings are line-oriented Markdown: one bullet is one line. A statement
    carrying a newline would otherwise split into a truncated bullet plus a
    continuation this parser reads as its own entry, and a metadata comment
    carrying one would break out of its own line.
    """
    return " ".join(value.split())


def _parse_date(token: str) -> date | None:
    """A real calendar date, or ``None``.

    The dashed ISO form is required explicitly: :meth:`date.fromisoformat`
    accepts the basic ``20240105`` form on Python 3.11+, and a learnings line
    that said ``[20240101 → 20240201]`` did not say what its writer meant.
    """
    if not _ISO_DATE_RE.fullmatch(token):
        return None
    try:
        return date.fromisoformat(token)
    except ValueError:
        return None


def _split_metadata(line: str) -> tuple[str, str | None]:
    """Split a line into its visible text and its metadata payload, if any.

    The *last* opening marker on the line wins, so a statement that itself
    contains ``<!-- ciao:learning … -->`` is read as prose while the real
    comment is still found. A marker not closed at the end of the line is not a
    comment either, which is what lets a statement carrying the literal text of
    one round-trip as prose instead of being mistaken for machine data.

    Whitespace after the closing ``-->`` is tolerated. Padding there is what an
    editor or a hand edit leaves behind, and a line is still a line when it
    ends in a space; reading it as prose would fold the comment into the
    statement, mint a new identifier for a learning that already has one, and
    demote its evidence on the very next write.
    """
    stripped = line.rstrip()
    index = stripped.rfind(METADATA_MARKER)
    if index == -1 or not stripped.endswith(METADATA_SUFFIX):
        return line, None
    return stripped[:index].rstrip(), stripped[
        index + len(METADATA_MARKER) : -len(METADATA_SUFFIX)
    ]


def _strip_sources(body: str) -> tuple[str, str]:
    """Separate the writer's trailing ``— sources: …`` clause from the statement.

    Only the *last* clause opener counts. A statement is free to contain those
    words — ``use foo — sources: bar, baz now`` is a sentence someone wrote —
    and :func:`render_learning` always writes the citation clause last, so the
    final opener is the one that belongs to the record. Splitting on the first
    would silently shorten the statement and move the rest of the sentence into
    the citations, which changes what the learning says without any diagnostic.
    """
    matches = list(_SOURCES_RE.finditer(body))
    if not matches:
        return body, ""
    clause = matches[-1]
    return body[: clause.start()], body[clause.end() :]


def _split_citations(body: str, metadata: _Metadata | None) -> tuple[str, str]:
    """The statement and its citation list, told apart by the stored record.

    :func:`render_learning` writes a citation clause only when the record
    carries observations, so a line whose comment lists none has no clause: the
    ``— sources:`` phrases on it are part of what the owner wrote, and
    splitting there would shorten the statement on every read. A line with no
    comment is the pre-existing shape, where a trailing clause is the only
    thing that can be a citation, and the last opener is the best reading of it.
    """
    body = body.rstrip()
    if metadata is not None and not metadata.observations:
        return body, ""
    return _strip_sources(body)


def _where(line: _Line) -> str:
    """A stable prefix for a diagnostic, naming the section and the line."""
    return f"{_SECTION_LABELS.get(line.section, 'Unsectioned')} line {line.number}"


def _display_date(
    token: str, label: str, where: str, problems: list[str]
) -> date | None:
    """One end of a canonical date range, honouring the ``unknown`` token.

    ``unknown`` is a date this entry does not have, not a malformed one: it is
    what a legacy bullet with no history renders to, and reading it back has to
    land on the same ``None`` rather than report a line this module wrote as
    broken.
    """
    if token == UNKNOWN_DATE:
        return None
    parsed = _parse_date(token)
    if parsed is None:
        problems.append(
            f"{where}: {label} {token!r} is neither a real date nor {UNKNOWN_DATE!r}"
        )
    return parsed


# ── Metadata encoding ──────────────────────────────────────────────────────


def _metadata_payload(record: LearningRecord) -> dict[str, Any]:
    """The mapping a record serializes to, legacy fields flattened to strings."""
    payload: dict[str, Any] = {
        "aliases": [_flatten(alias) for alias in record.aliases],
        "baseline": record.baseline_count,
        "id": record.learning_id,
        "observations": [observation.to_dict() for observation in record.observations],
        "schema": SCHEMA_VERSION,
    }
    if record.legacy:
        payload["legacy"] = {name: _flatten(value) for name, value in record.legacy}
    return payload


def _metadata_json(record: LearningRecord) -> str:
    """The comment payload: compact, key-sorted, and unable to escape its comment.

    ``<`` and ``>`` become ``\\u003c`` and ``\\u003e``, which JSON decodes back to
    the characters they stand for. That single substitution is what makes a
    statement, a source id or a legacy value containing ``-->`` or ``<!--``
    safe: the payload cannot open or close an HTML comment, so the machine
    record cannot break out of it and corrupt the visible line.
    """
    encoded = json.dumps(
        _metadata_payload(record),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return encoded.replace("<", "\\u003c").replace(">", "\\u003e")


# ── Metadata validation ────────────────────────────────────────────────────

_OBSERVATION_FIELDS = frozenset({"source", "turn", "request", "excerpt"})
_METADATA_FIELDS = frozenset(
    {"schema", "id", "observations", "baseline", "aliases", "legacy"}
)


def _metadata_string(raw: Any, name: str) -> str:
    """A required string field, or a schema complaint naming the field."""
    if not isinstance(raw, str):
        raise _MetadataError(f"{name} must be a string")
    return raw


def _observation_from_mapping(raw: Any, index: int) -> LearningObservation:
    """One observation out of its stored mapping, or a schema complaint."""
    if not isinstance(raw, dict):
        raise _MetadataError(f"observation {index} must be an object")
    unknown = sorted(set(raw) - _OBSERVATION_FIELDS)
    if unknown:
        raise _MetadataError(f"observation {index} has unknown field(s) {unknown}")
    source = _metadata_string(raw.get("source", ""), f"observation {index} source")
    request = _metadata_string(raw.get("request", ""), f"observation {index} request")
    excerpt = _metadata_string(raw.get("excerpt", ""), f"observation {index} excerpt")
    turn_raw = raw.get("turn")
    turn: int | None = None
    if turn_raw is not None:
        if isinstance(turn_raw, bool) or not isinstance(turn_raw, int) or turn_raw < 1:
            raise _MetadataError(f"observation {index} turn must be a positive integer")
        turn = turn_raw
    observation = LearningObservation(
        source=source, turn=turn, request=request, excerpt=excerpt
    )
    if observation.identity is None:
        raise _MetadataError(f"observation {index} has no stable identity")
    return observation


@dataclass(frozen=True, slots=True)
class _Metadata:
    """One validated ``ciao:learning`` payload, ready to fold into a record.

    Held apart from :class:`LearningRecord` because it is read before the
    shape of the visible line is: the payload says whether the line carries a
    citation clause, and that is a question about the machine record rather
    than about the prose beside it.
    """

    learning_id: str
    baseline: int | None
    observations: tuple[LearningObservation, ...]
    aliases: tuple[str, ...]
    legacy: tuple[tuple[str, str], ...]


def _parse_metadata(payload: str) -> _Metadata:
    """Validate one ``ciao:learning`` payload into the fields it stores.

    Every type is checked, and an unknown top-level key fails the payload
    rather than being skipped: a payload this code does not fully understand
    may be saying something about the record that a partial read would drop on
    the next write. The identity is kept as the stored string verbatim —
    normalizing a UUID's spelling here would silently rewrite a line that was
    otherwise untouched.
    """
    try:
        raw = json.loads(payload)
    except ValueError as exc:
        raise _MetadataError(f"not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise _MetadataError("payload must be a JSON object")
    unknown = sorted(set(raw) - _METADATA_FIELDS)
    if unknown:
        raise _MetadataError(f"unknown field(s) {unknown}")
    schema = raw.get("schema")
    if isinstance(schema, bool) or schema != SCHEMA_VERSION:
        raise _MetadataError(f"schema {schema!r} is not {SCHEMA_VERSION}")

    learning_id = _metadata_string(raw.get("id"), "id")
    if not learning_id:
        raise _MetadataError("id must not be empty")
    try:
        uuid.UUID(learning_id)
    except ValueError as exc:
        raise _MetadataError(f"id {learning_id!r} is not a UUID") from exc

    stored = raw.get("observations", [])
    if not isinstance(stored, list):
        raise _MetadataError("observations must be an array")
    observations = tuple(
        _observation_from_mapping(item, position)
        for position, item in enumerate(stored)
    )

    baseline_raw = raw.get("baseline")
    baseline: int | None = None
    if baseline_raw is not None:
        if isinstance(baseline_raw, bool) or not isinstance(baseline_raw, int):
            raise _MetadataError("baseline must be an integer or null")
        if baseline_raw < 1:
            raise _MetadataError("baseline must be a positive integer")
        baseline = baseline_raw

    aliases_raw = raw.get("aliases", [])
    if not isinstance(aliases_raw, list):
        raise _MetadataError("aliases must be an array")
    aliases = tuple(
        _metadata_string(item, f"alias {position}")
        for position, item in enumerate(aliases_raw)
    )

    legacy_raw = raw.get("legacy")
    legacy: tuple[tuple[str, str], ...] = ()
    if legacy_raw is not None:
        if not isinstance(legacy_raw, dict):
            raise _MetadataError("legacy must be an object")
        legacy = tuple(
            sorted(
                (name, _metadata_string(value, f"legacy {name}"))
                for name, value in legacy_raw.items()
            )
        )
    return _Metadata(
        learning_id=learning_id,
        baseline=baseline,
        observations=observations,
        aliases=aliases,
        legacy=legacy,
    )


# ── Entry parsing ──────────────────────────────────────────────────────────


def learning_key(text: str) -> str:
    """A short kebab identifier from the statement's first distinctive words.

    The shape the existing writer produces, so a migrated key is
    indistinguishable from one written by hand. It is a display label, not an
    identity: a reworded statement gets a different key and the same
    ``learning_id``.

    Public because the canonical writer has to label a learning it is filing for
    the first time, and a second implementation of the same four-word rule is
    how the two stopped agreeing.
    """
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:KEY_WORDS]) or FALLBACK_KEY


def normalized_statement(text: str) -> str:
    """The comparison form of a statement: lowercase, unpunctuated, one space.

    Identity is persisted, so this is *not* how a learning is identified — it is
    the read a caller has to make in the one case the persisted identity cannot
    answer. A statement filed for the first time has no comment yet, and the
    legacy bullets an installed vault still holds have none either, so matching
    a new sighting against them has to compare prose. Comparing the normalized
    form rather than the raw one is what keeps ``Airtable sort param returns
    400.`` and ``AIRTABLE  sort, param returns 400`` one learning instead of
    two, and what stops a retry that re-quotes the episode from filing it again.

    Lowercased and stripped of every non-alphanumeric character, so two
    spellings of one sentence collide, and two genuinely different sentences do
    not. Whitespace is collapsed by :func:`_flatten` in the parsed record, so
    callers may pass either raw or parsed text.
    """
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def allocate_learning_id(workspace: str, text: str) -> str:
    """The deterministic identifier for *text*, which carries none yet.

    Same derivation :func:`parse_learnings` uses for a legacy entry, so a
    statement filed by the writer and the same statement read back out of the
    file are one learning. Determinism is what makes the review card's preview
    and the accept that follows it land on identical bytes: the preview mints the
    identifier, and so does the write, and neither is told what the other chose.

    The name is built from the *statement*, not from the line that will carry
    the comment, so a writer's own entry and the legacy bullet a migration later
    finds under the same statement get different names and therefore different
    identifiers. That is the safe direction: they are two lines until something
    merges them, and the parser reports a duplicate identifier rather than
    silently letting one record's evidence overwrite the other's.

    The ordinal is always ``0`` here because a statement being filed is one
    entry, and the ordinal exists only to keep two byte-identical legacy bullets
    two entries.
    """
    return _allocate_id(workspace, _flatten(text), 0)


def _allocate_id(workspace: str, entry_text: str, occurrence: int) -> str:
    """The deterministic identifier for an entry that carries none yet.

    The name is versioned, workspace-scoped and newline-delimited, so ``work``
    and ``work-2`` are different workspaces rather than a prefix relationship
    that collides. ``occurrence`` is the index among *identical* entry texts in
    the same document, which is what keeps two byte-identical legacy bullets
    two entries instead of one entry counted twice.
    """
    name = f"{ID_NAME_VERSION}\n{workspace}\n{entry_text}\n{occurrence}"
    return str(uuid.uuid5(ID_NAMESPACE, name))


def _observations_from_citations(
    citations: str,
) -> tuple[tuple[LearningObservation, ...], tuple[tuple[str, str], ...]]:
    """Read a ``— sources: …`` list into observations, keeping prose in legacy.

    The two display spellings :attr:`LearningObservation.citation` writes are
    read back as what they are: ``chat-a#3`` is one source at one turn, and
    ``req:r1`` is one user request. Both characters are legal in a source
    identifier, so without this a replay of a migrated source was counted a
    second time — the identity on the line and the identity in the record had
    quietly stopped being the same thing.

    Any other token becomes an observation only when it *is* an identifier — no
    spaces, nothing but identifier characters. A token like ``the migration
    thread`` is a citation the owner wrote, not a chat id, and promoting it to
    one would fabricate provenance out of English; it is retained verbatim under
    ``legacy`` so the information survives without becoming countable.
    """
    observations: list[LearningObservation] = []
    prose: list[str] = []
    for token in citations.split(","):
        candidate = token.strip()
        if not candidate:
            continue
        turn = _TURN_CITATION_RE.match(candidate)
        if turn is not None:
            observations.append(
                LearningObservation(
                    source=turn.group("source"), turn=int(turn.group("turn"))
                )
            )
            continue
        request = _REQUEST_CITATION_RE.match(candidate)
        if request is not None:
            observations.append(LearningObservation(request=request.group("request")))
            continue
        if _IDENTIFIER_RE.fullmatch(candidate):
            observations.append(LearningObservation(source=candidate))
        else:
            prose.append(candidate)
    legacy = (("sources", ", ".join(prose)),) if prose else ()
    return tuple(observations), legacy


def _malformed(source_text: str, line: _Line, problems: list[str]) -> LearningEntry:
    """An entry kept exactly as written, with the reasons it was not read."""
    return LearningEntry(
        source_text=source_text,
        start=line.start,
        end=line.end,
        section=line.section,
        format=FORMAT_MALFORMED,
        record=None,
        diagnostics=tuple(problems),
    )


def _parse_canonical(
    visible: str, line: _Line, metadata: _Metadata | None
) -> tuple[LearningRecord | None, list[str]]:
    """Read a current ``- [key] [first → last] (xN) statement`` line."""
    match = _CANONICAL_RE.match(visible)
    if match is None:
        return None, []
    where = _where(line)
    problems: list[str] = []

    key = _flatten(match.group("key"))
    if not key:
        problems.append(f"{where}: canonical entry has an empty key")
    first = _display_date(match.group("first"), "first-seen", where, problems)
    last = _display_date(match.group("last"), "last-seen", where, problems)
    if first is not None and last is not None and first > last:
        problems.append(f"{where}: last-seen {last} is before first-seen {first}")

    token = match.group("count").strip()
    count: int | None = None
    if token == UNKNOWN_COUNT:
        count = None
    elif re.fullmatch(r"x\d+", token):
        count = int(token[1:])
        if count < 1:
            problems.append(f"{where}: recurrence {token} is not a positive count")
    else:
        problems.append(
            f"{where}: recurrence {token!r} is neither xN nor {UNKNOWN_COUNT!r}"
        )

    statement, citations = _split_citations(match.group("body"), metadata)
    text = _flatten(statement)
    if not text:
        problems.append(f"{where}: canonical entry has an empty statement")
    if problems:
        return None, problems

    observations, prose = _observations_from_citations(citations)
    return (
        LearningRecord(
            learning_id="",
            key=key,
            text=text,
            first_seen=first,
            last_seen=last,
            count=count,
            observations=observations,
            # A canonical line's count *is* the baseline: the xN already
            # happened before this code saw it, and its sources seed the seen
            # set rather than adding to it.
            baseline_count=count,
            legacy=prose,
        ),
        [],
    )


def _parse_legacy(
    visible: str, line: _Line, metadata: _Metadata | None
) -> tuple[LearningRecord | None, list[str]]:
    """Read a historical ``- [date] category: statement — confidence: …`` line."""
    match = _LEGACY_RE.match(visible)
    if match is None or not _DATE_ATTEMPT_RE.search(match.group("date")):
        return None, []
    where = _where(line)
    seen = _parse_date(match.group("date"))
    if seen is None:
        # A bracketed date that is not one. The shape is known, the value is
        # not, and nudging it to the nearest real date would move a learning's
        # history on the strength of a typo.
        return None, [
            f"{where}: legacy entry date {match.group('date')!r} is not a real date"
        ]

    remainder, citations = _split_citations(match.group("body"), metadata)
    legacy: list[tuple[str, str]] = []
    confidence = _CONFIDENCE_RE.search(remainder)
    if confidence is not None:
        legacy.append(("confidence", _flatten(confidence.group("confidence"))))
        remainder = remainder[: confidence.start()]

    category, separator, statement = remainder.partition(": ")
    if separator and category.strip():
        legacy.append(("category", _flatten(category)))
        remainder = statement
    text = _flatten(remainder)
    if not text:
        return None, [f"{where}: legacy entry has an empty statement"]

    observations, prose = _observations_from_citations(citations)
    legacy.extend(prose)
    return (
        LearningRecord(
            learning_id="",
            key=learning_key(text),
            text=text,
            first_seen=seen,
            last_seen=seen,
            # A date-only entry has no recurrence history. Leaving the count
            # unknown is the point: an invented x1 would be read by the
            # worklist as evidence of one sighting, which nobody witnessed.
            count=None,
            observations=observations,
            legacy=tuple(sorted(legacy)),
        ),
        [],
    )


def _read_metadata(
    payload: str | None, line: _Line
) -> tuple[_Metadata | None, list[str]]:
    """Validate the comment on a line, or report why it could not be read.

    Malformed metadata is not prose. Reading the line as an ordinary bullet
    would drop the identity and the evidence it carries the moment anything
    rewrote the file, so the entry is reported instead.
    """
    if payload is None:
        return None, []
    try:
        return _parse_metadata(payload), []
    except _MetadataError as exc:
        return None, [f"{_where(line)}: ciao:learning metadata {exc}"]


def _with_metadata(
    record: LearningRecord, metadata: _Metadata | None, line: _Line
) -> tuple[LearningRecord | None, list[str]]:
    """Fold a ``ciao:learning`` comment into a freshly read record.

    The comment wins on identity, baseline, observations, aliases and retained
    legacy provenance — that is where they are persisted, and they are the whole
    point of the comment. Once it is present the visible ``— sources:`` list is
    display, re-derived from the comment's observations on the next write, so a
    cited phrase that is not an identifier moves out of the visible line and
    into the machine record instead of being counted as a source. The visible
    line still owns the key, the statement and the dates, so rewording a line
    is a visible edit and nothing else.
    """
    if metadata is None:
        return record, []
    where = _where(line)
    if (
        record.count is not None
        and metadata.baseline is not None
        and record.count < metadata.baseline
    ):
        return None, [
            f"{where}: ciao:learning metadata baseline {metadata.baseline} exceeds "
            f"the recurrence {record.count} shown on the line"
        ]
    return (
        replace(
            record,
            learning_id=metadata.learning_id,
            observations=metadata.observations,
            baseline_count=metadata.baseline,
            aliases=metadata.aliases,
            legacy=metadata.legacy,
        ),
        [],
    )


@dataclass(frozen=True, slots=True)
class _Read:
    """One parsed line, and the identity it carried explicitly, if any."""

    entry: LearningEntry
    explicit_id: str | None


def _parse_bullet(line: _Line, source_text: str) -> _Read | None:
    """Read one line as whichever known shape it is, or diagnose it.

    ``source_text`` is the whole line, bullet marker included, because that is
    what the entry's offsets address. The metadata comment is peeled off that
    whole line first, then the shapes are matched against what remains, and the
    statement comes from the text after the bullet marker. The comment is
    validated before the shape is read: it says whether the visible line carries
    a citation clause at all, and a comment that does not hold up leaves the
    entry unreadable whatever the prose beside it looks like.
    """
    visible_line, payload = _split_metadata(source_text)
    bullet = _BULLET_RE.match(visible_line)
    if bullet is None:
        return None
    body = bullet.group("body")

    metadata, metadata_problems = _read_metadata(payload, line)
    if metadata_problems:
        return _Read(_malformed(source_text, line, metadata_problems), None)

    canonical = _CANONICAL_RE.match(visible_line)
    legacy = _LEGACY_RE.match(visible_line) if canonical is None else None
    legacy_looking = (
        legacy is not None and _DATE_ATTEMPT_RE.search(legacy.group("date")) is not None
    )

    if canonical is not None:
        shape = FORMAT_CANONICAL
        record, problems = _parse_canonical(visible_line, line, metadata)
    elif legacy_looking:
        shape = FORMAT_LEGACY
        record, problems = _parse_legacy(visible_line, line, metadata)
    else:
        shape = FORMAT_PLAIN
        text = _flatten(body)
        record, problems = (
            (LearningRecord(learning_id="", key=learning_key(text), text=text), [])
            if text
            else (None, [f"{_where(line)}: bullet has no text"])
        )

    if record is None or problems:
        return _Read(_malformed(source_text, line, problems), None)

    record, metadata_problems = _with_metadata(record, metadata, line)
    if record is None or metadata_problems:
        return _Read(_malformed(source_text, line, metadata_problems), None)
    return _Read(
        LearningEntry(
            source_text=source_text,
            start=line.start,
            end=line.end,
            section=line.section,
            format=shape,
            record=record,
        ),
        record.learning_id or None,
    )


def _resolve_conflicts(reads: list[_Read]) -> list[LearningEntry]:
    """Report a stored identifier this document used twice, without merging.

    Two lines claiming one identity cannot both be right, and silently picking
    a winner would let one learning's evidence overwrite another's. The first
    line to claim an id keeps it; every later claim becomes a diagnosed
    conflict whose bytes are preserved untouched, so the owner decides.
    """
    seen: set[str] = set()
    entries: list[LearningEntry] = []
    for read in reads:
        entry = read.entry
        if read.explicit_id is None:
            entries.append(entry)
            continue
        normalized = str(uuid.UUID(read.explicit_id))
        if normalized not in seen:
            seen.add(normalized)
            entries.append(entry)
            continue
        entries.append(
            replace(
                entry,
                format=FORMAT_CONFLICT,
                record=None,
                diagnostics=(
                    f"{_SECTION_LABELS.get(entry.section, 'Unsectioned')}: ciao:learning "
                    f"id {read.explicit_id} is already used by an earlier entry in "
                    "this document",
                ),
            )
        )
    return entries


# ── Public API ─────────────────────────────────────────────────────────────


def parse_learnings(text: str, *, workspace: str) -> LearningDocument:
    """Read a learnings document into entries, records and diagnostics.

    Only real list items inside the ``## Active`` and ``## Promoted / Resolved``
    sections become entries. Frontmatter, fenced code, the preamble above the
    first section, and every other section — a ``## Format`` note, a worked
    example — are left as content, because an example that parses as a
    learning would be migrated into a real one.

    ``workspace`` scopes the identifiers minted for entries that carry none.
    An entry that already has a stored identifier keeps it whatever the
    workspace argument says, which is what makes a reworded line, or a file
    re-parsed by a differently-configured caller, still the same learning.

    A shape that is recognized but does not hold up — an impossible date, a
    range that runs backwards, a count of zero, metadata that is not valid
    JSON, an identifier this document already used — yields an entry with no
    record and a diagnostic. Its bytes are not interpreted and not lost.
    """
    occurrences: dict[str, int] = {}
    reads: list[_Read] = []
    for line in _scan(text):
        if not line.parseable or not line.section:
            continue
        read = _parse_bullet(line, text[line.start : line.end])
        if read is not None:
            reads.append(read)

    entries = _resolve_conflicts(reads)
    identified: list[LearningEntry] = []
    for entry in entries:
        record = entry.record
        if record is not None and not record.learning_id:
            occurrence = occurrences.get(entry.source_text, 0)
            occurrences[entry.source_text] = occurrence + 1
            record = replace(
                record,
                learning_id=_allocate_id(workspace, entry.source_text, occurrence),
            )
            entry = replace(entry, record=record)
        identified.append(entry)
    diagnostics = tuple(
        problem for entry in identified for problem in entry.diagnostics
    )
    return LearningDocument(
        text=text, entries=tuple(identified), diagnostics=diagnostics
    )


def render_learning(record: LearningRecord) -> str:
    """The canonical line for *record*, comment included.

    The visible part keeps the shape the current writer emits — ``- [key]
    [first → last] (xN) statement — sources: …`` — with ``unknown`` for an
    unknown date and ``?`` for an unknown recurrence, so a legacy bullet
    migrated here is visibly honest about how much is not known rather than
    quietly stamped with today and an x1. The citation list is capped at
    :data:`MAX_DISPLAY_SOURCES` entries because it is read by people; the
    comment that follows carries every observation, however many there are.

    ``key`` is flattened exactly as ``text`` is: a key carrying a newline would
    otherwise split the bullet it lives on into a truncated line and a
    continuation.

    Raises ``ValueError`` for a record whose ``learning_id`` is empty or is not
    a UUID, and for a key carrying a bracket. Both write a line this module
    cannot read back — the identifier would be diagnosed as broken and the key
    would be read as prose — and both would be found out long after the caller
    that wrote the line had moved on. Refusing here keeps the write and the read
    agreeing.
    """
    try:
        uuid.UUID(record.learning_id)
    except (AttributeError, ValueError) as exc:
        raise ValueError(
            f"learning_id {record.learning_id!r} is not a UUID; a learning is "
            "rendered only once it has a stable identity"
        ) from exc
    key = _flatten(record.key)
    if "[" in key or "]" in key:
        raise ValueError(
            f"key {record.key!r} contains a bracket; the key is written between "
            "brackets on the line and would not be read back"
        )
    first = record.first_seen.isoformat() if record.first_seen else UNKNOWN_DATE
    last = record.last_seen.isoformat() if record.last_seen else UNKNOWN_DATE
    count = f"x{record.count}" if record.count is not None else UNKNOWN_COUNT
    line = f"- [{key}] [{first} → {last}] ({count}) {_flatten(record.text)}"
    cited = [
        observation.citation
        for observation in record.observations
        if observation.identity is not None
    ][:MAX_DISPLAY_SOURCES]
    if cited:
        line += f" — sources: {', '.join(cited)}"
    return f"{line} {METADATA_MARKER}{_metadata_json(record)}{METADATA_SUFFIX}"


def entry_revision(record: LearningRecord) -> str:
    """The revision of *this* learning — the hash of its own canonical line.

    Not the revision of the file it lives in. A learnings document is one
    workspace's running notes: appending a new lesson, re-reading a neighbour's
    entry, or the cleanup pass itself splicing out an unrelated line all rewrite
    it, so a whole-file hash cancels eligibility for every learning at once and
    two findings filed at different moments can never both match. What "unchanged
    since this finding was filed" actually has to mean is *this line has not been
    touched*, and that is what this hashes.

    The bytes are :func:`render_learning`'s — the canonical line, comment
    included — so the value is the same whether it is computed from a record
    parsed out of the file or from one the caller is about to write. A
    whitespace-only difference is a difference, deliberately: an entry somebody
    typed a space into is a line that changed, and the reading of it has to
    happen again before anything is removed on the strength of what it said
    before.

    Two consequences of hashing the *whole* canonical line, both in the safe
    direction. A **merge** changes the revision, because ``aliases`` are carried
    in the comment: a learning that absorbed another one is not the line the
    finding was written against, and re-reading it before anything is removed is
    exactly right. And a **new sighting** changes it too, because ``observations``
    are carried there — which is the difference between a whole-file hash and this
    one, and the reason a learning with a ninth sighting is not a learning whose
    line was rewritten behind a decision.

    :func:`ciao.memory_receipts.content_revision` is used rather than a second
    hash so one revision is one digest everywhere in the app: a caller that
    already holds ``content_revision`` of some text gets the same answer here
    that it would have computed itself, and no two surfaces can disagree about
    what a revision *is*.

    Raises ``ValueError`` for a record :func:`render_learning` refuses, because a
    revision of a line that cannot be written back is a revision of nothing.
    """
    return content_revision(render_learning(record))


def migrate_learnings(text: str, *, workspace: str) -> tuple[str, list[str]]:
    """Rewrite the Active entries *text* does not yet record, and nothing else.

    Returns the new text and the document's diagnostics. Only the spans of
    recognized entries under ``## Active`` are replaced, each with
    :func:`render_learning`; everything else — frontmatter, format notes, the
    ``## Promoted / Resolved`` section, blank lines, the BOM, the line endings —
    is carried through byte for byte, so a second pass finds nothing left to do
    and returns exactly what the first one produced.

    A document with no Active section is a no-op, and a document whose Active
    entries are all unreadable comes back identical with its diagnostics
    attached. The caller decides whether to write the result; this function
    never does.
    """
    document = parse_learnings(text, workspace=workspace)
    edits: list[tuple[int, int, str]] = []
    for entry in document.entries:
        if entry.section != SECTION_ACTIVE or entry.record is None:
            continue
        rendered = render_learning(entry.record)
        if rendered != entry.source_text:
            edits.append((entry.start, entry.end, rendered))

    migrated = text
    # Back to front, so each splice leaves the offsets of the ones before it
    # valid. Only the entry's own content is replaced; its terminator and
    # everything after it live in the untouched tail.
    for start, end, rendered in sorted(edits, key=lambda edit: edit[0], reverse=True):
        migrated = migrated[:start] + rendered + migrated[end:]
    return migrated, list(document.diagnostics)


def observe_learning(
    record: LearningRecord, observation: LearningObservation, *, today: date
) -> LearningRecord:
    """Record one sighting of *record*, or return it unchanged.

    The rules, all of which exist because a retry is the common case:

    * **A sighting with no stable identity is refused.** No ``source`` and no
      ``request`` means nothing can be told apart from any other sighting, so
      the record comes back untouched — no count, no date. A source-less
      re-file would otherwise inflate recurrence forever.
    * **A repeated identity is a no-op, dates included.** Identity is the
      source with its turn, or the request id, and never the excerpt: replaying
      ``chat-a`` with the episode quoted differently is still one sighting, and
      it does not move ``last_seen`` either. This is also what keeps a
      migrated ``xN`` from drifting — the sources that baseline already counted
      are already in ``observations``, so they replay as no-ops.
    * **A distinct sighting updates once.** ``last_seen`` moves forward to
      ``today`` and never backwards, so a backfilled observation cannot shorten
      a record's history, and ``count`` increments when it is known. An
      unknown count stays unknown: there is no evidence for what it was before
      this sighting, and guessing ``1`` would claim the record was never seen
      again when nobody knows that.

    ``baseline_count`` is left alone throughout. It is what the line already
    accounted for, and only the line's own count moves.
    """
    if observation.identity is None:
        return record
    if any(seen.identity == observation.identity for seen in record.observations):
        return record
    last_seen = record.last_seen
    if last_seen is None or today > last_seen:
        last_seen = today
    return replace(
        record,
        observations=record.observations + (observation,),
        last_seen=last_seen,
        count=None if record.count is None else record.count + 1,
    )
