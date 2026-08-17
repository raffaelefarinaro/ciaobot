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
    gws_add_profile,
    gws_remove_profile,
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
            Route(
                "/api/integrations/gws/profiles/add",
                gws_add_profile,
                methods=["POST"],
            ),
            Route(
                "/api/integrations/gws/profiles/remove",
                gws_remove_profile,
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
            "vault_root": "client-a",
            "default_provider": "claude",
            "default_model": "kimi-k2.7-code:cloud",
            "gws_profile": "work",
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
        {"value": "opencode", "label": "opencode"},
    ]

    stored = json.loads((tmp_path / ".runtime" / "workspaces.json").read_text())
    client_workspace = next(item for item in stored if item["name"] == "client-a")
    assert client_workspace == {
        "name": "client-a",
        "vault_root": "memory-vault/client-a",
        "default_provider": "claude",
        "default_model": "kimi-k2.7-code:cloud",
        "disallowed_tools": ["mcp__claude_ai_Slack", "Bash"],
        "claude_ai_mcps": None,
        "gws_profile": "work",
        "color": "pink",
    }


def test_patch_and_delete_workspace_update_runtime_registry(tmp_path):
    client, config, pcm = _client(tmp_path)
    client.post(
        "/api/workspaces",
        json={"name": "client-a", "vault_root": "client-a"},
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
            "vault_root": "memory-vault/personal",
            "default_provider": "claude",
            "default_model": "",
            "disallowed_tools": None,
            "claude_ai_mcps": None,
            "gws_profile": "personal",
            "color": "pink",
        },
        {
            "name": "work",
            "vault_root": "memory-vault/work",
            "default_provider": "claude",
            "default_model": "",
            "disallowed_tools": None,
            "claude_ai_mcps": None,
            "gws_profile": "work",
            "color": "pink",
        },
    ]
    assert pcm.refresh_count == 3


def test_workspace_save_never_accepts_a_request_body_vault_path(tmp_path):
    client, config, _pcm = _client(tmp_path)

    created = client.post(
        "/api/workspaces",
        json={"name": "research", "vault_root": "/"},
    )
    assert created.status_code == 201
    assert (
        config.workspace_vault_root("research")
        == tmp_path / "memory-vault" / "research"
    )

    existing = config.workspace("research")
    assert existing is not None
    existing.vault_root = str(tmp_path / "external-vault")
    patched = client.patch(
        "/api/workspaces/research",
        json={"vault_root": "../outside", "default_model": "sonnet"},
    )
    assert patched.status_code == 200
    assert config.workspace("research").vault_root == str(
        tmp_path / "external-vault"
    )


def test_workspace_creation_rejects_a_symlinked_vault_folder(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "research").symlink_to(target, target_is_directory=True)
    client, config, pcm = _client(tmp_path)

    response = client.post("/api/workspaces", json={"name": "research"})

    assert response.status_code == 400
    assert "symlink" in response.json()["error"]
    assert config.workspace("research") is None
    assert pcm.refresh_count == 0


def test_workspace_creation_rejects_case_and_vault_owner_collisions(tmp_path):
    client, config, _pcm = _client(tmp_path)
    assert client.post("/api/workspaces", json={"name": "Research"}).status_code == 201

    case_collision = client.post(
        "/api/workspaces",
        json={"name": "research"},
    )
    assert case_collision.status_code == 400
    assert "conflicts" in case_collision.json()["error"]

    existing = config.workspace("work")
    assert existing is not None
    existing.vault_root = str(tmp_path / "memory-vault" / "client")
    root_collision = client.post(
        "/api/workspaces",
        json={"name": "client"},
    )
    assert root_collision.status_code == 400
    assert "already owned" in root_collision.json()["error"]


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


def test_workspace_provider_options_are_the_runtime_providers(tmp_path):
    """The registry is the only source: no backend keys widen this list."""
    client, _config, _pcm = _client(tmp_path)

    data = client.get("/api/workspaces").json()
    assert data["provider_options"] == [
        {"value": "claude", "label": "Anthropic (via Claude Code)"},
        {"value": "codex", "label": "OpenAI (via Codex)"},
        {"value": "opencode", "label": "opencode"},
    ]

    resp = client.post(
        "/api/workspaces",
        json={"name": "client-a", "default_provider": "opencode"},
    )
    assert resp.status_code == 201
    assert resp.json()["workspaces"][-1]["default_provider"] == "opencode"


