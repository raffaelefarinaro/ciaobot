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

from ciao.task_attempts import build_prompt
from ciao.task_log import LOG_CLOSE, LOG_OPEN, render_item, upsert_item
from ciao.task_board import (
    MAX_TASK_BYTES,
    TaskBoardError,
    TaskBoardStore,
    parse_task,
    patch_task,
)
from ciao.task_resolution import (
    CLOSE as COMPLETIONS_CLOSE,
    OPEN as COMPLETIONS_OPEN,
    Completion,
    append_completion,
    extract_section,
    parse_completions,
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
    due: str | None = None,
    project: str | None = None,
    chat: str | None = None,
    attempt: str | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    def opt(value: str | None) -> str:
        return "null" if value is None else value

    lines = [
        "schema: 2",
        f"id: {task_id}",
        f"title: {title}",
        f"status: {status}",
        f"project_id: {opt(project)}",
        f"due: {opt(due)}",
        f"assignee: {assignee}",
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
    task_path(vault, trunc_id).write_bytes(b"---\nschema: 2\nid: " + trunc_id.encode() + b"\n")
    schema_id = "f" * 32
    hand_file(vault, schema_id, valid_frontmatter(schema_id).copy())
    schema_raw = task_path(vault, schema_id).read_bytes().replace(b"schema: 2", b"schema: 3")
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


def test_in_review_is_an_ordinary_column_and_linked_live_completion_is_refused(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    created = store.create(title="ops work")
    # Review is a column (#1069): no separate flag, no pairing rule.
    with pytest.raises(TaskBoardError) as excinfo:
        store.update(
            created.record.id,
            expected_revision=created.revision,
            changes={"review_state": "ready"},
            actor="user",
        )
    assert excinfo.value.code == "invalid_task"
    in_review = store.update(
        created.record.id,
        expected_revision=created.revision,
        changes={"status": "in_review"},
        actor="agent",
    )
    assert in_review.record.status == "in_review"
    with pytest.raises(TaskBoardError) as excinfo:
        store.update(
            created.record.id,
            expected_revision=in_review.revision,
            changes={"status": "on_hold"},
            actor="user",
        )
    assert excinfo.value.code == "invalid_task"

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


# ── Removal (#1021, child B3 of #973) ────────────────────────────────────
#
# `DELETE /api/tasks/{task_id}` needs a store removal, which B1 deliberately
# left out. It is added here rather than as an unlink beside the store: removal
# has to take the same workspace lock, the same no-follow read (so a link where
# the file must be is refused rather than followed), the same id validation and
# the same revision check as every other managed write.


def test_delete_removes_the_record_and_is_then_a_not_found(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    kept = store.create(title="kept")
    removed = store.create(title="removed")

    store.delete(removed.record.id, expected_revision=removed.revision)

    assert not task_path(vault, removed.record.id).exists()
    with pytest.raises(TaskBoardError) as excinfo:
        store.get(removed.record.id)
    assert excinfo.value.code == "not_found"
    # Only this store's task went; nothing else in the directory was touched.
    assert [document.record.id for document in store.list().tasks] == [kept.record.id]
    assert task_path(vault, kept.record.id).exists()

    with pytest.raises(TaskBoardError) as again:
        store.delete(removed.record.id, expected_revision=removed.revision)
    assert again.value.code == "not_found"


def test_delete_needs_the_current_revision_and_writes_nothing_when_stale(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    clock = Clock()
    store = make_store(vault, tmp_path / "runtime", clock)
    created = store.create(title="about to change")

    # No revision at all is refused before the file is even opened: this store
    # never removes a task nobody read.
    with pytest.raises(TaskBoardError) as excinfo:
        store.delete(created.record.id, expected_revision="")
    assert excinfo.value.code == "invalid_task"
    assert task_path(vault, created.record.id).exists()

    store.update(
        created.record.id,
        expected_revision=created.revision,
        changes={"title": "changed since the delete was planned"},
        actor="user",
    )
    with pytest.raises(TaskBoardError) as excinfo:
        store.delete(created.record.id, expected_revision=created.revision)
    assert excinfo.value.code == "revision_conflict"
    assert task_path(vault, created.record.id).exists()
    assert store.get(created.record.id).record.title == "changed since the delete was planned"


def test_delete_refuses_an_unsafe_id_or_a_linked_file(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    for bad_id in ("", "../escape", "a" * 31, "G" * 32, "with space"):
        with pytest.raises(TaskBoardError) as excinfo:
            store.delete(bad_id, expected_revision="x")
        assert excinfo.value.code == "unsafe_path"

    real = store.create(title="real")
    outside = tmp_path / "outside.md"
    outside.write_text("not a task\n", encoding="utf-8")
    link_id = "4" * 32
    tasks_dir(vault).mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(outside, task_path(vault, link_id))
    except OSError:
        pytest.skip("this platform could not create a symlink for the test")
    with pytest.raises(TaskBoardError) as excinfo:
        store.delete(link_id, expected_revision="x")
    assert excinfo.value.code == "unsafe_path"
    # The link target outside the task directory is untouched: a refused
    # removal is not a removal, and a link is never followed.
    assert outside.read_text(encoding="utf-8") == "not a task\n"
    assert task_path(vault, real.record.id).exists()


# ── Schema 1 → 2 (#1069) ─────────────────────────────────────────────


def _schema_1(task_id: str, status: str, review: str, *, newline: str = "\n") -> bytes:
    lines = [
        "---",
        "schema: 1",
        f"id: {task_id}",
        "title: legacy task",
        f'status: "{status}"',
        "project_id: null",
        "due: null",
        "assignee: agent",
        f"review_state: {review}",
        'created_at: "2026-10-03T12:00:00+00:00"',
        'updated_at: "2026-10-03T12:00:00+00:00"',
        "chat_id: null",
        "attempt_id: null",
        "# a comment the migration leaves alone",
        "---",
        "The body mentions status: on_hold and review_state: ready, untouched.",
        "",
    ]
    return newline.join(lines).encode()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_schema_1_files_are_rewritten_once_to_the_four_columns(tmp_path: Path, newline: str) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    held, reviewed, plain = "a" * 32, "b" * 32, "c" * 32
    for task_id, status, review in (
        (held, "on_hold", "none"),
        (reviewed, "in_progress", "ready"),
        (plain, "in_progress", "none"),
    ):
        path = task_path(vault, task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_schema_1(task_id, status, review, newline=newline))
    before = {task_id: task_path(vault, task_id).read_bytes() for task_id in (held, reviewed, plain)}

    migrated = store.migrate_schema_1()

    assert sorted(task_id for task_id, _old, _new in migrated) == sorted([held, reviewed, plain])
    statuses = {document.record.id: document.record.status for document in store.list().tasks}
    assert statuses == {held: "backlog", reviewed: "in_review", plain: "in_progress"}
    for task_id in (held, reviewed, plain):
        raw = task_path(vault, task_id).read_bytes()
        assert b"review_state:" not in raw.split(b"---")[1]
        assert b"schema: 2" in raw
        # Everything else is the bytes it was: the comment, the body, the newlines.
        assert b"# a comment the migration leaves alone" in raw
        assert raw.endswith(before[task_id].split(b"---", 2)[2])
        assert (b"\r\n" in raw) == (newline == "\r\n")
    # Once: a second run finds nothing to do.
    assert store.migrate_schema_1() == []


def test_a_schema_1_file_the_migration_cannot_read_is_left_untouched(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    task_id = "d" * 32
    path = task_path(vault, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _schema_1(task_id, "someday", "none")
    path.write_bytes(raw)
    assert store.migrate_schema_1() == []
    assert path.read_bytes() == raw
    assert store.list().invalid[0].code == "unsupported_schema"


# ── Completion history (#1152) ───────────────────────────────────────


@pytest.mark.parametrize("log_first", [False, True])
@pytest.mark.parametrize("markers_in_log", [False, True])
@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("include_close", [False, True])
def test_managed_history_sections_ignore_each_others_literal_delimiters(
    tmp_path: Path,
    log_first: bool,
    markers_in_log: bool,
    inline: bool,
    include_close: bool,
) -> None:
    store = make_store(tmp_path / "vault", tmp_path / "runtime", Clock())
    opener, closer = (
        (COMPLETIONS_OPEN, COMPLETIONS_CLOSE)
        if markers_in_log
        else (LOG_OPEN, LOG_CLOSE)
    )
    literal = f"Mention {opener} literally" if inline else opener
    if include_close:
        literal += f" and {closer} too" if inline else f"\n{closer}"
    summary = "SECRET LOG REPORT\n" + (literal if markers_in_log else "Delegated work")
    resolution = "SECRET RESOLUTION\n" + (
        literal if not markers_in_log else "Resolved work"
    )
    item = render_item(
        attempt_id="b" * 32, state="ready_for_review", outcome="done",
        summary=summary, detail="", created_at="2026-10-08T08:00:00+00:00",
        ended_at="2026-10-08T09:00:00+00:00", chat_id="chat",
        chat_title="Task chat", archive_path="",
    )
    log = upsert_item("", "b" * 32, item).strip()
    empty_history = f"{COMPLETIONS_OPEN}\n## Completion history\n{COMPLETIONS_CLOSE}"
    sections = (log,) if log_first else (empty_history, log)
    body = "Description.\n\n" + "\n\n".join(sections) + "\n"
    created = store.create(title="T", body=body)
    done = store.update(
        created.record.id, expected_revision=created.revision,
        changes={"status": "done"}, actor="user", resolution=resolution,
    )
    assert (
        done.body.index("\n" + LOG_OPEN)
        < done.body.index("\n" + COMPLETIONS_OPEN)
    ) == log_first
    assert log in done.body
    assert parse_completions(done.body)[0].resolution == resolution
    prompt = build_prompt(
        title="T", status="done", due="", project_id="",
        task_id=done.record.id, task_revision=done.revision,
        relative_path="Workspace/Tasks/task.md", body=done.body,
    )
    assert "Description." in prompt
    assert "SECRET LOG REPORT" not in prompt
    assert "SECRET RESOLUTION" not in prompt
    assert "<!-- completion:" not in prompt
    # A log rewrite must find its real section, not delimiters in resolution
    # prose, and leave the entire completion history unchanged.
    rewritten_log = upsert_item(
        done.body, "b" * 32, item + "\n  New log report"
    )
    assert "New log report" in rewritten_log
    assert extract_section(rewritten_log) == extract_section(done.body)
    edited = store.update(
        done.record.id, expected_revision=done.revision, changes={}, actor="user",
        resolution=resolution + "\nReworded",
    )
    assert log in edited.body
    assert parse_completions(edited.body)[0].resolution == resolution + "\nReworded"


def test_duplicate_standalone_completion_sections_still_refuse_a_write(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path / "vault", tmp_path / "runtime", Clock())
    section = f"{COMPLETIONS_OPEN}\n## Completion history\n{COMPLETIONS_CLOSE}\n"
    created = store.create(title="T", body=section + "\n" + section)
    with pytest.raises(TaskBoardError, match="more than one completion history section"):
        store.update(
            created.record.id, expected_revision=created.revision,
            changes={"status": "done"}, actor="user",
        )
    assert store.get(created.record.id).revision == created.revision


def test_literal_delimiters_in_description_survive_managed_writes(tmp_path: Path) -> None:
    store = make_store(tmp_path / "vault", tmp_path / "runtime", Clock())
    description = (
        f"Describe {COMPLETIONS_OPEN} and {COMPLETIONS_CLOSE} inline.\n"
        f"  {COMPLETIONS_OPEN}\n  {COMPLETIONS_CLOSE}\n"
        f"Describe {LOG_OPEN} and {LOG_CLOSE} inline.\n"
        f"  {LOG_OPEN}\n  {LOG_CLOSE}"
    )
    created = store.create(title="T", body=description)
    done = store.update(
        created.record.id, expected_revision=created.revision,
        changes={"status": "done"}, actor="user", resolution="Hidden resolution",
    )
    edited = store.update(
        done.record.id, expected_revision=done.revision, changes={}, actor="user",
        body=description + "\nMore description.",
    )
    prompt = build_prompt(
        title="T", status="done", due="", project_id="",
        task_id=edited.record.id, task_revision=edited.revision,
        relative_path="Workspace/Tasks/task.md", body=edited.body,
    )
    assert description in prompt
    assert "More description." in prompt
    assert "Hidden resolution" not in prompt
    assert "<!-- completion:" not in prompt


def test_literal_completion_and_edited_comments_survive_managed_resolution_edits(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = make_store(tmp_path / "vault", tmp_path / "runtime", clock)
    literal = (
        "<!-- completion:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --> "
        "<!-- edited:2026-10-08T08:00:00+00:00 -->"
    )
    created = store.create(title="T", body="Description.")
    done = store.update(
        created.record.id, expected_revision=created.revision,
        changes={"status": "done"}, actor="user", resolution="Fixed " + literal,
    )
    found = parse_completions(done.body)
    assert len(found) == 1
    original = found[0]
    assert original.id != "a" * 32
    assert original.resolution == "Fixed " + literal
    assert original.edited_at is None
    current = done
    for hour, prefix in ((9, "Edited "), (10, "Edited again ")):
        clock.now = datetime(2026, 10, 9, hour, tzinfo=UTC)
        edited = store.update(
            current.record.id, expected_revision=current.revision,
            changes={}, actor="user", resolution=prefix + literal,
        )
        assert edited.revision != current.revision
        reparsed = parse_completions(store.get(edited.record.id).body)
        assert len(reparsed) == 1
        assert reparsed[0].id == original.id
        assert reparsed[0].completed_at == original.completed_at
        assert reparsed[0].resolution == prefix + literal
        assert reparsed[0].edited_at == clock.now
        current = edited


@pytest.mark.parametrize("attempt_id", ["", "b" * 32])
@pytest.mark.parametrize("multiline", [False, True])
def test_literal_attempt_suffix_survives_managed_completion_and_edits(
    tmp_path: Path, attempt_id: str, multiline: bool,
) -> None:
    clock = Clock()
    store = make_store(tmp_path / "vault", tmp_path / "runtime", clock)
    literal = " · attempt `" + "a" * 32 + "`"
    resolution = "Fixed" + literal
    if multiline:
        resolution += f"\nMention {COMPLETIONS_OPEN} and {COMPLETIONS_CLOSE}\nLast" + literal
    created = store.create(title="T", body="Description.")
    done = store.update(
        created.record.id, expected_revision=created.revision,
        changes={"status": "done"}, actor="user", resolution=resolution,
        attempt_id=attempt_id,
    )
    original = parse_completions(store.get(done.record.id).body)[0]
    assert original.resolution.encode() == resolution.encode()
    assert original.attempt_id == attempt_id
    current = done
    for hour, text in ((9, "Edited " + resolution), (10, "Reworded" + literal)):
        clock.now = datetime(2026, 10, 9, hour, tzinfo=UTC)
        edited = store.update(
            current.record.id, expected_revision=current.revision,
            changes={}, actor="user", resolution=text,
        )
        assert edited.revision != current.revision
        parsed = parse_completions(store.get(edited.record.id).body)[0]
        assert parsed.resolution.encode() == text.encode()
        assert parsed.attempt_id == attempt_id
        assert parsed.id == original.id
        assert parsed.completed_at == original.completed_at
        assert parsed.edited_at == clock.now
        prompt = build_prompt(
            title="T", status="done", due="", project_id="",
            task_id=edited.record.id, task_revision=edited.revision,
            relative_path="Workspace/Tasks/task.md", body=edited.body,
        )
        assert "Description." in prompt
        assert "Reworded" not in prompt
        assert "Edited" not in prompt
        assert "<!-- completion:" not in prompt
        assert COMPLETIONS_OPEN not in prompt
        assert COMPLETIONS_CLOSE not in prompt
        assert literal not in prompt
        current = edited
    # Rewording without any suffix must not carry an invented attempt forward.
    plain = store.update(
        current.record.id, expected_revision=current.revision,
        changes={}, actor="user", resolution="Plain resolution",
    )
    parsed = parse_completions(plain.body)[0]
    assert parsed.resolution == "Plain resolution"
    assert parsed.attempt_id == attempt_id
    assert parsed.id == original.id
    assert parsed.completed_at == original.completed_at


def test_moving_to_done_records_a_completion_and_a_replay_does_not(
    tmp_path: Path,
) -> None:
    clock = Clock()
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", clock)
    created = store.create(title="T", body="Do the thing.")
    clock.now = datetime(2026, 10, 8, 8, 0, 0, tzinfo=UTC)

    done = store.update(
        created.record.id,
        expected_revision=created.revision,
        changes={"status": "done"},
        actor="user",
        resolution="All green\nsecond line.",
    )
    assert done.record.status == "done"
    found = parse_completions(done.body)
    assert len(found) == 1
    assert found[0].completed_at == datetime(2026, 10, 8, 8, 0, 0, tzinfo=UTC)
    assert found[0].resolution == "All green\nsecond line."
    assert len(found[0].id) == 32
    # The file bytes outside the new section match the pre-image's body.
    start = done.body.index(COMPLETIONS_OPEN)
    end = done.body.index(COMPLETIONS_CLOSE) + len(COMPLETIONS_CLOSE)
    assert (done.body[:start] + done.body[end:]).strip() == created.body.strip()

    # Replaying done is a no-op: no second item, not even a new revision.
    replay = store.update(
        created.record.id,
        expected_revision=done.revision,
        changes={"status": "done"},
        actor="user",
    )
    assert replay.revision == done.revision
    assert len(parse_completions(replay.body)) == 1

    # An ordinary edit while done still adds no item; the section is untouched.
    clock.now = datetime(2026, 10, 8, 9, 0, 0, tzinfo=UTC)
    retitled = store.update(
        created.record.id,
        expected_revision=replay.revision,
        changes={"title": "T2"},
        actor="user",
    )
    assert len(parse_completions(retitled.body)) == 1
    assert extract_section(retitled.body) == extract_section(done.body)


def test_reopen_keeps_history_and_the_next_done_appends(tmp_path: Path) -> None:
    clock = Clock()
    store = make_store(tmp_path / "vault", tmp_path / "runtime", clock)
    created = store.create(title="T", body="Do the thing.")
    clock.now = datetime(2026, 10, 8, 8, 0, 0, tzinfo=UTC)
    done = store.update(
        created.record.id,
        expected_revision=created.revision,
        changes={"status": "done"},
        actor="user",
        resolution="First",
    )
    clock.now = datetime(2026, 10, 8, 9, 0, 0, tzinfo=UTC)
    reopened = store.update(
        created.record.id,
        expected_revision=done.revision,
        changes={"status": "in_progress"},
        actor="user",
    )
    assert reopened.record.status == "in_progress"
    assert len(parse_completions(reopened.body)) == 1
    assert COMPLETIONS_OPEN in reopened.body

    clock.now = datetime(2026, 10, 8, 10, 0, 0, tzinfo=UTC)
    redone = store.update(
        created.record.id,
        expected_revision=reopened.revision,
        changes={"status": "done"},
        actor="user",
        resolution="Second",
    )
    found = parse_completions(redone.body)
    assert len(found) == 2
    assert found[0].id != found[1].id
    assert [item.resolution for item in found] == ["Second", "First"]
    assert found[0].completed_at == datetime(2026, 10, 8, 10, 0, 0, tzinfo=UTC)
    assert found[1].completed_at == datetime(2026, 10, 8, 8, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("bom", [False, True])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_description_save_preserves_completion_bytes(
    tmp_path: Path, bom: bool, newline: str
) -> None:
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", Clock())
    task_id = "d" * 32
    body = append_completion(
        "Original description.",
        Completion(
            id="5" * 32,
            completed_at=datetime(2026, 10, 8, 8, 0, 0, tzinfo=UTC),
            resolution="Done well.",
        ),
    )
    if newline == "\r\n":
        body = body.replace("\n", "\r\n")
    hand_file(
        vault,
        task_id,
        valid_frontmatter(task_id),
        body=body,
        newline=newline,
        bom=bom,
    )
    document = store.get(task_id)
    assert len(parse_completions(document.body)) == 1

    updated = store.update(
        task_id,
        expected_revision=document.revision,
        changes={},
        body="Edited description.",
        actor="user",
    )
    assert len(parse_completions(updated.body)) == 1
    assert parse_completions(updated.body)[0].resolution == "Done well."
    assert extract_section(updated.body) == extract_section(document.body)

    after = task_path(vault, task_id).read_bytes()
    assert after.startswith(b"\xef\xbb\xbf") == bom
    assert b"Edited description." in after
    assert b"Original description." not in after
    if newline == "\r\n":
        # The stored section keeps its own CRLF bytes through the save.
        assert b"\r\n" in extract_section(updated.body).encode()


def test_incoming_completion_history_is_not_accepted_without_stored_history(
    tmp_path: Path,
) -> None:
    clock = Clock()
    vault = tmp_path / "vault"
    store = make_store(vault, tmp_path / "runtime", clock)
    created = store.create(title="T", body="Desc.")

    def forged_section(item_id: str, text: str) -> str:
        return (
            f"{COMPLETIONS_OPEN}\n## Completion history\n\n"
            f"- 2026-10-08T08:00:00+00:00 · {text} "
            f"<!-- completion:{item_id} -->\n{COMPLETIONS_CLOSE}"
        )

    forged = f"Desc edited.\n\n{forged_section('f' * 32, 'Forged')}\n"
    updated = store.update(
        created.record.id,
        expected_revision=created.revision,
        changes={},
        body=forged,
        actor="agent",
    )
    assert parse_completions(updated.body) == []
    assert COMPLETIONS_OPEN not in updated.body
    assert "Forged" not in updated.body
    assert "Desc edited." in updated.body

    # Multiple incoming sections are all dropped, and the file stays valid:
    # the next ordinary edit does not trip the duplicate-section refusal.
    forged_two = (
        f"Desc edited again.\n\n{forged_section('e' * 32, 'First forged')}\n\n"
        f"{forged_section('d' * 32, 'Second forged')}\n"
    )
    updated_two = store.update(
        created.record.id,
        expected_revision=updated.revision,
        changes={},
        body=forged_two,
        actor="agent",
    )
    assert parse_completions(updated_two.body) == []
    assert COMPLETIONS_OPEN not in updated_two.body

    # A later managed completion still records exactly one real item.
    clock.now = datetime(2026, 10, 8, 8, 0, 0, tzinfo=UTC)
    done = store.update(
        created.record.id,
        expected_revision=updated_two.revision,
        changes={"status": "done"},
        actor="user",
        resolution="Real",
    )
    assert [item.resolution for item in parse_completions(done.body)] == ["Real"]

    # A legacy done file with no section is scrubbed the same way.
    legacy_id = "b" * 32
    hand_file(
        vault,
        legacy_id,
        valid_frontmatter(legacy_id, status="done"),
        body="Legacy.",
    )
    legacy = store.get(legacy_id)
    scrubbed = store.update(
        legacy_id,
        expected_revision=legacy.revision,
        changes={},
        body=f"Legacy edited.\n\n{forged_section('c' * 32, 'Forged')}\n",
        actor="agent",
    )
    assert scrubbed.record.status == "done"
    assert parse_completions(scrubbed.body) == []
    assert COMPLETIONS_OPEN not in scrubbed.body
