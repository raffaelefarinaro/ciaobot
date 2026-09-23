"""Archive and restore logical workspaces.

Removing a workspace from Settings used to MOVE its projects and notes into the
primary workspace. That broke the one promise workspaces make — separation: a
Work vault ended up inside Personal memory, and nothing could undo it.

A workspace is now archived the way a completed project is:

- It leaves the registry (``.runtime/workspaces.json``), so it disappears from
  the sidebar, the pickers, per-workspace system routines, skill sync, vault
  scans and the proposals queue — every one of them enumerates the registry.
- Its folder is MOVED, byte for byte, into
  ``<install>/.archived-workspaces/<name>-<YYYYMMDD-HHMMSS>/`` with an
  ``archive.json`` beside it recording what a restore needs (the registry
  entry, when, from where, and the user schedules that left with it).
- Nothing is merged anywhere and nothing is deleted.

Where the folder is depends on the install's layout, decided by the same
re-rooting receipt as ``CiaoConfig.agent_root``:

- **Per-root** (fresh and re-rooted installs): each workspace owns
  ``<install>/<name>/`` — its guide, ``.claude/`` assets, skills and
  ``memory-vault/``. The whole agent root moves.
- **Shared** (installs that have not re-rooted): every workspace's notes are a
  folder of one vault, ``<vault>/<name>/``, and the guide, skills and catalog
  are shared by all of them. Only that folder moves; the shared guide's bounded
  memory regions cannot be split per workspace and stay where they are.

The archive root is the same directory in both layouts: beside the agent roots,
outside every registered vault, in the install's own repository and on its
filesystem, so the move is one atomic ``rename``. Shapes this cannot archive
without guessing are REFUSED with a reason instead: a vault outside the install
(its own repository), a workspace whose vault is the whole shared vault, a
legacy vault pinned outside its standard folder, a symlinked folder, or a
folder on another filesystem.

Ownership boundary: this module owns the filesystem move, the archive
metadata, the registry entry and the derived-index cleanup. The route
(``routes_api.archive_workspace_setting``) orchestrates the parts that belong
to live managers — archiving the chats, taking the schedules, skill resync.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.config import WorkspaceConfig
from ciao.vault_index import ARCHIVED_WORKSPACES_DIR

logger = logging.getLogger(__name__)

ARCHIVE_DIR_NAME = ARCHIVED_WORKSPACES_DIR
METADATA_FILE = "archive.json"
METADATA_VERSION = 1

LAYOUT_PER_ROOT = "per-root"
LAYOUT_SHARED = "shared"

_ARCHIVE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}-\d{8}-\d{6}(?:-\d{1,3})?$")


class WorkspaceArchiveError(Exception):
    """An archive or restore that was refused; ``status`` is the HTTP code."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True, slots=True)
class ArchiveTarget:
    """What archiving one workspace moves, decided before anything moves."""

    name: str
    layout: str
    # The directory that moves. It may not exist yet — a workspace nobody
    # wrote to — in which case only the registry entry is archived.
    source: Path
    # The workspace's vault, whose search rows are dropped after the move.
    vault: Path


def archive_root(config: Any) -> Path:
    """``<install>/.archived-workspaces`` — the same place in both layouts."""
    return Path(config.workspace_root) / ARCHIVE_DIR_NAME


def _rerooted(config: Any) -> bool:
    check = getattr(config, "_rerooted", None)
    return bool(check()) if callable(check) else False


def _display(config: Any, path: Path) -> str:
    try:
        return str(path.relative_to(Path(config.workspace_root)))
    except ValueError:
        return str(path)


