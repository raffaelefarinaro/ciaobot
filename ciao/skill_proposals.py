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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ciao.memory_proposals import read_decisions, record_dismissal, record_promotion
from ciao.memory_receipts import queue_lock, write_queue_atomically

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

# Section headings, as rendered. Order is the rendered order.
_RENDERED_HEADINGS: tuple[tuple[str, str], ...] = (
    ("Problem", "problem"),
    ("Proposed change", "change"),
    ("Rationale", "rationale"),
)
_EVIDENCE_HEADING = "Evidence"

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
class SkillProposal:
    """One skill's open or settled improvement proposal.

    ``id`` is derived from ``(workspace, skill)`` by :func:`proposal_id` and
    never stored in the file. ``canonical_path`` and ``reviewed_revision`` name
    the exact bytes the proposal was written against, so a later reader can tell
    that the skill has changed since — the same revision-before-write contract
    :mod:`ciao.skills_inventory` resolves an owned source for. Which sources may
    be edited at all is that module's rule, not this record's.
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
    fields = _parse_body(body)
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
    )


def render_proposal(proposal: SkillProposal) -> str:
    """The record's canonical file text: a pure function of the record.

    Deterministic because every consumer depends on it. The no-op merge in
    :func:`upsert_proposal` compares the rendered bytes with what is on disk,
    and a preview that rendered differently from the stored file would show a
    document the queue does not hold.
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


def read_queue(config: CiaoConfig, workspace: str) -> list[SkillProposal]:
    """Every record in ``workspace`` still awaiting an answer, by skill name.

    A settled record is absent, which is what makes this the pending set: a
    dismissed proposal stays on disk as a record of the decision, and the next
    pass merges new evidence into it without re-asking.

    Sorted by skill so a listing is stable across runs, and one file read per
    record. :func:`enumerate_proposal_ids` is the same walk reduced to ids.
    """
    directory = queue_dir(config, workspace)
    if not directory.is_dir():
        return []
    found: list[SkillProposal] = []
    for path in sorted(directory.glob("*.md")):
        proposal = parse_proposal(path, workspace)
        if proposal is None or proposal.lifecycle in SETTLED_LIFECYCLES:
            continue
        found.append(proposal)
    found.sort(key=lambda item: item.skill)
    return found


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
    * ``updated_at`` moves only when something else did, which is what makes an
      unchanged re-run a true no-op rather than a rewrite that only differs by
      its clock.

    A record whose skill has already been decided stays decided, whether that is
    visible in the file or only in the decision sidecar (a file deleted by the
    CLI still has its decision on record), and a settled record keeps
    accumulating evidence without re-entering the queue.
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

    Raises ``ValueError`` for a lifecycle that is not a decision; a caller that
    invents one would otherwise write a record no reader can place.
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
) -> SkillProposal:
    """Write the decision, then flip the record. Order is the safety property."""
    record = record_promotion if lifecycle == APPLIED else record_dismissal
    record(
        Path(config.workspace_vault_root(proposal.workspace)).joinpath(*_DECISIONS_REL),
        text=decision_text(proposal.skill),
        kind="skill",
        via=via,
        outcome=reason,
        proposal_id=proposal.id,
    )
    path = proposal_path(config, proposal.workspace, proposal.skill)
    with queue_lock(path):
        stored = parse_proposal(path, proposal.workspace) or proposal
        settled = replace(
            stored, lifecycle=lifecycle, chat_id=chat_id or stored.chat_id
        )
        write_queue_atomically(path, render_proposal(settled))
    logger.info("Settled skill proposal %s as %s", proposal.id, lifecycle)
    return settled


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


