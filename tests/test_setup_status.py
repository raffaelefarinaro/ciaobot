from __future__ import annotations

import os
import json
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
from ciao.setup_status import setup_status
from ciao.web.auth import AuthMiddleware
from ciao.web.routes_api import (
    provider_connection_action,
    setup_finish_endpoint,
    setup_list_dirs_endpoint,
    setup_mkdir_endpoint,
    setup_status_endpoint,
)


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
    monkeypatch.setattr("ciao.setup_status._cli_version", lambda binary: "2.0.0 (Claude Code)")


def _config(tmp_path, env_extra: dict[str, str] | None = None) -> CiaoConfig:
    env = {
        "PWA_AUTH_TOKEN": "test-token",
        "CIAO_PUSH_CONTACT": "mailto:owner@example.com",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        "CIAO_VAULT_ROOT": "memory-vault",
        "CIAO_OLLAMA_API_KEY": "",
    }
    env.update(env_extra or {})
    return CiaoConfig.from_env(env)


def test_setup_status_reports_workspace_and_required_config(tmp_path) -> None:
    config = _config(tmp_path)
    (tmp_path / "memory-vault").mkdir()

    data = setup_status(
        config,
        env={
            "PWA_AUTH_TOKEN": "test-token",
            "CIAO_PUSH_CONTACT": "mailto:owner@example.com",
            "ANTHROPIC_API_KEY": "sk-anthropic",
        },
    )

    checks = {row["id"]: row for row in data["checks"]}
    assert data["workspace_root"] == str(tmp_path.resolve())
    assert data["vault_root"] == str((tmp_path / "memory-vault").resolve())
    assert checks["workspace"]["ok"] is True
    assert checks["vault"]["ok"] is True
    assert checks["pwa_auth_token"]["ok"] is True
    assert checks["push_contact"]["ok"] is True
    assert data["configured"] is True


def test_setup_status_reports_linked_workspace_guides(tmp_path) -> None:
    """The optional guides check tracks whether AGENTS.md resolves to CLAUDE.md."""
    config = _config(tmp_path)
    (tmp_path / "memory-vault").mkdir()
    env = {"PWA_AUTH_TOKEN": "test-token", "ANTHROPIC_API_KEY": "sk-anthropic"}

    checks = {row["id"]: row for row in setup_status(config, env=env)["checks"]}
    assert checks["workspace_guides"]["ok"] is False
    assert checks["workspace_guides"]["required"] is False

    (tmp_path / "CLAUDE.md").write_text("# Guide\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Custom runtime guide\n", encoding="utf-8")
    checks = {row["id"]: row for row in setup_status(config, env=env)["checks"]}
    assert checks["workspace_guides"]["ok"] is False

    (tmp_path / "AGENTS.md").unlink()
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")
    data = setup_status(config, env=env)
    checks = {row["id"]: row for row in data["checks"]}
    assert checks["workspace_guides"]["ok"] is True
    # Optional either way: an unlinked custom AGENTS.md never blocks setup.
    assert data["configured"] is True


def test_setup_status_configured_without_push_contact(tmp_path) -> None:
    """An empty CIAO_PUSH_CONTACT never blocks a configured workspace."""
    config = _config(tmp_path, {"CIAO_PUSH_CONTACT": ""})
    (tmp_path / "memory-vault").mkdir()

    data = setup_status(
        config,
        env={"PWA_AUTH_TOKEN": "test-token", "ANTHROPIC_API_KEY": "sk-anthropic"},
    )

    checks = {row["id"]: row for row in data["checks"]}
    assert checks["push_contact"]["ok"] is False
    assert data["configured"] is True