def test_stale_stored_provider_serializes_coerced_and_saves(tmp_path):
    """A registry written by a pre-refactor release (provider "ollama", legacy
    ``model_bucket`` key) must list with a registered provider — the PWA
    renders it into a <select> limited to ``provider_options`` — and a PATCH
    must round-trip instead of 400ing on the stale stored value."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "workspaces.json").write_text(
        json.dumps(
            [
                {
                    "name": "personal",
                    "vault_root": "memory-vault/personal",
                    "default_provider": "ollama",
                    "default_model": "qwen3:latest",
                    "model_bucket": "big",
                    "gws_profile": "personal",
                },
                {
                    "name": "work",
                    "vault_root": "memory-vault/work",
                    "default_provider": "claude",
                    "gws_profile": "work",
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    client, config, _pcm = _client(tmp_path)
    assert config.workspace("personal").default_provider == "ollama"

    listed = client.get("/api/workspaces").json()
    personal = next(w for w in listed["workspaces"] if w["name"] == "personal")
    option_values = {option["value"] for option in listed["provider_options"]}
    # Coerced to the effective provider, mirroring default_provider_for_workspace.
    assert personal["default_provider"] == "claude"
    assert personal["default_provider"] in option_values

    # A save that omits the provider keeps working despite the stale record.
    untouched = client.patch(
        "/api/workspaces/personal",
        json={"default_model": "sonnet"},
    )
    assert untouched.status_code == 200

    # Saving the coerced value back round-trips and rewrites a clean record.
    resp = client.patch(
        "/api/workspaces/personal",
        json={"default_provider": personal["default_provider"]},
    )
    assert resp.status_code == 200
    assert config.workspace("personal").default_provider == "claude"

    stored = json.loads((runtime / "workspaces.json").read_text())
    personal_stored = next(w for w in stored if w["name"] == "personal")
    assert personal_stored["default_provider"] == "claude"
    assert "model_bucket" not in personal_stored

    # Explicitly invalid writes are still rejected.
    bad = client.patch(
        "/api/workspaces/personal",
        json={"default_provider": "ollama"},
    )
    assert bad.status_code == 400


def test_persist_workspace_registry_normalizes_stale_provider(tmp_path):
    """Direct config persistence must not resurrect a removed provider."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "workspaces.json").write_text(
        json.dumps([{
            "name": "personal",
            "vault_root": "memory-vault/personal",
            "default_provider": "ollama",
        }]) + "\n",
        encoding="utf-8",
    )

    config = CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(runtime),
    })
    assert config.workspace("personal").default_provider == "ollama"

    config.persist_workspace_registry()

    stored = json.loads((runtime / "workspaces.json").read_text())
    assert stored[0]["default_provider"] == "claude"


def test_claude_ai_mcps_toggle_persists_and_resolves(tmp_path):
    """The claude.ai MCPs toggle is persisted on the workspace and drives the
    connector portion of the effective denylist (union with extras)."""
    client, config, _pcm = _client(tmp_path)

    # Personal default: toggle on -> connectors allowed, harness tools blocked.
    personal = config.disallowed_tools_for_workspace("personal")
    assert "mcp__claude_ai_Airtable" not in personal
    assert "EnterPlanMode" in personal

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


def test_workspace_color_defaults_persists_and_validates(tmp_path):
    """Accent color defaults to pink, persists on write, and rejects unknowns."""
    client, config, _pcm = _client(tmp_path)

    listed = client.get("/api/workspaces").json()
    personal = next(w for w in listed["workspaces"] if w["name"] == "personal")
    assert personal["color"] == "pink"

    create = client.post(
        "/api/workspaces",
        json={"name": "client-a", "color": "cyan"},
    )
    assert create.status_code == 201
    created = next(w for w in create.json()["workspaces"] if w["name"] == "client-a")
    assert created["color"] == "cyan"
    assert config.workspace("client-a").color == "cyan"

    patch = client.patch(
        "/api/workspaces/client-a",
        json={"color": "emerald"},
    )
    assert patch.status_code == 200
    updated = next(w for w in patch.json()["workspaces"] if w["name"] == "client-a")
    assert updated["color"] == "emerald"
    assert config.workspace("client-a").color == "emerald"

    stored = json.loads((tmp_path / ".runtime" / "workspaces.json").read_text())
    assert next(w for w in stored if w["name"] == "client-a")["color"] == "emerald"

    # Other fields can update without resetting color.
    keep = client.patch(
        "/api/workspaces/client-a",
        json={"default_model": "sonnet"},
    )
    assert keep.status_code == 200
    assert config.workspace("client-a").color == "emerald"

    bad = client.patch("/api/workspaces/client-a", json={"color": "neon"})
    assert bad.status_code == 400
    assert "color" in bad.json()["error"]


