"""Tests for the per-device config knobs (workspaces, vault layout)."""

from __future__ import annotations

import json
from pathlib import Path

from ciao.config import CiaoConfig, _DEFAULT_HARNESS_DISALLOWED_TOOLS
from ciao.execution_modes import HARNESS_DISABLED_SKILLS, harness_skill_overrides


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
    # "auto" was the user-facing A/B benchmark option; removed with the
    # benchmark, so the env value now falls back to the mcp default.
    config = _config(CIAO_WORKSPACE=str(tmp_path), CIAO_CONTROL_SURFACE="auto")
    assert config.control_surface == "mcp"


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


def test_insights_min_turns_defaults_to_multiturn(tmp_path: Path) -> None:
    config = _config(CIAO_WORKSPACE=str(tmp_path))
    assert config.insights_size_gate_turns == 2

    overridden = _config(
        CIAO_WORKSPACE=str(tmp_path), CIAO_INSIGHTS_MIN_TURNS="4"
    )
    assert overridden.insights_size_gate_turns == 4


def test_the_bootstrap_registry_is_read_off_the_vault(tmp_path: Path) -> None:
    """An install with no `workspaces.json` gets one workspace per vault folder.

    It used to manufacture `personal` AND `work` unconditionally, plus four
    `*_PERSONAL`/`*_WORK` env vars to configure them. Both are gone: a registered
    workspace with no vault directory makes the re-rooting plan refuse, so the
    phantom entry left such an install permanently unable to migrate. Per-workspace
    model and tool settings live in `workspaces.json`, which works for any name.

    CIAO_RUNTIME_ROOT must be explicit: with bootstrap_mode False (a token and
    CIAO_WORKSPACE are both set here), from_env resolves the default ".runtime"
    relative to the CWD, not tmp_path — so without this the test silently reads
    whatever real workspaces.json exists in the checkout the tests run from.
    """
    for name in ("personal", "work"):
        (tmp_path / "memory-vault" / name / "People").mkdir(parents=True)

    config = _config(
        CIAO_WORKSPACE=str(tmp_path),
        CIAO_RUNTIME_ROOT=str(tmp_path / ".runtime"),
    )

    assert list(config.workspaces) == ["personal", "work"]
    assert config.workspace("personal").vault_root == "memory-vault/personal"
    assert config.workspace("work").gws_profile == "work"


def test_the_bootstrap_registry_claims_only_what_exists(tmp_path: Path) -> None:
    """A vault with one workspace folder yields one workspace, not two."""
    (tmp_path / "memory-vault" / "personal" / "People").mkdir(parents=True)

    config = _config(
        CIAO_WORKSPACE=str(tmp_path),
        CIAO_RUNTIME_ROOT=str(tmp_path / ".runtime"),
    )

    assert list(config.workspaces) == ["personal"]


def test_a_note_folder_is_not_mistaken_for_a_workspace(tmp_path: Path) -> None:
    """A single-workspace vault keeps its notes directly under `People/`.

    The evidence test is nested for exactly this: `memory-vault/personal/People/`
    makes `personal` a workspace, while `memory-vault/People/` is a note folder
    and must not become a workspace called "People".
    """
    (tmp_path / "memory-vault" / "People").mkdir(parents=True)
    (tmp_path / "memory-vault" / "People" / "Sam.md").write_text(
        "---\ntype: person\n---\n# Sam\n", encoding="utf-8"
    )

    config = _config(
        CIAO_WORKSPACE=str(tmp_path),
        CIAO_RUNTIME_ROOT=str(tmp_path / ".runtime"),
    )

    assert list(config.workspaces) == ["personal"]

def test_ciao_workspaces_json_defines_named_workspaces(tmp_path: Path) -> None:
    raw = json.dumps(
        [
            {
                "name": "home",
                "vault_root": "memory-vault/home",
                "default_model": "haiku",
                "disallowed_tools": ["Bash", "mcp__example"],
                "gws_profile": "personal",
            },
            {
                "name": "client",
                "vault_root": "/tmp/client-vault",
                "default_model": "opus",
                "gws_profile": "work",
            },
        ]
    )

    config = _config(CIAO_WORKSPACE=str(tmp_path), CIAO_WORKSPACES=raw)

    assert list(config.workspaces) == ["home", "client"]
    assert config.workspace_names() == ["home", "client"]
    assert config.workspace("home").vault_root == "memory-vault/home"
    assert config.workspace("home").gws_profile == "personal"
    assert config.default_model_for_workspace("home") == "haiku"
    assert config.disallowed_tools_for_workspace("home") == ["Bash", "mcp__example"]
    assert config.default_model_for_workspace("client") == "opus"
    # A workspace with no explicit denylist gets the same defaults whatever it
    # is named — no branch on "personal"/"work" anywhere.
    assert config.disallowed_tools_for_workspace("client") == list(
        _DEFAULT_HARNESS_DISALLOWED_TOOLS
    )


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
    assert config.workspace("default").vault_root == str(
        tmp_path / "memory-vault"
    )
    assert config.workspace_vault_root("default") == tmp_path / "memory-vault"
    assert config.default_model_for_workspace("default") == "haiku"


def test_unknown_workspace_uses_global_defaults(tmp_path: Path) -> None:
    config = _config(CIAO_WORKSPACE=str(tmp_path), CLAUDE_MODELS="sonnet,haiku")

    assert config.workspace("missing") is None
    assert config.default_model_for_workspace("missing") == "sonnet"
    # A stale or renamed workspace name still gets the harness denies. Returning
    # [] made it the one input that reached the model with nothing denied.
    assert config.disallowed_tools_for_workspace("missing") == list(
        _DEFAULT_HARNESS_DISALLOWED_TOOLS
    )


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


def test_password_protection_is_on_by_default(tmp_path: Path) -> None:
    """A configured workspace with a token is protected without asking: the
    token is the password, so an .env written before the default flipped (no
    PWA_AUTH_REQUIRED line) still ends up protected."""
    config = CiaoConfig.from_env(
        {"CIAO_WORKSPACE": str(tmp_path), "PWA_AUTH_TOKEN": "hunter2"}
    )

    assert config.pwa_auth_required is True
    assert config.bootstrap_mode is False


def test_password_protection_can_be_opted_out_in_env(tmp_path: Path) -> None:
    config = CiaoConfig.from_env(
        {
            "CIAO_WORKSPACE": str(tmp_path),
            "PWA_AUTH_TOKEN": "hunter2",
            "PWA_AUTH_REQUIRED": "false",
        }
    )

    assert config.pwa_auth_required is False


def test_missing_token_without_auth_persists_random_secret_not_a_constant(tmp_path: Path) -> None:
    env = {"CIAO_WORKSPACE": str(tmp_path)}  # no PWA_AUTH_TOKEN

    config = CiaoConfig.from_env(env)

    # Nothing a human could type exists yet, so enforcing would lock the owner
    # out of their own install: protection waits for a password.
    assert config.pwa_auth_required is False
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


def test_harness_denylist_covers_superseded_bundled_skills() -> None:
    """The `Skill(...)` deny entries must stay in step with the skills hidden
    by the `skillOverrides` layer. Hiding without denying leaves an execution
    path open if a downstream settings file re-enables the skill; denying
    without hiding is what let the model pick `Skill(schedule)` in the first
    place."""
    entries = set(_DEFAULT_HARNESS_DISALLOWED_TOOLS)
    for name in HARNESS_DISABLED_SKILLS:
        assert f"Skill({name})" in entries
    assert harness_skill_overrides() == {name: "off" for name in HARNESS_DISABLED_SKILLS}
