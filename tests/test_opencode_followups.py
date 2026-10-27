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
from ciao.providers import opencode as opencode_module
from ciao.providers.opencode import OpencodeProvider
from ciao.web.chat_broker import ChatStream
from ciao.web.chat_streaming import ChatStreaming
from ciao.web.project_chats import ProjectChatManager
from ciao.web.subagent_watchers import BACKGROUND_WORK_MAX_S
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
async def test_a_moved_turn_parks_its_announce_for_the_wake(tmp_path):
    async def drive(moved: bool) -> _RecordingDriveHost:
        host = _RecordingDriveHost(
            [ResultEvent(type="result", result="Moved it; the output comes later.", session_id="ses_x")], tmp_path
        )
        host.chat.provider = "opencode"
        host.chat.session_id = "ses_x"
        streaming = ChatStreaming(host)  # type: ignore[arg-type]
        stream = ChatStream(prompt_text="hi")
        stream.moved_to_background = moved
        host._broker.register("chat-1", stream)
        await streaming.drive(chat_id="chat-1", project_id="project-1", prompt="hi",
            images=None, turn_index=None, chat_meta=host.chat, stream=stream,
            is_retry=False, unattended=False)
        return host

    assert (await drive(True)).announcements == []
    assert len((await drive(False)).announcements) == 1


@pytest.mark.asyncio
async def test_queued_followup_after_a_move_announces_normally(tmp_path):
    class _TwoTurnHost(_RecordingDriveHost):
        def __init__(self):
            super().__init__([], tmp_path)
            self.active_stream = None
            self.turn = 0

        @staticmethod
        def _native_question_request_id(_pending_question):
            return ""

        async def stream_chat(self, chat_id, prompt, images=None, *, unattended=False, compact=False):
            del chat_id, images, unattended, compact
            self.turn += 1
            if self.turn == 1:
                self.active_stream.moved_to_background = True
                self.active_stream.enqueue("follow-up")
                yield ResultEvent(type="result", result="Moved it; output comes later.", session_id="ses_x")
            else:
                yield ResultEvent(type="result", result="The follow-up is complete.", session_id="ses_x")

    host = _TwoTurnHost()
    host.chat.provider = "opencode"
    host.chat.session_id = "ses_x"
    stream = ChatStream(prompt_text="first")
    host.active_stream = stream
    host._broker.register("chat-1", stream)
    await ChatStreaming(host).drive(
        chat_id="chat-1", project_id="project-1", prompt="first", images=None,
        turn_index=None, chat_meta=host.chat, stream=stream, is_retry=False,
        unattended=False,
    )

    assert host.turn == 2
    assert stream.moved_to_background is False
    assert len(host.announcements) == 1


@pytest.mark.asyncio
async def test_late_move_response_cannot_park_a_queued_followup(tmp_path):
    release_move = asyncio.Event()

    class _DelayedMove:
        async def move_to_background(self):
            await release_move.wait()
            return "moved"

    class _TwoTurnHost(_RecordingDriveHost):
        def __init__(self):
            super().__init__([], tmp_path)
            self.active_stream = None
            self.move_task = None
            self.turn = 0
            self._providers["chat-1"] = _DelayedMove()

        @staticmethod
        def _native_question_request_id(_pending_question):
            return ""

        async def stream_chat(self, chat_id, prompt, images=None, *, unattended=False, compact=False):
            del chat_id, prompt, images, unattended, compact
            self.turn += 1
            if self.turn == 1:
                self.move_task = asyncio.create_task(
                    ProjectChatManager.move_chat_to_background(self, "chat-1")
                )
                self.active_stream.enqueue("follow-up")
                yield ResultEvent(type="result", result="Moved it; output comes later.", session_id="ses_x")
            else:
                # Release only after the queued turn has started, proving the
                # provider response arrives after its boundary reset.
                release_move.set()
                yield ResultEvent(type="result", result="The follow-up is complete.", session_id="ses_x")

    host = _TwoTurnHost()
    host.chat.provider = "opencode"
    host.chat.session_id = "ses_x"
    stream = ChatStream(prompt_text="first")
    host.active_stream = stream
    host._broker.register("chat-1", stream)
    await ChatStreaming(host).drive(
        chat_id="chat-1", project_id="project-1", prompt="first", images=None,
        turn_index=None, chat_meta=host.chat, stream=stream, is_retry=False,
        unattended=False,
    )
    assert await host.move_task == "moved"
    assert host.turn == 2
    assert stream.moved_to_background is False
    assert len(host.announcements) == 1


