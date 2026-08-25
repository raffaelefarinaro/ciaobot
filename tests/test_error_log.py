from __future__ import annotations

import asyncio
import logging
import logging.handlers
from pathlib import Path

import pytest

from ciao.error_log import (
    _is_benign_sdk_write_error,
    install_asyncio_noise_filter,
    resolve_log_level,
    setup_debug_logging,
    setup_error_logging,
    tail_debug_log,
)


@pytest.fixture(autouse=True)
def _isolated_root_logger():
    """Snapshot and restore the root logger around handler-attaching tests."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    for h in root.handlers:
        if h not in saved_handlers:
            h.close()
            root.removeHandler(h)
    root.setLevel(saved_level)


class _FakeCLIConnectionError(Exception):
    """Stand-in matching the SDK error by type name (no SDK import needed)."""


_FakeCLIConnectionError.__name__ = "CLIConnectionError"


def test_is_benign_sdk_write_error_matches_signature() -> None:
    assert _is_benign_sdk_write_error(
        _FakeCLIConnectionError("ProcessTransport is not ready for writing")
    )
    # Wrong type name.
    assert not _is_benign_sdk_write_error(
        RuntimeError("ProcessTransport is not ready for writing")
    )
    # Right type, unrelated message.
    assert not _is_benign_sdk_write_error(_FakeCLIConnectionError("some other error"))
    assert not _is_benign_sdk_write_error(None)


@pytest.mark.asyncio
async def test_noise_filter_swallows_benign_and_delegates_others() -> None:
    """The benign SDK write error is demoted; everything else still reaches the
    previous handler (issue #163)."""
    loop = asyncio.get_running_loop()
    seen: list[dict] = []
    loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx))

    install_asyncio_noise_filter()

    # Benign SDK control-task error: swallowed, does not reach the delegate.
    loop.call_exception_handler(
        {
            "message": "Task exception was never retrieved",
            "exception": _FakeCLIConnectionError(
                "ProcessTransport is not ready for writing"
            ),
        }
    )
    assert seen == []

    # Any other error still propagates to the previous handler.
    other = {"message": "boom", "exception": ValueError("nope")}
    loop.call_exception_handler(other)
    assert seen == [other]


@pytest.mark.asyncio
async def test_noise_filter_is_a_noop_without_prior_handler() -> None:
    """Installing when no prior handler exists must not raise on delegation."""
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(None)
    install_asyncio_noise_filter()
    # A benign error is swallowed; a real one falls back to the default handler
    # without raising (the default just logs).
    loop.call_exception_handler(
        {
            "message": "x",
            "exception": _FakeCLIConnectionError(
                "ProcessTransport is not ready for writing"
            ),
        }
    )


def test_resolve_log_level_defaults_to_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CIAO_LOG_LEVEL", raising=False)
    assert resolve_log_level() == logging.INFO
    for raw, expected in (
        ("debug", logging.DEBUG),
        ("DEBUG", logging.DEBUG),
        (" warning ", logging.WARNING),
        ("error", logging.ERROR),
        ("10", 10),
    ):
        monkeypatch.setenv("CIAO_LOG_LEVEL", raw)
        assert resolve_log_level() == expected


def test_resolve_log_level_invalid_falls_back_to_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CIAO_LOG_LEVEL", "chatty")
    assert resolve_log_level() == logging.INFO


def test_debug_logging_disabled_by_default(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.delenv("CIAO_LOG_LEVEL", raising=False)
    try:
        setup_error_logging(tmp_path)
        setup_debug_logging(tmp_path)
        root = logging.getLogger()
        file_handlers = [
            h
            for h in root.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert len(file_handlers) == 1  # error log only
        assert tail_debug_log(tmp_path) == ""
    finally:
        monkeypatch.undo()


def test_debug_logging_captures_debug_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIAO_LOG_LEVEL", "debug")
    setup_error_logging(tmp_path)
    setup_debug_logging(tmp_path)
    root = logging.getLogger()
    debug_path = tmp_path / ".runtime" / "server_debug.log"
    handlers = [
        h
        for h in root.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
        and h.baseFilename == str(debug_path)
    ]
    assert len(handlers) == 1
    assert root.level <= logging.DEBUG

    logging.getLogger("ciao.test.subject").debug("verbose breadcrumb")

    handlers[0].flush()
    content = tail_debug_log(tmp_path)
    assert "verbose breadcrumb" in content
    assert "ciao.test.subject" in content
    # ERROR records still land in the error log too.
    logging.getLogger("ciao.test.subject").error("real failure")
    handlers[0].flush()
    error_content = (tmp_path / ".runtime" / "server_errors.log").read_text()
    assert "real failure" in error_content


def test_setup_debug_logging_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIAO_LOG_LEVEL", "debug")
    setup_error_logging(tmp_path)
    setup_debug_logging(tmp_path)
    setup_debug_logging(tmp_path)  # second call must not duplicate
    root = logging.getLogger()
    debug_path = tmp_path / ".runtime" / "server_debug.log"
    handlers = [
        h
        for h in root.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
        and h.baseFilename == str(debug_path)
    ]
    assert len(handlers) == 1
