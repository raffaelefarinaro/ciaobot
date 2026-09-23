"""Route session-insight facts to their real destination.

The post-archive insights pipeline (``ciao/insights.py``) appends a
``## Session insights`` section to each archived chat. That section already
contains the high-signal facts we'd want in memory — errors, decisions, new
entities, user corrections, reusable snippets.

This module turns those facts into *destination-addressed* proposals. Each
bullet may carry a trailing destination tag written by the extraction model:

* ``[memory]``   — cross-project preference/environment/lesson → the
  ``ciao:memory`` region of the workspace ``AGENTS.md``.
* ``[profile]``  — identity/communication style → the ``ciao:profile`` region.
* ``[project]``  — true only within this project → the project's canonical
  doc (folded at archive time by :mod:`ciao.project_doc_update`; queued with
  the doc path only when the fold did not consume it).
* ``[people: <Name>]`` — durable fact about a person → ``People/<Name>.md``.
* ``[learnings]`` — reusable how-to knowledge → ``Workspace/Learnings.md``.
* ``[review]``   — the model was not sure → waits for human or curator review.

Untagged bullets fall back to conservative defaults derived from their
section (corrections → memory, operator identity → profile, everything else
→ review): a missing tag *is* uncertainty.

Auto-apply is the default posture (``auto_promote_memory``): every confident,
state-shaped fact is written straight to its destination at archive time —
regions through the :mod:`ciao.memory_audit` event-shape guard, people notes
as stubs when absent, learnings as dated bullets. Anything the guards reject,
any destination whose write fails, every region fact whose write-time
reconcile came back unusable, every region fact whose ``[idx=N]`` citation
the transcript does not support, and every ``[review]`` bullet land in the
queue file instead: ``<workspace-vault>/Workspace/Memory-Proposals.md``.

Shape is not evidence. The guards above ask whether a fact *looks* like
durable state; :func:`unsupported_region_facts` asks the separate question of
whether any turn the user actually typed says so, which a fluent model
satisfies on formatting alone otherwise. The structured form of that question
— the fact candidate v1 record, the normalized transcript it is checked
against, and the verdict codes — lives in :mod:`ciao.fact_candidates`; this
module owns the routing decision that follows from it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from urllib.parse import unquote
from dataclasses import dataclass, replace
from datetime import UTC, datetime, date
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

from ciao.curation_run import curation_in_progress
from ciao.vault_links import MARKDOWN_LINK_RE, WIKILINK_RE

logger = logging.getLogger(__name__)


_PROPOSALS_RELATIVE = "Workspace/Memory-Proposals.md"
_LEARNINGS_RELATIVE = "Workspace/Learnings.md"
_PEOPLE_DIR = "People"


# ── Typed decision statuses ───────────────────────────────────────────────
#
# The reconcile path used to speak in bare strings and a bare ``None``, and
# ``None`` carried two opposite meanings: "no reconcile was needed" and "the
# reconcile could not decide". Downstream they read the same, so a failed call
# took the plain append path and left an obsolete fact asserted beside its
# replacement in always-loaded memory. Every state the path can be in is named
# here instead, so the compiler — not a reader — checks that each one is
# handled.


ReconcileAction = Literal["add", "covered", "update", "defer"]
"""What the write-time reconcile decided about one candidate fact.

``add`` is new information (including the deterministic first entry into an
empty region), ``covered`` an exact or semantic duplicate, ``update`` a
validated supersession of exactly one existing entry, and ``defer`` every
uncertain state: a failed or unparseable reconciliation, a stale snapshot, a
row we could not read, and a fact the transcript does not support.
"""


class ReconcileDecision(TypedDict):
    """One candidate fact's decision row.

    ``index``/``text``/``old`` belong to ``update``: the 1-based entry to
    replace, the merged replacement, and the entry that index named *in the
    snapshot the model actually saw*, which the apply step re-checks against
    the live region before writing.

    ``reason`` and ``competing`` belong to ``defer``, and are the two things a
    deferral has to carry to be resolvable rather than merely safe: why this
    fact was not applied, and the region entries it may be in conflict with.
    ``evidence`` also belongs to ``defer``: the fact-candidate verdict row
    (:meth:`ciao.fact_candidates.Verdict.as_row`) behind an evidence refusal,
    which names *which* check said no rather than only that something did.
    """

    action: ReconcileAction
    index: NotRequired[int]
    text: NotRequired[str]
    old: NotRequired[str]
    reason: NotRequired[str]
    competing: NotRequired[list[str]]
    evidence: NotRequired[dict[str, Any]]


RegionDecisions = dict[str, ReconcileDecision]
"""``_decision_key`` (region + promotable fact text) → that fact's decision."""


PromotionOutcome = Literal[
    "written", "duplicate", "conflict", "failed", "unshaped", "deferred"
]
"""What happened to one region-bound fact.

``written`` and ``duplicate`` are settled — the fact is in the region either
way and leaves the queue. ``conflict`` (the destination moved under a
concurrent writer), ``failed`` (the write or its lock failed), ``unshaped``
(not state-shaped text) and ``deferred`` (nothing about this fact could be
trusted enough to write it unattended) all keep the fact queued.
"""


def _defer(
    reason: str,
    competing: list[str] | None = None,
    *,
    evidence: dict[str, Any] | None = None,
) -> ReconcileDecision:
    """A defer row carrying its reason and the entries it competes with.

    Every deferral is built here so none of them can be emitted bare: a queued
    fact with no reason is indistinguishable from an ordinary review row, and
    without the competing snapshot neither a human nor a retry knows what it
    was weighed against.

    ``evidence`` attaches the fact-candidate verdict row behind an evidence
    refusal, which carries the verdict code, the cited ids and the policy
    version alongside the human-readable reason.
    """
    row: ReconcileDecision = {"action": "defer", "reason": reason}
    if competing:
        # Capped: the snapshot is diagnostic, and a whole region in a log line
        # (or a run's extra) buries the reason it is attached to.
        row["competing"] = [_one_line(entry) for entry in competing[:5]]
    if evidence:
        row["evidence"] = evidence
    return row


@dataclass(slots=True, frozen=True)
class DeferredFact:
    """One fact the apply step queued instead of writing, and why.

    ``apply_proposals`` fills these into its optional ``deferrals``
    out-parameter. The count alone (``stats["deferred"]``) says a reconcile
    backend is down or a model is asserting uncited facts, but not which facts
    or against what — which is the whole of what a human needs to resolve one.
    """

    text: str
    region: str
    reason: str
    competing: tuple[str, ...] = ()


# ── Destinations ──────────────────────────────────────────────────────────


DESTINATIONS: tuple[str, ...] = (
    "memory",
    "profile",
    "project",
    "people",
    "learnings",
    "review",
)
"""Destination vocabulary shared with the extraction prompts. A bullet tagged
outside this set is treated as untagged and falls back to section defaults."""

# Matches a trailing destination tag: ``[memory]``, ``[project]``,
# ``[people: Mo Salah]``. The colon-payload form is what the extraction
# prompts ask for; the queue-file form uses a space (``[people Mo Salah]``),
# which :mod:`ciao.proposal_kinds` owns.
_DESTINATION_RE = re.compile(
    rf"\s*\[({'|'.join(DESTINATIONS)})(?::[ \t]*([^\]]+))?\]\s*$",
    re.IGNORECASE,
)

_IDX_TAG_RE = re.compile(r"\s*\[idx\s*=\s*([\d,\s]+)\]\s*$")


def _one_line(value: str) -> str:
    """Collapse every whitespace run (newlines included) into a single space.

    The proposals queue is line-oriented Markdown: one bullet is one line. A
    field carrying an embedded newline would otherwise split into a truncated
    bullet plus a continuation the parser reads as its own spurious proposal,
    and the original value would never appear as one parsed bullet, so
    re-filing it would dodge the text dedupe.
    """
    return " ".join(value.split())


def _peel_trailing_metadata(text: str) -> tuple[str, str, tuple[int, ...], str]:
    """Split trailing citation and destination metadata off a bullet.

    Returns ``(kind, payload, citations, remaining_text)``. Models write the
    tag after the citation per the prompt, but either order is accepted:
    trailing bracketed groups are peeled from the end, and each must be an
    ``[idx=…]`` citation or a destination tag — anything else stops the peel
    and stays in the text rather than being guessed at. When several tags
    somehow stack up, the one closest to the end of the line wins.

    ``citations`` are the transcript message indices the bullet cites. They
    used to be thrown away here, which left region promotion with no way to
    ask whether a fact was tied to any real turn: a confidently formatted
    bullet carrying a fabricated ``[idx=99]`` — or no citation at all — read
    exactly like a grounded one and was auto-saved into always-loaded context.
    :func:`unsupported_region_facts` is the consumer.
    """
    kind, payload = "", ""
    cited: list[int] = []
    while True:
        match = _DESTINATION_RE.search(text)
        if match is not None:
            if not kind:
                kind = match.group(1).lower()
                payload = (match.group(2) or "").strip()
            text = text[: match.start()].rstrip()
            continue
        match = _IDX_TAG_RE.search(text)
        if match is not None:
            cited.extend(int(part) for part in re.findall(r"\d+", match.group(1)))
            text = text[: match.start()].rstrip()
            continue
        return kind, payload, tuple(sorted(set(cited))), text


@dataclass(slots=True, frozen=True)
class MemoryProposal:
    """One proposed memory entry with its routing target."""

    target: str  # a DESTINATIONS member (legacy "user" normalized on write)
    text: str
    source_section: str
    payload: str = ""  # e.g. the person name for [people], doc path for [project]
    # Transcript message indices the bullet cited, as peeled from its
    # ``[idx=N]`` tag. Empty means the model cited nothing — which for a
    # region-bound fact is itself a reason to queue rather than auto-save.
    citations: tuple[int, ...] = ()

    def as_bullet(self) -> str:
        # Deliberately total: an unknown target is written through rather than
        # raising, so one odd proposal cannot fail a whole archive batch.
        target = "profile" if self.target == "user" else self.target
        # Every field is forced onto one line and kept clear of the delimiter
        # that closes its own slot: a `]` inside the payload would end the
        # destination head early, and a `)` inside the source would break the
        # `_(from: ...)_` tail so the whole bullet stops parsing — invisible
        # to the review UI and to dedupe alike.
        payload = _one_line(self.payload).replace("]", "")
        source = _one_line(self.source_section).replace(")", "")
        head = f"[{target} {payload}]" if payload else f"[{target}]"
        return f"- {head} {_one_line(self.text)}  _(from: {source})_"


# ── Parsing ───────────────────────────────────────────────────────────────


