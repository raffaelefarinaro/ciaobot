from __future__ import annotations

import json
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from ciao import cli


def test_cli_run_dispatches_server(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli, "_run_server", lambda: called.append("run") or 0)

    assert cli.main(["run"]) == 0
    assert called == ["run"]


def _raise_system_exit(code: int):
    def _main() -> None:
        raise SystemExit(code)

    return _main


def test_run_relaunches_on_restart_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A foreground `ciao run` must survive the setup/update restart exit:
    the CLI re-execs itself instead of dying."""
    import ciao.main

    execs: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(ciao.main, "main", _raise_system_exit(75))
    monkeypatch.setattr(cli.os, "execv", lambda exe, argv: execs.append((exe, argv)))
    monkeypatch.delenv("CIAO_RESTART_EXIT_CODE", raising=False)

    assert cli._run_server() == 75

    assert execs == [(cli.sys.executable, [cli.sys.executable, "-m", "ciao.cli", *cli.sys.argv[1:]])]
    assert "Restart requested — relaunching Ciaobot" in capsys.readouterr().err


def test_run_propagates_other_exit_codes_without_relaunch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ciao.main

    def fail_execv(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("execv must not be called for non-restart exits")

    monkeypatch.setattr(ciao.main, "main", _raise_system_exit(3))
    monkeypatch.setattr(cli.os, "execv", fail_execv)

    assert cli._run_server() == 3


def test_run_restart_exit_code_honors_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ciao.main

    execs: list[list[str]] = []
    monkeypatch.setattr(ciao.main, "main", _raise_system_exit(42))
    monkeypatch.setattr(cli.os, "execv", lambda exe, argv: execs.append(argv))
    monkeypatch.setenv("CIAO_RESTART_EXIT_CODE", "42")

    assert cli._run_server() == 42

    assert len(execs) == 1


def test_cli_public_preflight_dispatches_module(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli.public_release, "main", lambda argv: called.append(argv) or 7)

    assert cli.main(["public-preflight", "scan", "/tmp/export"]) == 7
    assert called == [["scan", "/tmp/export"]]


def test_cli_package_smoke_dispatches_module(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli.package_smoke, "main", lambda argv: called.append(argv) or 0)

    assert cli.main(["package-smoke", "--skip-frontend"]) == 0
    assert called == [["--skip-frontend"]]


def test_cli_prepare_release_dispatches_module(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli.release, "main", lambda argv: called.append(argv) or 0)

    assert cli.main(["prepare-release", "--version", "0.3.0"]) == 0
    assert called == [["--version", "0.3.0"]]


def test_cli_dev_dispatches_module(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli.dev, "main", lambda argv: called.append(argv) or 0)

    assert cli.main(["dev", "--workspace", "/tmp/app", "--no-install"]) == 0
    assert called == [
        [
            "--workspace",
            "/tmp/app",
            "--backend-port",
            "8543",
            "--frontend-port",
            "5173",
            "--no-install",
        ]
    ]


def test_cli_memory_command_removed() -> None:
    import pytest

    with pytest.raises(SystemExit):
        cli.main(["memory", "read", "--target", "memory"])


def test_cli_vault_search_dispatches_command(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli, "_vault_search_command", lambda args: called.append(args) or 0)

    assert cli.main(["vault-search", "query", "--limit", "3"]) == 0
    assert called[0].query == "query"
    assert called[0].limit == 3


def test_cli_vault_index_dispatches_command(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli, "_vault_index_command", lambda args: called.append(args) or 0)

    assert cli.main(["vault-index", "--workspace", "personal", "--format", "json"]) == 0
    assert called[0].workspace == "personal"
    assert called[0].format == "json"


def test_cli_vault_lint_dispatches_command(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli, "_vault_lint_command", lambda args: called.append(args) or 0)

    assert cli.main(["vault-lint", "--vault-root", "/tmp/vault"]) == 0
    assert str(called[0].vault_root) == "/tmp/vault"


def _write_healthy_audit_workspace(root: Path) -> None:
    from ciao.memory_tool import ensure_regions

    root.mkdir(parents=True)
    (root / "CLAUDE.md").write_text("- Use rtk for shell commands.\n", encoding="utf-8")
    ensure_regions(root / "CLAUDE.md")
    (root / "AGENTS.md").symlink_to("CLAUDE.md")
    (root / "memory-vault").mkdir()
    (root / ".runtime").mkdir()


def test_cli_os_audit_uses_workspace_and_vault_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    _write_healthy_audit_workspace(workspace)
    memory_dir = tmp_path / "bounded"
    memory_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    custom_runtime = workspace / "custom-runtime"
    custom_runtime.mkdir()
    monkeypatch.setenv("CIAO_RUNTIME_ROOT", "custom-runtime")
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(memory_dir))

    assert cli.main(["os-audit", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["setup_audit"]["workspace_root"] == str(workspace.resolve())
    assert report["setup_audit"]["vault_root"] == str((workspace / "memory-vault").resolve())
    assert report["setup_audit"]["runtime_root"] == str(custom_runtime.resolve())


def test_cli_os_audit_exit_codes_distinguish_findings_and_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    _write_healthy_audit_workspace(workspace)
    memory_dir = tmp_path / "bounded"
    memory_dir.mkdir()
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(memory_dir))

    (workspace / "skills" / "missing-md").mkdir(parents=True)
    assert cli.main(["os-audit", "--workspace", str(workspace), "--json"]) == 1

    assert cli.main([
        "os-audit",
        "--workspace",
        str(tmp_path / "missing"),
        "--json",
    ]) == 2


def test_cli_os_audit_passes_the_workspace_registry_to_upgrade_notices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    _write_healthy_audit_workspace(workspace)
    legacy = workspace / "research"
    (legacy / "projects" / "active" / "general").mkdir(parents=True)
    (workspace / ".runtime" / "workspaces.json").write_text(
        json.dumps([{"name": "research", "vault_root": "research"}]),
        encoding="utf-8",
    )
    memory_dir = tmp_path / "bounded"
    memory_dir.mkdir()
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(memory_dir))

    assert cli.main([
        "os-audit",
        "--workspace",
        str(workspace),
        "--json",
    ]) == 1
    report = json.loads(capsys.readouterr().out)
    notices = report["upgrade_notices"]["notices"]
    assert notices[0]["workspace"] == "research"
    assert "Open a Ciaobot chat" in notices[0]["remedy"]


def test_cli_create_chat_dispatches_command(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli, "_create_chat_command", lambda args: called.append(args) or 0)

    assert cli.main(["create-chat", "--prompt", "hello", "--workspace", "personal"]) == 0
    assert called[0].prompt == "hello"
    assert called[0].workspace == "personal"


def test_cli_cleanup_sdk_blobs_dispatches_command(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli, "_cleanup_sdk_blobs_command", lambda args: called.append(args) or 0)

    assert cli.main(["cleanup-sdk-blobs", "--workspace", "/tmp/workspace", "--apply"]) == 0
    assert str(called[0].workspace) == "/tmp/workspace"
    assert called[0].apply is True


def test_cli_skills_sync_dispatches_command(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli, "_skills_sync_command", lambda args: called.append(args) or 0)

    assert cli.main(["skills-sync", "write-cache", "lock.json", "heads.json", "cache.json"]) == 0
    assert called[0].args == ["write-cache", "lock.json", "heads.json", "cache.json"]


def test_cli_sync_skills_dispatches_command(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli, "_sync_skills_command", lambda args: called.append(args) or 0)

    assert (
        cli.main(
            [
                "sync-skills",
                "--workspace",
                "/tmp/workspace",
                "--skip-upstream",
            ]
        )
        == 0
    )
    assert str(called[0].workspace) == "/tmp/workspace"
    assert called[0].skip_upstream is True


def test_setup_scaffolds_workspace_from_stock(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    launch_agents = tmp_path / "LaunchAgents"
    apps = tmp_path / "Applications"

    rc = cli.main(
        [
            "setup",
            "--workspace",
            str(workspace),
            "--workspace-name",
            "research",
            "--auth-token",
            "test-token",
            "--push-contact",
            "mailto:owner@example.com",
            "--launch-agents-dir",
            str(launch_agents),
            "--app-dir",
            str(apps),
            "--python",
            "/opt/ciao/bin/python",
            "--port",
            "9443",
        ]
    )

    assert rc == 0
    assert (workspace / ".env").read_text(encoding="utf-8").splitlines()[:3] == [
        "PWA_AUTH_TOKEN=test-token",
        # Password protection is the default and is pinned explicitly, so an
        # unset value never has to be guessed at on the next start.
        "PWA_AUTH_REQUIRED=true",
        "CIAO_PUSH_CONTACT=mailto:owner@example.com",
    ]
    assert (workspace / ".claude" / "agents" / "memory.md").is_file()
    assert (workspace / "commands" / "remember.md").is_file()
    assert "ciao:memory" in (
        workspace / "commands" / "remember.md"
    ).read_text(encoding="utf-8")
    assert (workspace / "CLAUDE.md").is_file()
    assert (workspace / "AGENTS.md").is_symlink()
    assert (workspace / "AGENTS.md").readlink() == Path("CLAUDE.md")
    assert (workspace / "AGENTS.md").resolve() == (workspace / "CLAUDE.md").resolve()
    customization = workspace / "CIAO_CUSTOMIZATION.md"
    assert customization.is_file()
    assert "disallowed_tools" in customization.read_text(encoding="utf-8")
    assert (workspace / ".runtime" / "schedules.json").is_file()
    assert json.loads((workspace / ".runtime" / "schedules.json").read_text(encoding="utf-8")) == {"schedules": []}
    # Canonical user-asset sources exist so Workspace Health starts warning-free.
    assert (workspace / "subagents").is_dir()
    assert (workspace / "commands").is_dir()
    assert (workspace / "memory-vault" / "research" / "MEMORY.md").is_file()
    registry = json.loads(
        (workspace / ".runtime" / "workspaces.json").read_text(encoding="utf-8")
    )
    assert registry[0]["name"] == "research"
    assert registry[0]["vault_root"] == "memory-vault/research"
    plist = launch_agents / "com.ciao.server.plist"
    assert plist.is_file()
    plist_text = plist.read_text(encoding="utf-8")
    assert "<string>/opt/ciao/bin/python</string>" in plist_text
    assert "<string>run</string>" in plist_text
    assert f"<string>{workspace.resolve()}</string>" in plist_text
    assert "<string>9443</string>" in plist_text
    assert f"<string>{workspace.resolve()}/.runtime/ciao.stdout.log</string>" in plist_text
    # No menu-bar agent and no launcher bundle: Ciaobot.app is the menu bar
    # now, and nothing writes the retired rumps helper.
    assert not (launch_agents / "com.ciao.menubar.plist").exists()
    assert not (apps / "Ciaobot Server.app").exists()
    # Login Items still groups the server agent under the desktop app.
    assert "<key>AssociatedBundleIdentifiers</key>" in plist_text
    assert "<string>local.ciaobot.app</string>" in plist_text
    # Setup always mints the one-time login token, even with no desktop app:
    # the summary prints it as the login URL.
    setup_token = (workspace / ".runtime" / "setup-token").read_text(
        encoding="utf-8"
    ).strip()
    assert setup_token


def test_setup_no_auth_opts_out_of_password_protection(tmp_path: Path) -> None:
    """`--no-auth` is the only way a scripted setup gets an unprotected
    dashboard, and it must be pinned in .env — an unset value now means on."""
    workspace = tmp_path / "workspace"

    rc = cli.main(
        [
            "setup",
            "--workspace",
            str(workspace),
            "--auth-token",
            "test-token",
            "--no-auth",
            "--launch-agents-dir",
            str(tmp_path / "LaunchAgents"),
            "--app-dir",
            str(tmp_path / "Applications"),
        ]
    )

    assert rc == 0
    env_lines = (workspace / ".env").read_text(encoding="utf-8").splitlines()
    assert "PWA_AUTH_REQUIRED=false" in env_lines
    assert "PWA_AUTH_REQUIRED=true" not in env_lines


def test_setup_uses_bundled_launcher_when_python_is_not_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = "/Applications/Ciaobot.app/Contents/Resources/ciao-runtime/bin/ciao"
    monkeypatch.setenv("CIAO_ENGINE_PATH", engine)
    launch_agents = tmp_path / "LaunchAgents"

    cli.setup_workspace(
        tmp_path / "workspace",
        launch_agents_dir=launch_agents,
        python_path=None,
    )

    with (launch_agents / "com.ciao.server.plist").open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist["ProgramArguments"][0] == engine
    assert plist["ProgramArguments"][1:] == ["run"]
    assert plist["EnvironmentVariables"]["CIAO_NATIVE_SIDECAR"] == (
        "/Applications/Ciaobot.app/Contents/MacOS/ciaobot-native"
    )


def test_setup_uses_python_module_invocation_for_python_path(tmp_path: Path) -> None:
    launch_agents = tmp_path / "LaunchAgents"

    cli.setup_workspace(
        tmp_path / "workspace",
        launch_agents_dir=launch_agents,
        python_path="/opt/ciao/bin/python3.12",
    )

    with (launch_agents / "com.ciao.server.plist").open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist["ProgramArguments"][1:] == ["-m", "ciao.cli", "run"]
    assert plist["EnvironmentVariables"]["CIAO_NATIVE_SIDECAR"] == ""


def _write_desktop_app(app_dir: Path) -> Path:
    """Materialize the Tauri cask's ``Ciaobot.app``, bundle id and all."""

    app_root = app_dir / "Ciaobot.app"
    macos = app_root / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "ciaobot-desktop").write_text("", encoding="utf-8")
    (app_root / "Contents" / "Info.plist").write_text(
        "<plist><string>local.ciaobot.app</string></plist>", encoding="utf-8"
    )
    return app_root


