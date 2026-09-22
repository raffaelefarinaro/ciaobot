"""Fact candidate v1: the structured, evidence-carrying form of a session fact.

The extraction model writes Markdown (``ciao/insights.py``) and
``ciao/memory_proposals.py`` routes those bullets to their destinations. That
pipeline answers "does this fact *look* like durable state?"; it never had a
place to record — or to check — the separate question of what in the
transcript actually says so.

This module is that place. A :class:`FactCandidate` is one extracted fact with
its provenance attached: the destination it claims, the transcript message ids
it cites, the excerpt it claims those ids contain, its temporal bounds, and
whether the supporting turn was one the user attended. Every field is either
read from the archive or left explicitly unknown — nothing here invents
evidence a bullet did not carry.

Three entry points produce candidates:

* :func:`candidates_from_markdown` — the compatibility parser for the
  Markdown archives already on disk. A bullet with an ``[idx=N]`` citation
  yields ``provenance="cited"``; a bullet without one yields
  ``provenance="unknown"`` and an empty ``source_message_ids``. Unknown
  provenance is recorded as unknown; it is never upgraded into a guess.
* :func:`candidates_from_structured` — for a model that returns JSON. Rows it
  cannot read are *not* dropped: each one comes back as a ``[review]``
  candidate carrying the parse error, alongside the reasons in the returned
  error list, so invalid structured output is reviewable rather than lost.
* :func:`candidate_from_proposal` — lifts an already-parsed
  :class:`~ciao.memory_proposals.MemoryProposal` without re-parsing it.

:func:`validate_candidate` then checks a candidate against the *normalized*
transcript — the same filtered records the extraction model was shown, split
into what the user actually typed (``spoken``) and what merely passed through
the turn (``quoted``: fenced blocks, blockquotes, tool output, and the
app's own injected context capsule). The checks are deliberately
machine-decidable, and every one of them answers a question a confidence
number cannot:

===========================  ==================================================
``no-citation``              the bullet cites nothing
``unknown-source-id``        it cites a turn the transcript does not contain
``not-a-user-turn``          every cited turn is assistant output or automation
``quote-not-found``          the claimed excerpt is not in the cited turn
``quoted-material-only``     the claim is only in quoted/tool/injected text
``evidence-mismatch``        the cited turn does not contain the claim
``negated-evidence``         the cited turn denies what the candidate asserts
``hypothetical-evidence``    the cited turn is an example, not an assertion
``superseded``               a later user turn corrects the cited one
``unknown-destination``      the destination is outside the known registry
``unparsable-candidate``     the structured row could not be read
``unknown-provenance``       there is nothing to check the citation against
===========================  ==================================================

A failing verdict is a routing signal, not a delete: callers queue the
candidate for review. ``unknown-provenance`` is the one verdict that is not a
failure — a text-mode extraction is told not to emit indices at all, so
demanding them would queue every fact in a legacy archive for no evidence
gain. It is reported so the caller can record *why* a fact was accepted
without an id rather than pretending it had one.

:func:`render_insights_markdown` renders candidates back to the human-readable
``## Session insights`` shape, so the structure is the source of truth and the
Markdown is a view of it.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Versions ──────────────────────────────────────────────────────────────

SCHEMA_VERSION = "fact-candidate/v1"
"""Bumped when the candidate record's field set changes. Stamped onto every
candidate and into the region receipt, so a fact saved today can be read back
against the schema it was extracted under."""

EXTRACTION_VERSION_MARKDOWN = "insights-markdown/v1"
"""The tagged-Markdown extraction contract in ``ciao/insights.py``."""

EXTRACTION_VERSION_STRUCTURED = "insights-structured/v1"
"""A model that returns candidate rows as JSON rather than Markdown."""

POLICY_VERSION = "evidence-policy/v1"
"""The rule set :func:`validate_candidate` applies. Recorded next to the
saved fact so an accepted fact says which policy admitted it."""


# ── Provenance and verdict vocabulary ─────────────────────────────────────

PROVENANCE_CITED = "cited"
PROVENANCE_UNKNOWN = "unknown"

OK = "ok"
UNKNOWN_PROVENANCE = "unknown-provenance"
NO_CITATION = "no-citation"
UNKNOWN_SOURCE_ID = "unknown-source-id"
NOT_USER_TURN = "not-a-user-turn"
QUOTE_NOT_FOUND = "quote-not-found"
QUOTED_MATERIAL_ONLY = "quoted-material-only"
EVIDENCE_MISMATCH = "evidence-mismatch"
NEGATED_EVIDENCE = "negated-evidence"
HYPOTHETICAL_EVIDENCE = "hypothetical-evidence"
SUPERSEDED = "superseded"
UNKNOWN_DESTINATION = "unknown-destination"
UNPARSABLE = "unparsable-candidate"

UNREADABLE_SECTION = "Unreadable extraction output"
"""The insights section an unreadable structured row is rendered under.