def _split_sections(insights_md: str) -> dict[str, list[str]]:
    """Group bullet lines by their ``## Heading``.

    Strips bullet markers only. Citation tags — ``[idx=12]`` and the
    multi-index ``[idx=12,34]`` shape models improvise — and destination tags
    both survive here; they are split later, per bullet, by
    :func:`_peel_trailing_metadata`, where the routing decision happens.
    Stripping the citation at this stage destroyed the one piece of evidence
    that ties a fact to a real turn before anything could check it. Empty
    sections are dropped.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in insights_md.splitlines():
        line = raw_line.rstrip()
        heading_match = re.match(r"^##+\s+(.+?)\s*$", line)
        if heading_match:
            current = heading_match.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current is None:
            continue
        bullet = re.match(r"^\s*[-*]\s+(.+)$", line)
        if not bullet:
            continue
        text = bullet.group(1).strip()
        if text:
            sections[current].append(text)
    return sections


def _is_durable(text: str) -> bool:
    """Reject obvious per-session noise before proposing.

    This is the second line of defence behind the extraction prompt: bullets
    that reach here are already terse, but the model still drifts toward the
    "User said: X -> assistant did Y" event shape. A correction that only
    records what happened in one chat is exactly the shape ``memory_audit``
    flags as rot if it ever reaches a region, so it is stopped at the queue —
    unless it carries a ``Durable rule:`` clause, which is the durable part
    and survives regardless (a placeholder rule still stays pending for the
    curator to judge rather than being auto-promoted).
    """
    from ciao.memory_audit import find_event_shaped

    lowered = text.lower()
    if any(lowered.startswith(p) for p in ("tried ", "asked ", "ran ")):
        return False
    if len(text) < 12 or len(text) > 400:
        return False
    if _durable_rule_of(text) is not None:
        # Has a Durable rule clause (real or placeholder): keep it pending so
        # the curator decides. A real clause is promoted; a placeholder stays
        # queued rather than being silently proposed as durable.
        return True
    # No durable-rule clause: a bullet shaped like a transcript event is
    # session noise, not state. `find_event_shaped` is the same detector
    # memory_audit uses, so a bullet that would be flagged as rot on promotion
    # is never proposed at all.
    return not find_event_shaped("memory", [text])


def _durable_rule_of(text: str) -> str | None:
    """The standing rule a bullet asserts, or None when it carries no clause.

    Returns ``None`` when there is no "Durable rule:" clause; otherwise the
    clause text, which may be empty or a placeholder ("None"/"n/a"/an echoed
    template). Callers decide what an empty/placeholder clause means.
    """
    matches = list(_DURABLE_RULE_RE.finditer(text))
    if not matches:
        return None
    return matches[-1].group(1).strip().rstrip(".").strip()


# ── Proposal generation ───────────────────────────────────────────────────


# Section headers used by the extraction prompts in ``ciao/insights.py``.
_BEHAVIORAL_SECTIONS = ("User corrections", "Decisions")
_IDENTITY_SECTIONS = ("New entities",)


def _default_destination(section: str, text: str) -> tuple[str, str]:
    """Where an untagged bullet goes, given its insight section.

    Conservative by design: corrections and operator identity were always
    region-bound, so they keep those targets; anything else the model did not
    classify goes to review rather than pretending a region wants it.
    """
    if section == "User corrections":
        return "memory", ""
    if section == "New entities":
        match = re.match(r"^person\s*:\s*(operator|user)\b", text, re.I)
        if match:
            return "profile", ""
        person = re.match(r"^person\s*:\s*([^-–—]+?)\s*-", text, re.I)
        if person:
            return "people", person.group(1).strip()
    return "review", ""


# A "Decisions" bullet that opens on a past-tense action is a changelog line
# ("Added regression test ...", "Deleted the ESL schedule ...", "Committed only
# the three modified files"): it records what this session did, which the
# archive already holds. Measured on a real queue history: 60 such bullets,
# none ever accepted. A bullet that also states what holds from now on keeps
# its place — "Retired X; Y remains the active skill going forward" is state.
_CHANGELOG_VERBS = (
    "fixed|added|deleted|removed|committed|created|updated|renamed|moved|"
    "marked|stopped|merged|pushed|shipped|ran|wrote|edited|filed|sent|"
    "drafted|replied|implemented|refactored|reverted|restored|replaced|"
    "bumped|installed|configured|enabled|disabled|cleaned|tested|verified|"
    "confirmed|opened|closed|published|released|rewrote|migrated|documented|"
    "archived|retired|dropped|landed"
)
_CHANGELOG_RE = re.compile(rf"^(?:the\s+\w+\s+)?(?:{_CHANGELOG_VERBS})\b", re.IGNORECASE)
_STANDING_MARKER_RE = re.compile(
    r"going forward|from now on|in future|future sessions|this governs|"
    r"standard way|\bdefault\b|\balways\b|\bnever\b|\bshould\b|\bmust\b|"
    r"\brule\b|\bprefer",
    re.IGNORECASE,
)


def _is_changelog_decision(text: str) -> bool:
    """A Decisions bullet that only reports an action this session took."""
    return bool(_CHANGELOG_RE.match(text)) and not _STANDING_MARKER_RE.search(text)


def propose_from_insights(insights_md: str) -> list[MemoryProposal]:
    """Scan an insights markdown blob and emit destination-addressed proposals."""
    if not insights_md.strip():
        return []

    sections = _split_sections(insights_md)
    proposals: list[MemoryProposal] = []

    for heading in (
        *_BEHAVIORAL_SECTIONS, *_IDENTITY_SECTIONS, "Open loops"
    ):
        for item in sections.get(heading, []):
            kind, payload, citations, text = _peel_trailing_metadata(item)
            if heading == "Open loops" and not (kind == "project" and payload):
                # Open loops are the chat's own business and the doc fold's
                # to track; only one the model filed under a named other
                # project needs routing, because the fold skips those.
                continue
            if not kind:
                kind, payload = _default_destination(heading, text)
            if not _is_durable(text):
                continue
            if heading == "Decisions" and (
                kind == "review" or _is_changelog_decision(text)
            ):
                # A decision the model could not place is, in practice, a
                # one-off choice about this session: 564 of them on a real
                # queue, none accepted. A precedent-setting decision names its
                # home ([memory], [learnings], [project]) and still flows.
                continue
            proposals.append(MemoryProposal(
                target=kind,
                text=text,
                source_section=heading,
                payload=payload,
                citations=citations,
            ))

    return proposals


# ── Auto-apply ────────────────────────────────────────────────────────────


# The extraction prompt asks for the standing preference a correction implies
# as a trailing "Durable rule: <...>" sentence. That clause — not the
# "User said X -> assistant did Y" event around it — is what belongs in a
# region: the regions are a state surface, and memory_audit flags the event
# shape as rot for the nightly curator to remove.
#
# Both extraction prompts in ciao.insights embed this label verbatim (a test
# asserts the link), and the regex is built from it so the producer prompts
# and this consumer cannot drift apart silently. Case-sensitive and anchored
# to a sentence start so a chat fragment quoted inside the bullet ("... as a
# durable rule: ...") never matches.
DURABLE_RULE_LABEL = "Durable rule:"
_DURABLE_RULE_RE = re.compile(
    rf"(?:^|[.!?]\s+){re.escape(DURABLE_RULE_LABEL)}\s*(.+)$"
)

# Contentless fillers models emit instead of omitting the clause.
_NO_OP_RULES = frozenset({"none", "n/a", "na", "no", "-", "unknown"})


def _is_placeholder_rule(rule: str) -> bool:
    """An echoed template placeholder or a contentless no-op, not a rule."""
    if "<" in rule and ">" in rule:
        return True
    return rule.lower() in _NO_OP_RULES


def _promotable_text(text: str) -> str | None:
    """The state-shaped text safe to write to a bounded region, or None.

    None means "keep it in the proposals queue": the curator rephrases
    event-shaped corrections into standing rules on the next pass. Writing
    them verbatim used to overflow the always-injected region with entries
    the shipped audit itself classifies as event-shaped rot.
    """
    from ciao.memory_audit import find_event_shaped

    matches = list(_DURABLE_RULE_RE.finditer(text))
    if matches:
        rule = matches[-1].group(1).strip().rstrip(".").strip()
        if (
            rule
            and not _is_placeholder_rule(rule)
            and not find_event_shaped("memory", [rule])
        ):
            return rule + "."
        return None
    if find_event_shaped("memory", [text]):
        return None
    return text


def _provenance_row(proposal: MemoryProposal) -> dict[str, Any]:
    """The evidence chain stamped onto a region write's receipt.

    Records where the fact came from and under which rules it was admitted:
    the transcript turns it cited, the insights section it was extracted
    from, its temporal bounds, and the extraction/policy versions in force.
    A bullet that cited nothing is recorded as ``provenance: "unknown"``
    rather than being given a plausible id — the receipt has to be able to say
    "this archive never said where this came from", which is a different fact
    from "it came from turn 3".
    """
    from ciao.fact_candidates import candidate_from_proposal

    candidate = candidate_from_proposal(proposal)
    return {
        "schema": candidate.schema,
        "source_message_ids": list(candidate.source_message_ids),
        "provenance": candidate.provenance,
        "section": candidate.section,
        "as_of": candidate.as_of,
        "expires": candidate.expires,
        "extraction_version": candidate.extraction_version,
        "policy_version": candidate.policy_version,
    }


def _log_consolidation(
    vault_root: Path, region: str, old_entry: str, *, label: str = "auto-reconcile"
) -> None:
    """Copy a replaced region entry into the undo log before it disappears.

    ``Workspace/Memory-Consolidations.md`` is the standing rule for unattended
    region edits: nothing is dropped silently, and the user can restore any
    line. The file is in ``RESERVED_UNINDEXED_FILES``, so it never pollutes
    recall. Appended rather than rewritten: the log grows without bound, and a
    full read-modify-write would slow every consolidation as history piles up.
    """
    path = vault_root / "Workspace" / "Memory-Consolidations.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            "---\ntags: [ciao, memory, undo-log]\n---\n"
            "# Memory Consolidations\n\n"
            "Undo log for bounded-memory edits: every removed or replaced "
            "entry is copied here first.\n",
            encoding="utf-8",
        )
    heading = f"\n## {date.today().isoformat()} — ciao:{region} ({label})\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(heading + f"- {_one_line(old_entry)}\n")


def _promote_to_region(
    proposal: MemoryProposal,
    guide_path: Path,
    *,
    vault_root: Path | None = None,
    decision: ReconcileDecision | None = None,
    actor: str = "agent",
    source: str = "archive",
    workspace: str = "",
    deferral_out: list[ReconcileDecision] | None = None,
    receipt_out: dict[str, Any] | None = None,
) -> tuple[PromotionOutcome, str | None]:
    """Write one region-bound proposal.

    Returns ``(outcome, promotable_or_None)``; see :data:`PromotionOutcome`
    for what each status means and which of them leave the fact queued.

    ``deferral_out``, when given, receives the defer row behind a ``deferred``
    outcome — the one the caller handed in, or the one built here for a stale
    or unusable update. Deferring is only half the fix; a caller that cannot
    say why a fact was queued, or against which entries, has moved the problem
    rather than solved it.

    Fail-safe by construction: the guide lock is *required*, not best-effort.
    An earlier version caught a lock failure and proceeded with ``lock=None``,
    which reintroduced exactly the lost-update race the lock exists to prevent
    — two concurrent accepts both read, both append to their own snapshot, and
    the second write silently drops the first fact while its row is dismissed
    as promoted. A lock that cannot be taken now returns ``"failed"`` with zero
    guide writes and the fact left pending.

    Every write goes through :func:`ciao.memory_receipts.commit_region_change`,
    which records a stable-id receipt (actor/source, expected revision,
    before/after images, prepared/applied status) and re-checks the revision
    under the lock before replacing the file, so an external direct edit is
    reported as a conflict rather than overwritten.

    ``decision`` is this fact's row from :func:`plan_region_reconcile`, when
    the caller ran one: ``{"action": "covered"}`` drops the fact as already
    remembered, ``{"action": "update", "index": N, "text": ...}`` replaces
    entry ``N`` (1-based) with the merged text — the replaced entry goes to
    the consolidations undo log first — ``{"action": "defer", "reason": ...}``
    routes the fact to the queue, and ``{"action": "add"}`` or ``None`` is the
    plain append path.

    ``receipt_out`` is an optional caller-owned dict this fills with the
    receipt ``commit_region_change`` recorded, when a write actually happened.
    It is an out-parameter rather than a third return value on purpose: the
    return tuple is unpacked by the archive-time apply loop and by a dozen
    tests, and the only caller that needs the receipt is the PWA accept. The
    decision ledger records the ORIGINAL bullet — that is what append-time
    dedupe compares a re-extracted fact against — so an edited accept's ledger
    row cannot be matched back to its receipt by text. This is how the row gets
    the reference instead of guessing at it.
    """
    from ciao.memory_receipts import (
        RevisionConflict,
        commit_region_change,
        content_revision,
    )
    from ciao.memory_tool import (
        MemoryLockError,
        ensure_regions,
        guide_lock,
        read_region,
        release_guide_lock,
        resolve_region,
        serialize_entries,
    )

    promotable = _promotable_text(proposal.text)
    if promotable is None:
        return "unshaped", None
    from ciao.memory_audit import strip_learned_stamp

    # The whole read-merge-write, under the same lock `update_region` takes.
    # A lock we cannot take is a hard, retryable failure: never fall through to
    # an unlocked write.
    try:
        lock = guide_lock(guide_path)
    except MemoryLockError as exc:
        logger.info(
            "memory apply: guide lock unavailable, fact stays queued (%s)", exc
        )
        return "failed", promotable
    try:
        ensure_regions(guide_path)
        region = resolve_region(proposal.target)
        entries, diags = read_region(guide_path, region)
        if diags:
            logger.info(
                "memory apply: falling back to proposals (%s)",
                "; ".join(d.message for d in diags),
            )
            return "failed", promotable
        # Pin the exact body this merge was computed against. `commit_region_change`
        # re-reads under its lock and refuses on any drift, so a competing
        # managed write that landed between this read and the commit is a
        # conflict — the fact stays queued — instead of being overwritten.
        expected_revision = content_revision(serialize_entries(entries))
        # Compared with stamps stripped: the same fact promoted on two
        # different days is still the same fact.
        if promotable in {strip_learned_stamp(entry) for entry in entries}:
            logger.info(
                "memory apply: dropped exact duplicate %r",
                promotable[:80],
            )
            return "duplicate", promotable

        row: ReconcileDecision = decision or {"action": "add"}
        action: ReconcileAction = row.get("action") or "add"
        if action == "defer":
            # Reconcile ran against a non-empty region and came back with
            # nothing usable for this fact, so nobody knows whether it
            # supersedes an entry already there. Appending anyway is what put
            # "Insights model is deepseek-flash" and "Insights model is sonnet"
            # in the same always-loaded region; the queue holds the text
            # instead, so the fact is preserved without asserting itself.
            logger.info(
                "memory apply: deferring %r to the queue (%s)",
                promotable[:80],
                row.get("reason") or "uncertain reconcile",
            )
            if deferral_out is not None:
                deferral_out.append(row)
            return "deferred", promotable
        if action == "covered":
            logger.info(
                "memory apply: reconcile says already covered: %r",
                promotable[:80],
            )
            # A model verdict, not a provable string match: leave a trace in
            # the undo log so a hallucinated "covered" never silently loses
            # the fact — the standing "nothing is dropped silently" contract.
            # With nowhere to write that trace, honour the contract instead of
            # the verdict and take the plain append: a duplicate is visible and
            # removable, a silently dropped fact is neither.
            if vault_root is None:
                logger.info(
                    "memory apply: reconcile said covered but there is no vault "
                    "to log it to; appending instead of dropping %r",
                    promotable[:80],
                )
            else:
                _log_consolidation(
                    vault_root,
                    region,
                    f"(incoming fact judged covered; not written) {promotable}",
                    label="auto-reconcile covered",
                )
                return "duplicate", promotable
        if action == "update":
            if vault_root is None:
                # The replaced entry is copied to the consolidations undo log
                # before it disappears, and there is nowhere to write that. The
                # update cannot run, and the fact the model read as superseding
                # an existing entry must not be appended next to it, so it
                # waits in the queue.
                logger.info(
                    "memory apply: deferring %r to the queue (no vault for the "
                    "consolidations undo log)",
                    promotable[:80],
                )
                if deferral_out is not None:
                    deferral_out.append(
                        _defer(
                            "no vault for the consolidations undo log",
                            entries,
                        )
                    )
                return "deferred", promotable
            index = row.get("index")
            merged = str(row.get("text", "")).strip()
            plan_old = str(row.get("old", ""))
            from ciao.memory_audit import find_event_shaped

            if (
                isinstance(index, int)
                and 1 <= index <= len(entries)
                and merged
                # The region only ever holds state-shaped text; a model-merged
                # replacement must clear the same bar the plain append does.
                and not find_event_shaped(region, [merged])
                # The index was computed against a snapshot taken before an
                # up-to-two-minute model call; if the entry it names has
                # changed since (concurrent /remember, another archive's
                # apply), replacing it would overwrite an unrelated fact.
                and (
                    not plan_old
                    or strip_learned_stamp(entries[index - 1])
                    == strip_learned_stamp(plan_old)
                )
            ):
                old = entries[index - 1]
                updated = list(entries)
                # The model is told to keep every still-true part of the old
                # entry, and the numbered list it reads carries that entry's
                # learned-at stamp, so a compliant merge often echoes it back.
                # Stamping on top of it produced `... [2026-01-01] [2026-09-02]`,
                # and `_LEARNED_STAMP_RE` is `$`-anchored, so stripping such an
                # entry never yields the fact's own text again — the duplicate
                # guard above stops recognising it and the fact gets appended a
                # second time on the next archive pass.
                updated[index - 1] = (
                    f"{strip_learned_stamp(merged)} [{date.today().isoformat()}]"
                )
                _log_consolidation(vault_root, region, old)
                receipt = commit_region_change(
                    guide_path,
                    region,
                    entries=updated,
                    actor=actor,
                    source=source,
                    workspace=workspace,
                    vault_root=vault_root,
                    lock=lock,
                    expected_revision=expected_revision,
                    fact_text=promotable,
                    destination=f"ciao:{region}",
                    removed_texts=[old],
                    kind="region_update",
                    provenance=_provenance_row(proposal),
                )
                if receipt_out is not None:
                    receipt_out.update(receipt)
                logger.info(
                    "memory apply: reconciled update of entry %d in ciao:%s",
                    index,
                    region,
                )
                return "written", promotable
            # The update named an entry we cannot safely replace — out of
            # range, empty or event-shaped merge text, or an entry that changed
            # during the up-to-two-minute model call. Appending instead used to
            # leave the superseded entry and its replacement both live in the
            # region every session loads; neither is dropped, the fact goes to
            # the queue for a human to resolve against the current region.
            stale = bool(
                isinstance(index, int)
                and 1 <= index <= len(entries)
                and plan_old
                and strip_learned_stamp(entries[index - 1])
                != strip_learned_stamp(plan_old)
            )
            reason = (
                f"the entry this update named (ciao:{region} #{index}) changed "
                "while the reconcile was running"
                if stale
                else f"unusable update of entry {index!r} in ciao:{region}"
            )
            logger.info(
                "memory apply: deferring %r to the queue (%s)",
                promotable[:80],
                reason,
            )
            if deferral_out is not None:
                competing = (
                    [entries[index - 1]]
                    if isinstance(index, int) and 1 <= index <= len(entries)
                    else entries
                )
                deferral_out.append(_defer(reason, competing))
            return "deferred", promotable
        # The learned-at stamp is system time — when this fact entered the
        # region — read by the aging audit so unverified old facts surface
        # for re-verification instead of asserting themselves forever.
        stamped = f"{promotable} [{date.today().isoformat()}]"
        receipt = commit_region_change(
            guide_path,
            region,
            entries=entries + [stamped],
            actor=actor,
            source=source,
            workspace=workspace,
            vault_root=vault_root,
            lock=lock,
            expected_revision=expected_revision,
            fact_text=promotable,
            destination=f"ciao:{region}",
            kind="region_apply",
            provenance=_provenance_row(proposal),
        )
        if receipt_out is not None:
            receipt_out.update(receipt)
        return "written", promotable
    except RevisionConflict as exc:
        logger.info("memory apply: destination changed, fact stays queued (%s)", exc)
        return "conflict", promotable
    except MemoryLockError as exc:
        logger.info("memory apply: lost the guide lock, fact stays queued (%s)", exc)
        return "failed", promotable
    except Exception as exc:  # noqa: BLE001 — one bad write must not stop a batch
        logger.info("memory apply: falling back to proposals (%s)", exc)
        return "failed", promotable
    finally:
        release_guide_lock(lock)



def accept_region_fact(
    *,
    guide_path: Path,
    target: str,
    text: str,
    vault_root: Path | None,
    decision: ReconcileDecision | None = None,
    actor: str = "operator",
    source: str = "pwa",
    workspace: str = "",
    deferral_out: list[ReconcileDecision] | None = None,
    receipt_out: dict[str, Any] | None = None,
) -> tuple[PromotionOutcome, str | None]:
    """Write one approved region fact through the guarded path.

    The UI accept button used to call ``update_region(action="add")`` directly,
    which skipped everything the archive-time path does: the event-shape guard
    (so an event-shaped bullet landed verbatim in always-loaded context), the
    stamp-stripped duplicate check, the learned-at stamp the aging audit reads,
    and the consolidations undo log.

    Deliberately synchronous and model-free: one ``run_oneshot`` per row on a
    click is a 120s timeout each, and the batch endpoint accepts rows
    sequentially inside one request. Reconciliation is offered alongside it
    rather than inside it — :func:`reconcile_region_fact` runs one fresh call
    against the *current* region and hands the result in as ``decision``, which
    is how a fact deferred at archive time gets resolved on a retry. A caller
    that passes none takes the plain append path.

    Returns ``_promote_to_region``'s ``(outcome, promotable)``. Both
    out-parameters are forwarded unchanged. ``deferral_out`` lets an
    interactive accept report *why* its fact stayed queued and against which
    entries — a review UI that can only say "refused" gives the person no way
    to judge whether another retry is worth a second model call. Pass
    ``receipt_out`` to also learn which receipt performed the write: the
    caller records the decision under the bullet's ORIGINAL text, so an
    accept of an edited wording has no way to find its own receipt again.
    """
    proposal = MemoryProposal(target=target, text=text, source_section="review")
    return _promote_to_region(
        proposal,
        guide_path,
        vault_root=vault_root,
        decision=decision,
        actor=actor,
        source=source,
        workspace=workspace,
        deferral_out=deferral_out,
        receipt_out=receipt_out,
    )


def _safe_name(name: str) -> str:
    """A person payload as a filename stem, without path separators."""
    cleaned = re.sub(r"[\\/:*?\"<>|]+", " ", name).strip().rstrip(".")
    return cleaned[:80]


def people_note_path(vault_root: Path, name: str) -> Path | None:
    """Where a ``[people]`` accept would write, or None for an unusable name.

    Public so the review queue can name the destination — and say whether the
    note already exists — before the accept runs, without a second copy of the
    filename rules :func:`write_people_note` applies.
    """
    stem = _safe_name(name)
    if not stem:
        return None
    return vault_root / _PEOPLE_DIR / f"{stem}.md"


def write_people_note(vault_root: Path, name: str, text: str) -> bool:
    """Create a stub person note. False when it already exists (needs a merge).

    Public because accepting a ``[people]`` proposal from the review queue
    performs exactly this write.
    """
    stem = _safe_name(name)
    path = people_note_path(vault_root, name)
    if path is None:
        return False
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    # `updated:` records the note's creation as its first verification, so a
    # person the system stopped hearing about ages out visibly instead of
    # relying on mtime (which file copies and migrations reset silently).
    note = (
        "---\n"
        "tags: [person]\n"
        f"updated: {date.today().isoformat()}\n"
        f"---\n# {stem}\n\n{text}\n"
    )
    path.write_text(note, encoding="utf-8")
    return True


# One structured learning line. The shape is a contract: the curation skill
# reads the recurrence count to decide promotion (N ≥ 3) and the sources to
# cite episodes, so recurrence bookkeeping is mechanical instead of prose.
_LEARNING_LINE_RE = re.compile(
    r"^- \[(?P<key>[a-z0-9][a-z0-9-]*)\] "
    r"\[(?P<first>\d{4}-\d{2}-\d{2}) → (?P<last>\d{4}-\d{2}-\d{2})\] "
    r"\(x(?P<count>\d+)\) "
    r"(?P<text>.*?)"
    r"(?: — sources: (?P<sources>.*))?$"
)

_LEARNING_KEY_WORDS = 4
_LEARNING_MAX_SOURCES = 8


def _learning_key(text: str) -> str:
    """A short kebab identifier from the statement's first distinctive words."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:_LEARNING_KEY_WORDS]) or "learning"


