"""Tests for the idle provider sweep.

`ProjectChatManager._providers` is keyed by chat id and used to be emptied only
by a lifecycle event. opencode runs one `opencode serve` process per chat, so
without a reaper those processes accumulate for the life of the server. These
tests pin the safety conditions: a chat with work in flight, a parked question,
or a recent turn is never reclaimed.
"""

from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path

import pytest

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import (
    _CLAUDE_WARM_IDLE_TIMEOUT_SECONDS,
    _CLAUDE_WARM_PROVIDER_LIMIT,
    ProjectChatManager,
)


class _StubProvider:
    """Stands in for ProviderService; records that it was torn down."""

    background_work_outstanding = False
    background_activity_age = math.inf

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
    """`pending_queue` is chat state: it waits on the chat, no provider needed.

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


class _FailingProvider(_StubProvider):
    """A provider whose teardown raises — an opencode server that won't die."""

    def __init__(self, *, fail_times: int = 10_000) -> None:
        super().__init__()
        self.attempts = 0
        self._fail_times = fail_times

    async def disconnect(self) -> None:
        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise RuntimeError("provider process refused to terminate")
        self.disconnected = True


@pytest.mark.asyncio
async def test_a_failed_disconnect_keeps_the_provider_for_a_retry(
    tmp_path: Path,
) -> None:
    """The sweep pops before it disconnects, so a swallowed failure loses the
    only reference the shutdown hook could use to finish the teardown — an
    opencode process left holding a port with nothing left to kill it."""
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _FailingProvider()
    manager._providers[chat_id] = provider  # type: ignore[assignment]
    manager._provider_last_used[chat_id] = time.monotonic() - 10_000

    # Not reported as reclaimed: nothing was.
    assert await manager.reap_idle_providers() == []
    assert provider.attempts == 1
    # Still reachable, so the shutdown hook's snapshot of `_providers` (and the
    # next sweep) can try again.
    assert manager._providers[chat_id] is provider


@pytest.mark.asyncio
async def test_a_retried_disconnect_that_succeeds_is_reclaimed(
    tmp_path: Path,
) -> None:
    """The kept stamp is the stale one, so the retry is the very next sweep."""
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _FailingProvider(fail_times=1)
    manager._providers[chat_id] = provider  # type: ignore[assignment]
    manager._provider_last_used[chat_id] = time.monotonic() - 10_000

    assert await manager.reap_idle_providers() == []
    # No `force=True`: the second sweep only reaches it because the restored
    # stamp still reads as idle past the timeout.
    assert await manager.reap_idle_providers() == [chat_id]
    assert provider.attempts == 2
    assert provider.disconnected
    assert chat_id not in manager._providers
    assert chat_id not in manager._provider_disconnect_failures


@pytest.mark.asyncio
async def test_a_provider_that_never_disconnects_is_dropped_after_the_budget(
    tmp_path: Path,
) -> None:
    """An unbounded retry list is its own leak: the reference goes, loudly."""
    from ciao.web.project_chats import _PROVIDER_DISCONNECT_MAX_ATTEMPTS

    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _FailingProvider()
    manager._providers[chat_id] = provider  # type: ignore[assignment]
    manager._provider_last_used[chat_id] = time.monotonic() - 10_000

    for _ in range(_PROVIDER_DISCONNECT_MAX_ATTEMPTS):
        assert await manager.reap_idle_providers() == []

    assert provider.attempts == _PROVIDER_DISCONNECT_MAX_ATTEMPTS
    assert chat_id not in manager._providers
    assert chat_id not in manager._provider_last_used
    assert chat_id not in manager._provider_disconnect_failures


@pytest.mark.asyncio
async def test_a_provider_that_serves_a_turn_again_gets_its_budget_back(
    tmp_path: Path,
) -> None:
    """Failures separated by real use say nothing about the next teardown."""
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _FailingProvider()
    manager._providers[chat_id] = provider  # type: ignore[assignment]
    manager._provider_last_used[chat_id] = time.monotonic() - 10_000

    assert await manager.reap_idle_providers() == []
    assert manager._provider_disconnect_failures[chat_id] == 1

    manager.active_chat_ids = lambda: [chat_id]  # type: ignore[method-assign]
    await manager.reap_idle_providers()
    assert chat_id not in manager._provider_disconnect_failures


