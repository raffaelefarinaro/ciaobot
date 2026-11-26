from __future__ import annotations

import argparse
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao import cli


def _fake_python(tmp_path: Path) -> str:
    """The interpreter setup points its service at; setup only writes the path into the plist, so a made-up one will do."""
    return "/opt/ciao/bin/python"


def test_cli_run_dispatches_server(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli, "_run_server", lambda **kwargs: called.append(kwargs) or 0)

    assert cli.main(["run"]) == 0
    assert called == [{"supervised": False}]


def _raise_system_exit(code: int):
    def _main(*, supervised: bool = False) -> None:
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


def test_run_has_no_installer_flags() -> None:
    """The installer bootstrap is gone: each removed flag, and the `installer`
    command, is an unknown argument to argparse (exit 2)."""
    parser = cli.build_parser()
    for argv in (
        ["run", "--installer-bootstrap"],
        ["run", "--installer-run-id", "run-1"],
        ["run", "--installer-suggest-workspace", "/tmp/elsewhere"],
        ["installer", "activate", "--run-id", "run-1"],
    ):
        with pytest.raises(SystemExit) as caught:
            parser.parse_args(argv)
        assert caught.value.code == 2, argv


def test_run_server_passes_supervised_to_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine's restart watchdog has to know it is supervised too, or a
    wedged cleanup re-execs and bypasses the supervisor's relaunch."""
    import ciao.main

    seen: list[dict[str, object]] = []

    def _fake_main(**kwargs: object) -> None:
        seen.append(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(ciao.main, "main", _fake_main)

    assert cli._run_server(supervised=True) == 0
    assert seen == [{"supervised": True}]


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


def test_cli_dev_dispatches_module(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli.dev, "main", lambda argv: called.append(argv) or 0)

    assert cli.main(["dev", "--workspace", str(Path("/tmp/app")), "--no-install"]) == 0
    assert called == [
        [
            "--workspace",
            str(Path("/tmp/app")),
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

    assert cli.main(["vault-lint", "--vault-root", str(Path("/tmp/vault"))]) == 0
    assert str(called[0].vault_root) == str(Path("/tmp/vault"))


def test_cli_workspace_census_dispatches_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = []

    monkeypatch.setattr(
        cli, "_workspace_census_command", lambda args: called.append(args) or 0
    )

    assert cli.main(["workspace-census", "--vault-root", str(Path("/tmp/vault")), "--json"]) == 0
    assert str(called[0].vault_root) == str(Path("/tmp/vault"))
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


def test_cli_create_chat_dispatches_command(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli, "_create_chat_command", lambda args: called.append(args) or 0)

    assert cli.main(["create-chat", "--prompt", "hello", "--workspace", "personal"]) == 0
    assert called[0].prompt == "hello"
    assert called[0].workspace == "personal"


def test_cli_cleanup_sdk_blobs_dispatches_command(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    monkeypatch.setattr(cli, "_cleanup_sdk_blobs_command", lambda args: called.append(args) or 0)

    assert cli.main(["cleanup-sdk-blobs", "--workspace", str(Path("/tmp/workspace")), "--apply"]) == 0
    assert str(called[0].workspace) == str(Path("/tmp/workspace"))
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
                str(Path("/tmp/workspace")),
                "--skip-upstream",
            ]
        )
        == 0
    )
    assert str(called[0].workspace) == str(Path("/tmp/workspace"))
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
            _fake_python(tmp_path),
            "--port",
            "9443",
        ]
    )

    assert rc == 0
    assert (workspace / ".env").read_text(encoding="utf-8").splitlines()[:2] == [
        "PWA_AUTH_TOKEN=test-token",
        "CIAO_VAULT_ROOT=memory-vault",
    ]
    # Agent assets belong to the WORKSPACE root, not the install root: a fresh
    # setup now builds the per-workspace layout directly instead of the shared one
    # that then had to be migrated.
    root = workspace / "research"
    assert not (root / ".claude" / "skills" / "ciao-memory" / "SKILL.md").exists()
    assert (root / ".claude" / "skills" / "ciao-capabilities" / "references" / "memory-operations" / "guide.md").is_file()
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
    # The service definition: the plist.
    service = cli._service_definition(launch_agents)
    assert service is not None and service.is_file()
    plist_text = service.read_text(encoding="utf-8")
    assert "<string>/opt/ciao/bin/python</string>" in plist_text
    assert "<string>run</string>" in plist_text
    assert f"<string>{workspace.resolve()}</string>" in plist_text
    assert "<string>9443</string>" in plist_text
    assert f"<string>{workspace.resolve()}/.runtime/ciao.stdout.log</string>" in plist_text
    # Login Items still groups the server agent under the desktop app.
    assert "<key>AssociatedBundleIdentifiers</key>" in plist_text
    assert "<string>local.ciaobot.app</string>" in plist_text
    # No menu-bar agent and no launcher bundle: Ciaobot.app is the menu bar
    # now, and nothing writes the retired rumps helper.
    assert not (launch_agents / "com.ciao.menubar.plist").exists()
    assert not (apps / "Ciaobot Server.app").exists()
    # Setup always mints the one-time login token, even with no desktop app:
    # the summary prints it as the login URL.
    setup_token = (workspace / ".runtime" / "setup-token").read_text(
        encoding="utf-8"
    ).strip()
    assert setup_token


