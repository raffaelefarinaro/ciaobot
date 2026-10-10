from __future__ import annotations

import dataclasses
import json
import os
import plistlib
import subprocess
import sys
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
    assert calls == [
        ["launchctl", "enable", "gui/501/com.ciao.server"],
        ["launchctl", "bootstrap", "gui/501", runtime.server_plist],
        ["launchctl", "kickstart", "-k", "gui/501/com.ciao.server"],
    ]


def test_restart_and_stop_require_confirmation_for_active_chats(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(macos_service, "active_chat_ids", lambda _port: ["chat-1"])
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
    assert stop.details["active_chat_ids"] == ["chat-1"]
    assert calls == []


def test_force_stop_boots_out_server(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(macos_service, "active_chat_ids", lambda _port: ["chat-1"])
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
    assert not plist_path.exists()
    assert calls == []


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
    monkeypatch.setattr(macos_service, "active_chat_ids", lambda _port: ["chat-1"])
    monkeypatch.setattr(
        "ciao.package_version.update_package",
        lambda: (_ for _ in ()).throw(AssertionError("upgrade must not start")),
    )

    result = macos_service.update_engine(runtime=runtime, runner=_runner([]))

    assert result.ok is False
    assert result.details["requires_confirmation"] is True
    assert result.details["active_chat_ids"] == ["chat-1"]


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


HOSTED_HOST = "/Applications/Ciaobot Server.app/Contents/MacOS/CiaobotServerHost"

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


def test_restart_reloads_the_definition_when_the_plist_was_edited(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/new/bin/python"])
    monkeypatch.setattr(macos_service, "active_chat_ids", lambda _port: [])
    calls: list[list[str]] = []

    result = macos_service.restart_service(
        runtime=runtime, uid=501, runner=_launchd_runner(calls, HOSTED_PRINT)
    )

    assert result.ok is True
    assert result.details["restart"] == "reload"
    assert "reloaded the edited service definition" in result.message
    assert calls == [
        ["launchctl", "print", "gui/501/com.ciao.server"],
        ["launchctl", "bootout", "gui/501/com.ciao.server"],
        ["launchctl", "bootstrap", "gui/501", runtime.server_plist],
    ]


def test_restart_kickstarts_when_the_loaded_definition_matches(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    _write_program_arguments(runtime, [HOSTED_HOST, "serve", "--python", "/x/bin/python"])
    monkeypatch.setattr(macos_service, "active_chat_ids", lambda _port: [])
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
    monkeypatch.setattr(macos_service, "active_chat_ids", lambda _port: [])
    calls: list[list[str]] = []

    macos_service.restart_service(
        runtime=runtime,
        uid=501,
        runner=_launchd_runner(calls, NOT_LOADED_PRINT, print_returncode=113),
    )

    assert calls[-1] == ["launchctl", "kickstart", "-k", "gui/501/com.ciao.server"]
    assert not any("bootstrap" in call for call in calls)
