"""A deterministic verifier for one project-vocabulary statement.

This is the isolated first slice of #3623, deliberately not wired to chat
ingress, a consent grant, the evidence record, a producer or publication. It
takes the *whole* message as bytes, recognises one of three project-convention
sentence frames, and returns either the two literal terms plus an engine-owned
rendering, or a reason it deferred privately. It reads no clock, performs no
I/O, calls no model and imports nothing outside the standard library.

The accepted domain is a closed vocabulary, not a parser for arbitrary English::

    In this project, we call the weekly planning meeting the Monday huddle.
    For our project, the monthly review meeting is called the Friday sync.
    Within this project, we refer to the quarterly planning meeting as the sync.

The three frames are ``we call <definition> the <alias>``, ``<definition> is
called <alias>`` and ``we refer to <definition> as <alias>``. The definition is
an optional cadence plus ``planning`` or ``review`` plus ``meeting``; the alias
is an optional weekday plus ``huddle`` or ``sync``. Each slot has its own finite
dictionary, the dictionaries are disjoint, and the whole literal compound is
approved atomically. A word outside the dictionaries, a second clause, more than
one valid parse or punctuated/mixed text is deferred; the parser never guesses a
boundary or reads a longer name out of a sentence.

A statement has to be the entire message (surrounding whitespace and one final
period are allowed). Before the parser runs, the whole message is scanned for
negation, conditions, qualifiers, uncertainty, temporal qualifiers, corrections,
quotation/forwarding, code, instructions, secret-shaped values, private
identifiers and sensitive content. Those scans are not stripped out and ignored;
a marker anywhere defers the candidate, because a span match alone is not
evidence of meaning. The secret and sensitive scans are bounded, explicit
detectors for the shapes listed here, not a claim to find every secret or every
sensitive statement, and the approved vocabulary is not a claim that every
accepted phrase is private or true.

The output carries a SHA-256 of the exact input bytes and byte spans for both
terms, so a later integration can resolve them against its own immutable turn
record instead of trusting a model's offsets. It does not return the raw
message: the accepted record holds only the two terms, the project scope, the
fixed rendering, the digest and the two spans. The digest and spans are private
provenance for the engine record; they are not part of the rendering and must not
be written into a destination note. Attribution in the grammar is first-person
plural ("we call") or a project-scoped passive ("X is called Y").

Trust boundary: this module verifies that the bytes in front of it match one
approved convention. It cannot establish who typed them, whether a grant covers
them, or whether the destination audience is appropriate; the automatic ordinary
chat ingress, standing consent/grants, evidence, producer and publication
integrations tracked by #3623/#3614/#3615/#3617/#3618/#3624 remain unwired here.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

# ── The record ────────────────────────────────────────────────────────────

SCHEMA = "project-vocabulary/v2"
"""Bumped when the accepted record's shape or approved domain changes."""

RENDER_TEMPLATE = 'In {scope}, "{definition}" is called "{term}".'
"""The only rendering this module produces. Both terms are literal input text;
the surrounding words are engine-owned, so a caller never supplies prose,
a destination or a path."""

MAX_MESSAGE_BYTES = 8 * 1024
"""The whole message is the unit of evidence, and a convention is short. A
larger message is deferred rather than searched for a sentence inside it."""

MAX_BODY_TOKENS = 16
"""The longest approved frame uses 11 word tokens. A body beyond this bound
cannot be one of the three frames, so it is deferred before the parser checks
prefixes; that keeps the whole-turn check linear in the message size."""

# ── Finite slot dictionaries ───────────────────────────────────────────────
#
# These are the entire accepted vocabulary. A word outside them is deferred, so
# widening the domain is an explicit edit here, not a side effect of a looser
# pattern. The definition slots and alias slots are disjoint; tests pin that.

DEFINITION_CADENCES: frozenset[str] = frozenset(
    {
        "daily",
        "weekly",
        "biweekly",
        "fortnightly",
        "monthly",
        "quarterly",
        "yearly",
        "annual",
        "sprint",
        "release",
    }
)

