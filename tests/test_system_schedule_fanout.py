"""Per-workspace fan-out of packaged system routines.

The daily curation routine shipped with `workspace: "default"`, which was never
a real workspace: the resolver fell through it to the primary one, so exactly
one vault was ever curated and every work contact was filed under
`personal/People/`.

Both shipped routines are partitioned per workspace: Workspace care owns that
workspace's vault, while Skill reflection reads that workspace's trajectories
and canonical user-owned skills.

The set is derived on every read, not persisted: `list_entries` drops runtime
rows with `scope == "system"`, so it cannot be extended by writing to
`schedules.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao.job_runs import automation_summary
from ciao.schedules import (
    ScheduleStore,
    system_base_id,
    system_schedule_id,
    _stagger_time,
)


def _store(tmp_path: Path, *workspaces: str) -> ScheduleStore:
    return ScheduleStore(
        tmp_path,
        include_system=True,
        workspace_names=lambda: list(workspaces),
    )


def _ids(store: ScheduleStore) -> list[str]:
    return [entry.schedule_id for entry in store.list_entries()]


# ---- id helpers ------------------------------------------------------------


def test_base_id_survives_fan_out() -> None:
    assert system_base_id("system-memory-curation@work") == "system-memory-curation"
    assert system_base_id("system-skill-evolution@work") == "system-skill-evolution"
    assert system_base_id("") == ""


def test_is_system_schedule_id() -> None:
    from ciao.schedules import is_system_schedule_id

    assert is_system_schedule_id("system-memory-curation") is True
    assert is_system_schedule_id("system-memory-curation@work") is True
    assert is_system_schedule_id("sched-ee193709") is False
    assert is_system_schedule_id("") is False


def test_stagger_shifts_later_rows_and_wraps() -> None:
    assert _stagger_time("00:01", 0) == "00:01"
    assert _stagger_time("00:01", 1) == "00:08"
    assert _stagger_time("23:59", 1) == "00:06"
    # Malformed input is left alone; compute_next_run already treats it as
    # "never fires" rather than guessing.
    assert _stagger_time("nonsense", 1) == "nonsense"


# ---- fan-out ---------------------------------------------------------------


def test_curation_becomes_one_entry_per_workspace(tmp_path: Path) -> None:
    ids = _ids(_store(tmp_path, "personal", "work"))

    assert "system-memory-curation@personal" in ids
    assert "system-memory-curation@work" in ids
    assert "system-memory-curation" not in ids


def test_skill_reflection_is_fanned_out_per_workspace(tmp_path: Path) -> None:
    ids = _ids(_store(tmp_path, "personal", "work"))

    assert "system-skill-evolution@personal" in ids
    assert "system-skill-evolution@work" in ids
    assert "system-skill-evolution" not in ids


def test_each_fanned_out_row_carries_its_own_workspace(tmp_path: Path) -> None:
    rows = {
        entry.schedule_id: entry
        for entry in _store(tmp_path, "personal", "work").list_entries()
    }

    assert rows["system-memory-curation@personal"].workspace == "personal"
    assert rows["system-memory-curation@work"].workspace == "work"
    # Titled per workspace so two identical rows are distinguishable in the UI.
    assert "personal" in rows["system-memory-curation@personal"].title


def test_fanned_out_rows_do_not_all_fire_in_the_same_minute(tmp_path: Path) -> None:
    times = [
        entry.daily_time_utc
        for entry in _store(tmp_path, "personal", "work", "acme").list_entries()
        if entry.schedule_id.startswith("system-memory-curation")
    ]

    assert len(times) == len(set(times)) == 3


def test_a_new_workspace_gets_its_row_without_a_migration(tmp_path: Path) -> None:
    """The fan-out is derived per read, so adding a workspace is enough."""
    assert "system-memory-curation@acme" not in _ids(_store(tmp_path, "personal"))
    assert "system-memory-curation@acme" in _ids(_store(tmp_path, "personal", "acme"))


def test_without_a_resolver_the_definition_stays_single(tmp_path: Path) -> None:
    """Callers that only read user schedules must keep working unchanged."""
    ids = _ids(ScheduleStore(tmp_path, include_system=True))

    assert ids.count("system-memory-curation") == 1
    assert not any("@" in item for item in ids)


# ---- overlay ---------------------------------------------------------------


def _write_state(tmp_path: Path, payload: dict) -> None:
    (tmp_path / "system_schedules_state.json").write_text(
        json.dumps({"schedules": payload}), encoding="utf-8"
    )


def test_per_workspace_enable_is_independent(tmp_path: Path) -> None:
    _write_state(
        tmp_path,
        {system_schedule_id("system-memory-curation", "work"): {"enabled": False}},
    )
    rows = {
        entry.schedule_id: entry
        for entry in _store(tmp_path, "personal", "work").list_entries()
    }

    assert rows["system-memory-curation@work"].enabled is False
    assert rows["system-memory-curation@personal"].enabled is True


def test_a_disabled_routine_stays_disabled_after_the_fan_out(tmp_path: Path) -> None:
    """The upgrade path.

    The overlay used to be keyed by the bare definition id; the fan-out changed
    the key to `<base>@<workspace>` with no migration, so on the first read after
    an upgrade the new key held nothing and the packaged `enabled: true` won — a
    routine the user had deliberately switched off started running again, in
    every workspace.
    """
    _write_state(tmp_path, {"system-memory-curation": {"enabled": False}})

    rows = {
        entry.schedule_id: entry
        for entry in _store(tmp_path, "personal", "work").list_entries()
    }

    assert rows["system-memory-curation@personal"].enabled is False
    assert rows["system-memory-curation@work"].enabled is False


def test_a_migrated_row_outranks_the_pre_fan_out_key(tmp_path: Path) -> None:
    """The legacy key is only a fallback: a row that already has its own state
    keeps it, so re-enabling one workspace's row is not undone by the old value.
    """
    _write_state(
        tmp_path,
        {
            "system-memory-curation": {"enabled": False},
            "system-memory-curation@work": {"enabled": True},
        },
    )

    rows = {
        entry.schedule_id: entry
        for entry in _store(tmp_path, "personal", "work").list_entries()
    }

    assert rows["system-memory-curation@work"].enabled is True
    assert rows["system-memory-curation@personal"].enabled is False


def test_the_legacy_overlay_carries_the_last_run_forward(tmp_path: Path) -> None:
    """The rest of the overlay migrates too, so the upgrade does not also replay
    a missed occurrence in every workspace at once."""
    _write_state(
        tmp_path,
        {"system-memory-curation": {"enabled": True, "last_triggered_on": "2026-08-19"}},
    )

    rows = {
        entry.schedule_id: entry
        for entry in _store(tmp_path, "personal", "work").list_entries()
    }

    assert rows["system-memory-curation@work"].last_triggered_on == "2026-08-19"


def test_a_stale_workspace_in_the_overlay_no_longer_shadows_the_definition(
    tmp_path: Path,
) -> None:
    """The live-install failure mode.

    `workspace` is in SYSTEM_STATE_FIELDS and `_replace_system_state` writes
    every field on any save, so the packaged sentinel was copied into the
    overlay and then outranked the definition forever — meaning a fix to the
    packaged file alone would change nothing on an existing install.
    """
    _write_state(
        tmp_path,
        {
            "system-memory-curation": {"enabled": True, "workspace": "default"},
            "system-skill-evolution": {"enabled": True, "workspace": "default"},
        },
    )

    entries = _store(tmp_path, "personal", "work").list_entries()

    assert all(entry.workspace != "default" for entry in entries)
    assert "system-memory-curation@work" in {e.schedule_id for e in entries}


def test_an_overlay_cannot_move_a_fanned_out_row_to_another_workspace(
    tmp_path: Path,
) -> None:
    """The workspace is part of the row's identity, not mutable state."""
    _write_state(
        tmp_path,
        {"system-memory-curation@work": {"enabled": True, "workspace": "personal"}},
    )
    rows = {
        entry.schedule_id: entry
        for entry in _store(tmp_path, "personal", "work").list_entries()
    }

    assert rows["system-memory-curation@work"].workspace == "work"


