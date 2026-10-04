"""The import consent surface, the private batch store behind it, and the run.

Seven routes, all behind the signed session cookie like every other ``/api/*``
route (``ciao.web.auth.AuthMiddleware`` covers the prefix, so none is
registered in a public or loopback-only allowlist and none has an origin
exception of its own):

* ``GET /api/import/sources?workspace=`` — discovery. Metadata only: source ids,
  hints, the excluded rows with their reasons, the sources that could not be
  listed at all, and whether a listing cap was reached. No absolute path is sent
  to the browser, no conversation is opened and no character of one is returned.
* ``POST /api/import/preview`` — the confirmation payload for the **selected**
  refs: per-conversation counts, the source's own first date when it has one,
  what the reader omitted, the effective provider and model, an input-volume
  estimate and the per-batch cap. It reads the selected files, and nothing else.
* ``POST /api/import/batches`` — file a batch over a selection (C6).
* ``GET /api/import/batches?workspace=`` — that workspace's batches, with
  progress.
* ``POST /api/import/batches/{id}/run`` — schedule the extraction (C7).
* ``POST /api/import/batches/{id}/cancel`` — stop a batch, keeping what it
  produced.
* ``DELETE /api/import/batches/{id}`` — drop a batch record; queue and vault
  are untouched.

**Only the run route extracts anything.** Every other route here reads no
conversation and calls no model: the preview exists so that everything a person
is consenting to is stated *before* the first model call, and it answers with
counts and reasons rather than text — a full transcript never reaches the
browser, so a selection screen cannot leak the conversation it is asking about.

**The run route starts no turn in the request.** It moves the batch to
``running`` on the C6 store, schedules :func:`ciao.import_run.run_import_batch`
on the event loop and answers immediately with the batch; the extraction
happens after the response, in the runner, where C4 owns every model call. A
batch already ``running`` is answered with its current state rather than a
second run, and one that has settled is a 409. The model and provider are
resolved from the configuration by the runner — no request field chooses what
reads a user's history.

**The selection names a source, not a location.** A request body carries
``{"provider", "source_id"}`` pairs; every path is rebuilt from the workspace's
configured root inside :mod:`ciao.import_discover`, and a session id that is not
a plain file name is refused. An OpenCode id must additionally be one this
workspace's own ``session list`` named, because the CLI resolves ids across
projects. There is no route parameter through which a caller could hand this
module a path of its own.

Discovery and preview both run in a worker thread: the Claude Code listing is a
directory read and the OpenCode one shells out to the V2 CLI under a 60s timeout,
and neither belongs on the event loop that serves the WebSockets.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ciao.import_decouple import (
    RegistrySnapshotError,
    ciaobot_own_session_ids,
)
from ciao.import_discover import (
    MAX_SELECTION,
    UnknownWorkspace,
    discover_sources,
    preview_selected,
)
from ciao.import_sources import KNOWN_PROVIDERS, SourceRef
from ciao.import_store import (
    CONFLICT,
    INVALID_BATCH,
    NOT_FOUND,
    RUNNING,
    ImportStore,
    ImportStoreError,
    engine_store_path,
)
from ciao.web.routes_helpers import api_error

logger = logging.getLogger(__name__)

#: The scheduled extractions this process has running. A strong reference, so a
#: task nobody awaits cannot be collected mid-run, and a task that has finished
#: is dropped from it.
_RUN_TASKS: set[asyncio.Task[None]] = set()


async def import_sources(request: Request) -> JSONResponse:
    """Every conversation this workspace's known sources hold, as metadata.

    Answers ``{"workspace", "sources"}`` where ``sources`` is the whole
    :class:`~ciao.import_discover.DiscoveryResult`: the offered refs, the excluded
    ones with their reasons, the sources that could not be listed, and the
    per-source ``truncated`` flags.

    Each ref is sent as an id and its hint, **without its absolute path**: the
    panel never shows one, and the browser gains nothing from being told where a
    conversation lives inside the user's home directory.

    An unregistered or empty ``workspace`` is a 400 rather than the install root:
    a discovery listing answers "what can be imported *into this workspace*", and
    answering for a different one would offer the wrong history on the wrong vault.

    A source that could not be listed is a row in that answer, not a failure of
    the request: an OpenCode below the enforced V2 floor means "unsupported, export
    a file instead", and reporting it as an empty list would tell the user they
    have no conversations. What *is* a failure is Ciaobot's own registry being
    unreadable — the scan would then be unable to exclude Ciaobot's own sessions —
    and that answers 500 rather than a listing.
    """
    config = request.app.state.config
    workspace = str(request.query_params.get("workspace", "")).strip()
    if not workspace or config.workspace(workspace) is None:
        return api_error("unknown workspace: expected a registered workspace name", 400)
    try:
        result = await asyncio.to_thread(discover_sources, config, workspace)
    except UnknownWorkspace as exc:  # pragma: no cover — checked above
        return api_error(str(exc), 400)
    except RegistrySnapshotError as exc:
        logger.error("import discovery: Ciaobot's own records are unreadable (%s)", exc)
        return api_error(
            "Cannot read Ciaobot's own chat records, so nothing can be excluded "
            "from this listing. Nothing was scanned.",
            500,
        )
    payload = _without_paths(result.to_json())
    return JSONResponse({"workspace": workspace, "sources": payload})


async def import_preview(request: Request) -> JSONResponse:
    """What the selected conversations would process, before anything does.

    Body: ``{"workspace": "<name>", "sources": [{"provider", "source_id"}, ...]}``.
    The pairs are ids, not locations: :func:`ciao.import_discover.preview_selected`
    rebuilds every path from the workspace's configured root, refuses a session id
    that is not a plain file name, and requires an OpenCode id to be one this
    workspace's own listing named.

    Answers ``{"preview": {...}}`` with one row per requested ref — including the
    ones that are refused, each with its reason, because a selection screen has to
    be able to say why a conversation the user can see in their own tool will not
    be processed — and the confirmation payload beside them: the effective
    provider and model, the input-volume estimate, the per-batch cap and the
    destination workspace.

    No transcript text is in the answer. The rows carry counts, dates and the
    reader's omission record; the text stays on the engine host.
    """
    config = request.app.state.config
    body = await _read_json_object(request)
    if isinstance(body, JSONResponse):
        return body
    workspace = str(body.get("workspace") or "").strip()
    if not workspace or config.workspace(workspace) is None:
        return api_error("unknown workspace: expected a registered workspace name", 400)
    selected = body.get("sources")
    if not isinstance(selected, list):
        return api_error("expected a 'sources' list of {provider, source_id} pairs", 400)
    if len(selected) > MAX_SELECTION:
        return api_error(f"a preview covers at most {MAX_SELECTION} conversations", 400)
    try:
        refs = [_selected_ref(item) for item in selected]
    except _BadSelection as exc:
        return api_error(str(exc), 400)

    try:
        preview = await asyncio.to_thread(preview_selected, config, workspace, refs)
    except UnknownWorkspace as exc:  # pragma: no cover — checked above
        return api_error(str(exc), 400)
    except RegistrySnapshotError as exc:
        logger.error("import preview: Ciaobot's own records are unreadable (%s)", exc)
        return api_error(
            "Cannot read Ciaobot's own chat records, so nothing can be excluded. "
            "Nothing was read.",
            500,
        )
    return JSONResponse({"preview": preview.to_json()})


class _BadSelection(ValueError):
    """One requested source is not a ``{provider, source_id}`` pair."""


def _without_paths(payload: dict[str, Any]) -> dict[str, Any]:
    """The listing with every ref's absolute path dropped.

    :meth:`ciao.import_sources.contract.SourceRef.to_json` carries ``path``
    because an engine-side snapshot needs it — but the browser is not an engine
    side. Discovery resolves paths itself, the panel shows an id and a hint, and
    the one thing the path adds to a listing crossing into a browser is the
    user's home directory.

    The shape still comes from ``to_json``; this only removes one key, so the
    two cannot drift into describing different listings.
    """
    for ref in payload["available"]:
        ref.pop("path", None)
    for row in payload["excluded"]:
        row["ref"].pop("path", None)
    return payload


def _selected_ref(item: Any) -> SourceRef:
    """The :class:`SourceRef` a requested pair names.

    A ``SourceRef`` is built rather than trusted: the pair carries no path and no
    hint, because a caller may name a source but not where it lives. The
    ``ContractError`` a malformed pair raises becomes a 400 rather than a 500.
    """
    if not isinstance(item, dict):
        raise _BadSelection("each source must be an object")
    provider = item.get("provider")
    source_id = item.get("source_id")
    if not isinstance(provider, str) or provider not in KNOWN_PROVIDERS:
        raise _BadSelection(f"provider must be one of {list(KNOWN_PROVIDERS)}")
    if not isinstance(source_id, str) or not source_id.strip():
        raise _BadSelection("each source must name a session id")
    return SourceRef(provider=provider, source_id=source_id.strip())


async def _read_json_object(request: Request) -> dict[str, Any] | JSONResponse:
    """The request JSON when it is an object, else the 400 to answer with."""
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return api_error("invalid JSON", 400)
    if not isinstance(body, dict):
        return api_error("expected an object", 400)
    return body


# ── Import batches (C6), and the run that drives them (C7) ─────────────────
#
# Five routes over :mod:`ciao.import_store`, session-cookie gated like every
# other ``/api/*`` route. A request may only *name* a workspace; the store
# path is resolved from the engine config, so no caller-supplied path ever
# reaches the filesystem. Four of them read or move batch records only — the
# create handler reads Ciaobot's own registry (to refuse Ciaobot-own
# sessions) and the batch file, and nothing else. The fifth schedules
# :mod:`ciao.import_run`, which owns the conversation reads and every model
# call; this module starts no turn of its own.


def import_batch_store(config: Any) -> ImportStore:
    """The engine's batch store: ``<runtime>/import/import-batches.json``.

    Resolved through :func:`ciao.import_store.engine_store_path`, the one
    place that path is built — shared with the retention sweep, so the two
    spellings cannot drift.

    Constructed per call. The store is cheap and cross-process-safe by design
    (every mutation re-reads the file under its locks), so no ``app.state``
    wiring is needed.
    """
    return ImportStore(engine_store_path(config))


def _store(request: Request) -> ImportStore:
    """The request's batch store. See :func:`import_batch_store`."""
    return import_batch_store(request.app.state.config)


