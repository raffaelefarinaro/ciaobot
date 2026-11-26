"""Tests for ``ciao.os_audit`` AI OS Audit Suite."""

from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path

import pytest

from ciao import vault_lint

def _seed_guide(guide: Path, *, memory: list[str] | None = None, profile: list[str] | None = None) -> Path:
    from ciao.memory_tool import ensure_regions, write_region
    guide.parent.mkdir(parents=True, exist_ok=True)
    if not guide.exists():
        guide.write_text("# Guide\n\n", encoding="utf-8")
    ensure_regions(guide)
    if memory is not None:
        write_region(guide, "memory", memory)
    if profile is not None:
        write_region(guide, "profile", profile)
    return guide

from ciao.os_audit import (
    audit_job_runs,
    audit_memory,
    audit_rules,
    audit_skills,
    format_audit_markdown,
    run_os_audit,
)
from ciao.skills_inventory import MAX_SKILL_BYTES


@pytest.fixture(autouse=True)
def _no_real_xdg_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """User-global skill scans resolve ~/.config under the isolated home."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def test_audit_skills_over_budget(tmp_path: Path) -> None:
    exact_dir = tmp_path / "skills" / "exact-skill"
    exact_dir.mkdir(parents=True)
    (exact_dir / "SKILL.md").write_bytes(b"A" * MAX_SKILL_BYTES)

    over_dir = tmp_path / "skills" / "big-skill"
    over_dir.mkdir(parents=True)
    (over_dir / "SKILL.md").write_bytes(b"A" * (MAX_SKILL_BYTES + 1))

    res = audit_skills(tmp_path)
    assert res["total_skills"] == 2
    assert res["over_budget_count"] == 1
    assert res["issues"][0]["type"] == "skill_over_budget"
    # The audit quotes the budget it enforces, and the budget has one home:
    # `ciao.skills_inventory`. A second copy here could report a threshold the
    # import gate no longer warns about.
    assert res["issues"][0]["max_bytes"] == MAX_SKILL_BYTES


def test_audit_skills_deduplicates_provider_projections(tmp_path: Path) -> None:
    canonical = tmp_path / "skills" / "demo"
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text("# Demo\n", encoding="utf-8")

    claude = tmp_path / ".claude" / "skills"
    claude.mkdir(parents=True)
    claude_demo = claude / "demo"
    claude_demo.mkdir()
    (claude_demo / "SKILL.md").write_text("# Stale projection\n", encoding="utf-8")

    agents = tmp_path / ".agents" / "skills"
    agents.mkdir(parents=True)
    (agents / "demo").symlink_to(canonical, target_is_directory=True)
    agents_only = agents / "agents-only"
    agents_only.mkdir()
    (agents_only / "SKILL.md").write_text("# Shared agents skill\n", encoding="utf-8")

    res = audit_skills(tmp_path)
    assert res["total_skills"] == 2
    assert res["issues"] == []


def test_audit_skills_warns_about_over_budget_user_global_skills(tmp_path: Path) -> None:
    """OpenCode loads ~/.claude/skills and ~/.agents/skills in full too."""
    workspace = tmp_path / "ws"
    (workspace / "skills" / "shared").mkdir(parents=True)
    (workspace / "skills" / "shared" / "SKILL.md").write_text("# small\n", encoding="utf-8")
    home = Path.home()  # tests/conftest.py points it at tmp_path / "home"
    big_claude = home / ".claude" / "skills" / "big-claude"
    big_claude.mkdir(parents=True)
    (big_claude / "SKILL.md").write_bytes(b"A" * (MAX_SKILL_BYTES + 1))
    big_agents = home / ".agents" / "skills" / "big-agents"
    big_agents.mkdir(parents=True)
    (big_agents / "SKILL.md").write_bytes(b"A" * (MAX_SKILL_BYTES + 1))
    small = home / ".agents" / "skills" / "small"
    small.mkdir(parents=True)
    (small / "SKILL.md").write_text("# fine\n", encoding="utf-8")
    # A workspace skill shadows a global one of the same name.
    shadowed = home / ".claude" / "skills" / "shared"
    shadowed.mkdir(parents=True)
    (shadowed / "SKILL.md").write_bytes(b"A" * (MAX_SKILL_BYTES + 1))

    res = audit_skills(workspace)

    assert res["issues"] == [] and res["errors"] == []
    assert res["over_budget_count"] == 0
    assert res["user_over_budget_count"] == 2
    assert sorted(w["skill"] for w in res["warnings"]) == ["big-agents", "big-claude"]
    assert {w["type"] for w in res["warnings"]} == {"user_skill_over_budget"}
    report = run_os_audit(workspace_dir=workspace, vault_root=workspace / "memory-vault")
    assert report["skill_audit"]["user_over_budget_count"] == 2
    assert "User-global skills over 15 KiB" in format_audit_markdown(report)
    # Warnings only: they hold no defect and no scan error.
    shutil.rmtree(home / ".claude")
    shutil.rmtree(home / ".agents")
    clean = run_os_audit(workspace_dir=workspace, vault_root=workspace / "memory-vault")
    assert (clean["defect_count"], clean["total_errors"]) == (
        report["defect_count"],
        report["total_errors"],
    )


def test_audit_skills_warns_about_opencode_global_skills(tmp_path: Path) -> None:
    """OpenCode's own ~/.config/opencode/skills is loaded like the others."""
    big = Path.home() / ".config" / "opencode" / "skills" / "big-opencode"
    big.mkdir(parents=True)
    (big / "SKILL.md").write_bytes(b"A" * (MAX_SKILL_BYTES + 1))

    res = audit_skills(tmp_path / "ws")

    assert [w["skill"] for w in res["warnings"]] == ["big-opencode"]


