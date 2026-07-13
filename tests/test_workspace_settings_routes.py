from __future__ import annotations

import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.web.routes_api import (
    delete_workspace_setting,
    gws_integration_settings,
    gws_install,
    gws_save_client_secret,
    gws_auth_url,
    gws_exchange_code,
    gws_disconnect,
    list_workspaces,
    provider_config_settings,
    upsert_workspace_setting,
)


class _PCM:
    def __init__(self) -> None:
        self.refresh_count = 0

    def refresh_workspaces(self) -> None:
        self.refresh_count += 1


def _client(tmp_path: Path, env_extra: dict[str, str] | None = None):
    env = {
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        "CIAO_OLLAMA_LOCAL_DISCOVERY": "0",
    }
    env.update(env_extra or {})
    config = CiaoConfig.from_env(env)
    pcm = _PCM()
    app = Starlette(
        routes=[
            Route("/api/workspaces", list_workspaces, methods=["GET"]),
            Route("/api/workspaces", upsert_workspace_setting, methods=["POST"]),
            Route(
                "/api/workspaces/{name}",
                upsert_workspace_setting,
                methods=["PATCH"],
            ),
            Route(
                "/api/workspaces/{name}",
                delete_workspace_setting,
                methods=["DELETE"],
            ),
            Route(
                "/api/settings/providers",
                provider_config_settings,
                methods=["GET", "PATCH"],
            ),
            Route(
                "/api/integrations/gws",
                gws_integration_settings,
                methods=["GET"],
            ),
            Route(
                "/api/integrations/gws/install",
                gws_install,
                methods=["POST"],
            ),
            Route(
                "/api/integrations/gws/client-secret",
                gws_save_client_secret,
                methods=["POST"],
            ),
            Route(
                "/api/integrations/gws/auth-url",
                gws_auth_url,
                methods=["POST"],
            ),
            Route(
                "/api/integrations/gws/exchange",
                gws_exchange_code,
                methods=["POST"],
            ),
            Route(
                "/api/integrations/gws/disconnect",
                gws_disconnect,
                methods=["POST"],
            ),
        ]
    )
    app.state.config = config
    app.state.project_chat_manager = pcm
    return TestClient(app), config, pcm


def test_post_workspace_persists_runtime_registry_and_updates_live_config(tmp_path):
    client, config, pcm = _client(tmp_path)

    resp = client.post(
        "/api/workspaces",
        json={
            "name": "client-a",
            "vault_root": "vaults/client-a",
            "default_provider": "claude",
            "default_model": "kimi-k2.7-code:cloud",
            "gws_profile": "work",
            "model_bucket": "anthropic",
            "disallowed_tools": ["mcp__claude_ai_Slack", "Bash"],
        },
    )

    assert resp.status_code == 201
    data = resp.json()
    names = [workspace["name"] for workspace in data["workspaces"]]
    assert "client-a" in names
    assert config.workspace("client-a").default_provider == "claude"
    assert config.default_model_for_workspace("client-a") == "kimi-k2.7-code:cloud"
    assert config.disallowed_tools_for_workspace("client-a") == [
        "mcp__claude_ai_Slack",
        "Bash",
    ]
    assert pcm.refresh_count == 1
    assert data["provider_options"] == [
        {"value": "claude", "label": "Anthropic (via Claude Code)"},
        {"value": "codex", "label": "OpenAI (via Codex)"},
    ]

    stored = json.loads((tmp_path / ".runtime" / "workspaces.json").read_text())
    client_workspace = next(item for item in stored if item["name"] == "client-a")
    assert client_workspace == {
        "name": "client-a",
        "vault_root": "vaults/client-a",
        "default_provider": "claude",
        "default_model": "kimi-k2.7-code:cloud",
        "disallowed_tools": ["mcp__claude_ai_Slack", "Bash"],
        "claude_ai_mcps": None,
        "gws_profile": "work",
        "model_bucket": "anthropic",
    }


