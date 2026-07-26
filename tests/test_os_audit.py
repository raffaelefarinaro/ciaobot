"""Tests for ``ciao.os_audit`` AI OS Audit Suite."""

from __future__ import annotations

import json
from pathlib import Path

from ciao import memory_tool as mt
from ciao.os_audit import (
    audit_job_runs,
    audit_memory,
    audit_rules,
    audit_skills,
    format_audit_markdown,
    run_os_audit,
)


def test_audit_skills_over_budget(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "big-skill"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    # Write > 15,000 bytes
    skill_md.write_bytes(b"A" * 16_000)

    res = audit_skills(tmp_path)
    assert res["total_skills"] == 1
    assert res["over_budget_count"] == 1
    assert res["issues"][0]["type"] == "skill_over_budget"


def test_audit_rules_clash_detection(tmp_path: Path) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    agents_md = tmp_path / "AGENTS.md"

    rule_text = "- Keep code changes focused and covered by automated unit tests."
    claude_md.write_text(rule_text, encoding="utf-8")
    agents_md.write_text(rule_text, encoding="utf-8")

    res = audit_rules(tmp_path)
    assert res["rule_clashes_found"] == 1
    assert len(res["clashes"]) == 1


def test_audit_memory_hygiene(tmp_path: Path) -> None:
    mt.add_entry(tmp_path / "memory.md", "durable lesson", char_limit=200)
    mt.add_entry(tmp_path / "memory.md", "old task [expires: 2020-01-01]", char_limit=200)

    res = audit_memory(memory_dir=tmp_path)
    assert res["memory_entries"] == 2
    assert res["expired_memory_entries"] == 1


def test_audit_job_runs_failures(tmp_path: Path) -> None:
    runtime_dir = tmp_path / ".runtime"
    runtime_dir.mkdir(parents=True)
    job_file = runtime_dir / "job_runs.jsonl"
    job_file.write_text(
        json.dumps({"job": "skill_evolution", "status": "error", "error": "timeout", "ts": "2026-07-26T12:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    res = audit_job_runs(tmp_path)
    assert res["total_runs"] == 1
    assert res["failed_runs"] == 1
    assert res["recent_failures"][0]["job"] == "skill_evolution"


def test_run_os_audit_complete_flow(tmp_path: Path) -> None:
    report = run_os_audit(workspace_dir=tmp_path, vault_root=tmp_path / "memory-vault")
    assert "status" in report
    assert "total_issues" in report
    md_summary = format_audit_markdown(report)
    assert "# AI OS Audit Report" in md_summary
