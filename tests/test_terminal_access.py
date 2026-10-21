"""The browser terminal's unlock and status boundary (#1697).

Real ``AuthMiddleware``, the real routes and the real grant store: the tests
drive the same handlers the engine serves, so what they pin is the wire
contract and the access policy, not a mock's idea of it.

What is pinned here, beyond the shapes:

* the session is required on **every** terminal route, on a loopback peer as
  much as on a remote one — the terminal is not a local-only surface;
* the browser's origin must match this engine's own origin exactly (scheme,
  hostname and effective port), and a missing, `null`, malformed or
  cross-origin value is a refusal;
* a remote browser must arrive over HTTPS, and a raw ``X-Forwarded-Proto``
  or ``X-Forwarded-Host`` cannot talk the gate into believing otherwise;
* the unlock body is bounded, exactly ``{"password": str}``, and a wrong or
  malformed password is a counted failure — never an exception and never a
  grant;
* the grant itself never reaches JavaScript: it travels only in a host-only,
  HttpOnly, SameSite=Strict cookie.

No shell is launched here: this child owns the access policy, not the PTY.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.terminal_auth import (
    TERMINAL_COOKIE,
    TERMINAL_MAX_GRANTS,
    TERMINAL_UNLOCK_FAIL_LIMIT,
    TERMINAL_UNLOCK_TTL_S,
    TerminalGrantStore,
)
from ciao.web import auth
from ciao.web.app import create_app
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.routes_terminal import (
    terminal_grant,
    terminal_policy,
    terminal_status,
    terminal_unlock,
)

SECRET = "test-secret"
PASSWORD = "correct horse battery staple"
BASE = "http://localhost:8443"
UNLOCK = "/api/terminal/unlock"
STATUS = "/api/terminal/status"
SESSIONS = "/api/terminal/sessions"

#: A lone surrogate: what a hostile body can carry and what UTF-8 refuses.
#: Written as an escape so this file stays valid UTF-8.
_MALFORMED = "\ud800"


class FakeClock:
    """A monotonic clock the test moves by hand. No sleeps, no wall time."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _app(tmp_path: Path) -> Starlette:
    """A real app with the real middleware and the real terminal routes."""
    serializer = URLSafeTimedSerializer(SECRET)
    app = Starlette(
        routes=[
            Route(UNLOCK, terminal_unlock, methods=["POST"]),
            Route(STATUS, terminal_status, methods=["GET"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        pwa_auth_token=PASSWORD,
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
    )
    app.state.terminal_clock = time.monotonic
    app.state.terminal_grants = TerminalGrantStore(clock=app.state.terminal_clock)
    return app


def _client(
    app: Starlette,
    *,
    peer: str = "127.0.0.1",
    base_url: str = BASE,
    signed_in: bool = True,
) -> TestClient:
    """A client with an owner session in its cookie jar.

    The session lives in the jar rather than in an explicit header, so the
    grant cookie the unlock sets travels with it — a request that names its
    own `Cookie` header would hide the jar's cookies.
    """
    client = TestClient(app, base_url=base_url, client=(peer, 50000))
    if signed_in:
        serializer = app.state.serializer
        client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))
    return client


def _origin(origin: str = BASE) -> dict[str, str]:
    """The browser's own origin header."""
    return {"Origin": origin}


def _terminal_cookie(client: TestClient) -> str:
    """The grant cookie the unlock set, read out of the client's jar."""
    return client.cookies.get(TERMINAL_COOKIE) or ""


def _bare_request(
    *,
    host: str = "localhost:8443",
    peer: str = "127.0.0.1",
    scheme: str = "http",
    method: str = "GET",
    origin: str | None = None,
    referer: str | None = None,
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> Request:
    """A bare Starlette request, so the policy can be exercised directly.

    The `Host` header is what the browser sent, the peer address is the
    TCP source, and the scheme is the one the request arrived on.
    """
    raw: list[tuple[bytes, bytes]] = [(b"host", host.encode("latin-1"))]
    if origin is not None:
        raw.append((b"origin", origin.encode("latin-1")))
    if referer is not None:
        raw.append((b"referer", referer.encode("latin-1")))
    raw.extend(
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in extra_headers
    )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": scheme,
        "path": STATUS,
        "raw_path": STATUS.encode("latin-1"),
        "query_string": b"",
        "root_path": "",
        "headers": raw,
        "client": (peer, 50000),
        "server": ("127.0.0.1", 8443),
    }
    return Request(scope)


# ── The session is never skipped ───────────────────────────────────────────


def test_primary_nested_json_is_a_safe_refusal(tmp_path: Path) -> None:
    client = _client(_app(tmp_path))
    response = client.post(
        UNLOCK, content=b"[" * 1500 + b"0" + b"]" * 1500, headers=_origin()
    )
    assert response.status_code == 400
    assert "set-cookie" not in response.headers


def test_primary_huge_declared_length_is_a_safe_refusal(tmp_path: Path) -> None:
    client = _client(_app(tmp_path))
    response = client.post(
        UNLOCK,
        content=b"{}",
        headers={**_origin(), "Content-Length": "9" * 5000},
    )
    assert response.status_code == 413
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    ("label", "declared", "expected"),
    [
        # A small claim is answered on its size, not on its spelling.
        ("a leading-zero small claim", "0004096", 200),
        ("a leading-zero zero claim", "0000", 200),
        # A claim over the cap is refused, however it is written.
        ("a leading-zero claim just over the cap", "0004097", 413),
        ("a claim with many leading zeros", "0" * 5000 + "1", 200),
        ("a huge claim", "9" * 5000, 413),
        # A claim that is not a number carries no information, so the body
        # is read and answered on its own.
        ("a non-numeric claim", "not-a-number", 200),
        ("an empty claim", "", 200),
    ],
)
def test_the_declared_length_is_compared_as_a_number_not_as_a_spelling(
    tmp_path: Path, label: str, declared: str, expected: int
) -> None:
    """The header is a number, and its spelling is not a reason to refuse.

    A claim of `"0004096"` is 4096 bytes, not a million: the digits are
    compared after the leading zeros are stripped, so a small claim is
    answered on its size and not on how it was written. A claim of five
    thousand digits is refused without ever being turned into a number.
    """
    client = _client(_app(tmp_path))
    body = json.dumps({"password": PASSWORD}).encode("utf-8")
    response = client.post(
        UNLOCK,
        content=body,
        headers={
            **_origin(),
            "Content-Type": "application/json",
            "Content-Length": declared,
        },
    )

    assert response.status_code == expected, label
    if expected == 413:
        assert "set-cookie" not in response.headers, label