@pytest.mark.asyncio
async def test_disconnect_reports_whether_the_provider_is_gone(
    tmp_path: Path,
) -> None:
    """The lifecycle paths may ignore the answer; the sweep may not."""
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)

    assert await manager._disconnect_provider(chat_id, None) is True
    ok = _StubProvider()
    assert await manager._disconnect_provider(chat_id, ok) is True  # type: ignore[arg-type]
    assert ok.disconnected
    bad = _FailingProvider()
    assert await manager._disconnect_provider(chat_id, bad) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_idle_drain_does_not_hold_its_provider(tmp_path: Path) -> None:
    """A Claude drain lives as long as its CLI, so an idle one is not work (#1402)."""
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _attach(manager, chat_id, idle_for=manager._provider_idle_timeout + 1)
    drain = asyncio.ensure_future(asyncio.sleep(3600))
    manager._streaming._between_turn_drains[chat_id] = drain  # type: ignore[assignment]

    assert await manager.reap_idle_providers() == [chat_id]
    assert provider.disconnected
    await asyncio.sleep(0)
    assert drain.cancelled()


@pytest.mark.asyncio
async def test_a_drain_with_outstanding_background_work_holds_its_provider(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _attach(manager, chat_id, idle_for=manager._provider_idle_timeout * 10)
    provider.background_work_outstanding = True  # type: ignore[attr-defined]
    drain = asyncio.ensure_future(asyncio.sleep(3600))
    manager._streaming._between_turn_drains[chat_id] = drain  # type: ignore[assignment]

    try:
        assert await manager.reap_idle_providers(force=True) == []
        assert not provider.disconnected
    finally:
        drain.cancel()


@pytest.mark.asyncio
async def test_live_background_child_survives_the_idle_threshold_until_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live child holds its server across the idle threshold; its end releases it.

    Pins the backend owner for #1664: while the provider reports outstanding
    background work with recent activity, no sweep tears it down however long the
    chat has been idle, and the stamp is refreshed so completion does not reap
    it on the very next sweep. Once the work is gone the normal timeout applies.
    A provider whose work already ended is reaped on schedule, so a "running"
    row with nothing outstanding is stale status, not lost live work.
    """
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    threshold = manager._provider_idle_timeout
    clock = [1_000_000.0]
    monkeypatch.setattr("ciao.web.project_chats.time.monotonic", lambda: clock[0])
    provider = _StubProvider()
    manager._providers[chat_id] = provider  # type: ignore[assignment]
    manager._provider_last_used[chat_id] = clock[0]
    provider.background_work_outstanding = True
    provider.background_activity_age = 5.0

    for _ in range(3):
        clock[0] += threshold + 1
        assert await manager.reap_idle_providers() == []
        assert not provider.disconnected
        assert chat_id in manager._providers

    # Completion: the child ends. The stamp was refreshed by the last busy
    # sweep, so teardown waits one full idle window, then happens.
    provider.background_work_outstanding = False
    clock[0] += threshold - 1
    assert await manager.reap_idle_providers() == []
    clock[0] += 2
    assert await manager.reap_idle_providers() == [chat_id]
    assert provider.disconnected


@pytest.mark.asyncio
async def test_ended_background_work_does_not_hold_its_provider(tmp_path: Path) -> None:
    """No watcher and nothing outstanding: the idle timeout reclaims the server."""
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _attach(manager, chat_id, idle_for=manager._provider_idle_timeout + 1)
    assert manager._streaming.has_live_drain(chat_id) is False

    assert await manager.reap_idle_providers() == [chat_id]
    assert provider.disconnected


# ── The Claude warm pool ──────────────────────────────────────────────────
# A Claude provider is one CLI process whose rebuild costs a cold start that
# measured 13-53 s under load, which is what the SDK's initialize deadline
# times out on. The most recently used few therefore outlive the idle timeout.
# opencode is a whole `opencode serve` per chat and is never kept warm.


def _attach_claude(
    manager: ProjectChatManager, chat_id: str, *, idle_for: float
) -> _StubProvider:
    provider = _attach(manager, chat_id, idle_for=idle_for)
    manager._provider_kind[chat_id] = "claude"
    return provider


@pytest.mark.asyncio
async def test_recent_claude_providers_stay_warm_past_the_idle_timeout(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    idle = manager._provider_idle_timeout + 1
    # Four recent chats plus one that fell out of the pool.
    for offset in range(_CLAUDE_WARM_PROVIDER_LIMIT):
        _attach_claude(manager, _chat(manager), idle_for=idle + offset)
    stale = _chat(manager)
    stale_provider = _attach_claude(manager, stale, idle_for=idle + 1000)

    assert await manager.reap_idle_providers() == [stale]
    assert stale_provider.disconnected
    # The warm ones are still live and still stamped.
    assert len(manager._providers) == _CLAUDE_WARM_PROVIDER_LIMIT


@pytest.mark.asyncio
async def test_claude_provider_past_the_warm_window_is_reaped(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _attach_claude(
        manager, chat_id, idle_for=_CLAUDE_WARM_IDLE_TIMEOUT_SECONDS + 1
    )

    assert await manager.reap_idle_providers() == [chat_id]
    assert provider.disconnected


@pytest.mark.asyncio
async def test_opencode_provider_is_never_kept_warm(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    provider = _attach(manager, chat_id, idle_for=manager._provider_idle_timeout + 1)
    manager._provider_kind[chat_id] = "opencode"

    assert await manager.reap_idle_providers() == [chat_id]
    assert provider.disconnected


@pytest.mark.asyncio
async def test_force_reaps_even_a_warm_claude_provider(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    _attach_claude(manager, chat_id, idle_for=manager._provider_idle_timeout + 1)

    assert await manager.reap_idle_providers(force=True) == [chat_id]


def _claude_service(tmp_path: Path, *, task: bool, silent_for: float | None = None):
    from claude_agent_sdk import TaskStartedMessage

    from ciao.provider_service import ProviderService
    from ciao.providers.claude import ClaudeProvider

    provider = ClaudeProvider(tmp_path)
    if task:
        provider._observe_message(
            TaskStartedMessage(
                subtype="task_started", data={}, task_id="bash-1",
                description="sleep 9000", uuid="u", session_id="s",
                task_type="local_bash",
            )
        )
        if silent_for is not None:
            provider._last_activity = time.monotonic() - silent_for
    service = ProviderService(
        CiaoConfig(
            pwa_auth_token="t",
            workspace_root=tmp_path,
            state_path=tmp_path / "state.json",
            media_root=tmp_path / "media",
        ),
        provider="claude",
    )
    service._provider = provider  # type: ignore[assignment]
    disconnected: list[bool] = []

    async def disconnect() -> None:
        disconnected.append(True)

    service.disconnect = disconnect  # type: ignore[method-assign]
    return service, disconnected


@pytest.mark.asyncio
async def test_claude_native_task_holds_provider_past_idle_and_warm_limit(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    service, disconnected = _claude_service(tmp_path, task=True)
    manager._providers[chat_id] = service  # type: ignore[assignment]
    stale = time.monotonic() - manager._provider_idle_timeout * 10
    manager._provider_last_used[chat_id] = stale

    assert await manager.reap_idle_providers(force=True) == []
    assert disconnected == []
    # Busy providers get their stamp refreshed, so they are not reclaimed the
    # instant the task ends.
    assert manager._provider_last_used[chat_id] > stale

    # Without the task the same provider is reclaimed.
    service._provider._native_tasks.clear()  # type: ignore[union-attr]
    manager._provider_last_used[chat_id] = stale
    assert await manager.reap_idle_providers(force=True) == [chat_id]


@pytest.mark.asyncio
async def test_silent_background_work_older_than_one_hour_still_holds_provider(
    tmp_path: Path,
) -> None:
    from ciao.web.subagent_watchers import BACKGROUND_WORK_MAX_S

    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    service, disconnected = _claude_service(tmp_path, task=True, silent_for=7200)
    manager._providers[chat_id] = service  # type: ignore[assignment]
    manager._provider_last_used[chat_id] = time.monotonic() - 10**6

    assert await manager.reap_idle_providers(force=True) == []
    assert disconnected == []

    # Past the cap with no provider activity the provider is reclaimed.
    service._provider._last_activity = (  # type: ignore[union-attr]
        time.monotonic() - BACKGROUND_WORK_MAX_S - 1
    )
    manager._provider_last_used[chat_id] = time.monotonic() - 10**6
    assert await manager.reap_idle_providers(force=True) == [chat_id]
    assert disconnected == [True]
