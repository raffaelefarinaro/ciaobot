from __future__ import annotations

import dataclasses
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ciao import macos_service
from ciao.os_support.limits import SERVER_NOFILE_TARGET

# These reach the real launchd domain (`gui/<uid>`, built from os.getuid) or
# classify the Ciaobot.app launchd layout; neither exists on Windows, whose
# service is a Task Scheduler task (tests/test_windows_service.py). The parser
# contracts and the platform-stubbed tests stay on every OS.
launchd_only = pytest.mark.skipif(
    sys.platform == "win32", reason="the launchd domain and Ciaobot.app layout are macOS-only"
)


def _runtime(tmp_path: Path) -> macos_service.DesktopRuntime:
    workspace = tmp_path / "workspace"
    runtime = workspace / ".runtime"
    agents = tmp_path / "LaunchAgents"
    workspace.mkdir()
    runtime.mkdir()
    agents.mkdir()
    plist = agents / "com.ciao.server.plist"
    plist.write_bytes(plistlib.dumps({"Label": "com.ciao.server"}))
    return macos_service.DesktopRuntime(
        workspace=str(workspace),
        runtime_root=str(runtime),
        port=9443,
        server_plist=str(plist),
        python_path="/opt/homebrew/bin/python3",
    )


def _runner(calls: list[list[str]], *, returncode: int = 0):
    def run(command, **_kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, returncode, stdout="", stderr="")

    return run


def _engine_starts(monkeypatch, startup_id: str = "run-1") -> None:
    """The engine the CLI started answers as itself: a verified start (#1938).

    The CLI tests here drive `ciao service start` with launchctl stubbed, so
    no engine ever boots; without this the wait would run to its deadline and
    report an unconfirmed start.
    """
    monkeypatch.setattr(
        macos_service,
        "wait_for_engine",
        lambda _runtime, **_kw: macos_service.StartOutcome(
            "started", "Ciaobot engine started.", startup_id=startup_id, pid=4242
        ),
    )


def _write_bundle(path: Path, executable: str) -> None:
    macos = path / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / executable).write_text("binary", encoding="utf-8")
    (path / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "local.ciaobot.app",
                "CFBundleExecutable": executable,
            }
        )
    )


def test_discover_runtime_prefers_workspace_dotenv(tmp_path: Path) -> None:
    workspace = tmp_path / "Ciao Workspace"
    workspace.mkdir()
    # A CIAO_RUNTIME_ROOT in `.env` is not read: the service definition's is.
    (workspace / ".env").write_text(
        "PWA_PORT=9555\nCIAO_RUNTIME_ROOT=elsewhere\n",
        encoding="utf-8",
    )
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.ciao.server.plist").write_bytes(
        plistlib.dumps(
            {
                "WorkingDirectory": str(workspace),
                "EnvironmentVariables": {
                    "CIAO_PORT": "8443",
                    "CIAO_RUNTIME_ROOT": str(workspace / "var/runtime"),
                },
                "ProgramArguments": ["/stable/python", "-m", "ciao.cli", "run"],
            }
        )
    )

    runtime = macos_service.discover_runtime(launch_agents_dir=agents, environ={})

    assert runtime.workspace == str(workspace.resolve())
    assert runtime.port == 9555
    assert runtime.runtime_root == str((workspace / "var/runtime").resolve())
    assert runtime.python_path == "/stable/python"


def test_discover_runtime_reports_the_served_interpreter_for_a_hosted_definition(
    tmp_path: Path,
) -> None:
    # A hosted definition runs Ciaobot Server, which is not a Python
    # interpreter: reporting argv[0] as python_path would hand every consumer
    # (and update_engine's bundled-engine check) the host binary. The served
    # interpreter is the answer.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.ciao.server.plist").write_bytes(
        plistlib.dumps(
            {
                "WorkingDirectory": str(workspace),
                "ProgramArguments": [
                    "/Users/me/Applications/Ciaobot Server.app/Contents/MacOS/"
                    "Ciaobot Server",
                    "serve",
                    "--python",
                    "/opt/ciao/venv/bin/python",
                ],
            }
        )
    )

    runtime = macos_service.discover_runtime(launch_agents_dir=agents, environ={})

    assert runtime.python_path == "/opt/ciao/venv/bin/python"


def test_discover_runtime_keeps_argv0_for_a_legacy_direct_shape(
    tmp_path: Path,
) -> None:
    # The parser refuses an arbitrary console name and a python3-intel64-style
    # interpreter basename. Recognition is permissive on purpose: a working
    # install's status must not become an error, so argv[0] stays the answer.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.ciao.server.plist").write_bytes(
        plistlib.dumps(
            {
                "WorkingDirectory": str(workspace),
                "ProgramArguments": ["/opt/ciao/bin/python3-intel64", "run"],
            }
        )
    )

    runtime = macos_service.discover_runtime(launch_agents_dir=agents, environ={})

    assert runtime.python_path == "/opt/ciao/bin/python3-intel64"