def test_patch_and_delete_workspace_update_runtime_registry(tmp_path):
    client, config, pcm = _client(tmp_path)
    client.post(
        "/api/workspaces",
        json={"name": "client-a", "vault_root": "vaults/client-a"},
    )

    patch = client.patch(
        "/api/workspaces/client-a",
        json={"default_model": "sonnet", "disallowed_tools": "mcp__example,Bash"},
    )
    assert patch.status_code == 200
    assert config.workspace("client-a").default_model == "sonnet"
    assert config.disallowed_tools_for_workspace("client-a") == ["mcp__example", "Bash"]

    delete = client.delete("/api/workspaces/client-a")
    assert delete.status_code == 200
    assert config.workspace("client-a") is None
    assert json.loads((tmp_path / ".runtime" / "workspaces.json").read_text()) == [
        {
            "name": "personal",
            "vault_root": "personal",
            "default_provider": "claude",
            "default_model": "",
            "disallowed_tools": None,
            "claude_ai_mcps": None,
            "gws_profile": "personal",
            "model_bucket": "personal",
        },
        {
            "name": "work",
            "vault_root": "work",
            "default_provider": "claude",
            "default_model": "",
            "disallowed_tools": None,
            "claude_ai_mcps": None,
            "gws_profile": "work",
            "model_bucket": "work",
        },
    ]
    assert pcm.refresh_count == 3


def test_workspace_validation_rejects_bad_name_and_provider(tmp_path):
    client, _config, _pcm = _client(tmp_path)

    bad_name = client.post("/api/workspaces", json={"name": "../bad"})
    assert bad_name.status_code == 400
    assert "name" in bad_name.json()["error"]

    bad_provider = client.post(
        "/api/workspaces",
        json={"name": "client-a", "default_provider": "telepathy"},
    )
    assert bad_provider.status_code == 400
    assert "provider" in bad_provider.json()["error"]


def test_workspace_provider_options_follow_available_backends(tmp_path):
    client, _config, _pcm = _client(
        tmp_path,
        {
            "CIAO_OLLAMA_API_KEY": "sk-ollama",
            "OPENROUTER_API_KEY": "sk-or",
        },
    )

    data = client.get("/api/workspaces").json()
    assert data["provider_options"] == [
        {"value": "claude", "label": "Anthropic (via Claude Code)"},
        {"value": "codex", "label": "OpenAI (via Codex)"},
        {"value": "ollama", "label": "Ollama (via Claude Code)"},
        {"value": "openrouter", "label": "OpenRouter (via Claude Code)"},
    ]

    resp = client.post(
        "/api/workspaces",
        json={"name": "client-a", "default_provider": "openrouter"},
    )
    assert resp.status_code == 201
    assert resp.json()["workspaces"][-1]["default_provider"] == "openrouter"


def test_claude_ai_mcps_toggle_persists_and_resolves(tmp_path):
    """The claude.ai MCPs toggle is persisted on the workspace and drives the
    connector portion of the effective denylist (union with extras)."""
    client, config, _pcm = _client(tmp_path)

    # Personal default: toggle on -> connectors allowed, n8n extra blocked.
    personal = config.disallowed_tools_for_workspace("personal")
    assert "mcp__claude_ai_Airtable" not in personal
    assert "mcp__n8n_mcp" in personal

    # Flip the personal toggle off via PATCH; keep n8n as an explicit extra.
    resp = client.patch(
        "/api/workspaces/personal",
        json={"claude_ai_mcps": False, "disallowed_tools": "mcp__n8n_mcp"},
    )
    assert resp.status_code == 200
    ws = next(w for w in resp.json()["workspaces"] if w["name"] == "personal")
    assert ws["claude_ai_mcps"] is False
    # Connectors now blocked; n8n extra also blocked.
    assert "mcp__claude_ai_Airtable" in config.disallowed_tools_for_workspace("personal")
    assert "mcp__n8n_mcp" in config.disallowed_tools_for_workspace("personal")
    assert config.claude_ai_mcps_for_workspace("personal") is False

    # Persisted to disk.
    stored = json.loads((tmp_path / ".runtime" / "workspaces.json").read_text())
    personal_stored = next(w for w in stored if w["name"] == "personal")
    assert personal_stored["claude_ai_mcps"] is False
    assert personal_stored["disallowed_tools"] == ["mcp__n8n_mcp"]

    # "default" string clears the toggle back to the default (on).
    resp = client.patch(
        "/api/workspaces/personal",
        json={"claude_ai_mcps": "default"},
    )
    assert resp.status_code == 200
    assert config.claude_ai_mcps_for_workspace("personal") is True
    assert "mcp__claude_ai_Airtable" not in config.disallowed_tools_for_workspace("personal")

    # The payload advertises the connector set for the PWA label.
    payload = client.get("/api/workspaces").json()
    assert "mcp__claude_ai_Airtable" in payload["claude_ai_connectors"]


