"""``/api/tasks*``: the session-authenticated task board surface.

Six routes over one workspace-scoped service — the same ``workspace_task_*``
methods in ``ciao.control_plane`` that back ``ciao task …`` on the agent
surface — so the board a browser drives and the board an agent reads cannot
drift apart in what they are allowed to do. This module is transport only:
request parsing, the workspace check, and the mapping of one typed refusal to
one HTTP status. No task rule lives here, and nothing here opens a task file:
the store does that, from the workspace's authoritative vault root.

Three properties are structural rather than conventional:

**No request names a root.** A route may only name a *workspace*, and the
service resolves that name to its own vault (``workspace_vault_root``) — the
same resolution ``_vault_root`` uses for an agent principal. A caller cannot
point this surface at a directory it likes.

**A write presents the revision it read.** ``PATCH``, ``DELETE`` and
``complete`` all require ``expected_revision`` and refuse a stale one with a
409, writing nothing. A board that drew a card from an older read therefore
cannot silently overwrite what is on disk now.

**Completing is the user's act, and this is the user's session.** The browser
route acts as ``actor="user"``, so ``complete`` works here; the same operation
through the agent CLI acts as ``actor="agent"`` and is refused by the store.
Nothing here relaxes that rule — it changes who is calling.
"""

from __future__ import annotations

import asyncio
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.control_plane import ControlPlaneError

#: One status per application error the task service can raise. The codes are
#: the contract; this table is the only place one becomes a status, so a new
#: refusal cannot pick one by accident. Anything unmapped is a 500 — a refusal
#: this surface does not understand is a bug, not a request it may answer.
_STATUS_BY_CODE = {
    "workspace_not_found": 400,
    "workspace_unavailable": 409,
    "project_not_found": 400,
    "project_ambiguous": 400,
    "invalid_action": 400,
    "task_not_found": 404,
    "task_invalid": 400,
    "task_unsupported_schema": 400,
    # A stale revision is a conflict the caller resolves by re-reading, not a
    # malformed request: the request was well formed and its content moved on.
    "task_revision_conflict": 409,
    "task_completion_requires_user": 403,
    "task_read_failed": 500,
}

#: Task fields a ``PATCH`` may carry. A key outside this set is a 400 rather
#: than a store refusal, so a typo in a body is not reported as a broken task.
_PATCHABLE = ("title", "status", "project_id", "due", "assignee", "review_state")

#: Body keys every write may carry beyond the patchable fields.
_WRITE_KEYS = ("workspace", "expected_revision", "body")


def _control_plane(request: Request) -> Any | None:
    """The bound control plane, or ``None`` before the engine finished booting.

    Same source the agent surface reads, and the same 503 when it is missing:
    first-run bootstrap has no control plane, and a task write against an
    engine that cannot resolve a workspace is a server fault, not a 400.
    """
    service = getattr(request.app.state, "mcp_service", None)
    return getattr(service, "control_plane", None)


def _refusal(code: str, message: str, status: int, *, retryable: bool = False) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "retryable": retryable}},
        status_code=status,
    )


def _unavailable() -> JSONResponse:
    return _refusal(
        "unavailable", "Ciaobot is not ready to serve task operations.", 503, retryable=True
    )


def _workspace_required() -> JSONResponse:
    return _refusal(
        "workspace_required", "A registered workspace name is required.", 400
    )


def _error(exc: ControlPlaneError) -> JSONResponse:
    """One service refusal as this surface's envelope, with its mapped status."""
    return _refusal(exc.code, str(exc), _STATUS_BY_CODE.get(exc.code, 500), retryable=exc.retryable)


def _workspace(config: Any, value: Any) -> str | None:
    """A registered workspace name, or ``None`` when the request named none.

    The whole of the request's authority over where records live: a name that
    the registry does not know is refused here, before any path is built.
    """
    name = value.strip() if isinstance(value, str) else ""
    if not name or config.workspace(name) is None:
        return None
    return name


async def _body(request: Request) -> dict[str, Any] | JSONResponse:
    """The request JSON when it is an object, else the 400 to answer with."""
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return _refusal("invalid_json", "Body must be a JSON object.", 400)
    if not isinstance(body, dict):
        return _refusal("invalid_json", "Body must be a JSON object.", 400)
    return body


def _revision(body: dict[str, Any]) -> str | JSONResponse:
    """The revision a write must present, or the 400 to answer with.

    Required on every write here, including the ones that look harmless: a
    store that overwrote a task nobody had read would be a lost update, and the
    revision is the only thing standing between the board and that.
    """
    revision = str(body.get("expected_revision") or "").strip()
    if not revision:
        return _refusal(
            "expected_revision_required",
            "expected_revision is required: pass the revision you read.",
            400,
        )
    return revision


