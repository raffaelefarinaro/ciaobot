from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from ciao import cli


def test_cli_run_dispatches_server(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli, "_run_server", lambda **kwargs: called.append(kwargs) or 0)

    assert cli.main(["run"]) == 0
    assert called == [{"supervised": False}]


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

    assert cli._run_server() == 75

    assert execs == [(cli.sys.executable, [cli.sys.executable, "-m", "ciao.cli", *cli.sys.argv[1:]])]
    assert "Restart requested — relaunching Ciaobot" in capsys.readouterr().err


def test_run_relaunch_drops_dotenv_exports_so_the_new_process_rereads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_dotenv never overrides an inherited key, so a stale export would
    shadow a value edited in the workspace .env across the restart."""
    import ciao.main
    from ciao import config as ciao_config

    seen: list[str | None] = []
    monkeypatch.setattr(ciao.main, "main", _raise_system_exit(75))
    monkeypatch.setattr(
        cli.os, "execv", lambda exe, argv: seen.append(cli.os.environ.get("CIAO_TEST_DOTENV_KEY"))
    )
    monkeypatch.setenv("CIAO_TEST_DOTENV_KEY", "stale")
    monkeypatch.setattr(ciao_config, "_EXPORTED_DOTENV_KEYS", {"CIAO_TEST_DOTENV_KEY"})

    assert cli._run_server() == 75
    assert seen == [None]


def test_run_propagates_other_exit_codes_without_relaunch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ciao.main

    def fail_execv(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("execv must not be called for non-restart exits")

    monkeypatch.setattr(ciao.main, "main", _raise_system_exit(3))
    monkeypatch.setattr(cli.os, "execv", fail_execv)

    assert cli._run_server() == 3


def test_run_supervised_returns_restart_code_without_execv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The supervisor owns the relaunch, so the child only reports the code."""
    import ciao.main

    def fail_execv(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("execv must not be called under --supervised")

    monkeypatch.setattr(ciao.main, "main", _raise_system_exit(75))
    monkeypatch.setattr(cli.os, "execv", fail_execv)

    assert cli._run_server(supervised=True) == 75


def test_run_parser_accepts_supervised_flag() -> None:
    assert cli.build_parser().parse_args(["run", "--supervised"]).supervised is True


def test_supervise_is_registered_and_help_exits_zero() -> None:
    """Registered as a subparser too, so `ciao --help` discloses it."""
    parser = cli.build_parser()
    action = next(a for a in parser._subparsers._actions if hasattr(a, "choices") and a.choices)
    assert "supervise" in action.choices

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["supervise", "--help"])
    assert exc.value.code == 0


def test_supervise_is_not_an_agent_command() -> None:
    """`ciao supervise` is an operator launcher: the agent surface must not claim it."""
    from ciao.agent_cli import is_agent_invocation

    assert is_agent_invocation(["supervise"]) is False


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


