"""`/api/host/terminal`: the auth boundary and the wire contract.

Real ``AuthMiddleware`` and real handlers; ``host_terminal.capabilities`` and
``launch`` are recorders, so nothing is ever opened.
"""

from __future__ import annotations

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

from ciao import host_terminal
from ciao.web import auth
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.routes_api import host_browser_request
from ciao.web.routes_host_terminal import host_terminal_launch, host_terminal_status

SECRET = "test-secret"
BASE = "http://localhost:8443"
PATH = "/api/host/terminal"


class _Recorder:
    def __init__(self) -> None:
        self.launches: list[tuple[Path, str | None, Path]] = []
        self.caps = host_terminal.Capabilities(True, "macos", terminal="Terminal")
        self.launch_error: Exception | None = None
        self.capability_calls: list[None] = []

    def capabilities(self) -> host_terminal.Capabilities:
        self.capability_calls.append(None)
        return self.caps

    def launch(self, folder: Path, *, action: str | None, runtime_dir: Path) -> str:
        if self.launch_error is not None:
            raise self.launch_error
        self.launches.append((folder, action, runtime_dir))
        return "Terminal"


class _Pcm:
    def __init__(self, folder: Path) -> None:
        chat = SimpleNamespace(id="c1", project_id="p1", code_mode=False, repo_location=None)
        self.project = SimpleNamespace(id="p1", repo_path=str(folder), workspace="")
        self.chat = chat

    def get_chat(self, chat_id: str) -> Any:
        return self.chat if chat_id == "c1" else None

    def get_project(self, project_id: str) -> Any:
        return self.project if project_id == "p1" else None


