"""Deterministic, reversible review workflow for vault notes.

This module deliberately keeps review state outside the search index.  The
ledger is append-only JSONL and the readable queue is a projection of it.  A
candidate is identified by workspace, vault-relative path, and content hash,
so editing a note cannot accidentally inherit an old destructive decision.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import tempfile
from dataclasses import dataclass, asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from ciao.vault_index import scan_vault
from ciao.vault_lint import run_validation

RETENTION_DAYS = 30
MAX_CANDIDATES = 5
REVIEW_STATUSES = frozenset({"candidate", "reviewed", "archived", "trashed", "deleted"})
# No ``archive``: nothing here moves or marks an archived note, so accepting it
# wrote a ledger row, left the note exactly where it was, and then suppressed
# the candidate for good — a decision the caller was told had been carried out.
# Archiving a note is an ordinary vault edit; the review workflow owns only the
# reversible trash and the attended permanent deletion.
DISPOSITIONS = frozenset({"keep", "improve_link", "defer", "trash", "restore", "delete"})
DECISION_DISPOSITIONS = frozenset({"keep", "improve_link", "defer"})
_DEFER_RE = re.compile(r"\b(?:superseded|deprecated|obsolete|replaced by|moved to)\b", re.I)
_CANDIDATE_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_QUEUE_LOCKS: dict[tuple[Path, str], threading.Lock] = {}
_QUEUE_LOCKS_GUARD = threading.Lock()


def _workspace_dir(root: Path) -> Path:
    path = Path(root) / "Workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ledger_path(root: Path) -> Path:
    return Path(root) / "Workspace" / "Vault-Review.jsonl"


def queue_path(root: Path) -> Path:
    return Path(root) / "Workspace" / "Vault-Review.md"


def trash_dir(root: Path) -> Path:
    return Path(root) / "Workspace" / ".vault-trash"


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
    _workspace_dir(root)
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


def _suppressed(decision: dict[str, Any], now: datetime) -> bool:
    if decision.get("content_hash") == "":
        return False
    disposition = decision.get("disposition")
    if disposition in {"keep", "trash", "delete"}:
        return True
    if disposition != "defer":
        return False
    try:
        deadline = datetime.fromisoformat(str(decision.get("deferred_until", "")).replace("Z", "+00:00"))
    except ValueError:
        return False
    return now < deadline


def _write_queue(root: Path, candidates: list[ReviewCandidate], decisions: dict[str, dict[str, Any]]) -> None:
    _workspace_dir(root)
    lines = ["# Vault Review", "", "Pending note-review candidates. This file is generated from the append-only ledger.", ""]
    for item in candidates:
        decision = decisions.get(item.candidate_id, {})
        if decision.get("content_hash") == item.content_hash and _suppressed(decision, datetime.now(UTC)):
            continue
        reason = ", ".join(item.signals) or "weak provenance"
        lines.append(f"- `{item.path}` [{item.priority}] {reason} (candidate `{item.candidate_id}`)")
    destination = queue_path(root)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=destination.parent, delete=False
    ) as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, destination)


def _queue_lock(root: Path, workspace: str) -> threading.Lock:
    key = (root, workspace)
    with _QUEUE_LOCKS_GUARD:
        return _QUEUE_LOCKS.setdefault(key, threading.Lock())


def _generate_candidates(
    root: Path,
    *,
    workspace: str,
    max_candidates: int = MAX_CANDIDATES,
    now: datetime | None = None,
    write_queue: bool = True,
) -> list[ReviewCandidate]:
    """Generate explainable candidates without changing vault notes.

    ``write_queue`` refreshes the readable ``Workspace/Vault-Review.md``
    projection. Callers that promise to be read-only — the ``list``/``inspect``
    actions and ``GET /api/vault/review`` — pass ``False``: a listing that
    writes to the vault is a listing that cannot be trusted to be one.
    """
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
    # ``entry.related`` already carries both the frontmatter refs and the body's
    # markdown links — `scan_vault` extends it with `_extract_body_links` — so
    # one pass over it is the whole graph. An earlier revision re-read every
    # note to walk `_links_in` as well; that loop compared extension-less refs
    # against `.md`-suffixed keys, so it never matched, and only cost a second
    # full read of the vault.
    for entry in entries:
        source = str(entry.path)
        for target in entry.related:
            target_path = str(target)
            if target_path in incoming:
                outbound[source].append(target_path)
                incoming[target_path].append(source)
    today = (now or datetime.now(UTC)).date()
    candidates: list[ReviewCandidate] = []
    for entry in entries:
        path = str(entry.path)
        if any(part.casefold() == "workspace" for part in Path(path).parts):
            continue
        try:
            disk_path = root / Path(path).relative_to("memory-vault")
            raw = disk_path.read_bytes()
        except (OSError, ValueError):
            continue
        signals: list[str] = []
        if path in orphans and not entry.related:
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
        # Connectedness makes a note LESS disposable, so both connectedness
        # terms subtract. An earlier revision added +2 for `bridge` while
        # subtracting for backlinks: the two rules contradicted, and a hub with
        # four outbound links and no backlinks outranked genuine orphans for
        # the five candidate slots of a workflow whose terminal action is
        # deletion.
        priority = len(signals) - min(len(backlinks), 2) - (1 if evidence["bridge"] else 0)
        item = ReviewCandidate(
            candidate_id=candidate_id(workspace, path, digest), workspace=workspace,
            path=path, content_hash=digest, signals=tuple(sorted(signals)),
            priority=priority, evidence=evidence,
        )
        candidates.append(item)
    candidates.sort(key=lambda item: (-item.priority, item.path))
    decisions = _latest_decisions(root)
    active = [item for item in candidates if not _suppressed(decisions.get(item.candidate_id, {}), now or datetime.now(UTC)) or decisions.get(item.candidate_id, {}).get("content_hash") != item.content_hash]
    result = active[: max(1, min(int(max_candidates), 50))]
    if write_queue:
        _write_queue(root, result, decisions)
    return result


def generate_candidates(
    root: Path,
    *,
    workspace: str,
    max_candidates: int = MAX_CANDIDATES,
    now: datetime | None = None,
    write_queue: bool = True,
) -> list[ReviewCandidate]:
    """Generate candidates with one consistent snapshot per workspace."""
    root = Path(root).resolve()
    with _queue_lock(root, workspace):
        return _generate_candidates(
            root,
            workspace=workspace,
            max_candidates=max_candidates,
            now=now,
            write_queue=write_queue,
        )


def record_decision(root: Path, candidate: ReviewCandidate, disposition: str, *, actor: str = "user", defer_days: int = 7) -> dict[str, Any]:
    if disposition not in DECISION_DISPOSITIONS:
        raise ValueError(f"unsupported vault review disposition: {disposition}")
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
    metadata = {"candidate_id": candidate.candidate_id, "workspace": candidate.workspace, "original_path": candidate.path, "content_hash": candidate.content_hash, "edited_backlinks": [], "trashed_at": _now()}
    try:
        shutil.move(str(source), str(destination))
        destination.with_suffix(".json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _append(root, {**metadata, "path": candidate.path, "disposition": "trash", "status": "trashed", "actor": actor})
    except OSError as exc:
        if destination.is_file() and not source.exists():
            shutil.move(str(destination), str(source))
        destination.with_suffix(".json").unlink(missing_ok=True)
        raise ValueError(f"trash failed; note was restored: {exc}") from exc
    return metadata


def restore_note(root: Path, candidate_id_value: str, *, actor: str = "user") -> dict[str, Any]:
    _validate_candidate_id(candidate_id_value)
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
    try:
        # Keep the trash copy and metadata available until the restore audit is
        # durable; otherwise a read-only ledger could strand an untracked move.
        _append(root, {**metadata, "disposition": "restore", "status": "reviewed", "actor": actor})
    except OSError as exc:
        shutil.move(str(destination), str(source))
        raise ValueError(f"restore audit failed; note remains trashed: {exc}") from exc
    try:
        metadata_path.unlink()
    except OSError as exc:
        if destination.is_file() and not source.exists():
            shutil.move(str(destination), str(source))
        raise ValueError(f"restore metadata cleanup failed; note remains trashed: {exc}") from exc
    return metadata


def list_trashed(root: Path, *, workspace: str) -> list[dict[str, Any]]:
    """Read-only inventory of the reversible trash for one workspace.

    The trash view renders from this rather than from candidate generation:
    a trashed note is no longer in the vault, so it can never be a
    candidate again, and its only durable record is the ``.json`` sidecar
    `trash_note` wrote next to it. Scoped to one workspace; anything that
    is not a well-formed sidecar for a still-restorable note is skipped.
    """
    directory = trash_dir(root)
    if not directory.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for metadata_path in sorted(directory.glob("*.json")):
        if not _CANDIDATE_ID_RE.fullmatch(metadata_path.stem):
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(metadata, dict):
            continue
        if str(metadata.get("workspace") or "") != workspace:
            continue
        if str(metadata.get("candidate_id") or "") != metadata_path.stem:
            continue
        if not (directory / f"{metadata_path.stem}.md").is_file():
            continue
        items.append(
            {
                "candidate_id": metadata_path.stem,
                "workspace": workspace,
                "original_path": str(metadata.get("original_path") or ""),
                "content_hash": str(metadata.get("content_hash") or ""),
                "trashed_at": str(metadata.get("trashed_at") or ""),
            }
        )
    items.sort(key=lambda item: item["trashed_at"])
    return items


def delete_permanently(root: Path, candidate_id_value: str, *, confirm: str, actor: str = "user") -> dict[str, Any]:
    _validate_candidate_id(candidate_id_value)
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
    original = (Path(root).resolve() / Path(str(metadata["original_path"]).replace("memory-vault/", "", 1))).resolve()
    if not original.is_relative_to(Path(root).resolve()) or original.exists():
        raise ValueError("original note path is unavailable; refusing permanent deletion")
    original.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(source), str(original))
    except OSError as exc:
        raise ValueError(f"cannot delete: {exc}") from exc
    # Probe the target directory before any backlink is rewritten.
    # `strip_references` has to run while the note is still on disk — it
    # resolves refs against the vault's real files — so the rewrites are
    # committed before the unlink that ends the delete. Without the probe an
    # unwritable folder failed only at that unlink, by which point every live
    # link to the note had been dissolved: edits the rollback below cannot undo.
    # Same probe as the Memory Map's delete route.
    try:
        with tempfile.NamedTemporaryFile(dir=original.parent, prefix=".ciao-delete-", suffix=".probe"):
            pass
    except OSError as exc:
        shutil.move(str(original), str(source))
        raise ValueError(f"cannot delete: {exc}") from exc
    from ciao.vault_index import strip_references

    edited: list[str] = []
    undo: dict[str, str] = {}
    cleanup_error = ""
    try:
        edited = strip_references(
            Path(root), str(metadata["original_path"]), undo=undo
        )
    except OSError as exc:
        cleanup_error = str(exc)
    if cleanup_error:
        # The note is back where it was and nothing else was rewritten
        # (`strip_references` is all-or-nothing); leave it in the trash.
        shutil.move(str(original), str(source))
        raise ValueError(f"backlink cleanup failed: {cleanup_error}")
    try:
        # Keep the trash metadata until the completion audit is durable. If the
        # append fails, the note can still be restored from this recovery state.
        _append(root, {**metadata, "edited_backlinks": edited, "disposition": "delete", "status": "deleted", "actor": actor, "deleted_at": _now()})
    except OSError as exc:
        if original.is_file() and not source.exists():
            for path, text in undo.items():
                Path(path).write_text(text, encoding="utf-8")
            shutil.move(str(original), str(source))
        raise ValueError(f"delete audit failed; recovery metadata was retained: {exc}") from exc
    try:
        original.unlink()
    except OSError as exc:
        if original.is_file() and not source.exists():
            for path, text in undo.items():
                Path(path).write_text(text, encoding="utf-8")
            shutil.move(str(original), str(source))
        _append(root, {
            **metadata, "edited_backlinks": edited, "disposition": "delete",
            "status": "trashed", "actor": actor, "delete_failed_at": _now(),
            "delete_error": str(exc),
        })
        raise ValueError(f"cannot delete: {exc}") from exc
    metadata_path.unlink()
    return metadata


def _validate_candidate_id(value: str) -> None:
    if not _CANDIDATE_ID_RE.fullmatch(value):
        raise ValueError("invalid candidate id")
