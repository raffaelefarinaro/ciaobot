"""The skill-proposal queue, its identity, and the one writer that owns it.

A skill improvement suggestion used to be a loose Markdown file under
``<workspace vault>/Workspace/Skill-Proposals/``, written by the retired
weekly skill-evolution pass and listed by the review API as a bare filename.
Nothing in that shape could answer the three questions a review queue exists to
answer. There was no stable identity — the filename *was* the id, so a dated
proposal and today's were different rows for one finding. There was no record of
which sessions justified the change, so a re-run overwrote the previous run's
findings with its own. And there was no durable resolution distinct from file
deletion: unlinking was the only way to say "decided", which meant the next
pass re-derived the same suggestion as a new file, and a deleted proposal left
no trace that anyone had looked at it.

This module is the transport-neutral owner of the queue:

* :class:`SkillProposal` and :class:`SkillEvidence` — the record, and one
  observation behind it.
* :class:`SkillOrigin` — one finding on one learning, and what became of *that*
  finding. A record is one per skill, so it aggregates every finding the pass
  derived about it; without this link, accepting the record accepted all of them
  at once and every learning behind them went quiet in the same keystroke.
* :func:`learning_settlement` and :func:`learning_cleanup_eligibility` — the
  fold that answers "has this learning been dealt with?", and the hook a
  reconciliation run calls per learning to find out. Neither removes anything.
* :func:`proposal_id` — identity derived from ``(workspace, skill)`` and
  nothing else, so it is stable across evidence updates and correct even if the
  file is copied between workspaces. It is derived, never stored: a record's id
  is recomputed from where it is and what it is about, so the file and the id
  cannot drift apart.
* :func:`parse_proposal` / :func:`render_proposal` — one file shape, with the
  pre-#683 loose files still readable.
* :func:`read_queue` / :func:`upsert_proposal` / :func:`mark_implementing` /
  :func:`mark_outcome` / :func:`enumerate_proposal_ids` — the listing, the
  merging writer, the accept-into-chat lifecycle the server owns, and the one
  enumeration the API listing and the helper-chat archive check share so their
  ids cannot drift.
* :func:`render_implementation_prompt` — the prompt that chat is seeded with, so
  the scope of the work is the server's statement rather than the browser's.
* :func:`open_queue_names` — the same "still awaiting a decision" question
  asked by file name from a vault root, for the nightly curation worklist,
  which builds itself from files alone and holds no registry to name a
  workspace with.

There is no database and no second queue location. The files stay exactly where
they were, the write goes through the existing
:func:`ciao.memory_receipts.queue_lock` and
:func:`ciao.memory_receipts.write_queue_atomically` rather than a direct
``write_text``, and a decision lands in the workspace's existing
``Memory-Proposals.md`` decision sidecar through
``memory_proposals.record_dismissal`` / ``record_promotion`` — the same durable
record the memory queue already keeps, keyed by a synthetic ``skill:<name>``
text so a skill decision can never be read as a memory fact with the same
wording.

The file shape
--------------

``render_proposal`` is a pure function of the record, so the same record always
renders the same bytes and a merge that changed nothing writes nothing.
``parse_proposal`` reads both shapes, mapping the headings the retired evolution
prompt asked the model for (``What I noticed`` → problem, ``Suggested
improvement`` and ``Proposed edit`` → change, ``Why this should help`` →
rationale) and the ``Source sessions`` list into evidence. A heading this schema
does not name, and the prose before the first one, is appended to the rationale
rather than dropped: a legacy file's words are the finding, and the acceptance
test for a readable legacy record is that re-writing it loses none of them. That
includes
the file's own self-description ("This is a reviewable suggestion based on
repeated recent use"), which is prose this schema does not name and therefore
rides along in the rationale. Only the first level-1 heading becomes ``title``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ciao.learning_records import (
    LEARNINGS_RELATIVE,
    LearningRecord,
    entry_revision,
    normalized_statement,
)
from ciao.memory_proposals import read_decisions, record_dismissal, record_promotion
from ciao.memory_receipts import content_revision, queue_lock, write_queue_atomically

if TYPE_CHECKING:  # ``CiaoConfig`` is only ever a type here; duck-typed below.
    from ciao.config import CiaoConfig

logger = logging.getLogger(__name__)


#: Bumped only when the file shape changes incompatibly. A record carries the
#: version it was written under so a reader can tell "this is not mine" from
#: "this is mine and is empty".
SCHEMA_VERSION = 1

#: Where the queue lives, inside a workspace's vault. Unchanged from the loose
#: files this replaces: a proposal a user already has must not move.
QUEUE_REL = ("Workspace", "Skill-Proposals")

#: The memory proposal queue, whose decision sidecar records a skill decision.
#: ``record_dismissal``/``record_promotion`` derive the sidecar path from it, so
#: this is the path their readers (``_decided_with``) will look at too.
_DECISIONS_REL = ("Workspace", "Memory-Proposals.md")

#: The sidecar text a settlement is keyed by. Prefixed because the memory
#: queue's dedupe compares bare texts: a skill name and a memory fact must
#: never be able to collide into one decision.
_DECISION_PREFIX = "skill:"

# A record that has been decided. ``implementing`` is deliberately open: it is a
# proposal a chat is working on, so the review surface still shows it and a
# resolution helper must not archive itself until it lands. ``interrupted`` is
# open for the same reason and one more: an implementation that stopped is
# unfinished work, not an answer, so it stays queued and stays re-openable.
IMPLEMENTING = "implementing"
APPLIED = "applied"
DISMISSED = "dismissed"
NOT_APPLICABLE = "not_applicable"
INTERRUPTED = "interrupted"
LIFECYCLES = ("pending", IMPLEMENTING, APPLIED, DISMISSED, NOT_APPLICABLE, INTERRUPTED)
SETTLED_LIFECYCLES = frozenset({APPLIED, DISMISSED, NOT_APPLICABLE})

#: Every lifecycle an implementation can END in, settled or recoverable. The
#: chat that was working a proposal records one of these when it finishes, and
#: an interrupted run is refused ``applied`` by name — a chat that ended is not
#: evidence that a skill changed.
OUTCOME_LIFECYCLES = frozenset(SETTLED_LIFECYCLES | {INTERRUPTED})

#: The lifecycle a record waits in for a decision.
PENDING = "pending"

# ── Origin links ───────────────────────────────────────────────────────────

#: The version stamped into every stored origin. Bumped only when the shape
#: changes incompatibly, and read rather than assumed: an origin this code does
#: not fully understand is kept as an unattributable link, never interpreted.
ORIGIN_SCHEMA = 1

#: One origin's state. The record's lifecycles, plus the three answers only a
#: finding can give. Spelled as one vocabulary because the queue has exactly two
#: decisions to make about a link — has the lesson landed, or was the finding
#: rejected — and every one of these says something different about it.
ORIGIN_PENDING = PENDING
ORIGIN_IMPLEMENTING = IMPLEMENTING
ORIGIN_APPLIED = APPLIED
ORIGIN_DISMISSED = DISMISSED
ORIGIN_NOT_APPLICABLE = NOT_APPLICABLE
ORIGIN_INTERRUPTED = INTERRUPTED
#: The target already says this, so there was nothing to change. Only a
#: readback of the target can turn this into an ``applied``.
ORIGIN_ALREADY_COVERED = "already_covered"
#: Nobody can say whether the finding is real against what the target says now.
ORIGIN_UNCLEAR = "unclear"
#: The run that was working on it broke. Not an answer, so not a decision.
ORIGIN_FAILED = "failed"

ORIGIN_STATES: tuple[str, ...] = LIFECYCLES + (
    ORIGIN_ALREADY_COVERED,
    ORIGIN_UNCLEAR,
    ORIGIN_FAILED,
)

#: The two states that clear a learning. ``applied`` means the lesson is in the
#: target and someone verified it; ``dismissed`` means a person rejected that
#: finding on its own. Everything else leaves the learning Active, which is the
#: whole point: one accepted edit must not retire a lesson nobody looked at.
CLEARED_ORIGINS = frozenset({ORIGIN_APPLIED, ORIGIN_DISMISSED})

#: Decided-looking states that do not clear anything. Each needs one more
#: person-shaped fact — the target confirmed to already carry the lesson, or a
#: verdict on whether the finding ever held — and a queue cannot supply either.
REVIEW_ORIGINS = frozenset(
    {ORIGIN_ALREADY_COVERED, ORIGIN_NOT_APPLICABLE, ORIGIN_UNCLEAR}
)

#: An origin in one of these has not been decided: it is waiting, in flight, or
#: the run working on it stopped.
OPEN_ORIGINS = frozenset(ORIGIN_STATES) - CLEARED_ORIGINS - REVIEW_ORIGINS

#: Every origin state that is an answer rather than a wait. What a record's own
#: lifecycle is derived from: all of them decided closes the row, any of them
#: outstanding leaves it asking.
DECIDED_ORIGINS = CLEARED_ORIGINS | REVIEW_ORIGINS

# Section headings, as rendered. Order is the rendered order.
_RENDERED_HEADINGS: tuple[tuple[str, str], ...] = (
    ("Problem", "problem"),
    ("Proposed change", "change"),
    ("Rationale", "rationale"),
)
_EVIDENCE_HEADING = "Evidence"
#: The structured origin list, rendered as one compact JSON object per line
#: inside a fence. JSON rather than prose because every field is machine
#: identity: a `learning_id`, a revision and a state, none of which survive a
#: human-friendly rewrite of a bullet list. One object per line rather than one
#: array so a hand edit adds or removes one finding without touching the rest.
_ORIGINS_HEADING = "Origins"

# The headings a pass's prompt asks the model for, folded into the rendered
# ones. Without these table a re-run of an old file's finding would read as an
# unnamed section and land in the rationale.
_LEGACY_HEADINGS: dict[str, str] = {
    "what i noticed": "problem",
    "suggested improvement": "change",
    "proposed edit": "change",
    "why this should help": "rationale",
    **{heading.casefold(): field for heading, field in _RENDERED_HEADINGS},
}
# The old file's session table: one bullet per trajectory, first token the id.
_LEGACY_SOURCES_HEADING = "source sessions"

# Every key one stored origin may carry. An unknown key fails the object rather
# than being skipped: a payload this code does not fully understand may be
# saying something about the link that a partial read would drop on the next
# write, and a dropped origin is a learning quietly declared settled.
_ORIGIN_FIELDS = frozenset({
    "schema",
    "workspace",
    "learning_id",
    "source_revision",
    "finding",
    "summary",
    "state",
    "verification",
})

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
# A fence opener/closer, so a heading inside a code block stays content. This
# matters here specifically because the finding IS often a diff: a proposal that
# adds `## Usage` to a SKILL.md would otherwise be read as a section boundary,
# and the rest of the diff would land in the wrong field.
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_EVIDENCE_RE = re.compile(
    r"^- (?:`(?P<chat_id>[^`]*)`)?"
    r"(?: ?\((?P<archive>[^()]*)\))?"
    r"(?: ?turn (?P<turn>[^:]*?))?"
    r": (?P<excerpt>.*)$"
)
# A scalar that needs no quoting to be read back as the same string. ``schema``
# is a bare int, so digits lead; a YAML boolean spelling is quoted even though
# it matches, because ``lifecycle: no`` must not read as false.
_PLAIN_SCALAR_RE = re.compile(r"^[A-Za-z0-9][^#:{}[\],&*!|>'\"%@`]*$")
_YAML_WORDS = frozenset({"true", "false", "null", "yes", "no", "on", "off"})


@dataclass(frozen=True, slots=True)
class SkillEvidence:
    """One observation behind a proposal, and where it can be re-read.

    ``turn`` locates the observation inside ``archive`` and is empty for a
    whole-session observation, which is what a pass records: the trajectory it
    read is the evidence, not one turn of it. The triple is the dedupe key, so
    re-processing the same session adds nothing.
    """

    chat_id: str
    archive: str
    turn: str
    excerpt: str

    @property
    def key(self) -> tuple[str, str, str]:
        """The identity of this observation, for merge dedupe."""
        return (self.chat_id, self.archive, self.turn)


@dataclass(frozen=True, slots=True)
class SkillOrigin:
    """One finding on one learning, and the state of that finding.

    A proposal is one record per skill, so it aggregates: every finding the pass
    has derived about that skill lives here. ``origins`` is what stops that
    aggregation from being the answer to "was this learning dealt with?" — each
    entry names the ``learning_id`` it came from, the revision of
    ``Workspace/Learnings.md`` at filing, the finding it maps to, and what
    happened to *that* finding. A settlement recorded on the record alone used
    to clear every learning linked to the skill, so one accepted edit retired a
    lesson nobody had looked at.

    ``state`` is an explicit outcome, never a boolean, and only
    :data:`CLEARED_ORIGINS` clears a learning: ``applied`` means the lesson is
    in the target and ``verification`` names the receipt or the readback that
    proves it, and ``dismissed`` means a person rejected this finding on its
    own. ``already_covered`` needs the target confirmed to already carry the
    lesson, ``not_applicable`` and ``unclear`` need a person to say whether the
    finding ever held, and ``interrupted``/``failed`` are the absence of an
    answer — so all of them leave the learning Active.

    An origin with no ``learning_id`` is one this file could not read: a line an
    older writer or a hand edit left in a shape :func:`parse_origins` refuses.
    It is kept rather than dropped, and it blocks cleanup, because an
    unattributable finding could be the other half of this learning.
    """

    workspace: str = ""
    learning_id: str = ""
    source_revision: str = ""
    finding: str = ""
    summary: str = ""
    state: str = ORIGIN_PENDING
    verification: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """The identity of this origin, for merge dedupe.

        ``(learning_id, finding)``, with the finding compared in its normalized
        form so a pass that re-words the same finding adds a second origin to the
        same learning instead of splitting one finding in two. The workspace is
        not in the key because an origin is only ever stored in its own
        workspace's queue — a link naming another workspace is not a link, and
        :func:`learning_settlement` will not count it.
        """
        return (self.learning_id, normalized_statement(self.finding))

    @property
    def linked(self) -> bool:
        """Whether this origin can be attributed to a learning at all."""
        return bool(self.learning_id)

    @property
    def clears(self) -> bool:
        """Whether this origin's state is enough to retire its learning."""
        return self.state in CLEARED_ORIGINS

    def to_dict(self) -> dict[str, Any]:
        """The mapping stored in the file, with blanks omitted.

        ``schema`` is a bare int, like the learnings metadata comment's: a
        number is the one token a reader can compare without first deciding what
        a missing key meant.
        """
        raw: dict[str, Any] = {
            "schema": ORIGIN_SCHEMA,
            "workspace": self.workspace,
            "learning_id": self.learning_id,
            "source_revision": self.source_revision,
            "finding": " ".join(self.finding.split()),
            "summary": " ".join(self.summary.split()),
            "state": self.state,
            "verification": self.verification,
        }
        return {name: value for name, value in raw.items() if value}


