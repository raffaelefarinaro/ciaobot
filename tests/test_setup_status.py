from __future__ import annotations

import os
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.setup_marker import read_setup_vault_mode
from ciao.setup_status import claude_auth_status, claude_status_probe, setup_status
from ciao.web.auth import AuthMiddleware
from ciao.web.routes_api import (
    provider_connection_action,
    setup_finish_endpoint,
    setup_inspect_folder_endpoint,
    setup_list_dirs_endpoint,
    setup_mkdir_endpoint,
    setup_status_endpoint,
)


# The setup routes check the TCP peer; TestClient defaults to "testclient".
_LOOPBACK_PEER = ("127.0.0.1", 50000)


@pytest.fixture(autouse=True)
def _stub_auth_list(monkeypatch):
    monkeypatch.setattr("ciao.providers.opencode.bounded_output", lambda *a, **k: (0, b"[]"))


@pytest.fixture(autouse=True)
def claude_cli_present(monkeypatch):
    """Pretend the ``claude`` CLI is installed.

    Claude readiness now reports the install step first, so without this the
    rest of the module's expectations would depend on whether the machine
    running the tests happens to have Claude Code on PATH.
    """
    monkeypatch.setattr(
        "ciao.setup_status.claude_cli_path", lambda: "/usr/local/bin/claude"
    )
    # Claude chats run through the ACP adapter, so the adapter counts as installed too.
    monkeypatch.setattr(
        "ciao.setup_status.claude_adapter_path", lambda: "/usr/local/bin/claude-agent-acp"
    )
    monkeypatch.setattr("ciao.setup_status._cli_version", lambda binary: "2.0.0 (Claude Code)")
    monkeypatch.setattr(
        "ciao.setup_status.claude_auth_status",
        lambda *args, **kwargs: {
            "logged_in": False,
            "unparseable": False,
            "email": "",
            "org_name": "",
        },
    )


def _config(tmp_path, env_extra: dict[str, str] | None = None) -> CiaoConfig:
    env = {
        "PWA_AUTH_TOKEN": "test-token",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        "CIAO_VAULT_ROOT": "memory-vault",
        "CIAO_OLLAMA_API_KEY": "",
    }
    env.update(env_extra or {})
    return CiaoConfig.from_env(env)


def _sign_in_claude(monkeypatch) -> None:
    """Report the Claude CLI as signed in, so a provider is ready."""
    monkeypatch.setattr(
        "ciao.setup_status.claude_auth_status",
        lambda *args, **kwargs: {
            "logged_in": True,
            "unparseable": False,
            "email": "operator@example.com",
            "org_name": "",
        },
    )


def test_setup_status_reports_workspace_and_required_config(tmp_path, monkeypatch) -> None:
    _sign_in_claude(monkeypatch)
    config = _config(tmp_path)
    (tmp_path / "memory-vault").mkdir()

    data = setup_status(
        config,
        env={"PWA_AUTH_TOKEN": "test-token"},
    )

    checks = {row["id"]: row for row in data["checks"]}
    assert data["workspace_root"] == str(tmp_path.resolve())
    assert data["vault_root"] == str((tmp_path / "memory-vault").resolve())
    assert checks["workspace"]["ok"] is True
    assert checks["vault"]["ok"] is True
    assert checks["pwa_auth_token"]["ok"] is True
    assert "push_contact" not in checks
    assert data["configured"] is True


def test_setup_status_reports_the_workspace_guide(tmp_path, monkeypatch) -> None:
    """The optional guide check tracks whether the workspace has a guide.

    It used to check that AGENTS.md resolved to CLAUDE.md. There is one guide
    now (ciao/workspace_guide.py), so the pair — and the check for it — is
    gone; a workspace that has been migrated correctly must not be reported
    as broken for lacking the second file.
    """
    _sign_in_claude(monkeypatch)
    config = _config(tmp_path)
    (tmp_path / "memory-vault").mkdir()
    env = {"PWA_AUTH_TOKEN": "test-token"}

    checks = {row["id"]: row for row in setup_status(config, env=env)["checks"]}
    assert checks["workspace_guides"]["ok"] is False
    assert checks["workspace_guides"]["required"] is False

    (tmp_path / "AGENTS.md").write_text("# Guide\n", encoding="utf-8")
    data = setup_status(config, env=env)
    checks = {row["id"]: row for row in data["checks"]}
    assert checks["workspace_guides"]["ok"] is True
    assert checks["workspace_guides"]["detail"].endswith("AGENTS.md")
    assert data["configured"] is True


def test_setup_status_accepts_a_pre_migration_guide(tmp_path) -> None:
    """An install that has not run the guide migration still has its guide."""
    config = _config(tmp_path)
    (tmp_path / "memory-vault").mkdir()
    env = {"PWA_AUTH_TOKEN": "test-token"}
    (tmp_path / "CLAUDE.md").write_text("# Legacy guide\n", encoding="utf-8")

    checks = {row["id"]: row for row in setup_status(config, env=env)["checks"]}
    assert checks["workspace_guides"]["ok"] is True
    assert checks["workspace_guides"]["detail"].endswith("CLAUDE.md")


def test_setup_status_reports_missing_required_config(tmp_path) -> None:
    config = _config(tmp_path)

    data = setup_status(config, env={})

    checks = {row["id"]: row for row in data["checks"]}
    assert checks["vault"]["ok"] is False
    assert checks["pwa_auth_token"]["ok"] is True
    assert data["configured"] is False


def test_setup_status_survives_a_deleted_working_directory(tmp_path, monkeypatch) -> None:
    """A dead cwd must not turn the readiness payload into a 500.

    The desktop deploy relaunch can leave the engine with a cwd inside a staging
    bundle that the swap then renames, and ``os.getcwd()`` raises there.
    """
    config = _config(tmp_path)
    (tmp_path / "memory-vault").mkdir()
    expected_root = str(tmp_path.resolve())

    def _dead_cwd() -> str:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(os, "getcwd", _dead_cwd)

    data = setup_status(
        config,
        env={"PWA_AUTH_TOKEN": "test-token"},
    )

    checks = {row["id"]: row for row in data["checks"]}
    assert data["workspace_root"] == expected_root
    assert checks["workspace"]["ok"] is True
    assert checks["vault"]["ok"] is True

    # A config without an explicit workspace_root has no root to fall back to,
    # but the payload must still be produced instead of raising.
    bare = setup_status(
        SimpleNamespace(pwa_auth_token="test-token", suggested_workspace=None), env={}
    )
    assert isinstance(bare["checks"], list)
    assert "providers" in bare


def test_setup_status_marks_bootstrap_mode(tmp_path) -> None:
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})

    data = setup_status(config, env={})

    assert data["mode"] == "bootstrap"
    assert data["bootstrap"] is True
    assert data["configured"] is False


def test_setup_status_detects_claude_cli_oauth(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(
        "ciao.setup_status.claude_auth_status",
        lambda *args, **kwargs: {
            "logged_in": True,
            "unparseable": False,
            "email": "operator@example.com",
            "org_name": "Example Org",
        },
    )
    data = setup_status(
        config,
        env={},
    )
    assert data["providers"]["claude"]["ok"] is True
    assert data["providers"]["claude"]["auth"] == "oauth"
    assert "operator@example.com" in data["providers"]["claude"]["detail"]


def test_setup_status_does_not_treat_an_api_key_as_claude_auth(tmp_path) -> None:
    """Claude auth is the CLI login only; an inherited API key is not reported."""
    secret = "sk-ant-secret-value"
    config = _config(tmp_path)
    data = setup_status(config, env={"ANTHROPIC_API_KEY": secret})

    claude = data["providers"]["claude"]
    assert claude["ok"] is False
    assert claude["auth"] == "missing"
    assert secret not in json.dumps(data)


def test_setup_status_does_not_treat_oauth_account_metadata_as_auth(tmp_path) -> None:
    """Desktop metadata alone must not make setup claim Claude is connected."""
    config = _config(tmp_path)
    config_path = tmp_path / ".claude.json"
    config_path.write_text(
        '{"oauthAccount":{"emailAddress":"operator@example.com",'
        '"accountUuid":"abc","organizationName":"Example Org"}}',
        encoding="utf-8",
    )

    data = setup_status(config, env={}, claude_config_path=config_path)

    claude = data["providers"]["claude"]
    assert claude["ok"] is False
    assert claude["auth"] == "missing"


def test_setup_status_ignores_empty_oauth_account(tmp_path) -> None:
    config = _config(tmp_path)
    config_path = tmp_path / ".claude.json"
    config_path.write_text('{"oauthAccount":null}', encoding="utf-8")

    data = setup_status(config, env={}, claude_config_path=config_path)

    assert data["providers"]["claude"]["ok"] is False
    assert data["providers"]["claude"]["auth"] == "missing"


def test_setup_status_malformed_probe_never_falls_back_to_desktop_metadata(
    tmp_path, monkeypatch
) -> None:
    """Unparseable probe output fails closed even with Desktop-only metadata.

    Issue #347: an `oauthAccount` block in ~/.claude.json can belong to the
    Desktop app while the CLI credential store is signed out, so malformed
    probe output must report missing auth rather than guess from metadata.
    """
    config = _config(tmp_path)
    config_path = tmp_path / ".claude.json"
    config_path.write_text(
        '{"oauthAccount":{"emailAddress":"operator@example.com",'
        '"accountUuid":"abc","organizationName":"Example Org"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ciao.setup_status.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="update notice, not json", returncode=0),
    )

    data = setup_status(config, env={}, claude_config_path=config_path)

    claude = data["providers"]["claude"]
    assert claude["ok"] is False
    assert claude["auth"] == "missing"


def test_claude_auth_status_probe_argv_uses_auth_status_json(monkeypatch) -> None:
    """The probe must keep invoking `claude auth status --json` (issue #347)."""
    captured: dict[str, object] = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(stdout='{"loggedIn":false}', returncode=0)

    monkeypatch.setattr("ciao.setup_status.subprocess.run", fake_run)

    claude_auth_status("/claude")

    assert captured["cmd"] == ["/claude", "auth", "status", "--json"]


def test_claude_auth_status_parses_logged_in_json(monkeypatch) -> None:
    monkeypatch.setattr(
        "ciao.setup_status.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout='{"loggedIn":true,"email":"a@example.com","orgName":"Org"}',
            returncode=0,
        ),
    )
    assert claude_auth_status("/claude") == {
        "logged_in": True,
        "unparseable": False,
        "email": "a@example.com",
        "org_name": "Org",
        "cli_too_old": False,
    }


def test_claude_auth_status_timeout_is_not_authenticated(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 5)

    monkeypatch.setattr("ciao.setup_status.subprocess.run", timeout)
    assert claude_auth_status("/claude")["logged_in"] is False
    assert claude_auth_status("/claude")["unparseable"] is False


def test_claude_auth_status_malformed_json_is_marked_legacy_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "ciao.setup_status.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="not json", returncode=0),
    )
    status = claude_auth_status("/claude")
    assert status["logged_in"] is False
    assert status["unparseable"] is True


