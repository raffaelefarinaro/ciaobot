"""REST API routes for the PWA."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import errno
import functools
import hashlib
import html
import json
import logging
import math
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, UTC
from pathlib import Path
from stat import S_ISREG
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

from ciao.claude_trim import catalog_payload
from ciao.git_proc import EXACT_BYTES
from ciao.os_support.media_types import guess_type as guess_media_type
from ciao import host_terminal
from ciao import shared_brain
from ciao import service_backend
from ciao import backup_service
from ciao import entity_types
from ciao import provider_registry
from ciao import repo_link
from ciao.repo_location import resolve_chat_location
from ciao.git_mutation import RepositoryBusyError, ensure_mutable, repository_mutation
from ciao.jsonio import loads_untrusted, write_private_text
from ciao.file_kernel import QueueLockError
from ciao.unsure_list import UnsureError, change_line, read_list, is_line_id as is_unsure_line_id
from ciao.os_support.child_env import child_env
from ciao.port_in_use import BIND_PHASE
from ciao.web.auth import (
    LOOPBACK_HOST_NAMES,
    _localhost_request,
    _same_origin,
    _state_change_origin_allowed,
    _setup_finish_origin_allowed,
    _setup_finish_refusal,
    is_loopback_client,
)
from ciao.web.document_conversion import ANYDOC_EXTENSIONS, is_anydoc_document
from ciao.app_settings import DEFAULT_LOG_LEVEL, DEFAULT_PWA_HOST, LOG_LEVELS
from ciao.config import CLAUDE_MODELS, MAX_IMAGE_SIZE_BYTES, CiaoConfig, RESTART_EXIT_CODE, WorkspaceConfig
from ciao.model_tiers import MAX_MODEL_ID_CHARS
from ciao.models import THINKING_LEVELS, ChatContext
from ciao.workspaces import (
    WORKSPACE_NAME_RE,
    WorkspaceRegistryConflict,
    agent_root_for,
    persist_workspaces,
    run_while_claimed,
    workspace_from_request,
    workspace_provider_options,
    workspace_provider_values,
    workspace_to_dict,
)
# Kept as an alias: several call sites predate the shared module.
_WORKSPACE_NAME_RE = WORKSPACE_NAME_RE
from ciao.core_prompt import durable_memory_guide_path, unified_core
from ciao.care_report import (
    GUIDE_PATH as CARE_GUIDE_PATH,
    REPORT_RELATIVE_PATH as CARE_REPORT_PATH,
    STEP_FILES as CARE_STEP_FILES,
    UNSURE_RELATIVE_PATH as CARE_UNSURE_PATH,
    read_care_report,
)
from ciao.provider_service import capabilities_for, supported_providers
from ciao.subprocess_step import run_step
from ciao.schedule_command import normalize_schedule_command
from ciao.schedules import (
    DEFAULT_INTERVAL_MINUTES,
    DEFAULT_TIMEZONE,
    FREQUENCIES,
    INTERVAL_FREQUENCY,
    ScheduleConflict,
    ScheduleEntry,
    ScheduleNotFound,
    ScheduleStoreCorrupt,
    compute_last_expected_run,
    compute_next_run,
    is_interval,
    normalize_archive_policy,
    normalize_days_of_week,
    normalize_interval_minutes,
    publish_automations_changed,
    run_failed_since,
    stamp_fallback_project,
    system_base_id,
    system_schedule_id,
    wall_clock_time_error,
    wall_clock_time_value_error,
    normalize_system_schedule_update,
    was_dispatched_since,
)
from ciao.setup_status import setup_status
from ciao.cli import _auth_command_for_provider
from ciao.skills_inventory import build_skill_inventory
from ciao.vault_index import (
    Entry,
    _build_graph,
    canonical_type,
    filter_entries,
    scan_targets,
    scan_vault,
    strip_references,
    write_vocabulary_file,
)
from ciao.vault_lint import EXCLUDE_DIRS, _links_in
from ciao.async_reads import run_read
from ciao.web.memory_pass import (
    MemoryProjectRefused,
    is_memory_pass_chat,
    is_memory_pass_project,
)
from ciao.web.project_chats import (
    _ALLOWED_IMAGE_EXTENSIONS,
    ChatPinConflictError,
    ChatInfo,
    ProjectChatManager,
    ProjectOrderError,
    RecoveryLifecycleConflict,
    RecoveryRequestConflict,
    RecoveryRequestChatNotFound,
    RecoveryOutcomeUnknown,
)
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
from ciao.web import request_limits
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
        # Catalog the Settings trim card renders; engine-owned so the PWA never restates defaults.
        "claude_trim_options": catalog_payload(),
    }


def _workspace_from_request(
    data: dict,
    *,
    config,
    existing: WorkspaceConfig | None = None,
) -> WorkspaceConfig:
    return workspace_from_request(data, config=config, existing=existing)


def _persist_workspaces(config, candidate_workspaces=None, *, owner=None) -> None:
    persist_workspaces(config, candidate_workspaces, owner=owner)


def _workspace_registry_snapshot(config) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Comparable snapshot for detecting registry edits across claim awaits."""
    return tuple(
        (
            name,
            {
                key: value
                for key, value in dataclasses.asdict(workspace).items()
                if not key.startswith("_")
            },
        )
        for name, workspace in config.workspaces.items()
    )


def _refresh_project_manager_workspaces(request: Request) -> None:
    pcm = getattr(request.app.state, "project_chat_manager", None)
    refresh = getattr(pcm, "refresh_workspaces", None)
    if callable(refresh):
        refresh()


def _rename_schedule_store(request: Request) -> Any:
    """The schedule store a workspace rename rewrites.

    The live manager's store when the app has one, else a store on the same
    file: the store is stateless and re-reads under its locks, so both spell
    the same rows.
    """
    from ciao.schedules import ScheduleStore  # noqa: PLC0415

    manager = _schedule_manager(request)
    if manager is not None:
        inner = getattr(manager, "_store", None)
        return inner if inner is not None else manager
    config = request.app.state.config
    return ScheduleStore(Path(config.state_path).parent)


def _rename_webhook_store(request: Request) -> Any:
    from ciao.web.routes_webhooks import webhook_store  # noqa: PLC0415

    return webhook_store(request.app.state.config)


async def _workspace_from_request_async(
    data: dict, *, config, existing: WorkspaceConfig | None = None
) -> WorkspaceConfig:
    """``_workspace_from_request`` off the event loop when it resolves shared brains.

    Resolving a new folder runs git (several short subprocesses per row), which
    must not freeze every other request while it runs.
    """
    if "shared_brains" in data:
        return await run_while_claimed(_workspace_from_request, data, config=config, existing=existing)
    return _workspace_from_request(data, config=config, existing=existing)


async def workspace_shared_brains(request: Request) -> JSONResponse:
    """Live state of each brain the workspace registers."""
    config = request.app.state.config
    workspace = config.workspace(str(request.path_params.get("name", "")))
    if workspace is None:
        return JSONResponse({"error": "unknown workspace"}, status_code=404)
    states = [
        await asyncio.to_thread(shared_brain.brain_state, link, config, workspace.name)
        for link in list(workspace.shared_brains)
    ]
    return JSONResponse({"shared_brains": states})


def _registry_conflicts(handler):
    from functools import wraps
    from ciao.workspace_archive import WorkspaceArchiveError

    @wraps(handler)
    async def wrapped(request):
        try:
            return await handler(request)
        except WorkspaceRegistryConflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except WorkspaceArchiveError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)
    return wrapped


@_registry_conflicts
async def upsert_workspace_setting(request: Request) -> JSONResponse:
    config = request.app.state.config
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected an object"}, status_code=400)
    route_name = request.path_params.get("name")
    requested = str(body.get("name") or "").strip() if route_name else ""
    if route_name:
        body = {**body, "name": route_name}
    # Serialized with archive and restore; see ``_workspace_archive_lock``.
    async with _workspace_archive_lock(request):
        renamed: dict[str, str] | None = None
        from ciao.workspace_rename import WorkspaceRenameBusy, preview_renamed_config, rename_workspace
        renaming = bool(route_name and requested and requested != route_name)
        from ciao.workspaces import workspace_registry_lock  # noqa: PLC0415

        from ciao.workspaces import check_requested_brain_kinds, requested_brain_rows

        requested_rows = []
        if "shared_brains" in body:
            try:
                requested_rows = requested_brain_rows(body["shared_brains"])
            except (ValueError, OSError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            body = {**body, "shared_brains": requested_rows}

        while True:
            with workspace_registry_lock(config):
                previous_workspaces = dict(config.workspaces)
                registry_snapshot = _workspace_registry_snapshot(config)
                existing = config.workspace(str(body.get("name", "")).strip())
            editing = existing
            if renaming:
                if existing is None:
                    return JSONResponse({"error": f"unknown workspace '{route_name}'"}, status_code=400)
                editing = preview_renamed_config(existing, old=str(route_name), new=requested)
            existing_links = getattr(existing, "shared_brains", [])
            paths = {row["path"] for row in requested_rows}
            if renaming or "shared_brains" in body:
                paths.update(os.path.realpath(link.path) for link in existing_links)
            async with _checkout_claims_for_paths(config, paths), _workspace_registry_claim(config) as registry_owner:
                with workspace_registry_lock(config):
                    if _workspace_registry_snapshot(config) != registry_snapshot:
                        continue
                try:
                    if requested_rows:
                        check_requested_brain_kinds(registry_owner, requested_rows, existing_links)
                    workspace = await _workspace_from_request_async(
                        {**body, "name": requested} if renaming else body, config=config, existing=editing
                    )
                except ValueError as exc:
                    return JSONResponse({"error": str(exc)}, status_code=400)
                created = not renaming and workspace.name not in previous_workspaces
                candidate_workspaces = {**previous_workspaces, workspace.name: workspace}
                with workspace_registry_lock(config):
                    onboarding_target = None
                    if created:
                        # Validate receipt access before mutating the live registry.
                        # Installs without onboarding retain their old save behavior.
                        from ciao.onboarding import read_receipt  # noqa: PLC0415
                        from ciao.onboarding_progress import register_new_workspace  # noqa: PLC0415

                        if read_receipt(Path(config.state_path).parent) is not None:
                            register_new_workspace(config, workspace.name)
                            onboarding_target = {"kind": "workspace", "name": workspace.name}
                    if renaming:
                        try:
                            renamed = rename_workspace(
                                config, old=str(route_name), new=requested, owner=registry_owner,
                                updated=workspace,
                                projects=getattr(request.app.state, "project_chat_manager", None),
                                schedules=_rename_schedule_store(request),
                                webhooks=_rename_webhook_store(request),
                            )
                        except WorkspaceRenameBusy as exc:
                            return JSONResponse({"error": str(exc)}, status_code=409)
                        except ValueError as exc:
                            return JSONResponse({"error": str(exc)}, status_code=400)
                    else:
                        _persist_workspaces(config, candidate_workspaces, owner=registry_owner)
                        config.workspaces[workspace.name] = workspace
                _refresh_project_manager_workspaces(request)
                if created:
                    bootstrapped = await _bootstrap_new_agent_root(config, workspace.name)
            break
        # A newly linked folder gets its first update now, not at the next loop pass.
        linked_before = {link.path for link in getattr(existing, "shared_brains", [])}
        for link in workspace.shared_brains:
            if link.path not in linked_before:
                shared_brain.schedule_update(link, config)
        payload = _workspaces_payload(config)
        if renamed is not None:
            payload["renamed"] = renamed
        if created:
            if onboarding_target is not None:
                payload["onboarding_target"] = onboarding_target
            payload["bootstrapped"] = bootstrapped
        if renamed is not None:
            _publish_workspaces_changed(request, renamed["from"], renamed["to"])
        return JSONResponse(payload, status_code=201 if created else 200)


@_registry_conflicts
async def reorder_workspaces(request: Request) -> JSONResponse:
    """Persist a reordered workspace registry. Body: {"order": [name, ...]}."""
    config = request.app.state.config
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    order = body.get("order") if isinstance(body, dict) else None
    if not isinstance(order, list) or not all(isinstance(item, str) for item in order):
        return JSONResponse({"error": "order[] is required"}, status_code=400)
    # Under the registry lock, like create/update/archive/restore.
    async with _workspace_archive_lock(request):
        current = list(config.workspaces)
        if len(order) != len(set(order)) or set(order) != set(current):
            return JSONResponse(
                {"error": "order must list every registered workspace exactly once"},
                status_code=400,
            )
        if order != current:
            from ciao.workspaces import workspace_registry_lock  # noqa: PLC0415

            async with contextlib.AsyncExitStack() as claims:
                all_paths = [
                    link.path
                    for workspace in config.workspaces.values()
                    for link in getattr(workspace, "shared_brains", []) or []
                ]
                await claims.enter_async_context(_checkout_claims_for_paths(config, all_paths))
                registry_owner = await claims.enter_async_context(_workspace_registry_claim(config))
                with workspace_registry_lock(config):
                    before = config.primary_workspace()
                    previous = dict(config.workspaces)
                    rebuilt = {name: previous[name] for name in order}
                    config.workspaces.clear()
                    config.workspaces.update(rebuilt)
                    if config.primary_workspace() != before:
                        # Without a workspace named "personal" the first entry is
                        # the main workspace; reorder must not hand it away.
                        config.workspaces.clear()
                        config.workspaces.update(previous)
                        return JSONResponse(
                            {"error": f"'{before}' is the main workspace and stays first"},
                            status_code=409,
                        )
                    try:
                        _persist_workspaces(config, owner=registry_owner)
                    except (OSError, ValueError, WorkspaceRegistryConflict) as exc:
                        config.workspaces.clear()
                        config.workspaces.update(previous)
                        if isinstance(exc, WorkspaceRegistryConflict):
                            raise
                        return JSONResponse(
                            {"error": f"Failed to save the workspace order: {exc}"},
                            status_code=500,
                        )
            _refresh_project_manager_workspaces(request)
            _publish_workspaces_changed(request)
    return JSONResponse(_workspaces_payload(config))


async def _bootstrap_new_agent_root(config, name: str) -> bool:
    """Seed a freshly created workspace's own agent root.

    A new workspace's chats run from
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
        await run_while_claimed(sync_workspace_skills, root, refresh_upstream=False)
    except Exception:  # noqa: BLE001 - creation already succeeded
        logger.exception("Could not seed agent root for new workspace %s", name)
        return False
    return True


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


def _publish_workspaces_changed(
    request: Request, old: str | None = None, new: str | None = None
) -> None:
    """Tell every open client the workspace registry changed.

    Other tabs and devices only see ``project_*`` frames when a workspace is
    archived or restored; without this their workspace list and active
    workspace stayed stale until a reload. Carries no payload by default:
    clients refetch ``/api/workspaces``. A rename passes the pair, so a
    client can follow the workspace without a second fetch.
    Fire-and-forget, like ``schedules_changed``.
    """
    pcm = getattr(request.app.state, "project_chat_manager", None)
    events = getattr(pcm, "events", None)
    if events is None:
        return
    try:
        payload: dict[str, str] = {"type": "workspaces_changed"}
        if old and new:
            payload["from"] = old
            payload["to"] = new
        events.publish(payload)
    except Exception:  # noqa: BLE001 - a missed nudge only delays a refresh
        logger.debug("Could not publish workspaces_changed", exc_info=True)


def _workspace_archive_lock(request: Request) -> asyncio.Lock:
    """One registry change at a time per app: archive, restore, create, update, reorder.

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


@contextlib.asynccontextmanager
async def _checkout_claims_for_paths(config: Any, paths: Iterable[str]):
    """Claim each affected shared checkout in the same order as Settings saves."""
    async with contextlib.AsyncExitStack() as claims:
        for path in sorted(set(paths)):
            await claims.enter_async_context(shared_brain._checkout_lock(path))
            await claims.enter_async_context(shared_brain._checkout_claim(path, config))
        yield


@contextlib.asynccontextmanager
async def _workspace_registry_claim(config: Any):
    """Acquire explicit nonblocking runtime registry ownership off-loop."""
    from ciao.workspaces import acquire_workspace_registry

    acquire_task = asyncio.create_task(
        asyncio.to_thread(acquire_workspace_registry, config, blocking=False)
    )
    try:
        owner = await asyncio.shield(acquire_task)
    except asyncio.CancelledError:
        # Keep cancellation from detaching a worker that may already own the fd.
        while not acquire_task.done():
            try:
                await asyncio.shield(acquire_task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not acquire_task.cancelled() and acquire_task.exception() is None:
            acquire_task.result().close()
        raise
    try:
        from ciao.workspaces import ensure_registry_current
        ensure_registry_current(config)
        yield owner
    finally:
        owner.close()


@contextlib.asynccontextmanager
async def _workspace_checkout_claims(config: Any, workspace_name: str):
    workspace = config.workspace(workspace_name)
    paths = [link.path for link in getattr(workspace, "shared_brains", []) or []]
    async with _checkout_claims_for_paths(config, paths):
        yield


@contextlib.asynccontextmanager
async def _archive_checkout_claims(config: Any, archive_id: str):
    from ciao import workspace_archive  # noqa: PLC0415

    folder = workspace_archive._archive_folder(config, archive_id)
    metadata = workspace_archive._read_metadata(folder)
    if metadata is None:
        raise workspace_archive.WorkspaceArchiveError("archive metadata is unreadable", 409)
    paths = workspace_archive.archive_checkout_paths(config, metadata)
    async with _checkout_claims_for_paths(config, paths):
        yield metadata


@_registry_conflicts
async def archive_workspace_setting(request: Request) -> JSONResponse:
    """Archive a workspace: unregister it and move its folder aside, intact.

    Nothing is merged into another workspace and nothing is deleted - see
    ``ciao/workspace_archive.py``. Order matters: everything that is refused is
    refused before anything changes; schedules are taken and the folder moves
    first, so a failed move changes nothing that cannot be put back; only then
    are the chats archived (irreversible), still while the workspace is
    registered; its webhook verifiers are revoked just after that; the registry
    entry goes last. A failure after the move puts the folder and schedules
    back so the workspace stays registered and the archive can be retried.
    """
    from ciao.fts_search import ConnectedIndexBusy
    from ciao import workspace_archive  # noqa: PLC0415
    from ciao.web.routes_webhooks import webhook_store  # noqa: PLC0415

    config = request.app.state.config
    name = str(request.path_params.get("name", "")).strip()
    async with _workspace_archive_lock(request), _workspace_checkout_claims(config, name), _workspace_registry_claim(config) as owner:
        try:
            target = workspace_archive.plan_archive(config, name)
            index_sources = config.index_source_roots()
            linked_roots = tuple(Path(link.path) for link in config.workspaces[name].shared_brains)
        except workspace_archive.WorkspaceArchiveError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)

        pcm = getattr(request.app.state, "project_chat_manager", None)
        busy = getattr(pcm, "workspace_busy_chat_ids", None)
        if callable(busy) and busy(name):
            return JSONResponse(
                {
                    "error": (
                        f"a chat in '{name}' is still working; "
                        "let it finish or stop it, then archive the workspace"
                    )
                },
                status_code=409,
            )

        try:
            request.app.state.project_chat_manager.refuse_workspace_teardown(name)
        except RecoveryLifecycleConflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

        scope = getattr(pcm, "workspace_scope", None)
        project_ids, chat_ids = scope(name) if callable(scope) else (set(), set())

        def _belongs(item: dict) -> bool:
            if item.get("owner_chat_id"):
                # A chat-owned loop ends with its chat, which the removal below
                # archives in the same pass. It is never snapshotted.
                return False
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
        # The chats above are archived after the folder moved, so an archive
        # chat that wrote a transcript recreated ``<install>/<name>/Logs``.
        # Those files go into the archive before anything else can fail.
        try:
            workspace_archive.carry_late_transcripts(config, target, archived)
        except OSError:
            logger.exception("Could not move the late transcripts of %s into its archive", name)
            return _roll_back("its transcripts could not be moved into the archive")
        # Archiving destroys the workspace's webhook verifiers: a revoked
        # trigger cannot be re-enabled until it is rotated, so a restored
        # workspace name reactivates no old credential. Revoked before the
        # registry entry goes, and with no await in between: a create passes
        # the ``config.workspace(name)`` check only while the workspace is
        # registered, so this cannot race a trigger being created for a name
        # that is about to be unregistered. A failure here rolls back like any
        # other step after the move, so the archive is refused rather than
        # completed with live verifiers. If ``unregister`` then fails and rolls
        # back, the triggers stay revoked on a still-registered workspace, which
        # is the safe direction: rotating recovers.
        try:
            webhook_store(config).revoke_workspace(name)
        except Exception as exc:  # noqa: BLE001 - unregistering would leave them live
            logger.exception(
                "Could not revoke webhook triggers for archived workspace %s", name
            )
            return _roll_back(f"its webhook triggers could not be revoked ({exc})")
        try:
            workspace_archive.unregister(config, name, owner=owner)
        except (OSError, WorkspaceRegistryConflict) as exc:
            logger.exception("Could not save the registry after archiving %s", name)
            return _roll_back(f"the workspace registry could not be saved ({exc})")
        _refresh_project_manager_workspaces(request)
        _publish_workspaces_changed(request)
        if schedules:
            _publish_automations_changed(request)

        # The archive is durable before this await. Report its identity even
        # when pruning fails so the operator can recover this specific archive.
        def _forget() -> tuple[int | None, Exception | None]:
            rows: int | None = None
            cleanup_exc: Exception | None = None
            try:
                rows = workspace_archive.forget_search_rows(
                    config, target.vault, workspace=name,
                    source_roots=index_sources, linked_roots=linked_roots,
                )
            except Exception as exc:  # noqa: BLE001 - the archive exists; report it
                logger.exception("Could not clear the search index for archived workspace %s", name)
                cleanup_exc = exc
            return rows, cleanup_exc

        search_rows, cleanup_exc = await run_while_claimed(_forget)
        cleanup_error = ""
        status = 200
        if cleanup_exc is not None:
            status = 503 if isinstance(cleanup_exc, ConnectedIndexBusy) else 500
            cleanup_error = (
                f"Workspace '{name}' was archived, but its search index cleanup failed ({cleanup_exc}). "
                "After resolving the error, restore this archive and archive the workspace again."
            )
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
        }
        if cleanup_error:
            payload["error"] = cleanup_error
        return JSONResponse(payload, status_code=status)


