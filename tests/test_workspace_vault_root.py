"""Workspace names belong to the user, so nothing may key on a literal.

`personal` and `work` are the names the first release happened to ship. Vault
resolution special-cased exactly those two, so a workspace named anything else
had its vault placed next to `memory-vault/` instead of inside it — invisible to
the vault index, the linter, and the memory-proposal scans.
"""

from __future__ import annotations

from pathlib import Path

from ciao.config import CiaoConfig, WorkspaceConfig


def _config(tmp_path: Path, workspaces: dict[str, str]) -> CiaoConfig:
    """Config whose workspaces map name -> stored vault_root."""
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=root)
            for name, root in workspaces.items()
        },
    )


def test_bare_name_nests_under_the_vault_for_any_workspace(tmp_path: Path) -> None:
    config = _config(tmp_path, {"personal": "personal", "research": "research"})

    assert config.workspace_vault_root("personal") == tmp_path / "memory-vault" / "personal"
    # The whole point: a custom name is treated exactly like the built-in ones.
    assert config.workspace_vault_root("research") == tmp_path / "memory-vault" / "research"


def test_a_legacy_sibling_vault_is_pinned_into_the_registry(tmp_path: Path) -> None:
    """An install created before the fix must not appear to lose its vault.

    The location is pinned once, at load, so resolution stays pure — deciding
    per call from live filesystem state meant the vault silently relocated the
    moment the other candidate path appeared.
    """
    legacy = tmp_path / "research"
    (legacy / "Workspace").mkdir(parents=True)
    config = _config(tmp_path, {"research": "research"})

    assert config.workspace_vault_root("research") == legacy
    # Pinned as an absolute path in the registry, not re-derived.
    assert config.workspace("research").vault_root == str(legacy)

    # The nested path appearing later must not move the workspace off its data.
    (tmp_path / "memory-vault" / "research").mkdir(parents=True)
    assert config.workspace_vault_root("research") == legacy


def test_an_unrelated_sibling_directory_is_not_adopted_as_a_vault(tmp_path: Path) -> None:
    """Gating on mere existence would capture someone's document folder."""
    (tmp_path / "clients").mkdir()
    config = _config(tmp_path, {"clients": "clients"})

    assert config.workspace_vault_root("clients") == tmp_path / "memory-vault" / "clients"


def test_equivalent_path_spellings_resolve_alike(tmp_path: Path) -> None:
    """`research/` and `./research` name one segment, like `research`."""
    config = _config(tmp_path, {"a": "research", "b": "research/", "c": "./research"})
    expected = tmp_path / "memory-vault" / "research"

    assert config.workspace_vault_root("a") == expected
    assert config.workspace_vault_root("b") == expected
    assert config.workspace_vault_root("c") == expected


def test_an_explicit_relative_root_is_not_nested_twice(tmp_path: Path) -> None:
    """Setup writes `memory-vault/<name>` when it adopts a nested workspace."""
    config = _config(tmp_path, {"clientA": "memory-vault/clientA"})

    assert (
        config.workspace_vault_root("clientA") == tmp_path / "memory-vault" / "clientA"
    )


def test_an_absolute_root_is_used_as_given(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere" / "vault"
    config = _config(tmp_path, {"external": str(elsewhere)})

    assert config.workspace_vault_root("external") == elsewhere


def test_an_unregistered_workspace_still_resolves(tmp_path: Path) -> None:
    """A stale reference should not crash a scan that walks every name."""
    config = _config(tmp_path, {"personal": "personal"})

    assert config.workspace_vault_root("gone") == tmp_path / "memory-vault" / "gone"


def test_primary_workspace_prefers_personal_then_falls_back(tmp_path: Path) -> None:
    assert _config(tmp_path, {"work": "work", "personal": "personal"}).primary_workspace() == "personal"
    # No workspace named personal: use whatever is registered first, rather than
    # returning a name that does not exist.
    assert _config(tmp_path, {"research": "research", "family": "family"}).primary_workspace() == "research"


def test_model_bucket_fallback_does_not_key_on_the_name_work(tmp_path: Path) -> None:
    """An unregistered name has no bucket of its own; don't guess from the name."""
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            "research": WorkspaceConfig(
                name="research", vault_root="research", model_bucket="openrouter"
            ),
        },
    )

    assert config.model_bucket_for_workspace("research") == "openrouter"
    # Previously any name but the literal "work" fell through to "personal".
    assert config.model_bucket_for_workspace("unknown") == "openrouter"


def test_upgrade_notice_reports_a_vault_left_outside_the_vault_root(tmp_path: Path) -> None:
    """The install tells the operator, instead of a release note hoping they read it."""
    from ciao.os_audit import audit_upgrade_notices

    legacy = tmp_path / "research"
    (legacy / "Workspace").mkdir(parents=True)
    config = _config(tmp_path, {"research": "research"})

    result = audit_upgrade_notices(config)

    assert result["notices_found"] == 1
    notice = result["notices"][0]
    assert notice["type"] == "vault_outside_vault_root"
    assert notice["workspace"] == "research"
    # The remedy has to be the actual command, not advice to go read something.
    assert str(legacy) in notice["remedy"]
    assert str(tmp_path / "memory-vault" / "research") in notice["remedy"]


def test_upgrade_notices_stay_quiet_for_a_correctly_placed_vault(tmp_path: Path) -> None:
    from ciao.os_audit import audit_upgrade_notices

    (tmp_path / "memory-vault" / "research" / "Workspace").mkdir(parents=True)
    config = _config(tmp_path, {"research": "memory-vault/research"})

    assert audit_upgrade_notices(config)["notices_found"] == 0


def test_upgrade_notices_tolerate_a_config_without_a_registry() -> None:
    """Advisory only: a stub config must not turn the audit red."""
    from ciao.os_audit import audit_upgrade_notices

    assert audit_upgrade_notices(None)["notices_found"] == 0
    assert audit_upgrade_notices(object())["errors"] == []