def test_is_our_app_bundle_rejects_the_tauri_desktop_app(tmp_path: Path) -> None:
    # Same bundle id as our pre-rename launcher; only the executable differs.
    assert cli._is_our_app_bundle(_write_desktop_app(tmp_path)) is False


def test_remove_legacy_app_shortcuts_keeps_the_tauri_desktop_app(tmp_path: Path) -> None:
    app_root = _write_desktop_app(tmp_path)

    assert cli._remove_legacy_app_shortcuts(tmp_path) is False
    assert (app_root / "Contents" / "MacOS" / "ciaobot-desktop").is_file()


def _setup_argv(workspace: Path, launch_agents: Path, apps: Path, *, yes: bool = False) -> list[str]:
    argv = [
        "setup",
        "--workspace", str(workspace),
        "--launch-agents-dir", str(launch_agents),
        "--app-dir", str(apps),
        "--python", "/opt/ciao/bin/python",
        "--port", "9443",
    ]
    if yes:
        argv.append("--yes")
    return argv


def test_setup_refuses_source_checkout(tmp_path: Path, capsys) -> None:
    # A directory that looks like the Ciaobot source repo must be rejected so
    # setup can't hijack the real workspace by repointing the LaunchAgents.
    checkout = tmp_path / "ciaobot"
    (checkout / "ciao").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (checkout / "ciao" / "__init__.py").write_text("", encoding="utf-8")

    rc = cli.main(_setup_argv(checkout, tmp_path / "LaunchAgents", tmp_path / "Applications"))

    assert rc == 1
    assert "source checkout" in capsys.readouterr().err
    assert not (checkout / ".env").exists()  # nothing scaffolded