async def task_list(request: Request) -> JSONResponse:
    """One workspace's board rows; ``?workspace=`` names the workspace."""
    plane = _control_plane(request)
    if plane is None:
        return _unavailable()
    workspace = _workspace(request.app.state.config, request.query_params.get("workspace", ""))
    if workspace is None:
        return _workspace_required()
    try:
        # In a thread: a list reads and parses every task file, which is the
        # event loop's time to spend, not its own.
        rows = await asyncio.to_thread(plane.workspace_task_list, workspace)
    except ControlPlaneError as exc:
        return _error(exc)
    return JSONResponse({"workspace": workspace, "tasks": rows})


async def task_get(request: Request) -> JSONResponse:
    """One task, body included; ``?workspace=`` names the workspace.

    The list deliberately carries no ``body``, so this is the only honest read
    of a description: an editor that shows one and writes it back needs the prose
    it was shown, and a task the board never created has no other source. The
    record comes back exactly as the store reads it now, ``revision`` included,
    so the edit that follows presents the revision this answer carried.
    """
    plane = _control_plane(request)
    if plane is None:
        return _unavailable()
    workspace = _workspace(request.app.state.config, request.query_params.get("workspace", ""))
    if workspace is None:
        return _workspace_required()
    try:
        task = await asyncio.to_thread(
            plane.workspace_task_get,
            workspace,
            str(request.path_params.get("task_id") or ""),
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return JSONResponse({"workspace": workspace, "task": task})


async def task_create(request: Request) -> JSONResponse:
    """File one task: ``{"workspace", "title", "body", "project_id", "due"}``.

    201 with the record as stored, including the ``revision`` the next edit has
    to present.
    """
    plane = _control_plane(request)
    if plane is None:
        return _unavailable()
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    workspace = _workspace(request.app.state.config, body.get("workspace"))
    if workspace is None:
        return _workspace_required()
    title = str(body.get("title") or "").strip()
    if not title:
        return _refusal("title_required", "A task needs a title.", 400)
    try:
        task = await asyncio.to_thread(
            plane.workspace_task_create,
            workspace,
            title=title,
            body=str(body.get("body") or ""),
            project_id=body.get("project_id") or None,
            due=body.get("due") or None,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return JSONResponse({"workspace": workspace, "task": task}, status_code=201)


async def task_update(request: Request) -> JSONResponse:
    """Edit one task at ``expected_revision``; only the fields sent change."""
    plane = _control_plane(request)
    if plane is None:
        return _unavailable()
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    revision = _revision(body)
    if isinstance(revision, JSONResponse):
        return revision
    unknown = sorted(
        key for key in body if key not in _WRITE_KEYS and key not in _PATCHABLE
    )
    if unknown:
        return _refusal(
            "invalid_task_field",
            f"Not editable through this route: {', '.join(unknown)}.",
            400,
        )
    workspace = _workspace(request.app.state.config, body.get("workspace"))
    if workspace is None:
        return _workspace_required()
    changes = {key: body[key] for key in _PATCHABLE if key in body}
    # A `body` that is present but not a string is a no-op rather than an edit,
    # so it does not count as "something to change" either.
    description = body.get("body") if isinstance(body.get("body"), str) else None
    if not changes and description is None:
        return _refusal(
            "nothing_to_change", "Send at least one editable field, or a body.", 400
        )
    try:
        task = await asyncio.to_thread(
            plane.workspace_task_update,
            workspace,
            str(request.path_params.get("task_id") or ""),
            expected_revision=revision,
            changes=changes,
            body=description,
            actor="user",
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return JSONResponse({"workspace": workspace, "task": task})


async def task_complete(request: Request) -> JSONResponse:
    """Mark one task done, at ``expected_revision``.

    Reachable here because this is the signed-in user's own session; the same
    operation through the agent CLI is refused by the store. A task linked to a
    live chat or attempt is still refused — the delegation service's
    stop/detach workflow owns that, not a column.
    """
    plane = _control_plane(request)
    if plane is None:
        return _unavailable()
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    revision = _revision(body)
    if isinstance(revision, JSONResponse):
        return revision
    workspace = _workspace(request.app.state.config, body.get("workspace"))
    if workspace is None:
        return _workspace_required()
    try:
        task = await asyncio.to_thread(
            plane.workspace_task_action,
            workspace,
            "complete",
            str(request.path_params.get("task_id") or ""),
            expected_revision=revision,
            actor="user",
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return JSONResponse({"workspace": workspace, "task": task})


async def task_delete(request: Request) -> JSONResponse:
    """Remove one task record, at ``expected_revision``.

    The record is the user's own Markdown file and this unlinks it — there is
    no trash. ``expected_revision`` is required, so a delete planned against an
    older read is a 409 with the file still there.
    """
    plane = _control_plane(request)
    if plane is None:
        return _unavailable()
    body = await _body(request)
    if isinstance(body, JSONResponse):
        return body
    revision = _revision(body)
    if isinstance(revision, JSONResponse):
        return revision
    workspace = _workspace(request.app.state.config, body.get("workspace"))
    if workspace is None:
        return _workspace_required()
    try:
        result = await asyncio.to_thread(
            plane.workspace_task_delete,
            workspace,
            str(request.path_params.get("task_id") or ""),
            expected_revision=revision,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return JSONResponse({"workspace": workspace, **result})