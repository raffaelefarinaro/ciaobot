"""Cooperating-writer atomicity for ``ScheduleStore`` (#3068, slice 1)."""

from __future__ import annotations

import dataclasses
import json
import multiprocessing
import threading
from pathlib import Path

import pytest

from ciao.schedules import (
    ScheduleNotFound,
    ScheduleStore,
    ScheduleStoreCorrupt,
)


def _create(store: ScheduleStore, **overrides):
    values = dict(
        daily_time_utc="09:00",
        prompt="p",
        model="m",
        mode="auto",
        chat_id=1,
        title="t",
    )
    values.update(overrides)
    return store.create(**values)


def _raw(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "schedules.json").read_text(encoding="utf-8"))


def test_disjoint_field_edits_from_two_stores_both_survive(tmp_path: Path) -> None:
    first, second = ScheduleStore(tmp_path), ScheduleStore(tmp_path)
    entry = _create(first)
    stale_a = first.get(entry.schedule_id)
    stale_b = second.get(entry.schedule_id)
    assert stale_a and stale_b
    base_a, base_b = dataclasses.replace(stale_a), dataclasses.replace(stale_b)
    stale_a.title = "renamed"
    stale_b.enabled = False
    first.replace(stale_a, base=base_a)
    second.replace(stale_b, base=base_b)
    final = first.get(entry.schedule_id)
    assert final is not None
    assert final.title == "renamed"
    assert final.enabled is False


def test_stale_replace_does_not_resurrect_deleted_row(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store)
    stale = store.get(entry.schedule_id)
    assert stale is not None
    base = dataclasses.replace(stale)
    assert ScheduleStore(tmp_path).delete(entry.schedule_id) is True
    stale.title = "late"
    with pytest.raises(ScheduleNotFound):
        store.replace(stale, base=base)
    assert store.get(entry.schedule_id) is None


def test_unknown_fields_survive_a_scoped_replace(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store)
    data = _raw(tmp_path)
    data["schedules"][0]["future_field"] = {"keep": True}
    (tmp_path / "schedules.json").write_text(json.dumps(data), encoding="utf-8")
    current = store.get(entry.schedule_id)
    assert current is not None
    base = dataclasses.replace(current)
    current.title = "x"
    store.replace(current, base=base)
    assert _raw(tmp_path)["schedules"][0]["future_field"] == {"keep": True}


def test_authorize_runs_against_the_current_row(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store, workspace="alpha")
    stale = store.get(entry.schedule_id)
    assert stale is not None
    base = dataclasses.replace(stale)
    other = ScheduleStore(tmp_path)
    moved = other.get(entry.schedule_id)
    assert moved is not None
    moved_base = dataclasses.replace(moved)
    moved.workspace = "beta"
    other.replace(moved, base=moved_base)

    def authorize(current) -> None:
        if current.workspace != "alpha":
            raise PermissionError("moved")

    stale.title = "late"
    with pytest.raises(PermissionError):
        store.replace(stale, base=base, authorize=authorize)
    assert store.get(entry.schedule_id).title == "t"  # type: ignore[union-attr]
    with pytest.raises(PermissionError):
        store.delete(entry.schedule_id, authorize=authorize)
    assert store.get(entry.schedule_id) is not None


@pytest.mark.parametrize("name", ["schedules.json", "system_schedules_state.json"])
def test_corrupt_store_is_never_overwritten_as_empty(tmp_path: Path, name: str) -> None:
    store = ScheduleStore(tmp_path, include_system=True)
    (tmp_path / name).write_text("{not json", encoding="utf-8")
    with pytest.raises(ScheduleStoreCorrupt):
        if name == "schedules.json":
            _create(store)
        else:
            store.record_system_first_seen(__import__("datetime").datetime.now())
    assert (tmp_path / name).read_text(encoding="utf-8") == "{not json"


def test_reads_stay_tolerant_of_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / "schedules.json").write_text("{not json", encoding="utf-8")
    assert ScheduleStore(tmp_path).list_entries() == []


def test_two_instances_share_in_process_reentrancy(tmp_path: Path) -> None:
    first, second = ScheduleStore(tmp_path), ScheduleStore(tmp_path)
    with first._txn():
        # Same thread, second instance: must not deadlock on the file lock.
        with second._txn():
            _create(second)


def test_threads_over_separate_stores_do_not_lose_creates(tmp_path: Path) -> None:
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            store = ScheduleStore(tmp_path)
            for _ in range(10):
                _create(store)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(_raw(tmp_path)["schedules"]) == 40


def _proc_create(root: str, count: int) -> None:
    store = ScheduleStore(Path(root))
    for _ in range(count):
        _create(store)


def test_separate_processes_do_not_lose_creates(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_proc_create, args=(str(tmp_path), 8)) for _ in range(3)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    assert len(_raw(tmp_path)["schedules"]) == 24


def test_lock_sidecar_is_not_the_json_inode(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    _create(store)
    assert (tmp_path / "schedules.lock").exists()
    before = (tmp_path / "schedules.lock").stat().st_ino
    _create(store)
    assert (tmp_path / "schedules.lock").stat().st_ino == before


def test_system_overlay_scoped_write_keeps_concurrent_field(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path, include_system=True)
    system = next(e for e in store.list_entries() if e.scope == "system")
    stale_a, stale_b = dataclasses.replace(system), dataclasses.replace(system)
    base_a, base_b = dataclasses.replace(system), dataclasses.replace(system)
    stale_a.enabled = not system.enabled
    store.replace(stale_a, base=base_a)
    stale_b.archive_policy = "auto" if system.archive_policy != "auto" else "manual"
    ScheduleStore(tmp_path, include_system=True).replace(stale_b, base=base_b)
    final = store.get(system.schedule_id)
    assert final is not None
    assert final.enabled is stale_a.enabled
    assert final.archive_policy == stale_b.archive_policy