def test_setup_refuses_to_repoint_existing_workspace(tmp_path: Path, capsys) -> None:
    launch_agents = tmp_path / "LaunchAgents"
    apps = tmp_path / "Applications"
    first = tmp_path / "ws-one"
    second = tmp_path / "ws-two"

    assert cli.main(_setup_argv(first, launch_agents, apps)) == 0
    capsys.readouterr()

    # A second setup pointed elsewhere must refuse rather than silently move it.
    rc = cli.main(_setup_argv(second, launch_agents, apps))
    assert rc == 1
    assert "already set up" in capsys.readouterr().err
    assert not (second / ".env").exists()


def test_setup_yes_overrides_repoint_guard(tmp_path: Path) -> None:
    launch_agents = tmp_path / "LaunchAgents"
    apps = tmp_path / "Applications"
    first = tmp_path / "ws-one"
    second = tmp_path / "ws-two"

    assert cli.main(_setup_argv(first, launch_agents, apps)) == 0
    assert cli.main(_setup_argv(second, launch_agents, apps, yes=True)) == 0
    assert (second / ".env").exists()


def _stub_setup_for_launchd(workspace: Path, **kwargs) -> list[Path]:
    root = workspace.expanduser().resolve()
    (root / ".runtime").mkdir(parents=True)
    (root / ".runtime" / "setup-token").write_text("test-token\n", encoding="utf-8")
    launch_agents = Path(kwargs["launch_agents_dir"])
    launch_agents.mkdir(parents=True, exist_ok=True)
    (launch_agents / "com.ciao.server.plist").write_text("plist", encoding="utf-8")
    return []