def _normalized_learning(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def format_learning_line(
    text: str,
    *,
    first_seen: str,
    last_seen: str,
    count: int,
    sources: list[str],
) -> str:
    line = (
        f"- [{_learning_key(text)}] [{first_seen} → {last_seen}] "
        f"(x{count}) {_one_line(text)}"
    )
    cited = [s for s in sources if s][:_LEARNING_MAX_SOURCES]
    if cited:
        line += f" — sources: {', '.join(cited)}"
    return line


_LEARNINGS_STUB = (
    "---\n"
    "tags: [ciao, learnings]\n"
    "---\n"
    "# Learnings\n\n"
    "Reusable cross-project knowledge. Active entries are candidates "
    "for promotion into canonical guidance once they recur (x3 or "
    "more).\n"
)


def learnings_path(vault_root: Path) -> Path:
    """Where a ``[learnings]`` accept writes."""
    return vault_root / _LEARNINGS_RELATIVE


def render_learning_append(
    existing: str, text: str, *, source: str = "", today: str = ""
) -> tuple[str, str]:
    """The file ``append_learning`` would write, and which operation that is.

    Returns ``(updated_text, operation)`` — ``"add"`` for a new Active entry,
    ``"update"`` when an existing entry's recurrence count and last-seen date
    are refreshed, and ``"none"`` when an exact legacy duplicate is already
    there and nothing is written (``updated_text`` is then ``existing``).

    Split out of :func:`append_learning` so the review queue can show the exact
    replacement *before* the accept performs it. The write path goes through
    this same function, so a preview and the accept it precedes cannot
    disagree about what lands.
    """
    if f"- {_one_line(text)}" in existing:
        # Exact legacy duplicate: already recorded in the old plain shape.
        return existing, "none"

    stamp = today or date.today().isoformat()
    normalized = _normalized_learning(text)
    lines = existing.split("\n")
    for index, line in enumerate(lines):
        match = _LEARNING_LINE_RE.match(line)
        if match is None:
            continue
        if _normalized_learning(match.group("text")) != normalized:
            continue
        sources = [
            item.strip()
            for item in (match.group("sources") or "").split(",")
            if item.strip()
        ]
        if source and source not in sources:
            sources.append(source)
        lines[index] = format_learning_line(
            match.group("text"),
            first_seen=match.group("first"),
            last_seen=stamp,
            count=int(match.group("count")) + 1,
            sources=sources,
        )
        return "\n".join(lines), "update"

    entry = format_learning_line(
        text,
        first_seen=stamp,
        last_seen=stamp,
        count=1,
        sources=[source] if source else [],
    )
    marker = "\n## Active\n"
    if marker in existing:
        head, _, tail = existing.partition(marker)
        return f"{head}{marker}{entry}\n{tail}", "add"
    return existing.rstrip() + f"\n\n## Active\n\n{entry}\n", "add"


def read_learnings(vault_root: Path) -> str:
    """The current Learnings file, or the stub a first write would start from.

    Existence, not a swallowed read error, decides: a file that is there but
    unreadable must surface rather than be silently replaced by the stub,
    which a following write would then persist over the real content.
    """
    path = learnings_path(vault_root)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return _LEARNINGS_STUB


def append_learning(vault_root: Path, text: str, *, source: str = "") -> bool:
    """File one learning under the Active section of Workspace/Learnings.md.

    Structured entries carry a key, first-seen/last-seen dates, a recurrence
    count, and source chat ids. Re-observing a learning (same normalized
    statement) increments its count and refreshes last-seen instead of
    appending a duplicate — recurrence is what the curation skill promotes on,
    so it must be counted mechanically, not judged from prose. Legacy plain
    bullets are left untouched; an exact legacy duplicate still short-circuits.

    Public because accepting a ``[learnings]`` proposal from the review queue
    performs exactly this write.
    """
    path = learnings_path(vault_root)
    existing = read_learnings(vault_root)
    updated, operation = render_learning_append(existing, text, source=source)
    if operation == "none":
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    return True


def apply_proposals(
    proposals: list[MemoryProposal],
    *,
    guide_path: Path | None = None,
    vault_root: Path | None = None,
    region_decisions: RegionDecisions | None = None,
    learning_source: str = "",
    actor: str = "auto",
    source: str = "archive",
    workspace: str = "",
    stats: dict[str, int] | None = None,
    deferrals: list[DeferredFact] | None = None,
) -> tuple[list[MemoryProposal], list[str]]:
    """Write every confidently-addressed proposal to its destination.

    Returns ``(remaining, applied_texts)``. Regions take state-shaped text
    (see :func:`_promotable_text`); people notes are created only when absent;
    learnings append. ``[project]`` rows are owned by the archive-time doc
    fold and ``[review]`` rows by a human, so both stay in ``remaining``.
    Exact duplicates vanish from both lists: the fact is already remembered,
    which is neither an apply nor something to re-decide. On any write failure
    the proposal stays reviewable.

    ``region_decisions`` maps :func:`_decision_key` (region + promotable fact
    text) to its reconcile decision (see :func:`plan_region_reconcile`);
    absent or None, every region write is the plain append path.

    ``actor``/``source``/``workspace`` are stamped into the region write's
    receipt so the review History can say who changed memory and from where.

    ``stats``, when given, is filled with ``deferred``: region facts sent to
    the queue instead of the region because something about them could not be
    trusted — an uncertain reconcile, or a citation the transcript does not
    support (:func:`unsupported_region_facts`). They are in ``remaining`` like
    any other queued row, which alone cannot say why.

    ``deferrals``, when given, receives one :class:`DeferredFact` per such
    fact: its reason and the region entries it competes with. The count says a
    reconcile backend is down; only these say which facts are waiting and what
    they are waiting on.
    """
    from ciao.memory_tool import resolve_region

    remaining: list[MemoryProposal] = []
    applied: list[str] = []

    def _record_auto(*, text: str, kind: str, destination: str, outcome: str) -> None:
        # Best-effort: a decision-history write must never fail the apply it
        # is only recording. Silent skip when there is no vault to log to
        # (region-only callers, e.g. accept_region_fact, pass no vault_root).
        if vault_root is None:
            return
        try:
            record_promotion(
                vault_root / _PROPOSALS_RELATIVE,
                text=text,
                kind=kind,
                via="auto",
                source=learning_source,
                destination=destination,
                outcome=outcome,
            )
        except Exception:  # noqa: BLE001
            logger.info("memory apply: could not record auto decision for %r", text[:80])

    for proposal in proposals:
        try:
            if proposal.target in ("memory", "profile"):
                if guide_path is None:
                    remaining.append(proposal)
                    continue
                promotable_key = _promotable_text(proposal.text)
                decision = None
                if region_decisions and promotable_key:
                    decision = region_decisions.get(
                        _decision_key(resolve_region(proposal.target), promotable_key)
                    )
                deferral_out: list[ReconcileDecision] = []
                outcome, promotable = _promote_to_region(
                    proposal,
                    guide_path,
                    vault_root=vault_root,
                    decision=decision,
                    actor=actor,
                    source=source,
                    workspace=workspace,
                    deferral_out=deferral_out,
                )
                if outcome == "written":
                    applied.append(promotable or proposal.text)
                    _record_auto(
                        text=promotable or proposal.text,
                        kind=proposal.target,
                        destination=f"ciao:{resolve_region(proposal.target)}",
                        outcome="written",
                    )
                elif outcome == "duplicate":
                    _record_auto(
                        text=promotable or proposal.text,
                        kind=proposal.target,
                        destination=f"ciao:{resolve_region(proposal.target)}",
                        outcome="duplicate",
                    )
                else:
                    # Failed writes, revision conflicts, event-shaped text and
                    # deferred facts all stay queued: the first two for a retry
                    # or a re-plan, the third for a curator to rephrase into a
                    # standing rule, the fourth for a human to resolve against
                    # the region it may supersede. None of them is a decision
                    # yet, so none is recorded.
                    if outcome == "deferred":
                        if stats is not None:
                            stats["deferred"] = stats.get("deferred", 0) + 1
                        if deferrals is not None:
                            deferred_row = (
                                deferral_out[0] if deferral_out else _defer("")
                            )
                            deferrals.append(
                                DeferredFact(
                                    text=promotable or proposal.text,
                                    region=resolve_region(proposal.target),
                                    reason=deferred_row.get("reason")
                                    or "uncertain reconcile",
                                    competing=tuple(
                                        deferred_row.get("competing") or ()
                                    ),
                                )
                            )
                    remaining.append(proposal)
            elif proposal.target == "people" and vault_root is not None:
                name = proposal.payload or _safe_name(proposal.text.split("-")[0])
                if write_people_note(vault_root, name, proposal.text):
                    applied.append(proposal.text)
                    _record_auto(
                        text=proposal.text,
                        kind=proposal.target,
                        destination=f"{_PEOPLE_DIR}/{_safe_name(name)}.md",
                        outcome="written",
                    )
                else:
                    remaining.append(proposal)
            elif proposal.target == "learnings" and vault_root is not None:
                if append_learning(vault_root, proposal.text, source=learning_source):
                    applied.append(proposal.text)
                    _record_auto(
                        text=proposal.text,
                        kind=proposal.target,
                        destination=_LEARNINGS_RELATIVE,
                        outcome="written",
                    )
                else:
                    remaining.append(proposal)
            else:
                # project (the fold owns it) and review (a human owns it).
                remaining.append(proposal)
        except Exception as exc:  # noqa: BLE001 — never lose the batch to one row
            logger.info("memory apply: %s stays queued (%s)", proposal.target, exc)
            remaining.append(proposal)
    return remaining, applied


# ── Write-time reconcile (ADD / UPDATE / COVERED) ──────────────────────────


_RECONCILE_SYSTEM_PROMPT = """\
You maintain a small always-loaded memory region for a personal assistant.
You are given its current numbered entries and lettered candidate facts.
For each candidate, decide exactly one action:
- "add": genuinely new information no existing entry carries.
- "covered": an existing entry already states this fact (even in other words).
- "update": it supersedes or extends exactly ONE existing entry. Give that
  entry's number and the single merged replacement text: present tense, keep
  every part of the old entry that is still true, keep any [expires:] or
  [as-of:] tag that still applies, and never merge two unrelated facts.
Be conservative: when unsure between update and add, choose add.
Reply with ONLY a JSON array, one object per candidate in their given order:
[{"action": "add"}, {"action": "update", "index": 2, "text": "..."}, {"action": "covered"}]
No prose, no code fences, no trailing commentary.
"""

# One reconcile call is bounded by the region cap (~3000 chars) plus a few
# candidates, so a short timeout keeps a slow backend from stalling archive
# post-processing. Failure is safe: the caller degrades to plain appends.
_RECONCILE_TIMEOUT_S = 120.0


def _decision_key(region: str, fact: str) -> str:
    """The ``region_decisions`` map key for one candidate fact.

    Region-qualified: the same sentence can be bound to both ``memory`` and
    ``profile`` in one archive, and a bare-text key would let one region's
    ``update`` index be applied against the other region's entries.
    """
    return f"{region}\n{fact}"


def _parse_reconcile_reply(raw: str, count: int) -> list[ReconcileDecision] | None:
    """Parse the model's JSON array; None when the shape is unusable.

    Per-row problems degrade that row to ``{"action": "defer"}`` — the
    candidate is compared against a non-empty region, so a row we cannot read
    says nothing about whether the fact supersedes an entry already there, and
    appending it regardless is how both ended up asserted at once. A reply that
    is not a JSON array of the right length is discarded whole rather than
    guessed at, and the caller defers every candidate in that batch.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, list) or len(data) != count:
        return None
    rows: list[ReconcileDecision] = []
    for item in data:
        if not isinstance(item, dict):
            rows.append(_defer("unreadable reconcile row"))
            continue
        action = str(item.get("action", "")).lower()
        if action == "update":
            index = item.get("index")
            merged = str(item.get("text", "")).strip()
            if isinstance(index, bool) or not isinstance(index, int) or not merged:
                rows.append(_defer("update row missing index or text"))
                continue
            rows.append({"action": "update", "index": index, "text": merged})
        elif action == "covered":
            rows.append({"action": "covered"})
        elif action == "add":
            rows.append({"action": "add"})
        else:
            rows.append(_defer("unreadable reconcile row"))
    return rows


def _reconcile_candidates(
    archive_path: Path,
    guide_path: Path,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """The region-bound facts a reconcile call would compare, and its entries.

    Returns ``(candidates_by_region, entries_by_region)``. A candidate is a
    state-shaped fact that is not already an exact duplicate and whose region
    is non-empty — the two cases a model call cannot improve on. Shared with
    :func:`defer_region_facts` so the fallback defers exactly the facts the
    planner would have reconciled, no more.
    """
    from ciao.memory_audit import strip_learned_stamp
    from ciao.memory_tool import read_region, resolve_region

    by_region: dict[str, list[str]] = {}
    entries_by_region: dict[str, list[str]] = {}
    try:
        text = archive_path.read_text(encoding="utf-8")
    except OSError:
        return by_region, entries_by_region
    body = _extract_insights_section(text)
    if not body:
        return by_region, entries_by_region

    for proposal in propose_from_insights(body):
        if proposal.target not in ("memory", "profile"):
            continue
        promotable = _promotable_text(proposal.text)
        if promotable is None:
            continue
        try:
            region = resolve_region(proposal.target)
            if region not in entries_by_region:
                entries, diags = read_region(guide_path, region)
                if diags:
                    continue
                entries_by_region[region] = entries
        except Exception:  # noqa: BLE001 — reconcile is best-effort
            continue
        stripped = {
            strip_learned_stamp(entry) for entry in entries_by_region[region]
        }
        if promotable in stripped:
            continue
        # An empty region has nothing to reconcile against.
        if not entries_by_region[region]:
            continue
        by_region.setdefault(region, []).append(promotable)
    return by_region, entries_by_region


@dataclass(slots=True, frozen=True)
class TranscriptEvidence:
    """Which transcript turns exist, and which of them the user actually typed.

    Built from the line-oriented JSON :func:`ciao.insights.filter_session_jsonl`
    hands the extraction model — the same records whose ``idx`` the prompt
    tells it to cite — so "does this citation name a real turn" is answered
    against the exact material the model saw, not against prose.
    """

    known: frozenset[int]
    attended_user: frozenset[int]


def transcript_evidence(filtered_jsonl: str) -> TranscriptEvidence | None:
    """Index a filtered transcript by citation id. None when there is none.

    None means "no transcript to check against", not "nothing is supported":
    the text-mode extraction prompt explicitly asks for paraphrase citations
    and forbids ``[idx=N]``, and a legacy archive re-processed without its
    session blob has no indices either. Gating those on indices that were
    never meant to exist would queue every fact in them for no evidence gain.

    The id/role view of the same normalization
    :func:`ciao.fact_candidates.normalize_transcript` builds, kept as its own
    narrow type because most callers only need "does this turn exist, and did
    the user type it" and should not have to carry the turn bodies to ask.
    """
    from ciao.fact_candidates import normalize_transcript

    transcript = normalize_transcript(filtered_jsonl)
    if transcript is None:
        return None
    return TranscriptEvidence(transcript.known, transcript.attended_user)


def _evidence_gap(citations: tuple[int, ...], evidence: TranscriptEvidence) -> str:
    """Why a bullet's citation fails to support it, or "" when it holds.

    The id-only subset of :func:`ciao.fact_candidates.validate_candidate`,
    kept for callers that hold citations without the fact they support:
    nothing cited at all, a citation naming a turn the transcript does not
    contain (a fabricated id, or the ``[idx=0]`` the prompt forbids), and a
    citation that lands only on assistant output or on automation turns.
    """
    if not citations:
        return "bullet cites no source turn"
    unknown = sorted(idx for idx in citations if idx not in evidence.known)
    if unknown:
        return f"citation idx={unknown[0]} names no turn in the transcript"
    if not any(idx in evidence.attended_user for idx in citations):
        return "no cited turn is one the user typed"
    return ""


def unsupported_region_facts(
    archive_path: Path,
    *,
    filtered_jsonl: str,
) -> RegionDecisions:
    """Defer rows for region facts the transcript does not actually support.

    Region promotion checked a fact's *shape* — durable-rule clause, not
    event-shaped — which a fluent model satisfies whether or not any turn said
    the thing. This adds the missing half, through the fact-candidate v1
    evidence policy (:mod:`ciao.fact_candidates`): the bullet must cite a turn
    that exists, that the user typed, that contains the claim in their own
    words rather than in pasted or tool material, that asserts it rather than
    negating it or offering it as an example, and that no later turn corrects.
    Its destination must also be one this workspace actually routes to.

    Facts that fail take the ``"defer"`` route, so an unverifiable fact is
    handled exactly like an uncertain reconcile — queued in
    ``Workspace/Memory-Proposals.md`` for a human, never dropped and never
    written to always-loaded context on the model's word. Each row carries the
    verdict code, the cited ids and the policy version, so the queue can say
    *which* check refused the fact rather than only that something did.

    Keyed like :func:`plan_region_reconcile`'s rows so the caller can overlay
    these on top of a reconcile plan; an evidence failure must win over an
    ``add``/``update``/``covered`` the reconcile produced, because reconcile
    only compares a fact against the region, never against the transcript.

    Unlike the reconcile candidates, this covers facts bound for an *empty*
    region too: nothing to conflict with is not evidence, and the first entry
    written into an empty always-loaded region is the one nothing later
    contradicts.
    """
    from ciao.fact_candidates import (
        candidate_from_proposal,
        normalize_transcript,
        validate_candidate,
    )
    from ciao.memory_tool import resolve_region

    transcript = normalize_transcript(filtered_jsonl)
    if transcript is None:
        return {}
    try:
        text = archive_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    body = _extract_insights_section(text)
    if not body:
        return {}

    rows: RegionDecisions = {}
    for proposal in propose_from_insights(body):
        if proposal.target not in ("memory", "profile"):
            continue
        promotable = _promotable_text(proposal.text)
        if promotable is None:
            # Already headed for the queue on shape grounds; a second reason
            # to queue it would change nothing.
            continue
        # The evidence is weighed against the text that would actually be
        # written — the `Durable rule:` clause — not the narration around it.
        verdict = validate_candidate(
            candidate_from_proposal(proposal),
            transcript,
            claim_text=promotable,
        )
        if verdict.ok:
            continue
        try:
            region = resolve_region(proposal.target)
        except ValueError:
            continue
        logger.info(
            "memory evidence: %r is unverified (%s: %s); queuing for review",
            promotable[:80],
            verdict.code,
            verdict.reason,
        )
        rows[_decision_key(region, promotable)] = _defer(
            f"unverified: {verdict.reason}", evidence=verdict.as_row()
        )
    return rows


def defer_region_facts(
    archive_path: Path,
    guide_path: Path,
    *,
    reason: str,
) -> RegionDecisions | None:
    """Defer every fact :func:`plan_region_reconcile` would have compared.

    The planner swallows its own failures, but a raise that escapes it — or any
    other reason a caller cannot run it — leaves ``region_decisions`` at
    ``None``, and ``None`` is the plain append path: the obsolete fact and its
    replacement both land in the always-loaded region. Callers use this instead
    of ``None`` so an un-run reconcile is as conservative as a failed one.

    Never raises: it is a fallback, and a fallback that throws would put the
    caller back on the append path it is here to avoid.
    """
    try:
        by_region, entries_by_region = _reconcile_candidates(archive_path, guide_path)
    except Exception:  # noqa: BLE001 — a failed fallback must not resurface
        logger.info("memory reconcile: could not build deferral rows")
        return None
    decisions: RegionDecisions = {}
    for region_name, candidates in by_region.items():
        competing = entries_by_region.get(region_name, [])
        for fact in candidates:
            decisions[_decision_key(region_name, fact)] = _defer(reason, competing)
    return decisions or None


async def plan_region_reconcile(
    archive_path: Path,
    guide_path: Path,
    *,
    model: str,
    provider: str = "claude",
    cwd: Path | None = None,
) -> RegionDecisions | None:
    """Decide ADD / UPDATE / COVERED for an archive's region-bound facts.

    The Mem0 pattern, done at write time where dedupe is cheap: before the
    sync apply step runs, one small model call per region compares the new
    facts against the region's current entries (the whole region fits in a
    prompt — it is capped at a few thousand characters). Returns a map from
    promotable fact text to its decision row, or None when there is nothing to
    reconcile — the caller then takes the plain append path, which never blocks
    archiving.

    A call that fails or replies unusably yields a ``defer`` row per candidate
    rather than no row at all: the candidates that reach a model call are the
    ones with existing region content to conflict with, and appending them
    unreconciled is what left an obsolete fact and its replacement both live in
    the region every session loads. Deferred facts stay in the proposals queue,
    so nothing is lost and a human resolves them against the current region.
    """
    # Candidates per region: state-shaped facts that are not already exact
    # duplicates (those need no model call to drop).
    by_region, entries_by_region = _reconcile_candidates(archive_path, guide_path)
    if not by_region:
        return None

    decisions: RegionDecisions = {}
    for region_name, candidates in by_region.items():
        rows = await _reconcile_region(
            region_name,
            entries_by_region[region_name],
            candidates,
            model=model,
            provider=provider,
            cwd=cwd,
        )
        if rows is None:
            # No row at all reads downstream as "no reconcile was run", which
            # is the plain append path. These candidates were compared against
            # a non-empty region, so that is the one thing it must not mean.
            for fact in candidates:
                decisions[_decision_key(region_name, fact)] = _defer(
                    f"reconcile unavailable for ciao:{region_name}",
                    entries_by_region[region_name],
                )
            continue
        for fact, row in zip(candidates, rows):
            decisions[_decision_key(region_name, fact)] = row

    return decisions or None


async def _reconcile_region(
    region_name: str,
    entries: list[str],
    candidates: list[str],
    *,
    model: str,
    provider: str = "claude",
    cwd: Path | None = None,
    timeout_s: float = _RECONCILE_TIMEOUT_S,
) -> list[ReconcileDecision] | None:
    """One reconcile call: decide ADD / UPDATE / COVERED per candidate.

    Returns one row per candidate in the given order, or None when the call
    failed or replied unparseably — :func:`plan_region_reconcile` then defers
    every candidate in the batch to the proposals queue, which never blocks and
    never loses a fact.

    An ``update`` row carries ``old``: the entry its index named in the snapshot
    the model actually saw. The apply step re-reads the region and refuses the
    update if that entry changed during the call, so a stale index cannot
    overwrite an unrelated fact.
    """
    from ciao.providers.oneshot import run_oneshot

    numbered = "\n".join(
        f"{index}. {entry}" for index, entry in enumerate(entries, start=1)
    )
    lettered = "\n".join(
        f"{chr(ord('A') + index)}. {fact}" for index, fact in enumerate(candidates)
    )
    prompt = (
        f"Region `ciao:{region_name}` current entries:\n{numbered}\n\n"
        f"Candidate facts:\n{lettered}\n"
    )
    try:
        reply = await run_oneshot(
            prompt,
            system_prompt=_RECONCILE_SYSTEM_PROMPT,
            model=model,
            timeout_s=timeout_s,
            provider=provider,
            cwd=cwd,
        )
    except Exception as exc:  # noqa: BLE001 — defer rather than append blind
        logger.info("memory reconcile: call failed (%s); deferring to review", exc)
        return None
    rows = _parse_reconcile_reply(reply, len(candidates))
    if rows is None:
        logger.info(
            "memory reconcile: unparseable reply for ciao:%s; deferring to review",
            region_name,
        )
        return None
    out: list[ReconcileDecision] = []
    for row in rows:
        if row.get("action") == "update":
            index = row.get("index")
            if not (isinstance(index, int) and 1 <= index <= len(entries)):
                # Out-of-range against the very snapshot the model saw, so the
                # decision is junk — but it still says this fact supersedes
                # something in the region, which is the one case a plain append
                # must not take.
                out.append(
                    _defer("update index outside the region snapshot", entries)
                )
                continue
            out.append(
                {
                    "action": "update",
                    "index": index,
                    "text": row.get("text", ""),
                    "old": entries[index - 1],
                }
            )
            continue
        if row.get("action") == "defer" and not row.get("competing"):
            # A row the parser could not read still competes with the whole
            # region it was weighed against; without that snapshot the queued
            # fact says only "something went wrong".
            out.append(_defer(row.get("reason") or "unreadable reconcile row", entries))
            continue
        out.append(row)
    return out


# The review path runs one call for one fact on a click, so it cannot wait the
# archive path's two minutes: the operator is watching a spinner, and the whole
# point of the retry is that they can take the deterministic append instead.
_REVIEW_RECONCILE_TIMEOUT_S = 45.0


async def reconcile_region_fact(
    guide_path: Path,
    target: str,
    text: str,
    *,
    model: str,
    provider: str = "claude",
    cwd: Path | None = None,
    timeout_s: float = _REVIEW_RECONCILE_TIMEOUT_S,
) -> ReconcileDecision | None:
    """Reconcile one queued fact against the region's *current* entries.

    This is the retry half of the deferral: a fact queued because the
    archive-time reconcile timed out, replied unusably, or named an entry that
    had moved is not stuck there — a later attempt reads the region as it is
    now and can come back with a usable ``add``/``covered``/``update``. The
    decision is planned against the snapshot read here and applied by
    :func:`_promote_to_region`, which re-reads under the guide lock and refuses
    on any drift, so the retry can only ever land against a region that has not
    changed since it was planned.

    ``None`` means "no model call was needed": an empty region (the first entry
    is a plain add), an exact duplicate (already remembered), or text the shape
    guard will reject anyway. Those are the deterministic paths, and spending a
    model call on them is the cost this deliberately avoids.

    A call that fails or replies unusably comes back as a ``defer`` row with
    its reason and the competing entries, exactly as at archive time: a retry
    that cannot decide must not become a licence to append.
    """
    from ciao.memory_audit import strip_learned_stamp
    from ciao.memory_tool import read_region, resolve_region

    promotable = _promotable_text(text)
    if promotable is None:
        return None
    try:
        region = resolve_region(target)
        entries, diags = read_region(guide_path, region)
    except Exception:  # noqa: BLE001 — the apply step reports a bad region
        return None
    if diags or not entries:
        return None
    if promotable in {strip_learned_stamp(entry) for entry in entries}:
        return None

    rows = await _reconcile_region(
        region,
        entries,
        [promotable],
        model=model,
        provider=provider,
        cwd=cwd,
        timeout_s=timeout_s,
    )
    if not rows:
        return _defer(f"reconcile unavailable for ciao:{region}", entries)
    return rows[0]


# ── Persistence ───────────────────────────────────────────────────────────


def _decided_with(workspace_vault_root: Path, text: str, key: str) -> bool:
    """Whether ``text`` was *rejected* before, not merely decided before.

    `append_proposals` dedupes against both pending bullets and the decision
    sidecar and returns None for either, so a caller cannot otherwise tell "it
    is already waiting for you" from "you rejected this before, and it will not
    come back". Those need different words: the second is the one that silently
    loses a user who has changed their mind.

    Reads the sidecar directly rather than through `_dismissed_texts`, which
    returns every decided text — `record_promotion` writes to the same log with
    a `promoted_at` key. Reusing it would tell someone their fact had been
    rejected when it was in fact accepted and may already be live.

    Compared through `_one_line`, exactly as `append_proposals` compares.
    """
    out_path = Path(workspace_vault_root) / _PROPOSALS_RELATIVE
    if not out_path.exists():
        return False
    wanted = _one_line(text)
    for suffix in (_DISMISSED_LOG_SUFFIX, *_DISMISSED_LOG_LEGACY_SUFFIXES):
        try:
            raw = out_path.with_suffix(suffix).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if key not in entry:
                # The oldest sidecar shape records neither timestamp; it is a
                # dismissal, exactly as `read_decisions` and the append-time
                # dedupe in `_dismissed_texts` already treat it. Requiring the
                # key made those rows invisible here, so re-filing a fact an
                # ancient install had rejected was reported as "already in the
                # queue" when no queue row existed.
                if key != "dismissed_at" or "promoted_at" in entry:
                    continue
            if entry.get("history_only"):
                # Ledger-only row; it decided nothing, so it must not report
                # the fact as promoted or dismissed.
                continue
            if _one_line(str(entry.get("text", ""))) == wanted:
                return True
    return False



def was_dismissed(workspace_vault_root: Path, text: str) -> bool:
    """Whether ``text`` was rejected before."""
    return _decided_with(workspace_vault_root, text, "dismissed_at")


def was_promoted(workspace_vault_root: Path, text: str) -> bool:
    """Whether ``text`` was accepted before, and so may already be live.

    The third state a caller needs. `append_proposals` refuses a re-file for a
    promoted fact exactly as it does for a dismissed one, and reporting either
    as "already in the queue" is wrong — a promoted fact is not in the queue at
    all, it is in its destination.
    """
    return _decided_with(workspace_vault_root, text, "promoted_at")


def append_proposals(
    proposals: list[MemoryProposal],
    workspace_vault_root: Path,
    *,
    source_path: Path | None = None,
    allow_dismissed: bool = False,
) -> Path | None:
    """Append a timestamped batch to ``Workspace/Memory-Proposals.md``.

    Read-merge-write under :func:`ciao.memory_receipts.queue_lock` so a
    concurrent undo (or another writer) cannot land between the dedupe read and
    the rewrite and be silently overwritten.
    """
    if not proposals:
        return None

    from ciao.memory_receipts import queue_lock, write_queue_atomically

    out_path = workspace_vault_root / _PROPOSALS_RELATIVE
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with queue_lock(out_path):
        if out_path.exists():
            # An existing file's header may predate the bounded-region layout and
            # still name `~/.ciao/memory.md` / `ciao memory add`. Refresh just that
            # header so the corrected wording reaches installed queues; bullets and
            # anything below them are left byte-identical.
            existing = _refresh_header(out_path.read_text(encoding="utf-8"))
        else:
            existing = _STUB_HEADER

        decided = _promoted_texts(out_path) if allow_dismissed else _dismissed_texts(out_path)
        already = _existing_proposal_texts(existing) | decided
        # Compare the text exactly as ``as_bullet`` will write it, or a proposal
        # whose text is only whitespace-different from a queued/dismissed one
        # slips past dedupe and lands as a visually identical duplicate row.
        fresh = [p for p in proposals if _one_line(p.text) not in already]
        if not fresh:
            return None

        header = _proposals_header_block(source_path)
        lines = [p.as_bullet() for p in fresh]
        block = header + "\n".join(lines) + "\n"

        write_queue_atomically(out_path, existing + "\n" + block)
        return out_path

# Matches a bullet written by ``MemoryProposal.as_bullet``.
_BULLET_RE = re.compile(r"^- \[[^\]]+\] (.+?)  _\(from: [^)]*\)_\s*$")

# Matches a timestamped batch header written by ``_proposals_header_block``.
_BATCH_HEADER_RE = re.compile(r"^## \d{4}-\d{2}-\d{2}T\S+")


def _sweep_empty_batches(lines: list[str]) -> list[str]:
    """Drop timestamped batch headers whose bullets are all gone.

    Dismissal removes bullets one line at a time, so the last dismissal in a
    batch left its ``## <timestamp>`` header behind forever; on a real queue
    29 of 30 batches were empty headers plus blank lines. Precision-first:
    only a header this module wrote (the timestamp shape) whose whole section
    is blank is swept — a section holding any other text was written by a
    hand or an agent and is not this function's to delete.
    """
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _BATCH_HEADER_RE.match(line):
            end = index + 1
            while end < len(lines) and not lines[end].startswith("## "):
                end += 1
            if all(not item.strip() for item in lines[index + 1:end]):
                index = end
                continue
        out.append(line)
        index += 1
    return out


def _existing_proposal_texts(file_text: str) -> set[str]:
    """Return the set of proposal texts already recorded in the file."""
    out: set[str] = set()
    for line in file_text.splitlines():
        m = _BULLET_RE.match(line)
        if m:
            out.add(m.group(1).strip())
    return out


_DISMISSED_LOG_SUFFIX = ".dismissed.jsonl"
_DISMISSED_LOG_LEGACY_SUFFIXES = (".dismissed.log",)


def dismissed_log_path(proposals_path: Path) -> Path:
    """Sidecar that holds the texts of already-decided proposals.

    ``.jsonl`` rather than ``.log`` because setup writes ``*.log`` into the
    workspace gitignore: the history only prevents re-filing while it stays
    put, so it must be tracked and sync like the queue itself.
    """
    return proposals_path.with_suffix(_DISMISSED_LOG_SUFFIX)


def record_dismissal(
    proposals_path: Path,
    *,
    text: str,
    kind: str = "",
    via: str = "",
    source: str = "",
    destination: str = "",
    outcome: str = "",
    proposal_id: str = "",
    receipt_id: str = "",
    once: bool = False,
) -> bool:
    """Record a decided proposal so the queue stops re-asking about it.

    ``append_proposals`` dedupes against bullets still in the queue file, so
    removing a dismissed row erases the only evidence of the decision: the
    next curator pass that re-reads the same transcript would re-file the
    fact verbatim. The decision therefore has to outlive the row itself, in
    a sidecar this module owns rather than in the outcomes ledger (which
    counts by kind and is trimmed).

    Accepted rows need the same treatment (``record_decision``): the nightly
    curator dedupes against the live queue and this sidecar, never against
    the promoted destination, so a promotion must record the text too or the
    same fact comes back the next time the transcript is re-read.

    The extra fields (``via``, ``source``, ``destination``, ``outcome``,
    ``proposal_id``) turn this dedupe sidecar into the decision history the
    review page's History tab reads; they are optional and omitted when
    blank so the on-disk shape stays backward compatible with the readers
    below, which only ever look at the ``*_at`` key and ``text``.
    """
    return _record_decision(
        proposals_path,
        text=text,
        kind=kind,
        key="dismissed_at",
        via=via,
        source=source,
        destination=destination,
        outcome=outcome,
        proposal_id=proposal_id,
        receipt_id=receipt_id,
        once=once,
    )


def record_promotion(
    proposals_path: Path,
    *,
    text: str,
    kind: str = "",
    via: str = "",
    source: str = "",
    destination: str = "",
    outcome: str = "",
    proposal_id: str = "",
    receipt_id: str = "",
    once: bool = False,
    history_only: bool = False,
) -> bool:
    """Record an accepted proposal in the same decision history.

    Same sidecar and the same reason as :func:`record_dismissal`: the dedupe
    in ``append_proposals`` reads only the queue file and this log, never the
    region/doc the promotion wrote to, so without this entry the curator
    re-queues the accepted fact on its next pass over the same transcript.
    Mirrors the CLI's promote-then-dismiss flow, which records the text for
    every removal regardless of the outcome action.
    """
    return _record_decision(
        proposals_path,
        text=text,
        kind=kind,
        key="promoted_at",
        via=via,
        source=source,
        destination=destination,
        outcome=outcome,
        proposal_id=proposal_id,
        receipt_id=receipt_id,
        once=once,
        history_only=history_only,
    )


def _record_decision(
    proposals_path: Path,
    *,
    text: str,
    kind: str,
    key: str,
    via: str = "",
    source: str = "",
    destination: str = "",
    outcome: str = "",
    proposal_id: str = "",
    receipt_id: str = "",
    once: bool = False,
    history_only: bool = False,
) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    log_path = dismissed_log_path(proposals_path)
    if once and _has_decision(proposals_path, key=key, text=cleaned, outcome=outcome):
        # Idempotent paths only. A decision the operator makes is a fresh event
        # every time, but the archive-time pipeline re-derives the same
        # "already applied, skipped" verdict on every pass over the same
        # transcript, and appending a row per pass grew the history without
        # recording anything new.
        return False
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        key: datetime.now(UTC).isoformat(timespec="seconds"),
        "kind": kind,
        "text": cleaned,
    }
    # Omit blanks rather than writing empty strings: keeps legacy-shaped
    # entries (just the ``*_at`` key, ``kind``, ``text``) indistinguishable
    # from ones written before these fields existed.
    if via:
        entry["via"] = via
    if source:
        entry["source"] = source
    if destination:
        entry["destination"] = destination
    if outcome:
        entry["outcome"] = outcome
    if proposal_id:
        entry["proposal_id"] = proposal_id
    if receipt_id:
        # The memory receipt (see :mod:`ciao.memory_receipts`) that performed
        # this decision's write. ``text`` above stays the ORIGINAL bullet,
        # because that is what append-time dedupe compares a re-extracted fact
        # against; when the operator edited the wording before accepting, the
        # receipt records the edited text and no text match can find it again.
        # Recording the id is what lets History show that decision's change and
        # offer an undo. Rows written before this existed carry no id and are
        # joined heuristically instead.
        entry["receipt_id"] = receipt_id
    if history_only:
        # Ledger-only row: it records that a pass ran and decided nothing new,
        # so the dedupe readers must not treat it as a decision. Without this
        # the row makes ``was_promoted`` true and ``append_proposals`` refuses
        # the fact forever, so a reverted in-session edit could never be
        # re-queued. ``_has_decision`` still sees it, which is what keeps
        # ``once=True`` idempotent.
        entry["history_only"] = True
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return True


# Parsed sidecar lines, keyed by path and invalidated on stat identity.
_SIDECAR_CACHE: dict[Path, tuple[tuple[int, int, int], list[dict[str, Any]]]] = {}


def _sidecar_entries(path: Path) -> list[dict[str, Any]]:
    """Parsed JSON-object lines of one decision sidecar, newest last.

    Cached on ``(mtime_ns, size, inode)``: the suppression path calls
    :func:`_has_decision` once per proposal while archiving, so a long-lived
    ledger was otherwise re-read and re-parsed O(proposals x history) times in
    a single pass. Sidecars are append-only (and any rewrite lands on a new
    inode), so stat identity is a sound key. Callers must treat the returned
    entries as read-only.
    """
    try:
        stat = path.stat()
    except OSError:
        _SIDECAR_CACHE.pop(path, None)
        return []
    stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    cached = _SIDECAR_CACHE.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        _SIDECAR_CACHE.pop(path, None)
        return []
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    _SIDECAR_CACHE[path] = (stamp, entries)
    return entries


def _sidecar_paths(proposals_path: Path) -> tuple[Path, ...]:
    """Current plus legacy sidecar paths for one proposal queue, current first."""
    return tuple(
        proposals_path.with_suffix(suffix)
        for suffix in (_DISMISSED_LOG_SUFFIX, *_DISMISSED_LOG_LEGACY_SUFFIXES)
    )


def _has_decision(proposals_path: Path, *, key: str, text: str, outcome: str) -> bool:
    """Whether this exact decision (action, text, outcome) is already recorded.

    Used only by ``once=True`` writers; matching on the outcome as well keeps a
    later real promotion of a fact recordable after an earlier "skipped,
    already known" row for the same text.
    """
    for path in _sidecar_paths(proposals_path):
        for entry in _sidecar_entries(path):
            if key not in entry:
                continue
            if str(entry.get("text", "")).strip() != text:
                continue
            if str(entry.get("outcome", "")) == outcome:
                return True
    return False


def _dismissed_texts(proposals_path: Path) -> set[str]:
    """Texts of previously decided proposals, from the sidecar log."""
    # Older sidecars recorded only ``text`` and ``kind``; keep treating those
    # entries as decisions when rebuilding the general dedupe set.
    return _decision_texts(proposals_path, keys=())


def _promoted_texts(proposals_path: Path) -> set[str]:
    """Texts of proposals previously accepted into their destination."""
    return _decision_texts(proposals_path, keys=("promoted_at",))


def _decision_texts(proposals_path: Path, *, keys: tuple[str, ...]) -> set[str]:
    out: set[str] = set()
    for path in _sidecar_paths(proposals_path):
        for entry in _sidecar_entries(path):
            if keys and not any(key in entry for key in keys):
                continue
            if entry.get("history_only"):
                # Ledger-only row; it decided nothing, so it must not dedupe.
                continue
            text = _one_line(str(entry.get("text", "")))
            if text:
                out.add(text)
    return out


def history_row_id(entry: dict[str, Any], workspace: str = "") -> str:
    """Stable id for one decision-history row, derived from its content.

    Unlike the live queue's :func:`ciao.proposal_tracking.stable_proposal_id`,
    this cannot key off a path/line: auto-apply only has a vault root, the CLI
    only has an optional workspace name, and legacy sidecar rows have neither.

    Timestamps are second-precision, so content alone is not unique: the same
    fact accepted in two workspaces within one second, or two undated legacy
    rows with identical text, hashed identically — and the History list keys
    its ``<li>`` on this id, so a collision dropped a row on patch. The
    ``workspace`` and the row's position in its sidecar (``log`` + ``seq``,
    both assigned by :func:`read_decisions`) disambiguate. They are stable
    across reads because each sidecar is append-only and ``seq`` counts within
    one sidecar — a shared counter would renumber every legacy row each time a
    decision landed in the current one.
    """
    import hashlib

    basis = "|".join(
        (
            str(entry.get("ts", "")),
            str(entry.get("action", "")),
            str(entry.get("kind", "")),
            str(entry.get("text", "")),
            workspace,
            str(entry.get("log", "")),
            str(entry.get("seq", "")),
        )
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def read_decisions(proposals_path: Path) -> list[dict[str, Any]]:
    """Every recorded decision for one workspace's proposal queue, newest last.

    Normalizes both the current sidecar shape and the legacy ``.dismissed.log``
    text-only rows into one shape: ``{ts, action, via, kind, text, source,
    destination, outcome, proposal_id, receipt_id, log, seq}``. ``receipt_id``
    is empty for every row written before it was recorded, and for every
    decision made outside the receipt protocol. This is the read side of the
    decision history the review page's History tab renders; :func:`record_dismissal`
    and :func:`record_promotion` are the write side.

    ``seq`` is the row's position *within its own* append-only sidecar, and
    ``log`` names that sidecar. Together they let :func:`history_row_id` tell
    apart two rows whose content is byte-identical; both are dropped before the
    row reaches the API payload. Counting per sidecar rather than across the
    concatenation matters: a shared counter offsets every legacy row by the
    current sidecar's length, so each new decision changed the id of every
    legacy row — the exact instability ``seq`` exists to prevent.
    """
    rows: list[dict[str, Any]] = []
    for suffix in (_DISMISSED_LOG_SUFFIX, *_DISMISSED_LOG_LEGACY_SUFFIXES):
        seq = 0
        for entry in _sidecar_entries(proposals_path.with_suffix(suffix)):
            text = _one_line(str(entry.get("text", "")))
            if not text:
                continue
            if "promoted_at" in entry:
                ts, action = str(entry["promoted_at"]), "accepted"
            elif "dismissed_at" in entry:
                ts, action = str(entry["dismissed_at"]), "dismissed"
            else:
                # Oldest sidecar shape: no timestamp, no action recorded.
                # Treat as a dismissal (append-time dedupe already did) but
                # never fabricate a time.
                ts, action = "", "dismissed"
            rows.append(
                {
                    "ts": ts,
                    "action": action,
                    "via": str(entry.get("via", "")),
                    "kind": str(entry.get("kind", "")),
                    "text": text,
                    "source": str(entry.get("source", "")),
                    "destination": str(entry.get("destination", "")),
                    "outcome": str(entry.get("outcome", "")),
                    "proposal_id": str(entry.get("proposal_id", "")),
                    "receipt_id": str(entry.get("receipt_id", "")),
                    "log": suffix,
                    "seq": seq,
                }
            )
            seq += 1
    return rows


_STUB_HEADER = (
    "---\n"
    "tags: [ciao, memory, proposals]\n"
    "---\n"
    "# Memory Proposals\n\n"
    "Auto-generated proposals from session-insights curation. Each batch is "
    "timestamped. Confident facts are applied automatically at archive time; "
    "what lands here waited because the model was unsure or a write failed.\n\n"
    "Destinations: `[memory]` / `[profile]` are the bounded `ciao:memory` / "
    "`ciao:profile` regions of the workspace `AGENTS.md` (edit the region "
    "first, then dismiss with `ciao memory-proposal-dismiss --text-file <file> "
    "--promoted` so the outcome counts as a promotion); "
    "`[project <doc-path>]` folds into that canonical doc; `[people <Name>]` "
    "updates `People/<Name>.md`; `[learnings]` appends to "
    "`Workspace/Learnings.md`; `[review]` has no known destination yet — "
    "decide what it is first. Standing directives stay in the AGENTS.md body "
    "outside the fenced regions.\n"
)


def _proposals_header_block(source_path: Path | None) -> str:
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    if source_path is not None:
        return f"\n## {ts} — from `{source_path.name}`\n\n"
    return f"\n## {ts}\n\n"


def _refresh_header(file_text: str) -> str:
    """Replace a stale leading header with the current one, or return unchanged.

    Only a file that begins with YAML frontmatter is touched: that is the
    signal the leading block is the generated header rather than a section a
    user wrote by hand. The scan for the first timestamped batch or proposal
    bullet starts only after the frontmatter closes, so an indented YAML list
    inside the frontmatter is never mistaken for a bullet. When the boundary
    cannot be identified confidently the text is returned byte-identical rather
    than guessed at, and no bullet and no user text is ever edited.
    """
    if not file_text.startswith("---\n"):
        return file_text
    lines = file_text.splitlines(keepends=True)
    content_start = 0
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            content_start = idx + 1
            break
    if not content_start:
        # Frontmatter opened and never closed. The boundary scan below would
        # then start at line 0 and mistake an indented YAML list item for a
        # proposal bullet, splicing the header into the middle of the
        # frontmatter. An unparseable file is left byte-identical.
        return file_text
    boundary = len(file_text)
    for idx in range(content_start, len(lines)):
        stripped = lines[idx].lstrip()
        if stripped.startswith("## ") or stripped.startswith("- "):
            boundary = sum(len(item) for item in lines[:idx])
            break
    if boundary >= len(file_text):
        return file_text
    return _STUB_HEADER + file_text[boundary:]


# ── Already-applied guard ─────────────────────────────────────────────────

# Minimum character overlap for a proposal to be considered already present in
# a destination file. Shorter than a meaningful fact would be noise; longer
# than a sentence would require exact formatting.
_APPLIED_MIN_OVERLAP = 40


def _normalize_for_match(text: str) -> str:
    """Lowercased, whitespace-collapsed form for containment checks."""
    return " ".join(text.lower().split())


_NEGATION_RE = re.compile(r"\b(?:do not|don't|never|avoid)\b")

# Where one fact ends and the next begins, in text that has already been
# through `_normalize_for_match`. That collapse removes every newline, so a
# markdown file arrives here as one line and the bullet markers that started
# each item are the only separator left between two independent facts. Without
# them a single "- Never commit secrets" bullet reads as the containing
# sentence of everything below it, and every later fact in the file is scored
# as negated — so an already-applied proposal fails this guard and is queued
# back into Review, which is the exact loop the guard exists to close.
_SEGMENT_SPLIT_RE = re.compile(r"[.!?;\n]|(?:^|(?<= ))(?:[-*+\u2022\u2013\u2014]|\d+\.) ")


def _positive_contains(text: str, needle: str) -> bool:
    """Match *needle* unless its containing sentence is negated."""
    start = 0
    while True:
        at = text.find(needle, start)
        if at < 0:
            return False
        boundary = 0
        for match in _SEGMENT_SPLIT_RE.finditer(text, 0, at):
            boundary = match.end()
        if not _NEGATION_RE.search(text[boundary:at]):
            return True
        start = at + 1


def _is_already_in_file(path: Path, proposal_text: str) -> bool:
    """True when *path* already contains the proposal's substance."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    norm_content = _normalize_for_match(content)
    norm_proposal = _normalize_for_match(proposal_text)
    if not norm_proposal:
        return False
    # Exact containment first (fast, precise for copy-paste facts like paths).
    if _positive_contains(norm_content, norm_proposal):
        return True
    if len(norm_proposal) < _APPLIED_MIN_OVERLAP:
        return False
    # Also check the promotable variant for memory/profile: the region holds
    # "Avoid em dashes; use commas instead." while the proposal may carry
    # "User said: ... Durable rule: Avoid em dashes; use commas instead."
    promotable = _promotable_text(proposal_text)
    if promotable and promotable != proposal_text:
        norm_promotable = _normalize_for_match(promotable)
        if len(norm_promotable) >= _APPLIED_MIN_OVERLAP and _positive_contains(norm_content, norm_promotable):
            return True
    # Do not use unordered token overlap here. It treats contradictory or
    # unrelated sentences as equivalent (for example, "use Python" versus
    # "do not use Python"). A false negative leaves a reviewable proposal;
    # a false positive silently loses a user fact.
    return False