@dataclass(frozen=True, slots=True)
class OriginRef:
    """Which linked origin one settlement is about.

    ``learning_id`` alone names every origin of that learning on this record;
    adding ``finding`` narrows it to one finding. A ref that matches nothing is
    refused rather than quietly settling the record, because a caller naming a
    finding this proposal does not carry has a bug worth hearing about rather
    than a decision worth recording.
    """

    learning_id: str
    finding: str = ""

    def matches(self, origin: SkillOrigin) -> bool:
        """Whether ``origin`` is the one this ref names."""
        if origin.learning_id != self.learning_id:
            return False
        return not self.finding or normalized_statement(origin.finding) == (
            normalized_statement(self.finding)
        )


@dataclass(frozen=True, slots=True)
class SkillProposal:
    """One skill's open or settled improvement proposal.

    ``id`` is derived from ``(workspace, skill)`` by :func:`proposal_id` and
    never stored in the file. ``canonical_path`` and ``reviewed_revision`` name
    the exact bytes the proposal was written against, so a later reader can tell
    that the skill has changed since — the same revision-before-write contract
    :mod:`ciao.skills_inventory` resolves an owned source for. Which sources may
    be edited at all is that module's rule, not this record's.

    ``origins`` is the record's link back to the learnings it was derived from,
    one entry per finding, and is empty on every proposal filed before they
    existed. Empty is not the same as unlinked-and-settled: a proposal with no
    origins has nothing to fold, so every learning it might have covered stays
    exactly as Active as it was.
    """

    id: str
    workspace: str
    skill: str
    canonical_path: str
    reviewed_revision: str
    title: str
    problem: str
    change: str
    rationale: str
    sources: tuple[SkillEvidence, ...]
    lifecycle: str
    chat_id: str
    updated_at: str
    origins: tuple[SkillOrigin, ...] = ()


def queue_dir(config: CiaoConfig, workspace: str) -> Path:
    """The queue folder for one workspace.

    Derived from the registry, so there is one location per workspace and
    nothing may aim a write anywhere else.
    """
    return Path(config.workspace_vault_root(workspace)).joinpath(*QUEUE_REL)


def proposal_path(config: CiaoConfig, workspace: str, skill: str) -> Path:
    """The one file that holds ``skill``'s record in ``workspace``.

    Named after the skill, so one skill has one record: a re-run updates it
    rather than forking a second row for the same finding.
    """
    _check_skill_name(skill)
    return queue_dir(config, workspace) / f"{skill}.md"


def proposal_rel_path(workspace: str, skill: str) -> str:
    """The record's path relative to the vault root, as an API row carries it."""
    return Path(workspace).joinpath(*QUEUE_REL, f"{skill}.md").as_posix()


def proposal_id(workspace: str, skill: str) -> str:
    """The stable identity of one skill's proposal in one workspace.

    Content-derived from the pair and nothing else, so evidence updates, a
    rewrite, and a move of the file all leave it alone, and two workspaces
    holding the same skill name are two different proposals.
    """
    return hashlib.sha256(f"{workspace}\x00{skill}".encode("utf-8")).hexdigest()[:16]


def decision_text(skill: str) -> str:
    """The synthetic sidecar text a settlement is recorded under."""
    return f"{_DECISION_PREFIX}{skill}"