def test_provider_config_status_and_write_only_patch(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PWA_AUTH_TOKEN=t\nCIAO_PUSH_CONTACT=mailto:owner@example.com\nANTHROPIC_API_KEY=sk-anthropic\nOPENAI_API_KEY=old\n",
        encoding="utf-8",
    )
    client, _config, _pcm = _client(
        tmp_path,
        {
            "ANTHROPIC_API_KEY": "sk-anthropic",
            "OPENAI_API_KEY": "old",
            "CIAO_OLLAMA_API_KEY": "",
        },
    )

    data = client.get("/api/settings/providers").json()
    assert "ANTHROPIC_API_KEY" not in data["keys"]
    assert data["service_keys"]["OPENAI_API_KEY"]["configured"] is True
    assert data["keys"]["CIAO_OLLAMA_API_KEY"]["configured"] is False
    assert data["auto_update_github_skills"] is False
    assert "sk-anthropic" not in json.dumps(data)
    assert "OPENAI_API_KEY=old" not in json.dumps(data)

    resp = client.patch(
        "/api/settings/providers",
        json={
            "keys": {
                "OPENAI_API_KEY": "",
                "CIAO_OLLAMA_API_KEY": "sk-ollama",
                "UNSUPPORTED_KEY": "nope",
            }
        },
    )

    assert resp.status_code == 400

    resp = client.patch(
        "/api/settings/providers",
        json={
            "keys": {"OPENAI_API_KEY": "", "CIAO_OLLAMA_API_KEY": "sk-ollama"},
            "auto_update_github_skills": False
        },
    )
    assert resp.status_code == 200
    env_text = env_path.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=" not in env_text
    assert "CIAO_OLLAMA_API_KEY=sk-ollama" in env_text
    assert "CIAO_AUTO_UPDATE_GITHUB_SKILLS=false" in env_text
    assert "sk-ollama" not in json.dumps(resp.json())
    assert resp.json()["auto_update_github_skills"] is False


def test_gws_integration_reports_profile_status_and_usage(tmp_path, monkeypatch):
    from ciao.web import routes_api

    monkeypatch.setattr(
        routes_api,
        "resolve_tool",
        lambda name: "/usr/local/bin/gws" if name == "gws" else None,
    )
    personal_dir = tmp_path / "secrets" / "gws-personal"
    personal_dir.mkdir(parents=True)
    (personal_dir / "credentials.json").write_text("{}", encoding="utf-8")
    (personal_dir / "client_secret.json").write_text("{}", encoding="utf-8")

    client, _config, _pcm = _client(tmp_path)

    data = client.get("/api/integrations/gws").json()
    assert data["installed"] is True
    assert data["binary_path"] == "/usr/local/bin/gws"
    assert data["default_profile"] == "personal"

    profiles = {profile["name"]: profile for profile in data["profiles"]}
    assert profiles["personal"]["configured"] is True
    assert profiles["personal"]["client_secret_present"] is True
    assert profiles["personal"]["workspaces"] == ["personal"]
    assert profiles["personal"]["setup_command"] == "scripts/gws-profile.sh personal auth login --full"

    assert profiles["work"]["configured"] is False
    assert profiles["work"]["workspaces"] == ["work"]
    assert str(personal_dir) in profiles["personal"]["config_dir"]


def test_gws_install_when_already_present_is_noop(tmp_path, monkeypatch):
    from ciao.web import routes_api

    monkeypatch.setattr(
        routes_api,
        "resolve_tool",
        lambda name: "/usr/local/bin/gws" if name == "gws" else None,
    )
    client, _config, _pcm = _client(tmp_path)

    resp = client.post("/api/integrations/gws/install")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["integration"]["installed"] is True


def test_gws_install_reports_missing_npm(tmp_path, monkeypatch):
    from ciao.web import routes_api

    # Neither gws nor npm resolvable.
    monkeypatch.setattr(routes_api, "resolve_tool", lambda name: None)
    client, _config, _pcm = _client(tmp_path)

    resp = client.post("/api/integrations/gws/install")
    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False
    assert "npm" in body["error"]


