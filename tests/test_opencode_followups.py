"""Host wiring for OpenCode background follow-ups (#1391 Part B)."""

from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ciao.config import CiaoConfig
from ciao.models import ChatContext, ResultEvent
from ciao.provider_service import ProviderService
from ciao.providers.base import BACKGROUND_WORK_MAX_S
from ciao.providers.opencode import OpencodeProvider
from ciao.web.chat_broker import ChatStream
from ciao.web.chat_streaming import ChatStreaming
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
async def test_interim_reply_parks_without_a_move_flag(tmp_path):
    """A live drain parks an interim reply and announces a normal one."""

    async def drive(text: str) -> _RecordingDriveHost:
        host = _RecordingDriveHost(
            [ResultEvent(type="result", result=text, session_id="ses_x")], tmp_path
        )
        host.chat.provider = "opencode"
        host.chat.session_id = "ses_x"
        stream = ChatStream(prompt_text="hi")
        host._broker.register("chat-1", stream)
        await ChatStreaming(host).drive(  # type: ignore[arg-type]
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
        return host

    assert (await drive("waiting for my subagents")).announcements == []
    assert len((await drive("The work is done.")).announcements) == 1


@pytest.mark.asyncio
async def test_drain_between_turns_streams_an_opencode_wake(tmp_path, monkeypatch):
    pcm, project, chat = _opencode_chat(tmp_path)
    _, rest = _split(_fixture_lines("background_shell_wake.jsonl"), SHELL_SESSION)
    provider = _drain_provider(tmp_path, monkeypatch, SHELL_SESSION, rest)
    provider._background_shells = {"sh_bgshell"}
    _script(monkeypatch, provider, shells=[{"sh_bgshell"}, set()])
    pcm._providers[chat.chat_id] = SimpleNamespace(  # type: ignore[assignment]
        provider=provider,
        can_drain=True,
        drain_events=provider.drain_events,
        background_work_outstanding=False,
    )
    published = _capture_publish(pcm)

    await asyncio.wait_for(
        pcm._drain_between_turns(chat.chat_id, project.project_id), 5
    )

    kinds = [e["type"] for e in published if e.get("chat_id") == chat.chat_id]
    assert "chat_streaming_started" in kinds
    assert "chat_streaming_done" in kinds
    started = next(e for e in published if e["type"] == "chat_streaming_started")
    assert started["background"] is True
    assert chat.last_response == "BGDONE"
    assert chat.last_response_status == "success"


@pytest.mark.asyncio
async def test_drain_between_turns_records_the_opencode_wake_turn(tmp_path, monkeypatch):
    pcm, project, chat = _opencode_chat(tmp_path)
    _, rest = _split(_fixture_lines("background_shell_wake.jsonl"), SHELL_SESSION)
    provider = _drain_provider(tmp_path, monkeypatch, SHELL_SESSION, rest)
    provider._background_shells = {"sh_bgshell"}
    _script(monkeypatch, provider, shells=[{"sh_bgshell"}, set()])
    pcm._providers[chat.chat_id] = SimpleNamespace(  # type: ignore[assignment]
        provider=provider,
        can_drain=True,
        drain_events=provider.drain_events,
        background_work_outstanding=False,
    )

    await asyncio.wait_for(
        pcm._drain_between_turns(chat.chat_id, project.project_id), 5
    )

    ctx = ChatContext.for_web(chat.chat_id)
    rows = pcm._transcripts.current_messages(ctx, "opencode")
    assert [row["role"] for row in rows] == ["system", "assistant"]
    assert rows[0]["origin"] == "background_run"
    assert "— sleep 20 && echo BGDONE (sh_bgshell, ok, exit 0)" in rows[0]["content"]
    assert rows[1]["content"] == "BGDONE"
    transcript = pcm._transcripts._load_current(ctx, "opencode")
    assert len(transcript["turns"]) == 1
    assert transcript["turns"][0]["input_kind"] == "task-notification"


@pytest.mark.asyncio
async def test_recording_a_wake_turn_does_not_build_a_full_agent_request(tmp_path, monkeypatch):
    pcm, project, chat = _opencode_chat(tmp_path)
    _, rest = _split(_fixture_lines("background_shell_wake.jsonl"), SHELL_SESSION)
    provider = _drain_provider(tmp_path, monkeypatch, SHELL_SESSION, rest)
    provider._background_shells = {"sh_bgshell"}
    _script(monkeypatch, provider, shells=[{"sh_bgshell"}, set()])
    pcm._providers[chat.chat_id] = SimpleNamespace(  # type: ignore[assignment]
        provider=provider,
        can_drain=True,
        drain_events=provider.drain_events,
        background_work_outstanding=False,
    )
    chat.instructions_inline_pending = "/r/repo"
    built: list[bool] = []
    monkeypatch.setattr(
        pcm, "build_agent_request", lambda *a, **k: built.append(True)
    )

    await asyncio.wait_for(
        pcm._drain_between_turns(chat.chat_id, project.project_id), 5
    )

    assert built == []
    assert chat.instructions_inline_pending == "/r/repo"
    ctx = ChatContext.for_web(chat.chat_id)
    assert len(pcm._transcripts._load_current(ctx, "opencode")["turns"]) == 1


@pytest.mark.asyncio
async def test_idle_reaper_keeps_an_opencode_server_with_a_live_drain(tmp_path):
    pcm, _project, chat = _opencode_chat(tmp_path)
    disconnected: list[bool] = []

    async def disconnect() -> None:
        disconnected.append(True)

    # An OpenCode drain exists only while background work is outstanding; an
    # activity age past the horizon isolates the live-drain rule.
    service = SimpleNamespace(
        disconnect=disconnect,
        background_work_outstanding=True,
        background_activity_age=math.inf,
    )
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


# ── review round 1 ───────────────────────────────────────────────────────


def _announce_spy(pcm) -> list[tuple]:
    from tests.test_chat_subagents import _announce_spy as spy

    return spy(pcm)


@pytest.mark.asyncio
async def test_opencode_interim_turn_then_wake_announces_once(tmp_path, monkeypatch):
    pcm, project, chat = _opencode_chat(tmp_path)
    calls = _announce_spy(pcm)
    _, rest = _split(_fixture_lines("background_shell_wake.jsonl"), SHELL_SESSION)
    provider = _drain_provider(tmp_path, monkeypatch, SHELL_SESSION, rest)
    provider._background_shells = {"sh_bgshell"}
    _script(monkeypatch, provider, shells=[{"sh_bgshell"}, set()])
    pcm._providers[chat.chat_id] = SimpleNamespace(  # type: ignore[assignment]
        provider=provider,
        can_drain=True,
        drain_events=provider.drain_events,
        background_work_outstanding=False,
    )
    pcm._park_result_announce(chat.chat_id, project.project_id, "T", "interim")

    await asyncio.wait_for(pcm._drain_between_turns(chat.chat_id, project.project_id), 5)

    assert len(calls) == 1
    assert calls[0][3] == "BGDONE"


@pytest.mark.asyncio
async def test_reaper_keeps_a_server_with_outstanding_work_until_the_horizon(tmp_path):
    pcm, _project, chat = _opencode_chat(tmp_path)
    provider = _live_provider(tmp_path, chat.session_id)
    provider._background_shells = {"sh"}
    disconnected: list[bool] = []

    async def disconnect() -> None:
        disconnected.append(True)

    service = ProviderService(
        CiaoConfig(
            pwa_auth_token="t",
            workspace_root=tmp_path,
            state_path=tmp_path / "state.json",
            media_root=tmp_path / "media",
        ),
        provider="opencode",
    )
    service._provider = provider  # type: ignore[assignment]
    service.disconnect = disconnect  # type: ignore[method-assign]
    pcm._providers[chat.chat_id] = service  # type: ignore[assignment]
    pcm._provider_last_used[chat.chat_id] = time.monotonic() - 10**6

    # No drain is live (its SSE closed), but work is outstanding and recent.
    assert await pcm.reap_idle_providers(force=True) == []
    assert disconnected == []

    # Past the background-work cap with no provider activity the server is reclaimed.
    provider._last_activity = time.monotonic() - BACKGROUND_WORK_MAX_S - 1
    pcm._provider_last_used[chat.chat_id] = time.monotonic() - 10**6
    assert await pcm.reap_idle_providers(force=True) == [chat.chat_id]
    assert disconnected == [True]


class _ReportsWorkThenNone:
    """Two follow-up results; background work is outstanding for the first only."""

    def __init__(self) -> None:
        self.background_work_outstanding = True

    async def drain_events(self):
        yield ResultEvent(type="result", result="FIRST", session_id="s", origin="task-notification")
        self.background_work_outstanding = False
        yield ResultEvent(type="result", result="SECOND", session_id="s", origin="task-notification")


@pytest.mark.asyncio
async def test_drain_announces_once_when_the_provider_reports_no_background_work(tmp_path):
    pcm, project, chat = _opencode_chat(tmp_path)
    calls = _announce_spy(pcm)
    pcm._providers[chat.chat_id] = _ReportsWorkThenNone()  # type: ignore[assignment]
    pcm._park_result_announce(chat.chat_id, project.project_id, "T", "interim")

    await asyncio.wait_for(pcm._drain_between_turns(chat.chat_id, project.project_id), 5)

    # The first result left work outstanding, so it only replaced the park; the
    # second found none and announced once.
    assert len(calls) == 1
    assert calls[0][3] == "SECOND"
    assert chat.chat_id not in pcm._parked_result_announce


def test_background_work_outstanding_reads_the_chats_provider(tmp_path):
    pcm, _project, chat = _opencode_chat(tmp_path)
    assert pcm._background_work_outstanding(chat.chat_id) is False
    pcm._providers[chat.chat_id] = SimpleNamespace(background_work_outstanding=True)  # type: ignore[assignment]
    assert pcm._background_work_outstanding(chat.chat_id) is True
    pcm._providers[chat.chat_id] = SimpleNamespace(background_work_outstanding=False)  # type: ignore[assignment]
    assert pcm._background_work_outstanding(chat.chat_id) is False
