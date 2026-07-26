"""AI OS Setup & Context Hygiene Auditor for Ciaobot.

Provides comprehensive auditing across five core AI OS hygiene pillars:
1. Vault & Link Hygiene (wikilinks, orphans, duplicates).
2. Skill Budget & Quality (15KB cap check, missing SKILL.md files).
3. Rule Conflict & Overlap Detection (duplicate rules between CLAUDE.md, AGENTS.md, and memory.md).
4. Memory Hygiene & Expiration (expired entry count, memory proposals backlog).
5. System Job & Provider Health (recent job errors from .runtime/job_runs.jsonl).
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from pathlib import Path
from typing import Any

from ciao.memory_injector import is_entry_expired
from ciao.memory_tool import default_memory_dir, load_entries, memory_path, user_path
from ciao.vault_lint import run_validation as run_vault_validation

logger = logging.getLogger(__name__)

SKILL_MAX_BYTES = 15_000


def audit_skills(workspace_dir: Path) -> dict[str, Any]:
    """Audit skills in workspace/skills and .claude/skills."""
    issues: list[dict[str, Any]] = []
    total_skills = 0
    skill_dirs: set[Path] = set()

    for base in [workspace_dir / "skills", workspace_dir / ".claude" / "skills"]:
        if base.exists() and base.is_dir():
            for child in base.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    skill_dirs.add(child)

    for sdir in sorted(skill_dirs):
        total_skills += 1
        skill_md = sdir / "SKILL.md"
        if not skill_md.exists():
            issues.append({
                "type": "missing_skill_md",
                "skill": sdir.name,
                "path": str(skill_md),
                "message": f"Skill directory '{sdir.name}' is missing SKILL.md",
            })
        else:
            size = skill_md.stat().st_size
            if size > SKILL_MAX_BYTES:
                issues.append({
                    "type": "skill_over_budget",
                    "skill": sdir.name,
                    "path": str(skill_md),
                    "size_bytes": size,
                    "max_bytes": SKILL_MAX_BYTES,
                    "message": f"Skill '{sdir.name}' exceeds budget: {size:,} bytes > {SKILL_MAX_BYTES:,} bytes",
                })

    return {
        "total_skills": total_skills,
        "over_budget_count": len([i for i in issues if i["type"] == "skill_over_budget"]),
        "issues": issues,
    }


def audit_rules(workspace_dir: Path, memory_dir: Path | None = None) -> dict[str, Any]:
    """Audit instruction files for rule clashes and duplicates."""
    claude_md = workspace_dir / "CLAUDE.md"
    agents_md = workspace_dir / "AGENTS.md"
    mem_file = memory_path(memory_dir)

    rule_lines: dict[str, list[str]] = {}
    sources = [("CLAUDE.md", claude_md), ("AGENTS.md", agents_md), ("memory.md", mem_file)]

    for label, path in sources:
        if path.exists() and path.is_file():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    cleaned = line.strip()
                    # Only analyze bullet points or explicit rule lines (> 10 chars)
                    if (cleaned.startswith("- ") or cleaned.startswith("* ")) and len(cleaned) > 15:
                        normalized = re.sub(r"\s+", " ", cleaned.lower())
                        rule_lines.setdefault(normalized, []).append(f"{label}: {cleaned}")
            except Exception:  # noqa: BLE001
                pass

    clashes = [
        {"rule": occurrences[0], "sources": occurrences}
        for norm, occurrences in rule_lines.items()
        if len(occurrences) > 1
    ]

    return {
        "rule_clashes_found": len(clashes),
        "clashes": clashes,
    }


def audit_memory(memory_dir: Path | None = None, vault_root: Path | None = None) -> dict[str, Any]:
    """Audit bounded memory files and memory proposals."""
    mdir = memory_dir or default_memory_dir()
    mem_entries = load_entries(memory_path(mdir))
    usr_entries = load_entries(user_path(mdir))

    today = datetime.date.today()
    expired_mem = [e for e in mem_entries if is_entry_expired(e, today)]
    expired_usr = [e for e in usr_entries if is_entry_expired(e, today)]

    proposals_count = 0
    if vault_root and vault_root.exists():
        proposals_file = vault_root / "Workspace" / "Memory-Proposals.md"
        if proposals_file.exists():
            try:
                content = proposals_file.read_text(encoding="utf-8")
                proposals_count = len(re.findall(r"^-\s+", content, re.MULTILINE))
            except Exception:  # noqa: BLE001
                pass

    return {
        "memory_entries": len(mem_entries),
        "user_entries": len(usr_entries),
        "expired_memory_entries": len(expired_mem),
        "expired_user_entries": len(expired_usr),
        "pending_memory_proposals": proposals_count,
    }


def audit_job_runs(workspace_dir: Path) -> dict[str, Any]:
    """Audit recent background job runs from .runtime/job_runs.jsonl."""
    job_runs_file = workspace_dir / ".runtime" / "job_runs.jsonl"
    if not job_runs_file.exists():
        return {"total_runs": 0, "failed_runs": 0, "recent_failures": []}

    runs: list[dict[str, Any]] = []
    try:
        lines = job_runs_file.read_text(encoding="utf-8").splitlines()
        for line in lines[-50:]:  # Check last 50 runs
            if line.strip():
                try:
                    runs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception:  # noqa: BLE001
        pass

    failures = [r for r in runs if r.get("status") == "error" or r.get("error")]
    return {
        "total_runs": len(runs),
        "failed_runs": len(failures),
        "recent_failures": [
            {
                "job": r.get("job", "unknown"),
                "error": str(r.get("error", ""))[:200],
                "ts": r.get("ts"),
            }
            for r in failures[:5]
        ],
    }


def run_os_audit(
    workspace_dir: Path | None = None,
    vault_root: Path | None = None,
    memory_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute a complete AI OS Audit pass."""
    ws = workspace_dir or Path.cwd()
    vr = vault_root or (ws / "memory-vault")

    vault_res = run_vault_validation(vr) if vr.exists() else {"broken_links": [], "orphans": [], "duplicates": []}
    skill_res = audit_skills(ws)
    rule_res = audit_rules(ws, memory_dir)
    mem_res = audit_memory(memory_dir, vr)
    job_res = audit_job_runs(ws)

    issue_count = (
        len(vault_res.get("broken_links", []))
        + skill_res["over_budget_count"]
        + rule_res["rule_clashes_found"]
        + mem_res["expired_memory_entries"]
        + mem_res["expired_user_entries"]
        + job_res["failed_runs"]
    )

    status = "healthy" if issue_count == 0 else "needs_attention"

    return {
        "status": status,
        "total_issues": issue_count,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "vault_hygiene": vault_res,
        "skill_audit": skill_res,
        "rule_audit": rule_res,
        "memory_hygiene": mem_res,
        "job_runs_audit": job_res,
    }


