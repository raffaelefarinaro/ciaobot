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
from ciao.memory_audit import audit_entries
from ciao.memory_injector import expiration_tag_error, is_entry_expired
from ciao.memory_tool import (
    DEFAULT_MEMORY_CHAR_LIMIT,
    DEFAULT_USER_CHAR_LIMIT,
    MAX_ENTRY_CHARS,
    REGIONS,
    contains_invisible_unicode,
    read_region,
    region_usage,
    strip_region_blocks,
)
# Proposal kinds and bullet shape are owned by ciao.proposal_kinds; re-export
# here so this counter stays in sync with the control plane and the web layer.
from ciao.proposal_kinds import BULLET_RE
from ciao.vault_lint import (
    _markdown_source_paths,
    run_validation as run_vault_validation,
)

logger = logging.getLogger(__name__)

SKILL_MAX_BYTES = 15 * 1024

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


def _per_workspace_vault_paths(
    vault_root: Path,
    relative: str,
    *,
    error_type: str,
    what: str,
    workspace: str = "",
) -> tuple[list[Path], list[dict[str, str]]]:
    """Resolve ``<vault>/<relative>`` plus the same file in each child vault.

    Every workspace keeps its own vault directory under *vault_root*, so a
    per-workspace file exists once at the root and once per child.

    ``workspace`` narrows the scan to one logical workspace. Without it a single
    audit reports every workspace's findings, and the routine that runs it lands
    in one chat — so work-vault findings surfaced in a personal chat, the same
    cross-workspace disclosure the entity index fails closed on.
    """
    candidates = [vault_root / relative]
    errors: list[dict[str, str]] = []
    if workspace:
        # Named directly rather than discovered: a scoped audit must not depend
        # on what happens to be sitting in the vault root.
        return [vault_root / workspace / relative], errors
    if vault_root.is_dir():
        try:
            children = sorted(vault_root.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            children = []
            errors.append(
                _diagnostic(error_type, vault_root, f"failed to discover {what}: {exc}")
            )
        for child in children:
            # `iterdir` sees Logs/, Templates/ and any stray folder as a
            # workspace. Harmless while the candidate file does not exist, but
            # it is a guess; a scoped run above never makes it.
            if child.is_dir():
                candidates.append(child / relative)
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        lexical = path.absolute()
        if lexical not in seen:
            seen.add(lexical)
            unique.append(path)
    return unique, errors


def audit_rules(
    workspace_dir: Path,
    vault_root: Path | None = None,
    config: Any | None = None,
    workspace_name: str = "",
) -> dict[str, Any]:
    """Find exact cross-file overlaps and obvious opposite-polarity rules."""
    occurrences: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen_files: set[Path] = set()
    aliases_skipped: list[dict[str, str]] = []

    def add_occurrences(label: str, rules: list[str], *, overlap_eligible: bool) -> None:
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
                    "overlap_eligible": overlap_eligible,
                }
            )

    def read_source_text(label: str, path: Path) -> str | None:
        if not path.exists():
            return None
        if not path.is_file():
            errors.append(
                _diagnostic("invalid_rule_source", path, f"{label} is not a file")
            )
            return None
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
            return None
        if resolved in seen_files:
            aliases_skipped.append({"source": label, "target": str(resolved)})
            return None
        seen_files.add(resolved)
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(
                _diagnostic(
                    "unreadable_rule_source",
                    path,
                    f"failed to read {label}: {exc}",
                )
            )
            return None

    # 1. Guide bodies (CLAUDE.md, and AGENTS.md when it is a distinct file
    # rather than a symlink/alias of CLAUDE.md), excluding region bodies.
    guide_path = workspace_dir / "CLAUDE.md"
    for label, path in (("CLAUDE.md", guide_path), ("AGENTS.md", workspace_dir / "AGENTS.md")):
        text = read_source_text(label, path)
        if text is not None:
            add_occurrences(
                label, _guide_rules(strip_region_blocks(text)), overlap_eligible=True
            )

    # 2. Each bounded-memory region as its own source.
    for region in REGIONS:
        entries, _diags = read_region(guide_path, region)
        rules = [entry for entry in entries if len(entry) > 15]
        add_occurrences(f"ciao:{region}", rules, overlap_eligible=True)

    # 3. Workspace MEMORY.md files. These are large and noisy, so they only
    # feed opposite-polarity clash detection, not exact-overlap detection.
    memory_md_paths: list[Path] = []
    if config is not None:
        # The registry knows which workspaces exist and where each keeps its
        # MEMORY.md; prefer it over guessing from the vault layout. Imported
        # here because the web layer imports this module back.
        from ciao.web.agent_assets import _workspace_memory_paths

        vault_for_paths = vault_root or Path(
            getattr(config, "vault_root", workspace_dir / "memory-vault")
        )
        try:
            memory_md_paths = [
                path
                for path, _title in _workspace_memory_paths(
                    config, workspace_dir, vault_for_paths
                )
            ]
        except Exception as exc:  # noqa: BLE001 — advisory source discovery
            errors.append(
                _diagnostic(
                    "unreadable_memory_source",
                    workspace_dir,
                    f"failed to discover workspace MEMORY.md files: {exc}",
                )
            )
    elif vault_root is not None:
        memory_md_paths, vault_md_errors = _per_workspace_vault_paths(
            vault_root,
            "MEMORY.md",
            error_type="unreadable_vault_root",
            what="workspace MEMORY.md files",
            workspace=workspace_name,
        )
        errors.extend(vault_md_errors)

    for path in memory_md_paths:
        text = read_source_text(f"MEMORY.md ({path})", path)
        if text is not None:
            add_occurrences(f"MEMORY.md ({path})", _guide_rules(text), overlap_eligible=False)

    overlap_occurrences = [o for o in occurrences if o["overlap_eligible"]]
    by_normalized: dict[str, list[dict[str, Any]]] = {}
    for occurrence in overlap_occurrences:
        by_normalized.setdefault(occurrence["normalized"], []).append(occurrence)

    by_signature: dict[str, list[dict[str, Any]]] = {}
    for occurrence in occurrences:
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


