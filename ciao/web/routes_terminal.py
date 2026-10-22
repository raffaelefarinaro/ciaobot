"""The browser terminal's access policy: `POST /api/terminal/unlock` and
`GET /api/terminal/status` (#1697, child of #1680).

This is the *door* in front of the terminal, and nothing else. No shell is
launched here, no session is opened, no PTY exists: this child owns the
question "may this browser use the terminal at all, and does it currently
hold a grant", and answers it with a policy a later child can reuse
unchanged for the WebSocket.

The policy is deliberately stricter than the rest of the API, because a
terminal is a shell on the engine's machine:

* **The signed session is required**, on every route, from every peer. The
  terminal is in neither the public allowlist nor the loopback-only one:
  the second set lets a loopback peer skip the session, and a local
  process must not be able to open a shell just by being local. The route
  re-checks the session itself (defence in depth: the middleware is not
  the only thing standing between a request and a grant).
* **The browser's origin must be this engine's own origin** — the same
  scheme, hostname and effective port. `Origin` is the browser's
  declaration; a read may fall back to `Referer`, a write may not. A
  missing, `null`, malformed, userinfo-bearing, cross-host, cross-port or
  cross-scheme source is a refusal, and so is a source with a fragment, or
  (for an `Origin`) a path or a query. A `Referer` is a page address, so
  its path and its query are the page's own and are allowed.
* **A remote browser must arrive over HTTPS.** Plain HTTP is allowed only
  for a direct loopback caller on a loopback `Host` — the existing
  `is_loopback_client` gate, which a reverse proxy's headers defeat, so a
  proxied caller is not local. The scheme that counts is the effective
  ASGI scheme (what uvicorn's own proxy handling has already rewritten);
  a raw `X-Forwarded-Proto` never turns a plain request into a secure
  one, and a raw `X-Forwarded-Host` never makes a foreign origin match.
* **The grant never reaches JavaScript.** The bearer travels in one
  host-only, HttpOnly, SameSite=Strict `ciao_terminal` cookie and nowhere
  else: not in the JSON, not in a URL, not in a log line. `Path=/` is
  deliberate — both `/api/terminal` and `/ws/terminal` need it.

**The signed-in session is the terminal's trust boundary.** The unlock
route asks for no secret: a browser that holds a live session and came
from this engine's own page gets the grant, and the route takes no body
at all, so there is nothing to re-enter, nothing to guess and nothing
that could be phished a second time. What the grant still answers is
*which* browser may hold the socket and *for how long* — it binds the
browser, expires on its own fixed deadline, and is revoked when the
session behind it is re-keyed. The consequence is written down rather
than left implicit: a workspace that denies the agent `Bash` no longer
has a host-shell boundary, because a signed-in browser is enough.

Two things this module deliberately does *not* do. It does not widen any
existing allowlist, and it does not touch the native `/api/host/terminal`
route, which keeps its own, stricter "this browser is on the engine's
machine" rule.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket

from ciao import host_terminal
from ciao.terminal_auth import (
    TERMINAL_COOKIE,
    TERMINAL_UNLOCK_TTL_S,
    TerminalAuthError,
    TerminalGrant,
    TerminalGrantStore,
)
from ciao.web.auth import (
    LOOPBACK_HOST_NAMES,
    SESSION_COOKIE,
    _effective_port,
    _split_host,
    is_loopback_client,
    verify_session,
)

#: The unlock body cap, in bytes. The declared `Content-Length` is checked
#: against it and the body is capped again while it is read, because a
#: chunked request declares nothing and a declared length is the caller's
#: own claim. The route takes no body at all, so the cap is a guard on a
#: request that is going to be refused anyway — it is there so a hostile
#: body is never read into memory.
MAX_BODY_BYTES = 4096

#: The detached-session lifetime the status reports. A terminal session
#: detached from its browser stays recoverable for this long; it is a
#: fixed number, not a sliding one.
DETACHED_TTL_SECONDS = 900

#: One line per refusal reason, so the reason is a closed vocabulary and a
#: route cannot invent a new one by accident.
_POLICY_MESSAGES = {
    "https_required": (
        "the terminal needs HTTPS; open the PWA over https, or use it on the "
        "engine's own machine"
    ),
    "origin_required": (
        "the terminal needs the browser's own origin; open it from the PWA page"
    ),
    "forbidden_origin": "the terminal is only available from this engine's own page",
}


def _no_store(response: JSONResponse) -> JSONResponse:
    """Answers about a credential are never cacheable."""
    response.headers["Cache-Control"] = "no-store"
    return response


def _fail(reason: str, error: str, status: int) -> JSONResponse:
    return _no_store(
        JSONResponse(
            {"ok": False, "reason": reason, "error": error}, status_code=status
        )
    )


# ── The policy ─────────────────────────────────────────────────────────────


def _scheme_of(request: Request | WebSocket) -> str:
    """The effective ASGI scheme, with the WebSocket ones folded onto HTTP.

    This is the scheme the server saw the request arrive on — what
    uvicorn's own proxy handling has already rewritten when a trusted
    proxy terminated TLS. A raw `X-Forwarded-Proto` header is never read
    here: it is a header anyone can send, and only the deployment's proxy
    trust decides what the transport was.
    """
    scheme = str(request.url.scheme or "").lower()
    return {"ws": "http", "wss": "https"}.get(scheme, scheme)


def _is_read(request: Request | WebSocket) -> bool:
    """Whether a `Referer` may stand in for the `Origin`.

    Only a read may use the `Referer`: a `GET` from the PWA page carries
    one, and a `POST` from the same page carries an `Origin` too, so
    requiring it there costs nothing and closes the "a cross-site form
    post carries no Origin" hole. A WebSocket handshake is not a read: it
    may only present an `Origin`.
    """
    if request.scope.get("type") == "websocket":
        return False
    return str(request.scope.get("method", "") or "").upper() in {"GET", "HEAD"}


def _source(request: Request | WebSocket) -> tuple[str, bool]:
    """The browser's own origin declaration, and whether it is a `Referer`.

    `Origin` wins whenever it carries anything: a hostile `Origin` next to
    a good `Referer` is a refusal, never a fallback. An empty `Origin`
    header carries no origin information and reads as absent.
    """
    origin = str(request.headers.get("origin", "") or "").strip()
    if origin:
        return origin, False
    if _is_read(request):
        referer = str(request.headers.get("referer", "") or "").strip()
        if referer:
            return referer, True
    return "", False


def _request_origin(request: Request | WebSocket) -> tuple[str, str, int | None]:
    """This request's own origin: ``(scheme, host, port)``.

    The host is the `Host` header the browser sent — which a
    host-preserving proxy keeps correct — and never a raw
    `X-Forwarded-Host`, which is a header anyone can send. The spelling is
    compared as it arrived, exactly as the app's own origin check does it,
    so the two gates cannot disagree about one request.
    """
    host, port = _split_host(str(request.headers.get("host", "") or ""))
    if not host:
        host = str(request.url.hostname or "").lower()
        port = request.url.port
    return _scheme_of(request), host, port


def _loopback_host(request: Request | WebSocket) -> bool:
    """Whether the `Host` header names this machine.

    A rebound page carries its own name, so this is what tells the engine's
    own address apart from an attacker's. It is the same set the rest of
    the app uses for a local `Host`.
    """
    host, _port = _split_host(str(request.headers.get("host", "") or ""))
    if not host:
        host = str(request.url.hostname or "").lower()
    return host.rstrip(".") in LOOPBACK_HOST_NAMES


def _transport_refusal(request: Request | WebSocket) -> str | None:
    """Why this request is not on a secure transport, or None."""
    if _scheme_of(request) == "https":
        return None
    # Plain HTTP is only for a browser talking to the engine on this
    # machine, with no proxy in between: `is_loopback_client` reads the
    # TCP peer and refuses a caller that carries reverse-proxy headers, and
    # the `Host` must name this machine, so a rebound page's own name does
    # not qualify.
    if is_loopback_client(request) and _loopback_host(request):
        return None
    return "https_required"


def _origin_refusal(request: Request | WebSocket) -> str | None:
    """Why this request is not from this engine's own page, or None."""
    source, is_referer = _source(request)
    if not source:
        return "origin_required"
    try:
        parsed = urlsplit(source)
    except ValueError:
        return "forbidden_origin"
    if parsed.scheme.lower() not in {"http", "https"}:
        return "forbidden_origin"
    # Userinfo is refused by its *presence* in the authority, not by its
    # truthiness: `http://@host` carries an empty userinfo, and an empty
    # one is still userinfo. `urlsplit` reads the authority for us, so a
    # `@` in a page's query (`?email=a@b`) is not userinfo and is not
    # refused as one.
    if parsed.username is not None or parsed.password is not None:
        return "forbidden_origin"
    # A browser's `Origin` is a bare origin: no path, no query, no
    # fragment. A `Referer` is a page address, so its path and its query
    # are the page's own and are allowed — but a fragment never is: browsers
    # strip it before sending, so a `Referer` carrying one is not a page
    # this browser fetched.
    if parsed.fragment:
        return "forbidden_origin"
    if not is_referer and (parsed.path or parsed.query):
        return "forbidden_origin"
    try:
        origin_port = parsed.port
    except ValueError:
        return "forbidden_origin"
    origin_host = str(parsed.hostname or "").lower()
    if not origin_host:
        return "forbidden_origin"

    request_scheme, request_host, request_port = _request_origin(request)
    if parsed.scheme.lower() != request_scheme:
        return "forbidden_origin"
    if origin_host != request_host:
        return "forbidden_origin"
    if _effective_port(parsed.scheme.lower(), origin_port) != _effective_port(
        request_scheme, request_port
    ):
        return "forbidden_origin"
    return None


