"""Workspace names belong to the user, so nothing may key on a literal.

`personal` and `work` are the names the first release happened to ship. Vault
resolution special-cased exactly those two, so a workspace named anything else
had its vault placed next to `memory-vault/` instead of inside it — invisible to
the vault index, the linter, and the memory-proposal scans.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao.config import CiaoConfig, WorkspaceConfig


def _config(tmp_path: Path, workspaces: dict[str, str]) -> CiaoConfig:
    """Config whose workspaces map name -> stored vault_root."""
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=root)
            for name, root in workspaces.items()
        },
    )
    from ciao.setup_marker import write_vault_index_ownership

    write_vault_index_ownership(
        tmp_path / ".runtime",
        [config.agent_vault_root(next(iter(workspaces)))],
        owner="ciao",
    )
    return config


def test_bare_name_nests_under_the_vault_for_any_workspace(tmp_path: Path) -> None:
    config = _config(tmp_path, {"personal": "personal", "research": "research"})

    assert config.workspace_vault_root("personal") == tmp_path / "personal" / "memory-vault"
    # The whole point: a custom name is treated exactly like the built-in ones.
    assert config.workspace_vault_root("research") == tmp_path / "research" / "memory-vault"


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


def test_ciao_created_projects_are_enough_to_pin_a_legacy_vault(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "research"
    (legacy / "projects" / "active" / "general").mkdir(parents=True)
    config = _config(tmp_path, {"research": "research"})

    assert config.workspace_vault_root("research") == legacy


def test_a_stray_nested_proposal_folder_does_not_defeat_the_legacy_pin(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "research"
    (legacy / "projects" / "active" / "real-project").mkdir(parents=True)
    (tmp_path / "memory-vault" / "research" / "Workspace").mkdir(parents=True)

    config = _config(tmp_path, {"research": "research"})

    assert config.workspace_vault_root("research") == legacy


def test_an_unrelated_sibling_directory_is_not_adopted_as_a_vault(tmp_path: Path) -> None:
    """Gating on mere existence would capture someone's document folder."""
    (tmp_path / "clients").mkdir()
    config = _config(tmp_path, {"clients": "clients"})

    assert config.workspace_vault_root("clients") == tmp_path / "clients" / "memory-vault"


def test_one_segment_legacy_values_are_canonicalized_by_workspace_name(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, {"a": "research/"})

    assert config.workspace_vault_root("a") == tmp_path / "a" / "memory-vault"
    assert config.workspace("a").vault_root == "a/memory-vault"


@pytest.mark.parametrize(
    "raw_root",
    ["", "..", "../outside", "a/../b", "/"],
)
def test_unsafe_relative_root_is_replaced_with_the_standard_vault(
    tmp_path: Path,
    raw_root: str,
) -> None:
    config = _config(tmp_path, {"research": raw_root})

    assert (
        config.workspace_vault_root("research")
        == tmp_path / "research" / "memory-vault"
    )


def test_existing_folder_dot_root_remains_the_workspace_vault(
    tmp_path: Path,
) -> None:
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        vault_root=Path("."),
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            "research": WorkspaceConfig(name="research", vault_root="."),
        },
    )

    assert config.workspace_vault_root("research") == tmp_path
    assert config.workspace("research").vault_root == "."


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