def _proposal_paths(
    vault_root: Path,
    workspace: str = "",
) -> tuple[list[Path], list[dict[str, str]]]:
    return _per_workspace_vault_paths(
        vault_root,
        "Workspace/Memory-Proposals.md",
        error_type="unreadable_proposal_root",
        what="workspace proposal queues",
        workspace=workspace,
    )


def audit_memory(
    *,
    guide_path: Path | None = None,
    vault_root: Path | None = None,
    proposal_paths: list[Path] | None = None,
    today: datetime.date | None = None,
    memory_char_limit: int = DEFAULT_MEMORY_CHAR_LIMIT,
    user_char_limit: int = DEFAULT_USER_CHAR_LIMIT,
    workspace_dir: Path | None = None,
    workspace_name: str = "",
) -> dict[str, Any]:
    """Audit bounded memory regions and proposal queues.

    Covers both the mechanical health of the regions (caps, expiry, exact
    duplicates, invisible Unicode) and, via :mod:`ciao.memory_audit`, whether
    their content has rotted: chat events stored as state, cited paths that no
    longer exist, and one subject carrying two competing values.

    Bounded memory lives in the ``ciao:memory``/``ciao:profile`` fenced
    regions inside ``guide_path`` (the workspace ``CLAUDE.md``).
    """
    guide = guide_path or (Path.cwd() / "CLAUDE.md")
    workspace = workspace_dir or guide.parent
    current = today or datetime.date.today()
    region_limits = {"memory": memory_char_limit, "profile": user_char_limit}

    region_entries: dict[str, list[str]] = {}
    expired_by_region: dict[str, list[str]] = {}
    marker_errors: list[dict[str, str]] = []
    over_cap: list[dict[str, Any]] = []
    oversize_entries: list[dict[str, Any]] = []
    duplicate_entries: list[dict[str, Any]] = []
    invisible_unicode: list[dict[str, Any]] = []
    invalid_expirations: list[dict[str, str]] = []

    for region in REGIONS:
        entries, diagnostics = read_region(guide, region)
        region_entries[region] = entries
        for diag in diagnostics:
            marker_errors.append(
                {"region": diag.region, "code": diag.code, "message": diag.message}
            )

        limit = region_limits[region]
        usage = region_usage(entries, limit)
        if usage["used_chars"] > limit:
            over_cap.append(
                {"region": region, "used": usage["used_chars"], "limit": limit}
            )

        seen_counts: dict[str, int] = {}
        for entry in entries:
            if len(entry) > MAX_ENTRY_CHARS:
                oversize_entries.append(
                    {"region": region, "entry": entry[:160], "chars": len(entry)}
                )
            if contains_invisible_unicode(entry):
                invisible_unicode.append({"region": region, "entry": entry[:160]})
            key = _normalized_rule(entry)
            seen_counts[key] = seen_counts.get(key, 0) + 1
            if seen_counts[key] > 1:
                duplicate_entries.append({"region": region, "entry": entry[:160]})

        expired_by_region[region] = [
            entry for entry in entries if is_entry_expired(entry, current)
        ]
        for entry in entries:
            error = expiration_tag_error(entry)
            if error:
                invalid_expirations.append(
                    {
                        "target": region,
                        "entry": entry[:160],
                        "message": error,
                    }
                )

    mem_entries = region_entries["memory"]
    profile_entries = region_entries["profile"]
    expired_mem = expired_by_region["memory"]
    expired_profile = expired_by_region["profile"]

    proposals_count = 0
    proposal_files: list[dict[str, Any]] = []
    proposal_errors: list[dict[str, str]] = []
    if vault_root is not None:
        if proposal_paths is None:
            paths, discovery_errors = _proposal_paths(vault_root, workspace_name)
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
                1 for line in content.splitlines() if BULLET_RE.match(line)
            )
            proposals_count += pending
            proposal_files.append({"path": str(path), "pending": pending})

    rot = audit_entries(region_entries, workspace_dir=workspace)

    return {
        "memory_entries": len(mem_entries),
        "profile_entries": len(profile_entries),
        "expired_memory_entries": len(expired_mem),
        "expired_profile_entries": len(expired_profile),
        "invalid_expiration_entries": len(invalid_expirations),
        "invalid_expirations": invalid_expirations,
        "over_cap": over_cap,
        "oversize_entries": oversize_entries,
        "duplicate_entries": duplicate_entries,
        "invisible_unicode": invisible_unicode,
        "marker_errors": marker_errors,
        "pending_memory_proposals": proposals_count,
        "proposal_files": proposal_files,
        **rot,
        "errors": proposal_errors,
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
        rec_ts = _parsed_timestamp(record)
        cur_ts = _parsed_timestamp(current) if current else None
        if current is None or cur_ts is None or (rec_ts is not None and rec_ts > cur_ts):
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
            "orphans": [],
            "duplicates": [],
            "frontmatter_errors": [],
            "broken_markdown_links": [],
            "errors": [],
        }
    errors: list[dict[str, str]] = []
    try:
        for path, _ in _markdown_source_paths(vault_root):
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
            "orphans": [],
            "duplicates": [],
            "frontmatter_errors": [],
            "broken_markdown_links": [],
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


