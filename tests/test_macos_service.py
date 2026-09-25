from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from ciao import macos_service


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
    (workspace / ".env").write_text(
        "PWA_PORT=9555\nCIAO_RUNTIME_ROOT=var/runtime\n",
        encoding="utf-8",
    )
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.ciao.server.plist").write_bytes(
        plistlib.dumps(
            {
                "WorkingDirectory": str(workspace),
                "EnvironmentVariables": {"CIAO_PORT": "8443"},
                "ProgramArguments": ["/stable/python", "-m", "ciao.cli", "run"],
            }
        )
    )

    runtime = macos_service.discover_runtime(launch_agents_dir=agents, environ={})

    assert runtime.workspace == str(workspace.resolve())
    assert runtime.port == 9555
    assert runtime.runtime_root == str((workspace / "var/runtime").resolve())
    assert runtime.python_path == "/stable/python"


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
    monkeypatch.setattr(macos_service, "_active_chat_ids", lambda _port: ["chat-1"])
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
    monkeypatch.setattr(macos_service, "_active_chat_ids", lambda _port: ["chat-1"])
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
    monkeypatch.setattr(macos_service, "_server_reachable", lambda _port: True)

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
    monkeypatch.setattr(macos_service, "_server_reachable", lambda _port: True)

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


def test_service_start_registers_missing_launch_agent(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from ciao import cli

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".env").write_text("PWA_PORT=9555\n", encoding="utf-8")
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


def test_service_start_rejects_directory_without_env(
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
    assert "no .env" in payload["message"]
    assert not plist_path.exists()
    assert calls == []


def test_service_start_rejects_source_checkout(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from ciao import cli

    workspace = tmp_path / "ws"
    (workspace / "ciao").mkdir(parents=True)
    (workspace / ".env").write_text("PWA_PORT=9555\n", encoding="utf-8")
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


def test_service_start_register_honors_runtime_root_and_engine_path(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from ciao import cli

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "PWA_PORT=9555\nCIAO_RUNTIME_ROOT=rt\n", encoding="utf-8"
    )
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
        == str((workspace / "rt").resolve())
    )
    assert plist_data["ProgramArguments"][0] == str(engine)
    assert "-m" not in plist_data["ProgramArguments"]


def test_service_start_refuses_workspace_mismatch(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from ciao import cli

    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    for workspace in (ws_a, ws_b):
        workspace.mkdir()
        (workspace / ".env").write_text("PWA_PORT=9555\n", encoding="utf-8")
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
    monkeypatch.setattr(macos_service, "_active_chat_ids", lambda _port: ["chat-1"])
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
