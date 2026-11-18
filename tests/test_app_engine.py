"""The app-owned engine: pointer, ready record, parent watchdog, and the wiring.

The Mac app spawns ``ciao run --app-engine`` as its child. Before a takeover the
old LaunchAgent plist still names the 1.x workspace, and after it nothing does —
so in app mode the engine answers "which workspace" only through the app-owned
pointer, never through the LaunchAgent, and never plants a service definition of
its own.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import app_engine
from ciao.cli import setup_workspace
from ciao.config import CiaoConfig


def _pointer_body(workspace: object, runtime_root: object, *, schema: int = 1) -> str:
    return json.dumps(
        {
            "schema": schema,
            "workspace": str(workspace),
            "runtime_root": str(runtime_root),
        }
    )


def _write_pointer_file(body: str, name: str = "workspace.json") -> Path:
    state = app_engine.state_dir()
    state.mkdir(parents=True, exist_ok=True)
    (state / name).write_text(body, encoding="utf-8")
    return state / name


def _app_mode(monkeypatch: pytest.MonkeyPatch, bundled: bool = True) -> None:
    """Act as if this engine runs from Ciaobot.app's Resources/engine."""
    monkeypatch.setattr(
        "ciao.package_version.running_from_app_bundle", lambda: bundled
    )


# --- the workspace pointer -----------------------------------------------------


def test_read_pointer_round_trips_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "workspace" / ".runtime"
    runtime.mkdir(parents=True)

    target = app_engine.write_pointer(workspace, runtime)

    assert target == app_engine.state_dir() / "workspace.json"
    pointer = app_engine.read_pointer()
    assert pointer == app_engine.WorkspacePointer(
        workspace=workspace.resolve(), runtime_root=runtime.resolve()
    )


def test_read_pointer_returns_none_when_missing(tmp_path: Path) -> None:
    assert app_engine.read_pointer() is None


