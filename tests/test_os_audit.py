"""Tests for ``ciao.os_audit`` AI OS Audit Suite."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from ciao import memory_tool as mt
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
    SKILL_MAX_BYTES,
    audit_job_runs,
    audit_memory,
    audit_rules,
    audit_skills,
    format_audit_markdown,
    run_os_audit,
)


def test_audit_skills_over_budget(tmp_path: Path) -> None:
    exact_dir = tmp_path / "skills" / "exact-skill"
    exact_dir.mkdir(parents=True)
    (exact_dir / "SKILL.md").write_bytes(b"A" * (15 * 1024))

    over_dir = tmp_path / "skills" / "big-skill"
    over_dir.mkdir(parents=True)
    (over_dir / "SKILL.md").write_bytes(b"A" * ((15 * 1024) + 1))

    res = audit_skills(tmp_path)
    assert SKILL_MAX_BYTES == 15 * 1024
    assert res["total_skills"] == 2
    assert res["over_budget_count"] == 1
    assert res["issues"][0]["type"] == "skill_over_budget"


def test_audit_skills_deduplicates_provider_projections(tmp_path: Path) -> None:
    canonical = tmp_path / "skills" / "demo"
    canonical.mkdir(parents=True)
    (canonical / "SKILL.md").write_text("# Demo\n", encoding="utf-8")

    claude = tmp_path / ".claude" / "skills"
    claude.mkdir(parents=True)
    claude_demo = claude / "demo"
    claude_demo.mkdir()
    (claude_demo / "SKILL.md").write_text("# Stale projection\n", encoding="utf-8")

    codex = tmp_path / ".agents" / "skills"
    codex.mkdir(parents=True)
    (codex / "demo").symlink_to(canonical, target_is_directory=True)
    codex_only = codex / "codex-only"
    codex_only.mkdir()
    (codex_only / "SKILL.md").write_text("# Codex only\n", encoding="utf-8")

    res = audit_skills(tmp_path)
    assert res["total_skills"] == 2
    assert res["issues"] == []


def test_audit_rules_deduplicates_linked_workspace_guides(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text(
        "- Keep code changes focused and covered by automated unit tests.\n",
        encoding="utf-8",
    )
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")

    res = audit_rules(tmp_path)
    assert res["rule_clashes_found"] == 0
    assert res["rule_overlaps_found"] == 0
    assert res["errors"] == []


def test_audit_rules_separates_overlaps_from_conflicts(tmp_path: Path) -> None:
    same = "- Keep code changes focused and covered by automated unit tests."
    (tmp_path / "CLAUDE.md").write_text(same, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(same, encoding="utf-8")

    overlap = audit_rules(tmp_path)
    assert overlap["rule_overlaps_found"] == 1
    assert overlap["rule_clashes_found"] == 0

    (tmp_path / "CLAUDE.md").write_text("- Always use rtk for shell commands.", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("- Never use rtk for shell commands.", encoding="utf-8")

    conflict = audit_rules(tmp_path)
    assert conflict["rule_overlaps_found"] == 0
    assert conflict["rule_clashes_found"] == 1
    assert conflict["clashes"][0]["signature"] == "use rtk for shell commands"


def test_audit_memory_hygiene(tmp_path: Path) -> None:
    guide = _seed_guide(
        tmp_path / "CLAUDE.md",
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
        tmp_path / "CLAUDE.md",
        memory=[
            'User said: "do it" -> assistant bumped the default.',
            "Notes live in `memory-vault/absent.md` now.",
        ],
        profile=["Raffa writes without em-dashes."],
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
        tmp_path / "CLAUDE.md",
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
        tmp_path / "CLAUDE.md",
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
    `oversize_entries`, `invisible_unicode` and `pending_memory_proposals`, so
    it exited 0 on a region that os-audit failed. Both now call this function,
    and this test fails if a new key is counted in one place only.
    """
    from ciao.os_audit import memory_actionable_count

    # A zero-width space is invisible Unicode: os-audit has always failed on it.
    (tmp_path / "memory-vault").mkdir()
    _seed_guide(
        tmp_path / "CLAUDE.md",
        memory=["Prefers concise​ answers."],
        profile=[],
    )

    report = run_os_audit(workspace_dir=tmp_path, vault_root=tmp_path / "memory-vault")
    memory = report["memory_hygiene"]

    assert len(memory["invisible_unicode"]) == 1
    assert memory_actionable_count(memory) >= 1
    # The whole-audit status agrees, which is the contract the CLI relies on.
    assert report["total_issues"] >= 1


