"""``/api/tasks*``: the session-authenticated task board surface.

Every task route is a thin wrapper over one workspace-scoped service — the same
``workspace_task_*`` methods in ``ciao.control_plane`` that back ``ciao task …``
on the agent surface — so the board a browser drives and the board an agent
reads cannot drift apart in what they are allowed to do. This module is
transport only: request parsing, the workspace check, and the mapping of one
typed refusal to one HTTP status. No task rule lives here, and nothing here
opens a task file: the store does that, from the workspace's authoritative vault
root.

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
    # A well-formed request asking for something the record cannot do — editing a
    # value the store does not hold, delegating a task that is already in Done.
    # A 400: the request was understood and refused on its own terms.
    "invalid_task": 400,
    "task_invalid": 400,
    "task_unsupported_schema": 400,
    # A stale revision is a conflict the caller resolves by re-reading, not a
    # malformed request: the request was well formed and its content moved on.
    "task_revision_conflict": 409,
    "task_completion_requires_user": 403,
    # A delegated chat the manager would not create, or a turn it would not
    # start: a server fault with a way forward, and retryable because the reply
    # names the attempt to resume or retry.
    "chat_create_failed": 500,
    "task_launch_failed": 500,
    # The delegated chat is finishing a turn and will not take the update yet.
    # Nothing was sent and nothing was rebound, so it is the same conflict the
    # stale-revision case is: re-read, then send again.
    "task_update_busy": 409,
    "task_attempt_not_found": 404,
    # Resume against a chat that was archived or deleted: there is no
    # conversation to continue, and the way on is a retry in a new chat.
    "attempt_chat_archived": 409,
    "task_report_not_holder": 403,
    "task_read_failed": 500,
}

#: Task fields a ``PATCH`` may carry. A key outside this set is a 400 rather
#: than a store refusal, so a typo in a body is not reported as a broken task.
_PATCHABLE = ("title", "status", "project_id", "due", "assignee")

#: Body keys every write may carry beyond the patchable fields.
_WRITE_KEYS = ("workspace", "expected_revision", "body")

#: Body keys ``POST /delegate`` may carry. Deliberately its own set rather than
#: ``_WRITE_KEYS``: a delegation carries no editable task fields at all. The
#: prompt is built server-side from the record; ``instructions`` is the user's
#: note for this hand-over, quoted after the description in its own fence, so it
#: can shape how the task is done but cannot replace the task.
_DELEGATE_KEYS = ("workspace", "expected_revision", "project_id", "instructions")

#: Body keys ``POST …/attempt/{attempt_id}/update`` may carry. Its own set rather
#: than a reuse: an update carries the message the user approved and the revision it
#: was made at, and nothing else. There is no key here through which a request could
#: move the task, change its linkage or delegate it again.
_UPDATE_KEYS = ("workspace", "expected_revision", "message")


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


def _resolution(body: dict[str, Any]) -> str | None | JSONResponse:
    """The completion text a write carries, or the 400 to answer with.

    Absent means ``None`` (no note). Present must be a string: an explicit
    ``null``, a number, an object or a list is refused rather than stringified,
    so a client cannot record ``"None"`` or ``"{}"`` as a resolution.
    """
    if "resolution" not in body:
        return None
    value = body["resolution"]
    if not isinstance(value, str):
        return _refusal("invalid_task", "resolution must be a string", 400)
    return value


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
    """One workspace's board rows; ``?workspace=`` names the workspace.

    ``?completed_since=`` and ``?completed_before=`` (ISO-8601) narrow the list to
    tasks with a completion in ``[since, before)``. A bound that does not parse is
    a 400, not an ignored filter.
    """
    plane = _control_plane(request)
    if plane is None:
        return _unavailable()
    workspace = _workspace(request.app.state.config, request.query_params.get("workspace", ""))
    if workspace is None:
        return _workspace_required()
    try:
        # In a thread: a list reads and parses every task file, which is the
        # event loop's time to spend, not its own.
        rows = await asyncio.to_thread(
            plane.workspace_task_list,
            workspace,
            completed_since=request.query_params.get("completed_since"),
            completed_before=request.query_params.get("completed_before"),
        )
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
    """Edit one task at ``expected_revision``; only the fields sent change.

    ``resolution`` is the one extra field: it rewords the latest completion of a
    task that is already done. It is not a body replacement and it does not
    move ``completed_at``. A task that is not done is refused for it.
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
    unknown = sorted(
        key
        for key in body
        if key not in _WRITE_KEYS and key not in _PATCHABLE and key != "resolution"
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
    resolution = _resolution(body)
    if isinstance(resolution, JSONResponse):
        return resolution
    if not changes and description is None and resolution is None:
        return _refusal(
            "nothing_to_change",
            "Send at least one editable field, a body, or a resolution.",
            400,
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
            resolution=resolution,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return JSONResponse({"workspace": workspace, "task": task})


async def task_delegate(request: Request) -> JSONResponse:
    """Hand one task to the agent: ``{"workspace", "expected_revision", "project_id",
    "instructions"}``.

    200 with the attempt, the chat it runs in and the task as it now stands.
    ``created: false`` means a live attempt already existed and **nothing** was
    created or sent — a double click or a race with the agent gets the attempt
    that is already running, never a second chat. ``changed_since_delegated``
    says the task was edited after this attempt was handed over, which is a fact
    the reviewer has to see rather than something the route resolves.

    Deliberately not ``asyncio.to_thread``: ``start_stream`` creates an asyncio
    task and is only legal on the event loop, exactly as ``start_update_task``
    notes. The store writes it makes are short, locked file operations.

    A task already in *Done* is ``invalid_task`` — a 400. Delegation hands a task
    over as ``in_progress``/``agent``, so delegating a finished one would reopen
    the card behind the user's back; moving it out of *Done* is their gesture.
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
    unknown = sorted(key for key in body if key not in _DELEGATE_KEYS)
    if unknown:
        return _refusal(
            "invalid_task_field",
            f"Not part of a delegation: {', '.join(unknown)}.",
            400,
        )
    instructions = body.get("instructions", "")
    if not isinstance(instructions, str):
        # Quoted into the prompt as typed, so a list or object must not be
        # stringified into its repr and handed to the agent as the user's words.
        return _refusal(
            "invalid_task_field", "Delegation instructions must be text.", 400
        )
    try:
        outcome = plane.workspace_task_delegate(
            workspace,
            str(request.path_params.get("task_id") or ""),
            expected_revision=revision,
            project_id=str(body.get("project_id")) if body.get("project_id") else None,
            actor="user",
            instructions=instructions,
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return JSONResponse({"workspace": workspace, **outcome})


async def task_send_update(request: Request) -> JSONResponse:
    """Send the edited task into one attempt's own chat: ``{"workspace",
    "expected_revision", "message"}``.

    200 with the rebound attempt, the task as it now stands and whether the message
    was queued behind a turn already in flight. The rebind is the part the board
    needs and a composer send could not do: the attempt is bound to the revision the
    update was made at, so ``changed_since_delegated`` comes back ``false`` and the
    control retires. A refused send rebinds nothing, which is what keeps a cleared
    flag from ever claiming an update the agent did not receive.

    Not a delegation: no chat is created, no attempt is minted, the task's linkage
    is untouched, and the turn is attended exactly as the delegated one was — the
    same ``start_stream`` with no ``unattended``.

    Awaited, not run in a thread: it starts a turn, which is only legal on the
    loop, exactly as ``task_delegate``'s is.
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
    unknown = sorted(key for key in body if key not in _UPDATE_KEYS)
    if unknown:
        return _refusal(
            "invalid_task_field",
            f"Not part of a task update: {', '.join(unknown)}.",
            400,
        )
    try:
        outcome = plane.workspace_task_send_update(
            workspace,
            str(request.path_params.get("task_id") or ""),
            str(request.path_params.get("attempt_id") or ""),
            expected_revision=revision,
            message=str(body.get("message") or ""),
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return JSONResponse({"workspace": workspace, **outcome})


async def task_attempt_action(request: Request) -> JSONResponse:
    """One gesture on one attempt: ``stop``, ``resume``, ``retry`` or ``detach``.

    ``{"workspace"}`` is the only body key. The action is in the path, so a typo
    is a 404 from the router rather than a refusal the surface invented, and the
    service maps the four real verbs.

    ``stop`` is irreversible and is answered like any other write here: it settles
    the attempt and leaves the task linked, so completion stays refused until the
    user detaches it. ``resume`` continues the same chat under the same attempt;
    ``retry`` starts a new one in a new chat. Neither completes a task, and this
    is the user's session so ``detach`` is what makes completion possible at all.

    ``task_id`` is passed to the service rather than read here: the attempt id is
    the only thing this surface would otherwise act on, so a URL naming task A with
    task B's attempt would stop B's turn. The service answers a mismatch with
    ``task_attempt_not_found`` — a 404 — because from this URL there is no such
    attempt.

    Awaited, not run in a thread: ``stop`` and ``detach`` call the chat manager's
    ``async`` ``stop_chat``, which is only legal on the loop, exactly as
    ``task_delegate``'s ``start_stream`` is.
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
    try:
        outcome = await plane.workspace_task_attempt_action(
            workspace,
            str(request.path_params.get("attempt_id") or ""),
            str(request.path_params.get("action") or ""),
            task_id=str(request.path_params.get("task_id") or ""),
            actor="user",
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return JSONResponse({"workspace": workspace, **outcome})


async def task_attempts(request: Request) -> JSONResponse:
    """One task's whole attempt history, the live attempt first.

    The board draws a badge from the live attempt alone, which the task rows
    already carry. This is the read behind "what did we try", so a retry is
    visibly a second attempt rather than the first one running again.
    """
    plane = _control_plane(request)
    if plane is None:
        return _unavailable()
    workspace = _workspace(
        request.app.state.config, request.query_params.get("workspace", "")
    )
    if workspace is None:
        return _workspace_required()
    try:
        history = await asyncio.to_thread(
            plane.workspace_task_attempts,
            workspace,
            str(request.path_params.get("task_id") or ""),
        )
    except ControlPlaneError as exc:
        return _error(exc)
    return JSONResponse({"workspace": workspace, **history})


async def task_complete(request: Request) -> JSONResponse:
    """Mark one task done, at ``expected_revision``.

    Reachable here because this is the signed-in user's own session; the same
    operation through the agent CLI is refused by the store. A task whose turn is
    still in flight is refused too — stop or detach it first. A task whose attempt
    has a result waiting for review is the exception the delegation block handles
    itself: approving Done releases the linkage and closes the task in one
    gesture, leaving the attempt as the ``ready_for_review`` record it is.
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
    resolution = _resolution(body)
    if isinstance(resolution, JSONResponse):
        return resolution
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
            resolution=resolution,
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