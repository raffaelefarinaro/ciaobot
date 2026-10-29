"""The browser terminal's session routes: list, create and end (#1799).

This is the *wire* half of the terminal, and it is only that. The shells
themselves belong to :mod:`ciao.terminal_sessions`, the one manager that
owns every process, and the access policy belongs to
:mod:`ciao.web.routes_terminal`. This module asks both for what it needs
and re-decides neither: it has no policy of its own, and it owns no shell.

Three routes, and the same three gates in the same order on each of them:

* **the terminal policy** (``terminal_policy``) — the request must come
  from this engine's own page, over a secure transport when it is remote;
* **the signed session** — the terminal is in neither middleware allowlist,
  so a local process cannot skip it;
* **a fresh grant** (``terminal_grant``) — the browser must hold a grant
  for this engine. No grant is ``403 locked``: a caller without one has no
  session to reach, and nothing about the terminal is readable until the
  grant exists.

The order is the one the unlock uses, and it is the order that matters: the
policy comes first, so a request that is not this engine's own page never
has its body read; the session comes second, so an unsigned request never
reaches the grant check; and the grant comes last, so a browser that has
not unlocked never reaches a session.

Nothing here names a path or a command. The caller names a *target* — a
chat or a project — and the folder comes from
:func:`ciao.host_terminal.resolve_folder` alone, which is also what
decides whether the target exists: a ``ValueError`` is a 400, a
``LookupError`` a 404 and a ``FileNotFoundError`` a 409. The window size
is the manager's own; the body says nothing about it, because a body that
could choose a window could choose a command.

A refusal from the manager is typed, and every reason it can carry is
mapped to a status here: ``not_found`` 404, ``capacity`` 409,
``invalid_target`` 400, ``closed`` 409, ``spawn_failed`` 500, and anything
else a 400. The manager's message is the reason and nothing else, so no
fragment of a terminal's output can reach an error string.

The answers carry no output and no connection id. A record is the session
id, the folder, the window, the byte position, whether somebody is
attached, and whether the shell has exited — the facts a client needs to
draw a terminal, and nothing that would let a bystander impersonate the
connection that holds a session. Every response is ``no-store``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao import host_terminal
from ciao.host_terminal import FolderLocation
from ciao.terminal_sessions import (
    TerminalSessionError,
    TerminalSessionInfo,
    TerminalSessionManager,
    TerminalTargetKind,
)
from ciao.web.routes_terminal import (
    MAX_BODY_BYTES,
    _POLICY_MESSAGES,
    _fail,
    _no_store,
    _read_bounded_body,
    _session_ok,
    terminal_grant,
    terminal_policy,
)

#: The shape of a session id. The manager mints ids with
#: ``secrets.token_urlsafe``, so an id is URL-safe base64 and nothing
#: else; anything else is not a session id, and is refused before the
#: manager is asked about it.
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: The status each manager reason is answered with. A reason that is not
#: here is a 400: the request was refused, and nothing about the refusal
#: says the engine is at fault.
_MANAGER_STATUS: dict[str, int] = {
    "not_found": 404,
    "capacity": 409,
    "invalid_target": 400,
    "closed": 409,
    "spawn_failed": 500,
}

#: One line per manager reason, so a refusal says what happened without
#: echoing anything the terminal produced.
_MANAGER_MESSAGES: dict[str, str] = {
    "not_found": "no terminal session carries that id.",
    "capacity": "too many terminal sessions are live; try again later.",
    "invalid_target": "that target cannot hold a terminal session.",
    "closed": "the terminal session has ended.",
    "spawn_failed": "the shell could not be started.",
}

#: What a refusal with no line of its own says.
_DEFAULT_REFUSAL = "the terminal session request was refused."

#: What the locked answer says.
_LOCKED = (
    "there is no terminal grant for this browser; "
    "POST /api/terminal/unlock obtains one."
)


# ── the helpers ───────────────────────────────────────────────────────────


def _manager(request: Request) -> TerminalSessionManager:
    """The app's one terminal session manager.

    There is no fallback and no second registry: the app builds the
    manager, publishes it, and the routes read that one.
    """
    manager: TerminalSessionManager = request.app.state.terminal_sessions
    return manager


def _gate(request: Request) -> JSONResponse | None:
    """The refusal this request earns at the door, or ``None``.

    The three gates, in the unlock's order: the policy, the signed
    session, and a fresh grant. A request that clears all three may go
    on to the session manager; one that does not never reaches it.
    """
    refusal = terminal_policy(request)
    if refusal is not None:
        return _fail(refusal, _POLICY_MESSAGES[refusal], 403)
    if not _session_ok(request):
        return _fail("unauthorized", "a signed-in session is required.", 401)
    if terminal_grant(request) is None:
        return _fail("locked", _LOCKED, 403)
    return None


def _query_target(request: Request) -> tuple[TerminalTargetKind, str] | None:
    """The target the query names, or ``None`` when it names no usable one.

    Exactly one of the two keys may be there: neither names no target at
    all, and both name two, and a caller may only ask about one.
    """
    params = request.query_params
    has_chat = "chat_id" in params
    has_project = "project_id" in params
    if has_chat == has_project:
        return None
    if has_chat:
        return "chat", params["chat_id"]
    return "project", params["project_id"]


def _body_target(
    payload: Any,
) -> tuple[TerminalTargetKind, str, FolderLocation | None] | None:
    """The target the body names, or ``None`` when it names no usable one.

    The body is exactly ``{"chat_id": str}`` or exactly
    ``{"project_id": str}``, optionally with ``"location"`` set to
    ``"workspace"`` or ``"repo"``: a missing key, an unknown key, two target
    keys and a value of the wrong kind are all the same refusal, so a caller
    cannot learn the shape by trying variants. The location picks one of the
    engine's own folders; it never names a path.
    """
    if not isinstance(payload, dict):
        return None
    keys = set(payload)
    location: FolderLocation | None = None
    if "location" in keys:
        keys.discard("location")
        raw_location = payload["location"]
        if raw_location == "workspace":
            location = "workspace"
        elif raw_location == "repo":
            location = "repo"
        else:
            return None
    if keys == {"chat_id"}:
        value = payload["chat_id"]
        return ("chat", value, location) if isinstance(value, str) else None
    if keys == {"project_id"}:
        value = payload["project_id"]
        return ("project", value, location) if isinstance(value, str) else None
    return None


def _resolve_target(
    request: Request, kind: TerminalTargetKind, target_id: str, resolver: Any, **extra: Any
) -> Any:
    """Run one of :mod:`ciao.host_terminal`'s resolvers, or the refusal it earns.

    A target that names nothing is a 400, a target that does not exist is a
    404, and a target whose folder is gone is a 409.
    """
    try:
        return resolver(
            request.app.state.config,
            request.app.state.project_chat_manager,
            chat_id=target_id if kind == "chat" else "",
            project_id=target_id if kind == "project" else "",
            **extra,
        )
    except ValueError as exc:
        return _fail("bad_request", str(exc), 400)
    except LookupError as exc:
        return _fail("not_found", str(exc), 404)
    except FileNotFoundError as exc:
        return _fail("folder_missing", f"folder not found: {exc}", 409)


def _folder_for(
    request: Request,
    kind: TerminalTargetKind,
    target_id: str,
    location: FolderLocation | None = None,
) -> Path | JSONResponse:
    """The folder this target opens in, or the refusal it earns.

    The folder is the engine's own answer and never the caller's: it comes
    from :func:`ciao.host_terminal.resolve_folder`, which is also what
    decides whether the target exists at all.
    """
    result: Path | JSONResponse = _resolve_target(
        request, kind, target_id, host_terminal.resolve_folder, location=location
    )
    return result


def _choices_for(
    request: Request, kind: TerminalTargetKind, target_id: str
) -> dict[FolderLocation, Path] | JSONResponse:
    """The folders this target may open in, or the refusal it earns.

    The same resolution as :func:`_folder_for`, so the list and the create
    agree on whether a target exists and where its shells start.
    """
    result: dict[FolderLocation, Path] | JSONResponse = _resolve_target(
        request, kind, target_id, host_terminal.folder_choices
    )
    return result


def _manager_failure(exc: TerminalSessionError) -> JSONResponse:
    """A refusal from the manager, in the route's own envelope.

    The reason is the manager's, unchanged: it is a closed vocabulary, and
    a client maps it to a status without parsing prose.
    """
    reason = exc.reason
    status = _MANAGER_STATUS.get(reason, 400)
    error = _MANAGER_MESSAGES.get(reason, _DEFAULT_REFUSAL)
    return _fail(reason, error, status)


def _info_json(info: TerminalSessionInfo) -> dict[str, Any]:
    """The session's record, in the terms that are safe to show.

    The folder is a string, because a path is not a JSON value the browser
    should walk; no output bytes and no connection id travel, so a
    bystander cannot impersonate the connection that holds a session.
    """
    return {
        "session_id": info.session_id,
        "folder": str(info.folder),
        "cols": info.cols,
        "rows": info.rows,
        "seq": info.seq,
        "attached": info.attached,
        "exited": info.exited,
        "exit_code": info.exit_code,
    }


# ── the routes ────────────────────────────────────────────────────────────


async def terminal_sessions_list(request: Request) -> JSONResponse:
    """The terminal sessions opened for one chat or one project.

    The list is scoped exactly: a project session and a chat session that
    happen to share an id are two different sessions, and neither appears
    in the other's list. The answer also names the folders a new shell may
    start in (``locations``: the workspace, and the repository when one is
    linked), so the pane can offer the choice. That resolution is also the
    one place that decides whether a target exists — so an unknown chat is
    a 404 here rather than an empty list that looks like "nothing is
    running".
    """
    refusal = _gate(request)
    if refusal is not None:
        return refusal
    target = _query_target(request)
    if target is None:
        return _fail(
            "bad_request", "name exactly one of chat_id or project_id.", 400
        )
    kind, target_id = target
    choices = _choices_for(request, kind, target_id)
    if isinstance(choices, JSONResponse):
        return choices
    try:
        sessions = _manager(request).list_for(kind, target_id)
    except TerminalSessionError as exc:
        return _manager_failure(exc)
    return _no_store(
        JSONResponse(
            {
                "sessions": [_info_json(info) for info in sessions],
                "locations": [
                    {"location": location, "folder": str(folder)}
                    for location, folder in choices.items()
                ],
            }
        )
    )


async def terminal_sessions_create(request: Request) -> JSONResponse:
    """Open a shell for one chat or one project.

    The body names the target and, optionally, which of the engine's own
    folders to start in (``workspace`` or ``repo``); the path comes from the
    engine, the window comes from the manager, and the shell that is
    started is the manager's own. The slot is reserved before the shell
    exists, so a full manager refuses the request instead of starting a
    fifth process.
    """
    refusal = _gate(request)
    if refusal is not None:
        return refusal
    raw = await _read_bounded_body(request)
    if raw is None:
        return _fail(
            "too_large", f"the request body exceeds {MAX_BODY_BYTES} bytes.", 413
        )
    try:
        payload = json.loads(raw)
    except ValueError:
        # `JSONDecodeError` and `UnicodeDecodeError` are both `ValueError`s:
        # a malformed body and one that is not UTF-8 are one refusal.
        return _fail("bad_request", "invalid json", 400)
    target = _body_target(payload)
    if target is None:
        return _fail(
            "bad_request",
            'send exactly {"chat_id": "<id>"} or {"project_id": "<id>"},'
            ' optionally with "location": "workspace" or "repo"',
            400,
        )
    kind, target_id, location = target
    folder = _folder_for(request, kind, target_id, location)
    if isinstance(folder, JSONResponse):
        return folder
    try:
        info = await _manager(request).create(folder, kind, target_id)
    except TerminalSessionError as exc:
        return _manager_failure(exc)
    return _no_store(JSONResponse({"session": _info_json(info)}))


async def terminal_sessions_end(request: Request) -> JSONResponse:
    """End a terminal session: the shell closes, then the record goes.

    An end of a session that is not there is a 404 and not a no-op: the
    caller asked about a session, and the honest answer is that there is
    none. The close itself is the manager's, and it is not abandoned when
    it is refused.
    """
    refusal = _gate(request)
    if refusal is not None:
        return refusal
    session_id = str(request.path_params.get("session_id", "") or "")
    if not _SESSION_ID_PATTERN.match(session_id):
        return _fail("bad_request", "that is not a terminal session id.", 400)
    try:
        await _manager(request).end(session_id)
    except TerminalSessionError as exc:
        return _manager_failure(exc)
    return _no_store(JSONResponse({"ok": True}))