async def list_archived_workspaces(request: Request) -> JSONResponse:
    """Archived workspaces, newest first, with whether each can be restored."""
    from ciao import workspace_archive  # noqa: PLC0415

    config = request.app.state.config
    archives = await asyncio.to_thread(workspace_archive.list_archives, config)
    return JSONResponse({"archived": archives})


@_registry_conflicts
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
    async with _workspace_archive_lock(request), _archive_checkout_claims(config, archive_id) as expected_metadata, _workspace_registry_claim(config) as owner:
        try:
            folder, moved_metadata, destination = workspace_archive.move_back(
                config, archive_id, expected_metadata=expected_metadata,
            )
            metadata: dict[str, Any] = moved_metadata
        except workspace_archive.WorkspaceArchiveError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        # The registry is mutated here, on the event loop, never in the worker:
        # other handlers iterate ``config.workspaces`` on this thread.
        try:
            restored = workspace_archive.register_restored(
                config, folder, metadata, destination, owner=owner
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
                        config, folder, metadata, destination, owner=owner
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
        # The root's guide and assets came back with it, but its skill mirrors
        # may point at a packaged catalog that changed while it was archived.
        await _bootstrap_new_agent_root(config, name)
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


def _provider_config_payload_with_inventory(config) -> dict:
    from ciao.opencode_cli import opencode_credential_inventory

    payload = _provider_config_payload(config)
    row = payload["connections"].get("opencode")
    if row is not None:
        row["credential_inventory"] = opencode_credential_inventory()
    return payload


_CONNECT_NOT_ON_HOST = "This browser cannot open a terminal window on the computer running Ciaobot."


def _connect_hint(command: str) -> str:
    return (
        "Run this in the Ciaobot terminal (Terminal on any chat or project) or another "
        f"shell on the computer running Ciaobot, as the account Ciaobot runs as: {command}"
    )


async def provider_connection_action(request: Request) -> JSONResponse:
    provider = request.path_params["provider"]
    action = request.path_params["action"]
    config = request.app.state.config
    if not provider_registry.is_provider(provider):
        return JSONResponse({"error": "unsupported provider"}, status_code=404)
    if action == "connect":
        login_action = f"{provider}-login"
        if login_action not in host_terminal.ACTION_IDS:
            return JSONResponse({"error": "this provider has no sign-in action"}, status_code=404)
        try:
            command = await asyncio.to_thread(host_terminal.action_command, login_action)
        except host_terminal.NotInstalled as exc:
            return JSONResponse({"ok": False, "reason": "not_installed", "error": str(exc)}, status_code=409)
        if not host_browser_request(request):
            return JSONResponse(
                {
                    "ok": False,
                    "reason": "not_on_host",
                    "command": command,
                    "error": f"{_CONNECT_NOT_ON_HOST} {_connect_hint(command)}",
                },
                status_code=403,
            )
        workspace = Path(config.workspace_root)
        folder = workspace if workspace.is_dir() else Path.home()
        try:
            launched = await asyncio.to_thread(
                host_terminal.launch,
                folder,
                action=login_action,
                runtime_dir=Path(config.state_path).parent,
            )
        except host_terminal.TerminalUnavailable as exc:
            return JSONResponse(
                {"ok": False, "reason": exc.reason, "command": command,
                 "error": f"{exc} {_connect_hint(command)}"},
                status_code=409,
            )
        except OSError as exc:
            return JSONResponse(
                {"ok": False, "reason": "launch_failed", "command": command,
                 "error": f"Could not open a terminal: {exc}. {_connect_hint(command)}"},
                status_code=500,
            )
        return JSONResponse({"ok": True, "launched": launched, "command": command}, status_code=202)
    if action == "verify":
        adapter = provider_registry.require(provider).factory()
        adapter.invalidate_model_catalog()
        try:
            if not await adapter.wait_for_model_catalog_mutations():
                return JSONResponse(
                    {"error": "Provider logout is still running; status was not verified."},
                    status_code=503,
                    headers={"Cache-Control": "no-store"},
                )
            verification_epoch = adapter.model_catalog_epoch()

            def status_changed_during_verification() -> JSONResponse | None:
                if adapter.model_catalog_epoch() == verification_epoch:
                    return None
                return JSONResponse(
                    {"error": "Provider status changed during verification; status was not verified."},
                    status_code=503,
                    headers={"Cache-Control": "no-store"},
                )

            if provider == "claude":
                from ciao.setup_status import clear_claude_discovery_cache

                await asyncio.to_thread(clear_claude_discovery_cache)
            if provider == "opencode":
                payload = await asyncio.to_thread(_provider_config_payload_with_inventory, config)
                changed = status_changed_during_verification()
                if changed is not None:
                    return changed
                return JSONResponse(payload["connections"].get(provider, {}), headers={"Cache-Control": "no-store"})
            payload = await asyncio.to_thread(_provider_config_payload, config)
            changed = status_changed_during_verification()
            if changed is not None:
                return changed
            return JSONResponse(payload["connections"].get(provider, {}))
        finally:
            adapter.invalidate_model_catalog()
    if action == "logout":
        adapter = provider_registry.require(provider).factory()
        adapter.invalidate_model_catalog()
        logout_worker: asyncio.Task[subprocess.CompletedProcess[str]] | None = None
        try:
            # auth_command is `[binary, "auth", "login"]`, so dropping the last
            # element yields `[binary, "auth", "logout"]`. `[:1] + ["logout"]`
            # would run the obsolete top-level `claude logout`, which prints a
            # "did you mean" hint and exits 0 without signing out.
            logout_command = _auth_command_for_provider(provider)[:-1] + ["logout"]
            async def run_logout() -> subprocess.CompletedProcess[str]:
                worker = asyncio.current_task()
                try:
                    return await asyncio.to_thread(
                        subprocess.run,
                        logout_command,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                        env=child_env(),
                    )
                finally:
                    adapter.invalidate_model_catalog()
                    if worker is not None:
                        adapter.unregister_model_catalog_mutation(worker)

            # Keep a strong reference before the first await. Shielding lets the
            # subprocess finish, invalidate, and release model readers if this
            # HTTP request is canceled or the browser disconnects.
            logout_worker = asyncio.create_task(run_logout())
            adapter.register_model_catalog_mutation(logout_worker)

            def consume_worker_error(task: asyncio.Task[Any]) -> None:
                if not task.cancelled():
                    task.exception()

            logout_worker.add_done_callback(consume_worker_error)
            run = await asyncio.shield(logout_worker)
        except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        finally:
            if logout_worker is None:
                adapter.invalidate_model_catalog()
        if run.returncode != 0:
            return JSONResponse(
                {"error": (run.stderr or run.stdout or "logout failed").strip()},
                status_code=400,
            )
        return JSONResponse({"ok": True})
    return JSONResponse({"error": "unsupported action"}, status_code=404)


async def provider_config_settings(request: Request) -> JSONResponse:
    config = request.app.state.config
    return JSONResponse(
        await asyncio.to_thread(_provider_config_payload_with_inventory, config),
        headers={"Cache-Control": "no-store"},
    )


async def list_projects(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    workspace = request.query_params.get("workspace")
    projects = pcm.list_projects(workspace)
    return JSONResponse([p.to_dict() for p in projects])


async def create_project(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    config = request.app.state.config
    body = await request.json()
    repo_path = body.get("repo_path") or ""
    if not isinstance(repo_path, str):
        return JSONResponse({"error": "repo_path must be a string"}, status_code=400)
    try:
        project = pcm.create_project(
            name=body["name"],
            # An omitted workspace has to resolve to one that exists: create_project
            # does not validate the name, so an unknown one yields a project that is
            # filtered out of every workspace's sidebar.
            workspace=body.get("workspace") or config.primary_workspace(),
            context=body.get("context", ""),
            scaffold=True,
            repo_path=repo_path,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(project.to_dict(), status_code=201)


async def reorder_projects(request: Request) -> JSONResponse:
    """Persist a drag-reordered project sequence for one workspace."""
    pcm = request.app.state.project_chat_manager
    body = await request.json()
    workspace = body.get("workspace") if isinstance(body, dict) else None
    ordered_ids = body.get("order") if isinstance(body, dict) else None
    expected = body.get("expected_order") if isinstance(body, dict) else None
    if (
        not isinstance(workspace, str)
        or not workspace
        or not isinstance(ordered_ids, list)
        or not all(isinstance(pid, str) for pid in ordered_ids)
        or not isinstance(expected, list)
        or not all(isinstance(pid, str) for pid in expected)
    ):
        return JSONResponse(
            {
                "error": "workspace, order[] and expected_order[] are required",
                "code": "project_order_invalid",
            },
            status_code=400,
        )
    try:
        projects = pcm.reorder_projects(workspace, ordered_ids, expected)
    except ProjectOrderError as exc:
        if exc.code == "project_order_conflict":
            return JSONResponse(
                {"error": str(exc), "code": exc.code, "order": exc.current},
                status_code=409,
            )
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=400)
    return JSONResponse([p.to_dict() for p in projects])


async def project_detail(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    project_id = request.path_params["project_id"]
    if request.method == "DELETE":
        # `?delete_vault=1` removes the vault entry too (the folder under
        # projects/active/ and the inbound links into it). Omitting it keeps
        # the guarded default: a vault-backed project is refused.
        raw_delete_vault = request.query_params.get("delete_vault", "")
        delete_vault = raw_delete_vault.strip().lower() in {"1", "true"}
        try:
            result = pcm.delete_project(project_id, delete_vault=delete_vault)
        except RecoveryLifecycleConflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({
            "ok": result["deleted"],
            **{k: v for k, v in result.items() if k != "deleted"},
        })
    # PATCH
    body = await request.json()
    repo_path = body.get("repo_path")
    if repo_path is not None and not isinstance(repo_path, str):
        return JSONResponse({"error": "repo_path must be a string"}, status_code=400)
    try:
        project = pcm.update_project(
            project_id,
            name=body.get("name"),
            context=body.get("context"),
            vault_folder=body.get("vault_folder"),
            repo_path=repo_path,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if project is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(project.to_dict())


async def project_complete(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    project_id = request.path_params["project_id"]
    raw = await request.body()
    if raw:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
    else:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    outcome = body.get("outcome", "completed")
    note = body.get("note", "")
    if not isinstance(outcome, str) or not isinstance(note, str):
        return JSONResponse({"error": "outcome and note must be strings"}, status_code=400)
    try:
        result = pcm.complete_project(project_id, outcome=outcome, note=note)
    except RecoveryLifecycleConflict as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
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


async def repo_candidates(request: Request) -> JSONResponse:
    """Git checkouts the project Repository picker can offer. Read-only."""
    payload = await asyncio.to_thread(repo_link.candidates_payload)
    return JSONResponse(payload)


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
    if request.query_params.get("archived") == "1":
        if pcm.get_project(project_id) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            offset = int(request.query_params.get("offset", "0"))
            limit = int(request.query_params.get("limit", "10"))
        except ValueError:
            return JSONResponse({"error": "invalid pagination"}, status_code=400)
        if offset < 0 or not 1 <= limit <= 100:
            return JSONResponse({"error": "invalid pagination"}, status_code=400)
        archived = sorted(
            (chat for chat in chats if chat.archived),
            key=lambda chat: (chat.created_at, chat.chat_id),
            reverse=True,
        )
        return JSONResponse({
            "items": [
                {
                    "chat_id": chat.chat_id,
                    "title": chat.title,
                    "created_at": chat.created_at,
                    "archive_path": chat.archive_path,
                }
                for chat in archived[offset:offset + limit]
            ],
            "total": len(archived),
            "offset": offset,
            "limit": limit,
        })
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


async def chat_attachments_upload(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    chat = pcm.get_chat(request.path_params["chat_id"])
    if chat is None:
        return JSONResponse({"error": "chat not found"}, status_code=404)
    form = await request.form()
    saved: list[dict] = []
    errors: list[dict[str, str]] = []
    total_bytes = 0
    upload_count = 0
    for key in form:
        upload = form[key]
        if not hasattr(upload, "read"):
            continue
        # Counted per uploaded file: a PDF yields two refs (original + Markdown).
        if upload_count >= _DESKTOP_DROP_MAX_FILES:
            errors.append({"filename": "file", "error": "too many files"})
            break
        upload_count += 1
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
            for attach_path in entry["attach_paths"]:
                saved.append(_desktop_drop_ref(pcm, chat.chat_id, Path(attach_path)))
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
    the app that received the drop may read, so the shell that received it
    staged a copy first. When there is no staged copy, because the shell is
    older than that fix or the drop was not an image, the raw errno tells the
    user nothing they can act on.

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
        # here, so it needs no corroborating path check. The shell that
        # received the drop stages unreadable drops past this; a file over the
        # staging limit or an older shell still lands here. A non-image
        # dropped on this Mac does
        # not: the path is handed to the agent unread, so the agent hits the
        # same errno on its own.
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

        image_paths = [
            path
            for path in paths
            if path.is_file() and path.suffix.lower() in _ALLOWED_IMAGE_EXTENSIONS
        ]
        regular_paths = [path for path in paths if path not in image_paths]
        errors: list[dict[str, str]] = []
        file_refs: list[dict[str, str]] = []

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
                    data = await asyncio.to_thread(
                        _read_native_file_limited, path, chat_service._PROJECT_UPLOAD_MAX_BYTES
                    )
                    entry = await asyncio.to_thread(
                        pcm.save_chat_attachment_upload, project_id, data, path.name
                    )
                    for attach_path in entry["attach_paths"]:
                        file_refs.append(_desktop_drop_ref(pcm, chat_id, Path(attach_path)))
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
    helper = body.get("helper")
    if isinstance(helper, dict) and helper.get("kind") == "memory_pass":
        # A pass helper marks the chat as the engine's own housekeeping, and
        # that is what silences its notifications. The helper is the engine's
        # to write: a client that could set it could silence any chat it
        # created. Read on the raw kind, so a malformed forgery is refused
        # as well.
        return JSONResponse(
            {"error": "a memory pass helper is the engine's own"}, status_code=400
        )
    try:
        chat = pcm.create_chat(
            project_id,
            title=body.get("title", "New Chat"),
            model=body.get("model"),
            mode=body.get("mode"),
            provider=body.get("provider"),
            helper=helper,
        )
    except MemoryProjectRefused as exc:
        # The Memory project is app-owned: it holds the engine's own memory
        # passes, and an ordinary chat there would be silenced by the
        # project-kind half of the pass predicate without being one.
        return JSONResponse({"error": str(exc)}, status_code=400)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(chat.to_dict(local=True), status_code=201)


# ── Chats ────────────────────────────────────────────────────────────────

# The only top-level keys an ordinary `PATCH /api/chats/{id}` body may carry. A
# pin write is a dedicated body (`{"pin": {...}}`) and cannot bring any of them
# along: a pin is one shared selection per chat, so a combined write could land
# the title change and then conflict on the pin, leaving half the request
# applied. Checked by exclusion, so a new chat field does not silently become
# combinable with a pin.
_CHAT_PATCH_FIELDS = frozenset({
    "title", "model", "provider", "mode", "project_id", "thinking_level",
})


def _chat_pin_request(body: dict) -> tuple[str, int]:
    """Validate a `{"pin": {...}}` chat PATCH body. Returns ``(path, revision)``.

    Deliberately separate from `update_chat`: pins are one shared selection per
    chat with their own optimistic-concurrency revision, so they get their own
    parser rather than an extra optional argument on a function whose `None`
    already means "leave this field alone" — which is precisely the ambiguity an
    empty pin path would otherwise collide with.

    Raises ``ValueError`` with a caller-facing message for every malformed
    shape. The path itself is *not* resolved here: a nonempty path has to go
    through the viewer's exact resolver, and an empty one must not touch the
    filesystem at all so a deleted file can still be closed.
    """
    if set(body) - {"pin"}:
        raise ValueError(
            "pin cannot be combined with other chat updates "
            f"(ordinary fields: {', '.join(sorted(_CHAT_PATCH_FIELDS))})"
        )
    pin = body["pin"]
    if not isinstance(pin, dict):
        raise ValueError("pin must be an object")
    extra = set(pin) - {"path", "expected_revision"}
    if extra:
        raise ValueError(
            f"pin has unknown keys: {', '.join(sorted(extra))} "
            "(allowed: path, expected_revision)"
        )
    raw_path = pin.get("path")
    if not isinstance(raw_path, str):
        raise ValueError("pin.path must be a string")
    if any(unicodedata.category(c) == "Cc" for c in raw_path):
        # Control characters only (NUL included), matching the folder listing.
        # `str.isprintable()` would also refuse U+202F, the space macOS puts in
        # screenshot names, so a folder the browser lists could not be pinned.
        # The empty-path branch skips the resolver, so this check lives here.
        raise ValueError("pin.path contains characters that cannot be a path")
    revision = pin.get("expected_revision")
    # bool is an int subclass, so `True` must not be read as revision 1.
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
    ):
        raise ValueError("pin.expected_revision must be a non-negative integer")
    return raw_path.strip(), revision


def _resolve_pin_path(config, raw_path: str, *, agent_root: Path | None = None) -> Path | Response:
    """Resolve a nonempty manual pin path to the identity that gets stored.

    Reuses the viewer's resolver with fuzzy matching off. A pin is an identity
    key held for as long as the chat lives and matched exactly against later
    dismissals and agent surfaces, so a fuzzy hit would store a path nobody
    asked for. `_resolve_workspace_path` returns a real file only, so a
    directory is answered with its own 404 and needs no second check here.
    """
    roots = _allowed_roots(config)
    if agent_root is not None:
        roots = [agent_root, *roots]
    resolved = _resolve_workspace_path(roots, raw_path, allow_fuzzy=False)
    if isinstance(resolved, Response):
        return resolved
    if resolved.suffix.lower() not in _WORKSPACE_FILE_EXTS:
        # The panel renders the text viewer, so a pin has to name a file that
        # viewer can serve: same rule, same 415 as `/api/workspace/file`. This
        # reuses the existing viewer permissions rather than inventing a
        # sandbox or wider access for pins.
        return JSONResponse({"error": "unsupported type"}, status_code=415)
    return resolved


def _chat_memory_source(pcm: ProjectChatManager, chat: ChatInfo) -> dict[str, Any] | None:
    """The small source record Home needs for an open memory pass."""
    if chat.helper.get("kind") != "memory_pass":
        return None
    source = pcm.get_chat(str(chat.helper.get("source_chat_id") or ""))
    if source is None:
        return None
    return {
        "chat_id": source.chat_id,
        "title": source.title,
        "created_at": source.created_at,
        "last_activity_at": source.last_activity_at,
        "archive_path": source.archive_path,
    }


async def list_all_chats(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    # `?active_only=1` skips archived chats. The PWA's frequent syncLatest poll
    # uses it: the archive can be huge (thousands of rows) and is not needed to
    # keep the sidebar/home in sync — archived rows are already held locally and
    # loaded on demand. Filter BEFORE the per-chat `is_session_local` probe
    # (filesystem stat) so the poll never touches the archived rows at all.
    active_only = request.query_params.get("active_only") in {"1", "true"}
    if active_only:
        chats = [c for c in pcm.list_chats() if not c.archived]
        rows = []
        for chat in chats:
            row = chat.to_dict(local=True)
            source = _chat_memory_source(pcm, chat)
            if source is not None:
                row["memory_source"] = source
            rows.append(row)
        return JSONResponse(rows)
    return JSONResponse(pcm.list_chats_dicts())


async def search_chats(request: Request) -> JSONResponse:
    """Search bounded saved transcript text in one registered workspace."""
    if not _state_change_origin_allowed(request):
        return JSONResponse({"error": "forbidden origin"}, status_code=403)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 4096:
            return JSONResponse({"error": "request body too large"}, status_code=413,
                                headers={"Cache-Control": "no-store"})
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400,
                            headers={"Cache-Control": "no-store"})
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400,
                            headers={"Cache-Control": "no-store"})
    workspace, query, cursor = body.get("workspace"), body.get("q"), body.get("cursor")
    if (not isinstance(workspace, str) or not isinstance(query, str)
            or (cursor is not None and not isinstance(cursor, str))):
        return JSONResponse({"error": "workspace, q, and cursor must be text"}, status_code=400,
                            headers={"Cache-Control": "no-store"})
    query = query.strip()
    if not 2 <= len(query) <= 200:
        return JSONResponse({"error": "q must be 2 to 200 characters"}, status_code=400,
                            headers={"Cache-Control": "no-store"})
    if cursor is not None and (not cursor or len(cursor) > 2048):
        return JSONResponse({"error": "invalid cursor"}, status_code=400,
                            headers={"Cache-Control": "no-store"})
    config = request.app.state.config
    if config.workspace(workspace) is None:
        return JSONResponse({"error": "workspace not found"}, status_code=404,
                            headers={"Cache-Control": "no-store"})
    pcm = request.app.state.project_chat_manager
    try:
        result = await run_read(
            "chat-search:" + hashlib.sha256(
                json.dumps([workspace, query.casefold(), cursor], separators=(",", ":")).encode()
            ).hexdigest(),
            lambda: transcript_service.search_workspace_chats(pcm, workspace, query, cursor),
        )
    except ValueError:
        return JSONResponse({"error": "invalid cursor"}, status_code=400,
                            headers={"Cache-Control": "no-store"})
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


async def chat_detail(request: Request) -> Response:
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    if request.method == "GET":
        chat = pcm.get_chat(chat_id)
        if chat is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        row = chat.to_dict(local=True if chat.archived else pcm.is_session_local(chat))
        source = _chat_memory_source(pcm, chat)
        if source is not None:
            row["memory_source"] = source
        return JSONResponse(row)
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
        try:
            ok = pcm.delete_chat(chat_id)
        except RecoveryLifecycleConflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        logging.getLogger("ciao.web.routes").info("chat DELETE %s -> ok=%r", chat_id, ok)
        return JSONResponse({"ok": ok, "deleted": ok})
    # PATCH
    # The body is read once, here, and it has to be a JSON object before
    # anything else happens: a chat update is a set of named fields, and
    # a body that is malformed JSON, or JSON that is not an object, has
    # no fields to read. Refusing it here is what keeps a 500 out of a
    # request the engine was going to refuse anyway.
    #
    # Both refusals are static strings, and the raw body is never echoed:
    # it can carry a command, a credential or a private path, and an
    # error string ends up in a log line and a browser console.
    try:
        body = await request.json()
    except ValueError:
        # `json.JSONDecodeError` and a `UnicodeDecodeError` on a body
        # that is not UTF-8 are both `ValueError`s.
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    # A pin write is its own body shape, not another `update_chat` field
    # (#1118). It never falls through to `update_chat`, so `path: ""` cannot be
    # read as "no change" there.
    if "pin" in body:
        return await _chat_pin_patch(request, pcm, chat_id, body)
    # A model id is user-controlled and can reach a status line, so it is bounded as a
    # delegation's is. None leaves the chat's model alone.
    model = body.get("model")
    if isinstance(model, str) and len(model) > MAX_MODEL_ID_CHARS:
        return JSONResponse(
            {"error": f"model must be text of at most {MAX_MODEL_ID_CHARS} characters"},
            status_code=400,
        )
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


async def _chat_pin_patch(
    request: Request, pcm, chat_id: str, body: dict
) -> Response:
    """`PATCH /api/chats/{id}` with a `{"pin": {...}}` body (#1118).

    Sets or closes the chat's pinned file. Reuses the existing signed-session,
    origin-gated route: pins get no new endpoint and no auth exception.
    A path ending in a slash pins a folder instead (see the branch below).

    Ordering is deliberate. Shape first (400), then existence (404) *before*
    any filesystem probing, then the resolver, and only then the write: a
    nonexistent chat must not be answerable as "that file does not exist", and
    an invalid body must not cost a `resolve()`.

    The empty path never touches the filesystem, which is what lets a user
    close a pin whose file has since been deleted.
    """
    try:
        raw_path, expected_revision = _chat_pin_request(body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    if pcm.get_chat(chat_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    path = ""
    if raw_path:
        if raw_path.endswith(("/", "\\")):
            # A trailing slash pins a folder: confined to the roots the folder
            # browser lists (GET /api/workspace-dir?chat_id=), stored with one
            # trailing "/" so every client can tell it from a file pin.
            chat = pcm.get_chat(chat_id)  # non-None: checked above
            candidates = _chat_dir_root_candidates(request.app.state.config, pcm, chat)
            folder: tuple[Path, tuple[str, Path]] | Response = await asyncio.to_thread(
                lambda: _resolve_dir_target(_distinct_dir_roots(candidates), raw_path)
            )
            if isinstance(folder, Response):
                return folder
            posix = folder[0].as_posix()
            path = posix if posix.endswith("/") else f"{posix}/"
        else:
            resolved = _resolve_pin_path(
                request.app.state.config, raw_path, agent_root=pcm._agent_root_for_chat(chat_id)
            )
            if isinstance(resolved, Response):
                return resolved
            # Store the canonical absolute POSIX identity: relative and `~` forms
            # would not match a later surface or dismissal of the same file.
            path = resolved.as_posix()

    try:
        # Raises ChatPinConflictError before mutating anything when the quoted
        # revision is stale, so a 409 leaves the newer selection untouched.
        pcm.set_chat_pin(chat_id, path, expected_revision=expected_revision)
    except ChatPinConflictError as exc:
        return JSONResponse(
            {"error": "pin_revision_conflict", "pin": exc.pin}, status_code=409
        )
    except Exception as exc:
        # The write rolled back in the manager; report the failure rather than
        # a 200 for a pin that is not on disk.
        logger.exception("Failed to persist pin for chat %s", chat_id)
        return JSONResponse({"error": f"Failed to set pin: {exc}"}, status_code=500)
    chat = pcm.get_chat(chat_id)
    if chat is None:  # pragma: no cover — deleted between the checks above
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(chat.to_dict(local=pcm.is_session_local(chat)))


async def chat_new_session(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    try:
        chat = pcm.new_session(chat_id)
    except RecoveryLifecycleConflict as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
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
    if len(model) > MAX_MODEL_ID_CHARS:
        return JSONResponse(
            {"error": f"model must be text of at most {MAX_MODEL_ID_CHARS} characters"},
            status_code=400,
        )
    raw_messages = body.get("messages", [])
    messages = raw_messages if isinstance(raw_messages, list) else []
    try:
        chat = pcm.handover_chat(
            chat_id,
            provider=provider,
            model=model,
            messages=[m for m in messages if isinstance(m, dict)],
        )
    except RecoveryLifecycleConflict as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
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
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    action = str(body.get("action", "try_now"))
    request_id = body.get("request_id")
    if action == "issue_recovery_request":
        if set(body) != {"action"} or len(chat_id) > 128:
            return JSONResponse({"error": "invalid recovery request"}, status_code=400)
        try:
            record, _ = await asyncio.to_thread(pcm.recovery_request_record, chat_id, None)
        except RecoveryRequestChatNotFound:
            return JSONResponse({"error": "not found"}, status_code=404)
        except (OSError, ValueError):
            return JSONResponse({"status": "unknown"}, status_code=503)
        return JSONResponse({"request_id": record["request_id"], "expires": record["expires"]})
    if "request_id" in body or action == "recovery_request_status":
        if (len(chat_id) > 128 or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id)
                or action not in {"recovery_request_status", "continue_recovery", "continue_recovery_now"}):
            return JSONResponse({"error": "invalid recovery request"}, status_code=400)
        if action == "recovery_request_status":
            if set(body) != {"action", "request_id"}:
                return JSONResponse({"error": "invalid recovery request"}, status_code=400)
            intent = None
        else:
            failure_id = body.get("failure_id")
            if (set(body) != {"action", "failure_id", "request_id"} or not isinstance(failure_id, str)
                    or not failure_id or len(failure_id) > 128):
                return JSONResponse({"error": "invalid recovery request"}, status_code=400)
            intent = {"action": action, "failure_id": failure_id}
        # Cancellation cannot stop a worker thread. Retain the whole handler's
        # ownership until admission, effects, and terminal writes have all finished.
        operation = asyncio.create_task(_chat_recovery_request(request, body, intent))
        cancelled = False
        while True:
            try:
                response = await asyncio.shield(operation)
                break
            except asyncio.CancelledError:
                cancelled = True
                if operation.cancelled():
                    raise
        if cancelled:
            raise asyncio.CancelledError
        return response
    return await _chat_retry_action(request, body)


async def _chat_recovery_request(
    request: Request, body: dict[str, Any], intent: dict[str, str] | None,
) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    chat_id, request_id = request.path_params["chat_id"], body["request_id"]

    async def receipt(**kwargs: Any) -> tuple[dict[str, Any], bool]:
        # An executor Future (not a Task) also survives shutdown cancelling all
        # asyncio Tasks. Never close the ownership handle while its worker runs.
        pending = asyncio.get_running_loop().run_in_executor(
            None, functools.partial(pcm.recovery_request_record, chat_id, request_id, intent=intent, **kwargs)
        )
        cancelled = False
        result: tuple[dict[str, Any], bool]
        while True:
            try:
                result = await asyncio.shield(pending)
                break
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return result

    with contextlib.ExitStack() as lifetime:
        try:
            record, admitted = await receipt(lifetime=lifetime)
        except RecoveryRequestConflict:
            return JSONResponse({"error": "request ID conflict"}, status_code=409)
        except RecoveryRequestChatNotFound:
            return JSONResponse({"error": "not found", "request_id": request_id, "status": "unknown"}, status_code=404)
        except (OSError, ValueError):
            return JSONResponse({"request_id": request_id, "status": "unknown"}, status_code=503)
        if not admitted:
            status = record.get("status", "unknown")
            response = {"request_id": request_id, "status": status}
            if status == "completed":
                response["result"] = record.get("result")
            code = 503 if status == "unknown" else (200 if body["action"] == "recovery_request_status" or status == "completed" else (409 if status == "cancelled" else 202))
            return JSONResponse(response, status_code=code)
        try:
            inner = await _chat_retry_action(request, {key: value for key, value in body.items() if key != "request_id"})
            if inner.status_code >= 500:
                await receipt(outcome_unknown=True)
                return JSONResponse({"request_id": request_id, "status": "unknown"}, status_code=503)
            result = {"http_status": inner.status_code}
            record, _ = await receipt(result=result)
        except Exception:
            try:
                await receipt(outcome_unknown=True)
            except Exception:
                pass
            return JSONResponse({"request_id": request_id, "status": "unknown"}, status_code=503)
        return JSONResponse({"request_id": request_id, "status": record["status"], "result": record.get("result")})


async def _chat_retry_action(request: Request, body: dict[str, Any]) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    action = str(body.get("action", "try_now"))
    schedule_id = body.get("schedule_id")
    if "require_empty" in body and action != "set":
        return JSONResponse({"error": "require_empty is only valid for set"}, status_code=400)
    if "schedule_id" in body and (action not in {"stop", "try_now"} or not isinstance(schedule_id, str) or not schedule_id):
        return JSONResponse({"error": "invalid schedule_id"}, status_code=400)
    if action == "stop":
        status = pcm.recovery_status(chat_id)
        if status and status["mode"] == "continue_recovery" and status["status"] == "pending":
            return JSONResponse({"error": "use stop_recovery with the observed recovery_id"}, status_code=409)
        try:
            chat = pcm.stop_chat_retry(chat_id, allow_recovery=False, **({"schedule_id": schedule_id} if schedule_id is not None else {}))
        except ValueError:
            return JSONResponse({"error": "recovery is in flight or changed"}, status_code=409)
    elif action == "stop_recovery":
        recovery_id = body.get("recovery_id")
        if set(body) != {"action", "recovery_id"} or not isinstance(recovery_id, str) or not recovery_id:
            return JSONResponse({"error": "only action and recovery_id are accepted"}, status_code=400)
        if not pcm.stop_recovery(chat_id, recovery_id):
            return JSONResponse({"error": "recovery is in flight, changed, or not pending"}, status_code=409)
        return JSONResponse({"stopped": True})
    elif action == "recovery_status":
        if set(body) != {"action"}:
            return JSONResponse({"error": "only action is accepted"}, status_code=400)
        try:
            status = pcm.recovery_status(chat_id)
        except (OSError, ValueError):
            return JSONResponse({"error": "recovery status unavailable"}, status_code=503)
        if status is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(status)
    elif action == "set":
        if "require_empty" in body and body["require_empty"] is not True:
            return JSONResponse({"error": "require_empty must be true"}, status_code=400)
        prompt = str(body.get("prompt", ""))
        if body.get("require_empty") is True and not prompt.strip():
            return JSONResponse({"error": "prompt is required"}, status_code=400)
        images = [str(x) for x in body.get("images", []) if str(x)]
        chat = pcm.set_chat_retry(chat_id, prompt, image_refs=images, reason="manual",
                                  **({"require_empty": True} if body.get("require_empty") is True else {}))
        if chat is None and body.get("require_empty") is True and pcm.get_chat(chat_id) is not None:
            return JSONResponse({"error": "retry slot is not empty or unavailable"}, status_code=409)
    elif action in {"continue_recovery", "continue_recovery_now"}:
        failure_id = body.get("failure_id")
        if set(body) != {"action", "failure_id"} or not isinstance(failure_id, str) or not failure_id:
            return JSONResponse({"error": "only action and failure_id are accepted"}, status_code=400)
        chat = pcm.get_chat(chat_id)
        if chat is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if chat.failure_id != failure_id or not chat.session_id or not chat.last_error or chat.last_response_status != "error":
            return JSONResponse({"error": "failure or session changed, chat is busy, or a retry is pending"}, status_code=409)
        if chat.retry_status == "pending":
            if action != "continue_recovery_now" or not pcm._valid_recovery_retry(chat) or chat.retry_recovery == "":
                return JSONResponse({"error": "retry already pending"}, status_code=409)
        else:
            try:
                armed = pcm.schedule_continue_recovery(chat_id, chat.session_id, chat.last_error, chat.user_turn_count, failure_id, manual=action == "continue_recovery_now")
            except RecoveryOutcomeUnknown:
                return JSONResponse({"error": "recovery outcome unknown", "code": "recovery_outcome_unknown"}, status_code=503)
            if not armed:
                return JSONResponse({"error": "failure or session changed, chat is busy, or a retry is pending"}, status_code=409)
        if action == "continue_recovery_now" and pcm.try_chat_retry_now(chat_id) is None:
            return JSONResponse({"error": "recovery not started"}, status_code=409)
    elif action == "try_now":
        stream = pcm.try_chat_retry_now(chat_id, **({"schedule_id": schedule_id} if schedule_id is not None else {}))
        chat = pcm.get_chat(chat_id)
        if chat is not None and stream is None and (schedule_id is not None or chat.retry_status == "pending"):
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


async def chat_compact(request: Request) -> JSONResponse:
    """Compact the chat's provider session to shrink its context window.

    Runs as a stream like any turn, so the PWA sees the same status and result
    events. A chat with no provider session yet has nothing to compact, and a
    chat with a live user turn must wait for it to finish.
    """
    from ciao.models import COMPACT_COMMAND

    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    chat = pcm.get_chat(chat_id)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if chat.archived:
        return JSONResponse(
            {"error": "chat is archived", "archived": True}, status_code=409
        )
    active = pcm.get_active_stream(chat_id)
    # A between-turns drain is background work that a real run replaces.
    if active is not None and not active.background:
        return JSONResponse({"error": "chat is busy"}, status_code=409)
    if not chat.session_id:
        return JSONResponse({"error": "nothing to compact yet"}, status_code=409)

    try:
        pcm.start_stream(chat_id, COMPACT_COMMAND, compact=True)
    except ValueError as exc:
        # An open question the user has not answered yet.
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception as exc:
        logger.exception("Failed to start compaction for %s", chat_id)
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
    # (it flips ``archived=True`` but leaves project_id intact).
    chat_meta = pcm.get_chat(chat_id)
    project_meta = (
        pcm.get_project(chat_meta.project_id) if chat_meta is not None else None
    )
    try:
        outcome = await pcm.archive_chat(chat_id)
    except RecoveryLifecycleConflict as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    if outcome is not None:
        await pcm.run_archive_postprocess(chat_id, outcome, chat_meta, project_meta)
    return JSONResponse({
        "ok": True,
        "archived_to": str(outcome.path) if outcome is not None else None,
        # The initiating client clears the active pane as soon as this response
        # arrives. Return the memory-pass record as well as publishing it over
        # /ws/events, so that client cannot miss it in the archive/event race.
        "postprocess": (
            dict(chat_meta.postprocess)
            if chat_meta and chat_meta.postprocess
            else None
        ),
    })


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
    ACP chats, opencode included, read the session history from their session journal. Both
    fall back to the durable ``.runtime`` transcript when the provider-side session
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
    if _request_params(request).get("offset") in (None, "", "0"):
        pcm.schedule_repo_location_refresh(chat_id)
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


async def chat_worktree_changes(request: Request) -> Response:
    """Return one bounded, read-only Git snapshot for this authenticated chat."""
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    if pcm.get_chat(chat_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404,
                            headers={"Cache-Control": "no-store"})
    try:
        result = await pcm.worktree_changes_for_chat(chat_id)
    except Exception:
        return JSONResponse({"error": "unable to read checkout changes"}, status_code=503,
                            headers={"Cache-Control": "no-store"})
    if result is None:
        return JSONResponse({"error": "not found"}, status_code=404,
                            headers={"Cache-Control": "no-store"})
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


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
    ".css", ".html", ".htm", ".json",
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




def _is_same_chat_target(requested: str, found: Path) -> bool:
    """Whether a fuzzy match is the file a chat link named.

    Fuzzy search may drop leading directories or the extension, which can pick
    `scripts/report.py` for a missing `report.md`. A match is accepted only
    when it has the requested basename, or when its trailing path parts equal
    the requested ones. Case is ignored in both comparisons.
    """
    wanted = Path(requested)
    if found.name.lower() == wanted.name.lower():
        return True
    count = len(wanted.parts)
    if count > len(found.parts):
        return False
    return [p.lower() for p in found.parts[-count:]] == [p.lower() for p in wanted.parts]


def _resolve_chat_file(roots: list[Path], raw: str, *, fuzzy: bool = True) -> Path | Response:
    """Resolve a chat link against the chat's own roots only.

    Each root is tried on its own, exact matches before fuzzy ones, so a
    fuzzy match can only land inside a root the chat owns. Passing the roots
    together would let relative-path fuzzy search reach just the first one,
    and a single-root call per attempt keeps every search inside its root.
    A fuzzy match is kept only if it is the requested file (see
    ``_is_same_chat_target``). A root nested inside an earlier root is skipped
    for the fuzzy pass, since that earlier root's search already covers it.
    """
    for root in roots:
        result = _resolve_workspace_path([root], raw, allow_fuzzy=False)
        if not isinstance(result, Response) or result.status_code != 404:
            return result
    if not fuzzy:
        return JSONResponse({"error": "not found"}, status_code=404)
    requested = _LINE_SUFFIX_RE.sub("", raw)
    fuzzy_roots = [
        root for index, root in enumerate(roots)
        if not any(root.is_relative_to(earlier) for earlier in roots[:index])
    ]
    for root in fuzzy_roots:
        found = _resolve_workspace_path([root], raw, allow_fuzzy=True)
        if isinstance(found, Path):
            if _is_same_chat_target(requested, found):
                return found
        elif found.status_code != 404:
            return found
    return JSONResponse({"error": "not found"}, status_code=404)


async def _resolve_in_chat(
    request: Request, chat_id: str, *, fuzzy: bool = True
) -> Path | Response:
    """Resolve the request's ``path`` the way *chat_id*'s file links resolve.

    A relative path anchors to the chat's agent root, then its workspace vault
    root, and fuzzy matching (when *fuzzy*) stays inside those roots. This is
    anchoring, not confinement: an absolute or ``..`` path follows the viewers'
    existing no-sandbox policy (``_resolve_workspace_path``). ``fuzzy=False``
    skips the directory walk and answers 404 when no exact match exists.
    """
    pcm = request.app.state.project_chat_manager
    config = request.app.state.config
    if pcm.get_chat(chat_id) is None:
        return JSONResponse({"error": "chat not found"}, status_code=404)
    workspace = pcm._workspace_for_chat(chat_id)
    roots = list(dict.fromkeys([
        config.agent_root(workspace).resolve(),
        config.workspace_vault_root(workspace).resolve(),
    ]))
    # The fuzzy pass walks the directory tree, so it runs off the event loop.
    return await asyncio.to_thread(
        _resolve_chat_file, roots, request.query_params.get("path", "").strip(), fuzzy=fuzzy
    )


async def chat_file_path(request: Request) -> Response:
    """Canonical file identity for a link emitted in a chat's workspace.

    Resolves against the chat's agent root and its workspace vault root, and
    nothing else. A missing file must not match a same-named file in another
    workspace, so fuzzy matching is confined to those two roots. Absolute
    paths retain the viewer's existing policy.
    """
    result = await _resolve_in_chat(request, request.path_params["chat_id"])
    if isinstance(result, Response):
        return result
    extensions = _WORKSPACE_FILE_EXTS | _WORKSPACE_IMAGE_EXTS | _WORKSPACE_BINARY_EXTS
    if result.suffix.lower() not in extensions:
        return JSONResponse({"error": "unsupported type"}, status_code=415)
    return JSONResponse({"path": result.as_posix()})


# Browser-only discovery of files a completed reply mentions (#2118). Exact
# lookups inside the owning workspace's own roots: no fuzzy search, no content
# read, and no fallback to the primary workspace or the install root.
FILE_MENTION_MAX_BODY_BYTES = request_limits.FILE_MENTION_BODY_LIMIT
FILE_MENTION_MAX_PATHS = 50
FILE_MENTION_MAX_PATH_CHARS = 4096
_FILE_MENTION_MAX_LINE = 2147483647
_FILE_MENTION_LINE_RE = re.compile(r"(.+):([0-9]+)")
_FILE_MENTION_LOCATION_KEYS = ("path", "exists", "is_worktree", "branch")


@dataclass(frozen=True)
class _FileMentionContext:
    """The roots one batch may search, and the layout they were derived from.

    A batch's rows are returned only when a fresh snapshot compares equal, so any
    change to membership, roots, layout or repo state during the lookup nulls them.
    """

    roots: tuple[Path, ...]
    identity: tuple[object, ...]


def _no_store_json(content: object, status: int = 200) -> JSONResponse:
    return JSONResponse(content, status_code=status, headers={"Cache-Control": "no-store"})


def _valid_mention_string(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > FILE_MENTION_MAX_PATH_CHARS:
        return False
    # Printable means "no control character or lone surrogate". str.isprintable()
    # would also refuse U+202F, which macOS puts in screenshot names.
    return all(
        not 0xD800 <= ord(char) <= 0xDFFF and unicodedata.category(char) != "Cc"
        for char in value
    )


async def _read_file_mention_request(request: Request) -> list[str] | Response:
    """The validated `paths` list, or the error response for the whole body."""
    body = await request.body()
    if len(body) > FILE_MENTION_MAX_BODY_BYTES:
        return _no_store_json({"error": "request body too large"}, 413)
    try:
        payload = loads_untrusted(body)
    except (ValueError, RecursionError):
        return _no_store_json({"error": "bad json"}, 400)
    if not isinstance(payload, dict) or set(payload) != {"paths"}:
        return _no_store_json({"error": "invalid request"}, 400)
    paths = payload["paths"]
    if not isinstance(paths, list) or not all(_valid_mention_string(item) for item in paths):
        return _no_store_json({"error": "invalid request"}, 400)
    if len(paths) > FILE_MENTION_MAX_PATHS:
        return _no_store_json({"error": "too many paths"}, 413)
    return paths


def _owning_mention_roots(config: CiaoConfig, name: str) -> tuple[Path, ...] | None:
    """The canonical roots one registered workspace may search, or None (fail closed).

    The agent root and the nested vault both count.
    """
    try:
        vault = config.workspace_vault_root(name)
        agent = config.agent_root(name)
        roots: list[Path] = []
        for raw in [agent, vault]:
            if raw.is_symlink():
                return None
            canon = raw.resolve(strict=True)
            if not canon.is_dir() or canon == Path(canon.anchor):
                return None
            roots.append(canon)
        install = config.workspace_root.resolve(strict=True)
        for other in config.workspaces:
            if other == name:
                continue
            other_vault = config.workspace_vault_root(other).resolve(strict=True)
            other_agent = config.agent_root(other).resolve(strict=True)
            for root in roots:
                if root == other_vault or other_vault.is_relative_to(root):
                    return None
                if root in (other_agent, other_vault):
                    return None
        return tuple(dict.fromkeys(roots))
    except (OSError, RuntimeError, ValueError):
        return None


def _mention_protected_roots(config: CiaoConfig) -> tuple[Path, ...] | None:
    """The install root and every registered workspace's agent root and vault.

    A linked repo may not equal or contain any of them. None when one cannot be
    resolved, which fails the repo closed.
    """
    try:
        protected = [config.workspace_root.resolve(strict=True)]
        for name in config.workspaces:
            protected.append(config.agent_root(name).resolve(strict=True))
            protected.append(config.workspace_vault_root(name).resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return None
    return tuple(protected)


def _mention_repo_root(
    repo_path: str,
    recorded: bool,
    repo_now: dict[str, Any],
    protected: tuple[Path, ...] | None,
) -> Path | None:
    """The linked repo's root for this batch, or None when it is unavailable."""
    if not repo_path or protected is None:
        return None
    if recorded:
        # A recorded worktree that is gone or belongs elsewhere is unavailable.
        # Never substitute the main checkout for it.
        if not repo_now.get("exists"):
            return None
        raw = str(repo_now.get("path") or "")
    else:
        raw = repo_path
    path = Path(raw)
    if not raw or not path.is_dir():
        return None
    canon = path.resolve()
    if canon == Path(canon.anchor):
        return None
    # A repo that equals or contains another root would expose that root's files.
    if any(item.is_relative_to(canon) for item in protected):
        return None
    return canon


def _file_mention_identity(
    config: CiaoConfig,
    chat_id: str,
    workspace: str | None,
    repo_path: str,
    recorded: bool,
    location: tuple[object, ...],
    repo_now: dict[str, Any],
) -> _FileMentionContext:
    registered = tuple(sorted((name, item.vault_root) for name, item in config.workspaces.items()))
    head: tuple[object, ...] = (
        chat_id,
        workspace,
        registered,
        str(config.workspace_root),
        str(config.vault_root),
        str(config.state_path.parent),
        repo_path,
        location,
        tuple(repo_now.get(key) for key in _FILE_MENTION_LOCATION_KEYS),
    )
    if not workspace or config.workspace(workspace) is None:
        return _FileMentionContext((), (*head, "unavailable"))
    owning = _owning_mention_roots(config, workspace)
    if owning is None:
        return _FileMentionContext((), (*head, "unavailable"))
    repo = _mention_repo_root(repo_path, recorded, repo_now, _mention_protected_roots(config))
    roots = tuple(dict.fromkeys([*owning, *([repo] if repo is not None else [])]))
    return _FileMentionContext(roots, (*head, roots))


async def _snapshot_file_mention_context(request: Request, chat_id: str) -> _FileMentionContext | None:
    """The batch's roots and layout, or None when the chat does not exist."""
    pcm = request.app.state.project_chat_manager
    config = request.app.state.config
    chat = pcm.get_chat(chat_id)
    if chat is None:
        return None
    project = pcm.get_project(chat.project_id)
    # The repo's is_dir/resolve are filesystem work, so they stay off the event loop.
    repo_path = await asyncio.to_thread(pcm.linked_repo_for_chat, chat)
    location = dict(chat.repo_location or {})
    recorded = bool(location.get("path"))
    repo_now: dict[str, Any] = {}
    if repo_path and recorded:
        # One repo-identity check per snapshot. Its git common-dir match is enforced inside.
        repo_now = await resolve_chat_location(repo_path=repo_path, hints=[], previous=location)
    return await asyncio.to_thread(
        _file_mention_identity,
        config,
        chat.chat_id,
        project.workspace if project is not None else None,
        repo_path,
        recorded,
        tuple(location.get(key) for key in _FILE_MENTION_LOCATION_KEYS),
        dict(repo_now),
    )


def _strip_mention_line(raw: str) -> str | None:
    """`raw` without a final valid `:line`; None when a `:line` suffix is malformed."""
    match = _FILE_MENTION_LINE_RE.fullmatch(raw)
    # A bare drive letter ("C:17") is a drive, not a line suffix.
    if match is None or re.fullmatch(r"[A-Za-z]", match.group(1)):
        return raw
    digits = match.group(2)
    if digits[0] == "0" or not 1 <= int(digits) <= _FILE_MENTION_MAX_LINE:
        return None
    return match.group(1)


def _absolute_mention_candidate(roots: tuple[Path, ...], path: Path) -> set[Path]:
    try:
        canon = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return set()
    return {canon} if any(canon.is_relative_to(root) for root in roots) else set()


def _mention_candidates(roots: tuple[Path, ...], target: str) -> set[Path]:
    """Canonical paths `target` names exactly, one per root that confines them."""
    if target == "~" or target.startswith(("~/", "~\\")):
        return _absolute_mention_candidate(roots, Path.home() / target[2:])
    if target.startswith(("~", "\\\\", "//")):
        # `~otheruser`, UNC, device and network-share forms are never looked up.
        return set()
    if re.match(r"[A-Za-z]:([\\/]|$)", target):
        # A drive path names no file on this machine.
        return set()
    path = Path(target)
    if path.is_absolute():
        return _absolute_mention_candidate(roots, path)
    found: set[Path] = set()
    for root in roots:
        try:
            canon = (root / path).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if canon.is_relative_to(root):
            found.add(canon)
    return found


def _mention_file_path(canon: Path) -> str | None:
    """The canonical path if it is a regular file the viewers serve, within its size cap."""
    suffix = canon.suffix.lower()
    if suffix in _WORKSPACE_FILE_EXTS:
        cap = _WORKSPACE_FILE_MAX_BYTES
    elif suffix in _WORKSPACE_IMAGE_EXTS:
        cap = _WORKSPACE_IMAGE_MAX_BYTES
    elif suffix in _WORKSPACE_BINARY_EXTS:
        cap = _WORKSPACE_BINARY_MAX_BYTES
    else:
        return None
    info = canon.stat()
    if not S_ISREG(info.st_mode) or info.st_size > cap:
        return None
    return canon.as_posix()


def _resolve_one_file_mention(roots: tuple[Path, ...], raw: str) -> str | None:
    try:
        target = _strip_mention_line(raw)
        if target is None:
            return None
        found = _mention_candidates(roots, target)
        if len(found) != 1:
            # No match, or distinct exact targets (ambiguous): both are null.
            return None
        (canon,) = found
        return _mention_file_path(canon)
    except (OSError, ValueError, RuntimeError):
        return None


def _resolve_file_mention_batch(roots: tuple[Path, ...], paths: list[str]) -> list[dict[str, str | None]]:
    resolved: dict[str, str | None] = {}
    rows: list[dict[str, str | None]] = []
    for raw in paths:
        if raw not in resolved:
            resolved[raw] = _resolve_one_file_mention(roots, raw)
        rows.append({"input": raw, "path": resolved[raw]})
    return rows


async def resolve_chat_file_paths(request: Request) -> Response:
    """Exact canonical paths for the files a completed chat reply mentions (#2118).

    Browser-only and read-only: each input gets its canonical path or null. A
    response never carries content, reasons, stat data or possible matches.
    """
    chat_id = request.path_params["chat_id"]
    paths = await _read_file_mention_request(request)
    if isinstance(paths, Response):
        return paths
    snapshot = await _snapshot_file_mention_context(request, chat_id)
    if snapshot is None:
        return _no_store_json({"error": "chat not found"}, 404)
    rows = await asyncio.to_thread(_resolve_file_mention_batch, snapshot.roots, paths)
    fresh = await _snapshot_file_mention_context(request, chat_id)
    if fresh is None:
        return _no_store_json({"error": "chat not found"}, 404)
    if fresh != snapshot:
        rows = [{"input": raw, "path": None} for raw in paths]
    return _no_store_json({"results": rows})


# The folder browser's listing. Unlike the file viewers above, browsing is
# confined to named roots: a listing route that followed any absolute path
# would let a session walk the whole disk one folder at a time.
_WORKSPACE_DIR_MAX_ENTRIES = 2000


def _distinct_dir_roots(candidates: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    """Resolve each candidate root; drop missing ones and any nested in an earlier root."""
    kept: list[tuple[str, Path]] = []
    for name, raw in candidates:
        try:
            root = raw.expanduser().resolve()
        except (OSError, ValueError):
            continue
        if not root.is_dir():
            continue
        if any(root == prev or root.is_relative_to(prev) for _, prev in kept):
            continue
        kept.append((name, root))
    return kept


def _resolve_dir_target(
    roots: list[tuple[str, Path]], raw: str
) -> tuple[Path, tuple[str, Path]] | Response:
    """Resolve and classify a folder path against the browsable roots; shared by the listing and folder pins."""
    if any(unicodedata.category(c) == "Cc" for c in raw):
        return JSONResponse({"error": "bad path"}, status_code=400)
    raw = _LINE_SUFFIX_RE.sub("", raw.strip())
    if not roots:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        if raw == "":
            target = roots[0][1]
        else:
            candidate = Path(raw).expanduser()
            if candidate.is_absolute():
                target = candidate.resolve()
            else:
                target = (roots[0][1] / candidate).resolve()
                for _, root in roots:
                    if (root / candidate).exists():
                        target = (root / candidate).resolve()
                        break
        if target.is_file():
            return JSONResponse(
                {"error": "not_a_directory", "kind": "file", "path": target.as_posix()},
                status_code=400,
            )
        if not target.is_dir():
            return JSONResponse({"error": "not found"}, status_code=404)
    except (OSError, ValueError):
        return JSONResponse({"error": "bad path"}, status_code=400)
    owner = next(((n, r) for n, r in roots if target == r or target.is_relative_to(r)), None)
    if owner is None:
        return JSONResponse({"error": "outside_roots"}, status_code=403)
    return target, owner


def _list_workspace_dir(roots: list[tuple[str, Path]], raw: str) -> dict[str, Any] | Response:
    """Resolve ``raw`` against the roots and list it, or return the error response."""
    resolved = _resolve_dir_target(roots, raw)
    if isinstance(resolved, Response):
        return resolved
    target, owner = resolved
    entries: list[dict[str, Any]] = []
    try:
        with os.scandir(target) as scanned:
            children = list(scanned)
    except OSError:
        return JSONResponse({"error": "unreadable"}, status_code=403)
    for entry in children:
        if entry.name.startswith("."):
            continue
        try:
            is_dir = entry.is_dir()
            st = entry.stat()
        except OSError:  # broken symlink or vanished entry
            continue
        entries.append({
            "name": entry.name,
            "path": (target / entry.name).as_posix(),
            "type": "dir" if is_dir else "file",
            "size": None if is_dir else st.st_size,
            "mtime": int(st.st_mtime),
            "ext": "" if is_dir else Path(entry.name).suffix.lower().lstrip("."),
        })
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].casefold(), e["name"]))
    limit = _WORKSPACE_DIR_MAX_ENTRIES
    return {
        "path": target.as_posix(),
        "root": {"name": owner[0], "path": owner[1].as_posix()},
        "roots": [{"name": n, "path": r.as_posix()} for n, r in roots],
        "parent": None if target == owner[1] else target.parent.as_posix(),
        "entries": entries[:limit],
        "truncated": len(entries) > limit,
    }


def _chat_dir_root_candidates(config, pcm, chat) -> list[tuple[str, Path]]:
    """The chat's browsable roots: its agent root, workspace vault and linked repo.

    Shared by the folder listing and folder pins, so the tile can never pin a
    folder the browser would refuse to list.
    """
    return _dir_root_candidates(
        config, pcm._workspace_for_chat(chat.chat_id), pcm.get_project(chat.project_id)
    )


def _dir_root_candidates(config, workspace: str, project) -> list[tuple[str, Path]]:
    """A workspace's agent root and vault, plus the project's linked repo."""
    try:
        agent_root = config.agent_root(workspace)
    except ValueError:  # no registered workspace name
        agent_root = config.workspace_root
    candidates: list[tuple[str, Path]] = [("workspace", agent_root)]
    try:
        candidates.append(("vault", config.workspace_vault_root(workspace)))
    except ValueError:  # unresolvable vault: browse without it
        pass
    if project is not None and project.repo_path:
        candidates.append(("repo", Path(project.repo_path)))
    return candidates


async def workspace_dir(request: Request) -> Response:
    """Read-only folder listing for the PWA folder browser (`GET /api/workspace-dir`).

    With a chat or project, an empty path starts in the project's vault folder
    (where its work lives) rather than the workspace root.
    """
    config = request.app.state.config
    chat_id = request.query_params.get("chat_id", "").strip()
    project_id = request.query_params.get("project_id", "").strip()
    raw = request.query_params.get("path", "")
    candidates: list[tuple[str, Path]]
    home: Path | None = None
    if chat_id or project_id:
        pcm = request.app.state.project_chat_manager
    if chat_id:
        chat = pcm.get_chat(chat_id)
        if chat is None:
            return JSONResponse({"error": "chat not found"}, status_code=404)
        candidates = _chat_dir_root_candidates(config, pcm, chat)
        if chat.project_id:
            home = pcm.active_project_vault_dir(chat.project_id)
    elif project_id:
        project = pcm.get_project(project_id)
        if project is None:
            return JSONResponse({"error": "project not found"}, status_code=404)
        workspace = project.workspace
        if not pcm._is_known_workspace(workspace):
            workspace = config.primary_workspace()
        candidates = _dir_root_candidates(config, workspace, project)
        home = pcm.active_project_vault_dir(project_id)
    else:
        allowed = _allowed_roots(config)
        candidates = [("workspace", allowed[0])] + [("vault", r) for r in allowed[1:2]]
    roots = _distinct_dir_roots(candidates)
    if not raw.strip() and home is not None:
        raw = home.as_posix()
    result = await asyncio.to_thread(_list_workspace_dir, roots, raw)
    if isinstance(result, Response):
        return result
    return JSONResponse(result)


async def system_prompt(request: Request) -> Response:
    """The Ciaobot core every provider receives in its context capsule (read-only)."""
    text = unified_core()
    return JSONResponse({"text": text, "chars": len(text)})


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


async def workspace_file_path(request: Request) -> Response:
    """Canonical absolute identity of a path the standalone viewer opens.

    The file viewer anchors a relative path to `workspace_root` (the install
    root), while a chat pin anchors it to the chat's agent root. A caller that
    holds a viewer path (the care report, the workspace guide, the viewer's own
    open file) and has to pin it resolves it here first, with the viewer's own
    resolver and no fuzzy matching, and pins the absolute result.
    """
    config = request.app.state.config
    raw = request.query_params.get("path", "").strip()
    result = _resolve_workspace_path(_allowed_roots(config), raw, allow_fuzzy=False)
    if isinstance(result, Response):
        return result
    if result.suffix.lower() not in _WORKSPACE_FILE_EXTS:
        return JSONResponse({"error": "unsupported type"}, status_code=415)
    return JSONResponse({"path": result.as_posix()}, headers={"Cache-Control": "no-store"})


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

# Error responses from workspace_html are rendered inside the same Preview
# iframe as the artifact, so they must be frameable, and must carry no script.
# This is a separate constant from _ARTIFACT_CSP on purpose.
_ARTIFACT_ERROR_CSP = "; ".join(
    [
        "default-src 'none'",
        "style-src 'unsafe-inline'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'self'",
        "sandbox",
    ]
)

_ARTIFACT_ERROR_MESSAGES: dict[int, str] = {
    400: "No file path was given.",
    403: "This file is outside the folders Ciaobot can show.",
    404: "File not found.",
    415: "Only .html and .htm files can be previewed.",
}


def _artifact_error_page(status_code: int, message: str) -> Response:
    """Frameable HTML error for the artifact Preview iframe (same status)."""
    body = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        "<title>Preview unavailable</title>"
        "<style>body{font-family:system-ui,sans-serif;font-size:14px;"
        "color:#9aa0c0;background:transparent;padding:16px;margin:0}</style>"
        f"</head><body><p>{html.escape(message)}</p></body></html>"
    )
    return Response(
        content=body,
        status_code=status_code,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": _ARTIFACT_ERROR_CSP,
            "X-Frame-Options": "SAMEORIGIN",
            "Cache-Control": "no-cache",
        },
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
    history, or has history but refuses to render. Over the cap, and on every
    other error, the response is a small frameable HTML page
    (``_artifact_error_page``) with the same status, so the Preview frame shows
    why instead of the browser's blocked-frame page.

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
        return _artifact_error_page(
            result.status_code,
            _ARTIFACT_ERROR_MESSAGES.get(
                result.status_code, f"Preview failed (HTTP {result.status_code})."
            ),
        )
    resolved = result

    if resolved.suffix.lower() not in _WORKSPACE_HTML_EXTS:
        return _artifact_error_page(415, _ARTIFACT_ERROR_MESSAGES[415])
    size = resolved.stat().st_size
    if size > _WORKSPACE_FILE_MAX_BYTES:
        return _artifact_error_page(
            413,
            f"This page is {size / (1024 * 1024):.1f} MB, over the 2 MB preview limit. "
            "Use the ⋯ menu → Download to open it in your browser.",
        )

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
                display = resolved.relative_to(workspace).as_posix()
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
                display = resolved.relative_to(workspace).as_posix()
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


# How many overdue entries one Memory Map node names inline. The counts beside
# them are uncapped, so the cap costs the reader nothing but a "+N more" — and
# the alternative is a node card the height of a note with fifty overdue
# bullets, inside a graph that is a picture of the whole vault.
_MAP_ENTRY_FINDINGS = 3


def _vault_graph_payload(config: Any, workspace: str | None) -> dict[str, Any]:
    """Everything vault_graph returns, built off the event loop."""
    # Every vault in the install: one per agent root. Scanning `config.vault_root`
    # would return zero notes.
    from ciao.fts_search import ConnectedRegistration, lookup_connected_entries

    registration = ConnectedRegistration.capture(Path(config.state_path).parent)
    targets = config.vault_scan_targets()
    # Each target IS one workspace, so a `?workspace=`
    # request can drop the other roots before the scan instead of reading and
    # parsing every note in the install only to filter them out below — the
    # scan is the whole cost of this route.
    in_scope = [t for t in targets if workspace and t[1] == workspace]
    scan_list = in_scope or targets
    # Reads and parses every markdown file. The caller threads the whole
    # payload, so this scan runs off the event loop with the rest of it (a
    # large vault otherwise stalls the 5s chat-socket keepalives too).
    registration.verify()
    connected = {}
    for root, name, _prefix in scan_list:
        if name and not config.ciao_owned_vault_index(name):
            try:
                connected[name] = lookup_connected_entries(
                    root, name, Path(config.state_path).parent, Path(config.workspace_root),
                    source_roots=config.index_source_roots(), registration=registration, vault_base=config.vault_root,
                )
            except OSError as exc:
                logger.warning("Cannot build the local index for %s: %s", name, exc)
                connected[name] = []
    entries, absolute = scan_targets(scan_list, connected=connected)
    if in_scope:
        # The picker lists every workspace, and this scan only saw one. Taken
        # from the targets whose vault exists, which is the same set the full
        # scan would have produced entries for.
        workspaces = sorted(ws for root, ws, _ in targets if ws and (connected.get(ws) or Path(root).is_dir()))
    else:
        workspaces = sorted({e.workspace for e in entries if e.workspace})
    scoped = filter_entries(entries, workspace=workspace) if workspace else entries
    graph = _build_graph(scoped)
    by_path = {e.path_key for e in scoped}

    # `mtime` lets the Memory Map seed its local view from the note you touched
    # most recently, which is a far more useful entry point than "whatever the
    # biggest hub is". Entry carries no timestamp, so stat the files here; it is
    # one stat per note against files scan_vault has just read anyway.
    mtimes: dict[str, float] = {}

    def _mtime(rel: str) -> float:
        # Resolved through the scan's own map. Rendered paths are no longer a
        # fixed offset from one vault root, so stripping a `memory-vault/`
        # prefix and joining resolved to nothing on a migrated install and every
        # note reported mtime 0 — which silently broke the map's "most recently
        # touched note" entry point rather than failing loudly.
        if rel in mtimes:
            return mtimes[rel]
        target = absolute.get(rel)
        if target is None:
            mtimes[rel] = 0.0
            return 0.0
        try:
            mtimes[rel] = target.stat().st_mtime
        except OSError:
            # A note indexed but unreadable (race with a delete, broken
            # symlink) must not fail the whole graph request.
            mtimes[rel] = 0.0
        return mtimes[rel]

    # Aging uses the one predicate the audit and ``note related`` also use. Notes
    # that are bookkeeping (Workspace/ files, templates, completed projects) and
    # exempt types (logs, journals) keep their age but are never flagged.
    from ciao.memory_audit import never_aged, note_verification

    current_date = datetime.now(UTC).date()

    def _entry_coverage_for(e) -> dict[str, Any] | None:
        """One node's per-entry freshness, from the audit's own detector.

        ``None`` for a node whose body could not be read, which is the honest
        answer: this node cannot say what is inside the note, and a coverage
        figure of zero would read as "nothing to check".

        The counts come from :func:`ciao.memory_audit.note_entry_coverage`, the
        same function the care run's ``stale_entry`` pass and ``os-audit`` call, so
        the map, the audit and the plan cannot disagree about which bullet is
        overdue. A node whose note is exempt (a journal is as true as the day it
        was written) or one the review queue would never show still reports its
        counts — the map has always shown an exempt note's age — but the map's
        own "unchecked" flag stays off it, exactly as it does for notes.

        The findings list is capped: the map is a picture of the whole vault, and
        a note with fifty overdue bullets must not become a fifty-row card. The
        counts beside it are the uncapped truth.
        """
        from ciao.memory_audit import note_entry_coverage

        # Bookkeeping notes are never flagged, so their entries are not worth parsing.
        if never_aged(e.path_key):
            return None
        target = absolute.get(e.path_key)
        if target is None:
            return None
        try:
            text = target.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        try:
            relative = target.relative_to(
                Path(config.workspace_vault_root(str(e.workspace or ""))).resolve()
            ).as_posix()
        except (AttributeError, ValueError, OSError):
            relative = e.path_key
        coverage, selected, _document = note_entry_coverage(
            text,
            note_type=e.type or "",
            updated=e.updated or "",
            mtime=_mtime(e.path_key),
            note_path=relative,
            rendered=e.path_key,
            title=e.title,
            workspace=str(e.workspace or ""),
            today=current_date,
        )
        return {
            "entries": coverage.entries,
            "checked": coverage.checked,
            "exempt": coverage.exempt,
            "unverified": coverage.unverified,
            "uncovered": coverage.uncovered,
            "stale": coverage.stale,
            # Whether this note's TYPE never ages out — a `log`, a `journal`, a
            # `Workspace/` queue. Carried so a client can say so, because
            # `uncovered` on a journal is a different statement from `uncovered`
            # on a person note: the first is a record of what happened and is not
            # meant to be verified at all, the second is a fact nobody has read.
            # Without it the only way to tell them apart is to keep a copy of
            # the exempt set in the browser.
            "note_exempt": coverage.note_exempt,
            "coverage_ratio": round(coverage.coverage_ratio, 4),
            "fully_verified": coverage.fully_verified,
            "stale_entries": [
                {
                    "identity": verdict.identity,
                    "excerpt": verdict.excerpt,
                    "reason": verdict.reason_code,
                    "detail": verdict.reason,
                    "age_days": verdict.age_days,
                    "last_verified": (
                        verdict.last_verified.isoformat()
                        if verdict.last_verified
                        else ""
                    ),
                    "own_date": verdict.own_date,
                }
                for verdict in selected[:_MAP_ENTRY_FINDINGS]
            ],
            "more_stale_entries": max(0, len(selected) - _MAP_ENTRY_FINDINGS),
        }

    def _staleness_fields(e) -> dict[str, Any]:
        """One node's aging, and what the managed pass concluded about it.

        ``stale`` is the age rule, and the check state only ever *clears* it. A
        note somebody checked inside its cooldown is not "unchecked" however old
        its own ``updated:`` reads, and neither is one a proposal is waiting on:
        in both cases the question has been asked and the queue has deliberately
        stopped asking. Leaving the flag on would put this map and the queue it
        links to into open disagreement about the same notes, which is the one
        thing the shared predicate above exists to prevent.

        The check rides along either way, so the map can say *why* a note is not
        being asked about again rather than silently dropping it off the
        "unchecked" count.

        ``entry_coverage`` is the same honesty one level in, and the reason a
        ``stale`` note is not the only thing this route can say. A note whose
        ``updated:`` was re-stamped yesterday clears the flag above while still
        holding a fact from two years ago; without this block the map would show
        that note as clean, and the reader would have no way to learn otherwise
        from the surface that sent them there. It never *raises* the flag — the
        flag is the whole-note verdict, and the entry worklist is where a stale
        bullet becomes a plan — it reports beside it.
        """
        verification = note_verification(
            e.type or "", e.updated or "", _mtime(e.path_key), today=current_date
        )
        if verification is None:
            return {
                "stale": False,
                "age_days": None,
                "threshold_days": None,
                "check": None,
                # Reported even here. A note with no usable date has no age to
                # show, but its *entries* can each carry their own `[verified:]`
                # stamp and be current or not on that — the one case where the
                # whole-note answer is "I cannot tell" and the entry answer is
                # not.
                "entry_coverage": _entry_coverage_for(e),
            }
        stale = verification.stale and not never_aged(e.path_key)
        return {
            "stale": stale,
            # The horizon travels with the flag so the map can name the rule
            # without keeping its own copy of the thresholds table.
            "age_days": verification.age_days,
            "threshold_days": verification.threshold_days,
            "check": None,
            "entry_coverage": _entry_coverage_for(e),
        }

    nodes = [
        {
            "id": e.path_key,
            "title": e.title,
            "type": e.type,
            "tags": e.tags,
            "aliases": e.aliases,
            "description": e.description,
            "workspace": e.workspace,
            "degree": len(graph.get(e.path_key, ())),
            "mtime": _mtime(e.path_key),
            "updated": e.updated,
            **_staleness_fields(e),
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
    return {
        "workspace": workspace or "all",
        "workspaces": workspaces,
        "nodes": nodes,
        "edges": edges,
    }


async def vault_graph(request: Request) -> JSONResponse:
    """Return the vault as a note graph for the Memory Map page.

    Nodes are notes with frontmatter (or an inferred type); edges come from
    both frontmatter ``related:``/``relatedTo:`` and body markdown links,
    already merged and resolved to real paths by ``vault_index.scan_vault``.
    Optional ``?workspace=`` scopes to one logical workspace; cross-workspace
    edges are dropped rather than left dangling.

    Each node also carries ``entry_coverage``: what
    :func:`ciao.memory_audit.note_entry_coverage` found inside it, one level in
    from the node's own age. A note is not the unit a person keeps current, and
    a map that could only say "this note is current" would show a re-stamped
    person note as clean while it held an address from 2019.
    """
    config = request.app.state.config
    workspace = request.query_params.get("workspace", "").strip() or None
    # Scanning, parsing every note body for entry coverage and reading the
    # check state are all synchronous file and CPU work; on the loop they
    # stalled every other request for over a second on a real vault.
    from ciao.fts_search import ConnectedIndexBusy, ConnectedWorkspaceChanged

    try:
        return JSONResponse(await asyncio.to_thread(_vault_graph_payload, config, workspace))
    except ConnectedWorkspaceChanged as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ConnectedIndexBusy as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)


# The packaged Workspace care routine, fanned out per workspace
# (`system-memory-curation@<workspace>`).
CARE_SCHEDULE_BASE_ID = "system-memory-curation"


def _care_display_path(config: CiaoConfig, path: Path) -> str:
    """The path the file viewer opens: workspace-relative when it can be."""
    try:
        return path.relative_to(Path(config.workspace_root).resolve()).as_posix()
    except (ValueError, OSError):
        return str(path)


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def care_report(request: Request) -> JSONResponse:
    """GET one workspace's Workspace care report, parsed (read-only).

    The source is `<vault>/Workspace/Care-Report.md`, the checklist the daily
    care run writes; :mod:`ciao.care_report` reads it with a size cap and never
    raises, so a missing file is the "never run" state, not an error.

    Whether a run is in progress comes from the care schedule itself: the
    scheduler stamps `last_status = "running"` at dispatch and overwrites it with
    the outcome. The step it is on is the report's first open `- [ ]` line, and
    only when the report was written after that dispatch: before the new run has
    rewritten the checklist, the file on disk is the previous run's.
    """
    config = request.app.state.config
    workspace = request.query_params.get("workspace", "").strip()
    if not workspace or config.workspace(workspace) is None:
        return JSONResponse({"error": "workspace is required"}, status_code=400)
    try:
        vault = Path(config.workspace_vault_root(workspace)).resolve()
    except (AttributeError, ValueError, OSError) as exc:
        # The detail can name an absolute path: it goes to the log, not the client.
        logger.warning("care report: vault for %s unavailable: %s", workspace, exc)
        return JSONResponse({"error": "vault unavailable"}, status_code=409)
    body: dict[str, Any] = await asyncio.to_thread(read_care_report, vault)
    references = durable_memory_guide_path().parent / "references"
    for step in body["steps"]:
        name = CARE_STEP_FILES.get(step["id"], "")
        step["step_file"] = str(references / name) if name else ""
    # The item's own path is vault-relative (and confined by the parser); the
    # file viewer wants the workspace-relative one. The guide is found by the
    # app itself, so it has none here.
    for item in body["needs_you"]:
        path = item["path"]
        item["open_path"] = _care_display_path(config, vault / path) if path and path != CARE_GUIDE_PATH else ""

    schedule: dict[str, Any] | None = None
    running: dict[str, Any] | None = None
    sm = getattr(request.app.state, "schedule_manager", None)
    if sm is not None:
        schedule_id = system_schedule_id(CARE_SCHEDULE_BASE_ID, workspace)
        try:
            current = sm.get_with_revision(schedule_id)
        except ScheduleStoreCorrupt:
            current = None
        entry = current[0] if current is not None else None
        if entry is not None:
            schedule = {
                "id": entry.schedule_id,
                "enabled": bool(entry.enabled),
                "last_status": entry.last_status or "",
                "last_run_at": entry.last_dispatched_at or "",
            }
            if entry.last_status == "running":
                since = _parse_iso(entry.last_dispatched_at or "")
                written = _parse_iso(body["modified_at"])
                fresh = since is not None and written is not None and written >= since
                open_steps = [s for s in body["steps"] if s["state"] == "open"] if fresh else []
                counts = body["counts"]
                running = {
                    "since": entry.last_dispatched_at or "",
                    "step": open_steps[0]["id"] if open_steps else "",
                    "index": counts["done"] + counts["blocked"] + 1 if open_steps else 0,
                    "total": counts["total"] if open_steps else 0,
                }
    body.update({
        "workspace": workspace,
        "report_path": _care_display_path(config, vault / CARE_REPORT_PATH),
        "unsure_path": _care_display_path(config, vault / CARE_UNSURE_PATH),
        "schedule": schedule,
        "running": running,
    })
    return JSONResponse(body)


# The largest body the Unsure page sends: an action and a line id.
_UNSURE_BODY_MAX_BYTES = 1024


def _unsure_error(message: str, status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": message, "code": code}, status_code=status)


async def _bounded_body(request: Request, limit: int) -> bytes | None:
    """The request body, or None once it passes `limit` bytes (never buffered past it)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


def _care_running(request: Request, workspace: str) -> bool:
    """Whether the workspace's care run is in progress (the scheduler's own stamp)."""
    sm = getattr(request.app.state, "schedule_manager", None)
    if sm is None:
        return False
    try:
        current = sm.get_with_revision(system_schedule_id(CARE_SCHEDULE_BASE_ID, workspace))
    except ScheduleStoreCorrupt:
        return False
    entry = current[0] if current is not None else None
    return entry is not None and entry.last_status == "running"


async def unsure_list(request: Request) -> JSONResponse:
    """GET or change one workspace's Unsure list, `<vault>/Workspace/Unsure.md`.

    GET `?workspace=<name>` answers the list parsed (see
    :func:`ciao.unsure_list.read_list`) plus the file's display path. POST
    `{"workspace", "op": "tick" | "untick" | "remove", "id"}` changes the one
    item line with that id under the list's file lock and answers the list as
    written. Every refusal carries a `code`: `line_changed` (409, the page
    reads again), `care_running` (423: the care run edits the file with its own
    tools, which do not take the lock), `not_plain_file` (422), `too_large`
    (413), `busy` (503, lock held past 2 s), `vault_unavailable` (503),
    `bad_request` (400). The client names a workspace, never a path, and line
    text is never logged.
    """
    config = request.app.state.config
    if request.method == "POST":
        raw = await _bounded_body(request, _UNSURE_BODY_MAX_BYTES)
        if raw is None:
            return _unsure_error("body too large", 413, "too_large")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return _unsure_error("bad json", 400, "bad_request")
        if not isinstance(body, dict):
            return _unsure_error("bad json", 400, "bad_request")
        workspace = str(body.get("workspace", "")).strip()
        op = str(body.get("op", ""))
        target = str(body.get("id", ""))
        if op not in ("tick", "untick", "remove") or not is_unsure_line_id(target):
            return _unsure_error("op and id are required", 400, "bad_request")
    else:
        workspace = request.query_params.get("workspace", "").strip()
    if not workspace or config.workspace(workspace) is None:
        return _unsure_error("workspace is required", 400, "bad_request")
    try:
        vault = Path(config.workspace_vault_root(workspace)).resolve()
    except (AttributeError, ValueError, OSError) as exc:
        logger.warning("unsure list: vault for %s unavailable: %s", workspace, type(exc).__name__)
        return _unsure_error("the workspace's vault is not available", 503, "vault_unavailable")
    if request.method == "POST" and _care_running(request, workspace):
        return _unsure_error("Workspace care is running and may be editing the list; try again when it finishes", 423, "care_running")
    try:
        if request.method == "POST":
            payload = await asyncio.to_thread(change_line, vault, op, target)
        else:
            payload = await asyncio.to_thread(read_list, vault)
    except UnsureError as exc:
        return _unsure_error(str(exc), exc.status, exc.code)
    except QueueLockError:
        return _unsure_error("the list is being written; try again", 503, "busy")
    except OSError as exc:
        logger.warning("unsure list: %s for %s failed: %s", request.method, workspace, type(exc).__name__)
        return _unsure_error("the list could not be read or written", 500, "io_error")
    payload["workspace"] = workspace
    payload["path"] = _care_display_path(config, vault / CARE_UNSURE_PATH)
    return JSONResponse(payload)


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


def _scan_entity_types(
    vault: Path, workspace: str, registry: entity_types.EntityTypeRegistry
) -> tuple[list[Entry], dict[str, int]]:
    """One scan of *vault*: its entries, and its notes per ``type:`` value.

    A note counts under both the value it carries and the owner that value
    resolves to, because the two questions the callers ask are different. The
    owning side is what the index and the linter mean by a count, so
    ``type: doc`` counts for ``document``. The literal side is what a category's
    own notes say, and it is not redundant: a category the user has just added
    is in none of the static tables when its notes are typed with that
    category's own alias, so a count resolved through those tables alone would
    report every such new category as empty, and the delete guard below would
    drop a category whose notes still carried its ``type:``. The index, the
    linter and the staleness check all read the registry now (#626), but this
    scan runs its own literal-plus-canonical fold, so the literal side is what
    keeps a just-added category's notes counted here regardless.

    *registry* is the caller's already-loaded registry, not a second load, and
    it is the only thing that knows a custom category's own aliases: a note typed
    with one of those is the category's note, not drift under a spelling no row
    claims. The static table is consulted first, so a stock alias keeps
    resolving the way the index and the linter resolve it. The registry's alias
    view is enabled-only, which is right here too: a disabled category claims no
    ``type:``, so its aliases stay drift.

    A ``type:`` that is neither a category nor an alias of one is drift. It is
    counted under its own spelling, which no row claims, so it stays visible as
    an unlisted type instead of inflating a category that does not own it.
    """
    entries = scan_vault(vault, workspace=workspace)
    aliases = registry.aliases()
    counts: dict[str, int] = defaultdict(int)
    for entry in entries:
        raw = entry.type.strip()
        if raw:
            counts[raw] += 1
        owner = canonical_type(raw) or aliases.get(raw, "")
        if owner and owner != raw:
            counts[owner] += 1
    return entries, dict(counts)


def _write_entity_types_under_lock(
    vault: Path, entries: list[entity_types.EntityType]
) -> None:
    """Write the whole category list under the lock the accept also takes.

    ``write_vault_file`` is atomic but not locked, and the ``[category]`` accept
    reads the registry, appends to it and writes it as one transaction under
    this same lock — so a PATCH that wrote outside it could land between that
    read and that write, and the accepted category would be dropped with no
    error on either side. A PATCH is a full replacement rather than a diff, so
    serialization is all it needs: whichever of the two arrives second wins,
    which is the contract it already had.

    Raises :class:`QueueLockError` rather than writing unlocked, like every
    other managed writer of a file behind this lock.
    """
    from ciao.file_kernel import queue_lock

    with queue_lock(vault / entity_types.VAULT_FILENAME):
        entity_types.write_vault_file(vault, entries)


def _regenerate_vocabulary(
    vault: Path, workspace: str
) -> tuple[dict[str, int], entity_types.EntityTypeRegistry]:
    """Rewrite ``VOCABULARY.md`` with the Categories section; return the counts
    and the registry they were counted against.

    One worker thread for the scan and the write, and one load of the registry
    the save left on disk, shared by all three consumers of it — the counts, the
    Categories section and the body the caller answers — because a Categories
    section that disagreed with the type census beside it, or with the rows the
    response lists, would be worse than no section. The counts ride back out
    because the caller needs them for the response and the scan that produced
    them is already paid for.
    """
    registry = entity_types.load_entity_types(vault)
    entries, counts = _scan_entity_types(vault, workspace, registry)
    write_vocabulary_file(entries, vault / "VOCABULARY.md", registry=registry)
    return counts, registry


async def memory_entity_types(request: Request) -> JSONResponse:
    """GET the effective category list; PATCH the user's edits to it.

    One handler for both, as in ``settings_routines``, and both answer the same
    body: a PATCH returns the list it produced, so a client never has to re-GET
    a vault scan it can already have.

    The registry is per vault FILE, and ``<vault>`` is the agent vault root —
    the one that owns ``VOCABULARY.md`` and ``entity-types.yaml``, not a
    workspace's notes root. It is the same root that owns ``INDEX.md`` and ``VOCABULARY.md``, and the
    registry follows its file.

    A PATCH is the desired list of entries, not a diff: idempotent, and a
    client that sends the GET's own rows back unchanged writes nothing. Two
    rules in ``ciao.entity_types`` make the resubmission safe, both stated
    precisely because they hold for a stock id and not for a custom one: a row
    whose id is a stock id is a partial override of the shipped default (so a
    client that changes one field of a stock row resets nothing), and a stock
    row identical to the shipped default is not persisted at all (so an
    upgrade's change to a default still reaches the install). A custom id has
    no shipped default to fall back on, so a field it leaves out takes the
    built-in default instead — the contract is to send the whole list, which is
    exactly what the GET hands a client.
    """
    config = request.app.state.config
    workspace = request.query_params.get("workspace", "").strip()
    if not workspace or config.workspace(workspace) is None:
        return JSONResponse({"error": "workspace is required"}, status_code=400)
    try:
        vault = Path(config.agent_vault_root(workspace))
    except (AttributeError, ValueError, OSError) as exc:
        return JSONResponse({"error": f"vault unavailable: {exc}"}, status_code=409)

    if request.method == "GET":
        # One registry for the scan and the body: the counts have to resolve a
        # custom category's own aliases, and that registry is the same one the
        # rows are rendered from, so the two cannot describe different lists.
        registry = entity_types.load_entity_types(vault)
        _entries, counts = await asyncio.to_thread(
            functools.partial(_scan_entity_types, vault, workspace, registry)
        )
        return JSONResponse(_entity_types_body(vault, workspace, counts, registry))

    try:
        body = await request.json()
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict) or not isinstance(body.get("types"), list):
        return JSONResponse({"error": "expected an object with a types list"}, status_code=400)
    try:
        entries = entity_types.parse_payload(body["types"])
        entity_types.validate_entries(entries)
    except entity_types.EntityTypeFileError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    # A custom category the list omits is deleted from the file, and the notes
    # that carry its `type:` would be left pointing at nothing. So the delete is
    # refused while those notes exist, with the count that blocks it: the fix
    # is to retype them, not to lose the category. A stock id is never deleted
    # — omitting one leaves it at its default, and `enabled: false` is how a
    # user turns one off.
    registry = entity_types.load_entity_types(vault)
    submitted = {entry.id for entry in entries}
    orphaned = [
        entry
        for entry in registry.entries()
        if not entry.builtin and entry.id not in submitted
    ]
    if orphaned:
        _entries, counts = await asyncio.to_thread(
            functools.partial(_scan_entity_types, vault, workspace, registry)
        )
        blocking = [
            f"{entry.id} ({counts[entry.id]} note{'' if counts[entry.id] == 1 else 's'})"
            for entry in orphaned
            if counts.get(entry.id, 0)
        ]
        if blocking:
            return JSONResponse(
                {
                    "error": "refusing to delete a category its notes still use: "
                    + ", ".join(blocking)
                    + " — retype those notes first, or keep the category"
                },
                status_code=400,
            )

    try:
        await asyncio.to_thread(
            functools.partial(_write_entity_types_under_lock, vault, entries)
        )
    except QueueLockError as exc:
        return JSONResponse(
            {"error": f"the categories file is busy; nothing was written: {exc}"},
            status_code=503,
        )
    except OSError as exc:
        return JSONResponse(
            {"error": f"could not write the categories file: {exc}"}, status_code=500
        )
    try:
        counts, fresh = await asyncio.to_thread(
            functools.partial(_regenerate_vocabulary, vault, workspace)
        )
    except OSError as exc:
        return JSONResponse(
            {
                "error": "categories saved, but VOCABULARY.md could not be "
                f"regenerated: {exc}"
            },
            status_code=500,
        )
    # The write cleared the registry cache, so this body is read back off the
    # file that was just written: the list a client gets is the one the next GET
    # will serve, not the submission echoed at it.
    return JSONResponse(_entity_types_body(vault, workspace, counts, fresh))


def _entity_types_body(
    vault: Path,
    workspace: str,
    counts: dict[str, int],
    registry: entity_types.EntityTypeRegistry,
) -> dict[str, Any]:
    """The response both methods answer: the vault, and its effective list.

    The registry is the caller's, loaded once by the method that already had to
    read it, so the counts in the body and the rows beside them are the same
    configuration rather than two reads that could straddle a write.
    """
    return {
        "workspace": workspace,
        "vault": str(vault),
        "types": entity_types.effective_payload(registry, counts),
    }


# Binary downloads (PDFs, ZIPs, office docs) live under their own endpoint so
# the text and image viewers stay strictly typed. Same (unrestricted) path
# contract as ``workspace_file``/``workspace_image``: any allowlisted-extension
# file on disk is served, relative paths anchoring to the workspace. The browser
# decides whether to render inline (PDF) or save (everything else) based on
# the inferred MIME type. Saved-page archives are forced to download so their
# packaged HTML cannot execute under the PWA origin, and so is every document
# format kept only as the original of a chat attachment's Markdown copy.
_WORKSPACE_INLINE_BINARY_EXTS = frozenset({".pdf", ".zip", ".docx", ".xlsx", ".pptx"})
_WORKSPACE_BINARY_EXTS = (
    _WORKSPACE_INLINE_BINARY_EXTS | {".mht", ".mhtml"} | (ANYDOC_EXTENSIONS - {".csv"})
)
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
                    env=child_env(),
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
        media_type = guess_media_type(resolved.name)
        if media_type is None:
            _FALLBACK_MIMES = {
                ".pdf": "application/pdf",
                ".zip": "application/zip",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            }
            media_type = _FALLBACK_MIMES.get(resolved.suffix.lower(), "application/octet-stream")

    # `inline` lets PDFs preview in a tab; everything else is an explicit
    # download. Custom frame headers let supported previews embed inside the
    # PWA's same-origin file viewer iframe.
    disposition = "inline" if source_ext in _WORKSPACE_INLINE_BINARY_EXTS else "attachment"
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

    With ``chat_id`` a relative path anchors to that chat's agent and vault
    roots (``_resolve_in_chat``) instead of ``workspace_root``, exactly, with
    no fuzzy walk: a chat message's embedded image
    (``![shot](memory-vault/x.png)``) is served this way, and a message full of
    made-up image paths must not trigger directory walks. This anchors; it
    does not confine: absolute and ``..`` paths follow the same no-sandbox
    policy as without ``chat_id``.
    """
    chat_id = request.query_params.get("chat_id", "").strip()
    result: Path | Response
    if chat_id:
        result = await _resolve_in_chat(request, chat_id, fuzzy=False)
    else:
        roots = _allowed_roots(request.app.state.config)
        raw = request.query_params.get("path", "").strip()
        result = _resolve_workspace_path(roots, raw, allow_fuzzy=True)
    if isinstance(result, Response):
        return result
    resolved = result

    if resolved.suffix.lower() not in _WORKSPACE_IMAGE_EXTS:
        return JSONResponse({"error": "unsupported type"}, status_code=415)
    if resolved.stat().st_size > _WORKSPACE_IMAGE_MAX_BYTES:
        return JSONResponse({"error": "file too large"}, status_code=413)

    media_type = guess_media_type(resolved.name)
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
    opener = shutil.which("xdg-open")
    if not opener:
        raise OSError("xdg-open is not available on this platform")
    subprocess.run([opener, str(path)], check=True, timeout=30)


async def workspace_open(request: Request) -> Response:
    """Open a path with the OS default application on the machine running Ciao.

    Body: ``{"path": str}``. Uses the same path resolver as the workspace
    viewers (relative paths anchor to workspace_root; fuzzy basename lookup
    is allowed for files). Directories are opened too — that is the file
    manager — because an agent that points at a folder is pointing at a
    thing the user wants to see. The open happens on the engine's machine,
    so a remote caller (a phone, another Mac over Tailscale) would pop the
    file up on a screen nobody is looking at: refused, and the PWA hides
    the action there.
    """
    if not is_loopback_client(request):
        return JSONResponse(
            {"error": "Opening files is only available on the computer running Ciaobot."},
            status_code=403,
        )
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "bad json"}, status_code=400)
    raw = str(body.get("path", "")).strip()
    if not raw:
        return JSONResponse({"error": "missing path"}, status_code=400)

    config = request.app.state.config
    roots = _allowed_roots(config)
    result = _resolve_workspace_path(roots, raw, allow_fuzzy=True, allow_dir=True)
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
        resolved.write_text(content, encoding="utf-8", newline="")
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
    # Engine-internal streak: no PWA surface reads it, so it stays off the payload.
    entry_dict.pop("fail_streak", None)
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
        if getattr(entry, "owner_chat_id", ""):
            # A chat-owned loop is never re-homed: it is available only while its
            # own chat is live.
            entry_dict["context_available"] = chat is not None and not chat.archived
        else:
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


_SCHEDULE_CONFLICT = {
    "error": "schedule changed since you read it; reload and retry",
    "code": "schedule_conflict",
}
#: A schedule stores no permission mode. Refused rather than ignored, so a
#: caller never believes it chose one.
_SCHEDULE_MODE_REMOVED = {
    "error": "mode is no longer supported; a schedule's runs use the workspace's new-chat default mode",
}


async def list_schedules(request: Request) -> JSONResponse:
    sm = request.app.state.schedule_manager
    pcm = request.app.state.project_chat_manager
    return JSONResponse(
        [{**_enrich_schedule(s, pcm), "revision": revision} for s, revision in sm.snapshot()]
    )


def _schedule_target_refusal(
    pcm, project_id: str | None, chat_id: str | None
) -> str:
    """Why a schedule cannot run in this target, or '' when it can.

    The Memory project is app-owned: it holds the engine's own memory passes,
    and a routine run there would put an ordinary chat in a project where
    every chat is silenced. A memory pass is not a target either — its turn is
    the app's own, and a run posted into it never reaches the owner.
    """
    if project_id:
        if is_memory_pass_project(pcm.get_project(project_id)):
            return "the Memory project holds the engine's own memory passes"
    if chat_id:
        chat = pcm.get_chat(chat_id)
        if chat is not None and is_memory_pass_chat(
            chat, pcm.get_project(chat.project_id)
        ):
            return "a memory pass is not a target for a schedule"
    return ""


async def create_schedule(request: Request) -> JSONResponse:
    sm = request.app.state.schedule_manager
    pcm = request.app.state.project_chat_manager
    body = await request.json()

    if "mode" in body:
        return JSONResponse(_SCHEDULE_MODE_REMOVED, status_code=400)
    web_chat_id = body.get("web_chat_id")
    web_project_id = body.get("web_project_id")

    refusal = _schedule_target_refusal(pcm, web_project_id, web_chat_id)
    if refusal:
        return JSONResponse({"error": refusal}, status_code=400)

    # Persist only explicit overrides. Empty model/provider values mean
    # "inherit the selected workspace" and are resolved afresh at dispatch
    # time, so changing a workspace default also changes future runs.
    model = (body.get("model") or "").strip()

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
            tz = ZoneInfo(body.get("timezone", DEFAULT_TIMEZONE))
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
        command = normalize_schedule_command(body.get("command"))
        days_of_week = normalize_days_of_week(body.get("days_of_week"))
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
    try:
        entry = sm.create(
            daily_time_utc=body.get("time") or "",
            prompt=body["prompt"],
            model=model,
            provider=provider,
            chat_id=body.get("chat_id", 0),
            timezone_name=body.get("timezone", DEFAULT_TIMEZONE),
            days_of_week=days_of_week,
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
            command=command,
        )
    except ScheduleStoreCorrupt:
        return JSONResponse(
            {"error": "schedule store is unreadable; nothing was changed"}, status_code=503
        )
    # Records where a chat-bound entry re-homes once its chat is deleted; must
    # be captured while that chat still exists. See stamp_fallback_project.
    if stamp_fallback_project(entry, pcm):
        sm.replace(entry)
    publish_automations_changed(pcm)
    current = sm.get_with_revision(entry.schedule_id)
    created, revision = current if current is not None else (entry, None)
    return JSONResponse(
        {**_enrich_schedule(created, pcm), "revision": revision}, status_code=201
    )


async def run_schedule_now(request: Request) -> JSONResponse:
    """Trigger a schedule immediately."""
    schedule_id = request.path_params["schedule_id"]
    sm = request.app.state.schedule_manager
    # A routine whose target the app will not run in is refused here, with the
    # reason, rather than silently doing nothing: the run would otherwise be
    # skipped by the dispatch and the click would look like it worked.
    entry = sm._store.get(schedule_id)
    if entry is not None:
        refusal = request.app.state.project_chat_manager.schedule_target_refusal(entry)
        if refusal:
            return JSONResponse({"error": refusal}, status_code=400)
        # Workspace care rebuilds and ticks one report file; a second run on top
        # of a live one would rewrite the checklist under it.
        if system_base_id(entry.schedule_id) == CARE_SCHEDULE_BASE_ID and entry.last_status == "running":
            return JSONResponse({"error": "already running"}, status_code=409)
    try:
        result = await sm.dispatch_now(schedule_id)
    except MemoryProjectRefused as exc:
        # The target resolved to the app's own memory machinery, which the
        # app will not run a routine in. The schedule exists: say why.
        return JSONResponse({"error": str(exc)}, status_code=400)
    except ValueError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except RuntimeError as exc:
        if "paused" in str(exc).lower():
            return JSONResponse({"error": str(exc)}, status_code=409)
        raise
    if result.get("status") == "refused":
        # The scheduler asked the engine and the engine refused the target.
        return JSONResponse(
            {"error": result.get("error") or "the app will not run this routine here"},
            status_code=400,
        )
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


async def onboarding_interview(request: Request) -> JSONResponse:
    """Open the guided first-run interview on request (never on install)."""
    workspace = str(request.query_params.get("workspace") or "").strip()
    if not workspace:
        return JSONResponse({"error": "workspace is required"}, status_code=400)
    pcm = request.app.state.project_chat_manager
    general = next(
        (item for item in pcm.list_projects(workspace) if item.name == "General"),
        None,
    )
    if general is None:
        return JSONResponse({"error": "workspace not found"}, status_code=404)
    chat_id = pcm.start_onboarding_interview(general.project_id)
    if not chat_id:
        return JSONResponse({"error": "could not open the interview"}, status_code=500)
    return JSONResponse({"chat_id": chat_id})


async def schedule_detail(request: Request) -> JSONResponse:
    """Handle PATCH (update) and DELETE for a single schedule."""
    schedule_id = request.path_params["schedule_id"]
    if request.method == "DELETE":
        sm = request.app.state.schedule_manager
        try:
            ok = sm.delete(
                schedule_id,
                expected_revision=request.query_params.get("revision"),
            )
        except ScheduleConflict:
            return JSONResponse(_SCHEDULE_CONFLICT, status_code=409)
        except ScheduleStoreCorrupt:
            return JSONResponse(
                {"error": "schedule store is unreadable; nothing was changed"}, status_code=503
            )
        publish_automations_changed(request.app.state.project_chat_manager)
        return JSONResponse({"ok": ok})
    # PATCH
    store = request.app.state.schedule_manager._store
    entry = store.get(schedule_id)
    if entry is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    # The row as read, before the edits below: the store applies only the
    # fields that differ from it onto the current row, so a concurrent edit to
    # another field is kept and a row deleted meanwhile is not resurrected.
    base = dataclasses.replace(entry)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "request body must be valid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "request body must be an object"}, status_code=400)
    if "mode" in body:
        return JSONResponse(_SCHEDULE_MODE_REMOVED, status_code=400)
    # Optional precondition: the `revision` the caller read. Taken out of the
    # body first so the field loop and the system-row normalizer never see it.
    expected_revision = body.pop("revision", None)
    if expected_revision is not None and not isinstance(expected_revision, str):
        return JSONResponse({"error": "revision must be a string"}, status_code=400)
    # The target this update asks for, checked before anything is written: the
    # app will not run a routine in the Memory project or inside a pass, and a
    # half-applied update would leave the entry pointing somewhere else. An
    # update that does not touch the target is always allowed, so an existing
    # invalid routine can still be paused, renamed or disabled.
    if "web_project_id" in body or "web_chat_id" in body:
        refusal = _schedule_target_refusal(
            request.app.state.project_chat_manager,
            body.get("web_project_id") if "web_project_id" in body else entry.web_project_id,
            body.get("web_chat_id") if "web_chat_id" in body else entry.web_chat_id,
        )
        if refusal:
            return JSONResponse({"error": refusal}, status_code=400)
    timing_fields: set[str] = set()
    if entry.scope == "system":
        try:
            normalized, fields = normalize_system_schedule_update(body, entry)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        timing_fields = set(fields)
        body = {
            ("time" if key == "daily_time_utc" else "timezone" if key == "timezone_name" else key): value
            for key, value in normalized.items()
        }
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
        try:
            entry.days_of_week = normalize_days_of_week(body["days_of_week"])
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
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
        if "command" in body:
            entry.command = normalize_schedule_command(body.get("command"))
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
        request.app.state.schedule_manager.replace(
            entry,
            timing_fields=frozenset(timing_fields) if entry.scope == "system" else frozenset(),
            base=base,
            expected_revision=expected_revision,
        )
    except ScheduleConflict:
        return JSONResponse(_SCHEDULE_CONFLICT, status_code=409)
    except ScheduleNotFound:
        return JSONResponse({"error": "not found"}, status_code=404)
    except ScheduleStoreCorrupt:
        return JSONResponse(
            {"error": "schedule store is unreadable; nothing was changed"}, status_code=503
        )
    except ValueError as exc:
        # A fanned-out system routine's workspace is part of its id, so the
        # store refuses to "move" it. That is a bad request, not a server
        # fault: the PWA hides the control for those rows, but a direct API
        # caller would otherwise get a 500 for asking something answerable.
        return JSONResponse({"error": str(exc)}, status_code=400)
    pcm = request.app.state.project_chat_manager
    publish_automations_changed(pcm)
    current = store.get_with_revision(schedule_id)
    if current is None:
        return JSONResponse({"error": "schedule disappeared after update"}, status_code=500)
    reread, revision = current
    return JSONResponse({**_enrich_schedule(reread, pcm), "revision": revision})


# ── Models ───────────────────────────────────────────────────────────────

async def _acp_catalog(
    descriptor: provider_registry.ProviderDescriptor, workspace_root: Path, refresh: bool
) -> list[dict[str, Any]]:
    return await descriptor.factory().model_catalog(workspace_root, force=refresh)


async def list_models(request: Request) -> JSONResponse:
    config = request.app.state.config

    workspace_name = request.query_params.get("workspace")
    if workspace_name:
        workspace_config = config.workspace(workspace_name)
        if workspace_config is None:
            return JSONResponse({"error": "unknown workspace"}, status_code=400)
        workspace_root = config.agent_root(workspace_name)
    else:
        workspace_root = Path(config.workspace_root)
    # `?refresh=1` bypasses the ACP catalog cache. The catalog is served on
    # demand so a provider connected in another window shows up immediately.
    refresh = str(request.query_params.get("refresh", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    # Every descriptor that runs over ACP reports its own catalog, opencode included. A probe
    # that raises leaves that provider with an empty catalog; the exception is logged by type only.
    catalog_descriptors = [item for item in provider_registry.descriptors() if item.acp_spec_path]

    # Another provider probe can keep this aggregation open after a cached
    # result was read. Recheck the epoch immediately before the response and
    # re-read only providers whose auth/catalog changed during that wait.
    deadline = asyncio.get_running_loop().time() + 60.0
    results_by_provider: dict[str, Any] = {}
    epochs_by_provider: dict[str, int] = {}
    pending_descriptors = catalog_descriptors
    while pending_descriptors:
        before = {item.id: item.factory().model_catalog_epoch() for item in pending_descriptors}
        results = await asyncio.gather(
            *(_acp_catalog(item, workspace_root, refresh) for item in pending_descriptors),
            return_exceptions=True,
        )
        for item, result in zip(pending_descriptors, results, strict=True):
            results_by_provider[item.id] = result
            epochs_by_provider[item.id] = before[item.id]
        stale = [
            item for item in catalog_descriptors
            if item.id in epochs_by_provider
            and epochs_by_provider[item.id] != item.factory().model_catalog_epoch()
        ]
        if not stale:
            break
        if asyncio.get_running_loop().time() >= deadline:
            for item in stale:
                results_by_provider[item.id] = []
                epochs_by_provider[item.id] = item.factory().model_catalog_epoch()
            break
        pending_descriptors = stale
    acp_results = [results_by_provider.get(item.id, []) for item in catalog_descriptors]
    acp_models: dict[str, list[str]] = {}
    acp_defaults: dict[str, str] = {}
    acp_evidence: dict[str, dict[str, Any]] = {}
    acp_levels: dict[str, list[str]] = {}
    for item, result in zip(catalog_descriptors, acp_results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("%s model catalog failed (%s).", item.id, type(result).__name__)
        rows = result if isinstance(result, list) else []
        models = [str(row["model"]) for row in rows if row.get("model")]
        operator_default = config.default_model_for_provider(item.id)
        acp_models[item.id] = models
        acp_defaults[item.id] = (
            operator_default if operator_default in models else (models[0] if models else "")
        )
        acp_evidence[item.id] = {
            "status": "observed" if models else "unknown",
            "source": "acp_config_options" if models else None,
            "models": [
                {
                    "id": str(row["model"]),
                    "label": str(row.get("label") or row["model"]),
                    **({"description": str(row["description"])} if row.get("description") else {}),
                }
                for row in rows if row.get("model")
            ],
            "reason": None if models else "no_models_reported",
        }
        # Reasoning levels are keyed by bare model id, as the PWA reads them.
        for row in rows:
            if row.get("model"):
                acp_levels.setdefault(str(row["model"]), list(row.get("variants") or []))

    # The Claude catalog is the ACP one above. The top-level "models" and "default" are the
    # tier list and the default the PWA reads when that catalog is empty.
    claude_default = (
        acp_defaults.get("claude")
        or config.default_model_for_provider("claude")
        or config.claude_default_model
    )
    opencode_models = acp_models.get("opencode", [])
    return JSONResponse({
        "models": list(CLAUDE_MODELS),
        "default": claude_default,
        "provider_models": acp_models,
        "provider_defaults": acp_defaults,
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
        "model_reasoning_levels": acp_levels,
        "thinking_levels": {k: list(v) for k, v in THINKING_LEVELS.items()},
        "provider_catalog_evidence": acp_evidence,
    })


# ── Routine settings (Settings → Providers tab) ────────────────────────────

_ROUTINES_SETTINGS_KEYS = frozenset({
    "provider_default_models",
    "provider_default_modes",
    "provider_default_thinking",
    "provider_insights_models",
    "pwa_host",
    "log_level",
    "app_repo",
    "resume_after_restart",
})


def _routines_payload(config, app_settings) -> dict:
    """Shared GET/PATCH response: overrides, effective values, options."""
    s = app_settings.settings

    return {
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
        # Per-provider Session insights models, as stored (missing = that
        # source chat's model).
        "provider_insights_models": s.provider_insights_models or {},
        # Server settings that used to be workspace `.env` variables, as
        # stored ("" = default). The bind address and the log level are read
        # at startup, so a change takes effect after a restart; the source
        # checkout applies at once.
        "pwa_host": s.pwa_host,
        "log_level": s.log_level,
        "app_repo": s.app_repo,
        # Settings → General: resume a chat whose turn a restart cut off (#2128).
        "resume_after_restart": s.resume_after_restart,
        "server_defaults": {
            "pwa_host": DEFAULT_PWA_HOST,
            "log_level": DEFAULT_LOG_LEVEL,
            "log_levels": list(LOG_LEVELS),
        },
        # What the running server bound and logs at (the config keeps its
        # startup values), so the page can say a saved change is waiting for a
        # restart.
        "server_running": {
            "pwa_host": config.pwa_host,
            "log_level": config.log_level,
        },
        # Grouped options for the routine model selectors.
        "model_options": {
            "anthropic": list(CLAUDE_MODELS),
        },
        "backends": {
            "anthropic": True,
        },
        "workspace_context": {
            "workspace_root": str(config.workspace_root),
            # `vault_root` is the configured path, which is
            # the install's own vault folder name — true but useless on its own, so the vaults
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
        unknown = sorted(set(body) - _ROUTINES_SETTINGS_KEYS - {"insights_enabled", "verify_vault_answers"})
        if unknown:
            return JSONResponse(
                {"error": f"Unknown routines settings: {', '.join(unknown)}"},
                status_code=400,
            )
        try:
            app_settings.update(body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        app_settings.apply_to_config(config)
    return JSONResponse(_routines_payload(config, app_settings))


async def settings_keyboard(request: Request) -> JSONResponse:
    """Read or update keyboard and composer preferences shared by this engine's clients."""
    app_settings = request.app.state.app_settings
    if app_settings is None:
        return JSONResponse({"error": "settings store unavailable"}, status_code=503)
    def payload() -> dict[str, Any]:
        keyboard_shortcuts = app_settings.settings.keyboard_shortcuts or {}
        keyboard_send_mode = app_settings.settings.keyboard_send_mode or "modifier"
        revision_source = json.dumps(
            [keyboard_shortcuts, keyboard_send_mode],
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return {
            "keyboard_shortcuts": keyboard_shortcuts,
            "keyboard_send_mode": keyboard_send_mode,
            "revision": hashlib.sha256(revision_source).hexdigest(),
        }
    if request.method == "PATCH":
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "expected an object"}, status_code=400)
        expected_revision = body.pop("revision", None)
        unknown = sorted(set(body) - {
            "keyboard_shortcuts", "keyboard_send_mode",
        })
        if unknown:
            return JSONResponse(
                {"error": f"Unknown keyboard settings: {', '.join(unknown)}"},
                status_code=400,
            )
        current = payload()
        if not isinstance(expected_revision, str):
            return JSONResponse({"error": "revision is required"}, status_code=400)
        if expected_revision != current["revision"]:
            return JSONResponse({"error": "Keyboard settings changed on another device. Reload and try again."}, status_code=409)
        try:
            app_settings.update(body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        pcm = getattr(request.app.state, "project_chat_manager", None)
        if pcm is not None:
            pcm.events.publish({
                "type": "keyboard_settings_changed",
                **payload(),
            })
    return JSONResponse(payload())


_APPEARANCE_KEYS = frozenset(
    {"text_contrast", "body_font", "theme", "font_scale", "custom_colors"}
)


async def settings_appearance(request: Request) -> JSONResponse:
    """Read or update appearance preferences shared by every device of this engine."""
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
        expected_revision = body.pop("revision", None)
        unknown = sorted(set(body) - _APPEARANCE_KEYS)
        if unknown:
            return JSONResponse(
                {"error": f"Unknown appearance settings: {', '.join(unknown)}"},
                status_code=400,
            )
        if not isinstance(expected_revision, str):
            return JSONResponse({"error": "revision is required"}, status_code=400)
        if expected_revision != app_settings.appearance_payload()["revision"]:
            return JSONResponse(
                {"error": "Appearance settings changed on another device. Reload and try again."},
                status_code=409,
            )
        try:
            app_settings.update(body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        pcm = getattr(request.app.state, "project_chat_manager", None)
        if pcm is not None:
            pcm.events.publish({
                "type": "appearance_settings_changed",
                **app_settings.appearance_payload(),
            })
    return JSONResponse(app_settings.appearance_payload())


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
    """Return startup phase progress and the host's own version state."""
    from ciao import __version__

    tracker = getattr(request.app.state, "startup_tracker", None)
    payload = tracker.to_dict() if tracker is not None else {"phases": [], "overall_ready": True}
    if tracker is not None:
        startup_id = getattr(tracker, "startup_id", None)
        engine_pid = getattr(tracker, "engine_pid", None)
        if isinstance(startup_id, str) and startup_id:
            payload["startup_id"] = startup_id
        if isinstance(engine_pid, int) and not isinstance(engine_pid, bool):
            payload["pid"] = engine_pid
    # The port the engine binds is taken (#1654): the engine could not start,
    # and a client has to be able to tell that from "still starting" without
    # reading the phase's prose.
    payload["port_in_use"] = _port_in_use(payload.get("phases"))
    latest_version, update_available = await _cached_update_hint(request)
    payload.update({
        "version": __version__,
        "desktop_api_version": 1,
        "latest_version": latest_version,
        "update_available": update_available,
    })
    return JSONResponse(payload)


def _port_in_use(phases: object) -> bool:
    """Whether a failed bind phase is among the reported phases."""
    if not isinstance(phases, list):
        return False
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        if phase.get("name") == BIND_PHASE and phase.get("status") == "failed":
            return True
    return False


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
    session cookie) — see ``_LOOPBACK_ONLY_API`` in ``ciao.web.auth``. It
    returns titles and workspace names, so it must not be reachable from the network.
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
        if is_memory_pass_chat(chat, project):
            # The menu bar is a notification surface: a memory pass is app
            # housekeeping and must not reach its count or rows, blocked or not.
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


async def setup_status_endpoint(request: Request) -> JSONResponse:
    """Return first-run setup readiness for the onboarding wizard."""
    payload = await asyncio.to_thread(setup_status, request.app.state.config)
    return JSONResponse(payload)



def _interactive_foreground_run() -> bool:
    """True when setup can hand the bootstrap server to launchd.

    A process started without a terminal attached still owns the one-time
    onboarding process and must hand the configured server to the LaunchAgent
    when setup completes.
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
    if not service_backend.current_backend().schedule_server_handoff():
        return False
    print(
        "\nSetup complete — Ciaobot is moving to the background service.\n"
        "You can close this terminal; the server now starts automatically at login.\n",
        file=sys.stderr,
        flush=True,
    )
    return True


def _publish_setup_environment(workspace: Path, port: int, password: str) -> None:
    # Hand the chosen workspace to the relaunched process. A foreground
    # `ciao run` restarts by re-execing itself with the current environment,
    # and nothing else tells the fresh process where setup landed — without
    # this it boots straight back into the bootstrap wizard.
    os.environ["CIAO_WORKSPACE"] = str(workspace)
    os.environ["PWA_PORT"] = str(port)
    # Same reason for the credentials: `load_dotenv` does not override values
    # already in the environment, so a stale PWA_AUTH_TOKEN inherited from the
    # bootstrap process would outrank the password just written to .env.
    os.environ["PWA_AUTH_TOKEN"] = password


async def setup_finish_endpoint(request: Request) -> JSONResponse:
    """Write real setup config from bootstrap mode and request supervisor restart."""
    config = request.app.state.config
    refusal = _setup_finish_refusal(request)
    if refusal is not None:
        return refusal
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "json object is required"}, status_code=400)

    restart = bool(body.get("restart", True))
    # The wizard's primary question is the workspace: one root folder holding
    # the vault (memory-vault/ by default) plus app data, all one git repo.
    # vault_root is optional and only set when the second brain lives
    # elsewhere (existing notes folder).
    workspace = str(body.get("workspace", "")).strip()
    if not workspace:
        return JSONResponse({"error": "workspace is required"}, status_code=400)
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

    from ciao.cli import detect_vault_mode, select_setup_vault, setup_workspace, validate_external_setup_vault

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

    resolved_workspace = Path(workspace).expanduser().resolve()
    vault_root_arg = str(body.get("vault_root", "")).strip() or None
    notes_folder = str(body.get("notes_folder", "")).strip()
    notes_identity = None
    if notes_folder:
        try:
            notes = validate_external_setup_vault(resolved_workspace, notes_folder)
            if str(notes) != str(body.get("notes_resolved", "")):
                raise ValueError("notes folder changed since inspection; inspect it again")
            vault_root_arg = str(notes)
            notes_stat = notes.stat()
            notes_identity = (notes_stat.st_dev, notes_stat.st_ino)
            if str(body.get("notes_identity", "")) != f"{notes_stat.st_dev}:{notes_stat.st_ino}":
                raise ValueError("notes folder changed since inspection; inspect it again")
            vault_mode = "existing"
        except (OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    # Read-only preflight: the same selection inspection reported. An ambiguous
    # or unusable location is refused here, before any claim, write or exit.
    # Nothing between this call and the claim below awaits, so the claim stays
    # single-flight.
    try:
        selection = select_setup_vault(
            resolved_workspace, vault_mode=vault_mode, vault_root=vault_root_arg
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if notes_folder and "PWA_AUTH_TOKEN" in selection.existing_env and selection.existing_env["PWA_AUTH_TOKEN"] != password:
        return JSONResponse({"error": "Use the password chosen when setup began."}, status_code=400)

    # setup_workspace only writes workspace files and the bundled-engine
    # LaunchAgent. The release installer owns app downloads, so setup remains
    # responsive and never reaches out to GitHub.
    setup_kwargs: dict[str, Any] = {
        "auth_token": password,
        "vault_root": vault_root_arg,
        "vault_mode": vault_mode,
        "workspace_name": workspace_name,
        "default_provider": default_provider,
        "port": port,
        "_vault_selection": selection,
        "_adopt_exact_vault": bool(notes_folder),
        "_external_identity": notes_identity,
        "_complete_external_setup": not bool(notes_folder),
    }
    setup_kwargs["python_path"] = str(body.get("python", "")).strip() or None
    setup_kwargs["launch_agents_dir"] = str(body.get("launch_agents_dir", "")).strip() or None
    setup_kwargs["app_dir"] = str(body.get("app_dir", "")).strip() or None
    transaction = None
    try:
        if notes_folder:
            from ciao.setup_transaction import SetupTransaction

            transaction = SetupTransaction(resolved_workspace, notes)
            transaction.begin(password, notes_identity)
            setup_kwargs["_transaction"] = transaction
            setup_kwargs["_vault_selection"] = select_setup_vault(
                resolved_workspace, vault_mode=vault_mode, vault_root=vault_root_arg
            )
        written = await asyncio.to_thread(
            functools.partial(setup_workspace, str(resolved_workspace), **setup_kwargs)
        )
        _publish_setup_environment(resolved_workspace, port, password)
        if transaction is not None:
            transaction.complete()
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        if transaction is not None:
            transaction.close()
    # Only the real per-user LaunchAgents dir may be registered with launchd —
    # scripted/test setups pass a custom dir and must not touch it. App mode is
    # the third exclusion: the Mac app owns the bundled engine, so setup must
    # not hand it to launchd on restart. Nothing
    # menu-bar related happens here any more: Ciaobot.app is the menu bar, and
    # setup_workspace above has just retired the old `com.ciao.menubar` agent.
    from ciao.package_version import running_from_app_bundle

    real_launch_agents = (
        sys.platform == "darwin"
        and not str(body.get("launch_agents_dir", "")).strip()
        and not running_from_app_bundle()
    )

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
        "workspace": str(resolved_workspace),
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
    return _dir_listing_response(request)


def _dir_listing_response(request: Request) -> JSONResponse:
    """The folder-picker listing for ``?path=``, shared by every picker route."""
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
    from ciao import cli
    from ciao.cli import detect_vault_mode, select_setup_vault, validate_external_setup_vault
    from ciao.setup_status import (
        cloud_sync_warning,
        detect_nested_workspaces,
        macos_access_note,
    )

    guard = _setup_fs_guard(request)
    if guard is not None:
        return guard
    raw = str(request.query_params.get("path") or "").strip()
    if not raw:
        return JSONResponse({"error": "path is required"}, status_code=400)
    target = _resolve_setup_dir(raw)
    if target is None:
        return JSONResponse({"error": f"not a directory: {raw}"}, status_code=400)
    notes_raw = str(request.query_params.get("notes_folder") or "").strip()
    if notes_raw:
        try:
            notes = validate_external_setup_vault(target, notes_raw)
            notes_stat = notes.stat()
        except (OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"path": str(target), "notes_resolved": str(notes),
                             "notes_identity": f"{notes_stat.st_dev}:{notes_stat.st_ino}",
                             "cloud_sync_warning": cloud_sync_warning(target)})
    # The same read-only selection finish and direct setup use, so the wizard
    # reports the location the finish will write. An ambiguous or unusable
    # location is refused here, with the same message finish returns.
    mode = detect_vault_mode(target)
    try:
        selection = select_setup_vault(target, vault_mode=mode)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    vault_root = selection.vault_path
    if selection.registered_vaults is not None:
        nested = [name for name, _ in selection.registered_vaults]
    else:
        nested = detect_nested_workspaces(vault_root) if mode == "existing" else []
    return JSONResponse({
        "path": str(target),
        "mode": mode,
        "vault_root": str(vault_root),
        "existing_workspaces": nested,
        "cloud_sync_warning": cloud_sync_warning(target),
        "macos_access_note": (
            macos_access_note(target) if cli._verified_host_if_installed() is not None else ""
        ),
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


# ── Moving the install workspace ─────────────────────────────────────────
#
# Settings → Advanced → Ciaobot folder. Every route but the status read is for the
# computer running Ciaobot only: browsing folders and moving the install are
# filesystem operations a phone or a remote browser has no business making.
# "Local" is the setup routes' rule (`_localhost_request`: a loopback TCP peer
# whose Host names a local address, including the accepted 0.0.0.0 spelling)
# plus an exact same-origin Origin, so a DNS-rebound page on this machine
# cannot ride the session cookie.


def host_browser_request(request: Request) -> bool:
    """A browser on the computer running Ciaobot: loopback TCP peer, local Host, exact same-origin Origin/Referer.

    The browser's own origin must match this request's origin in scheme, host
    and effective port — a page on `http://localhost:3000` is a different
    origin from `http://localhost:8443`, and it is not this engine's own page.
    `Origin` is the browser's declaration and wins; `Referer` is only read
    when there is no `Origin`. An empty header carries no origin information,
    so it reads as absent. A request with neither is not a browser request
    and keeps the peer/Host gating alone.

    Narrows a session route; grants nothing by itself (AuthMiddleware still requires the session).
    """
    if not _localhost_request(request):
        return False
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True
    try:
        scheme = urlsplit(source).scheme.lower()
    except ValueError:
        return False
    if scheme not in {"http", "https"} or scheme != request.url.scheme.lower():
        return False
    # `_same_origin` compares the host and the effective port; a proxy-declared
    # host cannot stand in for the browser's own origin here, because a request
    # carrying proxy headers never reaches this point (`is_loopback_client`).
    return _same_origin(request, source)


def _workspace_move_local(request: Request) -> bool:
    return host_browser_request(request)


def _workspace_move_guard(request: Request) -> JSONResponse | None:
    if _workspace_move_local(request):
        return None
    return JSONResponse(
        {"error": "Moving the workspace is only available on the computer running Ciaobot."},
        status_code=403,
    )


async def workspace_move_status_endpoint(request: Request) -> JSONResponse:
    """Where the workspace is, whether this browser may move it, and the last move."""
    from ciao import workspace_move
    from ciao.package_version import running_from_app_bundle
    from ciao.setup_status import cloud_sync_warning

    op = await asyncio.to_thread(workspace_move.read_operation)
    root = Path(request.app.state.config.workspace_root).resolve()
    return JSONResponse(
        {
            "workspace_root": str(root),
            "cloud_sync_warning": cloud_sync_warning(root),
            "local": _workspace_move_local(request),
            # On Linux the service account cannot stop its own root-owned
            # unit; the administrator moves it with `sudo ciao workspace-move`.
            "admin_only": sys.platform.startswith("linux"),
            # In Ciaobot for Mac the engine moves the workspace itself: it
            # drains, restarts in place and repoints the app-owned pointer.
            "app": running_from_app_bundle(),
            "operation": asdict(op) if op is not None else None,
        }
    )


async def workspace_move_dirs_endpoint(request: Request) -> JSONResponse:
    """The folder picker's listing, for choosing where the workspace goes."""
    guard = _workspace_move_guard(request)
    if guard is not None:
        return guard
    return _dir_listing_response(request)


async def _workspace_move_target(request: Request) -> str | JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "json object is required"}, status_code=400)
    return str(body.get("target", "")).strip()


async def workspace_move_plan_endpoint(request: Request) -> JSONResponse:
    """Dry run: whether the move is possible and what to know first."""
    from ciao import workspace_move

    guard = _workspace_move_guard(request)
    if guard is not None:
        return guard
    target = await _workspace_move_target(request)
    if isinstance(target, JSONResponse):
        return target
    source = Path(request.app.state.config.workspace_root)
    move_plan = await asyncio.to_thread(workspace_move.plan_for, source, target)
    return JSONResponse(move_plan.as_dict())


async def workspace_move_start_endpoint(request: Request) -> JSONResponse:
    """Start the move. The engine stops, moves and restarts; the page reloads.

    In Ciaobot for Mac the move is recorded (409 when another move holds
    ``move.lock``) and drained as a task. There is no cancel while it drains:
    up to 600 s, with new-chat admission closed. Chats paused on a question
    form are not counted by ``active_chat_ids`` (same as the Settings restart
    drain), so the restart can interrupt them.
    """
    from ciao import workspace_move

    guard = _workspace_move_guard(request)
    if guard is not None:
        return guard
    if sys.platform.startswith("linux"):
        # On Linux the move runs in the caller's process, not as a sibling
        # job: started from here it would stop the engine running it, even
        # when the engine runs as root and the plan lets it through.
        return JSONResponse(
            {"error": "On Linux the administrator moves the workspace with `sudo ciao workspace-move`."},
            status_code=403,
        )
    target = await _workspace_move_target(request)
    if isinstance(target, JSONResponse):
        return target
    source = Path(request.app.state.config.workspace_root)
    move_plan = await asyncio.to_thread(workspace_move.plan_for, source, target)
    if not move_plan.ok:
        return JSONResponse({**move_plan.as_dict(), "error": " ".join(move_plan.refusals)}, status_code=409)
    from ciao.package_version import running_from_app_bundle

    if running_from_app_bundle():
        # In Ciaobot for Mac no launchd job is ever started: the engine
        # performs the move itself in the gap of its own restart. Record the
        # move (holding move.lock) off the event loop, then drain chats and
        # scheduled work as a task; the drain records `stopping` and requests
        # the engine's own restart, whose pre-boot pass moves the folder and
        # whose verify marks the record `done` once this engine is ready.
        from ciao import workspace_move_app

        try:
            op, handle = await asyncio.to_thread(
                workspace_move_app.record,
                move_plan,
                port=request.app.state.config.pwa_port,
            )
        except workspace_move.MoveError as exc:
            # Another move holds move.lock, or the pointer vanished: a conflict.
            return JSONResponse({"error": str(exc)}, status_code=409)
        request.app.state.workspace_move_task = asyncio.create_task(
            workspace_move_app.drain_then_restart(
                op,
                handle,
                pcm=request.app.state.project_chat_manager,
                schedules=getattr(request.app.state, "schedule_manager", None),
                request_restart=request.app.state.request_restart,
            )
        )
        return JSONResponse({"operation": asdict(op)}, status_code=202)
    try:
        op = await asyncio.to_thread(
            workspace_move.start, move_plan, port=request.app.state.config.pwa_port
        )
    except workspace_move.MoveError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"operation": asdict(op)}, status_code=202)


# ── Admin ────────────────────────────────────────────────────────────────


async def _snapshot_git(
    args: list[str], *, cwd: Path, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run one snapshot git step and never outlive it by being cancelled.

    ``asyncio.to_thread`` cannot be recalled once its worker thread is running:
    the ``await`` raises ``CancelledError``, the thread keeps going, and the git
    child goes on writing the index. The route would unwind at once,
    ``repository_mutation`` would release, and the backup, a manual sync or the
    conflict resync could start its own git in the same checkout while the
    abandoned step was still in flight — the exact overlap the lock exists to
    prevent, reachable by a client that simply disconnects mid-snapshot.

    So the call runs as its own task, shielded from the request's cancellation,
    and a cancelled route waits for that task to finish before re-raising. The
    wait absorbs further cancellation: a second ``cancel()`` arriving while
    cleanup is in progress must not detach the thread again and reopen the
    race. Cancellation still propagates, and because the route is inside the
    lock the whole time, the step that was in flight is the last one to run —
    no status, commit or push follows it.
    """
    step: asyncio.Task[subprocess.CompletedProcess[str]] = asyncio.create_task(
        asyncio.to_thread(
            subprocess.run,
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_env(),
        )
    )
    try:
        return await asyncio.shield(step)
    except asyncio.CancelledError:
        while not step.done():
            try:
                await asyncio.shield(step)
            except asyncio.CancelledError:
                # A repeated cancel: keep waiting, the thread is still git's.
                continue
            except Exception:
                # The step itself failed while we were being cancelled; the
                # route reports the cancellation, and the lock comes free in
                # the same unwind.
                break
        raise


async def admin_snapshot(request: Request) -> JSONResponse:
    """Trigger a git snapshot (add, commit, push).

    The whole add/status/commit/push transaction runs inside
    ``git_mutation.repository_mutation`` for ``config.workspace_root``. This
    route is a fourth writer in one checkout — manual sync, the post-conflict
    resync and the background backup are the other three — and a snapshot that
    interleaves its git commands with one of theirs races the same index, so
    the lock is taken before the preflight gate (what was checked is what gets
    committed) and held through the response.

    A failed step is a failure. ``git status`` exiting nonzero used to read as
    "nothing to commit", because git writes an error to stderr and leaves
    stdout empty, and the ``git commit`` return code was never looked at, so a
    snapshot that captured nothing answered 200 and went on to push. Both now
    return 500, and a failed commit never reaches the push.

    A cancelled request is not a shorter transaction. :func:`_snapshot_git`
    keeps an in-flight git step alive through the cancellation and waits for it
    here, inside the lock, so a disconnect cannot hand the checkout to another
    writer while git is still writing to it.
    """
    mgr = getattr(request.app.state, "local_session_manager", None)
    confirm_warnings = False
    try:
        body = await request.json()
        confirm_warnings = bool(body.get("confirm_warnings", False))
    except ValueError:
        pass

    config = request.app.state.config
    ws = config.workspace_root

    async with repository_mutation(ws):
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

        # A git operation Ciaobot did not start belongs to whoever started it.
        # A stale-looking index.lock may be a real commit on another machine
        # path, so the refusal is reported, never worked around by removing it.
        try:
            await asyncio.to_thread(ensure_mutable, ws)
        except RepositoryBusyError as exc:
            return JSONResponse({"error": exc.detail}, status_code=500)

        try:
            result = await _snapshot_git(["git", *EXACT_BYTES, "add", "-A"], cwd=ws, timeout=30)
            if result.returncode != 0:
                return JSONResponse({"error": f"git add failed: {result.stderr}"}, status_code=500)

            status = await _snapshot_git(
                ["git", *EXACT_BYTES, "status", "--porcelain"], cwd=ws, timeout=10
            )
            # Checked before the emptiness test: a failing status is empty
            # stdout, and reading that as a clean tree is how a snapshot
            # reported "nothing to commit" while git had refused outright.
            if status.returncode != 0:
                return JSONResponse({"error": f"git status failed: {status.stderr}"}, status_code=500)
            if not status.stdout.strip():
                return JSONResponse({"ok": True, "message": "Nothing to commit"})

            from datetime import UTC, datetime
            ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
            commit = await _snapshot_git(
                ["git", *EXACT_BYTES, "commit", "-m", f"pwa snapshot {ts}"], cwd=ws, timeout=30
            )
            # No push after a failed commit: a rejected commit (no identity, a
            # full disk, a pre-commit hook) leaves nothing recorded, and
            # pushing then reported a snapshot that does not exist.
            if commit.returncode != 0:
                return JSONResponse({"error": f"git commit failed: {commit.stderr}"}, status_code=500)

            push = await _snapshot_git(["git", *EXACT_BYTES, "push"], cwd=ws, timeout=60)

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
            "replaced by the development deploy action. Ciaobot for Mac updates its "
            "bundled engine together with the app."
        )
    return ""


def _resolve_codebase_root(config) -> Path:
    """Where the deploy steps run git, pip, and npm.

    The source checkout setting (Settings → Advanced → Developer) wins over the
    module path for source-checkout deploys. A
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
    return run_step(args, cwd=str(codebase_root), timeout=180)


def _restart_only(config) -> bool:
    """Whether Settings' Restart must only restart, never redeploy from source.

    Redeploy (``admin_deploy``) pulls, pip-installs, and rebuilds a source
    checkout, so it only makes sense for a developer running from one. A
    packaged Ciaobot.app never qualifies: its embedded runtime is not a
    checkout, and ``pip install -e`` cannot replace it even when the source
    checkout setting names one. Linux hosts are administrator-managed (the
    service account owns neither the checkout nor the virtualenv, docs/LINUX.md)
    and restart only. Everywhere else, anything that is not a deployable
    checkout (a plain package install) restarts only too, since deploy would
    stop at "locate checkout".
    """
    from ciao.package_version import detect_install_mode

    if detect_install_mode() in ("bundled_app", "installer"):
        return True
    if sys.platform.startswith("linux"):
        return True
    if config is None:
        return False
    return bool(_checkout_problem(_resolve_codebase_root(config)))


_BUNDLED_DEPLOY_REFUSAL = (
    "This engine runs from the installed Ciaobot.app, not a source checkout, "
    "so there is nothing to pull or rebuild. Use Restart to restart it; updates "
    "come from the Ciaobot app (Sparkle)."
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


# The updater addresses the engine as `localhost`, and it is the only caller
# allowed to. A page that rebinds DNS to a name resolving to 127.0.0.1 is a
# loopback *peer* whose Origin matches its own Host, so the origin check alone
# does not exclude it — and these routes need no session. The Host check
# closes that: the name in the request is the only part of a rebound request
# the attacker controls, so it has to be one this engine serves.


def _forbidden_host(request: Request) -> JSONResponse | None:
    """The 403 a request for a host this engine does not serve gets, or None."""
    host = (request.url.hostname or "").lower()
    if host in LOOPBACK_HOST_NAMES:
        return None
    return JSONResponse({"error": "forbidden host"}, status_code=403)


async def _drain_owner(request: Request) -> str | None:
    """The ``owner`` a drain request carries, or None when it names none.

    A drain belongs to exactly one operation, so both drain routes refuse a
    request that names no owner (an older, ownerless client must upgrade).
    A missing, empty or malformed document is "no owner".
    """
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    owner = body.get("owner")
    return owner if isinstance(owner, str) and owner else None


async def admin_drain(request: Request) -> JSONResponse:
    """Close admission for new turns (update drain); loopback-only, no session.

    The updater's foreground half calls this before it hands the rest of the
    transaction to the detached updater job, so a turn started after the drain
    began cannot extend the downtime it is waiting out. The active chat IDs
    come back in the same response the engine already publishes, so the caller
    does not have to make a second request to find out what it is waiting for.

    A ``{"owner": "<operation id>"}`` body is required: it names the operation
    the drain belongs to, which is what lets only that owner poll or cancel it.
    A request without one is a 400, and one naming another owner while a drain
    is held is a 409. The answer carries ``schedule_work_count``, the scheduled
    work still running; a caller treats a missing count as "not idle".

    Refused while a Settings restart is already draining: that restart owns the
    same flag, and taking it over would let the update's cancel reopen
    admission underneath a restart that is still waiting for its chats. The
    update's own repeated calls (it polls by asking again) are idempotent.
    """
    refusal = _forbidden_host(request)
    if refusal is not None:
        return refusal
    pcm = getattr(request.app.state, "project_chat_manager", None)
    if pcm is None:
        return JSONResponse({"error": "drain unavailable"}, status_code=503)
    owner = await _drain_owner(request)
    if owner is None:
        return JSONResponse({"error": "drain owner required"}, status_code=400)
    if pcm.restart_draining and not getattr(request.app.state, "update_drain_active", False):
        return JSONResponse({"error": "a restart is already draining"}, status_code=409)
    claimed_owner = str(getattr(pcm, "drain_owner", "") or "")
    if pcm.restart_draining and claimed_owner and claimed_owner != owner:
        return JSONResponse({"error": "drain owned by another operation"}, status_code=409)
    request.app.state.update_drain_active = True
    pcm.begin_restart_drain(owner)
    answer: dict[str, Any] = {"draining": True, "active_chat_ids": pcm.active_chat_ids()}
    schedules = getattr(request.app.state, "schedule_manager", None)
    if schedules is not None:
        # Omitted without a manager: clients read a missing count as "not idle".
        answer["schedule_work_count"] = schedules.work_count()
    return JSONResponse(answer)


async def admin_drain_cancel(request: Request) -> JSONResponse:
    """Reopen admission after an update's drain timed out; no session.

    Without this a drain that never completes would leave the running engine
    refusing turns forever, which is a far worse outcome than the update it was
    meant to enable. Only an update's own drain is cancelled: one that is not
    the engine's to cancel is left exactly as it is and reported as such, so a
    caller cannot reopen admission under a pending Settings restart.

    A body naming a different ``owner`` is refused with 409 and changes
    nothing: the drain belongs to that operation, and reopening it here would
    let a stale or unrelated caller drop a claim newer work is counting on.
    """
    refusal = _forbidden_host(request)
    if refusal is not None:
        return refusal
    pcm = getattr(request.app.state, "project_chat_manager", None)
    if pcm is None:
        return JSONResponse({"error": "drain unavailable"}, status_code=503)
    owner = await _drain_owner(request)
    if owner is None:
        return JSONResponse({"error": "drain owner required"}, status_code=400)
    if not getattr(request.app.state, "update_drain_active", False):
        return JSONResponse({"draining": pcm.restart_draining})
    # Only the owner reopens its drain: a cancel naming another owner is the
    # steal the owner token exists to refuse.
    claimed_owner = str(getattr(pcm, "drain_owner", "") or "")
    if claimed_owner and owner != claimed_owner:
        return JSONResponse(
            {"error": "drain owned by another operation"}, status_code=409
        )
    if not pcm.cancel_restart_drain(owner):
        return JSONResponse(
            {"error": "drain owned by another operation"}, status_code=409
        )
    request.app.state.update_drain_active = False
    return JSONResponse({"draining": False})


async def admin_restart_blockers(request: Request) -> JSONResponse:
    """Chats a restart/stop would interrupt, reason-tagged. Loopback without session (ciao service gate) or with session (Settings)."""
    pcm = getattr(request.app.state, "project_chat_manager", None)
    if pcm is None:
        return JSONResponse({"restart_blockers": []})
    return JSONResponse({"restart_blockers": pcm.restart_blockers()})


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
            f"{problem}. Set the source checkout in Settings → Advanced → Developer "
            "to the ciaobot checkout so Restart can "
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
        run_step, [sys.executable, "-m", "pip", "install", "-e", "."],
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
        run_step, ["npm", "run", "build"],
        cwd=str(web_dir), timeout=120,
    )
    steps.append(_record_step("npm build", result))
    if result.returncode != 0:
        return JSONResponse(
            {"steps": steps, "ok": False, "error": f"npm build failed: {steps[-1]['output']}"},
            status_code=500,
        )

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
        fn = getattr(request.app.state, "request_restart", None)
        if callable(fn):
            fn(RESTART_EXIT_CODE)
        else:
            raise RestartRequested(RESTART_EXIT_CODE)

    asyncio.create_task(_do_restart())
    steps.append({"step": "restart", "ok": True, "output": ""})

    return JSONResponse({"steps": steps, "ok": True})


async def admin_skills(request: Request) -> JSONResponse:
    """List skills known to Ciaobot, labelled as custom or GitHub/package.

    With `?workspace=<name>`, only that workspace's own agent root. The PWA
    Settings → Skills tab passes the active workspace, so the list is the
    catalog the user can actually act on: editing or deleting a row writes
    into the same root. Stock skills still appear, because `sync-skills`
    installs them into every agent root.

    Without the parameter the listing stays merged across every agent root,
    which is what the audit and CLI callers want. Reading `workspace_root`
    alone showed `{custom: 0, github: 0, stock: 29}` on a migrated install —
    measured — while 19 custom and 7 upstream skills sat in the primary
    root's catalog, so a bare install root is never the answer.

    A skill of the same name in two roots is reported once, with the workspaces
    that hold it, because the page is a catalog rather than a per-root listing
    and two rows for one name reads as a duplicate rather than as sharing.
    """
    config = request.app.state.config
    requested = request.query_params.get("workspace", "").strip()
    if requested and config.workspace(requested):
        roots = [(agent_root_for(config, requested), requested)]
    else:
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
    # exhaust temporary disk before the per-file cap below is ever applied. A
    # missing or malformed Content-Length (chunked/HTTP2 clients) is rejected
    # too: without it there is no cheap pre-parse bound, and a legitimate zip
    # upload always carries the header.
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
    # An upload with no workspace lands in the primary workspace's skills.
    workspace_name = workspace_name or config.primary_workspace()
    dest_skills: Path = (
        config.agent_root(workspace_name) if workspace_name else config.workspace_root
    ) / "skills"

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


def _host_general_project(config, pcm) -> Any:
    """The General project a system-opened chat should be hosted in, or None.

    Any workspace can host one; prefer the primary, then settle for the first
    workspace that has a General project. Keying on a workspace named
    "personal" meant the whole sync-conflict flow failed on installs whose
    workspaces are named anything else, and the same reasoning applies to the
    backup setup chat — both are opened by the app rather than by the user, so
    neither may depend on a workspace being called anything in particular.
    """
    project = next(
        (p for p in pcm.list_projects(config.primary_workspace()) if p.name == "General"),
        None,
    )
    if project is not None:
        return project
    for candidate in config.workspace_names():
        project = next(
            (p for p in pcm.list_projects(candidate) if p.name == "General"), None
        )
        if project is not None:
            return project
    return None


def _open_merge_chat(request: Request, branch: str) -> dict:
    """Open an interactive chat that resolves sync conflicts on ``branch``
    with the user. Returns {ok, chat_id, project_id} or {error}."""
    config = request.app.state.config
    pcm = request.app.state.project_chat_manager
    project = _host_general_project(config, pcm)
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
    config = getattr(request.app.state, "config", None)
    status["restart_only"] = _restart_only(config)
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


# ── Unattended memory backup (ciao/backup_service) ──
#
# Three routes over ONE service instance — the same object the five-minute loop
# holds, so a manual run and a scheduled tick cannot overlap and cannot report
# differently. Each is session-protected like every other /api route; the
# service itself refuses to run on a Mac that is not the host, and says so in
# the status rather than backing up where the host already does.


def _backup_service(request: Request):
    return getattr(request.app.state, "backup_service", None)


async def local_backup_status(request: Request) -> JSONResponse:
    """What the backup service knows: state, scope, and the last success.

    Always 200 — a data root with no remote is a state this endpoint reports,
    not a failure of the endpoint. Read-only: it never stages, commits or
    pushes.
    """
    service = _backup_service(request)
    if service is None:
        return JSONResponse(
            {"error": "backup service not initialised"}, status_code=500
        )
    return JSONResponse((await service.status()).as_dict())


async def local_backup_settings(request: Request) -> JSONResponse:
    """Turn the backup off/on, or pause/resume it. Persists across a restart.

    Body: ``{"enabled": bool}``, ``{"paused": bool}``, or both. An empty body is
    a 400 rather than a silent no-op, so a caller that meant to pause cannot
    read "200" as "paused".
    """
    service = _backup_service(request)
    if service is None:
        return JSONResponse(
            {"error": "backup service not initialised"}, status_code=500
        )
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = None
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": "send a JSON object with 'enabled' and/or 'paused'"},
            status_code=400,
        )
    unknown = sorted(set(body) - {"enabled", "paused"})
    if unknown or not body:
        return JSONResponse(
            {"error": f"expected 'enabled' and/or 'paused'; got {unknown or 'nothing'}"},
            status_code=400,
        )
    if any(not isinstance(value, bool) for value in body.values()):
        return JSONResponse(
            {"error": "'enabled' and 'paused' must be booleans"}, status_code=400
        )
    service.set_flags(
        enabled=body.get("enabled"),
        paused=body.get("paused"),
    )
    return JSONResponse((await service.status()).as_dict())


async def local_backup_run(request: Request) -> JSONResponse:
    """Back up now, through the same serialized path the five-minute loop uses.

    200 when the run left the repository in a state that needs nothing from the
    caller (``ready``, ``pending``, ``paused``), and 400 when it could not do
    its job — no repository, no remote, a refused credential, an unreachable
    remote. The body is the same status object either way, so the reason is in
    the response rather than only in a log line.
    """
    service = _backup_service(request)
    if service is None:
        return JSONResponse(
            {"error": "backup service not initialised"}, status_code=500
        )
    status = await service.run_backup(source="manual")
    settled = status.state in {
        backup_service.STATE_READY,
        backup_service.STATE_PENDING,
        backup_service.STATE_PAUSED,
    }
    return JSONResponse(status.as_dict(), status_code=200 if settled else 400)


async def local_backup_setup_prompt(request: Request) -> JSONResponse:
    """The canonical setup prompt, plus the context it was rendered from.

    One prompt, two actions: this is what a Settings surface copies, and the
    exact same text is what ``POST /api/local/backup/setup-chat`` dispatches.
    Returning the context beside it is what lets a UI show "backing up
    /path/to/data" without parsing prose out of a prompt.

    Cheap and entirely local — every fact is a configuration read or a git
    read that never contacts the remote — so a Settings page may poll it and
    readiness still comes from ``GET /api/local/backup`` alone.
    """
    service = _backup_service(request)
    if service is None:
        return JSONResponse(
            {"error": "backup service not initialised"}, status_code=500
        )
    config = request.app.state.config
    context = await asyncio.to_thread(backup_service.setup_context, config)
    return JSONResponse(
        {
            "context": context,
            "prompt": backup_service.render_setup_prompt(config, context),
        }
    )


async def local_backup_setup_chat(request: Request) -> JSONResponse:
    """Open (or re-enter) a setup chat and send the prompt into it.

    Sends, never drafts: the whole point of the in-app action is that the
    owner does not have to copy, paste, and press enter, so this returns once
    the prompt is on its way to the provider.

    Idempotent on purpose. A retry after a dropped response, a double tap, or
    a reload mid-setup all land here, and each would otherwise spawn another
    agent working the same repository at the same time. A live setup chat is
    found by its stable title and re-entered instead; only a chat the owner
    archived (or deleted) is replaced. The prompt is not re-sent into a chat
    that already has it — that would queue a second identical turn.

    Readiness is not decided here. The chat does the work, the service
    re-reads the repository's real state, and ``GET /api/local/backup``
    reports what it finds — which is also how an external agent's setup is
    detected. A guided setup that verifies turns the service on, unless the
    owner has paused it.
    """
    service = _backup_service(request)
    if service is None:
        return JSONResponse(
            {"error": "backup service not initialised"}, status_code=500
        )
    config = request.app.state.config
    pcm = request.app.state.project_chat_manager
    project = _host_general_project(config, pcm)
    if project is None:
        return JSONResponse(
            {"error": "no General project in any workspace to host the setup chat"},
            status_code=500,
        )

    live = next(
        (
            chat
            for chat in reversed(pcm.list_chats(project.project_id))
            if chat.title == backup_service.SETUP_CHAT_TITLE and not chat.archived
        ),
        None,
    )
    if live is not None:
        # Re-arming a live chat is harmless: the answer is owed until the
        # repository actually verifies, and a retry must not consume it.
        service.begin_guided_setup()
        return JSONResponse(
            {
                "ok": True,
                "chat_id": live.chat_id,
                "project_id": project.project_id,
                "reused": True,
            }
        )

    context = await asyncio.to_thread(backup_service.setup_context, config)
    # No model is pinned: the chat runs on the host workspace's own provider
    # and model, which is where the owner's configured agent already is. A
    # pinned Claude model would name a model the opencode backend does not
    # have, and setup is not the place to fail on that.
    chat = pcm.create_chat(
        project.project_id,
        title=backup_service.SETUP_CHAT_TITLE,
    )
    pcm.start_stream(chat.chat_id, backup_service.render_setup_prompt(config, context))
    service.begin_guided_setup()
    return JSONResponse(
        {
            "ok": True,
            "chat_id": chat.chat_id,
            "project_id": project.project_id,
            "reused": False,
        }
    )


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


async def usage_stats_endpoint(request: Request) -> JSONResponse:
    """Usage statistics for one workspace: ``?workspace=<name>&range=7|30|90|365``.

    Computed by :mod:`ciao.usage_stats` from the stores that already exist.
    The registry facts are copied here on the event loop; the file reads run in
    a worker thread. Completed days are cached in memory, today is live.
    """
    from ciao import usage_stats

    config = request.app.state.config
    workspace = request.query_params.get("workspace", "").strip()
    if not workspace or config.workspace(workspace) is None:
        return JSONResponse(
            {"error": "workspace must name a registered workspace"}, status_code=400
        )
    try:
        range_days = usage_stats.parse_range(request.query_params.get("range"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    sources = usage_stats.snapshot_sources(
        config, request.app.state.project_chat_manager, workspace
    )
    data = await asyncio.to_thread(usage_stats.compute_usage_stats, sources, range_days)
    return JSONResponse(data)


# ── Operator-action housekeeping strip ───────────────────────────────────


def _housekeeping_context(request: Request) -> "operator_actions.DetectionContext":
    """Build the cheap detection context from the request's app state.

    The package-status fetcher is the cached one the app already owns (see
    ``make_cached_package_status`` in ``app.py``), so detection never blocks
    on GitHub. The schedule manager, when present, exposes the missed one-time
    reminders. ``provider_checks_fetcher`` is the cached, offline provider-CLI
    check (``provider_versions.make_cached_checks``); absent, the provider
    update tile stays silent.
    """
    from ciao import operator_actions

    config = request.app.state.config
    fetcher = getattr(request.app.state, "package_status_fetcher", None)
    provider = getattr(request.app.state, "provider_checks_fetcher", None)
    return operator_actions.DetectionContext(
        config=config,
        schedule_store=getattr(request.app.state, "schedule_manager", None),
        package_status=fetcher if callable(fetcher) else None,
        provider_checks=provider if callable(provider) else None,
    )


async def list_housekeeping(request: Request) -> JSONResponse:
    """Return every detectable operator action for the home strip.

    This is the detector pass. Each action carries ``run_label``, ``chat_label``
    and ``chat_prompt`` so the client can render the buttons it needs and seed
    a chat without a second round-trip.
    """
    from ciao import operator_actions

    context = _housekeeping_context(request)
    actions = operator_actions.detect_actions(context)
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

    Only ask-style and advisory actions (the GitHub star nudge, a provider
    update) accept a dismissal; it
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


async def addresses_endpoint(request: Request) -> JSONResponse:
    """Where other devices can open this engine, trusted HTTPS first.

    Session-protected: it enumerates LAN interfaces. URLs never carry a
    password or setup token; every device signs in on its own.
    """
    from ciao.network_addresses import (
        is_loopback_url,
        server_addresses,
        tailscale_serve_urls,
    )

    config = request.app.state.config
    port = int(getattr(config, "pwa_port", 8443) or 8443)
    entries: list[dict[str, object]] = []
    # Tailscale Serve's HTTPS name never appears on an interface, so it is
    # asked for directly.
    for url in await asyncio.to_thread(tailscale_serve_urls, port):
        entries.append(
            {
                "url": url,
                "kind": "trusted",
                "source": "tailscale",
                "secure": True,
                "loopback": False,
            }
        )
    urls = await asyncio.to_thread(server_addresses, port)
    for url in urls:
        if is_loopback_url(url):
            continue
        entries.append({"url": url, "kind": "lan", "secure": False, "loopback": False})
    for url in urls:
        if is_loopback_url(url):
            entries.append({"url": url, "kind": "loopback", "secure": False, "loopback": True})
    return JSONResponse({"port": port, "addresses": entries})
