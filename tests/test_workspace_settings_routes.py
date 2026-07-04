from __future__ import annotations

import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.web.routes_api import (
    delete_workspace_setting,
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
    assert data["provider_options"] == [{"value": "claude", "label": "Claude"}]

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
        {"value": "claude", "label": "Claude"},
        {"value": "ollama", "label": "Ollama"},
        {"value": "openrouter", "label": "OpenRouter"},
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

    # Personal default: toggle off → connectors blocked, n8n extra blocked.
    personal = config.disallowed_tools_for_workspace("personal")
    assert "mcp__claude_ai_Airtable" in personal
    assert "mcp__n8n_mcp" in personal

    # Flip the personal toggle on via PATCH; keep n8n as an explicit extra.
    resp = client.patch(
        "/api/workspaces/personal",
        json={"claude_ai_mcps": True, "disallowed_tools": "mcp__n8n_mcp"},
    )
    assert resp.status_code == 200
    ws = next(w for w in resp.json()["workspaces"] if w["name"] == "personal")
    assert ws["claude_ai_mcps"] is True
    # Connectors now allowed; only the n8n extra remains.
    assert config.disallowed_tools_for_workspace("personal") == ["mcp__n8n_mcp"]
    assert config.claude_ai_mcps_for_workspace("personal") is True

    # Persisted to disk.
    stored = json.loads((tmp_path / ".runtime" / "workspaces.json").read_text())
    personal_stored = next(w for w in stored if w["name"] == "personal")
    assert personal_stored["claude_ai_mcps"] is True
    assert personal_stored["disallowed_tools"] == ["mcp__n8n_mcp"]

    # "default" string clears the toggle back to the per-workspace default.
    resp = client.patch(
        "/api/workspaces/personal",
        json={"claude_ai_mcps": "default"},
    )
    assert resp.status_code == 200
    assert config.claude_ai_mcps_for_workspace("personal") is False
    assert "mcp__claude_ai_Airtable" in config.disallowed_tools_for_workspace("personal")

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
    assert data["keys"]["ANTHROPIC_API_KEY"]["configured"] is True
    assert data["keys"]["OPENAI_API_KEY"]["configured"] is True
    assert data["keys"]["CIAO_OLLAMA_API_KEY"]["configured"] is False
    assert data["auto_update_github_skills"] is True
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
