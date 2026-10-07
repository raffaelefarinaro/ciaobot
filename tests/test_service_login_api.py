"""`GET`/`PATCH /api/service/login`: the wire contract and the auth boundary.

The handlers are the real ones and the middleware is the real
``AuthMiddleware``; only the OS seam is replaced. So these tests are about what
a caller can do to this engine:

* nothing without the signed session cookie — including a caller on loopback,
  because the route is deliberately absent from the loopback-only allowlist, so
  a local process cannot flip a per-user service without a session;
* a cross-origin PATCH is refused even with a valid cookie;
* a body that is not exactly ``{"enabled": <bool>}`` never reaches the service
  layer at all;
* the workspace is the engine's own root, taken from config and not from the
  request;
* a refusal is a 409 with the fresh status, an OS failure is a 503, and
  neither ever reports a position the machine did not confirm.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import service_login
from ciao.web import app as web_app
from ciao.web import auth
from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_service_login import service_login_status, service_login_update

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRET = "test-secret"
ORIGIN = "https://ciao.example"
HOST = "ciao.example"


class _Recorder:
    """Stands in for `service_login`, recording what the routes asked it."""

    def __init__(self) -> None:
        self.status_calls: list[Path] = []
        self.set_calls: list[tuple[Path, bool]] = []
        self.status_result: service_login.LoginStatus | Exception = service_login.LoginStatus(
            platform="macos",
            supported=True,
            installed=True,
            enabled=False,
            can_change=True,
            reason="The engine service will not start when this user signs in.",
            setup_command=None,
        )
        self.set_result: service_login.LoginStatus | Exception = service_login.LoginStatus(
            platform="macos",
            supported=True,
            installed=True,
            enabled=True,
            can_change=True,
            reason="The engine service will start when this user signs in.",
            setup_command=None,
        )

    def login_status(self, workspace: Path) -> service_login.LoginStatus:
        self.status_calls.append(Path(workspace))
        if isinstance(self.status_result, Exception):
            raise self.status_result
        return self.status_result

    def set_login_enabled(
        self, workspace: Path, enabled: bool
    ) -> service_login.LoginStatus:
        self.set_calls.append((Path(workspace), enabled))
        if isinstance(self.set_result, Exception):
            raise self.set_result
        return self.set_result


def _client(
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    client_host: str = "127.0.0.1",
) -> TestClient:
    """A minimal app with the real middleware, the real handlers, one workspace."""
    serializer = URLSafeTimedSerializer(SECRET)
    app = Starlette(
        routes=[
            Route("/api/service/login", service_login_status, methods=["GET"]),
            Route("/api/service/login", service_login_update, methods=["PATCH"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        workspace_root=tmp_path / "workspace",
    )
    monkeypatch.setattr(service_login, "login_status", recorder.login_status)
    monkeypatch.setattr(service_login, "set_login_enabled", recorder.set_login_enabled)
    return TestClient(app, base_url=f"https://{HOST}", client=(client_host, 51000))


def _signed(client: TestClient) -> dict[str, str]:
    """Headers carrying a valid session cookie and a same-origin browser."""
    cookie = f"{SESSION_COOKIE}={client.app.state.serializer.dumps({'user': 'owner'})}"
    return {"Cookie": cookie, "Origin": ORIGIN}


# --------------------------------------------------------------------------- #
# The auth boundary
# --------------------------------------------------------------------------- #


def test_get_needs_a_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A local caller is not a caller: the route is in neither allowlist, so a
    loopback peer buys it nothing."""
    recorder = _Recorder()
    with _client(recorder, monkeypatch, tmp_path) as client:
        response = client.get("/api/service/login")

    assert response.status_code == 401
    assert recorder.status_calls == []


def test_patch_needs_a_session_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    with _client(recorder, monkeypatch, tmp_path) as client:
        response = client.patch("/api/service/login", json={"enabled": True})

    assert response.status_code == 401
    assert recorder.set_calls == []


def test_a_signed_session_from_a_remote_client_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    with _client(recorder, monkeypatch, tmp_path, client_host="203.0.113.9") as client:
        headers = _signed(client)
        status = client.get("/api/service/login", headers=headers)
        update = client.patch(
            "/api/service/login", json={"enabled": True}, headers=headers
        )

    assert status.status_code == 200
    assert update.status_code == 200
    # The workspace is the engine's own root, never anything from the request.
    assert recorder.status_calls == [tmp_path / "workspace"]
    assert recorder.set_calls == [(tmp_path / "workspace", True)]


def test_a_cross_origin_patch_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    with _client(recorder, monkeypatch, tmp_path) as client:
        response = client.patch(
            "/api/service/login",
            json={"enabled": True},
            headers={"Cookie": _signed(client)["Cookie"], "Origin": "https://evil.example"},
        )

    assert response.status_code == 403
    assert recorder.set_calls == []