def test_read_pointer_returns_none_for_unparsable_json(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    _write_pointer_file("not json at all")

    assert app_engine.read_pointer() is None


def test_read_pointer_returns_none_for_a_foreign_schema(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = tmp_path / "workspace" / ".runtime"
    runtime.mkdir()
    _write_pointer_file(_pointer_body(workspace, runtime, schema=2))

    assert app_engine.read_pointer() is None


def test_read_pointer_returns_none_for_a_boolean_schema(tmp_path: Path) -> None:
    """JSON `true` compares equal to 1 in Python; only the integer 1 is schema 1."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = tmp_path / "workspace" / ".runtime"
    runtime.mkdir()
    _write_pointer_file(
        json.dumps({"schema": True, "workspace": str(workspace), "runtime_root": str(runtime)})
    )

    assert app_engine.read_pointer() is None


def test_read_pointer_returns_none_for_relative_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_pointer_file(_pointer_body("workspace", "workspace/.runtime"))

    assert app_engine.read_pointer() is None


def test_read_pointer_returns_none_for_a_pointer_that_is_a_list(
    tmp_path: Path,
) -> None:
    _write_pointer_file("[]")

    assert app_engine.read_pointer() is None


def test_read_pointer_returns_none_when_the_workspace_directory_is_gone(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    _write_pointer_file(_pointer_body(tmp_path / "deleted-workspace", runtime))

    assert app_engine.read_pointer() is None


def test_write_pointer_permissions_and_placement(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "workspace" / ".runtime"
    runtime.mkdir(parents=True)

    target = app_engine.write_pointer(workspace, runtime)

    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload == {
        "schema": 1,
        "workspace": str(workspace.resolve()),
        "runtime_root": str(runtime.resolve()),
    }


def test_write_pointer_replaces_the_previous_workspace(tmp_path: Path) -> None:
    first = tmp_path / "workspace"
    (first / ".runtime").mkdir(parents=True)
    second = tmp_path / "moved"
    (second / ".runtime").mkdir(parents=True)

    app_engine.write_pointer(first, first / ".runtime")
    target = app_engine.write_pointer(second, second / ".runtime")

    assert app_engine.read_pointer().workspace == second.resolve()  # type: ignore[union-attr]
    assert [entry.name for entry in target.parent.iterdir()] == ["workspace.json"]


def test_a_failed_pointer_write_leaves_no_temp_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "workspace" / ".runtime"
    runtime.mkdir(parents=True)
    app_engine.write_pointer(workspace, runtime)

    def broken_replace(src: object, dst: object) -> None:
        raise OSError("no rename")

    monkeypatch.setattr(app_engine.os, "replace", broken_replace)
    with pytest.raises(OSError):
        app_engine.write_pointer(tmp_path / "other", tmp_path / "other" / ".runtime")

    # The temp file was cleaned up, and the old pointer still stands.
    assert [entry.name for entry in app_engine.state_dir().iterdir()] == [
        "workspace.json"
    ]
    monkeypatch.undo()
    app_engine.write_pointer(tmp_path / "other", tmp_path / "other" / ".runtime")
    assert [entry.name for entry in app_engine.state_dir().iterdir()] == ["workspace.json"]


def test_clear_pointer_refuses_a_different_workspace(tmp_path: Path) -> None:
    named = tmp_path / "named"
    (named / ".runtime").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    app_engine.write_pointer(named, named / ".runtime")

    assert app_engine.clear_pointer(expected_workspace=other) is False

    pointer_path = app_engine.state_dir() / "workspace.json"
    assert pointer_path.exists()


def test_clear_pointer_removes_only_matching_workspaces(tmp_path: Path) -> None:
    named = tmp_path / "named"
    (named / ".runtime").mkdir(parents=True)

    # Missing pointer: nothing to clear, nothing to create.
    assert app_engine.clear_pointer(expected_workspace=named) is False
    # A corrupt file never names the workspace, so it is never cleared.
    _write_pointer_file("corrupt")
    assert app_engine.clear_pointer(expected_workspace=named) is False

    app_engine.write_pointer(named, named / ".runtime")
    assert app_engine.clear_pointer(expected_workspace=named.resolve()) is True
    assert not (app_engine.state_dir() / "workspace.json").exists()


# --- the ready record ----------------------------------------------------------


def test_write_ready_record_shape_and_permission(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    target = app_engine.write_ready_record(
        pid=os.getpid(),
        port=8443,
        startup_id="abc123",
        workspace=workspace,
        bootstrap=True,
    )

    assert target == app_engine.state_dir() / "ready.json"
    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "schema": 1,
        "pid": os.getpid(),
        "port": 8443,
        "startup_id": "abc123",
        "workspace": str(workspace.resolve()),
        "bootstrap": True,
    }


# --- the parent watchdog -------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="no kqueue on Windows")
def test_watch_parent_calls_back_at_once_for_init() -> None:
    fired: list[int] = []

    thread = app_engine.watch_parent(lambda: fired.append(1), ppid=1)
    thread.join(timeout=5)

    assert fired == [1]


@pytest.mark.skipif(sys.platform == "win32", reason="no kqueue on Windows")
def test_watch_parent_calls_back_at_once_when_the_parent_is_already_gone() -> None:
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()

    fired: list[int] = []

    thread = app_engine.watch_parent(lambda: fired.append(1), ppid=gone.pid)
    thread.join(timeout=5)

    assert fired == [1]


@pytest.mark.skipif(sys.platform != "darwin", reason="kqueue EVFILT_PROC is macOS-only")
def test_watch_parent_fires_when_the_watched_parent_dies(tmp_path: Path) -> None:
    """The app is killed while the engine keeps running; the engine must notice.

    The real shape, small: the grandchild here is the engine running
    ``watch_parent`` against its own parent (default ``os.getppid()``); killing
    the middle process is killing the app.
    """
    repo = str(Path(app_engine.__file__).resolve().parents[1])
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        textwrap.dedent(
            """
            import sys
            import time
            from pathlib import Path

            from ciao.app_engine import watch_parent


            def on_parent_exit() -> None:
                Path(sys.argv[1]).write_text("fired", encoding="utf-8")


            watch_parent(on_parent_exit)
            time.sleep(30)
            """
        ),
        encoding="utf-8",
    )
    child = tmp_path / "child.py"
    child.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import subprocess
            import sys

            repo, grandchild, marker = sys.argv[1:4]
            env = dict(os.environ)
            env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
            process = subprocess.Popen([sys.executable, grandchild, marker], env=env)
            print(json.dumps({"grandchild": process.pid}), flush=True)
            time.sleep(30)
            """
        ),
        encoding="utf-8",
    )
    marker = tmp_path / "marker"
    env = dict(os.environ)
    env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
    middle = subprocess.Popen(
        [sys.executable, str(child), repo, str(grandchild), str(marker)],
        stdout=subprocess.PIPE,
        env=env,
    )

    def _kill(pid: int) -> None:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass

    try:
        grandchild_pid = int(
            json.loads((middle.stdout or b"").readline().decode())["grandchild"]
        )
        # A moment for the grandchild to register its kqueue filter; after the
        # middle process dies only the callback below matters.
        time.sleep(1.0)
        _kill(middle.pid)
        middle.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.05)
        assert marker.exists(), "the engine never learned its parent died"
    finally:
        try:
            _kill(middle.pid)
        except ValueError:  # pragma: no cover — middle.pid is always an int
            pass
        if "grandchild_pid" in locals():
            _kill(grandchild_pid)
        if middle.stdout is not None:
            middle.stdout.close()


# --- the run command wiring -----------------------------------------------------


def test_app_engine_flag_is_hidden_and_parses() -> None:
    from ciao import cli

    args = cli.build_parser().parse_args(["run", "--app-engine"])
    assert args.app_engine is True
    assert cli.build_parser().parse_args(["run"]).app_engine is False


