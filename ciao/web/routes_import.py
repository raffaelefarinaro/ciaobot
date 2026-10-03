"""The import consent surface: what is on this machine, and what a selection would process.

Two routes, both behind the signed session cookie like every other ``/api/*``
route (``ciao.web.auth.AuthMiddleware`` covers the prefix, so neither is
registered in a public or loopback-only allowlist and neither has an origin
exception of its own):

* ``GET /api/import/sources?workspace=`` — discovery. Metadata only: source ids,
  hints, paths, the excluded rows with their reasons, the sources that could not
  be listed at all, and whether a listing cap was reached. No conversation is
  opened and no character of one is returned.
* ``POST /api/import/preview`` — the confirmation payload for the **selected**
  refs: per-conversation counts, the source's own first date when it has one,
  what the reader omitted, the effective provider and model, an input-volume
  estimate and the per-batch cap. It reads the selected files, and nothing else.

**Neither route extracts anything.** There is no model call, no proposal write
and no batch store here: extraction is C7 and the store is C6. The preview exists
so that everything a person is consenting to is stated *before* the first model
call, and it answers with counts and reasons rather than text — a full
transcript never reaches the browser, so a selection screen cannot leak the
conversation it is asking about.

**The selection names a source, not a location.** A request body carries
``{"provider", "source_id"}`` pairs; every path is rebuilt from the workspace's
configured root inside :mod:`ciao.import_discover`, and a session id that is not
a plain file name is refused. There is no route parameter through which a caller
could hand this module a path of its own.

Discovery and preview both run in a worker thread: the Claude Code listing is a
directory read and the OpenCode one shells out to the V2 CLI under a 60s timeout,
and neither belongs on the event loop that serves the WebSockets.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.import_decouple import RegistrySnapshotError
from ciao.import_discover import (
    MAX_SELECTION,
    UnknownWorkspace,
    discover_sources,
    preview_selected,
)
from ciao.import_sources import KNOWN_PROVIDERS, SourceRef
from ciao.web.routes_helpers import api_error

logger = logging.getLogger(__name__)


async def import_sources(request: Request) -> JSONResponse:
    """Every conversation this workspace's known sources hold, as metadata.

    Answers ``{"workspace", "sources"}`` where ``sources`` is the whole
    :class:`~ciao.import_discover.DiscoveryResult`: the offered refs, the excluded
    ones with their reasons, the sources that could not be listed, and the
    per-source ``truncated`` flags.

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
    payload = result.to_json()
    return JSONResponse({"workspace": workspace, "sources": payload})


async def import_preview(request: Request) -> JSONResponse:
    """What the selected conversations would process, before anything does.

    Body: ``{"workspace": "<name>", "sources": [{"provider", "source_id"}, ...]}``.
    The pairs are ids, not locations: :func:`ciao.import_discover.preview_selected`
    rebuilds every path from the workspace's configured root and refuses a session
    id that is not a plain file name.

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