def test_gws_install_runs_npm_and_returns_refreshed_status(tmp_path, monkeypatch):
    from ciao.web import routes_api

    resolved = {"gws": None, "npm": "/usr/local/bin/npm"}
    monkeypatch.setattr(routes_api, "resolve_tool", lambda name: resolved.get(name))

    captured = {}

    class _Result:
        returncode = 0
        stdout = "+ @googleworkspace/cli@1.2.3"
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        # Simulate gws now being installed for the post-install refresh.
        resolved["gws"] = "/usr/local/bin/gws"
        return _Result()

    monkeypatch.setattr(routes_api.subprocess, "run", fake_run)
    client, _config, _pcm = _client(tmp_path)

    resp = client.post("/api/integrations/gws/install")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert captured["cmd"] == ["/usr/local/bin/npm", "install", "-g", "@googleworkspace/cli"]
    assert body["integration"]["installed"] is True
    assert body["integration"]["binary_path"] == "/usr/local/bin/gws"


def test_gws_setup_endpoints(tmp_path, monkeypatch):
    import json

    client, _config, _pcm = _client(tmp_path)

    # 1. Test save client secret
    valid_secret = {
        "installed": {
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
            "redirect_uris": ["http://localhost"]
        }
    }
    resp = client.post(
        "/api/integrations/gws/client-secret",
        json={"profile": "personal", "client_secret": json.dumps(valid_secret)}
    )
    assert resp.status_code == 200
    data = resp.json()
    profiles = {p["name"]: p for p in data["profiles"]}
    assert profiles["personal"]["client_secret_present"] is True
    assert profiles["personal"]["configured"] is False

    # Check validation error
    resp = client.post(
        "/api/integrations/gws/client-secret",
        json={"profile": "personal", "client_secret": "{invalid json"}
    )
    assert resp.status_code == 400

    resp = client.post(
        "/api/integrations/gws/client-secret",
        json={"profile": "personal", "client_secret": json.dumps({"wrong": "format"})}
    )
    assert resp.status_code == 400

    # 2. Test get auth URL
    resp = client.post(
        "/api/integrations/gws/auth-url",
        json={"profile": "personal"}
    )
    assert resp.status_code == 200
    assert "accounts.google.com/o/oauth2/auth" in resp.json()["auth_url"]
    assert "client_id=test-client-id" in resp.json()["auth_url"]

    # 3. Test exchange code
    # Mock urllib.request.urlopen to return tokens
    class MockResponse:
        def read(self):
            import base64
            payload = base64.urlsafe_b64encode(b'{"email": "test-email@example.com"}').decode("utf-8")
            id_token_val = f"header.{payload}.signature"
            return f'{{"refresh_token": "mock-refresh-token", "id_token": "{id_token_val}"}}'.encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_called = False
    def mock_urlopen(req):
        nonlocal mock_called
        mock_called = True
        assert req.full_url == "https://oauth2.googleapis.com/token"
        return MockResponse()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    # Use a redirect URL as input
    resp = client.post(
        "/api/integrations/gws/exchange",
        json={"profile": "personal", "code": "http://localhost/?code=test-code"}
    )
    assert resp.status_code == 200
    assert mock_called is True
    data = resp.json()
    profiles = {p["name"]: p for p in data["profiles"]}
    assert profiles["personal"]["configured"] is True
    assert profiles["personal"]["email"] == "test-email@example.com"

    # 4. Test disconnect
    resp = client.post(
        "/api/integrations/gws/disconnect",
        json={"profile": "personal", "delete_client_secret": False}
    )
    assert resp.status_code == 200
    profiles = {p["name"]: p for p in resp.json()["profiles"]}
    assert profiles["personal"]["configured"] is False
    assert profiles["personal"]["client_secret_present"] is True

    # Disconnect and delete client secret
    resp = client.post(
        "/api/integrations/gws/disconnect",
        json={"profile": "personal", "delete_client_secret": True}
    )
    assert resp.status_code == 200
    profiles = {p["name"]: p for p in resp.json()["profiles"]}
    assert profiles["personal"]["configured"] is False
    assert profiles["personal"]["client_secret_present"] is False