def test_provider_config_offers_no_api_keys(tmp_path, monkeypatch):
    """Settings -> Providers has no key fields left to type into.

    Every provider authenticates through its own CLI, so both key maps are
    empty and a PATCH naming any key is rejected rather than silently written.
    """
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PWA_AUTH_TOKEN=t\nCIAO_PUSH_CONTACT=mailto:owner@example.com\nANTHROPIC_API_KEY=sk-anthropic\n",
        encoding="utf-8",
    )
    client, _config, _pcm = _client(tmp_path, {"ANTHROPIC_API_KEY": "sk-anthropic"})

    data = client.get("/api/settings/providers").json()
    assert data["keys"] == {}
    # service_keys is empty since voice moved on-device: OPENAI_API_KEY was
    # the only entry, and nothing in the app reads it any more.
    assert data["service_keys"] == {}
    assert data["auto_update_github_skills"] is False
    assert "sk-anthropic" not in json.dumps(data)
    # The connection rows survive: they are how a provider is signed in.
    assert set(data["connections"]) == {"claude", "codex", "opencode"}

    resp = client.patch(
        "/api/settings/providers",
        json={"keys": {"OPENROUTER_API_KEY": "sk-or"}},
    )
    assert resp.status_code == 400

    resp = client.patch(
        "/api/settings/providers",
        json={"auto_update_github_skills": False},
    )
    assert resp.status_code == 200
    assert "CIAO_AUTO_UPDATE_GITHUB_SKILLS=false" in env_path.read_text(encoding="utf-8")
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

    assert str(personal_dir) in profiles["personal"]["config_dir"]
    # Accounts are the user's: only the connected one is listed. "work" has no
    # credentials on disk and was never added, so it is not invented here.
    assert "work" not in profiles


def test_gws_integration_starts_with_no_google_accounts(tmp_path, monkeypatch):
    """A fresh install shows an empty account list, not a personal/work pair."""
    from ciao.web import routes_api

    monkeypatch.setattr(routes_api, "resolve_tool", lambda name: "")
    client, _config, _pcm = _client(tmp_path)

    data = client.get("/api/integrations/gws").json()

    assert data["profiles"] == []
    assert data["default_profile"] == ""


def test_gws_profile_add_and_remove_round_trip(tmp_path, monkeypatch):
    from ciao.web import routes_api

    monkeypatch.setattr(routes_api, "resolve_tool", lambda name: "")
    client, config, _pcm = _client(tmp_path)

    added = client.post(
        "/api/integrations/gws/profiles/add",
        json={"name": "Acme Corp", "label": "Acme (work)"},
    )
    assert added.status_code == 200
    profiles = {profile["name"]: profile for profile in added.json()["profiles"]}
    assert list(profiles) == ["acme-corp"]
    assert profiles["acme-corp"]["label"] == "Acme (work)"
    assert profiles["acme-corp"]["configured"] is False
    assert (
        profiles["acme-corp"]["setup_command"]
        == "scripts/gws-profile.sh acme-corp auth login --full"
    )

    # Adding the same account twice is a user error, not a silent duplicate.
    duplicate = client.post("/api/integrations/gws/profiles/add", json={"name": "acme-corp"})
    assert duplicate.status_code == 400

    # Credentials written by the OAuth flow are deleted along with the account.
    config_dir = tmp_path / "secrets" / "gws-acme-corp"
    config_dir.mkdir(parents=True)
    (config_dir / "credentials.json").write_text("{}", encoding="utf-8")

    removed = client.post(
        "/api/integrations/gws/profiles/remove", json={"profile": "acme-corp"}
    )
    assert removed.status_code == 200
    assert removed.json()["profiles"] == []
    assert not config_dir.exists()


def test_gws_profile_remove_unlinks_workspaces(tmp_path, monkeypatch):
    from ciao.web import routes_api

    monkeypatch.setattr(routes_api, "resolve_tool", lambda name: "")
    client, config, _pcm = _client(tmp_path)
    client.post("/api/integrations/gws/profiles/add", json={"name": "acme"})
    workspace = next(iter(config.workspaces.values()))
    workspace.gws_profile = "acme"

    body = client.post(
        "/api/integrations/gws/profiles/remove", json={"profile": "acme"}
    ).json()

    assert body["profiles"] == []
    assert config.workspaces[workspace.name].gws_profile == ""


