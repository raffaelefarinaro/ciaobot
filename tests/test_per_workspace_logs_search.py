"""Maintenance readers (search rebuild, transcript repair, SDK blob cleanup) cover
every transcript archive: each workspace's `<root>/Logs` and the shared `Logs/`."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ciao import cleanup_sdk_blobs
from ciao.config import reset_reroot_cache
from ciao.fts_search import init_db
from ciao.workspace_reroot import (
    mark_born_per_root,
    rebuild_search_index,
    registry_file,
    unindexed_transcript_archive,
)

WORK_UUID = "11111111-1111-1111-1111-111111111111"
SHARED_UUID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _clean_reroot_cache() -> None:
    reset_reroot_cache()


def _rerooted_install(tmp_path: Path, names: list[str]) -> Path:
    """A re-rooted install with a registry naming `names`, as a real one has."""
    install = tmp_path / "install"
    runtime = install / ".runtime"
    runtime.mkdir(parents=True)
    mark_born_per_root(install, runtime, names)
    registry_file(runtime).write_text(
        json.dumps([{"name": name} for name in names]), encoding="utf-8"
    )
    reset_reroot_cache()
    return install


def _transcript(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Chat\n\nkite talk\n", encoding="utf-8")
    return path


def test_rebuild_search_index_indexes_every_root(tmp_path: Path) -> None:
    install = _rerooted_install(tmp_path, ["personal"])
    (install / "personal" / "memory-vault").mkdir(parents=True)
    _transcript(install / "personal" / "Logs" / "Chats" / "c2" / "claude" / "b.md")
    _transcript(install / "Logs" / "Chats" / "c3" / "claude" / "c.md")
    db = tmp_path / "fts.db"

    result = rebuild_search_index(install, ["personal"], db_path=db)

    assert result["errors"] == []
    conn = sqlite3.connect(db)
    try:
        paths = {row[0] for row in conn.execute("SELECT path FROM transcript_meta")}
    finally:
        conn.close()
    assert paths == {
        "personal/Logs/Chats/c2/claude/b.md",
        "Logs/Chats/c3/claude/c.md",
    }


def test_rebuild_reports_transcript_totals_across_every_root(tmp_path: Path) -> None:
    """The reported counts sum every root. Two files under personal/Logs and one
    under the shared Logs/ must report 3: a first-root-only total gives 1 or 2."""
    install = _rerooted_install(tmp_path, ["personal"])
    (install / "personal" / "memory-vault").mkdir(parents=True)
    _transcript(install / "personal" / "Logs" / "Chats" / "c1" / "claude" / "a.md")
    _transcript(install / "personal" / "Logs" / "Chats" / "c2" / "claude" / "b.md")
    _transcript(install / "Logs" / "Chats" / "c3" / "claude" / "c.md")

    result = rebuild_search_index(install, ["personal"], db_path=tmp_path / "fts.db")

    assert result["logs"] == {"indexed": 3, "removed": 0}


def test_unindexed_transcript_archive_counts_per_workspace_logs(tmp_path: Path) -> None:
    install = _rerooted_install(tmp_path, ["work"])
    _transcript(install / "work" / "Logs" / "Chats" / "c1" / "claude" / "a.md")
    db = tmp_path / "fts.db"
    conn = sqlite3.connect(db)
    try:
        init_db(conn)
    finally:
        conn.close()

    assert unindexed_transcript_archive(install, db_path=db) == 1


def test_cleanup_sdk_blobs_reads_every_chats_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install = _rerooted_install(tmp_path, ["work"])
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / "memory"))
    _transcript(install / "work" / "Logs" / "Chats" / "c1" / "claude" / f"{WORK_UUID}.md")
    _transcript(install / "Logs" / "Chats" / "c3" / "claude" / f"{SHARED_UUID}.md")

    assert cleanup_sdk_blobs.main(["--workspace", str(install)]) == 0

    assert "Archived session IDs found: 2" in capsys.readouterr().out