def test_claude_auth_status_missing_binary_is_not_authenticated(monkeypatch) -> None:
    monkeypatch.setattr(
        "ciao.setup_status.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert claude_auth_status("/missing")["logged_in"] is False


def test_claude_auth_status_detects_unknown_auth_subcommand(monkeypatch) -> None:
    """An old CLI without `claude auth` must be told apart from signed-out
    (issue #354): it still fails closed on `logged_in`, but flags
    `cli_too_old` instead of leaving the caller to mistake it for a real
    "not signed in" reading."""
    monkeypatch.setattr(
        "ciao.setup_status.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="", stderr="error: unknown command 'auth'", returncode=1
        ),
    )
    status = claude_auth_status("/claude")
    assert status["logged_in"] is False
    assert status["cli_too_old"] is True


def test_claude_auth_status_unknown_subcommand_detected_on_stdout(monkeypatch) -> None:
    """Some CLIs print the unknown-command message to stdout, not stderr."""
    monkeypatch.setattr(
        "ciao.setup_status.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="Unrecognized command: auth", stderr="", returncode=1
        ),
    )
    assert claude_auth_status("/claude")["cli_too_old"] is True


def test_claude_auth_status_other_nonzero_exit_is_not_cli_too_old(monkeypatch) -> None:
    """A generic failure (e.g. a real error) must not be misread as an old CLI."""
    monkeypatch.setattr(
        "ciao.setup_status.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="", stderr="permission denied", returncode=1
        ),
    )
    status = claude_auth_status("/claude")
    assert status["logged_in"] is False
    assert status["cli_too_old"] is False


def test_setup_status_reports_cli_too_old_as_actionable_and_still_fails_closed(
    tmp_path, monkeypatch
) -> None:
    """The wizard must not report "signed out" for a CLI that cannot even run
    `claude auth status` (issue #354) — it should point at upgrading instead,
    while still keeping `ok=False` (fail closed)."""
    monkeypatch.setattr(
        "ciao.setup_status.claude_auth_status",
        lambda binary, env=None: {
            "logged_in": False,
            "unparseable": False,
            "email": "",
            "org_name": "",
            "cli_too_old": True,
        },
    )
    config = _config(tmp_path)
    claude = setup_status(config, env={})["providers"]["claude"]
    assert claude["ok"] is False
    assert claude["auth"] == "missing"
    assert "too old" in claude["detail"].lower()
    assert "upgrade" in claude["detail"].lower()


def test_setup_status_explains_desktop_login_is_app_private(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("ciao.setup_status.claude_app_path", lambda: "/Applications/Claude.app")
    config = _config(tmp_path)
    claude = setup_status(config, env={})["providers"]["claude"]
    assert claude["ok"] is False
    assert "app-private" in claude["detail"]
    # `ciao auth claude` only exists on a dev checkout: an app install puts no
    # `ciao` on PATH, so the wizard must hand out Claude's own login command.
    assert "claude auth login" in claude["detail"]
    assert "ciao auth" not in claude["detail"]
    assert "no separate CLI install" in claude["detail"]


def test_setup_status_reports_a_missing_claude_cli_as_an_install_step(
    tmp_path, monkeypatch
) -> None:
    """No CLI means no chats, so setup asks for the install, not for a login."""
    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: "")
    monkeypatch.setattr("ciao.setup_status.claude_app_path", lambda: "")
    monkeypatch.setattr("ciao.tool_path.resolve_on_terminal_path", lambda cmd: None)
    config = _config(tmp_path)

    claude = setup_status(config, env={})["providers"]["claude"]

    assert claude["ok"] is False
    assert claude["auth"] == "not_installed"
    assert claude["install_url"].startswith("https://code.claude.com/docs/")
    assert "install" in claude["command"]
    assert "not installed" in claude["detail"]
    # The installer drops `claude` into ~/.local/bin, which a default macOS
    # PATH omits, so the wizard offers the PATH line as a second step.
    assert "$HOME/.local/bin" in claude["path_command"]


def test_setup_status_names_the_desktop_app_when_only_the_cli_is_missing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: "")
    monkeypatch.setattr("ciao.setup_status.claude_app_path", lambda: "/Applications/Claude.app")
    config = _config(tmp_path)

    claude = setup_status(config, env={})["providers"]["claude"]

    assert claude["auth"] == "not_installed"
    assert claude["app_path"] == "/Applications/Claude.app"
    assert "/Applications/Claude.app" in claude["detail"]


def test_setup_status_reports_the_resolved_cli_path(tmp_path) -> None:
    """The wizard shows which binary it would run, not just that one exists."""
    config = _config(tmp_path)

    claude = setup_status(config, env={})["providers"]["claude"]

    assert claude["cli_path"] == "/usr/local/bin/claude"


def test_setup_status_offers_claudes_own_login_not_ciao_auth(tmp_path, monkeypatch) -> None:
    """`ciao` is not on PATH in an app install (the engine lives inside
    Ciaobot.app), so a wizard that told users to run `ciao auth claude` sent
    them to a command their shell could not resolve -- or, when some unrelated
    `ciao` was installed, to its "Unknown command 'auth'" error."""
    monkeypatch.setattr(
        "ciao.tool_path.resolve_on_terminal_path", lambda cmd: "/usr/local/bin/claude"
    )
    config = _config(tmp_path)

    claude = setup_status(config, env={})["providers"]["claude"]

    assert claude["command"] == "claude auth login"


def test_setup_status_names_a_bundled_claude_by_path(tmp_path, monkeypatch) -> None:
    """Ciaobot normally runs the `claude` bundled with the Agent SDK, which is
    on nobody's PATH: when the user's own shell has no `claude`, a bare
    `claude auth login` would fail for them too."""
    bundled = "/opt/sdk/claude_agent_sdk/_bundled/claude"
    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: bundled)
    monkeypatch.setattr("ciao.tool_path.resolve_on_terminal_path", lambda cmd: None)
    config = _config(tmp_path)

    claude = setup_status(config, env={})["providers"]["claude"]

    assert claude["command"] == f"{bundled} auth login"


def test_setup_status_offers_the_path_line_for_a_cli_the_terminal_cannot_find(
    tmp_path, monkeypatch
) -> None:
    """The install step is not the only moment the PATH line is needed: once
    `claude` exists in ~/.local/bin but the user's PATH still omits it, they
    are about to be handed a command to type into that same terminal."""
    monkeypatch.setattr(
        "ciao.setup_status.claude_cli_path",
        lambda: str(Path.home() / ".local" / "bin" / "claude"),
    )
    monkeypatch.setattr("ciao.tool_path.resolve_on_terminal_path", lambda cmd: None)
    config = _config(tmp_path)

    claude = setup_status(config, env={})["providers"]["claude"]

    assert claude["path_command"].startswith("echo 'export PATH=\"$HOME/.local/bin")


def test_setup_status_offers_no_path_line_for_the_bundled_cli(tmp_path, monkeypatch) -> None:
    """The CLI inside the app is not something to add to a user's PATH -- the
    login command names it in full instead."""
    bundled = "/opt/sdk/claude_agent_sdk/_bundled/claude"
    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: bundled)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: bundled)
    monkeypatch.setattr("ciao.tool_path.resolve_on_terminal_path", lambda cmd: None)
    config = _config(tmp_path)

    claude = setup_status(config, env={})["providers"]["claude"]

    assert "path_command" not in claude


def test_setup_status_offers_no_path_line_when_the_terminal_finds_claude(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "ciao.tool_path.resolve_on_terminal_path", lambda cmd: "/usr/local/bin/claude"
    )
    config = _config(tmp_path)

    claude = setup_status(config, env={})["providers"]["claude"]

    assert "path_command" not in claude


def test_claude_path_command_names_the_file_a_login_shell_reads(monkeypatch) -> None:
    """macOS terminals start login shells: bash reads ~/.bash_profile, and
    ~/.bashrc would fix only the shell the user is sitting in."""
    from ciao.setup_status import claude_path_command

    monkeypatch.setenv("SHELL", "/bin/bash")
    assert claude_path_command().endswith(">> ~/.bash_profile && source ~/.bash_profile")
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/fish")
    assert claude_path_command() == "fish_add_path $HOME/.local/bin"
    monkeypatch.delenv("SHELL")
    assert "~/.zshrc" in claude_path_command()


def test_claude_path_command_delegates_to_the_helper(monkeypatch) -> None:
    from ciao.setup_status import claude_path_command

    calls: list[tuple[str, bool]] = []

    def record(directory: str, *, persist: bool) -> str:
        calls.append((directory, persist))
        return "HINT"

    monkeypatch.setattr("ciao.setup_status.path_hint", record)

    assert claude_path_command("/x/bin") == "HINT"
    assert calls == [("/x/bin", True)]


