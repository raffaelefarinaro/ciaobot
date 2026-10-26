"""The installer handoff: one bounded result, re-verified before activation."""

from __future__ import annotations

import json
import os
import plistlib
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import cli, installer_handoff, macos_service, service_backend, windows_service
from ciao.config import CiaoConfig
from ciao.os_support.private import is_private
from ciao.web.auth import AuthMiddleware
from ciao.web.routes_api import setup_finish_endpoint


SUPPORTED = pytest.mark.skipif(
    sys.platform not in ("darwin", "win32"),
    reason="the installer bootstrap supports macOS and Windows only",
)


def _definition_name() -> str:
    if sys.platform == "win32":
        return windows_service.TASK_FILE_NAME
    return f"{macos_service.SERVER_LABEL}.plist"


def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


class _Backend:
    def __init__(self, name: str, live: Path) -> None:
        self.name = name
        self.live = live
        self.loaded: list[Path] = []
        self.status = 0

    def live_agents_dir(self) -> Path:
        return self.live

    def load_agent(self, definition: Path) -> int:
        self.loaded.append(definition)
        return self.status


def _plist_definition(live: Path, workspace: Path) -> Path:
    live.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        definition = live / windows_service.TASK_FILE_NAME
        definition.write_text(
            windows_service.render_task_xml(python=_windows_python(), workspace=str(workspace), user="DESKTOP-1\\me"),
            encoding="utf-16",
        )
        return definition
    definition = live / f"{macos_service.SERVER_LABEL}.plist"
    definition.write_bytes(
        plistlib.dumps({"EnvironmentVariables": {"CIAO_WORKSPACE": str(workspace)}})
    )
    return definition


def _windows_python() -> str:
    return str(Path(sys.executable))


def _recordable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, name: str | None = None
) -> tuple[_Backend, Path, Path, str]:
    _home(monkeypatch, tmp_path)
    backend = _Backend(name or service_backend.current_backend().name, tmp_path / "live")
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    definition = _plist_definition(backend.live, workspace)
    return backend, workspace, definition, str(uuid.uuid4())


def _record(backend: _Backend, workspace: Path, definition: Path, run_id: str) -> Path:
    return installer_handoff.record_setup_completion(
        run_id=run_id,
        startup_id="startup-1",
        workspace=workspace,
        written=[definition],
        port=8443,
    )


@SUPPORTED
def test_setup_completion_records_a_private_bounded_result(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)

    assert path == installer_handoff.result_path()
    assert is_private(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "schema": installer_handoff.SCHEMA_VERSION,
        "run_id": run_id,
        "startup_id": "startup-1",
        "workspace": str(workspace.resolve()),
        "service_backend": backend.name,
        "definition_path": str(definition.resolve()),
        "port": 8443,
    }


@SUPPORTED
def test_setup_completion_refuses_without_its_definition(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    with pytest.raises(installer_handoff.HandoffError, match="service definition"):
        installer_handoff.record_setup_completion(
            run_id=run_id,
            startup_id="startup-1",
            workspace=workspace,
            written=[],
            port=8443,
        )
    definition.unlink()
    with pytest.raises(installer_handoff.HandoffError, match="missing"):
        _record(backend, workspace, definition, run_id)


@SUPPORTED
@pytest.mark.parametrize(
    ("run_id", "startup_id", "port"),
    [
        ("not-a-uuid", "startup-1", 8443),
        ("7d4c9f1e-2b6a-4c3e-9a51-0f8e6b2d4c17", "", 8443),
        ("7d4c9f1e-2b6a-4c3e-9a51-0f8e6b2d4c17", "startup-1", 0),
        ("7d4c9f1e-2b6a-4c3e-9a51-0f8e6b2d4c17", "startup-1", 70000),
    ],
)
def test_setup_completion_refuses_unusable_fields(
    monkeypatch, tmp_path, run_id, startup_id, port
) -> None:
    backend, workspace, definition, _ = _recordable(monkeypatch, tmp_path)
    with pytest.raises(installer_handoff.HandoffError):
        installer_handoff.record_setup_completion(
            run_id=run_id,
            startup_id=startup_id,
            workspace=workspace,
            written=[definition],
            port=port,
        )


def _activate(monkeypatch, backend: _Backend, *, is_set_up=lambda _ws: True):
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)
    return installer_handoff.activate(
        run_id=json.loads(installer_handoff.result_path().read_text())["run_id"],
        is_set_up=is_set_up,
    )