def _batch_error(exc: ImportStoreError) -> JSONResponse:
    """Map a store refusal onto its status: 404/409/400, else 500."""
    if exc.code == NOT_FOUND:
        return JSONResponse({"error": str(exc)}, status_code=404)
    if exc.code == CONFLICT:
        return JSONResponse({"error": str(exc)}, status_code=409)
    if exc.code == INVALID_BATCH:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"error": str(exc)}, status_code=500)


def _require_registered_workspace(config: Any, workspace: Any) -> str | None:
    """The error for a missing or unregistered workspace, or ``None`` when ok."""
    name = workspace.strip() if isinstance(workspace, str) else ""
    if not name or config.workspace(name) is None:
        return "unknown workspace: expected a registered workspace name"
    return None


async def import_batches_create(request: Request) -> JSONResponse:
    """File a new batch over a selection: ``{"workspace", "sources": [...]}``.

    ``sources`` is one ``{provider, source_id}`` pair per selected
    conversation — ids, never paths — with an optional ``content_digest``
    when the caller already read the session. Answers 201 with the batch as
    stored: its selection, per-source digests (empty until C7 reads them),
    progress and provenance. Digests only, never transcript text.

    A selection naming a Ciaobot-own session is a 400, not a batch: the
    store refuses it against Ciaobot's own records, which are read here (an
    unreadable registry is a 500, like discovery, because the exclusion could
    not be established). A second batch while one is still open for the
    workspace, or a conversation a live batch already covers, is a 409.
    """
    config = request.app.state.config
    body = await _read_json_object(request)
    if isinstance(body, JSONResponse):
        return body
    error = _require_registered_workspace(config, body.get("workspace"))
    if error is not None:
        return api_error(error, 400)
    workspace = str(body.get("workspace")).strip()
    sources = body.get("sources")
    if not isinstance(sources, list):
        return api_error("expected a 'sources' list of {provider, source_id} pairs", 400)
    destination = body.get("destination")
    if destination is not None and not isinstance(destination, str):
        return api_error("destination must be a workspace name", 400)
    if isinstance(destination, str) and (
        not destination.strip() or config.workspace(destination.strip()) is None
    ):
        return api_error(
            "unknown destination: expected a registered workspace name", 400
        )
    try:
        known_own = await asyncio.to_thread(ciaobot_own_session_ids, config, workspace)
    except RegistrySnapshotError as exc:
        logger.error("import batches: Ciaobot's own records are unreadable (%s)", exc)
        return api_error(
            "Cannot read Ciaobot's own chat records, so Ciaobot's own sessions "
            "cannot be excluded. Nothing was filed.",
            500,
        )
    try:
        batch = await asyncio.to_thread(
            _store(request).create,
            workspace=workspace,
            sources=sources,
            destination=destination.strip() if isinstance(destination, str) else None,
            known_own_ids=known_own,
        )
    except ImportStoreError as exc:
        return _batch_error(exc)
    return JSONResponse({"batch": batch.to_json()}, status_code=201)


