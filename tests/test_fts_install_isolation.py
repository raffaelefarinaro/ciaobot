"""The FTS database is owned by one install, not by the whole user.

`get_db_path()` used to default to ``~/.ciao/vault-fts.db`` for every install on
the machine. ``_ensure_path_base`` drops all four search tables whenever the
stored key base changes, so a production install and a dev checkout using that
one database repeatedly invalidated each other's derived index — even though
workspace-prefix isolation kept the two installs' *notes* apart. These tests pin
the install-owned resolver around the public search entry points, so a partial
migration (one caller still using the legacy global path) fails here rather than
silently creating two indexes for one install.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import fts_search
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.control_plane import CiaoControlPlane, McpPrincipal


def _install(tmp_path: Path, name: str, *, workspaces: tuple[str, ...] = ("personal",)) -> CiaoConfig:
    """A minimal install with its own runtime, vault and registry."""
    root = tmp_path / name
    runtime = root / ".runtime"
    for workspace in workspaces:
        notes = root / workspace / "memory-vault" / "People"
        notes.mkdir(parents=True, exist_ok=True)
        (notes / f"{workspace.title()}.md").write_text(
            f"---\ntitle: {workspace} person\n---\n# {workspace}\n"
            f"findme-{name}-{workspace}\n",
            encoding="utf-8",
        )
    return CiaoConfig(
        pwa_auth_token="t",
        workspace_root=root,
        vault_root=root / "memory-vault",
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            w: WorkspaceConfig(name=w, vault_root=f"{w}/memory-vault") for w in workspaces
        },
    )


def _plane(config: CiaoConfig) -> CiaoControlPlane:
    root = config.workspace_root
    return CiaoControlPlane(
        config,
        project_chat_manager=SimpleNamespace(
            _workspace_vault_root=lambda ws: root / ws / "memory-vault"
        ),
        schedule_manager=SimpleNamespace(),
    )


def _principal(workspace: str = "personal") -> McpPrincipal:
    return McpPrincipal(
        token_id="t", chat_id="c", project_id="p", workspace=workspace, provider="claude"
    )


@pytest.fixture(autouse=True)
def _no_memory_dir_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let these tests exercise the install-owned default, not the test override."""
    monkeypatch.delenv("CIAO_MEMORY_DIR", raising=False)


def test_two_installs_keep_independent_indexes(tmp_path: Path) -> None:
    """Alternating searches from two install roots preserve both indexes.

    The regression: one shared database meant the second install's key base
    dropped the first install's rows (and refilled them on its own next search),
    so both spent every turn rebuilding a derived index the other had just
    cleared.
    """
    install_a = _install(tmp_path, "prod")
    install_b = _install(tmp_path, "dev")

    plane_a = _plane(install_a)
    plane_b = _plane(install_b)

    rows_a = plane_a.vault_search(_principal(), "findme-prod-personal")["data"]
    rows_b = plane_b.vault_search(_principal(), "findme-dev-personal")["data"]

    assert [r["path"] for r in rows_a] == ["personal/memory-vault/People/Personal.md"]
    assert [r["path"] for r in rows_b] == ["personal/memory-vault/People/Personal.md"]

    # Alternate again: the first install's rows survive the second's pass.
    assert plane_a.vault_search(_principal(), "findme-prod-personal")["data"]
    assert fts_search.get_db_path(install_a.state_path.parent) != fts_search.get_db_path(
        install_b.state_path.parent
    )
    assert (install_a.state_path.parent / fts_search.SEARCH_DB_NAME).is_file()
    assert (install_b.state_path.parent / fts_search.SEARCH_DB_NAME).is_file()


def test_one_install_shares_one_prefixed_index_across_workspaces(tmp_path: Path) -> None:
    """Multiple logical workspaces inside one install still share the prefixed index.

    Install ownership does not fragment a single install: both roots resolve the
    same database, keys stay relative to the install root, and the search filter
    keeps each workspace inside its own subtree.
    """
    config = _install(tmp_path, "multi", workspaces=("personal", "work"))
    plane = _plane(config)

    personal = plane.vault_search(_principal("personal"), "findme-multi-personal")["data"]
    work = plane.vault_search(_principal("work"), "findme-multi-work")["data"]

    assert [r["path"] for r in personal] == [
        "personal/memory-vault/People/Personal.md"
    ]
    assert [r["path"] for r in work] == ["work/memory-vault/People/Work.md"]
    # One database, two prefixed subtrees — not two databases.
    assert len(list(config.state_path.parent.glob("*.db"))) == 1


