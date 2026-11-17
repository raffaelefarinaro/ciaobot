"""#3172: teardown routes refuse while a chat's Stop teardown is unsettled."""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.chat_broker import ChatStream
from ciao.web.project_chats import (
    _TEARDOWN_CONFLICT,
    ChatTeardownConflict,
    ProjectChatManager,
)
from ciao.web.routes_api import chat_detail, project_detail

from .conftest import attach_stub_mcp


class _StubProvider:
    """A cached provider that records a teardown."""

    can_drain = False
    provider = None
    background_work_outstanding = False
    background_activity_age = math.inf

    def __init__(self) -> None:
        self.disconnected = 0

    async def disconnect(self) -> None:
        self.disconnected += 1


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return attach_stub_mcp(
        ProjectChatManager(
            config,
            state_store=StateStore(config.state_path, tmp_path, config.media_root),
            transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
            path=runtime / "web_projects.json",
        )
    )


def _chat(pcm: ProjectChatManager, name: str = "teardown"):
    project = pcm.create_project(name, workspace="personal")
    chat = pcm.create_chat(project.project_id, title=name, provider="claude")
    return project, chat


async def _handoff(pcm: ProjectChatManager, chat_id: str):
    """A background Stop whose provider abort is still running."""
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def finish() -> None:
        entered.set()
        await gate.wait()

    task = pcm._streaming.begin_background_stop(chat_id, finish)
    await asyncio.wait_for(entered.wait(), 5)
    return gate, task


async def _call(pcm: ProjectChatManager, op: str, chat_id: str) -> Any:
    if op == "delete_chat":
        return pcm.delete_chat(chat_id)
    if op == "handover_chat":
        chat = pcm.get_chat(chat_id)
        assert chat is not None
        return pcm.handover_chat(chat_id, provider="claude", model=chat.model or "sonnet")
    if op == "archive_chat":
        return await pcm.archive_chat(chat_id)
    assert op == "new_session"
    return pcm.new_session(chat_id)


OPS = ["delete_chat", "handover_chat", "archive_chat", "new_session"]


@pytest.mark.parametrize("op", OPS)
async def test_teardown_is_refused_while_a_stop_handoff_is_pending(
    tmp_path: Path, op: str
) -> None:
    pcm = _make_manager(tmp_path)
    _project, chat = _chat(pcm)
    provider = _StubProvider()
    pcm._providers[chat.chat_id] = provider  # type: ignore[assignment]
    gate, task = await _handoff(pcm, chat.chat_id)
    try:
        with pytest.raises(ChatTeardownConflict) as refused:
            await _call(pcm, op, chat.chat_id)
        assert str(refused.value) == _TEARDOWN_CONFLICT
        assert pcm.get_chat(chat.chat_id) is not None
        assert chat.chat_id in pcm._providers
        assert provider.disconnected == 0
        gate.set()
        await asyncio.wait_for(task, 5)
        assert pcm._streaming.teardown_unsettled(chat.chat_id) is False
        await _call(pcm, op, chat.chat_id)
    finally:
        gate.set()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, 5)