def terminal_policy(request: Request | WebSocket) -> str | None:
    """The reason this request may not use the terminal, or ``None``.

    ``None`` means the request may proceed to the session check. The reasons are a closed vocabulary — ``https_required``,
    ``origin_required``, ``forbidden_origin`` — because a caller is told
    *which* rule it broke, never what it sent.

    The transport is checked before the origin: whether the connection is
    secure is a fact about the request that no header can talk out of, so
    it is the honest answer for a remote browser on plain HTTP even when
    its `Origin` is also wrong.
    """
    refusal = _transport_refusal(request)
    if refusal is not None:
        return refusal
    return _origin_refusal(request)


# ── The session and the grant ──────────────────────────────────────────────


def _session_cookie(request: Request | WebSocket) -> str:
    return str(request.cookies.get(SESSION_COOKIE, "") or "")


def _session_ok(request: Request | WebSocket) -> bool:
    """Whether this request carries a live signed session.

    The serializer is read from the app every time, so a re-keyed password
    (which re-keys the signer) takes effect at once, and the check is not
    left to the middleware alone.
    """
    serializer = getattr(request.app.state, "serializer", None)
    if serializer is None:
        return False
    return verify_session(request, serializer)


def _store(request: Request | WebSocket) -> TerminalGrantStore:
    """The app's grant store.

    There is no fallback: the factory always wires one, and a route that
    cannot find it is a wiring bug rather than a request to answer.
    """
    store: TerminalGrantStore = request.app.state.terminal_grants
    return store