def test_primary_same_origin_referer_may_include_page_query(tmp_path: Path) -> None:
    client = _client(_app(tmp_path))
    response = client.get(STATUS, headers={"Referer": BASE + "/?chat=example"})
    assert response.status_code == 200
    assert response.json()["available"] is True


def test_primary_empty_userinfo_is_not_a_browser_origin() -> None:
    request = _bare_request(origin="http://@localhost:8443")
    assert terminal_policy(request) == "forbidden_origin"


@pytest.mark.parametrize("peer", ["127.0.0.1", "100.64.0.7"])
def test_terminal_routes_never_skip_session_even_on_loopback(
    tmp_path: Path, peer: str
) -> None:
    """No terminal route is in either middleware allowlist, so no peer skips it."""
    app = _app(tmp_path)
    client = _client(app, peer=peer, signed_in=False)

    assert client.get(STATUS).status_code == 401
    assert client.post(UNLOCK, json={"password": PASSWORD}).status_code == 401
    assert client.get(STATUS, headers=_origin()).status_code == 401
    assert (
        client.post(UNLOCK, json={"password": PASSWORD}, headers=_origin()).status_code
        == 401
    )
    # Nothing was unlocked, so no grant exists.
    assert app.state.terminal_grants.retained_grant_count() == 0


def test_the_terminal_paths_are_in_neither_middleware_allowlist() -> None:
    assert UNLOCK not in auth._PUBLIC_API
    assert UNLOCK not in auth._LOOPBACK_ONLY_API
    assert STATUS not in auth._PUBLIC_API
    assert STATUS not in auth._LOOPBACK_ONLY_API


# ── The happy path: a local browser unlocks ────────────────────────────────


def test_unlock_accepts_exact_local_origin_with_password_and_sets_http_only_cookie(
    tmp_path: Path,
) -> None:
    client = _client(_app(tmp_path))
    response = client.post(UNLOCK, json={"password": PASSWORD}, headers=_origin())

    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True, "expires_in": TERMINAL_UNLOCK_TTL_S}

    # The grant travels only in the cookie: nothing in the JSON names it.
    serialized = json.dumps(body)
    assert TERMINAL_COOKIE not in serialized
    assert PASSWORD not in serialized
    assert "token" not in serialized.lower()
    assert "grant" not in serialized.lower()

    # ...and the cookie is the shape the contract fixes: host-only (no
    # Domain), HttpOnly, SameSite=Strict, Path=/, bounded lifetime.
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{TERMINAL_COOKIE}=")
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie.replace("Strict", "strict")
    assert "Path=/" in set_cookie
    assert "Domain" not in set_cookie
    assert f"Max-Age={TERMINAL_UNLOCK_TTL_S}" in set_cookie
    # Plain HTTP on loopback: the cookie is not marked Secure, because the
    # browser would then refuse to send it back.
    assert "Secure" not in set_cookie

    assert _terminal_cookie(client)


def test_unlock_never_echoes_the_password_or_the_bearer_in_the_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    client = _client(_app(tmp_path))
    with caplog.at_level(logging.DEBUG):
        response = client.post(UNLOCK, json={"password": PASSWORD}, headers=_origin())

    assert response.status_code == 200
    bearer = _terminal_cookie(client)
    assert bearer

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert bearer not in logged
    assert PASSWORD not in logged
    assert "Set-Cookie" not in logged


# ── Status: session + policy, but no unlock ────────────────────────────────


def test_status_does_not_require_unlock_and_never_returns_credential_material(
    tmp_path: Path,
) -> None:
    client = _client(_app(tmp_path))

    response = client.get(STATUS, headers=_origin())
    assert response.status_code == 200
    body = response.json()

    assert set(body) == {
        "available",
        "reason",
        "platform",
        "host",
        "unlock_expires_in",
        "detached_ttl_seconds",
    }
    assert body["available"] is True
    assert body["reason"] == ""
    assert body["unlock_expires_in"] == 0
    assert body["detached_ttl_seconds"] == 900
    assert isinstance(body["platform"], str) and body["platform"]
    # The host is the engine's own name, not the browser's Host header.
    assert body["host"] and body["host"] != "localhost"

    # No credential material, in any spelling.
    serialized = json.dumps(body)
    assert PASSWORD not in serialized
    assert SESSION_COOKIE not in serialized
    assert TERMINAL_COOKIE not in serialized
    assert "token" not in serialized.lower()

    # The answer is not cacheable, and it sets no cookie of its own.
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