def audit_upgrade_notices(
    config: Any | None, runtime_dir: Path | None = None
) -> dict[str, Any]:
    """Actions an upgrade left for the operator, detected from the install.

    A release note only works if someone reads it and then remembers to act.
    These are the same conditions stated as facts about *this* machine, so the
    PWA can show them and the operator can act without consulting a changelog.

    Each notice carries an interactive Ciaobot-chat remedy. Detected, never
    applied: moving an existing vault can involve conflicts or user-owned
    layout decisions, so a normal chat inspects it and asks before acting.
    """
    notices: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    if config is None:
        return {"notices": notices, "notices_found": 0, "errors": errors}

    # Advisory section: a config that does not expose a workspace registry has
    # nothing to report, and must not turn the whole audit red.
    vault_raw = getattr(config, "vault_root", None)
    workspace_raw = getattr(config, "workspace_root", None)
    lister = getattr(config, "workspace_names", None)
    if vault_raw is None or workspace_raw is None or not callable(lister):
        return {"notices": notices, "notices_found": 0, "errors": errors}
    names = list(lister())

    resolver = getattr(config, "workspace_vault_root", None)
    if not callable(resolver):
        return {"notices": notices, "notices_found": 0, "errors": errors}
    standardizer = getattr(config, "canonical_workspace_vault_root", None)
    if not callable(standardizer):
        return {"notices": notices, "notices_found": 0, "errors": errors}

    for name in names:
        # Compare the registry-resolved path with the standard folder supplied
        # by config. Setup-created whole-vault roots, adopted external folders,
        # and pre-nesting siblings all remain usable until the user approves an
        # interactive migration, but all should receive the same guided path
        # into the standard named folder.
        try:
            actual = Path(resolver(name)).resolve()
            standard = Path(standardizer(name)).resolve()
        except Exception:  # noqa: BLE001 — advisory section
            continue
        if actual != standard and actual.is_dir():
            notices.append({
                "type": "vault_outside_vault_root",
                "workspace": name,
                "detail": (
                    f"Workspace '{name}' keeps its vault at the nonstandard "
                    f"location {actual}; its standard location is {standard}."
                ),
                "remedy": (
                    f"Open a Ciaobot chat in workspace '{name}' and ask it to "
                    f"migrate the vault from {actual} to {standard}. It should "
                    "inspect both locations, ask before resolving conflicts, "
                    "identify which files are vault content when the source also "
                    "contains Ciaobot runtime files, and make a backup before "
                    "moving anything. After confirmation it should move the "
                    "approved content, atomically update the active workspace "
                    "registry to the standard path, and restart Ciaobot as its "
                    "final step. Verify the workspace before removing the backup."
                ),
            })

    # The vault still speaks the retired link dialect. Surfaced, never applied:
    # rewriting a user's own notes is not a decision an upgrade makes on their
    # behalf, and this is the notice that makes the choice visible instead of
    # leaving it in a release note nobody re-reads. Because notices count toward
    # `actionable_count`, the weekly hygiene routine reports it too.
    if runtime_dir is not None:
        try:
            from ciao.vault_migrate_links import has_unmigrated_links, read_receipt

            if read_receipt(runtime_dir) is None:
                example = has_unmigrated_links(Path(vault_raw))
                if example:
                    notices.append({
                        "type": "unmigrated_vault_links",
                        "workspace": "",
                        "detail": (
                            "The vault still uses `[[wikilinks]]`, which nothing "
                            "reads any more: they are not graph edges, not "
                            "backlinks, and not clickable in the file viewer. "
                            f"First example: {example}."
                        ),
                        "remedy": (
                            "Preview with `ciao vault-migrate-links` (dry-run by "
                            "default), then apply with "
                            "`ciao vault-migrate-links --apply`. Every rewrite is "
                            "recorded, so `ciao vault-unmigrate-links --apply` "
                            "restores the notes byte for byte."
                        ),
                    })
        except Exception:  # noqa: BLE001 — advisory section, never fail the audit
            logger.exception("upgrade notices: link-dialect check failed")

    return {"notices": notices, "notices_found": len(notices), "errors": errors}


