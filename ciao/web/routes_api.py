"""REST API routes for the PWA."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import functools
import json
import logging
import math
import mimetypes
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

# Imported lazily inside the handlers (see `_housekeeping_context`); only
# the annotations need the name at module scope.
if TYPE_CHECKING:
    from ciao import operator_actions

from urllib.parse import parse_qsl, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from ciao import proposal_actions
from ciao import proposal_kinds
from ciao import proposal_outcomes
from ciao import subagent_tracking
from ciao import desktop_build
from ciao import provider_registry
from ciao.jsonio import write_private_text
from ciao.memory_receipts import QueueReceiptUnavailable
from ciao.web.document_conversion import is_anydoc_document
from ciao.native_sessions import live_sessions_for_workspace
from ciao.config import (
    CLAUDE_MODELS,
    GWS_DEFAULT_PROFILE,
    MAX_IMAGE_SIZE_BYTES,
    RESTART_EXIT_CODE,
    WorkspaceConfig,
)
from ciao.models import THINKING_LEVELS, ChatContext
from ciao.workspaces import (
    WORKSPACE_NAME_RE,
    persist_workspaces,
    workspace_from_request,
    workspace_provider_options,
    workspace_provider_values,
    workspace_to_dict,
)
# Kept as an alias: several call sites predate the shared module.
_WORKSPACE_NAME_RE = WORKSPACE_NAME_RE
from ciao.tool_path import resolve_tool
from ciao.providers.opencode import OpencodeProvider
from ciao.provider_service import capabilities_for, supported_providers
from ciao.schedules import (
    DEFAULT_INTERVAL_MINUTES,
    FREQUENCIES,
    INTERVAL_FREQUENCY,
    ScheduleEntry,
    compute_last_expected_run,
    compute_next_run,
    is_interval,
    normalize_archive_policy,
    normalize_interval_minutes,
    publish_automations_changed,
    run_failed_since,
    stamp_fallback_project,
    wall_clock_time_error,
    wall_clock_time_value_error,
    was_dispatched_since,
)
from ciao.setup_status import setup_status
from ciao.cli import _auth_command_for_provider
from ciao.skills_inventory import build_skill_inventory
from ciao.vault_index import (
    _build_graph,
    filter_entries,
    scan_targets,
    strip_references,
)
from ciao.vault_lint import EXCLUDE_DIRS, _links_in
from ciao.async_reads import run_read
from ciao.web.project_chats import _ALLOWED_IMAGE_EXTENSIONS
from ciao.web.routes_helpers import (
    _allowed_roots,
    _commit_and_push,
    _git_pull_with_retry,
    _resolve_workspace_path,
)
# The proposal queue's domain logic (scanning, rewriting, promotion) lives in
# its own module; the handlers below keep request parsing and response mapping.
# Imported as a module, not by name, so a test can patch one helper and have the
# handlers see the patch.
from ciao.web import proposal_service
# Same arrangement for the chat workflow's domain rules (upload policy, handover
# trimming, title derivation, scheduled-run grading), which ProjectChatManager
# and these handlers share.
from ciao.web import chat_service
# And again for the transcript read path: everything that turns a provider's
# stored messages into the rows the PWA renders. The handlers below keep the
# query params, the pagination envelope and the part cache.
from ciao.web import transcript_service

logger = logging.getLogger(__name__)

_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


async def _read_upload_limited(upload, max_bytes: int) -> bytes:
    """Read an UploadFile while buffering at most its size cap plus one byte.

    Starlette spools multipart files, but ``UploadFile.read()`` without a size
    copies the complete file into memory. Read at most one byte beyond the cap
    so oversized uploads are rejected before that unbounded allocation.
    """
    if max_bytes < 0:
        raise ValueError("invalid upload size limit")
    data = bytearray()
    while True:
        read_size = min(_UPLOAD_READ_CHUNK_BYTES, max_bytes + 1 - len(data))
        chunk = await upload.read(read_size)
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ValueError("file too large")


_STATS_CACHE_PATH = Path.home() / ".claude" / "stats-cache.json"

# Labels and example chips for the two account names that predate the account
# registry. Nothing creates them any more — a fresh install starts with no
# Google account — but an install that already has one keeps its wording.
# Annotated because the values are heterogeneous (str labels alongside a
# list[str] of examples): without it mypy widens every lookup to Sequence[str],
# and `meta["purpose"]` stops being usable where a str is expected.
_GWS_PROFILE_META: dict[str, dict[str, Any]] = {
    "personal": {
        "label": "Personal Google account",
        "purpose": "Private Google account. Keep this separate from company systems.",
        # Shown for accounts connected before scopes were recorded. Their
        # credentials.json has no `scopes` key and re-consent is the only way
        # to get one, so without this an upgrading user's connected account
        # silently loses every chip it used to show.
        "examples": ["Gmail", "Calendar", "Tasks"],
    },
    "work": {
        "label": "Work Google account",
        "purpose": "Company Google account used for work Drive, Docs, Sheets, and Slides.",
        "examples": ["Drive", "Docs", "Sheets", "Slides", "Gmail", "Calendar"],
    },
}
_GWS_AUTH_FILES = ("credentials.json", "credentials.enc")


def _gws_purpose_with_chips(purpose: str, chips: list[str]) -> str:
    """Append the granted services to the profile's standing description.

    The description is not replaced: for the personal profile it carries the
    "keep this separate from company systems" guidance, which matters most
    once an account is actually connected.
    """
    if len(chips) == 1:
        joined = chips[0]
    elif len(chips) == 2:
        joined = f"{chips[0]} and {chips[1]}"
    else:
        joined = f"{', '.join(chips[:-1])}, and {chips[-1]}"
    return f"{purpose} Connected to {joined}."


def _known_workspace_names(pcm: object) -> set[str]:
    config = getattr(pcm, "_config", None)
    workspace_names = getattr(config, "workspace_names", None)
    if callable(workspace_names):
        names = {str(name) for name in workspace_names() if str(name)}
        if names:
            return names
    return {"personal", "work"}


def _workspace_provider_options(config) -> list[dict[str, str]]:
    return workspace_provider_options(config)


def _workspace_provider_values(config) -> set[str]:
    return workspace_provider_values(config)


# ── Auth ────────────────────────────────────────────────────────────────
# Auth handlers live in routes_auth.py; app.py imports them from there.


# ── Projects ─────────────────────────────────────────────────────────────


async def list_workspaces(request: Request) -> JSONResponse:
    """Return configured logical workspaces for the PWA sidebar."""
    config = request.app.state.config
    return JSONResponse(_workspaces_payload(config))


def _workspace_to_dict(workspace: WorkspaceConfig, config) -> dict:
    return workspace_to_dict(workspace, config)


def _workspaces_payload(config) -> dict:
    workspaces = [_workspace_to_dict(workspace, config) for workspace in config.workspaces.values()]
    return {
        "workspaces": workspaces,
        "active": workspaces[0]["name"] if workspaces else None,
        # The workspace that cannot be archived; the PWA hides its Archive
        # button rather than offering an action the server refuses.
        "primary": config.primary_workspace() or None,
        "provider_options": _workspace_provider_options(config),
    }


def _workspace_from_request(
    data: dict,
    *,
    config,
    existing: WorkspaceConfig | None = None,
) -> WorkspaceConfig:
    return workspace_from_request(data, config=config, existing=existing)


def _persist_workspaces(config) -> None:
    persist_workspaces(config)


def _refresh_project_manager_workspaces(request: Request) -> None:
    pcm = getattr(request.app.state, "project_chat_manager", None)
    refresh = getattr(pcm, "refresh_workspaces", None)
    if callable(refresh):
        refresh()


async def upsert_workspace_setting(request: Request) -> JSONResponse:
    config = request.app.state.config
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected an object"}, status_code=400)
    route_name = request.path_params.get("name")
    if route_name:
        body = {**body, "name": route_name}
    # Serialized with archive and restore; see ``_workspace_archive_lock``.
    async with _workspace_archive_lock(request):
        existing = config.workspace(str(body.get("name", "")).strip())
        try:
            workspace = _workspace_from_request(body, config=config, existing=existing)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        created = workspace.name not in config.workspaces
        profile_changed = (
            not created
            and existing is not None
            and str(getattr(existing, "gws_profile", "") or "")
            != str(getattr(workspace, "gws_profile", "") or "")
        )
        config.workspaces[workspace.name] = workspace
        _persist_workspaces(config)
        _refresh_project_manager_workspaces(request)
        payload = _workspaces_payload(config)
        if created:
            payload["bootstrapped"] = await _bootstrap_new_agent_root(config, workspace.name)
        elif profile_changed:
            # Linking a workspace to its first account (or unlinking it) changes
            # which `gws-*` stock skills it should get, and skill sync only runs at
            # startup/repair. Resync now so the catalog matches the new linkage
            # instead of waiting for a restart. On a pre-re-root install the target
            # root is shared by every workspace, so the gate aggregates all of them
            # (unlinking one must not prune the shared catalog while another still
            # links an account).
            from ciao.sync_skills import (  # noqa: PLC0415
                resolve_workspace_skills_gws_gate,
                sync_workspace_skills,
            )

            try:
                root = Path(config.agent_root(workspace.name))
                await asyncio.to_thread(
                    sync_workspace_skills,
                    root,
                    refresh_upstream=False,
                    gws_profile=resolve_workspace_skills_gws_gate(
                        config, root, workspace.name
                    ),
                )
            except Exception:  # noqa: BLE001 - the update already succeeded
                logger.exception(
                    "Could not resync skills for workspace %s after profile change",
                    workspace.name,
                )
        return JSONResponse(payload, status_code=201 if created else 200)


async def _bootstrap_new_agent_root(config, name: str) -> bool:
    """Seed a freshly created workspace's own agent root.

    Once the re-rooting receipt has flipped, a new workspace's chats run from
    `<install>/<name>` immediately - but persisting the registry and refreshing
    the manager only creates the General vault document. Nothing seeded
    `CLAUDE.md`/`AGENTS.md`, commands, agents or the skill mirrors, so a chat
    started right after creation ran with NO workspace guide and none of the
    packaged or custom capabilities. The startup task repairs it, but only after
    a restart, which is far too late for the chat the operator just opened.

    Best-effort on purpose: the workspace is already registered and usable by
    the time this runs, so a sync failure is reported rather than unwound. The
    startup task remains the backstop.
    """
    from ciao.sync_skills import sync_workspace_skills

    try:
        root = Path(config.agent_root(name))
    except (AttributeError, ValueError):
        return False
    try:
        # `refresh_upstream=False`: this is a local seeding on a user-facing
        # request, not the periodic upstream regeneration.
        from ciao.gws_auth import workspace_gws_profile  # noqa: PLC0415

        await asyncio.to_thread(
            sync_workspace_skills,
            root,
            refresh_upstream=False,
            gws_profile=workspace_gws_profile(config, name),
        )
    except Exception:  # noqa: BLE001 - creation already succeeded
        logger.exception("Could not seed agent root for new workspace %s", name)
        return False
    return True


async def _resync_shared_skills(config, name: str, verb: str) -> None:
    """Resync the shared skill catalog after the registry changed.

    On a pre-re-root install the shared catalog serves every workspace, so
    removing (or restoring) the only one with a GWS profile changes whether the
    gws-* skills belong there. After re-rooting the active catalogs live under
    each <install>/<workspace> root and the install root is retired, so syncing
    it would recreate CLAUDE.md/.claude/skills there - skip it in that layout.
    """
    if getattr(config, "_rerooted", lambda: False)():
        return
    try:
        from ciao.sync_skills import (  # noqa: PLC0415
            resolve_workspace_skills_gws_gate,
            sync_workspace_skills,
        )

        root = Path(config.workspace_root)
        await asyncio.to_thread(
            sync_workspace_skills,
            root,
            refresh_upstream=False,
            gws_profile=resolve_workspace_skills_gws_gate(
                config, root, config.primary_workspace()
            ),
        )
    except Exception:  # noqa: BLE001 - the registry change already succeeded
        logger.exception("Could not resync shared skills after %s workspace %s", verb, name)


def _schedule_manager(request: Request) -> Any:
    return getattr(request.app.state, "schedule_manager", None)


def _publish_automations_changed(request: Request) -> None:
    pcm = getattr(request.app.state, "project_chat_manager", None)
    if pcm is None:
        return
    try:
        from ciao.schedules import publish_automations_changed  # noqa: PLC0415

        publish_automations_changed(pcm)
    except Exception:  # noqa: BLE001 - a missed nudge only delays a refresh
        logger.debug("Could not publish automations_changed", exc_info=True)


def _publish_workspaces_changed(request: Request) -> None:
    """Tell every open client the workspace registry changed.

    Other tabs and devices only see ``project_*`` frames when a workspace is
    archived or restored; without this their workspace list and active
    workspace stayed stale until a reload. Carries no payload: clients refetch
    ``/api/workspaces``. Fire-and-forget, like ``schedules_changed``.
    """
    pcm = getattr(request.app.state, "project_chat_manager", None)
    events = getattr(pcm, "events", None)
    if events is None:
        return
    try:
        events.publish({"type": "workspaces_changed"})
    except Exception:  # noqa: BLE001 - a missed nudge only delays a refresh
        logger.debug("Could not publish workspaces_changed", exc_info=True)


def _workspace_archive_lock(request: Request) -> asyncio.Lock:
    """One registry change at a time per app: archive, restore, create, update.

    Without it a double-submitted archive ran twice: the second pass found the
    folder already gone and recorded an empty "archived" copy, or its failed
    rename refreshed the manager while the workspace was still registered and
    recreated the General folder at the old path. Create and update take it
    too: a restore awaits its folder move, and a create for the archived name
    landing in that gap was silently overwritten by the archived settings.
    Kept on ``app.state`` rather
    than at module level so it binds to the app's own event loop.
    """
    lock = getattr(request.app.state, "workspace_archive_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.workspace_archive_lock = lock
    return lock


async def archive_workspace_setting(request: Request) -> JSONResponse:
    """Archive a workspace: unregister it and move its folder aside, intact.

    Nothing is merged into another workspace and nothing is deleted - see
    ``ciao/workspace_archive.py``. Order matters: everything that is refused is
    refused before anything changes; schedules are taken and the folder moves
    first, so a failed move changes nothing that cannot be put back; only then
    are the chats archived (irreversible), still while the workspace is
    registered; the registry entry goes last. A failure after the move puts
    the folder and schedules back so the workspace stays registered and the
    archive can be retried.
    """
    from ciao import workspace_archive  # noqa: PLC0415

    config = request.app.state.config
    name = str(request.path_params.get("name", "")).strip()
    async with _workspace_archive_lock(request):
        try:
            target = workspace_archive.plan_archive(config, name)
        except workspace_archive.WorkspaceArchiveError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)

        pcm = getattr(request.app.state, "project_chat_manager", None)
        busy = getattr(pcm, "workspace_busy_chat_ids", None)
        if callable(busy) and busy(name):
            return JSONResponse(
                {
                    "error": (
                        f"a chat in '{name}' is still working or being archived; "
                        "let it finish or stop it, then archive the workspace"
                    )
                },
                status_code=409,
            )

        scope = getattr(pcm, "workspace_scope", None)
        project_ids, chat_ids = scope(name) if callable(scope) else (set(), set())

        def _belongs(item: dict) -> bool:
            return (
                str(item.get("workspace") or "") == name
                or str(item.get("web_project_id") or "") in project_ids
                or str(item.get("fallback_project_id") or "") in project_ids
                or str(item.get("web_chat_id") or "") in chat_ids
            )

        counts = getattr(pcm, "workspace_counts", None)
        summary: dict[str, Any] = (
            dict(counts(name)) if callable(counts) else {"projects": 0, "chats": 0}
        )
        manager = _schedule_manager(request)
        take = getattr(manager, "take_user_items", None)
        schedules: list[dict] = take(_belongs) if callable(take) else []
        summary["schedules"] = len(schedules)
        try:
            # On the event loop on purpose: from the busy check above to the
            # chats being archived below there is no await, so no turn can start
            # in this workspace in between. The move is one same-filesystem
            # rename plus two small metadata writes.
            archived = workspace_archive.move_to_archive(
                config, target, schedules=schedules, summary=summary
            )
        except (workspace_archive.WorkspaceArchiveError, OSError) as exc:
            # ``move_to_archive`` leaves the workspace where it was on every
            # failure, so the schedules it took go straight back.
            put_back = getattr(manager, "put_back_user_items", None)
            if callable(put_back) and schedules:
                put_back(schedules)
            if isinstance(exc, workspace_archive.WorkspaceArchiveError):
                return JSONResponse({"error": exc.message}, status_code=exc.status)
            logger.exception("Archiving workspace %s failed", name)
            return JSONResponse(
                {"error": f"could not archive '{name}': {exc}"}, status_code=500
            )

        def _roll_back(failure: str) -> JSONResponse:
            """Leave the workspace registered, with its folder and schedules.

            A step after the move failed while the workspace is still
            registered. Chats already archived stay archived (that part is
            irreversible and was persisted); everything else goes back so the
            archive can simply be retried. The schedules go back even when the
            folder cannot: they belong to a registered workspace, and
            ``archive.json`` keeps its own copy.
            """
            undo_error = ""
            try:
                workspace_archive.undo_move_to_archive(config, target, archived)
            except workspace_archive.WorkspaceArchiveError as undo_exc:
                undo_error = undo_exc.message
            schedules_back = True
            put_back = getattr(manager, "put_back_user_items", None)
            if callable(put_back) and schedules:
                try:
                    put_back(schedules)
                except Exception:  # noqa: BLE001 - archive.json still holds them
                    schedules_back = False
                    logger.exception(
                        "Could not put back the schedules of %s; they remain in %s",
                        name,
                        archived["path"],
                    )
            if not undo_error:
                # ``archive.json`` outlived the folder move on purpose: it is
                # the schedules' only durable copy until they are back.
                if schedules_back:
                    workspace_archive.discard_rolled_back_archive(config, archived)
                else:
                    workspace_archive.mark_rolled_back(config, archived)
                    _refresh_project_manager_workspaces(request)
                    return JSONResponse(
                        {
                            "error": (
                                f"could not archive '{name}': {failure}; the "
                                "workspace was left in place, but its automations "
                                "could not be saved back and are kept in "
                                f"{archived['path']}/{workspace_archive.METADATA_FILE}"
                            )
                        },
                        status_code=500,
                    )
            if undo_error:
                # Not refreshed: the manager would recreate the General folder
                # at the old path, where the archived folder has to go back.
                return JSONResponse(
                    {
                        "error": (
                            f"could not archive '{name}': {failure}; {undo_error}. "
                            f"It is still registered and its folder is in "
                            f"{archived['path']}"
                        )
                    },
                    status_code=500,
                )
            # Chat archival may have removed projects from memory before failing;
            # the refresh recreates the General project of the registered workspace.
            _refresh_project_manager_workspaces(request)
            return JSONResponse(
                {
                    "error": (
                        f"could not archive '{name}': {failure}; the workspace "
                        "was left in place, retry the archive"
                    )
                },
                status_code=500,
            )

        archive_chats = getattr(pcm, "archive_workspace_projects", None)
        if callable(archive_chats):
            try:
                archive_chats(name)
            except Exception as exc:  # noqa: BLE001 - unregistering now would orphan the chats
                # Unregistering with the chat registry removal not saved would
                # bring the chats back at the next start, routed through the
                # primary workspace.
                logger.exception("Could not archive every chat of workspace %s", name)
                return _roll_back(f"its chats could not be archived ({exc})")
        try:
            workspace_archive.unregister(config, name)
        except OSError as exc:
            logger.exception("Could not save the registry after archiving %s", name)
            return _roll_back(f"the workspace registry could not be saved ({exc})")
        _refresh_project_manager_workspaces(request)
        _publish_workspaces_changed(request)
        if schedules:
            _publish_automations_changed(request)

        def _forget() -> tuple[int, bool]:
            rows = 0
            rebuilt = False
            try:
                rows = workspace_archive.forget_search_rows(config, target.vault)
            except Exception:  # noqa: BLE001 - derived state; the next index pass prunes it
                logger.exception("Could not drop search rows for archived workspace %s", name)
            try:
                rebuilt = workspace_archive.refresh_shared_index(config)
            except Exception:  # noqa: BLE001 - regenerated at the next startup
                logger.exception("Could not rebuild INDEX.md after archiving %s", name)
            return rows, rebuilt

        search_rows, index_rebuilt = await asyncio.to_thread(_forget)
        await _resync_shared_skills(config, name, "archiving")
        payload = _workspaces_payload(config)
        payload["archived"] = {
            "id": archived["id"],
            "name": name,
            "path": archived["path"],
            "archived_at": archived["archived_at"],
            "projects": int(summary.get("projects", 0)),
            "chats": int(summary.get("chats", 0)),
            "schedules": len(schedules),
            "search_rows_removed": search_rows,
            "index_rebuilt": index_rebuilt,
        }
        return JSONResponse(payload)


async def list_archived_workspaces(request: Request) -> JSONResponse:
    """Archived workspaces, newest first, with whether each can be restored."""
    from ciao import workspace_archive  # noqa: PLC0415

    config = request.app.state.config
    archives = await asyncio.to_thread(workspace_archive.list_archives, config)
    return JSONResponse({"archived": archives})


async def restore_archived_workspace(request: Request) -> JSONResponse:
    """Move an archived workspace back and re-register it."""
    from ciao import workspace_archive  # noqa: PLC0415

    config = request.app.state.config
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected an object"}, status_code=400)
    archive_id = str(body.get("id") or "").strip()
    async with _workspace_archive_lock(request):
        try:
            folder, metadata, destination = await asyncio.to_thread(
                workspace_archive.move_back, config, archive_id
            )
        except workspace_archive.WorkspaceArchiveError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        # The registry is mutated here, on the event loop, never in the worker:
        # other handlers iterate ``config.workspaces`` on this thread.
        try:
            restored = workspace_archive.register_restored(
                config, folder, metadata, destination
            )
        except workspace_archive.WorkspaceArchiveError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        name = str(restored["name"])
        pcm = getattr(request.app.state, "project_chat_manager", None)

        def _foreign_target(kind: str, target_id: str) -> bool:
            # A chat or project that resolves right now belongs to another
            # workspace: this one's were archived with it, and its projects
            # are only rediscovered by the refresh below.
            if pcm is None:
                return False
            if kind == "chat":
                chat = pcm.get_chat(target_id)
                project = pcm.get_project(chat.project_id) if chat is not None else None
                return chat is not None and getattr(project, "workspace", "") != name
            project = pcm.get_project(target_id)
            return project is not None and getattr(project, "workspace", "") != name

        # archive.json is synced through git, so its rows are untrusted input:
        # rebuilt, pinned to this workspace and paused (see
        # ``schedules.restorable_user_schedule``).
        schedules, dropped = workspace_archive.restorable_schedules(
            config, restored, foreign_target=_foreign_target
        )
        added = 0
        manager = _schedule_manager(request)
        put_back = getattr(manager, "put_back_user_items", None)
        if callable(put_back) and schedules:
            try:
                added = int(put_back(schedules))
            except Exception as exc:  # noqa: BLE001 - archive.json is their only copy
                logger.exception("Could not put back the schedules of %s", name)
                try:
                    workspace_archive.unregister_restored(
                        config, folder, metadata, destination
                    )
                except workspace_archive.WorkspaceArchiveError as undo_exc:
                    _refresh_project_manager_workspaces(request)
                    return JSONResponse(
                        {
                            "error": (
                                f"'{name}' was restored but its schedules could not "
                                f"be saved ({exc}), and undoing the restore failed: "
                                f"{undo_exc.message}. The schedules are kept in "
                                f"{workspace_archive.ARCHIVE_DIR_NAME}/{restored['id']}/"
                                f"{workspace_archive.METADATA_FILE}"
                            )
                        },
                        status_code=500,
                    )
                return JSONResponse(
                    {
                        "error": (
                            f"could not restore '{name}': its schedules could not "
                            f"be saved ({exc}); the archive was left in place"
                        )
                    },
                    status_code=500,
                )
        # Only now: until the schedules are back, archive.json is their only copy.
        workspace_archive.discard_archive_folder(folder)
        _refresh_project_manager_workspaces(request)
        _publish_workspaces_changed(request)
        if added:
            _publish_automations_changed(request)
        try:
            await asyncio.to_thread(workspace_archive.refresh_shared_index, config)
        except Exception:  # noqa: BLE001 - regenerated at the next startup
            logger.exception("Could not rebuild INDEX.md after restoring %s", name)
        if getattr(config, "_rerooted", lambda: False)():
            # The root's guide and assets came back with it, but its skill mirrors
            # may point at a packaged catalog that changed while it was archived.
            await _bootstrap_new_agent_root(config, name)
        else:
            await _resync_shared_skills(config, name, "restoring")
        payload = _workspaces_payload(config)
        payload["restored"] = {
            "id": archive_id,
            "name": name,
            "path": restored.get("restored_to", ""),
            # Every restored automation is paused until re-enabled.
            "schedules": added,
            "schedules_paused": added,
            "schedules_dropped": dropped,
        }
        return JSONResponse(payload)


def _env_path(config) -> Path:
    return Path(config.workspace_root).resolve() / ".env"


def _read_env_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _write_env_values(path: Path, updates: dict[str, str]) -> None:
    lines = _read_env_lines(path)
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key not in remaining:
            out.append(line)
            continue
        value = remaining.pop(key).strip()
        if value:
            out.append(f"{key}={value}")
    for key, value in remaining.items():
        value = value.strip()
        if value:
            out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    # .env carries provider keys and PWA_AUTH_TOKEN, so it must be owner-only
    # from creation, not only after a follow-up chmod
    write_private_text(path, "\n".join(out).rstrip() + "\n")


def _provider_config_payload(config) -> dict:
    providers = setup_status(config, env=os.environ).get("providers", {})

    return {
        # Each row carries its own labels so the Settings card does not have to
        # map provider ids to names; a new provider gets a correct card for
        # free instead of falling through to another provider's label.
        "connections": {
            descriptor.id: {
                **providers[descriptor.id],
                "label": descriptor.cli_label,
                "short_label": descriptor.short_label,
            }
            for descriptor in provider_registry.descriptors()
            if descriptor.id in providers
        },
    }


def _launch_provider_login(config, provider: str) -> tuple[bool, str]:
    """Open the provider-owned interactive login in macOS Terminal."""
    if not provider_registry.is_provider(provider):
        raise ValueError(f"unsupported provider '{provider}'")
    command = _auth_command_for_provider(provider)
    rendered = shlex.join(command)
    if sys.platform != "darwin":
        return False, rendered
    runtime_root = Path(config.state_path).parent
    runtime_root.mkdir(parents=True, exist_ok=True)
    script = runtime_root / f"provider-login-{provider}.command"
    script.write_text(
        "#!/bin/zsh\n"
        "script_path=$0\n"
        "rm -f -- \"$script_path\"\n"
        f"{rendered}\n"
        "status=$?\n"
        "echo\n"
        "echo 'Authentication finished. You can close this window.'\n"
        "exit $status\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    subprocess.Popen(
        ["/usr/bin/open", "-a", "Terminal", str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True, rendered


async def provider_connection_action(request: Request) -> JSONResponse:
    provider = request.path_params["provider"]
    action = request.path_params["action"]
    config = request.app.state.config
    if not provider_registry.is_provider(provider):
        return JSONResponse({"error": "unsupported provider"}, status_code=404)
    if action == "connect":
        try:
            opened, command = await asyncio.to_thread(_launch_provider_login, config, provider)
        except (FileNotFoundError, OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "opened": opened, "command": command}, status_code=202)
    if action == "verify":
        if provider == "claude":
            from ciao.setup_status import clear_claude_discovery_cache

            await asyncio.to_thread(clear_claude_discovery_cache)
        payload = await asyncio.to_thread(_provider_config_payload, config)
        return JSONResponse(payload["connections"].get(provider, {}))
    if action == "logout":
        try:
            # auth_command is `[binary, "auth", "login"]`, so dropping the last
            # element yields `[binary, "auth", "logout"]`. `[:1] + ["logout"]`
            # would run the obsolete top-level `claude logout`, which prints a
            # "did you mean" hint and exits 0 without signing out.
            logout_command = _auth_command_for_provider(provider)[:-1] + ["logout"]
            run = await asyncio.to_thread(
                subprocess.run,
                logout_command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if run.returncode != 0:
            return JSONResponse(
                {"error": (run.stderr or run.stdout or "logout failed").strip()},
                status_code=400,
            )
        return JSONResponse({"ok": True})
    return JSONResponse({"error": "unsupported action"}, status_code=404)


async def provider_config_settings(request: Request) -> JSONResponse:
    config = request.app.state.config
    return JSONResponse(await asyncio.to_thread(_provider_config_payload, config))


def _gws_profile_config_dir(config, profile: str) -> Path | None:
    # Single source of truth lives in ciao.gws_auth so the health monitor and
    # re-login manager map profiles to credential dirs the same way.
    from ciao import gws_auth

    return gws_auth.profile_config_dir(config, profile)


def _gws_file_present(config_dir: Path | None, names: tuple[str, ...]) -> bool:
    if config_dir is None:
        return False
    return any((config_dir / name).is_file() for name in names)


def _gws_profile_usage(config) -> dict[str, list[str]]:
    """Workspaces per profile.

    Only explicit links count: a workspace with no account selected is listed
    under none, rather than being attributed to an operator-level default it
    never chose.
    """
    usage: dict[str, list[str]] = {}
    for workspace in config.workspaces.values():
        profile = str(getattr(workspace, "gws_profile", "") or "").strip()
        if not profile:
            continue
        usage.setdefault(profile, []).append(getattr(workspace, "name", ""))
    return usage


def _gws_profile_names(config) -> list[str]:
    """The Google accounts to show: the ones the user added or connected.

    No built-in list: a fresh install shows none until an account is added.
    """
    from ciao import gws_auth

    return gws_auth.known_profiles(config)


def _ensure_gws_profile_registered(config, profile: str) -> None:
    """Record ``profile`` in the account registry if it is not there yet.

    Disconnecting deletes the credential files, which is also all that makes an
    unregistered (pre-registry or terminal-created) account discoverable. The
    account itself must survive that, or "Disconnect" would silently delete the
    card and leave no way back to it.
    """
    from ciao import gws_auth

    entries = gws_auth.load_profile_registry(config)
    if any(entry["name"] == profile for entry in entries):
        return
    label = str(_GWS_PROFILE_META.get(profile, {}).get("label", "")) or f"{profile} Google account"
    entries.append({"name": profile, "label": label})
    try:
        gws_auth.save_profile_registry(config, entries)
    except OSError:
        logger.exception("Failed to persist the Google account registry")


def _valid_gws_profile(profile: object) -> str:
    """Return the profile slug, or "" when the name cannot address a directory."""
    from ciao import gws_auth

    if not isinstance(profile, str):
        return ""
    return gws_auth.slugify_profile(profile)


def _gws_profile_payload(
    config,
    profile: str,
    usage: dict[str, list[str]],
    health: dict | None = None,
    labels: dict[str, str] | None = None,
) -> dict:
    custom_label = (labels or {}).get(profile, "")
    meta = _GWS_PROFILE_META.get(
        profile,
        {
            "label": custom_label or f"{profile} Google account",
            "purpose": "Google account you added. Link it to the workspaces that should use it.",
        },
    )
    if custom_label:
        meta = {**meta, "label": custom_label}
    config_dir = _gws_profile_config_dir(config, profile)
    credentials_present = _gws_file_present(config_dir, _GWS_AUTH_FILES)
    client_secret_present = _gws_file_present(config_dir, ("client_secret.json",))
    # These go through the `ciao` CLI that ships inside the installed app, so
    # the same commands work on a dev checkout and on an installed Ciaobot.app.
    setup_command = f"ciao gws {profile} auth login --full"
    headless_auth_command = f"ciao gws-auth-helper {profile}"

    from ciao import gws_auth

    email = ""
    chips: list[str] = []
    if config_dir:
        creds_path = config_dir / "credentials.json"
        if creds_path.is_file():
            try:
                with open(creds_path, "r", encoding="utf-8") as f:
                    creds_data = json.load(f)
                email = creds_data.get("email") or ""
                # gws_auth owns both the shape tolerance and the label
                # catalogue, so a scope added to its scope sets cannot show up
                # here as a raw URL without someone naming it there first.
                chips = gws_auth.scope_labels(creds_data.get("scopes"))
            except Exception:
                pass

    # Connections made before scopes were recorded have none, and keep the
    # profile's standing description and curated chip list.
    examples = chips or list(meta.get("examples") or [])
    purpose = _gws_purpose_with_chips(meta["purpose"], chips) if chips else meta["purpose"]

    # Cached token-health snapshot from the periodic monitor (issue #145).
    # Read-only and cheap — never runs the `auth status` subprocess here.
    token_valid: bool | None = None
    token_error = ""
    needs_relogin = False
    if credentials_present and isinstance(health, dict) and "token_valid" in health:
        token_valid = bool(health.get("token_valid"))
        token_error = str(health.get("token_error") or "")
        needs_relogin = not token_valid

    # Installed/desktop OAuth clients accept any loopback port, so the one-click
    # loopback flow works for them; web clients require a registered redirect
    # URI and must use the manual paste flow instead.
    loopback_eligible = bool(gws_auth.client_uses_loopback(config_dir))

    return {
        "name": profile,
        "label": meta["label"],
        "purpose": purpose,
        "examples": examples,
        "configured": credentials_present,
        "credentials_present": credentials_present,
        "client_secret_present": client_secret_present,
        "config_dir": str(config_dir) if config_dir is not None else "",
        "workspaces": usage.get(profile, []),
        "setup_command": setup_command,
        "headless_auth_command": headless_auth_command,
        "email": email,
        "token_valid": token_valid,
        "token_error": token_error,
        "needs_relogin": needs_relogin,
        "loopback_eligible": loopback_eligible,
    }


def _gws_integration_payload(config) -> dict:
    from ciao import gws_auth

    usage = _gws_profile_usage(config)
    binary_path = resolve_tool("gws") or ""
    try:
        health = gws_auth.read_health_cache(Path(config.state_path).parent)
    except Exception:
        health = {}
    labels = {
        entry["name"]: entry["label"]
        for entry in gws_auth.load_profile_registry(config)
        if entry.get("label")
    }
    names = _gws_profile_names(config)
    # An operator default that names no existing account is not a default the
    # UI should advertise; workspaces then show "No Google account" instead.
    default_profile = GWS_DEFAULT_PROFILE
    if default_profile not in names:
        default_profile = ""
    return {
        "installed": bool(binary_path),
        "binary_path": binary_path,
        "default_profile": default_profile,
        "cli_available": bool(binary_path),
        "profiles": [
            _gws_profile_payload(config, profile, usage, health.get(profile), labels)
            for profile in names
        ],
    }


async def gws_integration_settings(request: Request) -> JSONResponse:
    return JSONResponse(_gws_integration_payload(request.app.state.config))


GWS_CLI_PACKAGE = "@googleworkspace/cli"



async def gws_save_client_secret(request: Request) -> JSONResponse:
    config = request.app.state.config
    try:
        body = await request.json()
        profile = body.get("profile")
        client_secret_str = body.get("client_secret")
    except Exception:
        return JSONResponse({"error": "Invalid request payload"}, status_code=400)

    profile = _valid_gws_profile(profile)
    if not profile:
        return JSONResponse({"error": "Invalid profile"}, status_code=400)

    if not client_secret_str:
        return JSONResponse({"error": "Missing client_secret content"}, status_code=400)

    try:
        secret_json = json.loads(client_secret_str)
        if "installed" not in secret_json and "web" not in secret_json:
            return JSONResponse({"error": "client_secret.json missing 'installed' or 'web' section"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Invalid JSON format: {str(e)}"}, status_code=400)

    config_dir = _gws_profile_config_dir(config, profile)
    if config_dir is None:
        return JSONResponse({"error": "Could not determine config directory"}, status_code=500)

    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        try:
            # the dir holds only this profile's Google OAuth material; tighten
            # it for installs whose older setup left it group/world-readable
            config_dir.chmod(0o700)
        except OSError as exc:
            logger.warning("Failed to tighten %s permissions: %s", config_dir, exc)
        path = config_dir / "client_secret.json"
        # owner-only from creation: the file carries the Google client secret
        write_private_text(path, json.dumps(secret_json, indent=2))
    except Exception as e:
        return JSONResponse({"error": f"Failed to write client_secret.json: {str(e)}"}, status_code=500)

    return JSONResponse(_gws_integration_payload(config))


def _gws_manual_pkce_store(request: Request):
    """Return the app's manual-flow PKCE verifier store, creating one on first use.

    Mirrors :func:`_gws_relogin_manager`: lazily attached so a bare test app
    (only ``config`` on ``app.state``) still works, while ``main.py`` wires
    the shared instance at startup. See ``ManualPkceStore`` (issue #354).
    """
    store = getattr(request.app.state, "gws_manual_pkce_store", None)
    if store is None:
        from ciao.gws_auth import ManualPkceStore

        store = ManualPkceStore()
        request.app.state.gws_manual_pkce_store = store
    return store


async def gws_auth_url(request: Request) -> JSONResponse:
    config = request.app.state.config
    try:
        body = await request.json()
        profile = body.get("profile")
    except Exception:
        return JSONResponse({"error": "Invalid request payload"}, status_code=400)

    profile = _valid_gws_profile(profile)
    if not profile:
        return JSONResponse({"error": "Invalid profile"}, status_code=400)

    config_dir = _gws_profile_config_dir(config, profile)
    if config_dir is None:
        return JSONResponse({"error": "Could not determine config directory"}, status_code=500)

    from ciao import gws_auth

    try:
        installed = gws_auth.load_client_secret(config_dir)
        client_id = installed.get("client_id")
        if not client_id:
            return JSONResponse({"error": "client_secret.json missing client_id"}, status_code=400)
        redirect_uris = installed.get("redirect_uris", ["http://localhost"])
        redirect_uri = redirect_uris[0]
        # PKCE (issue #354): this manual/paste flow cannot validate `state` on
        # paste-back (the user, not the browser, carries the code across the
        # trust boundary), so a code_challenge is the RFC 8252 remedy. The
        # verifier is held server-side, keyed by profile, until the matching
        # exchange call.
        flow_id, code_verifier = _gws_manual_pkce_store(request).start(profile)
        auth_url = gws_auth.build_auth_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scopes=gws_auth.scopes_for_profile(profile),
            code_challenge=gws_auth.code_challenge_s256(code_verifier),
        )
        return JSONResponse({"auth_url": auth_url, "flow_id": flow_id})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Failed to generate authorization URL: {str(e)}"}, status_code=500)


async def gws_exchange_code(request: Request) -> JSONResponse:
    config = request.app.state.config
    try:
        body = await request.json()
        profile = body.get("profile")
        code_or_url = body.get("code")
        flow_id = body.get("flow_id")
    except Exception:
        return JSONResponse({"error": "Invalid request payload"}, status_code=400)

    profile = _valid_gws_profile(profile)
    if not profile:
        return JSONResponse({"error": "Invalid profile"}, status_code=400)

    if not code_or_url:
        return JSONResponse({"error": "Missing authorization code or redirect URL"}, status_code=400)
    if flow_id is not None and not isinstance(flow_id, str):
        return JSONResponse({"error": "Invalid flow ID"}, status_code=400)

    config_dir = _gws_profile_config_dir(config, profile)
    if config_dir is None:
        return JSONResponse({"error": "Could not determine config directory"}, status_code=500)

    from ciao import gws_auth

    try:
        installed = gws_auth.load_client_secret(config_dir)
        redirect_uris = installed.get("redirect_uris", ["http://localhost"])
        redirect_uri = redirect_uris[0]
        code = gws_auth.extract_code_from_input(code_or_url)
        # The flow ID binds this exchange to the exact auth URL that produced
        # the pasted code. Without it, an old client must not accidentally use
        # another tab's verifier.
        pkce_store = _gws_manual_pkce_store(request)
        if flow_id:
            pkce_status = pkce_store.status(flow_id, profile)
        else:
            pkce_status = "none"
            if pkce_store.status_for_profile(profile) == "active":
                return JSONResponse(
                    {"error": "This sign-in flow needs a flow ID. Start manual connect again."},
                    status_code=400,
                )
        if flow_id and pkce_status == "none":
            # An explicit flow ID the store does not know cannot mean "this
            # flow never used PKCE" — the client only has an ID because the
            # auth URL it came from carried a challenge. The usual cause is a
            # server restart between building that URL and pasting the code
            # back, which drops the in-memory verifier. Exchanging anyway sends
            # a challenged code with no verifier, which Google must reject, so
            # the user would see `invalid_grant` instead of what to do next.
            return JSONResponse(
                {
                    "error": (
                        "This sign-in flow is no longer available (the server "
                        "restarted before the code was pasted back). Start "
                        "manual connect again for a fresh link."
                    )
                },
                status_code=400,
            )
        if pkce_status == "expired":
            return JSONResponse(
                {
                    "error": (
                        "This sign-in link expired before the code was pasted "
                        "back. Start manual connect again for a fresh link."
                    )
                },
                status_code=400,
            )
        if pkce_status == "superseded":
            return JSONResponse(
                {"error": "This sign-in flow was replaced. Start manual connect again."},
                status_code=400,
            )
        code_verifier = pkce_store.peek(flow_id, profile) if pkce_status == "active" else None
        # Token exchange + credential write happen off the event loop; the
        # helper never logs the code, tokens, or secret.
        await asyncio.to_thread(
            gws_auth.exchange_and_store,
            config,
            profile,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
        # Retire the flow now that its code is spent. Only on success: a failed
        # exchange (wrong code, transient error) must keep the verifier so the
        # paste can be retried. Leaving it active held a spent secret for the
        # rest of its TTL and, because `status_for_profile` then still reported
        # "active", refused any flow-ID-less exchange for this profile in the
        # meantime.
        if flow_id:
            pkce_store.consume(flow_id, profile)
        # Refresh the cached token-validity state so the Settings UI clears
        # the "Login expired" banner immediately instead of waiting up to
        # for the next periodic check. Mirrors gws_relogin_status.
        monitor = getattr(request.app.state, "gws_health_monitor", None)
        if monitor is not None:
            try:
                await asyncio.to_thread(monitor.check_once)
            except Exception:
                logger.exception("Post-exchange health refresh failed")
        return JSONResponse(_gws_integration_payload(config))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Authentication exchange failed: {str(e)}"}, status_code=500)


async def gws_disconnect(request: Request) -> JSONResponse:
    config = request.app.state.config
    try:
        body = await request.json()
        profile = body.get("profile")
        delete_client_secret = bool(body.get("delete_client_secret", False))
    except Exception:
        return JSONResponse({"error": "Invalid request payload"}, status_code=400)

    profile = _valid_gws_profile(profile)
    if not profile:
        return JSONResponse({"error": "Invalid profile"}, status_code=400)

    config_dir = _gws_profile_config_dir(config, profile)
    if config_dir is None:
        return JSONResponse({"error": "Could not determine config directory"}, status_code=500)

    # Disconnecting keeps the account; only "remove" deletes it.
    _ensure_gws_profile_registered(config, profile)

    try:
        for name in ("credentials.json", "credentials.enc", "token_cache.json",
                     "credentials.json.old", "credentials.enc.old", "token_cache.json.old"):
            path = config_dir / name
            if path.exists():
                path.unlink()
        
        if delete_client_secret:
            secret_path = config_dir / "client_secret.json"
            if secret_path.exists():
                secret_path.unlink()
    except Exception as e:
        return JSONResponse({"error": f"Failed to disconnect profile: {str(e)}"}, status_code=500)

    return JSONResponse(_gws_integration_payload(config))


async def gws_add_profile(request: Request) -> JSONResponse:
    """Register a Google account so workspaces can be linked to it.

    Adding is bookkeeping only: it records the name and label. Credentials
    arrive later through the OAuth flow, which creates the credential dir.
    """
    config = request.app.state.config
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request payload"}, status_code=400)

    from ciao import gws_auth

    raw_name = str(body.get("name", "") or "").strip()
    profile = _valid_gws_profile(raw_name)
    if not profile:
        return JSONResponse(
            {"error": "Give the account a name using letters, numbers, dashes, or underscores."},
            status_code=400,
        )
    if profile in gws_auth.GWS_SERVICE_NAMES:
        return JSONResponse(
            {
                "error": (
                    f"'{profile}' is reserved for a Google Workspace service; "
                    "choose another account name."
                )
            },
            status_code=400,
        )
    if profile in _gws_profile_names(config):
        return JSONResponse(
            {"error": f"A Google account named '{profile}' already exists."},
            status_code=400,
        )
    label = str(body.get("label", "") or "").strip() or f"{raw_name} Google account"
    entries = gws_auth.load_profile_registry(config)
    entries.append({"name": profile, "label": label})
    try:
        gws_auth.save_profile_registry(config, entries)
    except OSError as exc:
        return JSONResponse({"error": f"Failed to save the account list: {exc}"}, status_code=500)
    return JSONResponse(_gws_integration_payload(config))


async def gws_remove_profile(request: Request) -> JSONResponse:
    """Forget a Google account and delete its stored credentials.

    Workspaces pointing at it are unlinked in the same pass so the registry
    cannot keep a dangling reference to an account that no longer exists.
    """
    config = request.app.state.config
    try:
        body = await request.json()
        profile = body.get("profile")
    except Exception:
        return JSONResponse({"error": "Invalid request payload"}, status_code=400)

    from ciao import gws_auth

    profile = _valid_gws_profile(profile)
    if not profile:
        return JSONResponse({"error": "Invalid profile"}, status_code=400)

    config_dir = _gws_profile_config_dir(config, profile)
    if config_dir is not None and config_dir.is_dir():
        try:
            shutil.rmtree(config_dir)
        except OSError as exc:
            return JSONResponse(
                {"error": f"Failed to delete stored credentials: {exc}"}, status_code=500
            )

    entries = [
        entry for entry in gws_auth.load_profile_registry(config) if entry["name"] != profile
    ]
    try:
        gws_auth.save_profile_registry(config, entries)
    except OSError as exc:
        return JSONResponse({"error": f"Failed to save the account list: {exc}"}, status_code=500)

    unlinked = False
    for workspace in config.workspaces.values():
        if getattr(workspace, "gws_profile", "") == profile:
            workspace.gws_profile = ""
            unlinked = True
    if unlinked:
        config.persist_workspace_registry()

    return JSONResponse(_gws_integration_payload(config))


def _gws_relogin_manager(request: Request):
    """Return the app's re-login manager, creating one on first use.

    Lazily attached so route modules (and tests) that build a bare app with a
    ``config`` on ``app.state`` still get a working manager without extra
    wiring. ``main.py`` also attaches one at startup.
    """
    manager = getattr(request.app.state, "gws_relogin_manager", None)
    if manager is None:
        from ciao.gws_auth import GwsReloginManager

        manager = GwsReloginManager(request.app.state.config)
        request.app.state.gws_relogin_manager = manager
    return manager


async def gws_relogin_start(request: Request) -> JSONResponse:
    """Start a server-managed OAuth re-login for a profile (issue #145).

    Binds a loopback callback listener inside this long-lived process and
    returns the Google consent URL. The listener survives across chat turns
    (unlike ``gws auth login`` in a background bash task), captures the
    redirect, and exchanges the code server-side. Never returns tokens.
    """
    try:
        body = await request.json()
        profile = body.get("profile")
    except Exception:
        return JSONResponse({"error": "Invalid request payload"}, status_code=400)

    profile = _valid_gws_profile(profile)
    if not profile:
        return JSONResponse({"error": "Invalid profile"}, status_code=400)

    manager = _gws_relogin_manager(request)
    try:
        result = await asyncio.to_thread(manager.start, profile)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Failed to start re-login: {str(e)}"}, status_code=500)
    return JSONResponse(result)


async def gws_relogin_status(request: Request) -> JSONResponse:
    """Poll a pending re-login. Returns pending/completed/error/none."""
    profile = _valid_gws_profile(request.query_params.get("profile", ""))
    if not profile:
        return JSONResponse({"error": "Invalid profile"}, status_code=400)
    manager = _gws_relogin_manager(request)
    result = manager.status(profile)
    # When a re-login just completed, refresh the health cache so Settings and
    # the next status check see the profile as valid without waiting a cycle.
    if result.get("status") == "completed":
        monitor = getattr(request.app.state, "gws_health_monitor", None)
        if monitor is not None:
            try:
                await asyncio.to_thread(monitor.check_once)
            except Exception:
                logger.exception("Post-relogin health refresh failed")
    return JSONResponse(result)


async def gws_relogin_cancel(request: Request) -> JSONResponse:
    """Cancel a pending re-login and tear down its loopback listener."""
    try:
        body = await request.json()
        profile = body.get("profile")
    except Exception:
        return JSONResponse({"error": "Invalid request payload"}, status_code=400)
    profile = _valid_gws_profile(profile)
    if not profile:
        return JSONResponse({"error": "Invalid profile"}, status_code=400)
    manager = _gws_relogin_manager(request)
    return JSONResponse(manager.cancel(profile))


async def list_projects(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    workspace = request.query_params.get("workspace")
    projects = pcm.list_projects(workspace)
    return JSONResponse([p.to_dict() for p in projects])


async def create_project(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    config = request.app.state.config
    body = await request.json()
    project = pcm.create_project(
        name=body["name"],
        # An omitted workspace has to resolve to one that exists: create_project
        # does not validate the name, so an unknown one yields a project that is
        # filtered out of every workspace's sidebar.
        workspace=body.get("workspace") or config.primary_workspace(),
        context=body.get("context", ""),
    )
    return JSONResponse(project.to_dict(), status_code=201)


async def reorder_projects(request: Request) -> JSONResponse:
    """Persist a drag-reordered project sequence for one workspace."""
    pcm = request.app.state.project_chat_manager
    body = await request.json()
    workspace = body.get("workspace") or ""
    ordered_ids = body.get("order")
    if not workspace or not isinstance(ordered_ids, list):
        return JSONResponse(
            {"error": "workspace and order[] are required"}, status_code=400
        )
    projects = pcm.reorder_projects(workspace, [str(pid) for pid in ordered_ids])
    return JSONResponse([p.to_dict() for p in projects])


async def project_detail(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    project_id = request.path_params["project_id"]
    if request.method == "DELETE":
        try:
            ok = pcm.delete_project(project_id)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": ok})
    # PATCH
    body = await request.json()
    try:
        project = pcm.update_project(
            project_id,
            name=body.get("name"),
            context=body.get("context"),
            vault_folder=body.get("vault_folder"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if project is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(project.to_dict())


async def project_complete(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    project_id = request.path_params["project_id"]
    try:
        result = pcm.complete_project(project_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result)


async def list_completed_projects(request: Request) -> JSONResponse:
    """List completed (archived) projects by scanning the vault completed/ tree.

    Read-only. Optional ``workspace`` query param scopes to one workspace.
    """
    pcm = request.app.state.project_chat_manager
    workspace = request.query_params.get("workspace")
    return JSONResponse(pcm.list_completed_projects(workspace))


async def project_restore(request: Request) -> JSONResponse:
    """Restore a completed project back to active/. Body: ``{workspace, stem}``."""
    pcm = request.app.state.project_chat_manager
    body = await request.json()
    try:
        result = pcm.restore_project(
            workspace=body.get("workspace", ""),
            stem=body.get("stem", ""),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result)


async def project_chats(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    project_id = request.path_params["project_id"]
    chats = pcm.list_chats(project_id)
    return JSONResponse([c.to_dict() for c in chats])


async def project_files_list(request: Request) -> JSONResponse:
    """List files under a project's vault folder.

    Returns 200 with ``[]`` for projects without a folder-backed vault entry
    (manual projects, single-file personal projects, missing folders), so the
    UI can hide the section without distinguishing the cases.
    """
    pcm = request.app.state.project_chat_manager
    project_id = request.path_params["project_id"]
    project = pcm.get_project(project_id)
    if project is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    files = pcm.list_project_files(project_id)
    return JSONResponse(files)


async def project_files_upload(request: Request) -> JSONResponse:
    """Upload one or more files into a project's vault folder."""
    pcm = request.app.state.project_chat_manager
    project_id = request.path_params["project_id"]
    project = pcm.get_project(project_id)
    if project is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    form = await request.form()
    saved: list[dict] = []
    errors: list[dict[str, str]] = []
    upload_count = 0
    total_bytes = 0
    for key in form:
        upload = form[key]
        if not hasattr(upload, "read"):
            continue
        upload_count += 1
        filename = getattr(upload, "filename", "") or ""
        display_name = _drop_display_name(Path(filename))
        if upload_count > _DESKTOP_DROP_MAX_FILES:
            errors.append({"filename": "file", "error": "too many files"})
            break
        try:
            data = await _read_upload_limited(upload, chat_service._PROJECT_UPLOAD_MAX_BYTES)
            total_bytes += len(data)
            if total_bytes > _DESKTOP_DROP_MAX_TOTAL_BYTES:
                errors.append({"filename": display_name, "error": "upload is too large"})
                continue
            entry = pcm.save_project_file_upload(project_id, data, filename)
            relative_path = Path(str(entry.get("path", "")))
            safe_path = (
                ""
                if relative_path.is_absolute() or ".." in relative_path.parts
                else relative_path.as_posix()
            )
            saved.append(
                {
                    "path": safe_path,
                    "kind": str(entry.get("kind", "binary"))[:32],
                    "size": int(entry.get("size", 0) or 0),
                    "mtime": str(entry.get("mtime", ""))[:64],
                }
            )
        except LookupError as exc:
            # Project has no vault folder to upload into. Same status across
            # all uploads in this request — return 409 immediately.
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (OSError, ValueError) as exc:
            errors.append({
                "filename": display_name,
                "error": _safe_desktop_drop_error(Path(filename), exc),
            })
    return JSONResponse({"saved": saved, "errors": errors})

async def chat_attachments_upload(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    chat = pcm.get_chat(request.path_params["chat_id"])
    if chat is None:
        return JSONResponse({"error": "chat not found"}, status_code=404)
    form = await request.form()
    saved: list[dict] = []
    errors: list[dict[str, str]] = []
    total_bytes = 0
    for key in form:
        upload = form[key]
        if not hasattr(upload, "read"):
            continue
        if len(saved) + len(errors) >= _DESKTOP_DROP_MAX_FILES:
            errors.append({"filename": "file", "error": "too many files"})
            break
        filename = getattr(upload, "filename", "") or ""
        display_name = _drop_display_name(Path(filename))
        try:
            data = await _read_upload_limited(upload, chat_service._PROJECT_UPLOAD_MAX_BYTES)
            total_bytes += len(data)
            if total_bytes > _DESKTOP_DROP_MAX_TOTAL_BYTES:
                errors.append({"filename": display_name, "error": "upload is too large"})
                continue
            entry = await asyncio.to_thread(
                pcm.save_chat_attachment_upload, chat.project_id, data, filename
            )
            source = entry.get("markdown_path") or entry.get("absolute_path")
            if not source:
                raise ValueError("upload produced no file")
            ref = pcm.register_file_ref(chat.chat_id, Path(str(source)))
            saved.append({"ref": ref, "name": display_name})
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (ValueError, RuntimeError, OSError) as exc:
            errors.append(
                {
                    "filename": display_name,
                    "error": _safe_desktop_drop_error(Path(filename), exc),
                }
            )
    return JSONResponse({"file_refs": saved, "errors": errors})


_DESKTOP_DROP_GRANT_TTL_SECONDS = 5 * 60
_DESKTOP_DROP_MAX_FILES = 100
_DESKTOP_DROP_MAX_PATH_BYTES = 4096
_DESKTOP_DROP_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_DESKTOP_DROP_MAX_ERROR_BYTES = 512
_DESKTOP_DROP_MAX_GRANT_BYTES = 512 * 1024


def _read_native_file_limited(path: Path, max_bytes: int) -> bytes:
    """Read a dropped file without trusting a size that can change after stat."""
    if max_bytes < 0:
        raise ValueError("invalid file size limit")
    with path.open("rb") as source:
        data = source.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("file too large")
    return data


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8", "surrogatepass"))


def _looks_like_nsird_screenshot(path: Path) -> bool:
    """True if ``path`` points into a macOS ``screencaptureui`` staging directory.

    Files freshly captured to the clipboard by ``screencaptureui`` land under
    ``.../TemporaryItems/NSIRD_screencaptureui_<id>/Screenshot *.png`` and the
    kernel only lets the capturing process read them, so another process
    reading them raises ``OSError`` (EPERM). Detect the case by path: match a
    ``NSIRD_`` path component rather than the errno (EPERM and EACCES both map
    to ``PermissionError``, so the subclass tells us nothing) or the literal
    ``screencaptureui`` name, which the id suffix changes per capture.
    """
    return any(part.startswith("NSIRD_") for part in path.parts)


def _desktop_drop_read_error(path: Path, exc: OSError) -> str:
    """User-facing text for a dropped file this process cannot read.

    A drag straight from the macOS screenshot thumbnail hands over a path only
    the app that received the drop may read, so the desktop shell stages a copy
    first (`stage_dropped_file` in desktop/src-tauri/src/lib.rs). When there is
    no staged copy, because the shell is older than that fix or the drop was not
    an image, the raw errno tells the user nothing they can act on.

    Four tiers, narrowest first. An NSIRD path gets screenshot-specific advice.
    ``EDEADLK`` means a cloud placeholder (see below). Any other permission
    denial still gets actionable text, just without naming a screenshot, so a
    plain unreadable drop is not mislabelled and does not regress to a raw
    errno. Unknown filesystem errors use a bounded generic message rather than
    echoing an errno string that may contain the source path.
    """
    if _looks_like_nsird_screenshot(path):
        return (
            "macOS won't let us read this screenshot directly. "
            "Save it to disk first, then drag it in."
        )
    if exc.errno == errno.EDEADLK:
        # A file dragged out of iCloud Drive (or any other File Provider) whose
        # bytes are not on disk: `stat` reports the real size, so the grant's
        # existence check passes, and the read is then refused with EDEADLK
        # ("Resource deadlock avoided") because this process may not ask the
        # provider to materialise it. Unlike EPERM the errno is unambiguous
        # here, so it needs no corroborating path check. The desktop shell
        # stages unreadable drops past this (`needs_drop_staging` in
        # desktop/src-tauri/src/lib.rs); a file over the staging limit, an
        # older shell, or a client node transferring a non-image still lands
        # here. A non-image dropped on a host does not: the path is handed to
        # the agent unread, so the agent hits the same errno on its own.
        return (
            f"{_drop_display_name(path)} is not downloaded to this Mac yet. Right-click it in "
            "Finder, choose Download Now, then drag it in again."
        )
    if isinstance(exc, PermissionError):
        return (
            f"macOS would not let Ciaobot read {_drop_display_name(path)}. Save the file to a "
            "folder first, then drag it in."
        )
    # Do not echo arbitrary OSError text: errno implementations commonly append
    # the source filename, which would turn a per-file error into an absolute
    # path disclosure.  The dropped basename is enough for the user to act.
    return f"Ciaobot could not read {_drop_display_name(path)}. Save the file to a folder and try again."


def _safe_desktop_drop_error(path: Path, exc: Exception) -> str:
    if isinstance(exc, OSError):
        return _desktop_drop_read_error(path, exc)
    message = " ".join(str(exc).split()) or "could not be imported"
    display_name = _drop_display_name(path)
    candidates = [str(path), display_name]
    try:
        candidates.append(str(path.resolve(strict=False)))
    except OSError:
        pass
    for candidate in candidates:
        if candidate:
            message = message.replace(candidate, display_name)
    # A conversion/import failure can mention a different source path than the
    # browser filename.  Redact any remaining absolute-looking token before the
    # message crosses the drop API boundary.
    message = re.sub(
        r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|/)[^\s\"']*",
        display_name,
        message,
    )
    return message[:_DESKTOP_DROP_MAX_ERROR_BYTES]


def _safe_remote_drop_error(value: object) -> str:
    """Bound a host-side per-file error without reflecting its filesystem."""
    message = str(value or "").strip()
    if (
        not message
        or "/" in message
        or "\\" in message
        or "\x00" in message
        or any(not char.isprintable() for char in message)
    ):
        return "host could not import this file"
    return message[:_DESKTOP_DROP_MAX_ERROR_BYTES]


def _safe_image_ref(value: object) -> str | None:
    """Accept only a bounded, path-free image reference from a host."""
    ref = str(value or "").strip()
    if (
        not ref
        or len(ref) > 128
        or "/" in ref
        or "\\" in ref
        or "\x00" in ref
        or any(not char.isprintable() for char in ref)
    ):
        return None
    return ref


def _desktop_drop_ref(pcm, chat_id: str, path: Path) -> dict[str, str]:
    ref = pcm.register_file_ref(chat_id, path)
    return {"ref": ref, "name": _drop_display_name(path)}


def _drop_display_name(path: Path) -> str:
    name = "".join(char for char in path.name if char.isprintable()).strip()
    return (name or "file")[:255]


def _clear_desktop_drop_staging(
    request: Request,
    grant_id: str,
    *,
    keep_paths: set[Path] | None = None,
) -> None:
    try:
        if str(UUID(grant_id)) != grant_id:
            return
    except (ValueError, AttributeError):
        return
    grant_dir = request.app.state.config.state_path.parent / "desktop-drop-grants"
    staged_dir = grant_dir / "staged" / grant_id
    keep = {path.resolve(strict=False) for path in (keep_paths or set())}
    try:
        for index_dir in staged_dir.iterdir():
            if not index_dir.is_dir():
                continue
            for staged in index_dir.iterdir():
                try:
                    if staged.resolve(strict=False) in keep:
                        continue
                except OSError:
                    pass
                if staged.is_file() or staged.is_symlink():
                    staged.unlink(missing_ok=True)
            try:
                index_dir.rmdir()
            except OSError:
                pass
        staged_dir.rmdir()
    except OSError:
        pass


def _consume_desktop_drop_grant(request: Request, grant_id: str) -> list[Path]:
    """Consume a native-app grant and return only its explicitly dropped paths."""
    try:
        canonical_id = str(UUID(grant_id))
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid desktop drop grant") from exc
    if canonical_id != grant_id:
        raise ValueError("invalid desktop drop grant")

    config = request.app.state.config
    grant_dir = config.state_path.parent / "desktop-drop-grants"
    source = grant_dir / f"{grant_id}.json"
    consuming = grant_dir / f".{grant_id}.consuming"
    try:
        source.replace(consuming)
    except FileNotFoundError as exc:
        raise LookupError("desktop drop grant not found or already used") from exc

    try:
        if consuming.stat().st_size > _DESKTOP_DROP_MAX_GRANT_BYTES:
            raise ValueError("invalid desktop drop grant")
        with consuming.open("rb") as source:
            raw_grant = source.read(_DESKTOP_DROP_MAX_GRANT_BYTES + 1)
        if len(raw_grant) > _DESKTOP_DROP_MAX_GRANT_BYTES:
            raise ValueError("invalid desktop drop grant")
        payload = json.loads(raw_grant.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("invalid desktop drop grant")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid desktop drop grant") from exc
    finally:
        consuming.unlink(missing_ok=True)

    if not isinstance(payload, dict):
        raise ValueError("invalid desktop drop grant")
    created_at = payload.get("created_at")
    raw_paths = payload.get("paths")
    if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
        raise ValueError("invalid desktop drop grant")
    try:
        created_at_seconds = float(created_at)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("invalid desktop drop grant") from exc
    if not math.isfinite(created_at_seconds):
        raise ValueError("invalid desktop drop grant")
    age = datetime.now(UTC).timestamp() - created_at_seconds
    if age < -30 or age > _DESKTOP_DROP_GRANT_TTL_SECONDS:
        # The two failure modes are different bugs and must not be reported
        # identically. Log the grant id, timestamp, age and reason so a 400
        # leaves evidence even though the grant file is already consumed.
        reason = (
            "grant timestamp is in the future"
            if age < -30
            else "grant is too old"
        )
        logger.warning(
            "Rejecting desktop drop grant %s: %s "
            "(created_at=%s, age=%.1fs, ttl=%ds)",
            grant_id,
            reason,
            created_at,
            age,
            _DESKTOP_DROP_GRANT_TTL_SECONDS,
        )
        raise ValueError(f"desktop drop grant expired: {reason}")
    if (
        not isinstance(raw_paths, list)
        or not raw_paths
        or len(raw_paths) > _DESKTOP_DROP_MAX_FILES
        or not all(isinstance(path, str) for path in raw_paths)
        or any(_utf8_size(path) > _DESKTOP_DROP_MAX_PATH_BYTES for path in raw_paths)
    ):
        raise ValueError("invalid desktop drop grant")

    paths = [Path(path) for path in raw_paths]
    total_bytes = 0
    for path in paths:
        if not path.is_absolute() or not path.exists():
            raise ValueError("a dropped file is no longer available")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ValueError("a dropped file is no longer available") from exc
        total_bytes += size
        if total_bytes > _DESKTOP_DROP_MAX_TOTAL_BYTES:
            raise ValueError("desktop drop is too large")
    return paths


async def desktop_drop_import(request: Request) -> JSONResponse:
    grant_id = ""
    keep_paths: set[Path] = set()
    preserve_staged = False
    try:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        grant_id = str(body.get("grant_id") or "")
        project_id = str(body.get("project_id") or "")
        chat_id = str(body.get("chat_id") or "")
        try:
            paths = _consume_desktop_drop_grant(request, grant_id)
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        node_mgr = getattr(request.app.state, "node_state_manager", None)
        from ciao.web.remote_boundary import is_client_mode, is_invalid_node_state

        if is_invalid_node_state(request):
            return JSONResponse({"error": "node state is invalid"}, status_code=503)
        is_client = is_client_mode(request)
        image_paths = [
            path
            for path in paths
            if path.is_file() and path.suffix.lower() in _ALLOWED_IMAGE_EXTENSIONS
        ]
        regular_paths = [path for path in paths if path not in image_paths]
        errors: list[dict[str, str]] = []
        file_refs: list[dict[str, str]] = []

        if not is_client:
            pcm = getattr(request.app.state, "project_chat_manager", None)
            if pcm is None:
                return JSONResponse({"error": "project chat manager unavailable"}, status_code=503)
            chat = pcm.get_chat(chat_id)
            if chat is None:
                return JSONResponse({"error": "chat not found"}, status_code=404)
            project_id = chat.project_id
            image_refs: list[str] = []
            for path in image_paths:
                try:
                    if path.stat().st_size > MAX_IMAGE_SIZE_BYTES:
                        raise ValueError("image too large")
                    image_data = await asyncio.to_thread(
                        _read_native_file_limited, path, MAX_IMAGE_SIZE_BYTES
                    )
                    image_refs.append(pcm.save_image_upload(image_data, path.name).path.name)
                except (OSError, ValueError) as exc:
                    errors.append(
                        {
                            "filename": _drop_display_name(path),
                            "error": _safe_desktop_drop_error(path, exc),
                        }
                    )
            for path in regular_paths:
                if not path.is_file():
                    errors.append(
                        {
                            "filename": _drop_display_name(path),
                            "error": "folders cannot be attached",
                        }
                    )
                    continue
                try:
                    if is_anydoc_document(path.name):
                        converted = await asyncio.to_thread(
                            pcm.convert_chat_document, project_id, path
                        )
                        generated = Path(str(converted.get("markdown_path") or ""))
                        if not generated.is_file():
                            raise ValueError("document conversion produced no file")
                        file_refs.append(_desktop_drop_ref(pcm, chat_id, generated))
                        keep_paths.add(path)
                    else:
                        file_refs.append(_desktop_drop_ref(pcm, chat_id, path))
                        keep_paths.add(path)
                except (OSError, LookupError, RuntimeError, ValueError) as exc:
                    errors.append(
                        {
                            "filename": _drop_display_name(path),
                            "error": _safe_desktop_drop_error(path, exc),
                        }
                    )
            preserve_staged = True
            return JSONResponse(
                {
                    "file_refs": file_refs,
                    "image_refs": image_refs,
                    "errors": errors,
                }
            )

        if node_mgr is None:
            return JSONResponse({"error": "client node state unavailable"}, status_code=503)
        host_url = node_mgr.get_active_peer_url()
        from ciao.node_state import peer_url_is_allowed

        if not host_url:
            return JSONResponse({"error": "client has no reachable host"}, status_code=503)
        if not peer_url_is_allowed(host_url, str(request.url.scheme or "")):
            return JSONResponse({"error": "client peer transport is not allowed"}, status_code=503)

        import httpx

        from ciao.web.auth import SESSION_COOKIE

        headers = {
            "origin": host_url.rstrip("/"),
            "x-ciao-desktop-drop": "1",
        }
        host_session = node_mgr.get_host_session()
        if host_session:
            headers["cookie"] = f"{SESSION_COOKIE}={host_session}"
        timeout = httpx.Timeout(10 * 60.0, connect=5.0)
        client_image_refs: list[str] = []

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            image_files = []
            for index, path in enumerate(image_paths):
                try:
                    if path.stat().st_size > MAX_IMAGE_SIZE_BYTES:
                        errors.append(
                            {"filename": _drop_display_name(path), "error": "image too large"}
                        )
                        continue
                    data = await asyncio.to_thread(
                        _read_native_file_limited, path, MAX_IMAGE_SIZE_BYTES
                    )
                except (OSError, ValueError) as exc:
                    errors.append(
                        {
                            "filename": _drop_display_name(path),
                            "error": _safe_desktop_drop_error(path, exc),
                        }
                    )
                    continue
                image_files.append(
                    (
                        f"file{index}",
                        (
                            path.name,
                            data,
                            mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                        ),
                    )
                )
            if image_files:
                response = await client.post(
                    f"{host_url.rstrip('/')}/api/chats/{chat_id}/images",
                    headers=headers,
                    files=image_files,
                )
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                if not response.is_success or not isinstance(payload, list):
                    raise ValueError("host image upload failed")
                for entry in payload:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("ref"):
                        image_ref = _safe_image_ref(entry.get("ref"))
                        if image_ref:
                            client_image_refs.append(image_ref)
                    elif entry.get("error"):
                        errors.append(
                            {
                                "filename": _drop_display_name(Path(str(entry.get("filename") or "file"))),
                                "error": _safe_remote_drop_error(entry["error"]),
                            }
                        )

            files: list[tuple[str, tuple[str, bytes, str]]] = []
            for path in regular_paths:
                if not path.is_file():
                    errors.append(
                        {
                            "filename": _drop_display_name(path),
                            "error": "folders cannot be transferred to the host",
                        }
                    )
                    continue
                try:
                    if path.stat().st_size > chat_service._PROJECT_UPLOAD_MAX_BYTES:
                        errors.append(
                            {"filename": _drop_display_name(path), "error": "file too large"}
                        )
                        continue
                    data = await asyncio.to_thread(
                        _read_native_file_limited,
                        path,
                        chat_service._PROJECT_UPLOAD_MAX_BYTES,
                    )
                except (OSError, ValueError) as exc:
                    errors.append(
                        {
                            "filename": _drop_display_name(path),
                            "error": _safe_desktop_drop_error(path, exc),
                        }
                    )
                    continue
                files.append(
                    (
                        f"file{len(files)}",
                        (
                            path.name,
                            data,
                            mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                        ),
                    )
                )
            if files:
                response = await client.post(
                    f"{host_url.rstrip('/')}/api/chats/{chat_id}/attachments",
                    headers=headers,
                    files=files,
                )
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                if not response.is_success or not isinstance(payload, dict):
                    raise ValueError("host file upload failed")
                for entry in payload.get("file_refs", []):
                    if not isinstance(entry, dict):
                        continue
                    ref = str(entry.get("ref") or "")
                    if not ref or not re.fullmatch(r"drop_[0-9a-f]{32}", ref):
                        continue
                    file_refs.append(
                        {
                            "ref": ref,
                            "name": _drop_display_name(Path(str(entry.get("name") or "file"))),
                        }
                    )
                for entry in payload.get("errors", []):
                    if isinstance(entry, dict):
                        errors.append(
                            {
                                "filename": _drop_display_name(Path(str(entry.get("filename") or "file"))),
                                "error": _safe_remote_drop_error(entry.get("error") or "upload failed"),
                            }
                        )
        return JSONResponse(
            {"file_refs": file_refs, "image_refs": client_image_refs, "errors": errors}
        )
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": _safe_desktop_drop_error(Path("file"), exc)}, status_code=502)
    except Exception:
        logger.exception("Desktop drop import failed")
        return JSONResponse({"error": "desktop drop import failed"}, status_code=500)
    finally:
        if grant_id:
            _clear_desktop_drop_staging(
                request,
                grant_id,
                keep_paths=keep_paths if preserve_staged else set(),
            )


async def create_project_chat(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    project_id = request.path_params["project_id"]
    body = await request.json()
    try:
        chat = pcm.create_chat(
            project_id,
            title=body.get("title", "New Chat"),
            model=body.get("model"),
            mode=body.get("mode"),
            provider=body.get("provider"),
            helper=body.get("helper"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(chat.to_dict(local=True), status_code=201)


# ── Chats ────────────────────────────────────────────────────────────────

async def list_all_chats(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    # `?active_only=1` skips archived chats. The PWA's frequent syncLatest poll
    # uses it: the archive can be huge (thousands of rows) and is not needed to
    # keep the sidebar/home in sync — archived rows are already held locally and
    # loaded once at boot. Filter BEFORE the per-chat `is_session_local` probe
    # (filesystem stat) so the poll never touches the archived rows at all.
    active_only = request.query_params.get("active_only") in {"1", "true"}
    if active_only:
        chats = [c for c in pcm.list_chats() if not c.archived]
        return JSONResponse([c.to_dict(local=True) for c in chats])
    return JSONResponse(pcm.list_chats_dicts())


async def chat_detail(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    if request.method == "DELETE":
        import logging
        # Log who deletes a chat so an unexpected deletion is auditable: the
        # explicit close (only_if_empty) vs a plain DELETE.
        logging.getLogger("ciao.web.routes").info(
            "chat DELETE %s only_if_empty=%r", chat_id,
            request.query_params.get("only_if_empty"),
        )
        # `only_if_empty` is how closing a chat discards a never-used draft.
        # The check has to happen here: "empty" means default title, no user
        # turns, no session and no live stream, and `user_turn_count` is not
        # in any payload the PWA receives — a client-side approximation of the
        # rule deletes chats the server would have kept.
        if request.query_params.get("only_if_empty") in {"1", "true"}:
            if not pcm.is_empty_chat(chat_id):
                return JSONResponse({"ok": False, "deleted": False, "reason": "not empty"})
        ok = pcm.delete_chat(chat_id)
        logging.getLogger("ciao.web.routes").info("chat DELETE %s -> ok=%r", chat_id, ok)
        return JSONResponse({"ok": ok, "deleted": ok})
    # PATCH
    body = await request.json()
    try:
        chat = pcm.update_chat(
            chat_id,
            title=body.get("title"),
            model=body.get("model"),
            provider=body.get("provider"),
            mode=body.get("mode"),
            project_id=body.get("project_id"),
            thinking_level=body.get("thinking_level"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(chat.to_dict(local=pcm.is_session_local(chat)))


async def chat_new_session(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    try:
        chat = pcm.new_session(chat_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(chat.to_dict(local=True))


async def chat_handover(request: Request) -> JSONResponse:
    """Explicitly continue a chat on a fresh provider session."""
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    body = await request.json()
    provider = str(body.get("provider", "")).strip()
    model = str(body.get("model", "")).strip()
    raw_messages = body.get("messages", [])
    messages = raw_messages if isinstance(raw_messages, list) else []
    try:
        chat = pcm.handover_chat(
            chat_id,
            provider=provider,
            model=model,
            messages=[m for m in messages if isinstance(m, dict)],
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(chat.to_dict(local=pcm.is_session_local(chat)))


async def chat_fork(request: Request) -> JSONResponse:
    """Create an independent chat from history through one final answer."""
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON body must be an object"}, status_code=400)
    messages = body.get("messages")
    if not isinstance(messages, list):
        return JSONResponse({"error": "messages must be a list"}, status_code=400)
    turn_index = body.get("turn_index")
    if (
        not isinstance(turn_index, int)
        or isinstance(turn_index, bool)
        or turn_index < 0
    ):
        return JSONResponse(
            {"error": "turn_index must be a non-negative integer"},
            status_code=400,
        )
    try:
        fork = pcm.fork_chat(
            chat_id,
            messages=[row for row in messages if isinstance(row, dict)],
            turn_index=turn_index,
        )
    except KeyError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except ValueError as exc:
        status = 404 if str(exc) == "Source project not found" else 400
        return JSONResponse({"error": str(exc)}, status_code=status)
    except Exception as exc:
        logger.exception("Failed to fork chat %s", chat_id)
        return JSONResponse({"error": f"Failed to fork chat: {exc}"}, status_code=500)
    return JSONResponse(fork.to_dict(local=True))


async def chat_continue(request: Request) -> JSONResponse:
    """Create a new active chat that continues from an archived chat."""
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    try:
        chat = pcm.continue_archived_chat(chat_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": f"Failed to continue chat: {exc}"}, status_code=500)
    return JSONResponse(chat.to_dict(local=pcm.is_session_local(chat)))


async def chat_retry(request: Request) -> JSONResponse:
    """Manage deferred retry state for a chat."""
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    body = await request.json()
    action = str(body.get("action", "try_now"))
    if action == "stop":
        chat = pcm.stop_chat_retry(chat_id)
    elif action == "set":
        prompt = str(body.get("prompt", ""))
        images = [str(x) for x in body.get("images", []) if str(x)]
        chat = pcm.set_chat_retry(chat_id, prompt, image_refs=images, reason="manual")
    elif action == "try_now":
        stream = pcm.try_chat_retry_now(chat_id)
        chat = pcm.get_chat(chat_id)
        if chat is not None and stream is None and chat.retry_status == "pending":
            return JSONResponse({"error": "retry not started"}, status_code=409)
    else:
        return JSONResponse({"error": "unknown retry action"}, status_code=400)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(chat.to_dict(local=pcm.is_session_local(chat)))


async def chat_stop(request: Request) -> JSONResponse:
    """Stop an in-flight turn over plain HTTP.

    The websocket ``stop`` message (see routes_chat.py) is the normal path,
    but it depends on that chat's socket being connected at the moment the
    user clicks Stop. A socket cycling through reconnects (e.g. the per-chat
    liveness watchdog force-reconnecting under load) can swallow the message
    indefinitely with no visible error, leaving a turn nobody can interrupt.
    This route reaches ``stop_chat`` independently of any socket state.
    """
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    if pcm.get_chat(chat_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    stopped = await pcm.stop_chat(chat_id)
    return JSONResponse({"stopped": stopped})


async def chat_prompt(request: Request) -> JSONResponse:
    """Send a prompt to start a model turn in the chat (background task)."""
    from ciao.models import ImageAttachment

    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    body = await request.json()
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)

    chat = pcm.get_chat(chat_id)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if chat.archived:
        # Reject before starting a doomed stream: stream_chat would raise
        # "Cannot send messages to an archived chat" from the background
        # task, producing a server traceback + a raw error bubble. Same
        # guard the loop dispatcher uses (issue #126).
        return JSONResponse(
            {"error": "chat is archived", "archived": True}, status_code=409
        )

    images: list[ImageAttachment] = []
    for ref in body.get("images", []):
        attachment = pcm.resolve_image_ref(ref)
        if attachment:
            images.append(attachment)

    try:
        pcm.start_stream(chat_id, prompt, images=images or None)
    except Exception as exc:
        logger.exception("Failed to start stream for %s", chat_id)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True, "chat_id": chat_id})


async def chat_mark_read(request: Request) -> JSONResponse:
    """Mark a chat as read on the server. Emits a chat_read event so other
    tabs/devices clear their unread state, and cancels any pending delayed
    push for this chat.
    """
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    chat = pcm.mark_read(chat_id)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True, "last_read_at": chat.last_read_at})


async def chat_mark_unread(request: Request) -> JSONResponse:
    """Mark a chat as unread on purpose ("come back to this"). Clears the
    server-side read stamp and emits a chat_unread event so other tabs and
    devices raise their unread state too.
    """
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    chat = pcm.mark_unread(chat_id)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True, "last_read_at": chat.last_read_at})


async def chats_mark_all_read(request: Request) -> JSONResponse:
    """Mark every unread, non-archived chat as read. Returns the affected ids."""
    pcm = request.app.state.project_chat_manager
    touched = pcm.mark_all_read()
    return JSONResponse({"ok": True, "chat_ids": touched})


async def chat_archive(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    # Capture chat/project metadata BEFORE archive_chat() mutates the chat
    # (it flips ``archived=True`` but leaves project_id intact; pull project
    # info too so the trajectory record carries workspace + context).
    chat_meta = pcm.get_chat(chat_id)
    project_meta = (
        pcm.get_project(chat_meta.project_id) if chat_meta is not None else None
    )
    outcome = await pcm.archive_chat(chat_id)
    if outcome is not None:
        pcm.run_archive_postprocess(chat_id, outcome, chat_meta, project_meta)
    return JSONResponse({
        "ok": True,
        "archived_to": str(outcome.path) if outcome is not None else None,
        # The initiating client clears the active pane as soon as this response
        # arrives. Return the lifecycle record as well as publishing it over
        # /ws/events, so that client cannot miss the first "running" state in
        # the archive/event race.
        "postprocess": (
            dict(chat_meta.postprocess)
            if chat_meta and chat_meta.postprocess
            else None
        ),
    })


async def chat_retry_insights(request: Request) -> JSONResponse:
    """Resume unfinished post-archive stages for a single archived chat.

    Re-runs whatever is still pending/failed on the archive's manifest —
    insights extraction when it is missing, plus the project fold, trajectory
    and memory proposals when a crash landed after insights. Returns the retry
    status and the manifest view so the archived-chat panel can render partial
    completion. A pipeline already running for the chat is left alone.
    """
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    result = pcm.retry_archive_steps(chat_id)
    status = result["status"]
    if status == "not_found":
        return JSONResponse({"error": "not found"}, status_code=404)
    if status == "not_archived":
        return JSONResponse(
            {"error": "chat is not archived", "chat_id": chat_id}, status_code=409
        )
    if status == "no_archive":
        return JSONResponse(
            {"error": "no archive file for this chat", "chat_id": chat_id}, status_code=409
        )
    if status == "running":
        return JSONResponse(
            {"status": "running", "chat_id": chat_id, "job": result["job"]},
            status_code=202,
        )
    if status == "complete":
        return JSONResponse({"status": "complete", "chat_id": chat_id, "job": result["job"]})
    if status == "blocked":
        return JSONResponse(
            {"status": "blocked", "chat_id": chat_id, "job": result["job"]}
        )
    return JSONResponse(
        {"status": "started", "chat_id": chat_id, "job": result["job"]},
        status_code=202,
    )


async def chat_archive_job(request: Request) -> JSONResponse:
    """The persisted post-archive manifest for one archived chat.

    Returns the per-stage statuses, the unfinished list and any blocked reason
    so a surface can report partial completion without a live pipeline. A chat
    with no manifest (archived before this feature, or never processed) returns
    ``{"job": null}`` rather than 404: the absence is a normal state, not an
    error.
    """
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    if pcm.get_chat(chat_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"job": pcm.archive_job_view(chat_id)})


_MSG_PAGE_DEFAULT_LIMIT = 50
_MSG_PAGE_MAX_LIMIT = 200
_PART_CACHE_TTL_SECONDS = 1.2
_PART_CACHE_MAX_ENTRIES = 256
_PART_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _request_params(request: Request) -> dict[str, str]:
    """Query params tolerant of hand-built request scopes.

    Real HTTP requests always carry ``scope["query_string"]``, but unit tests
    construct bare scopes; Starlette's ``query_params`` raises KeyError there.
    """
    raw = request.scope.get("query_string", b"")
    return {k: v for k, v in parse_qsl(raw.decode("latin-1"))}


def _messages_json_response(request: Request, rows: list[dict]) -> JSONResponse:
    """Serve history rows, paginated from the newest end when asked.

    Without ``offset``/``limit`` params this returns the legacy flat array so
    older clients keep working. With either param the response becomes
    ``{items, total, offset, limit, hasMore, nextOffset}`` where ``offset``
    counts rows back from the newest end (offset 0 is the live tail), and
    pruning + lazy markers are applied.
    """
    params = _request_params(request)
    if "offset" not in params and "limit" not in params:
        return JSONResponse(rows)

    total = len(rows)
    try:
        limit = int(params.get("limit", _MSG_PAGE_DEFAULT_LIMIT))
    except ValueError:
        limit = _MSG_PAGE_DEFAULT_LIMIT
    limit = max(1, min(limit, _MSG_PAGE_MAX_LIMIT))
    try:
        offset = int(params.get("offset", 0))
    except ValueError:
        offset = 0
    offset = max(0, offset)

    wire = transcript_service._prune_rows_for_wire(rows)
    end = max(0, total - offset)
    start = max(0, end - limit)
    items = wire[start:end]
    has_more = start > 0
    return JSONResponse({
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "hasMore": has_more,
        "nextOffset": offset + limit if has_more else None,
    })


async def chat_messages(request: Request) -> JSONResponse:
    """Return conversation history for a chat.

    Claude chats read the SDK session file via ``get_session_messages``.
    opencode chats read the session history from a short-lived ``opencode serve``. Both fall
    back to the durable ``.runtime`` transcript when the provider-side session
    is unreadable.

    When a chat is archived, provider-side session storage is deleted to reclaim
    disk space (Claude SDK blob or opencode session). In that case we fall back to the
    durable markdown transcript in the vault so the PWA can still render the
    conversation read-only.

    Pagination: without ``offset``/``limit`` query params the response is the
    legacy flat array. With either param it becomes a
    ``{items, total, offset, limit, hasMore, nextOffset}`` envelope where
    ``offset=0`` is the newest tail; oversized ``_thinking`` rows are pruned
    and flagged ``lazy`` (fetch the full row from the part endpoint).
    """
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    chat = pcm.get_chat(chat_id)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    rows = await transcript_service._assemble_chat_messages(
        pcm, request.app.state.config, chat
    )
    return _messages_json_response(request, rows)


async def _cached_assembled_messages(pcm: Any, config: Any, chat: Any) -> list[dict]:
    """Assemble full history with a tiny TTL cache for part fetches.

    Expanding a lazy row rebuilds the whole assembly otherwise; consecutive
    expands within the TTL reuse one build. The list endpoint never uses this
    cache — polls must stay fresh.
    """
    chat_id = chat.chat_id
    now = time.monotonic()
    hit = _PART_CACHE.get(chat_id)
    if hit is not None and now - hit[0] < _PART_CACHE_TTL_SECONDS:
        return hit[1]
    rows = await transcript_service._assemble_chat_messages(pcm, config, chat)
    if len(_PART_CACHE) >= _PART_CACHE_MAX_ENTRIES:
        oldest = min(_PART_CACHE, key=lambda k: _PART_CACHE[k][0])
        _PART_CACHE.pop(oldest, None)
    _PART_CACHE[chat_id] = (now, rows)
    return rows


async def chat_message_part(request: Request) -> JSONResponse:
    """Return one unpruned history row by absolute index.

    Serves ``GET /api/chats/{id}/messages/part?i=<index>`` where the index is
    the ``i`` annotated on paginated list rows. Indices are positions in the
    full assembled list (handover messages included), stable while history is
    append-only.
    """
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    chat = pcm.get_chat(chat_id)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        idx = int(_request_params(request).get("i", ""))
    except ValueError:
        return JSONResponse({"error": "invalid i"}, status_code=400)
    rows = await _cached_assembled_messages(
        pcm, request.app.state.config, chat
    )
    if idx < 0 or idx >= len(rows):
        return JSONResponse({"error": "out of range"}, status_code=404)
    row = dict(rows[idx])
    row["i"] = idx
    return JSONResponse(row)


async def native_sessions(request: Request) -> JSONResponse:
    """List locally-running Claude Code CLI sessions for a workspace.

    Serves ``GET /api/native/sessions?workspace=<path>``; without the param
    the configured workspace root is used. Read-only liveness probe used by
    the node-handover flow to warn about externally-started CLI sessions.
    """
    params = _request_params(request)
    workspace = params.get("workspace") or str(
        request.app.state.config.workspace_root
    )
    try:
        sessions = live_sessions_for_workspace(workspace)
    except OSError:
        logger.exception("Native session scan failed for %s", workspace)
        sessions = []
    return JSONResponse({
        "sessions": sessions,
        "workspace": workspace,
        "checked_at": datetime.now(UTC).isoformat(),
    })


async def chat_subagents(request: Request) -> JSONResponse:
    """Return subagent activity for this chat's session, if any.

    Uses the SDK helpers added in ``claude-agent-sdk`` v0.1.60:
    ``list_subagents`` to discover subagent ids, and ``get_subagent_messages``
    to fetch each one's transcript. Returns an array shaped like:

    ``[{"agent_id": str, "messages": [...same shape as /messages...]}]``

    Each entry additionally carries dispatch metadata parsed from the parent
    session JSONL when available (see ciao/subagent_tracking.py):
    ``tool_use_id``, ``description``, ``subagent_type``, ``is_async``,
    ``status`` ("running"/"completed"/"failed"/"stopped"), and ``turn_index`` —
    the user turn that dispatched the agent, aligned with the ``turn_index``
    stamped on user bubbles by /messages so the PWA can anchor the subagent
    panel to the right turn.

    Empty array when the chat has no session, no subagents were spawned, or
    the SDK can't find the session on this machine (e.g. a remote chat that
    hasn't been pulled locally).
    """
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    chat = pcm.get_chat(chat_id)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not chat.session_id:
        return JSONResponse([])

    # ``?agent_id=`` narrows the response to one agent. The read-only subagent
    # view shows a single transcript and polls it while the agent works;
    # without this it re-fetched and re-rendered every subagent the chat ever
    # spawned on every tick. Narrowing skips the sibling transcript reads
    # entirely rather than filtering after the fact.
    # Normalised once, here. The SDK and the opencode tree key agents by the
    # bare id while the local-transcript fallback uses the file stem
    # ("agent-a319…"), and both forms reach the client — so a prefixed id
    # arriving here matched nothing, the narrow SDK read came back empty, and
    # the request fell through to the whole-directory fallback it exists to
    # avoid, on every poll.
    wanted_agent_id = (request.query_params.get("agent_id") or "").strip().removeprefix("agent-")

    config = request.app.state.config
    if getattr(chat, "provider", "claude") == "opencode":
        opencode_entries: list[dict] = []
        provider_service = pcm._providers.get(chat_id)
        live_provider = provider_service.provider if provider_service is not None else None
        if (
            isinstance(live_provider, OpencodeProvider)
            and live_provider.has_live_server
            and live_provider.current_session_id == chat.session_id
        ):
            collab_tree = await live_provider.read_live_collab_tree()
        else:
            resolver = getattr(pcm, "_agent_root_for_chat", None)
            root = resolver(chat_id) if resolver is not None else config.workspace_root
            collab_tree = await OpencodeProvider.read_collab_tree(
                root, chat.session_id
            )
        for item in collab_tree:
            info = item.get("info")
            info = info if isinstance(info, dict) else {}
            agent_id = str(info.get("id") or "")
            if not agent_id:
                continue
            if wanted_agent_id and agent_id.removeprefix("agent-") != wanted_agent_id:
                continue
            messages = item.get("messages")
            messages = messages if isinstance(messages, list) else []
            opencode_entries.append({
                "agent_id": agent_id,
                "parent_agent_id": str(info.get("parentID") or ""),
                "messages": transcript_service._render_opencode_thread(item, chat, metadata=False),
                "tool_use_id": "",
                "description": str(info.get("title") or ""),
                "subagent_type": "opencode",
                "is_async": True,
                # opencode's session objects carry no status field, but this
                # endpoint is polled every few seconds while a turn streams —
                # derive the lifecycle from the child's own messages and anchor
                # it to the parent turn sent before the child was created.
                "status": transcript_service._opencode_child_status(
                    messages,
                    info,
                    item.get("active") if isinstance(item.get("active"), bool) else None,
                ),
                "turn_index": transcript_service._opencode_child_turn_index(info, chat),
            })
        return JSONResponse(opencode_entries)

    if getattr(chat, "provider", "claude") not in {"claude", "opencode"}:
        return JSONResponse([])

    workspace = str(config.workspace_root)
    resolver = getattr(pcm, "_agent_root_for_chat", None)
    agent_root = resolver(chat_id) if resolver is not None else None

    async def _finalize(entries: list[dict]) -> JSONResponse:
        # Catch-all for the local-transcript fallbacks, which read the whole
        # session directory and cannot narrow at the source. Those entries are
        # keyed by file stem ("agent-a319…") while the SDK and the route use
        # the bare id, so compare on the bare form or the filter drops the
        # agent it was asked for. `wanted_agent_id` is already bare.
        if wanted_agent_id:
            entries = [
                e
                for e in entries
                if str(e.get("agent_id", "")).removeprefix("agent-") == wanted_agent_id
            ]
        # Off the loop: this walks the whole parent session JSONL, which on a
        # long Claude chat is tens of MB, and the read-only subagent view polls
        # this endpoint every 4 seconds. Done inline it stalled every in-flight
        # stream for the length of the parse. `_running_subagent_rows` already
        # does the identical work in a thread.
        await asyncio.to_thread(
            _merge_subagent_dispatch_meta,
            entries,
            chat.session_id,
            Path(config.workspace_root),
            agent_root=agent_root,
        )
        return JSONResponse(entries)

    try:
        from claude_agent_sdk import get_subagent_messages, list_subagents
    except ImportError:
        return await _finalize(
            transcript_service._local_subagent_transcripts(
                chat.session_id, Path(config.workspace_root), agent_root=agent_root
            )
        )

    if wanted_agent_id:
        # Skip discovery: the caller already knows the id, and list_subagents
        # only exists to enumerate the siblings we are deliberately not reading.
        agent_ids = [wanted_agent_id]
    else:
        try:
            agent_ids = list_subagents(chat.session_id, directory=workspace)
        except (FileNotFoundError, ValueError):
            return await _finalize(
                transcript_service._local_subagent_transcripts(
                    chat.session_id, Path(config.workspace_root), agent_root=agent_root
                )
            )
        except Exception:  # noqa: BLE001 — defensive against SDK surprises
            return await _finalize(
                transcript_service._local_subagent_transcripts(
                    chat.session_id, Path(config.workspace_root), agent_root=agent_root
                )
            )

    result: list[dict] = []
    for agent_id in agent_ids:
        try:
            msgs = get_subagent_messages(
                chat.session_id,
                agent_id,
                directory=workspace,
            )
        except (FileNotFoundError, ValueError):
            continue
        except Exception:  # noqa: BLE001 — defensive
            continue

        rendered = transcript_service._render_subagent_messages(msgs)
        if not rendered and wanted_agent_id:
            # An empty read is a miss, not an empty transcript. On installs
            # whose CLI writes the nested "<session>/subagents/*.jsonl" layout,
            # ``list_subagents`` returns [] and ``get_subagent_messages``
            # answers any id with [] rather than raising. The unfiltered path
            # survives that (no ids -> empty result -> local fallback), but the
            # narrowed one skips discovery and asks for the id directly, so it
            # built a row with no messages and that non-empty ``result``
            # suppressed the very fallback that does find the transcript. The
            # read-only subagent view then showed "No captured turns" for every
            # Claude subagent while the in-chat panel had the full thread.
            #
            # Only the narrowed path skips: on the unfiltered one an empty
            # render is usually a just-dispatched agent that has not written
            # its first message yet, and dropping it would make its row
            # flicker out of the in-chat panel. There the all-empty case
            # already reaches the local fallback via the `not result` check.
            continue
        result.append({"agent_id": agent_id, "messages": rendered})

    if not result:
        result = transcript_service._local_subagent_transcripts(
            chat.session_id, Path(config.workspace_root), agent_root=agent_root
        )

    return await _finalize(result)


async def running_subagents(request: Request) -> JSONResponse:
    """Subagents working right now, per chat, for the sidebar's subagent rows.

    ``/api/chats/{id}/subagents`` is the transcript endpoint: it renders every
    subagent a chat ever spawned, which is far too much to poll for a sidebar
    that only shows live work. This returns dispatch metadata alone (no
    messages) and only for chats the manager already considers active, so the
    cost is one JSONL parse per working chat rather than one per chat in the
    registry.

    Shape: ``{"chats": {chat_id: [{agent_id, description, subagent_type,
    status, is_async, turn_index}]}}``. Chats with nothing running are omitted
    entirely, which is what lets the client drop their rows.

    For Claude chats this is the set the parent session can name — in practice
    background dispatches, because a foreground Task only appears in the parent
    file once it has already finished (see
    ``subagent_tracking.running_agents``). Foreground work is visible in the
    chat's own live trace instead, since the turn that spawned it is still
    streaming.
    """
    pcm = request.app.state.project_chat_manager
    config = request.app.state.config
    out: dict[str, list[dict]] = {}
    scanned: list[tuple[str, Any]] = []
    for chat_id in pcm.active_chat_ids():
        chat = pcm.get_chat(chat_id)
        if chat is None or chat.archived or not chat.session_id:
            continue
        scanned.append((chat_id, chat))
    # Gathered, not awaited one at a time. Each scan is an independent thread
    # hop over that chat's own session file (or an independent opencode read),
    # so a serial loop cost N full parses of wall clock every four seconds
    # while anything was working. `return_exceptions` preserves the per-chat
    # tolerance the loop had: one unreadable session must not blank the
    # sidebar for the others.
    results = await asyncio.gather(
        *(_running_subagent_rows(pcm, config, chat) for _cid, chat in scanned),
        return_exceptions=True,
    )
    for (chat_id, _chat), rows in zip(scanned, results):
        if isinstance(rows, BaseException):
            logger.warning(
                "running-subagent scan failed for chat %s", chat_id, exc_info=rows
            )
            continue
        if rows:
            out[chat_id] = rows
    return JSONResponse({"chats": out})


async def _running_subagent_rows(pcm, config, chat) -> list[dict]:
    """Live subagents for one chat, without loading any transcript."""
    provider = getattr(chat, "provider", "claude")
    if provider == "opencode":
        provider_service = pcm._providers.get(chat.chat_id)
        live_provider = provider_service.provider if provider_service is not None else None
        if (
            isinstance(live_provider, OpencodeProvider)
            and live_provider.has_live_server
            and live_provider.current_session_id == chat.session_id
        ):
            collab_tree = await live_provider.read_live_collab_tree()
        else:
            # Sessions are cwd-scoped: a chat in a non-primary workspace was
            # created under its agent root, not the install root. The
            # live-provider path already uses that root; the ephemeral fallback
            # must do the same, otherwise it returns no children and the sidebar
            # drops still-running agents.
            resolver = getattr(pcm, "_agent_root_for_chat", None)
            agent_root = resolver(chat.chat_id) if resolver is not None else None
            root = Path(agent_root) if agent_root else Path(config.workspace_root)
            collab_tree = await OpencodeProvider.read_collab_tree(
                root, chat.session_id
            )
        rows: list[dict] = []
        for item in collab_tree:
            info = item.get("info")
            info = info if isinstance(info, dict) else {}
            agent_id = str(info.get("id") or "")
            if not agent_id:
                continue
            messages = item.get("messages")
            messages = messages if isinstance(messages, list) else []
            if transcript_service._opencode_child_status(
                messages,
                info,
                item.get("active") if isinstance(item.get("active"), bool) else None,
            ) != "running":
                continue
            rows.append({
                "agent_id": agent_id,
                "description": str(info.get("title") or ""),
                "subagent_type": "opencode",
                "is_async": True,
                "status": "running",
                "turn_index": transcript_service._opencode_child_turn_index(info, chat),
            })
        return rows

    if provider != "claude":
        return []
    resolver = getattr(pcm, "_agent_root_for_chat", None)
    agent_root = resolver(chat.chat_id) if resolver is not None else None
    path = subagent_tracking.find_parent_session_file(
        chat.session_id, Path(config.workspace_root), agent_root=agent_root
    )
    if path is None:
        return []
    def _scan() -> list[subagent_tracking.SubagentInfo]:
        # Both halves belong off the event loop. running_agents() is not a
        # cheap filter over the parsed state: it stats and tail-reads each
        # running agent's own transcript, and this endpoint is polled by the
        # sidebar for every working chat.
        state = subagent_tracking.parse_session_subagents(path)
        return subagent_tracking.running_agents(path, state)


    return [
        {
            "agent_id": info.agent_id,
            "description": info.description,
            "subagent_type": info.subagent_type,
            "is_async": info.is_async,
            "status": info.status,
            "turn_index": info.turn_index,
        }
        for info in await asyncio.to_thread(_scan)
    ]


def _merge_subagent_dispatch_meta(
    entries: list[dict], session_id: str, workspace_root: Path, *, agent_root: Path | None = None
) -> None:
    """Attach dispatch metadata from the parent session JSONL in place."""
    if not entries:
        return
    path = subagent_tracking.find_parent_session_file(
        session_id, workspace_root, agent_root=agent_root
    )
    if path is None:
        return
    try:
        state = subagent_tracking.parse_session_subagents(path)
    except Exception:  # noqa: BLE001 — metadata is best-effort decoration
        logger.exception("subagent dispatch-meta parse failed for %s", session_id)
        return
    for entry in entries:
        # SDK ids are bare ("a319..."); the local-JSONL fallback uses the
        # file stem ("agent-a319...").
        agent_id = str(entry.get("agent_id", "")).removeprefix("agent-")
        info = state.subagents.get(agent_id)
        if info is None:
            continue
        entry["tool_use_id"] = info.tool_use_id
        entry["description"] = info.description
        entry["subagent_type"] = info.subagent_type
        entry["is_async"] = info.is_async
        entry["status"] = info.status
        if info.turn_index is not None:
            entry["turn_index"] = info.turn_index


# ── Images ───────────────────────────────────────────────────────────────

async def chat_images(request: Request) -> JSONResponse:
    """Upload images and return references."""
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    chat = pcm.get_chat(chat_id)
    if chat is None:
        return JSONResponse({"error": "chat not found"}, status_code=404)

    form = await request.form()
    results = []
    for key in form:
        upload = form[key]
        if not hasattr(upload, "read"):
            continue
        filename = getattr(upload, "filename", "image.jpg") or "image.jpg"
        try:
            data = await _read_upload_limited(
                upload, MAX_IMAGE_SIZE_BYTES
            )
            attachment = pcm.save_image_upload(data, filename)
            results.append({
                "ref": attachment.path.name,
                "mime_type": attachment.mime_type,
                "filename": attachment.original_filename,
            })
        except ValueError as exc:
            results.append({"error": str(exc), "filename": filename})

    return JSONResponse(results)


async def image_blob(request: Request) -> Response:
    """Serve an uploaded image file by its ref (filename under media_root)."""
    pcm = request.app.state.project_chat_manager
    ref = request.path_params["ref"]
    attachment = pcm.resolve_image_ref(ref)
    if attachment is None:
        return Response(status_code=404)
    return FileResponse(attachment.path, media_type=attachment.mime_type)


# Extensions the workspace-file viewer is allowed to serve. Keep this
# conservative: the PWA viewer is a read-only inspector, not a generic file
# server, and binary/media types are served by other dedicated endpoints.
_WORKSPACE_FILE_EXTS = frozenset({
    ".md", ".markdown", ".txt",
    ".py", ".ts", ".tsx", ".js", ".jsx", ".vue",
    ".css", ".html", ".json",
    ".yaml", ".yml", ".toml",
    ".sh", ".rs", ".go", ".java", ".xml", ".sql",
    ".cfg", ".ini", ".log", ".csv",
})
# Intentionally excluded: .env, .example — these commonly hold secrets or
# sample secrets. The viewer is a read-only inspector and should not serve
# them even though they are under workspace_root.
_WORKSPACE_FILE_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
_LINE_SUFFIX_RE = re.compile(r":\d+$")

# Images embedded in vault markdown docs (e.g. `![](images/foo.png)`) are
# served by a dedicated endpoint so the text viewer stays strictly text.
# MIME types are derived from the extension whitelist below.
_WORKSPACE_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp", ".ico",
})
# Larger cap than the text viewer: screenshots and dashboard captures are
# commonly a few MB. Still bounded so a pathological request can't stream a
# gigabyte.
_WORKSPACE_IMAGE_MAX_BYTES = 15 * 1024 * 1024  # 15 MB




async def workspace_file(request: Request) -> Response:
    """Serve a read-only allowlisted text file from the host filesystem.

    Path is provided as a query string (`?path=...`). The path may be
    workspace-relative or absolute, with an optional `:line` suffix that is
    stripped. All results canonicalise via ``Path.resolve()``. There is no
    workspace sandbox: any allowlisted-extension file on disk is served.
    Relative paths anchor to ``config.workspace_root``.
    """
    config = request.app.state.config
    raw = request.query_params.get("path", "").strip()
    roots = _allowed_roots(config)
    # `exact=1` turns the fuzzy fallback off. Fuzzy resolution ends in a bare
    # filename match against the primary root, which is right for a link a
    # model emitted with an approximate path and wrong for a caller naming one
    # specific file: asking for `work/AGENTS.md` on a workspace that has none
    # would otherwise serve `personal/AGENTS.md` with a 200, and the caller
    # cannot tell. The sidebar's guide card probes with it for that reason.
    exact = request.query_params.get("exact", "").strip().lower() in {"1", "true", "yes"}
    result = _resolve_workspace_path(roots, raw, allow_fuzzy=not exact)
    if isinstance(result, Response):
        return result
    resolved = result

    if resolved.suffix.lower() not in _WORKSPACE_FILE_EXTS:
        return JSONResponse({"error": "unsupported type"}, status_code=415)
    if resolved.stat().st_size > _WORKSPACE_FILE_MAX_BYTES:
        return JSONResponse({"error": "file too large"}, status_code=413)

    # Force revalidation on every load. Without this, browsers fall back to
    # heuristic freshness (~10% of file age since Last-Modified). Two consequences
    # that bit us in practice:
    #   1. Different callers can encode the same file under different paths
    #      (workspace-relative vs absolute), giving each its own cache entry.
    #      A stale entry then sticks around even after the file has been edited.
    #   2. Markdown previews kept showing pre-edit content for minutes/hours.
    # ETag + Last-Modified are still emitted by FileResponse, so a 304 path
    # remains available; we only change *whether* the browser asks.
    return FileResponse(
        resolved,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


# ── HTML artifacts ───────────────────────────────────────────────────────
# An artifact is a self-contained page the model authored (a dashboard, an
# annotated diff, a comparison) that the PWA embeds in the pinned panel.
# It is served from this host, so it needs a policy of its own.

_WORKSPACE_HTML_EXTS = frozenset({".html", ".htm"})

# Read this before "hardening" it: `script-src 'unsafe-inline'` is load-bearing.
# An artifact inlines its own <script> and <style> — that is the entire point of
# a single self-contained file — so removing 'unsafe-inline' does not tighten
# this policy, it breaks every artifact and leaves a blank frame with a console
# error the user never sees.
#
# Containment comes from the other directives, not from script-src:
#   - `sandbox allow-scripts` (no allow-same-origin) puts the document in an
#     opaque origin. It cannot read the session cookie, localStorage, or the
#     embedding page, even though it is served from the same host.
#   - `connect-src 'none'` kills fetch, XHR, WebSocket and EventSource.
#   - `img-src data:` (no http/https) closes the beacon-through-an-image-URL
#     exfiltration path that a permissive img-src leaves open.
#   - `form-action 'none'` and `base-uri 'none'` stop navigation-based leaks.
# The net effect: an artifact can render and respond to clicks, and has no way
# to reach /api/*, phone home, or read anything of the user's.
#
# `allow-popups` and `blob:` sources remain absent. Self-contained audio/video
# artifacts use data URLs, so media access is allowed only for embedded data.
_ARTIFACT_CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'unsafe-inline'",
        "style-src 'unsafe-inline'",
        "img-src data:",
        "media-src data:",
        "font-src data:",
        "connect-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors 'self'",
        "sandbox allow-scripts",
    ]
)

# CSP for host-rendered binary previews (PDF, and PPTX after conversion).
# Looser than _ARTIFACT_CSP because the renderer here is the browser's own
# viewer loading our assets, not model-authored markup.
_EMBEDDED_PREVIEW_CSP = "; ".join(
    [
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'self'",
        "form-action 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "font-src 'self' data: https://fonts.gstatic.com",
        "connect-src 'self' ws: wss: https://fonts.googleapis.com https://fonts.gstatic.com",
    ]
)


async def workspace_html(request: Request) -> Response:
    """Serve a workspace ``.html`` file as a renderable page, not as source.

    ``/api/workspace-file`` already serves ``.html`` but as ``text/plain``,
    which is what the panel's Code view wants. This endpoint is the Preview
    side: same file, ``text/html``, under ``_ARTIFACT_CSP``. See that constant
    for why the policy is shaped the way it is.

    Why a real endpoint instead of an ``srcdoc`` iframe: ``srcdoc`` and
    ``blob:`` documents inherit the *embedder's* CSP, which is ``script-src
    'self'`` (``ciao/web/security.py``). Inline artifact script would be
    silently blocked. A frame loaded from a URL gets its own CSP from these
    response headers instead.

    The size cap is deliberately the same 2 MB as the text viewer and the
    snapshot store, so there is no state where a file renders but has no
    history, or has history but refuses to render. Over the cap the panel
    shows a 413 and the user opens the file in a real browser instead
    (``/api/workspace-open``).

    Fuzzy resolution is kept for parity with the Code view, so the same path
    string that shows the source also renders the page. Note that this means
    fuzzy matching decides which document gets to execute script; the sandbox
    above is what keeps that from mattering.
    """
    config = request.app.state.config
    raw = request.query_params.get("path", "").strip()
    roots = _allowed_roots(config)
    result = _resolve_workspace_path(roots, raw, allow_fuzzy=True)
    if isinstance(result, Response):
        return result
    resolved = result

    if resolved.suffix.lower() not in _WORKSPACE_HTML_EXTS:
        return JSONResponse({"error": "unsupported type"}, status_code=415)
    if resolved.stat().st_size > _WORKSPACE_FILE_MAX_BYTES:
        return JSONResponse({"error": "file too large"}, status_code=413)

    # The bridge script is what makes the rendered page commentable: it watches
    # selections inside the sandboxed frame and posts anchors to the panel via
    # postMessage — the only channel an opaque-origin frame has. Inline script
    # is already permitted ('unsafe-inline' is load-bearing for artifacts), and
    # a fragment without <head> still gets the bridge prepended. Injecting
    # model-authored text is not a concern: we append our own markup, never
    # user content.
    from ciao.web.artifact_bridge import inject_bridge

    # Decode leniently rather than with the default strict codec: a workspace
    # can hold a cp1252/latin-1 HTML export, and a strict decode raises inside
    # the handler for a file that used to stream fine as a FileResponse. A
    # replacement char in one artifact beats a 500 for the whole page.
    source = resolved.read_bytes().decode("utf-8", errors="replace")

    return Response(
        content=inject_bridge(source),
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": _ARTIFACT_CSP,
            # SecurityHeadersMiddleware sets X-Frame-Options: DENY through
            # setdefault, so an explicit value here wins and lets the PWA
            # embed the frame. frame-ancestors above is the modern equivalent;
            # both are sent because the desktop shell's webview is older.
            "X-Frame-Options": "SAMEORIGIN",
            # Same revalidation reasoning as workspace_file: the panel reloads
            # the frame after the model revises an artifact, and a heuristically
            # fresh cache entry would show the pre-edit page.
            "Cache-Control": "no-cache",
        },
    )


_VAULT_MD_EXCLUDE_DIRS = frozenset({"Logs", "Templates", ".obsidian"})


def _collect_vault_markdown_paths(config) -> list[str]:
    """Walk the allowed roots for markdown paths, relative to the workspace.

    Synchronous on purpose: this whole traversal is one read operation that the
    route hands to a bounded worker, so the event loop never runs it inline.
    """
    workspace = config.workspace_root.resolve()
    paths: list[str] = []
    seen: set[str] = set()
    for root in _allowed_roots(config):
        if not root.is_dir():
            continue
        for md_path in root.rglob("*.md"):
            try:
                rel = md_path.relative_to(root)
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            if any(part in _VAULT_MD_EXCLUDE_DIRS for part in rel.parts):
                continue
            try:
                resolved = md_path.resolve()
            except OSError:
                continue
            try:
                display = str(resolved.relative_to(workspace))
            except ValueError:
                display = str(resolved)
            if display in seen:
                continue
            seen.add(display)
            paths.append(display)
        for md_path in root.rglob("*.markdown"):
            try:
                rel = md_path.relative_to(root)
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            if any(part in _VAULT_MD_EXCLUDE_DIRS for part in rel.parts):
                continue
            try:
                resolved = md_path.resolve()
            except OSError:
                continue
            try:
                display = str(resolved.relative_to(workspace))
            except ValueError:
                display = str(resolved)
            if display in seen:
                continue
            seen.add(display)
            paths.append(display)
    paths.sort()
    return paths


async def vault_markdown_paths(request: Request) -> JSONResponse:
    """Return workspace-relative paths to markdown files for link resolution."""
    config = request.app.state.config
    key = f"vault_markdown_paths:{config.workspace_root.resolve()}:{getattr(config, 'vault_root', '')}"
    paths = await run_read(key, lambda: _collect_vault_markdown_paths(config))
    return JSONResponse({"paths": paths})


_BACKLINKS_LIMIT = 30


def _without_markdown_extension(path: str) -> str:
    return re.sub(r"\.(?:md|markdown)$", "", path, flags=re.IGNORECASE)


def _normalize_vault_link_ref(ref: str) -> str:
    normalized = ref.strip().replace("\\", "/")
    if normalized.startswith("memory-vault/"):
        normalized = normalized[len("memory-vault/"):]
    return _without_markdown_extension(normalized)


def _add_backlink_index_entry(
    index: dict[str, list[str]],
    key: str,
    path: str,
) -> None:
    if not key:
        return
    matches = index.setdefault(key, [])
    if path not in matches:
        matches.append(path)


def _build_backlink_index(paths: Iterable[str]) -> dict[str, list[str]]:
    """Build the same path/stem lookup keys as the frontend vault-link index."""
    index: dict[str, list[str]] = {}
    for path in paths:
        no_ext = _without_markdown_extension(path)
        _add_backlink_index_entry(index, no_ext, path)
        _add_backlink_index_entry(index, posixpath.basename(no_ext), path)
        marker = "memory-vault/"
        marker_index = no_ext.find(marker)
        if marker_index >= 0:
            _add_backlink_index_entry(
                index,
                no_ext[marker_index + len(marker):],
                path,
            )
    return index


def _resolve_backlink_target(
    ref: str,
    current_path: str,
    index: dict[str, list[str]],
    path_set: set[str],
) -> str | None:
    """Resolve one link ref using the frontend's relative/path/stem rules.

    ``ref`` comes from ``vault_lint._links_in`` and is already note-relative and
    extension-less (`./People/Mo`), so the relative candidates below are the
    exact target in the common case. The path/stem fallbacks still matter: a
    note under ``Logs/`` cites vault-root-relative paths (see
    ``ciao/insights.py``), and those only resolve through the index.
    """
    normalized = _normalize_vault_link_ref(ref)
    if not normalized:
        return None

    current_dir = posixpath.dirname(current_path)
    relative_candidates = [
        posixpath.normpath(posixpath.join(current_dir, f"{normalized}.md")),
        posixpath.normpath(posixpath.join(current_dir, f"{normalized}.markdown")),
    ]
    for candidate in relative_candidates:
        if candidate in path_set:
            return candidate

    direct = index.get(normalized, [])
    if len(direct) == 1:
        return direct[0]
    if len(direct) > 1:
        relative_pick = next(
            (path for path in direct if path in relative_candidates),
            None,
        )
        if relative_pick is not None:
            return relative_pick
        if "/" in normalized:
            return direct[0]
        return None

    tail = posixpath.basename(normalized)
    stem_matches = index.get(tail, [])
    if len(stem_matches) == 1:
        return stem_matches[0]
    return None


def _references_note(
    content: str,
    current_path: str,
    target_path: str,
    index: dict[str, list[str]],
    path_set: set[str],
) -> bool:
    """True if ``content`` has a markdown link resolving to ``target_path``.

    Reuses ``vault_lint._links_in`` so links documented inside code fences/spans
    or escaped (``\\[label](x.md)``) don't count, and so a backlink and a
    broken-link finding can never disagree about what a link is. Resolution
    mirrors the frontend so two notes with the same filename stem do not share
    false backlinks.
    """
    for ref in _links_in(content):
        if _resolve_backlink_target(ref, current_path, index, path_set) == target_path:
            return True
    return False


def _collect_vault_backlinks(config, target_path: str) -> list[dict[str, str]]:
    """Traverse candidate notes and return incoming links to ``target_path``.

    The whole read — walk, index build, and per-note read/link parse — is one
    synchronous operation for the route to hand to a bounded worker.
    """
    workspace_root = config.workspace_root.resolve()
    candidates: list[tuple[Path, str]] = []
    seen_paths: set[str] = set()

    for root in _allowed_roots(config):
        if not root.is_dir():
            continue
        for pattern in ("*.md", "*.markdown"):
            for md_path in root.rglob(pattern):
                try:
                    relative_parts = md_path.relative_to(root).parts
                    resolved = md_path.resolve()
                except (OSError, ValueError):
                    continue
                if any(
                    part.startswith(".") or part in EXCLUDE_DIRS
                    for part in relative_parts
                ):
                    continue
                try:
                    display_path = str(resolved.relative_to(workspace_root))
                except ValueError:
                    display_path = str(resolved)
                display_path = display_path.replace("\\", "/")
                if display_path in seen_paths:
                    continue
                seen_paths.add(display_path)
                candidates.append((resolved, display_path))

    candidates.sort(key=lambda item: item[1])
    path_set = {display_path for _path, display_path in candidates}
    clean_target = _LINE_SUFFIX_RE.sub("", target_path).replace("\\", "/")
    resolved_target = next(
        (display for _path, display in candidates if display == clean_target),
        None,
    )
    if resolved_target is None:
        try:
            raw_target = Path(clean_target)
            target_on_disk = (
                raw_target.resolve()
                if raw_target.is_absolute()
                else (workspace_root / raw_target).resolve()
            )
        except (OSError, ValueError):
            return []
        resolved_target = next(
            (display for path, display in candidates if path == target_on_disk),
            None,
        )
    if resolved_target is None:
        return []

    index = _build_backlink_index(path_set)
    target_stem = Path(resolved_target).stem.casefold()
    backlinks: list[dict[str, str]] = []
    for md_path, display_path in candidates:
        if display_path == resolved_target:
            continue
        try:
            content = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Cheap gate before the code-fence stripping + link parse in _links_in.
        if target_stem not in content.casefold():
            continue

        if _references_note(
            content,
            display_path,
            resolved_target,
            index,
            path_set,
        ):
            backlinks.append({"path": display_path, "title": md_path.stem})
            if len(backlinks) >= _BACKLINKS_LIMIT:
                return backlinks
    return backlinks


async def vault_backlinks(request: Request) -> JSONResponse:
    """Return notes that link to the given markdown path (incoming links)."""
    target_path = request.query_params.get("path", "").strip()
    if not target_path:
        return JSONResponse({"backlinks": []})
    config = request.app.state.config
    key = (
        f"vault_backlinks:{config.workspace_root.resolve()}:"
        f"{getattr(config, 'vault_root', '')}:{target_path}"
    )
    backlinks = await run_read(key, lambda: _collect_vault_backlinks(config, target_path))
    return JSONResponse({"backlinks": backlinks})


async def vault_graph(request: Request) -> JSONResponse:
    """Return the vault as a note graph for the Memory Map page.

    Nodes are notes with frontmatter (or an inferred type); edges come from
    both frontmatter ``related:``/``relatedTo:`` and body markdown links,
    already merged and resolved to real paths by ``vault_index.scan_vault``.
    Optional ``?workspace=`` scopes to one logical workspace; cross-workspace
    edges are dropped rather than left dangling.
    """
    config = request.app.state.config
    workspace = request.query_params.get("workspace", "").strip() or None
    # Every vault in the install, which is ONE shared vault before the
    # re-rooting and one per agent root after it. Scanning `config.vault_root`
    # returned zero notes on a migrated install, so the whole map went blank.
    targets = config.vault_scan_targets()
    # After the re-rooting each target IS one workspace, so a `?workspace=`
    # request can drop the other roots before the scan instead of reading and
    # parsing every note in the install only to filter them out below — the
    # scan is the whole cost of this route. Before the re-rooting the single
    # shared target holds every workspace and no target can be dropped, which
    # is why the `filter_entries` scoping stays where it is either way.
    in_scope = [t for t in targets if workspace and t[1] == workspace]
    scan_list = in_scope or targets
    # Reads and parses every markdown file, so run it off the event loop or a
    # large vault stalls other requests, including the 5s chat-socket keepalives
    # (see chat_messages above for the same fix).
    entries, absolute = await asyncio.to_thread(scan_targets, scan_list)
    if in_scope:
        # The picker lists every workspace, and this scan only saw one. Taken
        # from the targets whose vault exists, which is the same set the full
        # scan would have produced entries for.
        workspaces = sorted(ws for root, ws, _ in targets if ws and Path(root).is_dir())
    else:
        workspaces = sorted({e.workspace for e in entries if e.workspace})
    scoped = filter_entries(entries, workspace=workspace) if workspace else entries
    graph = _build_graph(scoped)
    by_path = {str(e.path) for e in scoped}

    # `mtime` lets the Memory Map seed its local view from the note you touched
    # most recently, which is a far more useful entry point than "whatever the
    # biggest hub is". Entry carries no timestamp, so stat the files here; it is
    # one stat per note against files scan_vault has just read anyway.
    def _mtime(rel: str) -> float:
        # Resolved through the scan's own map. Rendered paths are no longer a
        # fixed offset from one vault root, so stripping a `memory-vault/`
        # prefix and joining resolved to nothing on a migrated install and every
        # note reported mtime 0 — which silently broke the map's "most recently
        # touched note" entry point rather than failing loudly.
        target = absolute.get(rel)
        if target is None:
            return 0.0
        try:
            return target.stat().st_mtime
        except OSError:
            # A note indexed but unreadable (race with a delete, broken
            # symlink) must not fail the whole graph request.
            return 0.0

    # Aging uses the one predicate the audit and the review queue's
    # `unverified` signal also use, so the map's "unchecked" count cannot
    # disagree with the queue it sends the user to. Notes the queue never lists
    # (Workspace/ files, templates, completed projects) and exempt types
    # (logs, journals) keep their age but are never flagged.
    from ciao.memory_audit import note_verification
    from ciao.vault_review import never_queued

    current_date = datetime.now(UTC).date()

    def _staleness(e) -> tuple[bool, int | None, int | None]:
        verification = note_verification(
            e.type or "", e.updated or "", _mtime(str(e.path)), today=current_date
        )
        if verification is None:
            return False, None, None
        stale = verification.stale and not never_queued(str(e.path))
        # The horizon travels with the flag so the map can name the rule
        # without keeping its own copy of the thresholds table.
        return stale, verification.age_days, verification.threshold_days

    nodes = [
        {
            "id": str(e.path),
            "title": e.title,
            "type": e.type,
            "tags": e.tags,
            "aliases": e.aliases,
            "description": e.description,
            "workspace": e.workspace,
            "degree": len(graph.get(str(e.path), ())),
            "mtime": _mtime(str(e.path)),
            "updated": e.updated,
            **dict(zip(("stale", "age_days", "threshold_days"), _staleness(e))),
        }
        for e in scoped
    ]
    seen: set[tuple[str, str]] = set()
    edges = []
    for src, targets in graph.items():
        if src not in by_path:
            continue
        for tgt in targets:
            if tgt not in by_path:
                continue
            first, second = sorted((src, tgt))
            key = (first, second)
            if key in seen:
                continue
            seen.add(key)
            edges.append({"source": key[0], "target": key[1]})
    return JSONResponse({
        "workspace": workspace or "all",
        "workspaces": workspaces,
        "nodes": nodes,
        "edges": edges,
    })


async def vault_review(request: Request) -> JSONResponse:
    """List or explicitly dispose of scoped vault-review candidates."""
    config = request.app.state.config
    workspace = request.query_params.get("workspace", "").strip()
    if not workspace or config.workspace(workspace) is None:
        return JSONResponse({"error": "workspace is required"}, status_code=400)
    try:
        root = Path(config.workspace_vault_root(workspace)).resolve()
    except (AttributeError, ValueError, OSError) as exc:
        return JSONResponse({"error": f"workspace vault unavailable: {exc}"}, status_code=409)
    from ciao import vault_review as review

    action = "" if request.method == "GET" else ""
    if request.method == "POST":
        try:
            payload = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        action = str(payload.get("action", "") or "")
    # A GET must not write to the vault: only the POST actions that already
    # mutate refresh the readable `Workspace/Vault-Review.md` projection.
    # `reopen` belongs here too: it looks its row up in the ledger, never in
    # `candidates`, so the pre-action scan was thrown away — and that scan
    # reads every note in the vault three times, twice per click.
    if action in {"restore", "delete", "reopen"}:
        candidates = []
    else:
        candidates = await asyncio.to_thread(
            functools.partial(
                review.generate_candidates,
                root,
                workspace=workspace,
                max_candidates=review.MAX_CANDIDATES_CEILING,
                write_queue=request.method != "GET",
            )
        )
    if request.method == "GET":
        review_body: dict[str, Any] = {"candidates": [item.as_dict() for item in candidates]}
        # The trash view renders the reversible trash, which candidate
        # generation can never return: a trashed note is no longer in the
        # vault. Same read-only contract as the candidate listing itself.
        include = {part.strip() for part in request.query_params.get("include", "").split(",")}
        # The way back in from a `keep`. Same read-only contract: a listing.
        if "cleared" in include:
            review_body["cleared"] = await asyncio.to_thread(
                functools.partial(review.list_cleared, root, workspace=workspace)
            )
        if "trashed" in include:
            review_body["trashed"] = await asyncio.to_thread(
                functools.partial(review.list_trashed, root, workspace=workspace)
            )
        return JSONResponse(review_body)

    candidate_id_value = str(payload.get("candidate_id", "") or "")
    # `reopen` addresses a candidate that is, by definition, no longer in the
    # generated list, so it cannot go through the lookup below.
    if action == "reopen":
        try:
            result = await asyncio.to_thread(
                functools.partial(review.reopen_note, root, candidate_id_value, workspace=workspace)
            )
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse({"ok": True, "result": result, **await _vault_review_snapshot(root, workspace)})
    if action in {"restore", "delete"}:
        try:
            result = review.restore_note(root, candidate_id_value) if action == "restore" else review.delete_permanently(root, candidate_id_value, confirm=str(payload.get("confirm", "")))
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return JSONResponse({"ok": True, "result": result, **await _vault_review_snapshot(root, workspace)})
    item = next((candidate for candidate in candidates if candidate.candidate_id == candidate_id_value), None)
    if item is None:
        return JSONResponse({"error": "candidate not found or changed"}, status_code=409)
    try:
        if action == "decide":
            result = review.record_decision(root, item, str(payload.get("disposition", "")), actor="user")
        elif action == "trash":
            result = review.trash_note(root, item)
        else:
            return JSONResponse({"error": "unsupported action"}, status_code=400)
    except (ValueError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    # The projection is pending-only and must reflect the successful mutation,
    # not the pre-action candidate list.
    return JSONResponse({"ok": True, "result": result, **await _vault_review_snapshot(root, workspace)})


async def _vault_review_snapshot(root: Path, workspace: str) -> dict[str, Any]:
    """Regenerate the queue after a mutation and hand it back to the caller.

    The regeneration is not optional — the readable `Workspace/Vault-Review.md`
    projection is pending-only and has to reflect the mutation. Returning its
    result is what lets the client skip a third scan: `generate_candidates`
    reads every note in the vault three times (scan, validate, per-entry
    read_bytes), so a client that re-GETs after every decision made one row
    click cost ~3x that. The trash inventory rides along because `restore` and
    `delete` change it and the panel renders both lists.
    """
    from ciao import vault_review as review

    candidates = await asyncio.to_thread(
        functools.partial(
            review.generate_candidates,
            root,
            workspace=workspace,
            max_candidates=review.MAX_CANDIDATES_CEILING,
            write_queue=True,
        )
    )
    trashed = await asyncio.to_thread(
        functools.partial(review.list_trashed, root, workspace=workspace)
    )
    cleared = await asyncio.to_thread(
        functools.partial(review.list_cleared, root, workspace=workspace)
    )
    return {
        "candidates": [item.as_dict() for item in candidates],
        "trashed": trashed,
        "cleared": cleared,
    }


async def vault_delete_note(request: Request) -> JSONResponse:
    """Permanently delete one vault note from the Memory Map.

    ``path`` is the same id the graph, backlinks, and file viewer already use
    (the ``Entry.path`` string form, e.g. "memory-vault/work/People/Mo.md").
    Deliberately scoped to ``config.vault_root`` — unlike the workspace-file
    endpoints, this is a permanent, unrecoverable delete, so it does not
    inherit their "any file on disk" reach. Every other note that links to it
    (frontmatter ``related:``/``relatedTo:`` or a body markdown link) is
    rewritten first, so deleting a note never leaves a dangling link in the
    graph or in another note's text.
    """
    config = request.app.state.config
    raw = request.query_params.get("path", "").strip()
    if not raw:
        return JSONResponse({"error": "missing path"}, status_code=400)
    if Path(raw).suffix.lower() not in {".md", ".markdown"}:
        return JSONResponse({"error": "unsupported type"}, status_code=415)

    # The id is a path rendered by the scan, which is `memory-vault/...` on a
    # shared vault and `<root>/memory-vault/...` per agent root. Matching a fixed
    # `memory-vault/` prefix rejected every id on a migrated install, so the
    # Memory Map could not delete anything, and a matching prefix joined to
    # `config.vault_root` would have resolved outside any real vault. Resolving
    # against the scan's own targets keeps the containment check meaningful:
    # exactly one vault can own the note, and it must be under that one.
    vault_root = None
    resolved = None
    vault_prefix = None
    for target, _name, prefix in config.vault_scan_targets():
        marker = f"{prefix.as_posix()}/"
        if not raw.startswith(marker):
            continue
        try:
            candidate_root = Path(target).expanduser().resolve()
            candidate = (candidate_root / Path(raw[len(marker):])).resolve()
            candidate.relative_to(candidate_root)
        except (OSError, ValueError):
            continue
        vault_root, resolved, vault_prefix = candidate_root, candidate, prefix
        break
    if resolved is None or vault_root is None:
        return JSONResponse({"error": "not a vault note"}, status_code=400)
    if not resolved.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)

    # Probe the target directory before any backlink is rewritten: an
    # unwritable folder used to fail only at the final unlink, by which time
    # strip_references had already stripped live references out of other
    # notes. With the probe plus the staged cleanup, the residual window on
    # the unlink below is negligible; if it ever fires, the vault keeps both
    # the target and its valid references.
    try:
        with tempfile.NamedTemporaryFile(
            dir=resolved.parent, prefix=".ciao-delete-", suffix=".probe"
        ):
            pass
    except OSError as exc:
        return JSONResponse({"error": f"cannot delete: {exc}"}, status_code=500)

    # The prefix the id was rendered with, not the helper's default: without it
    # the cleanup scan compares `memory-vault/...` against a `<root>/...` id,
    # matches nothing, and leaves every backlink dangling.
    try:
        edited = await asyncio.to_thread(
            functools.partial(strip_references, vault_root, raw, path_prefix=vault_prefix)
        )
    except OSError as exc:
        # strip_references is all-or-nothing: a raise here means every staged
        # rewrite was rolled back and the vault is exactly as before.
        return JSONResponse({"error": f"backlink cleanup failed: {exc}"}, status_code=500)
    try:
        await asyncio.to_thread(resolved.unlink)
    except OSError as exc:
        return JSONResponse({"error": f"delete failed: {exc}"}, status_code=500)
    return JSONResponse({"ok": True, "edited_backlinks": edited})


# Binary downloads (PDFs, ZIPs, office docs) live under their own endpoint so
# the text and image viewers stay strictly typed. Same (unrestricted) path
# contract as ``workspace_file``/``workspace_image``: any allowlisted-extension
# file on disk is served, relative paths anchoring to the workspace. The browser
# decides whether to render inline (PDF) or save (everything else) based on
# the inferred MIME type. Saved-page archives are forced to download so their
# packaged HTML cannot execute under the PWA origin.
_WORKSPACE_BINARY_EXTS = frozenset({
    ".pdf", ".zip", ".docx", ".xlsx", ".pptx", ".mht", ".mhtml",
})
_WORKSPACE_BINARY_MAX_BYTES = 50 * 1024 * 1024  # 50 MB


def _find_soffice() -> str | None:
    import shutil
    for cmd in ("soffice", "libreoffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if shutil.which(cmd) or Path(cmd).exists():
            return cmd
    return None


async def libreoffice_status_endpoint(request: Request) -> JSONResponse:
    """Whether LibreOffice (soffice) is available to render .pptx previews."""
    return JSONResponse({"available": _find_soffice() is not None})


async def workspace_binary(request: Request) -> Response:
    """Serve an allowlisted binary file from the workspace."""
    config = request.app.state.config
    raw = request.query_params.get("path", "").strip()
    roots = _allowed_roots(config)
    result = _resolve_workspace_path(roots, raw, allow_fuzzy=True)
    if isinstance(result, Response):
        return result
    resolved = result

    if resolved.suffix.lower() not in _WORKSPACE_BINARY_EXTS:
        return JSONResponse({"error": "unsupported type"}, status_code=415)
    if resolved.stat().st_size > _WORKSPACE_BINARY_MAX_BYTES:
        return JSONResponse({"error": "file too large"}, status_code=413)

    source_ext = resolved.suffix.lower()
    is_raw = request.query_params.get("raw") == "1"
    filename = resolved.name
    media_type: str | None = None

    if resolved.suffix.lower() == ".pptx" and not is_raw:
        soffice = _find_soffice()
        if not soffice:
            return JSONResponse(
                {
                    "error": (
                        "LibreOffice is required to preview PowerPoint files in the PWA. "
                        "Please install it (e.g. `brew install --cask libreoffice` on macOS "
                        "or `apt install libreoffice` on Linux) and try again."
                    )
                },
                status_code=500,
            )

        import hashlib
        import shutil
        import tempfile

        cache_dir = Path(config.state_path).parent / "pptx_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        path_hash = hashlib.sha256(str(resolved.resolve()).encode("utf-8")).hexdigest()
        pdf_path = cache_dir / f"{path_hash}.pdf"

        if not pdf_path.exists() or resolved.stat().st_mtime > pdf_path.stat().st_mtime:
            with tempfile.TemporaryDirectory() as tmp_dir:
                conversion = await asyncio.to_thread(
                    subprocess.run,
                    [
                        soffice,
                        "--headless",
                        "--convert-to",
                        "pdf",
                        "--outdir",
                        tmp_dir,
                        str(resolved),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if conversion.returncode != 0:
                    return JSONResponse(
                        {"error": f"LibreOffice conversion failed: {conversion.stderr or conversion.stdout}"},
                        status_code=500,
                    )
                generated = Path(tmp_dir) / (resolved.stem + ".pdf")
                if not generated.exists():
                    return JSONResponse(
                        {"error": "LibreOffice did not produce a PDF output."},
                        status_code=500,
                    )
                shutil.move(str(generated), str(pdf_path))

        orig_stem = resolved.stem
        resolved = pdf_path
        media_type = "application/pdf"
        filename = f"{orig_stem}.pdf"
    else:
        media_type, _ = mimetypes.guess_type(resolved.name)
        if media_type is None:
            _FALLBACK_MIMES = {
                ".pdf": "application/pdf",
                ".zip": "application/zip",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            }
            media_type = _FALLBACK_MIMES.get(resolved.suffix.lower(), "application/octet-stream")

    # `inline` lets PDFs preview in a tab; saved-page archives are explicit
    # downloads. Custom frame headers let supported previews embed inside the
    # PWA's same-origin file viewer iframe.
    disposition = "attachment" if source_ext in {".mht", ".mhtml"} else "inline"
    headers = {
        "Content-Disposition": f'{disposition}; filename="{filename}"',
        "X-Frame-Options": "SAMEORIGIN",
        "Content-Security-Policy": _EMBEDDED_PREVIEW_CSP,
    }
    return FileResponse(
        resolved,
        media_type=media_type,
        headers=headers,
    )


async def workspace_image(request: Request) -> Response:
    """Serve a read-only image from disk.

    Same (unrestricted) path contract as ``workspace_file``: any file on disk
    is served, relative paths anchoring to ``config.workspace_root``. Extension
    must be in ``_WORKSPACE_IMAGE_EXTS``; the correct media type is inferred
    from the extension so browsers render it in ``<img>`` tags.

    Used by the markdown viewer to resolve relative image references (e.g.
    ``![alt](images/foo.png)`` inside a vault doc) against the doc's folder.
    """
    config = request.app.state.config
    raw = request.query_params.get("path", "").strip()
    roots = _allowed_roots(config)
    result = _resolve_workspace_path(roots, raw, allow_fuzzy=True)
    if isinstance(result, Response):
        return result
    resolved = result

    if resolved.suffix.lower() not in _WORKSPACE_IMAGE_EXTS:
        return JSONResponse({"error": "unsupported type"}, status_code=415)
    if resolved.stat().st_size > _WORKSPACE_IMAGE_MAX_BYTES:
        return JSONResponse({"error": "file too large"}, status_code=413)

    media_type, _ = mimetypes.guess_type(resolved.name)
    if media_type is None:
        # Fallback: SVGs and a few uncommon types occasionally miss the
        # mimetypes DB depending on platform. Map from the extension.
        _FALLBACK_MIMES = {
            ".svg": "image/svg+xml",
            ".avif": "image/avif",
            ".webp": "image/webp",
        }
        media_type = _FALLBACK_MIMES.get(resolved.suffix.lower(), "application/octet-stream")
    return FileResponse(resolved, media_type=media_type)


def _open_path_with_default_app(path: Path) -> None:
    """Open *path* with the OS default application on the machine running Ciao."""
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=True, timeout=30)
        return
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    opener = shutil.which("xdg-open")
    if not opener:
        raise OSError("xdg-open is not available on this platform")
    subprocess.run([opener, str(path)], check=True, timeout=30)


async def workspace_open(request: Request) -> Response:
    """Open a file with the OS default application on the machine running Ciao.

    Body: ``{"path": str}``. Uses the same path resolver as the workspace
    viewers (relative paths anchor to workspace_root; fuzzy basename lookup
    is allowed). The open happens server-side, so this only works when the
    PWA is talking to a local Ciao instance.
    """
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "bad json"}, status_code=400)
    raw = str(body.get("path", "")).strip()
    if not raw:
        return JSONResponse({"error": "missing path"}, status_code=400)

    config = request.app.state.config
    roots = _allowed_roots(config)
    result = _resolve_workspace_path(roots, raw, allow_fuzzy=True)
    if isinstance(result, Response):
        return result
    resolved = result

    try:
        await asyncio.to_thread(_open_path_with_default_app, resolved)
    except FileNotFoundError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except subprocess.CalledProcessError as exc:
        return JSONResponse(
            {"error": f"failed to open file (exit {exc.returncode})"},
            status_code=500,
        )
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True, "path": str(resolved)})


# ── File snapshots / history ─────────────────────────────────────────────
#
# The PWA renders a History and Diff tab on every file card. These routes
# back those tabs. The capture path lives in ``project_chats.py`` (broker
# event loop hooks ``SnapshotStore.schedule_capture`` on file-touch tool
# calls); these routes are read-only views over the same store.
#
# Path sandboxing: snapshots are keyed by ``chat_id`` and ``file_path`` as
# supplied by the agent — we don't re-validate the path here because the
# store URL-encodes it into a single directory component and lookups are
# purely string-keyed. There's no filesystem traversal possible from the
# store side. Reading a snapshot's blob also stays inside the store, never
# the original path.

def _resolve_chat_for_snapshots(request: Request):
    """Shared lookup: return (pcm, chat, chat_id, file_path) or a Response."""
    pcm = request.app.state.project_chat_manager
    chat_id = request.query_params.get("chat_id", "").strip()
    file_path = request.query_params.get("file_path", "").strip()
    if not chat_id or not file_path:
        return JSONResponse({"error": "missing chat_id or file_path"}, status_code=400)
    chat = pcm.get_chat(chat_id)
    if chat is None:
        return JSONResponse({"error": "chat not found"}, status_code=404)
    return pcm, chat, chat_id, file_path


async def file_history(request: Request) -> Response:
    """List snapshots for ``(chat_id, file_path)``. Newest last."""
    resolved = _resolve_chat_for_snapshots(request)
    if isinstance(resolved, Response):
        return resolved
    pcm, _chat, chat_id, file_path = resolved
    snapshots = pcm.snapshots.list_snapshots(chat_id=chat_id, file_path=file_path)
    return JSONResponse({"snapshots": snapshots})


async def file_content(request: Request) -> Response:
    """Return the content of one snapshot.

    Query: ``chat_id``, ``file_path``, ``seq`` (int). 404 if the snapshot
    doesn't exist. 413 if the snapshot was recorded as truncated (file was
    bigger than ``MAX_SNAPSHOT_BYTES`` at capture time).
    """
    resolved = _resolve_chat_for_snapshots(request)
    if isinstance(resolved, Response):
        return resolved
    pcm, _chat, chat_id, file_path = resolved
    try:
        seq = int(request.query_params.get("seq", "0"))
    except ValueError:
        return JSONResponse({"error": "bad seq"}, status_code=400)
    if seq <= 0:
        return JSONResponse({"error": "bad seq"}, status_code=400)

    result = pcm.snapshots.read_snapshot(
        chat_id=chat_id, file_path=file_path, seq=seq,
    )
    if result is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    content, meta = result
    if meta.get("truncated"):
        return JSONResponse(
            {"error": "snapshot was too large to capture", "meta": meta},
            status_code=413,
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return JSONResponse({"error": "binary snapshot, use workspace-binary"}, status_code=415)
    return JSONResponse({"content": text, "meta": meta})


async def file_restore(request: Request) -> Response:
    """Restore a snapshot's content to disk. Writes a new snapshot to mark
    the restore so the history stays append-only and the user can undo by
    restoring the previous version again.

    Body: ``{"chat_id": str, "file_path": str, "seq": int}``.
    Returns: ``{"ok": true, "restored_seq": int, "new_seq": int}``.
    """
    pcm = request.app.state.project_chat_manager
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "bad json"}, status_code=400)
    chat_id = str(body.get("chat_id", "")).strip()
    file_path = str(body.get("file_path", "")).strip()
    try:
        seq = int(body.get("seq", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad seq"}, status_code=400)
    if not chat_id or not file_path or seq <= 0:
        return JSONResponse({"error": "missing chat_id, file_path, or seq"}, status_code=400)
    if pcm.get_chat(chat_id) is None:
        return JSONResponse({"error": "chat not found"}, status_code=404)

    # Resolve the write target. There is no workspace sandbox: restoration
    # writes wherever the snapshot's recorded path points. Relative paths
    # anchor to the primary workspace root.
    config = request.app.state.config
    roots = _allowed_roots(config)
    try:
        candidate = Path(file_path).expanduser()
        resolved = candidate.resolve() if candidate.is_absolute() else (roots[0] / candidate).resolve()
    except (OSError, ValueError):
        return JSONResponse({"error": "bad path"}, status_code=400)

    snap = pcm.snapshots.read_snapshot(chat_id=chat_id, file_path=file_path, seq=seq)
    if snap is None:
        return JSONResponse({"error": "snapshot not found"}, status_code=404)
    content, meta = snap
    if meta.get("truncated"):
        return JSONResponse({"error": "snapshot was truncated, cannot restore"}, status_code=409)

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(content)
    except OSError as exc:
        return JSONResponse({"error": f"write failed: {exc}"}, status_code=500)

    # Capture the restored state as a new snapshot so history stays linear.
    new_meta = await pcm.snapshots.capture(
        chat_id=chat_id, file_path=file_path, action="restored", tool="Restore",
    )
    new_seq = new_meta.seq if new_meta else 0
    return JSONResponse({"ok": True, "restored_seq": seq, "new_seq": new_seq})


async def workspace_file_write(request: Request) -> Response:
    """Write user-edited content back to a workspace file from the in-PWA
    editor (FileViewerModal edit mode). Snapshots the result so the edit is
    auditable alongside agent edits.

    Body: ``{"chat_id": str, "path": str, "content": str}``. ``chat_id``
    determines which chat's history the snapshot lands in; if omitted the
    write still goes through but no snapshot is recorded.
    """
    pcm = request.app.state.project_chat_manager
    config = request.app.state.config
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "bad json"}, status_code=400)
    raw_path = str(body.get("path", "")).strip()
    content = body.get("content", "")
    chat_id = str(body.get("chat_id", "")).strip()
    if not raw_path or not isinstance(content, str):
        return JSONResponse({"error": "missing path or content"}, status_code=400)
    if len(content.encode("utf-8")) > _WORKSPACE_FILE_MAX_BYTES:
        return JSONResponse({"error": "content too large"}, status_code=413)

    roots = _allowed_roots(config)
    result = _resolve_workspace_path(roots, raw_path, allow_fuzzy=False)
    if isinstance(result, Response):
        # Resolver returns 404 for missing files. For an edit-and-save flow
        # we allow creating new files anywhere; relative paths still anchor
        # to the primary workspace root.
        try:
            candidate = Path(raw_path).expanduser()
            resolved = candidate.resolve() if candidate.is_absolute() else (roots[0] / candidate).resolve()
        except (OSError, ValueError):
            return JSONResponse({"error": "bad path"}, status_code=400)
    else:
        resolved = result
    if resolved.suffix.lower() not in _WORKSPACE_FILE_EXTS:
        return JSONResponse({"error": "unsupported type"}, status_code=415)

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content)
    except OSError as exc:
        return JSONResponse({"error": f"write failed: {exc}"}, status_code=500)

    snap_meta = None
    if chat_id and pcm.get_chat(chat_id) is not None:
        snap_meta = await pcm.snapshots.capture(
            chat_id=chat_id,
            file_path=str(resolved),
            action="edited",
            tool="PWAEdit",
        )
    return JSONResponse({
        "ok": True,
        "snapshot": snap_meta.to_dict() if snap_meta else None,
    })


# ── Schedules ───────────────────────────────────────────────────────────

def _enrich_schedule(
    entry: ScheduleEntry, pcm=None, *, now: datetime | None = None
) -> dict:
    """Serialize a ScheduleEntry and attach computed fields (context_label, next_run)."""
    entry_dict = asdict(entry)
    if pcm is not None and hasattr(pcm, "schedule_effective_routing"):
        provider, model, workspace = pcm.schedule_effective_routing(entry)
        entry_dict["effective_provider"] = provider
        entry_dict["effective_model"] = model
        entry_dict["workspace"] = workspace
    else:
        entry_dict["effective_provider"] = entry.provider
        entry_dict["effective_model"] = entry.model
    web_project_id = entry_dict.get("web_project_id")
    web_chat_id = entry_dict.get("web_chat_id")
    # Whether the target still resolves. Stated explicitly because the PWA
    # cannot infer it from context_label: that field is always set, so a
    # truthy label suppressed the "unavailable" indicator and a stale target
    # rendered as an ordinary one (previously as a bare `proj-...` id).
    entry_dict["context_available"] = True
    if web_project_id and pcm:
        project = pcm.get_project(web_project_id)
        if project:
            entry_dict["context_label"] = f"{project.name} (new chat per run)"
        else:
            # A stale id is not a label. Show the remembered name, which is
            # also what the dispatcher re-homes by.
            remembered = (entry_dict.get("web_project_name") or "").strip()
            entry_dict["context_label"] = (
                f"{remembered} (new chat per run)" if remembered else "Project not found"
            )
            entry_dict["context_available"] = bool(
                remembered and pcm.find_project(remembered, entry_dict.get("workspace") or "")
            )
    elif web_chat_id and pcm:
        chat = pcm.get_chat(web_chat_id)
        entry_dict["context_label"] = chat.title if chat else web_chat_id
        # A chat-bound entry whose chat was archived or deleted is still
        # dispatchable on any cadence while its project resolves — the run
        # continues in a replacement chat (fork or fresh, prepare_schedule_chat
        # re-homes every cadence now) — so do not mark it unavailable and send
        # the user to re-pick a target they do not need to change.
        entry_dict["context_available"] = chat is not None or (
            pcm.resolve_automation_project(entry) is not None
        )
    else:
        entry_dict["context_label"] = ""
    next_run = compute_next_run(entry)
    entry_dict["next_run"] = next_run.isoformat() if next_run is not None else None
    # "Missed" detection: a schedule whose last expected fire has passed but
    # which never recorded a trigger for that day, or whose run for that day
    # ended in failure instead of finishing. The 5-minute grace avoids
    # flagging a schedule during the brief window between its fire time and the
    # next poll tick (or the startup catch-up pass).
    last_expected = compute_last_expected_run(entry, now=now)
    entry_dict["last_expected_run"] = (
        last_expected.isoformat() if last_expected is not None else None
    )
    # Interval entries never report a missed run: compute_last_expected_run
    # returns None for them, so this stays False by construction.
    missed = False
    if last_expected is not None:
        expected_day = last_expected.date().isoformat()
        # A schedule is "missed" only if the cron path skipped this slot. A
        # manual "Run now" stamps ``last_dispatched_at`` but not
        # ``last_triggered_on``, so we also check the dispatch stamp: any
        # dispatch at or after the expected fire means the schedule was
        # attended to (even a late manual run the next morning), regardless of
        # whether the auto tick stamped the daily-idempotency key.
        dispatched_since_expected = was_dispatched_since(entry, last_expected)
        # ...unless the run that dispatch started never finished. Both stamps
        # are written at dispatch, before the outcome is known, so a turn that
        # died mid-flight (server restart, provider subprocess killed) marked
        # the slot as served and the work vanished with only a `last_status`
        # on the detail page to show for it (issue #486). A failed run leaves
        # its slot unsatisfied, so it belongs in the Missed list where "Run
        # all" can recover it. A run that completed — or one still waiting on
        # the user or the provider ("skipped") — never lands here.
        failed_since_expected = run_failed_since(entry, last_expected)
        not_triggered = failed_since_expected or (
            (not entry.last_triggered_on or expected_day > entry.last_triggered_on)
            and not dispatched_since_expected
        )
        overdue = ((now or datetime.now(UTC)) - last_expected) > timedelta(minutes=5)
        missed = not_triggered and overdue
    entry_dict["missed"] = missed
    entry_dict["last_dispatched_at"] = entry.last_dispatched_at or None
    return entry_dict


async def list_schedules(request: Request) -> JSONResponse:
    sm = request.app.state.schedule_manager
    pcm = request.app.state.project_chat_manager
    schedules = sm.list_entries()
    return JSONResponse([_enrich_schedule(s, pcm) for s in schedules])


async def list_automation(request: Request) -> JSONResponse:
    """Status of background automations for the Settings → Automation page.

    Reads the job-run log and returns one entry per automation this machine
    can actually run (jobs that never ran still appear), each with its last
    run, recent history, and aggregate stats. Scheduled jobs whose schedule is
    not installed here are omitted — nothing would ever trigger them.
    Read-only.

    ``?include=outcomes`` answers ``{"jobs": [...], "proposal_outcomes":
    {...}}`` instead of the bare list, adding the memory-proposal
    promoted-vs-dismissed tally the page renders next to the job stats. The
    default stays a bare list so existing consumers keep working unchanged.
    """
    from ciao import job_runs

    installed: set[str] | None = None
    try:
        sm = request.app.state.schedule_manager
        installed = {entry.schedule_id for entry in sm.list_entries()}
    except Exception:  # noqa: BLE001 — no schedule manager: filter nothing
        installed = None

    summary = job_runs.automation_summary(installed_schedules=installed)
    if request.query_params.get("include", "") != "outcomes":
        return JSONResponse(summary)
    return JSONResponse({
        "jobs": summary,
        "proposal_outcomes": proposal_outcomes.tally_cached(),
    })


async def trigger_backfill_insights(request: Request) -> JSONResponse:
    """Run session insights over every archive that is missing them.

    Accepts an optional ``model`` for a one-off run with a different model —
    the recovery path when the configured insights model keeps failing (it
    times out on slow local backends). The stored Settings → Models choice is
    left alone.
    """
    from ciao.job_runs import track
    from ciao.insights import backfill_insights_task, format_backfill_summary

    config = request.app.state.config
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — empty body means "use the configured model"
        body = {}
    body = body if isinstance(body, dict) else {}
    model = body.get("model")
    model = model.strip() if isinstance(model, str) else ""
    force = body.get("force") is True
    if not getattr(config, "insights_enabled", True) and not force:
        return JSONResponse(
            {"error": "session insights are disabled in Settings"},
            status_code=409,
        )
    coordinator = getattr(request.app.state, "backfill_coordinator", None)
    if coordinator is None:
        return JSONResponse({"error": "backfill coordinator unavailable"}, status_code=503)
    chat_workspaces = request.app.state.project_chat_manager.chat_workspaces()

    async def _run_backfill():
        async with track(
            "backfill_insights", "Insights backfill", category="system",
            model=model,
        ) as handle:
            result = await backfill_insights_task(
                config,
                mode="both",
                model_override=model,
                manual=True,
                force=force,
                chat_workspaces=chat_workspaces,
            )
            handle.extra.update(result)
            summary = format_backfill_summary(result)
            handle.extra["summary"] = summary
            if model:
                handle.extra["model_override"] = model
            if force:
                handle.extra["forced"] = True
            if result["errors"]:
                handle.status = "error"
                handle.error = summary

    coordinator.submit(_run_backfill)
    return JSONResponse(
        {"status": "queued", "model": model, "forced": force},
        status_code=202,
    )


async def create_schedule(request: Request) -> JSONResponse:
    sm = request.app.state.schedule_manager
    pcm = request.app.state.project_chat_manager
    body = await request.json()

    web_chat_id = body.get("web_chat_id")
    web_project_id = body.get("web_project_id")

    # Persist only explicit overrides. Empty model/provider values mean
    # "inherit the selected workspace" and are resolved afresh at dispatch
    # time, so changing a workspace default also changes future runs.
    model = (body.get("model") or "").strip()
    # Left unset on purpose. The form offers no permission-mode choice, so
    # capturing one here froze whatever the Telegram context happened to be on
    # into the entry; mode inherits at dispatch like model and provider do.
    mode = ""

    frequency = body.get("frequency", "weekly")
    if frequency not in FREQUENCIES:
        return JSONResponse(
            {"error": f"unknown frequency '{frequency}'"}, status_code=400
        )
    # Interval cadence is measured from the last dispatch, so it needs no time
    # of day — but it does need a cadence and a target, since an entry with
    # neither would tick forever against nothing.
    interval_minutes = 0
    if frequency == INTERVAL_FREQUENCY:
        try:
            interval_minutes = normalize_interval_minutes(
                body.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if not web_chat_id and not web_project_id:
            return JSONResponse(
                {"error": "interval schedules require web_chat_id or web_project_id"},
                status_code=400,
            )
        if web_chat_id and pcm.get_chat(web_chat_id) is None:
            return JSONResponse(
                {"error": "web_chat_id must point to an existing chat"},
                status_code=400,
            )
    run_at_date = body.get("run_at_date")
    # Reject one-off schedules pointed at a past datetime — they would
    # never auto-fire, and silently keeping them around is worse than 400.
    if frequency == "once":
        if not run_at_date or not body.get("time"):
            return JSONResponse(
                {"error": "once schedules require run_at_date and time"},
                status_code=400,
            )
        try:
            target_date = datetime.fromisoformat(run_at_date).date()
            hh, mm = body["time"].split(":")
            tz = ZoneInfo(body.get("timezone", "Europe/Zurich"))
            target_dt = datetime(
                target_date.year, target_date.month, target_date.day,
                int(hh), int(mm), tzinfo=tz,
            )
        except (ValueError, KeyError):
            return JSONResponse({"error": "invalid run_at_date or time"}, status_code=400)
        if target_dt <= datetime.now(tz):
            return JSONResponse(
                {"error": "run_at_date must be in the future"},
                status_code=400,
            )

    # Manual and interval schedules don't need a time of day. For every other
    # cadence the entry is unusable without one: compute_next_run returns None,
    # tick() never matches, and the row sits there reading as enabled while
    # silently never firing. PATCH has always rejected this — and, because it
    # re-validates the whole entry, an entry created this way could not even be
    # paused afterwards. Same gate, same message, on the create door.
    time_error = wall_clock_time_value_error(frequency, str(body.get("time") or ""))
    if time_error:
        return JSONResponse({"error": time_error}, status_code=400)
    provider = (body.get("provider") or "").strip()
    if provider and provider not in supported_providers():
        return JSONResponse({"error": f"unknown provider '{provider}'"}, status_code=400)
    try:
        archive_policy = normalize_archive_policy(body.get("archive_policy"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    # Stamp the workspace from the target project so the schedule still routes
    # correctly after a fresh init regenerates project IDs (web_project_id goes
    # stale; workspace survives). Explicit body override wins.
    workspace = (body.get("workspace") or "").strip().lower()
    known_workspaces = _known_workspace_names(pcm)
    target_project = pcm.get_project(web_project_id) if web_project_id else None
    if workspace not in known_workspaces and web_project_id:
        workspace = target_project.workspace if target_project else ""
    if workspace not in known_workspaces and web_chat_id:
        # A chat-bound entry has no project id to stamp from, but it still
        # needs a workspace: `resolve_automation_project` is what lets an
        # interval run continue in a replacement chat once the target chat is
        # archived or deleted, and with neither field set it returns None and
        # the entry is disabled instead of re-homed. Derive it from the chat's
        # own project, which the retired loop routes always did.
        target_chat = pcm.get_chat(web_chat_id)
        chat_project = (
            pcm.get_project(target_chat.project_id)
            if target_chat is not None and target_chat.project_id
            else None
        )
        workspace = getattr(chat_project, "workspace", "") or ""
    entry = sm.create(
        daily_time_utc=body.get("time") or "",
        prompt=body["prompt"],
        model=model,
        provider=provider,
        mode=mode,
        chat_id=body.get("chat_id", 0),
        timezone_name=body.get("timezone", "Europe/Zurich"),
        days_of_week=body.get("days_of_week"),
        thread_id=body.get("thread_id"),
        frequency=frequency,
        interval_minutes=interval_minutes,
        day_of_month=body.get("day_of_month"),
        run_at_date=run_at_date,
        web_chat_id=web_chat_id,
        web_project_id=web_project_id,
        web_project_name=target_project.name if target_project else "",
        archive_policy=archive_policy,
        workspace=workspace if workspace in known_workspaces else "",
        title=str(body.get("title", "")).strip(),
        description=str(body.get("description", "")).strip(),
    )
    # Records where a chat-bound entry re-homes once its chat is deleted; must
    # be captured while that chat still exists. See stamp_fallback_project.
    if stamp_fallback_project(entry, pcm):
        sm.replace(entry)
    publish_automations_changed(pcm)
    return JSONResponse(_enrich_schedule(entry, pcm), status_code=201)


async def run_schedule_now(request: Request) -> JSONResponse:
    """Trigger a schedule immediately."""
    schedule_id = request.path_params["schedule_id"]
    sm = request.app.state.schedule_manager
    try:
        result = await sm.dispatch_now(schedule_id)
    except ValueError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except RuntimeError as exc:
        if "paused" in str(exc).lower():
            return JSONResponse({"error": str(exc)}, status_code=409)
        raise
    # Interval entries refuse rather than queue: a manual run into a chat that
    # is already streaming would stack a second prompt behind the live turn.
    if result.get("status") == "busy":
        return JSONResponse(
            {"error": "chat has a turn in flight; retry when it finishes", **result},
            status_code=409,
        )
    if result.get("status") == "missing-chat":
        return JSONResponse(
            {"error": "target chat no longer exists", **result}, status_code=409
        )
    publish_automations_changed(request.app.state.project_chat_manager)
    return JSONResponse(result, status_code=201)


async def schedule_detail(request: Request) -> JSONResponse:
    """Handle PATCH (update) and DELETE for a single schedule."""
    schedule_id = request.path_params["schedule_id"]
    if request.method == "DELETE":
        sm = request.app.state.schedule_manager
        ok = sm.delete(schedule_id)
        publish_automations_changed(request.app.state.project_chat_manager)
        return JSONResponse({"ok": ok})
    # PATCH
    store = request.app.state.schedule_manager._store
    entry = store.get(schedule_id)
    if entry is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    body = await request.json()
    if "title" in body:
        entry.title = str(body["title"]).strip()
    if "description" in body:
        entry.description = str(body["description"]).strip()
    if "time" in body:
        entry.daily_time_utc = body["time"]
    if "prompt" in body:
        entry.prompt = body["prompt"]
    if "timezone" in body:
        entry.timezone_name = body["timezone"]
    if "days_of_week" in body:
        entry.days_of_week = body["days_of_week"] or None
    if "thread_id" in body:
        entry.thread_id = body["thread_id"] or None
    if "chat_id" in body:
        entry.chat_id = body["chat_id"]
    if "frequency" in body:
        if body["frequency"] not in FREQUENCIES:
            return JSONResponse(
                {"error": f"unknown frequency '{body['frequency']}'"}, status_code=400
            )
        entry.frequency = body["frequency"]
        # Switching to interval without naming a cadence would leave 0 stored,
        # which interval_delta floors to one minute — far faster than anything
        # the caller asked for. Seed the default instead.
        if is_interval(entry) and not entry.interval_minutes:
            entry.interval_minutes = DEFAULT_INTERVAL_MINUTES
    if "interval_minutes" in body:
        try:
            entry.interval_minutes = normalize_interval_minutes(body["interval_minutes"])
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    if "day_of_month" in body:
        entry.day_of_month = body["day_of_month"]
    if "run_at_date" in body:
        entry.run_at_date = body["run_at_date"] or None
    if "web_chat_id" in body:
        entry.web_chat_id = body["web_chat_id"] or None
    if "web_project_id" in body:
        entry.web_project_id = body["web_project_id"] or None
        # Re-stamp the workspace and the target's name. Project ids regenerate
        # per instance, so the name is what lets a later run find the same
        # project again instead of silently falling back to General.
        pcm = request.app.state.project_chat_manager
        project = pcm.get_project(entry.web_project_id) if entry.web_project_id else None
        entry.workspace = project.workspace if project else ""
        entry.web_project_name = project.name if project else ""
    if "workspace" in body:
        ws = (body["workspace"] or "").strip().lower()
        pcm = request.app.state.project_chat_manager
        entry.workspace = ws if ws in _known_workspace_names(pcm) else ""
    if "model" in body:
        # Empty means "inherit workspace default" and must stay empty so the
        # next dispatch observes any workspace configuration change.
        entry.model = (body["model"] or "").strip()
    if "provider" in body:
        new_provider = (body["provider"] or "").strip()
        if new_provider and new_provider not in supported_providers():
            return JSONResponse(
                {"error": f"unknown provider '{new_provider}'"}, status_code=400
            )
        entry.provider = new_provider
    try:
        if "archive_policy" in body:
            entry.archive_policy = normalize_archive_policy(body.get("archive_policy"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if "enabled" in body:
        entry.enabled = bool(body["enabled"])
    # Never store an automation that cannot run. Shared with the MCP write path
    # (CiaoControlPlane.schedule_update) so the two cannot drift.
    time_error = wall_clock_time_error(entry)
    if time_error:
        return JSONResponse({"error": time_error}, status_code=400)
    # Retargeting the chat moves where this entry re-homes, so the fallback has
    # to move with it — otherwise it keeps pointing at the previous chat's
    # project. Also clears it when the entry becomes project-bound.
    stamp_fallback_project(entry, request.app.state.project_chat_manager)
    try:
        store.replace(entry)
    except ValueError as exc:
        # A fanned-out system routine's workspace is part of its id, so the
        # store refuses to "move" it. That is a bad request, not a server
        # fault: the PWA hides the control for those rows, but a direct API
        # caller would otherwise get a 500 for asking something answerable.
        return JSONResponse({"error": str(exc)}, status_code=400)
    pcm = request.app.state.project_chat_manager
    publish_automations_changed(pcm)
    return JSONResponse(_enrich_schedule(entry, pcm))


# ── Models ───────────────────────────────────────────────────────────────

async def list_models(request: Request) -> JSONResponse:
    config = request.app.state.config
    # `?refresh=1` bypasses the opencode catalog cache. The catalog is served on
    # demand so a provider connected in another window shows up immediately.
    refresh = str(request.query_params.get("refresh", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    opencode_catalog = await OpencodeProvider.model_catalog(
        config.workspace_root, force=refresh
    )
    # opencode is bring-your-own-provider: its catalog is whatever backends the
    # user has connected, so an empty list simply means "not signed in yet".
    opencode_models = [
        str(item.get("model") or "") for item in opencode_catalog if item.get("model")
    ]
    opencode_operator_default = config.default_model_for_provider("opencode")
    if opencode_operator_default in opencode_models:
        opencode_default = opencode_operator_default
    else:
        opencode_default = opencode_models[0] if opencode_models else ""
    # Per-model reasoning-effort variants for opencode.
    opencode_reasoning_levels = {
        str(item.get("model")): list(item.get("variants") or [])
        for item in opencode_catalog
        if item.get("model")
    }
    model_reasoning_levels = opencode_reasoning_levels
    # Claude Code serves one upstream, so its models are a single list rather
    # than the work/personal split the routing-backend era needed.
    claude_models = list(CLAUDE_MODELS)
    claude_default = (
        config.claude_default_model
        if config.claude_default_model in claude_models
        else claude_models[0]
    )

    return JSONResponse({
        "models": list(CLAUDE_MODELS),
        "default": config.claude_default_model,
        "provider_models": {
            "claude": claude_models,
            "opencode": opencode_models,
        },
        "provider_defaults": {
            "claude": claude_default,
            "opencode": opencode_default,
        },
        "backends": {
            "anthropic": True,
            "opencode": bool(opencode_models),
        },
        "opencode_models": opencode_models,
        # Registry-driven descriptors so the PWA can build its provider list
        # (labels, buckets, capabilities) without a hard-coded union.
        "providers": [
            {
                "id": item.id,
                "label": item.label,
                "short_label": item.short_label,
                "capabilities": asdict(capabilities_for(item.id)),
            }
            for item in provider_registry.descriptors()
        ],
        "model_reasoning_levels": model_reasoning_levels,
        "thinking_levels": {k: list(v) for k, v in THINKING_LEVELS.items()},
    })


# ── Routine settings (Settings → Models tab) ────────────────────────────

def _routines_payload(config, app_settings) -> dict:
    """Shared GET/PATCH response: overrides, effective values, options."""
    s = app_settings.settings
    from ciao.critique import critique_models_effective

    critique_effective = critique_models_effective(config)
    if config.insights_model_override:
        insights_effective = config.insights_model_override
    else:
        insights_effective = config.default_model_for_workspace(
            config.primary_workspace()
        )

    # On Automatic the insights routine resolves per workspace
    # (resolve_insights_model takes the chat's workspace), so the single
    # *_effective value above is only the primary-workspace answer. Reporting it
    # alone reads as a global choice and is wrong for every other workspace, so
    # ship the whole map and let the UI say what actually varies. Empty when an
    # override is set, because then one model really does apply everywhere.
    insights_by_workspace: dict[str, str] = {}
    for name in config.workspace_names():
        if not config.insights_model_override:
            insights_by_workspace[name] = config.default_model_for_workspace(name)

    return {
        # Overrides as stored ("" = automatic default).
        "insights_model": s.insights_model,
        "insights_enabled": config.insights_enabled,
        "trajectories_enabled": config.trajectories_enabled,

        "critique_models": s.critique_models,
        # Per-provider default model for new chats, as stored (missing =
        # provider's own catalog default).
        "provider_default_models": s.provider_default_models or {},
        # Per-provider default execution (permission) mode for new chats, as
        # stored ("manual" / "auto" / "bypass"; missing = app default auto).
        "provider_default_modes": {
            provider: ("manual" if mode == "normal" else mode)
            for provider, mode in (s.provider_default_modes or {}).items()
        },
        # Per-provider default thinking level for new chats, as stored.
        "provider_default_thinking": s.provider_default_thinking or {},
        # Per-provider routine models, as stored (missing = provider default).
        "provider_insights_models": s.provider_insights_models or {},
        # What actually runs right now, after defaults.
        "insights_model_effective": insights_effective,
        # Per-workspace resolution for the Automatic case; empty when overridden.
        "insights_model_by_workspace": insights_by_workspace,

        "critique_models_effective": critique_effective,
        # Grouped options for the routine model selectors.
        "model_options": {
            "anthropic": list(CLAUDE_MODELS),
        },
        "backends": {
            "anthropic": True,
        },
        "workspace_context": {
            "workspace_root": str(config.workspace_root),
            # `vault_root` is the configured path, which after the re-rooting is
            # the emptied shared one — true but useless on its own, so the vaults
            # that actually hold notes are reported beside it.
            "vault_root": str(config.vault_root),
            "vault_roots": [
                {"workspace": name, "path": str(root)}
                for root, name, _prefix in (
                    config.vault_scan_targets()
                    if hasattr(config, "vault_scan_targets")
                    else []
                )
            ],
        },
    }


async def settings_routines(request: Request) -> JSONResponse:
    """GET returns routine settings; PATCH updates the runtime overrides.

    Persisted in ``.runtime/app_settings.json`` and applied to the live
    config immediately — no restart needed. Empty string clears an
    override back to the env-backed default.
    """
    config = request.app.state.config
    app_settings = request.app.state.app_settings
    if app_settings is None:
        return JSONResponse({"error": "settings store unavailable"}, status_code=503)
    if request.method == "PATCH":
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "expected an object"}, status_code=400)
        try:
            app_settings.update(body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        app_settings.apply_to_config(config)
    return JSONResponse(_routines_payload(config, app_settings))


# ── Status ───────────────────────────────────────────────────────────────

async def status_endpoint(request: Request) -> JSONResponse:
    """GET returns status, PATCH updates model/mode."""
    state = request.app.state.state_store
    ctx = ChatContext(chat_id=0)
    ctx_state = state.get_context(ctx)
    if request.method == "PATCH":
        body = await request.json()
        if "model" in body:
            state.set_active_model(body["model"], ctx)
        if "mode" in body:
            state.set_mode(body["mode"], ctx)
        ctx_state = state.get_context(ctx)

    return JSONResponse({
        "active_model": ctx_state.active_model,
        "mode": ctx_state.mode,
        "cost": state.bot_state.cost,
    })


async def startup_status_endpoint(request: Request) -> JSONResponse:
    """Return startup phase progress and node role state."""
    from ciao import __version__

    node_mgr = getattr(request.app.state, "node_state_manager", None)
    role = node_mgr.get_role() if node_mgr else "host"
    active_peer_url = node_mgr.get_active_peer_url() if node_mgr else None
    config = getattr(request.app.state, "config", None)

    tracker = getattr(request.app.state, "startup_tracker", None)
    payload = tracker.to_dict() if tracker is not None else {"phases": [], "overall_ready": True}
    latest_version, update_available = await _cached_update_hint(request)
    payload.update({
        "version": __version__,
        "desktop_api_version": 1,
        # Identifies the machine that answered. A client asks its host for this
        # so the mirrored UI can name whose data it is showing.
        "node_id": node_mgr.node_id if node_mgr else "",
        "node_role": role,
        "state_valid": bool(node_mgr.is_valid()) if node_mgr else True,
        "active_peer_url": active_peer_url,
        "host_url": node_mgr.get_host_url() if node_mgr else None,
        "has_host_session": bool(node_mgr.get_host_session()) if node_mgr else False,
        "auth_required": bool(getattr(config, "pwa_auth_required", False)) if config else False,
        "latest_version": latest_version,
        "update_available": update_available,
    })
    return JSONResponse(payload)


async def _refresh_update_hint(app: Any, fetcher: Callable[[], dict[str, object]]) -> None:
    """Populate the cached update hint off the request path."""

    try:
        status = await asyncio.to_thread(fetcher)
    except Exception:
        # Deliberately broad: this runs detached, and a failed release lookup
        # must never surface as an unhandled task exception.
        return
    app.state.update_hint = (
        str(status.get("latest_version") or ""),
        bool(status.get("update_available")),
    )


async def _cached_update_hint(request: Request) -> tuple[str, bool]:
    """Return ``(latest_version, update_available)`` without ever blocking.

    The menu bar polls this endpoint on a short client timeout to decide whether
    the engine is alive, so the release lookup must stay off the request path
    entirely. ``asyncio.to_thread`` cannot be cancelled, so even a ``wait_for``
    around it would block until the thread finished — instead the lookup runs
    detached and this only reads the last value it stored. The first poll after
    a cold start reports no hint; the next one picks it up.
    """

    app = request.app
    fetcher = getattr(app.state, "package_status_fetcher", None)
    if not callable(fetcher):
        return "", False
    task = getattr(app.state, "update_hint_task", None)
    if task is None or task.done():
        app.state.update_hint_task = asyncio.create_task(_refresh_update_hint(app, fetcher))
    return getattr(app.state, "update_hint", ("", False))


async def active_chats_endpoint(request: Request) -> JSONResponse:
    """Return chat IDs with in-flight work (streaming or background subagents).

    Drives the macOS menu bar: it spins the icon while anything is working and
    marks those chats in the open-chats list. Unauthenticated like the
    startup-status endpoint, since the local menu bar process has no session;
    it only leaks opaque chat IDs, not their contents.
    """
    pcm = getattr(request.app.state, "project_chat_manager", None)
    if pcm is None:
        return JSONResponse({"active_chat_ids": []})
    return JSONResponse({"active_chat_ids": pcm.active_chat_ids()})


def _menubar_chat_needs_input(pending_question: str, pending_permission: str = "") -> bool:
    """True when AskUserQuestion JSON or an Approve/Deny prompt is waiting."""
    if (pending_permission or "").strip():
        return True
    raw = (pending_question or "").strip()
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(parsed, dict):
        return False
    questions = parsed.get("questions")
    return isinstance(questions, list) and len(questions) > 0


async def menubar_chats_endpoint(request: Request) -> JSONResponse:
    """Open-chat summaries for the macOS menu bar.

    Usable without a session, but only from a loopback peer (the tray holds no
    session cookie) — see ``_LOOPBACK_ONLY_API`` in ``ciao.web.auth``. Unlike
    ``/api/active-chats`` this returns titles and workspace names, so it must
    not be reachable from the network.

    In client mode the proxy forwards this to the active peer so the tray list
    matches the chats that ``/api/active-chats`` reports as working — local
    ``web_projects.json`` can lag the leader after handover.
    """
    limit_raw = request.query_params.get("limit", "10")
    try:
        limit = max(1, min(50, int(limit_raw)))
    except ValueError:
        limit = 10

    pcm = getattr(request.app.state, "project_chat_manager", None)
    if pcm is None:
        return JSONResponse({"chats": [], "attention_count": 0})
    chats = pcm.list_chats()
    rows: list[dict[str, object]] = []
    attention_count = 0
    for chat in chats:
        if chat.archived:
            continue
        project = pcm.get_project(chat.project_id)
        if project is None:
            continue
        activity = chat.last_activity_at or ""
        read = chat.last_read_at or ""
        unread = bool(activity) and activity > read
        needs_input = _menubar_chat_needs_input(
            chat.pending_question, getattr(chat, "pending_permission", "")
        )
        if unread or needs_input:
            attention_count += 1
        rows.append(
            {
                "chat_id": chat.chat_id,
                "title": chat.title or "Untitled chat",
                "workspace": project.workspace,
                "last_activity_at": activity,
                "unread": unread,
                "needs_input": needs_input,
            }
        )
    rows.sort(key=lambda row: str(row.get("last_activity_at") or ""), reverse=True)
    # attention_count is counted over every non-archived chat but the list is
    # truncated to `limit`, so a chat needing attention that is not among the
    # most recent would be counted in the menu bar badge with no row to explain
    # it. Float those chats to the front (stable, so recency order survives
    # within each group) — the badge then always has something to point at.
    rows.sort(key=lambda row: not (row["unread"] or row["needs_input"]))
    return JSONResponse({"chats": rows[:limit], "attention_count": attention_count})


async def open_chat_endpoint(request: Request) -> JSONResponse:
    """Ask an already-open PWA to navigate to a chat.

    macOS ``open -a PWA /chat/...`` often focuses the installed app without
    changing the window URL when it is already running. The menu bar calls
    this unauthenticated local endpoint first; connected clients receive an
    ``open_chat`` event over ``/ws/events`` and switch chats in place.
    """
    chat_id = str(request.path_params.get("chat_id") or "").strip()
    if not chat_id:
        return JSONResponse({"ok": False, "error": "missing chat_id"}, status_code=400)
    pcm = getattr(request.app.state, "project_chat_manager", None)
    if pcm is None or pcm.get_chat(chat_id) is None:
        return JSONResponse({"ok": False, "error": "chat not found"}, status_code=404)
    delivered = bool(getattr(pcm.events, "subscriber_count", 0))
    pcm.events.publish({"type": "open_chat", "chat_id": chat_id})
    return JSONResponse({"ok": True, "chat_id": chat_id, "delivered": delivered})


async def setup_status_endpoint(request: Request) -> JSONResponse:
    """Return first-run setup readiness for the onboarding wizard."""
    return JSONResponse(await asyncio.to_thread(setup_status, request.app.state.config))



def _host_name(value: str) -> str:
    host = value.strip()
    if host.startswith("["):
        end = host.find("]")
        host = host[1:end] if end != -1 else host
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return host.rstrip(".").lower()


def _localhost_request(request: Request) -> bool:
    name = _host_name(request.headers.get("host", ""))
    if not name:
        name = (request.url.hostname or "").rstrip(".").lower()
    # 0.0.0.0 counts as loopback: a browser pointed at it can only reach the
    # viewer's own machine (users copy it from the uvicorn bind-address log).
    return name in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _same_host_header(request: Request, value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if not parsed.hostname:
        return False
    request_host = _host_name(request.headers.get("host", ""))
    if not request_host:
        request_host = (request.url.hostname or "").rstrip(".").lower()
    return parsed.hostname.rstrip(".").lower() == request_host


def _setup_finish_origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin:
        return _same_host_header(request, origin)
    referer = request.headers.get("referer")
    if referer:
        return _same_host_header(request, referer)
    return True


def _interactive_foreground_run() -> bool:
    """True when setup can hand the bootstrap server to launchd.

    The bundled desktop app deliberately starts bootstrap with no terminal
    attached, but it still owns the one-time onboarding process and must hand
    the configured server to the LaunchAgent when setup completes.
    """
    try:
        return sys.stderr.isatty() or os.environ.get("CIAO_BOOTSTRAP_LAUNCHD_HANDOFF") == "1"
    except (AttributeError, ValueError):
        return os.environ.get("CIAO_BOOTSTRAP_LAUNCHD_HANDOFF") == "1"


def _schedule_launchd_server_handoff() -> bool:
    """Spawn a detached helper that starts the launchd server agent.

    The helper runs after this process exits (a foreground `ciao run` still
    holds the port), so the wizard's finish can hand the server to launchd
    and the user can close the terminal. The agent's RunAtLoad + KeepAlive
    cover the race: if the port is still held on first launch, launchd
    retries. Returns False when the plist is missing or the spawn fails, in
    which case the caller falls back to the in-place re-exec restart.
    """
    plist = Path.home() / "Library" / "LaunchAgents" / "com.ciao.server.plist"
    if not plist.exists():
        return False
    script = (
        "sleep 3; "
        f"/bin/launchctl load -w '{plist}' 2>/dev/null; "
        f"/bin/launchctl kickstart gui/{os.getuid()}/com.ciao.server 2>/dev/null; "
        "exit 0"
    )
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", script],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    print(
        "\nSetup complete — Ciaobot is moving to the background service.\n"
        "You can close this terminal; the server now starts automatically at login.\n",
        file=sys.stderr,
        flush=True,
    )
    return True


async def setup_finish_endpoint(request: Request) -> JSONResponse:
    """Write real setup config from bootstrap mode and request supervisor restart."""
    config = request.app.state.config
    if not getattr(config, "bootstrap_mode", False):
        return JSONResponse({"error": "setup finish is only available in bootstrap mode"}, status_code=409)
    if not _localhost_request(request) or not _setup_finish_origin_allowed(request):
        return JSONResponse(
            {
                "error": "setup finish is localhost-only — open the wizard at "
                f"http://localhost:{config.pwa_port}"
            },
            status_code=403,
        )
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "json object is required"}, status_code=400)

    # The wizard's primary question is the workspace: one root folder holding
    # the vault (memory-vault/ by default) plus app data, all one git repo.
    # vault_root is optional and only set when the second brain lives
    # elsewhere (existing notes folder).
    workspace = str(body.get("workspace", "")).strip()
    if not workspace:
        return JSONResponse({"error": "workspace is required"}, status_code=400)
    from ciao.setup_status import tcc_protected_location

    protected = tcc_protected_location(workspace)
    if protected:
        return JSONResponse(
            {
                "error": (
                    f"'{workspace}' is inside ~/{protected}, which macOS privacy "
                    "protection blocks background services from reading — the "
                    "Ciaobot server and menu bar would fail to start. Pick a "
                    "folder outside ~/Desktop, ~/Documents, and ~/Downloads "
                    "(for example ~/ciaobot)."
                )
            },
            status_code=400,
        )
    try:
        port = int(body.get("port") or config.pwa_port)
    except (TypeError, ValueError):
        return JSONResponse({"error": "port must be an integer"}, status_code=400)
    if port < 1 or port > 65535:
        return JSONResponse({"error": "port must be between 1 and 65535"}, status_code=400)
    default_provider = str(body.get("provider") or "claude").strip().lower()
    if default_provider not in _workspace_provider_values(config):
        return JSONResponse(
            {"error": f"unknown provider '{default_provider}'"}, status_code=400
        )

    from ciao.cli import detect_vault_mode, setup_workspace

    # The wizard no longer asks scratch-vs-existing: when the request does
    # not pin a mode, inspect the folder — empty starts from scratch, one
    # with visible content is an existing notes folder the onboarding agent
    # adapts in place.
    vault_mode = str(body.get("vault_mode", "")).strip().lower() or detect_vault_mode(workspace)
    workspace_name = str(body.get("workspace_name", "")).strip() or "personal"
    if not _WORKSPACE_NAME_RE.fullmatch(workspace_name):
        return JSONResponse(
            {
                "error": (
                    "workspace name must use letters, numbers, dashes, "
                    "or underscores"
                )
            },
            status_code=400,
        )

    # Password protection is not optional: the wizard collects the password, and
    # the bootstrap token it would otherwise inherit is a machine-generated
    # value nobody could type on a second device.
    from ciao.web.auth import MIN_PWA_PASSWORD_LENGTH

    password = str(body.get("password") or body.get("auth_token") or "").strip()
    if len(password) < MIN_PWA_PASSWORD_LENGTH:
        return JSONResponse(
            {
                "error": (
                    "password is required and must be at least "
                    f"{MIN_PWA_PASSWORD_LENGTH} characters"
                )
            },
            status_code=400,
        )

    # setup_workspace only writes workspace files and the bundled-engine
    # LaunchAgent. The release installer owns app downloads, so setup remains
    # responsive and never reaches out to GitHub.
    try:
        written = await asyncio.to_thread(
            functools.partial(
                setup_workspace,
                workspace,
                auth_token=password,
                auth_required=True,
                vault_root=str(body.get("vault_root", "")).strip() or None,
                vault_mode=vault_mode,
                workspace_name=workspace_name,
                default_provider=default_provider,
                python_path=str(body.get("python", "")).strip() or None,
                port=port,
                launch_agents_dir=str(body.get("launch_agents_dir", "")).strip() or None,
                app_dir=str(body.get("app_dir", "")).strip() or None,
            )
        )
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    # Hand the chosen workspace to the relaunched process. A foreground
    # `ciao run` restarts by re-execing itself with the current environment,
    # and nothing else tells the fresh process where setup landed — without
    # this it boots straight back into the bootstrap wizard.
    os.environ["CIAO_WORKSPACE"] = str(Path(workspace).expanduser().resolve())
    os.environ["PWA_PORT"] = str(port)
    # Same reason for the credentials: `load_dotenv` does not override values
    # already in the environment, so a stale PWA_AUTH_TOKEN inherited from the
    # bootstrap process would outrank the password just written to .env.
    os.environ["PWA_AUTH_TOKEN"] = password
    os.environ["PWA_AUTH_REQUIRED"] = "true"
    # Only the real per-user LaunchAgents dir may be registered with launchd —
    # scripted/test setups pass a custom dir and must not touch it. Nothing
    # menu-bar related happens here any more: Ciaobot.app is the menu bar, and
    # setup_workspace above has just retired the old `com.ciao.menubar` agent.
    real_launch_agents = (
        sys.platform == "darwin"
        and not str(body.get("launch_agents_dir", "")).strip()
    )

    restart = bool(body.get("restart", True))
    # An interactive foreground `ciao run` (the documented install flow) hands
    # the server over to launchd instead of re-execing: a detached helper
    # loads the server agent once this process has exited and released the
    # port, and the wizard requests a clean exit (code 0, no relaunch). The
    # user can then close the terminal. Under launchd stderr is a log file,
    # not a TTY, so a supervised server keeps the plain re-exec restart.
    handoff = (
        restart
        and real_launch_agents
        and _interactive_foreground_run()
        and _schedule_launchd_server_handoff()
    )
    if restart:
        restart_fn = getattr(request.app.state, "request_restart", None)
        if callable(restart_fn):
            restart_fn(0 if handoff else RESTART_EXIT_CODE)

    return JSONResponse({
        "ok": True,
        "restart_requested": restart,
        "workspace": str(Path(workspace).expanduser().resolve()),
        "written": [str(path) for path in written],
    })


def _setup_fs_guard(request: Request) -> JSONResponse | None:
    """Bootstrap-mode + localhost guard shared by the setup folder-picker routes."""
    config = request.app.state.config
    if not getattr(config, "bootstrap_mode", False):
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _localhost_request(request) or not _setup_finish_origin_allowed(request):
        return JSONResponse(
            {
                "error": "setup filesystem access is localhost-only — open the "
                f"wizard at http://localhost:{config.pwa_port}"
            },
            status_code=403,
        )
    return None


def _setup_dir_listing(target: Path) -> dict:
    """Return the folder-picker listing payload for a resolved directory."""
    home = Path.home().resolve()
    dirs: list[dict[str, str]] = []
    for entry in target.iterdir():
        if entry.name.startswith("."):
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        dirs.append({"name": entry.name, "path": str(entry)})
    dirs.sort(key=lambda row: row["name"].lower())
    display = str(target)
    if target == home:
        display = "~"
    elif str(target).startswith(str(home) + os.sep):
        display = "~" + str(target)[len(str(home)):]
    parent = target.parent
    return {
        "path": str(target),
        "display_path": display,
        "parent": str(parent) if parent != target else None,
        "dirs": dirs,
        "home": str(home),
    }


def _resolve_setup_dir(raw: str) -> Path | None:
    """Expand and resolve a picker path; None when it is not an existing directory."""
    try:
        target = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not target.is_dir():
        return None
    return target


async def setup_list_dirs_endpoint(request: Request) -> JSONResponse:
    """List local subdirectories for the first-run setup folder picker."""
    guard = _setup_fs_guard(request)
    if guard is not None:
        return guard
    raw = str(request.query_params.get("path") or "~").strip() or "~"
    target = _resolve_setup_dir(raw)
    if target is None:
        return JSONResponse({"error": f"not a directory: {raw}"}, status_code=400)
    try:
        return JSONResponse(_setup_dir_listing(target))
    except PermissionError:
        return JSONResponse({"error": f"permission denied: {target}"}, status_code=400)
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def setup_inspect_folder_endpoint(request: Request) -> JSONResponse:
    """Probe a candidate workspace folder for the first-run setup wizard.

    Returns the inferred vault mode ("scratch" vs "existing"), the resolved
    vault root, and any nested workspace directories the folder already
    contains (e.g. legacy ``memory-vault/personal/`` and
    ``memory-vault/work/``). The wizard uses this to show existing workspace
    chips when they are present; otherwise it asks for the logical workspace
    name that will be assigned to the selected folder.
    """
    from ciao.cli import detect_vault_mode
    from ciao.setup_status import detect_nested_workspaces

    guard = _setup_fs_guard(request)
    if guard is not None:
        return guard
    raw = str(request.query_params.get("path") or "").strip()
    if not raw:
        return JSONResponse({"error": "path is required"}, status_code=400)
    target = _resolve_setup_dir(raw)
    if target is None:
        return JSONResponse({"error": f"not a directory: {raw}"}, status_code=400)
    # Reuse the same "scratch vs existing" rule the setup/finish endpoint
    # applies, so the wizard and the server agree before the user clicks
    # Finish. The vault root mirrors setup_workspace's logic: an existing
    # notes folder (no prior scaffold) is the vault itself; otherwise the
    # vault lives under memory-vault/.
    mode = detect_vault_mode(target)
    existing_env_path = target / ".env"
    vault_root = target / "memory-vault"
    if mode == "existing" and not vault_root.is_dir():
        vault_root = target
    nested = detect_nested_workspaces(vault_root) if mode == "existing" else []
    return JSONResponse({
        "path": str(target),
        "mode": mode,
        "vault_root": str(vault_root),
        "existing_workspaces": nested,
        "has_env": existing_env_path.is_file(),
    })


async def setup_mkdir_endpoint(request: Request) -> JSONResponse:
    """Create a folder from the first-run setup folder picker and return the refreshed listing."""
    guard = _setup_fs_guard(request)
    if guard is not None:
        return guard
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "json object is required"}, status_code=400)
    name = str(body.get("name", "")).strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if "/" in name or "\\" in name or os.sep in name or name.startswith("."):
        return JSONResponse({"error": "folder name must not contain path separators or start with a dot"}, status_code=400)
    parent = _resolve_setup_dir(str(body.get("path", "")).strip())
    if parent is None:
        return JSONResponse({"error": "path must be an existing directory"}, status_code=400)
    try:
        (parent / name).mkdir()
    except FileExistsError:
        return JSONResponse({"error": f"already exists: {name}"}, status_code=400)
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    try:
        return JSONResponse(_setup_dir_listing(parent))
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


# ── Admin ────────────────────────────────────────────────────────────────

async def admin_snapshot(request: Request) -> JSONResponse:
    """Trigger a git snapshot (add, commit, push)."""
    mgr = getattr(request.app.state, "local_session_manager", None)
    confirm_warnings = False
    try:
        body = await request.json()
        confirm_warnings = bool(body.get("confirm_warnings", False))
    except ValueError:
        pass

    if mgr is not None:
        preflight = await mgr.preflight()
        if preflight["blockers"]:
            return JSONResponse(
                {"ok": False, "error": "Blocked by secrets check", "blockers": preflight["blockers"]},
                status_code=400
            )
        if preflight["warnings"] and not confirm_warnings:
            return JSONResponse(
                {"ok": False, "error": "Warnings exist, require confirmation", "warnings": preflight["warnings"]},
                status_code=400
            )

    config = request.app.state.config
    ws = config.workspace_root

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "add", "-A"],
            cwd=str(ws), capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return JSONResponse({"error": f"git add failed: {result.stderr}"}, status_code=500)

        status = await asyncio.to_thread(
            subprocess.run,
            ["git", "status", "--porcelain"],
            cwd=str(ws), capture_output=True, text=True, timeout=10,
        )
        if not status.stdout.strip():
            return JSONResponse({"ok": True, "message": "Nothing to commit"})

        from datetime import UTC, datetime
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
        await asyncio.to_thread(
            subprocess.run,
            ["git", "commit", "-m", f"pwa snapshot {ts}"],
            cwd=str(ws), capture_output=True, text=True, timeout=30,
        )

        push = await asyncio.to_thread(
            subprocess.run,
            ["git", "push"],
            cwd=str(ws), capture_output=True, text=True, timeout=60,
        )

        return JSONResponse({
            "ok": True,
            "message": f"Snapshot committed and {'pushed' if push.returncode == 0 else 'push failed'}",
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)





# Enough of a failing step's tail to carry a traceback or a pip resolver
# error. The old 500 was not enough for either.
_DEPLOY_STEP_OUTPUT_CHARS = 4000


def _record_step(step: str, result: subprocess.CompletedProcess) -> dict:
    """Capture a step's output, keeping the part that says what went wrong.

    Two things this must not do, both of which hid real failures:

    * Truncate from the *head*. Build tools put progress chatter first and
      the diagnosis last, so `[:500]` on a failed `pip install` showed
      "Preparing editable metadata..." and cut off before the error.
    * Take ``stdout or stderr``. pip writes progress to stdout and errors
      to stderr, so stdout is never empty and stderr was always discarded.

    The full output is logged regardless: the response is the only other
    copy, and a truncated card was previously the sole record of a failure.
    """
    parts = [p for p in (result.stdout.strip(), result.stderr.strip()) if p]
    combined = "\n".join(parts)
    out = combined[-_DEPLOY_STEP_OUTPUT_CHARS:]
    if len(combined) > _DEPLOY_STEP_OUTPUT_CHARS:
        out = f"[earlier output trimmed]\n{out}"
    ok = result.returncode == 0
    if not ok:
        logger.error(
            "deploy step %r failed (exit %s):\n%s", step, result.returncode, combined
        )
    return {"step": step, "ok": ok, "output": out}


def _pip_install_hint(output: str) -> str:
    """Turn a development deploy's "cannot uninstall" wall into guidance.

    A packaged app cannot be replaced by the developer-only deploy action. The
    raw installer output otherwise sends users to debug the wrong thing.
    """
    lowered = output.lower()
    if "cannot uninstall" in lowered or "no record file" in lowered:
        return (
            "The running engine belongs to a packaged Ciaobot.app and cannot be "
            "replaced by the development deploy action. Use the signed in-app "
            "updater or re-run the one-line installer for production updates."
        )
    return ""


def _resolve_codebase_root(config) -> Path:
    """Where the deploy steps run git, pip, and npm.

    ``CIAO_APP_REPO`` wins over the module path for developer-mode deploys. A
    packaged app does not resolve a checkout for production updates.
    """
    configured = getattr(config, "app_repo", None)
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2]


def _checkout_problem(codebase_root: Path) -> str:
    """Why ``codebase_root`` cannot be deployed from, or an empty string.

    Checked up front because the underlying failures are misleading: git says
    "not a git repository" and npm says ENOENT, neither of which points at an
    engine that was installed rather than checked out.
    """
    if not (codebase_root / ".git").exists():
        return f"{codebase_root} is not a git checkout"
    if not (codebase_root / "web" / "package.json").exists():
        return f"{codebase_root} has no web/package.json"
    return ""


def _run_root_npm_install(codebase_root: Path) -> subprocess.CompletedProcess:
    args = ["npm", "install", "--no-audit", "--no-fund"]
    if not (codebase_root / "package.json").exists():
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="skipped: no root package.json",
            stderr="",
        )
    return desktop_build.run_step(args, cwd=str(codebase_root), timeout=180)


def _restart_only(config, *, dev_mode: bool) -> bool:
    """Whether Settings' Restart must only restart, never redeploy from source.

    Redeploy (``admin_deploy``) pulls, pip-installs, and rebuilds a source
    checkout, so it only makes sense for a developer running from one. A
    packaged Ciaobot.app never qualifies: its embedded runtime is not a
    checkout, and ``pip install -e`` cannot replace it even when
    ``CIAO_APP_REPO`` names one. Linux hosts outside dev mode are
    administrator-managed and restart only. Everywhere else, including Linux
    dev mode, anything that is not a deployable checkout (a plain package
    install) restarts only too, since deploy would stop at "locate checkout".
    """
    from ciao.package_version import detect_install_mode

    if detect_install_mode() in ("bundled_app", "installer"):
        return True
    if sys.platform.startswith("linux") and not dev_mode:
        return True
    if config is None:
        return False
    return bool(_checkout_problem(_resolve_codebase_root(config)))


_BUNDLED_DEPLOY_REFUSAL = (
    "This engine runs from the installed Ciaobot.app, not a source checkout, "
    "so there is nothing to pull or rebuild. Use Restart to restart it; updates "
    "come from the in-app updater or the one-line installer."
)

_INSTALLER_DEPLOY_REFUSAL = (
    "This engine was installed by the Ciaobot engine installer, not run from a "
    "source checkout, so it cannot be redeployed from source. Re-run the "
    "installer to update it."
)


async def admin_restart(request: Request) -> JSONResponse:
    """Restart the installed engine after draining work, without updating code.

    ``request_restart`` drains chats, shuts uvicorn down, and returns the
    restart exit code from ``ciao.main``; ``ciao.cli._run_server`` then
    re-execs a fresh interpreter in the same process. That works under every
    supervisor: the bundled app's ``com.ciao.server`` LaunchAgent keeps
    tracking the same pid (and its ``KeepAlive`` relaunches the job if the
    exec ever fails), and a foreground ``ciao run`` comes back on its own.
    """
    from starlette.background import BackgroundTask

    restart = getattr(request.app.state, "request_restart", None)
    if not callable(restart):
        return JSONResponse({"ok": False, "error": "restart unavailable"}, status_code=503)

    async def after_response() -> None:
        restart(RESTART_EXIT_CODE)

    return JSONResponse(
        {"ok": True, "steps": [{"step": "restart", "ok": True, "output": "Waiting for active chat work to drain"}]},
        background=BackgroundTask(after_response),
    )


async def admin_deploy(request: Request) -> JSONResponse:
    """Snapshot local work, pull latest, rebuild frontend, restart service."""
    from ciao.package_version import detect_install_mode

    # Refuse before the secrets preflight and the snapshot: a packaged app can
    # never be redeployed from source, so none of the steps below may run.
    mode = detect_install_mode()
    if mode in ("bundled_app", "installer"):
        return JSONResponse(
            {"steps": [], "ok": False, "error": _BUNDLED_DEPLOY_REFUSAL if mode == "bundled_app" else _INSTALLER_DEPLOY_REFUSAL},
            status_code=400,
        )

    mgr = getattr(request.app.state, "local_session_manager", None)
    confirm_warnings = False
    try:
        body = await request.json()
        confirm_warnings = bool(body.get("confirm_warnings", False))
    except ValueError:
        pass

    if mgr is not None:
        preflight = await mgr.preflight()
        if preflight["blockers"]:
            return JSONResponse(
                {"steps": [], "ok": False, "error": "Blocked by secrets check", "blockers": preflight["blockers"]},
                status_code=400
            )
        if preflight["warnings"] and not confirm_warnings:
            return JSONResponse(
                {"steps": [], "ok": False, "error": "Warnings exist, require confirmation", "warnings": preflight["warnings"]},
                status_code=400
            )

    config = request.app.state.config
    ws = config.workspace_root
    codebase_root = _resolve_codebase_root(config)
    steps = []

    problem = _checkout_problem(codebase_root)
    if problem:
        hint = (
            f"{problem}. Set CIAO_APP_REPO to the ciaobot checkout so Restart can "
            "pull, reinstall, and rebuild from source."
        )
        steps.append({"step": "locate checkout", "ok": False, "output": hint})
        return JSONResponse({"steps": steps, "ok": False, "error": hint}, status_code=400)
    steps.append({"step": "locate checkout", "ok": True, "output": str(codebase_root)})

    # 0. Snapshot: stage, commit (if dirty), rebase, push.
    #    Captures in-flight writes so the pull that follows can't clobber them
    #    and so the peer instance can see this side's work.
    from datetime import UTC, datetime
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    ok, detail = await _commit_and_push(ws, f"pwa snapshot before deploy {ts}")
    steps.append({"step": "snapshot", "ok": ok, "output": detail[:500]})
    if not ok:
        return JSONResponse(
            {"steps": steps, "ok": False, "error": f"snapshot failed: {detail}"},
            status_code=500,
        )

    # 1. Git pull (idempotent after snapshot, but catches any race push).
    #    Uses the same retry helper as the snapshot step so a short DNS
    #    resolver flap doesn't fail the whole deploy with a confusing
    #    "Could not resolve host" error.
    rc, pull_out = await _git_pull_with_retry(codebase_root)
    if rc != 0:
        out = (pull_out or "").strip()[:500]
        steps.append({"step": "git pull", "ok": False, "output": out})
        return JSONResponse(
            {"steps": steps, "ok": False, "error": f"git pull failed: {out}"},
            status_code=500,
        )
    steps.append({"step": "git pull", "ok": True, "output": (pull_out or "").strip()[:500]})

    # 2. pip install
    import sys
    result = await asyncio.to_thread(
        desktop_build.run_step, [sys.executable, "-m", "pip", "install", "-e", "."],
        cwd=str(codebase_root), timeout=120,
    )
    steps.append(_record_step("pip install", result))
    if result.returncode != 0:
        # str() because `steps` is inferred as list[dict[str, object]] from its
        # first append; _record_step always puts a string here.
        hint = _pip_install_hint(str(steps[-1]["output"]))
        if hint:
            steps[-1]["output"] = f"{hint}\n\n{steps[-1]['output']}"
        # The step card renders the full output; the top-level error is the
        # one-line headline above it, not a second copy of the same text.
        return JSONResponse(
            {"steps": steps, "ok": False, "error": hint or "pip install failed."},
            status_code=500,
        )

    # 2b. npm install at repo root, only when a root package exists. The PWA's
    # package.json lives under web/, so running npm at the repo root on this
    # project would otherwise emit ENOENT on every deploy.
    result = await asyncio.to_thread(
        _run_root_npm_install, codebase_root,
    )
    steps.append(_record_step("npm install (root)", result))

    # 3. npm build
    web_dir = codebase_root / "web"
    result = await asyncio.to_thread(
        desktop_build.run_step, ["npm", "run", "build"],
        cwd=str(web_dir), timeout=120,
    )
    steps.append(_record_step("npm build", result))
    if result.returncode != 0:
        return JSONResponse(
            {"steps": steps, "ok": False, "error": f"npm build failed: {steps[-1]['output']}"},
            status_code=500,
        )

    # 3b. Desktop shell. Changes under desktop/ only reach the window through a
    # rebuilt bundle, so dev instances rebuild it here and swap it in during the
    # restart below. Released installs skip this: no checkout, no cargo. The
    # rebuild is minutes long, hence the staleness check rather than doing it on
    # every restart.

    relaunch_desktop = False
    # The desktop shell is a macOS Tauri bundle: attempting its rebuild on
    # Linux fails after git/pip/npm have already mutated the install, and the
    # resulting 500 aborts before the restart. Linux dev deploys skip it.
    if getattr(config, "dev_mode", False) and sys.platform == "darwin":
        needed, reason = await asyncio.to_thread(desktop_build.needs_rebuild, codebase_root)
        if not needed:
            steps.append({"step": "desktop app", "ok": True, "output": f"skipped: {reason}"})
        else:
            steps.append({"step": "desktop app", "ok": True, "output": f"rebuilding: {reason}"})
            desktop_steps, relaunch_desktop = await asyncio.to_thread(
                desktop_build.build_and_stage, codebase_root, runner=desktop_build.run_step,
            )
            steps.extend(desktop_steps)
            failed = next((s for s in desktop_steps if not s["ok"]), None)
            if failed is not None:
                return JSONResponse(
                    {"steps": steps, "ok": False, "error": f"{failed['step']} failed: {failed['output']}"},
                    status_code=500,
                )
    elif getattr(config, "dev_mode", False):
        steps.append({"step": "desktop app", "ok": True, "output": "skipped: the desktop shell builds on macOS only"})

    # 4. Signal restart. Must go through app.state.request_restart (which sets
    # the restart flag and calls server.shutdown()). Raising RestartRequested
    # inside this detached task does NOT work:
    # the exception never reaches the `except RestartRequested` wrapping
    # server.serve() in ciao.main, so it gets swallowed as an unhandled task
    # exception and the process keeps running with stale code. Deploy then looks
    # successful (frontend rebuilt) but backend changes never load.
    from ciao.signals import RestartRequested

    async def _do_restart():
        await asyncio.sleep(2)
        # The desktop swap runs before the engine restart, not after: the
        # relaunched app comes up against a live engine and then rides the
        # normal restart-drain path, instead of racing launchd for the runtime
        # directory while the engine is down.
        if relaunch_desktop:
            try:
                installed = await asyncio.to_thread(
                    desktop_build.install_staged_and_relaunch, runner=desktop_build.run_step,
                )
                for step in installed:
                    if step["ok"]:
                        logger.info("deploy: %s: %s", step["step"], step["output"])
                    else:
                        logger.error("deploy: %s: %s", step["step"], step["output"])
            except Exception:
                # A failed relaunch must not strand the engine on stale code;
                # the operator can reopen the app by hand.
                logger.exception("deploy: desktop install and relaunch failed")
        fn = getattr(request.app.state, "request_restart", None)
        if callable(fn):
            fn(RESTART_EXIT_CODE)
        else:
            raise RestartRequested(RESTART_EXIT_CODE)

    asyncio.create_task(_do_restart())
    steps.append({
        "step": "restart",
        "ok": True,
        "output": "swapping in the rebuilt desktop app first" if relaunch_desktop else "",
    })

    return JSONResponse({"steps": steps, "ok": True})


async def admin_skills(request: Request) -> JSONResponse:
    """List skills known to Ciaobot, labelled as custom or GitHub/package.

    Merged across every agent root. Reading `workspace_root` alone showed
    `{custom: 0, github: 0, stock: 29}` on a migrated install — measured — while
    19 custom and 7 upstream skills sat in the primary root's catalog. The page
    looked empty.

    A skill of the same name in two roots is reported once, with the workspaces
    that hold it, because the page is a catalog rather than a per-root listing
    and two rows for one name reads as a duplicate rather than as sharing.
    """
    config = request.app.state.config
    targets = getattr(config, "agent_root_targets", None)
    roots = list(targets()) if callable(targets) else [(config.workspace_root, "")]

    merged: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for root, name in roots:
        inventory = build_skill_inventory(root)
        for skill in inventory.get("skills", []):
            key = str(skill.get("name") or "")
            existing = merged.get(key)
            if existing is None:
                skill["workspaces"] = [name] if name else []
                merged[key] = skill
                counts[str(skill.get("label") or "")] = (
                    counts.get(str(skill.get("label") or ""), 0) + 1
                )
            elif name and name not in existing.get("workspaces", []):
                existing.setdefault("workspaces", []).append(name)
    return JSONResponse(
        {"counts": counts, "skills": [merged[k] for k in sorted(merged)]}
    )


async def admin_add_skill(request: Request) -> JSONResponse:
    """Deprecated: GitHub skill install removed.

    Kept as a 410 for old clients that still POST to this route.
    """
    return JSONResponse(
        {"ok": False, "error": "GitHub skill install removed. Add a local folder under skills/<name>/ or upload a zip via POST /api/skills/import."},
        status_code=410,
    )


def _with_warnings(message: str, warnings: list[str]) -> str:
    """Append non-blocking import notes to a success message."""
    return " ".join([message, *warnings]) if warnings else message


async def skill_import(request: Request) -> JSONResponse:
    """Import a skill from a validated zip archive.

    Accepts multipart/form-data with a ``file`` field containing the zip.
    Validates zip-slip, exactly one top-level folder with SKILL.md,
    frontmatter name/description, and name equals folder. On success extracts
    to ``skills/<name>/`` and syncs the catalog. A SKILL.md over the 15 KB
    context budget imports with a warning on ``message``/``warnings`` rather
    than being rejected.
    """
    config = request.app.state.config
    # Reject oversized bodies before multipart parsing. `request.form()` fully
    # consumes and spools the multipart file, so a very large upload would
    # exhaust temporary disk (and, in client mode, the proxy buffers the body in
    # memory) before the per-file cap below is ever applied. A missing or
    # malformed Content-Length (chunked/HTTP2 clients) is rejected too: without
    # it there is no cheap pre-parse bound, and a legitimate zip upload always
    # carries the header.
    max_zip_bytes = 10 * 1024 * 1024
    content_length = request.headers.get("content-length")
    try:
        declared = int(content_length) if content_length else -1
    except ValueError:
        declared = -1
    if declared < 0 or declared > max_zip_bytes:
        return JSONResponse(
            {"ok": False, "error": "Zip too large (max 10 MB)."}, status_code=400
        )
    # Parse multipart
    try:
        form = await request.form()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Invalid form: {exc}"}, status_code=400)
    # Resolve destination skills root: per-root installs keep skills per
    # workspace. The client passes the active workspace so an upload lands in
    # the workspace the operator is actually working in, not always the primary.
    workspace_name = str(form.get("workspace") or "").strip()
    if workspace_name and workspace_name not in config.workspace_names():
        # `config.agent_root` accepts any single-segment name, so an unvalidated
        # form value would scaffold `<install>/<name>/skills` and a whole
        # orphan agent root on the next sync — reachable from a stale client
        # after a workspace was deleted, or a typo in a direct API request.
        return JSONResponse(
            {"ok": False, "error": f"Unknown workspace: {workspace_name}"}, status_code=400
        )
    dest_skills: Path
    try:
        if workspace_name:
            dest_skills = Path(config.agent_root(workspace_name)) / "skills"  # type: ignore[attr-defined]
        elif getattr(config, "_rerooted", lambda: False)() if callable(getattr(config, "_rerooted", None)) else bool(getattr(config, "_rerooted", False)):
            # Use primary workspace's agent root when re-rooted
            try:
                primary = config.primary_workspace() if callable(getattr(config, "primary_workspace", None)) else ""
                if primary:
                    dest_skills = Path(config.agent_root(primary)) / "skills"  # type: ignore[attr-defined]
                else:
                    dest_skills = Path(config.workspace_root) / "skills"
            except Exception:
                dest_skills = Path(config.workspace_root) / "skills"
        else:
            # Shared layout: workspace_root holds skills/
            # Also check if agent_root_targets exists and is per-root
            targets = getattr(config, "agent_root_targets", None)
            if callable(targets):
                try:
                    roots = list(targets())  # type: ignore[no-untyped-call]
                    if len(roots) == 1:
                        # Single root (shared) – that root is the skills holder
                        dest_skills = Path(roots[0][0]) / "skills"
                    elif roots:
                        # Multiple roots (re-rooted) – use primary
                        primary = config.primary_workspace() if callable(getattr(config, "primary_workspace", None)) else roots[0][1]
                        try:
                            dest_skills = Path(config.agent_root(primary)) / "skills"  # type: ignore[attr-defined]
                        except Exception:
                            dest_skills = Path(roots[0][0]) / "skills"
                    else:
                        dest_skills = Path(config.workspace_root) / "skills"
                except Exception:
                    dest_skills = Path(config.workspace_root) / "skills"
            else:
                dest_skills = Path(config.workspace_root) / "skills"
    except Exception:
        dest_skills = Path(getattr(config, "workspace_root", ".")) / "skills"

    upload = form.get("file")
    # Fallback: allow any file field
    if upload is None:
        for key in form:
            val = form[key]
            if hasattr(val, "read"):
                upload = val
                break
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"ok": False, "error": "Missing file field 'file' (multipart zip upload required)."}, status_code=400)
    # Optional force flag
    force_raw = form.get("force")
    force = str(force_raw).strip().lower() in {"1", "true", "yes"} if force_raw is not None else False
    # Also check query param
    if not force and request.query_params.get("force", "").lower() in {"1", "true", "yes"}:
        force = True

    # Read upload with size limit (zip + slack). The 15 KB SKILL.md budget is
    # a warning, not a gate — see ciao/skill_import.validate_skill_zip.
    try:
        data = await _read_upload_limited(upload, max_zip_bytes)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Zip too large (max 10 MB)."}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Failed to read upload: {exc}"}, status_code=400)

    # Basic content-type check (accept zip or octet-stream)
    filename = getattr(upload, "filename", "") or ""
    if filename and not filename.lower().endswith(".zip"):
        # Still allow; some clients may send octet-stream without .zip, but warn if clearly not zip
        if filename.lower().endswith((".tar.gz", ".tgz", ".tar")):
            return JSONResponse({"ok": False, "error": "Only .zip is supported (zip only for v1)."}, status_code=400)

    from ciao.skill_import import extract_skill_zip

    dest_skills.mkdir(parents=True, exist_ok=True)
    # Non-blocking notes (currently: SKILL.md over the context budget). The
    # import still happens; the note rides along on the success message so the
    # owner learns what it costs without being stopped at the door.
    warnings: list[str] = []
    name, errors = await asyncio.to_thread(
        functools.partial(
            extract_skill_zip, data, dest_skills, overwrite=force, warnings=warnings
        )
    )
    if errors:
        return JSONResponse({"ok": False, "error": "; ".join(errors)}, status_code=400)
    assert name is not None
    # Sync catalog
    try:
        from ciao.sync_skills import sync_workspace_skills

        # Sync the root that owns the skills folder
        # If dest_skills is under an agent root, sync that root; otherwise sync workspace_root
        sync_root = dest_skills.parent
        await asyncio.to_thread(sync_workspace_skills, sync_root)
    except Exception as exc:  # noqa: BLE001 — sync failure should not hide successful import
        logger.warning("skill import sync failed for %s: %s", dest_skills, exc)
        return JSONResponse(
            {
                "ok": True,
                "name": name,
                "warnings": warnings,
                "message": _with_warnings(
                    f"Skill '{name}' imported (sync warning: {exc}).", warnings
                ),
            },
            status_code=200,
        )
    return JSONResponse({
        "ok": True,
        "name": name,
        "warnings": warnings,
        "message": _with_warnings(
            f"Skill '{name}' added — available to all operators after next sync.",
            warnings,
        ),
    })



