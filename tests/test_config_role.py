"""Tests for the per-device config knobs (workspaces, vault layout)."""

from __future__ import annotations

import json
from pathlib import Path

from ciao.config import CiaoConfig


def _config(**overrides: str) -> CiaoConfig:
    env = {"PWA_AUTH_TOKEN": "test-token"}
    env.update(overrides)
    return CiaoConfig.from_env(env)


def test_control_surface_defaults_to_mcp(tmp_path: Path) -> None:
    config = _config(CIAO_WORKSPACE=str(tmp_path))
    assert config.control_surface == "mcp"


def test_control_surface_env_override(tmp_path: Path) -> None:
    config = _config(CIAO_WORKSPACE=str(tmp_path), CIAO_CONTROL_SURFACE="legacy")
    assert config.control_surface == "legacy"
    config = _config(CIAO_WORKSPACE=str(tmp_path), CIAO_CONTROL_SURFACE="auto")
    assert config.control_surface == "auto"


def test_control_surface_invalid_falls_back_to_mcp(tmp_path: Path) -> None:
    config = _config(CIAO_WORKSPACE=str(tmp_path), CIAO_CONTROL_SURFACE="bogus")
    assert config.control_surface == "mcp"


def test_vault_root_defaults_under_workspace_root(tmp_path: Path) -> None:
    config = _config(CIAO_WORKSPACE=str(tmp_path))
    assert config.workspace_root == tmp_path.resolve()
    assert config.vault_root == (tmp_path / "memory-vault").resolve()


def test_vault_root_accepts_absolute_external_notes_folder(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    config = _config(CIAO_WORKSPACE=str(tmp_path / "ops"), CIAO_VAULT_ROOT=str(notes))
    assert config.workspace_root == (tmp_path / "ops").resolve()
    assert config.vault_root == notes.resolve()


def test_vault_root_relative_override_is_workspace_relative(tmp_path: Path) -> None:
    config = _config(CIAO_WORKSPACE=str(tmp_path), CIAO_VAULT_ROOT="notes")
    assert config.vault_root == (tmp_path / "notes").resolve()


def test_legacy_workspaces_are_exposed_as_workspace_configs(tmp_path: Path) -> None:
    config = _config(
        CIAO_WORKSPACE=str(tmp_path),
        CLAUDE_DEFAULT_MODEL_PERSONAL="deepseek-v4-flash:cloud",
        CLAUDE_DEFAULT_MODEL_WORK="opus",
        CIAO_DISALLOWED_TOOLS_WORK="Bash",
    )

    assert list(config.workspaces) == ["personal", "work"]
    assert config.workspace("personal").vault_root == "personal"
    assert config.workspace("personal").model_bucket == "personal"
    assert config.workspace("work").gws_profile == "work"
    assert config.default_model_for_workspace("personal") == "deepseek-v4-flash:cloud"
    assert config.default_model_for_workspace("work") == "opus"
    assert config.disallowed_tools_for_workspace("work") == ["Bash"]


def test_ciao_workspaces_json_defines_named_workspaces(tmp_path: Path) -> None:
    raw = json.dumps(
        [
            {
                "name": "home",
                "vault_root": "memory-vault/home",
                "default_model": "haiku",
                "disallowed_tools": ["Bash", "mcp__example"],
                "gws_profile": "personal",
                "model_bucket": "ollama",
            },
            {
                "name": "client",
                "vault_root": "/tmp/client-vault",
                "default_model": "opus",
                "gws_profile": "work",
                "model_bucket": "anthropic",
            },
        ]
    )

    config = _config(CIAO_WORKSPACE=str(tmp_path), CIAO_WORKSPACES=raw)

    assert list(config.workspaces) == ["home", "client"]
    assert config.workspace_names() == ["home", "client"]
    assert config.workspace("home").vault_root == "memory-vault/home"
    assert config.workspace("home").gws_profile == "personal"
    assert config.workspace("home").model_bucket == "ollama"
    assert config.default_model_for_workspace("home") == "haiku"
    assert config.disallowed_tools_for_workspace("home") == ["Bash", "mcp__example"]
    assert config.default_model_for_workspace("client") == "opus"
    assert config.disallowed_tools_for_workspace("client") == []


def test_runtime_workspaces_json_is_used_when_env_is_absent(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "workspaces.json").write_text(
        json.dumps(
            [
                {
                    "name": "default",
                    "vault_root": "memory-vault",
                    "default_model": "haiku",
                    "model_bucket": "anthropic",
                }
            ]
        ),
        encoding="utf-8",
    )

    config = _config(
        CIAO_WORKSPACE=str(tmp_path),
        CIAO_RUNTIME_ROOT=str(runtime),
    )

    assert config.workspace_names() == ["default"]
    assert config.workspace("default").vault_root == "memory-vault"
    assert config.default_model_for_workspace("default") == "haiku"


def test_unknown_workspace_uses_global_defaults(tmp_path: Path) -> None:
    config = _config(CIAO_WORKSPACE=str(tmp_path), CLAUDE_MODELS="sonnet,haiku")

    assert config.workspace("missing") is None
    assert config.default_model_for_workspace("missing") == "sonnet"
    assert config.disallowed_tools_for_workspace("missing") == []


def test_missing_auth_token_enters_bootstrap_mode_with_persisted_token(tmp_path: Path) -> None:
    bootstrap = tmp_path / "bootstrap"

    config = CiaoConfig.from_env(
        {
            "CIAO_BOOTSTRAP_WORKSPACE": str(bootstrap),
            "CIAO_PUSH_CONTACT": "",
        }
    )

    assert config.bootstrap_mode is True
    assert config.workspace_root == bootstrap.resolve()
    assert config.state_path == (bootstrap / ".runtime" / "state.json").resolve()
    assert config.vault_root == (bootstrap / "memory-vault").resolve()
    token_path = bootstrap / ".runtime" / "bootstrap-auth-token"
    assert token_path.read_text(encoding="utf-8").strip() == config.pwa_auth_token
    assert len(config.pwa_auth_token) >= 32

    restarted = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": str(bootstrap)})
    assert restarted.bootstrap_mode is True
    assert restarted.pwa_auth_token == config.pwa_auth_token


def test_missing_token_without_auth_persists_random_secret_not_a_constant(tmp_path: Path) -> None:
    env = {"CIAO_WORKSPACE": str(tmp_path)}  # no PWA_AUTH_TOKEN, auth not required

    config = CiaoConfig.from_env(env)

    assert config.bootstrap_mode is False
    assert config.pwa_auth_token != "ciao-insecure-fallback-secret-key"
    assert len(config.pwa_auth_token) >= 32
    secret_path = tmp_path / ".runtime" / "session-secret"
    assert secret_path.read_text(encoding="utf-8").strip() == config.pwa_auth_token

    # Secret is stable across restarts, not regenerated each load.
    restarted = CiaoConfig.from_env(env)
    assert restarted.pwa_auth_token == config.pwa_auth_token


def test_fallback_secret_constant_is_gone_from_source() -> None:
    source = Path(__file__).parents[1] / "ciao" / "config.py"
    assert "ciao-insecure-fallback-secret-key" not in source.read_text(encoding="utf-8")


def test_explicit_auth_token_stays_out_of_bootstrap_mode(tmp_path: Path) -> None:
    config = _config(CIAO_WORKSPACE=str(tmp_path))

    assert config.bootstrap_mode is False
