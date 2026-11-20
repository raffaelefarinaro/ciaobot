"""Startup wiring for discarding a never-applied staged update (#3410).

The discard runs off the event loop and is never awaited: removing a whole
staged env can take a while, and binding must not wait for it. It must also
never fail startup, so every exception stays inside its own task.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from pathlib import Path

import pytest

import ciao.engine_update as engine_update
from ciao import main as engine_main

MAIN_SOURCE = Path(engine_main.__file__).read_text(encoding="utf-8")


def test_startup_schedules_the_discard_without_awaiting_it_before_recovery() -> None:
    discard = MAIN_SOURCE.index("asyncio.create_task(_discard_staged_engine_update())")
    recover = MAIN_SOURCE.index("from ciao.engine_update import recover_interrupted_apply")

    assert "await _discard_staged_engine_update()" not in MAIN_SOURCE
    assert discard < recover
    assert re.search(r"asyncio\.create_task\(_discard_staged_engine_update\(\)\)", MAIN_SOURCE)


def test_discard_runs_retire_on_a_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, int] = {}

    def record() -> bool:
        seen["thread"] = threading.get_ident()
        return False

    monkeypatch.setattr(engine_update, "retire_staged", record)

    async def run() -> int:
        loop_thread = threading.get_ident()
        await engine_main._discard_staged_engine_update()
        return loop_thread

    loop_thread = asyncio.run(run())

    assert seen["thread"] != loop_thread


def test_discard_does_not_block_the_loop_or_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def slow_retire() -> bool:
        release.wait(timeout=5)
        return True

    monkeypatch.setattr(engine_update, "retire_staged", slow_retire)

    async def run() -> bool:
        task = asyncio.create_task(engine_main._discard_staged_engine_update())
        # Startup carries on while the discard is still running in its thread.
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=2)
        still_running = not task.done()
        release.set()
        await task
        return still_running

    assert asyncio.run(run()) is True


def test_an_exception_in_the_discard_is_logged_and_never_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> bool:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(engine_update, "retire_staged", broken)

    async def run() -> None:
        task = asyncio.create_task(engine_main._discard_staged_engine_update())
        await task
        assert task.exception() is None

    with caplog.at_level(logging.ERROR, logger="ciao.main"):
        asyncio.run(run())

    assert "Staged engine update cleanup failed" in caplog.text
