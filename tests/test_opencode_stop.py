"""Provider-level stop responsiveness for opencode.

``OpencodeActiveHandle.stop()`` flags the turn and aborts the session. The
streaming pump must then end the turn on the next event (or immediately skip
reconnects / the poll backstop) instead of waiting for a ``session.idle`` that
a half-healthy SSE subscription may never deliver — that wait is what made
the Stop button feel dead with opencode.
"""

from __future__ import annotations

import asyncio

import pytest

from ciao.providers.opencode import OpencodeActiveHandle
from tests.test_opencode_provider import _provider
from tests.test_opencode_sse_recovery import (
    _DELTA,
    _PART_FULL,
    _RecoveryClient,
    _REQUEST,
)


def _wire_stop(provider, monkeypatch, client) -> None:
    """Wire the provider to the fake server without touching asyncio.sleep.

    The SSE-recovery helper patches asyncio.sleep with an instant coroutine
    that never yields to the event loop; that would starve any polling wait
    in this module, so only the server/session seams are replaced here. The
    reconnect backoff is never reached (the stop flag breaks the loop first).
    """

    async def fake_server(_request):
        return client

    async def fake_session(_request):
        return "s1"

    monkeypatch.setattr(provider, "_ensure_server", fake_server)
    monkeypatch.setattr(provider, "_ensure_session", fake_session)


class _GatedStream:
    """Streams ``lines``, parking before the second until the gate opens."""

    def __init__(self, lines: list[str], gate: asyncio.Event) -> None:
        self._lines = lines
        self._gate = gate

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        for i, line in enumerate(self._lines):
            if i == 1:
                await self._gate.wait()
            yield line.encode("utf-8")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_stop_flag_ends_the_pump_without_waiting_for_idle(
    tmp_path, monkeypatch
) -> None:
    provider = _provider(tmp_path)
    gate = asyncio.Event()
    # Attempt 1: delta arrives, then the SSE goes quiet (gated) with no
    # session.idle in sight.
    client = _RecoveryClient(
        [_GatedStream([_DELTA, _PART_FULL], gate)],
        messages=[],
    )
    _wire_stop(provider, monkeypatch, client)

    task = asyncio.create_task(
        _collect(provider.run_streaming(_REQUEST, lambda _h: None))
    )
    # Wait until the delta was pumped before stopping.
    while not provider._answer_parts.get("p1") and not task.done():
        await asyncio.sleep(0.01)

    handle = OpencodeActiveHandle(provider, "s1")
    await asyncio.wait_for(handle.stop(), timeout=1.0)
    assert provider._stop_requested == "s1"

    # Release the gated event (not an idle event): the pump must end the
    # turn on its next iteration because the stop flag is set.
    gate.set()
    events = await asyncio.wait_for(task, timeout=2.0)

    # No reconnect, no poll backstop: the flag short-circuited recovery.
    assert client.stream_calls == 1
    assert "/session/s1/message" not in client.get_calls
    result = events[-1]
    assert result.type == "result"
    assert not result.is_error
    # Only the pre-stop delta is the answer; the post-stop event was dropped.
    assert result.result == "Hello"


async def _collect(agen):
    return [event async for event in agen]