async def admin_status(request: Request) -> JSONResponse:
    """Extended status for the settings page."""
    config = request.app.state.config
    state = request.app.state.state_store

    branch = ""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(config.workspace_root),
            capture_output=True, text=True, timeout=5,
        )
        branch = result.stdout.strip()
    except Exception:
        pass

    return JSONResponse({
        "cost": state.bot_state.cost,
        "branch": branch,
        "models": list(CLAUDE_MODELS),
        "default_model": config.claude_default_model,
        "default_mode": config.claude_mode,
    })


# ── Local session flow (current-branch sync + conflict-resolution chat) ──


def _local_manager(request: Request):
    return getattr(request.app.state, "local_session_manager", None)


def _open_merge_chat(request: Request, branch: str) -> dict:
    """Open an interactive chat that resolves sync conflicts on ``branch``
    with the user. Returns {ok, chat_id, project_id} or {error}."""
    config = request.app.state.config
    pcm = request.app.state.project_chat_manager
    # Any workspace can host this; prefer the primary one, then settle for the
    # first workspace that has a General project. Keying on a workspace named
    # "personal" meant the whole sync-conflict flow failed on installs whose
    # workspaces are named anything else.
    workspace = config.primary_workspace()
    project = next(
        (p for p in pcm.list_projects(workspace) if p.name == "General"), None
    )
    if project is None:
        for candidate in config.workspace_names():
            project = next(
                (p for p in pcm.list_projects(candidate) if p.name == "General"), None
            )
            if project is not None:
                break
    if project is None:
        return {"error": "no General project in any workspace to host the merge chat"}

    from datetime import UTC, datetime
    from ciao.local_session import MERGE_PROMPT

    title = f"Resolve sync conflicts: {branch} {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')}"
    prompt = MERGE_PROMPT.replace("{branch}", str(branch))

    chat = pcm.create_chat(
        project.project_id, title=title, model=config.claude_default_model
    )
    pcm.start_stream(chat.chat_id, prompt)
    return {"ok": True, "chat_id": chat.chat_id, "project_id": project.project_id}