def test_setup_status_route_is_public_before_login(tmp_path) -> None:
    config = _config(tmp_path)
    (tmp_path / "memory-vault").mkdir()
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/setup-status", setup_status_endpoint, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = False
    app.state.installer_run_id = None
    app.state.serializer = serializer

    resp = TestClient(app).get("/api/setup-status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["native_bootstrap"] is False
    assert {
        "configured",
        "bootstrap",
        "mode",
        "workspace_root",
        "vault_root",
        "suggested_workspace",
        "checks",
        "providers",
        "provider_ready",
    } <= payload.keys()
    assert payload["checks"][0]["id"] == "workspace"


def test_setup_status_route_reports_native_bootstrap_context(tmp_path) -> None:
    config = _config(tmp_path)
    config.suggested_workspace = tmp_path / "suggested-workspace"
    (tmp_path / "memory-vault").mkdir()
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/setup-status", setup_status_endpoint, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = False
    app.state.installer_run_id = None
    app.state.native_bootstrap = {
        "run_id": "native-run-id-sentinel-76-a91f",
        "workspace": "/private/native-bootstrap-workspace-sentinel-76-a91f",
    }
    app.state.serializer = serializer

    resp = TestClient(app).get("/api/setup-status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["native_bootstrap"] is True
    assert {
        "configured",
        "bootstrap",
        "mode",
        "workspace_root",
        "vault_root",
        "suggested_workspace",
        "checks",
        "providers",
        "provider_ready",
    } <= payload.keys()
    assert payload["suggested_workspace"] == str(config.suggested_workspace)
    assert "native-run-id-sentinel-76-a91f" not in resp.text
    assert "/private/native-bootstrap-workspace-sentinel-76-a91f" not in resp.text



def _fake_engine_python(tmp_path: Path) -> str:
    """The interpreter setup points the service at; setup only writes it into the plist, so a made-up path will do."""
    return "/opt/ciao/bin/python"

def test_setup_finish_writes_real_workspace_and_requests_restart(tmp_path, monkeypatch) -> None:
    # Guard the env handoff assertions below: monkeypatch restores these
    # after the endpoint mutates os.environ directly.
    monkeypatch.setenv("CIAO_WORKSPACE", "")
    monkeypatch.setenv("PWA_PORT", "")
    monkeypatch.setenv("PWA_AUTH_TOKEN", "")
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    restarts: list[int] = []
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = False
    app.state.installer_run_id = None
    app.state.serializer = serializer
    app.state.request_restart = restarts.append

    workspace = tmp_path / "workspace"
    notes = tmp_path / "notes"
    launch_agents = tmp_path / "LaunchAgents"
    apps = tmp_path / "Applications"
    resp = TestClient(app, base_url="http://localhost:8443", client=_LOOPBACK_PEER).post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(workspace),
            "vault_root": str(notes),
            "launch_agents_dir": str(launch_agents),
            "app_dir": str(apps),
            "python": _fake_engine_python(tmp_path),
            "port": 9443,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["restart_requested"] is True
    assert restarts == [75]
    # The env handoff for the re-exec'd foreground `ciao run`: without it the
    # relaunched process boots back into bootstrap mode.
    assert os.environ["CIAO_WORKSPACE"] == str(workspace.resolve())
    assert os.environ["PWA_PORT"] == "9443"
    # The wizard's password becomes the dashboard password, and it must reach the
    # relaunched process through the environment too: load_dotenv would not
    # override a PWA_AUTH_TOKEN already set for the bootstrap run.
    assert os.environ["PWA_AUTH_TOKEN"] == "wizard-pass"
    env_text = (workspace / ".env").read_text(encoding="utf-8")
    assert "PWA_AUTH_TOKEN=wizard-pass" in env_text
    assert "PWA_AUTH_REQUIRED" not in env_text
    assert "CIAO_PUSH_CONTACT" not in env_text
    assert f"CIAO_VAULT_ROOT={notes}" in env_text
    assert (notes / "MEMORY.md").is_file()
    assert not (workspace / "memory-vault" / "MEMORY.md").exists()
    assert (launch_agents / "com.ciao.server.plist").is_file()
    # The wizard no longer writes the retired rumps launcher bundle or its
    # LaunchAgent; Ciaobot.app is the menu bar.
    assert not (apps / "Ciaobot Server.app").exists()
    assert not (launch_agents / "com.ciao.menubar.plist").exists()


def _finish_client(tmp_path) -> TestClient:
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = False
    app.state.installer_run_id = None
    app.state.serializer = serializer
    app.state.request_restart = lambda code: None
    return TestClient(app, base_url="http://localhost:8443", client=_LOOPBACK_PEER)


def test_setup_finish_autodetects_scratch_for_empty_folder(tmp_path) -> None:
    """Without an explicit vault_mode, an empty workspace folder starts from
    scratch in the chosen logical workspace's named vault folder."""
    ws = tmp_path / "fresh"
    ws.mkdir()
    resp = _finish_client(tmp_path).post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(ws),
            "workspace_name": "life",
            "launch_agents_dir": str(tmp_path / "LaunchAgents"),
            "app_dir": str(tmp_path / "Applications"),
        },
    )
    assert resp.status_code == 200
    env_text = (ws / ".env").read_text(encoding="utf-8")
    assert "CIAO_VAULT_MODE" not in env_text
    assert read_setup_vault_mode(ws / ".runtime") == "scratch"
    # `<workspace>/memory-vault`: the wizard creates the per-workspace layout
    # directly, so a new install never has a shared vault to migrate.
    assert (ws / "life" / "memory-vault" / "MEMORY.md").is_file()
    assert not (ws / "memory-vault").exists()
    # The wizard's first workspace replaces the legacy personal+work
    # fallback: a one-entry registry with the chosen name.
    import json as _json

    registry = _json.loads((ws / ".runtime" / "workspaces.json").read_text(encoding="utf-8"))
    assert [w["name"] for w in registry] == ["life"]
    assert registry[0]["vault_root"] == "life/memory-vault"
    # Setup links no Google account: which accounts exist is the user's choice,
    # made in Settings → Overview → All workspaces after onboarding.
    assert registry[0]["gws_profile"] == ""


def test_setup_finish_rejects_a_traversal_workspace_name(tmp_path) -> None:
    workspace = tmp_path / "fresh"

    response = _finish_client(tmp_path).post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(workspace),
            "workspace_name": "../outside",
            "launch_agents_dir": str(tmp_path / "LaunchAgents"),
            "app_dir": str(tmp_path / "Applications"),
        },
    )

    assert response.status_code == 400
    assert "workspace name" in response.json()["error"]
    assert not workspace.exists()
    assert not (tmp_path / "outside").exists()


def test_setup_finish_autodetects_existing_notes_folder(tmp_path) -> None:
    """A folder with visible content is treated as existing notes: the vault
    lives in place and the onboarding agent adapts it."""
    ws = tmp_path / "notes"
    ws.mkdir()
    (ws / "ideas.md").write_text("# Ideas\n", encoding="utf-8")
    resp = _finish_client(tmp_path).post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(ws),
            "workspace_name": "journal",
            "launch_agents_dir": str(tmp_path / "LaunchAgents"),
            "app_dir": str(tmp_path / "Applications"),
        },
    )
    assert resp.status_code == 200
    env_text = (ws / ".env").read_text(encoding="utf-8")
    assert "CIAO_VAULT_MODE" not in env_text
    assert read_setup_vault_mode(ws / ".runtime") == "existing"
    assert "CIAO_VAULT_ROOT=." in env_text
    assert (ws / "MEMORY.md").is_file()
    assert not (ws / "memory-vault").exists()
    registry = json.loads(
        (ws / ".runtime" / "workspaces.json").read_text(encoding="utf-8")
    )
    assert registry[0]["name"] == "journal"
    assert registry[0]["vault_root"] == "."


def test_setup_finish_records_vault_mode_for_folder_with_its_own_env(tmp_path) -> None:
    """A notes folder that already holds an unrelated `.env` is still a first
    setup: setup merges into that file, and the onboarding chat must still
    learn that the folder is existing notes, not a scratch vault."""
    ws = tmp_path / "notes"
    ws.mkdir()
    (ws / "ideas.md").write_text("# Ideas\n", encoding="utf-8")
    (ws / ".env").write_text("SOME_TOOL_KEY=abc\n", encoding="utf-8")
    resp = _finish_client(tmp_path).post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(ws),
            "workspace_name": "journal",
            "launch_agents_dir": str(tmp_path / "LaunchAgents"),
            "app_dir": str(tmp_path / "Applications"),
        },
    )
    assert resp.status_code == 200
    assert "SOME_TOOL_KEY=abc" in (ws / ".env").read_text(encoding="utf-8")
    assert read_setup_vault_mode(ws / ".runtime") == "existing"

    loaded = CiaoConfig.from_env(
        {
            "PWA_AUTH_TOKEN": "test-token",
            "CIAO_WORKSPACE": str(ws),
            "CIAO_VAULT_ROOT": ".",
            "CIAO_RUNTIME_ROOT": str(ws / ".runtime"),
            "CIAO_OLLAMA_LOCAL_DISCOVERY": "0",
        }
    )
    assert loaded.workspace_vault_root("journal") == ws


def test_auth_check_reports_unauthenticated_in_bootstrap(tmp_path) -> None:
    """Bootstrap mode returns 401 from /api/auth/check so the SPA routes to
    the login view, where the first-run wizard renders — even for a caller that
    already carries a valid session cookie for the throwaway workspace."""
    from ciao.web.auth import SESSION_COOKIE
    from ciao.web.routes_auth import auth_check

    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/auth/check", auth_check, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    client = TestClient(app, base_url="http://localhost:8443", client=_LOOPBACK_PEER)
    client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))

    app.state.config = CiaoConfig.from_env(
        {"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")}
    )
    assert client.get("/api/auth/check").status_code == 401

    # Configured workspace: the session cookie is what makes it a 200, since a
    # token in .env means password protection is on.
    app.state.config = CiaoConfig.from_env(
        {"PWA_AUTH_TOKEN": "tok", "CIAO_WORKSPACE": str(tmp_path / "ws")}
    )
    assert client.get("/api/auth/check").status_code == 200
    client.cookies.clear()
    assert client.get("/api/auth/check").status_code == 401


