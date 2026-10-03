"""Session-authenticated webhook trigger management routes (#1001, child A2 of #974).

Management only: list, create, update, rotate and delete over ``WebhookStore``.
There is deliberately no ingress here — no ``/hooks/*`` receiver, no bearer
auth, no request recipe — that is the A3 child's design. Every route reads the
session cookie through the shared ``AuthMiddleware`` like every other
``/api/*`` route; a raw secret appears only in the create and rotate
responses, exactly once, and never in a list, an error, or a log.

``project_id`` is shape-checked here and nowhere else: the store validates its
form, not that the workspace has such a project. Binding it to a real project
is the dispatch service's job (A4), which must check it before a trigger runs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ciao.webhooks import (
    INVALID_TRIGGER,
    NOT_FOUND,
    REVISION_CONFLICT,
    WebhookStore,
    WebhookStoreError,
)


def webhook_store(config: Any) -> WebhookStore:
    """The engine's trigger store: ``<runtime>/webhooks.json``.

    The one place that path is built. Exported because the workspace-archive
    hook in ``routes_api`` revokes the same file, and two spellings of it would
    be free to drift.

    Constructed per call. The store is cheap and cross-process-safe by design
    (every mutation re-reads the file under its locks), so no ``app.state``
    wiring is needed.
    """
    return WebhookStore(Path(config.state_path).parent / "webhooks.json")


def _store(request: Request) -> WebhookStore:
    """The request's trigger store. See :func:`webhook_store`."""
    return webhook_store(request.app.state.config)


def _store_error(exc: WebhookStoreError) -> JSONResponse:
    """Map a store refusal onto its status: 404/409/400, else 500."""
    if exc.code == NOT_FOUND:
        return JSONResponse({"error": str(exc)}, status_code=404)
    if exc.code == REVISION_CONFLICT:
        return JSONResponse({"error": str(exc)}, status_code=409)
    if exc.code == INVALID_TRIGGER:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"error": str(exc)}, status_code=500)


def _require_registered_workspace(config: Any, workspace: Any) -> str | None:
    """The error for a missing or unregistered workspace, or ``None`` when ok."""
    name = workspace.strip() if isinstance(workspace, str) else ""
    if not name or config.workspace(name) is None:
        return "unknown workspace: expected a registered workspace name"
    return None


async def _read_dict_body(request: Request) -> dict[str, Any] | JSONResponse:
    """The request JSON when it is an object, else the 400 to answer with."""
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected an object"}, status_code=400)
    return body


async def webhook_list(request: Request) -> JSONResponse:
    """List one workspace's triggers. Public records only: never secrets."""
    config = request.app.state.config
    error = _require_registered_workspace(
        config, request.query_params.get("workspace", "")
    )
    if error is not None:
        return JSONResponse({"error": error}, status_code=400)
    workspace = str(request.query_params.get("workspace", "")).strip()
    try:
        # In a thread: every store call takes a file lock and fsyncs, which is
        # the event loop's time to spend, not its own.
        rows = await asyncio.to_thread(_store(request).list, workspace)
    except WebhookStoreError as exc:
        return _store_error(exc)
    return JSONResponse({"triggers": [row.to_dict() for row in rows]})


async def webhook_create(request: Request) -> JSONResponse:
    """Store a new (disabled) trigger and return it with its one-time secret."""
    config = request.app.state.config
    body = await _read_dict_body(request)
    if isinstance(body, JSONResponse):
        return body
    error = _require_registered_workspace(config, body.get("workspace"))
    if error is not None:
        return JSONResponse({"error": error}, status_code=400)
    arguments: dict[str, Any] = {
        "name": body.get("name"),
        "workspace": str(body.get("workspace")).strip(),
        "instructions": body.get("instructions"),
    }
    if body.get("project_id") is not None:
        arguments["project_id"] = body.get("project_id")
    if body.get("mode") is not None:
        arguments["mode"] = body.get("mode")
    try:
        trigger, secret = await asyncio.to_thread(_store(request).create, **arguments)
    except WebhookStoreError as exc:
        return _store_error(exc)
    return JSONResponse(
        {"trigger": trigger.to_dict(), "secret": secret}, status_code=201
    )


async def webhook_update(request: Request) -> JSONResponse:
    """Change a trigger's name, instructions or enabled flag, revision-checked."""
    body = await _read_dict_body(request)
    if isinstance(body, JSONResponse):
        return body
    trigger_id = str(request.path_params.get("trigger_id", ""))
    arguments: dict[str, Any] = {
        "expected_revision": body.get("expected_revision")
    }
    for field in ("name", "instructions", "enabled"):
        if field in body:
            arguments[field] = body[field]
    try:
        updated = await asyncio.to_thread(_store(request).update, trigger_id, **arguments)
    except WebhookStoreError as exc:
        return _store_error(exc)
    return JSONResponse({"trigger": updated.to_dict()})


async def webhook_rotate(request: Request) -> JSONResponse:
    """Replace a trigger's secret and return it with the new one-time secret."""
    body = await _read_dict_body(request)
    if isinstance(body, JSONResponse):
        return body
    trigger_id = str(request.path_params.get("trigger_id", ""))
    expected_revision: Any = body.get("expected_revision")
    try:
        updated, secret = await asyncio.to_thread(
            _store(request).rotate_secret,
            trigger_id,
            expected_revision=expected_revision,
        )
    except WebhookStoreError as exc:
        return _store_error(exc)
    return JSONResponse({"trigger": updated.to_dict(), "secret": secret})


async def webhook_delete(request: Request) -> Response:
    """Delete a trigger and its verifier, revision-checked. Answers 204."""
    trigger_id = str(request.path_params.get("trigger_id", ""))
    try:
        expected_revision = int(request.query_params.get("expected_revision", ""))
    except (TypeError, ValueError):
        return JSONResponse(
            {"error": "expected_revision is required as an integer query parameter"},
            status_code=400,
        )
    try:
        await asyncio.to_thread(
            _store(request).delete,
            trigger_id,
            expected_revision=expected_revision,
        )
    except WebhookStoreError as exc:
        return _store_error(exc)
    return Response(status_code=204)