Named here rather than spelled out at both ends: ``candidates_from_structured``
writes it and ``memory_proposals.propose_from_insights`` reads it, and a typo
in either copy would turn "queued for review" back into a silent drop."""


class CandidateSchemaError(ValueError):
    """A structured row that does not parse as a fact candidate v1 record."""


# ── The record ────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class FactCandidate:
    """One extracted fact plus the evidence it claims.

    ``text`` is the bullet as written, minus only the citation and
    destination tags — the temporal tags stay inline so a candidate renders
    back to the exact bullet it was parsed from, and :attr:`as_of` /
    :attr:`expires` surface the same dates as fields for policy checks.

    ``attended`` is tri-state on purpose: ``True`` when a cited turn is one
    the user typed, ``False`` when every cited turn is assistant output or an
    automation turn, and ``None`` when there is no transcript to decide it
    from. ``None`` is *not* ``False``; a compatibility-parsed archive has to
    be able to say "unknown" without that reading as "the assistant made it
    up".
    """

    text: str
    destination: str
    payload: str = ""
    section: str = ""
    source_message_ids: tuple[int, ...] = ()
    evidence_excerpt: str = ""
    as_of: str = ""
    expires: str = ""
    attended: bool | None = None
    provenance: str = PROVENANCE_UNKNOWN
    schema: str = SCHEMA_VERSION
    extraction_version: str = EXTRACTION_VERSION_MARKDOWN
    policy_version: str = POLICY_VERSION
    parse_error: str = ""

    def as_bullet(self) -> str:
        """Render back to the ``## Session insights`` bullet shape.

        The citation goes before the destination tag, which is the order both
        extraction prompts ask for. A candidate with unknown provenance emits
        no citation at all rather than a fabricated one — the round trip has
        to be able to represent "this archive never said".
        """
        parts = [self.text.strip()]
        if self.source_message_ids:
            joined = ",".join(str(i) for i in self.source_message_ids)
            parts.append(f"[idx={joined}]")
        if self.destination == "people" and self.payload:
            parts.append(f"[people: {self.payload}]")
        elif self.destination == "project" and self.payload:
            parts.append(f"[project: {self.payload}]")
        elif self.destination:
            parts.append(f"[{self.destination}]")
        return "- " + " ".join(p for p in parts if p)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe record. Round-trips through :func:`candidate_from_dict`."""
        return {
            "schema": self.schema,
            "text": self.text,
            "destination": self.destination,
            "payload": self.payload,
            "section": self.section,
            "source_message_ids": list(self.source_message_ids),
            "evidence_excerpt": self.evidence_excerpt,
            "as_of": self.as_of,
            "expires": self.expires,
            "attended": self.attended,
            "provenance": self.provenance,
            "extraction_version": self.extraction_version,
            "policy_version": self.policy_version,
            "parse_error": self.parse_error,
        }


@dataclass(slots=True, frozen=True)
class Verdict:
    """The outcome of validating one candidate.

    ``ok`` is the routing answer; ``code`` and ``reason`` are why. A verdict
    is always both — a bare boolean would make "queued because the id was
    fabricated" and "queued because a later turn corrected it" the same event
    in the queue, which is exactly what the decision history could not say
    before.
    """

    ok: bool
    code: str = OK
    reason: str = ""
    source_message_ids: tuple[int, ...] = ()
    attended: bool | None = None

    def as_row(self) -> dict[str, Any]:
        """The shape ``memory_proposals`` records alongside a queued fact."""
        return {
            "code": self.code,
            "reason": self.reason,
            "source_message_ids": list(self.source_message_ids),
            "attended": self.attended,
            "policy_version": POLICY_VERSION,
            "schema": SCHEMA_VERSION,
        }


# ── The normalized transcript ─────────────────────────────────────────────