@pytest.mark.asyncio
async def test_pending_move_response_does_not_delay_announce_parking(tmp_path):
    never_release = asyncio.Event()

    class _PendingMove:
        async def move_to_background(self):
            await never_release.wait()
            return "moved"

    class _OneTurnHost(_RecordingDriveHost):
        def __init__(self):
            super().__init__([], tmp_path)
            self.active_stream = None
            self.move_task = None
            self._providers["chat-1"] = _PendingMove()

        @staticmethod
        def _native_question_request_id(_pending_question):
            return ""

        async def stream_chat(self, chat_id, prompt, images=None, *, unattended=False, compact=False):
            del chat_id, prompt, images, unattended, compact
            self.move_task = asyncio.create_task(
                ProjectChatManager.move_chat_to_background(self, "chat-1")
            )
            # Let the move request synchronously mark the stream before the
            # provider blocks indefinitely.
            await asyncio.sleep(0)
            yield ResultEvent(type="result", result="Moved it; output comes later.", session_id="ses_x")

    host = _OneTurnHost()
    host.chat.provider = "opencode"
    host.chat.session_id = "ses_x"
    stream = ChatStream(prompt_text="first")
    host.active_stream = stream
    host._broker.register("chat-1", stream)
    try:
        await ChatStreaming(host).drive(
            chat_id="chat-1", project_id="project-1", prompt="first", images=None,
            turn_index=None, chat_meta=host.chat, stream=stream, is_retry=False,
            unattended=False,
        )
        assert host.announcements == []
        assert stream.moved_to_background is True
    finally:
        host.move_task.cancel()
        await asyncio.gather(host.move_task, return_exceptions=True)


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
    chat.instructions_inline_pending = True
    built: list[bool] = []
    monkeypatch.setattr(
        pcm, "build_agent_request", lambda *a, **k: built.append(True)
    )

    await asyncio.wait_for(
        pcm._drain_between_turns(chat.chat_id, project.project_id), 5
    )

    assert built == []
    assert chat.instructions_inline_pending is True
    ctx = ChatContext.for_web(chat.chat_id)
    assert len(pcm._transcripts._load_current(ctx, "opencode")["turns"]) == 1


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
        pcm._subagents._watch_opencode(chat.chat_id, project.project_id, []), 5
    )
    return published, wait


@pytest.mark.asyncio
async def test_opencode_watcher_does_not_count_background_shells_as_agents(tmp_path, monkeypatch):
    pcm, project, chat = _opencode_chat(tmp_path)
    provider = _live_provider(tmp_path, chat.session_id)

    published, wait = await _watch(pcm, project, chat, provider, monkeypatch, [1, 0])

    remaining = [e["remaining"] for e in published if e.get("type") == "chat_subagents_ready"]
    # The shell keeps the watcher polling (one wait) but is not an agent.
    assert remaining == [0]
    assert wait.await_count == 1


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
    assert remaining == [0]


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
async def test_opencode_watcher_keeps_a_parked_announce_for_the_live_drain(
    tmp_path, monkeypatch
):
    pcm, project, chat = _opencode_chat(tmp_path)
    calls = _announce_spy(pcm)
    provider = _live_provider(tmp_path, chat.session_id)
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)  # type: ignore[assignment]
    monkeypatch.setattr(provider, "read_live_collab_tree", AsyncMock(return_value=[]))
    monkeypatch.setattr(provider, "refresh_background_shells", AsyncMock(side_effect=[1, 0]))
    monkeypatch.setattr(provider, "wait_background_change", AsyncMock())
    pcm._park_result_announce(chat.chat_id, project.project_id, "T", "interim")
    drain = asyncio.ensure_future(asyncio.Event().wait())
    pcm._between_turn_drains[chat.chat_id] = drain  # type: ignore[assignment]
    try:
        await asyncio.wait_for(
            pcm._watch_subagent_completion(chat.chat_id, project.project_id), 5
        )
    finally:
        drain.cancel()

    assert calls == []
    assert chat.chat_id in pcm._parked_result_announce


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