def memory_actionable_count(memory_result: dict[str, Any]) -> int:
    """Findings in a memory report that a user can actually act on.

    One definition, shared by `run_os_audit`'s exit status and the standalone
    `ciao memory-audit` command. They previously each summed their own subset,
    and `memory-audit` omitted `oversize_entries`, `invisible_unicode` and
    `pending_memory_proposals` — so it reported "clean" (exit 0) for a region
    that `ciao os-audit` failed on, and the daily curation routine that reads
    the weaker verdict saw nothing to fix.

    `superseded_state_candidates` is deliberately excluded: it is a judgement
    the user may decline, and a finding that can never be cleared would hold
    the audit at needs_attention until people stop reading it.
    """
    # int() because memory_result is dict[str, Any]: the counts are ints at
    # runtime, but the sum is Any to the type checker.
    return int(
        memory_result["expired_memory_entries"]
        + memory_result["expired_profile_entries"]
        + memory_result["invalid_expiration_entries"]
        + len(memory_result["over_cap"])
        + len(memory_result["oversize_entries"])
        + len(memory_result["duplicate_entries"])
        + len(memory_result["invisible_unicode"])
        + len(memory_result["marker_errors"])
        + memory_result["pending_memory_proposals"]
        # Both concretely fixable: move the entry to Learnings.md, or correct
        # the path.
        + len(memory_result["event_shaped_entries"])
        + len(memory_result["stale_path_entries"])
    )