async def import_batches_list(request: Request) -> JSONResponse:
    """Every batch filed for ``?workspace=``, oldest first, with progress."""
    config = request.app.state.config
    error = _require_registered_workspace(
        config, request.query_params.get("workspace", "")
    )
    if error is not None:
        return api_error(error, 400)
    workspace = str(request.query_params.get("workspace", "")).strip()
    try:
        # In a thread: every store call takes a file lock, which is the event
        # loop's time to spend, not its own.
        batches = await asyncio.to_thread(
            _store(request).list_for_workspace, workspace
        )
    except ImportStoreError as exc:
        return _batch_error(exc)
    return JSONResponse(
        {"workspace": workspace, "batches": [batch.to_json() for batch in batches]}
    )


async def import_batch_run(request: Request) -> JSONResponse:
    """Schedule a filed batch's extraction. Answers 202 with the batch.

    The body names the workspace (``{"workspace": ...}``), and a batch filed for
    another workspace is a 404 rather than a refusal, like the cancel route: a
    caller that may only name a workspace learns nothing about batches outside
    it.

    **No model call happens in this request.** The batch moves to ``running`` on
    the store and :func:`ciao.import_run.run_import_batch` is scheduled on the
    event loop; the answer is the batch as it stands, and the extraction — the
    tool-less turn per selected conversation, and the proposals it files — happens
    after the response. A body naming a model or a provider is ignored on
    purpose: which model reads a user's own history is the configuration's
    answer, and specifically the per-provider insights model
    :func:`ciao.import_run._resolve_call` resolves — which is not necessarily
    the model ``preview_selected`` reported on the consent screen.

    A batch already ``running`` is answered **200 with its current state** rather
    than starting a second run: one batch at a time is C6's rule and this is the
    race two presses of the button would otherwise lose. A batch that has already
    settled — done, failed, cancelled or partial — is a 409 whose outcome stands;
    re-running it is a new batch.
    """
    config = request.app.state.config
    body = await _read_json_object(request)
    if isinstance(body, JSONResponse):
        return body
    error = _require_registered_workspace(config, body.get("workspace"))
    if error is not None:
        return api_error(error, 400)
    workspace = str(body.get("workspace")).strip()
    batch_id = str(request.path_params.get("batch_id", ""))
    store = _store(request)
    try:
        batch = await asyncio.to_thread(store.get, batch_id)
    except ImportStoreError as exc:
        return _batch_error(exc)
    if batch.workspace != workspace:
        return api_error(f"no import batch {batch_id!r} is recorded", 404)
    if batch.status == RUNNING:
        return JSONResponse({"batch": batch.to_json()})
    try:
        started = await asyncio.to_thread(store.begin, batch_id)
    except ImportStoreError as exc:
        if exc.code != CONFLICT:
            return _batch_error(exc)
        # Another press won the race. A batch now running is reported, not
        # refused: the answer is the state, and no second extraction starts.
        try:
            current = await asyncio.to_thread(store.get, batch_id)
        except ImportStoreError as inner:
            return _batch_error(inner)
        if current.status == RUNNING:
            return JSONResponse({"batch": current.to_json()})
        return _batch_error(exc)
    _schedule_run(config, started)
    return JSONResponse({"batch": started.to_json()}, status_code=202)