def test_setup_status_reports_missing_required_config(tmp_path) -> None:
    config = _config(tmp_path, {"CIAO_PUSH_CONTACT": ""})

    data = setup_status(config, env={})

    checks = {row["id"]: row for row in data["checks"]}
    assert checks["vault"]["ok"] is False
    assert checks["pwa_auth_token"]["ok"] is True
    # push contact is optional: reported as not ok, but never blocks setup
    assert checks["push_contact"]["ok"] is False
    assert checks["push_contact"]["required"] is False
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
        env={"PWA_AUTH_TOKEN": "test-token", "ANTHROPIC_API_KEY": "sk-anthropic"},
    )

    checks = {row["id"]: row for row in data["checks"]}
    assert data["workspace_root"] == expected_root
    assert checks["workspace"]["ok"] is True
    assert checks["vault"]["ok"] is True

    # A config without an explicit workspace_root has no root to fall back to,
    # but the payload must still be produced instead of raising.
    bare = setup_status(SimpleNamespace(pwa_auth_token="test-token"), env={})
    assert isinstance(bare["checks"], list)
    assert "providers" in bare


def test_setup_status_marks_bootstrap_mode(tmp_path) -> None:
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})

    data = setup_status(config, env={})

    assert data["mode"] == "bootstrap"
    assert data["bootstrap"] is True
    assert data["configured"] is False


def test_setup_status_detects_claude_api_key_and_credentials_file(tmp_path) -> None:
    config = _config(tmp_path, {"ANTHROPIC_API_KEY": "sk-anthropic"})
    data = setup_status(config, env={"ANTHROPIC_API_KEY": "sk-anthropic"})
    assert data["providers"]["claude"]["ok"] is True
    assert data["providers"]["claude"]["auth"] == "api_key"

    credentials = tmp_path / "claude" / ".credentials.json"
    credentials.parent.mkdir()
    credentials.write_text("{}", encoding="utf-8")
    data = setup_status(
        config,
        env={},
        claude_credentials_path=credentials,
    )
    assert data["providers"]["claude"]["ok"] is True
    assert data["providers"]["claude"]["auth"] == "oauth"


def test_setup_status_detects_claude_oauth_via_config_json(tmp_path) -> None:
    """macOS Claude Code stores the OAuth token in the Keychain and writes the
    account metadata to ~/.claude.json. The probe must treat a populated
    ``oauthAccount`` block as a logged-in session even when no credentials
    file exists and no API key is set."""
    config = _config(tmp_path)
    config_path = tmp_path / ".claude.json"
    config_path.write_text(
        '{"oauthAccount":{"emailAddress":"operator@example.com",'
        '"accountUuid":"abc","organizationName":"Example Org"}}',
        encoding="utf-8",
    )

    data = setup_status(config, env={}, claude_config_path=config_path)

    claude = data["providers"]["claude"]
    assert claude["ok"] is True
    assert claude["auth"] == "oauth"
    assert "operator@example.com" in claude["detail"]


def test_setup_status_ignores_empty_oauth_account(tmp_path) -> None:
    config = _config(tmp_path)
    config_path = tmp_path / ".claude.json"
    config_path.write_text('{"oauthAccount":null}', encoding="utf-8")

    data = setup_status(config, env={}, claude_config_path=config_path)

    assert data["providers"]["claude"]["ok"] is False
    assert data["providers"]["claude"]["auth"] == "missing"


def test_setup_status_reports_a_missing_claude_cli_as_an_install_step(
    tmp_path, monkeypatch
) -> None:
    """No CLI means no chats, so setup asks for the install, not for a login.

    An API key alone does not make Claude usable: Ciaobot drives the ``claude``
    binary through the Agent SDK.
    """
    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: "")
    monkeypatch.setattr("ciao.setup_status.claude_app_path", lambda: "")
    config = _config(tmp_path)

    claude = setup_status(config, env={"ANTHROPIC_API_KEY": "sk-anthropic"})["providers"]["claude"]

    assert claude["ok"] is False
    assert claude["auth"] == "not_installed"
    assert claude["install_url"].startswith("https://code.claude.com/docs/")
    assert "install" in claude["command"]
    assert "not installed" in claude["detail"]


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

    claude = setup_status(config, env={"ANTHROPIC_API_KEY": "sk-anthropic"})["providers"]["claude"]

    assert claude["cli_path"] == "/usr/local/bin/claude"


