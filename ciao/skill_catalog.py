"""Bounded, read-only skill catalog candidates for one Workspace care run (#3248).

:func:`prepare_skill_candidates` lists the installed skills (``skills_inventory``),
keeps the ones with a reason to look (an archived turn that used them, or an open
or settled proposal or upstream draft), and routes each by ownership: a
workspace-owned source to the skill-proposal queue, a packaged copy to the
upstream-draft queue. A skill is never a candidate because of its age or because
it is installed; absent usage is reported as ``unknown``, never as unused.

Exclusions come before the cap. A skill named by a paused or disabled schedule is
reported as protected, and a source that cannot be read or has changed since its
open proposal is deferred with a fixed reason. Only then does
:data:`MAX_CANDIDATES_PER_RUN` apply, to candidates taken in inventory-id order.

:func:`judge_candidates` hands each selected candidate to the existing read-only
judge (:func:`curation_judgments.judge_prepared_note`) as a :class:`PreparedNote`,
so validation is the one that already exists. Nothing here writes a proposal, a
draft, a skill or a schedule, and nothing calls this module from a schedule yet.

Not implemented: ``supporting_files`` is always ``unknown`` (no inventory signal
reports it), and essential-skill protection has no input in code. A schedule
state reaches the candidate only through the ``schedule_refs`` the caller passes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ciao import curation_run as cr
from ciao import memory_receipts as mr
from ciao import skill_proposals, upstream_drafts
from ciao.curation_evidence import PreparedNote, ProposalReference
from ciao.curation_judgments import JudgmentOutcome, judge_prepared_note
from ciao.skills_inventory import build_skill_inventory, resolve_owned_skill

#: The hard cap on candidates one run may judge.
MAX_CANDIDATES_PER_RUN = 5

STATUS_ELIGIBLE = "eligible"
STATUS_PROTECTED = "protected"
STATUS_DEFERRED = "deferred"

OWNERSHIP_WORKSPACE = "workspace"
OWNERSHIP_PACKAGED = "packaged"
ROUTE_PROPOSAL = "skill_proposal"
ROUTE_DRAFT = "upstream_draft"

TRIGGER_ARCHIVED_USE = "archived_use"
TRIGGER_OPEN_REFERENCE = "open_reference"
TRIGGER_SETTLED_REFERENCE = "settled_reference"

#: Only an archive-recorded marker is usage. Installed presence is not.
BASIS_ARCHIVE_MARKER = "archive_marker"
BASIS_UNKNOWN = "unknown"

PROTECTED_SCHEDULE_STATES = frozenset({"paused", "disabled"})

REASON_UNREADABLE_SOURCE = "the skill source is not a readable owned SKILL.md"
REASON_CHANGED_REVISION = "the skill changed since its open proposal was written"
REASON_PROTECTED_SCHEDULE = (
    "a paused or disabled schedule names this skill; it is reported, not changed"
)


@dataclass(frozen=True, slots=True)
class CatalogCandidate:
    """One installed skill with a reason to look, before or after the cap.

    ``inventory_id`` is ownership-bound (``workspace/<ownership>/<name>``) and is
    the worklist key. ``usage_turns`` are the archive citations, de-duplicated.
    ``references`` are the open and settled proposal and draft records for the
    skill, in the shape the judge already validates against.
    """

    inventory_id: str
    skill: str
    ownership: str
    route: str
    revision: str
    canonical_path: str
    triggers: tuple[str, ...]
    usage_basis: str
    usage_turns: tuple[str, ...]
    schedule_dependency: str
    status: str = STATUS_ELIGIBLE
    reason: str = ""
    references: tuple[ProposalReference, ...] = ()
    supporting_files: str = "unknown"


@dataclass(frozen=True, slots=True)
class CatalogSelection:
    selected: tuple[CatalogCandidate, ...]
    overflow: tuple[CatalogCandidate, ...]
    excluded: tuple[CatalogCandidate, ...]


@dataclass(frozen=True, slots=True)
class SkillJudgment:
    candidate: CatalogCandidate
    outcome: JudgmentOutcome


def select_candidates(entries: Sequence[CatalogCandidate]) -> CatalogSelection:
    """Drop the excluded entries first, then take the first cap in inventory-id order.

    Capping first would let a protected or deferred skill that sorts early take a
    slot an eligible one should have had.
    """
    eligible = sorted(
        (c for c in entries if c.status == STATUS_ELIGIBLE),
        key=lambda c: c.inventory_id,
    )
    excluded = sorted(
        (c for c in entries if c.status != STATUS_ELIGIBLE),
        key=lambda c: c.inventory_id,
    )
    return CatalogSelection(
        selected=tuple(eligible[:MAX_CANDIDATES_PER_RUN]),
        overflow=tuple(eligible[MAX_CANDIDATES_PER_RUN:]),
        excluded=tuple(excluded),
    )


def prepare_skill_candidates(
    config: Any,
    workspace: str,
    *,
    archive_usage: Mapping[str, Mapping[str, Sequence[int]]] | None = None,
    schedule_refs: Mapping[str, str] | None = None,
) -> CatalogSelection:
    """The bounded candidate set for ``workspace``. Read-only.

    ``archive_usage`` maps an archive name to what :func:`transcripts.read_archive_skills`
    returned for it. ``schedule_refs`` maps a skill name to the state of a schedule
    that names it; a name not in it is ``unknown``.
    """
    usage = _usage_by_skill(archive_usage or {})
    states = schedule_refs or {}
    proposals = skill_proposals.read_records(config, workspace)
    drafts = upstream_drafts.read_records(config, workspace)
    inventory = build_skill_inventory(config.agent_root(workspace), include_content=True)

    entries: list[CatalogCandidate] = []
    seen: set[str] = set()
    for skill in inventory["skills"]:
        name = skill["name"]
        if name in seen:
            continue
        seen.add(name)
        candidate = _candidate(
            config,
            workspace,
            skill,
            turns=usage.get(name, ()),
            schedule=states.get(name, "unknown"),
            proposals=proposals,
            drafts=drafts,
        )
        if candidate is not None:
            entries.append(candidate)
    return select_candidates(entries)


def worklist_items(selection: CatalogSelection) -> tuple[cr.WorklistItem, ...]:
    """The one worklist item this slice contributes, or nothing when none is selected."""
    if not selection.selected:
        return ()
    return (
        cr.WorklistItem(
            pass_id=cr.PASS_SKILL_CATALOG,
            label="Assess bounded skill catalog candidates (read-only)",
            reason=f"at most {MAX_CANDIDATES_PER_RUN} candidates per run; no mutation",
            keys=tuple(
                cr.item_key(cr.PASS_SKILL_CATALOG, c.inventory_id)
                for c in selection.selected
            ),
        ),
    )


async def judge_candidates(
    selection: CatalogSelection,
    *,
    run_id: str,
    batch_id: str,
    model: str,
    provider: str,
    timeout_s: float,
    judge: Callable[..., Awaitable[JudgmentOutcome]] = judge_prepared_note,
) -> list[SkillJudgment]:
    """One judgment per selected candidate, through the existing note judge."""
    results: list[SkillJudgment] = []
    for candidate in selection.selected:
        prepared = PreparedNote(
            item_key=cr.item_key(cr.PASS_SKILL_CATALOG, candidate.inventory_id),
            relative_path=candidate.canonical_path,
            revision=candidate.revision,
            references=candidate.references,
        )
        outcome = await judge(
            prepared,
            run_id=run_id,
            batch_id=batch_id,
            model=model,
            provider=provider,
            timeout_s=timeout_s,
        )
        results.append(SkillJudgment(candidate=candidate, outcome=outcome))
    return results


def _usage_by_skill(
    archive_usage: Mapping[str, Mapping[str, Sequence[int]]],
) -> dict[str, tuple[str, ...]]:
    """Citations per skill, de-duplicated across archives and turns, sorted."""
    cited: dict[str, set[str]] = {}
    for archive, skills in archive_usage.items():
        for skill, turns in skills.items():
            for turn in turns:
                cited.setdefault(skill, set()).add(f"{archive}#Turn {turn}")
    return {skill: tuple(sorted(found)) for skill, found in cited.items()}


def _triggers(
    turns: tuple[str, ...], references: tuple[ProposalReference, ...]
) -> tuple[str, ...]:
    found: list[str] = []
    if turns:
        found.append(TRIGGER_ARCHIVED_USE)
    if any(ref.status == "open" for ref in references):
        found.append(TRIGGER_OPEN_REFERENCE)
    if any(ref.status == "settled" for ref in references):
        found.append(TRIGGER_SETTLED_REFERENCE)
    return tuple(found)


def _references(
    skill: str,
    proposals: Sequence[skill_proposals.SkillProposal],
    drafts: Sequence[upstream_drafts.UpstreamDraft],
    current: str,
) -> tuple[ProposalReference, ...]:
    """Open and settled records for ``skill``. ``current`` is the revision read now."""
    refs: list[ProposalReference] = []
    for record in proposals:
        if record.skill != skill:
            continue
        is_open = record.lifecycle not in skill_proposals.SETTLED_LIFECYCLES
        refs.append(
            ProposalReference(
                sidecar_id=record.id,
                proposal_id=record.id if is_open else "",
                status="open" if is_open else "settled",
                revision="current" if record.reviewed_revision == current else "historical",
                operation="skill_proposal",
                expected_revision=record.reviewed_revision,
                settled_at="" if is_open else record.updated_at,
                accepted=record.lifecycle == skill_proposals.APPLIED,
            )
        )
    for draft in drafts:
        if draft.skill != skill:
            continue
        is_open = draft.lifecycle == upstream_drafts.DRAFT_PENDING
        refs.append(
            ProposalReference(
                sidecar_id=draft.id,
                proposal_id=draft.id if is_open else "",
                status="open" if is_open else "settled",
                revision="unknown",
                operation="upstream_draft",
                expected_revision="",
                settled_at="",
                accepted=draft.lifecycle == upstream_drafts.DRAFT_FILED,
            )
        )
    return tuple(refs)


def _source(
    config: Any, workspace: str, skill: Mapping[str, Any]
) -> tuple[str, str, str]:
    """``(revision, canonical path, refusal reason)`` for one inventory entry."""
    name = skill["name"]
    if skill["source_type"] != "custom":
        return mr.content_revision(skill.get("content", "")), skill["path"], ""
    path = f"skills/{name}/SKILL.md"
    try:
        owned = resolve_owned_skill(config, workspace, name)
    except (ValueError, OSError):
        return "", path, REASON_UNREADABLE_SOURCE
    return owned.revision, path, ""


def _candidate(
    config: Any,
    workspace: str,
    skill: Mapping[str, Any],
    *,
    turns: tuple[str, ...],
    schedule: str,
    proposals: Sequence[skill_proposals.SkillProposal],
    drafts: Sequence[upstream_drafts.UpstreamDraft],
) -> CatalogCandidate | None:
    """The candidate for one installed skill, or ``None`` when nothing gives a reason to look."""
    name = skill["name"]
    packaged = skill["source_type"] != "custom"
    ownership = OWNERSHIP_PACKAGED if packaged else OWNERSHIP_WORKSPACE
    revision, canonical_path, source_refusal = _source(config, workspace, skill)
    references = _references(name, proposals, drafts, revision)
    triggers = _triggers(turns, references)
    if not triggers:
        return None

    changed = not packaged and any(
        record.skill == name
        and record.lifecycle not in skill_proposals.SETTLED_LIFECYCLES
        and record.reviewed_revision != revision
        for record in proposals
    )
    if source_refusal:
        status, reason = STATUS_DEFERRED, source_refusal
    elif changed:
        status, reason = STATUS_DEFERRED, REASON_CHANGED_REVISION
    elif schedule in PROTECTED_SCHEDULE_STATES:
        status, reason = STATUS_PROTECTED, REASON_PROTECTED_SCHEDULE
    else:
        status, reason = STATUS_ELIGIBLE, ""
    return CatalogCandidate(
        inventory_id=f"{workspace}/{ownership}/{name}",
        skill=name,
        ownership=ownership,
        route=ROUTE_DRAFT if packaged else ROUTE_PROPOSAL,
        revision=revision,
        canonical_path=canonical_path,
        triggers=triggers,
        usage_basis=BASIS_ARCHIVE_MARKER if turns else BASIS_UNKNOWN,
        usage_turns=turns,
        schedule_dependency=schedule,
        status=status,
        reason=reason,
        references=references,
    )
