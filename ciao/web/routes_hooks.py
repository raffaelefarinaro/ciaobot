"""``POST /hooks/v1/{trigger_id}``: the webhook ingress receiver (#1010, child A3).

The machine surface, and it shares nothing with the browser session. A trusted
external sender calls this with **one trigger's own bearer secret**; the PWA
session cookie is not accepted, cannot be, and is not checked — the login
password is not a webhook credential and this route never looks at one.

That is not a stylistic choice, it is the whole reason the route has to own
every control itself: ``AuthMiddleware`` (``ciao/web/auth.py``) protects
``/api/*`` and ``/ws/*`` and nothing else, so a ``/hooks/v1/…`` path gets **no**
session check and **no** ``_state_change_origin_allowed`` check and falls
straight through to the router. What this module therefore does, in this order,
is:

1. ``405`` — only ``POST`` is accepted. The refusal is a second route for the
   same path rather than this endpoint's own method list, because the SPA
   catch-all registered *after* both is a full match for every ``GET`` and would
   otherwise answer the shell; :func:`webhook_method_not_allowed` is where that
   lives, and it runs before anything here is read.
2. ``403`` — a foreign or ``null`` ``Origin``/``Referer`` on a state-changing
   request, using the same predicate ``/api/*`` uses. A browser cannot set the
   ``Authorization`` header without a CORS preflight this origin never answers,
   so this is defence in depth against a caller that has the secret *and* runs
   page script; a machine sender sends neither header and is unaffected.
3. ``401`` — ``Authorization: Bearer <secret>`` checked against
   ``WebhookStore.authenticate(trigger_id, secret)``, *before* the body is read.
   With ``PWA_HOST=0.0.0.0`` this route is reachable from the LAN, so buffering
   an unauthenticated upload first would let anyone fill memory before the 401.
   A trigger that is missing, disabled, revoked or simply wrong all answer 401;
   a store this code cannot read raises and answers **500**, because answering
   "no" to every request would turn a corrupt store into a sender that blames
   its own secret.
4. ``400`` — an ``Idempotency-Key`` header, checked before the body is buffered
   so a request that will be refused anyway costs nothing.
5. ``413`` — the body, bounded twice: once on the declared ``Content-Length``
   (a header the caller controls independently of the bytes it sends) and again
   while reading, because a chunked request declares nothing.
6. ``202`` — a durable receipt, and nothing else. The launch is *scheduled*
   (:func:`schedule_webhook_dispatch`, #1020) rather than awaited: the response
   is a receipt, not a launch result, and a turn may run for minutes.

What a ``202`` does **not** mean: no model turn has run. Dispatch
(``ciao/webhook_dispatch.py``) starts the ordinary chat in the background and
records the outcome in the same journal — ``launched``, ``failed`` or
``interrupted`` — so the response carries ``status`` — ``accepted`` — and no
output, no chat id, and never the word "completed". A sender that needs the
outcome of the turn polls nothing yet; the receipt journal
(``<runtime>/webhook-receipts.jsonl``) is the record, and the chat the launch
created is an ordinary one the operator opens like any other.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ciao.web.auth import _state_change_origin_allowed
from ciao.webhook_dispatch import dispatch_receipt
from ciao.webhooks import (
    ACCEPTED,
    IDEMPOTENCY_CONFLICT,
    INVALID_EVENT,
    INVALID_IDEMPOTENCY_KEY,
    INVALID_RECEIPT,
    MAX_BODY_BYTES,
    PAYLOAD_TOO_LARGE,
    RATE_LIMITED,
    RATE_WINDOW_SECONDS,
    RECEIPT_UNAVAILABLE,
    TOO_MANY_PENDING,
    WebhookReceiver,
    WebhookReceiverError,
    WebhookReceipt,
    WebhookStoreError,
)
from ciao.web.routes_webhooks import webhook_store

logger = logging.getLogger(__name__)

#: Strong references to the dispatch tasks this process started, so the event
#: loop's task GC cannot collect one mid-flight. A dispatch that is garbage
#: collected mid-turn is a receipt stuck ``launched`` — which reads as a crash
#: and needs an operator — so the reference is kept until the task is done and
#: dropped there. The set is the engine's bound on concurrent launches: one
#: entry per accepted receipt, and the receiver's ``MAX_PENDING_RECEIPTS`` (20
#: per trigger) is what keeps the number of live entries finite.
_DISPATCH_TASKS: set[asyncio.Task[object]] = set()

#: One status per typed refusal. The codes are the contract; this table is the
#: only place a reason becomes a status code, so adding a reason cannot
#: accidentally pick one. Anything unmapped is a 500 — a refusal this route does
#: not understand is a bug, not a request it may answer.
_STATUS_BY_CODE = {
    INVALID_EVENT: 400,
    INVALID_IDEMPOTENCY_KEY: 400,
    PAYLOAD_TOO_LARGE: 413,
    IDEMPOTENCY_CONFLICT: 409,
    RATE_LIMITED: 429,
    TOO_MANY_PENDING: 503,
    RECEIPT_UNAVAILABLE: 503,
    INVALID_RECEIPT: 500,
}


def _error(
    code: str, message: str, status: int, *, retryable: bool = False
) -> JSONResponse:
    """The machine-surface error envelope, as ``/agent/v1/{op}`` spells it."""
    return JSONResponse(
        {
            "ok": False,
            "error": {"code": code, "message": message, "retryable": retryable},
        },
        status_code=status,
    )


def _too_large() -> JSONResponse:
    return _error(
        PAYLOAD_TOO_LARGE,
        f"the request body exceeds {MAX_BODY_BYTES} bytes.",
        413,
    )


def _receiver_error(exc: WebhookReceiverError) -> JSONResponse:
    """Map a typed receiver refusal onto its status, with a ``Retry-After``."""
    status = _STATUS_BY_CODE.get(exc.code, 500)
    response = _error(exc.code, str(exc), status, retryable=exc.retryable)
    if status == 429:
        # The window empties a second after its last attempt, so the honest
        # advice is "one window from now", not "whenever".
        response.headers["Retry-After"] = str(RATE_WINDOW_SECONDS)
    return response


def webhook_receiver(config: Any) -> WebhookReceiver:
    """The engine's receiver, with its journal beside the trigger store.

    Built per call, like the store it reads: both are cheap and cross-process
    safe by design (every decision re-reads the file under its locks), so no
    ``app.state`` wiring is needed and a rotated or revoked secret is visible to
    the very next request.
    """
    return WebhookReceiver(webhook_store(config).path)


def schedule_webhook_dispatch(
    request: Request, receipt: WebhookReceipt, receiver: WebhookReceiver
) -> None:
    """Schedule one accepted receipt's launch; never await it.

    The ``202`` is a receipt, not a launch result. A sender must not hold its
    connection open for a turn that may run for minutes and that it has no way
    to read progress from — the receipt *is* the answer to "did you get it", and
    the chat the launch creates is an ordinary one the operator opens.

    So the launch is a background task, and the two ways it can fail to start
    are both logged rather than raised, because there is no longer a request to
    fail:

    * **No chat manager.** This route builds a receiver per call and never
      needed ``app.state``; dispatch does, and an app wired without one (a
      first-run shell, a test harness) has no chat to launch into. The receipt
      stays ``accepted``, which is exactly the open state the startup sweep
      reads, so nothing is lost — it is dispatched later or not at all, and the
      journal says which.
    * **No running loop.** Only reachable outside a request, where the receipt
      stays ``accepted`` for the same reason.
    """
    config = request.app.state.config
    pcm = getattr(request.app.state, "project_chat_manager", None)
    if pcm is None:
        logger.warning(
            "webhook dispatch: no chat manager is wired; receipt %s stays accepted",
            receipt.id,
        )
        return
    try:
        task = asyncio.create_task(
            dispatch_receipt(receiver, webhook_store(config), pcm, receipt.id)
        )
    except RuntimeError:
        logger.debug(
            "webhook dispatch: no running event loop for receipt %s", receipt.id
        )
        return
    _DISPATCH_TASKS.add(task)
    task.add_done_callback(_DISPATCH_TASKS.discard)


async def webhook_method_not_allowed(request: Request) -> Response:
    """``405`` for any method but ``POST`` on the ingress path.

    A second route for one path, and it exists because the SPA catch-all
    registered after it (``Route("/{path:path}", _spa_catchall)``) is a *full*
    match for every ``GET``: Starlette stops at the first full match, so the
    partial match that would have produced its own ``405`` for a
    ``methods=["POST"]`` route is never reached, and a stray ``GET`` on a webhook
    URL would answer the SPA shell instead of refusing the method. The receiver
    owns its method restriction, so it refuses it here rather than letting the
    shell swallow it.
    """
    return Response(status_code=405, headers={"Allow": "POST"})


async def webhook_receive(request: Request) -> JSONResponse:
    """Accept one webhook event for one trigger and answer ``202`` + receipt."""
    # Same predicate as the `/api/*` middleware check, called here because the
    # middleware never sees a `/hooks/` path.
    if not _state_change_origin_allowed(request):
        return _error(
            "forbidden_origin",
            "a cross-origin browser request cannot call the webhook receiver.",
            403,
        )
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    trigger_id = str(request.path_params.get("trigger_id") or "")
    store = webhook_store(request.app.state.config)
    try:
        trigger = await asyncio.to_thread(store.authenticate, trigger_id, token)
    except WebhookStoreError as exc:
        # Fail closed and loudly: a store this engine cannot read is an operator
        # problem, and answering 401 would send the sender off to rotate a secret
        # that is perfectly fine.
        logger.error("webhook ingress: the trigger store could not be read: %s", exc)
        return _error(
            "store_unavailable",
            "the webhook trigger store could not be read.",
            500,
        )
    if trigger is None:
        # Missing trigger, disabled trigger, revoked trigger and wrong secret are
        # one answer: which one it was is not a sender's business.
        return _error("unauthorized", "a valid webhook bearer secret is required.", 401)
    key = request.headers.get("idempotency-key", "").strip()
    if not key:
        return _error(
            INVALID_IDEMPOTENCY_KEY, "an Idempotency-Key header is required.", 400
        )
    declared = request.headers.get("content-length", "")
    # `str.isdigit` is true for non-ASCII digits (`"²"`, `"٣"`) which `int()`
    # then refuses with a ValueError, so a 500 for a header h11/httptools would
    # not normally let through. ASCII-only is the check that matches the parser.
    if declared.isascii() and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return _too_large()
    # Chunked, because a request with no `Content-Length` declares nothing and a
    # cap that trusts the header is no cap at all.
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_BODY_BYTES:
            return _too_large()
        chunks.append(chunk)
    body = b"".join(chunks)
    receiver = webhook_receiver(request.app.state.config)
    try:
        # The receiver validates the body (a JSON object carrying only `text`),
        # dedupes, bounds the trigger and appends the receipt. In a thread: it
        # takes a file lock and fsyncs, which is the event loop's time to spend.
        receipt = await asyncio.to_thread(
            receiver.receive,
            trigger,
            idempotency_key=key,
            body=body,
        )
    except WebhookReceiverError as exc:
        return _receiver_error(exc)
    # A retry answered with the receipt it already has is not a new event, and
    # dispatch is idempotent by receipt state anyway, so this is safe either way
    # — but there is nothing to launch for a retry, so there is nothing to
    # schedule.
    if receipt.status == ACCEPTED:
        schedule_webhook_dispatch(request, receipt, receiver)
    return JSONResponse(
        {
            "receipt_id": receipt.id,
            "trigger_id": receipt.trigger_id,
            "status": receipt.status,
        },
        status_code=202,
    )