def _is_already_in_region(guide_path: Path, region: str, proposal_text: str) -> bool:
    """True when a bounded region already holds the proposal."""
    from ciao.memory_tool import read_region

    try:
        entries, diags = read_region(guide_path, region)
    except Exception:
        return False
    if diags:
        return False
    # Exact promotable match is the canonical dedupe (see _promote_to_region),
    # but the guard also suppresses near-duplicates already present with
    # different punctuation/casing so they never reach the review tab.
    promotable = _promotable_text(proposal_text) or proposal_text
    norm_promotable = _normalize_for_match(promotable)
    for entry in entries:
        norm_entry = _normalize_for_match(entry)
        if norm_promotable == norm_entry:
            return True
        if len(norm_promotable) >= _APPLIED_MIN_OVERLAP and _positive_contains(norm_entry, norm_promotable):
            return True
        if len(norm_entry) >= _APPLIED_MIN_OVERLAP and _positive_contains(norm_promotable, norm_entry):
            return True
    return False


def _resolve_doc_path(vault_root: Path, doc_path: str) -> Path:
    """Resolve *doc_path* against *vault_root*, handling shared-prefix duplication.

    ``doc_path`` may be absolute, vault-relative, or already a full path that
    starts with the vault's own prefix (so ``vault_root / doc_path`` would
    duplicate). Try vault-relative first, then the literal path (cwd-relative
    or absolute), preferring whichever exists.
    """
    p = Path(doc_path)
    if p.is_absolute():
        return p
    candidate = vault_root / doc_path
    if candidate.exists():
        return candidate
    if p.exists():
        return p
    # Fall back to the vault-relative candidate even when neither exists yet;
    # the caller will check existence before reading.
    return candidate


