"""The needs-you runtime: start, poll, dismiss, continue and lifecycle cancel.

``make_manager`` and ``make_runtime`` are shared by the other needs-you tests.
The handlers here are scripted stand-ins registered under the ``restart`` kind,
so the runtime's own behaviour is tested without any provider.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ciao.chat_actions import _VALIDATORS, ChatActionError
from ciao.config import CiaoConfig
from ciao.models import AssistantTextDelta, ResultEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.chat_actions_base import ActionContext, ActionHandler
from ciao.web.chat_actions_runtime import (
    ACTION_HANDLERS,
    TIMEOUT_ERROR,
    ChatActionsRuntime,
)
from ciao.web.chat_broker import ChatStream
from ciao.web.chat_streaming import ChatStreaming, ChatStreamingHost
from ciao.web.project_chats import ChatActionConflictError, ChatInfo, ProjectChatManager
from tests.test_chat_streaming_boundary import _DriveHost


def make_manager(tmp_path: Path) -> ProjectChatManager:
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


def make_runtime(manager: ProjectChatManager, **overrides: float) -> ChatActionsRuntime:
    settings: dict[str, float] = {
        "poll_interval_s": 0.01,
        "poll_deadline_s": 5.0,
    }
    settings.update(overrides)
    runtime = ChatActionsRuntime(manager, **settings)
    manager.actions_runtime = runtime
    return runtime


async def until(predicate: Any, timeout: float = 1.0) -> None:
    """Wait for ``predicate()`` to hold, polling briefly, or fail."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def _state(manager: ProjectChatManager, chat: ChatInfo, action_id: str) -> str:
    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    return action.state


class ScriptedHandler(ActionHandler):
    """A handler whose start and poll steps a test supplies; records start, poll and cancel calls."""

    start_states = frozenset({"open"})
    poll_states = frozenset({"waiting"})

    def __init__(
        self,
        *,
        on_start: Callable[[ActionContext, Any], Any] | None = None,
        on_poll: Callable[[ActionContext, Any], Any] | None = None,
    ) -> None:
        self._on_start = on_start
        self._on_poll = on_poll
        self.starts = 0
        self.polls = 0
        self.cancelled: list[str] = []

    async def start(self, ctx: ActionContext, action: Any, body: Any) -> None:
        self.starts += 1
        if self._on_start is not None:
            await self._on_start(ctx, action)
        else:
            ctx.pcm.update_action(ctx.chat.chat_id, action.id, state="waiting")

    async def poll(self, ctx: ActionContext, action: Any) -> None:
        self.polls += 1
        if self._on_poll is not None:
            await self._on_poll(ctx, action)

    async def cancel(self, ctx: ActionContext, action: Any) -> None:
        del ctx
        self.cancelled.append(action.id)

    def wake_prompt(self, action: Any) -> str:
        del action
        return "the user finished the step"


def scripted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler: ActionHandler, **runtime_settings: float
) -> tuple[ProjectChatManager, ChatInfo, ChatActionsRuntime, str]:
    """A chat with one open ``restart`` card whose handler is ``handler``."""
    monkeypatch.setitem(ACTION_HANDLERS, "restart", handler)
    monkeypatch.setitem(_VALIDATORS, "restart", lambda raw: {})
    manager = make_manager(tmp_path)
    chat = manager.create_chat(manager.create_project("P", workspace="work").project_id)
    runtime = make_runtime(manager, **runtime_settings)
    payload = manager.raise_action(chat.chat_id, "restart", {}, origin="engine")
    assert payload is not None
    return manager, chat, runtime, str(payload["id"])


# ── Start, poll, dismiss ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_runs_the_handler_and_polls_until_it_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def finish_on_second_tick(ctx: ActionContext, action: Any) -> None:
        if handler.polls >= 2:
            ctx.pcm.update_action(ctx.chat.chat_id, action.id, state="done")

    handler = ScriptedHandler(on_poll=finish_on_second_tick)
    manager, chat, runtime, action_id = scripted(tmp_path, monkeypatch, handler, poll_deadline_s=600.0)

    payload = await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    assert payload["state"] == "waiting"
    assert action_id in runtime._polls
    await until(lambda: _state(manager, chat, action_id) == "done" and action_id not in runtime._polls)
    assert handler.polls >= 2


@pytest.mark.asyncio
async def test_start_while_waiting_is_an_invalid_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, chat, runtime, action_id = scripted(
        tmp_path, monkeypatch, ScriptedHandler(), poll_deadline_s=600.0
    )
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    with pytest.raises(ChatActionError) as info:
        await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    assert info.value.reason == "invalid_state"
    runtime._polls[action_id].cancel()


