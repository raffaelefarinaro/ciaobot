"""Per-workspace fan-out of packaged system routines.

The daily curation routine shipped with `workspace: "default"`, which was never
a real workspace: the resolver fell through it to the primary one, so exactly
one vault was ever curated and every work contact was filed under
`personal/People/`.

A routine is fanned out only when its inputs *and* write targets are partitioned
per workspace (curation, hygiene). Routines whose subject is shared — the global
runtime directory, one skill catalog — stay single, because N runs would redo the
same work and report one problem N times.

The set is derived on every read, not persisted: `list_entries` drops runtime
rows with `scope == "system"`, so it cannot be extended by writing to
`schedules.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

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
    assert system_base_id("system-workspace-hygiene") == "system-workspace-hygiene"
    assert system_base_id("") == ""


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


def test_global_routines_stay_single(tmp_path: Path) -> None:
    """Only routines whose subject is shared stay single.

    `system-install-health` reports the audit sections whose subject is the
    global runtime directory — job failures and pending upgrade actions — which
    are identical in every workspace; `system-skill-evolution` reasons about one
    skill catalog, which stays one until the re-rooting has run and the user has
    triaged it. N runs of either would redo identical work, and for
    install-health it would also report one problem as N.
    """
    ids = _ids(_store(tmp_path, "personal", "work"))

    assert ids.count("system-install-health") == 1
    assert ids.count("system-skill-evolution") == 1
    assert not any(item.startswith("system-install-health@") for item in ids)
    assert not any(item.startswith("system-skill-evolution@") for item in ids)


def test_hygiene_is_fanned_out_per_workspace(tmp_path: Path) -> None:
    """Its audit reads one workspace's MEMORY.md and proposal queue and reports
    into that workspace's chat, so an unscoped run leaked another workspace's
    findings into the wrong chat."""
    ids = _ids(_store(tmp_path, "personal", "work"))

    assert "system-workspace-hygiene@personal" in ids
    assert "system-workspace-hygiene@work" in ids
    assert "system-workspace-hygiene" not in ids


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
            "system-workspace-hygiene": {"enabled": True, "workspace": "default"},
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