def parse_proposal(path: Path, workspace: str) -> SkillProposal | None:
    """Read one queue file into a record, or ``None`` when there is none to read.

    The file's name is what says which skill the record is about, so a
    frontmatter ``skill:`` that disagrees with it is ignored: the name is the
    queue's addressing, and a record that disagreed with the file it lives in
    would be written to a second file on the next merge.

    A file this cannot speak for at all — unreadable, or empty — reads as
    ``None`` rather than raising: a queue you cannot show is worse than one row
    short, and the writer that finds this will write a fresh record.
    """
    read = _read_record(path)
    if read is None:
        return None
    front, body = read
    skill = path.stem
    if not skill:
        return None
    fields = _parse_body(body, workspace)
    return SkillProposal(
        id=proposal_id(workspace, skill),
        workspace=workspace,
        skill=skill,
        canonical_path=str(front.get("canonical_path", "")),
        reviewed_revision=str(front.get("reviewed_revision", "")),
        title=fields["title"],
        problem=fields["problem"],
        change=fields["change"],
        rationale=fields["rationale"],
        sources=tuple(fields["sources"]),
        lifecycle=_front_lifecycle(front),
        chat_id=str(front.get("chat_id", "")),
        updated_at=str(front.get("updated_at", "")),
        origins=tuple(fields["origins"]),
    )


def render_proposal(proposal: SkillProposal) -> str:
    """The record's canonical file text: a pure function of the record.

    Deterministic because every consumer depends on it. The no-op merge in
    :func:`upsert_proposal` compares the rendered bytes with what is on disk,
    and a preview that rendered differently from the stored file would show a
    document the queue does not hold.

    ``## Origins`` is a body section rather than frontmatter because the origin
    list is a list of objects: the frontmatter reader is line-based and would
    drop a wrapped block, and a single line of compact JSON is unreadable to the
    person who has to check a link by hand. The fence is what keeps the payload
    from being read as prose — and, symmetrically, what keeps prose inside it
    from being read as a heading.
    """
    front = [
        "---",
        f"schema: {SCHEMA_VERSION}",
        # The old files carried this, and it is what a vault index reads to
        # recognize one of these as a proposal rather than a note.
        "type: skill-proposal",
        f"workspace: {_scalar(proposal.workspace)}",
        f"skill: {_scalar(proposal.skill)}",
    ]
    if proposal.title:
        front.append(f"title: {_scalar(proposal.title)}")
    front.append(f"lifecycle: {_scalar(proposal.lifecycle)}")
    if proposal.canonical_path:
        front.append(f"canonical_path: {_scalar(proposal.canonical_path)}")
    if proposal.reviewed_revision:
        front.append(f"reviewed_revision: {_scalar(proposal.reviewed_revision)}")
    if proposal.chat_id:
        front.append(f"chat_id: {_scalar(proposal.chat_id)}")
    if proposal.updated_at:
        front.append(f"updated_at: {_scalar(proposal.updated_at)}")
    front.append("---")

    body: list[str] = []
    if proposal.title:
        body.append(f"# {proposal.title}\n")
    for heading, field in _RENDERED_HEADINGS:
        value = getattr(proposal, field)
        if value:
            body.append(f"## {heading}\n\n{value}\n")
    if proposal.sources:
        body.append(f"## {_EVIDENCE_HEADING}\n\n{_render_evidence(proposal.sources)}\n")
    if proposal.origins:
        rendered = _render_origins(proposal.origins)
        body.append(f"## {_ORIGINS_HEADING}\n\n```json\n{rendered}\n```\n")
    return "\n".join(front) + "\n\n" + "\n".join(body)


def split_findings(text: str) -> tuple[str, str, str]:
    """A model's proposal text as the record's ``(problem, change, rationale)``.

    The same heading table the parser uses, so what a pass writes and what a
    later parse reads back are one vocabulary. Anything the table does not name
    rides along in the rationale, which is also what keeps a legacy file's words
    when it is rewritten.
    """
    fields = _parse_body(text)
    return fields["problem"], fields["change"], fields["rationale"]


def read_records(config: CiaoConfig, workspace: str) -> list[SkillProposal]:
    """Every record in ``workspace``'s queue, settled ones included, by skill.

    :func:`read_queue` filtered to what is still open, which is right for a
    review listing and wrong for anything asking "what has been said about this
    learning": a learning split across two proposals is settled only when *both*
    have been answered, and one of them answered is not in the queue any more.
    So the fold that decides a learning reads the settled records too.

    Sorted by skill so a walk is stable across runs, and one file read per
    record — the same read :func:`read_queue` makes, so a record cannot be
    counted twice or not at all depending on which function asked.
    """
    directory = queue_dir(config, workspace)
    if not directory.is_dir():
        return []
    found: list[SkillProposal] = []
    for path in sorted(directory.glob("*.md")):
        proposal = parse_proposal(path, workspace)
        if proposal is not None:
            found.append(proposal)
    found.sort(key=lambda item: item.skill)
    return found


def read_queue(config: CiaoConfig, workspace: str) -> list[SkillProposal]:
    """Every record in ``workspace`` still awaiting an answer, by skill name.

    A settled record is absent, which is what makes this the pending set: a
    dismissed proposal stays on disk as a record of the decision, and the next
    pass merges new evidence into it without re-asking.

    Sorted by skill so a listing is stable across runs, and one file read per
    record. :func:`enumerate_proposal_ids` is the same walk reduced to ids.
    """
    return [
        proposal
        for proposal in read_records(config, workspace)
        if proposal.lifecycle not in SETTLED_LIFECYCLES
    ]


def open_queue_names(vault_root: Path) -> list[str]:
    """The queue file names in ``vault_root`` that are still awaiting a decision.

    The vault-root form of :func:`read_queue`, for the caller that holds a root
    and no registry: the nightly curation worklist computes itself from files
    alone, so it cannot name a workspace and cannot ask for ids. What it wants
    is per-file — "is this record still open" — so the answer is names, and the
    lifecycle is read through the same parser the listing uses.

    A settled record stays on disk, because the decision is worth keeping. So
    this is the difference between a worklist that asks a question once and one
    that re-asks an answered question every night: a settled file is absent
    rather than reported, and a file this cannot read is absent too, matching
    what the review listing will show.
    """
    directory = Path(vault_root).joinpath(*QUEUE_REL)
    if not directory.is_dir():
        return []
    names: list[str] = []
    for path in sorted(directory.glob("*.md")):
        read = _read_record(path)
        if read is None:
            continue
        if _front_lifecycle(read[0]) not in SETTLED_LIFECYCLES:
            names.append(path.name)
    return names


def enumerate_proposal_ids(config: CiaoConfig) -> set[str]:
    """Every open skill-proposal id across every registered workspace.

    The one enumeration: the review listing and the helper-chat archive check
    both need "which proposals are still open", and two implementations of that
    question is how the ids that never matched — and the helper chats that
    consequently never archived — started.
    """
    ids: set[str] = set()
    for workspace in config.workspace_names():
        ids.update(proposal.id for proposal in read_queue(config, workspace))
    return ids


def upsert_proposal(config: CiaoConfig, proposal: SkillProposal) -> SkillProposal:
    """Merge ``proposal`` into its workspace's queue; return what is stored.

    Under the queue lock, so the read-merge-write is atomic against a concurrent
    settlement or a second pass. The merge keeps what the record already says
    and adds only what this proposal has that it lacks:

    * evidence is merged and deduped by ``(chat_id, archive, turn)``, so
      re-processing the same session changes nothing and a new one is appended
      rather than replacing what the last pass found;
    * identity, lifecycle and the recorded chat are never taken from an incoming
      record — a settle is a decision, and a re-run must not undo it, nor drop a
      record a chat is mid-way through implementing back to pending;
    * a field the incoming record leaves empty does not blank the stored one, so
      a stub write cannot erase a good finding;
    * an origin a filing brings is filed ``pending``, never with the state or the
      verification it claims, because a pass that routed a learning cannot know
      whether the lesson landed;
    * ``updated_at`` moves only when something else did, which is what makes an
      unchanged re-run a true no-op rather than a rewrite that only differs by
      its clock.

    A record whose skill has already been decided stays decided, whether that is
    visible in the file or only in the decision sidecar (a file deleted by the
    CLI still has its decision on record), and a settled record keeps
    accumulating evidence without re-entering the queue. A record that links
    learnings is the exception, and deliberately so: its lifecycle comes from
    its findings (:func:`_merged_lifecycle`), so a settled one whose siblings are
    still outstanding — or which has just been linked to a new finding — reopens
    instead of stranding them.
    """
    path = proposal_path(config, proposal.workspace, proposal.skill)
    with queue_lock(path):
        existing = parse_proposal(path, proposal.workspace)
        merged = _merge(config, existing, proposal)
        text = render_proposal(merged)
        current = ""
        if path.is_file():
            try:
                current = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                current = ""
        if text != current:
            write_queue_atomically(path, text)
            logger.info("Wrote skill proposal %s", path)
        _prune_legacy_dated(path, proposal.skill)
    return merged


