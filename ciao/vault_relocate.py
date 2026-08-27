"""Move one workspace's vault to its standard folder.

Distinct from ``workspace_reroot``, which re-architects EVERY registered
workspace into its own agent root in one shot. This handles the narrower,
common case the "vault is not in its standard folder" housekeeping card
flags: one workspace's vault sits at a non-standard path — usually because it
was adopted before the per-workspace subfolder convention, or hand-pointed at
an external directory — and needs to move to
``CiaoConfig.canonical_workspace_vault_root(name)``. Only that one
workspace's registry entry is touched.

Two shapes, both handled:

- the vault lives at some other path entirely (an absolute pinned root, or a
  legacy one-segment value outside the vault) — the whole directory is this
  workspace's own content, so it moves as a unit.
- the vault IS the shared vault root itself — a workspace adopted before
  per-workspace subfolders got everything at the top level, alongside other
  workspaces' already-nested folders and vault-wide shared state (Logs,
  Templates, the generated indexes). Each top-level entry is classified
  rather than moving the root itself, and a symlink or an entry this module
  cannot place is left ``unclassified`` — never guessed — so the caller (a
  chat agent, in practice) only has to ask about those, not about the move
  as a whole.

Every move is a ``git mv``, so ``git mv`` in reverse (what :func:`undo` does)
stays a working undo and history follows the file. Refuses before touching
anything if the plan refuses or a tracked file under the source has
uncommitted changes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.workspace_reroot import (
    _run_git,
    _write_registry,
    dirty_tracked_paths,
    ensure_rollback_history,
    registry_file,
)

logger = logging.getLogger(__name__)

RECEIPT_VERSION = 1

# Content that belongs to the shared vault as a whole, not to any one
# workspace. Moving these into a workspace's own folder would misfile shared
# state as that workspace's content. Mirrors workspace_reroot's own
# global/regenerated split, applied one level up: at vault_root's own
# children, not at vault_root/<name>'s.
_SHARED_NAMES: frozenset[str] = frozenset(
    {
        "Logs",
        "Templates",
        ".obsidian",
        "INDEX.md",
        "MEMORY.md",
        "VOCABULARY.md",
        ".DS_Store",
        ".runtime",
    }
)


@dataclass(frozen=True, slots=True)
class RelocationEntry:
    """One top-level name under the shared vault root, and what to do with it."""

    name: str
    action: str  # "move" | "skip" | "unclassified"
    reason: str


@dataclass(slots=True)
class RelocationPlan:
    """What the relocation would do, and why it would refuse. Never writes."""

    workspace: str
    source: str
    destination: str
    whole_directory: bool = False
    entries: list[RelocationEntry] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return bool(self.refusals) or any(
            entry.action == "unclassified" for entry in self.entries
        )

    @property
    def moves(self) -> list[RelocationEntry]:
        return [entry for entry in self.entries if entry.action == "move"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RECEIPT_VERSION,
            "workspace": self.workspace,
            "source": self.source,
            "destination": self.destination,
            "whole_directory": self.whole_directory,
            "entries": [asdict(entry) for entry in self.entries],
            "refusals": list(self.refusals),
            "refused": self.refused,
        }


def plan(config: Any, workspace: str) -> RelocationPlan:
    """Classify the move for one workspace's vault. Read only."""
    try:
        source = Path(config.workspace_vault_root(workspace)).resolve()
        destination = Path(config.canonical_workspace_vault_root(workspace)).resolve()
    except ValueError as exc:
        result = RelocationPlan(workspace=workspace, source="", destination="")
        result.refusals.append(str(exc))
        return result

    result = RelocationPlan(
        workspace=workspace, source=str(source), destination=str(destination)
    )

    if source == destination:
        result.refusals.append("vault is already at its standard location")
        return result
    if source.is_symlink() or destination.is_symlink():
        result.refusals.append("neither the source nor the destination may be a symlink")
        return result
    if not source.is_dir():
        result.refusals.append(f"vault is not a directory: {source}")
        return result
    if destination.is_dir() and any(destination.iterdir()):
        result.refusals.append(
            f"destination already exists and is not empty: {destination}"
        )
        return result

    try:
        vault_root = Path(config.vault_root).resolve()
    except Exception:  # noqa: BLE001 — advisory; a bad config must not crash the plan
        vault_root = None

    if source != vault_root:
        # The whole directory is this workspace's own content — nothing else
        # can live at an arbitrary path the operator pointed this workspace at.
        result.whole_directory = True
        return result

    # source IS the shared vault root: only this workspace's own content lives
    # loose at its top level, alongside other workspaces' already-nested
    # folders and vault-wide shared state.
    try:
        other_workspaces = {name for name in config.workspace_names() if name != workspace}
    except Exception:  # noqa: BLE001 — advisory; a bad config must not crash the plan
        other_workspaces = set()

    for entry in sorted(source.iterdir(), key=lambda p: p.name):
        name = entry.name
        if entry.is_symlink():
            result.entries.append(RelocationEntry(name, "unclassified", "symlink"))
        elif name in other_workspaces:
            result.entries.append(RelocationEntry(name, "skip", "belongs to another workspace"))
        elif name in _SHARED_NAMES:
            result.entries.append(RelocationEntry(name, "skip", "shared across every workspace"))
        else:
            result.entries.append(RelocationEntry(name, "move", "this workspace's own content"))

    return result