DEFINITION_KINDS: frozenset[str] = frozenset({"planning", "review"})
DEFINITION_NOUNS: frozenset[str] = frozenset({"meeting"})

ALIAS_WEEKDAYS: frozenset[str] = frozenset(
    {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    }
)
ALIAS_NOUNS: frozenset[str] = frozenset({"huddle", "sync"})


def _build_definitions() -> frozenset[str]:
    definitions = {
        f"{kind} {noun}"
        for kind in DEFINITION_KINDS
        for noun in DEFINITION_NOUNS
    }
    definitions.update(
        f"{cadence} {kind} {noun}"
        for cadence in DEFINITION_CADENCES
        for kind in DEFINITION_KINDS
        for noun in DEFINITION_NOUNS
    )
    return frozenset(definitions)


def _build_aliases() -> frozenset[str]:
    aliases = set(ALIAS_NOUNS)
    aliases.update(
        f"{weekday} {noun}"
        for weekday in ALIAS_WEEKDAYS
        for noun in ALIAS_NOUNS
    )
    return frozenset(aliases)


# The full compound is the unit of approval. Matching the slots piecewise would
# let an unapproved combination through, so these sets are generated once from
# the dictionaries and checked as whole phrases.
APPROVED_DEFINITIONS: frozenset[str] = _build_definitions()
APPROVED_ALIASES: frozenset[str] = _build_aliases()

_FRAME_WORDS: frozenset[str] = frozenset(
    {"we", "call", "is", "called", "refer", "to", "as", "the"}
)
_ALLOWED_BODY_WORDS: frozenset[str] = (
    _FRAME_WORDS
    | DEFINITION_CADENCES
    | DEFINITION_KINDS
    | DEFINITION_NOUNS
    | ALIAS_WEEKDAYS
    | ALIAS_NOUNS
)


@dataclass(frozen=True, slots=True)
class VocabularyFact:
    """One accepted convention, with the bytes it was read from identified.

    ``definition`` is the thing being named and ``term`` the project's name for
    it. The byte spans address the original whole message, not the stripped or
    decoded copy, so ``raw[slice(*fact.definition_span)]`` is exactly the
    literal term. ``source_sha256`` is over the original bytes too.
    """

    scope: str
    definition: str
    term: str
    rendered: str
    source_sha256: str
    definition_span: tuple[int, int]
    term_span: tuple[int, int]
    schema: str = SCHEMA


@dataclass(frozen=True, slots=True)
class Verdict:
    """The verifier's answer.

    Exactly one of ``fact`` and ``reason`` is set. A refusal never echoes the
    message, so a rejected secret or private identifier is not copied into a
    log or an error surface by this module. Every refusal is a private-review
    defer; the reason only names why the parser did not admit the bytes.
    """

    fact: VocabularyFact | None = None
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.fact is not None


# ── Refusal vocabulary ─────────────────────────────────────────────────────

INVALID_UTF8 = "invalid_utf8"
TOO_LARGE = "too_large"
EMPTY = "empty"
CONTROL_CHARACTER = "control_character"
NEGATED = "negated"
CONDITIONAL = "conditional"
UNCERTAIN = "uncertain"
TEMPORAL = "temporal"
CORRECTION = "correction"
CODE = "code"
INSTRUCTION = "instruction"
QUOTED_OR_FORWARDED = "quoted_or_forwarded"
SECRET_SHAPED = "secret_shaped"
PRIVATE_IDENTIFIER = "private_identifier"
SENSITIVE_CONTENT = "sensitive_content"
MIXED_SENSITIVE = "mixed_sensitive"
UNKNOWN_WORD = "unknown_word"
ADDED_CLAUSE = "added_clause"
MULTIPLE_PARSES = "multiple_parses"
MIXED_TEXT = "mixed_text"
UNSUPPORTED_FORM = "unsupported_form"