def settle_proposal(
    config: CiaoConfig,
    proposal_id: str,
    lifecycle: str,
    *,
    chat_id: str = "",
    reason: str = "",
    via: str = "pwa",
    selectors: Sequence[OriginRef] | None = None,
    verification: str = "",
) -> SkillProposal | None:
    """Record a decision about one open proposal, then close it.

    ``proposal_id`` is a :func:`proposal_id` over every registered workspace; the
    record is not found (``None``) when no open proposal has it, which is the
    idempotent case — a second dismiss of the same row, or one whose file is
    already gone, has nothing left to decide. ``via`` names the surface the
    decision was made on, for the decision history.

    The decision goes into the workspace's existing memory-queue decision
    sidecar under :func:`decision_text`, because that sidecar is what outlives
    the row: a settled record may be deleted later, and the next pass must not
    re-ask what was already answered. It is written BEFORE the record flips, so
    a sidecar that could not be written leaves the proposal open and visible
    rather than settled and unrecorded. ``reason`` rides along in the sidecar's
    ``outcome``, the free-text slot the review History tab already renders.

    **Settlement is per linked origin.** ``selectors`` names which of the
    record's :class:`SkillOrigin` entries this decision is about, and only those
    move: the siblings on the same record keep asking, because one skill's
    record aggregates every finding the pass derived about it and a person who
    accepted one of them has not accepted the rest. ``selectors=None`` is the
    person dismissing or applying the row itself, and settles all of them — but
    the record only *closes* once every origin is decided, so a partial
    acceptance leaves the row queued and honest about what is left.

    ``verification`` is what makes an ``applied`` an ``applied``: a managed write
    receipt, or the readback a helper-authored skill edit recorded. It is
    required before any linked origin can be marked applied, because "the chat
    ran" and "the queue row is gone" are the two things the queue can see for
    itself, and neither is the lesson being in the target.

    Raises ``ValueError`` for a lifecycle that is not a decision; a caller that
    invents one would otherwise write a record no reader can place. Also for a
    selector that matches no origin, for one naming a finding that has already
    been answered, and for an ``applied`` that arrives with no verification.
    """
    if lifecycle not in SETTLED_LIFECYCLES:
        raise ValueError(
            f"{lifecycle!r} is not a settled lifecycle: expected one of "
            f"{', '.join(sorted(SETTLED_LIFECYCLES))}"
        )
    for workspace in config.workspace_names():
        for proposal in read_queue(config, workspace):
            if proposal.id == proposal_id:
                return _settle(
                    config,
                    proposal,
                    lifecycle,
                    chat_id=chat_id,
                    reason=reason,
                    via=via,
                    selectors=selectors,
                    verification=verification,
                )
    return None


def _settle(
    config: CiaoConfig,
    proposal: SkillProposal,
    lifecycle: str,
    *,
    chat_id: str,
    reason: str,
    via: str,
    selectors: Sequence[OriginRef] | None = None,
    verification: str = "",
) -> SkillProposal:
    """Write the decision, then flip the record. Order is the safety property."""
    record = record_promotion if lifecycle == APPLIED else record_dismissal
    decisions = Path(config.workspace_vault_root(proposal.workspace)).joinpath(
        *_DECISIONS_REL
    )
    path = proposal_path(config, proposal.workspace, proposal.skill)
    with queue_lock(path):
        stored = parse_proposal(path, proposal.workspace) or proposal
        settled, decided = _apply_outcome(
            stored,
            lifecycle,
            chat_id=chat_id,
            selectors=selectors,
            verification=verification,
        )
        # One sidecar row per origin this decision named, so a learning's history
        # says which finding was answered rather than only that the skill row
        # was. A record with no origins keeps the single row it has always
        # written, and the decision text stays the same synthetic ``skill:<name>``
        # in every case so a skill decision can never read as a memory fact.
        for origin in decided or (None,):
            record(
                decisions,
                text=decision_text(proposal.skill),
                kind="skill",
                via=via,
                outcome=reason,
                proposal_id=proposal.id,
                learning_id=origin.learning_id if origin else "",
                finding=origin.finding if origin else "",
            )
        write_queue_atomically(path, render_proposal(settled))
    logger.info("Settled skill proposal %s as %s", proposal.id, lifecycle)
    return settled


def _apply_outcome(
    proposal: SkillProposal,
    outcome: str,
    *,
    chat_id: str = "",
    selectors: Sequence[OriginRef] | None = None,
    verification: str = "",
) -> tuple[SkillProposal, tuple[SkillOrigin, ...]]:
    """The record after one outcome, and the origins that outcome decided.

    Pure: it reads the record and returns what should replace it, so the caller
    can write the decision before anything on disk changes. A record carrying no
    origins takes the outcome as the record's own lifecycle, which is what every
    proposal filed before origins existed has always done — and is why an
    unlinked proposal cannot retire anything: there is no learning for it to fold.

    A selector that names a finding which has already been answered is refused
    (:data:`CLEARED_ORIGINS` never moves): naming a finding says which decision
    this is about, and a decision is not something a second decision undoes.
    """
    if outcome not in ORIGIN_STATES:
        raise ValueError(
            f"{outcome!r} is not a finding outcome: expected one of "
            f"{', '.join(ORIGIN_STATES)}"
        )
    if not proposal.origins:
        if selectors:
            raise ValueError(
                f"skill proposal {proposal.id} links no learning, so it has no "
                f"origin to settle as {outcome!r}"
            )
        return (
            replace(
                proposal, lifecycle=outcome, chat_id=chat_id or proposal.chat_id
            ),
            (),
        )
    if outcome == ORIGIN_APPLIED and not verification.strip():
        raise ValueError(
            f"skill proposal {proposal.id} links "
            f"{len(proposal.origins)} learning finding(s); marking one applied "
            "needs a verification — a managed write receipt, or the readback a "
            "helper-authored skill edit recorded. Chat completion and a queue row "
            "leaving the listing are not evidence that the lesson is in the "
            "target."
        )
    named = _named_origins(proposal.origins, selectors)
    if selectors and not named:
        raise ValueError(
            f"skill proposal {proposal.id} has no origin for "
            + ", ".join(sorted({ref.learning_id for ref in selectors}))
        )
    if selectors:
        settled_already = [origin for origin in named if origin.clears]
        if settled_already:
            # A selector says which finding this decision is about, so it cannot
            # be read as permission to decide it differently: it would turn a
            # verified lesson into a rejected finding on the strength of a
            # learning id and a sentence, with nobody having looked at the
            # target. Refused rather than overridden, and the caller is told
            # which finding is already answered.
            raise ValueError(
                f"skill proposal {proposal.id} already answers "
                + ", ".join(
                    f"{origin.finding!r} as {origin.state}"
                    for origin in settled_already
                )
                + "; a finding selector is not a way to un-decide it"
            )
    targets = named if selectors else list(proposal.origins)
    # Identity, not equality: two origins that happen to carry the same fields
    # are still two entries in the list, and only the one the selector named may
    # move.
    moving = {id(item) for item in targets}
    proof = verification.strip()
    decided: list[SkillOrigin] = []
    moved: list[SkillOrigin] = []
    for origin in proposal.origins:
        if id(origin) not in moving:
            moved.append(origin)
            continue
        # An origin that already cleared keeps its state: a later aggregate
        # settlement of the record must not turn a verified lesson back into
        # "dismissed", and a decision is not something a second decision undoes.
        if origin.clears and not selectors:
            moved.append(origin)
            decided.append(origin)
            continue
        settled_origin = replace(
            origin, state=outcome, verification=proof or origin.verification
        )
        moved.append(settled_origin)
        decided.append(settled_origin)
    updated = replace(
        proposal, origins=tuple(moved), chat_id=chat_id or proposal.chat_id
    )
    closed = _closed_lifecycle(moved)
    if closed:
        updated = replace(updated, lifecycle=closed)
    return updated, tuple(decided)


def _named_origins(
    origins: tuple[SkillOrigin, ...], selectors: Sequence[OriginRef] | None
) -> list[SkillOrigin]:
    """The origins ``selectors`` names, in record order. ``None`` means all."""
    if selectors is None:
        return list(origins)
    return [
        origin
        for origin in origins
        if any(ref.matches(origin) for ref in selectors)
    ]


def _closed_lifecycle(origins: Sequence[SkillOrigin]) -> str:
    """The lifecycle a record takes once every origin is answered, or ``""``.

    A finding still outstanding leaves the record asking, which is the point of
    splitting settlement in the first place. So does one that is answered but
    unconfirmed: ``already_covered`` and ``unclear`` are claims only somebody
    looking at the target can settle, and the record lifecycle has no word for
    either — inventing one would archive a finding nobody rejected.

    When they are all decisions the row is done, and the lifecycle it takes is
    the strongest answer among them: the skill changed if any finding landed,
    even if its siblings were rejected.
    """
    closable = CLEARED_ORIGINS | {ORIGIN_NOT_APPLICABLE}
    if any(origin.state not in closable for origin in origins):
        return ""
    if any(origin.state == ORIGIN_APPLIED for origin in origins):
        return APPLIED
    if any(origin.state == ORIGIN_NOT_APPLICABLE for origin in origins):
        return NOT_APPLICABLE
    return DISMISSED


def find_proposal(config: CiaoConfig, proposal_id: str) -> SkillProposal | None:
    """The open proposal with this id, across every registered workspace.

    ``None`` when nothing open has it — a settled record, a deleted file, or an
    id from another install. One walk, so the accept route and the outcome
    writer ask the same question of the same queue and cannot disagree about
    which row an id names.
    """
    for workspace in config.workspace_names():
        for proposal in read_queue(config, workspace):
            if proposal.id == proposal_id:
                return proposal
    return None


# ── Learning settlement ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LearningOriginLink:
    """One proposal's origin for one learning, as the fold sees it."""

    proposal_id: str
    skill: str
    workspace: str
    learning_id: str
    finding: str
    summary: str
    state: str
    source_revision: str
    verification: str

    @property
    def clears(self) -> bool:
        """Whether this link alone is enough to retire its learning."""
        return self.state in CLEARED_ORIGINS

    def to_dict(self) -> dict[str, Any]:
        """The link as the cleanup report carries it."""
        return {
            "proposal_id": self.proposal_id,
            "skill": self.skill,
            "workspace": self.workspace,
            "learning_id": self.learning_id,
            "finding": self.finding,
            "summary": self.summary,
            "state": self.state,
            "source_revision": self.source_revision,
            "verification": self.verification,
            "clears": self.clears,
        }