def test_setup_status_route_is_public_before_login(tmp_path) -> None:
    config = _config(tmp_path)
    (tmp_path / "memory-vault").mkdir()
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/setup-status", setup_status_endpoint, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.serializer = serializer

    resp = TestClient(app).get("/api/setup-status")

    assert resp.status_code == 200
    assert resp.json()["checks"][0]["id"] == "workspace"


def test_setup_finish_writes_real_workspace_and_requests_restart(tmp_path, monkeypatch) -> None:
    # Guard the env handoff assertions below: monkeypatch restores these
    # after the endpoint mutates os.environ directly.
    monkeypatch.setenv("CIAO_WORKSPACE", "")
    monkeypatch.setenv("PWA_PORT", "")
    monkeypatch.setenv("PWA_AUTH_TOKEN", "")
    monkeypatch.setenv("PWA_AUTH_REQUIRED", "")
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    restarts: list[int] = []
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.serializer = serializer
    app.state.request_restart = restarts.append

    workspace = tmp_path / "workspace"
    notes = tmp_path / "notes"
    launch_agents = tmp_path / "LaunchAgents"
    apps = tmp_path / "Applications"
    resp = TestClient(app, base_url="http://localhost:8443").post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(workspace),
            "vault_root": str(notes),
            "push_contact": "mailto:owner@example.com",
            "launch_agents_dir": str(launch_agents),
            "app_dir": str(apps),
            "python": "/opt/ciao/bin/python",
            "port": 9443,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["restart_requested"] is True
    assert restarts == [config.restart_exit_code]
    # The env handoff for the re-exec'd foreground `ciao run`: without it the
    # relaunched process boots back into bootstrap mode.
    assert os.environ["CIAO_WORKSPACE"] == str(workspace.resolve())
    assert os.environ["PWA_PORT"] == "9443"
    # The wizard's password becomes the dashboard password, and it must reach the
    # relaunched process through the environment too: load_dotenv would not
    # override a PWA_AUTH_TOKEN already set for the bootstrap run.
    assert os.environ["PWA_AUTH_TOKEN"] == "wizard-pass"
    assert os.environ["PWA_AUTH_REQUIRED"] == "true"
    env_text = (workspace / ".env").read_text(encoding="utf-8")
    assert "PWA_AUTH_TOKEN=wizard-pass" in env_text
    assert "PWA_AUTH_REQUIRED=true" in env_text
    assert "CIAO_PUSH_CONTACT=mailto:owner@example.com" in env_text
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
    app.state.serializer = serializer
    app.state.request_restart = lambda code: None
    return TestClient(app, base_url="http://localhost:8443")


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
    assert "CIAO_VAULT_MODE=scratch" in env_text
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
    # made in Settings → Workspaces after onboarding.
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
    assert "CIAO_VAULT_MODE=existing" in env_text
    assert "CIAO_VAULT_ROOT=." in env_text
    assert (ws / "MEMORY.md").is_file()
    assert not (ws / "memory-vault").exists()
    registry = json.loads(
        (ws / ".runtime" / "workspaces.json").read_text(encoding="utf-8")
    )
    assert registry[0]["name"] == "journal"
    assert registry[0]["vault_root"] == "."

    loaded = CiaoConfig.from_env(
        {
            "PWA_AUTH_TOKEN": "test-token",
            "CIAO_WORKSPACE": str(ws),
            "CIAO_VAULT_ROOT": ".",
            "CIAO_WORKSPACES": json.dumps(
                [{"name": "journal", "vault_root": "."}]
            ),
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
    client = TestClient(app, base_url="http://localhost:8443")
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
    """Host auth_check must mirror AuthMiddleware when PWA_AUTH_REQUIRED is on."""
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
            "PWA_AUTH_REQUIRED": "true",
            "PWA_AUTH_TOKEN": "secret",
            "CIAO_WORKSPACE": str(tmp_path / "ws"),
        }
    )
    client = TestClient(app, base_url="http://localhost:8443")

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
    monkeypatch.setenv("HOME", str(home))
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
    app.state.serializer = serializer
    app.state.request_restart = restarts.append

    resp = TestClient(app, base_url="http://localhost:8443").post(
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


def test_setup_finish_accepts_empty_push_contact(tmp_path) -> None:
    """Push contact is optional: setup finishes and writes an empty value
    (Web Push stays disabled until configured in Settings)."""
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.serializer = serializer

    workspace = tmp_path / "workspace"
    resp = TestClient(app, base_url="http://localhost:8443").post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(workspace),
            "push_contact": "",
            "launch_agents_dir": str(tmp_path / "LaunchAgents"),
            "app_dir": str(tmp_path / "Applications"),
            "restart": False,
        },
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    env_lines = (workspace / ".env").read_text(encoding="utf-8").splitlines()
    assert "CIAO_PUSH_CONTACT=" in env_lines
    assert not any(
        line.startswith("CIAO_PUSH_CONTACT=") and line != "CIAO_PUSH_CONTACT="
        for line in env_lines
    )


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
    app.state.serializer = serializer

    resp = TestClient(app, base_url="http://localhost:8443").post(
        "/api/setup/finish",
        json={"vault_root": str(tmp_path / "notes")},
    )

    assert resp.status_code == 400
    assert resp.json()["error"] == "workspace is required"