def test_status_reports_the_remaining_unlock_time(tmp_path: Path) -> None:
    client = _client(_app(tmp_path))
    assert (
        client.post(UNLOCK, json={"password": PASSWORD}, headers=_origin()).status_code
        == 200
    )

    body = client.get(STATUS, headers=_origin()).json()
    assert body["unlock_expires_in"] > 0
    assert body["unlock_expires_in"] <= TERMINAL_UNLOCK_TTL_S


def test_the_status_reads_the_grant_deadline_against_the_store_s_own_clock(
    tmp_path: Path,
) -> None:
    """The remaining time is read on the clock the deadline was minted on.

    A second clock would report a lifetime off by however far the two have
    drifted, and a test that injected one would see it.
    """
    app = _app(tmp_path)
    clock = FakeClock()
    app.state.terminal_clock = clock
    app.state.terminal_grants = TerminalGrantStore(clock=clock)
    client = _client(app)

    assert (
        client.post(UNLOCK, json={"password": PASSWORD}, headers=_origin()).status_code
        == 200
    )
    body = client.get(STATUS, headers=_origin()).json()
    assert body["unlock_expires_in"] == TERMINAL_UNLOCK_TTL_S

    # And the deadline moves with that clock, not with the wall clock.
    clock.advance(TERMINAL_UNLOCK_TTL_S - 10)
    body = client.get(STATUS, headers=_origin()).json()
    assert body["unlock_expires_in"] == 10

    clock.advance(TERMINAL_UNLOCK_TTL_S)
    body = client.get(STATUS, headers=_origin()).json()
    assert body["unlock_expires_in"] == 0


def test_status_never_unlocks_anything_by_itself(tmp_path: Path) -> None:
    """A status read is not an unlock: no grant appears, no grant is needed."""
    app = _app(tmp_path)
    client = _client(app)
    store: TerminalGrantStore = app.state.terminal_grants

    assert client.get(STATUS, headers=_origin()).status_code == 200
    assert store.retained_grant_count() == 0
    assert not _terminal_cookie(client)


# ── Transport: a remote browser must arrive over HTTPS ────────────────────


def test_remote_https_unlock_and_status_work(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = _client(app, peer="100.64.0.7", base_url="https://app.example")
    origin = _origin("https://app.example")

    response = client.post(UNLOCK, json={"password": PASSWORD}, headers=origin)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "expires_in": TERMINAL_UNLOCK_TTL_S}

    # Over HTTPS the grant cookie is marked Secure.
    assert "Secure" in response.headers["set-cookie"]

    body = client.get(STATUS, headers=origin).json()
    assert body["available"] is True
    assert body["unlock_expires_in"] > 0


def test_remote_plain_http_is_refused_before_password_read(tmp_path: Path) -> None:
    """A plain-HTTP remote browser sees "https required", never a password box."""
    app = _app(tmp_path)
    client = _client(app, peer="100.64.0.7", base_url="http://app.example")
    response = client.post(
        UNLOCK, json={"password": PASSWORD}, headers=_origin("http://app.example")
    )

    assert response.status_code == 403
    assert response.json()["reason"] == "https_required"
    # The body was never read, so no guess was counted.
    store: TerminalGrantStore = app.state.terminal_grants
    assert store.retained_grant_count() == 0
    assert not _terminal_cookie(client)