def _launchd_setup_argv(workspace: Path, launch_agents: Path) -> list[str]:
    return [
        "setup",
        "--workspace",
        str(workspace),
        "--auth-token",
        "test-token",
        "--launch-agents-dir",
        str(launch_agents),
        "--app-dir",
        str(workspace / "Applications"),
        "--load-launchd",
    ]


def test_setup_quiets_only_the_expected_launchd_unload_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if command[0] != "launchctl":
            return real_run(command, *args, **kwargs)
        calls.append((list(command), kwargs))
        return subprocess.CompletedProcess(
            command, 5 if command[1] == "unload" else 0
        )

    monkeypatch.setattr(cli, "setup_workspace", _stub_setup_for_launchd)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert (
        cli.main(_launchd_setup_argv(tmp_path / "workspace", tmp_path / "LaunchAgents"))
        == 0
    )

    assert calls[0][0][1] == "unload"
    assert calls[0][1]["stderr"] is subprocess.DEVNULL
    assert "stdout" not in calls[0][1]
    assert calls[1][0][1] == "load"
    assert calls[1][1] == {"check": False}


def test_setup_preserves_load_failure_status_and_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if command[0] != "launchctl":
            return real_run(command, *args, **kwargs)
        calls.append((list(command), kwargs))
        if command[1] == "load" and kwargs.get("stderr") is not subprocess.DEVNULL:
            print("launchctl: load failed", file=sys.stderr)
        return subprocess.CompletedProcess(command, 5 if command[1] == "load" else 0)

    monkeypatch.setattr(cli, "setup_workspace", _stub_setup_for_launchd)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli.main(
        _launchd_setup_argv(tmp_path / "workspace", tmp_path / "LaunchAgents")
    )

    assert result == 5
    assert calls[1][1] == {"check": False}
    assert capsys.readouterr().err == "launchctl: load failed\n"