def test_replace_round_trips_a_fanned_out_row(tmp_path: Path) -> None:
    store = _store(tmp_path, "personal", "work")
    entry = store.get("system-memory-curation@work")
    assert entry is not None

    entry.last_triggered_on = "2026-08-19"
    store.replace(entry)

    reloaded = _store(tmp_path, "personal", "work").get("system-memory-curation@work")
    assert reloaded is not None
    assert reloaded.last_triggered_on == "2026-08-19"
    # The sibling is untouched.
    sibling = _store(tmp_path, "personal", "work").get(
        "system-memory-curation@personal"
    )
    assert sibling is not None
    assert sibling.last_triggered_on == ""


def test_replace_refuses_to_move_a_fanned_out_row(tmp_path: Path) -> None:
    """The update APIs reported a move the identity cannot represent.

    `workspace` is an allowed system-schedule change and `replace` persisted it,
    but `_system_entries` drops it again for a fanned-out row because the
    workspace comes from the id suffix. So the caller got a payload naming
    `personal` and the next read said `work`: a move confirmed to a caller that
    never happened. The refusal has to name the reason, since "it moved, then it
    didn't" is not something the caller can diagnose.
    """
    store = _store(tmp_path, "personal", "work")
    entry = store.get("system-memory-curation@work")
    assert entry is not None
    entry.workspace = "personal"

    with pytest.raises(ValueError) as excinfo:
        store.replace(entry)

    message = str(excinfo.value)
    assert "system-memory-curation@work" in message
    assert "once per workspace" in message
    # Nothing was written, so the row is not left half-moved either.
    assert not (tmp_path / "system_schedules_state.json").exists()
    reloaded = _store(tmp_path, "personal", "work").get("system-memory-curation@work")
    assert reloaded is not None
    assert reloaded.workspace == "work"