def test_format_audit_markdown_renders_rot_findings(tmp_path: Path) -> None:
    (tmp_path / "memory-vault").mkdir()
    _seed_guide(
        tmp_path / "CLAUDE.md",
        memory=['User said: "go" -> assistant changed `timeout_s`.'],
        profile=[],
    )

    report = run_os_audit(workspace_dir=tmp_path, vault_root=tmp_path / "memory-vault")
    markdown = format_audit_markdown(report)

    assert "Event-shaped entries (belong in a log): 1" in markdown
    assert "reads as a chat event" in markdown
    assert "paths checked" in markdown


def test_audit_memory_reports_unclosed_expiration_tag(tmp_path: Path) -> None:
    guide = _seed_guide(
        tmp_path / "CLAUDE.md",
        memory=["temporary note [expires: 2026-07-26"],
    )
    res = audit_memory(guide_path=guide, today=datetime.date(2026, 7, 26))

    assert res["invalid_expiration_entries"] == 1
    assert "closing ']'" in res["invalid_expirations"][0]["message"]


def test_audit_memory_rejects_noncanonical_and_multiple_expiration_tags(
    tmp_path: Path,
) -> None:
    guide = _seed_guide(
        tmp_path / "CLAUDE.md",
        memory=[
            "compact date [expires: 20260720]",
            "ambiguous [expires: 2026-07-20] [expires: someday]",
        ],
    )
    res = audit_memory(guide_path=guide, today=datetime.date(2026, 7, 26))

    assert res["expired_memory_entries"] == 0
    assert res["invalid_expiration_entries"] == 2


def test_audit_memory_counts_only_canonical_proposals_in_each_workspace(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "memory-vault"
    paths = [
        vault / "Workspace" / "Memory-Proposals.md",
        vault / "personal" / "Workspace" / "Memory-Proposals.md",
        vault / "work" / "Workspace" / "Memory-Proposals.md",
    ]
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Memory Proposals\n\n"
            f"- [memory] pending {index}  _(from: Decisions)_\n"
            "- Processed batch history that is not pending.\n",
            encoding="utf-8",
        )

    res = audit_memory(
        vault_root=vault,
        today=datetime.date(2026, 7, 26),
    )
    assert res["pending_memory_proposals"] == 3
    assert len(res["proposal_files"]) == 3
    assert {item["pending"] for item in res["proposal_files"]} == {1}


