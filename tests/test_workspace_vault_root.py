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


def test_a_legacy_sibling_vault_keeps_resolving_where_its_data_is(tmp_path: Path) -> None:
    """An install created before the fix must not appear to lose its vault."""
    legacy = tmp_path / "research"
    (legacy / "Workspace").mkdir(parents=True)
    config = _config(tmp_path, {"research": "research"})

    assert config.workspace_vault_root("research") == legacy

    # Once the correct location exists it wins, so a migrated install moves on.
    (tmp_path / "memory-vault" / "research").mkdir(parents=True)
    assert (
        config.workspace_vault_root("research")
        == tmp_path / "memory-vault" / "research"
    )


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