@dataclass(frozen=True, slots=True)
class LearningSettlement:
    """Whether every finding filed against one learning has been answered.

    ``origins`` is every link that names the learning — across every proposal in
    the workspace, settled ones included, because a learning split across two
    proposals is only answered when both are. ``unlinked`` counts origins on
    those same proposals that name no learning at all: an unattributable finding
    could be this learning's other half, so it is counted and reported rather
    than ignored.

    ``settled`` is true only when there is something to fold, every link clears,
    and nothing on the record is unattributable. A learning no proposal has ever
    linked is not settled, and never was: that is the case a legacy proposal
    leaves behind, and it is why an unlinked proposal is not evidence of
    anything having been dealt with.
    """

    learning_id: str
    workspace: str
    settled: bool
    reason: str
    origins: tuple[LearningOriginLink, ...] = ()
    unlinked: int = 0


def learning_settlement(
    config: CiaoConfig, workspace: str, learning: LearningRecord
) -> LearningSettlement:
    """Fold every origin that names ``learning`` into one answer.

    A learning's identity is its ``learning_id`` plus the ``aliases`` of every
    record merged into it, so a merge does not orphan the links filed against
    the ids it absorbed. The fold reads one workspace's queue and only that
    workspace's: ``learning_id`` is workspace-scoped, so a same-``key`` entry in
    a second workspace is a different learning with a different id, and an
    origin in that second queue cannot answer a question about this one.

    An origin whose own ``workspace`` names somewhere else is not counted as a
    link at all. A queue is one workspace's, so a link pointing out of it is a
    claim this queue cannot make — counting it would let a workspace retire a
    learning by asserting a finding on someone else's.
    """
    wanted = {learning.learning_id, *learning.aliases}
    links: list[LearningOriginLink] = []
    unlinked = 0
    for record in read_records(config, workspace):
        if not any(
            origin.linked and origin.learning_id in wanted
            and origin.workspace in ("", record.workspace)
            for origin in record.origins
        ):
            continue
        for origin in record.origins:
            if not origin.linked:
                unlinked += 1
                continue
            if origin.learning_id not in wanted:
                continue
            if origin.workspace not in ("", record.workspace):
                continue
            links.append(
                LearningOriginLink(
                    proposal_id=record.id,
                    skill=record.skill,
                    workspace=record.workspace,
                    learning_id=origin.learning_id,
                    finding=origin.finding,
                    summary=origin.summary,
                    state=origin.state,
                    source_revision=origin.source_revision,
                    verification=origin.verification,
                )
            )
    settled, reason = _fold_reason(links, unlinked)
    return LearningSettlement(
        learning_id=learning.learning_id,
        workspace=workspace,
        settled=settled,
        reason=reason,
        origins=tuple(links),
        unlinked=unlinked,
    )


def _fold_reason(
    links: Sequence[LearningOriginLink], unlinked: int
) -> tuple[bool, str]:
    """The one answer the fold gives, and the words explaining it.

    The order is the order a person needs it in: something unattributable first,
    because that is a hole in the evidence rather than an outstanding answer;
    then what is still open; then what is answered but waiting on someone.
    """
    if not links:
        return False, "no proposal links a finding to this learning"
    if unlinked:
        return (
            False,
            f"{unlinked} finding(s) on a linked proposal name no learning, so this "
            "one may be incomplete",
        )
    outstanding = [link for link in links if not link.clears]
    if outstanding:
        states = ", ".join(sorted({link.state for link in outstanding}))
        return (
            False,
            f"{len(outstanding)} of {len(links)} finding(s) are not settled ({states})",
        )
    return (
        True,
        f"all {len(links)} finding(s) filed against this learning are applied or "
        "dismissed",
    )