async def local_preflight(request: Request) -> JSONResponse:
    """Git preflight check for dirty changes, file categories, and secrets."""
    mgr = _local_manager(request)
    if mgr is None:
        return JSONResponse(
            {"error": "local session manager not initialised"}, status_code=500
        )
    return JSONResponse(await mgr.preflight())


async def local_status(request: Request) -> JSONResponse:
    """Current workspace git state: git_repo, branch (may be null), dirty."""
    mgr = _local_manager(request)
    if mgr is None:
        return JSONResponse(
            {"error": "local session manager not initialised"}, status_code=500
        )
    status = dict(mgr.status())
    status["restart_only"] = _restart_only(
        getattr(request.app.state, "config", None),
        dev_mode=bool(status.get("dev_mode", False)),
    )
    return JSONResponse(status)


async def local_handback(request: Request) -> JSONResponse:
    """Commit the session and sync the current branch with origin.

    Clean pull -> pushed directly. Conflict -> an interactive resolution chat
    is opened in Ciaobot. Never creates or switches branches.
    """
    mgr = _local_manager(request)
    if mgr is None:
        return JSONResponse(
            {"error": "local session manager not initialised"}, status_code=500
        )
    branch = mgr.branch
    if branch is None:
        return JSONResponse(
            {"ok": False, "error": "workspace is not a git repository (or is on a detached HEAD)"},
            status_code=400,
        )

    confirm_warnings = False
    try:
        body = await request.json()
        confirm_warnings = bool(body.get("confirm_warnings", False))
    except ValueError:
        pass

    preflight = await mgr.preflight()
    if preflight["blockers"]:
        return JSONResponse(
            {"ok": False, "error": "Blocked by secrets check", "blockers": preflight["blockers"]},
            status_code=400
        )
    if preflight["warnings"] and not confirm_warnings:
        return JSONResponse(
            {"ok": False, "error": "Warnings exist, require confirmation", "warnings": preflight["warnings"]},
            status_code=400
        )

    result = await mgr.commit_and_sync()
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    if result.get("merged"):
        return JSONResponse(result)
    # Conflict: hand off to an interactive resolution chat.
    merge = _open_merge_chat(request, result.get("branch") or branch)
    return JSONResponse({**result, "merge": merge})