def test_gws_profile_add_rejects_an_unusable_name(tmp_path, monkeypatch):
    from ciao.web import routes_api

    monkeypatch.setattr(routes_api, "resolve_tool", lambda name: "")
    client, _config, _pcm = _client(tmp_path)

    resp = client.post("/api/integrations/gws/profiles/add", json={"name": "///"})

    assert resp.status_code == 400


def test_gws_profile_add_rejects_gws_service_names(tmp_path, monkeypatch):
    from ciao.web import routes_api

    monkeypatch.setattr(routes_api, "resolve_tool", lambda name: "")
    client, _config, _pcm = _client(tmp_path)

    service_names = (
        "Gmail",
        "calendar",
        "Drive",
        "docs",
        "sheets",
        "slides",
        "tasks",
        "contacts",
        "forms",
        "auth",
    )
    for name in service_names:
        resp = client.post("/api/integrations/gws/profiles/add", json={"name": name})

        assert resp.status_code == 400
        assert "reserved" in resp.json()["error"]


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


def test_gws_exchange_refreshes_health_monitor(tmp_path, monkeypatch):
    """A successful code exchange must trigger GwsHealthMonitor.check_once
    so the Settings UI clears the 'Login expired' banner immediately
    instead of waiting for the next periodic check.
    """
    from ciao.web import routes_api

    client, _config, _pcm = _client(tmp_path)

    # Seed a client_secret so load_client_secret succeeds.
    config_dir = routes_api._gws_profile_config_dir(_config, "personal")
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "client_secret.json").write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "cid",
                    "client_secret": "csecret",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )

    # Replace the network-bound exchange with a no-op so the test stays offline.
    import ciao.gws_auth
    monkeypatch.setattr(
        ciao.gws_auth, "exchange_and_store",
        lambda config, profile, *, code, redirect_uri: {"ok": True, "email": "x@y"},
    )

    class _FakeMonitor:
        def __init__(self) -> None:
            self.calls = 0

        def check_once(self) -> None:
            self.calls += 1

    monitor = _FakeMonitor()
    client.app.state.gws_health_monitor = monitor

    resp = client.post(
        "/api/integrations/gws/exchange",
        json={"profile": "personal", "code": "test-code"},
    )

    assert resp.status_code == 200, resp.text
    assert monitor.calls == 1

    # If the exchange itself raises, the health monitor must NOT be called.
    def boom(*args, **kwargs):
        raise ValueError("simulated exchange failure")

    monkeypatch.setattr(
        ciao.gws_auth, "exchange_and_store", boom
    )

    resp = client.post(
        "/api/integrations/gws/exchange",
        json={"profile": "personal", "code": "test-code"},
    )
    assert resp.status_code == 400
    assert monitor.calls == 1  # unchanged after failed exchange

    # If the health monitor itself raises, the exchange still succeeds and
    # the response is returned (best-effort refresh, never blocks the user).
    def loud(_self) -> None:
        raise RuntimeError("monitor blew up")

    monitor_cls = type(monitor)
    monkeypatch.setattr(monitor_cls, "check_once", loud)
    monkeypatch.setattr(
        ciao.gws_auth, "exchange_and_store",
        lambda config, profile, *, code, redirect_uri: {"ok": True, "email": "x@y"},
    )

    resp = client.post(
        "/api/integrations/gws/exchange",
        json={"profile": "personal", "code": "test-code"},
    )
    assert resp.status_code == 200


def test_gws_profile_payload_uses_granted_scopes_for_chips_and_purpose(tmp_path):
    client, config, _ = _client(tmp_path)
    config_dir = config.workspace_root / "secrets" / "gws-personal"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "credentials.json").write_text(
        json.dumps(
            {
                "client_id": "cid",
                "client_secret": "csecret",
                "refresh_token": "rtok",
                "type": "authorized_user",
                "email": "me@example.com",
                "scopes": [
                    "https://www.googleapis.com/auth/gmail.modify",
                    "https://www.googleapis.com/auth/calendar",
                    "https://www.googleapis.com/auth/drive",
                ],
            }
        ),
        encoding="utf-8",
    )

    resp = client.get("/api/integrations/gws")
    assert resp.status_code == 200
    payload = resp.json()
    personal = next(p for p in payload["profiles"] if p["name"] == "personal")
    assert personal["examples"] == ["Gmail", "Calendar", "Drive"]
    assert "Gmail" in personal["purpose"]
    assert "Calendar" in personal["purpose"]
    assert "Drive" in personal["purpose"]
    # Singular list of one scope collapses to a single-clause sentence.
    (config_dir / "credentials.json").write_text(
        json.dumps(
            {
                "client_id": "cid",
                "client_secret": "csecret",
                "refresh_token": "rtok",
                "type": "authorized_user",
                "scopes": ["https://www.googleapis.com/auth/tasks"],
            }
        ),
        encoding="utf-8",
    )
    resp = client.get("/api/integrations/gws")
    personal = next(p for p in resp.json()["profiles"] if p["name"] == "personal")
    assert personal["examples"] == ["Tasks"]
    assert personal["purpose"].endswith("Connected to Tasks.")


