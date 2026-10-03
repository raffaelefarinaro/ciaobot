"""Tests for ``ciao.task_board``.

The store is inert file handling, so every test runs against throwaway
vault/runtime directories with an injected clock: no live vaults, no
models, no services. The order follows the plan — identity and defaults,
ordering, dates, source preservation, revisions, confinement, and the
managed-operation rules — because that is the order in which a failure
explains itself.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ciao.task_board import (
    MAX_TASK_BYTES,
    TaskBoardError,
    TaskBoardStore,
    parse_task,
    patch_task,
)


class Clock:
    """An injected clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def make_store(
    vault: Path, runtime: Path, clock: Clock, workspace: str = "work"
) -> TaskBoardStore:
    return TaskBoardStore(
        workspace=workspace, vault_root=vault, runtime_dir=runtime, clock=clock
    )


def tasks_dir(vault: Path) -> Path:
    return vault / "Workspace" / "Tasks"


def task_path(vault: Path, task_id: str) -> Path:
    return tasks_dir(vault) / f"{task_id}.md"


def hand_file(
    vault: Path,
    task_id: str,
    frontmatter_lines: list[str],
    body: str = "",
    *,
    newline: str = "\n",
    bom: bool = False,
) -> Path:
    """Write a hand-edited task file verbatim (no store involved)."""
    tasks_dir(vault).mkdir(parents=True, exist_ok=True)
    text = "---" + newline + newline.join(frontmatter_lines) + newline + "---" + newline + body
    raw = text.encode("utf-8")
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    path = task_path(vault, task_id)
    path.write_bytes(raw)
    return path


