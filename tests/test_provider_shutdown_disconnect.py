"""Shutdown's per-provider disconnect: a timeout is a warning, not an error."""

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