async def local_resync(request: Request) -> JSONResponse:
    """After the conflict chat pushed the branch, merge origin/<branch> in."""
    mgr = _local_manager(request)
    if mgr is None:
        return JSONResponse(
            {"error": "local session manager not initialised"}, status_code=500
        )
    result = await mgr.resync()
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


async def handover_merge(request: Request) -> JSONResponse:
    """Open an interactive chat that resolves sync conflicts on a branch. Also
    used by ``local_handback`` when the automatic pull conflicts."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    branch = (body.get("branch") if isinstance(body, dict) else None) or ""
    if not branch:
        mgr = _local_manager(request)
        branch = (mgr.branch if mgr else None) or ""
    if not branch:
        return JSONResponse(
            {"error": "workspace is not a git repository (or is on a detached HEAD)"},
            status_code=400,
        )
    merge = _open_merge_chat(request, branch)
    return JSONResponse(merge, status_code=200 if merge.get("ok") else 500)



async def debug_issues(request: Request) -> JSONResponse:
    """Runtime issue report (server errors + failed job runs) for self-fix.

    Only available when ``CIAO_DEV_MODE`` is set; hidden (404) otherwise so
    the endpoint does not advertise itself on production instances.
    """
    config = request.app.state.config
    if not getattr(config, "dev_mode", False):
        return JSONResponse(
            {"error": "debug endpoints require CIAO_DEV_MODE"}, status_code=404
        )
    from ciao.debug_report import DEFAULT_LOG_LINES, build_issue_report

    try:
        lines = int(request.query_params.get("lines", DEFAULT_LOG_LINES))
    except ValueError:
        lines = DEFAULT_LOG_LINES
    lines = max(1, min(lines, 2000))
    report = await asyncio.to_thread(
        build_issue_report, config.workspace_root, log_lines=lines
    )
    return JSONResponse(report)


async def cli_stats(request: Request) -> JSONResponse:
    """Return Claude Code CLI stats from ~/.claude/stats-cache.json."""
    if not _STATS_CACHE_PATH.exists():
        return JSONResponse({"error": "stats-cache.json not found"}, status_code=404)
    try:
        data = json.loads(_STATS_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return JSONResponse({"error": "failed to read stats"}, status_code=500)
    return JSONResponse(data)


# ── Proposal queue ──────────────────────────────────────────────────────


async def list_proposals(request: Request) -> JSONResponse:
    """Return every queued proposal across workspaces, plus skill proposals.

    Rows are keyed by a stable content-derived id so a UI can act on one without
    a later dismiss renumbering it (see ``proposal_service._stable_proposal_id``). Rehome rows
    carry candidate destinations and a ``justified`` flag, so the UI never
    pre-fills an accept for a destination no tag backs. Skill-proposal files are
    surfaced under the same ``rows`` list with ``kind: "skill"``.
    """
    config = request.app.state.config
    rows, _by_id = proposal_service._scan_proposal_rows(config)
    return JSONResponse({"rows": rows})


_HISTORY_DEFAULT_LIMIT = 200
_HISTORY_MAX_LIMIT = 1000

# Receipts are read newest-first and only the newest slice can plausibly match
# a page of decisions, so the join never walks a whole long-lived journal.
_HISTORY_RECEIPT_WINDOW = 1000


def _history_norm(text: str) -> str:
    """Comparison form for joining a decision to the receipt that performed it."""
    return " ".join(str(text or "").split()).casefold()


def _receipt_for_row(
    row: dict[str, Any], candidates: list[dict[str, Any]], claimed: set[str]
) -> dict[str, Any] | None:
    """The receipt that recorded one decision, or None.

    Text-matching fallback, used only for ledger rows written before the
    decision carried its receipt's id. A row that names one is resolved by id
    and never reaches here.

    The two sides are written by different functions and do not share an id:
    the sidecar records the bullet's text, the receipt records the *promotable*
    text, which for an event-shaped bullet is only its trailing durable-rule
    clause. So an exact match is tried first and a contained one second.

    Each receipt is claimed by at most one decision. Accepting the same fact
    twice (accept, undo, accept) writes two of each, and without the claim both
    decisions pointed at the newest receipt — offering an undo on a row whose
    change had already been reversed.
    """
    target = _history_norm(row.get("text", ""))
    if not target:
        return None
    ts = str(row.get("ts", ""))

    def _pick(pool: list[dict[str, Any]]) -> dict[str, Any] | None:
        free = [r for r in pool if str(r.get("id", "")) not in claimed]
        if not free:
            return None
        # Nearest in time, on the ISO strings both sides write. A receipt is
        # recorded just before the decision it belongs to, so the closest one
        # is the right one even when the same fact was decided twice.
        return min(free, key=lambda r: abs_ts_gap(str(r.get("ts", "")), ts))

    exact = [r for r in candidates if _history_norm(r.get("fact_text", "")) == target]
    chosen = _pick(exact)
    if chosen is not None:
        return chosen
    partial = [
        r
        for r in candidates
        if (fact := _history_norm(r.get("fact_text", "")))
        and fact != target
        and fact in target
    ]
    return _pick(partial)


def abs_ts_gap(left: str, right: str) -> float:
    """Seconds between two ISO timestamps; a huge gap when either is unparseable."""
    try:
        return abs(
            (datetime.fromisoformat(left) - datetime.fromisoformat(right)).total_seconds()
        )
    except (TypeError, ValueError):
        return float("inf")


def _change_payload(receipt: dict[str, Any]) -> dict[str, Any]:
    """The `change` pointer one decision row carries, from its receipt."""
    from ciao.memory_receipts import is_undoable

    return {
        "receipt_id": str(receipt.get("id", "")),
        "kind": str(receipt.get("kind", "")),
        "status": str(receipt.get("status", "")),
        "destination": str(receipt.get("destination", ""))
        or str(receipt.get("region", "")),
        "undoable": is_undoable(receipt),
        "changed": bool(receipt.get("changed", True)),
        "ts": str(receipt.get("ts", "")),
    }


def _attach_change_receipts(vault: Path, rows: list[dict[str, Any]]) -> None:
    """Point each decision at the receipt that performed it, where one exists.

    A decision written since the ledger started carrying ``receipt_id`` names
    its receipt outright, and that is the only reliable join: the ledger records
    the ORIGINAL bullet — append-time dedupe compares a re-extracted fact
    against it — while the receipt records what was actually written, so an
    accept of an operator-edited wording shares no text with its own receipt.

    Rows with no receipt keep no ``change`` key at all, which is what the
    History surface renders as "No change snapshot available" — every decision
    recorded before the receipt protocol landed, and every one made outside it.
    Claiming an undo affordance for those would be a lie about what can be
    reversed.
    """
    from ciao.memory_receipts import (
        MemoryReceiptError,
        journal_path,
        read_receipts,
    )

    try:
        receipts = read_receipts(journal_path(vault, None))
    except (MemoryReceiptError, OSError, ValueError):
        return
    receipts.sort(key=lambda r: str(r.get("ts", "")), reverse=True)
    by_id = {str(r.get("id", "")): r for r in receipts if r.get("id")}
    claimed: set[str] = set()
    # Explicit references first, over the WHOLE journal rather than the window
    # the heuristic scans: a named receipt is right however old it is, and
    # claiming it here also keeps the text-matching pass below from handing the
    # same receipt to some other decision that merely reads alike.
    pending: list[dict[str, Any]] = []
    for row in rows:
        rid = str(row.pop("receipt_id", "") or "")
        found = by_id.get(rid) if rid else None
        if found is not None:
            claimed.add(rid)
            row["change"] = _change_payload(found)
        elif rid:
            # The ledger names a receipt this journal does not hold (a vault
            # restored without its journal, a trimmed archive). Recording it
            # was still a decision; it just has no snapshot to show, which is
            # the honest "No change snapshot available" state. Do NOT fall back
            # to text matching here: the id was written precisely because the
            # text cannot identify the write.
            continue
        else:
            pending.append(row)
    rows = pending
    receipts = receipts[:_HISTORY_RECEIPT_WINDOW]
    # Fallback for rows the ledger wrote before it carried a receipt id.
    #
    # A promotion writes the destination AND removes the bullet, so two
    # receipts describe it. Each action is matched against exactly one pool,
    # with no cross-fallback:
    #
    # * An ACCEPT means "what did this do to my memory", which is the
    #   destination write. Falling back to that accept's queue receipt would
    #   describe the bullet's removal instead, and its undo would re-queue a
    #   fact the destination still holds — a duplicate wearing an Undo button.
    #   A legacy accept whose destination receipt cannot be identified by text
    #   stays snapshot-less, which is the honest answer.
    # * A DISMISS touches nothing but the queue, so its queue receipt IS the
    #   change, and undoing it restores the bullet.
    region_rows = [r for r in receipts if str(r.get("kind", "")).startswith("region_")]
    queue_rows = [r for r in receipts if str(r.get("kind", "")) == "queue_resolve"]
    # Newest decision first, so the newest receipt is claimed by the decision it
    # actually belongs to rather than by an older one that merely matched.
    for row in sorted(rows, key=lambda r: str(r.get("ts", "")), reverse=True):
        pool = region_rows if row.get("action") == "accepted" else queue_rows
        found = _receipt_for_row(row, pool, claimed)
        if found is None:
            continue
        claimed.add(str(found.get("id", "")))
        row["change"] = _change_payload(found)


# The archive tree is `<logs_root>/Chats/<chat-id>/<provider>/<stem>.md`, and a
# proposal's `source` is that stem. Resolving it means listing that fixed depth,
# which is why the result is cached: a History page asks for up to 1000 rows and
# would otherwise re-list the tree for each one.
_SOURCE_INDEX: dict[str, tuple[float, dict[str, str]]] = {}
_SOURCE_INDEX_TTL_S = 60.0


def _source_index(config: Any) -> dict[str, str]:
    """Archive stem → absolute transcript path, cached briefly."""
    root = Path(config.logs_root) / "Chats"
    key = str(root)
    cached = _SOURCE_INDEX.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _SOURCE_INDEX_TTL_S:
        return cached[1]
    index: dict[str, str] = {}
    try:
        for chat_dir in root.iterdir():
            if not chat_dir.is_dir():
                continue
            for provider_dir in chat_dir.iterdir():
                if not provider_dir.is_dir():
                    continue
                for transcript in provider_dir.glob("*.md"):
                    index.setdefault(transcript.stem, str(transcript))
    except OSError:
        index = {}
    _SOURCE_INDEX[key] = (now, index)
    return index


async def proposals_history(request: Request) -> JSONResponse:
    """Every recorded proposal decision across workspaces, newest first.

    Reads the same per-workspace decision sidecar :func:`record_dismissal`
    and :func:`record_promotion` write (``Memory-Proposals.dismissed.jsonl``),
    which now carries ``via`` (who decided: the operator through the PWA, the
    curation agent, or the archive-time auto-promoter), a ``destination``, and
    an ``outcome`` qualifier alongside the original ``kind``/``text``. This is
    the read side of the review page's History tab: what was accepted or
    dismissed, by whom, and what the overnight pipeline added or skipped on
    its own.
    """
    from ciao.memory_proposals import history_row_id, read_decisions

    config = request.app.state.config
    workspace_filter = request.query_params.get("workspace", "").strip()
    action_filter = request.query_params.get("action", "").strip()
    if action_filter and action_filter not in {"accepted", "dismissed"}:
        return JSONResponse(
            {"error": "action must be accepted|dismissed"}, status_code=400
        )
    try:
        requested = int(request.query_params.get("limit", _HISTORY_DEFAULT_LIMIT))
    except ValueError:
        return JSONResponse({"error": "limit must be an integer"}, status_code=400)
    requested = max(1, requested)
    limit = min(requested, _HISTORY_MAX_LIMIT)

    rows: list[dict[str, Any]] = []
    # Two registered names can resolve to one vault root (a ``vault_root: "."``
    # entry, or the legacy entity layout), and then they share a sidecar. Read
    # each sidecar once, under the first name that claims it, or every decision
    # in it shows up twice and ``total`` double-counts.
    seen: set[str] = set()
    for workspace in config.workspace_names():
        if workspace_filter and workspace != workspace_filter:
            continue
        queue = proposal_service._proposals_file(config, workspace)
        try:
            key = str(queue.resolve())
        except OSError:
            key = str(queue)
        if key in seen:
            continue
        seen.add(key)
        workspace_rows: list[dict[str, Any]] = []
        for entry in read_decisions(queue):
            if action_filter and entry["action"] != action_filter:
                continue
            row = dict(entry)
            row["workspace"] = workspace
            row["id"] = history_row_id(entry, workspace)
            # Read-side disambiguators for the id only; not part of the contract.
            row.pop("seq", None)
            row.pop("log", None)
            workspace_rows.append(row)
        # Join this workspace's decisions to the receipts that performed them,
        # so History can show what each one actually changed and offer an undo
        # exactly where one is safe. The journal is per vault, so the join has
        # to happen inside the workspace loop rather than over the merged list.
        try:
            vault_for_receipts = Path(config.workspace_vault_root(workspace))
        except (AttributeError, ValueError):
            vault_for_receipts = queue.parent.parent
        await asyncio.to_thread(
            _attach_change_receipts, vault_for_receipts, workspace_rows
        )
        rows.extend(workspace_rows)

    # Newest first; undated legacy rows (empty ts) sort last within that order.
    rows.sort(key=lambda r: r["ts"], reverse=True)
    total = len(rows)
    # ``limit`` is the clamped value actually served, so ``truncated`` stays
    # honest about rows existing beyond the page. ``at_max`` is what tells the
    # client to stop asking: past the cap a wider limit returns the same page,
    # and a "show more" button wired to ``truncated`` alone stayed visible and
    # did nothing forever. It keys off the served limit reaching the cap, not
    # ``requested > limit`` — a request for exactly the cap is already at it,
    # and reporting False there bought one pointless full-page refetch.
    served = rows[:limit]
    # Resolve the archive each served decision came from, so History can link to
    # it instead of printing a bare filename stem. Only the served page is
    # resolved, and only once per request. Off the event loop: it lists a
    # directory tree.
    index = await asyncio.to_thread(_source_index, config)
    for row in served:
        path = index.get(str(row.get("source", "")))
        if path:
            row["source_path"] = path
    return JSONResponse(
        {
            "rows": served,
            "total": total,
            "truncated": total > limit,
            "limit": limit,
            "at_max": limit >= _HISTORY_MAX_LIMIT,
        }
    )


async def proposal_preview(request: Request) -> JSONResponse:
    """Exactly what accepting one queued proposal would write, without writing.

    The queued bullet says what was noticed; the promotion reconciles against
    whatever the destination holds now, so the bullet's own text is not the
    change. This returns the destination, the Add/Update operation and the
    before/after body the accept would produce, plus the ``revision`` that
    body was computed against.

    That revision is the contract with :func:`proposal_action`: hand it back on
    the accept and a destination that moved in between is refused with a
    conflict and a refreshed preview, rather than silently overwritten.

    ``?text=`` previews an edited wording (the review card's "edit suggestion")
    against the same current destination.
    """
    config = request.app.state.config
    pid = request.path_params["id"]
    _rows, by_id = proposal_service._scan_proposal_rows(config)
    ctx = by_id.get(pid)
    if ctx is None:
        return JSONResponse({"error": f"unknown proposal id: {pid}"}, status_code=404)
    text = request.query_params.get("text", "")
    # Off the event loop: the preview reads a guide (taking no lock) and a
    # learnings file, and a slow filesystem must not stall every other request.
    preview = await asyncio.to_thread(proposal_service.preview_row, config, ctx, text)
    return JSONResponse({"ok": True, "preview": preview})


async def memory_receipts(request: Request) -> JSONResponse:
    """List managed memory mutations and whether each can be undone.

    Reads the per-workspace receipt journal written by every managed region
    write, queue resolution and prune. A receipt is ``undoable`` only when it
    is applied, carries a before/after image, and names an operation this
    protocol knows how to reverse; unsupported legacy rows render without an
    Undo affordance.
    """
    from ciao.memory_receipts import list_receipts

    config = request.app.state.config
    workspace_filter = request.query_params.get("workspace", "").strip()
    try:
        requested = int(request.query_params.get("limit", "200"))
    except ValueError:
        return JSONResponse({"error": "limit must be an integer"}, status_code=400)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for workspace in config.workspace_names():
        if workspace_filter and workspace != workspace_filter:
            continue
        try:
            vault = Path(config.workspace_vault_root(workspace))
        except (AttributeError, ValueError):
            continue
        key = str(vault)
        if key in seen:
            continue
        seen.add(key)
        # No per-row workspace filter: this journal belongs to this workspace's
        # vault, and a caller that did not know the workspace name records a
        # blank ``workspace``. Filtering on it would hide those rows.
        for row in list_receipts(vault, limit=max(1, requested)):
            row["workspace"] = workspace
            rows.append(row)
    rows.sort(key=lambda row: str(row.get("ts", "")), reverse=True)
    return JSONResponse({"rows": rows[: max(1, requested)], "total": len(rows)})


def _receipt_units(text: str, separator: str) -> list[str]:
    """One body as the diffable units it is actually made of.

    Two conventions have to be stripped before a diff means anything:

    * The terminating newline a serialized region (or a markdown file) ends
      with is a file convention, not content. Splitting on it yields a trailing
      empty line, which surfaced as a blank added/removed row under every real
      change.
    * A bounded region's entries are joined by a ``\n§\n`` separator. Diffed
      as lines, appending one entry showed the separator as a second added row
      reading "§", and a multi-line entry was torn into unrelated rows. The
      unit of a region is the entry, so that is what gets compared.
    """
    if not text:
        return []
    body = text[:-1] if text.endswith("\n") else text
    return body.split(separator)


def _receipt_separator(kind: str) -> str:
    """What joins one destination's units; ``\n`` for an ordinary file."""
    from ciao.memory_tool import SECTION_SEP

    return f"\n{SECTION_SEP}\n" if kind.startswith("region_") else "\n"


# How much of a receipt's before/after image the detail view ships. The list
# surface strips the images entirely (see `list_receipts`); this is the one
# place they are served, and only for the row the operator opened.
_RECEIPT_IMAGE_MAX = 20_000
# Diff rows past this point are dropped with a flag. A region diff is a handful
# of lines; a queue-file receipt can carry a whole markdown queue.
_RECEIPT_DIFF_MAX = 400


def _receipt_diff(
    before: str, after: str, separator: str = "\n"
) -> tuple[list[dict[str, Any]], bool]:
    """A diff of one receipt's before/after, for the History card.

    Context is dropped: what a History row has to answer is *what changed*, and
    a region body reprinted in full buries the one entry that did.
    """
    import difflib

    old = _receipt_units(before, separator)
    new = _receipt_units(after, separator)
    rows: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for line in old[i1:i2]:
            rows.append({"op": "removed", "text": line})
        for line in new[j1:j2]:
            rows.append({"op": "added", "text": line})
    if len(rows) > _RECEIPT_DIFF_MAX:
        return rows[:_RECEIPT_DIFF_MAX], True
    return rows, False


def _find_receipt_in_workspaces(
    config: Any, rid: str, workspace: str
) -> tuple[dict[str, Any] | None, str]:
    """One receipt by id, searching every workspace journal when none is named."""
    from ciao.memory_receipts import find_receipt, journal_path

    names = [workspace] if workspace else list(config.workspace_names())
    for name in names:
        try:
            vault = Path(config.workspace_vault_root(name))
        except (AttributeError, ValueError):
            continue
        found = find_receipt(journal_path(vault, None), rid)
        if found is not None:
            return found, name
    return None, ""


async def memory_receipt_detail(request: Request) -> JSONResponse:
    """One receipt with its before/after images and a line diff.

    The list endpoint deliberately strips the images, so this is what the
    History row's Changes section reads when it is opened. A receipt with no
    image — a legacy row, a failure, an operation this protocol cannot reverse
    — comes back with ``has_snapshot: false``, which the UI renders as "No
    change snapshot available" rather than as an empty diff.
    """
    from ciao.memory_receipts import is_undoable

    config = request.app.state.config
    rid = request.path_params["id"]
    workspace = request.query_params.get("workspace", "").strip()
    receipt, found_in = await asyncio.to_thread(
        _find_receipt_in_workspaces, config, rid, workspace
    )
    if receipt is None:
        return JSONResponse({"error": f"unknown receipt: {rid}"}, status_code=404)
    before = receipt.get("before_text")
    after = receipt.get("after_text")
    has_snapshot = isinstance(before, str) and isinstance(after, str)
    payload: dict[str, Any] = {
        "id": rid,
        "workspace": found_in or str(receipt.get("workspace", "")),
        "kind": str(receipt.get("kind", "")),
        "status": str(receipt.get("status", "")),
        "ts": str(receipt.get("ts", "")),
        "actor": str(receipt.get("actor", "")),
        "source": str(receipt.get("source", "")),
        "destination": str(receipt.get("destination", "")) or str(receipt.get("region", "")),
        "fact_text": str(receipt.get("fact_text", "")),
        "undoable": is_undoable(receipt),
        "has_snapshot": has_snapshot,
        "changed": bool(receipt.get("changed", True)),
        "error": str(receipt.get("error", "")),
    }
    if not has_snapshot:
        payload["reason"] = (
            "this operation was recorded before change snapshots existed, or it "
            "is not one the receipt protocol can reverse"
        )
        return JSONResponse(payload)
    diff, diff_truncated = _receipt_diff(
        str(before), str(after), _receipt_separator(str(receipt.get("kind", "")))
    )
    payload["before"] = str(before)[:_RECEIPT_IMAGE_MAX]
    payload["after"] = str(after)[:_RECEIPT_IMAGE_MAX]
    payload["truncated"] = (
        len(str(before)) > _RECEIPT_IMAGE_MAX or len(str(after)) > _RECEIPT_IMAGE_MAX
    )
    payload["diff"] = diff
    payload["diff_truncated"] = diff_truncated
    if not payload["undoable"]:
        # Say which of the reasons applies rather than only hiding the button:
        # a row that simply belongs to a multi-row batch is not "legacy".
        if receipt.get("undoable") is False:
            payload["reason"] = (
                "this row is part of a batch whose single transaction receipt "
                "carries the undo; undoing it alone would restore the other rows too"
            )
        elif str(receipt.get("status", "")) != "applied":
            payload["reason"] = f"this operation is {receipt.get('status', 'unsettled')}"
        elif receipt.get("undo_of"):
            payload["reason"] = "this is itself an undo"
        else:
            payload["reason"] = "this operation is not one the protocol can reverse"
    return JSONResponse(payload)


async def memory_receipt_undo(request: Request) -> JSONResponse:
    """Reverse one applied, conflict-free receipt.

    Refuses (409) when the destination changed since the operation — undo would
    otherwise delete an unrelated later fact — and (400) when the receipt is
    unsupported/legacy/view-only.
    """
    from ciao.memory_receipts import (
        MemoryReceiptError,
        RevisionConflict,
        UndoUnsupported,
        find_receipt,
        journal_path,
        undo_receipt,
    )

    config = request.app.state.config
    rid = request.path_params["id"]
    workspace = request.query_params.get("workspace", "").strip()
    try:
        vault = Path(config.workspace_vault_root(workspace)) if workspace else None
    except (AttributeError, ValueError):
        vault = None
    if vault is None:
        # No workspace named: search every journal for the id.
        for candidate in config.workspace_names():
            try:
                candidate_vault = Path(config.workspace_vault_root(candidate))
            except (AttributeError, ValueError):
                continue
            if find_receipt(journal_path(candidate_vault, None), rid) is not None:
                vault = candidate_vault
                workspace = candidate
                break
    if vault is None:
        return JSONResponse({"error": f"unknown receipt: {rid}"}, status_code=404)
    try:
        result = await asyncio.to_thread(
            undo_receipt, rid, vault_root=vault, actor="operator", source="pwa"
        )
    except RevisionConflict as exc:
        return JSONResponse(
            {"error": str(exc), "id": rid, "conflict": True}, status_code=409
        )
    except UndoUnsupported as exc:
        return JSONResponse(
            {"error": str(exc), "id": rid, "undoable": False}, status_code=400
        )
    except MemoryReceiptError as exc:
        return JSONResponse({"error": str(exc), "id": rid}, status_code=404)
    return JSONResponse(
        {"ok": True, "id": rid, "workspace": workspace, "receipt": result.get("id", "")}
    )


async def dismiss_older_than(request: Request) -> JSONResponse:
    """Atomically drop every queued row dated before a cutoff.

    A July proposal about a forgotten chat is not worth promoting; this clears
    whole dated sections at once. Atomic: all matching rows are removed in one
    rewrite of each affected file, so a crash mid-batch leaves no file half
    written.
    """
    config = request.app.state.config
    raw = request.query_params.get("date", "").strip()
    try:
        cutoff = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return JSONResponse({"error": "date must be YYYY-MM-DD"}, status_code=400)
    removed = 0
    for workspace in config.workspace_names():
        queue = proposal_service._proposals_file(config, workspace)
        if not queue.is_file():
            continue
        # Read, classify and rewrite under the queue lock, off the event loop:
        # `queue_lock` retries with a synchronous sleep while another writer
        # holds it, so running it inline would stall every other request and
        # WebSocket for the wait.
        try:
            result = await asyncio.to_thread(
                proposal_service._sweep_queue_file, queue, cutoff, config, workspace
            )
        except QueueReceiptUnavailable as exc:
            return JSONResponse(
                {
                    "error": "the memory receipt journal is unavailable; "
                    "no proposals were removed",
                    "detail": str(exc),
                },
                status_code=503,
            )
        removed += result["removed"]
        if result["changed"]:
            for swept_kind, swept_text, swept_source in zip(
                result["kinds"], result["texts"], result["sources"]
            ):
                # Expiry is a decision too: without the text in the dedupe
                # history, a curator pass that re-reads the same transcript
                # re-files the fact the operator just let expire. Same handler
                # as an explicit dismiss, so both land in both ledgers.
                proposal_actions.record_decision(
                    queue,
                    action="dismiss",
                    text=swept_text,
                    kind=swept_kind,
                    via="pwa",
                    workspace=workspace,
                    source=swept_source,
                    outcome="swept",
                )
    return JSONResponse({"ok": True, "removed": removed})


async def proposals_batch(request: Request) -> JSONResponse:
    """Accept or dismiss a set of proposals atomically.

    Body: ``{"action": "accept"|"dismiss", "ids": [...]}``. Every id must
    resolve or the batch is rejected with 404 and no file changes. ``accept``
    routes through each row's own descriptor (region edit for memory/profile,
    a file move for rehome) and returns per-row results; it never performs the
    edit itself, matching the MCP resolve path where promotion is a separate
    explicit step.
    """
    config = request.app.state.config
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    action = str(body.get("action", "")).strip()
    raw_ids = body.get("ids")
    if action not in {"accept", "dismiss"} or not isinstance(raw_ids, list) or not raw_ids:
        return JSONResponse({"error": "action must be accept|dismiss and ids[] is required"}, status_code=400)
    ids = [str(pid).strip() for pid in raw_ids]
    requested_workspace = str(body.get("workspace", "") or "").strip()
    # Per-row revisions from the review cards the operator actually read, the
    # same contract the single-row accept uses. A row whose destination moved
    # since its preview fails on its own and stays queued; the rest of the
    # batch still runs, because one stale card is not a reason to refuse a
    # selection of twenty.
    raw_revisions = body.get("revisions")
    revisions: dict[str, str] = {}
    if isinstance(raw_revisions, dict):
        revisions = {
            str(key): str(value or "").strip()
            for key, value in raw_revisions.items()
            if str(value or "").strip()
        }
    resolved, error = proposal_service._resolve_batch(config, ids)
    if error or resolved is None:
        return JSONResponse({"error": error}, status_code=404)

    # An accept mutates every destination first - region writes, project-doc
    # folds, people notes, learnings counts - and only then rewrites the queue,
    # so both guards the single-row route applies have to run here too, before
    # the first mutation, and the claim has to stay held until that rewrite
    # lands. They cover the two ways the old order went wrong:
    #   * `_rewrite_queue_batch` is what raises `QueueReceiptUnavailable`, and
    #     it runs last, so an unwritable or full journal returned 503 with
    #     every destination already mutated and every bullet still queued - and
    #     the operator's retry applied the whole batch a second time;
    #   * two requests accepting the same row both reached the promotion and
    #     contended only at the rewrite, so the fact was written twice and the
    #     loser still reported success.
    # The moves below mutate two vaults, so they sit inside both guards.
    contested: list[str] = []
    with contextlib.ExitStack() as claims:
        if action == "accept":
            queued = [ctx for ctx in resolved if not ctx.get("file")]
            by_queue: dict[str, list[str]] = {}
            for ctx in queued:
                by_queue.setdefault(ctx["path"], []).append(ctx["row"]["id"])
            claimed: set[str] = set()
            for claim_path, claim_ids in by_queue.items():
                claimed |= claims.enter_context(
                    proposal_service.claim_proposals(Path(claim_path), claim_ids)
                )
            # A row another request is already promoting is reported as a
            # conflict and left queued; the rest of the batch still runs.
            contested = [
                ctx["row"]["id"] for ctx in queued if ctx["row"]["id"] not in claimed
            ]
            resolved = [
                ctx
                for ctx in resolved
                if ctx.get("file") or ctx["row"]["id"] in claimed
            ]
            # Off the event loop, like the single-row probe: a stat on a slow
            # filesystem must not stall every other request.
            targets = sorted({(ctx["workspace"], ctx["path"]) for ctx in queued})
            if targets and not await asyncio.to_thread(
                _accept_journals_writable, config, targets
            ):
                return JSONResponse(
                    {
                        "error": "the memory receipt journal is unavailable; "
                        "no proposals were accepted",
                    },
                    status_code=503,
                )

        # Re-home rows are MOVES, so they are handled before the queue-file grouping
        # too, and one at a time: each move rewrites references across both vaults, so
        # the second move has to see what the first one wrote. Off the event loop for
        # the same reason as the single-row path — a sweep per row is real work, and a
        # cancelled handler leaves notes moved with their rows still queued.
        move_rows = [
            ctx for ctx in resolved
            if action == "accept" and ctx["row"].get("kind") == "rehome"
        ]
        results_moves: list[dict[str, Any]] = []
        moved_ids: set[str] = set()
        moved_destinations: dict[str, str] = {}
        for ctx in move_rows:
            row = ctx["row"]
            target, target_error = proposal_service._rehome_target(row, requested_workspace)
            if target_error:
                results_moves.append(proposal_actions.ProposalActionResult(
                    id=row["id"], action="move_file", dismissed=False,
                    error=target_error,
                ).as_dict())
                continue
            outcome = await asyncio.to_thread(proposal_service._perform_rehome_move, config, row, target)
            if not outcome.get("ok"):
                results_moves.append(proposal_actions.ProposalActionResult(
                    id=row["id"], action="move_file", dismissed=False,
                    error=outcome["error"],
                ).as_dict())
                continue
            moved_ids.add(row["id"])
            moved_destinations[row["id"]] = str(outcome.get("destination", ""))
            results_moves.append(proposal_actions.ProposalActionResult(
                id=row["id"], action="move_file", dismissed=True,
                destination=str(outcome.get("destination", "")),
                already_moved=bool(outcome.get("already_moved", False)),
            ).as_dict())
        # Only the rows whose move landed may have their bullet dropped; a failed move
        # keeps its row so the note is not left somewhere nobody asked for with
        # nothing recording it.
        resolved = [
            ctx for ctx in resolved
            if ctx not in move_rows or ctx["row"]["id"] in moved_ids
        ]

        # Skill proposals are whole files, so they are handled before the grouping:
        # the grouping below rewrites a queue file by dropping bullet lines, and a
        # skill row has no line in any queue.
        results = list(results_moves)
        for contested_id in contested:
            results.append(proposal_actions.ProposalActionResult(
                id=contested_id,
                action=action,
                dismissed=False,
                error="this proposal is already being resolved",
            ).as_dict())
        file_rows = [ctx for ctx in resolved if ctx.get("file")]
        resolved = [ctx for ctx in resolved if not ctx.get("file")]
        for ctx in file_rows:
            row = ctx["row"]
            # Same result shape a bullet dismiss returns, so the client needs no
            # second contract for a row it renders identically.
            if action != "dismiss":
                results.append(proposal_actions.ProposalActionResult(
                    id=row["id"],
                    action=action,
                    dismissed=False,
                    error="a skill proposal is a file; there is nothing to promote",
                ).as_dict())
                continue
            outcome = proposal_service._dismiss_skill_proposal(ctx)
            skill_result = proposal_actions.ProposalActionResult(
                id=row["id"],
                action="dismiss",
                dismissed=bool(outcome.get("ok")),
                error=None if outcome.get("ok") else outcome["error"],
            )
            if outcome.get("ok") and ctx["workspace"]:
                # The file is already unlinked; a sidecar write failure must not
                # fail a dismiss that happened.
                try:
                    proposal_actions.record_decision(
                        proposal_service._proposals_file(config, ctx["workspace"]),
                        action="dismiss",
                        text=row["text"],
                        kind="skill",
                        via="pwa",
                        workspace=ctx["workspace"],
                        proposal_id=row["id"],
                    )
                except OSError:
                    logger.info(
                        "proposals: could not record skill dismissal for %s", row["id"]
                    )
            results.append(skill_result.as_dict())

        # Group by file so each affected file is rewritten exactly once.
        by_file: dict[str, dict[str, Any]] = {}
        for ctx in resolved:
            entry = by_file.setdefault(ctx["path"], {"workspace": ctx["workspace"], "lines": set(), "rows": []})
            entry["lines"].add(ctx["line"])
            entry["rows"].append(ctx["row"])

        # Rows whose bullet THIS request actually dropped. A concurrent resolver
        # may have removed a row between this request's scan and its write; the
        # loser reports success to the client (the row is gone either way) but
        # records no outcome - the winner already did.
        self_request_removed: set[str] = set()
        recorded: set[str] = set()

        for path, entry in by_file.items():
            queue = Path(path)
            # Write every promotion BEFORE dropping any bullet, and only drop the
            # ones that landed. A batch that removed the lines first would lose every
            # fact whose region was over cap, silently and in bulk.
            promoted: dict[str, proposal_service.AcceptOutcome] = {}
            keep_lines: set[int] = set()
            if action == "accept":
                # The claim above only covers this process. Another resolver
                # (the CLI, the undo path, a second server) may have taken a
                # row between this request's scan and here, and promoting it
                # anyway writes the fact a second time for a bullet this
                # request will then fail to remove. Read off the loop: the
                # check takes the queue's file lock.
                present = await asyncio.to_thread(
                    proposal_service.bullets_present,
                    queue,
                    [
                        (int(row["line"]), str(row.get("raw") or ""))
                        for row in entry["rows"]
                    ],
                )
                for row in entry["rows"]:
                    accept = proposal_kinds.accept_for(row["kind"])
                    if accept.action == "move_file":
                        # Performed above the grouping, one at a time off the loop;
                        # nothing to write here, only result shaping below.
                        continue
                    if int(row["line"]) not in present:
                        promoted[row["id"]] = proposal_service.AcceptOutcome(
                            ok=False, error="this proposal was already resolved"
                        )
                        keep_lines.add(int(row["line"]))
                        continue
                    expected = revisions.get(row["id"], "")
                    if expected:
                        current = await asyncio.to_thread(
                            proposal_service.destination_revision, config, row
                        )
                        if current and current != expected:
                            promoted[row["id"]] = proposal_service.AcceptOutcome(
                                ok=False,
                                conflict=True,
                                error="the destination changed since this "
                                "preview; nothing was written",
                            )
                            keep_lines.add(int(row["line"]))
                            continue
                    promotion: proposal_service.AcceptOutcome
                    if accept.action == "edit_region":
                        # No reconcile in the batch path: it is one model call
                        # per row, and a large selection would spend a timeout
                        # on each. A row that needs it is retried singly.
                        promotion = await proposal_service._promote_region_row(
                            config, row
                        )
                    elif accept.action == "fold_doc":
                        # A fold is a model call, so a large selection folds
                        # sequentially; write-then-dismiss still holds per row.
                        promotion = await proposal_service._accept_project_row(config, row)
                    elif accept.action == "write_people_note":
                        promotion = await proposal_service._accept_people_row(config, row)
                    elif accept.action == "append_learnings":
                        promotion = proposal_service._accept_learnings_row(config, row)
                    else:
                        # route_manually: nothing to perform, and the row stays.
                        promotion = proposal_service.AcceptOutcome(
                            ok=False, error="no destination yet"
                        )
                    promoted[row["id"]] = promotion
                    if not promotion.ok:
                        keep_lines.add(int(row["line"]))

            # The batch is one atomic file rewrite: a single transaction-level
            # prepared/applied receipt pair carries the whole-file before/after
            # image, and the remaining facts are recorded as non-undoable history
            # rows. Undoing each fact's row separately restored the whole pre-batch
            # file and resurrected the other bullets (including accepted ones). The
            # bracket writes the prepared row before the rewrite so a crash between
            # the write and the record is recoverable. The locked transaction runs
            # in a worker thread so a contended queue lock cannot stall the loop.
            try:
                vault_for_receipt = Path(config.workspace_vault_root(entry["workspace"]))
            except (AttributeError, ValueError):
                vault_for_receipt = queue.parent.parent
            removals = [
                {
                    "text": str(row.get("text") or ""),
                    "kind": str(row.get("kind") or ""),
                    "promoted": action == "accept",
                }
                for row in entry["rows"]
            ]
            try:
                removed_here = await asyncio.to_thread(
                    proposal_service._rewrite_queue_batch,
                    queue,
                    entry["rows"],
                    keep_lines,
                    removals,
                    entry["workspace"],
                    vault_for_receipt,
                )
            except QueueReceiptUnavailable as exc:
                return JSONResponse(
                    {
                        "error": "the memory receipt journal is unavailable; "
                        "no proposals were removed",
                        "detail": str(exc),
                    },
                    status_code=503,
                )
            self_request_removed.update(removed_here)
            # Record THIS queue's outcomes immediately after its rewrite lands: a
            # later file failing to persist must not take already-persisted
            # resolutions out of the tally — their ids are gone, so a retry can
            # never re-record them. Rows whose bullet this request did not remove
            # (a concurrent resolver won) record nothing; the winner already did.
            for row in entry["rows"]:
                pid = row["id"]
                if pid not in removed_here or pid in recorded:
                    continue
                recorded.add(pid)
                # Same contract as the single-row route, through the same
                # handler: the decision's text must outlive the row or the
                # nightly curator re-files it, and only the extraction kinds
                # reach the outcomes tally (skill rows come from skill
                # evolution, rehome rows from vault hygiene).
                destination = ""
                # An outcome with nothing set is how a row this request did not
                # promote reports: no ``ok`` at all, which the builders read as
                # "nothing was written here", not as a failure.
                row_outcome = proposal_service.AcceptOutcome()
                if action == "accept":
                    accept_here = proposal_kinds.accept_for(row["kind"])
                    if accept_here.action == "move_file":
                        # The move ran above the grouping; only where the note
                        # landed matters to the decision record.
                        row_outcome = proposal_service.AcceptOutcome(
                            destination=moved_destinations.get(pid, "")
                        )
                    else:
                        row_outcome = promoted.get(pid, proposal_service.AcceptOutcome())
                    destination = proposal_service._decision_destination(
                        accept_here.action, row, row_outcome
                    )
                proposal_actions.record_decision(
                    queue,
                    action=action,
                    text=str(row.get("text") or ""),
                    kind=str(row.get("kind") or ""),
                    via="pwa",
                    workspace=entry["workspace"],
                    source=str(row.get("source") or ""),
                    destination=destination,
                    outcome=(
                        ("duplicate" if row_outcome.duplicate else "written")
                        if action == "accept"
                        else ""
                    ),
                    proposal_id=pid,
                    # The ledger keeps the ORIGINAL bullet as ``text``
                    # (append-time dedupe compares against it), so an edited
                    # accept is unmatchable by text. The write hands its
                    # receipt back here instead. A dismiss has no outcome and
                    # so no receipt, which is the empty default.
                    receipt_id=row_outcome.receipt_id or "",
                )
            for row in entry["rows"]:
                if action == "accept":
                    accept = proposal_kinds.accept_for(row["kind"])
                    # An absent outcome means nothing was written here (a rehome
                    # move performed above the grouping), which is a success.
                    # `written` says what actually landed, which is not always
                    # the row's text: the event-shape guard can promote only a
                    # bullet's trailing "Durable rule:" clause, and `duplicate`
                    # says the fact was already there. A `conflict` the builder
                    # carries through is told apart from an ordinary refusal:
                    # the row is still promotable, just not against the body
                    # the operator read, so the UI reopens its preview rather
                    # than reporting a permanent failure.
                    results.append(proposal_actions.build_accept_result(
                        row["id"],
                        accept,
                        row,
                        promoted.get(
                            row["id"], proposal_service.AcceptOutcome()
                        ).as_dict(),
                        include_usage=False,
                    ).as_dict())
                else:
                    results.append(proposal_actions.ProposalActionResult(
                        id=row["id"], action="dismiss", dismissed=True
                    ).as_dict())
        return JSONResponse(
            {
                "ok": True,
                "action": action,
                "results": results,
                "summary": _batch_summary(action, results),
            }
        )


def _batch_destination(result: dict[str, Any]) -> str:
    """Where one batch row landed (or would have), as the summary groups it.

    A region row names its region; the file-writing kinds name their path; a
    dismiss has no destination at all, which is its own group.
    """
    action = str(result.get("action", ""))
    if action == "edit_region":
        region = str(result.get("region", ""))
        return f"ciao:{region}" if region else "ciao:memory"
    if action == "dismiss":
        return ""
    return str(result.get("destination", ""))


def _batch_summary(action: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per destination the batch touched, with its per-row outcomes.

    A fifty-row accept used to report fifty independent lines, which is the
    same information the queue already showed and says nothing about where the
    facts went. Grouping by destination answers the question a bulk accept
    actually raises — what changed, and where — while `failed_ids` keeps every
    per-row failure addressable rather than averaged away.
    """
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for result in results:
        destination = _batch_destination(result)
        group = groups.get(destination)
        if group is None:
            group = {
                "destination": destination,
                "action": action,
                "total": 0,
                "ok": 0,
                "failed": 0,
                "conflicts": 0,
                "duplicates": 0,
                "failed_ids": [],
                "errors": [],
            }
            groups[destination] = group
            order.append(destination)
        group["total"] += 1
        # A dismiss reports `dismissed`; an accept reports `promoted`, and a
        # rehome move reports neither because the move happened before the
        # grouping — `dismissed` is the only signal it leaves.
        succeeded = (
            bool(result.get("promoted"))
            if "promoted" in result
            else bool(result.get("dismissed"))
        )
        if result.get("duplicate"):
            group["duplicates"] += 1
        if succeeded and not result.get("error"):
            group["ok"] += 1
            continue
        group["failed"] += 1
        if result.get("conflict"):
            group["conflicts"] += 1
        group["failed_ids"].append(str(result.get("id", "")))
        message = str(result.get("error", ""))
        if message and message not in group["errors"]:
            group["errors"].append(message)
    return [groups[key] for key in order]


def _accept_journal_writable(config: Any, workspace: str, queue_path: str) -> bool:
    """Pre-flight for the proposal accept path; runs off the event loop.

    Even a stat-only probe must not run inline in the handler: on a slow or
    contended filesystem it stalls every concurrent ASGI request and
    WebSocket until it returns.
    """
    # Deferred import: a route handler in this module is also named
    # `memory_receipts`, which shadows the module at function scope.
    from ciao import memory_receipts as _receipts

    queue = Path(queue_path)
    try:
        vault = Path(config.workspace_vault_root(workspace))
    except (AttributeError, ValueError):
        vault = queue.parent.parent
    return _receipts.journal_writable(_receipts.journal_path(vault, queue.parent))


def _accept_journals_writable(
    config: Any, targets: Iterable[tuple[str, str]]
) -> bool:
    """The batch accept's pre-flight: every queue it would promote into.

    One worker-thread hop for the whole batch rather than one per row, for the
    same reason the single-row probe takes one: even stat-only work must not
    run on the event loop.
    """
    return all(
        _accept_journal_writable(config, workspace, path)
        for workspace, path in targets
    )


async def proposal_action(request: Request) -> JSONResponse:
    """Accept or dismiss exactly one proposal by its stable id.

    ``accept`` PERFORMS the promotion: a memory/profile row is written into that
    workspace's bounded region, then the bullet is dropped. Write-then-dismiss,
    never the reverse — if the write fails (over cap, unreadable guide) the bullet
    stays and the error comes back, because the reverse order loses the fact.

    ``dismiss`` drops the bullet without writing anything. Unknown id is 404.

    An optional JSON body carries the review card's two extras:

    * ``expected_revision`` — the destination digest the operator's preview was
      computed against. A destination that changed since then is refused with
      409 and a refreshed preview, so an accept can never land on top of an
      edit nobody saw. Absent means "no preview was shown", which keeps every
      existing client and the MCP path working unchanged.
    * ``text`` — an edited wording to promote instead of the bullet's own. The
      decision history still records the bullet's original text, because that
      is what the dedupe readers compare a re-extracted fact against.
    """
    config = request.app.state.config
    pid = request.path_params["id"]
    _rows, by_id = proposal_service._scan_proposal_rows(config)
    ctx = by_id.get(pid)
    if ctx is None:
        return JSONResponse({"error": f"unknown proposal id: {pid}"}, status_code=404)
    action = request.path_params.get("action", "").strip()
    # A body is optional here and always has been; a client that sends none
    # (or sends something unparseable) gets the pre-preview behaviour rather
    # than a 400 for a field it never had to supply.
    try:
        raw_body = await request.json()
    except Exception:  # noqa: BLE001 — no body, or not JSON: both mean "no extras"
        raw_body = {}
    body = raw_body if isinstance(raw_body, dict) else {}
    expected_revision = str(body.get("expected_revision", "") or "").strip()
    edited_text = str(body.get("text", "") or "").strip()
    # Validate BEFORE any file mutation, the same shape the batch endpoint uses.
    # Unvalidated, anything that was not "accept" skipped the promotion block
    # below but still fell through to the bullet removal and returned the
    # dismiss-shaped success payload: a typo in the path, or a stale client,
    # silently discarded a queued fact nobody had asked to dismiss.
    if action not in {"accept", "dismiss"}:
        return JSONResponse(
            {"error": "action must be accept|dismiss", "id": pid}, status_code=400
        )
    row = ctx["row"]

    if ctx.get("file"):
        # A whole file, not a bullet in a queue: the line-removal path below
        # would read it and delete line -1 of it.
        if action != "dismiss":
            return JSONResponse(
                {
                    "error": "a skill proposal is a file, so there is nothing to "
                             "promote; open it and turn it into a skill, or dismiss it",
                    "id": pid,
                },
                status_code=400,
            )
        outcome = proposal_service._dismiss_skill_proposal(ctx)
        if not outcome.get("ok"):
            return JSONResponse({"error": outcome["error"], "id": pid}, status_code=409)
        # Not recorded in the outcomes tally: that ledger measures the MEMORY
        # extraction pipeline, and skill proposals come from the separate
        # skill-evolution pipeline. It IS recorded in the decision history,
        # so the review page's History tab shows it was resolved.
        # Guarded like the batch path: ``workspace_vault_root("")`` falls back
        # to the default root, so a row with a blank workspace would file its
        # decision into the wrong workspace's sidecar. And the file is already
        # gone by now — a recording failure must not turn a completed dismiss
        # into a 500, or the client's retry 404s on work that succeeded.
        if row["workspace"]:
            try:
                proposal_actions.record_decision(
                    proposal_service._proposals_file(config, row["workspace"]),
                    action="dismiss",
                    text=row["text"],
                    kind="skill",
                    via="pwa",
                    workspace=row["workspace"],
                    proposal_id=pid,
                )
            except OSError:
                logger.info("proposals: could not record skill dismissal for %s", pid)
        return JSONResponse(
            proposal_actions.ProposalActionResult(
                id=pid, action="dismiss", dismissed=True
            ).as_dict()
        )

    # What the promotion did, whichever accept ran — one typed outcome, so the
    # refusals below, the result builder and the decision record read one
    # shape. A dismiss promotes nothing and leaves it empty.
    promoted = proposal_service.AcceptOutcome()
    queue = Path(ctx["path"])
    # Claimed BEFORE the promotion and held until the queue rewrite has landed:
    # two tabs accepting the same row both promoted (a doc folded twice, a
    # recurrence count incremented twice) and only then contended on the
    # rewrite, where the loser removed nothing and still reported success. See
    # `proposal_service.claim_proposals` for why the claim is in-process and
    # the file lock is not held across the promotion. A dismiss promotes
    # nothing, so it needs no claim: the loser's rewrite is already a no-op.
    with contextlib.ExitStack() as claim:
        if action == "accept":
            if pid not in claim.enter_context(
                proposal_service.claim_proposals(queue, [pid])
            ):
                return JSONResponse(
                    {
                        "error": "this proposal is already being resolved; "
                        "reload the queue to see the outcome",
                        "id": pid,
                    },
                    status_code=409,
                )
            # Checked BEFORE any promotion. The queue rewrite below is what raises
            # `QueueReceiptUnavailable`, and it runs last — so an unwritable
            # journal returned 503 with the region already written, the doc already
            # folded or the learning already appended, and the row still queued.
            # The retry then did it a second time.
            # Off the event loop (see _accept_journal_writable): the probe runs
            # before any promotion, and a slow filesystem must not stall the loop.
            if not await asyncio.to_thread(
                _accept_journal_writable, config, ctx["workspace"], ctx["path"]
            ):
                return JSONResponse(
                    {
                        "error": "the memory receipt journal is unavailable; "
                        "the proposal was not accepted",
                        "id": pid,
                    },
                    status_code=503,
                )
            # And the row must still BE queued. The claim above only covers
            # this process; the CLI, the undo path or a second server may have
            # resolved the row between this request's scan and here, and
            # promoting it anyway writes the fact a second time for a bullet
            # this request will then fail to remove. Off the loop: the check
            # reads the queue under its file lock.
            if not await asyncio.to_thread(
                proposal_service.bullets_present,
                queue,
                [(int(ctx["line"]), str(ctx["row"].get("raw") or ""))],
            ):
                return JSONResponse(
                    {
                        "error": "this proposal was already resolved; "
                        "reload the queue to see the outcome",
                        "id": pid,
                    },
                    status_code=409,
                )
            # The destination must still be what the operator's preview showed.
            # Without this, an accept sitting open while a /remember, a nightly
            # pass or a hand edit changed the region landed on top of a body
            # nobody had read — the "unseen overwrite" the review card exists to
            # rule out. The refreshed preview rides along so the client can
            # re-render the card instead of asking for it again. Off the loop:
            # it reads the destination.
            if expected_revision:
                current = await asyncio.to_thread(
                    proposal_service.destination_revision, config, row
                )
                if current and current != expected_revision:
                    refreshed = await asyncio.to_thread(
                        proposal_service.preview_row, config, ctx, edited_text
                    )
                    return JSONResponse(
                        {
                            "error": "the destination changed since this preview; "
                            "nothing was written. Review the refreshed change and "
                            "confirm again.",
                            "id": pid,
                            "conflict": True,
                            "preview": refreshed,
                        },
                        status_code=409,
                    )
            # An edited wording promotes instead of the bullet's own text. The
            # queue removal and the decision record below still use `row`: the
            # dedupe readers compare a re-extracted fact against the ORIGINAL
            # text, so recording the edit there would let the curator re-queue
            # the same bullet on its next pass.
            promote_row = {**row, "text": edited_text} if edited_text else row
            accept = proposal_kinds.accept_for(row["kind"])
            if accept.action == "move_file":
                target, error = proposal_service._rehome_target(row, request.query_params.get("workspace", "").strip())
                if error:
                    return JSONResponse({"error": error, "id": pid}, status_code=400)
                # Off the event loop: the sweep reads and rewrites notes across both
                # vaults, and doing that inline blocked the loop long enough for the
                # request to time out — after the git mv and before the queue row was
                # dropped, so the note moved and its row stayed.
                outcome = await asyncio.to_thread(proposal_service._perform_rehome_move, config, row, target)
                if not outcome.get("ok"):
                    # Move-then-dismiss, the same order as a region write: the bullet
                    # survives a failed move so the note is not silently left where it
                    # was with nothing recording that it should not be.
                    return JSONResponse(
                        {"error": outcome["error"], "id": pid}, status_code=409
                    )
                # The move reports more than an accept does (the rewritten files,
                # the mover's own result); what the response and the decision
                # record read from it is where the note landed.
                promoted = proposal_service.AcceptOutcome(
                    ok=True, destination=str(outcome.get("destination", ""))
                )
            elif accept.action == "edit_region":
                # `?reconcile=1` re-runs the write-time reconcile against the
                # region's current entries before writing, which is how a fact
                # the archive-time pass deferred (timed-out call, stale index)
                # gets resolved rather than appended beside what it supersedes.
                # Opt-in: it is a model call, and the plain accept is one
                # synchronous write.
                reconcile = (
                    request.query_params.get("reconcile", "").strip().lower()
                    in {"1", "true", "yes"}
                )
                promoted = await proposal_service._promote_region_row(
                    config, promote_row, reconcile=reconcile
                )
                if not promoted.ok:
                    # The bullet is untouched, so the fact is still queued and the
                    # operator can fix the cause (usually an over-cap region) and
                    # retry. Losing it silently is the one outcome to avoid.
                    refusal: dict[str, Any] = {
                        "error": promoted.error or "could not write the region",
                        "id": pid,
                        "region": promoted.region or "",
                    }
                    if promoted.deferred:
                        # The one refusal another `?reconcile=1` can resolve, so
                        # it is marked as such and carries what it was weighed
                        # against. Every other refusal here needs a human to
                        # change something first (an over-cap region, event-shaped
                        # text), and offering a retry for those would be a button
                        # that cannot do what it says.
                        refusal["deferred"] = True
                        refusal["reason"] = promoted.reason or ""
                        refusal["competing"] = list(promoted.competing or ())
                    return JSONResponse(refusal, status_code=409)
            elif accept.action == "fold_doc":
                promoted = await proposal_service._accept_project_row(config, promote_row)
                if not promoted.ok:
                    return JSONResponse(
                        {"error": promoted.error or "fold failed", "id": pid},
                        status_code=409,
                    )
            elif accept.action == "write_people_note":
                promoted = await proposal_service._accept_people_row(config, promote_row)
                if not promoted.ok:
                    return JSONResponse(
                        {"error": promoted.error or "could not write the note", "id": pid},
                        status_code=409,
                    )
            elif accept.action == "append_learnings":
                promoted = proposal_service._accept_learnings_row(config, promote_row)
                if not promoted.ok:
                    return JSONResponse(
                        {"error": promoted.error or "could not append", "id": pid},
                        status_code=409,
                    )
            else:
                # route_manually: a [review] row has no known destination, so an
                # accept would be a guess wearing a button.
                return JSONResponse(
                    {
                        "error": "this row has no destination yet; decide what it is first",
                        "id": pid,
                    },
                    status_code=400,
                )

        try:
            vault_for_receipt = Path(config.workspace_vault_root(ctx["workspace"]))
        except (AttributeError, ValueError):
            vault_for_receipt = queue.parent.parent
        # A concurrent request, the undo path, or the CLI may have removed this
        # bullet first; the loser must not rewrite the file around the winner's
        # deletion, and only the request that actually removes the row records its
        # outcome (the winner already did). The locked read/remove/rewrite runs in a
        # worker thread so a contended queue lock cannot stall the event loop, and
        # the prepared receipt is written before the rewrite so a crash between the
        # two is still recoverable: bullet gone means the removal landed.
        try:
            removed_ours = await asyncio.to_thread(
                proposal_service._rewrite_queue_single,
                queue,
                ctx["line"],
                str(ctx["row"].get("raw") or ""),
                str(row.get("text") or ""),
                str(row.get("kind") or ""),
                action == "accept",
                ctx["workspace"],
                vault_for_receipt,
            )
        except QueueReceiptUnavailable as exc:
            return JSONResponse(
                {
                    "error": "the memory receipt journal is unavailable; "
                    "the proposal was not removed",
                    "detail": str(exc),
                    "id": pid,
                },
                status_code=503,
            )

    if action == "accept":
        accept = proposal_kinds.accept_for(row["kind"])
        # The payload shape is the batch route's, built once: an accept that
        # reports a region, a destination or a rehome candidate must read the
        # same either way. `usage` is the one field only this route reports.
        # Rehome rows land in the last branch: the note itself is not moved
        # there. Moving a file and rewriting every reference to it is
        # `vault_rehome`'s job and it is reversible through its own receipt;
        # doing half of it from a queue row would leave the links pointing at
        # a path that moved.
        result = proposal_actions.build_accept_result(
            pid, accept, row, promoted.as_dict(), include_usage=True
        )
        if removed_ours:
            # Preserve the accepted row's text in the same decision history a
            # dismissal uses: append-time dedupe consults the live queue and
            # that sidecar — never the promoted destination — so the nightly
            # curator would otherwise re-read the transcript and queue the
            # already-accepted fact again. The outcomes tally is the same
            # call's second half.
            proposal_actions.record_decision(
                queue,
                action="accept",
                text=str(row.get("text") or ""),
                kind=str(row.get("kind") or ""),
                via="pwa",
                workspace=ctx["workspace"],
                source=str(row.get("source") or ""),
                destination=proposal_service._decision_destination(accept.action, row, promoted),
                outcome="duplicate" if promoted.duplicate else "written",
                proposal_id=pid,
                # See the batch path: the recorded text is the original bullet,
                # so the receipt reference is the only way back to what an
                # edited accept actually wrote.
                receipt_id=promoted.receipt_id or "",
            )
        return JSONResponse({"ok": True, "result": result.as_dict()})
    if removed_ours:
        # Preserve the decided row's text: append-time dedupe consults this
        # history, so without it the nightly curator re-files the fact the
        # operator just rejected while its transcript is still recent.
        proposal_actions.record_decision(
            queue,
            action="dismiss",
            text=str(row.get("text") or ""),
            kind=str(row.get("kind") or ""),
            via="pwa",
            workspace=ctx["workspace"],
            source=str(row.get("source") or ""),
            proposal_id=pid,
        )
    return JSONResponse(
        {
            "ok": True,
            "result": proposal_actions.ProposalActionResult(
                id=pid, action="dismiss", dismissed=True
            ).as_dict(),
        }
    )


# ── Operator-action housekeeping strip ───────────────────────────────────


def _housekeeping_context(request: Request) -> "operator_actions.DetectionContext":
    """Build the cheap detection context from the request's app state.

    The package-status fetcher is the cached one the app already owns (see
    ``make_cached_package_status`` in ``app.py``), so detection never blocks
    on GitHub. The schedule manager, when present, exposes the missed one-time
    reminders.
    """
    from ciao import operator_actions

    config = request.app.state.config
    fetcher = getattr(request.app.state, "package_status_fetcher", None)
    return operator_actions.DetectionContext(
        config=config,
        schedule_store=getattr(request.app.state, "schedule_manager", None),
        package_status=fetcher if callable(fetcher) else None,
    )


async def list_housekeeping(request: Request) -> JSONResponse:
    """Return every detectable operator action for the home strip.

    This is the detector pass. Each action carries ``run_label``, ``chat_label``
    and ``chat_prompt`` so the client can render the buttons it needs and seed
    a chat without a second round-trip.
    """
    from ciao import operator_actions

    actions = operator_actions.detect_actions(_housekeeping_context(request))
    return JSONResponse({"actions": [action.as_dict() for action in actions]})


async def run_housekeeping_action(request: Request) -> JSONResponse:
    """Perform one action's mechanical work, then re-detect and return the list.

    Re-running detection in the same response is what keeps the client from
    rendering a stale strip: a condition that cleared is gone, and one that
    persists returns with its detail replaced by the failure. Unknown id is
    404, never 500.
    """
    from ciao import operator_actions

    action_id = request.path_params["action_id"]
    context = _housekeeping_context(request)
    try:
        result, summary = await operator_actions.run_action(action_id, context)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:  # noqa: BLE001 — a failed run is a tile, not a crash
        logger.exception("operator action %s failed", action_id)
        actions = operator_actions.detect_actions(context)
        # The condition persisted, so the same id is still detected. Replace its
        # detail with the failure text so the client shows a failed tile rather
        # than silently re-offering the button as though nothing happened.
        failure = str(exc)
        return JSONResponse(
            {
                "ok": False,
                "action_id": action_id,
                "error": failure,
                "summary": f"Run failed: {failure}",
                "actions": [
                    action.as_dict()
                    if action.id != action_id
                    else {
                        **action.as_dict(),
                        "detail": f"Run failed: {failure}",
                    }
                    for action in actions
                ],
            }
        )
    actions = operator_actions.detect_actions(context)
    return JSONResponse(
        {
            "ok": True,
            "action_id": action_id,
            "result": result,
            "summary": summary,
            "actions": [action.as_dict() for action in actions],
        }
    )


async def dismiss_housekeeping_action(request: Request) -> JSONResponse:
    """Record a "not now" for one action, then re-detect and return the list.

    Only ask-style actions (e.g. the GitHub star nudge) accept a dismissal; it
    writes a suppression receipt so the tile stops surfacing for a while. The
    response shape mirrors ``run_housekeeping_action`` so the client re-renders
    the strip the same way. Unknown id is 404, never 500.
    """
    from ciao import operator_actions

    action_id = request.path_params["action_id"]
    context = _housekeeping_context(request)
    try:
        result, summary = operator_actions.dismiss_action(action_id, context)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    actions = operator_actions.detect_actions(context)
    return JSONResponse(
        {
            "ok": True,
            "action_id": action_id,
            "result": result,
            "summary": summary,
            "actions": [action.as_dict() for action in actions],
        }
    )
