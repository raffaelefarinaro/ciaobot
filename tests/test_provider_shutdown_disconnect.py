"""Shutdown teardown: provider disconnect timeouts and concurrent steps."""

from __future__ import annotations

import asyncio
import logging

from ciao import main


class _SlowProvider:
    async def disconnect(self) -> None:
        await asyncio.sleep(60)


class _BrokenProvider:
    async def disconnect(self) -> None:
        raise RuntimeError("boom")


def test_timeout_logs_a_warning_without_a_traceback(monkeypatch, caplog):
    monkeypatch.setattr(main, "PROVIDER_SHUTDOWN_TIMEOUT_S", 0.01)
    with caplog.at_level(logging.WARNING, logger="ciao.main"):
        asyncio.run(main._disconnect_for_shutdown(_SlowProvider()))  # type: ignore[arg-type]
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert "did not finish" in record.getMessage()


def test_other_failures_still_log_an_error(caplog):
    with caplog.at_level(logging.WARNING, logger="ciao.main"):
        asyncio.run(main._disconnect_for_shutdown(_BrokenProvider()))  # type: ignore[arg-type]
    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None



def test_bound_outlasts_the_sdk_stdin_eof_grace():
    # The Claude SDK waits 5 s after stdin EOF for the CLI to flush its session
    # file; cutting that short can lose the last assistant message.
    assert main.PROVIDER_SHUTDOWN_TIMEOUT_S > 5


def test_shutdown_steps_overlap():
    async def slow() -> None:
        await asyncio.sleep(0.2)

    async def run() -> float:
        loop = asyncio.get_running_loop()
        started = loop.time()
        await main._shutdown_concurrently(slow, slow)
        return loop.time() - started

    assert asyncio.run(run()) < 0.35


def test_a_failing_step_does_not_stop_the_others(caplog):
    finished: list[str] = []

    async def broken() -> None:
        raise RuntimeError("boom")

    async def fine() -> None:
        await asyncio.sleep(0)
        finished.append("fine")

    with caplog.at_level(logging.ERROR, logger="ciao.main"):
        asyncio.run(main._shutdown_concurrently(broken, fine))
    assert finished == ["fine"]
    [record] = caplog.records
    assert "broken" in record.getMessage()
    assert record.exc_info is not None
