"""Host wiring for OpenCode background follow-ups (#1391 Part B)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ciao.config import CiaoConfig
from ciao.models import ResultEvent
from ciao.provider_service import ProviderService
from ciao.providers import opencode as opencode_module
from ciao.providers.opencode import OpencodeProvider
from ciao.web.chat_broker import ChatStream
from ciao.web.chat_streaming import ChatStreaming, ChatStreamingHost
from tests.test_chat_streaming_boundary import _DriveHost
from tests.test_chat_subagents import _make_manager
from tests.test_opencode_background_wake import (
    SHELL_SESSION,
    _drain_provider,
    _fixture_lines,
    _script,
    _split,
)


def _live_provider(tmp_path: Path, session: str) -> OpencodeProvider:
    provider = OpencodeProvider(tmp_path)
    provider._client = object()  # type: ignore[assignment]
    provider._process = SimpleNamespace(returncode=None)  # type: ignore[assignment]
    provider._session_id = session
    return provider


def _opencode_chat(tmp_path: Path):
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("followups", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="followups")
    chat.provider = "opencode"
    chat.session_id = "ses_x"
    pcm._save()
    return pcm, project, chat


def _capture_publish(pcm) -> list[dict]:
    published: list[dict] = []
    original = pcm._events.publish

    def capture(payload: dict) -> None:
        published.append(payload)
        original(payload)

    pcm._events.publish = capture  # type: ignore[method-assign]
    return published


class _RecordingDriveHost(_DriveHost):
    def __init__(self, events, workspace_root) -> None:
        super().__init__(events, workspace_root)
        self.drains: list[tuple[str, str]] = []

    def _start_between_turns_drain(self, chat_id: str, project_id: str) -> None:
        self.drains.append((chat_id, project_id))


@pytest.mark.asyncio
async def test_turn_done_starts_the_between_turns_drain_for_opencode(tmp_path):
    host = _RecordingDriveHost(
        [ResultEvent(type="result", result="STARTED", session_id="ses_x")], tmp_path
    )
    host.chat.provider = "opencode"
    host.chat.session_id = "ses_x"
    streaming = ChatStreaming(host)  # type: ignore[arg-type]
    stream = ChatStream(prompt_text="hi")
    host._broker.register("chat-1", stream)

    await streaming.drive(
        chat_id="chat-1",
        project_id="project-1",
        prompt="hi",
        images=None,
        turn_index=None,
        chat_meta=host.chat,
        stream=stream,
        is_retry=False,
        unattended=False,
    )

    assert host.drains == [("chat-1", "project-1")]


@pytest.mark.asyncio
async def test_drain_between_turns_streams_an_opencode_wake(tmp_path, monkeypatch):
    pcm, project, chat = _opencode_chat(tmp_path)
    _, rest = _split(_fixture_lines("background_shell_wake.jsonl"), SHELL_SESSION)
    provider = _drain_provider(tmp_path, monkeypatch, SHELL_SESSION, rest)
    provider._background_shells = {"sh_bgshell"}
    _script(monkeypatch, provider, shells=[{"sh_bgshell"}, set()], tree=[[]])
    pcm._providers[chat.chat_id] = SimpleNamespace(  # type: ignore[assignment]
        provider=provider, can_drain=True, drain_events=provider.drain_events
    )
    published = _capture_publish(pcm)

    await asyncio.wait_for(
        pcm._drain_between_turns(chat.chat_id, project.project_id), 5
    )

    kinds = [e["type"] for e in published if e.get("chat_id") == chat.chat_id]
    assert "chat_streaming_started" in kinds
    assert "chat_streaming_done" in kinds
    assert chat.last_response == "BGDONE"
    assert chat.last_response_status == "success"


async def _watch(pcm, project, chat, provider, monkeypatch, shells):
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)  # type: ignore[assignment]
    provider._session_id = chat.session_id
    monkeypatch.setattr(provider, "read_live_collab_tree", AsyncMock(return_value=[]))
    refresh = AsyncMock(side_effect=shells)
    wait = AsyncMock()
    monkeypatch.setattr(provider, "refresh_background_shells", refresh)
    monkeypatch.setattr(provider, "wait_background_change", wait)
    published = _capture_publish(pcm)
    await asyncio.wait_for(
        pcm._subagents._watch_opencode(chat.chat_id, project.project_id), 5
    )
    return published, wait


@pytest.mark.asyncio
async def test_opencode_watcher_counts_background_shells(tmp_path, monkeypatch):
    pcm, project, chat = _opencode_chat(tmp_path)
    provider = _live_provider(tmp_path, chat.session_id)

    published, _wait = await _watch(pcm, project, chat, provider, monkeypatch, [1, 0])

    remaining = [e["remaining"] for e in published if e.get("type") == "chat_subagents_ready"]
    assert remaining == [1, 0]


@pytest.mark.asyncio
async def test_opencode_watcher_waits_on_background_change_when_live(tmp_path, monkeypatch):
    pcm, project, chat = _opencode_chat(tmp_path)
    provider = _live_provider(tmp_path, chat.session_id)

    _published, wait = await _watch(pcm, project, chat, provider, monkeypatch, [1, 1, 0])

    assert [c.args for c in wait.await_args_list] == [(3.0,), (3.0,)]


@pytest.mark.asyncio
async def test_opencode_watcher_stops_for_a_restart_drain(tmp_path, monkeypatch):
    pcm, project, chat = _opencode_chat(tmp_path)
    provider = _live_provider(tmp_path, chat.session_id)
    pcm._restart_draining = True

    published, wait = await _watch(pcm, project, chat, provider, monkeypatch, [1, 1, 1])

    assert wait.await_count == 0
    remaining = [e["remaining"] for e in published if e.get("type") == "chat_subagents_ready"]
    assert remaining == [1]


@pytest.mark.asyncio
async def test_idle_reaper_keeps_an_opencode_server_with_a_live_drain(tmp_path):
    pcm, _project, chat = _opencode_chat(tmp_path)
    disconnected: list[bool] = []

    async def disconnect() -> None:
        disconnected.append(True)

    service = SimpleNamespace(disconnect=disconnect)
    pcm._providers[chat.chat_id] = service  # type: ignore[assignment]
    pcm._provider_last_used[chat.chat_id] = time.monotonic() - 10**6
    drain = asyncio.ensure_future(asyncio.Event().wait())
    pcm._between_turn_drains[chat.chat_id] = drain  # type: ignore[assignment]
    try:
        assert await pcm.reap_idle_providers(force=True) == []
        assert chat.chat_id in pcm._providers
        assert disconnected == []
    finally:
        drain.cancel()


class _PruneProvider:
    def __init__(self, outstanding: bool) -> None:
        self.background_work_outstanding = outstanding

    async def run_streaming(self, request, register):
        del request, register
        yield ResultEvent(type="result", result="ok", session_id="s")


async def _run_prune(tmp_path, monkeypatch, outstanding: bool) -> list[bool]:
    from ciao.models import AgentRequest

    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=tmp_path / "state.json",
        media_root=tmp_path / "media",
    )
    service = ProviderService(config, provider="opencode")
    service._provider = _PruneProvider(outstanding)  # type: ignore[assignment]
    disconnects: list[bool] = []

    async def disconnect() -> None:
        disconnects.append(True)

    monkeypatch.setattr(service, "disconnect", disconnect)
    monkeypatch.setattr(
        "ciao.provider_service.prune_expired_entries",
        lambda *_a, **_k: {"removed": {"memory": 1}},
    )
    request = AgentRequest(prompt="hi", model="", mode="default", provider="opencode")
    [e async for e in service.execute_streaming(request)]
    return disconnects


@pytest.mark.asyncio
async def test_memory_prune_defers_reconnect_while_background_work_runs(tmp_path, monkeypatch):
    assert await _run_prune(tmp_path, monkeypatch, True) == []


@pytest.mark.asyncio
async def test_memory_prune_still_reconnects_without_background_work(tmp_path, monkeypatch):
    assert await _run_prune(tmp_path, monkeypatch, False) == [True]


@pytest.mark.asyncio
async def test_stop_on_a_background_opencode_stream_interrupts_the_wake(tmp_path, monkeypatch):
    pcm, _project, chat = _opencode_chat(tmp_path)
    provider = _live_provider(tmp_path, chat.session_id)
    abort = AsyncMock()
    monkeypatch.setattr(provider, "abort_session", abort)
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)  # type: ignore[assignment]
    stream = ChatStream(background=True)
    pcm._broker.register(chat.chat_id, stream)

    assert await pcm.stop_chat(chat.chat_id) is True

    abort.assert_awaited_once_with(chat.session_id)