def test_app_engine_refused_outside_the_bundle(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the engine inside Ciaobot.app may run in app mode."""
    from ciao import cli, package_version

    monkeypatch.setattr(package_version, "running_from_app_bundle", lambda: False)
    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli, "_run_server", lambda **kwargs: dispatched.append(kwargs) or 0
    )

    assert cli.main(["run", "--app-engine"]) == 2

    assert dispatched == []
    assert "Ciaobot.app" in capsys.readouterr().err


def test_app_engine_refuses_the_installer_flags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The installer handshake has no parent under the app: both modes at once
    is a contradiction."""
    from ciao import cli, package_version

    monkeypatch.setattr(package_version, "running_from_app_bundle", lambda: True)
    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli, "_run_server", lambda **kwargs: dispatched.append(kwargs) or 0
    )

    assert cli.main(["run", "--app-engine", "--installer-bootstrap"]) == 2

    assert dispatched == []
    assert "installer" in capsys.readouterr().err


def test_app_engine_dispatch_carries_the_app_engine_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ciao import cli, package_version

    monkeypatch.setattr(package_version, "running_from_app_bundle", lambda: True)
    dispatched: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli, "_run_server", lambda **kwargs: dispatched.append(kwargs) or 0
    )

    assert cli.main(["run", "--app-engine"]) == 0

    assert dispatched == [{"supervised": False, "app_engine": True}]


# --- setup under the app ---------------------------------------------------------


def test_setup_in_app_mode_writes_the_pointer_and_no_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Mac app owns this engine, so setup must not plant a LaunchAgent."""
    from ciao import app_engine, package_version

    monkeypatch.setattr(package_version, "running_from_app_bundle", lambda: True)
    launch_agents = tmp_path / "LaunchAgents"
    workspace = tmp_path / "workspace"

    written = setup_workspace(
        workspace, auth_token="t", launch_agents_dir=launch_agents, port=8443
    )

    assert not list(launch_agents.glob("*.plist"))
    pointer = app_engine.read_pointer()
    assert pointer is not None
    assert pointer.workspace == workspace.resolve()
    assert pointer.runtime_root == (workspace / ".runtime").resolve()
    assert app_engine.state_dir() / "workspace.json" in written


def test_setup_outside_the_app_still_writes_the_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import package_version
    from ciao.cli import setup_workspace

    monkeypatch.setattr(package_version, "running_from_app_bundle", lambda: False)
    launch_agents = tmp_path / "LaunchAgents"
    workspace = tmp_path / "workspace"

    setup_workspace(
        workspace, auth_token="t", launch_agents_dir=launch_agents, port=8443
    )

    assert list(launch_agents.glob("*.plist"))


# --- the server entry wiring ------------------------------------------------------


def _recording_config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        state_path=tmp_path / "runtime" / "state.json",
        workspace_root=tmp_path / "workspace",
        pwa_port=8443,
        bootstrap_mode=False,
        _workspace_registry_changed=False,
        import_legacy_workspaces_env=lambda: None,
        import_legacy_gws_profile_env=lambda: None,
    )


class _RecordingLock:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.startup_id = "startup-1"

    def __enter__(self) -> "_RecordingLock":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_async_main_publishes_the_ready_record_in_app_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The app's readiness proof: pid, port, startup_id and workspace, under
    the lock, before the server starts serving."""
    import asyncio

    from ciao import app_engine
    from ciao import main as ciao_main

    config = _recording_config(tmp_path)
    seen: list[str] = []

    async def run_server_locked(_config: object, **kwargs: object) -> int:
        seen.append("run")
        return 0

    watched: list[bool] = []
    monkeypatch.setattr(ciao_main.CiaoConfig, "from_env", lambda **_: config)
    monkeypatch.setattr("ciao.instance_lock.WorkspaceInstanceLock", _RecordingLock)
    monkeypatch.setattr(ciao_main, "_run_server_locked", run_server_locked)
    monkeypatch.setattr(
        app_engine, "watch_parent", lambda _cb: watched.append(True) or None
    )

    assert asyncio.run(ciao_main._async_main(app_engine=True)) == 0

    record = json.loads(
        (app_engine.state_dir() / "ready.json").read_text(encoding="utf-8")
    )
    assert record == {
        "schema": 1,
        "pid": os.getpid(),
        "port": 8443,
        "startup_id": "startup-1",
        "workspace": str((tmp_path / "workspace").resolve()),
        "bootstrap": False,
    }
    assert watched == [True]
    assert seen == ["run"]


def test_async_main_writes_no_ready_record_outside_app_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The legacy launchd run keeps its exact behaviour: no app files at all."""
    import asyncio

    from ciao import app_engine
    from ciao import main as ciao_main

    config = _recording_config(tmp_path)

    async def run_server_locked(_config: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr(ciao_main.CiaoConfig, "from_env", lambda **_: config)
    monkeypatch.setattr("ciao.instance_lock.WorkspaceInstanceLock", _RecordingLock)
    monkeypatch.setattr(ciao_main, "_run_server_locked", run_server_locked)

    assert asyncio.run(ciao_main._async_main()) == 0

    assert not (app_engine.state_dir() / "ready.json").exists()
