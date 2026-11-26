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

import asyncio
import json
import sqlite3
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
    (tmp_path / ".runtime").mkdir()
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

    result = asyncio.run(control_plane.vault_index_refresh(principal))

    assert result["ok"] is True
    index = (vault / "INDEX.md").read_text(encoding="utf-8")
    # Workspace-prefixed paths are what the visibility filter keys on, so every
    # workspace has to be present in the one file for any of them to resolve.
    assert "personal/People/Alba" in index
    assert "work/People/Aymen" in index


def test_refresh_preserves_a_synchronized_external_index(plane, monkeypatch) -> None:
    control_plane, principal, vault = plane
    control_plane.config.vault_index_ownership = lambda workspace: "external"
    control_plane.config.index_source_roots = lambda: frozenset({vault})
    sentinel = b"external sentinel\r\n"
    (vault / "INDEX.md").write_bytes(sentinel)

    result = asyncio.run(control_plane.vault_index_refresh(principal))

    assert result["ok"] is True
    assert result["data"]["markdown_index"] == "preserved_unmanaged"
    assert (vault / "INDEX.md").read_bytes() == sentinel


def test_the_shared_index_keeps_each_entry_own_workspace(plane) -> None:
    """The stamp must NOT be applied on a shared vault.

    `_entity_visible_in_workspace` filters on `Entry.workspace`, so stamping
    every entry with the operating workspace would make one workspace's chat see
    the other's notes as its own — fail-open, and invisible in the rendered
    paths, which is why this asserts on the workspaces rather than the file.
    """
    control_plane, principal, vault = plane
    from ciao.vault_index import scan_vault

    asyncio.run(control_plane.vault_index_refresh(principal))

    stamp = control_plane._index_stamp(principal)
    assert stamp == "", "a shared vault infers the workspace; it must not be stamped"
    entries = scan_vault(vault, workspace=stamp)
    assert sorted({e.workspace for e in entries}) == ["personal", "work"]


def test_refresh_does_not_write_a_per_workspace_index(plane) -> None:
    control_plane, principal, vault = plane

    asyncio.run(control_plane.vault_index_refresh(principal))

    assert not (vault / "personal" / "INDEX.md").exists()
    assert not (vault / "work" / "INDEX.md").exists()


def test_control_plane_refresh_requires_a_complete_scan_before_publish(plane, monkeypatch) -> None:
    from ciao import vault_index

    control_plane, principal, vault = plane
    index = vault / "INDEX.md"
    index.write_text("previous complete snapshot\n", encoding="utf-8")

    def incomplete_scan(root, **kwargs):
        assert kwargs.get("strict") is True
        raise OSError("cloud scan interrupted")

    monkeypatch.setattr(vault_index, "scan_vault", incomplete_scan)
    with pytest.raises(OSError, match="cloud scan interrupted"):
        asyncio.run(control_plane.vault_index_refresh(principal))

    assert index.read_text(encoding="utf-8") == "previous complete snapshot\n"


# -- after the re-rooting the index is per root ------------------------------


def _reroot(tmp_path: Path) -> None:
    import json




def test_refresh_writes_this_roots_index_after_the_re_rooting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before this it wrote the shared root, which the migration empties, so a
    migrated install's per-root indexes were never rebuilt by the MCP tool."""
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".ciao"))
    from ciao.config import CiaoConfig, WorkspaceConfig

    for name in ("personal", "work"):
        _note(tmp_path / name / "memory-vault" / "People" / f"{name}.md", name.title())
    (tmp_path / ".runtime").mkdir(parents=True, exist_ok=True)
    from ciao.setup_marker import write_vault_index_ownership

    write_vault_index_ownership(
        tmp_path / ".runtime",
        [tmp_path / "personal" / "memory-vault", tmp_path / "work" / "memory-vault"],
        owner="ciao",
    )
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
    )
    principal = McpPrincipal(
        token_id="t", chat_id="c", project_id="p", workspace="work", provider="claude"
    )

    result = asyncio.run(plane.vault_index_refresh(principal))

    assert result["ok"] is True
    index = tmp_path / "work" / "memory-vault" / "INDEX.md"
    assert index.is_file(), "the work root's own index"
    assert not (tmp_path / "memory-vault" / "INDEX.md").exists(), "not the emptied shared root"
    # A root's vault holds one workspace, so its index carries no prefix.
    text = index.read_text(encoding="utf-8")
    assert "work/People" not in text
    assert "People/work" in text