# The capsule the app injects at the head of a user turn. It is the app's
# words, not the user's: a fact "supported" only by capsule text is the
# machinery describing itself, which is precisely the maintenance-prompt
# material both extraction prompts are told never to extract.
_INJECTED_CONTEXT_RE = re.compile(
    r"(?s)\[CIAO_CONTEXT_BEGIN\].*?(?:\[CIAO_CONTEXT_END\]|\Z)"
)
_FENCE_RE = re.compile(r"(?sm)^[ \t]*```.*?(?:^[ \t]*```|\Z)")
_BLOCKQUOTE_RE = re.compile(r"(?m)^[ \t]*>.*$")


@dataclass(slots=True, frozen=True)
class TranscriptTurn:
    """One filtered transcript record, split by who is speaking.

    ``spoken`` is the turn's own prose. ``quoted`` is everything that merely
    passed through it — fenced blocks, blockquoted lines, tool input and tool
    output, and the injected context capsule. The split is what lets the
    validator refuse a fact whose only support is a command the user pasted
    or an instruction embedded in a document they shared.
    """

    idx: int
    role: str
    attended: bool
    spoken: str
    quoted: str


@dataclass(slots=True, frozen=True)
class NormalizedTranscript:
    """The filtered transcript indexed by the ids the prompt tells models to cite."""

    turns: tuple[TranscriptTurn, ...]
    _by_idx: dict[int, TranscriptTurn] = field(default_factory=dict, repr=False)

    def turn(self, idx: int) -> TranscriptTurn | None:
        return self._by_idx.get(idx)

    @property
    def known(self) -> frozenset[int]:
        return frozenset(self._by_idx)

    @property
    def attended_user(self) -> frozenset[int]:
        return frozenset(
            turn.idx
            for turn in self.turns
            if turn.role == "user" and turn.attended
        )

    def user_turns_after(self, idx: int) -> tuple[TranscriptTurn, ...]:
        return tuple(
            turn
            for turn in self.turns
            if turn.idx > idx and turn.role == "user" and turn.attended
        )


def normalize_transcript(filtered_jsonl: str) -> NormalizedTranscript | None:
    """Index the filtered session JSONL by citation id, or None when empty.

    None means "there is nothing to check against", never "nothing is
    supported": the text-mode extraction prompt forbids ``[idx=N]`` entirely,
    and a legacy archive re-processed without its session blob carries no
    indices either.
    """
    turns: list[TranscriptTurn] = []
    for line in filtered_jsonl.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        idx = record.get("idx")
        # `isinstance(True, int)` is True in Python, so a bool `idx` would
        # index turn 1 and hand a fabricated citation a real turn.
        if isinstance(idx, bool) or not isinstance(idx, int):
            continue
        role = str(record.get("type") or "")
        spoken_parts, quoted_parts = _split_blocks(record.get("content"))
        spoken = "\n".join(spoken_parts)
        # Strip the pass-through material out of the prose and keep it: the
        # text is still evidence of *something*, just not of the user
        # asserting it in their own words.
        spoken, carved = _carve_passthrough(spoken)
        quoted = "\n".join([*quoted_parts, *carved])
        turns.append(TranscriptTurn(
            idx=idx,
            role=role,
            attended=role == "user" and not bool(record.get("unattended")),
            spoken=spoken,
            quoted=quoted,
        ))
    if not turns:
        return None
    return NormalizedTranscript(
        turns=tuple(turns),
        _by_idx={turn.idx: turn for turn in turns},
    )


def _split_blocks(content: object) -> tuple[list[str], list[str]]:
    """Separate a record's spoken text from its tool traffic."""
    spoken: list[str] = []
    quoted: list[str] = []
    if isinstance(content, str):
        return ([content] if content.strip() else []), []
    if not isinstance(content, list):
        return [], []
    for block in content:
        if isinstance(block, str):
            if block.strip():
                spoken.append(block)
            continue
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype in {"text", "thinking"}:
            text = block.get("text") or block.get("thinking") or ""
            if isinstance(text, str) and text.strip():
                spoken.append(text)
            continue
        # tool_use / tool_result and anything else a provider improvises:
        # material the turn carried, never the speaker's own assertion.
        rendered = json.dumps(block, ensure_ascii=False, default=str)
        quoted.append(rendered)
    return spoken, quoted


def _carve_passthrough(text: str) -> tuple[str, list[str]]:
    """Pull the injected capsule, fenced blocks and blockquotes out of prose."""
    carved: list[str] = []

    def _take(match: re.Match[str]) -> str:
        carved.append(match.group(0))
        return "\n"

    text = _INJECTED_CONTEXT_RE.sub(_take, text)
    text = _FENCE_RE.sub(_take, text)
    text = _BLOCKQUOTE_RE.sub(_take, text)
    return text, carved