def _is_already_applied(
    proposal: MemoryProposal,
    vault_root: Path,
    guide_path: Path | None,
    project_doc_path: str = "",
) -> bool:
    """True when the destination already holds the proposal's fact.

    This is the extra check before creating a review card: if the chat already
    applied the edit (via Edit/Write/memory_update in-session), the post-archive
    insight will re-extract the same fact and must not re-queue it.
    """
    if proposal.target in ("memory", "profile") and guide_path is not None:
        try:
            if guide_path.exists() and _is_already_in_region(
                guide_path, proposal.target, proposal.text
            ):
                return True
        except Exception:
            pass
    if proposal.target == "project":
        doc_path = proposal.payload or project_doc_path
        if doc_path:
            p = _resolve_doc_path(vault_root, doc_path)
            if _is_already_in_file(p, proposal.text):
                return True
    if proposal.target == "people" and vault_root is not None:
        name = proposal.payload or _safe_name(proposal.text.split("-")[0])
        p = vault_root / _PEOPLE_DIR / f"{_safe_name(name)}.md"
        if p.exists() and _is_already_in_file(p, proposal.text):
            return True
        # Also check if any people note already contains the fact (payload may
        # differ from canonical filename due to model paraphrase).
        try:
            people_dir = vault_root / _PEOPLE_DIR
            if people_dir.is_dir():
                for note in people_dir.glob("*.md"):
                    if _is_already_in_file(note, proposal.text):
                        return True
        except OSError:
            pass
    if proposal.target == "learnings" and vault_root is not None:
        p = vault_root / _LEARNINGS_RELATIVE
        if p.exists() and _is_already_in_file(p, proposal.text):
            return True
    if proposal.target == "review":
        # Review has no known destination; suppress only if the fact is
        # demonstrably already remembered somewhere obvious (memory/profile
        # regions or the canonical doc). Do not scan the whole vault.
        if guide_path is not None and guide_path.exists():
            for region in ("memory", "profile"):
                if _is_already_in_region(guide_path, region, proposal.text):
                    return True
        if project_doc_path:
            p = _resolve_doc_path(vault_root, project_doc_path)
            if p.exists() and _is_already_in_file(p, proposal.text):
                return True
    return False