def test_gws_profile_payload_never_shows_a_raw_scope_url(tmp_path):
    """Feed the payload the scopes production actually requests.

    The other scope tests hand-pick URLs that happen to be in the label
    catalogue, so they pass whether or not the catalogue is complete. Every
    profile also requests `openid` and the two `userinfo.*` scopes, which had
    no labels and fell through as verbatim googleapis.com URLs in the sentence
    shown under the account name. Drive this from `scopes_for_profile` so
    adding a scope without naming it fails here.
    """
    from ciao.gws_auth import scopes_for_profile

    client, config, _ = _client(tmp_path)
    config_dir = config.workspace_root / "secrets" / "gws-personal"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "credentials.json").write_text(
        json.dumps(
            {
                "client_id": "cid",
                "client_secret": "csecret",
                "refresh_token": "rtok",
                "type": "authorized_user",
                "scopes": scopes_for_profile("personal"),
            }
        ),
        encoding="utf-8",
    )

    resp = client.get("/api/integrations/gws")
    assert resp.status_code == 200
    personal = next(p for p in resp.json()["profiles"] if p["name"] == "personal")

    assert not any("googleapis.com" in chip for chip in personal["examples"])
    assert "googleapis.com" not in personal["purpose"]
    assert "openid" not in personal["purpose"]
    # The services the user actually recognises still appear.
    assert "Gmail" in personal["examples"]
    assert "Forms" in personal["examples"]


def test_gws_profile_payload_falls_back_to_static_meta_when_no_scopes(tmp_path):
    client, config, _ = _client(tmp_path)
    client.post("/api/integrations/gws/profiles/add", json={"name": "personal"})
    # No credentials.json on disk -> configured is false, but the static
    # purpose should still be sent so the user sees something before connecting.
    resp = client.get("/api/integrations/gws")
    payload = resp.json()
    personal = next(p for p in payload["profiles"] if p["name"] == "personal")
    assert personal["configured"] is False
    # The curated chips describe what the profile is for, before there is any
    # connection to report.
    assert personal["examples"] == ["Gmail", "Calendar", "Tasks"]
    assert "Private Google account" in personal["purpose"]


def test_gws_profile_payload_keeps_chips_for_a_connection_predating_scopes(tmp_path):
    """An account connected before scopes were recorded must not lose its chips.

    credentials.json written by an older release has no `scopes` key, and
    re-running OAuth consent is the only way to add one. Deriving chips purely
    from granted scopes therefore blanked the card for every already-connected
    user on upgrade.
    """
    client, config, _ = _client(tmp_path)
    # `work` maps to secrets/gws, not secrets/gws-work (gws_auth.profile_config_dir).
    config_dir = config.workspace_root / "secrets" / "gws"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "credentials.json").write_text(
        json.dumps(
            {
                "client_id": "cid",
                "client_secret": "csecret",
                "refresh_token": "rtok",
                "type": "authorized_user",
                "email": "me@work.example",
            }
        ),
        encoding="utf-8",
    )

    resp = client.get("/api/integrations/gws")
    work = next(p for p in resp.json()["profiles"] if p["name"] == "work")
    assert work["configured"] is True
    assert work["examples"] == ["Drive", "Docs", "Sheets", "Slides", "Gmail", "Calendar"]
    assert "Company Google account" in work["purpose"]


def test_gws_personal_purpose_keeps_the_separation_warning_once_connected(tmp_path):
    """The "keep this separate" guidance matters most after connecting.

    Recomposing the sentence from the label alone dropped it exactly when the
    account went live, which is the only moment it is actionable.
    """
    client, config, _ = _client(tmp_path)
    config_dir = config.workspace_root / "secrets" / "gws-personal"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "credentials.json").write_text(
        json.dumps(
            {
                "client_id": "cid",
                "client_secret": "csecret",
                "refresh_token": "rtok",
                "type": "authorized_user",
                "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
            }
        ),
        encoding="utf-8",
    )

    resp = client.get("/api/integrations/gws")
    personal = next(p for p in resp.json()["profiles"] if p["name"] == "personal")
    assert "Keep this separate from company systems." in personal["purpose"]
    assert personal["purpose"].endswith("Connected to Gmail.")
