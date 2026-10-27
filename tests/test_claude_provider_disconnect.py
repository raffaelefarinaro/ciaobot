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
import logging
import math
import sys
from pathlib import Path

from claude_agent_sdk import (
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
)

from ciao.models import AgentRequest, provider_reuse_key
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


# -- Native background tasks ------------------------------------------------


def _task_started(task_id: str, description: str = "sleep 60") -> TaskStartedMessage:
    return TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id=task_id,
        description=description,
        uuid=f"u-{task_id}",
        session_id="sess-1",
        task_type="local_bash",
    )


def _task_notification(task_id: str, status: str = "completed") -> TaskNotificationMessage:
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status=status,  # type: ignore[arg-type]
        output_file="/tmp/out",
        summary="done",
        uuid=f"n-{task_id}",
        session_id="sess-1",
    )


def _task_updated(task_id: str, status: str) -> TaskUpdatedMessage:
    return TaskUpdatedMessage(
        subtype="task_updated",
        data={},
        task_id=task_id,
        patch={"status": status},
        status=status,  # type: ignore[arg-type]
    )


def test_native_task_started_and_notification_toggle_background_work(
    tmp_path: Path,
) -> None:
    provider = ClaudeProvider(tmp_path)
    assert provider.background_work_outstanding is False
    assert provider.seconds_since_activity == math.inf

    provider._observe_message(_task_started("t1"))
    provider._observe_message(_task_started("t2", "tail -f log"))
    assert provider.background_work_outstanding is True
    assert provider.seconds_since_activity < 5
    rows = provider.background_shell_rows()
    assert [r["id"] for r in rows] == ["t1", "t2"]
    assert rows[1]["command"] == "tail -f log"

    # A non-terminal update keeps the task; a terminal one clears it.
    provider._observe_message(_task_updated("t1", "running"))
    assert provider.background_work_outstanding is True
    provider._observe_message(_task_notification("t1", "completed"))
    assert [r["id"] for r in provider.background_shell_rows()] == ["t2"]
    provider._observe_message(_task_updated("t2", "killed"))
    assert provider.background_work_outstanding is False


def test_disconnect_clears_native_tasks(tmp_path: Path) -> None:
    provider = _provider_with(_SdkLikeClient(), tmp_path)
    provider._connected = True
    provider._observe_message(_task_started("t1"))

    asyncio.run(provider.disconnect())

    assert provider.background_work_outstanding is False
    assert provider._native_tasks == {}


def test_a_dead_cli_clears_native_tasks(tmp_path: Path) -> None:
    client = _SdkLikeClient()
    provider = _provider_with(client, tmp_path)
    provider._connected = True
    provider._observe_message(_task_started("t1"))

    async def run() -> None:
        provider._forget_dead_cli(client)
        await asyncio.sleep(0)

    asyncio.run(run())

    assert provider.background_work_outstanding is False


def _connected_provider(tmp_path: Path, monkeypatch, request: AgentRequest):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", lambda options: object())
    provider = ClaudeProvider(tmp_path)
    live = object()
    provider._client = live  # type: ignore[assignment]
    provider._connected = True
    provider._current_model = request.model
    provider._current_mode = request.mode
    provider._fs_scope_key = (
        request.agent_fs_scope, request.agent_roots, request.agent_denied, request.agent_denied_except,
    )
    provider._mcp_token = provider_reuse_key(request)
    provider._spawn_key = provider._spawn_key_for(request)
    stopped: list[bool] = []

    async def fake_disconnect() -> None:
        stopped.append(True)
        provider._client = None
        provider._connected = False

    monkeypatch.setattr(provider, "disconnect", fake_disconnect)
    return provider, live, stopped


def test_spawn_options_change_defers_reconnect_while_tasks_run(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    request = AgentRequest(prompt="x", model="sonnet", mode="auto")
    provider, live, stopped = _connected_provider(tmp_path, monkeypatch, request)
    old_key = provider._spawn_key
    provider._observe_message(_task_started("t1"))
    changed = AgentRequest(
        prompt="x", model="sonnet", mode="auto", thinking_level="high",
    )

    with caplog.at_level(logging.INFO, logger="ciao.providers.claude"):
        assert asyncio.run(provider._ensure_connected(changed)) is live
    assert stopped == []
    assert provider._spawn_key == old_key
    assert "Deferring spawn_options reconnect: 1 background task(s) running" in caplog.text

    # Once the task ends the pending change takes effect.
    provider._observe_message(_task_notification("t1"))
    asyncio.run(provider._ensure_connected(changed))
    assert stopped == [True]


def test_token_change_reconnects_even_with_tasks_and_warns(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    request = AgentRequest(prompt="x", model="sonnet", mode="auto")
    provider, _live, stopped = _connected_provider(tmp_path, monkeypatch, request)
    provider._observe_message(_task_started("t1", "sleep 600"))
    provider._mcp_token = "stale-token"

    with caplog.at_level(logging.WARNING, logger="ciao.providers.claude"):
        asyncio.run(provider._ensure_connected(request))

    assert stopped == [True]
    assert "sleep 600" in caplog.text
    assert any(r.levelno == logging.WARNING for r in caplog.records)
