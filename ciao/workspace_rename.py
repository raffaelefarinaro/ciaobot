"""Rename a workspace and every reference to it, in one locked operation.

The name is the identity key: the registry entry, the agent root directory on
a re-rooted install, and the ``workspace`` field in projects, schedules,
and webhooks. There is no display label, so
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

import contextlib
import dataclasses
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from ciao.workspaces import WORKSPACE_NAME_RE

logger = logging.getLogger(__name__)


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
    owner: Any = None,
    updated: Any = None,
) -> dict[str, str]:
    """Rename workspace ``old`` to ``new`` and return ``{"from": old, "to": new}``.

    ``projects`` is the project/chat manager, ``schedules`` its schedule
    store, ``webhooks`` the webhook trigger store. ``projects`` may be
    ``None`` when the caller has no chat registry; every other store is
    required. The caller holds the workspace archive lock: the registry is
    mutated here, on the event loop.
    """
    from ciao.workspaces import ensure_registry_current, workspace_registry_claim
    if owner is None:
        from ciao.shared_brain import checkout_claims_nowait
        links = [*getattr(config.workspace(old), "shared_brains", []), *getattr(updated, "shared_brains", [])]
        with checkout_claims_nowait((link.path for link in links), config), workspace_registry_claim(config) as claim:
            return rename_workspace(config, old=old, new=new, projects=projects, schedules=schedules,
                                    webhooks=webhooks, owner=claim, updated=updated)
    ensure_registry_current(config)
    return _rename_owned(config, old, new, projects, schedules, webhooks, owner, updated)


def _rename_owned(config: Any, old: str, new: str, projects: Any, schedules: Any, webhooks: Any, owner: Any, updated: Any) -> dict[str, str]:
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

    move: tuple[Path, Path] | None = None
    source = Path(config.agent_root(old))
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

    from ciao.fts_search import vault_key_prefix  # noqa: PLC0415

    search_prefix: str | None = vault_key_prefix(
        Path(config.workspace_vault_root(old)), Path(config.workspace_root)
    )

    if updated is None:
        updated = preview_renamed_config(existing, old=old, new=new)

    snapshot = _take_snapshot(
        config, projects, schedules
    )
    snapshot["rename_pair"] = (old, new)
    if projects is not None:
        # Cached providers were built with the old agent root and workspace
        # name. Evicted before the agent root and the project rows move: the
        # eviction is keyed by the chats that are still in ``old``, and the busy
        # check has already passed. The chats keep their session ids and the
        # next turn rebuilds the provider.
        projects.evict_workspace_providers(old)
    if move is not None:
        source, dest = move
        try:
            os.rename(source, dest)
        except OSError as exc:
            raise ValueError(
                f"could not rename '{old}' to '{new}': {exc}"
            ) from exc
    try:
        from ciao.onboarding import read_receipt  # noqa: PLC0415
        from ciao.onboarding_progress import rename_workspace_progress  # noqa: PLC0415

        receipt = read_receipt(Path(config.state_path).parent)
        snapshot_revision = snapshot.get("onboarding_revision")
        if (
            receipt is not None
            and isinstance(snapshot_revision, int)
            and receipt.revision != snapshot_revision
        ):
            from ciao.onboarding import OnboardingConflictError  # noqa: PLC0415

            raise OnboardingConflictError("onboarding receipt revision conflict")
        if receipt is not None:
            # Run even when the source has no entry: a stale destination entry
            # (say an archived name reused earlier) must not become the renamed
            # workspace's progress.
            renamed_receipt = rename_workspace_progress(
                config, source=old, destination=new,
                expected_revision=receipt.revision,
            )
            if renamed_receipt.revision != receipt.revision:
                snapshot["onboarding_written_revision"] = renamed_receipt.revision
        # Replace the key in place so the renamed workspace keeps its position
        # in the registry: sidebar order, the 1-9 shortcuts and the
        # first-registered primary fallback all follow it.
        renamed = [
            (new, updated) if name == old else (name, record)
            for name, record in config.workspaces.items()
        ]
        from ciao.workspaces import workspace_registry_lock  # noqa: PLC0415

        with workspace_registry_lock(config):
            config.workspaces.clear()
            config.workspaces.update(renamed)
            config.persist_workspace_registry(owner=owner)
            snapshot["registry_written_bytes"] = config._workspace_registry_bytes
        if projects is not None:
            entries = getattr(projects, "_projects", None)
            if isinstance(entries, dict):
                for info in entries.values():
                    if getattr(info, "workspace", None) == old:
                        info.workspace = new
            save = getattr(projects, "_save", None)
            if callable(save):
                save(reason="workspace_rename")
        # Registered before the call, so a failure partway through it (the user
        # rows saved, the overlay not) is still undone. The pre-rename bytes,
        # the rename and the bytes it left are taken under one hold of the
        # store's writer lock, so no other writer's edit falls between them: a
        # rollback restores the pre-rename bytes only while the files still read
        # exactly like what the rename wrote, and otherwise moves just the
        # renamed rows back.
        snapshot["schedule_store"] = schedules
        with _schedule_lock(snapshot):
            snapshot["schedule_bytes"] = _read_bytes_if_present(snapshot.get("schedule_path"))
            snapshot["system_state_bytes"] = _read_bytes_if_present(snapshot.get("system_state_path"))
            schedules.rename_workspace(old, new)
            snapshot["schedule_written"] = _read_bytes_if_present(snapshot.get("schedule_path"))
            snapshot["system_state_written"] = _read_bytes_if_present(snapshot.get("system_state_path"))
        undo: list[tuple[bytes, bytes]] = []
        snapshot["store_undo"].append((webhooks, undo))
        webhooks.rename_workspace(old, new, undo=undo)
    except Exception:
        _roll_back(config, projects, snapshot, move, owner)
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
    schedule_path = _store_path(schedules, "_path")
    system_state_path = _store_path(schedules, "_system_state_path")
    return {
        "workspaces": dict(config.workspaces),
        "onboarding_path": Path(config.state_path).parent / "onboarding.json",
        "onboarding_bytes": _read_bytes_if_present(
            Path(config.state_path).parent / "onboarding.json"
        ),
        "onboarding_revision": _onboarding_revision(
            Path(config.state_path).parent / "onboarding.json"
        ),
        "registry_bytes": config._workspace_registry_bytes,
        "project_workspaces": project_workspaces,
        "schedule_path": schedule_path,
        "schedule_bytes": _read_bytes_if_present(schedule_path),
        "system_state_path": system_state_path,
        "system_state_bytes": _read_bytes_if_present(system_state_path),
        "store_undo": [],
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
        # The secret-bearing webhook store is owner-only; a
        # rollback must not widen them to the default umask mode.
        mode = 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rename-")
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(snapshot)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _roll_back(
    config: Any,
    projects: Any,
    snapshot: dict[str, Any],
    move: tuple[Path, Path] | None,
    owner: Any,
) -> None:
    """Restore every reference the failed rename may have rewritten.

    Each step is guarded so one failing restore cannot hide the others, and
    every failure is logged loudly: the original exception still propagates,
    so a failed rollback never masquerades as a clean refusal.
    """
    from ciao.workspaces import WorkspaceRegistryConflict, read_workspace_registry_bytes, workspace_registry_lock

    raw = snapshot["registry_bytes"]
    written = snapshot.get("registry_written_bytes")
    expected = written if written is not None else raw
    if read_workspace_registry_bytes(config) != expected:
        raise WorkspaceRegistryConflict("registry changed during rename; inspect the workspace before retrying")

    if move is not None:
        source, dest = move
        if source.exists() or source.is_symlink():
            raise FileExistsError(f"the old workspace path was recreated: {source}")
        os.rename(dest, source)
    with workspace_registry_lock(config):
        if written is not None:
            try:
                owner.replace(raw, expected=written)
            except Exception:
                if move is not None and not dest.exists() and not dest.is_symlink():
                    os.rename(source, dest)
                raise
            config._workspace_registry_bytes = raw
        config.workspaces.clear()
        config.workspaces.update(snapshot["workspaces"])
    if projects is not None:
        project_workspaces = snapshot.get("project_workspaces")
        if isinstance(project_workspaces, dict):
            entries = getattr(projects, "_projects", None)
            if isinstance(entries, dict):
                for key, workspace in project_workspaces.items():
                    info = entries.get(key)
                    old, new = snapshot["rename_pair"]
                    if info is not None and workspace == old and info.workspace == new:
                        try:
                            info.workspace = old
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
    _restore_snapshot_files(snapshot)


def _restore_snapshot_files(snapshot: dict[str, Any]) -> None:
    """Write every snapshotted store file back, logging rather than raising."""
    _restore_onboarding_receipt(snapshot)
    _restore_schedule_files(snapshot)
    conflicts = []
    for store, undo in reversed(snapshot["store_undo"]):
        for before, written in undo:
            if written == before:
                continue
            with store._mutation():
                if store._path.read_bytes() != written:
                    conflicts.append(store._path.name)
                else:
                    _restore_file(store._path, before)
    if conflicts:
        raise RuntimeError("Newer edits were preserved during rename rollback; inspect " + ", ".join(conflicts))


def _schedule_lock(snapshot: dict[str, Any]) -> Any:
    """The store's writer lock for the rename's schedule files, if it has files.

    A store without a file (a test stub) has nothing to lock.
    """
    path = snapshot.get("schedule_path")
    if not isinstance(path, Path):
        return contextlib.nullcontext()
    from ciao.schedules import schedule_store_lock  # noqa: PLC0415

    return schedule_store_lock(path.parent)


def _restore_schedule_files(snapshot: dict[str, Any]) -> None:
    """Undo the schedule store's part of a failed rename.

    Exact restore while nothing else has written the files since the rename;
    once something has, only the renamed rows move back, so an unrelated edit
    is never discarded. A rename that failed before the schedule step has no
    store registered and restores nothing here; one that failed inside the step
    has no ``schedule_written``, so it always takes the row-wise path.
    """
    store = snapshot.get("schedule_store")
    pair = snapshot.get("rename_pair")
    if store is None or pair is None:
        return
    paths = (snapshot.get("schedule_path"), snapshot.get("system_state_path"))
    written = (snapshot.get("schedule_written"), snapshot.get("system_state_written"))
    originals = (snapshot.get("schedule_bytes"), snapshot.get("system_state_bytes"))
    try:
        with _schedule_lock(snapshot):
            untouched = all(
                _read_bytes_if_present(path) == wrote
                for path, wrote in zip(paths, written)
                if isinstance(path, Path)
            )
            if untouched:
                _restore_logged("schedules", originals[0], snapshot)
                _restore_logged("system schedule state", originals[1], snapshot)
            else:
                store.revert_rename(
                    pair[0], pair[1],
                    original_schedules=originals[0],
                    original_state=originals[1],
                )
    except Exception:
        logger.exception("Could not restore schedules after a failed workspace rename")


def _onboarding_revision(path: Path) -> int | None:
    """Return the valid receipt revision captured with a rename snapshot."""
    try:
        from ciao.onboarding import read_receipt  # noqa: PLC0415

        receipt = read_receipt(path.parent)
    except Exception:
        return None
    return None if receipt is None else receipt.revision


def _restore_onboarding_receipt(snapshot: dict[str, Any]) -> None:
    raw = snapshot.get("onboarding_bytes")
    path = snapshot.get("onboarding_path")
    written = snapshot.get("onboarding_written_revision")
    if not isinstance(raw, bytes) or not isinstance(path, Path):
        return
    # Only the rename's own write is ours to undo. Comparing "snapshot + 1"
    # would also match an unrelated PATCH that landed after the rename wrote,
    # and restoring over it would delete that progress.
    if not isinstance(written, int):
        return
    from ciao.async_reads import keyed_lock  # noqa: PLC0415

    try:
        with keyed_lock(f"onboarding:{path.resolve()}"):
            if _onboarding_revision(path) != written:
                return
            _restore_file(path, raw)
    except Exception:
        logger.exception("Could not restore onboarding receipt after a failed workspace rename")


def _restore_logged(label: str, raw: Any, snapshot: dict[str, Any]) -> None:
    """Restore one snapshotted file; a failure is logged, never raised."""
    if not isinstance(raw, bytes):
        return
    key = {
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