@pytest.mark.asyncio
async def test_start_states_and_poll_states_come_from_the_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class WorkingHandler(ScriptedHandler):
        start_states = frozenset({"working"})
        poll_states = frozenset({"working"})

        async def start(self, ctx: ActionContext, action: Any, body: Any) -> None:
            del ctx, action, body

    manager, chat, runtime, action_id = scripted(
        tmp_path, monkeypatch, WorkingHandler(), poll_deadline_s=600.0
    )

    with pytest.raises(ChatActionError) as info:
        await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)
    assert info.value.reason == "invalid_state"

    manager.update_action(chat.chat_id, action_id, state="working")
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    assert action_id in runtime._polls
    runtime._polls[action_id].cancel()


def test_the_context_carries_app_singletons(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = manager.create_chat(manager.create_project("P", workspace="work").project_id)
    runtime = make_runtime(manager)

    bare = runtime.context(chat.chat_id)
    assert bare is not None
    assert bare.app_state is None
    assert bare.terminals is None

    sentinel = object()
    app_state = SimpleNamespace(terminal_sessions=sentinel)
    runtime.bind_app_state(app_state)
    ctx = runtime.context(chat.chat_id)
    assert ctx is not None
    assert ctx.terminals is sentinel
    assert ctx.app_state is app_state
    assert ctx.agent_root == manager._agent_root_for_chat(chat.chat_id)


@pytest.mark.asyncio
async def test_continue_cancels_before_removing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = ScriptedHandler()
    manager, chat, runtime, action_id = scripted(tmp_path, monkeypatch, handler)
    monkeypatch.setattr(manager, "_deliver_wake", lambda *a, **k: "started")
    manager.update_action(chat.chat_id, action_id, state="done")

    await runtime.continue_action(chat.chat_id, action_id)

    assert handler.cancelled == [action_id]
    assert manager.get_action(chat.chat_id, action_id) is None


@pytest.mark.asyncio
async def test_the_base_handler_refuses_a_callback() -> None:
    with pytest.raises(ChatActionError) as info:
        await ActionHandler().callback(cast(Any, None), cast(Any, None), "x")
    assert info.value.reason == "unsupported_kind"


@pytest.mark.asyncio
async def test_start_of_an_unknown_action_conflicts(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    chat = manager.create_chat(manager.create_project("P", workspace="work").project_id)
    runtime = make_runtime(manager)

    with pytest.raises(ChatActionConflictError):
        await runtime.start(chat.chat_id, "missing", {}, is_loopback=True)


@pytest.mark.asyncio
async def test_a_poll_that_fails_the_action_ends_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(ctx: ActionContext, action: Any) -> None:
        ctx.pcm.update_action(ctx.chat.chat_id, action.id, state="failed", error="The sign-in did not finish.")

    manager, chat, runtime, action_id = scripted(
        tmp_path, monkeypatch, ScriptedHandler(on_poll=fail), poll_deadline_s=600.0
    )
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    await until(lambda: _state(manager, chat, action_id) == "failed" and action_id not in runtime._polls)

    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    assert action.error == "The sign-in did not finish."


@pytest.mark.asyncio
async def test_polling_times_out_after_the_deadline_and_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = ScriptedHandler()
    manager, chat, runtime, action_id = scripted(tmp_path, monkeypatch, handler, poll_deadline_s=0.05)
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    await until(lambda: _state(manager, chat, action_id) == "failed")

    action = manager.get_action(chat.chat_id, action_id)
    assert action is not None
    assert action.error == TIMEOUT_ERROR
    assert handler.cancelled == [action_id]


@pytest.mark.asyncio
async def test_a_dismissal_stops_polling_cancels_and_remembers_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = ScriptedHandler()
    manager, chat, runtime, action_id = scripted(tmp_path, monkeypatch, handler, poll_deadline_s=600.0)
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    remaining = await runtime.dismiss(chat.chat_id, action_id)

    assert remaining == []
    assert action_id not in runtime._polls
    assert handler.cancelled == [action_id]
    assert "restart" in chat.dismissed_action_keys


@pytest.mark.asyncio
async def test_continue_wakes_the_chat_and_removes_the_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    woken: list[dict[str, object]] = []

    def record_wake(parent: ChatInfo, prompt: str, *, count: int, origin: str) -> str:
        woken.append({"chat": parent.chat_id, "prompt": prompt, "count": count, "origin": origin})
        return "started"

    manager, chat, runtime, action_id = scripted(tmp_path, monkeypatch, ScriptedHandler())
    monkeypatch.setattr(manager, "_deliver_wake", record_wake)
    manager.update_action(chat.chat_id, action_id, state="done")

    remaining, delivery = await runtime.continue_action(chat.chat_id, action_id)

    assert (remaining, delivery) == ([], "started")
    assert woken == [
        {"chat": chat.chat_id, "prompt": "the user finished the step", "count": 1, "origin": "chat_action"}
    ]
    assert chat.dismissed_action_keys == set()


@pytest.mark.asyncio
async def test_continue_before_done_is_an_invalid_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, chat, runtime, action_id = scripted(tmp_path, monkeypatch, ScriptedHandler())

    with pytest.raises(ChatActionError) as info:
        await runtime.continue_action(chat.chat_id, action_id)

    assert info.value.reason == "invalid_state"


@pytest.mark.asyncio
async def test_a_dismissal_during_start_conflicts_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()

    async def slow_start(ctx: ActionContext, action: Any) -> None:
        ctx.pcm.update_action(ctx.chat.chat_id, action.id, state="working")
        await gate.wait()

    manager, chat, runtime, action_id = scripted(
        tmp_path, monkeypatch, ScriptedHandler(on_start=slow_start), poll_deadline_s=600.0
    )
    start = asyncio.create_task(runtime.start(chat.chat_id, action_id, {}, is_loopback=True))
    await until(lambda: _state(manager, chat, action_id) == "working")

    await runtime.dismiss(chat.chat_id, action_id)
    gate.set()

    with pytest.raises(ChatActionConflictError):
        await start
    assert action_id not in runtime._polls


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["new_session", "handover", "archive"])
async def test_a_lifecycle_clear_cancels_a_waiting_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lifecycle: str
) -> None:
    handler = ScriptedHandler()
    manager, chat, runtime, action_id = scripted(tmp_path, monkeypatch, handler, poll_deadline_s=600.0)
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    if lifecycle == "new_session":
        manager.new_session(chat.chat_id)
    elif lifecycle == "handover":
        manager.handover_chat(chat.chat_id, provider="claude", model="haiku")
    else:
        await manager.archive_chat(chat.chat_id)

    await until(lambda: handler.cancelled == [action_id])
    assert action_id not in runtime._polls


