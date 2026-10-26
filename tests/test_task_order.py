"""Tests for ``ciao.task_order`` and the store's order bookkeeping (#2086)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ciao.task_board import TaskBoardError, TaskBoardStore, TaskRecord
from ciao.task_order import (
    ORDER_RELATIVE,
    STATUSES,
    Placement,
    TaskOrder,
    TaskOrderError,
    apply_placement,
    baseline_key,
    parse_order,
    reconcile,
    render_order,
)
from ciao.vault_index import is_reserved_bookkeeping

NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def make_store(vault: Path, runtime: Path, clock: Clock) -> TaskBoardStore:
    return TaskBoardStore(workspace="work", vault_root=vault, runtime_dir=runtime, clock=clock)


def tid(n: int) -> str:
    return f"a{n:031x}"


def rec(
    n: int, title: str = "t", status: str = "backlog", due: str | None = None
) -> TaskRecord:
    return TaskRecord(
        schema=2,
        id=tid(n),
        title=title,
        status=status,
        project_id=None,
        due=due,
        assignee="user",
        created_at=NOW,
        updated_at=NOW,
        chat_id=None,
        attempt_id=None,
    )


def order_of(**sequences: tuple[str, ...]) -> TaskOrder:
    full = {status: sequences.get(status, ()) for status in STATUSES}
    return TaskOrder(version=3, sequences=full)


# ── Pure ─────────────────────────────────────────────────────────────


def ids(records: list[TaskRecord]) -> list[str]:
    return [r.id for r in sorted(records, key=baseline_key)]


def test_baseline_orders_due_then_title_then_id() -> None:
    records = [
        rec(1, "zeta"),
        rec(2, "beta", due="2026-11-01"),
        rec(3, "Alpha", due="2026-10-05"),
        rec(4, "alpha", due="2026-10-05"),
        rec(5, "Émile", due="2026-10-06"),
        rec(6, "emile", due="2026-10-06"),
        rec(7, "Zoe", due="2026-10-07"),
    ]
    # Dated first; case-insensitive tie-break falls to id; accent-stripped tie-break too.
    assert ids(records) == [tid(3), tid(4), tid(5), tid(6), tid(7), tid(2), tid(1)]
    assert ids([rec(9, "b"), rec(8, "A")]) == [tid(8), tid(9)]


def test_reconcile_appends_unlisted_in_baseline_order() -> None:
    records = [rec(1, "c"), rec(2, "a"), rec(3, "b"), rec(4, "z", status="done")]
    stored = order_of(backlog=(tid(3),))
    result = reconcile(stored, records)
    assert result.sequences["backlog"] == (tid(3), tid(2), tid(1))
    assert result.sequences["done"] == (tid(4),)
    assert result.version == 3
    assert reconcile(None, records).version == 0


def test_reconcile_ignores_orphans_wrong_status_and_duplicates() -> None:
    records = [rec(1, "a"), rec(2, "b"), rec(3, "c", status="done")]
    stored = order_of(
        backlog=(tid(99), tid(2), tid(3), tid(2), tid(1)),
        done=(tid(3), tid(1)),
    )
    result = reconcile(stored, records)
    assert result.sequences["backlog"] == (tid(2), tid(1))
    assert result.sequences["done"] == (tid(3),)


def known(*numbers: int) -> set[str]:
    return {tid(n) for n in numbers}


def test_apply_placement_filtered_insertion_keeps_hidden_relative_order() -> None:
    A, h, B, C = tid(1), tid(2), tid(3), tid(4)
    order = order_of(backlog=(A, h, B, C))
    moved = apply_placement(
        order, C, "backlog", Placement(B, None, 3), known_ids=known(1, 2, 3, 4)
    )
    assert moved.sequences["backlog"] == (A, h, C, B)
    assert moved.version == 4
    # After the visible end.
    moved = apply_placement(
        order, A, "backlog", Placement(None, C, 3), known_ids=known(1, 2, 3, 4)
    )
    assert moved.sequences["backlog"] == (h, B, C, A)
    # No anchors: append, no version check.
    moved = apply_placement(order, A, "backlog", None, known_ids=known(1, 2, 3, 4))
    assert moved.sequences["backlog"] == (h, B, C, A)
    moved = apply_placement(
        order, A, "done", Placement(None, None, 3), known_ids=known(1, 2, 3, 4)
    )
    assert moved.sequences["done"] == (A,)
    assert A not in moved.sequences["backlog"]


def test_apply_placement_rejects_bad_anchors() -> None:
    order = order_of(backlog=(tid(1), tid(2)), done=(tid(3),))
    k = known(1, 2, 3, 4)

    def refused(placement: Placement, status: str = "backlog") -> str:
        with pytest.raises(TaskOrderError) as info:
            apply_placement(order, tid(1), status, placement, known_ids=k)
        return info.value.code

    assert refused(Placement(tid(2), tid(2), 3)) == "invalid_order"
    assert refused(Placement(tid(1), None, 3)) == "invalid_order"
    assert refused(Placement(None, tid(1), 3)) == "invalid_order"
    assert refused(Placement(tid(3), None, 3)) == "invalid_order"  # other status
    assert refused(Placement(tid(77), None, 3)) == "invalid_order"  # unknown
    assert refused(Placement(tid(2), None, 2)) == "order_conflict"
    with pytest.raises(TaskOrderError):
        apply_placement(order, tid(55), "backlog", None, known_ids=k)


def test_parse_order_rejects_malformed() -> None:
    good = order_of(backlog=(tid(1),))
    assert parse_order(render_order(good)) == good
    seqs = '{"backlog":[],"in_progress":[],"in_review":[],"done":[]}'
    for bad in (
        b"not json",
        b'{"schema":2,"version":1,"sequences":' + seqs.encode() + b"}",
        b'{"schema":1,"version":0,"sequences":' + seqs.encode() + b"}",
        b'{"schema":1,"version":1,"sequences":{"backlog":[]}}',
        b'{"schema":1,"version":1,"sequences":{"backlog":["nothex"],"in_progress":[],"in_review":[],"done":[]}}',
        (
            '{"schema":1,"version":1,"sequences":{"backlog":["%s"],"in_progress":[],"in_review":[],"done":["%s"]}}'
            % (tid(1), tid(1))
        ).encode(),
    ):
        with pytest.raises(ValueError):
            parse_order(bad)


def test_order_file_is_reserved_bookkeeping() -> None:
    assert is_reserved_bookkeeping(Path("Workspace/task-order.json"))
    assert not is_reserved_bookkeeping(Path("Other/task-order.json"))
    assert ORDER_RELATIVE == Path("Workspace") / "task-order.json"


# ── Store ────────────────────────────────────────────────────────────


@pytest.fixture
def env(tmp_path: Path) -> tuple[TaskBoardStore, Path, Clock]:
    clock = Clock()
    vault = tmp_path / "vault"
    return make_store(vault, tmp_path / "runtime", clock), vault, clock


def order_file(vault: Path) -> Path:
    return vault / "Workspace" / "task-order.json"


def hand(vault: Path, n: int, title: str, status: str = "backlog", due: str | None = None) -> str:
    task_id = tid(n)
    directory = vault / "Workspace" / "Tasks"
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "schema: 2",
        f"id: {task_id}",
        f"title: {title}",
        f"status: {status}",
        "project_id: null",
        f"due: {due or 'null'}",
        "assignee: user",
        'created_at: "2026-10-03T12:00:00+00:00"',
        'updated_at: "2026-10-03T12:00:00+00:00"',
        "chat_id: null",
        "attempt_id: null",
        "---",
        "",
    ]
    (directory / f"{task_id}.md").write_text("\n".join(lines), encoding="utf-8")
    return task_id


def column(store: TaskBoardStore, status: str = "backlog") -> list[str]:
    return list(store.order().sequences[status])


def task_sha(vault: Path, task_id: str) -> str:
    return hashlib.sha256((vault / "Workspace" / "Tasks" / f"{task_id}.md").read_bytes()).hexdigest()


def seed_three(vault: Path) -> list[str]:
    return [hand(vault, 1, "alpha"), hand(vault, 2, "bravo"), hand(vault, 3, "charlie")]


def test_list_does_not_write_order_file_on_read(env) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    seed_three(vault)
    result = store.list()
    assert not order_file(vault).exists()
    assert store.order().version == 0
    assert result.order_error is None


def test_legacy_board_baseline_preserved_after_first_reorder(env) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    a, b, c = seed_three(vault)
    assert column(store) == [a, b, c]
    new = store.reorder(c, placement=Placement(a, None, 0))
    assert order_file(vault).exists()
    assert new.version == 1
    assert list(new.sequences["backlog"]) == [c, a, b]
    assert [d.record.id for d in store.list().tasks] == [c, a, b]


def test_create_appends_and_materializes_baseline(env) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    a, b, c = seed_three(vault)
    created = store.create(title="aaa earliest", due="2020-01-01")
    assert column(store) == [a, b, c, created.record.id]
    assert store.order().version == 1
    assert parse_order(order_file(vault).read_bytes()).sequences["backlog"] == (
        a,
        b,
        c,
        created.record.id,
    )


def test_title_due_body_edits_do_not_reposition(env) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    first = store.create(title="zzz")
    second = store.create(title="aaa")
    before = order_file(vault).read_bytes()
    doc = store.get(first.record.id)
    store.update(
        first.record.id,
        expected_revision=doc.revision,
        changes={"title": "000 renamed", "due": "2020-01-01"},
        body="new body",
        actor="user",
    )
    assert column(store) == [first.record.id, second.record.id]
    assert order_file(vault).read_bytes() == before


def test_status_change_without_placement_appends_to_destination(env) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    a = store.create(title="a").record.id
    b = store.create(title="b").record.id
    c = store.create(title="c").record.id
    store.update(a, expected_revision=store.get(a).revision, changes={"status": "done"}, actor="user")
    store.update(c, expected_revision=store.get(c).revision, changes={"status": "done"}, actor="user")
    assert column(store, "done") == [a, c]
    assert column(store) == [b]
    store.update(a, expected_revision=store.get(a).revision, changes={"status": "backlog"}, actor="user")
    assert column(store) == [b, a]
    assert column(store, "done") == [c]


def test_status_change_with_placement_is_atomic(env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    a = store.create(title="a").record.id
    b = store.create(title="b").record.id
    d = store.create(title="d").record.id
    store.update(d, expected_revision=store.get(d).revision, changes={"status": "done"}, actor="user")
    order_before = order_file(vault).read_bytes()
    task_before = (vault / "Workspace" / "Tasks" / f"{a}.md").read_bytes()
    version = store.order().version

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise TaskBoardError("read_failed", "disk full")

    monkeypatch.setattr(store, "_atomic_write", boom)
    with pytest.raises(TaskBoardError):
        store.update(
            a,
            expected_revision=store.get(a).revision,
            changes={"status": "done"},
            actor="user",
            placement=Placement(d, None, version),
        )
    assert order_file(vault).read_bytes() == order_before
    assert (vault / "Workspace" / "Tasks" / f"{a}.md").read_bytes() == task_before
    assert store.get(a).record.status == "backlog"
    assert column(store) == [a, b]


def test_status_change_with_placement_places_and_commits(env) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    a = store.create(title="a").record.id
    d = store.create(title="d").record.id
    store.update(d, expected_revision=store.get(d).revision, changes={"status": "done"}, actor="user")
    version = store.order().version
    store.update(
        a,
        expected_revision=store.get(a).revision,
        changes={"status": "done"},
        actor="user",
        placement=Placement(d, None, version),
    )
    assert column(store, "done") == [a, d]
    assert store.order().version == version + 1


def test_stale_version_is_order_conflict_and_writes_nothing(env) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    a = store.create(title="a").record.id
    b = store.create(title="b").record.id
    before = order_file(vault).read_bytes()
    stale = store.order().version - 1
    with pytest.raises(TaskBoardError) as info:
        store.reorder(b, placement=Placement(a, None, stale))
    assert info.value.code == "order_conflict"
    with pytest.raises(TaskBoardError) as info:
        store.update(
            a,
            expected_revision=store.get(a).revision,
            changes={"status": "done"},
            actor="user",
            placement=Placement(None, None, stale),
        )
    assert info.value.code == "order_conflict"
    assert order_file(vault).read_bytes() == before
    assert store.get(a).record.status == "backlog"


def test_placement_without_status_change_rejected(env) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    a = store.create(title="a").record.id
    before = order_file(vault).read_bytes()
    task_before = task_sha(vault, a)
    for changes in ({"status": "backlog"}, {"title": "x"}):
        with pytest.raises(TaskBoardError) as info:
            store.update(
                a,
                expected_revision=store.get(a).revision,
                changes=changes,
                actor="user",
                placement=Placement(None, None, store.order().version),
            )
        assert info.value.code == "invalid_order"
    assert order_file(vault).read_bytes() == before
    assert task_sha(vault, a) == task_before


def test_reorder_same_status_keeps_task_bytes_and_revision(env) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    a = store.create(title="a").record.id
    b = store.create(title="b").record.id
    revisions = {i: store.get(i).revision for i in (a, b)}
    shas = {i: task_sha(vault, i) for i in (a, b)}
    store.reorder(b, placement=Placement(a, None, store.order().version))
    assert column(store) == [b, a]
    assert {i: store.get(i).revision for i in (a, b)} == revisions
    assert {i: task_sha(vault, i) for i in (a, b)} == shas


def test_reorder_missing_task_is_not_found(env) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    store.create(title="a")
    with pytest.raises(TaskBoardError) as info:
        store.reorder(tid(42), placement=Placement(None, None, 1))
    assert info.value.code == "not_found"


def test_delete_prunes_membership_no_orphans_in_order(env) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    a = store.create(title="a").record.id
    b = store.create(title="b").record.id
    c = store.create(title="c").record.id
    version = store.order().version
    store.delete(b, expected_revision=store.get(b).revision)
    stored = parse_order(order_file(vault).read_bytes())
    assert stored.sequences["backlog"] == (a, c)
    assert stored.version == version + 1
    # A deleted id is no longer a usable anchor.
    with pytest.raises(TaskBoardError) as info:
        store.reorder(c, placement=Placement(b, None, stored.version))
    assert info.value.code == "invalid_order"


def test_delete_without_order_file_writes_none(env) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    a = hand(vault, 1, "a")
    store.delete(a, expected_revision=store.get(a).revision)
    assert not order_file(vault).exists()


def test_hand_created_task_appears_appended(env) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    a = store.create(title="a").record.id
    b = store.create(title="b").record.id
    stray = hand(vault, 9, "aaa stray", due="2020-01-01")
    assert column(store) == [a, b, stray]
    assert [d.record.id for d in store.list().tasks] == [a, b, stray]


def test_malformed_order_file_lists_baseline_with_error(env) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    a, b, c = seed_three(vault)
    order_file(vault).write_text("{broken", encoding="utf-8")
    result = store.list()
    assert [d.record.id for d in result.tasks] == [a, b, c]
    assert result.order_error is not None
    assert "Workspace/task-order.json" in result.order_error
    assert result.order.version == -1


def test_malformed_order_file_refuses_reorder_and_placement_but_allows_create(env) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    a, b, c = seed_three(vault)
    corrupt = b"{broken"
    order_file(vault).write_bytes(corrupt)
    with pytest.raises(TaskBoardError) as info:
        store.reorder(a, placement=Placement(None, None, -1))
    assert info.value.code == "order_unreadable"
    assert "Workspace/task-order.json" in info.value.message
    with pytest.raises(TaskBoardError) as info:
        store.update(
            a,
            expected_revision=store.get(a).revision,
            changes={"status": "done"},
            actor="user",
            placement=Placement(None, None, -1),
        )
    assert info.value.code == "order_unreadable"
    assert store.get(a).record.status == "backlog"
    created = store.create(title="new")
    store.update(b, expected_revision=store.get(b).revision, changes={"status": "done"}, actor="user")
    store.delete(c, expected_revision=store.get(c).revision)
    assert order_file(vault).read_bytes() == corrupt
    assert [d.record.id for d in store.list().tasks][-1] != c
    assert created.record.id in [d.record.id for d in store.list().tasks]


def test_done_reorder_keeps_completion_timestamp(env) -> None:  # type: ignore[no-untyped-def]
    store, vault, clock = env
    a = store.create(title="a").record.id
    b = store.create(title="b").record.id
    for task_id in (a, b):
        store.update(
            task_id, expected_revision=store.get(task_id).revision, changes={"status": "done"}, actor="user"
        )
    raw_a = (vault / "Workspace" / "Tasks" / f"{a}.md").read_bytes()
    updated = store.get(a).record.updated_at
    clock.now = datetime(2027, 1, 1, tzinfo=UTC)
    store.reorder(a, placement=Placement(None, b, store.order().version))
    assert column(store, "done") == [b, a]
    assert (vault / "Workspace" / "Tasks" / f"{a}.md").read_bytes() == raw_a
    assert store.get(a).record.updated_at == updated


def test_symlinked_order_file_refused(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    store, vault, _ = env
    a, b, c = seed_three(vault)
    target = tmp_path / "elsewhere.json"
    target.write_bytes(render_order(order_of(backlog=(c, b, a))))
    try:
        order_file(vault).symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    result = store.list()
    assert result.order_error is not None
    assert [d.record.id for d in result.tasks] == [a, b, c]
    with pytest.raises(TaskBoardError) as info:
        store.reorder(a, placement=Placement(None, None, -1))
    assert info.value.code == "order_unreadable"
    assert target.read_bytes() == render_order(order_of(backlog=(c, b, a)))