def _schedule_run(config: Any, batch: Any) -> None:
    """Run an already-begun batch's extraction in the background, and hold the task.

    The batch arrived here straight from :meth:`ImportStore.begin`, so it is
    already ``running`` and this is the only extraction that owns it — the gate
    was taken synchronously, so a second press could be answered rather than
    scheduled. The coroutine absorbs its own failures: a runner that raised into
    the event loop would take the WebSocket server down with it. The batch is
    not left ``running`` by such a failure — the runner settles it ``failed``
    before the exception reaches here — so all that is left to do is log it.
    """
    batch_id = batch.batch_id

    async def _run() -> None:
        from ciao.import_run import run_import_batch

        try:
            result = await run_import_batch(config, batch_id, batch=batch)
        except Exception:  # noqa: BLE001 — the loop must not see a run's failure
            logger.exception("Import batch %s could not be run", batch_id)
            return
        logger.info(
            "Import batch %s settled as %s (%d proposal(s) filed)",
            batch_id,
            result.status,
            result.proposals_filed,
        )

    try:
        task = asyncio.create_task(_run(), name=f"import-run-{batch_id}")
    except RuntimeError:  # pragma: no cover — no loop means no server
        logger.error("Import batch %s has no event loop to run on", batch_id)
        return
    _RUN_TASKS.add(task)
    task.add_done_callback(_RUN_TASKS.discard)