# ── Claim/evidence matching ───────────────────────────────────────────────


# Deliberately small: the list only has to stop function words from counting
# as shared content. Anything it misses costs a little precision, never
# correctness — a shared stopword alone cannot satisfy the overlap rule
# below, which needs two matching terms once a claim has any to spare.
_STOPWORDS = frozenset("""
a an and are as at be been being but by can could did do does for from had has
have how i if in is it its me my no not of on or our should so than that the
their them then there these they this to us was we were what when which who
will with would you your always never also just really very please thanks
""".split())

# Word characters plus *internal* punctuation only. A trailing period must not
# join the token: "Thursdays." and "Thursdays" would stem differently and a
# faithful rephrasing of the user's sentence would read as unsupported.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+(?:[.'+-][A-Za-z0-9_]+)*")
_NUMBER_RE = re.compile(r"\d[\d.,]*")
_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?")

# Negation markers. `n't` covers don't/doesn't/isn't/won't/didn't in one, and
# the multi-word forms are matched before tokenization so "no longer" is not
# read as the pair of stopwords it tokenizes to.
_NEGATION_PHRASES = (
    "no longer",
    "not any more",
    "not anymore",
    "stop using",
    "stopped using",
    "changed my mind",
)
_NEGATION_TOKENS = frozenset({
    "not", "never", "no", "none", "without", "avoid", "avoids", "avoiding",
    "stop", "stops", "stopped", "dropped", "drop", "cancel", "cancelled",
    "canceled", "wrong", "incorrect",
})

# Only unambiguous framing counts as hypothetical. A bare "if" is left out on
# purpose: "if a build fails, always retry twice" is a standing rule the user
# really stated, and treating every conditional as an example would queue the
# rules this system exists to keep.
_HYPOTHETICAL_PHRASES = (
    "for example",
    "for instance",
    "e.g.",
    "hypothetically",
    "what if",
    "suppose ",
    "supposing ",
    "imagine ",
    "pretend ",
    "in theory",
    "let's say",
    "lets say",
    "say i ",
    "say we ",
    "just an example",
    "as an example",
)

# Explicit correction framing. Paired with claim overlap, this is what makes a
# later turn *supersede* an earlier one rather than merely mention it again.
_CORRECTION_PHRASES = (
    "actually",
    "correction",
    "i meant",
    "i misspoke",
    "scratch that",
    "ignore that",
    "no longer",
    "changed my mind",
    "not any more",
    "not anymore",
    "that was wrong",
    "i was wrong",
)


def _stem(token: str) -> str:
    """Crude suffix stripping so "deploy" matches "deploys" and "deployed".

    Not linguistics — just enough that a faithful present-tense rephrasing of
    what the user typed still counts as the same claim. Short tokens are left
    alone; stripping them turns distinct words into the same stem.
    """
    lowered = token.lower()
    if len(lowered) <= 4:
        return lowered
    for suffix in ("ing", "ies", "ed", "es", "s", "ly"):
        if lowered.endswith(suffix) and len(lowered) - len(suffix) >= 3:
            base = lowered[: -len(suffix)]
            return base + "y" if suffix == "ies" else base
    return lowered


def _terms(text: str) -> frozenset[str]:
    """The distinctive stems in a piece of text."""
    return frozenset(
        _stem(token)
        for token in _TOKEN_RE.findall(text)
        if token.lower() not in _STOPWORDS and len(token) > 2
    )


def _is_negative(text: str) -> bool:
    lowered = text.lower()
    if any(phrase in lowered for phrase in _NEGATION_PHRASES):
        return True
    if "n't" in lowered:
        return True
    tokens = {t.lower() for t in _TOKEN_RE.findall(lowered)}
    return bool(tokens & _NEGATION_TOKENS)


def _is_hypothetical(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _HYPOTHETICAL_PHRASES)


def _is_correction(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _CORRECTION_PHRASES)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.findall(text) if s.strip()]


def _overlap(claim: frozenset[str], text: str) -> int:
    return len(claim & _terms(text))


def _best_sentence(claim: frozenset[str], text: str) -> tuple[str, int]:
    """The sentence in ``text`` that shares the most stems with the claim."""
    best, score = "", 0
    for sentence in _sentences(text):
        shared = _overlap(claim, sentence)
        if shared > score:
            best, score = sentence, shared
    if not best:
        # No sentence beat zero; fall back to the whole text so the caller
        # still sees the (zero) score rather than a spurious empty match.
        return text.strip(), _overlap(claim, text)
    return best, score


