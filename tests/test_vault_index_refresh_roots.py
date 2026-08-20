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


def test_refresh_does_not_write_a_per_workspace_index(plane) -> None:
    control_plane, principal, vault = plane

    control_plane.vault_index_refresh(principal)

    assert not (vault / "personal" / "INDEX.md").exists()
    assert not (vault / "work" / "INDEX.md").exists()