def test_adopted_refresh_indexes_locally_twice_and_preserves_cloud_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ciao import vault_index
    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.fts_search import (
        adopted_vault_key_prefix,
        connected_db_path,
        init_db,
        search_vault,
        vault_key_prefix,
    )

    runtime = tmp_path / ".runtime"
    cloud_root = tmp_path.parent / f"{tmp_path.name}-cloud-vault"
    note = cloud_root / "People" / "Garden.md"
    _note(note, "orchid")
    sentinel = cloud_root / "INDEX.md"
    sentinel.write_bytes(b"cloud provider owns this exact index\r\n")
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        vault_root=tmp_path / "memory-vault",
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={"personal": WorkspaceConfig(name="personal", vault_root=str(cloud_root))},
    )
    pcm = SimpleNamespace(_workspace_vault_root=lambda _workspace: cloud_root)
    plane = CiaoControlPlane(config, project_chat_manager=pcm, schedule_manager=SimpleNamespace())
    principal = McpPrincipal(
        token_id="t", chat_id="c", project_id="p", workspace="personal", provider="claude"
    )
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / "local-search"))

    def no_cross_volume_publish(*args, **kwargs):
        raise OSError(18, "EXDEV: no cloud-root rename/link is safe")

    monkeypatch.setattr(vault_index, "write_index_file", no_cross_volume_publish)
    namespace = adopted_vault_key_prefix(cloud_root, "personal")

    def local_hits(query: str) -> list[dict[str, str]]:
        conn = sqlite3.connect(connected_db_path(runtime, cloud_root))
        try:
            init_db(conn)
            return search_vault(
                conn,
                query,
                path_prefix=vault_key_prefix(
                    cloud_root, tmp_path, key_namespace=namespace
                ),
            )
        finally:
            conn.close()

    first = asyncio.run(plane.vault_index_refresh(principal))
    assert first["ok"] is True
    assert first["data"]["markdown_index"] == "preserved_unmanaged"
    assert local_hits("orchid")
    assert sentinel.read_bytes() == b"cloud provider owns this exact index\r\n"
    search = asyncio.run(plane.vault_search(principal, "orchid"))
    assert search["data"][0]["path"] == "People/Garden.md"

    from ciao.web.routes_api import _vault_graph_payload

    new_note = cloud_root / "People" / "New.md"
    _note(new_note, "calendula")
    note.unlink()
    assert asyncio.run(plane.vault_search(principal, "calendula"))["data"][0]["path"] == "People/New.md"
    graph = _vault_graph_payload(config, "personal")
    assert [item["title"] for item in graph["nodes"]] == ["calendula"]
    new_note.unlink()
    _note(note, "citrine")
    second = asyncio.run(plane.vault_index_refresh(principal))
    assert second["ok"] is True
    assert local_hits("orchid") == []
    assert local_hits("citrine")
    assert sentinel.read_bytes() == b"cloud provider owns this exact index\r\n"

    def interrupted(root: Path, **kwargs):
        assert kwargs.get("strict") is True
        raise OSError("cloud placeholder unavailable")

    monkeypatch.setattr(vault_index, "scan_vault", interrupted)
    with pytest.raises(OSError, match="cloud placeholder unavailable"):
        asyncio.run(plane.vault_index_refresh(principal))
    assert local_hits("citrine")
    assert sentinel.read_bytes() == b"cloud provider owns this exact index\r\n"


@pytest.mark.parametrize("operation", ["search", "refresh"])
def test_stale_server_workspace_root_cannot_authorize_new_registration(tmp_path, monkeypatch, operation):
    from ciao import fts_search
    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.control_plane import ControlPlaneError

    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "vault-index-ownership.json").write_text(
        json.dumps({"roots": [{"path": str(tmp_path), "owner": "external"}]}),
        encoding="utf-8",
    )
    old = tmp_path / "old"
    new = tmp_path / "new"
    _note(old / "Old.md", "privateword")
    _note(new / "New.md", "replacement")
    config = CiaoConfig(pwa_auth_token="t", workspace_root=tmp_path, state_path=runtime / "state.json",
                        media_root=runtime, workspaces={"personal": WorkspaceConfig("personal", str(old))})
    registry = runtime / "workspaces.json"
    # Another process saved the new root. The live server still holds its
    # previous config, so generation checks alone would all pass.
    registry.write_text(json.dumps([{"name": "personal", "vault_root": str(new)}]))
    plane = CiaoControlPlane(config, project_chat_manager=SimpleNamespace(_workspace_vault_root=lambda _: old),
                             schedule_manager=SimpleNamespace())
    principal = McpPrincipal(token_id="t", chat_id="c", project_id="p", workspace="personal", provider="claude")
    monkeypatch.setattr(fts_search, "read_connected_note", lambda *args: pytest.fail("read obsolete root"))
    with pytest.raises(ControlPlaneError, match="Source is no longer registered") as failure:
        asyncio.run(plane.vault_search(principal, "privateword") if operation == "search"
                    else plane.vault_index_refresh(principal))
    assert failure.value.code == "workspace_changed"
    database = fts_search.connected_db_path(runtime, old)
    with fts_search.connected_index_connection(database) as conn:
        fts_search.init_db(conn)
        namespace = fts_search.adopted_vault_key_prefix(old, "personal")
        assert fts_search.connected_entries(conn, namespace) is None
        assert not fts_search.search_vault(conn, "privateword", path_prefix=namespace)