def _supports(claim: frozenset[str], shared: int) -> bool:
    """Whether a shared-term count is enough to call a sentence the source.

    One shared term is enough for a two-term claim ("Uses Postgres"); a claim
    with more terms to spare must match at least two, so a single incidental
    word cannot pass a long fabricated sentence off as grounded. The bar is
    intentionally low — this is a fabrication check, not a paraphrase scorer,
    and the ambiguous middle is queued for a human either way.
    """
    if not claim:
        return False
    return shared >= (1 if len(claim) <= 2 else 2)


def _numbers(text: str) -> set[str]:
    """Digit runs with separators stripped, so 1,100 meets 1100."""
    return {re.sub(r"[.,]", "", match) for match in _NUMBER_RE.findall(text)}


def _normalize_quote(text: str) -> str:
    """Casefolded, whitespace-collapsed, quote-normalized text for substring checks."""
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    return " ".join(text.split()).casefold()


# ── Destination registry ──────────────────────────────────────────────────


def known_destinations() -> frozenset[str]:
    """The destination vocabulary this install will route to.

    Read from :mod:`ciao.memory_proposals` rather than restated, so a bullet
    is validated against the same registry the router actually owns.
    """
    from ciao.memory_proposals import DESTINATIONS

    return frozenset(DESTINATIONS)


def _destination_verdict(candidate: FactCandidate) -> Verdict | None:
    """Refuse a destination outside the registry. None when it is fine."""
    destination = (candidate.destination or "").strip().lower()
    if destination not in known_destinations():
        return Verdict(
            ok=False,
            code=UNKNOWN_DESTINATION,
            reason=(
                f"destination {candidate.destination!r} is not one this "
                "workspace routes to"
            ),
            source_message_ids=candidate.source_message_ids,
            attended=candidate.attended,
        )
    if destination in {"memory", "profile"}:
        from ciao.memory_tool import resolve_region

        try:
            resolve_region(destination)
        except ValueError:
            return Verdict(
                ok=False,
                code=UNKNOWN_DESTINATION,
                reason=f"no bounded region named {destination!r}",
                source_message_ids=candidate.source_message_ids,
                attended=candidate.attended,
            )
    if destination == "people" and not candidate.payload.strip():
        return Verdict(
            ok=False,
            code=UNKNOWN_DESTINATION,
            reason="a [people] fact carries no person name",
            source_message_ids=candidate.source_message_ids,
            attended=candidate.attended,
        )
    return None


# ── Validation ────────────────────────────────────────────────────────────


