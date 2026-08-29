"""A wall-clock schedule must never persist without a usable time.

An interval entry (and a loop migrated into one) carries no ``daily_time_utc``.
Moving it to daily/weekly/monthly therefore arrives with the time empty, and
``compute_next_run`` cannot parse that — it returns ``None``, ``tick()`` never
matches, and the automation sits there reading as enabled while silently never
firing.

There are two write paths into that state, REST and MCP, so the guard lives in
``ciao.schedules`` and both call it. These tests pin the shared rule and the MCP
path; ``tests/test_interval_schedule_api.py`` covers the REST one.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.control_plane import CiaoControlPlane, ControlPlaneError, McpPrincipal
from ciao.schedules import (
    INTERVAL_FREQUENCY,
    ScheduleEntry,
    compute_next_run,
    wall_clock_time_error,
)


def _entry(**overrides) -> ScheduleEntry:
    fields = {
        "schedule_id": "s1",
        "prompt": "p",
        "chat_id": 0,
        "created_at": "2026-01-01T00:00:00Z",
    }
    fields.update(overrides)
    return ScheduleEntry(**fields)


# --- the shared rule --------------------------------------------------------


@pytest.mark.parametrize("frequency", ["daily", "weekly", "monthly", "once"])
@pytest.mark.parametrize("bad", ["", "9:30", "25:00", "09:60", "nope", "09:30:00"])
def test_the_guard_rejects_every_non_canonical_time(frequency: str, bad: str) -> None:
    assert wall_clock_time_error(_entry(frequency=frequency, daily_time_utc=bad))


@pytest.mark.parametrize("unparseable", ["", "nope", "09:30:00"])
def test_an_unparseable_time_leaves_the_entry_unfireable(unparseable: str) -> None:
    """The failure the guard exists to prevent: enabled, but never dispatched."""
    entry = _entry(frequency="daily", daily_time_utc=unparseable)

    assert wall_clock_time_error(entry)
    assert compute_next_run(entry) is None


@pytest.mark.parametrize("out_of_range", ["25:00", "09:60"])
def test_an_out_of_range_time_raises_rather_than_returning_none(
    out_of_range: str,
) -> None:
    """Worse than unfireable, and the reason the guard checks range too.

    ``compute_next_run`` guards the *parse* but not the *range*: "25:00" splits
    and int()s cleanly, then ``datetime.replace(hour=25)`` raises inside
    ``tick()``. Pinned so the guard cannot be relaxed to a bare parse check.
    """
    entry = _entry(frequency="daily", daily_time_utc=out_of_range)

    assert wall_clock_time_error(entry)
    with pytest.raises(ValueError):
        compute_next_run(entry)


@pytest.mark.parametrize("good", ["00:00", "09:30", "23:59"])
def test_a_parseable_time_passes(good: str) -> None:
    entry = _entry(frequency="daily", daily_time_utc=good)

    assert wall_clock_time_error(entry) == ""
    assert compute_next_run(entry) is not None


@pytest.mark.parametrize("frequency", ["manual", INTERVAL_FREQUENCY])
def test_cadences_that_carry_no_time_are_exempt(frequency: str) -> None:
    entry = _entry(frequency=frequency, daily_time_utc="")

    assert wall_clock_time_error(entry) == ""


# --- the MCP write path -----------------------------------------------------


class _Store:
    def __init__(self, entry: ScheduleEntry) -> None:
        self.entries = [entry]
        self.replaced: list[ScheduleEntry] = []

    def list_entries(self) -> list[ScheduleEntry]:
        return list(self.entries)

    def replace(self, entry: ScheduleEntry) -> None:
        self.replaced.append(entry)
        self.entries = [entry]


@pytest.fixture
def plane(tmp_path: Path):
    entry = _entry(
        schedule_id="loop-abc",
        prompt="check PRs",
        frequency=INTERVAL_FREQUENCY,
        interval_minutes=5,
        daily_time_utc="",
        workspace="work",
        web_chat_id="chat-1",
    )
    store = _Store(entry)
    return SimpleNamespace(
        plane=CiaoControlPlane(
            SimpleNamespace(
                workspace=lambda name: object() if name == "work" else None,
                workspace_root=str(tmp_path),
            ),
            project_chat_manager=SimpleNamespace(
                get_project=lambda _pid: None,
                # stamp_fallback_project asks for the bound chat; None means
                # "cannot see it right now", which leaves the fallback alone.
                get_chat=lambda _cid: None,
                events=None,
            ),
            schedule_manager=store,
            background_runner=None,
        ),
        store=store,
    )


def _principal() -> McpPrincipal:
    return McpPrincipal(
        token_id="t",
        chat_id="chat-1",
        project_id="proj-1",
        workspace="work",
        provider="claude",
    )


def test_mcp_update_to_a_wall_clock_cadence_without_a_time_is_refused(plane) -> None:
    """The REST route rejects this; the MCP tool is the same door."""
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.plane.schedule_update(_principal(), "loop-abc", frequency="daily")

    assert excinfo.value.code == "invalid_time"
    assert plane.store.replaced == []
    assert plane.store.entries[0].frequency == INTERVAL_FREQUENCY


def test_mcp_update_with_a_time_is_accepted(plane) -> None:
    plane.plane.schedule_update(
        _principal(), "loop-abc", frequency="daily", daily_time_utc="09:30"
    )

    stored = plane.store.entries[0]
    assert stored.frequency == "daily"
    assert stored.daily_time_utc == "09:30"
    assert compute_next_run(stored) is not None


def test_mcp_update_staying_on_interval_still_needs_no_time(plane) -> None:
    plane.plane.schedule_update(_principal(), "loop-abc", interval_minutes=15)

    assert plane.store.entries[0].interval_minutes == 15
