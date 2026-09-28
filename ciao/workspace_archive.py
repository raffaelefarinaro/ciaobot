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
        if shared != install and (shared / ".git").exists():
            # The vault is inside the install but is its own repository, the
            # one git sync commits and pushes (``local_session.sync_root``).
            # Moving a folder out of it would sync as a deletion to every other
            # device while the archive sat unversioned on this one.
            raise WorkspaceArchiveError(
                f"the vault is its own repository ({shared}); archiving would "
                "move notes out of it and sync that as a deletion, so archive "
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
    rewritten after it. Every filesystem failure raises
    :class:`WorkspaceArchiveError` with the workspace exactly where it was: a
    failed folder creation or first metadata write changes nothing, a failed
    rename removes the empty archive folder, and a failed final metadata write
    moves the folder back first. The caller relies on that to put back the
    schedules it took.
    """
    workspace = config.workspaces[target.name]
    moment = now or datetime.now(UTC)
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
    try:
        folder = _new_archive_dir(config, target.name, moment)
        folder.mkdir(parents=True)
    except OSError as exc:
        raise WorkspaceArchiveError(
            f"could not create the archive folder in {_display(config, archive_root(config))}: {exc}",
            500,
        ) from exc
    try:
        _write_metadata(folder, metadata)
    except OSError as exc:
        _remove_empty_archive_folder(folder)
        raise WorkspaceArchiveError(
            f"could not write {METADATA_FILE} in {_display(config, folder)}: {exc}", 500
        ) from exc
    if moved:
        try:
            os.rename(target.source, folder / target.source.name)
        except OSError as exc:
            _remove_empty_archive_folder(folder)
            raise WorkspaceArchiveError(
                f"could not move {target.source} into the archive: {exc}", 500
            ) from exc
    metadata["status"] = "archived"
    try:
        _write_metadata(folder, metadata)
    except OSError as exc:
        if moved:
            try:
                os.rename(folder / target.source.name, target.source)
            except OSError as back_exc:
                # The folder is in the archive and its metadata still says
                # ``archiving``; say where it is rather than guess.
                raise WorkspaceArchiveError(
                    f"could not finish {METADATA_FILE} in {_display(config, folder)} "
                    f"({exc}), and moving the folder back to {target.source} failed "
                    f"too ({back_exc}); move it back by hand",
                    500,
                ) from exc
        _remove_empty_archive_folder(folder)
        raise WorkspaceArchiveError(
            f"could not write {METADATA_FILE} in {_display(config, folder)}: {exc}; "
            "the workspace was left where it was",
            500,
        ) from exc
    return {**metadata, "id": folder.name, "path": _display(config, folder)}


def _remove_empty_archive_folder(folder: Path) -> None:
    """Undo a fresh archive folder that holds nothing but its own metadata."""
    for leftover in (folder / METADATA_FILE, (folder / METADATA_FILE).with_suffix(".json.tmp")):
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        folder.rmdir()
    except OSError:
        logger.warning("Could not clean up %s after a failed archive", folder)


def unregister(config: Any, name: str) -> None:
    """Drop *name* from the registry, in memory and on disk, or from neither.

    A failed save puts the entry back (in its original position) before
    re-raising, so the running server never disagrees with
    ``workspaces.json`` about which workspaces exist.
    """
    previous = dict(config.workspaces)
    config.workspaces.pop(name, None)
    from ciao.workspaces import persist_workspaces  # noqa: PLC0415

    try:
        persist_workspaces(config)
    except BaseException:
        config.workspaces.clear()
        config.workspaces.update(previous)
        raise


def undo_move_to_archive(
    config: Any, target: ArchiveTarget, archived: dict[str, Any]
) -> None:
    """Put a folder :func:`move_to_archive` moved back where it was.

    For a step after the move that failed while the workspace is still
    registered: the workspace gets its folder back. The archive folder and its
    ``archive.json`` stay: until the caller has put the schedules back in the
    active file, the metadata is their only durable copy. The caller removes
    it with :func:`discard_rolled_back_archive` once they are back, or marks
    it with :func:`mark_rolled_back` when they could not be. Raises
    :class:`WorkspaceArchiveError` when the folder cannot be moved back; the
    archive is then left untouched.
    """
    folder = archive_root(config) / str(archived["id"])
    content_dir = str(archived.get("content_dir") or "")
    if content_dir:
        try:
            os.rename(folder / content_dir, target.source)
        except OSError as exc:
            raise WorkspaceArchiveError(
                f"moving {_display(config, folder / content_dir)} back to "
                f"{target.source} failed ({exc}); move it back by hand",
                500,
            ) from exc


def discard_rolled_back_archive(config: Any, archived: dict[str, Any]) -> None:
    """Remove the emptied archive folder of a rolled-back archive."""
    _remove_empty_archive_folder(archive_root(config) / str(archived["id"]))


def mark_rolled_back(config: Any, archived: dict[str, Any]) -> None:
    """Keep a rolled-back archive whose schedules could not be put back.

    The folder moved back, so ``archive.json`` holds nothing a restore could
    move — only the schedules. Its status changes so it is listed as an
    archive that did not finish (restore by hand) rather than one whose folder
    went missing. Best effort: if even this write fails, the file still says
    ``archived`` and still holds the schedules.
    """
    folder = archive_root(config) / str(archived["id"])
    metadata = _read_metadata(folder)
    if metadata is None:
        return
    metadata["status"] = "rolled-back"
    try:
        _write_metadata(folder, metadata)
    except OSError:
        logger.warning("Could not mark %s as rolled back", folder, exc_info=True)


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
        forget_subtree,
        get_db_path,
        init_db,
        vault_key_prefix,
    )

    db_path = get_db_path(Path(config.state_path).parent)
    if not db_path.exists():
        return 0
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


# ── restoring from untrusted metadata ───────────────────────────────────────
#
# ``.archived-workspaces/`` is in the install's git repository and git sync
# stages it like any note (``git add -A``). It cannot simply be ignored: the
# workspace folder was tracked where it lived, so ignoring its new home would
# make sync push the archive as a deletion to every other device and remote.
# The price is that ``archive.json`` arrives through the same channel as the
# notes, and on an install whose remote someone else can push to, it is input
# from them. A restore therefore takes nothing in it at face value: every
# field is validated with the rules the Settings and Automations routes apply,
# a value that fails falls back to the safe default rather than a wider one,
# and automations come back paused.
#
# No tamper hash is kept in ``.runtime/`` (which git does not sync): an
# archive made on another device and restored here is legitimate and would
# have no local hash, so a missing or mismatched hash could only warn, never
# decide. And the archived folder itself (guide, skills, notes) arrives over
# the same channel, so a hash of ``archive.json`` alone would not make the
# rest of it trustworthy. Validation plus a confirmation that shows what will
# be applied covers both cases.

_MCP_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")
_GWS_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:*()/@-][A-Za-z0-9_.:*()/@ -]{0,199}$")


def _string_list(raw: object, pattern: re.Pattern[str]) -> list[str] | None:
    """``raw`` as a list of strings that all match ``pattern``, else None."""
    if not isinstance(raw, list):
        return None
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not pattern.fullmatch(item.strip()):
            return None
        if item.strip() not in out:
            out.append(item.strip())
    return out


def restored_settings(config: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    """The registry fields a restore applies, validated; never the vault root.

    Same rules as ``workspaces.workspace_from_request``. A value that fails
    them falls back to what grants the least, not to the stored value: an
    unreadable MCP allowlist becomes ``[]`` (reach no server) and an
    unreadable tool deny-list becomes ``None`` (the per-workspace default
    denies). A readable value is kept as it is, which is why the restore
    confirmation shows the allowlist and deny-list before anything happens.
    """
    from ciao import provider_registry  # noqa: PLC0415
    from ciao.config import DEFAULT_WORKSPACE_COLOR, coerce_workspace_color  # noqa: PLC0415

    entry = _workspace_entry(metadata)
    provider = entry.get("default_provider")
    if not isinstance(provider, str) or provider not in provider_registry.provider_ids():
        provider = "claude"
    raw_denied = entry.get("disallowed_tools")
    disallowed = None if raw_denied is None else _string_list(raw_denied, _TOOL_NAME_RE)
    raw_allowed = entry.get("allowed_mcp_servers")
    if raw_allowed is None:
        allowed: list[str] | None = None
    else:
        allowed = _string_list(raw_allowed, _MCP_SERVER_NAME_RE)
        if allowed is None:
            allowed = []
    profile = entry.get("gws_profile")
    if not isinstance(profile, str) or not (
        profile == "" or _GWS_PROFILE_RE.fullmatch(profile)
    ):
        profile = ""
    try:
        color = coerce_workspace_color(entry.get("color"))
    except ValueError:
        color = DEFAULT_WORKSPACE_COLOR
    return {
        "default_provider": provider,
        "disallowed_tools": disallowed,
        "allowed_mcp_servers": allowed,
        "gws_profile": profile,
        "color": color,
    }


def restorable_schedules(
    config: Any,
    metadata: dict[str, Any],
    *,
    foreign_target: Any = None,
) -> tuple[list[dict[str, Any]], int]:
    """``(rebuilt rows, how many were dropped)`` for an archive's schedules.

    Every row is rebuilt by ``schedules.restorable_user_schedule``: validated,
    pinned to this workspace and paused.
    """
    from ciao import provider_registry  # noqa: PLC0415
    from ciao.schedules import restorable_user_schedule  # noqa: PLC0415

    raw = metadata.get("schedules")
    if not isinstance(raw, list):
        return [], 0
    name = str(metadata.get("name") or "")
    providers = set(provider_registry.provider_ids())
    rows: list[dict[str, Any]] = []
    for item in raw:
        row = restorable_user_schedule(
            item, workspace=name, providers=providers, foreign_target=foreign_target
        )
        if row is not None:
            rows.append(row)
    return rows, len(raw) - len(rows)


def _restored_vault_root(
    config: Any, metadata: dict[str, Any], folder: Path
) -> tuple[str, Path]:
    """``(registry value, absolute path)`` of the restored workspace's vault.

    The stored ``vault_root`` must land inside the folder being restored:
    anything else would point the restored workspace, and every chat run in
    it, at notes the archive never held. It is rebuilt as the registry value
    for that location rather than copied. Raises ``ValueError`` with the
    reason when it cannot be.
    """
    install = Path(config.workspace_root).resolve()
    shared = Path(config.vault_root).resolve()
    destination = _restore_destination(config, metadata)
    dest = Path(os.path.normpath(destination.parent.resolve() / destination.name))
    raw = _workspace_entry(metadata).get("vault_root")
    if raw is None or raw == "":
        if metadata.get("layout") == LAYOUT_PER_ROOT:
            candidate = dest / shared.name
        else:
            candidate = dest
    elif not isinstance(raw, str):
        raise ValueError("the archive's vault location is not a path")
    else:
        path = Path(raw.strip())
        if not raw.strip() or ".." in path.parts or "\\" in raw:
            raise ValueError("the archive's vault location is not a safe path")
        if path.is_absolute():
            candidate = Path(os.path.normpath(path))
        elif len(path.parts) == 1:
            candidate = Path(os.path.normpath(shared / path))
        else:
            candidate = Path(os.path.normpath(install / path))
    if candidate != dest and not candidate.is_relative_to(dest):
        raise ValueError(
            "the archive points the workspace's notes outside its own folder "
            f"({_display(config, candidate)})"
        )
    # No symlink on the way down from the restored folder: after the move it
    # would redirect the vault anywhere.
    content_dir = str(metadata.get("content_dir") or "")
    if content_dir:
        # Before the move the tree is in the archive; after it, at ``dest``.
        archived_tree = folder / content_dir
        walk = archived_tree if archived_tree.exists() else dest
        for part in candidate.relative_to(dest).parts:
            walk = walk / part
            if walk.is_symlink():
                raise ValueError(
                    f"the archived notes folder is a symlink ({_display(config, walk)})"
                )
    stored = str(candidate)
    if candidate.is_relative_to(install) and len(candidate.relative_to(install).parts) > 1:
        stored = str(candidate.relative_to(install))
    from ciao.workspaces import vault_root_owner  # noqa: PLC0415

    owner = vault_root_owner(config, candidate)
    if owner is not None and owner != metadata.get("name"):
        raise ValueError(f"its notes folder is already used by '{owner}'")
    return stored, candidate


def _taken_name(config: Any, name: str) -> str | None:
    for existing in config.workspace_names():
        if str(existing).casefold() == name.casefold():
            return str(existing)
    return None


def _current_layout(config: Any) -> str:
    return LAYOUT_PER_ROOT if _rerooted(config) else LAYOUT_SHARED


def _restore_destination(config: Any, metadata: dict[str, Any]) -> Path:
    # The folder goes back under the name it left with, not the workspace name:
    # on the shared layout a workspace's vault folder can be named differently
    # (Settings' free-text "Vault name"), and the restored registry entry still
    # points at that folder.
    folder_name = str(metadata.get("content_dir") or "") or str(metadata.get("name") or "")
    if metadata.get("layout") == LAYOUT_PER_ROOT:
        return Path(config.workspace_root) / folder_name
    return Path(config.vault_root) / folder_name


def _safe_content_dir(content_dir: str) -> bool:
    """Whether ``content_dir`` names one plain entry inside the archive folder.

    ``archive.json`` is a file on disk; an absolute path or ``..`` in it would
    otherwise make a restore move an arbitrary directory into the install.
    """
    return (
        content_dir not in {".", ".."}
        and "/" not in content_dir
        and "\\" not in content_dir
        and Path(content_dir).name == content_dir
    )


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
    if content_dir and not _safe_content_dir(content_dir):
        return f"the archive metadata in {_display(config, folder)} names an unsafe folder"
    if content_dir and (folder / content_dir).is_symlink():
        return f"the archived folder in {_display(config, folder)} is a symlink"
    if content_dir and not (folder / content_dir).is_dir():
        return f"the archived folder is missing from {_display(config, folder)}"
    destination = _restore_destination(config, metadata)
    if content_dir and (destination.exists() or destination.is_symlink()):
        return f"a folder already exists at {_display(config, destination)}"
    try:
        _restored_vault_root(config, metadata, folder)
    except ValueError as exc:
        return str(exc)
    return ""


def _registration_conflict(config: Any, metadata: dict[str, Any], folder: Path) -> str:
    """Why the restored entry cannot be registered now, or ``""``.

    Rechecked on the event loop right before the registry is mutated: the
    folder move ran in a worker thread, and a workspace created meanwhile
    under the same name (or over the same notes folder) must not be
    overwritten by the archived settings.
    """
    name = str(metadata.get("name") or "")
    taken = _taken_name(config, name)
    if taken is not None:
        return f"a workspace named '{taken}' was created while restoring"
    try:
        _restored_vault_root(config, metadata, folder)
    except ValueError as exc:
        return str(exc)
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
        blocker = _restore_blocker(config, metadata, folder)
        # What a restore would apply, after validation: the confirmation shows
        # it, because the metadata may have been edited on another device.
        settings = restored_settings(config, metadata)
        schedules, dropped = restorable_schedules(config, metadata)
        out.append({
            "id": folder.name,
            "name": str(metadata.get("name")),
            "archived_at": str(metadata.get("archived_at") or ""),
            "path": _display(config, folder),
            "layout": str(metadata.get("layout") or ""),
            "color": settings["color"],
            "default_provider": settings["default_provider"],
            "gws_profile": settings["gws_profile"],
            "disallowed_tools": settings["disallowed_tools"],
            "allowed_mcp_servers": settings["allowed_mcp_servers"],
            "schedules": len(schedules),
            "schedules_dropped": dropped,
            "restorable": not blocker,
            "blocked_reason": blocker,
        })
    out.sort(key=lambda item: item["archived_at"], reverse=True)
    return out


def move_back(config: Any, archive_id: str) -> tuple[Path, dict[str, Any], Path]:
    """Validate an archive and move its folder back; never touches the registry.

    Returns ``(archive folder, metadata, destination)`` for
    :func:`register_restored`. Split so the caller can run this filesystem half
    in a worker thread and mutate ``config.workspaces`` on its own thread.
    """
    folder = _archive_folder(config, archive_id)
    metadata = _read_metadata(folder)
    if metadata is None or not metadata.get("name"):
        raise WorkspaceArchiveError("archive metadata is missing or unreadable", 409)
    name = str(metadata["name"])
    from ciao.workspaces import WORKSPACE_NAME_RE  # noqa: PLC0415

    if not WORKSPACE_NAME_RE.fullmatch(name):
        raise WorkspaceArchiveError("archive names an invalid workspace", 409)
    blocker = _restore_blocker(config, metadata, folder)
    if blocker:
        raise WorkspaceArchiveError(f"cannot restore: {blocker}", 409)
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
    return folder, metadata, destination


def register_restored(
    config: Any, folder: Path, metadata: dict[str, Any], destination: Path
) -> dict[str, Any]:
    """Re-register a workspace whose folder :func:`move_back` put back.

    Every registry field is rebuilt from validated metadata (see
    :func:`restored_settings` and :func:`_restored_vault_root`), never copied.
    A conflict that appeared while the folder was moving, or a registry save
    that fails, moves the folder back into the archive so it can be retried.
    """
    name = str(metadata["name"])
    conflict = _registration_conflict(config, metadata, folder)
    if conflict:
        _move_into_archive_again(config, folder, metadata, destination, conflict)
        raise WorkspaceArchiveError(
            f"cannot restore: {conflict}; the archive was left in place", 409
        )
    vault_root, _vault = _restored_vault_root(config, metadata, folder)
    settings = restored_settings(config, metadata)
    config.workspaces[name] = WorkspaceConfig(
        name=name,
        vault_root=vault_root,
        default_provider=settings["default_provider"],
        disallowed_tools=settings["disallowed_tools"],
        allowed_mcp_servers=settings["allowed_mcp_servers"],
        gws_profile=settings["gws_profile"],
        color=settings["color"],
    )
    from ciao.workspaces import persist_workspaces  # noqa: PLC0415

    try:
        persist_workspaces(config)
    except OSError as exc:
        # Undo both halves so the archive stays restorable: without this the
        # folder sat at its old path while the registry on disk lacked the
        # entry, and a retry was refused (content missing, destination exists).
        config.workspaces.pop(name, None)
        content_dir = str(metadata.get("content_dir") or "")
        if content_dir:
            try:
                os.rename(destination, folder / content_dir)
            except OSError as back_exc:
                raise WorkspaceArchiveError(
                    f"the workspace registry could not be saved ({exc}), and "
                    f"moving {_display(config, destination)} back into "
                    f"{_display(config, folder)} failed too ({back_exc}); move "
                    "it back by hand to restore it again",
                    500,
                ) from exc
        raise WorkspaceArchiveError(
            f"could not restore '{name}': the workspace registry could not be "
            f"saved ({exc}); the archive was left in place",
            500,
        ) from exc
    return {
        **metadata,
        "id": folder.name,
        "restored_to": _display(config, destination),
    }


def _move_into_archive_again(
    config: Any, folder: Path, metadata: dict[str, Any], destination: Path, why: str
) -> None:
    content_dir = str(metadata.get("content_dir") or "")
    if not content_dir:
        return
    try:
        os.rename(destination, folder / content_dir)
    except OSError as exc:
        raise WorkspaceArchiveError(
            f"cannot restore: {why}, and moving {_display(config, destination)} "
            f"back into {_display(config, folder)} failed ({exc}); move it back "
            "by hand to restore it again",
            500,
        ) from exc


def unregister_restored(
    config: Any, folder: Path, metadata: dict[str, Any], destination: Path
) -> None:
    """Undo :func:`register_restored` so the archive can be restored again.

    For a step after re-registering that failed (putting the schedules back):
    the entry leaves the registry and the folder moves back into the archive,
    whose ``archive.json`` was kept for exactly this. Raises
    :class:`WorkspaceArchiveError` when either half fails; the workspace is
    then left registered with its folder and ``archive.json`` stays in place.
    """
    name = str(metadata["name"])
    content_dir = str(metadata.get("content_dir") or "")
    if content_dir:
        try:
            os.rename(destination, folder / content_dir)
        except OSError as exc:
            raise WorkspaceArchiveError(
                f"moving {_display(config, destination)} back into "
                f"{_display(config, folder)} failed ({exc})",
                500,
            ) from exc
    try:
        unregister(config, name)
    except OSError as exc:
        # Still registered, so the folder must be where the entry points.
        if content_dir:
            try:
                os.rename(folder / content_dir, destination)
            except OSError as back_exc:
                raise WorkspaceArchiveError(
                    f"the workspace registry could not be saved to undo the "
                    f"restore ({exc}), and moving {_display(config, folder / content_dir)} "
                    f"to {_display(config, destination)} failed too ({back_exc}); "
                    "move it there by hand",
                    500,
                ) from exc
        raise WorkspaceArchiveError(
            f"the workspace registry could not be saved to undo the restore ({exc})",
            500,
        ) from exc


def discard_archive_folder(folder: Path) -> None:
    """Remove a restored archive's ``archive.json`` and its emptied folder.

    Last step of a restore, once the schedules the metadata carried are back:
    until then ``archive.json`` is their only durable copy.
    """
    try:
        (folder / METADATA_FILE).unlink()
        folder.rmdir()
    except OSError:
        # Something else was left in the archive folder. It is not ours to
        # delete; the workspace itself is already back.
        logger.warning("Archive folder %s was not empty after restore", folder)


def restore_archive(config: Any, archive_id: str) -> dict[str, Any]:
    """Move an archived workspace back and re-register it.

    Refuses when the name is taken, the destination exists, or the archive was
    made on the other layout. Returns the archive metadata; the caller restores
    the schedules it carries and refreshes the live managers. The emptied
    archive folder is removed once the workspace is back. Projects and chats
    are not part of an archive: vault-backed projects are rediscovered from
    the restored notes, and every workspace gets its General project again.
    """
    folder, metadata, destination = move_back(config, archive_id)
    restored = register_restored(config, folder, metadata, destination)
    discard_archive_folder(folder)
    return restored