def validate_candidate(
    candidate: FactCandidate,
    transcript: NormalizedTranscript | None,
    *,
    claim_text: str = "",
) -> Verdict:
    """Check one candidate against the transcript it claims to come from.

    ``claim_text`` overrides the text the claim terms are read from. Callers
    that promote only part of a bullet — ``memory_proposals`` writes the
    ``Durable rule:`` clause, not the sentence around it — pass the text that
    will actually be saved, so the evidence is weighed against the assertion
    being made rather than the narration around it.

    Never raises on malformed input: a candidate the parser already marked
    unreadable comes back as an ``unparsable-candidate`` verdict, because a
    validator that throws would put the caller back on the path where a fact
    disappears with the exception.
    """
    if candidate.parse_error:
        return Verdict(
            ok=False,
            code=UNPARSABLE,
            reason=candidate.parse_error,
            source_message_ids=candidate.source_message_ids,
        )

    bad_destination = _destination_verdict(candidate)
    if bad_destination is not None:
        return bad_destination

    if transcript is None:
        # Nothing to check against. Reported rather than silently passed, so
        # an accepted fact still records that its provenance is unknown.
        return Verdict(
            ok=True,
            code=UNKNOWN_PROVENANCE,
            reason="no transcript is available to check this citation against",
            source_message_ids=candidate.source_message_ids,
            attended=None,
        )

    ids = candidate.source_message_ids
    if not ids:
        return Verdict(
            ok=False,
            code=NO_CITATION,
            reason="bullet cites no source turn",
            attended=None,
        )

    unknown = sorted(i for i in ids if i not in transcript.known)
    if unknown:
        return Verdict(
            ok=False,
            code=UNKNOWN_SOURCE_ID,
            reason=f"citation idx={unknown[0]} names no turn in the transcript",
            source_message_ids=ids,
            attended=None,
        )

    attended_ids = tuple(i for i in ids if i in transcript.attended_user)
    if not attended_ids:
        return Verdict(
            ok=False,
            code=NOT_USER_TURN,
            reason="no cited turn is one the user typed",
            source_message_ids=ids,
            attended=False,
        )

    claim = _terms(claim_text or candidate.text)

    excerpt = _normalize_quote(candidate.evidence_excerpt)
    if excerpt:
        spoken_hit = any(
            excerpt in _normalize_quote(_turn_or_empty(transcript, i).spoken)
            for i in attended_ids
        )
        if not spoken_hit:
            quoted_hit = any(
                excerpt in _normalize_quote(_turn_or_empty(transcript, i).quoted)
                for i in ids
            )
            return Verdict(
                ok=False,
                code=QUOTED_MATERIAL_ONLY if quoted_hit else QUOTE_NOT_FOUND,
                reason=(
                    "the quoted evidence appears only in pass-through material "
                    "(tool output, a fenced block, or injected context)"
                    if quoted_hit
                    else "the quoted evidence is not in any cited turn"
                ),
                source_message_ids=ids,
                attended=True,
            )

    # Which cited user turn actually carries the claim, and in what framing.
    best_turn: TranscriptTurn | None = None
    best_sentence = ""
    best_score = 0
    for i in attended_ids:
        turn = _turn_or_empty(transcript, i)
        sentence, score = _best_sentence(claim, turn.spoken)
        if score > best_score or best_turn is None:
            best_turn, best_sentence, best_score = turn, sentence, score

    if not _supports(claim, best_score):
        # Nothing in what the user typed. Before calling it a fabrication,
        # check the pass-through material: a claim that is only in a pasted
        # document, a tool result or the injected capsule is a real string in
        # the transcript that the user never asserted — the shape a quoted
        # instruction takes when it tries to become a memory.
        for i in ids:
            turn = _turn_or_empty(transcript, i)
            _, quoted_score = _best_sentence(claim, turn.quoted)
            if _supports(claim, quoted_score):
                return Verdict(
                    ok=False,
                    code=QUOTED_MATERIAL_ONLY,
                    reason=(
                        f"idx={i} carries this claim only as quoted or tool "
                        "material, not as something the user stated"
                    ),
                    source_message_ids=ids,
                    attended=True,
                )
        return Verdict(
            ok=False,
            code=EVIDENCE_MISMATCH,
            reason="no cited user turn contains this claim",
            source_message_ids=ids,
            attended=True,
        )

    assert best_turn is not None  # `attended_ids` is non-empty by this point.

    if _is_hypothetical(best_sentence):
        return Verdict(
            ok=False,
            code=HYPOTHETICAL_EVIDENCE,
            reason=(
                f"idx={best_turn.idx} frames this as an example, not a "
                "statement about the user"
            ),
            source_message_ids=ids,
            attended=True,
        )

    if _is_negative(best_sentence) and not _is_negative(claim_text or candidate.text):
        return Verdict(
            ok=False,
            code=NEGATED_EVIDENCE,
            reason=(
                f"idx={best_turn.idx} denies what the candidate asserts"
            ),
            source_message_ids=ids,
            attended=True,
        )

    if _is_negative(claim_text or candidate.text) and not _is_negative(best_sentence):
        # The mirror image: a negative claim citing positive evidence. Only
        # the first direction was checked, so "does not use X" citing "I
        # use X" read as supported and the exact opposite fact was
        # promotable into bounded memory.
        return Verdict(
            ok=False,
            code=NEGATED_EVIDENCE,
            reason=(
                f"idx={best_turn.idx} asserts what the candidate denies"
            ),
            source_message_ids=ids,
            attended=True,
        )

    claim_numbers = _numbers(claim_text or candidate.text)
    if claim_numbers and not claim_numbers <= _numbers(best_sentence):
        # Terms overlap but the figures do not: "950 EUR" is supported by a
        # turn saying "1100 EUR" under the two-term bar, and a stale or
        # fabricated number would pass the gate into durable state. A
        # refusal here queues the row for a human rather than dropping it,
        # so a paraphrased count ("two" for "2") costs review, not loss.
        missing = sorted(claim_numbers - _numbers(best_sentence))[0]
        return Verdict(
            ok=False,
            code=EVIDENCE_MISMATCH,
            reason=(
                f"idx={best_turn.idx} does not carry the claimed figure {missing}"
            ),
            source_message_ids=ids,
            attended=True,
        )

    # After the turn that supplied the evidence, not after the largest cited
    # id: a candidate citing both the original and its correction used to
    # start past both and pass the stale fact.
    superseding = _superseding_turn(transcript, claim, best_turn.idx)
    if superseding is not None:
        return Verdict(
            ok=False,
            code=SUPERSEDED,
            reason=(
                f"idx={superseding.idx} corrects the cited turn; the candidate "
                "records the superseded version"
            ),
            source_message_ids=ids,
            attended=True,
        )

    return Verdict(
        ok=True,
        code=OK,
        reason="",
        source_message_ids=ids,
        attended=True,
    )