def test_setup_finish_defaults_vault_inside_workspace(tmp_path) -> None:
    """Without an explicit vault_root the vault is created inside the
    workspace as memory-vault/ and everything is one git repo at the root."""
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.serializer = serializer

    workspace = tmp_path / "workspace"
    resp = TestClient(app, base_url="http://localhost:8443").post(
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
    # One repo at the install root; the nested vault is never double-inited.
    assert (workspace / ".git").is_dir()
    assert not (workspace / "personal" / "memory-vault" / ".git").exists()


def test_setup_finish_accepts_0000_host(tmp_path) -> None:
    """0.0.0.0 counts as loopback: users copy it from the bind-address log."""
    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.serializer = serializer

    resp = TestClient(app, base_url="http://0.0.0.0:8443").post(
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


def test_setup_finish_requires_bootstrap_mode(tmp_path) -> None:
    config = _config(tmp_path)
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.serializer = serializer

    resp = TestClient(app, base_url="http://localhost:8443").post(
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
    app.state.serializer = serializer

    resp = TestClient(app, base_url="https://ciao.example").post(
        "/api/setup/finish",
        json={
            "password": "wizard-pass",
            "workspace": str(tmp_path / "workspace"),
            "push_contact": "mailto:owner@example.com",
        },
    )

    assert resp.status_code == 403
    # The refusal tells the user where to go instead.
    assert "open the wizard at http://localhost:8443" in resp.json()["error"]


def _folder_picker_client(tmp_path, *, bootstrap: bool = True, base_url: str = "http://localhost:8443") -> TestClient:
    if bootstrap:
        config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path / "boot")})
    else:
        config = _config(tmp_path)
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[
            Route("/api/setup/list-dirs", setup_list_dirs_endpoint, methods=["GET"]),
            Route("/api/setup/mkdir", setup_mkdir_endpoint, methods=["POST"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.serializer = serializer
    return TestClient(app, base_url=base_url)


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


def test_setup_list_dirs_defaults_to_home_and_abbreviates_display_path(tmp_path) -> None:
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


def test_discover_claude_mcps_filters_connected_and_caches(monkeypatch, tmp_path) -> None:
    from ciao import setup_status

    setup_status.clear_claude_discovery_cache()
    calls = {"n": 0}
    config = tmp_path / ".claude.json"
    config.write_text(
        json.dumps(
            {
                "projects": {
                    str(tmp_path.resolve()): {
                        "disabledMcpServers": [
                            "claude.ai Excalidraw",
                            "claude.ai Gmail",
                            "claude.ai Google Calendar",
                            "claude.ai Google Drive",
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    class FakeResult:
        stdout = (
            "claude.ai Airtable: https://example - ✔ Connected\n"
            "claude.ai Slack: https://example - ! Needs authentication\n"
            "claude.ai Gmail: https://example - ✔ Connected\n"
            "claude.ai Excalidraw: https://example - ✔ Connected\n"
            "claude.ai Figma: https://example - ✔ Connected\n"
            "n8n_mcp: ✔ Connected\n"
            "notion: ✔ Connected\n"
        )
        stderr = ""

    def fake_run(*_args, **_kwargs):
        calls["n"] += 1
        return FakeResult()

    monkeypatch.setattr(setup_status.shutil, "which", lambda _name: "/bin/claude")
    monkeypatch.setattr(setup_status.subprocess, "run", fake_run)

    assert setup_status.discover_claude_mcps(
        tmp_path, config_path=config
    ) == ["Airtable", "Figma"]
    assert setup_status.discover_claude_mcps(
        tmp_path, config_path=config
    ) == ["Airtable", "Figma"]
    assert calls["n"] == 1

    setup_status.clear_claude_discovery_cache()
    assert setup_status.discover_claude_mcps(
        tmp_path, config_path=config
    ) == ["Airtable", "Figma"]
    assert calls["n"] == 2


async def test_provider_verify_action_busts_claude_discovery_cache(
    monkeypatch, tmp_path
) -> None:
    """Verify must not just echo the up-to-5-minute discovery cache.

    A stale "Configured MCP Servers" list that never reflects a connector the
    user just disconnected (or reconnected) would be worse than showing
    nothing, so hitting Verify for Claude has to bust the discovery cache
    before recomputing the payload. Other providers don't have this cache, so
    they should be left alone.
    """
    from ciao.web import routes_api

    calls = {"clear": 0, "payload": 0}

    def fake_clear() -> None:
        calls["clear"] += 1

    def fake_payload(_config):
        calls["payload"] += 1
        return {"connections": {"claude": {"mcps": ["Airtable"]}, "opencode": {}}}

    monkeypatch.setattr("ciao.setup_status.clear_claude_discovery_cache", fake_clear)
    monkeypatch.setattr(routes_api, "_provider_config_payload", fake_payload)

    config = _config(tmp_path)
    request = SimpleNamespace(
        path_params={"provider": "claude", "action": "verify"},
        app=SimpleNamespace(state=SimpleNamespace(config=config)),
    )

    response = await provider_connection_action(request)
    assert json.loads(response.body) == {"mcps": ["Airtable"]}
    assert calls == {"clear": 1, "payload": 1}

    request.path_params["provider"] = "opencode"
    await provider_connection_action(request)
    assert calls == {"clear": 1, "payload": 2}


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
    skill directory would otherwise never see it on the Providers tab.
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


# ── Claude MCP discovery cache ──────────────────────────────────────────
# `claude mcp list` measures ~12s on a real install and sits on the
# Settings -> Providers load path, so the cache decides whether that tab is
# usable. A plain TTL made every visit past the window pay it again.


def test_expired_mcp_cache_serves_stale_and_refreshes_in_background(monkeypatch):
    import threading
    import time as _time

    from ciao import setup_status as ss

    calls = []
    done = threading.Event()

    def slow_discovery(**kwargs):
        calls.append(1)
        if len(calls) > 1:
            done.set()
        return [f"mcp-{len(calls)}"]

    monkeypatch.setattr(ss, "_discover_claude_mcps_uncached", slow_discovery)
    monkeypatch.setattr(ss, "_claude_mcps_cache", None)
    monkeypatch.setattr(ss, "_claude_mcps_refreshing", False)
    monkeypatch.setattr(ss, "_claude_mcps_inflight", None)

    # First call has nothing to serve, so it waits.
    assert ss.discover_claude_mcps(None) == ["mcp-1"]
    assert len(calls) == 1

    # Expire the entry; the next call must return the stale value immediately
    # rather than paying the discovery again.
    stamp, ws_key, value = ss._claude_mcps_cache
    monkeypatch.setattr(
        ss, "_claude_mcps_cache", (stamp - ss._CLAUDE_DISCOVERY_TTL_SECONDS - 1, ws_key, value)
    )
    assert ss.discover_claude_mcps(None) == ["mcp-1"]

    # ...and a refresh runs behind it.
    assert done.wait(timeout=5), "background refresh never ran"
    deadline = _time.monotonic() + 5
    while ss._claude_mcps_cache[2] != ("mcp-2",) and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert ss._claude_mcps_cache[2] == ("mcp-2",)


def test_a_fresh_mcp_cache_does_not_refresh(monkeypatch):
    from ciao import setup_status as ss

    calls = []
    monkeypatch.setattr(
        ss, "_discover_claude_mcps_uncached", lambda **kw: calls.append(1) or ["a"]
    )
    monkeypatch.setattr(ss, "_claude_mcps_cache", None)
    monkeypatch.setattr(ss, "_claude_mcps_refreshing", False)
    monkeypatch.setattr(ss, "_claude_mcps_inflight", None)

    ss.discover_claude_mcps(None)
    ss.discover_claude_mcps(None)
    assert len(calls) == 1, "a fresh entry must not spawn a discovery"


# An empty discovery usually means the health pass timed out, not that nothing
# is connected, so it must expire fast instead of poisoning the tab for the
# full five-minute success TTL.


def test_a_fresh_empty_mcp_cache_serves_without_refresh(monkeypatch):
    from ciao import setup_status as ss

    calls = []
    monkeypatch.setattr(
        ss, "_discover_claude_mcps_uncached", lambda **kw: calls.append(1) or []
    )
    monkeypatch.setattr(ss, "_claude_mcps_cache", None)
    monkeypatch.setattr(ss, "_claude_mcps_refreshing", False)
    monkeypatch.setattr(ss, "_claude_mcps_inflight", None)

    assert ss.discover_claude_mcps(None) == []
    assert ss.discover_claude_mcps(None) == []
    assert len(calls) == 1, "a fresh (empty) entry must not spawn a discovery"


def test_an_empty_mcp_cache_expires_fast_and_recovers(monkeypatch):
    import threading
    import time as _time

    from ciao import setup_status as ss

    calls = []
    done = threading.Event()

    def discovery(**kwargs):
        calls.append(1)
        if len(calls) > 1:
            done.set()
        return [] if len(calls) == 1 else ["recovered"]

    monkeypatch.setattr(ss, "_discover_claude_mcps_uncached", discovery)
    monkeypatch.setattr(ss, "_claude_mcps_cache", None)
    monkeypatch.setattr(ss, "_claude_mcps_refreshing", False)
    monkeypatch.setattr(ss, "_claude_mcps_inflight", None)

    assert ss.discover_claude_mcps(None) == []
    assert calls == [1]

    # Age the entry past the empty TTL but well short of the success TTL: the
    # stale empty list still serves instantly, but a refresh must kick off.
    stamp, ws_key, value = ss._claude_mcps_cache
    assert value == ()
    aged = stamp - (ss._CLAUDE_DISCOVERY_EMPTY_TTL_SECONDS + 1)
    monkeypatch.setattr(ss, "_claude_mcps_cache", (aged, ws_key, value))

    assert ss.discover_claude_mcps(None) == []
    assert done.wait(timeout=5), "empty cache must re-discover in the background"
    deadline = _time.monotonic() + 5
    while ss._claude_mcps_cache[2] != ("recovered",) and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert ss._claude_mcps_cache[2] == ("recovered",)


def test_warm_claude_discovery_cache_populates_both_caches(monkeypatch, tmp_path):
    import time

    from ciao import setup_status as ss

    monkeypatch.setattr(ss, "_claude_mcps_cache", None)
    monkeypatch.setattr(ss, "_claude_mcps_refreshing", False)
    monkeypatch.setattr(ss, "_claude_mcps_inflight", None)
    monkeypatch.setattr(ss, "_claude_skills_cache", None)
    seen = []

    def fake_mcps(workspace_root=None, **kwargs):
        seen.append(("mcps", workspace_root))
        return ["Airtable"]

    def fake_skills():
        seen.append(("skills", None))
        return ["skill-creator"]

    monkeypatch.setattr(ss, "_discover_claude_mcps_uncached", fake_mcps)
    monkeypatch.setattr(ss, "_discover_claude_system_skills_uncached", fake_skills)

    ss.warm_claude_discovery_cache(tmp_path)

    deadline = time.monotonic() + 5
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ("mcps", tmp_path) in seen
    assert ("skills", None) in seen
    assert ss._claude_mcps_cache is not None
    assert ss._claude_mcps_cache[2] == ("Airtable",)
    assert ss._claude_skills_cache is not None
    assert ss._claude_skills_cache[1] == ("skill-creator",)


def test_requests_wait_for_an_in_flight_warmup(monkeypatch, tmp_path):
    """A request arriving mid-warm-up reuses the probe instead of stacking one.

    The warm-up thread and the request path must share a single
    `claude mcp list` run: otherwise a Providers visit right after startup
    blocks for another full health pass and pays for a duplicate probe.
    """
    import threading
    import time as _time

    from ciao import setup_status as ss

    calls = []
    release = threading.Event()
    request_done = threading.Event()

    def slow_probe(**kwargs):
        calls.append(1)
        release.wait(timeout=5)
        return ["Airtable"]

    monkeypatch.setattr(ss, "_discover_claude_mcps_uncached", slow_probe)
    monkeypatch.setattr(ss, "_claude_mcps_cache", None)
    monkeypatch.setattr(ss, "_claude_mcps_refreshing", False)
    monkeypatch.setattr(ss, "_claude_mcps_inflight", None)

    ss.warm_claude_discovery_cache(tmp_path)

    # Wait until the warm-up thread owns the in-flight probe.
    deadline = _time.monotonic() + 5
    while not calls and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert calls == [1], "warm-up probe never started"

    results: list[list[str]] = []

    def request() -> None:
        results.append(ss.discover_claude_mcps(tmp_path))
        request_done.set()

    waiter = threading.Thread(target=request)
    waiter.start()
    _time.sleep(0.05)
    assert len(calls) == 1, "a second probe must not start mid-warm-up"

    release.set()
    assert request_done.wait(timeout=5), "request never served"
    waiter.join(timeout=5)
    assert results == [["Airtable"]]
    assert len(calls) == 1, "request must reuse the warm-up probe"


def test_waiter_reacquires_ownership_when_warmup_targets_another_root(
    monkeypatch, tmp_path
):
    """A waiter never starts a probe while another is in flight.

    If the in-flight warm-up finishes without a usable entry for the waiter's
    workspace root (here: a different root), the waiter loops and reacquires
    ownership instead of running a duplicate probe alongside it.
    """
    import threading
    import time as _time

    from ciao import setup_status as ss

    other_root = tmp_path.parent / "other-ws-root"
    other_root.mkdir()

    calls: list[object] = []
    order: list[str] = []
    release = threading.Event()
    request_done = threading.Event()

    def probe(workspace_root=None, **kwargs):
        calls.append(workspace_root)
        if workspace_root == other_root:
            order.append("request-start")
            return ["Other"]
        order.append("warmup-start")
        release.wait(timeout=5)
        order.append("warmup-end")
        return ["Warm"]

    monkeypatch.setattr(ss, "_discover_claude_mcps_uncached", probe)
    monkeypatch.setattr(ss, "_claude_mcps_cache", None)
    monkeypatch.setattr(ss, "_claude_mcps_refreshing", False)
    monkeypatch.setattr(ss, "_claude_mcps_inflight", None)

    ss.warm_claude_discovery_cache(tmp_path)

    deadline = _time.monotonic() + 5
    while not calls and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert order == ["warmup-start"], "warm-up probe never started"

    results: list[list[str]] = []

    def request() -> None:
        results.append(ss.discover_claude_mcps(other_root))
        request_done.set()

    waiter = threading.Thread(target=request)
    waiter.start()
    _time.sleep(0.05)
    assert calls == [tmp_path], "waiter must not probe while warm-up owns"

    release.set()
    assert request_done.wait(timeout=5), "waiter never reacquired ownership"
    waiter.join(timeout=5)
    assert results == [["Other"]]
    assert order == ["warmup-start", "warmup-end", "request-start"], (
        "request probe must start only after the warm-up finished"
    )
    assert calls == [tmp_path, other_root]