# ── Whole-message context scanners ─────────────────────────────────────────
#
# Each scanner is case-insensitive and runs over the whole message before the
# parser. A word may be inside an approved dictionary and still be part of a
# statement whose meaning is changed by a negation, a condition, a quotation or
# a secret; the scans refuse those before the parser can see a clean phrase. The
# lists are deliberately conservative: an uncertain candidate belongs in
# private review, not in an automatic contribution.

_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|none|nobody|nothing|neither|nor|without|cannot)\b"
    r"|\b(?:rather\s+than|instead\s+of)\b"
    r"|\b\w+n['’]t\b",
    re.IGNORECASE,
)

_CONDITIONAL_RE = re.compile(
    r"\b(?:if|unless|when|whenever|provided|assuming|suppose|whether)\b"
    r"|\b(?:in\s+case|as\s+long\s+as)\b"
    r"|\b(?:would|could|should)\b"
    r"|\b(?:except|apart\s+from|other\s+than|excluding|including|besides|"
    r"save\s+for)\b",
    re.IGNORECASE,
)

_UNCERTAIN_RE = re.compile(
    r"\b(?:maybe|perhaps|probably|possibly|might|uncertain|unsure|unclear|"
    r"apparently|likely|presumably|seemingly|seems?|sort\s+of|kind\s+of)\b"
    r"|\b(?:i\s+think|i\s+believe|i\s+guess)\b",
    re.IGNORECASE,
)

_TEMPORAL_RE = re.compile(
    r"\b(?:formerly|previously|currently|now|today|tomorrow|yesterday|soon|"
    r"eventually|originally|initially|temporarily|until|since|next|last|"
    r"after|before|only|during|summer|winter|spring|autumn|seasonal|season|"
    r"going\s+forward|from\s+now\s+on|used\s+to|"
    r"for\s+now|this\s+week|next\s+week|last\s+week)\b",
    re.IGNORECASE,
)

_CORRECTION_RE = re.compile(
    r"\b(?:actually|correction|corrected|instead|rather|scratch\s+that|"
    r"i\s+mean|i\s+meant|sorry|renamed?|renaming|now\s+called)\b",
    re.IGNORECASE,
)

_CODE_RE = re.compile(
    r"```|`|<(?:code|pre|script)\b|"
    r"(?:^|\s)(?:def|class|import|from|function|const|let|var)\s+[A-Za-z_]",
    re.IGNORECASE | re.MULTILINE,
)

_INSTRUCTION_RE = re.compile(
    r"\b(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|above|earlier)\b"
    r"|\b(?:system|developer)\s+prompt\b"
    r"|\byou\s+(?:are|must|should|will)\b"
    r"|\b(?:run|execute)\s+(?:this|the\s+following)\b"
    r"|^\s*(?:sudo|rm|curl|wget|eval|exec)\b"
    r"|\b(?:send|email|post|upload|share|export|transfer|wire|forward)\s+"
    r"(?:the\s+)?(?:secrets?|passwords?|tokens?|keys?|credentials?|data|"
    r"files?)\b"
    r"|\b(?:send|email|post|upload|share|export|transfer|wire|forward)\b"
    r".*\b(?:attacker|attackers|outside|external|third\s+party)\b",
    re.IGNORECASE | re.MULTILINE,
)

_QUOTE_CHARS = frozenset("\"'`“”‘’«»")
_FORWARD_RE = re.compile(
    r"^\s*>|\b(?:forwarded\s+message|fwd:)\b|\bon\s+.+\s+wrote\s*:",
    re.IGNORECASE | re.MULTILINE,
)

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:sk|pk)[_-](?:live|test)[_-][A-Za-z0-9]{8,}\b", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{10,}=*", re.IGNORECASE),
    re.compile(
        r"\b(?:password|passwd|secret|token|api[\s_-]?key|private[\s_-]?key)"
        r"\s*[:=]",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:password|passwd|secret|token|api[\s_-]?key|private[\s_-]?key)"
        r"(?:\s*[:=]\s*|\s+)[A-Za-z0-9._~+/-]{5,}\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b[A-Za-z]{24,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    # Real digests and base64 blobs carry digits and/or a symbol; an all-letter
    # word is covered by the long-token rule above instead.
    re.compile(r"\b(?=[A-Fa-f0-9]*\d)[A-Fa-f0-9]{32,}\b"),
    re.compile(
        r"\b(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[+/=])"
        r"[A-Za-z0-9+/]{40,}={0,2}\b"
    ),
)