def _turn_or_empty(
    transcript: NormalizedTranscript, idx: int
) -> TranscriptTurn:
    turn = transcript.turn(idx)
    if turn is not None:
        return turn
    return TranscriptTurn(idx=idx, role="", attended=False, spoken="", quoted="")


def _superseding_turn(
    transcript: NormalizedTranscript,
    claim: frozenset[str],
    after: int,
) -> TranscriptTurn | None:
    """A later user turn that explicitly corrects the cited one, if any.

    Overlap alone is not enough — a user restating the same fact is not a
    correction — so the later turn must both talk about the same claim and
    carry explicit correction framing or flip the claim's polarity. The
    candidate that cites the *correcting* turn is unaffected, which is what
    makes an explicit correction traceable end to end: the saved fact points
    at the turn that established it.
    """
    for turn in transcript.user_turns_after(after):
        sentence, score = _best_sentence(claim, turn.spoken)
        if not _supports(claim, score):
            continue
        if _is_correction(sentence) or _is_negative(sentence):
            return turn
    return None


# ── Compatibility parser: Markdown archives ───────────────────────────────


_AS_OF_RE = re.compile(r"\[as-of:\s*(\d{4}-\d{2}-\d{2})\]", re.IGNORECASE)
_EXPIRES_RE = re.compile(r"\[expires:\s*(\d{4}-\d{2}-\d{2})\]", re.IGNORECASE)


def candidates_from_markdown(insights_md: str) -> list[FactCandidate]:
    """Parse an existing ``## Session insights`` body into candidates.

    This is the compatibility path for every archive already on disk, and it
    records what the bullet says and nothing more. A bullet with no ``[idx=N]``
    tag becomes ``provenance="unknown"`` with no source ids and
    ``attended=None`` — the archive genuinely does not know, and inventing a
    plausible turn id here would manufacture exactly the evidence chain this
    module exists to check.
    """
    from ciao.memory_proposals import propose_from_insights

    return [candidate_from_proposal(p) for p in propose_from_insights(insights_md)]


def candidate_from_proposal(proposal: Any) -> FactCandidate:
    """Lift an already-parsed :class:`~ciao.memory_proposals.MemoryProposal`.

    Typed loosely to keep the import one-directional: ``memory_proposals``
    calls into this module during validation, so a module-level import of its
    dataclass here would close the cycle.
    """
    text = str(proposal.text)
    ids = tuple(int(i) for i in getattr(proposal, "citations", ()) or ())
    as_of = _AS_OF_RE.search(text)
    expires = _EXPIRES_RE.search(text)
    return FactCandidate(
        text=text,
        destination=str(proposal.target),
        payload=str(getattr(proposal, "payload", "") or ""),
        section=str(getattr(proposal, "source_section", "") or ""),
        source_message_ids=ids,
        evidence_excerpt="",
        as_of=as_of.group(1) if as_of else "",
        expires=expires.group(1) if expires else "",
        attended=None,
        provenance=PROVENANCE_CITED if ids else PROVENANCE_UNKNOWN,
        extraction_version=EXTRACTION_VERSION_MARKDOWN,
    )


# ── Structured extraction ─────────────────────────────────────────────────


