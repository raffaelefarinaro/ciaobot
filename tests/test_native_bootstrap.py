"""The native bootstrap child context and its private bind record (#2340).

Every test keeps HOME in a private tmp directory, injects the host verifier and
the macOS service backend, and never runs launchctl, a service, the network or
git. The real ``setup_workspace`` template and the real schema-2 recorder are
exercised once, against that isolated home.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import json
import os
import plistlib
import select
import socket
import stat
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import cli, main, native_handoff, native_installer, server_host
from ciao.config import CiaoConfig
from ciao.native_installer import (
    MAX_BIND_NOTIFICATION_BYTES,
    NativeBootstrapContext,
    NativeBootstrapError,
    notify_native_bind,
)
from ciao.port_in_use import BIND_PHASE
from ciao.web.app import create_app
from ciao.web.auth import AuthMiddleware
from ciao.web.routes_api import setup_finish_endpoint

_LOOPBACK_PEER = ("127.0.0.1", 50000)


class _FakeMacBackend:
    name = "macos"

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def agents_dir(self) -> Path:
        return self._directory

    def live_agents_dir(self) -> Path:
        return self._directory

    def is_live_agents_dir(self, path: Path) -> bool:
        return Path(path).expanduser().resolve() == self._directory.resolve()

    def bootout_agent(self, label: str) -> None:
        raise AssertionError("the native setup path must never stop a service")

    def load_agent(self, definition: Path) -> int:
        raise AssertionError("the native setup path must never start a service")

    def schedule_server_handoff(self) -> bool:
        raise AssertionError("the native setup path must never hand off to a supervisor")


def _host_ownership() -> server_host.HostOwnership:
    executable = server_host.default_bundle_path() / "Contents" / "MacOS" / server_host.EXECUTABLE_NAME
    return server_host.HostOwnership(
        schema=server_host.SCHEMA_VERSION,
        bundle_path=str(server_host.default_bundle_path()),
        bundle_id=server_host.BUNDLE_ID,
        host_revision=server_host.HOST_REVISION,
        host_protocol=server_host.HOST_PROTOCOL,
        executable_sha256="a" * 64,
        per_arch_cdhashes={"arm64": "b" * 40},
        bundle_files={executable.relative_to(server_host.default_bundle_path()).as_posix(): "c" * 64},
    )


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    os.chmod(home, 0o700)
    monkeypatch.setenv("HOME", str(home))
    # The suite's conftest redirects the live LaunchAgents dir to a tmp path.
    # The installer bootstrap clears that redirect before this child exists, so
    # the native tests run with it absent (their fake backend names the tmp dir).
    monkeypatch.delenv("CIAO_LAUNCH_AGENTS_DIR", raising=False)
    monkeypatch.delenv("CIAO_ENGINE_PATH", raising=False)
    scratch = home / ".ciao" / "installer-bootstrap"
    (scratch / "runtime").mkdir(parents=True)
    os.chmod(home / ".ciao", 0o700)
    os.chmod(scratch, 0o700)
    os.chmod(scratch / "runtime", 0o700)
    fake_host = server_host.default_bundle_path() / "Contents" / "MacOS" / server_host.EXECUTABLE_NAME
    fake_host.parent.mkdir(parents=True)
    fake_host.write_bytes(b"fake host")
    ownership = _host_ownership()
    monkeypatch.setattr(native_installer, "_verify_owned_host", lambda *a, **k: ownership)
    monkeypatch.setattr(native_installer, "_server_job_absent", lambda: True)
    monkeypatch.setattr(cli, "verify_owned_host", lambda *a, **k: ownership)
    monkeypatch.setattr(server_host, "verify_owned_host", lambda *a, **k: ownership)
    return home


@pytest.fixture()
def notification_pipe() -> tuple[int, int]:
    read_fd, write_fd = os.pipe()
    yield read_fd, write_fd
    for fd in (read_fd, write_fd):
        try:
            os.close(fd)
        except OSError:
            pass


def _scratch(home: Path) -> Path:
    return home / ".ciao" / "installer-bootstrap"


def _config(home: Path, *, port: int = 8443) -> CiaoConfig:
    scratch = _scratch(home)
    return CiaoConfig(
        pwa_auth_token="t",
        workspace_root=scratch,
        state_path=scratch / "runtime" / "state.json",
        media_root=scratch / "runtime" / "media",
        vault_root=scratch / "memory-vault",
        bootstrap_mode=True,
        pwa_host="127.0.0.1",
        pwa_port=port,
    )


def _capture(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    *,
    fd: int,
    run_id: str | None = None,
    startup_id: str = "startup-1",
    expected_workspace: Path | None = None,
    port: int = 8443,
    bootstrap_host: str = "127.0.0.1",
) -> NativeBootstrapContext:
    monkeypatch.setattr(native_installer, "_bootstrap_platform", lambda: "darwin")
    launch = home / "LaunchAgents"
    launch.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "ciao.service_backend.current_backend", lambda: _FakeMacBackend(launch)
    )
    scratch = _scratch(home)
    return NativeBootstrapContext.capture(
        run_id=run_id or str(uuid.uuid4()),
        startup_id=startup_id,
        notification_fd=fd,
        config_root=scratch,
        runtime_root=scratch / "runtime",
        bootstrap_host=bootstrap_host,
        bootstrap_port=port,
        expected_workspace=expected_workspace,
        engine_python=Path(sys.executable),
    )


def _client(
    home: Path,
    context: NativeBootstrapContext,
    *,
    restarts: list[int],
    installer_bootstrap: bool = True,
) -> TestClient:
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = _config(home)
    app.state.installer_bootstrap = installer_bootstrap
    app.state.installer_run_id = context.run_id
    app.state.native_bootstrap = context
    app.state.serializer = serializer
    app.state.request_restart = restarts.append
    app.state.startup_tracker = SimpleNamespace(startup_id=context.startup_id)
    return TestClient(app, base_url="http://localhost:8443", client=_LOOPBACK_PEER)


def _post(client: TestClient, workspace: Path, **overrides: object):
    body: dict[str, object] = {
        "password": "wizard-pass",
        "workspace": str(workspace),
        "workspace_name": "personal",
        "port": 8443,
    }
    body.update(overrides)
    return client.post("/api/setup/finish", json=body)


# ── the private bind record ─────────────────────────────────────────────────


def test_a_bind_notification_is_one_bounded_record_and_closes_the_fd() -> None:
    read_fd, write_fd = os.pipe()
    try:
        notify_native_bind(write_fd, 54321)
        with pytest.raises(OSError):
            os.fstat(write_fd)
        record = os.read(read_fd, MAX_BIND_NOTIFICATION_BYTES + 1)
        assert record == b'{"port":54321,"schema":1}\n'
        assert len(record) <= MAX_BIND_NOTIFICATION_BYTES
        assert os.read(read_fd, 1) == b""
    finally:
        os.close(read_fd)


def test_a_second_bind_notification_is_refused(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, notification_pipe: tuple[int, int]
) -> None:
    read_fd, write_fd = notification_pipe
    context = _capture(monkeypatch, isolated_home, fd=write_fd)
    context.notify_bound(12345)
    assert os.read(read_fd, MAX_BIND_NOTIFICATION_BYTES) == b'{"port":12345,"schema":1}\n'
    with pytest.raises(NativeBootstrapError, match="already_notified"):
        context.notify_bound(12345)


def test_a_closed_reader_is_a_fixed_bind_failure(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    context = _capture(monkeypatch, isolated_home, fd=write_fd)
    with pytest.raises(NativeBootstrapError, match="bind_notification_failed"):
        context.notify_bound(8443)
    with pytest.raises(OSError):
        os.fstat(write_fd)


def test_a_real_child_process_writes_the_record_and_is_reaped() -> None:
    read_fd, write_fd = os.pipe()
    script = (
        "import sys\n"
        "from ciao.native_installer import notify_native_bind\n"
        "notify_native_bind(int(sys.argv[1]), int(sys.argv[2]))\n"
    )
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(write_fd), "45678"],
            pass_fds=(write_fd,),
            close_fds=True,
        )
        os.close(write_fd)
        ready, _, _ = select.select([read_fd], [], [], 10)
        assert ready, "the child did not write the bind record"
        record = os.read(read_fd, MAX_BIND_NOTIFICATION_BYTES + 1)
        assert os.read(read_fd, 1) == b""
        assert process.wait(timeout=10) == 0
        assert record == b'{"port":45678,"schema":1}\n'
    finally:
        for fd in (read_fd, write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def test_the_record_is_written_only_after_server_started_and_bind_phase_done() -> None:
    tracker = main.StartupTracker(startup_id="startup-1")
    order: list[str] = []
    tracker.on_finish = lambda phase: order.append(phase.name)
    server = SimpleNamespace(started=False, should_exit=False)

    async def run() -> None:
        task = asyncio.create_task(
            main._mark_bound_when_serving(
                server,
                tracker,
                poll_interval=0.001,
                on_bound=lambda: order.append("notified"),
            )
        )
        await asyncio.sleep(0.01)
        assert order == []
        assert tracker.phase(BIND_PHASE).status == "pending"
        server.started = True
        await asyncio.wait_for(task, timeout=1)
    asyncio.run(run())

    assert order == [BIND_PHASE, "notified"]
    assert tracker.phase(BIND_PHASE).status == "done"


def test_the_setup_wizard_is_not_opened_in_native_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(main, "_open_browser_when_ready", opened.append)
    main._open_wizard_browser("http://127.0.0.1:8443", None)
    assert opened == ["http://127.0.0.1:8443"]
    main._open_wizard_browser("http://127.0.0.1:8443", object())
    assert opened == ["http://127.0.0.1:8443"]


# ── capture and the pre-write rechecks ──────────────────────────────────────


def test_capture_binds_the_parent_snapshots(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, notification_pipe: tuple[int, int]
) -> None:
    _read_fd, write_fd = notification_pipe
    run_id = str(uuid.uuid4())
    context = _capture(monkeypatch, isolated_home, fd=write_fd, run_id=run_id)
    assert context.run_id == run_id
    assert context.startup_id == "startup-1"
    assert context.definition_path == (
        isolated_home / "LaunchAgents" / "com.ciao.server.plist"
    )
    assert context.handoff_path == _scratch(isolated_home) / "handoff.json"
    assert context.expected_host.bundle_id == server_host.BUNDLE_ID
    # The child marks its private descriptor non-inheritable immediately, so a
    # provider or uvicorn descendant started before the bind cannot carry it.
    assert os.get_inheritable(write_fd) is False


def test_capture_refuses_a_non_macos_backend(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, notification_pipe: tuple[int, int]
) -> None:
    _read_fd, write_fd = notification_pipe
    monkeypatch.setattr(native_installer, "_bootstrap_platform", lambda: "darwin")

    class _Windows:
        name = "windows"

        def live_agents_dir(self) -> Path:
            return isolated_home

    monkeypatch.setattr(
        "ciao.service_backend.current_backend", lambda: _Windows()
    )
    with pytest.raises(NativeBootstrapError, match="unsupported_backend"):
        NativeBootstrapContext.capture(
            run_id=str(uuid.uuid4()),
            startup_id="startup-1",
            notification_fd=write_fd,
            config_root=_scratch(isolated_home),
            runtime_root=_scratch(isolated_home) / "runtime",
            bootstrap_host="127.0.0.1",
            bootstrap_port=8443,
            expected_workspace=None,
            engine_python=Path(sys.executable),
        )


def test_capture_refuses_an_existing_definition_or_handoff(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, notification_pipe: tuple[int, int]
) -> None:
    _read_fd, write_fd = notification_pipe
    launch = isolated_home / "LaunchAgents"
    launch.mkdir()
    monkeypatch.setattr(
        "ciao.service_backend.current_backend", lambda: _FakeMacBackend(launch)
    )
    monkeypatch.setattr(native_installer, "_bootstrap_platform", lambda: "darwin")
    (launch / "com.ciao.server.plist").write_text("present", encoding="utf-8")
    with pytest.raises(NativeBootstrapError, match="definition_changed"):
        NativeBootstrapContext.capture(
            run_id=str(uuid.uuid4()),
            startup_id="startup-1",
            notification_fd=write_fd,
            config_root=_scratch(isolated_home),
            runtime_root=_scratch(isolated_home) / "runtime",
            bootstrap_host="127.0.0.1",
            bootstrap_port=8443,
            expected_workspace=None,
            engine_python=Path(sys.executable),
        )
    (launch / "com.ciao.server.plist").unlink()
    (_scratch(isolated_home) / "handoff.json").write_text("{}", encoding="utf-8")
    with pytest.raises(NativeBootstrapError, match="handoff_changed"):
        NativeBootstrapContext.capture(
            run_id=str(uuid.uuid4()),
            startup_id="startup-1",
            notification_fd=write_fd,
            config_root=_scratch(isolated_home),
            runtime_root=_scratch(isolated_home) / "runtime",
            bootstrap_host="127.0.0.1",
            bootstrap_port=8443,
            expected_workspace=None,
            engine_python=Path(sys.executable),
        )


def test_capture_refuses_a_registered_service(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, notification_pipe: tuple[int, int]
) -> None:
    _read_fd, write_fd = notification_pipe
    monkeypatch.setattr(native_installer, "_server_job_absent", lambda: False)
    with pytest.raises(NativeBootstrapError, match="service_changed"):
        _capture(monkeypatch, isolated_home, fd=write_fd)


def test_capture_refuses_a_non_loopback_bind(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, notification_pipe: tuple[int, int]
) -> None:
    _read_fd, write_fd = notification_pipe
    with pytest.raises(NativeBootstrapError, match="invalid_context"):
        _capture(monkeypatch, isolated_home, fd=write_fd, bootstrap_host="0.0.0.0")


def test_capture_refuses_a_non_pipe_notification_descriptor(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fd = os.open(os.devnull, os.O_WRONLY)
    try:
        with pytest.raises(NativeBootstrapError, match="invalid_context"):
            _capture(monkeypatch, isolated_home, fd=fd)
    finally:
        os.close(fd)


def _ready_context(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, fd: int | None = None
) -> NativeBootstrapContext:
    launch = isolated_home / "LaunchAgents"
    launch.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "ciao.service_backend.current_backend", lambda: _FakeMacBackend(launch)
    )
    read_fd, write_fd = os.pipe()
    try:
        return _capture(
            monkeypatch, isolated_home, fd=write_fd if fd is None else fd
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_ready_to_write_refuses_a_definition_created_during_the_wizard(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _ready_context(isolated_home, monkeypatch)
    context.definition_path.parent.mkdir(exist_ok=True)
    context.definition_path.write_text("appeared", encoding="utf-8")
    with pytest.raises(NativeBootstrapError, match="definition_changed"):
        context.assert_ready_to_write(
            startup_id=context.startup_id,
            workspace=isolated_home / "workspace",
            port=8443,
            engine_python=sys.executable,
            config_host="127.0.0.1",
            config_port=8443,
        )


def test_ready_to_write_refuses_a_handoff_created_during_the_wizard(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _ready_context(isolated_home, monkeypatch)
    context.handoff_path.write_text("{}", encoding="utf-8")
    with pytest.raises(NativeBootstrapError, match="handoff_changed"):
        context.assert_ready_to_write(
            startup_id=context.startup_id,
            workspace=isolated_home / "workspace",
            port=8443,
            engine_python=sys.executable,
            config_host="127.0.0.1",
            config_port=8443,
        )


def test_ready_to_write_refuses_a_changed_host(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _ready_context(isolated_home, monkeypatch)
    monkeypatch.setattr(
        native_installer,
        "_verify_owned_host",
        lambda *a, **k: (_ for _ in ()).throw(server_host.ServerHostError("gone")),
    )
    with pytest.raises(NativeBootstrapError, match="host_changed"):
        context.assert_ready_to_write(
            startup_id=context.startup_id,
            workspace=isolated_home / "workspace",
            port=8443,
            engine_python=sys.executable,
            config_host="127.0.0.1",
            config_port=8443,
        )


def test_ready_to_write_refuses_a_registered_or_unprovable_service(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _ready_context(isolated_home, monkeypatch)
    monkeypatch.setattr(native_installer, "_server_job_absent", lambda: False)
    with pytest.raises(NativeBootstrapError, match="service_changed"):
        context.assert_ready_to_write(
            startup_id=context.startup_id,
            workspace=isolated_home / "workspace",
            port=8443,
            engine_python=sys.executable,
            config_host="127.0.0.1",
            config_port=8443,
        )

    def unprovable() -> bool:
        raise RuntimeError("launchctl did not answer")

    monkeypatch.setattr(native_installer, "_server_job_absent", unprovable)
    with pytest.raises(NativeBootstrapError, match="service_changed"):
        context.assert_ready_to_write(
            startup_id=context.startup_id,
            workspace=isolated_home / "workspace",
            port=8443,
            engine_python=sys.executable,
            config_host="127.0.0.1",
            config_port=8443,
        )


def test_ready_to_write_refuses_a_late_service_or_engine_override(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _ready_context(isolated_home, monkeypatch)
    workspace = isolated_home / "workspace"
    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(isolated_home / "elsewhere"))
    with pytest.raises(NativeBootstrapError, match="config_changed"):
        context.assert_ready_to_write(
            startup_id=context.startup_id,
            workspace=workspace,
            port=8443,
            engine_python=sys.executable,
            config_host="127.0.0.1",
            config_port=8443,
        )
    monkeypatch.delenv("CIAO_LAUNCH_AGENTS_DIR")
    monkeypatch.setenv("CIAO_ENGINE_PATH", str(isolated_home / "other-python"))
    with pytest.raises(NativeBootstrapError, match="config_changed"):
        context.assert_ready_to_write(
            startup_id=context.startup_id,
            workspace=workspace,
            port=8443,
            engine_python=sys.executable,
            config_host="127.0.0.1",
            config_port=8443,
        )


def test_ready_to_write_refuses_a_changed_runtime_directory(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _ready_context(isolated_home, monkeypatch)
    runtime = _scratch(isolated_home) / "runtime"
    runtime.rename(_scratch(isolated_home) / "runtime-old")
    runtime.mkdir()
    os.chmod(runtime, 0o700)
    with pytest.raises(NativeBootstrapError, match="config_changed"):
        context.assert_ready_to_write(
            startup_id=context.startup_id,
            workspace=isolated_home / "workspace",
            port=8443,
            engine_python=sys.executable,
            config_host="127.0.0.1",
            config_port=8443,
        )


def test_ready_to_write_refuses_an_already_configured_workspace(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _ready_context(isolated_home, monkeypatch)
    workspace = isolated_home / "workspace"
    (workspace / ".runtime").mkdir(parents=True)
    (workspace / ".runtime" / "workspaces.json").write_text("[]", encoding="utf-8")
    with pytest.raises(NativeBootstrapError, match="workspace_already_configured"):
        context.assert_ready_to_write(
            startup_id=context.startup_id,
            workspace=workspace,
            port=8443,
            engine_python=sys.executable,
            config_host="127.0.0.1",
            config_port=8443,
        )


def test_ready_to_write_refuses_a_workspace_other_than_the_fixed_one(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = isolated_home / "fixed"
    context = _ready_context(isolated_home, monkeypatch)
    monkeypatch.setattr(context, "expected_workspace", fixed, raising=False)
    with pytest.raises(NativeBootstrapError, match="workspace_changed"):
        context.assert_ready_to_write(
            startup_id=context.startup_id,
            workspace=isolated_home / "other",
            port=8443,
            engine_python=sys.executable,
            config_host="127.0.0.1",
            config_port=8443,
        )


def test_ready_to_write_refuses_a_changed_startup_or_interpreter(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _ready_context(isolated_home, monkeypatch)
    workspace = isolated_home / "workspace"
    with pytest.raises(NativeBootstrapError, match="startup_changed"):
        context.assert_ready_to_write(
            startup_id="another-startup",
            workspace=workspace,
            port=8443,
            engine_python=sys.executable,
            config_host="127.0.0.1",
            config_port=8443,
        )
    with pytest.raises(NativeBootstrapError, match="engine_changed"):
        context.assert_ready_to_write(
            startup_id=context.startup_id,
            workspace=workspace,
            port=8443,
            engine_python=str(isolated_home / "other-python"),
            config_host="127.0.0.1",
            config_port=8443,
        )


# ── the authenticated setup endpoint ────────────────────────────────────────


def _native_client_with_fakes(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    expected_workspace: Path | None = None,
) -> tuple[TestClient, NativeBootstrapContext, Path, list[int]]:
    # The endpoint writes these for the relaunched process; monkeypatch
    # restores the worker's environment after each test.
    monkeypatch.setenv("CIAO_WORKSPACE", "")
    monkeypatch.setenv("PWA_PORT", "")
    monkeypatch.setenv("PWA_AUTH_TOKEN", "")
    launch = isolated_home / "LaunchAgents"
    launch.mkdir()
    monkeypatch.setattr(
        "ciao.service_backend.current_backend", lambda: _FakeMacBackend(launch)
    )
    monkeypatch.setattr(cli, "_service_platform", lambda: True)
    monkeypatch.setattr(cli, "ensure_workspace_git", lambda *a, **k: None)
    monkeypatch.setattr(cli, "ensure_vault_git", lambda *a, **k: None)
    read_fd, write_fd = os.pipe()
    try:
        context = _capture(
            monkeypatch,
            isolated_home,
            fd=write_fd,
            expected_workspace=expected_workspace,
        )
    finally:
        os.close(write_fd)
        os.close(read_fd)
    restarts: list[int] = []
    client = _client(isolated_home, context, restarts=restarts)
    return client, context, launch, restarts


def test_native_setup_writes_the_real_template_and_schema_2_record(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, context, launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    workspace = isolated_home / "workspace"

    record_calls: list[threading.Thread] = []
    real_record = native_handoff.record_setup_completion

    def recording(**kwargs):
        record_calls.append(threading.current_thread())
        return real_record(**kwargs)

    monkeypatch.setattr(native_handoff, "record_setup_completion", recording)

    resp = _post(client, workspace, port=9443)
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert restarts == [0]

    definition = launch / "com.ciao.server.plist"
    assert definition.is_file()
    assert stat.S_IMODE(definition.stat().st_mode) == 0o600
    resolved_workspace = workspace.resolve()
    parsed = plistlib.loads(definition.read_bytes())
    assert parsed["Label"] == "com.ciao.server"
    assert parsed["ProgramArguments"][0].endswith(server_host.EXECUTABLE_NAME)
    assert parsed["ProgramArguments"][1:] == ["serve", "--python", sys.executable]
    assert parsed["EnvironmentVariables"]["CIAO_WORKSPACE"] == str(resolved_workspace)
    assert parsed["EnvironmentVariables"]["CIAO_PORT"] == "9443"

    result_path = native_handoff.result_path()
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["schema"] == 2
    assert payload["run_id"] == context.run_id
    assert payload["startup_id"] == context.startup_id
    assert payload["workspace"] == str(resolved_workspace)
    assert payload["port"] == 9443
    assert payload["service_backend"] == "macos"
    assert payload["definition_path"] == str(definition.resolve())
    assert payload["definition_sha256"] == native_handoff.read_result(
        expected_run_id=context.run_id
    ).definition_sha256

    # The blocking recorder ran in a worker thread, not on the event loop.
    assert record_calls and record_calls[0] is not threading.main_thread()

    # One onboarding receipt for the destination, and no native receipt in the
    # private scratch directory (the schema-2 handoff is the only new record).
    assert (workspace / ".runtime" / "onboarding.json").is_file()
    assert sorted(p.name for p in workspace.rglob("*native*")) == []
    assert sorted(p.name for p in _scratch(isolated_home).glob("*.json")) == [
        "handoff.json"
    ]


def test_native_setup_refuses_a_race_before_writing_anything(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, context, launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    (launch / "com.ciao.server.plist").write_text("present", encoding="utf-8")
    monkeypatch.setattr(
        cli, "setup_workspace", lambda *a, **k: pytest.fail("setup wrote")
    )
    workspace = isolated_home / "workspace"
    resp = _post(client, workspace)
    assert resp.status_code == 409
    assert resp.json()["error"] == (
        "the installer state changed while setup was open; nothing was written"
    )
    assert not workspace.exists()
    assert not native_handoff.result_path().exists()
    assert (launch / "com.ciao.server.plist").read_text(encoding="utf-8") == "present"
    assert restarts == [1]


def test_native_setup_refuses_body_overrides_before_writing(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, _launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    monkeypatch.setattr(
        cli, "setup_workspace", lambda *a, **k: pytest.fail("setup wrote")
    )
    for field, value, message in (
        ("launch_agents_dir", str(isolated_home / "elsewhere"), "must not override launch_agents_dir"),
        ("app_dir", str(isolated_home / "apps"), "must not override app_dir"),
        ("python", sys.executable, "must not override python"),
        ("unknown_field", "x", "unsupported field"),
    ):
        resp = _post(client, isolated_home / "workspace", **{field: value})
        assert resp.status_code == 400
        assert message in resp.json()["error"]
    assert not (isolated_home / "workspace").exists()
    assert restarts == []


def test_native_setup_refuses_an_explicit_workspace_mismatch(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = isolated_home / "fixed"
    client, _context, _launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch, expected_workspace=fixed
    )
    monkeypatch.setattr(
        cli, "setup_workspace", lambda *a, **k: pytest.fail("setup wrote")
    )
    resp = _post(client, isolated_home / "other")
    assert resp.status_code == 409
    assert not (isolated_home / "other").exists()
    assert restarts == [1]


def test_native_setup_refuses_an_existing_definition_without_overwriting(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    definition = launch / "com.ciao.server.plist"
    definition.write_text("do not replace", encoding="utf-8")
    resp = _post(client, isolated_home / "workspace")
    assert resp.status_code == 409
    assert definition.read_text(encoding="utf-8") == "do not replace"
    assert not (isolated_home / "workspace").exists()
    assert not native_handoff.result_path().exists()
    assert restarts == [1]


def test_native_setup_errors_are_sanitized(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, launch, _restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    (launch / "com.ciao.server.plist").write_text("present", encoding="utf-8")
    resp = _post(client, isolated_home / "workspace", password="super-secret")
    body = resp.text
    assert resp.status_code == 409
    assert "super-secret" not in body
    assert str(isolated_home) not in body
    assert "Traceback" not in body


def test_the_existing_legacy_bootstrap_still_records_schema_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIAO_WORKSPACE", "")
    monkeypatch.setenv("PWA_PORT", "")
    monkeypatch.setenv("PWA_AUTH_TOKEN", "")
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path / "boot",
        state_path=tmp_path / "boot" / "runtime" / "state.json",
        media_root=tmp_path / "boot" / "runtime" / "media",
        bootstrap_mode=True,
    )
    (tmp_path / "boot" / "runtime").mkdir(parents=True)
    serializer = URLSafeTimedSerializer("test-secret")
    restarts: list[int] = []
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = True
    app.state.installer_run_id = str(uuid.uuid4())
    app.state.native_bootstrap = None
    app.state.serializer = serializer
    app.state.request_restart = restarts.append
    app.state.startup_tracker = SimpleNamespace(startup_id="legacy-startup")
    client = TestClient(app, base_url="http://localhost:8443", client=_LOOPBACK_PEER)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "setup_workspace", lambda *a, **k: [tmp_path / "ws" / "x"])
    monkeypatch.setattr(
        "ciao.installer_handoff.record_setup_completion", lambda **kwargs: calls.append(kwargs)
    )
    resp = client.post(
        "/api/setup/finish",
        json={"password": "wizard-pass", "workspace": str(tmp_path / "ws")},
    )
    assert resp.status_code == 200
    assert restarts == [0]
    assert calls and calls[0]["startup_id"] == "legacy-startup"
    assert calls[0]["run_id"] == app.state.installer_run_id


# ── app factory, CLI threading and exclusive definition creation ───────────


def test_create_app_threads_the_native_context(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _ready_context(isolated_home, monkeypatch)
    config = _config(isolated_home)
    app = create_app(
        config,
        installer_bootstrap=True,
        installer_run_id=context.run_id,
        native_bootstrap=context,
    )
    assert app.state.native_bootstrap is context
    plain = create_app(config, installer_bootstrap=True, installer_run_id=context.run_id)
    assert [route.path for route in plain.routes] == [route.path for route in app.routes]
    with pytest.raises(ValueError, match="requires installer bootstrap"):
        create_app(config, native_bootstrap=context)
    with pytest.raises(ValueError, match="does not match"):
        create_app(
            config,
            installer_bootstrap=True,
            installer_run_id=str(uuid.uuid4()),
            native_bootstrap=context,
        )


def test_the_cli_forwards_the_private_native_handover(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(cli, "_run_server", lambda **kwargs: seen.update(kwargs) or 0)
    run_id = str(uuid.uuid4())
    args = cli.build_parser().parse_args(
        [
            "run",
            "--installer-bootstrap",
            "--installer-run-id",
            run_id,
            "--installer-native-fd",
            "7",
            "--installer-native-workspace",
            str(tmp_path / "ws"),
        ]
    )
    assert args.func(args) == 0
    assert seen == {
        "supervised": False,
        "installer_bootstrap": True,
        "installer_run_id": run_id,
        "installer_suggest_workspace": None,
        "installer_native_fd": 7,
        "installer_native_workspace": str(tmp_path / "ws"),
    }


def test_the_private_native_handover_requires_a_bootstrap_and_run_id(
    capsys, tmp_path: Path
) -> None:
    run_id = str(uuid.uuid4())
    args = cli.build_parser().parse_args(
        ["run", "--installer-run-id", run_id, "--installer-native-fd", "7"]
    )
    assert args.func(args) == 2
    assert "requires --installer-bootstrap" in capsys.readouterr().err

    args = cli.build_parser().parse_args(
        ["run", "--installer-bootstrap", "--installer-native-fd", "7"]
    )
    assert args.func(args) == 2
    assert "requires --installer-run-id" in capsys.readouterr().err

    args = cli.build_parser().parse_args(
        ["run", "--installer-bootstrap", "--installer-run-id", run_id, "--installer-native-fd", "1"]
    )
    assert args.func(args) == 2
    assert "non-stdio" in capsys.readouterr().err


_HANDOVER_RUN_ID = str(uuid.uuid4())
_HANDOVER_REFUSALS = [
    pytest.param(
        ["run", "--installer-native-workspace", "/abs/ws"],
        "--installer-native-workspace requires --installer-bootstrap",
        id="workspace-without-bootstrap",
    ),
    pytest.param(
        ["run", "--installer-native-fd", "7"],
        "--installer-native-fd requires --installer-bootstrap",
        id="fd-without-bootstrap",
    ),
    pytest.param(
        ["run", "--installer-native-fd", "7", "--installer-native-workspace", "/abs/ws"],
        "--installer-native-workspace requires --installer-bootstrap",
        id="fd-and-workspace-without-bootstrap",
    ),
    pytest.param(
        [
            "run", "--installer-bootstrap", "--installer-run-id", _HANDOVER_RUN_ID,
            "--installer-native-workspace", "/abs/ws",
        ],
        "--installer-native-workspace requires --installer-native-fd",
        id="workspace-without-fd",
    ),
    pytest.param(
        ["run", "--installer-bootstrap", "--installer-native-fd", "7"],
        "--installer-native-fd requires --installer-run-id",
        id="fd-without-run-id",
    ),
    pytest.param(
        ["run", "--installer-bootstrap", "--installer-run-id", _HANDOVER_RUN_ID, "--installer-native-fd", "0"],
        "--installer-native-fd must be a non-stdio descriptor",
        id="fd-0-refused",
    ),
    pytest.param(
        ["run", "--installer-bootstrap", "--installer-run-id", _HANDOVER_RUN_ID, "--installer-native-fd", "1"],
        "--installer-native-fd must be a non-stdio descriptor",
        id="fd-1-refused",
    ),
    pytest.param(
        ["run", "--installer-bootstrap", "--installer-run-id", _HANDOVER_RUN_ID, "--installer-native-fd", "2"],
        "--installer-native-fd must be a non-stdio descriptor",
        id="fd-2-refused",
    ),
]


@pytest.mark.parametrize(("argv", "message"), _HANDOVER_REFUSALS)
def test_native_handover_refuses_each_bad_flag_combination(
    argv: list[str],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    started: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "_run_server", lambda **kwargs: started.append(kwargs) or 0)
    args = cli.build_parser().parse_args(argv)
    assert args.func(args) == 2
    assert capsys.readouterr().err == f"{message}\n"
    assert started == []


_HANDOVER_ACCEPTED = [
    pytest.param(
        ["--installer-native-fd", "3", "--installer-native-workspace", "/abs/ws"],
        {"installer_native_fd": 3, "installer_native_workspace": "/abs/ws"},
        id="fd-3-with-workspace",
    ),
    pytest.param(
        ["--installer-native-fd", "3"],
        {"installer_native_fd": 3, "installer_native_workspace": None},
        id="fd-3-without-workspace",
    ),
    pytest.param(
        [
            "--installer-native-fd", "3", "--installer-native-workspace", "/abs/ws",
            "--installer-suggest-workspace", "/abs/suggest",
        ],
        {
            "installer_native_fd": 3,
            "installer_native_workspace": "/abs/ws",
            "installer_suggest_workspace": "/abs/suggest",
        },
        id="fd-with-suggest-workspace",
    ),
]


@pytest.mark.parametrize(("extra", "forwarded"), _HANDOVER_ACCEPTED)
def test_native_handover_forwards_each_accepted_combination(
    extra: list[str],
    forwarded: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "_run_server", lambda **kwargs: seen.append(kwargs) or 0)
    args = cli.build_parser().parse_args(
        ["run", "--installer-bootstrap", "--installer-run-id", _HANDOVER_RUN_ID, *extra]
    )
    assert args.func(args) == 0
    expected: dict[str, object] = {
        "supervised": False,
        "installer_bootstrap": True,
        "installer_run_id": _HANDOVER_RUN_ID,
        "installer_suggest_workspace": None,
    }
    expected.update(forwarded)
    assert seen == [expected]


def test_an_existing_definition_is_never_overwritten_by_the_native_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_verified_host_if_installed", lambda: None)
    monkeypatch.setattr(cli, "ensure_workspace_git", lambda *a, **k: None)
    monkeypatch.setattr(cli, "ensure_vault_git", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_remove_legacy_app_shortcuts", lambda *a, **k: False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launch = tmp_path / "LaunchAgents"
    definition = launch / "com.ciao.server.plist"
    definition.parent.mkdir()
    definition.write_text("do not replace", encoding="utf-8")
    with pytest.raises(RuntimeError, match="already exists"):
        cli.setup_workspace(
            workspace,
            auth_token="t",
            launch_agents_dir=launch,
            exclusive_service_definition=True,
        )
    assert definition.read_text(encoding="utf-8") == "do not replace"


def test_native_setup_uses_the_verified_host_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_service_platform", lambda: True)
    monkeypatch.setattr(cli, "ensure_workspace_git", lambda *a, **k: None)
    monkeypatch.setattr(cli, "ensure_vault_git", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_remove_legacy_app_shortcuts", lambda *a, **k: False)
    monkeypatch.setattr(
        cli, "_verified_host_if_installed", lambda: pytest.fail("selected a host twice")
    )
    launch = tmp_path / "LaunchAgents"
    written = cli.setup_workspace(
        tmp_path / "workspace",
        auth_token="t",
        launch_agents_dir=launch,
        verified_host=_host_ownership(),
        exclusive_service_definition=True,
    )
    definition = launch / "com.ciao.server.plist"
    assert definition in written
    parsed = plistlib.loads(definition.read_bytes())
    assert parsed["ProgramArguments"][1] == "serve"
    assert parsed["ProgramArguments"][0].endswith(server_host.EXECUTABLE_NAME)


# ── native finish hardening (#2340 review) ──────────────────────────────────

_STATE_CHANGED = (
    "the installer state changed while setup was open; nothing was written"
)
_WRITE_REFUSED = (
    "setup could not write this install because the on-disk state differs from "
    "the one the installer prepared; nothing was started"
)
_ALREADY_CLAIMED = (
    "setup already ran for this native bootstrap; nothing else was written"
)


class _Exited(Exception):
    """Stands in for os._exit so the test can observe the call and stop there."""


@pytest.mark.parametrize("key", ["python", "launch_agents_dir", "app_dir"])
@pytest.mark.parametrize(
    "value", [0, False, [], {}], ids=["zero", "false", "empty-list", "empty-dict"]
)
def test_native_setup_refuses_falsy_non_string_trust_overrides(
    key: str, value: object, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, _launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    monkeypatch.setattr(
        cli, "setup_workspace", lambda *a, **k: pytest.fail("setup wrote")
    )
    resp = _post(client, isolated_home / "workspace", **{key: value})
    assert resp.status_code == 409
    assert not (isolated_home / "workspace").exists()
    assert restarts == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workspace", 5),
        ("provider", ["claude"]),
        ("vault_root", 5),
        ("vault_mode", 1),
        ("workspace_name", 1),
        ("password", 12345678),
        ("auth_token", 12345678),
    ],
)
def test_native_setup_refuses_non_string_fields_before_writing(
    field: str, value: object, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, _launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    monkeypatch.setattr(
        cli, "setup_workspace", lambda *a, **k: pytest.fail("setup wrote")
    )
    body: dict[str, object] = {
        "password": "wizard-pass",
        "workspace": str(isolated_home / "workspace"),
        "workspace_name": "personal",
        "port": 8443,
    }
    body[field] = value
    resp = client.post("/api/setup/finish", json=body)
    assert resp.status_code == 400
    assert restarts == []


def test_a_second_native_finish_request_is_refused_and_setup_runs_once(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, _launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    real_setup = cli.setup_workspace
    calls: list[str] = []

    def counting(*args, **kwargs):
        calls.append("write")
        return real_setup(*args, **kwargs)

    monkeypatch.setattr(cli, "setup_workspace", counting)
    first = _post(client, isolated_home / "workspace")
    second = _post(client, isolated_home / "workspace")
    assert first.status_code == 200, first.text
    assert second.status_code == 409
    assert second.json() == {"error": _ALREADY_CLAIMED}
    assert calls == ["write"]
    assert restarts == [0]


def test_a_refused_native_finish_keeps_the_claim(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    monkeypatch.setattr(
        cli, "setup_workspace", lambda *a, **k: pytest.fail("setup wrote")
    )
    definition = launch / "com.ciao.server.plist"
    definition.write_text("present", encoding="utf-8")
    first = _post(client, isolated_home / "workspace")
    assert first.status_code == 409
    assert first.json() == {"error": _STATE_CHANGED}
    # The refusal after the claim leaves the child to exit nonzero, and writes
    # nothing: no workspace, no handoff, the foreign definition untouched.
    assert restarts == [1]
    assert not (isolated_home / "workspace").exists()
    assert not native_handoff.result_path().exists()
    assert definition.read_text(encoding="utf-8") == "present"
    definition.unlink()
    second = _post(client, isolated_home / "workspace")
    assert second.status_code == 409
    assert second.json() == {"error": _ALREADY_CLAIMED}
    # The claim refusal is not a new claimed path, so it adds no second exit.
    assert restarts == [1]
    assert not (isolated_home / "workspace").exists()


@pytest.mark.parametrize(
    "error",
    [RuntimeError("write failed"), OSError("disk gone")],
    ids=["runtime-error", "os-error"],
)
def test_a_native_write_failure_fails_closed(
    error: Exception, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, _launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )

    def failing(*args, **kwargs):
        raise error

    monkeypatch.setattr(cli, "setup_workspace", failing)
    resp = _post(client, isolated_home / "workspace")
    assert resp.status_code == 409
    assert resp.json() == {"error": _WRITE_REFUSED}
    assert restarts == [1]


@pytest.mark.parametrize("state", ["missing", "not-callable"])
def test_native_failure_exit_never_silently_does_nothing(
    state: str,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _context, _launch, _restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    if state == "missing":
        monkeypatch.delattr(client.app.state, "request_restart")
    else:
        client.app.state.request_restart = None
    exits: list[int] = []

    def recording_exit(code: int) -> None:
        exits.append(code)
        raise _Exited

    monkeypatch.setattr(os, "_exit", recording_exit)

    def failing(*args, **kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(cli, "setup_workspace", failing)
    with pytest.raises(_Exited):
        _post(client, isolated_home / "workspace")
    assert exits == [1]


def test_a_written_definition_mismatch_fails_closed(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    real_setup = cli.setup_workspace

    def reports_nothing(*args, **kwargs):
        real_setup(*args, **kwargs)
        return []

    monkeypatch.setattr(cli, "setup_workspace", reports_nothing)
    resp = _post(client, isolated_home / "workspace")
    assert resp.status_code == 409
    assert resp.json() == {"error": _RECORD_REFUSED}
    assert "ok" not in resp.json()
    assert restarts == [1]
    assert (launch / "com.ciao.server.plist").is_file()


_RECORD_REFUSED = (
    "setup wrote the workspace but could not record the native handoff result; "
    "nothing was started"
)


@pytest.mark.parametrize(
    "error",
    [
        native_handoff.NativeHandoffError("record"),
        OSError("record"),
        ValueError("record"),
    ],
    ids=["handoff-error", "os-error", "value-error"],
)
def test_a_handoff_record_failure_fails_closed(
    error: Exception, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, _launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )

    def failing(**kwargs):
        raise error

    monkeypatch.setattr(native_handoff, "record_setup_completion", failing)
    resp = _post(client, isolated_home / "workspace")
    assert resp.status_code == 409
    assert resp.json() == {"error": _RECORD_REFUSED}
    assert "ok" not in resp.json()
    assert restarts == [1]


def test_a_handoff_read_back_failure_fails_closed(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, _launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )

    def failing(**kwargs):
        raise native_handoff.NativeHandoffError("read")

    monkeypatch.setattr(native_handoff, "read_result", failing)
    resp = _post(client, isolated_home / "workspace")
    assert resp.status_code == 409
    assert resp.json() == {"error": _RECORD_REFUSED}
    assert "ok" not in resp.json()
    assert restarts == [1]


def test_a_handoff_identity_mismatch_fails_closed(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, _launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    real_read = native_handoff.read_result

    def other_run(**kwargs):
        return dataclasses.replace(real_read(**kwargs), run_id=str(uuid.uuid4()))

    monkeypatch.setattr(native_handoff, "read_result", other_run)
    resp = _post(client, isolated_home / "workspace")
    assert resp.status_code == 409
    assert resp.json() == {"error": _RECORD_REFUSED}
    assert "ok" not in resp.json()
    assert restarts == [1]


def test_a_native_context_outside_installer_mode_is_inconsistent(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, _launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    client.app.state.installer_bootstrap = False
    monkeypatch.setattr(
        cli, "setup_workspace", lambda *a, **k: pytest.fail("setup wrote")
    )
    resp = _post(client, isolated_home / "workspace")
    assert resp.status_code == 409
    assert resp.json() == {"error": "the native bootstrap state is inconsistent"}
    assert not (isolated_home / "workspace").exists()
    assert restarts == []


def test_native_finish_refuses_a_handoff_that_appears_during_the_wizard(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, context, _launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    context.handoff_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        cli, "setup_workspace", lambda *a, **k: pytest.fail("setup wrote")
    )
    resp = _post(client, isolated_home / "workspace")
    assert resp.status_code == 409
    assert resp.json() == {"error": _STATE_CHANGED}
    assert not (isolated_home / "workspace").exists()
    assert context.handoff_path.read_text(encoding="utf-8") == "{}"
    assert restarts == [1]


def test_capture_refuses_a_read_only_notification_descriptor(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, notification_pipe: tuple[int, int]
) -> None:
    read_fd, _write_fd = notification_pipe
    with pytest.raises(NativeBootstrapError, match="invalid_context"):
        _capture(monkeypatch, isolated_home, fd=read_fd)


# ── bind notification and the native stop (#2340 review) ────────────────────


class _RaisingNotify:
    def notify_bound(self, port: int) -> None:
        raise RuntimeError("the pipe is gone")


def test_a_raising_bind_notification_stops_the_server() -> None:
    server = SimpleNamespace(started=True, should_exit=False)
    reported = main._report_native_bound(_RaisingNotify(), 8443, server)
    assert reported is False
    assert server.should_exit is True


def test_a_bind_watcher_cancelled_before_the_server_starts_reports_nothing() -> None:
    tracker = main.StartupTracker(startup_id="startup-1")
    reported: list[str] = []
    server = SimpleNamespace(started=False, should_exit=False)

    async def run() -> None:
        task = asyncio.create_task(
            main._mark_bound_when_serving(
                server,
                tracker,
                poll_interval=0.001,
                on_bound=lambda: reported.append("notified"),
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert reported == []
    assert tracker.phase(BIND_PHASE).status == "pending"


def test_a_refused_capture_exits_with_the_native_refusal_code(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, notification_pipe: tuple[int, int]
) -> None:
    read_fd, write_fd = notification_pipe

    def refused(cls, **kwargs):
        raise NativeBootstrapError("definition_changed")

    monkeypatch.setattr(NativeBootstrapContext, "capture", classmethod(refused))
    code = asyncio.run(
        main._run_server_locked(
            _config(isolated_home),
            supervised=False,
            startup_id="startup-1",
            engine_pid=os.getpid(),
            installer_bootstrap=True,
            installer_run_id=str(uuid.uuid4()),
            installer_native_fd=write_fd,
            installer_native_workspace=None,
        )
    )
    assert code == native_installer.NATIVE_BOOTSTRAP_REFUSED_EXIT_CODE


# ── the wizard's ready signal and the live-state recheck (#2340 review) ─────


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_wizard_ready_follows_a_successful_bind(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, notification_pipe: tuple[int, int]
) -> None:
    read_fd, write_fd = notification_pipe
    context = _capture(monkeypatch, isolated_home, fd=write_fd)
    port = _free_loopback_port()
    server = uvicorn.Server(
        uvicorn.Config(
            Starlette(routes=[]),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="off",
        )
    )
    tracker = main.StartupTracker(startup_id="startup-1")

    def nothing_yet() -> bool:
        ready, _, _ = select.select([read_fd], [], [], 0)
        return not ready

    async def run() -> None:
        assert nothing_yet()
        serving = asyncio.create_task(server.serve())
        watcher = asyncio.create_task(
            main._mark_bound_when_serving(
                server,
                tracker,
                poll_interval=0.001,
                on_bound=lambda: main._report_native_bound(context, port, server),
            )
        )
        await asyncio.wait_for(watcher, timeout=10)
        bound_port = server.servers[0].sockets[0].getsockname()[1]
        assert bound_port == port
        record = os.read(read_fd, MAX_BIND_NOTIFICATION_BYTES + 1)
        assert json.loads(record) == {"schema": 1, "port": bound_port}
        assert os.read(read_fd, 1) == b""
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=10)

    asyncio.run(run())
    assert tracker.phase(BIND_PHASE).status == "done"


def test_native_save_refuses_a_service_changed_while_wizard_open(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    # Only the launchd job probe changes: the definition stays absent, so the
    # refusal can only come from the job guard.
    monkeypatch.setattr(native_installer, "_server_job_absent", lambda: False)
    reasons: list[str] = []
    real_assert = NativeBootstrapContext.assert_ready_to_write

    def spy(self, **kwargs):
        try:
            return real_assert(self, **kwargs)
        except NativeBootstrapError as exc:
            reasons.append(exc.reason)
            raise

    monkeypatch.setattr(NativeBootstrapContext, "assert_ready_to_write", spy)
    monkeypatch.setattr(
        cli, "setup_workspace", lambda *a, **k: pytest.fail("setup wrote")
    )
    workspace = isolated_home / "workspace"
    resp = _post(client, workspace)
    assert resp.status_code == 409
    assert resp.json() == {"error": _STATE_CHANGED}
    assert reasons == ["service_changed"]
    assert not workspace.exists()
    assert not native_handoff.result_path().exists()
    assert not (launch / "com.ciao.server.plist").exists()
    assert restarts == [1]


def test_public_event_fd_is_not_inherited_by_children(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, notification_pipe: tuple[int, int]
) -> None:
    _read_fd, write_fd = notification_pipe
    _capture(monkeypatch, isolated_home, fd=write_fd)
    probe = (
        "import os, sys\n"
        "try:\n"
        "    os.fstat(int(sys.argv[1]))\n"
        "except OSError:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    child = subprocess.run(
        [sys.executable, "-c", probe, str(write_fd)],
        close_fds=False,
        timeout=30,
    )
    assert child.returncode == 0, "the private event descriptor leaked into a child"

    # Control: the same probe on a descriptor that is inheritable must see it,
    # so the zero above is a real refusal rather than a broken probe.
    control_read, control_write = os.pipe()
    try:
        os.set_inheritable(control_write, True)
        control = subprocess.run(
            [sys.executable, "-c", probe, str(control_write)],
            close_fds=False,
            timeout=30,
        )
        assert control.returncode == 1
    finally:
        os.close(control_read)
        os.close(control_write)


# ── every path after the claim fails closed (#2340 round 2) ─────────────────


@pytest.mark.parametrize("trigger", ["record", "read-back", "resolve"])
def test_any_failure_after_the_claim_fails_closed(
    trigger: str, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, _launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    if trigger == "record":

        def failing_record(**kwargs):
            raise RuntimeError("record")

        monkeypatch.setattr(native_handoff, "record_setup_completion", failing_record)
    elif trigger == "read-back":

        def failing_read(**kwargs):
            raise RuntimeError("read")

        monkeypatch.setattr(native_handoff, "read_result", failing_read)
    else:
        # Only the native branch's own resolves (routes_api frames) fail; the
        # workspace writer in ciao.cli keeps its real resolves.
        real_resolve = Path.resolve

        def failing_resolve(self, *args, **kwargs):
            if sys._getframe(1).f_globals.get("__name__") == "ciao.web.routes_api":
                raise RuntimeError("resolve")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", failing_resolve)
    resp = _post(client, isolated_home / "workspace")
    assert resp.status_code == 409
    assert resp.json() == {"error": _WRITE_REFUSED}
    assert "ok" not in resp.json()
    assert restarts == [1]


def test_an_unexpected_pre_write_exception_fails_closed(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )

    def unexpected(self, **kwargs):
        raise RuntimeError("the host check exploded")

    monkeypatch.setattr(NativeBootstrapContext, "assert_ready_to_write", unexpected)
    monkeypatch.setattr(
        cli, "setup_workspace", lambda *a, **k: pytest.fail("setup wrote")
    )
    resp = _post(client, isolated_home / "workspace")
    assert resp.status_code == 409
    assert resp.json() == {"error": _WRITE_REFUSED}
    assert not (isolated_home / "workspace").exists()
    assert not native_handoff.result_path().exists()
    assert not (launch / "com.ciao.server.plist").exists()
    assert restarts == [1]


# ── cancellation and failing restart hooks (#2340 round 3) ───────────────────


def test_a_cancelled_native_finish_fails_closed(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, _launch, restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )

    from ciao.web import routes_api

    async def cancelled_write(*args, **kwargs):
        # The awaited write is cancelled on the event loop itself, which is how
        # a disconnect or shutdown reaches the handler. A CancelledError raised
        # inside the worker thread would come back as a different class.
        raise asyncio.CancelledError()

    monkeypatch.setattr(routes_api, "_native_finish_write", cancelled_write)
    # AuthMiddleware (a BaseHTTPMiddleware) rewrites a cancellation raised below
    # it into "No response returned", so the endpoint is driven on a bare app
    # that carries the same state and no middleware.
    bare = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])]
    )
    for name in (
        "config",
        "installer_bootstrap",
        "installer_run_id",
        "native_bootstrap",
        "request_restart",
        "startup_tracker",
    ):
        setattr(bare.state, name, getattr(client.app.state, name))
    bare_client = TestClient(bare, base_url="http://localhost:8443", client=_LOOPBACK_PEER)
    # A client disconnect or shutdown cancels the awaited write. The child must
    # still leave nonzero, and the cancellation must propagate, not become a
    # response. TestClient's portal reports the cancelled task as a
    # concurrent.futures.CancelledError; a 409 response would not raise at all.
    with pytest.raises(concurrent.futures.CancelledError):
        _post(bare_client, isolated_home / "workspace")
    assert restarts == [1]


def test_a_raising_restart_hook_still_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.web import routes_api

    exits: list[int] = []

    def recording_exit(code: int) -> None:
        exits.append(code)
        raise _Exited

    monkeypatch.setattr(os, "_exit", recording_exit)
    hook_calls: list[int] = []

    def raising_hook(code: int) -> None:
        hook_calls.append(code)
        raise RuntimeError("the restart hook broke")

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(request_restart=raising_hook))
    )
    with pytest.raises(_Exited):
        routes_api._request_native_failure_exit(request)
    assert hook_calls == [1]
    assert exits == [1]


def test_a_raising_final_native_restart_exits(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, _launch, _restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )

    def raising_restart(code: int) -> None:
        raise RuntimeError("the final restart broke")

    client.app.state.request_restart = raising_restart
    exits: list[int] = []

    def recording_exit(code: int) -> None:
        exits.append(code)
        raise _Exited

    monkeypatch.setattr(os, "_exit", recording_exit)
    # The setup itself succeeded and was recorded; a dead restart must not
    # leave that child serving.
    with pytest.raises(_Exited):
        _post(client, isolated_home / "workspace")
    assert exits == [1]


def test_a_missing_final_native_restart_exits(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _context, _launch, _restarts = _native_client_with_fakes(
        isolated_home, monkeypatch
    )
    monkeypatch.delattr(client.app.state, "request_restart")
    exits: list[int] = []

    def recording_exit(code: int) -> None:
        exits.append(code)
        raise _Exited

    monkeypatch.setattr(os, "_exit", recording_exit)
    with pytest.raises(_Exited):
        _post(client, isolated_home / "workspace")
    assert exits == [1]