def valid_frontmatter(
    task_id: str,
    *,
    title: str = "A task",
    status: str = "backlog",
    assignee: str = "user",
    review: str = "none",
    due: str | None = None,
    project: str | None = None,
    chat: str | None = None,
    attempt: str | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    def opt(value: str | None) -> str:
        return "null" if value is None else value

    lines = [
        "schema: 1",
        f"id: {task_id}",
        f"title: {title}",
        f"status: {status}",
        f"project_id: {opt(project)}",
        f"due: {opt(due)}",
        f"assignee: {assignee}",
        f"review_state: {review}",
        'created_at: "2026-10-03T12:00:00+00:00"',
        'updated_at: "2026-10-03T12:00:00+00:00"',
        f"chat_id: {opt(chat)}",
        f"attempt_id: {opt(attempt)}",
    ]
    lines.extend(extra or [])
    return lines


# ── Identity and defaults ────────────────────────────────────────────


def test_empty_list_does_not_create_directory(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    runtime = tmp_path / "runtime"
    store = make_store(vault, runtime, Clock())
    result = store.list()
    assert result.tasks == ()
    assert result.invalid == ()
    assert not (vault / "Workspace" / "Tasks").exists()


def test_create_get_and_list_have_stable_identity_and_defaults(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    created = store.create(title="  First task  ", body="Do the thing.")
    assert len(created.record.id) == 32
    assert all(char in "0123456789abcdef" for char in created.record.id)
    assert created.relative_path == f"Workspace/Tasks/{created.record.id}.md"
    assert task_path(vault, created.record.id).exists()
    # Filename and frontmatter id agree; title is trimmed.
    assert created.record.title == "First task"
    assert created.record.status == "backlog"
    assert created.record.assignee == "user"
    assert created.record.review_state == "none"
    assert created.record.project_id is None
    assert created.record.due is None
    assert created.record.chat_id is None
    assert created.record.attempt_id is None
    assert created.record.created_at == created.record.updated_at
    assert created.body == "Do the thing."
    assert created.revision == hashlib.sha256(created.raw).hexdigest()

    fetched = store.get(created.record.id)
    assert fetched.record == created.record
    assert fetched.revision == created.revision

    result = store.list()
    assert [document.record.id for document in result.tasks] == [created.record.id]
    assert result.invalid == ()


def test_sort_is_due_date_then_creation_and_id(tmp_path: Path) -> None:
    clock = Clock()
    store = make_store(tmp_path / "vault", tmp_path / "runtime", clock)
    undated = store.create(title="undated")
    clock.now = datetime(2026, 10, 3, 12, 1, tzinfo=UTC)
    later_due = store.create(title="later due", due="2026-11-01")
    clock.now = datetime(2026, 10, 3, 12, 2, tzinfo=UTC)
    earlier_due = store.create(title="earlier due", due="2026-10-05")
    clock.now = datetime(2026, 10, 3, 12, 3, tzinfo=UTC)
    # Same due date and same creation instant: id breaks the tie.
    first_tie = store.create(title="tie one", due="2026-10-05")
    second_tie = store.create(title="tie two", due="2026-10-05")

    result = store.list()
    ids = [document.record.id for document in result.tasks]
    tie = sorted([first_tie.record.id, second_tie.record.id])
    assert ids == [earlier_due.record.id, *tie, later_due.record.id, undated.record.id]


def test_dates_are_calendar_dates_not_schedule_instants(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    quoted = store.create(title="quoted", due="2024-02-29")
    assert quoted.record.due == "2024-02-29"
    assert isinstance(quoted.record.due, str)

    task_id = "a" * 32
    # An unquoted YAML date scalar normalizes to ISO for the typed record.
    hand_file(vault, task_id, valid_frontmatter(task_id, due="2026-01-05"))
    assert store.get(task_id).record.due == "2026-01-05"

    for bad in ("2026-02-30", "2026-13-01", "not-a-date", "2026-1-5", "10:00"):
        with pytest.raises(TaskBoardError):
            store.create(title="bad", due=bad)
    # A datetime is never a due date, neither quoted nor bare.
    bad_id = "b" * 32
    hand_file(
        vault,
        bad_id,
        valid_frontmatter(bad_id, due="2026-01-05T10:00:00+00:00"),
    )
    with pytest.raises(TaskBoardError) as excinfo:
        store.get(bad_id)
    assert excinfo.value.code == "invalid_task"


# ── Source preservation ──────────────────────────────────────────────


@pytest.mark.parametrize("bom", [False, True])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_patch_preserves_unknown_metadata_comments_body_bom_and_crlf(
    tmp_path: Path, bom: bool, newline: str
) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    task_id = "c" * 32
    body = "See [the spec](https://example.test/spec) for details."
    extra = [
        "# a hand-written comment",
        "custom: keep me",
        "nested:",
        "  deep: true",
        "  items: [1, 2]",
        "# trailing comment",
    ]
    hand_file(
        vault,
        task_id,
        valid_frontmatter(task_id, title="Old title", extra=extra),
        body=body,
        newline=newline,
        bom=bom,
    )
    before = task_path(vault, task_id).read_bytes()
    document = store.get(task_id)

    updated = store.update(
        task_id,
        expected_revision=document.revision,
        changes={"title": "New title"},
        actor="user",
    )
    after = task_path(vault, task_id).read_bytes()
    # The clock did not advance, so updated_at is an idempotent no-op and
    # exactly one frontmatter line — the title span — changed.
    assert updated.record.title == "New title"
    assert updated.record.id == task_id
    before_lines = before.split(newline.encode())
    after_lines = after.split(newline.encode())
    assert len(before_lines) == len(after_lines)
    changed = [
        index
        for index, (old, new) in enumerate(zip(before_lines, after_lines))
        if old != new
    ]
    assert len(changed) == 1
    assert b"title:" in after_lines[changed[0]]

    assert after.startswith(b"\xef\xbb\xbf") == bom
    assert b"# a hand-written comment" in after
    assert b"# trailing comment" in after
    assert b"custom: keep me" in after
    assert b"nested:" in after
    assert after.endswith(body.encode())
    if newline == "\r\n":
        assert b"\n" in after
        # No lone LF outside CRLF pairs: every newline byte belongs to one.
        assert after.replace(b"\r\n", b"").find(b"\n") == -1


def test_description_and_links_change_only_on_explicit_body_update(tmp_path: Path) -> None:
    store = make_store(tmp_path / "vault", tmp_path / "runtime", Clock())
    created = store.create(title="T", body="See [a](https://a.test).")
    metadata_only = store.update(
        created.record.id,
        expected_revision=created.revision,
        changes={"title": "T2"},
        actor="user",
    )
    assert metadata_only.body == "See [a](https://a.test)."
    assert b"[a](https://a.test)" in metadata_only.raw

    noop = store.update(
        created.record.id,
        expected_revision=metadata_only.revision,
        changes={},
        actor="user",
    )
    assert noop.revision == metadata_only.revision

    replaced = store.update(
        created.record.id,
        expected_revision=metadata_only.revision,
        changes={},
        body="See [b](https://b.test).",
        actor="user",
    )
    assert replaced.body == "See [b](https://b.test)."
    assert b"[b](https://b.test)" in replaced.raw
    assert b"[a](https://a.test)" not in replaced.raw
    assert replaced.record.title == "T2"


def test_title_edit_never_renames_identity(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    created = store.create(title="Original")
    updated = store.update(
        created.record.id,
        expected_revision=created.revision,
        changes={"title": "Renamed three times over"},
        actor="user",
    )
    assert updated.record.id == created.record.id
    assert updated.relative_path == created.relative_path
    assert list(tasks_dir(vault).iterdir()) == [task_path(vault, created.record.id)]
    assert store.get(created.record.id).record.title == "Renamed three times over"


def test_invalid_duplicate_truncated_or_unsupported_metadata_is_visible_and_untouched(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    healthy = store.create(title="healthy")
    dup_id = "d" * 32
    dup_lines = valid_frontmatter(dup_id)
    dup_lines.insert(2, "title: second title for the same key")
    hand_file(vault, dup_id, dup_lines)
    trunc_id = "e" * 32
    task_path(vault, trunc_id).parent.mkdir(parents=True, exist_ok=True)
    task_path(vault, trunc_id).write_bytes(b"---\nschema: 1\nid: " + trunc_id.encode() + b"\n")
    schema_id = "f" * 32
    hand_file(vault, schema_id, valid_frontmatter(schema_id).copy())
    schema_raw = task_path(vault, schema_id).read_bytes().replace(b"schema: 1", b"schema: 2")
    task_path(vault, schema_id).write_bytes(schema_raw)
    alias_id = "0" * 32
    alias_lines = valid_frontmatter(alias_id, extra=["anchor: &x 1", "copied: *x"])
    hand_file(vault, alias_id, alias_lines)
    enum_id = "1" * 32
    enum_lines = valid_frontmatter(enum_id)
    enum_lines[3] = "status: someday"
    hand_file(vault, enum_id, enum_lines)

    result = store.list()
    assert [document.record.id for document in result.tasks] == [healthy.record.id]
    by_path = {entry.relative_path: entry for entry in result.invalid}
    assert len(by_path) == 5
    assert by_path[f"Workspace/Tasks/{dup_id}.md"].code == "invalid_task"
    assert by_path[f"Workspace/Tasks/{trunc_id}.md"].code == "invalid_task"
    assert by_path[f"Workspace/Tasks/{schema_id}.md"].code == "unsupported_schema"
    assert by_path[f"Workspace/Tasks/{alias_id}.md"].code == "invalid_task"
    assert by_path[f"Workspace/Tasks/{enum_id}.md"].code == "invalid_task"

    # A refused mutation leaves the file byte-identical.
    for bad_id in (dup_id, trunc_id, schema_id, alias_id, enum_id):
        path = task_path(vault, bad_id)
        before = path.read_bytes()
        with pytest.raises(TaskBoardError):
            store.update(
                bad_id,
                expected_revision=hashlib.sha256(before).hexdigest(),
                changes={"title": "nope"},
                actor="user",
            )
        assert path.read_bytes() == before


# ── Revisions and concurrency ────────────────────────────────────────


def test_stale_revision_and_write_failure_preserve_original_bytes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    created = store.create(title="v1", body="body v1")
    moved = store.update(
        created.record.id,
        expected_revision=created.revision,
        changes={"title": "v2"},
        actor="user",
    )
    path = task_path(vault, created.record.id)
    with pytest.raises(TaskBoardError) as excinfo:
        store.update(
            created.record.id,
            expected_revision=created.revision,
            changes={"title": "stale"},
            actor="user",
        )
    assert excinfo.value.code == "revision_conflict"
    assert path.read_bytes() == moved.raw


def test_write_failure_preserves_original_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ciao.task_board as task_board

    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    created = store.create(title="v1")

    def fail_replace(src: object, dst: object) -> None:
        raise OSError("disk is full (synthetic)")

    monkeypatch.setattr(task_board, "replace_file", fail_replace)
    with pytest.raises(TaskBoardError):
        store.update(
            created.record.id,
            expected_revision=created.revision,
            changes={"title": "v2"},
            actor="user",
        )
    assert task_path(vault, created.record.id).read_bytes() == created.raw
    # No stray temp file from the failed write is left behind.
    leftovers = [
        entry.name
        for entry in tasks_dir(vault).iterdir()
        if entry.name != f"{created.record.id}.md"
    ]
    assert leftovers == []


def test_multiple_store_instances_keep_unrelated_tasks_and_detect_stale_edits(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    runtime = tmp_path / "runtime"
    clock = Clock()
    first = make_store(vault, runtime, clock)
    second = make_store(vault, runtime, clock)
    one = first.create(title="one")
    two = first.create(title="two")

    stale = first.get(one.record.id)
    fresh = second.update(
        one.record.id,
        expected_revision=stale.revision,
        changes={"title": "one edited elsewhere"},
        actor="user",
    )
    assert fresh.record.title == "one edited elsewhere"
    with pytest.raises(TaskBoardError) as excinfo:
        first.update(
            one.record.id,
            expected_revision=stale.revision,
            changes={"title": "stale overwrite"},
            actor="user",
        )
    assert excinfo.value.code == "revision_conflict"
    # The unrelated task survived two writers without a scratch.
    assert first.get(two.record.id).record.title == "two"
    assert second.get(two.record.id).revision == two.revision


def test_hand_edit_is_seen_on_next_read(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    created = store.create(title="typed", body="old body")
    path = task_path(vault, created.record.id)
    edited = path.read_text(encoding="utf-8").replace("title: typed", "title: hand edited")
    edited = edited.replace("old body", "new body with [link](https://example.test)")
    path.write_text(edited, encoding="utf-8")

    fetched = store.get(created.record.id)
    assert fetched.record.title == "hand edited"
    assert fetched.body == "new body with [link](https://example.test)"
    assert fetched.revision != created.revision
    assert store.list().tasks[0].record.title == "hand edited"


# ── Confinement ──────────────────────────────────────────────────────


def test_unsafe_ids_symlinks_and_oversized_files_are_refused(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    for bad_id in ("", "../escape", "a" * 31, "a" * 33, "G" * 32, "ABC", "with space"):
        with pytest.raises(TaskBoardError) as excinfo:
            store.get(bad_id)
        assert excinfo.value.code == "unsafe_path"
        with pytest.raises(TaskBoardError) as excinfo:
            store.update(bad_id, expected_revision="x", changes={}, actor="user")
        assert excinfo.value.code == "unsafe_path"

    real = store.create(title="real")
    link_id = "2" * 32
    tasks_dir(vault).mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(task_path(vault, real.record.id), task_path(vault, link_id))
    except OSError:
        pytest.skip("this platform could not create a symlink for the test")
    with pytest.raises(TaskBoardError) as excinfo:
        store.get(link_id)
    assert excinfo.value.code == "unsafe_path"
    result = store.list()
    assert [document.record.id for document in result.tasks] == [real.record.id]
    codes = {entry.relative_path: entry.code for entry in result.invalid}
    assert codes.get(f"Workspace/Tasks/{link_id}.md") == "unsafe_path"

    big_id = "3" * 32
    task_path(vault, big_id).write_bytes(b"x" * (MAX_TASK_BYTES + 1))
    with pytest.raises(TaskBoardError):
        store.get(big_id)
    assert f"Workspace/Tasks/{big_id}.md" in {
        entry.relative_path for entry in store.list().invalid
    }


def test_store_never_reads_sibling_workspace(tmp_path: Path) -> None:
    clock = Clock()
    first = make_store(tmp_path / "vault-a", tmp_path / "runtime", clock, workspace="a")
    second = make_store(tmp_path / "vault-b", tmp_path / "runtime", clock, workspace="b")
    created = second.create(title="belongs to b")
    assert first.list().tasks == ()
    assert first.list().invalid == ()
    with pytest.raises(TaskBoardError) as excinfo:
        first.get(created.record.id)
    assert excinfo.value.code == "not_found"
    assert [document.record.id for document in second.list().tasks] == [created.record.id]


# ── Managed-operation rules ──────────────────────────────────────────


def test_agent_cannot_complete_and_user_can_complete_manual_task(tmp_path: Path) -> None:
    store = make_store(tmp_path / "vault", tmp_path / "runtime", Clock())
    created = store.create(title="manual work")
    path = task_path(tmp_path / "vault", created.record.id)
    with pytest.raises(TaskBoardError) as excinfo:
        store.update(
            created.record.id,
            expected_revision=created.revision,
            changes={"status": "done"},
            actor="agent",
        )
    assert excinfo.value.code == "completion_requires_user"
    assert path.read_bytes() == created.raw

    # The agent rule is about completion, not about every agent edit.
    renamed = store.update(
        created.record.id,
        expected_revision=created.revision,
        changes={"title": "manual work, clarified"},
        actor="agent",
    )
    done = store.update(
        created.record.id,
        expected_revision=renamed.revision,
        changes={"status": "done"},
        actor="user",
    )
    assert done.record.status == "done"
    assert done.record.chat_id is None and done.record.attempt_id is None


def test_ready_requires_agent_in_progress_and_linked_live_completion_is_refused(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    created = store.create(title="ops work")
    # Ready on a backlog task owned by the user is refused.
    with pytest.raises(TaskBoardError) as excinfo:
        store.update(
            created.record.id,
            expected_revision=created.revision,
            changes={"review_state": "ready"},
            actor="user",
        )
    assert excinfo.value.code == "invalid_task"

    in_progress = store.update(
        created.record.id,
        expected_revision=created.revision,
        changes={"status": "in_progress", "assignee": "agent"},
        actor="user",
    )
    ready = store.update(
        created.record.id,
        expected_revision=in_progress.revision,
        changes={"review_state": "ready"},
        actor="user",
    )
    assert ready.record.review_state == "ready"
    # Leaving In progress without an explicit review change clears ready.
    parked = store.update(
        created.record.id,
        expected_revision=ready.revision,
        changes={"status": "on_hold"},
        actor="user",
    )
    assert parked.record.status == "on_hold"
    assert parked.record.review_state == "none"

    # A task linked to a live attempt cannot be completed or reassigned
    # through this API, by either actor; the bytes stay untouched.
    linked = store.create(title="live work")
    link_path = task_path(vault, linked.record.id)
    linked_raw = link_path.read_text(encoding="utf-8").replace(
        "attempt_id: null", "attempt_id: att-1"
    )
    link_path.write_text(linked_raw, encoding="utf-8")
    current = store.get(linked.record.id)
    assert current.record.attempt_id == "att-1"
    for actor in ("user", "agent"):
        with pytest.raises(TaskBoardError):
            store.update(
                linked.record.id,
                expected_revision=current.revision,
                changes={"status": "done"},
                actor=actor,  # type: ignore[arg-type]
            )
        with pytest.raises(TaskBoardError) as excinfo:
            store.update(
                linked.record.id,
                expected_revision=current.revision,
                changes={"assignee": "agent"},
                actor=actor,  # type: ignore[arg-type]
            )
        assert excinfo.value.code == "invalid_task"
    assert link_path.read_bytes() == current.raw


def test_patch_rejects_uneditable_linkage_fields(tmp_path: Path) -> None:
    store = make_store(tmp_path / "vault", tmp_path / "runtime", Clock())
    created = store.create(title="t")
    for field in ("schema", "id", "created_at", "chat_id", "attempt_id"):
        with pytest.raises(TaskBoardError) as excinfo:
            patch_task(created, {field: "x"})
        assert excinfo.value.code == "invalid_task"


# ── Review round 1 fixes ─────────────────────────────────────────────


def test_bare_invalid_date_is_invalid_not_a_crash(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    task_id = "a" * 32
    lines = valid_frontmatter(task_id)
    lines[5] = "due: 2026-13-45"
    hand_file(vault, task_id, lines)
    with pytest.raises(TaskBoardError) as excinfo:
        store.get(task_id)
    assert excinfo.value.code == "invalid_task"
    result = store.list()
    assert result.tasks == ()
    assert len(result.invalid) == 1
    assert result.invalid[0].relative_path == f"Workspace/Tasks/{task_id}.md"
    assert result.invalid[0].code == "invalid_task"


def test_control_char_title_is_refused_without_writing(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    with pytest.raises(TaskBoardError) as excinfo:
        store.create(title="a\x7fb")
    assert excinfo.value.code == "invalid_task"
    with pytest.raises(TaskBoardError) as excinfo:
        store.create(title="ok", project_id="a\x7f")
    assert excinfo.value.code == "invalid_task"
    tasks = vault / "Workspace" / "Tasks"
    assert not tasks.exists() or list(tasks.iterdir()) == []


def test_quoted_merge_key_title_is_accepted(tmp_path: Path) -> None:
    store = make_store(tmp_path / "vault", tmp_path / "runtime", Clock())
    created = store.create(title="<<")
    assert created.record.title == "<<"
    assert store.get(created.record.id).record.title == "<<"
    assert store.list().invalid == ()


def test_symlinked_workspace_is_refused(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(outside, vault / "Workspace")
    except OSError:
        pytest.skip("this platform could not create a symlink for the test")
    store = make_store(vault, tmp_path / "runtime", Clock())
    with pytest.raises(TaskBoardError) as excinfo:
        store.create(title="escape")
    assert excinfo.value.code == "unsafe_path"
    with pytest.raises(TaskBoardError) as excinfo:
        store.list()
    assert excinfo.value.code == "unsafe_path"
    with pytest.raises(TaskBoardError) as excinfo:
        store.get("a" * 32)
    assert excinfo.value.code == "unsafe_path"
    assert list(outside.iterdir()) == []


def test_atomic_temp_files_are_invisible_to_list(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    created = store.create(title="real")
    tasks_dir(vault).joinpath(f".{created.record.id}.md.abc123.tmp").write_bytes(b"junk")
    result = store.list()
    assert [document.record.id for document in result.tasks] == [created.record.id]
    assert result.invalid == ()


def test_patch_empty_due_value(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    task_id = "b" * 32
    lines = valid_frontmatter(task_id)
    lines[5] = "due:"
    hand_file(vault, task_id, lines)
    document = store.get(task_id)
    assert document.record.due is None
    before = task_path(vault, task_id).read_bytes().split(b"\n")
    updated = store.update(
        task_id,
        expected_revision=document.revision,
        changes={"due": "2026-10-05"},
        actor="user",
    )
    assert updated.record.due == "2026-10-05"
    after = task_path(vault, task_id).read_bytes().split(b"\n")
    assert len(before) == len(after)
    changed = [index for index, (old, new) in enumerate(zip(before, after)) if old != new]
    assert len(changed) == 1
    assert b"2026-10-05" in after[changed[0]]
    assert after[changed[0]].startswith(b"due:")


def test_indented_delimiter_inside_block_scalar_survives(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    task_id = "c" * 32
    hand_file(
        vault,
        task_id,
        valid_frontmatter(
            task_id,
            title="Old",
            extra=["notes: |", "  keep this", "  ---", "  and this"],
        ),
    )
    document = store.get(task_id)
    assert document.record.title == "Old"
    before = task_path(vault, task_id).read_bytes().split(b"\n")
    updated = store.update(
        task_id,
        expected_revision=document.revision,
        changes={"title": "New"},
        actor="user",
    )
    assert updated.record.title == "New"
    after = task_path(vault, task_id).read_bytes().split(b"\n")
    assert b"  ---" in task_path(vault, task_id).read_bytes()
    assert len(before) == len(after)
    changed = [index for index, (old, new) in enumerate(zip(before, after)) if old != new]
    assert len(changed) == 1
    assert b"title:" in after[changed[0]]