def test_auth_check_requires_session_when_password_enabled(tmp_path) -> None:
    """Host auth_check must mirror AuthMiddleware: a session is always required."""
    from ciao.web.auth import SESSION_COOKIE
    from ciao.web.routes_auth import auth_check

    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/auth/check", auth_check, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = CiaoConfig.from_env(
        {
            "PWA_AUTH_TOKEN": "secret",
            "CIAO_WORKSPACE": str(tmp_path / "ws"),
        }
    )
    client = TestClient(app, base_url="http://localhost:8443", client=_LOOPBACK_PEER)

    assert client.get("/api/auth/check").status_code == 401

    cookie = {SESSION_COOKIE: serializer.dumps({"user": "owner"})}
    assert client.get("/api/auth/check", cookies=cookie).status_code == 200


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd handoff is macOS-only")
def test_setup_finish_foreground_handoff_to_launchd(tmp_path, monkeypatch) -> None:
    """An interactive foreground `ciao run` hands the server to launchd:
    finish schedules the detached agent loader and requests a clean exit
    (code 0, no re-exec) so the user can close the terminal."""
    import ciao.web.routes_api as routes_api

    home = tmp_path / "home"
    home.mkdir()
    # `_isolate_launch_agents` redirects the default away from the real
    # ~/Library/LaunchAgents; re-point it at this test's faked home so the
    # assertion still exercises per-user default resolution.
    monkeypatch.setenv(
        "CIAO_LAUNCH_AGENTS_DIR", str(home / "Library" / "LaunchAgents")
    )
    monkeypatch.setenv("CIAO_WORKSPACE", "")
    monkeypatch.setenv("PWA_PORT", "")
    monkeypatch.setattr(routes_api, "_interactive_foreground_run", lambda: True)
    # Intercept only the handoff shell and launchctl; setup_workspace's git
    # calls (run → Popen) must stay real.
    real_popen = routes_api.subprocess.Popen
    popen_calls: list[list[str]] = []

    def fake_popen(cmd, *a, **k):
        if cmd and cmd[0] == "/bin/sh":
            popen_calls.append(list(cmd))
            return SimpleNamespace(pid=1)
        return real_popen(cmd, *a, **k)

    monkeypatch.setattr(routes_api.subprocess, "Popen", fake_popen)
    real_run = routes_api.subprocess.run
    run_calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        if cmd and cmd[0] == "launchctl":
            run_calls.append(list(cmd))
            return SimpleNamespace(returncode=0)
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(routes_api.subprocess, "run", fake_run)

    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    restarts: list[int] = []
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = False
    app.state.installer_run_id = None
    app.state.serializer = serializer
    app.state.request_restart = restarts.append

    resp = TestClient(app, base_url="http://localhost:8443", client=_LOOPBACK_PEER).post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(tmp_path / "workspace"),
            "app_dir": str(tmp_path / "Applications"),
        },
    )

    assert resp.status_code == 200
    # No launch_agents_dir override: plists land in the (faked) per-user dir.
    assert (home / "Library" / "LaunchAgents" / "com.ciao.server.plist").is_file()
    # Clean exit instead of the re-exec restart code.
    assert restarts == [0]
    # The detached helper loads the server agent after this process exits.
    assert popen_calls, "launchd loader was not scheduled"
    script = " ".join(popen_calls[0])
    assert "com.ciao.server.plist" in script
    assert "launchctl" in script


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd handoff is macOS-only")
@pytest.mark.parametrize("handoff_result", [True, False])
def test_setup_finish_handoff_goes_through_the_backend(
    handoff_result: bool, tmp_path, monkeypatch
) -> None:
    """The wizard's finish asks the seam, not routes_api, to spawn the helper."""
    import ciao.web.routes_api as routes_api

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CIAO_WORKSPACE", "")
    monkeypatch.setenv("PWA_PORT", "")
    monkeypatch.setattr(routes_api, "_interactive_foreground_run", lambda: True)
    calls: list[bool] = []

    class FakeBackend:
        """Answers only the handoff; the directory methods stay real."""

        name = "fake"

        def agents_dir(self) -> Path:
            return Path(home / "Library" / "LaunchAgents")

        def live_agents_dir(self) -> Path:
            return Path(home / "Library" / "LaunchAgents")

        def is_live_agents_dir(self, path: Path) -> bool:
            return Path(path) == self.live_agents_dir()

        def bootout_agent(self, label: str) -> None:
            return None

        def load_agent(self, definition: Path) -> int:
            return 0

        def schedule_server_handoff(self) -> bool:
            calls.append(handoff_result)
            return handoff_result

    monkeypatch.setattr(
        routes_api.service_backend,
        "current_backend",
        lambda: FakeBackend(),
    )
    real_popen = routes_api.subprocess.Popen

    def fail_popen(cmd, *a, **k):
        if cmd and cmd[0] == "/bin/sh":
            raise AssertionError("routes_api must not spawn the helper itself")
        return real_popen(cmd, *a, **k)

    monkeypatch.setattr(routes_api.subprocess, "Popen", fail_popen)

    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    restarts: list[int] = []
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = False
    app.state.installer_run_id = None
    app.state.serializer = serializer
    app.state.request_restart = restarts.append

    resp = TestClient(app, base_url="http://localhost:8443", client=_LOOPBACK_PEER).post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(tmp_path / "workspace"),
            "app_dir": str(tmp_path / "Applications"),
        },
    )

    assert resp.status_code == 200
    assert calls == [handoff_result]
    expected = 0 if handoff_result else routes_api.RESTART_EXIT_CODE
    assert restarts == [expected]


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd handoff is macOS-only")
def test_setup_finish_in_app_mode_never_hands_off_to_launchd(
    tmp_path, monkeypatch
) -> None:
    """The Mac app owns the bundled engine: no plist, no handoff helper, and
    the restart keeps the plain in-place restart code for the app to watch."""
    import ciao.web.routes_api as routes_api
    from ciao import package_version
    from ciao.config import RESTART_EXIT_CODE

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CIAO_WORKSPACE", "")
    monkeypatch.setenv("PWA_PORT", "")
    monkeypatch.setattr(routes_api, "_interactive_foreground_run", lambda: True)
    monkeypatch.setattr(
        package_version, "running_from_app_bundle", lambda: True
    )
    # The app-mode finish must not even reach the launchd handoff seam, let
    # alone the detached loader helper (Popen) or launchctl itself.
    handoff_calls: list[bool] = []

    class FakeBackend:
        """Answers the handoff seam loudly; the rest stays real."""

        name = "fake"

        def agents_dir(self) -> Path:
            return Path(home / "Library" / "LaunchAgents")

        def live_agents_dir(self) -> Path:
            return Path(home / "Library" / "LaunchAgents")

        def is_live_agents_dir(self, path: Path) -> bool:
            return Path(path) == self.live_agents_dir()

        def schedule_server_handoff(self) -> bool:
            handoff_calls.append(True)
            return True

    monkeypatch.setattr(
        routes_api.service_backend, "current_backend", lambda: FakeBackend()
    )
    real_popen = routes_api.subprocess.Popen
    popen_calls: list[list[str]] = []

    def fake_popen(cmd, *a, **k):
        if cmd and cmd[0] == "/bin/sh":
            popen_calls.append(list(cmd))
            return SimpleNamespace(pid=1)
        return real_popen(cmd, *a, **k)

    monkeypatch.setattr(routes_api.subprocess, "Popen", fake_popen)
    real_run = routes_api.subprocess.run

    def fake_run(cmd, *a, **k):
        if cmd and cmd[0] == "launchctl":
            pytest.fail("launchctl must not be called in app mode")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(routes_api.subprocess, "run", fake_run)

    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    restarts: list[int] = []
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = False
    app.state.installer_run_id = None
    app.state.serializer = serializer
    app.state.request_restart = restarts.append

    resp = TestClient(app, base_url="http://localhost:8443", client=_LOOPBACK_PEER).post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(tmp_path / "workspace"),
            "app_dir": str(tmp_path / "Applications"),
        },
    )

    assert resp.status_code == 200
    assert handoff_calls == [], "app mode must not reach the launchd handoff seam"
    assert not (home / "Library" / "LaunchAgents" / "com.ciao.server.plist").is_file()
    assert popen_calls == [], "no detached launchd loader in app mode"
    assert restarts == [RESTART_EXIT_CODE]


def test_setup_finish_requires_a_password(tmp_path) -> None:
    """Password protection is the default, so the wizard cannot skip it: the
    bootstrap token it would otherwise inherit is machine-generated and unusable
    from a second device."""
    resp = _finish_client(tmp_path).post(
        "/api/setup/finish",
        json={
            "workspace": str(tmp_path / "workspace"),
            "launch_agents_dir": str(tmp_path / "LaunchAgents"),
            "app_dir": str(tmp_path / "Applications"),
            "restart": False,
        },
    )

    assert resp.status_code == 400
    assert "password" in resp.json()["error"]
    assert not (tmp_path / "workspace" / ".env").exists()


def test_setup_finish_rejects_a_too_short_password(tmp_path) -> None:
    resp = _finish_client(tmp_path).post(
        "/api/setup/finish",
        json={
            "workspace": str(tmp_path / "workspace"),
            "password": "ab",
            "launch_agents_dir": str(tmp_path / "LaunchAgents"),
            "app_dir": str(tmp_path / "Applications"),
            "restart": False,
        },
    )

    assert resp.status_code == 400
    assert "at least" in resp.json()["error"]


def test_setup_finish_requires_workspace(tmp_path) -> None:
    """The wizard's primary question is the workspace root: no folder, no finish."""
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = False
    app.state.installer_run_id = None
    app.state.serializer = serializer

    resp = TestClient(app, base_url="http://localhost:8443", client=_LOOPBACK_PEER).post(
        "/api/setup/finish",
        json={"vault_root": str(tmp_path / "notes")},
    )

    assert resp.status_code == 400
    assert resp.json()["error"] == "workspace is required"


def test_setup_finish_defaults_vault_inside_workspace(tmp_path) -> None:
    """Without an explicit vault_root the vault is created inside memory-vault/;
    setup does not initialize Git history without explicit consent."""
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = False
    app.state.installer_run_id = None
    app.state.serializer = serializer

    workspace = tmp_path / "workspace"
    resp = TestClient(app, base_url="http://localhost:8443", client=_LOOPBACK_PEER).post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(workspace),
            "launch_agents_dir": str(tmp_path / "LaunchAgents"),
            "app_dir": str(tmp_path / "Applications"),
            "restart": False,
        },
    )

    assert resp.status_code == 200
    assert resp.json()["workspace"] == str(workspace.resolve())
    env_text = (workspace / ".env").read_text(encoding="utf-8")
    assert "CIAO_VAULT_ROOT=memory-vault" in env_text
    assert (workspace / "personal" / "memory-vault" / "MEMORY.md").is_file()
    assert not (workspace / ".git").exists()
    assert not (workspace / "personal" / "memory-vault" / ".git").exists()


def test_setup_finish_existing_notes_does_not_create_initial_commit(tmp_path) -> None:
    from ciao import cli

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "ideas.md").write_text("private\n", encoding="utf-8")
    cli.setup_workspace(notes, vault_mode="existing", confirm_repoint=True)
    assert not (notes / ".git").exists()
    assert (notes / "ideas.md").read_text(encoding="utf-8") == "private\n"


def test_setup_finish_accepts_0000_host(tmp_path) -> None:
    """0.0.0.0 is an accepted local Host spelling: users copy it from the bind-address log."""
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = False
    app.state.installer_run_id = None
    app.state.serializer = serializer

    resp = TestClient(app, base_url="http://0.0.0.0:8443", client=_LOOPBACK_PEER).post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(tmp_path / "workspace"),
            "vault_root": str(tmp_path / "brain"),
            "launch_agents_dir": str(tmp_path / "LaunchAgents"),
            "app_dir": str(tmp_path / "Applications"),
            "restart": False,
        },
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_setup_finish_refuses_a_protected_folder_without_a_host(
    tmp_path, monkeypatch
) -> None:
    from ciao import cli

    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli, "_verified_host_if_installed", lambda: None)
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = False
    app.state.installer_run_id = None
    app.state.serializer = serializer
    workspace = home / "Documents" / "ws"

    resp = TestClient(app, base_url="http://localhost:8443", client=_LOOPBACK_PEER).post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(workspace),
            "launch_agents_dir": str(tmp_path / "LaunchAgents"),
            "app_dir": str(tmp_path / "Applications"),
            "restart": False,
        },
    )

    assert resp.status_code == 409
    assert "Ciaobot Server" in resp.json()["error"]
    assert not workspace.exists()


def test_setup_finish_requires_bootstrap_mode(tmp_path) -> None:
    config = _config(tmp_path)
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = False
    app.state.installer_run_id = None
    app.state.serializer = serializer

    resp = TestClient(app, base_url="http://localhost:8443", client=_LOOPBACK_PEER).post(
        "/api/setup/finish",
        json={"workspace": str(tmp_path / "workspace")},
        cookies={"ciao_session": serializer.dumps({"user": "owner"})},
    )

    assert resp.status_code == 409


def test_setup_finish_is_localhost_only(tmp_path) -> None:
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = False
    app.state.installer_run_id = None
    app.state.serializer = serializer

    resp = TestClient(app, base_url="https://ciao.example").post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(tmp_path / "workspace"),
        },
    )

    assert resp.status_code == 403
    # The refusal tells the user where to go instead.
    assert "open the wizard at http://localhost:8443" in resp.json()["error"]


def test_setup_routes_refuse_a_lan_peer_claiming_localhost(tmp_path) -> None:
    """First run binds 0.0.0.0: a LAN client can send `Host: localhost`, so
    the Host header alone must not make the setup routes reachable."""
    lan = ("192.168.1.20", 50000)
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = False
    app.state.installer_run_id = None
    app.state.serializer = serializer
    app.state.request_restart = lambda code: None

    finish = TestClient(app, base_url="http://localhost:8443", client=lan).post(
        "/api/setup/finish",
        json={"password": "wizard-pass", "workspace": str(tmp_path / "workspace")},
    )
    listing = _folder_picker_client(tmp_path, peer=lan).get(
        "/api/setup/list-dirs", params={"path": str(tmp_path)}
    )

    assert finish.status_code == 403
    assert not (tmp_path / "workspace" / ".env").exists()
    assert listing.status_code == 403