# ── Known-entity routing ──────────────────────────────────────────────────


# A "New entities" bullet opens on "<type>: <name>" ("Person: Mo - ...",
# "Anthropic person: Finn Cummins (...)", "Project: ai-native-sdk / ...").
_ENTITY_SUBJECT_RE = re.compile(
    r"^(?P<type>[^:]{1,40}?)\s*:\s*(?P<name>.+?)(?:\s+[-–—]\s|\s*[(;,:]|$)"
)
# Project folder names that are containers, not a project a fact belongs to.
_ROUTING_SKIP_PROJECTS = frozenset({"general"})
_NON_PROJECT_STEMS = frozenset({"readme", "index", "log"})


def _is_scaffold(name: str) -> bool:
    """An index, template or `_`-prefixed scaffold, not a project a fact belongs to.

    Deliberately narrower than ``vault_lint.is_template_stem`` (a substring
    test), which would also drop a real project named ``email-templates``.
    """
    lowered = name.lower()
    return (
        lowered in _ROUTING_SKIP_PROJECTS
        or lowered in _NON_PROJECT_STEMS
        or lowered.startswith(("_", "template"))
        or lowered.endswith(("-template", "_template"))
    )
# Shorter names ("mo", "ux") match too much prose to count as a mention.
_MIN_ENTITY_MENTION = 4