def receipt_path(runtime_root: Path, workspace: str) -> Path:
    return Path(runtime_root) / "migration" / f"vault-relocate-{workspace}.json"


def _write_receipt(runtime_root: Path, workspace: str, payload: dict[str, Any]) -> Path:
    """Persist a receipt atomically, keeping any earlier one beside it.

    Its own file, separate from ``workspace_reroot``'s ``workspace-rooting.json``
    receipt: that one gates on and never downgrades a ``status: "migrated"``
    full-install migration, and writing this operation's outcome into it would
    either collide with that state or be silently dropped by its guard.
    """
    path = receipt_path(runtime_root, workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path.replace(path.with_name(f"{path.stem}.{stamp}{path.suffix}"))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _repoint_registry(
    runtime_root: Path, workspace: str, config: Any
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
    """Point one workspace's registry entry at its new vault_root.

    Reads and rewrites the raw list rather than round-tripping through
    ``WorkspaceConfig``, so an unknown key a future release adds survives an
    edit that only means to change one field — same reasoning as
    ``workspace_reroot._rewrite_registry``. Also updates the in-memory
    ``WorkspaceConfig`` on ``config`` so the running process sees the new
    location immediately, without waiting for a reload.
    """
    path = registry_file(runtime_root)
    if not path.is_file():
        return None, None
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None, None
    if not isinstance(entries, list):
        return None, None

    before = json.loads(json.dumps(entries))
    stored = config.stored_workspace_vault_root(workspace)
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("name", "")) == workspace:
            entry["vault_root"] = stored
    _write_registry(runtime_root, entries)

    workspace_config = config.workspace(workspace)
    if workspace_config is not None:
        workspace_config.vault_root = stored
    return before, entries


def apply(
    config: Any,
    workspace: str,
    runtime_root: Path,
    *,
    plan_result: RelocationPlan | None = None,
) -> dict[str, Any]:
    """Move one workspace's vault to its standard folder, and repoint the registry.

    Refuses before touching anything if the plan refuses, or a tracked file
    under the source has uncommitted changes (so ``git mv`` in reverse stays a
    working undo). On a mid-run failure, rolls back every move already made.
    """
    result = plan_result if plan_result is not None else plan(config, workspace)
    payload = result.as_dict()
    payload["recorded_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    if result.refused:
        payload["status"] = "refused"
        payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
        return payload

    install_root = Path(config.workspace_root).resolve()
    history = ensure_rollback_history(install_root)
    payload["git_history"] = history
    if history["status"] in {"no_git_binary", "init_failed", "add_failed", "commit_failed"}:
        payload["status"] = "refused"
        payload["refusals"] = [
            "the install has no git history to roll back to and one could not "
            f"be created ({history.get('error') or history['status']}); every "
            "move is a git mv and git mv in reverse is the undo, so relocating "
            "without it would be unrecoverable"
        ]
        payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
        return payload

    source = Path(result.source)
    destination = Path(result.destination)
    try:
        source_rel = str(source.relative_to(install_root))
    except ValueError:
        source_rel = None
    try:
        dest_rel = str(destination.relative_to(install_root))
    except ValueError:
        dest_rel = None

    moves_to_apply: list[tuple[str, str]] = []
    if result.whole_directory:
        if source_rel is None or dest_rel is None:
            payload["status"] = "refused"
            payload["refusals"] = [
                "the source or destination lies outside the install's git "
                "worktree, so it cannot be moved with git mv; move it by hand"
            ]
            payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
            return payload
        moves_to_apply.append((source_rel, dest_rel))
    else:
        if source_rel is None or dest_rel is None:
            payload["status"] = "refused"
            payload["refusals"] = [
                "the vault root lies outside the install's git worktree, so it "
                "cannot be moved with git mv; move it by hand"
            ]
            payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
            return payload
        for entry in result.moves:
            moves_to_apply.append((f"{source_rel}/{entry.name}", f"{dest_rel}/{entry.name}"))

    dirty = dirty_tracked_paths(install_root, *(m[0] for m in moves_to_apply))
    if dirty:
        payload["status"] = "refused"
        first = dirty[0]
        others = f" (and {len(dirty) - 1} more)" if len(dirty) > 1 else ""
        payload["refusals"] = [
            f"{first}{others} has uncommitted changes; commit or stash it so "
            "git mv in reverse stays a working undo"
        ]
        payload["dirty_tracked"] = dirty[:20]
        payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
        return payload

    code, head = _run_git(install_root, "rev-parse", "HEAD")
    payload["git_head_before"] = head if code == 0 else ""

    if not result.whole_directory:
        destination.mkdir(parents=True, exist_ok=True)

    applied: list[dict[str, str]] = []
    for src_rel, dst_rel in moves_to_apply:
        (install_root / dst_rel).parent.mkdir(parents=True, exist_ok=True)
        code, out = _run_git(install_root, "mv", src_rel, dst_rel)
        if code != 0:
            for done in reversed(applied):
                _run_git(install_root, "mv", done["destination"], done["source"])
            payload["status"] = "refused"
            payload["refusals"] = [f"git mv failed for {src_rel}: {out}"]
            payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
            return payload
        applied.append({"source": src_rel, "destination": dst_rel})

    if not result.whole_directory and source.is_dir() and not any(source.iterdir()):
        try:
            source.rmdir()
        except OSError:
            pass

    registry_before, registry_after = _repoint_registry(runtime_root, workspace, config)
    payload["registry_before"] = registry_before
    payload["registry_after"] = registry_after
    payload["applied"] = applied
    payload["status"] = "relocated"
    payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
    return payload


def undo(config: Any, workspace: str, runtime_root: Path) -> dict[str, Any]:
    """Reverse the last completed relocation for one workspace, exactly.

    CLI only, same as ``workspace_reroot.undo``: there is no housekeeping
    button for this, only a receipt-driven reverse.
    """
    path = receipt_path(runtime_root, workspace)
    if not path.is_file():
        return {"status": "nothing_to_undo", "reason": "no relocation receipt for this workspace"}
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"status": "nothing_to_undo", "reason": "receipt is unreadable"}
    if not isinstance(receipt, dict) or receipt.get("status") != "relocated":
        return {"status": "nothing_to_undo", "reason": "last recorded run did not relocate anything"}

    install_root = Path(config.workspace_root).resolve()
    reversed_moves: list[str] = []
    already: list[str] = []
    for entry in reversed(receipt.get("applied", [])):
        source = install_root / entry["source"]
        destination = install_root / entry["destination"]
        if not destination.exists() and not destination.is_symlink() and (
            source.exists() or source.is_symlink()
        ):
            already.append(entry["source"])
            continue
        source.parent.mkdir(parents=True, exist_ok=True)
        code, out = _run_git(install_root, "mv", entry["destination"], entry["source"])
        if code != 0:
            return {
                "status": "failed",
                "reason": f"git mv failed reversing {entry['destination']}: {out}",
                "reversed": reversed_moves,
                "already_reversed": already,
            }
        reversed_moves.append(entry["source"])
        parent = (install_root / entry["destination"]).parent
        if parent != install_root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()

    before = receipt.get("registry_before")
    if before is not None:
        _write_registry(runtime_root, before)
        workspace_config = config.workspace(workspace)
        if workspace_config is not None:
            for entry in before:
                if isinstance(entry, dict) and str(entry.get("name", "")) == workspace:
                    workspace_config.vault_root = entry.get("vault_root", workspace_config.vault_root)

    path.unlink(missing_ok=True)
    return {"status": "undone", "reversed": reversed_moves, "already_reversed": already}