def terminal_grant(request: Request | WebSocket) -> TerminalGrant | None:
    """The terminal grant this request carries, or ``None``.

    ``None`` is the answer for every failure — no session, no cookie, an
    unknown, expired or foreign-session bearer — so a caller cannot tell
    them apart and nothing about the attempt is echoed back.
    """
    if not _session_ok(request):
        return None
    token = str(request.cookies.get(TERMINAL_COOKIE, "") or "")
    if not token:
        return None
    return _store(request).verify(token, _session_cookie(request))


def _clock(request: Request | WebSocket) -> Callable[[], float]:
    """The clock the app's grant store keeps its deadlines on.

    A grant's deadline is a reading of the store's clock, so the route
    must read the remaining time against the *same* clock — a second one
    would report a lifetime that is off by however far the two have
    drifted.
    """
    clock: Callable[[], float] = request.app.state.terminal_clock
    return clock


def _remaining_seconds(request: Request, grant: TerminalGrant) -> int:
    """The grant's remaining lifetime, rounded down and never negative."""
    return max(0, int(grant.expires_at - _clock(request)()))


# ── The body ───────────────────────────────────────────────────────────────


def _declared_length_over_cap(declared: str) -> bool:
    """Whether a declared `Content-Length` claims more than the cap.

    The header is the caller's own claim, so it is never trusted on its
    own — and it is never *converted* blindly either. A header of five
    thousand digits would make `int()` raise, which is a 500 on a request
    that should be a 413; so the digits are compared as text, after the
    leading zeros are stripped, and only a short enough value is ever
    turned into a number.

    Stripping the zeros first is what keeps a small claim honest:
    `"0004096"` is 4096 bytes, not a million, and refusing it because it
    has six digits would be a refusal for the spelling rather than for
    the size.
    """
    if not declared.isascii() or not declared.isdigit():
        return False
    digits = declared.lstrip("0")
    if not digits:
        # A declared length of zero is the smallest possible claim.
        return False
    if len(digits) > len(str(MAX_BODY_BYTES)):
        return True
    return int(digits) > MAX_BODY_BYTES


