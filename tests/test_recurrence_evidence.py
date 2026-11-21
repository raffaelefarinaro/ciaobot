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
from ciao.recurrence_evidence import EvidenceUnavailable, Lookup, resolve_completions
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


def _outcomes(config: CiaoConfig, ids: list[str]) -> dict[str, Outcome | None]:
    return resolve_completions(config, "work", ids).outcomes


def _tasks_dir(config: CiaoConfig) -> Path:
    return Path(config.workspace_vault_root("work")) / "Workspace" / "Tasks"


def test_persisted_completions_resolve_to_the_task_that_holds_them(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = _task(config, "First", [_cid(1)])
    second = _task(config, "Second", [_cid(2), _cid(3)])

    lookup = resolve_completions(config, "work", [_cid(1), _cid(3)])

    assert isinstance(lookup, Lookup)
    assert lookup.unreadable is False
    assert lookup.outcomes == {
        _cid(1): Outcome(completion_id=_cid(1), task_id=first),
        _cid(3): Outcome(completion_id=_cid(3), task_id=second),
    }


def test_an_id_no_task_file_holds_resolves_to_none(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _task(config, "Only", [_cid(1)])

    resolved = _outcomes(config, [_cid(1), _cid(99)])

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

    resolved = _outcomes(config, asked)

    assert len(resolved) == recurrence_evidence.MAX_IDS == 16
    assert set(resolved) == set(asked[:16])
    assert all(resolved[cid] is None for cid in asked[:16])


def test_an_absent_task_directory_resolves_nothing_without_raising(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert _outcomes(config, [_cid(1)]) == {_cid(1): None}


def test_a_truncated_task_named_file_is_reported_unreadable_not_raised(tmp_path: Path) -> None:
    """The file could hold a cited id, so the lookup says so; the caller decides."""
    config = _config(tmp_path)
    _task(config, "Good", [_cid(1)])
    (_tasks_dir(config) / f"{_cid(7)}.md").write_text("not a task at all\n", encoding="utf-8")

    lookup = resolve_completions(config, "work", [_cid(1), _cid(2)])

    assert lookup.unreadable is True
    assert lookup.outcomes[_cid(1)] is not None
    assert lookup.outcomes[_cid(2)] is None


def test_a_stray_file_and_an_unrelated_truncated_task_do_not_mark_it_unreadable_for_a_clean_hit(
    tmp_path: Path,
) -> None:
    """Non-task files are ignored; a truncated task still only sets the flag.

    The flag is what the caller reads, and it is set here because a truncated
    task-named file exists. Every cited id is still resolved from the readable
    tasks, so a caller whose verdict is met is never blocked by it.
    """
    config = _config(tmp_path)
    _task(config, "Good", [_cid(1)])
    (_tasks_dir(config) / "README.md").write_text("notes\n", encoding="utf-8")
    (_tasks_dir(config) / "backup.md.bak").write_text("x\n", encoding="utf-8")

    lookup = resolve_completions(config, "work", [_cid(1)])

    assert lookup.unreadable is False
    assert lookup.outcomes[_cid(1)] is not None


def test_a_malformed_decisions_file_does_not_block_the_lookup(tmp_path: Path) -> None:
    """The decisions file is not needed to find a completion; it is not read here."""
    config = _config(tmp_path)
    first = _task(config, "Good", [_cid(1)])
    decisions = Path(config.workspace_vault_root("work")) / "Workspace" / "Task-Decisions.jsonl"
    decisions.write_text("{this is not a decision row\n", encoding="utf-8")

    lookup = resolve_completions(config, "work", [_cid(1)])

    assert lookup.outcomes[_cid(1)] == Outcome(completion_id=_cid(1), task_id=first)


def test_too_many_task_named_files_is_unavailable_and_other_md_files_do_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    for n in range(2):
        _task(config, f"Task {n}", [_cid(n)])
    for name in ("README.md", "NOTES.md", "a.md"):
        (_tasks_dir(config) / name).write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(recurrence_evidence, "MAX_TASK_FILES", 2)

    # Two task files and three other .md files: the cap counts task files only.
    assert resolve_completions(config, "work", [_cid(0)]).outcomes[_cid(0)] is not None

    _task(config, "Task 2", [_cid(2)])
    with pytest.raises(EvidenceUnavailable, match="task files"):
        resolve_completions(config, "work", [_cid(0)])


def test_no_ids_asked_reads_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    lookup = resolve_completions(config, "work", [])
    assert lookup == Lookup({}, unreadable=False)