def test_a_cross_origin_get_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The origin guard is only applied to unsafe methods; this pins that GET
    is a safe read, so it is the session cookie alone that protects it."""
    recorder = _Recorder()
    with _client(recorder, monkeypatch, tmp_path) as client:
        response = client.get(
            "/api/service/login",
            headers={"Cookie": _signed(client)["Cookie"], "Origin": "https://evil.example"},
        )

    assert response.status_code == 200
    assert recorder.status_calls == [tmp_path / "workspace"]


def test_the_route_is_in_neither_middleware_allowlist() -> None:
    """A future edit that added it would quietly give up both checks above."""
    path = "/api/service/login"
    assert path not in auth._PUBLIC_API
    assert path not in auth._LOOPBACK_ONLY_API


# --------------------------------------------------------------------------- #
# The route table
# --------------------------------------------------------------------------- #


def _declared_routes() -> list[tuple[str, str, str]]:
    """(path, methods, handler name) parsed out of ``create_app``'s table."""
    tree = ast.parse((REPO_ROOT / "ciao" / "web" / "app.py").read_text(encoding="utf-8"))
    rows: list[tuple[str, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "Route" or len(node.args) < 2:
            continue
        path, endpoint = node.args[0], node.args[1]
        if not isinstance(path, ast.Constant) or not isinstance(endpoint, ast.Name):
            continue
        methods = ""
        for keyword in node.keywords:
            if keyword.arg == "methods" and isinstance(keyword.value, ast.List):
                methods = ",".join(
                    e.value for e in keyword.value.elts if isinstance(e, ast.Constant)
                )
        rows.append((path.value, methods, endpoint.id))
    return rows


def test_create_app_serves_both_methods_from_the_new_module() -> None:
    declared = _declared_routes()
    assert ("/api/service/login", "GET", "service_login_status") in declared
    assert ("/api/service/login", "PATCH", "service_login_update") in declared
    for row in ("service_login_status", "service_login_update"):
        handler = getattr(web_app, row)
        assert handler is service_login_status or handler is service_login_update
        assert handler.__module__ == "ciao.web.routes_service_login"


# --------------------------------------------------------------------------- #
# The body
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"enabled": True, "workspace": "/elsewhere"},
        {"enabled": "true"},
        {"enabled": 1},
        {"enabled": None},
        {"launchd": True},
        [],
        "true",
    ],
    ids=[
        "empty-object",
        "extra-key",
        "string",
        "number",
        "null",
        "wrong-key",
        "array",
        "bare-string",
    ],
)
def test_a_body_that_is_not_one_boolean_is_400_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: Any
) -> None:
    recorder = _Recorder()
    with _client(recorder, monkeypatch, tmp_path) as client:
        response = client.patch("/api/service/login", json=body, headers=_signed(client))

    assert response.status_code == 400
    assert response.json() == {
        "error": 'send exactly {"enabled": true} or {"enabled": false}'
    }
    assert recorder.set_calls == []


@pytest.mark.parametrize("raw", [b"{not json", b"", b"null"])
def test_a_body_that_is_not_json_is_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    recorder = _Recorder()
    with _client(recorder, monkeypatch, tmp_path) as client:
        response = client.patch(
            "/api/service/login",
            content=raw,
            headers={**_signed(client), "Content-Type": "application/json"},
        )

    assert response.status_code == 400
    assert recorder.set_calls == []


@pytest.mark.parametrize("enabled", [True, False])
def test_a_valid_boolean_is_passed_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    recorder = _Recorder()
    # A real change answers with the state it re-read, which is the one asked
    # for; the route serves that body unchanged.
    recorder.set_result = service_login.LoginStatus(
        platform="macos",
        supported=True,
        installed=True,
        enabled=enabled,
        can_change=True,
        reason="The engine service will start when this user signs in."
        if enabled
        else "The engine service will not start when this user signs in.",
        setup_command=None,
    )
    with _client(recorder, monkeypatch, tmp_path) as client:
        response = client.patch(
            "/api/service/login", json={"enabled": enabled}, headers=_signed(client)
        )

    assert response.status_code == 200
    assert recorder.set_calls == [(tmp_path / "workspace", enabled)]
    assert response.json()["enabled"] is enabled
    assert response.json()["can_change"] is True


# --------------------------------------------------------------------------- #
# The three answers
# --------------------------------------------------------------------------- #


