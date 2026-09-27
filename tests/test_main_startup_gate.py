"""The fail-closed legacy node-state startup gate (#636).

The behavior under test is one line of startup policy: a Mac whose
`node_state.json` says it was a *client* of another host — or says something
nobody can account for — must not run the writers, and a Mac that says *host*
or has no such file must. That policy is what keeps a second writer off a
runtime root that already has one once #577 removes node mode.

`guard_legacy_writers` is the whole gate, so it is called directly; the
`ScheduleManager` check is the real consumer of the verdict, wired through
`main.legacy_writers_active` exactly as `main.py` wires it, so the predicate is
shown to have teeth against an actual writer rather than only returning a
boolean. The role is not fixed at boot in this child, so that wiring is
exercised across a real mid-session role change too, and the second term's
independence is pinned by a test of its own. The last test pins the wiring
itself against the source, because the gate is only worth anything if the two
writer sites in `_run_server_locked` read it — a gate nothing consults passes
every assertion above it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ciao import main
from ciao.legacy_node_state import writers_armed
from ciao.node_state import NodeStateManager
from ciao.schedules import ScheduleManager, ScheduleStore

# The three runtime-root shapes the gate has to tell apart, and the answer each
# one gets. `host` and `none` arm the writers; `client` and `invalid` do not.
_CLIENT = {"role": "client", "host_url": "https://studio.example:8443"}
_INVALID = {"role": "replica"}
_AFTER_THE_SLOT = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
# 08:00 UTC on three consecutive days: `tick()` credits a slot to the day it
# matched, so one entry can be watched over a role change a day at a time.
_DAY_ONE = datetime(2026, 6, 15, 8, 0, tzinfo=UTC)
_DAY_TWO = datetime(2026, 6, 16, 8, 0, tzinfo=UTC)
_DAY_THREE = datetime(2026, 6, 17, 8, 0, tzinfo=UTC)


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


def _wired_predicate(
    armed: bool, manager: NodeStateManager
) -> Callable[[], bool]:
    """The predicate `main.py` hands `ScheduleManager`, built as it builds it.

    `main._run_server_locked` wraps this in a closure over its own two locals,
    which a test cannot reach without booting the whole server, so the call
    itself is what gets exercised: a change to either half of the conjunction —
    the boot verdict or the live role — is felt by every test that wires its
    predicate through here.
    """
    return lambda: main.legacy_writers_active(armed, manager.is_active)


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


def test_startup_does_not_crash_on_a_state_it_cannot_decode(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The gate is called with nothing between it and the boot, so it answers.

    `guard_legacy_writers` runs in `_run_server_locked` outside any handler, so
    a state file the process cannot decode used to end the boot in
    `UnicodeDecodeError` instead of producing a verdict. A truncated or
    foreign-encoded file is a corrupt state, not a reason to refuse to start:
    it classifies `invalid` and says so, which is the one outcome that needs a
    human to decide something.
    """
    runtime_root = tmp_path / ".runtime"
    runtime_root.mkdir()
    (runtime_root / "node_state.json").write_bytes(b'{"role": "host"}\xff')

    with caplog.at_level("WARNING", logger="ciao.main"):
        legacy, armed = main.guard_legacy_writers(runtime_root)

    assert armed is False
    assert legacy.kind == "invalid"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "invalid" in warnings[0].getMessage()


