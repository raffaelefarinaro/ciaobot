"""Tests for the idle provider sweep.

`ProjectChatManager._providers` is keyed by chat id and used to be emptied only
by a lifecycle event. opencode runs one `opencode serve` process per chat, so
without a reaper those processes accumulate for the life of the server. These
tests pin the safety conditions: a chat with work in flight, a parked question,
or a recent turn is never reclaimed.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


class _StubProvider:
    """Stands in for ProviderService; records that it was torn down."""

    def __init__(self) -> None:
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def _attach(manager: ProjectChatManager, chat_id: str, *, idle_for: float) -> _StubProvider:
    """Register a stub provider whose last use was ``idle_for`` seconds ago."""
    provider = _StubProvider()
    manager._providers[chat_id] = provider  # type: ignore[assignment]
    manager._provider_last_used[chat_id] = time.monotonic() - idle_for
    return provider


def _chat(manager: ProjectChatManager) -> str:
    project = manager.create_project("Reaper", workspace="work")
    return manager.create_chat(project.project_id, title="Chat").chat_id


@pytest.mark.asyncio
async def test_idle_provider_past_the_timeout_is_reclaimed(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _attach(manager, chat_id, idle_for=manager._provider_idle_timeout + 1)

    assert await manager.reap_idle_providers() == [chat_id]
    assert provider.disconnected
    assert chat_id not in manager._providers
    # The stamp goes with it, so a recycled chat id cannot inherit it.
    assert chat_id not in manager._provider_last_used


@pytest.mark.asyncio
async def test_recently_used_provider_is_left_alone(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _attach(manager, chat_id, idle_for=1.0)

    assert await manager.reap_idle_providers() == []
    assert not provider.disconnected
    assert chat_id in manager._providers


@pytest.mark.asyncio
async def test_a_chat_with_a_live_stream_is_never_reclaimed(tmp_path: Path) -> None:
    """Even forced, and even when idle far past the timeout."""
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _attach(manager, chat_id, idle_for=manager._provider_idle_timeout * 10)
    manager.active_chat_ids = lambda: [chat_id]  # type: ignore[method-assign]

    assert await manager.reap_idle_providers(force=True) == []
    assert not provider.disconnected


@pytest.mark.asyncio
async def test_a_parked_question_holds_its_provider(tmp_path: Path) -> None:
    """The reply routes back through the provider session that asked."""
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _attach(manager, chat_id, idle_for=manager._provider_idle_timeout * 10)
    manager._chats[chat_id].pending_question = "Which branch?"

    assert await manager.reap_idle_providers(force=True) == []
    assert not provider.disconnected

    manager._chats[chat_id].pending_question = ""
    assert await manager.reap_idle_providers(force=True) == [chat_id]
    assert provider.disconnected


@pytest.mark.asyncio
async def test_a_pending_approval_card_holds_its_provider(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _attach(manager, chat_id, idle_for=manager._provider_idle_timeout * 10)
    manager._chats[chat_id].pending_permission = "Bash(rm -rf /)"

    assert await manager.reap_idle_providers(force=True) == []
    assert not provider.disconnected


@pytest.mark.asyncio
async def test_an_unfinished_retry_loop_holds_its_provider(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _attach(manager, chat_id, idle_for=manager._provider_idle_timeout * 10)

    started = asyncio.Event()

    async def _never() -> None:
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(_never())
    await started.wait()
    manager._retry_tasks[chat_id] = task
    try:
        assert await manager.reap_idle_providers(force=True) == []
        assert not provider.disconnected
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_a_busy_chat_has_its_idle_clock_reset(tmp_path: Path) -> None:
    """Otherwise a long turn would be reclaimed the moment it finished."""
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    _attach(manager, chat_id, idle_for=manager._provider_idle_timeout * 10)
    manager.active_chat_ids = lambda: [chat_id]  # type: ignore[method-assign]

    await manager.reap_idle_providers()
    manager.active_chat_ids = lambda: []  # type: ignore[method-assign]

    assert await manager.reap_idle_providers() == []


@pytest.mark.asyncio
async def test_an_unstamped_provider_is_adopted_before_it_is_reclaimed(
    tmp_path: Path,
) -> None:
    """A provider attached by another path gets one grace sweep, not a teardown."""
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _StubProvider()
    manager._providers[chat_id] = provider  # type: ignore[assignment]

    assert await manager.reap_idle_providers(force=True) == []
    assert not provider.disconnected
    assert chat_id in manager._provider_last_used


@pytest.mark.asyncio
async def test_lifecycle_pop_forgets_the_idle_stamp(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    _attach(manager, chat_id, idle_for=1.0)

    assert manager._pop_provider(chat_id) is not None
    assert chat_id not in manager._providers
    assert chat_id not in manager._provider_last_used


@pytest.mark.asyncio
async def test_a_sweep_cancelled_mid_disconnect_puts_the_provider_back(
    tmp_path: Path,
) -> None:
    """Otherwise shutdown's snapshot misses it and leaks a half-closed transport.

    `reap_idle_providers` pops before it disconnects, and `CancelledError` is a
    BaseException that `_disconnect_provider`'s `except Exception` does not
    absorb. `stop_provider_reaper` is awaited *before* main.py snapshots
    `_providers`, so restoring here is what lets that hook finish the job.
    """
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)

    entered = asyncio.Event()

    class _HangingProvider(_StubProvider):
        async def disconnect(self) -> None:
            entered.set()
            await asyncio.sleep(3600)

    provider = _HangingProvider()
    manager._providers[chat_id] = provider  # type: ignore[assignment]
    manager._provider_last_used[chat_id] = time.monotonic() - 10_000

    sweep = asyncio.create_task(manager.reap_idle_providers(force=True))
    await entered.wait()
    sweep.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sweep

    # Back in the map, so the shutdown hook still sees and disconnects it.
    assert manager._providers[chat_id] is provider
    assert chat_id in manager._provider_last_used


@pytest.mark.asyncio
async def test_the_sweep_task_starts_once_and_stops_on_shutdown(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)

    manager._get_provider(chat_id)
    task = manager._provider_reaper
    assert task is not None and not task.done()

    # A second hand-out joins the running sweep rather than starting another.
    manager._get_provider(chat_id)
    assert manager._provider_reaper is task

    await manager.stop_provider_reaper()
    assert task.done()
    assert manager._provider_reaper is None


def test_the_sweep_is_not_started_without_a_running_loop(tmp_path: Path) -> None:
    """Managers are built in tests and CLI paths with no loop; that must not raise."""
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)

    manager._get_provider(chat_id)

    assert manager._provider_reaper is None
    assert chat_id in manager._provider_last_used


@pytest.mark.asyncio
async def test_a_parked_message_queue_does_not_pin_the_provider(
    tmp_path: Path,
) -> None:
    """`pending_queue` is chat state, re-seeded by the next `start_stream`.

    Treating it as busy would pin the provider forever for a chat that parked
    messages and was never reopened — the leak this sweep exists to close.
    """
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _attach(manager, chat_id, idle_for=manager._provider_idle_timeout * 10)
    manager._chats[chat_id].pending_queue = [{"id": "e1", "text": "later"}]

    assert await manager.reap_idle_providers() == [chat_id]
    assert provider.disconnected
    # The queue itself is untouched, so the next turn still flushes it.
    assert manager._chats[chat_id].pending_queue == [{"id": "e1", "text": "later"}]


@pytest.mark.parametrize("bad", ["0", "-5", "not-a-number", "nan", "inf", "-inf"])
def test_a_bad_env_override_falls_back_to_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """Each of these breaks the sweep in its own way, so none is accepted.

    `0` busy-loops it; `nan` makes the sleep timer never come due because every
    comparison against it is False; `inf` as the timeout means nothing is ever
    old enough to reclaim. `float()` accepts the last three happily, so a
    `<= 0` test alone would let them through.
    """
    from ciao.web.project_chats import (
        _PROVIDER_IDLE_TIMEOUT_SECONDS,
        _PROVIDER_REAP_INTERVAL_SECONDS,
    )

    monkeypatch.setenv("CIAO_PROVIDER_IDLE_TIMEOUT", bad)
    monkeypatch.setenv("CIAO_PROVIDER_REAP_INTERVAL", bad)
    manager = _make_manager(tmp_path)

    assert manager._provider_idle_timeout == _PROVIDER_IDLE_TIMEOUT_SECONDS
    assert manager._provider_reap_interval == _PROVIDER_REAP_INTERVAL_SECONDS


def test_a_valid_env_override_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIAO_PROVIDER_IDLE_TIMEOUT", "30")
    monkeypatch.setenv("CIAO_PROVIDER_REAP_INTERVAL", "5")
    manager = _make_manager(tmp_path)

    assert manager._provider_idle_timeout == 30.0
    assert manager._provider_reap_interval == 5.0