def _folder_picker_client(
    tmp_path,
    *,
    bootstrap: bool = True,
    base_url: str = "http://localhost:8443",
    peer: tuple[str, int] = _LOOPBACK_PEER,
) -> TestClient:
    if bootstrap:
        config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    else:
        config = _config(tmp_path)
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[
            Route("/api/setup/list-dirs", setup_list_dirs_endpoint, methods=["GET"]),
            Route("/api/setup/inspect-folder", setup_inspect_folder_endpoint, methods=["GET"]),
            Route("/api/setup/mkdir", setup_mkdir_endpoint, methods=["POST"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = False
    app.state.installer_run_id = None
    app.state.serializer = serializer
    return TestClient(app, base_url=base_url, client=peer)


def test_setup_list_dirs_requires_bootstrap_mode(tmp_path) -> None:
    client = _folder_picker_client(tmp_path, bootstrap=False)

    resp = client.get("/api/setup/list-dirs", params={"path": str(tmp_path)})

    assert resp.status_code == 404


def test_setup_list_dirs_is_localhost_only(tmp_path) -> None:
    client = _folder_picker_client(tmp_path, base_url="https://ciao.example")

    resp = client.get("/api/setup/list-dirs", params={"path": str(tmp_path)})

    assert resp.status_code == 403
    assert "open the wizard at http://localhost:8443" in resp.json()["error"]


def test_setup_list_dirs_lists_visible_directories_only(tmp_path) -> None:
    (tmp_path / "beta").mkdir()
    (tmp_path / "Alpha").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "notes.txt").write_text("nope", encoding="utf-8")
    client = _folder_picker_client(tmp_path)

    resp = client.get("/api/setup/list-dirs", params={"path": str(tmp_path)})

    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == str(tmp_path.resolve())
    assert body["parent"] == str(tmp_path.resolve().parent)
    assert [d["name"] for d in body["dirs"]] == ["Alpha", "beta", "boot"]
    assert body["dirs"][0]["path"] == str(tmp_path.resolve() / "Alpha")
    assert body["home"] == str(Path.home().resolve())


def test_setup_list_dirs_defaults_to_home_and_abbreviates_display_path(tmp_path, home_dir) -> None:
    client = _folder_picker_client(tmp_path)

    resp = client.get("/api/setup/list-dirs")

    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == str(Path.home().resolve())
    assert body["display_path"] == "~"


def test_setup_list_dirs_rejects_missing_or_file_path(tmp_path) -> None:
    (tmp_path / "notes.txt").write_text("nope", encoding="utf-8")
    client = _folder_picker_client(tmp_path)

    assert client.get("/api/setup/list-dirs", params={"path": str(tmp_path / "nope")}).status_code == 400
    assert client.get("/api/setup/list-dirs", params={"path": str(tmp_path / "notes.txt")}).status_code == 400


def test_setup_mkdir_creates_folder_and_returns_parent_listing(tmp_path) -> None:
    client = _folder_picker_client(tmp_path)

    resp = client.post("/api/setup/mkdir", json={"path": str(tmp_path), "name": "workspace"})

    assert resp.status_code == 200
    body = resp.json()
    assert (tmp_path / "workspace").is_dir()
    assert body["path"] == str(tmp_path.resolve())
    assert "workspace" in [d["name"] for d in body["dirs"]]


def test_setup_mkdir_rejects_invalid_names_and_paths(tmp_path) -> None:
    client = _folder_picker_client(tmp_path)

    for name in ["", "a/b", "a\\b", ".hidden", "../escape"]:
        resp = client.post("/api/setup/mkdir", json={"path": str(tmp_path), "name": name})
        assert resp.status_code == 400, name
    assert client.post("/api/setup/mkdir", json={"path": str(tmp_path / "nope"), "name": "ok"}).status_code == 400

    (tmp_path / "taken").mkdir()
    resp = client.post("/api/setup/mkdir", json={"path": str(tmp_path), "name": "taken"})
    assert resp.status_code == 400


def test_setup_mkdir_requires_bootstrap_mode(tmp_path) -> None:
    client = _folder_picker_client(tmp_path, bootstrap=False)

    resp = client.post("/api/setup/mkdir", json={"path": str(tmp_path), "name": "workspace"})

    assert resp.status_code == 404
    assert not (tmp_path / "workspace").exists()


def test_setup_inspect_folder_reachable_without_session_in_bootstrap(tmp_path) -> None:
    """The first-run wizard has no session yet; with password protection always
    on, the folder probe must be on the public list like the other setup
    filesystem routes, or the wizard never shows existing workspaces."""
    target = tmp_path / "notes"
    target.mkdir()
    client = _folder_picker_client(tmp_path)

    resp = client.get("/api/setup/inspect-folder", params={"path": str(target)})

    assert resp.status_code == 200
    assert "existing_workspaces" in resp.json()


def test_tcc_protected_location_flags_desktop(monkeypatch, tmp_path) -> None:
    """A workspace inside ~/Desktop|Documents|Downloads must be flagged on
    macOS (launchd agents get EPERM there) and cleared elsewhere."""
    from ciao import setup_status

    home = tmp_path / "home"
    (home / "Desktop" / "Cowork").mkdir(parents=True)
    (home / "projects" / "ws").mkdir(parents=True)
    monkeypatch.setattr(setup_status.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(setup_status.sys, "platform", "darwin")

    assert setup_status.tcc_protected_location(home / "Desktop" / "Cowork") == "Desktop"
    assert setup_status.tcc_protected_location(home / "projects" / "ws") is None

    # Non-macOS never flags (launchd/TCC is macOS-only).
    monkeypatch.setattr(setup_status.sys, "platform", "linux")
    assert setup_status.tcc_protected_location(home / "Desktop" / "Cowork") is None


async def test_provider_verify_action_busts_claude_discovery_cache(
    monkeypatch, tmp_path
) -> None:
    """Verify must not just echo the up-to-5-minute discovery cache.

    A stale skills list that never reflects a skill the user just added or
    removed would be worse than showing nothing, so hitting Verify for Claude
    has to bust the discovery cache before recomputing the payload. Other providers don't have this cache, so
    they should be left alone.
    """
    from ciao.web import routes_api

    calls = {"clear": 0, "payload": 0}

    def fake_clear() -> None:
        calls["clear"] += 1

    def fake_payload(_config):
        calls["payload"] += 1
        return {"connections": {"claude": {"skills": ["pdf"]}, "opencode": {}}}

    monkeypatch.setattr("ciao.setup_status.clear_claude_discovery_cache", fake_clear)
    monkeypatch.setattr(routes_api, "_provider_config_payload", fake_payload)
    monkeypatch.setattr("ciao.providers.opencode.opencode_credential_inventory",
                        lambda: {"status": "complete", "providers": []})

    config = _config(tmp_path)
    request = SimpleNamespace(
        path_params={"provider": "claude", "action": "verify"},
        app=SimpleNamespace(state=SimpleNamespace(config=config)),
    )

    response = await provider_connection_action(request)
    assert json.loads(response.body) == {"skills": ["pdf"]}
    assert calls == {"clear": 1, "payload": 1}

    request.path_params["provider"] = "opencode"
    await provider_connection_action(request)
    assert calls == {"clear": 1, "payload": 2}


async def test_opencode_verify_invalidates_models_before_probe(monkeypatch, tmp_path):
    from ciao.web import routes_api
    import ciao.providers.opencode as mod

    calls = []
    monkeypatch.setattr(mod, "invalidate_opencode_model_cache", lambda: calls.append("invalidate"))
    monkeypatch.setattr(mod, "opencode_credential_inventory",
                        lambda: (calls.append("inventory") or {"status": "complete", "providers": []}))
    monkeypatch.setattr(routes_api, "_provider_config_payload",
                        lambda config: (calls.append("payload") or
                                        {"connections": {"opencode": {}, "claude": {"ready": True}}}))
    monkeypatch.setattr("ciao.setup_status.clear_claude_discovery_cache", lambda: calls.append("claude-clear"))
    request = SimpleNamespace(path_params={"provider": "opencode", "action": "verify"},
                              app=SimpleNamespace(state=SimpleNamespace(config=_config(tmp_path))))
    await provider_connection_action(request)
    assert calls == ["invalidate", "payload", "inventory"]
    calls.clear()
    request.path_params["provider"] = "claude"
    response = await provider_connection_action(request)
    assert json.loads(response.body) == {"ready": True}
    assert calls == ["claude-clear", "payload"]
    calls.clear()
    await routes_api.provider_config_settings(request)
    assert calls == ["payload", "inventory"]
    calls.clear()
    monkeypatch.setattr(routes_api.host_terminal, "action_command", lambda action: "fake login")
    monkeypatch.setattr(routes_api, "host_browser_request", lambda request: False)
    request.path_params.update(provider="opencode", action="connect")
    await provider_connection_action(request)
    assert calls == []
    monkeypatch.setattr(routes_api, "_auth_command_for_provider", lambda provider: ["fake", "auth", "login"])
    monkeypatch.setattr(routes_api.subprocess, "run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""))
    request.path_params["action"] = "logout"
    await provider_connection_action(request)
    assert calls == []


async def test_provider_logout_action_runs_auth_logout(monkeypatch, tmp_path) -> None:
    """Logout must derive `auth logout` from the login command, not `claude logout`.

    The top-level `claude logout` subcommand no longer exists: the CLI prints a
    "did you mean" hint and exits 0, so `[:1] + ["logout"]` returned ok without
    signing anyone out.
    """
    from ciao.web import routes_api

    captured: dict[str, object] = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(routes_api.subprocess, "run", fake_run)
    monkeypatch.setattr(routes_api.asyncio, "to_thread", fake_to_thread)
    # The derivation is under test, not where this machine keeps `claude`.
    from ciao.providers import claude as claude_provider

    monkeypatch.setattr(claude_provider, "get_bundled_claude_path", lambda: str(tmp_path / "claude"))

    config = _config(tmp_path)
    request = SimpleNamespace(
        path_params={"provider": "claude", "action": "logout"},
        app=SimpleNamespace(state=SimpleNamespace(config=config)),
    )

    response = await provider_connection_action(request)

    assert json.loads(response.body) == {"ok": True}
    assert captured["cmd"][1:] == ["auth", "logout"]


def test_discover_claude_system_skills_filters_enabled_and_caches(
    monkeypatch, tmp_path
) -> None:
    from ciao import setup_status

    setup_status.clear_claude_discovery_cache()
    calls = {"n": 0}

    class FakeResult:
        stdout = (
            "❯ skill-creator@claude-plugins-official\n"
            "  Status: ✔ enabled\n"
            "❯ other-plugin@source\n"
            "  Status: ✘ disabled\n"
        )
        stderr = ""
        returncode = 0

    def fake_run(*_args, **_kwargs):
        calls["n"] += 1
        return FakeResult()

    monkeypatch.setattr(setup_status.shutil, "which", lambda _name: "/bin/claude")
    monkeypatch.setattr(setup_status.subprocess, "run", fake_run)
    monkeypatch.setattr(
        setup_status, "_claude_standalone_skills_dir", lambda: tmp_path / "absent"
    )

    assert setup_status.discover_claude_system_skills() == ["skill-creator"]
    assert setup_status.discover_claude_system_skills() == ["skill-creator"]
    assert calls["n"] == 1


def test_discover_claude_system_skills_merges_standalone_skills(
    monkeypatch, tmp_path
) -> None:
    """Standalone ~/.claude/skills entries show up alongside enabled plugins.

    `claude plugin list` only knows about plugins, so a user with a hand-installed
    skill directory would otherwise never see it on the Models & providers tab.
    """
    from ciao import setup_status

    setup_status.clear_claude_discovery_cache()

    class FakeResult:
        stdout = "❯ skill-creator@claude-plugins-official\n  Status: ✔ enabled\n"
        stderr = ""
        returncode = 0

    monkeypatch.setattr(setup_status.shutil, "which", lambda _name: "/bin/claude")
    monkeypatch.setattr(
        setup_status.subprocess, "run", lambda *_a, **_k: FakeResult()
    )
    skills_dir = tmp_path / "skills"
    (skills_dir / "ego-browser").mkdir(parents=True)
    (skills_dir / "note-taking.md").write_text("---\n---\n", encoding="utf-8")
    (skills_dir / "ignored.txt").write_text("not a skill", encoding="utf-8")
    monkeypatch.setattr(
        setup_status, "_claude_standalone_skills_dir", lambda: skills_dir
    )

    assert setup_status.discover_claude_system_skills() == [
        "ego-browser",
        "note-taking",
        "skill-creator",
    ]


def test_discover_claude_system_skills_lists_synced_skills_and_loaded_plugins(
    monkeypatch, tmp_path
) -> None:
    """claude.ai-synced skills sit under synced/<bucket>/, and synced plugins
    report `loaded` rather than `enabled`. The folder is not a skill."""
    from ciao import setup_status

    setup_status.clear_claude_discovery_cache()

    class FakeResult:
        stdout = (
            "❯ skill-creator@claude-plugins-official\n"
            "  Status: ✔ enabled\n"
            "❯ telegram@claude-plugins-official\n"
            "  Status: ✘ disabled\n"
            "Synced from claude.ai:\n"
            "❯ cowork-plugin-management@synced\n"
            "  Path: /x\n"
            "  Status: ✔ loaded\n"
        )
        stderr = ""
        returncode = 0

    monkeypatch.setattr(setup_status.shutil, "which", lambda _name: "/bin/claude")
    monkeypatch.setattr(
        setup_status.subprocess, "run", lambda *_a, **_k: FakeResult()
    )
    skills_dir = tmp_path / "skills"
    (skills_dir / "impeccable").mkdir(parents=True)
    bucket = skills_dir / "synced" / "org_user"
    for name in ("docx", "pdf"):
        (bucket / name).mkdir(parents=True)
        (bucket / name / "SKILL.md").write_text("---\n---\n", encoding="utf-8")
    (bucket / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        setup_status, "_claude_standalone_skills_dir", lambda: skills_dir
    )

    assert setup_status.discover_claude_system_skills() == [
        "cowork-plugin-management",
        "docx",
        "impeccable",
        "pdf",
        "skill-creator",
    ]


def test_claude_probe_reports_bundled_skills_once_a_chat_recorded_them(
    monkeypatch, tmp_path
) -> None:
    from ciao import setup_status

    monkeypatch.setattr(setup_status, "_claude_bundled_skills", None)
    monkeypatch.setattr(
        setup_status, "_claude_status", lambda *_a, **_k: {"name": "claude", "ok": True}
    )
    probe = lambda: setup_status.claude_status_probe({}, config_path=tmp_path / "c.yaml")  # noqa: E731

    assert "bundled_skills" not in probe()

    setup_status.record_claude_bundled_skills(["simplify", "code-review", " "])

    assert probe()["bundled_skills"] == ["code-review", "simplify"]


def test_discover_claude_system_skills_falls_back_to_installed_plugins(
    monkeypatch, tmp_path
) -> None:
    """With no CLI and no standalone skills, installed plugins still surface."""
    from ciao import setup_status

    setup_status.clear_claude_discovery_cache()
    monkeypatch.setattr(setup_status.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        setup_status, "_claude_standalone_skills_dir", lambda: tmp_path / "absent"
    )

    plugins_dir = tmp_path / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"legacy-plugin@source": {}}}), encoding="utf-8"
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    assert setup_status.discover_claude_system_skills() == ["legacy-plugin"]


def test_discover_claude_system_skills_falls_back_when_cli_fails(
    monkeypatch, tmp_path
) -> None:
    """A failed `claude plugin list` must not hide installed plugins.

    Fallback eligibility tracks CLI success, not the merged result: otherwise
    one standalone skill would suppress the installed_plugins recovery whenever
    the CLI transiently fails or times out.
    """
    import subprocess as _subprocess

    from ciao import setup_status

    setup_status.clear_claude_discovery_cache()

    def boom(*_args, **_kwargs):
        raise _subprocess.TimeoutExpired(cmd="claude plugin list", timeout=8)

    monkeypatch.setattr(setup_status.shutil, "which", lambda _name: "/bin/claude")
    monkeypatch.setattr(setup_status.subprocess, "run", boom)
    skills_dir = tmp_path / "skills"
    (skills_dir / "ego-browser").mkdir(parents=True)
    monkeypatch.setattr(
        setup_status, "_claude_standalone_skills_dir", lambda: skills_dir
    )
    plugins_dir = tmp_path / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"legacy-plugin@source": {}}}), encoding="utf-8"
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    assert setup_status.discover_claude_system_skills() == [
        "ego-browser",
        "legacy-plugin",
    ]