def test_audit_skills_does_not_scan_a_workspace_root_twice_as_user_global(
    tmp_path: Path,
) -> None:
    """A workspace at the home folder is not reported twice."""
    home = Path.home()
    big = home / ".claude" / "skills" / "big"
    big.mkdir(parents=True)
    (big / "SKILL.md").write_bytes(b"A" * (MAX_SKILL_BYTES + 1))

    res = audit_skills(home)

    assert res["over_budget_count"] == 1
    assert res["warnings"] == []


def test_audit_rules_deduplicates_linked_workspace_guides(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "- Keep code changes focused and covered by automated unit tests.\n",
        encoding="utf-8",
    )

    res = audit_rules(tmp_path)
    assert res["rule_clashes_found"] == 0
    assert res["rule_overlaps_found"] == 0
    assert res["errors"] == []


def test_audit_rules_separates_overlaps_from_conflicts(tmp_path: Path) -> None:
    """Overlap and clash detection, between the guide and a vault MEMORY.md.

    This used to compare CLAUDE.md against AGENTS.md. There is one guide now
    (ciao/workspace_guide.py), so two guides can no longer disagree — which
    was the point of removing the pair. The remaining sources still can.
    """
    def _guide(body: str, remembered: str) -> None:
        (tmp_path / "AGENTS.md").write_text(
            f"{body}\n\n"
            "<!-- ciao:memory:start cap=3000 -->\n"
            f"{remembered}\n"
            "<!-- ciao:memory:end -->\n",
            encoding="utf-8",
        )

    same = "- Keep code changes focused and covered by automated unit tests."
    _guide(same, same)

    overlap = audit_rules(tmp_path)
    assert overlap["rule_overlaps_found"] == 1
    assert overlap["rule_clashes_found"] == 0

    _guide("- Always use rtk for shell commands.", "- Never use rtk for shell commands.")

    conflict = audit_rules(tmp_path)
    assert conflict["rule_overlaps_found"] == 0
    assert conflict["rule_clashes_found"] == 1
    assert conflict["clashes"][0]["signature"] == "use rtk for shell commands"


def test_audit_memory_hygiene(tmp_path: Path) -> None:
    guide = _seed_guide(
        tmp_path / "AGENTS.md",
        memory=["durable lesson", "old task [expires: 2020-01-01]"],
        profile=["bad date [expires: someday]"],
    )
    res = audit_memory(guide_path=guide, today=datetime.date(2026, 7, 26))
    assert res["memory_entries"] == 2
    assert res["expired_memory_entries"] == 1
    assert res["invalid_expiration_entries"] == 1