def format_audit_markdown(report: dict[str, Any]) -> str:
    """Format an audit report dict into human-readable markdown."""
    status_emoji = "✅" if report["status"] == "healthy" else "⚠️"
    lines = [
        f"# AI OS Audit Report {status_emoji}",
        f"**Status**: {report['status'].upper()} | **Total Issues**: {report['total_issues']}",
        f"**Timestamp**: {report['timestamp']}",
        "",
        "---",
        "",
        "## 1. Vault & Knowledge Hygiene",
        f"- Broken Wikilinks: {len(report['vault_hygiene'].get('broken_links', []))}",
        f"- Orphaned Notes: {len(report['vault_hygiene'].get('orphans', []))}",
        f"- Duplicate Stems: {len(report['vault_hygiene'].get('duplicates', []))}",
        "",
        "## 2. Skill Budget & Quality",
        f"- Total Skills Scanned: {report['skill_audit']['total_skills']}",
        f"- Skills Over 15KB Budget: {report['skill_audit']['over_budget_count']}",
    ]

    for issue in report['skill_audit']['issues']:
        lines.append(f"  - ⚠️ {issue['message']}")

    lines.extend([
        "",
        "## 3. Rule Clashes & Overlaps",
        f"- Potential Rule Clashes: {report['rule_audit']['rule_clashes_found']}",
    ])
    for clash in report['rule_audit']['clashes'][:5]:
        lines.append(f"  - ⚠️ Duplicate rule found across files:")
        for src in clash['sources']:
            lines.append(f"    - {src}")

    lines.extend([
        "",
        "## 4. Memory & Context Hygiene",
        f"- Memory Entries: {report['memory_hygiene']['memory_entries']} (Expired: {report['memory_hygiene']['expired_memory_entries']})",
        f"- User Profile Entries: {report['memory_hygiene']['user_entries']} (Expired: {report['memory_hygiene']['expired_user_entries']})",
        f"- Pending Memory Proposals: {report['memory_hygiene']['pending_memory_proposals']}",
        "",
        "## 5. Background Automation & Job Runs",
        f"- Recent Job Failures: {report['job_runs_audit']['failed_runs']}/{report['job_runs_audit']['total_runs']}",
    ])
    for fail in report['job_runs_audit']['recent_failures']:
        lines.append(f"  - 🔴 [{fail.get('job')}]: {fail.get('error')}")

    return "\n".join(lines)