def _setup_cli_args(tmp_path: Path) -> list[str]:
    return [
        "setup",
        "--workspace",
        str(tmp_path / "workspace"),
        "--auth-token",
        "test-token",
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


def _setup_argv(workspace: Path, launch_agents: Path, apps: Path, *, yes: bool = False, initialize_git: bool = False) -> list[str]:
    argv = [
        "setup",
        "--workspace", str(workspace),
        "--launch-agents-dir", str(launch_agents),
        "--app-dir", str(apps),
        "--python", _fake_python(launch_agents.parent),
        "--port", "9443",
    ]
    if yes:
        argv.append("--yes")
    if initialize_git:
        argv.append("--initialize-git")
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


def test_setup_refuses_to_repoint_existing_workspace(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_setup_defers_git_unless_initialize_git_is_explicit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    assert cli.main(_setup_argv(workspace, tmp_path / "LaunchAgents", tmp_path / "Applications")) == 0
    assert not (workspace / ".git").exists()


def test_setup_yes_does_not_consent_to_git_initialization(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    assert cli.main(_setup_argv(workspace, tmp_path / "LaunchAgents", tmp_path / "Applications", yes=True)) == 0
    assert not (workspace / ".git").exists()


def test_setup_initialize_git_creates_workspace_history(tmp_path: Path) -> None:
    """The explicit CLI flag is the consent; it must actually reach the helper.

    Without this the flag could silently stop being forwarded and every other
    setup test would still pass, because they only assert that nothing was made.
    """
    workspace = tmp_path / "workspace"
    argv = _setup_argv(workspace, tmp_path / "LaunchAgents", tmp_path / "Applications", initialize_git=True)

    assert cli.main(argv) == 0
    assert (workspace / ".git").is_dir()
    log = subprocess.run(
        ["git", "-C", str(workspace), "log", "--oneline"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert "Initialize Ciaobot workspace" in log.stdout, log.stderr


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


class _RecordingBackend:
    """Stands in for `service_backend.current_backend()` in the load path."""

    name = "recording"

    def __init__(self, status: int = 0) -> None:
        self.status = status
        self.loaded: list[Path] = []

    def agents_dir(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents"

    def live_agents_dir(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents"

    def is_live_agents_dir(self, path: Path) -> bool:
        return False

    def bootout_agent(self, label: str) -> None:
        return None

    def load_agent(self, definition: Path) -> int:
        self.loaded.append(definition)
        return self.status

    def schedule_server_handoff(self) -> bool:
        return False


def _no_launchctl(monkeypatch: pytest.MonkeyPatch) -> None:
    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if command and command[0] == "launchctl":
            raise AssertionError(f"cli must not shell out to launchctl: {command!r}")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)


def test_setup_load_launchd_goes_through_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    backend = _RecordingBackend(status=0)
    monkeypatch.setattr(
        cli.service_backend, "current_backend", lambda: backend
    )
    monkeypatch.setattr(cli, "setup_workspace", _stub_setup_for_launchd)
    _no_launchctl(monkeypatch)

    assert (
        cli.main(_launchd_setup_argv(tmp_path / "workspace", tmp_path / "LaunchAgents"))
        == 0
    )

    assert backend.loaded == [tmp_path / "LaunchAgents" / "com.ciao.server.plist"]


@pytest.mark.parametrize(
    ("status", "expected"), [(3, 1), (5, 5)]
)
def test_setup_load_failure_through_the_backend(
    status: int,
    expected: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    backend = _RecordingBackend(status=status)
    monkeypatch.setattr(
        cli.service_backend, "current_backend", lambda: backend
    )
    monkeypatch.setattr(cli, "setup_workspace", _stub_setup_for_launchd)
    _no_launchctl(monkeypatch)

    result = cli.main(
        _launchd_setup_argv(tmp_path / "workspace", tmp_path / "LaunchAgents")
    )

    assert result == expected
    assert backend.loaded == [tmp_path / "LaunchAgents" / "com.ciao.server.plist"]


def test_disable_legacy_menubar_agent_boots_out_only_the_live_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Backend(_RecordingBackend):
        def __init__(self) -> None:
            super().__init__()
            self.booted: list[str] = []
            self.default_dir = tmp_path / "default"
            self.live_dir = tmp_path / "live"

        def agents_dir(self) -> Path:
            return self.default_dir

        def live_agents_dir(self) -> Path:
            return self.live_dir

        def bootout_agent(self, label: str) -> None:
            self.booted.append(label)

    backend = Backend()
    monkeypatch.setattr(
        cli.service_backend, "current_backend", lambda: backend
    )
    live_plist = backend.live_dir / "com.ciao.menubar.plist"
    default_plist = backend.default_dir / "com.ciao.menubar.plist"
    for plist in (live_plist, default_plist):
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text("plist", encoding="utf-8")
    other_plist = tmp_path / "other" / "com.ciao.menubar.plist"
    other_plist.parent.mkdir(parents=True)
    other_plist.write_text("plist", encoding="utf-8")

    assert cli._disable_legacy_menubar_agent(backend.live_dir) is True
    assert cli._disable_legacy_menubar_agent(tmp_path / "other") is True
    # No argument falls back to the backend's default agents dir.
    assert cli._disable_legacy_menubar_agent() is True

    assert not live_plist.exists()
    assert not default_plist.exists()
    assert not other_plist.exists()
    assert backend.booted == ["com.ciao.menubar"]


def test_write_launchd_plist_repoint_guard_uses_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CIAO_ALLOW_LAUNCH_AGENT_REPOINT", raising=False)
    agents = tmp_path / "a"
    agents.mkdir()
    workspace_a = tmp_path / "ws-a"
    workspace_a.mkdir()
    (agents / "com.ciao.server.plist").write_text(
        _plist_with_workspace(workspace_a), encoding="utf-8"
    )
    backend = _RecordingBackend()
    monkeypatch.setattr(
        cli.service_backend, "current_backend", lambda: backend
    )
    monkeypatch.setattr(cli, "_plist_workspace", lambda _path: workspace_a.resolve())

    def write(workspace: Path, is_live: bool) -> Path:
        backend.is_live_agents_dir = lambda _path: is_live  # type: ignore[method-assign]
        return cli._write_launchd_plist(
            workspace=workspace,
            launch_agents_dir=agents,
            port=8443,
            confirm_repoint=False,
        )

    with pytest.raises(RuntimeError, match="Refusing to repoint"):
        write(tmp_path / "ws-b", True)

    written = write(tmp_path / "ws-b", False)
    assert written.is_file()


class _PlatformShim:
    """`cli.sys` with a different `platform`, leaving the real module alone.

    `monkeypatch.setattr(cli.sys, "platform", ...)` would patch the global
    `sys` module, so unrelated code (shutil, ctypes) would take the win32
    branches too. Only cli needs to believe it is on another platform.
    """

    def __init__(self, real: object, platform: str) -> None:
        self._real = real
        self.platform = platform

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_setup_load_launchd_is_refused_off_macos(
    platform: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(cli, "sys", _PlatformShim(sys, platform))

    assert cli.main(["setup", "--workspace", str(tmp_path / "ws"), "--load-launchd"]) == 2
    assert "requires macOS. On Linux" in capsys.readouterr().err
    assert not (tmp_path / "ws").exists(), "the refusal must come before any setup work"


def _plist_with_workspace(workspace: Path) -> str:
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<plist version=\"1.0\"><dict><key>ProgramArguments</key><array>"
        f"<string>{workspace}</string></array></dict></plist>\n"
    )


def test_setup_workspace_early_guard_uses_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.delenv("CIAO_ALLOW_LAUNCH_AGENT_REPOINT", raising=False)
    agents = tmp_path / "a"
    agents.mkdir()
    workspace_a = tmp_path / "ws-a"
    workspace_a.mkdir()
    (agents / "com.ciao.server.plist").write_text(
        _plist_with_workspace(workspace_a), encoding="utf-8"
    )
    monkeypatch.setattr(
        _RecordingBackend,
        "is_live_agents_dir",
        lambda self, _path: True,
    )
    monkeypatch.setattr(
        cli.service_backend,
        "current_backend",
        _RecordingBackend,
    )
    monkeypatch.setattr(
        cli,
        "_plist_workspace",
        lambda _path: workspace_a.resolve(),
    )
    target = tmp_path / "ws-b"

    with pytest.raises(RuntimeError, match="Refusing to repoint"):
        cli.setup_workspace(target, launch_agents_dir=agents)

    assert not target.exists(), "the early guard must refuse before creating anything"


def test_setup_workspace_does_not_ask_for_a_backend_on_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "sys", _PlatformShim(sys, "linux"))
    monkeypatch.delenv("CIAO_ALLOW_LAUNCH_AGENT_REPOINT", raising=False)

    def no_backend():
        raise AssertionError("setup_workspace must not ask for a backend here")

    monkeypatch.setattr(cli.service_backend, "current_backend", no_backend)

    written = cli.setup_workspace(tmp_path / "ws")

    assert (tmp_path / "ws").is_dir()
    assert not any("LaunchAgents" in str(path) for path in written)


def test_setup_workspace_asks_the_backend_where_a_service_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS has a background service, so setup writes its definition by default (#845) and the definition dir comes from the backend."""
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.delenv("CIAO_ALLOW_LAUNCH_AGENT_REPOINT", raising=False)
    agents = tmp_path / "LaunchAgents"
    asked: list[str] = []

    class _DarwinBackend(_RecordingBackend):
        def agents_dir(self) -> Path:
            asked.append("agents_dir")
            return agents

        def is_live_agents_dir(self, _path: Path) -> bool:
            return False

    monkeypatch.setattr(cli.service_backend, "current_backend", _DarwinBackend)

    written = cli.setup_workspace(
        tmp_path / "ws", app_dir=tmp_path / "Applications"
    )

    assert set(asked) == {"agents_dir"}
    plist = agents / "com.ciao.server.plist"
    # written lists the workspace files first; the definition is written last.
    assert written[-1] == plist
    assert plist.is_file()


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
    assert "CIAO_VAULT_ROOT=" in content
    # The service definition carries these; setup no longer writes them.
    assert "CIAO_WORKSPACE=" not in content
    assert "CIAO_RUNTIME_ROOT=" not in content


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
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
    hint = cli._path_export_hint()
    assert hint == f'export PATH="{bin_dir}:$PATH"'
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", str(bin_dir)]))
    assert cli._path_export_hint() is None


def test_path_export_hint_names_the_installed_launcher_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import install_receipt

    # An installer engine runs from a uv tool env whose own bin dir is never on
    # PATH; `ciao` is the launcher in uv's bin dir, which the installer adds.
    launcher_dir = tmp_path / "First Last" / ".local" / "bin"
    install_receipt.write_receipt(install_receipt.InstallReceipt(
        version="1.2.3", executable=str(launcher_dir / "ciao"), python=sys.executable,
        service_backend="launchd", service_label="com.ciao.server",
        installed_at="2026-10-05T00:00:00+00:00",
    ))
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", str(launcher_dir)]))
    assert cli._path_export_hint() is None

    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", str(Path(sys.executable).parent)]))
    hint = cli._path_export_hint()
    assert hint is not None
    assert str(launcher_dir) in hint


def test_path_export_hint_uses_the_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "path_hint", lambda directory, *, persist: "HINT")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    assert cli._path_export_hint() == "HINT"


def test_setup_summary_prints_no_path_line_without_a_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    workspace = tmp_path / "workspace"
    token_path = workspace / ".runtime" / "setup-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("tok\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_path_export_hint", lambda: None)

    cli._print_setup_summary(workspace, 9443)

    out = capsys.readouterr().out
    assert "PATH" not in out


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


def test_service_start_reports_the_port_when_the_engine_cannot_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`ciao service start` has to name the port, not just "it failed".

    The engine leaves with the port-in-use exit code when it could not bind
    its port. The CLI reads that code off the launchd job and turns it back
    into the sentence the operator needs: which port is taken, and the two
    ways out.
    """
    import subprocess

    from ciao import macos_service
    from ciao.port_in_use import PORT_IN_USE_EXIT_CODE

    runtime = macos_service.DesktopRuntime(
        workspace=str(tmp_path / "workspace"),
        runtime_root=str(tmp_path / "workspace" / ".runtime"),
        port=8443,
        server_plist=str(tmp_path / "LaunchAgents" / "com.ciao.server.plist"),
        python_path="/opt/ciao/bin/python3",
    )
    Path(runtime.server_plist).parent.mkdir(parents=True, exist_ok=True)
    Path(runtime.server_plist).write_bytes(b"")
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(macos_service, "discover_runtime", lambda **_: runtime)
    # The test fakes the platform, and the macOS path then asks launchd for
    # the job in this user's domain - a uid `os.getuid` cannot give on
    # Windows, where the test still runs. The domain is not what is under
    # test here, so it is fixed (the same isolation #1666 applied).
    monkeypatch.setattr(macos_service, "_getuid", lambda: 501)

    def _run(command, **_kwargs):
        printed = (
            "gui/501/com.ciao.server = {\n"
            "\tstate = not running\n"
            f"\tlast exit code = {PORT_IN_USE_EXIT_CODE}\n"
            "}\n"
            if command[1] == "print"
            else ""
        )
        return subprocess.CompletedProcess(command, 0, stdout=printed, stderr="")

    monkeypatch.setattr(macos_service, "_launchctl", lambda args, runner=None: _run(["launchctl", *args]))

    assert cli.main(["service", "start"]) == 1

    err = capsys.readouterr().err
    assert "8443" in err
    assert "already in use" in err
    assert "PWA_PORT" in err


def test_auth_print_only_outputs_terminal_command(capsys) -> None:
    assert cli.main(["auth", "opencode", "--print-only"]) == 0

    assert capsys.readouterr().out.strip().endswith("auth login")


def test_auth_print_only_opencode_does_not_require_installation(monkeypatch, capsys) -> None:
    monkeypatch.setattr("ciao.opencode_cli.resolve_opencode_binary", lambda: None)

    assert cli.main(["auth", "opencode", "--print-only"]) == 0

    assert capsys.readouterr().out.strip() == "opencode auth login"


def test_auth_rejects_a_non_runtime_provider(capsys) -> None:
    """Only the three runtime providers have a login; nothing else is offered."""
    with pytest.raises(SystemExit):
        cli.main(["auth", "ollama", "--print-only"])


def test_auth_claude_uses_the_resolved_cli(monkeypatch) -> None:
    calls: list[list[str]] = []
    envs: list[object] = []
    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: "/opt/ciao/claude")

    def fake_run(cmd, check=False, env=None):
        calls.append(cmd)
        envs.append(env)
        return type("P", (), {"returncode": 0})()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.main(["auth", "claude"]) == 0

    assert calls == [["/opt/ciao/claude", "auth", "login"]]
    assert isinstance(envs[0], dict)


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


def test_cli_vault_search_logs_reads_own_workspace_and_shared_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On a re-rooted install `--logs` answers from the vault's own workspace
    archive and the shared archive, and never from a sibling workspace's."""
    from ciao.config import reset_reroot_cache
    from ciao.workspace_reroot import mark_born_per_root, registry_file

    install = tmp_path / "install"
    work_vault = install / "work" / "memory-vault"
    work_vault.mkdir(parents=True)
    runtime = install / ".runtime"
    mark_born_per_root(install, runtime, ["personal", "work"])
    # The registry names the workspace roots; a re-rooted install always has one.
    registry_file(runtime).write_text(
        json.dumps([{"name": "personal"}, {"name": "work"}]), encoding="utf-8"
    )
    reset_reroot_cache()
    transcripts = {
        "ownchat": install / "work" / "Logs" / "Chats" / "ownchat" / "claude" / "a.md",
        "siblingchat": install / "personal" / "Logs" / "Chats" / "siblingchat" / "claude" / "b.md",
        "rootchat": install / "Logs" / "Chats" / "rootchat" / "claude" / "c.md",
    }
    for path in transcripts.values():
        path.parent.mkdir(parents=True)
        path.write_text("# Chat\n\nWe talked about kiteboarding.\n", encoding="utf-8")

    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("CIAO_WORKSPACE", str(install))
    monkeypatch.delenv("CIAO_VAULT_ROOT", raising=False)
    monkeypatch.delenv("CIAO_RUNTIME_ROOT", raising=False)

    assert (
        cli.main(
            ["vault-search", "kiteboarding", "--logs", "--vault-root", str(work_vault)]
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "ownchat" in out
    assert "rootchat" in out
    assert "siblingchat" not in out


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
    assert (work_vault / "People" / "Aymen.md").as_uri() in out


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

    setup_workspace(root, auth_token="t")


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

    Discovery used to apply its overlay after PWA_AUTH_TOKEN was parsed, so
    the started server ignored the workspace's configured password.
    """
    from ciao.config import CiaoConfig, reset_reroot_cache

    workspace = tmp_path / "workspace"
    _per_root_workspace(workspace)
    # A distinctive token in the .env, as a configured install has.
    env_path = workspace / ".env"
    env_path.write_text(
        (env_path.read_text(encoding="utf-8")).replace("PWA_AUTH_TOKEN=t", "PWA_AUTH_TOKEN=ws-secret-token"),
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
    ):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "home").mkdir()

    reset_reroot_cache()
    try:
        config = CiaoConfig.from_env()
    finally:
        reset_reroot_cache()

    assert config.workspace_root == workspace.resolve()
    assert config.pwa_auth_token == "ws-secret-token"


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
    ):
        monkeypatch.delenv(name, raising=False)
    # The whole point: present in the environment, but empty.
    monkeypatch.setenv("CIAO_WORKSPACE", "")
    (tmp_path / "home").mkdir()

    reset_reroot_cache()
    try:
        config = CiaoConfig.from_env()
    finally:
        reset_reroot_cache()

    # The discovered install wins, so the .env and the root agree.
    assert config.workspace_root == workspace.resolve()
    assert config.pwa_auth_token == "ws-secret-token"


# ---- The workspace name the entry pass is planned under -------------------
#
# `entry_identity` digests the workspace name, and every operation that consumes
# one resolves the vault through the workspace registry and mints it under the
# name the registry knows. So a plan that covered the entry pass
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


def test_an_explicit_vault_root_is_planned_under_its_registered_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit `--vault-root` the registry knows is named by its owner.

    The operator named a directory, `client-a`, but the registry calls the
    workspace that owns it `work` — and every operation the plan hands work to
    resolves the vault through that registry, so `client-a` mints identities
    nothing resolves. The active workspace is still not consulted, because an
    explicit argument outranks the environment: naming it something else changes
    nothing.
    """
    root, vault = _registered_install(tmp_path)
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_WORKSPACE", str(root))
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")

    _workspace, resolved, _registry, name = cli._resolve_workspace_and_vaults(
        _curation_args(vault_root=str(vault))
    )

    assert (resolved, name) == (vault.resolve(), "work")

    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "nope")

    _workspace, resolved, _registry, name = cli._resolve_workspace_and_vaults(
        _curation_args(vault_root=str(vault))
    )

    assert (resolved, name) == (vault.resolve(), "work")


def test_an_unregistered_explicit_vault_root_keeps_its_directory_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `--vault-root` the registry does not know still keeps its directory name.

    The owner lookup has to be able to answer "none": the operator pointed at a
    directory in person, outside every registered workspace, and there is no
    workspace name to resolve. The directory's own name is used explicitly rather
    than left as nothing at all.
    """
    root, _vault = _registered_install(tmp_path)
    other = tmp_path / "loose" / "memory-vault"
    (other / "Workspace").mkdir(parents=True)
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_WORKSPACE", str(root))

    _workspace, resolved, _registry, name = cli._resolve_workspace_and_vaults(
        _curation_args(vault_root=str(other))
    )

    assert name == "memory-vault"
    assert resolved == other.resolve()


def test_a_bare_shell_explicit_vault_root_is_named_by_the_installed_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `CIAO_WORKSPACE`, just a terminal and a `--vault-root`.

    The owner lookup reads a registry, and from a bare shell the install root it
    used was the cwd — so the registered owner was never found and the name fell
    back to the vault directory's own name, `client-a`, which no operation that
    consumes an entry identity resolves. The install the operator actually has
    is the one the LaunchAgent points at, the same answer `_learnings_workspace`
    and `from_env` now share.
    """
    root, vault = _registered_install(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.delenv("CIAO_WORKSPACE", raising=False)
    monkeypatch.delenv("CIAO_RUNTIME_ROOT", raising=False)
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(
        "ciao.macos_service.discover_runtime",
        lambda **_: SimpleNamespace(
            workspace=str(root), runtime_root=str(root / ".runtime")
        ),
    )

    name = cli._resolve_workspace_and_vaults(_curation_args(vault_root=str(vault)))[3]

    assert name == "work"


def test_a_bare_shell_explicit_vault_root_with_an_empty_workspace_mints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exported empty `CIAO_WORKSPACE` must not turn a name lookup into a mint.

    `export CIAO_WORKSPACE=` in a shell profile leaves the key present but empty,
    so it survives the discovery helper (the caller's environment wins the merge)
    and `setdefault` would leave it in place. `from_env` then has a token and no
    workspace, which is exactly the bootstrap combination: it MINTED
    `~/.ciao/bootstrap/.runtime/bootstrap-auth-token` and read a registry nobody
    asked for, on a lookup whose whole contract is to read and name. On develop
    the line was an unconditional assignment, so this never happened.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CIAO_WORKSPACE", "")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "ciao.macos_service.discover_runtime",
        lambda **_: SimpleNamespace(workspace="", runtime_root=""),
    )
    vault = tmp_path / "v"

    name = cli._resolve_workspace_and_vaults(_curation_args(vault_root=str(vault)))[3]

    assert name == "v", "no registry knows this vault, so the directory's name is the answer"
    # What the empty string cost: `bootstrap_mode` went True and `from_env` wrote
    # `<bootstrap>/.runtime/bootstrap-auth-token`. Conftest redirects that root to
    # `tmp_path/bootstrap` (an operator sees `~/.ciao/bootstrap`), so assert on it.
    assert not (tmp_path / "bootstrap").exists(), "naming a vault must mint nothing"
    assert not (home / ".ciao").exists()


def test_a_bare_shell_active_workspace_does_not_mint_a_bootstrap_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`CIAO_ACTIVE_WORKSPACE` alone must resolve through the install, not bootstrap.

    A scheduled run exports the active workspace name without `CIAO_WORKSPACE`,
    so the branch built `dict(os.environ)` and skipped discovery. With no
    workspace, `from_env` took the bootstrap branch and wrote
    `<bootstrap>/.runtime/bootstrap-auth-token`, then read a registry nobody
    asked for. Routing through `installed_workspace_env` gives it the installed
    root and its registry, and the token stand-in only when blank keeps it out of
    bootstrap mode.
    """
    root, vault = _registered_install(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for name in ("CIAO_WORKSPACE", "CIAO_VAULT_ROOT", "CIAO_RUNTIME_ROOT", "PWA_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(
        "ciao.macos_service.discover_runtime",
        lambda **_: SimpleNamespace(
            workspace=str(root), runtime_root=str(root / ".runtime")
        ),
    )

    workspace, resolved, _registry, name = cli._resolve_workspace_and_vaults(
        _curation_args()
    )

    assert name == "work"
    assert workspace == root.resolve()
    assert resolved == vault.resolve()
    # What bootstrap mode cost: `from_env` wrote the token under the redirect
    # conftest installs (`tmp_path/bootstrap`), because discovery was skipped.
    assert not (tmp_path / "bootstrap").exists(), "a read-only resolution must mint nothing"



def test_a_bare_shell_vault_root_uses_the_installed_root_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install root, not the cwd, is the workspace a bare shell `--vault-root` is named after.

    Returning the cwd read the guide and wrote state beside the shell while the
    run was named after the installed workspace.
    """
    root, vault = _registered_install(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for name in ("CIAO_WORKSPACE", "CIAO_RUNTIME_ROOT", "CIAO_ACTIVE_WORKSPACE", "PWA_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(
        "ciao.macos_service.discover_runtime",
        lambda **_: SimpleNamespace(
            workspace=str(root), runtime_root=str(root / ".runtime")
        ),
    )

    workspace, resolved, _registry, name = cli._resolve_workspace_and_vaults(
        _curation_args(vault_root=str(vault))
    )

    assert name == "work"
    assert resolved == vault.resolve()
    assert workspace == root.resolve(), "the installed root, not the cwd"



def test_a_bare_shell_read_only_audit_targets_the_install_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os-audit` and `memory-audit` read the install, not the directory you are in.

    Both commands defaulted their workspace to the cwd, so from a bare shell they
    audited whatever directory happened to be current. Discovery seeds the
    installed root instead, and the token stand-in is applied only when blank so
    the read-only resolution can never enter bootstrap mode.
    """
    root = tmp_path / "workspace"
    _write_healthy_audit_workspace(root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for name in ("CIAO_WORKSPACE", "CIAO_VAULT_ROOT", "CIAO_RUNTIME_ROOT", "PWA_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CIAO_ACTIVE_WORKSPACE", raising=False)
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(
        "ciao.macos_service.discover_runtime",
        lambda **_: SimpleNamespace(
            workspace=str(root), runtime_root=str(root / ".runtime")
        ),
    )

    import ciao.os_audit as os_audit

    seen: dict[str, Any] = {}
    real_os_audit = os_audit.run_os_audit

    def _spy(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return real_os_audit(**kwargs)

    monkeypatch.setattr(os_audit, "run_os_audit", _spy)
    cli.main(["os-audit", "--json"])

    assert seen["workspace_dir"] == root.resolve()
    assert seen["vault_root"] == (root / "memory-vault").resolve()
    assert seen["runtime_dir"] == (root / ".runtime").resolve()

    import ciao.os_audit as os_audit_mod

    memory_seen: dict[str, Any] = {}
    real_scan = os_audit_mod._scan_memory_guide

    def _scan_spy(guide: Path, **kwargs: Any) -> dict[str, Any]:
        memory_seen["workspace_dir"] = kwargs["workspace_dir"]
        return real_scan(guide, **kwargs)

    monkeypatch.setattr(os_audit_mod, "_scan_memory_guide", _scan_spy)
    cli.main(["memory-audit", "--json"])

    assert memory_seen["workspace_dir"] == root.resolve()
    assert not (tmp_path / "bootstrap").exists(), "a read-only audit must mint nothing"
    assert not (elsewhere / ".runtime").exists(), "nothing may be created under the cwd"


def test_rotate_setup_token_writes_an_owner_only_file(tmp_path):
    from ciao.os_support.private import is_private

    token_path = tmp_path / ".runtime" / "setup-token"
    token_path.parent.mkdir()
    token_path.write_text("old\n", encoding="utf-8")
    os.chmod(token_path, 0o644)

    token = cli._rotate_setup_token(tmp_path)

    assert token_path.read_text(encoding="utf-8") == token + "\n"
    assert is_private(token_path)


def test_setup_url_reports_a_token_create_race_without_a_traceback(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    def raced(*args, **kwargs):
        raise FileExistsError("setup-token")

    monkeypatch.setattr(cli, "open_private", raced)

    assert cli.main(["setup-url", "--workspace", str(tmp_path)]) == 1

    err = capsys.readouterr().err.strip()
    assert len(err.splitlines()) == 1
    assert "setup token" in err


def test_ensure_setup_token_tightens_an_older_world_readable_file(tmp_path: Path) -> None:
    from ciao.os_support.private import is_private

    token_path = tmp_path / ".runtime" / "setup-token"
    token_path.parent.mkdir()
    token_path.write_text("old-token\n", encoding="utf-8")
    os.chmod(token_path, 0o644)

    assert cli._ensure_setup_token(tmp_path) == "old-token"
    assert is_private(token_path)


def test_setup_command_prints_the_macos_access_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    from tests.test_server_host_activation import _verified_host

    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    args = [
        "setup",
        "--workspace",
        str(home / "Documents" / "ws"),
        "--auth-token",
        "test-token",
        "--launch-agents-dir",
        str(tmp_path / "LaunchAgents"),
        "--app-dir",
        str(tmp_path / "Applications"),
        "--python",
        _fake_python(tmp_path),
    ]

    host = _verified_host()
    monkeypatch.setattr(cli, "_verified_host_if_installed", lambda: host)
    assert cli.main(args) == 0
    err = capsys.readouterr().err
    assert "Note:" in err and "Ciaobot Server" in err

    monkeypatch.setattr(cli, "_verified_host_if_installed", lambda: None)
    other = [*args[:2], str(home / "Documents" / "ws2"), *args[3:], "--yes"]
    assert cli.main(other) == 1
    err = capsys.readouterr().err
    assert "Ciaobot Server" in err and "Error:" in err
    assert not (home / "Documents" / "ws2").exists()


def test_cli_root_version_prints_version_without_side_effects(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import ciao

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == f"ciao {ciao.__version__}\n"
    assert list(tmp_path.iterdir()) == []


def test_cli_root_version_module_entry(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    import ciao

    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "ciao", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": str(root), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"ciao {ciao.__version__}\n"
    assert list(tmp_path.iterdir()) == []


def test_gws_is_not_a_cli_command() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["gws", "work", "gmail"])
    assert excinfo.value.code == 2


def test_setup_cli_accepts_existing_separate_notes_without_git(tmp_path, monkeypatch, capsys):
    from ciao import cli
    workspace = tmp_path / "engine"
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "idea.md").write_text("private", encoding="utf-8")
    (notes / "nested").mkdir()
    (notes / "nested" / "MEMORY.md").write_text("nested", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path / "no-executables"))
    result = cli.main(["setup", "--workspace", str(workspace), "--notes-folder", str(notes),
        "--auth-token", "password", "--launch-agents-dir", str(tmp_path / "agents"),
        "--app-dir", str(tmp_path / "apps")])
    assert result == 0
    assert (notes / "idea.md").read_text(encoding="utf-8") == "private"
    assert not (notes / ".git").exists()
    assert sorted(path.name for path in notes.iterdir()) == ["idea.md", "nested"]
    registry = json.loads((workspace / ".runtime" / "workspaces.json").read_text())
    assert registry[0]["vault_root"] == str(notes.resolve())
    assert len(registry) == 1
    assert str(notes.resolve()) in capsys.readouterr().out


@pytest.mark.parametrize("replace", [False, True])
def test_setup_cli_does_not_recreate_disappeared_notes(tmp_path, monkeypatch, replace):
    from ciao import cli

    workspace = tmp_path / "engine"
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "existing.md").write_text("private", encoding="utf-8")
    moved = tmp_path / "moved-notes"
    original = cli.ensure_workspace_git

    def remove_notes_during_setup(*args, **kwargs):
        if not moved.exists():
            notes.rename(moved)
            if replace:
                notes.mkdir()
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, "ensure_workspace_git", remove_notes_during_setup)
    result = cli.main(["setup", "--workspace", str(workspace), "--notes-folder", str(notes),
        "--auth-token", "password", "--launch-agents-dir", str(tmp_path / "agents"),
        "--app-dir", str(tmp_path / "apps")])
    assert result != 0
    assert notes.exists() == replace
    if replace:
        assert not list(notes.iterdir())
    assert moved.is_dir()
    assert (moved / "existing.md").read_text(encoding="utf-8") == "private"
    if replace:
        notes.rmdir()
    moved.rename(notes)
    monkeypatch.setattr(cli, "ensure_workspace_git", original)
    assert cli.main(["setup", "--workspace", str(workspace), "--notes-folder", str(notes),
        "--auth-token", "different", "--launch-agents-dir", str(tmp_path / "agents"),
        "--app-dir", str(tmp_path / "apps")]) != 0
    assert cli.main(["setup", "--workspace", str(workspace), "--notes-folder", str(notes),
        "--auth-token", "password", "--launch-agents-dir", str(tmp_path / "agents"),
        "--app-dir", str(tmp_path / "apps")]) == 0
    assert not (workspace / ".runtime" / "external-setup-pending.json").exists()
    assert (notes / "existing.md").read_text(encoding="utf-8") == "private"
