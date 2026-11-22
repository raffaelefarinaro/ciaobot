"""Bounded, read-only skill catalog candidates for a Workspace care run (#3248).

Every fixture is a throwaway install under ``tmp_path``. Nothing here may read
or write the developer's own install, so each config builds its own registry.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ciao import curation_evidence as ce
from ciao import curation_run as cr
from ciao import skill_catalog as sc
from ciao import skill_proposals, upstream_drafts
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.curation_judgments import NothingToJudge
from ciao.skill_proposals import SkillProposal
from ciao.skills_inventory import STOCK_SKILL_MARKER, resolve_owned_skill
from ciao.workspace_reroot import mark_born_per_root


def _config(tmp_path: Path, *names: str) -> CiaoConfig:
    runtime = tmp_path / ".runtime"
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={name: WorkspaceConfig(name=name, vault_root=name) for name in names},
    )
    mark_born_per_root(tmp_path, runtime, list(names))
    return config


def _write_skill(config: CiaoConfig, workspace: str, name: str, body: str | None = None) -> Path:
    root = config.agent_root(workspace) / "skills" / name
    root.mkdir(parents=True, exist_ok=True)
    skill_md = root / "SKILL.md"
    text = body if body is not None else f"---\nname: {name}\ndescription: Owned {name}\n---\n\n# {name}\n"
    skill_md.write_text(text, encoding="utf-8", newline="")
    return skill_md


def _revision(config: CiaoConfig, name: str) -> str:
    return resolve_owned_skill(config, "personal", name).revision


def _proposal(
    config: CiaoConfig,
    workspace: str,
    skill: str,
    *,
    lifecycle: str,
    reviewed_revision: str,
    updated_at: str = "2019-01-01T00:00:00+00:00",
) -> SkillProposal:
    record = SkillProposal(
        id=skill_proposals.proposal_id(workspace, skill),
        workspace=workspace,
        skill=skill,
        canonical_path=f"skills/{skill}/SKILL.md",
        reviewed_revision=reviewed_revision,
        title=f"Improve {skill}",
        problem="The procedure skips the retry step.",
        change="Add the retry step.",
        rationale="A transcript shows the retry was needed.",
        sources=(),
        lifecycle=lifecycle,
        chat_id="",
        updated_at=updated_at,
    )
    return skill_proposals.upsert_proposal(config, record)


def _tree(*roots: Path) -> list[tuple[str, int, bytes]]:
    entries: list[tuple[str, int, bytes]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            data = path.read_bytes() if path.is_file() else b""
            entries.append((str(path), path.lstat().st_mtime_ns, data))
    return sorted(entries)


def _candidate(
    name: str,
    *,
    status: str = sc.STATUS_ELIGIBLE,
    reason: str = "",
) -> sc.CatalogCandidate:
    return sc.CatalogCandidate(
        inventory_id=f"personal/{sc.OWNERSHIP_WORKSPACE}/{name}",
        skill=name,
        ownership=sc.OWNERSHIP_WORKSPACE,
        route=sc.ROUTE_PROPOSAL,
        revision="rev",
        canonical_path=f"skills/{name}/SKILL.md",
        triggers=(sc.TRIGGER_ARCHIVED_USE,),
        usage_basis=sc.BASIS_ARCHIVE_MARKER,
        usage_turns=(),
        schedule_dependency="unknown",
        status=status,
        reason=reason,
    )


def test_cap_applies_after_exclusions(tmp_path: Path) -> None:
    # Two protected skills sort first. Capping before excluding would spend
    # two of the five slots on them and select fewer eligible candidates.
    entries = [
        _candidate("a-paused", status=sc.STATUS_PROTECTED, reason="paused"),
        _candidate("b-disabled", status=sc.STATUS_PROTECTED, reason="disabled"),
    ] + [_candidate(f"skill-{i:02d}") for i in range(7)]
    selection = sc.select_candidates(entries)
    assert [c.skill for c in selection.selected] == [
        "skill-00",
        "skill-01",
        "skill-02",
        "skill-03",
        "skill-04",
    ]
    assert [c.skill for c in selection.overflow] == ["skill-05", "skill-06"]
    assert {c.skill for c in selection.excluded} == {"a-paused", "b-disabled"}


def test_selection_is_stable_regardless_of_input_order(tmp_path: Path) -> None:
    names = [f"skill-{i:02d}" for i in range(7)]
    forward = sc.select_candidates([_candidate(n) for n in names])
    backward = sc.select_candidates([_candidate(n) for n in reversed(names)])
    assert forward.selected == backward.selected


def test_usage_citations_collapse_duplicate_archive_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path, "personal")
    _write_skill(config, "personal", "demo")
    usage = {
        "chat-1.md": {"demo": (2, 2)},
        "chat-1-copy.md": {"demo": (2,)},
    }
    selection = sc.prepare_skill_candidates(config, "personal", archive_usage=usage)
    [candidate] = selection.selected
    assert candidate.usage_basis == sc.BASIS_ARCHIVE_MARKER
    assert candidate.usage_turns == ("chat-1-copy.md#Turn 2", "chat-1.md#Turn 2")


def test_unknown_usage_and_age_alone_never_produce_a_candidate(tmp_path: Path) -> None:
    config = _config(tmp_path, "personal")
    _write_skill(config, "personal", "idle")
    _write_skill(config, "personal", "old")
    _proposal(
        config,
        "personal",
        "old",
        lifecycle=skill_proposals.APPLIED,
        reviewed_revision=_revision(config, "old"),
        updated_at="2015-01-01T00:00:00+00:00",
    )
    selection = sc.prepare_skill_candidates(config, "personal", archive_usage={})
    names = {c.skill for c in selection.selected}
    # No archive evidence and no proposal: the skill is not a candidate at all.
    assert "idle" not in names
    # A settled proposal from years ago is a trigger to look, and its usage
    # basis stays unknown. It is never reported as stale.
    [old] = [c for c in selection.selected if c.skill == "old"]
    assert old.usage_basis == sc.BASIS_UNKNOWN
    assert old.triggers == (sc.TRIGGER_SETTLED_REFERENCE,)


def test_malformed_source_defers_with_exact_reason(tmp_path: Path) -> None:
    config = _config(tmp_path, "personal")
    _write_skill(config, "personal", "broken", body="")
    usage = {"chat-1.md": {"broken": (1,)}}
    selection = sc.prepare_skill_candidates(config, "personal", archive_usage=usage)
    assert not selection.selected
    [deferred] = selection.excluded
    assert deferred.skill == "broken"
    assert deferred.status == sc.STATUS_DEFERRED
    assert deferred.reason == sc.REASON_UNREADABLE_SOURCE


def test_changed_revision_defers_with_exact_reason(tmp_path: Path) -> None:
    config = _config(tmp_path, "personal")
    _write_skill(config, "personal", "drift")
    _proposal(
        config,
        "personal",
        "drift",
        lifecycle=skill_proposals.PENDING,
        reviewed_revision="not-the-current-revision",
    )
    selection = sc.prepare_skill_candidates(config, "personal", archive_usage={})
    assert not selection.selected
    [deferred] = selection.excluded
    assert deferred.status == sc.STATUS_DEFERRED
    assert deferred.reason == sc.REASON_CHANGED_REVISION


def test_paused_schedule_dependency_is_protected_not_changed(tmp_path: Path) -> None:
    config = _config(tmp_path, "personal")
    _write_skill(config, "personal", "nightly")
    usage = {"chat-1.md": {"nightly": (3,)}}
    selection = sc.prepare_skill_candidates(
        config,
        "personal",
        archive_usage=usage,
        schedule_refs={"nightly": "paused"},
    )
    assert not selection.selected
    [protected] = selection.excluded
    assert protected.status == sc.STATUS_PROTECTED
    assert protected.schedule_dependency == "paused"
    assert protected.reason == sc.REASON_PROTECTED_SCHEDULE


def test_external_skill_routes_to_upstream_draft(tmp_path: Path) -> None:
    config = _config(tmp_path, "personal")
    installed = config.agent_root("personal") / ".claude" / "skills" / "packaged-demo"
    installed.mkdir(parents=True)
    (installed / STOCK_SKILL_MARKER).write_text("", encoding="utf-8")
    (installed / "SKILL.md").write_text(
        "---\nname: packaged-demo\ndescription: Packaged\n---\n\n# packaged-demo\n",
        encoding="utf-8",
    )
    usage = {"chat-1.md": {"packaged-demo": (4,)}}
    selection = sc.prepare_skill_candidates(config, "personal", archive_usage=usage)
    [candidate] = selection.selected
    assert candidate.ownership == sc.OWNERSHIP_PACKAGED
    assert candidate.route == sc.ROUTE_DRAFT


def test_open_and_settled_references_are_cited(tmp_path: Path) -> None:
    config = _config(tmp_path, "personal")
    _write_skill(config, "personal", "cited")
    open_record = _proposal(
        config,
        "personal",
        "cited",
        lifecycle=skill_proposals.PENDING,
        reviewed_revision=_revision(config, "cited"),
    )
    settled_draft = upstream_drafts.file_draft(
        config,
        "personal",
        target=upstream_drafts.UPSTREAM_ISSUE,
        skill="cited",
        title="Document the retry",
        change="State that a timed-out fetch is retried once.",
        body="A timed-out fetch is reported as empty. Retry once first.",
        repository="example/tools",
        version="1.0.0",
        private_evidence="chat-9 turn 2",
    )
    upstream_drafts.reject_draft(config, settled_draft.id, reason="not worth filing")

    selection = sc.prepare_skill_candidates(config, "personal", archive_usage={})
    [candidate] = selection.selected
    ids = {ref.sidecar_id for ref in candidate.references}
    assert open_record.id in ids
    assert settled_draft.id in ids
    statuses = {ref.sidecar_id: ref.status for ref in candidate.references}
    assert statuses[open_record.id] == "open"
    assert statuses[settled_draft.id] == "settled"


def test_judgment_seam_gets_exactly_the_selected_set_and_writes_nothing(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "personal")
    names = [f"skill-{i:02d}" for i in range(7)]
    for name in names:
        _write_skill(config, "personal", name)
    _write_skill(config, "personal", "zz-paused")
    usage = {"chat-1.md": {name: (1,) for name in names + ["zz-paused"]}}
    vault = config.workspace_vault_root("personal")
    before = _tree(vault, config.agent_root("personal"))

    selection = sc.prepare_skill_candidates(
        config,
        "personal",
        archive_usage=usage,
        schedule_refs={"zz-paused": "disabled"},
    )
    handed: list[ce.PreparedNote] = []

    async def fake_judge(prepared: Any, **_: Any) -> NothingToJudge:
        handed.append(prepared)
        return NothingToJudge(reason="test seam")

    results = asyncio.run(
        sc.judge_candidates(
            selection,
            run_id="run",
            batch_id="batch",
            model="model",
            provider="provider",
            timeout_s=10.0,
            judge=fake_judge,
        )
    )

    expected = {cr.item_key(cr.PASS_SKILL_CATALOG, f"personal/workspace/{n}") for n in names[:5]}
    assert {p.item_key for p in handed} == expected
    assert len(results) == len(handed) == 5
    assert _tree(vault, config.agent_root("personal")) == before


def test_worklist_item_is_last_and_does_not_starve_hygiene(tmp_path: Path) -> None:
    assert cr.PASS_ORDER[-1] == cr.PASS_SKILL_CATALOG
    config = _config(tmp_path, "personal")
    for i in range(6):
        _write_skill(config, "personal", f"skill-{i}")
    usage = {"chat-1.md": {f"skill-{i}": (1,) for i in range(6)}}
    selection = sc.prepare_skill_candidates(config, "personal", archive_usage=usage)
    catalog = sc.worklist_items(selection)

    hygiene = cr.WorklistItem(
        pass_id=cr.PASS_HYGIENE,
        label="hygiene",
        reason="the weekly pass is due",
        keys=tuple(sorted(cr.REQUIRED_HYGIENE_KEYS)),
        weekly=True,
    )
    # build_worklist_views hands plan_run its items in PASS_ORDER, so do the same.
    ordered = sorted((*catalog, hygiene), key=lambda i: cr.PASS_ORDER.index(i.pass_id))
    worklist = cr.Worklist(
        items=tuple(ordered),
        weekly_due=True,
        last_full_pass="",
        generated_at="2026-11-22T00:00:00+00:00",
    )
    plan = cr.plan_run(
        worklist,
        cr.RunBudget(max_items=len(cr.REQUIRED_HYGIENE_KEYS), max_seconds=60.0),
    )
    planned_keys = {key for item in plan.planned for key in item.keys}
    assert cr.REQUIRED_HYGIENE_KEYS <= planned_keys
    assert all(item.pass_id != cr.PASS_SKILL_CATALOG for item in plan.planned)