def entity_key(name: str) -> str:
    """``Finn-Cummins`` / ``finn cummins`` / ``Finn_Cummins`` → ``finn cummins``."""
    return " ".join(re.sub(r"[-_]+", " ", name).lower().split())


def known_entities(vault_root: Path) -> tuple[dict[str, Path], dict[str, str]]:
    """Known projects (key → canonical doc) and people (key → note stem).

    The same roster the extraction prompt is shown (``insights._known_context_block``)
    read back so code can enforce what the prompt only asks: a fact about an
    entity the vault already has belongs in that entity's note.
    """
    projects: dict[str, Path] = {}
    people: dict[str, str] = {}
    try:
        for bucket in ("active", "completed"):
            folder = vault_root / "projects" / bucket
            if not folder.is_dir():
                continue
            for entry in folder.iterdir():
                if not entry.is_dir() or _is_scaffold(entry.name):
                    continue
                # README first, the order the app's own project-doc lookup
                # uses (`project_chats._project_doc_file`), so a folder with
                # both routes facts to the doc the app treats as canonical.
                for doc in (entry / "README.md", entry / f"{entry.name}.md"):
                    if doc.is_file():
                        projects.setdefault(entity_key(entry.name), doc)
                        break
        # The roster also lists single-file projects (``projects/<name>.md``);
        # a ``[project: <name>]`` tag naming one must resolve here too, or it
        # falls through to the chat's own doc.
        projects_dir = vault_root / "projects"
        if projects_dir.is_dir():
            for doc in projects_dir.glob("*.md"):
                if doc.is_file() and not _is_scaffold(doc.stem):
                    projects.setdefault(entity_key(doc.stem), doc)
        people_dir = vault_root / _PEOPLE_DIR
        if people_dir.is_dir():
            for note in people_dir.glob("*.md"):
                people.setdefault(entity_key(note.stem), note.stem)
    except OSError:
        logger.debug("memory proposals: could not read the entity roster", exc_info=True)
    return projects, people


def project_name(doc: Path) -> str:
    """The roster name of a project doc: its folder, or a single file's stem."""
    return doc.stem if doc.parent.name == "projects" else doc.parent.name


def entity_mention_counts(text: str, keys: Iterable[str]) -> dict[str, int]:
    """How often each entity key is named in *text* as a whole word.

    A key matches in its spaced or hyphenated spelling ("finn cummins",
    "finn-cummins"). One alternation, longest spelling first, scans the text
    once however many entities the vault has; the longest name wins where
    names overlap, so "finn cummins" is not also a mention of "finn".
    """
    # Keys are `entity_key`-normalized (no hyphens), so a spelling names
    # exactly one key.
    forms: dict[str, str] = {}
    for key in keys:
        for form in {key, key.replace(" ", "-")}:
            if len(form) >= _MIN_ENTITY_MENTION:
                forms[form] = key
    if not forms or not text:
        return {}
    pattern = re.compile(
        r"(?<![\w-])("
        + "|".join(re.escape(form) for form in sorted(forms, key=len, reverse=True))
        + r")(?![\w-])"
    )
    counts: dict[str, int] = {}
    for match in pattern.finditer(text.lower()):
        key = forms[match.group(1)]
        counts[key] = counts.get(key, 0) + 1
    return counts


def _unwrap_links(text: str) -> str:
    """Markdown links and wikilinks replaced by the words they display."""
    text = MARKDOWN_LINK_RE.sub(lambda m: m.group("label"), text)
    return WIKILINK_RE.sub(lambda m: m.group(2) or m.group(1), text)


def _known_project_doc(payload: str, projects: dict[str, Path]) -> Path | None:
    """The doc a ``[project: <name>]`` payload names, when it is a known project.

    The extraction prompt asks for the name exactly as the roster lists it;
    a model that writes the doc path instead still resolves by its folder.
    """
    if not payload.strip():
        return None
    raw = Path(payload.strip())
    # A path names its project by folder ("projects/active/wedding/notes/x.md"),
    # so every enclosing folder, innermost first, is tried before the stem.
    for candidate in (payload, *(parent.name for parent in raw.parents), raw.stem):
        doc = projects.get(entity_key(candidate))
        if doc is not None:
            return doc
    return None


def _address_tagged(
    proposal: MemoryProposal,
    projects: dict[str, Path],
    people: dict[str, str],
    *,
    own_doc: Path | None,
    own_doc_path: str = "",
    fold_wrote: bool,
) -> MemoryProposal | None:
    """Resolve a model-tagged destination against the vault roster.

    ``[project: <name>]`` naming a known project other than the chat's own
    goes to that project's doc — the extraction prompt lists the roster so
    the model can choose, and code resolves the choice instead of guessing
    from the wording. A bare ``[project]`` belongs to the chat's own doc:
    consumed (``None``) when the fold already read it, else addressed to it,
    and ``[review]`` in a chat that has no doc. ``[people: <Name>]`` is
    normalized to the existing note's stem, so "Finn Cummins" lands in
    ``People/Finn-Cummins.md`` rather than creating a second note.
    """
    if proposal.target == "people" and proposal.payload:
        stem = people.get(entity_key(proposal.payload))
        return replace(proposal, payload=stem) if stem else proposal
    if proposal.target != "project":
        return proposal
    named = _known_project_doc(proposal.payload, projects)
    if named is not None and not _same_doc(named, own_doc):
        return replace(proposal, payload=str(named))
    payload = proposal.payload.strip()
    if payload and named is None and "/" not in payload and not payload.endswith(".md"):
        # A named tag means "a different project"; one the roster cannot
        # resolve belongs to a human, not to this chat's doc. A path is the
        # older tag shape for this chat's own project and is handled below.
        return replace(proposal, target="review", payload="")
    if own_doc is not None:
        # The chat's resolved canonical doc is authoritative over a path the
        # model invented; only a roster name can move a fact elsewhere. Only
        # a bare [project] was the fold's to consume: the fold prompt skips
        # every named tag, so dropping one here would lose it.
        if fold_wrote and not proposal.payload:
            return None
        return replace(proposal, payload=own_doc_path or str(own_doc))
    # A General chat has no project document to own a project-scoped fact.
    # Keep the claim reviewable, never an unroutable project row.
    return replace(proposal, target="review", payload="")


def _same_doc(a: Path | str, resolved: Path | None) -> bool:
    """Whether *a* names the already-resolved doc, however *a* is spelled."""
    if resolved is None:
        return False
    try:
        return Path(a).resolve() == resolved
    except OSError:
        return Path(a) == resolved


def _route_to_known_entity(
    proposal: MemoryProposal,
    projects: dict[str, Path],
    people: dict[str, str],
) -> MemoryProposal:
    """Give a ``[review]`` fact about a known person or project its home.

    ``[review]`` means "nowhere to put this", and a review row cannot be
    accepted — so a fact about a project or person that already has a note
    reached the queue as a dead end. Routed, in order:

    * a "New entities" bullet whose subject is a known person or project →
      that note or doc. Whether it restates the note or changes it is not
      decidable from the wording (an update need not carry a date, and a
      restatement can), so it is routed either way and the accept's fold
      answers "already covered";
    * any bullet naming exactly one known project → that project's doc.

    Anything else stays ``[review]``. The destination accept still folds
    through a model call that can answer "already covered", so a routed row
    is a proposal, not a write.
    """
    if proposal.target != "review":
        return proposal
    text = proposal.text

    def routed(target: str, payload: str) -> MemoryProposal:
        return replace(proposal, target=target, payload=payload)

    if proposal.source_section == "New entities":
        # Unwrap links first: "project: [Wedding](./projects/...) - ..." names
        # Wedding, and the subject pattern would otherwise stop at the "(".
        subject = _ENTITY_SUBJECT_RE.match(_unwrap_links(text))
        if subject:
            key = entity_key(subject.group("name").split(" / ")[0])
            person = key in people and bool(
                re.search(r"\b(?:person|people)\b", subject.group("type"), re.I)
            )
            if person or key in projects:
                if person:
                    return routed("people", people[key])
                return routed("project", str(projects[key]))
    named = list(entity_mention_counts(text, projects))
    if len(named) == 1:
        return routed("project", str(projects[named[0]]))
    return proposal


# ── Facts the session already wrote ───────────────────────────────────────


_BACKTICK_TOKEN_RE = re.compile(r"`([^`\n]{4,})`")
# Vault changes to these trees record an episode, not a durable home; a fact
# that only reached a journal entry still deserves its own destination.
_EPISODIC_PREFIXES = ("journal/", "logs/")


def _session_vault_changes(insights_md: str) -> list[tuple[str, frozenset[int]]]:
    """``(path, cited idx)`` for each "Vault changes" bullet the extractor wrote."""
    out: list[tuple[str, frozenset[int]]] = []
    for item in _split_sections(insights_md).get("Vault changes", []):
        _kind, _payload, citations, text = _peel_trailing_metadata(item)
        # The citation rule asks for vault paths as relative Markdown links,
        # so "[Mo](./People/Mo.md) - ..." names People/Mo.md; a name with
        # spaces is `[Mo](<./People/Mo Salah.md>)` or `%20`-escaped. Match the
        # link before splitting on " - ", which a label may itself contain.
        link = MARKDOWN_LINK_RE.match(text.strip())
        if link:
            target = link.group("angle") or link.group("bare") or ""
            path = unquote(re.split(r"[#?]", target, maxsplit=1)[0])
        else:
            path = re.split(r"\s+[-–—]\s", text, maxsplit=1)[0].strip().strip("`")
        if path and citations:
            out.append((path, frozenset(citations)))
    return out