def test_audit_memory_reports_content_rot(tmp_path: Path) -> None:
    (tmp_path / "memory-vault").mkdir()
    guide = _seed_guide(
        tmp_path / "AGENTS.md",
        memory=[
            'User said: "do it" -> assistant bumped the default.',
            "Notes live in `memory-vault/absent.md` now.",
        ],
        profile=["The user writes without em-dashes."],
    )

    res = audit_memory(guide_path=guide, workspace_dir=tmp_path)

    assert len(res["event_shaped_entries"]) == 1
    assert len(res["stale_path_entries"]) == 1
    assert res["paths_checked"] == 1
    assert res["superseded_state_candidates"] == []


def test_run_os_audit_counts_rot_but_not_superseded_candidates(tmp_path: Path) -> None:
    """Superseded-state is a judgement call, so it must not gate the status.

    Counting it would leave the audit permanently at needs_attention for a user
    who has looked at the pair and decided to keep both entries.
    """
    (tmp_path / "memory-vault").mkdir()
    _seed_guide(
        tmp_path / "AGENTS.md",
        memory=[
            "Set `ollama_haiku_model` to the old slug.",
            "Set `ollama_haiku_model` to the new slug.",
        ],
        profile=[],
    )

    report = run_os_audit(workspace_dir=tmp_path, vault_root=tmp_path / "memory-vault")
    memory = report["memory_hygiene"]

    assert len(memory["superseded_state_candidates"]) == 1
    assert memory["event_shaped_entries"] == []
    assert memory["stale_path_entries"] == []

    baseline = run_os_audit(
        workspace_dir=tmp_path, vault_root=tmp_path / "memory-vault"
    )
    _seed_guide(
        tmp_path / "AGENTS.md",
        memory=['User said: "go" -> assistant changed it.'],
        profile=[],
    )
    with_event = run_os_audit(
        workspace_dir=tmp_path, vault_root=tmp_path / "memory-vault"
    )

    assert with_event["total_issues"] == baseline["total_issues"] + 1


def test_memory_actionable_count_covers_the_mechanical_findings(tmp_path: Path) -> None:
    """`ciao memory-audit` and `ciao os-audit` must agree on "clean".

    The CLI used to sum its own subset of the report and omitted
    `oversize_entries`, `invisible_unicode` and the pending Unsure items, so
    it exited 0 on a region that os-audit failed. Both now call this function,
    and this test fails if a new key is counted in one place only.
    """
    from ciao.os_audit import memory_actionable_count

    # A zero-width space is invisible Unicode: os-audit has always failed on it.
    (tmp_path / "memory-vault").mkdir()
    _seed_guide(
        tmp_path / "AGENTS.md",
        memory=["Prefers concise​ answers."],
        profile=[],
    )

    report = run_os_audit(workspace_dir=tmp_path, vault_root=tmp_path / "memory-vault")
    memory = report["memory_hygiene"]

    assert len(memory["invisible_unicode"]) == 1
    assert memory_actionable_count(memory) >= 1
    # The whole-audit status agrees, which is the contract the CLI relies on.
    assert report["total_issues"] >= 1


def test_run_os_audit_reports_stale_notes_as_informational(tmp_path: Path) -> None:
    """Aging notes are evidence for the curation routine, not audit defects.

    They render in memory_hygiene so the weekly report can name them, but they
    must not gate the status or the actionable count — a vault nobody has
    touched in months is not an install that needs emergency attention, it is
    a queue for the next curation pass.
    """
    import os as _os
    import time as _time

    vault = tmp_path / "memory-vault"
    (vault / "People").mkdir(parents=True)
    (vault / "People" / "Mo.md").write_text(
        "---\ntype: person\n---\n# Mo\n", encoding="utf-8"
    )
    old = _time.time() - 200 * 86400
    _os.utime(vault / "People" / "Mo.md", (old, old))
    _seed_guide(tmp_path / "AGENTS.md", memory=["durable lesson"], profile=[])

    report = run_os_audit(workspace_dir=tmp_path, vault_root=vault)
    memory = report["memory_hygiene"]

    stale = memory["stale_notes"]
    assert len(stale) == 1
    assert stale[0]["path"] == "memory-vault/People/Mo.md"
    assert stale[0]["threshold_days"] == 30
    assert memory["notes_checked"] >= 1

    from ciao.os_audit import format_audit_markdown, memory_actionable_count

    # Informational by contract: nothing here is actionable on its own.
    findings = memory_actionable_count(memory)
    assert findings == 0
    assert "Notes not verified within the review horizon" in (
        format_audit_markdown(report)
    )


