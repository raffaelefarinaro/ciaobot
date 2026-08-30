"""Deterministic, reversible review workflow for vault notes.

This module deliberately keeps review state outside the search index.  The
ledger is append-only JSONL and the readable queue is a projection of it.  A
candidate is identified by workspace, vault-relative path, and content hash,
so editing a note cannot accidentally inherit an old destructive decision.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from ciao.vault_index import scan_vault, resolve_vault_link
from ciao.vault_lint import _links_in, run_validation

RETENTION_DAYS = 30
MAX_CANDIDATES = 5
REVIEW_STATUSES = frozenset({"candidate", "reviewed", "archived", "trashed", "deleted"})
DISPOSITIONS = frozenset({"keep", "improve_link", "defer", "archive", "trash", "restore", "delete"})
_DEFER_RE = re.compile(r"\b(?:superseded|deprecated|obsolete|replaced by|moved to)\b", re.I)


def _workspace_dir(root: Path) -> Path:
    path = Path(root) / "Workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ledger_path(root: Path) -> Path:
    return _workspace_dir(root) / "Vault-Review.jsonl"


def queue_path(root: Path) -> Path:
    return _workspace_dir(root) / "Vault-Review.md"


def trash_dir(root: Path) -> Path:
    return _workspace_dir(root) / ".vault-trash"


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def candidate_id(workspace: str, path: str, digest: str) -> str:
    raw = f"{workspace}\0{Path(path).as_posix()}\0{digest}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    candidate_id: str
    workspace: str
    path: str
    content_hash: str
    signals: tuple[str, ...]
    priority: int
    evidence: dict[str, Any]
    status: str = "candidate"
    disposition: str = ""
    deferred_until: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _append(root: Path, payload: dict[str, Any]) -> None:
    path = ledger_path(root)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": _now(), **payload}, sort_keys=True) + "\n")


def read_ledger(root: Path) -> list[dict[str, Any]]:
    path = ledger_path(root)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _latest_decisions(root: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in read_ledger(root):
        if row.get("candidate_id"):
            latest[str(row["candidate_id"])] = row
    return latest


def _write_queue(root: Path, candidates: list[ReviewCandidate], decisions: dict[str, dict[str, Any]]) -> None:
    lines = ["# Vault Review", "", "Pending note-review candidates. This file is generated from the append-only ledger.", ""]
    for item in candidates:
        decision = decisions.get(item.candidate_id, {})
        if decision.get("disposition") in {"keep", "archive", "delete"} and decision.get("content_hash") == item.content_hash:
            continue
        reason = ", ".join(item.signals) or "weak provenance"
        lines.append(f"- `{item.path}` [{item.priority}] {reason} (candidate `{item.candidate_id}`)")
    queue_path(root).write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_candidates(
    root: Path,
    *,
    workspace: str,
    max_candidates: int = MAX_CANDIDATES,
    now: datetime | None = None,
) -> list[ReviewCandidate]:
    """Generate explainable candidates without changing vault notes."""
    root = Path(root).resolve()
    entries = scan_vault(root, workspace=workspace)
    validation = run_validation(root)
    # The linter reports paths relative to the vault root, while Entry IDs are
    # rendered in the shared ``memory-vault/`` namespace.
    def rendered(value: str) -> str:
        return value if value.startswith("memory-vault/") else f"memory-vault/{value}"

    orphans = {rendered(path) for path in validation.get("orphans", [])}
    duplicate_groups = [[rendered(path) for path in group] for group in validation.get("duplicates", [])]
    duplicate_by_path = {path: group for group in duplicate_groups for path in group}
    incoming: dict[str, list[str]] = {str(entry.path): [] for entry in entries}
    outbound: dict[str, list[str]] = {str(entry.path): [] for entry in entries}
    for entry in entries:
        source = str(entry.path)
        try:
            text = (root / Path(source).relative_to("memory-vault")).read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        for ref in _links_in(text):
            target = resolve_vault_link(entry.path, ref)
            if target in incoming:
                outbound[source].append(str(target))
                incoming[str(target)].append(source)
    today = (now or datetime.now(UTC)).date()
    candidates: list[ReviewCandidate] = []
    for entry in entries:
        path = str(entry.path)
        if "Workspace/" in path or path.endswith("/Workspace"):
            continue
        try:
            disk_path = root / Path(path).relative_to("memory-vault")
            raw = disk_path.read_bytes()
        except (OSError, ValueError):
            continue
        signals: list[str] = []
        if path in orphans:
            signals.append("unlinked")
        group = duplicate_by_path.get(path)
        if group:
            signals.append("possible_duplicate")
        if _DEFER_RE.search(raw.decode("utf-8", errors="replace")):
            signals.append("superseded_language")
        if not (entry.updated or entry.tags or entry.aliases):
            signals.append("weak_provenance")
        if not signals:
            continue
        digest = content_hash(raw)
        evidence = {
            "backlinks": sorted(incoming.get(path, [])),
            "outbound_links": sorted(outbound.get(path, [])),
            "bridge": len(incoming.get(path, [])) + len(outbound.get(path, [])) >= 4,
            "duplicate_group": group or [],
            "last_update": entry.updated or "",
            "type": entry.type or "note",
            "age_days": None,
        }
        if entry.updated:
            try:
                evidence["age_days"] = max(0, (today - datetime.fromisoformat(entry.updated).date()).days)
            except ValueError:
                pass
        backlinks = cast(list[str], evidence["backlinks"])
        priority = len(signals) + (2 if evidence["bridge"] else 0) - min(len(backlinks), 2)
        item = ReviewCandidate(
            candidate_id=candidate_id(workspace, path, digest), workspace=workspace,
            path=path, content_hash=digest, signals=tuple(sorted(signals)),
            priority=priority, evidence=evidence,
        )
        candidates.append(item)
    candidates.sort(key=lambda item: (-item.priority, item.path))
    decisions = _latest_decisions(root)
    active = [item for item in candidates if decisions.get(item.candidate_id, {}).get("disposition") != "keep" or decisions.get(item.candidate_id, {}).get("content_hash") != item.content_hash]
    result = active[: max(1, min(int(max_candidates), 50))]
    _write_queue(root, result, decisions)
    return result


def record_decision(root: Path, candidate: ReviewCandidate, disposition: str, *, actor: str = "user", defer_days: int = 7) -> dict[str, Any]:
    if disposition not in DISPOSITIONS:
        raise ValueError(f"unsupported vault review disposition: {disposition}")
    if disposition == "delete":
        raise ValueError("permanent deletion requires delete_permanently")
    row = {"candidate_id": candidate.candidate_id, "workspace": candidate.workspace, "path": candidate.path, "content_hash": candidate.content_hash, "disposition": disposition, "actor": actor, "status": "reviewed", "deferred_until": ""}
    if disposition == "defer":
        row["deferred_until"] = (datetime.now(UTC) + timedelta(days=max(1, defer_days))).isoformat().replace("+00:00", "Z")
    _append(root, row)
    return row


def trash_note(root: Path, candidate: ReviewCandidate, *, actor: str = "user") -> dict[str, Any]:
    """Move one exact note to the reversible workspace trash."""
    root = Path(root).resolve()
    source = (root / Path(candidate.path).relative_to("memory-vault")).resolve()
    if not source.is_relative_to(root):
        raise ValueError("note is outside the vault")
    if not source.is_file() or content_hash(source.read_bytes()) != candidate.content_hash:
        raise ValueError("candidate changed or no longer exists; regenerate the review")
    destination = trash_dir(root) / f"{candidate.candidate_id}.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    metadata = {"candidate_id": candidate.candidate_id, "workspace": candidate.workspace, "original_path": candidate.path, "content_hash": candidate.content_hash, "trashed_at": _now()}
    destination.with_suffix(".json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _append(root, {**metadata, "path": candidate.path, "disposition": "trash", "status": "trashed", "actor": actor})
    return metadata


def restore_note(root: Path, candidate_id_value: str, *, actor: str = "user") -> dict[str, Any]:
    metadata_path = trash_dir(root) / f"{candidate_id_value}.json"
    if not metadata_path.is_file():
        raise ValueError("trashed candidate not found")
    metadata = cast(dict[str, Any], json.loads(metadata_path.read_text(encoding="utf-8")))
    destination = (Path(root).resolve() / Path(str(metadata["original_path"]).replace("memory-vault/", "", 1))).resolve()
    source = trash_dir(root) / f"{candidate_id_value}.md"
    if not destination.is_relative_to(Path(root).resolve()):
        raise ValueError("restore path is outside the vault")
    if not source.is_file() or destination.exists() or content_hash(source.read_bytes()) != metadata["content_hash"]:
        raise ValueError("restore would overwrite data or the trashed note changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    metadata_path.unlink()
    _append(root, {**metadata, "disposition": "restore", "status": "reviewed", "actor": actor})
    return metadata


def delete_permanently(root: Path, candidate_id_value: str, *, confirm: str, actor: str = "user") -> dict[str, Any]:
    if confirm != candidate_id_value:
        raise ValueError("explicit candidate confirmation is required")
    source = trash_dir(root) / f"{candidate_id_value}.md"
    metadata_path = trash_dir(root) / f"{candidate_id_value}.json"
    if not source.is_file() or not metadata_path.is_file():
        raise ValueError("only trashed notes can be permanently deleted")
    metadata = cast(dict[str, Any], json.loads(metadata_path.read_text(encoding="utf-8")))
    digest = content_hash(source.read_bytes())
    if digest != metadata.get("content_hash"):
        raise ValueError("trashed note changed; refusing permanent deletion")
    source.unlink()
    metadata_path.unlink()
    _append(root, {**metadata, "disposition": "delete", "status": "deleted", "actor": actor, "deleted_at": _now()})
    return metadata