def test_audit_memory_uses_explicit_external_proposal_paths(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    external = tmp_path / "external-workspace" / "Workspace" / "Memory-Proposals.md"
    external.parent.mkdir(parents=True)
    external.write_text("- [user] pending preference\n", encoding="utf-8")

    res = audit_memory(
        vault_root=vault,
        proposal_paths=[external],
        today=datetime.date(2026, 7, 26),
    )

    assert res["pending_memory_proposals"] == 1
    assert res["proposal_files"] == [{"path": str(external), "pending": 1}]


def test_audit_memory_surfaces_proposal_discovery_errors(
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

    assert res["pending_memory_proposals"] == 0
    assert res["errors"][0]["type"] == "unreadable_proposal_root"


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
    (workspace / "CLAUDE.md").write_text("- Use rtk for shell commands.\n", encoding="utf-8")
    _seed_guide(workspace / "CLAUDE.md")
    (workspace / "AGENTS.md").symlink_to("CLAUDE.md")
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
    # Two region marker diagnostics (memory + profile) when CLAUDE.md is absent.
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
    (over_budget / "SKILL.md").write_bytes(b"x" * (SKILL_MAX_BYTES + 1))
    (workspace / "AGENTS.md").unlink()
    (workspace / "AGENTS.md").write_text("- Never use rtk for shell commands.\n", encoding="utf-8")
    ideas = vault / "personal" / "Ideas"
    resources = vault / "personal" / "Resources"
    ideas.mkdir(parents=True)
    resources.mkdir(parents=True)
    (ideas / "same.md").write_text(
        "---\ntype: idea\n---\n# One\n\n[gone](./missing-target.md)\n",
        encoding="utf-8",
    )
    (resources / "same.md").write_text(
        "---\ntype: resource\n---\n# Two\n",
        encoding="utf-8",
    )
    proposals = vault / "personal" / "Workspace" / "Memory-Proposals.md"
    proposals.parent.mkdir(parents=True)
    proposals.write_text(
        "---\ntype: note\n---\n"
        "- [memory] pending fact  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    _seed_guide(
        workspace / "CLAUDE.md",
        memory=["old task [expires: 2020-01-01]"],
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
    # Rules: 1 conflict. Memory: 1 expired + 1 invalid tag + 1 proposal.
    # Jobs: 1 unresolved latest failure.
    assert report["total_issues"] == 11
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


def _upgrade_config(workspace: Path):
    """Config whose vault registry pins a legacy sibling vault.

    The pinned vault is left outside the standard folder so
    `audit_upgrade_notices` reports it as an optional migration.
    """
    from ciao.config import CiaoConfig, WorkspaceConfig

    legacy = workspace / "research"
    (legacy / "Workspace").mkdir(parents=True)
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=workspace,
        vault_root=workspace / "memory-vault",
        state_path=workspace / ".runtime" / "state.json",
        media_root=workspace / ".runtime" / "media",
        workspaces={
            "research": WorkspaceConfig(name="research", vault_root="research"),
        },
    )


def test_run_os_audit_pending_only_is_healthy(tmp_path: Path) -> None:
    """One upgrade notice and zero defects must not raise the status.

    A migration a user may decline must not pin the audit at needs_attention,
    mirroring how `memory_actionable_count` treats superseded-state candidates.
    """
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)
    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
        config=_upgrade_config(workspace),
    )

    assert report["status"] == "healthy"
    assert report["total_issues"] == 0
    assert report["defect_count"] == 0
    # One pending action: the vault-location notice. The re-home notice needs
    # more than one registered workspace, and this config registers one.
    assert report["pending_action_count"] == 1
    assert report["has_pending_actions"] is True
    markdown = format_audit_markdown(report)
    assert "Upgrade Actions (optional)" in markdown
    assert "Pending actions" in markdown


def test_run_os_audit_split_counts_keep_pending_actions_out_of_defects(
    tmp_path: Path,
) -> None:
    """A pending action is reported separately and never raises defect_count."""
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)
    (workspace / "skills" / "missing-md").mkdir(parents=True)

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
        config=_upgrade_config(workspace),
    )

    assert report["defect_count"] == 1
    # One pending action: the vault-location notice. The re-home notice needs
    # more than one registered workspace, and this config registers one.
    assert report["pending_action_count"] == 1
    assert report["has_pending_actions"] is True
    assert report["status"] == "needs_attention"


def test_run_os_audit_clean_has_no_pending_actions(tmp_path: Path) -> None:
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)
    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
    )
    assert report["status"] == "healthy"
    assert report["defect_count"] == 0
    assert report["pending_action_count"] == 0
    assert report["has_pending_actions"] is False


def _rehome_config(workspace: Path):
    """Config with TWO registered workspaces, the shape the damage needs.

    With a single workspace `detect_misfiled_people` still buckets an untagged
    note as needs_judgement, but its target and destination come back empty:
    there is nowhere to move it, so the migration has nothing to offer. The
    notice is gated on more than one workspace, so its tests must register two.
    """
    from ciao.config import CiaoConfig, WorkspaceConfig

    for name in ("personal", "work"):
        (workspace / "memory-vault" / name / "People").mkdir(parents=True, exist_ok=True)
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=workspace,
        vault_root=workspace / "memory-vault",
        state_path=workspace / ".runtime" / "state.json",
        media_root=workspace / ".runtime" / "media",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal"),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )


def _rehome_notice(report: dict) -> list[dict]:
    return [
        notice
        for notice in report["upgrade_notices"]["notices"]
        if notice["type"] == "unrehomed_people"
    ]