def test_setup_removes_our_legacy_ciao_app_only(tmp_path: Path) -> None:
    apps = tmp_path / "Applications"
    ours = apps / "Ciao.app" / "Contents"
    ours.mkdir(parents=True)
    (ours / "Info.plist").write_text(
        "<plist><string>local.ciao.app</string></plist>", encoding="utf-8"
    )
    foreign = apps / "OtherCiao.app"

    rc = cli.main([
        "setup",
        "--workspace",
        str(tmp_path / "workspace"),
        "--launch-agents-dir",
        str(tmp_path / "LaunchAgents"),
        "--app-dir",
        str(apps),
    ])

    assert rc == 0
    assert not (apps / "Ciao.app").exists()
    # The launcher is retired: cleanup runs, nothing is written back.
    assert not (apps / "Ciaobot Server.app").exists()
    assert not foreign.exists()  # untouched (never created); guard for typos


def test_setup_migrates_native_ciaobot_app_without_removing_pwa(tmp_path: Path) -> None:
    apps = tmp_path / "Applications"
    legacy = apps / "Ciaobot.app" / "Contents"
    legacy.mkdir(parents=True)
    (legacy / "Info.plist").write_text(
        "<plist><string>local.ciaobot.app</string></plist>", encoding="utf-8"
    )
    pwa = apps / "Chrome Apps.localized" / "Ciaobot.app" / "Contents"
    pwa.mkdir(parents=True)
    (pwa / "Info.plist").write_text(
        "<plist><string>org.chromium.Chromium.app.ciaobot</string></plist>",
        encoding="utf-8",
    )

    assert cli.main([
        "setup",
        "--workspace",
        str(tmp_path / "workspace"),
        "--launch-agents-dir",
        str(tmp_path / "LaunchAgents"),
        "--app-dir",
        str(apps),
    ]) == 0

    assert not (apps / "Ciaobot.app").exists()
    # The launcher is retired: cleanup runs, nothing is written back.
    assert not (apps / "Ciaobot Server.app").exists()
    assert (apps / "Chrome Apps.localized" / "Ciaobot.app").is_dir()


