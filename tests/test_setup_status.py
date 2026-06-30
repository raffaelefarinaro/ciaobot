from __future__ import annotations

from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.setup_status import setup_status
from ciao.web.auth import AuthMiddleware
from ciao.web.routes_api import setup_finish_endpoint, setup_status_endpoint


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


def test_setup_status_reports_missing_required_config(tmp_path) -> None:
    config = _config(tmp_path, {"CIAO_PUSH_CONTACT": ""})

    data = setup_status(config, env={})

    checks = {row["id"]: row for row in data["checks"]}
    assert checks["vault"]["ok"] is False
    assert checks["pwa_auth_token"]["ok"] is True
    assert checks["push_contact"]["ok"] is False
    assert data["configured"] is False


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


def test_setup_status_detects_ollama_cloud_key_or_local_daemon(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path, {"CIAO_OLLAMA_API_KEY": "sk-ollama"})
    data = setup_status(config, env={"CIAO_OLLAMA_API_KEY": "sk-ollama"})
    assert data["providers"]["ollama"]["ok"] is True
    assert data["providers"]["ollama"]["auth"] == "api_key"

    monkeypatch.setattr("ciao.setup_status._ollama_daemon_ready", lambda url: True)
    config = _config(tmp_path)
    data = setup_status(config, env={})
    assert data["providers"]["ollama"]["ok"] is True
    assert data["providers"]["ollama"]["auth"] == "local_daemon"


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


def test_setup_finish_writes_real_workspace_and_requests_restart(tmp_path) -> None:
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
    env_text = (workspace / ".env").read_text(encoding="utf-8")
    assert f"PWA_AUTH_TOKEN={config.pwa_auth_token}" in env_text
    assert "CIAO_PUSH_CONTACT=mailto:owner@example.com" in env_text
    assert f"CIAO_VAULT_ROOT={notes}" in env_text
    assert (notes / "MEMORY.md").is_file()
    assert not (workspace / "memory-vault" / "MEMORY.md").exists()
    assert (launch_agents / "com.ciao.server.plist").is_file()
    assert (apps / "Ciao.app" / "Contents" / "MacOS" / "Ciao").is_file()


def test_setup_finish_requires_bootstrap_mode_and_push_contact(tmp_path) -> None:
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
            "workspace": str(tmp_path / "workspace"),
            "push_contact": "mailto:owner@example.com",
        },
    )

    assert resp.status_code == 403