def _changed_file_text(path: str, vault_root: Path) -> str | None:
    """The current text of a Vault changes path, or None when it is unreadable.

    Paths come back vault-relative ("People/Mo.md") or workspace-relative
    ("work/commands/styleit.md", two levels above the vault).
    """
    rel = path.lstrip("./")
    for base in (vault_root, vault_root.parent, vault_root.parent.parent):
        candidate = base / rel
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
    return None


def _written_this_session(
    proposal: MemoryProposal,
    changes: list[tuple[str, frozenset[int]]],
    vault_root: Path,
) -> bool:
    """True when the fact is the session's own edit to a file, restated.

    The extractor lists what the session wrote under "Vault changes" and then,
    often, repeats the same edit as a Decision ("Chose to create `/styleit`
    ... in `work/commands/styleit.md`"). The destination guard cannot see
    that — the edit went to a file, not the region the proposal targets — so
    the review queue asked for a fact that was already saved.

    Precision-first, three signals together: the bullet cites a turn that a
    vault change also cites, it names that changed file in backticks, and the
    same file as it is now contains another term the bullet puts in backticks —
    so an unrelated edit to a file the fact merely mentions ("`scripts/test.sh`
    is the entry point" alongside a new flag in it) is not taken as the fact
    being saved. A command file named for what it defines (`/styleit` for
    `commands/styleit.md`) only has to exist. Anything unreadable stays
    reviewable.
    A User correction is never suppressed: "run tests via `scripts/test.sh`"
    can share a turn with the edit to that script and still be a standing
    preference the file itself does not state.
    """
    if proposal.source_section == "User corrections":
        return False
    if not proposal.citations or not changes:
        return False
    cited = set(proposal.citations)
    tokens = {
        tok.strip().lstrip("./").lower()
        for tok in _BACKTICK_TOKEN_RE.findall(proposal.text)
    }
    if not tokens:
        return False
    co_cited = [
        (path, path.lstrip("./").lower())
        for path, idx in changes
        if cited & idx
        and not any(
            path.lstrip("./").lower().startswith(p) or f"/{p}" in path.lower()
            for p in _EPISODIC_PREFIXES
        )
    ]
    for path, norm in co_cited:
        text = _changed_file_text(path, vault_root)
        if text is None:
            continue
        named = {
            tok for tok in tokens
            if tok == norm or norm.endswith("/" + tok) or tok.endswith("/" + norm)
        }
        # The evidence must be in the file the fact names, not in some other
        # file the same turn happened to edit.
        if named and any(tok in text for tok in tokens - named):
            return True
        # The name a command file defines: `/styleit` for `commands/styleit.md`.
        # Only a definition file counts — `deploy` naming `scripts/deploy.sh`
        # is a mention, and an unrelated edit to that script proves nothing.
        stem = Path(norm).stem
        if (
            len(stem) >= 5
            and stem in tokens
            and any(part in {"commands", "subagents", "agents"} for part in Path(norm).parent.parts)
        ):
            return True
    return False


# ── Pipeline entry point ──────────────────────────────────────────────────


def proposals_from_archive(
    archive_path: Path,
    workspace_vault_root: Path,
    *,
    auto_promote_memory: bool = False,
    guide_path: Path | None = None,
    stats: dict[str, int] | None = None,
    project_doc_path: str = "",
    project_fold_wrote: bool = False,
    region_decisions: RegionDecisions | None = None,
    workspace: str = "",
    error_out: list[str] | None = None,
    deferrals: list[DeferredFact] | None = None,
) -> Path | None:
    """Read an archived chat, route its insights, optionally auto-apply.

    With ``auto_promote_memory`` set, every confidently-addressed proposal is
    written straight to its destination via :func:`apply_proposals`;
    everything else — plus any write that failed — lands in the proposals
    file.

    ``project_doc_path`` is the chat's canonical doc (workspace-root-relative
    or absolute). When ``project_fold_wrote`` is true the fold consumed this
    archive's insights, so ``[project]`` bullets are dropped rather than
    queued; otherwise they are queued addressed to that doc so a one-click
    accept can fold them later.

    Returns the proposals file path when something was written, else None.
    Swallows all exceptions; this runs as a fire-and-forget step.

    ``stats``, when given, is filled with ``proposed`` (how many proposals were
    written to the file), ``promoted`` (how many were auto-applied) and
    ``deferred`` (how many region facts an uncertain reconcile or a failed
    evidence check sent to the queue instead of the region). The archived
    chat reports these counts back
    to the user, which the returned path alone cannot express. It stays an
    out-parameter so the return contract every existing caller relies on is
    unchanged.

    ``deferrals``, when given, is filled with one :class:`DeferredFact` per
    deferred region fact — its reason and the entries it competes with — so a
    caller can report *what* is waiting, not only how much.

    ``error_out``, when given, records a reason for an internal failure (the
    archive was unreadable, or a write/dedupe step raised). ``None`` alone
    cannot distinguish that from a legitimate no-op — "the archive carried no
    actionable facts" also returns ``None`` — so the resumable pipeline passes
    this list to settle the stage as failed rather than succeeded.
    """
    try:
        if not archive_path.exists():
            return None
        text = archive_path.read_text(encoding="utf-8")
        body = _extract_insights_section(text)
        if not body:
            return None
        proposals = propose_from_insights(body)

        projects, people = known_entities(workspace_vault_root)
        own_doc = (
            _resolve_doc_path(workspace_vault_root, project_doc_path).resolve()
            if project_doc_path
            else None
        )
        def route(p: MemoryProposal) -> MemoryProposal | None:
            addressed = _address_tagged(
                p,
                projects,
                people,
                own_doc=own_doc,
                own_doc_path=project_doc_path,
                fold_wrote=project_fold_wrote,
            )
            if addressed is None:
                return None
            routed = _route_to_known_entity(addressed, projects, people)
            if (
                routed.target == "review"
                and routed.source_section == "Decisions"
                and not (p.target == "project" and p.payload)
            ):
                # Same rule as `propose_from_insights`, for the bare [project]
                # decisions a General chat demoted to review above. A named
                # project the roster could not resolve stays for a human.
                return None
            return routed

        routed_all = [route(p) for p in proposals]
        dropped = sum(1 for p in routed_all if p is None)
        if dropped:
            logger.info(
                "memory proposals: dropped %d fact(s) already folded, restated, "
                "or with no destination from %s",
                dropped,
                archive_path.name,
            )
        proposals = [p for p in routed_all if p is not None]
        session_changes = _session_vault_changes(body)

        # Extra guard before creating a review card: if the chat already
        # applied the change in-session (via memory_update/Edit/Write), the
        # destination now contains the fact and the insight must not re-queue it.
        if proposals:
            filtered: list[MemoryProposal] = []
            suppressed = 0
            for _p in proposals:
                if _written_this_session(
                    _p, session_changes, workspace_vault_root
                ) or _is_already_applied(
                    _p, workspace_vault_root, guide_path, project_doc_path
                ):
                    suppressed += 1
                    logger.info(
                        "memory proposals: suppressed already-applied %r from %s",
                        _p.text[:80],
                        archive_path.name,
                    )
                    try:
                        record_promotion(
                            workspace_vault_root / _PROPOSALS_RELATIVE,
                            text=_p.text,
                            kind=_p.target,
                            via="auto",
                            source=archive_path.stem,
                            outcome="suppressed",
                            # Re-processing the same archive re-derives this
                            # same verdict; record it once, not once per pass.
                            once=True,
                            # The fact was applied in-session, not promoted
                            # through the queue. Keep the row out of the dedupe
                            # readers so a later revert can be re-queued.
                            history_only=True,
                        )
                    except Exception:  # noqa: BLE001 — recording must not break the pipeline
                        logger.info(
                            "memory proposals: could not record suppression for %r",
                            _p.text[:80],
                        )
                    continue
                filtered.append(_p)
            if suppressed:
                logger.info(
                    "memory proposals: suppressed %d already-applied fact(s) from %s",
                    suppressed,
                    archive_path.name,
                )
            proposals = filtered
            if not proposals:
                if stats is not None:
                    stats["proposed"] = 0
                    stats["promoted"] = stats.get("promoted", 0)
                return None

        if auto_promote_memory and proposals and curation_in_progress(workspace_vault_root):
            # A curation run is mid-consolidation: it read the region minutes
            # ago and will write back a rewritten body. An append landing
            # underneath that read is either lost to the rewrite or duplicated
            # by it, and neither outcome is visible to anyone. Standing down
            # costs nothing here — every proposal falls through to
            # `append_proposals` below, which is the queue the curation run is
            # about to work anyway.
            auto_promote_memory = False
            logger.info(
                "memory proposals: curation holds %s; queuing %d fact(s) from %s "
                "instead of auto-applying",
                workspace_vault_root,
                len(proposals),
                archive_path.name,
            )

        if auto_promote_memory and proposals:
            proposals, promoted = apply_proposals(
                proposals,
                guide_path=guide_path,
                vault_root=workspace_vault_root,
                region_decisions=region_decisions,
                learning_source=archive_path.stem,
                actor="auto",
                source="archive",
                workspace=workspace,
                stats=stats,
                deferrals=deferrals,
            )
            if promoted:
                if stats is not None:
                    stats["promoted"] = len(promoted)
                logger.info(
                    "memory proposals: auto-applied %d fact(s) from %s",
                    len(promoted),
                    archive_path.name,
                )
        written = append_proposals(
            proposals,
            workspace_vault_root,
            source_path=archive_path,
        )
        if stats is not None:
            # Counted from what was actually filed, not from what was parsed:
            # auto-apply removes the applied facts from the list above.
            stats["proposed"] = len(proposals) if written else 0
        return written
    except Exception as exc:  # noqa: BLE001 — never crash the pipeline
        logger.exception("memory proposals failed for %s", archive_path)
        if error_out is not None:
            error_out.append(
                f"{type(exc).__name__}: {exc}"[:400] or "memory proposals failed"
            )
        return None


def _extract_insights_section(archive_md: str) -> str:
    """Return the body of the archive's real appended insights section, or ''.

    Delegates to :func:`ciao.insights.locate_insights_section` so a transcript
    that merely quotes the header (curation chats do) is never mistaken for
    the appended section — the old first-occurrence match re-proposed bullets
    the curator had already reviewed and deleted from the queue.
    """
    from ciao.insights import locate_insights_section

    location = locate_insights_section(archive_md)
    if location is None:
        return ""
    return archive_md[location[1]:].strip()


# ── Review surface (CLI) ──────────────────────────────────────────────────


def list_proposals(
    proposals_path: Path,
) -> list[dict[str, str]]:
    """Structured pending proposal bullets from a workspace's queue.

    Reuses the shared proposal-kind grammar so the CLI, the web layer, and the
    audit always agree on what a bullet is. Each row carries the raw ``kind``,
    the bullet ``text``, the optional ``source`` tag, and the optional
    ``target`` payload (a person name or doc path).
    """
    from ciao.proposal_kinds import parse_bullet

    if not proposals_path.exists():
        return []
    rows: list[dict[str, str]] = []
    for raw in proposals_path.read_text(encoding="utf-8").splitlines():
        bullet = parse_bullet(raw)
        if bullet is not None:
            rows.append(
                {
                    "kind": bullet.kind,
                    "text": bullet.text,
                    "source": bullet.source,
                    "target": bullet.target,
                }
            )
    return rows


def find_proposal_matches(proposals_path: Path, needle: str) -> list[int]:
    """Line indices of the pending bullets ``needle`` matches.

    `remove_proposal_by_substring` returns None both for no match and for an
    ambiguous one, which a caller that wants to try a second needle cannot act
    on: retrying after an AMBIGUOUS match can uniquely hit a different row and
    delete the wrong proposal. Indices rather than a count, because two needle
    forms matching one row each is not the same as them matching the same row —
    only the identities say which.

    Same matching rule as the remover — casefolded substring over parsed
    bullets — so the two never disagree.
    """
    from ciao.proposal_kinds import parse_bullet

    if not proposals_path.exists():
        return []
    needle = needle.strip()
    if not needle:
        return []
    folded = needle.casefold()
    lines = proposals_path.read_text(encoding="utf-8").splitlines()
    return [
        index
        for index, line in enumerate(lines)
        if parse_bullet(line) is not None and folded in line.casefold()
    ]


def remove_proposal_by_substring(
    proposals_path: Path,
    needle: str,
) -> tuple[str, str] | None:
    """Remove the single proposal whose bullet matches ``needle``.

    Matches a unique substring case-insensitively across the pending bullets
    (the same contract the removed MCP resolve tool used). Returns the removed
    bullet's ``(kind, text)`` so the caller can record the decision, or None
    when the file is missing or no match exists. An ambiguous match (more
    than one) is left unresolved and returns None so the caller can ask for a
    longer substring.
    """
    from ciao.proposal_kinds import parse_bullet

    if not proposals_path.exists():
        return None
    needle = needle.strip()
    if not needle:
        return None
    from ciao.memory_receipts import queue_lock

    with queue_lock(proposals_path):
        lines = proposals_path.read_text(encoding="utf-8").splitlines()
        candidates: list[int] = []
        for index, line in enumerate(lines):
            if parse_bullet(line) is None:
                continue
            if needle.casefold() in line.casefold():
                candidates.append(index)
        if len(candidates) != 1:
            return None
        bullet = parse_bullet(lines[candidates[0]])
        if bullet is None:
            return None
        del lines[candidates[0]]
        lines = _sweep_empty_batches(lines)
        from ciao.memory_receipts import write_queue_atomically

        write_queue_atomically(proposals_path, "\n".join(lines).rstrip() + "\n")
        return bullet.kind, bullet.text


def dismiss_proposal_by_substring(
    proposals_path: Path,
    needle: str,
) -> bool:
    """Remove one matching proposal, answering in booleans.

    Thin view over :func:`remove_proposal_by_substring` for callers that only
    need to know whether anything was removed.
    """
    return remove_proposal_by_substring(proposals_path, needle) is not None
