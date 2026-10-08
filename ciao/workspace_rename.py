"""Rename a workspace and every reference to it, in one locked operation.

The name is the identity key: the registry entry, the agent root directory on
a re-rooted install, and the ``workspace`` field in projects, schedules,
webhooks, import batches and background runs. There is no display label, so
renaming rewrites the key itself rather than adding a second string.

Order matters. Everything refused is refused before anything is written. The
agent-root directory moves first; only then are the registry and the
references persisted. If a step after the directory move fails, the directory
is moved back and the in-memory registry restored before re-raising. A step
that already persisted (projects, schedules) is retried by calling this
function again: each rewrite is idempotent on ``old`` → ``new``.
"""

from __future__ import annotations

import dataclasses
import logging
import os
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
    updated = dataclasses.replace(existing, name=new, vault_root=new_vault)

    if move is not None:
        source, dest = move
        try:
            os.rename(source, dest)
        except OSError as exc:
            raise ValueError(
                f"could not rename '{old}' to '{new}': {exc}"
            ) from exc
    try:
        config.workspaces.pop(old)
        config.workspaces[new] = updated
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
        if search_prefix is not None:
            _forget_search_prefix(config, search_prefix)
    except Exception:
        if move is not None:
            source, dest = move
            try:
                os.rename(dest, source)
            except OSError:
                logger.exception(
                    "Could not move the agent root back after a failed rename"
                )
        config.workspaces.pop(new, None)
        config.workspaces[old] = existing
        try:
            config.persist_workspace_registry()
        except Exception:
            logger.exception(
                "Could not restore the workspace registry after a failed rename"
            )
        raise
    return {"from": old, "to": new}


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