@pytest.mark.asyncio
async def test_a_poll_tick_that_raises_does_not_end_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def flaky(ctx: ActionContext, action: Any) -> None:
        if handler.polls == 1:
            raise RuntimeError("one bad tick")
        if handler.polls >= 3:
            ctx.pcm.update_action(ctx.chat.chat_id, action.id, state="done")

    handler = ScriptedHandler(on_poll=flaky)
    manager, chat, runtime, action_id = scripted(tmp_path, monkeypatch, handler, poll_deadline_s=600.0)
    await runtime.start(chat.chat_id, action_id, {}, is_loopback=True)

    await until(lambda: _state(manager, chat, action_id) == "done" and action_id not in runtime._polls)


@pytest.mark.asyncio
async def test_a_hanging_cancel_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ciao.web.chat_actions_runtime as runtime_module

    class HangingHandler(ScriptedHandler):
        async def cancel(self, ctx: ActionContext, action: Any) -> None:
            del ctx, action
            await asyncio.sleep(10)

    monkeypatch.setattr(runtime_module, "CANCEL_TIMEOUT_S", 0.05)
    manager, chat, runtime, action_id = scripted(tmp_path, monkeypatch, HangingHandler())

    started = time.monotonic()
    remaining = await runtime.dismiss(chat.chat_id, action_id)

    assert time.monotonic() - started < 1.0
    assert remaining == []


@pytest.mark.asyncio
async def test_a_finished_turn_starts_no_mcp_detection(tmp_path: Path) -> None:
    """A turn ends without starting MCP detection or raising any card."""
    host = _DriveHost(
        [
            AssistantTextDelta(type="text", text="hello"),
            ResultEvent(
                type="result",
                result="done",
                session_id="session-1",
                effective_model="opus",
                usage={"input_tokens": "3", "output_tokens": "5"},
                quota={"five_hour": "7"},
            ),
        ],
        tmp_path,
    )
    host._providers["chat-1"] = SimpleNamespace(
        provider=SimpleNamespace(is_connected=True)
    )
    streaming = ChatStreaming(cast(ChatStreamingHost, host))
    stream = ChatStream(prompt_text="hello")
    host._broker.register("chat-1", stream)

    await streaming.drive(
        chat_id="chat-1",
        project_id="project-1",
        prompt="hello",
        images=None,
        turn_index=None,
        chat_meta=host.chat,
        stream=stream,
        is_retry=False,
        unattended=False,
    )

    assert host.chat.actions == []
    assert not [name for name in host.detached_names if name.startswith("chat-actions-detect-")]