# ── Claude discovery warm-up ───────────────────────────────────────────


def test_warm_claude_discovery_cache_populates_the_skills_cache_only(monkeypatch):
    import time

    from ciao import setup_status as ss

    monkeypatch.setattr(ss, "_claude_skills_cache", None)
    seen = []

    def fake_skills():
        seen.append("skills")
        return ["skill-creator"]

    monkeypatch.setattr(ss, "_discover_claude_system_skills_uncached", fake_skills)
    runs: list[object] = []
    monkeypatch.setattr(ss.subprocess, "run", lambda *a, **k: runs.append(a))

    ss.warm_claude_discovery_cache()

    deadline = time.monotonic() + 5
    while ss._claude_skills_cache is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == ["skills"]
    assert runs == [], "the warm-up must run no CLI probe"
    assert ss._claude_skills_cache is not None
    assert ss._claude_skills_cache[1] == ("skill-creator",)


def test_status_does_not_run_claude_mcp_list(monkeypatch, tmp_path) -> None:
    """Building the provider status never shells out to `claude mcp ...`."""
    from ciao import setup_status as ss

    ss.clear_claude_discovery_cache()
    argvs: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        argvs.append(list(cmd))
        return SimpleNamespace(stdout="{}", stderr="", returncode=0)

    monkeypatch.setattr(ss, "claude_cli_path", lambda: "/bin/claude")
    monkeypatch.setattr(ss.subprocess, "run", fake_run)
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(ss, "_discover_claude_system_skills_uncached", lambda: [])

    data = ss.setup_status(_config(tmp_path), env={}, claude_config_path=tmp_path / ".claude.json")

    assert argvs, "the probe must still run the CLI for version and auth"
    assert not any("mcp" in argv for argv in argvs)
    assert "mcps" not in data["providers"]["claude"]


def test_a_registry_marks_a_set_up_workspace(tmp_path: Path) -> None:
    from ciao.setup_marker import is_set_up_workspace

    assert not is_set_up_workspace(tmp_path)
    (tmp_path / ".runtime").mkdir()
    (tmp_path / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")
    assert is_set_up_workspace(tmp_path)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('CIAO_WORKSPACES=[{"name":"personal"}]', True),
        ("CIAO_WORKSPACES='[{\"name\":\"personal\"}]'", True),
        ("CIAO_WORKSPACES=", False),
        ('CIAO_WORKSPACES=""', False),
        ('# CIAO_WORKSPACES=[{"name":"personal"}]', False),
        ("PWA_PORT=8443", False),
    ],
)
def test_a_pre_1_0_workspaces_env_marks_a_set_up_workspace(
    tmp_path: Path, line: str, expected: bool
) -> None:
    """Pre-1.0 path: no registry until the first start on a release that
    imports CIAO_WORKSPACES, so the `.env` variable still counts."""
    from ciao.setup_marker import is_set_up_workspace

    (tmp_path / ".env").write_text(line + "\n", encoding="utf-8")
    assert is_set_up_workspace(tmp_path) is expected


def test_provider_rows_carry_their_terminal_action(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: "")
    monkeypatch.setattr("ciao.setup_status.claude_app_path", lambda: "")
    monkeypatch.setattr("ciao.tool_path.resolve_on_terminal_path", lambda cmd: None)

    claude = setup_status(_config(tmp_path), env={})["providers"]["claude"]

    assert claude["auth"] == "not_installed"
    assert claude["terminal_action"] == "claude-install"


def test_cloud_synced_location_recognises_sync_folders(tmp_path) -> None:
    from ciao.setup_status import cloud_synced_location

    home = tmp_path / "h"
    mac = home / "Library" / "CloudStorage"
    cases = [
        ("darwin", home / "Library/Mobile Documents/com~apple~CloudDocs/ws", "iCloud Drive"),
        ("darwin", mac / "GoogleDrive-me@x.com/My Drive/ws", "Google Drive"),
        ("darwin", mac / "Dropbox/ws", "Dropbox"),
        ("darwin", mac / "OneDrive-Personal/ws", "OneDrive"),
        ("darwin", mac / "Nextcloud/ws", "a cloud-synced folder"),
        ("darwin", home / "Dropbox (Acme)/ws", "Dropbox"),
        ("darwin", home / "OneDrive - Contoso/ws", "OneDrive"),
        ("darwin", home / "dropbox" / "ws", "Dropbox"),
        ("darwin", home / "Ciaobot", None),
        ("darwin", home, None),
        ("darwin", mac, None),
        ("darwin", home / "Library/Application Support/x", None),
        ("linux", home / "Dropbox/ws", "Dropbox"),
        ("linux", home / "Library/Mobile Documents/x", None),
    ]
    for platform, path, expected in cases:
        assert cloud_synced_location(path, platform=platform, home=home) == expected, path


def test_cloud_synced_location_follows_symlinks(tmp_path) -> None:
    from ciao.setup_status import cloud_synced_location

    home = tmp_path / "h"
    real = home / "Library" / "CloudStorage" / "Dropbox"
    (real / "ws").mkdir(parents=True)
    (home / "Dropbox").symlink_to(real)
    assert cloud_synced_location(home / "Dropbox" / "ws", platform="darwin", home=home) == "Dropbox"


def test_cloud_sync_warning_names_the_service_and_is_empty_otherwise(tmp_path) -> None:
    from ciao.setup_status import cloud_sync_warning

    home = tmp_path / "h"
    path = home / "Dropbox" / "ws"
    warning = cloud_sync_warning(path, platform="darwin", home=home)
    assert "Dropbox" in warning
    assert str(path) in warning
    assert cloud_sync_warning(home / "Ciaobot", platform="darwin", home=home) == ""


def test_setup_inspect_folder_reports_cloud_sync_warning(tmp_path, monkeypatch) -> None:
    target = tmp_path / "notes"
    target.mkdir()
    client = _folder_picker_client(tmp_path)

    resp = client.get("/api/setup/inspect-folder", params={"path": str(target)})
    assert resp.json()["cloud_sync_warning"] == ""

    monkeypatch.setattr("ciao.setup_status.cloud_sync_warning", lambda path, **kw: "SYNCED")
    resp = client.get("/api/setup/inspect-folder", params={"path": str(target)})
    assert resp.json()["cloud_sync_warning"] == "SYNCED"