def test_format_audit_markdown_renders_rot_findings(tmp_path: Path) -> None:
    (tmp_path / "memory-vault").mkdir()
    _seed_guide(
        tmp_path / "AGENTS.md",
        memory=['User said: "go" -> assistant changed `timeout_s`.'],
        profile=[],
    )

    report = run_os_audit(workspace_dir=tmp_path, vault_root=tmp_path / "memory-vault")
    markdown = format_audit_markdown(report)

    assert "Event-shaped entries (belong in a log): 1" in markdown
    assert "reads as a chat event" in markdown
    assert "paths checked" in markdown


def test_format_audit_markdown_over_cap_names_the_fix(tmp_path: Path) -> None:
    """An over-cap line must arrive with the actions that shrink the region.

    The rendered audit has to carry the fix instead of assuming the reader
    knows the consolidation ritual.
    """
    (tmp_path / "memory-vault").mkdir()
    _seed_guide(
        tmp_path / "AGENTS.md",
        memory=[
            f"Durable lesson {index}: " + "x" * 500 for index in range(7)
        ],
        profile=["Raffa prefers plain, factual notes."],
    )

    report = run_os_audit(workspace_dir=tmp_path, vault_root=tmp_path / "memory-vault")
    markdown = format_audit_markdown(report)

    over_cap = report["memory_hygiene"]["over_cap"]
    assert [finding["region"] for finding in over_cap] == ["memory"]
    assert "Regions over cap: 1" in markdown
    assert "ciao:memory over cap: " in markdown
    assert "consolidate the region" in markdown
    assert "CIAO_MEMORY_CHAR_LIMIT" not in markdown


def test_audit_memory_reports_unclosed_expiration_tag(tmp_path: Path) -> None:
    guide = _seed_guide(
        tmp_path / "AGENTS.md",
        memory=["temporary note [expires: 2026-07-26"],
    )
    res = audit_memory(guide_path=guide, today=datetime.date(2026, 7, 26))

    assert res["invalid_expiration_entries"] == 1
    assert "closing ']'" in res["invalid_expirations"][0]["message"]


def test_audit_memory_rejects_noncanonical_and_multiple_expiration_tags(
    tmp_path: Path,
) -> None:
    guide = _seed_guide(
        tmp_path / "AGENTS.md",
        memory=[
            "compact date [expires: 20260720]",
            "ambiguous [expires: 2026-07-20] [expires: someday]",
        ],
    )
    res = audit_memory(guide_path=guide, today=datetime.date(2026, 7, 26))

    assert res["expired_memory_entries"] == 0
    assert res["invalid_expiration_entries"] == 2


def test_audit_memory_counts_only_unticked_unsure_items_in_each_workspace(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "memory-vault"
    paths = [
        vault / "Workspace" / "Unsure.md",
        vault / "personal" / "Workspace" / "Unsure.md",
        vault / "work" / "Workspace" / "Unsure.md",
    ]
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Unsure\n\n"
            f"- [ ] pending {index} — 2026-07-26\n"
            "- [x] a ticked item the agent has not written yet — 2026-07-26\n"
            "- A plain line that is not an item.\n",
            encoding="utf-8",
        )

    res = audit_memory(
        vault_root=vault,
        today=datetime.date(2026, 7, 26),
    )
    assert res["pending_unsure_items"] == 3
    assert len(res["unsure_files"]) == 3
    assert {item["pending"] for item in res["unsure_files"]} == {1}