def test_lookup_rechecks_a_preselected_link_against_the_pinned_registry(tmp_path, monkeypatch):
    from ciao import fts_search
    from ciao.config import SharedBrainLink

    install = tmp_path / "install"
    runtime = install / ".runtime"
    runtime.mkdir(parents=True)
    former = tmp_path / "former-link" / "notes"
    current = tmp_path / "current-link" / "notes"
    _note(former / "Old.md", "oldword")
    _note(current / "Current.md", "currentword")
    (runtime / "workspaces.json").write_text(json.dumps([{
        "name": "personal", "vault_root": "memory-vault/personal",
        "shared_brains": [{"name": "team", "path": str(current.parent),
                           "identity": "github.com/example/current", "lifetime_id": "new-link"}],
    }]))
    read = fts_search.read_connected_note
    reads = []

    def observed(root, rel):
        reads.append(root / rel)
        return read(root, rel)

    monkeypatch.setattr(fts_search, "read_connected_note", observed)
    with pytest.raises(fts_search.ConnectedWorkspaceChanged, match="Source is no longer registered"):
        fts_search.lookup_connected_entries(former, "personal", runtime, install, refresh=True)
    assert reads == []
    assert [e.title for e in fts_search.lookup_connected_entries(current, "personal", runtime, install,
                                                                 refresh=True, link=SharedBrainLink(
                                                                     "team", str(current.parent),
                                                                     "github.com/example/current", "new-link"))] == ["currentword"]
    assert reads == [current / "Current.md"]
    database = fts_search.connected_db_path(runtime, former)
    with fts_search.connected_index_connection(database) as conn:
        prefix = fts_search.adopted_vault_key_prefix(former, "personal")
        assert fts_search.connected_entries(conn, prefix) is None
        assert not fts_search.search_vault(conn, "oldword", path_prefix=prefix)


@pytest.mark.parametrize("changed", ["identity", "lifetime_id", "legacy"])
def test_independently_loaded_stale_link_cannot_index_same_path_replacement(tmp_path, monkeypatch, changed, capsys):
    from ciao import cli, config as config_module, fts_search
    from ciao.config import configured_connected_workspace

    install = tmp_path / "install"
    runtime = install / ".runtime"
    runtime.mkdir(parents=True)
    linked = tmp_path / "linked"
    notes = linked / "notes"
    _note(notes / "Topic.md", "privateword")
    env = {"CIAO_WORKSPACE": str(install), "CIAO_RUNTIME_ROOT": str(runtime),
           "CIAO_ACTIVE_WORKSPACE": "personal", "PWA_AUTH_TOKEN": "test"}
    row = {"name": "team", "path": str(linked), "identity": "github.com/example/first", "lifetime_id": "first"}

    if changed == "legacy":
        del row["lifetime_id"]

    def save():
        staging = runtime / "workspaces.tmp"
        staging.write_text(json.dumps([{
            "name": "personal", "vault_root": "memory-vault/personal", "shared_brains": [row],
        }]))
        staging.replace(runtime / "workspaces.json")

    save()
    stale = configured_connected_workspace(env, notes)
    assert stale is not None and stale.link is not None
    if changed != "legacy":
        row[changed] = "github.com/example/replacement" if changed == "identity" else "second"
    save()
    fresh = configured_connected_workspace(env, notes)
    assert fresh is not None and fresh.link is not None
    original_read = fts_search.read_connected_note
    reads = []

    def observed(root, rel):
        reads.append(root / rel)
        return original_read(root, rel)

    monkeypatch.setattr(fts_search, "read_connected_note", observed)
    for refresh in (False, True):
        with pytest.raises(fts_search.ConnectedWorkspaceChanged, match="Shared brain registration changed"):
            fts_search.lookup_connected_entries(notes, stale.name, runtime, install,
                                                refresh=refresh, link=stale.link)
    # The process selected its source from stale state before this request's
    # generation was captured. Exercise the actual CLI handoff too.
    with monkeypatch.context() as stale_process:
        for key, value in env.items():
            stale_process.setenv(key, value)
        stale_process.setattr(config_module, "configured_connected_workspace", lambda *a, **kw: stale)
        assert cli.main(["vault-search", "privateword", "--vault-root", str(notes)]) == 1
        assert "Shared brain registration changed" in capsys.readouterr().err
    assert reads == []
    database = fts_search.connected_db_path(runtime, notes)
    old_key = fts_search.adopted_vault_key_prefix(notes, stale.name, link=stale.link)
    new_key = fts_search.adopted_vault_key_prefix(notes, fresh.name, link=fresh.link)
    assert old_key != new_key
    with fts_search.connected_index_connection(database) as conn:
        fts_search.init_db(conn)
        assert conn.execute("SELECT COUNT(*) FROM vault_fts").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM connected_indexes").fetchone() == (0,)
    entries = fts_search.lookup_connected_entries(notes, fresh.name, runtime, install, link=fresh.link)
    assert [e.title for e in entries] == ["privateword"]
    assert reads == [notes / "Topic.md"]
    with fts_search.connected_index_connection(database) as conn:
        assert not fts_search.search_vault(conn, "privateword", path_prefix=old_key)
        assert fts_search.search_vault(conn, "privateword", path_prefix=new_key)
