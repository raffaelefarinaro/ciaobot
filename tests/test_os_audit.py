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

    res = audit_rules(tmp_path, memory_dir=tmp_path / "bounded")
    assert res["rule_clashes_found"] == 0
    assert res["rule_overlaps_found"] == 0
    assert res["errors"] == []


def test_audit_rules_separates_overlaps_from_conflicts(tmp_path: Path) -> None:
    same = "- Keep code changes focused and covered by automated unit tests."
    (tmp_path / "CLAUDE.md").write_text(same, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(same, encoding="utf-8")

    overlap = audit_rules(tmp_path, memory_dir=tmp_path / "bounded")
    assert overlap["rule_overlaps_found"] == 1
    assert overlap["rule_clashes_found"] == 0

    (tmp_path / "CLAUDE.md").write_text("- Always use rtk for shell commands.", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("- Never use rtk for shell commands.", encoding="utf-8")

    conflict = audit_rules(tmp_path, memory_dir=tmp_path / "bounded")
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
        memory_dir=tmp_path / "bounded",
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
        memory_dir=tmp_path / "bounded",
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
    res = audit_memory(memory_dir=tmp_path / "bounded", vault_root=vault)

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
        memory_dir=bounded,
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
        memory_dir=tmp_path / "bounded",
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
        "---\ntype: idea\n---\n# One\n\n[[missing-target]]\n",
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
        memory_dir=bounded,
        today=datetime.date(2026, 7, 26),
    )
    assert report["status"] == "needs_attention"
    # Vault: 1 broken link + 2 orphans + 1 duplicate.
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
        memory_dir=bounded,
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
        memory_dir=bounded,
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
        memory_dir=bounded,
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
        memory_dir=bounded,
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
        memory_dir=bounded,
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
        memory_dir=bounded,
    )

    assert report["status"] == "error"
    assert report["total_errors"] == 2
    assert len(report["scan_errors"]) == 2