def candidate_from_dict(row: object) -> FactCandidate:
    """Read one structured candidate row. Raises on a shape we cannot trust.

    Permissive about what it accepts and strict about what it claims: unknown
    keys are ignored and missing optional fields default, but a row without
    usable ``text`` or ``destination`` raises rather than producing a
    candidate with invented fields. Source ids must be positive integers —
    the extraction prompt numbers turns from 1 and forbids ``[idx=0]``, so a
    0 or a negative id is a malformed row, not a citation.
    """
    if not isinstance(row, dict):
        raise CandidateSchemaError(f"expected an object, got {type(row).__name__}")
    schema = str(row.get("schema") or SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise CandidateSchemaError(f"unsupported candidate schema {schema!r}")
    text = str(row.get("text") or "").strip()
    if not text:
        raise CandidateSchemaError("row carries no fact text")
    destination = str(row.get("destination") or "").strip().lower()
    if not destination:
        raise CandidateSchemaError("row carries no destination")

    raw_ids = row.get("source_message_ids")
    ids: list[int] = []
    if raw_ids is not None:
        if not isinstance(raw_ids, list):
            raise CandidateSchemaError("source_message_ids is not a list")
        for value in raw_ids:
            if isinstance(value, bool) or not isinstance(value, int):
                raise CandidateSchemaError(
                    f"source_message_ids holds a non-integer id {value!r}"
                )
            if value < 1:
                raise CandidateSchemaError(
                    f"source_message_ids holds an out-of-range id {value!r}"
                )
            ids.append(value)

    attended_raw = row.get("attended")
    if attended_raw is None:
        attended: bool | None = None
    elif isinstance(attended_raw, bool):
        attended = attended_raw
    else:
        raise CandidateSchemaError("attended is not a boolean")

    return FactCandidate(
        text=text,
        destination=destination,
        payload=str(row.get("payload") or ""),
        section=str(row.get("section") or ""),
        source_message_ids=tuple(sorted(set(ids))),
        evidence_excerpt=str(row.get("evidence_excerpt") or ""),
        as_of=str(row.get("as_of") or ""),
        expires=str(row.get("expires") or ""),
        attended=attended,
        provenance=PROVENANCE_CITED if ids else PROVENANCE_UNKNOWN,
        extraction_version=str(
            row.get("extraction_version") or EXTRACTION_VERSION_STRUCTURED
        ),
        policy_version=str(row.get("policy_version") or POLICY_VERSION),
    )


def candidates_from_structured(raw: str) -> tuple[list[FactCandidate], list[str]]:
    """Parse a model's JSON candidate array. Never loses a row silently.

    Returns ``(candidates, errors)``. A row that does not parse still comes
    back as a candidate — addressed to ``review``, carrying its
    ``parse_error`` and whatever text could be recovered — so invalid
    structured output lands in the review queue instead of vanishing. When
    the payload as a whole is unreadable, ``candidates`` is empty and
    ``errors`` says why; the caller's fallback is the Markdown path, not a
    silent success.
    """
    text = raw.strip()
    if not text:
        return [], ["structured extraction returned nothing"]
    # Models fence JSON about as often as not.
    fenced = re.match(r"(?s)^```(?:json)?\s*(.*?)\s*```$", text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except ValueError as exc:
        return [], [f"structured extraction is not valid JSON: {exc}"]
    if isinstance(payload, dict):
        payload = payload.get("facts", payload.get("candidates"))
    if not isinstance(payload, list):
        return [], ["structured extraction is not a list of candidate rows"]

    candidates: list[FactCandidate] = []
    errors: list[str] = []
    for position, row in enumerate(payload, start=1):
        try:
            candidates.append(candidate_from_dict(row))
        except CandidateSchemaError as exc:
            reason = f"row {position}: {exc}"
            errors.append(reason)
            recovered = ""
            if isinstance(row, dict):
                recovered = str(row.get("text") or "").strip()
            candidates.append(FactCandidate(
                text=recovered or f"(unreadable candidate row {position})",
                destination="review",
                section=UNREADABLE_SECTION,
                provenance=PROVENANCE_UNKNOWN,
                extraction_version=EXTRACTION_VERSION_STRUCTURED,
                parse_error=str(exc),
            ))
    return candidates, errors


# ── Rendering ─────────────────────────────────────────────────────────────


def render_insights_markdown(
    candidates: list[FactCandidate],
    *,
    sections: tuple[str, ...] = (),
) -> str:
    """Render candidates as the human-readable ``## Session insights`` body.

    Section order follows ``sections`` when given, then any remaining section
    in first-seen order, so a caller can pin the prompt's canonical order
    without this module restating it. A candidate with no section lands under
    a plain ``## Facts`` heading rather than being dropped.
    """
    grouped: dict[str, list[FactCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.section.strip() or "Facts", []).append(candidate)

    ordered = [s for s in sections if s in grouped]
    ordered.extend(s for s in grouped if s not in ordered)

    blocks: list[str] = []
    for heading in ordered:
        lines = [f"## {heading}"]
        lines.extend(candidate.as_bullet() for candidate in grouped[heading])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
