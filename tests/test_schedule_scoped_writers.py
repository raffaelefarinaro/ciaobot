"""Remaining schedule writers use locked, scoped writes (#3068, slice 3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao.schedules import (
    ScheduleManager,
    ScheduleStore,
    ScheduleStoreCorrupt,
    migrate_loops,
    schedule_store_lock,
)


def _create(store: ScheduleStore, **kw):
    values = dict(daily_time_utc="09:00", prompt="p", model="m", mode="auto", chat_id=1, title="t")
    values.update(kw)
    return store.create(**values)


def _manager(store: ScheduleStore) -> ScheduleManager:
    async def dispatch(*_a, **_k) -> None:
        return None

    return ScheduleManager(store=store, dispatch_to_web=dispatch)


def test_update_fields_changes_only_named_fields_on_the_current_row(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store)
    other = ScheduleStore(tmp_path)
    current = other.get(entry.schedule_id)
    assert current is not None
    import dataclasses

    base = dataclasses.replace(current)
    current.title = "theirs"
    other.replace(current, base=base)
    assert store.update_fields(entry.schedule_id, {"last_status": "ok"}) is True
    final = store.get(entry.schedule_id)
    assert final is not None and final.title == "theirs" and final.last_status == "ok"


def test_update_fields_never_resurrects_and_honours_only_if(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store)
    assert store.update_fields(entry.schedule_id, {"title": "x"}, only_if=lambda e: False) is False
    assert store.get(entry.schedule_id).title == "t"  # type: ignore[union-attr]
    store.delete(entry.schedule_id)
    assert store.update_fields(entry.schedule_id, {"title": "x"}) is False
    assert store.get(entry.schedule_id) is None


def test_add_if_absent_is_atomic_and_refuses_corrupt_store(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store)
    assert store.add_if_absent(entry) is False
    (tmp_path / "schedules.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(ScheduleStoreCorrupt):
        store.add_if_absent(entry)
    assert (tmp_path / "schedules.json").read_text(encoding="utf-8") == "{bad"


def test_migrate_loops_does_not_overwrite_an_existing_schedule(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    existing = _create(store, title="keep")
    (tmp_path / "loops.json").write_text(
        json.dumps({"loops": [
            {"loop_id": existing.schedule_id, "web_chat_id": "c", "prompt": "loop", "title": "loop"},
            {"loop_id": "loop-new", "web_chat_id": "c", "prompt": "n", "autostart": True},
        ]}),
        encoding="utf-8",
    )
    assert migrate_loops(tmp_path) == 1
    assert store.get(existing.schedule_id).title == "keep"  # type: ignore[union-attr]
    assert store.get("loop-new") is not None
    assert (tmp_path / "loops.json.migrated").exists()


def test_migrate_loops_leaves_the_file_when_the_store_is_corrupt(tmp_path: Path) -> None:
    (tmp_path / "schedules.json").write_text("{bad", encoding="utf-8")
    (tmp_path / "loops.json").write_text(
        json.dumps({"loops": [{"loop_id": "loop-a", "web_chat_id": "c", "prompt": "n"}]}),
        encoding="utf-8",
    )
    assert migrate_loops(tmp_path) == 0
    assert (tmp_path / "loops.json").exists()
    assert (tmp_path / "schedules.json").read_text(encoding="utf-8") == "{bad"


def test_backfill_keeps_a_concurrent_edit_to_another_field(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store, web_project_id="proj-1")
    manager = _manager(store)
    original_list = store.list_entries

    def list_then_edit(*args, **kwargs):
        rows = original_list(*args, **kwargs)
        # Another writer lands between the backfill's read and its write.
        ScheduleStore(tmp_path).update_fields(entry.schedule_id, {"title": "edited"})
        return rows

    store.list_entries = list_then_edit  # type: ignore[method-assign]
    assert manager.backfill_project_names(lambda pid: "Proj") == 1
    store.list_entries = original_list  # type: ignore[method-assign]
    final = store.get(entry.schedule_id)
    assert final is not None and final.title == "edited" and final.web_project_name == "Proj"


def test_backfill_skips_a_row_deleted_meanwhile(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store, web_project_id="proj-1")
    manager = _manager(store)
    original_list = store.list_entries

    def list_then_delete(*args, **kwargs):
        rows = original_list(*args, **kwargs)
        ScheduleStore(tmp_path).delete(entry.schedule_id)
        return rows

    store.list_entries = list_then_delete  # type: ignore[method-assign]
    assert manager.backfill_project_names(lambda pid: "Proj") == 0
    store.list_entries = original_list  # type: ignore[method-assign]
    assert store.get(entry.schedule_id) is None


def test_schedule_store_lock_is_the_stores_own_lock(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    with schedule_store_lock(tmp_path):
        with store._txn():  # reentrant: same shared lock
            _create(store)


def test_ensure_store_creates_once_and_keeps_existing_content(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / ".runtime")
    store.ensure_store()
    assert json.loads((tmp_path / ".runtime" / "schedules.json").read_text()) == {"schedules": []}
    _create(store)
    store.ensure_store()
    assert len(json.loads((tmp_path / ".runtime" / "schedules.json").read_text())["schedules"]) == 1


def test_revert_rename_keeps_edits_made_after_the_rename(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path, include_system=True, workspace_names=lambda: ["old", "new"])
    entry = _create(store, workspace="old")
    original = (tmp_path / "schedules.json").read_bytes()
    state_before = (tmp_path / "system_schedules_state.json").read_bytes() if (tmp_path / "system_schedules_state.json").exists() else None
    assert store.rename_workspace("old", "new") == 1
    other = ScheduleStore(tmp_path)
    other.update_fields(entry.schedule_id, {"title": "edited after rename"})
    store.revert_rename("old", "new", original_schedules=original, original_state=state_before)
    row = store.get(entry.schedule_id)
    assert row is not None
    assert row.workspace == "old"
    assert row.title == "edited after rename"


def test_revert_rename_leaves_rows_that_were_already_at_the_new_name(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    moved = _create(store, workspace="old")
    resident = _create(store, workspace="new")
    original = (tmp_path / "schedules.json").read_bytes()
    store.rename_workspace("old", "new")
    store.revert_rename("old", "new", original_schedules=original, original_state=None)
    assert store.get(moved.schedule_id).workspace == "old"  # type: ignore[union-attr]
    assert store.get(resident.schedule_id).workspace == "new"  # type: ignore[union-attr]
