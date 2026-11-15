"""Editable-content revision precondition for ``ScheduleStore`` (#3068, slice 2)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from ciao.schedules import ScheduleConflict, ScheduleStore


def _create(store: ScheduleStore, **kw):
    values = dict(daily_time_utc="09:00", prompt="p", model="m", mode="auto", chat_id=1, title="t")
    values.update(kw)
    return store.create(**values)


def _edit(store: ScheduleStore, schedule_id: str, **changes):
    current = store.get(schedule_id)
    assert current is not None
    base = dataclasses.replace(current)
    for key, value in changes.items():
        setattr(current, key, value)
    store.replace(current, base=base)


def test_revision_is_stable_and_opaque(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store)
    first = store.revision(entry.schedule_id)
    assert first and first == store.revision(entry.schedule_id)
    assert store.revision("missing") is None


def test_edit_changes_revision_and_revert_does_not_restore_it(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store)
    original = store.revision(entry.schedule_id)
    _edit(store, entry.schedule_id, title="b")
    changed = store.revision(entry.schedule_id)
    _edit(store, entry.schedule_id, title="t")
    reverted = store.revision(entry.schedule_id)
    assert len({original, changed, reverted}) == 3


def test_run_state_stamps_do_not_change_revision(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store)
    before = store.revision(entry.schedule_id)
    _edit(store, entry.schedule_id, last_status="ok", last_triggered_on="2026-01-01")
    assert store.revision(entry.schedule_id) == before


def test_stale_revision_conflicts_without_mutation(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store)
    stale = store.revision(entry.schedule_id)
    _edit(store, entry.schedule_id, title="other")
    raw_before = (tmp_path / "schedules.json").read_text(encoding="utf-8")
    current = store.get(entry.schedule_id)
    assert current is not None
    base = dataclasses.replace(current)
    current.prompt = "mine"
    with pytest.raises(ScheduleConflict):
        store.replace(current, base=base, expected_revision=stale)
    with pytest.raises(ScheduleConflict):
        store.delete(entry.schedule_id, expected_revision=stale)
    assert (tmp_path / "schedules.json").read_text(encoding="utf-8") == raw_before


def test_matching_revision_applies_and_delete_with_it_works(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store)
    token = store.revision(entry.schedule_id)
    current = store.get(entry.schedule_id)
    assert current is not None
    base = dataclasses.replace(current)
    current.title = "n"
    store.replace(current, base=base, expected_revision=token)
    assert store.delete(entry.schedule_id, expected_revision=store.revision(entry.schedule_id))


def test_restore_gets_a_fresh_revision(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store)
    token = store.revision(entry.schedule_id)
    taken = store.take_user_items(lambda item: True)
    assert store.put_back_user_items(taken) == 1
    assert store.revision(entry.schedule_id) != token


def test_rename_workspace_bumps_revision(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store, workspace="old")
    token = store.revision(entry.schedule_id)
    store.rename_workspace("old", "new")
    assert store.revision(entry.schedule_id) != token


def test_legacy_row_without_rev_key_has_a_revision_and_edits_bump_it(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store)
    data = json.loads((tmp_path / "schedules.json").read_text(encoding="utf-8"))
    data["schedules"][0].pop("_rev", None)
    (tmp_path / "schedules.json").write_text(json.dumps(data), encoding="utf-8")
    before = store.revision(entry.schedule_id)
    _edit(store, entry.schedule_id, title="z")
    assert store.revision(entry.schedule_id) != before


def test_system_revision_follows_effective_state_and_survives_inheritance(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path, include_system=True)
    system = next(e for e in store.list_entries() if e.scope == "system")
    token = store.revision(system.schedule_id)
    assert token
    _edit(store, system.schedule_id, enabled=not system.enabled)
    assert store.revision(system.schedule_id) != token
    stale = token
    current = store.get(system.schedule_id)
    assert current is not None
    base = dataclasses.replace(current)
    current.enabled = system.enabled
    with pytest.raises(ScheduleConflict):
        store.replace(current, base=base, expected_revision=stale)


def test_empty_revision_is_a_mismatch_not_no_precondition(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store)
    with pytest.raises(ScheduleConflict):
        store.delete(entry.schedule_id, expected_revision="")
    assert store.get(entry.schedule_id) is not None


def test_snapshot_pairs_each_row_with_its_own_revision(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    a, b = _create(store, title="a"), _create(store, title="b")
    pairs = {e.schedule_id: r for e, r in store.snapshot()}
    assert pairs == {a.schedule_id: store.revision(a.schedule_id), b.schedule_id: store.revision(b.schedule_id)}
    got = store.get_with_revision(a.schedule_id)
    assert got is not None and got[1] == pairs[a.schedule_id]
    assert store.get_with_revision("missing") is None


def test_unmaterialized_system_row_token_survives_first_seen_stamp(tmp_path: Path) -> None:
    from datetime import datetime

    store = ScheduleStore(tmp_path, include_system=True)
    system = next(e for e in store.list_entries() if e.scope == "system")
    token = store.revision(system.schedule_id)
    store.record_system_first_seen(datetime.now())
    assert store.revision(system.schedule_id) == token
    current = store.get(system.schedule_id)
    assert current is not None
    base = dataclasses.replace(current)
    current.enabled = not current.enabled
    store.replace(current, base=base, expected_revision=token)


def test_whole_row_replace_does_not_bump_for_a_normalization_only_difference(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _create(store)
    token = store.revision(entry.schedule_id)
    current = store.get(entry.schedule_id)
    assert current is not None
    current.last_status = "ok"
    store.replace(current)
    assert store.revision(entry.schedule_id) == token