def plan_archive(config: Any, name: str) -> ArchiveTarget:
    """Validate that *name* can be archived and say what would move.

    Never writes. Raises :class:`WorkspaceArchiveError` with a message the
    operator can act on.
    """
    if name not in config.workspaces:
        raise WorkspaceArchiveError("workspace not found", 404)
    if len(config.workspaces) <= 1:
        raise WorkspaceArchiveError("cannot archive the last workspace")
    if config.primary_workspace() == name:
        raise WorkspaceArchiveError("cannot archive the primary workspace")

    install = Path(config.workspace_root)
    try:
        vault = Path(config.workspace_vault_root(name))
    except ValueError as exc:
        raise WorkspaceArchiveError(
            f"'{name}' has an unsafe vault location ({exc}); fix it before archiving",
            409,
        ) from exc

    if _rerooted(config):
        source = Path(config.agent_root(name))
        layout = LAYOUT_PER_ROOT
        if source == install or not vault.is_relative_to(source):
            raise WorkspaceArchiveError(
                f"'{name}' keeps its notes outside its own folder ({vault}); "
                "move them into it before archiving",
                409,
            )
    else:
        layout = LAYOUT_SHARED
        shared = Path(config.vault_root)
        if not shared.is_relative_to(install):
            raise WorkspaceArchiveError(
                f"the vault lives outside the install ({shared}), in its own "
                "repository; archiving would move notes out of it, so archive "
                f"'{name}' by hand",
                409,
            )
        if vault == shared:
            raise WorkspaceArchiveError(
                f"'{name}' uses the whole shared vault, which holds every "
                "workspace's notes; it cannot be archived on its own",
                409,
            )
        if vault.parent != shared:
            raise WorkspaceArchiveError(
                f"'{name}' keeps its notes outside the standard folder "
                f"({vault}); run `ciao vault-relocate {name} --apply` first",
                409,
            )
        source = vault

    if source.is_symlink():
        raise WorkspaceArchiveError(
            f"'{name}' is a symlink ({source}); archive its target by hand", 409
        )
    if source.exists() and not source.is_dir():
        raise WorkspaceArchiveError(f"'{source}' is not a directory", 409)
    if source.exists():
        try:
            same_device = os.stat(source).st_dev == os.stat(install).st_dev
        except OSError as exc:
            raise WorkspaceArchiveError(f"cannot inspect {source}: {exc}", 409) from exc
        if not same_device:
            raise WorkspaceArchiveError(
                f"'{name}' lives on another filesystem than the install; a move "
                "there cannot be atomic, so archive it by hand",
                409,
            )
    return ArchiveTarget(name=name, layout=layout, source=source, vault=vault)


def _registry_entry(config: Any, workspace: WorkspaceConfig) -> dict[str, Any]:
    return {
        "name": workspace.name,
        "vault_root": workspace.vault_root,
        "default_provider": config.default_provider_for_workspace(workspace.name),
        "disallowed_tools": workspace.disallowed_tools,
        "allowed_mcp_servers": workspace.allowed_mcp_servers,
        "gws_profile": workspace.gws_profile,
        "color": workspace.color,
    }


def _new_archive_dir(config: Any, name: str, now: datetime) -> Path:
    root = archive_root(config)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    candidate = root / f"{name}-{stamp}"
    suffix = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = root / f"{name}-{stamp}-{suffix}"
        suffix += 1
    return candidate