def run_os_audit(
    workspace_dir: Path | None = None,
    vault_root: Path | None = None,
    runtime_dir: Path | None = None,
    *,
    proposal_paths: list[Path] | None = None,
    today: datetime.date | None = None,
    config: Any | None = None,
    workspace_name: str = "",
) -> dict[str, Any]:
    """Execute a complete AI OS audit pass.

    ``workspace_name`` scopes the per-workspace evidence (that workspace's
    ``MEMORY.md`` and proposal queue) to one logical workspace. The hygiene
    routine runs once per workspace and reports into that workspace's chat, so
    an unscoped audit would surface another workspace's findings there.

    ``config`` is optional for programmatic callers. The CLI and PWA both pass
    the live registry so upgrade notices are consistent across surfaces.
    """
    workspace = (workspace_dir or Path.cwd()).expanduser().resolve()
    vault = (vault_root or (workspace / "memory-vault")).expanduser().resolve()
    runtime = (runtime_dir or (workspace / ".runtime")).expanduser().resolve()
    guide_path = workspace / "CLAUDE.md"
    memory_char_limit = getattr(config, "memory_char_limit", DEFAULT_MEMORY_CHAR_LIMIT)
    user_char_limit = getattr(config, "user_char_limit", DEFAULT_USER_CHAR_LIMIT)

    setup_result = audit_setup(workspace, vault, runtime)
    vault_result = _vault_audit(vault)
    skill_result = audit_skills(workspace)
    rule_result = audit_rules(
        workspace, vault_root=vault, config=config, workspace_name=workspace_name
    )
    memory_result = audit_memory(
        guide_path=guide_path,
        vault_root=vault,
        proposal_paths=proposal_paths,
        today=today,
        memory_char_limit=memory_char_limit,
        user_char_limit=user_char_limit,
        workspace_dir=workspace,
        workspace_name=workspace_name,
    )
    job_result = audit_job_runs(workspace, runtime_dir=runtime)
    upgrade_result = audit_upgrade_notices(config, runtime_dir=runtime)

    collected_errors = [
        *setup_result["errors"],
        *vault_result["errors"],
        *skill_result["errors"],
        *rule_result["errors"],
        *memory_result["errors"],
        *job_result["errors"],
        *upgrade_result["errors"],
    ]
    scan_errors: list[dict[str, str]] = []
    seen_errors: set[tuple[str, ...]] = set()
    key: tuple[str, ...]
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
        + len(vault_result.get("orphans", []))
        + len(vault_result.get("duplicates", []))
        + len(vault_result.get("frontmatter_errors", []))
        + len(vault_result.get("broken_markdown_links", []))
        + len(skill_result["issues"])
        + rule_result["rule_clashes_found"]
        + memory_actionable_count(memory_result)
        + job_result["failed_runs"]
        + upgrade_result["notices_found"]
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
        "upgrade_notices": upgrade_result,
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
        f"- Frontmatter errors: {len(report['vault_hygiene'].get('frontmatter_errors', []))}",
        f"- Broken Markdown links: {len(report['vault_hygiene'].get('broken_markdown_links', []))}",
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
                f"- User profile entries: {memory['profile_entries']} "
                f"(expired: {memory['expired_profile_entries']})"
            ),
            f"- Invalid expiration tags: {memory['invalid_expiration_entries']}",
            f"- Regions over cap: {len(memory['over_cap'])}",
            f"- Oversize entries: {len(memory['oversize_entries'])}",
            f"- Duplicate entries: {len(memory['duplicate_entries'])}",
            f"- Invisible Unicode entries: {len(memory['invisible_unicode'])}",
            f"- Region marker errors: {len(memory['marker_errors'])}",
            f"- Pending memory proposals: {memory['pending_memory_proposals']}",
            (
                "- Event-shaped entries (belong in a log): "
                f"{len(memory.get('event_shaped_entries', []))}"
            ),
            (
                f"- Entries citing a missing path: "
                f"{len(memory.get('stale_path_entries', []))} "
                f"({memory.get('paths_checked', 0)} paths checked, "
                f"{memory.get('paths_unverifiable', 0)} not verifiable here)"
            ),
            (
                "- Informational superseded-state candidates: "
                f"{len(memory.get('superseded_state_candidates', []))}"
            ),
        ]
    )
    for finding in memory.get("event_shaped_entries", [])[:5]:
        lines.append(
            f"  - ⚠️ [{finding['region']}] reads as a chat event "
            f"({', '.join(finding['markers'])}): {finding['entry']}"
        )
    for finding in memory.get("stale_path_entries", [])[:5]:
        lines.append(
            f"  - ⚠️ [{finding['region']}] missing `{finding['path']}`: "
            f"{finding['entry']}"
        )
    for finding in memory.get("superseded_state_candidates", [])[:5]:
        lines.append(
            f"  - ℹ️ [{finding['region']}] {len(finding['entries'])} entries about "
            f"`{finding['subject']}`; check whether one supersedes the other"
        )

    lines.extend(
        [
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

    notices = report.get("upgrade_notices", {}).get("notices", [])
    if notices:
        lines.extend(["", "## Upgrade Actions"])
        for notice in notices:
            # A vault-wide notice has no workspace, and rendering the label
            # unconditionally printed a bare `****:` in front of it.
            scope = str(notice.get("workspace") or "").strip()
            prefix = f"**{scope}**: " if scope else ""
            lines.append(f"- ⚠️ {prefix}{notice['detail']}")
            # The remedy is prose containing its own backticked commands, so
            # wrapping the whole sentence in backticks nested them and broke the
            # code spans it already had.
            lines.append(f"  - Fix: {notice['remedy']}")

    if report["scan_errors"]:
        lines.extend(["", "## Scan Errors"])
        for error in report["scan_errors"]:
            lines.append(f"- ❌ {error['message']} (`{error['path']}`)")

    return "\n".join(lines)