def test_cli_gws_passthrough_forwards_leading_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ciao gws --version` must forward the option untouched, not reject it."""
    called = []

    monkeypatch.setattr(cli.gws_wrapper, "main", lambda argv: called.append(argv) or 0)

    assert cli.main(["gws", "--version"]) == 0
    assert called == [["--version"]]

    assert cli.main(["gws", "--profile", "work", "calendar", "list"]) == 0
    assert called[-1] == ["--profile", "work", "calendar", "list"]


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


def test_cli_gws_auth_helper_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(
        cli, "_gws_auth_helper_command", lambda args: called.append(args) or 0
    )

    assert cli.main(["gws-auth-helper", "work", "--redirect-url", "http://x"]) == 0
    assert called[0].profile == "work"
    assert called[0].redirect_url == "http://x"


def test_cli_workspace_census_dispatches_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = []

    monkeypatch.setattr(
        cli, "_workspace_census_command", lambda args: called.append(args) or 0
    )

    assert cli.main(["workspace-census", "--vault-root", "/tmp/vault", "--json"]) == 0
    assert str(called[0].vault_root) == "/tmp/vault"
    assert called[0].json is True


def _write_healthy_audit_workspace(root: Path) -> None:
    from ciao.memory_tool import ensure_regions

    root.mkdir(parents=True)
    (root / "AGENTS.md").write_text("- Use rtk for shell commands.\n", encoding="utf-8")
    ensure_regions(root / "AGENTS.md")
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


def test_cli_os_audit_pending_only_exits_zero_with_a_distinct_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pending-only audit exits 0 and prints its own line, not the healthy one."""
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

    assert cli.main(["os-audit", "--workspace", str(workspace)]) == 0
    out = capsys.readouterr().out
    assert "Pending actions" in out
    assert "Upgrade Actions (optional)" in out


def test_cli_os_audit_explicit_workspace_beats_an_ambient_runtime_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An absolute CIAO_RUNTIME_ROOT must not escape an explicit --workspace.

    A running Ciaobot chat exports CIAO_RUNTIME_ROOT for its own install. When
    the env won, `--workspace` still selected the vault but the registry, job
    runs and migration receipts came from the surrounding install, so the audit
    reported on a different workspace than the one it was asked about, without
    saying so.
    """
    workspace = tmp_path / "workspace"
    _write_healthy_audit_workspace(workspace)
    (workspace / "research" / "projects" / "active" / "general").mkdir(parents=True)
    (workspace / ".runtime" / "workspaces.json").write_text(
        json.dumps([{"name": "research", "vault_root": "research"}]),
        encoding="utf-8",
    )
    memory_dir = tmp_path / "bounded"
    memory_dir.mkdir()
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(memory_dir))

    # Another install's runtime root, exactly as a Ciaobot chat exports it.
    foreign = tmp_path / "other-install" / ".runtime"
    foreign.mkdir(parents=True)
    (foreign / "workspaces.json").write_text(
        json.dumps([{"name": "personal", "vault_root": "memory-vault/personal"}]),
        encoding="utf-8",
    )
    monkeypatch.setenv("CIAO_RUNTIME_ROOT", str(foreign))

    assert cli.main(["os-audit", "--workspace", str(workspace), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["job_runs_audit"]["runtime_root"] == str(workspace / ".runtime")
    # The named workspace's own registry was read, so its nonstandard vault is
    # still detected rather than the foreign install's healthy one.
    assert report["pending_action_count"] == 1
    assert report["upgrade_notices"]["notices"][0]["workspace"] == "research"


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

    # A pending upgrade notice is an optional action the operator may decline.
    # Under D2 it must not raise the status: this exits 0, and the notice is
    # surfaced on its own line instead of the healthy one.
    assert cli.main([
        "os-audit",
        "--workspace",
        str(workspace),
    ]) == 0
    out = capsys.readouterr().out
    assert "Pending actions" in out
    assert "Upgrade Actions (optional)" in out

    assert cli.main([
        "os-audit",
        "--workspace",
        str(workspace),
        "--json",
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "healthy"
    assert report["defect_count"] == 0
    assert report["pending_action_count"] == 1
    notices = report["upgrade_notices"]["notices"]
    assert notices[0]["workspace"] == "research"
    # The managed command, not a hand migration: the audit's remedy and the Home
    # card's are one sentence from `ciao.migration_notices` since #816.
    assert "ciao vault-relocate research --apply" in notices[0]["remedy"]


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


def test_cli_skills_sync_removed() -> None:
    # skills-sync command has been removed per simplification plan.
    try:
        cli.main(["skills-sync", "write-cache", "lock.json", "heads.json", "cache.json"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        assert False, "skills-sync should exit with code 2"


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
        # Password protection is the default and is pinned explicitly, so an
        # unset value never has to be guessed at on the next start.
        "PWA_AUTH_REQUIRED=true",
    ]
    # Agent assets belong to the WORKSPACE root, not the install root: a fresh
    # setup now builds the per-workspace layout directly instead of the shared one
    # that then had to be migrated.
    root = workspace / "research"
    assert (root / ".claude" / "skills" / "ciao-memory" / "SKILL.md").is_file()
    assert not (root / ".claude" / "agents" / "memory.md").exists()
    assert (root / "commands" / "remember.md").is_file()
    assert "ciao:memory" in (
        root / "commands" / "remember.md"
    ).read_text(encoding="utf-8")
    # setup seeds through _seed_stock_commands, so each stock copy carries the
    # sibling marker (with the sha256 of the written bytes) from the start.
    assert (root / "commands" / "remember.md.ciao-stock-command").is_file()
    assert (root / "AGENTS.md").is_file()
    # One real guide and no CLAUDE.md: Claude Code reads AGENTS.md natively
    # since 2.1.277, but only when no CLAUDE.md is present.
    assert not (root / "AGENTS.md").is_symlink()
    assert not (root / "CLAUDE.md").exists()
    assert "ciao:memory:start" in (root / "AGENTS.md").read_text(encoding="utf-8")
    customization = root / "CIAO_CUSTOMIZATION.md"
    assert customization.is_file()
    assert "disallowed_tools" in customization.read_text(encoding="utf-8")
    assert (workspace / ".runtime" / "schedules.json").is_file()
    assert json.loads((workspace / ".runtime" / "schedules.json").read_text(encoding="utf-8")) == {"schedules": []}
    # Canonical user-asset sources exist so Workspace Health starts warning-free.
    assert (root / "subagents").is_dir()
    assert (root / "commands").is_dir()
    # Nothing agent-shaped is left at the install root for the migration to move.
    assert not (workspace / "AGENTS.md").exists()
    assert not (workspace / "subagents").exists()
    assert (root / "memory-vault" / "MEMORY.md").is_file()
    assert not (workspace / "memory-vault").exists()
    registry = json.loads(
        (workspace / ".runtime" / "workspaces.json").read_text(encoding="utf-8")
    )
    assert registry[0]["name"] == "research"
    assert registry[0]["vault_root"] == "research/memory-vault"
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


def _setup_cli_args(tmp_path: Path) -> list[str]:
    return [
        "setup",
        "--workspace",
        str(tmp_path / "workspace"),
        "--auth-token",
        "test-token",
        "--no-auth",
        "--launch-agents-dir",
        str(tmp_path / "LaunchAgents"),
        "--app-dir",
        str(tmp_path / "Applications"),
    ]


def test_setup_exits_nonzero_and_warns_when_memory_regions_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """The memory step never blocks skill sync, but setup must say it failed
    and exit non-zero instead of reporting success (#790). The code is the
    one install-engine.sh tolerates, so a good install is not rolled back."""
    workspace = tmp_path / "workspace"

    def _boom(*a, **k):
        raise RuntimeError("guide unwritable")

    monkeypatch.setattr("ciao.memory_tool.ensure_regions", _boom)
    rc = cli.main(_setup_cli_args(tmp_path))

    assert rc == cli.SETUP_MEMORY_FAILED_RC
    err = capsys.readouterr().err
    assert "memory regions not set up for" in err
    assert "guide unwritable" in err
    # Skills were still synced before the failure was reported. A fresh setup
    # scaffolds assets per agent root, so that is `workspace/personal`.
    skills = workspace / "personal" / ".claude" / "skills"
    assert skills.is_dir()
    assert any(skills.iterdir())


def test_setup_exits_zero_without_warning_on_the_normal_path(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = cli.main(_setup_cli_args(tmp_path))

    assert rc == 0
    assert "memory regions not set up" not in capsys.readouterr().err


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
    assert "CIAO_NATIVE_SIDECAR" not in plist["EnvironmentVariables"]


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


def _write_desktop_app(app_dir: Path) -> Path:
    """Materialize a legacy ``Ciaobot.app``, bundle id and all."""

    app_root = app_dir / "Ciaobot.app"
    macos = app_root / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "ciaobot-desktop").write_text("", encoding="utf-8")
    (app_root / "Contents" / "Info.plist").write_text(
        "<plist><string>local.ciaobot.app</string></plist>", encoding="utf-8"
    )
    return app_root


def test_is_our_app_bundle_rejects_the_legacy_app_bundle(tmp_path: Path) -> None:
    # Same bundle id as our pre-rename launcher; only the executable differs.
    assert cli._is_our_app_bundle(_write_desktop_app(tmp_path)) is False


def test_remove_legacy_app_shortcuts_keeps_the_legacy_app_bundle(tmp_path: Path) -> None:
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
    monkeypatch.setattr(cli.sys, "platform", "darwin")
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
    monkeypatch.setattr(cli.sys, "platform", "darwin")
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


def test_setup_launchctl_failure_never_reads_as_the_memory_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install-engine.sh reads setup's exit 3 as the tolerated memory warning
    and carries on, so a launchctl load that happens to fail with 3 must not
    be reported as one: the install would continue with the agent unloaded
    (#790)."""
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if command[0] != "launchctl":
            return real_run(command, *args, **kwargs)
        return subprocess.CompletedProcess(command, 3 if command[1] == "load" else 0)

    monkeypatch.setattr(cli, "setup_workspace", _stub_setup_for_launchd)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cli.main(
        _launchd_setup_argv(tmp_path / "workspace", tmp_path / "LaunchAgents")
    )

    assert result == 1
    assert result != cli.SETUP_MEMORY_FAILED_RC


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


def test_setup_skips_legacy_companion_when_the_app_is_installed(
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

    assert calls == [["/opt/ciao/claude", "auth", "login"]]


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


# -- skill-proposal-remove --------------------------------------------------


def _skill_proposal_workspace(root: Path, name: str = "2026-08-09-defuddle") -> Path:
    """A workspace whose vault holds one skill proposal in the personal queue."""
    vault = root / "memory-vault"
    personal = vault / "personal"
    # A workspace evidence dir so the bootstrap registry sees a `personal` vault.
    (personal / "Workspace").mkdir(parents=True, exist_ok=True)
    source = personal / "Workspace" / "Skill-Proposals" / f"{name}.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(f"# {name}\n\nA proposed skill.\n", encoding="utf-8")
    return source


def test_cli_skill_proposal_remove_settles_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The agent-driven path the curation schedule uses. It used to unlink the
    file, which left no record that a decision had been made: the next evolution
    pass that saw the same evidence filed the same proposal again as a new file,
    with no way to know anyone had already answered it."""
    from ciao import skill_proposals

    workspace = tmp_path / "workspace"
    source = _skill_proposal_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")

    assert cli.main(["skill-proposal-remove", "defuddle"]) == 0

    assert source.is_file()
    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.lifecycle == skill_proposals.DISMISSED
    assert "Settled skill proposal 2026-08-09-defuddle" in capsys.readouterr().out


def test_cli_skill_proposal_remove_records_the_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Settled through the CLI is still a decision on record, keyed the same way
    the PWA's is, so the queue and the pass agree on what was answered."""
    from ciao.memory_proposals import read_decisions

    workspace = tmp_path / "workspace"
    _skill_proposal_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")

    assert cli.main(["skill-proposal-remove", "defuddle"]) == 0

    rows = read_decisions(
        workspace / "memory-vault" / "personal" / "Workspace" / "Memory-Proposals.md"
    )
    assert [row["text"] for row in rows] == ["skill:2026-08-09-defuddle"]
    assert rows[0]["via"] == "cli"
    assert rows[0]["action"] == "dismissed"


def test_cli_skill_proposal_remove_json_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    source = _skill_proposal_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")

    assert cli.main(["skill-proposal-remove", "2026-08-09-defuddle", "--json"]) == 0

    assert source.is_file()
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "settled": True,
        "name": "2026-08-09-defuddle",
        "workspace": "personal",
        "lifecycle": "dismissed",
    }


def test_cli_skill_proposal_remove_refuses_ambiguous_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    _skill_proposal_workspace(workspace, "2026-08-09-defuddle")
    _skill_proposal_workspace(workspace, "2026-08-16-defuddle")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")

    assert cli.main(["skill-proposal-remove", "defuddle"]) == 1

    err = capsys.readouterr().err
    assert "more than one" in err
    # Nothing was settled.
    queue = workspace / "memory-vault" / "personal" / "Workspace" / "Skill-Proposals"
    assert len(list(queue.glob("*.md"))) == 2


def _accepted_skill_proposal(
    root: Path, name: str = "2026-08-09-defuddle"
) -> Path:
    """A proposal a chat has been given, which is what an outcome records."""
    from ciao import skill_proposals
    from ciao.config import CiaoConfig

    source = _skill_proposal_workspace(root, name)
    config = CiaoConfig.from_env({
        "CIAO_WORKSPACE": str(root),
        "CIAO_VAULT_ROOT": "memory-vault",
        "PWA_AUTH_TOKEN": "test",
    })
    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    skill_proposals.mark_implementing(config, record.id, "chat-1")
    return source


def test_cli_skill_proposal_remove_applied_records_a_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`applied` and `dismissed` are the same shape to a reader and opposite
    facts: one says the change landed, the other that it will not. Only the
    caller that checked the skill may assert the first."""
    from ciao import skill_proposals
    from ciao.memory_proposals import read_decisions

    workspace = tmp_path / "workspace"
    source = _accepted_skill_proposal(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")

    assert cli.main(
        ["skill-proposal-remove", "defuddle", "--applied", "--reason", "verified"]
    ) == 0

    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.lifecycle == skill_proposals.APPLIED
    assert record.chat_id == "chat-1"
    rows = read_decisions(
        workspace / "memory-vault" / "personal" / "Workspace" / "Memory-Proposals.md"
    )
    assert [row["action"] for row in rows] == ["accepted"]
    assert rows[0]["outcome"] == "verified"


def test_cli_skill_proposal_remove_interrupted_leaves_the_proposal_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An implementation that stopped is not an answer. Recording it as a
    dismissal would archive unfinished work as though a person had rejected it,
    and the queue would never ask again."""
    from ciao import skill_proposals
    from ciao.memory_proposals import read_decisions

    workspace = tmp_path / "workspace"
    source = _accepted_skill_proposal(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")

    assert cli.main(["skill-proposal-remove", "defuddle", "--interrupted"]) == 0

    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.lifecycle == skill_proposals.INTERRUPTED
    assert record.chat_id == "chat-1"
    assert read_decisions(
        workspace / "memory-vault" / "personal" / "Workspace" / "Memory-Proposals.md"
    ) == []
    assert "stays queued" in capsys.readouterr().out


def test_cli_skill_proposal_remove_refuses_interrupting_work_that_never_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """There is no implementation to interrupt, so the flag would be a no-op
    dressed as a record. Say so instead."""
    workspace = tmp_path / "workspace"
    source = _skill_proposal_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")

    assert cli.main(["skill-proposal-remove", "defuddle", "--interrupted"]) == 1

    assert "no implementing chat" in capsys.readouterr().err
    from ciao import skill_proposals

    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.lifecycle == skill_proposals.PENDING


def test_cli_skill_proposal_remove_refuses_two_opposite_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    source = _accepted_skill_proposal(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")

    assert cli.main(
        ["skill-proposal-remove", "defuddle", "--applied", "--interrupted"]
    ) == 2

    assert "opposite outcomes" in capsys.readouterr().err
    from ciao import skill_proposals

    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.lifecycle == skill_proposals.IMPLEMENTING


def test_cli_skill_proposal_remove_not_applicable_is_a_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finding that no longer holds is a decision, and the chat implementing it
    has to be able to record one.

    `render_improvement_prompt` tells the implementation chat to settle the
    proposal that way when the skill no longer reads the way the reviewer saw it.
    Without a flag for it the prompt asked for a resolution the command could not
    express, and the branch where a chat concludes an edit is unwarranted had no
    way to answer for itself — so the queue kept asking.
    """
    from ciao import skill_proposals
    from ciao.memory_proposals import read_decisions

    workspace = tmp_path / "workspace"
    source = _accepted_skill_proposal(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")

    assert cli.main(
        [
            "skill-proposal-remove",
            "defuddle",
            "--not-applicable",
            "--reason",
            "the fallback landed already",
            "--json",
        ]
    ) == 0

    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.lifecycle == skill_proposals.NOT_APPLICABLE
    assert record.chat_id == "chat-1"
    rows = read_decisions(
        workspace / "memory-vault" / "personal" / "Workspace" / "Memory-Proposals.md"
    )
    # A dismissal, not a promotion: nothing improved the skill.
    assert [row["action"] for row in rows] == ["dismissed"]
    assert rows[0]["outcome"] == "the fallback landed already"


def test_cli_skill_proposal_remove_refuses_not_applicable_with_another_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """It is a third outcome, not a modifier. `--not-applicable --applied` would
    otherwise be a contradiction resolved by flag order rather than refused."""
    workspace = tmp_path / "workspace"
    source = _accepted_skill_proposal(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")

    assert cli.main(
        ["skill-proposal-remove", "defuddle", "--not-applicable", "--applied"]
    ) == 2

    assert "third outcome" in capsys.readouterr().err
    from ciao import skill_proposals

    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.lifecycle == skill_proposals.IMPLEMENTING


# -- skill-proposal-add ------------------------------------------------------


def _owned_skill_install(root: Path, name: str) -> Path:
    """A workspace whose vault registers `personal` and owns one skill source.

    The canonical owned source is ``<install>/skills/<name>/SKILL.md``: an
    install that has not re-rooted answers every workspace with the install
    root, and one owner is what makes the catalog this workspace's own.
    """
    (root / "memory-vault" / "personal" / "Workspace").mkdir(parents=True, exist_ok=True)
    skill_md = root / "skills" / name / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(f"---\nname: {name}\n---\n\n# {name}\n", encoding="utf-8")
    return skill_md


def _finding(
    tmp_path: Path,
    *,
    name: str = "finding",
    sources: list[dict] | None = None,
    origins: list[dict] | None = None,
) -> str:
    """Write one structured finding, as a pass or a person would, and return it."""
    path = tmp_path / f"{name}.json"
    payload: dict = {
        "title": "notes: read the categories block first",
        "problem": "The user corrected the note type twice.",
        "change": "Add a step: read the Categories block before a type.",
        "rationale": "The correction repeated, so it is reusable.",
        "sources": sources
        if sources is not None
        else [
            {
                "chat_id": "chat-7",
                "archive": "memory-vault/personal/logs/x.md",
                "turn": "3",
                "excerpt": "no, that's a person, not a project",
            }
        ],
    }
    if origins is not None:
        payload["origins"] = origins
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_cli_skill_proposal_add_files_through_the_validated_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One record per skill, with the resolved path and revision on it.

    The pass files what it found in a conversation, and a person can hand-author
    the same thing; both land through ``upsert_proposal``, so the review queue
    sees one identity per skill rather than a file per run. The canonical path
    and revision are the resolver's answers, not the caller's — a reviewer can
    then tell which bytes the proposal was written against.
    """
    from ciao import skill_proposals

    workspace = tmp_path / "workspace"
    skill_md = _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    finding = _finding(tmp_path)

    assert cli.main(["skill-proposal-add", "notes", "--input-file", finding]) == 0

    record_path = (
        workspace / "memory-vault" / "personal" / "Workspace" / "Skill-Proposals" / "notes.md"
    )
    record = skill_proposals.parse_proposal(record_path, "personal")
    assert record is not None
    assert record.id == skill_proposals.proposal_id("personal", "notes")
    assert record.lifecycle == skill_proposals.PENDING
    assert record.canonical_path == str(skill_md)
    assert record.reviewed_revision == hashlib.sha256(skill_md.read_bytes()).hexdigest()
    assert record.problem.startswith("The user corrected")
    assert record.change.startswith("Add a step")
    assert record.sources == (
        skill_proposals.SkillEvidence(
            chat_id="chat-7",
            archive="memory-vault/personal/logs/x.md",
            turn="3",
            excerpt="no, that's a person, not a project",
        ),
    )
    assert "Filed skill proposal for notes in personal" in capsys.readouterr().out


def test_cli_skill_proposal_add_refuses_a_skill_the_workspace_does_not_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stock copy, a mirror and an unknown name are all refused by name.

    The only source an improvement may be written against is the workspace's
    own canonical one. Resolving it here is what makes that true: the caller
    supplies a name, never a path, so nothing it says can aim the writer at an
    installed copy that sync overwrites.
    """
    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    stock = workspace / ".claude" / "skills" / "web-research"
    stock.mkdir(parents=True)
    (stock / ".ciao-stock-skill").write_text("stock\n", encoding="utf-8")
    (stock / "SKILL.md").write_text("# web-research\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    finding = _finding(tmp_path)

    assert cli.main(["skill-proposal-add", "web-research", "--input-file", finding]) == 1
    assert cli.main(["skill-proposal-add", "nonesuch", "--input-file", finding]) == 1

    err = capsys.readouterr().err
    assert "web-research" in err and "nonesuch" in err
    queue = workspace / "memory-vault" / "personal" / "Workspace" / "Skill-Proposals"
    assert not queue.is_dir() or list(queue.glob("*.md")) == []


def test_cli_skill_proposal_add_refuses_a_finding_with_no_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A proposal nobody can check is not filed, whatever its prose says.

    Evidence is the record's whole claim: which session, which turn, which
    words. A finding without it would be indistinguishable from an invention,
    and the review surface has no way to show it is unsupported. A routed lesson
    is the one finding whose evidence is a learning link instead, and the next
    test covers that; this one is the empty-sources case, which stays refused.
    """
    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    finding = _finding(tmp_path, sources=[])

    assert cli.main(["skill-proposal-add", "notes", "--input-file", finding]) == 2

    assert "sources" in capsys.readouterr().err
    queue = workspace / "memory-vault" / "personal" / "Workspace" / "Skill-Proposals"
    assert not queue.is_dir() or list(queue.glob("*.md")) == []


def test_cli_skill_proposal_add_accepts_a_lesson_routed_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lesson with no `sources` is the lesson-routing path, not a loose check.

    The finding is real — a lesson in `Workspace/Learnings.md` applies to a
    skill the conversation never loaded — and the only honest record of it is the
    `origins` link, because a `sources` entry would have to name a `turn` the
    transcript never contained. So the requirement is *one or the other*, and
    the record says which it was.
    """
    from ciao import skill_proposals

    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "unused-skill")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    finding = _finding(
        tmp_path,
        name="lesson",
        sources=None,
        origins=[
            {
                "learning_id": "lrn-1",
                "finding": "the lesson is about a skill this chat never loaded",
                "source_revision": "abc123",
                "summary": "Route it to the skill that covers the workflow",
            }
        ],
    )
    # The lesson route carries no `sources` at all.
    payload = json.loads(Path(finding).read_text(encoding="utf-8"))
    del payload["sources"]
    Path(finding).write_text(json.dumps(payload), encoding="utf-8")

    assert cli.main(["skill-proposal-add", "unused-skill", "--input-file", finding]) == 0

    record = skill_proposals.parse_proposal(
        workspace
        / "memory-vault"
        / "personal"
        / "Workspace"
        / "Skill-Proposals"
        / "unused-skill.md",
        "personal",
    )
    assert record is not None
    # No fabricated source, and the link that replaces it is filed pending.
    assert record.sources == ()
    assert len(record.origins) == 1
    assert record.origins[0].learning_id == "lrn-1"
    assert record.origins[0].state == skill_proposals.ORIGIN_PENDING
    assert record.origins[0].verification == ""


def test_cli_skill_proposal_add_still_refuses_a_finding_with_neither(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One or the other, not neither.

    Dropping the `sources` requirement for routed lessons must not have become
    dropping it: a payload carrying neither is a finding with nothing behind it,
    and the complaint has to name both fields so a caller knows what to add.
    """
    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    finding = _finding(tmp_path, name="bare")
    payload = json.loads(Path(finding).read_text(encoding="utf-8"))
    del payload["sources"]
    Path(finding).write_text(json.dumps(payload), encoding="utf-8")

    assert cli.main(["skill-proposal-add", "notes", "--input-file", finding]) == 2

    err = capsys.readouterr().err
    assert "sources" in err and "origins" in err
    queue = workspace / "memory-vault" / "personal" / "Workspace" / "Skill-Proposals"
    assert not queue.is_dir() or list(queue.glob("*.md")) == []


def test_cli_skill_proposal_add_refuses_a_settled_origin_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A filing is a question; only `skill-proposal-remove` answers one.

    A payload carrying its own `state` could declare the lesson applied and skip
    the verification an applied needs, so the key is refused by name rather than
    read and dropped — the same rule the origin reader has always applied.
    """
    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    finding = _finding(
        tmp_path,
        name="preset",
        origins=[{"learning_id": "lrn-1", "finding": "x", "state": "applied"}],
    )

    assert cli.main(["skill-proposal-add", "notes", "--input-file", finding]) == 2
    assert "state" in capsys.readouterr().err


def test_cli_skill_proposal_add_still_refuses_a_nonexistent_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The new-skill path does not come through here, and must not start to.

    `resolve_owned_skill` requires an existing owned source, and a lesson about
    a skill that does not exist yet is a `[review]` draft for a person to create
    — not a filer pointed at a name with no file behind it.
    """
    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    finding = _finding(tmp_path, name="nonesuch", sources=None, origins=[{"learning_id": "l", "finding": "f"}])

    assert cli.main(["skill-proposal-add", "does-not-exist", "--input-file", finding]) == 1
    err = capsys.readouterr().err
    assert "does-not-exist" in err and "owns no canonical source" in err


def test_cli_skill_proposal_add_reads_the_finding_from_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Shell metacharacters in the finding survive, because none of it is argv.

    Every field is text out of a conversation, so `$()`, backticks and quotes
    arrive in a file. A finding whose excerpt says "run $(rm -rf /)" has to
    reach the record as those characters, not as a command.
    """
    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    excerpt = 'never pipe into `sh -c` and never run $(whoami) here'
    finding = _finding(
        tmp_path,
        sources=[
            {"chat_id": "chat-7", "archive": "x.md", "turn": "2", "excerpt": excerpt}
        ],
    )

    assert cli.main(["skill-proposal-add", "notes", "--input-file", finding, "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["filed"] is True
    assert payload["skill"] == "notes"
    assert payload["lifecycle"] == "pending"
    assert payload["evidence"] == 1
    body = (
        workspace
        / "memory-vault"
        / "personal"
        / "Workspace"
        / "Skill-Proposals"
        / "notes.md"
    ).read_text(encoding="utf-8")
    assert excerpt in body


def test_cli_skill_proposal_add_merges_new_evidence_into_one_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second finding for the same skill updates the row rather than forking it.

    This is what makes the queue reviewable: the file is named after the skill,
    so two passes seeing the same skill cannot produce two rows a reviewer has
    to group to read as one.
    """
    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")

    first = _finding(
        tmp_path,
        name="first",
        sources=[{"chat_id": "chat-1", "archive": "a.md", "turn": "1", "excerpt": "one"}],
    )
    second = _finding(
        tmp_path,
        name="second",
        sources=[{"chat_id": "chat-2", "archive": "b.md", "turn": "4", "excerpt": "two"}],
    )
    assert cli.main(["skill-proposal-add", "notes", "--input-file", first]) == 0
    assert cli.main(["skill-proposal-add", "notes", "--input-file", second]) == 0

    queue = workspace / "memory-vault" / "personal" / "Workspace" / "Skill-Proposals"
    assert [path.name for path in sorted(queue.glob("*.md"))] == ["notes.md"]
    body = (queue / "notes.md").read_text(encoding="utf-8")
    assert "one" in body and "two" in body


def test_cli_skill_proposal_add_does_not_reopen_a_settled_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A decision stands; the finding only adds evidence to it.

    Re-filing is the pass's ordinary move — the same conversation can be
    archived again — so a proposal the owner already answered must not walk
    back into the queue because the skill was used once more.
    """
    from ciao import skill_proposals

    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    finding = _finding(tmp_path)

    assert cli.main(["skill-proposal-add", "notes", "--input-file", finding]) == 0
    assert cli.main(["skill-proposal-remove", "notes"]) == 0
    assert cli.main(["skill-proposal-add", "notes", "--input-file", finding]) == 0

    record_path = (
        workspace / "memory-vault" / "personal" / "Workspace" / "Skill-Proposals" / "notes.md"
    )
    record = skill_proposals.parse_proposal(record_path, "personal")
    assert record is not None
    assert record.lifecycle == skill_proposals.DISMISSED
    assert "already dismissed" in capsys.readouterr().out


def test_cli_skill_proposal_add_reports_an_unreadable_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    broken = tmp_path / "broken.json"
    broken.write_text("not json at all", encoding="utf-8")

    assert cli.main(["skill-proposal-add", "notes", "--input-file", str(broken)]) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_cli_skill_proposal_add_requires_the_input_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no other door: a finding cannot be typed as an argument."""
    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["skill-proposal-add", "notes"])
    assert excinfo.value.code == 2


# -- skill-proposal origins -------------------------------------------------


def _origin_input(**overrides: object) -> dict:
    """One learning link in a filed finding, as a routed pass would write it."""
    entry: dict = {
        "learning_id": "5d6b0a1e-6f4a-5b1c-9d2e-3a4b5c6d7e8f",
        "finding": "read the Categories block before a type",
        "source_revision": "c" * 64,
        "summary": "Add the Categories step.",
    }
    entry.update(overrides)
    return entry


def test_cli_skill_proposal_add_accepts_a_learning_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A finding that came from a learnings entry says so, and the link is what
    a later settlement folds — so it has to reach the record, in the workspace
    that minted the id, or the learning behind it can never be retired."""
    from ciao import skill_proposals

    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    finding = _finding(tmp_path, origins=[_origin_input()])

    assert cli.main(["skill-proposal-add", "notes", "--input-file", finding, "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["origins"] == 1
    record = skill_proposals.parse_proposal(
        workspace
        / "memory-vault"
        / "personal"
        / "Workspace"
        / "Skill-Proposals"
        / "notes.md",
        "personal",
    )
    assert record is not None
    assert len(record.origins) == 1
    # The workspace is this queue's, decided here rather than taken from the
    # payload: an id only means something where it was minted.
    assert record.origins[0].workspace == "personal"
    assert record.origins[0].state == skill_proposals.ORIGIN_PENDING


def test_cli_skill_proposal_add_files_a_finding_with_no_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Most findings are a correction the user made, with no learning behind them.
    Omitting the links entirely is the normal case and must stay cheap."""
    from ciao import skill_proposals

    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")

    assert cli.main(["skill-proposal-add", "notes", "--input-file", _finding(tmp_path)]) == 0

    record = skill_proposals.parse_proposal(
        workspace
        / "memory-vault"
        / "personal"
        / "Workspace"
        / "Skill-Proposals"
        / "notes.md",
        "personal",
    )
    assert record is not None and record.origins == ()


@pytest.mark.parametrize(
    ("origins", "because"),
    [
        ("not a list", "must be a list"),
        (["not an object"], "must be an object"),
        ([{"finding": "x"}], 'needs a non-empty "learning_id"'),
        ([{"learning_id": "x"}], 'needs a non-empty "finding"'),
        ([_origin_input(learning_id=7)], '"learning_id" must be a string'),
        ([_origin_input(source_revision=["x"])], '"source_revision" must be a string'),
        ([_origin_input(nonsense="x")], "unknown field"),
        ([_origin_input(state="applied")], "unknown field"),
        ([_origin_input(verification="read it back and it is there")], "unknown field"),
    ],
)
def test_cli_skill_proposal_add_fails_closed_on_a_malformed_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    origins: object,
    because: str,
) -> None:
    """A half-read link is a learning that looks settled and is not. Refusing the
    whole finding is the honest answer: the caller is told which entry is wrong
    and nothing lands on disk for it to be half-attributed later."""
    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    finding = _finding(tmp_path, origins=origins)

    assert cli.main(["skill-proposal-add", "notes", "--input-file", finding]) == 2

    assert because in capsys.readouterr().err
    queue = workspace / "memory-vault" / "personal" / "Workspace" / "Skill-Proposals"
    assert not queue.is_dir() or list(queue.glob("*.md")) == []


def test_cli_skill_proposal_add_refuses_a_link_from_another_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A learning id is minted inside a workspace, so an id from another one says
    nothing here. Filing it anyway would make a foreign id look like a link this
    queue had verified — and that link is what retires a learning."""
    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    finding = _finding(tmp_path, origins=[_origin_input(workspace="work")])

    assert cli.main(["skill-proposal-add", "notes", "--input-file", finding]) == 2

    err = capsys.readouterr().err
    assert "work" in err and "personal" in err
    queue = workspace / "memory-vault" / "personal" / "Workspace" / "Skill-Proposals"
    assert not queue.is_dir() or list(queue.glob("*.md")) == []


def test_cli_skill_proposal_add_refuses_a_finding_that_claims_to_be_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A finding is filed as a question, so a payload that arrives declaring its
    own finding ``applied`` is asserting a decision nobody made. Stored as filed,
    that would clear the learning behind it — skipping the verification an
    applied needs to mean anything, and standing in for a rejection a dismissed
    needs — and let the cleanup path retire a lesson nobody applied or rejected.
    Refused by name, and both fields named so the filer can fix the payload."""
    from ciao import skill_proposals

    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    finding = _finding(
        tmp_path,
        origins=[
            _origin_input(state="applied", verification="read it back and it is there")
        ],
    )

    assert cli.main(["skill-proposal-add", "notes", "--input-file", finding]) == 2

    err = capsys.readouterr().err
    assert "state" in err and "verification" in err
    queue = workspace / "memory-vault" / "personal" / "Workspace" / "Skill-Proposals"
    assert not queue.is_dir() or list(queue.glob("*.md")) == []
    # The vocabulary still exists — this is a refusal, not a field the queue
    # stopped speaking: `skill-proposal-remove` is what writes a state.
    assert "applied" in skill_proposals.ORIGIN_STATES


def test_cli_skill_proposal_add_tells_the_filer_the_links_landed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A link nobody knows landed is a learning that never goes quiet and nobody
    can say why. The person filing gets to see it."""
    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    finding = _finding(tmp_path, origins=[_origin_input()])

    assert cli.main(["skill-proposal-add", "notes", "--input-file", finding]) == 0

    assert "Linked 1 learning finding" in capsys.readouterr().out


# -- skill-proposal-remove: settling a linked finding -----------------------


LEARNING_ID = "5d6b0a1e-6f4a-5b1c-9d2e-3a4b5c6d7e8f"


def _linked_workspace(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    """An install owning one skill, filed against two findings of one learning."""
    workspace = root / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(root)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    finding = _finding(
        root,
        origins=[
            _origin_input(learning_id=LEARNING_ID, finding="read the Categories block"),
            _origin_input(learning_id=LEARNING_ID, finding="read it back afterwards"),
        ],
    )
    assert cli.main(["skill-proposal-add", "notes", "--input-file", finding]) == 0
    return workspace


def _stored_record(workspace: Path):
    from ciao import skill_proposals

    record = skill_proposals.parse_proposal(
        workspace
        / "memory-vault"
        / "personal"
        / "Workspace"
        / "Skill-Proposals"
        / "notes.md",
        "personal",
    )
    assert record is not None
    return record


def test_cli_skill_proposal_remove_applied_needs_a_verification_on_a_linked_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command the implementation prompt already names must not quietly
    retire a lesson. A finished chat and a row leaving the queue are the two
    things the CLI can see for itself, and neither is the lesson being in the
    skill — so the command says what is missing and settles nothing."""
    from ciao import skill_proposals

    workspace = _linked_workspace(monkeypatch, tmp_path)
    capsys.readouterr()

    assert cli.main(["skill-proposal-remove", "notes", "--applied"]) == 1

    assert "needs a verification" in capsys.readouterr().err
    record = _stored_record(workspace)
    assert record.lifecycle == skill_proposals.PENDING
    assert [origin.state for origin in record.origins] == [
        skill_proposals.ORIGIN_PENDING
    ] * 2


def test_cli_skill_proposal_remove_records_a_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The way out, on the command the prompt already spells: name the receipt or
    the readback, and the linked finding is applied with its proof on record."""
    from ciao import skill_proposals

    workspace = _linked_workspace(monkeypatch, tmp_path)
    capsys.readouterr()

    assert (
        cli.main(
            [
                "skill-proposal-remove",
                "notes",
                "--applied",
                "--verification",
                "read SKILL.md back: the Categories step is there",
            ]
        )
        == 0
    )

    record = _stored_record(workspace)
    assert record.lifecycle == skill_proposals.APPLIED
    assert [origin.state for origin in record.origins] == [
        skill_proposals.ORIGIN_APPLIED
    ] * 2
    assert record.origins[0].verification == (
        "read SKILL.md back: the Categories step is there"
    )


def test_cli_skill_proposal_remove_settles_one_linked_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A record is one row per skill, so a chat that dealt with one of its
    findings has not dealt with the rest. The CLI can say which, and reports
    what is still outstanding."""
    from ciao import skill_proposals

    workspace = _linked_workspace(monkeypatch, tmp_path)
    capsys.readouterr()

    assert (
        cli.main(
            [
                "skill-proposal-remove",
                "notes",
                "--applied",
                "--learning-id",
                LEARNING_ID,
                "--finding",
                "read the Categories block",
                "--verification",
                "mrcpt_abc123",
                "--json",
            ]
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)
    assert result["lifecycle"] == "pending"
    assert [origin["state"] for origin in result["origins"]] == [
        skill_proposals.ORIGIN_APPLIED,
        skill_proposals.ORIGIN_PENDING,
    ]
    assert _stored_record(workspace).lifecycle == skill_proposals.PENDING


def test_cli_skill_proposal_remove_refuses_a_finding_without_a_learning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A finding text on its own names nothing, and guessing which learning it
    belonged to is exactly the kind of silent link this shape exists to stop."""
    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    finding = _finding(tmp_path)
    assert cli.main(["skill-proposal-add", "notes", "--input-file", finding]) == 0
    capsys.readouterr()

    assert (
        cli.main(["skill-proposal-remove", "notes", "--applied", "--finding", "x"]) == 2
    )

    assert "--learning-id" in capsys.readouterr().err


def test_cli_skill_proposal_remove_reports_a_learning_it_does_not_carry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Naming a learning this proposal does not link is a bug worth hearing
    about, not a decision worth recording."""
    workspace = tmp_path / "workspace"
    _owned_skill_install(workspace, "notes")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    finding = _finding(tmp_path)
    assert cli.main(["skill-proposal-add", "notes", "--input-file", finding]) == 0
    capsys.readouterr()

    assert (
        cli.main(["skill-proposal-remove", "notes", "--learning-id", LEARNING_ID]) == 1
    )

    assert "links no learning" in capsys.readouterr().err


def _search_note(vault: Path, name: str) -> None:
    (vault / "People").mkdir(parents=True, exist_ok=True)
    (vault / "People" / f"{name}.md").write_text(
        f"---\ntype: person\ntitle: {name}\n---\n# {name}\n\nLoves kiteboarding.\n",
        encoding="utf-8",
    )


def test_cli_vault_search_logs_never_returns_a_sibling_agent_roots_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same cross-workspace leak, by way of `--logs`.

    Scoping the note query left the transcript query unscoped, because
    `search_logs` had no `path_prefix` at all — so `vault-search --logs` still
    printed another workspace's archived chats out of the shared database.
    """
    from ciao import fts_search

    install = tmp_path / "install"
    work_vault = install / "work" / "memory-vault"
    personal_vault = install / "personal" / "memory-vault"
    work_vault.mkdir(parents=True)
    personal_logs = personal_vault / "Logs" / "Chats"
    personal_logs.mkdir(parents=True)
    (personal_logs / "chat-alba.md").write_text(
        "# Chat\n\nWe talked about kiteboarding with Alba.\n", encoding="utf-8"
    )

    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("CIAO_WORKSPACE", str(install))
    monkeypatch.delenv("CIAO_VAULT_ROOT", raising=False)
    monkeypatch.delenv("CIAO_RUNTIME_ROOT", raising=False)

    # The sibling root's transcripts are already indexed, as the migration
    # rebuild leaves them.
    conn = sqlite3.connect(fts_search.get_db_path())
    try:
        fts_search.init_db(conn)
        fts_search.index_logs(
            conn,
            personal_vault,
            logs_root=personal_logs,
            path_base=install,
        )
    finally:
        conn.close()

    assert (
        cli.main(
            ["vault-search", "kiteboarding", "--logs", "--vault-root", str(work_vault)]
        )
        == 0
    )

    assert "Alba" not in capsys.readouterr().out


def test_cli_vault_search_never_returns_a_sibling_agent_roots_notes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cross-workspace leak: the FTS database is shared by every re-rooted agent
    root — the migration rebuild fills it that way on purpose — and the prune is
    now scoped, so it keeps the sibling roots' rows. An unscoped query therefore
    printed another workspace's note titles and snippets."""
    from ciao import fts_search

    install = tmp_path / "install"
    work_vault = install / "work" / "memory-vault"
    personal_vault = install / "personal" / "memory-vault"
    _search_note(work_vault, "Aymen")
    _search_note(personal_vault, "Alba")

    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("CIAO_WORKSPACE", str(install))
    monkeypatch.delenv("CIAO_VAULT_ROOT", raising=False)
    monkeypatch.delenv("CIAO_RUNTIME_ROOT", raising=False)

    # The other root's rows are already in the shared database, exactly as the
    # migration rebuild leaves them.
    conn = sqlite3.connect(fts_search.get_db_path())
    try:
        fts_search.init_db(conn)
        fts_search.index_vault(conn, personal_vault, path_base=install)
    finally:
        conn.close()

    assert cli.main(["vault-search", "kiteboarding", "--vault-root", str(work_vault)]) == 0

    out = capsys.readouterr().out
    assert "Aymen" in out  # this workspace's own note still resolves
    assert "Alba" not in out
    # And the link points at the note that actually exists on disk.
    assert str(work_vault / "People" / "Aymen.md") in out


def test_critique_is_reachable_through_the_ciao_entry_point(monkeypatch):
    """`/critique` must not depend on an external `python3`.

    An install puts only a `ciao` entry point on PATH and no bare `python3`
    from the same tree, so `python3 -m ciao.critique` resolves whatever
    interpreter the user's shell has — one with neither `ciao` nor its
    dependencies. The command doc therefore names `ciao critique`, and this
    pins that it works.
    """
    from ciao import cli

    seen: list[list[str]] = []

    def fake_main(argv):
        seen.append(list(argv))
        return 0

    monkeypatch.setattr("ciao.critique.main", fake_main)
    assert cli.main(["critique", "--input", "a.md", "--type", "plan"]) == 0
    # Flags reach the panel untouched: argparse must not eat them on the way.
    assert seen == [["--input", "a.md", "--type", "plan"]]


def test_ciao_help_lists_critique():
    """Registered as a subparser too, so `ciao --help` discloses it."""
    from ciao import cli

    parser = cli.build_parser()
    action = next(a for a in parser._subparsers._actions if hasattr(a, "choices") and a.choices)
    assert "critique" in action.choices


def test_critique_leaves_a_home_relative_input_for_expanduser(monkeypatch):
    """A quoted `~/...` reaches the CLI unexpanded and must not be rebased.

    `Path("~/x").is_absolute()` is False, so rebasing it against the caller's
    directory yields `<cwd>/~/x` and defeats the `expanduser()` that
    `ciao.critique` does later — an artifact that resolved fine before would be
    reported missing.
    """
    from ciao import cli

    monkeypatch.setenv("CIAO_INVOCATION_CWD", "/some/workspace")
    seen: list[list[str]] = []
    monkeypatch.setattr("ciao.critique.main", lambda argv: seen.append(list(argv)) or 0)

    assert cli.main(["critique", "--input", "~/Documents/plan.md"]) == 0
    assert seen == [["--input", "~/Documents/plan.md"]]

    seen.clear()
    assert cli.main(["critique", "--input=~/plan.md"]) == 0
    assert seen == [["--input=~/plan.md"]]


def _write_installed_launch_agent(
    agents: Path, workspace: Path, *, runtime_root: str = ".runtime"
) -> None:
    import plistlib

    agents.mkdir(parents=True, exist_ok=True)
    (agents / "com.ciao.server.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": "com.ciao.server",
                "WorkingDirectory": str(workspace),
                "EnvironmentVariables": {
                    "CIAO_WORKSPACE": str(workspace),
                    "CIAO_RUNTIME_ROOT": runtime_root,
                },
            }
        )
    )


def _per_root_workspace(root: Path) -> None:
    """Scaffold a migrated per-workspace install like setup_workspace does."""
    from ciao.cli import setup_workspace

    setup_workspace(root, auth_token="t", auth_required=True)


def test_cli_health_reports_the_installed_workspace_from_a_bare_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`ciao health get` with no CIAO_WORKSPACE in the environment must report
    on the workspace the installed server's LaunchAgent points at.

    The fallback used to be the bootstrap workspace, so a bare-shell probe
    (exactly what the desktop-install skill runs after an install) manufactured
    `~/.ciao/bootstrap`, resolved the LEGACY shared-vault layout there, and
    warned about a memory vault that was actually healthy. It also created
    `~/.ciao/bootstrap` as a side effect of a read-only diagnostic.
    """
    from ciao.config import reset_reroot_cache

    workspace = tmp_path / "workspace"
    _per_root_workspace(workspace)
    agents = tmp_path / "LaunchAgents"
    _write_installed_launch_agent(agents, workspace)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(agents))
    for name in (
        "CIAO_WORKSPACE",
        "CIAO_RUNTIME_ROOT",
        "CIAO_VAULT_ROOT",
        "CIAO_BOOTSTRAP_WORKSPACE",
    ):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    reset_reroot_cache()
    try:
        assert cli.main(["health", "get"]) == 0
    finally:
        reset_reroot_cache()
    out = capsys.readouterr().out
    # setup_workspace prints skill-install progress lines ahead of the JSON.
    report = json.loads(out[out.index("{"):])
    assert report["status"] == "ok", [c for c in report["checks"] if c["status"] != "ok"]
    # The probe is read-only: no bootstrap workspace may appear as a side effect.
    assert not (home / ".ciao" / "bootstrap").exists()
    # Read-only also means the caller's environment: the workspace .env must be
    # folded into the config resolution, not loaded into os.environ (load_dotenv
    # sets keys it has never seen and nothing restores them, so the probe would
    # leak the install's token into the shell that ran it).
    import os

    assert os.environ.get("CIAO_WORKSPACE") is None
    assert os.environ.get("PWA_AUTH_TOKEN") != "t"


def test_config_discovery_applies_the_workspace_auth_before_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare-shell `ciao run` against a stopped install must adopt the
    workspace .env's auth settings, not the bare shell's absence of them.

    Discovery used to apply its overlay after PWA_AUTH_TOKEN and
    PWA_AUTH_REQUIRED were parsed, so the started server ignored the
    workspace's configured password and booted unauthenticated.
    """
    from ciao.config import CiaoConfig, reset_reroot_cache

    workspace = tmp_path / "workspace"
    _per_root_workspace(workspace)
    # A distinctive token in the .env, as a configured install has.
    env_path = workspace / ".env"
    env_path.write_text(
        (env_path.read_text(encoding="utf-8")).replace("PWA_AUTH_TOKEN=t", "PWA_AUTH_TOKEN=ws-secret-token")
        + "PWA_AUTH_REQUIRED=true\n",
        encoding="utf-8",
    )
    agents = tmp_path / "LaunchAgents"
    _write_installed_launch_agent(agents, workspace)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(agents))
    for name in (
        "CIAO_WORKSPACE",
        "CIAO_RUNTIME_ROOT",
        "CIAO_VAULT_ROOT",
        "CIAO_BOOTSTRAP_WORKSPACE",
        "PWA_AUTH_TOKEN",
        "PWA_AUTH_REQUIRED",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()

    reset_reroot_cache()
    try:
        config = CiaoConfig.from_env()
    finally:
        reset_reroot_cache()

    assert config.workspace_root == workspace.resolve()
    assert config.pwa_auth_token == "ws-secret-token"
    assert config.pwa_auth_required is True


def test_config_discovery_survives_an_exported_empty_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exported-but-empty CIAO_WORKSPACE must not beat the discovered pin.

    Discovery is entered for an empty value as well as an unset one (a bare
    `export CIAO_WORKSPACE=` in a shell profile), but the process environment
    wins the overlay merge, so the empty string overrode the pin set inside it.
    That produced exactly the hybrid the pinning exists to prevent: the
    installed workspace's auth and provider settings applied to a freshly
    manufactured bootstrap root, because `bootstrap_mode` still saw no
    workspace.
    """
    from ciao.config import CiaoConfig, reset_reroot_cache

    workspace = tmp_path / "workspace"
    _per_root_workspace(workspace)
    env_path = workspace / ".env"
    env_path.write_text(
        (env_path.read_text(encoding="utf-8")).replace(
            "PWA_AUTH_TOKEN=t", "PWA_AUTH_TOKEN=ws-secret-token"
        ),
        encoding="utf-8",
    )
    agents = tmp_path / "LaunchAgents"
    _write_installed_launch_agent(agents, workspace)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(agents))
    for name in (
        "CIAO_RUNTIME_ROOT",
        "CIAO_VAULT_ROOT",
        "CIAO_BOOTSTRAP_WORKSPACE",
        "PWA_AUTH_TOKEN",
        "PWA_AUTH_REQUIRED",
    ):
        monkeypatch.delenv(name, raising=False)
    # The whole point: present in the environment, but empty.
    monkeypatch.setenv("CIAO_WORKSPACE", "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()

    reset_reroot_cache()
    try:
        config = CiaoConfig.from_env()
    finally:
        reset_reroot_cache()

    # The discovered install wins, so the .env and the root agree.
    assert config.workspace_root == workspace.resolve()
    assert config.pwa_auth_token == "ws-secret-token"


# -- skill-draft-* (#728-D) --------------------------------------------------


def _draft_workspace(tmp_path: Path) -> Path:
    """An install whose vault registers `personal`."""
    root = tmp_path / "workspace"
    (root / "memory-vault" / "personal" / "Workspace").mkdir(parents=True, exist_ok=True)
    return root


def _draft_payload(tmp_path: Path, **overrides) -> str:
    path = tmp_path / "draft.json"
    payload = {
        "target": "upstream_issue",
        "skill": "web-research",
        "title": "Document the offline fallback for a timed-out fetch",
        "change": "State that a timed-out fetch is retried once before reporting.",
        "body": "A fetch that times out is a transport failure, not an empty answer.",
        "repository": "example/tools",
        "version": "1.4.0",
        "private_evidence": "chat-42 turn 7",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_cli_skill_draft_add_files_a_review_row_and_writes_nothing_public(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Filing is local and complete: a queue row and a record, and no `gh`.

    The whole unattended contract is that this step is safe to run with nobody
    watching, so the command that does it must not reach the network — a test
    that only checked the row would pass with a `gh` call in the middle.
    """
    from ciao import upstream_drafts

    called: list[list[str]] = []
    monkeypatch.setattr(
        upstream_drafts, "_run_gh", lambda args, timeout=30.0: called.append(list(args))
    )
    workspace = _draft_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    draft = _draft_payload(tmp_path)

    assert cli.main(["skill-draft-add", "--input-file", draft, "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["filed"] is True
    assert payload["target"] == "upstream_issue"
    assert called == []
    assert upstream_drafts.find_draft(_workspace_config(workspace), payload["id"]) is not None


def test_cli_skill_draft_add_refuses_a_body_that_would_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate is at the door, so a leaking body is never written at all."""
    workspace = _draft_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    draft = _draft_payload(
        tmp_path, body="it happened in turn 4 of chat-1 and I logged it"
    )

    assert cli.main(["skill-draft-add", "--input-file", draft]) == 2
    assert "cannot file the draft" in capsys.readouterr().err
    sidecars = (
        workspace / "memory-vault" / "personal" / "Workspace" / "Skill-Drafts"
    )
    assert not sidecars.is_dir() or list(sidecars.glob("*.json")) == []


def test_cli_skill_draft_add_refuses_an_unknown_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No new top-level queue kind, so an unknown target is not a new kind.

    A draft is a routing decision, which is what `[review]` already means, and a
    command that accepted an arbitrary target string would be inventing kinds
    the three shared bullet readers never learned to parse.
    """
    workspace = _draft_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    draft = _draft_payload(tmp_path, target="brand_new_kind")

    assert cli.main(["skill-draft-add", "--input-file", draft]) == 2
    err = capsys.readouterr().err
    assert "upstream_issue" in err and "new_skill" in err


def test_cli_skill_drafts_lists_what_is_open_and_what_is_settled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _draft_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    draft = _draft_payload(tmp_path)
    assert cli.main(["skill-draft-add", "--input-file", draft, "--json"]) == 0
    identity = json.loads(capsys.readouterr().out)["id"]
    capsys.readouterr()

    assert cli.main(["skill-drafts", "--json"]) == 0
    open_rows = json.loads(capsys.readouterr().out)["drafts"]
    assert [row["id"] for row in open_rows] == [identity]

    assert cli.main(["skill-draft-reject", identity, "--reason", "covered", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["skill-drafts", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["drafts"] == []
    assert cli.main(["skill-drafts", "--all", "--json"]) == 0
    settled = json.loads(capsys.readouterr().out)["drafts"]
    assert [row["lifecycle"] for row in settled] == ["rejected"]


def test_cli_skill_draft_approve_searches_before_it_creates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The attended step, driven through the command, links an existing issue."""
    from ciao import upstream_drafts

    monkeypatch.setattr(
        upstream_drafts, "search_existing_issues", lambda **_: ["https://example/3"]
    )
    created: list[dict] = []
    monkeypatch.setattr(
        upstream_drafts, "create_issue", lambda **kw: created.append(kw) or "https://example/9"
    )
    workspace = _draft_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    assert cli.main(["skill-draft-add", "--input-file", _draft_payload(tmp_path), "--json"]) == 0
    identity = json.loads(capsys.readouterr().out)["id"]

    assert cli.main(["skill-draft-approve", identity, "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["lifecycle"] == "filed"
    assert payload["issue_url"] == "https://example/3"
    assert created == []


def test_cli_skill_draft_approve_creates_the_new_skill_from_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The new-skill route creates from the file it is given, then settles.

    The content comes from a file for the same reason every other finding does
    (a skill body is prose a shell would mangle), and the row settles only
    after the write — so an interrupted run leaves a draft a person can retry
    rather than a "created" record with no file.
    """
    workspace = _draft_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    draft = _draft_payload(
        tmp_path,
        target="new_skill",
        skill="invoice-recon",
        body="",
        change="Create skills/invoice-recon with a trigger and one step.",
    )
    assert cli.main(["skill-draft-add", "--input-file", draft, "--json"]) == 0
    identity = json.loads(capsys.readouterr().out)["id"]
    content = tmp_path / "SKILL.md"
    content.write_text(
        "---\nname: invoice-recon\ndescription: Reconcile an invoice\n---\n\n"
        "Match the invoice number first.\n",
        encoding="utf-8",
    )

    assert cli.main(
        ["skill-draft-approve", identity, "--content-file", str(content), "--json"]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["lifecycle"] == "filed"
    assert payload["issue_url"].endswith("skills/invoice-recon/SKILL.md")
    assert (workspace / "skills" / "invoice-recon" / "SKILL.md").is_file()


def test_cli_skill_draft_approve_needs_content_for_a_new_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A creation with no content would write an empty skill, so it is refused."""
    workspace = _draft_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    draft = _draft_payload(tmp_path, target="new_skill", skill="invoice-recon", body="")
    assert cli.main(["skill-draft-add", "--input-file", draft, "--json"]) == 0
    identity = json.loads(capsys.readouterr().out)["id"]

    assert cli.main(["skill-draft-approve", identity]) == 2
    assert "--content-file" in capsys.readouterr().err
    assert not (workspace / "skills" / "invoice-recon").exists()


def test_cli_skill_draft_approve_reports_an_unknown_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _draft_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")

    assert cli.main(["skill-draft-approve", "nope"]) == 1
    assert "no open draft" in capsys.readouterr().err
    assert cli.main(["skill-draft-reject", "nope"]) == 1


def _run_draft_add(capsys: pytest.CaptureFixture[str], payload: str) -> str:
    """File a draft through the CLI and return its ``--json`` stdout."""
    assert cli.main(["skill-draft-add", "--input-file", payload, "--json"]) == 0
    return capsys.readouterr().out


def test_an_unattended_run_cannot_file_create_or_settle_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command an automation actually shells, refused with its own exit code.

    `approve_draft(unattended=...)` only refused a caller that said so, and the
    CLI passed `False` on every path — so the flag proved nothing and a nightly
    Workspace care run could file a public issue, create a skill, or settle a
    draft by running the very command a person runs. Attendedness is read from
    the run's own curation lease here, so there is nothing to pass and nothing to
    override. The exit code is distinct from 1 because nothing failed: the
    request was well-formed and the answer is that no reviewer is present.
    """
    from ciao import upstream_drafts
    from ciao.curation_run import begin_run

    reached: list[str] = []
    monkeypatch.setattr(
        upstream_drafts, "create_issue", lambda **kw: reached.append("create") or "u"
    )
    monkeypatch.setattr(
        upstream_drafts,
        "search_existing_issues",
        lambda **kw: reached.append("search") or [],
    )
    workspace = _draft_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    config = _workspace_config(workspace)
    stock = json.loads(_run_draft_add(capsys, _draft_payload(tmp_path)))["id"]
    new = json.loads(
        _run_draft_add(
            capsys,
            _draft_payload(
                tmp_path,
                target="new_skill",
                skill="invoice-recon",
                body="",
                change="Create skills/invoice-recon with a trigger and one step.",
            ),
        )
    )["id"]
    content = tmp_path / "SKILL.md"
    content.write_text(
        "---\nname: invoice-recon\ndescription: Reconcile an invoice\n---\n\n"
        "Match the invoice number first.\n",
        encoding="utf-8",
    )
    begin_run(
        Path(config.workspace_vault_root("personal")), holder="nightly:1", ttl_s=600
    )

    refused = cli.UNATTENDED_REFUSED_EXIT
    assert cli.main(["skill-draft-approve", stock, "--json"]) == refused
    assert "unattended" in capsys.readouterr().err
    assert cli.main(
        ["skill-draft-approve", new, "--content-file", str(content), "--json"]
    ) == refused
    capsys.readouterr()
    assert cli.main(["skill-draft-reject", new, "--reason", "no"]) == refused
    capsys.readouterr()

    # Nothing reached GitHub, no skill was written, and both rows are still
    # queued — which is what the run reports under "What needs you".
    assert reached == []
    assert not (workspace / "skills" / "invoice-recon").exists()
    assert cli.main(["skill-drafts", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["drafts"]
    assert sorted(row["id"] for row in rows) == sorted([stock, new])
    assert all(row["lifecycle"] == "pending" for row in rows)



def _workspace_config(root: Path):
    """The config the CLI itself would build for ``root``, for a direct assertion."""
    from ciao.config import CiaoConfig

    return CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "test-token",
        "CIAO_WORKSPACE": str(root),
        "CIAO_VAULT_ROOT": str(root / "memory-vault"),
    })


def test_cli_skill_draft_add_says_when_the_target_disagrees_with_the_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The routing decision is a real answer, and the filer is told it.

    A pass that files an upstream-issue draft for a skill this workspace owns
    under `skills/` is filing blind, and the note names the command that would
    actually apply the change locally. It is a note rather than a refusal because
    a forked packaged skill is owned *and* still worth reporting upstream.
    """
    workspace = tmp_path / "workspace"
    (workspace / "memory-vault" / "personal" / "Workspace").mkdir(parents=True)
    skill_md = workspace / "skills" / "notes" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: notes\n---\n\n# notes\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    draft = _draft_payload(tmp_path, skill="notes")

    assert cli.main(["skill-draft-add", "--input-file", draft]) == 0

    out = capsys.readouterr().out
    assert "is a source this workspace owns under skills/" in out
    assert "ciao skill-proposal-add" in out


def test_cli_skill_draft_add_says_a_new_skill_name_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The creation would be refused later; the filer is told now.

    Naming an existing skill as a *new* one means the approval is going to fail
    on a collision, and a person who has not been told that reads the refusal as
    a bug rather than as a routing mistake.
    """
    workspace = tmp_path / "workspace"
    (workspace / "memory-vault" / "personal" / "Workspace").mkdir(parents=True)
    skill_md = workspace / "skills" / "notes" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: notes\n---\n\n# notes\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    draft = _draft_payload(tmp_path, target="new_skill", skill="notes", body="")

    assert cli.main(["skill-draft-add", "--input-file", draft]) == 0

    out = capsys.readouterr().out
    assert "already exists here, so this is not a new skill" in out


def test_cli_skill_draft_add_says_when_there_is_nothing_to_report_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No local source and no installed copy means no upstream either.

    An upstream-issue draft for a name that exists nowhere cannot be filed by
    anybody: there is no repository it belongs to, and guessing at one is what
    the contract forbids.
    """
    workspace = _draft_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    draft = _draft_payload(tmp_path, skill="never-heard-of-it")

    assert cli.main(["skill-draft-add", "--input-file", draft]) == 0

    out = capsys.readouterr().out
    assert "no packaged copy to report upstream" in out
    assert '"new_skill"' in out


# ---- The workspace name the entry pass is planned under -------------------
#
# `entry_identity` digests the workspace name, and every operation that consumes
# one resolves the vault through the workspace registry and mints it under the
# name the registry knows. So a `curation-begin` that planned the entry pass
# under the vault DIRECTORY's name would mint identities nothing resolves: every
# managed call comes back `conflict`, no verdict is ever filed, and the worklist
# key never settles. The name is therefore resolved once, by the same registry
# read that resolved the vault, and the only case where the directory's own name
# stands in for the registry's is an explicit `--vault-root`.


def _registered_install(tmp_path: Path) -> tuple[Path, Path]:
    """An install whose registry names workspace `work` inside `memory-vault/client-a`.

    The layout that makes the difference visible: a shared vault whose directory
    is `client-a` and whose registered name is `work`, so a name taken from the
    directory is provably the wrong one.
    """
    root = tmp_path / "workspace"
    vault = root / "memory-vault" / "client-a"
    (vault / "Workspace").mkdir(parents=True)
    (vault / "People").mkdir()
    (root / ".runtime").mkdir(parents=True)
    (root / ".runtime" / "workspaces.json").write_text(
        json.dumps({"work": {"name": "work", "vault_root": str(vault)}}),
        encoding="utf-8",
    )
    return root, vault


def _curation_args(**overrides: object) -> argparse.Namespace:
    """The `argparse.Namespace` a curation subcommand is dispatched with."""
    args = argparse.Namespace(
        workspace=None, vault_root=None, guide=None, max_items=None, max_seconds=None
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _stale_fact_note(vault: Path) -> Path:
    """One person note whose single bullet is stamped well past its horizon."""
    from ciao import curation_run as cr

    note = vault / "People" / "Ada.md"
    note.write_text(
        "---\ntype: person\nupdated: 2024-01-05\n---\n\n# Ada\n\n"
        "- Ada runs the release train [verified: 2024-01-05]\n",
        encoding="utf-8",
    )
    (vault / cr.CURATION_LOG_RELATIVE).write_text(
        "---\nlast_full_pass: 2026-09-18\n---\n\n# Curation log\n", encoding="utf-8"
    )
    return note


def test_the_curation_workspace_name_is_the_registry_s_not_the_directory_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name the entry pass mints identities under is the registered one.

    `client-a` is a directory, `work` is the workspace: nothing else in the
    install knows `client-a`, and the operations the plan exists to hand work to
    resolve the vault through the registry. The whole point is that the plan and
    the operation name the same workspace, so the name travels out of the one
    resolution that read the registry — it is not asked for again here, and it is
    not inferred from the directory.
    """
    root, vault = _registered_install(tmp_path)
    _stale_fact_note(vault)
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_WORKSPACE", str(root))
    monkeypatch.setenv("CIAO_VAULT_ROOT", str(root / "memory-vault"))
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    _workspace, resolved, _registry, name = cli._resolve_workspace_and_vaults(
        _curation_args()
    )

    assert name == "work"
    assert resolved == vault.resolve()
    assert name != resolved.name, "the directory's name is the guess this replaces"
    # And the plan it produces is keyed on identities minted under that name —
    # the one a managed `entry verify` call can actually resolve.
    payload, _worklist = cli._curation_plan(_curation_args())
    entries = [
        item for item in payload["items"] if item["pass"] == "stale_entry"
    ]
    assert len(entries) == 1, payload["items"]
    from ciao import note_entries as ne

    identity = ne.parse_note_entries(
        (vault / "People" / "Ada.md").read_text(encoding="utf-8"),
        note_path="People/Ada.md",
        workspace="work",
    ).entries[0].identity
    assert identity in entries[0]["reason"], entries[0]["reason"]


def test_an_explicit_vault_root_is_planned_under_the_directory_it_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--vault-root` is the one case where the directory's own name is the name.

    There is no registry to ask: the operator pointed at a directory in person,
    with no workspace named anywhere. So the name is taken from the directory,
    explicitly, rather than as a silent guess every caller inherits — and the
    active workspace is not consulted, because an explicit argument outranks the
    environment.
    """
    root, vault = _registered_install(tmp_path)
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_WORKSPACE", str(root))
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    _workspace, resolved, _registry, name = cli._resolve_workspace_and_vaults(
        _curation_args(vault_root=str(vault))
    )

    assert (resolved, name) == (vault.resolve(), "client-a")


def test_a_run_with_no_registered_name_plans_no_entries_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No name to resolve means the pass is skipped *and reported*, not guessed.

    Planning the entry pass under the directory's name is the failure this
    replaces: every identity it mints names no entry, every managed call answers
    `conflict`, and the worklist reports a backlog it can never drain. Skipping
    it *silently* is the other half of the same problem, because a skipped pass
    must never read as a pass that found nothing to do — so the worklist says
    which pass was not planned and why, and the note it would otherwise have
    planned is right there in the vault.
    """
    root, vault = _registered_install(tmp_path)
    _stale_fact_note(vault)
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_WORKSPACE", str(root))
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "")
    monkeypatch.setenv("CIAO_VAULT_ROOT", str(vault))

    _workspace, _vault, _guide, _budget, _registry, name = cli._curation_context(
        _curation_args()
    )

    assert name is None, "there is no registry to ask and no --vault-root to read"
    payload, _worklist = cli._curation_plan(_curation_args())
    assert [i for i in payload["items"] if i["pass"] == "stale_entry"] == []
    assert any("stale-entry pass was not planned" in n for n in payload["notes"]), (
        payload["notes"]
    )
    # And the fact it did not plan is one the pass *would* have found: the note is
    # past its horizon and its bullet carries its own 2024 stamp.
    from ciao import note_entries as ne

    entry = ne.parse_note_entries(
        (vault / "People" / "Ada.md").read_text(encoding="utf-8"),
        note_path="People/Ada.md",
        workspace="client-a",
    ).entries[0]
    assert entry.verified == date(2024, 1, 5)
