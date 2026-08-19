"""Trajectories are per-workspace evidence and must be read as such.

Every trajectory records the workspace it came from, but `list_trajectories`
had no workspace filter — it globbed all of them. Skill evolution then read
every workspace's sessions and wrote its proposals to a single queue (whichever
workspace was active, else the primary), so work-session content was quoted into
the personal vault.

The skill catalog itself is global, so the fix is routing, not fan-out: one pass,
each workspace's evidence to that workspace's queue.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ciao.trajectory_builder import list_trajectories


def _trajectory(root: Path, name: str, *, workspace: str, when: datetime) -> Path:
    month = root / when.strftime("%Y-%m")
    month.mkdir(parents=True, exist_ok=True)
    path = month / f"{name}.json"
    path.write_text(
        json.dumps(
            {
                "session_id": name,
                "workspace": workspace,
                "timestamp": when.isoformat().replace("+00:00", "Z"),
                "skills_loaded": ["pr"],
                "outcome": "error",
            }
        ),
        encoding="utf-8",
    )
    return path


def _fixture(tmp_path: Path) -> tuple[Path, datetime]:
    now = datetime(2026, 8, 19, tzinfo=UTC)
    root = tmp_path / "trajectories"
    _trajectory(root, "a", workspace="personal", when=now)
    _trajectory(root, "b", workspace="work", when=now)
    _trajectory(root, "c", workspace="work", when=now)
    return root, now


def test_workspace_filter_keeps_only_that_workspace(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path)

    personal = list_trajectories(workspace="personal", root=root)
    work = list_trajectories(workspace="work", root=root)

    assert [p.stem for p in personal] == ["a"]
    assert [p.stem for p in work] == ["b", "c"]


def test_no_workspace_filter_still_returns_everything(tmp_path: Path) -> None:
    """Unfiltered stays unfiltered — callers that want every workspace (the
    retention prune, a manual audit) must not silently narrow."""
    root, _ = _fixture(tmp_path)

    assert len(list_trajectories(root=root)) == 3


def test_workspace_filter_composes_with_since_and_skill(tmp_path: Path) -> None:
    root, now = _fixture(tmp_path)
    _trajectory(root, "old", workspace="work", when=now - timedelta(days=90))

    recent_work = list_trajectories(
        workspace="work", since=now - timedelta(days=7), skill="pr", root=root
    )

    assert [p.stem for p in recent_work] == ["b", "c"]


def test_a_record_without_a_workspace_is_not_claimed_by_one(tmp_path: Path) -> None:
    """Pre-field records have no owner; a filtered read must not adopt them into
    whichever workspace happens to ask."""
    root = tmp_path / "trajectories"
    month = root / "2026-08"
    month.mkdir(parents=True)
    (month / "legacy.json").write_text(
        json.dumps({"session_id": "legacy", "timestamp": "2026-08-19T00:00:00Z"}),
        encoding="utf-8",
    )

    assert list_trajectories(workspace="personal", root=root) == []
    assert len(list_trajectories(root=root)) == 1