def test_replace_refuses_a_blank_workspace_on_a_fanned_out_row(
    tmp_path: Path,
) -> None:
    """The PATCH route blanks an unrecognised workspace name rather than
    rejecting it, so "" arrives here as a move too — and it would be ignored the
    same way."""
    store = _store(tmp_path, "personal", "work")
    entry = store.get("system-memory-curation@work")
    assert entry is not None
    entry.workspace = ""

    with pytest.raises(ValueError):
        store.replace(entry)


def test_a_case_only_workspace_difference_is_not_a_move(tmp_path: Path) -> None:
    """The control plane lower-cases its target workspace before saving, so a
    registry name with capitals would otherwise make every enable toggle on that
    row fail."""
    store = _store(tmp_path, "personal", "Work")
    entry = store.get("system-memory-curation@Work")
    assert entry is not None
    entry.workspace = "work"
    entry.enabled = False

    store.replace(entry)

    reloaded = _store(tmp_path, "personal", "Work").get("system-memory-curation@Work")
    assert reloaded is not None
    assert reloaded.enabled is False
    assert reloaded.workspace == "Work"


# ---- consumers of the literal ids -----------------------------------------


def test_schedule_only_job_is_found_through_a_fanned_out_id() -> None:
    """A `schedule_only` job is hidden when its schedule is not installed, and
    the check compared literal ids. Only the fanned-out form is present here, so
    an exact-match check hides the row even though the routine is installed.

    No shipped `schedule_only` job maps to a per-workspace routine *yet* — this
    pins the resolution so marking one `per_workspace` later cannot silently
    make its row disappear.
    """
    rows = automation_summary(installed_schedules={"system-skill-evolution@work"})

    assert "skill_evolution" in {row["job"] for row in _flatten(rows)}


def test_schedule_only_job_is_still_hidden_when_nothing_installs_it() -> None:
    """The other half: base-id resolution must not make the check vacuous."""
    rows = automation_summary(installed_schedules=set())

    assert "skill_evolution" not in {row["job"] for row in _flatten(rows)}


def _flatten(rows: object) -> list[dict]:
    """Walk the nested (group/step/child) automation rows."""
    out: list[dict] = []
    items = rows if isinstance(rows, list) else rows.get("jobs", [])  # type: ignore[union-attr]
    stack = list(items)
    while stack:
        row = stack.pop()
        if not isinstance(row, dict):
            continue
        out.append(row)
        for key in ("steps", "children"):
            stack.extend(row.get(key) or [])
    return out