def test_status_reports_https_required_for_a_plain_http_remote_browser(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    client = _client(app, peer="100.64.0.7", base_url="http://app.example")
    body = client.get(STATUS, headers=_origin("http://app.example")).json()

    assert body["available"] is False
    assert body["reason"] == "https_required"
    assert body["unlock_expires_in"] == 0


def test_proxy_headers_never_grant_plain_http_loopback_exception(
    tmp_path: Path,
) -> None:
    """A loopback peer behind a proxy is not local, so plain HTTP is refused."""
    app = _app(tmp_path)
    client = _client(app)
    response = client.post(
        UNLOCK,
        json={"password": PASSWORD},
        headers={**_origin(), "X-Forwarded-For": "203.0.113.9"},
    )

    assert response.status_code == 403
    assert response.json()["reason"] == "https_required"
    assert not _terminal_cookie(client)


def test_raw_forwarded_proto_cannot_override_the_actual_scheme(tmp_path: Path) -> None:
    """The raw forwarded proto is not the transport, and never wins."""
    app = _app(tmp_path)
    client = _client(app, peer="100.64.0.7", base_url="http://app.example")
    response = client.post(
        UNLOCK,
        json={"password": PASSWORD},
        headers={**_origin("http://app.example"), "X-Forwarded-Proto": "https"},
    )

    assert response.status_code == 403
    assert response.json()["reason"] == "https_required"
    assert not _terminal_cookie(client)


def test_raw_forwarded_host_cannot_override_the_actual_origin(tmp_path: Path) -> None:
    """A forwarded host cannot make a foreign origin this engine's own."""
    app = _app(tmp_path)
    client = _client(app, peer="100.64.0.7", base_url="https://app.example")
    response = client.post(
        UNLOCK,
        json={"password": PASSWORD},
        headers={
            **_origin("https://evil.example"),
            "X-Forwarded-Host": "evil.example",
        },
    )

    assert response.status_code == 403
    assert response.json()["reason"] == "forbidden_origin"
    assert not _terminal_cookie(client)


# ── The origin must be this engine's own origin, exactly ───────────────────


#: (label, request kwargs, the reason the policy must answer). The peer is
#: a loopback one unless a case says otherwise, so these cases exercise the
#: origin rule and not the transport rule.
_LOCAL_POLICY_CASES: list[tuple[str, dict[str, Any], str | None]] = [
    # The matching origin is this engine's own page.
    ("a matching origin", {"origin": BASE}, None),
    (
        "a matching origin over ipv4",
        {"host": "127.0.0.1:8443", "origin": "http://127.0.0.1:8443"},
        None,
    ),
    (
        "a matching origin over ipv6",
        {"host": "[::1]:8443", "origin": "http://[::1]:8443"},
        None,
    ),
    # A default port is the same origin as the explicit standard port.
    ("a default port", {"host": "localhost", "origin": "http://localhost"}, None),
    (
        "an explicit standard port",
        {"host": "localhost:80", "origin": "http://localhost"},
        None,
    ),
    # The scheme must match the one the request arrived on.
    (
        "a matching https origin",
        {"scheme": "https", "origin": "https://localhost:8443"},
        None,
    ),
    (
        "an http origin against an https request",
        {"scheme": "https", "origin": "http://localhost:8443"},
        "forbidden_origin",
    ),
    # Cross-port, cross-scheme and cross-host are all refusals.
    (
        "a cross-port origin",
        {"origin": "http://localhost:3000"},
        "forbidden_origin",
    ),
    (
        "a cross-scheme origin on the same port",
        {"origin": "https://localhost:8443"},
        "forbidden_origin",
    ),
    (
        "a cross-host origin",
        {"origin": "http://evil.example:8443"},
        "forbidden_origin",
    ),
    # Malformed and unusable browser origins are refusals.
    ("a null origin", {"origin": "null"}, "forbidden_origin"),
    (
        "a malformed origin",
        {"origin": "http://localhost:not-a-port"},
        "forbidden_origin",
    ),
    ("an unparseable origin", {"origin": "http://[::1"}, "forbidden_origin"),
    # Userinfo is refused by its presence, empty or not.
    (
        "a credential-bearing origin",
        {"origin": "http://user:pw@localhost:8443"},
        "forbidden_origin",
    ),
    (
        "an empty-userinfo origin",
        {"origin": "http://@localhost:8443"},
        "forbidden_origin",
    ),
    (
        "an empty-userinfo referer",
        {"method": "GET", "referer": "http://@localhost:8443/page"},
        "forbidden_origin",
    ),
    (
        "an origin that is not a url",
        {"origin": "localhost:8443"},
        "forbidden_origin",
    ),
    # A browser's Origin is a bare origin: no query, no fragment, no path.
    (
        "an origin with a query",
        {"origin": "http://localhost:8443?a=1"},
        "forbidden_origin",
    ),
    (
        "an origin with a fragment",
        {"origin": "http://localhost:8443#a"},
        "forbidden_origin",
    ),
    (
        "an origin with a path",
        {"origin": "http://localhost:8443/page"},
        "forbidden_origin",
    ),
    # A Referer is a page address, so its path and its query are the
    # page's own and are allowed — but a fragment never is, because
    # browsers strip it before sending.
    (
        "a matching referer on a read",
        {"method": "GET", "referer": f"{BASE}/page"},
        None,
    ),
    (
        "a matching referer with a page query on a read",
        {"method": "GET", "referer": f"{BASE}/?chat=example"},
        None,
    ),
    (
        "a matching referer whose query carries an at sign on a read",
        {"method": "GET", "referer": f"{BASE}/page?email=a@b.example"},
        None,
    ),
    (
        "a matching referer with a fragment on a read",
        {"method": "GET", "referer": f"{BASE}/page#section"},
        "forbidden_origin",
    ),
    (
        "a cross-port referer on a read",
        {"method": "GET", "referer": "http://localhost:3000/page"},
        "forbidden_origin",
    ),
    (
        "a referer on a write is not a substitute",
        {"method": "POST", "referer": f"{BASE}/page"},
        "origin_required",
    ),
    # A missing source is a refusal, not a pass.
    ("no origin and no referer", {"method": "POST"}, "origin_required"),
    (
        "an empty origin header",
        {"method": "POST", "origin": ""},
        "origin_required",
    ),
    # A hostile Origin wins over a good Referer.
    (
        "a hostile origin with a good referer",
        {"method": "POST", "origin": "http://localhost:3000", "referer": f"{BASE}/p"},
        "forbidden_origin",
    ),
]


@pytest.mark.parametrize(
    ("label", "kwargs", "expected"),
    _LOCAL_POLICY_CASES,
    ids=[case[0] for case in _LOCAL_POLICY_CASES],
)
def test_the_origin_must_match_this_engine_s_own_origin_exactly(
    label: str, kwargs: dict[str, Any], expected: str | None
) -> None:
    """The policy's own answers, one per way a browser origin can be wrong."""
    request = _bare_request(**kwargs)  # type: ignore[arg-type]
    assert terminal_policy(request) == expected, label


#: The cases that arrive over plain HTTP: a remote caller on any of them is
#: refused for the transport, before the origin is even read.
_PLAIN_HTTP_CASES = [
    case for case in _LOCAL_POLICY_CASES if case[1].get("scheme", "http") == "http"
]


@pytest.mark.parametrize(
    ("label", "kwargs", "expected"),
    _PLAIN_HTTP_CASES,
    ids=[case[0] for case in _PLAIN_HTTP_CASES],
)
def test_a_remote_caller_is_refused_for_the_transport_before_the_origin(
    label: str, kwargs: dict[str, Any], expected: str | None
) -> None:
    """A remote caller on plain HTTP is refused for the transport, first.

    Whether the connection is secure is a fact about the request that no
    header can talk out of, so it is the honest answer even when the
    origin is wrong too.
    """
    request = _bare_request(peer="100.64.0.7", **kwargs)  # type: ignore[arg-type]
    assert terminal_policy(request) == "https_required", label


def test_a_loopback_plain_http_caller_is_allowed() -> None:
    """The one plain-HTTP exception: this machine, on a loopback Host."""
    request = _bare_request(origin=BASE)
    assert terminal_policy(request) is None


def test_a_loopback_plain_http_caller_with_a_rebound_host_is_refused() -> None:
    """A rebound page's own name is not a local Host, so it gets no exception."""
    request = _bare_request(host="evil.example:8443", origin="http://evil.example:8443")
    assert terminal_policy(request) == "https_required"


def test_a_loopback_plain_http_caller_behind_a_proxy_is_refused() -> None:
    request = _bare_request(
        origin=BASE, extra_headers=(("X-Forwarded-For", "203.0.113.9"),)
    )
    assert terminal_policy(request) == "https_required"


def test_the_plain_http_exception_takes_only_the_real_loopback_hosts() -> None:
    """`0.0.0.0` is the engine's bind address, not a loopback address.

    It is the spelling users copy from the bind-address log, and it is
    accepted as a local `Host` elsewhere in the app — but for the terminal
    it fails closed, because the plain-HTTP exception exists for a
    browser talking to this machine, and `0.0.0.0` names no machine.
    """
    for host in ("localhost:8443", "127.0.0.1:8443", "[::1]:8443"):
        request = _bare_request(host=host, origin=f"http://{host}")
        assert terminal_policy(request) is None, host

    request = _bare_request(host="0.0.0.0:8443", origin="http://0.0.0.0:8443")
    assert terminal_policy(request) == "https_required"


def test_a_remote_https_caller_is_allowed() -> None:
    """A remote caller on a secure transport passes the transport rule."""
    request = _bare_request(
        peer="100.64.0.7",
        scheme="https",
        host="app.example",
        origin="https://app.example",
    )
    assert terminal_policy(request) is None


def test_the_terminal_policy_is_at_least_as_strict_as_the_app_s_own_one() -> None:
    """The two gates never disagree in the dangerous direction.

    The middleware's `_state_change_origin_allowed` runs first on a write,
    and the terminal's own rule is intentionally stricter (it refuses a
    userinfo-bearing or non-bare origin that the app's rule tolerates). So
    the two are not expected to agree everywhere — a request the terminal
    policy refuses may still be one the middleware allows. What must
    never happen is the other way round: a request the middleware calls
    forbidden must not be one the terminal policy calls good.
    """
    for origin in (
        "http://localhost:8443",
        "http://localhost.:8443",
        "http://LOCALHOST:8443",
        "http://localhost:3000",
        "http://evil.example:8443",
        "null",
        "http://@localhost:8443",
        "http://localhost:8443/page",
    ):
        middleware_says = auth._state_change_origin_allowed(
            _bare_request(method="POST", origin=origin)
        )
        route_says = terminal_policy(_bare_request(method="POST", origin=origin))
        if not middleware_says:
            assert route_says is not None, origin


def test_a_missing_origin_is_refused_for_unlock(tmp_path: Path) -> None:
    client = _client(_app(tmp_path))
    response = client.post(UNLOCK, json={"password": PASSWORD})

    assert response.status_code == 403
    assert response.json()["reason"] == "origin_required"


def test_get_requires_origin_or_referer(tmp_path: Path) -> None:
    client = _client(_app(tmp_path))

    body = client.get(STATUS).json()
    assert body["available"] is False
    assert body["reason"] == "origin_required"

    # A matching Referer is the browser's own origin for a read.
    body = client.get(STATUS, headers={"Referer": f"{BASE}/some/page"}).json()
    assert body["available"] is True


def test_a_hostile_origin_wins_over_a_good_referer(tmp_path: Path) -> None:
    client = _client(_app(tmp_path))
    response = client.post(
        UNLOCK,
        json={"password": PASSWORD},
        headers={**_origin("http://evil.example:8443"), "Referer": f"{BASE}/page"},
    )

    assert response.status_code == 403
    assert not _terminal_cookie(client)


# ── The two layers of the origin gate ──────────────────────────────────────


def test_the_middleware_refuses_a_cross_origin_post_before_the_route(
    tmp_path: Path,
) -> None:
    """The ordinary `/api/*` origin check runs first, and answers plainly.

    It is a defence in depth, not the terminal's own policy: the route
    below it never sees the request. The envelope is the middleware's
    own — `{"error": ...}` with no `reason` — so a client must not expect
    every refusal to carry the terminal's `reason` key.
    """
    client = _client(_app(tmp_path))
    response = client.post(
        UNLOCK, json={"password": PASSWORD}, headers=_origin("http://evil.example:8443")
    )

    assert response.status_code == 403
    assert response.json() == {"error": "forbidden origin"}
    assert "reason" not in response.json()
    assert not _terminal_cookie(client)


@pytest.mark.parametrize(
    ("label", "origin"),
    [
        ("an origin with a path", "http://localhost:8443/page"),
        ("an origin with a query", "http://localhost:8443?a=1"),
        ("an origin with a fragment", "http://localhost:8443#f"),
    ],
)
def test_the_route_refuses_an_origin_that_is_not_a_bare_origin(
    tmp_path: Path, label: str, origin: str
) -> None:
    """The route's own origin rule, on the origins the middleware lets through."""
    app = _app(tmp_path)
    client = _client(app)
    response = client.post(UNLOCK, json={"password": PASSWORD}, headers=_origin(origin))

    assert response.status_code == 403, label
    assert response.json()["reason"] == "forbidden_origin", label
    assert app.state.terminal_grants.retained_grant_count() == 0
    assert not _terminal_cookie(client)


def test_the_status_route_answers_the_policy_refusals_with_its_own_reason(
    tmp_path: Path,
) -> None:
    """A read is not state-changing, so the route's own policy answers it."""
    client = _client(_app(tmp_path))

    body = client.get(STATUS, headers=_origin("http://evil.example:8443")).json()
    assert body["available"] is False
    assert body["reason"] == "forbidden_origin"

    body = client.get(STATUS, headers=_origin("http://localhost:8443/page")).json()
    assert body["available"] is False
    assert body["reason"] == "forbidden_origin"


# ── The password, the body and the guessing budget ────────────────────────


def test_a_wrong_password_is_refused_and_no_grant_is_issued(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = _client(app)
    response = client.post(UNLOCK, json={"password": "wrong"}, headers=_origin())

    assert response.status_code == 403
    assert response.json()["reason"] == "wrong_password"
    assert not _terminal_cookie(client)
    assert app.state.terminal_grants.retained_grant_count() == 0


def test_a_missing_or_wrongly_typed_password_is_refused(tmp_path: Path) -> None:
    client = _client(_app(tmp_path))

    for body in ({}, {"password": ""}, {"password": 5}, {"password": None}):
        response = client.post(UNLOCK, json=body, headers=_origin())
        assert response.status_code == 400
    assert not _terminal_cookie(client)


def test_a_malformed_unicode_password_is_refused_safely(tmp_path: Path) -> None:
    """A lone surrogate is a value, not an exception: a counted wrong password."""
    app = _app(tmp_path)
    client = _client(app)
    response = client.post(
        UNLOCK,
        content=json.dumps({"password": _MALFORMED}).encode("utf-8"),
        headers={**_origin(), "Content-Type": "application/json"},
    )

    assert response.status_code == 403
    assert response.json()["reason"] == "wrong_password"
    assert _MALFORMED not in response.text
    assert not _terminal_cookie(client)


def test_a_malformed_password_counts_towards_the_limit(tmp_path: Path) -> None:
    client = _client(_app(tmp_path))
    for _ in range(TERMINAL_UNLOCK_FAIL_LIMIT):
        response = client.post(
            UNLOCK,
            content=json.dumps({"password": _MALFORMED}).encode("utf-8"),
            headers={**_origin(), "Content-Type": "application/json"},
        )
        assert response.status_code == 403

    # The window is closed: the correct password is refused too, and the
    # refusal is a rate limit rather than a wrong-password answer.
    response = client.post(UNLOCK, json={"password": PASSWORD}, headers=_origin())
    assert response.status_code == 429
    assert response.json()["reason"] == "rate_limited"
    assert response.headers.get("Retry-After")
    assert not _terminal_cookie(client)


def test_locked_out_attempts_cannot_be_bypassed(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = _client(app)
    for _ in range(TERMINAL_UNLOCK_FAIL_LIMIT):
        assert (
            client.post(
                UNLOCK, json={"password": "wrong"}, headers=_origin()
            ).status_code
            == 403
        )

    # The right password inside the closed window is refused as well.
    response = client.post(UNLOCK, json={"password": PASSWORD}, headers=_origin())
    assert response.status_code == 429
    assert app.state.terminal_grants.retained_grant_count() == 0


def test_an_unknown_key_in_the_body_is_refused(tmp_path: Path) -> None:
    client = _client(_app(tmp_path))
    for body in (
        {"password": PASSWORD, "extra": 1},
        {"password": PASSWORD, "grant": "x"},
    ):
        response = client.post(UNLOCK, json=body, headers=_origin())
        assert response.status_code == 400
    assert not _terminal_cookie(client)


def test_malformed_json_is_refused(tmp_path: Path) -> None:
    client = _client(_app(tmp_path))
    response = client.post(
        UNLOCK,
        content=b"{nope",
        headers={**_origin(), "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert not _terminal_cookie(client)


def test_json_body_is_bounded_including_chunked_and_wrong_content_length(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    client = _client(app)

    # A declared Content-Length over the cap is refused before the body is
    # read.
    oversized = b'{"password": "' + b"a" * 5000 + b'"}'
    response = client.post(
        UNLOCK,
        content=oversized,
        headers={**_origin(), "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert not _terminal_cookie(client)

    # A *lying* Content-Length (a small one over a big body) is refused
    # while the body is read, because the declared length is a claim.
    response = client.post(
        UNLOCK,
        content=oversized,
        headers={
            **_origin(),
            "Content-Type": "application/json",
            "Content-Length": "10",
        },
    )
    assert response.status_code == 413

    # A chunked request declares nothing, so the cap has to hold while
    # reading.
    def _chunks():
        yield b'{"password": "'
        yield b"a" * 5000
        yield b'"}'

    response = client.post(
        UNLOCK,
        content=_chunks(),
        headers={**_origin(), "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert not _terminal_cookie(client)
    assert app.state.terminal_grants.retained_grant_count() == 0


def test_a_password_longer_than_the_cap_is_refused(tmp_path: Path) -> None:
    client = _client(_app(tmp_path))
    response = client.post(UNLOCK, json={"password": "a" * 1025}, headers=_origin())
    assert response.status_code == 400
    assert not _terminal_cookie(client)


# ── Grant rotation ─────────────────────────────────────────────────────────


def test_unlock_rotates_only_the_presented_grant_cookie(tmp_path: Path) -> None:
    """A re-unlock revokes the grant it renews, and only that one."""
    app = _app(tmp_path)
    store: TerminalGrantStore = app.state.terminal_grants

    first_client = _client(app)
    assert (
        first_client.post(
            UNLOCK, json={"password": PASSWORD}, headers=_origin()
        ).status_code
        == 200
    )
    first = _terminal_cookie(first_client)
    assert first

    # A second unlock in another browser (the same login cookie, no grant
    # cookie of its own) is a separate grant: it must not revoke the first.
    other = _client(app)
    other.cookies.set(SESSION_COOKIE, first_client.cookies.get(SESSION_COOKIE) or "")
    assert (
        other.post(UNLOCK, json={"password": PASSWORD}, headers=_origin()).status_code
        == 200
    )
    second = _terminal_cookie(other)
    assert second and second != first

    assert store.retained_grant_count() == 2

    # The first browser's own re-unlock rotates its grant: the presented
    # one dies, the new one lives, and the other browser's grant is not
    # touched.
    assert (
        first_client.post(
            UNLOCK, json={"password": PASSWORD}, headers=_origin()
        ).status_code
        == 200
    )
    rotated = _terminal_cookie(first_client)
    assert rotated and rotated != first

    session_cookie = first_client.cookies.get(SESSION_COOKIE) or ""
    assert store.verify(first, session_cookie) is None
    assert store.verify(rotated, session_cookie) is not None
    assert store.verify(second, session_cookie) is not None


def test_grant_capacity_is_a_refusal_and_not_a_crash(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = _client(app)
    store: TerminalGrantStore = app.state.terminal_grants

    for index in range(TERMINAL_MAX_GRANTS):
        store.issue(f"other-session-{index}")

    response = client.post(UNLOCK, json={"password": PASSWORD}, headers=_origin())
    assert response.status_code == 429
    assert response.json()["reason"] == "capacity"
    assert response.headers["cache-control"] == "no-store"
    assert not _terminal_cookie(client)


# ── The factory wires it up ────────────────────────────────────────────────


def _config(tmp_path: Path):
    from ciao.config import CiaoConfig, WorkspaceConfig

    return CiaoConfig(
        pwa_auth_token=SECRET,
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime",
        media_root=tmp_path / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "personal": WorkspaceConfig(
                name="personal", vault_root="memory-vault/personal"
            )
        },
    )


def test_factory_initializes_a_separate_grant_store_per_app_and_registers_its_routes(
    tmp_path: Path,
) -> None:
    first = create_app(_config(tmp_path))
    second = create_app(_config(tmp_path))

    store = first.state.terminal_grants
    assert isinstance(store, TerminalGrantStore)
    # Two apps, two stores: one app's grants are not another's.
    assert store is not second.state.terminal_grants

    paths = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in first.routes
        if getattr(route, "path", "").startswith("/api/terminal")
    }
    assert paths == {
        (UNLOCK, ("POST",)),
        (STATUS, ("GET", "HEAD")),
        # The session routes (#1799) live behind the same door.
        (SESSIONS, ("GET", "HEAD")),
        (SESSIONS, ("POST",)),
        (f"{SESSIONS}/{{session_id}}", ("DELETE",)),
    }


def test_the_factory_app_answers_the_status_route(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path))
    client = TestClient(app, base_url=BASE, client=("127.0.0.1", 50000))

    # Without a session the status route is a 401, exactly like every other
    # `/api/*` route.
    assert client.get(STATUS).status_code == 401

    serializer = app.state.serializer
    client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))
    body = client.get(STATUS, headers=_origin()).json()
    assert body["available"] is True


# ── The native terminal route is untouched ─────────────────────────────────


def test_native_host_terminal_is_still_remote_refused(tmp_path: Path) -> None:
    """The native route keeps its own, stricter "this is the host" rule."""
    from ciao.web.routes_host_terminal import host_terminal_status

    serializer = URLSafeTimedSerializer(SECRET)
    app = Starlette(
        routes=[Route("/api/host/terminal", host_terminal_status, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        workspace_root=tmp_path, state_path=tmp_path / ".runtime" / "state.json"
    )
    client = TestClient(app, base_url=BASE, client=("100.64.0.7", 50000))
    client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))

    body = client.get("/api/host/terminal", headers=_origin()).json()
    assert body["available"] is False
    assert body["reason"] == "not_on_host"


# ── The route's own session check, without the middleware ─────────────────


def _bare_app(tmp_path: Path) -> Starlette:
    """An app with the terminal routes and **no** `AuthMiddleware`.

    The middleware is the outer gate; this app removes it, so the route's
    own session check is the only thing standing between a request and a
    password comparison.
    """
    serializer = URLSafeTimedSerializer(SECRET)
    app = Starlette(
        routes=[
            Route(UNLOCK, terminal_unlock, methods=["POST"]),
            Route(STATUS, terminal_status, methods=["GET"]),
        ]
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        pwa_auth_token=PASSWORD,
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
    )
    app.state.terminal_clock = time.monotonic
    app.state.terminal_grants = TerminalGrantStore(clock=app.state.terminal_clock)
    return app


def test_the_route_refuses_an_unsigned_request_without_the_middleware(
    tmp_path: Path,
) -> None:
    """The route checks the session itself; the middleware is not the only gate."""
    app = _bare_app(tmp_path)
    client = TestClient(app, base_url=BASE, client=("127.0.0.1", 50000))

    response = client.get(STATUS, headers=_origin())
    assert response.status_code == 401
    assert response.json()["reason"] == "unauthorized"

    response = client.post(UNLOCK, json={"password": PASSWORD}, headers=_origin())
    assert response.status_code == 401
    assert response.json()["reason"] == "unauthorized"
    assert "set-cookie" not in response.headers
    assert app.state.terminal_grants.retained_grant_count() == 0


def test_an_unsigned_unlock_reads_no_body_and_issues_no_grant(
    tmp_path: Path,
) -> None:
    """The refusal comes before the body is read, so no guess is counted."""
    app = _bare_app(tmp_path)
    client = TestClient(app, base_url=BASE, client=("127.0.0.1", 50000))

    response = client.post(UNLOCK, json={"password": PASSWORD}, headers=_origin())
    assert response.status_code == 401
    # No guess was counted against the client address.
    assert app.state.terminal_grants.retained_grant_count() == 0
    assert not _terminal_cookie(client)


def test_a_tampered_session_cookie_is_refused_without_the_middleware(
    tmp_path: Path,
) -> None:
    app = _bare_app(tmp_path)
    client = TestClient(app, base_url=BASE, client=("127.0.0.1", 50000))
    client.cookies.set(SESSION_COOKIE, "not-a-signed-session")

    assert client.get(STATUS, headers=_origin()).status_code == 401
    assert (
        client.post(UNLOCK, json={"password": PASSWORD}, headers=_origin()).status_code
        == 401
    )
    assert app.state.terminal_grants.retained_grant_count() == 0


def test_a_session_signed_with_another_key_is_refused_without_the_middleware(
    tmp_path: Path,
) -> None:
    """A re-keyed password invalidates the old session, at the route too."""
    app = _bare_app(tmp_path)
    client = TestClient(app, base_url=BASE, client=("127.0.0.1", 50000))
    other = URLSafeTimedSerializer("a-different-secret")
    client.cookies.set(SESSION_COOKIE, other.dumps({"user": "owner"}))

    assert client.get(STATUS, headers=_origin()).status_code == 401
    assert (
        client.post(UNLOCK, json={"password": PASSWORD}, headers=_origin()).status_code
        == 401
    )
    assert app.state.terminal_grants.retained_grant_count() == 0


def test_a_signed_session_is_accepted_without_the_middleware(tmp_path: Path) -> None:
    """The signed happy case still works when the middleware is not there."""
    app = _bare_app(tmp_path)
    client = TestClient(app, base_url=BASE, client=("127.0.0.1", 50000))
    client.cookies.set(SESSION_COOKIE, app.state.serializer.dumps({"user": "owner"}))

    body = client.get(STATUS, headers=_origin()).json()
    assert body["available"] is True

    response = client.post(UNLOCK, json={"password": PASSWORD}, headers=_origin())
    assert response.status_code == 200
    assert _terminal_cookie(client)


def test_the_grant_helper_needs_a_session_of_its_own(tmp_path: Path) -> None:
    """`terminal_grant` is not a way around the session."""
    app = _bare_app(tmp_path)
    serializer = app.state.serializer
    session = serializer.dumps({"user": "owner"})

    # A grant that exists, presented without a session.
    token, _grant = app.state.terminal_grants.issue(session)
    request = _bare_request(method="GET", origin=BASE)
    request.scope["app"] = app
    request.scope["headers"].append(
        (b"cookie", f"{TERMINAL_COOKIE}={token}".encode("latin-1"))
    )
    assert terminal_grant(request) is None

    # The same grant, presented with a live session, is a grant.
    request = _bare_request(method="GET", origin=BASE)
    request.scope["app"] = app
    request.scope["headers"].extend(
        (
            (b"cookie", f"{SESSION_COOKIE}={session}".encode("latin-1")),
            (b"cookie", f"{TERMINAL_COOKIE}={token}".encode("latin-1")),
        )
    )
    assert terminal_grant(request) is not None


def test_the_direct_handler_tests_would_catch_a_disabled_session_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direct-handler tests are load-bearing, not decorative.

    The route's own session check is neutered here, and the unsigned
    request that the direct tests above refuse now reaches the password
    comparison instead. A suite that only ever went through the
    middleware would never have noticed — so the direct tests are what
    pin the defence in depth.
    """
    from ciao.web import routes_terminal

    app = _bare_app(tmp_path)
    client = TestClient(app, base_url=BASE, client=("127.0.0.1", 50000))

    # Unmutated: the route's own check refuses the unsigned request
    # before the body is read, so the password is never compared.
    response = client.post(UNLOCK, json={"password": "wrong"}, headers=_origin())
    assert response.status_code == 401
    assert response.json()["reason"] == "unauthorized"

    monkeypatch.setattr(routes_terminal, "_session_ok", lambda request: True)

    # Mutated: the same unsigned request now reaches the password
    # comparison, which is the difference the direct tests assert. The
    # direct tests above expect a 401, so they fail against this build.
    response = client.post(UNLOCK, json={"password": "wrong"}, headers=_origin())
    assert response.status_code == 403
    assert response.json()["reason"] == "wrong_password"
