"""One-time update migration: split the shared chat archive into workspaces.

Before per-workspace archives, every chat's transcripts sat in the install-wide
``<install>/Logs/Chats/``. After the re-rooting each workspace owns its own
``Logs/Chats`` (``CiaoConfig.chats_root``) and new archives land there. This
module moves each existing ``chat-*`` folder whose id the project registry
resolves to a workspace into that workspace, on the first boot after the update.

What stays in the shared root, on purpose: loose ``Logs/Chats/*.md``,
``Logs/CLI``, ``Logs/Telegram``, ``Logs/Meetings``, and any chat id the registry
does not know. The shared root remains a permanent read location.

The receipt ``<runtime>/migration/logs-split.json`` answers only ``migrated``
(``read_receipt``). A ``partial`` receipt is work still to do (a chat with a
pending turn journal, or a folder that could not move) and the next boot runs
again. Every step is idempotent, so a crash mid-move finishes on the next boot.

Registry rewrites are not done here. ``ProjectChatManager._discover_archived_chats``
repoints each moved chat's ``archive_path`` to the file it finds, so the
migration stays filesystem-only.

This retires design decision D5 ("Logs stays global because chat ids cannot be
mapped to a workspace") from #2532.
"""

from __future__ import annotations

import contextlib
import filecmp
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.workspace_reroot import chat_store_file

logger = logging.getLogger(__name__)

RECEIPT_VERSION = 1


def receipt_path(runtime_root: Path) -> Path:
    return runtime_root / "migration" / "logs-split.json"


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def read_receipt(runtime_root: Path) -> dict[str, Any] | None:
    """The receipt of a COMPLETED split, or None. A ``partial`` one is not done."""
    data = _read_json_dict(receipt_path(runtime_root))
    if data is None or data.get("status") != "migrated":
        return None
    return data


def chat_workspace_map(
    runtime_root: Path, workspaces: list[str], primary: str
) -> dict[str, str] | None:
    """Each registered chat id mapped to the workspace it archives into.

    The same rule ``ProjectChatManager._workspace_for_chat`` applies to new
    archives: a chat's project's workspace when that is a registered name,
    otherwise the primary workspace. Chat ids absent from the registry are not
    in the map at all.

    None when the registry cannot be used: missing, not valid JSON, or not the
    expected shape. That is not the same as a readable registry with no entry for
    a chat. ``split`` decides what None means from what is on disk.
    """
    data = _read_json_dict(chat_store_file(runtime_root))
    if data is None:
        return None
    chats = data.get("chats")
    projects = data.get("projects")
    if not isinstance(chats, dict) or not isinstance(projects, dict):
        return None
    known = set(workspaces)
    mapping: dict[str, str] = {}
    for cid, chat in chats.items():
        if not isinstance(chat, dict):
            continue
        project: Any = projects.get(str(chat.get("project_id") or ""))
        workspace = str(project.get("workspace") or "") if isinstance(project, dict) else ""
        mapping[str(cid)] = workspace if workspace in known else primary
    return mapping


def busy_chat_ids(runtime_root: Path) -> set[str]:
    """Chat ids with a turn journal on disk: a turn a crash interrupted, not yet folded."""
    base = runtime_root / "transcripts"
    if not base.is_dir():
        return set()
    busy: set[str] = set()
    for chat_dir in base.iterdir():
        journal = chat_dir / "journal"
        if journal.is_dir() and any(p.is_file() for p in journal.rglob("*")):
            busy.add(chat_dir.name)
    return busy


def _tree(source: Path) -> tuple[list[Path], list[Path], bool]:
    """Regular files and directories under *source*, and whether a symlink sits anywhere in it.

    Never follows a symlink: a link to a directory is reported, not walked, so
    nothing outside the chat folder is moved or deleted through it.
    """
    files: list[Path] = []
    directories: list[Path] = []
    linked = False
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        base = Path(dirpath)
        for name in dirnames:
            child = base / name
            if child.is_symlink():
                linked = True
            else:
                directories.append(child)
        for name in filenames:
            child = base / name
            if child.is_symlink():
                linked = True
            else:
                files.append(child)
    return files, directories, linked