def learnings_revision(config: CiaoConfig, workspace: str) -> str:
    """The revision of ``workspace``'s whole ``Learnings.md`` right now.

    ``content_revision("")`` when the workspace has no learnings document, which
    is the same answer a vault that has never recorded a learning deserves: a
    revision nobody filed an origin against.

    Kept for callers that genuinely want the file — the cleanup pass, which
    revision-checks the write it is about to make. It is **not** what
    :func:`learning_cleanup_eligibility` compares origins against: any unrelated
    edit to the document cancels eligibility for every learning at once, and the
    cleanup write that removes an entry is itself such an edit. That is
    :func:`ciao.learning_records.entry_revision`'s job.
    """
    path = Path(config.workspace_vault_root(workspace)).joinpath(LEARNINGS_RELATIVE)
    try:
        return content_revision(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return content_revision("")


def learning_cleanup_eligibility(
    config: CiaoConfig,
    workspace: str,
    learning: LearningRecord,
    *,
    current_revision: str = "",
) -> dict[str, Any]:
    """Whether one learning may be cleaned up, and why not when it may not.

    The hook, not the run: nothing here removes anything, and there is no
    schedule behind it. A caller reconciling a workspace asks this per learning
    and reports the reason, so "kept" is always an answer rather than a silence.

    Eligible needs all four of these, and each one cancels the whole thing:

    * every finding filed against the learning — across every proposal in the
      workspace, settled records included, and through the learning's
      ``aliases`` — is ``applied`` with a verification on record or ``dismissed``
      by an explicit rejection (:func:`learning_settlement`);
    * no finding on those proposals is unattributable, because it might be this
      learning's other half;
    * the learning's own line still reads the way it did when those findings were
      filed against it, so an entry edited since is re-read before anything is
      done about it. The compare is per entry, not per file
      (:func:`ciao.learning_records.entry_revision`): the caller already holds
      the record it parsed out of the current file, so a neighbour's edit, a new
      lesson appended below it, or the cleanup pass splicing out an unrelated
      line leaves this learning exactly as eligible as it was;
    * nothing was reopened: an origin back in ``pending``, ``implementing``,
      ``interrupted`` or ``failed`` is a question being asked again, and the
      reason says so by name.

    ``current_revision`` lets a caller that has already read the bytes pass the
    revision it saw — :func:`ciao.learnings_cleanup.apply_cleanup` does, so its
    re-check is against the file as it is under the write lock rather than
    against a record it parsed before taking it. Left empty, the record handed in
    is the current one, so its own line revision *is* the current revision and
    nothing has to be read.

    An origin whose recorded revision does not match is **kept**, not migrated.
    That is what makes the move from whole-file to per-entry safe for an install
    that already filed links: the values it holds are whole-file hashes, they
    cannot match any entry, and every such learning is reported as changed-since
    until a person re-files. No backfill is attempted, because rewriting a
    recorded revision to match the present file would assert that the file was
    unchanged since filing when nothing established that.
    """
    settlement = learning_settlement(config, workspace, learning)
    origins = [link.to_dict() for link in settlement.origins]
    report: dict[str, Any] = {
        "learning_id": learning.learning_id,
        "workspace": workspace,
        "eligible": False,
        "reason": settlement.reason,
        "origins": origins,
    }
    if not settlement.settled:
        return report
    revision = current_revision or entry_revision(learning)
    unrecorded = [link for link in settlement.origins if not link.source_revision]
    if unrecorded:
        report["reason"] = (
            f"{len(unrecorded)} finding(s) recorded no revision of the learning "
            "itself, so nothing can show what was filed against is unchanged"
        )
        return report
    stale = [
        link
        for link in settlement.origins
        if link.source_revision != revision
    ]
    if stale:
        report["reason"] = (
            f"this learning changed since {len(stale)} finding(s) were filed "
            f"against it ({', '.join(sorted(link.finding for link in stale)[:3])})"
        )
        return report
    report["eligible"] = True
    return report


def mark_implementing(
    config: CiaoConfig, proposal_id: str, chat_id: str, *, supersedes: str = ""
) -> SkillProposal | None:
    """Bind a chat to an open proposal and flip it to ``implementing``.

    This is what makes acceptance server-owned. The association lives on the
    record rather than in the browser, so a reload, a second device, or the CLI
    all read the same chat instead of each opening their own; the row stays in
    :func:`read_queue` because ``implementing`` is open, so the review surface
    keeps showing the work and a resolution helper cannot archive itself while
    it is in flight.

    Idempotent by chat: a record already bound to ``chat_id`` is returned as it
    stands, and a record bound to a DIFFERENT chat keeps that one. A second
    accept of the same row is not a reason to start a second implementation.

    ``supersedes`` is the one exception, and it is deliberately narrow. It names
    a chat the caller has already established is not live — archived, or gone —
    and this binding replaces exactly that one. Without it a re-accept after the
    chat died would create a fresh chat and then be refused by its own
    idempotency rule, leaving the record pointing at a chat the operator can no
    longer open while the new one is never recorded. A *different* stored chat
    is still left alone: this function has no way to know whether that one is
    live, and a caller that does must say so by name.

    Accepting moves every finding that is still waiting to ``implementing``, and
    nothing else. That is deliberately not a decision: the learning behind each
    of those findings stays Active until the chat reports what it actually did,
    which is what stops "a chat was started" from reading as "the lesson is in
    the skill".

    Raises ``ValueError`` for an empty ``chat_id`` — a proposal with no chat to
    point at is not being implemented by anything, and writing the lifecycle
    anyway would show a row as "in progress" that no one is working on.
    """
    if not chat_id.strip():
        raise ValueError("a skill proposal can only be marked implementing with a chat")
    proposal = find_proposal(config, proposal_id)
    if proposal is None:
        return None

    def _taken(current: str) -> bool:
        """Whether ``current`` blocks this binding.

        True for any chat that is not the one being bound and not the dead one
        the caller has named. Decided per read, because the file under the lock
        may not say what the walk above did.
        """
        return bool(current) and current not in (chat_id, supersedes)

    if _taken(proposal.chat_id):
        # Someone else is already implementing this. Their chat is the live one.
        return proposal
    if proposal.lifecycle == IMPLEMENTING and proposal.chat_id == chat_id:
        return proposal
    path = proposal_path(config, proposal.workspace, proposal.skill)
    with queue_lock(path):
        stored = parse_proposal(path, proposal.workspace) or proposal
        if _taken(stored.chat_id):
            return stored
        implementing = replace(
            stored,
            lifecycle=IMPLEMENTING,
            chat_id=chat_id,
            updated_at=_now(),
            origins=_transition_origins(stored.origins, ORIGIN_IMPLEMENTING),
        )
        write_queue_atomically(path, render_proposal(implementing))
    logger.info("Skill proposal %s is implementing in chat %s", proposal.id, chat_id)
    return implementing


def _transition_origins(
    origins: tuple[SkillOrigin, ...],
    state: str,
    only: Sequence[SkillOrigin] | None = None,
) -> tuple[SkillOrigin, ...]:
    """Every origin that has not been answered, moved to ``state``.

    ``only`` narrows the move to the origins a caller named; ``None`` is the
    whole record. The decided ones are left alone either way, and that is the
    point of the helper: a run that stopped must not reopen a finding somebody
    already rejected, and a chat that is merely starting must not overwrite an
    ``applied`` someone verified.
    """
    moving = {id(item) for item in only} if only is not None else None
    return tuple(
        origin
        if (moving is not None and id(origin) not in moving)
        or origin.state not in OPEN_ORIGINS
        or origin.state == state
        else replace(origin, state=state)
        for origin in origins
    )


def mark_outcome(
    config: CiaoConfig,
    proposal_id: str,
    lifecycle: str,
    reason: str = "",
    *,
    chat_id: str = "",
    via: str = "pwa",
    selectors: Sequence[OriginRef] | None = None,
    verification: str = "",
) -> SkillProposal | None:
    """Record how an implementation actually ended.

    The same settlement :func:`settle_proposal` performs, plus the one rule the
    queue could not express before: a run that did not finish is ``interrupted``,
    never ``applied``. Chat completion is not verification — a stream that ended
    cleanly, an agent that ran out of context and a provider that dropped the
    turn all look identical from here — so ``applied`` is only ever what a
    caller asserts after checking the skill itself, and ``interrupted`` leaves
    the record OPEN and queued so the work is still recoverable.

    ``selectors`` and ``verification`` mean what they mean in
    :func:`settle_proposal`: a selector narrows the outcome to the findings it
    names, and a linked origin can only be ``applied`` with a verification on
    record. ``applied`` is a promotion, everything else a dismissal, so the
    decision sidecar says the same thing it always did and the History tab keeps
    reading one of two outcomes.
    """
    if lifecycle not in OUTCOME_LIFECYCLES:
        raise ValueError(
            f"{lifecycle!r} is not an outcome: expected one of "
            f"{', '.join(sorted(OUTCOME_LIFECYCLES))}"
        )
    proposal = find_proposal(config, proposal_id)
    if proposal is None:
        return None
    if lifecycle == INTERRUPTED:
        return _interrupt(
            config,
            proposal,
            chat_id=chat_id,
            reason=reason,
            via=via,
            selectors=selectors,
        )
    return _settle(
        config,
        proposal,
        lifecycle,
        chat_id=chat_id,
        reason=reason,
        via=via,
        selectors=selectors,
        verification=verification,
    )


def _interrupt(
    config: CiaoConfig,
    proposal: SkillProposal,
    *,
    chat_id: str,
    reason: str,
    via: str,
    selectors: Sequence[OriginRef] | None = None,
) -> SkillProposal:
    """Flip an in-flight record to ``interrupted``, writing no decision.

    Deliberately not a settlement. The sidecar is what stops the next pass from
    re-asking a question, and this one has not been answered — writing a
    dismissal here would archive an unfinished edit as though a person had
    rejected it. So the record keeps its evidence, keeps its chat, goes back in
    the queue, and the operator can accept it again.

    The findings it was working on go with it: an interrupted run leaves the
    learning exactly as Active as it was, because the absence of an answer is
    not an answer. ``selectors`` narrows which of them stopped, on the same
    terms a settlement uses — a ref matching no origin is a bug worth hearing
    about rather than a no-op worth recording.
    """
    if reason:
        logger.info("Skill proposal %s interrupted (%s): %s", proposal.id, via, reason)
    if proposal.lifecycle == INTERRUPTED and not chat_id:
        return proposal
    named = _named_origins(proposal.origins, selectors)
    if selectors and not named:
        raise ValueError(
            f"skill proposal {proposal.id} has no origin for "
            + ", ".join(sorted({ref.learning_id for ref in selectors}))
        )
    stopped = replace(
        proposal,
        lifecycle=INTERRUPTED,
        chat_id=chat_id or proposal.chat_id,
        updated_at=_now(),
        origins=_transition_origins(
            proposal.origins, ORIGIN_INTERRUPTED, named if selectors else None
        ),
    )
    path = proposal_path(config, proposal.workspace, proposal.skill)
    with queue_lock(path):
        write_queue_atomically(path, render_proposal(stopped))
    return stopped


def render_improvement_prompt(proposal: SkillProposal) -> str:
    """The prompt the implementation chat is seeded with.

    Server-side on purpose. The prompt IS the acceptance: it says which skill to
    improve, what the finding was, and what counts as done, so it cannot be a
    per-client string that drifts from what the record actually says. The
    browser used to send its own, and it said *create a skill* — the finding was
    about a skill that already exists, so the chat was asked to build a second
    one under a new name and the queue never got what it asked for.

    Three things this has to carry, and did not before:

    * the canonical ``skills/<name>/SKILL.md`` by name, with the instruction to
      read it first — an edit to a skill nobody re-read is a guess;
    * the proposal id and the reviewed revision, so the chat can tell whether
      the skill still reads the way the reviewer saw it, and so the resolution
      it records names the proposal rather than the skill alone;
    * the resolution itself, through the CLI that owns the settlement. A chat
      that decided something had to be told how to say so, or the queue keeps
      re-asking a question the work already answered.
    """
    skill = proposal.skill
    canonical = proposal.canonical_path or f"skills/{skill}/SKILL.md"
    lines = [
        f"Improve the existing `{skill}` skill in the {proposal.workspace} workspace.",
        f"Work in this chat only; do not delegate this helper task.",
        "",
        f"The skill already exists at `{canonical}`. This is an improvement to "
        f"the skill as it stands, not a new skill: do not create a directory, do "
        f"not write a `SKILL.md` somewhere else, and do not rename it.",
        "",
        f"Proposal {proposal.id} (`{proposal_rel_path(proposal.workspace, skill)}`):",
    ]
    if proposal.title:
        lines += ["", proposal.title]
    for heading, field in _RENDERED_HEADINGS:
        value = getattr(proposal, field)
        if value:
            lines += ["", f"## {heading}", "", value]
    if proposal.sources:
        lines += ["", f"## {_EVIDENCE_HEADING}", "", _render_evidence(proposal.sources)]
    lines += [
        "",
        "Read the current skill first and tell me whether the finding still holds "
        "against what it says now."
        + (
            f" It was reviewed at revision `{proposal.reviewed_revision[:12]}`."
            if proposal.reviewed_revision
            else ""
        ),
        "If it does not, say so and why, and record the finding as no longer "
        "applying rather than editing on the strength of one that has expired:",
        "",
        f"    ciao skill-proposal-remove {skill} --not-applicable",
        "",
        "If it does, make the smallest focused change that addresses the problem "
        "— not a rewrite, and not changes the proposal did not ask for.",
        "",
        "Then verify the change (read the file back, and run whatever the skill "
        "itself tells a reader to run) and record the resolution so the queue "
        "stops asking:",
        "",
        f"    ciao skill-proposal-remove {skill} --applied",
        "",
        f"Use `--applied` only once the change is really in `{canonical}` and you "
        "have verified it. Add `--reason \"...\"` to either command to say in your "
        "own words what you found. If you stop part-way, say that instead:",
        "",
        f"    ciao skill-proposal-remove {skill} --interrupted",
        "",
        "That one is not a decision — it leaves the proposal queued and "
        "recoverable, which is what an unfinished edit should do. Never pass the "
        "proposal text as a shell argument.",
        "",
        "Before you finish, run `ciao sync-skills` so the providers see the "
        "updated skill. Neither command takes a `--workspace` argument: this "
        "chat's environment already names the install and this workspace, "
        "`--workspace` on both is an install-root *path*, and this chat's "
        "working directory is the workspace's own root, not the install root.",
    ]
    return "\n".join(lines)


def _merge(
    config: CiaoConfig, existing: SkillProposal | None, incoming: SkillProposal
) -> SkillProposal:
    """What the queue holds after ``incoming`` is merged into ``existing``."""
    skill = incoming.skill or (existing.skill if existing else "")
    origins = _merge_origins(existing.origins if existing else (), incoming.origins)
    merged = SkillProposal(
        id=proposal_id(incoming.workspace, skill),
        workspace=incoming.workspace,
        skill=skill,
        canonical_path=incoming.canonical_path
        or (existing.canonical_path if existing else ""),
        reviewed_revision=incoming.reviewed_revision
        or (existing.reviewed_revision if existing else ""),
        title=incoming.title or (existing.title if existing else ""),
        problem=incoming.problem or (existing.problem if existing else ""),
        change=incoming.change or (existing.change if existing else ""),
        rationale=incoming.rationale or (existing.rationale if existing else ""),
        sources=_merge_sources(
            existing.sources if existing else (), incoming.sources
        ),
        lifecycle=_merged_lifecycle(config, existing, incoming, origins),
        chat_id=incoming.chat_id or (existing.chat_id if existing else ""),
        updated_at=incoming.updated_at or _now(),
        origins=origins,
    )
    if existing is not None and _same_record(existing, merged):
        # Nothing about the finding changed, so the record's own clock must not
        # either: a re-run over the same trajectories has to be a no-op, not a
        # rewrite that differs only in when it happened.
        merged = replace(merged, updated_at=existing.updated_at)
    return merged


def _merged_lifecycle(
    config: CiaoConfig,
    existing: SkillProposal | None,
    incoming: SkillProposal,
    origins: Sequence[SkillOrigin],
) -> str:
    """The lifecycle the merged record carries.

    **A record that links learnings answers per finding, and the merged origins
    are what it answers from.** The whole-skill decision row cannot say that: one
    settlement of one finding writes the same ``skill:<name>`` row a settlement
    of the record would, so reading it back would settle every sibling with it —
    and the record would leave the queue with a finding nobody had answered still
    on it, stranded where no review can reach it. So a record carrying origins
    takes its lifecycle from those origins and from nothing else: all of them
    answered closes it, and anything outstanding leaves it asking, whether what
    is outstanding is the sibling of a decision just made or a finding this pass
    has only just linked. A new pending origin on a settled record therefore
    reopens it, which is what puts the finding back in front of somebody.

    Without origins the decision wins wherever it is recorded: in the file, or
    only in the sidecar for a record whose file was deleted afterwards. An
    incoming record never reopens a settled one, because nothing in the queue's
    vocabulary can say a decision was wrong — reopening is a change to that
    vocabulary, not a value this writer invents. (A record whose file is gone has
    lost its origins too, so the sidecar is all that is left of its findings, and
    honouring it is the only reading left.)

    Work in flight is protected the same way. A pass that re-derives the same
    finding while a chat is implementing it arrives as ``pending``, and taking
    that would drop the record out of ``implementing``/``interrupted`` under a
    live chat: the review row would stop offering "Open chat" and the queue
    would re-ask a question that is already being answered. A pass files
    findings; only the accept path and the resolution move this record on.
    """
    if origins:
        closed = _closed_lifecycle(origins)
        if closed:
            return closed
        if (
            existing is not None
            and existing.lifecycle in {IMPLEMENTING, INTERRUPTED}
            and incoming.lifecycle == PENDING
        ):
            return existing.lifecycle
        return incoming.lifecycle
    recorded = _recorded_lifecycle(config, incoming.workspace, incoming.skill)
    if recorded:
        return recorded
    if existing is not None and existing.lifecycle in SETTLED_LIFECYCLES:
        return existing.lifecycle
    if (
        existing is not None
        and existing.lifecycle in {IMPLEMENTING, INTERRUPTED}
        and incoming.lifecycle == PENDING
    ):
        return existing.lifecycle
    return incoming.lifecycle


def _recorded_lifecycle(config: CiaoConfig, workspace: str, skill: str) -> str:
    """The lifecycle the decision sidecar holds for ``skill``, or ``""``.

    Read through ``read_decisions`` rather than ``was_dismissed``: that helper
    gives up when the workspace has no ``Memory-Proposals.md`` yet, which is
    exactly the install where a skill proposal is the only thing in the queue and
    the decision would then be invisible to the pass that must honour it.
    """
    wanted = decision_text(skill)
    outcomes = {
        row["action"]
        for row in read_decisions(
            Path(config.workspace_vault_root(workspace)).joinpath(*_DECISIONS_REL)
        )
        if row["text"] == wanted
    }
    if "accepted" in outcomes:
        return APPLIED
    if "dismissed" in outcomes:
        return DISMISSED
    return ""


def _merge_sources(
    stored: tuple[SkillEvidence, ...], incoming: tuple[SkillEvidence, ...]
) -> tuple[SkillEvidence, ...]:
    """Stored evidence plus whatever the incoming record has not been seen with.

    Order is preserved and first-seen wins, so the record reads as the running
    account it is, and the same observation re-derived by a later pass adds
    nothing.
    """
    seen = {item.key for item in stored}
    merged = list(stored)
    for item in incoming:
        if item.key in seen:
            continue
        seen.add(item.key)
        merged.append(item)
    return tuple(merged)


def _merge_origins(
    stored: tuple[SkillOrigin, ...], incoming: tuple[SkillOrigin, ...]
) -> tuple[SkillOrigin, ...]:
    """Stored origins plus whatever the incoming record links that it does not.

    Same rule as the evidence merge, and for the same reason: a re-observation
    of one finding is the same origin, so it adds nothing, and a decision is
    never taken back by a later pass. First-seen wins — except that a stored
    origin whose ``source_revision`` was never recorded has it filled in from the
    incoming one, so a filing that only learned the revision on a later run
    still ends up able to prove which bytes it was written against.

    **No incoming origin brings a state or a verification.** A filing is a
    question: whoever routes a learning into a finding cannot know whether the
    lesson landed or whether a person rejected the finding, and an ``applied``
    that arrived without a receipt — or a ``dismissed`` without anybody having
    said no — would clear a learning nothing had been done about. So every
    origin a merge adds is filed ``pending``, whatever the caller asked for, and
    only a settlement (:func:`_apply_outcome`) writes a decision.
    """
    seen = {item.key for item in stored}
    merged = list(stored)
    for item in incoming:
        filed = replace(item, state=ORIGIN_PENDING, verification="")
        if filed.key not in seen:
            seen.add(filed.key)
            merged.append(filed)
            continue
        position = next(
            index for index, kept in enumerate(merged) if kept.key == filed.key
        )
        kept = merged[position]
        if not kept.source_revision and filed.source_revision:
            merged[position] = replace(kept, source_revision=filed.source_revision)
    return tuple(merged)


def _same_record(left: SkillProposal, right: SkillProposal) -> bool:
    """Whether two records differ in anything but their timestamp."""
    return replace(left, updated_at="") == replace(right, updated_at="")


def _prune_legacy_dated(path: Path, skill: str) -> None:
    """Drop the dated file an older pass wrote for the same skill.

    ``YYYY-MM-DD-<skill>.md`` was the old writer's naming, so a pass that wrote
    one leaves a second file for the same finding — two rows the review queue
    has to group to look like one. Removing exactly that pattern is what the old
    writer did on every run, and the record it is superseded by now holds the
    finding and its evidence. ``missing_ok`` keeps a concurrent pass's prune
    harmless.
    """
    dated = re.compile(rf"^\d{{4}}-\d{{2}}-\d{{2}}-{re.escape(skill)}\.md$")
    for legacy in sorted(path.parent.glob("*.md")):
        if legacy == path or not dated.fullmatch(legacy.name):
            continue
        legacy.unlink(missing_ok=True)
        logger.info("Pruned superseded skill proposal %s", legacy.name)


def _render_evidence(sources: tuple[SkillEvidence, ...]) -> str:
    """Evidence as one bullet per observation, locator first, text last."""
    rows: list[str] = []
    for item in sources:
        locator = f"`{item.chat_id}`" if item.chat_id else ""
        if item.archive:
            locator = f"{locator} ({item.archive})".strip()
        if item.turn:
            locator = f"{locator} turn {item.turn}".strip()
        excerpt = " ".join(item.excerpt.split())
        rows.append(f"- {locator}: {excerpt}" if locator else f"- {excerpt}")
    return "\n".join(rows)


def _render_origins(origins: tuple[SkillOrigin, ...]) -> str:
    """Origins as one compact JSON object per line.

    Compact and key-sorted so the same record always renders the same bytes, and
    one object per line so a single finding can be added or re-settled without
    reformatting its siblings. Blanks are omitted rather than written empty, the
    same rule the learnings metadata comment uses: an absent key and an empty
    one mean the same thing here, and the shorter line is the one a person has to
    read when checking a link.
    """
    return "\n".join(
        json.dumps(item.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for item in origins
    )


def parse_origins(text: str, *, workspace: str) -> tuple[SkillOrigin, ...]:
    """The origin links in one stored payload, keeping any that do not parse.

    Public because the ``skill-proposal-add`` input is the same shape read from a
    different place, and one parser is what stops a link from meaning one thing
    in a queue file and another in a filed finding.

    A line this cannot read becomes an *unlinked* origin: the text is kept as
    the finding, with no ``learning_id``. That is deliberately the same shape a
    hand-written, truncated or foreign-schema line lands in, and deliberately
    not a dropped line — an origin nobody can attribute could be the other half
    of a learning somebody is about to declare dealt with, so
    :func:`learning_cleanup_eligibility` treats it as a reason to keep that
    learning.
    """
    found: list[SkillOrigin] = []
    for line in text.splitlines():
        item = line.strip()
        if not item:
            continue
        try:
            found.append(_origin_from_mapping(json.loads(item), workspace))
        except (ValueError, _OriginError):
            found.append(
                SkillOrigin(workspace=workspace, finding=" ".join(item.split()))
            )
    return tuple(found)


class _OriginError(ValueError):
    """One stored origin that does not satisfy the origin schema."""


def _origin_from_mapping(raw: Any, workspace: str) -> SkillOrigin:
    """One origin out of its stored mapping, or a complaint naming the field.

    Same stance as the learnings metadata reader: every type is checked, and an
    unknown key fails the object rather than being skipped, because a partial
    read is a learning quietly declared settled on the next write.
    """
    if not isinstance(raw, dict):
        raise _OriginError("an origin must be a JSON object")
    unknown = sorted(set(raw) - _ORIGIN_FIELDS)
    if unknown:
        raise _OriginError(f"origin has unknown field(s) {unknown}")
    schema = raw.get("schema")
    if isinstance(schema, bool) or schema != ORIGIN_SCHEMA:
        raise _OriginError(f"origin schema {schema!r} is not {ORIGIN_SCHEMA}")
    text: dict[str, str] = {}
    for name in ("workspace", "learning_id", "source_revision", "finding", "summary", "verification"):
        value = raw.get(name, "")
        if not isinstance(value, str):
            raise _OriginError(f"origin {name} must be a string")
        text[name] = value
    state = raw.get("state", ORIGIN_PENDING)
    if not isinstance(state, str) or state not in ORIGIN_STATES:
        raise _OriginError(f"origin state {state!r} is not one of {ORIGIN_STATES}")
    if not text["finding"]:
        raise _OriginError("origin names no finding")
    return SkillOrigin(
        workspace=text["workspace"] or workspace,
        learning_id=text["learning_id"],
        source_revision=text["source_revision"],
        finding=" ".join(text["finding"].split()),
        summary=" ".join(text["summary"].split()),
        state=state,
        verification=text["verification"],
    )


def _parse_origins(lines: list[str], workspace: str) -> list[SkillOrigin]:
    """The ``## Origins`` section's payload lines, fence markers removed.

    The fence is stripped rather than required: a section whose lines are not
    inside one is still a list of origins, and refusing to read it would drop the
    links a person wrote by hand between the heading and the block.
    """
    payload: list[str] = []
    for line in lines:
        if _FENCE_RE.match(line):
            continue
        payload.append(line)
    return list(parse_origins("\n".join(payload), workspace=workspace))


def _parse_evidence(lines: list[str]) -> list[SkillEvidence]:
    """Evidence bullets back into records, keeping any that do not parse.

    A bullet this renderer did not write is still an observation, so it is kept
    as text with no locator rather than dropped: the record must not lose a
    finding because a line was formatted differently than expected.
    """
    found: list[SkillEvidence] = []
    for line in lines:
        item = line.strip()
        if not item.startswith("- "):
            continue
        match = _EVIDENCE_RE.match(item)
        if match is None:
            found.append(
                SkillEvidence(
                    chat_id="", archive="", turn="", excerpt=item[2:].strip()
                )
            )
            continue
        found.append(
            SkillEvidence(
                chat_id=match.group("chat_id") or "",
                archive=match.group("archive") or "",
                turn=(match.group("turn") or "").strip(),
                excerpt=match.group("excerpt"),
            )
        )
    return found


def _legacy_session_rows(lines: list[str]) -> list[SkillEvidence]:
    """The old file's session table as evidence, one observation per session.

    The first token of each row was the truncated session id, so it is the
    locator here; the whole row is the excerpt, kept verbatim.
    """
    found: list[SkillEvidence] = []
    for line in lines:
        item = line.strip()
        if not item.startswith("- "):
            continue
        text = item[2:].strip()
        chat_id, _, rest = text.partition(" ")
        found.append(
            SkillEvidence(
                chat_id=chat_id,
                archive="",
                turn="",
                excerpt=rest or text,
            )
        )
    return found


def _read_record(path: Path) -> tuple[dict[str, str], str] | None:
    """One queue file's frontmatter and body, or ``None`` when it holds no record.

    The one place a queue file is read, so :func:`parse_proposal` and
    :func:`open_queue_names` cannot disagree about what counts as a record
    there. A file this cannot speak for at all — unreadable, or empty — is
    ``None`` rather than a raise, the same call :func:`parse_proposal` makes.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.strip():
        return None
    return _split_frontmatter(text)


def _front_lifecycle(front: dict[str, str]) -> str:
    """The lifecycle a record's frontmatter claims, or that it is still open.

    A value this schema does not name reads as :data:`PENDING`: the queue's
    job is to put in front of a human anything it cannot read, and an unread
    value is not evidence of a decision.
    """
    lifecycle = str(front.get("lifecycle", "")).strip()
    return lifecycle if lifecycle in LIFECYCLES else PENDING


def _parse_body(body: str, workspace: str = "") -> dict[str, Any]:
    """The body read into the record's text fields, evidence, origins and title.

    ``workspace`` is the queue the file was read from, and it is the *only*
    thing that stands in for a stored origin's own ``workspace``: the links are
    written into one workspace's queue, so a payload that omits the field is a
    link made here and not somewhere else.
    """
    title, sections = _split_sections(body)
    fields: dict[str, Any] = {
        "title": title,
        "problem": "",
        "change": "",
        "rationale": "",
        "sources": [],
        "origins": [],
    }
    unplaced: list[str] = []
    for heading, lines in sections:
        name = heading.casefold()
        if name == _EVIDENCE_HEADING.casefold():
            fields["sources"].extend(_parse_evidence(lines))
            continue
        if name == _ORIGINS_HEADING.casefold():
            # Before the emptiness check: an ``## Origins`` heading with nothing
            # under it is an empty list, and a heading with a fence but no
            # payload in it must not fall through into the rationale as prose.
            fields["origins"].extend(_parse_origins(lines, workspace))
            continue
        if name == _LEGACY_SOURCES_HEADING:
            fields["sources"].extend(_legacy_session_rows(lines))
            continue
        text = "\n".join(lines).strip()
        if not text:
            continue
        field = _LEGACY_HEADINGS.get(name)
        if field is None:
            unplaced.append(text)
            continue
        current = str(fields[field])
        fields[field] = f"{current}\n\n{text}" if current else text
    if unplaced:
        current = str(fields["rationale"])
        fields["rationale"] = "\n\n".join([current, *unplaced] if current else unplaced)
    return fields


def _split_sections(body: str) -> tuple[str, list[tuple[str, list[str]]]]:
    """``(title, [(heading, lines)])`` for one proposal body.

    The first level-1 heading is the title. Its own prose, and any prose before
    it, are yielded under an empty heading so :func:`_parse_body` keeps them
    rather than dropping a legacy file's framing on the first rewrite.
    """
    title = ""
    sections: list[tuple[str, list[str]]] = []
    heading: str | None = None
    lines: list[str] = []
    fence: str | None = None
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            # Toggle on the SAME fence character, which is what closes a block
            # whose body happens to open a differently-marked one.
            marker = line.strip()[:3]
            fence = None if fence == marker else fence or marker
            lines.append(line)
            continue
        match = None if fence else _HEADING_RE.match(line)
        if match is None:
            lines.append(line)
            continue
        if len(match.group(1)) == 1 and not title:
            title = match.group(2)
            if any(row.strip() for row in lines):
                sections.append(("", lines))
            heading, lines = "", []
            continue
        if heading is not None:
            sections.append((heading, lines))
        heading, lines = match.group(2), []
    if heading is not None:
        sections.append((heading, lines))
    elif any(row.strip() for row in lines):
        # A body with no heading at all still has content, and it is the whole
        # finding: dropped here it would be dropped on the next write, which is
        # how a model answer that ignored the prompt's format used to vanish.
        sections.append(("", lines))
    return title, sections


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """``(fields, body)`` for a ``---``-delimited header, or ``({}, text)``.

    Line-based rather than split on ``---``, so a ``---`` inside the body — a
    horizontal rule, a diff hunk — cannot be mistaken for the closing fence. A
    header that never closes is not a header.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fields: dict[str, str] = {}
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            return fields, "\n".join(lines[index + 1 :])
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = _unquote(value.strip())
    return {}, text


def _scalar(value: str) -> str:
    """A frontmatter value, quoted only when a plain scalar would misread."""
    if (
        value
        and value == value.strip()
        and _PLAIN_SCALAR_RE.match(value)
        and value.casefold() not in _YAML_WORDS
    ):
        return value
    return json.dumps(value, ensure_ascii=False)


def _unquote(value: str) -> str:
    if len(value) > 1 and value.startswith('"') and value.endswith('"'):
        try:
            return str(json.loads(value))
        except ValueError:
            return value[1:-1]
    return value


def _check_skill_name(skill: str) -> None:
    """One plain name, refused before it can address a second directory.

    The same rule ``ciao.skills_inventory`` applies to a skill source's
    directory, restated here because this module owns where a *queue file* may
    be written and that is its own boundary: a name carrying a separator would
    put a record outside the queue folder the caller believes it named.
    """
    if "/" in skill or "\\" in skill or os.sep in skill:
        raise ValueError(f"{skill!r} is not one proposal file in the queue")
    if not skill or skill in {".", ".."} or skill.startswith("."):
        raise ValueError(f"{skill!r} is not a skill proposal name")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