def test_setup_keeps_browser_pwa_named_ciaobot_app(tmp_path: Path) -> None:
    apps = tmp_path / "Applications"
    pwa = apps / "Ciaobot.app" / "Contents"
    pwa.mkdir(parents=True)
    (pwa / "Info.plist").write_text(
        "<plist><string>org.chromium.Chromium.app.ciaobot</string></plist>",
        encoding="utf-8",
    )

    assert cli.main([
        "setup",
        "--workspace",
        str(tmp_path / "workspace"),
        "--launch-agents-dir",
        str(tmp_path / "LaunchAgents"),
        "--app-dir",
        str(apps),
    ]) == 0

    assert (apps / "Ciaobot.app").is_dir()
    # The launcher is retired: cleanup runs, nothing is written back.
    assert not (apps / "Ciaobot Server.app").exists()


def test_setup_skips_legacy_companion_when_tauri_app_is_installed(
    tmp_path: Path,
) -> None:
    apps = tmp_path / "Applications"
    executable = apps / "Ciaobot.app" / "Contents" / "MacOS" / "ciaobot-desktop"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    launch_agents = tmp_path / "LaunchAgents"

    assert cli.main([
        "setup",
        "--workspace",
        str(tmp_path / "workspace"),
        "--launch-agents-dir",
        str(launch_agents),
        "--app-dir",
        str(apps),
    ]) == 0

    assert (launch_agents / "com.ciao.server.plist").is_file()
    assert not (launch_agents / "com.ciao.menubar.plist").exists()
    assert not (apps / "Ciaobot Server.app").exists()


def test_default_app_dir_matches_the_release_installer() -> None:
    assert cli._default_app_dir() == Path.home() / "Applications"


