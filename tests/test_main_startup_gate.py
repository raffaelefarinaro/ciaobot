"""The fail-closed legacy node-state startup gate (#636).

The behavior under test is one line of startup policy: a Mac whose
`node_state.json` says it was a *client* of another host — or says something
nobody can account for — must not run the writers, and a Mac that says *host*
or has no such file must. That policy is what keeps a second writer off a
runtime root that already has one once #577 removes node mode.

`guard_legacy_writers` is the whole gate, so it is called directly; the
`ScheduleManager` check is the real consumer of the verdict, wired exactly as
`main.py` wires it, so the predicate is shown to have teeth against an actual
writer rather than only returning a boolean. The last test pins the wiring
itself against the source, because the gate is only worth anything if the two
writer sites in `_run_server_locked` read it — a gate nothing consults passes
every assertion above it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ciao import main
from ciao.legacy_node_state import writers_armed
from ciao.schedules import ScheduleManager, ScheduleStore

# The three runtime-root shapes the gate has to tell apart, and the answer each
# one gets. `host` and `none` arm the writers; `client` and `invalid` do not.
_CLIENT = {"role": "client", "host_url": "https://studio.example:8443"}
_INVALID = {"role": "replica"}
_AFTER_THE_SLOT = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def _write_state(runtime_root: Path, payload: dict[str, object]) -> None:
    (runtime_root / "node_state.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _runtime_root(tmp_path: Path, payload: dict[str, object] | None) -> Path:
    runtime_root = tmp_path / ".runtime"
    runtime_root.mkdir(exist_ok=True)
    if payload is not None:
        _write_state(runtime_root, payload)
    return runtime_root


@pytest.mark.parametrize(
    "payload",
    [None, {"role": "host", "host_url": None}, {"role": "active"}],
    ids=["none", "host", "active"],
)
def test_startup_arms_the_writers_for_host_and_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, payload: dict | None
) -> None:
    runtime_root = _runtime_root(tmp_path, payload)

    with caplog.at_level("WARNING", logger="ciao.main"):
        legacy, armed = main.guard_legacy_writers(runtime_root)

    assert armed is True
    assert writers_armed(legacy) is True
    # A refusal is a warning the operator has to act on. Arming quietly keeps
    # the log readable, and a normal host start must not look like a problem.
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_startup_refuses_the_writers_for_a_legacy_client(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runtime_root = _runtime_root(tmp_path, _CLIENT)

    with caplog.at_level("WARNING", logger="ciao.main"):
        legacy, armed = main.guard_legacy_writers(runtime_root)

    assert armed is False
    assert legacy.kind == "client"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    # The host is named because it is the answer to "where do I go instead",
    # and the kind is named because "client" and "invalid" are different
    # problems with different fixes.
    assert "client" in message
    assert "https://studio.example:8443" in message
    assert str(runtime_root / "node_state.json") in message


def test_startup_refuses_the_writers_for_an_unaccountable_state(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runtime_root = _runtime_root(tmp_path, _INVALID)

    with caplog.at_level("WARNING", logger="ciao.main"):
        legacy, armed = main.guard_legacy_writers(runtime_root)

    assert armed is False
    assert legacy.kind == "invalid"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    # Explicit-choice wording: an unreadable role is not a client we can hand
    # anyone, so the log has to say a human has to decide rather than naming a
    # host it does not have.
    assert "invalid" in message
    assert str(runtime_root / "node_state.json") in message


async def test_the_wired_predicate_stops_the_scheduler_for_client_and_invalid(
    tmp_path: Path,
) -> None:
    """The predicate `main.py` hands the scheduler, against the real writer.

    `is_node_active=lambda: writers_armed(legacy_node_state)` is the wiring under
    test, so it is built here exactly as `main.py` builds it rather than with a
    hardcoded lambda: a predicate that returned the wrong thing for a kind would
    be caught by the gate tests but still let every missed slot fire.
    """
    for label, payload in (("client", _CLIENT), ("invalid", _INVALID)):
        store_dir = tmp_path / label
        store_dir.mkdir()
        store = ScheduleStore(store_dir)
        entry = store.create(
            daily_time_utc="08:00",
            prompt="daily summary",
            model="sonnet",
            mode="bypass",
            chat_id=0,
            frequency="daily",
            timezone_name="UTC",
        )
        entry.created_at = "2026-01-01T00:00:00Z"
        store.replace(entry)

        legacy, armed = main.guard_legacy_writers(_runtime_root(tmp_path, payload))
        assert armed is False

        dispatched: list[str] = []

        async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
            dispatched.append(entry.schedule_id)

        mgr = ScheduleManager(
            store=store,
            dispatch_to_web=dispatch,
            is_node_active=lambda: writers_armed(legacy),
        )

        assert await mgr.catch_up(now=_AFTER_THE_SLOT) == [], label
        assert dispatched == [], label


async def test_the_wired_predicate_lets_the_scheduler_run_for_host_and_none(
    tmp_path: Path,
) -> None:
    for label, payload in (("none", None), ("host", {"role": "host"})):
        store_dir = tmp_path / label
        store_dir.mkdir()
        store = ScheduleStore(store_dir)
        entry = store.create(
            daily_time_utc="08:00",
            prompt="daily summary",
            model="sonnet",
            mode="bypass",
            chat_id=0,
            frequency="daily",
            timezone_name="UTC",
        )
        entry.created_at = "2026-01-01T00:00:00Z"
        store.replace(entry)

        legacy, armed = main.guard_legacy_writers(_runtime_root(tmp_path, payload))
        assert armed is True

        dispatched: list[str] = []

        async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
            dispatched.append(entry.schedule_id)

        mgr = ScheduleManager(
            store=store,
            dispatch_to_web=dispatch,
            is_node_active=lambda: writers_armed(legacy),
        )

        fired = await mgr.catch_up(now=_AFTER_THE_SLOT)
        # The dispatch is awaited off-loop, so the fired list lands first.
        await asyncio.sleep(0.05)

        assert fired == [entry.schedule_id], label
        assert dispatched == [entry.schedule_id], label


def test_main_wires_the_gate_into_both_writer_sites() -> None:
    """The gate is only worth anything where the writers are armed.

    Asserted against the source because both sites are inside one very long
    `_run_server_locked` that boots the whole server: there is no way to reach
    them in a test that does not do exactly the thing this child must not do.
    """
    source = inspect.getsource(main._run_server_locked)

    assert "guard_legacy_writers(" in source
    assert "is_node_active=lambda: writers_armed(legacy_node_state)" in source
    assert "app.state.legacy_node_state = legacy_node_state" in source
    assert "if not legacy_writers_armed:" in source

    # The detector is the answer that wins; the manager's own verdict is no
    # longer what arms a writer. #577B deletes the manager outright, so a
    # surviving `node_state_manager.is_active` here would be a gate that stops
    # working the moment the file it reads is gone.
    assert "node_state_manager.is_active" not in source

    # Read before the manager is built, because that manager *writes* a host
    # state when the file is absent: classified after it, a fresh install would
    # come back `host` off the detector's own write.
    assert source.index("guard_legacy_writers(") < source.index(
        "NodeStateManager(config.state_path.parent)"
    )