@SUPPORTED
def test_activation_verifies_and_loads_the_live_definition_once(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)

    result = _activate(monkeypatch, backend)

    assert result.workspace == workspace.resolve()
    assert backend.loaded == [backend.live / _definition_name()]


@SUPPORTED
def test_missing_or_stale_result_never_activates(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)
    with pytest.raises(installer_handoff.HandoffError):
        installer_handoff.activate(run_id=run_id, is_set_up=lambda _ws: True)
    _record(backend, workspace, definition, run_id)
    with pytest.raises(installer_handoff.HandoffError, match="different installer run"):
        installer_handoff.activate(run_id=str(uuid.uuid4()), is_set_up=lambda _ws: True)
    assert backend.loaded == []


@SUPPORTED
@pytest.mark.parametrize(
    "text",
    [
        "not json",
        json.dumps([]),
        json.dumps({"schema": 1}),
        json.dumps({"schema": 99}),
        json.dumps({"schema": True}),
    ],
)
def test_malformed_result_never_activates(monkeypatch, tmp_path, text) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)
    with pytest.raises(installer_handoff.HandoffError):
        installer_handoff.activate(run_id=run_id, is_set_up=lambda _ws: True)
    assert backend.loaded == []


@SUPPORTED
def test_oversized_result_never_activates(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)
    path.write_text("x" * (installer_handoff.MAX_RESULT_BYTES + 1), encoding="utf-8")
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)
    with pytest.raises(installer_handoff.HandoffError, match="too large"):
        installer_handoff.activate(run_id=run_id, is_set_up=lambda _ws: True)


@SUPPORTED
def test_result_for_another_platform_never_activates(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)
    other = _Backend("windows" if backend.name == "macos" else "macos", backend.live)
    monkeypatch.setattr(service_backend, "current_backend", lambda: other)
    with pytest.raises(installer_handoff.HandoffError, match="was written for"):
        installer_handoff.activate(run_id=run_id, is_set_up=lambda _ws: True)
    assert other.loaded == []


@SUPPORTED
def test_definition_outside_the_live_directory_never_activates(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)
    assert backend.name in ("macos", "windows")
    backend.live = tmp_path / "elsewhere"
    _plist_definition(backend.live, workspace)
    with pytest.raises(installer_handoff.HandoffError, match="not the live one"):
        _activate(monkeypatch, backend)


@SUPPORTED
def test_unfinished_workspace_never_activates(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)
    with pytest.raises(installer_handoff.HandoffError, match="not a completed"):
        _activate(monkeypatch, backend, is_set_up=lambda _ws: False)


@SUPPORTED
def test_definition_serving_another_workspace_never_activates(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)
    _plist_definition(backend.live, tmp_path / "other")
    with pytest.raises(installer_handoff.HandoffError, match="serves"):
        _activate(monkeypatch, backend)


@SUPPORTED
def test_service_refusal_is_reported(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)
    backend.status = 1
    with pytest.raises(installer_handoff.HandoffError, match="status 1"):
        _activate(monkeypatch, backend)