def test_standard_root_rejects_a_symlink_added_after_config_load(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, {"research": "memory-vault/research"})
    personal = tmp_path / "memory-vault" / "personal"
    personal.mkdir(parents=True)
    research = tmp_path / "memory-vault" / "research"
    research.symlink_to(personal, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        config.workspace_vault_root("research")


def test_external_symlink_is_pinned_then_replacement_fails_closed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "external-target"
    target.mkdir()
    selected_alias = tmp_path / "selected-notes"
    selected_alias.symlink_to(target, target_is_directory=True)

    config = _config(tmp_path, {"external": str(selected_alias)})

    assert config.workspace("external").vault_root == str(target)
    assert config.workspace_vault_root("external") == target

    target.rmdir()
    redirected = tmp_path / "different-vault"
    redirected.mkdir()
    target.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        config.workspace_vault_root("external")


def test_relative_path_through_configured_vault_alias_is_pinned(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external-notes"
    external.mkdir()
    (tmp_path / "memory-vault").symlink_to(
        external,
        target_is_directory=True,
    )

    config = _config(tmp_path, {"research": "memory-vault/research"})

    expected = external / "research"
    assert config.workspace("research").vault_root == str(expected)
    assert config.workspace_vault_root("research") == expected


def test_an_unregistered_workspace_still_resolves(tmp_path: Path) -> None:
    """A stale reference should not crash a scan that walks every name."""
    config = _config(tmp_path, {"personal": "personal"})

    assert config.workspace_vault_root("gone") == tmp_path / "memory-vault" / "gone"


def test_primary_workspace_prefers_personal_then_falls_back(tmp_path: Path) -> None:
    assert _config(tmp_path, {"work": "work", "personal": "personal"}).primary_workspace() == "personal"
    # No workspace named personal: use whatever is registered first, rather than
    # returning a name that does not exist.
    assert _config(tmp_path, {"research": "research", "family": "family"}).primary_workspace() == "research"


def test_legacy_pin_persists_and_round_trips_across_restart(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    registry = runtime / "workspaces.json"
    registry.write_text(
        json.dumps([{"name": "research", "vault_root": "research"}]),
        encoding="utf-8",
    )
    legacy = tmp_path / "research"
    (legacy / "projects" / "active" / "general").mkdir(parents=True)

    source = {
        "PWA_AUTH_TOKEN": "test-token",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(runtime),
        "CIAO_OLLAMA_LOCAL_DISCOVERY": "0",
    }
    first = CiaoConfig.from_env(source)
    assert first._workspace_registry_changed is True
    first.persist_workspace_registry()

    # A stale nested Workspace folder appearing later cannot steal ownership.
    (tmp_path / "memory-vault" / "research" / "Workspace").mkdir(parents=True)
    second = CiaoConfig.from_env(source)
    assert second.workspace_vault_root("research") == legacy


# -- registered per-root ownership ---------------------------------------------


def _per_root_config(root: Path, workspaces: dict[str, str]) -> CiaoConfig:
    """A re-rooted install whose registry holds the given vault_root values."""
    runtime = root / ".runtime"
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=root,
        vault_root=root / "memory-vault",
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=raw)
            for name, raw in workspaces.items()
        },
    )
    from ciao.setup_marker import write_vault_index_ownership

    write_vault_index_ownership(
        runtime,
        [
            config.agent_vault_root(name)
            for name in workspaces
            if not config.adopted_workspace_vault(name)
        ],
        owner="ciao",
    )
    return config


def test_per_root_dot_agent_vault_matches_registered_root(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    config = _per_root_config(root, {"work": "."})

    assert config.workspace_vault_root("work") == root
    assert config.agent_vault_root("work") == root


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("work/brain", ("work", "brain")),
        ("work/memory-vault", ("work", "memory-vault")),
        ("/external/notes", ("external", "notes")),
    ],
)
def test_per_root_agent_vault_uses_explicit_adopted_root(
    tmp_path: Path, stored: str, expected: tuple[str, str]
) -> None:
    base = tmp_path.resolve()
    root = base / "install"
    if stored.startswith("/"):
        registered = base.joinpath(*expected)
        registered.mkdir(parents=True, exist_ok=True)
        stored = str(registered)
    else:
        registered = root.joinpath(*expected)
    config = _per_root_config(root, {"work": stored})

    assert config.workspace_vault_root("work") == registered
    assert config.agent_vault_root("work") == registered


def test_adopted_root_does_not_redefine_canonical_destination(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    adopted = root / "work" / "brain"
    config = _per_root_config(root, {"work": "work/brain"})

    assert config.workspace_vault_root("work") == adopted
    assert config.agent_vault_root("work") == adopted
    assert config.canonical_workspace_vault_root("work") == root / "work" / "memory-vault"


def test_per_root_registration_at_old_shared_subpath_keeps_derived_vault(
    tmp_path: Path,
) -> None:
    """A registration still at the pre-rerooting ``memory-vault/<name>`` is the
    superseded standard, not an adoption: ownership keeps the derived per-root
    location, while the notes resolver still reports the registered subpath."""
    root = tmp_path.resolve()
    config = _per_root_config(root, {"work": "memory-vault/work"})

    assert config.workspace_vault_root("work") == root / "memory-vault" / "work"
    assert config.agent_vault_root("work") == root / "work" / "memory-vault"
    assert config.canonical_workspace_vault_root("work") == root / "work" / "memory-vault"


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("work/memory-vault", False),
        ("memory-vault/work", False),
        ("work/brain", True),
        (".", True),
    ],
)
def test_adopted_workspace_vault_is_a_registered_nonstandard_per_root_vault(
    tmp_path: Path, stored: str, expected: bool
) -> None:
    root = tmp_path.resolve()
    config = _per_root_config(root, {"work": stored})

    assert config.adopted_workspace_vault("work") is expected
    assert config.adopted_workspace_vault("unregistered") is False
    assert config.ciao_owned_vault_index("work") is (not expected)
    assert config.ciao_owned_vault_index("unregistered") is False


def test_explicit_pre_reroot_vault_is_not_treated_as_ciao_owned(tmp_path: Path) -> None:
    external = tmp_path / "cloud-drive" / "vault"
    config = CiaoConfig.from_env(
        {
            "PWA_AUTH_TOKEN": "test-token",
            "CIAO_WORKSPACE": str(tmp_path),
            "CIAO_VAULT_ROOT": str(external),
        },
        export=False,
    )

    assert config.vault_root == external
    assert config.ciao_owned_vault_index(None) is False