def test_macos_access_note_names_the_folder(monkeypatch, tmp_path) -> None:
    from ciao import setup_status

    home = tmp_path / "home"
    (home / "Documents" / "ws").mkdir(parents=True)
    (home / "projects" / "ws").mkdir(parents=True)
    monkeypatch.setattr(setup_status.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(setup_status.sys, "platform", "darwin")

    note = setup_status.macos_access_note(home / "Documents" / "ws")
    assert "Documents" in note and "Ciaobot Server" in note
    assert setup_status.macos_access_note(home / "projects" / "ws") == ""

    monkeypatch.setattr(setup_status.sys, "platform", "linux")
    assert setup_status.macos_access_note(home / "Documents" / "ws") == ""


def test_cloud_synced_location_detects_icloud_desktop_and_documents(tmp_path) -> None:
    from ciao.setup_status import cloud_synced_location

    home = tmp_path / "h"
    docs = home / "Documents" / "ws"
    desk = home / "Desktop" / "ws"
    label = "iCloud Drive (Desktop & Documents)"

    def where(path, prefs, platform="darwin"):
        return cloud_synced_location(path, platform=platform, home=home, finder_prefs=prefs)

    assert where(docs, {"FXICloudDriveDocuments": 1}) == label
    assert where(desk, {"FXICloudDriveDocuments": 1}) is None
    assert where(desk, {"FXICloudDriveDesktop": True}) == label
    assert where(docs, {"FXICloudDriveDesktop": True}) is None
    assert where(docs, {}) is None and where(desk, {}) is None
    assert where(docs, {"FXICloudDriveDocuments": 0}) is None
    assert where(docs, {"FXICloudDriveDocuments": 1}, platform="linux") is None


def test_finder_prefs_reads_the_plist_and_tolerates_garbage(tmp_path) -> None:
    import plistlib

    from ciao.setup_status import cloud_synced_location

    home = tmp_path / "h"
    prefs = home / "Library" / "Preferences" / "com.apple.finder.plist"
    prefs.parent.mkdir(parents=True)
    with prefs.open("wb") as handle:
        plistlib.dump({"FXICloudDriveDocuments": True}, handle, fmt=plistlib.FMT_BINARY)
    docs = home / "Documents" / "ws"
    assert (
        cloud_synced_location(docs, platform="darwin", home=home)
        == "iCloud Drive (Desktop & Documents)"
    )

    prefs.write_bytes(b"not a plist at all")
    assert cloud_synced_location(docs, platform="darwin", home=home) is None
    prefs.write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>FXICl'
    )
    assert cloud_synced_location(docs, platform="darwin", home=home) is None
    prefs.unlink()
    assert cloud_synced_location(docs, platform="darwin", home=home) is None


def test_setup_inspect_folder_reports_macos_access_note(tmp_path, monkeypatch) -> None:
    target = tmp_path / "notes"
    target.mkdir()
    client = _folder_picker_client(tmp_path)

    resp = client.get("/api/setup/inspect-folder", params={"path": str(target)})
    assert resp.json()["macos_access_note"] == ""

    monkeypatch.setattr("ciao.setup_status.macos_access_note", lambda path: "ALLOW IT")
    # No verified Ciaobot Server on this Mac: the folder is refused later, so
    # promising a prompt would mislead.
    monkeypatch.setattr("ciao.cli._verified_host_if_installed", lambda: None)
    resp = client.get("/api/setup/inspect-folder", params={"path": str(target)})
    assert resp.json()["macos_access_note"] == ""

    monkeypatch.setattr("ciao.cli._verified_host_if_installed", lambda: object())
    resp = client.get("/api/setup/inspect-folder", params={"path": str(target)})
    assert resp.json()["macos_access_note"] == "ALLOW IT"


def test_setup_warns_when_the_workspace_is_in_a_synced_folder(monkeypatch, capsys) -> None:
    from ciao import cli

    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.delenv("CIAO_ENGINE_PATH", raising=False)
    synced = Path.home() / "Dropbox" / "ws"
    assert cli.main(["setup", "--workspace", str(synced)]) == 0
    err = capsys.readouterr().err
    assert "Warning:" in err and "Dropbox" in err

    assert cli.main(["setup", "--workspace", str(Path.home() / "plain")]) == 0
    assert "Dropbox" not in capsys.readouterr().err


def _tested_record(running: str) -> dict:
    return {
        "schema": 1,
        "ciaobot": running,
        "recorded": "2026-10-20",
        "providers": {
            "opencode": {"status": "tested", "version": "2.1.0", "package": "@opencode/cli"},
            "claude": {"status": "tested", "version": "2.1.301", "source": "claude-agent-sdk 0.2.168"},
        },
    }


def test_claude_row_marks_bundled_release_check(tmp_path, monkeypatch) -> None:
    import ciao
    from ciao import provider_versions

    bundled = "/opt/sdk/claude_agent_sdk/_bundled/claude"
    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: bundled)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: bundled)
    monkeypatch.setattr(provider_versions, "load_record", lambda: _tested_record(ciao.__version__))
    config = _config(tmp_path)

    claude = setup_status(config, env={})["providers"]["claude"]

    assert claude["release_check"] == {"state": "bundled", "tested": "2.1.301"}


def test_opencode_unsupported_row_keeps_v2_migration_text(monkeypatch) -> None:
    import ciao
    from ciao import provider_versions
    from ciao.providers import opencode

    monkeypatch.setattr(opencode, "resolve_opencode_binary", lambda *a, **k: "/bin/opencode")
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: SimpleNamespace(stdout="1.14.0\n", returncode=0)
    )
    monkeypatch.setattr(provider_versions, "load_record", lambda: _tested_record(ciao.__version__))
    monkeypatch.setattr("ciao.tool_path.resolve_on_terminal_path", lambda cmd: None)

    row = opencode.opencode_login_status(inventory=False)

    assert row["auth"] == "unsupported_version"
    assert row["detail"] == opencode.OPENCODE_V2_REQUIRED
    assert row["terminal_action"] == "opencode-install"
    assert row["release_check"] == {"state": "unsupported", "tested": "2.1.0"}


def test_opencode_ok_row_carries_a_below_tested_release_check(monkeypatch) -> None:
    import ciao
    from ciao import provider_versions
    from ciao.providers import opencode

    monkeypatch.setattr(opencode, "resolve_opencode_binary", lambda *a, **k: "/bin/opencode")
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: SimpleNamespace(stdout="opencode v2.0.32\n", returncode=0)
    )
    monkeypatch.setattr(opencode, "_credential_count", lambda *a, **k: 1)
    monkeypatch.setattr(provider_versions, "load_record", lambda: _tested_record(ciao.__version__))

    row = opencode.opencode_login_status(inventory=False)

    assert row["release_check"]["state"] == "below_tested"
    assert row["release_check"]["terminal_action"] == "opencode-update"
    assert row["release_check"]["command"].endswith("upgrade 2.1.0")


def test_clearing_the_discovery_cache_rereads_a_system_cli_version(monkeypatch) -> None:
    from ciao import setup_status as module

    calls: list[str] = []

    def fake_run(argv, **_k):
        calls.append(argv[0])
        return SimpleNamespace(stdout="2.0.0\n", stderr="", returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    probe = module._CLI_VERSION_CACHE
    probe.cache_clear()
    try:
        probe("/x/claude")
        probe("/x/claude")
        assert len(calls) == 1
        module.clear_claude_discovery_cache()
        probe("/x/claude")
        assert len(calls) == 2
    finally:
        probe.cache_clear()


def test_setup_status_reports_the_installer_folder_suggestion(tmp_path) -> None:
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        bootstrap_mode=True,
        suggested_workspace=tmp_path / "notes",
    )

    assert setup_status(config, env={})["suggested_workspace"] == str(tmp_path / "notes")


def test_setup_status_has_no_suggestion_on_a_configured_install(tmp_path) -> None:
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})

    assert setup_status(config, env={})["suggested_workspace"] is None


def _setup_api_client(tmp_path) -> TestClient:
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[
            Route("/api/setup/list-dirs", setup_list_dirs_endpoint, methods=["GET"]),
            Route("/api/setup/inspect-folder", setup_inspect_folder_endpoint, methods=["GET"]),
            Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.installer_bootstrap = False
    app.state.installer_run_id = None
    app.state.serializer = serializer
    app.state.request_restart = lambda code: None
    return TestClient(app, base_url="http://localhost:8443", client=_LOOPBACK_PEER)


def _folder_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


_ESTABLISHED_CASES = ["env_and_dot_registry", "registry_only", "legacy_only"]


@pytest.mark.parametrize("case", _ESTABLISHED_CASES)
def test_established_inspection_and_finish_agree(
    tmp_path, monkeypatch, case: str
) -> None:
    monkeypatch.setenv("CIAO_WORKSPACE", "")
    monkeypatch.setenv("PWA_PORT", "")
    monkeypatch.setenv("PWA_AUTH_TOKEN", "")
    ws = tmp_path / "workspace"
    for name in ("work", "personal"):
        vault = ws / name / "memory-vault"
        vault.mkdir(parents=True)
        (vault / "MEMORY.md").write_text(f"# {name}\n", encoding="utf-8")
    runtime = ws / ".runtime"
    runtime.mkdir()
    if case == "env_and_dot_registry":
        (ws / ".env").write_text("CIAO_VAULT_ROOT=.\n", encoding="utf-8")
        (runtime / "workspaces.json").write_text(
            json.dumps([{"name": "personal", "vault_root": "."}]), encoding="utf-8"
        )
    elif case == "registry_only":
        (runtime / "workspaces.json").write_text(
            json.dumps([
                {"name": "personal", "vault_root": "personal/memory-vault"},
                {"name": "work", "vault_root": "work/memory-vault"},
            ]),
            encoding="utf-8",
        )
    else:
        (ws / ".env").write_text(
            "CIAO_WORKSPACES='[{\"name\": \"research\", \"vault_root\": \"memory-vault/research\"}]'\n",
            encoding="utf-8",
        )
    client = _setup_api_client(tmp_path)
    before = _folder_bytes(ws)

    inspect = client.get("/api/setup/inspect-folder", params={"path": str(ws)})

    assert inspect.status_code == 200, inspect.text
    inspected = inspect.json()
    assert _folder_bytes(ws) == before, "inspection must not write"
    expected_names = {
        "env_and_dot_registry": ["personal"],
        "registry_only": ["personal", "work"],
        "legacy_only": ["research"],
    }[case]
    assert sorted(inspected["existing_workspaces"]) == expected_names

    finish = client.post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(ws),
            "launch_agents_dir": str(tmp_path / "LaunchAgents"),
            "app_dir": str(tmp_path / "Applications"),
            "python": _fake_engine_python(tmp_path),
            "port": 9443,
            "restart": False,
        },
    )

    assert finish.status_code == 200, finish.text
    registry = json.loads((runtime / "workspaces.json").read_text(encoding="utf-8"))
    assert sorted(entry["name"] for entry in registry) == expected_names
    if case == "env_and_dot_registry":
        # Inspection and finish both select the operational root for ".".
        assert Path(inspected["vault_root"]).resolve() == ws.resolve()
        # A configured "." stays "." and is never reinterpreted.
        assert registry[0]["vault_root"] == "."
        assert (ws / ".env").read_text(encoding="utf-8").count("CIAO_VAULT_ROOT=.") == 1


def _nested_vault(ws: Path, name: str) -> Path:
    vault = ws / name / "memory-vault"
    vault.mkdir(parents=True)
    (vault / "entity-types.yaml").write_text(
        "- id: client\n  label: Client\n  kind: entity\n  folder: Clients\n",
        encoding="utf-8",
    )
    (vault / "MEMORY.md").write_text(f"# {name}\n", encoding="utf-8")
    return vault