async def import_batch_cancel(request: Request) -> JSONResponse:
    """Stop a batch, keeping what it produced. Idempotent; answers the batch.

    The body names the workspace (``{"workspace": ...}``), and a batch filed
    for another workspace is a 404 rather than a refusal: a caller that may
    only name a workspace learns nothing about batches outside it. A batch
    that already settled as done, failed or partial is a 409 — its outcome
    stands. Cancellation retains recorded progress and provenance and cannot
    unsend provider input.
    """
    config = request.app.state.config
    body = await _read_json_object(request)
    if isinstance(body, JSONResponse):
        return body
    error = _require_registered_workspace(config, body.get("workspace"))
    if error is not None:
        return api_error(error, 400)
    workspace = str(body.get("workspace")).strip()
    batch_id = str(request.path_params.get("batch_id", ""))
    store = _store(request)
    try:
        batch = await asyncio.to_thread(store.get, batch_id)
    except ImportStoreError as exc:
        return _batch_error(exc)
    if batch.workspace != workspace:
        return api_error(f"no import batch {batch_id!r} is recorded", 404)
    try:
        updated = await asyncio.to_thread(store.cancel, batch_id, reason="")
    except ImportStoreError as exc:
        return _batch_error(exc)
    return JSONResponse({"batch": updated.to_json()})


async def import_batch_delete(request: Request) -> Response:
    """Drop a batch record. Answers 204. Queue and vault are untouched.

    ``?workspace=`` is required, and a batch filed for another workspace is
    a 404, like the cancel route. Removing an import never implicitly deletes
    accepted memories: the filed proposals stay queued and the accepted facts
    stay in the vault — that is the existing review/undo path.
    """
    config = request.app.state.config
    error = _require_registered_workspace(
        config, request.query_params.get("workspace", "")
    )
    if error is not None:
        return api_error(error, 400)
    workspace = str(request.query_params.get("workspace", "")).strip()
    batch_id = str(request.path_params.get("batch_id", ""))
    store = _store(request)
    try:
        batch = await asyncio.to_thread(store.get, batch_id)
    except ImportStoreError as exc:
        return _batch_error(exc)
    if batch.workspace != workspace:
        return api_error(f"no import batch {batch_id!r} is recorded", 404)
    try:
        await asyncio.to_thread(store.forget, batch_id)
    except ImportStoreError as exc:
        return _batch_error(exc)
    return Response(status_code=204)