def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    peer: str = "127.0.0.1",
    base_url: str = BASE,
) -> tuple[TestClient, _Recorder, Path]:
    recorder = _Recorder()
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    serializer = URLSafeTimedSerializer(SECRET)
    app = Starlette(
        routes=[
            Route(PATH, host_terminal_status, methods=["GET"]),
            Route(PATH, host_terminal_launch, methods=["POST"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        workspace_root=tmp_path, state_path=tmp_path / ".runtime" / "state.json"
    )
    app.state.project_chat_manager = _Pcm(repo)
    monkeypatch.setattr(host_terminal, "capabilities", recorder.capabilities)
    monkeypatch.setattr(host_terminal, "launch", recorder.launch)
    return TestClient(app, base_url=base_url, client=(peer, 50000)), recorder, repo


def _signed(client: TestClient, origin: str = BASE) -> dict[str, str]:
    cookie = f"{SESSION_COOKIE}={client.app.state.serializer.dumps({'user': 'owner'})}"
    return {"Cookie": cookie, "Origin": origin}


def _request(
    *,
    host: str = "localhost:8443",
    peer: str = "127.0.0.1",
    scheme: str = "http",
    origin: str | None = None,
    referer: str | None = None,
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> Request:
    """A bare Starlette request, so the helper can be exercised directly.

    The Host header is what the browser sent, the peer address is the TCP
    source, and the scheme is the one the request arrived on.
    """
    raw: list[tuple[bytes, bytes]] = [(b"host", host.encode("latin-1"))]
    if origin is not None:
        raw.append((b"origin", origin.encode("latin-1")))
    if referer is not None:
        raw.append((b"referer", referer.encode("latin-1")))
    raw.extend((name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in extra_headers)
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": scheme,
        "path": "/api/host/terminal",
        "raw_path": b"/api/host/terminal",
        "query_string": b"",
        "root_path": "",
        "headers": raw,
        "client": (peer, 50000),
        "server": ("127.0.0.1", 8443),
    }
    return Request(scope)


# The exact-origin matrix for `host_browser_request`: scheme, host and
# effective port must all match the request's own origin. `Origin` wins over
# `Referer`; `Referer` is only read when `Origin` is absent.
_ORIGIN_CASES: list[tuple[str, dict[str, object], bool]] = [
    # The matching origin is the host browser.
    ("matching origin", {"origin": "http://localhost:8443"}, True),
    ("matching origin over ipv4", {"host": "127.0.0.1:8443", "origin": "http://127.0.0.1:8443"}, True),
    ("matching origin over ipv6", {"host": "[::1]:8443", "origin": "http://[::1]:8443"}, True),
    # The installed-user spelling: 0.0.0.0 is a local Host, not a loopback address.
    ("the 0.0.0.0 host spelling", {"host": "0.0.0.0:8443", "origin": "http://0.0.0.0:8443"}, True),
    # ...and that spelling is not a port bypass.
    ("the 0.0.0.0 host spelling with a cross-port origin", {"host": "0.0.0.0:8443", "origin": "http://0.0.0.0:3000"}, False),
    # A default port is the same origin as the explicit standard port.
    ("a default port", {"host": "localhost", "origin": "http://localhost"}, True),
    ("an explicit standard port", {"host": "localhost:80", "origin": "http://localhost"}, True),
    ("a default port against an explicit standard port", {"host": "localhost", "origin": "http://localhost:80"}, True),
    # The same for https, in both directions: the effective port, not the text.
    ("an https default port against an explicit 443", {"host": "localhost", "scheme": "https", "origin": "https://localhost:443"}, True),
    ("an explicit 443 against an https default port", {"host": "localhost:443", "scheme": "https", "origin": "https://localhost"}, True),
    # The scheme must match the one the request arrived on.
    ("a matching https origin", {"scheme": "https", "origin": "https://localhost:8443"}, True),
    ("an http origin against an https request", {"scheme": "https", "origin": "http://localhost:8443"}, False),
    # The dev server proxies /api with the browser's own Host and Origin.
    ("the vite dev origin", {"host": "localhost:5173", "origin": "http://localhost:5173"}, True),
    # Cross-port, cross-scheme and cross-host are all refusals.
    ("a cross-port origin", {"origin": "http://localhost:3000"}, False),
    ("a cross-scheme origin on the same port", {"origin": "https://localhost:8443"}, False),
    ("a cross-scheme origin on the default port", {"host": "localhost", "origin": "https://localhost"}, False),
    ("a cross-host origin", {"origin": "http://evil.example:8443"}, False),
    # Malformed and unusable browser origins are refusals.
    ("a null origin", {"origin": "null"}, False),
    ("a malformed origin", {"origin": "http://localhost:not-a-port"}, False),
    ("an unparseable origin", {"origin": "http://[::1"}, False),
    ("a credential-bearing origin", {"origin": "http://user:pw@localhost:8443"}, False),
    ("an origin that is not a url", {"origin": "localhost:8443"}, False),
    # A hostile Origin wins over a good Referer...
    ("a hostile origin with a good referer", {"origin": "http://localhost:3000", "referer": "http://localhost:8443/page"}, False),
    # ...and a good Origin wins over a hostile Referer.
    ("a matching origin with a hostile referer", {"origin": "http://localhost:8443", "referer": "http://evil.example/page"}, True),
    # Referer is only read when Origin is absent.
    ("a matching referer", {"referer": "http://localhost:8443/page"}, True),
    ("a cross-port referer", {"referer": "http://localhost:3000/page"}, False),
    ("a cross-scheme referer", {"referer": "https://localhost:8443/page"}, False),
    # An empty Origin carries no origin information, so it reads as absent and
    # the peer/Host gating alone decides (as `auth._same_origin_source` does).
    ("an empty origin header", {"origin": ""}, True),
    ("an empty origin header with a cross-port referer", {"origin": "", "referer": "http://localhost:3000/page"}, False),
    # No browser headers at all: the peer/Host gating decides.
    ("no origin and no referer", {}, True),
    ("no origin and no referer from a remote peer", {"peer": "100.64.0.7"}, False),
]


@pytest.mark.parametrize(
    ("label", "kwargs", "allowed"),
    _ORIGIN_CASES,
    ids=[case[0] for case in _ORIGIN_CASES],
)
def test_host_browser_request_requires_exact_origin(
    label: str, kwargs: dict[str, object], allowed: bool
) -> None:
    request = _request(**kwargs)  # type: ignore[arg-type]
    assert host_browser_request(request) is allowed


def test_host_browser_request_refuses_a_remote_peer_claiming_localhost() -> None:
    request = _request(peer="100.64.0.7", origin="http://localhost:8443")
    assert host_browser_request(request) is False


def test_host_browser_request_refuses_a_loopback_peer_behind_a_proxy() -> None:
    request = _request(
        origin="http://localhost:8443",
        extra_headers=(("X-Forwarded-For", "203.0.113.9"),),
    )
    assert host_browser_request(request) is False


def test_host_browser_request_refuses_a_rebound_host() -> None:
    request = _request(host="evil.example:8443", origin="http://evil.example:8443")
    assert host_browser_request(request) is False


def test_host_browser_request_does_not_trust_a_forwarded_host() -> None:
    """A proxy-declared host must not stand in for the browser's own origin.

    `auth._same_origin` accepts an origin naming a proxy-declared
    `X-Forwarded-Host`; here the peer check refuses a loopback peer carrying
    any `X-Forwarded-*` header first, so that path is never reached.
    """
    request = _request(
        origin="http://app.example",
        extra_headers=(("X-Forwarded-Host", "app.example"),),
    )
    assert host_browser_request(request) is False


def test_bootstrap_setup_still_accepts_a_same_host_cross_port_origin() -> None:
    """First-run setup keeps its own, looser rule: bootstrap is out of scope here.

    `_setup_finish_origin_allowed` compares host names only, so a setup request
    from another local port is still accepted. This pins that on purpose: a
    future "reuse the exact-origin helper everywhere" refactor must break this
    test rather than change first-run setup silently.
    """
    from ciao.web.routes_api import _setup_finish_origin_allowed

    request = _request(origin="http://localhost:3000")
    assert _setup_finish_origin_allowed(request) is True
    # The exact-origin helper refuses the same request, which is what makes the
    # difference between the two rules visible.
    assert host_browser_request(request) is False


def test_route_is_in_neither_middleware_allowlist() -> None:
    assert PATH not in auth._PUBLIC_API
    assert PATH not in auth._LOOPBACK_ONLY_API


def test_loopback_without_a_session_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    assert client.get(PATH).status_code == 401
    assert client.post(PATH, json={"action": "gws-install"}, headers={"Origin": BASE}).status_code == 401
    assert rec.launches == []


def test_remote_signed_in_browser_sees_not_on_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _client(tmp_path, monkeypatch, peer="100.64.0.7")
    response = client.get(PATH, headers=_signed(client))
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False and body["reason"] == "not_on_host" and body["actions"] == []


def test_remote_signed_in_browser_cannot_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch, peer="100.64.0.7")
    response = client.post(PATH, json={"action": "gws-install"}, headers=_signed(client))
    assert response.status_code == 403
    assert response.json()["reason"] == "not_on_host"
    assert rec.launches == []


def test_rebound_host_header_is_not_on_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin = "http://evil.example:8443"
    client, rec, _ = _client(tmp_path, monkeypatch, base_url=origin)
    response = client.post(PATH, json={"action": "gws-install"}, headers=_signed(client, origin))
    assert response.status_code == 403
    assert response.json()["reason"] == "not_on_host"
    assert rec.launches == []


def test_loopback_peer_behind_a_proxy_is_not_on_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    headers = {**_signed(client), "X-Forwarded-For": "203.0.113.9"}
    response = client.post(PATH, json={"action": "gws-install"}, headers=headers)
    assert response.status_code == 403
    assert rec.launches == []


def test_cross_origin_post_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    response = client.post(
        PATH, json={"action": "gws-install"}, headers=_signed(client, "https://evil.example")
    )
    assert response.status_code == 403
    assert rec.launches == []


def test_status_on_the_host_reports_capabilities(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _client(tmp_path, monkeypatch)
    body = client.get(PATH, headers=_signed(client)).json()
    assert body["available"] is True and body["terminal"] == "Terminal"
    assert body["actions"] == list(host_terminal.ACTION_IDS)


def test_action_launches_in_the_workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    response = client.post(PATH, json={"action": "gws-install"}, headers=_signed(client))
    assert response.status_code == 200
    assert response.json()["ok"] is True and response.json()["launched"] == "Terminal"
    assert rec.launches == [(tmp_path, "gws-install", tmp_path / ".runtime")]


@pytest.mark.parametrize(
    "body",
    [
        {"action": "rm -rf"},
        {"action": 5},
        {"command": "ls"},
        {"action": "gws-install", "command": "ls"},
        {"action": "gws-install", "path": "/etc"},
        {"open": "folder"},
        {"open": "/etc", "chat_id": "c1"},
        {"open": "folder", "chat_id": "c1", "path": "/etc"},
        ["gws-install"],
    ],
)
def test_unknown_action_and_free_text_command_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: Any
) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    response = client.post(PATH, json=body, headers=_signed(client))
    assert response.status_code == 400
    assert rec.launches == []


def test_invalid_json_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    response = client.post(PATH, content=b"{nope", headers=_signed(client))
    assert response.status_code == 400
    assert rec.launches == []


def test_folder_open_resolves_from_the_chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, repo = _client(tmp_path, monkeypatch)
    response = client.post(PATH, json={"open": "folder", "chat_id": "c1"}, headers=_signed(client))
    assert response.status_code == 200
    assert rec.launches == [(repo.resolve(), None, tmp_path / ".runtime")]


def test_unknown_chat_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    response = client.post(PATH, json={"open": "folder", "chat_id": "nope"}, headers=_signed(client))
    assert response.status_code == 404
    assert rec.launches == []


def test_headless_host_is_409_with_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    rec.launch_error = host_terminal.TerminalUnavailable("unavailable_headless")
    response = client.post(PATH, json={"action": "gws-install"}, headers=_signed(client))
    assert response.status_code == 409
    assert response.json()["reason"] == "unavailable_headless"


def test_update_action_not_applicable_is_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao import provider_versions

    client, rec, _ = _client(tmp_path, monkeypatch)

    def launch(folder: Path, *, action: str | None, runtime_dir: Path) -> str:
        host_terminal.action_command(action or "")
        return rec.launch(folder, action=action, runtime_dir=runtime_dir)

    monkeypatch.setattr(host_terminal, "launch", launch)
    monkeypatch.setattr(
        provider_versions, "current_checks", lambda: {"opencode": {"state": "current"}}
    )
    response = client.post(PATH, json={"action": "opencode-update"}, headers=_signed(client))
    assert response.status_code == 409
    assert response.json()["reason"] == "not_applicable"
    assert rec.launches == []


def test_a_lookup_error_from_launch_is_not_reported_as_not_applicable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    rec.launch_error = LookupError("something else")
    with pytest.raises(LookupError):
        client.post(PATH, json={"action": "gws-install"}, headers=_signed(client))


def test_launch_oserror_is_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    rec.launch_error = OSError("boom")
    response = client.post(PATH, json={"action": "gws-install"}, headers=_signed(client))
    assert response.status_code == 500
    assert response.json()["reason"] == "launch_failed"


def test_rebound_host_header_sees_not_on_host_on_get(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin = "http://evil.example:8443"
    client, _, _ = _client(tmp_path, monkeypatch, base_url=origin)
    body = client.get(PATH, headers=_signed(client, origin)).json()
    assert body["available"] is False and body["reason"] == "not_on_host"


def test_proxy_header_sees_not_on_host_on_get(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _client(tmp_path, monkeypatch)
    headers = {**_signed(client), "X-Forwarded-For": "203.0.113.9"}
    body = client.get(PATH, headers=headers).json()
    assert body["available"] is False and body["reason"] == "not_on_host"


def test_cross_port_origin_sees_not_on_host_on_get(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A page on another local port gets the host-browser answer, not the capabilities."""
    client, rec, _ = _client(tmp_path, monkeypatch)
    headers = {**_signed(client, "http://localhost:3000")}
    body = client.get(PATH, headers=headers).json()
    assert body["available"] is False and body["reason"] == "not_on_host"
    assert body["actions"] == []
    # The refusal must not reach the native capability probe.
    assert rec.capability_calls == []


def test_cross_port_referer_sees_not_on_host_on_get(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an Origin the Referer is the browser's own origin, and it must match exactly."""
    client, rec, _ = _client(tmp_path, monkeypatch)
    headers = {
        "Cookie": f"{SESSION_COOKIE}={client.app.state.serializer.dumps({'user': 'owner'})}",
        "Referer": "http://localhost:3000/page",
    }
    body = client.get(PATH, headers=headers).json()
    assert body["available"] is False and body["reason"] == "not_on_host"
    assert body["actions"] == []
    assert rec.capability_calls == []


def test_cross_scheme_origin_sees_not_on_host_on_get(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A page on the same host and port but the other scheme is a different origin."""
    client, rec, _ = _client(tmp_path, monkeypatch)
    headers = {**_signed(client, "https://localhost:8443")}
    body = client.get(PATH, headers=headers).json()
    assert body["available"] is False and body["reason"] == "not_on_host"
    assert body["actions"] == []
    # The refusal must not reach the native capability probe.
    assert rec.capability_calls == []


def test_cross_port_post_cannot_launch_a_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The state-changing middleware refuses a cross-port Origin before the handler."""
    client, rec, _ = _client(tmp_path, monkeypatch)
    response = client.post(
        PATH, json={"action": "gws-install"}, headers=_signed(client, "http://localhost:3000")
    )
    assert response.status_code == 403
    assert rec.launches == []


@pytest.mark.parametrize(
    "body",
    [
        {"open": "folder", "chat_id": {"a": 1}},
        {"open": "folder", "project_id": 5},
        {"open": "folder", "chat_id": "c1", "project_id": ["p1"]},
    ],
)
def test_non_string_ids_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: Any) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    response = client.post(PATH, json=body, headers=_signed(client))
    assert response.status_code == 400
    assert response.json()["reason"] == "bad_request"
    assert rec.launches == []