async def _read_bounded_body(request: Request) -> bytes | None:
    """The request body, bounded; ``None`` when it is over the cap.

    The declared `Content-Length` is checked first — it is the caller's own
    claim, so it is never trusted on its own — and the body is capped again
    while it is read, because a chunked request declares nothing.
    """
    declared = str(request.headers.get("content-length", "") or "")
    if _declared_length_over_cap(declared):
        return None
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


# ── The routes ─────────────────────────────────────────────────────────────


async def terminal_status(request: Request) -> JSONResponse:
    """Whether this browser may use the terminal, and how long its grant lasts.

    A status read needs the ordinary signed session and the strict policy,
    but no fresh grant: it reports, it never grants. The answer names no
    credential — the grant's bearer and the session cookie are both absent
    from it — and it is never cacheable.
    """
    if not _session_ok(request):
        return _fail("unauthorized", "a signed-in session is required.", 401)
    refusal = terminal_policy(request)
    if refusal is not None:
        return _no_store(
            JSONResponse(
                {
                    "available": False,
                    "reason": refusal,
                    "platform": host_terminal.host_platform(),
                    "host": socket.gethostname(),
                    "unlock_expires_in": 0,
                    "detached_ttl_seconds": DETACHED_TTL_SECONDS,
                }
            )
        )

    grant = terminal_grant(request)
    return _no_store(
        JSONResponse(
            {
                "available": True,
                "reason": "",
                "platform": host_terminal.host_platform(),
                # The engine's own name, never the browser's `Host` header:
                # the status is about the machine, not about the caller.
                "host": socket.gethostname(),
                "unlock_expires_in": (
                    _remaining_seconds(request, grant) if grant is not None else 0
                ),
                "detached_ttl_seconds": DETACHED_TTL_SECONDS,
            }
        )
    )


async def terminal_unlock(request: Request) -> JSONResponse:
    """Give this browser the terminal grant.

    The order is the policy first, then the session, then the body: a
    request that is not this engine's own page, or not on a secure
    transport, never has its body read, so a refusal can never become a
    guess.

    There is no password. The signed-in session is the terminal's trust
    boundary: a browser that holds a live session and came from this
    engine's own page gets the grant, and the route takes no body at all,
    so there is no secret to send, nothing to re-enter and nothing to
    phish. A request that carries a body is refused.

    A refusal is one of a closed set of reasons and never carries the
    bearer or the cookie. The grant itself leaves only in the
    `Set-Cookie` header.
    """
    refusal = terminal_policy(request)
    if refusal is not None:
        return _fail(refusal, _POLICY_MESSAGES[refusal], 403)
    if not _session_ok(request):
        return _fail("unauthorized", "a signed-in session is required.", 401)

    raw = await _read_bounded_body(request)
    if raw is None:
        return _fail(
            "too_large", f"the request body exceeds {MAX_BODY_BYTES} bytes.", 413
        )
    if raw:
        # The route takes no body: a request that carries one is asking
        # for something this route does not do. Whatever it carries is
        # never read as a secret, never compared and never kept.
        return _fail("bad_request", "this route takes no body.", 400)

    store = _store(request)
    try:
        token, _grant = store.issue(
            _session_cookie(request),
            replace_token=str(request.cookies.get(TERMINAL_COOKIE, "") or ""),
        )
    except TerminalAuthError as exc:
        if exc.reason == "capacity":
            return _fail(
                "capacity", "too many terminal unlocks are live; try again later.", 429
            )
        if exc.reason == "invalid_session":
            return _fail("unauthorized", "a signed-in session is required.", 401)
        raise

    response = _no_store(
        JSONResponse({"ok": True, "expires_in": TERMINAL_UNLOCK_TTL_S})
    )
    # The bearer leaves here and nowhere else: host-only (no `Domain`),
    # HttpOnly so no script can read it, `SameSite=Strict` so it does not
    # travel on a cross-site request, and `Path=/` because both
    # `/api/terminal` and `/ws/terminal` need it.
    response.set_cookie(
        TERMINAL_COOKIE,
        token,
        max_age=TERMINAL_UNLOCK_TTL_S,
        httponly=True,
        samesite="strict",
        path="/",
        secure=_scheme_of(request) == "https",
    )
    return response
