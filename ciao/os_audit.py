"""AI OS setup and context-hygiene auditing for Ciaobot.

The audit is deliberately stricter than normal runtime readers. Ciaobot's
chat and automation paths fail open when optional context cannot be loaded,
but an auditor must never turn missing or malformed evidence into a clean
bill of health.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from ciao.job_runs import JOB_RUNS_LATEST_NAME, JOB_RUNS_NAME
from ciao.memory_injector import expiration_tag_error, is_entry_expired
from ciao.memory_tool import default_memory_dir, memory_path, parse_entries, user_path
from ciao.vault_lint import run_validation as run_vault_validation

logger = logging.getLogger(__name__)

SKILL_MAX_BYTES = 15 * 1024

_PROPOSAL_BULLET_RE = re.compile(r"^\s*-\s*\[(?:memory|user)\]\s+\S", re.IGNORECASE)
_RULE_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
_RULE_NEGATION_RE = re.compile(
    r"\b(?:never|(?:do|does|must|should|can|cannot)\s+not|don't|doesn't|mustn't|shouldn't|can't)\b",
    re.IGNORECASE,
)
_RULE_LEADING_MODAL_RE = re.compile(
    r"^(?:always|never|must(?:\s+not)?|should(?:\s+not)?|"
    r"do\s+not|does\s+not|don't|doesn't)\s+",
    re.IGNORECASE,
)


def _diagnostic(type_: str, path: Path, message: str) -> dict[str, str]:
    return {
        "type": type_,
        "path": str(path),
        "message": message,
    }


def audit_setup(
    workspace_dir: Path,
    vault_root: Path,
    runtime_dir: Path,
) -> dict[str, Any]:
    """Validate roots required for a reliable audit."""
    errors: list[dict[str, str]] = []
    issues: list[dict[str, str]] = []
    checks: list[dict[str, str]] = []

    for name, path in (
        ("workspace_root", workspace_dir),
        ("vault_root", vault_root),
        ("runtime_root", runtime_dir),
    ):
        if not path.exists():
            checks.append({"name": name, "status": "error", "path": str(path)})
            errors.append(
                _diagnostic(
                    f"missing_{name}",
                    path,
                    f"{name.replace('_', ' ')} does not exist",
                )
            )
            continue
        if not path.is_dir():
            checks.append({"name": name, "status": "error", "path": str(path)})
            errors.append(
                _diagnostic(
                    f"invalid_{name}",
                    path,
                    f"{name.replace('_', ' ')} is not a directory",
                )
            )
            continue
        if not os.access(path, os.R_OK):
            checks.append({"name": name, "status": "error", "path": str(path)})
            errors.append(
                _diagnostic(
                    f"unreadable_{name}",
                    path,
                    f"{name.replace('_', ' ')} is not readable",
                )
            )
            continue
        checks.append({"name": name, "status": "ok", "path": str(path)})

    if workspace_dir.is_dir():
        for filename in ("CLAUDE.md", "AGENTS.md"):
            path = workspace_dir / filename
            if not path.is_file():
                issues.append(
                    _diagnostic(
                        "missing_instruction_file",
                        path,
                        f"{filename} is missing",
                    )
                )

    return {
        "workspace_root": str(workspace_dir),
        "vault_root": str(vault_root),
        "runtime_root": str(runtime_dir),
        "checks": checks,
        "issues": issues,
        "errors": errors,
    }


def audit_skills(workspace_dir: Path) -> dict[str, Any]:
    """Audit logical skills once across canonical and provider projections."""
    issues: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    skill_dirs: dict[str, Path] = {}

    roots = (
        workspace_dir / "skills",
        workspace_dir / ".claude" / "skills",
        workspace_dir / ".agents" / "skills",
    )
    for base in roots:
        if not base.exists():
            continue
        if not base.is_dir():
            errors.append(
                _diagnostic("invalid_skill_root", base, "skill root is not a directory")
            )
            continue
        try:
            children = sorted(base.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            errors.append(
                _diagnostic("unreadable_skill_root", base, f"failed to list skills: {exc}")
            )
            continue
        for child in children:
            try:
                is_directory = child.is_dir()
            except OSError as exc:
                errors.append(
                    _diagnostic(
                        "unreadable_skill_path",
                        child,
                        f"failed to inspect skill path: {exc}",
                    )
                )
                continue
            if is_directory and not child.name.startswith("."):
                # Canonical skills win, followed by Claude and Codex projections.
                skill_dirs.setdefault(child.name, child)

    for name, skill_dir in sorted(skill_dirs.items()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            issues.append(
                {
                    "type": "missing_skill_md",
                    "skill": name,
                    "path": str(skill_md),
                    "message": f"Skill directory '{name}' is missing SKILL.md",
                }
            )
            continue
        try:
            size = skill_md.stat().st_size
        except OSError as exc:
            errors.append(
                _diagnostic(
                    "unreadable_skill_file",
                    skill_md,
                    f"failed to inspect SKILL.md: {exc}",
                )
            )
            continue
        if size > SKILL_MAX_BYTES:
            issues.append(
                {
                    "type": "skill_over_budget",
                    "skill": name,
                    "path": str(skill_md),
                    "size_bytes": size,
                    "max_bytes": SKILL_MAX_BYTES,
                    "message": (
                        f"Skill '{name}' exceeds budget: "
                        f"{size:,} bytes > {SKILL_MAX_BYTES:,} bytes"
                    ),
                }
            )

    return {
        "total_skills": len(skill_dirs),
        "over_budget_count": sum(
            1 for issue in issues if issue["type"] == "skill_over_budget"
        ),
        "missing_skill_md_count": sum(
            1 for issue in issues if issue["type"] == "missing_skill_md"
        ),
        "issues": issues,
        "errors": errors,
    }


def _normalized_rule(rule: str) -> str:
    lowered = rule.lower().replace("’", "'")
    return re.sub(r"\s+", " ", lowered).strip()


def _rule_signature(rule: str) -> tuple[str, str]:
    polarity = "negative" if _RULE_NEGATION_RE.search(rule) else "positive"
    without_modal = _RULE_LEADING_MODAL_RE.sub("", rule.strip())
    signature = re.sub(r"[^a-z0-9]+", " ", without_modal.lower()).strip()
    return signature, polarity


def _guide_rules(text: str) -> list[str]:
    rules: list[str] = []
    for line in text.splitlines():
        match = _RULE_BULLET_RE.match(line)
        if match:
            rule = match.group(1).strip()
            if len(rule) > 15:
                rules.append(rule)
    return rules


def audit_rules(
    workspace_dir: Path,
    memory_dir: Path | None = None,
) -> dict[str, Any]:
    """Find exact cross-file overlaps and obvious opposite-polarity rules."""
    sources = [
        ("CLAUDE.md", workspace_dir / "CLAUDE.md", "guide"),
        ("AGENTS.md", workspace_dir / "AGENTS.md", "guide"),
        ("memory.md", memory_path(memory_dir), "memory"),
    ]
    occurrences: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    seen_files: set[Path] = set()
    aliases_skipped: list[dict[str, str]] = []

    for label, path, kind in sources:
        if not path.exists():
            continue
        if not path.is_file():
            errors.append(
                _diagnostic("invalid_rule_source", path, f"{label} is not a file")
            )
            continue
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            errors.append(
                _diagnostic(
                    "unreadable_rule_source",
                    path,
                    f"failed to resolve {label}: {exc}",
                )
            )
            continue
        if resolved in seen_files:
            aliases_skipped.append({"source": label, "target": str(resolved)})
            continue
        seen_files.add(resolved)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(
                _diagnostic(
                    "unreadable_rule_source",
                    path,
                    f"failed to read {label}: {exc}",
                )
            )
            continue

        if kind == "memory":
            rules = [entry for entry in parse_entries(text) if len(entry) > 15]
        else:
            rules = _guide_rules(text)
        for rule in rules:
            signature, polarity = _rule_signature(rule)
            if not signature:
                continue
            occurrences.append(
                {
                    "source": label,
                    "rule": rule,
                    "normalized": _normalized_rule(rule),
                    "signature": signature,
                    "polarity": polarity,
                }
            )

    by_normalized: dict[str, list[dict[str, str]]] = {}
    by_signature: dict[str, list[dict[str, str]]] = {}
    for occurrence in occurrences:
        by_normalized.setdefault(occurrence["normalized"], []).append(occurrence)
        by_signature.setdefault(occurrence["signature"], []).append(occurrence)

    overlaps: list[dict[str, Any]] = []
    for matching in by_normalized.values():
        unique_sources = {item["source"] for item in matching}
        if len(unique_sources) < 2:
            continue
        overlaps.append(
            {
                "rule": matching[0]["rule"],
                "sources": [
                    f"{item['source']}: {item['rule']}" for item in matching
                ],
            }
        )

    clashes: list[dict[str, Any]] = []
    for signature, matching in by_signature.items():
        polarities = {item["polarity"] for item in matching}
        unique_sources = {item["source"] for item in matching}
        if polarities != {"positive", "negative"} or len(unique_sources) < 2:
            continue
        clashes.append(
            {
                "signature": signature,
                "sources": [
                    f"{item['source']}: {item['rule']}" for item in matching
                ],
            }
        )

    return {
        "rule_clashes_found": len(clashes),
        "clashes": clashes,
        "rule_overlaps_found": len(overlaps),
        "overlaps": overlaps,
        "aliases_skipped": aliases_skipped,
        "errors": errors,
    }


def _strict_memory_entries(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    try:
        return parse_entries(path.read_text(encoding="utf-8")), []
    except (OSError, UnicodeDecodeError) as exc:
        return [], [
            _diagnostic(
                "unreadable_memory_file",
                path,
                f"failed to read bounded memory: {exc}",
            )
        ]


def _proposal_paths(
    vault_root: Path,
) -> tuple[list[Path], list[dict[str, str]]]:
    candidates = [vault_root / "Workspace" / "Memory-Proposals.md"]
    errors: list[dict[str, str]] = []
    if vault_root.is_dir():
        try:
            children = sorted(vault_root.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            children = []
            errors.append(
                _diagnostic(
                    "unreadable_proposal_root",
                    vault_root,
                    f"failed to discover workspace proposal queues: {exc}",
                )
            )
        for child in children:
            if child.is_dir():
                candidates.append(child / "Workspace" / "Memory-Proposals.md")
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        lexical = path.absolute()
        if lexical not in seen:
            seen.add(lexical)
            unique.append(path)
    return unique, errors


def audit_memory(
    memory_dir: Path | None = None,
    vault_root: Path | None = None,
    *,
    proposal_paths: list[Path] | None = None,
    today: datetime.date | None = None,
) -> dict[str, Any]:
    """Audit bounded memory expiration and proposal queues."""
    memory_root = memory_dir or default_memory_dir()
    mem_entries, mem_errors = _strict_memory_entries(memory_path(memory_root))
    usr_entries, usr_errors = _strict_memory_entries(user_path(memory_root))
    current = today or datetime.date.today()

    expired_mem = [entry for entry in mem_entries if is_entry_expired(entry, current)]
    expired_usr = [entry for entry in usr_entries if is_entry_expired(entry, current)]
    invalid_expirations: list[dict[str, str]] = []
    for target, entries in (("memory", mem_entries), ("user", usr_entries)):
        for entry in entries:
            error = expiration_tag_error(entry)
            if error:
                invalid_expirations.append(
                    {
                        "target": target,
                        "entry": entry[:160],
                        "message": error,
                    }
                )

    proposals_count = 0
    proposal_files: list[dict[str, Any]] = []
    proposal_errors: list[dict[str, str]] = []
    if vault_root is not None:
        if proposal_paths is None:
            paths, discovery_errors = _proposal_paths(vault_root)
            proposal_errors.extend(discovery_errors)
        else:
            paths = proposal_paths
        for path in dict.fromkeys(paths):
            if not path.exists():
                continue
            if not path.is_file():
                proposal_errors.append(
                    _diagnostic(
                        "invalid_proposal_file",
                        path,
                        "memory proposal path is not a file",
                    )
                )
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                proposal_errors.append(
                    _diagnostic(
                        "unreadable_proposal_file",
                        path,
                        f"failed to read memory proposals: {exc}",
                    )
                )
                continue
            pending = sum(
                1 for line in content.splitlines() if _PROPOSAL_BULLET_RE.match(line)
            )
            proposals_count += pending
            proposal_files.append({"path": str(path), "pending": pending})

    return {
        "memory_entries": len(mem_entries),
        "user_entries": len(usr_entries),
        "expired_memory_entries": len(expired_mem),
        "expired_user_entries": len(expired_usr),
        "invalid_expiration_entries": len(invalid_expirations),
        "invalid_expirations": invalid_expirations,
        "pending_memory_proposals": proposals_count,
        "proposal_files": proposal_files,
        "errors": [*mem_errors, *usr_errors, *proposal_errors],
    }


def _record_timestamp(record: dict[str, Any]) -> str:
    raw = record.get("ended_at") or record.get("started_at")
    return raw if isinstance(raw, str) else ""


def _parsed_timestamp(record: dict[str, Any]) -> datetime.datetime | None:
    raw = _record_timestamp(record)
    if not raw:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _job_record_error(record: Any) -> str | None:
    if not isinstance(record, dict):
        return "record must be a JSON object"
    if not isinstance(record.get("job"), str) or not record["job"].strip():
        return "job must be a non-empty string"
    status = record.get("status")
    if status not in {"ok", "error", "skipped"}:
        return "status must be one of: ok, error, skipped"
    if _parsed_timestamp(record) is None:
        return "started_at or ended_at must be a valid ISO-8601 timestamp"
    error = record.get("error")
    if error is not None and not isinstance(error, str):
        return "error must be a string or null"
    if status in {"ok", "skipped"} and error:
        return f"status {status!r} contradicts a non-empty error"
    return None


def _valid_job_record(record: Any) -> bool:
    return _job_record_error(record) is None


def audit_job_runs(
    workspace_dir: Path,
    *,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Audit the latest state of each background job with strict diagnostics."""
    runtime = runtime_dir or (workspace_dir / ".runtime")
    log_path = runtime / JOB_RUNS_NAME
    latest_path = runtime / JOB_RUNS_LATEST_NAME
    errors: list[dict[str, str]] = []
    invalid_records = 0
    history: list[dict[str, Any]] = []

    if not runtime.exists() or not runtime.is_dir():
        errors.append(
            _diagnostic(
                "missing_runtime_root",
                runtime,
                "runtime root is missing or is not a directory",
            )
        )
    elif log_path.exists():
        if not log_path.is_file():
            errors.append(
                _diagnostic(
                    "invalid_job_run_log",
                    log_path,
                    "job-run log is not a file",
                )
            )
        else:
            try:
                lines = log_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(
                    _diagnostic(
                        "unreadable_job_run_log",
                        log_path,
                        f"failed to read job-run log: {exc}",
                    )
                )
                lines = []
            for line_number, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    invalid_records += 1
                    continue
                if not _valid_job_record(record):
                    invalid_records += 1
                    continue
                history.append(record)
            if invalid_records:
                errors.append(
                    _diagnostic(
                        "invalid_job_run_records",
                        log_path,
                        f"{invalid_records} malformed or invalid job-run record(s)",
                    )
                )

    latest_by_job: dict[str, dict[str, Any]] = {}
    for record in history:
        job = record["job"]
        current = latest_by_job.get(job)
        if current is None or _parsed_timestamp(record) > _parsed_timestamp(current):
            latest_by_job[job] = record

    if latest_path.exists():
        if not latest_path.is_file():
            errors.append(
                _diagnostic(
                    "invalid_latest_job_index",
                    latest_path,
                    "latest job-run index is not a file",
                )
            )
        else:
            try:
                raw_latest = json.loads(latest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                errors.append(
                    _diagnostic(
                        "unreadable_latest_job_index",
                        latest_path,
                        f"failed to read latest job-run index: {exc}",
                    )
                )
                raw_latest = {}
            if not isinstance(raw_latest, dict):
                errors.append(
                    _diagnostic(
                        "invalid_latest_job_index",
                        latest_path,
                        "latest job-run index must be a JSON object",
                    )
                )
                raw_latest = {}
            for job, record in raw_latest.items():
                record_error = _job_record_error(record)
                if record_error is None and record.get("job") != job:
                    record_error = "record job does not match its index key"
                if record_error is not None:
                    errors.append(
                        _diagnostic(
                            "invalid_latest_job_record",
                            latest_path,
                            f"latest record for {job!r} is invalid: {record_error}",
                        )
                    )
                    continue
                current = latest_by_job.get(job)
                current_ts = _parsed_timestamp(current) if current else None
                latest_ts = _parsed_timestamp(record)
                if current is None or latest_ts is None or current_ts is None or latest_ts > current_ts:
                    latest_by_job[job] = record

    failures = [
        record for record in latest_by_job.values() if record.get("status") == "error"
    ]
    failures.sort(
        key=lambda record: _parsed_timestamp(record)
        or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
        reverse=True,
    )

    return {
        "runtime_root": str(runtime),
        "total_runs": len(history),
        "latest_jobs": len(latest_by_job),
        "failed_runs": len(failures),
        "recent_failures": [
            {
                "job": record.get("job", "unknown"),
                "error": str(record.get("error") or "")[:200],
                "ts": _record_timestamp(record) or None,
            }
            for record in failures[:5]
        ],
        "invalid_records": invalid_records,
        "errors": errors,
    }


def _vault_audit(vault_root: Path) -> dict[str, Any]:
    if not vault_root.is_dir():
        return {
            "broken_links": [],
            "orphans": [],
            "duplicates": [],
            "errors": [],
        }
    errors: list[dict[str, str]] = []
    try:
        markdown_files = vault_root.rglob("*.md")
        for path in markdown_files:
            try:
                path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(
                    _diagnostic(
                        "unreadable_vault_file",
                        path,
                        f"failed to read vault markdown: {exc}",
                    )
                )
        result = run_vault_validation(vault_root)
    except Exception as exc:  # noqa: BLE001
        logger.exception("OS audit: vault validation failed")
        return {
            "broken_links": [],
            "orphans": [],
            "duplicates": [],
            "errors": [
                _diagnostic(
                    "vault_validation_failed",
                    vault_root,
                    f"vault validation failed: {exc}",
                )
            ],
        }
    result["errors"] = errors
    return result


def run_os_audit(
    workspace_dir: Path | None = None,
    vault_root: Path | None = None,
    memory_dir: Path | None = None,
    runtime_dir: Path | None = None,
    *,
    proposal_paths: list[Path] | None = None,
    today: datetime.date | None = None,
) -> dict[str, Any]:
    """Execute a complete AI OS audit pass."""
    workspace = (workspace_dir or Path.cwd()).expanduser().resolve()
    vault = (vault_root or (workspace / "memory-vault")).expanduser().resolve()
    runtime = (runtime_dir or (workspace / ".runtime")).expanduser().resolve()

    setup_result = audit_setup(workspace, vault, runtime)
    vault_result = _vault_audit(vault)
    skill_result = audit_skills(workspace)
    rule_result = audit_rules(workspace, memory_dir)
    memory_result = audit_memory(
        memory_dir,
        vault,
        proposal_paths=proposal_paths,
        today=today,
    )
    job_result = audit_job_runs(workspace, runtime_dir=runtime)

    collected_errors = [
        *setup_result["errors"],
        *vault_result["errors"],
        *skill_result["errors"],
        *rule_result["errors"],
        *memory_result["errors"],
        *job_result["errors"],
    ]
    scan_errors: list[dict[str, str]] = []
    seen_errors: set[tuple[str, ...]] = set()
    for error in collected_errors:
        if error["type"] in {
            "missing_workspace_root",
            "missing_vault_root",
            "missing_runtime_root",
            "invalid_workspace_root",
            "invalid_vault_root",
            "invalid_runtime_root",
            "unreadable_workspace_root",
            "unreadable_vault_root",
            "unreadable_runtime_root",
        }:
            key = (error["type"], error["path"])
        else:
            key = (error["type"], error["path"], error["message"])
        if key not in seen_errors:
            seen_errors.add(key)
            scan_errors.append(error)
    actionable_count = (
        len(setup_result["issues"])
        + len(vault_result.get("broken_links", []))
        + len(vault_result.get("orphans", []))
        + len(vault_result.get("duplicates", []))
        + len(skill_result["issues"])
        + rule_result["rule_clashes_found"]
        + memory_result["expired_memory_entries"]
        + memory_result["expired_user_entries"]
        + memory_result["invalid_expiration_entries"]
        + memory_result["pending_memory_proposals"]
        + job_result["failed_runs"]
    )
    total_errors = len(scan_errors)
    total_issues = actionable_count + total_errors
    if total_errors:
        status = "error"
    elif total_issues:
        status = "needs_attention"
    else:
        status = "healthy"

    return {
        "status": status,
        "total_issues": total_issues,
        "total_errors": total_errors,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "setup_audit": setup_result,
        "vault_hygiene": vault_result,
        "skill_audit": skill_result,
        "rule_audit": rule_result,
        "memory_hygiene": memory_result,
        "job_runs_audit": job_result,
        "scan_errors": scan_errors,
    }


def format_audit_markdown(report: dict[str, Any]) -> str:
    """Format an audit report dict into human-readable Markdown."""
    status_icon = {
        "healthy": "✅",
        "needs_attention": "⚠️",
        "error": "❌",
    }.get(report["status"], "⚠️")
    lines = [
        f"# AI OS Audit Report {status_icon}",
        (
            f"**Status**: {report['status'].upper()} | "
            f"**Total Issues**: {report['total_issues']} | "
            f"**Scan Errors**: {report['total_errors']}"
        ),
        f"**Timestamp**: {report['timestamp']}",
        "",
        "## 1. Setup",
        f"- Workspace: `{report['setup_audit']['workspace_root']}`",
        f"- Vault: `{report['setup_audit']['vault_root']}`",
        f"- Runtime: `{report['setup_audit']['runtime_root']}`",
        f"- Setup findings: {len(report['setup_audit']['issues'])}",
        "",
        "## 2. Vault & Knowledge Hygiene",
        f"- Broken wikilinks: {len(report['vault_hygiene'].get('broken_links', []))}",
        f"- Orphaned notes: {len(report['vault_hygiene'].get('orphans', []))}",
        f"- Duplicate stems: {len(report['vault_hygiene'].get('duplicates', []))}",
        "",
        "## 3. Skill Budget & Quality",
        f"- Logical skills scanned: {report['skill_audit']['total_skills']}",
        f"- Skills over 15 KiB: {report['skill_audit']['over_budget_count']}",
        f"- Missing SKILL.md: {report['skill_audit']['missing_skill_md_count']}",
    ]
    for issue in report["skill_audit"]["issues"]:
        lines.append(f"  - ⚠️ {issue['message']}")

    lines.extend(
        [
            "",
            "## 4. Rule Conflicts & Overlaps",
            f"- Potential conflicts: {report['rule_audit']['rule_clashes_found']}",
            (
                "- Informational exact overlaps: "
                f"{report['rule_audit']['rule_overlaps_found']}"
            ),
        ]
    )
    for clash in report["rule_audit"]["clashes"][:5]:
        lines.append(f"  - ⚠️ Opposite rules for `{clash['signature']}`:")
        for source in clash["sources"]:
            lines.append(f"    - {source}")

    memory = report["memory_hygiene"]
    lines.extend(
        [
            "",
            "## 5. Memory & Context Hygiene",
            (
                f"- Memory entries: {memory['memory_entries']} "
                f"(expired: {memory['expired_memory_entries']})"
            ),
            (
                f"- User profile entries: {memory['user_entries']} "
                f"(expired: {memory['expired_user_entries']})"
            ),
            f"- Invalid expiration tags: {memory['invalid_expiration_entries']}",
            f"- Pending memory proposals: {memory['pending_memory_proposals']}",
            "",
            "## 6. Background Automation",
            (
                "- Unresolved latest job failures: "
                f"{report['job_runs_audit']['failed_runs']}/"
                f"{report['job_runs_audit']['latest_jobs']}"
            ),
        ]
    )
    for failure in report["job_runs_audit"]["recent_failures"]:
        lines.append(
            f"  - 🔴 [{failure.get('job')} at {failure.get('ts')}]: "
            f"{failure.get('error')}"
        )

    if report["scan_errors"]:
        lines.extend(["", "## Scan Errors"])
        for error in report["scan_errors"]:
            lines.append(f"- ❌ {error['message']} (`{error['path']}`)")

    return "\n".join(lines)