def _write_metadata(folder: Path, metadata: dict[str, Any]) -> None:
    path = folder / METADATA_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def move_to_archive(
    config: Any,
    target: ArchiveTarget,
    *,
    schedules: list[dict[str, Any]] | None = None,
    summary: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Move the workspace folder into a fresh archive folder and describe it.

    The metadata is written BEFORE the move (status ``archiving``) so a crash
    between the two leaves a folder that says what it was for, and is
    rewritten after it. A failed rename removes the empty archive folder and
    raises, leaving the workspace exactly where it was.
    """
    workspace = config.workspaces[target.name]
    moment = now or datetime.now(UTC)
    folder = _new_archive_dir(config, target.name, moment)
    folder.mkdir(parents=True)
    moved = target.source.is_dir()
    metadata: dict[str, Any] = {
        "schema_version": METADATA_VERSION,
        "status": "archiving",
        "name": target.name,
        "archived_at": moment.isoformat().replace("+00:00", "Z"),
        "layout": target.layout,
        "original_path": _display(config, target.source),
        "content_dir": target.source.name if moved else "",
        "workspace": _registry_entry(config, workspace),
        "schedules": list(schedules or []),
        "summary": dict(summary or {}),
    }
    _write_metadata(folder, metadata)
    if moved:
        try:
            os.rename(target.source, folder / target.source.name)
        except OSError as exc:
            try:
                (folder / METADATA_FILE).unlink()
                folder.rmdir()
            except OSError:
                logger.warning("Could not clean up %s after a failed archive", folder)
            raise WorkspaceArchiveError(
                f"could not move {target.source} into the archive: {exc}", 500
            ) from exc
    metadata["status"] = "archived"
    _write_metadata(folder, metadata)
    return {**metadata, "id": folder.name, "path": _display(config, folder)}


def unregister(config: Any, name: str) -> None:
    config.workspaces.pop(name, None)
    from ciao.workspaces import persist_workspaces  # noqa: PLC0415

    persist_workspaces(config)


def refresh_shared_index(config: Any) -> bool:
    """Rebuild the shared ``INDEX.md`` on an install that has not re-rooted.

    There one index lists every workspace's notes under a workspace prefix and
    entity hints read it back, so an archived folder's entries must leave it
    now rather than at the next restart. A per-root install needs nothing: the
    archived root's own index moved with it.
    """
    if _rerooted(config):
        return False
    root = Path(config.vault_root)
    if not root.is_dir():
        return False
    from ciao import vault_index  # noqa: PLC0415

    entries = vault_index.scan_vault(root)
    vault_index.write_index_file(entries, root / "INDEX.md")
    return True


def forget_search_rows(config: Any, vault: Path) -> int:
    """Drop the archived vault's rows from the install's search database."""
    from ciao.async_reads import keyed_lock  # noqa: PLC0415
    from ciao.fts_search import (  # noqa: PLC0415
        SEARCH_DB_NAME,
        forget_subtree,
        get_db_path,
        init_db,
        vault_key_prefix,
    )

    runtime_dir = Path(config.state_path).parent
    override = os.environ.get("CIAO_MEMORY_DIR", "").strip()
    expected = (
        Path(override).expanduser() if override else runtime_dir
    ) / SEARCH_DB_NAME
    if not expected.exists():
        return 0
    db_path = get_db_path(runtime_dir)
    prefix = vault_key_prefix(vault, Path(config.workspace_root))
    conn = sqlite3.connect(db_path)
    try:
        with keyed_lock(f"fts-index:{db_path}"):
            init_db(conn)
            return forget_subtree(conn, prefix)
    finally:
        conn.close()


def _read_metadata(folder: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((folder / METADATA_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _workspace_entry(metadata: dict[str, Any]) -> dict[str, Any]:
    entry = metadata.get("workspace")
    return entry if isinstance(entry, dict) else {}


def _taken_name(config: Any, name: str) -> str | None:
    for existing in config.workspace_names():
        if str(existing).casefold() == name.casefold():
            return str(existing)
    return None


def _current_layout(config: Any) -> str:
    return LAYOUT_PER_ROOT if _rerooted(config) else LAYOUT_SHARED


def _restore_destination(config: Any, metadata: dict[str, Any]) -> Path:
    name = str(metadata.get("name") or "")
    if metadata.get("layout") == LAYOUT_PER_ROOT:
        return Path(config.workspace_root) / name
    return Path(config.vault_root) / name


def _restore_blocker(config: Any, metadata: dict[str, Any], folder: Path) -> str:
    """Why this archive cannot be restored right now, or ``""``."""
    name = str(metadata.get("name") or "")
    if metadata.get("status") != "archived":
        return (
            "this archive did not finish; inspect "
            f"{_display(config, folder)} and restore it by hand"
        )
    taken = _taken_name(config, name)
    if taken is not None:
        return f"a workspace named '{taken}' already exists"
    if metadata.get("layout") != _current_layout(config):
        return (
            "archived before this install moved each workspace into its own "
            f"folder; restore it by hand from {_display(config, folder)}"
        )
    content_dir = str(metadata.get("content_dir") or "")
    if content_dir and not (folder / content_dir).is_dir():
        return f"the archived folder is missing from {_display(config, folder)}"
    destination = _restore_destination(config, metadata)
    if content_dir and (destination.exists() or destination.is_symlink()):
        return f"a folder already exists at {_display(config, destination)}"
    return ""


def _archive_folder(config: Any, archive_id: str) -> Path:
    if not _ARCHIVE_ID_RE.fullmatch(archive_id or ""):
        raise WorkspaceArchiveError("invalid archive id")
    folder = archive_root(config) / archive_id
    if folder.is_symlink() or not folder.is_dir():
        raise WorkspaceArchiveError("archived workspace not found", 404)
    return folder


def list_archives(config: Any) -> list[dict[str, Any]]:
    """Every archived workspace, newest first, with whether it can come back."""
    root = archive_root(config)
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for folder in root.iterdir():
        if folder.is_symlink() or not folder.is_dir():
            continue
        if not _ARCHIVE_ID_RE.fullmatch(folder.name):
            continue
        metadata = _read_metadata(folder)
        if metadata is None or not metadata.get("name"):
            continue
        workspace = _workspace_entry(metadata)
        blocker = _restore_blocker(config, metadata, folder)
        schedules = metadata.get("schedules")
        out.append({
            "id": folder.name,
            "name": str(metadata.get("name")),
            "archived_at": str(metadata.get("archived_at") or ""),
            "path": _display(config, folder),
            "layout": str(metadata.get("layout") or ""),
            "color": str(workspace.get("color") or ""),
            "default_provider": str(workspace.get("default_provider") or ""),
            "gws_profile": str(workspace.get("gws_profile") or ""),
            "schedules": len(schedules) if isinstance(schedules, list) else 0,
            "restorable": not blocker,
            "blocked_reason": blocker,
        })
    out.sort(key=lambda item: item["archived_at"], reverse=True)
    return out


def restore_archive(config: Any, archive_id: str) -> dict[str, Any]:
    """Move an archived workspace back and re-register it.

    Refuses when the name is taken, the destination exists, or the archive was
    made on the other layout. Returns the archive metadata; the caller restores
    the schedules it carries and refreshes the live managers. The emptied
    archive folder is removed once the workspace is back.
    """
    folder = _archive_folder(config, archive_id)
    metadata = _read_metadata(folder)
    if metadata is None or not metadata.get("name"):
        raise WorkspaceArchiveError("archive metadata is missing or unreadable", 409)
    blocker = _restore_blocker(config, metadata, folder)
    if blocker:
        raise WorkspaceArchiveError(f"cannot restore: {blocker}", 409)
    name = str(metadata["name"])
    from ciao.workspaces import WORKSPACE_NAME_RE  # noqa: PLC0415

    if not WORKSPACE_NAME_RE.match(name):
        raise WorkspaceArchiveError("archive names an invalid workspace", 409)
    content_dir = str(metadata.get("content_dir") or "")
    destination = _restore_destination(config, metadata)
    if content_dir:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(folder / content_dir, destination)
        except OSError as exc:
            raise WorkspaceArchiveError(
                f"could not move the archive back to {destination}: {exc}", 500
            ) from exc

    entry = _workspace_entry(metadata)
    try:
        stored_root = config.stored_workspace_vault_root(name)
    except ValueError:
        stored_root = name
    config.workspaces[name] = WorkspaceConfig(
        name=name,
        vault_root=str(entry.get("vault_root") or stored_root),
        default_provider=str(entry.get("default_provider") or "claude"),
        disallowed_tools=entry.get("disallowed_tools"),
        allowed_mcp_servers=entry.get("allowed_mcp_servers"),
        gws_profile=str(entry.get("gws_profile") or ""),
        color=str(entry.get("color") or "pink"),
    )
    from ciao.workspaces import persist_workspaces  # noqa: PLC0415

    persist_workspaces(config)
    try:
        (folder / METADATA_FILE).unlink()
        folder.rmdir()
    except OSError:
        # Something else was left in the archive folder. It is not ours to
        # delete; the workspace itself is already back.
        logger.warning("Archive folder %s was not empty after restore", folder)
    return {**metadata, "id": archive_id, "restored_to": _display(config, destination)}