def test_start_service_uses_explicit_launchctl_argv(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    calls: list[list[str]] = []

    result = macos_service.start_service(
        runtime=runtime,
        uid=501,
        runner=_runner(calls),
    )

    assert result.ok is True
    assert calls[:3] == [
        ["launchctl", "enable", "gui/501/com.ciao.server"],
        ["launchctl", "bootstrap", "gui/501", runtime.server_plist],
        ["launchctl", "kickstart", "-k", "gui/501/com.ciao.server"],
    ]


def test_restart_and_stop_require_confirmation_for_active_chats(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(macos_service, "restart_blockers", lambda _port: [{"chat_id": "chat-1", "reasons": ["streaming"]}])
    calls: list[list[str]] = []

    restart = macos_service.restart_service(
        runtime=runtime,
        runner=_runner(calls),
    )
    stop = macos_service.stop_service(
        runtime=runtime,
        runner=_runner(calls),
    )

    assert restart.ok is False
    assert restart.details["requires_confirmation"] is True
    assert stop.ok is False
    assert stop.details["restart_blockers"] == [{"chat_id": "chat-1", "reasons": ["streaming"]}]
    assert calls == []


def test_force_stop_boots_out_server(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(macos_service, "restart_blockers", lambda _port: [{"chat_id": "chat-1", "reasons": ["streaming"]}])
    calls: list[list[str]] = []

    result = macos_service.stop_service(
        runtime=runtime,
        force=True,
        uid=777,
        runner=_runner(calls),
    )

    assert result.ok is True
    assert calls == [["launchctl", "bootout", "gui/777/com.ciao.server"]]


def test_migrate_and_rollback_preserve_recoverable_assets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    agents = Path(runtime.server_plist).parent
    legacy_plist = agents / "com.ciao.menubar.plist"
    legacy_plist.write_text("legacy plist", encoding="utf-8")
    applications = tmp_path / "Applications"
    desktop = applications / "Ciaobot.app"
    legacy_app = applications / "Ciaobot Server.app"
    _write_bundle(desktop, "ciaobot-desktop")
    _write_bundle(legacy_app, "CiaobotServer")
    (legacy_app / "marker").write_text("legacy app", encoding="utf-8")
    trash = tmp_path / "Trash"
    calls: list[list[str]] = []
    monkeypatch.setattr(macos_service, "server_reachable", lambda _port: True)

    migrated = macos_service.migrate_legacy_companion(
        runtime=runtime,
        launch_agents_dir=agents,
        applications_dirs=[applications],
        trash_dir=trash,
        uid=501,
        runner=_runner(calls),
    )

    assert migrated.ok is True
    assert not legacy_plist.exists()
    assert (Path(runtime.runtime_root) / "migration/com.ciao.menubar.plist").exists()
    assert not legacy_app.exists()
    assert (trash / "Ciaobot Server.app/marker").read_text() == "legacy app"
    receipt = json.loads(
        (Path(runtime.runtime_root) / "migration/desktop-migration.json").read_text()
    )
    assert "desktop-token" not in json.dumps(receipt)

    rolled_back = macos_service.rollback_legacy_companion(
        runtime=runtime,
        launch_agents_dir=agents,
        uid=501,
        runner=_runner(calls),
    )

    assert rolled_back.ok is True
    assert legacy_plist.read_text() == "legacy plist"
    assert (legacy_app / "marker").read_text() == "legacy app"


@launchd_only
def test_migrate_is_idempotent_and_ignores_unrecognized_apps(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    agents = Path(runtime.server_plist).parent
    legacy_plist = agents / "com.ciao.menubar.plist"
    legacy_plist.write_text("legacy plist", encoding="utf-8")
    applications = tmp_path / "Applications"
    _write_bundle(applications / "Ciaobot.app", "ciaobot-desktop")
    unrecognized = applications / "Ciaobot Server.app"
    _write_bundle(unrecognized, "UnexpectedExecutable")
    trash = tmp_path / "Trash"
    monkeypatch.setattr(macos_service, "server_reachable", lambda _port: True)

    first = macos_service.migrate_legacy_companion(
        runtime=runtime,
        launch_agents_dir=agents,
        applications_dirs=[applications],
        trash_dir=trash,
        runner=_runner([]),
    )
    second = macos_service.migrate_legacy_companion(
        runtime=runtime,
        launch_agents_dir=agents,
        applications_dirs=[applications],
        trash_dir=trash,
        runner=_runner([]),
    )

    assert first.ok is True
    assert second.ok is True
    assert second.details["already_migrated"] is True
    assert unrecognized.is_dir()
    assert not trash.exists()


def test_migrate_rejects_an_uninstalled_calling_bundle(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    applications = tmp_path / "Applications"
    development_bundle = tmp_path / "target" / "Ciaobot.app"
    _write_bundle(development_bundle, "ciaobot-desktop")

    result = macos_service.migrate_legacy_companion(
        runtime=runtime,
        running_app=development_bundle,
        applications_dirs=[applications],
        runner=_runner([]),
    )

    assert result.ok is False
    assert "installed Ciaobot.app" in result.message


def test_desktop_service_parser_contract() -> None:
    from ciao.cli import build_parser

    parser = build_parser()

    restart = parser.parse_args(["desktop-service", "restart", "--force", "--json"])
    update = parser.parse_args(
        ["desktop-service", "update-engine", "--force", "--json"]
    )
    login = parser.parse_args(["desktop-service", "login", "disable", "--json"])
    migrate = parser.parse_args(
        [
            "desktop-service",
            "migrate",
            "--app-bundle",
            "/Applications/Ciaobot.app",
            "--json",
        ]
    )

    assert restart.service_action == "restart"
    assert restart.deprecated_alias is True
    assert restart.force is True
    assert restart.as_json is True
    assert update.service_action == "update-engine"
    assert update.force is True
    assert login.login_action == "disable"
    assert migrate.app_bundle == Path("/Applications/Ciaobot.app")


def test_service_parser_contract() -> None:
    from ciao.cli import build_parser

    parser = build_parser()

    start = parser.parse_args(
        ["service", "start", "--workspace", "/tmp/ws", "--json"]
    )
    login = parser.parse_args(["service", "login", "enable"])
    stop = parser.parse_args(["service", "stop", "--force"])

    assert start.service_action == "start"
    assert start.workspace == Path("/tmp/ws")
    assert start.as_json is True
    assert start.deprecated_alias is False
    assert login.login_action == "enable"
    assert stop.force is True


@launchd_only
def test_service_start_registers_missing_launch_agent(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from ciao import cli

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".env").write_text("PWA_PORT=9555\n", encoding="utf-8")
    (workspace / ".runtime").mkdir(parents=True, exist_ok=True)
    (workspace / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    _engine_starts(monkeypatch)
    monkeypatch.setattr(
        macos_service,
        "_launchctl",
        lambda args, runner=None: calls.append(list(args))
        or subprocess.CompletedProcess(["launchctl", *args], 0, "", ""),
    )

    rc = cli.main(
        ["service", "start", "--workspace", str(workspace), "--json"]
    )

    plist_path = (
        Path(os.environ["CIAO_LAUNCH_AGENTS_DIR"]) / "com.ciao.server.plist"
    )
    assert rc == 0
    assert plist_path.is_file()
    plist_data = plistlib.loads(plist_path.read_bytes())
    environment = plist_data["EnvironmentVariables"]
    assert environment["CIAO_WORKSPACE"] == str(workspace.resolve())
    assert environment["CIAO_PORT"] == "9555"
    assert plist_data["SoftResourceLimits"]["NumberOfFiles"] == SERVER_NOFILE_TARGET
    assert plist_data["HardResourceLimits"]["NumberOfFiles"] == SERVER_NOFILE_TARGET
    assert ["bootstrap", f"gui/{os.getuid()}", str(plist_path)] in calls
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_service_start_without_workspace_reports_setup_hint(
    monkeypatch, capsys
) -> None:
    from ciao import cli

    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        macos_service,
        "_launchctl",
        lambda args, runner=None: calls.append(list(args))
        or subprocess.CompletedProcess(["launchctl", *args], 0, "", ""),
    )

    rc = cli.main(["service", "start", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["details"]["setup_required"] is True
    assert "--workspace" in payload["message"]
    assert calls == []


def test_service_start_rejects_a_directory_that_was_never_set_up(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from ciao import cli

    workspace = tmp_path / "ws"
    workspace.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        macos_service,
        "_launchctl",
        lambda args, runner=None: calls.append(list(args))
        or subprocess.CompletedProcess(["launchctl", *args], 0, "", ""),
    )

    rc = cli.main(
        ["service", "start", "--workspace", str(workspace), "--json"]
    )

    payload = json.loads(capsys.readouterr().out)
    plist_path = (
        Path(os.environ["CIAO_LAUNCH_AGENTS_DIR"]) / "com.ciao.server.plist"
    )
    assert rc == 1
    assert "no .runtime/workspaces.json" in payload["message"]
    assert not plist_path.exists()
    assert calls == []


def test_service_start_rejects_source_checkout(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from ciao import cli

    workspace = tmp_path / "ws"
    (workspace / "ciao").mkdir(parents=True)
    (workspace / ".env").write_text("PWA_PORT=9555\n", encoding="utf-8")
    (workspace / ".runtime").mkdir(parents=True, exist_ok=True)
    (workspace / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")
    (workspace / "pyproject.toml").write_text("", encoding="utf-8")
    (workspace / "ciao" / "__init__.py").write_text("", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        macos_service,
        "_launchctl",
        lambda args, runner=None: calls.append(list(args))
        or subprocess.CompletedProcess(["launchctl", *args], 0, "", ""),
    )

    rc = cli.main(
        ["service", "start", "--workspace", str(workspace), "--json"]
    )

    payload = json.loads(capsys.readouterr().out)
    plist_path = (
        Path(os.environ["CIAO_LAUNCH_AGENTS_DIR"]) / "com.ciao.server.plist"
    )
    assert rc == 1
    assert "source checkout" in payload["message"]
    assert not plist_path.exists()
    assert calls == []


def test_service_start_rejects_tcc_protected_workspace(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from ciao import cli

    home = tmp_path
    workspace = home / "Documents" / "ws"
    workspace.mkdir(parents=True)
    (workspace / ".env").write_text("PWA_PORT=9555\n", encoding="utf-8")
    (workspace / ".runtime").mkdir(parents=True, exist_ok=True)
    (workspace / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli, "_verified_host_if_installed", lambda: None)
    monkeypatch.setattr(
        macos_service,
        "_launchctl",
        lambda args, runner=None: calls.append(list(args))
        or subprocess.CompletedProcess(["launchctl", *args], 0, "", ""),
    )

    rc = cli.main(
        ["service", "start", "--workspace", str(workspace), "--json"]
    )

    payload = json.loads(capsys.readouterr().out)
    plist_path = (
        Path(os.environ["CIAO_LAUNCH_AGENTS_DIR"]) / "com.ciao.server.plist"
    )
    assert rc == 1
    assert "Documents" in payload["message"]
    assert "Ciaobot Server" in payload["message"]
    assert not plist_path.exists()
    assert calls == []


def test_service_start_registers_a_protected_workspace_under_the_host(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import plistlib

    from ciao import cli
    from tests.test_server_host_activation import (
        _ENGINE_PYTHON,
        _hosted_expected,
        _verified_host,
    )

    home = tmp_path
    workspace = home / "Documents" / "ws"
    workspace.mkdir(parents=True)
    (workspace / ".env").write_text("PWA_PORT=9555\n", encoding="utf-8")
    (workspace / ".runtime").mkdir(parents=True, exist_ok=True)
    (workspace / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")
    host = _verified_host()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "platform", "darwin")
    _engine_starts(monkeypatch)
    monkeypatch.setattr(cli, "_verified_host_if_installed", lambda: host)
    monkeypatch.setattr(sys, "executable", _ENGINE_PYTHON)
    monkeypatch.setattr(macos_service, "_getuid", lambda: 501)
    monkeypatch.delenv("CIAO_ENGINE_PATH", raising=False)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        macos_service,
        "_launchctl",
        lambda args, runner=None: calls.append(list(args))
        or subprocess.CompletedProcess(["launchctl", *args], 0, "", ""),
    )

    rc = cli.main(["service", "start", "--workspace", str(workspace), "--json"])

    plist_path = (
        Path(os.environ["CIAO_LAUNCH_AGENTS_DIR"]) / "com.ciao.server.plist"
    )
    assert rc == 0, capsys.readouterr().out
    with plist_path.open("rb") as handle:
        argv = plistlib.load(handle)["ProgramArguments"]
    assert argv == _hosted_expected(host, _ENGINE_PYTHON)
    gui_domains = [arg for call in calls for arg in call if arg.startswith("gui/")]
    assert gui_domains
    assert all(arg == "gui/501" or arg.startswith("gui/501/") for arg in gui_domains)


@launchd_only
def test_service_start_register_pins_the_runtime_root_and_honors_engine_path(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The runtime root is always `<workspace>/.runtime`: a CIAO_RUNTIME_ROOT
    left in an old `.env` is not read when the service definition is written."""
    from ciao import cli

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "PWA_PORT=9555\nCIAO_RUNTIME_ROOT=rt\n", encoding="utf-8"
    )
    (workspace / ".runtime").mkdir()
    (workspace / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")
    engine = tmp_path / "bin" / "ciao"
    calls: list[list[str]] = []
    monkeypatch.setenv("CIAO_ENGINE_PATH", str(engine))
    monkeypatch.setattr(sys, "platform", "darwin")
    _engine_starts(monkeypatch)
    monkeypatch.setattr(
        macos_service,
        "_launchctl",
        lambda args, runner=None: calls.append(list(args))
        or subprocess.CompletedProcess(["launchctl", *args], 0, "", ""),
    )

    rc = cli.main(
        ["service", "start", "--workspace", str(workspace), "--json"]
    )

    plist_path = (
        Path(os.environ["CIAO_LAUNCH_AGENTS_DIR"]) / "com.ciao.server.plist"
    )
    assert rc == 0
    assert plist_path.is_file()
    plist_data = plistlib.loads(plist_path.read_bytes())
    assert (
        plist_data["EnvironmentVariables"]["CIAO_RUNTIME_ROOT"]
        == str((workspace / ".runtime").resolve())
    )
    assert plist_data["ProgramArguments"][0] == str(engine)
    assert "-m" not in plist_data["ProgramArguments"]


@launchd_only
def test_service_start_refuses_workspace_mismatch(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from ciao import cli

    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    for workspace in (ws_a, ws_b):
        workspace.mkdir()
        (workspace / ".env").write_text("PWA_PORT=9555\n", encoding="utf-8")
        (workspace / ".runtime").mkdir(parents=True, exist_ok=True)
        (workspace / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    _engine_starts(monkeypatch)
    monkeypatch.setattr(
        macos_service,
        "_launchctl",
        lambda args, runner=None: calls.append(list(args))
        or subprocess.CompletedProcess(["launchctl", *args], 0, "", ""),
    )

    assert cli.main(["service", "start", "--workspace", str(ws_a), "--json"]) == 0
    calls.clear()
    capsys.readouterr()

    rc = cli.main(["service", "start", "--workspace", str(ws_b), "--json"])

    payload = json.loads(capsys.readouterr().out)
    plist_path = (
        Path(os.environ["CIAO_LAUNCH_AGENTS_DIR"]) / "com.ciao.server.plist"
    )
    plist_data = plistlib.loads(plist_path.read_bytes())
    assert rc == 1
    assert str(ws_a.resolve()) in payload["message"]
    assert calls == []
    assert plist_data["EnvironmentVariables"]["CIAO_WORKSPACE"] == str(
        ws_a.resolve()
    )


def test_service_refuses_on_non_macos(monkeypatch, capsys) -> None:
    from ciao import cli

    def unexpected_launchctl(*_args, **_kwargs):
        raise AssertionError("launchctl must not run on non-macOS")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(macos_service, "_launchctl", unexpected_launchctl)

    rc = cli.main(["service", "status", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "linux-service" in payload["message"]


def test_desktop_service_alias_warns_only_without_json(monkeypatch, capsys) -> None:
    from ciao import cli

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        macos_service,
        "service_status",
        lambda **_: macos_service.ServiceResult(True, "status", "ok", {}),
    )

    assert cli.main(["desktop-service", "status"]) == 0
    first = capsys.readouterr()
    assert "deprecated" in first.err

    assert cli.main(["desktop-service", "status", "--json"]) == 0
    second = capsys.readouterr()
    assert second.err == ""
    assert json.loads(second.out)["ok"] is True


def test_update_engine_requires_confirmation_before_upgrading_active_chats(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(macos_service, "restart_blockers", lambda _port: [{"chat_id": "chat-1", "reasons": ["streaming"]}])
    monkeypatch.setattr(
        "ciao.package_version.update_package",
        lambda: (_ for _ in ()).throw(AssertionError("upgrade must not start")),
    )

    result = macos_service.update_engine(runtime=runtime, runner=_runner([]))

    assert result.ok is False
    assert result.details["requires_confirmation"] is True
    assert result.details["restart_blockers"] == [{"chat_id": "chat-1", "reasons": ["streaming"]}]


def test_update_engine_surfaces_already_current_for_a_noop_upgrade(
    tmp_path: Path, monkeypatch
) -> None:
    # The desktop's single "Update…" runs the engine first and then the app. A
    # no-op engine upgrade reports ok=False (so the PWA banner stays honest),
    # so the desktop needs a structured flag to know it should still go on and
    # update the app instead of aborting with an error dialog.
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(
        "ciao.package_version.update_package",
        lambda: {
            "ok": False,
            "already_current": True,
            "version": "0.5.3",
            "error": "Still on 0.5.3 after running the upgrade",
        },
    )

    result = macos_service.update_engine(runtime=runtime, runner=_runner([]))

    assert result.ok is False
    assert result.details["already_current"] is True


def test_update_engine_does_not_flag_already_current_on_real_failure(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(
        "ciao.package_version.update_package",
        lambda: {"ok": False, "error": "Homebrew 'brew' command not found in PATH."},
    )

    result = macos_service.update_engine(runtime=runtime, runner=_runner([]))

    assert result.ok is False
    assert result.details["already_current"] is False


def _desktop_host_agents(
    tmp_path: Path, *, node_state: object | None = None
) -> Path:
    """A LaunchAgents dir whose `com.ciao.server` runs a live Ciaobot.app."""
    agents = tmp_path / "home" / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    (workspace / ".runtime").mkdir(parents=True)
    (workspace / ".env").write_text("PWA_PORT=9555\n", encoding="utf-8")
    (workspace / ".runtime").mkdir(parents=True, exist_ok=True)
    (workspace / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")
    program = tmp_path / "Ciaobot.app" / "Contents" / "Resources" / "bin" / "ciao"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (agents / "com.ciao.server.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": "com.ciao.server",
                "ProgramArguments": [str(program), "run"],
                "EnvironmentVariables": {"CIAO_WORKSPACE": str(workspace)},
                "WorkingDirectory": str(workspace),
            }
        )
    )
    if node_state is not None:
        text = node_state if isinstance(node_state, str) else json.dumps(node_state)
        (workspace / ".runtime" / "node_state.json").write_text(text, encoding="utf-8")
    return agents


def test_migration_classify_parser_contract() -> None:
    from ciao.cli import build_parser

    args = build_parser().parse_args(
        ["desktop-service", "migration-classify", "--json"]
    )

    assert args.service_action == "migration-classify"
    assert args.deprecated_alias is True
    assert args.as_json is True


@launchd_only
def test_migration_classify_reports_a_live_desktop_host(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # The app asks this before offering the hand-over, so a live host has to
    # come back as `desktop_host` with the workspace the installer will keep.
    from ciao import cli

    agents = _desktop_host_agents(tmp_path)
    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(agents))
    monkeypatch.setattr(sys, "platform", "darwin")

    rc = cli.main(["desktop-service", "migration-classify", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["action"] == "migration-classify"
    assert payload["details"]["kind"] == "desktop_host"
    assert payload["details"]["workspace"] == str(tmp_path / "workspace")
    assert payload["details"]["port"] == 9555


@launchd_only
def test_migration_classify_refuses_to_guess_an_unreadable_state(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # A node_state.json that is not a dict is the case the app must ask about:
    # the two guesses are both expensive, so the bridge reports `desktop_invalid`
    # rather than letting the app infer a role.
    from ciao import cli

    agents = _desktop_host_agents(tmp_path, node_state={"role": "replica"})
    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(agents))
    monkeypatch.setattr(sys, "platform", "darwin")

    rc = cli.main(["desktop-service", "migration-classify", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["details"]["kind"] == "desktop_invalid"
    assert payload["details"]["node_role"] == "invalid"
    assert payload["details"]["host_url"] == ""


def test_migration_classify_names_a_mac_with_nothing_to_migrate(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # No plist at all: the app must show no offer, and the classifier is the
    # only thing that can say so.
    from ciao import cli

    agents = tmp_path / "home" / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(agents))
    monkeypatch.setattr(sys, "platform", "darwin")

    rc = cli.main(["desktop-service", "migration-classify", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["details"]["kind"] == "none"


HOSTED_HOST = "/Applications/Ciaobot Server.app/Contents/MacOS/Ciaobot Server"

# What `launchctl print gui/<uid>/com.ciao.server` renders for a loaded hosted job.
HOSTED_PRINT = f"""gui/501/com.ciao.server = {{
\tactive count = 1
\tpath = /Users/me/Library/LaunchAgents/com.ciao.server.plist
\ttype = LaunchAgent
\tstate = running

\tprogram = {HOSTED_HOST}
\targuments = {{
\t\t{HOSTED_HOST}
\t\tserve
\t\t--python
\t\t/x/bin/python
\t}}

\tworking directory = /Users/me/ws
\tpid = 4242
}}
"""

# What launchctl prints (to stderr, so stdout is empty) for a job that is not loaded.
NOT_LOADED_PRINT = ""


def _write_program_arguments(runtime: macos_service.DesktopRuntime, arguments: list[str]) -> None:
    Path(runtime.server_plist).write_bytes(
        plistlib.dumps({"Label": "com.ciao.server", "ProgramArguments": arguments})
    )


def _launchd_runner(calls: list[list[str]], printed: str, *, print_returncode: int = 0):
    # Answers `launchctl print` with the given text and every other verb with success.
    def run(command, **_kwargs):
        calls.append(list(command))
        if command[1] == "print":
            return subprocess.CompletedProcess(command, print_returncode, stdout=printed, stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    return run


def test_a_revision_one_host_argv_is_not_hosted() -> None:
    # The retired executable name is no longer the hosted shape, so it reads as
    # an ordinary argv: no hosted interpreter, and the service python is argv[0]
    # as it is for any other unrecognised command.
    argv = [
        "/Applications/Ciaobot Server.app/Contents/MacOS/CiaobotServerHost",
        "serve",
        "--python",
        "/x/python",
    ]
    assert macos_service.hosted_service_python(argv) is None
    assert macos_service.service_python_path(argv) == argv[0]


def test_parse_launchctl_print_arguments_reads_the_hosted_argv() -> None:
    parsed = macos_service.parse_launchctl_print_arguments(HOSTED_PRINT)

    assert parsed == [HOSTED_HOST, "serve", "--python", "/x/bin/python"]
    assert macos_service.service_python_path(parsed) == "/x/bin/python"


def test_parse_launchctl_print_arguments_is_none_when_not_loaded() -> None:
    assert macos_service.parse_launchctl_print_arguments(NOT_LOADED_PRINT) is None
    # A job launchd lists without an arguments block is not read as one.
    assert (
        macos_service.parse_launchctl_print_arguments(
            "gui/501/com.ciao.server = {\n\tstate = waiting\n}\n"
        )
        is None
    )


def test_status_reports_a_loaded_definition_that_matches_the_plist(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/x/bin/python"])
    monkeypatch.setattr(macos_service, "server_reachable", lambda _port: False)

    result = macos_service.service_status(
        runtime=runtime, uid=501, runner=_launchd_runner([], HOSTED_PRINT)
    )

    assert result.details["loaded"] is True
    assert result.details["loaded_python_path"] == "/x/bin/python"
    assert result.details["plist_matches_loaded"] is True
    assert "Warning" not in result.message


def test_status_warns_when_the_plist_differs_from_the_loaded_definition(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = dataclasses.replace(_runtime(tmp_path), python_path="/new/bin/python")
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/new/bin/python"])
    monkeypatch.setattr(macos_service, "server_reachable", lambda _port: False)

    result = macos_service.service_status(
        runtime=runtime, uid=501, runner=_launchd_runner([], HOSTED_PRINT)
    )

    assert result.details["loaded_python_path"] == "/x/bin/python"
    assert result.details["plist_matches_loaded"] is False
    # python_path keeps reading the plist on disk; only the new fields follow launchd.
    assert result.details["python_path"] == "/new/bin/python"
    assert "Warning" in result.message
    assert "ciao service restart" in result.message


def test_status_leaves_the_match_unknown_when_not_loaded(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/x/bin/python"])
    monkeypatch.setattr(macos_service, "server_reachable", lambda _port: False)

    result = macos_service.service_status(
        runtime=runtime,
        uid=501,
        runner=_launchd_runner([], NOT_LOADED_PRINT, print_returncode=113),
    )

    assert result.details["loaded"] is False
    assert result.details["loaded_python_path"] is None
    assert result.details["plist_matches_loaded"] is None
    assert "Warning" not in result.message


GONE_PRINT = 'Could not find service "com.ciao.server" in domain for port'


def _teardown_runner(
    calls: list[list[str]], *, running_polls: int = 0, print_forever_running: bool = False
):
    """Answers `print` as running until a bootout, then as gone after `running_polls` polls.

    Every verb other than `print` succeeds. Records each post-bootout print in `calls`.
    """
    state = {"booted_out": False, "polls_left": running_polls}

    def run(command, **_kwargs):
        calls.append(list(command))
        if command[1] == "bootout":
            state["booted_out"] = True
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1] == "print":
            if not state["booted_out"]:
                return subprocess.CompletedProcess(command, 0, stdout=HOSTED_PRINT, stderr="")
            if not print_forever_running and state["polls_left"] == 0:
                return subprocess.CompletedProcess(command, 113, stdout="", stderr=GONE_PRINT)
            state["polls_left"] -= 1
            return subprocess.CompletedProcess(command, 0, stdout=HOSTED_PRINT, stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    return run


def test_restart_reloads_the_definition_when_the_plist_was_edited(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/new/bin/python"])
    monkeypatch.setattr(macos_service, "restart_blockers", lambda _port: [])
    monkeypatch.delenv("CIAO_CHAT_ID", raising=False)
    monkeypatch.setattr(macos_service, "_sleep", lambda _s: None)
    calls: list[list[str]] = []

    result = macos_service.restart_service(
        runtime=runtime, uid=501, runner=_teardown_runner(calls)
    )

    assert result.ok is True
    assert result.details["restart"] == "reload"
    assert "reloaded the edited service definition" in result.message
    assert calls == [
        ["launchctl", "print", "gui/501/com.ciao.server"],
        ["launchctl", "bootout", "gui/501/com.ciao.server"],
        ["launchctl", "print", "gui/501/com.ciao.server"],
        ["launchctl", "bootstrap", "gui/501", runtime.server_plist],
    ]


def test_reload_waits_for_launchd_to_forget_the_job_before_bootstrap(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/new/bin/python"])
    monkeypatch.setattr(macos_service, "restart_blockers", lambda _port: [])
    monkeypatch.delenv("CIAO_CHAT_ID", raising=False)
    sleeps: list[float] = []
    monkeypatch.setattr(macos_service, "_sleep", sleeps.append)
    calls: list[list[str]] = []

    result = macos_service.restart_service(
        runtime=runtime, uid=501, runner=_teardown_runner(calls, running_polls=3)
    )

    assert result.ok is True
    # Three polls still see the stopping job (sleeping 0.5 s between polls), the fourth does not.
    assert sleeps == [0.5] * 3
    verbs = [call[1] for call in calls]
    assert verbs == ["print", "bootout", "print", "print", "print", "print", "bootstrap"]


def test_reload_times_out_without_bootstrapping_a_half_dead_job(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/new/bin/python"])
    monkeypatch.setattr(macos_service, "restart_blockers", lambda _port: [])
    monkeypatch.delenv("CIAO_CHAT_ID", raising=False)
    sleeps: list[float] = []
    monkeypatch.setattr(macos_service, "_sleep", sleeps.append)
    calls: list[list[str]] = []

    result = macos_service.restart_service(
        runtime=runtime,
        uid=501,
        runner=_teardown_runner(calls, print_forever_running=True),
    )

    assert result.ok is False
    assert "did not stop within 45 s" in result.message
    assert result.details["teardown_timed_out"] is True
    assert not any("bootstrap" in call for call in calls)
    assert len(sleeps) == int(macos_service.RELOAD_TEARDOWN_TIMEOUT_S / macos_service.RELOAD_POLL_INTERVAL_S)


def test_reload_is_refused_from_inside_a_chat_shell(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/new/bin/python"])
    monkeypatch.setattr(macos_service, "restart_blockers", lambda _port: [])
    monkeypatch.setenv("CIAO_CHAT_ID", "chat-1")
    calls: list[list[str]] = []

    result = macos_service.restart_service(
        runtime=runtime, uid=501, runner=_launchd_runner(calls, HOSTED_PRINT)
    )

    assert result.ok is False
    assert result.details["refused"] is True
    assert "ciao service restart" in result.message
    assert "Settings → Restart" in result.message
    assert calls == [["launchctl", "print", "gui/501/com.ciao.server"]]


def test_kickstart_still_restarts_from_inside_a_chat_shell(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/x/bin/python"])
    monkeypatch.setattr(macos_service, "restart_blockers", lambda _port: [])
    monkeypatch.setenv("CIAO_CHAT_ID", "chat-1")
    calls: list[list[str]] = []

    result = macos_service.restart_service(
        runtime=runtime, uid=501, runner=_launchd_runner(calls, HOSTED_PRINT)
    )

    assert result.ok is True
    assert result.details["restart"] == "kickstart"
    assert calls[-1] == ["launchctl", "kickstart", "-k", "gui/501/com.ciao.server"]


def test_restart_kickstarts_when_the_loaded_definition_matches(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/x/bin/python"])
    monkeypatch.setattr(macos_service, "restart_blockers", lambda _port: [])
    calls: list[list[str]] = []

    result = macos_service.restart_service(
        runtime=runtime, uid=501, runner=_launchd_runner(calls, HOSTED_PRINT)
    )

    assert result.ok is True
    assert result.details["restart"] == "kickstart"
    assert result.message == "Ciaobot engine restarted."
    assert calls == [
        ["launchctl", "print", "gui/501/com.ciao.server"],
        ["launchctl", "kickstart", "-k", "gui/501/com.ciao.server"],
    ]


def test_restart_kickstarts_when_the_job_is_not_loaded(tmp_path: Path, monkeypatch) -> None:
    # Nothing is loaded to compare against, so restart keeps today's kickstart.
    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/x/bin/python"])
    monkeypatch.setattr(macos_service, "restart_blockers", lambda _port: [])
    calls: list[list[str]] = []

    macos_service.restart_service(
        runtime=runtime,
        uid=501,
        runner=_launchd_runner(calls, NOT_LOADED_PRINT, print_returncode=113),
    )

    assert calls[-1] == ["launchctl", "kickstart", "-k", "gui/501/com.ciao.server"]
    assert not any("bootstrap" in call for call in calls)


def test_load_server_plist_reads_the_server_agent(tmp_path: Path) -> None:
    with (tmp_path / "com.ciao.server.plist").open("wb") as handle:
        plistlib.dump({"ProgramArguments": ["/x", "serve"]}, handle)

    assert macos_service.load_server_plist(tmp_path)["ProgramArguments"] == ["/x", "serve"]


@pytest.mark.parametrize(
    "content",
    [
        None,
        b"garbage",
        b'<?xml version="1.0"?><plist><dict><key>a</key>',
        plistlib.dumps(["a"]),
    ],
    ids=["missing", "garbage", "truncated-xml", "non-dict-root"],
)
def test_load_server_plist_answers_none_for_missing_malformed_truncated_and_non_dict(
    tmp_path: Path, content: bytes | None
) -> None:
    if content is not None:
        (tmp_path / "com.ciao.server.plist").write_bytes(content)

    assert macos_service.load_server_plist(tmp_path) is None


def test_load_server_plist_defaults_to_the_isolated_launch_agents_dir() -> None:
    agents = macos_service.default_launch_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    with (agents / "com.ciao.server.plist").open("wb") as handle:
        plistlib.dump({"ProgramArguments": ["/x", "serve"]}, handle)

    assert macos_service.load_server_plist() == {"ProgramArguments": ["/x", "serve"]}


def test_gate_refuses_when_blockers_cannot_be_read(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)

    def unreadable(_port: int) -> list[dict[str, object]]:
        raise macos_service.RestartBlockersUnavailable("the engine answered HTTP 404")

    monkeypatch.setattr(macos_service, "restart_blockers", unreadable)
    monkeypatch.setattr(macos_service, "server_reachable", lambda _port, **_kw: True)
    # The forced stop walks the launchd path, which needs a uid (absent on Windows).
    monkeypatch.setattr(macos_service, "_getuid", lambda: 501)
    calls: list[list[str]] = []

    refused = macos_service.stop_service(runtime=runtime, runner=_runner(calls))

    assert refused.ok is False
    assert refused.details["restart_blockers"] is None
    assert refused.details["requires_confirmation"] is True
    assert "--force" in refused.message
    assert calls == []

    forced = macos_service.stop_service(runtime=runtime, force=True, runner=_runner(calls))

    assert forced.details.get("requires_confirmation") is not True
    assert calls


def test_restart_blockers_returns_empty_when_engine_is_down(monkeypatch) -> None:
    import urllib.error

    def refuse(*_args, **_kwargs):
        raise urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(macos_service.urllib.request, "urlopen", refuse)

    assert macos_service.restart_blockers(8543) == []


def test_restart_blockers_raises_on_http_error(monkeypatch) -> None:
    import urllib.error

    def not_found(*_args, **_kwargs):
        raise urllib.error.HTTPError("http://x", 404, "nf", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(macos_service.urllib.request, "urlopen", not_found)

    with pytest.raises(macos_service.RestartBlockersUnavailable, match="404"):
        macos_service.restart_blockers(8543)


def test_status_reports_restart_blockers(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    blockers = [{"chat_id": "chat-1", "reasons": ["open_question"]}]
    monkeypatch.setattr(macos_service, "server_reachable", lambda _port: True)
    monkeypatch.setattr(macos_service, "restart_blockers", lambda _port: blockers)

    result = macos_service.service_status(runtime=runtime, uid=501, runner=_launchd_runner([], ""))

    assert result.details["restart_blockers"] == blockers
    assert result.message.endswith("1 chat(s) would be interrupted by a restart.")


def test_gate_lets_a_stopped_engine_through_when_blockers_cannot_be_read(tmp_path: Path, monkeypatch) -> None:
    def unreadable(_port: int) -> list[dict[str, object]]:
        raise macos_service.RestartBlockersUnavailable("timed out")

    monkeypatch.setattr(macos_service, "restart_blockers", unreadable)
    monkeypatch.setattr(macos_service, "server_reachable", lambda _port, **_kw: False)

    assert macos_service.restart_gate(8443, "restart", force=False) is None


def test_status_reports_a_workspace_access_denial(tmp_path: Path, monkeypatch) -> None:
    from ciao import workspace_access

    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/x/bin/python"])
    line = "macOS denied Ciaobot Server access to /w: allow it in System Settings."
    record = workspace_access.denial_path()
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(line + "\n", encoding="utf-8")
    runner = _launchd_runner([], NOT_LOADED_PRINT, print_returncode=113)

    monkeypatch.setattr(macos_service, "server_reachable", lambda _port: False)
    down = macos_service.service_status(runtime=runtime, uid=501, runner=runner)

    assert down.message.startswith("Ciaobot engine is not running: macOS denied")
    assert down.details["workspace_access_denied"] == line

    monkeypatch.setattr(macos_service, "server_reachable", lambda _port: True)
    monkeypatch.setattr(macos_service, "restart_blockers", lambda _port: [])
    up = macos_service.service_status(runtime=runtime, uid=501, runner=runner)

    assert up.message == "Ciaobot engine is running."
    assert up.details["workspace_access_denied"] is None


# ── the port the engine binds is taken (#1654) ────────────────────────────


def _launchd_print_with_exit_code(code: int) -> str:
    """A `launchctl print` block that reports a last exit code."""
    return (
        "gui/501/com.ciao.server = {\n"
        "\tstate = not running\n"
        "\tlast exit code = %d\n"
        "}\n" % code
    )


def test_the_last_exit_code_is_read_out_of_the_launchd_print(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/x/bin/python"])
    monkeypatch.setattr(macos_service, "server_reachable", lambda _port: False)

    result = macos_service.service_status(
        runtime=runtime,
        uid=501,
        runner=_launchd_runner([], _launchd_print_with_exit_code(0)),
    )

    assert result.details["last_exit_code"] == 0


def test_launchd_exit_observation_reports_access_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(macos_service, "_getuid", lambda: 501)
    monkeypatch.setattr(macos_service.os, "getuid", lambda: 502, raising=False)
    commands: list[list[str]] = []

    def launchctl(args, runner):
        commands.append(list(args))
        return subprocess.CompletedProcess(
            [], 0, stdout=_launchd_print_with_exit_code(78), stderr=""
        )

    monkeypatch.setattr(
        macos_service,
        "_launchctl",
        launchctl,
    )

    assert macos_service.server_last_exit_code() == 78
    assert commands == [["print", "gui/501/com.ciao.server"]]


def test_status_names_the_port_when_the_engine_could_not_bind(
    tmp_path: Path, monkeypatch
) -> None:
    """`ciao service status` has to say *why* the engine is not running.

    The engine leaves with the port-in-use exit code when it could not bind
    its port, so the job's last exit code is that code — and the status has
    to turn it back into the sentence the operator needs: which port, and
    what to do about it.
    """
    from ciao.port_in_use import PORT_IN_USE_EXIT_CODE

    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/x/bin/python"])
    monkeypatch.setattr(macos_service, "server_reachable", lambda _port: False)

    result = macos_service.service_status(
        runtime=runtime,
        uid=501,
        runner=_launchd_runner([], _launchd_print_with_exit_code(PORT_IN_USE_EXIT_CODE)),
    )

    assert result.details["port_in_use"] is True
    assert str(runtime.port) in result.message
    assert "already in use" in result.message
    assert "PWA_PORT" in result.message


def test_status_says_nothing_about_the_port_for_an_ordinary_exit(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/x/bin/python"])
    monkeypatch.setattr(macos_service, "server_reachable", lambda _port: False)

    result = macos_service.service_status(
        runtime=runtime,
        uid=501,
        runner=_launchd_runner([], _launchd_print_with_exit_code(1)),
    )

    assert result.details["port_in_use"] is False
    assert "PWA_PORT" not in result.message


def test_status_says_nothing_about_the_port_when_the_exit_code_is_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    """A job that was never run has no last exit code, and no verdict."""
    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/x/bin/python"])
    monkeypatch.setattr(macos_service, "server_reachable", lambda _port: False)

    result = macos_service.service_status(
        runtime=runtime,
        uid=501,
        runner=_launchd_runner([], NOT_LOADED_PRINT, print_returncode=113),
    )

    assert result.details["last_exit_code"] is None
    assert result.details["port_in_use"] is False


def test_status_names_the_port_from_the_record_the_engine_left(
    tmp_path: Path, monkeypatch
) -> None:
    """The record is the truth, because the job is being held.

    The supervisor holds the job after a bind failure rather than letting
    the service manager respawn it, so the job's last exit code is stale:
    the reason is what the engine recorded.
    """
    from ciao import port_in_use

    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/x/bin/python"])
    monkeypatch.setattr(macos_service, "server_reachable", lambda _port: False)
    port_in_use.record_port_in_use(
        port_in_use.port_in_use_message(runtime.port, Path(runtime.workspace))
    )

    result = macos_service.service_status(
        runtime=runtime,
        uid=501,
        runner=_launchd_runner([], _launchd_print_with_exit_code(0)),
    )

    assert result.details["port_in_use"] is True
    assert str(runtime.port) in result.message
    assert "already in use" in result.message
    assert "PWA_PORT" in result.message
    port_in_use.clear_port_in_use()


def test_start_service_reports_the_port_the_engine_could_not_bind(
    tmp_path: Path,
) -> None:
    """`ciao service start` names the port instead of a bare failure.

    The job was started and the engine died on the bind, so the job's last
    exit code is the port-in-use one. The operator has to be told which port
    and how to move it, not that "launchctl start failed".
    """
    from ciao.port_in_use import PORT_IN_USE_EXIT_CODE

    runtime = _runtime(tmp_path)
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(list(command))
        printed = (
            _launchd_print_with_exit_code(PORT_IN_USE_EXIT_CODE)
            if command[1] == "print"
            else ""
        )
        return subprocess.CompletedProcess(command, 0, stdout=printed, stderr="")

    result = macos_service.start_service(runtime=runtime, uid=501, runner=run)

    assert result.ok is False
    assert result.details["port_in_use"] is True
    assert str(runtime.port) in result.message
    assert "already in use" in result.message
    assert "PWA_PORT" in result.message


def test_start_service_succeeds_when_the_engine_did_not_fail_on_the_port(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(list(command))
        printed = (
            _launchd_print_with_exit_code(0)
            if command[1] == "print"
            else ""
        )
        return subprocess.CompletedProcess(command, 0, stdout=printed, stderr="")

    result = macos_service.start_service(runtime=runtime, uid=501, runner=run)

    assert result.ok is True
    assert result.message == "Ciaobot engine started."
    assert "PWA_PORT" not in result.message



# ── the CLI waits for the engine it started, and for no other (#1938) ─────


class _Clock:
    """A clock the test advances: `sleep` moves it, nothing waits for real."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _lock_owner(desktop: macos_service.DesktopRuntime, startup_id: str, **over: object) -> dict:
    """Lock metadata as the engine writes it; ``over`` overrides any field,
    the ``runtime`` directory included."""
    return {
        "pid": 4242,
        "port": desktop.port,
        "workspace": desktop.workspace,
        "runtime": desktop.runtime_root,
        "started_at": "2026-10-23T15:14:55Z",
        "status": "running",
        "startup_id": startup_id,
        **over,
    }


def _write_lock(runtime: macos_service.DesktopRuntime, owner: dict) -> None:
    (Path(runtime.runtime_root) / macos_service.SERVER_LOCK_NAME).write_text(
        json.dumps(owner), encoding="utf-8"
    )


def _quiet_records(monkeypatch, tmp_path: Path) -> None:
    """No bind failure and no denial recorded anywhere this test can see."""
    monkeypatch.setattr(macos_service, "read_port_in_use", lambda: "")
    monkeypatch.setattr(macos_service, "denial_path", lambda: tmp_path / "no-denial.txt")


def test_read_lock_owner_answers_none_for_missing_malformed_and_non_object(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    assert macos_service.read_lock_owner(runtime) is None
    lock = Path(runtime.runtime_root) / macos_service.SERVER_LOCK_NAME
    lock.write_text("{not json", encoding="utf-8")
    assert macos_service.read_lock_owner(runtime) is None
    lock.write_text("[1, 2]", encoding="utf-8")
    assert macos_service.read_lock_owner(runtime) is None
    _write_lock(runtime, _lock_owner(runtime, "run-1"))
    assert macos_service.read_lock_owner(runtime)["startup_id"] == "run-1"


def test_start_waits_past_two_seconds_for_its_own_engine(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    _quiet_records(monkeypatch, tmp_path)
    clock = _Clock()
    start = clock.now
    _write_lock(runtime, _lock_owner(runtime, "run-old", status="stopped"))

    # The engine takes 4.5 s to take its lock and answer — longer than the
    # 2 s the old wait allowed (measured: 4.1 s to first answer, #1938).
    def identity(port: int, *, timeout: float):
        assert port == runtime.port
        assert 0.1 <= timeout <= macos_service.START_PROBE_TIMEOUT_S
        if clock.now - start < 4.5:
            return None
        _write_lock(runtime, _lock_owner(runtime, "run-new"))
        return {"startup_id": "run-new", "pid": 4242, "overall_ready": False}

    original_fresh = macos_service._fresh_lock_owner

    def fresh(rt, previous_id):
        # The lock lands with the first answer in this scenario.
        if clock.now - start >= 4.5:
            _write_lock(runtime, _lock_owner(runtime, "run-new"))
        return original_fresh(rt, previous_id)

    monkeypatch.setattr(macos_service, "engine_identity", identity)
    monkeypatch.setattr(macos_service, "_fresh_lock_owner", fresh)

    outcome = macos_service.wait_for_engine(
        runtime,
        previous={"startup_id": "run-old", "status": "stopped"},
        started_at=0.0,
        clock=clock,
        sleep=clock.sleep,
    )

    assert outcome.ok is True
    assert outcome.state == "started"
    assert outcome.startup_id == "run-new"
    assert outcome.pid == 4242
    assert clock.now - start >= 4.5
    assert clock.now - start < macos_service.START_SETTLE_TIMEOUT_S


def test_start_rejects_an_unrelated_startup_status_listener(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    _quiet_records(monkeypatch, tmp_path)
    clock = _Clock()
    _write_lock(runtime, _lock_owner(runtime, "run-new"))
    # Something answers the port with a 2xx and no identity: a bare JSON
    # object, which is what satisfied the old wait.
    monkeypatch.setattr(macos_service, "engine_identity", lambda _port, **_kw: None)

    outcome = macos_service.wait_for_engine(
        runtime, previous=None, started_at=0.0, clock=clock, sleep=clock.sleep,
    )

    assert outcome.ok is False
    assert outcome.state == "timeout"


def test_engine_identity_ignores_a_2xx_without_a_startup_id(monkeypatch) -> None:
    import io

    class _Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    answers = iter([b"ok", b'{"overall_ready": true}', b'{"startup_id": "", "pid": 1}', b'{"startup_id": "run-1", "pid": 7}'])
    monkeypatch.setattr(
        macos_service.urllib.request, "urlopen", lambda *_a, **_kw: _Response(next(answers))
    )

    assert macos_service.engine_identity(9443) is None
    assert macos_service.engine_identity(9443) is None
    assert macos_service.engine_identity(9443) is None
    assert macos_service.engine_identity(9443) == {"startup_id": "run-1", "pid": 7}


def test_start_rejects_the_previous_engine_identity(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    _quiet_records(monkeypatch, tmp_path)
    clock = _Clock()
    # The run from before the kickstart is still the lock owner and still
    # answers the port as itself: that is the old engine, not a new start.
    previous = _lock_owner(runtime, "run-old")
    _write_lock(runtime, previous)
    monkeypatch.setattr(
        macos_service, "engine_identity", lambda _port, **_kw: {"startup_id": "run-old", "pid": 1}
    )

    outcome = macos_service.wait_for_engine(
        runtime, previous=previous, started_at=0.0, clock=clock, sleep=clock.sleep,
    )

    assert outcome.state == "timeout"


def test_start_rejects_another_runtime_and_a_mismatched_port(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    _quiet_records(monkeypatch, tmp_path)
    monkeypatch.setattr(
        macos_service, "engine_identity", lambda _port, **_kw: {"startup_id": "run-x", "pid": 1}
    )
    other = tmp_path / "other-runtime"
    other.mkdir()

    _write_lock(runtime, _lock_owner(runtime, "run-x", runtime=str(other)))
    assert macos_service._fresh_lock_owner(runtime, None) is None
    _write_lock(runtime, _lock_owner(runtime, "run-x", port=runtime.port + 1))
    assert macos_service._fresh_lock_owner(runtime, None) is None
    _write_lock(runtime, _lock_owner(runtime, "run-x", status="stopped"))
    assert macos_service._fresh_lock_owner(runtime, None) is None
    _write_lock(runtime, _lock_owner(runtime, "run-x"))
    assert macos_service._fresh_lock_owner(runtime, None)["startup_id"] == "run-x"


def test_start_reports_a_delayed_bind_failure(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(macos_service, "denial_path", lambda: tmp_path / "no-denial.txt")
    clock = _Clock()
    start = clock.now
    # The record lands 3 s in — after the old 2 s deadline had already
    # reported "started".
    monkeypatch.setattr(
        macos_service,
        "read_port_in_use",
        lambda: "port 9443 is in use by another process" if clock.now - start >= 3 else "",
    )
    monkeypatch.setattr(macos_service, "engine_identity", lambda _port, **_kw: None)

    outcome = macos_service.wait_for_engine(
        runtime, previous=None, started_at=0.0, clock=clock, sleep=clock.sleep,
    )

    assert outcome.state == "port_in_use"
    assert outcome.ok is False
    assert "in use" in outcome.message
    assert clock.now - start < macos_service.START_SETTLE_TIMEOUT_S


def test_start_timeout_is_not_success(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    _quiet_records(monkeypatch, tmp_path)
    clock = _Clock()
    start = clock.now
    monkeypatch.setattr(macos_service, "engine_identity", lambda _port, **_kw: None)

    outcome = macos_service.wait_for_engine(
        runtime, previous=None, started_at=0.0, clock=clock, sleep=clock.sleep,
    )

    assert outcome.ok is False
    assert outcome.state == "timeout"
    assert outcome.startup_id is None
    assert "did not confirm" in outcome.message
    assert "ciao service status" in outcome.message
    assert clock.now - start >= macos_service.START_SETTLE_TIMEOUT_S
    # Bounded: the wait polled, it did not spin.
    assert all(step == macos_service.START_POLL_INTERVAL_S for step in clock.slept)


def test_start_reports_workspace_access_denial(tmp_path: Path, monkeypatch) -> None:
    import os as _os

    runtime = _runtime(tmp_path)
    monkeypatch.setattr(macos_service, "read_port_in_use", lambda: "")
    monkeypatch.setattr(macos_service, "engine_identity", lambda _port, **_kw: None)
    record = tmp_path / "workspace-access-denied.txt"
    monkeypatch.setattr(macos_service, "denial_path", lambda: record)
    line = "macOS denied Ciaobot Server access to /w: allow it in System Settings."
    clock = _Clock()

    # A denial left over from an earlier run is not this start's verdict.
    record.write_text(line + "\n", encoding="utf-8")
    stale = time.time() - 600
    _os.utime(record, (stale, stale))
    outcome = macos_service.wait_for_engine(
        runtime, previous=None, started_at=time.time(), clock=clock, sleep=clock.sleep,
    )
    assert outcome.state == "timeout"

    # One recorded after the kickstart is.
    started_at = time.time() - 5
    record.write_text(line + "\n", encoding="utf-8")
    outcome = macos_service.wait_for_engine(
        runtime, previous=None, started_at=started_at, clock=_Clock(), sleep=lambda _s: None,
    )
    assert outcome.state == "denied"
    assert outcome.message == line


def test_cli_wait_does_not_make_worker_start_submission_blocking(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    calls: list[list[str]] = []

    def never(*_a, **_kw):
        raise AssertionError("start_service must not wait for the engine")

    monkeypatch.setattr(macos_service, "wait_for_engine", never)
    monkeypatch.setattr(macos_service, "engine_identity", never)

    result = macos_service.start_service(runtime=runtime, uid=501, runner=_runner(calls))

    assert result.ok is True
    assert [c[1] for c in calls[:3]] == ["enable", "bootstrap", "kickstart"]


def test_cli_start_reports_an_unconfirmed_start_as_a_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    from ciao import cli

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".env").write_text("PWA_PORT=9555\n", encoding="utf-8")
    (workspace / ".runtime").mkdir(parents=True, exist_ok=True)
    (workspace / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "darwin")
    # The forced darwin branch builds the launchd domain from the uid, which
    # os.getuid does not provide on Windows; the CLI contract under test is
    # the same there.
    monkeypatch.setattr(macos_service, "_getuid", lambda: 501)
    monkeypatch.setattr(
        macos_service,
        "_launchctl",
        lambda args, runner=None: subprocess.CompletedProcess(["launchctl", *args], 0, "", ""),
    )
    seen: dict[str, object] = {}

    def wait(rt, **kw):
        seen.update(kw)
        return macos_service.StartOutcome("timeout", "The engine did not confirm its start within 30s.")

    monkeypatch.setattr(macos_service, "wait_for_engine", wait)

    rc = cli.main(["service", "start", "--workspace", str(workspace), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["details"]["verified"] is False
    assert payload["details"]["start_outcome"] == "timeout"
    assert payload["details"]["port_in_use"] is False
    # The previous identity was read before the kickstart (none here) and
    # the kickstart time is what a denial is measured against.
    assert seen["previous"] is None
    assert isinstance(seen["started_at"], float)


def test_cli_start_reads_the_launchd_exit_code_after_a_timeout(tmp_path: Path, monkeypatch, capsys) -> None:
    from ciao import cli

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".env").write_text("PWA_PORT=9555\n", encoding="utf-8")
    (workspace / ".runtime").mkdir(parents=True, exist_ok=True)
    (workspace / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(macos_service, "_getuid", lambda: 501)
    monkeypatch.setattr(
        macos_service,
        "_launchctl",
        lambda args, runner=None: subprocess.CompletedProcess(["launchctl", *args], 0, "", ""),
    )
    monkeypatch.setattr(
        macos_service,
        "wait_for_engine",
        lambda _rt, **_kw: macos_service.StartOutcome("timeout", "The engine did not confirm its start within 30s."),
    )
    # The engine ran unsupervised and exited on the port-in-use code; launchd
    # remembers that even though the engine left no record of its own. The
    # submission's own read, right after the kickstart, is still too early to
    # see it; the read after the deadline is not.
    reasons = iter([None, "port 9555 is in use by pid 77"])
    monkeypatch.setattr(macos_service, "port_in_use_reason", lambda _rt, **_kw: next(reasons))

    rc = cli.main(["service", "start", "--workspace", str(workspace), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["details"]["start_outcome"] == "port_in_use"
    assert payload["details"]["port_in_use"] is True
    assert "in use" in payload["message"]


def test_cli_start_reports_the_verified_identity(tmp_path: Path, monkeypatch, capsys) -> None:
    from ciao import cli

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".env").write_text("PWA_PORT=9555\n", encoding="utf-8")
    (workspace / ".runtime").mkdir(parents=True, exist_ok=True)
    (workspace / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(macos_service, "_getuid", lambda: 501)
    monkeypatch.setattr(
        macos_service,
        "_launchctl",
        lambda args, runner=None: subprocess.CompletedProcess(["launchctl", *args], 0, "", ""),
    )
    _engine_starts(monkeypatch, startup_id="run-7")

    rc = cli.main(["service", "start", "--workspace", str(workspace), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["details"]["verified"] is True
    assert payload["details"]["startup_id"] == "run-7"
    assert payload["details"]["pid"] == 4242