def test_audit_memory_uses_explicit_external_unsure_paths(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    external = tmp_path / "external-workspace" / "Workspace" / "Unsure.md"
    external.parent.mkdir(parents=True)
    external.write_text("- [ ] pending preference\n", encoding="utf-8")

    res = audit_memory(
        vault_root=vault,
        proposal_paths=[external],
        today=datetime.date(2026, 7, 26),
    )

    assert res["pending_unsure_items"] == 1
    assert res["unsure_files"] == [{"path": str(external), "pending": 1}]


def test_audit_memory_surfaces_unsure_discovery_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    original_iterdir = Path.iterdir

    def failing_iterdir(path: Path):
        if path == vault:
            raise OSError("cannot list vault")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", failing_iterdir)
    res = audit_memory(vault_root=vault)

    assert res["pending_unsure_items"] == 0
    assert res["errors"][0]["type"] == "unreadable_unsure_root"


def test_audit_job_runs_uses_latest_status_and_real_timestamps(tmp_path: Path) -> None:
    runtime_dir = tmp_path / ".runtime"
    runtime_dir.mkdir(parents=True)
    job_file = runtime_dir / "job_runs.jsonl"
    job_file.write_text(
        "\n".join(
            [
                json.dumps({
                    "job": "recovered",
                    "status": "error",
                    "error": "old failure",
                    "started_at": "2026-07-26T10:00:00+00:00",
                    "ended_at": "2026-07-26T10:01:00+00:00",
                }),
                json.dumps({
                    "job": "recovered",
                    "status": "ok",
                    "error": None,
                    "started_at": "2026-07-26T11:00:00+00:00",
                    "ended_at": "2026-07-26T11:01:00+00:00",
                }),
                json.dumps({
                    "job": "still_broken",
                    "status": "error",
                    "error": "timeout",
                    "started_at": "2026-07-26T12:00:00+00:00",
                    "ended_at": "2026-07-26T12:01:00+00:00",
                }),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (runtime_dir / "job_runs_latest.json").write_text(
        json.dumps({
            "latest_only": {
                "job": "latest_only",
                "status": "error",
                "error": "token expired",
                "started_at": "2026-07-26T13:00:00+00:00",
                "ended_at": "2026-07-26T13:01:00+00:00",
            }
        }),
        encoding="utf-8",
    )

    res = audit_job_runs(tmp_path, runtime_dir=runtime_dir)
    assert res["total_runs"] == 3
    assert res["latest_jobs"] == 3
    assert res["failed_runs"] == 2
    assert [item["job"] for item in res["recent_failures"]] == [
        "latest_only",
        "still_broken",
    ]
    assert res["recent_failures"][0]["ts"] == "2026-07-26T13:01:00+00:00"
    assert res["errors"] == []


def test_audit_job_runs_chooses_latest_history_record_by_timestamp(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / ".runtime"
    runtime_dir.mkdir()
    (runtime_dir / "job_runs.jsonl").write_text(
        "\n".join(
            [
                json.dumps({
                    "job": "out_of_order",
                    "status": "ok",
                    "started_at": "2026-07-26T12:00:00+00:00",
                    "ended_at": "2026-07-26T12:01:00+00:00",
                }),
                json.dumps({
                    "job": "out_of_order",
                    "status": "error",
                    "error": "older failure",
                    "started_at": "2026-07-26T10:00:00+00:00",
                    "ended_at": "2026-07-26T10:01:00+00:00",
                }),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    res = audit_job_runs(tmp_path, runtime_dir=runtime_dir)

    assert res["failed_runs"] == 0
    assert res["errors"] == []


def test_audit_job_runs_reports_malformed_timestamps(tmp_path: Path) -> None:
    runtime_dir = tmp_path / ".runtime"
    runtime_dir.mkdir()
    (runtime_dir / "job_runs.jsonl").write_text(
        json.dumps({
            "job": "bad_timestamp",
            "status": "error",
            "error": "boom",
            "started_at": "yesterday",
            "ended_at": "later",
        })
        + "\n",
        encoding="utf-8",
    )

    res = audit_job_runs(tmp_path, runtime_dir=runtime_dir)

    assert res["invalid_records"] == 1
    assert res["failed_runs"] == 0
    assert res["errors"][0]["type"] == "invalid_job_run_records"


def test_run_os_audit_reports_unreadable_vault_markdown(tmp_path: Path) -> None:
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)
    unreadable = vault / "personal" / "Ideas" / "binary.md"
    unreadable.parent.mkdir(parents=True)
    unreadable.write_bytes(b"\xff\xfe")

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
    )

    assert report["status"] == "error"
    assert any(
        error["type"] == "unreadable_vault_file"
        and error["path"] == str(unreadable)
        for error in report["scan_errors"]
    )


def test_audit_job_runs_surfaces_malformed_records(tmp_path: Path) -> None:
    runtime_dir = tmp_path / ".runtime"
    runtime_dir.mkdir()
    (runtime_dir / "job_runs.jsonl").write_text(
        "not-json\nnull\n",
        encoding="utf-8",
    )
    (runtime_dir / "job_runs_latest.json").write_text(
        "{broken",
        encoding="utf-8",
    )

    res = audit_job_runs(tmp_path, runtime_dir=runtime_dir)
    assert res["invalid_records"] == 2
    assert len(res["errors"]) == 2


def test_audit_job_runs_rejects_unknown_and_contradictory_statuses(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / ".runtime"
    runtime_dir.mkdir()
    (runtime_dir / "job_runs.jsonl").write_text(
        json.dumps({
            "job": "unknown_status",
            "status": "failed",
            "error": "boom",
            "ended_at": "2026-07-26T10:00:00+00:00",
        })
        + "\n",
        encoding="utf-8",
    )
    (runtime_dir / "job_runs_latest.json").write_text(
        json.dumps({
            "contradictory": {
                "job": "contradictory",
                "status": "ok",
                "error": "boom",
                "ended_at": "2026-07-26T11:00:00+00:00",
            }
        }),
        encoding="utf-8",
    )

    res = audit_job_runs(tmp_path, runtime_dir=runtime_dir)

    assert res["latest_jobs"] == 0
    assert res["failed_runs"] == 0
    assert res["invalid_records"] == 1
    assert len(res["errors"]) == 2


def _healthy_roots(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("- Use rtk for shell commands.\n", encoding="utf-8")
    _seed_guide(workspace / "AGENTS.md")
    vault = workspace / "memory-vault"
    vault.mkdir()
    runtime = workspace / ".runtime"
    runtime.mkdir()
    bounded = tmp_path / "bounded"
    bounded.mkdir()
    return workspace, vault, runtime, bounded


def test_run_os_audit_missing_roots_is_error(tmp_path: Path) -> None:
    workspace = tmp_path / "missing"
    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=workspace / "memory-vault",
        runtime_dir=workspace / ".runtime",
    )
    assert report["status"] == "error"
    assert report["total_errors"] == 3
    # Two region marker diagnostics (memory + profile) when AGENTS.md is absent.
    assert report["total_issues"] == 5
    assert {
        (item["type"], item["path"]) for item in report["scan_errors"]
    } == {
        ("missing_workspace_root", str(workspace)),
        ("missing_vault_root", str(workspace / "memory-vault")),
        ("missing_runtime_root", str(workspace / ".runtime")),
    }


def test_run_os_audit_counts_every_actionable_finding(tmp_path: Path) -> None:
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)

    (workspace / "skills" / "missing-md").mkdir(parents=True)
    over_budget = workspace / "skills" / "over-budget"
    over_budget.mkdir()
    (over_budget / "SKILL.md").write_bytes(b"x" * (MAX_SKILL_BYTES + 1))
    # The clash is between the guide body and what was remembered into its
    # memory region (seeded below). It used to be between CLAUDE.md and
    # AGENTS.md; there is one guide now, so those are the two sources left
    # that can disagree.
    (workspace / "AGENTS.md").write_text(
        "- Always use rtk for shell commands.\n\n", encoding="utf-8"
    )
    ideas = vault / "personal" / "Ideas"
    ideas.mkdir(parents=True)
    (ideas / "same.md").write_text(
        "---\ntype: idea\n---\n# One\n\n[gone](./missing-target.md)\n",
        encoding="utf-8",
    )
    # The duplicate pair shares a directory: a repeated stem across folders is
    # how a vault holds one report/log/slides per project, so the linter only
    # calls two notes duplicates when they sit in the same place under two
    # spellings of one name.
    (ideas / "S-a-m-e.md").write_text(
        "---\ntype: idea\n---\n# Two\n",
        encoding="utf-8",
    )
    proposals = vault / "personal" / "Workspace" / "Unsure.md"
    proposals.parent.mkdir(parents=True)
    proposals.write_text("# Unsure\n\n- [ ] pending fact — 2026-07-26\n", encoding="utf-8")
    _seed_guide(
        workspace / "AGENTS.md",
        memory=[
            "old task [expires: 2020-01-01]",
            "Never use rtk for shell commands.",
        ],
        profile=["bad expiry [expires: someday]"],
    )
    (runtime / "job_runs_latest.json").write_text(
        json.dumps({
            "broken_job": {
                "job": "broken_job",
                "status": "error",
                "error": "boom",
                "started_at": "2026-07-26T10:00:00+00:00",
                "ended_at": "2026-07-26T10:01:00+00:00",
            }
        }),
        encoding="utf-8",
    )

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
        today=datetime.date(2026, 7, 26),
    )
    assert report["status"] == "needs_attention"
    # Vault: 1 broken markdown link + 2 orphans + 1 duplicate.
    # Skills: 1 missing SKILL.md + 1 over budget.
    # Rules: 1 conflict. Memory: 1 expired + 1 invalid tag (an Unsure item is the person's own list and is not counted).
    # Jobs: 1 unresolved latest failure.
    assert report["total_issues"] == 10
    assert report["total_errors"] == 0
    md_summary = format_audit_markdown(report)
    assert "# AI OS Audit Report" in md_summary
    assert "NEEDS_ATTENTION" in md_summary


def test_os_audit_counts_and_formats_new_vault_findings(tmp_path: Path) -> None:
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)
    page = vault / "personal" / "Page.md"
    page.parent.mkdir(parents=True)
    page.write_text("[missing](missing.md)\n", encoding="utf-8")

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
    )

    assert len(report["vault_hygiene"]["frontmatter_errors"]) == 1
    assert len(report["vault_hygiene"]["broken_markdown_links"]) == 1
    assert report["total_issues"] == 2
    assert report["status"] == "needs_attention"
    markdown = format_audit_markdown(report)
    assert "Frontmatter errors: 1" in markdown
    assert "Broken Markdown links: 1" in markdown


def test_os_audit_counts_frontmatter_findings_without_markdown_findings(
    tmp_path: Path,
) -> None:
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)
    page = vault / "Page.md"
    page.write_text("# Missing metadata\n", encoding="utf-8")

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
    )

    assert len(report["vault_hygiene"]["frontmatter_errors"]) == 1
    assert len(report["vault_hygiene"]["broken_markdown_links"]) == 0
    assert report["total_issues"] == 1
    markdown = format_audit_markdown(report)
    assert "Frontmatter errors: 1" in markdown
    assert "Broken Markdown links: 0" in markdown


def test_os_audit_counts_markdown_findings_without_frontmatter_findings(
    tmp_path: Path,
) -> None:
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)
    page = vault / "Page.md"
    page.write_text(
        "---\ntype: note\n---\n[missing](missing.md)\n",
        encoding="utf-8",
    )

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
    )

    assert len(report["vault_hygiene"]["frontmatter_errors"]) == 0
    assert len(report["vault_hygiene"]["broken_markdown_links"]) == 1
    assert report["total_issues"] == 1
    markdown = format_audit_markdown(report)
    assert "Frontmatter errors: 0" in markdown
    assert "Broken Markdown links: 1" in markdown