def test_background_count_now_reads_the_live_opencode_provider(tmp_path):
    pcm, _project, chat = _opencode_chat(tmp_path)
    provider = _live_provider(tmp_path, chat.session_id)
    provider._background_shells = {"a"}
    provider._background_children = {"b", "c"}
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)  # type: ignore[assignment]
    pcm._subagents._background_agents_last[chat.chat_id] = 0  # stale published count

    assert pcm._background_count_now(chat.chat_id) == 3


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


@pytest.mark.asyncio
async def test_a_follow_up_that_leaves_background_work_restarts_the_watcher(tmp_path):
    host = _RecordingDriveHost([], tmp_path)
    started: list[tuple[str, str]] = []
    host._start_subagent_watcher = lambda c, p: started.append((c, p))  # type: ignore[method-assign]
    host._providers = {"chat-1": _FakeDrainService(outstanding=True)}  # type: ignore[assignment]
    host._background_count_now = lambda _c: 1  # type: ignore[attr-defined]
    host._last_drain = None
    streaming = ChatStreaming(host)  # type: ignore[arg-type]

    await streaming.drain_between_turns("chat-1", "project-1")

    assert started == [("chat-1", "project-1")]


class _FakeDrainService:
    def __init__(self, outstanding: bool) -> None:
        self.background_work_outstanding = outstanding

    async def drain_events(self):
        yield ResultEvent(
            type="result",
            result="BGDONE",
            session_id="s",
            origin="task-notification",
        )


@pytest.mark.asyncio
async def test_opencode_watcher_publishes_children_not_shells(tmp_path, monkeypatch):
    pcm, project, chat = _opencode_chat(tmp_path)
    provider = _live_provider(tmp_path, chat.session_id)
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)  # type: ignore[assignment]
    tree = [{"info": {"id": "ses_child"}, "active": True}]
    monkeypatch.setattr(provider, "read_live_collab_tree", AsyncMock(return_value=tree))
    # Two shells stay alive for two ticks, then exit; the child ends with them.
    monkeypatch.setattr(provider, "refresh_background_shells", AsyncMock(side_effect=[2, 2, 0]))
    ended: list = []

    async def wait(_timeout):
        if len(ended) == 1:
            tree[0]["active"] = False
        ended.append(True)

    monkeypatch.setattr(provider, "wait_background_change", wait)
    published = _capture_publish(pcm)
    work_seen: list[int] = []
    original = pcm._subagents.publish_count

    def spy(chat_id, project_id, count, *, work=None):
        work_seen.append(pcm._subagents.work_count(chat_id))
        original(chat_id, project_id, count, work=work)
        work_seen.append(pcm._subagents.work_count(chat_id))

    monkeypatch.setattr(pcm._subagents, "publish_count", spy)

    await asyncio.wait_for(
        pcm._subagents._watch_opencode(chat.chat_id, project.project_id, []), 5
    )

    remaining = [e["remaining"] for e in published if e.get("type") == "chat_subagents_ready"]
    assert remaining == [1, 0]
    # The watcher kept going while the shells were alive (three polls, not one).
    assert len(ended) == 2
    assert work_seen[1] == 3
