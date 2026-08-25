"""`vault_index_refresh` writes the shared entity index, not a per-workspace one.

`INDEX.md` is a single artifact at the top-level vault root: entity lookup
resolves `<vault>/INDEX.md` and filters by workspace itself, so it needs
every workspace's prefixed paths in one file. Writing it into the
active workspace's subtree instead produced an index whose paths no filter
recognized, left the real one stale, and littered the vault with entry-less
stubs.

The FTS index is the opposite: it backs `vault_search`, whose isolation boundary
is the per-workspace root, so it stays workspace-scoped.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.control_plane import CiaoControlPlane, McpPrincipal


def _note(path: Path, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: note\n---\n# {title}\n", encoding="utf-8")


@pytest.fixture
def plane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".ciao"))
    vault = tmp_path / "memory-vault"
    _note(vault / "personal" / "People" / "Alba.md", "Alba")
    _note(vault / "work" / "People" / "Aymen.md", "Aymen")

    config = SimpleNamespace(
        workspace=lambda name: object() if name in {"personal", "work"} else None,
        vault_root=vault,
        workspace_root=tmp_path,
        # Faithful to the pre-migration truth: every workspace's agent vault IS
        # the shared vault. Omitting it made `_index_stamp` fall into its
        # AttributeError branch, so a mutation that always stamped went unnoticed.
        agent_vault_root=lambda name: vault,
    )
    pcm = SimpleNamespace(_workspace_vault_root=lambda ws: vault / ws)
    control_plane = CiaoControlPlane(
        config,
        project_chat_manager=pcm,
        schedule_manager=SimpleNamespace(),
        loop_manager=SimpleNamespace(),
    )
    principal = McpPrincipal(
        token_id="token-1",
        chat_id="chat-1",
        project_id="proj-1",
        workspace="personal",
        provider="claude",
    )
    return control_plane, principal, vault


def test_refresh_writes_the_shared_index_with_prefixed_paths(plane) -> None:
    control_plane, principal, vault = plane

    result = control_plane.vault_index_refresh(principal)

    assert result["ok"] is True
    index = (vault / "INDEX.md").read_text(encoding="utf-8")
    # Workspace-prefixed paths are what the visibility filter keys on, so every
    # workspace has to be present in the one file for any of them to resolve.
    assert "personal/People/Alba" in index
    assert "work/People/Aymen" in index


def test_the_shared_index_keeps_each_entry_own_workspace(plane) -> None:
    """The stamp must NOT be applied on a shared vault.

    `_entity_visible_in_workspace` filters on `Entry.workspace`, so stamping
    every entry with the operating workspace would make one workspace's chat see
    the other's notes as its own — fail-open, and invisible in the rendered
    paths, which is why this asserts on the workspaces rather than the file.
    """
    control_plane, principal, vault = plane
    from ciao.vault_index import scan_vault

    control_plane.vault_index_refresh(principal)

    stamp = control_plane._index_stamp(principal)
    assert stamp == "", "a shared vault infers the workspace; it must not be stamped"
    entries = scan_vault(vault, workspace=stamp)
    assert sorted({e.workspace for e in entries}) == ["personal", "work"]


def test_refresh_does_not_write_a_per_workspace_index(plane) -> None:
    control_plane, principal, vault = plane

    control_plane.vault_index_refresh(principal)

    assert not (vault / "personal" / "INDEX.md").exists()
    assert not (vault / "work" / "INDEX.md").exists()


# -- after the re-rooting the index is per root ------------------------------


def _reroot(tmp_path: Path) -> None:
    import json

    receipt = tmp_path / ".runtime" / "migration" / "workspace-rooting.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"status": "migrated"}), encoding="utf-8")
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()


def test_refresh_writes_this_roots_index_after_the_re_rooting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before this it wrote the shared root, which the migration empties, so a
    migrated install's per-root indexes were never rebuilt by the MCP tool."""
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".ciao"))
    from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache

    for name in ("personal", "work"):
        _note(tmp_path / name / "memory-vault" / "People" / f"{name}.md", name.title())
    (tmp_path / ".runtime").mkdir(parents=True, exist_ok=True)
    reset_reroot_cache()
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        vault_root=tmp_path / "memory-vault",
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="personal/memory-vault"),
            "work": WorkspaceConfig(name="work", vault_root="work/memory-vault"),
        },
    )
    _reroot(tmp_path)
    plane = CiaoControlPlane(
        config,
        project_chat_manager=SimpleNamespace(
            _workspace_vault_root=lambda ws: tmp_path / ws / "memory-vault"
        ),
        schedule_manager=SimpleNamespace(),
        loop_manager=SimpleNamespace(),
    )
    principal = McpPrincipal(
        token_id="t", chat_id="c", project_id="p", workspace="work", provider="claude"
    )

    result = plane.vault_index_refresh(principal)

    assert result["ok"] is True
    index = tmp_path / "work" / "memory-vault" / "INDEX.md"
    assert index.is_file(), "the work root's own index"
    assert not (tmp_path / "memory-vault" / "INDEX.md").exists(), "not the emptied shared root"
    # A root's vault holds one workspace, so its index carries no prefix.
    text = index.read_text(encoding="utf-8")
    assert "work/People" not in text
    assert "People/work" in text