def test_installer_activate_command_reports_success(monkeypatch, capsys) -> None:
    run_id = str(uuid.uuid4())
    result = installer_handoff.HandoffResult(
        run_id=run_id,
        startup_id="startup-1",
        workspace=Path("/tmp/ws"),
        service_backend="macos",
        definition_path=Path("/tmp/live/com.ciao.server.plist"),
        port=8443,
    )
    monkeypatch.setattr(installer_handoff, "activate", lambda **_kwargs: result)

    assert cli.main(["installer", "activate", "--run-id", run_id, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["workspace"] == str(Path("/tmp/ws"))


def test_installer_activate_command_reports_refusal(monkeypatch, capsys) -> None:
    def refuse(**_kwargs):
        raise installer_handoff.HandoffError("not fresh")

    monkeypatch.setattr(installer_handoff, "activate", refuse)

    assert cli.main(["installer", "activate", "--run-id", str(uuid.uuid4())]) == 1
    assert "not fresh" in capsys.readouterr().err


def _finish_client(tmp_path: Path, *, run_id: str):
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    restarts: list[int] = []
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.serializer = serializer
    app.state.installer_bootstrap = True
    app.state.installer_run_id = run_id
    app.state.startup_tracker = SimpleNamespace(startup_id="startup-1")
    app.state.request_restart = restarts.append
    client = TestClient(app, base_url="http://localhost:8443", client=("127.0.0.1", 50000))
    return client, restarts


def _fake_engine_python(tmp_path: Path) -> str:
    if sys.platform != "win32":
        return "/opt/ciao/bin/python"
    bin_dir = tmp_path / "fake-python"
    bin_dir.mkdir(exist_ok=True)
    for name in ("python.exe", "pythonw.exe"):
        (bin_dir / name).write_bytes(b"")
    return str(bin_dir / "python.exe")


@SUPPORTED
def test_installer_finish_records_the_result_and_exits_zero(
    monkeypatch, tmp_path
) -> None:
    run_id = str(uuid.uuid4())
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "ciao.web.routes_api._schedule_launchd_server_handoff",
        lambda: pytest.fail("the installer child must not hand off to a supervisor"),
    )
    client, restarts = _finish_client(tmp_path, run_id=run_id)
    workspace = tmp_path / "notes"

    resp = client.post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(workspace),
            "launch_agents_dir": str(tmp_path / "LaunchAgents"),
            "app_dir": str(tmp_path / "Applications"),
            "python": _fake_engine_python(tmp_path),
            "port": 9443,
        },
    )

    assert resp.status_code == 200, resp.text
    assert restarts == [0]
    payload = json.loads((installer_handoff.result_path()).read_text(encoding="utf-8"))
    assert payload["run_id"] == run_id
    assert payload["workspace"] == str(workspace.resolve())
    assert payload["port"] == 9443
    assert Path(payload["definition_path"]).name == _definition_name()


@SUPPORTED
def test_installer_finish_refuses_to_stay_running(monkeypatch, tmp_path) -> None:
    run_id = str(uuid.uuid4())
    _home(monkeypatch, tmp_path)

    def explode(*_args, **_kwargs):
        raise AssertionError("a refused finish must not provision anything")

    monkeypatch.setattr("ciao.cli.setup_workspace", explode)
    client, restarts = _finish_client(tmp_path, run_id=run_id)

    resp = client.post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(tmp_path / "notes"),
            "restart": False,
        },
    )

    assert resp.status_code == 400
    assert restarts == []
    assert not installer_handoff.result_path().exists()


@SUPPORTED
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_world_readable_result_never_activates(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)
    path.chmod(0o644)
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)
    with pytest.raises(installer_handoff.HandoffError, match="owner-only"):
        installer_handoff.activate(run_id=run_id, is_set_up=lambda _ws: True)
    assert backend.loaded == []


@SUPPORTED
def test_symlinked_scratch_directory_never_activates(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)
    real = tmp_path / "real-scratch"
    path.parent.rename(real)
    path.parent.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)
    with pytest.raises(installer_handoff.HandoffError, match="symlink"):
        installer_handoff.activate(run_id=run_id, is_set_up=lambda _ws: True)
    assert backend.loaded == []