def test_os_audit_source_discovery_matches_vault_lint_exclusions_and_suffixes(
    tmp_path: Path,
) -> None:
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)
    included = vault / "included" / "Unreadable.MD"
    included.parent.mkdir(parents=True)
    included.write_bytes(b"\xff\xfe")
    for directory in ("Logs", "Templates"):
        excluded = vault / directory / "Unreadable.Md"
        excluded.parent.mkdir(parents=True)
        excluded.write_bytes(b"\xff\xfe")

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
    )

    unreadable_paths = {
        error["path"]
        for error in report["scan_errors"]
        if error["type"] == "unreadable_vault_file"
    }
    assert str(included) in unreadable_paths
    assert str(vault / "Logs" / "Unreadable.Md") not in unreadable_paths
    assert str(vault / "Templates" / "Unreadable.Md") not in unreadable_paths


def test_os_audit_reports_vault_traversal_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)

    def fail_walk(*args: object, **kwargs: object):
        raise OSError("cannot inspect vault")

    monkeypatch.setattr(vault_lint.os, "walk", fail_walk)

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
    )

    assert report["status"] == "error"
    assert any(
        error["type"] == "vault_validation_failed"
        and "cannot inspect vault" in error["message"]
        for error in report["scan_errors"]
    )


def test_run_os_audit_preserves_distinct_errors_for_the_same_file(
    tmp_path: Path,
) -> None:
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)
    (runtime / "job_runs_latest.json").write_text(
        json.dumps({
            "one": {
                "job": "different",
                "status": "ok",
                "ended_at": "2026-07-26T10:00:00+00:00",
            },
            "two": {
                "job": "two",
                "status": "unknown",
                "ended_at": "2026-07-26T11:00:00+00:00",
            },
        }),
        encoding="utf-8",
    )

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
    )

    assert report["status"] == "error"
    assert report["total_errors"] == 2
    assert len(report["scan_errors"]) == 2


