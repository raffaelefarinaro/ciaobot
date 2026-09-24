"""Tests for the unconditional credential / runtime-state path denies.

`<workspace_root>/.env` holds `PWA_AUTH_TOKEN`, which `POST /api/auth/login`
trades for an owner session cookie — strictly wider authority than the
chat-scoped MCP bearer token the same agent process holds. `.runtime/` holds
the stores the control plane guards behind `runtime_file_forbidden`, and
`secrets/` holds OAuth material. Both providers deny their native file tools
these paths, and no workspace override clears them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao.config import CiaoConfig
from ciao.execution_modes import (
    CLAUDE_CREDENTIAL_DENY_TOOLS,
    CREDENTIAL_DENY_PATTERNS,
    OPENCODE_CREDENTIAL_DENY_PERMISSIONS,
    credential_path_deny_rules,
    opencode_credential_deny_rules,
)
from ciao.providers.opencode import mode_settings


def _config(tmp_path: Path, **overrides: object) -> CiaoConfig:
    env: dict[str, str] = {
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
    }
    for key, value in overrides.items():
        env[key] = str(value)
    return CiaoConfig.from_env(env)


def test_every_protected_path_is_denied_to_every_claude_file_tool() -> None:
    rules = set(credential_path_deny_rules())
    for tool in CLAUDE_CREDENTIAL_DENY_TOOLS:
        for pattern in CREDENTIAL_DENY_PATTERNS:
            assert f"{tool}({pattern})" in rules


def test_the_dotenv_and_runtime_and_secrets_paths_are_all_covered() -> None:
    """Named explicitly so dropping one from the tuple fails here, not in prod."""
    assert "**/.env" in CREDENTIAL_DENY_PATTERNS
    assert "**/.runtime/**" in CREDENTIAL_DENY_PATTERNS
    assert "**/secrets/**" in CREDENTIAL_DENY_PATTERNS


def test_env_templates_are_not_denied() -> None:
    """A template is not a secret — the convention this codebase already keeps.

    `local_session.py` and `public_release.py` both
    carve out `.env.example` / `.sample` / `.template` / `.schema`, and this
    repo ships `.env.example`. A blanket `**/.env.*` would contradict that with
    no operator opt-out, since these rules sit outside the workspace extras.
    """
    assert "**/.env.*" not in CREDENTIAL_DENY_PATTERNS
    assert not any(".env.*" in pattern for pattern in CREDENTIAL_DENY_PATTERNS)


def test_a_relocated_runtime_root_is_denied_by_path(tmp_path: Path) -> None:
    """`CIAO_RUNTIME_ROOT` can leave the tree, where `**/.runtime/**` misses it."""
    moved = tmp_path / "var" / "state"
    rules = credential_path_deny_rules(moved)

    assert f"Read({moved})" in rules
    assert f"Write({moved}/**)" in rules
    # The name-based patterns stay, for every caller that cannot resolve a root.
    assert "Read(**/.runtime/**)" in rules


def test_no_runtime_root_leaves_only_the_name_based_patterns() -> None:
    assert credential_path_deny_rules(None) == credential_path_deny_rules()
    assert credential_path_deny_rules("") == credential_path_deny_rules()


def test_a_relocated_runtime_root_reaches_opencode_too(tmp_path: Path) -> None:
    moved = tmp_path / "var" / "state"
    patterns = {rule["resource"] for rule in opencode_credential_deny_rules(moved)}

    assert str(moved) in patterns
    assert f"{moved}/**" in patterns


def test_the_configured_runtime_root_is_denied_for_a_workspace(tmp_path: Path) -> None:
    """End to end: a non-default `CIAO_RUNTIME_ROOT` lands in the denylist."""
    moved = tmp_path / "var" / "state"
    config = _config(tmp_path, CIAO_RUNTIME_ROOT=moved)

    tools = config.disallowed_tools_for_workspace("personal")
    assert f"Read({moved})" in tools
    assert f"Edit({moved}/**)" in tools


def test_a_default_workspace_gets_the_credential_denies(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tools = config.disallowed_tools_for_workspace("personal")
    for rule in credential_path_deny_rules():
        assert rule in tools


def test_an_unregistered_workspace_still_gets_them(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tools = config.disallowed_tools_for_workspace("no-such-workspace")
    assert "Read(**/.env)" in tools


def test_a_custom_workspace_denylist_does_not_drop_them(tmp_path: Path) -> None:
    """`disallowed_tools` replaces the harness defaults; it must not replace these."""
    config = _config(tmp_path)
    workspace = config.workspace("personal")
    assert workspace is not None
    workspace.disallowed_tools = ["SomethingElse"]

    tools = config.disallowed_tools_for_workspace("personal")
    assert "SomethingElse" in tools
    assert "Read(**/.env)" in tools


def test_the_none_opt_out_does_not_unlock_the_credentials(tmp_path: Path) -> None:
    """`none` is the documented "deny nothing" escape hatch for the harness set."""
    config = _config(tmp_path)
    workspace = config.workspace("personal")
    assert workspace is not None
    workspace.disallowed_tools = []

    tools = config.disallowed_tools_for_workspace("personal")
    assert "EnterPlanMode" not in tools
    assert "Read(**/.env)" in tools
    assert "Write(**/.runtime/**)" in tools


@pytest.mark.parametrize("mode", ["plan", "normal", "auto", "bypass"])
def test_every_opencode_mode_carries_the_denies(mode: str) -> None:
    _agent, rules = mode_settings(mode)  # type: ignore[arg-type]
    for expected in opencode_credential_deny_rules():
        assert expected in rules


@pytest.mark.parametrize("mode", ["auto", "bypass"])
def test_the_denies_come_after_the_wildcard_allow(mode: str) -> None:
    """opencode resolves last-match-wins, so order is the whole mechanism.

    `auto` and `bypass` both open with a wildcard `allow`; a deny placed before
    it would be overridden and this protection would silently do nothing.
    """
    _agent, rules = mode_settings(mode)  # type: ignore[arg-type]
    wildcard_allow = next(
        i
        for i, rule in enumerate(rules)
        if rule["resource"] == "*" and rule["action"] == "*" and rule["effect"] == "allow"
    )
    first_deny = next(i for i, rule in enumerate(rules) if rule["effect"] == "deny")
    assert first_deny > wildcard_allow


def test_the_opencode_denies_cover_the_same_paths_as_claude() -> None:
    """One source of truth: the two providers cannot drift apart on coverage."""
    rules = opencode_credential_deny_rules()
    assert {rule["resource"] for rule in rules} == set(CREDENTIAL_DENY_PATTERNS)
    assert {rule["action"] for rule in rules} == set(
        OPENCODE_CREDENTIAL_DENY_PERMISSIONS
    )
    assert all(rule["effect"] == "deny" for rule in rules)


def test_shell_is_deliberately_not_claimed_to_be_covered() -> None:
    """The denies are path globs; no shell rule can be path-scoped."""
    assert "shell" not in OPENCODE_CREDENTIAL_DENY_PERMISSIONS
    assert not any(rule.startswith("Bash(") for rule in credential_path_deny_rules())