@SUPPORTED
def test_duplicate_fields_never_activate(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace('"port": 8443', '"port": 9443,\n  "port": 8443'),
        encoding="utf-8",
    )
    path.chmod(0o600)
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)
    with pytest.raises(installer_handoff.HandoffError, match="repeats the field"):
        installer_handoff.activate(run_id=run_id, is_set_up=lambda _ws: True)
    assert backend.loaded == []


@SUPPORTED
@pytest.mark.skipif(sys.platform == "win32", reason="the plist is the POSIX definition")
def test_relative_definition_workspace_never_activates(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)
    definition.write_bytes(
        plistlib.dumps({
            "EnvironmentVariables": {
                "CIAO_WORKSPACE": os.path.relpath(workspace, Path.cwd())
            }
        })
    )
    with pytest.raises(installer_handoff.HandoffError, match="relative workspace"):
        _activate(monkeypatch, backend)


@SUPPORTED
def test_service_manager_oserror_becomes_a_handoff_error(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)

    def fail(_definition):
        raise OSError("launchctl disappeared")

    backend.load_agent = fail  # type: ignore[method-assign]
    with pytest.raises(installer_handoff.HandoffError, match="launchctl disappeared"):
        _activate(monkeypatch, backend)


@SUPPORTED
def test_unbounded_startup_id_is_refused(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    with pytest.raises(installer_handoff.HandoffError, match="size limit"):
        installer_handoff.record_setup_completion(
            run_id=run_id,
            startup_id="x" * installer_handoff.MAX_RESULT_BYTES,
            workspace=workspace,
            written=[definition],
            port=8443,
        )


@SUPPORTED
def test_a_predictable_temporary_path_cannot_redirect_the_write(
    monkeypatch, tmp_path
) -> None:
    """A hardlinked ``handoff.json.tmp`` must not be written through."""
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("untouched", encoding="utf-8")
    scratch = installer_handoff.scratch_root()
    scratch.mkdir(parents=True, exist_ok=True)
    os.link(sentinel, scratch / f"{installer_handoff.FILENAME}.tmp")
    _record(backend, workspace, definition, run_id)
    assert sentinel.read_text(encoding="utf-8") == "untouched"


@SUPPORTED
def test_windows_activation_requires_a_private_result(monkeypatch, tmp_path) -> None:
    """The DACL gate is asked on every platform, not just where mode bits exist."""
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)
    monkeypatch.setattr(installer_handoff, "is_private", lambda _path: False)
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)
    with pytest.raises(installer_handoff.HandoffError, match="owner-only"):
        installer_handoff.activate(run_id=run_id, is_set_up=lambda _ws: True)
    assert backend.loaded == []


@SUPPORTED
@pytest.mark.parametrize("failure", [OSError("no home"), RuntimeError("no dir"), ValueError("bad")])
def test_live_directory_failure_is_reported(monkeypatch, tmp_path, failure) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)

    def explode():
        raise failure

    backend.live_agents_dir = explode  # type: ignore[method-assign]
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)
    with pytest.raises(installer_handoff.HandoffError, match="live service definitions"):
        installer_handoff.activate(run_id=run_id, is_set_up=lambda _ws: True)


@SUPPORTED
def test_service_manager_value_error_is_reported(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)

    def fail(_definition):
        raise ValueError("invalid plist")

    backend.load_agent = fail  # type: ignore[method-assign]
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)
    with pytest.raises(installer_handoff.HandoffError, match="invalid plist"):
        installer_handoff.activate(run_id=run_id, is_set_up=lambda _ws: True)


@SUPPORTED
def test_unrelated_definition_errors_are_not_swallowed(monkeypatch, tmp_path) -> None:
    """A programming error in the task parser surfaces as itself."""
    definition = tmp_path / "task.xml"
    definition.write_text("<Task/>", encoding="utf-8")

    def explode(_definition):
        raise TypeError("bug")

    monkeypatch.setattr(windows_service, "task_workspace", explode)
    with pytest.raises(TypeError, match="bug"):
        installer_handoff._definition_workspace("windows", definition)