async def test_delete_is_refused_while_a_drive_is_waiting_on_its_predecessor(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    _project, chat = _chat(pcm)
    provider = _StubProvider()
    pcm._providers[chat.chat_id] = provider  # type: ignore[assignment]
    stream = ChatStream(prompt_text="waiting")
    pcm._streaming._pre_entry[chat.chat_id] = (stream, asyncio.Event())
    with pytest.raises(ChatTeardownConflict):
        pcm.delete_chat(chat.chat_id)
    assert pcm.get_chat(chat.chat_id) is not None
    assert provider.disconnected == 0
    pcm._streaming._pre_entry.pop(chat.chat_id)
    assert pcm.delete_chat(chat.chat_id) is True
    assert pcm.get_chat(chat.chat_id) is None


async def _idle_drain(gate: asyncio.Event | None = None) -> asyncio.Task[None]:
    async def drain() -> None:
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if gate is None or gate.is_set():
                    raise
                # A hung cleanup: ignore this cancel until the gate opens.
                await asyncio.shield(gate.wait())
                raise

    task = asyncio.create_task(drain())
    await asyncio.sleep(0)
    return task


async def test_an_idle_or_cancelling_drain_never_blocks_delete(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    _project, idle = _chat(pcm, "idle")
    _project2, cancelling = _chat(pcm, "cancelling")
    _project3, hung = _chat(pcm, "hung")
    gate = asyncio.Event()
    tasks = {
        idle.chat_id: await _idle_drain(),
        cancelling.chat_id: await _idle_drain(gate),
        hung.chat_id: await _idle_drain(gate),
    }
    for chat_id, task in tasks.items():
        pcm._streaming.between_turn_drains[chat_id] = task
    tasks[cancelling.chat_id].cancel()  # cancelling() > 0, no handoff, no slot
    await asyncio.sleep(0)
    try:
        assert tasks[cancelling.chat_id].cancelling() > 0
        assert pcm._streaming.teardown_unsettled(idle.chat_id) is False
        assert pcm._streaming.teardown_unsettled(cancelling.chat_id) is False
        for chat in (idle, cancelling, hung):
            assert pcm.delete_chat(chat.chat_id) is True
            assert pcm.get_chat(chat.chat_id) is None
    finally:
        gate.set()
        for task in tasks.values():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, 5)


async def test_a_hung_drain_cannot_hold_the_stop_handoff_forever(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("ciao.web.chat_streaming._STOP_HANDOFF_DRAIN_TIMEOUT_S", 0.05)
    pcm = _make_manager(tmp_path)
    _project, chat = _chat(pcm)
    gate = asyncio.Event()
    drain = await _idle_drain(gate)
    pcm._streaming.between_turn_drains[chat.chat_id] = drain
    finished: list[str] = []

    async def finish() -> None:
        finished.append(chat.chat_id)

    task = pcm._streaming.begin_background_stop(chat.chat_id, finish)
    try:
        assert pcm._streaming.teardown_unsettled(chat.chat_id) is True
        with pytest.raises(ChatTeardownConflict):
            pcm.delete_chat(chat.chat_id)
        await asyncio.wait_for(task, 5)
        # The drain still ignores its cancel, yet the handoff has let go.
        assert not drain.done()
        assert finished == [chat.chat_id]
        assert pcm._streaming.teardown_unsettled(chat.chat_id) is False
        assert pcm.delete_chat(chat.chat_id) is True
        assert pcm.get_chat(chat.chat_id) is None
    finally:
        gate.set()
        drain.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(drain, 5)


async def test_archive_refusal_after_the_transcript_await_mutates_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    pcm = _make_manager(tmp_path)
    _project, chat = _chat(pcm)
    chat.retry_status = "pending"
    chat.retry_prompt = "replay"
    provider = _StubProvider()
    pcm._providers[chat.chat_id] = provider  # type: ignore[assignment]
    reading = threading.Event()
    release = threading.Event()
    revoked: list[str] = []
    monkeypatch.setattr(pcm, "_revoke_mcp_chat", lambda chat_id: revoked.append(chat_id))

    def gated_read(_ctx, _chat):
        reading.set()
        release.wait(5)
        return 0, None

    monkeypatch.setattr(pcm, "_read_archive_inputs", gated_read)
    archiving = asyncio.create_task(pcm.archive_chat(chat.chat_id))
    try:
        while not reading.is_set():
            await asyncio.sleep(0.01)
        # A Stop teardown lands while the transcript is being read.
        pcm._streaming._pre_entry[chat.chat_id] = (
            ChatStream(prompt_text="late"),
            asyncio.Event(),
        )
        release.set()
        with pytest.raises(ChatTeardownConflict):
            await asyncio.wait_for(archiving, 5)
        assert revoked == []
        assert chat.retry_status == "pending" and chat.retry_prompt == "replay"
        assert chat.chat_id in pcm._providers and provider.disconnected == 0
        assert chat.archived is False
    finally:
        release.set()
        pcm._streaming._pre_entry.pop(chat.chat_id, None)
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(archiving, 5)


def _request(method: str, path: str, params: dict[str, str], pcm) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "app": SimpleNamespace(
                state=SimpleNamespace(config=pcm._config, project_chat_manager=pcm)
            ),
            "path_params": params,
            "query_string": b"",
        }
    )


async def test_http_delete_answers_409_with_the_fixed_message(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project, chat = _chat(pcm)
    gate, task = await _handoff(pcm, chat.chat_id)
    try:
        response = await chat_detail(
            _request("DELETE", f"/api/chats/{chat.chat_id}", {"chat_id": chat.chat_id}, pcm)
        )
        assert response.status_code == 409
        assert json.loads(response.body) == {"error": _TEARDOWN_CONFLICT}
        assert pcm.get_chat(chat.chat_id) is not None

        response = await project_detail(
            _request(
                "DELETE",
                f"/api/projects/{project.project_id}",
                {"project_id": project.project_id},
                pcm,
            )
        )
        assert response.status_code == 409
        assert json.loads(response.body) == {"error": _TEARDOWN_CONFLICT}
        assert pcm.get_project(project.project_id) is not None
        assert pcm.get_chat(chat.chat_id) is not None
    finally:
        gate.set()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, 5)


async def test_project_complete_and_workspace_archive_refuse_before_any_mutation(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project, chat = _chat(pcm)
    gate, task = await _handoff(pcm, chat.chat_id)
    try:
        with pytest.raises(ChatTeardownConflict):
            pcm.complete_project(project.project_id)
        with pytest.raises(ChatTeardownConflict):
            pcm.archive_workspace_projects("personal")
        assert pcm.get_project(project.project_id) is not None
        assert pcm.get_chat(chat.chat_id) is not None
    finally:
        gate.set()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, 5)


async def test_reload_and_the_reaper_skip_an_unsettled_chat(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    _project, chat = _chat(pcm)
    provider = _StubProvider()
    pcm._providers[chat.chat_id] = provider  # type: ignore[assignment]
    pcm._provider_last_used[chat.chat_id] = time.monotonic() - (
        pcm._provider_idle_timeout + 1
    )
    gate, task = await _handoff(pcm, chat.chat_id)
    try:
        assert await pcm.reload_provider(chat.chat_id) is False
        assert await pcm.reap_idle_providers() == []
        assert chat.chat_id in pcm._providers and provider.disconnected == 0
        gate.set()
        await asyncio.wait_for(task, 5)
        # A busy sweep refreshes the idle stamp; age it again for the real reap.
        pcm._provider_last_used[chat.chat_id] = time.monotonic() - (
            pcm._provider_idle_timeout + 1
        )
        assert await pcm.reap_idle_providers() == [chat.chat_id]
    finally:
        gate.set()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, 5)