def test_the_report_carries_no_upgrade_notices(tmp_path: Path) -> None:
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)
    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
    )
    assert report["status"] == "healthy"
    assert report["defect_count"] == 0
    for key in ("upgrade_notices", "pending_action_count", "has_pending_actions"):
        assert key not in report
    assert "Upgrade Actions" not in format_audit_markdown(report)


def test_a_search_index_defect_is_rendered_not_just_counted(tmp_path: Path) -> None:
    """A defect that changes `status` has to be legible in the report.

    `search_index` findings were added to `defect_count` when they landed but
    never rendered, so an install whose only defect was the search index printed
    "Total Issues: 1" above a report with every section empty — and `--repair`,
    which is the fix, went unmentioned. The inline comment beside the count
    asserted the opposite.
    """
    report = run_os_audit(
        workspace_dir=tmp_path, vault_root=tmp_path / "memory-vault"
    )
    report["search_index"] = {
        "missing": True,
        "stale_rows": ["personal/memory-vault/People/Mo.md"],
        "transcripts_unindexed": 3,
        "errors": [],
    }

    text = format_audit_markdown(report)

    assert "## 7. Search Index" in text
    assert "--repair" in text
    assert "personal/memory-vault/People/Mo.md" in text
    assert "3 transcript archive(s) unindexed" in text


def test_a_clean_search_index_renders_no_section(tmp_path: Path) -> None:
    """Silence is the healthy answer; an empty section reads as a finding."""
    report = run_os_audit(
        workspace_dir=tmp_path, vault_root=tmp_path / "memory-vault"
    )
    report["search_index"] = {
        "missing": False,
        "stale_rows": [],
        "transcripts_unindexed": 0,
        "errors": [],
    }

    assert "## 7. Search Index" not in format_audit_markdown(report)


def test_os_audit_skips_ciaoignored_vault_files(tmp_path: Path) -> None:
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)
    (vault / "automations" / "x").mkdir(parents=True)
    (vault / "automations" / "x" / "report.md").write_text("# Report\n", encoding="utf-8")
    unreadable = vault / "automations" / "x" / "bad.md"
    unreadable.write_bytes(b"\xff\xfe")
    (vault / ".ciaoignore").write_text("automations/\n", encoding="utf-8")

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
    )

    assert report["vault_hygiene"]["frontmatter_errors"] == []
    assert not any(
        error["type"] == "unreadable_vault_file" and error["path"] == str(unreadable)
        for error in report["scan_errors"]
    )
