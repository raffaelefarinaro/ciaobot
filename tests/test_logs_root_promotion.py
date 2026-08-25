"""D5's `Logs/` promotion, seen from the READ side.

The re-rooting promotes `memory-vault/Logs` to `<install>/Logs` unmoved, because
it holds roughly 72% of the vault's notes, is derived output rather than curated
content, and its chat ids cannot each be resolved back to one workspace.

Nothing that reads it knew that. Five sites computed `vault_root / "Logs"`,
including the TranscriptStore root, so on a migrated install chat archiving would
recreate `<install>/memory-vault/Logs/Chats` and write new transcripts into a
fresh empty tree — nothing lost, but the archive silently splits in two and the
promoted half becomes invisible. `config.logs_root` is the one place the layout
question is answered.
"""

from __future__ import annotations

import json
from pathlib import Path

from ciao.config import CiaoConfig, WorkspaceConfig, logs_root_for


def _config(root: Path) -> CiaoConfig:
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=root,
        vault_root=root / "memory-vault",
        state_path=root / ".runtime" / "state.json",
        media_root=root / ".runtime" / "media",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal"),
        },
    )


def _migrated_receipt(root: Path) -> None:
    path = root / ".runtime" / "migration" / "workspace-rooting.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": "migrated"}), encoding="utf-8")
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()


def test_logs_root_is_inside_the_vault_before_the_re_rooting(tmp_path: Path) -> None:
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()
    config = _config(tmp_path)

    assert config.logs_root == tmp_path / "memory-vault" / "Logs"


def test_logs_root_follows_the_promotion_after_the_re_rooting(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _migrated_receipt(tmp_path)

    assert config.logs_root == tmp_path / "Logs"


def test_the_standalone_form_answers_the_same_question(tmp_path: Path) -> None:
    """A CLI holding only paths must not end up with a second copy of the rule."""
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()
    config = _config(tmp_path)
    args = (tmp_path, tmp_path / "memory-vault", tmp_path / ".runtime")
    assert logs_root_for(*args) == config.logs_root

    _migrated_receipt(tmp_path)
    assert logs_root_for(*args) == config.logs_root == tmp_path / "Logs"


def test_the_transcript_store_root_follows_the_archive(tmp_path: Path) -> None:
    """The site that would have split the archive in two."""
    import inspect

    from ciao import main

    source = inspect.getsource(main)
    assert "config.logs_root / \"Chats\"" in source
    assert 'config.vault_root / "Logs"' not in source


def test_index_logs_indexes_the_promoted_archive(tmp_path: Path) -> None:
    """Derived as `vault_root / "Logs"` it indexed nothing on a migrated install."""
    import sqlite3

    from ciao import fts_search

    install = tmp_path / "install"
    promoted = install / "Logs" / "Chats"
    promoted.mkdir(parents=True)
    (promoted / "session.md").write_text("# a chat\nfindme\n", encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    fts_search.init_db(conn)

    # The vault root no longer contains Logs, which is the migrated shape.
    stale, _ = fts_search.index_logs(conn, install / "personal" / "memory-vault")
    assert stale == 0

    indexed, _ = fts_search.index_logs(
        conn, install / "personal" / "memory-vault", logs_root=install / "Logs"
    )
    assert indexed == 1
    assert fts_search.search_logs(conn, "findme", limit=5)


def test_no_reader_derives_the_archive_from_the_vault_root_any_more(tmp_path: Path) -> None:
    """Conservation over the sweep itself: the five sites are the whole set.

    Asserted against the source rather than behaviour because the failure is a
    path that silently resolves elsewhere, which no unit test of one caller would
    notice.
    """
    import inspect

    from ciao import cleanup_sdk_blobs, insights, main
    from ciao.web import project_chats

    for module in (main, insights, project_chats, cleanup_sdk_blobs):
        source = inspect.getsource(module)
        offenders = [
            line.strip()
            for line in source.splitlines()
            if 'vault_root / "Logs"' in line and not line.strip().startswith("#")
        ]
        assert offenders == [], f"{module.__name__}: {offenders}"


def test_a_dry_run_backfill_log_line_cannot_raise(tmp_path: Path) -> None:
    """It rendered the archive path relative to the VAULT root.

    The re-rooting promotes Logs/ out of the vault, so `relative_to(vault_root)`
    raises ValueError — from inside a `logger.info` call, taking down the dry run
    with it. A log line must never be the thing that fails.
    """
    import inspect

    from ciao import insights

    source = inspect.getsource(insights)
    assert "md.relative_to(vault_root)" not in source
    # And the replacement is total.
    assert "shown = md" in source

    install = tmp_path / "install"
    archive = install / "Logs" / "Chats" / "chat-1" / "session.md"
    archive.parent.mkdir(parents=True)
    archive.write_text("# chat\n", encoding="utf-8")
    # The shape that used to raise: the archive is not under the vault root.
    try:
        archive.relative_to(install / "memory-vault")
    except ValueError:
        pass
    else:  # pragma: no cover - guards the premise of this test
        raise AssertionError("expected the archive to sit outside the vault root")


def test_setup_does_not_invent_a_shared_vault_after_the_re_rooting(tmp_path: Path) -> None:
    """`ciao setup` died on a migrated install.

    `ensure_vault_git` probed `<install>/memory-vault`, which the migration
    removes, and fell through to writing a `.gitignore` inside it —
    FileNotFoundError, out of `setup --load-launchd`, which is the command the
    installer runs to re-render the launchd plist. Each root's vault lives inside
    the install repo already, so there is nothing to initialise and nothing to
    invent.
    """
    from ciao.cli import ensure_vault_git

    missing = tmp_path / "memory-vault"
    assert not missing.exists()

    ensure_vault_git(missing)

    assert not missing.exists(), "it must not scaffold the directory the migration removed"


def test_setup_scaffolds_agent_roots_not_the_install_root(tmp_path: Path) -> None:
    """`ciao setup --load-launchd` is what the installer runs, so scaffolding the
    install root unconditionally put a stock CLAUDE.md, stock commands and a
    subagents/ directory beside the real per-root ones on every reinstall of a
    migrated install."""
    import json

    from ciao.config import agent_roots_for

    install = tmp_path / "install"
    runtime = install / ".runtime"
    runtime.mkdir(parents=True)

    # Before the re-rooting: the install root IS the agent root.
    assert agent_roots_for(install, runtime) == [(install, "")]

    (runtime / "migration").mkdir()
    (runtime / "migration" / "workspace-rooting.json").write_text(
        json.dumps({"status": "migrated"}), encoding="utf-8"
    )
    (runtime / "workspaces.json").write_text(
        json.dumps([{"name": "work"}, {"name": "personal"}]), encoding="utf-8"
    )
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()

    assert agent_roots_for(install, runtime) == [
        (install / "personal", "personal"),
        (install / "work", "work"),
    ]


def test_the_standalone_helper_never_manufactures_workspace_names(tmp_path: Path) -> None:
    """Setup runs on installs with no registry yet, and the bootstrap fallback
    would scaffold two roots for an install that has none."""
    import json

    from ciao.config import agent_roots_for, reset_reroot_cache

    install = tmp_path / "install"
    runtime = install / ".runtime" / "migration"
    runtime.mkdir(parents=True)
    (runtime / "workspace-rooting.json").write_text(
        json.dumps({"status": "migrated"}), encoding="utf-8"
    )
    reset_reroot_cache()

    # Migrated receipt but no registry: fall back to the install root, not to
    # invented `personal` and `work` directories.
    assert agent_roots_for(install, install / ".runtime") == [(install, "")]
