"""Session-authenticated webhook trigger management routes (#1001, child A2 of #974).

Management routes only, plus the receipt-history reads over the same journal
(#1044): list, create, update, rotate and delete over ``WebhookStore``, and
``GET …/receipts`` over ``WebhookReceiver``. The ingress receiver is
``routes_hooks.py``, which takes a bearer secret and an idempotency key; nothing
here authorizes a machine sender or carries a request recipe. Every route reads
the session cookie through the shared ``AuthMiddleware`` like every other
``/api/*`` route; a raw secret appears only in the create and rotate responses,
exactly once, and never in a list, an error, a receipt row or a log.

The two receipt reads are the one place here that touches the ingress journal,
and they are reads only: no second journal, no write, and nothing that changes
what a sender's next delivery does. Both are workspace-scoped the way the writes
are — a trigger that belongs to another workspace is the same 404 as one that is
not there, so the history is never a way to probe for another workspace's ids.

``project_id`` is shape-checked here and nowhere else: the store validates its
form, not that the workspace has such a project. Binding it to a real project is
the dispatch service's job (A4), which must check it before a trigger runs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ciao.webhooks import (
    INVALID_RECEIPT,
    INVALID_TRIGGER,
    NOT_FOUND,
    RECEIPTS_HISTORY_LIMIT,
    RECEIPT_UNAVAILABLE,
    REVISION_CONFLICT,
    WebhookReceiver,
    WebhookReceiverError,
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


def webhook_receiver(config: Any) -> WebhookReceiver:
    """The engine's receiver, with its journal beside the trigger store.

    Here rather than in ``routes_hooks.py`` because it is built from the store's
    path, and :func:`webhook_store` is what knows that: the ingress route and the
    history reads must not be free to drift onto two different journals. Built
    per call, like the store it reads, so a rotated or revoked secret is visible
    to the very next request.
    """
    return WebhookReceiver(webhook_store(config).path)


#: One status per typed receiver refusal a *read* can raise. The same
#: code-to-status discipline as the ingress route's table, and deliberately its
#: own: a journal that cannot be read is a 503 the caller may retry, and a row
#: this code cannot decode is a 500 rather than a history that quietly omits it.
#: Anything unmapped is a 500 — a refusal this route does not understand is a
#: bug, not a request it may answer.
_RECEIPT_READ_STATUS = {
    RECEIPT_UNAVAILABLE: 503,
    INVALID_RECEIPT: 500,
}


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


def _receipt_error(exc: WebhookReceiverError) -> JSONResponse:
    """Map a typed receiver refusal onto its status. See `_RECEIPT_READ_STATUS`."""
    status = _RECEIPT_READ_STATUS.get(exc.code, 500)
    return JSONResponse({"error": str(exc)}, status_code=status)


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


# ── The receipt history (#1044) ────────────────────────────────────────────
#
# A read surface over the journal the receiver already writes, so a person can
# see what a trigger received and which chat each event became — including the
# one stuck `interrupted`, which nothing else will ever report. Two rules hold
# across both routes and are the reason they are shaped like the management
# routes rather than like the ingress one:
#
# * **The session decides the workspace, and only then is the journal read.**
#   An unregistered or absent `?workspace=` is a 400 before anything else
#   happens; a trigger that is not that workspace's is the same 404 as one that
#   is not there at all. Which of those two it was would otherwise be a way to
#   ask this engine whether an id it did not give you exists anywhere.
# * **A receipt row is a projection.** `WebhookReceipt.to_public_dict` is the only
#   shape that leaves the engine, and it carries no verifier, no idempotency key
#   and no body digest.


def _requested_workspace(request: Request) -> tuple[str, JSONResponse | None]:
    """The validated `?workspace=`, or the 400 to answer with instead."""
    error = _require_registered_workspace(
        request.app.state.config, request.query_params.get("workspace", "")
    )
    if error is not None:
        return "", JSONResponse({"error": error}, status_code=400)
    return str(request.query_params.get("workspace", "")).strip(), None


async def webhook_trigger_receipts(request: Request) -> JSONResponse:
    """List one trigger's recorded events, newest first, capped (#1044).

    `launched` is the success outcome and its `chat_id` is the chat the event
    became, so a caller can link the receipt to an ordinary chat rather than
    searching for one whose title says "New Chat". A receipt that has not
    settled yet is listed as it stands (`accepted` or `launching`), because
    "recorded but not launched" is exactly what an operator has to be able to
    see.
    """
    workspace, refusal = _requested_workspace(request)
    if refusal is not None:
        return refusal
    trigger_id = str(request.path_params.get("trigger_id", ""))
    try:
        trigger = await asyncio.to_thread(_store(request).get, trigger_id)
    except WebhookStoreError as exc:
        return _store_error(exc)
    if trigger.workspace != workspace:
        # The same sentence, and the same status, as an unknown id: the session is
        # scoped to one workspace and this route does not confirm what another
        # workspace has configured.
        return JSONResponse(
            {"error": f"no webhook trigger {trigger_id!r} is configured"},
            status_code=404,
        )
    try:
        receipts = await asyncio.to_thread(
            webhook_receiver(request.app.state.config).recent_receipts_for, trigger_id
        )
    except WebhookReceiverError as exc:
        return _receipt_error(exc)
    return JSONResponse(
        {
            "trigger_id": trigger.trigger_id,
            "trigger_name": trigger.name,
            "limit": RECEIPTS_HISTORY_LIMIT,
            "receipts": [receipt.to_public_dict() for receipt in receipts],
        }
    )


async def webhook_receipts(request: Request) -> JSONResponse:
    """List a workspace's recorded events across every trigger, newest first (#1044).

    The workspace-wide companion to :func:`webhook_trigger_receipts`, and it
    filters on each receipt's own `workspace` rather than on the triggers that
    exist now — so a receipt whose trigger has since been deleted or retargeted
    is still listed under the workspace that received it.
    """
    workspace, refusal = _requested_workspace(request)
    if refusal is not None:
        return refusal
    try:
        receipts = await asyncio.to_thread(
            webhook_receiver(request.app.state.config).recent_receipts, workspace
        )
    except WebhookReceiverError as exc:
        return _receipt_error(exc)
    return JSONResponse(
        {
            "workspace": workspace,
            "limit": RECEIPTS_HISTORY_LIMIT,
            "receipts": [receipt.to_public_dict() for receipt in receipts],
        }
    )
