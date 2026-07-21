from __future__ import annotations

import asyncio

import pytest

from ciao.error_log import (
    _is_benign_sdk_write_error,
    install_asyncio_noise_filter,
)


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