def test_setup_inspection_and_finish_select_same_nested_vault(
    tmp_path, monkeypatch
) -> None:
    from ciao.config import reset_reroot_cache

    monkeypatch.setenv("CIAO_WORKSPACE", "")
    monkeypatch.setenv("PWA_PORT", "")
    monkeypatch.setenv("PWA_AUTH_TOKEN", "")
    ws = (tmp_path / "workspace").resolve()
    vault = _nested_vault(ws, "work")
    client = _setup_api_client(tmp_path)

    inspect = client.get("/api/setup/inspect-folder", params={"path": str(ws)})
    assert inspect.status_code == 200, inspect.text
    assert Path(inspect.json()["vault_root"]) == vault

    finish = client.post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(ws),
            "launch_agents_dir": str(tmp_path / "LaunchAgents"),
            "app_dir": str(tmp_path / "Applications"),
            "python": _fake_engine_python(tmp_path),
            "port": 9443,
            "restart": False,
        },
    )
    assert finish.status_code == 200, finish.text

    assert "CIAO_VAULT_ROOT=work/memory-vault" in (ws / ".env").read_text(
        encoding="utf-8"
    ).splitlines()
    registry = json.loads((ws / ".runtime" / "workspaces.json").read_text(encoding="utf-8"))
    assert registry[0]["vault_root"] == "work/memory-vault"
    reset_reroot_cache()
    config = CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "test-token",
        "CIAO_WORKSPACE": str(ws),
        "CIAO_RUNTIME_ROOT": str(ws / ".runtime"),
        "CIAO_OLLAMA_LOCAL_DISCOVERY": "0",
    })
    assert config.workspace_vault_root("personal") == vault


def test_setup_ambiguous_non_native_finish_returns_400_without_writes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CIAO_WORKSPACE", "")
    monkeypatch.setenv("PWA_PORT", "")
    monkeypatch.setenv("PWA_AUTH_TOKEN", "")
    ws = (tmp_path / "workspace").resolve()
    _nested_vault(ws, "work")
    _nested_vault(ws, "personal")
    client = _setup_api_client(tmp_path)
    restarts: list[int] = []
    client.app.state.request_restart = restarts.append
    before = _folder_bytes(ws)

    inspect = client.get("/api/setup/inspect-folder", params={"path": str(ws)})
    finish = client.post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(ws),
            "launch_agents_dir": str(tmp_path / "LaunchAgents"),
            "app_dir": str(tmp_path / "Applications"),
            "python": _fake_engine_python(tmp_path),
            "port": 9443,
        },
    )

    assert inspect.status_code == 400
    assert finish.status_code == 400
    assert finish.json()["error"] == inspect.json()["error"]
    message = finish.json()["error"]
    assert message.index("personal/memory-vault") < message.index("work/memory-vault")
    assert _folder_bytes(ws) == before
    assert restarts == []
    assert os.environ["CIAO_WORKSPACE"] == ""
    assert os.environ["PWA_PORT"] == ""
    assert not (tmp_path / "LaunchAgents").exists()


def test_ambiguous_parent_can_be_retried_as_selected_nested_vault(
    tmp_path, monkeypatch
) -> None:
    from ciao.config import reset_reroot_cache

    monkeypatch.setenv("CIAO_WORKSPACE", "")
    monkeypatch.setenv("PWA_PORT", "")
    monkeypatch.setenv("PWA_AUTH_TOKEN", "")
    ws = (tmp_path / "workspace").resolve()
    work_vault = _nested_vault(ws, "work")
    _nested_vault(ws, "personal")
    client = _setup_api_client(tmp_path)

    listing = client.get("/api/setup/list-dirs", params={"path": str(ws)})
    assert listing.status_code == 200, listing.text
    assert "work" in listing.text
    refused = client.get("/api/setup/inspect-folder", params={"path": str(ws)})
    assert refused.status_code == 400
    assert "work/memory-vault" in refused.json()["error"]

    # The wizard's path field is editable: navigate into the chosen nested
    # vault and inspect that folder as the main Ciaobot folder.
    navigated = client.get("/api/setup/list-dirs", params={"path": str(work_vault)})
    assert navigated.status_code == 200, navigated.text
    reinspected = client.get("/api/setup/inspect-folder", params={"path": str(work_vault)})
    assert reinspected.status_code == 200, reinspected.text
    assert Path(reinspected.json()["vault_root"]) == work_vault

    finish = client.post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(work_vault),
            "launch_agents_dir": str(tmp_path / "LaunchAgents"),
            "app_dir": str(tmp_path / "Applications"),
            "python": _fake_engine_python(tmp_path),
            "port": 9443,
            "restart": False,
        },
    )
    assert finish.status_code == 200, finish.text

    # The selected folder is its own vault; the parent gained no defaults.
    assert "CIAO_VAULT_ROOT=." in (work_vault / ".env").read_text(
        encoding="utf-8"
    ).splitlines()
    assert not (ws / "memory-vault").exists()
    reset_reroot_cache()
    config = CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "test-token",
        "CIAO_WORKSPACE": str(work_vault),
        "CIAO_RUNTIME_ROOT": str(work_vault / ".runtime"),
        "CIAO_OLLAMA_LOCAL_DISCOVERY": "0",
    })
    assert config.workspace_vault_root("personal") == work_vault


def test_completed_legacy_import_inspection_does_not_list_removed_workspaces(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CIAO_WORKSPACE", "")
    monkeypatch.setenv("PWA_PORT", "")
    monkeypatch.setenv("PWA_AUTH_TOKEN", "")
    ws = (tmp_path / "workspace").resolve()
    (ws / ".runtime").mkdir(parents=True)
    (ws / ".runtime" / "workspaces-env-imported.json").write_text(
        json.dumps({"imported_at": "2026-01-01T00:00:00+00:00", "imported": ["research"]}),
        encoding="utf-8",
    )
    (ws / ".env").write_text(
        "CIAO_WORKSPACES='[{\"name\": \"research\", \"vault_root\": \"memory-vault/research\"}]'\n",
        encoding="utf-8",
    )
    client = _setup_api_client(tmp_path)

    inspect = client.get("/api/setup/inspect-folder", params={"path": str(ws)})

    assert inspect.status_code == 200, inspect.text
    assert "research" not in inspect.json()["existing_workspaces"]


def test_claude_mcp_command_uses_bare_name_on_terminal_path(monkeypatch) -> None:
    from ciao.setup_status import claude_mcp_command

    monkeypatch.setattr("ciao.tool_path.resolve_on_terminal_path", lambda cmd: "/usr/bin/claude")

    assert claude_mcp_command("/x y/claude") == "claude mcp"


def test_claude_mcp_command_quotes_the_bundled_binary(monkeypatch) -> None:
    from ciao.setup_status import claude_mcp_command

    monkeypatch.setattr("ciao.tool_path.resolve_on_terminal_path", lambda cmd: None)
    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: "/x y/claude")

    assert claude_mcp_command() == "'/x y/claude' mcp"


def test_installed_claude_row_carries_mcp_command(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("ciao.tool_path.resolve_on_terminal_path", lambda cmd: None)

    # Signed out (the autouse fixture) and signed in are both installed rows.
    signed_out = setup_status(_config(tmp_path), env={})["providers"]["claude"]
    _sign_in_claude(monkeypatch)
    signed_in = setup_status(_config(tmp_path), env={})["providers"]["claude"]

    assert signed_out["mcp_command"] == "/usr/local/bin/claude mcp"
    assert signed_in["mcp_command"] == "/usr/local/bin/claude mcp"


def test_missing_claude_row_has_no_mcp_command(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: "")
    monkeypatch.setattr("ciao.setup_status.claude_app_path", lambda: "")
    monkeypatch.setattr("ciao.tool_path.resolve_on_terminal_path", lambda cmd: None)

    claude = setup_status(_config(tmp_path), env={})["providers"]["claude"]

    assert claude["auth"] == "not_installed"
    assert "mcp_command" not in claude


def test_claude_cli_without_the_acp_adapter_is_not_ready(monkeypatch) -> None:
    monkeypatch.setattr("ciao.setup_status.claude_adapter_path", lambda: "")

    row = claude_status_probe({}, config_path=Path("/nonexistent"))

    assert row["ok"] is False
    assert row["detail"] == (
        "Claude Code is installed, but the ACP adapter (claude-agent-acp) is not. "
        "Use Install in Settings → Providers."
    )
    assert row["command"] == "npm install -g @agentclientprotocol/claude-agent-acp@0.108.1"
    assert row["terminal_action"] == "claude-install"


_FAKE_CODEX = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "codex-cli 0.166.0"; exit 0; fi
if [ "$1" = "login" ] && [ "$2" = "status" ]; then
  echo "{printed}"
  echo "{printed}" >&2
  exit {code}
fi
exit 2
"""


from ciao.setup_status import codex_status_probe  # noqa: E402


def _fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, code: int, printed: str = "ok") -> None:
    script = tmp_path / "codex"
    script.write_text(_FAKE_CODEX.format(code=code, printed=printed), encoding="utf-8")
    script.chmod(0o755)
    from ciao.providers.acp import agents

    monkeypatch.setattr(agents, "codex_binary", lambda: str(script))
    monkeypatch.setattr(agents, "codex_acp_binary", lambda: "/fake/codex-acp")


@pytest.mark.skipif(sys.platform == "win32", reason="fake CLI is a POSIX shell script")
def test_codex_status_probe_missing_adapter_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_codex(tmp_path, monkeypatch, code=0)
    from ciao.providers.acp import agents

    monkeypatch.setattr(agents, "codex_acp_binary", lambda: None)
    row = codex_status_probe({})
    assert row["auth"] == "not_installed"
    assert row["ok"] is False
    assert row["terminal_action"] == "codex-install"
    from ciao import host_terminal

    assert row["command"] == host_terminal.CODEX_INSTALL_COMMAND


@pytest.mark.skipif(sys.platform == "win32", reason="fake CLI is a POSIX shell script")
def test_codex_status_probe_logged_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_codex(tmp_path, monkeypatch, code=1)
    row = codex_status_probe({})
    assert row["auth"] == "logged_out"
    assert row["ok"] is False
    assert row["version"] == "0.166.0"
    assert row["terminal_action"] == "codex-login"


@pytest.mark.skipif(sys.platform == "win32", reason="fake CLI is a POSIX shell script")
def test_codex_status_probe_signed_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_codex(tmp_path, monkeypatch, code=0)
    row = codex_status_probe({})
    assert row["auth"] == "signed_in"
    assert row["ok"] is True
    assert "terminal_action" not in row


@pytest.mark.skipif(sys.platform == "win32", reason="fake CLI is a POSIX shell script")
def test_codex_status_probe_never_logs_token_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    token = "sk-live-TOKEN-7f3a9c"
    _fake_codex(tmp_path, monkeypatch, code=0, printed=f"Logged in as {token}")
    caplog.set_level(logging.DEBUG)
    row = codex_status_probe({})
    assert row["ok"] is True
    assert token not in json.dumps(row)
    assert token not in caplog.text


@pytest.mark.skipif(sys.platform == "win32", reason="fake CLI is a POSIX shell script")
def test_codex_status_probe_child_env_drops_secrets_and_keeps_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import setup_status

    _fake_codex(tmp_path, monkeypatch, code=0)
    monkeypatch.setenv("CIAO_PROBE_TEST_API_KEY", "sk-probe-secret-123")
    seen: list[dict[str, str] | None] = []
    real_run = subprocess.run

    def spy(argv, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(kwargs.get("env"))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(setup_status.subprocess, "run", spy)
    codex_status_probe(dict(os.environ))
    assert len(seen) == 2
    for env in seen:
        assert env is not None
        assert "CIAO_PROBE_TEST_API_KEY" not in env
        assert "sk-probe-secret-123" not in env.values()
        assert env.get("PATH") == os.environ.get("PATH")
