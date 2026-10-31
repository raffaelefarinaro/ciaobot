"""Claude provider: recover from a missing claude.ai connector catalog.

Headless Claude Code fetches the account's connector list once, with a 5 s
timeout and no later retry, so one slow response leaves the CLI without its
claude.ai connectors. The provider watches for the rows after each spawn and
respawns the CLI on the next turn when they never showed up.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import ciao.providers.claude as claude_mod
from ciao.models import AgentRequest, SystemStatusEvent
from ciao.providers.claude import (
    CLAUDEAI_CATALOG_GAVE_UP_NOTICE,
    CLAUDEAI_CATALOG_RESPAWNED_NOTICE,
    CLAUDEAI_CATALOG_RETRY_NOTICE,
    _CLAUDEAI_CATALOG_MAX_RETRIES,
    ClaudeProvider,
)
from tests.test_claude_provider_disconnect import _connected_provider, _task_started

_NO_ROWS: dict[str, Any] = {"mcpServers": [{"name": "local", "status": "connected"}]}
_SLACK_ROW: dict[str, Any] = {"mcpServers": [{"name": "claude.ai Slack", "status": "pending"}]}


@pytest.fixture(autouse=True)
def _fast_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_mod, "_CLAUDEAI_CATALOG_DEADLINE_S", 0.05)
    monkeypatch.setattr(claude_mod, "_CLAUDEAI_CATALOG_POLL_S", 0.01)


@pytest.fixture
def patient_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    # For tests whose rows appear on a later poll: a 50 ms deadline let a loaded
    # xdist worker give up before the second poll ran (#2367). The watch returns
    # as soon as rows show, so the longer deadline costs nothing when it works.
    monkeypatch.setattr(claude_mod, "_CLAUDEAI_CATALOG_DEADLINE_S", 5.0)


class _StatusClient:
    def __init__(self, *responses: Any) -> None:
        self.get_mcp_status = AsyncMock(side_effect=list(responses) or None, return_value=_NO_ROWS)

    async def disconnect(self) -> None:
        return None


def _provider(tmp_path: Path, client: object) -> ClaudeProvider:
    provider = ClaudeProvider(tmp_path)
    provider._client = client  # type: ignore[assignment]
    provider._connected = True
    return provider


def _marker(tmp_path: Path) -> Path:
    return tmp_path / ".runtime" / claude_mod._CLAUDEAI_SEEN_MARKER


def _mark_seen(tmp_path: Path) -> None:
    _marker(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _marker(tmp_path).touch()


def _watch(provider: ClaudeProvider, client: object) -> None:
    asyncio.run(provider._watch_claudeai_catalog(client))  # type: ignore[arg-type]


def test_watch_marks_seen_and_resets_retries_when_rows_appear(tmp_path: Path, patient_watch: None) -> None:
    client = _StatusClient(_NO_ROWS, _SLACK_ROW)
    provider = _provider(tmp_path, client)
    provider._claudeai_catalog_retries = 1

    _watch(provider, client)

    assert _marker(tmp_path).exists()
    assert provider._claudeai_catalog_missing is False
    assert provider._claudeai_catalog_retries == 0


def test_watch_drops_stale_gave_up_notice_when_rows_appear(tmp_path: Path, patient_watch: None) -> None:
    client = _StatusClient(_SLACK_ROW)
    provider = _provider(tmp_path, client)
    provider._claudeai_gave_up_notice_pending = True

    _watch(provider, client)

    assert provider._take_respawn_notice() is None


def test_watch_flags_missing_and_emits_notice_when_seen_before(tmp_path: Path) -> None:
    _mark_seen(tmp_path)
    client = _StatusClient()
    provider = _provider(tmp_path, client)
    sink: list[str] = []
    provider._turn_status_sink = sink.append

    _watch(provider, client)

    assert provider._claudeai_catalog_missing is True
    assert sink == [CLAUDEAI_CATALOG_RETRY_NOTICE]


def test_watch_emits_gave_up_notice_after_max_retries(tmp_path: Path) -> None:
    _mark_seen(tmp_path)
    client = _StatusClient()
    provider = _provider(tmp_path, client)
    provider._claudeai_catalog_retries = _CLAUDEAI_CATALOG_MAX_RETRIES
    sink: list[str] = []
    provider._turn_status_sink = sink.append

    _watch(provider, client)

    assert sink == [CLAUDEAI_CATALOG_GAVE_UP_NOTICE]
    # Giving up forgets the account had connectors, so later chats stop retrying.
    assert not _marker(tmp_path).exists()


def test_watch_flags_missing_on_a_fresh_provider_after_engine_restart(tmp_path: Path) -> None:
    # An earlier engine process saw the rows; this one has seen none yet.
    _mark_seen(tmp_path)
    client = _StatusClient()
    provider = _provider(tmp_path, client)
    sink: list[str] = []
    provider._turn_status_sink = sink.append

    _watch(provider, client)

    assert provider._claudeai_catalog_missing is True
    assert sink == [CLAUDEAI_CATALOG_RETRY_NOTICE]


def test_watch_silent_when_account_never_had_connectors(tmp_path: Path) -> None:
    client = _StatusClient()
    provider = _provider(tmp_path, client)
    sink: list[str] = []
    provider._turn_status_sink = sink.append

    _watch(provider, client)

    assert provider._claudeai_catalog_missing is False
    assert sink == []


def test_watch_survives_status_errors(tmp_path: Path, patient_watch: None) -> None:
    client = _StatusClient(asyncio.TimeoutError(), _SLACK_ROW)
    provider = _provider(tmp_path, client)

    _watch(provider, client)

    assert _marker(tmp_path).exists()


def test_watch_stops_when_client_replaced(tmp_path: Path) -> None:
    _mark_seen(tmp_path)
    client = _StatusClient()
    provider = _provider(tmp_path, client)
    provider._client = _StatusClient()  # type: ignore[assignment]

    _watch(provider, client)

    assert provider._claudeai_catalog_missing is False
    client.get_mcp_status.assert_not_called()


def _request() -> AgentRequest:
    return AgentRequest(prompt="x", model="sonnet", mode="auto")


def test_ensure_connected_respawns_when_catalog_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    request = _request()
    provider, _live, stopped = _connected_provider(tmp_path, monkeypatch, request)
    provider._claudeai_catalog_missing = True
    # Stop right after the "client=new" log line: no CLI is spawned.
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: None)
    monkeypatch.setattr("ciao.tool_path.resolve_tool", lambda name: None)

    with caplog.at_level(logging.INFO, logger="ciao.providers.claude"):
        with pytest.raises(FileNotFoundError):
            asyncio.run(provider._ensure_connected(request))

    assert stopped == [True]
    assert provider._claudeai_catalog_retries == 1
    assert provider._claudeai_respawn_notice_pending is True
    assert "client=new reason=claudeai_catalog" in caplog.text


def test_ensure_connected_no_respawn_at_max_retries_or_with_background_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request()
    provider, live, stopped = _connected_provider(tmp_path, monkeypatch, request)
    provider._claudeai_catalog_missing = True
    provider._claudeai_catalog_retries = _CLAUDEAI_CATALOG_MAX_RETRIES

    assert asyncio.run(provider._ensure_connected(request)) is live
    assert stopped == []

    provider._claudeai_catalog_retries = 0
    provider._observe_message(_task_started("t1"))

    assert asyncio.run(provider._ensure_connected(request)) is live
    assert stopped == []
    assert provider._claudeai_catalog_retries == 0


def test_respawn_notice_emitted_once_on_next_turn(tmp_path: Path) -> None:
    provider = ClaudeProvider(tmp_path)
    provider._claudeai_respawn_notice_pending = True

    first = provider._take_respawn_notice()
    second = provider._take_respawn_notice()

    assert isinstance(first, SystemStatusEvent)
    assert first.status == CLAUDEAI_CATALOG_RESPAWNED_NOTICE
    assert second is None


def test_disconnect_cancels_watch_and_clears_missing(tmp_path: Path) -> None:
    async def run() -> tuple[asyncio.Task[None], ClaudeProvider]:
        client = _StatusClient()
        provider = _provider(tmp_path, client)
        provider._claudeai_catalog_missing = True
        task = asyncio.ensure_future(asyncio.sleep(60))
        provider._claudeai_watch = task
        await provider.disconnect()
        await asyncio.sleep(0)
        return task, provider

    task, provider = asyncio.run(run())

    assert task.cancelled()
    assert provider._claudeai_watch is None
    assert provider._claudeai_catalog_missing is False


def test_watch_not_started_when_connectors_disabled(tmp_path: Path) -> None:
    async def run() -> ClaudeProvider:
        client = _StatusClient()
        provider = _provider(tmp_path, client)
        provider._claudeai_expected = False
        provider._start_claudeai_watch(client)  # type: ignore[arg-type]
        return provider

    provider = asyncio.run(run())

    assert provider._claudeai_watch is None


def test_start_watch_creates_task_and_replaces_previous(tmp_path: Path) -> None:
    async def run() -> tuple[asyncio.Task[None], asyncio.Task[None]]:
        client = _StatusClient()
        provider = _provider(tmp_path, client)
        provider._claudeai_expected = True
        provider._start_claudeai_watch(client)  # type: ignore[arg-type]
        first = provider._claudeai_watch
        assert first is not None
        provider._start_claudeai_watch(client)  # type: ignore[arg-type]
        second = provider._claudeai_watch
        assert second is not None
        await asyncio.sleep(0)
        return first, second

    first, second = asyncio.run(run())

    assert first is not second
    assert first.cancelled()


def test_gave_up_notice_deferred_to_next_turn_when_no_sink(tmp_path: Path) -> None:
    _mark_seen(tmp_path)
    client = _StatusClient()
    provider = _provider(tmp_path, client)
    provider._claudeai_catalog_retries = _CLAUDEAI_CATALOG_MAX_RETRIES

    _watch(provider, client)

    first = provider._take_respawn_notice()
    second = provider._take_respawn_notice()
    assert isinstance(first, SystemStatusEvent)
    assert first.status == CLAUDEAI_CATALOG_GAVE_UP_NOTICE
    assert second is None