def test_get_answers_200_for_a_state_nobody_could_prove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    recorder.status_result = service_login.LoginStatus(
        platform="windows",
        supported=True,
        installed=None,
        enabled=None,
        can_change=False,
        reason="Task Scheduler did not return the engine task's definition.",
        setup_command='schtasks.exe /Query /TN "\\Ciaobot\\Engine" /XML',
    )
    with _client(recorder, monkeypatch, tmp_path) as client:
        response = client.get("/api/service/login", headers=_signed(client))

    assert response.status_code == 200
    assert response.json() == {
        "platform": "windows",
        "supported": True,
        "installed": None,
        "enabled": None,
        "can_change": False,
        "reason": "Task Scheduler did not return the engine task's definition.",
        "setup_command": 'schtasks.exe /Query /TN "\\Ciaobot\\Engine" /XML',
    }


def test_get_answers_200_when_the_platform_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    recorder.status_result = service_login.LoginStatus(
        platform="linux",
        supported=False,
        installed=None,
        enabled=None,
        can_change=False,
        reason="Ciaobot does not read or change an engine's start-at-sign-in state on Linux.",
        setup_command=None,
    )
    with _client(recorder, monkeypatch, tmp_path) as client:
        response = client.get("/api/service/login", headers=_signed(client))

    assert response.status_code == 200
    assert response.json()["supported"] is False
    assert response.json()["can_change"] is False


def test_a_refusal_is_409_with_the_fresh_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    recorder.set_result = service_login.LoginRefused(
        "The Ciaobot engine service is not installed for this user, so there is "
        "nothing to start at sign-in yet.",
        service_login.LoginStatus(
            platform="macos",
            supported=True,
            installed=False,
            enabled=None,
            can_change=False,
            reason="The Ciaobot engine service is not installed for this user, "
            "so there is nothing to start at sign-in yet.",
            setup_command="ciao service start --workspace /tmp/ciao",
        ),
    )
    with _client(recorder, monkeypatch, tmp_path) as client:
        response = client.patch(
            "/api/service/login", json={"enabled": True}, headers=_signed(client)
        )

    assert response.status_code == 409
    payload = response.json()
    assert "not installed" in payload["error"]
    assert payload["installed"] is False
    assert payload["can_change"] is False
    assert payload["setup_command"] == "ciao service start --workspace /tmp/ciao"


def test_an_os_failure_is_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    recorder.set_result = service_login.LoginUnavailable(
        "launchctl did not change the engine's sign-in state: Operation not permitted",
        service_login.LoginStatus(
            platform="macos",
            supported=True,
            installed=True,
            enabled=False,
            can_change=True,
            reason="The engine service will not start when this user signs in.",
            setup_command=None,
        ),
    )
    with _client(recorder, monkeypatch, tmp_path) as client:
        response = client.patch(
            "/api/service/login", json={"enabled": True}, headers=_signed(client)
        )

    assert response.status_code == 503
    payload = response.json()
    assert "Operation not permitted" in payload["error"]
    # The last verified position, not the one that was asked for.
    assert payload["enabled"] is False


def test_a_verification_failure_is_503_and_never_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    recorder.set_result = service_login.LoginUnavailable(
        "Task Scheduler reported the change, but the re-read sign-in state is False.",
        service_login.LoginStatus(
            platform="windows",
            supported=True,
            installed=True,
            enabled=False,
            can_change=True,
            reason="The engine service will not start when this user signs in.",
            setup_command=None,
        ),
    )
    with _client(recorder, monkeypatch, tmp_path) as client:
        response = client.patch(
            "/api/service/login", json={"enabled": True}, headers=_signed(client)
        )

    assert response.status_code == 503
    assert response.json()["enabled"] is False


def test_a_workspace_root_that_is_not_configured_is_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An engine with no workspace has no service to speak for."""
    recorder = _Recorder()
    client = _client(recorder, monkeypatch, Path("/tmp"))
    client.app.state.config = SimpleNamespace(workspace_root=None)
    with client:
        status = client.get("/api/service/login", headers=_signed(client))
        update = client.patch(
            "/api/service/login", json={"enabled": True}, headers=_signed(client)
        )

    assert status.status_code == 500
    assert update.status_code == 500
    assert recorder.status_calls == []
    assert recorder.set_calls == []


def test_neither_route_reaches_the_operating_system_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both handlers stop at the service layer: the seams below it are dead
    code for the length of a request."""

    def _unavailable(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("the route reached the operating system")

    for name in ("_launchctl_run", "_query_task_xml", "_set_task_enabled", "_uid"):
        monkeypatch.setattr(service_login, name, _unavailable)

    recorder = _Recorder()
    with _client(recorder, monkeypatch, tmp_path) as client:
        headers = _signed(client)
        assert client.get("/api/service/login", headers=headers).status_code == 200
        response = client.patch(
            "/api/service/login", json={"enabled": False}, headers=headers
        )

    assert response.status_code == 200
