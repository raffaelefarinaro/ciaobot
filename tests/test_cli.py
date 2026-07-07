from __future__ import annotations

import json
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


def test_cli_memory_dispatches_command(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli, "_memory_command", lambda args: called.append(args) or 0)

    assert cli.main(["memory", "read", "--target", "memory"]) == 0
    assert called[0].action == "read"
    assert called[0].target == "memory"


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
    assert (workspace / ".env").read_text(encoding="utf-8").splitlines()[:2] == [
        "PWA_AUTH_TOKEN=test-token",
        "CIAO_PUSH_CONTACT=mailto:owner@example.com",
    ]
    assert (workspace / ".claude" / "agents" / "memory.md").is_file()
    assert (workspace / ".claude" / "commands" / "remember.md").is_file()
    assert (workspace / "CLAUDE.md").is_file()
    customization = workspace / "CIAO_CUSTOMIZATION.md"
    assert customization.is_file()
    assert "disallowed_tools" in customization.read_text(encoding="utf-8")
    assert (workspace / ".runtime" / "schedules.json").is_file()
    assert json.loads((workspace / ".runtime" / "schedules.json").read_text(encoding="utf-8")) == {"schedules": []}
    # Canonical user-asset sources exist so Workspace Health starts warning-free.
    assert (workspace / "subagents").is_dir()
    assert (workspace / "commands").is_dir()
    assert (workspace / "memory-vault" / "MEMORY.md").is_file()
    plist = launch_agents / "com.ciao.server.plist"
    assert plist.is_file()
    plist_text = plist.read_text(encoding="utf-8")
    assert "<string>/opt/ciao/bin/python</string>" in plist_text
    assert "<string>-m</string>" in plist_text
    assert "<string>ciao.cli</string>" in plist_text
    assert "<string>run</string>" in plist_text
    assert f"<string>{workspace.resolve()}</string>" in plist_text
    assert "<string>9443</string>" in plist_text
    assert f"<string>{workspace.resolve()}/.runtime/ciao.stdout.log</string>" in plist_text
    menubar_plist = launch_agents / "com.ciao.menubar.plist"
    assert menubar_plist.is_file()
    menubar_text = menubar_plist.read_text(encoding="utf-8")
    # Login Items groups both agents under Ciaobot.app instead of python3.13.
    assert "<key>AssociatedBundleIdentifiers</key>" in plist_text
    assert "<string>local.ciaobot.app</string>" in plist_text
    assert "<key>AssociatedBundleIdentifiers</key>" in menubar_text
    assert "<string>local.ciaobot.app</string>" in menubar_text
    assert "<string>com.ciao.menubar</string>" in menubar_text
    assert "<string>/opt/ciao/bin/python</string>" in menubar_text
    assert "<string>menubar</string>" in menubar_text
    assert "<string>9443</string>" in menubar_text
    assert f"<string>{workspace.resolve()}/.runtime/ciao.menubar.stdout.log</string>" in menubar_text
    app_exe = apps / "Ciaobot.app" / "Contents" / "MacOS" / "Ciaobot"
    assert app_exe.is_file()
    assert app_exe.stat().st_mode & 0o111
    app_text = app_exe.read_text(encoding="utf-8")
    # The launcher reads the one-time setup token live from disk (it is
    # deleted after first login), so it must NOT bake a frozen token value
    # into the URL -- otherwise a second launch shows "invalid setup token".
    token_file = workspace / ".runtime" / "setup-token"
    assert f'token=$(tr -d "[:space:]" < "{token_file}"' in app_text
    assert 'open "http://localhost:9443/?setup=$token"' in app_text
    assert 'open "http://localhost:9443/"' in app_text
    # The launcher starts the server and menu bar agents when they're down.
    assert 'launchctl kickstart "gui/$(id -u)/com.ciao.server"' in app_text
    assert 'launchctl kickstart "gui/$(id -u)/com.ciao.menubar"' in app_text
    setup_token = token_file.read_text(encoding="utf-8").strip()
    assert setup_token
    # The literal token value must not appear in the script -- it is read live.
    assert setup_token not in app_text
    icns = apps / "Ciaobot.app" / "Contents" / "Resources" / "Ciaobot.icns"
    assert icns.is_file() and icns.stat().st_size > 0
    info_plist = (apps / "Ciaobot.app" / "Contents" / "Info.plist").read_text(encoding="utf-8")
    assert "<key>CFBundleIconFile</key>" in info_plist
    assert "<string>Ciaobot</string>" in info_plist


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
    assert (apps / "Ciaobot.app").is_dir()
    assert not foreign.exists()  # untouched (never created); guard for typos


def test_default_app_dir_prefers_system_applications(monkeypatch) -> None:
    monkeypatch.setattr(cli.os, "access", lambda path, mode: True)
    assert cli._default_app_dir() == Path("/Applications")

    monkeypatch.setattr(cli.os, "access", lambda path, mode: False)
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
    assert (system_apps / "Ciaobot.app").is_dir()


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


def test_setup_is_idempotent_and_does_not_overwrite_env(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("PWA_AUTH_TOKEN=existing\n", encoding="utf-8")

    assert cli.main([
        "setup",
        "--workspace",
        str(workspace),
        "--launch-agents-dir",
        str(tmp_path / "LaunchAgents"),
        "--app-dir",
        str(tmp_path / "Applications"),
    ]) == 0

    assert (workspace / ".env").read_text(encoding="utf-8") == "PWA_AUTH_TOKEN=existing\n"


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
    assert cli.main(["auth", "ollama", "--print-only"]) == 0

    assert capsys.readouterr().out.strip() == "ollama signin"


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


def test_vault_index_accepts_arbitrary_workspace_name(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(cli, "_vault_index_command", lambda args: called.append(args) or 0)

    assert cli.main(["vault-index", "--workspace", "client"]) == 0

    assert called[0].workspace == "client"


def test_create_chat_accepts_configured_workspace_and_bucket(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(cli, "_create_chat_command", lambda args: called.append(args) or 0)

    assert (
        cli.main(
            [
                "create-chat",
                "--prompt",
                "hello",
                "--workspace",
                "client",
                "--model-bucket",
                "anthropic",
            ]
        )
        == 0
    )

    assert called[0].workspace == "client"
    assert called[0].model_bucket == "anthropic"


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
