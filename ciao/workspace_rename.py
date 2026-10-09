"""Rename a workspace and every reference to it, in one locked operation.

The name is the identity key: the registry entry, the agent root directory on
a re-rooted install, and the ``workspace`` field in projects, schedules,
webhooks, import batches and background runs. There is no display label, so
renaming rewrites the key itself rather than adding a second string.

Order matters. Everything refused is refused before anything is written. The
agent-root directory moves first; only then are the registry and the
references persisted. If a step after the directory move fails, every store
already rewritten is restored to the ``old`` workspace — persisted files and
in-memory objects alike — the directory is moved back and the in-memory
registry restored, before the original exception is re-raised. A rollback step
that itself fails is logged loudly; the original exception still propagates,
so a failed rollback never masquerades as a clean refusal.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import stat
from pathlib import Path
from typing import Any

from ciao.workspaces import WORKSPACE_NAME_RE

logger = logging.getLogger(__name__)

_BUSY_RUN_STATUSES = frozenset({"queued", "running"})


class WorkspaceRenameBusy(ValueError):
    """A rename refused because the workspace is in use.

    The route maps this to 409; every other ``ValueError`` from
    :func:`rename_workspace` is a 400.
    """

    status = 409


def preview_renamed_config(existing: Any, *, old: str, new: str) -> Any:
    """The registry record a rename of ``old`` to ``new`` would persist.

    Shared by :func:`rename_workspace` and the settings route, so the route's
    pre-rename validation of the submitted settings sees exactly the record
    the rename would write: the same name and the same adjusted
    ``vault_root``. A relative ``vault_root`` whose first segment is the old
    name follows it; anything else (absolute, ``.``, or already pointing
    elsewhere) is left unchanged.
    """
    stored_vault = str(getattr(existing, "vault_root", "") or "")
    vault_parts = Path(stored_vault).parts
    new_vault = stored_vault
    if (
        not Path(stored_vault).is_absolute()
        and stored_vault != "."
        and vault_parts
        and vault_parts[0] == old
    ):
        new_vault = Path(new, *vault_parts[1:]).as_posix()
    return dataclasses.replace(existing, name=new, vault_root=new_vault)


def rename_workspace(
    config: Any,
    *,
    old: str,
    new: str,
    projects: Any,
    schedules: Any,
    webhooks: Any,
    imports: Any,
    runs: Any,
) -> dict[str, str]:
    """Rename workspace ``old`` to ``new`` and return ``{"from": old, "to": new}``.

    ``projects`` is the project/chat manager, ``schedules`` its schedule
    store, ``webhooks`` the webhook trigger store, ``imports`` the import
    batch store, and ``runs`` the background-run store. ``projects`` may be
    ``None`` when the caller has no chat registry; every other store is
    required. The caller holds the workspace archive lock: the registry is
    mutated here, on the event loop.
    """
    if not WORKSPACE_NAME_RE.match(new):
        raise ValueError(
            "workspace name must use letters, numbers, dashes, or underscores"
        )
    if new == old:
        raise ValueError(f"workspace is already named '{old}'")
    for name in config.workspace_names():
        if name != old and name.casefold() == new.casefold():
            raise ValueError(
                f"workspace name conflicts with existing workspace '{name}'"
            )
    if new.casefold() in config._archived_workspace_names():
        raise ValueError(
            f"workspace name '{new}' is already used by an archived workspace"
        )
    existing = config.workspace(old)
    if existing is None:
        raise ValueError(f"unknown workspace '{old}'")

    if projects is not None:
        busy_check = getattr(projects, "workspace_busy_chat_ids", None)
        busy_chats = list(busy_check(old)) if callable(busy_check) else []
        if busy_chats:
            raise WorkspaceRenameBusy(
                f"a chat in '{old}' is still working; "
                "let it finish or stop it, then rename the workspace"
            )
    live_runs = [
        run
        for run in runs.list()
        if getattr(run, "workspace", "") == old
        and getattr(run, "status", "") in _BUSY_RUN_STATUSES
    ]
    if live_runs:
        raise WorkspaceRenameBusy(
            f"a background run in '{old}' is still queued or running; "
            "let it finish or stop it, then rename the workspace"
        )

    move: tuple[Path, Path] | None = None
    old_root: Path | None = None
    if config._rerooted():
        source = Path(config.agent_root(old))
        if source != Path(config.workspace_root):
            dest = Path(config.workspace_root) / new
            if dest.exists() or dest.is_symlink():
                raise ValueError(
                    f"cannot rename '{old}': '{dest}' is already in the way"
                )
            if source.is_symlink():
                raise ValueError(
                    f"cannot rename '{old}': '{source}' is a symlink; "
                    "move its target by hand"
                )
            if source.exists():
                try:
                    same_device = (
                        os.stat(source).st_dev == os.stat(source.parent).st_dev
                    )
                except OSError as exc:
                    raise ValueError(
                        f"cannot rename '{old}': cannot inspect '{source}' ({exc})"
                    ) from exc
                if not same_device:
                    raise ValueError(
                        f"'{old}' lives on another filesystem than the install; "
                        "a move there cannot be atomic, so rename it by hand"
                    )
                move = (source, dest)
                old_root = source

    search_prefix: str | None = None
    if config._rerooted():
        from ciao.fts_search import vault_key_prefix  # noqa: PLC0415

        search_prefix = vault_key_prefix(
            Path(config.workspace_vault_root(old)), Path(config.workspace_root)
        )

    updated = preview_renamed_config(existing, old=old, new=new)

    snapshot = _take_snapshot(
        config, projects, schedules, webhooks, imports, runs
    )
    if move is not None:
        source, dest = move
        try:
            os.rename(source, dest)
        except OSError as exc:
            raise ValueError(
                f"could not rename '{old}' to '{new}': {exc}"
            ) from exc
    if projects is not None:
        # Cached providers were built with the old agent root and workspace
        # name. Evicted before the project rows move, because the eviction is
        # keyed by the chats that are still in ``old``; the chats keep their
        # session ids and the next turn rebuilds the provider.
        projects.evict_workspace_providers(old)
    try:
        # Replace the key in place so the renamed workspace keeps its position
        # in the registry: sidebar order, the 1-9 shortcuts and the
        # first-registered primary fallback all follow it.
        renamed = [
            (new, updated) if name == old else (name, record)
            for name, record in config.workspaces.items()
        ]
        config.workspaces.clear()
        config.workspaces.update(renamed)
        config.persist_workspace_registry()
        if projects is not None:
            entries = getattr(projects, "_projects", None)
            if isinstance(entries, dict):
                for info in entries.values():
                    if getattr(info, "workspace", None) == old:
                        info.workspace = new
            save = getattr(projects, "_save", None)
            if callable(save):
                save(reason="workspace_rename")
        schedules.rename_workspace(old, new)
        webhooks.rename_workspace(old, new)
        imports.rename_workspace(old, new)
        new_root = move[1] if move is not None else None
        for run in runs.list():
            if getattr(run, "workspace", "") != old:
                continue
            cwd = str(getattr(run, "cwd", "") or "")
            if new_root is not None and old_root is not None and cwd:
                try:
                    relative = Path(cwd).relative_to(old_root)
                except ValueError:
                    relative = None
                if relative is not None:
                    cwd = (
                        str(new_root)
                        if str(relative) == "."
                        else (new_root / relative).as_posix()
                    )
            runs.replace(dataclasses.replace(run, workspace=new, cwd=cwd))
    except Exception:
        _roll_back(config, projects, snapshot, move)
        raise
    if search_prefix is not None:
        # Derived state, pruned only once every reference write succeeded: a
        # pruning failure must not unwind a completed rename, and is logged
        # rather than reported.
        try:
            _forget_search_prefix(config, search_prefix)
        except Exception:  # noqa: BLE001 - the next index pass prunes it
            logger.exception(
                "Could not drop search rows for renamed workspace %s", old
            )
    return {"from": old, "to": new}


def _read_bytes_if_present(path: Path | None) -> bytes | None:
    """The file's bytes, ``None`` when there is no path or no file.

    ``None`` also covers an unreadable file: the snapshot is best-effort, and
    a rollback that restores every other store still beats restoring none.
    """
    if path is None:
        return None
    try:
        if not path.is_file() or path.is_symlink():
            return None
        return path.read_bytes()
    except OSError:
        logger.exception("Could not snapshot %s before a workspace rename", path)
        return None


def _store_path(store: Any, *names: str) -> Path | None:
    """The first ``Path`` found under ``names``, following one ``_store`` hop.

    File-backed stores carry their file (``_path``); a runner wraps its store
    as ``_store``. Returns ``None`` when neither spells a path.
    """
    candidates: list[Any] = [store]
    inner = getattr(store, "_store", None)
    if inner is not None and inner is not store:
        candidates.append(inner)
    for candidate in candidates:
        for name in names:
            raw = getattr(candidate, name, None)
            if raw is None:
                continue
            try:
                return Path(raw)
            except (TypeError, ValueError):
                continue
    return None


def _take_snapshot(
    config: Any,
    projects: Any,
    schedules: Any,
    webhooks: Any,
    imports: Any,
    runs: Any,
) -> dict[str, Any]:
    """Record every piece of rename-mutated state, before anything is written."""
    project_workspaces: dict[Any, str] | None = None
    if projects is not None:
        entries = getattr(projects, "_projects", None)
        if isinstance(entries, dict):
            project_workspaces = {
                key: str(getattr(info, "workspace", "") or "")
                for key, info in entries.items()
            }
    project_path = _store_path(projects, "_path") if projects is not None else None
    schedule_path = _store_path(schedules, "_path")
    system_state_path = _store_path(schedules, "_system_state_path")
    webhook_path = _store_path(webhooks, "_path")
    import_path = _store_path(imports, "_path")
    run_path = _store_path(runs, "_path")
    return {
        "workspaces": dict(config.workspaces),
        "registry_bytes": _read_bytes_if_present(
            Path(config.state_path).parent / "workspaces.json"
        ),
        "project_workspaces": project_workspaces,
        "project_path": project_path,
        "project_bytes": _read_bytes_if_present(project_path),
        "schedule_path": schedule_path,
        "schedule_bytes": _read_bytes_if_present(schedule_path),
        "system_state_path": system_state_path,
        "system_state_bytes": _read_bytes_if_present(system_state_path),
        "webhook_path": webhook_path,
        "webhook_bytes": _read_bytes_if_present(webhook_path),
        "import_path": import_path,
        "import_bytes": _read_bytes_if_present(import_path),
        "run_path": run_path,
        "run_bytes": _read_bytes_if_present(run_path),
    }


def _restore_file(path: Path | None, snapshot: bytes | None) -> None:
    """Put one persisted file back the way the snapshot found it.

    A ``None`` snapshot means the file did not exist (or could not be read)
    before the rename, so there is nothing known-good to restore: leaving the
    file alone preserves the evidence rather than deleting it.
    """
    if path is None or snapshot is None:
        return
    try:
        current = path.read_bytes() if path.is_file() else None
    except OSError:
        current = None
    if current == snapshot:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        # Secret-bearing stores (webhooks, imports) are owner-only; a
        # rollback must not widen them to the default umask mode.
        mode = 0o600
    tmp = path.with_name(f".{path.name}.rename-rollback.tmp")
    tmp.write_bytes(snapshot)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _roll_back(
    config: Any,
    projects: Any,
    snapshot: dict[str, Any],
    move: tuple[Path, Path] | None,
) -> None:
    """Restore every reference the failed rename may have rewritten.

    Each step is guarded so one failing restore cannot hide the others, and
    every failure is logged loudly: the original exception still propagates,
    so a failed rollback never masquerades as a clean refusal.
    """
    if move is not None:
        source, dest = move
        try:
            os.rename(dest, source)
        except OSError:
            logger.exception(
                "Could not move the agent root back after a failed rename"
            )
    config.workspaces.clear()
    config.workspaces.update(snapshot.get("workspaces", {}))
    try:
        config.persist_workspace_registry()
    except Exception:
        logger.exception(
            "Could not restore the workspace registry after a failed rename"
        )
        try:
            registry_path = Path(config.state_path).parent / "workspaces.json"
            raw = snapshot.get("registry_bytes")
            if isinstance(raw, bytes):
                _restore_file(registry_path, raw)
        except Exception:
            logger.exception(
                "Could not restore the workspace registry file after a failed rename"
            )
    if projects is not None:
        project_workspaces = snapshot.get("project_workspaces")
        if isinstance(project_workspaces, dict):
            entries = getattr(projects, "_projects", None)
            if isinstance(entries, dict):
                for key, workspace in project_workspaces.items():
                    info = entries.get(key)
                    if info is not None:
                        try:
                            info.workspace = workspace
                        except Exception:  # noqa: BLE001 - keep restoring the rest
                            logger.exception(
                                "Could not restore project %r after a failed rename",
                                key,
                            )
            save = getattr(projects, "_save", None)
            if callable(save):
                try:
                    save(reason="workspace_rename_rollback")
                except Exception:
                    logger.exception(
                        "Could not re-save projects after a failed workspace rename"
                    )
        project_path = _store_path(projects, "_path")
        if project_path is not None and snapshot.get("project_bytes") is not None:
            try:
                _restore_file(project_path, snapshot["project_bytes"])
            except Exception:
                logger.exception(
                    "Could not restore the project registry file after a failed rename"
                )
    # File restores below run reverse to the forward writes (runs first,
    # schedules last); each is independent, so one failing still leaves the
    # others restored.
    _restore_snapshot_files(snapshot)


def _restore_snapshot_files(snapshot: dict[str, Any]) -> None:
    """Write every snapshotted store file back, logging rather than raising."""
    _restore_logged("background runs", snapshot.get("run_bytes"), snapshot)
    _restore_logged("import batches", snapshot.get("import_bytes"), snapshot)
    _restore_logged("webhook triggers", snapshot.get("webhook_bytes"), snapshot)
    _restore_logged("schedules", snapshot.get("schedule_bytes"), snapshot)
    _restore_logged(
        "system schedule state", snapshot.get("system_state_bytes"), snapshot
    )


def _restore_logged(label: str, raw: Any, snapshot: dict[str, Any]) -> None:
    """Restore one snapshotted file; a failure is logged, never raised."""
    if not isinstance(raw, bytes):
        return
    key = {
        "background runs": "run_path",
        "import batches": "import_path",
        "webhook triggers": "webhook_path",
        "schedules": "schedule_path",
        "system schedule state": "system_state_path",
    }[label]
    path = snapshot.get(key)
    if not isinstance(path, Path):
        return
    try:
        _restore_file(path, raw)
    except Exception:
        logger.exception(
            "Could not restore %s after a failed workspace rename", label
        )


def _forget_search_prefix(config: Any, prefix: str) -> None:
    """Drop the old vault's rows from the install's search database."""
    import sqlite3  # noqa: PLC0415

    from ciao.async_reads import keyed_lock  # noqa: PLC0415
    from ciao.fts_search import forget_subtree, get_db_path, init_db  # noqa: PLC0415

    db_path = get_db_path(Path(config.state_path).parent)
    if not db_path.exists():
        return
    conn = sqlite3.connect(db_path)
    try:
        with keyed_lock(f"fts-index:{db_path}"):
            init_db(conn)
            forget_subtree(conn, prefix)
    finally:
        conn.close()
