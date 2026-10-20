"""The Claude provider closes its SDK client exactly once.

The SDK's `disconnect()` is not idempotent and the subprocess transport's
`close()` is not re-entrant, so a second close of the same client raises
`AttributeError: 'NoneType' object has no attribute 'returncode'` (the
transport clears its process handle in the first close, and the second reads
`returncode` off None).

That happened during engine shutdown: the teardown disconnected the provider
while a fire-and-forget close was still in flight, both callers read the same
client out of the provider's slot, and the second close raised. The provider
now takes the client out of its slot before awaiting the close, so exactly one
caller owns it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ciao.providers.claude import ClaudeProvider


class _SdkLikeClient:
    """A client that behaves like the SDK's: closing it twice is fatal.

    Mirrors `SubprocessCLITransport.close()`: the process handle is cleared at
    the end of a close, and a close that finds it already cleared reads
    `returncode` off None.
    """

    def __init__(self) -> None:
        self.close_calls = 0
        self._process: object | None = object()

    async def disconnect(self) -> None:
        self.close_calls += 1
        if self._process is None:
            raise AttributeError(
                "'NoneType' object has no attribute 'returncode'"
            )
        # The real close awaits (stdin EOF, then the process), so a second
        # caller can arrive in the middle of the first.
        await asyncio.sleep(0)
        self._process = None


def _provider_with(client: object, tmp_path: Path) -> ClaudeProvider:
    provider = ClaudeProvider(tmp_path)
    provider._client = client  # type: ignore[assignment]
    return provider


def test_concurrent_disconnects_close_the_client_once(tmp_path: Path) -> None:
    client = _SdkLikeClient()
    provider = _provider_with(client, tmp_path)

    async def run() -> None:
        # The engine's teardown racing a fire-and-forget close.
        await asyncio.gather(provider.disconnect(), provider.disconnect())

    asyncio.run(run())
    assert client.close_calls == 1


def test_a_disconnect_racing_a_dead_cli_close_closes_the_client_once(
    tmp_path: Path,
) -> None:
    client = _SdkLikeClient()
    provider = _provider_with(client, tmp_path)
    reached_close = asyncio.Event()

    class _SlowClient(_SdkLikeClient):
        async def disconnect(self) -> None:
            reached_close.set()
            await super().disconnect()

    slow = _SlowClient()
    provider._client = slow  # type: ignore[assignment]

    async def run() -> None:
        # The teardown starts closing the client...
        closing = asyncio.ensure_future(provider.disconnect())
        await reached_close.wait()
        # ...and the drain consumer notices the CLI died while that close is
        # still in flight, and starts its own.
        provider._forget_dead_cli(slow)
        await closing
        # Let the background close task finish.
        await asyncio.sleep(0)

    asyncio.run(run())
    assert slow.close_calls == 1


def test_disconnect_clears_the_provider_state(tmp_path: Path) -> None:
    client = _SdkLikeClient()
    provider = _provider_with(client, tmp_path)
    provider._connected = True
    provider._turn_open = True
    provider._session_id = "sess-1"

    asyncio.run(provider.disconnect())

    assert provider._client is None
    assert provider._connected is False
    assert provider._turn_open is False
    assert provider._session_id is None
    assert client.close_calls == 1


def test_disconnect_without_a_client_is_a_no_op(tmp_path: Path) -> None:
    provider = ClaudeProvider(tmp_path)
    asyncio.run(provider.disconnect())
    assert provider._client is None
