"""Progress notes on a task file, and how a delegation prompt quotes them."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ciao.task_attempts import build_prompt, build_resume_prompt
from ciao.task_board import TaskBoardError, TaskBoardStore
from ciao.task_updates import (
    CLOSE,
    OPEN,
    TaskUpdate,
    append_update,
    parse_updates,
    unseen_updates,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _store(tmp_path: Path) -> TaskBoardStore:
    vault = tmp_path / "vault"
    runtime = tmp_path / "runtime"
    vault.mkdir()
    runtime.mkdir()
    return TaskBoardStore(
        workspace="work", vault_root=vault, runtime_dir=runtime, clock=Clock()
    )


def test_progress_round_trips_and_stays_out_of_the_description(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.create(title="Ship the board", body="The work that was asked for.")
    moved = store.update(
        created.record.id,
        expected_revision=created.revision,
        changes={"status": "in_progress"},
        actor="user",
    )
    written = store.add_update(
        moved.record.id,
        expected_revision=moved.revision,
        actor="user",
        text="Schema is in.\nWaiting on credentials.",
    )
    notes = parse_updates(written.body)
    assert len(notes) == 1
    assert notes[0].actor == "user"
    assert notes[0].text == "Schema is in.\nWaiting on credentials."
    assert OPEN in written.body and CLOSE in written.body

    saved = store.update(
        written.record.id,
        expected_revision=written.revision,
        changes={},
        body="A rewritten description.",
        actor="user",
    )
    assert "A rewritten description." in saved.body
    assert parse_updates(saved.body)[0].text == "Schema is in.\nWaiting on credentials."
    assert "Schema is in." not in saved.body.split(OPEN)[0]


def test_progress_is_refused_off_the_active_columns(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.create(title="Still to do", body="Not started.")
    with pytest.raises(TaskBoardError) as refused:
        store.add_update(
            created.record.id,
            expected_revision=created.revision,
            actor="user",
            text="Started anyway.",
        )
    assert refused.value.code == "invalid_task"
    reread = store.get(created.record.id)
    assert reread.revision == created.revision
    assert parse_updates(reread.body) == []


def test_a_stale_revision_writes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.create(title="In flight", body="Do the thing.")
    moved = store.update(
        created.record.id,
        expected_revision=created.revision,
        changes={"status": "in_review"},
        actor="user",
    )
    with pytest.raises(TaskBoardError) as refused:
        store.add_update(
            moved.record.id,
            expected_revision=created.revision,
            actor="agent",
            text="Too late.",
        )
    assert refused.value.code == "revision_conflict"
    assert parse_updates(store.get(moved.record.id).body) == []


def test_edit_keeps_the_author_and_recorded_time(tmp_path: Path) -> None:
    store = _store(tmp_path)
    created = store.create(title="In flight", body="Do the thing.")
    moved = store.update(
        created.record.id,
        expected_revision=created.revision,
        changes={"status": "in_progress"},
        actor="user",
    )
    written = store.add_update(
        moved.record.id,
        expected_revision=moved.revision,
        actor="agent",
        text="First wording.",
        attempt_id="attempt-1",
        chat_id="chat-1",
    )
    original = parse_updates(written.body)[0]
    edited = store.edit_update(
        written.record.id,
        original.id,
        expected_revision=written.revision,
        text="Clearer wording.",
    )
    note = parse_updates(edited.body)[0]
    assert note.id == original.id
    assert note.actor == "agent"
    assert note.recorded_at == original.recorded_at
    assert note.text == "Clearer wording."
    assert note.edited_at is not None
    assert note.attempt_id == "attempt-1"


def test_delegation_quotes_progress_after_the_description() -> None:
    when = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    body = append_update(
        "Describe the work.",
        TaskUpdate(id="a" * 32, recorded_at=when, actor="user", text="Halfway."),
    )
    prompt = build_prompt(
        title="Ship it",
        status="in_progress",
        due="",
        project_id="",
        task_id="b" * 32,
        task_revision="c" * 64,
        relative_path="Workspace/Tasks/x.md",
        body=body,
    )
    description = prompt.split("<task-board-task>", 1)[1].split("</task-board-task>", 1)[0]
    assert "Describe the work." in description
    assert "Halfway." not in description
    progress = prompt.split("</task-board-task>", 1)[1]
    assert "<task-progress>" in progress
    assert "Halfway." in progress
    assert "user" in progress

    resume = build_resume_prompt(
        title="Ship it",
        state="interrupted",
        task_id="b" * 32,
        updates=tuple(reversed(parse_updates(body))),
    )
    assert "Halfway." in resume
    bare = build_resume_prompt(title="Ship it", state="interrupted", task_id="b" * 32)
    assert "Halfway." not in bare


def test_unseen_notes_are_the_ones_after_the_cursor() -> None:
    when = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    first = TaskUpdate(id="a" * 32, recorded_at=when, actor="user", text="First.")
    second = TaskUpdate(id="b" * 32, recorded_at=when, actor="agent", text="Second.")
    body = append_update(append_update("Work.", first), second)
    assert [note.text for note in unseen_updates(body, "")] == ["First.", "Second."]
    assert [note.text for note in unseen_updates(body, first.id)] == ["Second."]
    assert unseen_updates(body, second.id) == []
    assert [note.text for note in unseen_updates(body, "f" * 32)] == ["First.", "Second."]
