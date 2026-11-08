"""Pure validation of a pasted loopback OAuth callback.

When a sign-in ends on a page that cannot load (the provider redirected to a
listener on the engine's computer, and the user is on another device), the user
copies that address and pastes it into the card. This module decides whether the
pasted address is the callback the pending authorize URL is waiting for, and if
so, what the engine should replay to its own listener.

The authorize URL is the one the engine obtained from the provider; it is the only
source of the host, port, path and state. Refusals carry a closed reason word and
never the URL, the query or the code. Stdlib only.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import parse_qs, urlsplit

MAX_PASTED_URL_CHARS = 4096
LOOPBACK_CALLBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})

_MAX_REDIRECT_PATH_CHARS = 256
_QUERY_SAFE = re.compile(r"^[A-Za-z0-9._~%&=+\-]{1,3800}$")

CallbackRefusal = Literal[
    "too_long",
    "not_a_url",
    "wrong_scheme",
    "wrong_host",
    "wrong_port",
    "wrong_path",
    "bad_query",
    "state_mismatch",
    "missing_code",
    "provider_error",
    "bad_authorize_url",
]


class CallbackRefused(ValueError):
    """A pasted callback the pending sign-in does not accept.

    ``reason`` is one of :data:`CallbackRefusal`. The message is the reason and
    nothing else, so the pasted URL never reaches a log line or a response body.
    """

    def __init__(self, reason: CallbackRefusal) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CallbackTarget:
    """Where the engine replays a pasted callback: its own listener, with the pasted query verbatim."""

    port: int
    path: str
    query: str = field(repr=False)

    def replay_url(self) -> str:
        """Always ``127.0.0.1``, whatever spelling the user pasted."""
        return f"http://127.0.0.1:{self.port}{self.path}?{self.query}"


def redirect_target(authorize_url: str) -> tuple[str, int, str, str]:
    """The ``(host, port, path, state)`` the pending authorize URL expects its callback on."""
    try:
        parts = urlsplit(authorize_url)
        if parts.scheme != "https":
            raise CallbackRefused("bad_authorize_url")
        query = parse_qs(parts.query, keep_blank_values=True)
        redirects = query.get("redirect_uri", [])
        states = query.get("state", [])
        if len(redirects) != 1 or len(states) != 1 or not states[0]:
            raise CallbackRefused("bad_authorize_url")
        redirect = urlsplit(redirects[0])
        host = (redirect.hostname or "").lower()
        port = redirect.port
    except ValueError:
        raise CallbackRefused("bad_authorize_url") from None
    if (
        redirect.scheme != "http"
        or host not in LOOPBACK_CALLBACK_HOSTS
        or port is None
        or not 1024 <= port <= 65535
        or redirect.username
        or redirect.password
        or not redirect.path.startswith("/")
        or len(redirect.path) > _MAX_REDIRECT_PATH_CHARS
        or redirect.query
        or redirect.fragment
    ):
        raise CallbackRefused("bad_authorize_url")
    return host, port, redirect.path, states[0]


def validate_pasted_callback(authorize_url: str, pasted: str) -> CallbackTarget:
    """Accept the pasted address only if it is the pending sign-in's own callback.

    Each step raises :class:`CallbackRefused` with the first reason that applies.
    """
    text = pasted.strip()
    if len(text) > MAX_PASTED_URL_CHARS:
        raise CallbackRefused("too_long")
    if any(ord(character) < 33 or ord(character) == 127 for character in text):
        raise CallbackRefused("not_a_url")

    _host, port, path, state = redirect_target(authorize_url)

    try:
        pasted_parts = urlsplit(text)
    except ValueError:
        raise CallbackRefused("not_a_url") from None
    if pasted_parts.scheme != "http":
        raise CallbackRefused("wrong_scheme")
    if (
        (pasted_parts.hostname or "").lower() not in LOOPBACK_CALLBACK_HOSTS
        or pasted_parts.username
        or pasted_parts.password
    ):
        raise CallbackRefused("wrong_host")
    try:
        pasted_port: int | None = pasted_parts.port
    except ValueError:
        pasted_port = None
    if pasted_port != port:
        raise CallbackRefused("wrong_port")
    if pasted_parts.path != path:
        raise CallbackRefused("wrong_path")
    if pasted_parts.fragment:
        raise CallbackRefused("bad_query")

    if not _QUERY_SAFE.match(pasted_parts.query):
        raise CallbackRefused("bad_query")
    try:
        query = parse_qs(pasted_parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise CallbackRefused("bad_query") from None

    if "error" in query:
        raise CallbackRefused("provider_error")
    states = query.get("state")
    if (
        states is None
        or len(states) != 1
        or not hmac.compare_digest(states[0].encode(), state.encode())
    ):
        raise CallbackRefused("state_mismatch")
    codes = query.get("code")
    if codes is None or len(codes) != 1 or not codes[0]:
        raise CallbackRefused("missing_code")

    return CallbackTarget(port=port, path=path, query=pasted_parts.query)