def move_chat_dir(source: Path, destination: Path) -> str:
    """Move one chat folder, merging into the destination when it already exists.

    ``moved``: the folder was renamed into place. ``merged``: every file was
    moved or was byte-identical to a copy already there, and the source is gone.
    ``conflict``: some file differs from its copy, or a symlink is left in the
    source; the source stays in the shared root, readable, and the rest is merged.
    ``failed``: a rename or unlink refused, the source is left where it was, and a
    later run finishes it.
    """
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(source, destination)
        except OSError:
            return "failed"
        return "moved"
    if not destination.is_dir():
        return "failed"
    files, directories, linked = _tree(source)
    conflict = linked
    for file in sorted(files):
        target = destination / file.relative_to(source)
        try:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.rename(file, target)
            elif target.is_file() and filecmp.cmp(file, target, shallow=False):
                file.unlink()
            else:
                conflict = True
        except OSError:
            return "failed"
    directories.sort(key=lambda p: len(p.parts), reverse=True)
    for directory in directories:
        with contextlib.suppress(OSError):
            directory.rmdir()
    with contextlib.suppress(OSError):
        source.rmdir()
    if source.exists():
        return "conflict" if conflict else "failed"
    return "merged"


def split(config: Any) -> dict[str, Any]:
    """Move every registry-resolved ``Logs/Chats/chat-*`` folder. No receipt gate.

    The registry rule: an unusable registry (missing, invalid JSON, wrong shape)
    is a fresh install when the shared ``Chats`` holds no ``chat-*`` folder, and
    nothing moves. When it holds one, the split stops with
    ``registry_unreadable`` set, moves nothing, and the caller records a partial
    receipt so the next boot retries. A readable registry with no entry for a
    chat leaves that chat in the shared root as unresolved.
    """
    runtime_root = Path(config.state_path).parent
    shared = Path(config.logs_root) / "Chats"
    result: dict[str, Any] = {
        "moved": 0,
        "merged": 0,
        "unresolved": 0,
        "busy": [],
        "failed": [],
        "conflicts": [],
        "registry_unreadable": False,
    }
    if not shared.is_dir():
        return result
    workspaces = sorted(n for n in config.workspace_names() if n)
    mapping = chat_workspace_map(runtime_root, workspaces, config.primary_workspace())
    if mapping is None:
        # An unusable registry with chats still on disk must not be read as "no
        # chat is registered": that would make them unresolved and the split
        # would call itself done. With no chats there is nothing to protect, so
        # the install is treated as fresh.
        has_chats = any(
            e.name.startswith("chat-") and e.is_dir() and not e.is_symlink()
            for e in shared.iterdir()
        )
        if has_chats:
            result["registry_unreadable"] = True
            logger.warning("logs split: the project registry is unreadable; nothing moved")
            return result
        mapping = {}
    busy_now = busy_chat_ids(runtime_root)
    for entry in sorted(shared.iterdir()):
        cid = entry.name
        if not cid.startswith("chat-") or not entry.is_dir() or entry.is_symlink():
            continue
        workspace = mapping.get(cid, "")
        if not workspace:
            result["unresolved"] += 1
            continue
        if cid in busy_now:
            result["busy"].append(cid)
            continue
        outcome = move_chat_dir(entry, config.chats_root(workspace) / cid)
        if outcome == "moved":
            result["moved"] += 1
        elif outcome == "merged":
            result["merged"] += 1
        elif outcome == "conflict":
            result["conflicts"].append(cid)
        else:
            result["failed"].append(cid)
    logger.info(
        "logs split: moved %d, merged %d, unresolved %d, busy %d, failed %d, conflicts %d",
        result["moved"],
        result["merged"],
        result["unresolved"],
        len(result["busy"]),
        len(result["failed"]),
        len(result["conflicts"]),
    )
    return result


def _write_receipt(runtime_root: Path, payload: dict[str, Any]) -> None:
    path = receipt_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline=""
    )
    tmp.replace(path)


def migrate_if_needed(config: Any) -> dict[str, Any]:
    """Run the split once per install, at startup, after the re-rooting.

    Gates, in order: a completed receipt, a bootstrap launcher, and an install that
    is not re-rooted (no receipt, so a later re-root still gets the split). Never
    raises: a split that cannot run is reported, and the upgrade goes on.
    """
    try:
        runtime_root = Path(config.state_path).parent
        if read_receipt(runtime_root) is not None:
            return {"status": "already_migrated"}
        if config.bootstrap_mode:
            return {"status": "skipped"}
        if not config._rerooted():
            return {"status": "not_applicable", "reason": "the install is not re-rooted"}
        result = split(config)
        complete = not result["busy"] and not result["failed"] and not result["registry_unreadable"]
        status = "migrated" if complete else "partial"
        payload: dict[str, Any] = {
            "schema_version": RECEIPT_VERSION,
            "status": status,
            **result,
            "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        _write_receipt(runtime_root, payload)
        return payload
    except Exception as exc:  # noqa: BLE001 — a failed split must not fail the upgrade
        logger.exception("logs split could not finish")
        return {"status": "error", "reason": str(exc)}