def test_setup_cleans_our_bundles_from_home_applications(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    home_apps = tmp_path / "home" / "Applications"
    for name, bundle_id in (("Ciao.app", "local.ciao.app"), ("Ciaobot.app", "local.ciaobot.app")):
        contents = home_apps / name / "Contents"
        contents.mkdir(parents=True)
        (contents / "Info.plist").write_text(
            f"<plist><string>{bundle_id}</string></plist>", encoding="utf-8"
        )
    system_apps = tmp_path / "SystemApplications"

    assert cli.main([
        "setup",
        "--workspace",
        str(tmp_path / "workspace"),
        "--launch-agents-dir",
        str(tmp_path / "LaunchAgents"),
        "--app-dir",
        str(system_apps),
    ]) == 0

    assert not (home_apps / "Ciao.app").exists()
    assert not (home_apps / "Ciaobot.app").exists()
    assert not (system_apps / "Ciaobot Server.app").exists()


def test_setup_keeps_unrelated_ciao_app(tmp_path: Path) -> None:
    apps = tmp_path / "Applications"
    unrelated = apps / "Ciao.app" / "Contents"
    unrelated.mkdir(parents=True)
    (unrelated / "Info.plist").write_text(
        "<plist><string>com.somebody.else</string></plist>", encoding="utf-8"
    )

    assert cli.main([
        "setup",
        "--workspace",
        str(tmp_path / "workspace"),
        "--launch-agents-dir",
        str(tmp_path / "LaunchAgents"),
        "--app-dir",
        str(apps),
    ]) == 0

    assert (apps / "Ciao.app").is_dir()


def test_setup_merges_into_existing_env(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "# my own settings\nMY_API_KEY=secret\nPWA_AUTH_TOKEN=existing\n",
        encoding="utf-8",
    )

    assert cli.main([
        "setup",
        "--workspace",
        str(workspace),
        "--launch-agents-dir",
        str(tmp_path / "LaunchAgents"),
        "--app-dir",
        str(tmp_path / "Applications"),
    ]) == 0

    content = (workspace / ".env").read_text(encoding="utf-8")
    # The user's file survives verbatim: comments, own variables, and an
    # already-configured token are never rewritten or regenerated.
    assert content.startswith(
        "# my own settings\nMY_API_KEY=secret\nPWA_AUTH_TOKEN=existing\n"
    )
    assert content.count("PWA_AUTH_TOKEN=") == 1
    # Missing Ciaobot variables are appended so the install actually works.
    assert "CIAO_WORKSPACE=." in content
    assert "CIAO_VAULT_ROOT=" in content
    assert "CIAO_RUNTIME_ROOT=.runtime" in content


def test_setup_env_merge_is_idempotent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    args = [
        "setup",
        "--workspace",
        str(workspace),
        "--launch-agents-dir",
        str(tmp_path / "LaunchAgents"),
        "--app-dir",
        str(tmp_path / "Applications"),
    ]

    assert cli.main(args) == 0
    first = (workspace / ".env").read_text(encoding="utf-8")
    assert cli.main(args) == 0

    # A second run finds every variable present and leaves the file alone.
    assert (workspace / ".env").read_text(encoding="utf-8") == first


def test_setup_prints_workspace_and_login_url(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"

    assert cli.main([
        "setup",
        "--workspace",
        str(workspace),
        "--launch-agents-dir",
        str(tmp_path / "LaunchAgents"),
        "--app-dir",
        str(tmp_path / "Applications"),
        "--port",
        "9443",
    ]) == 0

    out = capsys.readouterr().out
    token = (workspace / ".runtime" / "setup-token").read_text(encoding="utf-8").strip()
    assert f"Workspace: {workspace.resolve()}" in out
    assert f"Open Ciaobot: http://localhost:9443/?setup={token}" in out


def test_path_export_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = Path(cli.sys.executable).parent
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert cli._path_export_hint() == f'export PATH="{bin_dir}:$PATH"'
    monkeypatch.setenv("PATH", f"/usr/bin:{bin_dir}")
    assert cli._path_export_hint() is None


def test_setup_url_rotates_token_by_default(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    token_path = workspace / ".runtime" / "setup-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("stale-token\n", encoding="utf-8")

    assert cli.main(["setup-url", "--workspace", str(workspace)]) == 0

    new_token = token_path.read_text(encoding="utf-8").strip()
    assert new_token and new_token != "stale-token"
    out = capsys.readouterr().out
    assert f"Workspace: {workspace.resolve()}" in out
    assert f"http://localhost:8443/?setup={new_token}" in out


def test_setup_url_no_rotate_reuses_existing_token(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    token_path = workspace / ".runtime" / "setup-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("keep-me\n", encoding="utf-8")

    assert cli.main(["setup-url", "--workspace", str(workspace), "--no-rotate"]) == 0

    assert token_path.read_text(encoding="utf-8").strip() == "keep-me"
    assert "http://localhost:8443/?setup=keep-me" in capsys.readouterr().out


def test_setup_url_reads_port_from_env(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("PWA_PORT=9999\n", encoding="utf-8")

    assert cli.main(["setup-url", "--workspace", str(workspace)]) == 0

    assert "http://localhost:9999/?setup=" in capsys.readouterr().out


def test_auth_print_only_outputs_terminal_command(capsys) -> None:
    assert cli.main(["auth", "opencode", "--print-only"]) == 0

    assert capsys.readouterr().out.strip().endswith("auth login")


def test_auth_print_only_opencode_does_not_require_installation(monkeypatch, capsys) -> None:
    monkeypatch.setattr("ciao.providers.opencode.resolve_opencode_binary", lambda: None)

    assert cli.main(["auth", "opencode", "--print-only"]) == 0

    assert capsys.readouterr().out.strip() == "opencode auth login"


def test_auth_rejects_a_non_runtime_provider(capsys) -> None:
    """Only the three runtime providers have a login; nothing else is offered."""
    with pytest.raises(SystemExit):
        cli.main(["auth", "ollama", "--print-only"])


def test_auth_claude_uses_bundled_cli(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "ciao.providers.claude.get_bundled_claude_path",
        lambda: "/opt/ciao/claude",
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda cmd, check=False: calls.append(cmd) or type("P", (), {"returncode": 0})(),
    )

    assert cli.main(["auth", "claude"]) == 0

    assert calls == [["/opt/ciao/claude", "login"]]


def test_auth_codex_supports_device_authorization(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "ciao.providers.codex.resolve_codex_binary",
        lambda: "/opt/ciao/codex",
    )

    assert cli.main(["auth", "codex", "--device-auth", "--print-only"]) == 0

    assert capsys.readouterr().out.strip() == "/opt/ciao/codex login --device-auth"


def test_vault_index_accepts_arbitrary_workspace_name(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(cli, "_vault_index_command", lambda args: called.append(args) or 0)

    assert cli.main(["vault-index", "--workspace", "client"]) == 0

    assert called[0].workspace == "client"


def test_create_chat_accepts_a_configured_workspace(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(cli, "_create_chat_command", lambda args: called.append(args) or 0)

    assert cli.main(["create-chat", "--prompt", "hello", "--workspace", "client"]) == 0

    assert called[0].workspace == "client"


def test_create_chat_rejects_the_removed_model_bucket_flag(monkeypatch) -> None:
    """The bucket named which upstream a tier alias resolved to; nothing reads it."""
    monkeypatch.setattr(cli, "_create_chat_command", lambda args: 0)

    with pytest.raises(SystemExit):
        cli.main(["create-chat", "--prompt", "hi", "--model-bucket", "anthropic"])


def test_create_chat_command_uses_active_workspace_without_name_clamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "client")
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(_opener, url: str, *, data=None, method: str = "GET"):
        calls.append((method, url, data))
        if url == "http://test/api/auth":
            return {}
        if url == "http://test/api/projects?workspace=client":
            return [{"project_id": "proj-client", "name": "General", "is_auto": True}]
        if url == "http://test/api/projects/proj-client/chats":
            return {
                "chat_id": "chat-client",
                "title": "New Chat",
                "project_id": "proj-client",
                "model": "opus",
                "provider": "claude",
            }
        if url == "http://test/api/chats/chat-client/prompt":
            return {}
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(cli, "_make_json_request", fake_request)

    assert (
        cli.main(
            [
                "create-chat",
                "--prompt",
                "hello",
                "--workspace-root",
                str(tmp_path),
                "--base-url",
                "http://test",
            ]
        )
        == 0
    )

    assert ("GET", "http://test/api/projects?workspace=client", None) in calls
    assert all("workspace=personal" not in url for _, url, _ in calls)
    assert "Workspace: client" in capsys.readouterr().out




def test_desktop_uninstall_reports_when_nothing_is_installed(
    tmp_path: Path, capsys
) -> None:
    assert cli.main(["desktop", "uninstall", "--app-dir", str(tmp_path)]) == 0
    assert "Nothing to remove" in capsys.readouterr().out


def test_desktop_uninstall_json_reports_a_refusal(tmp_path: Path, capsys) -> None:
    pwa = tmp_path / "Ciaobot.app" / "Contents" / "MacOS"
    pwa.mkdir(parents=True)
    (pwa / "app_mode_loader").write_text("chrome pwa", encoding="utf-8")

    assert cli.main(["desktop", "uninstall", "--app-dir", str(tmp_path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "not the Ciaobot desktop app" in payload["error"]


def test_setup_does_not_download_or_install_the_desktop_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workspace setup is local; the release installer owns app installation."""
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    apps = tmp_path / "Applications"
    apps.mkdir()
    monkeypatch.setattr(cli, "_default_app_dir", lambda: apps)

    written = cli.setup_workspace(
        tmp_path / "workspace",
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=apps,
    )

    assert written
    assert not (apps / "Ciaobot.app").exists()
    assert not (apps / "Ciaobot Server.app").exists()
