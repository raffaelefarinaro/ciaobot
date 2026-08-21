"""The audit's search-index section.

The audit checked notes, links, guides, regions and skills — never the search
index. That mattered most for the case where the audit is the ONLY backstop: a
vault reorganised by hand (or by a model following the migration prompt) moves
notes without rebuilding the index, and nothing reported that search was silently
answering from the old paths.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ciao import os_audit
from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache


@pytest.fixture(autouse=True)
def _own_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".ciao"))


def _install(tmp_path: Path) -> CiaoConfig:
    (tmp_path / ".runtime" / "migration").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".runtime" / "migration" / "workspace-rooting.json").write_text(
        json.dumps({"status": "migrated"}), encoding="utf-8"
    )
    for name in ("personal", "work"):
        notes = tmp_path / name / "memory-vault" / "People"
        notes.mkdir(parents=True, exist_ok=True)
        (notes / f"{name.title()}Person.md").write_text(
            "---\ntype: person\n---\n# Someone\n", encoding="utf-8"
        )
        (tmp_path / name / "CLAUDE.md").write_text(
            "# Guide\n<!-- ciao:memory:start cap=2200 -->\n<!-- ciao:memory:end -->\n"
            "<!-- ciao:profile:start cap=1375 -->\n<!-- ciao:profile:end -->\n",
            encoding="utf-8",
        )
    reset_reroot_cache()
    return CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        vault_root=tmp_path / "memory-vault",
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            n: WorkspaceConfig(name=n, vault_root=f"{n}/memory-vault")
            for n in ("personal", "work")
        },
    )


def _audit(tmp_path: Path, config: CiaoConfig) -> dict:
    return os_audit.run_os_audit(
        tmp_path, tmp_path / "memory-vault", tmp_path / ".runtime",
        config=config, scope="all",
    )


def test_an_absent_index_is_reported_when_notes_exist(tmp_path: Path) -> None:
    config = _install(tmp_path)

    report = _audit(tmp_path, config)

    assert report["search_index"]["missing"] is True
    assert report["search_index"]["stale_rows"] == []


def test_rows_pointing_at_moved_notes_are_reported(tmp_path: Path) -> None:
    """What a hand-migration leaves: notes moved, index untouched."""
    from ciao.workspace_reroot import rebuild_search_index

    config = _install(tmp_path)
    rebuild_search_index(tmp_path, ["personal", "work"])
    moved = tmp_path / "personal" / "memory-vault" / "People" / "PersonalPerson.md"
    moved.rename(tmp_path / "work" / "memory-vault" / "People" / "PersonalPerson.md")

    report = _audit(tmp_path, config)

    assert report["search_index"]["missing"] is False
    assert report["search_index"]["stale_rows"] == [
        "personal/memory-vault/People/PersonalPerson.md"
    ]


def test_a_current_index_is_silent(tmp_path: Path) -> None:
    from ciao.workspace_reroot import rebuild_search_index

    config = _install(tmp_path)
    rebuild_search_index(tmp_path, ["personal", "work"])

    report = _audit(tmp_path, config)

    assert report["search_index"] == {"missing": False, "stale_rows": [], "errors": []}


def test_an_index_finding_counts_as_a_defect(tmp_path: Path) -> None:
    """Reported but not counted would render in the report and never change
    `status` — the same as not reporting it for anyone reading the summary."""
    config = _install(tmp_path)

    with_missing = _audit(tmp_path, config)["defect_count"]
    from ciao.workspace_reroot import rebuild_search_index

    rebuild_search_index(tmp_path, ["personal", "work"])
    with_index = _audit(tmp_path, config)["defect_count"]

    assert with_missing == with_index + 1


def test_an_empty_install_is_not_told_its_index_is_missing(tmp_path: Path) -> None:
    """No notes, nothing to index, nothing to report."""
    (tmp_path / ".runtime" / "migration").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".runtime" / "migration" / "workspace-rooting.json").write_text(
        json.dumps({"status": "migrated"}), encoding="utf-8"
    )
    reset_reroot_cache()
    config = CiaoConfig(
        pwa_auth_token="t", workspace_root=tmp_path,
        vault_root=tmp_path / "memory-vault",
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={"personal": WorkspaceConfig(name="personal", vault_root="personal/memory-vault")},
    )

    assert _audit(tmp_path, config)["search_index"]["missing"] is False