_PRIVATE_PATTERNS = (
    re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b"),
    re.compile(r"\b(?:https?|ftp)://\S+", re.IGNORECASE),
    re.compile(r"\bwww\.\S+", re.IGNORECASE),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?<!\d)(?:\+?\d[\d .()\-]{7,}\d)(?!\d)"),
    re.compile(r"\b\d{9,}\b"),
    re.compile(r"(?:^|\s)(?:~?/|\.{1,2}/)\S+"),
)

# One row per sensitivity family. A message with a marker from one family is
# deferred as sensitive; a message spanning two or more is deferred as mixed.
_SENSITIVE_CATEGORIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "health",
        re.compile(
            r"\b(?:health|medical|mental\s+health|diagnos\w*|medication|"
            r"therapy|therapist|patient|hospital|clinic|symptom|illness|"
            r"disease|prescription|psychiatr\w*|disabilit\w*|cancer|"
            r"pregnan\w*|hiv|aids|depress\w*|anxiety|anxious|suicid\w*|"
            r"self[\s-]?harm|trauma|ptsd|addiction|overdose|abuse|"
            r"suffer\w*|eating\s+disorder|bipolar|schizophren\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "finance",
        re.compile(
            r"\b(?:bank|banking|iban|swift|routing|credit\s+card|debit\s+card|"
            r"salary|payroll|tax|taxes|invoice|payment|mortgage|loan|debt|ssn|"
            r"social\s+security|pension|investment|brokerage|net\s+worth|"
            r"revenue|budget)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "identity",
        re.compile(
            r"\b(?:passport|driver'?s\s+license|national\s+id|date\s+of\s+birth|"
            r"birth\s+date|home\s+address|citizenship|visa|immigration|lawsuit|"
            r"attorney|lawyer|court|nda|privileged)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "personal",
        re.compile(
            r"\b(?:religion|religious|ethnic|racial|sexual\s+orientation|"
            r"political|politics|biometric|fingerprint|face\s+id|genetic|dna)\b",
            re.IGNORECASE,
        ),
    ),
)

# ── Parser ─────────────────────────────────────────────────────────────────

_SCOPE_RE = re.compile(
    r"^(?:in|for|within)\s+(?P<scope>this|our)\s+project\s*,\s*(?P<body>.*)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _Token:
    text: str
    start: int
    end: int
    value: str


@dataclass(frozen=True, slots=True)
class _Candidate:
    definition: tuple[_Token, ...]
    alias: tuple[_Token, ...]


# ── Entry point ────────────────────────────────────────────────────────────


def verify_project_vocabulary(raw: bytes) -> Verdict:
    """Verify one whole message and return the accepted fact or the refusal.

    ``raw`` must be ``bytes``; passing ``str`` is a caller bug and raises
    ``TypeError`` rather than guessing an encoding. Invalid UTF-8, an oversized
    message, control characters or an empty message are ordinary refusals with
    a reason, not exceptions, so an ingress that later calls this can record
    them without a traceback.
    """
    if not isinstance(raw, bytes):
        raise TypeError("verify_project_vocabulary() requires the exact message bytes")

    if len(raw) > MAX_MESSAGE_BYTES:
        return Verdict(reason=TOO_LARGE)
    if not raw:
        return Verdict(reason=EMPTY)

    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Verdict(reason=INVALID_UTF8)

    prefix_chars = len(decoded) - len(decoded.lstrip())
    prefix_bytes = len(decoded[:prefix_chars].encode("utf-8"))
    text = decoded.strip()
    if not text:
        return Verdict(reason=EMPTY)
    if any(unicodedata.category(ch) in {"Cc", "Cf", "Cs"} for ch in text):
        return Verdict(reason=CONTROL_CHARACTER)

    reason = _context_refusal(text)
    if reason:
        return Verdict(reason=reason)

    return _parse(text, raw, prefix_bytes)


def _context_refusal(text: str) -> str:
    """Return the first whole-message marker that makes the text inadmissible.

    Negation runs before quotation so a contraction such as "don't" is reported
    as a negated convention rather than as a stray apostrophe. Secrets and
    private identifiers run before the parser so their presence is never hidden
    behind a generic unknown-word answer.
    """
    for pattern, reason in (
        (_NEGATION_RE, NEGATED),
        (_CONDITIONAL_RE, CONDITIONAL),
        (_UNCERTAIN_RE, UNCERTAIN),
        (_TEMPORAL_RE, TEMPORAL),
        (_CORRECTION_RE, CORRECTION),
        (_CODE_RE, CODE),
        (_INSTRUCTION_RE, INSTRUCTION),
    ):
        if pattern.search(text):
            return reason

    if any(ch in _QUOTE_CHARS for ch in text) or _FORWARD_RE.search(text):
        return QUOTED_OR_FORWARDED
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        return SECRET_SHAPED
    if any(pattern.search(text) for pattern in _PRIVATE_PATTERNS):
        return PRIVATE_IDENTIFIER

    categories = [
        name for name, pattern in _SENSITIVE_CATEGORIES if pattern.search(text)
    ]
    if len(categories) >= 2:
        return MIXED_SENSITIVE
    if categories:
        return SENSITIVE_CONTENT
    return ""


def _parse(text: str, raw: bytes, prefix_bytes: int) -> Verdict:
    scope_match = _SCOPE_RE.match(text)
    if scope_match is None:
        return Verdict(reason=UNSUPPORTED_FORM)

    body = scope_match.group("body").rstrip()
    if body.endswith("."):
        body = body[:-1].rstrip()
    body_offset = scope_match.start("body")
    tokenized = _tokenize(body, body_offset)
    if isinstance(tokenized, str):
        return Verdict(reason=tokenized)
    tokens = tokenized
    if len(tokens) > MAX_BODY_TOKENS:
        return Verdict(reason=ADDED_CLAUSE)

    candidates = _candidates(tokens)
    if len(candidates) > 1:
        return Verdict(reason=MULTIPLE_PARSES)
    if not candidates:
        if _has_valid_prefix(tokens):
            return Verdict(reason=ADDED_CLAUSE)
        if any(token.value not in _ALLOWED_BODY_WORDS for token in tokens):
            return Verdict(reason=UNKNOWN_WORD)
        return Verdict(reason=UNSUPPORTED_FORM)

    candidate = candidates[0]
    definition = text[candidate.definition[0].start : candidate.definition[-1].end]
    term = text[candidate.alias[0].start : candidate.alias[-1].end]
    scope = f"{scope_match.group('scope').casefold()} project"
    fact = VocabularyFact(
        scope=scope,
        definition=definition,
        term=term,
        rendered=RENDER_TEMPLATE.format(
            scope=scope, definition=definition, term=term
        ),
        source_sha256=hashlib.sha256(raw).hexdigest(),
        definition_span=_token_span(text, candidate.definition, prefix_bytes),
        term_span=_token_span(text, candidate.alias, prefix_bytes),
    )
    return Verdict(fact=fact)


def _tokenize(body: str, body_offset: int) -> tuple[_Token, ...] | str:
    """Split the body into word tokens, refusing punctuation and empty bodies."""
    tokens: list[_Token] = []
    for match in re.finditer(r"\S+", body):
        text = match.group(0)
        if re.fullmatch(r"[A-Za-z0-9]+", text) is None:
            return MIXED_TEXT
        tokens.append(
            _Token(
                text=text,
                start=body_offset + match.start(),
                end=body_offset + match.end(),
                value=text.casefold(),
            )
        )
    if not tokens:
        return UNSUPPORTED_FORM
    return tuple(tokens)


def _candidates(tokens: tuple[_Token, ...]) -> list[_Candidate]:
    return [
        *_call_candidates(tokens),
        *_passive_candidates(tokens),
        *_refer_candidates(tokens),
    ]


def _call_candidates(tokens: tuple[_Token, ...]) -> list[_Candidate]:
    if len(tokens) < 3 or tokens[0].value != "we" or tokens[1].value != "call":
        return []
    rest = _strip_article(tokens[2:])
    candidates: list[_Candidate] = []
    for index in range(1, len(rest) - 1):
        if rest[index].value != "the":
            continue
        definition = rest[:index]
        alias = rest[index + 1 :]
        if _is_definition(definition) and _is_alias(alias):
            candidates.append(_Candidate(definition, alias))
    return candidates


def _passive_candidates(tokens: tuple[_Token, ...]) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for index in range(len(tokens) - 1):
        if tokens[index].value != "is" or tokens[index + 1].value != "called":
            continue
        definition = _strip_article(tokens[:index])
        alias = _strip_article(tokens[index + 2 :])
        if _is_definition(definition) and _is_alias(alias):
            candidates.append(_Candidate(definition, alias))
    return candidates


def _refer_candidates(tokens: tuple[_Token, ...]) -> list[_Candidate]:
    if (
        len(tokens) < 4
        or tokens[0].value != "we"
        or tokens[1].value != "refer"
        or tokens[2].value != "to"
    ):
        return []
    rest = _strip_article(tokens[3:])
    candidates: list[_Candidate] = []
    for index in range(1, len(rest) - 1):
        if rest[index].value != "as":
            continue
        definition = rest[:index]
        alias = _strip_article(rest[index + 1 :])
        if _is_definition(definition) and _is_alias(alias):
            candidates.append(_Candidate(definition, alias))
    return candidates


def _strip_article(tokens: tuple[_Token, ...]) -> tuple[_Token, ...]:
    if tokens and tokens[0].value == "the":
        return tokens[1:]
    return tokens


def _is_definition(tokens: tuple[_Token, ...]) -> bool:
    return bool(tokens) and _phrase(tokens) in APPROVED_DEFINITIONS


def _is_alias(tokens: tuple[_Token, ...]) -> bool:
    return bool(tokens) and _phrase(tokens) in APPROVED_ALIASES


def _phrase(tokens: tuple[_Token, ...]) -> str:
    return " ".join(token.value for token in tokens)


def _has_valid_prefix(tokens: tuple[_Token, ...]) -> bool:
    """Whether a proper prefix of the body is already a complete approved parse."""
    return any(_candidates(tokens[:end]) for end in range(1, len(tokens)))


def _token_span(
    text: str, tokens: tuple[_Token, ...], prefix_bytes: int
) -> tuple[int, int]:
    begin = prefix_bytes + len(text[: tokens[0].start].encode("utf-8"))
    end = prefix_bytes + len(text[: tokens[-1].end].encode("utf-8"))
    return begin, end


__all__ = [
    "ALIAS_NOUNS",
    "ALIAS_WEEKDAYS",
    "APPROVED_ALIASES",
    "APPROVED_DEFINITIONS",
    "DEFINITION_CADENCES",
    "DEFINITION_KINDS",
    "DEFINITION_NOUNS",
    "MAX_BODY_TOKENS",
    "MAX_MESSAGE_BYTES",
    "RENDER_TEMPLATE",
    "SCHEMA",
    "Verdict",
    "VocabularyFact",
    "verify_project_vocabulary",
]
