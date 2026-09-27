"""The fail-closed legacy node-state startup gate (#636, kept by #642).

The behavior under test is one line of startup policy: a Mac whose
`node_state.json` says it was a *client* of another host — or says something
nobody can account for — must not run the writers, and a Mac that says *host*
or has no such file must. That policy is what keeps a second writer off a
runtime root that already has one, and it is now the *only* thing standing
there: node mode, the manager that used to answer the same question live, and
every route that could rewrite the role mid-session are gone (#642), so the
boot verdict is no longer one term of a conjunction.

`guard_legacy_writers` is the whole gate, so it is called directly; the
`ScheduleManager` check is the real consumer of the verdict, wired through
`main.py`'s own closure the way `main.py` wires it, so the predicate is shown
to have teeth against an actual writer rather than only returning a boolean.
The module-level test pins the wiring itself against the source, because the
gate is only worth anything if the two writer sites in `_run_server_locked`
read it — a gate nothing consults passes every assertion above it.
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
# 08:00 UTC: `tick()` credits a slot to the day it matched, so the one entry can
# be watched across the verdict the boot reached.
_SLOT = datetime(2026, 6, 15, 8, 0, tzinfo=UTC)


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


def test_no_node_state_manager_exists_and_the_gate_still_refuses(
    tmp_path: Path,
) -> None:
    """#642: the verdict refuses with nothing behind it but the file.

    The gate used to be ANDed with `NodeStateManager.is_active()`, a live
    re-read of the same file that node mode's routes could rewrite mid-session.
    The manager and those routes are gone, so the detector is the only input —
    and the input it is given here is a file on disk and a fresh runtime root,
    with no manager, no `/api/node/*` route and no proxy anywhere to soften the
    answer. Nothing about the refusal may depend on machinery that no longer
    exists.
    """
    import ciao
    import ciao.main as main_module

    # The module is gone from the package on disk, not merely unimported.
    assert not (Path(ciao.__file__).parent / "node_state.py").exists()

    for label, payload, kind in (
        ("client", _CLIENT, "client"),
        ("invalid", _INVALID, "invalid"),
    ):
        runtime_root = tmp_path / label
        runtime_root.mkdir()
        _write_state(runtime_root, payload)

        # No manager to answer instead, and none left for the gate to ask.
        assert not hasattr(main_module, "legacy_writers_active")
        assert not hasattr(main_module, "node_state_manager")
        assert "NodeStateManager" not in inspect.getsource(
            main._run_server_locked
        )

        legacy, armed = main.guard_legacy_writers(runtime_root)
        assert armed is False, label
        assert legacy.kind == kind, label
        # The state file is still there, untouched: the refusal is a refusal to
        # write, never a deletion that would silently re-arm the next boot.
        assert (runtime_root / "node_state.json").exists(), label


async def test_the_wired_predicate_stops_the_scheduler_for_client_and_invalid(
    tmp_path: Path,
) -> None:
    """The predicate `main.py` hands the scheduler, against the real writer.

    The wiring under test is the boot verdict on its own, so it is built here
    the way `main.py` builds it — `lambda: legacy_writers_armed` — rather than
    with a hardcoded constant: a predicate that returned the wrong thing for a
    kind would be caught by the gate tests but still let every missed slot fire.
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
        _legacy, legacy_writers_armed = main.guard_legacy_writers(runtime_root)
        assert legacy_writers_armed is False

        dispatched: list[str] = []

        async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
            dispatched.append(entry.schedule_id)

        mgr = ScheduleManager(
            store=store,
            dispatch_to_web=dispatch,
            is_node_active=lambda: legacy_writers_armed,
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
        _legacy, legacy_writers_armed = main.guard_legacy_writers(runtime_root)
        assert legacy_writers_armed is True

        dispatched: list[str] = []

        async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
            dispatched.append(entry.schedule_id)

        mgr = ScheduleManager(
            store=store,
            dispatch_to_web=dispatch,
            is_node_active=lambda: legacy_writers_armed,
        )

        fired = await mgr.catch_up(now=_AFTER_THE_SLOT)
        # The dispatch is awaited off-loop, so the fired list lands first.
        await asyncio.sleep(0.05)

        assert fired == [entry.schedule_id], label
        assert dispatched == [entry.schedule_id], label

        # The detector reads the file raw and never writes it, so an install
        # that armed the writers with no state file still has none after the
        # gate ran — the manager that used to create one on construction is
        # gone, and with it the write.
        if payload is None:
            assert not (runtime_root / "node_state.json").exists(), label


async def test_the_wired_predicate_follows_the_verdict_across_a_rewritten_file(
    tmp_path: Path,
) -> None:
    """A verdict read once is a snapshot; nothing may rewrite it under us now.

    While node mode existed, `/api/node/connect` and `/api/node/handover`
    rewrote the role under a running server and the live `is_active()` term is
    what let a promoted client get its automations back. Both are gone, so the
    honest property is the opposite one: a file rewritten after the boot does
    not re-arm a writer this boot refused, because the verdict is the only
    input and nothing re-reads it.
    """
    runtime_root = _runtime_root(tmp_path, _CLIENT)
    _legacy, legacy_writers_armed = main.guard_legacy_writers(runtime_root)
    assert legacy_writers_armed is False

    # The same file, rewritten to a role the old manager would have been happy
    # with. Nothing consults it now.
    _write_state(runtime_root, {"role": "host", "host_url": None})

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
        is_node_active=lambda: legacy_writers_armed,
    )

    assert await mgr.tick(now=_SLOT) is None
    await asyncio.sleep(0.05)
    assert dispatched == []


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
    # Both sites read the boot verdict rather than anything derived from it, so
    # the scheduler and the backup push alike stay shut on a refused boot.
    assert "is_node_active=lambda: legacy_writers_armed" in source
    assert "if not legacy_writers_armed:" in source
    assert "app.state.legacy_node_state = legacy_node_state" in source

    # The manager that used to be the second term is not reconstructed, and no
    # live re-read replaces the verdict: the gate is the file, once.
    assert "NodeStateManager" not in source
    assert "node_state_manager" not in source
    assert "legacy_writers_active" not in source
