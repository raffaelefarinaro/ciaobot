from __future__ import annotations

import json
import plistlib
import subprocess
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

    assert restart.desktop_service_action == "restart"
    assert restart.force is True
    assert restart.as_json is True
    assert update.desktop_service_action == "update-engine"
    assert update.force is True
    assert login.login_action == "disable"
    assert migrate.app_bundle == Path("/Applications/Ciaobot.app")


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
