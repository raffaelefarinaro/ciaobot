"""The drive loop's reload after an agent ran `ciao chat reload` mid-turn."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ciao.cli_envelopes import RELOAD_CONTINUE_PROMPT
from ciao.models import ResultEvent
from tests.test_code_mode_continuation import _switch_on
from tests.test_project_chats_queue import _make_manager


def _result(chat, *, is_error: bool = False) -> ResultEvent:
    return ResultEvent(
        type="result",
        result="assistant answer",
        session_id="sess-x",
        is_error=is_error,
        effective_model=chat.model,
        usage={},
        quota={},
        cost_usd=0.0,
    )


class _FakeProvider:
    background_work_outstanding = False
    background_activity_age = 0.0

    def __init__(self) -> None:
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True


async def _run(tmp_path: Path, first_turn, *, queue_during_first: str | None = None):
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-reload", workspace="work")
    chat = pcm.create_chat(project.project_id, title="reload")
    provider = _FakeProvider()
    pcm._providers[chat.chat_id] = provider  # type: ignore[assignment]
    turn_calls: list[str] = []
    seen_at_turn: list[tuple[bool, bool]] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        turn_calls.append(prompt)
        seen_at_turn.append((provider.disconnected, chat_id in pcm._providers))
        is_error = False
        if len(turn_calls) == 1:
            is_error = bool(first_turn(pcm, chat_id))
            if queue_during_first:
                assert pcm.queue_message(chat_id, queue_during_first) is True
        yield _result(chat, is_error=is_error)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    captured: list[dict] = []

    async def consume(stream) -> None:
        async for ev in stream.subscribe():
            captured.append(ev)

    stream = pcm.start_stream(chat.chat_id, "initial")
    await asyncio.wait_for(asyncio.create_task(consume(stream)), timeout=5.0)
    return pcm, chat, provider, turn_calls, seen_at_turn, captured


def _arm(pcm, cid):
    assert pcm.arm_reload_continuation(cid) is True


async def test_reload_runs_one_hidden_continuation_after_disconnecting(tmp_path: Path) -> None:
    pcm, chat, provider, turn_calls, seen, captured = await _run(tmp_path, _arm)
    assert turn_calls == ["initial", RELOAD_CONTINUE_PROMPT]
    assert len([e for e in captured if e.get("type") == "user_echo"]) == 1
    assert provider.disconnected is True
    assert seen[1] == (True, False)
    assert chat.user_turn_count == 1


async def test_reload_with_a_queued_message_reloads_without_a_hidden_turn(
    tmp_path: Path,
) -> None:
    _, _, provider, turn_calls, seen, _ = await _run(
        tmp_path, _arm, queue_during_first="msg A"
    )
    assert turn_calls == ["initial", "msg A"]
    assert RELOAD_CONTINUE_PROMPT not in turn_calls
    assert provider.disconnected is True
    assert seen[1] == (True, False)


async def test_no_reload_after_a_user_stop(tmp_path: Path) -> None:
    def first(pcm, cid):
        _arm(pcm, cid)
        pcm.get_active_stream(cid).user_stopped = True

    _, _, provider, turn_calls, _, _ = await _run(tmp_path, first)
    assert turn_calls == ["initial"]
    assert provider.disconnected is False


async def test_reload_skipped_while_background_agents_run(tmp_path: Path) -> None:
    def first(pcm, cid):
        _arm(pcm, cid)
        pcm._subagents._background_agents_last[cid] = 1

    pcm, _, provider, turn_calls, _, _ = await _run(tmp_path, first)
    assert turn_calls == ["initial"]
    assert provider.disconnected is False


async def test_code_mode_and_reload_together_run_one_hidden_turn(tmp_path: Path) -> None:
    def first(pcm, cid):
        _switch_on(pcm, cid)
        _arm(pcm, cid)

    _, _, _, turn_calls, _, _ = await _run(tmp_path, first)
    assert len(turn_calls) == 2
    assert RELOAD_CONTINUE_PROMPT not in turn_calls


async def test_a_stop_during_the_reload_runs_no_hidden_turn(tmp_path: Path) -> None:
    """Stop pressed while the provider disconnects: the between-turns stop check (#1109) runs no hidden turn."""
    holder: dict = {}

    def first(pcm, cid):
        _arm(pcm, cid)
        stream = pcm.get_active_stream(cid)
        holder["stream"] = stream
        provider = pcm._providers[cid]

        async def stopping_disconnect() -> None:
            provider.disconnected = True
            stream.user_stopped = True

        provider.disconnect = stopping_disconnect

    _, _, provider, turn_calls, _, _ = await _run(tmp_path, first)
    assert turn_calls == ["initial"]
    assert provider.disconnected is True
    assert holder["stream"].user_stopped is False
