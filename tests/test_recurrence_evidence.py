"""Read-only lookup of persisted completions for the recurrence gate (#3238-B).

Every fixture is a throwaway vault under ``tmp_path``. The lookup reads the
workspace task files, so these tests build real task files through
:class:`TaskBoardStore` rather than hand-writing frontmatter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ciao import recurrence_evidence
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.recurrence import Outcome
from ciao.recurrence_evidence import EvidenceUnavailable, resolve_completions
from ciao.task_board import TaskBoardStore
from ciao.task_resolution import Completion, append_completion


def _config(tmp_path: Path) -> CiaoConfig:
    runtime = tmp_path / ".runtime"
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={"work": WorkspaceConfig(name="work", vault_root="work")},
    )


def _store(config: CiaoConfig) -> TaskBoardStore:
    return TaskBoardStore(
        workspace="work",
        vault_root=Path(config.workspace_vault_root("work")),
        runtime_dir=config.state_path.parent,
        clock=lambda: datetime.now(UTC),
    )


def _task(config: CiaoConfig, title: str, completion_ids: list[str]) -> str:
    """One task whose body records ``completion_ids``; returns the task id."""
    body = ""
    for cid in completion_ids:
        body = append_completion(
            body,
            Completion(
                id=cid,
                completed_at=datetime(2026, 11, 1, 9, 0, tzinfo=UTC),
                resolution="Done.",
            ),
        )
    return _store(config).create(title=title, body=body).record.id


def _cid(n: int) -> str:
    return f"{n:032x}"


def test_persisted_completions_resolve_to_the_task_that_holds_them(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = _task(config, "First", [_cid(1)])
    second = _task(config, "Second", [_cid(2), _cid(3)])

    resolved = resolve_completions(config, "work", [_cid(1), _cid(3)])

    assert resolved == {
        _cid(1): Outcome(completion_id=_cid(1), task_id=first),
        _cid(3): Outcome(completion_id=_cid(3), task_id=second),
    }


def test_an_id_no_task_file_holds_resolves_to_none(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _task(config, "Only", [_cid(1)])

    resolved = resolve_completions(config, "work", [_cid(1), _cid(99)])

    assert resolved[_cid(1)] is not None
    assert resolved[_cid(99)] is None


def test_ids_beyond_the_cap_are_never_read(tmp_path: Path) -> None:
    """The cap is applied to the ids asked for, so the list is not silently widened.

    Twenty ids are asked for and the real ones sit past the sixteenth. A lookup
    that resolved everything would report them; the bound must leave them out.
    """
    config = _config(tmp_path)
    _task(config, "Real", [_cid(n) for n in range(100, 118)])
    asked = [_cid(n) for n in range(0, 16)] + [_cid(n) for n in range(100, 104)]

    resolved = resolve_completions(config, "work", asked)

    assert len(resolved) == recurrence_evidence.MAX_IDS == 16
    assert set(resolved) == set(asked[:16])
    assert all(resolved[cid] is None for cid in asked[:16])


def test_an_absent_task_directory_resolves_nothing_without_raising(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert resolve_completions(config, "work", [_cid(1)]) == {_cid(1): None}


def test_a_malformed_task_file_makes_the_history_unavailable(tmp_path: Path) -> None:
    """An unreadable task could hold the completion; it must never read as absent."""
    config = _config(tmp_path)
    _task(config, "Good", [_cid(1)])
    tasks = Path(config.workspace_vault_root("work")) / "Workspace" / "Tasks"
    (tasks / f"{_cid(7)}.md").write_text("not a task at all\n", encoding="utf-8")

    with pytest.raises(EvidenceUnavailable, match="unreadable"):
        resolve_completions(config, "work", [_cid(1)])


def test_a_malformed_decisions_file_makes_the_history_unavailable(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _task(config, "Good", [_cid(1)])
    decisions = Path(config.workspace_vault_root("work")) / "Workspace" / "Task-Decisions.jsonl"
    decisions.write_text("{this is not a decision row\n", encoding="utf-8")

    with pytest.raises(EvidenceUnavailable, match="decision"):
        resolve_completions(config, "work", [_cid(1)])


def test_too_many_task_files_is_unavailable_not_a_partial_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    for n in range(3):
        _task(config, f"Task {n}", [_cid(n)])
    monkeypatch.setattr(recurrence_evidence, "MAX_TASK_FILES", 2)

    with pytest.raises(EvidenceUnavailable, match="task files"):
        resolve_completions(config, "work", [_cid(0)])


def test_no_ids_asked_reads_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert resolve_completions(config, "work", []) == {}