async def test_the_wired_predicate_stops_the_scheduler_for_client_and_invalid(
    tmp_path: Path,
) -> None:
    """The predicate `main.py` hands the scheduler, against the real writer.

    The wiring under test is `legacy_writers_active(verdict, is_active)`, so it
    is built here through that call rather than with a hardcoded lambda: a
    predicate that returned the wrong thing for a kind would be caught by the
    gate tests but still let every missed slot fire.
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

        runtime_root = _runtime_root(tmp_path, payload)
        legacy, armed = main.guard_legacy_writers(runtime_root)
        assert armed is False

        dispatched: list[str] = []

        async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
            dispatched.append(entry.schedule_id)

        mgr = ScheduleManager(
            store=store,
            dispatch_to_web=dispatch,
            is_node_active=_wired_predicate(armed, NodeStateManager(runtime_root)),
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

        runtime_root = _runtime_root(tmp_path, payload)
        legacy, armed = main.guard_legacy_writers(runtime_root)
        assert armed is True

        dispatched: list[str] = []

        async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
            dispatched.append(entry.schedule_id)

        # A fresh install has no state file, so the manager is asked to create
        # one — the write the detector exists to avoid. It answers `host`, which
        # is why the verdict is the term that matters here: the manager's `host`
        # must never be the reason a writer arms.
        manager = NodeStateManager(runtime_root)

        mgr = ScheduleManager(
            store=store,
            dispatch_to_web=dispatch,
            is_node_active=_wired_predicate(armed, manager),
        )

        fired = await mgr.catch_up(now=_AFTER_THE_SLOT)
        # The dispatch is awaited off-loop, so the fired list lands first.
        await asyncio.sleep(0.05)

        assert fired == [entry.schedule_id], label
        assert dispatched == [entry.schedule_id], label


async def test_the_wired_predicate_follows_a_role_change_mid_session(
    tmp_path: Path,
) -> None:
    """A verdict read once is a snapshot; the role it was read from is not.

    Node mode is still in this child, and its routes rewrite the role under a
    running server: `/api/node/connect` and `/api/node/handover` do not request
    a restart, so the file `main.py` classified at boot can be rewritten a
    minute later. A predicate built from the verdict alone would keep
    dispatching this Mac's slots while its PWA tunnels to the host it just
    connected to — the second writer #636 exists to prevent — and would keep
    automations dead for a client promoted back to host. So the same entry, the
    same manager and the same wired predicate are watched across both
    transitions, one day at a time.

    This covers the interim conjunction and dies with it: #577B deletes the
    manager and `/api/node/*`, and with them the mid-session transition this
    test drives.
    """
    runtime_root = _runtime_root(tmp_path, {"role": "host"})
    _legacy, armed = main.guard_legacy_writers(runtime_root)
    assert armed is True
    manager = NodeStateManager(runtime_root)

    store_dir = tmp_path / "store"
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

    dispatched: list[str] = []

    async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
        dispatched.append(entry.schedule_id)

    mgr = ScheduleManager(
        store=store,
        dispatch_to_web=dispatch,
        is_node_active=_wired_predicate(armed, manager),
    )

    async def tick_and_settle(now: datetime) -> None:
        await mgr.tick(now=now)
        # The dispatch is awaited off-loop, so the fired list lands first.
        await asyncio.sleep(0.05)

    await tick_and_settle(_DAY_ONE)
    assert dispatched == [entry.schedule_id], "a host runs its own slots"

    # Settings -> Connect as client. The route writes the role and returns; the
    # server is not restarted.
    manager.connect_as_client("https://studio.example:8443")
    await tick_and_settle(_DAY_TWO)
    assert dispatched == [entry.schedule_id], (
        "a client must not dispatch a slot the host it tunneled to owns"
    )

    # And back again: a promoted client gets its automations without a restart.
    manager.promote()
    await tick_and_settle(_DAY_THREE)
    # Two of the three days, and the one in the middle is the client day.
    assert dispatched == [entry.schedule_id] * 2, "a host runs its own slots"


def test_a_manager_saying_host_never_overrides_the_verdict(
    tmp_path: Path,
) -> None:
    """The conjunction's other half: the manager never arms a writer alone.

    `NodeStateManager` answers `host` for a state this boot refused whenever the
    file has been rewritten since — which is exactly what #577B leaves behind
    when the detector's verdict becomes the only input. A predicate that took
    the manager's answer on its own would arm a writer off a `client` or
    `invalid` file the moment anything touched it, so both terms are required.
    """
    runtime_root = _runtime_root(tmp_path, _CLIENT)
    _legacy, armed = main.guard_legacy_writers(runtime_root)
    assert armed is False

    manager = NodeStateManager(runtime_root)
    # The same file, rewritten to a role the manager is happy with.
    _write_state(runtime_root, {"role": "host", "host_url": None})
    assert manager.is_active() is True

    assert main.legacy_writers_active(armed, manager.is_active) is False
    # The short circuit is what keeps a refused boot from re-reading the file
    # on every tick as well: the manager is not consulted at all.
    consulted: list[bool] = []

    def _never_called() -> bool:
        consulted.append(True)
        return True

    assert main.legacy_writers_active(armed, _never_called) is False
    assert consulted == []


def test_main_wires_the_gate_into_both_writer_sites() -> None:
    """The gate is only worth anything where the writers are armed.

    Asserted against the source because both sites are inside one very long
    `_run_server_locked` that boots the whole server: there is no way to reach
    them in a test that does not do exactly the thing this child must not do.
    Everything the wiring *computes* is covered above against the real
    `ScheduleManager`; what is left here is that both writer sites ask for it,
    which no behavior test can reach.
    """
    source = inspect.getsource(main._run_server_locked)

    assert "guard_legacy_writers(" in source
    # Both sites read the combined predicate rather than the boot verdict on
    # its own, so a role change under a running server is felt by the scheduler
    # and by the backup push alike.
    assert "is_node_active=_writers_active" in source
    assert "if not _writers_active():" in source
    assert "app.state.legacy_node_state = legacy_node_state" in source

    # The detector's verdict is one of the two terms, and the manager's is never
    # the whole of it: `is_active` alone would arm a writer for a state the
    # detector called `client` or `invalid`, and that is the answer which has to
    # win once #577B deletes the manager and `/api/node/*`.
    assert "legacy_writers_armed, node_state_manager.is_active" in source
    assert "is_node_active=node_state_manager.is_active" not in source

    # Read before the manager is built, because that manager *writes* a host
    # state when the file is absent: classified after it, a fresh install would
    # come back `host` off the detector's own write.
    assert source.index("guard_legacy_writers(") < source.index(
        "NodeStateManager(config.state_path.parent)"
    )