def mark_implementing(
    config: CiaoConfig, proposal_id: str, chat_id: str
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

    Raises ``ValueError`` for an empty ``chat_id`` — a proposal with no chat to
    point at is not being implemented by anything, and writing the lifecycle
    anyway would show a row as "in progress" that no one is working on.
    """
    if not chat_id.strip():
        raise ValueError("a skill proposal can only be marked implementing with a chat")
    proposal = find_proposal(config, proposal_id)
    if proposal is None:
        return None
    if proposal.chat_id and proposal.chat_id != chat_id:
        # Someone else is already implementing this. Their chat is the live one.
        return proposal
    if proposal.lifecycle == IMPLEMENTING and proposal.chat_id == chat_id:
        return proposal
    path = proposal_path(config, proposal.workspace, proposal.skill)
    with queue_lock(path):
        stored = parse_proposal(path, proposal.workspace) or proposal
        if stored.chat_id and stored.chat_id != chat_id:
            return stored
        implementing = replace(
            stored, lifecycle=IMPLEMENTING, chat_id=chat_id, updated_at=_now()
        )
        write_queue_atomically(path, render_proposal(implementing))
    logger.info("Skill proposal %s is implementing in chat %s", proposal.id, chat_id)
    return implementing


def mark_outcome(
    config: CiaoConfig,
    proposal_id: str,
    lifecycle: str,
    reason: str = "",
    *,
    chat_id: str = "",
    via: str = "pwa",
) -> SkillProposal | None:
    """Record how an implementation actually ended.

    The same settlement :func:`settle_proposal` performs, plus the one rule the
    queue could not express before: a run that did not finish is ``interrupted``,
    never ``applied``. Chat completion is not verification — a stream that ended
    cleanly, an agent that ran out of context and a provider that dropped the
    turn all look identical from here — so ``applied`` is only ever what a
    caller asserts after checking the skill itself, and ``interrupted`` leaves
    the record OPEN and queued so the work is still recoverable.

    ``applied`` is a promotion, everything else a dismissal, so the decision
    sidecar says the same thing it always did and the History tab keeps reading
    one of two outcomes.
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
        return _interrupt(config, proposal, chat_id=chat_id, reason=reason, via=via)
    return _settle(
        config, proposal, lifecycle, chat_id=chat_id, reason=reason, via=via
    )


def _interrupt(
    config: CiaoConfig,
    proposal: SkillProposal,
    *,
    chat_id: str,
    reason: str,
    via: str,
) -> SkillProposal:
    """Flip an in-flight record to ``interrupted``, writing no decision.

    Deliberately not a settlement. The sidecar is what stops the next pass from
    re-asking a question, and this one has not been answered — writing a
    dismissal here would archive an unfinished edit as though a person had
    rejected it. So the record keeps its evidence, keeps its chat, goes back in
    the queue, and the operator can accept it again.
    """
    if reason:
        logger.info("Skill proposal %s interrupted (%s): %s", proposal.id, via, reason)
    if proposal.lifecycle == INTERRUPTED and not chat_id:
        return proposal
    stopped = replace(
        proposal,
        lifecycle=INTERRUPTED,
        chat_id=chat_id or proposal.chat_id,
        updated_at=_now(),
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
        "If it does not, say so and why, and settle the proposal as not applicable "
        "rather than editing on the strength of a finding that has expired. If it "
        "does, make the smallest focused change that addresses the problem — not a "
        "rewrite, and not changes the proposal did not ask for.",
        "",
        "Then verify the change (read the file back, and run whatever the skill "
        "itself tells a reader to run), run `ciao sync-skills --workspace "
        f"{proposal.workspace}` so the providers see the updated skill, and record "
        "the resolution so the queue stops asking:",
        "",
        f"    ciao skill-proposal-remove {skill} --workspace . --applied",
        "",
        f"Use `--applied` only once the change is really in `{canonical}` and you "
        "have verified it. If you stop part-way, record `--interrupted` with a "
        "reason instead: that leaves the proposal queued and recoverable, which "
        "is what an unfinished edit should do. Never pass the proposal text as a "
        "shell argument.",
    ]
    return "\n".join(lines)


def _merge(
    config: CiaoConfig, existing: SkillProposal | None, incoming: SkillProposal
) -> SkillProposal:
    """What the queue holds after ``incoming`` is merged into ``existing``."""
    skill = incoming.skill or (existing.skill if existing else "")
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
        lifecycle=_merged_lifecycle(config, existing, incoming),
        chat_id=incoming.chat_id or (existing.chat_id if existing else ""),
        updated_at=incoming.updated_at or _now(),
    )
    if existing is not None and _same_record(existing, merged):
        # Nothing about the finding changed, so the record's own clock must not
        # either: a re-run over the same trajectories has to be a no-op, not a
        # rewrite that differs only in when it happened.
        merged = replace(merged, updated_at=existing.updated_at)
    return merged


def _merged_lifecycle(
    config: CiaoConfig, existing: SkillProposal | None, incoming: SkillProposal
) -> str:
    """The lifecycle the merged record carries.

    A decision wins, wherever it is recorded: in the file, or only in the sidecar
    for a record whose file was deleted afterwards. An incoming record never
    reopens a settled one, because nothing in the queue's vocabulary can say a
    decision was wrong — reopening is a change to that vocabulary, not a value
    this writer invents.

    Work in flight is protected the same way. A pass that re-derives the same
    finding while a chat is implementing it arrives as ``pending``, and taking
    that would drop the record out of ``implementing``/``interrupted`` under a
    live chat: the review row would stop offering "Open chat" and the queue
    would re-ask a question that is already being answered. A pass files
    findings; only the accept path and the resolution move this record on.
    """
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


def _parse_body(body: str) -> dict[str, Any]:
    """The body read into the record's text fields, evidence, and title."""
    title, sections = _split_sections(body)
    fields: dict[str, Any] = {
        "title": title,
        "problem": "",
        "change": "",
        "rationale": "",
        "sources": [],
    }
    unplaced: list[str] = []
    for heading, lines in sections:
        name = heading.casefold()
        if name == _EVIDENCE_HEADING.casefold():
            fields["sources"].extend(_parse_evidence(lines))
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