def test_cli_mcp_and_archive_entry_points_resolve_the_same_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every caller must resolve one install-owned database, or partial migration
    would create two indexes for the same install.
    """
    from ciao import cli

    config = _install(tmp_path, "shared")
    plane = _plane(config)
    runtime = config.state_path.parent

    # MCP and startup indexing resolve the install runtime directory.
    mcp_db = fts_search.get_db_path(plane._search_runtime_dir())
    startup_db = fts_search.get_db_path(runtime)
    assert mcp_db == startup_db

    monkeypatch.setenv("CIAO_WORKSPACE", str(config.workspace_root))
    monkeypatch.delenv("CIAO_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("CIAO_MEMORY_DIR", raising=False)
    assert (
        cli.main(
            ["vault-search", "findme-shared-personal", "--vault-root", str(config.vault_root)]
        )
        == 0
    )

    # The CLI wrote the same install-owned database, not a second one.
    assert mcp_db.is_file()
    assert list(runtime.glob("*.db")) == [mcp_db]
    # The archive path indexes into the install runtime, not a global cache.
    assert fts_search.get_db_path(Path(config.state_path).parent) == mcp_db


def test_legacy_global_cache_is_left_in_place_and_not_used(tmp_path: Path, monkeypatch) -> None:
    """Upgrade: the old ``~/.ciao/vault-fts.db`` is neither moved nor deleted.

    It is derived state, so the install simply rebuilds its own index on first
    use. The user's markdown is irrelevant to this path and must be untouched.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    legacy_dir = home / ".ciao"
    legacy_dir.mkdir(parents=True)
    legacy = legacy_dir / fts_search.SEARCH_DB_NAME
    conn = sqlite3.connect(legacy)
    fts_search.init_db(conn)
    conn.execute(
        "INSERT INTO vault_meta (path, mtime, indexed_at) VALUES ('legacy/note.md', 1.0, 'x')"
    )
    conn.commit()
    conn.close()

    config = _install(tmp_path, "upgraded")
    note = config.workspace_root / "personal" / "memory-vault" / "People" / "Personal.md"
    before = note.read_text(encoding="utf-8")

    plane = _plane(config)
    rows = plane.vault_search(_principal(), "findme-upgraded-personal")["data"]

    install_db = fts_search.get_db_path(config.state_path.parent)
    assert install_db != legacy
    assert install_db.is_file()
    assert rows, "the fresh install-owned index serves the search"
    rows = conn_rows(legacy)
    assert rows == ["legacy/note.md"], "the legacy cache was left untouched"
    assert note.read_text(encoding="utf-8") == before, "source markdown is never rewritten"


def conn_rows(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT path FROM vault_meta")]
    finally:
        conn.close()


def test_explicit_override_still_wins_over_the_install_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CIAO_MEMORY_DIR`` remains the explicit override for tests and migration.

    It is the operator's escape hatch, so it wins — and because it is explicit,
    the ownership check in ``_ensure_path_base`` diagnoses a shared database
    rather than silently producing two key formats.
    """
    override = tmp_path / "override"
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(override))
    config = _install(tmp_path, "overridden")

    assert fts_search.get_db_path(config.state_path.parent) == override / fts_search.SEARCH_DB_NAME
    assert not (config.state_path.parent / fts_search.SEARCH_DB_NAME).exists()


def test_two_installs_sharing_an_override_diagnose_the_reset(tmp_path: Path, caplog) -> None:
    """A deliberately shared database is diagnosed, not silently mixed.

    When two key bases use one database, the second pass drops the first's rows.
    That is unavoidable for derived state, but it must be visible in the log so
    an operator sharing ``CIAO_MEMORY_DIR`` across installs is told why search
    keeps going empty.
    """
    shared = tmp_path / "shared.db"
    conn = sqlite3.connect(shared)
    try:
        fts_search.init_db(conn)
        install_a = tmp_path / "a"
        install_b = tmp_path / "b"
        fts_search.index_vault(conn, install_a / "memory-vault", path_base=install_a)
        with caplog.at_level("WARNING"):
            fts_search.index_vault(conn, install_b / "memory-vault", path_base=install_b)
    finally:
        conn.close()

    assert any("path base changed" in record.message for record in caplog.records)