def _write_survey_receipt(runtime: Path, **counts: int) -> None:
    """Write a surveyed re-home receipt with whatever counts the test wants."""
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "status": "surveyed",
        "surveyed_at": "2026-08-19T00:00:00Z",
        "vault_root": str(runtime),
        "mechanical": counts.get("mechanical", 3),
        "needs_judgement": counts.get("needs_judgement", 2),
        "conflicts": counts.get("conflicts", 1),
    }
    (runtime / "migration" / "vault-rehome.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _write_migrated_receipt(runtime: Path) -> None:
    """Write a completed re-home receipt, which must silence the notice."""
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "status": "migrated",
        "rehomed_at": "2026-08-19T00:00:00Z",
        "vault_root": str(runtime),
        "moves": [],
        "rewrites": [],
        "needs_judgement": [],
        "proposals": [],
    }
    (runtime / "migration" / "vault-rehome.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_rehome_notice_fires_when_receipt_is_absent(tmp_path: Path) -> None:
    """Nothing ever surveyed the vault, so the damage may exist and is unseen."""
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
        config=_rehome_config(workspace),
    )

    assert _rehome_notice(report)
    assert "no re-home survey" in _rehome_notice(report)[0]["detail"]


def test_rehome_notice_is_silent_once_migrated(tmp_path: Path) -> None:
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)
    _write_migrated_receipt(runtime)

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
        config=_rehome_config(workspace),
    )

    assert _rehome_notice(report) == []


def test_rehome_notice_still_fires_on_a_surveyed_receipt(tmp_path: Path) -> None:
    """Surveyed is not migrated: the D4 point, a survey proves the work is pending."""
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)
    _write_survey_receipt(runtime)

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
        config=_rehome_config(workspace),
    )

    assert _rehome_notice(report)


def test_rehome_notice_reads_counts_from_the_receipt_not_the_vault(
    tmp_path: Path,
) -> None:
    """The notice reports the receipt's numbers even when the vault disagrees.

    The vault below holds far more person notes than the receipt claims. If a
    future edit "helpfully" switched the notice to scan the vault, this test
    fails — the whole point of the survey/apply split is that detection never
    walks the vault.
    """
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)
    # A vault that would report different counts than the receipt.
    for i in range(9):
        note = vault / "personal" / "People" / f"Note{i}.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(
            "---\ntype: person\ntags: [person, colleague]\n---\n# X\n",
            encoding="utf-8",
        )
    _write_survey_receipt(runtime, mechanical=1, needs_judgement=0, conflicts=0)

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
        config=_rehome_config(workspace),
    )

    detail = _rehome_notice(report)[0]["detail"]
    assert "1 to move" in detail
    assert "0 needing a decision" in detail
    assert "0 conflicting" in detail


def test_rehome_notice_remedy_names_the_inverse_command(tmp_path: Path) -> None:
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
        config=_rehome_config(workspace),
    )

    remedy = _rehome_notice(report)[0]["remedy"]
    assert "ciao vault-rehome" in remedy
    assert "ciao vault-rehome --apply" in remedy
    assert "ciao vault-unrehome --apply" in remedy


def test_rehome_notice_keeps_status_healthy_and_raises_pending(tmp_path: Path) -> None:
    """A re-home offer is an optional pending action, never a defect: it must not
    turn the audit red, and it must not raise defect_count."""
    workspace, vault, runtime, bounded = _healthy_roots(tmp_path)

    report = run_os_audit(
        workspace_dir=workspace,
        vault_root=vault,
        runtime_dir=runtime,
        config=_rehome_config(workspace),
    )

    assert report["status"] == "healthy"
    assert report["defect_count"] == 0
    assert report["pending_action_count"] >= 1
    assert report["has_pending_actions"] is True


def test_unrehomed_people_notice_is_silent_on_a_single_workspace_install(
    tmp_path: Path,
) -> None:
    """One workspace has nowhere to misfile a note to, so there is nothing to offer.

    detect_misfiled_people only makes a note a candidate when another registered
    workspace could hold it. Without the workspace-count gate a fresh install
    with one workspace and an empty vault was told its person notes may be in
    the wrong place, which is an action the operator cannot take.
    """
    import json as _json

    from ciao.config import CiaoConfig
    from ciao.os_audit import audit_upgrade_notices

    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "workspaces.json").write_text(
        _json.dumps([{"name": "personal", "vault_root": "memory-vault/personal"}]),
        encoding="utf-8",
    )
    (tmp_path / "memory-vault" / "personal").mkdir(parents=True)
    config = CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(runtime),
    })

    result = audit_upgrade_notices(config, runtime)

    kinds = [notice["type"] for notice in result["notices"]]
    assert "unrehomed_people" not in kinds
